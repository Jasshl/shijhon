"""The track lists of added releases, read again from time to time.

A release in the library - a catalog album someone added, or the release that filled an
owned album - keeps the track list the catalog had when it was added. The library pass,
after its albums, reads the lists of the current catalog's releases again
(``fill.refresh``), a few a run:

- each release about once a month, and once a week while it is new (its release date
  ahead, or no more than two months back: lists still change then; a date needs its
  month for that); first that long after it was added;
- at most a share of them a run (``per_run``), the longest due first, so that a library
  whose releases all come due together - a first run, or after a long stop - asks the
  catalog over many runs instead of at once; each request takes its turn in the pass's
  pace, one release at a time, only while no opened or shown album waits and the catalog
  does not rest;
- only two things change (the refresh's narrow mode): tracks the catalog added become
  placeholders where no song of the album is, and a silent placeholder whose length the
  catalog corrected gets that length. Everything else stays: titles, credits, ISRCs and
  explicit marks, every disc and number, every song ID, owned files, delivered and
  imported audio, tracks the catalog no longer lists. A song the catalog lists
  differently in another way is counted with what was left as it is, and so is a new
  track that is, or may be, a recording the album already has under another ID;
- the owner's recordings on other albums are looked for again, for the release's
  placeholders without one, only when a track was added or corrected;
- the pass's **dry run** writes nothing: it records what it would add and correct
  (``pending``), and the first run switched on reads those releases again and applies
  them;
- a catalog that fails is left alone (it rests, as after a failed match) and the run
  ends there: nothing changes, the release it failed for is asked again a day later, the
  others at the next run. A release the catalog no longer has stays as it is. One whose
  tracks or corrections could not all be written is read again a week later (not sooner:
  what cannot be written for good is not written and taken out again every day).

What each reading led to is kept by release (``release_refreshes``): when it was read,
and the counts the Library page shows.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import anyio

from shijhon.catalog.base import CatalogError
from shijhon.delivery.addon import NEEDS_SIGN_IN
from shijhon.fill.fills import Fills
from shijhon.fill.refresh import Refresh, Refreshed
from shijhon.navidrome.client import NavidromeError
from shijhon.store import Store

log = logging.getLogger(__name__)

DAY = 86400.0
OLD_SECONDS = 30 * DAY  # a release is read again this long after its last reading
NEW_SECONDS = 7 * DAY  # ... and this long while it is new
NEW_DAYS = 60  # new: released no longer ago than this (or not yet)
RETRY_SECONDS = DAY  # after a reading that failed
UNWRITTEN_SECONDS = 7 * DAY  # after one whose changes could not all be written
MIN_PER_RUN = 20  # releases a run reads at least, when that many are due
HEADROOM = 2.0  # a run may read this many times its even share of the releases


@dataclass(frozen=True)
class Entry:
    """An added release and its last reading."""

    ref: str
    album_id: str
    added_at: float
    release_date: str | None
    checked_at: float | None = None
    outcome: str | None = None


def is_new(release_date: str | None, now: float) -> bool:
    """Whether a release is new enough for its list to change still: released no more than
    ``NEW_DAYS`` ago, or not yet. A date without its day counts to the end of its month;
    one without its month, none, or one that cannot be read: not new."""
    parts = (release_date or "").split("-")
    try:
        year, month = int(parts[0]), int(parts[1])
        day = date(year, month, int(parts[2]) if len(parts) > 2 else 1)
        if len(parts) == 2:  # the month's end
            day = date(year + month // 12, month % 12 + 1, 1)
    except (ValueError, IndexError):
        return False
    today = datetime.fromtimestamp(now, UTC).date()
    return (today - day).days <= NEW_DAYS


def due_at(entry: Entry, now: float, *, apply: bool) -> float:
    """When the release's list is to be read next. ``apply``: the pass is switched on - what
    a dry run found is read again and applied at once."""
    if entry.checked_at is None:
        return entry.added_at + (NEW_SECONDS if is_new(entry.release_date, now) else OLD_SECONDS)
    if entry.outcome == "failed":
        return entry.checked_at + RETRY_SECONDS
    if entry.outcome == "unwritten":
        return entry.checked_at + UNWRITTEN_SECONDS
    if entry.outcome == "pending" and apply:
        return entry.checked_at
    return entry.checked_at + (NEW_SECONDS if is_new(entry.release_date, now) else OLD_SECONDS)


def per_run(entries: list[Entry], now: float, run_seconds: float) -> int:
    """How many releases one run reads at most: ``HEADROOM`` times what keeps up with all
    of them when runs come every ``run_seconds``, and ``MIN_PER_RUN`` at least."""
    new = sum(1 for e in entries if is_new(e.release_date, now))
    share = run_seconds * ((len(entries) - new) / OLD_SECONDS + new / NEW_SECONDS)
    return max(MIN_PER_RUN, math.ceil(HEADROOM * share))


def due(
    entries: list[Entry], now: float, *, apply: bool, run_seconds: float
) -> tuple[list[Entry], int]:
    """(The releases this run reads - the longest due first -, how many more are due.)"""
    waiting = sorted(
        ((due_at(e, now, apply=apply), e.ref, e) for e in entries),
        key=lambda item: item[:2],
    )
    ready = [e for at, _, e in waiting if at <= now]
    limit = per_run(entries, now, run_seconds)
    return ready[:limit], max(0, len(ready) - limit)


class TrackLists:
    def __init__(
        self,
        store: Store,
        refresh: Refresh,  # on the catalog as it is now, at the pass's pace
        fills: Fills,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.refresh = refresh
        self.fills = fills
        self.enabled = enabled  # ``[fill] refresh_track_lists``; read before every release
        self.clock = clock
        self.last: dict[str, int] = {}  # the last run's counts

    async def entries(self) -> list[Entry]:
        """The current catalog's releases in the library, none being taken out."""
        prefix = f"{self.refresh.catalog.key}:"
        async with (
            self.store.snapshot() as conn,
            conn.execute(
                "SELECT r.ref, COALESCE(r.owned_album_id, r.album_id) AS album_id,"
                " r.created_at, json_extract(r.data, '$.release_date') AS release_date,"
                " f.checked_at, f.outcome FROM releases r"
                " LEFT JOIN release_refreshes f ON f.ref = r.ref"
                " WHERE r.removing_at IS NULL AND substr(r.ref, 1, ?) = ?",
                [len(prefix), prefix],
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [
            Entry(
                str(row["ref"]),
                str(row["album_id"]),
                float(row["created_at"]),
                str(row["release_date"]) if row["release_date"] else None,
                float(row["checked_at"]) if row["checked_at"] is not None else None,
                str(row["outcome"]) if row["outcome"] else None,
            )
            for row in rows
        ]

    async def run(
        self, *, dry_run: bool, run_seconds: float, pause_seconds: float = 0.0
    ) -> dict[str, int] | None:
        """Read the lists that are due, one release at a time; the run's counts, or None
        when Navidrome stopped it. ``run_seconds``: how often runs come (the pass's
        interval); ``pause_seconds``: after each release written."""
        if not self.enabled:
            return {}
        name = "track lists (dry run)" if dry_run else "track lists"
        todo, more = due(
            await self.entries(), self.clock(), apply=not dry_run, run_seconds=run_seconds
        )
        if not todo:
            return {}
        if not dry_run:
            try:
                why = await self.refresh.engine.refused()
            except NavidromeError as exc:
                log.warning("%s: Navidrome unavailable (%s); trying again later", name, exc)
                return None
            if why:  # nothing is asked for what could not be written
                log.info("%s: not read again now (%s)", name, why)
                return {}
        counts: Counter[str] = Counter()
        left = 0  # differences left as they are (logged; kept by release)
        for index, entry in enumerate(todo):
            await self.fills.wait_idle()  # opened and shown albums first; a rest waited out
            if not self.enabled:  # switched off meanwhile
                break
            try:
                fresh = await self.refresh.read(entry.ref)
            except CatalogError as exc:
                if exc.kind == "not_found":
                    await self._note(entry.ref, "gone", reason="the catalog no longer has it")
                    counts["gone"] += 1
                    continue
                more += len(todo) - index
                if NEEDS_SIGN_IN in exc.reason:  # (its operator's to do: no rest, no record)
                    log.debug("%s: waiting for the catalog's sign-in", name)
                    break
                self.fills.resting_until = self.fills.clock() + self.fills.rest_seconds
                await self._note(entry.ref, "failed", reason=exc.reason)
                log.info("%s: the catalog failed (%s); the rest waits", name, exc.reason)
                break
            try:
                result = await self.refresh.apply(
                    entry.album_id,
                    entry.ref,
                    fresh,
                    narrow=True,  # tracks added, lengths corrected, nothing else
                    write=not dry_run,
                    back_unchanged=False,  # (no searches of Navidrome for nothing)
                )
            except NavidromeError as exc:
                log.warning("%s stopped: Navidrome unavailable (%s)", name, exc)
                return None
            except Exception as exc:  # the next release still gets its turn
                log.warning("%s: a release failed: %s", name, type(exc).__name__)
                await self._note(entry.ref, "failed", reason=type(exc).__name__)
                counts["failed"] += 1
                continue
            outcome = _outcome(result)
            left += max(0, len(result.skipped) - result.failed)
            await self._note(entry.ref, outcome, result)
            counts["read"] += 1
            counts[outcome] += 1
            if result.added or result.updated:
                counts["added"] += result.added
                counts["updated"] += result.updated
                log.info(
                    "%s: %s",
                    name,
                    f"{entry.ref}: would add {result.added} track(s), would correct"
                    f" {result.updated} length(s)"
                    if dry_run
                    else result,
                )
                if not dry_run:
                    await anyio.sleep(pause_seconds)
            elif result.refused:
                log.debug("%s: %s", name, result)
        self.last = dict(counts)
        log.info(
            "%s: %d release(s) read again: %s %d track(s), %s %d length(s), %d difference(s)"
            " left as they are; %d the catalog no longer has, %d not usable, %d not all"
            " written, %d failed; %d more due",
            name,
            counts["read"],
            "would add" if dry_run else "added",
            counts["added"],
            "would correct" if dry_run else "corrected",
            counts["updated"],
            left,
            counts["gone"],
            counts["refused"],
            counts["unwritten"],
            counts["failed"],
            more,
        )
        return self.last

    async def _note(
        self, ref: str, outcome: str, result: Refreshed | None = None, *, reason: str | None = None
    ) -> None:
        """Keep a reading's result - unless its release went meanwhile."""
        added = result.added if result else 0
        updated = result.updated if result else 0
        left_out = len(result.skipped) - result.failed if result else 0
        await self.store.execute(
            "INSERT INTO release_refreshes (ref, checked_at, outcome, added, updated, left_out,"
            " reason) SELECT ?, ?, ?, ?, ?, ?, ?"
            " WHERE EXISTS (SELECT 1 FROM releases WHERE ref = ?)"
            " ON CONFLICT (ref) DO UPDATE SET checked_at = excluded.checked_at,"
            " outcome = excluded.outcome, added = excluded.added, updated = excluded.updated,"
            " left_out = excluded.left_out, reason = excluded.reason",
            [
                ref,
                self.clock(),
                outcome,
                added,
                updated,
                max(0, left_out),
                reason or (result.refused if result else None),
                ref,
            ],
        )


def _outcome(result: Refreshed) -> str:
    if result.refused:
        return "failed" if result.retry else "refused"
    if result.failed:
        return "unwritten"
    if result.added or result.updated:
        return "pending" if result.planned else "changed"
    return "same"
