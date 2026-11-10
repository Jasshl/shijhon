"""Suite K (continued) - the add-ons' plan.

The add-ons are asked in steps (their order on the Add-ons page). For a song someone waits
for, the add-ons of a step start together: the first whose audio comes plays and the others
are stopped. The next step's add-ons join once the step has had its time, or at once when
every add-on asked has failed or lacked the song; the add-ons asked keep trying. One still
trying past its step's time when another's audio comes counts a timeout if it is also
slower than ``slow_after`` times its usual time (with none known, past its step's time
alone); an add-on that keeps timing out cools down, and one that said it lacks a song is not
asked for it again for a while. With ready checks on, an add-on that says it has the song
ready joins a step still without audio. Downloads ask one add-on at a time, each for its
time for a download. Each start is kept for Diagnostics, with its steps.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from shijhon.delivery.pacing import Limits
from shijhon.delivery.sources import Source
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon

CHECKS = ("stream", "isrc", "availability")
STEP = 1.0  # each step's time


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("plan"),
        budget_seconds=STEP,
        availability_timeout_seconds=0.5,
        cooldown_seconds=30.0,
        cooldown_timeouts=2,
        warm_ahead_depth=0,
    ) as w:
        yield w


def arrange(world: DeliveryWorld, *steps: list[FakeAddon], seconds: float = STEP) -> None:
    """The plan: these steps, each with its add-ons (added now) and ``seconds``."""
    world.clear_sources()
    ids = [[world.add_source(addon) for addon in step] for step in steps]
    world.server.call(lambda: world.services.sources.arrange([(i, seconds) for i in ids]))


def play(world: DeliveryWorld, song: str) -> bytes:
    return world.client().request("stream", {"id": song}).content


def source(world: DeliveryWorld, name: str) -> Source:
    return next(s for s in world.server.call(world.services.sources.enabled) if s.name == name)


def latest(world: DeliveryWorld) -> object:
    return world.services.deliverer.recent[-1]


def test_add_ons_in_one_step_race_and_the_first_audio_plays(world: DeliveryWorld) -> None:
    slow, quick = world.addon("Slow"), world.addon("Quick")
    arrange(world, [slow, quick])
    song, fakes, audio = world.placeholder_track("race", [slow, quick])
    fakes[slow].first_byte_delay = 0.8
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < 0.7
    # Both were asked at once; the slow one was stopped once the quick one's audio came.
    assert slow.requests("stream") and quick.requests("audio")
    start = latest(world)
    assert start.served == "Quick"
    assert any("Slow: stopped" in step for step in start.steps)


def test_a_step_without_audio_in_its_time_leads_to_the_next(world: DeliveryWorld) -> None:
    hanging, next_one = world.addon("Hanging"), world.addon("Next")
    arrange(world, [hanging], [next_one])
    song, fakes, audio = world.placeholder_track("step-time", [hanging, next_one])
    fakes[hanging].first_byte_delay = 6.0
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    took = time.monotonic() - started
    assert STEP <= took < STEP + 1.2
    assert latest(world).served == "Next"


def test_a_step_whose_add_ons_lack_the_song_ends_at_once(world: DeliveryWorld) -> None:
    lacking, other = world.addon("Lacking"), world.addon("Other")
    arrange(world, [lacking], [other])
    song, _, audio = world.placeholder_track("lacks", [other])
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < STEP
    # It said it lacks it: not asked for it again (a new routing, without the link kept).
    asked = len(lacking.requests())
    world.services.deliverer.forget(song)
    assert play(world, song) == audio.read_bytes()
    assert len(lacking.requests()) == asked
    assert any("Lacking: did not have it" in step for step in latest(world).steps)


def test_an_add_on_that_keeps_timing_out_cools_down(world: DeliveryWorld) -> None:
    dead, alive = world.addon("Dead"), world.addon("Alive")
    arrange(world, [dead], [alive])
    for index in range(3):
        song, fakes, audio = world.placeholder_track(f"dead-{index}", [dead, alive])
        fakes[dead].first_byte_delay = 6.0
        before = len(dead.requests("stream"))
        assert play(world, song) == audio.read_bytes()
        asked = len(dead.requests("stream")) > before
        # Two timeouts in a row cool it down: the third song does not wait for it.
        assert asked == (index < 2), index
    assert any("Dead: cooling down" in step for step in latest(world).steps)


def test_a_ready_add_on_joins_a_slow_step(world: DeliveryWorld) -> None:
    slow, ready = world.addon("Slow-First"), world.addon("Ready", resources=CHECKS)
    arrange(world, [slow], [ready], seconds=3.0)
    song, fakes, audio = world.placeholder_track("handover", [slow, ready])
    fakes[slow].first_byte_delay = 6.0
    fakes[ready].ready = True
    deliverer = world.services.deliverer
    before = (deliverer.settings.ready_checks, deliverer.settings.ready_handover_seconds)
    deliverer.settings.ready_checks, deliverer.settings.ready_handover_seconds = True, 0.6
    try:
        started = time.monotonic()
        assert play(world, song) == audio.read_bytes()
        assert 0.6 <= time.monotonic() - started < 1.8
    finally:
        deliverer.settings.ready_checks, deliverer.settings.ready_handover_seconds = before
    start = latest(world)
    assert start.served == "Ready"
    assert any("has it ready" in step for step in start.steps)


def test_ready_first_asks_a_ready_add_on_before_the_plan(world: DeliveryWorld) -> None:
    other = world.addon("Not-Ready", resources=CHECKS)
    ready = world.addon("Ready-2", resources=CHECKS)
    arrange(world, [other], [ready])
    song, fakes, audio = world.placeholder_track("ready-first", [other, ready])
    fakes[other].ready = False
    fakes[ready].ready = True
    deliverer = world.services.deliverer
    before = (deliverer.settings.ready_checks, deliverer.settings.ready_handover_seconds)
    deliverer.settings.ready_checks, deliverer.settings.ready_handover_seconds = True, 0.0
    try:
        assert play(world, song) == audio.read_bytes()
    finally:
        deliverer.settings.ready_checks, deliverer.settings.ready_handover_seconds = before
    assert other.requests("stream") == []
    assert latest(world).served == "Ready-2"


def test_a_song_none_can_play_is_kept_with_its_steps(world: DeliveryWorld) -> None:
    first, second = world.addon("Empty-1"), world.addon("Empty-2")
    arrange(world, [first, second])
    song, _, _ = world.placeholder_track("none", [])
    response = world.client().request("stream", {"id": song})
    assert b"audio unavailable" in response.content
    start = latest(world)
    assert start.served == "" and start.ended
    assert any(step.startswith("Empty-1:") for step in start.steps)
    assert any(step.startswith("Empty-2:") for step in start.steps)


def test_a_download_never_races(world: DeliveryWorld) -> None:
    """A download-first fetch (nobody listens to it) asks one add-on at a time, in the
    plan's order, even where a play would start them together."""
    first, second = world.addon("Download-1"), world.addon("Download-2")
    arrange(world, [first, second])
    song, _, _ = world.placeholder_track("download", [first, second])

    async def fetch() -> str | None:
        return await world.services.download_first.ensure(song, "shijhon-tests")

    assert world.server.call(fetch) is None  # delivered
    assert first.requests("audio") and second.requests("stream") == []
    assert latest(world).purpose == "download"


def test_add_ons_that_all_cool_down_are_asked_all_the_same(world: DeliveryWorld) -> None:
    one, two = world.addon("Cooling-1"), world.addon("Cooling-2")
    arrange(world, [one], [two])
    song, _, audio = world.placeholder_track("all-cooling", [one, two])
    sources = world.server.call(world.services.sources.enabled)
    for source in sources:
        world.services.sources.cool_down(source.id, 30.0)
    try:
        assert play(world, song) == audio.read_bytes()
    finally:
        for source in sources:
            world.services.sources._fresh_start(source.id)
    assert any("asked all the same" in step for step in latest(world).steps)


def test_an_app_s_probe_races_too(world: DeliveryWorld) -> None:
    """A HEAD of a song (an app's probe before it plays) starts a step's add-ons together
    like the play, and the play that follows uses the link that answered."""
    slow, quick = world.addon("Probe-Slow"), world.addon("Probe-Quick")
    arrange(world, [slow, quick])
    song, fakes, audio = world.placeholder_track("probe", [slow, quick])
    fakes[slow].first_byte_delay = 0.8
    started = time.monotonic()
    probe = world.client().request("stream", {"id": song}, http_method="HEAD")
    assert probe.status_code == 200
    assert time.monotonic() - started < 0.7
    assert play(world, song) == audio.read_bytes()
    assert latest(world).served == "Probe-Quick"


def test_an_add_on_asked_keeps_trying_when_the_next_step_joins(world: DeliveryWorld) -> None:
    """The first step's add-on is slower than its step: the next one joins at the step's
    time, and the first one's audio, which comes first all the same, plays."""
    first, joining = world.addon("Keeps-Trying"), world.addon("Joining")
    arrange(world, [first], [joining])
    song, fakes, audio = world.placeholder_track("keeps-trying", [first, joining])
    fakes[first].first_byte_delay = STEP + 0.6
    fakes[joining].first_byte_delay = 2.0
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert STEP + 0.6 <= time.monotonic() - started < STEP + 1.4
    assert joining.requests("stream")  # it had joined
    start = latest(world)
    assert start.served == "Keeps-Trying"
    assert any("joining: Joining" in step for step in start.steps)
    assert any("Joining: stopped after" in step for step in start.steps)


def test_a_step_s_time_is_its_longest_including_the_default(world: DeliveryWorld) -> None:
    """One add-on of the step has its own 0.3 s, the other none (the installation's 1 s):
    the next step joins after 1 s, as the dashboard shows the step."""
    timed, plain, after = world.addon("Own-Time"), world.addon("No-Time"), world.addon("After")
    arrange(world, [timed, plain], [after], seconds=0.3)
    sources = world.services.sources
    world.server.call(lambda: sources.update(source(world, plain.name).id, clear_step_seconds=True))
    song, fakes, audio = world.placeholder_track("longest-step", [timed, plain, after])
    fakes[timed].first_byte_delay = fakes[plain].first_byte_delay = 6.0
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert STEP <= time.monotonic() - started < STEP + 1.2
    assert latest(world).served == "After"


def test_the_next_step_joins_at_once_when_every_add_on_asked_has_failed(
    world: DeliveryWorld,
) -> None:
    failing, other = world.addon("Failing"), world.addon("Other-2")
    arrange(world, [failing], [other], seconds=3.0)
    song, fakes, audio = world.placeholder_track("failed-at-once", [failing, other])
    fakes[failing].stream_status = 502
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert time.monotonic() - started < 1.0  # not its step's 3 s
    assert latest(world).served == "Other-2"


def _usual(world: DeliveryWorld, slow: FakeAddon, fast: FakeAddon, key: str) -> None:
    """Two songs only ``slow`` has, each after 0.7 s: its usual time."""
    for index in range(2):
        song, fakes, audio = world.placeholder_track(f"{key}-{index}", [slow])
        fakes[slow].first_byte_delay = 0.7
        assert play(world, song) == audio.read_bytes()


@pytest.mark.parametrize("own", [None, 1.0])
def test_slow_is_judged_against_the_usual_time(world: DeliveryWorld, own: float | None) -> None:
    """Stopped after about 1 s for the next step's audio: past its step's 0.5 s, but within
    twice its usual 0.7 s - not counted; with its own "slow after" of 1, counted."""
    slow, fast = world.addon(f"Usual-{own}"), world.addon(f"Fast-{own}")
    arrange(world, [slow], [fast], seconds=0.5)
    if own is not None:
        sources = world.services.sources
        world.server.call(lambda: sources.update(source(world, slow.name).id, slow_after=own))
    _usual(world, slow, fast, f"usual-{own}")
    song, fakes, audio = world.placeholder_track(f"judged-{own}", [slow, fast])
    fakes[slow].first_byte_delay = 3.0
    fakes[fast].first_byte_delay = 0.5
    assert play(world, song) == audio.read_bytes()
    assert latest(world).served == fast.name
    counted = source(world, slow.name).stats.timeouts_since_delivery
    assert counted == (0 if own is None else 1)
    stopped = [step for step in latest(world).steps if step.startswith(f"{slow.name}: stopped")]
    assert stopped and ("slow: counted" in stopped[0]) == (own is not None)


def test_a_download_has_the_add_on_s_time_for_a_download(world: DeliveryWorld) -> None:
    """Its step's time (0.5 s) is for plays; a download gives it its own time (3 s)."""
    preparing = world.addon("Download-Time")
    arrange(world, [preparing], seconds=0.5)
    sources = world.services.sources
    world.server.call(lambda: sources.update(source(world, "Download-Time").id, budget_seconds=3.0))
    song, fakes, _ = world.placeholder_track("download-time", [preparing])
    fakes[preparing].resolve_delay = 1.5

    async def fetch() -> str | None:
        return await world.services.download_first.ensure(song, "shijhon-tests")

    assert world.server.call(fetch) is None  # delivered, after 1.5 s of its 3


def first_asked(addon: FakeAddon) -> float:
    return min(r["at"] for r in addon.requests("resolve-isrc") + addon.requests("stream"))


def test_the_next_step_joins_once_the_step_before_has_ended(world: DeliveryWorld) -> None:
    """The first step's add-on still tries; the second step's lacked the song at once: the
    third joins then, not after the second's time."""
    trying, lacking, third = (
        world.addon("Still-Trying"),
        world.addon("Lacks-It"),
        world.addon("Third"),
    )
    arrange(world, [trying], [lacking], [third])
    song, fakes, audio = world.placeholder_track("step-ended", [trying, third])
    fakes[trying].first_byte_delay = 8.0
    started = time.monotonic()
    assert play(world, song) == audio.read_bytes()
    assert first_asked(third) - started < STEP + 0.6  # not 2 x STEP
    assert latest(world).served == "Third"


def test_a_request_on_a_link_the_race_left_follows_the_race(world: DeliveryWorld) -> None:
    """A second request, waiting for the first's routing, takes the first step's link as it
    is published, before its audio; when the second step's audio comes first, it leaves
    that link at once and gets the winner's."""
    slow, quick = world.addon("Published"), world.addon("Wins")
    arrange(world, [slow], [quick])
    song, fakes, audio = world.placeholder_track("left-link", [slow, quick])
    fakes[slow].lookup_delay = 0.4  # the second request waits for its link
    fakes[slow].first_byte_delay = 6.0
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(play, world, song)
        time.sleep(0.1)
        began = time.monotonic()
        second = pool.submit(play, world, song)
        assert second.result() == audio.read_bytes()
        took = time.monotonic() - began
        assert first.result() == audio.read_bytes()
    assert took < STEP + 1.0  # not the slow add-on's time
    assert latest(world).served == "Wins"


@pytest.mark.parametrize(
    ("case", "own", "other", "counted"),
    [
        ("within the default", 1.0, 0.7, False),  # past 2 x its usual 0.3 s, not past 1 s
        ("past the default", 3.0, 1.5, True),
        ("no usual time", 3.0, 1.5, True),
    ],
)
def test_the_last_step_counts_the_default_step_time_as_its_own(
    world: DeliveryWorld, case: str, own: float, other: float, counted: bool
) -> None:
    """One step (Fastest): the last step has no time of its own - its stored 5 s count for
    nothing; the default step time (1 s) is its time for "slow after"."""
    tag = case.replace(" ", "-")
    slow, fast = world.addon(f"Last-{tag}"), world.addon(f"Sibling-{tag}")
    arrange(world, [slow, fast], seconds=5.0)
    if case != "no usual time":
        for index in range(2):  # its usual time: 0.3 s
            history, fakes, audio = world.placeholder_track(f"last-{tag}-{index}", [slow])
            fakes[slow].first_byte_delay = 0.3
            assert play(world, history) == audio.read_bytes()
    song, fakes, audio = world.placeholder_track(f"last-{tag}", [slow, fast])
    fakes[slow].first_byte_delay = own
    fakes[fast].first_byte_delay = other
    assert play(world, song) == audio.read_bytes()
    assert latest(world).served == fast.name
    assert source(world, slow.name).stats.timeouts_since_delivery == int(counted)
    assert any("slow: counted" in step for step in latest(world).steps) == counted


def test_waits_at_its_own_limits_are_not_its_slowness(world: DeliveryWorld) -> None:
    """Its lookup waited at its own request limit nearly all the time it was asked: not
    slow, though stopped past its step's time."""
    held, quick = world.addon("Held-Back"), world.addon("Quick-2")
    arrange(world, [held], [quick], seconds=0.5)
    sources = world.services.sources
    limits = Limits(requests_per_second=0.2, request_burst=1, audio_openings=0)
    world.server.call(lambda: sources.update(source(world, "Held-Back").id, limits=limits))
    song, fakes, audio = world.placeholder_track("held-back", [held, quick])
    fakes[quick].first_byte_delay = 0.5
    assert play(world, song) == audio.read_bytes()
    assert latest(world).served == "Quick-2"
    assert source(world, "Held-Back").stats.timeouts_since_delivery == 0


@pytest.mark.parametrize("fault", ["first byte", "relinked"])
def test_a_failed_link_published_early_is_not_left_as_the_song_s(
    world: DeliveryWorld, fault: str
) -> None:
    """Its audio failed (or its fresh link failed too): while the next step still tries, no
    link of it is the song's for a request to take; nor after the routing."""
    failing, slow = world.addon(f"Dead-Link-{fault}"), world.addon(f"Then-{fault}")
    arrange(world, [failing], [slow], seconds=3.0)
    song, fakes, audio = world.placeholder_track(f"dead-link-{fault}", [failing, slow])
    if fault == "first byte":
        fakes[failing].first_byte_status = 502
    else:
        fakes[failing].expire_after = 0  # every audio request: 410, a fresh link too
    fakes[slow].lookup_delay = 1.5  # the next step joins at once, and takes a while

    async def song_link() -> str | None:
        found = world.services.deliverer.pinned(song)
        return None if found is None else found.source.name

    with ThreadPoolExecutor(1) as pool:
        played = pool.submit(play, world, song)
        time.sleep(0.8)
        assert world.server.call(song_link) is None
        assert played.result() == audio.read_bytes()
    assert world.server.call(song_link) == slow.name


def test_the_race_is_settled_when_apps_stop_waiting(world: DeliveryWorld) -> None:
    """Still trying when apps stop waiting: its timeout counted, its link not the song's."""
    hanging = world.addon("Hangs-Past-Wait")
    arrange(world, [hanging])
    song, fakes, _ = world.placeholder_track("past-wait", [hanging])
    fakes[hanging].first_byte_delay = 6.0
    settings = world.services.deliverer.settings
    saved, settings.max_wait_seconds = settings.max_wait_seconds, 1.5
    try:
        response = world.client().request("stream", {"id": song})
    finally:
        settings.max_wait_seconds = saved
    assert b"audio unavailable" in response.content
    assert world.services.deliverer.pinned(song) is None
    assert source(world, "Hangs-Past-Wait").stats.timeouts_since_delivery == 1


@contextlib.contextmanager
def request_timeout(world: DeliveryWorld, seconds: float) -> Iterator[None]:
    """The add-on request timeout (its clients'), for the add-ons read from now on."""
    sources = world.services.sources
    before = sources.request_timeout

    async def apply(value: float) -> None:
        sources.request_timeout = value
        await sources.invalidate()

    world.server.call(lambda: apply(seconds))
    try:
        yield
    finally:
        world.server.call(lambda: apply(before))


def test_in_a_play_an_add_on_s_request_may_take_until_apps_stop_waiting(
    world: DeliveryWorld,
) -> None:
    """Its first byte comes after the add-on request timeout (1 s): in a play it plays, the
    only add-on, well before apps stop waiting; a download from it still ends at its own
    time (1 s here)."""
    patient = world.addon("Patient")
    arrange(world, [patient])
    with request_timeout(world, 1.0):
        song, fakes, audio = world.placeholder_track("past-request-timeout", [patient])
        fakes[patient].first_byte_delay = 2.0
        started = time.monotonic()
        assert play(world, song) == audio.read_bytes()
        assert 2.0 <= time.monotonic() - started < 4.0
        later, fakes, _ = world.placeholder_track("download-request-timeout", [patient])
        fakes[patient].first_byte_delay = 2.0

        async def fetch() -> str | None:
            return await world.services.download_first.ensure(later, "shijhon-tests")

        started = time.monotonic()
        assert world.server.call(fetch) is not None  # not delivered
        assert time.monotonic() - started < 2.0


def test_after_its_first_bytes_a_stalled_stream_fails_within_the_request_timeout(
    world: DeliveryWorld,
) -> None:
    """Only a song's start may take until apps stop waiting: the add-on's first bytes come
    at once, then it stalls (4 s between its chunks). The stall is noticed within the add-on
    request timeout (1 s), not the play's longer time."""
    stalling = world.addon("Stalls")
    arrange(world, [stalling])
    with request_timeout(world, 1.0):
        song, fakes, _ = world.placeholder_track("stalls", [stalling])
        fakes[stalling].chunk_delay = 4.0
        started = time.monotonic()
        with pytest.raises(httpx.RemoteProtocolError):  # (cut short: the add-on stalled)
            world.client().request("stream", {"id": song})
        assert time.monotonic() - started < 1.0 + 1.5
