"""Preserved names, cross-page ambiguity and nested retained-storage safety."""

import os

import pytest

from shijhon.music_imports import ImportError, ManifestEntry
from tests.unit.test_music_imports import (
    audio,
    chunks,
    declared,
    freeze,
    reviewed,
)

pytest_plugins = ("tests.unit.test_music_imports",)
pytestmark = pytest.mark.anyio


async def test_preserved_tree_and_identity_survive_restart(harness):
    h = harness
    source = "My Library: Volume 1/Glass Orchard/01 First Light.flac"
    job = await declared(h, [ManifestEntry("one", source, len(audio()))])
    item = job["items"][0]
    await h.service.receive("a", job["id"], item["id"], 0, chunks(audio()))
    job = await freeze(h, "a", job["id"])
    await h.service.approve("a", job["id"], job["revision"], "approve")
    await h.service.tick()
    done = await h.service.status("a", job["id"])
    assert done["state"] == "done"
    receipt = await h.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert receipt["final_name"] == "batch-" + job["id"] + "/" + source
    assert receipt["asset_id"] not in source
    final = h.root / "_imports" / receipt["final_name"]
    assert final.read_bytes() == audio()
    await h.service.aclose()
    h.service = h.build()
    await h.service.initialize()
    native = dict(h.access.rows[0], libraryId=1)
    assert await h.service.asset_lookup("1", [native]) == {native["id"]: receipt["asset_id"]}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Folder/Song.flac", "Folder/Song.flac"),
        ("Folder/Song.flac", "Folder/song.FLAC"),
        ("Café/Song.flac", "Cafe\u0301/Song.flac"),
        ("Folder/Song.flac", "folder/Other.flac"),
        ("Folder.flac", "Folder.flac/Song.flac"),
        ("Folder.flac/Song.flac", "Folder.flac"),
    ],
)
@pytest.mark.parametrize("cross_page", [False, True])
async def test_manifest_refuses_ambiguous_paths_without_reservation(
    harness, first, second, cross_page
):
    h = harness
    job = await h.service.create("a", "collisions")
    entries = [ManifestEntry("one", first, 1), ManifestEntry("two", second, 1)]
    if cross_page:
        await h.service.declare("a", job["id"], "first", entries[:1])
    with pytest.raises(ImportError) as error:
        await h.service.declare("a", job["id"], "second", entries[1:] if cross_page else entries)
    assert error.value.code == "path_collision"
    count = await h.store.fetchone("SELECT count(*) FROM music_import_items")
    assert count[0] == int(cross_page)


async def test_shared_exact_directory_is_preserved(harness):
    job = await harness.service.create("a", "shared-folder")
    for n in range(2):
        result = await harness.service.declare(
            "a", job["id"], str(n), [ManifestEntry(str(n), f"Album/{n}.flac", 1)]
        )
    assert result["total_files"] == 2


async def test_nested_symlink_parent_never_publishes_outside_root(harness):
    h = harness
    job = await reviewed(h)
    row = await h.store.fetchone("SELECT * FROM music_import_items WHERE job_id=?", [job["id"]])
    outside = h.root / "outside"
    outside.mkdir()
    batch = (h.root / "_imports" / row["final_name"]).parent
    batch.symlink_to(outside, target_is_directory=True)
    await h.service.approve("a", job["id"], job["revision"], "approve")
    await h.service.tick()
    assert (await h.service.status("a", job["id"]))["state"] == "paused"
    assert list(outside.iterdir()) == []
    assert not await h.store.fetchone("SELECT 1 FROM collection_originals")


async def test_legacy_flat_receipt_still_publishes_and_resolves(harness):
    h = harness
    job = await reviewed(h)
    row = await h.store.fetchone("SELECT * FROM music_import_items WHERE job_id=?", [job["id"]])
    final_name = row["asset_id"] + ".flac"
    await h.store.execute(
        "UPDATE music_import_items SET final_name=? WHERE id=?", [final_name, row["id"]]
    )
    # Re-freeze after simulating a receipt created by the earlier version.
    job = await freeze(h, "a", job["id"])
    await h.service.approve("a", job["id"], job["revision"], "approve")
    await h.service.tick()
    assert (await h.service.status("a", job["id"]))["state"] == "done"
    with h.service.storage.final_parent(final_name) as (parent, leaf):
        assert leaf == final_name
        assert (
            os.stat(leaf, dir_fd=parent).st_ino == (h.root / "_imports" / final_name).stat().st_ino
        )
    native = dict(h.access.rows[0], libraryId=1)
    assert await h.service.asset_lookup("1", [native]) == {native["id"]: row["asset_id"]}


@pytest.mark.parametrize("interrupted_mkdir", [False, True])
async def test_published_directories_are_scannable_with_private_umask(harness, interrupted_mkdir):
    h = harness
    job = await reviewed(h)
    row = await h.store.fetchone("SELECT * FROM music_import_items WHERE job_id=?", [job["id"]])
    destination = h.root / "_imports" / row["final_name"]
    if interrupted_mkdir:
        destination.parent.mkdir(mode=0o700)
    await h.service.approve("a", job["id"], job["revision"], "approve")
    previous = os.umask(0o077)
    try:
        await h.service.tick()
    finally:
        os.umask(previous)
    assert (await h.service.status("a", job["id"]))["state"] == "done"
    assert destination.parent.stat().st_mode & 0o777 == 0o755
    assert destination.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "path",
    [
        ".Song.flac",
        ".git/Song.flac",
        ".Hidden/Song.flac",
        "Visible/.Nested/Song.flac",
        "lost+found/Song.flac",
    ],
)
async def test_native_ignored_paths_get_visible_destinations(harness, path):
    from shijhon.music_imports.model import scanner_ignored_path

    h = harness
    job = await declared(h, [ManifestEntry("file", path, len(audio()))])
    assert job["rejected_files"] == 0
    item = job["items"][0]
    assert item["path"] == path
    await h.service.receive("a", job["id"], item["id"], 0, chunks(audio()))
    job = await freeze(h, "a", job["id"])
    # A second review must preserve the allocated destination as well.
    job = await freeze(h, "a", job["id"])
    await h.service.approve("a", job["id"], job["revision"], "go")
    assert await h.service.tick()
    row = await h.service._item(job["id"], item["id"])
    assert not scanner_ignored_path(row["final_name"])
    assert row["final_name"] == row["source_name"]
    assert (h.root / "_imports" / row["final_name"]).read_bytes() == audio()


@pytest.mark.parametrize("reverse", [False, True])
async def test_visible_names_do_not_merge_source_folders_across_pages_and_restart(harness, reverse):
    h = harness
    job = await h.service.create("a", "hidden-collisions")
    paths = [
        ".Velvet Comet/One.flac",
        "Velvet Comet/Two.flac",
        ".Velvet Comet/.Three.flac",
        "Velvet Comet (2)/Four.flac",
    ]
    if reverse:
        paths.reverse()
    for n, path in enumerate(paths):
        await h.service.declare("a", job["id"], str(n), [ManifestEntry(str(n), path, 4)])
        await h.service.aclose()
        h.service = h.build()
        await h.service.initialize()
    rows = await h.store.fetchall("SELECT display_path,final_name FROM music_import_items")
    targets = {r["display_path"]: r["final_name"] for r in rows}
    hidden = targets[".Velvet Comet/One.flac"].rsplit("/", 1)[0]
    assert targets[".Velvet Comet/.Three.flac"].rsplit("/", 1)[0] == hidden
    assert len({r["final_name"].rsplit("/", 1)[0] for r in rows}) == 3
    assert len({r["final_name"].casefold() for r in rows}) == 4


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Folder/.Song.flac", "Folder/Song.flac"),
        (".Café/One.flac", "Cafe\u0301/Two.flac"),
        (".Album.flac", "Album.flac/Song.flac"),
        ("." + "é" * 124 + ".flac", "é" * 124 + ".flac"),
    ],
)
async def test_visible_destination_collisions_keep_safe_names_and_audio_suffixes(
    harness, first, second
):
    from shijhon.music_imports.model import relative_path, scanner_ignored_path

    job = await declared(harness, [ManifestEntry("one", first, 1), ManifestEntry("two", second, 1)])
    rows = await harness.store.fetchall(
        "SELECT final_name FROM music_import_items WHERE job_id=?", [job["id"]]
    )
    names = {row["final_name"] for row in rows}
    assert len(names) == 2
    for name in names:
        assert relative_path(name) == name and not scanner_ignored_path(name)
        assert name.endswith(".flac")


async def test_same_inode_duplicate_must_still_have_the_recorded_digest(harness):
    import hashlib

    h = harness
    name = "a" * 32 + ".part"
    final = "batch-" + "b" * 32 + "/Song.flac"
    original = b"original audio"
    descriptor = h.service.storage.file(name, create=True)
    os.write(descriptor, original)
    os.close(descriptor)
    h.service.storage.place(name, final)
    digest = hashlib.sha256(original).hexdigest()
    assert h.service.storage.holds(final, name, len(original), digest)
    path = h.root / "_imports" / final
    with path.open("r+b") as changed:
        changed.write(b"modified audio")
    with pytest.raises(ImportError) as error:
        h.service.storage.holds(final, name, len(original), digest)
    assert error.value.code == "identity"
