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
from http.client import HTTPConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

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


def chosen_time(world: DeliveryWorld, a: FakeAddon, key: str, order: str) -> float:
    """The chosen song's time to its first audio bytes: alone, or asked for together with
    the next song (``order``: whose request comes first)."""
    _, params, track, _ = LIMITS[world.limit]  # type: ignore[attr-defined]
    time.sleep(0.6)  # past the moment around the songs before
    chosen, _, _ = world.placeholder_track(f"{key}-chosen", [a], seconds=20, **track)
    other, _, _ = world.placeholder_track(f"{key}-next", [a], seconds=20, **track)
    client = world.client(client="two-elements")
    address = urlsplit(client.base_url)
    # Their connections open, as an app's are. The requests are sent from one thread, the
    # second 0.01 s after the first (so that the first comes first): two threads' requests
    # can come apart by more than "together" (0.05 s) on a busy machine.
    connections = [HTTPConnection(address.netloc, timeout=60) for _ in range(2)]
    for connection in connections:
        connection.connect()

    def send(connection: HTTPConnection, song: str) -> float:
        items = [*client.auth_params(), ("f", "json"), ("id", song), *params.items()]
        started = time.monotonic()
        connection.request("GET", f"/rest/stream?{urlencode(items)}", headers={"range": "bytes=0-"})
        return started

    def first_audio(connection: HTTPConnection, started: float) -> float:
        answer = connection.getresponse()
        assert answer.status in (200, 206) and answer.read(1), answer.status
        return time.monotonic() - started

    try:
        asked = [(connections[0], chosen), (connections[1], other)]
        if order == "alone":
            asked = asked[:1]
        elif order == "next first":
            asked.reverse()
        sent = []
        for connection, song in asked:
            sent.append((connection, song, send(connection, song)))
            time.sleep(0.01)
        with ThreadPoolExecutor(2) as pool:
            took = {song: pool.submit(first_audio, c, started) for c, song, started in sent}
            if other in took:
                took[other].result()
            return took[chosen].result()
    finally:
        for connection in connections:
            connection.close()
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
