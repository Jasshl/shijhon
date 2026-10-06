"""Refresh from the current catalog: an action on an album in the library
that a catalog release filled or made - a filled owned album, or a committed catalog
album. The service is built (``Services.refresh``); no page or command offers it yet.

The release is read again, without caches, and, under the release's lock:

- tracks it gained are added as placeholders (joining the same album);
- placeholders whose track changed (title, artists, ISRC, number, length, version) are
  rewritten in place - their song IDs, and so every favorite, rating, play count,
  playlist entry and queue, stay; the album must stay the same too;
- positions are planned for the whole release at once: a placeholder of the release that
  moves frees its position for another, while every other song of the album (owned songs,
  delivered audio, tracks the release no longer lists) keeps its own; a track whose
  position stays taken is left out, and so is a placeholder that would move onto it;
- nothing is removed: tracks the release no longer lists stay as they are, and so do
  placeholders with delivered audio;
- album-level tags never change (they keep the album one album);
- the owner's recordings on other albums back the new placeholders and those without a
  backing.

A release from another catalog than the current one is not matched again here (not
built yet): the action says so.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, release_data
from shijhon.navidrome.client import NavidromeService
from shijhon.placeholders.engine import MaterializeError, PlaceholderEngine, ReplaceError
from shijhon.store import Store

log = logging.getLogger(__name__)

Position = tuple[int, int]


@dataclass
class Refreshed:
    """What a refresh did to one release of an album."""

    release: str
    added: int = 0
    updated: int = 0
    kept: list[str] = field(default_factory=list)  # titles no longer on the release
    skipped: list[str] = field(default_factory=list)  # what was left out, and why
    refused: str | None = None  # why nothing was done

    def __str__(self) -> str:
        if self.refused:
            return f"{self.release}: not refreshed ({self.refused})"
        text = f"{self.release}: {self.added} added, {self.updated} updated"
        if self.kept:
            text += f", {len(self.kept)} no longer on the release (kept)"
        if self.skipped:
            text += f", {len(self.skipped)} left out"
        return text


class Refresh:
    """``catalog``: the current catalog, not its cache (the release as it is now)."""

    def __init__(
        self,
        store: Store,
        navidrome: NavidromeService,
        engine: PlaceholderEngine,
        catalog: Catalog,
    ) -> None:
        self.store = store
        self.navidrome = navidrome
        self.engine = engine
        self.catalog = catalog

    async def refresh(self, album_id: str) -> list[Refreshed]:
        """Refresh the album's releases; an empty list when no release made or filled it."""
        rows = await self.store.fetchall(
            "SELECT ref, title, artist FROM releases WHERE album_id = ? OR owned_album_id = ?"
            " ORDER BY created_at",
            [album_id, album_id],
        )
        done = []
        for row in rows:
            result = await self._release(album_id, str(row["ref"]))
            log.info("refreshed %s - %s from the catalog: %s", row["artist"], row["title"],
                     result)  # fmt: skip
            done.append(result)
        return done

    async def _release(self, album_id: str, ref_text: str) -> Refreshed:
        result = Refreshed(ref_text)
        ref = CatalogRef.parse(ref_text)
        if ref.catalog != self.catalog.key:
            result.refused = f"a release of another catalog ({ref.catalog})"
            return result
        try:
            fresh = await self.catalog.album(ref.id)
        except CatalogError as exc:
            result.refused = (
                "the catalog no longer has it" if exc.kind == "not_found" else exc.reason
            )
            return result
        async with self.engine.lock_for(ref_text):
            try:  # placeholders a stop left half written go first: they are not its songs
                await self.engine.settle(ref_text)
            except MaterializeError as exc:
                result.refused = exc.reason
                return result
            if (
                await self.store.fetchone("SELECT 1 FROM releases WHERE ref = ?", [ref_text])
                is None
            ):
                result.refused = "no longer in the library"
                return result
            if (why := await self._unfit(album_id, ref_text, fresh)) is not None:
                result.refused = why
                return result
            new, retag = await self._plan(album_id, ref_text, fresh, result)
            stayed: set[tuple[int, int]] = set()  # positions of placeholders that did not move
            for track, song_id, was in retag:
                try:
                    if await self.engine.retag(song_id, track, fresh):
                        result.updated += 1
                except ReplaceError as exc:
                    result.skipped.append(f"{track.title}: {exc}")
                    stayed.add(was)
            clash = [t for t in new if (t.disc, t.number) in stayed]
            for track in clash:
                result.skipped.append(f"{track.title}: its position is another song's")
            new = [t for t in new if t not in clash]
            if new:
                try:
                    made, _, _ = await self.engine.materialize_held(
                        fresh, only=[t.ref for t in new]
                    )
                except MaterializeError as exc:
                    result.skipped.append(f"{len(new)} new track(s): {exc.reason}")
                else:
                    result.added = len(made.created)
            if result.added or result.updated:
                await self.store.execute(
                    "UPDATE releases SET data = ? WHERE ref = ?",
                    [json.dumps(release_data(fresh)), ref_text],
                )
            unbacked = await self._unbacked(ref_text, fresh)
            linked = [
                str(r["song_id"])
                for r in await self.store.fetchall(
                    "SELECT song_id FROM track_links WHERE release_ref = ? AND owned = 1",
                    [ref_text],
                )
            ]
        # The owner's recordings on other albums (outside the release's lock), once: the
        # new placeholders and those still without a backing.
        await self.engine.back_placeholders(fresh, None, unbacked, linked)
        return result

    async def _unfit(self, album_id: str, ref_text: str, fresh: CatalogRelease) -> str | None:
        """Why an owned album's release is not refreshed from this answer, or None: a track
        list that could not be read in full; or track numbers that are only the order of
        the catalog's list (``numbered`` off) with an owned song elsewhere than that order
        puts it - its placeholders would be moved around files that stay."""
        row = await self.store.fetchone(
            "SELECT owned_album_id FROM releases WHERE ref = ?", [ref_text]
        )
        if row is None or not row["owned_album_id"]:
            return None
        if fresh.incomplete:
            return "the catalog's track list of the release is incomplete"
        if fresh.numbered:
            return None
        links = {
            str(r["track_ref"]): str(r["song_id"])
            for r in await self.store.fetchall(
                "SELECT track_ref, song_id FROM track_links WHERE release_ref = ? AND owned = 1",
                [ref_text],
            )
        }
        places = {
            str(s["id"]): (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0))
            for s in await self.navidrome.songs_of_album(album_id)
        }
        for track in fresh.tracks:
            song = links.get(str(track.ref))
            if song is not None and places.get(song, (track.disc, track.number)) != (
                track.disc,
                track.number,
            ):
                return (
                    "the catalog lists the release's tracks without numbers, and the owned"
                    " files are numbered differently from its order"
                )
        return None

    async def _plan(
        self, album_id: str, ref_text: str, fresh: CatalogRelease, result: Refreshed
    ) -> tuple[list[CatalogTrack], list[tuple[CatalogTrack, str, tuple[int, int]]]]:
        """(tracks to add, placeholders to rewrite with their current positions), with every
        position planned at once."""
        links = {
            str(r["track_ref"])
            for r in await self.store.fetchall(
                "SELECT track_ref FROM track_links WHERE release_ref = ?", [ref_text]
            )
        }
        placeholders = {
            str(r["track_ref"]): r
            for r in await self.store.fetchall(
                "SELECT * FROM placeholders WHERE release_ref = ?", [ref_text]
            )
        }
        fresh_refs = {str(t.ref) for t in fresh.tracks}
        result.kept = [
            str(row["title"]) for key, row in placeholders.items() if key not in fresh_refs
        ]
        # The release's silent placeholders the catalog still lists may move; every other
        # song of the album keeps its position.
        movable: dict[str, tuple[CatalogTrack, Any]] = {}
        for track in fresh.tracks:
            row = placeholders.get(str(track.ref))
            if row is None:
                continue
            if row["state"] == "placeholder":
                movable[str(row["song_id"])] = (track, row)
            elif _changed(track, row):
                result.skipped.append(f"{track.title}: delivered audio is kept as it is")
        fixed: dict[Position, str] = {
            (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0)): str(s["id"])
            for s in await self.navidrome.songs_of_album(album_id)
            if not s.get("missing") and str(s["id"]) not in movable
        }
        wanted = [(track, None) for track in fresh.tracks if str(track.ref) not in links] + [
            (track, song_id) for song_id, (track, _) in movable.items()
        ]
        blocked: set[str] = set()
        while True:  # until no blocked placeholder takes back a position someone wanted
            claims: dict[Position, str] = {}
            newly = False
            for track, song_id in wanted:
                key = str(track.ref)
                if key in blocked:
                    continue
                position = (track.disc, track.number)
                if position in fixed or position in claims:
                    blocked.add(key)
                    newly = True
                    if song_id is not None:  # it stays where it is, as it is
                        row = movable[song_id][1]
                        fixed[(int(row["disc"]), int(row["track"]))] = song_id
                else:
                    claims[position] = key
            if not newly:
                break
        new, retag = [], []
        for track, song_id in wanted:
            key = str(track.ref)
            if key in blocked:
                if song_id is None or _changed(track, movable[song_id][1]):
                    result.skipped.append(f"{track.title}: its position is another song's")
            elif song_id is None:
                new.append(track)
            elif _changed(track, (row := movable[song_id][1])):
                retag.append((track, song_id, (int(row["disc"]), int(row["track"]))))
        return new, retag

    async def _unbacked(
        self, ref_text: str, fresh: CatalogRelease
    ) -> list[tuple[CatalogTrack, str]]:
        """The release's placeholders without a backing, with their current tracks."""
        tracks = {str(t.ref): t for t in fresh.tracks}
        return [
            (tracks[str(r["track_ref"])], str(r["song_id"]))
            for r in await self.store.fetchall(
                "SELECT song_id, track_ref FROM placeholders WHERE release_ref = ?"
                " AND backing_song_id IS NULL AND state = 'placeholder'",
                [ref_text],
            )
            if str(r["track_ref"]) in tracks
        ]


def _changed(track: CatalogTrack, row: Any) -> bool:
    """Whether the catalog's track differs from the placeholder's (as far as the
    placeholder shows it)."""
    tags = json.loads(row["tags"] or "{}")
    artists = list(track.artists) if len(track.artists) > 1 else None
    return (
        track.title != row["title"]
        or track.artist != row["artist"]
        or (track.isrc or None) != (row["isrc"] or None)
        or track.disc != row["disc"]
        or track.number != row["track"]
        or track.duration_ms != row["duration_ms"]
        or tags.get("artists") != artists
        or (track.explicit != (tags.get("itunesadvisory") == ["1"]))
    )
