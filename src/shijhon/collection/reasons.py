"""What brings whole albums into an account's Original imported library besides its own
imports.

The albums of the songs and albums an account favorited in Navidrome, and of the songs
of the playlists it imported (``policy.original_albums``). Both are the account's own, kept
in Shijhon's database:

- **Favorites.** A ``star`` or ``unstar`` that Navidrome accepts through Shijhon - from
  an app or the native website - counts at once (``favorited``). Shijhon also reads the
  account's favorites from Navidrome as that account (``getStarred2``, with the
  credentials of the account's own request, never kept) when its apps read the library,
  at most once every ``interval`` (five minutes): that learns the favorites made before,
  or past Shijhon. An account's very first read is waited for, up to ``first_wait``;
  later ones run beside the request that started them.
- **Imported playlists.** The songs an import published (its matched entries, not the
  owner's later edits), written in the publication's own transaction (``imported``).

Downloads, plays, generated playlists and other accounts' choices are no reasons here;
the account's own activity brings catalog albums into its Shijhon library instead
(``collection/history.py``, ``policy.shijhon_albums``) - favorites too: a favorite read
here that Shijhon had not seen is kept to record as activity.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any
from urllib.parse import urlencode

import aiosqlite
import anyio

from shijhon.access import Principal
from shijhon.collection.history import queue_pending
from shijhon.proxy.auth import CREDENTIAL_PARAMS
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Upstream
from shijhon.store import Store
from shijhon.views.answers import raw_library_answer

log = logging.getLogger(__name__)
FAVORITES_SECONDS = 300.0  # how often an account's favorites are read again, at most
FIRST_WAIT_SECONDS = 5.0  # how long a request waits for an account's first read
RETRY_SECONDS = 30.0  # a read that failed is tried again after this
MAX_BYTES = 32 << 20  # an answer with the account's favorites
MAX_ITEMS = 500  # IDs of one star or unstar that are recorded
READ_SECONDS = 30.0  # one read of an account's favorites, whole (its credentials go then)
READS_AT_ONCE = 2  # reads of favorites under way at the same time, of all accounts
WAITING = 16  # reads begun or waiting their turn; more accounts are read later

Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], bool]
# Reads the account's favorites from Navidrome as the account: (kind, item ID) pairs.
Starred = Callable[[], Awaitable[set[tuple[str, str]]]]


class Reasons:
    def __init__(
        self,
        store: Store,
        library_id: str,
        upstream: Upstream,
        spawn: Spawn,
        *,
        interval: float = FAVORITES_SECONDS,
        first_wait: float = FIRST_WAIT_SECONDS,
    ) -> None:
        self.store, self.library_id, self.upstream, self.spawn = (
            store,
            library_id,
            upstream,
            spawn,
        )
        self.interval, self.first_wait = interval, first_wait
        self._due: dict[str, float] = {}  # owner -> when its favorites are read next
        self._running: dict[str, anyio.Event] = {}
        self._changes: dict[str, int] = {}  # owner -> stars and unstars recorded
        self._reading = anyio.CapacityLimiter(READS_AT_ONCE)
        self.read_seconds = READ_SECONDS
        self.reads = 0  # observable in tests
        # Told when favorites were kept to record (``CollectionRequests.notify``).
        self.learned: Callable[[], None] | None = None

    # --- favorites -----------------------------------------------------------------------

    async def favorited(self, owner: str, call: RestCall) -> None:
        """A ``star`` or ``unstar`` Navidrome accepted, as it got it (native IDs). An ``id``
        names a song, an album or an artist: an album the library index knows counts as
        an album, anything else as a song (an artist's ID then matches no song)."""
        if call.name not in ("star", "unstar"):
            return
        items = [(k, v) for k, v in call.params if k in ("id", "albumId") and 0 < len(v) <= 300]
        if not items:
            return
        self._changes[owner] = self._changes.get(owner, 0) + 1
        async with self.store.transaction() as db:
            for key, value in items[:MAX_ITEMS]:
                if call.name == "unstar":
                    await db.execute(
                        "DELETE FROM collection_favorites WHERE owner_id=? AND library_id=?"
                        " AND item_id=? AND (kind='album' OR ?='id')",
                        [owner, self.library_id, value, key],
                    )
                    continue
                kind = "album"
                if key == "id":
                    cursor = await db.execute(
                        "SELECT 1 FROM collection_tracks WHERE library_id=? AND album_id=?"
                        " AND present=1 LIMIT 1",
                        [self.library_id, value],
                    )
                    kind = "album" if await cursor.fetchone() else "song"
                await db.execute(
                    "INSERT OR IGNORE INTO collection_favorites VALUES (?,?,?,?)",
                    [owner, self.library_id, kind, value],
                )

    async def due(self, principal: Principal, call: RestCall) -> None:
        """A verified app request of this account reads the library: its favorites are
        read when due, with that request's credentials (``getStarred2``)."""
        read = _read_call(call)
        await self.due_with(principal.user_id, lambda: self._getstarred(read))

    async def due_with(self, owner: str, starred: Starred) -> None:
        """Read the account's favorites with ``starred`` - as the account, with the
        credentials or native sign-in of its own verified request - when they are due:
        beside the request, unless they were never read (then waited for, up to
        ``first_wait``). A read is bounded as a whole (``read_seconds``); a few run at a
        time, and while many accounts wait, more are read later."""
        now = time.monotonic()
        if owner in self._running or now < self._due.get(owner, 0.0):
            return
        if len(self._running) >= WAITING:
            return  # read at one of its later requests
        # Admitted and reserved before anything is awaited: requests of many accounts at
        # once cannot all pass the admission check.
        done = anyio.Event()
        self._running[owner] = done
        self._due[owner] = now + self.interval
        spawned = False
        try:
            first = not await self.store.fetchone(
                "SELECT 1 FROM collection_favorites_read WHERE owner_id=? AND library_id=?",
                [owner, self.library_id],
            )
            spawned = self.spawn(lambda: self._run(owner, starred, done))
        finally:
            if not spawned:  # failed, canceled or stopping: the reservation goes
                self._running.pop(owner, None)
                self._due.pop(owner, None)
                done.set()
        if first:
            with anyio.move_on_after(self.first_wait):
                await done.wait()

    async def _run(self, owner: str, starred: Starred, done: anyio.Event) -> None:
        """One admitted read, bounded as a whole; its reservation ends with it."""
        try:
            with anyio.fail_after(self.read_seconds):
                async with self._reading:
                    await self._read(owner, starred)
        except Exception as exc:
            log.info("favorites not read: %s", type(exc).__name__)
            self._due[owner] = time.monotonic() + min(self.interval, RETRY_SECONDS)
        finally:
            self._running.pop(owner, None)
            done.set()

    async def _getstarred(self, call: RestCall) -> set[tuple[str, str]]:
        answer = await raw_library_answer(self.upstream, call, max_bytes=MAX_BYTES)
        found = await answer.parsed("starred2") if answer is not None else None
        if found is None:
            raise ValueError("no favorites in Navidrome's answer")
        starred = found[1]
        return {
            (kind, str(item["id"]))
            for kind, key in (("song", "song"), ("album", "album"))
            for item in starred.get(key) or []
            if isinstance(item, dict) and item.get("id")
        }

    async def _read(self, owner: str, starred: Starred) -> None:
        changes = self._changes.get(owner, 0)
        self.reads += 1
        rows = await starred()
        async with self.store.transaction() as db:
            if self._changes.get(owner, 0) != changes:
                # A star or unstar came while Navidrome was read: read again soon.
                self._due[owner] = time.monotonic() + min(self.interval, RETRY_SECONDS)
                return
            cursor = await db.execute(
                "SELECT kind,item_id FROM collection_favorites WHERE owner_id=? AND library_id=?",
                [owner, self.library_id],
            )
            known = {(str(r[0]), str(r[1])) for r in await cursor.fetchall()}
            # A favorite is the account's activity too: one Shijhon had not seen (made at
            # Navidrome, or before) is kept to record (``CollectionRequests.drain``) - its
            # catalog album joins the Shijhon library, and stays there after an unstar.
            learned = sorted(rows - known)
            await queue_pending(
                db,
                owner,
                self.library_id,
                [("albumId" if kind == "album" else "songId", item) for kind, item in learned],
                limit=None,  # every one: the next read no longer sees them as new
            )
            await db.execute(
                "DELETE FROM collection_favorites WHERE owner_id=? AND library_id=?",
                [owner, self.library_id],
            )
            await db.executemany(
                "INSERT OR IGNORE INTO collection_favorites VALUES (?,?,?,?)",
                [(owner, self.library_id, kind, item) for kind, item in sorted(rows)],
            )
            await db.execute(
                "INSERT INTO collection_favorites_read VALUES (?,?,?)"
                " ON CONFLICT(owner_id,library_id) DO UPDATE SET read_at=excluded.read_at",
                [owner, self.library_id, time.time()],
            )
        if learned and self.learned is not None:
            self.learned()

    # --- imported playlists ----------------------------------------------------------------

    async def imported(
        self,
        principal: Principal,
        import_id: str,
        song_ids: Sequence[str],
        db: aiosqlite.Connection,
    ) -> None:
        """The songs a playlist import published, in its publication's transaction: they
        replace what the import published before."""
        if not principal.can_access(int(self.library_id)):
            return
        await db.execute(
            "DELETE FROM collection_imported WHERE owner_id=? AND library_id=? AND import_id=?",
            [principal.user_id, self.library_id, import_id],
        )
        await db.executemany(
            "INSERT OR IGNORE INTO collection_imported VALUES (?,?,?,?)",
            [(principal.user_id, self.library_id, import_id, s) for s in dict.fromkeys(song_ids)],
        )


def _read_call(call: RestCall) -> RestCall:
    """``getStarred2`` with the credentials of the account's own request (all libraries:
    the songs of others match no song of this one)."""
    params = [(k, v) for k, v in call.params if k in CREDENTIAL_PARAMS or k in ("v", "c")]
    params.append(("f", "json"))
    headers = [
        (k, v)
        for k, v in call.headers
        if k.lower() not in (b"range", b"content-length", b"content-type")
    ]
    return RestCall.build(
        "getStarred2", "GET", b"/rest/getStarred2", urlencode(params).encode(), headers, None
    )
