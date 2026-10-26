"""Selected-library OpenSubsonic enumerations below catalog discovery handlers.

Visibility never authorizes a song: direct media, favorites, history, queues and
playlists remain Navidrome reads. Explicit unsaved album navigation remains discovery.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol
from urllib.parse import urlencode

import anyio

from shijhon.access import AccessDenied, AccessService, AccessUnavailable, Principal
from shijhon.collection.contracts import REAL_AUDIO, IndexState, Mode, Selection
from shijhon.collection.query_index import (
    Binding,
    IndexFailed,
    IndexPending,
    Page,
    QueryIndex,
    SetupRequired,
    SourcePage,
    digest,
)
from shijhon.proxy.app import ProxyApp, RequestContext
from shijhon.proxy.auth import Caller, CredentialChecker, Refusal
from shijhon.proxy.params import RestCall, folders_read, header
from shijhon.proxy.responses import callback_refused, encoded
from shijhon.proxy.upstream import Upstream
from shijhon.views.answers import LibraryAnswer, Shown, raw_library_answer
from shijhon.views.ids import ABSENT

# What keeps a library view from being given (``Visibility.failure`` answers each).
FAILURES = (
    AccessDenied,
    SetupRequired,
    AccessUnavailable,
    IndexFailed,
    IndexPending,
    ValueError,
    sqlite3.Error,
    OSError,
)
# How long a list or search of an account that gets real audio only waits for its first
# generation (``QueryIndex.ensure``) instead of ``Budgets.wait_seconds``: an analyzer reads
# the whole library at once, and like apps it does not ask again after an answer that says
# the view is being prepared.
REAL_AUDIO_WAIT_SECONDS = 30.0


class Collections(Protocol):
    async def mode(self, owner_id: str) -> Mode: ...
    async def index_state(self, owner_id: str, library_id: str) -> IndexState: ...


# method -> (container, row key, kind, count parameter, offset parameter)
_LISTS = {
    "getAlbumList": ("albumList", "album", "album", "size", "offset"),
    "getAlbumList2": ("albumList2", "album", "album", "size", "offset"),
    "getSongsByGenre": ("songsByGenre", "song", "song", "count", "offset"),
    "getAlbum": ("album", "song", "song", "", ""),
    "getArtist": ("artist", "album", "album", "", ""),
    "getMusicDirectory": ("directory", "child", "directory", "", ""),
    "getArtists": ("artists", "artist", "artist", "", ""),
    "getIndexes": ("indexes", "artist", "artist", "", ""),
    "getGenres": ("genres", "genre", "genre", "", ""),
    "getRandomSongs": ("randomSongs", "song", "song", "size", ""),
}
_SUPPORTED = frozenset(_LISTS) | {"search2", "search3", "getScanStatus"}


_PRIVATE = {"u", "p", "t", "s", "jwt", "apiKey", "f", "callback", "c"}
# What Navidrome reads of a search besides its counts and offsets.
_SEARCH_READ = frozenset({"query", "musicFolderId"})
_SEARCH_COUNTS = {
    "artistCount",
    "albumCount",
    "songCount",
    "artistOffset",
    "albumOffset",
    "songOffset",
}


def number(value: str | None, default: int, maximum: int = 10_000_000) -> int:
    try:
        return max(0, min(int(value or str(default)), maximum))
    except ValueError:
        return default


def query_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Canonical names, with every repeated value kept in its original order.

    Native scalar parameters use their first value; genuine multi-values also retain
    all values. Sorting whole pairs would alias requests with different first values.
    """
    return sorted(pairs, key=lambda pair: pair[0])


def changed(call: RestCall, values: Mapping[str, str], *, name: str | None = None) -> RestCall:
    pairs = [(k, v) for k, v in call.params if k not in values]
    pairs.extend(values.items())
    return RestCall.build(
        name or call.name,
        "GET",
        ("/rest/" + (name or call.name)).encode(),
        urlencode(pairs).encode(),
        call.headers,
        None,
    )


def representation(call: RestCall) -> RestCall:
    """Read a bounded body internally, then frame/suppress it at the reply boundary."""
    if call.fmt == "jsonp":
        return changed(call, {"f": "json"})
    return changed(call, {}) if call.http_method == "HEAD" else call


def response(
    call: RestCall,
    status: int,
    payload: dict[str, Any],
    *,
    total: int | None = None,
    selected: bool = True,
    original_songs: Mapping[str, frozenset[str]] | None = None,
    shown: Shown | None = None,
) -> LibraryAnswer:
    server = {
        k: v
        for k, v in payload.items()
        if k in ("version", "type", "serverVersion", "openSubsonic")
    }
    data = {k: v for k, v in payload.items() if k not in (*server, "status")}
    content_type, body = encoded(
        call, "ok" if status == 200 and "error" not in data else "failed", data, server
    )
    headers = [(b"content-type", content_type), (b"cache-control", b"no-store")]
    if total is not None:
        headers.append((b"x-total-count", str(total).encode()))
    if status == 503:
        headers.append((b"retry-after", b"1"))
    return LibraryAnswer(
        status, headers, body, selected_view=selected, original_songs=original_songs, shown=shown
    )


def selected_native(answer: LibraryAnswer, total: int, *, selected: bool = True) -> LibraryAnswer:
    """Keep exact native bytes with private transport headers.

    Complete album details are discovery, so their reply must still allow the
    complete-album handler to add a saved plan's unmaterialized tracks.
    """
    content_type = header(answer.headers, b"content-type").split(";", 1)[0].encode()
    return LibraryAnswer(
        answer.status,
        [
            (b"content-type", content_type),
            (b"cache-control", b"no-store"),
            (b"x-total-count", str(total).encode()),
        ],
        answer.body,
        selected_view=selected,
    )


def external_failure(call: RestCall, answer: LibraryAnswer | None) -> LibraryAnswer | None:
    if answer is None or call.fmt != "jsonp" or answer.status != 200:
        return answer
    data = json.loads(answer.body)
    payload = data.get("subsonic-response") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        raise IndexFailed("The native response is invalid.")
    return response(call, answer.status, payload)


class Visibility:
    def __init__(
        self,
        upstream: Upstream,
        checker: CredentialChecker,
        access: AccessService,
        collection: Collections,
        index: QueryIndex,
        *,
        token_for: Callable[[Principal], Awaitable[str | None]] | None = None,
        artist_participations: bool = False,
        complete_albums: bool = False,
    ) -> None:
        self.upstream, self.checker, self.access, self.collection, self.index = (
            upstream,
            checker,
            access,
            collection,
            index,
        )
        self.token_for = token_for
        self.artist_count_role = "maincredit" if artist_participations else "albumartist"
        self.complete_albums = complete_albums
        self.prepare_foreign: Callable[[Binding, Principal, str | None], Awaitable[None]] | None = (
            None
        )
        # Whether a user name is one of [placeholders] real_audio_accounts: that account's
        # lists, searches and pages hold only songs whose audio is in the library.
        self.real_audio: Callable[[str], bool] = lambda username: False
        # A verified account reads its library: its favorites are read when due, with
        # its request's credentials (``collection.reasons.Reasons.due``).
        self.learn: Callable[[Principal, RestCall], Awaitable[None]] | None = None

    def attach(self, proxy: ProxyApp) -> None:
        """Call after existing catalog/audio handlers have been installed."""
        self.upstream.library_answer = self.answer
        proxy.final_library_answer = self.final_answer

    def whole_albums(self, call: RestCall, mode: Selection | None = None) -> bool:
        """``[fill] complete_lists``: the ID3 library lists' JSON and XML GET count an album
        listed with all of its original songs as Navidrome does, the songs Shijhon added
        to it (a fill's placeholders) included; HEAD and JSONP keep the selected counts,
        and an account that gets real audio only its own."""
        return (
            self.complete_albums
            and mode != REAL_AUDIO
            and call.name in ("getAlbumList2", "search3")
            and call.fmt != "jsonp"
            and call.http_method != "HEAD"
        )

    @staticmethod
    def _patience(mode: Selection | None) -> float | None:
        return REAL_AUDIO_WAIT_SECONDS if mode == REAL_AUDIO else None

    @staticmethod
    def applicable(call: RestCall) -> bool:
        return (
            call.name in _SUPPORTED
            and call.http_method in ("GET", "HEAD", "POST")
            and not call.catalog_discovery
            and not callback_refused(call)
            and not (call.get("id") or "").startswith("sh.")
        )

    async def final_answer(self, call: RestCall, ctx: RequestContext) -> LibraryAnswer | None:
        if not self.applicable(call) or (caller := await ctx.caller()) is None:
            return None  # the proxy retains the first credential refusal or native bypass
        return await self.answer(call, caller=caller)

    async def binding(
        self, principal: Principal, requested: frozenset[str] | None = None
    ) -> Binding:
        granted = frozenset(str(i) for i in principal.library_ids)
        libraries = granted if requested is None else granted & requested
        if requested is not None and not requested <= granted:
            raise AccessDenied
        generations = []
        configured = str(self.access.navidrome.library_id)
        for library in sorted(libraries & {configured}):
            state = await self.index.source_state(library)
            if not state.generation or state.updated_at <= 0:
                if state.state == "indexing":
                    raise IndexPending(state.indexed)
                raise SetupRequired
            generations.append((library, state.generation))
        authority = digest(
            (principal.user_id, principal.username, principal.is_admin, sorted(granted))
        )
        return Binding(principal.user_id, libraries, tuple(generations), authority)

    async def answer(self, call: RestCall, *, caller: Caller | None = None) -> LibraryAnswer | None:
        if not self.applicable(call):
            return None
        verdict = caller if caller is not None else await self.checker.check(call, fresh=True)
        if isinstance(verdict, Refusal):
            return LibraryAnswer(verdict.status, verdict.headers, verdict.body, selected_view=True)
        if not isinstance(verdict, Caller):
            return None  # no supplied credentials: Navidrome decides the answer
        real = self.real_audio(verdict.username)
        try:
            principal = await self.access.verify(verdict.username)
            if not principal.can_access(self.access.navidrome.library_id):
                return None
            if self.learn is not None and not real:
                await self.learn(principal, call)
            read_call = representation(call)
            if call.name in ("getAlbum", "getArtist", "getMusicDirectory"):
                direct = await raw_library_answer(self.upstream, read_call, max_bytes=32 << 20)
                container = _LISTS[call.name][0]
                if direct is None or await direct.parsed(container) is None:
                    # Preserve direct native refusals, framed in the requested format.
                    return external_failure(call, direct)
            # As Navidrome reads them, also when spelled otherwise ("01"), and not at all
            # where it does not: a folder parameter is no way to the native answer.
            folders = folders_read(call)
            if not set(folders) <= {str(i) for i in principal.library_ids}:
                return None  # a library the caller cannot read: Navidrome's own error
            if folders and str(self.access.navidrome.library_id) not in folders:
                return None  # all other physical libraries retain native semantics
            if call.name in ("getAlbum", "getArtist", "getMusicDirectory"):
                assert direct is not None
                found = await direct.parsed(_LISTS[call.name][0])
                assert found is not None
                document, detail = found
                rows = list(detail.get(_LISTS[call.name][1]) or [])
                if not real and (
                    call.name in ("getAlbum", "getArtist")
                    or not any(row.get("isDir") for row in rows)
                ):
                    # Explicit album/artist pages discover complete native contents.
                    # Filtering an artist's albums here also hides their catalog
                    # cards once materialized, making them disappear after first use.
                    # Collection policy still controls library lists and shuffle.
                    # Keep this before source readiness: the fresh caller's native
                    # answer is authoritative even before collection preparation.
                    if call.fmt == "jsonp":
                        return response(
                            call,
                            200,
                            document["subsonic-response"],
                            total=len(rows),
                            selected=False,
                        )
                    return selected_native(direct, len(rows), selected=False)
            binding = await self.binding(principal, frozenset(folders) if folders else None)
            if binding.foreign:
                if self.prepare_foreign is None:
                    raise SetupRequired
                token = await self.token_for(principal) if self.token_for else None
                await self.prepare_foreign(binding, principal, token)
            # An account that gets real audio only: its own selection, also of the
            # album, artist and folder pages.
            mode: Selection | None = REAL_AUDIO if real else None

            async def validate() -> None:
                current = await self.access.verify(
                    principal.username, expected_user_id=principal.user_id
                )
                if frozenset(str(i) for i in current.library_ids) != frozenset(
                    str(i) for i in principal.library_ids
                ):
                    raise AccessDenied
                if await self.binding(current, frozenset(folders) if folders else None) != binding:
                    raise IndexPending()

            if call.name == "getScanStatus":
                answer = await raw_library_answer(self.upstream, read_call, max_bytes=32 << 20)
                if answer is None or (found := await answer.parsed("scanStatus")) is None:
                    return external_failure(call, answer)
                count = await self.index.count(binding, mode)
                found[1]["count"] = count
                return response(call, 200, found[0]["subsonic-response"])
            if call.name in ("search2", "search3"):
                return await self._search(call, binding, mode, validate)
            if call.name in ("getAlbum", "getArtist", "getMusicDirectory"):
                assert direct is not None
                return await self._direct(call, binding, mode, direct)
            return await self._list(call, binding, mode, validate)
        except FAILURES as exc:
            return self.failure(call, exc)

    def failure(self, call: RestCall, exc: Exception) -> LibraryAnswer:
        """The answer when the library view cannot be given (one of ``FAILURES``)."""
        if isinstance(exc, AccessDenied):
            return response(
                call,
                403,
                {"error": {"code": 50, "message": "Library access could not be verified."}},
            )
        if isinstance(exc, SetupRequired):
            return response(call, 503, {"error": {"code": 0, "message": str(exc)}})
        if isinstance(exc, IndexPending):
            answer = response(call, 503, {"error": {"code": 0, "message": str(exc)}})
            answer.headers.append((b"x-shijhon-view-state", b"indexing"))
            return answer
        return response(
            call,
            503,
            {"error": {"code": 0, "message": "The library view is unavailable. Retry shortly."}},
        )

    async def real_items(
        self, caller: Caller, kind: str, idents: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """For an account that gets real audio only: each album or artist among ``idents``
        that the library index has, with the statistics of its songs whose audio is in the
        library, as that account's lists count them (``{}``: it has none). Raises one of
        ``FAILURES`` when that cannot be told."""
        if not idents:
            return {}
        principal = await self.access.verify(caller.username)
        if not principal.can_access(self.access.navidrome.library_id):
            raise AccessDenied
        binding = await self.binding(principal)
        if binding.foreign:
            if self.prepare_foreign is None:
                raise SetupRequired
            token = await self.token_for(principal) if self.token_for else None
            await self.prepare_foreign(binding, principal, token)
        return await self.index.stats_many(binding, REAL_AUDIO, kind, list(dict.fromkeys(idents)))

    async def not_found(self, call: RestCall) -> LibraryAnswer | None:
        """Navidrome's own answer to the request for an item it does not have (its "not
        found" error), in the format asked for."""
        asked = changed(representation(call), {"id": ABSENT})
        return external_failure(call, await raw_library_answer(self.upstream, asked))

    async def _source(
        self,
        call: RestCall,
        container: str,
        field: str,
        count: str,
        offset_name: str,
        offset: int,
        limit: int,
    ) -> SourcePage:
        values = {"f": "json"}
        if count:
            values[count] = str(limit)
        if offset_name:
            values[offset_name] = str(offset)
        asked = changed(call, values)
        answer = await raw_library_answer(self.upstream, asked, max_bytes=32 << 20)
        if answer is None or (found := await answer.parsed(container)) is None:
            raise IndexFailed("Navidrome did not return a complete library page.")
        document, data = found
        envelope = {k: v for k, v in document["subsonic-response"].items() if k != container}
        meta = {k: v for k, v in data.items() if k != field}
        rows = list(data.get(field) or [])
        if call.name in ("getArtists", "getIndexes"):
            rows = [
                {**artist, "__index": group["name"]}
                for group in data.get("index", [])
                for artist in group.get("artist", [])
            ]
            meta.pop("index", None)
        if call.name == "getGenres":
            rows = [
                {**g, "id": str(g.get("value", "")), "name": str(g.get("value", ""))} for g in rows
            ]
        # Unpaginated endpoints are consumed once, not split or filtered as though a
        # page were complete. The network reader enforces a separate body budget.
        try:
            total = int(header(answer.headers, b"x-total-count"))
        except ValueError:
            total = len(rows) if not count else -1
        return SourcePage(
            rows,
            total if total >= 0 else None,
            {"response": envelope, "meta": meta},
            complete=not count,
        )

    async def _list(
        self, call: RestCall, binding: Binding, mode: Selection | None, validate: Any
    ) -> LibraryAnswer:
        container, field, kind, count, offset_name = _LISTS[call.name]
        random = call.name == "getRandomSongs" or call.get("type") == "random"
        source = call
        if call.name == "getRandomSongs":
            source = changed(
                call, {"query": "", "artistCount": "0", "albumCount": "0"}, name="search3"
            )
            source_container, count, offset_name = "searchResult3", "songCount", "songOffset"
            source = RestCall.build(
                source.name,
                "GET",
                source.raw_path,
                urlencode(
                    [
                        (k, v)
                        for k, v in source.params
                        if k not in {"size", "genre", "fromYear", "toYear"}
                    ]
                ).encode(),
                source.headers,
                None,
            )
        else:
            source_container = container
        if call.get("type") == "random":
            source = changed(source, {"type": "alphabeticalByName"})
        if call.name == "getIndexes":
            source = changed(source, {"ifModifiedSince": "0"})
        key_params = query_pairs(
            (k, v)
            for k, v in source.params
            if k not in _PRIVATE | {count, offset_name, "ifModifiedSince"}
        )

        async def fetch(offset: int, limit: int) -> SourcePage:
            if not count and offset:
                return SourcePage([])
            return await self._source(
                source, source_container, field, count, offset_name, offset, limit
            )

        # Unpaginated artist indexes/directories are bounded by response bytes instead
        # of a made-up row limit. The cache's row budget remains an absolute ceiling.
        query_key: object = (source.name, key_params, header(call.headers, b"host"))
        if call.name == "getRandomSongs":
            params = [(k, v) for k, v in key_params if k not in _SEARCH_COUNTS]
            query_key = ("search3", "song", params, header(call.headers, b"host"))
        key = await self.index.ensure(
            binding, query_key, fetch, validate, patience=self._patience(mode)
        )
        default = 10 if count == "size" else 10_000_000 if not count else 10
        limit = number(
            call.get(_LISTS[call.name][3]) if _LISTS[call.name][3] else None,
            default,
            500 if _LISTS[call.name][3] else self.index.budgets.rows,
        )
        offset = number(call.get(_LISTS[call.name][4]) if _LISTS[call.name][4] else None, 0)
        native = None
        verify_songs: list[str] = []
        if call.name == "getSongsByGenre":
            read_call = representation(call)
            native = await raw_library_answer(self.upstream, read_call, max_bytes=32 << 20)
            found = await native.parsed(container) if native else None
            if found is None:
                raise IndexFailed("The native genre page is unavailable.")
            verify_songs = [str(row["id"]) for row in found[1].get(field, [])]
        direct = call.get("id") or "" if call.name in ("getAlbum", "getMusicDirectory") else ""
        page = await self.index.page(
            key,
            binding,
            kind,
            mode,
            offset,
            limit,
            random=random,
            direct_album=direct,
            song_filters=call if call.name == "getRandomSongs" else None,
            artist_role="albumartist" if call.name in ("getArtists", "getIndexes") else "",
            artist_count_role=self.artist_count_role,
            verify_songs=verify_songs,
            whole_albums=self.whole_albums(call, mode),
        )
        if native is not None and page.all_selected:
            if call.fmt == "jsonp":
                found = await native.parsed(container)
                assert found is not None
                return response(call, 200, found[0]["subsonic-response"], total=page.total)
            return selected_native(native, page.total)
        return self._render(call, page, container, field)

    async def _direct(
        self, call: RestCall, binding: Binding, mode: Selection | None, native: LibraryAnswer
    ) -> LibraryAnswer:
        container, field, kind, _, _ = _LISTS[call.name]
        found = await native.parsed(container)
        assert found is not None  # the original caller preflight succeeded
        document, data = found
        rows = list(data.get(field) or [])
        page = await self.index.live_page(
            binding,
            mode,
            kind,
            SourcePage(
                rows,
                len(rows),
                {
                    "response": {
                        k: v for k, v in document["subsonic-response"].items() if k != container
                    },
                    "meta": {k: v for k, v in data.items() if k != field},
                },
                complete=True,
            ),
            direct_album=call.get("id") or ""
            if call.name in ("getAlbum", "getMusicDirectory")
            else "",
            artist_count_role=self.artist_count_role,
        )
        if mode == REAL_AUDIO and not page.rows:
            # No song of it has its audio in the library: as Navidrome answers for an
            # album, artist or folder it does not have.
            missing = await self.not_found(call)
            if missing is None:
                raise IndexFailed("Navidrome did not answer.")
            return missing
        native_count = data.get("albumCount") if call.name == "getArtist" else data.get("songCount")
        if (
            call.fmt != "jsonp"
            and page.rows == rows
            and (native_count is None or native_count == page.total)
        ):
            # Keep full selected/discovery answers byte for byte. This also keeps
            # per-request caller ratings, plays, favorites and native row annotations.
            return selected_native(native, page.total)
        return self._render(call, page, container, field)

    def _render(self, call: RestCall, page: Page, container: str, field: str) -> LibraryAnswer:
        meta = dict(page.envelope.get("meta") or {})
        rows = page.rows
        if call.name in ("getArtists", "getIndexes"):
            groups: dict[str, list[dict[str, Any]]] = {}
            for artist in rows:
                group = artist.pop("__index", "#")
                groups.setdefault(group, []).append(artist)
            if groups:
                meta["index"] = [
                    {"name": name, "artist": artists} for name, artists in groups.items()
                ]
            else:
                meta.pop("index", None)
            # A collection edit is a resync even when Navidrome's scan timestamp did not change.
        else:
            if call.name == "getGenres":
                rows = [{k: v for k, v in g.items() if k not in ("id", "name")} for g in rows]
            if rows:
                meta[field] = rows
            else:
                meta.pop(field, None)
        if call.name == "getAlbum" or (
            call.name == "getMusicDirectory" and rows and not rows[0].get("isDir")
        ):
            if "duration" in meta and page.total != meta.get("songCount"):
                meta["duration"] = int(page.duration or 0)
            meta["songCount"] = page.total
        if call.name == "getArtist":
            meta["albumCount"] = page.total
        root = dict(page.envelope.get("response") or {})
        root[container] = meta
        return response(call, 200, root, total=page.total, original_songs=page.original_songs)

    async def _search(
        self, call: RestCall, binding: Binding, mode: Selection | None, validate: Any
    ) -> LibraryAnswer:
        container = "searchResult3" if call.name == "search3" else "searchResult2"
        root: dict[str, Any] = {}
        results: dict[str, Any] = {}
        requests: list[tuple[str | SourcePage, str, int, int]] = []
        failures: list[Exception] = []

        async def prepare(
            position: int,
            query: object,
            reader: Callable[[int, int], Awaitable[SourcePage]],
            lane: str,
        ) -> None:
            try:
                key = await self.index.ensure(
                    binding, query, reader, validate, lane=lane, patience=self._patience(mode)
                )
                requests[position] = (key, *requests[position][1:])
            except Exception as exc:  # the first failure answers the search
                failures.append(exc)
                group.cancel_scope.cancel()

        # A kind that needs preparing waits beside the next kinds' first pages, its wait
        # registered (``QueryIndex.waiting``) as soon as it begins: the search waits as
        # long as one list would.
        async with anyio.create_task_group() as group:
            try:
                for kind in ("artist", "album", "song"):
                    count_name, offset_name = kind + "Count", kind + "Offset"
                    count = number(call.get(count_name), 20, self.index.budgets.rows)
                    if not count:
                        continue
                    values = {n: "0" for n in _SEARCH_COUNTS}
                    values[count_name] = "500"
                    source = changed(call, values)
                    # A search is what Navidrome reads of it - its text and library
                    # folder -: two searches that differ only in parameters it ignores
                    # are one, and the newer does not cancel the other (``lane``) as if
                    # the listener had typed on.
                    params = query_pairs((k, v) for k, v in source.params if k in _SEARCH_READ)

                    async def fetch(
                        offset: int,
                        limit: int,
                        *,
                        source: RestCall = source,
                        kind: str = kind,
                        count_name: str = count_name,
                        offset_name: str = offset_name,
                    ) -> SourcePage:
                        return await self._source(
                            source, container, kind, count_name, offset_name, offset, limit
                        )

                    lane = (
                        binding.owner_id + ":" + call.client + ":" + call.name + ":" + kind
                        if (call.get("query") or "").strip('" ')
                        else ""
                    )
                    query = (call.name, kind, params, header(call.headers, b"host"))
                    offset = number(call.get(offset_name), 0)
                    reader: Callable[[int, int], Awaitable[SourcePage]] = fetch
                    if lane and not await self.index.has_ready(binding, query):
                        first = await fetch(0, self.index.budgets.page_size)
                        if not first.rows:
                            # This is the complete caller-authorized result, not a
                            # filtered first page. Empty typeahead/burst reads need no
                            # background job and cannot cancel another request's
                            # required generation.
                            root = dict((first.envelope or {}).get("response") or {})
                            continue
                        if len(first.rows) < self.index.budgets.page_size and first.total in (
                            None,
                            len(first.rows),
                        ):
                            # Less than a page is the complete result too: the selection
                            # applies to it directly, in this search's one snapshot.
                            # Nothing waits for a background job or is kept, and the
                            # lane's previous build goes on for the requests waiting
                            # for it (``QueryIndex.moved_on``).
                            await self.index.moved_on(binding, query, lane)
                            requests.append((replace(first, complete=True), kind, offset, count))
                            continue
                        original_fetch = fetch

                        async def prefetched(
                            offset: int,
                            limit: int,
                            first: SourcePage = first,
                            original_fetch: Any = original_fetch,
                        ) -> SourcePage:
                            return first if offset == 0 else await original_fetch(offset, limit)

                        reader = prefetched
                    group.start_soon(prepare, len(requests), query, reader, lane)
                    requests.append(("", kind, offset, count))
            except Exception as exc:  # raised once the kinds being prepared stop waiting
                failures.append(exc)
                group.cancel_scope.cancel()
        if failures:
            raise failures[0]
        pages = await self.index.page_many(
            binding,
            mode,
            requests,
            artist_count_role=self.artist_count_role,
            whole_albums=self.whole_albums(call, mode),
        )
        originals = None
        for (_, kind, _, _), page in zip(requests, pages, strict=True):
            root = dict(page.envelope.get("response") or {})
            if page.rows:
                results[kind] = page.rows
            if kind == "album":
                originals = page.original_songs
        root[container] = results

        async def shown(kind: str, idents: Sequence[str]) -> frozenset[str]:
            """What the selection shows of these songs or albums: catalog additions leave
            out an item the library has only when its library copy is among them."""
            return await self.index.shown(binding, mode, kind, idents)

        return response(call, 200, root, original_songs=originals, shown=shown)
