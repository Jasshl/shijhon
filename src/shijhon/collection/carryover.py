"""The catalog albums carried over at the upgrade to schema 24 (migration 0024): those the
library held then that no account's record explained - much of what Shijhon added before
recorded nothing per account. They are in no account's library (they stay in Navidrome,
in playlists, in search and in the Collection page's "Everything on the server") until
``shijhon carry-over --account NAME`` gives them, once, to that account's Shijhon library,
and so to its All (``policy.shijhon_albums``). Giving them again to the same account
changes nothing; another account gets them only with ``--replace``, and the first one then
no longer has them by this rule.

The account is the one Navidrome names, by its immutable user ID (resolved by the caller,
``access.AccessService.verify``). Only Shijhon's database is written, in one short
transaction: the running service reads the libraries per request, so it may run beside
it."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

# The carried-over albums still in the library, and their songs (Shijhon's own records of
# what it added: also before the Collection page has read the library).
_COUNT = (
    "SELECT count(DISTINCT r.ref),count(l.track_ref) FROM collection_carryover c"
    " JOIN releases r ON r.ref=c.release_ref AND r.removing_at IS NULL"
    " LEFT JOIN track_links l ON l.release_ref=r.ref"
)


@dataclass(frozen=True)
class Owner:
    """The account the carried-over albums were given to: its Navidrome user ID, its user
    name then, and when."""

    user_id: str
    username: str
    given_at: float


@dataclass(frozen=True)
class CarryOver:
    albums: int
    songs: int
    owner: Owner | None


@dataclass(frozen=True)
class Given:
    carried: CarryOver  # as it is now
    changed: bool  # False: they were the account's already, and nothing was written
    previous: Owner | None  # the account they were taken from (``replace``)


class GivenAlready(Exception):
    """The carried-over albums are another account's: nothing was written."""

    def __init__(self, owner: Owner) -> None:
        super().__init__("The carried-over albums were given to another account.")
        self.owner = owner


def state(conn: sqlite3.Connection) -> CarryOver:
    """The carried-over albums still in the library, and the account they were given to."""
    albums, songs = conn.execute(_COUNT).fetchone()
    row = conn.execute(
        "SELECT owner_id,username,given_at FROM collection_carryover_owner"
    ).fetchone()
    return CarryOver(int(albums), int(songs), Owner(*row) if row is not None else None)


def give(
    conn: sqlite3.Connection,
    user_id: str,
    username: str,
    *,
    replace: bool = False,
    now: float | None = None,
) -> Given:
    """Give the carried-over albums to the account, in one transaction (``conn`` in
    autocommit mode). The same account again: nothing changes. Another account's already:
    ``GivenAlready``, unless ``replace``."""
    if not user_id:
        raise ValueError("an account's user ID is needed")
    conn.execute("BEGIN IMMEDIATE")
    try:
        found = state(conn)
        before = found.owner
        if before is not None and before.user_id == user_id:
            conn.execute("ROLLBACK")
            return Given(found, changed=False, previous=None)
        if before is not None and not replace:
            raise GivenAlready(before)
        owner = Owner(user_id, username, time.time() if now is None else now)
        conn.execute(
            "INSERT INTO collection_carryover_owner(slot,owner_id,username,given_at)"
            " VALUES (1,?,?,?) ON CONFLICT(slot) DO UPDATE SET owner_id=excluded.owner_id,"
            " username=excluded.username,given_at=excluded.given_at",
            [owner.user_id, owner.username, owner.given_at],
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return Given(CarryOver(found.albums, found.songs, owner), changed=True, previous=before)
