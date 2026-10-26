# ruff: noqa: S608 -- SQL comes from the validated shared policy.
"""One physical library, independently proved originals and explicit shared choices."""

from __future__ import annotations

import json
import secrets
import sqlite3

import anyio
import pytest

from shijhon.collection.contracts import CollectionError, Query
from shijhon.collection.origins import Origins, record_verified_original
from shijhon.collection.policy import default_profile_id, predicate
from shijhon.collection.query_index import Binding, Budgets, QueryIndex, SourcePage
from shijhon.collection.service import Collection
from shijhon.store import Store
from tests.unit.test_collection import FakeAccess, finish, song

pytestmark = pytest.mark.anyio


async def credit(service, owner, number):
    async with service.store.transaction() as conn:
        await record_verified_original(
            conn,
            owner_id=owner,
            library_id="1",
            track_key=f"native:s{number}",
            song_id=f"s{number}",
            provenance_id=f"receipt:{owner}:{number}",
            asset_id=f"asset-{number}",
        )


async def mutate(service, *, owner="one", profile="all", action="save", query=None, selected=None):
    ident = await service.prepare(
        owner,
        "1",
        query or Query(view="all", scope="song"),
        profile_id=profile,
        action=action,
        selected=selected,
        request_key=secrets.token_hex(16),
    )
    reviewed = await finish(service, ident, owner)
    assert reviewed["state"] == "review"
    await service.approve(owner, ident)
    applied = await finish(service, ident, owner)
    assert applied["state"] == "done"
    return ident, reviewed, applied


async def visible(service, owner="one", profile="all"):
    return await service.visible_song_ids(
        owner, "1", ["s1", "s2", "s3", "s4", "s5"], profile_id=profile
    )


@pytest.fixture
async def personal(tmp_path):
    store = await Store.open(tmp_path / "personal.db")
    # Each account's originals and the catalog songs are in albums of their own (one
    # artist, r1, holds them together): an album with an account's original is whole in
    # its All, so only explicit choices bring the others in.
    access = FakeAccess(
        [
            song(1),
            song(2, albumId="a3", album="Cloud House Live"),
            song(3, path="_shijhon/3.flac", albumId="c1", album="Cloud House Extras"),
            song(4, albumId="a2", album="Quiet Lake", artistId="r2", artist="Paper Field"),
            song(
                5,
                path="_shijhon/5.flac",
                albumId="c2",
                album="Quiet Lake Extras",
                artistId="r2",
                artist="Paper Field",
            ),
        ]
    )
    service = Collection(store, access)
    await Origins(store).initialize_personal("1")
    await service.refresh("one", "1")
    await credit(service, "one", 1)
    await credit(service, "two", 2)
    try:
        yield store, access, service
    finally:
        await store.close()


async def test_three_users_share_files_but_import_credit_never_fans_out(personal):
    store, access, service = personal
    assert await visible(service, "one") == {"s1"}
    assert await visible(service, "two") == {"s2"}
    assert await visible(service, "new") == set()
    assert await visible(service, "new-native-id-for-one") == set()
    baseline = [tuple(row) for row in await store.fetchall("SELECT * FROM collection_originals")]
    generation = (await service.index_state("one", "1")).generation
    for caller in ("one", "two", "new"):
        await service.refresh(caller, "1")
    assert (await service.index_state("one", "1")).generation == generation
    assert [
        tuple(row) for row in await store.fetchall("SELECT * FROM collection_originals")
    ] == baseline
    custom = await service.create_profile("two", "Live originals")
    await mutate(service, owner="two", selected=["native:s1", "catalog:s3"])
    assert await visible(service, "two") == {"s1", "s2", "s3"}
    assert await visible(service, "two", "original") == {"s2"}
    # All's hand choices are All's alone: Shijhon library holds no activity of "two".
    assert await visible(service, "two", "shijhon") == set()
    assert await visible(service, "two", custom) == {"s2"}
    await mutate(service, owner="two", selected=["native:s1"], action="remove")
    assert await visible(service, "two") == {"s2", "s3"}
    assert await visible(service, "one") == {"s1"}
    assert len(access.rows) == 5
    assert [
        tuple(row) for row in await store.fetchall("SELECT * FROM collection_originals")
    ] == baseline
    # A later verified import joins Original imported library without scanning candidates;
    # the custom library, made from it earlier, changes only by hand.
    await credit(service, "two", 4)
    assert await visible(service, "two", "original") == {"s2", "s4"}
    assert await visible(service, "two", custom) == {"s2"}
    assert (await service.index_state("one", "1")).generation == generation
    fresh_service = Collection(store, access)
    assert await visible(fresh_service, "two") == {"s2", "s3", "s4"}
    assert await visible(fresh_service, "new") == set()


@pytest.mark.parametrize("scope", ["song", "album", "artist"])
async def test_source_membership_counts_and_pages_precede_pagination(personal, scope):
    _store, _access, service = personal
    query = Query(view="all", scope=scope, limit=1, sort="title")
    first = await service.browse("one", "1", query, profile_id="all")
    assert first["counts"] == {"all_count": 5, "saved_count": 1, "unsaved_count": 4}
    assert first["sources"] == {"imported_count": 3, "catalog_count": 2}
    assert first["total"] == {"song": 5, "album": 5, "artist": 2}[scope]
    imported = await service.browse("one", "1", Query(view="all", scope=scope, origin="imported"))
    assert imported["total"] == {"song": 3, "album": 3, "artist": 2}[scope]
    assert all(r["imported_tracks"] == r["tracks"] for r in imported["rows"])
    assert sum(r["tracks"] for r in imported["rows"]) == 3
    # The recorded changes' "own" filter: the account's own import alone.
    own = await service.browse("one", "1", Query(view="all", scope=scope, origin="own"))
    assert own["total"] == 1
    row = own["rows"][0]
    assert row["imported_tracks"] == row["originals"] == row["tracks"] == 1
    assert row["editable"] == 1  # a hand choice may remove an own import too
    if scope == "artist":
        assert row["all_imported"] == 2 and row["all_tracks"] == 3
        assert row["full_saved"] == 1
    elif scope == "album":  # Cloud House holds the account's own original alone
        assert row["all_imported"] == row["all_tracks"] == row["full_saved"] == 1
    shared = await service.browse("one", "1", Query(view="all", scope=scope, origin="shared"))
    assert sum(r["tracks"] for r in shared["rows"]) == 2
    assert all(r["imported_tracks"] == r["tracks"] and r["editable"] == 1 for r in shared["rows"])
    catalog = await service.browse("one", "1", Query(view="all", scope=scope, origin="catalog"))
    assert sum(r["tracks"] for r in catalog["rows"]) == 2
    assert all(r["imported_tracks"] == 0 for r in catalog["rows"])
    missing = await service.browse("one", "1", Query(view="unsaved", scope=scope))
    assert sum(r["tracks"] for r in missing["rows"]) == 4
    # Original imported library holds the account's import's album; Shijhon library
    # nothing yet: everything else is missing from them.
    originals = await service.browse(
        "one", "1", Query(view="unsaved", scope=scope), profile_id="original"
    )
    assert sum(r["tracks"] for r in originals["rows"]) == 4
    assert originals["counts"] == {"all_count": 5, "saved_count": 1, "unsaved_count": 4}
    shijhon = await service.browse(
        "one", "1", Query(view="unsaved", scope=scope), profile_id="shijhon"
    )
    assert sum(r["tracks"] for r in shijhon["rows"]) == 5
    assert shijhon["counts"] == {"all_count": 5, "saved_count": 0, "unsaved_count": 5}
    for source, tracks in (("own", 1), ("shared", 2), ("original", 3), ("imported", 3)):
        narrowed = await service.browse(
            "one", "1", Query(view="all", scope=scope, origin=source), profile_id="shijhon"
        )
        assert narrowed["query"]["origin"] == source
        assert sum(r["tracks"] for r in narrowed["rows"]) == tracks


async def test_frozen_shared_actions_remove_undo_and_revision_protection(personal):
    store, _access, service = personal
    operation, reviewed, applied = await mutate(
        service, query=Query(view="all", scope="album", artist_id="r1")
    )
    # The account's own import is frozen too (it may be chosen by hand); it was in All.
    assert reviewed["total"] == 3 and reviewed["affected"] == applied["changed"] == 2
    frozen = await store.fetchall(
        "SELECT track_key FROM collection_operation_items WHERE operation_id=?", [operation]
    )
    assert {r["track_key"] for r in frozen} == {"native:s1", "native:s2", "catalog:s3"}
    assert await visible(service) == {"s1", "s2", "s3"}
    shared, _, _ = await mutate(service, selected=["native:s2"], action="remove")
    assert await visible(service) == {"s1", "s3"}
    assert (await finish(service, await service.undo("one", shared)))["changed"] == 1
    # The earlier Undo changes the choices nothing changed since; s2's newer choice is
    # protected, and s2 is an own import by now.
    await credit(service, "one", 2)
    undone = await finish(service, await service.undo("one", operation))
    assert undone["changed"] == 2 and undone["skipped"] == 1
    assert await visible(service) == {"s1", "s2"}
    pending = await service.prepare(
        "one",
        "1",
        Query(view="all", scope="song", origin="shared"),
        action="remove",
        request_key="new-own",
        selected=["native:s4"],
    )
    assert (await finish(service, pending))["total"] == 1
    # Imported by the account meanwhile: the reviewed removal still wins in All.
    await credit(service, "one", 4)
    await service.approve("one", pending)
    result = await finish(service, pending)
    assert result["changed"] == 1 and result["skipped"] == 0
    assert await visible(service) == {"s1", "s2"}
    assert await visible(service, profile="original") == {"s1", "s2", "s4"}
    assert (
        await store.fetchone(
            "SELECT saved FROM collection_membership WHERE owner_id='one' AND track_key='native:s4'"
        )
    )["saved"] == 0


async def test_all_four_copies_hold_what_their_source_holds(personal):
    store, _access, service = personal
    await mutate(service, selected=["native:s2", "catalog:s3"])
    # A hand removal of an own import in All is All's choice: copies of All keep it.
    await store.execute(
        "INSERT INTO collection_membership VALUES ('one',?,'1','native:s1',0,9,'prior')",
        [default_profile_id("one")],
    )
    await mutate(service, profile="shijhon", selected=["catalog:s5"])
    custom = await service.create_profile("one", "Custom")
    await mutate(service, profile=custom, selected=["native:s1"], action="remove")
    await mutate(service, profile=custom, selected=["native:s4"])
    for source, expected in (
        ("original", {"s1"}),
        ("all", {"s2", "s3", "s5"}),
        ("shijhon", {"s5"}),
        (custom, {"s4"}),
    ):
        assert await visible(service, profile=source) == expected
        copy = await service.create_profile("one", "Copy " + source, start="copy", source=source)
        assert await visible(service, profile=copy) == expected
        memberships = {
            tuple(r)
            for r in await store.fetchall(
                "SELECT track_key,saved FROM collection_membership WHERE profile_id=?", [copy]
            )
        }
        # The copy holds those songs as its own choices, with no base of imports: a song
        # left out stays out without a choice of its own (its album holds nothing here).
        keys = {"s1": "native:s1", "s2": "native:s2", "s3": "catalog:s3", "s4": "native:s4"}
        keys["s5"] = "catalog:s5"
        assert {key for key, saved in memberships if saved == 1} == {keys[s] for s in expected}
        assert ("native:s1", 0) not in memberships
        made = await store.fetchone(
            "SELECT base_original FROM collection_profiles WHERE id=?", [copy]
        )
        assert made["base_original"] == 0
    assert (await service.profile("one"))["selection_id"] == "all"
    assert await visible(service, "two") == {"s2"}


async def test_undo_of_an_addition_leaves_a_song_that_became_an_own_import(personal):
    store, _access, service = personal
    operation, _, _ = await mutate(service, selected=["native:s4"])
    before = dict(
        await store.fetchone(
            "SELECT * FROM collection_membership WHERE owner_id='one' AND track_key='native:s4'"
        )
    )
    await credit(service, "one", 4)
    undo = await finish(service, await service.undo("one", operation))
    assert undo["changed"] == 1 and undo["skipped"] == 0
    after = dict(
        await store.fetchone(
            "SELECT * FROM collection_membership WHERE owner_id='one' AND track_key='native:s4'"
        )
    )
    assert after["saved"] is None and after["revision"] == before["revision"] + 1
    # The hand choice is undone; the account's import keeps the song in All.
    assert await visible(service) == {"s1", "s4"}


async def test_native_predicate_counts_and_exact_pages_share_the_owner_policy(personal):
    store, _access, service = personal
    await mutate(service, selected=["native:s2", "catalog:s3"])
    await mutate(service, profile="shijhon", selected=["catalog:s5"])
    custom = await service.create_profile("one", "Subset")
    await mutate(service, profile=custom, selected=["native:s1"], action="remove")
    await mutate(service, profile=custom, selected=["native:s4", "catalog:s5"])
    for owner, selection, expected in (
        ("one", "all", ["s1", "s2", "s3", "s5"]),
        ("one", "original", ["s1"]),
        ("one", "shijhon", ["s5"]),
        ("one", custom, ["s4", "s5"]),
        ("new", "all", []),
    ):
        pred = predicate(owner, "1", song_expression="native.song_id", profile_id=selection)
        sql = " FROM collection_tracks native WHERE " + pred.sql
        assert (await store.fetchone("SELECT count(*) n" + sql, pred.params))["n"] == len(expected)
        for offset in range(len(expected) + 1):
            page = await store.fetchall(
                "SELECT native.song_id" + sql + " ORDER BY native.song_id LIMIT 1 OFFSET ?",
                [*pred.params, offset],
            )
            assert [r["song_id"] for r in page] == expected[offset : offset + 1]


async def test_shared_import_receipt_can_credit_second_owner_but_selection_cannot(personal):
    store, _access, service = personal
    await mutate(service, owner="two", selected=["native:s1"])
    assert await visible(service, "two", "original") == {"s2"}
    await credit(service, "two", 1)
    assert await visible(service, "two", "original") == {"s1", "s2"}
    own = await service.browse("two", "1", Query(view="all", scope="song", origin="own"))
    assert {row["item_key"] for row in own["rows"]} == {"native:s1", "native:s2"}
    assert own["sources"] == {"imported_count": 3, "catalog_count": 2}
    assert (await store.fetchone("SELECT count(*) n FROM collection_tracks WHERE song_id='s1'"))[
        "n"
    ] == 1
    assert (
        await store.fetchone(
            "SELECT count(*) n FROM collection_originals WHERE track_key='native:s1'"
        )
    )["n"] == 2


async def test_new_personal_policy_has_no_path_scan_login_or_grant_inference(tmp_path):
    store = await Store.open(tmp_path / "empty.db")
    service = Collection(store, FakeAccess([song(1), song(2, path="Imports/asset-two.flac")]))
    try:
        await Origins(store).initialize_personal("1")
        await service.refresh("admin", "1")
        empty = await service.create_profile("one", "Empty", start="empty")
        base = await service.create_profile("one", "Original base")
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        for owner in ("admin", "one", "new"):
            await service.profile(owner)
            assert await visible(service, owner) == set()
        await credit(service, "one", 1)
        assert await visible(service, "one", empty) == set()
        # A copy of Original made before the import changes only by hand; Original and
        # All hold the import's whole album (s2 shares it).
        assert await visible(service, "one", base) == set()
        assert await visible(service, "one", "original") == {"s1", "s2"}
        assert await visible(service, "one") == {"s1", "s2"}
        assert await visible(service, "admin") == set()
    finally:
        await store.close()


async def test_warm_native_candidates_read_live_ownership_for_pages_stats_and_random(
    personal, tmp_path
):
    _store, access, service = personal
    generation = (await service.index_state("one", "1")).generation
    binding = Binding("one", frozenset({"1"}), (("1", generation),))
    calls = []

    async def fetch(start, limit):
        calls.append((start, limit))
        return SourcePage(access.rows[start : start + limit], len(access.rows))

    async def authorize():
        await service.actor("one", "1")

    async with anyio.create_task_group() as group:
        index = QueryIndex(
            tmp_path / "cache" / "queries.db",
            tmp_path / "personal.db",
            group.start_soon,
            budgets=Budgets(wait_seconds=2),
        )
        key = await index.ensure(binding, "all-candidates", fetch, authorize)
        assert (await index.page(key, binding, "song", None, 0, 2)).total == 1
        before = list(calls)
        await credit(service, "one", 4)
        await mutate(service, selected=["native:s2", "catalog:s3"])
        assert await index.ensure(binding, "all-candidates", fetch, authorize) == key
        page = await index.page(key, binding, "song", None, 1, 2)
        assert page.total == 4 and [r["id"] for r in page.rows] == ["s2", "s3"]
        assert (await index.stats(binding, None, "song", ""))["songCount"] == 4
        assert (await index.stats(binding, "original", "song", ""))["songCount"] == 2
        # All's hand choices are not Shijhon library's.
        assert (await index.stats(binding, "saved", "song", "")).get("songCount", 0) == 0
        random = await index.page(key, binding, "song", None, 0, 100, random=True)
        assert {r["id"] for r in random.rows} == {"s1", "s2", "s3", "s4"}
        assert calls == before
        assert (await service.index_state("one", "1")).generation == generation


async def test_missing_policy_fails_closed_and_current_ownership_is_read_in_snapshot(
    personal, tmp_path
):
    store, _access, service = personal
    reader = sqlite3.connect(tmp_path / "personal.db", isolation_level=None)
    pred = predicate("new", "1", song_expression="t.song_id")
    sql = "SELECT t.song_id FROM collection_tracks t WHERE " + pred.sql + " ORDER BY t.song_id"
    try:
        reader.execute("BEGIN")
        assert reader.execute(sql, pred.params).fetchall() == []
        await credit(service, "new", 4)
        assert reader.execute(sql, pred.params).fetchall() == []
        reader.execute("COMMIT")
        assert reader.execute(sql, pred.params).fetchall() == [("s4",)]
        await mutate(service, selected=["catalog:s3"])
        await store.execute("DELETE FROM collection_library_policy WHERE library_id='1'")
        assert await visible(service) == set()
        listing = await service.browse("one", "1", Query(view="all", scope="song"))
        assert listing["total"] == 0 and listing["counts"]["saved_count"] == 0
        assert await Origins(store).initialize_personal("1") == 1
        assert await visible(service) == {"s1", "s3"}
        with pytest.raises(CollectionError):
            await Origins(store).initialize_legacy("1")
    finally:
        reader.close()


async def test_frozen_physical_filter_keeps_meaning_in_personal_mode(personal):
    store, _access, service = personal
    custom = await service.create_profile("new", "Physical snapshot", start="empty")
    # This is the pre-personal query representation, without any new source labels.
    old = json.loads('{"view":"original","scope":"song","origin":"original"}')
    operation, review, result = await mutate(
        service, owner="new", profile=custom, query=Query(**old)
    )
    assert review["total"] == result["changed"] == 3
    assert await visible(service, "new", custom) == {"s1", "s2", "s4"}
    assert await visible(service, "new", "original") == set()
    assert json.loads((await service.operation("new", operation))["query"])["origin"] == "original"
    assert not await store.fetchone("SELECT 1 FROM collection_originals WHERE owner_id='new'")


async def test_import_alias_restores_owner_and_choices_and_refuses_native_conflicts(tmp_path):
    store = await Store.open(tmp_path / "restore.db")
    access = FakeAccess([song(1, path="Imports/asset.flac")])

    async def assets(library, rows):
        assert library == "1"
        return {r["id"]: "asset-1" for r in rows if r["path"] == "Imports/asset.flac"}

    service = Collection(store, access, asset_lookup=assets)
    try:
        await Origins(store).initialize_personal("1")
        await service.refresh("one", "1")
        await credit(service, "one", 1)
        await mutate(service, owner="two", selected=["native:s1"])
        access.rows = []
        await service.refresh("two", "1")
        assert await visible(service) == set()
        assert (await store.fetchone("SELECT count(*) n FROM collection_originals"))["n"] == 1
        access.rows = [song(5, path="Imports/asset.flac")]
        await service.refresh("two", "1")
        assert await visible(service) == await visible(service, "two") == {"s5"}
        assert (await store.fetchone("SELECT track_key FROM collection_tracks WHERE present=1"))[
            "track_key"
        ] == "native:s1"
        await store.execute("INSERT INTO collection_track_aliases VALUES ('1','native:s4','other')")
        access.rows = [song(4, path="Imports/asset.flac")]
        with pytest.raises(CollectionError, match="conflicting"):
            await service.refresh("two", "1")
        assert await visible(service) == {"s5"}
        assert (
            await store.fetchone(
                "SELECT track_key FROM collection_track_aliases WHERE alias='native:s4'"
            )
        )["track_key"] == "other"
    finally:
        await store.close()
