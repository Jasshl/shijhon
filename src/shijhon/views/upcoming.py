"""Upcoming songs for warm-ahead, whether their album is in the library yet or not:
a placeholder that still needs add-on audio, or a catalog song not in the library (then
keyed by its catalog track). Owned songs and delivered placeholders need no add-on.
"""

from __future__ import annotations

import logging
from typing import Any

from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.model import CatalogRef
from shijhon.delivery.download_first import catalog_track, track_of
from shijhon.delivery.playback import Track
from shijhon.store import Store

log = logging.getLogger(__name__)


def _needs_addon(row: Any) -> bool:
    return row["state"] == "placeholder" and not row["backing_song_id"]


class UpcomingSongs:
    def __init__(self, store: Store, catalog: Catalog | None) -> None:
        self.store = store
        self.catalog = catalog

    async def track(self, key: str) -> Track | None:
        column = "track_ref" if ":" in key else "song_id"
        row = await self.store.fetchone(
            f"SELECT * FROM placeholders WHERE {column} = ?",  # noqa: S608
            [key],
        )
        if row is not None:
            return track_of(row) if _needs_addon(row) else None
        if ":" not in key or self._ref(key) is None:
            return None  # an owned song
        linked = await self.store.fetchone("SELECT 1 FROM track_links WHERE track_ref = ?", [key])
        if linked is not None:
            return None  # an owned recording on a filled album
        ref = self._ref(key)
        assert ref is not None and self.catalog is not None
        try:
            return catalog_track(await self.catalog.song(ref.id))
        except CatalogError as exc:
            log.info("no upcoming song: %s", exc.reason)
            return None

    async def after(self, track: Track, depth: int) -> list[Track]:
        if track.release is None or depth <= 0:
            return []
        committed = await self.store.fetchone(
            "SELECT 1 FROM releases WHERE ref = ?", [track.release]
        )
        if committed is not None:
            rows = await self.store.fetchall(
                "SELECT * FROM placeholders WHERE release_ref = ? AND state = 'placeholder'"
                " AND backing_song_id IS NULL AND (disc > ? OR (disc = ? AND track > ?))"
                " ORDER BY disc, track LIMIT ?",
                [track.release, track.disc, track.disc, track.number, depth],
            )
            return [track_of(row) for row in rows]
        ref = self._ref(track.release)
        if ref is None or self.catalog is None:
            return []
        try:
            release = await self.catalog.album(ref.id)
        except CatalogError as exc:
            log.info("no upcoming songs: %s", exc.reason)
            return []
        later = sorted(
            (t for t in release.tracks if (t.disc, t.number) > (track.disc, track.number)),
            key=lambda t: (t.disc, t.number),
        )
        return [catalog_track(t) for t in later[:depth]]

    def _ref(self, key: str) -> CatalogRef | None:
        try:
            ref = CatalogRef.parse(key)
        except ValueError:
            return None
        catalog = self.catalog
        return ref if catalog is not None and ref.catalog == catalog.key else None
