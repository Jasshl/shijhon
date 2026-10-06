"""Per-user limits on add-on work.

A bulk reader - an offline sync of many catalog songs, an analyzer walking the library,
a client fetching far ahead - must not turn into a flood of add-on requests:

- **routings**: songs whose audio one user's requests are looking up at the add-ons at the
  same time (a play's resolution up to its first byte); more wait for a turn, within the
  client's wait cap (a client that left, or whose wait ran out, gets none) - except the
  song being played, which never waits behind the client's fetches ahead of it: plays have
  turns of their own (twice as many, so that quick skipping stays bounded too), and a
  lookup waiting for a turn that the client then plays takes one of those (promoted);
- **downloads**: download-first fetches of whole files (``download`` requests; streams in
  another format, at a lower bitrate or from an offset; transcode decisions, the jukebox,
  share links - each share its own user) running at once per user, the rest waiting their
  turn - except the song being played, which never waits behind them, and a waiting fetch
  the client then plays (promoted: it goes at once). Plain streams take nothing from these
  limits, also a client saving songs for offline use by streaming them;
- **downloads an hour**: one user's allowance (120: one every 30 s) - a few start at once
  after a quiet while (``downloads_burst``, 4: a first offline sync does not start with the
  whole hour's; 0: the whole hour's at once), then one every hour / allowance; past it
  a download waits its turn, in order, so a large offline sync slows down instead of
  failing. The song being played never waits for it (it takes from the allowance all the
  same).

Settings of 0 turn a limit off. Users are Navidrome's (the credentials checked first).
"""

from __future__ import annotations

import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio

HOUR = 3600.0


class Limited(Exception):
    """No turn came in time; ``reason`` says so."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _User:
    routings: anyio.Semaphore | None
    plays: anyio.Semaphore | None  # the songs being played: turns of their own
    downloads: anyio.Semaphore | None
    allowance: float  # downloads the user may start now (refilled over the hour)
    filled_at: float
    pace: anyio.Lock = field(default_factory=anyio.Lock)  # downloads waiting, in order
    refilled: anyio.Event = field(default_factory=anyio.Event)  # allowance given back
    users: int = 0  # requests holding or waiting for this record


class UserLimits:
    def __init__(
        self,
        *,
        routings: int = 4,
        downloads: int = 2,
        downloads_per_hour: int = 120,
        downloads_burst: int = 0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self.routings = routings
        self.downloads = downloads
        self.per_hour = downloads_per_hour
        self.burst = downloads_burst  # of the hour's allowance, at once (0: all of it)
        self.hour = HOUR  # the allowance's period (shorter in tests)
        self.clock = clock
        self.sleep = sleep
        self._users: dict[str, _User] = {}
        # Lookups waiting for a turn, by user and song: told when the client plays the song.
        self._awaited: dict[tuple[str, str], set[anyio.Event]] = {}

    @property
    def most(self) -> float:
        """The most of the allowance a user can have saved up: what may start at once."""
        return float(min(self.burst, self.per_hour) if self.burst > 0 else self.per_hour)

    def promote(self, user: str, key: str) -> None:
        """The user's client plays the song ``key`` now (its report, its saved queue): a
        lookup of it waiting for a turn goes on as the song being played."""
        for event in self._awaited.get((user, key), ()):
            event.set()

    @asynccontextmanager
    async def _user(self, user: str) -> AsyncIterator[_User]:
        record = self._users.get(user)
        if record is None:
            record = self._users[user] = _User(
                # (With their most: a turn given back twice is an error, not a wider limit.)
                _turns(self.routings),
                _turns(2 * self.routings),
                _turns(self.downloads),
                self.most,
                self.clock(),
            )
        record.users += 1
        try:
            yield record
        finally:
            record.users -= 1
            if record.users <= 0 and self._refill(record) >= self.most:
                self._users.pop(user, None)  # nothing to remember

    @asynccontextmanager
    async def routing(
        self, user: str, *, wait: float | None = None, queue: bool = True, key: str | None = None
    ) -> AsyncIterator[float]:
        """One of the user's add-on routings; yields how long it waited for its turn
        (``queue`` False: the song being played, which takes one of the plays' turns and
        never waits behind the client's fetches ahead). ``key``: the song looked up - while
        it waits for a turn, the client playing it (``promote``) takes it out of that wait
        to one of the plays' turns. Raises ``Limited`` when no turn came within
        ``wait`` seconds."""
        async with self._user(user) as record:
            started = self.clock()
            turns = record.routings if queue else record.plays
            if turns is None or record.plays is None:
                yield 0.0
                return
            # A lookup of a song (``key``) waits so that the client playing it can take it
            # out of the wait; every wait is within ``wait``, in the order the requests came.
            held: anyio.Semaphore | None = None
            promoted = anyio.Event() if queue and key is not None else None
            if promoted is not None and key is not None:
                self._awaited.setdefault((user, key), set()).add(promoted)
            try:
                with anyio.move_on_after(max(0.0, wait) if wait is not None else None):
                    if await _unless(promoted, turns.acquire, undo=turns.release):
                        held = turns
                    else:  # the client plays it now: a play's turn, not behind its fetches
                        await record.plays.acquire()
                        held = record.plays
            finally:
                if promoted is not None and key is not None:
                    awaited = self._awaited.get((user, key), set())
                    awaited.discard(promoted)
                    if not awaited:
                        self._awaited.pop((user, key), None)
            if held is None:
                raise Limited(f"no turn within {wait:g}s among the user's other lookups")
            try:
                yield self.clock() - started
            finally:
                held.release()

    @asynccontextmanager
    async def download(
        self, user: str, *, queue: bool = True, promoted: anyio.Event | None = None
    ) -> AsyncIterator[Callable[[], None]]:
        """One of the user's download-first fetches: waits for a turn among the user's
        fetches, then for the hour's allowance, in order. ``queue`` False - the song being
        played - waits for neither; nor does a fetch once ``promoted`` is set (the client
        plays it now). Yields a callable that gives the allowance back (the fetch turned out
        not to need the add-ons)."""
        async with self._user(user) as record:
            waiting = queue and not (promoted is not None and promoted.is_set())
            slot = False
            if waiting and (downloads := record.downloads) is not None:
                slot = await _unless(promoted, downloads.acquire, undo=downloads.release)
            try:
                # (A cancellation that comes once the allowance is taken, before the turn
                # is had, gives it back: nothing was fetched for it.)
                took = waiting and await _unless(
                    promoted, lambda: self._take(record), undo=lambda: self._give_back(record)
                )
                if not took:
                    self._take_now(record)
                returned = False

                def give_back() -> None:
                    nonlocal returned
                    if not returned:
                        returned = True
                        self._give_back(record)

                yield give_back
            finally:
                if slot and record.downloads is not None:
                    record.downloads.release()

    def _give_back(self, record: _User) -> None:
        """One download back to the allowance; a download waiting for it goes now."""
        if self.per_hour > 0:
            self._refill(record)
            record.allowance = min(self.most, record.allowance + 1)
            record.refilled.set()
            record.refilled = anyio.Event()

    def _refill(self, record: _User) -> float:
        if self.per_hour <= 0:
            return 0.0
        now = self.clock()
        rate = self.per_hour / self.hour
        record.allowance = min(self.most, record.allowance + (now - record.filled_at) * rate)
        record.filled_at = now
        return record.allowance

    async def _take(self, record: _User) -> None:
        """One download from the allowance, waiting in order for it to refill."""
        if self.per_hour <= 0:
            return
        async with record.pace:
            while self._refill(record) < 1:
                pause = (1 - record.allowance) * self.hour / self.per_hour
                await _unless(record.refilled, functools.partial(self.sleep, pause))
            record.allowance -= 1

    def _take_now(self, record: _User) -> None:
        """One download from the allowance without waiting (the song being played): the
        fetches that wait then wait longer (at most an hour's worth of debt)."""
        if self.per_hour > 0:
            self._refill(record)
            record.allowance = max(-float(self.per_hour), record.allowance - 1)


def _turns(count: int) -> anyio.Semaphore | None:
    return anyio.Semaphore(count, max_value=count) if count > 0 else None


async def _unless(
    event: anyio.Event | None,
    wait: Callable[[], Awaitable[None]],
    *,
    undo: Callable[[], None] | None = None,
) -> bool:
    """Wait for ``wait`` (True) unless ``event`` is set first (False: nothing taken). A
    cancellation after ``wait`` took something gives it back (``undo``)."""
    if event is None:
        await wait()
        return True
    done = False
    try:
        async with anyio.create_task_group() as tg:

            async def run() -> None:
                nonlocal done
                await wait()
                done = True
                tg.cancel_scope.cancel()

            async def watch() -> None:
                await event.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(run)
            tg.start_soon(watch)
    except BaseException:
        if done and undo is not None:
            undo()
        raise
    return done
