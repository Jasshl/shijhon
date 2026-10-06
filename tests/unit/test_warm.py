"""Warm-ahead's jobs: a few at once for all listeners together."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest

from shijhon.delivery.listening import Listening
from shijhon.delivery.playback import Track
from shijhon.delivery.warm import WarmAhead


def track(name: str) -> Track:
    return Track(name, None, name, "Artist", 180_000)


class Deliverer:
    """Opens the songs it is asked to warm, noting how many at once."""

    def __init__(self, group: Any, seconds: float = 0.05) -> None:
        self.settings = SimpleNamespace(warm_ahead_depth=2)
        self.group = group
        self.seconds = seconds
        self.warming = 0
        self.most = 0
        self.warmed: list[str] = []

    def background(self, work: Callable[[], Awaitable[Any]]) -> None:
        self.group.start_soon(work)

    async def prewarm(self, item: Track) -> bool:
        self.warming += 1
        self.most = max(self.most, self.warming)
        await anyio.sleep(self.seconds)
        self.warming -= 1
        self.warmed.append(item.song_id)
        return True


class Upcoming:
    async def track(self, key: str) -> Track | None:
        return track(key)

    async def after(self, played: Track, depth: int) -> list[Track]:
        return [track(f"{played.song_id}+{n}") for n in range(1, depth + 1)]


def warm_ahead(deliverer: Deliverer, jobs: int) -> WarmAhead:
    return WarmAhead(cast(Any, deliverer), Listening(), Upcoming(), delay_seconds=0.0, jobs=jobs)


@pytest.mark.anyio
async def test_only_so_many_warm_ahead_jobs_run_at_once() -> None:
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group)
        warm = warm_ahead(deliverer, jobs=2)
        for listener in range(5):  # five listeners' plays start together
            warm.started(None, track(f"play-{listener}"), 0.0)
    assert deliverer.most == 2  # two jobs, each opening its songs one after the other
    assert len(deliverer.warmed) == 10 and warm.warmed == 10  # the others had their turn


@pytest.mark.anyio
async def test_one_job_at_once_and_a_changed_setting_applies_at_once() -> None:
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group)
        warm = warm_ahead(deliverer, jobs=1)
        assert warm.jobs == 1
        for listener in range(3):
            warm.started(None, track(f"play-{listener}"), 0.0)
        await anyio.sleep(0.02)
        assert deliverer.most == 1
    assert deliverer.most == 1 and len(deliverer.warmed) == 6
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group)
        warm.deliverer = cast(Any, deliverer)
        warm.jobs = 3  # (the dashboard)
        for listener in range(4):
            warm.started(None, track(f"more-{listener}"), 0.0)
    assert deliverer.most == 3


@pytest.mark.anyio
async def test_a_job_whose_turn_does_not_come_is_dropped() -> None:
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group, seconds=0.2)
        warm = warm_ahead(deliverer, jobs=1)
        warm.patience = 0.05
        warm.started(None, track("first"), 0.0)
        await anyio.sleep(0.01)
        warm.started(None, track("late"), 0.0)  # waits 0.05 s for a job that takes 0.4 s
    assert deliverer.warmed == ["first+1", "first+2"]
    # ... and nothing is left taken: the next play's job runs.
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group, seconds=0.01)
        warm.deliverer = cast(Any, deliverer)
        warm.started(None, track("next"), 0.0)
    assert deliverer.warmed == ["next+1", "next+2"]


@pytest.mark.anyio
async def test_each_listener_s_play_of_a_song_has_its_own_job() -> None:
    """Two listeners playing the same song: one's job waiting for its turn (or dropped) does
    not keep the other from its warm-ahead."""
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group, seconds=0.01)
        warm = warm_ahead(deliverer, jobs=2)
        warm.started(
            ("ann", "phone"), track("same"), warm.listening.started(("ann", "phone"), "same")
        )
        warm.started(("bob", "car"), track("same"), warm.listening.started(("bob", "car"), "same"))
        warm.started(("ann", "phone"), track("same"), 0.0)  # (its own job is under way)
    assert sorted(deliverer.warmed) == ["same+1", "same+1", "same+2", "same+2"]


@pytest.mark.anyio
async def test_a_job_that_waited_while_its_listener_moved_on_is_dropped() -> None:
    now = [100.0]
    listening = Listening(clock=lambda: now[0])
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group, seconds=0.05)
        warm = WarmAhead(cast(Any, deliverer), listening, Upcoming(), delay_seconds=0.0, jobs=1)
        who = ("ann", "phone")
        warm.started(None, track("other"), 0.0)  # holds the one turn for a while
        await anyio.sleep(0.01)
        warm.started(who, track("skipped"), listening.started(who, "skipped"))
        now[0] += 5.0
        listening.started(who, "next")  # the listener skipped on while the job waited
        warm.started(who, track("next"), now[0])
    assert sorted(deliverer.warmed) == ["next+1", "next+2", "other+1", "other+2"]


@pytest.mark.anyio
async def test_a_listener_back_at_a_song_while_its_job_waited_gets_its_warm_ahead() -> None:
    """A to B and back to A while A's job waits for its turn: the job is for the latest
    start of A, and what was started before that is no "moved on"."""
    now = [100.0]
    listening = Listening(clock=lambda: now[0])
    async with anyio.create_task_group() as group:
        deliverer = Deliverer(group, seconds=0.05)
        warm = WarmAhead(cast(Any, deliverer), listening, Upcoming(), delay_seconds=0.0, jobs=1)
        who = ("ann", "phone")
        warm.started(None, track("other"), 0.0)  # holds the one turn for a while
        await anyio.sleep(0.01)
        warm.started(who, track("a"), listening.started(who, "a"))
        now[0] += 5.0
        listening.started(who, "b")  # skipped on ...
        now[0] += 5.0
        warm.started(who, track("a"), listening.started(who, "a"))  # ... and back
    assert sorted(deliverer.warmed) == ["a+1", "a+2", "other+1", "other+2"]
