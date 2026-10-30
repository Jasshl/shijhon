"""Scanner-visible destinations retain original bytes and distinct source folders."""

from dataclasses import replace

import pytest

from shijhon.music_imports.model import scanner_ignored_path
from tests.acceptance.test_music_import_overlap import LIBRARY, approve, upload, world
from tests.harness.library import write_album

pytestmark = [pytest.mark.anyio, pytest.mark.canary]


async def test_hidden_source_folders_are_indexed_without_merging_visible_names(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    w = await world(nd, tmp_path, ("ben",))
    owner = w.users["ben"][0]
    source = tmp_path / "source"
    files = write_album(source, replace(LIBRARY, tracks=LIBRARY.tracks[:3]))
    paths = [".Velvet Comet/One.flac", "Velvet Comet/Two.flac", ".Velvet Comet/.Three.flac"]
    renamed = []
    for file, path in zip(files, paths, strict=True):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        file.rename(target)
        renamed.append(target)
    try:
        review = await upload(w, "ben", renamed, source, "hidden-folders")
        assert {item["path"] for item in review["items"]} == set(paths)
        done = await approve(w, "ben", review)
        assert done["complete_files"] == 3
        rows = await w.store.fetchall("SELECT * FROM music_import_items ORDER BY display_path")
        assert len({r["final_name"].rsplit("/", 1)[0] for r in rows}) == 2
        for row in rows:
            assert not scanner_ignored_path(row["final_name"])
            final = nd.music / "_imports" / row["final_name"]
            assert final.read_bytes() == (source / row["display_path"]).read_bytes()
            native = await w.imports.native_lookup(owner, "1", "_imports/" + row["final_name"])
            assert native and native.song_id == row["song_id"]
            response = await w.call("ben", "getSong", {"id": native.song_id})
            assert response["song"]["title"].startswith("Lantern")
    finally:
        await w.caller.aclose()
        await w.imports.aclose()
        await w.store.close()
