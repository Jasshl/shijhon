"""Suite K - manners toward add-ons.

An add-on that answers "too many requests" (HTTP 429) is left alone for the time its
``Retry-After`` names - seconds or an HTTP date, an hour at most, the cooldown when it
names none or nonsense - on API requests, availability checks and audio requests alike;
nothing more is sent to it meanwhile, also by a routing already under way. Its manifest is
read once for requests that first use it together. Every request carries Shijhon's
``User-Agent``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import httpx
import pytest

from shijhon.delivery import pacing
from shijhon.delivery.netpolicy import USER_AGENT
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon

CHECKS = ("stream", "isrc", "availability")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("manners"),
        warm_ahead_depth=0,
        budget_seconds=6.0,
        max_wait_seconds=20.0,
        cooldown_seconds=2.0,  # after a rate limit that names no time
    ) as w:
        yield w


@pytest.fixture
def a_and_b(world: DeliveryWorld) -> Iterator[tuple[FakeAddon, FakeAddon]]:
    """Two fresh add-ons, A before B, as the only sources (each its own origin)."""
    world.clear_sources()
    a, b = world.addon("A", resources=CHECKS), world.addon("B")
    world.add_source(a)
    world.add_source(b)
    yield a, b
    world.clear_sources()
    settings = world.services.deliverer.settings
    settings.routing, settings.primary_source = "ordered", ""


def stream(world: DeliveryWorld, song: str) -> httpx.Response:
    client = world.client(timeout=40.0)
    try:
        return client.request("stream", {"id": song})
    finally:
        client.close()


def left_alone_for(world: DeliveryWorld, addon: FakeAddon) -> float:
    """The seconds the add-on is passed over still (0: it is asked)."""
    sources = world.services.sources
    source = next(s for s in world.server.call(sources.enabled) if s.name == addon.name)
    until = sources.cooling_until(source.id)
    return 0.0 if until is None else until - time.time()


@pytest.mark.parametrize(
    ("named", "seconds"),
    [
        ("7", 7.0),
        ("date:9", 9.0),  # an HTTP date nine seconds from now
        ("0", pacing.MIN_RETRY_AFTER),  # "at once": a moment all the same
        ("date:-600", pacing.MIN_RETRY_AFTER),  # a date that has passed
        (None, 2.0),  # none named: the cooldown
        ("-30", 2.0),  # not valid: the cooldown
        ("in a while", 2.0),
        ("86400", pacing.MAX_RETRY_AFTER),  # a day: an hour at most
    ],
)
def test_a_rate_limited_add_on_is_left_alone_for_the_time_it_names(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], named: str | None, seconds: float
) -> None:
    a, b = a_and_b
    first, fakes, audio = world.placeholder_track(f"m-named-{seconds}-{named}", [a, b])
    fakes[a].rate_limit_streams = 1  # A's link request answers 429
    if named is not None and named.startswith("date:"):
        named = formatdate(time.time() + float(named[5:]), usegmt=True)
    a.retry_after = named
    assert stream(world, first).content == audio.read_bytes()  # the fallback plays it
    assert b.requests("audio")
    left = left_alone_for(world, a)
    assert seconds - 1.5 <= left <= seconds + 0.5
    # Meanwhile nothing is sent to it: the next song goes to the other add-on at once.
    second, _, audio2 = world.placeholder_track(f"m-named-2-{seconds}-{named}", [a, b])
    a.clear()
    if left > 0.5:
        assert stream(world, second).content == audio2.read_bytes()
        assert a.requests() == []


def test_after_its_time_a_rate_limited_add_on_is_asked_again(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    first, fakes, _ = world.placeholder_track("m-again-1", [a, b])
    fakes[a].rate_limit_streams = 1
    a.retry_after = "2"
    assert stream(world, first).status_code == 200
    time.sleep(2.2)
    assert left_alone_for(world, a) == 0.0
    second, _, audio = world.placeholder_track("m-again-2", [a, b])
    a.clear()
    assert stream(world, second).content == audio.read_bytes()
    assert [r["endpoint"] for r in a.requests()] == ["resolve-isrc", "stream", "audio"]


def test_a_rate_limit_on_its_audio_names_its_time_too(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """The audio address answers 429 with Retry-After: honored like an API answer's
    (before: the cooldown, whatever it named)."""
    a, b = a_and_b
    song, fakes, audio = world.placeholder_track("m-audio", [a, b])
    fakes[a].rate_limit_audio = 1
    a.retry_after = "8"
    assert stream(world, song).content == audio.read_bytes()
    assert len(a.requests("audio")) == 1 and b.requests("audio")
    assert 6.0 <= left_alone_for(world, a) <= 8.5
    # Its audio is not asked for either while it is left alone (a link it handed out before).
    other, _, _ = world.placeholder_track("m-audio-2", [a])
    a.clear()
    refused = stream(world, other)
    assert "audio unavailable" in refused.text and a.requests() == []


def test_a_rate_limit_on_its_api_leaves_the_songs_playing_from_it_alone(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """An add-on that says "too many requests" to a lookup is asked for nothing new - but a
    song already playing from it goes on: its later ranges are served from the link it
    has."""
    a, b = a_and_b
    playing, _, audio = world.placeholder_track("m-playing", [a], seconds=6)
    assert stream(world, playing).content == audio.read_bytes()
    limited, fakes, _ = world.placeholder_track("m-playing-limited", [a, b])
    fakes[a].rate_limit_streams = 1
    a.retry_after = "20"
    assert stream(world, limited).status_code == 200  # (the other add-on plays that one)
    assert left_alone_for(world, a) > 15
    a.clear()
    client = world.client()
    seek = client.request("stream", {"id": playing}, headers={"range": "bytes=2000-"})
    assert seek.status_code == 206 and seek.content == audio.read_bytes()[2000:]
    assert [r["endpoint"] for r in a.requests()] == ["audio"]  # its audio, nothing else


def test_a_rate_limit_on_an_availability_check_names_its_time_too(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """Primary-first routing asks the other add-ons whether they have the song ready: a
    429 there leaves that add-on alone (before: ignored, and asked again with every play)."""
    a, b = a_and_b
    settings = world.services.deliverer.settings
    settings.routing, settings.primary_source = "primary_first", "B"
    song, _, audio = world.placeholder_track("m-check", [a, b], ready=True)
    a.rate_limit_availability = 1
    a.retry_after = "6"
    assert stream(world, song).content == audio.read_bytes()  # the primary plays it
    assert [r["endpoint"] for r in a.requests()] == ["manifest", "availability"]
    assert 4.0 <= left_alone_for(world, a) <= 6.5
    assert left_alone_for(world, b) == 0.0
    again, _, audio2 = world.placeholder_track("m-check-2", [a, b], ready=True)
    a.clear()
    assert stream(world, again).content == audio2.read_bytes()
    assert a.requests() == []  # no check while it is left alone


def test_an_add_on_not_asked_is_asked_again_once_its_time_has_passed(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """Not asked is no failure: a song whose only add-on was left alone when it was
    wanted plays at the client's retry once the time has passed - the add-on is not
    skipped as one that failed for the song, and nothing of it is measured."""
    a, _ = a_and_b
    world.server.call(lambda: world.services.sources.set_enabled(source_id(world, "B"), False))
    settings = world.services.deliverer.settings
    settings.routing = "ready_first"
    song, _, audio = world.placeholder_track("m-not-asked", [a], ready=True)
    a.rate_limit_availability = 1
    a.retry_after = "1"
    refused = stream(world, song)
    assert "audio unavailable" in refused.text and pacing.BLOCKED in refused.text
    assert [r["endpoint"] for r in a.requests()] == ["manifest", "availability"]
    stats = world.services.sources.stats(source_id(world, "A"))
    assert stats is not None and len(stats.recent) == 0  # no attempt of its own
    time.sleep(1.3)
    assert stream(world, song).content == audio.read_bytes()


def source_id(world: DeliveryWorld, name: str) -> int:
    stored = world.server.call(world.services.sources.stored)
    return next(s.id for s in stored if s.name == name)


def test_a_preparation_request_s_rate_limit_is_honored_too(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """Ready-first routing may ask an add-on that said "not now" to prepare the song, in
    the background: its 429 leaves the add-on alone like any other answer's (before: the
    answer was dropped, and the next request went out at once)."""
    a, b = a_and_b
    settings = world.services.deliverer.settings
    settings.routing, settings.reliable_source = "ready_first", "B"
    settings.prepare_when_not_ready = True
    try:
        a.rate_limit_prepare, a.retry_after = True, "30"
        song, _, audio = world.placeholder_track("m-prepare", [a, b], ready=False)
        assert stream(world, song).content == audio.read_bytes()  # the other add-on plays it
        deadline = time.monotonic() + 5
        while len(a.requests("availability")) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [r["prepare"] for r in a.requests("availability")] == [False, True]
        deadline = time.monotonic() + 5
        while left_alone_for(world, a) == 0.0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert 25.0 <= left_alone_for(world, a) <= 30.5
        again, _, audio2 = world.placeholder_track("m-prepare-2", [a, b], ready=False)
        a.clear()
        assert stream(world, again).content == audio2.read_bytes()
        assert a.requests() == []  # no check, no preparation request meanwhile
    finally:
        settings.reliable_source, settings.prepare_when_not_ready = "", False


def test_a_routing_under_way_sends_nothing_more_once_the_add_on_says_to_wait(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """The time an add-on named is looked at before every request: a play whose lookup
    was under way when another request was told "too many requests" does not ask for its
    link there any more."""
    a, b = a_and_b
    slow, slow_fakes, slow_audio = world.placeholder_track("m-under-way", [a, b])
    slow_fakes[a].lookup_delay = 1.2
    limited, limited_fakes, _ = world.placeholder_track("m-limited", [a, b])
    limited_fakes[a].rate_limit_streams = 1
    a.retry_after = "10"
    with ThreadPoolExecutor(2) as pool:
        under_way = pool.submit(stream, world, slow)
        time.sleep(0.4)  # its lookup at A is under way
        assert pool.submit(stream, world, limited).result().status_code == 200
        answer = under_way.result()
    assert answer.content == slow_audio.read_bytes()  # from the other add-on
    asked = [(r["endpoint"], r.get("key") or r.get("isrc")) for r in a.requests()]
    assert ("resolve-isrc", slow_fakes[a].isrc) in asked
    assert ("stream", slow_fakes[a].key()) not in asked  # its link was not asked for
    assert [e for e, _ in asked].count("stream") == 1  # only the one that was refused


def test_requests_that_first_use_an_add_on_together_read_its_manifest_once(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    a.manifest_delay = 0.5
    songs = [world.placeholder_track(f"m-first-{n}", [a])[0] for n in range(6)]
    with ThreadPoolExecutor(6) as pool:
        answers = list(pool.map(lambda song: stream(world, song), songs))
    assert all(r.status_code == 200 and r.content[:4] == b"fLaC" for r in answers)
    assert len(a.requests("manifest")) == 1
    assert len(a.requests("resolve-isrc")) == 6


def test_every_request_names_shijhon_its_version_and_its_repository(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    settings = world.services.deliverer.settings
    settings.routing, settings.primary_source = "primary_first", "B"
    song, _, audio = world.placeholder_track("m-agent", [a, b], ready=False)
    assert stream(world, song).content == audio.read_bytes()
    seen = {r["endpoint"]: r["user_agent"] for r in a.requests() + b.requests()}
    assert set(seen) == {"manifest", "availability", "resolve-isrc", "stream", "audio"}
    assert set(seen.values()) == {USER_AGENT}
    assert USER_AGENT.startswith("Shijhon/") and USER_AGENT.endswith(
        "(+https://github.com/Jasshl/shijhon)"
    )
    # An add-on may hand headers over with its link: the audio request alone carries them.
    theirs, fakes, audio = world.placeholder_track("m-agent-theirs", [b])
    fakes[b].stream_extra = {"headers": {"User-Agent": "Their-Player/1.0", "X-Token": "t"}}
    b.clear()
    assert stream(world, theirs).content == audio.read_bytes()
    agents = {r["endpoint"]: r["user_agent"] for r in b.requests()}
    assert agents == {
        "resolve-isrc": USER_AGENT,
        "stream": USER_AGENT,
        "audio": "Their-Player/1.0",
    }
