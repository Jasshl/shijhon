"""The source registry's read-outs for the dashboard: cooldown ends, counters since the last
success, and every stored add-on (disabled ones too); the recent attempts kept across
restarts."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import anyio
import pytest

from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.pacing import Limits
from shijhon.delivery.sources import RECENT_ATTEMPTS, Attempt, SourceRegistry, StoredSource
from shijhon.store import Store


@pytest.mark.anyio
async def test_cooling_until_is_wall_clock_and_ends(tmp_path: Path) -> None:
    now = [100.0]
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store, clock=lambda: now[0])
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        assert sources.cooling_until(one) is None
        sources.cool_down(one, 30)
        until = sources.cooling_until(one)
        assert until is not None and abs(until - (time.time() + 30)) < 1
        now[0] += 31
        assert sources.cooling_until(one) is None and not sources.cooling(one)
        assert sources.cooling_until(12345) is None  # no such source
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_stats_count_failures_since_the_last_success(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        assert sources.stats(one) is None  # not used yet
        (source,) = await sources.enabled()
        sources.failed(source, "timeout")
        sources.failed(source, "add-on error (HTTP 502)")
        stats = sources.stats(one)
        assert stats is not None and stats.failures_since_success == 2
        assert stats.last_success_at is None and stats.last_failure == "add-on error (HTTP 502)"
        before = time.time()
        sources.succeeded(source, 0.3)
        assert stats.failures_since_success == 0 and stats.successes == 1
        assert stats.last_success_at is not None and stats.last_success_at >= before
        await sources.invalidate()  # rebuilt sources keep their counters
        (again,) = await sources.enabled()
        assert again.stats is stats
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_stored_lists_every_addon_in_order(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x", {"quality": "6"})
        two = await sources.add(
            "Two", "http://127.0.0.1:9/y", reach=Reach.LOOPBACK, budget_seconds=40
        )
        await sources.set_enabled(one, False)
        await sources.reorder([two, one])
        listed = await sources.stored()
        assert listed == [
            StoredSource(two, "Two", "http://127.0.0.1:9/y", {}, True, 1, Reach.LOOPBACK, 40),
            StoredSource(
                one,
                "One",
                "https://one.example.invalid/x",
                {"quality": "6"},
                False,
                2,
                Reach.PUBLIC,
                None,
            ),
        ]
        assert [s.name for s in await sources.enabled()] == ["Two"]
        assert "one.example.invalid" not in repr(listed[1])  # the URL may hold a key
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_a_changed_or_reenabled_addon_starts_a_fresh_count(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        (source,) = await sources.enabled()
        for _ in range(3):
            sources.failed(source, "timeout")
        stats = sources.stats(one)
        assert stats is not None and stats.failures_since_success == 3
        await sources.update(one, budget_seconds=5)  # not the add-on itself
        assert stats.failures_since_success == 3
        stats.errors_since_success = 3
        sources.cool_down(one, 60)
        await sources.update(one, base_url="https://one.example.invalid/new-key")
        assert stats.failures_since_success == 0 and stats.failures == 3
        assert stats.errors_since_success == 0 and not sources.cooling(one)  # its errors too
        sources.failed(source, "timeout")
        await sources.set_enabled(one, False)
        assert stats.failures_since_success == 1
        await sources.set_enabled(one, True)
        assert stats.failures_since_success == 0
    finally:
        await sources.aclose()
        await store.close()


# --- the fallbacks' measurements, kept across restarts ----------------------------


@pytest.mark.anyio
async def test_recent_attempts_are_kept_across_a_restart(tmp_path: Path) -> None:
    """Each source's recent attempts (the fallbacks' order by measurement) are written
    to the database and read back at the next start, on the new process's clock: their age
    is kept, what the check said, whether the audio came and the seconds."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        two = await sources.add("Two", "https://two.example.invalid/y")
        first, second = await sources.enabled()
        sources.attempted(first, Attempt(time.monotonic(), "ready", True, 2.5))
        sources.attempted(first, Attempt(time.monotonic(), "-", False, 9.0))
        sources.attempted(second, Attempt(time.monotonic(), "not now", True, 30.0))
    finally:
        await sources.aclose()  # a stop writes what is not written yet
        await store.close()
    store = await Store.open(tmp_path / "state.sqlite3")
    restarted = SourceRegistry(store)
    try:
        clock = [5000.0]  # another process's clock
        await restarted.load_attempts(lambda: clock[0])
        stats_one, stats_two = restarted.stats(one), restarted.stats(two)
        assert stats_one is not None and stats_two is not None
        assert [(a.answer, a.delivered, a.seconds) for a in stats_one.recent] == [
            ("ready", True, 2.5),
            ("-", False, 9.0),
        ]
        assert [(a.answer, a.delivered, a.seconds) for a in stats_two.recent] == [
            ("not now", True, 30.0)
        ]
        assert all(4990 < a.at <= 5000 for a in stats_one.recent)  # a moment ago
        (first, second) = await restarted.enabled()
        assert first.stats is stats_one  # the sources use them
    finally:
        await restarted.aclose()
        await store.close()


@pytest.mark.anyio
async def test_only_the_last_attempts_are_kept(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        (source,) = await sources.enabled()
        for n in range(RECENT_ATTEMPTS + 7):
            sources.attempted(source, Attempt(time.monotonic(), "-", True, float(n)))
        await sources.save_attempts()
        row = await store.fetchone("SELECT COUNT(*) AS n FROM source_attempts")
        assert row is not None and row["n"] == RECENT_ATTEMPTS
        restarted = SourceRegistry(store)
        await restarted.load_attempts(time.monotonic)
        stats = restarted.stats(one)
        assert stats is not None
        assert [a.seconds for a in stats.recent] == [
            float(n) for n in range(7, RECENT_ATTEMPTS + 7)
        ]
        await restarted.aclose()
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_attempts_older_than_a_week_are_neither_loaded_nor_kept(tmp_path: Path) -> None:
    """Also those of a source that has had no attempt since (the next write takes them
    out); a wall clock that was ahead then makes none newer than now."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        two = await sources.add("Two", "https://two.example.invalid/y")
        (first, _) = await sources.enabled()
        sources.attempted(first, Attempt(time.monotonic(), "-", True, 1.0))
        await sources.save_attempts()
        for source_id, ago, seconds in ((one, 8 * 86400, 99.0), (two, 8 * 86400, 98.0),
                                        (two, -3600, 7.0)):  # fmt: skip
            await store.execute(
                "INSERT INTO source_attempts (source_id, at, answer, delivered, seconds)"
                " VALUES (?, ?, '-', 1, ?)",
                [source_id, time.time() - ago, seconds],
            )
        restarted = SourceRegistry(store)
        clock = [100.0]
        await restarted.load_attempts(lambda: clock[0])
        stats_one, stats_two = restarted.stats(one), restarted.stats(two)
        assert stats_one is not None and [a.seconds for a in stats_one.recent] == [1.0]
        assert stats_two is not None and [a.seconds for a in stats_two.recent] == [7.0]
        assert stats_two.recent[0].at == 100.0  # an hour ahead: now, not later
        await restarted.save_attempts()  # nothing new: the old ones go all the same
        rows = await store.fetchall("SELECT seconds FROM source_attempts ORDER BY id")
        assert [r["seconds"] for r in rows] == [1.0, 7.0]
        await restarted.aclose()
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_attempts_recorded_before_the_load_come_after_the_saved_ones(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        (source,) = await sources.enabled()
        sources.attempted(source, Attempt(time.monotonic(), "-", True, 1.0))
        await sources.save_attempts()
        restarted = SourceRegistry(store)
        (again,) = await restarted.enabled()
        restarted.attempted(again, Attempt(time.monotonic(), "ready", True, 2.0))
        await restarted.load_attempts(time.monotonic)
        stats = restarted.stats(one)
        assert stats is not None and [a.seconds for a in stats.recent] == [1.0, 2.0]
        await restarted.aclose()
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_a_canceled_write_writes_its_attempts_once(tmp_path: Path) -> None:
    """The stop cancels the minute's write wherever it is (a queued BEGIN or COMMIT still
    runs): the attempts are written once all the same, and the stop's own write works."""
    for steps in range(16):
        store = await Store.open(tmp_path / f"state-{steps}.sqlite3")
        sources = SourceRegistry(store)
        try:
            await sources.add("One", "https://one.example.invalid/x")
            (source,) = await sources.enabled()
            sources.attempted(source, Attempt(time.monotonic(), "-", True, 1.0))
            with anyio.fail_after(10):  # a transaction left open would hang the next write
                async with anyio.create_task_group() as group:
                    group.start_soon(sources.save_attempts)
                    await anyio.sleep(steps * 0.0004)  # another moment of the write each time
                    group.cancel_scope.cancel()
                sources.attempted(source, Attempt(time.monotonic(), "-", True, 2.0))
                await sources.aclose()  # the stop's write
        finally:
            await sources.aclose()
        rows = await store.fetchall("SELECT seconds FROM source_attempts ORDER BY id")
        assert [r["seconds"] for r in rows] == [1.0, 2.0], steps
        await store.close()


@pytest.mark.anyio
async def test_a_removed_source_s_attempts_go_with_it(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        two = await sources.add("Two", "https://two.example.invalid/y")
        first, second = await sources.enabled()
        sources.attempted(first, Attempt(time.monotonic(), "-", True, 1.0))
        await sources.save_attempts()
        sources.attempted(first, Attempt(time.monotonic(), "-", True, 2.0))
        sources.attempted(second, Attempt(time.monotonic(), "-", True, 3.0))
        await sources.remove(one)  # its unwritten attempt is dropped, its written ones go
        await sources.save_attempts()
        rows = await store.fetchall("SELECT source_id, seconds FROM source_attempts")
        assert [(r["source_id"], r["seconds"]) for r in rows] == [(two, 3.0)]
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_attempts_a_write_failed_to_save_are_written_next_time(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        await sources.add("One", "https://one.example.invalid/x")
        (source,) = await sources.enabled()
        sources.attempted(source, Attempt(time.monotonic(), "-", True, 1.0))
        await store.execute("ALTER TABLE source_attempts RENAME TO elsewhere")
        with pytest.raises(sqlite3.Error):
            await sources.save_attempts()
        await store.execute("ALTER TABLE elsewhere RENAME TO source_attempts")
        sources.attempted(source, Attempt(time.monotonic(), "-", True, 2.0))
        await sources.save_attempts()
        rows = await store.fetchall("SELECT seconds FROM source_attempts ORDER BY id")
        assert [r["seconds"] for r in rows] == [1.0, 2.0]
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_an_addons_own_limits_are_stored_and_its_origins_limits_shared(
    tmp_path: Path,
) -> None:
    """What one add-on is sent in all - the installation's limits, or the add-on's
    own; add-ons at one origin share one count, with the stricter of their limits."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store, limits=Limits(2.0, 4, 4))
    try:
        one = await sources.add("One", "https://one.example.invalid/a?key=1")
        twin = await sources.add(
            "Twin", "https://one.example.invalid/b", limits=Limits(10.0, 8, None)
        )
        own = await sources.add(
            "Own", "http://127.0.0.1:9/y", reach=Reach.LOOPBACK, limits=Limits(0, 20, 0)
        )
        stored = {s.name: s.limits for s in await sources.stored()}
        assert stored == {"One": Limits(), "Twin": Limits(10.0, 8, None), "Own": Limits(0, 20, 0)}
        by_name = {s.name: s for s in await sources.enabled()}
        shared = by_name["One"].pace
        assert shared is not None and shared is by_name["Twin"].pace
        assert shared is by_name["One"].addon.pace  # its API requests take their turn there
        assert shared.limits == Limits(2.0, 4, 4)  # the stricter of the two entries
        assert by_name["Own"].pace is not shared
        assert by_name["Own"].pace is not None and by_name["Own"].pace.limits == Limits(0, 20, 0)

        # The stricter entry given more: the origin's limits follow; its count goes on.
        await shared.request()
        await sources.update(one, limits=Limits(50.0, 30, 6))
        by_name = {s.name: s for s in await sources.enabled()}
        assert by_name["One"].pace is shared and shared.sent == 1
        assert shared.limits == Limits(10.0, 8, 4)
        # An entry switched off no longer counts for its origin's limits.
        await sources.set_enabled(twin, False)
        await sources.enabled()
        assert shared.limits == Limits(50.0, 30, 6)

        # The installation's limits changed (the dashboard): at once, where none are set.
        await sources.update(one, limits=Limits())
        await sources.enabled()
        sources.limit(Limits(5.0, 6, 2))
        assert shared.limits == Limits(5.0, 6, 2)
        assert sources.paces.of("http://127.0.0.1:9/elsewhere") is by_name["Own"].pace
        assert by_name["Own"].pace.limits == Limits(0, 20, 0)

        for bad in (Limits(-1.0), Limits(2000.0), Limits(None, 0), Limits(None, None, -1)):
            with pytest.raises(ValueError):
                await sources.update(own, limits=bad)
            with pytest.raises(ValueError):
                await sources.add("Bad", "https://bad.example.invalid/", limits=bad)
        assert [s.name for s in await sources.stored()] == ["One", "Twin", "Own"]
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_a_cooldown_is_never_shortened_and_a_rate_limit_leaves_the_origin_alone(
    tmp_path: Path,
) -> None:
    """A new cooldown extends the one under way; after "too many requests" nothing is
    sent to the add-on's origin - whichever entry was asked - until the time it named."""
    now = [100.0]
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store, clock=lambda: now[0])
    sources.paces.clock = lambda: now[0]
    try:
        one = await sources.add("One", "https://one.example.invalid/a")
        twin = await sources.add("Twin", "https://one.example.invalid/b?key=2")
        other = await sources.add("Other", "https://other.example.invalid/")
        by_name = {s.name: s for s in await sources.enabled()}
        sources.cool_down(one, 60)
        sources.cool_down(one, 5)  # a shorter one later: the first still stands
        until = sources.cooling_until(one)
        assert until is not None and abs(until - (time.time() + 60)) < 1
        now[0] += 30
        sources.cool_down(one, 45)  # longer than what is left: extended
        until = sources.cooling_until(one)
        assert until is not None and abs(until - (time.time() + 45)) < 1
        assert not sources.cooling(twin) and not sources.cooling(other)
        now[0] += 46
        assert not sources.cooling(one)

        # A rate limit at one entry: its origin is left alone, the other entry there too.
        assert sources.limited(by_name["Twin"], 120) == pytest.approx(120)
        assert sources.cooling(one) and sources.cooling(twin) and not sources.cooling(other)
        until = sources.cooling_until(one)
        assert until is not None and abs(until - (time.time() + 120)) < 1
        pace = by_name["One"].pace
        assert pace is not None and pace.blocked == pytest.approx(120)
        assert sources.limited(by_name["One"], 10) == pytest.approx(120)  # never shorter
        # An add-on changed or switched on again starts afresh - but what its origin asked
        # for still holds.
        sources.cool_down(one, 500)
        await sources.update(one, base_url="https://one.example.invalid/a?key=new")
        until = sources.cooling_until(one)
        assert until is not None and abs(until - (time.time() + 120)) < 1
        now[0] += 121
        assert not sources.cooling(one) and not sources.cooling(twin)
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_an_addons_own_limits_apply_before_anything_was_played(tmp_path: Path) -> None:
    """The dashboard asks for an address's limits (its manifest checks): the add-ons' own
    limits are read with the list first - also before the first play, and after an edit."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store, limits=Limits(2.0, 4, 4))
    try:
        one = await sources.add("One", "https://one.example.invalid/a", limits=Limits(0.1, 1, None))
        pace = await sources.pace_at("https://one.example.invalid/a/manifest.json")
        assert pace is not None and pace.limits == Limits(0.1, 1, 4)
        await sources.update(one, limits=Limits(0.05, None, None))  # edited: not yet read
        again = await sources.pace_at("https://one.example.invalid/elsewhere")
        assert again is pace and pace.limits == Limits(0.05, 4, 4)
        assert await sources.pace_at("not an address") is None
    finally:
        await sources.aclose()
        await store.close()
