from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import anyio
import pytest

from shijhon.store.db import Store, apply_migrations


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", isolation_level=None)


def test_applies_in_order_and_is_idempotent() -> None:
    conn = _conn()
    migrations = [
        (1, "0001_a.sql", "CREATE TABLE a (x INTEGER);"),
        (2, "0002_b.sql", "CREATE TABLE b (y TEXT);\nINSERT INTO a VALUES (1);"),
    ]
    assert apply_migrations(conn, migrations) == 2
    assert apply_migrations(conn, migrations) == 2
    assert conn.execute("SELECT count(*) FROM a").fetchone()[0] == 1


def test_failed_migration_rolls_back() -> None:
    conn = _conn()
    migrations = [(1, "0001_a.sql", "CREATE TABLE a (x INTEGER);\nINSERT INTO nope VALUES (1);")]
    with pytest.raises(RuntimeError):
        apply_migrations(conn, migrations)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None


def test_rejects_gaps_and_newer_databases() -> None:
    with pytest.raises(RuntimeError):
        apply_migrations(_conn(), [(2, "0002_a.sql", "SELECT 1;")])
    conn = _conn()
    conn.execute("PRAGMA user_version = 5")
    with pytest.raises(RuntimeError):
        apply_migrations(conn, [(1, "0001_a.sql", "SELECT 1;")])


def test_database_file_is_private(tmp_path: Path) -> None:
    async def main() -> None:
        store = await Store.open(tmp_path / "state" / "db.sqlite3")
        await store.close()

    anyio.run(main)
    mode = stat.S_IMODE((tmp_path / "state" / "db.sqlite3").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
