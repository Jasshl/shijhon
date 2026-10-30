"""The ``sound_facts`` table: one row per song with facts, keyed by Navidrome song ID.

Raw values; the percentiles are worked out from all rows when they are read
(``facts.Distribution``). ``item_id`` holds AudioMuse-AI's fingerprint of the song's analysis
(its ``fp``): a song whose fingerprint is unchanged is not fetched again. The table is a
cache that can be rebuilt.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

import aiosqlite

from shijhon.audiomuse.facts import Analysis, Distribution
from shijhon.playlists.sound import FEATURES
from shijhon.store import Store

CHUNK = 500  # song IDs in one statement


def _chunks(items: Sequence[str]) -> Iterable[list[str]]:
    for start in range(0, len(items), CHUNK):
        yield list(items[start : start + CHUNK])


class FactsTable:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def fingerprints(self) -> dict[str, str]:
        """Every song with facts, and the fingerprint of the analysis they came from."""
        rows = await self.store.fetchall("SELECT song_id, item_id FROM sound_facts")
        return {row["song_id"]: row["item_id"] for row in rows}

    async def count(self) -> int:
        row = await self.store.fetchone("SELECT COUNT(*) AS n FROM sound_facts")
        return int(row["n"]) if row else 0

    async def last_synced(self) -> float:
        """When a sync last confirmed a song (0: never)."""
        row = await self.store.fetchone("SELECT MAX(synced) AS at FROM sound_facts")
        return float(row["at"]) if row and row["at"] is not None else 0.0

    async def present(self, ids: Sequence[str]) -> set[str]:
        """Of the songs, those with facts."""
        found: set[str] = set()
        for chunk in _chunks(list(dict.fromkeys(ids))):
            rows = await self.store.fetchall(
                "SELECT song_id FROM sound_facts WHERE song_id IN (SELECT value FROM json_each(?))",
                [json.dumps(chunk)],
            )
            found.update(row["song_id"] for row in rows)
        return found

    async def rows(self, ids: Sequence[str]) -> dict[str, Analysis]:
        found: dict[str, Analysis] = {}
        for chunk in _chunks(list(dict.fromkeys(ids))):
            rows = await self.store.fetchall(
                "SELECT * FROM sound_facts WHERE song_id IN (SELECT value FROM json_each(?))",
                [json.dumps(chunk)],
            )
            found.update((row["song_id"], _analysis(row)) for row in rows)
        return found

    async def distribution(self) -> Distribution:
        rows = await self.store.fetchall("SELECT energy, features FROM sound_facts")
        return Distribution(
            [row["energy"] for row in rows], [_features(row["features"]) for row in rows]
        )

    async def put(self, rows: Sequence[Analysis], at: float) -> None:
        """Insert or replace the songs' facts, confirmed at ``at``."""
        if not rows:
            return
        async with self.store.transaction() as conn:
            await conn.executemany(
                "INSERT INTO sound_facts (song_id, item_id, tempo, musical_key, energy,"
                " features, labels, synced) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (song_id) DO UPDATE SET item_id = excluded.item_id,"
                " tempo = excluded.tempo, musical_key = excluded.musical_key,"
                " energy = excluded.energy, features = excluded.features,"
                " labels = excluded.labels, synced = excluded.synced",
                [
                    (
                        r.song_id,
                        r.fingerprint,
                        r.tempo,
                        r.key,
                        r.energy,
                        json.dumps(r.features),
                        json.dumps(r.labels),
                        at,
                    )
                    for r in rows
                ],
            )

    async def confirm(self, ids: Sequence[str], at: float) -> None:
        """The songs' facts are still current at ``at``."""
        async with self.store.transaction() as conn:
            for chunk in _chunks(list(ids)):
                await conn.execute(
                    "UPDATE sound_facts SET synced = ?"
                    " WHERE song_id IN (SELECT value FROM json_each(?))",
                    [at, json.dumps(chunk)],
                )

    async def delete(self, ids: Sequence[str]) -> int:
        """Remove the songs' facts; how many there were."""
        removed = 0
        async with self.store.transaction() as conn:
            for chunk in _chunks(list(ids)):
                cursor = await conn.execute(
                    "DELETE FROM sound_facts WHERE song_id IN (SELECT value FROM json_each(?))",
                    [json.dumps(chunk)],
                )
                removed += cursor.rowcount
        return removed

    async def placeholders(self) -> set[str]:
        """Songs that are silent placeholders now: no audio to describe."""
        rows = await self.store.fetchall(
            "SELECT song_id FROM placeholders WHERE state = 'placeholder'"
        )
        return {row["song_id"] for row in rows}

    async def gone(self, library_id: str) -> set[str]:
        """Songs Navidrome's library no longer has, as Shijhon's index knows them
        (a song it never indexed is not in this list)."""
        rows = await self.store.fetchall(
            "SELECT DISTINCT t.song_id FROM collection_tracks t"
            " WHERE t.library_id = ? AND t.present = 0"
            " AND NOT EXISTS (SELECT 1 FROM collection_tracks p"
            " WHERE p.library_id = t.library_id AND p.song_id = t.song_id AND p.present = 1)",
            [library_id],
        )
        return {row["song_id"] for row in rows}


def _features(text: str) -> dict[str, float]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if k in FEATURES and isinstance(v, (int, float))}


def _analysis(row: aiosqlite.Row) -> Analysis:
    try:
        labels = json.loads(row["labels"])
    except ValueError:
        labels = []
    return Analysis(
        row["song_id"],
        row["item_id"],
        row["tempo"],
        row["musical_key"],
        row["energy"],
        _features(row["features"]),
        [str(label) for label in labels] if isinstance(labels, list) else [],
    )
