"""A request's turn among its user's fetches from the add-ons: a repeat of a
request (a client's retry) waits for that request's fetch instead of queueing again - and
goes on at once itself when that request fetched nothing; the song being played promotes a
fetch of it waiting for its turn (a new request for it, or the client's report); a turn in
which nothing was fetched gives the hour's allowance back."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import anyio
import pytest

from shijhon.delivery.download_first import DownloadFirst, whole
from shijhon.delivery.intercept import _subsonic_error
from shijhon.delivery.limits import UserLimits
from shijhon.delivery.listening import Listening
from shijhon.delivery.playback import Opened


def fetches(downloads: int = 1, per_hour: int = 0) -> DownloadFirst:
    limits = UserLimits(downloads=downloads, downloads_per_hour=per_hour)
    none = cast(Any, None)
    return DownloadFirst(none, none, none, Path("unused"), limits=limits)


async def settle() -> None:
    """Let the other tasks run as far as they can."""
    for _ in range(20):
        await anyio.lowlevel.checkpoint()


@pytest.mark.anyio
async def test_a_repeat_gets_the_outcome_of_the_request_it_repeats() -> None:
    d = fetches()
    seen: list[Any] = []
    entered, release = anyio.Event(), anyio.Event()

    async def first() -> None:
        async with d.turn("cat:1", "ann") as turn:
            entered.set()
            await release.wait()
            turn.spent = True
            turn.decided("no source: nothing found")

    async def repeat() -> None:
        async with d.turn("cat:1", "ann") as turn:
            seen.append((turn.waited, turn.after))

    async with anyio.create_task_group() as group:
        group.start_soon(first)
        await entered.wait()
        group.start_soon(repeat)
        await settle()
        assert seen == []  # it waits for the first one
        release.set()
    assert seen == [(True, "no source: nothing found")]


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["nothing fetched", "canceled"])
async def test_a_repeat_of_a_request_that_fetched_nothing_goes_on_at_once(ending: str) -> None:
    """The first request streamed the song as it is (or was canceled): its repeat does not
    queue again behind the user's other fetches."""
    d = fetches()
    entered, release, other_in, done = anyio.Event(), anyio.Event(), anyio.Event(), anyio.Event()
    seen: list[bool] = []

    async def first(scope: anyio.CancelScope) -> None:
        with scope:
            async with d.turn("cat:1", "ann"):
                entered.set()
                await release.wait()  # served as it is: nothing fetched, nothing decided

    async def other() -> None:  # another song's fetch: next in the one turn, and long
        async with d.turn("cat:2", "ann") as turn:
            other_in.set()
            turn.spent = True
            await done.wait()

    async def repeat() -> None:
        async with d.turn("cat:1", "ann") as turn:
            seen.append(turn.waited)
        done.set()

    scope = anyio.CancelScope()
    async with anyio.create_task_group() as group:
        group.start_soon(first, scope)
        await entered.wait()
        group.start_soon(repeat)
        await settle()
        group.start_soon(other)
        await settle()
        if ending == "canceled":
            scope.cancel()
        else:
            release.set()
        with anyio.fail_after(2):
            await done.wait()  # the repeat went on: never queued behind the other fetch
    assert seen == [False] and other_in.is_set()


@pytest.mark.anyio
async def test_a_repeat_of_a_fetch_that_was_interrupted_gets_its_failure() -> None:
    """Canceled in the middle of its fetch: no "fetched nothing" for its repeats - they do
    not start a fetch at once, past the user's turns."""
    d = fetches()
    entered = anyio.Event()
    seen: list[Any] = []

    async def first(scope: anyio.CancelScope) -> None:
        with scope:
            async with d.turn("cat:1", "ann") as turn:
                turn.spent = True  # fetching from the add-ons
                entered.set()
                await anyio.sleep_forever()

    async def repeat() -> None:
        async with d.turn("cat:1", "ann") as turn:
            seen.append((turn.waited, turn.after))

    scope = anyio.CancelScope()
    async with anyio.create_task_group() as group:
        group.start_soon(first, scope)
        await entered.wait()
        group.start_soon(repeat)
        await settle()
        scope.cancel()
    assert seen == [(True, "the request it waited for failed")]


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["a request for it", "the client's report"])
async def test_the_song_being_played_promotes_its_fetch_waiting_for_a_turn(how: str) -> None:
    d = fetches()
    listening = Listening()
    listening.on_current = d.promote
    holding, release, went = anyio.Event(), anyio.Event(), anyio.Event()

    async def holder() -> None:
        async with d.turn("cat:9", "ann") as turn:
            turn.spent = True
            holding.set()
            await release.wait()

    async def waiting() -> None:  # a fetch ahead of the song, waiting for the one turn
        async with d.turn("cat:1", "ann"):
            went.set()

    async with anyio.create_task_group() as group:
        group.start_soon(holder)
        await holding.wait()
        group.start_soon(waiting)
        await settle()
        assert not went.is_set()
        if how == "a request for it":
            async with d.turn("cat:1", "ann", playing=True):
                pass  # the client plays that song now: this request goes at once too
        else:
            listening.reported("bob", "cat:1")  # another user's report: not this fetch's
            await settle()
            assert not went.is_set()
            listening.reported("ann", "cat:1")
        with anyio.fail_after(2):
            await went.wait()  # before the holder is done
        release.set()


@pytest.mark.anyio
async def test_a_song_the_client_plays_already_does_not_queue() -> None:
    """Its report came before the request reached its turn: it goes at once all the same."""
    d = fetches()
    listening = Listening()
    d.current = lambda user, key: listening.current_for(user) == key
    holding, release = anyio.Event(), anyio.Event()

    async def holder() -> None:
        async with d.turn("cat:9", "ann") as turn:
            turn.spent = True
            holding.set()
            await release.wait()

    async with anyio.create_task_group() as group:
        group.start_soon(holder)
        await holding.wait()
        listening.reported("ann", "cat:1")
        with anyio.fail_after(2):
            async with d.turn("cat:1", "ann"):
                pass  # not behind the holder
        release.set()


@pytest.mark.anyio
async def test_a_turn_in_which_nothing_was_fetched_gives_its_allowance_back() -> None:
    d = fetches(downloads=2, per_hour=3)
    assert d.limits is not None

    def allowance() -> float:
        records = list(d.limits._users.values()) if d.limits is not None else []
        return records[0].allowance if records else 3.0

    async with d.turn("cat:1", "ann"):  # e.g. its audio needed no converting
        assert allowance() < 2.01
    assert allowance() > 2.99
    async with d.turn("cat:1", "ann") as turn:
        turn.spent = True  # the add-ons' audio was fetched
    assert allowance() < 2.01
    async with d.turn("cat:2", "ann", playing=True):  # the song being played: the same
        pass
    assert 1.99 < allowance() < 2.01


def test_an_answer_holding_the_whole_file() -> None:
    def answer(status: int, content_range: str | None = None, body: bool = True) -> Opened:
        headers = [(b"content-range", content_range.encode())] if content_range else []

        async def nothing() -> None:
            return None

        return Opened(status, headers, cast(Any, object()) if body else None, nothing)

    assert whole(answer(200))
    assert whole(answer(206, "bytes 0-1999/2000"))
    assert not whole(answer(206, "bytes 0-1/2000"))  # a client's probe
    assert not whole(answer(206, "bytes 100-1999/2000"))
    assert not whole(answer(206, "bytes 0-1999/*"))
    assert not whole(answer(200, body=False)) and not whole(answer(416, "bytes */2000"))


def test_a_subsonic_error_is_told_in_every_format() -> None:
    assert _subsonic_error(b'{"subsonic-response":{"status":"failed","error":{"code":10}}}')
    assert _subsonic_error(b'{"subsonic-response": {"status" : "failed"}}')
    assert _subsonic_error(b'<subsonic-response xmlns="x" status="failed" version="1.16.1">')
    assert _subsonic_error(b'callback({"subsonic-response":{"status":"failed"}});')
    assert not _subsonic_error(b'{"subsonic-response":{"status":"ok"}}')
    assert not _subsonic_error(b"fLaC\x00\x00\x00\x22")


def test_listening_tells_which_song_a_client_plays_now() -> None:
    told: list[tuple[str, str]] = []
    listening = Listening()
    listening.on_current = lambda user, key: told.append((user, key))
    listening.reported("ann", "cat:1")
    listening.saved_queue("ann", ["cat:1", "cat:2"], 1)
    listening.saved_queue("ann", ["cat:3"], 5)  # no such position: nothing told
    assert told == [("ann", "cat:1"), ("ann", "cat:2")]
