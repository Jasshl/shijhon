"""Putting a file back after a write that did not finish (``LibraryFiles._restore``):
whether the swap happened is told by the files present, never by inode numbers (a union
may derive them from the path), and a backup goes back over the name only when its bytes
are the old file's. Invented bytes; no Navidrome needed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shijhon.artist_lists import files
from shijhon.artist_lists.files import LibraryFiles, file_digest

OLD, NEW = b"old song bytes " * 64, b"new song bytes with a list " * 64


def work_for(path: Path) -> dict[str, str]:
    size, sha = file_digest(path)
    return {
        "temp": ".shijhon-t.tmp",
        "backup": ".shijhon-t.keep",
        "part": ".shijhon-t.part",
        "size": str(size),
        "sha": sha,
    }


@pytest.fixture
def song(tmp_path: Path) -> Path:
    path = tmp_path / "Wren Hollow - Quiet Engines.flac"
    path.write_bytes(OLD)
    return path


def test_a_swap_that_happened_is_put_back(song: Path) -> None:
    work = work_for(song)
    song.with_name(work["backup"]).write_bytes(OLD)  # (a whole-copy backup)
    song.write_bytes(NEW)  # (the copy now under the name; the temp was consumed)
    assert LibraryFiles._restore(song, work) is True
    assert song.read_bytes() == OLD
    assert not song.with_name(work["backup"]).exists()


def test_a_path_derived_inode_does_not_hide_the_swap(
    song: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With inode numbers derived from the path, the new file under the name has the old
    file's number; the swap is still put back."""
    work = work_for(song)
    work["ino"] = str(os.lstat(song).st_ino)
    os.link(song, song.with_name(work["backup"]))
    song.with_name(".new").write_bytes(NEW)
    os.replace(song.with_name(".new"), song)
    real = os.lstat

    def same_number(path: object, *args: object, **kwargs: object) -> os.stat_result:
        found = real(path, *args, **kwargs)  # type: ignore[arg-type]
        if Path(str(path)) == song:
            fields = list(found)
            fields[1] = int(work["ino"])
            return os.stat_result(fields)
        return found

    monkeypatch.setattr(files.os, "lstat", same_number)
    assert LibraryFiles._restore(song, work) is True
    assert song.read_bytes() == OLD


def test_no_swap_yet_leaves_the_file_and_drops_the_backup(song: Path) -> None:
    work = work_for(song)
    song.with_name(work["temp"]).write_bytes(NEW)
    song.with_name(work["backup"]).write_bytes(OLD)
    assert LibraryFiles._restore(song, work) is False
    assert song.read_bytes() == OLD
    assert not song.with_name(work["temp"]).exists()
    assert not song.with_name(work["backup"]).exists()


def test_a_backup_that_is_not_the_old_file_is_never_moved_over_the_name(song: Path) -> None:
    work = work_for(song)
    song.with_name(work["backup"]).write_bytes(OLD[:100])  # (cut short)
    song.write_bytes(NEW)
    assert LibraryFiles._restore(song, work) is False
    assert song.read_bytes() == NEW  # (left as it is)
    assert song.with_name(work["backup"]).exists()  # (kept for a person to look at)


def test_a_copy_backup_cut_short_is_never_taken_for_the_backup(song: Path) -> None:
    work = work_for(song)
    song.with_name(work["temp"]).write_bytes(NEW)
    song.with_name(work["part"]).write_bytes(OLD[:50])  # (a stop during the whole copy)
    assert LibraryFiles._restore(song, work) is False
    assert song.read_bytes() == OLD
    assert not song.with_name(work["part"]).exists()
    assert not song.with_name(work["temp"]).exists()


def test_the_whole_copy_is_named_the_backup_only_when_complete(song: Path) -> None:
    part, backup = song.with_name(".p"), song.with_name(".k")
    files._copy_backup(song, part, backup, os.lstat(song))
    assert backup.read_bytes() == OLD and not part.exists()
