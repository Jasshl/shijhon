"""The Library page's data: the album matches' counts, the library pass's progress, the
review list with the releases it considered, and the review actions that run in the
background (a fill waits for Navidrome's scan: up to a couple of minutes).

Everything is read from ``album_matches`` (one scope: the running catalog and region)
and done through ``Fills``' public methods; nothing here matches or fills by itself.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio

from shijhon.store import Store

PAGE = 25  # review rows a page
DESCRIBE_SECONDS = 5.0  # a page's wait for the catalog to describe candidate releases
DESCRIBED_SECONDS = 86400.0
UNDESCRIBED_SECONDS = 900.0  # a release the catalog could not describe: asked again later
JOB_SHOWN_SECONDS = 900.0  # a finished action's notice

# The matcher's and the fills' reasons, as a headline and a sentence (others: shown as
# they are).
_REASONS: dict[str, tuple[str, str]] = {
    "several plausible editions": (
        "Several releases match",
        "More than one edition fits the owned songs.",
    ),
    "the owned tracks are only on releases with other titles": (
        "Only on releases with other titles",
        "The owned songs are on releases under other names, such as a compilation.",
    ),
    "the release numbers its tracks differently from the owned files": (
        "Track numbers differ",
        "The release numbers its tracks differently from your files.",
    ),
    "only a larger edition matches; the owned album may be complete": (
        "Only a larger edition matches",
        "Your album may be complete as it is.",
    ),
    "the catalog's track list could not be read in full": (
        "The track list is incomplete",
        "The catalog's track list could not be read in full.",
    ),
    "the release is in the library as another album": (
        "Already in the library",
        "The release is in the library as another album.",
    ),
    "the placeholders did not join the album": (
        "The fill did not join the album",
        "The added songs did not join your album after Navidrome's scan.",
    ),
}
_PART = re.compile(r"^part of (?P<album>.+?) \(album [^)]*\): (?P<state>.+)$", re.DOTALL)


def is_part(reason: str) -> bool:
    """A part of another owned album's release: never filled on its own."""
    return (reason or "").startswith("part of ")


def explain(reason: str) -> tuple[str, str, bool]:
    """(headline, sentence, whether the album is part of another's release)."""
    if is_part(reason):
        part = _PART.match(reason)
        sentence = f"{part['album']}: {part['state']}." if part else reason[len("part of ") :]
        return "Part of another album", sentence, True
    if reason in _REASONS:
        headline, sentence = _REASONS[reason]
        return headline, sentence, False
    text = (reason or "No reason given").strip()
    return text[0].upper() + text[1:], "", False


@dataclass
class Counts:
    would_fill: int = 0  # a dry run's plan, and deferred albums the fill policy allows now
    filled: int = 0
    deferred: int = 0  # shown complete, filled on first use
    complete: int = 0
    review: int = 0
    none: int = 0  # no match
    failed: int = 0
    kept: int = 0

    @property
    def checked(self) -> int:
        """Every album with an outcome, those without a match and failures included."""
        return sum(self.__dict__.values())


async def counts(store: Store, scope: str, library: set[str] | None, policy: Any = None) -> Counts:
    """The matches of the albums in the library (``library``: their IDs; None: unknown, all
    rows count), and albums filled under another catalog or region as filled. With
    the fill ``policy``, what an automatic fill would fill counts as "would fill": a dry
    run's plan or a deferred album that it allows now - the pass switched on fills it at its
    next run, and a view fills it while the pass is off - not one whose fill the cleanup
    took out (filled only on its next use); a plan it does not allow is filled on
    first use."""
    rows = await store.fetchall(
        "SELECT album_id, outcome, planned, owned_songs, release_tracks, cleaned_at"
        " FROM album_matches WHERE scope = ?",
        [scope],
    )
    filled = await store.fetchall(
        "SELECT owned_album_id FROM releases WHERE owned_album_id IS NOT NULL"
    )
    found = Counts()
    seen: set[str] = set()
    releases = {row["owned_album_id"] for row in filled}
    for row in rows:
        if library is not None and row["album_id"] not in library:
            continue  # an album no longer in the library
        seen.add(row["album_id"])
        outcome = row["outcome"]
        if row["album_id"] in releases:  # filled (also under another catalog): as it is
            found.filled += 1
        elif outcome == "filled" and row["planned"]:  # a dry run's plan: the policy decides
            if policy is None or allowed(row, policy):
                found.would_fill += 1
            else:
                found.deferred += 1
        elif outcome == "deferred" and allowed(row, policy):
            found.would_fill += 1
        elif hasattr(found, outcome):
            setattr(found, outcome, getattr(found, outcome) + 1)
    for row in filled:
        album_id = row["owned_album_id"]
        if album_id not in seen and (library is None or album_id in library):
            seen.add(album_id)
            found.filled += 1
    return found


def allowed(row: Any, policy: Any) -> bool:
    """A match with a plan that the fill policy allows now (not one whose fill the cleanup
    took out: never filled automatically)."""
    if policy is None or row["cleaned_at"] is not None:
        return False
    return bool(policy.allows(int(row["owned_songs"] or 0), int(row["release_tracks"] or 0)))


@dataclass
class ReviewRow:
    album_id: str
    title: str
    artist: str
    owned: int
    tracks: int
    headline: str
    sentence: str
    part: bool
    refs: list[str]  # the releases considered (the chosen one first)
    planned: bool  # from a dry run


async def review(store: Store, scope: str, page: int) -> tuple[list[ReviewRow], int, int]:
    """One page of the review list (artist, then title), the list's length, and the page
    shown (a page past the end shows the last one)."""
    total_row = await store.fetchone(
        "SELECT COUNT(*) AS n FROM album_matches WHERE scope = ? AND outcome = 'review'",
        [scope],
    )
    total = int(total_row["n"]) if total_row else 0
    page = min(max(1, page), max(1, -(-total // PAGE)))
    rows = await store.fetchall(
        "SELECT album_id, title, artist, owned_songs, release_tracks, reason, candidates,"
        " release_ref, planned FROM album_matches WHERE scope = ? AND outcome = 'review'"
        " ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, album_id LIMIT ? OFFSET ?",
        [scope, PAGE, (page - 1) * PAGE],
    )
    found = []
    for row in rows:
        headline, sentence, part = explain(row["reason"] or "")
        refs = [row["release_ref"]] if row["release_ref"] else []
        refs += [r for r in (row["candidates"] or "").split(",") if r and r not in refs]
        found.append(
            ReviewRow(
                row["album_id"],
                row["title"] or "Unknown album",
                row["artist"] or "Unknown artist",
                int(row["owned_songs"] or 0),
                int(row["release_tracks"] or 0),
                headline,
                sentence,
                part,
                refs,
                bool(row["planned"]),
            )
        )
    return found, total, page


def release_label(release: Any) -> str:
    """ "Deluxe edition, 2020, 18 songs (clean)"."""
    songs = len(release.tracks) or release.track_count or 0
    parts = [release.title]
    if release.year:
        parts.append(str(release.year))
    if songs:
        parts.append(f"{songs} song{'s' if songs != 1 else ''}")
    label = ", ".join(parts)
    if release.clean:
        label += " (clean)"
    elif release.explicit:
        label += " (explicit)"
    return label


class Describer:
    """Words for candidate releases, from the (cached) catalog, remembered for a day. A
    page waits at most a few seconds; what is not described by then is named by its ID
    (a reload shows more)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._known: dict[str, tuple[float, str]] = {}

    def label(self, ref: str) -> str | None:
        known = self._known.get(ref)
        return known[1] if known is not None and known[1] else None

    async def describe(self, catalog: Any, refs: list[str]) -> None:
        """Ask the catalog about the releases not described yet: two at a time (behind
        the listeners' requests at its rate), a failure remembered for a quarter of an
        hour. Pass the library pass's own cached catalog where there is one, so these
        answers do not push listeners' out of the shared cache."""
        now = self.clock()
        wanted = []
        for ref in dict.fromkeys(refs):
            known = self._known.get(ref)
            age = DESCRIBED_SECONDS if known is None or known[1] else UNDESCRIBED_SECONDS
            if known is None or now - known[0] > age:
                wanted.append(ref)
        if catalog is None or not wanted:
            return
        limiter = anyio.Semaphore(2)

        async def one(ref: str) -> None:
            key, _, item = ref.partition(":")
            if key != getattr(catalog, "key", None) or not item:
                return
            async with limiter:
                try:
                    release = await catalog.album(item)
                except Exception:
                    self._known[ref] = (self.clock(), "")  # not asked again for a while
                    return
            self._known[ref] = (self.clock(), release_label(release))

        with anyio.move_on_after(DESCRIBE_SECONDS):
            async with anyio.create_task_group() as group:
                for ref in wanted:
                    group.start_soon(one, ref)


@dataclass
class Job:
    """A review action running in the background, or its result."""

    album_id: str
    title: str
    action: str  # fill | rematch
    started: float
    done: float | None = None
    ok: bool = False
    message: str = ""


@dataclass
class Jobs:
    clock: Callable[[], float] = time.time
    _jobs: dict[str, Job] = field(default_factory=dict)
    _shown: dict[str, set[int]] = field(default_factory=dict)  # viewer -> jobs shown
    _one_fill: anyio.Lock | None = None

    @property
    def one_fill(self) -> anyio.Lock:
        """Fills run one at a time (made in the event loop, on first use)."""
        if self._one_fill is None:
            self._one_fill = anyio.Lock()
        return self._one_fill

    def running(self, album_id: str) -> Job | None:
        job = self._jobs.get(album_id)
        return job if job is not None and job.done is None else None

    def start(self, album_id: str, title: str, action: str) -> Job:
        job = Job(album_id, title, action, self.clock())
        self._jobs[album_id] = job
        return job

    def claim(self, album_id: str, title: str, action: str) -> Job | None:
        """Start an action unless one is running for the album (no await in between)."""
        if self.running(album_id) is not None:
            return None
        return self.start(album_id, title, action)

    def drop(self, job: Job) -> None:
        """An action answered on the page itself: nothing to show later."""
        if self._jobs.get(job.album_id) is job:
            del self._jobs[job.album_id]

    def finish(self, job: Job, *, ok: bool, message: str) -> None:
        job.done, job.ok, job.message = self.clock(), ok, message

    def recent(self, viewer: str = "") -> list[Job]:
        """Finished in the last quarter of an hour and not shown to this viewer yet, newest
        first; older ones forgotten."""
        now = self.clock()
        for album_id in [
            a
            for a, j in self._jobs.items()
            if j.done is not None and now - j.done > JOB_SHOWN_SECONDS
        ]:
            del self._jobs[album_id]
        live = {id(j) for j in self._jobs.values()}
        shown = self._shown.setdefault(viewer, set()) & live  # forget what is gone
        done = [j for j in self._jobs.values() if j.done is not None and id(j) not in shown]
        shown.update(id(j) for j in done)
        self._shown[viewer] = shown
        if len(self._shown) > 100:
            self._shown = {viewer: shown}
        return sorted(done, key=lambda j: j.done or 0, reverse=True)


async def library_albums(navidrome: Any, store: Store) -> set[str] | None:
    """The owned albums in the library (IDs): Navidrome's albums less those only the
    catalog made. None when Navidrome cannot be asked."""
    try:
        with anyio.fail_after(10):
            albums = await navidrome.album_counts()
    except Exception:
        return None
    rows = await store.fetchall("SELECT album_id FROM releases WHERE owned_album_id IS NULL")
    catalog_only = {row["album_id"] for row in rows}
    return {album_id for album_id, *_ in albums if album_id not in catalog_only}
