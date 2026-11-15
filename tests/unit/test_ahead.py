"""Fetches ahead of the song being played, units: which play is current, and a fetch
ahead promoted when the client reports its song as playing."""

from __future__ import annotations

import anyio
import pytest

from shijhon.delivery.ahead import AheadGate
from shijhon.delivery.listening import Listening

pytestmark = pytest.mark.anyio
WHO = ("ann", "queue-player")


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_a_play_right_after_another_is_ahead_unless_reported() -> None:
    clock = Clock()
    listening = Listening(clock=clock)
    gate = AheadGate(listening, window_seconds=0.5, clock=clock)
    first = listening.started(WHO, "demo:1")
    assert gate.current(WHO, "demo:1", first)
    clock.now += 0.1  # (such a client's fetches ahead come 0.1-0.3 s after its song)
    second = listening.started(WHO, "demo:2")
    assert not gate.current(WHO, "demo:2", second)
    assert gate.current(("ann", "Other"), "demo:3", listening.started(("ann", "Other"), "demo:3"))
    listening.reported("ann", "demo:2")  # the listener skipped to it
    assert gate.current(WHO, "demo:2", second)


async def test_two_songs_asked_for_together_are_both_plays_in_either_order() -> None:
    """A player with two audio elements loads the song it plays and the next one at the
    same moment, and reports its song only later: which request is noted first is chance,
    so neither waits behind the other."""
    for order in (("demo:a", "demo:b"), ("demo:b", "demo:a")):
        gate = AheadGate(Listening(), window_seconds=0.5)
        arrived = {"demo:a": 100.0, "demo:b": 100.01}
        assert [gate.note(WHO, key, arrived[key]) for key in order] == [True, True]


@pytest.mark.parametrize(
    "order",
    [
        ("c", "b", "a"),  # noted in reverse
        ("a", "b", "c"),
        ("b", "d", "a", "c"),  # interleaved
        ("d", "c", "b", "a"),
        ("c", "a", "d", "b"),
    ],
)
async def test_at_most_two_plays_whatever_order_starts_are_noted_in(order: tuple[str, ...]) -> None:
    """Requests that came 10 ms apart may be noted in any order (one waited for a catalog
    lookup, another for its credentials): at most two of them are plays."""
    clock = Clock()
    listening = Listening(clock=clock)
    gate = AheadGate(listening, window_seconds=0.5, clock=clock)
    arrived = {key: 100.0 + 0.01 * n for n, key in enumerate("abcd")}
    clock.now = 100.5  # all noted later, as of when each came
    starts = {key: listening.started(WHO, f"demo:{key}", clock.now - arrived[key]) for key in order}
    plays = [gate.current(WHO, f"demo:{key}", starts[key]) for key in order]
    assert sum(plays) == 2 and plays[:2] == [True, True]
    assert [gate.current(WHO, f"demo:{key}", starts[key]) for key in order] == plays  # kept


async def test_an_offline_save_of_an_album_streamed_at_once_has_at_most_two_plays() -> None:
    clock = Clock()
    listening = Listening(clock=clock)
    gate = AheadGate(listening, window_seconds=0.5, clock=clock)
    clock.now = 101.0
    plays = []
    for n in range(12):  # noted in the reverse order of their requests
        at = listening.started(WHO, f"demo:{n}", clock.now - (100.0 + 0.001 * (12 - n)))
        plays.append(gate.current(WHO, f"demo:{n}", at))
    assert sum(plays) == 2


async def test_a_song_noted_beside_a_fetch_ahead_or_long_after_its_play_is_a_fetch_ahead() -> None:
    gate = AheadGate(Listening(), window_seconds=0.5)
    assert gate.note(WHO, "demo:1", 100.0)
    assert not gate.note(WHO, "demo:2", 100.2)  # 0.2 s after: a fetch ahead
    assert not gate.note(WHO, "demo:3", 100.21)  # together with a fetch ahead only
    assert gate.note(WHO, "demo:4", 101.0)  # past the window: a new play


async def test_only_the_same_client_s_report_tells_a_pair_apart() -> None:
    """The account's reports and saved queues are any of its devices': another device's
    report of one of the two songs says nothing about this client's pair."""
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5)
    listening.reported("ann", "demo:next", client="other-device")
    assert gate.note(WHO, "demo:next", 100.0)
    assert gate.note(WHO, "demo:played", 100.01)
    listening.reported("ann", "demo:next2", client=WHO[1])  # this client names it
    assert gate.note(WHO, "demo:next2", 200.0)
    assert not gate.note(WHO, "demo:played2", 200.01)


async def test_a_start_is_noted_as_of_when_its_request_came() -> None:
    """Work done for a request before its start is noted (a credential check, a catalog
    lookup) does not move it apart from a song asked for together with it."""
    clock = Clock()
    listening = Listening(clock=clock)
    first = listening.started(WHO, "demo:1")
    clock.now += 0.3  # the other request came 0.01 s after the first, its start noted now
    second = listening.started(WHO, "demo:2", ago=0.29)
    assert second == pytest.approx(first + 0.01)


async def test_a_listener_s_backdated_starts_do_not_make_it_look_quiet() -> None:
    """A start noted later but as of an earlier moment comes last in the list: the
    clean-up of quiet listeners looks at the newest start, not the last one noted."""
    clock = Clock()
    listening = Listening(clock=clock, memory_seconds=600.0)
    listening.started(WHO, "demo:new")
    listening.started(WHO, "demo:old", ago=700.0)
    for n in range(1025):
        listening.started((f"user{n}", "app"), "demo:x", ago=900.0)
    clock.now += 1.0
    listening.started(("someone", "app"), "demo:y")  # the clean-up runs
    assert listening.fetched_between(WHO, clock.now - 10, clock.now, set()) == {"demo:new"}


async def test_fetches_ahead_wait_for_every_play_being_opened() -> None:
    """Two plays opened at once (a pair): the one that gets its first byte first does
    not release the client's fetches ahead while the other has none yet."""
    gate = AheadGate(Listening(), window_seconds=0.5, report_seconds=0.0, current_seconds=5.0)
    order: list[str] = []
    slow_done = anyio.Event()

    async def play(name: str, seconds: float) -> None:
        async with gate.playing(WHO):
            await anyio.sleep(seconds)
            order.append(name)

    async def fetch_ahead() -> None:
        async with gate.turn(WHO, "demo:3"):
            order.append("ahead")

    async def slow() -> None:
        await play("slow", 0.4)
        slow_done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(slow)
        tg.start_soon(play, "fast", 0.1)
        await anyio.sleep(0.01)
        tg.start_soon(fetch_ahead)
    assert order == ["fast", "slow", "ahead"]


async def test_fetches_ahead_wait_for_the_played_song_and_each_other() -> None:
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5, report_seconds=0.0)
    order: list[str] = []

    async def fetch_ahead(key: str) -> None:
        async with gate.turn(WHO, key) as turn:
            order.append(f"{key}{'!' if turn.promoted else ''}+")
            await anyio.sleep(0.05)
            order.append(f"{key}-")

    async with anyio.create_task_group() as tg, gate.playing(WHO):
        tg.start_soon(fetch_ahead, "demo:2")
        tg.start_soon(fetch_ahead, "demo:3")
        await anyio.sleep(0.2)
        order.append("played")
    # After the played song, one at a time, in the order they came.
    assert order == ["played", "demo:2+", "demo:2-", "demo:3+", "demo:3-"]


async def test_the_next_song_of_the_saved_queue_takes_its_turn_first() -> None:
    """The fetches ahead come in any order; the song after the current one in the
    client's saved queue is the one prepared first, then the others as they came - and
    without a queue (or once the next one is done), the order of their coming."""
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5, report_seconds=0.0)
    listening.saved_queue("ann", ["s:1", "s:2", "s:3", "s:4", "s:5"], 0)
    order: list[str] = []

    async def fetch_ahead(key: str) -> None:
        async with gate.turn(WHO, key):
            order.append(key)
            await anyio.sleep(0.06)

    async with anyio.create_task_group() as tg, gate.playing(WHO):
        for key in ("s:5", "s:4", "s:2", "s:3"):  # the next one (s:2) comes third
            tg.start_soon(fetch_ahead, key)
            await anyio.sleep(0.01)
    assert order == ["s:2", "s:5", "s:4", "s:3"]
    assert gate._waiting == {}
    # Another client of the same user has turns of its own; a queue that does not hold
    # the current song says nothing.
    listening.reported("ann", "s:9")
    order.clear()
    async with anyio.create_task_group() as tg:
        for key in ("s:3", "s:2"):
            tg.start_soon(fetch_ahead, key)
            await anyio.sleep(0.01)
    assert order == ["s:3", "s:2"]


async def test_a_fetch_ahead_that_leaves_while_waiting_does_not_hold_the_others() -> None:
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5, report_seconds=0.0)
    order: list[str] = []
    leaving = anyio.CancelScope()

    async def fetch_ahead(key: str, scope: anyio.CancelScope | None = None) -> None:
        with scope or anyio.CancelScope():
            async with gate.turn(WHO, key):
                order.append(key)

    async with anyio.create_task_group() as tg, gate.playing(WHO):
        tg.start_soon(fetch_ahead, "s:2", leaving)  # the first in line
        await anyio.sleep(0.01)
        tg.start_soon(fetch_ahead, "s:3")
        await anyio.sleep(0.05)
        leaving.cancel()  # its client left while it waited for the played song
        await anyio.sleep(0.05)
    assert order == ["s:3"] and gate._waiting == {}


async def test_a_fetch_ahead_reported_as_playing_goes_at_once() -> None:
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5)
    went: list[bool] = []
    done = anyio.Event()

    async def fetch_ahead() -> None:
        async with gate.turn(WHO, "demo:2") as turn:
            went.append(turn.promoted)
        done.set()

    async with anyio.create_task_group() as tg, gate.playing(WHO):  # never ends here
        tg.start_soon(fetch_ahead)
        await anyio.sleep(0.1)
        listening.reported("ann", "demo:2")
        with anyio.fail_after(1):
            await done.wait()
    assert went == [True]


async def test_a_fetch_ahead_that_waits_out_its_turn_goes_alone() -> None:
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5, report_seconds=0.0, wait_seconds=0.2)
    turns = []
    holding = anyio.Event()

    async def first() -> None:
        async with gate.turn(WHO, "demo:2") as turn:
            turns.append(turn)
            holding.set()
            await anyio.sleep(0.5)  # a slow fetch ahead keeps the turn

    async with anyio.create_task_group() as tg:
        tg.start_soon(first)
        await holding.wait()
        async with gate.turn(WHO, "demo:3") as second:
            turns.append(second)
    assert not turns[0].alone  # it had the turn
    assert turns[1].alone and turns[1].waited >= 0.2  # the other waited it out


async def test_a_preload_after_the_played_song_stays_a_fetch_ahead_without_a_report() -> None:
    """A preload of the next song 0.1-0.3 s after the played one, with no report or queue
    in time: it waits for the played song's first byte (or the bound), however long the
    moment for the report has passed - never a second play racing it."""
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5, report_seconds=0.1, current_seconds=0.6)
    played = listening.started(WHO, "demo:played", ago=0.2)
    assert gate.current(WHO, "demo:played", played)
    following = listening.started(WHO, "demo:next")
    assert not gate.current(WHO, "demo:next", following)
    # (The played song has no first byte yet.)
    async with gate.playing(WHO), gate.turn(WHO, "demo:next") as turn:
        assert not turn.promoted and turn.waited >= 0.6


async def test_a_start_that_came_first_is_classified_first_though_noted_later() -> None:
    """The played song's request came 0.2 s before the next song's preload, but its
    credentials take longer to check: the preload waits for it (at most the window), so
    the order they came in decides - the played song a play, the preload a fetch ahead."""
    gate = AheadGate(Listening(), window_seconds=0.5)
    played = gate.arriving(WHO, 100.0)
    preload = gate.arriving(WHO, 100.2)
    classes: dict[str, bool] = {}

    async def note_preload() -> None:
        await gate.settle(WHO, 100.2, preload)
        classes["next"] = gate.note(WHO, "demo:next", 100.2)
        gate.arrived(WHO, preload)

    async with anyio.create_task_group() as tg:
        tg.start_soon(note_preload)
        await anyio.sleep(0.1)  # the played song's check ends later
        assert "next" not in classes
        classes["played"] = gate.note(WHO, "demo:played", 100.0)
        gate.arrived(WHO, played)
    assert classes == {"played": True, "next": False}


async def test_a_start_waits_for_an_earlier_request_no_longer_than_the_window() -> None:
    gate = AheadGate(Listening(), window_seconds=0.3)
    gate.arriving(WHO, 100.0)  # never noted (its request ends some other way, late)
    later = gate.arriving(WHO, 100.1)
    with anyio.fail_after(1):
        await gate.settle(WHO, 100.1, later)
    assert gate.note(WHO, "demo:2", 100.1)


async def test_requests_for_one_song_count_once_beside_another() -> None:
    """The next song asked for twice (a probe and its play, a retry) and the played song
    together: still a pair of two songs, both plays."""
    gate = AheadGate(Listening(), window_seconds=0.5)
    assert gate.note(WHO, "demo:next", 100.0)
    assert gate.note(WHO, "demo:next", 100.01)
    assert gate.note(WHO, "demo:played", 100.02)
    assert not gate.note(WHO, "demo:third", 100.03)  # a third song: a fetch ahead


@pytest.mark.parametrize("ends", ["failure", "cancel"])
async def test_a_play_whose_opening_fails_or_is_canceled_releases_the_fetches_ahead(
    ends: str,
) -> None:
    gate = AheadGate(Listening(), window_seconds=0.5, report_seconds=0.0, current_seconds=5.0)
    order: list[str] = []

    async def play(scope: anyio.CancelScope) -> None:
        with scope:
            try:
                async with gate.playing(WHO):
                    await anyio.sleep(0.2)
                    if ends == "failure":
                        raise RuntimeError("no audio")
                    scope.cancel()
                    await anyio.sleep(10)
            except RuntimeError:
                order.append("failed")
        order.append("ended")

    async def fetch_ahead() -> None:
        async with gate.turn(WHO, "demo:2"):
            order.append("ahead")

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tg:
            tg.start_soon(play, anyio.CancelScope())
            await anyio.sleep(0.01)
            tg.start_soon(fetch_ahead)
    assert order[-1] == "ahead" and "ended" in order
    assert gate._opening == {}


async def test_a_play_that_never_gets_audio_holds_the_fetches_ahead_only_for_the_bound() -> None:
    gate = AheadGate(Listening(), window_seconds=0.5, report_seconds=0.0, current_seconds=0.3)
    async with gate.playing(WHO):  # no first byte while this runs
        with anyio.fail_after(2):
            async with gate.turn(WHO, "demo:2") as turn:
                assert turn.waited >= 0.3 and not turn.alone
