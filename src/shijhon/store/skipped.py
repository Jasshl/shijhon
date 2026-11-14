"""An upgrade of the database's schema that went on without its snapshot.

When ``upgrade_snapshots`` is on and the snapshot cannot be written (``backup.py``), the
start upgrades all the same (the owner's choice: an upgrade without a snapshot is what
every start did before snapshots existed) and keeps a record of it in the upgraded database
(``skipped_snapshots``, one row): from which schema to which, when, and why - a kind and
the numbers that go with it, never the text of an exception, which can hold paths. The
dashboard shows it to administrators and the notifications report it once, both from one
read at startup, until an administrator dismisses it. A later upgrade without a snapshot
before that keeps the first schema and takes the rest from the later one.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shijhon.store import backup

if TYPE_CHECKING:
    from shijhon.store import Store

# The reasons a record names (``backup.CopyError.kind``), and ``error`` for any other.
REASONS = ("no_room", "unwritable", "damaged", "unreadable", "copy_check", "exists", "error")
NAME_LENGTH = 60


@dataclass(frozen=True)
class Skipped:
    from_schema: int
    to_schema: int
    at: float
    reason: str
    needed: int | None = None  # bytes, for no_room
    free: int | None = None  # bytes, for no_room
    code: int | None = None  # the system's error number, when there was one
    error: str = ""  # the kind of another error: its class's name

    @property
    def damaged(self) -> bool:
        return self.reason == "damaged"

    def why(self) -> str:
        """The reason in plain words (no path, nothing secret)."""
        cause = f" ({os.strerror(self.code)})" if self.code is not None else ""
        if self.reason == "no_room" and self.needed is not None and self.free is not None:
            return (
                f"there was not enough free space (it needed {backup.megabytes(self.needed)},"
                f" and {backup.megabytes(self.free)} was free)"
            )
        if self.reason == "no_room":
            return "there was not enough free space"
        if self.reason == "unwritable":
            return f"the snapshots folder in the state folder could not be written{cause}"
        if self.reason == "damaged":
            return "the database did not pass SQLite's integrity check, so it may be damaged"
        if self.reason == "unreadable":
            return "the database could not be read for the copy"
        if self.reason == "copy_check":
            return "the copy did not pass SQLite's integrity check"
        if self.reason == "exists":
            return "a file in the snapshots folder already had the snapshot's name"
        said = [self.error] if self.error else []
        if self.code is not None:
            said.append(os.strerror(self.code))
        return "an error stopped it" + (f" ({': '.join(said)})" if said else "")

    def advice(self) -> str:
        """What to do now."""
        if self.reason == "damaged":
            return (
                "Look into it soon, and restore an earlier backup if you have one: shijhon"
                " backup cannot copy a database that fails the check."
            )
        if self.reason == "no_room":
            first = "Free some space so that the next upgrade can take one"
        elif self.reason == "unwritable":
            first = (
                "Make the snapshots folder writable for Shijhon so that the next upgrade can"
                " take one"
            )
        else:
            return "The log of that start says more. Make a backup now with shijhon backup."
        return f"{first}, and make a backup now with shijhon backup."


def of(exc: BaseException, from_schema: int, to_schema: int, at: float) -> Skipped:
    """The record of an upgrade from ``from_schema`` to ``to_schema`` whose snapshot failed
    with ``exc``."""
    if isinstance(exc, backup.CopyError) and exc.kind in REASONS and exc.kind != "error":
        return Skipped(from_schema, to_schema, at, exc.kind, exc.needed, exc.free, exc.code, "")
    code = exc.code if isinstance(exc, backup.CopyError) else getattr(exc, "errno", None)
    name = type(exc).__name__ if not isinstance(exc, backup.CopyError) else ""
    return Skipped(
        from_schema,
        to_schema,
        at,
        "error",
        code=code if isinstance(code, int) else None,
        error="".join(ch for ch in name if ch.isalnum() or ch == "_")[:NAME_LENGTH],
    )


def record(conn: sqlite3.Connection, skipped: Skipped) -> None:
    """Keep ``skipped`` in the database (just migrated, on its own connection). One
    record: an earlier one not yet dismissed keeps its first schema."""
    conn.execute(
        "INSERT INTO skipped_snapshots"
        " (id, from_schema, to_schema, at, reason, needed, free, code, error)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET to_schema=excluded.to_schema, at=excluded.at,"
        " reason=excluded.reason, needed=excluded.needed, free=excluded.free,"
        " code=excluded.code, error=excluded.error",
        [
            skipped.from_schema,
            skipped.to_schema,
            skipped.at,
            skipped.reason,
            skipped.needed,
            skipped.free,
            skipped.code,
            skipped.error,
        ],
    )


async def read(store: Store) -> Skipped | None:
    row = await store.fetchone(
        "SELECT from_schema, to_schema, at, reason, needed, free, code, error"
        " FROM skipped_snapshots WHERE id = 1"
    )
    if row is None:
        return None
    return Skipped(
        int(row["from_schema"]),
        int(row["to_schema"]),
        float(row["at"]),
        str(row["reason"]),
        None if row["needed"] is None else int(row["needed"]),
        None if row["free"] is None else int(row["free"]),
        None if row["code"] is None else int(row["code"]),
        str(row["error"]),
    )


async def dismiss(store: Store) -> None:
    await store.execute("DELETE FROM skipped_snapshots")
