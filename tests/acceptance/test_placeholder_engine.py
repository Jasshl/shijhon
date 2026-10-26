"""Placeholder engine operations beyond the lettered suites: recovery from
marker tags, scans racing Navidrome's own watcher, silence at the exact duration and
concurrent materialization of one release."""

from __future__ import annotations

import json
import shutil
import sys
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.delivery.download_first import kept_suffix
from shijhon.placeholders import layout as layout_module
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import MaterializeError, ReplaceError
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.recover import recover
from shijhon.placeholders.silence import SilenceMaker
from tests.conftest import NavidromeFactory
from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import frequency_for, tone
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio


async def test_recovery_from_marker_tags(navidrome: NavidromeInstance, tmp_path: Path) -> None:
    release = catalog_release("r-main", "Recovered", "Recovery Artist", 3)
    first_state = tmp_path / "first"
    async with engine_for(navidrome, first_state) as parts:
        result = await parts.engine.materialize(release)
        delivered = tmp_path / "delivered.m4a"
        shutil.copyfile(tone(frequency_for("recover"), 3, "m4a"), delivered)
        replaced = result.created[release.tracks[1].ref]
        await parts.engine.replace_with_delivered(replaced, delivered)
        rows = await parts.store.fetchall(
            "SELECT song_id, path, placeholder_path, track_ref, release_ref, isrc, state"
            " FROM placeholders ORDER BY song_id"
        )
        original = [dict(r) for r in rows]

    async with engine_for(navidrome, tmp_path / "second") as parts:
        report = await recover(parts.engine)
        assert (report.releases, report.placeholders, report.skipped) == (1, 3, [])
        rows = await parts.store.fetchall(
            "SELECT song_id, path, placeholder_path, track_ref, release_ref, isrc, state"
            " FROM placeholders ORDER BY song_id"
        )
        assert [dict(r) for r in rows] == original
        # The recovered record is good enough to revert the delivered file.
        await parts.engine.revert_to_placeholder(replaced)
        song = await parts.navidrome.song(replaced)
        assert song is not None and song["path"].endswith(".flac")
        again = await recover(parts.engine)
        assert (again.releases, again.placeholders) == (0, 0)


@pytest.mark.parametrize(
    ("artist", "title"),
    [
        (". .." + " " * 100 + "x", "Outside"),
        ("..", ".."),
        (". .staging", "Staged"),
        ("../..", "../../up"),
    ],
)
async def test_no_catalog_name_leaves_the_placeholder_folder(
    navidrome: NavidromeInstance, tmp_path: Path, artist: str, title: str
) -> None:
    """Whatever a catalog names an artist or an album, its placeholders are written
    inside the placeholder folder - where Navidrome finds them - and nowhere else."""
    key = f"pe-{zlib.crc32((artist + title).encode())}"
    release = catalog_release(key, title, artist, 2)
    outside = navidrome.music.parent

    def others() -> set[str]:
        found = {str(p) for p in outside.rglob("*")}
        return {p for p in found if not p.startswith(str(navidrome.music / "_shijhon") + "/")}

    before = others() | {str(navidrome.music / "_shijhon")}
    async with engine_for(navidrome, tmp_path) as parts:
        result = await parts.engine.materialize(release)
        rows = await parts.store.fetchall("SELECT path FROM placeholders")
        assert len(rows) == 2
        for row in rows:
            path = parts.layout.contained(row["path"])
            assert path.is_file() and path.parent.parent.parent == parts.layout.root
            assert not any(part.startswith(".") for part in Path(row["path"]).parts)
    assert others() - before == set()
    detail = navidrome.client().ok("getAlbum", {"id": result.album_id})["album"]
    assert detail["songCount"] == 2


async def test_a_path_outside_the_placeholder_folder_is_never_written(
    navidrome: NavidromeInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check: were a name to slip through, the write is refused before anything
    is recorded or moved."""
    release = catalog_release("pe-last", "Last Check", "Checked Artist", 2)
    monkeypatch.setattr(layout_module, "safe_name", lambda text, limit=80: "..")
    async with engine_for(navidrome, tmp_path) as parts:
        with pytest.raises(MaterializeError, match="outside the placeholder folder"):
            await parts.engine.materialize(release)
        monkeypatch.undo()
        # ... also for a recorded folder, and for a file's name.
        monkeypatch.setattr(
            Layout, "release_folder", lambda self, ref, artist, title: "_shijhon/../Escaped [0]"
        )
        with pytest.raises(MaterializeError, match="outside the placeholder folder"):
            await parts.engine.materialize(release)
        monkeypatch.undo()
        monkeypatch.setattr(Layout, "file_name", staticmethod(lambda track, suffix=".flac": ".."))
        with pytest.raises(MaterializeError, match="outside the placeholder folder"):
            await parts.engine.materialize(release)
        monkeypatch.undo()
        assert await parts.store.fetchall("SELECT 1 FROM pending_placeholders") == []
        assert await parts.store.fetchall("SELECT 1 FROM placeholders") == []
        assert not (navidrome.music / "Escaped [0]").exists()
        result = await parts.engine.materialize(release)  # and it is written as usual after
        assert len(result.created) == 2


async def test_a_path_recorded_outside_the_folder_is_never_swapped(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """A placeholder recorded outside the placeholder folder (a name that leads out of it)
    gets no delivered audio, no retag and no repair written there."""
    release = catalog_release("pe-recorded", "Recorded Elsewhere", "Recorded Artist", 1)
    delivered = tmp_path / "delivered.flac"
    shutil.copyfile(tone(frequency_for("pe-recorded"), 3, "flac"), delivered)
    async with engine_for(navidrome, tmp_path / "state") as parts:
        result = await parts.engine.materialize(release)
        song = result.created[release.tracks[0].ref]
        await parts.store.execute(
            "UPDATE placeholders SET path = ?, placeholder_path = ? WHERE song_id = ?",
            ["_shijhon/../Escaped/1-01 x.flac", "_shijhon/../Escaped/1-01 x.flac", song],
        )
        with pytest.raises(ReplaceError, match="outside the placeholder folder"):
            await parts.engine.replace_with_delivered(song, delivered)
        retitled = replace(release.tracks[0], title="Another Title")
        with pytest.raises(ReplaceError, match="outside the placeholder folder"):
            await parts.engine.retag(song, retitled, release)
    assert not (navidrome.music / "Escaped").exists()


async def test_a_folder_that_is_a_link_out_of_the_placeholder_folder_is_never_written(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """Containment is also where a path really is: an artist's folder that is a link into
    the owner's music is refused, nothing written through it."""
    owned = navidrome.music / "Owned Elsewhere"
    owned.mkdir(parents=True, exist_ok=True)
    release = catalog_release("pe-linked", "Linked Album", "Linked Artist", 2)
    async with engine_for(navidrome, tmp_path) as parts:
        parts.layout.ensure()
        (parts.layout.root / "Linked Artist").symlink_to(owned, target_is_directory=True)
        try:
            with pytest.raises(MaterializeError, match="outside the placeholder folder"):
                await parts.engine.materialize(release)
            assert list(owned.iterdir()) == []
            assert await parts.store.fetchall("SELECT 1 FROM pending_placeholders") == []
        finally:
            (parts.layout.root / "Linked Artist").unlink()
        assert len((await parts.engine.materialize(release)).created) == 2  # a real folder


async def test_a_rollback_leaves_recorded_paths_outside_the_folder_alone(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """A record of new placeholders, or of a release taken out, that names a path outside
    the placeholder folder: nothing there is removed or replaced."""
    outside = navidrome.music / "Escaped Album"
    outside.mkdir(parents=True, exist_ok=True)
    song, cover = outside / "1-01 x.flac", outside / "cover.jpg"
    song.write_bytes(b"an owned file")
    cover.write_bytes(b"its cover")
    folder = "_shijhon/../Escaped Album"
    async with engine_for(navidrome, tmp_path) as parts:
        parts.layout.ensure()
        confirmed = await parts.engine._roll_back(
            [f"{folder}/1-01 x.flac"], folder, True, removed_folder=True, bound=1.0
        )
        assert confirmed in (True, False)
        release = {"folder": folder}
        row = {"path": f"{folder}/1-01 x.flac", "placeholder_path": f"{folder}/1-01 x.flac"}

        async def gone() -> None:
            await parts.engine._take_out(release, [row])

        with anyio.move_on_after(5):
            await gone()
        assert song.read_bytes() == b"an owned file" and cover.read_bytes() == b"its cover"
        # ... also by a release's folder that is a link there: its cover is not Shijhon's.
        (parts.layout.root / "Linked Out").mkdir()
        linked = parts.layout.root / "Linked Out" / "Album [0a1b2c3d]"
        linked.symlink_to(outside, target_is_directory=True)
        folder = "_shijhon/Linked Out/Album [0a1b2c3d]"
        try:
            await parts.engine._roll_back(
                [f"{folder}/1-01 x.flac"], folder, True, removed_folder=True, bound=1.0
            )
            row = {"path": f"{folder}/1-01 x.flac", "placeholder_path": f"{folder}/1-01 x.flac"}
            with anyio.move_on_after(5):
                await parts.engine._take_out({"folder": folder}, [row])
        finally:
            linked.unlink()
            (parts.layout.root / "Linked Out").rmdir()
    assert song.read_bytes() == b"an owned file" and cover.read_bytes() == b"its cover"


async def test_materialize_while_the_watcher_scans(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    watched = navidrome_factory({"ND_SCANNER_WATCHERWAIT": "200ms"})
    async with engine_for(watched, tmp_path) as parts:
        releases = [catalog_release(f"w-{n}", f"Watched {n}", "Watch Artist", 4) for n in range(6)]
        results = []
        for release in releases:
            results.append(await parts.engine.materialize(release))
            await anyio.sleep(0.1)
    for result in results:
        detail = watched.client().ok("getAlbum", {"id": result.album_id})["album"]
        assert detail["songCount"] == 4


async def test_concurrent_materialization_of_one_release(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release("c-once", "Once Only", "Concurrent Artist", 3)
    outcomes = []
    async with engine_for(navidrome, tmp_path) as parts:

        async def one() -> None:
            outcomes.append(await parts.engine.materialize(release))

        async with anyio.create_task_group() as tg:
            for _ in range(5):
                tg.start_soon(one)
        rows = await parts.store.fetchall("SELECT song_id FROM placeholders")
    assert len(rows) == 3
    assert sum(len(o.created) for o in outcomes) == 3
    assert len({o.album_id for o in outcomes}) == 1


async def test_silence_has_the_exact_duration(tmp_path: Path) -> None:
    from mutagen.flac import FLAC

    maker = SilenceMaker(parallel=2)
    durations = [217_431, 1_000, 3_500, 240_000]

    async with anyio.create_task_group() as tg:
        for ms in durations:
            tg.start_soon(maker.write, ms, tmp_path / f"{ms}.flac")
    for ms in durations:
        path = tmp_path / f"{ms}.flac"
        info = FLAC(path).info
        assert abs(info.length * 1000 - ms) < 1  # to the sample
        # A decoder accepts every frame (the frames' CRCs are checked).
        decoded = await anyio.run_process(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], check=False
        )
        assert decoded.returncode == 0 and decoded.stderr == b""
    assert maker.generated == 4
    assert not list(tmp_path.glob(".partial-*"))  # noqa: ASYNC240 - test
    # About 13 KB for four minutes (most of a short file is fixed FLAC padding).
    assert (tmp_path / "240000.flac").stat().st_size < 16_000


async def test_placeholder_length_matches_delivered_audio(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """Clients show the same length before and after real audio replaces a placeholder."""
    release = catalog_release("s-exact", "Exact Lengths", "Exact Artist", 1, seconds=3)
    delivered = tmp_path / "real.flac"
    shutil.copyfile(tone(frequency_for("exact"), 3.6, "flac"), delivered)
    # The catalog says 3.6 s: the placeholder must not round that to 4 s.
    track = replace(release.tracks[0], duration_ms=3_600)
    release = replace(release, tracks=(track,))
    async with engine_for(navidrome, tmp_path / "state") as parts:
        result = await parts.engine.materialize(release)
        song = result.created[track.ref]
        before = navidrome.client().ok("getSong", {"id": song})["song"]["duration"]
        await parts.engine.replace_with_delivered(song, delivered)
        after = navidrome.client().ok("getSong", {"id": song})["song"]["duration"]
    assert before == after == 3


async def test_delivered_audio_never_takes_the_place_of_another_file(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """Audio of another format goes beside the silent file's name: a file already there is
    left as it is, and so is a placeholder file that is not the silent one (someone's
    audio put there) or a link - nothing is moved, the row stays a placeholder."""
    release = catalog_release("pe-collide", "Colliding", "Colliding Artist", 3)
    delivered = tmp_path / "delivered.m4a"
    shutil.copyfile(tone(frequency_for("pe-collide"), 3, "m4a"), delivered)
    async with engine_for(navidrome, tmp_path / "state") as parts:
        result = await parts.engine.materialize(release)
        songs = [result.created[track.ref] for track in release.tracks]
        rows = [await parts.engine.placeholder_row(song) for song in songs]
        paths = [parts.layout.absolute(row["placeholder_path"]) for row in rows]

        beside = paths[0].with_suffix(".m4a")
        beside.write_bytes(b"someone's file")
        with pytest.raises(ReplaceError, match="another file is at"):
            await parts.engine.replace_with_delivered(songs[0], delivered)
        assert beside.read_bytes() == b"someone's file" and paths[0].exists()

        audio = tone(frequency_for("pe-collide-own"), 3, "flac").read_bytes()
        paths[1].write_bytes(audio)
        with pytest.raises(ReplaceError, match="not its silent file"):
            await parts.engine.replace_with_delivered(songs[1], delivered)
        assert paths[1].read_bytes() == audio

        elsewhere = tmp_path / "elsewhere.flac"
        elsewhere.write_bytes(audio)
        paths[2].unlink()
        paths[2].symlink_to(elsewhere)
        with pytest.raises(ReplaceError, match="is a link"):
            await parts.engine.replace_with_delivered(songs[2], delivered)
        assert paths[2].is_symlink() and elsewhere.read_bytes() == audio
        for song in songs:
            row = await parts.engine.placeholder_row(song)
            assert row["state"] == "placeholder"


# Every container a kept stream may be (``download_first._EVERY``), as music imports'
# made-up samples have it.
DELIVERED_FORMATS = (
    "flac", "aac-m4a", "alac-m4a", "mp3", "opus", "vorbis", "ogg-flac", "speex-spx", "speex-ogg",
    "wav-pcm", "aiff", "dsf", "trueaudio", "monkeys-audio", "wavpack", "musepack", "wma",
    "matroska-flac",
)  # fmt: skip
# The pinned Darwin build does not index valid TrueAudio files; the Linux ARM64 one does
# (``docs/music-imports.md``). Where it does not, Navidrome's check of the swap fails and the
# placeholder is put back; elsewhere TrueAudio is checked as every other container.
UNINDEXED = ("trueaudio",) if sys.platform == "darwin" else ()


async def test_delivered_audio_of_every_container_keeps_its_song_and_album(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """Delivered audio in each container Shijhon tags (Vorbis comments, ID3, MPEG-4, APEv2,
    ASF; Matroska by an ffmpeg stream copy): Navidrome reads it under the same song ID in
    the same album, with the placeholder's title, artist and track number, and Shijhon's
    marker in it is read again (recovery, a revert) - and its silent placeholder comes
    back."""
    formats = {f.key: f for f in IMPORT_FORMATS}
    release = catalog_release("pe-formats", "Every Format", "Format Artist", len(DELIVERED_FORMATS))
    async with engine_for(navidrome, tmp_path / "state") as parts:
        result = await parts.engine.materialize(release)
        songs = [result.created[track.ref] for track in release.tracks]
        before = {song: await parts.navidrome.song(song) for song in songs}
        album = {s["albumId"] for s in before.values() if s is not None}
        assert len(album) == 1
        for key, track, song in zip(DELIVERED_FORMATS, release.tracks, songs, strict=True):
            sample = import_tone(tmp_path / "samples" / key, formats[key], tagged=False)
            suffix = kept_suffix(sample)
            assert suffix is not None, key
            delivered = sample.with_name(f"delivered{suffix}")
            sample.rename(delivered)
            if key in UNINDEXED:
                with pytest.raises(ReplaceError, match="the placeholder was restored"):
                    await parts.engine.replace_with_delivered(song, delivered)
                row = await parts.engine.placeholder_row(song)
                assert row["state"] == "placeholder" and (await parts.navidrome.song(song)), key
                continue
            path = await parts.engine.replace_with_delivered(song, delivered)
            assert path.endswith(suffix), key
            found = await parts.navidrome.song(song)
            assert found is not None and not found.get("missing"), key
            assert found["albumId"] in album and found["path"] == path, key
            assert (found["title"], found["artist"]) == (track.title, track.artist), key
            assert found.get("trackNumber") == track.number, (key, found.get("trackNumber"))
            marker = tagging.marker_of(parts.layout.absolute(path))
            assert marker is not None and not marker.silence, key
            assert marker.track == str(track.ref), key
        for key, song in zip(DELIVERED_FORMATS, songs, strict=True):
            if key in UNINDEXED:
                continue
            assert await parts.engine.revert_to_placeholder(song), key
            back = await parts.navidrome.song(song)
            assert back is not None and back["path"].endswith(".flac") and back["albumId"] in album


async def test_a_missing_placeholder_file_is_written_anew_only_beside_shijhon_s_own_files(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """The silent file is gone, a file of its name in another format beside it: someone's
    own (no marker of this track) stays as it is, byte for byte, and the swap is refused;
    Shijhon's own file of this track (a delivered file left there) goes, and the swap is
    made."""
    release = catalog_release("pe-beside", "Beside", "Beside Artist", 2)
    delivered = tmp_path / "delivered.m4a"
    shutil.copyfile(tone(frequency_for("pe-beside"), 3, "m4a"), delivered)
    async with engine_for(navidrome, tmp_path / "state") as parts:
        result = await parts.engine.materialize(release)
        songs = [result.created[track.ref] for track in release.tracks]
        rows = [await parts.engine.placeholder_row(song) for song in songs]
        paths = [parts.layout.absolute(row["placeholder_path"]) for row in rows]

        owned = paths[0].with_suffix(".wav")
        sample = import_tone(tmp_path / "owned", {f.key: f for f in IMPORT_FORMATS}["wav-pcm"],
                             tagged=True)  # fmt: skip
        shutil.copyfile(sample, owned)
        before = owned.read_bytes()
        paths[0].unlink()
        with pytest.raises(ReplaceError, match="another file is at"):
            await parts.engine.replace_with_delivered(songs[0], delivered)
        assert owned.read_bytes() == before
        assert (await parts.engine.placeholder_row(songs[0]))["state"] == "placeholder"
        owned.unlink()

        stale = paths[1].with_suffix(".m4a")
        shutil.copyfile(delivered, stale)
        tagging.write_delivered(stale, json.loads(rows[1]["tags"]))
        assert (marker := tagging.marker_of(stale)) is not None and not marker.silence
        paths[1].unlink()
        path = await parts.engine.replace_with_delivered(songs[1], delivered)
        assert path.endswith(".m4a")
        assert (await parts.engine.placeholder_row(songs[1]))["state"] == "delivered"


def _as_listed(song: dict[str, Any]) -> dict[str, Any]:
    """What Navidrome tells of a song's identity and metadata (not of its file)."""
    roles = song.get("participants") or {}
    return {
        "id": song["id"], "albumId": song["albumId"], "title": song["title"],
        "artist": song.get("artist"), "albumArtist": song.get("albumArtist"),
        "genres": sorted(g["name"] for g in song.get("genres") or []),
        "participants": {r: sorted(p["name"] for p in roles[r]) for r in sorted(roles)},
        "track": song.get("trackNumber"), "disc": song.get("discNumber"),
    }  # fmt: skip


async def test_delivered_matroska_keeps_every_genre_and_refuses_what_it_cannot_carry(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """Matroska with the placeholder's several genres (one value Navidrome splits again):
    Navidrome lists the song as before - its ID, album, genres, artists. Several artists
    Matroska cannot carry (ffmpeg writes one value a name, Navidrome splits no artists):
    the engine refuses it rather than flatten them, the placeholder stays."""
    sample = import_tone(tmp_path / "mka", {f.key: f for f in IMPORT_FORMATS}["matroska-flac"],
                         tagged=False)  # fmt: skip
    base = catalog_release("pe-mka-genres", "Matroska Genres", "Genre Artist", 1)
    genres = replace(base, genres=("Folk", "Jazz"))
    duo = catalog_release("pe-mka-duo", "Matroska Duo", "Ann", 1)
    duo = replace(
        duo, artists=("Ann", "Bob"), genres=("Folk", "Jazz"),
        tracks=(replace(duo.tracks[0], artists=("Ann", "Bob")),),
    )  # fmt: skip
    async with engine_for(navidrome, tmp_path / "state") as parts:
        for release in (genres, duo):
            await parts.engine.materialize(release)
        row = await parts.store.fetchone(
            "SELECT * FROM placeholders WHERE track_ref = ?", [str(genres.tracks[0].ref)]
        )
        song = str(row["song_id"])
        before = await parts.navidrome.song(song)
        delivered = tmp_path / "delivered.mka"
        shutil.copyfile(sample, delivered)
        path = await parts.engine.replace_with_delivered(song, delivered)
        after = await parts.navidrome.song(song)
        assert path.endswith(".mka") and before is not None and after is not None
        assert _as_listed(after) == _as_listed(before)
        assert _as_listed(after)["genres"] == ["Folk", "Jazz"]
        marker = tagging.marker_of(parts.layout.absolute(path))
        assert marker is not None and not marker.silence
        assert await parts.engine.revert_to_placeholder(song)

        row = await parts.store.fetchone(
            "SELECT * FROM placeholders WHERE track_ref = ?", [str(duo.tracks[0].ref)]
        )
        with pytest.raises(ReplaceError, match="cannot tag"):
            await parts.engine.replace_with_delivered(str(row["song_id"]), delivered)
        row = await parts.engine.placeholder_row(str(row["song_id"]))
        assert row["state"] == "placeholder"
