"""Checked copies of Shijhon's database: the snapshot startup takes before it upgrades the
schema, and ``shijhon backup``.

A copy is made with SQLite's online backup, also while Shijhon runs: it is the database as
of one commit, never a file copy that could catch a write half done. It is written under a
temporary name beside where it goes, checked with SQLite's integrity check, synced to disk
and only then given its own name, so a copy under its own name is always a whole one. What
a stop leaves under a temporary name is removed by the next copy into that folder - only
names in Shijhon's own temporary format, nothing else in the folder. The copy only reads
the database's data: nothing is written into it. (A connection on a database in WAL mode
may move committed pages from the WAL into the main file when it closes, as any connection
does: the file's bytes can change then, what the database holds does not.)

Snapshots go into ``snapshots/`` beside the database - the state folder, so the Docker
volume or the NixOS service's state directory alike - named for the schema they hold and
numbered up (``shijhon-schema37-20261112T153000Z-4.sqlite3``). The number, not the time,
orders snapshots of one schema: a clock set back cannot make a later snapshot look older.
A new snapshot is in place before any older one goes; then the newest snapshot of its
schema and of the highest other schema that has one are kept, and the rest removed. A copy
holds the secrets the database holds (settings saved on the dashboard): it is written
readable by its owner only, like the database.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shijhon import __version__
from shijhon.placeholders import durable

log = logging.getLogger(__name__)

SNAPSHOTS = "snapshots"
BACKUPS = "backups"
# Free space a copy leaves, beyond its own size: the running service, the migration that
# follows a snapshot and SQLite's journal need room too.
MARGIN = 64 * 2**20
SNAPSHOT_NAME = re.compile(r"^shijhon-schema(\d+)-(\d{8}T\d{6}Z)-(\d+)\.sqlite3$")
# What a stop leaves while a copy is written: exactly these names, nothing else is touched.
# A snapshot's temporary file (with SQLite's journal of it), and a backup's folder.
_SNAPSHOT_PARTIAL = re.compile(
    r"^\.shijhon-schema\d+-\d{8}T\d{6}Z-\d+\.sqlite3\.partial(-journal)?$"
)
_BACKUP_PARTIAL = re.compile(r"^\.shijhon-backup-\d{8}T\d{6}Z(-\d+)?\.partial$")


class CopyError(RuntimeError):
    """The copy was not made; the message says why (paths and sizes, nothing secret).
    Nothing of it is left under its own name, and nothing was written into the database.

    ``kind`` says why in a word a record can keep: ``no_room`` (with the bytes ``needed``
    and ``free``), ``unwritable`` (a folder or the copy could not be written; ``code``: the
    system's error number when there is one), ``damaged`` (the database fails SQLite's
    integrity check), ``unreadable`` (the database could not be read), ``copy_check`` (the
    copy failed the check, the database did not), ``exists`` (a file has the copy's name),
    ``error`` (another error; ``code`` as for ``unwritable``)."""

    def __init__(
        self,
        message: str,
        kind: str = "error",
        *,
        code: int | None = None,
        needed: int | None = None,
        free: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.needed = needed
        self.free = free


# The system's error numbers of a full disk or a used-up quota.
_FULL = (errno.ENOSPC, errno.EDQUOT)


@dataclass(frozen=True)
class Copy:
    path: Path
    schema: int
    size: int  # bytes


def stamp(now: float) -> str:
    """A UTC time in a file name: the same in every time zone."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def megabytes(size: int) -> str:
    return f"{size / 2**20:.1f} MB"


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def schema_of(database: Path) -> int:
    """The schema version of an existing database (``PRAGMA user_version``), read without
    writing into it; ``sqlite3.Error`` when it cannot be read."""
    conn = sqlite3.connect(database, isolation_level=None)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def make_dirs(folder: Path) -> None:
    """``folder`` and its missing parents, readable by their owner only, each one's entry
    on disk (its parent synced) before anything is put into it."""
    for made in durable.made_dirs(folder):
        made.mkdir(mode=0o700, exist_ok=True)
        durable.sync_dir(made.parent)


def _clear(folder: Path, pattern: re.Pattern[str], *, folders: bool) -> None:
    """Remove what stopped copies left in ``folder``: entries named in Shijhon's own
    temporary format ``pattern`` (folders, or files), nothing else. The caller makes sure
    no copy into the folder is under way."""
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return
    for entry in entries:
        if not pattern.match(entry.name) or entry.is_symlink():
            continue
        if folders and entry.is_dir():
            shutil.rmtree(entry)
        elif not folders and entry.is_file():
            entry.unlink(missing_ok=True)


def _writable(database: Path) -> None:
    """A database in WAL mode is read through files SQLite makes beside it (its WAL index
    and WAL): without them no consistent read of a database that may be written, so the
    folder must be writable for whoever copies it."""
    if not os.access(database.parent, os.W_OK):
        raise CopyError(
            f"the database's folder {database.parent} must be writable for the user running"
            " this: SQLite makes the files it needs to read a database in WAL mode"
            " consistently beside it",
            "unwritable",
            code=errno.EACCES,
        )


def copy_database(
    source: Path,
    target: Path,
    *,
    margin: int = MARGIN,
    free: Callable[[Path], int] | None = None,
    checked: Callable[[Path], None] | None = None,
) -> Copy:
    """Write a checked copy of the database ``source`` as the new file ``target`` (in an
    existing folder; :class:`CopyError` when ``target`` exists - nothing is replaced).
    ``free``: the free bytes of a folder (tests); ``checked``: called with the temporary
    file once it passed the check, before it gets its name (tests stop there)."""
    folder = target.parent
    partial = target.with_name(f".{target.name}.partial")
    journal = partial.with_name(partial.name + "-journal")
    _writable(source)
    try:
        conn = sqlite3.connect(source, isolation_level=None)
    except sqlite3.Error as exc:
        raise CopyError(f"the database {source} cannot be opened ({exc})", "unreadable") from None
    try:
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        except sqlite3.Error as exc:
            raise CopyError(f"the database {source} cannot be read ({exc})", "unreadable") from None
        size = int(pages) * int(page_size)
        try:
            available = free(folder) if free is not None else shutil.disk_usage(folder).free
        except OSError as exc:
            raise CopyError(f"{folder}: {_reason(exc)}", code=exc.errno) from None

        def no_room(said: str) -> CopyError:
            return CopyError(
                f"not enough free space in {folder}: the copy needs {megabytes(size)} and"
                f" {megabytes(margin)} to spare, {said}",
                "no_room",
                needed=size + margin,
                free=available,
            )

        if available < size + margin:
            raise no_room(f"{megabytes(available)} is free")
        try:
            for leftover in (partial, journal):
                leftover.unlink(missing_ok=True)
            os.close(os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except OSError as exc:
            raise _unwritable(folder, exc) from None
        try:
            schema = _write(conn, source, partial, no_room)
            if checked is not None:
                checked(partial)
            durable.sync_file(partial)
            _name(partial, target)
            durable.sync_dir(folder)
        except BaseException as exc:
            for leftover in (partial, journal):
                with contextlib.suppress(OSError):  # (the first failure is the one said)
                    leftover.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                if exc.errno in _FULL:
                    raise no_room(f"and the disk filled up ({_reason(exc)})") from None
                raise _unwritable(folder, exc) from None
            raise
    finally:
        conn.close()
    return Copy(target, schema, target.stat().st_size)


def _unwritable(folder: Path, exc: OSError) -> CopyError:
    return CopyError(f"{folder} cannot be written ({_reason(exc)})", "unwritable", code=exc.errno)


def _name(partial: Path, target: Path) -> None:
    """Give the checked copy its own name, never over another file: a hard link fails
    where one exists. Where the file system has no hard links, a rename once the name is
    seen to be free (callers hold a lock that keeps other copies out of the folder)."""
    try:
        os.link(partial, target)
    except FileExistsError:
        raise CopyError(f"{target} exists already: nothing was replaced", "exists") from None
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
            raise
        if os.path.lexists(target):  # (a link that leads nowhere is a name taken too)
            raise CopyError(f"{target} exists already: nothing was replaced", "exists") from None
        os.rename(partial, target)
        return
    partial.unlink()


def _write(
    conn: sqlite3.Connection,
    source: Path,
    partial: Path,
    no_room: Callable[[str], CopyError],
) -> int:
    """The online backup of ``conn`` into ``partial``, as one file (no WAL beside it),
    checked; its schema version."""
    try:
        copy = sqlite3.connect(partial, isolation_level=None)
    except sqlite3.Error as exc:
        raise CopyError(f"the copy cannot be written ({exc})", "unwritable") from None
    try:
        try:
            conn.backup(copy)
        except sqlite3.Error as exc:
            reason = str(exc)
            if "readonly" in reason.lower():
                _writable(source)  # (says why, when it is the folder)
            if "full" in reason.lower():
                raise no_room(f"and the disk filled up ({reason})") from None
            if "disk i/o" in reason.lower():
                raise CopyError(f"the copy could not be written ({reason})", "unwritable") from None
            raise CopyError(
                f"the database {source} could not be read ({reason})", "unreadable"
            ) from None
        try:
            # A file of its own, whatever mode the database is in (Shijhon's is in WAL mode,
            # and puts it back when it opens a restored copy).
            copy.execute("PRAGMA journal_mode = DELETE")
            found = [row[0] for row in copy.execute("PRAGMA integrity_check").fetchall()]
            schema = int(copy.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as exc:
            found, schema = [str(exc)], -1
    finally:
        copy.close()
    if found != ["ok"]:
        if not _intact(conn):
            raise CopyError(
                f"the database {source} fails SQLite's integrity check ({found[0]}): it is"
                " damaged, and a copy of it would be too",
                "damaged",
            )
        raise CopyError(f"the copy failed SQLite's integrity check ({found[0]})", "copy_check")
    return schema


def _intact(conn: sqlite3.Connection) -> bool:
    try:
        return [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()] == ["ok"]
    except sqlite3.Error:
        return False


@dataclass(frozen=True)
class Snapshot:
    path: Path
    schema: int
    number: int


def snapshots(folder: Path) -> list[Snapshot]:
    """The snapshots in ``folder``, by schema, then by number (the oldest first)."""
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return []
    found = [
        Snapshot(entry, int(match.group(1)), int(match.group(3)))
        for entry in entries
        if (match := SNAPSHOT_NAME.match(entry.name)) and entry.is_file()
    ]
    return sorted(found, key=lambda s: (s.schema, s.number))


def kept(found: list[Snapshot], new: Snapshot) -> set[Path]:
    """What stays once ``new`` is in place: the newest snapshot of its schema (``new``) and
    the newest of the highest other schema that has one."""
    others = [s for s in found if s.schema != new.schema]
    stay = {new.path}
    if others:
        top = max(s.schema for s in others)
        stay.add(max((s for s in others if s.schema == top), key=lambda s: s.number).path)
    return stay


def snapshot_before_upgrade(
    database: Path,
    latest: int,
    *,
    now: Callable[[], float] = time.time,
    free: Callable[[Path], int] | None = None,
    checked: Callable[[Path], None] | None = None,
) -> Copy | None:
    """Before migrations bring ``database`` to schema ``latest``: a checked snapshot of it
    in ``snapshots/`` beside it, when it is an existing database at an older schema (a new
    or empty one, at schema 0, needs none; a newer one is refused by the migrations). Once
    it is in place, older snapshots go (``kept``). Raises :class:`CopyError` when the
    snapshot cannot be written - then no snapshot was removed, and nothing of it is left.
    Only one process may run it on a database at a time (the service's start, under its
    writer lock)."""
    if not database.is_file():
        return None
    try:
        schema = schema_of(database)
    except sqlite3.Error as exc:
        raise CopyError(f"the database {database} cannot be read ({exc})", "unreadable") from None
    if schema == 0 or schema >= latest:
        return None
    folder = database.parent / SNAPSHOTS
    try:
        make_dirs(folder)
        _clear(folder, _SNAPSHOT_PARTIAL, folders=False)
    except OSError as exc:
        raise _unwritable(folder, exc) from None
    found = snapshots(folder)
    number = max((s.number for s in found), default=0) + 1
    while True:
        target = folder / f"shijhon-schema{schema}-{stamp(now())}-{number}.sqlite3"
        if not target.exists() and not target.with_name(f".{target.name}.partial").exists():
            break
        number += 1
    try:
        made = copy_database(database, target, free=free, checked=checked)
    except CopyError as exc:
        if exc.kind != "no_room":
            raise
        raise CopyError(
            f"{exc}; no snapshot was removed to make room - remove an older snapshot from"
            f" {folder} by hand if it is no longer needed, or free space elsewhere",
            exc.kind,
            needed=exc.needed,
            free=exc.free,
        ) from None
    log.info(
        "startup: database snapshot before the upgrade from schema %d to %d: %s (%s, checked)",
        schema,
        latest,
        made.path,
        megabytes(made.size),
    )
    stay = kept(found, Snapshot(made.path, schema, number))
    for old in found:
        if old.path not in stay:
            try:
                old.path.unlink(missing_ok=True)
            except OSError as exc:  # (the new snapshot is in place: the upgrade may go on)
                log.warning(
                    "startup: an earlier database snapshot could not be removed: %s (%s)",
                    old.path.name,
                    _reason(exc),
                )
            else:
                log.info("startup: an earlier database snapshot removed: %s", old.path.name)
    durable.sync_dir(folder)
    return made


# ``shijhon backup``: a folder of its own for each backup, with these files.
DATABASE = "shijhon.sqlite3"
CONFIGURATION = "shijhon.toml"
SERVICE_ACCOUNT = "navidrome-password"
README = "README.txt"
NOT_COVERED = (
    "Navidrome's database",
    "the music folder with the placeholder folder",
    "the music imports folder",
    "settings given in the environment or an environment file",
    "caches Shijhon fills again by itself (catalog covers, DASH audio, downloads under way,"
    " the collection's query index)",
)


@dataclass(frozen=True)
class Backup:
    path: Path
    database: Copy
    # (file name, what it is) of the files beside the database, and what was left out.
    files: tuple[tuple[str, str], ...]
    left_out: tuple[str, ...]


def make_backup(
    database: Path,
    folder: Path,
    *,
    configuration: Path | None = None,
    password_file: Path | None = None,
    now: Callable[[], float] = time.time,
    free: Callable[[Path], int] | None = None,
) -> Backup:
    """``shijhon backup``: a folder ``shijhon-backup-<UTC time>`` in ``folder`` with a
    checked copy of ``database``, the configuration file and the service account's
    password file when given, and a README that says what it holds and what not. Written
    as ``.<name>.partial`` and renamed when complete; readable by its owner only.
    :class:`CopyError` when it cannot be made, also when a file it is to hold cannot be
    read (nothing of it is left)."""
    if not database.is_file():
        raise CopyError(f"no database at {database}")
    files: list[tuple[str, str, bytes]] = []  # (name, what, contents), read first
    for source, name, what in (
        (configuration, CONFIGURATION, "the configuration file"),
        (password_file, SERVICE_ACCOUNT, "the service account's password file"),
    ):
        if source is None:
            continue
        try:
            files.append((name, f"{what} ({source})", source.read_bytes()))
        except OSError as exc:
            raise CopyError(f"{what} {source} cannot be read ({_reason(exc)})") from None
    left_out = () if configuration else ("the configuration file (none was given with --config)",)
    _writable(database)
    try:
        make_dirs(folder)
        lock = os.open(folder / ".backup.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise CopyError(f"{folder} cannot be written ({_reason(exc)})") from None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise CopyError(f"another backup is being written into {folder}") from None
            raise CopyError(f"{folder} cannot hold a lock ({_reason(exc)})") from None
        return _backup(database, folder, files, left_out, now(), free)
    finally:
        os.close(lock)


def _backup(
    database: Path,
    folder: Path,
    files: list[tuple[str, str, bytes]],
    left_out: tuple[str, ...],
    at: float,
    free: Callable[[Path], int] | None,
) -> Backup:
    try:
        _clear(folder, _BACKUP_PARTIAL, folders=True)
    except OSError as exc:
        raise CopyError(f"{folder}: {_reason(exc)}") from None
    name = f"shijhon-backup-{stamp(at)}"
    taken = 1
    while (folder / name).exists() or (folder / f".{name}.partial").exists():
        taken += 1
        name = f"shijhon-backup-{stamp(at)}-{taken}"
    final, partial = folder / name, folder / f".{name}.partial"
    try:
        partial.mkdir(mode=0o700)
    except OSError as exc:
        raise CopyError(f"{folder} cannot be written ({_reason(exc)})") from None
    try:
        copied = copy_database(database, partial / DATABASE, free=free)
        for file_name, _, data in files:
            _write_private(partial / file_name, data)
        held = [(file_name, what) for file_name, what, _ in files]
        _write_private(partial / README, _readme(copied, held, left_out, at).encode())
        durable.sync_dir(partial)
        os.rename(partial, final)  # (final did not exist: under the lock, checked above)
        durable.sync_dir(folder)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            shutil.rmtree(partial)
        if isinstance(exc, OSError):
            raise CopyError(f"the backup could not be written ({_reason(exc)})") from None
        raise
    return Backup(final, Copy(final / DATABASE, copied.schema, copied.size), tuple(held), left_out)


def _write_private(target: Path, data: bytes) -> None:
    """A new file readable by its owner only, on disk before it counts."""
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def _readme(
    copied: Copy, files: list[tuple[str, str]], left_out: tuple[str, ...], at: float
) -> str:
    made = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(at))
    lines = [
        f"A backup of Shijhon's own state, made by Shijhon {__version__} on {made}.",
        "",
        f"{DATABASE}: Shijhon's database at schema {copied.schema}, a consistent copy made"
        " with SQLite's online backup and checked with its integrity check. It holds the"
        " settings saved on the dashboard, secrets included.",
    ]
    lines += [f"{name}: {what}." for name, what in files]
    lines += ["", "Not in it:"]
    lines += [f"- {item}" for item in (*left_out, *NOT_COVERED)]
    lines += [
        "",
        "Keep it private: it holds secrets.",
        "",
        "To restore it, stop Shijhon. Copy this shijhon.sqlite3 into the state folder"
        " under a temporary name, such as restored.sqlite3. Only once that copy is"
        " complete, remove shijhon.sqlite3-wal and shijhon.sqlite3-shm there (they belong"
        " to the database being replaced) and rename restored.sqlite3 to shijhon.sqlite3."
        " Restore what is not in it (above) from the same time too, and only then start"
        " this Shijhon version or a later one"
        ' (docs/reference/advanced-deployment.md, "Restoring a backup").',
    ]
    return "\n".join(lines) + "\n"
