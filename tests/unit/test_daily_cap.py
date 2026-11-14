"""The daily cap on adding (``placeholders/daily_cap.py``): who is counted, what counts, the
24 hours it counts over, a request's places taken in one step, places taken by writes
under way, how fresh an account's role is, and what a restart keeps."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest

from shijhon.app import ShijhonApp
from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.config import load_settings
from shijhon.placeholders.daily_cap import (
    DAY,
    REACHED,
    DailyCap,
    DailyLimitReached,
    Hold,
    adder,
    adding_for,
    adding_for_request,
    installation,
    reached,
)
from shijhon.placeholders.engine import MaterializeError
from shijhon.store import Store
from shijhon.views import commits as commits_module
from shijhon.views.commits import CommitError, Commits

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    opened = await Store.open(tmp_path / "cap.db")
    try:
        yield opened
    finally:
        await opened.close()


async def add(cap: DailyCap, ref: str) -> Hold | None:
    """One counted write from its place to its record, as the engine makes it."""
    hold = await cap.reserve(ref)
    try:
        async with cap.store.transaction() as conn:
            await cap.record(conn, hold)
    finally:
        cap.release(hold)
    return hold


async def rows(store: Store) -> list[tuple[str, str]]:
    found = await store.fetchall(
        "SELECT user_id, release_ref FROM release_additions ORDER BY user_id, release_ref"
    )
    return [(str(r["user_id"]), str(r["release_ref"])) for r in found]


async def test_without_a_cap_nothing_is_counted_or_kept(store: Store) -> None:
    cap = DailyCap(store)  # 0: no limit, the default
    with adding_for("lena", admin=False):
        for number in range(50):
            assert await add(cap, f"demo:{number}") is None
        await cap.check(f"demo:{n}" for n in range(500))
    assert await rows(store) == []
    assert await cap.standing("lena", admin=False) is None


async def test_an_account_adds_up_to_the_cap_and_no_further(store: Store) -> None:
    cap = DailyCap(store, limit=2)
    with adding_for("lena", admin=False):
        first = await add(cap, "demo:1")
        assert first is not None and (first.user_id, first.ref, first.recorded) == (
            "lena",
            "demo:1",
            True,
        )
        assert await add(cap, "demo:2") is not None
        with pytest.raises(DailyLimitReached) as refused:
            await cap.reserve("demo:3")
        assert refused.value.reason == REACHED and isinstance(refused.value, MaterializeError)
        with pytest.raises(DailyLimitReached):
            await cap.check(["demo:3"])
    assert await rows(store) == [("lena", "demo:1"), ("lena", "demo:2")]
    standing = await cap.standing("lena", admin=False)
    assert standing is not None
    assert (standing.limit, standing.used, standing.left) == (2, 2, 0)


async def test_each_account_has_its_own_count(store: Store) -> None:
    cap = DailyCap(store, limit=1)
    with adding_for("lena", admin=False):
        await add(cap, "demo:1")
    with adding_for("milo", admin=False):
        assert await add(cap, "demo:2") is not None
        with pytest.raises(DailyLimitReached):
            await cap.reserve("demo:3")


async def test_administrators_and_the_installations_own_work_are_not_counted(
    store: Store,
) -> None:
    cap = DailyCap(store, limit=1)
    with adding_for("root", admin=True):
        for number in range(5):
            assert await add(cap, f"demo:{number}") is None
    for number in range(5, 10):  # nobody named: the fill policy, the library pass
        assert await add(cap, f"demo:{number}") is None
    with adding_for(None):
        assert await add(cap, "demo:10") is None
    assert await rows(store) == []


async def test_an_accounts_role_is_read_when_a_place_is_taken_and_used_briefly(
    store: Store,
) -> None:
    """Not kept for an operation: an answer is used for ``role_seconds`` at most, then
    asked again - so an administrator demoted while a request waits, or between two
    releases of one publication, is counted from the next place on."""
    now = [100.0]
    admins = {"root"}
    asked: list[str] = []

    async def is_admin(user_id: str) -> bool:
        asked.append(user_id)
        return user_id in admins

    cap = DailyCap(store, limit=1, is_admin=is_admin, role_seconds=5.0, monotonic=lambda: now[0])
    with adding_for("root"):  # (its role not said: asked)
        assert await add(cap, "demo:1") is None
        now[0] += 4.9
        assert await add(cap, "demo:2") is None
        assert asked == ["root"]  # one answer, used for under five seconds
        admins.clear()  # demoted, between two releases of one publication
        now[0] += 0.2
        assert await add(cap, "demo:3") is not None  # asked again: counted now
        with pytest.raises(DailyLimitReached):
            await cap.reserve("demo:4")
        assert asked == ["root", "root"]
    assert await rows(store) == [("root", "demo:3")]


async def test_a_role_the_caller_just_read_is_not_trusted_for_long(store: Store) -> None:
    """An app's request names its account with the role just read for it (an
    administrator); the request then waits for the catalog, and the account is demoted
    meanwhile: the place is taken with the role as it is then."""
    admins = {"root"}

    async def is_admin(user_id: str) -> bool:
        return user_id in admins

    cap = DailyCap(store, limit=1, is_admin=is_admin, role_seconds=0.0)
    with adding_for("root", admin=True):
        async with cap.places(["demo:1", "demo:2"]) as taken:  # an administrator: no places
            assert taken.refused == () and not cap._held
            assert await add(cap, "demo:1") is None
            admins.clear()  # demoted during the wait for the second release
            assert await add(cap, "demo:2") is not None  # its own place, at the write
            with pytest.raises(DailyLimitReached):
                await cap.reserve("demo:3")
    assert await rows(store) == [("root", "demo:2")] and not cap._held


async def test_a_role_that_cannot_be_told_is_counted(store: Store) -> None:
    async def is_admin(user_id: str) -> bool:
        raise OSError("Navidrome is away")

    cap = DailyCap(store, limit=1, is_admin=is_admin)
    with adding_for("someone"):
        assert await add(cap, "demo:1") is not None
        with pytest.raises(DailyLimitReached):
            await cap.reserve("demo:2")
    assert await DailyCap(store, limit=1).standing("someone") is not None  # no way to ask


async def test_the_count_is_of_the_last_24_hours(store: Store) -> None:
    clock = Clock()
    cap = DailyCap(store, limit=2, clock=clock)
    with adding_for("lena", admin=False):
        await add(cap, "demo:1")
        clock.now += 6 * 3600
        await add(cap, "demo:2")
        standing = await cap.standing("lena", admin=False)
        assert standing is not None and standing.frees_at == clock.now - 6 * 3600 + DAY
        # No new day brings the allowance back at once: 23 hours after the first, still full.
        clock.now += 17 * 3600
        with pytest.raises(DailyLimitReached):
            await cap.reserve("demo:3")
        clock.now += 3600 + 1  # the first is 24 hours old: one place is free, not both
        assert await add(cap, "demo:3") is not None
        with pytest.raises(DailyLimitReached):
            await cap.reserve("demo:4")


async def test_a_release_added_back_within_24_hours_needs_no_second_place(store: Store) -> None:
    """Taken out and added back: within the 24 hours of the place the same account used
    for it, it needs none - at its cap either. After them it needs one like any other, so
    months of additions cannot all come back in one day. Another account always needs
    its own."""
    clock = Clock()
    cap = DailyCap(store, limit=1, clock=clock)
    with adding_for("lena", admin=False):
        await add(cap, "demo:1")
        assert await cap.reserve("demo:1") is None  # at the cap, and still not refused
        await cap.check(["demo:1"])
        async with cap.places(["demo:1"]) as taken:
            assert taken.refused == ()
        clock.now += DAY + 1  # its place is no longer counted: a new one is needed
        assert await add(cap, "demo:2") is not None
        with pytest.raises(DailyLimitReached):
            await cap.reserve("demo:1")
        with pytest.raises(DailyLimitReached):
            async with cap.places(["demo:1"]):
                pass
    with adding_for("milo", admin=False):
        assert await add(cap, "demo:1") is not None
    assert await rows(store) == [("lena", "demo:2"), ("milo", "demo:1")]


async def test_an_addition_older_than_24_hours_saves_no_place(store: Store) -> None:
    """Also while its row is still there (rows go at the next place taken): a look that
    takes nothing sees the release as one that needs a place."""
    clock = Clock()
    cap = DailyCap(store, limit=1, clock=clock)
    for ref, age in (("demo:old", 2 * DAY), ("demo:recent", 3600.0)):
        await store.execute(
            "INSERT INTO release_additions (user_id, release_ref, added_at) VALUES (?, ?, ?)",
            ["lena", ref, clock.now - age],
        )
    with adding_for("lena", admin=False):
        await cap.check(["demo:recent"])  # its place is still its own
        with pytest.raises(DailyLimitReached):
            await cap.check(["demo:old"])
        # ... and it takes no room either: with a cap of 2, one more release fits.
        await DailyCap(store, limit=2, clock=clock).check(["demo:new"])
    assert len(await rows(store)) == 2  # (a look removes nothing)


async def test_additions_older_than_24_hours_are_not_kept(store: Store) -> None:
    clock = Clock()
    cap = DailyCap(store, limit=5, clock=clock)
    with adding_for("lena", admin=False):
        await add(cap, "demo:1")
        clock.now += 12 * 3600
        await add(cap, "demo:2")
        assert len(await rows(store)) == 2
        clock.now += 12 * 3600 + 1  # the first is 24 hours old: gone at the next place
        await add(cap, "demo:3")
        assert await rows(store) == [("lena", "demo:2"), ("lena", "demo:3")]
        clock.now += 2 * DAY
        await cap.prune()  # as at every start
        assert await rows(store) == []
        # Added again after that, a release is counted anew (no old row in its way).
        await add(cap, "demo:1")
        assert await rows(store) == [("lena", "demo:1")]


async def test_a_look_counts_all_the_releases_of_a_request(store: Store) -> None:
    cap = DailyCap(store, limit=3)
    with adding_for("lena", admin=False):
        await add(cap, "demo:1")
        await cap.check(["demo:1", "demo:2", "demo:3"])  # two new ones: room for both
        with pytest.raises(DailyLimitReached):
            await cap.check(["demo:2", "demo:3", "demo:4"])
    assert await rows(store) == [("lena", "demo:1")]  # a look takes nothing


async def test_a_request_takes_all_its_places_in_one_step_or_none(store: Store) -> None:
    """A request for several releases: its places are taken together before the first
    write, so nothing another request does meanwhile can leave it half done; one that
    does not fit takes none. Places no write used are given back at its end."""
    cap = DailyCap(store, limit=3)
    with adding_for("lena", admin=False):
        await add(cap, "demo:0")
        async with cap.places(["demo:1", "demo:2"]) as taken:
            assert taken.refused == () and cap._held["lena"] == 2
            # A competing addition right after: no room is left for it ...
            with pytest.raises(DailyLimitReached):
                await cap.reserve("demo:9")
            with pytest.raises(DailyLimitReached):
                async with cap.places(["demo:8", "demo:9"]):
                    pass
            # ... and the request's own writes find their places.
            assert await add(cap, "demo:1") is not None
            assert await add(cap, "demo:2") is not None
            assert not cap._held  # counted in the database now, not twice
        assert await rows(store) == [("lena", f"demo:{n}") for n in (0, 1, 2)]
    assert not cap._held


async def test_a_request_that_does_not_fit_takes_nothing(store: Store) -> None:
    cap = DailyCap(store, limit=2)
    with adding_for("lena", admin=False):
        await add(cap, "demo:0")
        with pytest.raises(DailyLimitReached):
            async with cap.places(["demo:1", "demo:2"]):
                raise AssertionError("refused before it began")
        assert not cap._held and await rows(store) == [("lena", "demo:0")]
        assert await add(cap, "demo:3") is not None  # the room is still there


async def test_places_a_write_did_not_use_are_given_back(store: Store) -> None:
    """A write that fails gives its place back to its request (for another try); the
    request's end frees what no write used, also when it ends with an error."""
    cap = DailyCap(store, limit=2)
    with adding_for("lena", admin=False):
        with pytest.raises(RuntimeError):
            async with cap.places(["demo:1", "demo:2"]):
                hold = await cap.reserve("demo:1")
                assert hold is not None and hold.of is not None
                cap.release(hold)  # its write failed: nothing recorded
                assert cap._held["lena"] == 2  # still the request's, both
                again = await cap.reserve("demo:1")  # tried again within the request
                assert again is not None
                async with store.transaction() as conn:
                    await cap.record(conn, again)
                cap.release(again)
                raise RuntimeError("the request stopped before its second release")
        assert not cap._held and await rows(store) == [("lena", "demo:1")]
        assert await add(cap, "demo:3") is not None  # the place given back is used


async def test_a_playlist_takes_places_in_the_order_of_its_songs(store: Store) -> None:
    cap = DailyCap(store, limit=3)
    with adding_for("lena", admin=False):
        await add(cap, "demo:0")
        wanted = ["demo:5", "demo:0", "demo:4", "demo:5", "demo:3", "demo:2"]
        async with cap.places(wanted, partial=True) as taken:
            # Counted already: no place. Then two places, for the first two new ones.
            assert taken.refused == ("demo:3", "demo:2")
            assert await add(cap, "demo:5") is not None and await add(cap, "demo:4") is not None
            with pytest.raises(DailyLimitReached):
                await cap.reserve("demo:3")
    assert [ref for _, ref in await rows(store)] == ["demo:0", "demo:4", "demo:5"]


async def test_places_taken_before_the_handlers_are_used_by_them(store: Store) -> None:
    """A request whose old IDs took its places (kept with the task, given back when its
    handlers are done): the commit's own reservation uses those and takes only what is
    missing - all of it, or nothing more."""
    cap = DailyCap(store, limit=3)
    with adding_for("lena", admin=False):
        batch = await cap.reserve_all(["demo:1", "demo:2"])
        cap.keep(batch)
        async with cap.places(["demo:2", "demo:3"]) as taken:
            assert taken.refused == () and cap._held["lena"] == 3
        assert cap._held["lena"] == 3  # the request is not over
        with pytest.raises(DailyLimitReached):
            async with cap.places(["demo:4"]):
                pass
        assert await add(cap, "demo:1") is not None
        cap.give_back(batch)
        assert not cap._held and batch.closed
        async with cap.places(["demo:4", "demo:5"]):  # a closed one is nobody's
            assert cap._held["lena"] == 2
    assert await rows(store) == [("lena", "demo:1")]


async def test_requests_at_once_each_fit_whole_or_not_at_all(store: Store) -> None:
    cap = DailyCap(store, limit=5)
    outcomes: list[int] = []

    async def request(number: int) -> None:
        refs = [f"demo:{number}-{n}" for n in range(2)]
        try:
            async with cap.places(refs):
                for ref in refs:
                    await anyio.sleep(0.005)
                    assert await add(cap, ref) is not None
        except DailyLimitReached:
            outcomes.append(0)
        else:
            outcomes.append(2)

    with adding_for("lena", admin=False):
        async with anyio.create_task_group() as group:
            for number in range(10):
                group.start_soon(request, number)
    assert sorted(outcomes) == [0] * 8 + [2] * 2  # two whole requests: 4 of 5 places
    assert len(await rows(store)) == 4 and not cap._held


async def test_writes_under_way_hold_their_places(store: Store) -> None:
    """Two writes at once for an account with one place left: the second is refused while
    the first is still writing, and gets the place when the first fails."""
    cap = DailyCap(store, limit=1)
    with adding_for("lena", admin=False):
        first = await cap.reserve("demo:1")
        assert first is not None
        with pytest.raises(DailyLimitReached):
            await cap.reserve("demo:2")
        with pytest.raises(DailyLimitReached):
            await cap.check(["demo:2"])
        cap.release(first)  # its write failed: nothing recorded
        second = await cap.reserve("demo:2")
        assert second is not None
        cap.release(second)
    assert await rows(store) == [] and not cap._held


async def test_many_writes_at_once_never_pass_the_cap(store: Store) -> None:
    cap = DailyCap(store, limit=3)
    outcomes: list[bool] = []

    async def one(number: int) -> None:
        try:
            hold = await cap.reserve(f"demo:{number}")
        except DailyLimitReached:
            outcomes.append(False)
            return
        try:
            await anyio.sleep(0.01)  # the write, with the others asking meanwhile
            async with store.transaction() as conn:
                await cap.record(conn, hold)
        finally:
            cap.release(hold)
        outcomes.append(True)

    with adding_for("lena", admin=False):
        async with anyio.create_task_group() as group:
            for number in range(20):
                group.start_soon(one, number)
    assert outcomes.count(True) == 3 and len(await rows(store)) == 3


async def test_the_count_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "kept.db"
    store = await Store.open(path)
    try:
        with adding_for("lena", admin=False):
            await add(DailyCap(store, limit=1), "demo:1")
    finally:
        await store.close()
    store = await Store.open(path)
    try:
        cap = DailyCap(store, limit=1)  # as after a restart: nothing kept in memory
        with adding_for("lena", admin=False), pytest.raises(DailyLimitReached):
            await cap.reserve("demo:2")
    finally:
        await store.close()


async def test_tasks_started_in_a_block_add_for_its_account() -> None:
    seen: list[str | None] = []

    async def child() -> None:
        who = adder()
        seen.append(who.user_id if who else None)

    with adding_for("lena", admin=False):
        async with anyio.create_task_group() as group:
            group.start_soon(child)
    assert seen == ["lena"] and adder() is None


async def test_background_work_never_adds_for_the_request_that_started_it(
    tmp_path: Path,
) -> None:
    """A background task starts as the installation's own work, whoever's request spawned
    it: a fill it makes later is not counted for (or refused to) that person."""
    settings = load_settings(
        None,
        state_dir=tmp_path / "state",
        navidrome={"url": "http://127.0.0.1:9", "library_path": tmp_path},
    )
    app = ShijhonApp(settings)
    seen: list[object] = []

    async def work() -> None:
        seen.append(adder())

    async with anyio.create_task_group() as group:
        app._background = group
        with adding_for("lena", admin=False):
            app.spawn(work)
    assert seen == [None]
    with adding_for("lena", admin=False):
        installation()
        assert adder() is None


async def test_a_requests_account_is_the_one_its_library_access_was_read_for() -> None:
    class Principal:
        user_id, is_admin = "id-1", True

    with adding_for_request({"shijhon.principal": Principal()}, "root"):
        who = adder()
        assert who is not None and (who.user_id, who.admin) == ("id-1", True)
    with adding_for_request({}, "lena"):  # not read: counted under its name
        who = adder()
        assert who is not None and (who.user_id, who.admin) == ("lena", False)


async def test_the_refusal_is_known_wherever_it_is_passed_on() -> None:
    assert reached(DailyLimitReached())
    assert reached(MaterializeError(REACHED))  # passed on as a plain error
    assert reached(CommitError(0, "any words", category="daily_limit"))
    assert reached(ExceptionGroup("tasks", [ValueError("x"), DailyLimitReached()]))
    assert not reached(MaterializeError("the owned album has no owned songs"))
    assert not reached(CommitError(0, "could not add the catalog album"))
    assert not reached(ExceptionGroup("tasks", [ValueError("x")]))


async def test_what_a_request_would_add_is_looked_up_as_its_commit_is() -> None:
    """With a cap set, the releases a request would add are told before its first write:
    its catalog items are looked up a few at a time (as committing them is), not one after
    another, and told in the items' order, each release once."""
    at_once = most = 0

    async def song(ident: str) -> CatalogTrack:
        nonlocal at_once, most
        at_once += 1
        most = max(most, at_once)
        await anyio.sleep(0.01)
        at_once -= 1
        album = "shared" if ident in ("s1", "s2") else f"album-{ident}"
        return CatalogTrack(
            CatalogRef("demo", ident), ident, "A", 1000, album=CatalogRef("demo", album)
        )

    async def nothing(ref: Any) -> None:
        return None

    catalog = SimpleNamespace(key="demo", song=song)
    commits = Commits(
        cast(Any, catalog),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(song=nothing, album=nothing)),
    )
    texts = [f"sh.tr.demo.s{n}" for n in range(1, 9)]
    new = await commits.new_releases(texts)
    assert new == ["demo:shared", *(f"demo:album-s{n}" for n in range(3, 9))]
    assert most == commits_module._PARALLEL
