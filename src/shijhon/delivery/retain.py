"""Streamed audio kept in the library (``[delivery] keep_streamed_audio``).

Once Shijhon has received a song's whole audio for a client's stream, that audio replaces
the song's silent placeholder, as a download's does (``DownloadFirst.keep_streamed``):
Navidrome then serves the song - its seeks, other formats and bitrates, offline saves,
shares, the jukebox - and the add-ons are not asked for it again until it goes back to
its placeholder (``[delivery] delivered_days``, ``delivered_gb``). Only what Shijhon
receives anyway is kept: nothing is asked of an add-on to finish a song a client stopped,
and nothing is fetched again for it.

- **Direct links**: an answer's bytes, as they come from the add-on - before a range is
  cut from an answer that ignored ``Range``, the first bytes read to check its length
  included - are held in memory until the answer goes to a client (``accept``; a probe,
  warm-ahead or a fetch for the library is never kept), then written at their offsets
  into a file of the work folder. The answers of one representation fill one file
  together - a play's start, its seeks, a resumed play: the same add-on track, size and
  strong ETag, or without an ETag the same link; bytes that overlap must be the same,
  else nothing is kept. An answer whose size is not told is kept only when it is the
  whole file alone, read to the end its framing tells (chunked, or HTTP/2).
- **DASH links**: their file is Shijhon's already (``delivery/dash.py``): a joined file at
  once, a file served at once when its fetching has all its segments.
- **Into the library** once every byte is there, as a download's audio: its real format
  read from its content, its length the catalog's, tagged, confirmed by Navidrome under
  the same song ID - only while the song is a placeholder of the same catalog track, so
  audio another fetch put in place meanwhile, an import's own audio and owned files are
  never replaced. A catalog song played before it is in the library is put in place
  once it is, within ``WAIT_SECONDS``.
- **Bounded**: ``ENTRIES`` songs being filled at once (past it the least recently used
  goes), ``HELD`` whole files being copied or put in place or waiting for their
  placeholder (more are not kept), ``STAGED_BYTES`` in all of them (by their told sizes,
  else what came of them; direct and DASH alike),
  ``QUEUE_BYTES`` waiting to be written - a slow or failing disk, or a slow library
  write, drops the song, never holds playback up - and ``IDLE_SECONDS`` without its
  bytes end one.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shutil
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
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
    over: bool = False  # dropped, or handed over to be put in place

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


class Capture:
    """What an opened answer's audio is kept by, once it goes to a client."""

    def accept(self, track: Track) -> None:
        """The answer goes to a client playing ``track``."""
        raise NotImplementedError


class _Answer(Capture):
    """A direct link's answer, its bytes as they come (``tap``) from ``first`` on."""

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
        self.max_bytes = max_bytes  # a song's file at most, as a download's
        self.clock = clock
        self.spawn: Spawn | None = None  # background work, outliving the request
        self.kept = 0  # songs put in place (observable)
        self._entries: OrderedDict[tuple[str, Representation], _Entry] = OrderedDict()
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
        if not (self.enabled and self._running) or track.ref is None or self._recent(track.key):
            return None  # (no catalog track: an owned song played from the add-ons)
        key = (track.key, representation)
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
            entry.used = self.clock()
            return entry
        while len(self._entries) >= ENTRIES:
            oldest = next(iter(self._entries.values()))
            self._drop(oldest, "more songs are being kept")
        size = representation.total or 0
        while self._entries and self._staged() + size > STAGED_BYTES:
            oldest = next(iter(self._entries.values()))
            self._drop(oldest, "more songs are being kept")
        if self._staged() + size > STAGED_BYTES:
            return None  # (whole files held take the room)
        path = self.work_dir / f".{uuid.uuid4().hex}.part"
        entry = _Entry(track, representation, path, representation.total, self.clock())
        self._entries[key] = entry
        return entry

    def _staged(self) -> int:
        """The bytes of the songs' files being filled - their told sizes, else what came of
        them - and of the whole files held."""
        filling = sum(max(e.total or 0, e.received) for e in self._entries.values())
        return filling + self._held_bytes

    def _hold(self, size: int) -> Callable[[], None] | None:
        """A place for a whole file until it is put in place or goes (the call returned,
        once), or None: too many are held, or too much (``HELD``, ``STAGED_BYTES``)."""
        if self._held >= HELD or self._staged() + size > STAGED_BYTES:
            return None
        self._held += 1
        self._held_bytes += size
        done = False

        def release() -> None:
            nonlocal done
            if not done:
                done = True
                self._held -= 1
                self._held_bytes -= size

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
        if not entry.writing:
            self._discard(entry)

    @staticmethod
    def _close(entry: _Entry) -> None:
        if entry.fd is not None:
            fd, entry.fd = entry.fd, None
            with contextlib.suppress(OSError):
                os.close(fd)

    def _discard(self, entry: _Entry) -> None:
        self._close(entry)
        with contextlib.suppress(OSError):
            entry.path.unlink(missing_ok=True)

    def _check(self, entry: _Entry) -> None:
        """A song whose every byte is written is put in place (in the background)."""
        if not self.enabled:
            self._drop(entry, "keeping streamed songs was switched off")
            return
        if entry.over or entry.queued or not entry.complete:
            return
        key = (entry.track.key, entry.representation)
        if self._entries.get(key) is entry:
            del self._entries[key]  # (its size is held from here, or it goes)
        held = self._hold(entry.total or 0)
        if held is None:
            self._drop(entry, "too many whole songs are being put in place")
            return
        entry.over = True
        self._close(entry)
        self._handed(entry.track.key)
        self._background(entry.track, entry.path, None, held)

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
                self._queued -= len(chunk)
                entry.queued -= len(chunk)
                if entry.over:
                    continue
                end = offset + len(chunk)
                entry.writing = True
                try:
                    await anyio.to_thread.run_sync(
                        self._write, entry, offset, chunk, entry.overlaps(offset, end)
                    )
                except _Conflict:
                    entry.writing = False
                    self._drop(entry, "two of its answers differ")
                    self._discard(entry)  # (also when it was dropped meanwhile)
                    continue
                except OSError as exc:
                    entry.writing = False
                    self._drop(entry, f"it could not be written ({exc.strerror or 'OSError'})")
                    self._discard(entry)
                    continue
                finally:
                    entry.writing = False
                if entry.over:  # (dropped while it was written)
                    self._discard(entry)
                    continue
                entry.add(offset, end)
                self._check(entry)

    async def _sweep(self) -> None:
        while True:
            await anyio.sleep(SWEEP_SECONDS)
            idle = self.clock() - IDLE_SECONDS
            for entry in list(self._entries.values()):
                if entry.used < idle:
                    self._drop(entry, "none of its bytes came for a while")

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
    ) -> None:
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
                return
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
