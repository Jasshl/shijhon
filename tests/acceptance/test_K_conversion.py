"""Suite K (continued) - no conversion when none is needed.

Navidrome 0.64.2 serves a file as it is when a request's format is the file's own and its
bitrate at most the requested one (or none is requested), when a bitrate alone is at least
the file's, and whatever an offset alone says (it applies only to a conversion): checked
here against the test Navidrome with an owned file. A placeholder whose delivered audio
meets such a request is therefore a plain stream from the add-ons; only requests that
really need converting go download-first (the link found is used for its fetch).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from shijhon.delivery.length import audio_kind, audio_length
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon

SECONDS = 30  # long enough that an MP3's Info header makes no rounding difference


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("conversion")) as w:
        yield w


@pytest.fixture
def addon(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    source = world.addon("Source")
    world.add_source(source)
    yield source
    world.clear_sources()


def mp3(path: Path, bitrate: int, title: str = "") -> Path:
    """An MP3 at ``bitrate`` kbit/s (0: variable, LAME's best)."""
    tags = ["-metadata", f"title={title}", "-metadata", "artist=Conversion Band",
            "-metadata", "album=Conversion Record"] if title else []  # fmt: skip
    rate = ["-b:a", f"{bitrate}k"] if bitrate else ["-q:a", "0"]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=550:duration={SECONDS}", "-ac", "2", "-c:a", "libmp3lame",
         *rate, *tags, str(path)],
        check=True,
    )  # fmt: skip
    return path


def kind_of(data: bytes) -> tuple[str, int | None] | None:
    head = data[:65536]
    return audio_kind(head, len(data), audio_length(head, len(data)))


def audio_gets(addon: FakeAddon) -> int:
    return len(addon.requests("audio"))


def stream(world: DeliveryWorld, song: str, **params: object) -> httpx.Response:
    return world.client().request("stream", {"id": song, **params})


def state(world: DeliveryWorld, song: str) -> str:
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT state FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert row is not None
    return str(row["state"])


def test_navidrome_serves_a_file_that_needs_no_converting_as_it_is(world: DeliveryWorld) -> None:
    """The rules Shijhon follows, as the test Navidrome applies them to an owned MP3 at
    320 kbit/s - and the bitrate Shijhon reads from its first bytes is Navidrome's."""
    folder = world.nd.music / "Conversion Band" / "Conversion Record"
    folder.mkdir(parents=True, exist_ok=True)
    data = mp3(folder / "01 Owned.mp3", 320, "Owned Conversion").read_bytes()
    variable = mp3(folder / "02 Variable.mp3", 0, "Variable Conversion").read_bytes()
    world.nd.scan(targets=["Conversion Band/Conversion Record"])
    [song] = world.nd.client().ok("search3", {"query": "Owned Conversion"})["searchResult3"]["song"]
    assert song["bitRate"] == 320 and song["suffix"] == "mp3"
    assert kind_of(data) == ("mp3", 320)
    [vbr] = world.nd.client().ok("search3", {"query": "Variable Conversion"})["searchResult3"][
        "song"
    ]
    assert kind_of(variable) == ("mp3", vbr["bitRate"])  # TagLib's reading, rounding too
    for params in (
        {"format": "mp3", "maxBitRate": 320},
        {"format": "mp3"},
        {"maxBitRate": 320},
        {"timeOffset": 5},
        {"format": "raw", "maxBitRate": 128},
    ):
        answer = world.nd.client().request("stream", {"id": song["id"], **params})
        assert answer.content == data, params
    for params in ({"format": "mp3", "maxBitRate": 128}, {"format": "opus"}):
        answer = world.nd.client().request("stream", {"id": song["id"], **params})
        assert answer.status_code == 200 and answer.content != data, params


def test_a_request_the_delivered_audio_meets_is_a_plain_stream(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """The source delivers MP3 at 320 kbit/s, the client asks for MP3
    at 320 - streamed as it is, not fetched first; likewise a bitrate alone at or above it."""
    song, fakes, _ = world.placeholder_track("conv-mp3", [addon], seconds=SECONDS)
    audio = mp3(world.tmp / "conv-mp3-320.mp3", 320)
    fakes[addon].audio = audio
    fakes[addon].content_type = "audio/mpeg"
    fetches = world.services.download_first.fetches
    for params in ({"format": "mp3", "maxBitRate": 320}, {"maxBitRate": 320}):
        answer = stream(world, song, **params)
        assert answer.status_code == 200 and answer.content == audio.read_bytes(), params
        assert answer.headers["content-type"] == "audio/mpeg"
    assert world.services.download_first.fetches == fetches
    assert state(world, song) == "placeholder"


def test_a_request_that_needs_converting_is_download_first_on_the_link_found(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A lower bitrate than the delivered audio's: download-first, then Navidrome converts
    the real file - the fetch reads on from the answer its first bytes came from: the link
    is looked up once and asked once."""
    song, fakes, _ = world.placeholder_track("conv-lower", [addon], seconds=SECONDS)
    audio = mp3(world.tmp / "conv-lower-320.mp3", 320)
    fakes[addon].audio = audio
    fakes[addon].content_type = "audio/mpeg"
    answer = stream(world, song, format="mp3", maxBitRate=128)
    assert answer.status_code == 200 and answer.content != audio.read_bytes()
    assert state(world, song) == "delivered"
    assert len(addon.requests("stream")) == 1  # one link: the fetch did not route again
    assert audio_gets(addon) == 1  # its first bytes, and the rest of the file from there
    delivered = world.nd.native("GET", f"song/{song}").json()["path"]
    placed = (world.nd.music / delivered).read_bytes()  # its tags are the placeholder's
    assert placed.endswith(audio.read_bytes()[-65536:])  # the whole file, to its end


def test_a_conversion_is_navidrome_s_own_at_the_bitrate_asked(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A request for AAC at 256 kbit/s goes to Navidrome as it was sent
    once the audio is in place - Shijhon neither drops nor changes the format or the
    bitrate: the answer is, byte for byte, what Navidrome itself gives for that request,
    at the bitrate asked (and another one at another bitrate)."""
    sizes = {}
    # (A placeholder of its own for each: the request that fetches is the one looked at;
    # 96 first - 256 is also what Navidrome converts to when no bitrate is named.)
    for bitrate in (96, 256, 320):
        song, fakes, _ = world.placeholder_track(f"conv-aac-{bitrate}", [addon], seconds=SECONDS)
        fakes[addon].audio = mp3(world.tmp / f"conv-aac-{bitrate}.mp3", 320)
        fakes[addon].content_type = "audio/mpeg"
        ours = stream(world, song, format="aac", maxBitRate=bitrate)
        assert ours.status_code == 200 and ours.headers["content-type"] == "audio/aac"
        assert state(world, song) == "delivered"
        direct = world.nd.client().request(
            "stream", {"id": song, "format": "aac", "maxBitRate": bitrate}
        )
        assert ours.content == direct.content and len(ours.content) > 20_000
        sizes[bitrate] = len(ours.content)
        assert len(ours.content) * 8 / SECONDS / 1000 <= bitrate * 1.1  # kbit/s
    assert len(set(sizes.values())) == 3  # (a tone: the encoder stays below what is asked)


def test_a_probe_that_needs_converting_is_fetched_whole_by_a_request_of_its_own(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A client's short range at byte zero holds no whole file to read on from: its answer
    is closed, and the fetch asks the link itself."""
    song, _, _ = world.placeholder_track("conv-probe", [addon])
    answer = world.client().request(
        "stream", {"id": song, "format": "mp3"}, headers={"Range": "bytes=0-1"}
    )
    assert answer.headers["content-type"] == "audio/mpeg"  # Navidrome's conversion
    assert state(world, song) == "delivered"
    assert len(addon.requests("stream")) == 1  # one link
    assert [r["range"] for r in addon.requests("audio")] == ["bytes=0-1", None]


def test_a_stream_its_audio_meets_takes_nothing_from_the_hour_s_allowance(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """Its first bytes are read in the user's turn (the allowance must not be used up), but
    a request served as it is takes none of it; one that needs converting does."""
    limits = world.services.download_first.limits
    assert limits is not None
    saved = limits.per_hour
    limits.per_hour = 2
    limits._users.clear()
    try:
        song, fakes, _ = world.placeholder_track("conv-allowance", [addon], seconds=SECONDS)
        audio = mp3(world.tmp / "conv-allowance-320.mp3", 320)
        fakes[addon].audio = audio
        fakes[addon].content_type = "audio/mpeg"
        assert stream(world, song, format="mp3", maxBitRate=320).content == audio.read_bytes()
        assert all(r.allowance > 1.99 for r in limits._users.values())  # none taken
        assert stream(world, song, format="mp3", maxBitRate=128).status_code == 200
        assert state(world, song) == "delivered"
        [record] = limits._users.values()
        assert 0.99 < record.allowance < 1.5  # the download took one
    finally:
        limits.per_hour = saved
        limits._users.clear()


def test_a_flac_request_of_flac_and_an_offset_alone_are_plain_streams(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    song, _, audio = world.placeholder_track("conv-flac", [addon])
    fetches = world.services.download_first.fetches
    for params in ({"format": "flac"}, {"timeOffset": 10}, {"format": "FLAC", "timeOffset": 1}):
        answer = stream(world, song, **params)
        assert answer.content == audio.read_bytes(), params
    assert world.services.download_first.fetches == fetches
    assert state(world, song) == "placeholder"
    # Another format still needs converting.
    answer = stream(world, song, format="mp3")
    assert answer.headers["content-type"] == "audio/mpeg"
    assert state(world, song) == "delivered"


def test_a_seek_on_a_link_read_already_is_decided_by_it(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A later range of the play that started as it is continues as it is (its link's first
    bytes told already), not download-first in the middle of it."""
    song, fakes, _ = world.placeholder_track("conv-seek", [addon], seconds=SECONDS)
    audio = mp3(world.tmp / "conv-seek-320.mp3", 320)
    fakes[addon].audio = audio
    fakes[addon].content_type = "audio/mpeg"
    params = {"id": song, "format": "mp3", "maxBitRate": 320}
    assert world.client().request("stream", params).content == audio.read_bytes()
    later = world.client().request("stream", params, headers={"Range": "bytes=1000-1999"})
    assert later.status_code == 206 and later.content == audio.read_bytes()[1000:2000]
    assert state(world, song) == "placeholder"


def test_a_link_read_already_that_is_replaced_decides_by_the_new_audio(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """The play's link (MP3 at 320) no longer works and its source fails: the next source's
    audio (FLAC) needs converting for the request - download-first, not that FLAC as it
    is."""
    other = world.addon("Other")
    world.add_source(other)
    song, fakes, _ = world.placeholder_track("conv-replaced", [addon, other], seconds=SECONDS)
    audio = mp3(world.tmp / "conv-replaced-320.mp3", 320)
    fakes[addon].audio = audio
    fakes[addon].content_type = "audio/mpeg"
    fakes[addon].expire_after = 1  # its link answers once
    params = {"format": "mp3", "maxBitRate": 320}
    assert stream(world, song, **params).content == audio.read_bytes()
    fakes[addon].stream_status = 503  # and it fails now
    answer = stream(world, song, **params)
    assert answer.status_code == 200 and answer.headers["content-type"] == "audio/mpeg"
    assert answer.content != audio.read_bytes()
    assert state(world, song) == "delivered"  # the FLAC, converted by Navidrome


def test_a_later_range_after_the_link_expired_stays_as_it_is(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """The play's link expired meanwhile (a paused song): its representation, known with
    its format, still meets the request - a plain stream at the same file, not
    download-first in the middle of the play."""
    song, fakes, _ = world.placeholder_track("conv-expired", [addon], seconds=SECONDS)
    audio = mp3(world.tmp / "conv-expired-320.mp3", 320)
    fakes[addon].audio = audio
    fakes[addon].content_type = "audio/mpeg"
    params = {"id": song, "format": "mp3", "maxBitRate": 320}
    assert world.client().request("stream", params).content == audio.read_bytes()
    settings = world.services.deliverer.settings
    saved, settings.pin_ttl_seconds = settings.pin_ttl_seconds, 0.0
    try:
        later = world.client().request("stream", params, headers={"Range": "bytes=1000-1999"})
    finally:
        settings.pin_ttl_seconds = saved
    assert later.status_code == 206 and later.content == audio.read_bytes()[1000:2000]
    assert state(world, song) == "placeholder"


def test_head_decides_by_a_link_read_already_else_download_first(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    song, fakes, _ = world.placeholder_track("conv-head", [addon], seconds=SECONDS)
    audio = mp3(world.tmp / "conv-head-320.mp3", 320)
    fakes[addon].audio = audio
    fakes[addon].content_type = "audio/mpeg"
    params = {"id": song, "format": "mp3", "maxBitRate": 320}
    assert world.client().request("stream", params).status_code == 200
    head = world.client().request("stream", params, http_method="HEAD")
    assert head.status_code == 200 and head.headers["content-length"] == str(audio.stat().st_size)
    assert state(world, song) == "placeholder"
    other, _, _ = world.placeholder_track("conv-head-unread", [addon])
    world.client().request("stream", {"id": other, "format": "mp3"}, http_method="HEAD")
    assert state(world, other) == "delivered"  # nothing read to tell by: as before


def test_a_request_without_a_routing_turn_goes_download_first_as_before(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """The user's lookups are all taken (slow plays) until the wait ends: the request is
    not streamed as it is untold - download-first, which has its own limits, converts it."""
    limits = world.services.download_first.limits
    assert limits is not None
    settings = world.services.deliverer.settings
    slow = [world.placeholder_track(f"conv-busy-{n}", [addon])[0] for n in range(2)]
    for track in addon.tracks.values():
        track.resolve_delay = 3.0
    song, fakes, _ = world.placeholder_track("conv-busy-mp3", [addon])
    fakes[addon].resolve_delay = 0.0
    saved, waited = limits.routings, settings.max_wait_seconds
    limits.routings = 1
    limits._users.clear()
    settings.max_wait_seconds = 1.5
    try:
        with ThreadPoolExecutor(3) as pool:
            busy = [pool.submit(stream, world, s) for s in slow]
            time.sleep(0.3)
            answer = stream(world, song, format="mp3", maxBitRate=96)
            [b.result() for b in busy]
    finally:
        limits.routings = saved
        limits._users.clear()
        settings.max_wait_seconds = waited
        for track in addon.tracks.values():
            track.resolve_delay = 0.0
    assert answer.status_code == 200 and answer.headers["content-type"] == "audio/mpeg"
    assert state(world, song) == "delivered"
