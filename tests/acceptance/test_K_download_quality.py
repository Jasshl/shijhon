"""Quality replacements of explicit downloads obey download admission and refusals."""

from pathlib import Path

import anyio
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_USER


@pytest.mark.parametrize("delivered", [False, True], ids=["owned", "delivered"])
@pytest.mark.parametrize("available", [False, True], ids=["refused", "permitted"])
def test_quality_downloads_use_only_permitted_audio_under_bulk_admission(
    navidrome_factory: NavidromeFactory, tmp_path: Path, delivered: bool, available: bool
) -> None:
    nd = navidrome_factory()
    write_album(
        nd.music,
        Album(
            "Invented Hollow",
            "Download Ember",
            (Track("Low Ember", 1, isrc="XXQ0L2000301"),),
            "mp3",
        ),
    )
    nd.scan(full=True)
    native = nd.client().ok("search3", {"query": "Low Ember"})["searchResult3"]["song"][0]
    with delivery_world(nd, tmp_path, warm_ahead_depth=0) as world:
        song = native["id"]
        if delivered:
            release = catalog_release(
                "quality-download", "Low Album", "Invented Hollow", 1, seconds=3
            )
            song = world.materialize(release).created[release.tracks[0].ref]
            world.server.call(lambda: world.services.engine.set_backing(song, native["id"]))
            assert (
                world.server.call(lambda: world.services.download_first.ensure(song, ADMIN_USER))
                is None
            )

            async def state() -> str:
                row = await world.services.store.fetchone(
                    "SELECT state FROM placeholders WHERE song_id = ?", [song]
                )
                assert row is not None
                return str(row["state"])

            assert world.server.call(state) == "delivered"
        first = world.addon("No downloads")
        first.manifest_extra = {"allowDownloads": False}
        world.add_source(first)
        flac = world.audio("better-download", "flac", 3)
        isrc = release.tracks[0].isrc if delivered else "XXQ0L2000301"
        first.add(FakeTrack(isrc=isrc, audio=flac))
        if available:
            low = world.addon("Low download")
            world.add_source(low)
            low.add(
                FakeTrack(
                    isrc=isrc,
                    audio=world.audio("low-download", "mp3", 3),
                    content_type="audio/mpeg",
                )
            )
            other = world.addon("Permitted download")
            world.add_source(other)
            other.add(FakeTrack(isrc=isrc, audio=flac))
        minimums = world.services.quality
        assert minimums is not None
        minimums.default = "192"
        bulk = world.services.download_first.bulk
        assert bulk is not None
        calls: list[str] = []
        original = bulk.open

        async def opened(*args, **kwargs):
            calls.append(args[0].song_id)
            return await original(*args, **kwargs)

        bulk.open = opened

        async def released() -> None:
            with anyio.fail_after(5):
                while bulk._active:  # noqa: ASYNC110
                    await anyio.sleep(0.01)

        answer = world.client().request("download", {"id": song})
        assert answer.status_code == 200 and calls == [song]
        assert not first.requests("stream") and not first.requests("audio")
        if available:
            assert answer.content == flac.read_bytes()
            assert low.requests("audio") and other.requests("audio")
            world.server.call(released)
            head = world.client().request("download", {"id": song}, http_method="HEAD")
            assert head.status_code == 200 and head.content == b""
            part = world.client().request(
                "download", {"id": song}, headers={"range": "bytes=10-99"}
            )
            assert part.status_code == 206 and part.content == flac.read_bytes()[10:100]
            world.server.call(released)
        else:
            assert answer.content[:4] != b"fLaC"
