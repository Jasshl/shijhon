"""The paced pass over the whole library.

It goes through every album of the library, most recently added first, and matches the
ones without a match in the current catalog and region - one album at a time, only
when no opened or shown album is waiting (those come first) and not while the catalog
rests after failing, with its catalog requests at a pace of their own (about one a
second: three to four requests an album, once), and a pause after each fill it
makes (a plan followed asks the catalog nothing, but writes files and scans). Then it
looks again every few hours, for new albums only (and failures whose wait is over). When
Navidrome cannot be asked, the run stops without holding it against the albums and is
tried again a few minutes later.

Every album is looked at: those the fill policy allows (the owner has enough of them)
are filled, the others matched and kept with their plan (``deferred``), so that a view shows
them complete at once, even among many, and their first use fills them.

Its mode is a setting: a **dry run** (the default) records what it would do - fill, complete,
review, no match - as plans, and writes nothing; ``shijhon matches`` lists them. Nothing
else fills automatically meanwhile (views, searches and syncs only match): only first use
fills (``Fills.auto_fill``). When the
pass is switched on, it follows those plans without asking the catalog again (unless the
owned album changed) and matches the rest. ``off`` leaves the library to opened and shown
albums.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Literal

import anyio

from shijhon.fill.fills import Decision, Fills
from shijhon.matching.matcher import Matcher
from shijhon.navidrome.client import NavidromeError, NavidromeService

log = logging.getLogger(__name__)
PROGRESS_EVERY = 100  # albums between progress lines
RETRY_SECONDS = 300.0  # after a run Navidrome stopped


class LibraryPass:
    def __init__(
        self,
        fills: Fills,
        navidrome: NavidromeService,
        matcher: Matcher,
        *,
        mode: Literal["off", "dry_run", "on"] = "dry_run",
        start_seconds: float = 60.0,
        interval_seconds: float = 6 * 3600.0,
        pause_seconds: float = 5.0,
    ) -> None:
        self.fills = fills
        self.navidrome = navidrome
        self.matcher = matcher  # on a paced catalog
        self.mode = mode  # also whether anything else fills automatically (``Fills.auto_fill``)
        self.start_seconds = start_seconds
        self.interval_seconds = interval_seconds
        self.pause = pause_seconds  # after each fill made
        self.progress: tuple[int, int] = (0, 0)  # albums done, albums to look at
        self.running = False  # a run is under way (its progress above)
        self.last: dict[str, int] = {}  # the outcomes of the last run

    @property
    def mode(self) -> Literal["off", "dry_run", "on"]:
        return self._mode

    @mode.setter
    def mode(self, mode: Literal["off", "dry_run", "on"]) -> None:
        """A dry run fills nothing automatically anywhere: only first use fills."""
        self._mode = mode
        self.fills.auto_fill = mode != "dry_run"

    async def run(self) -> None:
        """For the application's lifetime: a pass after the start delay, then one every
        interval."""
        if self.mode == "off":
            return
        await anyio.sleep(self.start_seconds)
        while True:
            try:
                done = await self.run_once()
            except Exception as exc:  # the next pass tries again
                log.warning("library pass failed: %s", type(exc).__name__)
                done = None
            await anyio.sleep(self.interval_seconds if done is not None else RETRY_SECONDS)

    async def run_once(self) -> dict[str, int] | None:
        """One pass over the library; the number of albums of each outcome, or None when
        Navidrome stopped it. ``running`` says so meanwhile."""
        self.running = True
        try:
            return await self._run_once()
        finally:
            self.running = False

    async def _run_once(self) -> dict[str, int] | None:
        dry_run = self.mode == "dry_run"
        name = "library pass (dry run)" if dry_run else "library pass"
        self.progress = (0, 0)  # this run's, not the last one's
        await self.fills.wait_idle()  # e.g. a rest of the catalog
        try:
            albums = await self.navidrome.album_counts()
            todo = [a for a, songs, _ in albums if await self.fills.due(a, dry_run=dry_run,
                                                                        songs=songs)]  # fmt: skip
        except NavidromeError as exc:
            log.warning("%s: Navidrome unavailable (%s); trying again later", name, exc)
            return None
        if not todo:  # one line a run: the pass is alive
            log.info(
                "%s: nothing to do (%d albums, all checked)%s",
                name,
                len(albums),
                await self._earlier(dry_run, {a for a, _, _ in albums}),
            )
            return {}
        log.info("%s: %d of %d albums to look at", name, len(todo), len(albums))
        self.progress = (0, len(todo))
        counts: Counter[str] = Counter()
        added = 0
        for index, album_id in enumerate(todo, 1):
            try:
                decision = await self._one(album_id, dry_run)
            except NavidromeError as exc:
                log.warning("%s stopped: Navidrome unavailable (%s)", name, exc)
                return None
            except Exception as exc:  # the next album still gets its turn
                log.warning("%s: an album failed: %s", name, type(exc).__name__)
                counts["failed"] += 1
                continue
            outcome = decision.outcome if decision is not None else "skipped"
            counts[outcome] += 1
            if decision is not None and decision.outcome == "filled":
                added += decision.added
                if not dry_run:
                    await anyio.sleep(self.pause)
            self.progress = (index, len(todo))
            if index % PROGRESS_EVERY == 0 and index < len(todo):
                log.info("%s: %d of %d albums looked at", name, index, len(todo))
        self.last = dict(counts)
        filled = "would fill" if dry_run else "filled"
        log.info(
            "%s done: %s %d album(s) with %d placeholder(s)%s; %d complete, %d for review,"
            " %d without a match, %d to fill on first use, %d failed",
            name,
            filled,
            counts["filled"],
            added,
            await self._earlier(dry_run, {a for a, _, _ in albums}),
            counts["complete"],
            counts["review"],
            counts["none"],
            counts["deferred"],
            counts["failed"],
        )
        return self.last

    async def _earlier(self, dry_run: bool, albums: set[str]) -> str:
        """For a dry run's lines: what the pass switched on would fill in all - this run's
        plans and those of the albums matched earlier (a dry run does not look at them
        again) - as the fill policy decides now."""
        if not dry_run:
            return ""
        count = await self.fills.would_fill(albums)
        if not count:
            return ""
        return f"; switched on, it would fill {count} album(s) in all (earlier matches included)"

    async def _one(self, album_id: str, dry_run: bool) -> Decision | None:
        """One album, once no opened or shown album waits and the catalog does not rest
        (a rest that began just before the album's turn is waited for too)."""
        while True:
            await self.fills.wait_idle()
            decision = await self.fills.fill(
                album_id, matcher=self.matcher, dry_run=dry_run, navidrome_errors=False, auto=True
            )
            if decision is not None or not self.fills.resting:
                return decision
