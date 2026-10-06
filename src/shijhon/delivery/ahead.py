"""A client's fetches ahead of the song it plays.

Some clients fetch the next songs of their queue the moment a song starts: the current
song and the next four, within a quarter of a second. Each first fetch of a catalog song
is a full routing - a resolution at the primary add-on and availability checks at the
others - so four plays cost eighteen resolutions and run into a rate limit.

A play at byte zero is *ahead* when the same client started another song a moment before
(within a window, 0.5 s: such fetches come 0.1-0.3 s after the current one) and neither its
"now playing" report nor its saved queue says this song is the current one. A fetch ahead
first waits a moment (0.5 s) for the client's report, so a listener skipping quickly is
routed as a play. Fetches ahead go to the primary add-on alone, without the
availability checks, one at a time per client, and only after the song being played has
its first byte; one the primary cannot serve (it does not have it, fails, or cools down) is
routed as usual - availability checks and fallbacks - still in the client's turn. A fetch
ahead waiting its turn that a report (or a saved queue) names as current goes at once,
routed as a play. Other plays are routed as before.

The turns go in the order of playing: the song that follows the current one in the
client's saved queue first, when that is known, else in the order the fetches came - so
that the next song is the one prepared, also while the primary is down and each routing
takes its time (the later ones may then wait out their turn; they come again, nearer the
front, when the client moves on).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio

from shijhon.delivery.listening import Listener, Listening

POLL = 0.05  # seconds between looks at the listening state while a fetch ahead waits


@dataclass(eq=False)
class _Waiting:
    """A fetch ahead waiting for its turn."""

    key: str
    since: float


@dataclass(frozen=True)
class Turn:
    promoted: bool  # the client reports it as playing now: route it as a play
    alone: bool  # it waited out its turn: the primary alone, no fallbacks
    waited: float  # seconds spent waiting (counted in the client's wait cap)


class AheadGate:
    def __init__(
        self,
        listening: Listening,
        *,
        window_seconds: float = 0.5,
        report_seconds: float = 0.5,
        current_seconds: float = 3.0,
        wait_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.listening = listening
        self.window = window_seconds
        self.report = report_seconds  # how long a fetch ahead waits for a report first
        # How long it waits for the song being played (a quick skip leaves one behind).
        self.current_wait = current_seconds
        self.wait = wait_seconds  # the longest a fetch ahead waits for its turn
        self.clock = clock
        self._playing: dict[Listener, anyio.Event] = {}  # set once the play has its first byte
        self._turns: dict[Listener, anyio.Lock] = {}
        self._waiting: dict[Listener, list[_Waiting]] = {}  # in the order they came

    def current(self, who: Listener, key: str, since: float) -> bool:
        """Whether a play started at ``since`` is the client's current song, as far as Shijhon
        can tell: its report or saved queue says so, or it started no other song a moment
        before."""
        if self.window <= 0 or self.listening.current_for(who[0]) == key:
            return True
        return not self.listening.fetched_between(who, since - self.window, since, {key})

    @asynccontextmanager
    async def playing(self, who: Listener) -> AsyncIterator[None]:
        """Around a current play's routing: fetches ahead wait until it has its first byte."""
        event = self._playing[who] = anyio.Event()
        try:
            yield
        finally:
            event.set()
            if self._playing.get(who) is event:
                del self._playing[who]
            self._forget()

    @asynccontextmanager
    async def turn(self, who: Listener, key: str) -> AsyncIterator[Turn]:
        """A fetch ahead's turn: after the client's current play has its first byte (or a
        few seconds), one at a time - the next song of the client's saved queue first, else
        in the order they came -, after a moment for the client's report. The turn says
        whether the song became the current one meanwhile (route it as a play; a report is
        heeded while it waits for the lock too), and whether it waited out ``wait`` without
        getting the lock (then it goes alone: the primary only, no fallbacks)."""
        started = self.clock()
        lock = self._turns.setdefault(who, anyio.Lock())
        acquired = False
        mine = _Waiting(key, started)
        waiting = self._waiting.setdefault(who, [])
        waiting.append(mine)
        try:
            while self.clock() - started < self.wait:
                if self.listening.current_for(who[0]) == key:
                    break
                waited = self.clock() - started
                playing = self._playing.get(who)
                played = playing is None or playing.is_set() or waited >= self.current_wait
                if played and waited >= self.report and self._first(who[0], waiting) is mine:
                    try:
                        lock.acquire_nowait()
                    except anyio.WouldBlock:
                        pass
                    else:
                        acquired = True
                        break
                await anyio.sleep(POLL)
        finally:
            waiting.remove(mine)
            if not waiting and self._waiting.get(who) is waiting:
                del self._waiting[who]
        promoted = self.listening.current_for(who[0]) == key
        try:
            yield Turn(promoted, not promoted and not acquired, self.clock() - started)
        finally:
            if acquired:
                lock.release()

    def _first(self, user: str, waiting: list[_Waiting]) -> _Waiting:
        """Whose turn is next among a client's waiting fetches ahead: the song after the
        current one in the client's saved queue, when it waits; else the one that came
        first."""
        current = self.listening.current_for(user)
        upcoming = self.listening.next_in_queue(user, current, 1) if current is not None else None
        if upcoming:
            for fetch in waiting:
                if fetch.key == upcoming[0]:
                    return fetch
        return waiting[0]

    def _forget(self) -> None:
        if len(self._turns) > 1024:
            self._turns = {w: lock for w, lock in self._turns.items() if lock.locked()}
