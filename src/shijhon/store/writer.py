"""One process writes at a time.

The running service holds an advisory lock on a file beside Shijhon's database for as long
as it runs; a command that writes the library (``shijhon fills-undo --apply``) takes the
same lock, and refuses while it is held. Two writers would not see each other's locks or
pending records (the engine keeps them in memory), so a removal by one could run under a
stream, a download or a fill of the other. ``flock`` is released by the kernel when its
process ends, however it ends, and it holds between processes on one machine, and
between containers on a Linux host that share the folder. It does not hold where the
folder reaches the containers through a file-sharing layer that keeps locks apart -
Docker Desktop on macOS and Windows (measured: two containers both get the lock), a
network file system between machines; Shijhon's database does not belong on those
either, and there only the guide's order (stop the service, then the command) keeps the
writers apart. The lock is
that of the database file itself, whatever name it was reached by. Without the lock
nothing starts: a folder that cannot hold one is an error, not a warning.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

_HELD = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}


class AlreadyRunning(RuntimeError):
    """Another process holds the lock: a running Shijhon, or a command that writes."""


class WriterLock:
    def __init__(self, database: Path) -> None:
        target = database.resolve()  # (a link to the database is the same database)
        self.path = target.with_name(target.name + ".lock")
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock, or raise :class:`AlreadyRunning` when another process has it
        (never waits); ``OSError`` when the folder cannot hold such a lock (the caller
        must not go on without it)."""
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in _HELD:
                raise AlreadyRunning(
                    f"another Shijhon process is using {self.path.parent} (its lock,"
                    f" {self.path.name}, is held)"
                ) from None
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)  # (closing it releases the lock)

    async def aclose(self) -> None:
        self.release()
