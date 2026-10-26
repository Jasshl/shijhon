# ruff: noqa: S608 -- SQL fragments/identifiers are trusted constants; values are bound.
"""Private, bounded indexes of caller-authenticated enumeration answers.

The native API cannot join a Shijhon collection before LIMIT. Keep its exact ordered
candidate set on disk, then apply the common policy before COUNT/LIMIT. A partial scan
is never published. The cache is disposable; it contains no credentials. Structural
facts come from the collection index, never from another caller's annotated answers.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import zlib
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from shijhon.collection.contracts import REAL_AUDIO, IndexState, Selection
from shijhon.collection.policy import predicate, real_audio_predicate
from shijhon.proxy.params import RestCall

Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
Kind = str
QUERY_REVISION = 2  # previous pair-sorted keys could alias different repeated scalar filters


class IndexPending(Exception):
    def __init__(self, indexed: int = 0, total: int | None = None) -> None:
        self.indexed, self.total = indexed, total
        super().__init__("The selected library view is indexing. Retry shortly.")


class IndexFailed(Exception):
    """A safe failure with no upstream parameters or credentials in its message."""


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
        return digest((self.owner_id, self.authority, sorted(self.libraries), self.generations))[
            :24
        ]

    @property
    def foreign(self) -> frozenset[str]:
        return self.libraries - frozenset(library for library, _ in self.generations)

    def key(self, query: object) -> str:
        return digest(
            (
                QUERY_REVISION,
                self.owner_id,
                self.authority,
                sorted(self.libraries),
                self.generations,
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


DEFAULT_BUDGETS = Budgets()


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


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
 total INTEGER, envelope TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT ''
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
        self.progress: dict[str, str] = {}
        self.attempted: dict[str, float] = {}
        self.failed: dict[str, float] = {}
        self.jobs: dict[str, tuple[anyio.Event, anyio.CancelScope]] = {}
        self.lanes: dict[str, str] = {}
        # key -> the lanes of the requests waiting for its build ("": a request without one)
        self.waiting: dict[str, list[str]] = {}
        self.job_lanes: dict[str, str] = {}  # a search's build -> the lane that began it
        self.opened = False
        self.closed = False
        self.network_pages = 0
        self.keeper: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA main.synchronous=NORMAL")  # disposable cache, never collection state
        conn.create_function(
            "UNPACK", 1, lambda value: zlib.decompress(value).decode(), deterministic=True
        )
        conn.create_function("TAG_ID", 2, native_tag_id, deterministic=True)
        conn.execute("PRAGMA max_page_count=" + str((self.budgets.bytes - (4 << 20)) // 8192))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "ATTACH DATABASE ? AS collectiondb",
            (self.collection_path.resolve().as_uri() + "?mode=ro",),
        )
        return conn

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
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA max_page_count=" + str((self.budgets.bytes - (4 << 20)) // 8192))
            conn.execute("PRAGMA main.journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM queries WHERE state<>'ready'")
        # Retain a connection so every 500-row writer does not close the last WAL
        # connection and checkpoint the whole cache. No transaction stays open.
        self.keeper = sqlite3.connect(self.path, check_same_thread=False)

    async def close(self) -> None:
        self.closed = True
        jobs = list(self.jobs.values())
        for _, scope in jobs:
            scope.cancel()
        with anyio.CancelScope(shield=True), anyio.move_on_after(5):
            for event, _ in jobs:
                await event.wait()
        if self.keeper is not None:
            await anyio.to_thread.run_sync(self.keeper.close)
            self.keeper = None

    async def source_state(self, library: str) -> IndexState:
        """Read structural readiness AFTER the adapter proves the current principal.

        This reads no songs, memberships or credentials, and deliberately does not
        require a remembered dashboard connection for a valid REST-only caller.
        """
        await self.open()

        def read() -> IndexState:
            with closing(self._connect()) as conn:
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
            with closing(self._connect()) as conn:
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
            if (
                row is not None
                and row["state"] == "ready"
                and (time.time() - row["updated"] <= self.budgets.fresh_seconds)
            ):
                return key
            if row is not None and row["state"] == "failed" and time.time() - row["created"] < 5:
                raise IndexFailed("The library index could not be completed. Retry shortly.")
            if (
                row is not None
                and row["state"] == "ready"
                and time.monotonic() - self.attempted.get(key, -10000) < 5
            ):
                return key
            if key not in self.jobs:
                self.attempted[key] = time.monotonic()
                if len(self.attempted) > self.budgets.queries:
                    self.attempted.pop(next(iter(self.attempted)))
                if lane:
                    self._supersede(lane, key)
                if len(self.jobs) >= self.budgets.workers * 4:
                    raise IndexPending()
                event, scope = anyio.Event(), anyio.CancelScope()
                self.jobs[key] = (event, scope)
                if lane:
                    self.job_lanes[key] = lane

                async def build() -> None:
                    await self._build(
                        key, binding, fetch, validate, event, scope, structural, page_size
                    )

                self.spawn(build)
            event = self.jobs[key][0]
            if row is not None and row["state"] == "ready":
                return key  # same principal/grants/source: refresh in background
            if wait:
                self.waiting.setdefault(key, []).append(lane)
        if wait:
            try:
                with anyio.move_on_after(
                    self.budgets.wait_seconds if patience is None else patience
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
            and time.time() - row["updated"] <= self.budgets.fresh_seconds
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
        with closing(self._connect()) as conn:
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
    ) -> None:
        temporary = secrets.token_hex(16)
        self.progress[key] = temporary
        try:
            with scope, anyio.fail_after(self.budgets.seconds):
                async with self.slots:
                    await validate()
                    async with self.lock:
                        await anyio.to_thread.run_sync(self._begin, temporary, binding)
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
        except Exception:
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
                    self.jobs.pop(key, None)
                    self.job_lanes.pop(key, None)
                    self.progress.pop(key, None)
                    event.set()

    def _begin(self, key: str, binding: Binding) -> None:
        with closing(self._connect()) as conn, conn:
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
                "INSERT INTO queries(key,owner,facts,created,updated,used,state) "
                "VALUES (?,?,?,?,?,?,'indexing')",
                (key, binding.owner_id, binding.facts, now, now, now),
            )

    def _evict(self, conn: sqlite3.Connection, owner: str = "") -> None:
        victim = conn.execute(
            "SELECT q.key FROM queries q WHERE q.state IN ('ready','failed') "
            + ("AND q.owner=? " if owner else "")
            + "AND NOT EXISTS (SELECT 1 FROM slots s JOIN json_each(?) j ON j.value=s.key "
            "WHERE s.generation=q.key) ORDER BY q.used LIMIT 1",
            (*((owner,) if owner else ()), json.dumps(list(self.jobs))),
        ).fetchone()
        if victim is None:
            raise IndexFailed("The private library index has reached its capacity. Retry shortly.")
        conn.execute("DELETE FROM queries WHERE key=?", (victim[0],))
        conn.execute("DELETE FROM facts WHERE key NOT IN (SELECT facts FROM queries)")

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
            conn.execute("DELETE FROM facts WHERE key NOT IN (SELECT facts FROM queries)")

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

    def _selection(self, binding: Binding, mode: Selection | None) -> tuple[str, list[Any]]:
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

    def _facts(self, conn: sqlite3.Connection, binding: Binding) -> None:
        for library, generation in binding.generations:
            current = conn.execute(
                "SELECT generation FROM collectiondb.collection_index WHERE library_id=?",
                (library,),
            ).fetchone()
            if current is None or current[0] != generation:
                raise IndexPending()
        if conn.execute("SELECT 1 FROM facts WHERE key=?", (binding.facts,)).fetchone():
            for library, generation in binding.generations:
                self._reconcile(conn, binding.facts, library, generation)
            return
        conn.execute("INSERT INTO facts VALUES (?)", (binding.facts,))
        for library, generation in binding.generations:
            revision = self._revision(conn, library, generation)
            self._edges(conn, binding.facts, library, generation)
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
            self._edges(conn, facts, library, generation, songs[start : start + 500])
        conn.execute(
            "INSERT OR REPLACE INTO fact_libraries VALUES (?,?,?,?)",
            (facts, library, generation, current),
        )

    def _edges(
        self,
        conn: sqlite3.Connection,
        facts: str,
        library: str,
        generation: str,
        songs: Sequence[str] | None = None,
    ) -> None:
        """The structural edges of a library's snapshot (``songs``: of those songs only)."""
        only = "" if songs is None else " AND t.song_id IN (SELECT value FROM json_each(?))"
        base = (
            " FROM collectiondb.collection_tracks t WHERE t.library_id=? AND "
            "t.generation=? AND t.present=1" + only
        )
        args: tuple[Any, ...] = (facts, library, generation)
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
                "t.generation=? AND t.present=1 AND json_extract(p.value,'$.id') "
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
                "WHERE t.library_id=? AND t.generation=? AND t.present=1 AND "
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
                "WHERE t.library_id=? AND t.generation=? AND t.present=1 "
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
                "t.generation=? AND t.present=1 AND "
                "json_extract(v.value,'$.name') IS NOT NULL" + only
            ),
            args,
        )

    async def extend(self, library: str, generation: str) -> None:
        """Songs were added to a library's snapshot in place (``Index.add``): every set of
        facts bound to that snapshot - as its binding declared it - takes them in now
        (each would before its next read anyway: ``_reconcile``), and every list is
        refreshed beside its next request, which it keeps answering meanwhile. No list
        has to be prepared again from nothing."""
        await self.open()
        async with self.lock:

            def extend() -> None:
                with self._reading() as conn:
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
        async with self.lock:
            return await anyio.to_thread.run_sync(
                lambda: self._page(
                    key,
                    binding,
                    kind,
                    mode,
                    offset,
                    limit,
                    random,
                    count_sort,
                    descending,
                    direct_album,
                    song_filters,
                    random_seed=random_seed,
                    artist_role=artist_role,
                    artist_count_role=artist_count_role,
                    verify_songs=verify_songs,
                    preserve_album_metadata=preserve_album_metadata,
                    whole_albums=whole_albums,
                )
            )

    def _page(
        self,
        key: str,
        binding: Binding,
        kind: str,
        mode: Selection | None,
        offset: int,
        limit: int,
        random: bool,
        count_sort: str,
        descending: bool,
        direct_album: str,
        song_filters: RestCall | None,
        connection: sqlite3.Connection | None = None,
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
        selected, params = self._selection(binding, mode)
        answers = schema + ".answers"
        with self._reading(connection) as conn:
            self._facts(conn, binding)
            query = conn.execute(
                (
                    f"SELECT q.* FROM {schema}.slots s JOIN {schema}.queries q "
                    "ON q.key=s.generation "
                    "WHERE s.key=? AND q.owner=? AND q.facts=? AND q.state='ready'"
                ),
                (key, binding.owner_id, binding.facts),
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
                    "e.role IN ('artist','albumartist')"
                    if artist_role == "maincredit"
                    else "e.role=?"
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
            maincredit_sort = kind == "artist" and count_sort.startswith("maincredit_")
            if maincredit_sort:
                count_sort = count_sort.removeprefix("maincredit_")
            matching = "e.role IN ('artist','albumartist')" if maincredit_sort else "1"
            size_match = matching if maincredit_sort else "e.role<>''" if kind == "artist" else "1"
            cte = (
                "WITH visible AS (SELECT r.*,COUNT(DISTINCT CASE WHEN "
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
                "facts,kind,item_id,song_id,library_id,album_id,duration,size,year"
                + (",role" if kind == "artist" else "")
                + " FROM edges WHERE "
                + edge_filter
                + ") e ON e.facts=? AND "
                + relation
                + " WHERE r.key=? AND ("
                + selected
                + ")"
                + extra
                + candidate_extra
                + " GROUP BY r.ordinal) "
            )
            args = (
                *(() if kind == "directory" else (relation_kind,)),
                binding.facts,
                query["key"],
                *params,
                *extra_params,
                *candidate_params,
            )
            aggregated = kind != "song" and count_sort in (
                "songCount",
                "albumCount",
                "duration",
                "size",
            )
            if not aggregated:
                # Count visibility without calculating every candidate's aggregates.
                # Hydrate statistics only for the bounded page unless ordering itself
                # requires those statistics. Native candidate order stays intact.
                # Unary + prevents SQLite 3.51's EXISTS-to-JOIN rewrite from counting
                # matching songs toward OFFSET and repeating items across pages.
                cte = (
                    "WITH visible AS (SELECT r.* FROM " + answers + " r WHERE r.key=? AND +EXISTS ("
                    "SELECT 1 FROM edges e WHERE e.facts=? AND "
                    + edge_filter
                    + " AND "
                    + relation
                    + " AND ("
                    + selected
                    + ")"
                    + extra
                    + ")) "
                )
                if candidate_extra:
                    cte = cte.removesuffix(") ") + candidate_extra + ") "
                args = (
                    query["key"],
                    binding.facts,
                    *(() if kind == "directory" else (relation_kind,)),
                    *params,
                    *extra_params,
                    *candidate_params,
                )
            total = conn.execute(cte + "SELECT COUNT(*) FROM visible", args).fetchone()[0]
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
                    "AND e.kind='song' AND e.song_id IN ("
                    + ",".join("?" for _ in ids)
                    + ") AND ("
                    + selected
                    + ")",
                    (binding.facts, *ids, *params),
                ).fetchone()[0]
                all_selected = found == len(ids)
            sort = "RANDOM()" if random else "ordinal"
            if random and random_seed:
                conn.create_function(
                    "SHUFFLE",
                    1,
                    lambda ident: hashlib.sha256((random_seed + "\0" + ident).encode()).digest(),
                    deterministic=True,
                )
                sort = "SHUFFLE(item_id)" + (" DESC" if descending else " ASC")
            if kind != "song" and count_sort in ("songCount", "albumCount", "duration", "size"):
                sort = (
                    {"songCount": "songs", "albumCount": "albums"}.get(count_sort, count_sort)
                    + (" DESC" if descending else " ASC")
                    + ",ordinal"
                )
            rows = conn.execute(
                cte + "SELECT * FROM visible ORDER BY " + sort + " LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            whole: set[str] = set()
            originals: dict[str, frozenset[str]] | None = None
            if whole_albums and kind == "album":
                whole, originals = (
                    self._whole_albums(
                        conn, binding, [str(row["item_id"]) for row in rows], selected, params
                    )
                    if rows
                    else (set(), {})
                )
            out = []
            for row in rows:
                item = json.loads(zlib.decompress(row["document"]))
                if kind != "song" and not (
                    kind == "album" and (preserve_album_metadata or row["item_id"] in whole)
                ):
                    stat_kind = (
                        ("album" if item.get("isDir") else "song") if kind == "directory" else kind
                    )
                    stat_id = item.get("name", item["id"]) if kind == "genre" else item["id"]
                    use_aggregate = aggregated and kind != "artist"
                    stats = (
                        dict(row)
                        if use_aggregate
                        else self._stats(
                            binding,
                            mode,
                            stat_kind,
                            stat_id,
                            conn,
                            discovery=bool(direct_album and selected == "1"),
                        )
                    )
                    complete = item.get("songCount") == stats.get(
                        "songs" if use_aggregate else "songCount"
                    )
                    for field, column in (
                        ("songCount", "songs"),
                        ("albumCount", "albums"),
                        ("duration", "duration"),
                        ("size", "size"),
                    ):
                        if field in item:
                            if field != "songCount" and complete:
                                continue  # complete selected native metadata stays exact
                            value = stats.get(column if use_aggregate else field, 0)
                            item[field] = int(value) if isinstance(item[field], int) else value
                    if kind == "artist" and artist_count_role and "albumCount" in item:
                        item["albumCount"] = (
                            stats.get("stats", {}).get(artist_count_role, {}).get("albumCount", 0)
                        )
                if kind == "artist" and "stats" in item:
                    item["stats"] = (
                        self._stats(binding, mode, kind, item["id"], conn)
                        if use_aggregate
                        else stats
                    ).get("stats", {})
                out.append(item)
            conn.execute(
                f"UPDATE {schema}.queries SET used=? WHERE key=?", (time.time(), query["key"])
            )
            duration = None
            if direct_album and kind == "song":
                duration = self._stats(
                    binding, mode, "album", direct_album, conn, discovery=selected == "1"
                ).get("duration", 0)
            return Page(
                out,
                total,
                json.loads(query["envelope"]),
                query["updated"],
                duration,
                all_selected,
                originals,
            )

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
        await self.open()
        async with self.lock:

            def read() -> Page:
                with self._reading() as conn:
                    self._facts(conn, binding)
                    self._stage(conn, binding, "live", source)
                    return self._page(
                        "live",
                        binding,
                        kind,
                        mode,
                        0,
                        len(source.rows),
                        False,
                        "",
                        False,
                        direct_album,
                        None,
                        conn,
                        artist_count_role=artist_count_role,
                        schema="temp",
                    )

            return await anyio.to_thread.run_sync(read)

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
            "INSERT INTO temp.queries(key,owner,facts,created,updated,used,state,rows,envelope) "
            "VALUES (?,?,?,?,?,?,'ready',?,?)",
            (
                key,
                binding.owner_id,
                binding.facts,
                now,
                now,
                now,
                len(source.rows),
                json.dumps(source.envelope or {}),
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
                    zlib.compress(json.dumps(dict(row), separators=(",", ":")).encode(), 3),
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
        await self.open()
        async with self.lock:
            return await anyio.to_thread.run_sync(lambda: self._stats(binding, mode, kind, ident))

    async def stats_many(
        self, binding: Binding, mode: Selection | None, kind: str, idents: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """``stats`` of several items in one read snapshot: each item the index has, with
        the statistics of its selected songs (``{}``: none of them is selected). An item
        the index does not have is left out."""
        if len(idents) > self.budgets.native_page_size:
            raise IndexFailed("The answer exceeds its statistics budget.")
        await self.open()
        async with self.lock:

            def read() -> dict[str, dict[str, Any]]:
                found: dict[str, dict[str, Any]] = {}
                with self._reading() as conn:
                    for ident in idents:
                        if self._stats(binding, mode, kind, ident, conn, discovery=True):
                            found[ident] = self._stats(binding, mode, kind, ident, conn)
                return found

            return await anyio.to_thread.run_sync(read)

    async def shown(
        self, binding: Binding, mode: Selection | None, kind: str, idents: Sequence[str]
    ) -> frozenset[str]:
        """Of these songs or albums, those the selection shows - as its lists and searches
        select them: in the library index with a selected song. One local read, the IDs
        bound as one value; no Navidrome request."""
        if kind not in ("song", "album"):
            raise IndexFailed("Unsupported library index kind.")
        wanted = list(dict.fromkeys(idents))
        if not wanted:
            return frozenset()
        if len(wanted) > self.budgets.page_size:
            raise IndexFailed("The answer exceeds its selection budget.")
        await self.open()
        async with self.lock:

            def read() -> frozenset[str]:
                selected, params = self._selection(binding, mode)
                with self._reading() as conn:
                    self._facts(conn, binding)
                    rows = conn.execute(
                        "SELECT DISTINCT e.item_id FROM edges e WHERE e.facts=? AND e.kind=? "
                        "AND e.item_id IN (SELECT value FROM json_each(?)) AND (" + selected + ")",
                        (binding.facts, kind, json.dumps(wanted), *params),
                    ).fetchall()
                return frozenset(str(row[0]) for row in rows)

            return await anyio.to_thread.run_sync(read)

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

    def _stats(
        self,
        binding: Binding,
        mode: Selection | None,
        kind: str,
        ident: str,
        connection: sqlite3.Connection | None = None,
        discovery: bool = False,
    ) -> dict[str, Any]:
        if kind not in ("song", "album", "artist", "tag", "genre"):
            raise IndexFailed("Unsupported library statistic.")
        selected, params = self._selection(binding, mode)
        if discovery:
            selected, params = "1", []
        with self._reading(connection) as conn:
            self._facts(conn, binding)
            row = conn.execute(
                "SELECT COUNT(*) songs,COUNT(DISTINCT album_id) "
                "albums,SUM(duration) duration,SUM(size) size FROM (SELECT "
                "DISTINCT song_id,album_id,duration,size FROM edges e WHERE "
                "e.facts=? AND e.kind=?"
                + (" AND e.item_id=?" if ident else "")
                + " AND ("
                + selected
                + "))",
                (binding.facts, kind, *((ident,) if ident else ()), *params),
            ).fetchone()
            if not row["songs"]:
                return {}
            stats = {
                "songCount": row["songs"],
                "albumCount": row["albums"],
                "duration": row["duration"],
                "size": row["size"],
            }
            if kind == "artist":
                roles = conn.execute(
                    "SELECT role,COUNT(DISTINCT song_id) songs,COUNT(DISTINCT "
                    "album_id) albums,SUM(size) size FROM edges e WHERE e.facts=? AND "
                    "e.kind='artist' AND e.item_id=? AND e.role<>'' AND ("
                    + selected
                    + ") GROUP BY role",
                    (binding.facts, ident, *params),
                ).fetchall()
                stats["stats"] = {
                    r["role"]: {
                        "songCount": r["songs"],
                        "albumCount": r["albums"],
                        "size": r["size"],
                    }
                    for r in roles
                }
                stats["size"] = sum(r["size"] for r in roles)
                main = conn.execute(
                    "SELECT COUNT(DISTINCT song_id) songs,COUNT(DISTINCT album_id) albums,"
                    "SUM(size) size FROM edges e WHERE e.facts=? AND e.kind='artist' "
                    "AND e.item_id=? AND e.role IN ('artist','albumartist') AND (" + selected + ")",
                    (binding.facts, ident, *params),
                ).fetchone()
                if main["songs"]:
                    stats["stats"]["maincredit"] = {
                        "songCount": main["songs"],
                        "albumCount": main["albums"],
                        "size": main["size"],
                    }
            return stats

    @contextmanager
    def _reading(self, existing: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        if existing is not None:
            with nullcontext(existing) as conn:
                yield conn
            return
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN")
            yield conn

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
        for generation, *_ in requests:
            if isinstance(generation, SourcePage):
                ids = [row.get("id") for row in generation.rows]
                if not generation.complete or len(ids) > self.budgets.page_size:
                    raise IndexFailed("The direct native answer is incomplete.")
                if len(set(ids)) != len(ids) or not all(
                    isinstance(i, str) and i and len(i) <= 300 for i in ids
                ):
                    raise IndexFailed("The library returned an invalid identity.")
        await self.open()
        async with self.lock:

            def read() -> list[Page]:
                pages = []
                with self._reading() as conn:
                    for n, (generation, kind, offset, limit) in enumerate(requests):
                        if isinstance(generation, SourcePage):
                            key, schema = "live" + str(n), "temp"
                            self._stage(conn, binding, key, generation)
                        else:
                            key, schema = generation, "main"
                        pages.append(
                            self._page(
                                key,
                                binding,
                                kind,
                                mode,
                                offset,
                                limit,
                                False,
                                "",
                                False,
                                "",
                                None,
                                conn,
                                artist_count_role=artist_count_role,
                                whole_albums=whole_albums,
                                schema=schema,
                            )
                        )
                return pages

            return await anyio.to_thread.run_sync(read)
