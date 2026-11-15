"""Streamed audio held, and kept in the library (``[delivery] keep_streamed_audio``).

An add-on's answer that goes to a client is held in a file of the work folder as it comes:

- **Read on**: an answer that goes to its file's end (also one of a size not told, its
  end told by its framing) is read on to that end while the client keeps its connection -
  also while the client, a player that loaded the song ahead, reads nothing for minutes -
  and the client is fed from what was read: from memory until it is written, then from the
  file (``_ReadOn``). The add-on sends the song once, at its own pace; its connection never
  sits idle until it is closed. A client that leaves stops it: what came is held, nothing
  more is asked of the add-on. The file is never the client's only source: a write that
  fails, or a queue past its bound, only ends the holding, and a disk slower than the
  add-on (``SLOW_BYTES`` waiting to be written) ends it too - from then on the answer is
  read as the client reads it (``MEMORY_BYTES`` ahead), as if it were not read on.
- **Later ranges** of the play - a seek, a player reading on after a pause, a range asked
  again - are served from the file when its written bytes cover them, before any link is
  asked for: also when the song's link expired and no add-on gives that file again
  (``Deliverer._from_held``). Only the play's own file: the one its first answer filled
  (the listener's play, ``PlayFile``); for a play not known (a restart), the same add-on
  track, size and strong ETag, or without one the same link or the same first bytes.
- **Into the library** once every byte is there, while songs are kept: a copy of it, as a
  download's audio is put in place - its real format read from its content, its length the
  catalog's, tagged, confirmed by Navidrome under the same song ID - only while the song is
  a placeholder of the same catalog track, so audio another fetch put in place meanwhile,
  an import's own audio and owned files are never replaced. A catalog song played before it
  is in the library is put in place once it is, within ``WAIT_SECONDS``. Without the
  setting nothing goes into the library, and the file goes like the others.
- **Direct links**: the answers of one representation fill one file together - a play's
  start, its seeks, a resumed play: the same add-on track, size and strong ETag, or without
  an ETag the same link; bytes that overlap must be the same, else nothing is kept. An
  answer whose size is not told is held only when it is the whole file alone, read to the
  end its framing tells (chunked, or HTTP/2); its size is the file's once it ended.
- **DASH links**: their file is Shijhon's already (``delivery/dash.py``): kept from a
  joined file at once, from a file served at once when its fetching has all its segments.
- **Bounded**: ``ENTRIES`` songs held at once (past it the least recently used that no
  answer reads or fills goes), ``HELD`` whole files being copied or put in place or waiting
  for their placeholder, ``STAGED_BYTES`` in all of them (by their told sizes, else what
  came of them; direct and DASH alike), ``QUEUE_BYTES`` waiting to be written (a slow or
  failing disk, or a slow library write, drops the song, never holds playback up), and
  ``IDLE_SECONDS`` unused end one. A file is leased while an answer reads it - from the
  moment it is chosen for a range to the answer's close, also one never read - and is
  neither evicted nor removed until the last lease ends; dropped meanwhile, it still counts
  in the bounds until it is removed. A whole song that finds no place
  to be put in place gets one when a place is free (or at the next sweep); one whose copy
  or write failed is tried again by a later play.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shutil
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import httpx

from shijhon.delivery.dash import Dash, Joined
from shijhon.delivery.download_first import NOT_YET
from shijhon.delivery.length import PEEK_BYTES
from shijhon.delivery.playback import Pin, Track
from shijhon.delivery.segmented import Served

log = logging.getLogger(__name__)

ENTRIES = 16  # songs being kept at once
BUFFER_BYTES = 2 * PEEK_BYTES  # an answer's bytes held before it is known to go to a client
QUEUE_BYTES = 32 * 2**20  # bytes waiting to be written, for all songs together
IDLE_SECONDS = 3600.0  # a song none of whose bytes came for this long is not kept
WAIT_SECONDS = 1800.0  # a whole song waits this long for its placeholder ...
RECHECK_SECONDS = 30.0  # ... looked for this often
HELD = 8  # whole files being copied, put in place or waiting for their placeholder
STAGED_BYTES = 4 * 2**30  # songs' files being filled and whole ones held, in all
DONE_SECONDS = 600.0  # a song handed over is not kept again for this long
DASH_WAITING = 64  # files served at once that plays wait for
SWEEP_SECONDS = 60.0
RETRY_SECONDS = 60.0  # a whole song whose hand-over failed waits this long for the next
READ_BYTES = 256 * 1024  # read at once from a held file for a client
# An answer read on keeps what its client has not taken in memory while it is not yet
# written; past SLOW_BYTES of it the disk is too slow and the answer is no longer held -
# then it is read only MEMORY_BYTES ahead of its client.
SLOW_BYTES = 8 * 2**20
MEMORY_BYTES = 2 * 2**20

Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], Any]
Keep = Callable[[Track, Path, str | None], Awaitable[str | None]]


@dataclass(frozen=True)
class Representation:
    """One file at an add-on: its track there, its size (None: not told) and what tells
    its answers apart from another file's - its strong ETag, else its link."""

    source: int
    track_id: str
    total: int | None
    validator: str


class _Conflict(Exception):
    """Two answers of one representation sent other bytes at the same offsets."""


@dataclass(eq=False)
class _Entry:
    """A song's file being filled from the answers of one representation."""

    track: Track
    representation: Representation
    path: Path
    total: int | None
    used: float
    covered: list[tuple[int, int]] = field(default_factory=list)  # [first, end), merged
    queued: int = 0
    received: int = 0  # bytes taken for it (its share of ``STAGED_BYTES`` when untold)
    fd: int | None = None
    writing: bool = False
    over: bool = False  # dropped
    placing: bool = False  # its whole file was handed over to be put in place
    # Not handed over again before this (its last hand-over failed) - unless a later
    # answer writes to it.
    retry_at: float = 0.0
    # Readers on it - an answer read on into it, answers served from it, a copy for the
    # library: never evicted meanwhile.
    active: int = 0
    # Set (and replaced) when more of it is written, or when it is dropped.
    grew: anyio.Event | None = None

    def notify(self) -> None:
        if self.grew is not None:
            self.grew.set()
        self.grew = None

    def wake(self) -> anyio.Event:
        if self.grew is None:
            self.grew = anyio.Event()
        return self.grew

    def written_to(self, at: int) -> int:
        """Where the bytes written from ``at`` on end (``at``: none there)."""
        for first, end in self.covered:
            if first <= at < end:
                return end
        return at

    def overlaps(self, first: int, end: int) -> list[tuple[int, int]]:
        return [(max(a, first), min(b, end)) for a, b in self.covered if a < end and b > first]

    def add(self, first: int, end: int) -> None:
        merged: list[tuple[int, int]] = []
        for a, b in sorted([*self.covered, (first, end)]):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        self.covered = merged

    @property
    def complete(self) -> bool:
        return bool(self.total) and self.covered == [(0, self.total)]


Close = Callable[[], Awaitable[None]]


class Capture:
    """What an opened answer's audio is kept by, once it goes to a client."""

    def accept(self, track: Track) -> None:
        """The answer goes to a client playing ``track``."""
        raise NotImplementedError

    def read_on(
        self, body: AsyncIterator[bytes], close: Close, first: int, last: int | None
    ) -> tuple[AsyncIterator[bytes], Close] | None:
        """The answer (accepted), going to its file's end (``last``; None: a size not told),
        read on to that end into its file while the client keeps its connection, the client
        fed from what was read: its body and close; None: not read on (as it comes)."""
        return None

    @property
    def representation(self) -> Representation | None:
        """The file held that the answer fills (accepted), or None."""
        return None


class _Answer(Capture):
    """A direct link's answer, its bytes as they come (``tap``) from ``first`` on."""

    @property
    def representation(self) -> Representation | None:
        return self._representation if self._entry is not None else None

    def __init__(self, retention: Retention, representation: Representation, first: int) -> None:
        self._retention = retention
        self._representation = representation
        self._first = self._at = first
        self._held: list[tuple[int, bytes]] = []
        self._holding = 0
        self._entry: _Entry | None = None
        self._dropped = False
        self._ended: bool | None = None  # its end, once it ended: whether read to its end

    async def tap(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        whole = False
        try:
            async for chunk in chunks:
                self._guarded(self._took, chunk)
                yield chunk
            whole = True
        finally:
            self._guarded(self._end, whole)

    def accept(self, track: Track) -> None:
        self._guarded(self._accept, track)

    def read_on(
        self, body: AsyncIterator[bytes], close: Close, first: int, last: int | None
    ) -> tuple[AsyncIterator[bytes], Close] | None:
        entry, retention = self._entry, self._retention
        total = self._representation.total
        if entry is None or self._dropped or entry.over or first < self._first:
            return None
        if (last is None) != (total is None) or (total is not None and last != total - 1):
            return None  # (not to its file's end)
        if retention.spawn is None or not retention.holding:
            return None
        if not retention.reads_on(self._representation.source):
            return None
        reading = _ReadOn(retention, self, entry, first, last)
        retention.lease(entry)  # (the reader's)
        retention.lease(entry)  # (the client's)
        if retention.spawn(partial(reading.read, body, close)) is False:
            retention.release(entry)
            reading.done = True
            reading.release()
            return None
        return reading.body(), reading.closed

    def _guarded(self, step: Callable[[Any], None], value: Any) -> None:
        """A step of keeping the answer: its failure drops it, never the client's audio."""
        try:
            step(value)
        except Exception as exc:
            self._drop()
            log.warning("keeping streamed audio failed: %s", type(exc).__name__)

    def _accept(self, track: Track) -> None:
        if self._dropped or self._entry is not None:
            return
        entry = self._retention._entry_for(track, self._representation)
        if entry is None:
            self._drop()
            return
        self._entry = entry
        held, self._held, self._holding = self._held, [], 0
        for offset, chunk in held:
            if not self._retention._put(entry, offset, chunk):
                self._drop()
                return
        if self._ended is not None:
            self._end(self._ended)

    def _took(self, chunk: bytes) -> None:
        if self._dropped or not chunk:
            return
        offset = self._at
        self._at += len(chunk)
        total = self._representation.total
        if self._at > (total if total is not None else self._retention.max_bytes):
            self._drop()  # more than its file, or a song's file at most: not kept
            return
        if self._entry is None:
            self._held.append((offset, chunk))
            self._holding += len(chunk)
            if self._holding > BUFFER_BYTES:
                self._drop()
        elif not self._retention._put(self._entry, offset, chunk):
            self._dropped = True

    def _end(self, whole: bool) -> None:
        self._ended = whole
        if self._entry is not None and not self._dropped:
            self._retention._ended(self._entry, self._first, self._at if whole else None)

    def _drop(self) -> None:
        self._dropped = True
        self._held, self._holding = [], 0


class _ReadOn:
    """An answer read on to its end while its client keeps its connection: each chunk goes
    into the song's file (``tap``) and stays in memory until the client took it or it is
    written. The client gets the bytes in order from memory, else from the file - never
    only what the disk managed: a write that fails, or a queue past its bound, only ends the
    holding. A disk slower than the add-on ends it too (``SLOW_BYTES`` waiting to be
    written): from then on the answer is read as the client reads (``MEMORY_BYTES`` ahead),
    as an answer that is not read on - the client never waits for the disk."""

    def __init__(
        self, retention: Retention, answer: _Answer, entry: _Entry, first: int, last: int | None
    ) -> None:
        self.retention, self.answer, self.entry = retention, answer, entry
        self.last = last
        self.chunks: deque[tuple[int, bytes]] = deque()  # read, not taken nor written
        self.kept = 0  # their bytes
        self.read_to = first  # the next byte from the add-on
        self.sent_to = first  # the next byte for the client
        self.done = False  # the add-on's answer is over (read to its end, or broke off)
        self.taken: anyio.Event | None = None  # set when the client took bytes
        self.scope = anyio.CancelScope()
        self._released = False

    def holding(self) -> bool:
        return not self.answer._dropped and not self.entry.over

    def _trim(self) -> None:
        holding = self.holding()
        while self.chunks:
            at, data = self.chunks[0]
            end = at + len(data)
            if end <= self.sent_to or (holding and self.entry.written_to(at) >= end):
                self.chunks.popleft()
                self.kept -= len(data)
            else:
                break

    async def read(self, body: AsyncIterator[bytes], close: Close) -> None:
        entry = self.entry
        try:
            with self.scope:
                async for chunk in body:  # (``tap`` puts each chunk into the file)
                    if not chunk:
                        continue
                    self.chunks.append((self.read_to, chunk))
                    self.kept += len(chunk)
                    self.read_to += len(chunk)
                    entry.notify()
                    self._trim()
                    if self.holding() and self.kept > SLOW_BYTES:
                        self.answer._drop()  # the disk is slower than the add-on
                        log.info("%r is no longer held: its bytes come faster than they are"
                                 " written", entry.track.title)  # fmt: skip
                    while not self.holding() and self.kept > MEMORY_BYTES:
                        self.taken = anyio.Event()
                        await self.taken.wait()
                        self._trim()
        except Exception as exc:  # the add-on's answer broke off: what came is there
            log.debug("reading on %r stopped: %s", entry.track.title, type(exc).__name__)
        finally:
            with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                await close()
            self.done = True
            entry.notify()
            self.retention.release(entry)

    def _from_memory(self, at: int) -> bytes | None:
        for first, data in self.chunks:
            if first <= at < first + len(data):
                return data[at - first :]
        return None

    async def body(self) -> AsyncIterator[bytes]:
        entry = self.entry
        fd: int | None = None
        try:
            while self.last is None or self.sent_to <= self.last:
                woken = entry.wake()
                at = self.sent_to
                data = self._from_memory(at)
                if data is None:
                    end = entry.written_to(at)
                    if self.last is not None:
                        end = min(end, self.last + 1)
                    if end > at:
                        if fd is None:
                            fd = await anyio.to_thread.run_sync(os.open, entry.path, os.O_RDONLY)
                        data = await anyio.to_thread.run_sync(
                            os.pread, fd, min(end - at, READ_BYTES), at
                        )
                if data:
                    if self.last is not None:
                        data = data[: self.last + 1 - at]
                    self.sent_to += len(data)
                    entry.used = self.retention.clock()
                    self._trim()
                    if self.taken is not None:
                        self.taken.set()
                    yield data
                    continue
                if self.done and self._from_memory(at) is None and entry.written_to(at) <= at:
                    return  # nothing more comes: the client asks again from here
                await woken.wait()
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            self.release()

    def release(self) -> None:
        if not self._released:
            self._released = True
            self.retention.release(self.entry)

    async def closed(self) -> None:
        """The client's answer is over (or never began): the add-on's is closed (``read``),
        and the file is no longer the client's."""
        self.scope.cancel()
        self.release()


class _DashFile(Capture):
    """A DASH answer, read from the file Shijhon keeps for its link."""

    def __init__(self, retention: Retention, kept: Joined | Served, dash: Dash) -> None:
        self._retention, self._kept, self._dash = retention, kept, dash

    def accept(self, track: Track) -> None:
        try:
            self._retention._dash_played(track, self._kept, self._dash)
        except Exception as exc:  # never the client's audio
            log.warning("keeping streamed audio failed: %s", type(exc).__name__)


class Retention:
    def __init__(
        self,
        keep: Keep,
        work_dir: Path,
        *,
        enabled: bool = False,
        max_bytes: int = 1024 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.keep = keep  # puts a whole file in place (``DownloadFirst.keep_streamed``)
        self.work_dir = work_dir
        self.enabled = enabled  # (a setting; changed at once)
        # Streams held - read on into their files, later ranges served from them - whether
        # or not songs are kept (False, for tests of the add-ons' links alone: as before).
        self.holding = True
        # Whether an add-on's answers may be read on (by its source's ID): not one that asks
        # not to be used for downloads - reading on would ask it for a whole song the app
        # may never play.
        self.reads_on: Callable[[int], bool] = lambda source: True
        self.max_bytes = max_bytes  # a song's file at most, as a download's
        self.clock = clock
        self.spawn: Spawn | None = None  # background work, outliving the request
        self.kept = 0  # songs put in place (observable)
        self._entries: OrderedDict[tuple[str, Representation], _Entry] = OrderedDict()
        # Files dropped while an answer still reads them: on disk until their last lease
        # ends, and counted in the bounds until then.
        self._doomed: set[_Entry] = set()
        self._send, self._receive = anyio.create_memory_object_stream[
            tuple[_Entry, int, bytes | None]
        ](math.inf)
        self._queued = 0
        self._held = 0  # whole files held (``HELD``) ...
        self._held_bytes = 0  # ... and their sizes
        self._done: OrderedDict[str, float] = OrderedDict()
        self._dash: OrderedDict[int, tuple[Served, Dash, list[Track]]] = OrderedDict()
        self._running = False  # its writer runs (``run``): else nothing is kept

    # --- what playback asks -------------------------------------------------------------

    def capture(
        self, pin: Pin, response: httpx.Response, total: int | None, etag: str | None
    ) -> _Answer | None:
        """A direct link's answer to be kept once it goes to a client (``tap`` its body),
        or None: not a file that can be. ``total``: its file's size as it says; ``etag``:
        its strong ETag."""
        if response.status_code == 200:
            first = 0
        elif response.status_code == 206:
            spans = response.headers.get("content-range", "").removeprefix("bytes ")
            begin = spans.split("-", 1)[0]
            if not begin.isdigit():
                return None
            first = int(begin)
        else:
            return None
        if total is not None and not 0 < total <= self.max_bytes:
            return None
        if total is None:
            # Its end must be told by its framing (chunked, or HTTP/2's own): an answer
            # cut off at a closed connection would otherwise look whole.
            framed = "chunked" in response.headers.get("transfer-encoding", "").lower()
            framed = framed or response.http_version in ("HTTP/2", "HTTP/3")
            if response.status_code != 200 or not framed:
                return None
            validator = uuid.uuid4().hex  # (this answer alone)
        else:
            # (An answer without the ETag its link's others gave, of the same size: the
            # same file, as playback holds it.)
            etag = etag or (pin.etag if pin.size == total else None)
            validator = etag or f"link {pin.info.url}"
        return _Answer(self, Representation(pin.source.id, pin.track_id, total, validator), first)

    def lease(self, entry: _Entry) -> None:
        """The file is read (or written, copied) for someone: never evicted nor removed
        until ``release``."""
        entry.active += 1

    def release(self, entry: _Entry) -> None:
        entry.active -= 1
        if entry.active <= 0 and entry.over and not entry.writing:
            self._discard(entry)  # (dropped while it was read: removed now)
        if entry.active <= 0:
            self._retry()

    def held(
        self, names: tuple[str, ...], same: Callable[[Representation], bool], first: int,
        last: int | None,
    ) -> tuple[_Entry, int] | None:  # fmt: skip
        """A file held for one of the song's names, of a representation ``same`` accepts,
        whose written bytes cover ``first`` to ``last`` (None: its end): it (leased: the
        caller releases it, or ``serve`` does) and the last byte."""
        for (key, representation), entry in reversed(self._entries.items()):
            if key not in names or entry.over or not entry.total:
                continue
            end = entry.total - 1 if last is None else min(last, entry.total - 1)
            if first > end or entry.written_to(first) <= end:
                continue
            if same(representation):
                entry.used = self.clock()
                self.lease(entry)
                return entry, end
        return None

    def serve(self, entry: _Entry, first: int, last: int) -> tuple[AsyncIterator[bytes], Close]:
        """A range of a file leased by ``held``, written already: its body, and its close -
        which ends the lease also when the body was never read."""
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.release(entry)

        async def body() -> AsyncIterator[bytes]:
            fd: int | None = None
            try:
                at = first
                while at <= last:
                    if fd is None:
                        fd = await anyio.to_thread.run_sync(os.open, entry.path, os.O_RDONLY)
                    data = await anyio.to_thread.run_sync(
                        os.pread, fd, min(last + 1 - at, READ_BYTES), at
                    )
                    if not data:
                        return
                    at += len(data)
                    entry.used = self.clock()
                    yield data
            finally:
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
                release()

        async def close() -> None:
            release()

        return body(), close

    @staticmethod
    def start_of(entry: _Entry, size: int) -> bytes | None:
        """Its first ``size`` bytes, when written (read in the caller's thread; the caller
        holds a lease)."""
        if entry.written_to(0) < size:
            return None
        with contextlib.suppress(OSError):
            fd = os.open(entry.path, os.O_RDONLY)
            try:
                return os.pread(fd, size, 0)
            finally:
                os.close(fd)
        return None

    def dash(self, kept: Joined | Served, dash: Dash | None) -> Capture | None:
        """A DASH answer, read from ``kept``: kept from that file once it goes to a client."""
        return None if dash is None else _DashFile(self, kept, dash)

    def dash_completed(self, served: Served) -> None:
        """A file served at once has all its segments now (``Dash.completed``): the songs
        whose plays read it are put in place from it."""
        waiting = self._dash.pop(id(served), None)
        if waiting is None or waiting[0] is not served:
            return
        _, dash, tracks = waiting
        for track in tracks:
            self._copy(track, served, dash, served.kind)

    # --- the songs' files ---------------------------------------------------------------

    def _recent(self, key: str) -> bool:
        """Whether the song was handed over to be put in place a moment ago."""
        now = self.clock()
        while self._done and next(iter(self._done.values())) < now - DONE_SECONDS:
            self._done.popitem(last=False)
        return key in self._done

    def _handed(self, key: str) -> None:
        self._done.pop(key, None)
        self._done[key] = self.clock()

    def _entry_for(self, track: Track, representation: Representation) -> _Entry | None:
        """The file a song's answers of ``representation`` fill - whether or not songs are
        kept in the library: an answer read on, and later ranges, are served from it."""
        if not self._running or track.ref is None or not (self.holding or self.enabled):
            return None  # (no catalog track: an owned song played from the add-ons)
        key = (track.key, representation)
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
            entry.used = self.clock()
            return entry
        size = representation.total or 0
        while len(self._entries) + len(self._doomed) >= ENTRIES or (
            self._entries and self._staged() + size > STAGED_BYTES
        ):
            idle = next((e for e in self._entries.values() if not e.active), None)
            if idle is None:
                return None  # (all of them in use)
            self._drop(idle, "more songs are being kept")
        if self._staged() + size > STAGED_BYTES:
            return None  # (whole files held take the room)
        path = self.work_dir / f".{uuid.uuid4().hex}.part"
        entry = _Entry(track, representation, path, representation.total, self.clock())
        self._entries[key] = entry
        return entry

    def _staged(self) -> int:
        """The bytes of the songs' files being filled - their told sizes, else what came of
        them -, of those dropped but still read, and of the whole files held."""
        entries = [*self._entries.values(), *self._doomed]
        filling = sum(max(e.total or 0, e.received) for e in entries)
        return filling + self._held_bytes

    def _hold(self, size: int, beside: _Entry | None = None) -> Callable[[], None] | None:
        """A place for a whole file until it is put in place or goes (the call returned,
        once), or None: too many are held, or too much (``HELD``, ``STAGED_BYTES``). Whole
        files held only for later ranges (idle, not ``beside``: the one being copied) make
        room; files still being filled do not."""
        if self._held >= HELD:
            return None
        while self._staged() + size > STAGED_BYTES:
            idle = next(
                (
                    e
                    for e in self._entries.values()
                    if e.complete and not e.active and e is not beside
                ),
                None,
            )
            if idle is None:
                return None
            self._drop(idle, "a whole song needs the room")
        self._held += 1
        self._held_bytes += size
        done = False

        def release() -> None:
            nonlocal done
            if not done:
                done = True
                self._held -= 1
                self._held_bytes -= size
                self._retry()  # (a whole song waiting for a place gets it)

        return release

    def _put(self, entry: _Entry, offset: int, chunk: bytes) -> bool:
        if entry.over:
            return False
        if not self._running:
            self._drop(entry, "its writer does not run")
            return False
        if self._queued + len(chunk) > QUEUE_BYTES:
            self._drop(entry, "its bytes came faster than they could be written")
            return False
        if entry.total is None and self._staged() + len(chunk) > STAGED_BYTES:
            self._drop(entry, "more songs are being kept than there is room for")
            return False
        entry.received += len(chunk)
        entry.queued += len(chunk)
        self._queued += len(chunk)
        entry.used = self.clock()
        key = (entry.track.key, entry.representation)
        if self._entries.get(key) is entry:
            self._entries.move_to_end(key)  # (the least recently used goes first)
        self._send.send_nowait((entry, offset, chunk))
        return True

    def _ended(self, entry: _Entry, first: int, end: int | None) -> None:
        """An answer for ``entry`` ended (``end``: read to its end there). One whose file's
        size was not told is the whole file only when it was read to its end."""
        if entry.over or entry.total is not None:
            return
        if end is None or first != 0 or end == 0:
            self._drop(entry, "its answer, of a size not told, ended before its end")
            return
        if not self._running:
            self._drop(entry, "its writer does not run")
            return
        self._send.send_nowait((entry, end, None))

    def _drop(self, entry: _Entry, why: str) -> None:
        if entry.over:
            return
        entry.over = True
        key = (entry.track.key, entry.representation)
        if self._entries.get(key) is entry:
            del self._entries[key]
        log.debug("the streamed audio of %r is not kept: %s", entry.track.title, why)
        entry.notify()
        if not entry.writing and not entry.active:
            self._discard(entry)
        else:
            self._doomed.add(entry)  # (removed once its last reader is done: ``release``)

    @staticmethod
    def _close(entry: _Entry) -> None:
        if entry.fd is not None:
            fd, entry.fd = entry.fd, None
            with contextlib.suppress(OSError):
                os.close(fd)

    def _discard(self, entry: _Entry) -> None:
        self._doomed.discard(entry)
        self._close(entry)
        with contextlib.suppress(OSError):
            entry.path.unlink(missing_ok=True)

    def _check(self, entry: _Entry) -> None:
        """A song whose every byte is written is put in place (in the background), while
        songs are kept: a copy of it - the file stays, for the ranges of its play still to
        come, until its place among the held files is needed or it is left idle. One that
        finds no place among the whole files being put in place now gets one when a place
        is free (``_retry``)."""
        if not self.enabled or entry.over or entry.placing or entry.queued or not entry.complete:
            return
        if self._recent(entry.track.key):
            return
        held = self._hold(entry.total or 0, entry)
        if held is None:
            log.debug("the streamed audio of %r waits for a place among the whole songs being"
                      " put in place", entry.track.title)  # fmt: skip
            return
        entry.placing = True  # (its writer's descriptor stays: a later answer may write)
        self._handed(entry.track.key)
        self.lease(entry)
        path = self.work_dir / f".{uuid.uuid4().hex}.part"

        def failed() -> None:  # (a later play, or a free place later, puts it in place)
            entry.placing = False
            entry.retry_at = self.clock() + RETRY_SECONDS
            self._done.pop(entry.track.key, None)

        async def copied() -> None:
            try:
                await anyio.to_thread.run_sync(shutil.copyfile, entry.path, path)
            except OSError as exc:
                path.unlink(missing_ok=True)
                failed()
                held()
                log.info("the streamed audio of %r was not kept: it could not be copied (%s)",
                         entry.track.title, exc.strerror or type(exc).__name__)  # fmt: skip
                return
            finally:
                self.release(entry)
            if not await self._placed(entry.track, path, None, held):
                failed()

        if self.spawn is None or self.spawn(copied) is False:
            failed()
            self.release(entry)
            held()

    def _retry(self) -> None:
        """Whole songs held that wait for a place to be put in place, while songs are kept."""
        if not self.enabled or self._held >= HELD:
            return
        now = self.clock()
        for entry in list(self._entries.values()):
            waiting = entry.complete and not (entry.placing or entry.over or entry.queued)
            if waiting and entry.retry_at <= now:
                self._check(entry)
                if self._held >= HELD:
                    return

    @staticmethod
    def _write(entry: _Entry, offset: int, chunk: bytes, overlaps: list[tuple[int, int]]) -> None:
        if entry.fd is None:
            entry.fd = os.open(entry.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        for first, end in overlaps:
            if os.pread(entry.fd, end - first, first) != chunk[first - offset : end - offset]:
                raise _Conflict
        view, at = memoryview(chunk), offset
        while view:
            written = os.pwrite(entry.fd, view, at)
            view, at = view[written:], at + written

    async def run(self) -> None:
        """Writes the songs' bytes as they come, and ends the songs left idle (for the
        application's lifetime)."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._sweep)
            try:
                await self._writer()
            finally:
                self._running = False  # (stopping, or failed: nothing more is kept)
                tg.cancel_scope.cancel()
                for entry in list(self._entries.values()):
                    self._drop(entry, "Shijhon is stopping")

    async def _writer(self) -> None:
        async with self._receive:
            async for entry, offset, chunk in self._receive:
                if chunk is None:  # an answer of a size not told, read to its end
                    if not entry.over:
                        entry.total = offset
                        self._check(entry)
                    continue
                try:
                    await self._written(entry, offset, chunk)
                finally:
                    # (Counted as waiting until written: a reader of the file waits for it.)
                    self._queued -= len(chunk)
                    entry.queued -= len(chunk)
                    entry.notify()
                self._check(entry)

    async def _written(self, entry: _Entry, offset: int, chunk: bytes) -> None:
        if entry.over:
            return
        end = offset + len(chunk)
        entry.writing = True
        try:
            await anyio.to_thread.run_sync(
                self._write, entry, offset, chunk, entry.overlaps(offset, end)
            )
        except _Conflict:
            entry.writing = False
            self._drop(entry, "two of its answers differ")
            if not entry.active:
                self._discard(entry)  # (also when it was dropped meanwhile)
            return
        except OSError as exc:
            entry.writing = False
            self._drop(entry, f"it could not be written ({exc.strerror or 'OSError'})")
            if not entry.active:
                self._discard(entry)
            return
        finally:
            entry.writing = False
        if entry.over:  # (dropped while it was written)
            if not entry.active:
                self._discard(entry)
            return
        entry.add(offset, end)

    async def _sweep(self) -> None:
        while True:
            await anyio.sleep(SWEEP_SECONDS)
            idle = self.clock() - IDLE_SECONDS
            for entry in list(self._entries.values()):
                if entry.used < idle and not entry.active:
                    self._drop(entry, "none of its bytes came for a while")
            self._retry()

    # --- DASH ---------------------------------------------------------------------------

    def _dash_played(self, track: Track, kept: Joined | Served, dash: Dash) -> None:
        if not self.enabled or track.ref is None or self._recent(track.key):
            return
        if isinstance(kept, Served) and not kept.complete:
            # Put in place once its fetching has every segment (``dash_completed``) - no
            # fetching is started for it.
            waiting = self._dash.pop(id(kept), None)
            tracks = waiting[2] if waiting is not None and waiting[0] is kept else []
            if track not in tracks:
                tracks.append(track)
            self._dash[id(kept)] = (kept, dash, tracks)
            while len(self._dash) > DASH_WAITING:
                self._dash.popitem(last=False)
            return
        self._copy(track, kept, dash, kept.kind if isinstance(kept, Served) else None)

    def _copy(self, track: Track, kept: Joined | Served, dash: Dash, remux: str | None) -> None:
        """A kept DASH file, copied out of the DASH folder (held meanwhile: never removed
        under the copy), put in place for ``track``."""
        if not self.enabled or self._recent(track.key):
            return
        held = self._hold(kept.size)
        if held is None:
            log.debug("the streamed audio of %r is not kept: too many whole songs are being"
                      " put in place", track.title)  # fmt: skip
            return
        release = dash.lease(kept)
        if release is None:
            held()
            return
        self._handed(track.key)
        path = self.work_dir / f".{uuid.uuid4().hex}.part"

        async def copied() -> None:
            try:
                try:
                    self.work_dir.mkdir(parents=True, exist_ok=True)
                    await anyio.to_thread.run_sync(shutil.copyfile, kept.path, path)
                finally:
                    release()
            except OSError as exc:
                path.unlink(missing_ok=True)
                held()
                log.info(
                    "the streamed audio of %r was not kept: its DASH file could not be copied (%s)",
                    track.title,
                    exc.strerror or type(exc).__name__,
                )
                return
            await self._placed(track, path, remux, held)

        if self.spawn is None or self.spawn(copied) is False:
            release()
            held()

    # --- into the library ---------------------------------------------------------------

    def _background(
        self, track: Track, path: Path, remux: str | None, held: Callable[[], None]
    ) -> None:
        async def placed() -> None:
            await self._placed(track, path, remux, held)

        if self.spawn is None or self.spawn(placed) is False:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            held()

    async def _placed(
        self, track: Track, path: Path, remux: str | None, held: Callable[[], None]
    ) -> bool:
        """The whole file in place of the song's placeholder - a catalog song played before
        it is in the library once it is (``WAIT_SECONDS``); ``path`` goes in any case, and
        its place among the files held (``held``) with it."""
        waiting = False
        failure: str | None
        try:
            until = self.clock() + WAIT_SECONDS
            while True:
                if not self.enabled:
                    failure = "keeping streamed songs was switched off"
                    break
                failure = await self.keep(track, path, remux)
                if failure != NOT_YET:
                    break
                if track.song_id != track.ref or self.clock() > until:
                    break  # (no catalog song: an owned song's, an old ID's - never kept)
                waiting = True  # (its file held meanwhile: ``HELD``)
                await anyio.sleep(RECHECK_SECONDS)
            if failure is None:
                self.kept += 1
                log.info("kept the streamed audio of %r in place of its placeholder", track.title)
                return True
            self._done.pop(track.key, None)  # (a later play may keep it after all)
            if failure != NOT_YET or waiting:
                log.info("the streamed audio of %r was not kept: %s", track.title, failure)
        except Exception as exc:  # never more than this song's
            self._done.pop(track.key, None)
            log.warning("keeping streamed audio failed: %s", type(exc).__name__)
        finally:
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(OSError):
                    await anyio.Path(path).unlink(missing_ok=True)
                held()
        return False
