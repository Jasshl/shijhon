"""Scoped async write authorization on real disposable native library paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from shijhon.placeholders.engine import MaterializeError
from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("operation", ["catalog", "fill", "restore"])
async def test_write_guard_rechecks_after_staging_and_keeps_recovery(
    navidrome: NavidromeInstance, tmp_path: Path, operation: str
) -> None:
    release = catalog_release(
        "guard-" + operation, "Guard " + operation, "Synthetic guard artist", 2
    )
    owned_id = None
    links = {}
    if operation == "fill":
        write_album(
            navidrome.music,
            Album(release.artist, release.title, (Track(release.tracks[0].title, 1),)),
        )
        navidrome.scan()
        song = navidrome.client().ok("search3", {"query": release.title})["searchResult3"]["song"][
            0
        ]
        owned_id = song["albumId"]
        links[release.tracks[0].ref] = song["id"]
    async with engine_for(navidrome, tmp_path) as parts:
        engine = parts.engine
        original = None
        if operation == "restore":
            original = await engine.materialize(release)
            removed = await engine.remove_release(str(release.ref))
            assert removed.removed == 2
        revoked = False
        staged = False

        async def authorize() -> None:
            if revoked:
                raise MaterializeError("authorization revoked")

        async def hook(stage: str) -> None:
            nonlocal revoked, staged
            if stage == "staged":
                staged = revoked = True

        engine._at = hook
        with (
            engine.guard_writes(authorize),
            pytest.raises(MaterializeError, match="authorization revoked"),
        ):
            await engine.materialize(release, owned_album_id=owned_id, links=links)
        assert staged
        assert not await parts.store.fetchall("SELECT * FROM placeholders")
        assert not await parts.store.fetchall("SELECT * FROM pending_placeholders")
        assert not list(parts.layout.staging.glob("*.flac"))
        assert not list(
            parts.layout.absolute(
                parts.layout.release_folder(release.ref, release.artist, release.title)
            ).glob("*.flac")
        )
        # Leaving this caller's guard restores ordinary behavior; same IDs on restore.
        result = await engine.materialize(release, owned_album_id=owned_id, links=links)
        assert len(result.created) == (
            1 if operation == "fill" else 2 if operation == "catalog" else 0
        )
        if original:
            assert result.album_id == original.album_id
            assert await parts.navidrome.song(next(iter(original.created.values())))
