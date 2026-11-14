"""Checked copies of Shijhon's database (``store/backup.py``): the snapshot a start takes
before it upgrades the schema, and ``shijhon backup``. Shown: what each holds and where it
goes; that a new or current database gets no snapshot; that the latest two are kept; that a
failure (no space, a folder that cannot be written, a damaged database, any other error)
or a stop in the middle leaves nothing under a snapshot's own name and removes no snapshot;
and that the upgrade then goes on without one, keeping a record of why (``store/skipped.py``)
- unless the setting is off, when nothing is recorded."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import signal
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from shijhon import cli
from shijhon.store import backup, skipped
from shijhon.store.db import (
    apply_migrations,
    latest_schema,
    load_migrations,
    prepare_database,
)

MIGRATIONS = load_migrations(Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations")
OLD = 30
SAVE = "INSERT INTO saved_settings VALUES (?, ?, ?, ?, 'admin')"
not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root writes into read-only folders")


def old_database(path: Path, schema: int = OLD) -> Path:
    """A database as an earlier Shijhon left it: in WAL mode, at ``schema``, with a saved
    setting (a secret among them) and a placeholder's record."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    apply_migrations(conn, MIGRATIONS[:schema])
    conn.execute(SAVE, ("catalog", "token", '"an invented secret"', 1.0))
    conn.close()
    path.chmod(0o600)
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def saved(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute("SELECT section, key FROM saved_settings"))
    finally:
        conn.close()


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith("."))


def record(path: Path) -> skipped.Skipped | None:
    """The record of an upgrade without its snapshot, as the database keeps it."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM skipped_snapshots").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return skipped.Skipped(
        row["from_schema"],
        row["to_schema"],
        row["at"],
        row["reason"],
        row["needed"],
        row["free"],
        row["code"],
        row["error"],
    )


# --- the snapshot before an upgrade ---------------------------------------------------------


def test_an_upgrade_is_preceded_by_a_checked_snapshot_of_the_old_schema(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = old_database(tmp_path / "state" / "shijhon.sqlite3")
    before = digest(database)
    with caplog.at_level(logging.INFO, "shijhon.store.backup"):
        prepare_database(database)
    assert schema(database) == latest_schema()
    [found] = backup.snapshots(tmp_path / "state" / "snapshots")
    made = found.path
    assert made.name.startswith(f"shijhon-schema{OLD}-") and made.name.endswith("-1.sqlite3")
    # The database as it was before the upgrade (in a file of its own: no WAL), whole,
    # private.
    assert schema(made) == OLD and saved(made) == [("catalog", "token")]
    conn = sqlite3.connect(made)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
    assert stat.S_IMODE(made.stat().st_mode) == 0o600
    assert stat.S_IMODE(made.parent.stat().st_mode) == 0o700
    assert before != digest(database)  # (migrated after the snapshot)
    assert leftovers(made.parent) == []
    assert f"from schema {OLD} to {latest_schema()}" in caplog.text
    assert record(database) is None  # (nothing to say)


@pytest.mark.parametrize("existing", ["none", "empty", "current"])
def test_a_new_or_current_database_gets_no_snapshot(tmp_path: Path, existing: str) -> None:
    database = tmp_path / "shijhon.sqlite3"
    if existing == "empty":
        database.touch()
    if existing == "current":
        prepare_database(database)
    prepare_database(database)
    assert schema(database) == latest_schema()
    assert not (tmp_path / "snapshots").exists()


def test_the_setting_turns_the_snapshot_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    # (Had a snapshot been tried, it would have failed: nothing is recorded all the same.)
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: type("U", (), {"free": 0}))
    prepare_database(database, snapshot=False)
    assert schema(database) == latest_schema()
    assert not (tmp_path / "snapshots").exists()
    assert record(database) is None


def names(folder: Path) -> list[str]:
    return [found.path.name for found in backup.snapshots(folder)]


def at_schema(database: Path, number: int) -> None:
    """The database moved on to ``number`` (an upgrade that went through)."""
    conn = sqlite3.connect(database, isolation_level=None)
    apply_migrations(conn, MIGRATIONS[:number])
    conn.close()


def test_snapshots_are_kept_by_schema_the_newest_of_the_two_latest(tmp_path: Path) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3", 28)
    folder = tmp_path / "snapshots"
    folder.mkdir()
    (folder / "mine.sqlite3").write_text("a file of the owner's")
    (folder / ".x.partial").write_text("the owner's too")
    take = lambda: backup.snapshot_before_upgrade(database, latest_schema())  # noqa: E731
    take()
    at_schema(database, 29)
    take()
    assert [s.schema for s in backup.snapshots(folder)] == [28, 29]
    at_schema(database, 30)
    take()
    # The upgrade before (29) and this one (30): 28 went once 30 was in place.
    assert [(s.schema, s.number) for s in backup.snapshots(folder)] == [(29, 2), (30, 3)]
    # Attempts again and again on one schema replace its snapshot, never the one before.
    take()
    take()
    assert [(s.schema, s.number) for s in backup.snapshots(folder)] == [(29, 2), (30, 5)]
    assert (folder / "mine.sqlite3").read_text() == "a file of the owner's"
    assert (folder / ".x.partial").read_text() == "the owner's too"


def test_a_later_snapshot_stays_the_newer_whatever_the_clock_says(tmp_path: Path) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    folder = tmp_path / "snapshots"
    for clock in (2_000_000.0, 2_000_000.0, 1_000_000.0):  # the same second, then set back
        made = backup.snapshot_before_upgrade(database, latest_schema(), now=lambda c=clock: c)
        assert made is not None
        assert names(folder) == [made.path.name]  # (never replaced: a name of its own)
    assert names(folder) == [f"shijhon-schema{OLD}-{backup.stamp(1_000_000.0)}-3.sqlite3"]


def test_a_copy_never_replaces_a_file_of_its_name(tmp_path: Path) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    target = tmp_path / "copy.sqlite3"
    target.write_text("something else")
    with pytest.raises(backup.CopyError, match="exists already"):
        backup.copy_database(database, target)
    assert target.read_text() == "something else"
    assert leftovers(tmp_path) == []


def no_hard_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file system without hard links: the name is taken by a checked rename."""

    def refuse(*_: object, **__: object) -> None:
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(backup.os, "link", refuse)


def test_without_hard_links_a_copy_still_gets_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_hard_links(monkeypatch)
    database = old_database(tmp_path / "shijhon.sqlite3")
    target = tmp_path / "copy.sqlite3"
    made = backup.copy_database(database, target)
    assert made.path == target and target.is_file() and not target.is_symlink()
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("taken", ["file", "dangling link"])
def test_without_hard_links_a_copy_never_replaces_a_name_that_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, taken: str
) -> None:
    no_hard_links(monkeypatch)
    database = old_database(tmp_path / "shijhon.sqlite3")
    target = tmp_path / "copy.sqlite3"
    if taken == "file":
        target.write_text("something else")
    else:
        target.symlink_to(tmp_path / "nowhere")
    with pytest.raises(backup.CopyError, match="exists already"):
        backup.copy_database(database, target)
    if taken == "file":
        assert target.read_text() == "something else"
    else:
        assert target.is_symlink() and os.readlink(target) == str(tmp_path / "nowhere")
    assert leftovers(tmp_path) == []


def test_a_restored_older_snapshot_keeps_the_copy_just_made_and_the_highest_other(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "snapshots"
    database = tmp_path / "shijhon.sqlite3"
    for schema in (28, 30, 28, 29):  # upgrades, then a restore of 28 and its upgrades
        for beside in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            beside.unlink(missing_ok=True)
        old_database(database, schema)
        made = backup.snapshot_before_upgrade(database, latest_schema())
        assert made is not None and made.path.name in names(folder)
    assert sorted(name.split("-")[1] for name in names(folder)) == ["schema29", "schema30"]


def test_without_room_no_snapshot_is_removed_and_the_upgrade_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3", 29)
    backup.snapshot_before_upgrade(database, latest_schema())
    at_schema(database, OLD)
    backup.snapshot_before_upgrade(database, latest_schema())
    folder = tmp_path / "snapshots"
    before_snapshots = names(folder)
    assert len(before_snapshots) == 2
    before = digest(database)

    class Usage:
        free = database.stat().st_size + backup.MARGIN - 1

    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: Usage)
    with pytest.raises(backup.CopyError) as refused:
        backup.snapshot_before_upgrade(database, latest_schema())
    message = str(refused.value)
    assert "not enough free space" in message and "the copy needs" in message
    assert "no snapshot was removed" in message and "by hand" in message
    assert refused.value.kind == "no_room"
    assert digest(database) == before
    prepare_database(database)
    assert names(folder) == before_snapshots
    assert schema(database) == latest_schema()
    assert leftovers(folder) == []


def test_a_new_snapshots_folder_is_on_disk_before_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = old_database(tmp_path / "state" / "shijhon.sqlite3")
    synced: list[Path] = []
    real = backup.durable.sync_dir
    monkeypatch.setattr(backup.durable, "sync_dir", lambda path: (synced.append(path), real(path)))
    backup.snapshot_before_upgrade(database, latest_schema())
    # The state folder, where the new snapshots folder's entry is, before the snapshot's.
    assert database.parent in synced
    assert synced.index(database.parent) < synced.index(database.parent / "snapshots")


def test_an_abandoned_wal_keeps_its_data_when_the_snapshot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    # A process that committed and ended without closing: its commit is only in the WAL.
    subprocess.run(
        [sys.executable, "-c", ABANDONED, str(database)],
        check=True,
    )
    assert database.with_name(database.name + "-wal").exists()
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: type("U", (), {"free": 0}))
    prepare_database(database)
    # Migrated without a snapshot, and the committed data are there.
    assert schema(database) == latest_schema()
    assert sorted(saved(database)) == [("catalog", "token"), ("fill", "abandoned")]
    assert names(tmp_path / "snapshots") == []
    found = record(database)
    assert found is not None and found.reason == "no_room"


ABANDONED = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA wal_autocheckpoint = 0")
conn.execute("INSERT INTO saved_settings VALUES ('fill', 'abandoned', '1', 4, 'admin')")
os._exit(0)
"""


def _went_on(database: Path, caplog: pytest.LogCaptureFixture, reason: str) -> skipped.Skipped:
    """The start without its snapshot: a warning with the reason, the database upgraded
    with its data, nothing under a snapshot's name, and the record of why."""
    with caplog.at_level(logging.WARNING, "shijhon.store.db"):
        prepare_database(database, clock=lambda: 2_000_000.0)
    assert schema(database) == latest_schema()
    assert saved(database) == [("catalog", "token")]
    assert backup.snapshots(database.parent / "snapshots") == []
    assert "could not be written, so the upgrade goes on without one" in caplog.text
    assert "an invented secret" not in caplog.text
    found = record(database)
    assert found is not None
    assert (found.from_schema, found.to_schema, found.at) == (OLD, latest_schema(), 2_000_000.0)
    assert found.reason == reason
    for words in (found.why(), found.advice()):  # (no path, no secret)
        assert str(database.parent) not in words and "an invented secret" not in words
    return found


def test_without_room_for_the_snapshot_the_upgrade_goes_on_and_says_how_much(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    size = database.stat().st_size
    free = size + backup.MARGIN - 1

    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: type("U", (), {"free": free}))
    found = _went_on(database, caplog, "no_room")
    assert "not enough free space" in caplog.text
    assert found.free == free and found.needed is not None and found.needed > free
    assert found.why() == (
        f"there was not enough free space (it needed {backup.megabytes(found.needed)},"
        f" and {backup.megabytes(free)} was free)"
    )
    assert "Free some space" in found.advice() and "shijhon backup" in found.advice()
    assert leftovers(tmp_path / "snapshots") == []


def test_a_disk_that_fills_up_while_the_copy_is_written_is_no_room_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")

    def full(partial: Path) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    real = backup.copy_database
    monkeypatch.setattr(backup, "copy_database", lambda *a, **k: real(*a, **{**k, "checked": full}))
    found = _went_on(database, caplog, "no_room")
    assert found.needed is not None and found.free is not None
    assert leftovers(tmp_path / "snapshots") == []


@not_root
def test_a_folder_that_cannot_be_written_does_not_stop_the_upgrade(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    (tmp_path / "snapshots").mkdir(mode=0o500)
    try:
        found = _went_on(database, caplog, "unwritable")
    finally:
        (tmp_path / "snapshots").chmod(0o700)
    assert "cannot be written" in caplog.text
    assert found.code == errno.EACCES
    assert found.why() == (
        "the snapshots folder in the state folder could not be written"
        f" ({os.strerror(errno.EACCES)})"
    )
    assert "Make the snapshots folder writable" in found.advice()


def test_a_damaged_database_is_not_copied_and_the_record_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    conn = sqlite3.connect(database)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.close()
    with database.open("r+b") as out:  # (the header, and with it the schema, stays)
        out.seek(page_size * (pages // 2))
        out.write(b"\xff" * page_size * 3)
    found = _went_on(database, caplog, "damaged")
    assert "damaged" in caplog.text and found.damaged
    assert "may be damaged" in found.why() and "restore an earlier backup" in found.advice()
    assert leftovers(tmp_path / "snapshots") == []


@pytest.mark.parametrize(
    ("raised", "words"),
    [
        (RuntimeError("at /a/private/path"), "an error stopped it (RuntimeError)"),
        (
            OSError(errno.EIO, os.strerror(errno.EIO), "/a/private/path"),
            f"an error stopped it (OSError: {os.strerror(errno.EIO)})",
        ),
        (
            backup.CopyError("the copy failed SQLite's integrity check (x)", "copy_check"),
            "the copy did not pass SQLite's integrity check",
        ),
        (
            backup.CopyError("the database /a/private/path cannot be read", "unreadable"),
            "the database could not be read for the copy",
        ),
    ],
    ids=["another-exception", "a-system-error", "copy-check", "unreadable"],
)
def test_any_other_failure_does_not_stop_the_upgrade_and_is_named_by_its_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raised: Exception,
    words: str,
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")

    def fails(*_: object, **__: object) -> None:
        raise raised

    monkeypatch.setattr(backup, "snapshot_before_upgrade", fails)
    found = _went_on(database, caplog, getattr(raised, "kind", "error"))
    assert found.why() == words and "/a/private/path" not in found.why()
    assert found.advice() == (
        "The log of that start says more. Make a backup now with shijhon backup."
    )


def test_a_new_database_records_nothing_even_when_the_snapshot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(*_: object, **__: object) -> None:
        raise backup.CopyError("x", "unreadable")

    monkeypatch.setattr(backup, "snapshot_before_upgrade", fails)
    database = tmp_path / "shijhon.sqlite3"
    prepare_database(database)
    prepare_database(database)  # (and one already current)
    assert schema(database) == latest_schema() and record(database) is None


def test_a_later_upgrade_without_a_snapshot_keeps_the_first_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shijhon.store import db

    current = latest_schema()
    database = old_database(tmp_path / "shijhon.sqlite3", current)
    conn = sqlite3.connect(database, isolation_level=None)
    skipped.record(conn, skipped.Skipped(OLD, current, 1.0, "unwritable", code=errno.EACCES))
    conn.close()
    # A later Shijhon with one more migration, and still no room for a snapshot.
    later = [*MIGRATIONS, (current + 1, f"{current + 1:04d}_later.sql", "CREATE TABLE t (x);")]
    monkeypatch.setattr(db, "_bundled_migrations", lambda: later)
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: type("U", (), {"free": 0}))
    prepare_database(database, clock=lambda: 5.0)
    found = record(database)
    assert found is not None and schema(database) == current + 1
    assert (found.from_schema, found.to_schema, found.at) == (OLD, current + 1, 5.0)
    assert (found.reason, found.free, found.code) == ("no_room", 0, None)


def test_a_record_that_cannot_be_saved_does_not_stop_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: type("U", (), {"free": 0}))

    def refuse(*_: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(skipped, "record", refuse)
    with caplog.at_level(logging.WARNING, "shijhon.store.db"):
        prepare_database(database)
    assert schema(database) == latest_schema()
    assert "could not be saved, so the dashboard will not show it" in caplog.text


def test_the_record_is_a_migration_of_its_own(tmp_path: Path) -> None:
    prepare_database(tmp_path / "s.sqlite3")
    with sqlite3.connect(tmp_path / "s.sqlite3") as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(skipped_snapshots)")]
    assert columns == [
        "id",
        "from_schema",
        "to_schema",
        "at",
        "reason",
        "needed",
        "free",
        "code",
        "error",
    ]


WARNED = """
import sys
from pathlib import Path
import anyio
from shijhon.store import Store
async def main():
    store = await Store.open(Path(sys.argv[1]))
    await store.close()
anyio.run(main)
"""


@not_root
def test_a_command_that_migrates_without_logging_set_up_still_prints_the_warning(
    tmp_path: Path,
) -> None:
    # ``shijhon fills-undo --apply`` opens the store as this does, without the service's
    # logging: the warning reaches its error output all the same.
    database = old_database(tmp_path / "shijhon.sqlite3")
    (tmp_path / "snapshots").mkdir(mode=0o500)
    try:
        done = subprocess.run(
            [sys.executable, "-c", WARNED, str(database)], capture_output=True, text=True
        )
    finally:
        (tmp_path / "snapshots").chmod(0o700)
    assert done.returncode == 0, done.stderr
    assert "so the upgrade goes on without one" in done.stderr
    assert schema(database) == latest_schema()
    found = record(database)
    assert found is not None and found.reason == "unwritable"


def test_a_failure_while_the_copy_is_written_leaves_nothing(tmp_path: Path) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3")
    before = digest(database)

    def full(partial: Path) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with pytest.raises(backup.CopyError, match="not enough free space") as refused:
        backup.snapshot_before_upgrade(database, latest_schema(), checked=full)
    assert refused.value.kind == "no_room"
    assert digest(database) == before
    assert backup.snapshots(tmp_path / "snapshots") == []
    assert leftovers(tmp_path / "snapshots") == []


STOPPED = """
import os, signal, sys
from pathlib import Path
from shijhon.store import backup
from shijhon.store.db import latest_schema
def stop(partial):
    Path(sys.argv[2]).write_text(partial.name)
    os.kill(os.getpid(), signal.SIGKILL)
backup.snapshot_before_upgrade(Path(sys.argv[1]), latest_schema(), checked=stop)
"""


def test_a_stop_in_the_middle_of_the_snapshot_leaves_no_snapshot_and_the_database_as_it_was(
    tmp_path: Path,
) -> None:
    database = old_database(tmp_path / "state" / "shijhon.sqlite3")
    before = digest(database)
    marker = tmp_path / "stopped-at"
    # Killed once the copy is written and checked, before it has its own name: as late as
    # a stop can come.
    ended = subprocess.run([sys.executable, "-c", STOPPED, str(database), str(marker)])
    assert ended.returncode == -signal.SIGKILL
    folder = database.parent / "snapshots"
    assert leftovers(folder) == [marker.read_text()]
    assert backup.snapshots(folder) == []
    assert digest(database) == before and schema(database) == OLD
    # The next start removes what the stop left, takes its snapshot and upgrades.
    prepare_database(database)
    assert schema(database) == latest_schema()
    assert [schema(found.path) for found in backup.snapshots(folder)] == [OLD]
    assert leftovers(folder) == []


# --- shijhon backup -------------------------------------------------------------------------


def configuration(tmp_path: Path) -> tuple[Path, Path]:
    password = tmp_path / "config" / "navidrome-password"
    password.parent.mkdir()
    password.write_text("an-invented-password\n")
    password.chmod(0o600)
    config = tmp_path / "config" / "shijhon.toml"
    config.write_text(
        f'state_dir = "{tmp_path / "state"}"\n\n[navidrome]\npassword_file = "{password}"\n'
    )
    return config, password


def test_backup_holds_a_checked_copy_the_configuration_and_the_password_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, password = configuration(tmp_path)
    database = old_database(tmp_path / "state" / "shijhon.sqlite3", latest_schema())
    cli.main(["--config", str(config), "backup"])
    out = capsys.readouterr().out
    [made] = (tmp_path / "state" / "backups").glob("shijhon-backup-*")
    assert made.name.startswith("shijhon-backup-")
    assert f"backup written: {made}" in out
    assert sorted(p.name for p in made.iterdir()) == [
        "README.txt",
        "navidrome-password",
        "shijhon.sqlite3",
        "shijhon.toml",
    ]
    assert saved(made / "shijhon.sqlite3") == [("catalog", "token")]
    assert schema(made / "shijhon.sqlite3") == latest_schema()
    assert (made / "navidrome-password").read_bytes() == password.read_bytes()
    assert (made / "shijhon.toml").read_bytes() == config.read_bytes()
    # What it holds and what not, in the output and the README; no secret anywhere.
    for text in (out, (made / "README.txt").read_text()):
        assert f"schema {latest_schema()}" in text
        assert "Navidrome's database" in text and "placeholder folder" in text
        assert "an-invented-password" not in text and "an invented secret" not in text
    assert stat.S_IMODE(made.stat().st_mode) == 0o700
    assert {stat.S_IMODE(p.stat().st_mode) for p in made.iterdir()} == {0o600}
    # Only read: the database is where it was, at its schema, with nothing beside it.
    assert schema(database) == latest_schema()
    assert sorted(p.name for p in database.parent.iterdir()) == ["backups", "shijhon.sqlite3"]


def test_backup_while_shijhon_writes_copies_the_last_commit(tmp_path: Path) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3", latest_schema())
    # The running service: commits not yet moved out of the WAL, and a write under way.
    service = sqlite3.connect(database, isolation_level=None)
    service.execute("PRAGMA wal_autocheckpoint = 0")
    service.execute(SAVE, ("fill", "enabled", "1", 2.0))
    service.execute("BEGIN IMMEDIATE")
    service.execute(SAVE, ("fill", "pending", "1", 3.0))
    try:
        made = backup.make_backup(database, tmp_path / "out")
    finally:
        service.execute("ROLLBACK")
        service.close()
    assert sorted(saved(made.database.path)) == [("catalog", "token"), ("fill", "enabled")]
    assert sorted(p.name for p in made.path.iterdir()) == ["README.txt", "shijhon.sqlite3"]
    assert "the configuration file (none was given with --config)" in made.left_out


def test_backup_after_a_stop_or_a_failure_leaves_only_whole_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3", latest_schema())
    folder = tmp_path / "out"
    folder.mkdir()
    # What a killed backup leaves: its folder under the temporary name.
    (folder / ".shijhon-backup-20261101T000000Z.partial").mkdir()
    (folder / ".shijhon-backup-20261101T000000Z.partial" / "shijhon.sqlite3").write_bytes(b"x")
    # And what is someone else's in the same folder, named much like it.
    (folder / ".unrelated.partial").mkdir()
    (folder / ".unrelated.partial" / "important").write_text("keep")
    (folder / ".x.partial").write_text("keep")
    (folder / ".shijhon-backup-mine.partial").mkdir()
    theirs = [".shijhon-backup-mine.partial", ".unrelated.partial", ".x.partial"]
    real = backup._write_private

    def full(target: Path, data: bytes) -> None:
        if target.name == "README.txt":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        real(target, data)

    monkeypatch.setattr(backup, "_write_private", full)
    with pytest.raises(backup.CopyError, match="No space left"):
        backup.make_backup(database, folder)
    assert sorted(p.name for p in folder.iterdir() if p.name != ".backup.lock") == theirs
    monkeypatch.setattr(backup, "_write_private", real)
    made = backup.make_backup(database, folder)
    assert sorted(p.name for p in folder.iterdir() if p.name != ".backup.lock") == sorted(
        [*theirs, made.path.name]
    )
    assert (folder / ".unrelated.partial" / "important").read_text() == "keep"


def test_backup_refuses_without_a_database_or_room(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["backup", "--database", str(tmp_path / "none.sqlite3")])
    assert refused.value.code == 1 and "no database at" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []
    database = old_database(tmp_path / "shijhon.sqlite3", latest_schema())
    with pytest.raises(backup.CopyError, match="not enough free space"):
        backup.make_backup(database, tmp_path / "out", free=lambda _: 1024)
    assert [p.name for p in (tmp_path / "out").iterdir()] == [".backup.lock"]


@not_root
def test_backup_is_not_run_as_root_on_a_database_of_another_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3", latest_schema())
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit) as refused:
        cli.main(["backup", "--database", str(database)])
    assert refused.value.code == 1 and "not as root" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shijhon.sqlite3"]


@pytest.mark.parametrize("which", ["configuration", "password file"])
@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_backup_fails_when_a_file_it_is_to_hold_cannot_be_read(
    tmp_path: Path, which: str, how: str
) -> None:
    if how == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads unreadable files")
    config, password = configuration(tmp_path)
    database = old_database(tmp_path / "state" / "shijhon.sqlite3", latest_schema())
    target = config if which == "configuration" else password
    if how == "missing":
        target.unlink()
    else:
        target.chmod(0)
    try:
        with pytest.raises(backup.CopyError) as refused:
            backup.make_backup(
                database, tmp_path / "out", configuration=config, password_file=password
            )
    finally:
        if target.exists():
            target.chmod(0o600)
    message = str(refused.value)
    assert str(target) in message and "cannot be read" in message
    assert "an-invented-password" not in message
    assert not (tmp_path / "out").exists()  # (nothing begun, nothing that looks finished)


def test_the_command_says_which_password_file_it_could_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config, password = configuration(tmp_path)
    old_database(tmp_path / "state" / "shijhon.sqlite3", latest_schema())
    password.unlink()
    with pytest.raises(SystemExit) as refused:
        cli.main(["--config", str(config), "backup"])
    err = capsys.readouterr().err
    assert refused.value.code == 1 and f"password file {password} cannot be read" in err
    assert not (tmp_path / "state" / "backups").exists()


@not_root
def test_backup_says_the_database_folder_must_be_writable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stopped database in WAL mode, in a folder this user can only read.
    database = old_database(tmp_path / "state" / "shijhon.sqlite3", latest_schema())
    database.parent.chmod(0o500)
    try:
        with pytest.raises(SystemExit) as refused:
            cli.main(["backup", "--database", str(database), "--to", str(tmp_path / "out")])
    finally:
        database.parent.chmod(0o700)
    err = capsys.readouterr().err
    assert refused.value.code == 1
    assert f"the database's folder {database.parent} must be writable" in err
    assert "WAL" in err
    assert not (tmp_path / "out").exists()


def test_backup_folders_it_creates_are_on_disk_before_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3", latest_schema())
    synced: list[Path] = []
    real = backup.durable.sync_dir
    monkeypatch.setattr(backup.durable, "sync_dir", lambda path: (synced.append(path), real(path)))
    made = backup.make_backup(database, tmp_path / "out" / "nested")
    # Each new folder's entry in its parent, before the backup goes into it.
    assert synced[:2] == [tmp_path, tmp_path / "out"]
    assert synced[-1] == tmp_path / "out" / "nested" and made.path.parent == synced[-1]


def test_the_readme_restores_through_a_temporary_name(tmp_path: Path) -> None:
    database = old_database(tmp_path / "shijhon.sqlite3", latest_schema())
    readme = (backup.make_backup(database, tmp_path / "out").path / "README.txt").read_text()
    words = " ".join(readme.split())
    copy, remove, rename = (
        words.index("under a temporary name"),
        words.index("remove shijhon.sqlite3-wal"),
        words.index("rename restored.sqlite3 to shijhon.sqlite3"),
    )
    assert copy < remove < rename
    assert "remove shijhon.sqlite3 and" not in words
