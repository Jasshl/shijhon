"""The source registry's read-outs for the dashboard: cooldown ends, counters since the last
success, and every stored add-on (disabled ones too); the recent attempts kept across
restarts."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import anyio
import pytest
from pydantic import SecretStr

from shijhon.config import AddonSettings
from shijhon.delivery.addon import addon_key
from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.pacing import Limits
from shijhon.delivery.sources import (
    RECENT_ATTEMPTS,
    Attempt,
    NameTaken,
    SourceRegistry,
    StoredSource,
)
from shijhon.store import Store
from shijhon.store.db import apply_migrations, load_migrations


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
            StoredSource(
                two,
                "Two",
                "http://127.0.0.1:9/y",
                {},
                True,
                1,
                Reach.LOOPBACK,
                40,
                key=addon_key("Two"),
                step=2,  # (its step stays when only the order changes)
            ),
            StoredSource(
                one,
                "One",
                "https://one.example.invalid/x",
                {"quality": "6"},
                False,
                2,
                Reach.PUBLIC,
                None,
                key=addon_key("One"),
                step=1,
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


@pytest.mark.anyio
async def test_a_rename_keeps_the_addons_key(tmp_path: Path) -> None:
    """An add-on's key is given when it is added - the one its name makes - and a rename
    changes only its name. Names stay unique. An add-on added later under the first name
    is another add-on: another key, so it never gets the renamed one's catalog."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        two = await sources.add("Two", "https://two.example.invalid/x")
        assert [s.key for s in await sources.stored()] == [addon_key("One"), addon_key("Two")]
        written: list[str] = []

        async def also(conn: object) -> None:
            written.append("with the rename")

        await sources.update(one, name="Uno", also=also)  # type: ignore[arg-type]
        assert written == ["with the rename"]
        [uno] = [s for s in await sources.enabled() if s.id == one]
        assert (uno.name, uno.key) == ("Uno", addon_key("One"))
        await sources.stored()  # (the list read, as at startup: the keys by name)
        assert sources.key_of("Uno") == addon_key("One")
        with pytest.raises(NameTaken):
            await sources.update(one, name="Two")
        with pytest.raises(NameTaken):  # (nothing else of that save is written)
            await sources.update(one, name="Two", budget_seconds=9)
        assert [(s.name, s.budget) for s in await sources.stored()] == [
            ("Uno", None),
            ("Two", None),
        ]
        await sources.update(one, name="Uno")  # its own name: not another's
        again = await sources.add("One", "https://new.example.invalid/x")
        keys = {s.id: s.key for s in await sources.stored()}
        assert keys[again] == addon_key("One", 2) and keys[one] == addon_key("One")
        assert sources.key_of("One") == addon_key("One", 2)
        # Removed and added again under its name, an add-on gets its key back.
        await sources.remove(two)
        back = await sources.add("Two", "https://two.example.invalid/x")
        assert {s.id: s.key for s in await sources.stored()}[back] == addon_key("Two")
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_the_files_list_finds_an_addon_renamed_in_the_dashboard(tmp_path: Path) -> None:
    """Handed back to the configuration file, the list finds an add-on renamed in the
    dashboard by its key, and gives it the file's name again - the same add-on, so the same
    catalog."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)

    def listed(*names: str) -> list[AddonSettings]:
        return [
            AddonSettings(name=name, base_url=SecretStr(f"https://{n}.example.invalid/x"))
            for n, name in enumerate(names)
        ]

    try:
        await sources.sync(listed("Alpha", "Beta"))
        alpha = next(s for s in await sources.stored() if s.name == "Alpha")
        await sources.update(alpha.id, name="Renamed")
        await sources.sync(listed("Alpha", "Beta"))
        stored = await sources.stored()
        first = stored[0]
        assert (first.id, first.name, first.key, first.enabled) == (
            alpha.id,
            "Alpha",
            addon_key("Alpha"),
            True,
        )
        assert len(stored) == 2
        # Named by the file too, the renamed one is not taken for the file's other add-on.
        await sources.update(alpha.id, name="Gamma")
        await sources.sync(listed("Gamma", "Alpha"))
        stored = await sources.stored()
        assert [(s.name, s.enabled) for s in stored] == [
            ("Gamma", True),
            ("Alpha", True),
            ("Beta", False),
        ]
        assert stored[0].id == alpha.id and stored[1].key == addon_key("Alpha", 2)
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_startup_keeps_the_twin_that_owns_the_catalogs_ids(tmp_path: Path) -> None:
    """Of two stored add-ons with one name - the first off, the second on - migration 0032
    gives the name's key to the enabled one, and the file's list keeps that one too: the
    add-on whose catalog the stored IDs are stays on."""
    path = tmp_path / "state.sqlite3"
    folder = Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    setup = sqlite3.connect(path, isolation_level=None)
    apply_migrations(setup, load_migrations(folder)[:31])
    for enabled, position in ((0, 1), (1, 2)):
        setup.execute(
            "INSERT INTO sources (name, base_url, enabled, position, created_at)"
            " VALUES ('Twin', 'https://twin.example.invalid/x', ?, ?, 1)",
            (enabled, position),
        )
    setup.close()
    store = await Store.open(path)  # (migrated to the latest schema)
    sources = SourceRegistry(store)
    try:
        before = {s.id: s.key for s in await sources.stored()}
        assert before[2] == addon_key("Twin") and before[1] != addon_key("Twin")
        listed = [AddonSettings(name="Twin", base_url=SecretStr("https://twin.example.invalid/x"))]
        await sources.sync(listed)
        assert [(s.id, s.enabled) for s in await sources.stored()] == [(2, True), (1, False)]
        assert [s.key for s in await sources.enabled()] == [addon_key("Twin")]
        assert sources.key_of("Twin") == addon_key("Twin")
        assert await sources.differs(listed) == []
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_a_name_no_addon_has_is_no_addons_key(tmp_path: Path) -> None:
    """A name no stored add-on has resolves to nothing; the key an add-on added under it
    would get is one nobody holds - never that of the add-on renamed away from it."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        alpha = await sources.add("Alpha", "https://alpha.example.invalid/x")
        await sources.update(alpha, name="Beta")
        await sources.stored()
        assert sources.key_of("Beta") == addon_key("Alpha")
        assert sources.key_of("Alpha") is None
        vacant = sources.vacant_key("Alpha")
        assert vacant == addon_key("Alpha", 2) != sources.key_of("Beta")
        again = await sources.add("Alpha", "https://new.example.invalid/x")
        assert {s.id: s.key for s in await sources.stored()}[again] == vacant
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_the_files_list_finds_a_renamed_holder_past_a_duplicate(tmp_path: Path) -> None:
    """Two stored Alphas: the enabled one holds the name's key (migration 0032), the other
    has its own. The holder is renamed Beta; the file's list, naming Alpha, takes the holder
    back - by its key, not the duplicate by its name - and renames what named Beta with it."""
    path = tmp_path / "state.sqlite3"
    folder = Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    setup = sqlite3.connect(path, isolation_level=None)
    apply_migrations(setup, load_migrations(folder)[:31])
    for enabled, position in ((1, 1), (0, 2)):
        setup.execute(
            "INSERT INTO sources (name, base_url, enabled, position, created_at)"
            " VALUES ('Alpha', 'https://alpha.example.invalid/x', ?, ?, 1)",
            (enabled, position),
        )
    setup.close()
    store = await Store.open(path)
    sources = SourceRegistry(store)
    renames: list[tuple[str, str]] = []

    async def renamed(conn: object, old: str, new: str) -> None:
        renames.append((old, new))

    try:
        keys = {s.id: s.key for s in await sources.stored()}
        assert keys[1] == addon_key("Alpha") and keys[2] != addon_key("Alpha")
        await sources.update(1, name="Beta")
        listed = [
            AddonSettings(name="Alpha", base_url=SecretStr("https://alpha.example.invalid/x"))
        ]
        assert await sources.sync(listed, renamed=renamed) == [("Beta", "Alpha")]  # type: ignore[arg-type]
        assert renames == [("Beta", "Alpha")]
        stored = await sources.stored()
        assert [(s.id, s.name, s.enabled) for s in stored] == [
            (1, "Alpha", True),
            (2, "Alpha", False),
        ]
        assert [s.key for s in await sources.enabled()] == [addon_key("Alpha")]
        assert sources.key_of("Alpha") == addon_key("Alpha")
        assert await sources.sync(listed, renamed=renamed) == []  # (and again: no change)
        assert [(s.id, s.enabled) for s in await sources.stored()] == [(1, True), (2, False)]
    finally:
        await sources.aclose()
        await store.close()


async def _steps(sources: SourceRegistry) -> list[tuple[str, int, float | None]]:
    return [(s.name, s.step, s.step_seconds) for s in await sources.enabled()]


@pytest.mark.anyio
async def test_the_plan_is_stored_as_steps_with_their_times(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x", budget_seconds=60)
        two = await sources.add("Two", "https://two.example.invalid/x")
        three = await sources.add("Three", "https://three.example.invalid/x")
        assert await _steps(sources) == [("One", 1, None), ("Two", 2, None), ("Three", 3, None)]
        await sources.arrange([([three, one], 4.0), ([two], 7.5)])
        assert await _steps(sources) == [("Three", 1, 4.0), ("One", 1, 4.0), ("Two", 2, 7.5)]
        # The plan sets the steps' times; an add-on's time for a download stays its own.
        assert [s.budget for s in await sources.enabled()] == [None, 60, None]
        # An add-on turned off leaves the steps apart: they are numbers, not neighbours.
        await sources.arrange([([one], 4.0), ([three], 4.0), ([two], 4.0)])
        await sources.set_enabled(three, False)
        assert await _steps(sources) == [("One", 1, 4.0), ("Two", 3, 4.0)]
        for wrong in ([([one, one], 4.0)], [([one], 0.0)], [([12345], 4.0)]):
            with pytest.raises(ValueError):
                await sources.arrange(wrong)
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_add_ons_left_out_of_the_plan_keep_their_places(tmp_path: Path) -> None:
    """A plan of some add-ons (those asked for audio) puts them in the places they had
    among them; the others (a catalog only) stay where they are."""
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        one = await sources.add("One", "https://one.example.invalid/x")
        shelf = await sources.add("Shelf", "https://shelf.example.invalid/x")
        two = await sources.add("Two", "https://two.example.invalid/x")
        await sources.arrange([([two, one], 3.0)])
        assert [s.name for s in await sources.stored()] == ["Two", "Shelf", "One"]
        assert [s.step for s in await sources.stored()] == [1, 2, 1]
        await sources.remove(shelf)
        assert await _steps(sources) == [("Two", 1, 3.0), ("One", 1, 3.0)]
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_the_files_list_sets_the_steps(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        listed = [
            AddonSettings(name="One", base_url=SecretStr("https://one.example.invalid/x")),
            AddonSettings(
                name="Two", base_url=SecretStr("https://two.example.invalid/x"), with_previous=True
            ),
            AddonSettings(name="Three", base_url=SecretStr("https://three.example.invalid/x")),
        ]
        await sources.sync(listed)
        assert [(n, s) for n, s, _ in await _steps(sources)] == [
            ("One", 1),
            ("Two", 1),
            ("Three", 2),
        ]
        assert await sources.differs(listed) == []
        listed[1] = listed[1].model_copy(update={"with_previous": False})
        assert await sources.differs(listed) == ["the steps"]
    finally:
        await sources.aclose()
        await store.close()


@pytest.mark.anyio
async def test_the_files_list_sets_the_step_and_download_times_and_slow_after(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "state.sqlite3")
    sources = SourceRegistry(store)
    try:
        listed = [
            AddonSettings(
                name="One",
                base_url=SecretStr("https://one.example.invalid/x"),
                step_seconds=5.0,
                budget_seconds=120.0,
                slow_after=3.0,
            ),
            AddonSettings(name="Two", base_url=SecretStr("https://two.example.invalid/x")),
        ]
        await sources.sync(listed)
        assert [
            (s.name, s.step_seconds, s.budget, s.slow_after) for s in await sources.enabled()
        ] == [
            ("One", 5.0, 120.0, 3.0),
            ("Two", None, None, None),
        ]
        assert await sources.differs(listed) == []
        for change in ({"step_seconds": 6.0}, {"slow_after": None}):
            assert await sources.differs([listed[0].model_copy(update=change), listed[1]]) == [
                "One"
            ]
        await sources.sync([listed[0].model_copy(update={"slow_after": None}), listed[1]])
        assert (await sources.enabled())[0].slow_after is None
        with pytest.raises(ValueError):
            await sources.update(1, slow_after=0.5)
    finally:
        await sources.aclose()
        await store.close()
