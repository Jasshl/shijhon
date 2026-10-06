"""Suite K (continued) — DASH songs start at once.

An add-on's DASH link (made-up audio: ffmpeg's dash muxer over a synthetic tone, the
segments on another origin than the manifest) is served from its first segments on, as
one MP4 file of known size: its init segment, an index of its media segments (``sidx``),
the segments. Each segment's size is asked for first (one byte each, a few at once, as
audio openings at the add-on); a range waits only for the segments it covers, and the rest
is fetched in the background into the kept file, which is byte for byte the one served.
Segments without a size fall back to the complete file (as ``dash_start = "complete"``,
which is unchanged: ``test_K_dash.py``); download-first still places a native FLAC or a
fast-start M4A in the library.
"""

from __future__ import annotations

import logging
import struct
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from defusedxml import ElementTree

from shijhon.delivery.dash import Dash
from shijhon.delivery.pacing import Limits
from shijhon.delivery.playback import Track
from shijhon.delivery.segmented import neutral
from tests.conftest import NavidromeFactory
from tests.harness.dash_fixtures import FLAC as FLAC_ID
from tests.harness.dash_fixtures import dash_audio, segment_names
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import ADDON_HOST, CDN_HOST, FakeAddon, FakeTrack
from tests.harness.library import frequency_for, tone
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

SECONDS = 6  # three segments of two seconds
LONG = 20  # ten


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("dash-stream"),
        length_tolerance_seconds=1.0,
        budget_seconds=4.0,
        warm_ahead_depth=1,
        warm_ahead_delay_seconds=0.2,
    ) as w:
        yield w


@pytest.fixture
def a_and_b(world: DeliveryWorld) -> Iterator[tuple[FakeAddon, FakeAddon]]:
    """A (whose links are DASH) before B (a direct FLAC), as the only sources."""
    world.clear_sources()
    a, b = world.addon("A"), world.addon("B")
    world.add_source(a)
    world.add_source(b)
    yield a, b
    world.clear_sources()


@pytest.fixture
def dash(world: DeliveryWorld) -> Iterator[Dash]:
    joiner = world.services.deliverer.dash
    assert joiner is not None
    settings = world.services.deliverer.settings
    assert settings.dash_start == "at_once"  # the default
    yield joiner
    settings.dash_start = "at_once"
    settings.dash_quality_from, settings.dash_quality_to = "any", "lossless"
    settings.dash_segments_at_once = 4


def dash_song(
    world: DeliveryWorld,
    key: str,
    a: FakeAddon,
    b: FakeAddon | None = None,
    *,
    seconds: float = SECONDS,
    catalog_seconds: float | None = None,
    layout: str = "timeline",
    sets: int = 1,
    **dash: Any,
) -> tuple[str, FakeTrack, Path]:
    """A one-track catalog song: A's link to it is DASH, B's (when given) a direct FLAC.
    Returns (song ID, A's track, its DASH folder)."""
    song, fakes, _ = world.placeholder_track(
        key, [b] if b is not None else [], seconds=catalog_seconds or seconds
    )
    if fakes:
        isrc = next(iter(fakes.values())).isrc
    else:
        row = world.server.call(
            lambda: world.services.store.fetchone(
                "SELECT isrc FROM placeholders WHERE song_id = ?", [song]
            )
        )
        assert row is not None
        isrc = str(row["isrc"])
    folder = dash_audio(key, seconds, layout, sets)
    fake = a.add(FakeTrack(isrc=isrc, audio=folder / "out.mpd", dash=folder, **dash))
    return song, fake, folder


def stream(
    world: DeliveryWorld, song: str, headers: dict[str, str] | None = None, **params: object
) -> httpx.Response:
    return world.client().request("stream", {"id": song, **params}, headers=headers)


def failed(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json") and (
        response.json()["subsonic-response"]["status"] == "failed"
    )


def pcm(data: bytes | Path, *before: str) -> bytes:
    """Decoded audio (16-bit stereo samples at 44.1 kHz)."""
    source = ["-i", str(data)] if isinstance(data, Path) else ["-i", "pipe:0"]
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", *before, *source, "-f", "s16le", "-ac", "2",
         "-ar", "44100", "pipe:1"],
        input=None if isinstance(data, Path) else data, capture_output=True, check=True,
    ).stdout  # fmt: skip


def boxes(data: bytes) -> list[tuple[bytes, int, int]]:
    """Top-level boxes: (kind, start, size)."""
    out, at = [], 0
    while at + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, at)
        out.append((kind, at, size))
        at += size
    return out


def expected(folder: Path, representation: str = FLAC_ID) -> tuple[bytes, list[bytes]]:
    """A representation's init segment and media segments as the file holds them."""
    init = (folder / f"init-stream{representation}.m4s").read_bytes()
    names = segment_names(folder, representation)
    return init, [neutral((folder / name).read_bytes()) for name in names]


def timeline(folder: Path, representation: str = FLAC_ID) -> list[int]:
    """The segments' durations in the manifest ffmpeg wrote (its version decides where it
    cuts), in the representation's timescale."""
    root = ElementTree.parse(folder / "out.mpd").getroot()
    ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
    for rep in root.iterfind(".//d:Representation", ns):
        if rep.get("id") == representation:
            entries = rep.find("d:SegmentTemplate/d:SegmentTimeline", ns)
            assert entries is not None
            return [
                int(s.get("d", "0"))
                for s in entries.iterfind("d:S", ns)
                for _ in range(1 + int(s.get("r", "0")))
            ]
    raise AssertionError(f"no representation {representation} in the manifest")


def media(a: FakeAddon) -> list[dict[str, Any]]:
    """The add-on's segment requests that fetched audio (not a size)."""
    return [r for r in a.requests("segment") if not r["probe"] and not r["name"].startswith("init")]


def probes(a: FakeAddon) -> list[dict[str, Any]]:
    return [r for r in a.requests("segment") if r["probe"]]


def complete(dash: Dash, timeout: float = 10) -> None:
    """Until every DASH file being served is complete (and none is being planned)."""
    deadline = time.monotonic() + timeout
    while dash._planning or any(not s.file.complete for s in dash._served.values()):
        assert time.monotonic() < deadline, "the background fill did not end"
        time.sleep(0.05)


def test_a_dash_song_is_one_mp4_file_init_index_segments(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, folder = dash_song(world, "s-basic", a)
    whole = stream(world, song)
    assert whole.status_code == 200
    assert whole.headers["content-type"] == "audio/mp4"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-length"] == str(len(whole.content))
    etag = whole.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/")
    init, segments = expected(folder)
    data = whole.content
    assert data.startswith(init)
    kinds = [kind for kind, _, _ in boxes(data[len(init) :])]
    assert kinds[0] == b"sidx" and b"sidx" not in kinds[1:]  # one index: the file's
    index = data[len(init) : len(init) + boxes(data[len(init) :])[0][2]]
    assert data == init + index + b"".join(segments)
    # One reference a segment: its size, its duration (the timeline's), a SAP at its start.
    count = struct.unpack_from(">H", index, 30)[0]
    refs = [struct.unpack_from(">III", index, 32 + 12 * n) for n in range(count)]
    assert [r[0] for r in refs] == [len(s) for s in segments]
    assert [r[1] for r in refs] == timeline(folder) and {r[2] for r in refs} == {0x90000000}
    assert pcm(data) == pcm(tone(frequency_for("s-basic"), SECONDS, "flac").read_bytes())
    for header, wanted in (
        ("bytes=0-99", data[:100]),
        ("bytes=1000-", data[1000:]),
        ("bytes=-64", data[-64:]),
        ("bytes=10-10", data[10:11]),
    ):
        part = stream(world, song, {"range": header})
        assert part.status_code == 206 and part.content == wanted, header
        assert part.headers["content-range"].endswith(f"/{len(data)}")
        assert part.headers["etag"] == etag
    past = stream(world, song, {"range": f"bytes={len(data) + 10}-"})
    assert past.status_code == 416 and past.headers["content-range"] == f"bytes */{len(data)}"
    head = world.client().request("stream", {"id": song}, http_method="HEAD")
    assert head.status_code == 200 and head.content == b""
    for name in ("content-length", "content-type", "accept-ranges", "etag"):
        assert head.headers[name] == whole.headers[name], name
    # The manifest once; each media segment's size once (one byte), and its audio once.
    assert len(a.requests("mpd")) == 1
    assert sorted(r["name"] for r in probes(a)) == segment_names(folder, FLAC_ID)
    assert all(r["range"] == "bytes=0-0" for r in probes(a))
    assert sorted(r["name"] for r in media(a)) == segment_names(folder, FLAC_ID)
    assert {r["origin"] for r in a.requests("segment")} == {"cdn"}


def test_the_first_byte_does_not_wait_for_later_segments(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    folder = dash_audio("s-first", LONG)
    names = segment_names(folder, FLAC_ID)
    slow = {name: 2.5 for name in names[2:]}  # every segment after the second is slow
    song, _, _ = dash_song(world, "s-first", a, seconds=LONG, segment_delays=slow)
    _, segments = expected(folder)
    later = sum(len(s) for s in segments[2:])
    started = time.monotonic()
    response = world.client().request("stream", {"id": song}, stream=True)
    try:
        assert response.status_code == 200
        chunks = response.iter_raw()
        first = next(chunks)
        at_first = time.monotonic() - started
        got = len(first)
        while got < int(response.headers["content-length"]) - later:  # all but the slow ones
            got += len(next(chunks))
        at_more = time.monotonic() - started
        assert at_first < 1.5 and at_more < 1.5  # the first segments, before the slow ones
        rest = b"".join(chunks)
    finally:
        response.close()
    assert got + len(rest) == int(response.headers["content-length"])
    assert time.monotonic() - started > 2.0  # (the slow segments came later)


@pytest.mark.parametrize("where", ["mid-file", "suffix"])
def test_a_range_is_answered_with_only_the_segments_it_covers(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, where: str
) -> None:
    """Every segment but those the range covers takes 3 s: its answer does not wait for
    them (they are fetched first, before the rest)."""
    a, _ = a_and_b
    key = f"s-range-{where}"
    folder = dash_audio(key, LONG)
    names = segment_names(folder, FLAC_ID)
    init, segments = expected(folder)
    fast = {names[5], names[6]} if where == "mid-file" else {names[-1]}
    slow = {name: 3.0 for name in names if name not in fast}
    song, _, _ = dash_song(world, key, a, seconds=LONG, segment_delays=slow)
    total = len(init) + 32 + 12 * len(segments) + sum(len(s) for s in segments)
    start = total - sum(len(s) for s in segments[5:])  # segment 5's first byte
    if where == "mid-file":
        header, wanted = (
            f"bytes={start + 7}-{start + len(segments[5]) + 99}",
            (segments[5] + segments[6][:100])[7:],
        )
    else:
        header, wanted = "bytes=-500", segments[-1][-500:]
    started = time.monotonic()
    answer = stream(world, song, {"range": header})
    assert time.monotonic() - started < 2.0
    assert answer.status_code == 206 and answer.content == wanted
    assert answer.headers["content-range"].endswith(f"/{total}")
    first = [r["name"] for r in media(a)][: len(fast)]
    assert set(first) == fast  # the range's segments were asked for first


def test_ffprobe_reads_the_length_and_ffmpeg_seeks_by_the_index(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, tmp_path: Path
) -> None:
    a, _ = a_and_b
    song, _, _ = dash_song(world, "s-ffprobe", a, seconds=LONG)
    path = tmp_path / "served.mp4"
    path.write_bytes(stream(world, song).content)
    said = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert abs(float(said) - LONG) < 0.05
    sought = pcm(path, "-ss", "14")  # from 14 s on: six seconds, by the index
    assert 5.5 * 44100 * 4 < len(sought) < 6.5 * 44100 * 4


def test_the_kept_file_is_byte_for_byte_the_one_served(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, _ = dash_song(world, "s-kept", a, seconds=LONG)
    first = stream(world, song, {"range": "bytes=0-99"})  # one range: the rest comes behind
    assert first.status_code == 206
    complete(dash)
    kept = [s.file for s in dash._served.values() if s.file.etag == first.headers["etag"]]
    assert len(kept) == 1 and kept[0].complete
    data = kept[0].path.read_bytes()
    a.clear()
    whole = stream(world, song)
    assert whole.content == data and whole.headers["etag"] == first.headers["etag"]
    assert a.requests("mpd") == [] and a.requests("segment") == []  # from the kept file


@pytest.mark.parametrize("changed", [False, True], ids=["same", "another-file"])
def test_a_seek_after_a_new_link_gets_the_same_bytes_or_is_refused(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, changed: bool
) -> None:
    a, _ = a_and_b
    # Links that expire 7 s after they are given: a pin counts them gone 5 s before.
    song, fake, _ = dash_song(world, f"s-relink-{changed}", a, link_seconds=7, segment_etag=True)
    whole = stream(world, song)
    etag = whole.headers["etag"]
    time.sleep(2.5)
    kept = stream(world, song, {"range": "bytes=1000-1999"})  # the kept file
    assert kept.status_code == 206 and kept.content == whole.content[1000:2000]
    assert kept.headers["etag"] == etag

    async def forget() -> None:  # not kept any more (pushed out past the cache's size)
        for served in dash._served.values():
            served.file.path.unlink()
        dash._served.clear()

    world.server.call(forget)
    if changed:  # the add-on's audio is another file now (the same size)
        fake.dash_init_edit = lambda data: data.replace(b"Lavf", b"Lavg")
    time.sleep(2.5)
    seek = stream(world, song, {"range": "bytes=-500"})
    if not changed:  # planned again from the new link: the same file, the same ETag
        assert seek.status_code == 206 and seek.content == whole.content[-500:]
        assert seek.headers["etag"] == etag
        return
    assert failed(seek)  # never another file's bytes in the middle of a play
    fresh = stream(world, song)  # a new play at byte zero takes the new file
    assert fresh.status_code == 200 and fresh.headers["etag"] != etag


def test_a_new_link_while_the_file_is_fetched_goes_on_with_it_unprobed(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    """The link expires while the file's segments are still being fetched: a seek takes a
    new link, whose manifest offers the same representation - the segments still missing
    come from it (the old link's answer 403 now), no size is asked for again, and the
    bytes and the ETag are the play's."""
    a, _ = a_and_b
    world.services.deliverer.settings.dash_segments_at_once = 1
    folder = dash_audio("s-relink-fill", LONG)
    names = segment_names(folder, FLAC_ID)
    slow = {name: 0.6 for name in names[1:]}
    song, _, _ = dash_song(
        world, "s-relink-fill", a, seconds=LONG, link_seconds=7, segment_delays=slow
    )
    first = stream(world, song, {"range": "bytes=0-99"})
    assert first.status_code == 206
    time.sleep(2.5)  # the pin counts the link gone; a few segments are still missing
    assert any(not s.file.complete for s in dash._served.values())
    seek = stream(world, song, {"range": "bytes=-300"})
    assert seek.status_code == 206 and seek.headers["etag"] == first.headers["etag"]
    complete(dash)
    _, segments = expected(folder)
    assert seek.content == segments[-1][-300:]
    assert len(a.requests("stream")) == 2 and len(a.requests("mpd")) == 2
    assert len(probes(a)) == len(names)  # sizes asked for once, of the first link
    assert {r["generation"] for r in media(a) if r["name"] == names[-1]} == {2}
    whole = stream(world, song)
    assert whole.content[:100] == first.content and whole.content.endswith(segments[-1])


@pytest.mark.parametrize(
    "layout,sets,base,kind",
    [
        ("timeline", 1, True, "audio/mp4"),  # the segments' host as a BaseURL
        ("duration", 1, False, "audio/mp4"),
        ("list", 1, False, "audio/mp4"),
        ("ranges", 1, False, "audio/mp4"),  # byte ranges of one file: their sizes known
        ("base", 1, True, "audio/flac"),  # one file: joined first
        ("timeline", 2, False, "audio/mp4"),  # the FLAC in an AdaptationSet of its own
    ],
    ids=["timeline-baseurl", "duration", "list", "ranges", "segmentbase", "two-sets"],
)
def test_every_standard_layout_plays_at_once(
    world: DeliveryWorld,
    a_and_b: tuple[FakeAddon, FakeAddon],
    dash: Dash,
    layout: str,
    sets: int,
    base: bool,
    kind: str,
) -> None:
    a, _ = a_and_b
    key = f"s-layout-{layout}-{sets}-{base}"
    song, _, _ = dash_song(world, key, a, layout=layout, sets=sets, dash_base=base)
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == kind
    assert pcm(whole.content) == pcm(tone(frequency_for(key), SECONDS, "flac").read_bytes())
    if layout == "ranges":
        assert probes(a) == []  # (the manifest's byte ranges are the sizes)


def test_a_full_length_manifest_over_a_short_timeline_is_a_preview(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    """The manifest says 30 s, its segments 6 s: refused before any segment is asked."""
    a, b = a_and_b
    song, _, _ = dash_song(
        world, "s-short", a, b, catalog_seconds=30,
        dash_edit=lambda t: t.replace('Duration="PT6.0S"', 'Duration="PT30.0S"'),
    )  # fmt: skip
    assert stream(world, song).status_code == 200 and b.requests("audio")
    assert a.requests("mpd") and a.requests("segment") == []


def test_a_much_longer_stream_is_another_recording(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    """The catalog's song is 3 s, the stream's index 6 s: another recording, as a direct
    link's first bytes would tell - the next add-on plays, A is not asked for it a while."""
    a, b = a_and_b
    song, _, _ = dash_song(world, "s-longer", a, b, catalog_seconds=3)
    assert stream(world, song).status_code == 200 and b.requests("audio")
    assert world.services.deliverer._wrong  # remembered, as for a direct link


def test_segments_without_a_size_fall_back_to_the_complete_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, folder = dash_song(world, "s-sizeless", a, seconds=LONG, segment_ranges=False)
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == "audio/flac"
    assert len(probes(a)) < len(segment_names(folder, FLAC_ID))  # stopped at the first
    a.clear()
    again = stream(world, song, {"range": "bytes=100-199"})  # the complete file's, kept
    assert again.content == whole.content[100:200] and probes(a) == []


def test_dash_start_complete_joins_the_whole_file_first(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    world.services.deliverer.settings.dash_start = "complete"
    song, _, _ = dash_song(world, "s-complete", a)
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == "audio/flac"
    assert probes(a) == []


@pytest.mark.parametrize("quality", ["lossless", "aac"])
def test_download_first_still_places_a_flac_or_a_fast_start_m4a(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, quality: str
) -> None:
    a, _ = a_and_b
    settings = world.services.deliverer.settings
    if quality == "aac":
        settings.dash_quality_from, settings.dash_quality_to = "192", "320"
    song, _, _ = dash_song(world, f"s-download-{quality}", a)
    response = stream(world, song, maxBitRate=96, format="mp3")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg"
    info = world.client().ok("getSong", {"id": song})["song"]
    assert info["suffix"] == ("flac" if quality == "lossless" else "m4a")
    assert info["duration"] == SECONDS  # Navidrome reads its length: not a fragmented file
    a.clear()
    raw = stream(world, song)  # Navidrome's own file from now on
    assert a.requests() == []
    if quality == "aac":
        assert raw.content.find(b"moov") < raw.content.find(b"mdat")  # fast start
        assert b"moof" not in raw.content
    else:
        assert raw.content.startswith(b"fLaC")


def library_file(world: DeliveryWorld, song: str) -> bytes:
    """The song's file in the library, once its audio is in place."""

    async def path() -> str:
        row = await world.services.store.fetchone(
            "SELECT state, path FROM placeholders WHERE song_id = ?", [song]
        )
        assert row is not None and row["state"] == "delivered"
        return str(row["path"])

    return world.services.engine.layout.absolute(world.server.call(path)).read_bytes()


def test_a_play_keeps_its_file_when_the_song_is_saved_while_it_plays(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    """The app saves the song as MP3 while it plays at once (an offline quality):
    download-first puts a native FLAC copied out of it in the library. The play's seeks -
    during that and after it - get its own file's bytes, size and ETag (from the kept
    file); a new play of the song gets the library's FLAC, and so do its seeks and another
    app's."""
    a, _ = a_and_b
    folder = dash_audio("s-saved", LONG)
    names = segment_names(folder, FLAC_ID)
    song, _, _ = dash_song(world, "s-saved", a, seconds=LONG, segment_delays={names[-1]: 3.0})
    first = stream(world, song, {"range": "bytes=0-999"})
    assert first.status_code == 206
    etag, total = first.headers["etag"], first.headers["content-range"].rsplit("/", 1)[1]
    with ThreadPoolExecutor(1) as pool:
        params = {"id": song, "format": "mp3", "maxBitRate": 96}
        saving = pool.submit(world.client().request, "stream", params)
        time.sleep(0.5)
        during = stream(world, song, {"range": "bytes=1000-1999"})
        assert not saving.done()  # (the seek came while the song was being saved)
        assert saving.result(timeout=30).status_code == 200
    library = library_file(world, song)
    assert library.startswith(b"fLaC")
    a.clear()
    after = [stream(world, song, {"range": r}) for r in ("bytes=2000-2999", "bytes=-500")]
    assert a.requests() == []  # from the kept file
    kept = [s.file for s in dash._served.values() if s.file.etag == etag]
    assert len(kept) == 1 and kept[0].complete
    data = kept[0].path.read_bytes()
    for part, wanted in zip(
        [during, *after], [data[1000:2000], data[2000:3000], data[-500:]], strict=True
    ):
        assert part.status_code == 206 and part.content == wanted
        assert part.headers["content-range"].endswith(f"/{total}")
        assert part.headers["etag"] == etag
    other = world.client(client="other-app")
    seek = other.request("stream", {"id": song}, headers={"range": "bytes=100-199"})
    assert seek.status_code == 206 and seek.content == library[100:200]  # another app's
    fresh = stream(world, song)  # a new play: the library's file
    assert fresh.status_code == 200 and fresh.content == library
    seek = stream(world, song, {"range": "bytes=100-199"})
    assert seek.status_code == 206 and seek.content == library[100:200]
    assert seek.headers["content-range"] == f"bytes 100-199/{len(library)}"
    assert a.requests() == []


def test_once_the_play_s_link_expires_its_seeks_are_the_library_s(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, _ = dash_song(world, "s-saved-expiry", a, seconds=LONG)
    first = stream(world, song, {"range": "bytes=0-999"})
    complete(dash)
    assert world.client().request("download", {"id": song}).status_code == 200
    kept = stream(world, song, {"range": "bytes=1000-1999"})
    assert kept.status_code == 206 and kept.headers["etag"] == first.headers["etag"]
    library = library_file(world, song)
    settings = world.services.deliverer.settings
    ttl = settings.pin_ttl_seconds
    settings.pin_ttl_seconds = 0.0  # the song's link expires now
    try:
        a.clear()
        seek = stream(world, song, {"range": "bytes=1000-1999"})
    finally:
        settings.pin_ttl_seconds = ttl
    assert seek.status_code == 206 and seek.content == library[1000:2000]
    assert seek.headers["content-range"] == f"bytes 1000-1999/{len(library)}"
    later = stream(world, song, {"range": "bytes=-100"})  # handed over for good
    assert later.status_code == 206 and later.content == library[-100:]
    assert a.requests() == []


def test_with_dash_start_complete_a_play_keeps_its_joined_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    world.services.deliverer.settings.dash_start = "complete"
    song, _, _ = dash_song(world, "s-saved-complete", a)
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == "audio/flac"
    assert world.client().request("download", {"id": song}).status_code == 200
    library = library_file(world, song)
    assert library[100:] != whole.content[100:]  # retagged in the library: another file
    a.clear()
    seek = stream(world, song, {"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == whole.content[100:]
    assert seek.headers["etag"] == whole.headers["etag"]
    assert a.requests() == []  # from the kept file
    assert stream(world, song).content == library  # a new play
    assert stream(world, song, {"range": "bytes=100-"}).content == library[100:]


def test_probes_are_audio_openings_at_the_add_on_s_limits(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, folder = dash_song(world, "s-paced", a, seconds=LONG, probe_delay=0.05)
    assert stream(world, song).status_code == 200
    assert a.most_probes_at_once == 8  # the song being played: 8 at once
    pace = world.services.sources.paces.of(a.base_url)
    assert pace is not None
    pace.configure(Limits(0.0, 4, 1))  # one audio opening at once, for all but a play
    try:
        a.most_probes_at_once = 0
        other, fake, _ = dash_song(world, "s-paced-warm", a, seconds=LONG, probe_delay=0.05)
        track = Track(other, fake.isrc, "Title s-paced-warm", "Artist s-paced-warm", LONG * 1000)
        assert world.server.call(lambda: world.services.deliverer.prewarm(track))
        assert a.most_probes_at_once == 1  # warm-ahead's probes: one at a time
        warm = [r for r in probes(a) if r["isrc"] == fake.isrc]
        assert len(warm) == len(segment_names(folder, FLAC_ID))
    finally:
        pace.configure(Limits(0.0, 4, 0))


def test_a_segment_host_the_network_policy_denies_is_never_asked(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    song, _, _ = dash_song(world, "s-private", a, b, dash_host="private.fake.test")
    assert stream(world, song).status_code == 200 and b.requests("audio")
    assert a.requests("mpd") and a.requests("segment") == []


def test_a_segment_that_fails_once_is_asked_again_and_a_probe_too(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    folder = dash_audio("s-retry")
    second = segment_names(folder, FLAC_ID)[1]
    song, _, _ = dash_song(
        world, "s-retry", a, b, segment_faults={second: [503]}, probe_faults={second: [503]}
    )
    whole = stream(world, song)
    init, segments = expected(folder)
    assert whole.content.startswith(init) and whole.content.endswith(b"".join(segments))
    assert not b.requests("audio")
    assert [r["name"] for r in media(a)].count(second) == 2
    assert [r["name"] for r in probes(a)].count(second) == 2


def test_requests_at_once_share_one_plan(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, folder = dash_song(world, "s-shared", a, probe_delay=0.2)
    with ThreadPoolExecutor(3) as pool:
        answers = list(
            pool.map(
                lambda h: stream(world, song, h),
                [None, {"range": "bytes=0-1"}, {"range": "bytes=500-"}],
            )
        )
    assert [r.status_code for r in answers] == [200, 206, 206]
    assert len(a.requests("mpd")) == 1
    assert len(probes(a)) == len(segment_names(folder, FLAC_ID))
    assert sorted(r["name"] for r in media(a)) == segment_names(folder, FLAC_ID)


def test_warm_ahead_prepares_the_next_song_whole(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    from tests.harness.engine import catalog_release

    release = catalog_release("s-warm", "Warm DASH", "Warm Artist", 2, seconds=SECONDS)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    for track in release.tracks:
        assert track.isrc
        folder = dash_audio(track.title, SECONDS)
        a.add(FakeTrack(isrc=track.isrc, audio=folder / "out.mpd", dash=folder))
    world.client(client="warm-stream").request("stream", {"id": songs[0]})
    deadline = time.monotonic() + 10
    while len(a.requests("mpd")) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    complete(dash)
    a.clear()
    second = world.client(client="warm-stream").request("stream", {"id": songs[1]})
    assert second.status_code == 200 and second.headers["content-type"] == "audio/mp4"
    assert a.requests("mpd") == [] and a.requests("segment") == []


class _Everything(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_no_address_in_any_log_line_and_the_shape_at_debug(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    logger = logging.getLogger("shijhon")
    handler, level = _Everything(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        song, _, _ = dash_song(world, "s-logs", a)
        assert stream(world, song).status_code == 200
        complete(dash)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    for line in handler.lines:
        for secret in (ADDON_HOST, CDN_HOST, "sig=", "/seg/", "/dash/", "http"):
            assert secret not in line, line
    shapes = [line for line in handler.lines if line.startswith("DASH from A:")]
    assert len(shapes) == 2, shapes  # served at once; then complete
    assert "SegmentTemplate+SegmentTimeline, 3 representation(s)" in shapes[0]
    assert "3 probe(s) in " in shapes[0] and "first byte after " in shapes[0]
    assert "chose flac" in shapes[0] and "3 segment(s)" in shapes[0]
    assert "all 3 segment(s)" in shapes[1]


def test_the_playback_page_has_the_dash_start(world: DeliveryWorld) -> None:
    browser = Browser(world.server.base_url)
    try:
        browser.sign_in(ADMIN_USER, ADMIN_PASSWORD)
        page = browser.get("playback").text
        assert 'id="delivery-dash-start"' in page
        saved = browser.submit("playback", {"dash_start": "complete"})
        assert saved.status_code in (200, 303)
        assert world.services.deliverer.settings.dash_start == "complete"
        browser.submit("playback", {"dash_start": "at_once"})
        assert world.services.deliverer.settings.dash_start == "at_once"
    finally:
        browser.close()
