"""Suite K (continued) - each listener's play keeps the file it began with.

Two listeners with different minimum qualities play one song whose own file is below
both: the first's play comes from an add-on's 192 kbit/s MP3, the second's - which wants
lossless audio - from another add-on's FLAC. Each play's later ranges stay on its own file,
whatever the other's play is of.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.subsonic import SubsonicClient

ISRC = "XXQ0L2000101"
LOW = ("play-files-low", "invented-password-l")  # at least 160 kbit/s
LOSSLESS = ("play-files-lossless", "invented-password-ll")  # lossless only


@dataclass
class Two:
    world: DeliveryWorld
    song: str
    mp3: bytes
    flac: bytes


@pytest.fixture(scope="module")
def two(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Two]:
    nd = navidrome_factory()
    write_album(
        nd.music,
        Album("Two Rivers", "Twin Road", (Track("Twin Ember", 1, seconds=20, isrc=ISRC),), "mp3"),
    )  # (a 128 kbit/s MP3)
    nd.scan(full=True)
    song = nd.client().ok("search3", {"query": "Twin Ember", "songCount": 1})["searchResult3"][
        "song"
    ][0]["id"]
    low_id, lossless_id = nd.create_user(*LOW), nd.create_user(*LOSSLESS)
    tmp = tmp_path_factory.mktemp("play-files")
    with delivery_world(nd, tmp, warm_ahead_depth=0) as world:
        first, second = world.addon("Lossy"), world.addon("Lossless")
        world.add_source(first)
        world.add_source(second)
        mp3 = tmp / "twin-192.mp3"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
             "sine=frequency=470:duration=20", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k",
             str(mp3)],
            check=True,
        )  # fmt: skip
        flac = world.audio("twin-flac", "flac", 20)
        first.add(FakeTrack(isrc=ISRC, audio=mp3, content_type="audio/mpeg"))
        second.add(FakeTrack(isrc=ISRC, audio=flac))
        minimums = world.services.quality
        assert minimums is not None
        world.server.call(lambda: minimums.set(low_id, "160"))
        world.server.call(lambda: minimums.set(lossless_id, "lossless"))
        yield Two(world, song, mp3.read_bytes(), flac.read_bytes())


def test_each_listener_s_seek_stays_on_the_file_its_play_began_with(two: Two) -> None:
    base = two.world.server.base_url
    low, lossless = SubsonicClient(base, *LOW), SubsonicClient(base, *LOSSLESS)
    try:
        assert low.request("stream", {"id": two.song}).content == two.mp3
        assert lossless.request("stream", {"id": two.song}).content == two.flac
        seek = low.request("stream", {"id": two.song}, headers={"range": "bytes=1000-"})
        assert seek.status_code == 206 and seek.content == two.mp3[1000:]
        other = lossless.request("stream", {"id": two.song}, headers={"range": "bytes=1000-"})
        assert other.status_code == 206 and other.content == two.flac[1000:]
    finally:
        low.close()
        lossless.close()
