"""Whole-file bulk scheduling through real Navidrome and synthetic audio."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from shijhon.delivery.bulk import BulkAcquisition
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("bulk")) as world:

        async def install() -> None:
            services = world.services
            services.download_first.bulk = BulkAcquisition(services.deliverer)

        world.server.call(install)
        yield world


def test_legacy_add_on_transfers_overlap_but_full_bodies_stay_bounded(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon("Demo bulk")
    world.add_source(addon)
    songs = [
        world.placeholder_track(f"bulk-{i}", [addon], seconds=60, chunk_delay=0.02)[0]
        for i in range(4)
    ]
    addon.clear()
    started = time.monotonic()
    with ThreadPoolExecutor(4) as pool:
        answers = list(
            pool.map(lambda song: world.client().request("download", {"id": song}), songs)
        )
    assert all(
        answer.status_code == 200 and answer.content.startswith(b"fLaC") for answer in answers
    )
    assert time.monotonic() - started < 15
    audio = sorted(r["at"] for r in addon.requests("audio"))
    assert len(audio) == 4 and audio[1] - audio[0] < 0.5
    assert audio[2] - audio[0] > 0.15  # first bytes alone never release the transfer slot
    for song in songs:
        info = world.client().ok("getSong", {"id": song})["song"]
        assert info["id"] == song and info["suffix"] == "flac"
    manager = world.services.download_first.bulk
    assert manager is not None
    assert world.server.call(lambda: _active(manager)) == 0


async def _active(manager: BulkAcquisition) -> int:
    return len(manager._active)


def test_same_song_requests_join_and_downloaded_audio_needs_no_add_on(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon("Demo joins")
    world.add_source(addon)
    song, _, audio = world.placeholder_track("bulk-join", [addon], seconds=10, chunk_delay=0.02)
    addon.clear()
    with ThreadPoolExecutor(3) as pool:
        answers = list(
            pool.map(lambda _: world.client().request("download", {"id": song}), range(3))
        )
    assert all(answer.status_code == 200 for answer in answers)
    assert all(len(answer.content) > 1000 for answer in answers)
    assert len(addon.requests("audio")) == 1
    addon.clear()
    again = world.client().request("download", {"id": song})
    assert again.status_code == 200 and again.content.startswith(audio.read_bytes()[:4])
    assert addon.requests() == []


def test_playback_starts_while_a_bulk_body_occupies_the_backend(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon("Demo playback")
    world.add_source(addon)
    slow, _, _ = world.placeholder_track("bulk-slow", [addon], seconds=60, chunk_delay=0.04)
    playing, _, _ = world.placeholder_track("bulk-play", [addon])
    manager = world.services.download_first.bulk
    assert manager is not None
    manager.source_downloads = 1
    addon.clear()
    with ThreadPoolExecutor(2) as pool:
        download = pool.submit(lambda: world.client().request("download", {"id": slow}))
        deadline = time.monotonic() + 3
        while not addon.requests("audio") and time.monotonic() < deadline:
            time.sleep(0.01)
        began = time.monotonic()
        play = world.client().request("stream", {"id": playing})
        assert play.status_code == 200 and play.content.startswith(b"fLaC")
        assert time.monotonic() - began < 1
        assert download.result().status_code == 200
    manager.source_downloads = 2
