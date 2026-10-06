"""Fetches ahead of the song being played, units: which play is current, and a fetch
ahead promoted when the client reports its song as playing."""

from __future__ import annotations

import anyio
import pytest

from shijhon.delivery.ahead import AheadGate
from shijhon.delivery.listening import Listening

pytestmark = pytest.mark.anyio
WHO = ("ann", "queue-player")


async def test_a_play_right_after_another_is_ahead_unless_reported() -> None:
    listening = Listening()
    gate = AheadGate(listening, window_seconds=0.5)
    first = listening.started(WHO, "demo:1")
    assert gate.current(WHO, "demo:1", first)
    second = listening.started(WHO, "demo:2")
    assert not gate.current(WHO, "demo:2", second)
    assert gate.current(("ann", "Other"), "demo:3", listening.started(("ann", "Other"), "demo:3"))
    listening.reported("ann", "demo:2")  # the listener skipped to it
    assert gate.current(WHO, "demo:2", second)


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
