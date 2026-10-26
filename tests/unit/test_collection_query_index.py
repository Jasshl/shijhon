"""Exact visibility paging uses full caller candidates, never one filtered page."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.collection.contracts import REAL_AUDIO
from shijhon.collection.query_index import (
    Binding,
    Budgets,
    IndexFailed,
    IndexPending,
    QueryIndex,
    SourcePage,
    digest,
)
from shijhon.collection.visibility import query_pairs
from shijhon.store import apply_migrations


def seed(path: Path, count: int = 23) -> list[dict[str, object]]:
    rows = []
    with sqlite3.connect(path) as conn:
        apply_migrations(conn)
        conn.execute("INSERT INTO collection_library_policy VALUES ('1','legacy_shared',1,NULL)")
        for owner in ("one", "two"):
            conn.execute(
                (
                    "INSERT INTO "
                    "collection_profiles(id,owner_id,kind,name,base_original,created)"
                    " VALUES (?,?,'custom',?,1,1)"
                ),
                (owner + "-custom", owner, "Optional fixture"),
            )
            conn.execute(
                "INSERT INTO collection_preferences VALUES (?,?,0)", (owner, owner + "-custom")
            )
        conn.execute("INSERT INTO collection_index VALUES ('1','gen','ready',?,1,'')", (count,))
        for i in range(count):
            ident = f"s{i:05}"
            metadata = {
                "albumArtistId": "artist",
                "genres": [{"id": "g", "name": "Invented"}],
                "tags": {"genre": [{"id": "g", "value": "Invented"}]},
                "participants": {"albumartist": [{"id": "artist"}]},
            }
            conn.execute(
                "INSERT INTO collection_tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "1",
                    "native:" + ident,
                    ident,
                    "release",
                    f"album{i // 3}",
                    "artist",
                    ident,
                    "Album",
                    "Artist",
                    ident,
                    10,
                    100,
                    2020,
                    json.dumps(metadata),
                    int(i % 3 == 0),
                    1,
                    "gen",
                    1,
                ),
            )
            rows.append({"id": ident, "title": ident, "albumId": f"album{i // 3}", "duration": 10})
        # The fixture libraries hold the originals alone, as hand choices (a song added by
        # hand brings its album: the album's other songs are removed).
        for owner in ("one", "two"):
            conn.execute(
                "INSERT INTO collection_membership(owner_id,profile_id,library_id,track_key,"
                "saved,revision,last_operation) SELECT ?,?,library_id,track_key,original,0,''"
                " FROM collection_tracks",
                (owner, owner + "-custom"),
            )
    return rows


async def valid() -> None:
    pass


def test_query_identity_keeps_first_value_and_all_genuine_multivalues() -> None:
    binding = Binding("one", frozenset({"1", "2"}), (("1", "gen"),))
    fiction = [
        ("genre", "Fiction"),
        ("musicFolderId", "2"),
        ("genre", "Fable"),
        ("musicFolderId", "1"),
    ]
    fable = [
        ("genre", "Fable"),
        ("musicFolderId", "2"),
        ("genre", "Fiction"),
        ("musicFolderId", "1"),
    ]
    names_reordered = [
        ("musicFolderId", "2"),
        ("musicFolderId", "1"),
        ("genre", "Fiction"),
        ("genre", "Fable"),
    ]
    assert binding.key(query_pairs(fiction)) != binding.key(query_pairs(fable))
    assert binding.key(query_pairs(fiction)) == binding.key(query_pairs(names_reordered))
    assert [value for key, value in query_pairs(fiction) if key == "musicFolderId"] == ["2", "1"]


@pytest.mark.anyio
async def test_previous_ambiguous_query_generation_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 6)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    query = ("getSongsByGenre", [("genre", "Fable"), ("genre", "Fiction")], "music.test")
    fetched: list[str] = []

    async def fiction(start: int, limit: int) -> SourcePage:
        fetched.append("Fiction")
        return SourcePage(rows[:3], 3)

    async def fable(start: int, limit: int) -> SourcePage:
        fetched.append("Fable")
        return SourcePage(rows[3:], 3)

    def legacy_key(self: Binding, query: object) -> str:
        return digest(
            (self.owner_id, self.authority, sorted(self.libraries), self.generations, query)
        )

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        # The previous implementation could publish Fiction under the pair-sorted
        # key that also described a Fable-first request. Emulate that persisted slot.
        with monkeypatch.context() as previous:
            previous.setattr(Binding, "key", legacy_key)
            await index.ensure(binding, query, fiction, valid)
        key = await index.ensure(binding, query, fable, valid)
        assert fetched == ["Fiction", "Fable"]
        assert [s["id"] for s in (await index.page(key, binding, "song", None, 0, 20)).rows] == [
            "s00003"
        ]


@pytest.mark.anyio
async def test_exact_pages_membership_modes_and_two_owners(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection)
    one = Binding("one", frozenset({"1"}), (("1", "gen"),))
    two = replace(one, owner_id="two")
    calls = []

    async def fetch(start: int, limit: int) -> SourcePage:
        calls.append((start, limit))
        return SourcePage(rows[start : start + limit], len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "private" / "cache.sqlite3",
            collection,
            group.start_soon,
            budgets=Budgets(page_size=5, wait_seconds=2),
        )
        key = await index.ensure(one, "songs", fetch, valid)
        page = await index.page(key, one, "song", None, 2, 3)
        assert page.total == 8
        assert [i["id"] for i in page.rows] == ["s00006", "s00009", "s00012"]
        assert len(calls) == 5
        assert not page.all_selected
        with sqlite3.connect(collection) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO collection_membership VALUES "
                "('one','one-custom','1','native:s00000',0,1,'remove')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO collection_membership VALUES "
                "('one','one-custom','1','native:s00001',1,1,'save')"
            )
        assert await index.ensure(one, "songs", fetch, valid) == key
        saved = await index.page(key, one, "song", None, 0, 3)
        assert [i["id"] for i in saved.rows] == ["s00001", "s00003", "s00006"]
        original = await index.page(key, one, "song", "original", 0, 3)
        assert original.rows[0]["id"] == "s00000"
        # Every album holds an original, so All has every album whole (the custom
        # library's removal of s00000 is not All's).
        assert (await index.page(key, one, "song", "all", 0, 100)).total == len(rows)
        assert len(calls) == 5  # membership and mode edits never rescan upstream
        other = await index.ensure(two, "songs", fetch, valid)
        assert other != key
        assert (await index.page(other, two, "song", None, 0, 3)).rows[0]["id"] == "s00000"
        with pytest.raises(IndexPending):
            await index.page(key, two, "song", None, 0, 3)
        revoked = replace(one, libraries=frozenset(), generations=())
        with pytest.raises(IndexPending):
            await index.page(key, revoked, "song", "all", 0, 3)
        assert (index.path.stat().st_mode & 0o777) == 0o600
        assert (index.path.parent.stat().st_mode & 0o777) == 0o700


@pytest.mark.anyio
async def test_full_raw_proof_checks_actual_ids_and_current_selection(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 12)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    with sqlite3.connect(collection) as conn:
        conn.execute("UPDATE collection_profiles SET base_original=1 WHERE owner_id='one'")
        conn.executemany(
            "INSERT OR REPLACE INTO collection_membership"
            " VALUES ('one','one-custom','1',?,1,1,'all')",
            [("native:" + str(row["id"]),) for row in rows],
        )

    async def fetch(start: int, limit: int) -> SourcePage:
        return SourcePage(rows[start : start + limit], len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        key = await index.ensure(binding, "songs", fetch, valid)
        assert (
            await index.page(key, binding, "song", None, 0, 2, verify_songs=["s00001", "s00000"])
        ).all_selected
        assert not (
            await index.page(key, binding, "song", None, 0, 2, verify_songs=["new-unknown"])
        ).all_selected
        with sqlite3.connect(collection) as conn:
            conn.execute("UPDATE collection_membership SET saved=0 WHERE track_key='native:s00011'")
        assert not (
            await index.page(key, binding, "song", None, 0, 2, verify_songs=["s00001", "s00000"])
        ).all_selected
        with sqlite3.connect(collection) as conn:
            conn.execute("UPDATE collection_index SET generation='new-source'")
        with pytest.raises(IndexPending):
            await index.page(key, binding, "song", None, 0, 2)


@pytest.mark.anyio
async def test_live_detail_uses_same_policy_without_persisting_caller_answers(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 12)[:3]
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    rows[0]["userRating"] = 4
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        source = SourcePage(rows, len(rows), {"meta": {"userRating": 4}}, complete=True)
        page = await index.live_page(binding, None, "song", source, direct_album="album0")
        assert page.rows == rows[:1] and page.total == 1 and page.duration == 10
        assert page.envelope["meta"]["userRating"] == 4
        with sqlite3.connect(index.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0
        with sqlite3.connect(collection) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO collection_membership"
                " VALUES ('one','one-custom','1',?,1,1,'save')",
                [("native:s00001",), ("native:s00002",)],
            )
        assert (
            await index.live_page(binding, None, "song", source, direct_album="album0")
        ).rows == rows


@pytest.mark.anyio
async def test_aggregate_counts_shuffle_and_direct_discovery(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    seed(collection, 12)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    albums = [{"id": f"album{i}", "songCount": 3, "duration": 30, "size": 300} for i in range(4)]

    async def fetch(start: int, limit: int) -> SourcePage:
        return SourcePage(albums[start : start + limit], len(albums))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        key = await index.ensure(binding, "albums", fetch, valid)
        page = await index.page(key, binding, "album", None, 0, 4)
        assert all(a["songCount"] == 1 and a["duration"] == 10 for a in page.rows)
        assert (await index.stats(binding, None, "artist", "artist"))["songCount"] == 4
        for _ in range(4):
            random = await index.page(key, binding, "album", None, 0, 4, random=True)
            assert {a["id"] for a in random.rows} == {a["id"] for a in page.rows}
        with sqlite3.connect(collection) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO collection_membership VALUES "
                "('one','one-custom','1','native:s00000',0,1,'remove')"
            )
        assert (await index.page(key, binding, "album", None, 0, 4)).total == 3
        direct = await index.page(key, binding, "album", None, 0, 4, direct_album="album0")
        assert direct.total == 4
        assert direct.rows[0]["songCount"] == 3


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["album", "artist", "genre", "directory"])
async def test_pages_count_each_visible_item_once_with_multiple_selected_songs(
    tmp_path: Path, kind: str
) -> None:
    """Offsets count items, even when several selected songs make each item visible."""
    collection = tmp_path / "collection.sqlite3"
    seed(collection, 18)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    if kind in ("album", "directory"):
        rows = [{"id": f"album{i}", "songCount": 3} for i in range(6)]
        if kind == "directory":
            rows = [{**row, "isDir": True} for row in rows]
    else:
        rows = (
            [{"id": "artist", "songCount": 18}]
            if kind == "artist"
            else [{"id": "Invented", "name": "Invented", "songCount": 18}]
        )

    async def fetch(start: int, limit: int) -> SourcePage:
        return SourcePage(rows[start : start + limit], len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        key = await index.ensure(binding, ("paged", kind), fetch, valid)
        seen = []
        for offset in range(0, len(rows) + 3, 2):
            page = await index.page(key, binding, kind, "all", offset, 2)
            assert page.total == len(rows)
            assert [row["id"] for row in page.rows] == [
                row["id"] for row in rows[offset : offset + 2]
            ], (kind, offset)
            seen.extend(row["id"] for row in page.rows)
        assert seen == [row["id"] for row in rows]
        assert len(set(seen)) == len(seen)


@pytest.mark.anyio
async def test_whole_albums_keep_native_counts_only_when_every_original_song_is_selected(
    tmp_path: Path,
) -> None:
    """Each album: one original song and two songs Shijhon added (``seed``)."""
    collection = tmp_path / "collection.sqlite3"
    seed(collection, 18)
    with sqlite3.connect(collection) as conn:
        # album1: one added song saved too; album2: its original song removed, an added
        # one saved; album3: every song saved; album4: no original song at all, one
        # saved; album5: original songs only, all selected (Navidrome's row lags).
        # (album4 and album5 change which songs are originals: the library's choices
        # follow by hand.)
        for song, saved in (("s00004", 1), ("s00006", 0), ("s00007", 1), ("s00010", 1),
                            ("s00011", 1), ("s00013", 1), ("s00012", 0), ("s00016", 1),
                            ("s00017", 1)):  # fmt: skip
            conn.execute(
                "INSERT OR REPLACE INTO collection_membership"
                " VALUES ('one','one-custom','1',?,?,1,?)",
                ("native:" + song, saved, "save" if saved else "remove"),
            )
        conn.execute("UPDATE collection_tracks SET original=0 WHERE song_id='s00012'")
        conn.execute("UPDATE collection_tracks SET original=1 WHERE song_id IN ('s00016','s00017')")
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    albums = [{"id": f"album{i}", "songCount": 3, "duration": 30, "size": 300} for i in range(6)]
    albums[5] = {"id": "album5", "songCount": 4, "duration": 40, "size": 400}

    async def fetch(start: int, limit: int) -> SourcePage:
        return SourcePage(albums[start : start + limit], len(albums))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        key = await index.ensure(binding, "albums", fetch, valid)
        selected = await index.page(key, binding, "album", None, 0, 6)
        whole = await index.page(key, binding, "album", None, 0, 6, whole_albums=True)
        [many] = await index.page_many(binding, None, [(key, "album", 0, 6)], whole_albums=True)
    assert selected.total == whole.total == many.total == 6
    assert [(a["songCount"], a["duration"]) for a in selected.rows] == [
        (1, 10), (2, 20), (1, 10), (3, 30), (1, 10), (3, 30)
    ]  # fmt: skip
    assert [(a["songCount"], a["duration"]) for a in whole.rows] == [
        (3, 30), (3, 30), (1, 10), (3, 30), (1, 10), (3, 30)
    ]  # fmt: skip
    assert many.rows == whole.rows
    # The original songs of each album listed with all of them, for saved plans.
    assert selected.original_songs is None
    assert (
        whole.original_songs
        == many.original_songs
        == {
            "album0": frozenset({"s00000"}),
            "album1": frozenset({"s00003"}),
            "album3": frozenset({"s00009"}),
            "album5": frozenset({"s00015", "s00016", "s00017"}),
        }
    )


@pytest.mark.anyio
async def test_whole_albums_of_a_large_page_bind_one_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page's albums are one bound value, whatever SQLite's variable limit (a search
    may ask for thousands of albums)."""
    collection = tmp_path / "collection.sqlite3"
    seed(collection, 180)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    albums = [{"id": f"album{i}", "songCount": 3, "duration": 30} for i in range(60)]
    connect = QueryIndex._connect

    def limited(self: QueryIndex) -> sqlite3.Connection:
        conn = connect(self)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 40)
        return conn

    monkeypatch.setattr(QueryIndex, "_connect", limited)

    async def fetch(start: int, limit: int) -> SourcePage:
        return SourcePage(albums[start : start + limit], len(albums))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        key = await index.ensure(binding, "albums", fetch, valid)
        page = await index.page(key, binding, "album", None, 0, 60, whole_albums=True)
        [many] = await index.page_many(binding, None, [(key, "album", 0, 60)], whole_albums=True)
    assert page.total == 60 and len(page.rows) == 60 and many.rows == page.rows
    # Each album: its one original song selected, the two songs Shijhon added not.
    assert all((a["songCount"], a["duration"]) == (3, 30) for a in page.rows)
    assert page.original_songs is not None and len(page.original_songs) == 60


@pytest.mark.anyio
async def test_incomplete_changed_duplicate_and_revoked_queries_never_publish(
    tmp_path: Path,
) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 10)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(page_size=5, wait_seconds=2),
        )

        async def duplicate(start: int, limit: int) -> SourcePage:
            return SourcePage(rows[:5], 10)

        with pytest.raises(IndexFailed):
            await index.ensure(binding, "duplicates", duplicate, valid)

        async def changes(start: int, limit: int) -> SourcePage:
            return SourcePage(rows[start : start + limit], 10 if start == 0 else 9)

        with pytest.raises(IndexFailed):
            await index.ensure(binding, "changes", changes, valid)
        validations = 0

        async def revoke() -> None:
            nonlocal validations
            validations += 1
            if validations > 1:
                raise RuntimeError("revoked")

        async def fetch(start: int, limit: int) -> SourcePage:
            return SourcePage(rows[start : start + limit], 10)

        with pytest.raises(IndexFailed):
            await index.ensure(binding, "revoked", fetch, revoke)
        with sqlite3.connect(index.path) as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM queries WHERE state='ready'").fetchone()[0] == 0
            )
            assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0


@pytest.mark.anyio
async def test_profile_activation_seeded_pages_refresh_and_capacity(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 23)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),), "current-grants")
    calls = 0

    async def fetch(start: int, limit: int) -> SourcePage:
        nonlocal calls
        calls += 1
        return SourcePage(rows[start : start + limit], len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(queries=2, owner_queries=2, wait_seconds=2),
        )
        key = await index.ensure(binding, "songs", fetch, valid)
        full = await index.page(
            key, binding, "song", None, 0, 100, random=True, random_seed="same-seed"
        )
        first = await index.page(
            key, binding, "song", None, 0, 3, random=True, random_seed="same-seed"
        )
        second = await index.page(
            key, binding, "song", None, 3, 3, random=True, random_seed="same-seed"
        )
        assert first.rows + second.rows == full.rows[:6]
        with sqlite3.connect(collection) as conn:
            conn.execute(
                "UPDATE collection_preferences SET active_profile_id='shijhon' WHERE owner_id='one'"
            )
        assert (await index.page(key, binding, "song", None, 0, 100)).total == 0
        assert calls == 1
        changed = replace(binding, authority="different-current-grants")
        with pytest.raises(IndexPending):
            await index.page(key, changed, "song", None, 0, 100)
        await index.ensure(binding, "another", fetch, valid)
        await index.ensure(binding, "third", fetch, valid)
        with pytest.raises(IndexPending):
            await index.page(key, binding, "song", None, 0, 100)
        assert calls == 3
        await index.close()


@pytest.mark.anyio
async def test_superseded_typeahead_cancels_before_publish(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    started, cancelled = anyio.Event(), anyio.Event()

    async def slow(start: int, limit: int) -> SourcePage:
        started.set()
        try:
            await anyio.sleep(10)
        finally:
            cancelled.set()
        return SourcePage(rows, len(rows))

    async def fast(start: int, limit: int) -> SourcePage:
        return SourcePage(rows[:1], 1)

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        with pytest.raises(IndexPending):
            await index.ensure(binding, "old search", slow, valid, lane="owner:search", wait=False)
        await started.wait()
        key = await index.ensure(binding, "new search", fast, valid, lane="owner:search")
        await cancelled.wait()
        assert (await index.page(key, binding, "song", None, 0, 10)).total == 1
        with sqlite3.connect(index.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 1
        await index.close()


def placeholders(path: Path, states: dict[str, str]) -> None:
    """Placeholder rows for seeded songs (every seeded song that is not original is in
    the placeholder folder): ``states`` song -> placeholder or delivered."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO releases VALUES ('test:r','_shijhon/r','album0',NULL,'R','A','{}','{}',1,"
            "NULL)"
        )
        for song, state in states.items():
            conn.execute(
                "INSERT INTO placeholders (song_id,path,placeholder_path,track_ref,release_ref,"
                "title,artist,album,duration_ms,disc,track,tags,state,created_at)"
                " VALUES (?,?,?,?,'test:r','T','A','R',1000,1,1,'{}',?,1)",
                (song, "_shijhon/" + song, "_shijhon/" + song, "test:" + song, state),
            )


@pytest.mark.anyio
async def test_real_audio_selects_original_and_delivered_songs_only(tmp_path: Path) -> None:
    """Originals: s00000, s00003, ...; s00001 delivered, s00002 and s00004 placeholders,
    s00005 a file in the placeholder folder no row tells of."""
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 12)
    placeholders(
        collection, {"s00001": "delivered", "s00002": "placeholder", "s00004": "placeholder"}
    )
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))

    async def fetch(start: int, limit: int) -> SourcePage:
        return SourcePage(rows[start : start + limit], len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        key = await index.ensure(binding, "songs", fetch, valid)
        page = await index.page(key, binding, "song", REAL_AUDIO, 0, 20)
        assert [s["id"] for s in page.rows] == ["s00000", "s00001", "s00003", "s00006", "s00009"]
        assert await index.count(binding, REAL_AUDIO) == 5
        # An album page holds only its real audio, and discovers nothing without any.
        album1 = SourcePage(rows[3:6], 3, complete=True)
        direct = await index.live_page(binding, REAL_AUDIO, "song", album1, direct_album="album1")
        assert [s["id"] for s in direct.rows] == ["s00003"] and direct.duration == 10
        known = await index.stats_many(binding, REAL_AUDIO, "album", ["album0", "album1", "x"])
        assert known["album0"]["songCount"] == 2 and known["album1"]["songCount"] == 1
        assert "x" not in known  # not in the index at all
        with sqlite3.connect(collection) as conn:  # a delivered song reverted: silent again
            conn.execute("UPDATE placeholders SET state='placeholder' WHERE song_id='s00001'")
        assert await index.count(binding, REAL_AUDIO) == 4
        known = await index.stats_many(binding, REAL_AUDIO, "album", ["album0"])
        assert known["album0"]["songCount"] == 1
        await index.close()


@pytest.mark.anyio
async def test_real_audio_album_pages_discover_nothing(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 6)
    placeholders(collection, {"s00004": "placeholder", "s00005": "placeholder"})
    with sqlite3.connect(collection) as conn:  # album1: s00003 (original), s00004, s00005
        conn.execute("UPDATE collection_tracks SET original=0 WHERE song_id='s00003'")
        # No longer an original: album1 is not in the fixture's library at all.
        conn.execute("DELETE FROM collection_membership WHERE track_key IN"
                     " ('native:s00003','native:s00004','native:s00005')")  # fmt: skip
        conn.execute(
            "INSERT INTO placeholders (song_id,path,placeholder_path,track_ref,release_ref,"
            "title,artist,album,duration_ms,disc,track,tags,state,created_at) VALUES"
            " ('s00003','_shijhon/3','_shijhon/3','test:3','test:r','T','A','R',1,1,1,'{}',"
            "'placeholder',1)"
        )
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        album1 = SourcePage(rows[3:6], 3, complete=True)
        everyone = await index.live_page(binding, None, "song", album1, direct_album="album1")
        assert len(everyone.rows) == 3  # an ordinary page discovers the whole album
        real = await index.live_page(binding, REAL_AUDIO, "song", album1, direct_album="album1")
        assert real.rows == [] and real.total == 0
        known = await index.stats_many(binding, REAL_AUDIO, "album", ["album1"])
        assert known == {"album1": {}}  # known, without real audio
        await index.close()


@pytest.mark.anyio
async def test_a_patient_query_waits_for_its_first_generation(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 3)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))

    async def slow(start: int, limit: int) -> SourcePage:
        await anyio.sleep(0.3)
        return SourcePage(rows, len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db",
            collection,
            group.start_soon,
            budgets=Budgets(wait_seconds=0.01),
        )
        with pytest.raises(IndexPending):
            await index.ensure(binding, "impatient", slow, valid)
        key = await index.ensure(binding, "patient", slow, valid, patience=5)
        assert (await index.page(key, binding, "song", REAL_AUDIO, 0, 10)).total == 1
        await index.close()


@pytest.mark.anyio
async def test_a_first_generation_of_a_second_is_answered_without_asking_again(
    tmp_path: Path,
) -> None:
    """Apps do not ask again after an answer that the view is being prepared: by default a
    request outlasts a first build of a second (it waited 0.35 s)."""
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 3)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))

    async def slow(start: int, limit: int) -> SourcePage:
        await anyio.sleep(1)
        return SourcePage(rows, len(rows))

    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "cache" / "db", collection, group.start_soon)
        key = await index.ensure(binding, "slow", slow, valid)
        assert (await index.page(key, binding, "song", None, 0, 10)).total == 1
        await index.close()


@pytest.mark.anyio
async def test_a_complete_answer_reads_as_its_kept_generation_and_is_not_kept(
    tmp_path: Path,
) -> None:
    """A search's complete first page (``Visibility._search``) has the rows, totals and
    counts its generation would have, in one snapshot with kept generations, and nothing
    of it is kept."""
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    albums = [{"id": f"album{i}", "songCount": 3, "duration": 30, "size": 300} for i in range(8)]
    envelope = {"response": {"status": "ok", "version": "1.16.1"}, "meta": {}}

    async def songs(start: int, limit: int) -> SourcePage:
        return SourcePage(rows[start : start + limit], len(rows), envelope)

    async def album_rows(start: int, limit: int) -> SourcePage:
        return SourcePage(albums[start : start + limit], len(albums), envelope)

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=2)
        )
        song_key = await index.ensure(binding, "songs", songs, valid)
        album_key = await index.ensure(binding, "albums", album_rows, valid)
        live_songs = SourcePage(rows, len(rows), envelope, complete=True)
        live_albums = SourcePage(albums, len(albums), envelope, complete=True)
        with sqlite3.connect(index.path) as conn:
            kept = conn.execute("SELECT COUNT(*), SUM(rows) FROM queries").fetchone()

        def same(page: Any) -> tuple[Any, ...]:
            return page.rows, page.total, page.envelope, page.all_selected, page.original_songs

        for mode in (None, REAL_AUDIO):
            for offset, limit in ((0, 50), (2, 3), (6, 20)):
                for whole in (False, True):
                    song: Any = (song_key, "song", offset, limit)
                    album: Any = (album_key, "album", offset, limit)
                    expected = await index.page_many(
                        binding, mode, [song, album], whole_albums=whole
                    )
                    live_song: Any = (live_songs, "song", offset, limit)
                    live_album: Any = (live_albums, "album", offset, limit)
                    for requests in (
                        [live_song, album],
                        [song, live_album],
                        [live_song, live_album],
                    ):
                        pages = await index.page_many(binding, mode, requests, whole_albums=whole)
                        assert [same(p) for p in pages] == [same(p) for p in expected]
        assert expected[0].total == 8 and expected[1].rows  # the selection left songs out
        with sqlite3.connect(index.path) as conn:
            assert conn.execute("SELECT COUNT(*), SUM(rows) FROM queries").fetchone() == kept
        # Only a complete answer of at most one source page, with valid distinct IDs.
        for wrong in (
            SourcePage(rows, len(rows)),
            SourcePage([{"id": str(i)} for i in range(501)], 501, complete=True),
            SourcePage([rows[0], rows[0]], 2, complete=True),
            SourcePage([{"title": "no ID"}], 1, complete=True),
        ):
            with pytest.raises(IndexFailed):
                await index.page_many(binding, None, [(wrong, "song", 0, 10)])
        await index.close()


@pytest.mark.anyio
async def test_typing_on_cancels_only_a_build_no_other_lane_waits_for(tmp_path: Path) -> None:
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 3)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    release = anyio.Event()
    started = {"a": anyio.Event(), "shared": anyio.Event()}
    cancelled: list[str] = []
    outcomes: dict[str, str] = {}

    def held(name: str) -> Callable[[int, int], Awaitable[SourcePage]]:
        async def fetch(start: int, limit: int) -> SourcePage:
            started[name].set()  # its first request is waiting for it by now
            try:
                await release.wait()
            except anyio.get_cancelled_exc_class():
                cancelled.append(name)
                raise
            return SourcePage(rows, len(rows))

        return fetch

    async def fast(start: int, limit: int) -> SourcePage:
        return SourcePage(rows[:1], 1)

    app, other = "one:App:search3:song", "one:Other:search3:song"
    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=5)
        )
        done = {name: anyio.Event() for name in ("a", "shared", "other app")}

        async def ask(name: str, query: str, fetch: Any, lane: str) -> None:
            try:
                await index.ensure(binding, query, fetch, valid, lane=lane)
                outcomes[name] = "ready"
            except IndexPending:
                outcomes[name] = "pending"
            if name in done:
                done[name].set()

        # Only the lane's own request waits for "a": typing on cancels its build.
        group.start_soon(ask, "a", "a", held("a"), app)
        await started["a"].wait()
        await ask("ab", "ab", fast, app)
        await done["a"].wait()
        assert outcomes == {"a": "pending", "ab": "ready"} and cancelled == ["a"]
        # Another app's request waits for "shared" too: that build goes on and answers both.
        group.start_soon(ask, "shared", "shared", held("shared"), app)
        await started["shared"].wait()
        group.start_soon(ask, "other app", "shared", held("shared"), other)
        await until(lambda: index.waiting.get(binding.key("shared")) == [app, other])
        await ask("shared!", "shared!", fast, app)
        release.set()
        await done["other app"].wait()
        await done["shared"].wait()
        assert outcomes["other app"] == outcomes["shared"] == "ready" and cancelled == ["a"]
        assert index.waiting == {}
        await index.close()


async def until(check: Callable[[], bool]) -> None:
    for _ in range(500):
        if check():
            return
        await anyio.sleep(0.01)
    raise AssertionError("the expected state never came")


@pytest.mark.anyio
async def test_a_search_answered_directly_lets_a_build_finish_only_while_awaited(
    tmp_path: Path,
) -> None:
    """A search answered without a build (``moved_on``) may be a separate lookup of a
    burst, not typing on: the lane's previous build answers the request waiting for it.
    A build nobody waits for any more stops once its lane has moved on."""
    collection = tmp_path / "collection.sqlite3"
    rows = seed(collection, 3)
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    names = ("intro", "abandoned", "given up")
    release = {name: anyio.Event() for name in names}
    started = {name: anyio.Event() for name in names}
    done = {name: anyio.Event() for name in names}
    cancelled: list[str] = []
    outcomes: dict[str, str] = {}
    app = "one:App:search3:song"

    def held(name: str) -> Callable[[int, int], Awaitable[SourcePage]]:
        async def fetch(start: int, limit: int) -> SourcePage:
            started[name].set()
            try:
                await release[name].wait()
            except anyio.get_cancelled_exc_class():
                cancelled.append(name)
                raise
            return SourcePage(rows, len(rows))

        return fetch

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "db", collection, group.start_soon, budgets=Budgets(wait_seconds=5)
        )

        async def ask(name: str, patience: float) -> None:
            try:
                await index.ensure(binding, name, held(name), valid, lane=app, patience=patience)
                outcomes[name] = "ready"
            except IndexPending:
                outcomes[name] = "pending"
            done[name].set()

        # A burst: "intro" is prepared, "interlude" answered directly; both are needed.
        group.start_soon(ask, "intro", 5)
        await started["intro"].wait()
        await index.moved_on(binding, "interlude", app)
        release["intro"].set()
        await done["intro"].wait()
        assert outcomes["intro"] == "ready" and cancelled == []
        # Its request gave up; the build goes on until the lane moves on.
        group.start_soon(ask, "abandoned", 0.1)
        await done["abandoned"].wait()
        assert outcomes["abandoned"] == "pending" and binding.key("abandoned") in index.jobs
        await index.moved_on(binding, "abandoned!", app)
        await until(lambda: cancelled == ["abandoned"])
        # The lane moved on first; the build stops when its request gives up.
        group.start_soon(ask, "given up", 0.3)
        await started["given up"].wait()
        await index.moved_on(binding, "given up!", app)
        assert cancelled == ["abandoned"]
        await done["given up"].wait()
        await until(lambda: cancelled == ["abandoned", "given up"] and not index.jobs)
        assert index.waiting == {} and index.job_lanes == {}
        await index.close()
