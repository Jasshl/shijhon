"""Suite K (continued) — DASH links joined first (``dash_start = "complete"``; served at
once, the default: ``test_K_dash_stream.py``).

An add-on's link to a DASH manifest (made-up audio: ffmpeg's dash muxer over a synthetic
tone, three representations, the segments on another origin than the manifest) plays as
one ordinary file - a native FLAC or a fast-start M4A, the audio copied, not converted -
with ranges, HEAD and a strong ETag. Manifests Shijhon does not play, and previews, fall
through to the next add-on without a segment fetched; the segments' requests go through the
add-on's network policy and limits, with retries, expiry and rate limits as for a direct
link; one join serves concurrent requests, warm-ahead and download-first.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from shijhon.delivery.dash import Dash
from shijhon.delivery.length import PEEK_BYTES, audio_length
from shijhon.delivery.playback import Track
from tests.conftest import NavidromeFactory
from tests.harness.dash_fixtures import AAC_320, MP3_192, dash_audio, segment_names
from tests.harness.dash_fixtures import FLAC as FLAC_ID
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import ADDON_HOST, CDN_HOST, FakeAddon, FakeTrack
from tests.harness.library import frequency_for, tone
from tests.harness.logs import collected
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

SECONDS = 6  # three segments of two seconds


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    # The length check on (the tones are as long as the catalog says): previews.
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("dash"),
        length_tolerance_seconds=1.0,
        budget_seconds=4.0,
        warm_ahead_depth=1,
        warm_ahead_delay_seconds=0.2,
        dash_start="complete",
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
    yield joiner
    settings = world.services.deliverer.settings
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
    mix: str = "flac",
    **dash: Any,
) -> tuple[str, FakeTrack, Path]:
    """A one-track catalog song: A's link to it is DASH, B's (when given) a direct FLAC.
    Returns (song ID, A's track, the tone it is made of)."""
    addons = [b] if b is not None else []
    song, fakes, _ = world.placeholder_track(key, addons, seconds=catalog_seconds or seconds)
    isrc = next(iter(fakes.values())).isrc if fakes else _isrc(world, song)
    folder = dash_audio(key, seconds, layout, sets, mix)
    fake = a.add(FakeTrack(isrc=isrc, audio=folder / "out.mpd", dash=folder, **dash))
    return song, fake, tone(frequency_for(key), seconds, "flac")


def _isrc(world: DeliveryWorld, song: str) -> str:
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT isrc FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert row is not None
    return str(row["isrc"])


def stream(
    world: DeliveryWorld, song: str, headers: dict[str, str] | None = None, **params: object
) -> httpx.Response:
    return world.client().request("stream", {"id": song, **params}, headers=headers)


def pcm(data: bytes) -> bytes:
    """Decoded audio (16-bit stereo samples at 44.1 kHz)."""
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "2",
         "-ar", "44100", "pipe:1"],
        input=data, capture_output=True, check=True,
    ).stdout  # fmt: skip


def saved(tmp: Path, name: str, data: bytes) -> Path:
    path = tmp / name
    path.write_bytes(data)
    return path


def test_a_dash_link_plays_as_one_file_with_ranges_and_head(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, tmp_path: Path
) -> None:
    a, _ = a_and_b
    song, _, source = dash_song(world, "d-basic", a)
    whole = stream(world, song)
    assert whole.status_code == 200
    assert whole.headers["content-type"] == "audio/flac"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-length"] == str(len(whole.content))
    etag = whole.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/")
    # A native FLAC of the song's length (STREAMINFO counts its samples), the audio the
    # tone's, sample for sample: copied, not converted.
    flac = FLAC(saved(tmp_path, "whole.flac", whole.content))
    assert flac.info.total_samples == SECONDS * 44100
    assert abs(flac.info.length - SECONDS) < 0.05
    assert pcm(whole.content) == pcm(source.read_bytes())
    data = whole.content
    for header, expected in (
        ("bytes=0-99", data[:100]),
        ("bytes=1000-", data[1000:]),
        ("bytes=-64", data[-64:]),
        ("bytes=10-10", data[10:11]),
    ):
        part = stream(world, song, {"range": header})
        assert part.status_code == 206 and part.content == expected, header
        assert part.headers["content-length"] == str(len(expected))
        assert part.headers["content-range"].endswith(f"/{len(data)}")
        assert part.headers["etag"] == etag
    past = stream(world, song, {"range": f"bytes={len(data) + 10}-"})
    assert past.status_code == 416 and past.headers["content-range"] == f"bytes */{len(data)}"
    head = world.client().request("stream", {"id": song}, http_method="HEAD")
    assert head.status_code == 200 and head.content == b""
    for name in ("content-length", "content-type", "accept-ranges", "etag"):
        assert head.headers[name] == whole.headers[name], name
    # The manifest from the add-on's origin, each segment once from the other origin, the
    # FLAC representation's alone.
    assert [r["origin"] for r in a.requests("mpd")] == ["main"]
    segments = a.requests("segment")
    assert {r["origin"] for r in segments} == {"cdn"}
    names = [r["name"] for r in segments]
    assert sorted(names) == sorted({*names})
    assert set(names) == {"init-stream2.m4s", *segment_names(dash_audio("d-basic"), FLAC_ID)}
    assert len(a.requests("stream")) == 1


def test_the_highest_quality_in_the_range_chosen(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, tmp_path: Path
) -> None:
    """From 192 to 320 kb/s: the AAC at 320 (not the FLAC, above the range; not the AAC at
    96, below it)."""
    a, _ = a_and_b
    settings = world.services.deliverer.settings
    settings.dash_quality_from, settings.dash_quality_to = "192", "320"
    song, _, _ = dash_song(world, "d-aac", a)
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == "audio/mp4"
    m4a = MP4(saved(tmp_path, "whole.m4a", whole.content))
    assert m4a.info.codec.startswith("mp4a") and abs(m4a.info.length - SECONDS) < 0.1
    # A fast-start M4A: its index before its audio.
    assert whole.content.find(b"moov") < whole.content.find(b"mdat")
    names = {r["name"] for r in a.requests("segment")}
    assert names == {"init-stream1.m4s", *segment_names(dash_audio("d-aac"), AAC_320)}


def test_a_song_with_no_quality_in_the_range_is_played_from_the_next_add_on(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    settings = world.services.deliverer.settings
    settings.dash_quality_from, settings.dash_quality_to = "lossless", "lossless"
    song, _, _ = dash_song(world, "d-out-of-range", a, b, mix="mp3")  # MP3 192k, AAC 128k
    with collected("shijhon.delivery.playback") as lines:
        assert stream(world, song).status_code == 200 and b.requests("audio")
    assert a.requests("mpd") and a.requests("segment") == []
    assert any("no quality in the chosen range (lossless to lossless" in line for line in lines)
    # A choice of the installation's, not the add-on's failure.
    source = next(s for s in world.server.call(world.services.sources.enabled) if s.name == "A")
    assert source.stats.errors_since_success == 0 and source.stats.last_failure is None
    assert not world.server.call(lambda: _cooling(world, source.id))


async def _cooling(world: DeliveryWorld, source_id: int) -> bool:
    return world.services.sources.cooling(source_id)


def test_the_playback_page_shows_the_range_in_one_row_and_refuses_it_upside_down(
    world: DeliveryWorld,
) -> None:
    browser = Browser(world.server.base_url)
    try:
        browser.sign_in(ADMIN_USER, ADMIN_PASSWORD)
        page = browser.get("playback").text
        rows = [m.start() for m in re.finditer(r'<div class="setting(?: [^"]*)?">', page)]
        start = page.index('id="delivery-dash-quality-from"')
        begins = max(at for at in rows if at < start)
        row = page[begins : min((at for at in rows if at > start), default=len(page))]
        assert "DASH quality: from" in row and ">to</span>" in row
        assert row.count("<select") == 2 and 'id="delivery-dash-quality-to"' in row
        assert page.count('id="delivery-dash-quality-to"') == 1
        refused = browser.submit("playback", {"dash_quality_from": "320", "dash_quality_to": "128"})
        assert refused.status_code == 400
        assert "is above the other end of the range" in refused.text
        assert world.services.deliverer.settings.dash_quality_from == "any"
    finally:
        browser.close()


def test_mp3_in_mp4_is_copied_out_as_an_mp3(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, tmp_path: Path
) -> None:
    a, _ = a_and_b
    song, _, _ = dash_song(world, "d-mp3", a, mix="mp3")
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == "audio/mpeg"
    mp3 = MP3(saved(tmp_path, "whole.mp3", whole.content))
    assert abs(mp3.info.length - SECONDS) < 0.1 and mp3.info.bitrate // 1000 in (191, 192)
    # Its length as Shijhon reads it from the first bytes (its Xing/Info frame).
    length = audio_length(whole.content[:PEEK_BYTES], len(whole.content))
    assert length is not None and abs(length - SECONDS) < 0.1
    names = {r["name"] for r in a.requests("segment")}
    assert names == {"init-stream0.m4s", *segment_names(dash_audio("d-mp3", mix="mp3"), MP3_192)}


@pytest.mark.parametrize("said_by", ["field", "path", "type"])
def test_a_dash_link_is_known_by_its_answer_its_path_or_its_content_type(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, said_by: str
) -> None:
    a, _ = a_and_b
    song, _, source = dash_song(world, f"d-said-{said_by}", a, dash_link=said_by)
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == "audio/flac"
    assert pcm(whole.content) == pcm(source.read_bytes())


@pytest.mark.parametrize(
    "layout,sets,base",
    [
        ("timeline", 1, True),  # the segments' host as a BaseURL
        ("duration", 1, False),
        ("list", 1, False),
        ("ranges", 1, False),  # byte ranges of one file, under nested BaseURLs
        ("base", 1, True),
        ("timeline", 2, False),  # the FLAC in an AdaptationSet of its own
    ],
    ids=["timeline-baseurl", "duration", "list", "ranges", "segmentbase", "two-sets"],
)
def test_every_standard_layout_plays(
    world: DeliveryWorld,
    a_and_b: tuple[FakeAddon, FakeAddon],
    dash: Dash,
    layout: str,
    sets: int,
    base: bool,
) -> None:
    a, _ = a_and_b
    key = f"d-layout-{layout}-{sets}-{base}"
    song, _, source = dash_song(world, key, a, layout=layout, sets=sets, dash_base=base)
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.headers["content-type"] == "audio/flac"
    assert pcm(whole.content) == pcm(source.read_bytes())
    assert {r["origin"] for r in a.requests("segment")} == {"cdn"}
    if layout == "ranges":
        assert all(r["range"] for r in a.requests("segment"))


PROTECTION = '<ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"/>'
BOMB = (
    '<?xml version="1.0"?><!DOCTYPE MPD [<!ENTITY a "aaaaaaaaaa">'
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
    ']><MPD type="static">&c;</MPD>'
)


def _two_periods(text: str) -> str:
    period = text[text.index("<Period") : text.index("</Period>")]
    return text.replace("</Period>", "</Period>" + period + "</Period>")


def _other_codecs(text: str) -> str:
    return text.replace('codecs="mp4a.40.2"', 'codecs="ec-3"').replace('"flac"', '"opus"')


REFUSED = {
    "protected-set": lambda t: t.replace("<Representation", PROTECTION + "<Representation", 1),
    "protected-representation": lambda t: t.replace(
        "<SegmentTemplate", PROTECTION + "<SegmentTemplate", 1
    ),
    "live": lambda t: t.replace('type="static"', 'type="dynamic"'),
    "two-periods": _two_periods,
    "codecs": _other_codecs,
    "oversize": lambda t: t.replace("<Period", "<!--" + "x" * 1_100_000 + "--><Period", 1),
    "entity-bomb": lambda t: BOMB,
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_a_manifest_shijhon_does_not_play_falls_through_without_a_segment(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, case: str
) -> None:
    a, b = a_and_b
    song, _, _ = dash_song(world, f"d-refused-{case}", a, b, dash_edit=REFUSED[case])
    whole = stream(world, song)
    assert whole.status_code == 200 and b.requests("audio")
    assert a.requests("mpd") and a.requests("segment") == []
    # Not the add-on's error: no cooldown; its failure is in its diagnostics.
    source = next(s for s in world.server.call(world.services.sources.enabled) if s.name == "A")
    assert source.stats.errors_since_success == 0
    assert "unsupported DASH" in (source.stats.last_failure or "")


def test_an_init_segment_that_says_encrypted_is_refused_before_any_media(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    song, _, _ = dash_song(
        world, "d-encrypted", a, b, dash_init_edit=lambda data: data.replace(b"fLaC", b"enca", 1)
    )
    assert stream(world, song).status_code == 200 and b.requests("audio")
    assert [r["name"] for r in a.requests("segment")] == ["init-stream2.m4s"]


def test_a_preview_manifest_is_not_fetched_and_not_remembered(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    # The catalog's song is 30 s; the add-on's manifest says 6 s.
    song, _, _ = dash_song(world, "d-preview", a, b, catalog_seconds=30)
    whole = stream(world, song)
    assert whole.status_code == 200 and b.requests("audio")
    assert a.requests("mpd") and a.requests("segment") == []
    assert world.services.deliverer._wrong == {}  # not another recording for a week
    # Asked again for a later play, not passed over for a week.
    world.services.deliverer.forget(song)
    a.clear()
    assert stream(world, song).status_code == 200
    assert a.requests("mpd")


def test_a_full_length_manifest_of_short_audio_is_not_kept(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    song, _, _ = dash_song(
        world, "d-short", a, b, catalog_seconds=30,
        dash_edit=lambda t: t.replace('Duration="PT6.0S"', 'Duration="PT30.0S"'),
    )  # fmt: skip
    kept = len(dash._files)
    assert stream(world, song).status_code == 200 and b.requests("audio")
    assert a.requests("segment")  # joined, checked, and left
    assert len(dash._files) == kept
    assert world.services.deliverer._wrong == {}


def test_a_segment_that_fails_once_is_asked_for_again(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    second = segment_names(dash_audio("d-retry"), FLAC_ID)[1]
    song, _, source = dash_song(world, "d-retry", a, b, segment_faults={second: [503]})
    whole = stream(world, song)
    assert pcm(whole.content) == pcm(source.read_bytes()) and not b.requests("audio")
    assert [r["name"] for r in a.requests("segment")].count(second) == 2


@pytest.mark.parametrize("fault", [503, 0], ids=["5xx", "broken-off"])
def test_a_segment_that_fails_twice_falls_through(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash, fault: int
) -> None:
    """Twice an HTTP 503, or twice an answer that breaks off (0)."""
    a, b = a_and_b
    second = segment_names(dash_audio(f"d-twice-{fault}"), FLAC_ID)[1]
    faults = {second: [fault, fault]}
    song, _, _ = dash_song(world, f"d-twice-{fault}", a, b, segment_faults=faults)
    assert stream(world, song).status_code == 200 and b.requests("audio")
    source = next(s for s in world.server.call(world.services.sources.enabled) if s.name == "A")
    assert source.stats.errors_since_success == 1  # an error of the add-on's, counted once
    assert [r["name"] for r in a.requests("segment")].count(second) == 2


def test_an_expired_segment_link_is_resolved_again(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    second = segment_names(dash_audio("d-expired"), FLAC_ID)[1]
    song, _, source = dash_song(world, "d-expired", a, b, segment_faults={second: [403]})
    whole = stream(world, song)
    assert pcm(whole.content) == pcm(source.read_bytes()) and not b.requests("audio")
    assert len(a.requests("stream")) == 2 and len(a.requests("mpd")) == 2


def test_a_rate_limited_segment_leaves_the_add_on_s_audio_alone(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    a.retry_after = "5"
    second = segment_names(dash_audio("d-429"), FLAC_ID)[1]
    song, _, _ = dash_song(world, "d-429", a, b, segment_faults={second: [429]})
    assert stream(world, song).status_code == 200 and b.requests("audio")
    pace = world.services.sources.paces.of(a.base_url)
    assert pace is not None and pace.audio_blocked > 3


def test_slow_segments_past_the_budget_fall_through_in_time(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    song, _, _ = dash_song(world, "d-slow", a, b, segment_delay=3.0)
    started = time.monotonic()
    assert stream(world, song).status_code == 200 and b.requests("audio")
    assert time.monotonic() - started < 4.5  # the byte-zero budget (4 s) and a moment


def test_a_join_fetches_a_few_segments_at_once_each_an_audio_opening(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    world.services.deliverer.settings.dash_segments_at_once = 2
    song, _, _ = dash_song(world, "d-at-once", a, segment_delay=0.3)
    assert stream(world, song).status_code == 200
    assert a.most_segments_at_once == 2
    # Warm-ahead's join waits for the add-on's audio openings: one at a time at a limit of 1.
    from shijhon.delivery.pacing import Limits

    pace = world.services.sources.paces.of(a.base_url)
    assert pace is not None
    pace.configure(Limits(0.0, 4, 1))
    try:
        a.most_segments_at_once = 0
        other, fake, _ = dash_song(world, "d-at-once-warm", a, segment_delay=0.3)
        track = Track(other, fake.isrc, "Title d-at-once-warm", "Artist d-at-once-warm", 6000)
        assert world.server.call(lambda: world.services.deliverer.prewarm(track))
        assert a.most_segments_at_once == 1
    finally:
        pace.configure(Limits(0.0, 4, 0))


def test_a_segment_host_the_network_policy_denies_is_never_asked(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    song, _, _ = dash_song(world, "d-private", a, b, dash_host="private.fake.test")
    assert stream(world, song).status_code == 200 and b.requests("audio")
    assert a.requests("mpd") and a.requests("segment") == []


def test_a_seek_after_the_link_expired_gets_the_same_file_kept_or_joined_again(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    # Links that expire 7 s after they are given: a pin counts them gone 5 s before.
    song, _, _ = dash_song(world, "d-pinned", a, link_seconds=7)
    whole = stream(world, song)
    etag = whole.headers["etag"]
    time.sleep(2.5)
    seek = stream(world, song, {"range": "bytes=1000-1999"})
    assert seek.status_code == 206 and seek.content == whole.content[1000:2000]
    assert seek.headers["etag"] == etag
    assert len(a.requests("stream")) == 2 and len(a.requests("mpd")) == 1  # the kept file

    # Gone from the cache: joined again from a new link, byte for byte the same.

    async def forget() -> None:
        for joined in dash._files.values():
            joined.path.unlink()
        dash._files.clear()

    world.server.call(forget)
    time.sleep(2.5)
    again = stream(world, song, {"range": "bytes=-500"})
    assert again.status_code == 206 and again.content == whole.content[-500:]
    assert again.headers["etag"] == etag
    assert len(a.requests("stream")) == 3 and len(a.requests("mpd")) == 2


def test_requests_at_once_share_one_join(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, _ = dash_song(world, "d-shared", a, segment_delay=0.2)
    with ThreadPoolExecutor(3) as pool:
        answers = list(
            pool.map(
                lambda h: stream(world, song, h),
                [None, {"range": "bytes=0-1"}, {"range": "bytes=500-"}],
            )
        )
    assert [r.status_code for r in answers] == [200, 206, 206]
    assert len(a.requests("mpd")) == 1
    names = [r["name"] for r in a.requests("segment")]
    assert len(names) == len(set(names)) == 4


def test_download_first_places_the_joined_flac_in_the_library(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    song, _, _ = dash_song(world, "d-download", a)
    response = stream(world, song, maxBitRate=96, format="mp3")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg"
    info = world.client().ok("getSong", {"id": song})["song"]
    assert info["suffix"] == "flac" and info["duration"] == SECONDS  # the same song ID
    a.clear()
    assert stream(world, song).headers["content-type"] == "audio/flac" and a.requests() == []


def test_warm_ahead_joins_the_next_song(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, _ = a_and_b
    from tests.harness.engine import catalog_release

    release = catalog_release("d-warm", "Warm DASH", "Warm Artist", 2, seconds=SECONDS)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    for track in release.tracks:
        assert track.isrc
        folder = dash_audio(track.title, SECONDS)
        a.add(FakeTrack(isrc=track.isrc, audio=folder / "out.mpd", dash=folder))
    world.client(client="warm-dash").request("stream", {"id": songs[0]})
    deadline = time.monotonic() + 10
    while len(a.requests("mpd")) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.5)  # its join's segments
    a.clear()
    second = world.client(client="warm-dash").request("stream", {"id": songs[1]})
    assert second.status_code == 200 and second.headers["content-type"] == "audio/flac"
    assert a.requests("mpd") == [] and a.requests("segment") == []


def test_past_the_cache_size_the_least_recently_used_go(
    world: DeliveryWorld,
    a_and_b: tuple[FakeAddon, FakeAddon],
    dash: Dash,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, _ = a_and_b
    # (A file just joined is kept a few seconds for its waiters: here, none.)
    monkeypatch.setattr("shijhon.delivery.dash.FRESH_SECONDS", 0.0)
    limit = dash.max_bytes
    dash.max_bytes = 1
    try:
        for key in ("d-cache-1", "d-cache-2"):
            song, _, _ = dash_song(world, key, a)
            assert stream(world, song).status_code == 200
        assert len(dash._files) == 1  # the newest, whatever its size
        assert len([p for p in dash.folder.iterdir() if not p.name.startswith(".")]) == 1
    finally:
        dash.max_bytes = limit


@pytest.fixture
def a_alone(world: DeliveryWorld, dash: Dash) -> Iterator[FakeAddon]:
    """A (whose links are DASH) as the only source, with no join under way: its attempts
    get the whole byte-zero budget (4 s)."""
    world.clear_sources()
    quiet(world)
    a = world.addon("A")
    world.add_source(a)
    yield a
    world.clear_sources()
    quiet(world)


def quiet(world: DeliveryWorld, seconds: float = 20.0) -> None:
    """Until no join is under way (one an earlier test's attempt gave up on goes on)."""
    joiner = world.services.deliverer.dash
    assert joiner is not None
    deadline = time.monotonic() + seconds
    while world.server.call(lambda: _joins(joiner)) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not world.server.call(lambda: _joins(joiner)), "a join is still under way"


async def _joins(joiner: Dash) -> int:
    return len(joiner._jobs)


async def _at_once(joiner: Dash, joins: int) -> None:
    joiner.joins_at_once = joins


def test_a_join_not_done_in_time_goes_on_and_the_retry_gets_its_file(
    world: DeliveryWorld, a_alone: FakeAddon, dash: Dash
) -> None:
    """The song's join takes longer than the budget (4 s): the play gets no audio after its
    budget, as before; the join goes on, and the app's retry - while it is still joining,
    and again once it is done - gets the file: the manifest asked for once, each segment
    once, the link once."""
    a = a_alone
    song, _, source = dash_song(world, "d-goes-on", a, segment_delay=3.0)  # about 6 s
    started = time.monotonic()
    with collected("shijhon.delivery.playback") as lines:
        first = stream(world, song)
    assert time.monotonic() - started < 5.0  # the budget, and a moment
    assert not first.headers.get("content-type", "").startswith("audio/")
    assert any("timeout while joining the segments (the join goes on)" in line for line in lines)
    assert world.server.call(lambda: _joins(dash)) == 1  # still joining
    retry = stream(world, song)  # while it is still joining: the same join
    assert retry.status_code == 200 and retry.headers["content-type"] == "audio/flac"
    assert pcm(retry.content) == pcm(source.read_bytes())
    started = time.monotonic()
    again = stream(world, song)
    assert again.status_code == 200 and again.content == retry.content
    assert time.monotonic() - started < 1.0  # the kept file, at once
    assert len(a.requests("mpd")) == 1 and len(a.requests("stream")) == 1
    names = [r["name"] for r in a.requests("segment")]
    assert len(names) == len(set(names)) == 4  # the FLAC's init and its three segments


def test_a_retry_after_the_join_is_done_gets_the_file_at_once(
    world: DeliveryWorld, a_alone: FakeAddon, dash: Dash
) -> None:
    a = a_alone
    song, _, _ = dash_song(world, "d-done-later", a, segment_delay=3.0)
    assert not stream(world, song).headers.get("content-type", "").startswith("audio/")
    quiet(world)  # joined in the background
    started = time.monotonic()
    retry = stream(world, song)
    assert retry.status_code == 200 and retry.headers["content-type"] == "audio/flac"
    assert time.monotonic() - started < 1.0
    assert len(a.requests("mpd")) == 1 and len(a.requests("stream")) == 1
    names = [r["name"] for r in a.requests("segment")]
    assert len(names) == len(set(names)) == 4


def test_a_join_nobody_waits_for_ends_at_the_wait_cap_and_leaves_nothing(
    world: DeliveryWorld, a_alone: FakeAddon, dash: Dash
) -> None:
    """A budget of 1.5 s and a wait cap of 3 s: the play gives up after 1.5 s, its join
    after 3 s - before the add-on's slow segments come (and before a request's own time,
    5 s, would end it)."""
    a = a_alone
    settings = world.services.deliverer.settings
    saved = settings.budget_seconds, settings.max_wait_seconds
    settings.budget_seconds, settings.max_wait_seconds = 1.5, 3.0
    try:
        song, _, _ = dash_song(world, "d-capped", a, segment_delay=30.0)
        kept = len(dash._files)
        with collected("shijhon.delivery.dash") as lines:
            started = time.monotonic()
            assert not stream(world, song).headers.get("content-type", "").startswith("audio/")
            assert time.monotonic() - started < 2.5
            assert [p.suffix for p in dash.folder.iterdir()].count(".part") == 1
            quiet(world, 10.0)
            ended = time.monotonic() - started
        assert 2.5 < ended < 4.5  # at the cap, not at the request's own timeout
        assert not [p for p in dash.folder.iterdir() if p.name.startswith(".")]  # no part
        assert len(dash._files) == kept
        assert any(
            line.startswith("DASH join from A ended after 3.") and "no request waiting" in line
            for line in lines
        ), lines
        asked = len(a.requests("segment"))
        time.sleep(0.5)
        assert len(a.requests("segment")) == asked  # nothing more asked of the add-on
    finally:
        settings.budget_seconds, settings.max_wait_seconds = saved


def test_only_so_many_joins_at_once_and_the_song_being_played_never_waits(
    world: DeliveryWorld, a_alone: FakeAddon, dash: Dash
) -> None:
    """One join at a time: warm-ahead joins one song; another warm-ahead comes and waits;
    a play comes and its join starts at once, beside the first; the second warm-ahead's
    join starts once both are done. Each segment is asked for once."""
    a = a_alone
    world.server.call(lambda: _at_once(dash, 1))
    tracks = {}
    for key in ("d-turn-warm-1", "d-turn-warm-2", "d-turn-play"):
        song, fake, _ = dash_song(world, key, a, segment_delay=1.0)  # about 2 s each
        tracks[key] = Track(song, fake.isrc, f"Title {key}", f"Artist {key}", SECONDS * 1000)
    deliverer = world.services.deliverer

    def warm(key: str) -> bool:
        return world.server.call(lambda: deliverer.prewarm(tracks[key]))

    try:
        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(warm, "d-turn-warm-1")
            time.sleep(0.3)  # its join under way
            second = pool.submit(warm, "d-turn-warm-2")
            time.sleep(0.2)
            played = pool.submit(stream, world, tracks["d-turn-play"].song_id)
            assert played.result().status_code == 200
            assert first.result() and second.result()
    finally:
        world.server.call(lambda: _at_once(dash, 3))
    by_isrc = {track.isrc: key for key, track in tracks.items()}
    spans: dict[str, list[float]] = {}
    names: list[tuple[str, str]] = []
    for request in a.requests():
        if request["endpoint"] in ("mpd", "segment"):
            key = by_isrc[request["isrc"]]
            spans.setdefault(key, []).append(request["at"])
            if request["endpoint"] == "segment":
                names.append((key, request["name"]))
    warm_1, warm_2, play = (spans[k] for k in ("d-turn-warm-1", "d-turn-warm-2", "d-turn-play"))
    # The play's join began while the first warm-ahead's last segment was still coming
    # (it is answered a second after it is asked for); the second waited for both.
    assert min(warm_1) < min(play) < max(warm_1) + 1.0
    assert min(warm_2) > max(max(warm_1), max(play))
    assert len(names) == len(set(names)) == 12


def test_an_orderly_stop_ends_the_joins_and_leaves_no_part(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    # (A request to the add-on may take 20 s: only the stop ends the join in time.)
    with delivery_world(
        navidrome_factory(),
        tmp_path,
        budget_seconds=1.5,
        request_timeout_seconds=20.0,
        dash_start="complete",
    ) as other:
        a = other.addon("A")
        other.add_source(a)
        song, _, _ = dash_song(other, "d-stopped", a, segment_delay=30.0)
        assert not stream(other, song).headers.get("content-type", "").startswith("audio/")
        joiner = other.services.deliverer.dash
        assert joiner is not None
        assert other.server.call(lambda: _joins(joiner)) == 1  # it goes on
        parts = [p for p in joiner.folder.iterdir() if p.suffix == ".part"]
        assert len(parts) == 1
        started = time.monotonic()
        other.server.stop()
        assert not other.server.thread.is_alive()
        assert time.monotonic() - started < 4.0  # (its segment would take 30 s)
        assert joiner._jobs == {}
        assert not [p for p in joiner.folder.iterdir() if p.name.startswith(".")]


class _Everything(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_no_address_in_any_log_line_and_the_join_s_shape_at_debug(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], dash: Dash
) -> None:
    a, b = a_and_b
    logger = logging.getLogger("shijhon")
    handler, level = _Everything(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        song, _, _ = dash_song(world, "d-logs", a)
        assert stream(world, song).status_code == 200
        refused, _, _ = dash_song(world, "d-logs-refused", a, b, dash_edit=REFUSED["live"])
        assert stream(world, refused).status_code == 200
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    for line in handler.lines:
        for secret in (ADDON_HOST, CDN_HOST, "sig=", "/seg/", "/dash/", "http"):
            assert secret not in line, line
    shapes = [line for line in handler.lines if line.startswith("DASH from A:")]
    assert len(shapes) == 1
    assert "SegmentTemplate+SegmentTimeline, 3 representation(s)" in shapes[0]
    assert "chose flac" in shapes[0] and "3 segment(s)" in shapes[0]


def test_without_ffmpeg_dash_links_are_refused_and_direct_links_play(
    navidrome_factory: NavidromeFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shijhon.app.ffmpeg_path", lambda: None)
    with delivery_world(navidrome_factory(), tmp_path) as other:
        a, b = other.addon("A"), other.addon("B")
        other.add_source(a)
        other.add_source(b)
        song, _, _ = dash_song(other, "d-no-ffmpeg", a, b)
        assert stream(other, song).status_code == 200 and b.requests("audio")
        assert a.requests("stream") and a.requests("mpd") == []
        assert other.services.deliverer.dash_off == "ffmpeg was not found"
        browser = Browser(other.server.base_url)
        browser.sign_in(ADMIN_USER, ADMIN_PASSWORD)
        page = browser.get("addons").text
        assert "DASH links are not played: ffmpeg was not found" in page
        browser.close()
