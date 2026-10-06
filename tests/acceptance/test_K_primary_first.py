"""Suite K (continued) — primary-first routing, a setting.

The primary source starts at once while the other sources are asked whether they can
deliver the recording now. If the primary has no first byte after the short primary budget
and a source reported the track ready, playback switches to that source. Otherwise the
primary keeps its chance up to the byte-zero budget, and only then the reliable source is
tried: one track is never streamed from two sources at once. Primary timeouts are recorded
as failures; repeated timeouts, or repeated switches away from the primary, cool it down
(both thresholds are settings). A client's whole wait for the first byte is capped. Warm-ahead
uses the same routing. The source actually used is logged when a play starts.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from shijhon.delivery.sources import Source
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.navidrome import ADMIN_USER

CHECKS = ("stream", "isrc", "availability")
BUDGET = 3.0  # byte-zero budget: how long the primary may take without an alternative that is ready
PRIMARY_BUDGET = 1.0  # when a ready source takes over
HANG = 6.0  # a primary that does not answer within the byte-zero budget
WARM_DELAY = 0.2  # warm-ahead starts this long after a play's first byte


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("primary"),
        budget_seconds=BUDGET,
        routing="primary_first",
        primary_source="Primary",
        primary_budget_seconds=PRIMARY_BUDGET,
        reliable_source="Reliable",
        availability_timeout_seconds=0.5,
        cooldown_seconds=30.0,
        warm_ahead_depth=2,
        warm_ahead_delay_seconds=WARM_DELAY,
    ) as w:
        yield w


@pytest.fixture
def sources(world: DeliveryWorld) -> Iterator[dict[str, FakeAddon]]:
    world.clear_sources()
    addons = {
        "primary": world.addon("Primary"),
        "a": world.addon("Checker-A", resources=CHECKS),
        "b": world.addon("Checker-B", resources=CHECKS),
        "reliable": world.addon("Reliable"),
    }
    for addon in addons.values():
        world.add_source(addon)
    yield addons
    world.clear_sources()


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def playback_log() -> Iterator[list[str]]:
    collector = Collect()
    logger = logging.getLogger("shijhon.delivery.playback")
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(collector)
    yield collector.lines
    logger.removeHandler(collector)
    logger.setLevel(previous)


def play(world: DeliveryWorld, song: str, client: str = "shijhon-tests") -> bytes:
    """A play by ``client``: warm-ahead is per client, and several quick plays by one
    client look like a client fetching ahead itself."""
    return world.client(client=client).request("stream", {"id": song}).content


def source(world: DeliveryWorld, name: str) -> Source:
    enabled = world.server.call(lambda: world.services.sources.enabled())
    return next(s for s in enabled if s.name == name)


def first_request(addon: FakeAddon) -> float:
    """When the add-on was first asked for the track (lookup or stream)."""
    return min(r["at"] for r in addon.requests("resolve-isrc") + addon.requests("stream"))


def logged(lines: list[str], prefix: str) -> bool:
    return any(line.startswith(prefix) for line in lines)


def test_a_quick_primary_is_used(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    song, fakes, audio = world.placeholder_track("p-quick", list(sources.values()))
    fakes[sources["b"]].ready = True
    assert play(world, song) == audio.read_bytes()
    assert sources["primary"].requests("audio")
    for name in ("a", "b", "reliable"):
        assert sources[name].requests("stream") == [], name
    assert logged(playback_log, "playing 'Title p-quick Song 1' from Primary")


def test_a_slow_primary_gives_way_to_a_ready_source(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    song, fakes, audio = world.placeholder_track("p-slow", list(sources.values()))
    fakes[sources["primary"]].first_byte_delay = 5.0
    fakes[sources["a"]].ready = False
    fakes[sources["b"]].ready = True
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert PRIMARY_BUDGET <= time.monotonic() - started < PRIMARY_BUDGET + 1.5
    assert sources["b"].requests("audio")
    # The checks ran while the primary was trying; the switch came after the short budget.
    checks = sources["a"].requests("availability") + sources["b"].requests("availability")
    assert checks and all(r["at"] < started + PRIMARY_BUDGET for r in checks)
    assert first_request(sources["b"]) >= started + PRIMARY_BUDGET
    assert sources["reliable"].requests("stream") == []
    assert any(
        line.startswith("primary-first 'Title p-slow Song 1': Primary had no first byte after")
        and "and Checker-B has it ready" in line
        for line in playback_log
    )
    assert logged(playback_log, "playing 'Title p-slow Song 1' from Checker-B")


def test_without_a_ready_source_the_primary_keeps_its_chance(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    song, fakes, audio = world.placeholder_track("p-patient", list(sources.values()))
    # Past the short budget, within the byte-zero budget.
    fakes[sources["primary"]].first_byte_delay = PRIMARY_BUDGET + 1.0
    fakes[sources["a"]].ready = False
    fakes[sources["b"]].ready = None
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started >= PRIMARY_BUDGET + 1.0
    for name in ("a", "b", "reliable"):
        assert sources[name].requests("stream") == [], name
    assert logged(playback_log, "playing 'Title p-patient Song 1' from Primary")
    assert source(world, "Primary").stats.timeouts_since_delivery == 0


def test_a_hanging_primary_falls_back_to_the_reliable_source_after_the_budget(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    song, fakes, audio = world.placeholder_track("p-hanging", list(sources.values()))
    fakes[sources["primary"]].resolve_delay = HANG
    fakes[sources["a"]].ready = False
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert BUDGET <= time.monotonic() - started < BUDGET + 2.0
    assert sources["reliable"].requests("audio")
    # Never two sources at once: the reliable source only after the primary gave up.
    assert first_request(sources["reliable"]) >= started + BUDGET
    assert sources["a"].requests("stream") == [] and sources["b"].requests("stream") == []
    # The timeout is recorded as the primary's failure.
    primary = source(world, "Primary")
    assert primary.stats.last_failure == "timeout"
    assert primary.stats.timeouts_since_delivery == 1
    assert logged(
        playback_log, "primary-first 'Title p-hanging Song 1': Primary: timeout; plan Reliable"
    )
    assert logged(playback_log, "playing 'Title p-hanging Song 1' from Reliable")


def test_a_primary_hanging_at_the_first_byte_times_out_too(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    song, fakes, audio = world.placeholder_track("p-silent", list(sources.values()))
    fakes[sources["primary"]].first_byte_delay = HANG
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert BUDGET <= time.monotonic() - started < BUDGET + 2.0
    assert sources["reliable"].requests("audio")
    primary = source(world, "Primary")
    assert primary.stats.last_failure == "timeout before the first byte"
    assert primary.stats.timeouts_since_delivery == 1


def test_a_primary_with_its_own_budget_gets_it(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A primary that prepares files keeps its chance beyond the global budget."""
    primary = source(world, "Primary")
    world.server.call(lambda: world.services.sources.update(primary.id, budget_seconds=BUDGET + 2))
    song, fakes, audio = world.placeholder_track("p-own", list(sources.values()))
    fakes[sources["primary"]].first_byte_delay = BUDGET + 0.5
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started >= BUDGET + 0.5
    assert sources["reliable"].requests("stream") == []
    assert logged(playback_log, "playing 'Title p-own Song 1' from Primary")


def test_concurrent_requests_share_one_routing(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    song, fakes, audio = world.placeholder_track("p-together", list(sources.values()))
    fakes[sources["primary"]].resolve_delay = PRIMARY_BUDGET + 0.5  # slow to prepare
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: play(world, song), range(2)))
    assert results == [audio.read_bytes()] * 2
    # One resolution at the primary, and nothing asked of the fallbacks.
    assert len(sources["primary"].requests("stream")) == 1
    for name in ("a", "b", "reliable"):
        assert sources[name].requests("stream") == [], name


def test_a_disabled_primary_is_skipped(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    primary = source(world, "Primary")
    world.server.call(lambda: world.services.sources.set_enabled(primary.id, False))
    song, _, audio = world.placeholder_track("p-disabled", list(sources.values()))
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < 1.5
    assert sources["primary"].requests() == []
    assert logged(
        playback_log, "primary-first 'Title p-disabled Song 1': no primary source; plan Reliable"
    )


def test_a_primary_without_the_track_is_passed_at_once(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    song, fakes, audio = world.placeholder_track("p-missing", list(sources.values()))
    fakes[sources["primary"]].available = False
    fakes[sources["a"]].ready = True
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < 1.5
    assert sources["a"].requests("audio")
    assert source(world, "Primary").stats.timeouts_since_delivery == 0


def test_repeated_primary_timeouts_cool_the_primary_down(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    tracks = [world.placeholder_track(f"p-cool-{n}", list(sources.values())) for n in (1, 2, 3)]
    for _, fakes, _ in tracks:
        fakes[sources["primary"]].resolve_delay = HANG
    for song, _, audio in tracks[:2]:
        assert play(world, song) == audio.read_bytes()
    assert logged(playback_log, "primary Primary timed out 2 times without delivering; cooling")
    sources["primary"].clear()
    song, _, audio = tracks[2]
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < 1.5  # no wait for the hanging primary
    assert sources["primary"].requests() == []
    assert logged(
        playback_log,
        "primary-first 'Title p-cool-3 Song 1': Primary is cooling down; plan Reliable",
    )


def test_a_delivery_resets_the_timeout_count(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    hanging, fakes, _ = world.placeholder_track("p-reset-1", list(sources.values()))
    fakes[sources["primary"]].resolve_delay = HANG
    quick, _, _ = world.placeholder_track("p-reset-2", list(sources.values()))
    play(world, hanging)
    assert source(world, "Primary").stats.timeouts_since_delivery == 1
    play(world, quick)
    assert source(world, "Primary").stats.timeouts_since_delivery == 0


def test_repeated_switches_away_from_a_slow_primary_cool_it_down(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Three switches in a row (the default setting) without a delivery in between."""
    assert world.services.deliverer.settings.primary_cooldown_switches == 3
    tracks = [world.placeholder_track(f"p-switch-{n}", list(sources.values())) for n in range(4)]
    for _, fakes, _ in tracks:
        fakes[sources["primary"]].first_byte_delay = 5.0
        fakes[sources["b"]].ready = True
    for song, _, audio in tracks[:2]:
        assert play(world, song) == audio.read_bytes()
    assert source(world, "Primary").stats.switches_since_delivery == 2
    assert not logged(playback_log, "primary Primary was switched away from")
    assert play(world, tracks[2][0]) == tracks[2][2].read_bytes()
    assert logged(
        playback_log, "primary Primary was switched away from 3 times without delivering; cooling"
    )
    sources["primary"].clear()
    song, _, audio = tracks[3]
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < PRIMARY_BUDGET  # straight to the ready source
    assert sources["primary"].requests() == []


def test_a_delivery_resets_the_switch_count(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    slow, fakes, _ = world.placeholder_track("p-sreset-1", list(sources.values()))
    fakes[sources["primary"]].first_byte_delay = 5.0
    fakes[sources["b"]].ready = True
    quick, _, _ = world.placeholder_track("p-sreset-2", list(sources.values()))
    play(world, slow)
    assert source(world, "Primary").stats.switches_since_delivery == 1
    play(world, quick)
    assert source(world, "Primary").stats.switches_since_delivery == 0


def test_the_timeout_threshold_is_a_setting(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    settings = world.services.deliverer.settings
    settings.primary_cooldown_timeouts = 1
    try:
        song, fakes, audio = world.placeholder_track("p-threshold", list(sources.values()))
        fakes[sources["primary"]].resolve_delay = HANG
        assert play(world, song) == audio.read_bytes()
        assert logged(playback_log, "primary Primary timed out 1 times without delivering")
    finally:
        settings.primary_cooldown_timeouts = 2


def test_the_whole_wait_for_the_first_byte_is_capped(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    """The primary's budget plus a reliable source's own budget would exceed the cap: the
    client gets its answer (an error, so it skips) when the cap is reached."""
    reliable = source(world, "Reliable")
    world.server.call(lambda: world.services.sources.update(reliable.id, budget_seconds=20))
    settings = world.services.deliverer.settings
    cap = BUDGET + 1.5
    settings.max_wait_seconds = cap
    try:
        song, fakes, _ = world.placeholder_track("p-cap", list(sources.values()))
        fakes[sources["primary"]].resolve_delay = HANG
        fakes[sources["reliable"]].first_byte_delay = HANG * 2
        started = time.monotonic()
        answer = world.client().request("stream", {"id": song, "f": "json"})
        elapsed = time.monotonic() - started
    finally:
        settings.max_wait_seconds = 30.0
    assert cap - 0.2 <= elapsed < cap + 1.0
    assert answer.json()["subsonic-response"]["status"] == "failed"
    # The reliable source had its turn (what was left of the cap), after the primary.
    assert first_request(sources["reliable"]) >= started + BUDGET


def test_a_primary_timeout_cut_short_by_the_cap_counts(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The cap cutting the primary's own budget short is a
    timeout like any other and counts toward its cooldown."""
    primary = source(world, "Primary")
    world.server.call(lambda: world.services.sources.update(primary.id, budget_seconds=20))
    settings = world.services.deliverer.settings
    settings.max_wait_seconds, settings.primary_cooldown_timeouts = 2.0, 1
    try:
        song, fakes, _ = world.placeholder_track("p-cutshort", list(sources.values()))
        fakes[sources["primary"]].resolve_delay = HANG
        started = time.monotonic()
        answer = world.client().request("stream", {"id": song, "f": "json"})
        elapsed = time.monotonic() - started
    finally:
        settings.max_wait_seconds, settings.primary_cooldown_timeouts = 30.0, 2
    assert answer.json()["subsonic-response"]["status"] == "failed"
    assert 1.8 <= elapsed < 3.0
    assert logged(playback_log, "primary Primary timed out 1 times without delivering")


def test_the_cap_covers_waiting_for_a_warm_ahead_s_routing(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A play that finds a warm-ahead routing its track waits for it only up to the cap."""
    release = catalog_release("p-capwarm", "Cap Warm", "Warm Artist", 2)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    for index, track in enumerate(release.tracks):
        assert track.isrc
        audio = world.audio(track.title)
        hang = {"resolve_delay": HANG * 3} if index == 1 else {}
        for name in ("primary", "reliable"):
            sources[name].add(FakeTrack(isrc=track.isrc, audio=audio, **hang))
    settings = world.services.deliverer.settings
    cap = 1.5
    try:
        play(world, songs[0], "warm-cap")  # starts the warm-ahead of the second track, which hangs
        time.sleep(WARM_DELAY + 0.3)
        settings.max_wait_seconds = cap
        started = time.monotonic()
        answer = world.client(client="warm-cap").request("stream", {"id": songs[1], "f": "json"})
        elapsed = time.monotonic() - started
    finally:
        settings.max_wait_seconds = 30.0
    assert elapsed < cap + 1.0
    assert answer.json()["subsonic-response"]["status"] == "failed"
    # The primary's timeout was the warm-ahead's, not held against it twice.
    assert source(world, "Primary").stats.timeouts_since_delivery <= 1


def wait_for(lines: list[str], prefix: str, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while not logged(lines, prefix) and time.monotonic() < deadline:
        time.sleep(0.1)
    return logged(lines, prefix)


def test_warm_ahead_switches_to_a_ready_source(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    release = catalog_release("p-warm", "Primary Warm", "Warm Artist", 2)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    for index, track in enumerate(release.tracks):
        assert track.isrc
        audio = world.audio(track.title)
        slow = 5.0 if index == 1 else 0.0  # the primary is too slow for the next track
        sources["primary"].add(FakeTrack(isrc=track.isrc, audio=audio, first_byte_delay=slow))
        sources["b"].add(FakeTrack(isrc=track.isrc, audio=audio, ready=True))
    play(world, songs[0], "warm-switch")
    assert wait_for(playback_log, "warmed 'Primary Warm Song 2' from Checker-B")
    # A warm-ahead's switch is no play's wait: it does not count toward the cooldown.
    assert source(world, "Primary").stats.switches_since_delivery == 0
    sources["b"].clear()
    started = time.monotonic()
    play(world, songs[1], "warm-switch")
    assert time.monotonic() - started < 1.0  # pinned ahead at the ready source
    assert sources["b"].requests("stream") == []


def test_warm_ahead_waits_for_the_primary_then_falls_back(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    release = catalog_release("p-warm2", "Patient Warm", "Warm Artist", 3)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    delays = [{}, {"first_byte_delay": PRIMARY_BUDGET + 1.0}, {"resolve_delay": HANG}]
    for track, delay in zip(release.tracks, delays, strict=True):
        assert track.isrc
        audio = world.audio(track.title)
        sources["primary"].add(FakeTrack(isrc=track.isrc, audio=audio, **delay))
        for name in ("a", "reliable"):
            sources[name].add(FakeTrack(isrc=track.isrc, audio=audio, ready=False))
    play(world, songs[0], "warm-patient")
    # Nothing ready: the second track waits for the slow primary; the third falls back to
    # the reliable source only once the primary used up the byte-zero budget.
    assert wait_for(playback_log, "warmed 'Patient Warm Song 2' from Primary")
    assert wait_for(playback_log, "warmed 'Patient Warm Song 3' from Reliable")
    third = release.tracks[2].isrc
    asked = min(r["at"] for r in sources["primary"].requests("resolve-isrc") if r["isrc"] == third)
    assert sources["a"].requests("stream") == []
    # The primary's budget starts just before its lookup; allow for that round trip.
    assert first_request(sources["reliable"]) >= asked + BUDGET - 0.2
    sources["reliable"].clear()
    started = time.monotonic()
    play(world, songs[2], "warm-patient")
    assert time.monotonic() - started < 1.0  # pinned ahead at the reliable source
    assert sources["reliable"].requests("stream") == []


def test_fetches_ahead_go_to_the_primary_one_at_a_time_after_the_played_song(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A client starting the next songs of its queue with the one it plays (four
    within a quarter of a second) - those go to the primary alone, without
    availability checks, one at a time, after the played song has its first byte."""
    gate = world.services.interceptor.ahead
    assert gate is not None
    gate.window = 0.5  # the default (the test harness turns it off for other tests)
    tracks = [world.placeholder_track(f"p-ahead-{n}", list(sources.values())) for n in range(4)]
    for n, (_, fakes, _) in enumerate(tracks):
        fakes[sources["primary"]].resolve_delay = 1.5 if n == 0 else 0.3  # the played song
        fakes[sources["a"]].ready = n > 0  # the played song stays with the slow primary
    try:
        with ThreadPoolExecutor(4) as pool:
            current = pool.submit(play, world, tracks[0][0], "ahead-client")
            time.sleep(0.1)
            ahead = [pool.submit(play, world, song, "ahead-client") for song, _, _ in tracks[1:]]
            assert current.result() == tracks[0][2].read_bytes()
            assert [f.result() for f in ahead] == [audio.read_bytes() for _, _, audio in tracks[1:]]
    finally:
        gate.window = 0.0
    isrcs = [next(iter(fakes.values())).isrc for _, fakes, _ in tracks]
    checked = {r["isrc"] for r in sources["a"].requests("availability")}
    assert checked == {isrcs[0]}  # only the played song was routed with checks
    streams = sorted(sources["primary"].requests("stream"), key=lambda r: r["at"])
    assert [r["key"] for r in streams] == isrcs[:1] + sorted(isrcs[1:], key=lambda i: next(
        r["at"] for r in streams if r["key"] == i))  # fmt: skip
    starts = [r["at"] for r in streams]
    # After the played song's first byte (its primary took 1.5 s), then one at a time.
    assert starts[1] - starts[0] >= 1.4
    assert all(b - a >= 0.28 for a, b in itertools.pairwise(starts[1:]))
    assert sources["reliable"].requests() == [] and sources["b"].requests("stream") == []
    assert sum(line.startswith("fetched ahead 'Title p-ahead-") for line in playback_log) == 3


def test_a_fetch_ahead_the_primary_cannot_serve_is_routed_as_usual(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A fetch ahead the primary does not have, or fails, is not left to fail
    (a client may skip that song later): it is routed as usual - availability checks,
    fallbacks - still in the client's turn. The primary alone serves the common case."""
    gate = world.services.interceptor.ahead
    assert gate is not None
    gate.window = 0.5
    everyone = list(sources.values())
    others = [a for name, a in sources.items() if name != "primary"]
    tracks = [
        world.placeholder_track("p-fb-played", everyone),
        world.placeholder_track("p-fb-missing", others),  # the primary does not have it
        world.placeholder_track("p-fb-served", everyone),
        world.placeholder_track("p-fb-failing", everyone),
    ]
    tracks[3][1][sources["primary"]].expire_after = 0  # its links answer an error
    tracks[3][1][sources["primary"]].expire_status = 500
    for _, fakes, _ in tracks:
        if sources["a"] in fakes:
            fakes[sources["a"]].ready = True
    try:
        with ThreadPoolExecutor(4) as pool:
            played = pool.submit(play, world, tracks[0][0], "fallback-client")
            time.sleep(0.1)
            ahead = [pool.submit(play, world, song, "fallback-client") for song, _, _ in tracks[1:]]
            assert played.result() == tracks[0][2].read_bytes()
            assert [f.result() for f in ahead] == [audio.read_bytes() for _, _, audio in tracks[1:]]
    finally:
        gate.window = 0.0
    isrcs = [next(iter(fakes.values())).isrc for _, fakes, _ in tracks]
    checked = {r["isrc"] for r in sources["a"].requests("availability")}
    assert checked == {isrcs[0], isrcs[1], isrcs[3]}  # not for the song the primary served
    fell_back = [line for line in playback_log if line.startswith("fetch ahead 'Title p-fb-")]
    assert len(fell_back) == 2 and all("routing it as usual" in line for line in fell_back)
    failing = [r for r in sources["primary"].requests("stream") if r["key"] == isrcs[3]]
    assert len(failing) == 1  # the primary is not asked again
    assert any(line.startswith("fetched ahead 'Title p-fb-served") and "from Primary" in line
               for line in playback_log)  # fmt: skip


def test_a_fetch_ahead_while_the_primary_cools_down_is_routed_as_usual(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    gate = world.services.interceptor.ahead
    assert gate is not None
    gate.window = 0.5
    tracks = [world.placeholder_track(f"p-cooling-{n}", list(sources.values())) for n in range(2)]
    for _, fakes, _ in tracks:
        fakes[sources["a"]].ready = True
    primary = source(world, "Primary")
    try:
        with ThreadPoolExecutor(2) as pool:
            played = pool.submit(play, world, tracks[0][0], "cool-client")
            time.sleep(0.1)
            world.services.sources.cool_down(primary.id, 30.0)  # after the played song began
            ahead = pool.submit(play, world, tracks[1][0], "cool-client")
            assert played.result() == tracks[0][2].read_bytes()
            assert ahead.result() == tracks[1][2].read_bytes()
    finally:
        gate.window = 0.0
        world.services.sources.cool_down(primary.id, 0.0)
    isrc = next(iter(tracks[1][1].values())).isrc
    assert isrc in {r["isrc"] for r in sources["a"].requests("availability")}  # checks allowed
    assert not [r for r in sources["primary"].requests("stream") if r["key"] == isrc]
    assert any(line.startswith("primary-first 'Title p-cooling-1") and "cooling down" in line
               for line in playback_log)  # fmt: skip


def test_during_a_primary_outage_the_next_song_is_prepared_first(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """With the primary down, each fetch ahead is routed as usual
    and takes its time, one at a time - so the turns go in the order of playing: the
    song after the current one in the client's saved queue first, whenever its request
    came, then the others as they came."""
    gate = world.services.interceptor.ahead
    assert gate is not None
    gate.window = 3.0  # (a wide one: the fetches below come one by one, each seen waiting)
    tracks = [world.placeholder_track(f"p-outage-{n}", list(sources.values())) for n in range(4)]
    for _, fakes, _ in tracks:
        fakes[sources["a"]].ready = True
        fakes[sources["a"]].resolve_delay = 0.4  # a routing takes a while
        # The reliable source, looked up meanwhile, lacks them: the checking source's
        # answer, not which of the two answers first, decides where each song comes from.
        fakes[sources["reliable"]].available = False
    isrcs = [next(iter(fakes.values())).isrc for _, fakes, _ in tracks]

    async def keys() -> list[str]:
        found = []
        for song, _, _ in tracks:
            row = await world.services.store.fetchone(
                "SELECT track_ref FROM placeholders WHERE song_id = ?", [song]
            )
            assert row is not None
            found.append(str(row["track_ref"]))
        return found

    primary = source(world, "Primary")
    world.services.sources.cool_down(primary.id, 30.0)
    queue = world.server.call(keys)
    gate.listening.saved_queue(ADMIN_USER, queue, 0)
    who = (ADMIN_USER, "outage-client")
    # What is tested is the order of the turns, not how fast the test's requests come: a
    # client known to fetch ahead itself (no warm-ahead of its next song, which would
    # otherwise start when its first fetch ahead comes later than the warm-ahead delay), the
    # checking source's answers awaited (on a slow machine they may take longer than the
    # module's short timeout), and the fetches ahead held at their wait for the client's
    # report until all three are seen waiting, then given the usual moment.
    gate.listening.mark_prefetching(who)
    settings = world.services.deliverer.settings
    timeout, report = settings.availability_timeout_seconds, gate.report
    settings.availability_timeout_seconds = 5.0

    def waiting() -> list[str]:
        return [fetch.key for fetch in gate._waiting.get(who, [])]

    def until(done: Any) -> None:
        end = time.monotonic() + 10
        while not done():
            assert time.monotonic() < end
            time.sleep(0.005)

    try:
        with ThreadPoolExecutor(4) as pool:
            played = pool.submit(play, world, tracks[0][0], "outage-client")
            until(lambda: sources["a"].requests("stream"))  # the played song is being routed
            gate.report = gate.wait  # (held: released below)
            ahead = []
            for count, n in enumerate((3, 2, 1), start=1):  # the next song's request last
                ahead.append(pool.submit(play, world, tracks[n][0], "outage-client"))
                until(lambda count=count: len(waiting()) == count)  # each seen waiting
            assert waiting() == [queue[3], queue[2], queue[1]]
            gate.report = report
            assert played.result() == tracks[0][2].read_bytes()
            assert [f.result() for f in ahead] == [tracks[n][2].read_bytes() for n in (3, 2, 1)]
    finally:
        gate.window, gate.report = 0.0, report
        settings.availability_timeout_seconds = timeout
        gate.listening.clear()
        world.services.sources.cool_down(primary.id, 0.0)
    served = sorted(sources["a"].requests("stream"), key=lambda r: r["at"])
    assert [r["key"] for r in served] == [isrcs[0], isrcs[1], isrcs[3], isrcs[2]]
    starts = [r["at"] for r in served]
    assert all(b - a >= 0.35 for a, b in itertools.pairwise(starts))  # one at a time
    assert sources["primary"].requests("stream") == []  # cooling down: not asked
