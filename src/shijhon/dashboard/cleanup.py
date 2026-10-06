"""The Cleanup page's data: what Shijhon added to the
library, by origin, the cleanup's last check and what it listed, and downloaded audio
against its limits.

A cheap view: Shijhon's own records and what the last check kept in memory. The page
never reads Navidrome's database: only a check does - the daily one, or the dashboard's
dry run (a button, paced).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from shijhon.cleanup import Candidate, Listing, Swept
from shijhon.store import Store

PAGE = 25  # listed releases a page
DRY_RUN_EVERY = 60.0  # seconds between the dashboard's dry runs
PREVIEW_SECONDS = 20.0  # how long the switch to "on" waits for its dry run
SHOWN_NAMES = 10  # releases named in the confirmation


@dataclass
class Origin:
    releases: int = 0  # with placeholders in the library
    songs: int = 0  # placeholders
    delivered: int = 0  # of those, with downloaded audio in place


@dataclass
class Overview:
    catalog: Origin = field(default_factory=Origin)  # catalog albums (a commit)
    fills: Origin = field(default_factory=Origin)  # songs added to partly owned albums
    removed_catalog: int = 0  # releases taken out, kept as records
    removed_fills: int = 0
    removed_songs: int = 0

    @property
    def delivered(self) -> int:
        return self.catalog.delivered + self.fills.delivered


async def overview(store: Store) -> Overview:
    """Shijhon's own records, a few counting queries."""
    found = Overview()
    rows = await store.fetchall(
        "SELECT r.owned_album_id IS NULL AS catalog, COUNT(DISTINCT p.release_ref) AS releases,"
        " COUNT(p.song_id) AS songs, COALESCE(SUM(p.state = 'delivered'), 0) AS delivered"
        " FROM releases r JOIN placeholders p ON p.release_ref = r.ref"
        " GROUP BY r.owned_album_id IS NULL"
    )
    for row in rows:
        origin = found.catalog if row["catalog"] else found.fills
        origin.releases, origin.songs = int(row["releases"]), int(row["songs"])
        origin.delivered = int(row["delivered"])
    for row in await store.fetchall(
        "SELECT owned_album_id IS NULL AS catalog, COUNT(*) AS n FROM removed_releases"
        " GROUP BY owned_album_id IS NULL"
    ):
        if row["catalog"]:
            found.removed_catalog = int(row["n"])
        else:
            found.removed_fills = int(row["n"])
    songs = await store.fetchone("SELECT COUNT(*) AS n FROM removed_songs")
    found.removed_songs = int(songs["n"]) if songs else 0
    return found


@dataclass
class Row:
    """One release the last check listed as due."""

    title: str
    artist: str
    fill: bool
    added: float
    songs: int
    state: str  # "out" (taken out, or would be) | "kept" | "failed" | "left"
    words: str


def rows(swept: Swept) -> list[Row]:
    """The last check's releases due: those it takes out (or would) first, then those kept
    and why."""
    listed = swept.listed
    if listed is None:
        return []
    dry = swept.mode != "on"
    found = []
    for candidate in listed.due:
        state, words = _state(candidate, swept, dry)
        found.append(
            Row(
                candidate.title or "Unknown album",
                candidate.artist or "Unknown artist",
                candidate.fill,
                candidate.created_at,
                len(candidate.songs),
                state,
                words,
            )
        )
    order = {"out": 0, "failed": 1, "left": 2, "kept": 3}
    return sorted(found, key=lambda r: (order[r.state], r.added))


def _state(candidate: Candidate, swept: Swept, dry: bool) -> tuple[str, str]:
    if candidate.kept:
        return "kept", "Kept, in use: " + ", ".join(candidate.kept)
    if swept.listed is not None and swept.listed.refused:
        if candidate.stranger:
            return "left", "Not taken out: its songs are not in Navidrome's records as recorded"
        return "left", "Not taken out: Navidrome's records cannot say who uses it"
    if dry:
        return "out", "Would be taken out"
    outcome = swept.outcomes.get(candidate.ref, "")
    if outcome == "taken out":
        return "out", "Taken out"
    if outcome.startswith("kept: "):
        return "kept", "Kept, found in use as it was taken out: " + outcome.removeprefix("kept: ")
    if outcome == "failed":
        return "failed", "Not taken out: it failed (tried again at the next check)"
    if outcome == "gone":
        return "left", "Not taken out: it was no longer in the library as listed"
    if swept.refused:
        return "left", f"Not taken out: {swept.refused}"
    if swept.stopped == "switched off":
        return "left", "Not taken out: the cleanup was switched off meanwhile"
    if swept.stopped:
        return "left", f"Not taken out: the check stopped ({swept.stopped}; see the log)"
    return "left", "Not taken out"


@dataclass
class Counts:
    out: int = 0
    kept: int = 0
    failed: int = 0
    left: int = 0
    songs_out: int = 0


def counts(listed_rows: list[Row]) -> Counts:
    found = Counts()
    for row in listed_rows:
        setattr(found, row.state, getattr(found, row.state) + 1)
        if row.state == "out":
            found.songs_out += row.songs
    return found


def page_of(listed_rows: list[Row], page: int) -> tuple[list[Row], int, int]:
    """One page of the listed releases, the page shown and the number of pages."""
    pages = max(1, -(-len(listed_rows) // PAGE))
    page = min(max(1, page), pages)
    return listed_rows[(page - 1) * PAGE : page * PAGE], page, pages


@dataclass
class Preview:
    """What the next check would take out, for the confirmation of the switch to "on"."""

    listing: Listing | None = None
    problem: str = ""  # why there is no listing

    @property
    def unused(self) -> list[Candidate]:
        return self.listing.unused if self.listing is not None else []

    @property
    def songs(self) -> int:
        return sum(len(c.songs) for c in self.unused)

    def names(self) -> list[str]:
        return [f"{c.artist} - {c.title}" for c in self.unused[:SHOWN_NAMES]]


def next_dry_run(cleanup: Any) -> float | None:
    """When the dashboard's next dry run may start (wall clock), or None: now."""
    if cleanup.manual_at is None:
        return None
    at = float(cleanup.manual_at) + DRY_RUN_EVERY
    return at if at > time.time() else None
