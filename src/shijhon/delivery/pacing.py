"""What one installation sends to one add-on, in all.

An add-on is somebody's service: whatever the clients, the users and Shijhon's own
background work ask for together, one add-on gets at most

- so many **requests a second** (a token bucket: a few at once, then a steady rate) - every
  API request Shijhon sends it: its manifest, lookups, link requests, availability checks
  and the dashboard's manifest checks;
- so many **audio openings at once** - requests to the audio address it handed over, from
  the request until its first bytes of audio come (the audio then streams as the client
  reads it). The cap holds back everything but the song being played: a play's or a seek's
  audio is asked for at once, and counts.

A redirect is a request too: every hop of an API request takes its turn at the limit of the
origin it goes to (``get``).

The limits belong to the add-on's **origin** (scheme, host and port): two configured entries
at one origin share them, and the stricter entry's settings apply.

**The song being played goes first**. Requests waiting for a turn go by their urgency
- the song being played, then a client's other requests (fetches ahead, probes, queued
downloads), then Shijhon's own background work (warm-ahead, preparation), then the
dashboard's checks - and within one urgency in the order they came. An idle add-on is asked
at once: a single play waits for nothing. A request already waiting becomes as urgent as
the most urgent request for the same song (``Urgency.raise_to``): a play never waits behind
its own warm-ahead.

**An add-on that says "too many requests" is left alone.** After an HTTP 429 no API
request is sent to its origin until the time it named in ``Retry-After`` (seconds or an
HTTP date; an hour at most), or for the cooldown when it named none: the block is looked
at before every request, also those already waiting, and a later answer can only make it
longer (``AddonPace.block``, ``Blocked``). A 429 on an audio request stops its audio
requests too; one on an API request does not - the songs already playing from it go on
(their audio is what a listener hears, and usually comes from another address).

A wait here is part of the request's own time budget - whoever waits is canceled by the
budget it already has (``Waits`` says that it was waiting here, or had waited here for more
than a moment, so the routing can say why and does not count it as the add-on's failure).

This is custom because the mature rate limiters for asyncio (aiolimiter, limits) have no
order by urgency and no hook for a waiter whose urgency changes; it is small: a token
bucket, a counter, and one queue discipline for both.
"""

from __future__ import annotations

import calendar
import email.utils
import itertools
import math
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import anyio
import httpx

# Urgencies: the lower, the sooner.
PLAY = 0  # the song being played: its lookups, its link, its audio, its seeks
QUEUED = 1  # a client's other requests: fetches ahead, probes, queued downloads
WARM = 2  # Shijhon's own background work: warm-ahead, preparation requests
CHECK = 3  # the dashboard's manifest checks

REQUESTS = "waiting for the add-on's request limit"
OPENINGS = "waiting for the add-on's audio limit"
BLOCKED = "not asked: the add-on said to wait (rate limited)"

MAX_RETRY_AFTER = 3600.0  # the longest an add-on's Retry-After is taken for (seconds)
MIN_RETRY_AFTER = 1.0  # ... and the shortest a rate limit keeps Shijhon away
COOLDOWN = 30.0  # after a rate limit that names no time ([delivery] cooldown_seconds)
_SECONDS = re.compile(r"[0-9]+(\.[0-9]+)?")
_TWO_DIGIT_YEAR = re.compile(r"\d-[A-Za-z]{3}-(\d\d)\s")  # an RFC 850 date's

Origin = tuple[str, str, int]


def origin(url: str | None) -> Origin | None:
    """Where an address sends what goes with it: its scheme, host and port (the scheme's
    default filled in). None for no address, or one that cannot be read."""
    if not url:
        return None
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, ValueError, TypeError):
        return None
    scheme = parsed.scheme.lower()
    if not scheme or not parsed.host:
        return None
    port = parsed.port or {"http": 80, "https": 443}.get(scheme, 0)
    return (scheme, parsed.host.lower(), port)


def retry_after(header: str | None, *, now: float | None = None) -> float | None:
    """The seconds an HTTP ``Retry-After`` asks for (RFC 9110, 10.2.3): a number of
    seconds - digits, however many; a decimal fraction is taken too (add-ons differ) - or
    an HTTP date in any of its three forms (the seconds until then; one that has passed:
    0). At most ``MAX_RETRY_AFTER``. None for no header, or one that is neither (a negative
    number, words, a date that cannot be read)."""
    text = (header or "").strip()
    if not text:
        return None
    if _SECONDS.fullmatch(text):
        whole = text.partition(".")[0].lstrip("0")
        if len(whole) > 6:  # (more than an hour whatever the digits: not turned into one)
            return MAX_RETRY_AFTER
        return min(float(text), MAX_RETRY_AFTER)
    current = time.time() if now is None else now
    try:
        parsed = email.utils.parsedate_tz(text)
        if parsed is None:
            return None
        year, rest, zone = parsed[0], parsed[1:6], parsed[9] or 0  # (no zone: GMT)
        if (short := _TWO_DIGIT_YEAR.search(text)) is not None:
            # A two-digit year (RFC 9110, 5.6.7): of this century - unless that moment is
            # more than 50 years ahead, then the latest year in the past with those digits.
            this_year = time.gmtime(current).tm_year
            year = this_year - this_year % 100 + int(short[1])
            ahead = time.gmtime(current)
            if calendar.timegm((year, *rest)) - zone > calendar.timegm(
                (ahead.tm_year + 50, *ahead[1:6])
            ):
                year -= 100
        when = calendar.timegm((year, *rest)) - zone
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return min(max(0.0, when - current), MAX_RETRY_AFTER)


class Blocked(Exception):
    """Nothing is sent to the add-on now: it answered "too many requests", and the time it
    named (or the cooldown) has not passed. ``seconds``: how long still."""

    def __init__(self, seconds: float) -> None:
        super().__init__(BLOCKED)
        self.seconds = seconds


class Urgency:
    """How urgent a request's add-on work is. One object for all the add-on requests of one
    client request (its lookups, its checks, its audio), so that it can be raised while
    they wait."""

    def __init__(self, level: int = PLAY) -> None:
        self.level = level
        self._lines: list[_Line] = []  # where requests of it wait now (one entry each)
        # Urgencies that take this one's level into theirs (a DASH join's, while this
        # request waits for it): told when it changes.
        self.followers: list[Urgency] = []

    def raise_to(self, level: int) -> None:
        """At least as urgent as ``level`` from now on - also for its requests waiting."""
        if level < self.level:
            self.level = level
            self.changed()

    def changed(self) -> None:
        """Its level may have changed: where its requests wait - and those of the
        urgencies following it -, the first in line looks again."""
        for line in dict.fromkeys(self._lines):
            line.changed()
        for follower in list(self.followers):
            follower.changed()


@dataclass
class Waits:
    """What a request waited for at add-ons' limits: ``at`` - what it is waiting for now
    (it stays set when the wait is canceled: the request's time ran out there) - and the
    seconds it waited in all. A watcher around this one (``outer``) is told the same. One
    record is one request's: tasks started inside ``watched`` would share it."""

    at: str | None = None
    seconds: float = 0.0
    outer: Waits | None = None
    since: float | None = None  # when the wait under way began (``time.monotonic``)

    def _began(self, what: str) -> None:
        now = time.monotonic()
        node: Waits | None = self
        while node is not None:
            node.at, node.since = what, now
            node = node.outer

    def _ended(self, seconds: float) -> None:
        node: Waits | None = self
        while node is not None:
            node.at, node.since = None, None
            node.seconds += seconds
            node = node.outer

    def joined(self) -> tuple[float, float, float | None]:
        """What another request notes of this record when it starts to wait for this
        request's answer (``shared``): when, what was waited so far, and since when a wait
        is under way."""
        return time.monotonic(), self.seconds, self.since

    def shared(self, other: Waits, joined: tuple[float, float, float | None]) -> None:
        """This request waited for another request's answer (a manifest read once for
        both) from ``joined`` (``other.joined()``) until now: what that one waited at the
        limits meanwhile - only meanwhile - this one waited there too, and it is waiting
        there still when the other is (its own time ran out). What the other waited before
        this one came, or spends at the add-on, is not this one's wait at the limits."""
        at, before, under_way = joined
        now = time.monotonic()
        waited = other.seconds - before  # its waits that ended since
        if under_way is not None and other.since != under_way:
            waited -= at - under_way  # (of a wait under way then: only what came after)
        if other.at is not None and other.since is not None:
            waited += now - max(other.since, at)  # ... and of the one under way now
        waited = max(0.0, min(waited, now - at))
        if waited > 0 or other.at is not None:
            self._began(other.at or REQUESTS)
            self._ended(waited)
            if other.at is not None:
                self._began(other.at)

    def held(self, moment: float = 1.0) -> str | None:
        """Why a time that ran out is not the add-on's doing: it ran out while the request
        was waiting at the limits (what for), or the request had waited there for more than
        ``moment`` seconds in all, which the add-on then did not get. None: the add-on had
        the time."""
        return self.at or (REQUESTS if self.seconds >= moment else None)


_URGENCY: ContextVar[Urgency | None] = ContextVar("shijhon_addon_urgency", default=None)
_WAITS: ContextVar[Waits | None] = ContextVar("shijhon_addon_waits", default=None)


def current() -> Urgency | None:
    """The urgency the caller's add-on requests have, when one is set."""
    return _URGENCY.get()


def watching() -> Waits | None:
    """The record of the caller's waits at add-ons' limits, when they are watched."""
    return _WAITS.get()


@contextmanager
def urgent(urgency: Urgency | int) -> Iterator[Urgency]:
    """The add-on requests made within have this urgency (tasks started within too)."""
    mine = urgency if isinstance(urgency, Urgency) else Urgency(urgency)
    token = _URGENCY.set(mine)
    try:
        yield mine
    finally:
        _URGENCY.reset(token)


@contextmanager
def watched(record: Waits | None = None) -> Iterator[Waits]:
    """Notes the waits at add-ons' limits of the requests made within - also for a watcher
    around this one (an attempt's lookup and audio, watched each and together). ``record``:
    one of its own, which no watcher around it is told of (work of a task of its own that
    requests wait for: they note its waits as shared)."""
    waits = record if record is not None else Waits(outer=_WAITS.get())
    token = _WAITS.set(waits)
    try:
        yield waits
    finally:
        _WAITS.reset(token)


@dataclass(eq=False)
class _Waiter:
    urgency: Urgency
    number: int
    woken: anyio.Event = field(default_factory=anyio.Event)


class _Line:
    """Requests waiting for something an add-on gives out a little at a time: the most
    urgent first, then in the order they came. Only the first in line looks whether its
    turn has come: a change wakes that one, not all of them."""

    def __init__(self) -> None:
        self._waiting: list[_Waiter] = []
        self._numbers = itertools.count()

    def __len__(self) -> int:
        return len(self._waiting)

    def _first(self) -> _Waiter | None:
        return min(self._waiting, key=lambda w: (w.urgency.level, w.number), default=None)

    def changed(self) -> None:
        """Something changed (one left, one became more urgent, more is given out): the
        first in line - as it is now - looks again."""
        first = self._first()
        if first is not None:
            first.woken.set()

    async def enter(self, urgency: Urgency, take: Callable[[], float | None]) -> None:
        """Wait for a turn: ``take`` takes one (None) or says in how many seconds one may be
        had (infinity: when told, ``changed``). Nobody waiting and one to be had: at once."""
        if not self._waiting and take() is None:
            return
        me = _Waiter(urgency, next(self._numbers))
        self._waiting.append(me)
        urgency._lines.append(self)
        try:
            while True:
                # (Nothing between this look and the wait can change the line: no await.)
                delay = take() if self._first() is me else math.inf
                if delay is None:
                    return
                me.woken = anyio.Event()
                with anyio.move_on_after(None if delay == math.inf else delay):
                    await me.woken.wait()
        finally:
            self._waiting.remove(me)
            urgency._lines.remove(self)
            self.changed()  # the next one's turn


class Turns:
    """So many at once for everyone together, more waiting their turn by urgency (``_Line``)
    - work that reaches add-ons, such as joining DASH links. As with an add-on's audio
    openings, the song being played is not held back (and counts). A wait for a turn is
    noted as one at the limits (``what``: what it was waiting for), so that a time that
    runs out there is not taken for the add-on's."""

    def __init__(self, total: int, what: str) -> None:
        self._total = max(1, total)
        self._taken = 0
        self._line = _Line()
        self.what = what

    @property
    def total(self) -> int:
        """How many at once (changed at once: those waiting look again)."""
        return self._total

    @total.setter
    def total(self, value: int) -> None:
        self._total = max(1, value)
        self._line.changed()

    @property
    def taken(self) -> int:
        return self._taken

    def _take(self, urgency: Urgency) -> float | None:
        if urgency.level <= PLAY or self._taken < self._total:
            self._taken += 1
            return None
        return math.inf

    @asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        """One turn, for the time of the block: at once while one is free and nobody waits."""
        urgency = _URGENCY.get() or Urgency()
        waits = _WAITS.get()
        began = time.monotonic()
        if waits is not None:
            waits._began(self.what)
        # (Canceled: the wait stays noted.)
        await self._line.enter(urgency, lambda: self._take(urgency))
        if waits is not None:
            waits._ended(time.monotonic() - began)
        try:
            yield
        finally:
            self._taken -= 1
            self._line.changed()


@dataclass(frozen=True)
class Limits:
    """An add-on's limits: requests a second (0: no limit), how many may go at once after a
    quiet moment, and audio openings at once (0: no limit). None: the installation's."""

    requests_per_second: float | None = None
    request_burst: int | None = None
    audio_openings: int | None = None

    def over(self, default: Limits) -> Limits:
        """These, with ``default`` where they say nothing."""
        return Limits(
            default.requests_per_second
            if self.requests_per_second is None
            else self.requests_per_second,
            default.request_burst if self.request_burst is None else self.request_burst,
            default.audio_openings if self.audio_openings is None else self.audio_openings,
        )

    def stricter(self, other: Limits) -> Limits:
        """The stricter of each (both complete; 0 is no limit)."""

        def least(mine: float | None, theirs: float | None) -> float:
            values = [v for v in (mine, theirs) if v]
            return min(values) if values else 0

        return Limits(
            least(self.requests_per_second, other.requests_per_second),
            max(1, int(min(self.request_burst or 1, other.request_burst or 1))),
            int(least(self.audio_openings, other.audio_openings)),
        )


DEFAULTS = Limits(2.0, 4, 4)


class AddonPace:
    """One add-on origin's limits, shared by everything Shijhon sends it."""

    def __init__(
        self, limits: Limits = DEFAULTS, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.clock = clock
        self.rate = 0.0
        self.burst = 1
        self.openings = 0
        self._tokens = 0.0
        self._filled_at = clock()
        self._requests = _Line()
        self._audio = _Line()
        self._opening = 0  # audio openings under way
        self._blocked_until = -math.inf  # after a rate limit: no API request before
        self._audio_blocked_until = -math.inf  # ... on its audio: no audio request either
        self.cooldown = COOLDOWN  # ... for this long, when its answer names no time
        self.sent = 0  # API requests given their turn (observable)
        self.configure(limits, fill=True)

    def configure(self, limits: Limits, *, fill: bool = False) -> None:
        """Its limits from now on (complete: ``Limits.over``); those waiting look again."""
        complete = limits.over(DEFAULTS)
        assert complete.requests_per_second is not None and complete.request_burst is not None
        assert complete.audio_openings is not None
        self._refill()
        self.rate = max(0.0, float(complete.requests_per_second))
        self.burst = max(1, int(complete.request_burst))
        self.openings = max(0, int(complete.audio_openings))
        self._tokens = float(self.burst) if fill else min(self._tokens, float(self.burst))
        self._requests.changed()
        self._audio.changed()

    @property
    def limits(self) -> Limits:
        return Limits(self.rate, self.burst, self.openings)

    @property
    def idle(self) -> bool:
        """Nothing waits, nothing is being opened, its requests are all to be had, and it
        is not left alone after a rate limit."""
        self._refill()
        full = self.rate <= 0 or self._tokens >= self.burst
        quiet = not self._opening and not len(self._requests) and not len(self._audio)
        return full and quiet and not self.blocked and not self.audio_blocked

    def block(self, seconds: float, *, audio: bool = False) -> float:
        """The add-on answered "too many requests": no API request is sent to it for
        ``seconds`` (at least a moment, at most ``MAX_RETRY_AFTER``) - or until an earlier
        answer's time, when that is later: a block is only ever made longer. ``audio``: its
        audio said so - no audio request either. Those waiting for a turn are told
        (``Blocked``). Returns the seconds it is blocked for now."""
        seconds = min(max(MIN_RETRY_AFTER, seconds), MAX_RETRY_AFTER)
        until = self.clock() + seconds
        self._blocked_until = max(self._blocked_until, until)
        self._requests.changed()
        if audio:
            self._audio_blocked_until = max(self._audio_blocked_until, until)
            self._audio.changed()
        return self.blocked

    def limited(self, named: float | None, *, audio: bool = False) -> float:
        """One of the add-on's answers was "too many requests", naming ``named`` seconds in
        its ``Retry-After`` (``retry_after``; None: none, or not valid - the cooldown
        then): it is left alone (``block``; ``audio``: the answer was its audio's). Told by
        whoever got the answer, at once - a lookup, a check, a preparation request, the
        dashboard - so that nobody need remember to."""
        return self.block(self.cooldown if named is None else named, audio=audio)

    @property
    def blocked(self) -> float:
        """How long no API request is sent to the add-on still (0: it may be asked)."""
        return max(0.0, self._blocked_until - self.clock())

    @property
    def audio_blocked(self) -> float:
        """How long no audio request is sent to it either (after its audio's 429)."""
        return max(0.0, self._audio_blocked_until - self.clock())

    def _refill(self) -> None:
        now = self.clock()
        if self.rate > 0:
            gained = (now - self._filled_at) * self.rate
            self._tokens = min(float(self.burst), self._tokens + gained)
        self._filled_at = now

    def _take(self) -> float | None:
        if (left := self.blocked) > 0:
            raise Blocked(left)
        if self.rate <= 0:
            self.sent += 1
            return None
        self._refill()
        if self._tokens >= 1 - 1e-9:
            self._tokens = max(0.0, self._tokens - 1)
            self.sent += 1
            return None
        return (1 - self._tokens) / self.rate

    def _take_opening(self, urgency: Urgency) -> float | None:
        if (left := self.audio_blocked) > 0:
            raise Blocked(left)
        # The song being played is asked for at once (and counts): the cap holds back the
        # rest - fetches ahead, probes, downloads, warm-ahead.
        if self.openings <= 0 or urgency.level <= PLAY or self._opening < self.openings:
            self._opening += 1
            return None
        return math.inf

    async def request(self) -> None:
        """One API request's turn at the add-on (at once while it is idle). Raises
        ``Blocked`` while the add-on is left alone after a rate limit - also for a request
        that was waiting when that began."""
        waits = _WAITS.get()
        if waits is None:
            await self._requests.enter(_URGENCY.get() or Urgency(), self._take)
            return
        began = self.clock()
        waits._began(REQUESTS)
        await self._requests.enter(_URGENCY.get() or Urgency(), self._take)
        waits._ended(self.clock() - began)

    async def open(self) -> Callable[[], None]:
        """One audio request's opening: waits for one of the add-on's openings at once
        (never for the song being played), and returns what ends it - told once the
        answer's first bytes are there, or it has ended without any (told twice: once).
        Raises ``Blocked`` like ``request``."""
        urgency = _URGENCY.get() or Urgency()
        waits = _WAITS.get()
        began = self.clock()
        if waits is not None:
            waits._began(OPENINGS)
        await self._audio.enter(urgency, lambda: self._take_opening(urgency))
        if waits is not None:
            waits._ended(self.clock() - began)
        over = False

        def ended() -> None:
            nonlocal over
            if not over:
                over = True
                self._opening -= 1
                self._audio.changed()

        return ended

    @asynccontextmanager
    async def opening(self) -> AsyncIterator[None]:
        """Around one audio opening (``open``, ended when the block ends)."""
        ended = await self.open()
        try:
            yield
        finally:
            ended()


async def send(
    http: httpx.AsyncClient,
    request: httpx.Request,
    *,
    turn: Callable[[httpx.URL], Awaitable[None]],
    max_redirects: int = 5,
    private: tuple[str, ...] = (),
) -> httpx.Response:
    """Send ``request`` to an add-on (its API, its audio), each hop after ``turn`` (told
    the address the hop goes to; it may wait, or refuse with ``Blocked``): redirects are
    followed here, one request after the other, not by the client - what holds for a
    request holds for every hop of it. ``private``: headers not sent on from a hop to
    another origin than the request's own. Returns the answer that is no redirect, its body
    still to be read: the caller closes it."""
    home = origin(str(request.url))
    for _ in range(max_redirects + 1):
        await turn(request.url)
        response = await http.send(request, stream=True, follow_redirects=False)
        following = response.next_request  # set for a redirect that says where to
        if following is None:
            return response
        await response.aclose()
        if private and origin(str(following.url)) != home:
            for name in private:
                following.headers.pop(name, None)
        request = following
    raise httpx.TooManyRedirects("too many redirects", request=request)


@asynccontextmanager
async def get(
    http: httpx.AsyncClient,
    url: httpx.URL | str,
    *,
    turn: Callable[[httpx.URL], Awaitable[None]],
    headers: Mapping[str, str] | None = None,
    max_redirects: int = 5,
) -> AsyncIterator[httpx.Response]:
    """A GET to an add-on's API, each hop of which takes its turn (``send``): a redirected
    request is two requests, and counts as two. Yields the answer that is no redirect, its
    body still to be read."""
    request = http.build_request("GET", url, headers=headers)
    response = await send(http, request, turn=turn, max_redirects=max_redirects)
    try:
        yield response
    finally:
        await response.aclose()


@dataclass
class Paces:
    """Every add-on origin's limits. ``defaults``: the installation's (settings), for an
    origin no enabled add-on names limits of its own for."""

    defaults: Limits = DEFAULTS
    clock: Callable[[], float] = time.monotonic
    # How long an add-on is left alone after a rate limit that names no time.
    cooldown: float = COOLDOWN
    _paces: dict[Origin, AddonPace] = field(default_factory=dict)
    _own: dict[Origin, Limits] = field(default_factory=dict)  # the enabled add-ons' limits

    def of(self, base_url: str) -> AddonPace | None:
        """The limits of the origin ``base_url`` is at (None: not an address)."""
        where = origin(base_url)
        if where is None:
            return None
        pace = self._paces.get(where)
        if pace is None:
            if len(self._paces) >= 256:  # addresses once tried (the dashboard's): forgotten
                for key in [k for k, p in self._paces.items() if k not in self._own and p.idle]:
                    del self._paces[key]
            pace = self._paces[where] = AddonPace(self._limits(where), clock=self.clock)
        pace.cooldown = self.cooldown
        return pace

    def own(self, url: str) -> AddonPace | None:
        """The limits of the origin ``url`` is at when an enabled add-on is there; None for
        any other address (a catalog's cover on an image host takes no turn; one the
        add-on serves itself does)."""
        where = origin(url)
        return self.of(url) if where is not None and where in self._own else None

    def _limits(self, where: Origin) -> Limits:
        return self._own.get(where, self.defaults)

    def apply(self, addons: list[tuple[str, Limits]]) -> None:
        """The enabled add-ons (address, own limits): each origin gets its add-on's limits
        over the installation's - of several add-ons at one origin, the stricter of each."""
        own: dict[Origin, Limits] = {}
        for base_url, limits in addons:
            where = origin(base_url)
            if where is None:
                continue
            complete = limits.over(self.defaults)
            own[where] = own[where].stricter(complete) if where in own else complete
        self._own = own
        for where, pace in self._paces.items():
            wanted = self._limits(where)
            if pace.limits != wanted:
                pace.configure(wanted)
