# ruff: noqa: S608 -- SQL fragments/identifiers are trusted constants; values are bound.
"""Private, bounded indexes of caller-authenticated enumeration answers.

The native API cannot join a Shijhon collection before LIMIT. Keep its exact ordered
candidate set on disk, then apply the common policy before COUNT/LIMIT. A partial scan
is never published. The cache is disposable; it contains no credentials. Structural
facts come from the collection index, never from another caller's annotated answers.

Reads never wait for this cache's writer: each is a snapshot of its own (WAL), writes
nothing to the cache, and at most ``Budgets.read_workers`` run at a time. What a read would
otherwise repeat for every statement is kept instead, each with what makes it exact: an
account's selection, as its songs, for as long as ``collection_revisions`` stands still; a
generation's visible order for that selection, in memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random as randomness
import secrets
import sqlite3
import threading
import time
import zlib
from array import array
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import anyio

from shijhon.collection.contracts import REAL_AUDIO, IndexState, Selection
from shijhon.collection.policy import predicate, real_audio_predicate
from shijhon.proxy.params import RestCall

Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
Kind = str
QUERY_REVISION = 4  # facts of the configured library are shared between accounts
log = logging.getLogger(__name__)
FACTS_BYTES_PER_SONG = 1024  # measured: about 700 for a song with several credits and tags
T = TypeVar("T")


class IndexPending(Exception):
    def __init__(self, indexed: int = 0, total: int | None = None) -> None:
        self.indexed, self.total = indexed, total
        super().__init__("The selected library view is indexing. Retry shortly.")


class IndexFailed(Exception):
    """A safe failure with no upstream parameters or credentials in its message."""


class _FactsBehind(Exception):
    """The structural facts a read needs have to be written first."""


class _SelectionBehind(Exception):
    """The selection's kept songs are older than what decides them."""


class SetupRequired(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Sign in once at /shijhon/sign-in?next=/shijhon/collection to prepare "
            "your library view. Your existing music and playlists are preserved."
        )


@dataclass(frozen=True)
class Binding:
    owner_id: str
    libraries: frozenset[str]
    generations: tuple[tuple[str, str], ...]
    authority: str = ""

    @property
    def facts(self) -> str:
        """Whose structural facts these are. Those of the configured library come from its
        snapshot alone, whoever asks, and accounts share them; a caller's own answers about
        other libraries (``foreign``) stay that caller's."""
        if not self.foreign:
            return digest((sorted(self.libraries), self.generations))[:24]
        return digest((self.owner_id, self.authority, sorted(self.libraries), self.generations))[
            :24
        ]

    @property
    def scope(self) -> str:
        """Who reads which libraries, whichever snapshot of them: a list answers no other."""
        return digest((self.owner_id, self.authority, sorted(self.libraries)))[:24]

    @property
    def foreign(self) -> frozenset[str]:
        return self.libraries - frozenset(library for library, _ in self.generations)

    def key(self, query: object) -> str:
        """A list's identity. Over a new snapshot of the configured library it stays the
        same list: only its candidates can be out of date, and the current facts and
        selection decide what of them is shown (``QueryIndex.ensure`` refreshes it first)."""
        return digest(
            (
                QUERY_REVISION,
                self.owner_id,
                self.authority,
                sorted(self.libraries),
                self.generations if self.foreign else (),
                query,
            )
        )


@dataclass(frozen=True)
class SourcePage:
    rows: Sequence[Mapping[str, Any]]
    total: int | None = None
    envelope: Mapping[str, Any] | None = None
    complete: bool = False


@dataclass(frozen=True)
class Page:
    rows: list[dict[str, Any]]
    total: int
    envelope: dict[str, Any]
    indexed_at: float
    duration: float | None = None
    all_selected: bool = False
    # With ``whole_albums``: album -> its original songs, for each album listed with all of
    # them (an album page's saved plan is checked against them).
    original_songs: Mapping[str, frozenset[str]] | None = None


@dataclass(frozen=True)
class _View:
    """What one read selects by, checked in its snapshot: a condition on ``edges e``, and
    what identifies its result exactly (``None``: nothing of it can be kept)."""

    selected: str
    params: tuple[Any, ...]
    identity: tuple[Any, ...] | None


@dataclass(frozen=True)
class Budgets:
    workers: int = 2
    queries: int = 128
    owner_queries: int = 32
    rows: int = 2_000_000
    bytes: int = 1 << 30
    seconds: float = 120
    fresh_seconds: float = 60
    page_size: int = 500
    native_page_size: int = 5000
    # The longest a request waits for its query's first generation (a maximum, not a
    # delay): apps do not ask again after an answer that the view is being prepared. A
    # search waits as long for the catalog beside it (``[search] budget_seconds``).
    wait_seconds: float = 8.0
    # The same for a list that exists, over a new snapshot of the library: then it answers.
    stale_wait_seconds: float = 3.0
    # Reads running at once, whatever else the cache is doing.
    read_workers: int = 4
    # Selections kept (their songs on disk) and visible orders kept (their rows in memory).
    selections: int = 16
    remembered_rows: int = 4_000_000
    remembered_statistics: int = 200  # items of a page from which its statistics are kept
    # The longest a read waits for its selection to be kept before working it out itself,
    # which takes longer unless the writer is busy with much else.
    selection_wait_seconds: float = 4.0
    # A list that takes this long to read is refreshed for at most this share of the time,
    # unless the library itself changed: a whole-library song list would otherwise be read
    # from Navidrome again and again while it is browsed. Quicker lists are left as they are.
    paced_from_seconds: float = 3.0
    refresh_share: float = 0.05


DEFAULT_BUDGETS = Budgets()


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def annotation_filtered(query: object) -> bool:
    """Queries whose membership/order depends on native per-user annotations."""
    if not isinstance(query, (tuple, list)) or not query:
        return False
    if query[0] in ("getStarred", "getStarred2"):
        return True
    pairs = (
        query[2] if query[0] == "native" and len(query) > 2 else query[1] if len(query) > 1 else ()
    )
    if not isinstance(pairs, (tuple, list)):
        return False
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        name, value = pair
        if name == "type" and value in ("starred", "highest", "frequent", "recent"):
            return True
        if name in ("starred", "rating", "min_rating", "max_rating"):
            return True
        if name == "_sort" and value in ("rating", "play_count", "play_date", "starred_at"):
            return True
    return False


def native_tag_id(name: str, value: str) -> str:
    """Pinned 0.64.2 model/id.NewTagID; identity only, never a security digest."""
    number = int.from_bytes(
        hashlib.md5(
            (name.lower() + "\u200b" + value.lower() + "\u200b").encode(), usedforsecurity=False
        ).digest(),
        "big",
    )
    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = ""
    while number:
        number, digit = divmod(number, 62)
        result = digits[digit] + result
    return result.rjust(22, "0")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
 key TEXT PRIMARY KEY, owner TEXT NOT NULL, facts TEXT NOT NULL, created REAL NOT NULL,
 updated REAL NOT NULL, used REAL NOT NULL, state TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0,
 total INTEGER, envelope TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 annotation_filtered INTEGER NOT NULL DEFAULT 0, scope TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS query_owner ON queries(owner,used);
CREATE TABLE IF NOT EXISTS slots (
 key TEXT PRIMARY KEY, generation TEXT NOT NULL REFERENCES queries(key) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS answers (
 key TEXT NOT NULL REFERENCES queries(key) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, item_id TEXT NOT NULL, document BLOB NOT NULL,
 PRIMARY KEY(key,ordinal), UNIQUE(key,item_id)
);
CREATE TABLE IF NOT EXISTS facts (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS fact_libraries (
 facts TEXT NOT NULL REFERENCES facts(key) ON DELETE CASCADE, library_id TEXT NOT NULL,
 generation TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(facts,library_id)
);
CREATE TABLE IF NOT EXISTS foreign_ready (
 facts TEXT PRIMARY KEY REFERENCES facts(key) ON DELETE CASCADE, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
 facts TEXT NOT NULL REFERENCES facts(key) ON DELETE CASCADE,
 kind TEXT NOT NULL, item_id TEXT NOT NULL, song_id TEXT NOT NULL, library_id TEXT NOT NULL,
 album_id TEXT NOT NULL, duration REAL NOT NULL, size INTEGER NOT NULL,
 year INTEGER NOT NULL, role TEXT NOT NULL,
 PRIMARY KEY(facts,kind,item_id,library_id,song_id,role)
);
CREATE TABLE IF NOT EXISTS selections (
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, library_id TEXT NOT NULL,
 mode TEXT NOT NULL,
 stamp TEXT NOT NULL, computed REAL NOT NULL, UNIQUE(owner,library_id,mode)
);
CREATE TABLE IF NOT EXISTS selected_songs (
 selection INTEGER NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
 song_id TEXT NOT NULL, PRIMARY KEY(selection,song_id)
) WITHOUT ROWID;
"""


class QueryIndex:
    def __init__(
        self, path: Path, collection_path: Path, spawn: Spawn, *, budgets: Budgets = DEFAULT_BUDGETS
    ) -> None:
        self.path, self.collection_path, self.spawn, self.budgets = (
            path,
            collection_path,
            spawn,
            budgets,
        )
        self.lock = anyio.Lock()
        self.scheduling = anyio.Lock()
        self.slots = anyio.CapacityLimiter(budgets.workers)
        self.readers = anyio.CapacityLimiter(budgets.workers)
        self.reads = anyio.CapacityLimiter(budgets.read_workers)
        # generation -> when it was last read, written with the next build (``_flush_used``)
        self.used: dict[str, float] = {}
        self.remembered: OrderedDict[tuple[Any, ...], tuple[Any, int]] = OrderedDict()
        self.remembered_rows = 0
        self.remembering = threading.Lock()
        self._idle: list[sqlite3.Connection] = []
        self._idle_lock = threading.Lock()
        # key -> the monotonic time before which it is not refreshed (``ensure``)
        self.paced: dict[str, float] = {}
        # facts -> the event of their writing; library -> the snapshot its facts are ready for
        self.writing_facts: dict[str, anyio.Event] = {}
        self.prepared: dict[str, str] = {}
        self.progress: dict[str, str] = {}
        self.attempted: dict[str, float] = {}
        self.failed: dict[str, float] = {}
        self.jobs: dict[str, tuple[anyio.Event, anyio.CancelScope]] = {}
        self.retiring: set[tuple[anyio.Event, anyio.CancelScope]] = set()
        self.lanes: dict[str, str] = {}
        # key -> the lanes of the requests waiting for its build ("": a request without one)
        self.waiting: dict[str, list[str]] = {}
        self.annotation_jobs: set[str] = set()
        self.job_owners: dict[str, str] = {}
        self.job_lanes: dict[str, str] = {}  # a search's build -> the lane that began it
        self.opened = False
        self.closed = False
        self.network_pages = 0
        self.keeper: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA main.synchronous=NORMAL")  # disposable cache, never collection state
        conn.create_function(
            "UNPACK", 1, lambda value: zlib.decompress(value).decode(), deterministic=True
        )
        conn.create_function("TAG_ID", 2, native_tag_id, deterministic=True)
        self._limit_size(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "ATTACH DATABASE ? AS collectiondb",
            (self.collection_path.resolve().as_uri() + "?mode=ro",),
        )
        return conn

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        """A connection for one read, kept for the next: opening one and attaching the
        collection takes longer than most reads. No transaction and nothing temporary
        outlives the read: it is rolled back."""
        with self._idle_lock:
            conn = self._idle.pop() if self._idle else None
        if conn is None:
            conn = self._connect()
        try:
            yield conn
        except sqlite3.Error:
            conn.close()
            raise
        except BaseException:
            conn.rollback()  # what stopped the read says nothing about the connection
            self._keep_idle(conn)
            raise
        conn.rollback()
        self._keep_idle(conn)

    def _keep_idle(self, conn: sqlite3.Connection) -> None:
        with self._idle_lock:
            if not self.closed and len(self._idle) < self.budgets.read_workers:
                self._idle.append(conn)
                return
        conn.close()

    def _limit_size(self, conn: sqlite3.Connection) -> None:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count={(self.budgets.bytes - (4 << 20)) // page_size}")

    async def open(self) -> None:
        if self.opened:
            return
        async with self.lock:
            if self.opened:
                return
            await anyio.to_thread.run_sync(self._open)
            self.opened = True

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            # Lists of another revision can never be read (``Binding.key``) and would only
            # fill the cache; it is disposable.
            with closing(sqlite3.connect(self.path)) as conn:
                revision = conn.execute("PRAGMA user_version").fetchone()[0]
            if revision != QUERY_REVISION:
                for part in ("", "-wal", "-shm"):
                    self.path.with_name(self.path.name + part).unlink(missing_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA main.journal_mode=WAL")
            conn.execute(f"PRAGMA main.user_version={QUERY_REVISION}")
            conn.executescript(_SCHEMA)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(queries)")}
            if "annotation_filtered" not in columns:
                conn.execute(
                    "ALTER TABLE queries ADD COLUMN annotation_filtered INTEGER NOT NULL DEFAULT 0"
                )
            if "scope" not in columns:
                conn.execute("ALTER TABLE queries ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
            conn.execute("DELETE FROM queries WHERE state<>'ready'")
            # What they were kept with is only comparable within one database's life.
            conn.execute("DELETE FROM selections")
        # Retain a connection so every 500-row writer does not close the last WAL
        # connection and checkpoint the whole cache. No transaction stays open.
        self.keeper = sqlite3.connect(self.path, check_same_thread=False)

    async def close(self) -> None:
        self.closed = True
        jobs = [*self.jobs.values(), *self.retiring]
        for _, scope in jobs:
            scope.cancel()
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            for event, _ in jobs:
                await event.wait()
        if self.keeper is not None:
            await anyio.to_thread.run_sync(self.keeper.close)
            self.keeper = None
        with self._idle_lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    async def source_state(self, library: str) -> IndexState:
        """Read structural readiness AFTER the adapter proves the current principal.

        This reads no songs, memberships or credentials, and deliberately does not
        require a remembered dashboard connection for a valid REST-only caller.
        """
        await self.open()

        def read() -> IndexState:
            with self._reader() as conn:
                row = conn.execute(
                    "SELECT * FROM collectiondb.collection_index WHERE library_id=?", (library,)
                ).fetchone()
                return (
                    IndexState(**dict(row)) if row else IndexState(library, "", "missing", 0, 0, "")
                )

        return await anyio.to_thread.run_sync(read)

    async def foreign_ready(self, binding: Binding) -> bool:
        await self.open()

        def read() -> bool:
            with self._reader() as conn:
                return (
                    conn.execute(
                        "SELECT 1 FROM foreign_ready WHERE facts=?", (binding.facts,)
                    ).fetchone()
                    is not None
                )

        return await anyio.to_thread.run_sync(read)

    async def invalidate(self, owner: str | None = None) -> None:
        await self.open()
        async with self.lock:

            def invalidate() -> None:
                with closing(self._connect()) as conn, conn:
                    if owner is None:
                        conn.execute("UPDATE queries SET updated=0")
                    else:
                        conn.execute("UPDATE queries SET updated=0 WHERE owner=?", (owner,))

            await anyio.to_thread.run_sync(invalidate)

    async def annotations_changed(self, owner: str, *, every: bool = False) -> None:
        """Retire annotation-filtered views; refresh other views while serving them.
        ``every``: retire all of the owner's views (a star or a rating: the next answer
        shows it; playback only marks the others stale, so browsing stays warm).

        Detach slots, rather than cascading through every cached answer under the
        global writer lock. Obsolete generations are reclaimed by ordinary eviction.
        Publishing checks their state in the same transaction, so old builds cannot
        restore a retired view. Cancellation never waits on another owner's scheduler.
        """
        await self.open()
        async with self.scheduling:

            def retire() -> list[str]:
                with closing(self._connect()) as conn, conn:
                    keys = [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT s.key FROM slots s JOIN queries q ON q.key=s.generation"
                            " WHERE q.owner=?",
                            (owner,),
                        )
                    ]
                    # 1, not 0 (``invalidate``): stale, but the library has not changed.
                    conn.execute("UPDATE queries SET updated=1 WHERE owner=?", (owner,))
                    conn.execute(
                        "UPDATE queries SET state='obsolete' WHERE owner=?"
                        + ("" if every else " AND annotation_filtered=1"),
                        (owner,),
                    )
                    conn.execute(
                        "DELETE FROM slots WHERE generation IN"
                        " (SELECT key FROM queries WHERE owner=? AND state='obsolete')",
                        (owner,),
                    )
                    return keys

            keys = await anyio.to_thread.run_sync(retire)
            for key in keys:
                self.attempted.pop(key, None)
                self.failed.pop(key, None)
            for key, job in list(self.jobs.items()):
                if self.job_owners.get(key) == owner and (every or key in self.annotation_jobs):
                    job[1].cancel()
                    # A new request can build immediately, even during slow cleanup.
                    self.jobs.pop(key, None)
                    self.retiring.add(job)
                    self.job_owners.pop(key, None)
                    self.annotation_jobs.discard(key)
                    self.job_lanes.pop(key, None)
                    self.attempted.pop(key, None)
                    self.failed.pop(key, None)

    async def has_ready(self, binding: Binding, query: object) -> bool:
        await self.open()
        row = await anyio.to_thread.run_sync(self._state, binding.key(query))
        return row is not None and row["state"] == "ready"

    async def ensure(
        self,
        binding: Binding,
        query: object,
        fetch: Callable[[int, int], Awaitable[SourcePage]],
        validate: Callable[[], Awaitable[None]],
        *,
        lane: str = "",
        wait: bool = True,
        structural: bool = False,
        page_size: int | None = None,
        patience: float | None = None,
    ) -> str:
        """The key of the query's generation, once it is ready: waited for at most
        ``budgets.wait_seconds`` (``patience``: as long), else ``IndexPending``."""
        page_size = page_size or self.budgets.page_size
        if not 1 <= page_size <= max(self.budgets.page_size, self.budgets.native_page_size):
            raise IndexFailed("Invalid source page budget.")
        if self.closed:
            raise IndexFailed("The library query service is stopping.")
        await self.open()
        key = binding.key(query)
        async with self.scheduling:
            row = await anyio.to_thread.run_sync(self._state, key)
            if row is None and time.monotonic() - self.failed.get(key, -10000) < 5:
                raise IndexFailed("The private library index is unavailable. Retry shortly.")
            ready = row is not None and row["state"] == "ready"
            # Read over an older snapshot of the library, which has changed since.
            outdated = ready and row is not None and row["facts"] != binding.facts
            if (
                ready
                and not outdated
                and row is not None
                and (time.time() - row["updated"] <= self.budgets.fresh_seconds)
            ):
                return key
            if row is not None and row["state"] == "failed" and time.time() - row["created"] < 5:
                raise IndexFailed("The library index could not be completed. Retry shortly.")
            if ready and time.monotonic() - self.attempted.get(key, -10000) < 5 and not outdated:
                return key
            if (
                ready
                and not outdated
                and row is not None
                and row["updated"] > 0
                and time.monotonic() < self.paced.get(key, 0)
            ):
                return key
            began = key not in self.jobs
            if began:
                self.attempted[key] = time.monotonic()
                if len(self.attempted) > self.budgets.queries:
                    self.attempted.pop(next(iter(self.attempted)))
                if lane:
                    self._supersede(lane, key)
                if len(self.jobs) >= self.budgets.workers * 4:
                    if row is not None and row["state"] == "ready":
                        return key  # retain the complete view even when refresh cannot queue
                    raise IndexPending()
                event, scope = anyio.Event(), anyio.CancelScope()
                self.jobs[key] = (event, scope)
                self.job_owners[key] = binding.owner_id
                filtered = annotation_filtered(query)
                if filtered:
                    self.annotation_jobs.add(key)
                if lane:
                    self.job_lanes[key] = lane

                async def build() -> None:
                    await self._build(
                        key, binding, fetch, validate, event, scope, structural, page_size, filtered
                    )

                self.spawn(build)
            event = self.jobs[key][0]
            if ready and not (outdated and began and wait):
                return key  # same principal/grants/source: refresh in background
            if wait:
                self.waiting.setdefault(key, []).append(lane)
        if wait:
            try:
                with anyio.move_on_after(
                    self.budgets.stale_wait_seconds
                    if outdated
                    else self.budgets.wait_seconds
                    if patience is None
                    else patience
                ):
                    await event.wait()
            finally:
                waiting = self.waiting[key]
                waiting.remove(lane)
                if not waiting:
                    del self.waiting[key]
                    self._abandon(key)
        row = await anyio.to_thread.run_sync(self._state, key)
        if (
            row is not None
            and row["state"] == "ready"
            and (outdated or time.time() - row["updated"] <= self.budgets.fresh_seconds)
        ):
            return key
        if row is not None and row["state"] == "failed":
            raise IndexFailed("The library index could not be completed. Retry shortly.")
        if row is None and key in self.failed:
            raise IndexFailed("The private library index is unavailable. Retry shortly.")
        raise IndexPending(row["rows"] if row else 0, row["total"] if row else None)

    async def moved_on(self, binding: Binding, query: object, lane: str) -> None:
        """``lane``'s newer search is ``query``, answered without a build (``page_many``).
        A burst's searches can be separate lookups, each needed: the lane's previous build
        goes on for the requests waiting for it, and stops once none does (``_abandon``)."""
        async with self.scheduling:
            previous = self.lanes.get(lane)
            self._track(lane, binding.key(query))
            if previous:
                self._abandon(previous)

    def _supersede(self, lane: str, key: str) -> None:
        """The lane's search is now ``key``, which it builds: the listener typed on, so
        the build of its previous search is cancelled - unless a request of another lane
        (or of none) is waiting for that build too; then it stops once none does."""
        previous = self.lanes.get(lane)
        if previous and previous != key:
            old = self.jobs.get(previous)
            if old is not None and all(w == lane for w in self.waiting.get(previous, ())):
                old[1].cancel()
        self._track(lane, key)

    def _track(self, lane: str, key: str) -> None:
        self.lanes[lane] = key
        if len(self.lanes) > self.budgets.queries:
            self.lanes.pop(next(iter(self.lanes)))

    def _abandon(self, key: str) -> None:
        """A search's build stops once its lane has moved on and no request waits for it:
        nobody will read it (apps do not ask again)."""
        lane = self.job_lanes.get(key)
        job = self.jobs.get(key)
        if lane and job and self.lanes.get(lane) != key and not self.waiting.get(key):
            job[1].cancel()

    def _state(self, key: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT q.* FROM slots s JOIN queries q ON q.key=s.generation WHERE s.key=?", (key,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM queries WHERE key=?", (self.progress.get(key, key),)
                ).fetchone()
            return dict(row) if row is not None else None

    async def _build(
        self,
        key: str,
        binding: Binding,
        fetch: Callable[[int, int], Awaitable[SourcePage]],
        validate: Callable[[], Awaitable[None]],
        event: anyio.Event,
        scope: anyio.CancelScope,
        structural: bool,
        page_size: int,
        filtered: bool = False,
    ) -> None:
        temporary = secrets.token_hex(16)
        self.progress[key] = temporary
        try:
            with scope, anyio.fail_after(self.budgets.seconds):
                async with self.slots:
                    began = time.monotonic()
                    await validate()
                    async with self.lock:
                        await anyio.to_thread.run_sync(self._begin, temporary, binding, filtered)
                    offset, expected = 0, None
                    batch: list[SourcePage] = []
                    while True:
                        # Each source read authenticates the original caller upstream.
                        # Recheck immutable principal/grants/source before publication;
                        # no partial scan is ever served between those checks.
                        if not batch:
                            positions = [offset]
                            if expected is not None:
                                positions = list(
                                    range(
                                        offset,
                                        min(expected, offset + page_size * 2),
                                        page_size,
                                    )
                                )
                            received: dict[int, SourcePage] = {}

                            async def read(
                                position: int, received: dict[int, SourcePage] = received
                            ) -> None:
                                async with self.readers:
                                    received[position] = await fetch(position, page_size)
                                    self.network_pages += 1

                            async with anyio.create_task_group() as group:
                                for position in positions:
                                    group.start_soon(read, position)
                            batch = [received[position] for position in positions]
                        source = batch.pop(0)
                        count = len(source.rows)
                        if (
                            count > page_size and not source.complete
                        ) or offset + count > self.budgets.rows:
                            raise IndexFailed(
                                "The library query exceeds the configured index budget."
                            )
                        if source.total is not None:
                            if expected is not None and expected != source.total:
                                raise IndexFailed(
                                    "The library changed while it was indexing. Retry."
                                )
                            expected = source.total
                        async with self.lock:
                            await anyio.to_thread.run_sync(self._append, temporary, offset, source)
                        offset += count
                        if (
                            source.complete
                            or count < page_size
                            or (expected is not None and offset >= expected)
                        ):
                            if expected is not None and offset != expected:
                                raise IndexFailed("The library page and total disagree. Retry.")
                            break
                    await validate()
                    async with self.lock:
                        await anyio.to_thread.run_sync(
                            self._publish, temporary, key, binding, structural
                        )
                    now = time.monotonic()
                    self.paced.pop(key, None)
                    if now - began >= self.budgets.paced_from_seconds:
                        self.paced[key] = now + (now - began) / self.budgets.refresh_share
                        if len(self.paced) > self.budgets.queries:
                            self.paced.pop(next(iter(self.paced)))
        except Exception as exc:
            # The type alone for anything else: an upstream error can name an address.
            log.warning(
                "a library list could not be built: %s",
                exc if isinstance(exc, (sqlite3.Error, IndexFailed)) else type(exc).__name__,
            )
            self.failed[key] = time.monotonic()
            if len(self.failed) > self.budgets.queries:
                self.failed.pop(next(iter(self.failed)))
            async with self.lock:
                # A full/unwritable disposable cache cannot stop the application.
                with suppress(sqlite3.Error, OSError):
                    await anyio.to_thread.run_sync(self._failed, key, binding)
        finally:
            with anyio.CancelScope(shield=True):
                async with self.lock:
                    try:
                        state = await anyio.to_thread.run_sync(self._state, key)
                        if state is None or state["key"] != temporary or state["state"] != "ready":
                            await anyio.to_thread.run_sync(self._discard, temporary)
                    except (sqlite3.Error, OSError):
                        pass
                    if self.jobs.get(key, (None,))[0] is event:
                        self.jobs.pop(key, None)
                        self.job_owners.pop(key, None)
                        self.annotation_jobs.discard(key)
                        self.job_lanes.pop(key, None)
                    if self.progress.get(key) == temporary:
                        self.progress.pop(key, None)
                    self.retiring.discard((event, scope))
                    event.set()

    def _begin(self, key: str, binding: Binding, filtered: bool = False) -> None:
        with closing(self._connect()) as conn, conn:
            self._flush_used(conn)
            # LRU eviction is bounded globally and per immutable owner. Active staging
            # generations count too; on disk no unbounded typeahead history accumulates.
            while (
                conn.execute("SELECT count(*) FROM queries").fetchone()[0] >= self.budgets.queries
            ):
                self._evict(conn)
            while (
                conn.execute(
                    "SELECT count(*) FROM queries WHERE owner=?", (binding.owner_id,)
                ).fetchone()[0]
                >= self.budgets.owner_queries
            ):
                self._evict(conn, binding.owner_id)
            now = time.time()
            conn.execute(
                "INSERT INTO queries(key,owner,facts,created,updated,used,state,"
                "annotation_filtered,scope) "
                "VALUES (?,?,?,?,?,?,'indexing',?,?)",
                (key, binding.owner_id, binding.facts, now, now, now, filtered, binding.scope),
            )

    def _flush_used(self, conn: sqlite3.Connection) -> None:
        used, self.used = self.used, {}
        conn.executemany(
            "UPDATE queries SET used=? WHERE key=?", [(at, key) for key, at in used.items()]
        )

    def _evict(self, conn: sqlite3.Connection, owner: str = "") -> None:
        self._flush_used(conn)
        victim = conn.execute(
            "SELECT q.key FROM queries q WHERE q.state IN ('ready','failed','obsolete') "
            + ("AND q.owner=? " if owner else "")
            + "AND NOT EXISTS (SELECT 1 FROM slots s JOIN json_each(?) j ON j.value=s.key "
            "WHERE s.generation=q.key) ORDER BY q.used LIMIT 1",
            (*((owner,) if owner else ()), json.dumps(list(self.jobs))),
        ).fetchone()
        if victim is None:
            raise IndexFailed("The private library index has reached its capacity. Retry shortly.")
        conn.execute("DELETE FROM queries WHERE key=?", (victim[0],))
        self._drop_unused_facts(conn)

    def _room(self, conn: sqlite3.Connection, incoming: int) -> None:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        maximum = conn.execute("PRAGMA max_page_count").fetchone()[0]
        # Free pages are reusable even if the settled main file retains its high
        # water mark. Reclaim complete LRU generations before hitting SQLITE_FULL.
        while True:
            used = (
                conn.execute("PRAGMA page_count").fetchone()[0]
                - conn.execute("PRAGMA freelist_count").fetchone()[0]
            )
            if used + (incoming + page_size - 1) // page_size <= maximum:
                return
            self._evict(conn)

    def _append(self, key: str, offset: int, page: SourcePage) -> None:
        rows = []
        for rank, item in enumerate(page.rows, offset):
            ident = item.get("id")
            if not isinstance(ident, str) or not ident or len(ident) > 300:
                raise IndexFailed("The library returned an invalid identity.")
            rows.append(
                (
                    key,
                    rank,
                    ident,
                    zlib.compress(json.dumps(dict(item), separators=(",", ":")).encode(), 3),
                )
            )
        with closing(self._connect()) as conn, conn:
            self._room(conn, 65536 + sum(len(row[3]) + 1024 for row in rows) * 2)
            conn.executemany("INSERT INTO answers VALUES (?,?,?,?)", rows)
            conn.execute(
                "UPDATE queries SET rows=?,total=?,envelope=? WHERE key=?",
                (offset + len(rows), page.total, json.dumps(dict(page.envelope or {})), key),
            )
            if (
                conn.execute("PRAGMA page_count").fetchone()[0]
                * conn.execute("PRAGMA page_size").fetchone()[0]
                > self.budgets.bytes
            ):
                raise IndexFailed("The private library index has reached its disk budget.")

    def _publish(
        self, temporary: str, key: str, binding: Binding, structural: bool = False
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute("SELECT state FROM queries WHERE key=?", (temporary,)).fetchone()
            if state is None or state[0] == "obsolete":
                return
            self._facts(conn, binding)
            if structural:
                self._foreign_facts(conn, temporary, binding)
            old = conn.execute("SELECT generation FROM slots WHERE key=?", (key,)).fetchone()
            conn.execute(
                "UPDATE queries SET state='ready',updated=? WHERE key=?", (time.time(), temporary)
            )
            conn.execute("INSERT OR REPLACE INTO slots VALUES (?,?)", (key, temporary))
            if old:
                conn.execute("DELETE FROM queries WHERE key=?", (old[0],))
            self._drop_unused_facts(conn)

    def _foreign_facts(self, conn: sqlite3.Connection, key: str, binding: Binding) -> None:
        # Only this principal's authenticated native source enters these private
        # edges. Shared collection metadata remains configured-library only.
        for library in binding.foreign:
            conn.execute(
                "DELETE FROM edges WHERE facts=? AND library_id=?", (binding.facts, library)
            )
        records = conn.execute("SELECT document FROM answers WHERE key=? ORDER BY ordinal", (key,))
        for record in records:
            song = json.loads(zlib.decompress(record[0]))
            library = str(song.get("libraryId", ""))
            if library not in binding.foreign:
                raise IndexFailed("The native source returned an unexpected library.")
            ident, album = str(song["id"]), str(song.get("albumId", ""))
            relations = {("song", ident, ""), ("album", album, "")}
            for field in ("artistId", "albumArtistId"):
                if song.get(field):
                    relations.add(("artist", str(song[field]), ""))
            for role, people in (song.get("participants") or {}).items():
                for person in people:
                    if person.get("id"):
                        relations.add(("artist", str(person["id"]), role))
            for name, tags in (song.get("tags") or {}).items():
                for tag in tags:
                    if isinstance(tag, str):
                        relations.add(("tag", native_tag_id(name, tag), ""))
                    elif isinstance(tag, dict) and tag.get("id"):
                        relations.add(("tag", str(tag["id"]), ""))
            for genre in song.get("genres") or []:
                relations.add(("genre", str(genre.get("name", "")), ""))
                if genre.get("id"):
                    relations.add(("tag", str(genre["id"]), ""))
            conn.executemany(
                "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        binding.facts,
                        kind,
                        item,
                        ident,
                        library,
                        album,
                        song.get("duration", 0),
                        song.get("size", 0),
                        song.get("year", 0),
                        role,
                    )
                    for kind, item, role in relations
                    if item
                ],
            )
        conn.execute(
            "INSERT OR REPLACE INTO foreign_ready VALUES (?,?)", (binding.facts, time.time())
        )

    def _live_selection(self, binding: Binding, mode: Selection | None) -> tuple[str, list[Any]]:
        """The selection worked out by the policy for every song it is asked about."""
        clauses: list[str] = []
        params: list[Any] = []
        for library in sorted(binding.libraries):
            if library in binding.foreign:
                clauses.append("e.library_id=?")
                params.append(library)
                continue
            if mode == REAL_AUDIO:
                p = real_audio_predicate(
                    library, song_expression="e.song_id", database="collectiondb"
                )
            else:
                p = predicate(
                    binding.owner_id,
                    library,
                    song_expression="e.song_id",
                    database="collectiondb",
                    mode=mode,
                )
            clauses.append("(e.library_id=? AND " + p.sql + ")")
            params.extend((library, *p.params))
        return " OR ".join(clauses) or "0", params

    def _failed(self, key: str, binding: Binding) -> None:
        with closing(self._connect()) as conn, conn:
            if conn.execute("SELECT 1 FROM slots WHERE key=?", (key,)).fetchone():
                return  # preserve the last complete same-authority generation
            conn.execute(
                (
                    "INSERT OR REPLACE INTO "
                    "queries(key,owner,facts,created,updated,used,state,error) "
                    "VALUES (?,?,?,?,?,?,'failed','Index failed')"
                ),
                (key, binding.owner_id, binding.facts, time.time(), 0, time.time()),
            )

    def _discard(self, temporary: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM queries WHERE key=?", (temporary,))

    @staticmethod
    def _drop_unused_facts(conn: sqlite3.Connection) -> None:
        """Facts no list reads, unless they are the configured library's current ones, which
        a read needs next and which no list has to hold on to."""
        current = [
            Binding("", frozenset({str(row[0])}), ((str(row[0]), str(row[1])),)).facts
            for row in conn.execute(
                "SELECT library_id,generation FROM collectiondb.collection_index"
                " WHERE generation<>''"
            )
        ]
        conn.execute(
            "DELETE FROM facts WHERE key NOT IN (SELECT facts FROM queries)"
            " AND key NOT IN (SELECT value FROM json_each(?))",
            (json.dumps(current),),
        )

    def _facts(self, conn: sqlite3.Connection, binding: Binding) -> None:
        moved_on: set[str] = set()
        for library, generation in binding.generations:
            current = conn.execute(
                "SELECT generation FROM collectiondb.collection_index WHERE library_id=?",
                (library,),
            ).fetchone()
            if current is None or current[0] != generation:
                # Facts of the snapshot before stay readable; none are written for it now.
                if not conn.execute(
                    "SELECT 1 FROM fact_libraries WHERE facts=? AND library_id=? AND generation=?",
                    (binding.facts, library, generation),
                ).fetchone():
                    raise IndexPending()
                moved_on.add(library)
        if conn.execute("SELECT 1 FROM facts WHERE key=?", (binding.facts,)).fetchone():
            for library, generation in binding.generations:
                if library not in moved_on:
                    self._reconcile(conn, binding.facts, library, generation)
            return
        # Older lists give way: new facts must fit, or nothing can be read at all.
        songs = sum(
            conn.execute(
                "SELECT count(*) FROM collectiondb.collection_tracks"
                " WHERE library_id=? AND present=1",
                (library,),
            ).fetchone()[0]
            for library, _ in binding.generations
        )
        self._room(conn, songs * FACTS_BYTES_PER_SONG)
        conn.execute("INSERT INTO facts VALUES (?)", (binding.facts,))
        for library, generation in binding.generations:
            revision = self._revision(conn, library, generation)
            self._edges(conn, binding.facts, library)
            conn.execute(
                "INSERT OR REPLACE INTO fact_libraries VALUES (?,?,?,?)",
                (binding.facts, library, generation, revision),
            )

    @staticmethod
    def _revision(conn: sqlite3.Connection, library: str, generation: str) -> int:
        """The last addition to a library's snapshot in place (0: none)."""
        row = conn.execute(
            "SELECT COALESCE(max(revision),0) FROM collectiondb.collection_index_additions"
            " WHERE library_id=? AND generation=?",
            (library, generation),
        ).fetchone()
        return int(row[0])

    def _reconcile(
        self, conn: sqlite3.Connection, facts: str, library: str, generation: str
    ) -> None:
        """The facts of a library's snapshot take in the additions made to it in place since
        they were built or last reconciled (``Index.add``), as recorded durably: a stop
        between an addition and its extension leaves nothing out for good."""
        stored = conn.execute(
            "SELECT generation,revision FROM fact_libraries WHERE facts=? AND library_id=?",
            (facts, library),
        ).fetchone()
        since = int(stored[1]) if stored is not None and stored[0] == generation else 0
        current = self._revision(conn, library, generation)
        if stored is not None and stored[0] == generation and current <= since:
            return
        songs = [
            str(row[0])
            for row in conn.execute(
                "SELECT song_id FROM collectiondb.collection_index_additions"
                " WHERE library_id=? AND generation=? AND revision>? AND revision<=?",
                (library, generation, since, current),
            )
        ]
        for start in range(0, len(songs), 500):
            self._edges(conn, facts, library, songs[start : start + 500])
        conn.execute(
            "INSERT OR REPLACE INTO fact_libraries VALUES (?,?,?,?)",
            (facts, library, generation, current),
        )

    def _edges(
        self,
        conn: sqlite3.Connection,
        facts: str,
        library: str,
        songs: Sequence[str] | None = None,
    ) -> None:
        """The structural edges of a library's snapshot, as the collection's present songs
        are (``songs``: of those songs only)."""
        only = "" if songs is None else " AND t.song_id IN (SELECT value FROM json_each(?))"
        base = " FROM collectiondb.collection_tracks t WHERE t.library_id=? AND t.present=1" + only
        args: tuple[Any, ...] = (facts, library)
        if songs is not None:
            args = (*args, json.dumps(list(songs)))
        prefix = "INSERT OR IGNORE INTO edges SELECT ?,"
        tail = ",t.song_id,t.library_id,t.album_id,t.duration,t.size,t.year,''"
        conn.execute(prefix + "'song',t.song_id" + tail + base, args)
        conn.execute(prefix + "'album',t.album_id" + tail + base, args)
        for expression in ("t.artist_id", "json_extract(t.metadata,'$.albumArtistId')"):
            conn.execute(
                prefix
                + "'artist',"
                + expression
                + tail
                + base
                + " AND COALESCE("
                + expression
                + ",'')<>''",
                args,
            )
        conn.execute(
            prefix
            + (
                "'artist',json_extract(p.value,'$.id'),t.song_id,t.library_id,t.a"
                "lbum_id,t.duration,t.size,t.year,roles.key FROM "
                "collectiondb.collection_tracks "
                "t,json_each(t.metadata,'$.participants') "
                "roles,json_each(roles.value) p WHERE t.library_id=? AND "
                "t.present=1 AND json_extract(p.value,'$.id') "
                "IS NOT NULL" + only
            ),
            args,
        )
        conn.execute(
            prefix + "'tag',CASE WHEN v.type='text' THEN TAG_ID(tags.key,v.value) "
            "ELSE json_extract(v.value,'$.id') END"
            + tail
            + (
                " FROM collectiondb.collection_tracks "
                "t,json_each(t.metadata,'$.tags') tags,json_each(tags.value) v "
                "WHERE t.library_id=? AND t.present=1 AND "
                "(v.type='text' OR json_extract(v.value,'$.id') IS NOT NULL)" + only
            ),
            args,
        )
        conn.execute(
            prefix
            + "'tag',json_extract(v.value,'$.id')"
            + tail
            + (
                " FROM collectiondb.collection_tracks t,json_each(t.metadata,'$.genres') v "
                "WHERE t.library_id=? AND t.present=1 "
                "AND json_extract(v.value,'$.id') IS NOT NULL" + only
            ),
            args,
        )
        conn.execute(
            prefix
            + "'genre',json_extract(v.value,'$.name')"
            + tail
            + (
                " FROM collectiondb.collection_tracks "
                "t,json_each(t.metadata,'$.genres') v WHERE t.library_id=? AND "
                "t.present=1 AND "
                "json_extract(v.value,'$.name') IS NOT NULL" + only
            ),
            args,
        )

    async def prepare(self, library: str, generation: str) -> None:
        """A new snapshot of the configured library: its facts now, rather than inside the
        next request of whoever comes first."""
        await self.open()
        binding = Binding("", frozenset({library}), ((library, generation),))
        await self._write_facts_once(binding).wait()

    async def usable_generation(self, library: str, generation: str) -> str:
        """The snapshot of the configured library that reads of it use, ``generation`` being
        the current one. Writing a new snapshot's facts takes a pass over the whole library:
        meanwhile reads go on with the last complete one, under today's selection (a removed
        song is gone already, a new one comes with its facts). Only without any do they wait."""
        if self.prepared.get(library) == generation:
            return generation
        await self.open()

        def ready() -> list[str]:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT facts,generation FROM fact_libraries WHERE library_id=? "
                    "ORDER BY rowid DESC",
                    (library,),
                ).fetchall()
            return [
                str(row["generation"])
                for row in rows
                if row["facts"]
                == Binding("", frozenset({library}), ((library, row["generation"]),)).facts
            ]

        complete = await anyio.to_thread.run_sync(ready)
        if generation in complete:
            self.prepared[library] = generation
            return generation
        self._write_facts_once(Binding("", frozenset({library}), ((library, generation),)))
        return complete[0] if complete else generation

    def _write_facts_once(self, binding: Binding) -> anyio.Event:
        """The event of these facts' writing, beside the requests that wait for it."""
        event = self.writing_facts.get(binding.facts)
        if event is not None:
            return event
        event = self.writing_facts[binding.facts] = anyio.Event()

        async def write() -> None:
            try:
                async with self.lock:
                    await anyio.to_thread.run_sync(self._write_facts, binding)
            except IndexPending:
                pass  # the library moved on: the next read asks again
            except (sqlite3.Error, OSError, IndexFailed) as exc:
                # The next read asks again; a disposable cache cannot stop the application.
                log.warning("a library snapshot's facts could not be written: %s", exc)
            finally:
                del self.writing_facts[binding.facts]
                event.set()

        self.spawn(write)
        return event

    async def extend(self, library: str, generation: str) -> None:
        """Songs were added to a library's snapshot in place (``Index.add``): every set of
        facts bound to that snapshot - as its binding declared it - takes them in now
        (each would before its next read anyway: ``_reconcile``), and every list is
        refreshed beside its next request, which it keeps answering meanwhile. No list
        has to be prepared again from nothing."""
        await self.open()
        async with self.lock:

            def extend() -> None:
                with closing(self._connect()) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    keys = [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT facts FROM fact_libraries WHERE library_id=? AND generation=?",
                            (library, generation),
                        )
                    ]
                    for key in keys:
                        self._reconcile(conn, key, library, generation)

            await anyio.to_thread.run_sync(extend)
        await self.invalidate()

    async def page(
        self,
        key: str,
        binding: Binding,
        kind: str,
        mode: Selection | None,
        offset: int,
        limit: int,
        *,
        random: bool = False,
        random_seed: str = "",
        count_sort: str = "",
        descending: bool = False,
        direct_album: str = "",
        song_filters: RestCall | None = None,
        artist_role: str = "",
        artist_count_role: str = "",
        verify_songs: Sequence[str] = (),
        preserve_album_metadata: bool = False,
        whole_albums: bool = False,
    ) -> Page:
        """``whole_albums``: an album listed with all of its original songs while some
        songs Shijhon added to it (a fill's placeholders, catalog songs) are not selected
        keeps Navidrome's own entry, counting the whole album; the page tells the original
        songs of each album listed with all of them (``_whole_albums``)."""
        return await self._read(
            binding,
            mode,
            lambda conn, view: self._page(
                conn,
                view,
                key,
                binding,
                kind,
                mode,
                offset,
                limit,
                random=random,
                count_sort=count_sort,
                descending=descending,
                direct_album=direct_album,
                song_filters=song_filters,
                random_seed=random_seed,
                artist_role=artist_role,
                artist_count_role=artist_count_role,
                verify_songs=verify_songs,
                preserve_album_metadata=preserve_album_metadata,
                whole_albums=whole_albums,
            ),
        )

    def _page(
        self,
        conn: sqlite3.Connection,
        view: _View,
        key: str,
        binding: Binding,
        kind: str,
        mode: Selection | None,
        offset: int,
        limit: int,
        *,
        random: bool = False,
        count_sort: str = "",
        descending: bool = False,
        direct_album: str = "",
        song_filters: RestCall | None = None,
        random_seed: str = "",
        artist_role: str = "",
        artist_count_role: str = "",
        verify_songs: Sequence[str] = (),
        preserve_album_metadata: bool = False,
        whole_albums: bool = False,
        schema: str = "main",
    ) -> Page:
        """``schema``: "temp" reads a generation of the connection's own temporary tables
        (``_stage``), "main" one the cache keeps."""
        if kind not in ("song", "album", "artist", "genre", "tag", "directory"):
            raise IndexFailed("Unsupported library index kind.")
        if schema not in ("main", "temp"):
            raise IndexFailed("Unsupported library index schema.")
        if kind == "album" and preserve_album_metadata:
            # Native album rows keep their complete metadata and the source's order
            # by those same fields; selection only controls inclusion and paging.
            count_sort = ""
        selected, params = view.selected, list(view.params)
        answers = schema + ".answers"
        query = conn.execute(
            (
                f"SELECT q.* FROM {schema}.slots s JOIN {schema}.queries q "
                "ON q.key=s.generation "
                "WHERE s.key=? AND q.owner=? AND q.scope=? AND q.state='ready'"
                + (" AND q.facts=?" if binding.foreign else "")
            ),
            (
                key,
                binding.owner_id,
                binding.scope,
                *((binding.facts,) if binding.foreign else ()),
            ),
        ).fetchone()
        if query is None:
            raise IndexPending()
        # Explicit album navigation can discover an unsaved album. A partially
        # selected album always uses the selected tracks and matching totals. Real
        # audio only discovers nothing: an album without any is not there.
        if direct_album and kind == "directory":
            first = conn.execute(
                f"SELECT document FROM {answers} WHERE key=? ORDER BY ordinal LIMIT 1",
                (query["key"],),
            ).fetchone()
            if first and json.loads(zlib.decompress(first[0])).get("isDir"):
                direct_album = ""
        if direct_album and mode != REAL_AUDIO:
            has = conn.execute(
                "SELECT 1 FROM edges e WHERE e.facts=? AND e.kind='album' AND e.item_id=? AND ("
                + selected
                + ") LIMIT 1",
                (binding.facts, direct_album, *params),
            ).fetchone()
            if has is None:
                selected, params = "1", []
        order = self._order(
            conn,
            view,
            query["key"],
            binding,
            kind,
            selected,
            params,
            answers,
            count_sort=count_sort,
            descending=descending,
            song_filters=song_filters,
            shuffle_seed=random_seed if random else "",
            artist_role=artist_role,
            remember=schema == "main",
        )
        total = len(order)
        all_selected = total == query["rows"]
        if all_selected and verify_songs:
            # An exact native request may have different ties at another LIMIT.
            # Check its actual returned IDs in this same policy snapshot too;
            # unknown/new or disabled rows never enter a raw-answer shortcut.
            if len(verify_songs) > self.budgets.page_size:
                raise IndexFailed("The native page exceeds its verification budget.")
            ids = set(verify_songs)
            found = conn.execute(
                "SELECT COUNT(DISTINCT e.song_id) FROM edges e WHERE e.facts=? "
                "AND e.kind='song' AND e.song_id IN (SELECT value FROM json_each(?)) AND ("
                + selected
                + ")",
                (binding.facts, json.dumps(sorted(ids)), *params),
            ).fetchone()[0]
            all_selected = found == len(ids)
        if random and not random_seed and not self._sorted_by_count(kind, count_sort):
            ordinals = randomness.sample(order, min(limit, max(0, total - offset)))
        else:
            ordinals = list(order[offset : offset + limit])
        rows = {
            row["ordinal"]: row
            for row in conn.execute(
                f"SELECT ordinal,item_id,document FROM {answers} WHERE key=? "
                "AND ordinal IN (SELECT value FROM json_each(?))",
                (query["key"], json.dumps(ordinals)),
            )
        }
        documents = [rows[ordinal] for ordinal in ordinals]
        whole: set[str] = set()
        originals: dict[str, frozenset[str]] | None = None
        if whole_albums and kind == "album":
            whole, originals = (
                self._whole_albums(
                    conn, binding, [str(row["item_id"]) for row in documents], selected, params
                )
                if documents
                else (set(), {})
            )
        items = [json.loads(zlib.decompress(row["document"])) for row in documents]
        counted = [
            kind != "song"
            and not (kind == "album" and (preserve_album_metadata or row["item_id"] in whole))
            for row in documents
        ]
        wanted: dict[str, list[str]] = {}
        for item, counts in zip(items, counted, strict=True):
            if counts:
                wanted.setdefault(self._stat_kind(kind, item), []).append(self._stat_id(kind, item))
        statistics = {
            stat_kind: self._page_stats(
                conn, view, binding, stat_kind, idents, selected, params, schema
            )
            for stat_kind, idents in wanted.items()
        }
        for item, counts in zip(items, counted, strict=True):
            if not counts:
                continue
            stats = statistics[self._stat_kind(kind, item)].get(self._stat_id(kind, item), {})
            complete = item.get("songCount") == stats.get("songCount")
            for field in ("songCount", "albumCount", "duration", "size"):
                if field in item:
                    if field != "songCount" and complete:
                        continue  # complete selected native metadata stays exact
                    value = stats.get(field, 0)
                    item[field] = int(value) if isinstance(item[field], int) else value
            if kind == "artist" and artist_count_role and "albumCount" in item:
                item["albumCount"] = (
                    stats.get("stats", {}).get(artist_count_role, {}).get("albumCount", 0)
                )
            if kind == "artist" and "stats" in item:
                item["stats"] = stats.get("stats", {})
        if schema == "main":
            self.used[query["key"]] = time.time()
        duration = None
        if direct_album and kind == "song":
            duration = (
                self._stats_of(conn, binding, "album", [direct_album], selected, params)
                .get(direct_album, {})
                .get("duration", 0)
            )
        return Page(
            items,
            total,
            json.loads(query["envelope"]),
            query["updated"],
            duration,
            all_selected,
            originals,
        )

    def _page_stats(
        self,
        conn: sqlite3.Connection,
        view: _View,
        binding: Binding,
        kind: str,
        idents: Sequence[str],
        selected: str,
        params: Sequence[Any],
        schema: str,
    ) -> dict[str, dict[str, Any]]:
        """The statistics of a page's items. Those of a whole directory, which apps ask for
        every time they open and which takes a pass over most of the facts, are kept."""

        def read() -> dict[str, dict[str, Any]]:
            return self._stats_of(conn, binding, kind, idents, selected, params)

        if (
            schema != "main"
            or view.identity is None
            or len(idents) < self.budgets.remembered_statistics
        ):
            return read()
        return self._remembered(
            ("statistics", view.identity, kind, selected == "1", digest(sorted(idents))), 64, read
        )

    @staticmethod
    def _stat_kind(kind: str, item: Mapping[str, Any]) -> str:
        return ("album" if item.get("isDir") else "song") if kind == "directory" else kind

    @staticmethod
    def _stat_id(kind: str, item: Mapping[str, Any]) -> str:
        return str(item.get("name", item["id"]) if kind == "genre" else item["id"])

    @staticmethod
    def _sorted_by_count(kind: str, count_sort: str) -> bool:
        return kind != "song" and count_sort.removeprefix("maincredit_") in (
            "songCount",
            "albumCount",
            "duration",
            "size",
        )

    def _order(
        self,
        conn: sqlite3.Connection,
        view: _View,
        generation: str,
        binding: Binding,
        kind: str,
        selected: str,
        params: Sequence[Any],
        answers: str,
        *,
        count_sort: str,
        descending: bool,
        song_filters: RestCall | None,
        shuffle_seed: str,
        artist_role: str,
        remember: bool,
    ) -> Sequence[int]:
        """The ordinals of a generation's items this selection shows, in the order asked
        for. A page, its total and a random pick all read from it, so walking a list costs
        each page its own rows; it is kept for as long as what it comes from stands."""
        extra = ""
        extra_params: list[Any] = []
        if song_filters is not None:
            for parameter, operator in (("fromYear", ">="), ("toYear", "<=")):
                try:
                    year = int(song_filters.get(parameter) or "0")
                except ValueError:
                    year = 0
                if year:
                    extra += " AND e.year" + operator + "?"
                    extra_params.append(year)
            genre = song_filters.get("genre")
            if genre:
                # Navidrome resolves genre names with SQLite LIKE. Materialize
                # matching song/library pairs once before selection and shuffle.
                extra += (
                    " AND (e.song_id,e.library_id) IN ("
                    "SELECT eg.song_id,eg.library_id FROM edges eg "
                    "WHERE eg.facts=? AND eg.kind='genre' AND eg.item_id LIKE ?)"
                )
                extra_params.extend((binding.facts, genre))
        relation_kind = "genre" if kind == "genre" else kind
        relation = "e.item_id=r.item_id"
        if kind == "genre":
            # Native genre carries a tag ID; REST getGenres uses the name.
            relation = (
                "(e.item_id=json_extract(UNPACK(r.document),'$.name') OR e.item_id=r.item_id)"
            )
        edge_filter = "kind=?"
        candidate_extra = ""
        candidate_params: tuple[Any, ...] = ()
        if kind == "artist" and artist_role:
            role_filter = (
                "e.role IN ('artist','albumartist')" if artist_role == "maincredit" else "e.role=?"
            )
            candidate_extra = (
                " AND EXISTS (SELECT 1 FROM edges e WHERE e.facts=? AND e.kind='artist' "
                "AND e.item_id=r.item_id AND " + role_filter + " AND (" + selected + "))"
            )
            candidate_params = (
                binding.facts,
                *((artist_role,) if artist_role != "maincredit" else ()),
                *params,
            )
        if kind == "directory":
            edge_filter = "kind IN ('song','album')"
            relation = (
                "e.item_id=r.item_id AND e.kind=CASE WHEN "
                "json_extract(UNPACK(r.document),'$.isDir') THEN 'album' ELSE "
                "'song' END"
            )
        by_count = self._sorted_by_count(kind, count_sort)
        sort = "ordinal"
        if by_count:
            maincredit = count_sort.startswith("maincredit_")
            count_sort = count_sort.removeprefix("maincredit_")
            matching = "e.role IN ('artist','albumartist')" if maincredit else "1"
            size_match = matching if maincredit else "e.role<>''" if kind == "artist" else "1"
            sort = (
                {"songCount": "songs", "albumCount": "albums"}.get(count_sort, count_sort)
                + (" DESC" if descending else " ASC")
                + ",ordinal"
            )
            statement = (
                "WITH visible AS (SELECT r.ordinal,r.item_id,COUNT(DISTINCT CASE WHEN "
                + matching
                + " THEN e.song_id END) AS "
                "songs,COUNT(DISTINCT CASE WHEN "
                + matching
                + " THEN e.album_id END) AS albums,SUM(e.duration) AS "
                "duration,SUM(CASE WHEN "
                + size_match
                + " THEN e.size ELSE 0 END) AS size FROM "
                + answers
                + " r JOIN (SELECT "
                "DISTINCT "
                "kind,item_id,song_id,library_id,album_id,duration,size,year"
                + (",role" if kind == "artist" else "")
                + " FROM edges WHERE facts=? AND "
                + edge_filter
                + ") e ON "
                + relation
                + " WHERE r.key=? AND ("
                + selected
                + ")"
                + extra
                + candidate_extra
                + " GROUP BY r.ordinal) "
            )
            args = (
                binding.facts,
                *(() if kind == "directory" else (relation_kind,)),
                generation,
                *params,
                *extra_params,
                *candidate_params,
            )
        else:
            # Unary + prevents SQLite 3.51's EXISTS-to-JOIN rewrite from counting a visible
            # item once for each of its matching songs. Native candidate order stays intact.
            statement = (
                "WITH visible AS (SELECT r.ordinal,r.item_id FROM "
                + answers
                + " r WHERE r.key=? AND +EXISTS ("
                "SELECT 1 FROM edges e WHERE e.facts=? AND "
                + edge_filter
                + " AND "
                + relation
                + " AND ("
                + selected
                + ")"
                + extra
                + ")"
                + candidate_extra
                + ") "
            )
            args = (
                generation,
                binding.facts,
                *(() if kind == "directory" else (relation_kind,)),
                *params,
                *extra_params,
                *candidate_params,
            )
            if shuffle_seed:
                conn.create_function(
                    "SHUFFLE",
                    1,
                    lambda ident: hashlib.sha256((shuffle_seed + "\0" + ident).encode()).digest(),
                    deterministic=True,
                )
                sort = "SHUFFLE(item_id)" + (" DESC" if descending else " ASC")

        def read() -> array[int]:
            return array(
                "I",
                (
                    row[0]
                    for row in conn.execute(
                        statement + "SELECT ordinal FROM visible ORDER BY " + sort, args
                    )
                ),
            )

        if not remember or view.identity is None:
            return read()
        return self._remembered(
            (generation, view.identity, selected == "1", statement, sort, shuffle_seed, *args),
            1,
            read,
        )

    def _remembered(self, key: tuple[Any, ...], weight: int, read: Callable[[], T]) -> T:
        """``read``'s value, kept under ``key``, which identifies it exactly. Each read works
        it out by itself until one has kept it. ``weight``: its rows, by how many times a
        visible order's row one of them takes up in memory."""
        with self.remembering:
            found = self.remembered.get(key)
            if found is not None:
                self.remembered.move_to_end(key)
                return found[0]  # type: ignore[no-any-return]
        value = read()
        cost = len(value) * weight  # type: ignore[arg-type]
        with self.remembering:
            if key not in self.remembered and cost <= self.budgets.remembered_rows:
                self.remembered[key] = (value, cost)
                self.remembered_rows += cost
                while (
                    self.remembered_rows > self.budgets.remembered_rows
                    or len(self.remembered) > 256
                ):
                    self.remembered_rows -= self.remembered.popitem(last=False)[1][1]
        return value

    async def live_page(
        self,
        binding: Binding,
        mode: Selection | None,
        kind: str,
        source: SourcePage,
        *,
        direct_album: str = "",
        artist_count_role: str = "",
    ) -> Page:
        """Apply the same SQL to one complete bounded, fresh caller detail answer.

        This is never a paginated-library shortcut. Direct albums/directories already
        arrive whole under the transport byte cap. Temporary tables keep annotations
        fresh without publishing or persisting another native query generation.
        """
        if not source.complete or len(source.rows) > self.budgets.rows:
            raise IndexFailed("The direct native answer is incomplete.")

        def read(conn: sqlite3.Connection, view: _View) -> Page:
            self._stage(conn, binding, "live", source)
            return self._page(
                conn,
                view,
                "live",
                binding,
                kind,
                mode,
                0,
                len(source.rows),
                direct_album=direct_album,
                artist_count_role=artist_count_role,
                schema="temp",
            )

        return await self._read(binding, mode, read)

    @staticmethod
    def _stage(conn: sqlite3.Connection, binding: Binding, key: str, source: SourcePage) -> None:
        """``source`` as the ready generation ``key`` of this connection's own temporary
        tables (gone with it), read with the same SQL (``_page``, ``schema="temp"``)."""
        for table in ("queries", "answers", "slots"):
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS "
                + table
                + " AS SELECT * FROM main."
                + table
                + " WHERE 0"
            )
        now = time.time()
        conn.execute(
            "INSERT INTO temp.queries(key,owner,facts,created,updated,used,state,rows,envelope,"
            "scope) VALUES (?,?,?,?,?,?,'ready',?,?,?)",
            (
                key,
                binding.owner_id,
                binding.facts,
                now,
                now,
                now,
                len(source.rows),
                json.dumps(source.envelope or {}),
                binding.scope,
            ),
        )
        conn.execute("INSERT INTO temp.slots VALUES (?,?)", (key, key))
        conn.executemany(
            "INSERT INTO temp.answers VALUES (?,?,?,?)",
            [
                (
                    key,
                    i,
                    str(row["id"]),
                    # Read once, in this connection: stored, not worth compressing.
                    zlib.compress(json.dumps(dict(row), separators=(",", ":")).encode(), 0),
                )
                for i, row in enumerate(source.rows)
            ],
        )

    async def count(self, binding: Binding, mode: Selection | None) -> int:
        stats = await self.stats(binding, mode, "song", "")
        return int(stats.get("songCount", 0))

    async def stats(
        self, binding: Binding, mode: Selection | None, kind: str, ident: str
    ) -> dict[str, Any]:
        """The statistics of an item's selected songs (``ident`` empty: of all songs of the
        kind); ``{}``: none of them is selected."""
        found = await self._read(
            binding,
            mode,
            lambda conn, view: self._stats_of(
                conn, binding, kind, [ident] if ident else None, view.selected, view.params
            ),
        )
        return found.get(ident, {})

    async def stats_many(
        self, binding: Binding, mode: Selection | None, kind: str, idents: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """``stats`` of several items in one read snapshot: each item the index has, with
        the statistics of its selected songs (``{}``: none of them is selected). An item
        the index does not have is left out."""
        if len(idents) > self.budgets.native_page_size:
            raise IndexFailed("The answer exceeds its statistics budget.")

        def read(conn: sqlite3.Connection, view: _View) -> dict[str, dict[str, Any]]:
            known = self._stats_of(conn, binding, kind, idents, "1", ())
            shown = self._stats_of(conn, binding, kind, list(known), view.selected, view.params)
            return {ident: shown.get(ident, {}) for ident in idents if ident in known}

        return await self._read(binding, mode, read)

    async def shown(
        self, binding: Binding, mode: Selection | None, kind: str, idents: Sequence[str]
    ) -> frozenset[str]:
        """Of these songs, albums or artists, those the selection shows - as its lists and
        searches select them: in the library index with a selected song. One local read, the
        IDs bound as one value; no Navidrome request."""
        if kind not in ("song", "album", "artist"):
            raise IndexFailed("Unsupported library index kind.")
        wanted = list(dict.fromkeys(idents))
        if not wanted:
            return frozenset()
        if len(wanted) > self.budgets.page_size:
            raise IndexFailed("The answer exceeds its selection budget.")

        def read(conn: sqlite3.Connection, view: _View) -> frozenset[str]:
            rows = conn.execute(
                "SELECT DISTINCT e.item_id FROM edges e WHERE e.facts=? AND e.kind=? "
                "AND e.item_id IN (SELECT value FROM json_each(?)) AND (" + view.selected + ")",
                (binding.facts, kind, json.dumps(wanted), *view.params),
            ).fetchall()
            return frozenset(str(row[0]) for row in rows)

        return await self._read(binding, mode, read)

    def _whole_albums(
        self,
        conn: sqlite3.Connection,
        binding: Binding,
        albums: Sequence[str],
        selected: str,
        params: Sequence[Any],
    ) -> tuple[set[str], dict[str, frozenset[str]]]:
        """Of a page's ``albums``: (those listed with every original song while a song
        Shijhon added - a fill's placeholder, a catalog song - is left out; the original
        songs - owned or imported files, not added by Shijhon - of each album listed with
        all of them). One query in the page's snapshot, the IDs bound as one value."""
        found = conn.execute(
            "SELECT item_id,json_group_array(CASE WHEN original=1 THEN song_id END) songs,"
            "SUM((1-original)*(1-chosen)) left_out FROM (SELECT e.item_id,e.song_id,"
            "t.original,CASE WHEN ("
            + selected
            + ") THEN 1 ELSE 0 END AS chosen FROM (SELECT DISTINCT item_id,song_id,library_id "
            "FROM edges WHERE facts=? AND kind='album' AND item_id IN "
            "(SELECT value FROM json_each(?))) e JOIN collectiondb.collection_tracks t ON "
            "t.library_id=e.library_id AND t.song_id=e.song_id AND t.present=1) "
            "GROUP BY item_id HAVING SUM(original)>0 AND SUM(original*(1-chosen))=0",
            (*params, binding.facts, json.dumps(list(albums))),
        ).fetchall()
        whole = {str(row["item_id"]) for row in found if row["left_out"] > 0}
        originals = {
            str(row["item_id"]): frozenset(
                str(song) for song in json.loads(row["songs"]) if song is not None
            )
            for row in found
        }
        return whole, originals

    def _stats_of(
        self,
        conn: sqlite3.Connection,
        binding: Binding,
        kind: str,
        idents: Sequence[str] | None,
        selected: str,
        params: Sequence[Any],
    ) -> dict[str, dict[str, Any]]:
        """Each of these items with a selected song -> the statistics of its selected songs,
        from one pass over the items' edges, however many they are. ``idents`` ``None``: all
        songs of the kind, as one item with the ID ''."""
        if kind not in ("song", "album", "artist", "tag", "genre"):
            raise IndexFailed("Unsupported library statistic.")
        item = "e.item_id" if idents is not None else "''"
        among = " AND e.item_id IN (SELECT value FROM json_each(?))" if idents is not None else ""
        wanted: tuple[Any, ...] = (
            (json.dumps(list(dict.fromkeys(idents))),) if idents is not None else ()
        )
        found: dict[str, dict[str, Any]] = {
            str(row["item"]): {
                "songCount": row["songs"],
                "albumCount": row["albums"],
                "duration": row["duration"],
                "size": row["size"],
            }
            for row in conn.execute(
                "SELECT item,COUNT(*) songs,COUNT(DISTINCT album_id) albums,SUM(duration) duration,"
                "SUM(size) size FROM (SELECT DISTINCT " + item + " AS item,e.song_id,e.album_id,"
                "e.duration,e.size FROM edges e WHERE e.facts=? AND e.kind=?"
                + among
                + " AND ("
                + selected
                + ")) GROUP BY item",
                (binding.facts, kind, *wanted, *params),
            )
        }
        if kind != "artist" or not found:
            return found
        for stats in found.values():
            stats["stats"] = {}
            stats["size"] = 0
        credits = (
            "SELECT " + item + " AS item,{role} AS role,COUNT(DISTINCT e.song_id) songs,"
            "COUNT(DISTINCT e.album_id) albums,SUM(e.size) size FROM edges e WHERE e.facts=? "
            "AND e.kind='artist'" + among + " AND {roles} AND (" + selected + ") GROUP BY item{by}"
        )
        args = (binding.facts, *wanted, *params)
        for row in conn.execute(
            credits.format(role="e.role", roles="e.role<>''", by=",e.role"), args
        ):
            stats = found[str(row["item"])]
            stats["stats"][row["role"]] = {
                "songCount": row["songs"],
                "albumCount": row["albums"],
                "size": row["size"],
            }
            stats["size"] += row["size"]
        main = credits.format(role="''", roles="e.role IN ('artist','albumartist')", by="")
        for row in conn.execute(main, args):
            found[str(row["item"])]["stats"]["maincredit"] = {
                "songCount": row["songs"],
                "albumCount": row["albums"],
                "size": row["size"],
            }
        return found

    async def _read(
        self,
        binding: Binding,
        mode: Selection | None,
        work: Callable[[sqlite3.Connection, _View], T],
    ) -> T:
        """``work`` in one read snapshot of the cache and the collection, without waiting
        for this cache's writer unless what it needs has to be written first: structural
        facts a read cannot go without, and an account's selection, which it works out by
        itself after a moment rather than wait long."""
        await self.open()
        live = False
        for _ in range(4):
            try:
                return await anyio.to_thread.run_sync(
                    self._in_snapshot, binding, mode, work, live, limiter=self.reads
                )
            except _FactsBehind:
                with anyio.move_on_after(self.budgets.wait_seconds) as waited:
                    await self._write_facts_once(binding).wait()
                if waited.cancelled_caught:
                    raise IndexPending() from None
            except _SelectionBehind:
                live = await self._keep_selection(binding, mode)
        raise IndexPending()

    def _in_snapshot(
        self,
        binding: Binding,
        mode: Selection | None,
        work: Callable[[sqlite3.Connection, _View], T],
        live: bool,
    ) -> T:
        with self._reader() as conn:
            conn.execute("BEGIN")
            return work(conn, self._view(conn, binding, mode, live))

    def _view(
        self, conn: sqlite3.Connection, binding: Binding, mode: Selection | None, live: bool
    ) -> _View:
        facts = self._current_facts(conn, binding)
        if live:
            selected, params = self._live_selection(binding, mode)
            return _View(selected, tuple(params), None)
        stamp = self._stamp(conn, binding.owner_id, mode)
        clauses: list[str] = []
        values: list[Any] = []
        for library in sorted(binding.libraries):
            if library in binding.foreign:
                clauses.append("e.library_id=?")
                values.append(library)
                continue
            row = conn.execute(
                "SELECT id,stamp FROM selections WHERE owner=? AND library_id=? AND mode=?",
                (binding.owner_id, library, mode or ""),
            ).fetchone()
            if row is None or row["stamp"] != stamp:
                raise _SelectionBehind
            clauses.append(
                "(e.library_id=? AND EXISTS (SELECT 1 FROM selected_songs ss "
                "WHERE ss.selection=? AND ss.song_id=e.song_id))"
            )
            values.extend((library, row["id"]))
        # Whose, which and as of when: enough on its own to tell one result from another's.
        identity = None if binding.foreign else (binding.owner_id, mode, facts, stamp, *values)
        return _View(" OR ".join(clauses) or "0", tuple(values), identity)

    def _current_facts(
        self, conn: sqlite3.Connection, binding: Binding
    ) -> tuple[tuple[str, str, int], ...]:
        """The state of the binding's facts, as of this snapshot, or what keeps them from
        being read."""
        current = []
        for library, generation in binding.generations:
            index = conn.execute(
                "SELECT generation FROM collectiondb.collection_index WHERE library_id=?",
                (library,),
            ).fetchone()
            if index is None:
                raise IndexPending()
            stored = conn.execute(
                "SELECT generation,revision FROM fact_libraries WHERE facts=? AND library_id=?",
                (binding.facts, library),
            ).fetchone()
            if index[0] != generation:
                # A snapshot before this one: its facts as they are, or not at all.
                if stored is None or stored[0] != generation:
                    raise IndexPending()
            elif (
                stored is None
                or stored[0] != generation
                or self._revision(conn, library, generation) > stored[1]
            ):
                raise _FactsBehind
            current.append((library, generation, int(stored[1])))
        if (
            not current
            and conn.execute("SELECT 1 FROM facts WHERE key=?", (binding.facts,)).fetchone() is None
        ):
            raise _FactsBehind
        return tuple(current)

    def _write_facts(self, binding: Binding) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._facts(conn, binding)
            if not binding.foreign:  # (a caller's own may have no list yet)
                self._drop_unused_facts(conn)

    @staticmethod
    def _stamp(conn: sqlite3.Connection, owner: str, mode: Selection | None) -> str:
        """What the selection's songs depend on now (``collection_revisions``)."""
        scope = "placeholders" if mode == REAL_AUDIO else "owner:" + owner
        revisions = dict(
            conn.execute(
                "SELECT scope,revision FROM collectiondb.collection_revisions"
                " WHERE scope IN ('',?)",
                (scope,),
            ).fetchall()
        )
        return f"{revisions.get('', 0)}.{revisions.get(scope, 0)}"

    async def _keep_selection(self, binding: Binding, mode: Selection | None) -> bool:
        """Keep the selection's songs as of now. Whether the read should rather work the
        selection out itself: the writer is busy, or the cache cannot take them."""
        if self.writing_facts:
            return True  # a pass over the whole library, one transaction
        with anyio.move_on_after(self.budgets.selection_wait_seconds):
            async with self.lock:
                try:
                    await anyio.to_thread.run_sync(self._write_selection, binding, mode)
                except (sqlite3.Error, OSError, IndexFailed) as exc:
                    log.warning("an account's selection could not be kept: %s", exc)
                    return True
                return False
        return True

    def _write_selection(self, binding: Binding, mode: Selection | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            stamp = self._stamp(conn, binding.owner_id, mode)
            for library in sorted(binding.libraries - binding.foreign):
                row = conn.execute(
                    "SELECT id,stamp FROM selections WHERE owner=? AND library_id=? AND mode=?",
                    (binding.owner_id, library, mode or ""),
                ).fetchone()
                if row is not None and row["stamp"] == stamp:
                    continue
                policy = (
                    real_audio_predicate(
                        library, song_expression="t.song_id", database="collectiondb"
                    )
                    if mode == REAL_AUDIO
                    else predicate(
                        binding.owner_id,
                        library,
                        song_expression="t.song_id",
                        database="collectiondb",
                        mode=mode,
                    )
                )
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS chosen(song_id TEXT PRIMARY KEY) WITHOUT ROWID"
                )
                conn.execute("DELETE FROM temp.chosen")
                count = conn.execute(
                    "INSERT OR IGNORE INTO temp.chosen SELECT t.song_id "
                    "FROM collectiondb.collection_tracks t WHERE t.library_id=? AND t.present=1 "
                    "AND (" + policy.sql + ")",
                    (library, *policy.params),
                ).rowcount
                self._room(conn, count * 128)
                if row is None:
                    conn.execute(
                        "DELETE FROM selections WHERE id IN (SELECT id FROM selections "
                        "ORDER BY computed DESC LIMIT -1 OFFSET ?)",
                        (self.budgets.selections - 1,),
                    )
                    selection = conn.execute(
                        "INSERT INTO selections(owner,library_id,mode,stamp,computed)"
                        " VALUES (?,?,?,?,?) RETURNING id",
                        (binding.owner_id, library, mode or "", stamp, time.time()),
                    ).fetchone()[0]
                else:
                    selection = row["id"]
                    conn.execute(
                        "UPDATE selections SET stamp=?,computed=? WHERE id=?",
                        (stamp, time.time(), selection),
                    )
                # What changed only: a played catalog song changes a few rows of thousands.
                conn.execute(
                    "DELETE FROM selected_songs WHERE selection=? "
                    "AND song_id NOT IN (SELECT song_id FROM temp.chosen)",
                    (selection,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO selected_songs SELECT ?,song_id FROM temp.chosen",
                    (selection,),
                )

    async def page_many(
        self,
        binding: Binding,
        mode: Selection | None,
        requests: Sequence[tuple[str | SourcePage, str, int, int]],
        *,
        artist_count_role: str = "",
        whole_albums: bool = False,
    ) -> list[Page]:
        """Pages of several generations in one read snapshot. A generation is the key
        ``ensure`` gave, or a caller's complete answer of at most one source page, read
        with the same SQL and not kept."""
        if not requests:
            return []  # (a search that found nothing needs no selection, ready or not)
        for generation, *_ in requests:
            if isinstance(generation, SourcePage):
                ids = [row.get("id") for row in generation.rows]
                if not generation.complete or len(ids) > self.budgets.page_size:
                    raise IndexFailed("The direct native answer is incomplete.")
                if len(set(ids)) != len(ids) or not all(
                    isinstance(i, str) and i and len(i) <= 300 for i in ids
                ):
                    raise IndexFailed("The library returned an invalid identity.")

        def read(conn: sqlite3.Connection, view: _View) -> list[Page]:
            pages = []
            for n, (generation, kind, offset, limit) in enumerate(requests):
                if isinstance(generation, SourcePage):
                    key, schema = "live" + str(n), "temp"
                    self._stage(conn, binding, key, generation)
                else:
                    key, schema = generation, "main"
                pages.append(
                    self._page(
                        conn,
                        view,
                        key,
                        binding,
                        kind,
                        mode,
                        offset,
                        limit,
                        artist_count_role=artist_count_role,
                        whole_albums=whole_albums,
                        schema=schema,
                    )
                )
            return pages

        return await self._read(binding, mode, read)
