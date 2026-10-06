"""Commits: a catalog ID used for anything beyond viewing is materialized
first and rewritten to its native Navidrome ID; then the request continues as usual
(forwarded, or intercepted as placeholder audio).

A commit on a song or an album materializes the **whole release**: a library
album never shows only the committed tracks, and warm-ahead finds the next tracks.
Catalog-only albums get the catalog's cover image. Concurrent commits of one release
materialize it once (a lock per release and a re-check of the database). A catalog artist
has a native equivalent only once Navidrome knows an artist of that name.

Commit on playing, not on fetching ahead: a plain ``stream`` of a catalog song
that is not in the library is served from the add-ons (``play``) and commits nothing -
clients fetch songs they may never play. The client's "now playing" report (``scrobble``,
``reportPlayback``) commits it, as do the other actions. Requests that need the
placeholder (another format, a lower bitrate, an offset, downloads, transcoding, the
jukebox) still commit first.

A saved play queue commits only the album of its current song; other catalog songs
not in the library are left out of the queue Navidrome keeps, until they are played.

An owned album shown complete before its fill: committing a catalog song or album
of its release fills that album - never a second, catalog copy - and an action on the
album itself (a star, a rating, a share of its ID) fills it too. A track of the release
owned in another album plays that owned song until the fill.

No work where it would be wasted: removing a star or a rating from something that is not
in the library changes nothing; jukebox commands go through only when the jukebox is on;
album and artist downloads with catalog tracks are refused anyway.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import anyio

from shijhon.catalog.artwork import raster_type
from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, artwork_url
from shijhon.locks import KeyedLocks
from shijhon.matching.normalize import fold
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.engine import MaterializeError, PlaceholderEngine
from shijhon.proxy.app import Forward, Handler, HandlerResult, RequestContext
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import subsonic_error, subsonic_ok
from shijhon.views.ids import CatalogId
from shijhon.views.virtual import NOT_FOUND, Materialized, rewrite

log = logging.getLogger(__name__)


class _Quiet:
    """One log line per message and minute (a client retrying an error at once would
    repeat it)."""

    def __init__(self, seconds: float = 60.0) -> None:
        self.seconds = seconds
        self._last: dict[str, float] = {}

    def note(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last.get(message, -self.seconds) >= self.seconds:
            self._last[message] = now
            if len(self._last) > 1000:
                self._last = {k: v for k, v in self._last.items() if now - v < self.seconds}
            log.info("%s", message)


# Subsonic methods that commit, and the parameters that may carry catalog IDs.
COMMITS: dict[str, tuple[str, ...]] = {
    "stream": ("id",),
    "download": ("id",),
    "getTranscodeDecision": ("mediaId",),
    "getTranscodeStream": ("mediaId",),
    "jukeboxControl": ("id",),
    "star": ("id", "albumId", "artistId"),
    "unstar": ("id", "albumId", "artistId"),
    "setRating": ("id",),
    "scrobble": ("id",),
    "reportPlayback": ("mediaId",),
    "createPlaylist": ("songId",),
    "updatePlaylist": ("songIdToAdd",),
    "savePlayQueue": ("id", "current"),
    "savePlayQueueByIndex": ("id",),
    "createBookmark": ("id",),
    "createShare": ("id",),
}
_PARALLEL = 4  # releases materialized at the same time for one request
REFUSED_DOWNLOAD = (
    "downloading a whole album, artist or playlist with catalog tracks is not supported"
)


# (call, context, catalog song) -> the song served from the add-ons; None: commit first
CatalogPlay = Callable[[RestCall, RequestContext, CatalogTrack], Awaitable[HandlerResult]]
_QUEUES = ("savePlayQueue", "savePlayQueueByIndex")
# Actions on an owned album's own ID that fill it when it is shown complete.
_ALBUM_ACTIONS: dict[str, tuple[str, ...]] = {
    "star": ("id", "albumId"),
    "setRating": ("id",),
    "createShare": ("id",),
}


class OwnedAlbums(Protocol):
    """Owned albums shown complete before their fill (``fill.fills.Fills``)."""

    async def pending(self, album_id: str) -> bool: ...

    async def backed(self, release_ref: str, track: CatalogRef) -> str | None: ...

    async def first_use(self, release_ref: str, trigger: str) -> str | None: ...

    async def fill_album(self, album_id: str, trigger: str) -> object: ...


class CommitError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Commits:
    def __init__(
        self,
        catalog: Catalog | None,
        engine: PlaceholderEngine,
        navidrome: NavidromeService,
        materialized: Materialized,
        *,
        artwork_size: int = 1200,
        jukebox_enabled: Callable[[], Awaitable[bool]] | None = None,
        server: Callable[[], dict[str, object]] = dict,
        play: CatalogPlay | None = None,
        committed: Callable[[], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.engine = engine
        self.navidrome = navidrome
        self.materialized = materialized
        self.artwork_size = artwork_size
        self.jukebox_enabled = jukebox_enabled
        self.server = server  # Navidrome's envelope fields for Shijhon's own answers
        self.play = play
        self.committed = committed  # after a release was added (the library changed)
        self.owned: OwnedAlbums | None = None  # owned albums shown complete
        self._locks = KeyedLocks()
        self._errors = _Quiet()  # errors answered to clients, in the log
        self.materializations = 0  # observable in tests

    def wrap(self, method: str, inner: Handler | None) -> Handler:
        """The handler for ``method``: commit catalog IDs, then ``inner`` (if any)."""
        keys = set(COMMITS[method])

        async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
            if method in _ALBUM_ACTIONS and self.owned is not None:
                try:
                    await self._album_action(method, call, ctx)
                except Exception as exc:  # the action goes on: never a 500 for the fill
                    log.warning("fill on first use (%s) failed: %s", method, type(exc).__name__)
                    log.debug("fill on first use (%s) failed", method, exc_info=True)
            wanted = {v for k, v in call.params if k in keys and CatalogId.parse(v)}
            if not wanted:
                return await inner(call, ctx) if inner is not None else None
            if await ctx.caller() is None:
                return None  # Navidrome answers with its own credential error
            try:
                return await self._commit(method, keys, wanted, call, ctx, inner)
            except CommitError as exc:
                # A client (a play) may wait on this answer for good: always in the log.
                self._errors.note(
                    f"{method} of a catalog item answered with an error: {exc.message}"
                )
                return subsonic_error(call, exc.code, exc.message, self.server())
            except Exception as exc:  # never a 500 for a catalog item
                log.warning("commit failed: %s", type(exc).__name__)
                return subsonic_error(call, 0, "catalog item unavailable", self.server())

        return handle

    async def _commit(
        self,
        method: str,
        keys: set[str],
        wanted: set[str],
        call: RestCall,
        ctx: RequestContext,
        inner: Handler | None,
    ) -> HandlerResult:
        if method == "jukeboxControl" and (
            call.get("action") not in ("set", "add")
            or (self.jukebox_enabled is not None and not await self.jukebox_enabled())
        ):
            # Nothing to commit. The jukebox's own handler answers: while the jukebox is off,
            # Navidrome's 501 by Shijhon itself - never a forward.
            return await inner(call, ctx) if inner is not None else None
        if method == "download" and any(_kind(v) != "tr" for v in wanted):
            return subsonic_error(call, 0, REFUSED_DOWNLOAD, self.server())
        if method == "unstar" or (method == "setRating" and (call.get("rating") or "0") == "0"):
            return await self._remove(keys, wanted, call)
        if method == "stream" and len(wanted) == 1:
            played = await self._play(next(iter(wanted)), call, ctx)
            if played is not None:
                return played
        if method in _QUEUES:
            committed = await self._queue(method, keys, wanted, call)
        else:
            committed = rewrite(call, keys, await self.natives(wanted, method))
        if inner is not None and (result := await inner(committed, ctx)) is not None:
            return result
        return Forward(committed)

    async def _play(self, text: str, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """A catalog song not in the library yet, played: from the add-ons, without a
        commit (``play``). None: commit first, as for any other request."""
        cid = CatalogId.parse(text)
        if self.play is None or cid is None or cid.kind != "tr":
            return None
        if await self.materialized.song(cid.ref) is not None:
            return None  # in the library: the native song
        track: CatalogTrack = await self._fetch(lambda c: c.song(cid.ref.id), cid)
        if track.album is not None and self.owned is not None:
            song = await self.owned.backed(str(track.album), cid.ref)
            if song is not None:  # owned in another album of the release
                return Forward(rewrite(call, {"id"}, {text: song}))
        return await self.play(call, ctx, track)

    async def _album_action(self, method: str, call: RestCall, ctx: RequestContext) -> None:
        """A star, rating or share of an owned album shown complete: it is filled
        first. A failed fill - whatever the error - leaves the album as it is; the action
        goes on (the album keeps its ID)."""
        assert self.owned is not None
        if method == "setRating" and (call.get("rating") or "0") == "0":
            return
        keys = _ALBUM_ACTIONS[method]
        albums = {v for k, v in call.params if k in keys and "." not in v and v}
        pending = [a for a in sorted(albums) if await self.owned.pending(a)]
        if pending and await ctx.caller() is not None:
            for album_id in pending:
                await self.owned.fill_album(album_id, method)

    async def _queue(
        self, method: str, keys: set[str], wanted: set[str], call: RestCall
    ) -> RestCall:
        """Commit the album of the queue's current song only. Catalog songs not in
        the library are left out of the queue Navidrome keeps (they get in once played)."""
        ids = call.getall("id")
        current = _current(method, call, ids)
        cid = CatalogId.parse(current)
        if current in wanted and cid is not None and cid.kind == "tr":
            await self.native(cid, method)  # a failure is the client's error: its old queue stays
        mapping: dict[str, str] = {}
        for text in wanted:
            item = CatalogId.parse(text)
            assert item is not None
            native = await self.materialized.song(item.ref) if item.kind == "tr" else None
            if native is not None:
                mapping[text] = native

        def kept(value: str) -> bool:
            return value not in wanted or value in mapping

        left_out = sum(1 for v in ids if not kept(v))
        if left_out:
            log.info(
                "play queue: left out %d catalog song(s) not in the library yet (of %d)",
                left_out,
                len(ids),
            )
        index: str | None = None
        position = _index(call)
        if method == "savePlayQueueByIndex" and position is not None and position < len(ids):
            # The current song is kept (it was committed): its place among the kept ones.
            index = str(sum(1 for v in ids[:position] if kept(v)))

        def change(key: str, value: str) -> str | None:
            if key == "currentIndex" and index is not None:
                return index
            return mapping.get(value) if key in keys else None

        return call.rewritten(change, drop=lambda k, v: k in keys and not kept(v))

    async def _remove(self, keys: set[str], wanted: set[str], call: RestCall) -> HandlerResult:
        """Unstar, or clear a rating: only items in the library can carry either."""
        mapping: dict[str, str] = {}
        for text in wanted:
            cid = CatalogId.parse(text)
            assert cid is not None
            if cid.kind == "tr":
                native = await self.materialized.song(cid.ref)
            elif cid.kind == "al":
                native = await self.materialized.album(cid.ref)
            else:
                try:
                    native = await self._artist(cid)
                except CommitError:
                    native = None
            if native is not None:
                mapping[text] = native
        committed = call.rewritten(
            lambda k, v: mapping.get(v) if k in keys else None,
            drop=lambda k, v: k in keys and v in wanted and v not in mapping,
        )
        if not any(k in keys for k, _ in committed.params):
            return subsonic_ok(call, {}, self.server())  # nothing in the library to change
        return Forward(committed)

    async def natives(self, texts: set[str], trigger: str = "commit") -> dict[str, str]:
        mapping: dict[str, str] = {}
        failures: list[CommitError] = []
        limiter = anyio.CapacityLimiter(_PARALLEL)

        async def one(text: str) -> None:
            cid = CatalogId.parse(text)
            assert cid is not None
            async with limiter:
                try:
                    mapping[text] = await self.native(cid, trigger)
                except CommitError as exc:
                    failures.append(exc)

        async with anyio.create_task_group() as tg:
            for text in sorted(texts):
                tg.start_soon(one, text)
        if failures:
            raise failures[0]
        return mapping

    async def native(self, cid: CatalogId, trigger: str = "commit") -> str:
        """The native ID for a catalog item, materializing its release if needed: into
        the owned album it is shown with, else as a catalog album."""
        if cid.kind == "tr":
            found = await self.materialized.song(cid.ref)
            if found is not None:
                return found
            track = await self._fetch(lambda c: c.song(cid.ref.id), cid)
            album = track.album
            if album is None:
                # The catalog's answer named no album: one that can look for it does now.
                album = await self._fetch(lambda c: _album_of(c, cid.ref.id), cid)
            if album is None:
                raise CommitError(NOT_FOUND, "the catalog song has no album")
            if self.owned is not None and await self.owned.first_use(str(album), trigger):
                found = await self.materialized.song(cid.ref)
                if found is not None:
                    return found
                if await self.materialized.album(album) is None:
                    # Never a second, catalog copy of an owned album's release.
                    raise CommitError(0, "could not add the album's missing songs")
            await self._album(album, cid)
            found = await self.materialized.song(cid.ref)
            if found is None:  # e.g. a bonus track a fill left out: add just this one
                await self._album(album, cid, only=cid.ref)
                found = await self.materialized.song(cid.ref)
            if found is None:
                raise CommitError(NOT_FOUND, "the song is not on its album's release")
            return found
        if cid.kind == "al":
            found = await self.materialized.album(cid.ref)
            owned = self.owned
            if found is None and owned is not None and await owned.first_use(str(cid.ref), trigger):
                found = await self.materialized.album(cid.ref)
                if found is None:
                    raise CommitError(0, "could not add the album's missing songs")
            return found if found is not None else await self._album(cid.ref, cid)
        return await self._artist(cid)

    async def _fetch(self, get: Any, cid: CatalogId) -> Any:
        catalog = self.catalog
        if catalog is None or catalog.key != cid.ref.catalog:
            raise CommitError(NOT_FOUND, "catalog item not found")
        try:
            return await get(catalog)
        except CatalogError as exc:
            if exc.kind == "not_found":
                raise CommitError(NOT_FOUND, "catalog item not found") from None
            raise CommitError(0, f"catalog unavailable: {exc.reason}") from None

    async def _album(
        self, ref: CatalogRef, cid: CatalogId, *, only: CatalogRef | None = None
    ) -> str:
        async with self._locks.hold(str(ref)):
            found = await self.materialized.album(ref)
            if found is not None and only is None:
                return found
            started = time.monotonic()
            release: CatalogRelease = await self._fetch(lambda c: c.album(ref.id), cid)
            # A song's commit: its album must list it - checked before anything is written
            # (the album's answer may differ from the one that named it as the song's).
            wanted = only if only is not None else cid.ref if cid.kind == "tr" else None
            if wanted is not None and wanted not in {t.ref for t in release.tracks}:
                raise CommitError(NOT_FOUND, "the song is not on its album's release")
            cover = await self._cover(release) if found is None else None
            try:
                result = await self.engine.materialize(
                    release, cover=cover, only=[only] if only is not None else None
                )
            except MaterializeError as exc:
                log.warning("commit of %s failed: %s", ref, exc.reason)
                raise CommitError(0, f"could not add the catalog album: {exc.reason}") from None
            self.materializations += 1
            if self.committed is not None:
                self.committed()
            log.info(
                "committed %s: %d placeholder(s) in %.1fs",
                ref,
                len(result.created),
                time.monotonic() - started,
            )
            return result.album_id

    async def cover_for(self, release: CatalogRelease) -> bytes | None:
        """The catalog's cover for a catalog album added back (best effort)."""
        return await self._cover(release)

    async def _cover(self, release: CatalogRelease) -> bytes | None:
        """The catalog's cover for a catalog-only album (best effort)."""
        url = artwork_url(release.artwork_template, self.artwork_size)
        if url is None or self.catalog is None:
            return None
        try:
            data, _ = await self.catalog.artwork(url)
        except CatalogError as exc:
            log.info("no cover for %s: %s", release.ref, exc.reason)
            return None
        # (A JPEG by its bytes, whatever the catalog calls it: it is written as cover.jpg.)
        return data if raster_type(data) == "image/jpeg" else None

    async def _artist(self, cid: CatalogId) -> str:
        artist = await self._fetch(lambda c: c.artist(cid.ref.id), cid)
        try:
            found = await self.navidrome.subsonic(
                "search3",
                [
                    ("query", artist.name),
                    ("artistCount", "20"),
                    ("albumCount", "0"),
                    ("songCount", "0"),
                ],
            )
        except NavidromeError as exc:
            raise CommitError(0, f"library search failed: {exc}") from None
        wanted = fold(artist.name)
        for entry in found.get("searchResult3", {}).get("artist", []):
            if fold(entry.get("name")) == wanted:
                return str(entry["id"])
        raise CommitError(NOT_FOUND, "this artist has no albums in the library yet")


async def _album_of(catalog: Any, song_id: str) -> CatalogRef | None:
    """The album of a song whose answer named none, from a catalog that can look for it
    (``catalog.base``: an optional ``album_of``); None from any other."""
    lookup = getattr(catalog, "album_of", None)
    if lookup is None:
        return None
    found: CatalogRef | None = await lookup(song_id)
    return found


def _kind(text: str) -> str | None:
    cid = CatalogId.parse(text)
    return cid.kind if cid else None


def _index(call: RestCall) -> int | None:
    try:
        position = int(call.get("currentIndex") or "")
    except ValueError:
        return None
    return position if position >= 0 else None


def _current(method: str, call: RestCall, ids: list[str]) -> str | None:
    """The queue's current song as the client sent it."""
    if method == "savePlayQueue":
        return call.get("current")
    position = _index(call)
    return ids[position] if position is not None and position < len(ids) else None
