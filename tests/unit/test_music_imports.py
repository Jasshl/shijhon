"""Import safety using real synthetic MP3/FLAC bytes and durable SQLite recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from mutagen.flac import FLAC

from shijhon.collection.contracts import Actor
from shijhon.collection.origins import Origins
from shijhon.collection.service import Collection
from shijhon.music_imports import (
    AssetResolver,
    ImportError,
    ImportLimits,
    ManifestEntry,
    MusicImports,
    NativeFile,
)
from shijhon.music_imports import service as import_service
from shijhon.store import Store
from tests.harness.library import tone
from tests.unit.test_collection import FakeAccess, song

pytestmark = pytest.mark.anyio


class CountingAccess(FakeAccess):
    def __init__(self):
        super().__init__()
        self.source_pages = self.source_rows = 0

    async def songs(self, owner, library, offset, limit):
        rows = await super().songs(owner, library, offset, limit)
        self.source_pages += 1
        self.source_rows += len(rows)
        return rows


class Harness:
    def __init__(self, root, store, *, limits=None):
        self.root, self.store = root, store
        self.access = CountingAccess()
        self.collection = Collection(store, self.access)
        self.scans = self.refreshes = self.lookups = 0
        self.existing = []
        self.events = []
        self.before_scan = None
        self.after_scan = None
        self.before_refresh = None
        self.after_refresh = None
        self.mapping = None
        self.limits = limits or replace(ImportLimits(), min_free_bytes=0)
        self.service = self.build()

    def build(self):
        return MusicImports(
            self.store,
            self.access,
            library_id="1",
            library_path=self.root,
            limits=self.limits,
            scan=self.scan,
            native_lookup=self.lookup,
            refresh=self.refresh,
        )

    async def scan(self, folders):
        self.scans += 1
        self.events.append(("scan", ""))
        assert folders == ["_imports"]
        if self.before_scan:
            await self.before_scan()
        self.access.rows = self.existing + [
            song(
                n,
                id="import-" + hashlib.sha256(str(path).encode()).hexdigest(),
                path=path.relative_to(self.root).as_posix(),
                size=path.stat().st_size,
            )
            for n, path in enumerate(sorted((self.root / "_imports").rglob("*.flac")), 1)
        ]
        self.access.rows.extend(
            song(
                100 + n,
                id="import-" + hashlib.sha256(str(path).encode()).hexdigest(),
                path=path.relative_to(self.root).as_posix(),
                size=path.stat().st_size,
            )
            for n, path in enumerate(
                sorted(
                    p for p in (self.root / "_imports").rglob("*") if p.suffix.lower() == ".mp3"
                ),
                1,
            )
        )
        if self.after_scan:
            await self.after_scan()

    async def refresh(self, owner, library):
        self.refreshes += 1
        self.events.append(("refresh", ""))
        if self.before_refresh:
            await self.before_refresh()
        await self.collection.refresh(owner, library)
        if self.after_refresh:
            await self.after_refresh()

    async def lookup(self, owner, library, path):
        self.lookups += 1
        self.events.append(("lookup", path))
        assert owner in ("a", "b") and library == "1"
        if self.mapping:
            return self.mapping
        matches = [r for r in self.access.rows if r["path"] == path]
        if len(matches) != 1:
            return None
        return NativeFile(matches[0]["id"], path, matches[0]["size"])


@pytest.fixture
async def harness(tmp_path):
    # Resolve /var on macOS; production deliberately rejects symlink ancestors.
    root = tmp_path.resolve()
    (root / "_imports").mkdir()
    store = await Store.open(root / "state.db")
    await Origins(store).initialize_personal("1")
    harness = Harness(root, store)
    await harness.service.initialize()
    try:
        yield harness
    finally:
        await harness.service.aclose()
        await store.close()


async def chunks(data):
    for start in range(0, len(data), 4096):
        yield data[start : start + 4096]


def audio(fmt="flac", frequency=440):
    return tone(frequency, 0.3, fmt).read_bytes()


async def declared(h, entries=None, key="job", owner="a"):
    data = audio()
    entries = entries or [ManifestEntry("file", "Folder/Song.flac", len(data))]
    result = await h.service.create(owner, key)
    result = await h.service.declare(owner, result["id"], "page", entries)
    return await h.service.seal(owner, result["id"])


async def freeze(h, owner, job_id, **kwargs):
    result = await h.service.review(owner, job_id, **kwargs)
    assert result["state"] == "checking"
    for _ in range(result["total_files"] + 1):
        await h.service.tick()
        result = await h.service.status(owner, job_id)
        if result["state"] != "checking":
            break
    assert result["state"] == "review", result
    return result


async def reviewed(h, data=None, owner="a", key="job"):
    data = data or audio()
    result = await declared(h, [ManifestEntry("file", "Song.flac", len(data))], key, owner)
    item = result["items"][0]
    await h.service.receive(owner, result["id"], item["id"], item["attempt"], chunks(data))
    return await freeze(h, owner, result["id"])


async def approved_batch(h, count=3):
    data = audio()
    result = await declared(
        h, [ManifestEntry(str(n), f"{n}.flac", len(data)) for n in range(count)]
    )
    for item in result["items"]:
        await h.service.receive("a", result["id"], item["id"], item["attempt"], chunks(data))
    result = await freeze(h, "a", result["id"])
    return await h.service.approve("a", result["id"], result["revision"], "approve")


async def test_private_receive_frozen_review_explicit_approval_and_owner_receipt(harness):
    h = harness
    data = audio()
    result = await reviewed(h)
    assert result["state"] == "review" and result["eligible_files"] == 1
    assert result["items"][0]["warnings"]  # real untagged file: filename fallback warning
    assert h.scans == 0 and not await h.store.fetchone("SELECT 1 FROM collection_originals")
    assert list((h.root / "_imports").rglob("*.flac")) == []
    assert not await h.service.tick()
    approved = await h.service.approve("a", result["id"], result["revision"], "approve")
    assert approved["state"] == "approved"
    await h.service.tick()
    complete = await h.service.status("a", result["id"])
    assert complete["state"] == "done" and complete["complete_files"] == 1
    assert (next((h.root / "_imports").rglob("*.flac"))).read_bytes() == data
    assert [
        r["owner_id"] for r in await h.store.fetchall("SELECT * FROM collection_originals")
    ] == ["a"]
    assert (
        await h.store.fetchone(
            "SELECT alias FROM collection_track_aliases WHERE alias LIKE 'import:%'"
        )
    )[0]

    # Exact replay never consumes bytes, duplicates publication, or changes ownership.
    async def forbidden():
        pytest.fail("finished item stream was consumed")
        yield b""

    item = complete["items"][0]
    await h.service.receive("a", result["id"], item["id"], item["attempt"], forbidden())
    await h.service.approve("a", result["id"], result["revision"], "approve")
    assert not await h.service.tick()
    assert len(list((h.root / "_imports").rglob("*.flac"))) == 1
    assert complete["items"][0]["sha256"] == hashlib.sha256(data).hexdigest()
    serialized = json.dumps(complete)
    assert str(h.root) not in serialized and ".part" not in serialized
    assert "owner_id" not in serialized


@pytest.mark.parametrize("refusal", ["foreign", "revoked", "recreated", "policy", "attempt"])
async def test_authorization_and_admission_before_any_stream_consumption(harness, refusal):
    h = harness
    result = await declared(h)
    item, owner, attempt = result["items"][0], "a", 0
    if refusal == "foreign":
        owner = "b"
    elif refusal == "revoked":
        h.access.allowed = False
    elif refusal == "recreated":

        async def changed(_):
            return Actor("new-immutable-id", frozenset({"1"}))

        h.access.authorize = changed
    elif refusal == "policy":
        await h.store.execute("DELETE FROM collection_library_policy")
    else:
        attempt = 99

    async def forbidden():
        pytest.fail("refused request consumed upload bytes")
        yield b""

    with pytest.raises(ImportError):
        await h.service.receive(owner, result["id"], item["id"], attempt, forbidden())
    assert not list((h.root / "_imports" / ".incoming").glob("*.part"))


async def test_idempotent_pages_seal_conflicts_and_bounded_owner_only_status(harness):
    h = harness
    entries = [ManifestEntry(str(i), f"Folder/{i}.flac", 1) for i in range(501)]
    job = await h.service.create("a", "job")
    assert (await h.service.create("a", "job"))["id"] == job["id"]
    with pytest.raises(ImportError, match="500"):
        await h.service.declare("a", job["id"], "big", entries)
    await h.service.declare("a", job["id"], "page", entries[:3])
    await h.service.declare("a", job["id"], "page", entries[:3])
    with pytest.raises(ImportError):
        await h.service.declare("a", job["id"], "page", entries[:2])
    await h.service.seal("a", job["id"])
    await h.service.seal("a", job["id"])
    with pytest.raises(ImportError):
        await h.service.declare("a", job["id"], "late", entries[3:4])
    page = await h.service.status("a", job["id"], limit=2)
    assert page["total_files"] == 3 and len(page["items"]) == 2 and page["next_offset"] == 2
    assert len((await h.service.status("a", job["id"], offset=2))["items"]) == 1
    with pytest.raises(ImportError):
        await h.service.status("b", job["id"])
    assert await h.service.listing("b") == []


async def test_large_manifest_status_has_bounded_database_work_and_preserves_copy_details(harness):
    h = harness
    job = await declared(h, [ManifestEntry("first", "First.flac", 1)])
    original = job["items"][0]["id"]
    await h.service.receive("a", job["id"], original, 0, chunks(b"x"))
    async with h.store.transaction() as conn:
        await conn.executemany(
            "INSERT INTO music_import_items"
            " (id,asset_id,job_id,client_key,display_path,expected_size,reserved_size,"
            "state,staging_name,final_name) VALUES (?,?,?,?,?,1,1,'declared',?,?)",
            [
                (
                    f"{n:032x}",
                    f"asset-{n}",
                    job["id"],
                    f"key-{n}",
                    f"Song {n}.flac",
                    f"{n}.part",
                    f"batch-{job['id']}/{n}.flac",
                )
                for n in range(5999)
            ],
        )
        for key, state in (("active-copy", "eligible"), ("old-copy", "discarding")):
            await conn.execute(
                "INSERT INTO music_import_items"
                " (id,asset_id,job_id,client_key,display_path,expected_size,reserved_size,"
                "state,staging_name,final_name,derived_from,received_size)"
                " VALUES (?,?,?,?,?,1,1,?,?,?,?,1)",
                [key, key, job["id"], key, "First.flac", state, key, key, original],
            )

    # A deterministic instruction budget catches repeated full-table scans without a
    # wall-clock assertion that would fail on a busy development machine.
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 1000
        return instructions > 5_000_000

    await h.store.conn.set_progress_handler(progress, 1000)
    try:
        first = await h.service.status("a", job["id"], limit=100)
        last = await h.service.status("a", job["id"], offset=5999, limit=100)
    finally:
        await h.store.conn.set_progress_handler(None, 0)
    assert first["total_files"] == first["filtered_total"] == 6000
    assert first["total_bytes"] == last["total_bytes"] == 6000
    assert first["received_bytes"] == last["received_bytes"] == 1
    assert first["received_files"] == last["received_files"] == 1
    assert len(first["items"]) == 100 and first["next_offset"] == 100
    assert last["next_offset"] is None and len(last["items"]) == 1
    assert last["items"][0]["id"] == original
    assert last["items"][0]["copy_state"] == "eligible"
    assert last["copy_files"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.flac",
        "../escape.flac",
        "a/../x.mp3",
        "a\\x.mp3",
        "a//b.flac",
        "x\n.flac",
        "https:example.flac",
        "C:/Song.flac",
        "C:Song.flac",
    ],
)
async def test_display_paths_cannot_be_filesystem_inputs(harness, path):
    job = await harness.service.create("a", "job")
    with pytest.raises(ImportError):
        await harness.service.declare("a", job["id"], "page", [ManifestEntry("file", path, 3)])


async def test_reservations_minfree_owner_and_global_limits_before_bytes(harness, monkeypatch):
    h = harness
    h.service.limits = replace(h.limits, max_owner_bytes=10, max_global_bytes=12)
    first = await h.service.create("a", "first")
    await h.service.declare("a", first["id"], "one", [ManifestEntry("1", "1.flac", 8)])
    second = await h.service.create("a", "second")
    with pytest.raises(ImportError) as error:
        await h.service.declare("a", second["id"], "one", [ManifestEntry("1", "2.flac", 3)])
    assert error.value.code == "bytes_limit"
    other = await h.service.create("b", "other")
    with pytest.raises(ImportError):
        await h.service.declare("b", other["id"], "one", [ManifestEntry("1", "3.flac", 5)])
    monkeypatch.setattr(h.service.storage, "free", lambda: 9)
    with pytest.raises(ImportError) as error:
        await h.service.declare("b", other["id"], "two", [ManifestEntry("1", "3.flac", 2)])
    assert error.value.code == "space"
    await h.service.cancel("a", first["id"])
    await h.service.declare("b", other["id"], "two", [ManifestEntry("1", "3.flac", 2)])


@pytest.mark.parametrize("failure", ["short", "long", "digest", "cancel", "revoke"])
async def test_whole_file_retry_size_digest_cancellation_and_grant_checks(harness, failure):
    h = harness
    data = audio()
    entry = ManifestEntry("file", "song.flac", len(data), "0" * 64 if failure == "digest" else "")
    result = await declared(h, [entry])
    item = result["items"][0]

    async def failing():
        if failure == "short":
            yield data[:-1]
        elif failure == "long":
            yield data + b"x"
        elif failure == "cancel":
            yield data[:10]
            raise anyio.get_cancelled_exc_class()()
        elif failure == "revoke":
            yield data[:10]
            h.access.allowed = False
            yield data[10:]
        else:
            yield data

    with pytest.raises((ImportError, anyio.get_cancelled_exc_class())):
        await h.service.receive("a", result["id"], item["id"], 0, failing())
    h.access.allowed = True
    status = await h.service.status("a", result["id"])
    assert status["items"][0]["attempt"] == 1 and status["items"][0]["state"] == "declared"
    assert not list((h.root / "_imports" / ".incoming").glob("*.part"))
    if failure != "digest":
        await h.service.receive("a", result["id"], item["id"], 1, chunks(data))
        assert (await h.service.status("a", result["id"]))["received_files"] == 1


async def test_upload_rechecks_current_access_by_elapsed_time_not_chunk_count(harness, monkeypatch):
    h = harness
    result = await declared(h, [ManifestEntry("file", "song.flac", 24)])
    item = result["items"][0]
    clock = [0.0]
    monkeypatch.setattr(
        import_service,
        "time",
        SimpleNamespace(time=import_service.time.time, monotonic=lambda: clock[0]),
    )
    checks = []
    original = h.access.authorize

    async def authorize(owner):
        checks.append(clock[0])
        return await original(owner)

    monkeypatch.setattr(h.access, "authorize", authorize)

    async def stream():
        for now in (0.0, 0.25, 0.75, 1.0, 1.9, 2.0):
            clock[0] = now
            yield b"data"

    received = await h.service.receive("a", result["id"], item["id"], 0, stream())
    assert received["received_files"] == 1
    assert received["received_bytes"] == 24
    assert checks.count(0.0) == 2  # fresh admission checks
    assert checks.count(1.0) == 1 and checks.count(2.0) == 3  # interval, final and status
    assert not any(now in checks for now in (0.25, 0.75, 1.9))


@pytest.mark.parametrize("when", ["periodic", "finish"])
async def test_upload_revocation_at_interval_or_before_receipt_discards_partial_bytes(
    harness, monkeypatch, when
):
    h = harness
    result = await declared(h, [ManifestEntry("file", "song.flac", 8)])
    item = result["items"][0]
    clock = [0.0]
    monkeypatch.setattr(
        import_service,
        "time",
        SimpleNamespace(time=import_service.time.time, monotonic=lambda: clock[0]),
    )

    async def stream():
        yield b"data"
        h.access.allowed = False
        if when == "periodic":
            clock[0] = 1.0
        yield b"data"

    with pytest.raises(ImportError) as exc:
        await h.service.receive("a", result["id"], item["id"], 0, stream())
    assert exc.value.code == "access"
    h.access.allowed = True
    status = await h.service.status("a", result["id"])
    assert status["received_files"] == 0
    assert status["received_bytes"] == 0
    assert status["items"][0]["state"] == "declared"
    assert status["items"][0]["attempt"] == 1
    assert not list((h.root / "_imports" / ".incoming").glob("*.part"))


async def test_review_exclusions_revision_no_late_upload_and_invalid_audio(harness):
    h = harness
    data = audio()
    result = await declared(
        h,
        [
            ManifestEntry("good", "song.flac", len(data)),
            ManifestEntry("bad", "broken.mp3", 5),
            ManifestEntry("archive", "bundle.zip", 5),
        ],
    )
    for item in result["items"]:
        if item["state"] == "declared":
            await h.service.receive(
                "a",
                result["id"],
                item["id"],
                0,
                chunks(data if item["client_key"] == "good" else b"abcde"),
            )
    reviewed = await freeze(h, "a", result["id"])
    assert reviewed["eligible_files"] == 1 and reviewed["rejected_files"] == 2
    good = next(i for i in reviewed["items"] if i["client_key"] == "good")
    excluded = await freeze(h, "a", result["id"], exclude=[good["id"]])
    assert excluded["revision"] == reviewed["revision"] + 1 and excluded["excluded_files"] == 1
    with pytest.raises(ImportError):
        await h.service.approve("a", result["id"], reviewed["revision"], "stale")
    included = await freeze(h, "a", result["id"])
    assert included["revision"] == excluded["revision"] + 1
    assert (await freeze(h, "a", result["id"]))["revision"] == included["revision"]
    await h.service.approve("a", result["id"], included["revision"], "approve")
    with pytest.raises(ImportError):
        await freeze(h, "a", result["id"])
    with pytest.raises(ImportError):
        await h.service.approve("a", result["id"], included["revision"], "different")


async def test_changed_staging_after_review_cannot_publish(harness):
    result = await reviewed(harness)
    staged = next((harness.root / "_imports" / ".incoming").glob("*.part"))
    staged.write_bytes(b"tampered")
    await harness.service.approve("a", result["id"], result["revision"], "approve")
    await harness.service.tick()
    assert (await harness.service.status("a", result["id"]))["state"] == "paused"
    assert not list((harness.root / "_imports").rglob("*.flac"))
    assert not await harness.store.fetchone("SELECT 1 FROM collection_originals")


@pytest.mark.parametrize("crash", ["intent", "linked", "scanned", "revoked", "cancelled"])
async def test_publication_recovery_retains_original_and_never_duplicates(
    harness, monkeypatch, crash
):
    h = harness
    result = await reviewed(h)
    await h.service.approve("a", result["id"], result["revision"], "approve")
    original = h.service.storage.place
    if crash in ("intent", "linked"):

        def place(staged, final):
            if crash == "linked":
                original(staged, final)
            raise OSError("simulated crash")

        monkeypatch.setattr(h.service.storage, "place", place)
    else:

        async def stop():
            if crash == "revoked":
                h.access.allowed = False
            elif crash == "cancelled":
                await h.service.cancel("a", result["id"])
            else:
                raise OSError("simulated scan failure")

        h.after_scan = stop
    await h.service.tick()
    h.access.allowed = True
    status = await h.service.status("a", result["id"])
    assert status["state"] == ("cancelled" if crash == "cancelled" else "paused")
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    paths = list((h.root / "_imports").rglob("*.flac"))
    if crash != "intent":
        assert len(paths) == 1 and paths[0].read_bytes() == audio()
    if crash == "cancelled":
        await h.service.resume("a", result["id"])
        assert not await h.service.tick()
        return
    await h.service.aclose()
    h.after_scan = None
    h.service = h.build()
    await h.service.initialize()
    await h.service.resume("a", result["id"])
    await h.service.tick()
    assert (await h.service.status("a", result["id"]))["state"] == "done"
    assert len(list((h.root / "_imports").rglob("*.flac"))) == 1


async def test_collision_and_symlink_never_replace_or_adopt_preexisting_file(harness):
    h = harness
    result = await reviewed(h)
    item = await h.store.fetchone("SELECT * FROM music_import_items WHERE job_id=?", [result["id"]])
    occupied = h.root / "_imports" / item["final_name"]
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_bytes(audio())  # even identical bytes are not this receipt's inode
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    assert (await h.service.status("a", result["id"]))["error"] == "collision"
    await h.service.resume("a", result["id"])
    await h.service.tick()
    assert (await h.service.status("a", result["id"]))["error"] == "collision"
    assert occupied.read_bytes() == audio() and not await h.store.fetchone(
        "SELECT 1 FROM collection_originals"
    )


async def test_root_lock_marker_and_symlink_fail_closed(harness, tmp_path):
    h = harness
    second = h.build()
    with pytest.raises(ImportError):
        await second.initialize()
    incoming = h.root / "_imports" / ".incoming"
    incoming.rename(incoming.with_name("saved"))
    incoming.symlink_to(incoming.with_name("saved"), target_is_directory=True)
    with pytest.raises(ImportError):
        await h.service.create("a", "cannot")
    incoming.unlink()
    incoming.with_name("saved").rename(incoming)
    await h.service.aclose()
    unrelated = await Store.open(tmp_path / "different.db")
    other = MusicImports(
        unrelated,
        h.access,
        library_id="1",
        library_path=h.root,
        limits=h.limits,
        scan=h.scan,
        native_lookup=h.lookup,
        refresh=h.collection.refresh,
    )
    try:
        with pytest.raises(ImportError):
            await other.initialize()
    finally:
        await other.aclose()
        await unrelated.close()


@pytest.mark.parametrize("bad", ["path", "size", "missing"])
async def test_native_exact_mapping_required_before_provenance(harness, bad):
    h = harness
    result = await reviewed(h)
    if bad != "missing":
        item = await h.store.fetchone(
            "SELECT final_name FROM music_import_items WHERE job_id=?", [result["id"]]
        )
        h.mapping = NativeFile(
            "wrong",
            "wrong/path.flac" if bad == "path" else "_imports/" + item[0],
            len(audio()) + (1 if bad == "size" else 0),
        )
    else:

        async def missing(*args):
            return None

        h.service.native_lookup = missing
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    assert (await h.service.status("a", result["id"]))["state"] == "paused"
    assert len(list((h.root / "_imports").rglob("*.flac"))) == 1
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")


async def test_mp3_is_preserved_and_validated(harness):
    h = harness
    data = audio("mp3")
    result = await declared(h, [ManifestEntry("mp3", "song.MP3", len(data))])
    await h.service.receive("a", result["id"], result["items"][0]["id"], 0, chunks(data))
    result = await freeze(h, "a", result["id"])
    assert result["eligible_files"] == 1
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    assert (await h.service.status("a", result["id"]))["state"] == "done"
    assert next((h.root / "_imports").rglob("*.MP3")).read_bytes() == data


async def test_checking_is_background_and_resume_preserves_exact_exclusions(harness):
    h = harness
    data = audio()
    result = await declared(h, [ManifestEntry(str(n), f"{n}.flac", len(data)) for n in range(3)])
    for item in result["items"]:
        await h.service.receive("a", result["id"], item["id"], 0, chunks(data))
    excluded = result["items"][1]["id"]
    queued = await h.service.review("a", result["id"], exclude=[excluded])
    assert queued["state"] == "checking" and queued["checked_files"] == 0
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    await h.service.tick()
    assert (await h.service.status("a", result["id"]))["checked_files"] == 1
    replay = await h.service.review("a", result["id"], exclude=[excluded])
    assert replay["checked_files"] == 1
    h.access.allowed = False
    await h.service.tick()
    h.access.allowed = True
    assert (await h.service.status("a", result["id"]))["state"] == "paused"
    await h.service.aclose()
    h.service = h.build()
    await h.service.initialize()
    await h.service.resume("a", result["id"])
    await h.service.tick()
    await h.service.tick()
    status = await h.service.status("a", result["id"])
    assert status["state"] == "review" and status["checked_files"] == 3
    assert [i["id"] for i in status["items"] if i["state"] == "excluded"] == [excluded]
    assert not list((h.root / "_imports").rglob("*.flac"))


async def test_global_slots_refuse_before_bytes_and_stop_interrupts_waiting_upload(harness):
    h = harness
    h.service.limits = replace(h.limits, max_uploads=1)
    data = audio()
    result = await declared(h, [ManifestEntry(str(n), f"{n}.flac", len(data)) for n in range(2)])
    started, release = anyio.Event(), anyio.Event()

    async def slow():
        started.set()
        await release.wait()
        yield data

    async def upload():
        with pytest.raises(ImportError):
            await h.service.receive("a", result["id"], result["items"][0]["id"], 0, slow())

    async def forbidden():
        pytest.fail("busy upload consumed bytes")
        yield data

    async with anyio.create_task_group() as group:
        group.start_soon(upload)
        await started.wait()
        with pytest.raises(ImportError) as refused:
            await h.service.receive("a", result["id"], result["items"][1]["id"], 0, forbidden())
        assert refused.value.code == "busy"
        await h.service.cancel("a", result["id"])
        release.set()
    assert not h.service._uploads
    assert not list((h.root / "_imports" / ".incoming").glob("*.part"))


async def test_restart_preserves_completed_files_but_restarts_unfinished_from_zero(harness):
    h = harness
    data = audio()
    result = await declared(h, [ManifestEntry(str(n), f"{n}.flac", len(data)) for n in range(2)])
    first, second = result["items"]
    await h.service.receive("a", result["id"], first["id"], 0, chunks(data))
    pending = await h.service._item(result["id"], second["id"])
    descriptor = h.service.storage.file(pending["staging_name"], create=True)
    os.write(descriptor, data[:10])
    os.close(descriptor)
    await h.store.execute(
        "UPDATE music_import_items SET state='uploading' WHERE id=?", [second["id"]]
    )
    await h.service.aclose()
    h.service = h.build()
    await h.service.initialize()
    status = await h.service.status("a", result["id"])
    assert status["received_files"] == 1
    pending = next(i for i in status["items"] if i["id"] == second["id"])
    assert pending["attempt"] == 1 and pending["state"] == "declared"
    await h.service.receive("a", result["id"], second["id"], 1, chunks(data))
    assert (await h.service.status("a", result["id"]))["received_files"] == 2


async def test_validation_deadline_kills_subprocess_without_publication(harness, monkeypatch):
    h = harness
    h.service.limits = replace(h.limits, validation_seconds=1)
    result = await declared(h)
    await h.service.receive("a", result["id"], result["items"][0]["id"], 0, chunks(audio()))
    launched = []
    original = anyio.open_process

    async def sleeping(*args, **kwargs):
        process = await original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(anyio, "open_process", sleeping)
    await h.service.review("a", result["id"])
    with anyio.fail_after(5):
        await h.service.tick()
    assert launched and launched[0].returncode is not None
    status = await h.service.status("a", result["id"])
    assert status["state"] == "review" and status["rejected_files"] == 1
    assert status["items"][0]["error"] == "invalid_audio"
    assert not list((h.root / "_imports").rglob("*.flac"))


async def test_invalid_review_exclusion_cannot_modify_frozen_revision(harness):
    h = harness
    result = await reviewed(h)
    with pytest.raises(ImportError):
        await h.service.review("a", result["id"], exclude=["foreign-item"])
    unchanged = await h.service.status("a", result["id"])
    assert unchanged["state"] == "review" and unchanged["revision"] == result["revision"]
    assert unchanged["eligible_files"] == 1


async def test_asset_lookup_exact_path_library_size_inode_and_no_ownership_inference(harness):
    h = harness
    result = await reviewed(h)
    item = await h.service._item(result["id"], result["items"][0]["id"])
    native = song(1, libraryId=1, path="_imports/" + item["final_name"], size=len(audio()))
    assert await h.service.asset_lookup("1", [native]) == {}  # still private
    await h.store.execute("UPDATE music_import_items SET state='placing' WHERE id=?", [item["id"]])
    h.service.storage.place(item["staging_name"], item["final_name"])
    assert await h.service.asset_lookup("1", [native]) == {"s1": item["asset_id"]}
    for changed in (
        dict(native, size=1),
        dict(native, path="_shijhon/x.flac"),
        dict(native, libraryId=2),
    ):
        assert await h.service.asset_lookup("1", [changed]) == {}
    assert await h.service.asset_lookup("2", [native]) == {}
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    # Collision file with the same size cannot become a durable import alias.
    placed = h.root / "_imports" / item["final_name"]
    placed.unlink()
    placed.write_bytes(audio())
    assert await h.service.asset_lookup("1", [native]) == {}


async def test_problem_pages_filter_before_paging_and_keep_exact_counts(harness):
    h = harness
    result = await declared(h, [ManifestEntry(str(n), f"{n}.flac", 1) for n in range(8)])
    items = result["items"]
    for n, item in enumerate(items):
        await h.store.execute(
            "UPDATE music_import_items SET state=?,warnings=? WHERE id=?",
            [
                "rejected" if n == 2 else "excluded" if n == 5 else "eligible",
                '["Missing tags"]' if n in (4, 7) else "[]",
                item["id"],
            ],
        )
    page = await h.service.status("a", result["id"], problems_only=True, limit=2)
    assert page["total_files"] == 8 and page["filtered_total"] == 4
    assert [i["id"] for i in page["items"]] == [items[2]["id"], items[4]["id"]]
    assert page["next_offset"] == 2
    last = await h.service.status("a", result["id"], problems_only=True, offset=2, limit=2)
    assert [i["id"] for i in last["items"]] == [items[5]["id"], items[7]["id"]]
    assert last["next_offset"] is None and last["filtered_total"] == 4
    default = await h.service.status("a", result["id"], limit=2)
    assert default["filtered_total"] == 8 and default["next_offset"] == 2
    assert [i["id"] for i in default["items"]] == [items[0]["id"], items[1]["id"]]
    with pytest.raises(ImportError):
        await h.service.status("b", result["id"], problems_only=True)
    with pytest.raises(ImportError):
        await h.service.status("a", result["id"], problems_only="false")


async def test_readonly_assets_survive_disabled_intake_without_any_storage_mutation(
    harness, monkeypatch
):
    h = harness
    result = await reviewed(h)
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    item = await h.service._item(result["id"], result["items"][0]["id"])
    await h.service.aclose()
    resolver = AssetResolver(h.store, library_id="1", library_path=h.root)
    root = h.root / "_imports"
    before = {path.name: (path.stat().st_mtime_ns, path.stat().st_mode) for path in root.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("read-only asset resolution attempted a storage mutation")

    monkeypatch.setattr(os, "mkdir", forbidden)
    monkeypatch.setattr(os, "fchmod", forbidden)
    import fcntl

    monkeypatch.setattr(fcntl, "flock", forbidden)
    opened = os.open

    def readonly_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_TRUNC)
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", readonly_open)
    # A changed native ID can recover the same durable asset alias after restore.
    native = song(
        999, libraryId=1, path="_imports/" + item["final_name"], size=item["expected_size"]
    )
    assert await resolver("1", [native]) == {"s999": item["asset_id"]}
    assert before == {
        path.name: (path.stat().st_mtime_ns, path.stat().st_mode) for path in root.iterdir()
    }
    assert (await h.store.fetchone("SELECT count(*) FROM collection_originals"))[0] == 1
    assert await resolver("2", [native]) == {}


async def test_disabled_no_imports_needs_no_root_but_existing_receipts_fail_closed(harness):
    h = harness
    resolver = AssetResolver(h.store, library_id="1", library_path=h.root)
    await h.service.aclose()
    root = h.root / "_imports"
    root.rename(h.root / "temporarily-away")
    assert await resolver("1", []) == {}  # no existing retained assets
    (h.root / "temporarily-away").rename(root)
    h.service = h.build()
    await h.service.initialize()
    result = await reviewed(h)
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    await h.service.aclose()
    root.rename(h.root / "temporarily-away")
    with pytest.raises(ImportError) as refused:
        await resolver("1", [])  # fail even on a native page without import paths
    assert refused.value.code == "storage"
    (h.root / "temporarily-away").rename(root)
    marker = root / ".import-root"
    marker.write_text("{}")
    with pytest.raises(ImportError):
        await resolver("1", [])


async def test_native_identity_reuse_cannot_rebind_existing_owner_during_any_refresh(harness):
    h = harness
    result = await reviewed(h)
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    receipt = (await h.service.status("a", result["id"]))["items"][0]
    # Another caller refreshing after a bad native scan must also refuse the reused
    # ID before its existing owner's native alias can attach to a different path.
    resolver = AssetResolver(h.store, library_id="1", library_path=h.root)
    with pytest.raises(ImportError) as reused:
        await resolver(
            "1",
            [
                song(
                    1,
                    id=receipt["song_id"],
                    libraryId=1,
                    path="Unknown/another.flac",
                    size=len(audio()),
                )
            ],
        )
    assert reused.value.code == "identity"
    assert (await h.store.fetchone("SELECT owner_id FROM collection_originals"))[0] == "a"


async def test_large_asset_page_bounds_query_work_and_preserves_restored_identity(harness):
    h = harness
    result = await reviewed(h)
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    receipt = await h.service._item(result["id"], result["items"][0]["id"])
    async with h.store.transaction() as conn:
        await conn.executemany(
            "INSERT INTO music_import_items"
            " (id,asset_id,job_id,client_key,display_path,expected_size,reserved_size,"
            "state,staging_name,final_name,song_id) VALUES (?,?,?,?,?,?,0,'complete',?,?,?)",
            [
                (
                    f"receipt-{n}",
                    f"asset-{n}",
                    result["id"],
                    f"key-{n}",
                    f"Song {n}.flac",
                    receipt["expected_size"],
                    f"{n:032x}.part",
                    f"{n:032x}.flac",
                    f"native-{n}" if n % 2 else "",  # Restore can precede the song receipt.
                )
                for n in range(6000)
            ],
        )
        await conn.executemany(
            "INSERT INTO collection_track_aliases(library_id,alias,track_key) VALUES ('1',?,?)",
            [
                (alias, f"track-{n}")
                for n in range(6000)
                for alias in (f"native:native-{n}", f"import:asset-{n}")
            ],
        )
    data = (h.root / "_imports/.incoming" / receipt["staging_name"]).read_bytes()
    page = []
    for n in range(5500, 5999):
        staged = h.root / f"_imports/.incoming/{n:032x}.part"
        staged.write_bytes(data)
        os.link(staged, h.root / f"_imports/{n:032x}.flac")
        page.append(
            song(
                n,
                id=f"native-{n}",
                libraryId=1,
                path=f"_imports/{n:032x}.flac",
                size=receipt["expected_size"],
            )
        )
    page.append(song(7000, libraryId=1, path="Other/unrelated.flac"))
    resolver = AssetResolver(h.store, library_id="1", library_path=h.root)
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 1000
        return instructions > 40_000_000

    await h.store.conn.set_progress_handler(progress, 1000)
    try:
        assert await resolver("1", page) == {f"native-{n}": f"asset-{n}" for n in range(5500, 5999)}
    finally:
        await h.store.conn.set_progress_handler(None, 0)
    # A second asset claiming a page identity must still fail closed, including
    # when that identity exists only in aliases learned during restore.
    await h.store.execute(
        "UPDATE collection_track_aliases SET track_key='track-5500'"
        " WHERE library_id='1' AND alias='import:asset-0'"
    )
    with pytest.raises(ImportError) as ambiguous:
        await resolver("1", page)
    assert ambiguous.value.code == "identity"


async def test_malformed_manifest_unicode_digest_and_overflow_are_safe_refusals(harness):
    h = harness
    job = await h.service.create("a", "job")
    for entry in (
        ManifestEntry("file", "bad\ud800.flac", 1),
        ManifestEntry("file", "song.flac", 1, 123),
        {"client_key": "file", "path": "song.flac", "size": 1},
    ):
        with pytest.raises(ImportError):
            await h.service.declare("a", job["id"], "page", [entry])
    with pytest.raises(ValueError):
        replace(ImportLimits(), max_file_bytes=2**100)


async def test_stop_after_manifest_capacity_cannot_insert_or_retain_late_reservations(
    harness, monkeypatch
):
    h = harness
    h.service.limits = replace(h.limits, max_owner_bytes=10, max_global_bytes=10)
    job = await h.service.create("a", "stopped")
    capacity = h.service._capacity

    async def stop_after_capacity(owner, added=0):
        await capacity(owner, added)
        stopped = await h.service.cancel(owner, job["id"])
        assert stopped["state"] == "cancelled" and stopped["total_files"] == 0

    monkeypatch.setattr(h.service, "_capacity", stop_after_capacity)
    with pytest.raises(ImportError) as refused:
        await h.service.declare("a", job["id"], "page", [ManifestEntry("file", "1.flac", 10)])
    assert refused.value.code == "sealed"
    assert not await h.store.fetchone(
        "SELECT 1 FROM music_import_pages WHERE job_id=?", [job["id"]]
    )
    assert not await h.store.fetchone(
        "SELECT 1 FROM music_import_items WHERE job_id=?", [job["id"]]
    )
    monkeypatch.setattr(h.service, "_capacity", capacity)
    next_job = await h.service.create("a", "next")
    entries = [ManifestEntry("next", "2.flac", 10)]
    await h.service.declare("a", next_job["id"], "page", entries)
    assert (await h.store.fetchone("SELECT sum(reserved_size) FROM music_import_items"))[0] == 10
    # A committed declaration can still replay after Stop without reacquiring quota.
    await h.service.cancel("a", next_job["id"])
    replay = await h.service.declare("a", next_job["id"], "page", entries)
    assert replay["state"] == "cancelled" and replay["total_files"] == 1
    assert (await h.store.fetchone("SELECT sum(reserved_size) FROM music_import_items"))[0] == 0
    third = await h.service.create("b", "third")
    assert (await h.service.declare("b", third["id"], "page", entries))["total_files"] == 1


async def test_batch_shares_one_scan_and_full_refresh_and_keeps_exact_file_proofs(harness):
    h = harness
    h.existing = [song(n) for n in range(1000)]
    approved = await approved_batch(h)

    async def before_refresh():
        assert h.lookups == 3 and h.scans == 1
        assert not await h.store.fetchone("SELECT 1 FROM collection_originals")

    h.before_refresh = before_refresh
    assert await h.service.tick()
    done = await h.service.status("a", approved["id"])
    assert done["state"] == "done" and done["complete_files"] == 3
    assert (h.scans, h.refreshes, h.lookups) == (1, 1, 6)
    assert (h.access.source_pages, h.access.source_rows) == (3, 1003)
    assert [kind for kind, _ in h.events] == [
        "scan",
        "lookup",
        "lookup",
        "lookup",
        "refresh",
        "lookup",
        "lookup",
        "lookup",
    ]
    assert h.events[1:4] == h.events[5:8]  # exact evidence repeated for every file
    assert (await h.store.fetchone("SELECT count(*) FROM collection_originals"))[0] == 3
    assert len({i["song_id"] for i in done["items"]}) == 3
    assert all(path.read_bytes() == audio() for path in (h.root / "_imports").rglob("*.flac"))
    assert not await h.service.tick()


@pytest.mark.parametrize("bound", ["files", "bytes", "one_oversized_file"])
async def test_bounded_phases_resume_without_starving_large_files(harness, monkeypatch, bound):
    h = harness
    if bound == "files":
        monkeypatch.setattr(import_service, "PUBLISH_BATCH_FILES", 2)
    else:
        monkeypatch.setattr(
            import_service,
            "PUBLISH_BATCH_BYTES",
            len(audio()) * 2 if bound == "bytes" else len(audio()) - 1,
        )
    approved = await approved_batch(h, count=5)
    expected = [2, 4, 5] if bound != "one_oversized_file" else [1, 2, 3, 4, 5]
    for phase, complete in enumerate(expected, 1):
        assert await h.service.tick()
        status = await h.service.status("a", approved["id"])
        assert status["complete_files"] == complete
        assert status["state"] == ("done" if complete == 5 else "publishing")
        assert (h.scans, h.refreshes, h.lookups) == (phase, phase, 2 * complete)
    assert len(list((h.root / "_imports").rglob("*.flac"))) == 5
    assert (await h.store.fetchone("SELECT count(*) FROM collection_originals"))[0] == 5


async def test_default_batch_selection_caps_files_bytes_and_recovers_placed_first(harness):
    h = harness
    job = await h.service.create("a", "bounds")
    entries = [ManifestEntry(str(n), f"{n}.flac", 1) for n in range(501)]
    await h.service.declare("a", job["id"], "first", entries[:500])
    await h.service.declare("a", job["id"], "last", entries[500:])
    # Inspect scheduling bounds without allocating large files or invoking codec checks.
    await h.store.execute(
        "UPDATE music_import_items SET state='eligible' WHERE job_id=?", [job["id"]]
    )
    batch = await h.service._batch(job["id"])
    assert len(batch) == 500
    assert [i["id"] for i in batch] == sorted(i["id"] for i in batch)
    pending = (await h.service.status("a", job["id"], offset=500))["items"][0]
    await h.store.execute(
        "UPDATE music_import_items SET state='placed' WHERE id=?", [pending["id"]]
    )
    assert (await h.service._batch(job["id"]))[0]["id"] == pending["id"]
    await h.store.execute(
        "UPDATE music_import_items SET expected_size=? WHERE job_id=?", [512 * 1024**2, job["id"]]
    )
    batch = await h.service._batch(job["id"])
    assert len(batch) == 2 and sum(i["expected_size"] for i in batch) == 1024**3
    await h.store.execute(
        "UPDATE music_import_items SET expected_size=? WHERE id=?", [2 * 1024**3, pending["id"]]
    )
    batch = await h.service._batch(job["id"])
    assert len(batch) == 1 and batch[0]["id"] == pending["id"]
    assert batch[0]["expected_size"] == 2 * 1024**3


@pytest.mark.parametrize("boundary", ["placement", "scan", "evidence", "refresh", "receipt"])
async def test_stop_during_batch_retains_placed_files_and_atomic_completed_receipts(
    harness, monkeypatch, boundary
):
    h = harness
    approved = await approved_batch(h)

    async def stop():
        await h.service.cancel("a", approved["id"])

    if boundary == "placement":
        verify = h.service._verify

        async def verified(item, *, final=False):
            await verify(item, final=final)
            if final:
                await stop()

        monkeypatch.setattr(h.service, "_verify", verified)
    elif boundary == "scan":
        h.after_scan = stop
    elif boundary == "refresh":
        h.after_refresh = stop
    elif boundary == "evidence":
        lookup = h.service.native_lookup

        async def stopped_lookup(*args):
            native = await lookup(*args)
            await stop()
            return native

        monkeypatch.setattr(h.service, "native_lookup", stopped_lookup)
    else:
        credit = h.service._credit

        async def credited(*args):
            await credit(*args)
            await stop()

        monkeypatch.setattr(h.service, "_credit", credited)
    assert await h.service.tick()
    stopped = await h.service.status("a", approved["id"])
    assert stopped["state"] == "cancelled"
    completed = 1 if boundary == "receipt" else 0
    assert stopped["complete_files"] == completed
    assert (await h.store.fetchone("SELECT count(*) FROM collection_originals"))[0] == completed
    paths = list((h.root / "_imports").rglob("*.flac"))
    assert len(paths) == (1 if boundary == "placement" else 3)
    assert all(p.read_bytes() == audio() for p in paths)
    assert (await h.service.resume("a", approved["id"]))["state"] == "cancelled"
    assert not await h.service.tick()


@pytest.mark.parametrize("boundary", ["placement", "scan", "refresh", "receipt"])
async def test_cancelled_task_restarts_remaining_batch_without_duplicate_credit(
    harness, monkeypatch, boundary
):
    h = harness
    approved = await approved_batch(h)

    async def interrupt():
        raise anyio.get_cancelled_exc_class()()

    if boundary == "placement":
        place = h.service.storage.place

        def placed(*args):
            place(*args)
            raise anyio.get_cancelled_exc_class()()

        monkeypatch.setattr(h.service.storage, "place", placed)
    elif boundary == "scan":
        h.after_scan = interrupt
    elif boundary == "refresh":
        h.after_refresh = interrupt
    else:
        credit = h.service._credit

        async def credited(*args):
            await credit(*args)
            await interrupt()

        monkeypatch.setattr(h.service, "_credit", credited)
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await h.service.tick()
    paused = await h.service.status("a", approved["id"])
    assert paused["state"] == "paused" and paused["error"] == "interrupted"
    assert paused["complete_files"] == (1 if boundary == "receipt" else 0)
    await h.service.aclose()
    h.after_scan = h.after_refresh = None
    h.service = h.build()
    await h.service.initialize()
    await h.service.resume("a", approved["id"])
    assert await h.service.tick()
    done = await h.service.status("a", approved["id"])
    assert done["state"] == "done" and done["complete_files"] == 3
    assert len({i["song_id"] for i in done["items"]}) == 3
    assert len(list((h.root / "_imports").rglob("*.flac"))) == 3
    assert all(p.read_bytes() == audio() for p in (h.root / "_imports").rglob("*.flac"))
    assert (await h.store.fetchone("SELECT count(*) FROM collection_originals"))[0] == 3
    assert h.scans == (1 if boundary == "placement" else 2)
    assert h.refreshes == (2 if boundary in ("refresh", "receipt") else 1)
    assert not await h.service.tick()


@pytest.mark.parametrize("change", ["grant", "revision", "configuration", "root"])
async def test_batch_rechecks_access_revision_configuration_and_root_after_refresh(harness, change):
    h = harness
    approved = await approved_batch(h)
    root = h.root / "_imports"

    async def changed():
        if change == "grant":
            h.access.allowed = False
        elif change == "revision":
            await h.store.execute(
                "UPDATE music_import_jobs SET revision=revision+1 WHERE id=?", [approved["id"]]
            )
        elif change == "configuration":
            h.service.config_id = "changed"
        else:
            root.rename(h.root / "moved-imports")

    h.after_refresh = changed
    assert await h.service.tick()
    h.access.allowed = True
    if change == "root":
        (h.root / "moved-imports").rename(root)
    paused = await h.service.status("a", approved["id"])
    assert paused["state"] == "paused" and paused["complete_files"] == 0
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    assert len(list(root.rglob("*.flac"))) == 3


@pytest.mark.parametrize(
    "failure", ["duplicate_before_refresh", "changed_after_refresh", "bytes_after_refresh"]
)
async def test_batch_never_credits_ambiguous_changed_native_or_modified_bytes(
    harness, monkeypatch, failure
):
    h = harness
    approved = await approved_batch(h)
    lookup = h.service.native_lookup
    if failure != "bytes_after_refresh":

        async def altered(*args):
            native = await lookup(*args)
            return replace(
                native,
                song_id="duplicate"
                if failure == "duplicate_before_refresh"
                else native.song_id + ("changed" if h.refreshes else ""),
            )

        monkeypatch.setattr(h.service, "native_lookup", altered)
    else:

        async def changed_bytes():
            item = await h.service._item(approved["id"], approved["items"][0]["id"])
            path = h.root / "_imports" / item["final_name"]
            path.write_bytes(b"x" * item["expected_size"])

        h.after_refresh = changed_bytes
    assert await h.service.tick()
    status = await h.service.status("a", approved["id"])
    assert status["state"] == "paused" and status["complete_files"] == 0
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")
    assert h.refreshes == (0 if failure == "duplicate_before_refresh" else 1)
    assert len(list((h.root / "_imports").rglob("*.flac"))) == 3


async def test_missing_title_warning_explains_preserved_music_app_filename(harness):
    result = await reviewed(harness)
    warnings = result["items"][0]["warnings"]
    assert any("Missing title tag" in w and "preserved filename" in w for w in warnings)
    assert any("Add tags before importing" in w for w in warnings)


async def test_existing_title_with_other_missing_tags_does_not_claim_filename_fallback(harness):
    path = harness.root / "title-only.flac"
    path.write_bytes(audio())
    tags = FLAC(path)
    tags["title"] = ["First Light"]
    tags.save()
    result = await reviewed(harness, data=path.read_bytes())
    warnings = result["items"][0]["warnings"]
    assert any("Missing artist or album tags" in warning for warning in warnings)
    assert not any("preserved filename" in warning for warning in warnings)


async def test_stop_wins_after_final_upload_read_without_retained_bytes_or_lost_quota(
    harness, monkeypatch
):
    h = harness
    h.service.limits = replace(h.limits, max_owner_bytes=4, max_global_bytes=4)
    job = await declared(h, [ManifestEntry("file", "one.flac", 4)])
    item = await h.service._item(job["id"], job["items"][0]["id"])
    staged = h.root / "_imports" / ".incoming" / item["staging_name"]
    original_job = h.service._job
    stopped = False

    async def stop_after_final_read(*args, **kwargs):
        nonlocal stopped
        current = await original_job(*args, **kwargs)
        if not stopped and item["id"] in h.service._uploads and staged.stat().st_size == 4:
            stopped = True
            assert (await h.service.cancel("a", job["id"]))["state"] == "cancelled"
            live = await h.store.fetchone(
                "SELECT * FROM music_import_items WHERE id=?", [item["id"]]
            )
            assert live["reserved_size"] == 4 and live["received_size"] == 0
        return current  # return the pre-Stop uploading snapshot, exactly at R2's boundary

    monkeypatch.setattr(h.service, "_job", stop_after_final_read)
    with pytest.raises(ImportError) as refused:
        await h.service.receive("a", job["id"], item["id"], 0, chunks(b"abcd"))
    assert refused.value.code == "stopped" and stopped
    live = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert (live["state"], live["reserved_size"], live["received_size"], live["attempt"]) == (
        "declared",
        0,
        0,
        1,
    )
    assert not staged.exists() and not list(staged.parent.glob("*.part"))
    next_job = await h.service.create("a", "next")
    await h.service.declare("a", next_job["id"], "page", [ManifestEntry("next", "two.flac", 4)])
    assert (await h.store.fetchone("SELECT sum(reserved_size) FROM music_import_items"))[0] == 4


async def test_received_commit_wins_while_stop_waits_and_retained_bytes_stay_reserved(
    harness, monkeypatch
):
    h = harness
    h.service.limits = replace(h.limits, max_owner_bytes=4, max_global_bytes=4)
    job = await declared(h, [ManifestEntry("file", "one.flac", 4)])
    item = await h.service._item(job["id"], job["items"][0]["id"])
    written, commit, stop_started, stop_done = (anyio.Event() for _ in range(4))
    original = h.store.transaction
    paused = False

    @asynccontextmanager
    async def pause_before_received_commit():
        nonlocal paused
        async with original() as conn:
            yield conn
            cursor = await conn.execute(
                "SELECT state FROM music_import_items WHERE id=?", [item["id"]]
            )
            row = await cursor.fetchone()
            if row["state"] == "received" and not paused:
                paused = True
                written.set()
                await commit.wait()

    monkeypatch.setattr(h.store, "transaction", pause_before_received_commit)

    async def receive():
        await h.service.receive("a", job["id"], item["id"], 0, chunks(b"abcd"))

    async def stop():
        stop_started.set()
        await h.service.cancel("a", job["id"])
        stop_done.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(receive)
            await written.wait()
            group.start_soon(stop)
            await stop_started.wait()
            await anyio.lowlevel.checkpoint()
            assert not stop_done.is_set()
            commit.set()
    assert stop_done.is_set() and paused
    live = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert (live["state"], live["reserved_size"], live["received_size"]) == ("received", 4, 4)
    assert live["digest"] == hashlib.sha256(b"abcd").hexdigest()
    staged = h.root / "_imports" / ".incoming" / item["staging_name"]
    assert staged.read_bytes() == b"abcd"
    assert (await h.service.status("a", job["id"]))["state"] == "cancelled"
    next_job = await h.service.create("b", "next")
    with pytest.raises(ImportError) as full:
        await h.service.declare("b", next_job["id"], "page", [ManifestEntry("next", "two.flac", 4)])
    assert full.value.code == "bytes_limit"


async def test_late_task_cancellation_cannot_discard_a_committed_received_file(
    harness, monkeypatch
):
    h = harness
    job = await declared(h, [ManifestEntry("file", "one.flac", 4)])
    item = await h.service._item(job["id"], job["items"][0]["id"])
    original = h.store.transaction
    interrupted = False

    @asynccontextmanager
    async def interrupt_after_commit():
        nonlocal interrupted
        async with original() as conn:
            yield conn
        row = await h.store.fetchone(
            "SELECT state FROM music_import_items WHERE id=?", [item["id"]]
        )
        if row["state"] == "received" and not interrupted:
            interrupted = True
            raise anyio.get_cancelled_exc_class()()

    monkeypatch.setattr(h.store, "transaction", interrupt_after_commit)
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await h.service.receive("a", job["id"], item["id"], 0, chunks(b"abcd"))
    live = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert (live["state"], live["reserved_size"], live["received_size"]) == ("received", 4, 4)
    assert (h.root / "_imports" / ".incoming" / item["staging_name"]).read_bytes() == b"abcd"


@pytest.mark.parametrize("boundary", ["before_discard", "after_discard"])
async def test_stop_partial_upload_restart_keeps_capacity_until_private_discard_is_durable(
    harness, monkeypatch, boundary
):
    h = harness
    h.service.limits = replace(h.limits, max_owner_bytes=4, max_global_bytes=4)
    job = await declared(h, [ManifestEntry("file", "one.flac", 4)])
    item = await h.service._item(job["id"], job["items"][0]["id"])
    staged = h.root / "_imports" / ".incoming" / item["staging_name"]
    partial = anyio.Event()

    async def stream():
        yield b"ab"
        partial.set()
        await anyio.sleep_forever()

    async def receive():
        await h.service.receive("a", job["id"], item["id"], 0, stream())

    async with anyio.create_task_group() as group:
        group.start_soon(receive)
        await partial.wait()
        assert staged.read_bytes() == b"ab"
        await h.service.cancel("a", job["id"])
        live = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
        assert live["reserved_size"] == 4 and live["state"] == "uploading"
        next_job = await h.service.create("a", "next")
        with pytest.raises(ImportError) as full:
            await h.service.declare(
                "a", next_job["id"], "page", [ManifestEntry("next", "two.flac", 4)]
            )
        assert full.value.code == "bytes_limit"
        if boundary == "before_discard":

            async def interrupted(*args):
                raise anyio.get_cancelled_exc_class()()

            monkeypatch.setattr(h.service, "_discard_upload", interrupted)
        else:
            discard = h.service.storage.discard_attempt

            def interrupted(*args):
                discard(*args)
                raise anyio.get_cancelled_exc_class()()

            monkeypatch.setattr(h.service.storage, "discard_attempt", interrupted)
        group.cancel_scope.cancel()
    live = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert (live["state"], live["reserved_size"], live["received_size"]) == ("uploading", 4, 0)
    assert staged.exists() == (boundary == "before_discard")
    await h.service.aclose()
    h.service = h.build()
    await h.service.initialize()
    live = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert (live["state"], live["reserved_size"], live["received_size"]) == ("declared", 0, 0)
    assert not staged.exists() and not list(staged.parent.glob("*.part"))
    await h.service.declare("a", next_job["id"], "page", [ManifestEntry("next", "two.flac", 4)])
    assert (await h.store.fetchone("SELECT sum(reserved_size) FROM music_import_items"))[0] == 4


@pytest.mark.parametrize("field", ["attempt", "state"])
async def test_received_commit_requires_the_admitted_live_item_and_attempt(
    harness, monkeypatch, field
):
    h = harness
    job = await declared(h, [ManifestEntry("file", "one.flac", 4)])
    item = await h.service._item(job["id"], job["items"][0]["id"])
    staged = h.root / "_imports" / ".incoming" / item["staging_name"]
    original_job = h.service._job
    changed = False

    async def change_after_final_read(*args, **kwargs):
        nonlocal changed
        current = await original_job(*args, **kwargs)
        if not changed and item["id"] in h.service._uploads and staged.stat().st_size == 4:
            changed = True
            sql = (
                "UPDATE music_import_items SET attempt=attempt+1 WHERE id=?"
                if field == "attempt"
                else "UPDATE music_import_items SET state='declared' WHERE id=?"
            )
            await h.store.execute(sql, [item["id"]])
        return current

    monkeypatch.setattr(h.service, "_job", change_after_final_read)
    with pytest.raises(ImportError) as refused:
        await h.service.receive("a", job["id"], item["id"], 0, chunks(b"abcd"))
    assert refused.value.code == "attempt" and changed
    live = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert live["state"] != "received" and live["received_size"] == 0 and live["digest"] == ""
    assert live["reserved_size"] == 4  # never delete or release another admitted attempt


@pytest.mark.parametrize("stage", ["complete", "placed", "placing"])
@pytest.mark.parametrize("reuse", ["path", "size", "inode"])
async def test_restored_native_alias_cannot_rebind_import_with_intake_stopped(
    harness, monkeypatch, stage, reuse
):
    h = harness
    result = await reviewed(h)
    await h.service.approve("a", result["id"], result["revision"], "approve")

    async def native_first():
        h.access.rows = [dict(row, id="native-first", libraryId=1) for row in h.access.rows]

    h.after_scan = native_first
    if stage == "placing":
        place = h.service.storage.place

        def interrupted_place(*args):
            place(*args)
            raise OSError("interrupted after visibility, before placed state")

        monkeypatch.setattr(h.service.storage, "place", interrupted_place)
    elif stage == "placed":

        async def interrupted_refresh():
            raise OSError("interrupted before receipt")

        h.after_refresh = interrupted_refresh
    await h.service.tick()
    item = await h.service._item(result["id"], result["items"][0]["id"])
    assert item["state"] == stage
    if stage != "complete":
        assert item["song_id"] == item["track_key"] == ""
    # A native watcher can see an original while its durable intent is still placing.
    if stage == "placing":
        await h.scan(["_imports"])
    await h.service.aclose()
    resolver = AssetResolver(h.store, library_id="1", library_path=h.root)
    h.collection.index.asset_lookup = resolver
    await h.collection.refresh("b", "1")
    initial = dict(h.access.rows[0])
    restored = dict(initial, id="native-restored")
    h.access.rows = [restored]
    await h.collection.refresh("b", "1")
    alias = await h.store.fetchone(
        "SELECT track_key FROM collection_track_aliases WHERE library_id='1' AND alias=?",
        ["native:native-restored"],
    )
    asset_alias = await h.store.fetchone(
        "SELECT track_key FROM collection_track_aliases WHERE library_id='1' AND alias=?",
        ["import:" + item["asset_id"]],
    )
    assert alias["track_key"] == asset_alias["track_key"] == "native:native-first"
    old_generation = (await h.collection.index_state("b", "1")).generation
    previous_rows = [dict(row) for row in await h.store.fetchall("SELECT * FROM collection_tracks")]
    previous_credit = [
        tuple(row) for row in await h.store.fetchall("SELECT * FROM collection_originals")
    ]
    expected = {"native-restored"} if stage == "complete" else set()
    assert (
        await h.collection.visible_song_ids("a", "1", ["native-restored"], profile_id="original")
        == expected
    )
    assert (
        await h.collection.visible_song_ids("b", "1", ["native-restored"], profile_id="original")
        == set()
    )
    wrong = dict(restored, title="Different File")
    if reuse == "path":
        wrong["path"] = "Other/different.flac"  # same size, unrelated native path
    elif reuse == "size":
        wrong["size"] = 4
    else:
        final = h.root / "_imports" / item["final_name"]
        final.unlink()
        final.write_bytes(b"x" * item["expected_size"])
    h.access.rows = [wrong]
    with pytest.raises(ImportError) as refused:
        await h.collection.refresh("b", "1")
    assert refused.value.code == ("storage" if reuse == "inode" else "identity")
    failed = await h.collection.index_state("b", "1")
    assert failed.state == "failed" and failed.generation == old_generation
    assert [
        dict(row) for row in await h.store.fetchall("SELECT * FROM collection_tracks")
    ] == previous_rows
    assert [
        tuple(row) for row in await h.store.fetchall("SELECT * FROM collection_originals")
    ] == previous_credit
    assert (
        await h.collection.visible_song_ids("a", "1", ["native-restored"], profile_id="original")
        == expected
    )
    assert (
        await h.service._item(result["id"], item["id"]) == item
    )  # immutable completed receipt too
    assert (h.root / "_imports" / ".incoming" / item["staging_name"]).read_bytes() == audio()


async def test_native_aliases_without_import_mapping_keep_native_metadata_authority(harness):
    h = harness
    result = await reviewed(h)
    await h.service.approve("a", result["id"], result["revision"], "approve")
    await h.service.tick()
    imported = dict(h.access.rows[0], libraryId=1)
    await h.service.aclose()
    h.collection.index.asset_lookup = AssetResolver(h.store, library_id="1", library_path=h.root)
    unrelated = song(999, libraryId=1, albumId="other", path="Other/first.flac")
    h.access.rows = [imported, unrelated]
    await h.collection.refresh("b", "1")
    h.access.rows = [
        imported,
        dict(unrelated, path="Other/second.flac", title="Other Title", size=123),
    ]
    await h.collection.refresh("b", "1")
    track = await h.store.fetchone("SELECT title,size FROM collection_tracks WHERE song_id='s999'")
    assert (track["title"], track["size"]) == ("Other Title", 123)
    assert await h.collection.visible_song_ids(
        "a", "1", [imported["id"], "s999"], profile_id="original"
    ) == {imported["id"]}
    assert (
        await h.collection.visible_song_ids(
            "b", "1", [imported["id"], "s999"], profile_id="original"
        )
        == set()
    )


async def test_actual_receive_crash_restart_then_stop_does_not_orphan_partial_bytes(tmp_path):
    root = tmp_path.resolve()
    child = textwrap.dedent("""
        import json
        import os
        import sys
        from dataclasses import replace
        from pathlib import Path

        import anyio

        from shijhon.collection.origins import Origins
        from shijhon.music_imports import ImportLimits, ManifestEntry
        from shijhon.store import Store
        from tests.unit.test_music_imports import Harness

        async def run():
            root = Path(sys.argv[1])
            (root / "_imports").mkdir()
            store = await Store.open(root / "state.db")
            await Origins(store).initialize_personal("1")
            limits = replace(ImportLimits(), min_free_bytes=0,
                             max_owner_bytes=4, max_global_bytes=4)
            harness = Harness(root, store, limits=limits)
            service = harness.service
            await service.initialize()
            job = await service.create("a", "interrupted")
            job = await service.declare("a", job["id"], "page",
                                        [ManifestEntry("file", "one.flac", 4)])
            await service.seal("a", job["id"])
            item = await service._item(job["id"], job["items"][0]["id"])
            print(json.dumps({"job": job["id"], "item": item["id"],
                              "staging": item["staging_name"]}), flush=True)

            async def stream():
                yield b"ab"
                fd = service.storage.file(item["staging_name"])
                os.fsync(fd)
                os.close(fd)
                os.fsync(service.storage.incoming)
                os._exit(73)  # actual process death: no finally/shielded cleanup

            await service.receive("a", job["id"], item["id"], 0, stream())
            raise AssertionError("crash checkpoint was not reached")

        anyio.run(run)
    """)
    with anyio.fail_after(10):
        crashed = await anyio.run_process(
            [sys.executable, "-B", "-c", child, str(root)], check=False
        )
    assert crashed.returncode == 73, crashed.stderr.decode()
    ids = json.loads(crashed.stdout)
    staged = root / "_imports" / ".incoming" / ids["staging"]
    assert staged.read_bytes() == b"ab"
    store = await Store.open(root / "state.db")
    limits = replace(ImportLimits(), min_free_bytes=0, max_owner_bytes=4, max_global_bytes=4)
    h = Harness(root, store, limits=limits)
    try:
        # Inspect the actual crash's durable state, before any recovery or mutation.
        before = await store.fetchone("SELECT * FROM music_import_items WHERE id=?", [ids["item"]])
        assert (
            before["state"],
            before["attempt"],
            before["reserved_size"],
            before["received_size"],
        ) == ("uploading", 0, 4, 0)
        await h.service.initialize()
        recovered = await store.fetchone(
            "SELECT * FROM music_import_items WHERE id=?", [ids["item"]]
        )
        assert (recovered["state"], recovered["attempt"], recovered["reserved_size"]) == (
            "declared",
            1,
            4,
        )
        assert not staged.exists()
        stopped = await h.service.cancel("a", ids["job"])
        assert stopped["state"] == "cancelled" and stopped["received_files"] == 0
        assert (
            await store.fetchone(
                "SELECT reserved_size FROM music_import_items WHERE id=?", [ids["item"]]
            )
        )[0] == 0
        assert not list(staged.parent.glob("*.part"))
        next_job = await h.service.create("a", "next")
        await h.service.declare("a", next_job["id"], "page", [ManifestEntry("next", "two.flac", 4)])
        assert (await store.fetchone("SELECT sum(reserved_size) FROM music_import_items"))[0] == 4
        await h.service.aclose()
        h.service = h.build()
        await h.service.initialize()
        after = await store.fetchone("SELECT * FROM music_import_items WHERE id=?", [ids["item"]])
        assert (
            after["state"],
            after["attempt"],
            after["reserved_size"],
            after["received_size"],
        ) == ("declared", 1, 0, 0)
        assert (await h.service.status("a", next_job["id"]))["total_bytes"] == 4
        assert not list(staged.parent.glob("*.part"))
        assert (await store.fetchone("SELECT sum(reserved_size) FROM music_import_items"))[0] == 4
    finally:
        await h.service.aclose()
        await store.close()
