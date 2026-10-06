"""DASH links of add-ons: served at once as one MP4 file of their segments, or joined into
one ordinary file first.

**At once** (``Dash.served``, ``[delivery] dash_start = "at_once"``): the manifest, its
init segment (refused when its boxes say the audio is encrypted), then one byte of each
media segment, a few at once, for its size and ETag; the file - the init segment, an
index of the segments, the segments (``delivery.segmented``) - is served from then on, a
range waiting only for the segments it covers, and the rest is fetched in the background
into the kept file, which is the very bytes served. A segment is checked as it comes: its
size and ETag those its probe told, its boxes a movie fragment's, nothing encrypted. A
manifest whose segments cannot be indexed (one file, no durations) or a segment that
tells no size is joined as below instead.

**A join** (``Dash.joined``; ``dash_start = "complete"``, the fallback above, and every
copy for the library), one at a time for an add-on's track and the quality asked for,
which every request for it waits for:

1. the manifest, at most 1 MiB (``delivery.mpd`` reads it);
2. its init segment - refused when its boxes say the audio is encrypted;
3. its segments, a few at once, written in order into one fragmented MP4 (a whole file as
   it comes); a segment that fails with an HTTP 5xx or a broken connection is asked for
   once more;
4. ffmpeg, a separate process reading only that local file and only as MP4, copies the
   audio without re-encoding it into a native FLAC, an MP3 or a fast-start M4A (AAC,
   ALAC);
5. the result is checked - audio, of the catalog's length - and kept.

Every request of it is an audio request of the add-on's, as a direct link's is: through
the add-on's client (each host and each redirect checked by the network policy) and its
limits (each one an audio opening). Headers the add-on gave with its link go only to the
link's own origin, not on through a redirect to another.

A join - and the fetching of a file served at once - is background work: it goes on when
the request that started it ends (its time ran out, the client left, a fallback was
taken), for up to the client's wait cap from its start, or longer while a request waiting
for it still has time - so that the app's retry, or the next request for the song, gets
its file at once or waits for the same join. It is as urgent at the add-on's limits as
the most urgent request waiting for it (the song being played first); with none waiting,
it is Shijhon's own background work. Only a few run at once, for everyone together,
besides the songs being played: the others wait their turn, the most urgent first. A stop
ends them all: a join leaves nothing half-joined, and a file served at once stays until the
next start empties the folder.

Joined files and files served at once are kept in a folder of their own, the least
recently used going first past its size - never one being read or fetched, never the
newest. None outlives a restart.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import logging
import re
import shutil
import subprocess
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import anyio
import anyio.to_thread
import httpx
import mutagen
from anyio import AsyncFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from shijhon.delivery import mpd, pacing, segmented
from shijhon.delivery.netpolicy import denied
from shijhon.delivery.pacing import AddonPace
from shijhon.delivery.segmented import Probe, Served

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 1024 * 1024 * 1024  # a joined file, as a download-first file, at most
MAX_SEGMENT_BYTES = 16 * 1024 * 1024  # one segment (held in memory until its turn)
MAX_INIT_BYTES = 1024 * 1024  # an init segment (a few kilobytes as muxers write them)
FRESH_SECONDS = 5.0  # a file just joined is not pushed out before its waiters take it
REMUX_SECONDS = 30.0
JOINS_AT_ONCE = 3  # for everyone together ([delivery] dash_joins_at_once)
PROBES_AT_ONCE = 8  # segments' sizes asked for at once (each an audio opening), at most
MAX_PROBE_BYTES = 64  # a probe's answer (one byte asked for)
WHOLE_KEYS = 4096  # tracks remembered as not to be served at once ...
WHOLE_SECONDS = 3600.0  # ... for this long
TURN = "waiting for a turn among the DASH joins"
_MOMENT = 1.0  # a wait at the add-on's limits longer than this: the add-on lacked the time
_OURS = re.compile(
    r"\.[0-9a-f]{32}\.(part|flac|m4a|mp3)|[0-9a-f]{24}-[0-9a-f]{32}\.(flac|m4a|mp3|mp4)"
)
_STRONG = re.compile(r'"[\x21\x23-\x7e]{1,200}"')
_PROBED = re.compile(r"bytes 0-0/(\d{1,15})")
_T = TypeVar("_T")
# What a joined file of each kind of audio is written as: its suffix, its content type,
# ffmpeg's output options.
_OUTPUTS = {
    "flac": (".flac", "audio/flac", ["-f", "flac"]),
    "mp3": (".mp3", "audio/mpeg", ["-f", "mp3"]),
    "aac": (".m4a", "audio/mp4", ["-movflags", "+faststart", "-f", "mp4"]),
    "alac": (".m4a", "audio/mp4", ["-movflags", "+faststart", "-f", "mp4"]),
}


def ffmpeg_path() -> str | None:
    """ffmpeg on the PATH (DASH links are not played without it)."""
    return shutil.which("ffmpeg")


class DashError(Exception):
    """A join that ended without a file. ``kind``: unsupported (a manifest or audio Shijhon
    does not play) | range (no quality in the range chosen) | short (``seconds`` long: a
    preview) | expired (``status``: 403, 410, 412) | rate_limited | cooling (not asked: rate
    limited a while ago) | denied (the network policy) | error (an HTTP 5xx, a broken
    connection or answer, a remux that failed) | failed (another answer) | timeout | paced
    (its time ran out at the add-on's limits) | changed (a segment is not the one its probe
    told: another file) | whole (not to be served at once: joined instead). The reason
    names no address."""

    def __init__(
        self, kind: str, reason: str, *, status: int | None = None, seconds: float | None = None
    ) -> None:
        super().__init__(reason)
        self.kind = kind
        self.status = status
        self.seconds = seconds
        self.counted = False  # (the deliverer's: counted as the add-on's failure once)


class _Broken(Exception):
    """An HTTP 5xx, or a connection that broke: the part is asked for once more."""


@dataclass(frozen=True)
class Link:
    """An add-on's DASH link, as a join needs it. ``key``: the add-on's track (and the
    quality asked for) - the same for every link the add-on gives for it."""

    key: str
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    http: httpx.AsyncClient = field(repr=False)
    pace: AddonPace | None = field(repr=False)
    name: str  # the add-on's, for the log


@dataclass(eq=False)
class Joined:
    """A joined file, kept."""

    path: Path
    size: int
    etag: str  # strong, of its bytes
    content_type: str
    seconds: float
    kept_at: float = 0.0  # on the joiner's clock
    readers: int = 0
    gone: bool = False  # no longer kept: removed once its last reader is done
    used: int = 0  # when it was last used (the order of uses, files served at once too)


class _Wanted(pacing.Urgency):
    """A join's urgency at the add-on's limits and among the joins: that of the most urgent
    request waiting for it (a play that waits for a warm-ahead's join makes it a play's);
    with none waiting, Shijhon's own background work."""

    def __init__(self) -> None:
        self.waiting: list[pacing.Urgency] = []
        super().__init__(pacing.WARM)

    @property
    def level(self) -> int:
        return min((urgency.level for urgency in self.waiting), default=self._alone)

    @level.setter
    def level(self, value: int) -> None:
        self._alone = value

    @contextlib.contextmanager
    def wanted(self, urgency: pacing.Urgency) -> Iterator[None]:
        """A request waits for the join meanwhile (its urgency raised meanwhile - a
        warm-ahead's song reported as playing - is the join's at once)."""
        self.waiting.append(urgency)
        urgency.followers.append(self)
        self.changed()
        try:
            yield
        finally:
            urgency.followers.remove(self)
            self.waiting.remove(urgency)
            self.changed()


@dataclass(eq=False)
class _Job:
    # Its time (on anyio's clock): the wait cap from its start, or until the latest
    # deadline of the requests waiting for it when that is later.
    scope: anyio.CancelScope
    began: float = 0.0
    urgency: _Wanted = field(default_factory=_Wanted)
    waits: pacing.Waits = field(default_factory=pacing.Waits)
    done: anyio.Event = field(default_factory=anyio.Event)
    planned: anyio.Event = field(default_factory=anyio.Event)  # (served at once: its file)
    result: Joined | None = None
    error: DashError | None = None
    outlived = False  # a request waiting for it ran out of time while it went on

    def wanted_until(self, deadline: float) -> None:
        self.scope.deadline = max(self.scope.deadline, deadline)


@dataclass(eq=False)
class Stream:
    """A DASH link's audio served at once: its file, and where its segments come from now
    (the latest link's manifest - the same representation as the first's)."""

    key: str
    file: Served
    link: Link
    plan: mpd.Plan
    init: bytes
    urgency: _Wanted  # its fetching's, whichever fill goes on (the requests waiting for it)
    at_once: int
    lasting: float  # how long a fill goes on without a request (the client's wait cap)
    limited: Callable[[float | None], None]
    began: float  # (the joiner's clock: when its manifest was asked for)
    probes: tuple[int, float]  # sizes asked for, in how many seconds
    quality: tuple[str, str] = ("any", "lossless")
    seconds: float = 0.0  # its length, as its index tells it
    track_scale: int | None = None  # the init segment's timescale (its segments' runs)
    job: _Job | None = None  # the fill under way
    spare: Link | None = None  # a newer link, taken when its own one stops working
    used: int = 0
    shown: bool = False  # its shape logged
    relinking: anyio.Lock = field(default_factory=anyio.Lock)


@dataclass
class Dash:
    folder: Path
    ffmpeg: str
    max_bytes: int = 1024 * 1024 * 1024  # the kept files in all (the newest whatever its size)
    # Starts a join as background work, so that it outlives the request that started it
    # (False: it was not started - Shijhon is stopping).
    spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], bool | None] | None = None
    clock: Callable[[], float] = time.monotonic
    _files: OrderedDict[str, Joined] = field(default_factory=OrderedDict)  # oldest use first
    _jobs: dict[str, _Job] = field(default_factory=dict)
    _turns: pacing.Turns = field(default_factory=lambda: pacing.Turns(JOINS_AT_ONCE, TURN))
    _served: OrderedDict[str, Stream] = field(default_factory=OrderedDict)  # served at once
    _planning: dict[str, _Job] = field(default_factory=dict)  # ... being planned
    # Joined instead: why, and until when (the joiner's clock).
    _whole: OrderedDict[str, tuple[str, float]] = field(default_factory=OrderedDict)
    _uses: Iterator[int] = field(default_factory=itertools.count)

    @property
    def joins_at_once(self) -> int:
        """Joins at once, for everyone together (a setting; changed at once)."""
        return self._turns.total

    @joins_at_once.setter
    def joins_at_once(self, value: int) -> None:
        self._turns.total = value

    def joining(self, key: str) -> bool:
        """Whether a join of ``key`` is under way (or waiting for its turn)."""
        return key in self._jobs

    def outlasts(self, key: str) -> str | None:
        """A request waiting for the join of ``key`` ran out of time: "first" when the join
        goes on for more than a moment yet and no request's time ran out before (a slow
        add-on's timeout counts once a join), "again" when one did, "kept" when its file
        was kept meanwhile; None when it ended without one, or ends now."""
        stream = self._served.get(key)
        if key in self._files or (stream is not None and stream.file.complete):
            return "kept"
        job = self._jobs.get(key) or self._planning.get(key)
        if job is None and stream is not None:
            job = stream.job  # (fetching a file served at once)
        if job is None or job.scope.deadline - anyio.current_time() <= _MOMENT:
            return None
        said, job.outlived = ("again" if job.outlived else "first"), True
        return said

    def clear(self) -> int:
        """Files a stop left behind - half-done joins and kept files alike - go (at start,
        before any request). Only files named as this module names them; how many."""
        if not self.folder.is_dir():
            return 0
        removed = 0
        for path in self.folder.iterdir():
            if path.is_file() and _OURS.fullmatch(path.name):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    async def joined(
        self,
        link: Link,
        *,
        quality: tuple[str, str],
        at_once: int,
        seconds: float,
        tolerance: float,
        deadline: float,
        lasting: float = 0.0,
        limited: Callable[[float | None], None],
    ) -> Joined:
        """The link's audio as a joined file - kept, being joined, or joined now. A join
        started now goes on for ``lasting`` seconds (the client's wait cap), or until
        ``deadline`` (on the clock) when that is later; one under way, until the latest
        deadline of the requests waiting for it when that is later still. ``quality``: the
        range chosen from (``mpd.plan``). ``seconds``: the catalog's length; audio
        shorter by more than ``tolerance`` (0: no check) is a preview, not kept.
        ``limited``: told the ``Retry-After`` (seconds, or None) of an answer "too many
        requests". The caller's own time bounds its wait: the join goes on without it.
        Raises ``DashError``."""
        until = anyio.current_time() + max(0.01, deadline - self.clock())
        mine = pacing.current() or pacing.Urgency()
        while True:
            kept = self._kept(link.key)
            if kept is not None:
                return kept
            job = self._jobs.get(link.key)
            work = None
            if job is not None:
                job.wanted_until(until)
            else:
                now = anyio.current_time()
                job = self._jobs[link.key] = _Job(
                    anyio.CancelScope(deadline=max(until, now + max(0.0, lasting))), began=now
                )
                work = partial(
                    self._run, job, link, quality, max(1, at_once), seconds, tolerance, limited
                )
            watching = pacing.watching()
            began = job.waits.joined()
            with job.urgency.wanted(mine):
                if work is not None:
                    if self.spawn is None:
                        await work()
                    elif self.spawn(work) is False:
                        del self._jobs[link.key]
                        raise DashError("failed", "Shijhon is stopping")
                try:
                    await job.done.wait()
                finally:
                    if watching is not None:  # its waits at the add-on's limits are this one's
                        watching.shared(job.waits, began)
            if job.error is not None:
                raise job.error
            # (Kept still, unless joins that ended meanwhile pushed it out: then again.)

    def body(self, joined: Joined, first: int, last: int) -> httpx.AsyncByteStream:
        """Bytes ``first`` to ``last`` of a kept file; it is not removed until the stream
        is closed (or read to its end)."""
        return _Reader(self, joined, first, last)

    def holding(self, key: str, etag: str) -> str | None:
        """Which kept file of ``key`` has ``etag``: "complete" (joined), "at_once" (served
        at once) or None - a play goes on with the file it started with."""
        joined = self._files.get(key)
        if joined is not None and joined.etag == etag:
            return "complete"
        stream = self._served.get(key)
        if stream is not None and stream.file.etag == etag:
            return "at_once"
        return None

    # --- served at once -----------------------------------------------------------------

    async def served(
        self,
        link: Link,
        *,
        quality: tuple[str, str],
        at_once: int,
        seconds: float,
        tolerance: float,
        deadline: float,
        lasting: float = 0.0,
        limited: Callable[[float | None], None],
    ) -> Stream:
        """The link's audio as a file served at once - kept (whole or not), or planned now
        (its manifest, its init segment, every media segment's size). Its missing segments
        are fetched in the background, as a join's: for ``lasting`` seconds (the client's
        wait cap) from the fetching's start, or until ``deadline`` (on the clock) when that
        is later - and later still while a request waits for a segment. Another link of the
        song (a new one, or another listener's) is kept for when the segments' own link
        stops working (``_segment``). The arguments are ``joined``'s. Raises ``DashError``
        - "whole" when the link is to be joined instead."""
        until = anyio.current_time() + max(0.01, deadline - self.clock())
        mine = pacing.current() or pacing.Urgency()
        while True:
            whole = self._whole.get(link.key)
            if whole is not None and whole[1] > self.clock():
                raise DashError("whole", whole[0])
            stream = self._served.get(link.key)
            if stream is not None:
                self._served.move_to_end(link.key)
                stream.used = next(self._uses)
                if not stream.file.complete and stream.link.url != link.url:
                    # Another link of the song (a new one, or another listener's): taken
                    # when the segments' own one stops working.
                    stream.spare = link
                if not stream.file.complete:
                    self._fill(stream, until)
                return stream
            job = self._planning.get(link.key)
            work = None
            if job is not None:
                job.wanted_until(until)
            else:
                now = anyio.current_time()
                job = self._planning[link.key] = _Job(
                    anyio.CancelScope(deadline=max(until, now + max(0.0, lasting))), began=now
                )
                work = partial(
                    self._plan_and_fill, job, link, quality, max(1, at_once), seconds,
                    tolerance, lasting, limited,
                )  # fmt: skip
            watching = pacing.watching()
            began = job.waits.joined()
            with job.urgency.wanted(mine):
                if work is not None and (self.spawn is None or self.spawn(work) is False):
                    del self._planning[link.key]
                    raise DashError("failed", "Shijhon is stopping")
                try:
                    await job.planned.wait()
                finally:
                    if watching is not None:  # its waits at the add-on's limits are this one's
                        watching.shared(job.waits, began)
            if job.error is not None:
                raise job.error
            # (Kept now, unless files kept meanwhile pushed it out: then again.)

    async def ready(self, stream: Stream, index: int) -> None:
        """Until media segment ``index`` of a file served at once is there - for a request
        about to answer (its own time bounds the wait). Raises ``DashError``."""
        mine = pacing.current() or pacing.Urgency()
        try:
            with stream.urgency.wanted(mine):
                await stream.file.present(index, partial(self._again, stream))
        except DashError:
            raise
        except OSError as exc:
            raise DashError(
                "failed", f"cannot write the file ({exc.strerror or 'OSError'})"
            ) from None
        except Exception as exc:  # (a fill's own failure: a segment of another size)
            raise DashError("error", f"unexpected {type(exc).__name__}") from None

    def stream_body(self, stream: Stream, first: int, last: int) -> httpx.AsyncByteStream:
        """Bytes ``first`` to ``last`` of a file served at once, each segment's as soon as
        it is there; the file is not removed until the stream is closed. A segment that
        does not come (its fetching failed, or took the client's wait cap) ends the answer
        early, as a source's broken answer would."""
        return _StreamReader(self, stream, first, last, pacing.current() or pacing.Urgency())

    async def copied_out(self, source: Path, out: Path, kind: str) -> float:
        """A file served at once (``source``, read whole), its audio copied as it is into
        ``out`` as a join's is - a native FLAC, an MP3 or a fast-start M4A (``kind``): what
        the library gets. Its length (seconds). Raises ``DashError``."""
        inspected = await anyio.to_thread.run_sync(_inspect, source)
        if inspected.encrypted:
            raise DashError("unsupported", "encrypted audio")
        if kind not in _OUTPUTS:
            raise DashError("unsupported", "audio of another kind")
        return await self._remux(source, out, kind, inspected)

    async def _segment_at(self, stream: Stream, index: int, mine: pacing.Urgency) -> None:
        """A reader's wait for a segment in the middle of its answer: for up to the
        client's wait cap (its fetching goes on that long too); its failure ends the
        answer early (an ``httpx`` error, as a source's broken answer)."""
        if stream.file.has(index):
            return
        until = anyio.current_time() + max(1.0, stream.lasting)
        if stream.job is not None:
            stream.job.wanted_until(until)
        try:
            with anyio.fail_after(max(1.0, stream.lasting)), stream.urgency.wanted(mine):
                await stream.file.present(index, partial(self._again, stream))
        except TimeoutError:
            log.info("DASH from %s: a segment did not come in time", stream.link.name)
            raise httpx.ReadTimeout("a DASH segment did not come in time") from None
        except Exception as exc:
            reason = str(exc) if isinstance(exc, DashError) else type(exc).__name__
            log.info("DASH from %s: a segment could not be fetched: %s", stream.link.name, reason)
            raise httpx.ReadError(f"a DASH segment could not be fetched: {reason}") from None

    def _again(self, stream: Stream) -> None:
        """A fill of ``stream`` for a reader that waits for a segment (none is under way)."""
        self._fill(stream, anyio.current_time() + max(1.0, stream.lasting))

    def _fill(self, stream: Stream, until: float) -> None:
        """The stream's missing segments fetched in the background - by the fill under way
        (it goes on until ``until`` at least, on anyio's clock), or by one started now for
        the client's wait cap. Raises ``DashError`` when none can start (a stop)."""
        if stream.file.complete:
            return
        if stream.job is not None and stream.file.filling:
            stream.job.wanted_until(until)
            return
        if self._served.get(stream.key) is not stream:  # (dropped: another file now)
            raise DashError("changed", "the file served is not the link's any more")
        now = anyio.current_time()
        job = stream.job = _Job(
            anyio.CancelScope(deadline=max(until, now + max(0.0, stream.lasting))),
            began=now,
            urgency=stream.urgency,
        )
        stream.file.started()
        if self.spawn is None or self.spawn(partial(self._filling, stream, job)) is False:
            stream.job = None
            failure = DashError("failed", "Shijhon is stopping")
            stream.file.stopped(failure)
            raise failure

    async def _plan_and_fill(
        self,
        job: _Job,
        link: Link,
        quality: tuple[str, str],
        at_once: int,
        seconds: float,
        tolerance: float,
        lasting: float,
        limited: Callable[[float | None], None],
    ) -> None:
        """A file served at once, planned (``_plan``) and kept; then its segments fetched,
        as its first fill."""
        stream: Stream | None = None
        try:
            with pacing.watched(job.waits), pacing.urgent(job.urgency):
                with job.scope:
                    # One of the few at once (the song being played does not wait).
                    async with self._turns.turn():
                        stream = await self._plan(
                            job, link, quality, at_once, seconds, tolerance, lasting, limited
                        )
                        job.planned.set()  # (the requests waiting answer from here)
                        if self._planning.get(link.key) is job:  # (its fill: ``stream.job``)
                            del self._planning[link.key]
                        await stream.file.fill(partial(self._segment, stream), at_once)
                if job.scope.cancelled_caught and stream is None:
                    waits = pacing.watching()
                    held = waits.held(_MOMENT) if waits is not None else None
                    job.error = (
                        DashError("paced", held)
                        if held is not None
                        else DashError("timeout", "timeout while reading the segments' sizes")
                    )
        except DashError as exc:
            job.error = exc
            if exc.kind == "whole":
                self._joined_instead(link.key, str(exc))
        except OSError as exc:  # its files (a full disk): not the add-on's doing
            reason = exc.strerror or type(exc).__name__
            job.error = DashError("failed", f"cannot write the file ({reason})")
        except Exception as exc:  # fetching must not break playback
            log.warning("DASH from %s failed: %s", link.name, type(exc).__name__)
            job.error = DashError("error", f"unexpected {type(exc).__name__}")
        finally:
            if stream is None and job.error is None:  # canceled (a stop)
                job.error = DashError("failed", "the join was stopped")
            self._ended(stream, job, link)

    async def _filling(self, stream: Stream, job: _Job) -> None:
        """A later fill of a file served at once: its missing segments."""
        try:
            with pacing.watched(job.waits), pacing.urgent(job.urgency), job.scope:
                async with self._turns.turn():
                    await stream.file.fill(partial(self._segment, stream), stream.at_once)
        except DashError as exc:
            job.error = exc
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            job.error = DashError("failed", f"cannot write the file ({reason})")
        except Exception as exc:
            log.warning("DASH from %s failed: %s", stream.link.name, type(exc).__name__)
            job.error = DashError("error", f"unexpected {type(exc).__name__}")
        finally:
            self._ended(stream, job, stream.link)

    def _ended(self, stream: Stream | None, job: _Job, link: Link) -> None:
        """A planning's or a fill's end: what it leaves, and what is said of it."""
        if self._planning.get(link.key) is job:
            del self._planning[link.key]
        if stream is not None:
            if stream.job is job:  # (not when a newer fill has begun meanwhile)
                stream.job = None
                if stream.file.filling:  # (it never began: its turn did not come)
                    stream.file.stopped(job.error or DashError("timeout", "no turn in time"))
            if job.error is not None and job.error.kind in ("changed", "unsupported"):
                self._drop(stream)  # another file than the one served: never served on
                if job.error.kind == "unsupported":  # (the join says why, from now on)
                    self._joined_instead(stream.key, str(job.error))
            if stream.file.complete and job.error is None:
                log.debug(
                    "DASH from %s: all %d segment(s) in %.2fs, %d bytes kept",
                    link.name,
                    len(stream.plan.media),
                    self.clock() - stream.began,
                    stream.file.size,
                )
        if job.error is not None and not job.urgency.waiting:  # (no request tells it)
            log.info(
                "DASH from %s ended after %.1fs without %s, no request waiting for it: %s",
                link.name,
                anyio.current_time() - job.began,
                "every segment" if stream is not None else "a file",
                job.error,
            )
        job.planned.set()
        job.done.set()

    async def _plan(
        self,
        job: _Job,
        link: Link,
        quality: tuple[str, str],
        at_once: int,
        seconds: float,
        tolerance: float,
        lasting: float,
        limited: Callable[[float | None], None],
    ) -> Stream:
        began = self.clock()
        data, address = await self._manifest(link, limited)
        try:
            plan = mpd.plan(data, address, quality)
        except mpd.OutOfRange as exc:
            raise DashError("range", str(exc)) from None
        except mpd.Unsupported as exc:
            raise DashError("unsupported", str(exc)) from None
        if plan.seconds is not None and _short(plan.seconds, seconds, tolerance):
            raise DashError("short", "a preview", seconds=plan.seconds)
        if plan.whole or plan.init is None or plan.durations is None:
            raise DashError("whole", "segments the file cannot index")
        length = sum(plan.durations) / plan.timescale  # (what the index will say)
        if _short(length, seconds, tolerance):
            raise DashError("short", "a preview", seconds=length)
        raw = await self._retried(link, plan.init, limited, limit=MAX_INIT_BYTES)
        assert isinstance(raw, bytes)
        inspected = await anyio.to_thread.run_sync(mpd.inspect, raw)
        if inspected.encrypted:
            raise DashError("unsupported", "encrypted audio")
        track = segmented.track_id(raw)
        if track is None:
            raise DashError("whole", "an init segment without a track")
        try:
            init = segmented.unindexed(raw)
        except ValueError:
            raise DashError("whole", "an init segment that is not whole boxes") from None
        probing = self.clock()
        media = plan.media
        found = await segmented.probed(
            len(media),
            lambda n: self._probe(link, media[n], limited),
            min(PROBES_AT_ONCE, 2 * max(1, at_once)),
        )
        probed = self.clock() - probing
        if found is None:
            raise DashError("whole", "a segment that tells no size")
        sizes = [told.size for told in found]
        if max(sizes) > MAX_SEGMENT_BYTES:
            raise DashError("unsupported", "a segment over the size limit")
        try:
            index = segmented.index_box(track, plan.timescale, plan.earliest, sizes, plan.durations)
        except ValueError as exc:
            raise DashError("whole", str(exc)) from None
        layout = segmented.Layout(init + index, tuple(sizes))
        if layout.total > MAX_FILE_BYTES:
            raise DashError("unsupported", "audio over the size limit")
        validators = [told.validator for told in found]
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{link.key[:24]}-{uuid.uuid4().hex}.mp4"
        try:
            await anyio.to_thread.run_sync(segmented.create, path, layout)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        file = Served(
            layout, path=path, etag=segmented.served_etag(layout.head, validators),
            kind=plan.kind, validators=validators,
        )  # fmt: skip
        file.kept_at = self.clock()
        stream = Stream(
            link.key, file, link, plan, init, job.urgency, at_once, lasting, limited, began,
            (sum(1 for part in media if part.first is None), probed), quality=quality,
            seconds=length, track_scale=inspected.timescale, job=job,
        )  # fmt: skip
        file.started()
        self._keep_stream(stream)
        return stream

    def _joined_instead(self, key: str, why: str) -> None:
        """The track is joined, not served at once, for a while."""
        self._whole.pop(key, None)
        self._whole[key] = (why, self.clock() + WHOLE_SECONDS)
        while len(self._whole) > WHOLE_KEYS:
            self._whole.popitem(last=False)

    def _keep_stream(self, stream: Stream) -> None:
        stream.used = next(self._uses)
        old = self._served.pop(stream.key, None)
        if old is not None:
            self._drop(old)
        self._served[stream.key] = stream
        self._evict(stream.file)

    def _drop(self, stream: Stream) -> None:
        """A file served at once is no longer kept: its fill ends, its readers finish what
        is there (a segment still missing ends their answers)."""
        if self._served.get(stream.key) is stream:
            del self._served[stream.key]
        if stream.job is not None:
            stream.job.scope.cancel()
        self._retire(stream.file)

    async def _relinked(self, stream: Stream, link: Link) -> bool:
        """The stream's segments from ``link`` from now on - its manifest read first: True
        when it offers the same representation (the same init segment, segments and
        durations); otherwise the stream is dropped (False)."""
        async with stream.relinking:
            if stream.link.url == link.url or stream.file.complete:
                return self._served.get(stream.key) is stream
            limited = stream.limited
            data, address = await self._manifest(link, limited)
            try:
                plan = mpd.plan(data, address, stream.quality)
            except mpd.Unsupported:
                plan = None
            old = stream.plan
            same = plan is not None and (
                (plan.kind, plan.codec, plan.durations, plan.timescale, plan.earliest)
                == (old.kind, old.codec, old.durations, old.timescale, old.earliest)
                and [p.size for p in plan.media] == [p.size for p in old.media]
                and plan.init is not None
            )
            if plan is not None and same:
                assert plan.init is not None
                raw = await self._retried(link, plan.init, limited, limit=MAX_INIT_BYTES)
                assert isinstance(raw, bytes)
                try:
                    same = segmented.unindexed(raw) == stream.init
                except ValueError:
                    same = False
            if plan is None or not same:
                log.info("DASH from %s: a new link to another file than the one served", link.name)
                self._drop(stream)
                return False
            stream.link, stream.plan = link, plan
            if stream.spare is link:
                stream.spare = None
            return True

    async def _segment(self, stream: Stream, index: int) -> bytes:
        """Media segment ``index`` of a file served at once, as the file holds it: of the
        size its probe told, a movie fragment of about the duration the index gives it, not
        encrypted; its index of its own made a ``free`` box (``segmented.neutral``). When
        its link stops working (403, 410, 412) and a newer one has come, the stream takes
        that one - the same representation, or it is dropped - and asks again."""
        link, part = stream.link, stream.plan.media[index]
        try:
            data = await self._retried(link, part, stream.limited, limit=MAX_SEGMENT_BYTES)
        except DashError as exc:
            if exc.kind != "expired":
                raise
            if stream.link is link:  # (not taken meanwhile by another segment's fetch)
                newer = stream.spare
                if newer is None or newer.url == link.url:
                    raise
                try:
                    taken = await self._relinked(stream, newer)
                except DashError:
                    if stream.spare is newer:  # (its manifest did not answer: not again)
                        stream.spare = None
                    raise
                if not taken:
                    raise DashError("changed", "a new link to another file") from None
            return await self._segment(stream, index)
        assert isinstance(data, bytes)
        file = stream.file
        if len(data) != file.layout.sizes[index]:
            raise DashError("changed", "a segment of another size than its probe told")
        try:
            data = segmented.neutral(data)
        except ValueError as exc:
            raise DashError("unsupported", str(exc)) from None
        found = await anyio.to_thread.run_sync(mpd.inspect, data)
        if found.encrypted:
            raise DashError("unsupported", "encrypted audio")
        durations = stream.plan.durations
        if stream.track_scale and found.units and durations is not None:
            told = durations[index] / stream.plan.timescale
            if abs(found.units / stream.track_scale - told) > max(1.0, told / 4):
                raise DashError("unsupported", "a segment of another length than the manifest's")
        if not stream.shown:
            stream.shown = True
            self._shown(stream)
        return data

    def _shown(self, stream: Stream) -> None:
        """The shape of a file served at once, at debug (no address), once its first
        segment is there."""
        plan = stream.plan
        count, seconds = stream.probes
        log.debug(
            "DASH from %s: static, %s, %d representation(s) (%s), chose %s %dk, %d segment(s),"
            " %s, protection none, served at once: %d probe(s) in %.2fs, first byte after"
            " %.2fs, %d bytes",
            stream.link.name,
            plan.layout,
            len(plan.offered),
            ", ".join(plan.offered),
            plan.codec,
            round(plan.bandwidth / 1000),
            len(plan.media),
            f"{plan.seconds:.1f}s" if plan.seconds else "no length given",
            count,
            seconds,
            self.clock() - stream.began,
            stream.file.size,
        )

    async def _probe(
        self, link: Link, part: mpd.Part, limited: Callable[[float | None], None]
    ) -> Probe | None:
        """A media segment's size and ETag, from one byte of it (asked for once more after
        an HTTP 5xx or a broken connection); None when the answer tells no size (no range
        answered). A byte range of a file the manifest names is its size already."""
        if part.size is not None:
            return Probe(part.size)
        probe = mpd.Part(part.url, 0, 0)

        async def read(response: httpx.Response, begun: Callable[[], None]) -> Probe | None:
            return await _probed(response, begun, limited)

        try:
            return await self._ask(link, probe, limited, read)
        except _Broken:
            pass
        try:
            return await self._ask(link, probe, limited, read)
        except _Broken as exc:
            raise DashError("error", f"{exc}, asked twice") from None

    def _kept(self, key: str) -> Joined | None:
        found = self._files.get(key)
        if found is not None:
            self._files.move_to_end(key)
            found.used = next(self._uses)
        return found

    def _keep(self, key: str, joined: Joined) -> None:
        joined.kept_at = self.clock()
        joined.used = next(self._uses)
        old = self._files.pop(key, None)
        if old is not None:
            self._retire(old)
        self._files[key] = joined
        self._evict(joined)

    def _evict(self, newest: Joined | Served) -> None:
        """Past the size, the least recently used go - joined files and files served at
        once alike; never the newest, one being read or fetched, or one just kept."""
        kept: list[tuple[int, str, Joined | Stream]] = [
            *((j.used, k, j) for k, j in self._files.items()),
            *((s.used, k, s) for k, s in self._served.items()),
        ]
        total = sum(_held(item).size for _, _, item in kept)
        for _, key, item in sorted(kept, key=lambda entry: entry[0]):
            if total <= self.max_bytes:
                break
            held = _held(item)
            fresh = newest.kept_at - held.kept_at < FRESH_SECONDS  # its waiters may not have it
            busy = isinstance(item, Stream) and (item.job is not None or item.file.filling)
            if held is newest or held.readers or fresh or busy:
                continue
            total -= held.size
            if isinstance(item, Stream):
                self._drop(item)
            else:
                del self._files[key]
                self._retire(item)

    def _retire(self, joined: Joined | Served) -> None:
        joined.gone = True
        if not joined.readers:
            joined.path.unlink(missing_ok=True)

    def _done_reading(self, joined: Joined | Served) -> None:
        joined.readers -= 1
        if joined.gone and not joined.readers:
            joined.path.unlink(missing_ok=True)

    async def _run(
        self,
        job: _Job,
        link: Link,
        quality: tuple[str, str],
        at_once: int,
        seconds: float,
        tolerance: float,
        limited: Callable[[float | None], None],
    ) -> None:
        try:
            # (Its requests at the add-on and its turn as urgent as the requests waiting.)
            with pacing.watched(job.waits), pacing.urgent(job.urgency):
                job.result = await self._join(
                    link, quality, at_once, seconds, tolerance, job.scope, limited
                )
        except DashError as exc:
            job.error = exc
        except OSError as exc:  # its files (a full disk): not the add-on's doing
            reason = exc.strerror or type(exc).__name__
            job.error = DashError("failed", f"cannot write the joined file ({reason})")
        except Exception as exc:  # a join must not break playback
            log.warning("DASH join from %s failed: %s", link.name, type(exc).__name__)
            job.error = DashError("error", f"unexpected {type(exc).__name__}")
        finally:
            if job.result is None and job.error is None:  # canceled (a stop)
                job.error = DashError("failed", "the join was stopped")
            if self._jobs.get(link.key) is job:
                del self._jobs[link.key]
            if job.error is not None and not job.urgency.waiting:  # (no request tells it)
                log.info(
                    "DASH join from %s ended after %.1fs without a file, no request waiting"
                    " for it: %s",
                    link.name,
                    anyio.current_time() - job.began,
                    job.error,
                )
            job.done.set()

    async def _join(
        self,
        link: Link,
        quality: tuple[str, str],
        at_once: int,
        seconds: float,
        tolerance: float,
        scope: anyio.CancelScope,
        limited: Callable[[float | None], None],
    ) -> Joined:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = uuid.uuid4().hex
        part = self.folder / f".{name}.part"
        out: Path | None = None
        try:
            with scope:
                # One of the few joins at once (the song being played does not wait).
                async with self._turns.turn():
                    began = self.clock()
                    data, address = await self._manifest(link, limited)
                    try:
                        plan = mpd.plan(data, address, quality)
                    except mpd.OutOfRange as exc:
                        raise DashError("range", str(exc)) from None
                    except mpd.Unsupported as exc:
                        raise DashError("unsupported", str(exc)) from None
                    if plan.seconds is not None and _short(plan.seconds, seconds, tolerance):
                        raise DashError("short", "a preview", seconds=plan.seconds)
                    await self._fetch(link, plan, part, at_once, limited)
                    fetched = self.clock()
                    inspected = await anyio.to_thread.run_sync(_inspect, part)
                    if inspected.encrypted:
                        raise DashError("unsupported", "encrypted audio")
                    suffix, content_type, _ = _OUTPUTS[plan.kind]
                    out = part.with_name(f".{name}{suffix}")
                    length = await self._remux(part, out, plan.kind, inspected)
            if scope.cancelled_caught:  # its time ran out (the wait cap, every request's)
                waits = pacing.watching()
                held = waits.held(_MOMENT) if waits is not None else None
                if held is not None:
                    raise DashError("paced", held)
                raise DashError("timeout", "timeout while joining the segments")
            assert out is not None  # (the block ran to its end)
            if _short(length, seconds, tolerance):
                raise DashError("short", "a preview", seconds=length)
            size = out.stat().st_size
            etag = await anyio.to_thread.run_sync(_etag, out)
            kept = self.folder / f"{link.key[:24]}-{name}{out.suffix}"
            out.rename(kept)  # (whole, or not there at all)
            joined = Joined(kept, size, etag, content_type, length)
            self._keep(link.key, joined)
            log.debug(
                "DASH from %s: static, %s, %d representation(s) (%s), chose %s %dk,"
                " %d segment(s), %s, protection none, join %.2fs, remux %.2fs, %d bytes",
                link.name,
                plan.layout,
                len(plan.offered),
                ", ".join(plan.offered),
                plan.codec,
                round(plan.bandwidth / 1000),
                len(plan.media),
                f"{plan.seconds:.1f}s" if plan.seconds else "no length given",
                fetched - began,
                self.clock() - fetched,
                size,
            )
            return joined
        finally:
            part.unlink(missing_ok=True)
            if out is not None:
                out.unlink(missing_ok=True)  # (gone already when it was kept)

    async def _manifest(
        self, link: Link, limited: Callable[[float | None], None]
    ) -> tuple[bytes, str]:
        """The manifest's bytes, and its address after redirects (its parts' base)."""
        found: list[str] = []
        try:
            data = await self._part(
                link, mpd.Part(link.url), limited, limit=mpd.MAX_MANIFEST_BYTES, seen=found,
                too_large="a manifest over 1 MiB",
            )  # fmt: skip
        except _Broken as exc:  # (an HTTP 5xx, no connection: the add-on's error)
            raise DashError("error", str(exc)) from None
        assert isinstance(data, bytes)
        return data, found[0]

    async def _fetch(
        self,
        link: Link,
        plan: mpd.Plan,
        path: Path,
        at_once: int,
        limited: Callable[[float | None], None],
    ) -> None:
        """The init segment (checked first) and the media, in order, into ``path``."""
        async with await anyio.open_file(path, "wb") as out:
            written = 0
            if plan.init is not None:
                init = await self._retried(link, plan.init, limited, limit=MAX_INIT_BYTES)
                assert isinstance(init, bytes)
                if (await anyio.to_thread.run_sync(mpd.inspect, init)).encrypted:
                    raise DashError("unsupported", "encrypted audio")
                await out.write(init)
                written = len(init)
            if plan.whole:
                await self._retried(
                    link, plan.media[0], limited, limit=MAX_FILE_BYTES - written, into=out
                )
                return
            await self._segments(link, plan.media, out, at_once, limited, written)

    async def _segments(
        self,
        link: Link,
        parts: tuple[mpd.Part, ...],
        out: AsyncFile[bytes],
        at_once: int,
        limited: Callable[[float | None], None],
        written: int,
    ) -> None:
        """``parts`` fetched ``at_once`` at a time (those fetched and not written yet
        included) and written in their order."""
        ready = [anyio.Event() for _ in parts]
        got: dict[int, bytes] = {}
        failure: list[DashError] = []
        slots = anyio.Semaphore(at_once)

        async with anyio.create_task_group() as group:

            async def fetch(index: int) -> None:
                try:
                    data = await self._retried(link, parts[index], limited, limit=MAX_SEGMENT_BYTES)
                    assert isinstance(data, bytes)
                    got[index] = data
                except DashError as exc:  # (raised as itself, not as a group)
                    failure.append(exc)
                    group.cancel_scope.cancel()
                finally:
                    ready[index].set()

            async def feed() -> None:
                for index in range(len(parts)):
                    await slots.acquire()
                    group.start_soon(fetch, index)

            group.start_soon(feed)
            for index in range(len(parts)):
                await ready[index].wait()
                if failure:
                    break
                data = got.pop(index)
                written += len(data)
                if written > MAX_FILE_BYTES:
                    failure.append(DashError("unsupported", "audio over the size limit"))
                    break
                await out.write(data)
                slots.release()
            group.cancel_scope.cancel()
        if failure:
            raise failure[0]

    async def _retried(
        self,
        link: Link,
        part: mpd.Part,
        limited: Callable[[float | None], None],
        *,
        limit: int,
        into: AsyncFile[bytes] | None = None,
    ) -> bytes | int:
        """``_part``, asked for once more after an HTTP 5xx or a broken connection."""
        start = await into.tell() if into is not None else 0
        try:
            return await self._part(link, part, limited, limit=limit, into=into)
        except _Broken:
            if into is not None:
                await into.seek(start)
                await into.truncate()
        try:
            return await self._part(link, part, limited, limit=limit, into=into)
        except _Broken as exc:
            raise DashError("error", f"{exc}, asked twice") from None

    async def _part(
        self,
        link: Link,
        part: mpd.Part,
        limited: Callable[[float | None], None],
        *,
        limit: int,
        into: AsyncFile[bytes] | None = None,
        seen: list[str] | None = None,
        too_large: str = "a segment over the size limit",
    ) -> bytes | int:
        """One request of the join, its body read whole - into ``into`` when given (then
        its size). An audio opening at the add-on until its first bytes. ``seen``: told
        the address the answer came from."""
        chunks: list[bytes] = []
        size = 0

        async def read(response: httpx.Response, begun: Callable[[], None]) -> None:
            nonlocal size
            _check(response, part, limited)
            if seen is not None:
                seen.append(str(response.url))
            async for chunk in response.aiter_bytes():
                begun()  # its first bytes: the opening is over
                size += len(chunk)
                if size > limit:
                    raise DashError("unsupported", too_large)
                if into is not None:
                    await into.write(chunk)
                else:
                    chunks.append(chunk)

        await self._ask(link, part, limited, read)
        if size == 0:
            raise DashError("error", "an empty answer")
        if part.size is not None and size != part.size:
            raise DashError("error", "a part of another size than its range")
        return size if into is not None else b"".join(chunks)

    async def _ask(
        self,
        link: Link,
        part: mpd.Part,
        limited: Callable[[float | None], None],
        read: Callable[[httpx.Response, Callable[[], None]], Awaitable[_T]],
    ) -> _T:
        """One request of the add-on's, its answer read by ``read`` (given it and what to
        call at its first bytes): an audio opening at the add-on until then, through its
        client and limits; the link's headers for the link's own origin only. Its
        failures as ``DashError`` (``_Broken``: an HTTP 5xx or a broken connection)."""
        headers = {"accept-encoding": "identity"}
        private: tuple[str, ...] = ()
        if pacing.origin(part.url) == pacing.origin(link.url):  # the link's headers: there only
            headers = {**link.headers, **headers}
            private = tuple(link.headers)
        if part.first is not None:
            headers["range"] = f"bytes={part.first}-{part.last}"
        pace = link.pace

        async def hop(url: httpx.URL) -> None:
            if pace is not None and (left := pace.audio_blocked) > 0:
                raise pacing.Blocked(left)

        ended: Callable[[], None] | None = None

        def begun() -> None:
            nonlocal ended
            if ended is not None:
                ended()
                ended = None

        try:
            if pace is not None:
                ended = await pace.open()
            request = link.http.build_request("GET", part.url, headers=headers)
            response = await pacing.send(link.http, request, turn=hop, private=private)
            try:
                return await read(response, begun)
            finally:
                await response.aclose()
        except pacing.Blocked:
            raise DashError("cooling", pacing.BLOCKED) from None
        except httpx.HTTPError as exc:
            if denied(exc):
                raise DashError("denied", "not allowed by the network policy") from None
            if isinstance(exc, httpx.TimeoutException) and not isinstance(
                exc, httpx.ConnectTimeout
            ):  # slow, not failing (as for a direct link)
                raise DashError("timeout", type(exc).__name__) from None
            if isinstance(exc, httpx.TransportError):  # no connection, or one that broke
                raise _Broken(type(exc).__name__) from None
            raise DashError("error", type(exc).__name__) from None
        except (RuntimeError, ValueError) as exc:  # a closed client, a malformed address
            raise DashError("failed", type(exc).__name__) from None
        finally:
            begun()

    async def _remux(self, part: Path, out: Path, kind: str, inspected: mpd.Inspected) -> float:
        """The joined MP4's audio (``kind``), copied as it is into ``out``: a native FLAC,
        an MP3, or a fast-start M4A (AAC, ALAC). Its length (seconds), read back."""
        flac = kind == "flac"
        args = [
            self.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror",
            # Local input only, read as MP4 whatever its bytes say.
            "-protocol_whitelist", "file", "-f", "mp4", "-i", str(part),
            "-map", "0:a:0", "-c", "copy", "-map_metadata", "-1", "-fflags", "+bitexact",
            *_OUTPUTS[kind][2],
            "-y", str(out),
        ]  # fmt: skip
        try:
            code, said = await _process(args, REMUX_SECONDS)
        except TimeoutError:  # (on this machine: not the add-on's error)
            raise DashError("failed", "the remux took too long") from None
        except OSError as exc:
            raise DashError(
                "failed", f"ffmpeg could not be started ({type(exc).__name__})"
            ) from None
        if code != 0:
            last = said.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            log.debug("DASH remux failed (exit %d): %s", code, last[0][:200])
            raise DashError("error", "the audio could not be remuxed")
        if flac:
            await anyio.to_thread.run_sync(_count_samples, out, inspected)
        length = await anyio.to_thread.run_sync(_length, out, kind)
        if length is None or length <= 0:
            raise DashError("error", "no audio after the remux")
        return length


class _Reader(httpx.AsyncByteStream):
    def __init__(self, dash: Dash, joined: Joined, first: int, last: int) -> None:
        self._dash, self._joined = dash, joined
        self._first, self._last = first, last
        self._open = True
        joined.readers += 1

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            left = self._last - self._first + 1
            async with await anyio.open_file(self._joined.path, "rb") as source:
                await source.seek(self._first)
                while left > 0:
                    chunk = await source.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._open:
            self._open = False
            self._dash._done_reading(self._joined)


class _StreamReader(httpx.AsyncByteStream):
    def __init__(
        self, dash: Dash, stream: Stream, first: int, last: int, urgency: pacing.Urgency
    ) -> None:
        self._dash, self._stream = dash, stream
        self._first, self._last = first, last
        self._urgency = urgency  # the request's (its body is read after it returned)
        self._open = True
        stream.file.readers += 1

    async def __aiter__(self) -> AsyncIterator[bytes]:
        stream = self._stream

        async def wait(index: int) -> None:
            await self._dash._segment_at(stream, index, self._urgency)

        try:
            async for chunk in stream.file.chunks(self._first, self._last, wait):
                yield chunk
        except OSError as exc:  # (its file: as a source's broken answer)
            reason = type(exc).__name__
            raise httpx.ReadError(f"the DASH file could not be read ({reason})") from None
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._open:
            self._open = False
            self._dash._done_reading(self._stream.file)


def _held(item: Joined | Stream) -> Joined | Served:
    return item.file if isinstance(item, Stream) else item


async def _probed(
    response: httpx.Response, begun: Callable[[], None], limited: Callable[[float | None], None]
) -> Probe | None:
    """A probe's answer (one byte asked for): the segment's size and strong ETag, or None
    when it tells no size (the whole segment, 200: no range answered)."""
    _refused(response, limited)
    found = _PROBED.fullmatch(response.headers.get("content-range", "").strip())
    if response.status_code != 206 or found is None:
        return None  # (no range answered, or not this one: closed unread)
    read = 0
    async for chunk in response.aiter_bytes():
        begun()
        read += len(chunk)
        if read > MAX_PROBE_BYTES:
            raise DashError("error", "an answer longer than the byte asked for")
    size = int(found[1])
    if size <= 0:
        return None
    etag = response.headers.get("etag", "")
    return Probe(size, etag if _STRONG.fullmatch(etag) else None)


def _refused(response: httpx.Response, limited: Callable[[float | None], None]) -> None:
    """An answer that is no audio: an expired link, "too many requests", an error."""
    status = response.status_code
    if status in (403, 410, 412):
        raise DashError("expired", f"HTTP {status}", status=status)
    if status == 429:
        limited(pacing.retry_after(response.headers.get("retry-after")))
        raise DashError("rate_limited", "rate limited")
    if status >= 500:
        raise _Broken(f"HTTP {status}")


def _check(
    response: httpx.Response, part: mpd.Part, limited: Callable[[float | None], None]
) -> None:
    _refused(response, limited)
    status = response.status_code
    if part.first is None:
        if status != 200:
            raise DashError("failed", f"HTTP {status}")
        return
    span = f"bytes {part.first}-{part.last}/"
    if status != 206 or not response.headers.get("content-range", "").startswith(span):
        raise DashError("failed", f"HTTP {status}, not the byte range asked for")


def _short(length: float, wanted: float, tolerance: float) -> bool:
    """Shorter than the catalog's length by more than the tolerance: a preview."""
    return tolerance > 0 and wanted > 0 and length < wanted - tolerance


async def _process(args: list[str], seconds: float) -> tuple[int, bytes]:
    """Run ``args`` (no input, no output but its messages); killed when its ``seconds`` run
    out or the caller is canceled. (exit status, its last messages)."""
    process = await anyio.open_process(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    said = b""
    try:
        with anyio.fail_after(seconds):
            assert process.stderr is not None
            async for chunk in process.stderr:
                said = (said + chunk)[-4096:]
            code = await process.wait()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):  # (it had ended meanwhile)
            process.kill()
        with anyio.CancelScope(shield=True):
            await process.wait()
            await process.aclose()
        raise
    await process.aclose()
    return code, said


def _inspect(path: Path) -> mpd.Inspected:
    with path.open("rb") as source:
        return mpd.inspect(source)


def _count_samples(path: Path, inspected: mpd.Inspected) -> None:
    """A FLAC copied out of MP4 fragments says nothing of its length (STREAMINFO's total
    samples 0: no length for tag readers, Navidrome's included): the count from the
    fragments' durations is written in."""
    with path.open("r+b") as flac:
        head = flac.read(26)
        found = mpd.flac_samples(head)
        if found is None:
            raise DashError("error", "no FLAC after the remux")
        rate, _ = found  # (a count it states is a claim: the fragments joined are the audio)
        if not rate or not inspected.timescale or not inspected.units:
            return
        flac.seek(0)
        flac.write(mpd.with_samples(head, round(inspected.units * rate / inspected.timescale)))


def _length(path: Path, kind: str) -> float | None:
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError:
        return None
    wanted = {"flac": FLAC, "mp3": MP3}.get(kind, MP4)
    # (A file without tags is falsy: only None is no audio.)
    if audio is None or not isinstance(audio, wanted):
        return None
    return float(audio.info.length)


def _etag(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return f'"{digest.hexdigest()[:32]}"'


def link_key(
    base: str, settings: Mapping[str, str], track_id: str, quality: tuple[str, str]
) -> str:
    """The key of an add-on's track in a quality range: the add-on (its address and
    settings, which may choose what it serves) and its track ID - not the link, which
    changes."""
    parts = [base, *(f"{k}={v}" for k, v in sorted(settings.items())), track_id, *quality]
    return hashlib.sha256("\n".join(parts).encode("utf-8", "surrogatepass")).hexdigest()
