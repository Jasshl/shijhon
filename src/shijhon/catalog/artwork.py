"""Catalog covers without extra catalog requests.

- **Artwork index**: the artwork templates of the catalog items Shijhon has shown
  (search results, artist pages, albums), so that a client's cover request for one of them
  needs no album-detail request first (a large catalog artist page would cost one per
  cover). Albums' and artists' templates are also kept on disk (a small SQLite file of its
  own, with the covers: a cache like them), so an item shown before a restart needs none
  either; a song's template stands for its album's when that is not known.
- **Disk cache**: resized covers are kept in the state directory, keyed by the cover (the
  item and the size a client asked for), for a while (30 days) and up to a size limit (the
  oldest go first), so a cover seen before needs no catalog request at all, also after a
  restart. Concurrent requests for one cover share one fetch.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread

from shijhon.catalog.base import SearchResults
from shijhon.catalog.model import CatalogArtist, CatalogRelease, CatalogTrack
from shijhon.locks import KeyedLocks

log = logging.getLogger(__name__)

Fetch = Callable[[], Awaitable[tuple[bytes, str]]]
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
TRIM_SECONDS = 86400.0
PARTIAL_SECONDS = 3600.0  # a partial file older than this was left by a failed write
KEPT_KINDS = ("al", "ar")  # the covers clients ask for (a song's is its album's)
KEPT_ROWS = 200_000  # templates kept on disk (the most recently shown)
FLUSH_STALE = 60.0  # seconds: a flush started this long ago and not done never ran


class ArtworkIndex:
    """Artwork templates by item ("al"/"tr"/"ar" and the catalog reference), the most
    recently shown kept in memory; albums' and artists' also in ``path`` (written in the
    background: ``spawn``)."""

    def __init__(
        self,
        maxsize: int = 50_000,
        *,
        path: Path | None = None,
        spawn: Spawn | None = None,
        kept_rows: int = KEPT_ROWS,
    ) -> None:
        self.maxsize = maxsize
        self._templates: OrderedDict[str, str] = OrderedDict()
        self._kept = _KeptTemplates(path, kept_rows) if path is not None else None
        self.spawn = spawn
        self._pending: dict[str, str | None] = {}  # to write (None: to remove)
        self._writing: dict[str, str | None] = {}  # being written now
        self._flushing_since: float | None = None  # a flush was started then

    def template(self, kind: str, ref: object) -> str | None:
        return self._templates.get(f"{kind}:{ref}")

    async def find(self, kind: str, ref: object) -> str | None:
        """The item's template: shown since the start, else shown before (kept on disk)."""
        key = f"{kind}:{ref}"
        found = self._templates.get(key)
        if found is not None or self._kept is None or kind not in KEPT_KINDS:
            return found
        for waiting in (self._pending, self._writing):
            if key in waiting:
                return waiting[key]
        found = await anyio.to_thread.run_sync(self._kept.get, key)
        if found is not None:
            self._remember(key, found)
        return found

    def forget(self, kind: str, ref: object) -> None:
        key = f"{kind}:{ref}"
        self._templates.pop(key, None)
        if self._kept is not None and kind in KEPT_KINDS:
            self._write(key, None)

    def note(self, kind: str, ref: object, template: str | None) -> None:
        if not template:
            return
        key = f"{kind}:{ref}"
        changed = self._templates.get(key) != template
        self._remember(key, template)
        if changed and self._kept is not None and kind in KEPT_KINDS:
            self._write(key, template)

    def _remember(self, key: str, template: str) -> None:
        self._templates[key] = template
        self._templates.move_to_end(key)
        while len(self._templates) > self.maxsize:
            self._templates.popitem(last=False)

    def _write(self, key: str, template: str | None) -> None:
        self._pending[key] = template
        now = time.monotonic()
        # One flush at a time; one that never ran (no background any more) is not waited for.
        started = self._flushing_since
        if self.spawn is not None and (started is None or now - started > FLUSH_STALE):
            self._flushing_since = now
            self.spawn(self.flush)

    async def flush(self) -> None:
        """Write what was noted since the last flush (in the background)."""
        try:
            while self._pending and self._kept is not None:
                self._writing, self._pending = self._pending, {}
                await anyio.to_thread.run_sync(self._kept.put, self._writing)
                self._writing = {}
        except Exception as exc:  # a cache: those items are asked for again after a restart
            log.warning("artwork index not written: %s", type(exc).__name__)
        finally:
            self._writing = {}
            self._flushing_since = None

    def releases(self, releases: Iterable[CatalogRelease]) -> None:
        for release in releases:
            self.note("al", release.ref, release.artwork_template)
            self.tracks(release.tracks)

    def tracks(self, tracks: Iterable[CatalogTrack]) -> None:
        for track in tracks:
            self.note("tr", track.ref, track.artwork_template)
            # A song's cover is its album's: known from the song when the album is not.
            if track.album is not None and self.template("al", track.album) is None:
                self.note("al", track.album, track.artwork_template)

    def artists(self, artists: Iterable[CatalogArtist]) -> None:
        for artist in artists:
            self.note("ar", artist.ref, artist.artwork_template)

    def results(self, results: SearchResults) -> None:
        self.artists(results.artists)
        self.releases(results.albums)
        self.tracks(results.songs)


class _KeptTemplates:
    """Templates on disk: one SQLite file of its own (a cache; not the state database),
    used from worker threads. A failure turns it off (logged once): covers never wait on
    it."""

    def __init__(self, path: Path, rows: int) -> None:
        self.path = path
        self.rows = rows
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._broken = False
        self._writes = 0

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS templates (key TEXT PRIMARY KEY,"
                " template TEXT NOT NULL, noted_at REAL NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS templates_noted ON templates (noted_at)")
            self._conn = conn
        return self._conn

    def _failed(self, exc: Exception) -> None:
        if not self._broken:
            log.warning("artwork index: not kept on disk (%s)", type(exc).__name__)
        self._broken = True

    def get(self, key: str) -> str | None:
        with self._lock:
            if self._broken:
                return None
            try:
                row = (
                    self._db()
                    .execute("SELECT template FROM templates WHERE key = ?", [key])
                    .fetchone()
                )
            except (sqlite3.Error, OSError) as exc:
                self._failed(exc)
                return None
        return str(row[0]) if row else None

    def put(self, batch: dict[str, str | None]) -> None:
        with self._lock:
            if self._broken:
                return
            now = time.time()
            try:
                db = self._db()
                db.execute("BEGIN")
                db.executemany(
                    "INSERT INTO templates (key, template, noted_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET template = excluded.template,"
                    " noted_at = excluded.noted_at",
                    [(k, v, now) for k, v in batch.items() if v is not None],
                )
                db.executemany(
                    "DELETE FROM templates WHERE key = ?",
                    [(k,) for k, v in batch.items() if v is None],
                )
                self._writes += len(batch)
                if self._writes >= 1000:  # now and then: the most recently shown are kept
                    self._writes = 0
                    db.execute(
                        "DELETE FROM templates WHERE key IN (SELECT key FROM templates"
                        " ORDER BY noted_at DESC LIMIT -1 OFFSET ?)",
                        [self.rows],
                    )
                db.execute("COMMIT")
            except (sqlite3.Error, OSError) as exc:
                if self._conn is not None:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.execute("ROLLBACK")
                self._failed(exc)


_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
)


def raster_type(data: bytes) -> str | None:
    """The type of a raster image Shijhon serves and keeps - JPEG, PNG, GIF, WebP - read
    from its first bytes, never from a claim (a catalog's content type, a stored one);
    None for anything else. Catalog artwork is answered from Shijhon's own origin, where
    an image that can carry script (SVG) would run with the caller's credentials: only what
    is one of these by its bytes is served, under that type."""
    for signature, content_type in _SIGNATURES:
        if data.startswith(signature):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class ArtworkCache:
    """Resized covers on disk: ``<folder>/<2 hex>/<sha256 of the key>.img``. ``spawn``
    runs the occasional trim in the background (without it, in the request)."""

    def __init__(
        self,
        folder: Path,
        *,
        max_bytes: int,
        max_age_seconds: float,
        clock: Callable[[], float] = time.time,
        spawn: Spawn | None = None,
    ) -> None:
        self.folder = folder
        self.spawn = spawn
        self.max_bytes = max_bytes
        self.max_age = max_age_seconds
        self.clock = clock
        self._locks = KeyedLocks()
        self._size: int | None = None  # bytes on disk, counted at the first use
        self._trimming = False
        self._trimmed_at = float("-inf")
        self._write_failed = False
        self.fetches = 0  # observable in tests

    async def has(self, key: str) -> bool:
        """Whether the cover is kept (and not too old)."""
        path = self._path(key)

        def fresh() -> bool:
            try:
                return self.clock() - path.stat().st_mtime <= self.max_age
            except OSError:
                return False

        return await anyio.to_thread.run_sync(fresh)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.folder / digest[:2] / f"{digest}.img"

    async def get(self, key: str, fetch: Fetch) -> tuple[bytes, str]:
        """The cover kept under ``key``, else ``fetch()``'s (kept when it is a raster image
        of a known type, ``raster_type``, and then answered as that type; anything else is
        passed on under its claimed type without being kept - not to be served)."""
        path = self._path(key)
        async with self._locks.hold(path.name):
            found = await anyio.to_thread.run_sync(self._read, path)
            if found is not None:
                return found
            self.fetches += 1
            data, content_type = await fetch()
            sniffed = raster_type(data)
            if sniffed is not None:
                await self._store(path, data)
            return data, sniffed or content_type

    def _read(self, path: Path) -> tuple[bytes, str] | None:
        try:
            if self.clock() - path.stat().st_mtime > self.max_age:
                return None  # fetched again, and replaced
            data = path.read_bytes()
        except OSError:
            return None
        content_type = raster_type(data)
        return (data, content_type) if content_type else None

    async def _store(self, path: Path, data: bytes) -> None:
        try:
            replaced = await anyio.to_thread.run_sync(self._write, path, data)
        except OSError as exc:
            if not self._write_failed:  # once: a full disk would repeat it for every cover
                log.warning("artwork cache: a cover was not saved (%s)", type(exc).__name__)
                self._write_failed = True
            return
        if self._size is not None:
            self._size += len(data) - replaced
        # Counted (and expired covers removed) at the first save, then once a day, and
        # whenever the limit is passed.
        due = self._size is None or self.clock() - self._trimmed_at > TRIM_SECONDS
        if (due or self._size > self.max_bytes) and not self._trimming:  # type: ignore[operator]
            self._trimming = True
            if self.spawn is not None:
                self.spawn(self._trim_now)  # the cover's request does not wait for it
            else:
                await self._trim_now()

    async def _trim_now(self) -> None:
        try:
            self._size = await anyio.to_thread.run_sync(self._trim)
            self._trimmed_at = self.clock()
        except OSError as exc:
            log.warning("artwork cache: not trimmed (%s)", type(exc).__name__)
        finally:
            self._trimming = False

    def _write(self, path: Path, data: bytes) -> int:
        """Write atomically; returns the size of the file it replaced (0: none)."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            replaced = path.stat().st_size
        except OSError:
            replaced = 0
        partial = path.with_suffix(f".{os.getpid()}.part")
        try:
            partial.write_bytes(data)
            now = self.clock()
            os.utime(partial, (now, now))  # its age, on the cache's clock
            os.replace(partial, path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return replaced

    def _files(self) -> list[tuple[float, int, Path]]:
        found = []
        for path in self.folder.glob("*/*.img"):
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, stat.st_size, path))
        return found

    def _trim(self) -> int:
        """Remove expired covers, then the oldest until below 90 % of the limit (and partial
        files a failed write left); returns the size left."""
        now = self.clock()
        for partial in self.folder.glob("*/*.part"):
            try:
                if now - partial.stat().st_mtime > PARTIAL_SECONDS:
                    partial.unlink()
            except OSError:
                continue
        files = sorted(self._files())
        total = sum(size for _, size, _ in files)
        removed = 0
        for mtime, size, path in files:
            if now - mtime <= self.max_age and total <= self.max_bytes * 0.9:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        if removed:
            log.info("artwork cache: removed %d cover(s); %d MB kept", removed, total // 2**20)
        return total
