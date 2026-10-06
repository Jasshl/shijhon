"""Suite K - what one add-on is sent in all.

One add-on - its origin - gets at most so many API requests a second (a few at once, then
a steady rate), whoever asks: every user, their clients' probes and fetches ahead, queued
downloads, Shijhon's own background work and the dashboard. The song being played goes
first; a single play on an idle add-on waits for nothing. A request whose time runs out
while it waits for its turn ends as a timeout that says so, and is not the add-on's
failure. Audio requests to one add-on are opened a few at a time.

The per-user limits are off here: every request reaches the add-on's limits at once (the
bulk readers of the request analysis - a queue pre-loaded, an analyzer's probes, an offline
sync - with nothing else holding them back).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.pacing import OPENINGS, REQUESTS, Limits
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.logs import collected

API = ("manifest", "resolve-isrc", "resolve", "stream", "availability")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("addon-limits"),
        warm_ahead_depth=0,
        user_routings=0,
        user_downloads=0,
        budget_seconds=12.0,
        max_wait_seconds=20.0,
        # (The harness has them off; each test sets what it shows.)
        addon_requests_per_second=2.0,
        addon_request_burst=4,
        addon_audio_openings=4,
    ) as w:
        yield w


def limit(world: DeliveryWorld, rate: float, burst: int, openings: int = 0) -> None:
    """The installation's limits from now on (as the dashboard sets them)."""

    async def apply() -> None:
        world.services.sources.limit(Limits(rate, burst, openings))

    world.server.call(apply)


@pytest.fixture
def addon(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("Source")  # its own port: an origin of its own, nothing sent yet
    world.add_source(addon)
    yield addon
    world.clear_sources()
    limit(world, 2.0, 4, 4)


def api_requests(addon: FakeAddon, since: float = 0.0) -> list[float]:
    return sorted(r["at"] for r in addon.requests() if r["endpoint"] in API and r["at"] >= since)


def known(world: DeliveryWorld, addon: FakeAddon, key: str) -> float:
    """The add-on's manifest is read (a first song played, or found missing); returns when
    the requests that follow begin."""
    (song,) = songs(world, addon, key, 1)
    assert fetch(world, "stream", song).status_code == 200
    return time.monotonic()


def most_within(times: list[float], seconds: float) -> int:
    """The most requests in any stretch of ``seconds``."""
    return max(
        (sum(1 for at in times if start <= at < start + seconds) for start in times), default=0
    )


def fetch(
    world: DeliveryWorld,
    method: str,
    song: str,
    http_method: str = "GET",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    client = world.client(timeout=40.0)
    try:
        return client.request(method, {"id": song}, http_method=http_method, headers=headers)
    finally:
        client.close()


def songs(
    world: DeliveryWorld, addon: FakeAddon, prefix: str, count: int, **track: Any
) -> list[str]:
    return [
        world.placeholder_track(f"{prefix}-{index}", [addon], **track)[0] for index in range(count)
    ]


def test_probes_and_a_preloaded_queue_stay_within_the_add_ons_limit(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """An analyzer's HEAD probes and a client pre-loading its queue, for cold songs, all at
    once: the add-on is asked at its rate, not at theirs - and every one is answered."""
    limit(world, 10.0, 4)
    probed = songs(world, addon, "l-probe", 10)
    queued = songs(world, addon, "l-queue", 10)
    since = known(world, addon, "l-first")
    with ThreadPoolExecutor(20) as pool:
        heads = [pool.submit(fetch, world, "stream", song, "HEAD") for song in probed]
        gets = [pool.submit(fetch, world, "stream", song) for song in queued]
        answers = [f.result() for f in heads + gets]
    assert all(a.status_code == 200 for a in answers)
    assert all(a.content[:4] == b"fLaC" for a in answers[10:])
    sent = api_requests(addon, since)
    assert len(sent) == 40  # a lookup and a link a song
    # A few at once, then the rate: in any second at most the burst and a second's worth.
    assert most_within(api_requests(addon), 1.0) <= 4 + 10 + 3
    assert sent[-1] - sent[0] >= (40 - 4) / 10 - 0.3  # paced over the time the rate takes


def test_an_offline_sync_stays_within_the_add_ons_limits(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """Whole-song downloads, as many as are asked for at once: the add-on's API at its rate,
    and its audio opened two at a time."""
    limit(world, 10.0, 2, openings=2)
    wanted = songs(world, addon, "l-sync", 8, first_byte_delay=0.3)
    since = known(world, addon, "l-sync-first")
    with ThreadPoolExecutor(8) as pool:
        answers = list(pool.map(lambda song: fetch(world, "download", song), wanted))
    assert all(a.status_code == 200 and a.content[:4] == b"fLaC" for a in answers)
    sent = api_requests(addon, since)
    assert len(sent) == 16 and most_within(api_requests(addon), 1.0) <= 2 + 10 + 3
    # Each audio request is being opened until its first byte (0.3 s here): never more than
    # two of them at once.
    opened = sorted(r["at"] for r in addon.requests("audio") if r["at"] >= since)
    assert len(opened) == 8
    assert most_within(opened, 0.28) <= 2
    assert opened[-1] - opened[0] >= 3 * 0.3 - 0.1


def test_the_song_being_played_overtakes_the_requests_waiting(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    limit(world, 4.0, 1)
    backlog = songs(world, addon, "l-backlog", 12)
    (played,) = songs(world, addon, "l-played", 1)
    since = known(world, addon, "l-backlog-first")
    with ThreadPoolExecutor(13) as pool:
        probes = [pool.submit(fetch, world, "stream", song, "HEAD") for song in backlog]
        time.sleep(0.6)  # the probes wait at the limit: 24 requests, 6 s at its rate
        began = time.monotonic()
        answer = pool.submit(fetch, world, "stream", played).result()
        took = time.monotonic() - began
        before = len(api_requests(addon, since))  # asked by the time it played
        assert all(p.result().status_code == 200 for p in probes)
    assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    # Its lookup and its link took the next two turns (a quarter of a second apart), not
    # the queue's end: most of the probes' requests were still to come when it played.
    assert took < 2.0
    assert len(api_requests(addon, since)) == 2 * 13
    assert 2 <= before <= 2 * 13 - 12


def test_a_play_takes_its_songs_request_already_waiting_along(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A song being looked up for something less urgent - a probe here, a warm-ahead or a
    fetch ahead alike - that is then played: its lookup goes first from then on, so the
    play does not wait behind the other background work with it."""
    limit(world, 4.0, 1)
    backlog = songs(world, addon, "l-along", 10)
    since = known(world, addon, "l-along-first")
    with ThreadPoolExecutor(11) as pool:
        probes = []
        for song in backlog:  # in order: the last one's requests are the queue's last
            probes.append(pool.submit(fetch, world, "stream", song, "HEAD"))
            time.sleep(0.02)
        time.sleep(0.4)
        began = time.monotonic()
        answer = pool.submit(fetch, world, "stream", backlog[-1]).result()
        took = time.monotonic() - began
        before = len(api_requests(addon, since))
        assert all(p.result().status_code == 200 for p in probes)
    assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    assert took < 2.0  # (the queue's end was 5 s away)
    assert len(api_requests(addon, since)) == 2 * 10  # looked up once, for both
    assert before <= 2 * 10 - 8


def test_the_song_being_played_is_never_held_back_by_the_audio_openings(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """The cap on audio openings holds back downloads, probes, fetches ahead and warm-ahead,
    never a play or a seek: with the add-on's one opening taken by a slow download, the
    listener's song starts - and seeks - at once."""
    limit(world, 50.0, 10, openings=1)
    (slow,) = songs(world, addon, "l-slow", 1, first_byte_delay=3.0)
    (played,) = songs(world, addon, "l-held", 1, seconds=6)
    (queued,) = songs(world, addon, "l-held-queued", 1)
    since = known(world, addon, "l-held-first")
    with ThreadPoolExecutor(2) as pool:
        opening = pool.submit(fetch, world, "download", slow)  # holds the one opening
        time.sleep(0.6)
        began = time.monotonic()
        assert fetch(world, "stream", played).status_code == 200
        seek = fetch(world, "stream", played, headers={"range": "bytes=1000-"})
        assert seek.status_code == 206 and time.monotonic() - began < 1.5
        # ... while another download waits for the opening to be over.
        waiting = pool.submit(fetch, world, "download", queued)
        assert opening.result().status_code == 200 and waiting.result().status_code == 200
    at = [r["at"] for r in addon.requests("audio") if r["at"] >= since]
    assert len(at) == 4  # the slow download, the play, its seek, the queued download
    assert at[1] - at[0] < 1.5 and at[2] - at[0] < 2.0  # the play and the seek: at once
    assert at[3] - at[0] >= 2.8  # the queued download: once the first one's audio began


def test_an_audio_opening_lasts_until_the_audio_s_first_bytes(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """An audio address may answer at once and send its first bytes much later (a file
    being prepared): the opening is not over at the answer's headers."""
    limit(world, 50.0, 10, openings=1)
    wanted = songs(world, addon, "l-body", 3, body_delay=0.6)
    since = known(world, addon, "l-body-first")
    with ThreadPoolExecutor(3) as pool:
        answers = list(pool.map(lambda song: fetch(world, "download", song), wanted))
    assert all(a.status_code == 200 and a.content[:4] == b"fLaC" for a in answers)
    opened = sorted(r["at"] for r in addon.requests("audio") if r["at"] >= since)
    assert len(opened) == 3
    assert opened[1] - opened[0] >= 0.5 and opened[2] - opened[1] >= 0.5  # one at a time


def test_a_download_that_gets_no_audio_opening_in_its_time_keeps_its_link(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit(world, 50.0, 10, openings=1)
    (slow,) = songs(world, addon, "l-taken", 1, first_byte_delay=3.5)
    (wanted,) = songs(world, addon, "l-late", 1)
    known(world, addon, "l-late-first")
    (source,) = world.server.call(world.services.sources.enabled)
    with ThreadPoolExecutor(1) as pool, collected("shijhon.delivery.playback") as lines:
        opening = pool.submit(fetch, world, "download", slow)  # holds the one opening
        time.sleep(0.6)
        monkeypatch.setattr(world.services.deliverer.settings, "budget_seconds", 1.2)
        began = time.monotonic()
        refused = fetch(world, "download", wanted)
        assert 1.0 <= time.monotonic() - began < 2.5  # its own time, then an answer
        assert "download unavailable" in refused.text and OPENINGS in refused.text
        assert opening.result().status_code == 200
    assert any(OPENINGS in line for line in lines if line.startswith("no audio for"))
    stats = world.services.sources.stats(source.id)
    assert stats is not None and stats.failures == 0  # not the add-on's failure
    assert not world.services.sources.cooling(source.id)
    # Its link was found and is kept: the next request asks for its audio, nothing else.
    since = time.monotonic()
    assert fetch(world, "download", wanted).content[:4] == b"fLaC"
    assert api_requests(addon, since) == []


def test_a_turn_that_came_late_is_not_the_add_ons_timeout(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that waited at the limit and got its turns - but too late for the add-on
    to answer in what was left of its time: not the add-on's failure, nothing toward its
    cooldown as the primary, nothing in the measured order."""
    settings = world.services.deliverer.settings
    monkeypatch.setattr(settings, "routing", "primary_first")
    monkeypatch.setattr(settings, "primary_source", "Source")
    wanted = songs(world, addon, "l-late-turn", 2, first_byte_delay=5.0)
    known(world, addon, "l-late-turn-first")
    (source,) = world.server.call(world.services.sources.enabled)
    recorded = len(world.services.sources.stats(source.id).recent)  # type: ignore[union-attr]
    limit(world, 0.5, 1)  # its lookup at once, its link two seconds later
    monkeypatch.setattr(settings, "budget_seconds", 4.5)
    with collected("shijhon.delivery.playback") as lines:
        for song in wanted:
            answer = fetch(world, "stream", song)
            assert "audio unavailable" in answer.text and REQUESTS in answer.text
    assert len(addon.requests("audio")) >= 3  # its audio was asked for: the turns did come
    stats = world.services.sources.stats(source.id)
    assert stats is not None and stats.failures == 0 and stats.timeouts_since_delivery == 0
    assert not world.services.sources.cooling(source.id)
    assert len(stats.recent) == recorded  # no attempt of the add-on's to measure
    assert sum(1 for line in lines if line.startswith("no audio for")) == 2


def test_a_redirected_request_takes_a_turn_for_every_hop(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """An add-on that redirects its API (to another path, another scheme): each hop is a
    request at its limit, not one turn for the chain."""
    addon.api_redirects = 1
    limit(world, 5.0, 1)
    (song,) = songs(world, addon, "l-redirected", 1)
    answer = fetch(world, "stream", song)
    assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    hops = sorted(r["at"] for r in addon.requests() if r["endpoint"] in (*API, "api-redirect"))
    assert len(hops) == 6  # the manifest, the lookup and the link, each asked for twice
    assert hops[-1] - hops[0] >= (6 - 1) / 5 - 0.2  # one turn each (one a chain: 0.4 s)
    assert most_within(hops, 1.0) <= 1 + 5 + 2
    (source,) = world.server.call(world.services.sources.enabled)
    assert source.pace is not None and source.pace.sent == 6


def test_a_primary_held_up_at_its_own_limit_is_not_counted_as_slow(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Primary-first routing switches to an add-on that has the song ready when the primary
    has no first byte in its short budget - and counts that against the primary, which
    cools down after three. Not when the primary was waiting for its turn at Shijhon's own
    limit: it was never asked."""
    limit(world, 50.0, 10)
    ready = world.addon("Ready", resources=("stream", "isrc", "availability"))
    world.add_source(ready)
    settings = world.services.deliverer.settings
    monkeypatch.setattr(settings, "routing", "primary_first")
    monkeypatch.setattr(settings, "primary_source", "Source")
    monkeypatch.setattr(settings, "primary_budget_seconds", 0.5)
    known(world, addon, "l-switch-first")  # (the primary's manifest is read)
    primary = next(
        s for s in world.server.call(world.services.sources.enabled) if s.name == "Source"
    )
    # The primary's own limit: a request every 20 s (its count goes on: none to be had).
    world.server.call(lambda: world.services.sources.update(primary.id, limits=Limits(0.05, 1, 0)))
    wanted = [
        world.placeholder_track(f"l-switch-{n}", [addon, ready], ready=True)[0] for n in range(4)
    ]
    for song in wanted:
        answer = fetch(world, "stream", song)
        assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    assert len(ready.requests("audio")) == 4  # each switched to the add-on that had it ready
    stats = world.services.sources.stats(primary.id)
    assert stats is not None and stats.switches_since_delivery == 0
    assert stats.failures == 0 and not world.services.sources.cooling(primary.id)


def test_a_lookup_meanwhile_that_gets_no_turn_ends_its_attempt_once(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The likely fallback is looked up while the primary is asked: when that lookup waits
    out its time at the fallback's limit, the fallback's attempt is over - it gets no second
    budget - and it did not fail."""
    limit(world, 50.0, 10)
    fallback = world.addon("Fallback")
    world.add_source(fallback)
    settings = world.services.deliverer.settings
    monkeypatch.setattr(settings, "routing", "primary_first")
    monkeypatch.setattr(settings, "primary_source", "Source")
    monkeypatch.setattr(settings, "reliable_lookup_after_seconds", 0.0)
    first, _, _ = world.placeholder_track("l-meanwhile-first", [addon, fallback])
    assert fetch(world, "stream", first).status_code == 200  # (both manifests are read)
    held = next(
        s for s in world.server.call(world.services.sources.enabled) if s.name == "Fallback"
    )
    world.server.call(lambda: world.services.sources.update(held.id, limits=Limits(0.05, 1, 0)))
    world.server.call(world.services.sources.enabled)
    assert held.pace is not None
    world.server.call(held.pace.request)  # its one request is taken: the next waits 20 s
    monkeypatch.setattr(settings, "budget_seconds", 1.5)
    song, _, _ = world.placeholder_track("l-meanwhile", [addon, fallback], available=False)
    fallback.clear()
    began = time.monotonic()
    answer = fetch(world, "stream", song)
    took = time.monotonic() - began
    assert "audio unavailable" in answer.text and REQUESTS in answer.text
    assert 1.3 <= took < 2.6  # one budget's time, not two
    assert fallback.requests() == []  # it was never asked
    stats = world.services.sources.stats(held.id)
    assert stats is not None and stats.failures == 0
    assert not world.services.sources.cooling(held.id)


def test_a_single_play_on_an_idle_add_on_waits_for_nothing(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """With the built-in limits (2 requests a second, 4 at once): a cold play - the add-on's
    manifest, the lookup, the link, the audio - is not held up anywhere."""
    (song,) = songs(world, addon, "l-idle", 1)
    (source,) = world.server.call(world.services.sources.enabled)
    assert source.pace is not None and source.pace.limits == Limits(2.0, 4, 4)
    began = time.monotonic()
    answer = fetch(world, "stream", song)
    took = time.monotonic() - began
    assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    sent = api_requests(addon)
    assert len(sent) == 3 and took < 2.0
    # None of them waited: a request waits only when less than one is to be had.
    assert source.pace.sent == 3 and source.pace._tokens >= 0.99


def test_a_request_that_gets_no_turn_in_its_time_is_a_timeout_and_not_the_add_ons_failure(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit(world, 0.1, 1)  # one request every ten seconds
    settings = world.services.deliverer.settings
    monkeypatch.setattr(settings, "budget_seconds", 0.8)
    # As the primary: its timeouts would cool it down after two.
    monkeypatch.setattr(settings, "routing", "primary_first")
    monkeypatch.setattr(settings, "primary_source", "Source")
    wanted = songs(world, addon, "l-starved", 3)
    (source,) = world.server.call(world.services.sources.enabled)
    with collected("shijhon.delivery.playback") as lines:
        for song in wanted:
            began = time.monotonic()
            answer = fetch(world, "stream", song)
            assert 0.7 <= time.monotonic() - began < 3.0  # its budget, as a timeout's
            assert answer.status_code == 200 and REQUESTS in answer.text
            assert "audio unavailable" in answer.text
    # The add-on got its manifest request and nothing else: nothing it could have failed at.
    assert [r["endpoint"] for r in addon.requests()] == ["manifest"]
    sources = world.services.sources
    stats = sources.stats(source.id)
    assert stats is not None and stats.failures == 0 and stats.failures_since_success == 0
    assert stats.timeouts_since_delivery == 0 and not sources.cooling(source.id)
    assert len(stats.recent) == 0  # ... and nothing for the measured order of the fallbacks
    said = [line for line in lines if line.startswith("no audio for")]
    assert len(said) == 3 and all(f"Source: {REQUESTS}" in line for line in said)
    # A client's retry is not kept from the add-on (it did not fail for the song).
    limit(world, 50.0, 4)
    monkeypatch.setattr(settings, "budget_seconds", 8.0)
    again = fetch(world, "stream", wanted[0])
    assert again.status_code == 200 and again.content[:4] == b"fLaC"


def test_two_add_ons_at_one_address_share_its_limits(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    limit(world, 10.0, 2)
    # A second entry at the same origin (the same add-on, configured another way).
    world.server.call(
        lambda: world.services.sources.add(
            "Twin", f"{addon.base_url}?variant=2", {}, reach=Reach.LOOPBACK
        )
    )
    sources = world.server.call(world.services.sources.enabled)
    assert [s.name for s in sources] == ["Source", "Twin"]
    assert sources[0].pace is sources[1].pace and sources[0].pace is not None
    # Songs neither has: each is looked up at both entries - all of it at the one rate.
    (first,) = songs(world, addon, "l-shared-first", 1, available=False)
    assert "audio unavailable" in fetch(world, "stream", first).text  # (both manifests read)
    since = time.monotonic()
    missing = songs(world, addon, "l-shared", 6, available=False)
    with ThreadPoolExecutor(6) as pool:
        answers = list(pool.map(lambda song: fetch(world, "stream", song), missing))
    assert all("audio unavailable" in a.text for a in answers)
    sent = api_requests(addon, since)
    assert len(sent) == 2 * 6  # a lookup at each entry a song
    assert most_within(api_requests(addon), 1.0) <= 2 + 10 + 3
    assert sent[-1] - sent[0] >= (12 - 2) / 10 - 0.3
    assert sources[0].pace.sent == 4 + 12


def test_an_add_ons_own_limits_replace_the_installations(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """An add-on of one's own, given more (the dashboard's Add-ons page, [[addons]])."""
    limit(world, 1.0, 1)
    (source,) = world.server.call(world.services.sources.enabled)
    world.server.call(lambda: world.services.sources.update(source.id, limits=Limits(200.0, 50, 0)))
    (source,) = world.server.call(world.services.sources.enabled)
    assert source.pace is not None and source.pace.limits == Limits(200.0, 50, 0)
    wanted = songs(world, addon, "l-own", 6)
    began = time.monotonic()
    with ThreadPoolExecutor(6) as pool:
        answers = list(pool.map(lambda song: fetch(world, "stream", song), wanted))
    assert all(a.status_code == 200 for a in answers)
    assert time.monotonic() - began < 4.0  # 13 requests: 12 s at the installation's rate
    # Its own limits removed again: the installation's.
    world.server.call(lambda: world.services.sources.update(source.id, limits=Limits()))
    (source,) = world.server.call(world.services.sources.enabled)
    assert source.pace is not None and source.pace.limits == Limits(1.0, 1, 0)


def test_a_listening_session_with_every_limit_at_its_built_in_value(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """The limits together, as an installation has them: primary-first routing with an
    availability check at the other add-on, warm-ahead of the next two songs, the per-user
    limits, 2 requests a second per add-on after 4 at once. A listener playing an album -
    and skipping on - hears each song promptly, the next ones are warm, and no add-on is
    asked faster than its limit."""
    with delivery_world(
        navidrome_factory(),
        tmp_path,
        routing="primary_first",
        primary_source="Primary",
        warm_ahead_depth=2,
        warm_ahead_delay_seconds=0.3,
        budget_seconds=9.0,
        max_wait_seconds=30.0,
        addon_requests_per_second=2.0,
        addon_request_burst=4,
        addon_audio_openings=4,
        user_download_burst=4,
    ) as world:
        primary = world.addon("Primary")
        checked = world.addon("Checked", resources=("stream", "isrc", "availability"))
        world.add_source(primary)
        world.add_source(checked)
        release = catalog_release("l-session", "Session Album", "Session Artist", 6)
        created = world.materialize(release).created
        for item in release.tracks:
            assert item.isrc
            for addon in (primary, checked):
                addon.add(FakeTrack(isrc=item.isrc, audio=world.audio(item.title), ready=False))
        album = [created[item.ref] for item in release.tracks]
        client = world.client(client="session", timeout=40.0)
        with collected("shijhon.delivery.playback") as lines:
            for song in album[:4]:  # plays one, skips on after a moment, three times
                answer = client.request("stream", {"id": song})
                assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
                client.ok("scrobble", {"id": song, "submission": "false"})
                time.sleep(1.5)
            deadline = time.monotonic() + 15  # the last play's warm-ahead
            while len(primary.requests("resolve-isrc")) < 6 and time.monotonic() < deadline:
                time.sleep(0.1)
            time.sleep(0.5)  # ... and nothing more
        # Every song of the album was looked up once, by its play or its warm-ahead (the
        # next two of each song played): never twice.
        looked_up = [r["isrc"] for r in primary.requests("resolve-isrc")]
        assert len(looked_up) == len(set(looked_up)) == 6
        for addon, burst in ((primary, 4), (checked, 4)):
            sent = api_requests(addon)
            assert most_within(sent, 1.0) <= burst + 2 + 2, addon.name
            assert most_within(sent, 4.0) <= burst + 2 * 4 + 2, addon.name
        # No play was held up at a limit (whatever the machine's speed: a wait there is in
        # the play's log line), and none went without audio.
        played = [line for line in lines if line.startswith("playing ")]
        assert len(played) == 4 and not any("request limit" in line for line in played)
        assert not any(line.startswith("no audio for") for line in lines)
