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

Two songs a client asks for *together* (their requests came within ``TOGETHER_SECONDS`` of
each other, 0.05 s - such a client's fetches ahead come 0.1-0.3 s after its current song)
cannot be told apart by when they came: a player that keeps two audio elements loads the
song it plays and the next one at the same moment, and which request is taken first is a
matter of chance. Unless that client's own report or saved queue names the other one, both
are routed as plays, at once - never the song being played behind the other one's first
byte (its report may come only seconds later). At most two: each start is classified once,
when it is noted (``note``), against the client's starts around it, whatever order they
are noted in - a start beside two other songs, or beside a fetch ahead, is a fetch ahead
(requests for one song count once). A start waits, at most the window, for the client's
requests that came before it and are not noted yet, so that the order they came in
decides, not which got through its credential check first. Reports
and saved queues are the account's; only the same client's tell the two apart (another
device's report of one of them says nothing about this client's pair).

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
from dataclasses import dataclass, field

import anyio

from shijhon.delivery.listening import Listener, Listening

POLL = 0.05  # seconds between looks at the listening state while a fetch ahead waits
# Two plays a client started this close together are asked for together: neither is told
# a fetch ahead of the other by when it came.
TOGETHER_SECONDS = 0.05


@dataclass(eq=False)
class _Waiting:
    """A fetch ahead waiting for its turn."""

    key: str
    since: float


@dataclass(eq=False)
class _Start:
    """A client's start at byte zero: its song, when its request came, and whether it was
    taken as a play (else a fetch ahead)."""

    key: str
    at: float
    play: bool


MAX_STARTS = 64  # starts kept per client (only those within the window matter)


@dataclass(eq=False)
class Arrival:
    """A request that may start a song, not noted yet."""

    at: float
    done: anyio.Event = field(default_factory=anyio.Event)


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
        # Each client's plays being opened (until their first byte): fetches ahead wait for
        # all of them.
        self._opening: dict[Listener, int] = {}
        self._starts: dict[Listener, list[_Start]] = {}
        # Requests that may start a song, by the user they name and their client, not noted
        # yet (their credentials being checked, their song looked up).
        self._arriving: dict[Listener, list[Arrival]] = {}
        self._turns: dict[Listener, anyio.Lock] = {}
        self._waiting: dict[Listener, list[_Waiting]] = {}  # in the order they came

    def current(self, who: Listener, key: str, since: float) -> bool:
        """Whether a play started at ``since`` may be the client's current song, as far as
        Shijhon can tell: a report or saved queue of the account says so, or ``note`` took
        it as a play."""
        if self.window <= 0 or self.listening.current_for(who[0]) == key:
            return True
        return self.note(who, key, since)

    def note(self, who: Listener, key: str, at: float) -> bool:
        """A client's start of a song at byte zero (its request came at ``at``), classified
        once: a play when the client's own report or saved queue names it, when no other
        start of the client is within the window around it, or when the only one is a play
        whose request came together with this one and the client does not name it; else a
        fetch ahead. The same start noted again keeps its class."""
        if self.window <= 0:
            return True
        starts = self._starts.setdefault(who, [])
        for start in starts:
            if start.key == key and start.at == at:
                return start.play
        if len(self._starts) > 1024:  # forget clients that went quiet
            recent = at - 60.0
            self._starts = {
                w: kept for w, kept in self._starts.items() if any(s.at >= recent for s in kept)
            }
            starts = self._starts.setdefault(who, [])
        said = self.listening.current_for_client(who)
        near = [s for s in starts if s.key != key and abs(s.at - at) <= self.window]
        # By song: requests for one song (a probe and its play, a retry) are one start.
        songs = {s.key for s in near}
        if said == key or not songs:
            play = True
        elif len(songs) > 1 or said in songs:
            play = False
        else:
            play = any(s.play and abs(s.at - at) <= TOGETHER_SECONDS for s in near)
        starts.append(_Start(key, at, play))
        starts[:] = [s for s in starts if s.at >= at - 60.0][-MAX_STARTS:]
        return play

    def arriving(self, claimed: Listener, at: float) -> Arrival | None:
        """A client's request that may start a song at byte zero came at ``at``: until it
        is noted (or turns out to be no start: ``arrived``), later starts of the client wait
        for it (``settle``). ``claimed``: the user it names (not verified yet) and its
        client."""
        if self.window <= 0:
            return None
        entry = Arrival(at)
        self._arriving.setdefault(claimed, []).append(entry)
        return entry

    def arrived(self, claimed: Listener, entry: Arrival | None) -> None:
        if entry is None or entry.done.is_set():
            return
        entry.done.set()
        waiting = self._arriving.get(claimed, [])
        if entry in waiting:
            waiting.remove(entry)
        if not waiting:
            self._arriving.pop(claimed, None)

    async def settle(self, claimed: Listener, at: float, mine: Arrival | None = None) -> None:
        """Until the client's requests that came before ``at`` (within the window) are noted
        or gone - a credential check or a placeholder's lookup can take one longer than a
        later one -, at most the window: starts are classified in the order they came."""
        if self.window <= 0:
            return
        with anyio.move_on_after(self.window):
            while True:
                earlier = [
                    e
                    for e in self._arriving.get(claimed, [])
                    if e is not mine and e.at < at and at - e.at <= self.window
                ]
                if not earlier:
                    return
                await earlier[0].done.wait()

    @asynccontextmanager
    async def playing(self, who: Listener) -> AsyncIterator[None]:
        """Around a current play's routing: fetches ahead wait until every play of the
        client being opened has its first byte."""
        self._opening[who] = self._opening.get(who, 0) + 1
        try:
            yield
        finally:
            left = self._opening.get(who, 1) - 1
            if left > 0:
                self._opening[who] = left
            else:
                self._opening.pop(who, None)
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
                played = not self._opening.get(who) or waited >= self.current_wait
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
