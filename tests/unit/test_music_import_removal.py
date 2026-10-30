"""Regressions at the final retained-file mutation boundary."""

from __future__ import annotations

import hashlib
import json
import signal
import sqlite3
import sys
import textwrap
from dataclasses import replace
from unittest.mock import AsyncMock

import anyio
import pytest

from shijhon.collection.contracts import CollectionError
from shijhon.music_imports import ImportError, NativeFile
from shijhon.music_imports.removal import ImportRemovals
from shijhon.navidrome.usage import UsageSource
from tests.unit import test_music_imports as imports_tests
from tests.unit.test_usage_export import NAVIDROME

harness = imports_tests.harness
reviewed = imports_tests.reviewed

pytestmark = pytest.mark.anyio


async def unindexed_removal(h):
    async def no_index():
        h.access.rows = []

    h.after_scan = no_index
    review = await reviewed(h)
    await h.service.approve("a", review["id"], review["revision"], "publish")
    assert await h.service.tick()
    assert (await h.service.status("a", review["id"]))["error"] == "indexing"
    await h.service.cancel("a", review["id"])
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    path = h.root / "synthetic-native.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(NAVIDROME)
        for table in (
            "annotation",
            "playlist_tracks",
            "bookmark",
            "playqueue",
            "share",
            "scrobbles",
            "media_file",
            "album",
        ):
            conn.execute("DELETE FROM " + table)  # noqa: S608 -- fixed synthetic tables
    return retained_unindexed_removal(h), item, path


def retained_unindexed_removal(h):
    async def no_index():
        h.access.rows = []

    h.after_scan = no_index
    return ImportRemovals(
        h.service,
        usage=UsageSource(h.root / "synthetic-native.db", export=False),
        gate=AsyncMock(),
        administrator=AsyncMock(return_value=False),
        native_absent=AsyncMock(return_value=True),
    )


async def to_review(service, item):
    await service.review("a", item["job_id"])
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "review" and not status["error"], status
    assert status["total"] == status["remove_files"] == 1
    assert status["items"][0]["song_id"] == ""
    return status


async def test_unindexed_review_remove_restore_does_not_mint_identity_or_credit(harness):
    service, item, _ = await unindexed_removal(harness)
    status = await to_review(service, item)
    await service.approve("a", item["job_id"], status["fingerprint"], "remove")
    for _ in range(2):
        assert await service.tick()
    assert (await service.status("a", item["job_id"]))["state"] == "removed"
    final = harness.root / "_imports" / item["final_name"]
    assert not final.exists()
    await service.restore("a", item["job_id"])
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "restored" and not status["error"], status
    assert hashlib.sha256(final.read_bytes()).hexdigest() == item["digest"]
    assert not await harness.store.fetchone("SELECT 1 FROM collection_originals")
    current = await harness.store.fetchone("SELECT song_id,track_key,state FROM music_import_items")
    assert tuple(current) == ("", "", "placed")


@pytest.mark.parametrize(
    "failure",
    [
        "no-callback",
        "native-missing",
        "usage-present",
        "usage-missing",
        "complete",
        "lost-id",
        "lost-key",
        "credit",
    ],
)
async def test_unindexed_review_fails_closed_for_known_or_uncertain_identity(harness, failure):
    service, item, path = await unindexed_removal(harness)
    if failure == "no-callback":
        service.native_absent = None
    elif failure == "native-missing":
        service.native_absent.return_value = False
    elif failure.startswith("usage-"):
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO media_file VALUES ('old',?,'Synthetic',?,'','','','')",
                ["_imports/" + item["final_name"], int(failure == "usage-missing")],
            )
            conn.execute("INSERT INTO playlist_tracks VALUES (1,'used','old')")
    elif failure == "credit":
        await harness.store.execute(
            "INSERT INTO collection_originals VALUES ('a','1','known',?,0)",
            ["import:" + item["id"]],
        )
    else:
        column, value = {
            "complete": ("state", "complete"),
            "lost-id": ("song_id", "old"),
            "lost-key": ("track_key", "known"),
        }[failure]
        await harness.store.execute(f"UPDATE music_import_items SET {column}=?", [value])  # noqa: S608
    await service.review("a", item["job_id"])
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "reviewing" and status["error"], status
    assert (harness.root / "_imports" / item["final_name"]).exists()


@pytest.mark.parametrize("boundary", ["checking", "after-hash", "postscan"])
async def test_later_native_indexing_invalidates_empty_identity_without_losing_bytes(
    harness, boundary
):
    service, item, _ = await unindexed_removal(harness)
    status = await to_review(service, item)
    await service.approve("a", item["job_id"], status["fingerprint"], "remove")
    if boundary == "checking":
        service.native_absent.return_value = False
        assert await service.tick()
        assert (await service.status("a", item["job_id"]))["state"] == "stale"
    else:
        assert await service.tick()  # checking -> removing
        if boundary == "after-hash":
            verify = harness.service.storage.verify

            def indexing(*args, **kwargs):
                verify(*args, **kwargs)
                service.native_absent.return_value = False

            harness.service.storage.verify = indexing
        elif boundary == "postscan":

            async def indexed_after_scan():
                service.native_absent.return_value = False

            harness.after_scan = indexed_after_scan
        assert await service.tick()
        assert (await service.status("a", item["job_id"]))["state"] == "restoring"
        if boundary == "after-hash":
            assert (harness.root / "_imports" / item["final_name"]).exists()
    assert not await harness.store.fetchone("SELECT 1 FROM collection_originals")
    await harness.service._verify(item)


async def removed_unindexed(h):
    service, item, path = await unindexed_removal(h)
    status = await to_review(service, item)
    await service.approve("a", item["job_id"], status["fingerprint"], "remove")
    assert await service.tick()
    assert await service.tick()
    assert (await service.status("a", item["job_id"]))["state"] == "removed"
    h.collection.index.asset_lookup = h.service.asset_lookup
    return service, item, path


def index_delayed(h, item, path, *, missing=False):
    final = h.root / "_imports" / item["final_name"]
    ident = "import-" + hashlib.sha256(str(final).encode()).hexdigest()
    for row in h.access.rows:
        row["libraryId"] = 1
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM media_file")
        conn.execute(
            "INSERT INTO media_file VALUES (?,?,'Synthetic',?,'','','','')",
            [ident, "_imports/" + item["final_name"], int(missing)],
        )
        conn.execute("INSERT INTO playlist_tracks VALUES (1,'used',?)", [ident])
    return ident


@pytest.mark.parametrize("when", ["before-restore", "restore-scan"])
async def test_delayed_identity_restore_finishes_and_new_review_accounts_for_its_use(harness, when):
    h = harness
    service, item, path = await removed_unindexed(h)
    asset = item["asset_id"]
    ident = index_delayed(h, item, path, missing=True) if when == "before-restore" else None

    async def indexed():
        index_delayed(h, item, path)

    h.after_scan = indexed
    service.native_absent.return_value = False
    await service.restore("a", item["job_id"])
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert not status["error"], status["error"]
    assert status["state"] == "restored" and not status["error"], status
    native_id = h.access.rows[0]["id"]
    assert ident is None or native_id == ident
    assert status["newly_indexed_files"] == 1
    assert status["items"][0]["song_id"] == ""
    assert status["items"][0]["restored_song_id"] == native_id
    final = h.root / "_imports" / item["final_name"]
    assert hashlib.sha256(final.read_bytes()).hexdigest() == item["digest"]
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    receipt = await h.store.fetchone(
        "SELECT song_id,track_key,asset_id,state FROM music_import_items"
    )
    assert tuple(receipt) == ("", "", asset, "placed")
    aliases = [tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")]
    assert any(r[1] == "import:" + asset for r in aliases)
    await service.review("a", item["job_id"])
    assert await service.tick()
    fresh = await service.status("a", item["job_id"])
    assert fresh["state"] == "review" and not fresh["error"], fresh
    assert fresh["items"][0]["song_id"] == native_id
    assert fresh["remove_files"] == 0 and "in a playlist" in fresh["items"][0]["reasons"]
    assert [
        tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")
    ] == aliases
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")


@pytest.mark.parametrize("failure", ["collision", "known-id", "wrong-source", "grant", "asset"])
async def test_delayed_restore_still_fails_closed_for_conflicts_and_access(harness, failure):
    h = harness
    service, item, path = await removed_unindexed(h)
    final = h.root / "_imports" / item["final_name"]
    if failure == "collision":
        final.write_bytes(b"unrelated original")
    elif failure == "wrong-source":
        opened = service.usage.open

        def other():
            conn, snapshot = opened()
            return conn, replace(snapshot, source="different")

        service.usage.open = other
    elif failure == "grant":
        h.service.native_lookup = AsyncMock(side_effect=CollectionError("Sign in again."))
    elif failure == "known-id":
        h.after_scan = None
        await h.scan(["_imports"])
        # A known native alias from another physical file must not be stolen.
        from tests.unit.test_collection import song

        ident = index_delayed(h, item, path)
        h.access.rows = [song(1, id=ident, path="other/file.flac")]
        await h.collection.refresh("a", "1")
        h.after_scan = None
    else:
        h.service.asset_lookup = AsyncMock(return_value={})
    if failure in ("known-id", "asset"):

        async def indexed():
            index_delayed(h, item, path)

        h.after_scan = indexed
    await service.restore("a", item["job_id"])
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "restoring" and status["error"], status
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    await h.service._verify(item)
    if failure == "collision":
        assert final.read_bytes() == b"unrelated original"


async def test_known_indexed_restore_refuses_replacement_before_learning_aliases(harness):
    h = harness
    service, item = await removal(h)
    service._evidence = ImportRemovals._evidence.__get__(service)
    await service.restore("a", item["job_id"])
    aliases = [tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")]
    refreshes = h.refreshes
    h.service.native_lookup = AsyncMock(
        return_value=NativeFile(
            "replacement", "_imports/" + item["final_name"], item["expected_size"]
        )
    )
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "restoring" and "exact imported file" in status["error"], status
    assert h.refreshes == refreshes
    assert [
        tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")
    ] == aliases
    assert (
        hashlib.sha256((h.root / "_imports" / item["final_name"]).read_bytes()).hexdigest()
        == item["digest"]
    )


async def test_overlapping_refresh_after_unlink_preserves_native_key_and_allows_fresh_review(
    harness,
):
    h = harness
    service, item, database = await unindexed_removal(h)
    h.collection.index.asset_lookup = h.service.asset_lookup
    review = await to_review(service, item)
    await service.approve("a", item["job_id"], review["fingerprint"], "remove")
    assert await service.tick()
    final = h.root / "_imports" / item["final_name"]
    scan = h.service.scan
    aliases = []

    async def overlap(folders):
        nonlocal aliases
        assert not final.exists()
        from tests.unit.test_collection import song

        ident = index_delayed(h, item, database)
        h.access.rows = [
            song(
                1,
                id=ident,
                libraryId=1,
                path="_imports/" + item["final_name"],
                size=item["expected_size"],
            )
        ]
        await h.collection.refresh("a", "1")
        aliases = [
            tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")
        ]
        assert aliases == [("1", "native:" + ident, "native:" + ident)]
        with sqlite3.connect(database) as conn:
            conn.execute("UPDATE media_file SET missing=1")
        service.native_absent.return_value = False
        h.service.scan = scan
        await scan(folders)

    h.service.scan = overlap
    assert await service.tick()
    assert (await service.status("a", item["job_id"]))["state"] == "restoring"

    async def indexed():
        index_delayed(h, item, database)

    h.after_scan = indexed
    await service.restore("a", item["job_id"])
    assert await service.tick()
    restored = await service.status("a", item["job_id"])
    assert restored["state"] == "restored" and not restored["error"], restored
    ident = restored["items"][0]["restored_song_id"]
    assert restored["newly_indexed_files"] == 1
    now = [tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")]
    assert set(aliases) <= set(now)
    assert ("1", "import:" + item["asset_id"], "native:" + ident) in now
    assert hashlib.sha256(final.read_bytes()).hexdigest() == item["digest"]
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    receipt = await h.store.fetchone("SELECT song_id,track_key,asset_id FROM music_import_items")
    assert tuple(receipt) == ("", "", item["asset_id"])
    await service.review("a", item["job_id"])
    assert await service.tick()
    fresh = await service.status("a", item["job_id"])
    assert fresh["state"] == "review" and not fresh["error"], fresh
    assert fresh["items"][0]["song_id"] == ident
    assert fresh["remove_files"] == 0 and "in a playlist" in fresh["items"][0]["reasons"]


@pytest.mark.parametrize(
    "failure",
    [
        "stored-size",
        "stored-path",
        "stored-song",
        "not-original",
        "catalog",
        "asset",
        "native",
        "receipt",
        "distinct-asset",
        "corrupt-key",
        "missing-row",
        "stored-id",
        "metadata-type",
        "native-path",
        "native-size",
        "source-after-scan",
    ],
)
async def test_native_only_alias_reconciliation_refuses_conflicting_durable_evidence(
    harness, failure
):
    h = harness
    service, item, database = await removed_unindexed(h)
    from tests.unit.test_collection import song

    ident = index_delayed(h, item, database, missing=True)
    h.access.rows = [
        song(
            1,
            id=ident,
            libraryId=1,
            path="_imports/" + item["final_name"],
            size=item["expected_size"],
        )
    ]
    await h.collection.refresh("a", "1")
    key = "native:" + ident
    if failure in ("stored-size", "stored-song", "not-original"):
        column, value = {
            "stored-size": ("size", item["expected_size"] + 1),
            "stored-song": ("song_id", "other"),
            "not-original": ("original", 0),
        }[failure]
        await h.store.execute(f"UPDATE collection_tracks SET {column}=?", [value])  # noqa: S608
    elif failure in ("stored-path", "stored-id", "metadata-type"):
        row = await h.store.fetchone("SELECT metadata FROM collection_tracks")
        metadata = json.loads(row["metadata"])
        if failure == "metadata-type":
            metadata = []
        else:
            metadata["path" if failure == "stored-path" else "id"] = "other/file.flac"
        await h.store.execute("UPDATE collection_tracks SET metadata=?", [json.dumps(metadata)])
    elif failure == "distinct-asset":
        await h.store.execute(
            "INSERT INTO collection_track_aliases VALUES ('1',?,'native:other')",
            ["import:" + item["asset_id"]],
        )
    elif failure in ("catalog", "asset", "native"):
        prefix = "import" if failure == "asset" else failure
        await h.store.execute(
            "INSERT INTO collection_track_aliases VALUES ('1',?,?)", [prefix + ":other", key]
        )
    elif failure == "receipt":
        other = dict(item)
        other.update(
            id="other",
            asset_id="other",
            client_key="other",
            final_name="other.flac",
            song_id=ident,
            track_key=key,
        )
        columns = ",".join(other)
        await h.store.execute(
            f"INSERT INTO music_import_items ({columns}) VALUES ({','.join('?' for _ in other)})",  # noqa: S608 -- columns from the synthetic row
            list(other.values()),
        )
    elif failure == "corrupt-key":
        await h.store.execute("UPDATE collection_track_aliases SET track_key='corrupt'")
        await h.store.execute("UPDATE collection_tracks SET track_key='corrupt'")
    elif failure == "missing-row":
        await h.store.execute("DELETE FROM collection_tracks")
    elif failure in ("native-path", "native-size"):
        h.service.native_lookup = AsyncMock(
            return_value=NativeFile(
                ident,
                "other/file.flac" if failure == "native-path" else "_imports/" + item["final_name"],
                item["expected_size"] + int(failure == "native-size"),
            )
        )
    aliases = [tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")]

    async def indexed():
        index_delayed(h, item, database)
        if failure == "source-after-scan":
            opened = service.usage.open

            def other():
                conn, snapshot = opened()
                return conn, replace(snapshot, source="different")

            service.usage.open = other

    h.after_scan = indexed
    refreshes = h.refreshes
    await service.restore("a", item["job_id"])
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "restoring" and status["error"], status
    assert h.refreshes == refreshes
    assert [
        tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_track_aliases")
    ] == aliases
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    await h.service._verify(item)


async def test_unindexed_usage_source_change_refuses_approved_removal(harness):
    service, item, _ = await unindexed_removal(harness)
    status = await to_review(service, item)
    await service.approve("a", item["job_id"], status["fingerprint"], "remove")
    opened = service.usage.open

    def other_source():
        conn, snapshot = opened()
        return conn, replace(snapshot, source="another-native-database")

    service.usage.open = other_source
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["error"] and status["state"] == "checking"
    assert (harness.root / "_imports" / item["final_name"]).exists()


async def test_later_indexed_identity_and_use_require_new_review(harness):
    h = harness
    service, item, path = await unindexed_removal(h)
    status = await to_review(service, item)
    await service.approve("a", item["job_id"], status["fingerprint"], "remove")
    h.after_scan = None
    await h.scan(["_imports"])
    native = h.access.rows[0]
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO media_file VALUES (?,?,'Synthetic',0,'','','','')",
            [native["id"], native["path"]],
        )
        conn.execute("INSERT INTO playlist_tracks VALUES (1,'used',?)", [native["id"]])
    await h.collection.refresh("a", "1")  # learns the real native/asset alias, not import credit
    assert await service.tick()
    assert (await service.status("a", item["job_id"]))["state"] == "stale"
    await service.review("a", item["job_id"])
    assert await service.tick()
    changed = await service.status("a", item["job_id"])
    assert changed["state"] == "review" and not changed["error"], changed
    assert changed["total"] == 1 and changed["remove_files"] == 0
    assert changed["items"][0]["song_id"] == native["id"]
    assert "in a playlist" in changed["items"][0]["reasons"]
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    assert (h.root / "_imports" / item["final_name"]).exists()


async def test_unindexed_causally_stale_usage_snapshot_refuses_review(harness):
    service, item, _ = await unindexed_removal(harness)
    service.usage.covers = lambda snapshot, mark: False
    await service.review("a", item["job_id"])
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "reviewing" and "shared-use snapshot" in status["error"]
    assert (harness.root / "_imports" / item["final_name"]).exists()


async def removal(h):
    review = await reviewed(h)
    await h.service.approve("a", review["id"], review["revision"], "publish")
    while await h.service.tick():
        pass
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    await h.store.execute(
        "INSERT INTO music_import_removals"
        "(id,job_id,owner_id,state,include_used,created) VALUES ('remove',?,'a','removing',1,0)",
        [item["job_id"]],
    )
    await h.store.execute(
        "INSERT INTO music_import_removal_items"
        "(removal_id,item_id,song_id,reasons,use_key,remove_file)"
        " VALUES ('remove',?,?,'[]','key',1)",
        [item["id"], item["song_id"]],
    )
    return retained_removal(h), item


def retained_removal(h):
    service = ImportRemovals(
        h.service, usage=None, gate=AsyncMock(), administrator=AsyncMock(return_value=True)
    )

    async def stable(row, items, **kwargs):
        return "synthetic", [{"song_id": i["reviewed_song"], "use_key": "key"} for i in items]

    service._evidence = stable
    return service


async def test_expired_native_sign_in_explains_how_to_resume_review(harness):
    h = harness
    service, item = await removal(h)
    await h.store.execute("UPDATE music_import_removals SET state='reviewing'")
    h.service.refresh = AsyncMock(
        side_effect=CollectionError("Sign in again to refresh your music.")
    )
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "reviewing"
    assert status["error"] == "Sign in again to refresh your music."
    assert (h.root / "_imports" / item["final_name"]).is_file()


@pytest.mark.parametrize("revocation", ["administrator", "native-policy"])
async def test_hashing_finishes_before_fresh_role_and_native_policy_checks(harness, revocation):
    h = harness
    service, item = await removal(h)
    valid = True
    verify = h.service.storage.verify

    def revoked_during_hash(*args, **kwargs):
        nonlocal valid
        verify(*args, **kwargs)
        valid = False

    async def gate():
        if revocation == "native-policy" and not valid:
            raise ImportError("identity", "Native file retention changed.")

    async def administrator(owner):
        return valid if revocation == "administrator" else True

    h.service.storage.verify = revoked_during_hash
    service.gate, service.administrator = gate, administrator
    assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["error"] and status["state"] == "removing"
    assert (h.root / "_imports" / item["final_name"]).is_file()


async def test_demoted_during_native_policy_wait_cannot_remove_used_music(harness):
    service, item = await removal(harness)
    admin = True

    async def gate():
        nonlocal admin
        admin = False

    async def administrator(owner):
        return admin

    service.gate, service.administrator = gate, administrator
    assert await service.tick()
    assert (harness.root / "_imports" / item["final_name"]).is_file()
    assert (await service.status("a", item["job_id"]))["error"]


async def test_restore_replaces_removal_work_selected_before_the_job_lock(harness):
    service, item = await removal(harness)
    entered, release, selected = anyio.Event(), anyio.Event(), anyio.Event()
    first_gate = True

    async def gate():
        nonlocal first_gate
        if first_gate:
            first_gate = False
            entered.set()
            await release.wait()

    fetchone = harness.store.fetchone

    async def observe(sql, *args, **kwargs):
        row = await fetchone(sql, *args, **kwargs)
        if "SELECT * FROM music_import_removals WHERE state IN" in sql:
            assert row["state"] == "removing"
            selected.set()
        return row

    service.gate = gate
    harness.store.fetchone = observe
    replies = {}

    async def request_restore():
        replies["restore"] = await service.restore("a", item["job_id"])

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(request_restore)
            await entered.wait()
            tasks.start_soon(service.tick)
            await selected.wait()
            release.set()
    status = await service.status("a", item["job_id"])
    assert replies["restore"]["state"] in ("restoring", "restored")
    assert status["state"] in ("restoring", "restored") and not status["error"]
    assert (harness.root / "_imports" / item["final_name"]).is_file()


@pytest.mark.parametrize("boundary", ["quarantine", "restore"])
@pytest.mark.parametrize("unindexed", [False, True])
async def test_process_kill_after_synced_file_action_recovers_bytes_and_identity(
    harness, boundary, unindexed
):
    h = harness
    if unindexed:
        service, item, _ = await unindexed_removal(h)
        status = await to_review(service, item)
        await service.approve("a", item["job_id"], status["fingerprint"], "remove")
        assert await service.tick()
    else:
        service, item = await removal(h)
    provenance = [tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_originals")]
    if boundary == "restore":
        assert await service.tick()
        assert (await service.status("a", item["job_id"]))["state"] == "removed"
        await service.restore("a", item["job_id"])
    await h.service.aclose()  # release the real import writer lock for the child
    child = textwrap.dedent("""
        import os
        import signal
        import sys
        from pathlib import Path
        import anyio
        from shijhon.music_imports import removal as actions
        from shijhon.store import Store
        from tests.unit.test_music_imports import Harness
        from tests.unit.test_music_import_removal import (
            retained_removal, retained_unindexed_removal,
        )

        async def run():
            root = Path(sys.argv[1])
            store = await Store.open(root / "state.db")
            h = Harness(root, store)
            await h.service.initialize()
            build = retained_unindexed_removal if sys.argv[3] == "True" else retained_removal
            service = build(h)
            action = getattr(actions, sys.argv[2])
            def killed(storage, item, receipt):
                action(storage, item, receipt)
                os.kill(os.getpid(), signal.SIGKILL)
            setattr(actions, sys.argv[2], killed)
            await service.tick()
            raise AssertionError("durable file-action boundary not reached")

        anyio.run(run)
    """)
    with anyio.fail_after(15):
        crashed = await anyio.run_process(
            [sys.executable, "-B", "-c", child, str(h.root), boundary, str(unindexed)], check=False
        )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr.decode()
    state = await h.store.fetchone("SELECT state FROM music_import_removal_items")
    assert state["state"] == ("removing" if boundary == "quarantine" else "removed")
    final = h.root / "_imports" / item["final_name"]
    assert final.exists() is (boundary == "restore")
    h.service = h.build()
    await h.service.initialize()
    service = retained_unindexed_removal(h) if unindexed else retained_removal(h)
    assert await service.tick()
    if boundary == "quarantine":
        assert (await service.status("a", item["job_id"]))["state"] == "removed"
        await service.restore("a", item["job_id"])
        assert await service.tick()
    status = await service.status("a", item["job_id"])
    assert status["state"] == "restored" and not status["error"]
    assert hashlib.sha256(final.read_bytes()).hexdigest() == item["digest"]
    assert (await h.store.fetchone("SELECT song_id FROM music_import_items"))["song_id"] == item[
        "song_id"
    ]
    assert [
        tuple(r) for r in await h.store.fetchall("SELECT * FROM collection_originals")
    ] == provenance


async def test_additional_queue_share_and_album_use_invalidate_the_review(harness):
    h = harness
    service, item = await removal(h)
    path = h.root / "synthetic-native.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(NAVIDROME)
        for table in (
            "annotation",
            "playlist_tracks",
            "bookmark",
            "playqueue",
            "share",
            "scrobbles",
            "media_file",
            "album",
        ):
            conn.execute("DELETE FROM " + table)  # noqa: S608 -- fixed synthetic table names
        conn.execute(
            "INSERT INTO media_file VALUES (?,?,?,0,'al1','ar1','ar1','')",
            [item["song_id"], "_imports/" + item["final_name"], "Synthetic"],
        )
        conn.execute("INSERT INTO album VALUES ('al1','Synthetic','Invented','ar1','')")
        conn.execute(
            "INSERT INTO playqueue VALUES ('q1','u1',?,?)", [item["song_id"], item["song_id"]]
        )
        conn.execute("INSERT INTO share VALUES ('s1','u1','',?)", [item["song_id"]])
    service.usage = UsageSource(path, export=False)
    native = {**item, "native_id": item["song_id"]}
    _, before = await service._native_uses([native], False, "")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO playqueue VALUES ('q2','u2',?,?)", [item["song_id"], item["song_id"]]
        )
    _, queued = await service._native_uses([native], False, "")
    assert queued != before
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO share VALUES ('s2','u2','',?)", [item["song_id"]])
    _, shared = await service._native_uses([native], False, "")
    assert shared != queued
