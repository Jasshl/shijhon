"""The usage export: what the cleanup needs from Navidrome's database, and nothing else.

Navidrome's database also holds what Shijhon has no business reading: the secrets its
sessions and share links are signed with, every user's password (encrypted with a key
that is public unless one is set), the keys of linked scrobblers, plugin settings. So that
Shijhon's own container - the one that faces the network and parses what add-ons and
catalogs send - never sees that file, ``shijhon usage-export`` runs beside it, without
a network: it reads Navidrome's database read-only, in one read transaction, and writes a
small SQLite file with only the tables and columns the checks read (``COPIED``, under
their own names: the checks' queries run on either) - no users, no user IDs, no secrets,
no share IDs, no playlist names. It is written beside its place and renamed into it, so
a reader has a whole export or the one before.

Each export says what it is (``META``): when its snapshot was taken (stamped before the
read began), which request of Shijhon's it answers (the request file's content as the
exporter read it before it began: a causal acknowledgment, good whatever the clocks do),
and which database file it was read from (its device and inode: a removal's two looks must
come from the same one). The cleanup acts only on an export made after the moment it asks
about (``UsageSource``, ``usage.py``): Shijhon writes a request to ``<export>.request``,
which the exporter looks at every second, and waits for the export that answers it.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import signal
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

log = logging.getLogger(__name__)

FORMAT = 1
POLL_SECONDS = 1.0  # how often the request file is looked at
RETRY_SECONDS = 30.0  # a failed export is tried again after this (or in its turn, if sooner)
REMIND_SECONDS = 3600.0  # a failure that lasts is logged again after this
# A database file written less than this ago is not read as it is (file systems keep
# modification times to 2 s at the coarsest).
SETTLED_NS = 2_500_000_000
META = "shijhon_usage_export"  # one row: what the export says of itself (``Snapshot``)
# Table -> its columns, and whether a Navidrome without it cannot be exported (the checks
# read ``scrobbles`` only where there is one).
COPIED: dict[str, tuple[tuple[str, ...], bool]] = {
    "annotation": (
        (
            "item_id",
            "item_type",
            "starred",
            "starred_at",
            "rating",
            "rated_at",
            "play_count",
            "play_date",
        ),
        True,
    ),
    "playlist_tracks": (("playlist_id", "media_file_id"), True),
    "bookmark": (("item_id",), True),
    "playqueue": (("items",), True),
    "share": (("resource_ids",), True),
    "scrobbles": (("media_file_id",), False),
    "media_file": (
        ("id", "path", "missing", "album_id", "artist_id", "album_artist_id"),
        True,
    ),
    "album": (("id", "name", "album_artist", "album_artist_id"), True),
}
# What the checks look rows up by (Navidrome's own indexes are not copied).
INDEXED: dict[str, tuple[str, ...]] = {
    "annotation": ("item_id",),
    "playlist_tracks": ("media_file_id",),
    "bookmark": ("item_id",),
    "scrobbles": ("media_file_id",),
    "media_file": ("id", "album_id"),
    "album": ("id",),
}


class ExportError(RuntimeError):
    """The export was not made; the one before stays as it is."""


class NotNavidrome(ExportError):
    """What was to be exported is not a Navidrome database (this version can read)."""


@dataclass(frozen=True)
class Snapshot:
    """What an export says of itself."""

    at: float  # stamped before its read began: what happened before then is in it
    answers: str = ""  # the request it answers (Shijhon's, as read before it began)
    source: str = ""  # the database file it was read from (device:inode)


def request_path(export: Path) -> Path:
    """The file Shijhon writes to ask for an export now."""
    return export.with_name(export.name + ".request")


def lock_path(export: Path) -> Path:
    return export.with_name(export.name + ".lock")


def read_meta(conn: sqlite3.Connection) -> Snapshot | None:
    """An export's own account of itself; None for a database that is not an export.
    Raises :class:`ExportError` for one of another format."""
    try:
        row = conn.execute(
            f"SELECT format, snapshot_at, answers, source FROM {META}"  # noqa: S608
        ).fetchone()
    except sqlite3.OperationalError:
        try:  # (its table, with other columns: another version's)
            conn.execute(f"SELECT 1 FROM {META}").fetchone()  # noqa: S608
        except sqlite3.OperationalError:
            return None
        raise ExportError("the usage export was made by another version of Shijhon") from None
    if row is None or int(row[0]) != FORMAT:
        raise ExportError("the usage export was made by another version of Shijhon")
    return Snapshot(float(row[1]), str(row[2] or ""), str(row[3] or ""))


@contextmanager
def writing(out: Path) -> Iterator[None]:
    """One exporter writes a given export at a time: an advisory lock beside it (a service
    and a timer set up for the same file). Where locks do not reach between containers
    (Docker Desktop's file sharing) each still stages under a name of its own and renames
    a finished file into place: an export read is whole either way."""
    try:
        fd = os.open(lock_path(out), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ExportError(f"the export cannot be written ({exc.strerror or exc})") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise ExportError(f"another usage-export is writing {out}") from None
            raise ExportError(f"{out.parent} cannot hold a lock ({exc.strerror or exc})") from None
        yield
    finally:
        os.close(fd)


def export(
    navidrome_db: Path,
    out: Path,
    *,
    mode: int = 0o640,
    answers: str | None = None,
    locked: bool = False,
) -> Snapshot:
    """Write the usage export of ``navidrome_db`` to ``out`` and return what it says of
    itself. Navidrome's database is opened read-only and read in one transaction: every
    table as it was at one moment, not earlier than the time stamped. ``answers``: the
    request it answers (default: the request file's content now, read before anything
    else). ``locked``: the caller holds ``writing(out)``. Raises :class:`ExportError`
    (the export before, if any, stays)."""
    if locked:
        return _export(navidrome_db, out, mode, answers)
    with writing(out):
        return _export(navidrome_db, out, mode, answers)


def _export(navidrome_db: Path, out: Path, mode: int, answers: str | None) -> Snapshot:
    if not navidrome_db.is_file():
        raise ExportError(f"no database at {navidrome_db}")
    if answers is None:  # (before the read begins: this export is made after that request)
        answers = _request(out)
    _never_over(navidrome_db, out)
    # Stamped before the snapshot is taken (the transaction's first read): whatever
    # happened in Navidrome before this time is in the export. The file it names must be
    # the one read: looked at again once it is read (a database put in its place between
    # the two would be another one under this one's name).
    taken = Snapshot(time.time(), answers, _identity(navidrome_db))
    source, unchanged = _open(navidrome_db)
    target: sqlite3.Connection | None = None
    staged: Path | None = None
    try:
        if source.execute("SELECT 1 FROM sqlite_master WHERE name = ?", [META]).fetchone():
            raise NotNavidrome(f"{navidrome_db} is a usage export, not Navidrome's database")
        handle, name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
        os.close(handle)
        staged = Path(name)
        staged.chmod(mode)
        target = sqlite3.connect(staged, isolation_level=None)
        # Nothing of it outside its own file: no journal (a file of its own until it is
        # renamed), sorting in memory (the container has no other writable folder).
        for connection in (source, target):
            connection.execute("PRAGMA temp_store = MEMORY")
        target.execute("PRAGMA journal_mode = OFF")
        target.execute("PRAGMA synchronous = OFF")  # (synced as a whole below)
        target.execute("BEGIN")
        for table, (columns, required) in COPIED.items():
            there = {str(r[1]) for r in source.execute(f"PRAGMA table_info({table})")}
            if not there and not required:
                continue
            if missing := [column for column in columns if column not in there]:
                raise NotNavidrome(
                    f"{navidrome_db} is not a Navidrome database this version can read"
                    f" (no {table}.{missing[0]})"
                    if there
                    else f"{navidrome_db} is not a Navidrome database this version can read"
                    f" (no {table} table)"
                )
            listed = ", ".join(columns)
            distinct = "DISTINCT " if table == "scrobbles" else ""
            rows = source.execute(f"SELECT {distinct}{listed} FROM {table}")  # noqa: S608
            target.execute(f"CREATE TABLE {table} ({listed})")
            marks = ", ".join("?" * len(columns))
            target.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)  # noqa: S608
            for column in INDEXED.get(table, ()):
                target.execute(f"CREATE INDEX {table}_{column} ON {table} ({column})")
        target.execute(
            f"CREATE TABLE {META} (format INTEGER NOT NULL, snapshot_at REAL NOT NULL,"
            " answers TEXT NOT NULL, source TEXT NOT NULL)"
        )
        target.execute(
            f"INSERT INTO {META} VALUES (?, ?, ?, ?)",  # noqa: S608
            [FORMAT, taken.at, taken.answers, taken.source],
        )
        target.execute("COMMIT")
        target.close()
        target = None
        source.execute("ROLLBACK")
        if not unchanged():
            raise ExportError("Navidrome's database changed while it was read")
        if _identity(navidrome_db) != taken.source:
            raise ExportError("Navidrome's database was replaced while it was read")
        _sync(staged)
        os.replace(staged, out)
        staged = None
        _sync(out.parent)
        return taken
    except sqlite3.DatabaseError as exc:
        if type(exc) is sqlite3.DatabaseError and "not a database" in str(exc):
            raise NotNavidrome(f"{navidrome_db} is not a Navidrome database ({exc})") from None
        raise ExportError(f"the export failed ({type(exc).__name__}: {exc})") from None
    except sqlite3.Error as exc:
        raise ExportError(f"the export failed ({type(exc).__name__}: {exc})") from None
    except OSError as exc:
        raise ExportError(f"the export cannot be written ({exc.strerror or exc})") from None
    finally:
        if target is not None:
            target.close()
        source.close()
        if staged is not None:
            staged.unlink(missing_ok=True)


def _identity(navidrome_db: Path) -> str:
    """Which file this is (its device and inode): another file at the same path differs."""
    stat = navidrome_db.stat()
    return f"{stat.st_dev}:{stat.st_ino}"


def _request(out: Path) -> str:
    """Shijhon's request as it is now ("" when there is none)."""
    try:
        return request_path(out).read_text(errors="replace").strip()[:200]
    except OSError:
        return ""


def _never_over(navidrome_db: Path, out: Path) -> None:
    """The export never replaces anything but an export: not Navidrome's database or a
    file beside it (the same path given twice), nor any other file there is."""
    source, target = navidrome_db.resolve(), out.resolve()
    beside = {source.with_name(source.name + suffix) for suffix in ("", "-wal", "-shm", "-journal")}
    if target in beside:
        raise ExportError(f"{out} is Navidrome's database itself: not written")
    if not target.exists() or target.stat().st_size == 0:
        return
    try:
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
        try:
            known = read_meta(conn) is not None
        finally:
            conn.close()
    except ExportError:
        known = True  # (an export of another version: replaced)
    except sqlite3.Error:
        known = False
    if not known:
        raise ExportError(f"{out} exists and is not a usage export: not overwritten")


def _open(navidrome_db: Path) -> tuple[sqlite3.Connection, Callable[[], bool]]:
    """Navidrome's database, read-only, in a read transaction - and how to tell, once it is
    read, that what was read is one state of it.

    Navidrome keeps its database in WAL mode: a reader needs the ``-wal`` and ``-shm``
    files beside it, which exist only while some program has the database open (an idle
    Navidrome closes it), and a read-only folder lets nobody make them. Without them the
    database file is complete by itself - every change was moved into it when the last
    connection closed - so it is then read as it is (``immutable``, without locks), and the
    export is kept only if the file did not change meanwhile. Only in exactly that case:
    a database in WAL mode, with no ``-wal`` and no rollback journal beside it, that SQLite
    could not open because of the files it may not make."""
    resolved = navidrome_db.resolve()
    uri = resolved.as_uri()
    try:
        source = sqlite3.connect(uri + "?mode=ro", uri=True, isolation_level=None)
        try:
            source.execute("PRAGMA query_only = ON")
            source.execute("BEGIN")
            source.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except BaseException:
            source.close()
            raise
        return source, lambda: True
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY):
            raise ExportError(f"{navidrome_db} cannot be read now ({exc})") from None
        reason = str(exc)
    except sqlite3.Error as exc:
        raise ExportError(f"{navidrome_db} cannot be opened ({exc})") from None
    wal = resolved.with_name(resolved.name + "-wal")
    journal = resolved.with_name(resolved.name + "-journal")

    def state() -> tuple[int, int, bool]:
        stat = resolved.stat()
        return stat.st_mtime_ns, stat.st_size, wal.exists() or journal.exists()

    try:
        with resolved.open("rb") as header:
            in_wal_mode = header.read(20)[18:20] == b"\x02\x02"
        before = state()
        if not in_wal_mode or before[2]:  # in use, or not that case: tried again in its turn
            raise ExportError(f"{navidrome_db} cannot be read now ({reason})")
        # Written a moment ago: a change within the same tick of the file system's clock
        # (up to two seconds on some) would not show afterwards. Waited out.
        settled = before[0] + SETTLED_NS - time.time_ns()
        if settled > 0:
            time.sleep(min(settled, SETTLED_NS) / 1e9)
            if state() != before:
                raise ExportError(f"{navidrome_db} is being written: tried again")
        source = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, isolation_level=None)
        source.execute("BEGIN")
    except (OSError, sqlite3.Error) as exc:
        raise ExportError(f"{navidrome_db} cannot be opened ({exc})") from None
    # Changes made while it is read go to a new -wal file first; the database file itself
    # changes when they are moved into it: only then was more than one state read.
    return source, lambda: state()[:2] == before[:2]


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # a file system that cannot sync a directory
        if not path.is_dir():
            raise
    finally:
        os.close(fd)


def _asked(request: Path) -> int:
    """When Shijhon last asked for an export (0: never)."""
    try:
        return request.stat().st_mtime_ns
    except OSError:
        return 0


def run(navidrome_db: Path, out: Path, *, every: float, once: bool = False) -> int:
    """Export now, then every ``every`` seconds and whenever Shijhon asks (it writes the
    request file; looked at every second). A failed export is logged and tried again at
    the next turn - the export before stays. ``once``: one export, its exit status."""
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s"
    )
    if once:
        try:
            taken = export(navidrome_db, out)
        except ExportError as exc:
            log.error("usage export: %s", exc)
            return 1
        log.info("usage export written (snapshot %s)", _clock(taken.at))
        return 0

    def stop(signum: int, frame: FrameType | None) -> None:
        raise SystemExit(0)

    # As a container's first process, the default actions do not apply: stop at once.
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        held = writing(out)
        held.__enter__()  # for as long as it runs: the one exporter of this file
    except (ExportError, OSError) as exc:
        log.error("usage export: %s", exc)
        return 1
    try:
        for stale in out.parent.glob(f".{out.name}.*.tmp"):  # what a kill left
            stale.unlink(missing_ok=True)
        request = request_path(out)
        said: str | None = None
        said_at = 0.0
        while True:
            served = _asked(request)
            try:
                export(navidrome_db, out, locked=True)
                problem = ""
            except ExportError as exc:
                problem = str(exc)
            # Each change once - and a failure that lasts, again every hour.
            if problem != said or (problem and time.monotonic() - said_at > REMIND_SECONDS):
                if problem:
                    log.error("usage export: %s (tried again; the export before stays)", problem)
                else:
                    log.info("usage export: written; again every %gs and when asked", every)
                said, said_at = problem, time.monotonic()
            # (after a failure - Navidrome not started yet, say - sooner than in its turn)
            deadline = time.monotonic() + (min(every, RETRY_SECONDS) if problem else every)
            while time.monotonic() < deadline and _asked(request) == served:
                time.sleep(POLL_SECONDS)
    finally:
        held.__exit__(None, None, None)


def _clock(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))
