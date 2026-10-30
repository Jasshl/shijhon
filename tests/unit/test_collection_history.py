"""Durable account music is independent from active profiles and materialization."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import asdict, replace

import anyio
import pytest

from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.collection.contracts import Actor, CollectionError, Query
from shijhon.collection.history import History
from shijhon.collection.origins import Origins
from shijhon.collection.service import Collection
from shijhon.store import Store, apply_migrations
from shijhon.store.db import _bundled_migrations
from shijhon.views.ids import album_id, artist_id
from tests.unit.test_collection import FakeAccess, finish, managed_link, song
from tests.unit.test_collection_personal import credit, mutate

pytestmark = pytest.mark.anyio
PERSONAL = Query(pool="personal", view="all", scope="song")


def recording(n=1):
    return CatalogTrack(
        CatalogRef("demo", f"t{n}"),
        f"Song {n}",
        "Glass Orchard",
        180000,
        album=CatalogRef("demo", "a1"),
        album_title="Cloud House",
        isrc=f"XXAAA20000{n:02d}",
    )


@pytest.fixture
async def collection(tmp_path):
    store = await Store.open(tmp_path / "history.db")
    # The original is in an album of its own: an album with an account's original is
    # whole in its All, and these tests are about explicit choices of catalog songs.
    access = FakeAccess(
        [song(n, path=f"_shijhon/{n}.flac") for n in range(1, 7)]
        + [song(7, albumId="a7", album="Own Shelf")]
    )
    service = Collection(store, access)
    await Origins(store).initialize_personal("1")
    await service.refresh("one", "1")
    try:
        yield store, access, service
    finally:
        await store.close()


async def keys(service, owner="one", profile="all", query=PERSONAL):
    result = await service.browse(owner, "1", query, profile_id=profile)
    return {row["item_key"] for row in result["rows"]}


async def prepare(service, *, owner="one", query=PERSONAL, profile="all", action="save"):
    op = await service.prepare(
        owner, "1", query, profile_id=profile, action=action, request_key=secrets.token_hex(8)
    )
    assert (await finish(service, op, owner))["state"] == "review"
    return op


async def test_disabled_copied_archived_and_undone_music_stays_personal(collection):
    store, access, service = collection
    generation = (await service.index_state("one", "1")).generation
    await credit(service, "one", 7)
    profile = await service.create_profile("one", "First", start="empty")
    save, _, _ = await mutate(
        service, profile=profile, selected=["catalog:s1", "catalog:s2", "catalog:s3"]
    )
    await mutate(service, owner="two", selected=["catalog:s4", "catalog:s5"])
    await mutate(service, profile=profile, selected=["catalog:s2"], action="remove")
    copy = await service.create_profile("one", "Second", start="copy", source=profile)
    await service.archive_profile("one", profile)
    # The songs added by hand brought their whole album (s1-s6); the removal stays.
    album = {f"catalog:s{n}" for n in range(1, 7)}
    history = {"catalog:s1", "catalog:s2", "catalog:s3", "native:s7"}
    assert await keys(service, profile=copy) == album | {"native:s7"}
    saved = replace(PERSONAL, view="saved")
    assert await keys(service, profile=copy, query=saved) == album - {"catalog:s2"}
    # A new library's collected music: the account's history and imports, whatever later
    # became of the libraries that added them.
    empty = await service.create_profile("one", "Third", start="empty")
    assert await keys(service, profile=empty) == history
    # The second account's hand additions bring their album into its All, not the first's.
    assert await keys(service, owner="two") == album
    assert await keys(service, owner="new") == set()
    assert len(await keys(service, query=replace(PERSONAL, pool="server"))) == 7
    assert (await service.index_state("one", "1")).generation == generation
    assert len(access.rows) == 7
    await service.archive_profile("one", profile, archived=False)
    undo = await service.undo("one", save)
    assert (await finish(service, undo))["state"] == "done"
    assert await keys(service, profile=profile) == history
    assert not await keys(service, profile=profile, query=saved)
    assert (
        await store.fetchone(
            "SELECT active_profile_id FROM collection_preferences WHERE owner_id='one'"
        )
    )["active_profile_id"] == "all"


@pytest.mark.parametrize("catalog_artist", [False, True])
async def test_metadata_drilldown_survives_native_ids_without_widening_account_scope(
    collection, catalog_artist
):
    store, _, service = collection
    artist = CatalogRef("demo", "artist1")
    track = replace(recording(), artist_refs=(artist,) if catalog_artist else ())
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [track])
    await History(store).record_catalog(
        Actor("two", frozenset({"1"})), "1", [replace(track, ref=CatalogRef("demo", "other"))]
    )
    await History(store).record_catalog(
        Actor("one", frozenset({"1"})),
        "1",
        [
            replace(
                track, ref=CatalogRef("demo", "different"), artist="Other artist", artist_refs=()
            )
        ],
    )
    # A structural refresh/preparation replaces provisional group IDs, retaining
    # durable per-track identity. The original navigation still names the same music.
    await store.execute(
        "UPDATE collection_tracks SET album_id='native-album',artist_id='native-artist'"
        " WHERE track_key IN ('catalog:demo:t1','catalog:demo:other','catalog:demo:different')"
    )
    query = replace(
        PERSONAL,
        album_id=album_id(track.album),
        artist_id=artist_id(artist) if catalog_artist else "name:" + track.artist,
    )
    listing = await service.browse("one", "1", query, profile_id="all")
    assert {row["item_key"] for row in listing["rows"]} == {"catalog:demo:t1"}
    assert listing["total"] == 1 and listing["scope_label"]["album"] == "Cloud House"
    operation = await prepare(service, query=query)
    frozen = await store.fetchall(
        "SELECT track_key FROM collection_operation_items WHERE operation_id=?", [operation]
    )
    assert [row["track_key"] for row in frozen] == ["catalog:demo:t1"]


@pytest.mark.parametrize("scope", ["song", "album", "artist"])
async def test_pool_before_count_pagination_full_group_and_frozen_selection(collection, scope):
    store, access, service = collection
    # Each account's songs in an album of its own: a song added by hand brings its album,
    # and these accounts' albums must not touch.
    albums = {1: "x1", 2: "x1", 3: "x2", 4: "x2", 5: "x3", 6: "x3"}
    access.rows = [
        dict(row, albumId=albums[int(row["id"][1:])]) if int(row["id"][1:]) in albums else row
        for row in access.rows
    ]
    await service.refresh("one", "1")
    await mutate(service, selected=["catalog:s1", "catalog:s2"])
    await mutate(service, owner="two", selected=["catalog:s3", "catalog:s4"])
    target = await service.create_profile("one", "Target", start="empty")
    query = replace(PERSONAL, scope=scope, limit=1, sort="title")
    result = await service.browse("one", "1", query, profile_id=target)
    assert result["total"] == (2 if scope == "song" else 1)
    assert result["counts"] == {"all_count": 2, "saved_count": 0, "unsaved_count": 2}
    row = result["rows"][0]
    assert row["all_tracks"] == (1 if scope == "song" else 2) and row["editable"] == 1
    assert row["full_saved"] == 0
    assert await keys(service, profile=target, query=replace(PERSONAL, offset=1, limit=1)) == {
        "catalog:s2"
    }
    # A song search should not widen whole-album badges or actions into another user.
    narrowed = replace(query, search="Song 1")
    filtered = await service.browse("one", "1", narrowed, profile_id=target)
    assert filtered["rows"][0]["all_tracks"] == (1 if scope == "song" else 2)
    op = await prepare(service, query=query, profile=target)
    frozen = await store.fetchall(
        "SELECT track_key FROM collection_operation_items WHERE operation_id=?", [op]
    )
    assert {r["track_key"] for r in frozen} == {"catalog:s1", "catalog:s2"}
    await service.approve("one", op)
    assert (await finish(service, op))["changed"] == 2
    assert await keys(service, "two") == {"catalog:s3", "catalog:s4"}


async def test_legacy_query_json_keeps_server_semantics(collection):
    store, _, service = collection
    legacy = asdict(PERSONAL)
    legacy.pop("pool")
    query = Query(**json.loads(json.dumps(legacy)))
    assert query.pool == "server"
    assert len(await keys(service, query=query)) == 7
    op = await prepare(service, query=query)
    # Recreate a genuinely old frozen operation document.
    await store.execute(
        "UPDATE collection_operations SET query=? WHERE id=?", [json.dumps(legacy), op]
    )
    assert (await service.operation("one", op))["total"] == 7
    assert not await keys(service)
    with pytest.raises(CollectionError):
        Query(pool="anyone")


async def test_trusted_history_is_bounded_idempotent_and_does_not_enable_or_scan(collection):
    store, access, service = collection
    actor = Actor("one", frozenset({"1"}))
    history = History(store)
    before = (await service.index_state("one", "1")).generation
    access.allowed = False  # Accepted request proof needs no dashboard credentials.
    await history.record_catalog(actor, "1", [recording()])
    await history.record_catalog(actor, "1", [replace(recording(), title="Changed later")])
    assert (await history.snapshots(actor, "1", [recording().ref]))["demo:t1"] == recording()
    assert await history.snapshots(Actor("two", actor.library_ids), "1", [recording().ref]) == {}
    assert not await store.fetchone("SELECT 1 FROM collection_membership")
    assert not await store.fetchone("SELECT 1 FROM collection_preferences")
    with pytest.raises(CollectionError):
        await history.record_catalog(actor, "2", [recording()])
    with pytest.raises(CollectionError):
        await history.record_catalog(actor, "1", [recording()] * 501)
    access.allowed = True
    result = await service.browse("one", "1", PERSONAL, profile_id="all")
    assert result["rows"][0]["unavailable_tracks"] == 1
    assert result["rows"][0]["restorable_tracks"] == 1
    assert result["rows"][0]["saved"] == 0
    assert (await service.index_state("one", "1")).generation == before


async def test_history_joins_publisher_transaction_and_rolls_back(collection):
    store, _, _ = collection
    history = History(store)
    with pytest.raises(RuntimeError):
        async with store.transaction() as db:
            await history.record_catalog(Actor("one", frozenset({"1"})), "1", [recording()], db=db)
            raise RuntimeError("publisher rollback")
    assert not await store.fetchone("SELECT 1 FROM collection_history")
    assert not await store.fetchone(
        "SELECT 1 FROM collection_track_aliases WHERE alias='catalog:demo:t1'"
    )
    assert not await store.fetchone(
        "SELECT 1 FROM collection_tracks WHERE track_key='catalog:demo:t1'"
    )


async def test_worker_history_and_membership_commit_or_rollback_together(collection, monkeypatch):
    store, _, service = collection
    from shijhon.collection import service as module

    original = module.record_positive

    async def fail(*args):
        await original(*args)
        raise RuntimeError("interrupted attribution")

    monkeypatch.setattr(module, "record_positive", fail)
    op = await prepare(service, query=replace(PERSONAL, pool="server"))
    await service.approve("one", op)
    assert (await finish(service, op))["state"] == "failed"
    assert not await store.fetchone("SELECT 1 FROM collection_membership")
    assert not await store.fetchone("SELECT 1 FROM collection_history")
    monkeypatch.setattr(module, "record_positive", original)
    await service.retry("one", op)
    assert (await finish(service, op))["changed"] == 7
    await service.tick()
    assert (await store.fetchone("SELECT count(*) n FROM collection_history"))["n"] == 7


async def test_unavailable_history_restores_only_after_approval_and_extends_complete_generation(
    collection,
):
    store, access, service = collection
    history = History(store)
    actor = Actor("one", frozenset({"1"}))
    await history.record_catalog(actor, "1", [recording()])
    before = (await service.index_state("one", "1")).generation
    calls = []

    async def restore(actual_actor, library, refs, *, authorize):
        await authorize()
        calls.append((actual_actor, library, refs))
        await managed_link(store, "restored")
        return {"demo:t1": song(99, id="restored", path="_shijhon/restored.flac")}

    service.restore = restore
    op = await prepare(service)
    assert not calls
    assert (await service.operation("one", op))["affected"] == 1
    assert not await service.visible_song_ids("one", "1", ["restored"])
    await service.approve("one", op)
    assert (await finish(service, op))["state"] == "done"
    assert calls == [(actor, "1", [recording().ref])]
    assert await service.visible_song_ids("one", "1", ["restored"]) == {"restored"}
    state = await service.index_state("one", "1")
    assert state.generation == before
    additions = await store.fetchall("SELECT generation,song_id FROM collection_index_additions")
    assert [(row["generation"], row["song_id"]) for row in additions] == [(before, "restored")]
    present = await store.fetchall("SELECT generation FROM collection_tracks WHERE present=1")
    assert len(present) == len(access.rows) + 1
    assert {r["generation"] for r in present} == {state.generation}
    # Retry/tick does not publish another generation or duplicate files/history.
    await service.tick()
    assert len(calls) == 1
    assert (await service.index_state("one", "1")).generation == state.generation
    await store.execute("DELETE FROM releases")
    await service.refresh("one", "1")
    result = await service.browse("one", "1", PERSONAL, profile_id="all")
    rows = {r["item_key"]: r for r in result["rows"]}
    assert rows["catalog:demo:t1"]["saved"] == 0
    assert rows["catalog:demo:t1"]["restorable_tracks"] == 1
    # The song was added by hand: its album's other songs stay in All meanwhile.
    assert all(rows[f"catalog:s{n}"]["saved"] == 1 for n in range(1, 7))
    assert (await history.snapshots(actor, "1", [recording().ref]))["demo:t1"] == recording()


@pytest.mark.parametrize("failure", ["omitted", "wrong", "exception"])
async def test_failed_or_partial_restore_never_enables_missing_history(collection, failure):
    store, _, service = collection
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [recording()])
    before = (await service.index_state("one", "1")).generation

    async def restore(*args, authorize):
        await authorize()
        if failure == "omitted":
            return {}
        if failure == "wrong":
            return {"demo:other": song(99)}
        raise RuntimeError("private upstream error")

    service.restore = restore
    op = await prepare(service)
    await service.approve("one", op)
    result = await finish(service, op)
    assert result["state"] == ("done" if failure == "omitted" else "failed")
    assert result["skipped"] == (1 if failure == "omitted" else 0)
    assert "private" not in result["error"]
    assert not await store.fetchone("SELECT 1 FROM collection_membership")
    assert (await service.index_state("one", "1")).generation == before


async def test_missing_original_is_visible_but_never_sent_to_catalog_restore(collection):
    _, access, service = collection
    await credit(service, "one", 7)
    target = await service.create_profile("one", "Subset", start="empty")
    access.rows = []
    await service.refresh("one", "1")
    calls = []

    async def restore(*args, authorize):
        await authorize()
        calls.append(args)
        return {}

    service.restore = restore
    result = await service.browse("one", "1", PERSONAL, profile_id=target)
    assert result["rows"][0]["unavailable_tracks"] == 1
    assert result["rows"][0]["restorable_tracks"] == 0
    op = await prepare(service, profile=target)
    await service.approve("one", op)
    assert (await finish(service, op))["skipped"] == 1
    assert not calls


@pytest.mark.parametrize(
    "change",
    [
        "archived_profile",
        "retargeted_operation",
        "stopped_operation",
        "unapproved_operation",
        "newer_revision",
        "replaced_frozen_revision",
        "processed_item",
        "changed_alias",
        "noneditable_item",
        "revoked_caller",
    ],
)
async def test_restore_revalidates_frozen_intent_after_publisher_wait(collection, change):
    store, access, service = collection
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [recording()])
    target = await service.create_profile("one", "Target", start="empty")
    other = await service.create_profile("one", "Other", start="empty")
    operation = await prepare(service, profile=target)
    await service.approve("one", operation)
    op = await service.operation("one", operation)
    generation = (await service.index_state("one", "1")).generation
    waiting, release = anyio.Event(), anyio.Event()
    effects, refused = [], []

    async def restore(actor, library, refs, *, authorize):
        await authorize()
        waiting.set()
        await release.wait()
        await authorize()
        effects.extend(refs)
        await managed_link(store, "restored")
        return {"demo:t1": song(99, id="restored", path="_shijhon/restored.flac")}

    async def apply():
        try:
            await service._restore_batch(op)
        except CollectionError as exc:
            refused.append(exc)

    service.restore = restore
    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(apply)
            await waiting.wait()
            if change == "archived_profile":
                await service.archive_profile("one", target)
            elif change == "retargeted_operation":
                await store.execute(
                    "UPDATE collection_operations SET profile_id=? WHERE id=?",
                    [other, operation],
                )
            elif change == "stopped_operation":
                await store.execute(
                    "UPDATE collection_operations SET state='failed' WHERE id=?", [operation]
                )
            elif change == "unapproved_operation":
                await store.execute(
                    "UPDATE collection_operations SET approved_at=NULL WHERE id=?", [operation]
                )
            elif change == "newer_revision":
                newer = await service.prepare(
                    "one",
                    "1",
                    PERSONAL,
                    profile_id=target,
                    action="remove",
                    request_key="newer-remove",
                )
                await service.repo.freeze(await service.operation("one", newer))
                await service.approve("one", newer)
                await service._batch(await service.operation("one", newer))
                assert (await service.operation("one", newer))["state"] == "done"
            elif change == "replaced_frozen_revision":
                await store.execute(
                    "INSERT INTO collection_membership VALUES ('one',?,'1','catalog:demo:t1',"
                    "0,1,'newer')",
                    [target],
                )
                await store.execute(
                    "UPDATE collection_operation_items SET before_revision=1 WHERE operation_id=?",
                    [operation],
                )
            elif change == "processed_item":
                await store.execute(
                    "UPDATE collection_operation_items SET processed=1 WHERE operation_id=?",
                    [operation],
                )
            elif change == "changed_alias":
                await store.execute(
                    "UPDATE collection_track_aliases SET alias='catalog:demo:other'"
                    " WHERE alias='catalog:demo:t1'"
                )
            elif change == "noneditable_item":
                await store.execute(
                    "UPDATE collection_tracks SET original=1 WHERE track_key='catalog:demo:t1'"
                )
            else:
                access.allowed = False
            release.set()

    assert len(refused) == 1 and not effects
    assert not await store.fetchone("SELECT 1 FROM placeholders")
    assert not await store.fetchone("SELECT 1 FROM collection_membership WHERE saved=1")
    assert (await store.fetchone("SELECT generation FROM collection_index"))["generation"] == (
        generation
    )
    assert not await store.fetchone(
        "SELECT 1 FROM collection_tracks WHERE track_key='catalog:demo:t1' AND present=1"
    )


@pytest.mark.parametrize("empty", [False, True])
async def test_restore_rechecks_durable_intent_after_callback_even_when_empty(collection, empty):
    store, _, service = collection
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [recording()])
    operation = await prepare(service)
    await service.approve("one", operation)
    generation = (await service.index_state("one", "1")).generation

    async def restore(actor, library, refs, *, authorize):
        await authorize()
        await store.execute(
            "UPDATE collection_operations SET state='failed' WHERE id=?", [operation]
        )
        return {} if empty else {"demo:t1": song(99)}

    service.restore = restore
    with pytest.raises(CollectionError, match="selection changed"):
        await service._restore_batch(await service.operation("one", operation))
    assert (await service.index_state("one", "1")).generation == generation
    assert not await store.fetchone("SELECT 1 FROM collection_membership")


async def test_restore_keeps_approved_target_when_active_profile_changes(collection):
    store, _, service = collection
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [recording()])
    target = await service.create_profile("one", "Target", start="empty")
    other = await service.create_profile("one", "Other", start="empty")
    operation = await prepare(service, profile=target)
    await service.approve("one", operation)

    async def restore(actor, library, refs, *, authorize):
        await service.activate_profile("one", other)
        await authorize()
        await managed_link(store, "restored")
        return {"demo:t1": song(99, id="restored", path="_shijhon/restored.flac")}

    service.restore = restore
    assert (await finish(service, operation))["changed"] == 1
    assert (await service.profile("one"))["id"] == other
    assert await service.visible_song_ids("one", "1", ["restored"], profile_id=target) == {
        "restored"
    }
    assert not await service.visible_song_ids("one", "1", ["restored"], profile_id=other)


async def test_restore_retry_reuses_partially_published_batch_idempotently(collection):
    store, _, service = collection
    await History(store).record_catalog(
        Actor("one", frozenset({"1"})), "1", [recording(1), recording(2)]
    )
    # A custom library: in All the recorded activity holds the songs once they are back.
    target = await service.create_profile("one", "Target", start="empty")
    operation = await prepare(service, profile=target)
    await service.approve("one", operation)
    published, calls = {}, []
    before = (await service.index_state("one", "1")).generation

    async def restore(actor, library, refs, *, authorize):
        calls.append(list(refs))
        for ref in refs:
            await authorize()
            if str(ref) not in published:
                ident = "restored-" + ref.id
                await managed_link(store, ident, str(ref))
                published[str(ref)] = song(99, id=ident, path=f"_shijhon/{ident}.flac")
            if len(calls) == 1:
                raise RuntimeError("synthetic interruption after first publication")
        return published

    service.restore = restore
    assert (await finish(service, operation))["state"] == "failed"
    assert len(published) == 1
    assert not await store.fetchone("SELECT 1 FROM collection_membership")
    assert (await service.index_state("one", "1")).generation == before
    await service.retry("one", operation)
    assert (await finish(service, operation))["changed"] == 2
    assert calls == [[recording(1).ref, recording(2).ref]] * 2
    assert (await store.fetchone("SELECT count(*) n FROM placeholders"))["n"] == 2
    assert (await store.fetchone("SELECT count(*) n FROM collection_membership"))["n"] == 2
    assert (await store.fetchone("SELECT count(*) n FROM collection_history"))["n"] == 2
    restored = ["restored-t1", "restored-t2"]
    assert await service.visible_song_ids("one", "1", restored, profile_id=target) == set(restored)
    assert await service.visible_song_ids("one", "1", restored, profile_id="shijhon") == set(
        restored
    )
    complete = (await service.index_state("one", "1")).generation
    assert complete == before
    await service.tick()
    assert len(calls) == 2
    assert (await service.index_state("one", "1")).generation == complete


async def test_legacy_originals_visible_without_assignment_until_personal_conversion(tmp_path):
    store = await Store.open(tmp_path / "legacy.db")
    service = Collection(
        store,
        FakeAccess([song(1), song(2), song(3, path="_shijhon/3.flac", albumId="c1")]),
    )
    try:
        await Origins(store).initialize_legacy("1")
        await service.refresh("one", "1")
        assert await keys(service) == {"native:s1", "native:s2"}
        assert (await service.browse("one", "1", PERSONAL))["legacy_shared"] is True
        assert not await store.fetchone("SELECT 1 FROM collection_history")
        assert not await store.fetchone("SELECT 1 FROM collection_originals")

        class Setup(FakeAccess):
            async def authorize_setup(self, owner):
                return await self.authorize(owner)

        origins = Origins(store)
        setup = await origins.review_legacy(
            actor_id="admin",
            library_id="1",
            legacy_owner_id="one",
            existing_owner_ids=["one", "two"],
            access=Setup(),
        )
        await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=Setup())
        assert await keys(service) == {"native:s1", "native:s2"}
        # Existing shared choices are explicitly preserved by reviewed setup; a
        # newly created account gains no credit from those originals being present.
        assert await keys(service, "two") == {"native:s1", "native:s2"}
        assert await keys(service, "new") == set()
        assert len(await keys(service, query=replace(PERSONAL, pool="server"))) == 3
        assert (await service.browse("one", "1", PERSONAL))["legacy_shared"] is False
    finally:
        await store.close()


async def test_migration_backfills_only_positive_owner_evidence_and_is_idempotent():
    db = sqlite3.connect(":memory:")
    migrations = _bundled_migrations()
    try:
        apply_migrations(db, migrations[:15])
        db.execute(
            "INSERT INTO collection_profiles(id,owner_id,name,created) VALUES ('p','one','P',0)"
        )
        for key, value in (("positive", 1), ("negative", 0), ("null", None)):
            db.execute(
                "INSERT INTO collection_membership VALUES ('one','p','1',?,?,0,'')", [key, value]
            )
        for ident, state, approved, processed, revision in (
            ("successful", "done", 1, 1, 1),
            ("partly-applied", "failed", 1, 1, 1),
            ("reviewed", "review", None, 0, None),
            ("queued", "queued", 1, 0, None),
            ("skipped", "done", 1, 1, None),
        ):
            db.execute(
                "INSERT INTO collection_operations(id,owner_id,library_id,profile_id,"
                "request_key,fingerprint,action,state,query,selected,approved_at,created)"
                " VALUES (?,'one','1','p',?,?,'save',?,'{}','[]',?,0)",
                [ident, ident, ident, state, approved],
            )
            db.execute(
                "INSERT INTO collection_operation_items"
                "(operation_id,track_key,processed,after_revision)"
                " VALUES (?,?,?,?)",
                [ident, ident, processed, revision],
            )
        db.commit()
        assert apply_migrations(db, migrations[:16]) == 16
        assert set(db.execute("SELECT owner_id,track_key FROM collection_history")) == {
            ("one", "positive"),
            ("one", "successful"),
            ("one", "partly-applied"),
        }
        assert apply_migrations(db, migrations[:16]) == 16
        assert db.execute("SELECT count(*) FROM collection_history").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM collection_originals").fetchone()[0] == 0
    finally:
        db.close()


async def test_activity_is_told_apart_from_choices_on_the_collection_page(collection):
    """A recorded app action or publication is the account's activity (its album joins the
    Shijhon library); a Save on the Collection page is a choice of its library only."""
    store, _, service = collection
    await mutate(service, selected=["catalog:s1"])
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [recording(2)])

    async def activity():
        rows = await store.fetchall(
            "SELECT track_key,activity FROM collection_history WHERE owner_id='one'"
        )
        return {r["track_key"]: r["activity"] for r in rows}

    assert await activity() == {"catalog:s1": 0, "catalog:demo:t2": 1}
    assert not await service.visible_song_ids("one", "1", ["s1"], profile_id="shijhon")
    # The same song played later: activity from then on, the choice stays the choice.
    await store.execute(
        "INSERT INTO collection_track_aliases VALUES ('1','catalog:demo:t1','catalog:s1')"
    )
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [recording(1)])
    assert (await activity())["catalog:s1"] == 1
    assert await service.visible_song_ids(
        "one", "1", [f"s{n}" for n in range(1, 8)], profile_id="shijhon"
    ) == {f"s{n}" for n in range(1, 7)}  # the whole album, not the import's own album


@pytest.mark.parametrize("extend_now", [False, True])
async def test_restore_keeps_other_owners_ready_queries_and_reconciles_additions(
    collection, tmp_path, extend_now
):
    from shijhon.collection.query_index import Binding, QueryIndex, SourcePage

    store, access, service = collection
    generation = (await service.index_state("one", "1")).generation
    await History(store).record_catalog(Actor("one", frozenset({"1"})), "1", [recording()])

    async def valid():
        pass

    async def fetch(start, limit):
        return SourcePage(access.rows, len(access.rows))

    async def restore(actor, library, refs, *, authorize):
        await authorize()
        await managed_link(store, "restored")
        return {"demo:t1": song(99, id="restored", path="_shijhon/restored.flac")}

    service.restore = restore
    operation = await prepare(service)
    await service.approve("one", operation)
    async with anyio.create_task_group() as group:
        index = QueryIndex(tmp_path / "queries" / "db", tmp_path / "history.db", group.start_soon)
        bindings = [
            Binding(owner, frozenset({"1"}), (("1", generation),)) for owner in ("one", "two")
        ]
        for binding in bindings:
            await index.ensure(binding, "songs", fetch, valid)
        if extend_now:
            service.query_index = index
        assert (await finish(service, operation))["state"] == "done"
        for binding in bindings:
            assert await index.has_ready(binding, "songs")
            # The durable revision also repairs facts if the process stopped before extend.
            await index.page(binding.key("songs"), binding, "song", None, 0, 20)
            with sqlite3.connect(index.path) as conn:
                assert conn.execute(
                    "SELECT 1 FROM edges WHERE facts=? AND kind='song' AND item_id='restored'",
                    (binding.facts,),
                ).fetchone()
        assert (await service.index_state("one", "1")).generation == generation
        await index.close()
