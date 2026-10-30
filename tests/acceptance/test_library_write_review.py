"""Library-write regressions, using generated recordings and a real Navidrome."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import mutagen
import pytest
from mutagen.mp4 import MP4

from shijhon.music_imports import tagmap
from shijhon.placeholders import engine as engine_module
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import MaterializeError, ReplaceError
from shijhon.placeholders.layout import Layout
from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import tone
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio


async def test_materialization_finds_a_free_name_after_repeated_collisions(
    navidrome: NavidromeInstance,
    tmp_path: Path,
) -> None:
    release = catalog_release("collision-loop", "Name Loop", "Name Artist", 1)
    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        original = p.layout.absolute(f"{made.folder}/{Layout.file_name(release.tracks[0])}")
        gained = replace(
            release.tracks[0], ref=replace(release.tracks[0].ref, id="gained"), number=2
        )
        # A previous retag can leave a file at the name now wanted by a new track.
        await p.engine.retag(
            next(iter(made.created.values())), replace(release.tracks[0], number=3), release
        )
        gained = replace(gained, number=1)
        digest = hashlib.sha256(str(gained.ref).encode()).hexdigest()[:6]
        occupied = [
            original.parent / Layout.file_name(gained, f" [{digest}].flac"),
            original.parent / Layout.file_name(gained, f" [{digest}-2].flac"),
        ]
        # Non-audio foreign files are still occupied names.
        for path in occupied:
            path.write_bytes(b"foreign occupant")
        fresh = replace(release, tracks=(replace(release.tracks[0], number=3), gained))
        added = await p.engine.materialize(fresh)
        row = await p.engine.placeholder_row(added.created[gained.ref])
        assert row["path"].endswith(f" [{digest}-3].flac")
        assert all(path.read_bytes() == b"foreign occupant" for path in occupied)


async def test_a_file_arriving_after_planning_is_never_replaced_or_rolled_back(
    navidrome: NavidromeInstance,
    tmp_path: Path,
) -> None:
    release = catalog_release("collision-race", "Late Occupant", "Name Artist", 1)
    async with engine_for(navidrome, tmp_path) as p:
        target = p.layout.absolute(
            p.layout.release_folder(release.ref, release.artist, release.title)
        ) / Layout.file_name(release.tracks[0])

        async def step(at: str) -> None:
            if at == "staged":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"late foreign occupant")

        p.engine.step = step
        with pytest.raises(MaterializeError):
            await p.engine.materialize(release)
        assert target.read_bytes() == b"late foreign occupant"
        assert await p.store.fetchall("SELECT * FROM pending_placeholders") == []
        assert await p.store.fetchall("SELECT * FROM placeholders") == []
        assert not list(p.layout.staging.glob("*.flac"))


@pytest.mark.parametrize(
    "foreign", ["unmarked", "track", "release", "silent", "destination", "missing"]
)
async def test_revert_leaves_foreign_files_untouched(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    foreign: str,
) -> None:
    release = catalog_release("foreign-" + foreign, "Foreign " + foreign, "Revert Artist", 1)
    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        song = next(iter(made.created.values()))
        await p.engine.replace_with_delivered(song, tone(432, 3, "m4a"))
        row = await p.engine.placeholder_row(song)
        source = p.layout.absolute(row["path"])
        target = p.layout.absolute(row["placeholder_path"])
        protected = source
        if foreign in ("destination", "missing"):
            target.write_bytes(b"foreign destination")
            protected = target
            if foreign == "missing":
                source.unlink()
        elif foreign == "unmarked":
            shutil.copyfile(tone(433, 3, "m4a"), source)
        else:
            audio = MP4(source)
            key = tagging.MARKER_TRACK if foreign == "track" else tagging.MARKER_RELEASE
            audio[f"----:com.apple.iTunes:{key.upper()}"] = [b"test:foreign"]
            if foreign == "silent":
                audio[f"----:com.apple.iTunes:{tagging.MARKER_SILENCE.upper()}"] = [b"1"]
            audio.save()
        before = protected.read_bytes()
        with pytest.raises(ReplaceError, match="left as it is"):
            await p.engine.revert_to_placeholder(song)
        assert protected.read_bytes() == before
        assert (await p.engine.placeholder_row(song))["state"] == "delivered"


@pytest.mark.parametrize("missing", [False, True])
async def test_revert_accepts_its_own_silent_file_at_the_original_path(
    navidrome: NavidromeInstance, tmp_path: Path, missing: bool
) -> None:
    release = catalog_release(
        "silent-revert-" + str(missing), "Silent Return " + str(missing), "Revert Artist", 1
    )
    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        song = next(iter(made.created.values()))
        row = await p.engine.placeholder_row(song)
        target = p.layout.absolute(row["path"])
        silent = target.read_bytes()
        delivered = await p.engine.replace_with_delivered(song, tone(437, 3, "m4a"))
        target.write_bytes(silent)
        if missing:
            p.layout.absolute(delivered).unlink()
        assert await p.engine.revert_to_placeholder(song, only_if_missing=missing)
        row = await p.engine.placeholder_row(song)
        assert row["state"] == "placeholder" and row["path"] == row["placeholder_path"]
        assert not p.layout.absolute(delivered).exists()
        marker = tagging.marker_of(target)
        assert marker is not None and marker.silence
        assert (await p.navidrome.song(song))["path"] == row["path"]


@pytest.mark.parametrize("key", ["wma", "wavpack", "monkeys-audio", "musepack"])
async def test_fill_reads_album_tags_from_owned_native_formats(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    key: str,
) -> None:
    release = catalog_release("native-fill-" + key, "Native " + key, "Native Artist", 2)
    fmt = next(f for f in IMPORT_FORMATS if f.key == key)
    owned = import_tone(navidrome.music / ("owned-" + key), fmt, tagged=False)
    tagging.write_delivered(
        owned,
        tagging.placeholder_tags(tagging.catalog_album_tags(release), release.tracks[0], release),
    )
    navidrome.scan(full=True)
    async with engine_for(navidrome, tmp_path) as p:
        songs = await p.navidrome.songs_under("owned-" + key)
        assert len(songs) == 1
        made = await p.engine.materialize(
            release,
            owned_album_id=songs[0]["albumId"],
            links={release.tracks[0].ref: songs[0]["id"]},
        )
        assert len(made.created) == 1
        assert made.album_id == songs[0]["albumId"]
        assert len(await p.navidrome.songs_of_album(made.album_id)) == 2
        assert tagging.album_tags_of(owned) == tagmap.album_tags_at(owned)


@pytest.mark.parametrize("key", ["aac-m4a", "wma", "import-m4a"])
@pytest.mark.parametrize("release_date", [None, "2004"])
async def test_delivery_and_import_keep_the_owned_album_and_supported_dates(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    key: str,
    release_date: str | None,
) -> None:
    release = catalog_release(
        "date-" + key + str(release_date),
        "Dated " + key + str(release_date),
        "Date Artist",
        2,
        date=None,
    )
    owned = navidrome.music / ("date-owned-" + key + str(release_date)) / "first.flac"
    owned.parent.mkdir()
    shutil.copyfile(tone(434, 3, "flac"), owned)
    album_tags = {
        "album": [release.title],
        "albumartist": [release.artist],
        "recordingdate": ["2001"],
    }
    if release_date:
        album_tags["releasedate"] = [release_date]
    comments = tagging.placeholder_tags(
        album_tags,
        release.tracks[0],
        release,
    )
    tagging.write_flac(owned, comments)
    navidrome.scan(full=True)
    async with engine_for(navidrome, tmp_path) as p:
        [first] = await p.navidrome.songs_under(owned.parent.name)
        made = await p.engine.materialize(
            release, owned_album_id=first["albumId"], links={release.tracks[0].ref: first["id"]}
        )
        song = next(iter(made.created.values()))
        before = await p.navidrome.song(song)
        assert before is not None and before["year"] == 2001
        if key == "import-m4a":
            # Exercise the import writer joining the owned album, then Navidrome's read.
            from shijhon.music_imports import retag

            imported = navidrome.music / owned.parent.name / "imported.m4a"
            with tone(435, 3, "m4a").open("rb") as source, imported.open("w+b") as target:
                retag.retag(source, target, album_tags, release.artist, "m4a")
            navidrome.scan(full=True)
            songs = await p.navidrome.songs_under(owned.parent.name)
            [after] = [s for s in songs if s["path"].endswith("imported.m4a")]
            audio = MP4(imported)
        else:
            fmt = next(f for f in IMPORT_FORMATS if f.key == key)
            delivered = import_tone(tmp_path / "delivery", fmt, tagged=False)
            await p.engine.replace_with_delivered(song, delivered)
            row = await p.engine.placeholder_row(song)
            assert row["state"] == "delivered"
            after = await p.navidrome.song(song)
            audio = MP4(p.layout.absolute(row["path"])) if key == "aac-m4a" else None
        assert after is not None and after["albumId"] == before["albumId"]
        assert after["trackNumber"] == (0 if key == "import-m4a" else before["trackNumber"])
        assert after.get("releaseDate") == before.get("releaseDate")
        if audio is not None:
            assert after["year"] == 2001
            assert audio["----:com.apple.iTunes:RECORDINGDATE"] == [b"2001"]
            assert audio.get("\xa9day") == (["2001"] if release_date else None)
            if release_date:
                assert audio["----:com.apple.iTunes:RELEASEDATE"] == [release_date.encode()]
        else:
            # ASF has no separate recording date: its year follows the release date.
            assert after.get("year", 0) == (2004 if release_date else 0)


@pytest.mark.parametrize("how", ["path", "descriptor", "redeliver"])
async def test_failed_delivery_copy_removes_partial_staging(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    release = catalog_release("copy-" + how, "Copy " + how, "Copy Artist", 1)
    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        song = next(iter(made.created.values()))
        audio = tone(436, 3, "m4a")
        if how == "redeliver":
            await p.engine.replace_with_delivered(song, audio)
        row = dict(await p.engine.placeholder_row(song))
        original = p.layout.absolute(row["path"]).read_bytes()

        def fail(source: Any, destination: Path) -> None:
            destination.write_bytes(b"partial copy")
            raise OSError("copy failed")

        monkeypatch.setattr(engine_module, "_copy_descriptor", fail)
        monkeypatch.setattr(engine_module.shutil, "copyfile", fail)
        with pytest.raises(ReplaceError, match="OSError"):
            if how == "redeliver":
                await p.engine.redeliver(song, audio, unused_since=p.engine.clock())
            elif how == "descriptor":
                with audio.open("rb") as file:
                    await p.engine.replace_with_delivered(
                        song, source_fd=file.fileno(), suffix=".m4a"
                    )
            else:
                await p.engine.replace_with_delivered(song, audio)
        assert dict(await p.engine.placeholder_row(song)) == row
        assert p.layout.absolute(row["path"]).read_bytes() == original
        assert {f.name for f in p.layout.staging.iterdir()} == {".ndignore"}


async def test_artist_case_changes_reuse_the_existing_folder(
    navidrome: NavidromeInstance,
    tmp_path: Path,
) -> None:
    async with engine_for(navidrome, tmp_path) as p:
        first = catalog_release("case-one", "First Case", "Copper Finch", 1)
        second = catalog_release("case-two", "Second Case", "COPPER FINCH", 1)
        await p.engine.materialize(first)
        made = await p.engine.materialize(second)
        assert Path(made.folder).parent.name == "Copper Finch"
        [song] = await p.navidrome.songs_under(made.folder)
        assert song["id"] == next(iter(made.created.values()))


async def test_backup_cleanup_explains_the_running_repair_interval(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with engine_for(navidrome, tmp_path) as p:
        backup = tmp_path / "unremovable-backup"
        backup.mkdir()  # unlink fails: the repair has to retry later
        await p.engine._drop(backup)
        assert "tried again within a minute (and at the next start)" in caplog.text


async def test_a_clean_track_on_a_mixed_release_stays_clean_for_delivery(
    navidrome: NavidromeInstance,
    tmp_path: Path,
) -> None:
    from shijhon.delivery.addon import Found, Wanted, mismatches
    from shijhon.delivery.download_first import track_of

    release = catalog_release("mixed-clean", "Mixed Lanterns", "Paper Quartet", 2)
    clean = replace(release.tracks[0], clean=True)
    release = replace(release, tracks=(clean, replace(release.tracks[1], explicit=True)))
    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        row = await p.engine.placeholder_row(made.created[clean.ref])
        assert tagging.read_vorbis(p.layout.absolute(row["path"]))["itunesadvisory"] == ["2"]
        persisted = track_of(row)
        assert persisted.version == "clean"
        wanted = Wanted(
            persisted.isrc,
            persisted.title,
            persisted.artist,
            persisted.duration_ms,
            persisted.version,
        )
        explicit = Found(
            "explicit", clean.title + " (Explicit)", clean.artist, clean.duration_ms, "ZZSHJ2600997"
        )
        assert mismatches(explicit, wanted) == ["title"]


@pytest.mark.parametrize("failure", [ValueError, subprocess.SubprocessError, mutagen.MutagenError])
async def test_unreadable_owned_album_tags_are_a_materialization_error(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    from shijhon.placeholders.engine import OwnedAlbum

    release = catalog_release("broken-owned", "Broken Lantern", "Tag Artist", 1)
    owned = OwnedAlbum("owned", [{"path": "broken.mka"}])

    def broken(path: str) -> dict[str, list[str]]:
        raise failure("unreadable tags")

    # MKA's album reader uses ffprobe; these are its parse/process failures.
    monkeypatch.setattr(tagmap, "matroska_album_tags", broken)
    async with engine_for(navidrome, tmp_path) as p:
        with pytest.raises(MaterializeError, match="cannot read the owned album's tags"):
            await p.engine._album_tags(release, owned)
        assert await p.store.fetchall("SELECT * FROM placeholders") == []
