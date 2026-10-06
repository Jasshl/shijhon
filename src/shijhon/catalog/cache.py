"""TTL cache and request coalescing in front of any catalog.

Concurrent identical requests share one call to the catalog, and answers are reused for
a while, so clients that prefetch every album they list, and search-as-you-type, do not
become per-request catalog traffic. Failures are not cached.

A song shown moments ago - in a search result or an album - is remembered as the catalog
answered it: while the catalog fails (an outage, a service it needs down) or keeps a song
lookup waiting more than a moment, a song looked up by its ID (a play of it) is that song,
so a play does not depend on a second catalog request succeeding (without this, a
catalog failure answers every request of a play with an error). Only for failures:
"not found" stays not found.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any

import anyio
from async_lru import alru_cache

from shijhon.catalog.artwork import ArtworkIndex, raster_type
from shijhon.catalog.base import Catalog, CatalogError, addresses_for_clients
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    parse_item_artist,
)

log = logging.getLogger(__name__)
SHOWN_SONGS = 5000  # songs of recent answers remembered for a catalog failure
SHOWN_WAIT = 2.0  # seconds a remembered song's lookup may take before it is used instead


def _noting[T](
    call: Callable[..., Awaitable[T]], note: Callable[[T], None]
) -> Callable[..., Coroutine[Any, Any, T]]:
    """``call``, with the artwork of what it answers noted on every answer (cached or not)."""

    async def noted(*args: Any, **kwargs: Any) -> T:
        answer = await call(*args, **kwargs)
        note(answer)
        return answer

    if hasattr(call, "cache_clear"):
        noted.cache_clear = call.cache_clear  # type: ignore[attr-defined]
    return noted


class CachedCatalog:
    """``index``: the artwork of every answer is noted there."""

    def __init__(
        self,
        inner: Catalog,
        *,
        ttl: float,
        maxsize: int = 4096,
        index: ArtworkIndex | None = None,
        remember: int = SHOWN_SONGS,
    ) -> None:
        self.inner = inner
        self.key = inner.key
        self.region = inner.region
        self.client_artwork = addresses_for_clients(inner)
        self.index = index or ArtworkIndex()
        index = self.index
        # Songs of recent answers, by catalog ID: a song while the catalog fails (0: none,
        # for a cache no play asks).
        self._shown: OrderedDict[str, CatalogTrack] = OrderedDict()
        self._remember = remember
        self._told: dict[str, float] = {}  # the fallback's log line: once a minute a song

        def searched(results: Any) -> None:
            index.results(results)
            self._keep(results.songs)

        def listed(tracks: tuple[CatalogTrack, ...]) -> None:
            index.tracks(tracks)
            self._keep(tracks)

        def released(release: CatalogRelease) -> None:
            index.releases([release])
            self._keep(release.tracks)

        self.search = _noting(alru_cache(maxsize=maxsize, ttl=ttl)(inner.search), searched)
        self.album = _noting(alru_cache(maxsize=maxsize, ttl=ttl)(inner.album), released)
        self._song = _noting(
            alru_cache(maxsize=maxsize, ttl=ttl)(inner.song), lambda t: index.tracks([t])
        )
        self.songs_by_isrc = alru_cache(maxsize=maxsize, ttl=ttl)(inner.songs_by_isrc)
        self.artist = _noting(
            alru_cache(maxsize=maxsize, ttl=ttl)(self._artist), lambda a: index.artists([a])
        )
        self.artist_releases = _noting(
            alru_cache(maxsize=maxsize, ttl=ttl)(self._artist_releases), index.releases
        )
        self.top_songs = _noting(alru_cache(maxsize=maxsize, ttl=ttl)(self._top_songs), listed)
        # Failures are not cached: a later search asks again.
        self.artists_of = alru_cache(maxsize=maxsize, ttl=ttl)(self._artists_of)
        # Images are larger: fewer are kept (clients' covers are kept on disk).
        self.artwork = alru_cache(maxsize=64, ttl=ttl)(self._artwork)

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def _artwork(self, url: str) -> tuple[bytes, str]:
        """The catalog's image, when it is one Shijhon serves (``raster_type``): anything
        else is a failure, so it is never kept here - the next request asks again."""
        data, content_type = await self.inner.artwork(url)
        if raster_type(data) is None:
            raise CatalogError("invalid", "artwork that is not a JPEG, PNG, GIF or WebP image")
        return data, content_type

    async def song(self, song_id: str) -> CatalogTrack:
        """The catalog's song; while the catalog fails, or keeps the lookup waiting
        more than a moment, the song as a recent answer showed it (a search result, an
        album). A lookup given up on goes on for the next request (shared, cached)."""
        shown = self._shown.get(song_id)
        if shown is None:
            return await self._song(song_id)
        try:
            with anyio.move_on_after(SHOWN_WAIT):
                return await self._song(song_id)
            why = f"no answer within {SHOWN_WAIT:g}s"
        except CatalogError as exc:
            if exc.kind == "not_found":
                raise
            why = exc.reason
        now = time.monotonic()
        if now - self._told.get(song_id, -60.0) >= 60.0:
            self._told[song_id] = now
            if len(self._told) > 1000:
                self._told = {k: v for k, v in self._told.items() if now - v < 60.0}
            log.info("catalog unavailable (%s): %r as a recent answer showed it", why, shown.title)
        return shown

    def remember(self, songs: Iterable[CatalogTrack]) -> None:
        """Songs an answer showed from elsewhere (a saved list): their artwork, and the
        songs themselves for a catalog failure, as if the catalog had just answered."""
        songs = tuple(songs)
        self.index.tracks(songs)
        self._keep(songs)
        # A catalog that cannot be asked for one song (it knows the songs its answers
        # showed) is told these too: a saved list's songs play after a restart.
        told = getattr(self.inner, "remember", None)
        if told is not None:
            told(songs)

    def _keep(self, songs: Iterable[CatalogTrack]) -> None:
        if self._remember <= 0:
            return
        for track in songs:
            self._shown[track.ref.id] = track
            self._shown.move_to_end(track.ref.id)
        while len(self._shown) > self._remember:
            self._shown.popitem(last=False)

    async def album_of(self, song_id: str) -> CatalogRef | None:
        """The album of a song whose catalog answer named none, from a catalog that can
        look for it (an optional ``album_of``; it may cost requests, so only a commit
        asks); None otherwise."""
        lookup = getattr(self.inner, "album_of", None)
        if lookup is None:
            return None
        # (Albums are opened through this cache: the answer the lookup checked is the one
        # the commit then writes.)
        found: CatalogRef | None = await lookup(song_id, self.album)
        return found

    async def _real_artist(self, artist_id: str) -> str:
        """The catalog's own ID for an artist referenced through the song or album that
        credits it (``item_artist``): that item's artist items, one request (cached)."""
        found = parse_item_artist(artist_id)
        if found is None:
            return artist_id
        kind, item_id, index = found
        # The catalog's own answer (a remembered song has no artist items).
        item = await (self._song(item_id) if kind == "t" else self.album(item_id))
        refs = item.artist_refs
        if index < len(refs):
            return refs[index].id
        if len(refs) == 1:  # the credit named more artists than the catalog lists
            return refs[0].id
        raise CatalogError("not_found", "no such artist on that item")

    async def _artists_of(
        self, songs: tuple[str, ...], albums: tuple[str, ...]
    ) -> dict[str, tuple[CatalogRef, ...]]:
        lookup = getattr(self.inner, "artists_of", None)
        return await lookup(songs, albums) if lookup is not None else {}

    async def _top_songs(self, artist_id: str, limit: int = 10) -> tuple[CatalogTrack, ...]:
        lookup = getattr(self.inner, "top_songs", None)
        if lookup is None:
            return ()
        found: tuple[CatalogTrack, ...] = await lookup(await self._real_artist(artist_id), limit)
        return found

    async def _artist(self, artist_id: str) -> CatalogArtist:
        return await self.inner.artist(await self._real_artist(artist_id))

    async def _artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        return await self.inner.artist_releases(await self._real_artist(artist_id))
