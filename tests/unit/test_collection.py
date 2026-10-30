# ruff: noqa: S608 -- Uses the validated shared collection predicate.
from __future__ import annotations

import json

import pytest

from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.index import Index
from shijhon.store import Store

pytestmark = pytest.mark.anyio


class FakeAccess:
    def __init__(self, songs=()):
        self.rows = list(songs)
        self.allowed = True

    async def authorize(self, owner):
        if not self.allowed:
            raise CollectionError("Access revoked")
        return Actor(owner, frozenset({"1"}))

    async def songs(self, owner, library, offset, limit):
        return self.rows[offset : offset + limit]


def song(n, **extra):
    return {
        "id": f"s{n}",
        "title": f"Song {n}",
        "album": "Cloud House",
        "albumId": "a1",
        "artist": "Glass Orchard",
        "artistId": "r1",
        "path": f"Original/{n}.flac",
        "duration": 180,
        "size": 100,
        "year": 2020,
        **extra,
    }


async def test_shared_metadata_never_contains_caller_annotations(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    try:
        access = FakeAccess(
            [
                song(
                    1,
                    starred="private",
                    playCount=99,
                    userRating=5,
                    unexpected="secret",
                    genres=[{"name": "Invented", "starred": 1}],
                    participants=[{"id": "r1", "name": "Glass Orchard", "bookmark": 7}],
                )
            ]
        )
        await Index(store, access, "_shijhon").scan("owner-a", "1")
        first = await store.fetchone("SELECT metadata FROM collection_tracks")
        assert json.loads(first["metadata"])["genres"] == [{"name": "Invented"}]
        assert not any(
            k in first["metadata"]
            for k in ("private", "secret", "starred", "playCount", "bookmark")
        )
        access.rows = [song(1, userRating=1)]
        await Index(store, access, "_shijhon").scan("owner-b", "1")
        assert (
            "userRating"
            not in (await store.fetchone("SELECT metadata FROM collection_tracks"))["metadata"]
        )
    finally:
        await store.close()


async def test_native_string_tag_values_survive_without_nested_annotations(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    try:
        access = FakeAccess(
            [
                song(
                    1,
                    tags={
                        "genre": ["Imaginary"],
                        "mood": [{"id": "m1", "value": "Calm", "starred": "private"}],
                    },
                    participants={
                        "composer": [{"id": "c1", "name": "Invented Composer", "playCount": 99}]
                    },
                )
            ]
        )
        await Index(store, access, "_shijhon").scan("owner", "1")
        value = json.loads(
            (await store.fetchone("SELECT metadata FROM collection_tracks"))["metadata"]
        )
        assert value["tags"] == {"genre": ["Imaginary"], "mood": [{"id": "m1", "value": "Calm"}]}
        assert value["participants"] == {"composer": [{"id": "c1", "name": "Invented Composer"}]}
        assert "private" not in json.dumps(value) and "playCount" not in json.dumps(value)
    finally:
        await store.close()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "120", True])
async def test_invalid_numeric_metadata_preserves_completed_snapshot(tmp_path, value):
    store = await Store.open(tmp_path / "state.db")
    try:
        access = FakeAccess([song(1)])
        index = Index(store, access, "_shijhon")
        await index.scan("owner", "1")
        before = dict(await store.fetchone("SELECT * FROM collection_tracks"))
        access.rows = [song(2, duration=value)]
        with pytest.raises(CollectionError):
            await index.scan("owner", "1")
        assert dict(await store.fetchone("SELECT * FROM collection_tracks")) == before
    finally:
        await store.close()


async def test_cross_page_duplicate_refuses_refresh(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    try:
        access = FakeAccess([song(1)])
        index = Index(store, access, "_shijhon")
        await index.scan("owner", "1")
        before = (await store.fetchone("SELECT generation FROM collection_index"))["generation"]
        access.rows = [song(n) for n in range(500)] + [song(0)]
        with pytest.raises(CollectionError, match="repeated"):
            await index.scan("owner", "1")
        assert (await store.fetchone("SELECT count(*) n FROM collection_tracks"))["n"] == 1
        state = await store.fetchone("SELECT * FROM collection_index")
        assert state["generation"] == before and state["state"] == "failed"
    finally:
        await store.close()


async def test_final_scan_authorization_prevents_publishing_after_revocation(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    try:
        access = FakeAccess([song(1)])
        index = Index(store, access, "_shijhon")
        await index.scan("owner", "1")
        before = dict(await store.fetchone("SELECT * FROM collection_tracks"))

        async def revoked_page(owner, library, offset, limit):
            access.allowed = False
            return [song(2)]

        access.songs = revoked_page
        with pytest.raises(CollectionError):
            await index.scan("owner", "1")
        assert dict(await store.fetchone("SELECT * FROM collection_tracks")) == before
    finally:
        await store.close()


async def test_unchanged_refresh_keeps_generation_but_structural_change_replaces_it(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    try:
        access = FakeAccess([song(1, playCount=1)])
        index = Index(store, access, "_shijhon")
        await index.scan("owner", "1")
        before = (await store.fetchone("SELECT generation FROM collection_index"))["generation"]
        # Private caller annotations cannot invalidate a shared structural generation.
        access.rows = [song(1, playCount=999, starred="private")]
        await index.scan("owner", "1")
        assert (await store.fetchone("SELECT generation FROM collection_index"))[
            "generation"
        ] == before
        access.rows = [song(1, title="A corrected title")]
        await index.scan("owner", "1")
        assert (await store.fetchone("SELECT generation FROM collection_index"))[
            "generation"
        ] != before
    finally:
        await store.close()


async def setup_collection(tmp_path, rows):
    from shijhon.collection.service import Collection

    store = await Store.open(tmp_path / "collection.db")
    access = FakeAccess(rows)
    service = Collection(store, access)
    from shijhon.collection.origins import Origins

    await Origins(store).initialize_legacy("1")
    await service.refresh("one", "1")
    return store, access, service


async def finish(service, ident, owner="one"):
    for _ in range(30):
        await service.tick()
        op = await service.operation(owner, ident)
        if op["state"] not in ("preparing", "queued", "applying"):
            return op
    raise AssertionError("worker did not finish")


async def change(service, query, action="save", owner="one", selected=None, profile=None):
    import secrets

    ident = await service.prepare(
        owner,
        "1",
        query,
        action=action,
        request_key=secrets.token_hex(16),
        selected=selected,
        profile_id=profile,
    )
    op = await finish(service, ident, owner)
    assert op["state"] == "review", op
    await service.approve(owner, ident)
    assert (await finish(service, ident, owner))["state"] == "done"
    return ident


async def test_defaults_provenance_and_personal_exclusion(tmp_path):
    from shijhon.collection.contracts import Query

    # Catalog songs in an album of their own: no original brings it into All.
    store, _access, service = await setup_collection(
        tmp_path,
        [
            song(1),
            song(2, path="_shijhon/2.flac", albumId="c1"),
            song(3, path="_shijhon/3.flac", albumId="c1"),
        ],
    )
    try:
        assert await service.visible_song_ids("one", "1", ["s1", "s2", "s3"]) == {"s1"}
        personal = await service.create_profile("one", "Subset", start="original")
        await service.activate_profile("one", personal)
        ident = await change(
            service, Query(view="all", scope="song"), "remove", selected=["native:s1"]
        )
        assert not await service.visible_song_ids("one", "1", ["s1"])
        assert await service.visible_song_ids("two", "1", ["s1"]) == {"s1"}
        assert await service.visible_song_ids("one", "1", ["s1"], mode="original") == {"s1"}
        assert (await service.browse("one", "1", Query(view="all", scope="song")))["total"] == 3
        undo = await service.undo("one", ident)
        await finish(service, undo)
        assert await service.visible_song_ids("one", "1", ["s1"]) == {"s1"}
        assert (await service.profile("one"))["id"] == personal
        await service.browse("one", "1", Query(view="all"))
        assert (await service.profile("one"))["id"] == personal
        await service.set_mode("one", "all")
        assert await service.visible_song_ids("one", "1", ["s1", "s2", "s3"]) == {"s1"}
        assert (await service.profile("two"))["selection_id"] == "all"
    finally:
        await store.close()


async def test_frozen_album_and_artist_actions_include_only_current_contents(tmp_path):
    from shijhon.collection.contracts import Query

    store, access, service = await setup_collection(
        tmp_path, [song(i, path=f"_shijhon/{i}.flac") for i in range(10)]
    )
    try:
        ident = await service.prepare(
            "one", "1", Query(view="all", scope="artist"), action="save", request_key="freeze"
        )
        op = await finish(service, ident)
        assert op["total"] == 10 and op["affected"] == 10
        access.rows.append(song(11, path="_shijhon/11.flac"))
        await service.refresh("one", "1")
        await service.approve("one", ident)
        await finish(service, ident)
        # The frozen change chose the ten songs it saw; the song that arrived later has no
        # choice of its own - its album, added by hand, brings it in whole.
        chosen = await store.fetchall(
            "SELECT track_key FROM collection_membership WHERE saved=1 AND track_key<>''"
        )
        assert {r["track_key"] for r in chosen} == {f"catalog:s{i}" for i in range(10)}
        assert await service.visible_song_ids("one", "1", ["s1", "s11"]) == {"s1", "s11"}
        listing = await service.browse("one", "1", Query(view="saved", scope="album"))
        assert listing["rows"][0]["all_tracks"] == 11
        assert listing["rows"][0]["full_saved"] == 11
    finally:
        await store.close()


async def test_bulk_batches_resume_idempotently_and_undo_skips_newer_same_value_intent(tmp_path):
    from shijhon.collection.contracts import Query

    query = Query(view="all", scope="song")
    store, _access, service = await setup_collection(
        tmp_path, [song(i, path=f"_shijhon/{i}.flac") for i in range(651)]
    )
    try:
        ident = await service.prepare("one", "1", query, action="save", request_key="all")
        assert ident == await service.prepare("one", "1", query, action="save", request_key="all")
        with pytest.raises(CollectionError, match="different"):
            await service.prepare("one", "1", query, action="remove", request_key="all")
        assert (await finish(service, ident))["total"] == 651
        await service.approve("one", ident)
        await service.tick()
        assert (await service.operation("one", ident))["processed"] == 250
        await finish(service, ident)
        # Saving an already saved song is a new explicit choice: old Undo must respect it.
        newer = await change(service, query, selected=["catalog:s1"])
        assert (await service.operation("one", newer))["changed"] == 0
        undo = await service.undo("one", ident)
        assert undo == await service.undo("one", ident)
        op = await finish(service, undo)
        assert op["skipped"] == 1
        choices = {
            r["track_key"]: r["saved"]
            for r in await store.fetchall(
                "SELECT track_key,saved FROM collection_membership"
                " WHERE track_key IN ('catalog:s0','catalog:s1','catalog:s2')"
            )
        }
        assert choices == {"catalog:s0": None, "catalog:s1": 1, "catalog:s2": None}
        # The newer addition of s1 stands, and brings its whole album.
        assert await service.visible_song_ids("one", "1", ["s0", "s1", "s2"]) == {
            "s0",
            "s1",
            "s2",
        }
    finally:
        await store.close()


async def test_newer_changes_after_review_are_protected_and_access_is_rechecked(tmp_path):
    from shijhon.collection.contracts import Query

    query = Query(view="all", scope="song")
    store, access, service = await setup_collection(tmp_path, [song(1)])
    try:
        custom = await service.create_profile("one", "Subset", start="original")
        await service.activate_profile("one", custom)
        old = await service.prepare("one", "1", query, action="remove", request_key="old")
        await finish(service, old)
        await change(service, query, action="save")
        await service.approve("one", old)
        assert (await finish(service, old))["skipped"] == 1
        access.allowed = False
        for call in (
            service.profile("one"),
            service.browse("one", "1", query),
            service.operation("one", old),
            service.set_mode("one", "all"),
            service.visible_song_ids("one", "1", ["s1"]),
        ):
            with pytest.raises(CollectionError):
                await call
    finally:
        await store.close()


async def test_cancellation_during_scan_keeps_old_complete_generation(tmp_path):
    import anyio

    store, access, service = await setup_collection(tmp_path, [song(99)])
    before = (await service.index_state("one", "1")).generation
    access.rows = [song(i) for i in range(700)]
    original = access.songs
    waiting = anyio.Event()

    async def delayed(owner, library, offset, limit):
        if offset:
            waiting.set()
            await anyio.sleep_forever()
        return await original(owner, library, offset, limit)

    access.songs = delayed
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(service.refresh, "one", "1")
            await waiting.wait()
            assert (await service.index_state("one", "1")).generation == before
            tasks.cancel_scope.cancel()
        assert (await service.index_state("one", "1")).generation == before
        assert (await store.fetchone("SELECT count(*) n FROM collection_tracks WHERE present=1"))[
            "n"
        ] == 1
        assert (await store.fetchone("SELECT count(*) n FROM collection_scan_tracks"))["n"] == 0
    finally:
        await store.close()


async def test_same_native_identity_keeps_explicit_remove_when_provenance_changes(tmp_path):
    from shijhon.collection.contracts import Query

    store, access, service = await setup_collection(tmp_path, [song(1, path="_shijhon/1.flac")])
    try:
        custom = await service.create_profile("one", "Subset", start="original")
        await service.activate_profile("one", custom)
        await change(service, Query(view="all", scope="song"), "remove")
        access.rows = [song(1, path="Purchased/1.flac")]
        await service.refresh("one", "1")
        row = await store.fetchone("SELECT * FROM collection_tracks WHERE present=1")
        assert row["track_key"] == "catalog:s1" and row["original"] == 1
        assert not await service.visible_song_ids("one", "1", ["s1"])
        assert await service.visible_song_ids("one", "1", ["s1"], mode="original") == {"s1"}
    finally:
        await store.close()


async def test_policy_is_before_count_and_pagination_and_identifiers_are_validated(tmp_path):
    from shijhon.collection import policy

    store, _access, service = await setup_collection(
        tmp_path,
        # Catalog songs in an album of their own: no original brings it into All.
        [
            song(i, path="_shijhon/a.flac", albumId="c1")
            if i < 5
            else song(i, path="Original/a.flac")
            for i in range(10)
        ],
    )
    try:
        pred = await service.predicate("one", "1", song_expression="native.id")
        rows = await store.fetchall(
            "SELECT native.id FROM (SELECT song_id id FROM collection_tracks) native WHERE "
            + pred.sql
            + " ORDER BY native.id LIMIT 2 OFFSET 1",
            pred.params,
        )
        assert [r["id"] for r in rows] == ["s6", "s7"]
        for expression in ("id) OR 1=1 --", "id;DELETE", "a.b.c", "value + 1"):
            with pytest.raises(CollectionError):
                policy.predicate("one", "1", song_expression=expression)
        with pytest.raises(CollectionError):
            policy.predicate("one", "1", database="main;drop")
    finally:
        await store.close()


async def managed_link(store, ident, track_ref="demo:t1"):
    await store.execute(
        "INSERT OR IGNORE INTO releases(ref,folder,album_id,title,artist,album_tags,data,"
        "created_at)"
        " VALUES ('demo:r1','_shijhon/r1','a1','Cloud House','Glass Orchard','{}','{}',1)"
    )
    await store.execute(
        "INSERT INTO placeholders(song_id,path,placeholder_path,track_ref,release_ref,title,artist,"
        "album,duration_ms,disc,track,tags,created_at)"
        " VALUES (?,?,?,?,'demo:r1','Song','Glass Orchard','Cloud House',180000,1,1,'{}',1)",
        [ident, f"_shijhon/{ident}.flac", f"_shijhon/{ident}.flac", track_ref],
    )


async def test_catalog_alias_survives_cleanup_and_add_back_with_new_native_id(tmp_path):
    from shijhon.collection.contracts import Query
    from shijhon.collection.service import Collection

    store = await Store.open(tmp_path / "state.db")
    access = FakeAccess([song(1, path="_shijhon/s1.flac")])
    service = Collection(store, access)
    from shijhon.collection.origins import Origins

    await Origins(store).initialize_legacy("1")
    try:
        await managed_link(store, "s1")
        await service.refresh("one", "1")
        await change(service, Query(view="all", scope="song"))
        await store.execute("DELETE FROM releases")
        access.rows = []
        await service.refresh("one", "1")
        assert not await service.visible_song_ids("one", "1", ["s1"])
        await managed_link(store, "s99")
        access.rows = [song(99, path="_shijhon/s99.flac")]
        await service.refresh("one", "1")
        assert await service.visible_song_ids("one", "1", ["s99"]) == {"s99"}
        row = await store.fetchone("SELECT track_key FROM collection_tracks WHERE present=1")
        assert row["track_key"] == "catalog:demo:t1"
        assert (await store.fetchone("SELECT count(*) n FROM collection_membership"))["n"] == 1
    finally:
        await store.close()


async def test_saved_metadata_guard_is_current_and_not_an_audio_retention_use(tmp_path):
    from shijhon.collection.contracts import Query
    from shijhon.collection.retention import SAVED_RELEASE_SQL
    from shijhon.collection.service import Collection
    from shijhon.placeholders.engine import _used_meanwhile

    store = await Store.open(tmp_path / "state.db")
    access = FakeAccess([song(1, path="_shijhon/s1.flac")])
    service = Collection(store, access)
    from shijhon.collection.origins import Origins

    await Origins(store).initialize_legacy("1")
    try:
        await managed_link(store, "s1")
        await service.refresh("one", "1")
        assert not await store.fetchone(SAVED_RELEASE_SQL, ["demo:r1"])
        saved = await change(service, Query(view="all", scope="song"))
        assert await store.fetchone(SAVED_RELEASE_SQL, ["demo:r1"])
        async with store.transaction() as conn:
            assert await _used_meanwhile(conn, "demo:r1") == ["saved in a collection"]
        assert (await store.fetchone("SELECT state,last_used_at FROM placeholders"))[
            "last_used_at"
        ] is None
        undo = await service.undo("one", saved)
        await finish(service, undo)
        assert not await store.fetchone(SAVED_RELEASE_SQL, ["demo:r1"])
    finally:
        await store.close()


async def test_profiles_are_independent_activation_is_explicit_and_archive_is_reversible(tmp_path):
    from shijhon.collection.contracts import Query

    store, _access, service = await setup_collection(
        tmp_path, [song(1), song(2, path="_shijhon/2.flac", albumId="c1")]
    )
    try:
        everyday = (await service.profile("one"))["selection_id"]
        assert everyday == "all"
        assert await service.list_profiles("one") == []
        empty = await service.create_profile("one", "Discovery", start="empty")
        assert (await service.profile("one"))["selection_id"] == everyday
        assert not await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id=empty)
        ident = await service.prepare(
            "one",
            "1",
            Query(view="all", scope="song"),
            action="save",
            request_key="same",
            selected=["catalog:s2"],
            profile_id=empty,
        )
        await finish(service, ident)
        await service.approve("one", ident)
        await finish(service, ident)
        assert await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id=empty) == {"s2"}
        assert await service.visible_song_ids("one", "1", ["s1", "s2"]) == {"s1"}
        copied = await service.create_profile("one", "Workout", start="copy", source=empty)
        assert await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id=copied) == {"s2"}
        await service.rename_profile("one", copied, "Training")
        assert (await service.profile("one"))["selection_id"] == "all"
        await service.activate_profile("one", copied)
        assert (await service.profile("one"))["id"] == copied
        assert await service.visible_song_ids("one", "1", ["s1", "s2"]) == {"s2"}
        await service.archive_profile("one", empty)
        assert not await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id=empty)
        await service.archive_profile("one", empty, archived=False)
        assert await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id=empty) == {"s2"}
        with pytest.raises(CollectionError, match="another profile"):
            await service.archive_profile("one", copied)
        with pytest.raises(CollectionError):
            await service.activate_profile("two", empty)
        with pytest.raises(CollectionError):
            await service.create_profile("two", "Foreign copy", start="copy", source=empty)
        undo = await service.undo("one", ident)
        await finish(service, undo)
        assert not await service.visible_song_ids("one", "1", ["s2"], profile_id=empty)
        assert await service.visible_song_ids("one", "1", ["s2"], profile_id=copied) == {"s2"}
        assert (await service.profile("one"))["id"] == copied
    finally:
        await store.close()


async def test_shared_native_map_metadata_strips_nested_annotations():
    from shijhon.collection.index import structural_metadata

    value = json.loads(
        structural_metadata(
            song(
                1,
                coverArt="al-a1",
                participants={
                    "composer": [
                        {
                            "id": "ar1",
                            "name": "Glass Orchard",
                            "subRole": "composer",
                            "userRating": 5,
                            "starred": "private",
                            "playCount": 9,
                        }
                    ]
                },
                tags={"mood": [{"id": "tag1", "value": "Calm", "bookmark": "private"}]},
            )
        )
    )
    assert value["participants"] == {
        "composer": [{"id": "ar1", "name": "Glass Orchard", "subRole": "composer"}]
    }
    assert value["tags"] == {"mood": [{"id": "tag1", "value": "Calm"}]}
    assert value["coverArt"] == "al-a1"


async def test_profile_and_mode_resolve_inside_one_sqlite_snapshot(tmp_path):
    import sqlite3

    from shijhon.collection.policy import predicate

    store, _access, service = await setup_collection(tmp_path, [song(1)])
    try:
        await service.profile("one")
        empty = await service.create_profile("one", "Empty", start="empty")
        reader = sqlite3.connect(tmp_path / "collection.db", isolation_level=None)
        pred = predicate("one", "1", song_expression="t.song_id")
        sql = "SELECT song_id FROM collection_tracks t WHERE " + pred.sql
        reader.execute("BEGIN")
        assert reader.execute(sql, pred.params).fetchall() == [("s1",)]
        await service.activate_profile("one", empty)
        assert reader.execute(sql, pred.params).fetchall() == [("s1",)]
        reader.execute("COMMIT")
        assert reader.execute(sql, pred.params).fetchall() == []
        reader.close()
    finally:
        await store.close()


@pytest.mark.parametrize("scope", ["album", "artist"])
async def test_filtered_bulk_freezes_exact_source_tracks_in_mixed_groups(tmp_path, scope):
    from shijhon.collection.contracts import Query

    store, _access, service = await setup_collection(
        tmp_path, [song(1), song(2, path="_shijhon/2.flac")]
    )
    try:
        custom = await service.create_profile("one", "Subset", start="empty")
        ident = await service.prepare(
            "one",
            "1",
            Query(view="all", scope=scope, origin="original"),
            action="save",
            request_key="originals",
            profile_id=custom,
        )
        op = await finish(service, ident)
        assert op["total"] == 1 and op["group_count"] == 1
        # The review counts the album's other song too: a song added by hand brings it.
        assert op["affected"] == 2
        await service.approve("one", ident)
        await finish(service, ident)

        async def chosen(profile: str) -> set[str]:
            rows = await store.fetchall(
                "SELECT track_key FROM collection_membership WHERE profile_id=? AND saved=1",
                [profile],
            )
            return {r["track_key"] for r in rows}

        # Only the frozen original was chosen; its album comes whole.
        assert await chosen(custom) == {"native:s1"}
        assert await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id=custom) == {
            "s1",
            "s2",
        }
        builtin = await service.prepare(
            "one",
            "1",
            Query(view="all", scope=scope, origin="catalog"),
            action="save",
            request_key="catalog",
            profile_id="shijhon",
        )
        assert (await finish(service, builtin))["total"] == 1
        await service.approve("one", builtin)
        await finish(service, builtin)
        assert await chosen((await service.profile("one", "shijhon"))["id"]) == {"catalog:s2"}
        assert await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id="shijhon") == {
            "s1",
            "s2",
        }
        assert await service.visible_song_ids("one", "1", ["s1", "s2"]) == {"s1", "s2"}
    finally:
        await store.close()


async def test_retry_never_infers_approval_from_frozen_count(tmp_path):
    from shijhon.collection.contracts import Query

    store, _access, service = await setup_collection(tmp_path, [song(1, path="_shijhon/a.flac")])
    try:
        ident = await service.prepare(
            "one", "1", Query(view="all", scope="song"), action="save", request_key="retry"
        )
        assert (await finish(service, ident))["total"] == 1
        await store.execute("UPDATE collection_operations SET state='failed' WHERE id=?", [ident])
        await service.retry("one", ident)
        op = await finish(service, ident)
        assert op["state"] == "review" and op["approved_at"] is None
        assert not await service.visible_song_ids("one", "1", ["s1"])
        await service.approve("one", ident)
        assert (await service.operation("one", ident))["approved_at"]
        await store.execute("UPDATE collection_operations SET state='failed' WHERE id=?", [ident])
        await service.retry("one", ident)
        assert (await finish(service, ident))["state"] == "done"
        assert await service.visible_song_ids("one", "1", ["s1"]) == {"s1"}
    finally:
        await store.close()


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_cancellation_at_snapshot_replacement_never_exposes_half_an_index(tmp_path, stage):
    import anyio

    store, access, service = await setup_collection(tmp_path, [song(1)])
    previous = (await service.index_state("one", "1")).generation
    access.rows = [song(2), song(3)]
    execute = store.conn.execute
    try:
        with anyio.CancelScope() as scope:

            def cancelling(sql, params=()):
                marker = (
                    "UPDATE collection_tracks SET present=0"
                    if stage == "before"
                    else "UPDATE collection_index SET generation="
                )
                if marker not in sql:
                    return execute(sql, params)

                async def run():
                    cursor = await execute(sql, params)
                    scope.cancel()
                    if stage == "before":
                        await anyio.lowlevel.checkpoint()
                    return cursor

                return run()

            store.conn.execute = cancelling
            await service.refresh("one", "1")
            await anyio.lowlevel.checkpoint()
        store.conn.execute = execute
        state = await service.index_state("one", "1")
        rows = await store.fetchall(
            "SELECT song_id FROM collection_tracks WHERE present=1 ORDER BY song_id"
        )
        if stage == "before":
            assert state.generation == previous and [r["song_id"] for r in rows] == ["s1"]
        else:
            assert state.generation != previous and [r["song_id"] for r in rows] == ["s2", "s3"]
    finally:
        store.conn.execute = execute
        await store.close()


@pytest.mark.parametrize("scope", ["album", "artist"])
@pytest.mark.parametrize("selected_group", [False, True])
@pytest.mark.parametrize("action", ["save", "remove"])
async def test_builtin_bulk_imported_filter_never_reaches_catalog_songs(
    tmp_path, scope, selected_group, action
):
    from shijhon.collection.contracts import Query

    # The catalog song is in an album of its own: no original brings it into All.
    store, _access, service = await setup_collection(
        tmp_path, [song(1), song(2, path="_shijhon/a.flac", albumId="c1")]
    )
    try:
        shown = await service.browse(
            "one", "1", Query(view="all", origin="imported", scope=scope), profile_id="all"
        )
        assert shown["rows"][0]["tracks"] == 1 and shown["rows"][0]["originals"] == 1
        ident = await service.prepare(
            "one",
            "1",
            Query(view="all", origin="imported", scope=scope),
            action=action,
            selected=(["a1"] if scope == "album" else ["r1"]) if selected_group else None,
            request_key="no-expansion",
            profile_id="all",
        )
        frozen = await finish(service, ident)
        # Only the imported song: the own import is in All already, and a removal of it
        # wins in All.
        assert frozen["total"] == 1 and frozen["affected"] == int(action == "remove")
        await service.approve("one", ident)
        assert (await finish(service, ident))["changed"] == int(action == "remove")
        assert await service.visible_song_ids("one", "1", ["s1", "s2"]) == (
            {"s1"} if action == "save" else set()
        )
        assert await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id="original") == {
            "s1"
        }
        rows = await store.fetchall("SELECT track_key,saved FROM collection_membership")
        assert [tuple(r) for r in rows] == [("native:s1", int(action == "save"))]
    finally:
        await store.close()


@pytest.mark.parametrize("source,old_action", [("all", "remove"), ("shijhon", "save")])
async def test_builtin_copy_keeps_hand_choices_on_tracks_that_became_original(
    tmp_path, source, old_action
):
    from shijhon.collection.contracts import Query

    store, access, service = await setup_collection(
        tmp_path, [song(1), song(2, path="_shijhon/2.flac"), song(3, path="_shijhon/3.flac")]
    )
    try:
        query = Query(view="all", scope="song")
        await change(service, query, old_action, selected=["catalog:s2"], profile=source)
        await change(service, query, "save", selected=["catalog:s3"], profile=source)
        access.rows = [song(1), song(2, path="Purchased/2.flac"), song(3, path="_shijhon/3.flac")]
        await service.refresh("one", "1")
        expected = await service.visible_song_ids("one", "1", ["s1", "s2", "s3"], profile_id=source)
        # A hand choice wins in its library, also over a song that is an import now; songs
        # added by hand to Shijhon library bring their whole album.
        assert expected == ({"s1", "s3"} if source == "all" else {"s1", "s2", "s3"})
        copied = await service.create_profile("one", "Copy", start="copy", source=source)
        assert (
            await service.visible_song_ids("one", "1", ["s1", "s2", "s3"], profile_id=copied)
            == expected
        )
        row = await store.fetchone(
            "SELECT saved FROM collection_membership WHERE owner_id='one' AND profile_id=?"
            " AND track_key='catalog:s2'",
            [copied],
        )
        assert row["saved"] == int(old_action == "save")
    finally:
        await store.close()


@pytest.mark.parametrize("old_action", ["save", "remove"])
async def test_management_counts_match_builtin_policy_after_catalog_becomes_original(
    tmp_path, old_action
):
    from shijhon.collection.contracts import Query

    store, access, service = await setup_collection(
        tmp_path, [song(1), song(2, path="_shijhon/2.flac"), song(3, path="_shijhon/3.flac")]
    )
    try:
        await change(service, Query(view="all", scope="song"), old_action, selected=["catalog:s2"])
        await change(service, Query(view="all", scope="song"), "save", selected=["catalog:s3"])
        await change(
            service,
            Query(view="all", scope="song"),
            "save",
            selected=["catalog:s3"],
            profile="shijhon",
        )
        custom = await service.create_profile("one", "Personal subset", start="copy", source="all")
        access.rows = [song(1), song(2, path="Purchased/2.flac"), song(3, path="_shijhon/3.flac")]
        await service.refresh("one", "1")
        keys = {
            row["track_key"]: row["song_id"]
            for row in await store.fetchall("SELECT track_key,song_id FROM collection_tracks")
        }
        # Original holds the whole album of the import, Shijhon library the whole album of
        # its hand-added song; All's own choices win in All and in its copy, also over the
        # song that became an import.
        chosen = {"s1", "s2", "s3"} if old_action == "save" else {"s1", "s3"}
        for profile, expected in (
            ("original", {"s1", "s2", "s3"}),
            ("shijhon", {"s1", "s2", "s3"}),
            ("all", chosen),
            (custom, chosen),
        ):
            assert (
                await service.visible_song_ids("one", "1", ["s1", "s2", "s3"], profile_id=profile)
                == expected
            )
            listing = await service.browse(
                "one", "1", Query(view="saved", scope="song"), profile_id=profile
            )
            assert {keys[row["item_key"]] for row in listing["rows"]} == expected
            assert listing["counts"]["saved_count"] == len(expected)
            assert all(row["saved"] == row["full_saved"] == 1 for row in listing["rows"])
    finally:
        await store.close()
