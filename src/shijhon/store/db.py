"""SQLite access.

Migrations are numbered ``NNNN_name.sql`` files applied in order; the applied version is
kept in ``PRAGMA user_version``. This is deliberately tiny: Shijhon has a handful of tables
and one process, so a migration framework would add more than it saves.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite
import anyio
import anyio.to_thread

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _bundled_migrations() -> list[tuple[int, str, str]]:
    folder = resources.files("shijhon.store") / "migrations"
    found = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text()))
    return sorted(found)


def load_migrations(folder: Path) -> list[tuple[int, str, str]]:
    found = []
    for path in folder.iterdir():
        match = _MIGRATION_NAME.match(path.name)
        if match:
            found.append((int(match.group(1)), path.name, path.read_text()))
    return sorted(found)


def apply_migrations(
    conn: sqlite3.Connection, migrations: Iterable[tuple[int, str, str]] | None = None
) -> int:
    """Apply pending migrations in order; returns the resulting schema version."""
    items = list(_bundled_migrations() if migrations is None else migrations)
    numbers = [number for number, _, _ in items]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError(f"migrations must be numbered 1..n without gaps, got {numbers}")
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(items):
        raise RuntimeError(
            f"database schema version {current} is newer than this Shijhon ({len(items)})"
        )
    for number, name, sql in items[current:]:
        try:
            conn.execute("BEGIN")
            # executescript would commit implicitly; run statements one by one instead.
            for statement in _statements(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"migration {name} failed") from exc
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _statements(sql: str) -> list[str]:
    statements, buffer = [], ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise RuntimeError("migration ends with an incomplete statement")
    return statements


def prepare_database(path: Path) -> None:
    """Create the database privately (add-on settings may hold secrets) and migrate it."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    # Also when it existed (and its journal files: SQLite gives them the database's mode).
    for part in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if part.exists() and part.stat().st_mode & 0o077:
            part.chmod(0o600)
    setup = sqlite3.connect(path, isolation_level=None)
    try:
        setup.execute("PRAGMA journal_mode = WAL")
        apply_migrations(setup)
    finally:
        setup.close()


class Store:
    """One aiosqlite connection (a single writer thread) shared by the whole process.

    Statements and transactions are serialized by a lock, so a transaction never picks up
    another coroutine's statements. Inside :meth:`transaction`, use the yielded connection
    (calling the store's own methods there would wait for the lock forever).
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self._lock = anyio.Lock()

    @classmethod
    async def open(cls, path: Path) -> Store:
        await anyio.to_thread.run_sync(prepare_database, path)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        # Every commit on disk before it returns (SQLite's default, said here: the library
        # writes rely on it - a pending record before its files, a row before its backup
        # goes).
        await conn.execute("PRAGMA synchronous = FULL")
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """One transaction. Its BEGIN, COMMIT and ROLLBACK are not cut short by a
        cancellation: the connection's thread runs a statement once it is queued, so a
        canceled wait for one would leave the transaction open (BEGIN) or roll back what
        was committed (COMMIT)."""
        async with self._lock:
            with anyio.CancelScope(shield=True):
                await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self.conn.execute("ROLLBACK")
                raise
            try:
                with anyio.CancelScope(shield=True):
                    await self.conn.execute("COMMIT")
            except BaseException:
                with anyio.CancelScope(shield=True):
                    if self.conn.in_transaction:
                        await self.conn.execute("ROLLBACK")
                raise

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock, self.conn.execute(sql, tuple(params)) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock, self.conn.execute(sql, tuple(params)) as cursor:
            return list(await cursor.fetchall())

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run one statement in its own transaction; returns the last row id."""
        async with self._lock, self.conn.execute(sql, tuple(params)) as cursor:
            return cursor.lastrowid or 0
