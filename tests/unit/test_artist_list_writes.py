"""Library files given their artist lists (``artist_lists/files.py``, ``repair.py``) with a
stand-in for Navidrome that reads the files only when a scan is asked for: a stop while
files are put back, and an album split by a hard stop."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import pytest
from mutagen.flac import FLAC

from shijhon.artist_lists.files import LibraryFiles
from shijhon.artist_lists.repair import ArtistLists, Candidate, Summary
from shijhon.artist_lists.sources import Resolution, Subject
from shijhon.placeholders.silence import silent_flac
from shijhon.store import Store

pytestmark = pytest.mark.anyio

JOINED = "Ash Vane, Bell Orrin"
NAMES = ["Ash Vane", "Bell Orrin"]


def audio(path: Path, title: str, number: int) -> None:
    path.write_bytes(silent_flac(44100))
    tags = FLAC(path)
    tags.update(title=[title], artist=[JOINED], album=["Night"], tracknumber=[str(number)])
    tags.save()


class Library:
    """Navidrome's songs of the files, as last read by a scan."""

    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths, self.rows = paths, {}
        self.scans = 0
        self.refresh()

    def refresh(self) -> None:
        for ident, path in self.paths.items():
            tags = FLAC(path)
            self.rows[ident] = {
                "id": ident,
                "path": path.name,
                "size": path.stat().st_size,
                "albumId": "a1",
                "albumArtist": JOINED,
                "artist": JOINED,
                "participants": {
                    "artist": [{"name": n} for n in tags.get("artists", [JOINED])],
                    "albumartist": [{"name": n} for n in tags.get("albumartists", [JOINED])],
                },
            }

    async def song(self, key: str) -> dict[str, Any] | None:
        return self.rows.get(key)

    async def songs_of_album(self, album_id: str) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["albumId"] == album_id]

    async def until(self, folders: Any, ready: Any, **kwargs: Any) -> bool:
        self.scans += 1
        self.refresh()
        return bool(await ready())

    async def scan(self, folders: Any) -> None:
        self.scans += 1
        self.refresh()


def candidate(nd: Library, key: str) -> Candidate:
    return Candidate(
        nd.rows[key],
        Subject(key, JOINED),
        "file",
        key,
        nd.paths[key].name,
        "a1",
        JOINED,
        Resolution("resolved", tuple(NAMES), "link"),
    )


async def world(
    folder: Path, count: int, *, fresh: bool = True
) -> tuple[dict[str, Path], Store, Library, LibraryFiles, ArtistLists]:
    paths = {f"s{i}": folder / f"s{i}.flac" for i in range(count)}
    if fresh:
        for i, path in enumerate(paths.values()):
            audio(path, f"Night {i}", i + 1)
    store = await Store.open(folder / "state.db")
    nd = Library(paths)
    files = LibraryFiles(folder, store, nd, nd)  # type: ignore[arg-type]
    worker = ArtistLists(
        nd,  # type: ignore[arg-type]
        store,
        None,  # type: ignore[arg-type]
        files,
        placeholder_folder="_shijhon",
        library=True,
        report=False,
    )
    return paths, store, nd, files, worker


async def test_a_file_put_back_by_a_stop_is_read_again_at_the_next_recovery(
    tmp_path: Path,
) -> None:
    paths, store, nd, files, worker = await world(tmp_path, 1)
    try:
        with anyio.CancelScope() as scope:

            async def stop(step: str) -> None:
                if step == "write verified":  # (Navidrome has read the copy)
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

            files.step = stop
            await worker._write_album([candidate(nd, "s0")], NAMES, Summary())
        files.step = None
        assert "artists" not in FLAC(paths["s0"])  # (the file as it was)
        assert nd.rows["s0"]["participants"]["artist"] == [{"name": n} for n in NAMES]
        row = await store.fetchone("SELECT phase, work FROM artist_lists")
        assert row["phase"] == "write" and '"reread"' in row["work"]  # (a scan owed)
        assert await files.recover() == 1
        assert nd.rows["s0"]["participants"]["artist"] == [{"name": JOINED}]
        row = await store.fetchone("SELECT phase, work FROM artist_lists")
        assert (row["phase"], row["work"]) == ("", None)
    finally:
        await store.close()


async def test_a_recovery_whose_scan_fails_keeps_it_owed(tmp_path: Path) -> None:
    paths, store, nd, files, worker = await world(tmp_path, 1)
    try:
        with anyio.CancelScope() as scope:

            async def stop(step: str) -> None:
                if step == "write swapped":
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

            async def killed(one: Any, **kwargs: Any) -> None:
                pass  # (a hard stop: nothing is put back)

            files.step = stop
            files._restore_one = killed  # type: ignore[method-assign]
            await worker._write_album([candidate(nd, "s0")], NAMES, Summary())
        files.step = None
        del files._restore_one
        assert FLAC(paths["s0"])["artists"] == NAMES  # (the copy under the name)
        nd.refresh()  # (Navidrome read the copy meanwhile on its own)
        scan = nd.scan

        async def down(folders: Any) -> None:
            raise RuntimeError("scanner down")

        nd.scan = down  # type: ignore[method-assign]
        assert await files.recover() == 0
        row = await store.fetchone("SELECT phase, work FROM artist_lists")
        assert row["phase"] == "write" and '"reread"' in row["work"]
        nd.scan = scan  # type: ignore[method-assign]
        assert await files.recover() == 1
        assert nd.rows["s0"]["participants"]["artist"] == [{"name": JOINED}]
        assert (await store.fetchone("SELECT phase FROM artist_lists"))["phase"] == ""
    finally:
        await store.close()


class Stop(BaseException):
    """A hard stop at a chosen point (nothing after it runs)."""


async def test_a_recovery_stopped_right_after_its_put_back_still_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, store, nd, files, worker = await world(tmp_path, 1)
    try:
        c = candidate(nd, "s0")
        await worker._note(c, "failed", reason="interrupted")
        await files._prepare(worker._entry(c, NAMES))  # (a stop left the copy in place)
        nd.refresh()  # (Navidrome read the copy)
        restore = LibraryFiles._restore

        def stopped(path: Path, work: Any) -> bool:
            restore(path, work)
            raise Stop()  # (right after the old file is back)

        monkeypatch.setattr(files, "_restore", stopped)
        with pytest.raises(Stop):
            await files.recover()
        monkeypatch.setattr(files, "_restore", restore)
        assert "artists" not in FLAC(paths["s0"])
        scans = nd.scans
        assert await files.recover() == 1
        assert nd.scans == scans + 1
        assert nd.rows["s0"]["participants"]["artist"] == [{"name": JOINED}]
        assert (await store.fetchone("SELECT phase FROM artist_lists"))["phase"] == ""
    finally:
        await store.close()


@pytest.mark.parametrize("where", ["unshown", "failed"])
async def test_a_put_back_whose_scan_is_cut_short_stays_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """(A file Navidrome does not show as it should, or a write failing after the swap:
    put back, then the scan that would read it again is cut short by a stop.)"""
    from dataclasses import replace

    from shijhon.artist_lists.files import ListError

    paths, store, nd, files, worker = await world(tmp_path, 1)
    try:
        c = candidate(nd, "s0")
        entry = worker._entry(c, NAMES)
        if where == "unshown":
            entry = replace(entry, settled=lambda song, size: False)
        else:

            async def fail(step: str) -> None:
                if step == "write swapped":
                    nd.refresh()  # (Navidrome read the copy)
                    raise ListError("failed")

            files.step = fail
        await worker._note(c, "failed", reason="interrupted")
        with anyio.CancelScope() as scope:

            async def cut(restored: Any) -> None:
                scope.cancel()
                await anyio.lowlevel.checkpoint()

            monkeypatch.setattr(files, "_reread", cut)
            await files.write_many([entry])
        files.step = None
        monkeypatch.undo()
        assert "artists" not in FLAC(paths["s0"])
        row = await store.fetchone("SELECT phase, work FROM artist_lists")
        assert row["phase"] == "write" and '"reread"' in row["work"]
        assert await files.recover() == 1
        assert nd.rows["s0"]["participants"]["artist"] == [{"name": JOINED}]
        assert (await store.fetchone("SELECT phase FROM artist_lists"))["phase"] == ""
    finally:
        await store.close()


async def test_an_album_a_hard_stop_left_part_listed_is_finished_by_the_next_pass(
    tmp_path: Path,
) -> None:
    killed = await anyio.run_process(
        [sys.executable, __file__, str(tmp_path)], check=False, cwd=Path(__file__).parents[2]
    )
    assert killed.returncode == 73, killed.stderr  # (after the first file's commit)
    paths, store, nd, files, worker = await world(tmp_path, 2, fresh=False)
    try:
        assert await files.recover() >= 1
        listed = [tuple(FLAC(p).get("albumartists", [])) for p in paths.values()]
        assert listed == [tuple(NAMES), ()]  # (split: the second file put back)
        # The next pass: only the second is still credited to the joined artist.
        nd.refresh()
        rest = [candidate(nd, "s1")]
        album = await worker._album_list("a1", rest, rest)
        assert album == NAMES
        await worker._write_album(rest, album, Summary())
    finally:
        await store.close()
    assert [FLAC(p).get("albumartists") for p in paths.values()] == [NAMES, NAMES]


async def test_an_album_list_is_not_given_when_a_listed_song_lacks_it(tmp_path: Path) -> None:
    _, store, nd, _, worker = await world(tmp_path, 2)
    try:
        await worker._write_album([candidate(nd, "s0")], None, Summary())  # (no album list)
        nd.refresh()
        rest = [candidate(nd, "s1")]
        assert await worker._album_list("a1", rest, rest) is None
    finally:
        await store.close()


async def _killed_mid_album(folder: Path) -> None:
    _, _, nd, files, worker = await world(folder, 2)

    async def die(step: str) -> None:
        if step == "write committed":
            os._exit(73)

    files.step = die
    await worker._write_album([candidate(nd, "s0"), candidate(nd, "s1")], NAMES, Summary())


if __name__ == "__main__":
    anyio.run(_killed_mid_album, Path(sys.argv[1]))
