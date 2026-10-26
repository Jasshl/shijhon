"""Actual native sharing, retained-byte recovery and identities after import removal."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest

from shijhon.collection.origins import Origins
from shijhon.collection.reader import CallerLibrary
from shijhon.collection.service import Collection
from shijhon.music_imports import ImportError, ImportLimits, ManifestEntry, MusicImports
from shijhon.music_imports.removal import ImportRemovals
from shijhon.navidrome.checks import PlaceholderWrites
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import UsageSource
from shijhon.store import Store
from tests.acceptance.test_music_imports import Caller
from tests.harness.library import simple_album, write_album
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, _platform_key

pytestmark = [pytest.mark.anyio, pytest.mark.canary]


async def advance(removals, owner, job, wanted):
    for _ in range(20):
        status = await removals.status(owner, job)
        assert not status.get("error"), status
        if status["state"] == wanted:
            return status
        assert await removals.tick(), status
    pytest.fail(f"Removal did not reach {wanted}")


@pytest.mark.parametrize("when", ["before-restore", "restore-scan", "unlink-refresh"])
async def test_actual_delayed_flac_scan_restores_and_returns_to_normal_review(
    navidrome_factory, tmp_path, monkeypatch, when
):
    from shijhon.music_imports import removal as removal_module

    monkeypatch.setattr(removal_module, "BATCH", 1)
    nd = navidrome_factory()
    owner = nd.create_user("delayed-owner", "invented-password")
    login = nd.login("delayed-owner", "invented-password")
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD)
    caller = CallerLibrary(SimpleNamespace(client=SimpleNamespace(native=native)))
    caller.remember(await caller.access.verify_native(login["token"]), login["token"])
    store = await Store.open(tmp_path / "state.db")
    collection, scans = Collection(store, caller), ScanCoordinator(native)
    (nd.music / "_imports").mkdir()
    enabled, used = False, set()
    overlap, overlap_aliases, removal_gates = False, [], 0
    client = nd.client("delayed-owner", "invented-password")

    async def record_use():
        for song in await caller.songs(owner, "1", 0, 100):
            if song["id"] not in used:
                await anyio.to_thread.run_sync(
                    lambda song=song: client.ok(
                        "createPlaylist", [("name", "Delayed scan use"), ("songId", song["id"])]
                    )
                )
                used.add(song["id"])

    async def scan(folders):
        nonlocal overlap, overlap_aliases
        # Delay the real scanner, not its native evidence or collection answers.
        if enabled:
            if overlap:
                overlap = False
                removed = await store.fetchone(
                    "SELECT i.* FROM music_import_items i JOIN music_import_removal_items r"
                    " ON r.item_id=i.id WHERE r.state='removed' LIMIT 1"
                )
                assert removed and not (nd.music / "_imports" / removed["final_name"]).exists()
                await collection.refresh(owner, "1")
                overlap_aliases = [
                    tuple(r) for r in await store.fetchall("SELECT * FROM collection_track_aliases")
                ]
                assert any(r[1].startswith("native:") for r in overlap_aliases)
                assert not any(r[1] == "import:" + removed["asset_id"] for r in overlap_aliases)
            await scans.scan(folders)
            await record_use()

    imports = MusicImports(
        store,
        caller,
        library_id="1",
        library_path=nd.music,
        limits=replace(ImportLimits(), min_free_bytes=0),
        scan=scan,
        native_lookup=caller.imported_file,
        refresh=collection.refresh,
    )
    collection.index.asset_lookup = imports.asset_lookup
    writes = PlaceholderWrites(native)

    async def gate():
        nonlocal enabled, overlap, removal_gates
        assert await writes.refused(fresh=True) is None
        phase = await store.fetchone("SELECT state FROM music_import_removals")
        if when == "unlink-refresh" and phase and phase["state"] == "removing":
            removal_gates += 1
            if removal_gates == 2:
                await scans.scan(["_imports"])
                await record_use()
                assert len(used) == 2
                enabled = overlap = True

    async def administrator(ident):
        return (await caller._native(ident))[0].is_admin

    removals = ImportRemovals(
        imports,
        usage=UsageSource(nd.data / "navidrome.db", export=False),
        gate=gate,
        administrator=administrator,
        native_absent=caller.imported_file_absent,
    )
    files = write_album(tmp_path / "source", simple_album("Glass Orchard", "Delayed", 2))
    originals = [f.read_bytes() for f in files]
    try:
        await Origins(store).initialize_personal("1")
        await imports.initialize()
        job = await imports.create(owner, "delayed-scan")
        job = await imports.declare(
            owner,
            job["id"],
            "page",
            [
                ManifestEntry(str(i), f"Folder/{f.name}", f.stat().st_size)
                for i, f in enumerate(files)
            ],
        )
        await imports.seal(owner, job["id"])
        for item in job["items"]:

            async def stream(item=item):
                yield originals[int(item["client_key"])]

            await imports.receive(owner, job["id"], item["id"], 0, stream())
        await imports.review(owner, job["id"])
        while await imports.tick():
            pass
        review = await imports.status(owner, job["id"])
        await imports.approve(owner, job["id"], review["revision"], "publish")
        assert await imports.tick()
        assert (await imports.status(owner, job["id"]))["error"] == "indexing"
        assert await caller.songs(owner, "1", 0, 100) == []
        receipts = [
            tuple(r)
            for r in await store.fetchall(
                "SELECT id,asset_id,song_id,track_key,state FROM music_import_items ORDER BY id"
            )
        ]
        await imports.cancel(owner, job["id"])
        await removals.review(owner, job["id"])
        review = await advance(removals, owner, job["id"], "review")
        await removals.approve(owner, job["id"], review["fingerprint"], "remove")
        target = {
            "before-restore": "removing",
            "restore-scan": "removed",
            "unlink-refresh": "restoring",
        }[when]
        await advance(removals, owner, job["id"], target)
        enabled = True
        if when == "before-restore":
            await scan(["_imports"])
            assert len(await caller.songs(owner, "1", 0, 100)) == 2
        await removals.restore(owner, job["id"])
        restored = await advance(removals, owner, job["id"], "restored")
        assert restored["newly_indexed_files"] == 2 and not restored["error"], restored
        for offset in range(2):
            entry = (await removals.status(owner, job["id"], offset=offset))["items"][0]
            assert entry["song_id"] == "" and entry["restored_song_id"] in used
        assert [
            tuple(r)
            for r in await store.fetchall(
                "SELECT id,asset_id,song_id,track_key,state FROM music_import_items ORDER BY id"
            )
        ] == receipts
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        for item in job["items"]:
            final = nd.music / "_imports" / ("batch-" + job["id"]) / item["path"]
            assert final.read_bytes() == originals[int(item["client_key"])]
        aliases = [
            tuple(r)
            for r in await store.fetchall("SELECT * FROM collection_track_aliases ORDER BY alias")
        ]
        assert set(overlap_aliases) <= set(aliases)  # existing keys were never repointed
        await removals.review(owner, job["id"])
        current = await advance(removals, owner, job["id"], "review")
        assert current["total"] == 2 and current["remove_files"] == 0
        for offset in range(2):
            entry = (await removals.status(owner, job["id"], offset=offset))["items"][0]
            assert entry["song_id"] in used and "in a playlist" in entry["reasons"]
        assert [
            tuple(r)
            for r in await store.fetchall("SELECT * FROM collection_track_aliases ORDER BY alias")
        ] == aliases
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        assert all(f.read_bytes() == original for f, original in zip(files, originals, strict=True))
    finally:
        client.close()
        await imports.aclose()
        await caller.aclose()
        await native.aclose()
        await store.close()


@pytest.mark.parametrize("mixed", [False, True])
async def test_native_unindexed_trueaudio_removal_restore_and_partial_indexed_batch(
    navidrome_factory, tmp_path, monkeypatch, mixed
):
    if not _platform_key().startswith("darwin_"):
        pytest.skip("This unmodified native build's TrueAudio indexing gap is Darwin-specific.")
    from shijhon.music_imports import removal as removal_module

    monkeypatch.setattr(removal_module, "BATCH", 1)  # durable cursors across mixed identities
    nd = navidrome_factory()
    owner = nd.create_user("unindexed-owner", "invented-password")
    login = nd.login("unindexed-owner", "invented-password")
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD)
    caller = CallerLibrary(SimpleNamespace(client=SimpleNamespace(native=native)))
    caller.remember(await caller.access.verify_native(login["token"]), login["token"])
    store = await Store.open(tmp_path / "state.db")
    collection = Collection(store, caller)
    scans = ScanCoordinator(native)
    (nd.music / "_imports").mkdir()
    imports = MusicImports(
        store,
        caller,
        library_id="1",
        library_path=nd.music,
        limits=replace(ImportLimits(), min_free_bytes=0),
        scan=scans.scan,
        native_lookup=caller.imported_file,
        refresh=collection.refresh,
    )
    collection.index.asset_lookup = imports.asset_lookup
    writes = PlaceholderWrites(native)

    async def gate():
        assert await writes.refused(fresh=True) is None

    async def administrator(ident):
        return (await caller._native(ident))[0].is_admin

    removals = ImportRemovals(
        imports,
        usage=UsageSource(nd.data / "navidrome.db", export=False),
        gate=gate,
        administrator=administrator,
        native_absent=caller.imported_file_absent,
    )
    fmt = next(fmt for fmt in IMPORT_FORMATS if fmt.key == "trueaudio")
    files = [import_tone(tmp_path / str(tagged), fmt, tagged=tagged) for tagged in (False, True)]
    if mixed:
        files.extend(write_album(tmp_path / "flac", simple_album("Glass Orchard", "Partial", 1)))
    originals = [f.read_bytes() for f in files]
    try:
        await Origins(store).initialize_personal("1")
        await imports.initialize()
        job = await imports.create(owner, "unindexed-batch")
        job = await imports.declare(
            owner,
            job["id"],
            "page",
            [
                ManifestEntry(str(i), f"Folder {i}/{f.name}", f.stat().st_size)
                for i, f in enumerate(files)
            ],
        )
        await imports.seal(owner, job["id"])
        for item in job["items"]:

            async def stream(item=item):
                yield originals[int(item["client_key"])]

            await imports.receive(owner, job["id"], item["id"], 0, stream())
        await imports.review(owner, job["id"])
        while await imports.tick():
            pass
        review = await imports.status(owner, job["id"])
        await imports.approve(owner, job["id"], review["revision"], "publish")
        assert await imports.tick()
        paused = await imports.status(owner, job["id"])
        assert paused["state"] == "paused" and paused["error"] == "indexing", paused
        indexed = await caller.songs(owner, "1", 0, 100)
        assert len(indexed) == int(mixed)
        known_ids = {r["id"] for r in indexed}
        for item in paused["items"]:
            final = nd.music / "_imports" / ("batch-" + job["id"]) / item["path"]
            assert final.read_bytes() == originals[int(item["client_key"])]
            if item["path"].endswith(".tta"):
                assert item["song_id"] == "" and item["state"] == "placed"
        before = [tuple(r) for r in await store.fetchall("SELECT * FROM collection_originals")]
        await imports.cancel(owner, job["id"])
        await removals.review(owner, job["id"])
        reviewed_status = await advance(removals, owner, job["id"], "review")
        assert reviewed_status["total"] == reviewed_status["remove_files"] == len(files)
        reviewed_items = []
        for offset in range(len(files)):
            reviewed_items.extend((await removals.status(owner, job["id"], offset=offset))["items"])
        assert sum(i["song_id"] == "" for i in reviewed_items) == 2
        await removals.approve(owner, job["id"], reviewed_status["fingerprint"], "remove")
        removed = await advance(removals, owner, job["id"], "removed")
        assert removed["removed_files"] == len(files)
        assert not list((nd.music / "_imports").rglob("*.tta"))
        assert not list((nd.music / "_imports").rglob("*.flac"))
        assert await caller.songs(owner, "1", 0, 100) == []
        for entry in indexed:
            # Omission of missing=false really does expose the native retained ID.
            assert not await caller.imported_file_absent(owner, "1", entry["path"])
        await removals.restore(owner, job["id"])
        restored = await advance(removals, owner, job["id"], "restored")
        assert not restored["error"]
        assert {r["id"] for r in await caller.songs(owner, "1", 0, 100)} == known_ids
        for item in paused["items"]:
            final = nd.music / "_imports" / ("batch-" + job["id"]) / item["path"]
            assert final.read_bytes() == originals[int(item["client_key"])]
        assert [
            tuple(r) for r in await store.fetchall("SELECT * FROM collection_originals")
        ] == before
        for row in await store.fetchall("SELECT * FROM music_import_items"):
            if row["display_path"].endswith(".tta"):
                assert (row["state"], row["song_id"], row["track_key"]) == ("placed", "", "")
        assert all(f.read_bytes() == original for f, original in zip(files, originals, strict=True))
    finally:
        await imports.aclose()
        await caller.aclose()
        await native.aclose()
        await store.close()


async def test_native_shared_use_explicit_removal_restore_bytes_tags_ids_and_provenance(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    owner = nd.create_user("file-owner", "invented-owner-password")
    second = nd.create_user("shared-listener", "invented-second-password")
    recovery = nd.native(
        "POST",
        "user",
        json={
            "userName": "recovery-admin",
            "name": "Recovery",
            "password": "invented-recovery-password",
            "isAdmin": True,
        },
    ).json()["id"]
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD)
    caller = Caller(
        native,
        [
            nd.login("file-owner", "invented-owner-password"),
            nd.login("shared-listener", "invented-second-password"),
            nd.login("recovery-admin", "invented-recovery-password"),
        ],
    )
    store = await Store.open(tmp_path / "state.db")
    collection = Collection(store, caller)
    scan = ScanCoordinator(native)
    (nd.music / "_imports").mkdir()
    imports = MusicImports(
        store,
        caller,
        library_id="1",
        library_path=nd.music,
        limits=replace(ImportLimits(), min_free_bytes=0),
        scan=scan.scan,
        native_lookup=caller.exact,
        refresh=collection.refresh,
    )
    collection.index.asset_lookup = imports.asset_lookup
    writes = PlaceholderWrites(native)

    async def gate():
        assert await writes.refused(fresh=True) is None

    async def administrator(ident):
        return (await caller.access.verify_native(caller.tokens[ident])).is_admin

    removals = ImportRemovals(
        imports,
        usage=UsageSource(nd.data / "navidrome.db", export=False),
        gate=gate,
        administrator=administrator,
    )
    try:
        await Origins(store).initialize_personal("1")
        await imports.initialize()
        files = write_album(
            tmp_path / "originals", simple_album("Glass Orchard", "Retained Music", 2)
        )
        originals = [f.read_bytes() for f in files]
        job = await imports.create(owner, "synthetic-removal")
        ident = job["id"]
        job = await imports.declare(
            owner,
            ident,
            "page",
            [
                ManifestEntry(str(i), f"Folder/{f.name}", f.stat().st_size)
                for i, f in enumerate(files)
            ],
        )
        await imports.seal(owner, ident)
        for item in job["items"]:

            async def stream(item=item):
                yield originals[int(item["client_key"])]

            await imports.receive(owner, ident, item["id"], 0, stream())
        await imports.review(owner, ident)
        while await imports.tick():
            pass
        review = await imports.status(owner, ident)
        await imports.approve(owner, ident, review["revision"], "publish")
        while await imports.tick():
            pass
        done = await imports.status(owner, ident)
        assert done["state"] == "done", done
        ids = [i["song_id"] for i in done["items"]]
        before = [
            tuple(r)
            for r in await store.fetchall("SELECT * FROM collection_originals ORDER BY track_key")
        ]
        listener = nd.client("shared-listener", "invented-second-password")
        try:
            playlist = await anyio.to_thread.run_sync(
                lambda: listener.ok(
                    "createPlaylist", [("name", "Shared saved music"), ("songId", ids[0])]
                )["playlist"]
            )
            await anyio.to_thread.run_sync(lambda: listener.ok("star", {"id": ids[0]}))
            await removals.review(owner, ident)
            reviewed = await advance(removals, owner, ident, "review")
            assert reviewed["total"] == 2 and reviewed["remove_files"] == 1
            used = next(i for i in reviewed["items"] if i["song_id"] == ids[0])
            assert not used["remove_file"] and "in a playlist" in used["reasons"]
            with pytest.raises(ImportError):
                await removals.approve(second, ident, reviewed["fingerprint"], "foreign")
            with pytest.raises(ImportError):
                await removals.approve(owner, ident, "old-review", "wrong")
            with pytest.raises(ImportError):
                await removals.review(owner, ident, include_used=True)
            await removals.approve(owner, ident, reviewed["fingerprint"], "remove-unused")
            removed = await advance(removals, owner, ident, "removed")
            assert removed["removed_files"] == 1
            current = await caller.songs(owner, "1", 0, 100)
            assert [s["id"] for s in current] == [ids[0]]
            assert (
                await anyio.to_thread.run_sync(
                    lambda: listener.ok("getPlaylist", {"id": playlist["id"]})
                )
            )["playlist"]["entry"][0]["id"] == ids[0]
            assert [
                tuple(r)
                for r in await store.fetchall(
                    "SELECT * FROM collection_originals ORDER BY track_key"
                )
            ] == before
            await removals.restore(owner, ident)
            await advance(removals, owner, ident, "restored")
            assert {s["id"] for s in await caller.songs(owner, "1", 0, 100)} == set(ids)
            for item in done["items"]:
                downloaded = await anyio.to_thread.run_sync(
                    lambda item=item: listener.request("download", {"id": item["song_id"]}).content
                )
                assert (
                    hashlib.sha256(downloaded).digest()
                    == hashlib.sha256(originals[int(item["client_key"])]).digest()
                )
            # A new use between review and approval invalidates the whole frozen plan.
            await removals.review(owner, ident)
            reviewed = await advance(removals, owner, ident, "review")
            await anyio.to_thread.run_sync(lambda: listener.ok("star", {"id": ids[1]}))
            await removals.approve(owner, ident, reviewed["fingerprint"], "stale-sharing")
            assert await removals.tick()
            assert (await removals.status(owner, ident))["state"] == "stale"
            assert {s["id"] for s in await caller.songs(owner, "1", 0, 100)} == set(ids)
            # An actual administrator can review another account's used batch, then
            # recover it even after the importing account is deleted. No impersonation.
            await removals.review(recovery, ident, include_used=True)
            admin_review = await advance(removals, recovery, ident, "review")
            assert admin_review["remove_files"] == 2
            await removals.approve(recovery, ident, admin_review["fingerprint"], "admin-remove")
            await advance(removals, recovery, ident, "removed")
            nd.native("DELETE", f"user/{owner}").raise_for_status()
            with pytest.raises(ImportError):
                await removals.restore(owner, ident)
            await removals.restore(recovery, ident)
            await advance(removals, recovery, ident, "restored")
            assert {s["id"] for s in await caller.songs(recovery, "1", 0, 100)} == set(ids)
            assert [
                tuple(r)
                for r in await store.fetchall(
                    "SELECT * FROM collection_originals ORDER BY track_key"
                )
            ] == before
        finally:
            listener.close()
    finally:
        await imports.aclose()
        await caller.http.aclose()
        await native.aclose()
        await store.close()
