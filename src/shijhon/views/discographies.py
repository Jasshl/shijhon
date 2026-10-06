"""Artist discographies saved in Shijhon's database for artist pages, and artists'
top songs (getTopSongs of an artist the library does not have) the same way.

A saved list answers an artist page at once; one older than the configured age still does,
and is refreshed in the background. Only an artist without a saved list waits for the
catalog. Other catalog answers keep the catalog's in-memory cache. Top songs share
the table (their keys say ``top``): no schema change.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shijhon.catalog.model import (
    CatalogRelease,
    CatalogTrack,
    release_data,
    release_from_data,
    track_data,
    track_from_data,
)
from shijhon.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Saved:
    releases: tuple[CatalogRelease, ...]
    fresh: bool


@dataclass(frozen=True)
class SavedSongs:
    songs: tuple[CatalogTrack, ...]
    fresh: bool


class Discographies:
    def __init__(
        self, store: Store, *, max_age_seconds: float, clock: Callable[[], float] = time.time
    ) -> None:
        self.store = store
        self.max_age = max_age_seconds
        self.clock = clock

    async def get(self, key: str) -> Saved | None:
        found = await self._read(key, release_from_data, "release")
        return Saved(*found) if found is not None else None

    async def get_songs(self, key: str) -> SavedSongs | None:
        found = await self._read(key, track_from_data, "song")
        return SavedSongs(*found) if found is not None else None

    async def _read[T](
        self, key: str, read: Callable[[Any], T], what: str
    ) -> tuple[tuple[T, ...], bool] | None:
        """(the saved items, whether fresh), or None when nothing readable is saved."""
        row = await self.store.fetchone(
            "SELECT releases, fetched_at FROM discographies WHERE key = ?", [key]
        )
        if row is None:
            return None
        try:
            data = json.loads(row["releases"])
        except ValueError as exc:
            log.info("saved %s list unreadable: %s", what, type(exc).__name__)
            return None
        if not isinstance(data, list):
            return None
        items: list[T] = []
        for item in data:  # each on its own: one unreadable item costs only itself
            try:
                items.append(read(item))
            except Exception as exc:  # written by another version, or damaged
                log.info("a saved %s unreadable: %s", what, type(exc).__name__)
        if data and not items:
            return None  # nothing readable: as if not saved
        # One that lost items is refreshed (in the background) like an old one.
        fresh = len(items) == len(data) and self.clock() - float(row["fetched_at"]) < self.max_age
        return tuple(items), fresh

    async def get_name(self, key: str) -> str | None:
        """A name saved with ``save_name`` (a top song list's artist)."""
        row = await self.store.fetchone("SELECT releases FROM discographies WHERE key = ?", [key])
        try:
            name = json.loads(row["releases"]) if row is not None else None
        except ValueError:
            return None
        return name if isinstance(name, str) and name else None

    async def save_name(self, key: str, name: str) -> None:
        await self._write(key, name)

    async def save(self, key: str, releases: tuple[CatalogRelease, ...]) -> None:
        await self._write(key, [release_data(r) for r in releases])

    async def save_songs(self, key: str, songs: tuple[CatalogTrack, ...]) -> None:
        await self._write(key, [track_data(t) for t in songs])

    async def _write(self, key: str, items: list[dict[str, Any]] | str) -> None:
        data = json.dumps(items)
        await self.store.execute(
            "INSERT INTO discographies (key, releases, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET releases = excluded.releases,"
            " fetched_at = excluded.fetched_at",
            [key, data, self.clock()],
        )
