"""Durable provenance and explicit, frozen legacy conversion."""

from __future__ import annotations

import sqlite3

import pytest

from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.origins import Origins, record_verified_original
from shijhon.collection.policy import default_profile_id
from shijhon.collection.service import Collection
from shijhon.store import Store
from tests.unit.test_collection import FakeAccess, song

pytestmark = pytest.mark.anyio


class SetupAccess(FakeAccess):
    async def authorize_setup(self, actor_id):
        if actor_id != "admin":
            raise CollectionError("Administrator required")
        return await self.authorize(actor_id)


async def test_explicit_initializers_are_idempotent_and_personal_sticky(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    origins = Origins(store)
    try:
        assert await origins.initialize_legacy("1") == 1
        assert await origins.initialize_legacy("1") == 1
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        with pytest.raises(CollectionError):
            await origins.initialize_personal("1")
        assert await origins.initialize_personal("2") == 1
        with pytest.raises(CollectionError):
            await origins.initialize_legacy("2")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be downgraded"):
            await store.execute(
                "UPDATE collection_library_policy SET mode='legacy_shared' WHERE library_id='2'"
            )
    finally:
        await store.close()


async def test_frozen_assignment_preserves_choices_without_new_account_or_future_file_grants(
    tmp_path,
):
    store = await Store.open(tmp_path / "state.db")
    access = SetupAccess([song(1), song(2)])
    service, origins = Collection(store, access), Origins(store)
    try:
        await origins.initialize_legacy("1")
        await service.refresh("admin", "1")
        custom = await service.create_profile("two", "Selection")
        empty = await service.create_profile("two", "Empty", start="empty")
        await service.activate_profile("two", custom)
        # (The library started as a copy of Original: s1 and s2 by hand. An earlier hand
        # removal of s2 replaces that choice.)
        await store.execute(
            "INSERT OR REPLACE INTO collection_membership"
            " VALUES ('two',?,'1','native:s2',0,7,'prior')",
            [custom],
        )
        setup = await origins.review_legacy(
            actor_id="admin",
            library_id="1",
            legacy_owner_id="one",
            existing_owner_ids=["one", "two"],
            access=access,
        )
        # Newly seen files and new accounts cannot expand an already reviewed grant. (In
        # an album of its own: an album with the account's original is whole in All.)
        access.rows.append(song(3, albumId="a3"))
        await service.refresh("two", "1")
        await service.profile("new")
        assert (
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
            == 2
        )
        assert (
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
            == 2
        )
        assert [
            tuple(r)
            for r in await store.fetchall(
                "SELECT owner_id,track_key FROM collection_originals ORDER BY track_key"
            )
        ] == [("one", "native:s1"), ("one", "native:s2")]
        choices = [
            dict(r)
            for r in await store.fetchall(
                "SELECT * FROM collection_membership WHERE owner_id='two' AND profile_id=?",
                [custom],
            )
        ]
        assert {r["track_key"]: r["saved"] for r in choices} == {"native:s1": 1, "native:s2": 0}
        removed = next(r for r in choices if r["track_key"] == "native:s2")
        assert removed["revision"] == 7 and removed["last_operation"] == "prior"
        assert not await store.fetchone(
            "SELECT 1 FROM collection_membership WHERE owner_id='new' OR profile_id=?", [empty]
        )
        assert (await service.profile("two"))["id"] == custom
        for owner, selection, expected in (
            ("one", "original", {"s1", "s2"}),
            ("one", "all", {"s1", "s2"}),
            ("one", "shijhon", set()),
            # Original imported library holds each person's own imports now; All keeps
            # the shared choices; Shijhon library holds no one's originals.
            ("two", "original", set()),
            ("two", "shijhon", set()),
            ("two", "all", {"s1", "s2"}),
            ("two", custom, {"s1"}),
            ("two", empty, set()),
            ("new", "all", set()),
        ):
            assert (
                await service.visible_song_ids(owner, "1", ["s1", "s2", "s3"], profile_id=selection)
                == expected
            )
        with pytest.raises(CollectionError):
            await origins.initialize_legacy("1")
    finally:
        await store.close()


async def test_interrupted_legacy_commit_rolls_back_policy_credit_and_shared_choices(tmp_path):
    store = await Store.open(tmp_path / "atomic.db")
    access = SetupAccess([song(1)])
    origins, service = Origins(store), Collection(store, access)
    try:
        await origins.initialize_legacy("1")
        await service.refresh("admin", "1")
        await service.activate_profile("two", "original")
        setup = await origins.review_legacy(
            actor_id="admin",
            library_id="1",
            legacy_owner_id="one",
            existing_owner_ids=["one", "two"],
            access=access,
        )
        execute = store.conn.execute

        def interrupted(sql, params=()):
            if not sql.startswith("UPDATE collection_library_policy SET mode='personal'"):
                return execute(sql, params)

            async def stopped():
                await execute(sql, params)
                raise RuntimeError("Stopped before commit")

            return stopped()

        store.conn.execute = interrupted
        with pytest.raises(RuntimeError, match="Stopped"):
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
        store.conn.execute = execute
        assert (await store.fetchone("SELECT mode FROM collection_library_policy"))[
            "mode"
        ] == "legacy_shared"
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        assert not await store.fetchone("SELECT 1 FROM collection_membership")
        assert not await store.fetchone("SELECT 1 FROM collection_profiles WHERE kind='custom'")
        assert (await service.profile("two"))["selection_id"] == "original"
        assert (await store.fetchone("SELECT state FROM collection_original_setups"))[
            "state"
        ] == "review"
        assert (
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
            == 2
        )
        assert await service.visible_song_ids("two", "1", ["s1"]) == {"s1"}
    finally:
        await store.close()


async def test_setup_rechecks_immutable_accounts_and_refuses_stale_native_mapping(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    access = SetupAccess([song(1)])
    origins, service = Origins(store), Collection(store, access)
    try:
        await origins.initialize_legacy("1")
        await service.refresh("admin", "1")
        setup = await origins.review_legacy(
            actor_id="admin",
            library_id="1",
            legacy_owner_id="one",
            existing_owner_ids=["one"],
            access=access,
        )
        original = access.authorize

        async def recreated(owner):
            return Actor("recreated", frozenset({"1"})) if owner == "one" else await original(owner)

        access.authorize = recreated
        with pytest.raises(CollectionError):
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
        access.authorize = original
        await store.execute("UPDATE collection_tracks SET song_id='replaced'")
        with pytest.raises(CollectionError, match="Reviewed music changed"):
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        assert (await store.fetchone("SELECT mode FROM collection_library_policy"))[
            "mode"
        ] == "legacy_shared"
    finally:
        await store.close()


async def test_preference_freeze_is_required_for_older_unapplied_reviews(tmp_path):
    store = await Store.open(tmp_path / "old-review.db")
    access = SetupAccess([song(1)])
    origins, service = Origins(store), Collection(store, access)
    try:
        await origins.initialize_legacy("1")
        await service.refresh("admin", "1")
        await service.activate_profile("two", "original")
        setup = await origins.review_legacy(
            actor_id="admin",
            library_id="1",
            legacy_owner_id="one",
            existing_owner_ids=["one", "two"],
            access=access,
        )
        # A review persisted before migration 0015 has no frozen preferences.
        await store.execute("DELETE FROM collection_original_setup_preferences")
        with pytest.raises(CollectionError, match="Review setup again"):
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        assert (await service.profile("two"))["selection_id"] == "original"
        assert await service.visible_song_ids("two", "1", ["s1"]) == {"s1"}
    finally:
        await store.close()


@pytest.mark.parametrize("later_choice", ["", "shijhon", "original"])
async def test_active_original_preserves_exact_effective_songs_with_preference_cas(
    tmp_path, later_choice
):
    store = await Store.open(tmp_path / "active-original.db")
    # The catalog song is in an album of its own: Original holds the originals' album whole.
    access = SetupAccess([song(1), song(2), song(3, path="_shijhon/catalog/3.flac", albumId="c3")])
    origins, service = Origins(store), Collection(store, access)
    try:
        await origins.initialize_legacy("1")
        await service.refresh("admin", "1")
        await service.activate_profile("two", "original")
        # All's own hand choices do not affect Original. Copying All would silently add
        # s3; applying its remove override would silently drop s2.
        for track, saved in (("native:s2", 0), ("catalog:s3", 1)):
            await store.execute(
                "INSERT INTO collection_membership VALUES ('two',?,'1',?,?,7,'earlier')",
                [default_profile_id("two"), track, saved],
            )
        await service.create_profile("two", "Shared original library", start="empty")
        candidates = ["s1", "s2", "s3", "s4"]
        before = await service.visible_song_ids("two", "1", candidates)
        assert before == {"s1", "s2"}
        setup = await origins.review_legacy(
            actor_id="admin",
            library_id="1",
            legacy_owner_id="one",
            existing_owner_ids=["one", "two"],
            access=access,
        )
        if later_choice:
            # Even an explicit re-selection of Original increments the preference
            # revision. The approved setup must respect that newer choice.
            await service.activate_profile("two", later_choice)
        await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
        active = await service.profile("two")
        if later_choice:
            assert active["selection_id"] == later_choice
            # Neither library holds another person's imports (All's choices are All's).
            assert await service.visible_song_ids("two", "1", candidates) == set()
            assert not await store.fetchone(
                "SELECT 1 FROM collection_profiles WHERE name='Shared original library (2)'"
            )
        else:
            assert not active["builtin"] and active["base_original"] == 0
            assert active["name"] == "Shared original library (2)"
            assert await service.visible_song_ids("two", "1", candidates) == before
            # Later structural originals never expand the independent snapshot (in an album
            # of its own: a later song of an album the snapshot holds would complete it).
            access.rows.append(song(4, albumId="a4"))
            await service.refresh("two", "1")
            assert await service.visible_song_ids("two", "1", candidates) == before
            await origins.apply_legacy(setup, actor_id="admin", expected_revision=1, access=access)
            assert (await service.profile("two"))["id"] == active["id"]
        assert [
            tuple(row)
            for row in await store.fetchall(
                "SELECT saved,revision,last_operation FROM collection_membership"
                " WHERE owner_id='two' AND profile_id=? AND track_key IN ('native:s2','catalog:s3')"
                " ORDER BY saved",
                [default_profile_id("two")],
            )
        ] == [(0, 7, "earlier"), (1, 7, "earlier")]
    finally:
        await store.close()


async def test_publication_is_atomic_idempotent_and_rejects_alias_conflicts(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    service = Collection(store, FakeAccess([song(1), song(2)]))
    origins = Origins(store)
    args = dict(
        owner_id="one",
        library_id="1",
        track_key="native:s1",
        song_id="s1",
        provenance_id="receipt:invented",
        asset_id="asset-one",
    )
    try:
        await origins.initialize_personal("1")
        await service.refresh("one", "1")
        with pytest.raises(CollectionError):
            await record_verified_original(store.conn, **args)
        with pytest.raises(RuntimeError):
            async with store.transaction() as conn:
                await record_verified_original(conn, **args)
                raise RuntimeError("receipt publication failed")
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        async with store.transaction() as conn:
            await record_verified_original(conn, **args)
            await record_verified_original(conn, **args)
        assert (await store.fetchone("SELECT count(*) n FROM collection_originals"))["n"] == 1
        assert (await store.fetchone("SELECT revision FROM collection_library_policy"))[
            "revision"
        ] == 2
        with pytest.raises(CollectionError, match="conflicts"):
            async with store.transaction() as conn:
                await record_verified_original(
                    conn, **{**args, "track_key": "native:s2", "song_id": "s2"}
                )
        assert (await store.fetchone("SELECT count(*) n FROM collection_originals"))["n"] == 1
    finally:
        await store.close()
