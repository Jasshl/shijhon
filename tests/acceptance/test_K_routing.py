"""Suite K (continued) — ready-first routing and warm-ahead.

Ready-first: sources that can tell are asked "can you deliver this now?"; playback uses
the first (in order) that says yes, otherwise goes straight to the designated reliable
source. Nothing is streamed to find out. Sources that cannot tell, or said no, remain
fallbacks. Optionally one source that said no is asked to prepare the track.

Warm-ahead: when a placeholder starts, the next placeholder tracks of the release are
resolved in the background with the same routing - or the next songs of the client's saved
queue; only for the song being played, and not for a client that fetches upcoming songs
itself.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.logs import collected

CHECKS = ("stream", "isrc", "availability")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("routing"),
        budget_seconds=8.0,
        routing="ready_first",
        reliable_source="Reliable",
        availability_timeout_seconds=1.0,
        prepare_when_not_ready=True,
        warm_ahead_depth=2,
        warm_ahead_delay_seconds=0.2,
    ) as w:
        yield w


@pytest.fixture
def sources(world: DeliveryWorld) -> Iterator[dict[str, FakeAddon]]:
    """In order: Checker-A and Checker-B (with the check), Unknown (without), Reliable."""
    world.clear_sources()
    addons = {
        "a": world.addon("Checker-A", resources=CHECKS),
        "b": world.addon("Checker-B", resources=CHECKS),
        "unknown": world.addon("Unknown"),
        "reliable": world.addon("Reliable"),
    }
    for addon in addons.values():
        world.add_source(addon)
    yield addons
    world.clear_sources()


def play(world: DeliveryWorld, song: str, client: str = "shijhon-tests") -> bytes:
    """A play by ``client``: warm-ahead is per client, and several quick plays by one
    client look like a client fetching ahead itself."""
    return world.client(client=client).request("stream", {"id": song}).content


def test_first_source_that_can_deliver_now_is_used(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    song, fakes, audio = world.placeholder_track("r-ready", list(sources.values()))
    fakes[sources["a"]].ready = False
    fakes[sources["b"]].ready = True
    assert play(world, song) == audio.read_bytes()
    assert sources["b"].requests("audio")
    assert not sources["b"].requests("resolve-isrc")  # the check's ID was used
    for name in ("a", "unknown", "reliable"):
        assert sources[name].requests("stream") == [], name  # nothing else was streamed


def test_a_check_s_id_that_is_refused_is_looked_up_and_asked_for_again(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    """The ID a check named is not there (404); the lookup names the same ID, and it is
    asked for again - as before the add-ons' own catalog IDs existed (only those are
    asked for once)."""
    song, fakes, audio = world.placeholder_track("r-again", list(sources.values()))
    fakes[sources["a"]].ready = False
    fakes[sources["b"]].ready = True
    fakes[sources["b"]].gone_streams = 1
    assert play(world, song) == audio.read_bytes()
    assert len(sources["b"].requests("stream")) == 2 and sources["b"].requests("audio")
    assert len(sources["b"].requests("resolve-isrc")) == 1
    for name in ("a", "unknown", "reliable"):
        assert sources[name].requests("stream") == [], name


def test_nobody_ready_goes_straight_to_the_reliable_source(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    song, fakes, audio = world.placeholder_track("r-none", list(sources.values()))
    fakes[sources["a"]].ready = False
    fakes[sources["b"]].ready = False
    assert play(world, song) == audio.read_bytes()
    assert sources["reliable"].requests("audio")
    for name in ("a", "b", "unknown"):
        assert sources[name].requests("stream") == [], name
    # One source that said no is asked to prepare the track for next time.
    time.sleep(0.3)
    prepared = [r for r in sources["a"].requests("availability") if r["prepare"]]
    assert len(prepared) == 1
    assert not [r for r in sources["b"].requests("availability") if r["prepare"]]


def test_sources_that_cannot_tell_are_fallbacks(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    song, fakes, audio = world.placeholder_track(
        "r-fallback", [sources["unknown"], sources["reliable"]]
    )
    fakes[sources["reliable"]].available = False  # the reliable source does not have it
    assert play(world, song) == audio.read_bytes()
    assert sources["unknown"].requests("audio")


def test_a_slow_check_does_not_hold_up_playback(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    song, fakes, audio = world.placeholder_track("r-slowcheck", list(sources.values()))
    fakes[sources["a"]].ready = True
    fakes[sources["a"]].availability_delay = 3.0  # longer than the 1 s check timeout
    fakes[sources["b"]].ready = False
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < 2.5
    assert sources["reliable"].requests("audio")


def test_warm_ahead_resolves_the_next_tracks(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    release = catalog_release("r-warm", "Warm Album", "Warm Artist", 4)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    reliable = sources["reliable"]
    for track in release.tracks:
        assert track.isrc
        reliable.add(FakeTrack(isrc=track.isrc, audio=world.audio(track.title), resolve_delay=0.5))
    play(world, songs[0], "warm-album")
    deadline = time.monotonic() + 10
    while len(reliable.requests("stream")) < 3 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(reliable.requests("stream")) == 3  # the played track and the next two
    reliable.clear()
    started = time.monotonic()
    play(world, songs[1], "warm-album")
    assert time.monotonic() - started < 0.5  # resolved ahead: no resolve delay
    assert reliable.requests("stream") == []


@pytest.fixture(autouse=True)
def fresh_listening(world: DeliveryWorld) -> None:
    """What earlier tests reported playing must not steer this test's warm-ahead."""
    warm = world.services.interceptor.warm
    assert warm is not None
    warm.listening.clear()


@pytest.fixture
def warm_log() -> Iterator[list[str]]:
    with collected("shijhon.delivery.warm") as lines:
        yield lines


def albums(
    world: DeliveryWorld, addon: FakeAddon, *specs: tuple[str, int]
) -> tuple[list[str], dict[str, str]]:
    """Materialized releases whose audio ``addon`` has: song IDs in order, and each song's
    ISRC."""
    songs, isrcs = [], {}
    for key, count in specs:
        release = catalog_release(key, f"Album {key}", "Queue Artist", count)
        result = world.materialize(release)
        for track in release.tracks:
            assert track.isrc
            addon.add(FakeTrack(isrc=track.isrc, audio=world.audio(track.title)))
            songs.append(result.created[track.ref])
            isrcs[result.created[track.ref]] = track.isrc
    return songs, isrcs


def streamed(addon: FakeAddon) -> set[str]:
    return {r["path"].rsplit("/", 1)[1] for r in addon.requests("stream")}


def settle(addon: FakeAddon, count: int, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while len(streamed(addon)) < count and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)  # and nothing more


def test_warm_ahead_follows_the_client_s_saved_queue(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    reliable = sources["reliable"]
    songs, isrcs = albums(world, reliable, ("r-q1", 3), ("r-q2", 3))
    player = world.client(client="queue-player")
    queue = [songs[0], songs[5], songs[2], songs[3]]  # across both albums
    player.ok("savePlayQueue", [*(("id", s) for s in queue), ("current", queue[0])])
    player.ok("scrobble", {"id": queue[0], "submission": "false"})
    player.request("stream", {"id": queue[0]})
    settle(reliable, 3)
    assert streamed(reliable) == {isrcs[s] for s in queue[:3]}  # not the album's next song


def test_a_client_that_fetches_ahead_itself_gets_no_warm_ahead(
    world: DeliveryWorld, sources: dict[str, FakeAddon], warm_log: list[str]
) -> None:
    """Like a client queueing search results: the play and the next songs at once."""
    reliable = sources["reliable"]
    songs, isrcs = albums(world, reliable, ("r-self", 6))
    world.client(client="fetcher").ok("scrobble", {"id": songs[0], "submission": "false"})
    with ThreadPoolExecutor(3) as pool:
        list(
            pool.map(
                lambda s: world.client(client="fetcher").request("stream", {"id": s}), songs[:3]
            )
        )
    deadline = time.monotonic() + 5
    while not warm_log and time.monotonic() < deadline:
        time.sleep(0.05)
    settle(reliable, 3)
    assert streamed(reliable) == {isrcs[s] for s in songs[:3]}  # the client's own only
    assert warm_log == ["warm-ahead: fetcher fetches upcoming songs itself; none from Shijhon"
                        " for 600s"]  # fmt: skip
    # Its next plays get none either, for a while.
    reliable.clear()
    fetcher = world.client(client="fetcher")
    fetcher.ok("scrobble", {"id": songs[3], "submission": "false"})
    fetcher.request("stream", {"id": songs[3]})
    settle(reliable, 1)
    assert streamed(reliable) == {isrcs[songs[3]]}


def test_skipping_to_another_song_is_not_fetching_ahead(
    world: DeliveryWorld, sources: dict[str, FakeAddon], warm_log: list[str]
) -> None:
    reliable = sources["reliable"]
    songs, isrcs = albums(world, reliable, ("r-skip", 4))
    skipper = world.client(client="skipper")
    warm = world.services.interceptor.warm
    assert warm is not None
    warm.delay = 1.5  # the skip's report is in before the first play's warm-ahead looks
    try:
        skipper.ok("scrobble", {"id": songs[0], "submission": "false"})
        skipper.request("stream", {"id": songs[0]})
        time.sleep(0.6)
        skipper.ok("scrobble", {"id": songs[2], "submission": "false"})  # the listener skips
        skipper.request("stream", {"id": songs[2]})
        settle(reliable, 3, seconds=6.0)
    finally:
        warm.delay = 0.2
    # Nothing for the song skipped past; the song played gets its next one.
    assert streamed(reliable) == {isrcs[s] for s in (songs[0], songs[2], songs[3])}
    assert warm_log == []


def test_a_song_fetched_while_another_still_plays_gets_no_warm_ahead(
    world: DeliveryWorld, sources: dict[str, FakeAddon], warm_log: list[str]
) -> None:
    """A gapless client fetches the next song before the current one ends: that fetch is
    not a play, so it does not open the songs after it."""
    reliable = sources["reliable"]
    release = catalog_release("r-gapless", "Gapless", "Queue Artist", 4, seconds=60)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    for track in release.tracks:
        assert track.isrc
        reliable.add(FakeTrack(isrc=track.isrc, audio=world.audio(track.title)))
    player = world.client(client="gapless")
    player.ok("scrobble", {"id": songs[0], "submission": "false"})  # 60 s long
    time.sleep(1.0)
    reliable.clear()
    player.request("stream", {"id": songs[1]})  # fetched ahead, not reported
    settle(reliable, 1)
    assert streamed(reliable) == {release.tracks[1].isrc}
    assert warm_log == []
