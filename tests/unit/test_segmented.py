"""The DASH file served from its first segments on (``delivery.segmented``): its index box,
what a range is made of, its ETag, a segment's own index made a ``free`` box; the file
assembled from ffmpeg's made-up DASH audio, read by ffprobe (its length) and ffmpeg (a
seek by the index, the audio sample for sample); and a fill behind a made-up fetch - the
first bytes before later segments, a reader's segments first, a few at once, a failure
for the readers waiting and a later fill going on from what is there."""

from __future__ import annotations

import contextlib
import re
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest

from shijhon.delivery import segmented
from shijhon.delivery.segmented import (
    Layout,
    Probe,
    Served,
    index_box,
    neutral,
    served_etag,
    track_id,
)
from tests.harness.dash_fixtures import FLAC, dash_audio, segment_names
from tests.harness.library import frequency_for, tone


def parse_index(box: bytes) -> dict[str, object]:
    """A ``sidx`` box's fields, read as ISO/IEC 14496-12 lays them out."""
    size, kind = struct.unpack_from(">I4s", box)
    assert kind == b"sidx" and size == len(box)
    version = box[8]
    track, timescale = struct.unpack_from(">II", box, 12)
    if version == 0:
        earliest, first, at = *struct.unpack_from(">II", box, 20), 28
    else:
        earliest, first, at = *struct.unpack_from(">QQ", box, 20), 36
    reserved, count = struct.unpack_from(">HH", box, at)
    refs = [struct.unpack_from(">III", box, at + 4 + 12 * n) for n in range(count)]
    assert at + 4 + 12 * count == len(box) and reserved == 0
    return {
        "version": version,
        "track": track,
        "timescale": timescale,
        "earliest": earliest,
        "first_offset": first,
        "sizes": [r[0] & 0x7FFFFFFF for r in refs],
        "types": [r[0] >> 31 for r in refs],
        "durations": [r[1] for r in refs],
        "sap": [(r[2] >> 31, (r[2] >> 28) & 7, r[2] & 0x0FFFFFFF) for r in refs],
    }


def test_the_index_has_a_reference_for_each_segment_starting_with_a_sap() -> None:
    box = index_box(1, 44100, 0, [100, 200, 300], [88200, 88200, 4410])
    fields = parse_index(box)
    assert fields == {
        "version": 0,
        "track": 1,
        "timescale": 44100,
        "earliest": 0,
        "first_offset": 0,
        "sizes": [100, 200, 300],
        "types": [0, 0, 0],  # media, not another index
        "durations": [88200, 88200, 4410],
        "sap": [(1, 1, 0)] * 3,
    }
    later = parse_index(index_box(2, 48000, 2**33, [5], [7]))
    assert later["version"] == 1 and later["earliest"] == 2**33 and later["track"] == 2


@pytest.mark.parametrize(
    "args",
    [
        (1, 44100, 0, [], []),
        (1, 44100, 0, [1, 2], [1]),
        (0, 44100, 0, [1], [1]),
        (1, 0, 0, [1], [1]),
        (1, 44100, 0, [2**31], [1]),
        (1, 44100, 0, [0], [1]),
        (1, 44100, 0, [1], [2**32]),
        (1, 44100, 0, [1] * 65536, [1] * 65536),
    ],
    ids=["none", "unequal", "no-track", "no-timescale", "huge", "empty", "long", "many"],
)
def test_an_index_that_cannot_say_it_is_refused(args: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        index_box(*args)  # type: ignore[arg-type]


def test_a_range_is_mapped_onto_the_head_and_the_segments() -> None:
    layout = Layout(b"H" * 10, (5, 7, 3))  # the file: 0-9 head, 10-14, 15-21, 22-24
    assert layout.total == 25 and layout.offsets == (10, 15, 22)
    assert layout.pieces(0, 0) == [(-1, 0, 0)]
    assert layout.pieces(0, 24) == [(-1, 0, 9), (0, 0, 4), (1, 0, 6), (2, 0, 2)]
    assert layout.pieces(12, 16) == [(0, 2, 4), (1, 0, 1)]  # mid-file: two segments only
    assert layout.pieces(15, 21) == [(1, 0, 6)]  # one whole segment, exactly
    assert layout.pieces(24, 24) == [(2, 2, 2)]  # the last byte
    assert layout.pieces(9, 10) == [(-1, 9, 9), (0, 0, 0)]
    assert [layout.segment_at(b) for b in (0, 9, 10, 14, 15, 24)] == [None, None, 0, 0, 1, 2]


def test_the_etag_changes_with_the_head_and_the_validators() -> None:
    head = b"init" + index_box(1, 1000, 0, [10, 20], [1000, 1000])
    same = served_etag(head, [None, None])
    assert same == served_etag(head, [None, None]) and re.fullmatch(r'"[0-9a-f]{32}"', same)
    other_sizes = b"init" + index_box(1, 1000, 0, [10, 21], [1000, 1000])
    assert served_etag(other_sizes, [None, None]) != same
    assert served_etag(head, ['"a"', '"b"']) != same
    assert served_etag(head, ['"a"', '"b"']) != served_etag(head, ['"a"', '"c"'])


def test_a_segment_s_own_index_becomes_a_free_box_of_its_size() -> None:
    folder = dash_audio("seg-neutral")
    data = (folder / segment_names(folder, FLAC)[0]).read_bytes()
    assert b"sidx" in data[:200]  # ffmpeg's dash muxer writes one into each segment
    edited = neutral(data)
    assert len(edited) == len(data) and b"sidx" not in edited[:200] and b"free" in edited[:200]
    assert neutral(edited) == edited
    plain = struct.pack(">I4s", 16, b"moof") + bytes(8) + struct.pack(">I4s", 12, b"mdat") + b"abcd"
    assert neutral(plain) is plain  # nothing to change: the bytes as they are
    for broken in (data[:-1], struct.pack(">I4s", 12, b"mdat") + b"abcd", b""):
        with pytest.raises(ValueError):
            neutral(broken)


def test_the_track_id_is_read_from_the_init_segment() -> None:
    folder = dash_audio("seg-track")
    assert track_id((folder / f"init-stream{FLAC}.m4s").read_bytes()) == 1
    assert track_id(b"") is None and track_id(b"\x00\x00\x00\x08moov") is None


def assembled(folder: Path, representation: str) -> tuple[Layout, list[bytes]]:
    """The served file's layout for one representation of ffmpeg's made-up DASH audio."""
    text = (folder / "out.mpd").read_text()
    block = re.search(rf'<Representation id="{representation}".*?</Representation>', text, re.S)
    assert block is not None
    timescale = int(re.findall(r'timescale="(\d+)"', block[0])[0])
    durations: list[int] = []
    for d, r in re.findall(r'<S (?:t="\d+" )?d="(\d+)"(?: r="(\d+)")? />', block[0]):
        durations += [int(d)] * (int(r or 0) + 1)
    init = (folder / f"init-stream{representation}.m4s").read_bytes()
    segments = [neutral((folder / n).read_bytes()) for n in segment_names(folder, representation)]
    tid = track_id(init)
    assert tid is not None
    sizes = [len(s) for s in segments]
    head = init + index_box(tid, timescale, 0, sizes, durations)
    return Layout(head, tuple(sizes)), segments


def test_the_assembled_file_tells_its_length_and_seeks_by_its_index(tmp_path: Path) -> None:
    folder = dash_audio("seg-ffprobe", 20)
    layout, segments = assembled(folder, FLAC)
    path = tmp_path / "served.mp4"
    path.write_bytes(layout.head + b"".join(segments))
    assert path.stat().st_size == layout.total
    said = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert abs(float(said) - 20) < 0.05  # (each segment's own index would have said 2 s)
    # The audio is the tone's, sample for sample (copied, never converted).
    source = tone(frequency_for("seg-ffprobe"), 20, "flac")
    assert pcm(path) == pcm(source)
    # A seek to 14 s decodes from there: the index's segment, not from the start.
    sought = pcm(path, "-ss", "14")
    assert 5.5 * 44100 * 4 < len(sought) < 6.5 * 44100 * 4


def pcm(path: Path, *before: str) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", *before, "-i", str(path), "-f", "s16le",
         "-ac", "2", "-ar", "44100", "pipe:1"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip


# --- a fill behind a made-up fetch ----------------------------------------------------


def served(tmp_path: Path, count: int = 12, size: int = 1000) -> Served:
    layout = Layout(b"HEAD" * 4, tuple(size for _ in range(count)))
    path = tmp_path / "served.mp4"
    segmented.create(path, layout)
    return Served(layout, path=path, etag='"x"', kind="flac", validators=[None] * count)


def body(index: int, size: int = 1000) -> bytes:
    return bytes([index % 256]) * size


class Fetch:
    """Segment ``n`` is ``n`` repeated; ``hold``: segments that wait for ``release``;
    ``fail``: segments that raise."""

    def __init__(self, delay: float = 0.0) -> None:
        self.order: list[int] = []
        self.delay = delay
        self.at_once = 0
        self.most = 0
        self.hold: set[int] = set()
        self.released = anyio.Event()
        self.fail: dict[int, Exception] = {}

    async def __call__(self, index: int) -> bytes:
        self.order.append(index)
        self.at_once += 1
        self.most = max(self.most, self.at_once)
        try:
            if index in self.hold:
                await self.released.wait()
            await anyio.sleep(self.delay)
            if index in self.fail:
                raise self.fail.pop(index)
            return body(index)
        finally:
            self.at_once -= 1


def starter(
    file: Served, fetch: Fetch, group: anyio.abc.TaskGroup, at_once: int = 2
) -> Callable[[], None]:
    """How a keeper starts a fill (its failure is what the readers waiting get)."""

    def start() -> None:
        file.started()

        async def run() -> None:
            with contextlib.suppress(Exception):
                await file.fill(fetch, at_once)

        group.start_soon(run)

    return start


@pytest.mark.anyio
async def test_a_fill_writes_every_segment_at_its_place_a_few_at_once(tmp_path: Path) -> None:
    file = served(tmp_path)
    fetch = Fetch(delay=0.01)
    await file.fill(fetch, 3)
    assert file.complete and fetch.most == 3
    assert fetch.order == list(range(12))  # in order, with no reader anywhere
    assert file.path.read_bytes() == file.layout.head + b"".join(body(n) for n in range(12))


@pytest.mark.anyio
async def test_the_first_bytes_go_out_before_later_segments_are_there(tmp_path: Path) -> None:
    file = served(tmp_path)
    fetch = Fetch()
    fetch.hold = {1}
    got: list[bytes] = []
    async with anyio.create_task_group() as group:
        start = starter(file, fetch, group, at_once=1)

        async def wait(index: int) -> None:
            await file.present(index, start)

        async def read() -> None:
            async for chunk in file.chunks(0, file.size - 1, wait):
                got.append(chunk)

        group.start_soon(read)
        with anyio.fail_after(5):
            while len(got) < 2:  # noqa: ASYNC110
                await anyio.sleep(0.01)
        # The head and segment 0 are out; segment 1 is not there yet.
        assert got == [file.layout.head, body(0)] and not file.has(1)
        fetch.released.set()
    assert b"".join(got) == file.layout.head + b"".join(body(n) for n in range(12))


@pytest.mark.anyio
async def test_a_reader_s_segments_are_fetched_first(tmp_path: Path) -> None:
    """A seek to segment 8 while a fill goes on in order: 8 and the few after it next, while
    the reader is there."""
    file = served(tmp_path)
    fetch = Fetch(delay=0.02)
    async with anyio.create_task_group() as group:
        start = starter(file, fetch, group, at_once=1)
        start()
        await anyio.sleep(0.05)  # a few in order meanwhile

        async def wait(index: int) -> None:
            await file.present(index, start)

        offset = file.layout.offsets[8]
        with anyio.fail_after(5):
            data = b"".join([c async for c in file.chunks(offset, file.size - 1, wait)])
        assert data == b"".join(body(n) for n in range(8, 12))
    sought = fetch.order.index(8)
    assert all(n < 8 for n in fetch.order[:sought])  # in order until the reader came
    assert fetch.order[sought : sought + 4] == [8, 9, 10, 11]
    assert sorted(fetch.order) == list(range(12)) and file.complete


@pytest.mark.anyio
async def test_a_mid_file_range_waits_only_for_its_segments(tmp_path: Path) -> None:
    file = served(tmp_path)
    fetch = Fetch()
    fetch.hold = set(range(12)) - {5, 6}
    async with anyio.create_task_group() as group:
        start = starter(file, fetch, group, at_once=2)

        async def wait(index: int) -> None:
            await file.present(index, start)

        offset = file.layout.offsets[5] + 10
        with anyio.fail_after(5):
            data = b"".join([c async for c in file.chunks(offset, offset + 1500, wait)])
        assert data == (body(5) + body(6))[10:1511]
        fetch.released.set()


@pytest.mark.anyio
async def test_a_failure_reaches_the_readers_and_a_later_fill_goes_on(tmp_path: Path) -> None:
    file = served(tmp_path)
    fetch = Fetch()
    fetch.fail = {3: RuntimeError("HTTP 503, asked twice")}
    with pytest.raises(RuntimeError, match="asked twice"):
        await file.fill(fetch, 1)
    assert not file.filling and [file.has(n) for n in range(4)] == [True, True, True, False]
    fetch.order.clear()
    fetch.fail = {6: RuntimeError("HTTP 403")}
    async with anyio.create_task_group() as group:
        start = starter(file, fetch, group, at_once=1)
        with pytest.raises(RuntimeError, match="403"), anyio.fail_after(5):
            await file.present(6, start)  # its fill fails: the reader waiting gets it
        assert fetch.order == [6]  # the reader's segment first; nothing fetched twice
        with anyio.fail_after(5):
            await file.present(6, start)  # a new fill
    assert 0 not in fetch.order and fetch.order.count(6) == 2


@pytest.mark.anyio
async def test_a_canceled_fill_keeps_what_it_fetched_and_a_reader_starts_another(
    tmp_path: Path,
) -> None:
    file = served(tmp_path)
    fetch = Fetch(delay=0.02)
    with anyio.move_on_after(0.07):
        await file.fill(fetch, 1)
    kept = file.fetched
    assert 0 < kept < 12 and not file.filling
    async with anyio.create_task_group() as group:
        start = starter(file, fetch, group, at_once=4)
        with anyio.fail_after(5):
            await file.present(11, start)
    assert file.complete
    assert file.path.read_bytes() == file.layout.head + b"".join(body(n) for n in range(12))


# --- the sizes asked for first ----------------------------------------------------------


@pytest.mark.anyio
async def test_every_size_is_asked_for_a_few_at_once_in_order() -> None:
    asked: list[int] = []
    now = {"at_once": 0, "most": 0}

    async def probe(index: int) -> Probe:
        asked.append(index)
        now["at_once"] += 1
        now["most"] = max(now["most"], now["at_once"])
        await anyio.sleep(0.01)
        now["at_once"] -= 1
        return Probe(100 + index, f'"{index}"')

    found = await segmented.probed(30, probe, 8)
    assert found == [Probe(100 + n, f'"{n}"') for n in range(30)]
    assert now["most"] == 8 and asked == list(range(30))


@pytest.mark.anyio
async def test_a_segment_without_a_size_ends_the_probes() -> None:
    asked: list[int] = []

    async def probe(index: int) -> Probe | None:
        asked.append(index)
        await anyio.sleep(0.01)
        return None if index == 3 else Probe(10)

    assert await segmented.probed(40, probe, 2) is None
    assert len(asked) < 10  # the rest not asked

    async def failing(index: int) -> Probe:
        if index == 2:
            raise RuntimeError("not allowed by the network policy")
        return Probe(10)

    with pytest.raises(RuntimeError, match="network policy"):
        await segmented.probed(5, failing, 8)


def test_an_index_in_the_init_segment_becomes_a_free_box() -> None:
    folder = dash_audio("seg-init-index")
    init = (folder / f"init-stream{FLAC}.m4s").read_bytes()
    index = index_box(1, 1000, 0, [10], [1000])
    edited = segmented.unindexed(init + index)
    assert len(edited) == len(init + index) and edited.startswith(init)
    assert edited[len(init) + 4 : len(init) + 8] == b"free"
    assert segmented.unindexed(init) == init
    with pytest.raises(ValueError):
        segmented.unindexed(init[:-3])


@pytest.mark.anyio
async def test_a_failure_lets_the_segments_under_way_finish(tmp_path: Path) -> None:
    """Segment 1 fails at once while segment 0, which a reader waits for, is still coming:
    the reader gets segment 0, and no segment after them is begun."""
    file = served(tmp_path)
    fetch = Fetch()
    fetch.hold = {0}
    fetch.fail = {1: RuntimeError("HTTP 503, asked twice")}
    async with anyio.create_task_group() as group:
        start = starter(file, fetch, group, at_once=2)
        group.start_soon(lambda: file.present(0, start))
        await anyio.sleep(0.05)  # segment 1 has failed meanwhile
        fetch.released.set()
        with anyio.fail_after(5):
            await file.present(0, start)
    assert file.has(0) and not file.has(1)
    assert sorted(fetch.order) == [0, 1]  # nothing begun after the failure
