# ruff: noqa: S608 -- SQL comes from the validated shared policy.
"""Each account's built-in libraries, whole albums by their rules, and its hand choices.

Original imported library: the albums of the account's imports, of the playlists it
imported and of its favorites. Shijhon library: the catalog albums its activity brought in
(an app's play, favorite, rating, playlist, download, share or bookmark; a playlist
Shijhon published for it; audio it prepared) and, for the one account they were given to,
those carried over at the upgrade (``test_collection_carryover.py``). All:
the two together. In each, a hand addition or removal on the Collection page wins; All's
own hand choices come last. Custom libraries hold hand choices only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlencode

import anyio
import anyio.abc
import httpx
import pytest

from shijhon.access import Principal
from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    release_data,
)
from shijhon.collection import reasons as reasons_module
from shijhon.collection.contracts import Query
from shijhon.collection.history import History, queue_pending
from shijhon.collection.origins import Origins
from shijhon.collection.policy import (
    builtin_profile_id,
    default_profile_id,
    original_albums,
    predicate,
    shijhon_albums,
)
from shijhon.collection.reasons import Reasons
from shijhon.collection.service import Collection
from shijhon.proxy.params import RestCall
from shijhon.store import Store, apply_migrations
from shijhon.store.db import load_migrations
from shijhon.views.answers import LibraryAnswer
from shijhon.views.collection_requests import CollectionRequests
from tests.unit.test_collection import FakeAccess, finish, song
from tests.unit.test_collection_personal import credit, mutate

pytestmark = pytest.mark.anyio
SONGS = [f"s{n}" for n in range(1, 12)]


def library_rows() -> list[dict[str, Any]]:
    """a1: an original and two catalog songs; a2-a5: catalog songs only."""

    def catalog(n: int, album: str) -> dict[str, Any]:
        return song(n, path=f"_shijhon/{n}.flac", albumId=album, album=album.upper())

    return [
        song(1, path="Original/1.flac"),
        catalog(2, "a1"),
        catalog(3, "a1"),
        catalog(4, "a2"),  # its s4 is favorited
        catalog(5, "a2"),
        catalog(6, "a3"),  # the album is favorited
        catalog(7, "a3"),
        catalog(8, "a4"),  # its s8 is in an imported playlist
        catalog(9, "a4"),
        catalog(10, "a5"),  # no reason for Original; played for Shijhon library
        catalog(11, "a5"),
    ]


async def open_library(path: Path, *, personal: bool = False) -> tuple[Store, Collection]:
    store = await Store.open(path)
    service = Collection(store, FakeAccess(library_rows()))
    if personal:
        await Origins(store).initialize_personal("1")
    else:
        await Origins(store).initialize_legacy("1")
    await service.refresh("one", "1")
    return store, service


async def favorite(store: Store, owner: str, kind: str, item: str) -> None:
    await store.execute("INSERT INTO collection_favorites VALUES (?,'1',?,?)", [owner, kind, item])


async def imported(store: Store, owner: str, songs: list[str], mix: str = "") -> None:
    mix = mix or "import-" + owner
    await store.execute(
        "INSERT OR IGNORE INTO playlist_owners(id,username,connection,generation)"
        " VALUES (?,?,'{}','g')",
        [owner, owner],
    )
    await store.execute(
        "INSERT OR IGNORE INTO playlist_mixes(id,owner,definition) VALUES (?,?,'{}')",
        [mix, owner],
    )
    for ident in songs:
        await store.execute(
            "INSERT INTO collection_imported VALUES (?,'1',?,?)", [owner, mix, ident]
        )


async def played(store: Store, owner: str, *songs: str, activity: int = 1) -> None:
    """The account's activity on these catalog songs, as ``History.record_catalog`` keeps
    it (``activity`` 0: a choice on the Collection page, as ``record_positive``)."""
    for ident in songs:
        await store.execute(
            "INSERT INTO collection_history(owner_id,library_id,track_key,added_at,activity)"
            " VALUES (?,'1',?,?,?)"
            " ON CONFLICT DO UPDATE SET activity=max(activity,excluded.activity)",
            [owner, "catalog:" + ident, time.time(), activity],
        )


async def reasons_of_one(store: Store) -> None:
    await favorite(store, "one", "song", "s4")
    await favorite(store, "one", "album", "a3")
    await imported(store, "one", ["s8"])


async def visible(service: Collection, owner: str = "one", profile: str = "all") -> set[str]:
    return set(await service.visible_song_ids(owner, "1", SONGS, profile_id=profile))


@pytest.fixture
async def legacy(tmp_path: Path) -> Any:
    store, service = await open_library(tmp_path / "reasons.db")
    await reasons_of_one(store)
    try:
        yield store, service
    finally:
        await store.close()


ORIGINAL_OF_ONE = {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"}
ALL_OF_ONE = ORIGINAL_OF_ONE  # nothing played yet


async def test_each_built_in_library_holds_whole_albums_by_its_rule(legacy: Any) -> None:
    store, service = legacy
    assert await visible(service, profile="original") == ORIGINAL_OF_ONE
    assert await visible(service, profile="shijhon") == set()
    assert await visible(service) == ORIGINAL_OF_ONE
    rows = await store.fetchall(original_albums("one", "1").sql, original_albums("one", "1").params)
    assert sorted(r[0] for r in rows) == ["a1", "a2", "a3", "a4"]  # one row per album
    # One play of a catalog song brings its whole album into Shijhon library and All.
    await played(store, "one", "s10")
    assert await visible(service, profile="shijhon") == {"s10", "s11"}
    assert await visible(service) == ORIGINAL_OF_ONE | {"s10", "s11"}
    assert await visible(service, profile="original") == ORIGINAL_OF_ONE
    rows = await store.fetchall(shijhon_albums("one", "1").sql, shijhon_albums("one", "1").params)
    assert [r[0] for r in rows] == ["a5"]
    # A choice on the Collection page is not activity: it brings no album in by itself.
    await played(store, "one", "s2", activity=0)
    assert await visible(service, profile="shijhon") == {"s10", "s11"}
    # Custom libraries hold what they started with, and change only by hand.
    based = await service.create_profile("one", "Originals", start="original")
    empty = await service.create_profile("one", "Empty", start="empty")
    assert await visible(service, profile=based) == ORIGINAL_OF_ONE
    assert await visible(service, profile=empty) == set()
    await favorite(store, "one", "song", "s11")
    assert await visible(service, profile=based) == ORIGINAL_OF_ONE
    # Another account's favorites, imports and plays bring it nothing: only the shared
    # original's album.
    await favorite(store, "two", "song", "s10")
    await imported(store, "two", ["s11"])
    await played(store, "two", "s4")
    assert await visible(service, "two", "original") == {"s1", "s2", "s3", "s10", "s11"}
    assert await visible(service, "two", "shijhon") == {"s4", "s5"}
    assert await visible(service, "two") == {"s1", "s2", "s3", "s4", "s5", "s10", "s11"}
    assert await visible(service) == ORIGINAL_OF_ONE | {"s10", "s11"}
    assert await visible(service, "new") == {"s1", "s2", "s3"}  # the shared original
    assert await visible(service, "new", "shijhon") == set()


async def test_the_predicate_counts_and_pages_with_the_rules(legacy: Any) -> None:
    store, _service = legacy
    await played(store, "one", "s10")
    pred = predicate("one", "1", song_expression="t.song_id", mode="all")
    sql = " FROM collection_tracks t WHERE " + pred.sql
    assert (await store.fetchone("SELECT count(*) n" + sql, pred.params))["n"] == 11
    page = await store.fetchall(
        "SELECT t.song_id" + sql + " ORDER BY t.song_id LIMIT 2 OFFSET 3", pred.params
    )
    assert [r["song_id"] for r in page] == ["s2", "s3"]  # (s1, s10, s11 first)
    for mode, count in (("saved", 2), ("original", 9)):
        other = predicate("one", "1", song_expression="t.song_id", mode=mode)  # type: ignore[arg-type]
        query = "SELECT count(*) n" + sql.replace(pred.sql, other.sql)
        assert (await store.fetchone(query, other.params))["n"] == count


async def test_original_follows_favorites_and_imports_while_activity_stays(legacy: Any) -> None:
    store, service = legacy
    await played(store, "one", "s4")  # a play of the favorite's song
    await store.execute("DELETE FROM collection_favorites WHERE item_id='s4'")
    await store.execute("DELETE FROM playlist_mixes WHERE id='import-one'")  # cascades
    assert not await store.fetchone("SELECT 1 FROM collection_imported")
    assert await visible(service, profile="original") == {"s1", "s2", "s3", "s6", "s7"}
    # The play keeps its album in Shijhon library, and so in All.
    assert await visible(service, profile="shijhon") == {"s4", "s5"}
    assert await visible(service) == {"s1", "s2", "s3", "s4", "s5", "s6", "s7"}
    # A favorite of a song that is not in the library index brings in no album; neither
    # does a song no longer in the library.
    await favorite(store, "one", "song", "never-indexed")
    await store.execute("UPDATE collection_tracks SET present=0 WHERE song_id IN ('s6','s4')")
    await favorite(store, "one", "song", "s6")
    await store.execute("DELETE FROM collection_favorites WHERE kind='album'")
    assert await visible(service, profile="original") == {"s1", "s2", "s3"}
    assert await visible(service) == {"s1", "s2", "s3"}


async def test_personal_mode_counts_only_the_account_s_own_originals(tmp_path: Path) -> None:
    store, service = await open_library(tmp_path / "personal.db", personal=True)
    try:
        assert await visible(service) == set()
        await credit(service, "one", 1)
        assert await visible(service) == {"s1", "s2", "s3"}
        assert await visible(service, "two") == set()
        await imported(store, "two", ["s10"])
        assert await visible(service, "two") == {"s10", "s11"}
        await played(store, "two", "s2")
        assert await visible(service, "two", "shijhon") == {"s1", "s2", "s3"}  # whole album
        assert await visible(service) == {"s1", "s2", "s3"}
    finally:
        await store.close()


async def test_a_hand_choice_wins_in_its_library_and_all_follows(legacy: Any) -> None:
    store, service = legacy
    await played(store, "one", "s10", "s6")
    # Removed from All: out of All only.
    removal, reviewed, applied = await mutate(service, selected=["catalog:s2"], action="remove")
    assert reviewed["affected"] == applied["changed"] == 1
    assert (await service.operation("one", removal))["selection"] == "all"
    assert "s2" not in await visible(service)
    assert "s2" in await visible(service, profile="original")
    # Removed from Original: All shows Original as left - unless Shijhon library holds it.
    await mutate(
        service, profile="original", selected=["catalog:s4", "catalog:s7"], action="remove"
    )
    assert await visible(service, profile="original") == ORIGINAL_OF_ONE - {"s4", "s7"}
    assert "s4" not in await visible(service) and "s7" in await visible(service)
    # Removed from Shijhon library too: out of All.
    await mutate(service, profile="shijhon", selected=["catalog:s7"], action="remove")
    assert await visible(service, profile="shijhon") == {"s6", "s10", "s11"}
    assert "s7" not in await visible(service)
    # Added by hand to Original: in All, a song (not its album).
    await mutate(service, profile="original", selected=["catalog:s9"])
    # A removal wins over the account's own imports too, in its library only.
    await mutate(service, selected=["native:s1"], action="remove")
    assert "s1" not in await visible(service) and "s1" in await visible(service, profile="original")
    expected = (ORIGINAL_OF_ONE | {"s10", "s11"}) - {"s1", "s2", "s4", "s7"}
    assert await visible(service) == expected
    # All's own addition wins over a removal elsewhere.
    await mutate(service, selected=["catalog:s7"])
    assert await visible(service) == expected | {"s7"}
    undo = await finish(service, await service.undo("one", removal))
    assert undo["changed"] == 1 and undo["selection"] == "all"
    assert await visible(service) == expected | {"s7", "s2"}
    row = await store.fetchone(
        "SELECT saved FROM collection_membership WHERE track_key='catalog:s2' AND profile_id=?",
        [default_profile_id("one")],
    )
    assert row["saved"] is None
    # Each library's choices are in a profile of its own.
    profiles = {
        r["profile_id"]
        for r in await store.fetchall("SELECT profile_id FROM collection_membership")
    }
    assert profiles == {builtin_profile_id("one", s) for s in ("all", "original", "shijhon")}


async def test_review_and_change_count_the_songs_a_rule_shows(legacy: Any) -> None:
    store, service = legacy
    query = Query(view="all", scope="album")
    operation, reviewed, applied = await mutate(
        service, query=query, selected=["a2"], action="remove"
    )
    # Both songs of the favorite's album leave All: the review and the change say so.
    assert reviewed["total"] == reviewed["affected"] == applied["changed"] == 2
    assert await visible(service) == ALL_OF_ONE - {"s4", "s5"}
    undo = await finish(service, await service.undo("one", operation))
    assert undo["changed"] == 2 and undo["skipped"] == 0
    assert await visible(service) == ALL_OF_ONE
    # Adding what a rule already shows changes nothing anyone sees.
    _, reviewed, applied = await mutate(service, query=query, selected=["a3"])
    assert reviewed["total"] == 2 and reviewed["affected"] == applied["changed"] == 0
    # Shijhon library never held these songs: removing them there changes nothing.
    ident = await service.prepare(
        "one",
        "1",
        query,
        profile_id="shijhon",
        action="remove",
        selected=["a4"],
        request_key="shijhon-remove",
    )
    frozen = await finish(service, ident)
    assert frozen["selection"] == "shijhon" and frozen["total"] == 2 and frozen["affected"] == 0
    assert frozen["profile_id"] == builtin_profile_id("one", "shijhon")
    await played(store, "one", "s10")
    _, reviewed, applied = await mutate(
        service, query=query, profile="shijhon", selected=["a5"], action="remove"
    )
    assert reviewed["affected"] == applied["changed"] == 2
    assert "s10" not in await visible(service)
    custom = await service.create_profile("one", "Custom")
    ident = await service.prepare(
        "one", "1", query, profile_id=custom, action="save", selected=["a5"], request_key="c"
    )
    assert (await finish(service, ident))["selection"] == ""


async def test_the_collection_page_shows_each_library_and_what_you_collected(
    legacy: Any,
) -> None:
    store, service = legacy
    await played(store, "one", "s10")

    async def keys(view: str, pool: str = "personal", profile: str = "all") -> set[str]:
        listing = await service.browse(
            "one", "1", Query(view=view, scope="song", pool=pool), profile_id=profile
        )
        return {row["item_key"] for row in listing["rows"]}

    in_all = {"native:s1"} | {f"catalog:s{n}" for n in range(2, 12)}
    assert await keys("saved") == in_all
    listing = await service.browse(
        "one", "1", Query(view="saved", scope="song", pool="personal"), profile_id="all"
    )
    assert listing["counts"] == {"all_count": 11, "saved_count": 11, "unsaved_count": 0}
    assert listing["sources"] == {"imported_count": 1, "catalog_count": 10}
    # Not in this library: the music the account collected, here All's two libraries.
    assert await keys("unsaved") == set()
    assert await keys("unsaved", profile="original") == {"catalog:s10", "catalog:s11"}
    assert await keys("unsaved", profile="shijhon") == in_all - {"catalog:s10", "catalog:s11"}
    assert await keys("saved", profile="shijhon") == {"catalog:s10", "catalog:s11"}
    # Everything on the server: also what nobody's rule brought in for this account.
    await store.execute("UPDATE collection_history SET owner_id='two'")
    assert await keys("unsaved") == set()
    assert await keys("unsaved", "server") == {"catalog:s10", "catalog:s11"}
    await mutate(service, selected=["catalog:s5"], action="remove")
    assert await keys("unsaved") == {"catalog:s5"}  # removed, still the account's music
    assert await keys("saved") == in_all - {"catalog:s5", "catalog:s10", "catalog:s11"}
    albums = await service.browse(
        "one", "1", Query(view="all", scope="album", album_id="a2"), profile_id="all"
    )
    [row] = albums["rows"]
    assert row["all_tracks"] == 2 and row["full_saved"] == 1 and row["editable"] == 1
    for origin, expected in (("imported", {"native:s1"}), ("catalog", in_all - {"native:s1"})):
        listing = await service.browse(
            "one",
            "1",
            Query(view="all", scope="song", pool="server", origin=origin),
            profile_id="all",
        )
        assert {row["item_key"] for row in listing["rows"]} == expected


async def test_a_copy_holds_what_its_library_holds_as_its_own_choices(legacy: Any) -> None:
    store, service = legacy
    await played(store, "one", "s10")
    await mutate(service, selected=["catalog:s2"], action="remove")
    await mutate(service, selected=["native:s1"], action="remove")
    expected = await visible(service)
    copy = await service.create_profile("one", "Copy", start="copy", source="all")
    assert await visible(service, profile=copy) == expected
    rows = {
        r["track_key"]: r["saved"]
        for r in await store.fetchall(
            "SELECT track_key,saved FROM collection_membership WHERE profile_id=?", [copy]
        )
    }
    assert rows["catalog:s2"] == 0 and rows["catalog:s10"] == 1 and rows["catalog:s3"] == 1
    assert rows["native:s1"] == 0  # removed by hand over the copy's base of imports
    # The copy is explicit: a later favorite or play changes All, not the copy.
    await favorite(store, "one", "song", "s11")
    await played(store, "one", "s6")
    assert await visible(service) == expected
    assert await visible(service, profile=copy) == expected
    await store.execute("DELETE FROM collection_favorites WHERE kind='album'")
    assert await visible(service, profile=copy) == expected
    # Copies of the other two hold what they hold.
    for source in ("shijhon", "original"):
        held = await visible(service, profile=source)
        made = await service.create_profile("one", "Copy " + source, start="copy", source=source)
        assert await visible(service, profile=made) == held
    assert (await service.profile("one"))["id"] == default_profile_id("one")


async def test_a_copy_of_all_after_remove_and_undo_keeps_what_all_shows(legacy: Any) -> None:
    """Remove then Undo leaves a NULL choice, and All shows the song by its album's rule
    again; a copy of All holds it too. A removal still in place stays one."""
    store, service = legacy
    removal, _, _ = await mutate(service, selected=["catalog:s5"], action="remove")
    await finish(service, await service.undo("one", removal))
    await mutate(service, selected=["catalog:s3"], action="remove")
    row = await store.fetchone(
        "SELECT saved FROM collection_membership WHERE track_key='catalog:s5'"
    )
    assert row is not None and row["saved"] is None
    expected = await visible(service)
    assert "s5" in expected and "s3" not in expected
    copy = await service.create_profile("one", "After undo", start="copy", source="all")
    assert await visible(service, profile=copy) == expected
    rows = {
        r["track_key"]: r["saved"]
        for r in await store.fetchall(
            "SELECT track_key,saved FROM collection_membership WHERE profile_id=?", [copy]
        )
    }
    assert rows["catalog:s5"] == 1 and rows["catalog:s3"] == 0


async def test_built_in_names_and_profiles_are_kept_apart(legacy: Any) -> None:
    _store, service = legacy
    for name in ("Shijhon library", "original imported library", "ALL"):
        with pytest.raises(Exception, match=r"built-in|different"):
            await service.create_profile("one", name)
    for selection in ("all", "original", "shijhon"):
        profile = await service.profile("one", selection)
        assert profile["id"] == builtin_profile_id("one", selection) and profile["builtin"]
        # Its membership profile's ID names the same library.
        assert (await service.profile("one", profile["id"]))["selection_id"] == selection
        with pytest.raises(Exception, match="Built-in"):
            await service.archive_profile("one", profile["id"])


def test_the_migration_records_the_songs_imports_published_before() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    before = next(i for i, (_, name, _) in enumerate(migrations) if name.startswith("0020_"))
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    assert apply_migrations(conn, migrations[:before]) == before
    conn.execute(
        "INSERT INTO playlist_owners(id,username,connection,generation)"
        " VALUES ('owner','admin','{}','g')"
    )
    for mix in ("imported", "described", "broken", "unpublished"):
        conn.execute(
            "INSERT INTO playlist_mixes(id,owner,definition) VALUES (?,'owner','{}')", [mix]
        )
    for mix in ("imported", "broken", "unpublished"):
        conn.execute(
            "INSERT INTO playlist_imports(id,owner,library,request_key,input_hash,input)"
            " VALUES (?,'owner','1',?,'h','')",
            [mix, mix],
        )
    for ident, mix, created, state, data in (
        ("first", "imported", 1, "published", {"native_ids": ["old-1", "both"]}),
        ("last", "imported", 2, "published", {"native_ids": ["both", "new-1", "both", ""]}),
        ("later", "imported", 3, "failed", {"native_ids": ["failed-1"]}),
        ("generated", "described", 1, "published", {"native_ids": ["described-1"]}),
        ("unpublished", "unpublished", 1, "preview", {"native_ids": ["preview-1"]}),
    ):
        conn.execute(
            "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,?,?)",
            [ident, mix, created, state, json.dumps(data)],
        )
    conn.execute(
        "INSERT INTO playlist_runs(id,mix,created,state,data)"
        " VALUES ('bad','broken',1,'published','not json')"
    )
    conn.execute(
        "INSERT INTO collection_profiles(id,owner_id,name,created) VALUES ('p','owner','P',0)"
    )
    conn.execute(
        "INSERT INTO collection_operations(id,owner_id,library_id,profile_id,request_key,"
        "fingerprint,action,state,query,selected,created) VALUES"
        " ('op','owner','1','p','k','f','save','done','{}','[]',0)"
    )
    assert apply_migrations(conn, migrations) == migrations[-1][0]
    rows = conn.execute(
        "SELECT owner_id,library_id,import_id,song_id FROM collection_imported ORDER BY song_id"
    ).fetchall()
    assert rows == [("owner", "1", "imported", "both"), ("owner", "1", "imported", "new-1")]
    assert conn.execute("SELECT selection FROM collection_operations").fetchone() == ("",)
    for table in ("collection_favorites", "collection_favorites_read"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    conn.execute("DELETE FROM playlist_owners WHERE id='owner'")
    assert conn.execute("SELECT count(*) FROM collection_imported").fetchone() == (0,)


# --- the review's reproductions (2026-10-26) ----------------------------------------------


async def ledger(store: Store) -> None:
    """Shijhon's placeholder ledger for the catalog songs of ``library_rows``: each album a
    release ``demo:<album>``, each song the recording ``demo:t<n>``."""
    albums: dict[str, list[int]] = {}
    for row in library_rows():
        if row["path"].startswith("_shijhon/"):
            albums.setdefault(row["albumId"], []).append(int(row["id"][1:]))
    for album, numbers in albums.items():
        ref = CatalogRef("demo", album)
        release = CatalogRelease(
            ref,
            album.upper(),
            "Glass Orchard",
            ReleaseKind.ALBUM,
            "2020",
            tuple(
                CatalogTrack(
                    CatalogRef("demo", f"t{n}"),
                    f"Song {n}",
                    "Glass Orchard",
                    180000,
                    number=i + 1,
                    album=ref,
                    album_title=album.upper(),
                )
                for i, n in enumerate(numbers)
            ),
        )
        await store.execute(
            "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
            " VALUES (?,?,?,?,'Glass Orchard','{}',?,1)",
            [
                str(ref),
                "_shijhon/" + album,
                album,
                album.upper(),
                json.dumps(release_data(release)),
            ],
        )
        for n in numbers:
            await store.execute(
                "INSERT INTO track_links VALUES (?,?,?,0)", [f"demo:t{n}", f"s{n}", str(ref)]
            )


def requests(store: Store) -> CollectionRequests:
    """The recorder of accepted actions (its queue's ``drain``), without a Navidrome."""
    commits = SimpleNamespace(navidrome=SimpleNamespace(library_id=1))
    return CollectionRequests(commits, None, History(store))  # type: ignore[arg-type]


async def test_a_copy_stays_as_made_when_an_import_comes_later(tmp_path: Path) -> None:
    store, service = await open_library(tmp_path / "custom.db", personal=True)
    try:
        copied = await service.create_profile("one", "Fixed copy", start="original")
        assert await visible(service, profile=copied) == set()
        await credit(service, "one", 1)
        assert await visible(service, profile="original") == {"s1", "s2", "s3"}
        assert await visible(service, profile=copied) == set()  # only by hand
        row = await store.fetchone("SELECT base_original FROM collection_profiles WHERE id=?",
                                   [copied])  # fmt: skip
        assert row["base_original"] == 0
    finally:
        await store.close()


async def test_a_song_added_by_hand_brings_its_whole_album(tmp_path: Path) -> None:
    store, service = await open_library(tmp_path / "hand.db", personal=True)
    try:
        _, reviewed, applied = await mutate(service, profile="shijhon", selected=["catalog:s10"])
        assert await visible(service, profile="shijhon") == {"s10", "s11"}
        assert reviewed["affected"] == 2 and applied["changed"] == 1  # the review counts both
        assert await visible(service) == {"s10", "s11"}
        # A song removed by hand stays out: the album may be partial then.
        await mutate(service, profile="shijhon", selected=["catalog:s11"], action="remove")
        assert await visible(service, profile="shijhon") == {"s10"}
        # In a custom library too.
        custom = await service.create_profile("one", "Mine", start="empty")
        await mutate(service, profile=custom, selected=["catalog:s4"])
        assert await visible(service, profile=custom) == {"s4", "s5"}
    finally:
        await store.close()


async def test_a_favorite_read_from_navidrome_also_enters_shijhon_library(tmp_path: Path) -> None:
    store, service = await open_library(tmp_path / "favorite.db", personal=True)
    try:
        await ledger(store)
        learning = Reasons(store, "1", None, lambda _: False)  # type: ignore[arg-type]
        told: list[bool] = []
        learning.learned = lambda: told.append(True)

        async def starred() -> set[tuple[str, str]]:
            return {("song", "s4")}

        async def none() -> set[tuple[str, str]]:
            return set()

        await learning._read("one", starred)
        assert told == [True]
        await requests(store).drain()
        assert await visible(service, profile="original") == {"s4", "s5"}
        assert await visible(service, profile="shijhon") == {"s4", "s5"}
        assert not await store.fetchone("SELECT 1 FROM collection_pending")
        # Taken back: out of Original imported library; Shijhon library keeps it, and All.
        await learning._read("one", none)
        assert await visible(service, profile="original") == set()
        assert await visible(service) == {"s4", "s5"}
        # Read again, a favorite already recorded is not kept again.
        await learning._read("one", starred)
        await learning._read("one", starred)
        assert (await store.fetchone("SELECT count(*) n FROM collection_pending"))["n"] == 1
    finally:
        await store.close()


async def test_a_hand_removal_from_all_wins_over_an_own_import(tmp_path: Path) -> None:
    store, service = await open_library(tmp_path / "remove.db", personal=True)
    try:
        await credit(service, "one", 1)
        await mutate(service, action="remove", selected=["native:s1"])
        assert "s1" not in await visible(service)
        assert "s1" in await visible(service, profile="original")
    finally:
        await store.close()


async def test_kept_actions_are_recorded_and_a_failed_set_stays_to_try_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service = await open_library(tmp_path / "kept.db", personal=True)
    try:
        await ledger(store)
        async with store.transaction() as db:
            items = [("songId", "s4"), ("albumId", "a5"), ("songId", "s1"), ("id", "unknown")]
            await queue_pending(db, "one", "1", items)
        recorder = requests(store)
        record = recorder.history.record_catalog

        async def failing(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("transient")

        monkeypatch.setattr(recorder.history, "record_catalog", failing)
        await recorder.drain()
        rows = await store.fetchall("SELECT attempts,next_at FROM collection_pending")
        # The two that record nothing went; the others wait about a minute.
        assert len(rows) == 2 and {r["attempts"] for r in rows} == {1}
        assert all(r["next_at"] > time.time() + 30 for r in rows)
        assert await visible(service, profile="shijhon") == set()
        monkeypatch.setattr(recorder.history, "record_catalog", record)
        await recorder.drain()  # not due yet
        assert (await store.fetchone("SELECT count(*) n FROM collection_pending"))["n"] == 2
        await store.execute("UPDATE collection_pending SET next_at=0")  # a minute later
        await recorder.drain()
        assert not await store.fetchone("SELECT 1 FROM collection_pending")
        # The songs' and the album's recordings, as activity; the original records nothing.
        assert await visible(service, profile="shijhon") == {"s4", "s5", "s10", "s11"}
    finally:
        await store.close()


async def test_an_app_action_on_more_than_500_songs_is_kept_and_recorded(tmp_path: Path) -> None:
    """Review finding B3: an accepted playlist of more songs than one request confirms is
    not dropped. Its distinct songs are kept and recorded from the ledger; the same song
    many times is one song, confirmed with the request."""
    store, service = await open_library(tmp_path / "large.db", personal=True)
    try:
        await ledger(store)
        recorder = requests(store)
        accepted = LibraryAnswer(
            200,
            [(b"content-type", b"application/json")],
            json.dumps({"subsonic-response": {"status": "ok"}}).encode(),
        )

        async def answer(*args: Any) -> LibraryAnswer:
            return accepted

        recorder._answer = answer  # type: ignore[method-assign]
        principal = Principal("one", "listener", False, frozenset({1}))
        caller = AsyncMock(return_value=object())
        context = SimpleNamespace(head=False, kept={"shijhon.principal": principal}, caller=caller)
        songs = [f"native-{n}" for n in range(600)] + ["s4", "s10"]
        call = RestCall.build(
            "createPlaylist",
            "GET",
            b"/rest/createPlaylist",
            urlencode([("name", "Large"), *(("songId", song) for song in songs)]).encode(),
            [],
            None,
        )
        await recorder(call, context, None)  # type: ignore[arg-type]
        assert (await store.fetchone("SELECT count(*) n FROM collection_pending"))["n"] == 602
        await recorder.drain()
        assert not await store.fetchone("SELECT 1 FROM collection_pending")
        assert await visible(service, profile="shijhon") == {"s4", "s5", "s10", "s11"}
    finally:
        await store.close()


# --- the follow-up review's reproductions (2026-10-26) ---------------------------------


async def act(recorder: CollectionRequests, method: str, pairs: list[tuple[str, str]]) -> None:
    """An app's ``method`` request Navidrome accepted, as the recorder sees it."""

    async def accepted(*args: Any) -> LibraryAnswer:
        return LibraryAnswer(
            200,
            [(b"content-type", b"application/json")],
            b'{"subsonic-response":{"status":"ok"}}',
        )

    recorder._answer = accepted  # type: ignore[method-assign]
    principal = Principal("one", "listener", False, frozenset({1}))
    caller = AsyncMock(return_value=object())
    context = SimpleNamespace(head=False, kept={"shijhon.principal": principal}, caller=caller)
    call = RestCall.build(
        method, "POST", ("/rest/" + method).encode(), urlencode(pairs).encode(), [], None
    )
    await recorder(call, context, None)  # type: ignore[arg-type]


async def test_a_large_playlist_update_and_a_failed_confirmation_are_kept(
    tmp_path: Path,
) -> None:
    """Follow-up review R2-B1: songs added to a playlist (``songIdToAdd``) and a played
    song (``mediaId``) are kept under the queue's own names, not dropped."""
    store, service = await open_library(tmp_path / "update.db", personal=True)
    try:
        await ledger(store)
        recorder = requests(store)
        adding = [("songIdToAdd", f"native-{n}") for n in range(500)] + [("songIdToAdd", "s4")]
        await act(recorder, "updatePlaylist", [("playlistId", "list"), *adding])
        await recorder.drain()
        assert await visible(service, profile="shijhon") == {"s4", "s5"}
        recorder._verify = AsyncMock(side_effect=TimeoutError)  # type: ignore[method-assign]
        await act(recorder, "reportPlayback", [("mediaId", "s10"), ("state", "playing")])
        await recorder.drain()
        assert await visible(service, profile="shijhon") == {"s4", "s5", "s10", "s11"}
    finally:
        await store.close()


async def test_a_failing_item_waits_and_comes_back_without_holding_up_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up review R2-S1: an item that keeps failing is tried again later, at growing
    intervals up to an hour, never given up; a start of Shijhon or the same action again
    makes it due at once; the items beside it are recorded meanwhile."""
    store, service = await open_library(tmp_path / "retry.db", personal=True)
    try:
        await ledger(store)
        recorder = requests(store)
        record = recorder.history.record_catalog

        async def failing_s4(actor: Any, library: str, tracks: Any, **kwargs: Any) -> None:
            if any(t.ref.id == "t4" for t in tracks):
                raise RuntimeError("temporary")
            await record(actor, library, tracks, **kwargs)

        monkeypatch.setattr(recorder.history, "record_catalog", failing_s4)
        await recorder._queue("one", [("songId", "s4"), ("songId", "s10")])
        for _ in range(30):
            await store.execute("UPDATE collection_pending SET next_at=0")  # time passes
            await recorder.drain()
        assert await visible(service, profile="shijhon") == {"s10", "s11"}  # its neighbor
        row = await store.fetchone("SELECT attempts,next_at FROM collection_pending")
        assert row["attempts"] == 30
        assert time.time() + 3000 < row["next_at"] <= time.time() + 3600  # an hour at most
        await recorder.drain()  # not due: not tried
        assert (await store.fetchone("SELECT attempts FROM collection_pending"))["attempts"] == 30
        monkeypatch.setattr(recorder.history, "record_catalog", record)
        # The same action again makes it due, and it is recorded.
        await recorder._queue("one", [("songId", "s4")])
        await recorder.drain()
        assert await visible(service, profile="shijhon") == {"s4", "s5", "s10", "s11"}
        assert not await store.fetchone("SELECT 1 FROM collection_pending")
        # A start makes a waiting item due once more.
        monkeypatch.setattr(recorder.history, "record_catalog", failing_s4)
        await store.execute("DELETE FROM collection_history WHERE track_key='catalog:s4'")
        await recorder._queue("one", [("songId", "s4")])
        await recorder.drain()
        assert (await store.fetchone("SELECT next_at FROM collection_pending"))["next_at"] > 0
        monkeypatch.setattr(recorder.history, "record_catalog", record)
        started = requests(store)
        with anyio.move_on_after(1):
            await started.run()
        assert not await store.fetchone("SELECT 1 FROM collection_pending")
    finally:
        await store.close()


async def test_every_new_favorite_of_a_large_read_is_kept(tmp_path: Path) -> None:
    """Follow-up review R2-S2: a read of more than 10,000 new favorites keeps every one to
    record, not the first 10,000 (the next read no longer sees them as new)."""
    store, service = await open_library(tmp_path / "many.db", personal=True)
    try:
        await ledger(store)
        learning = Reasons(store, "1", None, lambda _: False)  # type: ignore[arg-type]

        async def starred() -> set[tuple[str, str]]:
            return {("song", f"a{n:05}") for n in range(10_000)} | {("song", "s4")}

        await learning._read("one", starred)
        assert (await store.fetchone("SELECT count(*) n FROM collection_pending"))["n"] == 10_001
        await requests(store).drain()
        await learning._read("one", starred)
        await requests(store).drain()
        assert await visible(service, profile="original") == {"s4", "s5"}
        assert await visible(service, profile="shijhon") == {"s4", "s5"}
    finally:
        await store.close()


def test_the_upgrade_keeps_every_custom_library_s_songs_also_after_undo() -> None:
    """Follow-up review R2-B2: a custom library holds after the upgrade exactly what it
    held before - with and without the import base, with explicit choices, with choices an
    Undo left empty (NULL), for two accounts."""
    conn, migrations = schema23()
    conn.execute("INSERT INTO collection_library_policy VALUES ('1','personal',1,NULL)")
    conn.execute("INSERT INTO collection_index VALUES ('1','g','ready',16,1,'')")
    for owner in ("one", "two"):
        for base in (0, 1):
            conn.execute(
                "INSERT INTO collection_profiles(id,owner_id,kind,name,base_original,created)"
                " VALUES (?,?,'custom',?,?,0)",
                [owner + str(base), owner, "copy" + str(base), base],
            )
    for n in range(16):
        key = f"song:{n}"
        conn.execute(
            "INSERT INTO collection_tracks(library_id,track_key,song_id,release_key,album_id,"
            "artist_id,title,album,artist,search_text,original,generation,indexed_at)"
            " VALUES ('1',?,?,'rel',?,'artist','Title','Album','Artist','title',?,'g',0)",
            [key, f"s{n}", f"a{n // 4}", n % 3 == 0],
        )
        if n % 2 == 0:  # imports (and links) of one account or the other
            conn.execute(
                "INSERT INTO collection_originals VALUES (?,'1',?,?,0)",
                ["one" if n % 4 == 0 else "two", key, "link:import" if n % 3 else "receipt:x"],
            )
    for owner in ("one", "two"):
        for base in (0, 1):
            for n, saved in ((1, 1), (2, 0), (3, None), (6, 0), (9, 1)):
                conn.execute(
                    "INSERT INTO collection_membership VALUES (?,?,'1',?,?,2,'prior')",
                    [owner, owner + str(base), f"song:{n}", saved],
                )

    def before(owner: str, profile: str) -> set[str]:
        rows = conn.execute(
            "SELECT t.song_id FROM collection_tracks t"
            " JOIN collection_profiles p ON p.id=? AND p.owner_id=?"
            " LEFT JOIN collection_membership m ON m.owner_id=p.owner_id AND m.profile_id=p.id"
            " AND m.library_id=t.library_id AND m.track_key=t.track_key"
            " LEFT JOIN collection_originals o ON o.owner_id=p.owner_id"
            " AND o.library_id=t.library_id AND o.track_key=t.track_key"
            " WHERE t.present=1 AND COALESCE(m.saved,((t.original=1 AND o.owner_id IS NOT NULL)"
            " OR coalesce(o.provenance_id,'') GLOB 'link:*')*p.base_original)=1",
            [profile, owner],
        )
        return {r[0] for r in rows}

    held = {(o, o + str(b)): before(o, o + str(b)) for o in ("one", "two") for b in (0, 1)}
    assert held[("one", "one0")] == {"s1", "s9"}  # s3: chosen, then undone - out
    assert apply_migrations(conn, migrations) == migrations[-1][0]
    for (owner, profile), songs in held.items():
        rule = predicate(owner, "1", profile_id=profile, song_expression="t.song_id")
        found = conn.execute("SELECT song_id FROM collection_tracks t WHERE " + rule.sql,
                             rule.params)  # fmt: skip
        assert {r[0] for r in found} == songs, profile
    custom = "SELECT sum(base_original) FROM collection_profiles WHERE kind='custom'"
    assert conn.execute(custom).fetchone() == (0,)
    revisions = conn.execute(
        "SELECT DISTINCT revision,last_operation FROM collection_membership"
        " WHERE track_key='song:3'"
    ).fetchall()
    assert revisions == [(2, "prior")]  # the choice's history kept


def schema23(path: Path | str = ":memory:") -> tuple[sqlite3.Connection, list[Any]]:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    before = next(i for i, (_, name, _) in enumerate(migrations) if name.startswith("0024_"))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    assert apply_migrations(conn, migrations[:before]) == 23
    return conn, migrations


def operation(conn: sqlite3.Connection, ident: str, action: str, selection: str, at: float,
              items: list[tuple[str, int | None, int]], owner: str = "owner") -> None:  # fmt: skip
    """A change done before the upgrade, in the profile All and Shijhon library shared."""
    conn.execute(
        "INSERT INTO collection_operations(id,owner_id,library_id,profile_id,request_key,"
        "fingerprint,action,state,query,selected,created,approved_at,selection)"
        " VALUES (?,?,'1',?,?,'f',?,'done','{}','[]',?,?,?)",
        [ident, owner, default_profile_id(owner), "k-" + ident, action, at, at, selection],
    )
    for key, before, after in items:
        conn.execute(
            "INSERT INTO collection_operation_items(operation_id,track_key,before_saved,"
            "before_revision,after_revision,processed) VALUES (?,?,?,?,?,1)",
            [ident, key, before, after - 1, after],
        )


async def test_the_upgrade_maps_choices_classifies_history_and_carries_albums_over(
    tmp_path: Path,
) -> None:
    """Migration 0024 on a database like the owner's (schema 23). Hand-enabled catalog songs
    become Shijhon library's own additions. A removal stays in the library its change was
    made in; one no change tells about (it hid the song from both) is in both, and an inert
    removal of an own import leaves All. The changes made in Shijhon library move there, and
    Undo of an older change reverts its choices wherever they are now. The active library
    carries over, recorded rows are told apart (activity or a choice), favorites read before
    are kept to record, and the catalog albums no record explains are carried over - not
    completions of owned albums, not albums an account's record explains - into no
    account's library: the owner's command gives them to one account."""
    conn, migrations = schema23(tmp_path / "upgrade.db")
    now = time.time()
    run = conn.execute
    run("INSERT INTO collection_library_policy VALUES ('1','legacy_shared',1,NULL)")
    run("INSERT INTO collection_index VALUES ('1','g','ready',9,?,'')", [now])
    # Albums: o1 owned (s1) completed by a fill (s2); c1 played before anything was
    # recorded; c2 played since (recorded); c3 hand-enabled; c4 a favorite album.
    tracks = [
        ("native:s1", "s1", "native:o1", "o1", 1),
        ("catalog:t2", "s2", "fill", "o1", 0),
        ("catalog:t3", "s3", "rel-c1", "c1", 0),
        ("catalog:t4", "s4", "rel-c2", "c2", 0),
        ("catalog:t5", "s5", "rel-c3", "c3", 0),
        ("catalog:t6", "s6", "rel-c3", "c3", 0),
        ("catalog:t7", "s7", "rel-c4", "c4", 0),
    ]
    for key, ident, release, album, original in tracks:
        run(
            "INSERT INTO collection_tracks(library_id,track_key,song_id,release_key,album_id,"
            "artist_id,title,album,artist,search_text,original,generation,indexed_at)"
            " VALUES ('1',?,?,?,?,'r','t','a','r','t',?,'g',0)",
            [key, ident, release, album, original],
        )
        if not original:
            alias = "catalog:" + key[8:]
            run("INSERT INTO collection_track_aliases VALUES ('1',?,?)", [alias, key])
    for release, album, owned in (
        ("fill", "o1", "o1"),
        ("rel-c1", "c1", None),
        ("rel-c2", "c2", None),
        ("rel-c3", "c3", None),
        ("rel-c4", "c4", None),
    ):
        run(
            "INSERT INTO releases(ref,folder,album_id,owned_album_id,title,artist,album_tags,"
            "data,created_at) VALUES (?,?,?,?,'t','a','{}','{}',0)",
            [release, release, album, owned],
        )
    for key, ident, release, _album, original in tracks:
        if not original:
            run("INSERT INTO track_links VALUES (?,?,?,0)", [key[8:], ident, release])
    owner = "owner"
    all_id, shijhon_id = default_profile_id(owner), builtin_profile_id(owner, "shijhon")
    run(
        "INSERT INTO collection_profiles(id,owner_id,kind,name,created)"
        " VALUES (?,?,'builtin','All',0)",
        [all_id, owner],
    )
    run("INSERT INTO collection_preferences VALUES (?,'shijhon',0)", [owner])
    # Changes in the shared profile: t5 enabled in Shijhon library, t6 removed there, t3
    # removed in All, t7 removed by a change from before the libraries named themselves,
    # the own import s1 removed with no change recorded.
    operation(conn, "save", "save", "shijhon", now - 50, [("catalog:t5", None, 1)])
    operation(conn, "rm-shijhon", "remove", "shijhon", now - 45, [("catalog:t6", None, 1)])
    operation(conn, "rm-all", "remove", "all", now - 44, [("catalog:t3", None, 1)])
    operation(conn, "rm-old", "remove", "", now - 43, [("catalog:t7", None, 1)])
    for key, saved, change in (
        ("catalog:t5", 1, "save"),
        ("catalog:t6", 0, "rm-shijhon"),
        ("catalog:t3", 0, "rm-all"),
        ("catalog:t7", 0, "rm-old"),
        ("native:s1", 0, ""),
    ):
        run(
            "INSERT INTO collection_membership VALUES (?,?,'1',?,?,1,?)",
            [owner, all_id, key, saved, change],
        )
    # History: the 0016 backfill (0), the Save's own row (after approval), a play.
    for key, at in (("catalog:t6", 0), ("catalog:t5", now - 40), ("catalog:t4", now - 60)):
        run(
            "INSERT INTO collection_history(owner_id,library_id,track_key,added_at)"
            " VALUES (?,'1',?,?)",
            [owner, key, at],
        )
    run("INSERT INTO collection_favorites VALUES ('fan','1','album','c4')")
    assert apply_migrations(conn, migrations) == migrations[-1][0]
    dump = list(conn.iterdump())
    assert apply_migrations(conn, migrations) == migrations[-1][0]  # nothing again
    assert list(conn.iterdump()) == dump
    activity = dict(run("SELECT track_key,activity FROM collection_history").fetchall())
    assert activity == {"catalog:t6": 0, "catalog:t5": 0, "catalog:t4": 1}

    def membership() -> dict[tuple[str, str], int | None]:
        return {
            (str(r[0]).split("-")[0], r[1]): r[2]
            for r in run("SELECT profile_id,track_key,saved FROM collection_membership")
        }

    assert membership() == {
        ("shijhon", "catalog:t5"): 1,
        ("shijhon", "catalog:t6"): 0,
        ("default", "catalog:t3"): 0,
        ("default", "catalog:t7"): 0,
        ("shijhon", "catalog:t7"): 0,
        ("shijhon", "native:s1"): 0,
    }
    profiles = dict(run("SELECT id,profile_id FROM collection_operations").fetchall())
    assert profiles == {"save": shijhon_id, "rm-shijhon": shijhon_id, "rm-all": all_id,
                        "rm-old": all_id}  # fmt: skip
    assert run("SELECT active_profile_id FROM collection_preferences").fetchone() == ("shijhon",)
    assert run("SELECT release_ref FROM collection_carryover").fetchall() == [("rel-c1",)]
    assert run("SELECT * FROM collection_carryover_owner").fetchall() == []  # nobody's yet
    assert run("SELECT owner_id,param,item FROM collection_pending").fetchall() == [
        ("fan", "albumId", "c4")
    ]

    def visible(who: str, mode: str) -> set[str]:
        pred = predicate(who, "1", song_expression="t.song_id", mode=mode)  # type: ignore[arg-type]
        rows = run("SELECT t.song_id FROM collection_tracks t WHERE " + pred.sql, pred.params)
        return {r[0] for r in rows}

    # Shijhon library: the recorded play's album and the hand addition's album without the
    # song removed there - not the carried-over one: it is no account's until given.
    assert visible(owner, "saved") == {"s4", "s5"}
    assert visible(owner, "original") == {"s1", "s2"}  # the owned album, completed
    assert visible(owner, "all") == {"s1", "s2", "s4", "s5"}  # t3 stays removed from All
    assert visible("fan", "saved") == set()
    assert visible("later", "saved") == set()
    conn.close()
    # Undo of the older change reverts its removal in both libraries it is in now.
    store = await Store.open(tmp_path / "upgrade.db")
    try:
        service = Collection(store, FakeAccess([]))
        undo = await finish(service, await service.undo(owner, "rm-old"), owner)
        assert undo["state"] == "done" and undo["changed"] == 1 and undo["skipped"] == 0
        rows = await store.fetchall(
            "SELECT profile_id,saved FROM collection_membership WHERE track_key='catalog:t7'"
        )
        assert {r["profile_id"]: r["saved"] for r in rows} == {all_id: None, shijhon_id: None}
    finally:
        await store.close()


def test_the_upgrade_keeps_a_removal_made_in_shijhon_library() -> None:
    """The review's reproduction: a song played, saved and then removed in Shijhon
    library, with Shijhon library in use, stays removed after the upgrade."""
    conn, migrations = schema23()
    profile = default_profile_id("one")
    conn.execute("INSERT INTO collection_library_policy VALUES ('1','personal',1,NULL)")
    conn.execute("INSERT INTO collection_index VALUES ('1','g','ready',2,1,'')")
    conn.execute(
        "INSERT INTO collection_profiles(id,owner_id,kind,name,created)"
        " VALUES (?,'one','builtin','All',0)",
        [profile],
    )
    conn.execute("INSERT INTO collection_preferences VALUES ('one','shijhon',0)")
    for n, saved in ((1, 0), (2, 1)):
        conn.execute(
            "INSERT INTO collection_tracks(library_id,track_key,song_id,release_key,album_id,"
            "artist_id,title,album,artist,search_text,original,generation,indexed_at)"
            " VALUES ('1',?,?,'test:album','album','artist','Song','Album','Artist','song',0,"
            "'g',0)",
            [f"catalog:s{n}", f"s{n}"],
        )
        conn.execute(
            "INSERT INTO collection_membership VALUES ('one',?,'1',?,?,?,?)",
            [profile, f"catalog:s{n}", saved, 2 if n == 1 else 1, "remove" if n == 1 else "save"],
        )
    # Played at 5, saved at 7, removed in Shijhon library at 10.
    conn.execute("INSERT INTO collection_history VALUES ('one','1','catalog:s1',5,'{}')")
    conn.execute("INSERT INTO collection_history VALUES ('one','1','catalog:s2',8,'{}')")
    saved = [("catalog:s1", None, 1), ("catalog:s2", None, 1)]
    operation(conn, "save", "save", "shijhon", 7, saved, owner="one")
    operation(conn, "remove", "remove", "shijhon", 10, [("catalog:s1", 1, 2)], owner="one")
    assert apply_migrations(conn, migrations) == migrations[-1][0]
    pred = predicate("one", "1", mode="saved", song_expression="t.song_id")
    found = conn.execute("SELECT song_id FROM collection_tracks t WHERE " + pred.sql, pred.params)
    assert {r[0] for r in found} == {"s2"}


def test_an_early_installation_reaches_the_new_schema() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    assert apply_migrations(conn, migrations[:2]) == 2
    conn.execute(
        "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
        " VALUES ('rel','rel','al','t','a','{}','{}',0)"
    )
    assert apply_migrations(conn, migrations) == migrations[-1][0]
    assert conn.execute("SELECT release_ref FROM collection_carryover").fetchall() == [("rel",)]
    assert conn.execute("SELECT * FROM collection_carryover_owner").fetchall() == []


# --- learning favorites and imports ------------------------------------------------------


class Navidrome:
    """Answers getStarred2 with ``starred``; ``hold``: the answer waits for ``release``."""

    library_answer = None

    def __init__(self) -> None:
        self.starred: dict[str, list[dict[str, str]]] = {"song": [], "album": []}
        self.asked: list[tuple[bytes, list[tuple[str, str]]]] = []
        self.status = 200
        self.hold = False
        self.release = anyio.Event()
        self.waiting = anyio.Event()

    async def send(self, method: str, path: bytes, query: bytes, *rest: Any) -> httpx.Response:
        self.asked.append((path, parse_qsl(query.decode())))
        if self.hold:
            self.waiting.set()
            await self.release.wait()
        body = {"subsonic-response": {"status": "ok", "starred2": self.starred}}
        return httpx.Response(
            self.status,
            headers={"content-type": "application/json"},
            content=json.dumps(body).encode(),
        )


def request(name: str = "getAlbumList2", *params: tuple[str, str]) -> RestCall:
    query = [
        ("u", "listener"),
        ("t", "token"),
        ("s", "salt"),
        ("v", "1.16.1"),
        ("c", "app"),
        ("f", "xml"),
        ("type", "newest"),
        ("musicFolderId", "1"),
        *params,
    ]
    return RestCall.build(
        name, "GET", b"/rest/" + name.encode(), urlencode(query).encode(), [], None
    )


def star(name: str, *params: tuple[str, str]) -> RestCall:
    return RestCall.build(
        name, "GET", b"/rest/" + name.encode(), urlencode(params).encode(), [], None
    )


ONE = Principal("one", "listener", False, frozenset({1}))


class Spawner:
    """Starts background work in a task group; ``ended``: one event per work started."""

    def __init__(self, group: anyio.abc.TaskGroup) -> None:
        self.group, self.ended = group, list[anyio.Event]()

    def __call__(self, work: Any) -> bool:
        ended = anyio.Event()
        self.ended.append(ended)

        async def run() -> None:
            try:
                await work()
            finally:
                ended.set()

        self.group.start_soon(run)
        return True


async def favorites(store: Store, owner: str = "one") -> set[tuple[str, str]]:
    rows = await store.fetchall(
        "SELECT kind,item_id FROM collection_favorites WHERE owner_id=?", [owner]
    )
    return {(r["kind"], r["item_id"]) for r in rows}


@pytest.fixture
async def learning(tmp_path: Path) -> Any:
    store, service = await open_library(tmp_path / "learning.db")
    try:
        yield store, service
    finally:
        await store.close()


async def test_accepted_stars_and_unstars_change_favorites_at_once(learning: Any) -> None:
    store, service = learning
    reasons = Reasons(store, "1", Navidrome(), lambda work: False)
    await reasons.favorited("one", star("star", ("id", "s4"), ("id", "a3"), ("albumId", "a5")))
    assert await favorites(store) == {("song", "s4"), ("album", "a3"), ("album", "a5")}
    assert await visible(service) == {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s10", "s11"}
    await reasons.favorited("one", star("star", ("artistId", "r1"), ("id", "")))
    await reasons.favorited("one", star("getSong", ("id", "s8")))
    assert len(await favorites(store)) == 3
    await reasons.favorited("one", star("unstar", ("id", "s4"), ("albumId", "a3")))
    assert await favorites(store) == {("album", "a5")}
    await reasons.favorited("one", star("unstar", ("id", "a5")))  # an ID of either kind
    assert await favorites(store) == set()
    await reasons.favorited("two", star("star", ("id", "s8")))
    assert await favorites(store, "two") == {("song", "s8")}
    assert await visible(service) == {"s1", "s2", "s3"}


async def test_the_first_read_is_waited_for_and_later_ones_are_due_by_interval(
    learning: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, service = learning
    navidrome = Navidrome()
    navidrome.starred = {"song": [{"id": "s4"}, {"id": "s8"}], "album": [{"id": "a3"}]}
    await favorite(store, "one", "song", "s10")  # no longer a favorite in Navidrome
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)

        reasons = Reasons(store, "1", navidrome, spawn, interval=1000)
        await reasons.due(ONE, request())
        # Waited for: the answer already counts.
        assert await favorites(store) == {("song", "s4"), ("song", "s8"), ("album", "a3")}
        assert await visible(service) == ALL_OF_ONE
        [(path, params)] = navidrome.asked
        assert path == b"/rest/getStarred2"
        assert sorted(params) == sorted(
            [
                ("u", "listener"),
                ("t", "token"),
                ("s", "salt"),
                ("v", "1.16.1"),
                ("c", "app"),
                ("f", "json"),
            ]
        )
        assert await store.fetchone("SELECT 1 FROM collection_favorites_read")
        await reasons.due(ONE, request())  # within the interval: no read
        assert reasons.reads == 1
        # A later read runs beside the request: it does not wait for Navidrome.
        later = Reasons(store, "1", navidrome, spawn, interval=0)
        navidrome.hold, navidrome.starred = True, {"song": [{"id": "s10"}], "album": []}
        await later.due(ONE, request())
        await navidrome.waiting.wait()
        assert await favorites(store) == {("song", "s4"), ("song", "s8"), ("album", "a3")}
        navidrome.release.set()
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        assert await favorites(store) == {("song", "s10")}


async def test_a_failed_read_is_tried_again_soon_and_counts_nothing(
    learning: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _service = learning
    monkeypatch.setattr(reasons_module, "RETRY_SECONDS", 0.0)
    navidrome = Navidrome()
    navidrome.status = 500
    await favorite(store, "one", "song", "s4")
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)

        reasons = Reasons(store, "1", navidrome, spawn, interval=1000)
        await reasons.due(ONE, request())
        assert reasons.reads == 1 and await favorites(store) == {("song", "s4")}
        assert not await store.fetchone("SELECT 1 FROM collection_favorites_read")
        navidrome.status = 200
        await reasons.due(ONE, request())  # retried at once (RETRY_SECONDS), waited for
        assert reasons.reads == 2 and await favorites(store) == set()
        await reasons.due(ONE, request())
        assert reasons.reads == 2  # then at the interval again


async def test_a_star_during_a_read_is_not_overwritten_by_it(learning: Any) -> None:
    store, _service = learning
    navidrome = Navidrome()
    navidrome.hold = True
    navidrome.starred = {"song": [{"id": "s4"}], "album": []}  # read before the star
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)

        reasons = Reasons(store, "1", navidrome, spawn, interval=1000, first_wait=0)
        await reasons.due(ONE, request())
        await navidrome.waiting.wait()
        await reasons.favorited("one", star("star", ("id", "s10")))
        navidrome.release.set()
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        # The read began before the star: it is not applied, and it is due again soon.
        assert await favorites(store) == {("song", "s10")}
        assert not await store.fetchone("SELECT 1 FROM collection_favorites_read")
        assert reasons._due["one"] - time.monotonic() <= reasons_module.RETRY_SECONDS


async def test_an_import_s_songs_replace_its_earlier_ones(learning: Any) -> None:
    store, service = learning
    reasons = Reasons(store, "1", Navidrome(), lambda work: False)
    await imported(store, "one", [], mix="mix")
    async with store.transaction() as db:
        await reasons.imported(ONE, "mix", ["s8", "s10", "s8"], db)
    assert await visible(service) == {"s1", "s2", "s3", "s8", "s9", "s10", "s11"}
    async with store.transaction() as db:
        await reasons.imported(ONE, "mix", ["s10"], db)
    rows = await store.fetchall("SELECT import_id,song_id FROM collection_imported")
    assert [tuple(r) for r in rows] == [("mix", "s10")]
    other = Principal("one", "listener", False, frozenset({2}))
    async with store.transaction() as db:
        await reasons.imported(other, "mix", ["s4"], db)
    rows = await store.fetchall("SELECT import_id,song_id FROM collection_imported")
    assert [tuple(r) for r in rows] == [("mix", "s10")]
    assert await visible(service, "two") == {"s1", "s2", "s3"}


async def test_a_stalled_read_of_favorites_ends_on_time_and_is_tried_again(
    learning: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 4: a read whose answer never comes ends at its deadline - its
    credentials with it - and the account is read again later."""
    store, _service = learning
    monkeypatch.setattr(reasons_module, "RETRY_SECONDS", 0.0)
    navidrome = Navidrome()
    navidrome.hold = True  # Navidrome never answers
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        reasons = Reasons(store, "1", navidrome, spawn, interval=1000, first_wait=0.05)
        reasons.read_seconds = 0.2
        await reasons.due(ONE, request())  # the first read is waited for, briefly
        assert "one" in reasons._running
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        assert "one" not in reasons._running
        navidrome.hold, navidrome.starred = False, {"song": [{"id": "s4"}], "album": []}
        await reasons.due(ONE, request())
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
    assert await favorites(store) == {("song", "s4")} and reasons.reads == 2


async def test_reads_of_favorites_are_admitted_and_run_a_few_at_a_time(learning: Any) -> None:
    """Review finding 4: many accounts at once - a bounded number of reads is admitted, and
    only a few ask Navidrome at the same time; the others are read later."""
    store, _service = learning
    navidrome = Navidrome()
    navidrome.hold = True
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        reasons = Reasons(store, "1", navidrome, spawn, first_wait=0)
        accounts = [
            Principal(f"u{n}", f"user{n}", False, frozenset({1}))
            for n in range(reasons_module.WAITING + 5)
        ]
        for account in accounts:
            await reasons.due(account, request())
        await anyio.sleep(0.1)
        assert len(reasons._running) == reasons_module.WAITING
        assert len(navidrome.asked) == reasons_module.READS_AT_ONCE
        navidrome.release.set()
        with anyio.fail_after(5):
            for ended in spawn.ended:
                await ended.wait()
        assert not reasons._running and reasons.reads == reasons_module.WAITING
        later = accounts[-1]
        await reasons.due(later, request())  # not admitted before: read at a later request
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
        assert reasons.reads == reasons_module.WAITING + 1


async def test_admission_holds_for_many_accounts_arriving_at_once(learning: Any) -> None:
    """Review round 2, finding 4: requests of many accounts at the same moment are admitted
    up to the limit - the reservation is made before anything is awaited."""
    store, _service = learning
    navidrome = Navidrome()
    navidrome.hold = True
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        reasons = Reasons(store, "1", navidrome, spawn, first_wait=0)
        accounts = [Principal(f"u{n}", f"user{n}", False, frozenset({1})) for n in range(64)]
        async with anyio.create_task_group() as callers:
            for account in accounts:
                callers.start_soon(reasons.due, account, request())
        await anyio.sleep(0.1)
        assert len(spawn.ended) == len(reasons._running) == reasons_module.WAITING
        navidrome.release.set()
        with anyio.fail_after(5):
            for ended in spawn.ended:
                await ended.wait()
    assert reasons.reads == reasons_module.WAITING and not reasons._running


async def test_a_reservation_goes_when_its_read_cannot_begin(
    learning: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _service = learning
    navidrome = Navidrome()
    async with anyio.create_task_group() as group:
        spawn = Spawner(group)
        reasons = Reasons(store, "1", navidrome, spawn, first_wait=0)
        fetchone = store.fetchone

        async def failing(*args: Any) -> Any:
            raise sqlite3.OperationalError("synthetic")

        monkeypatch.setattr(store, "fetchone", failing)
        with pytest.raises(sqlite3.OperationalError):
            await reasons.due(ONE, request())
        assert not reasons._running and "one" not in reasons._due
        monkeypatch.setattr(store, "fetchone", fetchone)
        await reasons.due(ONE, request())  # the next request reads it
        with anyio.fail_after(5):
            await spawn.ended[-1].wait()
    assert reasons.reads == 1
