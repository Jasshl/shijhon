"""Streaming placeholder audio from add-ons.

- **Resolution**: the add-ons' plan (``SourceRegistry``: their order, in steps), skipping
  disabled and cooling ones. For a song someone waits for, the add-ons of a step start
  together, each within the step's time; the first audio plays and the others are
  stopped, and a step that ends without audio leads to the next (``_run_plan``). Other
  requests - fetches ahead, warm-ahead, downloads, probes - ask one add-on at a time in
  the plan's order. With ``ready_checks`` the add-ons that can tell are asked whether they
  have the song ready: one that has goes ahead, one that has not goes last.
- **Pinning**: a play is pinned to one representation — the source's URL, and its strong
  ETag (sent as ``If-Match``) and size once seen. Seeks (non-zero ranges) never switch to
  another file: a play continuing after its pin expired or its link stopped working (a
  paused song) is re-resolved at the source it started from first, then at the others,
  and each is accepted only with the same representation (size and ETag). A new
  representation is taken only at byte zero.
- **Ranges** pass through; a source that ignores ``Range`` is sliced to the requested
  range; ``HEAD`` is answered from a one-byte ranged GET (some sources refuse HEAD).
- **DASH links** (``delivery.dash``) answer as a ranged source would: served at once as
  one MP4 file of their segments, or joined into one file first.
- **Cooldowns** after a source reports a rate limit, and after errors in a row.
- **Limits per add-on** (``pacing``): what one add-on is sent in all - its API requests
  a second, its audio requests being opened at once - with the song being played first. A
  wait there is part of the request's budget; one that runs out there is a timeout that is
  not the add-on's failure.
- **Misses** are remembered (an add-on that said it lacks a recording is not asked for it
  again for a while), and so are timeouts: an add-on that keeps timing out cools down.
- **A request's own test** of the audio (``Deliverer.open``'s ``accept``: a listener's
  minimum quality, say): audio it refuses is passed over like another recording - the
  routing goes on to the next source, in the same order and budget - but the link stays,
  and the add-on is not held to have failed.
- **Diagnostics**: a play that ends without audio is logged with the reason and the time
  of each step at each source (a routing trace); so is a play served by a fallback. The
  latest routings are kept for the dashboard's Diagnostics (``Deliverer.recent``).
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any

import anyio
import httpx

from shijhon.catalog.model import ITEM_ID
from shijhon.delivery import pacing
from shijhon.delivery.addon import (
    AddonError,
    Availability,
    StreamInfo,
    Wanted,
    of_catalog,
    own_track,
)
from shijhon.delivery.dash import CapacityLease, Dash, DashError, Link, Stream, link_key
from shijhon.delivery.length import PEEK_BYTES, audio_kind, audio_length, complete, peek
from shijhon.delivery.netpolicy import denied
from shijhon.delivery.sources import Attempt, Source, SourceRegistry, usual_seconds
from shijhon.locks import KeyedLocks

if TYPE_CHECKING:
    from shijhon.delivery.bulk import BulkAcquisition
    from shijhon.delivery.retain import Capture, Retention

_BULK_LEASE: ContextVar[CapacityLease | None] = ContextVar("bulk_upstream_lease", default=None)
_BULK_REUSE: ContextVar[bool] = ContextVar("bulk_existing_dash_job", default=False)
_LIBRARY_ACQUISITION: ContextVar[bool] = ContextVar("library_acquisition", default=False)
# A request's own test of the audio it opens, by its format and bitrate (``Deliverer.open``'s
# ``accept``): None when it will do, else why not.
Accept = Callable[[str | None, int | None], str | None]
_ACCEPT: ContextVar[Accept | None] = ContextVar("shijhon_accept", default=None)
# Whether the audio opened may go to a client (not a probe, warm-ahead or a fetch for the
# library): its bytes are captured for ``Deliverer.retention`` until it is known.
_KEEPS: ContextVar[bool] = ContextVar("shijhon_keeps", default=False)
# The request is for a listener waiting for the audio (not a download, nor a fetch for the
# library): its add-ons may race.
_WAITED: ContextVar[bool] = ContextVar("shijhon_waited", default=False)

log = logging.getLogger(__name__)

DASH_KEPT = "shijhon.dash_kept"  # a DASH answer's extension: the kept file it is read from
CAP_GRACE = 0.25  # seconds past a client's wait cap before its request is abandoned
_GONE = ("unavailable", "expired")  # a track ID the add-on had before is no longer there
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_STRONG_ETAG = re.compile(r'^"[\x21\x23-\x7e]*"$')
_TYPES = {
    "flac": "audio/flac",
    "alac": "audio/mp4",
    "aac": "audio/mp4",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "vorbis": "audio/ogg",
    "wav": "audio/wav",
}


class NoSource(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons) or "no enabled source")
        self.reasons = reasons


class PinBroken(Exception):
    """The pinned representation is gone and the request is not at byte zero."""


class _Reacquire(Exception):
    """A continuing DASH request now needs its own upstream admission."""

    def __init__(self, reason: str, *, resolve: bool = False) -> None:
        super().__init__(reason)
        self.resolve = resolve


class _Changed(Exception):
    """Expired or changed representation; ``status``: the link's HTTP answer (403, 410,
    412), None when it served another file."""

    waited = 0.0  # seconds its request waited at the add-on's limits: not its time

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.status = status


class _Failed(Exception):
    waited = 0.0  # seconds its request waited at the add-on's limits: not its time


class _TimedOut(_Failed):
    pass


class _Paced(_TimedOut):
    """The request's time ran out while it waited for its turn at the add-on's limits (its
    requests a second, its audio openings at once) - or after it had waited there for
    more than a moment, which the add-on then did not have: a timeout for the routing, and
    nothing the add-on did - not its failure, nothing toward a cooldown or the measured
    order, and a client's retry asks it again."""


class _Joining(_TimedOut):
    """The attempt's time ran out while its DASH link was being joined, and the join goes
    on (up to the client's wait cap) or has just kept its file: a timeout as any other for
    the routing - but the link is kept, and a client's retry asks that add-on again, so that
    it gets the joined file at once or waits for the same join. ``again``: an earlier
    request's time ran out on the same join (a primary's timeout counts once a join)."""

    def __init__(self, reason: str, *, again: bool = False) -> None:
        super().__init__(reason)
        self.again = again


class _Preparing(_TimedOut):
    """The add-on was still preparing the song (an HTTP 503 with a ``Retry-After``): not
    ready in time. Its time without audio goes into the measured order; no error, and no
    failure for the song that a client's retry - or a routing that continues this one -
    would pass the add-on over for: by then the song may be ready. ``waited_out``: the
    attempt waited for it until its time ran out (or until less was left than the next
    wait) - a timeout for the routing (an attempt used, a primary's timeout); else its first
    answer named a longer time than was left, and the routing moved on at once (no attempt
    used, nothing toward the primary's timeouts)."""

    def __init__(self, reason: str, *, waited_out: bool) -> None:
        super().__init__(reason)
        self.waited_out = waited_out


class _Cooling(_Failed):
    """The source was not asked: it answered "too many requests" a while ago, and the time it
    named has not passed. Nothing it failed at now: no failure of its own, no attempt
    used - the routing goes on to the next source."""


class _Limited(_Failed):
    """The source answered "too many requests" (its lookup, its link, its audio): a failed
    attempt as any other for the routing, and it is left alone for its ``Retry-After`` -
    but nothing about the song: a background preparation asks it again after that time."""


class _Error(_Failed):
    """The source answered with an error (an HTTP 5xx), a malformed answer, or not at all
    (no connection, an unexpected failure): counted toward its cooldown, unlike a
    timeout, a refusal or a miss."""


class _Left(Exception):
    """A request left another request's link: that request's race went on without it."""


class _Other(Exception):
    """Another request's link turned out to link another file than the one a continuing
    play started with: not that play's (the link itself is fine and stays)."""


class _Wrong(_Failed):
    """The source delivered another recording: its length is not the catalog's."""


class _Unusable(_Failed):
    """The link's audio is not for playing, and nothing went wrong at the add-on: a DASH
    manifest of a kind Shijhon does not play (live, protected, another codec, too large),
    or a preview. Like another recording it uses no attempt and counts toward no cooldown;
    unlike one it is not remembered: the add-on is asked again after the retry window."""


class _Refused(_Failed):
    """The request refused the audio (``Deliverer.open``'s ``accept``: below the listener's
    minimum quality, say), and nothing went wrong at the add-on: like another recording it
    uses no attempt and counts toward no cooldown - but the link stays (it may do for
    another request), nothing is remembered, and a client's retry asks the add-on again."""


class _Missing(Exception):
    """The source does not have the track (not counted as an attempt). ``absent``: it said
    so (not found), rather than refusing or lacking the resource."""

    waited = 0.0  # seconds its request waited at the add-on's limits: not its time

    def __init__(self, reason: str, *, absent: bool = False) -> None:
        super().__init__(reason)
        self.absent = absent


@dataclass
class Leftover:
    """What a routing that ended without audio left to try: its order, the add-on track IDs
    known, the sources done with (they failed or lacked the song) and the one the client's
    wait cap cut short while it was still preparing the song."""

    at: float = 0.0
    order: list[int] = field(default_factory=list)
    hints: dict[int, str] = field(default_factory=dict)
    done: set[int] = field(default_factory=set)
    cut: int | None = None
    planned: bool = False  # it got as far as its plan (else nothing to continue)
    # Of ``done``, those given up on while they may still have been preparing the song
    # (stopped for another add-on's audio, or for one that has the song ready): no failure
    # for a client's retry.
    slow: set[int] = field(default_factory=set)
    # Those that said they were preparing the song when their time ran out: not done with -
    # a routing that continues this one asks them again (after the one cut short).
    preparing: set[int] = field(default_factory=set)


@dataclass
class Trace:
    """What one routing did - each step with its source and time - for the log line of a
    play that ends without audio (and of one a fallback served); and what it left to try,
    for a routing that continues it."""

    started: float
    steps: list[str] = field(default_factory=list)
    ended: str = ""  # why the routing stopped without audio
    failed: int = 0  # sources that failed (not merely lacked the track)
    left: Leftover = field(default_factory=Leftover)
    # Set when the routing is over. A request that used a link it found, which failed, waits
    # for it - for a client's routing (``capped``: bounded by a client's wait cap) -
    # and gets its answer when it is one shared with requests that waited (``shared``: a
    # play's full routing that ended without audio).
    over: anyio.Event = field(default_factory=anyio.Event)
    ended_at: float = 0.0  # when it was over
    capped: bool = False
    shared: list[str] | None = None

    def add(self, text: str) -> None:
        if len(self.steps) < 40:
            self.steps.append(text)

    def stop(self, why: str) -> None:
        if not self.ended:
            self.ended = why


@dataclass
class Departure:
    """When the waiting client left before its answer (set by the request's handler, on the
    deliverer's clock)."""

    at: float | None = None


@dataclass
class _Checks:
    """The availability checks of one play's routing (``ready_checks``), answered while its
    steps run: they may outlast the step they began in."""

    sources: list[Source]  # those asked
    answers: dict[int, Availability | None] = field(default_factory=dict)
    asked: set[int] = field(default_factory=set)  # answered, failed or timed out
    done: anyio.Event = field(default_factory=anyio.Event)
    ready_seen: anyio.Event = field(default_factory=anyio.Event)  # a check said "ready"

    def answer(self, source: Source) -> str:
        """Its check's answer: "ready", "not now" or "-" (it cannot tell, has no check, or
        has not answered yet)."""
        answer = self.answers.get(source.id) if source.id in self.asked else None
        if answer is None or answer.available is None:
            return "-"
        return "ready" if answer.available else "not now"

    def ready(self, source: Source) -> bool:
        return self.answer(source) == "ready"

    def said_no(self, source: Source) -> bool:
        return self.answer(source) == "not now"

    def hints(self) -> dict[int, str]:
        """The add-on track IDs the checks that said ready gave."""
        return {
            source_id: answer.track_id
            for source_id, answer in self.answers.items()
            if answer is not None and answer.available and answer.track_id
        }


@dataclass
class _Handover:
    """When an add-on whose check says it has the song ready takes over from a step still
    without audio (None: not, or not any more)."""

    at: float | None


@dataclass(frozen=True)
class Routing:
    """One routing at byte zero, for the dashboard's Diagnostics: when (wall clock), the
    song, what it was for, the add-on that served it (empty: none), how long its first
    byte took, and its steps."""

    at: float
    title: str
    artist: str
    purpose: str
    served: str
    seconds: float
    steps: tuple[str, ...]
    ended: str = ""  # why it ended without audio


_TRACE: ContextVar[Trace | None] = ContextVar("shijhon_routing_trace", default=None)
# A download-first fetch's routing: the sources it does not use (``Deliverer.open``'s
# ``download``, ``not_for_downloads``), in their order - each with why, for its failures.
_NOT_FOR_DOWNLOAD: ContextVar[tuple[tuple[int, str], ...]] = ContextVar(
    "shijhon_not_for_download", default=()
)


def _left_out() -> frozenset[int]:
    """The sources the download-first fetch under way does not use (none: no such fetch)."""
    return frozenset(source_id for source_id, _ in _NOT_FOR_DOWNLOAD.get())


def _step(text: str) -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.add(text)


def _stop(why: str) -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.stop(why)


def _done(
    source: Source, *, cut: bool = False, slow: bool = False, preparing: bool = False
) -> None:
    """The routing is done with ``source`` (it failed or lacked the song); ``cut``: the
    client's wait cap ended its attempt while it was still preparing the song; ``slow``:
    given up on for another source while it may still have been preparing it;
    ``preparing``: it said it was preparing the song (``_Preparing``) - not done with."""
    trace = _TRACE.get()
    if trace is not None:
        if cut:
            trace.left.cut = source.id
        elif preparing:
            trace.left.preparing.add(source.id)
        else:
            trace.left.done.add(source.id)
            if slow:
                trace.left.slow.add(source.id)


def _until_started(pin: Pin, cap: float, clock: Callable[[], float]) -> float:
    """The time a request on a race's link may take: until the client's wait, until the
    link's audio came."""
    return 0.0 if pin.delivered else cap - clock()


def _abandon(pin: Pin) -> None:
    """A link a race published early is not the song's: the requests of others waiting on
    it leave it (they follow the race to its next link or its answer)."""
    pin.gone = True
    for scope in pin.watchers or ():
        scope.cancel()


def _failed_step(text: str) -> None:
    """A step at which a source failed (an error, a timeout), not merely lacked the track."""
    trace = _TRACE.get()
    if trace is not None:
        trace.failed += 1
        trace.add(text)


class _Shared(NoSource):
    """The answer of another request's failed routing of the song, shared."""


# Add-on errors that count toward a source's cooldown: an HTTP 5xx or no connection
# (a connect timeout too) - "failed" - and a malformed answer ("broken"). Answers about one
# song (an unexpected status, no URL: "invalid") and slow answers do not (the primary's
# timeouts have a rule of their own).
_ERRORS = {"failed", "broken"}
ERROR_RUN_SECONDS = 600.0  # errors further apart than this are not one run

_PURPOSES = {
    "play": "play",
    "ahead": "fetch ahead",
    "alone": "fetch ahead",
    "warm": "warm-ahead",
    "download": "download",
}
# How urgent a request's add-on work is at the add-ons' limits: the song being played
# first, then a client's fetches ahead, then warm-ahead.
_URGENCIES = {"ahead": pacing.QUEUED, "alone": pacing.QUEUED, "warm": pacing.WARM}


@dataclass(frozen=True)
class ByteRange:
    start: int | None  # None: suffix range
    end: int | None

    @classmethod
    def parse(cls, header: str | None) -> ByteRange | None:
        if not header:
            return None
        match = _RANGE.match(header.strip().split(",")[0].strip())
        if not match or not any(match.groups()):
            return None
        start = int(match.group(1)) if match.group(1) else None
        end = int(match.group(2)) if match.group(2) else None
        if start is not None and end is not None and end < start:
            return None
        return cls(start, end)

    @property
    def at_zero(self) -> bool:
        return self.start == 0

    def header(self) -> str:
        start = "" if self.start is None else str(self.start)
        end = "" if self.end is None else str(self.end)
        return f"bytes={start}-{end}"

    def resolve(self, total: int) -> tuple[int, int] | None:
        """Absolute (first, last) for a representation of ``total`` bytes; None if unsatisfiable."""
        if self.start is None:
            if not self.end:
                return None
            return max(0, total - self.end), total - 1
        if self.start >= total:
            return None
        return self.start, total - 1 if self.end is None else min(self.end, total - 1)


@dataclass(frozen=True)
class Track:
    """A track to deliver. ``song_id`` keys its routing and pin: the native song ID, or the
    catalog track (``ref``) for a song played before it is in the library."""

    song_id: str
    isrc: str | None
    title: str
    artist: str
    duration_ms: int
    ref: str | None = None  # the catalog track, e.g. "demo:123"
    release: str | None = None  # its catalog release
    disc: int = 1
    number: int = 0
    version: str | None = None  # "clean" or "explicit", when the catalog says

    @property
    def wanted(self) -> Wanted:
        return Wanted(self.isrc, self.title, self.artist, self.duration_ms, self.version)

    @property
    def key(self) -> str:
        """The same recording before and after its commit."""
        return self.ref or self.song_id


@dataclass
class Pin:
    song_id: str
    source: Source
    track_id: str
    info: StreamInfo
    created: float
    etag: str | None = None
    size: int | None = None
    content_type: str | None = None
    # By its step's time: when its first byte must have arrived, and when it would have
    # without a client's wait cap.
    first_byte_by: float | None = None
    own_by: float | None = None
    # How long its lookup and its link took (for the routing trace).
    lookup_seconds: float = 0.0
    link_seconds: float = 0.0
    # Its source's stats: counted once per link (requests may share a pin).
    delivered: bool = False  # its audio answered: the source's success
    failed: bool = False  # a new link that did not work: the source's failure
    # The routing that found it: another request that waited for it uses its first byte
    # with a budget of its own, not what the wait left of its own.
    by: Trace | None = None
    # The audio's length (seconds) as its first bytes tell, once read; ``checked``: read
    # (or found not to be readable), so later requests do not read it again.
    length: float | None = None
    checked: bool = False
    wrong: str | None = None  # read and rejected: another recording (why)
    # Its format as Navidrome names a file of it, and its bitrate (kbit/s) as Navidrome reads
    # it, when its first bytes tell.
    kind: str | None = None
    kbps: int | None = None
    errored: bool = False  # its error counted toward the source's cooldown (once a link)
    # What its check had said when it was tried, and how long its lookup meanwhile took
    # before its attempt (its attempt's record).
    answer: str = "-"
    looked_before: float = 0.0
    waited: float = 0.0  # seconds its lookup and link waited at the add-on's limits
    seconds: float = 0.0  # the catalog's length of the song it was found for
    # A DASH link served at once: what its audio is copied out as for the library, and its
    # length as its index tells it (an MP4 of fragments tells none in its first bytes).
    remux: str | None = None
    told: float | None = None
    cached: bool = False  # a ready-only variant: choose from the audio actually cached
    # A link a play's race published before its audio (``Deliverer._race``): the scopes of
    # the requests of others that wait on it, canceled when the race goes on without it
    # (``gone``) - another add-on's audio came first, or it failed.
    watchers: list[anyio.CancelScope] | None = field(default=None, repr=False, compare=False)
    gone: bool = False


@dataclass
class Opened:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: AsyncIterator[bytes] | None
    close: Callable[[], Awaitable[None]]
    source: str = ""
    length: float | None = None  # the audio's length, when its first bytes told it
    kind: str | None = None  # its format and bitrate, when they told them
    kbps: int | None = None
    # Seconds the request's attempt waited at the add-on's limits - for the link too,
    # when this request found it: not the add-on's time.
    waited: float = 0.0
    # A DASH link's file served at once (an MP4 of its segments): its audio is copied out
    # as this ("flac", "aac", "alac", "mp3") for the library (``Dash.copied_out``).
    remux: str | None = None
    # Its bytes as they come from the add-on, kept once it goes to a client
    # (``delivery/retain.py``: ``accept``); None: not kept.
    capture: Capture | None = None


@dataclass(frozen=True)
class Known:
    """The representation a song was last played from (size, strong ETag), and the source
    and add-on track ID it came from."""

    size: int | None
    etag: str | None
    source_id: int
    track_id: str
    kind: str | None = None  # its format and bitrate, when its first bytes told
    kbps: int | None = None


async def _nothing() -> None:
    return None


@dataclass
class PlaybackSettings:
    # Only this configured catalog may use its confirmed, declared item IDs directly.
    catalog_addon: str = ""
    catalog_identity: str = ""
    # A new step's time (when the next step joins a play's start), and an add-on's time
    # for a download, for an add-on without its own (``Source.step_seconds``, ``budget``).
    budget_seconds: float = 9.0
    pin_ttl_seconds: float = 1800.0
    cooldown_seconds: float = 30.0
    seek_timeout_seconds: float = 15.0
    # Ask the add-ons that can tell whether they have the song ready: one that has takes
    # over after ready_handover_seconds without audio (0: it goes first), one that has not
    # goes last.
    ready_checks: bool = False
    ready_handover_seconds: float = 4.5
    # An add-on cools down after this many timeouts without a delivery in between.
    cooldown_timeouts: int = 2
    # An add-on still trying when another has sent the song counts a timeout once it is
    # past its step's time and this many times its usual time to first audio (its own
    # ``Source.slow_after`` first); one with no usual time, once past its step's time.
    slow_after: float = 2.0
    # Any source cools down after this many errors in a row (an HTTP 5xx, no answer, a
    # broken answer) without a delivery in between, and again after each further one until
    # it delivers. 0: off.
    cooldown_errors: int = 3
    # A client's whole wait for the first byte (primary, fallbacks and own budgets
    # together), below typical client request timeouts.
    max_wait_seconds: float = 30.0
    availability_timeout_seconds: float = 2.0
    prepare_when_not_ready: bool = False
    warm_ahead_depth: int = 2
    warm_ahead_budget_seconds: float = 60.0
    # A recording an add-on did not have is not asked of it again for this long.
    miss_hours: float = 24.0
    # Delivered audio whose length (from its first bytes) differs from the catalog's by
    # more than the larger of these is another recording: not used, the next source is
    # tried, and that source is not asked for the recording again for a while. Both
    # 0: no check.
    length_tolerance_seconds: float = 10.0
    length_tolerance_percent: float = 5.0
    # A new routing of a song whose routing failed skips the sources that failed for it (or
    # lacked it) within this long, and goes to the others - a client's retry does not repeat
    # known failures; after it everything is tried again. 0: every routing tries all.
    retry_skip_seconds: float = 60.0
    # DASH links: of several qualities, the highest within this range (``mpd.QUALITIES``);
    # and the segments fetched at once for one join.
    dash_quality_from: str = "any"
    dash_quality_to: str = "lossless"
    # "at_once": served from the first segments on (``Dash.served``); "complete": joined
    # first (``Dash.joined``).
    dash_start: str = "at_once"
    dash_segments_at_once: int = 4


MAX_MISSES = 20000  # remembered misses (an add-on and a recording)
RECENT_ROUTINGS = 200  # routings kept for Diagnostics
MAX_PINS = 4096  # songs' links kept (past it: the expired, then the oldest go)
MAX_TWINS = 4096  # songs committed while they were played, known under both their names
RESUME_SECONDS = 60.0  # a routing that ended without audio can be continued this long
CUT_SECONDS = 1.0  # a source the wait cap took more than this from was cut short
# An add-on preparing the song is asked for its link again after its Retry-After, at least
# this long (seconds); one not ready within the attempt's time is "not ready in time".
MIN_PREPARING_WAIT = 0.5
NOT_READY = "not ready within its budget"
WRONG_SECONDS = 7 * 86400.0  # a source that delivered another recording is not asked for it
# A client's short range at byte zero (a probe of two bytes, warm-ahead's one) is asked from
# the source as this many first bytes - as many as reading a length may need (a large ID3
# tag) - so their length is read before the client gets any. Only what decides is read.
PROBE_BYTES = PEEK_BYTES


@dataclass
class Deliverer:
    registry: SourceRegistry
    settings: PlaybackSettings = field(default_factory=PlaybackSettings)
    clock: Callable[[], float] = time.monotonic
    _pins: dict[str, Pin] = field(default_factory=dict)
    # Links dropped after they failed, until they would have expired (``holds``).
    _lapsed: dict[str, Pin] = field(default_factory=dict)
    _resolving: KeyedLocks = field(default_factory=KeyedLocks)
    # Last representation each song was played from, and where; outlives pins so a resumed
    # play continues from the same file (at the same source first).
    _known: dict[str, Known] = field(default_factory=dict)
    # Starts background work (warm-ahead, preparation requests); set by the application.
    spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], None] | None = None
    # The urgencies of the requests under way, by song (its key): a request for a song
    # takes those already waiting at an add-on's limits for it along.
    _urgencies: dict[str, list[pacing.Urgency]] = field(default_factory=dict)
    # The songs (their keys) whose audio a client was sent: played - not merely probed
    # (HEAD), warmed or fetched for the library. The newest last; past 20,000 the oldest go.
    _heard: dict[str, None] = field(default_factory=dict)
    # (source, recording) -> until when: the add-on did not have the recording (by ISRC,
    # else the track); it is skipped for it until then.
    _misses: dict[tuple[int, str], float] = field(default_factory=dict)
    # The latest routings at byte zero, newest last (Diagnostics).
    recent: deque[Routing] = field(default_factory=lambda: deque(maxlen=RECENT_ROUTINGS))
    # The last failed routing of each song: when, and why.
    _failed: dict[str, tuple[float, list[str]]] = field(default_factory=dict)
    # What the last routing of each song that ended without audio left to try.
    _leftovers: dict[str, Leftover] = field(default_factory=dict)
    # (source, recording) -> until when: the source delivered another recording.
    _wrong: dict[tuple[int, str], float] = field(default_factory=dict)
    # The sources that failed for each song (or lacked it) in routings that ended without
    # audio, and when: a new routing of it skips them for a while.
    _failed_at: dict[str, dict[int, float]] = field(default_factory=dict)
    # Set when a song gets a new link: requests whose borrowed link failed wait for it.
    _published: dict[str, anyio.Event] = field(default_factory=dict)
    # A song committed while it was played or routed under its catalog track (``adopt``):
    # its native ID -> that track, and back. The two names are one song: one routing at a
    # time, a link found or dropped under one is found or dropped under the other, and a
    # request waiting for the song's next link under either gets it - so a request by the
    # native ID that took over the catalog play's link gets that routing's next one.
    _provisional: dict[str, str] = field(default_factory=dict)
    _committed: dict[str, str] = field(default_factory=dict)
    # Joins DASH links into files (None: DASH is off, for ``dash_off``'s reason).
    dash: Dash | None = None
    dash_off: str = "it is not set up"
    bulk: BulkAcquisition | None = None
    # Audio streamed in full replaces the song's placeholder (``delivery/retain.py``).
    retention: Retention | None = None

    def _names(self, song_id: str) -> tuple[str, ...]:
        """The names the song's link is kept under: ``song_id``, and its other one."""
        twin = self._provisional.get(song_id) or self._committed.get(song_id)
        return (song_id,) if twin is None else (song_id, twin)

    def _routing_lock(self, song_id: str) -> AbstractAsyncContextManager[None]:
        """The lock of the song's routing: one under both its names."""
        return self._resolving.hold(self._provisional.get(song_id, song_id))

    def pinned(self, song_id: str) -> Pin | None:
        pin = self._pins.get(song_id)
        if pin is None:
            return None
        if pin.wrong is not None:  # another recording: not the song's link
            self.forget(song_id, pin)
            return None
        if self._expired(pin):
            self.forget(song_id, pin)
            return None
        return pin

    def _expired(self, pin: Pin) -> bool:
        return self.clock() - pin.created > self.settings.pin_ttl_seconds or (
            pin.info.expires_at is not None and time.time() > pin.info.expires_at - 5
        )

    def forget(self, song_id: str, pin: Pin | None = None) -> None:
        """Drop the song's link - only ``pin`` when given: a request whose link failed late
        must not drop the newer link another request found meanwhile. Such a link, not
        expired, is still the song's for ``holds`` until it would have expired (its
        request looks for a new one to the same file)."""
        for name in self._names(song_id):
            if pin is None or self._pins.get(name) is pin:
                self._pins.pop(name, None)
                if pin is not None and pin.wrong is None and not self._expired(pin):
                    self._lapsed.pop(name, None)  # newest last: the oldest go first
                    self._lapsed[name] = pin
                    while len(self._lapsed) > MAX_PINS:
                        self._lapsed.pop(next(iter(self._lapsed)))

    def holds(self, track: Track) -> bool:
        """Whether the song has a link (under its native ID or its catalog track) that has
        not expired: kept, or dropped after it failed - a play's request is looking for a
        new link to its file then, which others of it wait for."""
        for name in (track.song_id, track.ref):
            if not name:
                continue
            if self.pinned(name) is not None:
                return True
            lapsed = self._lapsed.get(name)
            if lapsed is not None and not self._expired(lapsed):
                return True
            self._lapsed.pop(name, None)
        return False

    def in_library(self, track: Track) -> None:
        """The song's audio was put in the library: its links are forgotten - unless a
        client was sent its audio (``played``), or a request for it is being opened (a
        play whose first bytes are not there yet). Then they stay until they expire, so
        that a play going on keeps its file for its later ranges: the library's copy is
        another file (retagged, or copied out of a DASH link's)."""
        if self.played(track) or self._urgencies.get(track.key):
            return
        self.forget(track.song_id)
        if track.ref:  # its link from before its commit
            self.forget(track.ref)

    def known(self, song_id: str, ref: str | None = None) -> Known | None:
        """The representation the song was last played from (also under its catalog
        track, before its commit), when its format is known."""
        for key in (song_id, ref):
            found = self._known.get(key) if key else None
            if found is not None and found.kind is not None:
                return found
        return None

    def adopt(self, provisional: str, song_id: str) -> None:
        """A play under the catalog track (``provisional``) carries its pin and
        representation over to the song's native ID, so seeks continue from the same source.
        The provisional entry stays for requests still using it - and while the song is in
        use under it (a link, a routing going on, a request waiting for its next link), the
        two names are one song from here on (``_names``)."""
        in_use = (
            provisional in self._pins
            or provisional in self._published
            or self._resolving.held(provisional)
        )
        if provisional != song_id and in_use and self._provisional.get(song_id) != provisional:
            self._provisional.pop(song_id, None)  # newest last: the oldest go first
            self._provisional[song_id] = provisional
            self._committed[provisional] = song_id
            if len(self._provisional) > MAX_TWINS:
                self._trim_names()
        pin = self.pinned(provisional)
        if pin is not None and self.pinned(song_id) is None:
            pin.song_id = song_id
            self._remember(song_id, pin)
        known = self._known.get(provisional)
        if known is not None and song_id not in self._known:
            self._learned(song_id, known)

    def _trim_names(self) -> None:
        """Past MAX_TWINS the oldest pairs of names go - those not in use: a pair with a
        link, a routing going on or a request waiting stays one song (apart, its two names
        would have a lock and a link each)."""
        for native, ref in list(self._provisional.items()):
            if len(self._provisional) <= MAX_TWINS:
                break
            in_use = (
                self._resolving.held(ref)
                or any(name in self._published for name in (native, ref))
                or any(self.pinned(name) is not None for name in (native, ref))
            )
            if not in_use:
                del self._provisional[native]
                if self._committed.get(ref) == native:
                    del self._committed[ref]

    def _remember(self, song_id: str, pin: Pin) -> None:
        if (left_out := _left_out()) and (
            (current := self.pinned(song_id)) is not None and current.source.id in left_out
        ):
            # A download's link, while the song's link is at a source downloads do not use
            # (a play's): kept by the download alone - the play goes on with its own file.
            return
        for name in self._names(song_id):  # (under both its names, once it was committed)
            self._pins.pop(name, None)  # newest last: past MAX_PINS the oldest go first
            self._pins[name] = pin
            self._lapsed.pop(name, None)
            published = self._published.pop(name, None)
            if published is not None:  # requests waiting for the song's next link
                published.set()
        if len(self._pins) > MAX_PINS:
            for key in list(self._pins):  # ``pinned`` drops expired and rejected links
                self.pinned(key)
            while len(self._pins) > MAX_PINS:  # all still valid: the oldest go
                self._pins.pop(next(iter(self._pins)))

    def _learned(self, song_id: str, known: Known) -> None:
        if _left_out() or _LIBRARY_ACQUISITION.get():
            # (A download that left sources out: what a play of the song knows of its file
            # - perhaps at one of them - stays as it is.)
            return
        self._known.pop(song_id, None)
        self._known[song_id] = known
        while len(self._known) > 20000:
            self._known.pop(next(iter(self._known)))

    async def open(
        self,
        track: Track,
        range_header: str | None,
        *,
        head: bool = False,
        budget: float | None = None,
        purpose: str = "play",
        waited: float = 0.0,
        departure: Departure | None = None,
        resume: bool = False,
        heard: bool = True,
        download: bool = False,
        accept: Accept | None = None,
    ) -> Opened:
        """``purpose``: "play", "warm", "ahead" (a client's fetch ahead) or "alone"
        (a fetch ahead that waited out its turn: the primary alone, no fallbacks).
        ``waited``: how long the client has waited already (its turn): the wait cap counts
        it. ``departure``: set by the caller when the client leaves before its answer.
        ``resume``: continue the song's routing that ended without audio a moment ago (a
        download-first fetch whose request now streams the source format) - the source
        its wait cap cut short first, then those it did not try - instead of a new one.
        ``heard``: the audio goes to a client (False: it is fetched for the library - a
        download-first fetch, which is no play of the song). ``download``: a download-first
        fetch, which does not use the sources that ask not to be used for downloads
        (``not_for_downloads``). ``accept``: the request's own test of the audio at byte
        zero, by the format and bitrate its first bytes tell (None: it will do; else why
        not) - audio it refuses is passed over for the next source (``_Refused``)."""
        started = self.clock()
        trace = Trace(started - waited)
        if waited >= 0.5:
            trace.add(f"waited {waited:.1f}s for its turn")
        excluded = await self.not_for_downloads() if download else ()
        notes = tuple((s.id, f"{s.name}: not used for downloads ({why})") for s, why in excluded)
        for _, note in notes:
            trace.add(note)
        token = _TRACE.set(trace)
        unused = _NOT_FOR_DOWNLOAD.set(notes)
        tested = _ACCEPT.set(accept)
        keeps = _KEEPS.set(
            heard and not head and not download and purpose != "warm"
            and not _LIBRARY_ACQUISITION.get()
        )  # fmt: skip
        listener = heard and not download and not _LIBRARY_ACQUISITION.get()
        waited_on = _WAITED.set(listener)
        # (What it was for, in Diagnostics: a fetch for downloads or the library is no play.)
        shown = purpose if listener or purpose != "play" else "download"
        requested = ByteRange.parse(range_header)
        starts = not head and (requested is None or requested.at_zero)
        try:
            native: str | None = None
            ref = track.ref
            if ref is not None and ref != track.song_id and self.pinned(track.song_id) is None:
                # The song was played (or warmed) before its commit: that play continues,
                # with its routing and source - and a seek after its pin expired still only
                # accepts the representation it started with.
                joins = self.pinned(ref) is None and self._resolving.held(ref)
                # (Once the two names are one song, the request stays under its own: the
                # lock, the links and a shared failure are both names' already - and what
                # is known of the play it continues is kept under the native ID.)
                if joins and track.song_id not in self._provisional:
                    native, track = track.song_id, replace(track, song_id=ref)
                else:
                    self.adopt(ref, track.song_id)
            try:
                opened = await self._open(
                    track, range_header, head=head, budget=budget, purpose=purpose,
                    waited=waited, resume=resume,
                )  # fmt: skip
            except (NoSource, PinBroken) as exc:
                at_zero = requested is None or requested.at_zero
                self._no_audio(track, shown if at_zero else "seek", trace, exc, departure)
                if at_zero and isinstance(exc, NoSource) and not isinstance(exc, _Shared):
                    # The sources a download did not use are left to try: a stream
                    # continuing its routing (in the song's own format) may use them.
                    order = trace.left.order
                    order += [s.id for s, _ in excluded if s.id not in order]
                    self._left_over(track.song_id, trace.left)
                    if purpose != "warm":  # a client's request (its retry skips them)
                        self._sources_failed(track, trace.left)
                raise
            if native is not None and ref is not None:
                self.adopt(ref, native)
            if requested is None or requested.at_zero:
                self._failed_at.pop(track.key, None)  # it plays: a later routing tries all
        finally:
            _WAITED.reset(waited_on)
            _KEEPS.reset(keeps)
            _ACCEPT.reset(tested)
            _NOT_FOR_DOWNLOAD.reset(unused)
            _TRACE.reset(token)
            trace.ended_at = self.clock()
            trace.over.set()
        if starts:
            self._served(track, shown, trace, opened, departure)
        if heard and not head and purpose != "warm" and opened.status in (200, 206):
            self._heard.pop(track.key, None)  # a client was sent its audio: it is played
            self._heard[track.key] = None
            while len(self._heard) > 20000:
                self._heard.pop(next(iter(self._heard)))
        return opened

    def _served(
        self,
        track: Track,
        purpose: str,
        trace: Trace,
        opened: Opened,
        departure: Departure | None,
    ) -> None:
        """The source actually used, when a play (or warm-ahead) starts; with the steps
        before it when it was not the first source asked."""
        elapsed = self.clock() - trace.started
        self._noted(track, purpose, trace, opened.source or "?", elapsed, "")
        steps = f": {'; '.join(trace.steps)}" if trace.steps else ""
        if departure is not None and departure.at is not None:
            log.info(
                "no audio for %r (%s): the client left after %.1fs%s; the routing went on:"
                " first byte from %s after %.1fs",
                track.title,
                _PURPOSES.get(purpose, purpose),
                departure.at - trace.started,
                steps,
                opened.source or "?",
                elapsed,
            )
            return
        length = "" if opened.length is None else f", length {opened.length:.1f}s"
        log.info(
            "%s %r from %s (first byte after %.1fs%s%s)",
            {
                "play": "playing",
                "ahead": "fetched ahead",
                "alone": "fetched ahead",
                "download": "fetched",
            }.get(purpose, "warmed"),
            track.title,
            opened.source or "?",
            elapsed,
            length,
            steps,
        )

    def _no_audio(
        self,
        track: Track,
        purpose: str,
        trace: Trace,
        exc: NoSource | PinBroken,
        departure: Departure | None,
    ) -> None:
        """A play (or fetch ahead, warm-ahead, seek) that ends without audio: why, and each
        step with its source and time."""
        elapsed = self.clock() - trace.started
        why = (
            trace.ended
            or ("; ".join(exc.reasons) if isinstance(exc, NoSource) else str(exc))
            or "no source to try"
        )
        if departure is not None and departure.at is not None:
            why = f"the client left after {departure.at - trace.started:.1f}s; then {why}"
        if purpose != "seek":
            self._noted(track, purpose, trace, "", elapsed, why)
        steps = "; ".join(trace.steps) or "no source was asked"
        log.info(
            "no audio for %r (%s) after %.1fs: %s; %s",
            track.title,
            _PURPOSES.get(purpose, purpose),
            elapsed,
            why,
            steps,
        )

    def _noted(
        self, track: Track, purpose: str, trace: Trace, served: str, seconds: float, ended: str
    ) -> None:
        """A routing at byte zero, kept for Diagnostics (its steps name no links)."""
        self.recent.append(
            Routing(
                time.time() - seconds, track.title, track.artist, purpose, served, seconds,
                tuple(trace.steps), ended,
            )
        )  # fmt: skip

    async def _open(
        self,
        track: Track,
        range_header: str | None,
        *,
        head: bool,
        budget: float | None,
        purpose: str,
        waited: float = 0.0,
        resume: bool = False,
    ) -> Opened:
        with self._urgent(track, purpose, head):
            return await self._open_capped(
                track, range_header, head=head, budget=budget, purpose=purpose,
                waited=waited, resume=resume,
            )  # fmt: skip

    @contextlib.contextmanager
    def _urgent(self, track: Track, purpose: str, head: bool) -> Iterator[None]:
        """The request's urgency at the add-ons' limits, for all its add-on work: the
        caller's when it set one (a queued download), else by what the request is - the
        song being played (a play, a seek) first, then a client's fetches ahead and its
        probes (a HEAD), then warm-ahead. Requests for the same song already under way
        become as urgent as this one: a play that waits for its own warm-ahead's routing
        does not wait behind other background work with it."""
        mine = pacing.current()
        if mine is None:
            level = _URGENCIES.get(purpose, pacing.PLAY)
            mine = pacing.Urgency(max(level, pacing.QUEUED) if head else level)
        under_way = self._urgencies.setdefault(track.key, [])
        for other in under_way:
            other.raise_to(mine.level)
        under_way.append(mine)
        try:
            with pacing.urgent(mine):
                yield
        finally:
            under_way.remove(mine)
            if not under_way and self._urgencies.get(track.key) is under_way:
                del self._urgencies[track.key]

    async def not_for_downloads(self) -> tuple[tuple[Source, str], ...]:
        """The enabled sources a download-first fetch does not use, in their order, each
        with why: those whose manifest asks not to be used for downloads
        (``allowDownloads``), and those whose manifest is not known (what they ask is not
        known either; their attempt would need it): not read yet while they cool down, or
        not readable within the availability checks' timeout. Each manifest is read once
        (``Addon.manifest``), all at the same time."""
        sources = await self.registry.streaming()
        why: dict[int, str] = {}
        unknown = "its manifest could not be read"

        async def look(source: Source) -> None:
            manifest = source.addon.known
            if manifest is None and self.registry.cooling(source.id):
                why[source.id] = "it cools down, and its manifest is not known"
                return
            why[source.id] = unknown  # until read (also when the time runs out)
            try:
                manifest = manifest or await source.addon.manifest()
            except Exception:
                return
            if manifest.downloads:
                del why[source.id]
            else:
                why[source.id] = "the add-on asks this"

        with anyio.move_on_after(max(0.1, self.settings.availability_timeout_seconds)):
            async with anyio.create_task_group() as group:
                for source in sources:
                    group.start_soon(look, source)
        return tuple((s, why[s.id]) for s in sources if s.id in why)

    def played(self, track: Track) -> bool:
        """Whether the song was played from the add-ons, as far as is remembered: a client
        was sent its audio (before or after its commit). A probe (HEAD), a warm-ahead or a
        download-first fetch leaves a link and what is known of the file, but is no play
        (a download that leaves add-ons out leaves neither while the song's link is at one
        of them)."""
        return track.key in self._heard

    def promote(self, key: str) -> None:
        """The song ``key`` is the one being played now (the client's report, its saved
        queue): requests for it under way - a fetch ahead, a warm-ahead - go first at the
        add-ons' limits from now on."""
        for urgency in self._urgencies.get(key, ()):
            urgency.raise_to(pacing.PLAY)

    async def _open_capped(
        self,
        track: Track,
        range_header: str | None,
        *,
        head: bool,
        budget: float | None,
        purpose: str,
        waited: float = 0.0,
        resume: bool = False,
    ) -> Opened:
        requested = ByteRange.parse(range_header)
        if (requested is None or requested.at_zero) and budget is None:
            # A client waiting for its first byte waits at most max_wait_seconds in all,
            # including any wait for a routing another request (a warm-ahead) holds, and a
            # fetch ahead's wait for its turn.
            wait = max(0.5, self.settings.max_wait_seconds - waited)
            trace = _TRACE.get()
            if trace is not None:
                trace.capped = True
            # The sources' own deadlines end at the cap; this scope is a moment later, so
            # that a timeout at the cap is recorded as one (a primary's counts toward its
            # cooldown) before the client gets its answer.
            with anyio.move_on_after(wait + CAP_GRACE):
                return await self._route(
                    track, requested, head, budget, self.clock() + wait, purpose, resume
                )
            _stop(f"the wait cap was reached ({wait:g}s)")
            raise NoSource([f"no first byte within {wait:g}s"])
        return await self._route(track, requested, head, budget, None, purpose, resume)

    async def _route(
        self,
        track: Track,
        requested: ByteRange | None,
        head: bool,
        budget: float | None,
        cap: float | None,
        purpose: str,
        resume: bool = False,
    ) -> Opened:
        """Find a source and open the request. ``cap``: when a waiting client's first byte
        must have arrived, whatever the sources' own budgets."""
        since = self.clock()
        return await self._route_from(track, requested, head, budget, cap, purpose, since, resume)

    def _routing_failed(
        self, track: Track, requested: ByteRange | None, purpose: str, exc: NoSource
    ) -> None:
        """A play's full routing (not one its own wait cap cut short) ended without audio:
        requests for the song that waited meanwhile get this answer at once. Recorded before
        anything else can run (the song's lock is free already)."""
        if isinstance(exc, _Shared) or _left_out() or _ACCEPT.get() is not None:
            # (A download's routing that left sources out is no answer for a play, nor one
            # that tested the audio by its own terms.)
            return
        trace = _TRACE.get()
        capped = trace is not None and trace.ended.startswith("the wait cap")
        if (requested is None or requested.at_zero) and purpose == "play" and not capped:
            for name in self._names(track.song_id):  # (one song under both its names)
                self._failed[name] = (self.clock(), list(exc.reasons))
            if trace is not None:  # for requests whose link from it failed
                trace.shared = list(exc.reasons)
            if len(self._failed) > 4096:
                self._failed.clear()

    def _left_over(self, song_id: str, left: Leftover) -> None:
        """A routing at byte zero ended without audio: what it left to try, kept a moment
        for a request that continues it. A routing that tried nothing (it shared
        another's failure, or had no source) leaves the one before in place."""
        if not left.planned:
            return
        left.at = self.clock()
        self._leftovers.pop(song_id, None)  # newest last: the oldest go first
        self._leftovers[song_id] = left
        if len(self._leftovers) > 4096:
            now = self.clock()
            for key in [k for k, v in self._leftovers.items() if now - v.at > RESUME_SECONDS]:
                del self._leftovers[key]
            while len(self._leftovers) > 4096:
                self._leftovers.pop(next(iter(self._leftovers)))

    def _sources_failed(self, track: Track, left: Leftover) -> None:
        """A routing at byte zero ended without audio: the sources it is done with - those
        that failed or lacked the song, not those it gave up on while they may still be
        preparing it (cut short by the wait cap, a primary switched away from) - are kept
        for a while, so that a client's retry goes to the others."""
        if self.settings.retry_skip_seconds <= 0:
            return
        now = self.clock()
        record = self._failed_at.pop(track.key, {})  # newest last: the oldest go first
        for source_id in left.slow | ({left.cut} if left.cut is not None else set()):
            record.pop(source_id, None)
        for source_id in left.done - left.slow:
            record[source_id] = now
        self._failed_at[track.key] = record
        while len(self._failed_at) > 4096:
            self._failed_at.pop(next(iter(self._failed_at)))

    async def _skip_failed(self, track: Track, tried: set[int]) -> None:
        """The sources that failed for this song (or lacked it) a moment ago are not tried
        again (``tried``) until the window has passed; logged once each."""
        recent = {s: ago for s, ago in self._recently_failed(track).items() if s not in tried}
        if not recent:
            return
        tried |= set(recent)
        skipped = [
            f"{s.name} {recent[s.id]:.0f}s ago"
            for s in await self.registry.streaming()
            if s.id in recent
        ]
        if skipped:
            _step(f"not asked again yet (failed for this song): {', '.join(skipped)}")

    def _recently_failed(self, track: Track) -> dict[int, float]:
        """The sources that failed for this song (or lacked it) within the retry window, and
        how long ago."""
        window = self.settings.retry_skip_seconds
        record = self._failed_at.get(track.key)
        if window <= 0 or not record:
            return {}
        now = self.clock()
        return {s: now - at for s, at in record.items() if now - at <= window}

    def _resumable(self, song_id: str) -> Leftover | None:
        left = self._leftovers.get(song_id)
        if left is None or self.clock() - left.at > RESUME_SECONDS:
            return None
        return left

    async def _first_done(self, left: Leftover) -> bool:
        """The routing ``left`` came from is done with the plan's first add-on (it failed or
        lacked the song; not cut short)."""
        for source in await self.registry.streaming():
            return source.id in left.done
        return False

    async def _left_plan(self, left: Leftover) -> tuple[list[Source], dict[int, str]]:
        """A continued routing's order: the source the wait cap cut short while it was
        preparing the song, then those that said they were preparing it - in the routing's
        order -, then those the routing did not try (in its order); never again those it is
        done with (they failed or lacked the song a moment ago)."""
        enabled = {s.id: s for s in await self.registry.streaming()}
        unordered = sorted(i for i in left.preparing if i not in left.order)
        preparing = unordered + [i for i in left.order if i in left.preparing]
        order = ([left.cut] if left.cut is not None else []) + preparing + left.order
        plan: list[Source] = []
        for source_id in order:
            source = enabled.get(source_id)
            if source is not None and source_id not in left.done and source not in plan:
                plan.append(source)
        return plan, dict(left.hints)

    def _shared_failure(self, song_id: str, since: float) -> None:
        """Another request's routing of this song failed while this one waited for it: the
        same answer, at once (repeating it would double the client's wait)."""
        failed = self._failed.get(song_id)
        if failed is not None and failed[0] >= since:
            _stop("a routing of this song failed a moment ago (its answer is shared)")
            raise _Shared(failed[1])

    async def _route_from(
        self,
        track: Track,
        requested: ByteRange | None,
        head: bool,
        budget: float | None,
        cap: float | None,
        purpose: str,
        since: float,
        resume: bool = False,
    ) -> Opened:
        """``_routing`` with a task group for a play's availability checks (``_run_plan``):
        they may outlast the step they began in, and order the steps left once they answer -
        until the routing ends."""
        error: Exception | None = None
        opened: Opened | None = None
        try:
            async with anyio.create_task_group() as checking:
                try:
                    opened = await self._routing(
                        track, requested, head, budget, cap, purpose, since, resume, checking
                    )
                except Exception as exc:  # raised as itself, not as a group
                    error = exc
                    if isinstance(exc, NoSource):  # before the group's exit lets others run
                        self._routing_failed(track, requested, purpose, exc)
                finally:
                    checking.cancel_scope.cancel()
        except BaseException:
            # Canceled while the group ended (the client's wait cap): no response leaks.
            if opened is not None:
                with anyio.CancelScope(shield=True):
                    await opened.close()
            raise
        if error is not None:
            raise error
        assert opened is not None
        return opened

    async def _routing(
        self,
        track: Track,
        requested: ByteRange | None,
        head: bool,
        budget: float | None,
        cap: float | None,
        purpose: str,
        since: float,
        resume: bool,
        checking: anyio.abc.TaskGroup,
    ) -> Opened:
        at_zero = requested is None or requested.at_zero
        if budget is None:
            budget = await self._walk_seconds() if at_zero else self.settings.seek_timeout_seconds
        # ``loose``: when the routing's budget ends if no wait cap ends it first - a source
        # the cap takes more than a moment of that from is cut short, not failed.
        loose = self.clock() + budget
        deadline = _earliest(loose, cap)
        if not at_zero and cap is None:
            cap = deadline  # a seek waits at most its timeout, add-ons' own budgets too
        # A download-first fetch: not the sources that ask not to be used for downloads -
        # nor a link found at one of them.
        excluded = _left_out()
        tried: set[int] = set(excluded)
        failures: list[str] = [note for _, note in _NOT_FOR_DOWNLOAD.get()]

        def pinned() -> Pin | None:
            found = self.pinned(track.song_id)
            return None if found is not None and found.source.id in excluded else found

        pin = pinned()
        plan: tuple[list[Source], dict[int, str]] | None = None
        resolved = False  # the pin was just resolved by a resumed routing
        left = None
        if resume and pin is None and at_zero:
            left = self._resumable(track.song_id)
            if left is not None and purpose == "alone":
                # A fetch ahead that waited out its turn goes to the first add-on alone:
                # not again when that routing is done with it, only when it was cut short.
                if await self._first_done(left):
                    _stop(
                        "a fetch ahead that waited out its turn: the first add-on could not"
                        " serve it a moment ago"
                    )
                    raise NoSource(["the first add-on could not serve it a moment ago"])
                left = None
        if left is None and pin is None and at_zero:
            # A retry of a song that failed a moment ago: not the sources that failed for it
            # (or lacked it) then - the others; after the window, all again.
            await self._skip_failed(track, tried)
        fetching_ahead = left is None and pin is None and at_zero and purpose in ("ahead", "alone")
        ahead = await self._ahead_plan(track, tried) if fetching_ahead else None
        alone = fetching_ahead and purpose == "alone"
        if ahead is None and alone:
            # It waited out its turn: the first add-on alone, which cannot serve it now
            # (cooling down, or it lacks the song) - no others side by side with the
            # client's other fetches ahead.
            _stop("a fetch ahead that waited out its turn: the first add-on cannot serve it")
            raise NoSource(["the first add-on cannot serve it now"])
        if ahead is not None:
            # A client's fetch ahead of the song it plays: the plan's first add-on alone,
            # without availability checks; if it cannot serve the song, the song is routed
            # as usual below, one add-on at a time (it is not asked again) - unless the fetch
            # waited out its turn ("alone": no others, so fetches ahead never run side by
            # side).
            first = await self._ahead_first(
                track, requested, head, cap, failures, ahead, alone=purpose == "alone"
            )
            if isinstance(first, Pin):
                # Another request's routing found the song while this one waited for it:
                # its link, on the terms that routing gave it.
                pin = first
            elif first is not None:
                return first
            elif purpose == "alone":
                raise NoSource(failures)
            else:
                tried.add(ahead.id)  # its attempt recorded what it left (_try_one)
                loose = self.clock() + budget
                deadline = _earliest(loose, cap)
        races = at_zero and purpose == "play" and _WAITED.get()
        if left is None and pin is None and races:
            # Another request routes the song now: its link as soon as it has one (a step
            # of one add-on publishes it before its first byte, as the request reads it side
            # by side), else its lock (``_run_plan`` below; a race publishes its winner).
            key = self._provisional.get(track.song_id, track.song_id)
            holding = self.clock()
            while pin is None and self._resolving.held(key):
                published = self._published.setdefault(track.song_id, anyio.Event())
                with anyio.move_on_after(0.1):
                    await published.wait()
                pin = pinned()
                if not published.is_set() and self._published.get(track.song_id) is published:
                    del self._published[track.song_id]  # (only a link sets it)
            if self.clock() - holding >= 0.5:
                _step(
                    f"waited {self.clock() - holding:.1f}s for another request's routing of"
                    " this song"
                )
        if left is not None:
            # The song's routing that ended without audio a moment ago, continued.
            async with self._routing_lock(track.song_id):
                pin = pinned()
                if pin is None:
                    self._shared_failure(track.song_id, since)
                    left = self._resumable(track.song_id) or left  # the newest, after the wait
                    loose = self.clock() + budget
                    deadline = _earliest(loose, cap)
                    plan = await self._left_plan(left)
                    tried |= left.done
                    names = ", ".join(s.name for s in plan[0]) or "nothing left to try"
                    _step(f"continuing its routing of {self.clock() - left.at:.1f}s ago: {names}")
                    if not plan[0]:
                        failures.append(
                            "nothing left to try: each source failed or lacked it a moment ago"
                        )
                    pin = await self._resolve_held(
                        track, deadline, tried, failures, plan=plan, cap=cap
                    )
                    resolved = True
        elif pin is None and races:
            # A song someone waits for: the plan, the add-ons of a step together. One routing
            # per track at a time: a concurrent request (the play a warm-ahead prepares,
            # HEAD and GET) waits for it and uses its link instead of asking again.
            holding = self.clock()
            async with self._routing_lock(track.song_id):
                waited = self.clock() - holding
                if waited >= 0.5:
                    _step(f"waited {waited:.1f}s for another request's routing of this song")
                pin = pinned()
                if pin is None:
                    self._shared_failure(track.song_id, since)
                    await self._skip_failed(track, tried)  # also a routing waited for
                    # The availability checks may outlast a step (they run in ``checking``).
                    opened = await self._run_plan(
                        track, requested, head, tried, failures, cap, checking
                    )
                    if opened is not None:
                        return opened
                    raise NoSource(failures)
        known = None if at_zero else self._known.get(track.song_id)
        if known is not None and pin is not None and not _holds(pin, known):
            pin = None  # another request's link to another file: the play looks for its own
        foreign: list[Pin] = []  # links found to be other files than a continuing play's
        renewed = False  # the deadline renewed once for another routing's pin
        owner: Trace | None = None  # the routing whose link failed for this request too
        failed_link: Pin | None = None
        while True:
            fresh = pin is None or resolved
            resolved = False
            if pin is None and owner is not None:
                # Another request's routing found the link that failed here: it goes on to
                # its next source - its link or its answer, not a routing of this one's.
                until = cap if cap is not None else deadline
                pin = await self._after(owner, track, until, tried, failed_link)
                owner = None
                renewed = False  # the wait used this request's budget again
                fresh = pin is None
                if pin is None:  # routing on its own: a budget of its own, once
                    loose = max(loose, self.clock() + budget)
                    deadline = _earliest(loose, cap)
            if pin is None and at_zero and purpose == "alone":
                # A fetch ahead that waited out its turn and used another request's link,
                # which failed: the first add-on alone, never the others.
                _stop("a fetch ahead that waited out its turn: the link it used failed")
                raise NoSource(failures or ["the link it used failed"])
            if pin is None:
                if known is not None:
                    # Continuing a play whose pin expired (a paused song): the source it
                    # started from first, then the others.
                    plan = await self._continuing(known, tried)
                try:
                    pin = await self._resolve(
                        track, deadline, tried, failures, plan=plan, cap=cap, since=since,
                        own=None if known is None else known.source_id, holding=known,
                        foreign=foreign,
                    )  # fmt: skip
                except NoSource as exc:
                    if known is not None:
                        raise PinBroken(str(exc)) from None
                    raise
            # A link found while this request ran - by its own routing (``fresh``) or by
            # another request's it waited for - has the terms that routing gave it: its
            # step's time. One pinned before the request has its step's time from now.
            new = fresh or pin.created >= since
            if pin.by is not _TRACE.get():
                # Another routing found it: this request waited, which used up its own
                # budget - its first byte gets a budget of its own, once.
                fresh = False  # that routing's steps, stats and log line
                if at_zero and not renewed:
                    renewed = True
                    loose = max(loose, self.clock() + budget)
                    deadline = _earliest(loose, cap)
            if new and pin.first_byte_by is not None and not (pin.delivered and not fresh):
                # Its step's time, for resolution and first byte. (Not for a link whose
                # audio came already, at another request's routing - a step of the plan
                # keeps its link only once its audio is there: this request waited for that
                # and gets its step's time from now, below.)
                limit = pin.first_byte_by
                wanted = limit if pin.own_by is None else pin.own_by
                loose, deadline = max(loose, wanted), max(deadline, limit)
            elif at_zero:
                step = self._seconds(pin.source)
                limit = min(deadline, self.clock() + step)
                wanted = min(loose, self.clock() + step)
            else:
                limit = wanted = deadline
            asked = self.clock()
            # The wait cap took more than a moment of the time it had (its own budget, or
            # its share): cut short - it may still be preparing the song.
            capped = cap is not None and wanted - limit > CUT_SECONDS
            try:
                if pin.wrong is not None:  # rejected by a request at once: not asked again
                    raise _Wrong(pin.wrong)
                if at_zero and pin.checked:  # what its first bytes tell is known already
                    _refuse(pin.kind, pin.kbps)
                ask = self._probed(pin, requested, head)
                # Another request's race published the link before its audio: this request
                # leaves it once that race goes on without it (``_abandon``: another add-on's
                # audio came first, or the link failed) and follows the race.
                watched = pin.watchers if pin.by is not _TRACE.get() else None
                leaving = anyio.CancelScope()
                if watched is not None:
                    watched.append(leaving)
                    if pin.gone:
                        leaving.cancel()
                answered: Opened | None = None
                # (On a race's link, as the race's own request: until the client's wait.)
                # (Only a play an app waits on: not a download or a fetch ahead on it.)
                playing = at_zero and purpose == "play" and _WAITED.get()
                longest = (
                    None
                    if watched is None or cap is None or not playing
                    else partial(_until_started, pin, cap, self.clock)
                )
                try:
                    with leaving, pacing.lasting(longest):
                        # A continuing play: only the file it started with will do - checked
                        # on the link's answer, for this request alone (the link may be a new
                        # play's, to another file; what is known of it may change while this
                        # one waits).
                        answered = await self._request(pin, ask, head, limit, known, mine=fresh)
                finally:
                    if watched is not None and leaving in watched:
                        watched.remove(leaving)
                if answered is None:
                    raise _Left
                opened = answered
                opened = await self._verify(pin, opened, track, ask, limit, cap)
                if at_zero:
                    opened = await _tested(pin, opened)
                opened = _narrowed(opened, ask, requested, pin.size)
                self._delivered(pin, opened, asked)
                if pin.song_id != track.song_id and opened.status in (200, 206):
                    # A link kept under both of the song's names (``_names``), found under
                    # the other one: what this request played from is known under its own
                    # name too - its next seek is held to this file, not to the one an
                    # earlier play under this name started with. (What the other name
                    # knows, and a seek going on has read, stay as they are.)
                    self._learned(
                        track.song_id,
                        Known(pin.size, pin.etag, pin.source.id, pin.track_id, pin.kind, pin.kbps),
                    )
                if fresh and at_zero:  # the attempt's record (its waits at the limits out)
                    took = pin.looked_before + pin.lookup_seconds + pin.link_seconds
                    took += self.clock() - asked - opened.waited
                    self._record(pin.source, pin.answer, True, took)
                trace = _TRACE.get()
                if fresh and at_zero and trace is not None and trace.steps:
                    _step(
                        f"{pin.source.name}: lookup {pin.lookup_seconds:.1f}s, link"
                        f" {pin.link_seconds:.1f}s, first byte {self.clock() - asked:.1f}s"
                    )
                if known is not None and fresh:
                    log.info(
                        "continuing %r at %s with a new link to the same file",
                        track.title,
                        pin.source.name,
                    )
                return opened
            except _Left:
                _step(f"{pin.source.name}: left after {self.clock() - asked:.1f}s, another"
                      " request's routing went on without it")  # fmt: skip
                # Its DASH join, which that race let go while this request still waited for
                # it, stops now unless yet another request waits for it.
                self._let_go(pin)
                if pin.by is not None:
                    owner, failed_link = pin.by, pin
                pin = None
                continue
            except _Other as exc:
                # Another request's link, to another file: left alone; the play's own file
                # is looked for.
                failures.append(f"{pin.source.name}: {exc}")
                foreign.append(pin)
                pin = None
            except _Changed as exc:
                if at_zero and (by := self._routing_on(pin, since)) is not None:
                    owner, failed_link = self._borrowed(pin, exc, failures, asked, by), pin
                    pin = None
                    continue
                self.forget(track.song_id, pin)
                failures.append(f"{pin.source.name}: {exc}")
                if new:  # counted once per link
                    self._link_failed(pin, exc, known is None)
                if fresh and at_zero:  # the attempt's record
                    took = pin.looked_before + pin.lookup_seconds + pin.link_seconds
                    took += self.clock() - asked - exc.waited
                    self._record(pin.source, pin.answer, False, took)
                if at_zero:
                    _failed_step(
                        f"{pin.source.name}: {exc} (lookup {pin.lookup_seconds:.1f}s, link"
                        f" {pin.link_seconds:.1f}s, then {self.clock() - asked:.1f}s)"
                    )
                    log.info(
                        "representation changed at %s (%s); re-resolving", pin.source.name, exc
                    )
                elif known is None:
                    raise PinBroken(str(exc)) from None
                elif new:  # a new link without the play's representation: not here
                    tried.add(pin.source.id)
                # Otherwise the play's own link expired: its source gets a fresh one first.
                pin = None  # the same source may give a fresh URL; it is not excluded
            except _Failed as exc:
                # A timeout is this request's own (its share of the budget), not the link's:
                # it routes on as before; another recording or an error is the link's. So is
                # audio this request refused (``accept``): the link stays for others.
                link = not isinstance(exc, (_TimedOut, _Refused))
                if at_zero and link and (by := self._routing_on(pin, since)) is not None:
                    owner, failed_link = self._borrowed(pin, exc, failures, asked, by), pin
                    pin = None
                    continue
                if not _kept(exc):  # (not asked for in time, still joining, refused: it stays)
                    self.forget(track.song_id, pin)
                if new:  # a link found for this request: its error counts, once a link
                    self._failure(pin.source, exc, pin)
                    if fresh and at_zero:
                        self._count_timeout(pin.source, exc)
                elif not isinstance(exc, (_Wrong, _Unusable, _Paced, _Cooling, _Refused)):
                    self.registry.failed(pin.source, str(exc))  # its old link (not the add-on's)
                if fresh and at_zero and not _spared(exc):  # the attempt's record
                    took = pin.looked_before + pin.lookup_seconds + pin.link_seconds
                    took += self.clock() - asked - exc.waited
                    self._record(pin.source, pin.answer, False, took)
                failures.append(f"{pin.source.name}: {exc}")
                (_step if isinstance(exc, _Refused) else _failed_step)(
                    f"{pin.source.name}: {exc} (lookup {pin.lookup_seconds:.1f}s, link"
                    f" {pin.link_seconds:.1f}s, then {self.clock() - asked:.1f}s)"
                )
                if not at_zero:
                    # (Held up at the add-on's limits, or its join goes on: its link is
                    # good - no new one is asked for, the request's time is over.)
                    if known is None or _kept(exc):
                        raise PinBroken(str(exc)) from None
                    if not new:  # the play's own link failed: its source gets a fresh one
                        pin = None
                        continue
                _done(pin.source, cut=capped and isinstance(exc, _TimedOut), slow=_unfinished(exc))
                tried.add(pin.source.id)
                pin = None

    def _routing_on(self, pin: Pin, since: float) -> Trace | None:
        """The routing that found ``pin``, when it is another request's - a client's, still
        going on: it will go on to its next source when the link fails; or one that
        ended without audio, since this request began, with an answer shared with requests
        that waited."""
        by = pin.by
        if by is None or by is _TRACE.get():
            return None
        if by.over.is_set():
            return by if by.shared is not None and by.ended_at >= since else None
        return by if by.capped else None

    def _borrowed(
        self, pin: Pin, exc: Exception, failures: list[str], asked: float, by: Trace
    ) -> Trace:
        """Another request's link failed here too: noted; the routing that found it counts
        it and goes on (this request does not forget its pin or count it)."""
        failures.append(f"{pin.source.name}: {exc}")
        _step(f"{pin.source.name}: {exc} (another request's link, then"
              f" {self.clock() - asked:.1f}s)")  # fmt: skip
        return by

    async def _after(
        self, owner: Trace, track: Track, until: float, tried: set[int], failed: Pin | None
    ) -> Pin | None:
        """Wait (until ``until``) for another request's routing whose link (``failed``)
        failed here: its next link as soon as it has one, or its answer when it ended
        without audio; None when it is still going on at ``until``, or ended without a link
        this request may use (it routes then)."""
        began = self.clock()
        while True:
            pin = self.pinned(track.song_id)
            if pin is not None and pin is not failed and pin.source.id not in tried:
                break
            pin = None
            if owner.over.is_set() or self.clock() >= until:
                break
            if len(self._published) > 4096:  # its waiters wake, look, and wait again
                for waited in self._published.values():
                    waited.set()
                self._published.clear()
            published = self._published.setdefault(track.song_id, anyio.Event())
            with anyio.move_on_after(max(0.0, until - self.clock())):
                await _either(owner.over, published)
        if self.clock() - began >= 0.5:
            _step(f"waited {self.clock() - began:.1f}s for the routing that found that link")
        if pin is not None:
            return pin
        if owner.over.is_set() and owner.shared is not None:
            if failed is not None:
                self.forget(track.song_id, failed)  # a link kept by mistake: not the song's
            _stop("the routing that found that link ended without audio (its answer is shared)")
            raise _Shared(owner.shared)
        return None

    async def _ahead_first(
        self,
        track: Track,
        requested: ByteRange | None,
        head: bool,
        cap: float | None,
        failures: list[str],
        first: Source,
        *,
        alone: bool = False,
    ) -> Opened | Pin | None:
        """A fetch ahead at the plan's first add-on alone, as an attempt of a play (its
        timeouts count toward its cooldown, a delivery resets it); None when it cannot
        serve the song; the pin another request's routing of the song found while this one
        waited for it (it is used)."""
        async with self._routing_lock(track.song_id):
            if (pinned := self.pinned(track.song_id)) is not None:
                return pinned
            if first.id in self._recently_failed(track):  # failed for it meanwhile
                failures.append(f"{first.name}: failed for this song a moment ago")
                return None
            opened = await self._try_one(first, track, requested, head, failures, cap)
        if opened is None:
            log.info(
                "fetch ahead %r: %s could not serve it (%s); %s",
                track.title,
                first.name,
                failures[-1] if failures else "no answer",
                "no others: it waited out its turn" if alone else "routing it as usual",
            )
        return opened

    async def _ahead_plan(self, track: Track, tried: set[int]) -> Source | None:
        """The plan's first add-on that can be asked for this song now: on, not cooling
        down, not failed for it a moment ago (``tried``), nor known to lack it or to have
        another recording; None: none (the fetch ahead is routed as usual, still one at a
        time)."""
        for source in await self.registry.streaming():
            if self.registry.cooling(source.id) or source.id in tried:
                continue
            if self._lacks(source, track) or self._known_wrong(source, track):
                continue
            return source
        return None

    def _seconds(self, source: Source) -> float:
        """The add-on's time when it is asked alone (a download, a song prepared ahead, a
        fetch ahead, a routing continued): its own, else the installation's."""
        return source.budget if source.budget is not None else self.settings.budget_seconds

    def _step_seconds(self, step: list[Source]) -> float:
        """A step's time in a play: when the next step's add-ons join, the longest of its
        add-ons' times (each its own, else the installation's), as the dashboard shows it."""
        fallback = self.settings.budget_seconds
        times = [s.step_seconds if s.step_seconds is not None else fallback for s in step]
        return max(times) if times else fallback

    def _slow_now(
        self, source: Source, trying: float, step_seconds: float, last: bool = False
    ) -> bool:
        """Whether an add-on stopped after ``trying`` seconds (its waits at its own limits
        left out), because another's audio came first, counts a timeout: past its step's
        time and ``slow_after`` times its usual time to first audio (with none known, past
        its step's time alone). The last step has no time of its own: there the default
        step time (``budget_seconds``) counts as its time."""
        if last:
            step_seconds = self.settings.budget_seconds
        if trying <= step_seconds:
            return False
        usual = usual_seconds(source.stats)
        if usual is None:
            return True
        factor = source.slow_after if source.slow_after is not None else self.settings.slow_after
        return trying > factor * usual

    async def _walk_seconds(self) -> float:
        """A request's time when it asks the add-ons one at a time: each one's own time
        (not those cooling down)."""
        sources = [s for s in await self.registry.streaming() if not self.registry.cooling(s.id)]
        return sum(self._seconds(s) for s in sources) or self.settings.budget_seconds

    async def _continuing(
        self, known: Known, tried: set[int]
    ) -> tuple[list[Source], dict[int, str]]:
        """A continuing play's order: the source it started from (with its track ID), then
        the others; each must deliver the same representation. Without a size or an ETag to
        recognize it by, only the source it started from."""
        enabled = await self.registry.streaming()
        order = [s for s in enabled if s.id == known.source_id]
        if known.size is not None or known.etag is not None:
            order += [s for s in enabled if s.id != known.source_id]
        return [s for s in order if s.id not in tried], {known.source_id: known.track_id}

    async def _resolve(
        self,
        track: Track,
        deadline: float,
        tried: set[int],
        reasons: list[str],
        *,
        plan: tuple[list[Source], dict[int, str]] | None = None,
        cap: float | None = None,
        since: float | None = None,
        own: int | None = None,
        holding: Known | None = None,
        foreign: list[Pin] | None = None,
    ) -> Pin:
        """``holding``: a continuing play's file - the song's link is used only when it
        links that file (another request may have replaced it with another file meanwhile:
        the play looks for its own, never switching files); ``foreign``: links the
        request found to be other files."""
        # Concurrent first requests for a track share one resolution.
        async with self._routing_lock(track.song_id):
            pin = self.pinned(track.song_id)
            mine = pin is not None and all(pin is not other for other in foreign or ())
            if pin is not None and pin.source.id not in tried and mine and _holds(pin, holding):
                return pin
            if since is not None and not tried:
                self._shared_failure(track.song_id, since)
            return await self._resolve_held(
                track, deadline, tried, reasons, plan=plan, cap=cap, own=own
            )

    async def _resolve_held(
        self,
        track: Track,
        deadline: float,
        tried: set[int],
        reasons: list[str],
        *,
        plan: tuple[list[Source], dict[int, str]] | None = None,
        cap: float | None = None,
        own: int | None = None,
    ) -> Pin:
        """``_resolve`` for a caller that holds the track's resolution lock: the add-ons one
        at a time, in the plan's order (``plan``: another order, with add-on track IDs
        known), each within its step's time and ``deadline``. ``cap``: when a waiting
        client's first byte must have arrived, whatever the steps' times."""
        enabled = [s for s in await self.registry.streaming() if s.id not in tried]
        # A continuing play's own source (``own``) is asked while it cools down too:
        # only it has the play's file.
        candidates = [s for s in enabled if s.id == own or not self.registry.cooling(s.id)]
        trace = _TRACE.get()
        for source in enabled:
            if source not in candidates:
                note = f"{source.name}: cooling down"
                if trace is not None and note not in trace.steps:
                    trace.add(note)
        hints: dict[int, str] = {}
        if plan is not None:
            # The current source objects: the configuration may have changed meanwhile.
            current = {s.id: s for s in candidates}
            candidates, hints = [current[s.id] for s in plan[0] if s.id in current], plan[1]
        if trace is not None and not trace.left.planned:  # the plan, for a routing continuing it
            trace.left.planned = True
            trace.left.order = [s.id for s in candidates]
            trace.left.hints.update(hints)
        missing = failed = timeless = 0
        for source in candidates:
            if source.id != own and self._lacks(source, track):
                missing += 1
                _done(source)
                tried.add(source.id)
                reasons.append(f"{source.name}: did not have it (remembered)")
                _step(f"{source.name}: did not have it (remembered)")
                continue
            if cap is not None and cap <= self.clock():
                reasons.append(f"{source.name}: the wait ran out")
                _stop("the wait cap was reached")
                break
            remaining = deadline - self.clock()
            if remaining <= 0:
                reasons.append(f"{source.name}: time budget used up")
                _step(f"{source.name}: no time left")
                timeless += 1
                continue
            limit = min(self._seconds(source), remaining)
            if cap is None:
                # Nobody waits (warm-ahead): an add-on still preparing the song gets an equal
                # share of the routing's longer time, if that is more than its step's.
                left = len(candidates) - candidates.index(source)
                limit = max(limit, remaining / left)
            # The wait cap ends it, taking more than a moment of its step's time: cut short.
            capped = cap is not None and self._seconds(source) - (cap - self.clock()) > CUT_SECONDS
            if cap is not None:
                limit = max(0.01, min(limit, cap - self.clock()))
            started = self.clock()
            try:
                pin = await self._resolve_at(source, track, limit, hints.get(source.id), cap=cap)
            except _Missing as exc:
                missing += 1
                self._record(source, "-", False, self.clock() - started - exc.waited)
                _done(source)
                tried.add(source.id)  # not asked again by a later pass
                reasons.append(f"{source.name}: {exc}")
                _step(f"{source.name}: {exc} {self.clock() - started:.1f}s")
                if exc.absent:
                    self._missed(source, track)
                continue
            except _Failed as exc:
                failed += 1
                if not _spared(exc):
                    self._record(source, "-", False, self.clock() - started - exc.waited)
                _done(source, cut=capped and isinstance(exc, _TimedOut), slow=_unfinished(exc),
                      preparing=isinstance(exc, _Preparing))  # fmt: skip
                tried.add(source.id)  # not asked again by a later pass
                reasons.append(f"{source.name}: {exc}")
                if cap is not None:  # (a timeout nobody waited for is no reason to cool it)
                    self._count_timeout(source, exc)
                _failed_step(f"{source.name}: {exc} {self.clock() - started:.1f}s")
                continue
            pin.by = _TRACE.get()
            self._remember(track.song_id, pin)
            return pin
        failed += trace.failed if trace is not None else 0
        if not candidates:
            _stop("no source to try")
        elif failed == 0 and missing == 0 and timeless:
            _stop("the time was used up")
        elif failed == 0 and missing:
            rejected = any("a match was rejected" in r for r in reasons)
            _stop("no source had it" + (" (a match was rejected)" if rejected else ""))
        else:
            _stop("no source delivered")
        raise NoSource(reasons)

    async def _resolve_at(
        self,
        source: Source,
        track: Track,
        limit: float,
        hint: str | None = None,
        *,
        cap: float | None = None,
        looked: anyio.Event | None = None,
        before: float = 0.0,
        until: float | None = None,
    ) -> Pin:
        """Resolve ``track`` at one source within ``limit`` seconds. Raises ``_Missing``
        when the source does not have it and ``_Failed`` when the attempt failed (recorded
        in the source's diagnostics, except for an unsupported stream type). ``looked`` is
        set once its lookup has answered. ``before``: the seconds its lookup meanwhile took
        (its own budget counts from then). ``until``: when its first byte must come (an
        add-on in a play's race: the client's wait), instead of its own time. What the
        attempt waited at the add-on's request limit is noted with its outcome
        (``Pin.waited``, the exception's ``waited``)."""
        capacity = _BULK_LEASE.get()
        if _BULK_REUSE.get() or (
            capacity is not None and (not capacity.active or capacity.owner is not None)
        ):
            # Existing DASH admission covers joining its job, never a new preparation
            # or a replacement link which might open an independent transfer.
            raise _Reacquire("a replacement link needs its own upstream admission", resolve=True)
        with pacing.watched() as waits:
            try:
                pin = await self._resolved_at(
                    source, track, limit, hint, waits, cap=cap, looked=looked, before=before,
                    until=until,
                )  # fmt: skip
            except (_Missing, _Failed) as exc:
                exc.waited = waits.seconds
                raise
        pin.waited = waits.seconds
        return pin

    async def _resolved_at(
        self,
        source: Source,
        track: Track,
        limit: float,
        hint: str | None,
        waits: pacing.Waits,
        *,
        cap: float | None,
        looked: anyio.Event | None,
        before: float,
        until: float | None = None,
    ) -> Pin:
        if self._known_wrong(source, track):
            raise _Missing("another recording (remembered)")
        started = self.clock()
        notes: list[str] = []
        looked_up = started
        lacks = False  # an add-on for its catalog only: no audio to look up
        preparing: list[float] = []  # the waits for the add-on preparing the song
        try:
            with anyio.fail_after(limit):
                try:
                    lacks = "stream" not in (await source.addon.manifest()).resources
                    # A song of this add-on's own catalog carries the add-on's track
                    # ID: asked for by it, without a lookup.
                    own = None if hint or lacks else await _own_id(source, track, self.settings)
                    known = None if lacks else hint or own
                    track_id = (
                        None if lacks else known or await source.addon.find(track.wanted, notes)
                    )
                finally:
                    if looked is not None:
                        looked.set()
                looked_up = self.clock()
                try:
                    info = None if track_id is None else await _link(source, track_id, preparing)
                except AddonError as exc:
                    # (Its catalog's ID may be refused in other words than "not found".)
                    gone = ("unavailable", "expired", "invalid") if own else _GONE
                    if not known or exc.kind not in gone:
                        raise
                    # The ID it had before (or reported, or gave in its catalog) is gone:
                    # look the recording up.
                    track_id = await source.addon.find(track.wanted, notes)
                    if own and track_id == own:
                        raise  # (its catalog's ID again: asked for once, not twice)
                    info = None if track_id is None else await _link(source, track_id, preparing)
        except AddonError as exc:
            self._source_error(source, exc)
            if exc.kind == "unavailable":  # a refusal, or a resource it lacks: not "absent"
                raise _Missing(exc.reason) from None
            if exc.kind == "cooling":  # not asked (a rate limit's time has not passed)
                raise _Cooling(exc.reason) from None
            if exc.kind == "rate_limited":
                raise _Limited(exc.reason) from None
            if exc.kind == "timeout" and not exc.shared:  # slow, as audio's is (not one
                raise _TimedOut(exc.reason) from None  # shared: another request's)
            if exc.kind == "preparing":  # not ready yet, and not within the time left
                # Waited for it (asked again): its time is used up but for less than the next
                # wait. Never waited (the first answer named more than was left): on at once.
                waited_out = bool(preparing)
                if waited_out:
                    self.registry.failed(source, NOT_READY)
                why = _not_ready(preparing, exc.retry_after)
                raise _Preparing(why, waited_out=waited_out) from None
            raise (_Error if exc.kind in _ERRORS else _Failed)(exc.reason) from None
        except TimeoutError:
            # Its time ran out in the queue, or what the queue left of it: not the add-on's
            # doing (it never had the time).
            if (held := waits.held(CUT_SECONDS)) is not None:
                raise _Paced(held) from None
            if preparing:  # ... asked again while it prepares the song, until the end
                self.registry.failed(source, NOT_READY)
                raise _Preparing(_not_ready(preparing), waited_out=True) from None
            self.registry.failed(source, "timeout")
            raise _TimedOut("timeout") from None
        except Exception as exc:  # a broken add-on must not break playback
            reason = f"unexpected {type(exc).__name__}"
            log.warning("source %s failed: %s", source.name, reason)
            self._failure(source, _Error(reason))
            raise _Error(reason) from None
        if lacks:
            raise _Missing("add-on does not stream")
        if track_id is None or info is None:
            # Absent only when its lookup said so (a source that cannot look it up - no
            # ISRC lookup for a track without one, no /resolve, no search - lacks nothing).
            absent = track_id is None and await _looks_up(source, track)
            raise _Missing(f"not available ({notes[0]})" if notes else "not available",
                           absent=absent)  # fmt: skip
        if info.transport == "hls":
            raise _Failed("HLS streams are not supported")
        if info.transport == "dash" and self.dash is None:
            raise _Failed(f"DASH is off ({self.dash_off})")
        if waits.seconds >= 0.5:
            _step(f"{source.name}: waited {waits.seconds:.1f}s for its request limit")
        # A link is not yet a success: the source's first byte of audio is (``_request``).
        pin = Pin(track.song_id, source, track_id, info, self.clock())
        pin.seconds = track.duration_ms / 1000
        pin.lookup_seconds, pin.link_seconds = looked_up - started, self.clock() - looked_up
        # Its own time, from its lookup meanwhile (``before``), if any - or, with nobody
        # waiting (warm-ahead), the longer time its attempt was given; in a play's race,
        # until the client's wait (``until``).
        span = self._seconds(source) if cap is not None else max(self._seconds(source), limit)
        pin.own_by = until if until is not None else started - before + span
        pin.first_byte_by = _earliest(pin.own_by, cap)
        return pin

    async def _prepare(self, declined: list[Source], track: Track) -> None:
        """Ask the first source that said "not now" to prepare the recording for next time
        - the first that does not ask not to be used for downloads (preparing is fetching
        the song for later): in the background, after everything a listener waits for,
        within a budget's time. (Its answer "too many requests" leaves the add-on alone like
        any other: the client sees to it.)"""
        with pacing.urgent(pacing.WARM), anyio.move_on_after(self.settings.budget_seconds):
            for source in declined:
                try:
                    if not (await source.addon.manifest()).downloads:
                        continue
                    await source.addon.availability(track.wanted, prepare=True)
                except AddonError as exc:
                    self._source_error(source, exc)
                return

    async def _run_plan(
        self,
        track: Track,
        requested: ByteRange | None,
        head: bool,
        tried: set[int],
        failures: list[str],
        cap: float | None,
        checking: anyio.abc.TaskGroup,
    ) -> Opened | None:
        """A play's routing by the plan (``_race``): the add-ons of a step start together,
        and the next step's join them once the step has had its time, or once every add-on
        asked so far has ended without audio; those asked keep trying. The first audio
        plays. None: no audio (``failures`` say why).

        With ``ready_checks``, the add-ons that can tell are asked meanwhile whether they
        have the song ready (in ``checking``: the checks may outlast the step they began
        in). One that has joins after ``ready_handover_seconds`` without audio (0: the
        checks are waited for, and it goes first); the steps not begun are arranged by the
        answers: the add-ons that have it ready first, those that have not last, one at a
        time - a stream there most likely fails, and may start a preparation."""
        usable: list[Source] = []
        for source in await self.registry.streaming():
            if source.id in tried:
                continue
            if self.registry.cooling(source.id):
                _step(f"{source.name}: cooling down")
                continue
            if self._lacks(source, track):
                _done(source)
                tried.add(source.id)
                failures.append(f"{source.name}: did not have it (remembered)")
                _step(f"{source.name}: did not have it (remembered)")
                continue
            usable.append(source)
        if not usable:
            # Every add-on left cools down: they are asked all the same (one may deliver),
            # rather than no audio at once.
            cooling = [
                s
                for s in await self.registry.streaming()
                if s.id not in tried and self.registry.cooling(s.id) and not self._lacks(s, track)
            ]
            if cooling:
                _step("every add-on cools down: asked all the same")
                usable = cooling
        steps = _steps(usable)
        handover = _Handover(None)
        checks: _Checks | None = None
        if self.settings.ready_checks and len(usable) > 1:
            first = self.settings.ready_handover_seconds <= 0
            # The first step's add-ons start at once: only the others are asked - unless
            # the checks are waited for.
            asked = usable if first else [s for step in steps[1:] for s in step]
            checks = _Checks(asked)
            checking.start_soon(self._check_all, checks, track)
            if first:
                wait = self.settings.availability_timeout_seconds
                if cap is not None:
                    wait = min(wait, cap - self.clock())
                with anyio.move_on_after(max(0.0, wait)):
                    await checks.done.wait()
                steps = self._arranged(steps, checks)
            else:
                handover.at = self.clock() + self.settings.ready_handover_seconds
        trace = _TRACE.get()
        opened = await self._race(track, requested, head, steps, cap, failures, tried, checks,
                                  handover)  # fmt: skip
        if opened is not None:
            return opened
        if not usable:
            _stop("no source to try")
        elif trace is not None and trace.failed == 0:
            rejected = any("a match was rejected" in r for r in failures)
            _stop("no source had it" + (" (a match was rejected)" if rejected else ""))
        else:
            _stop("no source delivered")
        return None

    def _arranged(self, steps: list[list[Source]], checks: _Checks) -> list[list[Source]]:
        """The steps left, by the checks' answers: the add-ons that have the song ready
        first, together; those that have not last, one at a time."""
        ready = [s for step in steps for s in step if checks.ready(s)]
        said_no = [s for step in steps for s in step if checks.said_no(s)]
        rest = [[s for s in step if s not in ready and s not in said_no] for step in steps]
        arranged = ([ready] if ready else []) + [step for step in rest if step]
        arranged += [[s] for s in said_no]
        if arranged != steps:
            _step(
                "then, by the checks: "
                + "; ".join(", ".join(s.name for s in step) for step in arranged)
            )
        return arranged

    async def _check_all(self, checks: _Checks, track: Track) -> None:
        """Ask ``checks.sources`` whether they have the song ready; with none that has and
        ``prepare_when_not_ready``, one that has not is asked to prepare it for next time."""
        started = self.clock()

        async def ask(source: Source) -> None:
            if self._known_wrong(source, track):  # another recording there: not counted on
                checks.answers[source.id] = Availability(False)
                checks.asked.add(source.id)
                return
            try:
                with anyio.fail_after(self.settings.availability_timeout_seconds):
                    answer = await source.addon.availability(track.wanted)
                checks.answers[source.id] = answer
                if answer is not None and answer.available:
                    checks.asked.add(source.id)
                    checks.ready_seen.set()
            except AddonError as exc:  # it cannot tell
                if exc.kind == "rate_limited":  # ... and is left alone for a while
                    self._source_error(source, exc)
            except Exception as exc:  # timeouts and broken answers: it cannot tell
                log.debug("availability check of %s failed: %s", source.name, type(exc).__name__)
            checks.asked.add(source.id)

        try:
            async with anyio.create_task_group() as asking:
                for source in checks.sources:
                    asking.start_soon(ask, source)
        finally:
            checks.done.set()
        states = _states(checks.sources, checks.answers, checks.asked)
        _step(f"checks {self.clock() - started:.1f}s: {states}")
        if self.settings.prepare_when_not_ready and not any(
            checks.ready(s) for s in checks.sources
        ):
            declined = [s for s in checks.sources if checks.said_no(s)]
            if declined:
                self.background(lambda: self._prepare(declined, track))

    async def _race(
        self,
        track: Track,
        requested: ByteRange | None,
        head: bool,
        steps: list[list[Source]],
        cap: float | None,
        failures: list[str],
        tried: set[int],
        checks: _Checks | None,
        handover: _Handover,
    ) -> Opened | None:
        """A play's plan, its steps joining in turn: the add-ons of the first start
        together, and each next step's join them once the step before has had its time
        (``_step_seconds``), or at once when every add-on of the step before (or every one
        asked so far) has ended without audio. The add-ons asked keep trying, each until the
        client's wait cap (with none, its own time). The first whose audio comes plays - its
        link is kept for the song; the others are stopped: their links are not kept (the
        requests of others waiting on one published early leave it: ``Pin.watchers``), a
        DASH join one of them began stops unless another request waits for it, and one past
        its step's time and slow for its usual time (``_slow_now``) counts a timeout. With
        ``checks``, an add-on that has the song ready joins once the ``handover`` moment has
        passed, and the steps not begun are arranged by the answers. None: no audio."""
        begun = self.clock()
        pending = [list(step) for step in steps]
        scopes: dict[int, anyio.CancelScope] = {}
        starts: dict[int, float] = {}  # when each add-on asked began (its step's start)
        times: dict[int, float] = {}  # the time of each one's step
        order: list[Source] = []  # the add-ons asked, in the order they joined
        pins: dict[int, Pin] = {}
        # Links whose attempt ended without audio but that stay (``_kept``: a DASH join
        # going on, audio not asked for in time at the add-on's limits, audio this request
        # refused).
        kept: list[Pin] = []
        published: list[Pin] = []  # links made the song's before their audio came
        over: set[int] = set()  # attempts that ended by themselves
        first: list[tuple[Source, Pin, Opened]] = []
        nudge = [anyio.Event()]  # set when an attempt ends or audio comes
        own_waits: dict[int, pacing.Waits] = {}  # each one's waits at its own limits
        lasts: set[int] = set()  # the add-ons of the plan's last step
        latest: list[int] = []  # the add-ons of the step that joined last

        def running() -> list[Source]:
            return [s for s in order if s.id not in over]

        def unpublish(pin: Pin) -> None:
            """A link published early that is not the song's after all (it failed, or a
            fresh one replaces it): dropped at once - unless it stays (``kept``)."""
            if any(pin is other for other in published) and all(pin is not k for k in kept):
                published[:] = [other for other in published if other is not pin]
                self.forget(track.song_id, pin)
                _abandon(pin)

        def end(source: Source) -> None:
            over.add(source.id)
            nudge[0].set()

        async def run(source: Source) -> None:
            answer = checks.answer(source) if checks is not None else "-"
            started = self.clock()
            until = cap if cap is not None else started + self._seconds(source)
            # The wait cap ends it, taking more than a moment of its own time: cut short.
            capped = cap is not None and started + self._seconds(source) - cap > CUT_SECONDS
            hint = (checks.hints() if checks is not None else {}).get(source.id)
            relinked = False
            # In a play an app waits on, its requests for the song may take until the app
            # stops waiting, beyond its client's timeout: it tries many in parallel, and its
            # own time is a download's. Only until the song starts: then (its DASH join's
            # later segments) the client's own timeout again.
            longest = None if cap is None else (lambda: 0.0 if first else cap - self.clock())
            with scopes[source.id], pacing.watched() as waits, pacing.lasting(longest):
                own_waits[source.id] = waits
                try:
                    while True:
                        begun_link = self.clock()
                        pin = await self._resolve_at(
                            source, track, max(0.01, until - begun_link), hint, cap=cap,
                            until=until,
                        )  # fmt: skip
                        pins[source.id] = pin
                        if not first and all(s is source for s in running()):
                            # Nothing to race (yet): the link is the song's at once, so
                            # requests waiting for it read it side by side (it is dropped
                            # again when its audio does not come, and another's audio that
                            # comes first takes its place).
                            pin.by = _TRACE.get()
                            pin.watchers = []
                            self._remember(track.song_id, pin)
                            published.append(pin)
                        asked = self.clock()
                        ask = self._probed(pin, requested, head)
                        try:
                            opened = await self._request(pin, ask, head, until)
                        except _Changed as exc:
                            # Its link expired or changed before its audio came (an HTTP
                            # 403, 410 or 412, a DASH segment's): the add-on gives a fresh
                            # one, as for a play's own link - asked again while its time
                            # lasts.
                            if self.clock() >= until or relinked:
                                raise
                            relinked = True  # (once: an add-on whose links keep failing fails)
                            self._link_failed(pin, exc, True)
                            unpublish(pin)  # (the fresh one is published as this one was)
                            _failed_step(f"{source.name}: {exc} {self.clock() - asked:.1f}s,"
                                         " asked for a new link")  # fmt: skip
                            continue
                        break
                    opened = await self._verify(pin, opened, track, ask, until, cap)
                    opened = await _tested(pin, opened)
                    opened = _narrowed(opened, ask, requested, pin.size)
                except _Missing as exc:
                    _done(source)
                    tried.add(source.id)
                    self._record(source, answer, False, self.clock() - started - exc.waited)
                    failures.append(f"{source.name}: {exc}")
                    _step(f"{source.name}: {exc} {self.clock() - started:.1f}s")
                    if exc.absent:
                        self._missed(source, track)
                except _Changed as exc:
                    _done(source)
                    tried.add(source.id)
                    self._link_failed(pins[source.id], exc, True)
                    unpublish(pins[source.id])
                    self._record(source, answer, False, self.clock() - started - exc.waited)
                    failures.append(f"{source.name}: {exc}")
                    _failed_step(f"{source.name}: {exc} {self.clock() - started:.1f}s")
                except _Failed as exc:
                    if source.id in pins:  # its link's or its audio's (its lookup's: counted)
                        self._failure(source, exc, pins[source.id])
                        if _kept(exc):
                            kept.append(pins[source.id])
                        unpublish(pins[source.id])
                    _done(source, cut=capped and isinstance(exc, _TimedOut), slow=_unfinished(exc),
                          preparing=isinstance(exc, _Preparing))  # fmt: skip
                    tried.add(source.id)
                    if not _spared(exc):
                        self._record(source, answer, False, self.clock() - started - exc.waited)
                    failures.append(f"{source.name}: {exc}")
                    self._count_timeout(source, exc)
                    (_step if isinstance(exc, _Refused) else _failed_step)(
                        f"{source.name}: {exc} {self.clock() - started:.1f}s"
                    )
                except Exception as exc:  # a broken add-on must not break playback
                    reason = f"unexpected {type(exc).__name__}"
                    log.warning("source %s failed: %s", source.name, reason)
                    self._failure(source, _Error(reason))
                    _done(source)
                    tried.add(source.id)
                    failures.append(f"{source.name}: {reason}")
                    if source.id in pins:
                        unpublish(pins[source.id])
                else:
                    if first:  # another one's audio came first: not used
                        with anyio.CancelScope(shield=True):
                            await opened.close()
                        _step(f"{source.name}: audio after {self.clock() - started:.1f}s,"
                              f" {first[0][0].name}'s came first")  # fmt: skip
                    else:
                        self._delivered(pin, opened, asked)
                        took = self.clock() - started - opened.waited
                        self._record(source, answer, True, took)
                        first.append((source, pin, opened))
                        for other, scope in scopes.items():
                            if other != source.id:
                                scope.cancel()
                        if len(order) > 1 or ((trace := _TRACE.get()) is not None and trace.steps):
                            _step(
                                f"{source.name}: lookup {pin.lookup_seconds:.1f}s, link"
                                f" {pin.link_seconds:.1f}s, first byte {self.clock() - asked:.1f}s"
                            )
                end(source)

        def join(step: list[Source], racing: anyio.abc.TaskGroup, planned: bool = True) -> float:
            """Start ``step``'s add-ons (``planned``: a step of the plan, not the add-ons
            that have the song ready); when the next step joins."""
            now = self.clock()
            seconds = self._step_seconds(step)
            names = ", ".join(s.name for s in step)
            if order:
                _step(f"at {now - begun:.1f}s, joining: {names}")
            elif len(step) > 1:
                _step(f"together: {names}")
            if planned:
                latest[:] = [source.id for source in step]
            for source in step:
                scopes[source.id] = anyio.CancelScope()
                starts[source.id], times[source.id] = now, seconds
                if not pending:
                    lasts.add(source.id)
                order.append(source)
                racing.start_soon(run, source)
            trace = _TRACE.get()
            if trace is not None:  # what it left, for a routing continuing it
                trace.left.planned = True
                trace.left.order = [s.id for later in pending for s in later]
                trace.left.hints = checks.hints() if checks is not None else {}
            return now + seconds

        def settle() -> None:
            """The add-ons still trying when the race ended, stopped: also when the caller
            gave up (its wait cap's grace), so that what they did is counted before the
            client's answer."""
            winner = first[0] if first else None
            for pin in list(published):
                if winner is not None and pin is winner[1]:
                    continue
                if winner is None:
                    unpublish(pin)  # (not the song's: no audio came from it)
                else:  # (the winner's link takes its place)
                    self.forget(track.song_id, pin)
                    _abandon(pin)
            for source in order:
                if source.id in over:
                    continue
                # Stopped: another add-on's audio came first, or its attempt did not end at
                # the wait cap by itself.
                over.add(source.id)
                if (link := pins.get(source.id)) is not None:
                    self._let_go(link)
                tried.add(source.id)
                trying = self.clock() - starts[source.id]
                if winner is not None:
                    _done(source, slow=True)
                    waits = own_waits.get(source.id)
                    held = 0.0 if waits is None else waits.seconds
                    if waits is not None and waits.since is not None:
                        held += max(0.0, time.monotonic() - waits.since)
                    slow = self._slow_now(
                        source, trying - held, times[source.id], source.id in lasts
                    )
                    if slow:
                        self.registry.failed(source, "timeout")
                        self._record(source, "-", False, trying - held)
                        self._slow(source)
                    counted = ", slow: counted toward its cooldown" if slow else ""
                    _step(f"{source.name}: stopped after {trying:.1f}s, {winner[0].name}'s audio"
                          f" came first{counted}")  # fmt: skip
                else:
                    _done(source)
                    self.registry.failed(source, "timeout")
                    failures.append(f"{source.name}: timeout")
                    self._slow(source)
                    _failed_step(f"{source.name}: timeout {trying:.1f}s")

        waited_ready = False
        # Each attempt ends at the wait cap by itself; this is in case one does not - before
        # the caller gives up (``CAP_GRACE``).
        hard = (cap if cap is not None else float("inf")) + CAP_GRACE / 2
        try:
            async with anyio.create_task_group() as racing:
                next_at = join(pending.pop(0), racing) if pending else float("inf")
                while True:
                    nudge[0] = anyio.Event()
                    if first:
                        break
                    now = self.clock()
                    if not running() and not pending:
                        break  # every add-on asked ended without audio
                    if now >= hard or (cap is not None and now >= cap and not running()):
                        break
                    ended = bool(latest) and all(i in over for i in latest)
                    if pending and (now >= next_at or not running() or ended):
                        # The step before has had its time, or every add-on of it (or every
                        # one asked) has ended: the next one joins (by the checks' answers
                        # so far).
                        if checks is not None and checks.answers:
                            pending = self._arranged(pending, checks)
                        if cap is not None and cap <= now:
                            break
                        next_at = join(pending.pop(0), racing)
                        continue
                    at = handover.at if checks is not None else None
                    if checks is not None and at is not None and now >= at:
                        ready = [s for later in pending for s in later if checks.ready(s)]
                        if ready:
                            # An add-on that has the song ready joins now.
                            handover.at = None
                            pending = [[s for s in later if s not in ready] for later in pending]
                            pending = [later for later in pending if later]
                            _step(f"{', '.join(s.name for s in ready)}: has it ready")
                            join(ready, racing, planned=False)
                            continue
                        if checks.done.is_set():
                            handover.at = None  # none has it ready: the plan goes on
                        waited_ready = checks.ready_seen.is_set()  # (none of use: wait on)
                    wake = min(next_at if pending else hard, hard)
                    if at is not None and at > now:
                        wake = min(wake, at)
                    with anyio.move_on_after(max(0.0, wake - now)):
                        if checks is not None and at is not None and now >= at:
                            # The first "ready" answer, or all of them.
                            seen = [] if waited_ready else [checks.ready_seen]
                            await _either(nudge[0], checks.done, *seen)
                        else:
                            await nudge[0].wait()
                racing.cancel_scope.cancel()
        except BaseException:
            # The caller gave up (or something broke): a delivered response must not leak.
            if first:
                with anyio.CancelScope(shield=True):
                    await first[0][2].close()
            raise
        finally:
            settle()
        winner = first[0] if first else None
        trace = _TRACE.get()
        if trace is not None and winner is not None:
            trace.left.order = []
        if winner is None:
            if trace is not None:
                trace.left.order = [s.id for later in pending for s in later]
            if cap is not None and cap <= self.clock():
                failures.append("the wait ran out")
                _stop("the wait cap was reached")
            if kept:
                # A link that stays is the song's (the first asked), as one found one
                # add-on at a time is: a client's retry asks it again - and gets the file
                # its DASH join goes on to make, or waits for that join.
                ordered = sorted(kept, key=lambda p: order.index(p.source))
                ordered[0].by = _TRACE.get()
                self._remember(track.song_id, ordered[0])
            return None
        _, pin, opened = winner
        pin.by = _TRACE.get()
        self._remember(track.song_id, pin)
        return opened

    def _let_go(self, pin: Pin) -> None:
        """A stopped attempt's link: a DASH join it began stops, unless another request
        waits for it."""
        if self.dash is not None and pin.info.transport == "dash":
            self.dash.let_go(self._dash_key(pin))

    async def _try_one(
        self,
        source: Source,
        track: Track,
        requested: ByteRange | None,
        head: bool,
        failures: list[str],
        cap: float | None = None,
    ) -> Opened | None:
        """One add-on's attempt, given up after its step's time, or earlier when the
        client's wait cap comes first. A timeout is recorded as a failure (also one the cap
        cut short) and counts toward its cooldown."""
        full = self._seconds(source)
        limit = full if cap is None else min(full, cap - self.clock())
        if limit <= 0:
            failures.append(f"{source.name}: no time left")
            return None
        capped = full - limit > CUT_SECONDS  # the wait cap ends it: it may still be preparing
        deadline = self.clock() + limit
        started = self.clock()
        try:
            pin = await self._resolve_at(source, track, limit)
        except _Missing as exc:
            _done(source)
            self._record(source, "-", False, self.clock() - started - exc.waited)
            failures.append(f"{source.name}: {exc}")
            _step(f"{source.name}: {exc} {self.clock() - started:.1f}s")
            if exc.absent:
                self._missed(source, track)
            return None
        except _Failed as exc:  # recorded by _resolve_at (as far as it is a failure)
            _done(source, cut=capped and isinstance(exc, _TimedOut), slow=_unfinished(exc),
                  preparing=isinstance(exc, _Preparing))  # fmt: skip
            if not _spared(exc):
                self._record(source, "-", False, self.clock() - started - exc.waited)
            failures.append(f"{source.name}: {exc}")
            self._count_timeout(source, exc)
            _failed_step(f"{source.name}: {exc} {self.clock() - started:.1f}s")
            return None
        asked = self.clock()
        try:
            ask = self._probed(pin, requested, head)
            opened = await self._request(pin, ask, head, deadline)
            opened = await self._verify(pin, opened, track, ask, deadline, cap)
            if requested is None or requested.at_zero:
                opened = await _tested(pin, opened)
            opened = _narrowed(opened, ask, requested, pin.size)
            self._delivered(pin, opened, asked)
        except _Changed as exc:
            _done(source)
            self._link_failed(pin, exc, True)
            self._record(source, "-", False, self.clock() - started - exc.waited)
            failures.append(f"{source.name}: {exc}")
            _failed_step(f"{source.name}: {exc} {self.clock() - started:.1f}s")
            return None
        except _Failed as exc:
            # (Its DASH join going on: a retry asks it again, and joins the same join.)
            _done(source, cut=capped and isinstance(exc, _TimedOut), slow=_unfinished(exc))
            self._failure(source, exc)
            if not _spared(exc):
                self._record(source, "-", False, self.clock() - started - exc.waited)
            failures.append(f"{source.name}: {exc}")
            self._count_timeout(source, exc)
            (_step if isinstance(exc, _Refused) else _failed_step)(
                f"{source.name}: {exc} {self.clock() - started:.1f}s"
            )
            return None
        self._record(source, "-", True, self.clock() - started - opened.waited)
        pin.by = _TRACE.get()
        self._remember(track.song_id, pin)
        return opened

    def _record(self, source: Source, answer: str, delivered: bool, seconds: float) -> None:
        """One attempt at ``source`` at byte zero (Add-ons and Diagnostics show them); kept
        across restarts."""
        self.registry.attempted(source, Attempt(self.clock(), answer, delivered, max(0.0, seconds)))

    def _missed(self, source: Source, track: Track) -> None:
        """``source`` does not have this recording: remembered for a while, so that later
        requests do not ask it again."""
        if self.settings.miss_hours <= 0:
            return
        key = (source.id, _recording(track))
        self._misses.pop(key, None)  # newest last: the oldest go first
        self._misses[key] = self.clock() + self.settings.miss_hours * 3600
        if len(self._misses) > MAX_MISSES:
            now = self.clock()
            for old in [k for k, until in self._misses.items() if until <= now]:
                del self._misses[old]
            while len(self._misses) > MAX_MISSES:
                self._misses.pop(next(iter(self._misses)))

    def _lacks(self, source: Source, track: Track) -> bool:
        """``source`` said it does not have this recording (remembered)."""
        return self._misses.get((source.id, _recording(track)), 0.0) > self.clock()

    def _count_timeout(self, source: Source, exc: Exception) -> None:
        """A timeout of ``source`` toward its cooldown: not a wait at its limits, a DASH
        join's beyond its first, nor one that was preparing the song unless it waited until
        its time ran out."""
        again = isinstance(exc, _Joining) and exc.again
        at_once = isinstance(exc, _Preparing) and not exc.waited_out
        if isinstance(exc, _TimedOut) and not _paced(exc) and not again and not at_once:
            self._slow(source)

    def _slow(self, source: Source) -> None:
        """Count a timeout of ``source``; enough of them without a delivery in between cool
        it down. Until it delivers again, each further one cools it down again."""
        count = source.stats.timeouts_since_delivery + 1
        source.stats.timeouts_since_delivery = count
        if count >= max(1, self.settings.cooldown_timeouts):
            self.registry.cool_down(source.id, self.settings.cooldown_seconds)
            log.info(
                "source %s timed out %d times without delivering; cooling down for %gs",
                source.name,
                count,
                self.settings.cooldown_seconds,
            )

    def background(self, work: Callable[[], Awaitable[Any]]) -> None:
        """Run ``work`` in the application's background task group (if there is one)."""
        if self.spawn is None:
            return

        async def run() -> None:
            try:
                await work()
            except Exception as exc:
                log.info("background work failed: %s", type(exc).__name__)

        self.spawn(run)

    async def prewarm(self, track: Track) -> bool:
        """Warm-ahead: open the first byte of a track before its play, with the same routing.
        The pin is kept for that play, and slow sources, and those that prepare a song before
        they can deliver it, get time to prepare the audio."""
        if self.pinned(track.song_id) is not None:
            return True
        try:
            opened = await self.open(
                track,
                "bytes=0-0",
                budget=self.settings.warm_ahead_budget_seconds,
                purpose="warm",
            )
        except (NoSource, PinBroken):
            return False
        await opened.close()
        return opened.status in (200, 206)

    def _known_wrong(self, source: Source, track: Track) -> bool:
        """``source`` delivered another recording of this one lately."""
        return self._wrong.get(_wrong_key(source, track), 0.0) > self.clock()

    def _probed(self, pin: Pin, requested: ByteRange | None, head: bool) -> ByteRange | None:
        """The range to ask the source for: a short one at byte zero on a link whose length
        is not read yet becomes its first ``PROBE_BYTES`` (then cut back to the client's) -
        while the length check is on."""
        if (
            self.settings.length_tolerance_seconds <= 0
            and self.settings.length_tolerance_percent <= 0
        ):
            return requested
        return _probed(pin, requested, head)

    async def _verify(
        self,
        pin: Pin,
        opened: Opened,
        track: Track,
        requested: ByteRange | None,
        deadline: float,
        cap: float | None = None,
    ) -> Opened:
        """The audio's length, read from its first bytes once per link, against the
        catalog's: another recording - an add-on's lookup handed over a remix
        album's extended version - is not used; the source is not asked for this recording
        again for a while (``_Wrong``). A format or range that does not tell passes. The
        first bytes are waited for until ``deadline`` (at least a moment): a source that
        answers and then stalls is timed out."""
        opened.length, opened.kind, opened.kbps = pin.length, pin.kind, pin.kbps
        if pin.wrong is not None:  # read and rejected already (by a request at once)
            await _closed(opened)
            raise _Wrong(pin.wrong)
        if (
            pin.checked
            or opened.body is None
            or opened.status not in (200, 206)
            or not (requested is None or requested.start == 0)
        ):
            return opened
        wait = max(1.0, deadline - self.clock())  # a moment at least, within the wait cap
        if cap is not None:
            wait = max(0.1, min(wait, cap - self.clock()))
        try:
            with anyio.fail_after(wait):
                head, opened.body, ended = await peek(opened.body)
        except (TimeoutError, httpx.TimeoutException):  # slow, not failing
            await _closed(opened)
            # (After a wait at the add-on's limits that left it too little time: not its.)
            late = _Paced(pacing.REQUESTS) if opened.waited >= CUT_SECONDS else None
            failure = late or _TimedOut("timeout before the first bytes")
            failure.waited = opened.waited
            raise failure from None
        except httpx.HTTPError as exc:  # its audio broke off at once
            await _closed(opened)
            raise _Error(type(exc).__name__) from None
        except Exception as exc:  # a broken stream (closed, consumed): the next source
            await _closed(opened)
            raise _Failed(type(exc).__name__) from None
        except BaseException:  # canceled (a switch, the wait cap)
            await _closed(opened)
            raise
        if pin.wrong is not None:  # rejected by a request at once while this one read
            await _closed(opened)
            raise _Wrong(pin.wrong)
        if not complete(head):
            if ended:  # a short range or file: nothing to decide on; a later request does
                return opened
            pin.checked = True  # past what is read (a large tag): no length
            return opened
        pin.checked = True
        pin.length = opened.length = pin.told or audio_length(head, pin.size)
        found = audio_kind(head, pin.size, pin.length)
        if found is not None:
            pin.kind, pin.kbps = opened.kind, opened.kbps = found
            known = self._known.get(pin.song_id)
            if (
                not _LIBRARY_ACQUISITION.get()
                and known is not None
                and (known.source_id, known.size) == (pin.source.id, pin.size)
            ):
                self._known[pin.song_id] = replace(known, kind=pin.kind, kbps=pin.kbps)
        settings = self.settings
        wanted = track.duration_ms / 1000
        tolerance = max(
            settings.length_tolerance_seconds, settings.length_tolerance_percent / 100 * wanted
        )
        if pin.length is None or tolerance <= 0 or wanted <= 0:
            return opened
        if abs(pin.length - wanted) <= tolerance:
            return opened
        pin.wrong = f"another recording ({pin.length:.1f}s, the catalog's {wanted:.1f}s)"
        await _closed(opened)
        if not _left_out() and not _LIBRARY_ACQUISITION.get():
            self._known.pop(pin.song_id, None)  # not the song's representation
        self.registry.failed(pin.source, pin.wrong)  # once for the link
        key = _wrong_key(pin.source, track)
        self._wrong.pop(key, None)  # newest last: the oldest go first
        self._wrong[key] = self.clock() + WRONG_SECONDS
        while len(self._wrong) > MAX_MISSES:
            self._wrong.pop(next(iter(self._wrong)))
        log.info(
            "%s delivered another recording of %r: %.1fs long, the catalog's %.1fs;"
            " not asked for it again for %g days",
            pin.source.name,
            track.title,
            pin.length,
            wanted,
            WRONG_SECONDS / 86400,
        )
        raise _Wrong(pin.wrong)

    def _link_failed(self, pin: Pin, exc: _Changed, unknown: bool) -> None:
        """A link the source just gave did not work: its failure (once per link). Not when it
        served another file than the play knew (``unknown`` False: a continuing play), which
        is normal at another source."""
        rejected = exc.status in (403, 410) or (exc.status == 412 and unknown)
        if rejected and not pin.failed and not pin.delivered:
            pin.failed = True
            self.registry.failed(pin.source, f"a new link answered {exc}")

    def _source_error(self, source: Source, exc: AddonError) -> None:
        """An add-on's answer that is an error: its failure, and - a rate limit - the time
        it is left alone for; errors count toward its cooldown. Not one it was not asked
        for ("cooling"), one that it is preparing the song ("preparing": no failure - not
        ready in time is counted where the attempt ends), nor another request's answer
        shared (counted there)."""
        if exc.kind in ("cooling", "preparing") or exc.shared:
            return
        self.registry.failed(source, exc.reason)
        if exc.kind == "rate_limited":
            self._rate_limited(source, exc.retry_after)
        elif exc.kind in _ERRORS:
            self._errored(source, exc.reason)

    def _failure(self, source: Source, exc: _Failed, pin: Pin | None = None) -> None:
        """A failed attempt at ``source`` (its link or its audio), in its diagnostics; an
        error counts toward its cooldown - once for a link (``pin``)."""
        if isinstance(exc, (_Wrong, _Unusable, _Paced, _Cooling, _Refused)):
            # Counted once, where it was found; a wait at its limits is not its failure, nor
            # audio a request refused by its own terms.
            return
        self.registry.failed(source, str(exc))
        if isinstance(exc, _Error) and not (pin is not None and pin.errored):
            if pin is not None:
                pin.errored = True
            self._errored(source, str(exc))

    def _errored(self, source: Source, reason: str) -> None:
        """An error from the source - an HTTP 5xx, no answer, a broken answer; not a miss, a
        refusal or a slow answer. Enough of them in a row without a delivery in between cool
        it down, so that plays go to the other sources at once; until it delivers again,
        each further one cools it down again - briefly each time, so that it comes back
        quickly."""
        now = self.clock()
        if now - source.stats.last_error_at > ERROR_RUN_SECONDS:
            source.stats.errors_since_success = 0  # an old error: a new run
        source.stats.last_error_at = now
        source.stats.errors_since_success += 1
        count = source.stats.errors_since_success
        threshold = self.settings.cooldown_errors
        if threshold <= 0 or count < threshold or self.registry.cooling(source.id):
            return
        self.registry.cool_down(source.id, self.settings.cooldown_seconds)
        log.info(
            "source %s: %d errors in a row (the last: %s); passed over for %gs",
            source.name,
            count,
            reason,
            self.settings.cooldown_seconds,
        )

    def _rate_limited(
        self, source: Source, retry_after: float | None, *, audio: bool = False
    ) -> None:
        """The source answered "too many requests" (an API request, an availability check,
        its audio): no API request is sent to its origin for the time its ``Retry-After``
        named (``retry_after``: seconds, validated - ``pacing.retry_after``), else for the
        cooldown - and, when its audio said so (``audio``), no audio request either; a time
        under way is never shortened."""
        wanted = self.settings.cooldown_seconds if retry_after is None else retry_after
        seconds = self.registry.limited(source, wanted, audio=audio)
        log.info(
            "source %s rate limited (%s); not asked for %.0fs",
            source.name,
            "no Retry-After" if retry_after is None else f"Retry-After {retry_after:g}s",
            seconds,
        )

    async def _request(
        self,
        pin: Pin,
        requested: ByteRange | None,
        head: bool,
        deadline: float,
        expect: Known | None = None,
        *,
        mine: bool = True,
    ) -> Opened:
        """The link's answer by its first byte (``deadline``); the caller counts its audio
        as the source's success once it is checked (``_delivered``). ``expect``: the file a
        continuing play started with - another file is ``_Changed`` for a link this request
        found (``mine``), else ``_Other`` (the link is left alone).

        The request is one of the add-on's audio openings at once (the song being
        played is not held back by them) until its first bytes of audio are there, or it
        ends without any. What the attempt waited at the add-on's limits - for this, and
        for the link when this request found it - is noted with the answer
        (``Opened.waited``) or the exception: a time that ran out after such a wait is
        ``_Paced``, not the add-on's timeout."""
        with pacing.watched() as waits:
            before = pin.waited if mine else 0.0
            try:
                opened = await self._requested(pin, requested, head, deadline, expect, mine, waits)
            except (_Failed, _Changed) as exc:
                exc.waited = before + waits.seconds
                raise
        opened.waited = before + waits.seconds
        return opened

    def _timed_out(self, reason: str, waits: pacing.Waits, before: float = 0.0) -> _TimedOut:
        """A request's time ran out: the add-on's timeout - unless the time ran out while
        the request waited at the add-on's limits, or it had waited there for more than a
        moment (``before``: for its link too): then the add-on never had it (``_Paced``)."""
        held = waits.at or (pacing.REQUESTS if before + waits.seconds >= CUT_SECONDS else None)
        return _TimedOut(reason) if held is None else _Paced(held)

    async def _requested(
        self,
        pin: Pin,
        requested: ByteRange | None,
        head: bool,
        deadline: float,
        expect: Known | None,
        mine: bool,
        waits: pacing.Waits,
    ) -> Opened:
        capacity = _BULK_LEASE.get()
        if (
            _BULK_REUSE.get()
            or (capacity is not None and (not capacity.active or capacity.owner is not None))
        ) and pin.info.transport != "dash":
            raise _Reacquire("a direct transfer needs its own upstream admission", resolve=True)
        headers = dict(pin.info.headers)
        headers["accept-encoding"] = "identity"
        if requested is not None:
            headers["range"] = requested.header()
        elif head:
            headers["range"] = "bytes=0-0"
        own_tag = pin.etag  # (another request may identify the link while this one waits)
        matching = own_tag or (expect.etag if expect is not None else None)
        if matching:
            headers["if-match"] = matching
        http = pin.source.http
        pace = pin.source.pace
        before = pin.waited if mine else 0.0
        ended: Callable[[], None] | None = None  # its audio opening at the add-on is over
        kept = False  # ... once its answer's first bytes come (told by its body)

        async def hop(url: httpx.URL) -> None:
            """Before each hop of a redirected audio request: not while the add-on's audio
            is left alone after a rate limit (another request's answer meanwhile)."""
            if pace is not None and (left := pace.audio_blocked) > 0:
                raise pacing.Blocked(left)

        try:
            try:
                with anyio.fail_after(max(0.1, deadline - self.clock())):
                    if pin.info.transport == "dash":
                        response = await self._joined(pin, requested, head, deadline, matching)
                    else:
                        request = http.build_request("GET", pin.info.url, headers=headers)
                        if pace is not None:
                            ended = await pace.open()
                        response = await pacing.send(
                            http, request, turn=hop, private=tuple(pin.info.headers)
                        )
                        if _a_manifest(response):
                            # A plain link that answers with a DASH manifest: joined as one.
                            await response.aclose()
                            if ended is not None:
                                ended()
                                ended = None
                            pin.info = replace(pin.info, transport="dash")
                            response = await self._joined(pin, requested, head, deadline, matching)
            except pacing.Blocked:  # left alone after a rate limit: nothing is sent
                raise _Cooling(pacing.BLOCKED) from None
            except TimeoutError:
                if pin.info.transport == "dash":
                    raise self._join_timed_out(pin, waits, before) from None
                raise self._timed_out("timeout before the first byte", waits, before) from None
            except httpx.ConnectTimeout as exc:  # no connection: an error
                raise _Error(type(exc).__name__) from None
            except httpx.TimeoutException as exc:  # slow, not failing
                raise self._timed_out(type(exc).__name__, waits, before) from None
            except httpx.HTTPError as exc:
                if denied(exc):
                    raise _Failed("audio URL not allowed by the network policy") from None
                raise _Error(type(exc).__name__) from None
            except (RuntimeError, ValueError) as exc:  # closed client, malformed URL or header
                raise _Failed(type(exc).__name__) from None
            try:
                if response.status_code == 412 and expect is not None and not mine and not own_tag:
                    # The play's own ETag was asked for: that link's file is another one.
                    raise _Other("another file (HTTP 412)")
                opened = self._answer(pin, requested, head, response, expect, mine)
                opened.remux = pin.remux if pin.info.transport == "dash" else None
                file = response.extensions.get(DASH_KEPT)
                if file is not None and opened.body is not None and self._keeps():
                    assert self.retention is not None
                    opened.capture = self.retention.dash(file, self.dash)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
                raise
            if opened.body is None:
                # (Closed whatever comes: an answer received is not lost to a request
                # leaving another's link - ``_Left`` - or giving up.)
                with anyio.CancelScope(shield=True):
                    await response.aclose()
            elif ended is not None:
                # The opening lasts until the audio's first bytes (an answer's headers may
                # come long before them), or until the answer is closed unread.
                opened.body = _begun(opened.body, ended)
                opened.close = _closing(opened.close, ended)
                kept = True
            urgency = pacing.current()
            if (
                self.bulk is not None
                and _BULK_LEASE.get() is None
                and (urgency is None or urgency.level <= pacing.PLAY)
                and opened.body is not None
                and pin.info.transport != "dash"
            ):
                finished = self.bulk.observe_playback(pin.source)
                opened.body = _finishing(opened.body, finished)
                opened.close = _closing(opened.close, finished)
            return opened
        finally:
            if ended is not None and not kept:
                ended()

    def _join_timed_out(self, pin: Pin, waits: pacing.Waits, before: float) -> _TimedOut:
        """A DASH link's attempt whose time ran out: a timeout as ``_timed_out`` says - and
        when its join goes on, one that keeps the link for a client's retry (``_Joining``)."""
        timed_out = self._timed_out("timeout while joining the segments", waits, before)
        if _paced(timed_out) or self.dash is None:
            return timed_out
        state = self.dash.outlasts(self._dash_key(pin))
        if state is None:
            return timed_out
        said = "joined a moment later" if state == "kept" else "the join goes on"
        return _Joining(f"timeout while joining the segments ({said})", again=state == "again")

    def _dash_key(self, pin: Pin) -> str:
        """The key of the pin's add-on track in the quality range chosen (``link_key``)."""
        addon = pin.source.addon
        quality = self._dash_quality(pin)
        return link_key(str(addon.base), addon.settings, pin.track_id, quality)

    def _dash_quality(self, pin: Pin) -> tuple[str, str]:
        return (
            ("any", "lossless")
            if pin.cached
            else (self.settings.dash_quality_from, self.settings.dash_quality_to)
        )

    def _continuing_dash(self, pin: Pin, capacity: CapacityLease) -> bool:
        dash = self.dash
        if dash is None:
            return False
        key = self._dash_key(pin)
        start = (dash.holding(key, pin.etag) if pin.etag else None) or self.settings.dash_start
        return dash.continuing(key, start, capacity)

    async def _joined(
        self,
        pin: Pin,
        requested: ByteRange | None,
        head: bool,
        deadline: float,
        matching: str | None,
    ) -> httpx.Response:
        if self.bulk is None or _BULK_LEASE.get() is not None:
            return await self._joined_from(pin, requested, head, deadline, matching)
        while True:
            lease = self.bulk.playback_lease(pin.source)
            token = _BULK_LEASE.set(lease)
            try:
                return await self._joined_from(pin, requested, head, deadline, matching)
            except _Reacquire:
                # Playback admission is immediate. A whole-file fallback or a live
                # selection change gets a new lease rather than recycling a job's.
                pass
            finally:
                _BULK_LEASE.reset(token)
                lease.close()

    async def _joined_from(
        self,
        pin: Pin,
        requested: ByteRange | None,
        head: bool,
        deadline: float,
        matching: str | None,
    ) -> httpx.Response:
        """A DASH link's answer, as a ranged source's would be: its file served at once
        (``dash_start``), or its joined file (kept, or joined now by ``deadline``); the
        range asked for (416 past its end; a HEAD's one byte), with its size, type and
        strong ETag - 412 when ``matching`` is another. A play goes on with the kind of
        file it started with. A join this starts goes on for up to the client's wait cap,
        whatever this request's time - for a request someone waits on, for that long after
        its own time (the app's retry joins it)."""
        dash = self.dash
        if dash is None:  # (a plain link that answered with a manifest)
            raise _Failed(f"DASH is off ({self.dash_off})")
        settings = self.settings
        quality = self._dash_quality(pin)
        capacity = _BULK_LEASE.get()
        link = Link(
            self._dash_key(pin),
            pin.info.url,
            pin.info.headers,
            pin.source.http,
            pin.source.pace,
            pin.source.name,
            capacity,
            _BULK_REUSE.get()
            or (capacity is not None and (not capacity.active or capacity.owner is not None)),
        )
        wanted = pin.seconds
        tolerance = max(
            settings.length_tolerance_seconds, settings.length_tolerance_percent / 100 * wanted
        )
        lasting = settings.max_wait_seconds
        if _WAITED.get():  # (its time is the client's wait: the retry's comes after it)
            lasting += max(0.0, deadline - self.clock())
        start = (dash.holding(link.key, matching) if matching else None) or settings.dash_start
        if start == "at_once":
            try:
                stream = await dash.served(
                    link,
                    quality=quality,
                    at_once=settings.dash_segments_at_once,
                    seconds=wanted,
                    tolerance=tolerance,
                    deadline=deadline,
                    lasting=lasting,
                    limited=lambda named: self._rate_limited(pin.source, named, audio=True),
                )
            except DashError as exc:
                if exc.kind != "whole":
                    raise self._dash_failed(pin, exc) from None
            else:
                return await self._streamed(pin, stream, requested, head, matching)
        pin.remux = pin.told = None
        try:
            joined = await dash.joined(
                link,
                quality=quality,
                at_once=settings.dash_segments_at_once,
                seconds=wanted,
                tolerance=tolerance,
                deadline=deadline,
                lasting=lasting,
                limited=lambda named: self._rate_limited(pin.source, named, audio=True),
            )
        except DashError as exc:
            raise self._dash_failed(pin, exc) from None
        # (From here to the reader's lease nothing waits: the file cannot go meanwhile.)
        headers = {
            "content-type": joined.content_type,
            "accept-ranges": "bytes",
            "etag": joined.etag,
        }
        if matching and matching != joined.etag:
            return httpx.Response(412, headers=headers)
        first, last, status = 0, joined.size - 1, 200
        asked = requested if requested is not None else ByteRange(0, 0) if head else None
        if asked is not None:
            span = asked.resolve(joined.size)
            if span is None:
                return httpx.Response(416, headers={"content-range": f"bytes */{joined.size}"})
            (first, last), status = span, 206
            headers["content-range"] = f"bytes {first}-{last}/{joined.size}"
        headers["content-length"] = str(last - first + 1)
        return httpx.Response(
            status,
            headers=headers,
            stream=dash.body(joined, first, last),
            extensions={DASH_KEPT: joined},
        )

    async def _streamed(
        self,
        pin: Pin,
        stream: Stream,
        requested: ByteRange | None,
        head: bool,
        matching: str | None,
    ) -> httpx.Response:
        """A DASH link's file served at once, as a ranged source's answer: its first media
        segment the range covers waited for (its failure is the attempt's), the rest sent
        as it comes."""
        dash = self.dash
        assert dash is not None
        file = stream.file
        headers = {"content-type": file.content_type, "accept-ranges": "bytes", "etag": file.etag}
        if matching and matching != file.etag:
            return httpx.Response(412, headers=headers)
        first, last, status = 0, file.size - 1, 200
        asked = requested if requested is not None else ByteRange(0, 0) if head else None
        if asked is not None:
            span = asked.resolve(file.size)
            if span is None:
                return httpx.Response(416, headers={"content-range": f"bytes */{file.size}"})
            (first, last), status = span, 206
            headers["content-range"] = f"bytes {first}-{last}/{file.size}"
        headers["content-length"] = str(last - first + 1)
        pin.remux, pin.told = file.kind, stream.seconds
        body = dash.stream_body(stream, first, last)  # (its lease: the file stays meanwhile)
        covered = [index for index, _, _ in file.layout.pieces(first, last) if index >= 0]
        if covered and not head:
            try:
                await dash.ready(stream, covered[0])
            except DashError as exc:
                await body.aclose()
                raise self._dash_failed(pin, exc) from None
            except BaseException:
                await body.aclose()
                raise
        return httpx.Response(status, headers=headers, stream=body, extensions={DASH_KEPT: file})

    def _dash_failed(self, pin: Pin, exc: DashError) -> Exception:
        """A join's failure as the routing's: a manifest Shijhon does not play, or a
        preview, is unusable audio (the add-on's failure in its diagnostics, once a join),
        and so is one without a quality in the range chosen (no failure of the add-on's);
        an expired link is re-resolved; the others are as a direct link's would be."""
        if exc.kind == "capacity":
            return _Reacquire(str(exc))
        if exc.kind == "range":
            return _Unusable(str(exc))
        if exc.kind in ("unsupported", "short"):
            reason = f"unsupported DASH: {exc}"
            if exc.kind == "short":
                reason = f"a preview ({exc.seconds or 0:.1f}s, the catalog's {pin.seconds:.1f}s)"
            if not exc.counted:  # (one join's answer goes to each request that waited)
                exc.counted = True
                self.registry.failed(pin.source, reason)
            return _Unusable(reason)
        if exc.kind == "expired":
            return _Changed(str(exc), exc.status)
        if exc.kind == "changed":  # (the file served is not that link's any more)
            return _Changed(str(exc), 412)
        if exc.kind == "cooling":
            return _Cooling(str(exc))
        if exc.kind == "denied":
            return _Failed("audio URL not allowed by the network policy")
        if exc.kind == "error":
            return _Error(str(exc))
        if exc.kind == "timeout":
            return _TimedOut(str(exc))
        if exc.kind == "paced":
            return _Paced(str(exc))
        if exc.kind == "rate_limited":
            return _Limited(str(exc))
        return _Failed(str(exc))  # another answer

    def _delivered(self, pin: Pin, opened: Opened, asked: float) -> None:
        """Its audio answered: the source's success (once per link); a delivery ends its run
        of timeouts and of errors (``SourceRegistry.succeeded``)."""
        if opened.status in (200, 206) and not pin.delivered:
            pin.delivered = True
            self.registry.succeeded(pin.source, self.clock() - asked)

    def _answer(
        self,
        pin: Pin,
        requested: ByteRange | None,
        head: bool,
        response: httpx.Response,
        expect: Known | None = None,
        mine: bool = True,
    ) -> Opened:
        """Turn the source's response into the client's; the caller closes ``response``
        when the result has no body."""
        status = response.status_code
        if status in (403, 410, 412):
            raise _Changed(f"HTTP {status}", status)
        if status == 429:
            named = pacing.retry_after(response.headers.get("retry-after"))
            self._rate_limited(pin.source, named, audio=True)
            raise _Limited("rate limited")
        if status == 416:
            size = pin.size or _total(response)
            out = [(b"content-range", f"bytes */{size}".encode())] if size else []
            return Opened(416, [*out, (b"content-length", b"0")], None, _nothing, pin.source.name)
        if status not in (200, 206):
            raise (_Error if status >= 500 else _Failed)(f"HTTP {status}")
        if status == 206 and not response.headers.get("content-range"):
            raise _Error("partial response without Content-Range")  # a broken answer

        total = _total(response)
        etag = response.headers.get("etag")
        etag = etag if etag and _STRONG_ETAG.match(etag) else None
        if expect is not None:
            # Only the file the continuing play started with will do - whatever another
            # request has learned of the link while this one waited for its answer.
            if not mine and not _holds(pin, expect):
                raise _Other("another file")  # known as one by now: that link is left alone
            sized = expect.size is None or total is None or total == expect.size
            tagged = not expect.etag or not etag or etag == expect.etag
            if not (sized and tagged):
                if mine:
                    raise _Changed("size changed" if not sized else "representation changed")
                if pin.size is None:
                    pin.size, pin.etag = total, etag  # what that link serves
                raise _Other("another file")
        if pin.size is None:
            if pin.etag and etag and etag != pin.etag:  # it ignored If-Match
                raise _Changed("representation changed")
            pin.size, pin.etag = total, etag or pin.etag
        elif total is not None and total != pin.size:
            raise _Changed("size changed")
        elif pin.etag and etag and etag != pin.etag:
            raise _Changed("representation changed")
        if expect is not None:
            # The play's file: what the play knows of it stays known where this answer does
            # not tell (no size, no ETag; a later range tells no format) - its next ranges
            # are held to it.
            pin.size = expect.size if pin.size is None else pin.size
            pin.etag = pin.etag or expect.etag
            if pin.kind is None:
                pin.kind, pin.kbps = expect.kind, expect.kbps
        if pin.content_type is None:
            pin.content_type = _content_type(response.headers.get("content-type"), pin.info)
        self._learned(
            pin.song_id,
            Known(pin.size, pin.etag, pin.source.id, pin.track_id, pin.kind, pin.kbps),
        )
        content_type = pin.content_type or "application/octet-stream"
        common = [(b"content-type", content_type.encode()), (b"accept-ranges", b"bytes")]
        if pin.etag:
            common.append((b"etag", pin.etag.encode()))
        encoding = response.headers.get("content-encoding")
        if encoding and encoding != "identity":
            common.append((b"content-encoding", encoding.encode()))

        if head:
            if requested is None or total is None:
                length = [(b"content-length", str(total).encode())] if total is not None else []
                return Opened(200, common + length, None, _nothing, pin.source.name)
            span = requested.resolve(total)
            if span is None:
                return Opened(
                    416, [(b"content-range", f"bytes */{total}".encode())], None, _nothing
                )
            return Opened(
                206, common + _range_headers(*span, total), None, _nothing, pin.source.name
            )

        capture = None
        if (
            self.retention is not None
            and self._keeps()
            and pin.info.transport != "dash"
            and (not encoding or encoding == "identity")
        ):
            capture = self.retention.capture(pin, response, total, etag)
        raw = response.aiter_raw() if capture is None else capture.tap(response.aiter_raw())

        if status == 206:
            out = [
                *common,
                (b"content-range", response.headers["content-range"].encode()),
                (b"content-length", response.headers.get("content-length", "").encode()),
            ]
            out = [(k, v) for k, v in out if v]
            return Opened(206, out, raw, response.aclose, pin.source.name, capture=capture)

        # 200: the whole representation.
        if requested is not None and total is not None:
            span = requested.resolve(total)
            if span is None:
                return Opened(
                    416, [(b"content-range", f"bytes */{total}".encode())], None, _nothing
                )
            first, last = span
            body = _slice(raw, first, last)
            return Opened(
                206,
                common + _range_headers(first, last, total),
                body,
                response.aclose,
                pin.source.name,
                capture=capture,
            )
        length = [(b"content-length", str(total).encode())] if total is not None else []
        return Opened(200, common + length, raw, response.aclose, pin.source.name, capture=capture)

    def _keeps(self) -> bool:
        """Whether the audio being opened may go to a client and be kept once it has
        (``retention``)."""
        return self.retention is not None and self.retention.enabled and _KEEPS.get()


def _states(sources: list[Source], answers: dict[int, Availability | None], asked: set[int]) -> str:
    """The availability checks' answers for a log line."""

    def state(source: Source) -> str:
        if source.id not in asked:
            return "no answer yet"
        answer = answers.get(source.id)
        if answer is None or answer.available is None:
            return "cannot tell"
        return "ready" if answer.available else "not now"

    return ", ".join(f"{s.name} {state(s)}" for s in sources)


async def _own_id(source: Source, track: Track, settings: PlaybackSettings) -> str | None:
    """The source's own track ID: by its key, or its confirmed catalog identity.

    A declared identity is trusted only at the configured catalog add-on, while its
    current manifest still matches the operator's confirmation.
    """
    if of_catalog(source.key, track.ref):
        return own_track(source.key, (await source.addon.manifest()).id, track.ref)
    if source.name != settings.catalog_addon or not settings.catalog_identity:
        return None
    manifest = await source.addon.manifest()
    declared = manifest.full_catalog
    key, _, item = (track.ref or "").partition(":")
    if (
        declared is not None
        and declared.key
        and declared.identity == settings.catalog_identity
        and key == declared.key
    ):
        return item if ITEM_ID.fullmatch(item) and item not in (".", "..") else None
    return None


async def _link(source: Source, track_id: str, preparing: list[float]) -> StreamInfo:
    """The add-on's link for ``track_id``. While it says it is preparing the song (an HTTP
    503 with a valid ``Retry-After``), it is asked again after the time it named (half a
    second at least, each time) as often as the attempt's time allows - its budget, the
    client's wait cap; when the time named is longer than what is left, not again
    (``AddonError`` "preparing"). ``preparing``: the seconds waited, each."""
    while True:
        try:
            return await source.addon.stream(track_id)
        except AddonError as exc:
            if exc.kind != "preparing":
                raise
            wait = max(MIN_PREPARING_WAIT, exc.retry_after or 0.0)
            if wait >= anyio.current_effective_deadline() - anyio.current_time():
                raise
            _step(f"{source.name}: preparing, asked again after {wait:.1f}s")
            await anyio.sleep(wait)
            preparing.append(wait)


def _not_ready(preparing: list[float], ready_in: float | None = None) -> str:
    """Why: "not ready within its budget (preparing, ready in 5s, asked again 2 times)"."""
    asked = len(preparing)
    told = f", ready in {ready_in:g}s" if ready_in is not None else ""
    again = f", asked again {asked} time{'s' if asked != 1 else ''}" if asked else ""
    return f"{NOT_READY} (preparing{told}{again})"


async def _looks_up(source: Source, track: Track) -> bool:
    """Whether the source can look the recording up (its answer "not found" means it lacks
    it): by ISRC, when the track has one, or by ``/resolve`` or its search."""
    try:
        resources = (await source.addon.manifest()).resources
    except Exception:
        return False
    return ("isrc" in resources and bool(track.isrc)) or bool({"resolve", "search"} & resources)


async def _begun(chunks: AsyncIterator[bytes], begun: Callable[[], None]) -> AsyncIterator[bytes]:
    """``chunks``, telling ``begun`` at the first of them - or at their end, when none
    came."""
    try:
        async for chunk in chunks:
            begun()
            yield chunk
    finally:
        begun()


def _closing(close: Callable[[], Awaitable[None]], ended: Callable[[], None]) -> Any:
    """``close``, telling ``ended`` once it is done (an answer closed unread)."""

    async def closed() -> None:
        try:
            await close()
        finally:
            ended()

    return closed


async def _finishing(body: AsyncIterator[bytes], ended: Callable[[], None]) -> AsyncIterator[bytes]:
    try:
        async for chunk in body:
            yield chunk
    finally:
        ended()


def _paced(exc: BaseException) -> bool:
    """The request's time ran out at the add-on's limits, not at the add-on."""
    return isinstance(exc, _Paced)


def _spared(exc: BaseException) -> bool:
    """Nothing the add-on did: it never had the request's time (``_Paced``), was not
    asked (``_Cooling``), or the request refused its audio by its own terms (``_Refused``)
    - no attempt of its own to measure, and no failure for the song that a client's retry
    would skip it for."""
    return isinstance(exc, (_Paced, _Cooling, _Refused))


def _unfinished(exc: BaseException) -> bool:
    """No failure for the song that a client's retry would skip the add-on for: nothing it
    did (``_spared``), it is still joining the song's DASH link (``_Joining``), or it was
    still preparing the song (``_Preparing``)."""
    return _spared(exc) or isinstance(exc, (_Joining, _Preparing))


def _kept(exc: BaseException) -> bool:
    """The link stays: its audio was not asked for in time (``_Paced``), its DASH join
    goes on (``_Joining``), or the request refused its audio by its own terms
    (``_Refused``: it may do for another)."""
    return isinstance(exc, (_Paced, _Joining, _Refused))


def _refuse(kind: str | None, kbps: int | None) -> None:
    """Raise :class:`_Refused` when the request's own test (``Deliverer.open``'s
    ``accept``) does not take audio of this format and bitrate."""
    accept = _ACCEPT.get()
    why = None if accept is None else accept(kind, kbps)
    if why is not None:
        raise _Refused(why)


async def _tested(pin: Pin, opened: Opened) -> Opened:
    """Audio opened at byte zero, as the request's own test takes it, by what its first
    bytes told (``_verify``) - refused and closed when it does not do. A probe whose link
    is not read yet (a HEAD) has nothing to tell by: it passes."""
    if _ACCEPT.get() is None or opened.status not in (200, 206):
        return opened
    if opened.body is None and not pin.checked:
        return opened
    try:
        _refuse(opened.kind, opened.kbps)
    except _Refused:
        await _closed(opened)
        raise
    return opened


def _wrong_key(source: Source, track: Track) -> tuple[int, str]:
    """A source's other recording is remembered for the recording and its catalog length
    (one ISRC on two versions of different lengths stays two)."""
    return source.id, f"{_recording(track)}@{round(track.duration_ms / 10_000)}"


def _probed(pin: Pin, requested: ByteRange | None, head: bool) -> ByteRange | None:
    """The range to ask the source for: a short one at byte zero on a link whose length is
    not read yet becomes its first ``PROBE_BYTES`` (then cut back to the client's)."""
    if (
        head
        or pin.checked
        or requested is None
        or requested.start != 0
        or requested.end is None
        or requested.end >= PROBE_BYTES - 1
    ):
        return requested
    return ByteRange(0, PROBE_BYTES - 1)


def _narrowed(
    opened: Opened, asked: ByteRange | None, requested: ByteRange | None, total: int | None
) -> Opened:
    """The source's answer to a widened range (``_probed``), cut back to the client's - never
    past what the source sent. A 200 (its total unknown) goes as the source sent it."""
    if asked is requested or requested is None or opened.body is None:
        return opened
    if opened.status != 206 or requested.start is None or requested.end is None:
        return opened
    sent = _sent_last(opened.headers)
    first, last = requested.start, requested.end
    if sent is not None:
        last = min(last, sent)
    if total is not None:
        last = min(last, total - 1)
    if last < first:
        return opened
    kept = [(k, v) for k, v in opened.headers if k not in (b"content-range", b"content-length")]
    size = "*" if total is None else str(total)
    ranged = [
        (b"content-range", f"bytes {first}-{last}/{size}".encode()),
        (b"content-length", str(last - first + 1).encode()),
    ]
    return replace(opened, status=206, headers=kept + ranged, body=_slice(opened.body, first, last))


def _sent_last(headers: list[tuple[bytes, bytes]]) -> int | None:
    """The last byte an answer's Content-Range says it holds."""
    for key, value in headers:
        if key == b"content-range":
            match = re.match(rb"bytes (\d+)-(\d+)/", value)
            return int(match.group(2)) if match else None
    return None


async def _closed(opened: Opened) -> None:
    with anyio.CancelScope(shield=True):
        await opened.close()


def _recording(track: Track) -> str:
    """A recording as the primary's misses remember it: its ISRC, else the track."""
    isrc = "".join(ch for ch in track.isrc or "" if ch.isalnum()).upper()
    return f"isrc:{isrc}" if isrc else f"track:{track.key}"


def _holds(pin: Pin, known: Known | None) -> bool:
    """Whether ``pin`` links the file a continuing play started with (``known``), by its
    size and strong ETag as far as both know them; a link not answered yet takes the play's
    file (its first answer checks it)."""
    if known is None:
        return True
    if known.size is None and known.etag is None:  # nothing to recognize it by: its source
        return pin.source.id == known.source_id
    if known.etag and pin.etag and pin.etag != known.etag:
        return False
    return pin.size is None or known.size is None or pin.size == known.size


def _earliest(deadline: float, cap: float | None) -> float:
    return deadline if cap is None else min(deadline, cap)


async def _either(*events: anyio.Event) -> None:
    """Wait until any of ``events`` is set."""
    async with anyio.create_task_group() as tg:

        async def wait(event: anyio.Event) -> None:
            await event.wait()
            tg.cancel_scope.cancel()

        for event in events:
            tg.start_soon(wait, event)


def _a_manifest(response: httpx.Response) -> bool:
    """A link's answer that is a DASH manifest, not audio."""
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return response.status_code in (200, 206) and kind == "application/dash+xml"


def _total(response: httpx.Response) -> int | None:
    content_range = response.headers.get("content-range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[1]
        return int(total) if total.isdigit() else None
    length = response.headers.get("content-length", "")
    return int(length) if response.status_code == 200 and length.isdigit() else None


def _range_headers(first: int, last: int, total: int) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-range", f"bytes {first}-{last}/{total}".encode()),
        (b"content-length", str(last - first + 1).encode()),
    ]


def _content_type(header: str | None, info: StreamInfo) -> str:
    value = (header or "").split(";")[0].strip().lower()
    if value.startswith("audio/") or value in ("video/mp4", "application/ogg"):
        return "audio/mp4" if value == "video/mp4" else value
    for hint in (info.format, info.container, info.codec):
        if hint and hint in _TYPES:
            return _TYPES[hint]
    suffix = httpx.URL(info.url).path.rsplit(".", 1)[-1].lower()
    return _TYPES.get(suffix, "application/octet-stream")


async def _slice(chunks: AsyncIterator[bytes], first: int, last: int) -> AsyncIterator[bytes]:
    offset = 0
    async for chunk in chunks:
        end = offset + len(chunk)
        if end > first:
            yield chunk[max(0, first - offset) : last - offset + 1]
        offset = end
        if offset > last:
            return


def _steps(sources: list[Source]) -> list[list[Source]]:
    """``sources`` (in the plan's order) by their steps."""
    steps: list[list[Source]] = []
    for source in sources:
        if steps and steps[-1][0].step == source.step:
            steps[-1].append(source)
        else:
            steps.append([source])
    return steps
