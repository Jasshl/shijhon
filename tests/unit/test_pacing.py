"""What one add-on is sent in all: requests a second with a
few at once, audio openings at once, the song being played first."""

from __future__ import annotations

import time
from email.utils import formatdate
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from shijhon.delivery import pacing
from shijhon.delivery.pacing import AddonPace, Blocked, Limits, Paces, Urgency, origin, retry_after


def test_an_origin_is_scheme_host_and_port() -> None:
    assert origin("https://Addon.Example.invalid/a/b?key=1") == (
        "https",
        "addon.example.invalid",
        443,
    )
    assert origin("http://addon.example.invalid:8080/manifest.json") == (
        "http",
        "addon.example.invalid",
        8080,
    )
    assert origin("https://addon.example.invalid:443/x") == origin("https://addon.example.invalid")
    assert origin("http://addon.example.invalid") != origin("https://addon.example.invalid")
    assert origin("") is None and origin(None) is None and origin("no address") is None


def test_own_limits_go_over_the_installations_and_the_stricter_of_two_apply() -> None:
    assert Limits().over(pacing.DEFAULTS) == Limits(2.0, 4, 4)
    assert Limits(10.0, None, 0).over(pacing.DEFAULTS) == Limits(10.0, 4, 0)
    assert Limits(10.0, 8, 0).stricter(Limits(2.0, 4, 4)) == Limits(2.0, 4, 4)
    assert Limits(0, 8, 6).stricter(Limits(0, 20, 0)) == Limits(0, 8, 6)  # 0: no limit


@pytest.mark.anyio
async def test_an_idle_add_on_is_asked_at_once_up_to_the_burst() -> None:
    pace = AddonPace(Limits(2.0, 4, 4))
    with pacing.watched() as waits:
        began = time.monotonic()
        for _ in range(4):  # a play: its manifest, its lookups, its link
            await pace.request()
        assert time.monotonic() - began < 0.05
    assert waits.seconds < 0.05 and waits.at is None and pace.sent == 4


@pytest.mark.anyio
async def test_past_the_burst_requests_go_at_the_rate() -> None:
    pace = AddonPace(Limits(50.0, 2, 0))
    began = time.monotonic()
    sent: list[float] = []

    async def ask() -> None:
        await pace.request()
        sent.append(time.monotonic() - began)

    async with anyio.create_task_group() as tg:
        for _ in range(12):
            tg.start_soon(ask)
    # 2 at once, then one every 20 ms: the twelfth not before 200 ms, whoever asks.
    assert len(sent) == 12 and sent[-1] >= 0.19
    for index in range(len(sent)):  # never more than the burst and the rate in any stretch
        within = [at for at in sent if sent[index] <= at < sent[index] + 0.1]
        assert len(within) <= 2 + 50 * 0.1 + 1


async def queued(pace: AddonPace, arrivals: list[tuple[str, Urgency | int]]) -> list[str]:
    """The order in which requests that all wait for the add-on's one request get their
    turns: they arrive in the order given, then the tap is opened."""
    order: list[str] = []

    async def ask(name: str, urgency: Urgency | int) -> None:
        with pacing.urgent(urgency):
            await pace.request()
        order.append(name)

    await pace.request()  # the one request to be had: everyone after it waits
    async with anyio.create_task_group() as tg:
        for name, urgency in arrivals:
            tg.start_soon(ask, name, urgency)
            await anyio.lowlevel.checkpoint()  # (each is in line before the next arrives)
            await anyio.lowlevel.checkpoint()
        assert order == [] and len(pace._requests) == len(arrivals)
        for _, urgency in arrivals:
            if isinstance(urgency, Urgency) and urgency.level == pacing.WARM:
                urgency.raise_to(pacing.PLAY)  # the listener skipped to its song
                urgency.raise_to(pacing.WARM)  # (never less urgent again)
        pace.configure(Limits(500.0, 1, 0))  # one every 2 ms from here
    return order


@pytest.mark.anyio
async def test_the_song_being_played_goes_before_everything_waiting() -> None:
    order = await queued(
        AddonPace(Limits(0.001, 1, 0)),
        [
            ("warm 0", pacing.WARM),
            ("warm 1", pacing.WARM),
            ("check", pacing.CHECK),
            ("ahead 0", pacing.QUEUED),
            ("warm 2", pacing.WARM),
            ("ahead 1", pacing.QUEUED),
            ("play", pacing.PLAY),  # the last to come
        ],
    )
    assert order == ["play", "ahead 0", "ahead 1", "warm 0", "warm 1", "warm 2", "check"]


@pytest.mark.anyio
async def test_a_waiting_request_goes_first_once_its_song_is_played() -> None:
    warming = Urgency(pacing.WARM)
    order = await queued(
        AddonPace(Limits(0.001, 1, 0)),
        [
            ("ahead", pacing.QUEUED),
            ("other warm-ahead", pacing.WARM + 0),
            ("the next song's warm-ahead", warming),
        ],
    )
    assert order == ["the next song's warm-ahead", "ahead", "other warm-ahead"]
    assert warming.level == pacing.PLAY


@pytest.mark.anyio
async def test_a_wait_cut_short_by_the_requests_own_time_says_where_it_waited() -> None:
    pace = AddonPace(Limits(0.5, 1, 0))
    await pace.request()
    # (An attempt, watched around its lookup's own watch.)
    with pacing.watched() as outer, pacing.watched() as waits, anyio.move_on_after(0.05):
        await pace.request()
    assert waits.at == pacing.REQUESTS  # the routing's reason for its timeout
    assert outer.at == pacing.REQUESTS and outer.held() == pacing.REQUESTS
    assert pace.sent == 1 and not len(pace._requests)  # it left the queue, and took nothing
    # ... and the next in line is not held up by the one that left.
    quick = AddonPace(Limits(5.0, 1, 0))
    await quick.request()
    async with anyio.create_task_group() as tg:
        with anyio.move_on_after(0.02):
            await quick.request()
        tg.start_soon(quick.request)
    assert quick.sent == 2


@pytest.mark.anyio
async def test_a_turn_that_came_late_is_told_from_an_add_on_that_was_slow() -> None:
    """What a request waited at the limits is kept: when its time then runs out at the
    add-on, the add-on never had that time (more than a moment of it)."""
    pace = AddonPace(Limits(10.0, 1, 0))
    await pace.request()
    with pacing.watched() as attempt:
        with pacing.watched() as lookup:
            await pace.request()  # waits about 0.1 s for its turn
        assert lookup.at is None and 0.05 <= lookup.seconds < 1.0
        with pacing.watched() as link:
            await pace.request()
        assert attempt.seconds == pytest.approx(lookup.seconds + link.seconds)
    assert attempt.at is None and attempt.held(1.0) is None  # a moment only: the add-on's time
    assert attempt.held(0.05) == pacing.REQUESTS  # more than that: not the add-on's
    assert pacing.Waits().held() is None


@pytest.mark.anyio
async def test_audio_openings_at_once_are_capped_and_never_hold_the_song_being_played() -> None:
    pace = AddonPace(Limits(0, 1, 2))
    opening, most = 0, 0
    order: list[str] = []
    release = anyio.Event()

    async def open_audio(name: str, level: int, hold: anyio.Event | None = None) -> None:
        nonlocal opening, most
        with pacing.urgent(level):
            async with pace.opening():
                order.append(name)
                opening += 1
                most = max(most, opening)
                if hold is not None:
                    await hold.wait()
                opening -= 1

    async with anyio.create_task_group() as tg:
        tg.start_soon(open_audio, "a", pacing.WARM, release)
        tg.start_soon(open_audio, "b", pacing.QUEUED, release)
        await anyio.sleep(0.01)
        tg.start_soon(open_audio, "warm", pacing.WARM)
        tg.start_soon(open_audio, "check", pacing.CHECK)
        tg.start_soon(open_audio, "download", pacing.QUEUED)
        await anyio.sleep(0.01)
        assert order == ["a", "b"]  # two at once: the others wait
        # The song being played - a play, a seek - is asked for at once, whatever waits.
        tg.start_soon(open_audio, "play", pacing.PLAY, release)
        await anyio.sleep(0.01)
        assert order == ["a", "b", "play"] and pace._opening == 3  # ... and counts
        release.set()
    assert order == ["a", "b", "play", "download", "warm", "check"]
    assert pace.idle


@pytest.mark.anyio
async def test_an_opening_lasts_until_it_is_told_over_once() -> None:
    pace = AddonPace(Limits(0, 1, 1))
    with pacing.urgent(pacing.QUEUED):
        over = await pace.open()
        assert pace._opening == 1
        waiting: list[str] = []

        async def next_one() -> None:
            async with pace.opening():
                waiting.append("opened")

        async with anyio.create_task_group() as tg:
            tg.start_soon(next_one)
            await anyio.sleep(0.01)
            assert waiting == []  # the answer's first bytes are not there yet
            over()
            over()  # (its body began, then it was closed: once)
    assert waiting == ["opened"] and pace.idle and pace._opening == 0


@pytest.mark.anyio
async def test_an_opening_whose_time_runs_out_in_the_queue_says_so_and_holds_nothing() -> None:
    pace = AddonPace(Limits(0, 1, 1))
    with pacing.urgent(pacing.QUEUED):
        async with pace.opening():
            with pacing.watched() as waits, anyio.move_on_after(0.02):
                async with pace.opening():
                    raise AssertionError("no second opening at once")
            assert waits.at == pacing.OPENINGS
        async with pace.opening():  # free again
            pass
    assert pace.idle


@pytest.mark.anyio
async def test_no_limit_asks_at_once_whatever_waits() -> None:
    pace = AddonPace(Limits(0, 1, 0))
    for _ in range(50):
        await pace.request()
    with pacing.urgent(pacing.WARM):
        async with pace.opening(), pace.opening(), pace.opening():
            pass
    assert pace.sent == 50 and pace.idle


@pytest.mark.anyio
async def test_changed_limits_reach_the_requests_already_waiting() -> None:
    pace = AddonPace(Limits(0.2, 1, 0))  # one request every five seconds
    await pace.request()
    done = anyio.Event()

    async def ask() -> None:
        await pace.request()
        done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(ask)
        await anyio.sleep(0.02)
        assert not done.is_set()
        pace.configure(Limits(100.0, 4, 0))
        with anyio.fail_after(1):
            await done.wait()


@pytest.mark.anyio
async def test_add_ons_at_one_origin_share_their_limits_and_the_stricter_apply() -> None:
    paces = Paces(Limits(2.0, 4, 4))
    paces.apply(
        [
            ("https://addon.example.invalid/one/manifest.json", Limits(10.0, 8, None)),
            ("https://addon.example.invalid/two?key=x", Limits()),
            ("https://own.example.invalid/x", Limits(50.0, 20, 0)),
        ]
    )
    one = paces.of("https://addon.example.invalid/one")
    two = paces.of("https://addon.example.invalid:443/two")
    own = paces.of("https://own.example.invalid/x")
    other = paces.of("https://elsewhere.example.invalid/")  # no add-on of the list: the default
    assert one is two and one is not own and one is not None and own is not None
    assert other is not None and paces.of("not an address") is None
    assert one.limits == Limits(2.0, 4, 4)  # the stricter of the two entries
    assert own.limits == Limits(50.0, 20, 0)
    assert other.limits == Limits(2.0, 4, 4)
    # The installation's limits changed (the dashboard): at once, for those without their own.
    paces.defaults = Limits(5.0, 6, 2)
    paces.apply([("https://own.example.invalid/x", Limits(50.0, 20, 0))])
    assert own.limits == Limits(50.0, 20, 0)
    assert one.limits == Limits(5.0, 6, 2) and other.limits == Limits(5.0, 6, 2)
    assert paces.of("https://addon.example.invalid/three") is one  # kept: its count goes on


def test_retry_after_is_seconds_or_an_http_date_within_bounds() -> None:
    now = 1_800_000_000.0  # January 2027
    assert retry_after("120") == 120.0 and retry_after(" 7 ") == 7.0
    assert retry_after("0") == 0.0 and retry_after("2.5") == 2.5  # (a fraction: taken too)
    assert retry_after("00000000000000000120") == 120.0  # digits, however many
    # An HTTP date, in each of its three forms: the seconds until then; one that has
    # passed: now.
    assert retry_after(formatdate(now + 90, usegmt=True), now=now) == pytest.approx(90)
    assert retry_after(formatdate(now - 500, usegmt=True), now=now) == 0.0
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=now) == 0.0
    assert retry_after("Sun, 06 Nov 1994 08:49:37 GMT", now=784111777.0 - 30) == pytest.approx(30)
    assert retry_after("Sunday, 06-Nov-94 08:49:37 GMT", now=784111777.0 - 30) == pytest.approx(30)
    assert retry_after("Sun Nov  6 08:49:37 1994", now=784111777.0 - 30) == pytest.approx(30)
    # A two-digit year is of this century unless that is more than 50 years ahead.
    assert retry_after("Friday, 01-Jan-27 00:10:00 GMT", now=1798761600.0) == pytest.approx(600)
    assert retry_after("Tuesday, 01-Jan-69 00:00:00 GMT", now=now) == pacing.MAX_RETRY_AFTER
    assert retry_after("Sunday, 06-Nov-94 08:49:37 GMT", now=now) == 0.0  # 1994, not 2094
    # ... by the moment, not the year: 50 years and a month ahead is the last century's.
    october_2026 = 1790812800.0
    assert retry_after("Saturday, 06-Nov-76 08:49:37 GMT", now=october_2026) == 0.0
    assert retry_after("Tuesday, 01-Sep-76 00:00:00 GMT", now=october_2026) == (
        pacing.MAX_RETRY_AFTER
    )
    # Never more than an hour, whatever it says.
    assert retry_after("86400") == pacing.MAX_RETRY_AFTER
    assert retry_after("999999999999") == pacing.MAX_RETRY_AFTER
    assert retry_after("9" * 5000) == pacing.MAX_RETRY_AFTER
    assert retry_after(formatdate(now + 10**7, usegmt=True), now=now) == pacing.MAX_RETRY_AFTER
    # Not valid: no time named (the cooldown applies).
    for bad in (None, "", "  ", "-5", "-0.1", "soon", "1e9", "inf", "nan", "0x10", "12 seconds",
                "Wed, 99 Foo 2015", "+5", "1,5", "٣٠", "1.", ".5"):  # fmt: skip
        assert retry_after(bad, now=now) is None, bad


@pytest.mark.anyio
async def test_a_rate_limited_add_on_is_left_alone_until_its_time() -> None:
    now = [100.0]
    pace = AddonPace(Limits(1000.0, 10, 2), clock=lambda: now[0])
    await pace.request()
    assert pace.block(30) == pytest.approx(30) and pace.blocked == pytest.approx(30)
    with pytest.raises(Blocked) as refused:
        await pace.request()
    assert refused.value.seconds == pytest.approx(30) and str(refused.value) == pacing.BLOCKED
    # Its API said so: the audio of the songs playing from it goes on.
    with pacing.urgent(pacing.QUEUED):
        async with pace.opening():
            pass
    assert pace.audio_blocked == 0
    # Its audio said so: no audio request either (and no API request: no new links).
    assert pace.block(30, audio=True) == pytest.approx(30)
    for level in (pacing.PLAY, pacing.QUEUED):
        with pytest.raises(Blocked), pacing.urgent(level):
            async with pace.opening():
                raise AssertionError("no audio request while it is left alone")
    assert pace.sent == 1 and not pace.idle
    # A later answer makes the time longer, never shorter.
    now[0] += 10
    assert pace.block(5) == pytest.approx(20)
    assert pace.block(60) == pytest.approx(60)
    # At least a moment, an hour at most.
    other = AddonPace(Limits(0, 1, 0), clock=lambda: now[0])
    assert other.block(0) == pytest.approx(pacing.MIN_RETRY_AFTER)
    assert other.block(10**9) == pytest.approx(pacing.MAX_RETRY_AFTER)
    # An answer that names a time, and one that names none: the cooldown (a setting).
    third = AddonPace(Limits(0, 1, 0), clock=lambda: now[0])
    third.cooldown = 45.0
    assert third.limited(None) == pytest.approx(45) and third.limited(7.0) == pytest.approx(45)
    assert third.limited(90.0) == pytest.approx(90)
    paces = Paces(Limits(2.0, 4, 4), cooldown=12.0)
    made = paces.of("https://addon.example.invalid/")
    assert made is not None and made.cooldown == 12.0
    now[0] += 60.1
    await pace.request()  # its time has passed
    async with pace.opening():
        pass
    assert pace.sent == 2 and pace.blocked == 0 and pace.audio_blocked == 0


@pytest.mark.anyio
async def test_requests_waiting_for_a_turn_are_told_when_the_add_on_says_to_wait() -> None:
    pace = AddonPace(Limits(0.1, 1, 1))  # a request every ten seconds, one opening at once
    await pace.request()
    outcomes: list[str] = []

    async def ask(name: str) -> None:
        try:
            await pace.request()
            outcomes.append(f"{name} sent")
        except Blocked:
            outcomes.append(f"{name} blocked")

    async def open_audio(name: str) -> None:
        try:
            with pacing.urgent(pacing.QUEUED):
                async with pace.opening():
                    outcomes.append(f"{name} opened")
        except Blocked:
            outcomes.append(f"{name} blocked")

    with anyio.fail_after(2):
        await told(pace, ask, open_audio, outcomes)
    assert sorted(outcomes) == ["a blocked", "audio blocked", "b blocked", "c blocked"]
    assert pace.sent == 1 and not len(pace._requests) and not len(pace._audio)


async def told(pace: AddonPace, ask: Any, open_audio: Any, outcomes: list[str]) -> None:
    with pacing.urgent(pacing.WARM):
        over = await pace.open()
    async with anyio.create_task_group() as tg:
        for name in ("a", "b", "c"):
            tg.start_soon(ask, name)
        tg.start_soon(open_audio, "audio")
        await anyio.sleep(0.02)
        assert outcomes == []
        pace.block(5, audio=True)  # (its audio said "too many requests"): they all end
    over()


@pytest.mark.anyio
async def test_many_waiting_requests_cost_a_look_each_not_a_look_by_all() -> None:
    """Only the first in line looks whether its turn has come: a thousand waiting do not
    keep the loop busy (they did, each woken at every turn)."""
    pace = AddonPace(Limits(0.001, 1, 0))
    await pace.request()
    looks = 0
    take = pace._take

    def counted() -> float | None:
        nonlocal looks
        looks += 1
        return take()

    pace._take = counted  # type: ignore[method-assign]
    done = 0

    async def ask() -> None:
        nonlocal done
        with pacing.urgent(pacing.QUEUED):
            await pace.request()
        done += 1

    began = time.monotonic()
    async with anyio.create_task_group() as tg:
        for _ in range(1000):
            tg.start_soon(ask)
        await anyio.sleep(0.01)
        pace.configure(Limits(100_000.0, 1, 0))
    assert done == 1000 and time.monotonic() - began < 5.0
    assert looks < 10 * 1000  # (each waiter woken at every turn: about half a million)


@pytest.mark.anyio
async def test_every_hop_of_a_redirected_request_takes_a_turn() -> None:
    """A redirect is a request too: each hop takes its turn at the limits of the origin it
    goes to - the client does not follow redirects by itself."""
    import httpx

    seen: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        hop = int(request.url.params.get("hop", "0"))
        if request.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        if hop < 3:
            host = "other.example.invalid" if hop == 1 else request.url.host
            return httpx.Response(307, headers={"location": f"https://{host}/x?hop={hop + 1}"})
        return httpx.Response(200, json={"hop": hop})

    turns: list[str] = []

    async def turn(url: httpx.URL) -> None:
        turns.append(url.host)

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as http:
        async with pacing.get(http, "https://addon.example.invalid/x", turn=turn) as response:
            assert response.status_code == 200 and (await response.aread()) == b'{"hop":3}'
        assert turns == ["addon.example.invalid"] * 2 + ["other.example.invalid"] * 2
        assert len(seen) == 4  # a turn a request, never one for the chain
        turns.clear()
        with pytest.raises(httpx.TooManyRedirects):
            async with pacing.get(http, "https://addon.example.invalid/loop", turn=turn):
                raise AssertionError("no answer after too many redirects")
        assert len(turns) == 6  # the request and five redirects, each in its turn

        async def refused(url: httpx.URL) -> None:
            if turns:
                raise Blocked(30)  # the origin the redirect goes to is left alone
            turns.append(url.host)

        turns.clear()
        seen.clear()
        with pytest.raises(Blocked):
            async with pacing.get(http, "https://addon.example.invalid/x", turn=refused):
                raise AssertionError("not sent")
        assert len(seen) == 1  # the hop was not sent


class _Clock:
    """A clock moved by hand, in place of the ``time`` module the wait records read: how
    long a sleep really took decides nothing."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def passes(self, seconds: float) -> None:
        self.now += seconds


def test_a_wait_for_another_request_s_answer_counts_what_overlapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request waiting for another's manifest read waited at the limit only as far as
    that one did meanwhile: what the reader waited before this one came, or spends at the
    add-on, is the add-on's time."""
    clock = _Clock()
    monkeypatch.setattr(pacing, "time", clock)

    reader = pacing.Waits(seconds=1.1)  # it waited 1.1 s at the limit, and is at the add-on
    joined = reader.joined()
    clock.passes(2.0)  # ... where it stays while this one waits for it
    mine = pacing.Waits()
    mine.shared(reader, joined)
    assert mine.at is None and mine.seconds == 0.0 and mine.held(0.5) is None

    # The reader waits at the limit while this one waits for it, and still does when this
    # one's time runs out: it is waiting there too.
    reader = pacing.Waits()
    reader._began(pacing.REQUESTS)
    joined = reader.joined()
    clock.passes(0.02)
    mine = pacing.Waits()
    mine.shared(reader, joined)
    assert mine.at == pacing.REQUESTS and mine.seconds == pytest.approx(0.02)

    # A wait under way when this one came, over since: only what came after counts.
    reader = pacing.Waits()
    reader._began(pacing.REQUESTS)
    clock.passes(0.03)
    joined = reader.joined()
    clock.passes(0.02)
    reader._ended(0.05)  # (the whole wait: 30 ms before this one came, 20 ms after)
    clock.passes(0.5)  # ... and the reader is at the add-on since
    mine = pacing.Waits()
    mine.shared(reader, joined)
    assert mine.at is None and mine.seconds == pytest.approx(0.02)
