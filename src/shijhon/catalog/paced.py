"""A catalog whose requests keep to a pace: the library pass asks about one a second
whatever the catalog's own limit for interactive requests. Its requests are background
work: where a catalog's requests wait by urgency (the catalog of an add-on, at that
add-on's request limit), they go after the listeners'."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiolimiter import AsyncLimiter

from shijhon.catalog.base import Catalog, SearchResults
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
)
from shijhon.delivery import pacing


class PacedCatalog:
    """Every call waits for its turn, answered from the cache or not (so the pace holds
    whatever the cache holds). The wrapped catalog belongs to the application."""

    def __init__(self, catalog: Catalog, per_second: float) -> None:
        self.inner = catalog
        self.key = catalog.key
        self.region = catalog.region
        self.per_second = per_second
        self._limiter = AsyncLimiter(1, 1 / per_second)
        self.requests = 0  # observable in tests and progress lines

    async def _turn(self) -> None:
        await self._limiter.acquire()
        self.requests += 1

    async def _paced[T](self, ask: Callable[[], Awaitable[T]]) -> T:
        await self._turn()
        with pacing.urgent(pacing.WARM):  # background work: after the listeners' requests
            return await ask()

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        return await self._paced(lambda: self.inner.search(term, limit))

    async def album(self, album_id: str) -> CatalogRelease:
        return await self._paced(lambda: self.inner.album(album_id))

    async def song(self, song_id: str) -> CatalogTrack:
        return await self._paced(lambda: self.inner.song(song_id))

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        return await self._paced(lambda: self.inner.songs_by_isrc(isrc))

    async def artist(self, artist_id: str) -> CatalogArtist:
        return await self._paced(lambda: self.inner.artist(artist_id))

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        return await self._paced(lambda: self.inner.artist_releases(artist_id))

    async def top_songs(self, artist_id: str, limit: int = 10) -> tuple[CatalogTrack, ...]:
        return await self._paced(lambda: self.inner.top_songs(artist_id, limit))

    async def artists_of(
        self, songs: tuple[str, ...], albums: tuple[str, ...]
    ) -> dict[str, tuple[CatalogRef, ...]]:
        return await self._paced(lambda: self.inner.artists_of(songs, albums))

    async def artwork(self, url: str) -> tuple[bytes, str]:
        return await self._paced(lambda: self.inner.artwork(url))

    async def aclose(self) -> None:
        return None  # the wrapped catalog is closed by its owner
