"""Suite K (continued) - which of a client's starts is the song being played, in the order
the requests came, and fetches ahead held while a play is being opened.

- A start waits for the client's requests that came before it and are still having their
  credentials checked (or their song looked up), at most the moment around a start: the
  played song whose check takes longer is not taken for a fetch ahead of the next song's
  preload that came after it.
- A fetch ahead that the client reports as playing while it waits is a play being opened
  from then on: the client's other fetches ahead wait for its first byte too (or the
  usual bound).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import anyio
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("ahead-order"),
        ahead_window_seconds=0.5,
        warm_ahead_depth=1,  # (reports are kept while warm-ahead is on; one-song releases)
        budget_seconds=9.0,
    ) as w:
        yield w


@pytest.fixture
def a(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("A")
    world.add_source(addon)
    yield addon
    world.clear_sources()


def first_audio(client: Any, song: str) -> float:
    started = time.monotonic()
    answer = client.request("stream", {"id": song}, headers={"range": "bytes=0-"}, stream=True)
    try:
        for chunk in answer.iter_bytes():
            if chunk:
                return time.monotonic() - started
        raise AssertionError("no audio")
    finally:
        answer.close()


def clients(world: DeliveryWorld, count: int, name: str) -> list[Any]:
    found = [world.client(client=name) for _ in range(count)]
    for client in found:  # their connections open, as an app's are
        client.ok("ping")
    return found


def test_a_played_song_whose_check_takes_longer_is_not_a_fetch_ahead_of_a_later_preload(
    world: DeliveryWorld, a: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The played song's request comes 0.2 s before the next song's preload, but checking
    its credentials takes 0.4 s: the preload, checked at once and slow at its add-on (2 s),
    is noted after it - the played song starts in its own time, not after the preload's."""
    time.sleep(0.6)
    played, played_fakes, _ = world.placeholder_track("order-played", [a], seconds=20)
    preload, preload_fakes, _ = world.placeholder_track("order-next", [a], seconds=20)
    played_fakes[a].resolve_delay = 0.3
    preload_fakes[a].resolve_delay = 2.0
    checker = world.app.checker
    check = checker.check

    async def slow_for_the_played_song(call: Any, **kwargs: Any) -> Any:
        if call.get("id") == played:
            await anyio.sleep(0.4)
        return await check(call, **kwargs)

    monkeypatch.setattr(checker, "check", slow_for_the_played_song)
    first, second = clients(world, 2, "check-order")
    with ThreadPoolExecutor(2) as pool:
        current = pool.submit(first_audio, first, played)
        time.sleep(0.2)
        ahead = pool.submit(first_audio, second, preload)
        took = current.result()
        ahead.result()
    assert took < 1.5, took  # its check and its add-on, not the preload's 2 s first


def test_a_fetch_ahead_reported_as_playing_holds_the_others_until_its_audio(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """A plays (its add-on takes 1.5 s); B and C are fetched ahead; the client then reports
    B as playing (its add-on takes 2.5 s). C waits for B's first byte too, not only for A's
    (within the bound of 3 s)."""
    time.sleep(0.6)
    songs = {}
    for name, delay in (("a", 1.5), ("b", 2.5), ("c", 0.0)):
        song, fakes, _ = world.placeholder_track(f"held-{name}", [a], seconds=20)
        fakes[a].resolve_delay = delay
        songs[name] = (song, fakes[a].isrc)
    one, two, three = clients(world, 3, "held-app")
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(first_audio, one, songs["a"][0])]
        time.sleep(0.15)
        futures.append(pool.submit(first_audio, two, songs["b"][0]))
        time.sleep(0.15)
        futures.append(pool.submit(first_audio, three, songs["c"][0]))
        time.sleep(0.1)
        one.ok("scrobble", {"id": songs["b"][0], "submission": "false"})
        for future in futures:
            future.result()
    asked = {r["key"]: r["at"] for r in a.requests("stream")}
    # B's first byte came 2.5 s after it was asked for; C was asked for after that.
    assert asked[songs["c"][1]] >= asked[songs["b"][1]] + 2.4, asked
