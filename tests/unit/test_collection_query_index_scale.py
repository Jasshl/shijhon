"""What keeps library lists fast as a library grows, and exact while they are."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.collection.contracts import REAL_AUDIO
from shijhon.collection.policy import (
    builtin_profile_id,
    chosen_albums,
    original_albums,
    shijhon_albums,
)
from shijhon.collection.query_index import Binding, Budgets, IndexPending, QueryIndex, SourcePage
from shijhon.store import apply_migrations

LIBRARY = "1"
GENERATION = "gen"
OWNER = "one"
BINDING = Binding(OWNER, frozenset({LIBRARY}), ((LIBRARY, GENERATION),), "grants")


def track(n: int, *, album: int, artist: int, original: bool) -> tuple[Any, ...]:
    metadata = {
        "albumArtistId": f"ar{artist}",
        "genres": [{"id": f"g{album % 3}", "name": f"Genre {album % 3}"}],
        "participants": {
            "albumartist": [{"id": f"ar{artist}"}],
            "artist": [{"id": f"ar{artist}"}, {"id": f"ar{(artist + 1) % 7}"}],
        },
    }
    return (
        LIBRARY,
        f"k{n}",
        f"s{n}",
        f"release{album}",
        f"al{album}",
        f"ar{artist}",
        f"Song {n}",
        f"Album {album}",
        f"Artist {artist}",
        f"song {n}",
        100.5 + n,
        1000 + n,
        2000 + album,
        json.dumps(metadata),
        int(original),
        1,
        GENERATION,
        1,
    )


def library(path: Path, songs: int = 90, *, seed: int = 11) -> dict[str, list[str]]:
    """An invented library in which every rule of the policy holds a few albums."""
    rnd = random.Random(seed)
    with closing(sqlite3.connect(path)) as conn, conn:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO collection_library_policy VALUES (?, 'personal', 1, NULL)", (LIBRARY,)
        )
        conn.execute(
            "INSERT INTO collection_index VALUES (?, ?, 'ready', ?, 1, '')",
            (LIBRARY, GENERATION, songs),
        )
        tracks = [
            track(n, album=n // 5, artist=(n // 5) % 7, original=(n // 5) % 3 == 0)
            for n in range(songs)
        ]
        conn.executemany(
            "INSERT INTO collection_tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tracks
        )
        for t in tracks:
            if t[14] and rnd.random() < 0.7:
                conn.execute(
                    "INSERT INTO collection_originals VALUES (?,?,?,'import',1)",
                    (OWNER, LIBRARY, t[1]),
                )
            if not t[14] and rnd.random() < 0.1:
                conn.execute(
                    "INSERT INTO collection_history(owner_id,library_id,track_key,added_at,"
                    "activity) VALUES (?,?,?,1,1)",
                    (OWNER, LIBRARY, t[1]),
                )
        conn.execute(
            "INSERT INTO collection_favorites VALUES (?,?,'album','al4')", (OWNER, LIBRARY)
        )
        conn.execute("INSERT INTO collection_favorites VALUES (?,?,'song','s36')", (OWNER, LIBRARY))
        conn.execute(
            "INSERT INTO collection_profiles(id,owner_id,kind,name,base_original,created)"
            " VALUES ('custom-one',?,'custom','Hand picked',0,1)",
            (OWNER,),
        )
        for key, saved in (("k50", 1), ("k51", 0), ("k1", 0)):
            conn.execute(
                "INSERT INTO collection_membership(owner_id,profile_id,library_id,track_key,"
                "saved,revision,last_operation) VALUES (?,'custom-one',?,?,?,1,'')",
                (OWNER, LIBRARY, key, saved),
            )
        for key, saved in (("k55", 1), ("k0", 0)):
            conn.execute(
                "INSERT INTO collection_membership(owner_id,profile_id,library_id,track_key,"
                "saved,revision,last_operation) VALUES (?,?,?,?,?,1,'')",
                (OWNER, builtin_profile_id(OWNER, "all"), LIBRARY, key, saved),
            )
    return {
        "song": [f"s{n}" for n in range(songs)],
        "album": [f"al{n}" for n in range((songs + 4) // 5)],
        "artist": [f"ar{n}" for n in range(7)],
    }


def rows_of(kind: str, idents: list[str]) -> list[dict[str, Any]]:
    if kind == "song":
        return [{"id": i, "title": i} for i in idents]
    if kind == "album":
        return [{"id": i, "name": i, "songCount": 5, "duration": 500, "size": 9} for i in idents]
    return [{"id": i, "name": i, "albumCount": 99, "stats": {}, "size": 9} for i in idents]


def source(rows: list[dict[str, Any]]) -> Callable[[int, int], Any]:
    async def fetch(start: int, limit: int) -> SourcePage:
        return SourcePage(rows[start : start + limit], len(rows))

    return fetch


async def valid() -> None:
    pass


def count_statements(index: QueryIndex, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement the index's connections run, as they run it."""
    statements: list[str] = []
    connect = index._connect

    def traced() -> sqlite3.Connection:
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(index, "_connect", traced)
    return statements


async def built(index: QueryIndex) -> None:
    with anyio.fail_after(5):
        for event, _ in list(index.jobs.values()):
            await event.wait()


def edit(path: Path, statement: str, *values: Any) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute(statement, values).rowcount


def revisions(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as conn:
        return dict(conn.execute("SELECT scope,revision FROM collection_revisions").fetchall())


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [None, "all", "original", "saved", REAL_AUDIO])
@pytest.mark.parametrize("kind", ["song", "album", "artist"])
async def test_the_kept_selection_answers_as_the_policy_does(
    tmp_path: Path, kind: str, mode: Any
) -> None:
    """Whatever a read keeps, its pages and statistics are those of the policy read live."""
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, kind, source(rows_of(kind, ids[kind])), valid)

        def read(*, live: bool) -> Any:
            def work(conn: sqlite3.Connection, view: Any) -> Any:
                page = index._page(
                    conn,
                    view,
                    key,
                    BINDING,
                    kind,
                    mode,
                    0,
                    1000,
                    artist_count_role="albumartist",
                )
                stats = index._stats_of(conn, BINDING, kind, ids[kind], view.selected, view.params)
                whole = index._stats_of(conn, BINDING, kind, None, view.selected, view.params)
                return page.rows, page.total, page.all_selected, stats, whole

            return index._in_snapshot(BINDING, mode, work, live)

        kept = await index._read(BINDING, mode, lambda conn, view: view.identity)
        assert kept is not None
        assert read(live=False) == read(live=True)
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("statement", "values"),
    [
        (
            "INSERT INTO collection_originals VALUES (?,?,'k5','import',1)",
            (OWNER, LIBRARY),
        ),
        (
            "DELETE FROM collection_originals WHERE track_key=(SELECT min(track_key) FROM "
            "collection_originals)",
            (),
        ),
        ("UPDATE collection_tracks SET present=0 WHERE song_id='s0'", ()),
        ("UPDATE collection_tracks SET album_id='al16' WHERE song_id='s0'", ()),
        ("DELETE FROM collection_favorites WHERE item_id='al4'", ()),
        ("INSERT INTO collection_favorites VALUES (?,?,'album','al7')", (OWNER, LIBRARY)),
        (
            "INSERT INTO collection_membership(owner_id,profile_id,library_id,track_key,saved,"
            "revision,last_operation) VALUES (?,?,?,'k12',1,1,'')",
            (OWNER, builtin_profile_id(OWNER, "all"), LIBRARY),
        ),
        ("UPDATE collection_membership SET saved=1 WHERE track_key='k0'", ()),
        ("UPDATE collection_library_policy SET mode='personal',setup_id='x'", ()),
        ("UPDATE collection_preferences SET active_profile_id='custom-one'", ()),
    ],
)
async def test_every_change_of_what_decides_a_selection_shows_in_the_next_read(
    tmp_path: Path, statement: str, values: tuple[Any, ...]
) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    edit(collection, "INSERT INTO collection_preferences VALUES (?,'all',0)", OWNER)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, "song", source(rows_of("song", ids["song"])), valid)
        await index.page(key, BINDING, "song", None, 0, 500)
        with closing(sqlite3.connect(collection)) as conn, conn:
            conn.execute(statement, values)

        def live(conn: sqlite3.Connection, view: Any) -> Any:
            return index._page(conn, view, key, BINDING, "song", None, 0, 500).rows

        assert (await index.page(key, BINDING, "song", None, 0, 500)).rows == (
            await anyio.to_thread.run_sync(index._in_snapshot, BINDING, None, live, True)
        )
        await index.close()
        group.cancel_scope.cancel()


def test_the_revisions_count_each_writer_and_leave_out_what_decides_nothing(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    library(collection)
    owner = "owner:" + OWNER
    before = revisions(collection)
    # A song played before: its recorded activity is rewritten with the same value.
    edit(collection, "UPDATE collection_history SET activity=1,catalog_data='{\"a\":1}'")
    edit(collection, "UPDATE collection_tracks SET title='Another title'")
    assert revisions(collection) == before
    edit(collection, "UPDATE collection_history SET activity=0")
    after = revisions(collection)
    assert after[owner] > before[owner] and after[""] == before[""]
    edit(collection, "UPDATE collection_tracks SET present=0 WHERE song_id='s0'")
    assert revisions(collection)[""] > after[""]
    # Another account's choices are not this account's.
    edit(
        collection,
        "INSERT INTO collection_favorites VALUES ('two',?,'album','al1')",
        LIBRARY,
    )
    assert revisions(collection)[owner] == after[owner]
    assert revisions(collection)["owner:two"] == 1


@pytest.mark.anyio
async def test_a_real_audio_selection_follows_the_placeholders(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    with closing(sqlite3.connect(collection)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO placeholders(song_id,path,placeholder_path,track_ref,release_ref,title,"
            "artist,album,duration_ms,disc,track,tags,state,created_at) VALUES "
            "('s5','a','a','t','r','t','a','a',1,1,1,'{}','placeholder',1)"
        )
        conn.execute("UPDATE collection_tracks SET original=1 WHERE song_id='s5'")
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, "song", source(rows_of("song", ids["song"])), valid)
        first = await index.page(key, BINDING, "song", REAL_AUDIO, 0, 500)
        assert "s5" not in {row["id"] for row in first.rows}
        edit(collection, "UPDATE placeholders SET state='delivered' WHERE song_id='s5'")
        second = await index.page(key, BINDING, "song", REAL_AUDIO, 0, 500)
        assert "s5" in {row["id"] for row in second.rows}
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
@pytest.mark.parametrize(("kind", "few", "many"), [("album", 2, 30), ("artist", 1, 7)])
async def test_a_page_runs_as_many_statements_however_many_items_it_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, few: int, many: int
) -> None:
    """The artist directory took three statements per artist, each working the selection
    out again: a count of statements catches that on any machine."""
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection, 600)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, kind, source(rows_of(kind, ids[kind])), valid)
        await index.page(key, BINDING, kind, "all", 0, 1)
        statements = count_statements(index, monkeypatch)

        async def statements_of(limit: int) -> int:
            statements.clear()
            page = await index.page(key, BINDING, kind, "all", 0, limit, artist_count_role="x")
            assert len(page.rows) == limit
            return len(statements)

        assert await statements_of(many) == await statements_of(few)
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_reads_write_nothing_and_do_not_wait_for_the_writer(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, "album", source(rows_of("album", ids["album"])), valid)
        expected = await index.page(key, BINDING, "album", None, 0, 500)
        with closing(sqlite3.connect(index.path)) as conn:
            before = conn.execute("SELECT used FROM queries").fetchall()
        async with index.lock:  # a build's write, as long as it may take
            with anyio.fail_after(0.5):
                again = await index.page(key, BINDING, "album", None, 0, 500)
                assert await index.count(BINDING, None)
                assert await index.stats(BINDING, None, "album", "al0")
                assert await index.shown(BINDING, None, "album", ["al0"])
        assert again.rows == expected.rows
        with closing(sqlite3.connect(index.path)) as conn:
            assert conn.execute("SELECT used FROM queries").fetchall() == before
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_changed_selection_does_not_wait_long_for_a_busy_writer(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(selection_wait_seconds=0.01),
        )
        key = await index.ensure(BINDING, "song", source(rows_of("song", ids["song"])), valid)
        before = await index.page(key, BINDING, "song", "original", 0, 500)
        edit(collection, "DELETE FROM collection_favorites WHERE item_id='al4'")
        release = anyio.Event()

        async def write() -> None:
            async with index.lock:  # a build's write, as long as it may take
                await release.wait()

        group.start_soon(write)
        await anyio.wait_all_tasks_blocked()
        with anyio.fail_after(1):
            after = await index.page(key, BINDING, "song", "original", 0, 500)
        release.set()
        assert after.total == before.total - 5  # the favorite album's songs, exactly
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_list_takes_its_rows_from_a_kept_order_only_while_that_order_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, "song", source(rows_of("song", ids["song"])), valid)
        first = await index.page(key, BINDING, "song", None, 0, 10)
        statements = count_statements(index, monkeypatch)
        await index.page(key, BINDING, "song", None, 10, 10)
        assert not any("visible" in statement for statement in statements)
        # A new order for what changed, and for anything else asked of the same list.
        edit(
            collection,
            "UPDATE collection_tracks SET present=0 WHERE song_id=?",
            first.rows[0]["id"],
        )
        changed = await index.page(key, BINDING, "song", None, 0, 10)
        assert changed.total == first.total - 1
        assert changed.rows[0]["id"] != first.rows[0]["id"]
        statements.clear()
        assert (await index.page(key, BINDING, "song", "original", 0, 10)).total < changed.total
        assert any("visible" in statement for statement in statements)
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_large_random_pick_holds_distinct_visible_items(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, "song", source(rows_of("song", ids["song"])), valid)
        visible = await index.page(key, BINDING, "song", None, 0, 500)
        all_of_them = {row["id"] for row in visible.rows}
        pick = await index.page(key, BINDING, "song", None, 0, 12, random=True)
        names = [row["id"] for row in pick.rows]
        assert len(names) == len(set(names)) == 12 and set(names) <= all_of_them
        assert pick.total == visible.total
        past = await index.page(key, BINDING, "song", None, visible.total, 12, random=True)
        assert past.rows == []
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_used_list_stays_after_a_later_list_needs_its_room(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("song", ids["song"])
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(queries=2, owner_queries=2),
        )
        first = await index.ensure(BINDING, "first", source(rows), valid)
        second = await index.ensure(BINDING, "second", source(rows), valid)
        await anyio.sleep(0.01)
        await index.page(first, BINDING, "song", None, 0, 1)
        await index.ensure(BINDING, "third", source(rows), valid)
        assert (await index.page(first, BINDING, "song", None, 0, 1)).rows
        with pytest.raises(IndexPending):
            await index.page(second, BINDING, "song", None, 0, 1)
        await index.close()
        group.cancel_scope.cancel()


def new_snapshot(path: Path, generation: str, *added: int) -> None:
    """What a full scan of a changed library leaves behind."""
    with closing(sqlite3.connect(path)) as conn, conn:
        for n in added:
            conn.execute(
                "INSERT INTO collection_tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                track(n, album=n // 5, artist=0, original=True),
            )
            conn.execute(
                "INSERT INTO collection_originals VALUES (?,?,?,'import',1)",
                (OWNER, LIBRARY, f"k{n}"),
            )
        conn.execute("UPDATE collection_tracks SET generation=?", (generation,))
        conn.execute("UPDATE collection_index SET generation=?,updated_at=2", (generation,))


@pytest.mark.anyio
async def test_over_a_new_library_snapshot_a_list_shows_it_or_answers_as_it_stands(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("song", ids["song"])
    fetched = {"count": 0}
    gate = anyio.Event()
    gated = {"on": False}

    async def fetch(start: int, limit: int) -> SourcePage:
        fetched["count"] += 1
        if gated["on"]:
            await gate.wait()
        return SourcePage(rows[start : start + limit], len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(stale_wait_seconds=0.2, wait_seconds=5),
        )
        key = await index.ensure(BINDING, "songs", fetch, valid)
        before = await index.page(key, BINDING, "song", None, 0, 500)
        # A new snapshot with a new song, a build that cannot finish now: the list answers,
        # by the new facts, at once.
        new_snapshot(collection, "gen2", 900)
        moved = Binding(OWNER, frozenset({LIBRARY}), ((LIBRARY, "gen2"),), "grants")
        rows.append({"id": "s900", "title": "s900"})
        gated["on"] = True
        assert await index.ensure(moved, "songs", fetch, valid) == key
        served = await index.page(key, moved, "song", None, 0, 500)
        assert served.rows == before.rows
        # Once the build finishes the list holds the new song, as soon as it is asked for.
        gated["on"] = False
        gate.set()
        await built(index)
        assert await index.ensure(moved, "songs", fetch, valid) == key
        assert "s900" in {
            row["id"] for row in (await index.page(key, moved, "song", None, 0, 500)).rows
        }
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_reads_go_on_with_the_last_snapshot_while_a_new_ones_facts_are_written(
    tmp_path: Path,
) -> None:
    """A new snapshot's facts take a pass over the whole library. Reads do not wait for it:
    a removed song is gone at once, a new one comes with its facts."""
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("song", ids["song"])
    write = threading.Event()

    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        written = index._write_facts

        def slowly(binding: Binding) -> None:
            write.wait(10)
            written(binding)

        index._write_facts = slowly  # type: ignore[method-assign]
        assert await index.usable_generation(LIBRARY, GENERATION) == GENERATION
        write.set()
        key = await index.ensure(BINDING, "songs", source(rows), valid)
        before = await index.page(key, BINDING, "song", None, 0, 500)
        removed = before.rows[0]["id"]
        write.clear()

        new_snapshot(collection, "gen2", 900)
        edit(collection, "UPDATE collection_tracks SET present=0 WHERE song_id=?", removed)
        with anyio.fail_after(1):
            usable = await index.usable_generation(LIBRARY, "gen2")
            assert usable == GENERATION
            old = Binding(OWNER, frozenset({LIBRARY}), ((LIBRARY, usable),), "grants")
            page = await index.page(key, old, "song", None, 0, 500)
            assert await index.count(old, None) == before.total - 1
        names = {row["id"] for row in page.rows}
        assert removed not in names and "s900" not in names
        assert page.total == before.total - 1

        write.set()
        rows.append({"id": "s900", "title": "s900"})
        with anyio.fail_after(5):
            await index.prepare(LIBRARY, "gen2")
        assert await index.usable_generation(LIBRARY, "gen2") == "gen2"
        new = Binding(OWNER, frozenset({LIBRARY}), ((LIBRARY, "gen2"),), "grants")
        assert await index.ensure(new, "songs", source(rows), valid) == key
        await built(index)
        assert "s900" in {
            row["id"] for row in (await index.page(key, new, "song", None, 0, 500)).rows
        }
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_list_read_slowly_is_refreshed_for_a_share_of_the_time_unless_the_library_changed(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("song", ids["song"])
    fetched = {"count": 0}

    async def fetch(start: int, limit: int) -> SourcePage:
        fetched["count"] += 1
        return SourcePage(rows[start : start + limit], len(rows))

    def stale(value: float) -> None:
        with closing(sqlite3.connect(index.path)) as conn, conn:
            conn.execute("UPDATE queries SET updated=?", (value,))

    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        await index.ensure(BINDING, "songs", fetch, valid)
        key = BINDING.key("songs")
        assert fetched["count"] == 1
        index.paced[key] = time.monotonic() + 60
        index.attempted.pop(key, None)
        stale(time.time() - 3600)
        await index.ensure(BINDING, "songs", fetch, valid)
        await index.annotations_changed(OWNER)  # a play: lists are only stale
        await index.ensure(BINDING, "songs", fetch, valid)
        await anyio.sleep(0.05)
        assert fetched["count"] == 1
        await index.invalidate()  # the library changed
        await index.ensure(BINDING, "songs", fetch, valid)
        await built(index)
        assert fetched["count"] == 2
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_only_a_list_slow_to_read_is_paced(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("song", ids["song"])

    async def slowly(start: int, limit: int) -> SourcePage:
        await anyio.sleep(0.05)
        return SourcePage(rows[start : start + limit], len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(paced_from_seconds=0.04, refresh_share=0.1),
        )
        await index.ensure(BINDING, "slow", slowly, valid)
        await index.ensure(BINDING, "quick", source(rows), valid)
        now = time.monotonic()
        assert index.paced[BINDING.key("slow")] > now + 0.2
        assert BINDING.key("quick") not in index.paced
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_accounts_share_the_library_facts_and_nothing_they_may_not_read(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("album", ids["album"])
    other = Binding("two", frozenset({LIBRARY}), ((LIBRARY, GENERATION),), "other-grants")
    edit(
        collection,
        "INSERT INTO collection_favorites VALUES ('two',?,'album','al1')",
        LIBRARY,
    )
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        mine = await index.ensure(BINDING, "albums", source(rows), valid)
        theirs = await index.ensure(other, "albums", source(rows), valid)
        assert BINDING.facts == other.facts
        with closing(sqlite3.connect(index.path)) as conn:
            assert conn.execute("SELECT count(*) FROM facts").fetchone() == (1,)
        assert [r["id"] for r in (await index.page(theirs, other, "album", None, 0, 500)).rows] == [
            "al1"
        ]
        assert (await index.page(mine, BINDING, "album", None, 0, 500)).total > 1
        with pytest.raises(IndexPending):
            await index.page(mine, other, "album", None, 0, 500)
        # A caller's own answers about another library are that caller's.
        foreign = Binding(OWNER, frozenset({LIBRARY, "9"}), ((LIBRARY, GENERATION),), "grants")
        assert foreign.facts != BINDING.facts
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_kept_results_are_never_those_of_an_account_whose_selection_went(
    tmp_path: Path,
) -> None:
    """A selection's identity does not come back, and each kept result names its account."""
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("album", ids["album"])
    for account in ("two", "three"):
        edit(
            collection,
            "INSERT INTO collection_favorites VALUES (?,?,'album',?)",
            account,
            LIBRARY,
            "al1" if account == "two" else "al3",
        )
    two = Binding("two", frozenset({LIBRARY}), ((LIBRARY, GENERATION),), "grants-two")
    three = Binding("three", frozenset({LIBRARY}), ((LIBRARY, GENERATION),), "grants-three")
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(selections=1, remembered_statistics=1),
        )
        key = await index.ensure(two, "albums", source(rows), valid)
        ids_seen = []
        for _ in range(3):
            for binding in (two, three):
                await index.stats(binding, None, "album", "al1")
                with closing(sqlite3.connect(index.path)) as conn:
                    ids_seen.extend(r[0] for r in conn.execute("SELECT id FROM selections"))
        assert sorted(ids_seen) == sorted(set(ids_seen))
        assert (await index.stats(two, None, "album", "al1"))["songCount"] == 5
        assert await index.stats(three, None, "album", "al1") == {}
        assert [r["id"] for r in (await index.page(key, two, "album", None, 0, 50)).rows] == ["al1"]
        identities = {key[1] for key in index.remembered if key[0] != "statistics"}
        assert all(identity[0] == "two" for identity in identities)
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_facts_of_old_snapshots_are_reclaimed_without_lists_holding_them(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        for number in range(2, 8):
            new_snapshot(collection, f"gen{number}")
            await index.prepare(LIBRARY, f"gen{number}")
        with closing(sqlite3.connect(index.path)) as conn:
            assert conn.execute("SELECT count(*) FROM facts").fetchone() == (1,)
            assert conn.execute("SELECT generation FROM fact_libraries").fetchall() == [("gen7",)]
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_second_account_with_another_library_keeps_its_facts_evictable(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(queries=2, owner_queries=2),
        )
        for name in ("a", "b", "c", "d"):
            combined = Binding(
                name, frozenset({LIBRARY, "9"}), ((LIBRARY, GENERATION),), "grants-" + name
            )
            assert combined.foreign
            await index.ensure(combined, "songs", source([]), valid)
        with closing(sqlite3.connect(index.path)) as conn:
            kept = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
            lists = conn.execute("SELECT count(DISTINCT facts) FROM queries").fetchone()[0]
        assert kept == lists <= 2
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_the_memory_of_reads_ends_with_the_next_build(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    rows = rows_of("song", ids["song"])
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(BINDING, "songs", source(rows), valid)
        for _ in range(4):
            await index.page(key, BINDING, "song", None, 0, 1)
            await index.invalidate()
            index.attempted.clear()
            await index.ensure(BINDING, "songs", source(rows), valid)
            await built(index)
        assert len(index.used) <= 1
        await index.close()
        group.cancel_scope.cancel()


def test_each_rule_of_a_selection_finds_its_songs_by_the_index_made_for_it(tmp_path: Path) -> None:
    """Without table statistics SQLite takes any index it can use: a general one over all the
    library's songs made one rule read the whole library once per carried-over release."""
    path = tmp_path / "collection.sqlite3"
    library(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO collection_carryover_owner VALUES (1,?,'listener',1)", (OWNER,))
        conn.execute("INSERT INTO collection_carryover VALUES ('release1')")
        for rule in (
            original_albums(OWNER, LIBRARY),
            shijhon_albums(OWNER, LIBRARY),
            chosen_albums(OWNER, LIBRARY, "all"),
        ):
            for row in conn.execute("EXPLAIN QUERY PLAN " + rule.sql, rule.params):
                assert "collection_tracks_cover" not in row[3], row[3]
        plan = " ".join(
            row[3]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + shijhon_albums(OWNER, LIBRARY).sql,
                shijhon_albums(OWNER, LIBRARY).params,
            )
        )
        assert "collection_tracks_release" in plan
        cover = (
            "EXPLAIN QUERY PLAN SELECT 1 FROM collection_tracks WHERE library_id=?"
            " AND present=1 AND json_extract(metadata,'$.coverArt')=?"
        )
        assert "collection_tracks_cover" in conn.execute(cover, (LIBRARY, "x")).fetchone()[3]


@pytest.mark.anyio
async def test_a_search_that_found_nothing_does_not_wait_for_facts(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(wait_seconds=0),
        )
        await index.open()
        release = anyio.Event()

        async def write() -> None:
            async with index.lock:  # the library's first facts, as long as they take
                await release.wait()

        group.start_soon(write)
        await anyio.wait_all_tasks_blocked()
        assert await index.page_many(BINDING, None, []) == []
        release.set()
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_new_facts_get_room_from_older_lists_in_a_full_cache(tmp_path: Path) -> None:
    """Facts that do not fit leave every read of every account waiting: they come first."""
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    path = tmp_path / "cache" / "db"
    rows = rows_of("song", ids["song"])
    async with anyio.create_task_group() as group:
        index = QueryIndex(path, collection, group.start_soon)
        await index.ensure(BINDING, "first", source(rows), valid)
        await index.ensure(BINDING, "second", source(rows), valid)
        await index.close()
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            used = conn.execute("PRAGMA page_count").fetchone()[0]
            size = conn.execute("PRAGMA page_size").fetchone()[0]
        # The same cache, now with hardly any room left.
        full = Budgets(bytes=(4 << 20) + (used + 2) * size)
        index = QueryIndex(path, collection, group.start_soon, budgets=full)
        new_snapshot(collection, "gen2")
        with anyio.fail_after(5):
            await index.prepare(LIBRARY, "gen2")
        assert await index.usable_generation(LIBRARY, "gen2") == "gen2"
        moved = Binding(OWNER, frozenset({LIBRARY}), ((LIBRARY, "gen2"),), "grants")
        assert await index.count(moved, None)
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_the_cache_limit_is_the_budget_whatever_its_page_size(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    library(collection)
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(bytes=64 << 20)
        )
        await index.open()
        with closing(index._connect()) as conn:
            pages = conn.execute("PRAGMA max_page_count").fetchone()[0]
            size = conn.execute("PRAGMA page_size").fetchone()[0]
        assert pages * size == 60 << 20
        await index.close()
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_cache_of_another_revision_starts_over_and_its_own_stays(tmp_path: Path) -> None:
    """Its lists could never be read again, and would hold the room new ones need."""
    collection = tmp_path / "collection.sqlite3"
    ids = library(collection)
    path = tmp_path / "cache" / "db"
    rows = rows_of("song", ids["song"])
    async with anyio.create_task_group() as group:
        index = QueryIndex(path, collection, group.start_soon)
        await index.ensure(BINDING, "songs", source(rows), valid)
        await index.close()

        def lists() -> int:
            with closing(sqlite3.connect(path)) as conn:
                return int(conn.execute("SELECT count(*) FROM queries").fetchone()[0])

        kept = QueryIndex(path, collection, group.start_soon)
        await kept.open()
        assert lists() == 1
        await kept.close()
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA user_version=3")
        older = QueryIndex(path, collection, group.start_soon)
        await older.open()
        assert lists() == 0
        assert (path.stat().st_mode & 0o777) == 0o600
        await older.close()
        group.cancel_scope.cancel()
