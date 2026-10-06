"""Durability of library writes: a file and the directory entries naming it reach the
disk before Shijhon's database records them, and before the backup that could replace them
goes. Without it a power loss (or a kernel crash) shortly after a write could leave a
recorded placeholder or delivered file empty, or missing, while its backup is gone:
SQLite's commit is on disk, the file's data may not be (ext4 flushes a file renamed to a
new name only at its next commit, some seconds later).

Only where a crash would otherwise lose a recorded file or its backup: the staged file
before it moves into the library, the directories of a rename or an unlink before the row
that depends on them is written. Measured on a Mac's SSD: well under a millisecond each,
a few per placeholder; the targeted scan that follows takes hundreds.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

# Filesystems that cannot sync a directory (some network and FUSE file systems) say so:
# the file's own sync still happened, and there is nothing more to do.
_UNSUPPORTED = {errno.EINVAL, errno.ENOTSUP, errno.EBADF}


def sync_file(path: Path) -> None:
    """The file's data and metadata on disk."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_dir(path: Path) -> None:
    """The directory's entries (renames, new and removed files) on disk; nothing when the
    directory is gone or its file system cannot sync a directory."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except FileNotFoundError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def sync_dirs(*paths: Path) -> None:
    for path in dict.fromkeys(paths):
        sync_dir(path)


def made_dirs(target: Path) -> list[Path]:
    """The directories ``target.mkdir(parents=True)`` would create, the outermost first."""
    missing = []
    while not target.exists():
        missing.append(target)
        if target.parent == target:
            break
        target = target.parent
    return missing[::-1]
