"""Releases the cleanup took out, as clients that kept their IDs see them.

Clients that synced a release keep its IDs, and sync again: a view of an old ID must not
undo the cleanup. So a view writes nothing, and shows the release complete from its record:

- ``getAlbum`` and ``getMusicDirectory`` of a removed catalog album's ID answer as
  Navidrome answered before the removal. Navidrome keeps the album and its songs as missing
  files (``PurgeMissing`` "never"): it answers the album without songs, and ``getSong`` of
  each old song ID with its entry as it was. The view is Navidrome's own answer with the
  record's songs, each as Navidrome's ``getSong`` answers it for the client (its own
  favorites, ratings and plays), in the record's order - XML byte for byte as Navidrome
  answered before, JSON the same document.
- ``getSong`` of an old song ID is Navidrome's own answer (nothing here).
- A cover of an old ID is the release's: a catalog album's from the catalog (the
  artwork its record keeps: no catalog request for it), a fill's its owned album's.

A plain stream of an old song ID plays from the add-ons (``delivery/intercept.py``). Only a
use adds the release back (``cleanup.OldIds``). An old ID whose item the library has again
- the owner's own copy of the album, with the same tags and so the same IDs - drops the
record: those are the owner's songs, answered as any other (``PlaceholderEngine.
still_removed``). Credentials first.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import anyio

from shijhon.catalog.model import release_from_data
from shijhon.proxy.app import Forward, Handler, HandlerResult, RequestContext
from shijhon.proxy.auth import CheckFailed
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import subsonic_error
from shijhon.proxy.upstream import Upstream
from shijhon.views.answers import LibraryAnswer, accepts_gzip, library_answer
from shijhon.views.ids import CatalogId

if TYPE_CHECKING:
    from shijhon.catalog.artwork import ArtworkIndex
    from shijhon.placeholders.engine import PlaceholderEngine
    from shijhon.views.virtual import VirtualViews

log = logging.getLogger(__name__)
PARALLEL = 8  # songs asked of Navidrome at once for one view
# Where a view's songs go: the answer's element and its songs' element.
CONTAINERS = {"getAlbum": ("album", "song"), "getMusicDirectory": ("directory", "child")}
# A cover ID as Navidrome makes them: al-/mf-/dc- (a disc's: ":<disc>"), "_<hash>" after.
_COVER = re.compile(r"(?:(?:al|mf|dc)-)?([^_:]+)(?::\d+)?(?:_[0-9a-fA-F]+)?")
_THERE = object()  # the album has songs again: answered as any other
NOT_FOUND = 70  # Navidrome's error code for a song it does not have
_NOT_FOUND_XML = re.compile(r'<error\s[^>]*\bcode="70"')
_DURATION = re.compile(r'\sduration="(\d+)"')


class _Unavailable(Exception):
    """Navidrome did not say what one of the release's songs is: the view is not built."""


def _sum(lengths: list[Any]) -> int | None:
    """The songs' lengths together (None: one of them has none)."""
    if all(isinstance(n, int) and not isinstance(n, bool) for n in lengths):
        return int(sum(lengths))
    return None


# Headers that describe Navidrome's body, not the one sent.
_BODY_HEADERS = {b"content-length", b"content-encoding", b"etag", b"last-modified"}


class RemovedViews:
    def __init__(
        self,
        engine: PlaceholderEngine,
        upstream: Upstream,
        *,
        views: VirtualViews | None = None,
        artwork: ArtworkIndex | None = None,
    ) -> None:
        self.engine = engine
        self.upstream = upstream
        self.views = views  # catalog covers
        self.artwork = artwork  # the record's artwork, noted: no catalog request for it
        self.shown = 0  # views answered from a record (observable in tests)

    def wrap(self, method: str, inner: Handler | None) -> Handler:
        """``method``'s handler: old IDs of removed releases here, the rest as ``inner``."""
        own = self._cover if method == "getCoverArt" else self._view

        async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
            try:
                result = await own(call, ctx)
            except CheckFailed:  # no verdict on the credentials: the proxy's error
                raise
            except Exception as exc:  # Navidrome's own answer, then
                log.warning("%s of a removed release failed: %s", method, type(exc).__name__)
                result = None
            if result is not None:
                return result
            return await inner(call, ctx) if inner is not None else None

        return handle

    # --- views ------------------------------------------------------------------------------

    async def _view(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        album_id = call.get("id") or ""
        if self.engine.removed_album(album_id) is None or call.fmt == "jsonp":
            return None  # not an old album ID (no work), or a format left to Navidrome
        if await ctx.caller() is None:
            return None  # Navidrome answers with its own credential error
        ref = await self.engine.still_removed(album_id)
        if ref is None:
            return None  # the owner's album now: answered as any other
        songs = await self.engine.removed_songs(ref)
        if not songs:
            return None
        answer = await library_answer(self.upstream, _as(call, call.name, {}))
        if answer is None:
            return None  # Navidrome unreachable: the forward says so
        key, child = CONTAINERS[call.name]
        try:  # (XML for any format but JSON, as Navidrome answers: "f=XML" too)
            if call.xml:
                body = await self._xml(call, answer, key, child, songs)
            else:
                body = await self._json(call, answer, key, child, songs)
        except _Unavailable:
            # Navidrome did not say what one of its songs is (it failed, or was not
            # reached): an error the client asks again after - never the album with fewer
            # songs than it has, which a client would keep in place of the whole one.
            log.info("a removed release's view: a song's lookup failed; answered with an error")
            return subsonic_error(call, 0, "The album could not be read just now: try again")
        if body is _THERE:
            return None  # songs there again: answered as any other album
        if not isinstance(body, bytes):
            return answer.reply(head=ctx.head, compress=accepts_gzip(call))
        self.shown += 1
        headers = [(k, v) for k, v in answer.headers if k.lower() not in _BODY_HEADERS]
        return LibraryAnswer(answer.status, headers, body).reply(
            head=ctx.head, compress=accepts_gzip(call)
        )

    async def _json(
        self, call: RestCall, answer: LibraryAnswer, key: str, child: str, songs: list[str]
    ) -> bytes | object | None:
        found = await answer.parsed(key)
        if found is None:
            return None  # an error: as Navidrome gave it
        document, container = found
        if container.get(child):
            return _THERE  # songs there again (the owner's own album)
        entries: list[Any] = []
        for text in await self._songs(call, songs):
            try:
                song = json.loads(text)["subsonic-response"]
                entry = song["song"] if song.get("status") == "ok" else song["error"]["code"]
            except (ValueError, KeyError, TypeError):
                entry = None
            if isinstance(entry, dict):
                entries.append(entry)
            elif entry != NOT_FOUND:  # neither the song nor "Navidrome does not have it"
                raise _Unavailable
        if not entries:  # none of its songs is Navidrome's any more: no album to show
            raise _Unavailable
        if len(entries) < len(songs):  # songs Navidrome no longer has: counted as shown
            lengths = [e.get("duration") for e in entries]
            for name, value in (("songCount", len(entries)), ("duration", _sum(lengths))):
                if isinstance(container.get(name), int) and value is not None:
                    container[name] = value
        if child == "song":  # an album's songs come last, a directory's children first
            container[child] = entries
        else:
            rest = dict(container)
            container.clear()
            container.update({child: entries, **rest})
        return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()

    async def _xml(
        self, call: RestCall, answer: LibraryAnswer, key: str, child: str, songs: list[str]
    ) -> bytes | object | None:
        """Navidrome's XML with the songs' elements (as its ``getSong`` writes them) where
        its own album answer would have them: at the end of the element."""
        body = answer.body.decode("utf-8", "replace")
        closing = f"</{key}></subsonic-response>"
        opened, at = body.find(f"<{key} "), body.rfind(closing)
        if (
            answer.status != 200
            or not _ok(body)
            or opened < 0
            or at < opened
            or body[at:].strip() != closing
        ):
            return None
        if f"<{child} " in body[opened:] or f"<{child}>" in body[opened:]:
            return _THERE  # songs there again (the owner's own album)
        elements = []
        lengths: list[Any] = []
        for text in await self._songs(call, songs):
            start, end = text.find("<song "), text.rfind("</subsonic-response>")
            if not _ok(text) or start < 0 or end < start or not text[start:end].endswith("</song>"):
                if _NOT_FOUND_XML.search(text) is None or _ok(text):
                    raise _Unavailable  # neither the song nor "Navidrome does not have it"
                continue
            element = text[start:end]
            length = _DURATION.search(element[: element.find(">")])
            lengths.append(int(length.group(1)) if length else None)
            if child != "song":  # a directory's children: the same entries, named so
                element = f"<{child} " + element[len("<song ") : -len("</song>")] + f"</{child}>"
            elements.append(element)
        if not elements:  # none of its songs is Navidrome's any more: no album to show
            raise _Unavailable
        if len(elements) < len(songs):  # songs Navidrome no longer has: counted as shown
            head_end = body.find(">", opened)
            head = body[opened:head_end]
            for name, value in (("songCount", len(elements)), ("duration", _sum(lengths))):
                if value is not None:
                    head = re.sub(rf'(\s{name}=")\d+(")', rf"\g<1>{value}\g<2>", head, count=1)
            body = body[:opened] + head + body[head_end:]
            at = body.rfind(closing)
        return (body[:at] + "".join(elements) + body[at:]).encode()

    async def _songs(self, call: RestCall, songs: list[str]) -> list[str]:
        """Navidrome's ``getSong`` answers for the client (its credentials, its format),
        in order - each the song or an error (a song Navidrome no longer has is left out
        by the caller). :class:`_Unavailable` when Navidrome gave no answer for one."""
        found: dict[int, str] = {}
        limiter = anyio.CapacityLimiter(PARALLEL)

        async def one(index: int, song: str) -> None:
            async with limiter:
                answer = await library_answer(self.upstream, _as(call, "getSong", {"id": song}))
            if answer is not None and answer.status == 200:
                found[index] = answer.body.decode("utf-8", "replace")

        async with anyio.create_task_group() as group:
            for index, song in enumerate(songs):
                group.start_soon(one, index, song)
        if len(found) < len(songs):
            raise _Unavailable
        return [found[i] for i in sorted(found)]

    # --- covers -------------------------------------------------------------------------------

    async def _cover(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        original = call.get("id") or ""
        match = _COVER.fullmatch(original)
        if match is None or self.engine.removed_release(match.group(1)) is None:
            return None  # not an old ID (no work)
        if await ctx.caller() is None:
            return None
        ref = await self.engine.still_removed(match.group(1))
        if ref is None:
            return None  # the owner's songs now: Navidrome's cover
        stored = await self.engine.removed_record(ref)
        if stored is None:
            return None
        if stored["owned_album_id"]:  # a fill: its album's cover, which Navidrome has
            album = f"al-{stored['owned_album_id']}"
            return Forward(call.rewritten(lambda k, v: album if k == "id" else None))
        if self.views is None:
            return None
        record = json.loads(stored["record"])
        release = release_from_data(json.loads(record["release"]["data"]))
        if self.artwork is not None and release.artwork_template:
            self.artwork.note("al", release.ref, release.artwork_template)
        cid = str(CatalogId("al", release.ref))
        return await self.views.get_cover_art(
            call.rewritten(lambda k, v: cid if k == "id" else None), ctx
        )


def _as(call: RestCall, name: str, params: dict[str, str]) -> RestCall:
    """The client's request as ``name`` with ``params`` changed - its own credentials and
    format - read in full (a HEAD is asked as a GET)."""
    view = b".view" if call.raw_path.endswith(b".view") else b""
    changed = call.rewritten(lambda k, v: params.get(k))
    method = "GET" if call.http_method == "HEAD" else call.http_method
    return RestCall.build(
        name, method, b"/rest/" + name.encode() + view, changed.query, changed.headers, changed.body
    )


def _ok(xml: str) -> bool:
    """An XML answer whose status is "ok" (in its opening element)."""
    start = xml.find("<subsonic-response")
    return start >= 0 and 'status="ok"' in xml[start : xml.find(">", start) + 1]
