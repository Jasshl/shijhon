"""Placeholder engine operations beyond the lettered suites: recovery from
marker tags, scans racing Navidrome's own watcher, silence at the exact duration and
concurrent materialization of one release."""

from __future__ import annotations

import shutil
import zlib
from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from shijhon.placeholders import layout as layout_module
from shijhon.placeholders.engine import MaterializeError, ReplaceError
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.recover import recover
from shijhon.placeholders.silence import SilenceMaker
from tests.conftest import NavidromeFactory
from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import frequency_for, tone
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
