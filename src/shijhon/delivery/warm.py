"""Warm-ahead: when a song starts playing from the add-ons, open the next songs in the
background, so they start quickly and slow sources, and those that prepare a song before
they can deliver it, get time to prepare them. It uses the same routing as playback. The
depth is a setting; zero turns it off.

Which songs: the next ones in the client's saved play queue when Shijhon knows it,
else the next ones of the album. After a short delay - so the client's own requests and
reports around a play's start are in - warm-ahead runs only for the song being played: a
song fetched while the client reports another one playing (reported around this play, or
earlier and still within its length) gets none; without reports, every play gets it. A
client that fetches upcoming songs itself - it started this play's next songs, or two or
more other songs, within a moment of the play - gets none at all for a while.
The aim is add-on requests close to the songs actually played plus the configured depth.

Warm-ahead is Shijhon's own background work: of all the listeners together only a few jobs
run at once (a setting, 2) - the others wait their turn, and one whose turn does not
come within a while is dropped (its song has moved on) - and its add-on requests come after
every request a listener waits for (``pacing``).
"""

from __future__ import annotations

import logging
from typing import Protocol

import anyio

from shijhon.delivery.listening import Listener, Listening
from shijhon.delivery.playback import Deliverer, Track

log = logging.getLogger(__name__)

# Other songs fetched this long before, or after, a play's own request are "around it".
_BEFORE = 0.5
_AFTER = 1.0
WAIT_SECONDS = 30.0  # the longest a warm-ahead job waits for its turn among the jobs


class Upcoming(Protocol):
    """Songs as delivery needs them, whether in the library yet or not."""

    async def track(self, key: str) -> Track | None:
        """The song for a queue entry's key; None when no add-on is needed for it."""
        ...

    async def after(self, track: Track, depth: int) -> list[Track]:
        """The next songs of ``track``'s album that need add-ons."""
        ...


class WarmAhead:
    def __init__(
        self,
        deliverer: Deliverer,
        listening: Listening,
        upcoming: Upcoming,
        *,
        delay_seconds: float = 2.0,
        jobs: int = 2,
    ) -> None:
        self.deliverer = deliverer
        self.listening = listening
        self.upcoming = upcoming
        self.delay = delay_seconds
        self.patience = WAIT_SECONDS
        # Whose warm-ahead of which song is running or waiting its turn, and when that
        # listener last started the song (a start that comes while its job waits).
        self._busy: dict[tuple[Listener | None, str], float] = {}
        # Jobs at the add-ons at once, for all listeners together.
        self._turns = anyio.CapacityLimiter(max(1, jobs))
        self.warmed = 0  # observable in tests

    @property
    def jobs(self) -> int:
        """How many warm-ahead jobs run at once (a setting; changed at once)."""
        return int(self._turns.total_tokens)

    @jobs.setter
    def jobs(self, value: int) -> None:
        self._turns.total_tokens = max(1, int(value))

    def started(self, who: Listener | None, track: Track, at: float) -> None:
        """A play of ``track`` (requested at ``at``) has its first byte."""
        depth = self.deliverer.settings.warm_ahead_depth
        job = (who, track.key)
        if depth <= 0:
            return
        if job in self._busy:  # its job waits or runs: it is for this start too
            self._busy[job] = max(self._busy[job], at)
            return
        if who is not None and self.listening.prefetching(who):
            return

        async def warm() -> None:
            if job in self._busy:
                self._busy[job] = max(self._busy[job], at)
                return
            self._busy[job] = at
            try:
                await anyio.sleep(self.delay)
                mine = object()
                with anyio.move_on_after(self.patience) as waiting:
                    await self._turns.acquire_on_behalf_of(mine)
                if waiting.cancelled_caught:
                    log.info(
                        "warm-ahead: no turn among the jobs within %gs; dropped", self.patience
                    )
                    return
                try:
                    began = self._busy[job]  # the listener's latest start of the song
                    if who is not None and self._moved_on(who, track, began):
                        return  # the listener started another song meanwhile: its job, then
                    # (Asked after the wait: what the client plays now decides.)
                    for item in await self._following(who, track, began, depth):
                        # One after the other: a preparing source may have a single slot.
                        if await self.deliverer.prewarm(item):
                            self.warmed += 1
                finally:
                    self._turns.release_on_behalf_of(mine)
            finally:
                self._busy.pop(job, None)

        self.deliverer.background(warm)

    def _moved_on(self, who: Listener, track: Track, at: float) -> bool:
        """The listener's client started another song since this play's start (past the
        moment around it, which ``_following`` looks at): a job that waited for its turn
        meanwhile is for a song skipped past."""
        now = self.listening.clock()
        return bool(self.listening.fetched_between(who, at + _AFTER, now, {track.key}))

    async def _following(
        self, who: Listener | None, track: Track, at: float, depth: int
    ) -> list[Track]:
        if who is None:
            return await self.upcoming.after(track, depth)
        user, client = who
        if self.listening.prefetching(who) or not await self._playing(user, track, at):
            return []
        keys = self.listening.next_in_queue(user, track.key, depth)
        following = await self.upcoming.after(track, depth) if keys is None else None
        upcoming = set(keys) if keys is not None else {t.key for t in following or ()}
        # The client fetched this play's next songs itself, or several other songs, around
        # its start (one other song may be a change of mind).
        others = self.listening.fetched_between(who, at - _BEFORE, at + _AFTER, {track.key})
        if others & upcoming or len(others) >= 2:
            if self.listening.mark_prefetching(who):
                log.info(
                    "warm-ahead: %s fetches upcoming songs itself; none from Shijhon for %gs",
                    client or "a client",
                    self.listening.memory,
                )
            return []
        if following is not None:
            return following
        found = [await self.upcoming.track(key) for key in keys or ()]
        return [t for t in found if t is not None]

    async def _playing(self, user: str, track: Track, at: float) -> bool:
        """Whether ``track`` is what the user's client plays, as far as its reports tell:
        not when another song was reported around this play (skipped past, or fetched
        ahead), nor while another reported song is still within its length (fetched ahead
        by a gapless client)."""
        latest = self.listening.latest(user)
        if latest is None or latest[0] == track.key:
            return True
        key, reported = latest
        if reported >= at - _BEFORE:
            return False
        other = await self.upcoming.track(key)
        length = other.duration_ms / 1000 if other is not None else 0.0
        return self.listening.clock() - reported >= length
