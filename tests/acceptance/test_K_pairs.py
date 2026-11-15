"""Suite K (continued) - two songs an app asks for together, under constrained capacity.

A player with two audio elements loads the song it plays and the next one at the same
moment: both are plays (``delivery/ahead.py``). Whichever request comes first, the song
chosen must not start later than it would beside a fetch ahead - with the add-on's API
requests rate-limited, one audio opening at a time, one preparation worker, or one
download-first fetch per user. Its time alone, under the same limit, is the measure: with
its own request first it is not slowed by the other; with the other's first it no longer
waits for the other's first byte, only for what the two share at the add-on (its requests'
turn, its one worker), as before.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon

MARGIN = 0.4  # seconds of scheduling noise

# Each limit: the settings, the request's parameters, the songs' scripted behavior, and
# how the chosen song's time with the other's request first compares with its time
# alone (1: the same; 2: the other's turn at what they share comes first).
LIMITS: dict[str, tuple[dict[str, Any], dict[str, str], dict[str, Any], int]] = {
    "api-requests": ({"addon_requests_per_second": 2.0, "addon_request_burst": 1}, {}, {}, 2),
    "audio-openings": ({"addon_audio_openings": 1}, {}, {"first_byte_delay": 0.8}, 1),
    "one-worker": ({}, {}, {"prepare_seconds": 1.0}, 2),
    "download-first": (
        {"user_downloads": 1},
        {"format": "mp3", "maxBitRate": "64"},
        {"chunk_delay": 0.01},
        1,
    ),
}


@pytest.fixture(params=list(LIMITS))
def world(
    request: pytest.FixtureRequest,
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DeliveryWorld]:
    settings = {"addon_requests_per_second": 0.0, "addon_audio_openings": 0}
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("pairs"),
        ahead_window_seconds=0.5,
        warm_ahead_depth=0,
        budget_seconds=9.0,
        **{**settings, **LIMITS[request.param][0]},
    ) as w:
        w.limit = request.param  # type: ignore[attr-defined]
        yield w


@pytest.fixture
def a(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("A")
    addon.workers = 1  # (only songs that are prepared use it)
    world.add_source(addon)
    yield addon
    world.clear_sources()


def first_audio(client: Any, song: str, params: dict[str, str]) -> float:
    started = time.monotonic()
    answer = client.request(
        "stream", {"id": song, **params}, headers={"range": "bytes=0-"}, stream=True
    )
    try:
        for chunk in answer.iter_bytes():
            if chunk:
                return time.monotonic() - started
        raise AssertionError("no audio")
    finally:
        answer.close()


def chosen_time(world: DeliveryWorld, a: FakeAddon, key: str, order: str) -> float:
    """The chosen song's time to its first audio bytes: alone, or asked for together with
    the next song (``order``: whose request comes first)."""
    _, params, track, _ = LIMITS[world.limit]  # type: ignore[attr-defined]
    time.sleep(0.6)  # past the moment around the songs before
    chosen, _, _ = world.placeholder_track(f"{key}-chosen", [a], seconds=20, **track)
    other, _, _ = world.placeholder_track(f"{key}-next", [a], seconds=20, **track)
    clients = [world.client(client="two-elements") for _ in range(2)]
    for client in clients:  # their connections open, as an app's are
        client.ok("ping")
    try:
        if order == "alone":
            return first_audio(clients[0], chosen, params)
        asked = [(clients[0], chosen), (clients[1], other)]
        if order == "next first":
            asked.reverse()
        with ThreadPoolExecutor(2) as pool:
            futures = {}
            for client, song in asked:
                futures[song] = pool.submit(first_audio, client, song, params)
                time.sleep(0.01)
            futures[other].result()
            return futures[chosen].result()
    finally:
        for client in clients:
            client.close()


def test_the_chosen_song_is_not_slowed_by_the_next_one_asked_for_with_it(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    limit = world.limit  # type: ignore[attr-defined]
    shared = LIMITS[limit][3]
    alone = chosen_time(world, a, f"pair-{limit}-alone", "alone")
    first = chosen_time(world, a, f"pair-{limit}-first", "chosen first")
    second = chosen_time(world, a, f"pair-{limit}-second", "next first")
    assert first <= alone + MARGIN, (limit, alone, first)
    assert second <= shared * alone + MARGIN, (limit, alone, second)
