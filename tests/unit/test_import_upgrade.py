"""Upgrade journal, collisions, retained originals and storage accounting."""

import hashlib
import json
import os
from dataclasses import replace

import pytest

from shijhon.music_imports import ImportError, NativeFile
from shijhon.music_imports.upgrade import Upgrades
from shijhon.quality import Quality
from tests.unit.test_music_imports import reviewed

pytest_plugins = ("tests.unit.test_music_imports",)
pytestmark = pytest.mark.anyio


async def complete(h):
    job = await reviewed(h)
    await h.service.approve("a", job["id"], job["revision"], "approve")
    await h.service.tick()
    return dict(await h.store.fetchone("SELECT * FROM music_import_items"))


def carry(h, monkeypatch):
    async def made(item, fetched, name):
        data = fetched.read_bytes()
        fd = h.service.storage.file(name, create=True)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    async def native(owner, library, path):
        row = await h.store.fetchone("SELECT song_id FROM music_import_items")
        file = h.root / path
        return NativeFile(row["song_id"], path, file.stat().st_size) if file.exists() else None

    monkeypatch.setattr(h.service.upgrades, "_carry", made)
    monkeypatch.setattr(h.service, "native_lookup", native)


@pytest.mark.parametrize("collision", ["before_intent", "after_intent", "recovery"])
async def test_occupied_upgrade_path_is_never_removed_and_releases_intent(
    harness, monkeypatch, tmp_path, collision
):
    h = harness
    before = await complete(h)
    carry(h, monkeypatch)
    fetched = tmp_path / "new.mp3"
    fetched.write_bytes(b"invented upgraded audio")
    destination = (h.root / "_imports" / before["final_name"]).with_suffix(".mp3")
    if collision == "before_intent":
        destination.write_bytes(b"another file")
    else:
        actor = h.service._actor

        async def raced(owner):
            await actor(owner)
            destination.write_bytes(b"another file")

        monkeypatch.setattr(h.service, "_actor", raced)
    if collision == "recovery":
        rollback = Upgrades._roll_back

        async def interrupted(*args):
            raise OSError("stopped")

        monkeypatch.setattr(Upgrades, "_roll_back", interrupted)
    why = await h.service.upgrades.upgrade("a", before["song_id"], fetched, Quality(format="mp3"))
    assert why and destination.read_bytes() == b"another file"
    if collision == "recovery":
        monkeypatch.setattr(Upgrades, "_roll_back", rollback)
        assert await h.service.upgrades.recover() == 1
    row = await h.store.fetchone("SELECT * FROM music_import_items")
    assert row["upgrade"] == "" and row["reserved_size"] == before["reserved_size"]
    assert (h.root / "_imports" / row["final_name"]).is_file()
    assert {p.name for p in (h.root / "_imports" / ".incoming").iterdir()} == {
        before["staging_name"],
        ".ndignore",
    }


async def test_access_lost_before_swap_needs_no_native_rollback_verification(
    harness, monkeypatch, tmp_path
):
    h = harness
    before = await complete(h)
    carry(h, monkeypatch)
    scans = h.scans
    fetched = tmp_path / "new.mp3"
    fetched.write_bytes(b"invented upgraded audio")

    async def revoked(owner):
        raise ImportError("access", "Access revoked")

    async def unavailable(*args):
        pytest.fail("An untouched original must not need the revoked owner's native read")

    monkeypatch.setattr(h.service, "_actor", revoked)
    monkeypatch.setattr(h.service, "native_lookup", unavailable)
    assert await h.service.upgrades.upgrade("a", before["song_id"], fetched, Quality(format="mp3"))
    row = await h.store.fetchone("SELECT * FROM music_import_items")
    assert row["upgrade"] == "" and row["reserved_size"] == before["reserved_size"]
    assert h.scans == scans
    assert await h.service.upgrades.recover() == 0


async def test_repeated_upgrades_keep_the_original_and_all_retained_bytes(
    harness, monkeypatch, tmp_path
):
    h = harness
    before = await complete(h)
    carry(h, monkeypatch)
    first = tmp_path / "new.mp3"
    first.write_bytes(b"first invented upgrade")
    second = tmp_path / "new.flac"
    second.write_bytes(b"second invented upgrade")
    for path in (first, second):
        assert (
            await h.service.upgrades.upgrade(
                "a", before["song_id"], path, Quality(format=path.suffix[1:])
            )
            == ""
        )
    row = await h.store.fetchone("SELECT * FROM music_import_items")
    original = json.loads(row["upgraded_from"])
    assert original["staging"] == before["staging_name"]
    assert original["sha256"] == before["digest"]
    assert original["final"] == before["final_name"]
    assert len(original["upgrades"]) == 1
    previous = original["upgrades"][0]
    incoming = h.root / "_imports" / ".incoming"
    assert (incoming / previous["staging"]).read_bytes() == first.read_bytes()
    assert (incoming / row["staging_name"]).read_bytes() == second.read_bytes()
    assert (
        hashlib.sha256((incoming / original["staging"]).read_bytes()).hexdigest()
        == before["digest"]
    )
    assert (
        row["reserved_size"]
        == before["reserved_size"] + first.stat().st_size + second.stat().st_size
    )
    # Already retained bytes still count against quotas, but not against free disk twice.
    h.service.limits = replace(h.limits, min_free_bytes=10)
    monkeypatch.setattr(h.service.storage, "free", lambda: 15)
    await h.service._capacity("a", 5)
    h.service.limits = replace(h.service.limits, max_owner_bytes=row["reserved_size"])
    with pytest.raises(ImportError, match="reserved music storage"):
        await h.service._capacity("a", 1)
