"""Accounts that get real audio only: ``[placeholders] real_audio_accounts``.

An analyzer - an acoustic-analysis service that signs in like any app - lists the library
and downloads every song. Through Shijhon it would get the placeholders' silence, or start
a delivery from the add-ons for each catalog song, and the catalog's additions. For the
Navidrome accounts this setting names, Shijhon adds nothing and shows only songs whose
audio is in the library: owned and imported files, and placeholders whose delivered audio
is in place.

- Lists, searches, genres, shuffle, and album, artist and folder pages
  (``collection/visibility.py``) select those songs before paging and count only them; an
  album, artist or folder without any is not found. Albums are never shown complete.
- The answers Shijhon does not page (``ANSWERS``: playlists, favorites, similar and top
  songs, now playing, play queues, bookmarks, sonic matches) leave the other songs out,
  and the albums and artists without real audio; their counts follow.
- An ID of anything else - a placeholder, a catalog item, a song of a removed release, an
  album or artist without real audio - is one Navidrome does not have: the request goes to
  Navidrome with an ID it never has (``ABSENT``), for its own answer to that ("not found",
  or nothing done).
- Audio (``stream``, ``download``, transcoding) is Navidrome's own file
  (``Interceptor.real_audio``): never a delivery, a preparation, a fetch ahead or any
  add-on request.
- The jukebox and sharing are off for such an account (``REFUSED``: Navidrome's own answer
  while they are off), and a share link of one of its shares that holds a song without real
  audio is one Navidrome does not have (``share_links``): its queue or its links could
  reach silence, or a delivery.

None of Shijhon's other handlers runs for such an account: no catalog request, no release
added to the library, no warm-ahead from its reports - and no parameter takes it past
them (the native library's folder bypass does not apply to it). The account is the one
Navidrome's credential check names, never the client's name; a request whose credentials
name another account passes with no extra work.
"""

from __future__ import annotations

import json
import logging
import string
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

import anyio
import httpx

from shijhon.collection.query_index import IndexFailed
from shijhon.collection.visibility import (
    FAILURES,
    changed,
    external_failure,
    representation,
    response,
)
from shijhon.navidrome.client import NavidromeError
from shijhon.proxy.app import (
    Before,
    Forward,
    Handler,
    HandlerResult,
    MusicGate,
    PathInterceptor,
    ProxyApp,
    Reply,
)
from shijhon.proxy.auth import Caller, CredentialChecker
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import music_send, not_implemented
from shijhon.proxy.upstream import (
    Receive,
    Scope,
    Send,
    Upstream,
    relay,
    request_headers,
    send_bytes,
)
from shijhon.store import Store
from shijhon.views.answers import LibraryAnswer, accepts_gzip, raw_library_answer
from shijhon.views.catalog_ids import id_key
from shijhon.views.ids import ABSENT, CatalogId

if TYPE_CHECKING:
    from shijhon.collection.visibility import Visibility
    from shijhon.delivery.intercept import Interceptor
    from shijhon.placeholders.engine import PlaceholderEngine
    from shijhon.proxy.app import RequestContext

log = logging.getLogger(__name__)

AUDIO = ("stream", "download", "getTranscodeDecision", "getTranscodeStream")
# Answered as Navidrome answers them while the jukebox and sharing are off.
REFUSED = ("jukeboxControl", "getShares", "createShare", "updateShare", "deleteShare")
# Answers Shijhon does not page: the element each is in, and its lists with what each
# entry of them is.
ANSWERS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "getPlaylist": ("playlist", (("entry", "song"),)),
    "getPlaylists": ("playlists", (("playlist", "playlist"),)),
    "getStarred": ("starred", (("artist", "artist"), ("album", "album"), ("song", "song"))),
    "getStarred2": ("starred2", (("artist", "artist"), ("album", "album"), ("song", "song"))),
    "getNowPlaying": ("nowPlaying", (("entry", "song"),)),
    "getSimilarSongs": ("similarSongs", (("song", "song"),)),
    "getSimilarSongs2": ("similarSongs2", (("song", "song"),)),
    "getTopSongs": ("topSongs", (("song", "song"),)),
    "getPlayQueue": ("playQueue", (("entry", "song"),)),
    "getPlayQueueByIndex": ("playQueueByIndex", (("entry", "song"),)),
    "getBookmarks": ("bookmarks", (("bookmark", "bookmark"),)),
    "getArtistInfo": ("artistInfo", (("similarArtist", "artist"),)),
    "getArtistInfo2": ("artistInfo2", (("similarArtist", "artist"),)),
}
# ... and the sonic matches (a plugin's), a list at the answer's top.
SONIC = ("getSonicSimilarTracks", "findSonicPath")
# Requests that may name an album or an artist by ID, and the parameters that may: one the
# library index has without any real audio is one Navidrome does not have.
ITEMS: dict[str, dict[str, tuple[str, ...]]] = {
    "getAlbumInfo": {"id": ("album",)},
    "getAlbumInfo2": {"id": ("album",)},
    "getArtistInfo": {"id": ("artist",)},
    "getArtistInfo2": {"id": ("artist",)},
    "getSimilarSongs": {"id": ("album", "artist")},
    "getSimilarSongs2": {"id": ("album", "artist")},
    "star": {"id": ("album", "artist"), "albumId": ("album",), "artistId": ("artist",)},
    "unstar": {"id": ("album", "artist"), "albumId": ("album",), "artistId": ("artist",)},
    "setRating": {"id": ("album", "artist")},
}
PARALLEL = 8  # playlists read at once for one list of them
MAX_SHARES = 5000  # Navidrome's shares listed at most (more: not told)
QUIET = "quiet"  # a share archive answered with no delivery
BODY_LIMIT = 32 << 20  # the largest answer read to be filtered
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def fold(name: str) -> str:
    """A user name as Navidrome compares them at sign-in (SQLite's NOCASE: ASCII letters
    in either case, nothing else)."""
    return name.translate(_ASCII_LOWER)


class RealAudioAccounts:
    """The accounts ``[placeholders] real_audio_accounts`` names."""

    def __init__(self, names: Iterable[str]) -> None:
        self.names = frozenset(fold(name) for name in names)

    def __bool__(self) -> bool:
        return bool(self.names)

    def named(self, username: str) -> bool:
        """Whether the user Navidrome authenticated is one of them."""
        return fold(username) in self.names

    def may_be(self, claimed: Sequence[str], credentials: bool) -> bool:
        """Whether a request may be one of theirs - only Navidrome's check tells: it names
        one of them (``u``, the reverse-proxy user), or it names no one and carries
        credentials Navidrome alone can put a name to (an API key)."""
        if not self.names or not credentials:
            return False
        return not claimed or any(self.named(name) for name in claimed)


def _id(entry: Any) -> str | None:
    return str(entry["id"]) if isinstance(entry, dict) and "id" in entry else None


def song_ids(method: str, data: dict[str, Any]) -> list[str]:
    """The IDs of the songs in an answer's element (``ANSWERS``)."""
    found: list[str] = []
    for key, kind in ANSWERS[method][1]:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if kind == "song":
                found.append(_id(row) or "")
            elif kind == "bookmark" and isinstance(row, dict):
                found.append(_id(row.get("entry")) or "")
    if method == "getPlayQueue" and isinstance(data.get("current"), str):
        found.append(data["current"])
    return [ident for ident in dict.fromkeys(found) if ident]


def item_ids(method: str, data: dict[str, Any], kind: str) -> list[str]:
    """The IDs of the albums or artists (``kind``) in an answer's element."""
    found = []
    for key, listed in ANSWERS[method][1]:
        rows = data.get(key)
        if listed == kind and isinstance(rows, list):
            found += [ident for row in rows if (ident := _id(row))]
    return list(dict.fromkeys(found))


def _counted(item: dict[str, Any], stats: dict[str, Any], kind: str, role: str) -> bool:
    """An album's or artist's entry with the counts of its songs with real audio, as the
    selected lists count them (``QueryIndex._page``); whether it changed."""
    before = dict(item)
    if kind == "album":
        complete = item.get("songCount") == stats.get("songCount")
        for field in ("songCount", "duration", "size"):
            if field not in item or (field != "songCount" and complete):
                continue  # complete native metadata stays exact
            value = stats.get(field, 0)
            item[field] = int(value) if isinstance(item[field], int) else value
    elif "albumCount" in item:
        item["albumCount"] = stats.get("stats", {}).get(role, {}).get("albumCount", 0)
    return item != before


def keep(
    method: str,
    data: dict[str, Any],
    silent: set[str],
    items: dict[str, dict[str, dict[str, Any]]],
    *,
    role: str = "albumartist",
) -> bool:
    """An answer's element (``ANSWERS``) with only real audio, in place: the songs not in
    ``silent``, the albums and artists ``items`` has with a song of real audio (with its
    counts); whether it changed. A list left empty is left out, as Navidrome leaves it."""
    changed_any = False
    original = list(data.get("entry") or []) if method == "getPlayQueueByIndex" else []
    for key, kind in ANSWERS[method][1]:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        if kind == "song":
            kept = [row for row in rows if _id(row) not in silent]
        elif kind == "bookmark":
            kept = [
                row
                for row in rows
                if not isinstance(row, dict) or _id(row.get("entry")) not in silent
            ]
        elif kind in ("album", "artist"):
            found = items.get(kind, {})
            kept = []
            for row in rows:
                stats = found.get(_id(row) or "")
                if not stats:
                    continue  # none of its songs has real audio (or no song is known)
                if isinstance(row, dict) and _counted(row, stats, kind, role):
                    changed_any = True
                kept.append(row)
        else:
            continue
        if len(kept) != len(rows):
            changed_any = True
            if kept:
                data[key] = kept
            else:
                del data[key]
    if not changed_any:
        return False
    if method == "getPlaylist":
        entries = [e for e in data.get("entry") or [] if isinstance(e, dict)]
        data["songCount"] = len(entries)
        data["duration"] = sum(int(e.get("duration") or 0) for e in entries)
    if method == "getPlayQueue" and data.get("current") in silent:
        data.pop("current", None)
        data.pop("position", None)
    if method == "getPlayQueueByIndex" and isinstance(data.get("currentIndex"), int):
        at = data["currentIndex"]
        if 0 <= at < len(original) and _id(original[at]) not in silent:
            data["currentIndex"] = sum(1 for e in original[:at] if _id(e) not in silent)
        else:
            data.pop("currentIndex", None)
            data.pop("position", None)
    return True


def shown_to_native(indexes: Sequence[str], shown: Sequence[int], total: int) -> dict[str, str]:
    """``updatePlaylist``'s ``songIndexToRemove`` values, as the account counts a playlist's
    entries (its real songs: native positions ``shown``), in Navidrome's positions among
    all ``total`` entries; one past the end stays past it."""
    mapping = {}
    for value in indexes:
        try:
            at = int(value)
        except ValueError:
            continue  # Navidrome's own answer to it
        if 0 <= at < len(shown):
            mapping[value] = str(shown[at])
        elif at >= len(shown):
            mapping[value] = str(total + at - len(shown))
    return mapping


class RealAudio:
    def __init__(
        self,
        accounts: RealAudioAccounts,
        store: Store,
        checker: CredentialChecker,
        interceptor: Interceptor,
        engine: PlaceholderEngine,
        visibility: Visibility,
        upstream: Upstream,
        *,
        library_id: str,
    ) -> None:
        self.accounts = accounts
        self.store = store
        self.checker = checker
        self.interceptor = interceptor
        self.engine = engine
        self.visibility = visibility
        self.upstream = upstream
        self.library_id = library_id

    def attach(self, proxy: ProxyApp) -> None:
        """Every handler, the fallback and the work before them, for such an account: its
        own; the music gate never lets it past them; share links of its shares. Call once
        all of them are in place."""
        proxy.handlers = {method: self.wrap(inner) for method, inner in proxy.handlers.items()}
        proxy.fallback = self.wrap(proxy.fallback)
        proxy.before = self.wrap_before(proxy.before)
        proxy.music_gate = self.wrap_gate(proxy.music_gate)
        links = proxy.path_interceptors.get("/share/")
        proxy.path_interceptors["/share/"] = self.share_links(links)
        self.visibility.real_audio = self.accounts.named

    def wrap_gate(self, inner: MusicGate | None) -> MusicGate:
        """The music gate, which sends a request past Shijhon's handlers to the native
        library (a caller without the library, a list of another one): never such an
        account's - also not for a list, whatever the gate's reading of its folder."""

        async def gate(call: RestCall, ctx: RequestContext) -> bool:
            if inner is None or await inner(call, ctx):
                return True
            caller = await ctx.caller()  # (within the gate: the caller Navidrome accepted)
            return caller is not None and self.accounts.named(caller.username)

        return gate

    async def caller(self, call: RestCall, ctx: RequestContext) -> Caller | None:
        """The caller, when it is one of the accounts (Navidrome checked its credentials);
        None for any other request - one that names another account with no work."""
        claimed, credentials = self.checker.claimed(call)
        if not self.accounts.may_be(claimed, credentials):
            return None
        caller = await ctx.caller()
        return caller if caller is not None and self.accounts.named(caller.username) else None

    def wrap(self, inner: Handler | None) -> Handler:
        async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
            caller = await self.caller(call, ctx)
            if caller is None:
                return await inner(call, ctx) if inner is not None else None
            return await self.handle(call, ctx, caller)

        return handle

    def wrap_before(self, inner: Before | None) -> Before:
        async def before(call: RestCall, ctx: RequestContext) -> None:
            if inner is not None and await self.caller(call, ctx) is None:
                await inner(call, ctx)  # (a removed release's old ID adds it back)

        return before

    async def handle(self, call: RestCall, ctx: RequestContext, caller: Caller) -> HandlerResult:
        """A request of such an account."""
        if call.name in REFUSED:
            return not_implemented(call)
        try:
            if call.name in AUDIO:
                return await self.interceptor.real_audio(call, ctx, self.absent)
            missing = await self.absent(
                [v for k, v in call.params if id_key(k) and v], artwork=call.name == "getCoverArt"
            )
            missing |= await self._absent_items(call, caller, missing)
            asked = (
                call.rewritten(lambda k, v: ABSENT if id_key(k) and v in missing else None)
                if missing
                else call
            )
            if call.name == "updatePlaylist" and asked.getall("songIndexToRemove"):
                asked = await self._indexes(asked)
            if call.name in ANSWERS or call.name in SONIC:
                return await self._answer(asked, ctx, caller)
            if (
                asked.http_method not in ("GET", "HEAD")
                and asked.body is None
                and self.visibility.applicable(asked)
            ):
                # A body Shijhon did not read (no form: Navidrome reads no parameter from it)
                # goes on behind the handlers, past the library view: answered here, as
                # the same request without it.
                plain = RestCall.build(
                    asked.name, "GET", asked.raw_path, asked.query, asked.headers, None
                )
                selected = await self.visibility.answer(plain, caller=caller)
                if selected is not None:
                    return selected.reply(compress=accepts_gzip(asked))
        except FAILURES as exc:
            return self.visibility.failure(call, exc).reply(head=ctx.head)
        return Forward(asked) if asked is not call else None

    # --- what has no real audio --------------------------------------------------------------

    async def absent(self, idents: Sequence[str], *, artwork: bool = False) -> set[str]:
        """The IDs among ``idents`` that name no song with real audio: catalog items, old
        IDs of removed releases, silent placeholders (also those an owned recording plays
        for), and files in the placeholder folder that no placeholder row tells of. Any
        other ID (an owned or delivered song, an album, a playlist...) is not named."""
        found: set[str] = set()
        rest: list[str] = []
        parse = CatalogId.parse_artwork if artwork else CatalogId.parse
        for ident in dict.fromkeys(idents):
            if (
                ident == ABSENT
                or parse(ident) is not None
                or (
                    self.engine.removed_release(ident) is not None
                    and await self.engine.still_removed(ident) is not None
                )
            ):
                found.add(ident)
            else:
                rest.append(ident)
        return found | await self.silent(rest)

    async def silent(self, idents: Sequence[str]) -> set[str]:
        """The songs among ``idents`` whose file is silent: placeholders without delivered
        audio, and files in the placeholder folder no row tells of (the library index's
        record of them). One query per 500."""
        found: set[str] = set()
        for start in range(0, len(idents), 500):
            chunk = json.dumps(list(idents[start : start + 500]))
            rows = await self.store.fetchall(
                "SELECT song_id FROM placeholders WHERE state<>'delivered'"
                " AND song_id IN (SELECT value FROM json_each(?))"
                " UNION SELECT ct.song_id FROM collection_tracks ct"
                " WHERE ct.library_id=? AND ct.present=1 AND ct.original=0"
                " AND ct.song_id IN (SELECT value FROM json_each(?))"
                " AND NOT EXISTS (SELECT 1 FROM placeholders p"
                " WHERE p.song_id=ct.song_id AND p.state='delivered')",
                [chunk, self.library_id, chunk],
            )
            found |= {str(row["song_id"]) for row in rows}
        return found

    async def _absent_items(self, call: RestCall, caller: Caller, missing: set[str]) -> set[str]:
        """The album and artist IDs a request names (``ITEMS``) that the library index has
        without any song of real audio."""
        wanted: dict[str, list[str]] = {}
        for key, kinds in ITEMS.get(call.name, {}).items():
            for value in call.getall(key):
                if value and value not in missing:
                    for kind in kinds:
                        wanted.setdefault(kind, []).append(value)
        found: set[str] = set()
        for kind, idents in wanted.items():
            known = await self.visibility.real_items(caller, kind, idents)
            found |= {ident for ident, stats in known.items() if not stats}
        return found

    # --- answers -------------------------------------------------------------------------

    async def _read(self, call: RestCall) -> LibraryAnswer:
        answer = await raw_library_answer(self.upstream, call, max_bytes=BODY_LIMIT)
        if answer is None:
            raise IndexFailed("Navidrome did not answer.")
        return answer

    async def _answer(self, call: RestCall, ctx: RequestContext, caller: Caller) -> Reply:
        """Navidrome's answer with only real audio in it."""
        answer = await self._read(representation(call))
        document = await answer.document()
        changed_any = False
        if document is not None:
            payload = document["subsonic-response"]
            if call.name in SONIC:
                changed_any = await self._sonic(payload)
            elif isinstance(data := payload.get(ANSWERS[call.name][0]), dict):
                if call.name == "getPlaylists":
                    changed_any = await self._playlists(call, data)
                else:
                    items = {
                        kind: await self.visibility.real_items(
                            caller, kind, item_ids(call.name, data, kind)
                        )
                        for kind in ("album", "artist")
                    }
                    silent = await self.absent(song_ids(call.name, data))
                    changed_any = keep(
                        call.name,
                        data,
                        silent,
                        items,
                        role=self.visibility.artist_count_role,
                    )
        compress = accepts_gzip(call)
        if not changed_any:
            unchanged = external_failure(call, answer)  # (JSONP: read as JSON, written back)
            assert unchanged is not None
            return unchanged.reply(head=ctx.head, compress=compress)
        assert document is not None
        if call.fmt == "jsonp":
            payload = document["subsonic-response"]
            return response(call, 200, payload).reply(head=ctx.head, compress=compress)
        return answer.reply(head=ctx.head, document=document, compress=compress)

    async def _sonic(self, payload: dict[str, Any]) -> bool:
        matches = payload.get("sonicMatch")
        if not isinstance(matches, list):
            return False
        entries = [m.get("entry") if isinstance(m, dict) else None for m in matches]
        silent = await self.absent([ident for e in entries if (ident := _id(e))])
        kept = [m for m, e in zip(matches, entries, strict=True) if _id(e) not in silent]
        if len(kept) == len(matches):
            return False
        payload["sonicMatch"] = kept
        return True

    async def _entries(self, call: RestCall, playlist: str) -> list[dict[str, Any]] | None:
        """A playlist's entries as the caller gets them (None: Navidrome has no such
        playlist for it)."""
        asked = changed(call, {"id": playlist, "f": "json"}, name="getPlaylist")
        found = await (await self._read(asked)).parsed("playlist")
        if found is None:
            return None
        return [e for e in found[1].get("entry") or [] if isinstance(e, dict)]

    async def _playlists(self, call: RestCall, data: dict[str, Any]) -> bool:
        """The playlists' song counts and lengths, of their songs with real audio (each
        playlist read; ``PARALLEL`` at once)."""
        rows = [row for row in data.get("playlist") or [] if isinstance(row, dict)]
        counted: dict[int, tuple[int, int]] = {}
        limiter = anyio.CapacityLimiter(PARALLEL)

        async def count(at: int, playlist: dict[str, Any]) -> None:
            async with limiter:
                entries = await self._entries(call, str(playlist.get("id", "")))
                if entries is None:
                    raise IndexFailed("A playlist could not be read.")
                silent = await self.absent([i for e in entries if (i := _id(e))])
                if silent:
                    kept = [e for e in entries if _id(e) not in silent]
                    counted[at] = (len(kept), sum(int(e.get("duration") or 0) for e in kept))

        async with anyio.create_task_group() as group:
            for at, playlist in enumerate(rows):
                group.start_soon(count, at, playlist)
        for at, (songs, seconds) in counted.items():
            rows[at]["songCount"], rows[at]["duration"] = songs, seconds
        return bool(counted)

    async def _indexes(self, call: RestCall) -> RestCall:
        """``updatePlaylist``'s positions to remove, as the account counts the playlist's
        entries, in Navidrome's positions."""
        entries = await self._entries(call, call.get("playlistId") or "")
        if entries is None:
            return call  # Navidrome's own answer to the update
        silent = await self.absent([i for e in entries if (i := _id(e))])
        if not silent:
            return call
        shown = [at for at, e in enumerate(entries) if _id(e) not in silent]
        mapping = shown_to_native(call.getall("songIndexToRemove"), shown, len(entries))
        return call.rewritten(lambda k, v: mapping.get(v) if k == "songIndexToRemove" else None)

    # --- share links -------------------------------------------------------------------------

    def share_links(self, inner: PathInterceptor | None) -> PathInterceptor:
        """Share links of such an account's shares, which it may have made before (sharing
        is off for it now): a share page, its playlist or its archive (``/share/<id>``,
        ``/share/<id>/m3u``, ``/share/d/<id>``) of a share that holds a song without real
        audio, or a stream (``/share/s/<token>``) of a song without it that such a share
        holds, is answered as Navidrome answers for a share or a link it does not have.
        Their other links never reach the add-ons either: delivered audio has its use
        recorded and comes from the library, an archive with a silent file is refused.
        Other links: as before (``inner``)."""

        async def intercept(scope: Scope, receive: Receive, send: Send) -> bool:
            if scope["method"] in ("GET", "HEAD"):
                decided = await self._link(str(scope["path"]))
                if decided == QUIET:
                    return await self.interceptor.share(scope, receive, send, deliver=False)
                if decided is not None:
                    await self._forward(scope, decided, receive, send)
                    return True
            return await inner(scope, receive, send) if inner is not None else False

        return intercept

    async def _link(self, path: str) -> str | None:
        """For a link of such an account's share: the path to ask Navidrome (itself, or
        one naming nothing it has), or ``QUIET`` (an archive: never a delivery); None for
        any other link."""
        parts = path.removeprefix("/share/").split("/")
        if len(parts) == 2 and parts[0] == "s" and parts[1]:
            return await self._stream_link(path, parts[1])
        if len(parts) == 2 and parts[0] == "d" and parts[1]:
            share, closed, open_ = parts[1], "/share/d/" + ABSENT, QUIET
        elif parts[0] not in ("", "s", "d", "img") and (
            len(parts) == 1 or (len(parts) == 2 and parts[1] in ("", "m3u"))
        ):
            share, closed, open_ = parts[0], "/share/" + "/".join([ABSENT, *parts[1:]]), path
        else:
            return None
        try:
            found = await self.interceptor.navidrome.share(share)
        except NavidromeError as exc:
            # One Navidrome does not have: its own answer; not told: none.
            return None if exc.status == 404 else closed
        if not found or not self.accounts.named(str(found.get("username") or "")):
            return None
        try:
            silent = await self.absent(await self.interceptor.shared_songs(share))
        except (*FAILURES, NavidromeError):
            return closed
        return closed if silent else open_

    async def _stream_link(self, path: str, token: str) -> str | None:
        """A share link's stream of a song a share of such an account holds: its delivered
        audio from the library (its use recorded), else nothing."""
        song = self.interceptor.shared_song(token)
        if song is None:
            return None
        closed = "/share/s/" + ABSENT
        try:
            row = await self.store.fetchone(
                "SELECT state FROM placeholders WHERE song_id = ?", [song]
            )
            if row is None and not self.engine.writing() and not await self.absent([song]):
                return None  # an owned song: Navidrome's file, no delivery either way
            if song not in await self._shared_by_accounts():
                return None
            if row is not None and row["state"] == "delivered":
                settled = await self.interceptor.download_first.settled(song, used=True)
                if settled is not None and settled["state"] == "delivered":
                    return path
        except Exception:  # not told (Navidrome, a stop's unfinished swap): no audio
            log.info("a share link's song could not be told apart: not served")
        return closed

    async def _shared_by_accounts(self) -> set[str]:
        """The songs that shares of these accounts hold now (Navidrome's list of every
        share, complete, and those shares' songs - read anew for each link that could
        start a delivery: a share's playlist may have gained a placeholder since).
        ``NavidromeError`` when not told."""
        navidrome = self.interceptor.navidrome
        response = await navidrome.native("GET", "share", params={"_start": 0, "_end": MAX_SHARES})
        try:
            listed = response.json()
            total = int(response.headers.get("x-total-count", ""))
        except ValueError:
            raise NavidromeError("shares: not listed") from None
        if not isinstance(listed, list) or total > len(listed):
            raise NavidromeError("shares: not completely listed")
        songs: set[str] = set()
        for share in listed:
            if isinstance(share, dict) and self.accounts.named(str(share.get("username") or "")):
                songs.update(await self.interceptor.shared_songs(str(share.get("id") or "")))
        return songs

    async def _forward(self, scope: Scope, path: str, receive: Receive, send: Send) -> None:
        """The request as Navidrome answers it at ``path`` instead."""
        method = str(scope["method"])
        headers = request_headers(list(scope["headers"]), keep_length=True)
        answered = music_send(method, list(scope["headers"]), send)
        try:
            response = await self.upstream.send(
                method, path.encode(), scope.get("query_string", b""), headers, None
            )
        except httpx.HTTPError:
            await send_bytes(
                answered, 502, b"Navidrome is unreachable\n", [(b"content-type", b"text/plain")]
            )
            return
        await relay(response, receive, answered, head=method == "HEAD")
