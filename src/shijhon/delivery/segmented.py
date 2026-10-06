"""A DASH link's audio served as one MP4 file from its first segments on.

The file is the chosen representation's init segment, an index of its media segments,
then the media segments in their order:

- the index is a ``sidx`` box as DASH's on-demand profile has one: a reference for each
  media segment, with its size and duration, each starting with a stream access point.
  Players seek in a fragmented MP4 by it, without reading from its start (some cannot
  seek in one at all without it);
- each media segment's size is known before the file is served (``delivery.dash`` asks
  for one byte of each first), so the file's size is exact from its first byte;
- an index a segment carries of its own (some packagers write one into each) becomes a
  ``free`` box of the same size: some players take every index they meet for the file's.

A range is answered from the segments fetched already, or waits for those it covers,
which are fetched first, together with a few after them (``READ_AHEAD``); the rest is
fetched in the background (``Served.fill``), a few at a time, each written into the file
at its place. Once every segment is there, the file is complete: the very bytes that were
served, kept for later ranges and plays.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import anyio.to_thread
from anyio import AsyncFile

READ_AHEAD = 3  # segments fetched first after the one a reader is at
CHUNK = 256 * 1024  # what a reader sends at a time
MAX_REFERENCES = 65535  # an index's references (16 bits)
CONTENT_TYPE = "audio/mp4"  # for every codec (FLAC in MP4 too)
_SAP = 0x90000000  # each reference starts with a stream access point (type 1)
_HEAD = -1  # the init segment and the index, in ``Layout.pieces``


@dataclass(frozen=True)
class Probe:
    """What one byte of a media segment told: its size, and its strong ETag when it has
    one."""

    size: int
    validator: str | None = None


def index_box(
    track_id: int, timescale: int, earliest: int, sizes: Sequence[int], durations: Sequence[int]
) -> bytes:
    """A ``sidx`` box for media segments of ``sizes`` (bytes) and ``durations`` (in
    ``timescale``), the first starting at ``earliest`` and right after the box. Raises
    ``ValueError`` for what such a box cannot say."""
    if not sizes or len(sizes) != len(durations) or len(sizes) > MAX_REFERENCES:
        raise ValueError("no index for this many segments")
    if not 0 < timescale < 2**32 or not 0 < track_id < 2**32 or not 0 <= earliest < 2**64:
        raise ValueError("no index for this track")
    if any(not 0 < size < 2**31 for size in sizes):
        raise ValueError("a segment too large for an index")
    if any(not 0 < duration < 2**32 for duration in durations):
        raise ValueError("a segment's duration an index cannot say")
    version = 0 if earliest < 2**32 else 1
    times = struct.pack(">II" if version == 0 else ">QQ", earliest, 0)  # (no gap after it)
    payload = b"".join(
        [
            bytes([version, 0, 0, 0]),
            struct.pack(">II", track_id, timescale),
            times,
            struct.pack(">HH", 0, len(sizes)),
            *(struct.pack(">III", s, d, _SAP) for s, d in zip(sizes, durations, strict=True)),
        ]
    )
    return struct.pack(">I4s", 8 + len(payload), b"sidx") + payload


def track_id(init: bytes) -> int | None:
    """The ID of the audio track an init segment describes (its first track's header):
    what the index refers to."""
    for kind, _, payload, end in _boxes(init, 0, len(init)):
        if kind != b"moov":
            continue
        for inner, _, at, stop in _boxes(init, payload, end):
            if inner != b"trak":
                continue
            for box, _, start, finish in _boxes(init, at, stop):
                if box == b"tkhd" and finish - start >= 24:
                    # Version 0: 32-bit creation and modification times before it; 1: 64-bit.
                    offset = start + (20 if init[start] == 1 else 12)
                    if offset + 4 <= finish:
                        found = int(struct.unpack_from(">I", init, offset)[0])
                        return found or None
            return None
    return None


def neutral(segment: bytes) -> bytes:
    """A media segment as the file holds it: an index of its own made a ``free`` box of the
    same size. Raises ``ValueError`` for bytes that are not whole boxes holding a movie
    fragment."""
    edited, kinds = _unindexed(segment)
    if b"moof" not in kinds:
        raise ValueError("a segment that is not a movie fragment")
    return edited


def unindexed(init: bytes) -> bytes:
    """An init segment as the file holds it: an index it carries (a byte range of a file
    with one) made a ``free`` box of the same size - the file's own index is the only one.
    Raises ``ValueError`` for bytes that are not whole boxes."""
    return _unindexed(init)[0]


def _unindexed(data: bytes) -> tuple[bytes, set[bytes]]:
    """``data`` with its top-level ``sidx`` and ``ssix`` boxes made ``free``, and the kinds
    of its top-level boxes."""
    edited: bytearray | None = None
    kinds: set[bytes] = set()
    read = 0  # (to where the boxes go)
    for kind, start, _, stop in _boxes(data, 0, len(data)):
        read = stop
        kinds.add(kind)
        if kind in (b"sidx", b"ssix"):
            edited = edited if edited is not None else bytearray(data)
            edited[start + 4 : start + 8] = b"free"
    if read != len(data) or not kinds:
        raise ValueError("bytes that are not whole boxes")
    return (bytes(edited) if edited is not None else data), kinds


def served_etag(head: bytes, validators: Sequence[str | None]) -> str:
    """The file's strong ETag, known before its media segments are: of its first bytes (the
    init segment and the index, which holds every segment's size and duration) and the
    segments' own ETags (those they have). A representation planned again - after a new
    link, or a restart - has the same ETag exactly when it is the same."""
    digest = hashlib.sha256(b"shijhon dash file 1\n")
    digest.update(head)
    for validator in validators:
        digest.update(b"\n" + (validator or "").encode("utf-8", "replace"))
    return f'"{digest.hexdigest()[:32]}"'


@dataclass(frozen=True)
class Layout:
    """Where everything is in the file: ``head`` (the init segment and the index) first,
    then the media segments of ``sizes``."""

    head: bytes
    sizes: tuple[int, ...]
    offsets: tuple[int, ...] = field(init=False)  # where each media segment begins
    total: int = field(init=False)

    def __post_init__(self) -> None:
        offsets, at = [], len(self.head)
        for size in self.sizes:
            offsets.append(at)
            at += size
        object.__setattr__(self, "offsets", tuple(offsets))
        object.__setattr__(self, "total", at)

    def segment_at(self, byte: int) -> int | None:
        """The media segment byte ``byte`` is in (None: in the head)."""
        if byte < len(self.head):
            return None
        return bisect.bisect_right(self.offsets, byte) - 1

    def pieces(self, first: int, last: int) -> list[tuple[int, int, int]]:
        """Bytes ``first`` to ``last`` (within the file) as the parts they are of: (-1 for
        the head, else a media segment's index; the first and the last byte within it)."""
        out: list[tuple[int, int, int]] = []
        head = len(self.head)
        if first < head:
            out.append((_HEAD, first, min(last, head - 1)))
        index = self.segment_at(max(first, head))
        while index is not None and index < len(self.sizes):
            begins = self.offsets[index]
            if begins > last:
                break
            start = max(first, begins) - begins
            out.append((index, start, min(last - begins, self.sizes[index] - 1)))
            index += 1
        return out


class Served:
    """A DASH representation's file, served while its media segments are fetched.

    ``path`` is a file of the whole size with the head written; each media segment is
    written at its place when it is fetched (``fill``). Readers wait for the segments they
    need (``present``); a fill fetches those first. A fill that fails ends with its
    failure, which the readers waiting for it get; a later fill goes on from what is
    there."""

    def __init__(
        self, layout: Layout, *, path: Path, etag: str, kind: str, validators: Sequence[str | None]
    ) -> None:
        self.layout = layout
        self.path = path
        self.etag = etag
        self.kind = kind  # the audio's ("flac", "aac", "alac", "mp3"): what it is copied out as
        self.validators = tuple(validators)
        self.size = layout.total
        self.content_type = CONTENT_TYPE
        self.readers = 0
        self.gone = False  # no longer kept: removed once its last reader is done
        self.kept_at = 0.0  # (its keeper's clock)
        self.filling = False  # a fill is under way (or about to start)
        self._have = bytearray(len(layout.sizes))
        self._missing = len(layout.sizes)
        self._low = 0  # no segment below it is missing
        self._busy: set[int] = set()
        self._at: dict[int, int] = {}  # where readers are (segment: how many)
        self._ended = 0  # fills ended
        self._failure: BaseException | None = None  # the last fill's
        self._changed = anyio.Event()

    @property
    def complete(self) -> bool:
        return self._missing == 0

    def has(self, index: int) -> bool:
        return bool(self._have[index])

    @property
    def fetched(self) -> int:
        return len(self._have) - self._missing

    def started(self) -> None:
        """A fill is to start (its keeper's): readers wait for it, not for another."""
        self.filling = True

    def stopped(self, failure: BaseException) -> None:
        """A fill that was to start did not (``failure``)."""
        self._end(failure)

    async def fill(self, fetch: Callable[[int], Awaitable[bytes]], at_once: int) -> None:
        """Fetches the segments that are not there - those after where readers are first
        (``READ_AHEAD``), then the rest in order - ``at_once`` at a time, until all are
        there. ``fetch``: a segment's bytes, of its size (``neutral`` already). The first
        failure ends it - no other segment is begun, those under way are finished - and is
        raised as itself; one canceled ends too, keeping what it fetched."""
        self.filling = True
        failure: list[BaseException] = []
        try:
            async with anyio.create_task_group() as group:

                async def worker() -> None:
                    # (After a failure no segment is begun; those under way are finished,
                    # a reader may be waiting for one of them.)
                    while not failure and (index := self._next()) is not None:
                        self._busy.add(index)
                        try:
                            data = await fetch(index)
                            if len(data) != self.layout.sizes[index]:
                                raise ValueError("a segment of another size")
                            await anyio.to_thread.run_sync(self._write, index, data)
                            self._got(index)
                        except Exception as exc:  # (raised as itself, not as a group)
                            failure.append(exc)
                        finally:
                            self._busy.discard(index)

                for _ in range(max(1, at_once)):
                    group.start_soon(worker)
        except BaseException:  # canceled (its time is up, a stop): a later fill goes on
            self._end(failure[0] if failure else None)
            raise
        self._end(failure[0] if failure else None)
        if failure:
            raise failure[0]

    async def present(self, index: int, start: Callable[[], None]) -> None:
        """Until media segment ``index`` is in the file. ``start``: called when no fill is
        under way, to start one (it raises when none can start). Raises the failure of a
        fill that ended without the segment after this wait began."""
        if self._have[index]:
            return
        seen = self._ended
        with self.at(index):
            while not self._have[index]:
                if not self.filling:
                    if self._ended > seen and self._failure is not None:
                        raise self._failure
                    start()
                    seen = self._ended
                await self._changed.wait()

    @contextlib.contextmanager
    def at(self, index: int) -> Iterator[None]:
        """A reader is at media segment ``index`` meanwhile: a fill fetches it and the few
        after it first."""
        self._at[index] = self._at.get(index, 0) + 1
        try:
            yield
        finally:
            self._at[index] -= 1
            if not self._at[index]:
                del self._at[index]

    async def chunks(
        self, first: int, last: int, wait: Callable[[int], Awaitable[None]]
    ) -> AsyncIterator[bytes]:
        """Bytes ``first`` to ``last`` of the file, each media segment's as soon as it is
        there (``wait``: until it is - ``present``, with its keeper's way to start a
        fill)."""
        source: AsyncFile[bytes] | None = None
        try:
            for index, start, end in self.layout.pieces(first, last):
                if index == _HEAD:
                    yield self.layout.head[start : end + 1]
                    continue
                with self.at(index):
                    await wait(index)
                    if source is None:  # (unbuffered: what a buffer read ahead may be
                        # older than a segment written since)
                        source = await anyio.open_file(self.path, "rb", buffering=0)
                    await source.seek(self.layout.offsets[index] + start)
                    left = end - start + 1
                    while left > 0:
                        chunk = await source.read(min(CHUNK, left))
                        if not chunk:
                            raise OSError("the file is shorter than its layout")
                        left -= len(chunk)
                        yield chunk
        finally:
            if source is not None:
                with anyio.CancelScope(shield=True):
                    await source.aclose()

    def _next(self) -> int | None:
        """The segment to fetch next: one after where a reader is first, then the first
        that is missing."""
        count = len(self._have)
        for at in sorted(self._at):
            for index in range(at, min(at + READ_AHEAD + 1, count)):
                if not self._have[index] and index not in self._busy:
                    return index
        while self._low < count and self._have[self._low]:
            self._low += 1
        for index in range(self._low, count):
            if not self._have[index] and index not in self._busy:
                return index
        return None

    def _write(self, index: int, data: bytes) -> None:
        with self.path.open("r+b") as out:
            out.seek(self.layout.offsets[index])
            out.write(data)

    def _got(self, index: int) -> None:
        if not self._have[index]:
            self._have[index] = 1
            self._missing -= 1
        self._wake()

    def _end(self, failure: BaseException | None) -> None:
        self.filling = False
        self._ended += 1
        self._failure = failure
        self._wake()

    def _wake(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()


async def probed(
    count: int, probe: Callable[[int], Awaitable[Probe | None]], at_once: int
) -> list[Probe] | None:
    """Every media segment's size and ETag, ``at_once`` probes at a time (``probe``: one
    segment's, None when it tells no size). None as soon as one tells none - the rest are
    not asked then; the first failure is raised as itself."""
    found: list[Probe | None] = [None] * count
    failure: list[BaseException] = []
    sizeless = False
    upcoming = iter(range(count))
    async with anyio.create_task_group() as group:

        async def worker() -> None:
            nonlocal sizeless
            try:
                for index in upcoming:
                    told = await probe(index)
                    if told is None:
                        sizeless = True
                        group.cancel_scope.cancel()
                        return
                    found[index] = told
            except Exception as exc:  # (raised as itself, not as a group)
                failure.append(exc)
                group.cancel_scope.cancel()

        for _ in range(max(1, min(at_once, count))):
            group.start_soon(worker)
    if failure:
        raise failure[0]
    if sizeless:
        return None
    return [told for told in found if told is not None]


def create(path: Path, layout: Layout) -> None:
    """The file of a ``Served``: the head written, the whole size taken (the rest unwritten
    until its segments come)."""
    with path.open("wb") as out:
        out.write(layout.head)
        out.truncate(layout.total)


def _boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int, int]]:
    """The boxes between ``start`` and ``end``: (kind, where it begins, where its payload
    begins, its end). A box that does not fit ends them."""
    at = start
    while at + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, at)
        header = 8
        if size == 1:
            if at + 16 > end:
                return
            size, header = int(struct.unpack_from(">Q", data, at + 8)[0]), 16
        elif size == 0:
            size = end - at
        if size < header or at + size > end:
            return
        yield kind, at, at + header, at + size
        at += size
