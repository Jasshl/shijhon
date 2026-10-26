"""Suite K (continued) — streamed songs kept (``[delivery] keep_streamed_audio``).

A song whose whole audio Shijhon received while an app streamed it replaces its
placeholder, as a download's audio does: the same song ID and favorites, the delivered
format, Navidrome's file from then on. A play's start and its seeks fill one file together
(the same size and ETag, or the same link); an add-on that ignores ``Range`` sends the
whole file for a seek, and that is kept; a play the app left, a broken answer and a probe
are not, and nothing more is asked of the add-on for them. DASH songs are kept from the
file Shijhon joined or served at once.
"""

from __future__ import annotations

import dataclasses
import socket
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from shijhon.delivery import download_first
from shijhon.delivery.dash import Dash
from shijhon.delivery.length import PEEK_BYTES
from tests.conftest import NavidromeFactory
from tests.harness.dash_fixtures import dash_audio
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.fragmented import fragmented_mp4, is_fragmented
from tests.harness.library import frequency_for
from tests.harness.logs import collected
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone

SECONDS = 6  # a DASH song: three segments of two seconds


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(), tmp_path_factory.mktemp("kept"), keep_streamed_audio=True
    ) as w:
        yield w


@pytest.fixture
def a(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("A")
    world.add_source(addon)
    yield addon
    world.clear_sources()
    retention = world.services.deliverer.retention
    assert retention is not None
    retention.enabled = True


def stream(
    world: DeliveryWorld, song: str, headers: dict[str, str] | None = None, **params: object
) -> httpx.Response:
    return world.client().request("stream", {"id": song, **params}, headers=headers)


def row(world: DeliveryWorld, song: str) -> dict[str, object]:
    found = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT state, path FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert found is not None
    return dict(found)


def kept(world: DeliveryWorld, song: str, timeout: float = 20.0) -> Path:
    """Until the song's audio is in the library: its file."""
    deadline = time.monotonic() + timeout
    while (found := row(world, song))["state"] != "delivered":
        assert time.monotonic() < deadline, "the streamed audio was not kept"
        time.sleep(0.1)
    return world.services.engine.layout.absolute(str(found["path"]))


def not_kept(world: DeliveryWorld, song: str, wait: float = 1.5) -> None:
    time.sleep(wait)
    assert row(world, song)["state"] == "placeholder"


def test_a_song_streamed_in_full_replaces_its_placeholder(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    song, fakes, audio = world.placeholder_track("kept-whole", [a])
    client = world.client()
    client.ok("star", {"id": song})
    answer = stream(world, song)
    assert answer.status_code == 200 and answer.content == audio.read_bytes()
    path = kept(world, song)
    assert path.suffix == ".flac"
    info = client.ok("getSong", {"id": song})["song"]
    assert info["id"] == song and info["suffix"] == "flac"
    assert song in [e["id"] for e in client.ok("getStarred2")["starred2"].get("song", [])]
    assert len(a.requests("stream")) == 1 and len(a.requests("audio")) == 1  # nothing more
    a.clear()
    again = stream(world, song)  # Navidrome's file now
    assert again.status_code == 200 and a.requests() == []
    assert again.content == path.read_bytes()
    assert fakes[a].isrc  # (the add-on's track)


def test_a_play_s_start_and_seeks_are_kept_together(world: DeliveryWorld, a: FakeAddon) -> None:
    song, _, audio = world.placeholder_track("kept-ranges", [a], seconds=4)
    data = audio.read_bytes()
    middle = len(data) // 2
    for wanted in ("bytes=0-1", f"bytes=0-{middle - 1}", f"bytes={middle + 1000}-"):
        assert stream(world, song, {"range": wanted}).status_code == 206
    not_kept(world, song)  # a gap is left
    assert stream(world, song, {"range": f"bytes={middle - 10}-{middle + 1009}"}).status_code == 206
    kept(world, song)
    assert len(a.requests("stream")) == 1  # one link for all of them


def test_a_seek_at_an_add_on_that_ignores_ranges_brings_the_whole_file(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """Its answer to a seek is the whole file, cut to the range for the app: Shijhon
    received every byte, and keeps them. (The song is longer than what the first request
    reads to check its length.)"""
    song, _, audio = world.placeholder_track(
        "kept-no-ranges", [a], ranges=False, seconds=60, chunk_delay=0.01
    )
    data = audio.read_bytes()
    assert len(data) > PEEK_BYTES
    first = stream(world, song, {"range": "bytes=0-99"})
    assert first.status_code == 206 and first.content == data[:100]
    not_kept(world, song, wait=0.5)
    seek = stream(world, song, {"range": "bytes=5000-"})
    assert seek.status_code == 206 and seek.content == data[5000:]
    kept(world, song)


def test_without_an_etag_the_answers_of_one_link_are_kept_together(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    song, _, audio = world.placeholder_track("kept-no-etag", [a], etag=False)
    data = audio.read_bytes()
    assert stream(world, song, {"range": "bytes=0-4999"}).content == data[:5000]
    assert stream(world, song, {"range": "bytes=5000-"}).content == data[5000:]
    kept(world, song)


def test_another_format_is_kept_in_its_own(world: DeliveryWorld, a: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("kept-m4a", [a], fmt="m4a", content_type="audio/mp4")
    assert stream(world, song).status_code == 200
    assert kept(world, song).suffix == ".m4a"
    assert world.client().ok("getSong", {"id": song})["song"]["suffix"] == "m4a"


def test_a_play_the_app_left_is_not_kept_and_not_finished(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    song, fakes, audio = world.placeholder_track("kept-left", [a], seconds=60, chunk_delay=0.05)
    size = audio.stat().st_size
    client = world.client()
    query = str(httpx.QueryParams([*client.auth_params(), ("id", song)]))
    with socket.create_connection(("127.0.0.1", world.server.port)) as sock:
        sock.sendall(f"GET /rest/stream?{query} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        sock.recv(4096)
    time.sleep(1.0)
    sent = a.bytes_sent.get(fakes[a].isrc, 0)
    not_kept(world, song)
    assert a.bytes_sent.get(fakes[a].isrc, 0) == sent < size
    assert len(a.requests("audio")) == 1


def test_a_broken_answer_is_not_kept(world: DeliveryWorld, a: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("kept-broken", [a], truncate_after=20000)
    with pytest.raises(httpx.HTTPError):
        stream(world, song).read()
    not_kept(world, song)


def test_nothing_is_kept_while_the_setting_is_off(world: DeliveryWorld, a: FakeAddon) -> None:
    retention = world.services.deliverer.retention
    assert retention is not None
    retention.enabled = False  # (as the dashboard sets it)
    song, _, audio = world.placeholder_track("kept-off", [a])
    assert stream(world, song).content == audio.read_bytes()
    not_kept(world, song)


def test_a_probe_is_not_kept(world: DeliveryWorld, a: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("kept-probe", [a])
    assert world.client().request("stream", {"id": song}, http_method="HEAD").status_code == 200
    not_kept(world, song, wait=0.5)


# --- DASH ---------------------------------------------------------------------------------


@pytest.fixture
def dash(world: DeliveryWorld) -> Iterator[Dash]:
    joiner = world.services.deliverer.dash
    assert joiner is not None
    yield joiner
    world.services.deliverer.settings.dash_start = "at_once"


def dash_song(world: DeliveryWorld, key: str, a: FakeAddon) -> str:
    song, _, _ = world.placeholder_track(key, [], seconds=SECONDS)
    found = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT isrc FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert found is not None
    folder = dash_audio(key, SECONDS, "timeline", 1)
    a.add(FakeTrack(isrc=str(found["isrc"]), audio=folder / "out.mpd", dash=folder))
    return song


def test_a_dash_song_served_at_once_is_kept_once_its_segments_are_there(
    world: DeliveryWorld, a: FakeAddon, dash: Dash
) -> None:
    """The app read only its start; the file Shijhon fetched for it has every segment
    (no request is made for keeping it): copied out as a FLAC."""
    song = dash_song(world, "kept-dash", a)
    assert stream(world, song, {"range": "bytes=0-1"}).status_code == 206
    path = kept(world, song)
    assert path.suffix == ".flac"
    assert world.client().ok("getSong", {"id": song})["song"]["id"] == song


def test_a_joined_dash_song_is_kept(world: DeliveryWorld, a: FakeAddon, dash: Dash) -> None:
    world.services.deliverer.settings.dash_start = "complete"
    song = dash_song(world, "kept-joined", a)
    assert stream(world, song).status_code == 200
    assert kept(world, song).suffix == ".flac"


def test_audio_a_download_put_in_place_meanwhile_is_never_replaced(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """An app downloads the song while another one streams it: the download's file is the
    library's, and the stream that ends after it changes nothing."""
    song, _, audio = world.placeholder_track("kept-race", [a], seconds=30, chunk_delay=0.1)
    assert audio.stat().st_size > 16384 * 15  # (the stream takes more than a second)
    with collected("shijhon.delivery.retain") as lines, ThreadPoolExecutor(1) as pool:
        playing = pool.submit(lambda: stream(world, song).content)
        time.sleep(0.3)
        assert world.client(client="other-app").request("download", {"id": song}).status_code == 200
        path = kept(world, song)
        downloaded = path.read_bytes()
        assert playing.result() == audio.read_bytes()
        deadline = time.monotonic() + 10
        while not any("its audio is in place already" in line for line in lines):
            assert time.monotonic() < deadline, lines
            time.sleep(0.1)
    assert row(world, song)["state"] == "delivered"
    assert path.read_bytes() == downloaded


def isrc_of(world: DeliveryWorld, song: str) -> str:
    found = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT isrc FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert found is not None
    return str(found["isrc"])


def packets(path: Path) -> list[str]:
    """The checksums of a file's audio packets as they are encoded (none decoded): the same
    list for the same audio in another container."""
    found = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-c", "copy",
         "-f", "framemd5", "-"],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    # (stream, dts, pts, duration, size, the packet's checksum[, its side data's])
    return [line.split(",")[5].strip() for line in found.splitlines() if line[:1].isdigit()]


@pytest.mark.parametrize(
    ("key", "suffix"),
    [("wav-pcm", ".wav"), ("wav-float", ".wav"), ("wavpack", ".wv"), ("wma", ".wma"),
     ("matroska-flac", ".mka"), ("aac-adts", ".m4a"), ("aac-m4a", ".m4a"),
     ("alac-m4a", ".m4a"), ("mp3", ".mp3"), ("flac", ".flac"), ("opus", ".opus"),
     ("vorbis", ".ogg"), ("ogg-flac", ".flac"), ("speex-spx", ".spx"), ("aiff", ".aiff"),
     ("dsf", ".dsf"), ("monkeys-audio", ".ape"), ("musepack", ".mpc")],
)  # fmt: skip
def test_a_song_in_another_container_is_kept_in_it(
    world: DeliveryWorld, a: FakeAddon, tmp_path: Path, key: str, suffix: str
) -> None:
    """An add-on's audio in any container a kept stream takes: kept as it is, tagged in its
    own family (ID3, APEv2, ASF, Matroska's by an ffmpeg stream copy), under the same song
    ID - and Navidrome reads its length. Raw ADTS AAC and Ogg FLAC, whose length Navidrome
    does not read, go in as the same audio in an M4A and a native FLAC."""
    song, _, _ = world.placeholder_track(f"kept-{key}", [], seconds=1)
    formats = {f.key: f for f in IMPORT_FORMATS}
    sample = import_tone(tmp_path, formats[key], tagged=False)
    a.add(FakeTrack(isrc=isrc_of(world, song), audio=sample,
                    content_type="application/octet-stream"))  # fmt: skip
    assert stream(world, song).content == sample.read_bytes()
    path = kept(world, song)
    assert path.suffix == suffix
    assert world.client().ok("getSong", {"id": song})["song"]["id"] == song
    native = world.server.call(lambda: world.services.navidrome.song(song))
    assert native is not None and native["duration"] > 0.2  # (its length read: 0.3 s)


@pytest.mark.parametrize(
    ("codec", "mehd", "suffix"),
    [("aac", True, ".m4a"), ("aac", False, ".m4a"), ("alac", True, ".m4a"),
     ("flac", True, ".flac"), ("mp3", False, ".mp3"), ("opus", True, ".m4a")],
)  # fmt: skip
def test_a_fragmented_mp4_is_kept_in_one_piece_with_its_length(
    world: DeliveryWorld, a: FakeAddon, tmp_path: Path, codec: str, mehd: bool, suffix: str
) -> None:
    """An add-on's whole song as a fragmented MP4 - its movie header tells no length, and
    Navidrome reads none from it (zero: apps show 0:00 and cannot seek) - goes in as the
    same encoded audio copied into one piece, as a DASH song's: FLAC as a native FLAC, MP3
    as an MP3, AAC, ALAC and anything else in an ordinary MP4. The app was streamed the
    add-on's file as it is; Navidrome lists the song under its ID with its length."""
    song, _, _ = world.placeholder_track(f"kept-frag-{codec}-{mehd}", [], seconds=6)
    sample = fragmented_mp4(tmp_path / "fragmented.m4a", frequency_for(song), 6, codec,
                            mehd=mehd)  # fmt: skip
    a.add(FakeTrack(isrc=isrc_of(world, song), audio=sample, content_type="audio/mp4"))
    assert stream(world, song).content == sample.read_bytes()
    path = kept(world, song)
    assert path.suffix == suffix and not is_fragmented(path)
    assert packets(path) == packets(sample)  # (nothing encoded anew)
    native = world.server.call(lambda: world.services.navidrome.song(song))
    assert native is not None and native["id"] == song
    assert abs(native["duration"] - 6) < 0.5


@pytest.mark.parametrize(
    ("codec", "options", "suffix", "streams"),
    [("flac", {"held": True, "mehd": False}, ".flac", 1), ("aac", {"tracks": 2}, ".m4a", 2)],
)
def test_a_fragmented_mp4_of_several_tracks_or_with_samples_in_its_header_is_kept_whole(
    world: DeliveryWorld,
    a: FakeAddon,
    tmp_path: Path,
    codec: str,
    options: dict[str, Any],
    suffix: str,
    streams: int,
) -> None:
    """A fragmented MP4 whose header holds its first fragment's samples (its header tells only
    their length): its FLAC is counted whole. One of two audio tracks: both stay, in an
    ordinary MP4, and Navidrome reads the first's length - neither is dropped, nor the
    length counted twice."""
    song, _, _ = world.placeholder_track(f"kept-frag-{codec}-{streams}", [], seconds=6)
    sample = fragmented_mp4(tmp_path / "fragmented.m4a", frequency_for(song), 6, codec, **options)
    a.add(FakeTrack(isrc=isrc_of(world, song), audio=sample, content_type="audio/mp4"))
    assert stream(world, song).content == sample.read_bytes()
    path = kept(world, song)
    assert path.suffix == suffix and not is_fragmented(path)
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
         str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    assert probed == ["audio"] * streams
    assert packets(path) == packets(sample)  # (its first track's)
    native = world.server.call(lambda: world.services.navidrome.song(song))
    assert native is not None and native["id"] == song
    assert abs(native["duration"] - 6) < 0.5


@pytest.mark.parametrize("layout", ["unknown", "text"])
def test_a_fragmented_mp4_whose_tracks_are_not_told_or_with_text_keeps_its_audio(
    world: DeliveryWorld,
    a: FakeAddon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    """When ffprobe cannot tell its tracks (here: not run), every audio track is copied out
    - none is dropped on a guess. A timed-text track beside one audio track goes, as from a
    DASH song's file; the audio is copied out as usual."""
    song, _, _ = world.placeholder_track(f"kept-frag-{layout}", [], seconds=6)
    sample = fragmented_mp4(tmp_path / "fragmented.m4a", frequency_for(song), 6,
                            tracks=2 if layout == "unknown" else 1)  # fmt: skip
    if layout == "text":
        lyrics, texted = tmp_path / "lyrics.srt", tmp_path / "texted.m4a"
        lyrics.write_text("1\n00:00:00,000 --> 00:00:03,000\nAn invented line\n")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(sample), "-i", str(lyrics),
             "-map", "0:a", "-map", "1:s", "-c:a", "copy", "-c:s", "mov_text",
             "-movflags", "+frag_keyframe+empty_moov+default_base_moof+skip_trailer",
             "-frag_duration", "2000000", "-f", "mp4", str(texted)],
            check=True, capture_output=True,
        )  # fmt: skip
        sample = texted
    else:
        monkeypatch.setattr(download_first, "_probe", lambda path: None)
    a.add(FakeTrack(isrc=isrc_of(world, song), audio=sample, content_type="audio/mp4"))
    assert stream(world, song).content == sample.read_bytes()
    path = kept(world, song)
    assert path.suffix == ".m4a" and not is_fragmented(path)
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
         str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    assert probed == ["audio"] * (2 if layout == "unknown" else 1)
    native = world.server.call(lambda: world.services.navidrome.song(song))
    assert native is not None and abs(native["duration"] - 6) < 0.5


def test_a_fragmented_mp4_of_another_length_is_not_kept(
    world: DeliveryWorld, a: FakeAddon, tmp_path: Path
) -> None:
    """Its length is known once it is in one piece: another recording (here 30 s for the
    catalog's 6 s, past a 10 s tolerance) stays out, as any kept stream of another length
    does. (Without a ``mehd`` box its first bytes told no length to the play.)"""
    song, _, _ = world.placeholder_track("kept-frag-long", [], seconds=6)
    sample = fragmented_mp4(tmp_path / "long.m4a", frequency_for(song), 30, mehd=False)
    a.add(FakeTrack(isrc=isrc_of(world, song), audio=sample, content_type="audio/mp4"))
    settings = world.services.deliverer.settings
    settings.length_tolerance_seconds = 10.0
    try:
        with collected("shijhon.delivery.retain") as lines:
            assert stream(world, song).content == sample.read_bytes()
            not_kept(world, song, wait=3)
    finally:
        settings.length_tolerance_seconds = 0.0
    assert any("another recording" in line for line in lines)


def listed(world: DeliveryWorld, song: str) -> dict[str, Any]:
    """Navidrome's identity and metadata of a song (not of its file)."""
    found = world.server.call(lambda: world.services.navidrome.song(song))
    assert found is not None
    roles = found.get("participants") or {}
    return {
        "id": found["id"], "albumId": found["albumId"], "title": found["title"],
        "artist": found.get("artist"),
        "genres": sorted(g["name"] for g in found.get("genres") or []),
        "participants": {r: sorted(p["name"] for p in roles[r]) for r in sorted(roles)},
    }  # fmt: skip


def matroska_song(
    world: DeliveryWorld,
    a: FakeAddon,
    tmp_path: Path,
    key: str,
    *,
    cover: bool = False,
    artists: tuple[str, ...] = ("Ann", "Bob"),
) -> tuple[str, Path]:
    """A one-song catalog release with ``artists`` and several genres; the add-on's audio
    is FLAC in Matroska (``cover``: with a cover picture, attached as Matroska keeps one -
    ffprobe lists it as a video stream that is an attached picture)."""
    base = catalog_release(key, f"Title {key}", artists[0], 1, seconds=1)
    track = dataclasses.replace(base.tracks[0], artists=artists)
    release = dataclasses.replace(base, tracks=(track,), artists=artists, genres=("Folk", "Jazz"))
    song = world.materialize(release).created[track.ref]
    sample = import_tone(tmp_path, {f.key: f for f in IMPORT_FORMATS}["matroska-flac"],
                         tagged=False)  # fmt: skip
    if cover:
        picture, covered = tmp_path / "cover.png", tmp_path / "covered.mka"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=16x16",
                        "-frames:v", "1", str(picture)], check=True)  # fmt: skip
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(sample), "-attach", str(picture),
                        "-metadata:s:t", "mimetype=image/png", "-map", "0", "-c", "copy",
                        str(covered)], check=True)  # fmt: skip
        sample = covered
    assert track.isrc is not None
    a.add(FakeTrack(isrc=track.isrc, audio=sample, content_type="audio/x-matroska"))
    return song, sample


def test_a_matroska_song_with_several_artists_keeps_them_all(
    world: DeliveryWorld, a: FakeAddon, tmp_path: Path
) -> None:
    """Matroska cannot carry several artists so that Navidrome reads them (ffmpeg writes one
    value a name): the same FLAC audio goes in, copied out of it as a FLAC - and Navidrome
    lists the song as before, its artists and genres too."""
    song, sample = matroska_song(world, a, tmp_path, "kept-mka-duo")
    before = listed(world, song)
    assert before["participants"]["artist"] == ["Ann", "Bob"]
    assert stream(world, song).content == sample.read_bytes()
    assert kept(world, song).suffix == ".flac"
    assert listed(world, song) == before


def test_a_matroska_song_with_a_cover_picture_is_kept(
    world: DeliveryWorld, a: FakeAddon, tmp_path: Path
) -> None:
    """One artist: kept in Matroska, its several genres as one value Navidrome splits."""
    song, sample = matroska_song(world, a, tmp_path, "kept-mka-cover", cover=True, artists=("Ann",))
    before = listed(world, song)
    assert before["genres"] == ["Folk", "Jazz"]
    assert stream(world, song).content == sample.read_bytes()
    assert kept(world, song).suffix == ".mka"
    assert listed(world, song) == before
