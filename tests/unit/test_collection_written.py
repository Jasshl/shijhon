"""Songs Shijhon wrote join the library index from apps' requests (``collection/written.py``).

The snapshot is prepared once from native reads; later placeholders are read with the
app's own Subsonic credentials and added to it in one transaction: at most one addition
at a time and one per gap, none without a snapshot, nothing for songs it already has.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

import anyio
import httpx
import pytest

from shijhon.access import Principal
from shijhon.collection.contracts import CollectionError
from shijhon.collection.origins import Origins
from shijhon.collection.query_index import Binding, Budgets, QueryIndex, SourcePage, native_tag_id
from shijhon.collection.service import Collection
from shijhon.collection.written import Written, native_shape
from shijhon.proxy.params import RestCall
from shijhon.store import Store
from tests.unit.test_collection import FakeAccess, song
from tests.unit.test_collection_query_index import valid
from tests.unit.test_collection_reasons import Spawner

pytestmark = pytest.mark.anyio
ONE = Principal("one", "listener", False, frozenset({1}))
ELSEWHERE = Principal("two", "other", False, frozenset({2}))


def rest_song(ident: str, **extra: Any) -> dict[str, Any]:
    """A placeholder as Subsonic's getSong gives it (a path that is not the file's)."""
    return {
        "id": ident,
        "title": "Song " + ident,
        "album": "Written Album",
        "albumId": "w1",
        "artist": "Invented Writer",
        "artistId": "rw",
        "path": "Invented Writer/Written Album/01 - Song.flac",
        "duration": 180,
        "size": 1000,
        "year": 2020,
        "starred": "2026-10-22T10:00:00Z",
        "playCount": 7,
        "artists": [{"id": "rw", "name": "Invented Writer"}],
        "albumArtists": [{"id": "rw", "name": "Invented Writer"}],
        "contributors": [{"role": "composer", "artist": {"id": "rc", "name": "Composer"}}],
        "genres": [{"name": "Imaginary"}],
        "isrc": ["ZZW000000001"],
        **extra,
    }


class Navidrome:
    """Answers getSong for ``songs``; others are not found."""

    library_answer = None

    def __init__(self, songs: dict[str, dict[str, Any]]) -> None:
        self.songs, self.asked = songs, list[list[tuple[str, str]]]()

    async def send(self, method: str, path: bytes, query: bytes, *rest: Any) -> httpx.Response:
        params = parse_qsl(query.decode())
        self.asked.append(params)
        ident = dict(params).get("id", "")
        if ident in self.songs:
            body: dict[str, Any] = {"status": "ok", "song": self.songs[ident]}
        else:
            body = {"status": "failed", "error": {"code": 70, "message": "not found"}}
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"subsonic-response": body}).encode(),
        )


def request() -> RestCall:
    query = [("u", "listener"), ("t", "token"), ("s", "salt"), ("v", "1.16.1"), ("c", "app"),
             ("f", "xml"), ("type", "newest")]  # fmt: skip
    return RestCall.build(
        "getAlbumList2", "GET", b"/rest/getAlbumList2", urlencode(query).encode(), [], None
    )


async def placeholders(store: Store, songs: list[str]) -> None:
    await store.execute(
        "INSERT OR IGNORE INTO releases VALUES ('test:w','_shijhon/w','w1',NULL,'W','A','{}',"
        "'{}',1,NULL)"
    )
    for ident in songs:
        await store.execute(
            "INSERT INTO placeholders (song_id,path,placeholder_path,track_ref,release_ref,"
            "title,artist,album,duration_ms,disc,track,tags,state,created_at)"
            " VALUES (?,?,?,?,'test:w','T','A','W',1000,1,1,'{}','placeholder',1)",
            [ident, "_shijhon/" + ident, "_shijhon/" + ident, "test:" + ident],
        )


@pytest.fixture
async def library(tmp_path: Path) -> Any:
    store = await Store.open(tmp_path / "written.db")
    service = Collection(store, FakeAccess([song(1, path="Original/1.flac")]))
    try:
        yield store, service
    finally:
        await store.close()


async def indexed(store: Store) -> dict[str, dict[str, Any]]:
    rows = await store.fetchall("SELECT * FROM collection_tracks WHERE present=1")
    return {str(r["song_id"]): dict(r) for r in rows}


async def generation(store: Store) -> str:
    row = await store.fetchone("SELECT generation FROM collection_index WHERE library_id='1'")
    return str(row["generation"]) if row else ""


async def test_new_placeholders_are_added_to_the_snapshot_once(library: Any) -> None:
    store, service = library
    navidrome = Navidrome({"p1": rest_song("p1"), "p2": rest_song("p2")})
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        written = Written(
            store, service.index, "1", navidrome, spawn, delay_seconds=0, check_seconds=0
        )
        await placeholders(store, ["p1"])
        await written.due(ONE, request())  # no snapshot yet: the first comes at a sign-in
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        assert not navidrome.asked and written.additions == 0
        await service.refresh("one", "1")
        before = await generation(store)
        await placeholders(store, ["p2"])
        await written.due(ONE, request())
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
    rows = await indexed(store)
    assert set(rows) == {"s1", "p1", "p2"} and written.additions == 1
    # In the snapshot's own generation: the lists cached for it stay valid.
    assert await generation(store) == before
    assert {r["generation"] for r in rows.values()} == {before}
    added = rows["p2"]
    assert (added["track_key"], added["original"], added["album_id"]) == (
        "catalog:test:p2",
        0,
        "w1",
    )
    assert (added["artist_id"], added["release_key"], added["duration"]) == ("rw", "test:w", 180)
    metadata = json.loads(added["metadata"])
    assert "starred" not in metadata and "playCount" not in metadata and "path" not in metadata
    # Asked as the app's account, with its own credentials and nothing else of its request.
    credentials = [("u", "listener"), ("t", "token"), ("s", "salt"), ("v", "1.16.1"),
                   ("c", "app"), ("f", "json")]  # fmt: skip
    assert sorted(dict(params)["id"] for params in navidrome.asked) == ["p1", "p2"]
    for params in navidrome.asked:
        assert sorted(p for p in params if p[0] != "id") == sorted(credentials)


async def test_checks_and_additions_are_bounded_in_time(library: Any) -> None:
    store, service = library
    await service.refresh("one", "1")
    navidrome = Navidrome({f"p{n}": rest_song(f"p{n}") for n in range(1, 4)})
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        written = Written(
            store, service.index, "1", navidrome, spawn, delay_seconds=0, check_seconds=1000
        )
        await placeholders(store, ["p1"])
        await written.due(ONE, request())
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        await placeholders(store, ["p2", "p3"])  # a bulk write
        for _ in range(5):
            await written.due(ONE, request())  # within the check interval: not looked at
        assert len(spawn.ended) == 1
        written.check_seconds = 0  # looked at, but within the gap after the last addition
        for _ in range(5):
            await written.due(ONE, request())
        assert len(spawn.ended) == 1
        written.gap_seconds, written._checked = 0, -float("inf")  # due: one addition
        await written.due(ONE, request())
        await written.due(ONE, request())  # one at a time
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        assert len(spawn.ended) == 2 and written.additions == 2
        await written.due(ONE, request())  # looked at beside the request: nothing missing
    assert len(spawn.ended) == 3 and written.additions == 2 and len(navidrome.asked) == 3
    assert set(await indexed(store)) == {"s1", "p1", "p2", "p3"}


async def test_songs_navidrome_does_not_give_are_not_asked_for_again(library: Any) -> None:
    store, service = library
    await service.refresh("one", "1")
    navidrome = Navidrome({"p1": rest_song("p1")})
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        written = Written(
            store, service.index, "1", navidrome, spawn, delay_seconds=0, check_seconds=0
        )
        written.gap_seconds = 0
        await placeholders(store, ["p1", "gone"])
        await written.due(ONE, request())
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        await written.due(ONE, request())
        await written.due(ELSEWHERE, request())  # an account without this library
    assert len(spawn.ended) == 2 and len(navidrome.asked) == 2
    assert set(await indexed(store)) == {"s1", "p1"}


async def test_the_index_keeps_what_it_has_and_refuses_another_identity(library: Any) -> None:
    store, service = library
    no_snapshot = await service.index.add("1", [native_shape(rest_song("p1"))])
    assert no_snapshot == ("", [])
    await service.refresh("one", "1")
    await placeholders(store, ["p1"])
    before = await generation(store)
    assert await service.index.add("1", [native_shape(rest_song("s1"))]) == (before, [])
    assert await service.index.add("1", [native_shape(rest_song("p1"))]) == (before, ["p1"])
    assert await generation(store) == before
    with pytest.raises(CollectionError):
        await service.index.add("1", [native_shape(rest_song(f"x{n}")) for n in range(501)])
    for alias, key in (("native:p9", "native:s1"), ("catalog:test:p9", "catalog:other")):
        await store.execute("INSERT INTO collection_track_aliases VALUES ('1',?,?)", [alias, key])
    await placeholders(store, ["p9"])
    with pytest.raises(CollectionError):
        await service.index.add("1", [native_shape(rest_song("p9"))])
    assert "p9" not in await indexed(store)


def test_a_subsonic_song_reads_as_a_native_one_without_path_or_annotations() -> None:
    shaped = native_shape(rest_song("p1", track=3, discNumber=1))
    assert "path" not in shaped and "starred" not in shaped and "playCount" not in shaped
    assert shaped["albumArtistId"] == "rw" and shaped["albumArtist"] == "Invented Writer"
    assert shaped["participants"] == {
        "artist": [{"id": "rw", "name": "Invented Writer"}],
        "albumartist": [{"id": "rw", "name": "Invented Writer"}],
        "composer": [{"id": "rc", "name": "Composer"}],
    }
    assert shaped["genres"] == [{"id": native_tag_id("genre", "Imaginary"), "name": "Imaginary"}]
    assert shaped["tags"] == {"genre": ["Imaginary"]}
    assert shaped["isrc"] == "ZZW000000001" and shaped["track"] == 3
    bare = native_shape({"id": "p2", "title": "T", "duration": 1})
    assert bare == {"id": "p2", "title": "T", "duration": 1, "participants": {}}


def listed_song(ident: str, album: str = "w1") -> dict[str, Any]:
    return {"id": ident, "title": ident, "albumId": album, "duration": 10}


async def reading(index: QueryIndex, binding: Binding, key: str, mode: str) -> list[str]:
    return [str(s["id"]) for s in (await index.page(key, binding, "song", mode, 0, 50)).rows]


async def test_an_addition_reaches_cached_facts_also_when_never_extended(
    tmp_path: Path,
) -> None:
    """The source's addition commits before the cached facts take it in;
    a stop between the two (no ``extend``) must not hide the song for good."""
    store = await Store.open(tmp_path / "written.db")
    service = Collection(store, FakeAccess([song(1, path="Original/1.flac")]))
    try:
        await Origins(store).initialize_legacy("1")
        await service.refresh("one", "1")
        binding = Binding("one", frozenset({"1"}), (("1", await generation(store)),))
        listed = [listed_song("s1", "a1")]

        async def fetch(start: int, limit: int) -> SourcePage:
            return SourcePage(listed[start : start + limit], len(listed))

        async with anyio.create_task_group() as group:
            index = QueryIndex(
                tmp_path / "cache" / "db", tmp_path / "written.db", group.start_soon,
                budgets=Budgets(wait_seconds=2),
            )  # fmt: skip
            key = await index.ensure(binding, "songs", fetch, valid)
            assert await reading(index, binding, key, "all") == ["s1"]  # facts built
            await placeholders(store, ["p1"])
            await store.execute("INSERT INTO collection_favorites VALUES ('one','1','song','p1')")
            assert (await service.index.add("1", [native_shape(rest_song("p1"))]))[1] == ["p1"]
            # ... and the process stops here: no extension. Navidrome lists p1 now.
            listed.append(listed_song("p1"))
            await index.invalidate()
            deadline = anyio.current_time() + 15  # (a list is refreshed at most every 5 s)
            while "p1" not in await reading(index, binding, key, "all"):
                assert anyio.current_time() < deadline, "the added song stayed out"
                await anyio.sleep(0.05)
                await index.ensure(binding, "songs", fetch, valid, wait=False)
            await index.close()
    finally:
        await store.close()


async def test_a_combined_binding_whose_library_was_empty_takes_additions_in(
    tmp_path: Path,
) -> None:
    """Facts are extended by the libraries their binding declares, also
    when the configured library had no songs and another library's edges are there."""
    store = await Store.open(tmp_path / "written.db")
    service = Collection(store, FakeAccess([]))
    try:
        await Origins(store).initialize_legacy("1")
        await service.refresh("one", "1")
        gen = await generation(store)
        assert gen
        binding = Binding("one", frozenset({"1", "2"}), (("1", gen),))
        async with anyio.create_task_group() as group:
            index = QueryIndex(tmp_path / "cache" / "db", tmp_path / "written.db", group.start_soon)
            assert await index.shown(binding, None, "song", ["f1"]) == frozenset()  # facts
            with sqlite3.connect(tmp_path / "cache" / "db") as cache:  # the other library's
                cache.execute(
                    "INSERT INTO edges VALUES (?,'song','f1','f1','2','fa',1,1,2020,'')",
                    (binding.facts,),
                )
            await placeholders(store, ["p1"])
            generation_, added = await service.index.add("1", [native_shape(rest_song("p1"))])
            assert added == ["p1"]
            await index.extend("1", generation_)
            with sqlite3.connect(tmp_path / "cache" / "db") as cache:
                edges = cache.execute(
                    "SELECT kind FROM edges WHERE facts=? AND song_id='p1' AND library_id='1'",
                    (binding.facts,),
                ).fetchall()
            assert {"song", "album"} <= {e[0] for e in edges}
            await index.close()
    finally:
        await store.close()


async def test_a_song_whose_placeholder_record_went_is_never_added_as_an_original(
    library: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer about a song whose placeholder record is gone - never
    there, of a release being taken out, or removed while the addition was prepared - is
    not added, and never as an original."""
    store, service = library
    await service.refresh("one", "1")
    index = service.index
    assert await index.add("1", [native_shape(rest_song("x1"))]) == (await generation(store), [])
    await placeholders(store, ["p1"])
    await store.execute("UPDATE releases SET removing_at=1 WHERE ref='test:w'")
    assert (await index.add("1", [native_shape(rest_song("p1"))]))[1] == []
    await store.execute("UPDATE releases SET removing_at=NULL WHERE ref='test:w'")
    rows = index._rows

    async def removed_meanwhile(*args: Any) -> Any:
        built = await rows(*args)
        await store.execute("DELETE FROM placeholders WHERE song_id='p1'")  # the cleanup
        return built

    monkeypatch.setattr(index, "_rows", removed_meanwhile)
    assert (await index.add("1", [native_shape(rest_song("p1"))]))[1] == []
    assert set(await indexed(store)) == {"s1"}
    assert not await store.fetchone("SELECT 1 FROM collection_index_additions")


class Refusing(Navidrome):
    """Answers every getSong with Navidrome's error ``code`` (or an HTTP ``status``)."""

    def __init__(self, code: int = 40, status: int = 200) -> None:
        super().__init__({})
        self.code, self.status = code, status

    async def send(self, method: str, path: bytes, query: bytes, *rest: Any) -> httpx.Response:
        self.asked.append(parse_qsl(query.decode()))
        await anyio.sleep(0.01)
        body = {"status": "failed", "error": {"code": self.code, "message": "refused"}}
        return httpx.Response(
            self.status,
            headers={"content-type": "application/json"},
            content=json.dumps({"subsonic-response": body}).encode(),
        )


@pytest.mark.parametrize(("code", "status", "again"), [(40, 200, True), (0, 503, False)])
async def test_a_refusal_or_failure_stops_the_addition_and_holds_nothing_against_the_songs(
    library: Any, code: int, status: int, again: bool
) -> None:
    """Credentials refused (code 40) or Navidrome failing (503) stop the
    whole addition after the reads under way; no song is skipped for later callers, and
    after a refusal another account's request may try again at once."""
    store, service = library
    await service.refresh("one", "1")
    songs = [f"p{n:03}" for n in range(40)]
    await placeholders(store, songs)
    refusing = Refusing(code, status)
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        written = Written(store, service.index, "1", refusing, spawn, delay_seconds=0,
                          check_seconds=0)  # fmt: skip
        await written.due(ONE, request())
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        assert len(refusing.asked) <= 4 and written.additions == 0
        assert written._skipped == {}
        good = Navidrome({ident: rest_song(ident) for ident in songs})
        written.upstream = good
        await written.due(ONE, request())  # another account's request, say
        if again:
            with anyio.fail_after(5):
                await spawn.ended[-1].wait()
            assert written.additions == 1 and len(good.asked) == len(songs)
        else:
            assert len(spawn.ended) == 1  # Navidrome's trouble: the gap holds
    if again:
        assert set(songs) <= set(await indexed(store))


async def test_a_song_navidrome_does_not_have_is_skipped_for_a_while(library: Any) -> None:
    store, service = library
    await service.refresh("one", "1")
    await placeholders(store, ["p1"])
    missing = Refusing(70)
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        written = Written(store, service.index, "1", missing, spawn, delay_seconds=0,
                          check_seconds=0, gap_seconds=0)  # fmt: skip
        await written.due(ONE, request())
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        assert set(written._skipped) == {"p1"}
        await written.due(ONE, request())
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
    assert len(missing.asked) == 1
