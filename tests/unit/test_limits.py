"""Per-user limits on add-on work."""

from __future__ import annotations

import anyio
import anyio.lowlevel
import pytest

from shijhon.delivery.limits import Limited, UserLimits


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await anyio.lowlevel.checkpoint()


@pytest.mark.anyio
async def test_routings_wait_for_a_turn_per_user() -> None:
    limits = UserLimits(routings=2)
    running, most = 0, 0

    async def route(user: str) -> None:
        nonlocal running, most
        async with limits.routing(user):
            running += 1
            most = max(most, running)
            await anyio.sleep(0.02)
            running -= 1

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(route, "ann")
    assert most == 2
    assert limits._users == {}  # nothing kept once nobody waits


@pytest.mark.anyio
async def test_other_users_have_their_own_turns() -> None:
    limits = UserLimits(routings=1)
    async with limits.routing("ann"):
        with anyio.fail_after(1):
            async with limits.routing("bob") as waited:
                assert waited < 0.1


@pytest.mark.anyio
async def test_past_the_hour_s_allowance_downloads_wait_their_turn() -> None:
    """The allowance at once, then one every hour / allowance, in order: a large offline
    sync slows down instead of failing."""
    clock = Clock()
    limits = UserLimits(downloads=1, downloads_per_hour=2, clock=clock, sleep=clock.sleep)
    for _ in range(2):
        async with limits.download("ann"):
            pass
    assert clock.slept == []
    async with limits.download("ann"):  # paced: 1800 s later
        pass
    assert sum(clock.slept) == pytest.approx(1800)
    async with limits.download("bob"):  # another user's allowance
        pass
    assert sum(clock.slept) == pytest.approx(1800)
    clock.now += 3600  # an hour later the allowance is whole again
    for _ in range(2):
        async with limits.download("ann"):
            pass
    assert sum(clock.slept) == pytest.approx(1800)


@pytest.mark.anyio
async def test_the_song_being_played_takes_from_the_allowance_without_waiting() -> None:
    clock = Clock()
    limits = UserLimits(downloads=1, downloads_per_hour=2, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        async with limits.download("ann", queue=False):
            pass
    assert clock.slept == []  # never waits
    async with limits.download("ann"):  # the fetch after it waits for the debt too
        pass
    assert sum(clock.slept) == pytest.approx(2 * 1800)


@pytest.mark.anyio
async def test_a_download_that_needed_no_add_on_gives_its_allowance_back() -> None:
    clock = Clock()
    limits = UserLimits(downloads=1, downloads_per_hour=1, clock=clock, sleep=clock.sleep)
    async with limits.download("ann") as give_back:
        give_back()
        give_back()  # once only
    async with limits.download("ann"):
        pass
    assert clock.slept == []


@pytest.mark.anyio
async def test_a_waiting_download_the_client_plays_goes_at_once() -> None:
    limits = UserLimits(downloads=1, downloads_per_hour=10)
    promoted = anyio.Event()
    went = anyio.Event()

    async def fetch_ahead() -> None:
        async with limits.download("ann", promoted=promoted):
            went.set()

    async with limits.download("ann"), anyio.create_task_group() as tg:  # the one turn
        tg.start_soon(fetch_ahead)
        await anyio.sleep(0.05)
        assert not went.is_set()  # waits its turn
        promoted.set()  # the client plays it now
        with anyio.fail_after(1):
            await went.wait()
    assert limits._users["ann"].downloads.value == 1  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_zero_turns_a_limit_off() -> None:
    limits = UserLimits(routings=0, downloads=0, downloads_per_hour=0)
    async with limits.routing("ann"), limits.routing("ann"):
        pass
    for _ in range(500):
        async with limits.download("ann"):
            pass


@pytest.mark.anyio
async def test_a_turn_is_awaited_only_within_the_wait_cap() -> None:
    limits = UserLimits(routings=1)
    async with limits.routing("ann"):
        with pytest.raises(Limited, match=r"no turn within 0\.05s"):
            async with limits.routing("ann", wait=0.05):
                pass
    async with limits.routing("ann", wait=0.05) as waited:  # free again
        assert waited < 0.05


@pytest.mark.anyio
async def test_the_song_being_played_does_not_wait_behind_routings() -> None:
    limits = UserLimits(routings=1)
    async with limits.routing("ann"):
        with anyio.fail_after(1):
            async with limits.routing("ann", queue=False) as waited:
                assert waited < 0.1


@pytest.mark.anyio
async def test_plays_have_turns_of_their_own_twice_as_many() -> None:
    """Quick skipping stays bounded: two plays per routing turn at once."""
    limits = UserLimits(routings=1)
    async with limits.routing("ann", queue=False), limits.routing("ann", queue=False):
        with pytest.raises(Limited):
            async with limits.routing("ann", queue=False, wait=0.05):
                pass


@pytest.mark.anyio
async def test_the_song_being_played_does_not_wait_behind_downloads() -> None:
    limits = UserLimits(downloads=1, downloads_per_hour=10)
    async with limits.download("ann"):
        with anyio.fail_after(1):
            async with limits.download("ann", queue=False):  # the song being played
                pass


@pytest.mark.anyio
async def test_a_canceled_wait_never_keeps_a_download_turn() -> None:
    limits = UserLimits(downloads=1, downloads_per_hour=0)
    promoted = anyio.Event()
    with anyio.move_on_after(0.05):
        async with limits.download("ann", promoted=promoted):
            await anyio.sleep(1)  # canceled while holding it
    with anyio.fail_after(1):
        async with limits.download("ann"):
            pass


@pytest.mark.anyio
async def test_an_allowance_given_back_wakes_the_download_waiting_for_it() -> None:
    clock = Clock()
    limits = UserLimits(downloads=2, downloads_per_hour=1, clock=clock)
    limits.hour = 3600.0
    async with limits.download("ann") as give_back, anyio.create_task_group() as tg:
        went = anyio.Event()

        async def waiting() -> None:
            async with limits.download("ann"):
                went.set()

        tg.start_soon(waiting)
        await anyio.sleep(0.05)
        assert not went.is_set()  # it would wait half an hour
        give_back()  # the first needed no add-on after all
        with anyio.fail_after(1):
            await went.wait()


@pytest.mark.anyio
async def test_a_lookup_waiting_for_a_turn_that_the_client_plays_takes_a_play_s_turn() -> None:
    """A "now playing" report (or a saved queue) for a song whose lookup already waits
    for a turn takes it out of that wait - to one of the plays' turns, not past them."""
    limits = UserLimits(routings=1)
    went, other = anyio.Event(), anyio.Event()

    async def lookup(key: str, done: anyio.Event) -> None:
        async with limits.routing("ann", wait=5, key=key):
            done.set()
            await anyio.sleep(0.05)

    async with anyio.create_task_group() as tg:
        async with limits.routing("ann", key="held"):  # the user's one turn
            tg.start_soon(lookup, "next", went)
            tg.start_soon(lookup, "later", other)
            await anyio.sleep(0.05)
            assert not went.is_set() and not other.is_set()
            limits.promote("bob", "next")  # another user's report
            limits.promote("ann", "another song")
            await anyio.sleep(0.05)
            assert not went.is_set()
            limits.promote("ann", "next")  # the client plays it now
            with anyio.fail_after(1):
                await went.wait()
            assert not other.is_set()  # the others wait on
            assert limits._users["ann"].routings.value == 0  # type: ignore[union-attr]
        with anyio.fail_after(1):
            await other.wait()
    assert limits._awaited == {} and limits._users == {}


@pytest.mark.anyio
async def test_a_promoted_lookup_waits_among_the_plays_within_its_wait() -> None:
    limits = UserLimits(routings=1)
    async with (
        limits.routing("ann"),
        limits.routing("ann", queue=False),
        limits.routing("ann", queue=False),  # the plays' two turns
        anyio.create_task_group() as tg,
    ):
        failed: list[str] = []

        async def lookup() -> None:
            try:
                async with limits.routing("ann", wait=0.2, key="next"):
                    failed.append("got a turn")
            except Limited as exc:
                failed.append(exc.reason)

        tg.start_soon(lookup)
        await anyio.sleep(0.05)
        limits.promote("ann", "next")
    assert failed == ["no turn within 0.2s among the user's other lookups"]
    record = limits._users.get("ann")
    assert record is None and limits._awaited == {}  # every turn given back


@pytest.mark.anyio
async def test_a_canceled_wait_for_a_lookup_s_turn_keeps_nothing() -> None:
    limits = UserLimits(routings=1)
    async with limits.routing("ann"):
        with anyio.move_on_after(0.05):
            async with limits.routing("ann", key="next"):
                pytest.fail("no turn was free")
        assert limits._awaited == {}
    with anyio.fail_after(1):
        async with limits.routing("ann", key="next"), limits.routing("ann", queue=False):
            pass


@pytest.mark.anyio
async def test_a_cancellation_after_the_allowance_was_taken_gives_it_back() -> None:
    """A request canceled between taking from the hour's allowance and getting its turn
    (the client left): the allowance is not lost for the hour."""
    clock = Clock()
    limits = UserLimits(downloads=2, downloads_per_hour=3, clock=clock, sleep=clock.sleep)
    take = limits._take
    with anyio.CancelScope() as scope:

        async def take_then_cancel(record: object) -> None:
            await take(record)  # type: ignore[arg-type]
            scope.cancel()  # the request ends here, the allowance taken

        limits._take = take_then_cancel  # type: ignore[method-assign]
        async with limits.download("ann", promoted=anyio.Event()):
            pytest.fail("canceled before its turn")
    assert scope.cancelled_caught
    limits._take = take  # type: ignore[method-assign]
    async with limits.download("ann", promoted=anyio.Event()):
        record = limits._users["ann"]
        assert record.allowance == 2  # 3, less this one: the canceled one took nothing
        assert record.downloads is not None and record.downloads.value == 1
    assert clock.slept == []


class _CancelsOnceAcquired:
    """A user's turns, whose waiter's request ends at the moment it gets one."""

    def __init__(self, turns: anyio.Semaphore, scope: anyio.CancelScope) -> None:
        self.turns, self.scope = turns, scope

    async def acquire(self) -> None:
        await self.turns.acquire()
        self.scope.cancel()

    def release(self) -> None:
        self.turns.release()


@pytest.mark.anyio
async def test_a_turn_acquired_as_its_wait_is_canceled_is_given_back() -> None:
    """The request ends (its client left) at the moment its lookup gets a turn."""
    limits = UserLimits(routings=1)
    entered: list[bool] = []
    scope = anyio.CancelScope()
    async with anyio.create_task_group() as tg, limits.routing("ann"):
        record = limits._users["ann"]
        turns = record.routings
        assert turns is not None
        record.routings = _CancelsOnceAcquired(turns, scope)  # type: ignore[assignment]

        async def waiter() -> None:
            with scope:
                async with limits.routing("ann", key="next"):
                    entered.append(True)

        tg.start_soon(waiter)
        await anyio.sleep(0.05)
        record.routings = turns
        # The turn is free: the waiter gets it, and is canceled at once.
    assert entered == [] and scope.cancelled_caught and turns.value == 1
    assert limits._awaited == {}


@pytest.mark.anyio
async def test_a_wait_that_is_over_gets_no_turn_and_lookups_keep_their_order() -> None:
    limits = UserLimits(routings=1)
    with pytest.raises(Limited):
        async with limits.routing("ann", wait=0, key="late"):  # no time left: no turn
            pass
    order: list[str] = []

    async def lookup(key: str) -> None:
        async with limits.routing("ann", key=key):
            order.append(key)

    async with anyio.create_task_group() as tg:
        async with limits.routing("ann", key="held"):
            tg.start_soon(lookup, "first")
            await anyio.lowlevel.checkpoint()  # (registered, its wait not yet begun)
        tg.start_soon(lookup, "second")  # comes as the turn is free: not before the first
    assert order == ["first", "second"]


@pytest.mark.anyio
async def test_of_the_hour_s_allowance_only_a_few_start_at_once() -> None:
    """A first offline sync starts with a few downloads (the burst), then goes on at
    the hour's pace - and a quiet while saves up no more than the burst again."""
    clock = Clock()
    limits = UserLimits(
        downloads=1, downloads_per_hour=120, downloads_burst=4, clock=clock, sleep=clock.sleep
    )
    for _ in range(4):
        async with limits.download("ann"):
            pass
    assert clock.slept == []
    for _ in range(3):  # then one every 30 s
        async with limits.download("ann"):
            pass
    assert clock.slept == [pytest.approx(30)] * 3
    clock.now += 3600  # an hour of quiet: four at once again, not a hundred and twenty
    for _ in range(4):
        async with limits.download("ann"):
            pass
    assert len(clock.slept) == 3
    async with limits.download("ann"):
        pass
    assert clock.slept[-1] == pytest.approx(30)
    # What was not needed goes back, up to the burst and no further.
    clock.now += 3600
    async with limits.download("ann") as give_back:
        give_back()
    for _ in range(4):
        async with limits.download("ann"):
            pass
    assert len(clock.slept) == 4
    # The song being played never waits (it takes from the allowance all the same).
    async with limits.download("ann", queue=False):
        pass
    assert len(clock.slept) == 4
    # A burst above the hour's allowance is the hour's; 0: the whole hour's at once.
    assert UserLimits(downloads_per_hour=2, downloads_burst=4).most == 2
    assert UserLimits(downloads_per_hour=120, downloads_burst=0).most == 120
    assert UserLimits(downloads_per_hour=120, downloads_burst=4).most == 4
