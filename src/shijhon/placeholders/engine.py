"""Materialize catalog releases as placeholder tracks in the Navidrome library.

Every change follows the same sequence: write files (staged, then moved into place), run
a targeted scan, then **verify** with Navidrome before recording anything:

- all new files are indexed and belong to one album;
- when completing an owned album: that album's ID did not change, none of its songs
  changed ID or album, and it now holds the owned plus the new tracks (no split);
- for a catalog-only album: the album holds exactly this release's placeholders
  (it did not join some other album).

On failure the new files are removed, the folder is rescanned and the error reported.
New placeholders are recorded as pending before their files move into the library:
until their rows are written - in the same transaction that ends the pending record -
requests for their songs are never forwarded to Navidrome as owned songs, and a stop in
between leaves the record, by which the next start (or the release's next write) takes
the files out again.
Replacing a placeholder with delivered audio and reverting it follow the same rule:
targeted scan, then the song ID must be unchanged, else roll back.

Taking a release out again (the cleanup, ``fills-undo``) is marked first, then its
files go and a targeted scan confirms it; a failure, or a use found at the last check,
puts it back (a stop in the middle: at the next start). A release the cleanup took out is
kept as a record: a use of one of its old IDs, or a commit or fill of it, adds it
back from that record - at the same paths, so with the same song IDs.
"""

from __future__ import annotations

import errno
import glob
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
import anyio
import anyio.to_thread
import mutagen

from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    release_data,
)
from shijhon.locks import KeyedLocks
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import used_before
from shijhon.placeholders import durable
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.backing import OwnedRecordings
from shijhon.placeholders.layout import Layout, LayoutError
from shijhon.placeholders.silence import SilenceMaker, samples_for
from shijhon.store import Store

log = logging.getLogger(__name__)


_AUDIO_SUFFIXES = {".flac", ".m4a", ".mp4", ".mp3", ".ogg", ".opus"}


class MaterializeError(RuntimeError):
    """The library was left as it was before; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReplaceError(RuntimeError):
    pass


# Why a release's placeholders must stay (their rows as they are now): empty when unused.
# Asked twice: before anything is touched (False), and once the files are gone and Navidrome
# lists the songs as missing (True).
UsageCheck = Callable[[list[Any], bool], Awaitable[list[str]]]
# More statements in the transaction that takes a release out or adds it back.
Statements = Callable[[aiosqlite.Connection], Awaitable[None]]
RETRY_RESTORE = 600.0  # seconds before a failed restore of a removed release is tried again
MISSING_SECONDS = 60.0  # how long an old ID found missing in Navidrome is believed so
# How long a compensation that a cancellation started (a shutdown, a client that left) waits
# for Navidrome to confirm it. The files are put right first, without a bound; a scan not
# confirmed in time is the next start's (its repairs), or the next scan's.
COMPENSATION_SECONDS = 5.0
# How long new placeholders wait for requests listing a collection through Navidrome, before
# their write is refused; such a request waits less for Navidrome's answer to start
# (``delivery/intercept.py``), so only requests one after another make a write wait it out.
LISTING_WAIT_SECONDS = 60.0


@dataclass
class Removal:
    removed: int = 0  # placeholders taken out
    kept: list[str] = field(default_factory=list)  # why they stay (in use): nothing removed
    # Kept once its files were gone, and not confirmed back yet: the repairs finish it.
    unfinished: bool = False


@dataclass
class OwnedAlbum:
    album_id: str
    songs: list[dict[str, Any]]  # native song records of owned (non-placeholder) files


@dataclass
class MaterializeResult:
    release: CatalogRef
    album_id: str
    created: dict[CatalogRef, str] = field(default_factory=dict)  # track -> song ID
    linked: dict[CatalogRef, str] = field(default_factory=dict)  # track -> owned song ID
    folder: str = ""
    scan_seconds: float = 0.0
    album_tags: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _Planned:
    track: CatalogTrack
    relative: str
    comments: dict[str, list[str]]


@dataclass
class _Pending:
    """New placeholders of a release being written: recorded before their files move
    into the library, ended by the transaction that records them or once they are gone
    again. ``running``: this process writes them now; otherwise a stop (or a rollback that
    failed) left them: undone before the release is written again. ``restoring``: a release
    being added back from its record (its mark, ``restoring_at``, is its record)."""

    ref: str
    folder: str
    paths: frozenset[str]
    cover: bool  # a new catalog album's cover.jpg came with them
    new_folder: bool  # the release's folder was new
    running: bool
    restoring: bool = False
    ended: anyio.Event = field(default_factory=anyio.Event)


class PlaceholderEngine:
    def __init__(
        self,
        *,
        layout: Layout,
        navidrome: NavidromeService,
        scans: ScanCoordinator,
        silence: SilenceMaker,
        store: Store,
        clock: Callable[[], float] = time.time,
        backing: OwnedRecordings | None = None,
        writes_refused: Callable[..., Awaitable[str | None]] | None = None,
    ) -> None:
        self.layout = layout
        # Why the library may not be written now (Navidrome would purge missing files'
        # favorites), or None; asked - of Navidrome, each time (``fresh=True``) -
        # before every library write: a release added, taken out or put back, a
        # placeholder's file swapped, a repair.
        self.writes_refused = writes_refused
        self.navidrome = navidrome
        self.scans = scans
        self.silence = silence
        self.store = store
        self.clock = clock
        self._locks = KeyedLocks()
        # New placeholders of recordings the owner has on other albums play those files.
        self.backing = backing
        # Test hook: adjust tags before writing, e.g. to force a split.
        self.tag_hook: Callable[[dict[str, list[str]]], None] | None = None
        # Releases taken out: old song IDs (and catalog albums' album IDs) -> the
        # release; loaded on first use.
        self._removed: dict[str, str] | None = None
        self._removed_albums: set[str] = set()  # of those, catalog albums' album IDs
        self._removed_refs: set[str] = set()
        self._restore_failed: dict[str, float] = {}  # release -> when its restore failed
        self._missing: dict[str, float] = {}  # old ID -> when Navidrome had it missing
        self._removing: dict[str, str] = {}  # IDs of a release being taken out -> it
        self._removing_albums: set[str] = set()  # of those, the albums' IDs
        # New placeholders being written, or left half written by a stop: by release.
        self._pending: dict[str, _Pending] = {}
        # Requests listing a collection through Navidrome now: new placeholders wait.
        self._listing = 0
        self._listings_done: anyio.Event | None = None
        # Test hook: awaited at each named step of a library write (tests stop, cancel or
        # kill the process there).
        self.step: Callable[[str], Awaitable[None]] | None = None

    def lock_for(self, key: str) -> AbstractAsyncContextManager[None]:
        return self._locks.hold(key)

    async def _at(self, step: str) -> None:
        if self.step is not None:
            await self.step(step)

    # --- placeholders being written -----------------------------------------------

    def writing(self) -> bool:
        """Whether new placeholders are being written (or a stop left some half written)."""
        return bool(self._pending)

    async def idle(self) -> None:
        """Until no new placeholders are being written (a restart asked for in the dashboard
        waits for the writes under way to end)."""
        while running := [p for p in self._pending.values() if p.running]:
            with anyio.move_on_after(0.5):  # (looked at again: not every end sets the event)
                await running[0].ended.wait()

    def left(self) -> bool:
        """Whether new placeholders a stop (or a failed rollback) left half written wait to be
        taken out again (``repair_pending``)."""
        return any(not p.running for p in self._pending.values())

    async def written(self, song_id: str, *, wait: float) -> bool | None:
        """Whether ``song_id`` is a new placeholder being written - scanned before its row
        is recorded, or left half written by a stop. None: it is not (or nothing is being
        written); True: its write ended meanwhile (its row is recorded now, or its file is
        gone again); False: it did not end within ``wait`` seconds, or Navidrome could not
        say where the song is while a write is under way - either way it must not be
        forwarded as an owned song."""
        if not self._pending:
            return None
        try:
            song = await self.navidrome.song(song_id)
        except Exception as exc:
            # Not known where the song is: refused while a write is under way, or a file
            # left half written is still there (it could be that silent file); with those
            # files gone, no request can reach silence.
            unsafe = any(p.running for p in self._pending.values()) or (
                await anyio.to_thread.run_sync(self._left_files)
            )
            log.warning(
                "song %s not looked up while placeholders are pending (%s): %s",
                song_id,
                type(exc).__name__,
                "refused" if unsafe else "forwarded",
            )
            return False if unsafe else None
        path = str(song.get("path") or "") if song is not None else ""
        pending = next((p for p in self._pending.values() if path in p.paths), None)
        if pending is None:
            return None
        with anyio.move_on_after(wait):
            await pending.ended.wait()
        return pending.ended.is_set()

    def left_paths(self) -> set[str]:
        """The files of new placeholders left half written (not being written now)."""
        return {path for p in self._pending.values() if not p.running for path in p.paths}

    def _left_files(self) -> bool:
        """Whether a file left half written is still in the library."""
        return any(self.layout.absolute(path).exists() for path in self.left_paths())

    @asynccontextmanager
    async def listing(self, *, wait: float) -> AsyncIterator[bool]:
        """While a request lists a collection's songs through Navidrome - an album's or a
        playlist's archive, a shared album -, from its check until Navidrome's answer
        starts: no new placeholders move into the library (their writes wait, at most
        ``LISTING_WAIT_SECONDS``), and the writes under way end first. Yields whether they
        ended within ``wait`` (then every placeholder the collection can hold has its row,
        or is left half written: ``left_paths``)."""
        self._listing += 1
        try:
            with anyio.move_on_after(wait):
                for pending in [p for p in self._pending.values() if p.running]:
                    await pending.ended.wait()
            yield not any(p.running for p in self._pending.values())
        finally:
            self._listing -= 1
            if not self._listing and self._listings_done is not None:
                self._listings_done.set()

    async def _after_listings(self) -> None:
        """Before new placeholders are registered: the collections being listed first. The
        registration follows without a checkpoint, so a listing from then on waits for it.
        Never past a listing: after ``LISTING_WAIT_SECONDS`` the write is refused."""
        with anyio.move_on_after(LISTING_WAIT_SECONDS):
            while self._listing:
                if self._listings_done is None or self._listings_done.is_set():
                    self._listings_done = anyio.Event()
                await self._listings_done.wait()
        if self._listing:
            raise MaterializeError(
                "archives are being downloaded through Navidrome: nothing is added meanwhile"
            )

    async def load_pending(self) -> int:
        """Read the new placeholders a stop left half written, and the releases it left
        half added back (at startup, before requests are served: their songs are never
        forwarded as owned songs until they are taken out again - by ``repair_pending``,
        or before their release is written again)."""
        rows = await self.store.fetchall("SELECT * FROM pending_placeholders")
        for row in rows:
            ref = str(row["release_ref"])
            if ref not in self._pending:
                self._pending[ref] = _Pending(
                    ref,
                    str(row["folder"]),
                    frozenset(json.loads(row["paths"])),
                    bool(row["cover"]),
                    bool(row["new_folder"]),
                    running=False,
                )
        restoring = await self.store.fetchall(
            "SELECT ref, record FROM removed_releases WHERE restoring_at IS NOT NULL"
        )
        for row in restoring:
            ref = str(row["ref"])
            record = json.loads(row["record"])
            if ref not in self._pending:
                paths = frozenset(str(r["placeholder_path"]) for r in record["placeholders"])
                folder = str(record["release"]["folder"])
                self._pending[ref] = _Pending(
                    ref, folder, paths, False, True, running=False, restoring=True
                )
        if rows or restoring:
            log.warning("%d release(s) a stop left half written: taken out again once"
                        " Navidrome answers", len(rows) + len(restoring))  # fmt: skip
        return len(rows) + len(restoring)

    async def repair_pending(self) -> int:
        """New placeholders a stop left half written go again (their release's next commit
        or fill writes them anew), and so do releases it left half added back (their record
        stays); a targeted scan confirms it. Returns how many releases. Nothing while the
        library may not be written: they stay as they are, and intercepted."""
        if await self.refused():
            return 0
        undone = 0
        for ref in [r for r, p in self._pending.items() if not p.running]:
            try:  # each on its own: one that fails does not hold up the others
                async with self.lock_for(ref):
                    undone += await self._repair_left(ref)
            except Exception as exc:
                log.error("%s, left half written, is not repaired: %s", ref, type(exc).__name__)
        return undone

    async def _repair_left(self, ref: str) -> bool:
        pending = self._pending.get(ref)
        if pending is None or pending.running or await self.refused():
            return False  # (asked again for each, under its lock: an earlier answer is old)
        if pending.restoring:
            stored = await self.removed_record(ref)
            if stored is None or stored["restoring_at"] is None:
                self._ended(pending)  # added back, or undone, meanwhile
                return False
            return await self._undo_restore(stored)
        if not await self._undo_pending(pending):
            return False
        log.info("a stop interrupted writing %d placeholder(s) of %s: taken out again",
                 len(pending.paths), ref)  # fmt: skip
        return True

    async def settle(self, ref: str) -> None:
        """For a caller that holds the release's lock, before it reads or writes the
        release: the new placeholders a stop left half written go first (raises
        :class:`MaterializeError` when they cannot, or may not now)."""
        await self._settle_left(ref)

    async def _intend(
        self, ref: str, folder: str, planned: list[_Planned], cover: bool, *, new_folder: bool
    ) -> _Pending:
        """Record new placeholders as pending before their files move into the library."""
        pending = _Pending(
            ref, folder, frozenset(p.relative for p in planned), cover, new_folder, running=True
        )
        await self._after_listings()
        self._pending[ref] = pending
        try:
            # Asked again here, after the waits (the release's lock, the archives being
            # listed - up to a minute): Navidrome may have been restarted with another
            # setting meanwhile. Registered first: a listing from now on waits for this.
            await self._writable()
            # Shielded: a canceled wait for the statement would still write it.
            with anyio.CancelScope(shield=True):
                await self.store.execute(
                    "INSERT INTO pending_placeholders (release_ref, folder, paths, cover,"
                    " new_folder, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                    [ref, folder, json.dumps(sorted(pending.paths)), int(cover),
                     int(new_folder), self.clock()],
                )  # fmt: skip
        except BaseException as exc:
            self._ended(pending)  # no file was written
            if isinstance(exc, sqlite3.IntegrityError):
                raise MaterializeError(
                    "another write of this release is pending (another Shijhon process?)"
                ) from None
            raise
        return pending

    async def _settle_left(self, ref: str) -> None:
        """Before a release is written (under its lock): the new placeholders a stop left
        half written go first (an add-back a stop interrupted is finished by the write)."""
        pending = self._pending.get(ref)
        if pending is None or pending.running or pending.restoring:
            return
        await self._writable()  # asked here, under the lock: taking them out is a write
        if not await self._undo_pending(pending):
            raise MaterializeError("placeholders a stop left half written could not be taken out")

    async def _undo_pending(self, pending: _Pending, *, bound: float | None = None) -> bool:
        """The pending placeholders' files go (never a file a row records), a targeted scan
        confirms it (waited for ``bound`` seconds at most), then the pending record. False
        when that failed: the record stays (left, as after a stop: tried again before the
        release is written again, a minute later, and at the next start)."""
        try:
            taken = await self._recorded_paths(pending.folder)
            paths = sorted(pending.paths - taken)
            cover = pending.cover and (
                await self.store.fetchone(
                    "SELECT 1 FROM releases WHERE folder = ?", [pending.folder]
                )
                is None
            )
            confirmed = await self._roll_back(
                paths, pending.folder, cover, removed_folder=pending.new_folder, bound=bound
            )
            if not confirmed:
                raise MaterializeError("Navidrome still lists them")
            with anyio.CancelScope(shield=True):
                await self.store.execute(
                    "DELETE FROM pending_placeholders WHERE release_ref = ?", [pending.ref]
                )
        except Exception as exc:
            reason = getattr(exc, "reason", None) or getattr(exc, "strerror", None)
            log.error("new placeholders of %s could not be taken out again (%s): tried again"
                      " later", pending.ref, reason or type(exc).__name__)  # fmt: skip
            pending.running = False
            return False
        self._ended(pending)
        return True

    async def _forget_pending(self, pending: _Pending) -> None:
        """A write that moved no file into the library: its pending record goes, with no
        file to take out and no scan (Navidrome is not asked to scan for nothing - least
        of all while it would purge missing files). A record that cannot go stays as a
        stop would leave it: the repairs see to it."""
        try:
            await self.store.execute(
                "DELETE FROM pending_placeholders WHERE release_ref = ?", [pending.ref]
            )
        except Exception as exc:
            log.error("the pending record of %s could not be removed (%s): tried again later",
                      pending.ref, type(exc).__name__)  # fmt: skip
            pending.running = False
            return
        self._ended(pending)

    def _ended(self, pending: _Pending) -> None:
        if self._pending.get(pending.ref) is pending:
            del self._pending[pending.ref]
        pending.ended.set()

    async def _still_pending(self, ref: str) -> bool:
        """Whether the pending record of ``ref`` is still there (its transaction was not
        done); True when that cannot be read."""
        try:
            row = await self.store.fetchone(
                "SELECT 1 FROM pending_placeholders WHERE release_ref = ?", [ref]
            )
        except Exception as exc:
            log.error("pending placeholders of %s not read: %s", ref, type(exc).__name__)
            return True
        return row is not None

    async def _recorded_paths(self, folder: str) -> set[str]:
        """The library-relative paths rows record in ``folder`` (current and silent)."""
        rows = await self.store.fetchall(
            "SELECT path, placeholder_path FROM placeholders WHERE path LIKE ? ESCAPE '\\'"
            " OR placeholder_path LIKE ? ESCAPE '\\'",
            [_like_prefix(folder) + "%"] * 2,
        )
        return {str(r["path"]) for r in rows} | {str(r["placeholder_path"]) for r in rows}

    # --- owned albums -----------------------------------------------------------------

    async def owned_album(self, album_id: str) -> OwnedAlbum:
        songs = await self.navidrome.songs_of_album(album_id)
        owned = [
            s
            for s in songs
            if not s.get("missing") and not self.layout.is_placeholder_path(s["path"])
        ]
        return OwnedAlbum(album_id, owned)

    # --- materialize ------------------------------------------------------------------

    async def materialize(
        self,
        release: CatalogRelease,
        *,
        owned_album_id: str | None = None,
        links: Mapping[CatalogRef, str] | None = None,
        only: Iterable[CatalogRef] | None = None,
        cover: bytes | None = None,
    ) -> MaterializeResult:
        """Create placeholders for the release's tracks that are neither linked to an owned
        song (``links``) nor already materialized. ``only`` limits which tracks to create."""
        async with self.lock_for(str(release.ref)):
            result, planned, linked = await self.materialize_held(
                release, owned_album_id=owned_album_id, links=links, only=only, cover=cover
            )
        # The owner's recordings of new tracks on other albums (outside the release's lock).
        await self.back_placeholders(release, result, planned, linked)
        return result

    async def materialize_held(
        self,
        release: CatalogRelease,
        *,
        owned_album_id: str | None = None,
        links: Mapping[CatalogRef, str] | None = None,
        only: Iterable[CatalogRef] | None = None,
        cover: bytes | None = None,
    ) -> tuple[MaterializeResult, list[_Planned], list[str]]:
        """``materialize`` for a caller that holds the release's lock (``lock_for``); no
        backing is looked up. Returns (the result, the new placeholders, the linked songs)."""
        links = dict(links or {})
        only = list(only) if only is not None else None
        await self._writable()
        ref = str(release.ref)
        await self._settle_left(ref)
        if ref not in await self._removed_releases():
            return await self._materialize(release, owned_album_id, links, only, cover)

        async def anew() -> None:
            """Whether the release's tracks, added anew, fit the owned album as it is now
            (raises ``MaterializeError``: they do not)."""
            if not owned_album_id:
                return
            owned = await self.owned_album(owned_album_id)
            if not owned.songs:
                raise MaterializeError("the owned album has no owned songs")
            fresh = [
                t for t in release.tracks if t.ref not in links and (only is None or t.ref in only)
            ]
            await self._fits_owned(release, owned, links, fresh)

        # Taken out as unused: back from its record first (the same song IDs); a record
        # that does not fit is dropped, and its old IDs stay known until the release is made
        # anew (a stream of one waits for it: never Navidrome's silent file meanwhile).
        await self._restore_held(
            ref, cover, added_as=(owned_album_id,), owned_now=set(links), keep_ids=True, anew=anew
        )
        try:
            return await self._materialize(release, owned_album_id, links, only, cover)
        finally:
            if ref in self._removed_refs and await self.removed_record(ref) is None:
                await self._forget_removed(ref, in_database=False)

    async def _fits_owned(
        self,
        release: CatalogRelease,
        owned: OwnedAlbum,
        linked: Mapping[CatalogRef, str],
        to_create: list[CatalogTrack],
    ) -> None:
        """Every addition to an owned album keeps the rules its first fill keeps - whoever
        asks: a fill, a song added to an album filled before, a refresh. Refused
        (``MaterializeError``, nothing written): a release whose track list could not be
        read in full; one whose track numbers are only the order the catalog listed them
        in (``numbered`` off) while the album's songs - the owned ones it is linked to, the
        placeholders it has there - sit elsewhere; and a new track whose disc and number
        another song of the album has. ``linked``: track -> the song it is."""
        if release.incomplete:
            raise MaterializeError(
                "the catalog's track list of the release is incomplete: nothing is added"
                " to an owned album"
            )
        files = {
            str(s["id"]): (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0))
            for s in owned.songs
        }
        rows = await self.store.fetchall(
            "SELECT track_ref, disc, track FROM placeholders WHERE release_ref = ?",
            [str(release.ref)],
        )
        placed = {str(r["track_ref"]): (int(r["disc"]), int(r["track"])) for r in rows}
        if not release.numbered:
            for track in release.tracks:
                at = (track.disc, track.number)
                song = linked.get(track.ref)
                elsewhere = song is not None and files.get(song, at) != at
                if elsewhere or placed.get(str(track.ref), at) != at:
                    raise MaterializeError(
                        "the catalog lists the release's tracks without numbers, and the"
                        " album's songs are numbered differently from its order: nothing is"
                        " added"
                    )
        taken = set(files.values()) | set(placed.values())
        for track in to_create:
            if (track.disc, track.number) in taken:
                raise MaterializeError(
                    f"{track.title!r} would take another song's place in the owned album"
                    " (its disc and number): nothing is added"
                )

    async def refused(self) -> str | None:
        """Why the library may not be written now, or None - asked of Navidrome now, never
        from an earlier answer: its setting changes with a restart of it, which Shijhon
        does not see."""
        if self.writes_refused is None:
            return None
        return await self.writes_refused(fresh=True)

    async def _writable(self) -> None:
        if why := await self.refused():
            raise MaterializeError(why)

    async def _swappable(self) -> None:
        if why := await self.refused():
            raise ReplaceError(why)

    async def _still_swappable(self, staged: Path) -> None:
        """``_swappable``, asked again once ``staged`` is made, right before a file moves, a
        row changes or a scan is asked for: refused - or canceled while asking - the
        staged file goes, and nothing else has happened."""
        try:
            await self._swappable()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await anyio.Path(staged).unlink(missing_ok=True)
            raise

    async def _materialize(
        self,
        release: CatalogRelease,
        owned_album_id: str | None,
        links: dict[CatalogRef, str],
        only: Iterable[CatalogRef] | None,
        cover: bytes | None,
    ) -> tuple[MaterializeResult, list[_Planned], list[str]]:
        existing = await self.store.fetchone(
            "SELECT folder, album_id, owned_album_id, album_tags FROM releases WHERE ref = ?",
            [str(release.ref)],
        )
        if existing is not None and existing["owned_album_id"]:
            owned_album_id = owned_album_id or existing["owned_album_id"]
        known = {
            CatalogRef.parse(row["track_ref"]): row["song_id"]
            for row in await self.store.fetchall(
                "SELECT track_ref, song_id FROM track_links WHERE release_ref = ?",
                [str(release.ref)],
            )
        }
        wanted = set(only) if only is not None else None
        to_create = [
            t
            for t in release.tracks
            if t.ref not in links and t.ref not in known and (wanted is None or t.ref in wanted)
        ]
        try:
            folder = (
                existing["folder"]
                if existing
                else self.layout.release_folder(release.ref, release.artist, release.title)
            )
        except LayoutError as exc:
            raise MaterializeError(f"{exc}: nothing written") from None
        result = MaterializeResult(release.ref, existing["album_id"] if existing else "")
        result.folder = folder
        result.linked = {ref: sid for ref, sid in links.items() if ref not in known}
        if not to_create:
            if not existing and not result.linked:
                raise MaterializeError("nothing to materialize")
            if not existing:
                assert owned_album_id is not None, "links without placeholders need the album"
                result.album_id = owned_album_id
            await self._record(release, result, [], owned_album_id, existing is None)
            return result, [], []

        owned = await self.owned_album(owned_album_id) if owned_album_id else None
        if owned_album_id and (owned is None or not owned.songs):
            raise MaterializeError("the owned album has no owned songs")
        if owned is not None:
            await self._fits_owned(release, owned, {**known, **links}, to_create)
        # Later additions reuse the release's album tags so they always join the same album.
        album_tags: dict[str, list[str]] = (
            json.loads(existing["album_tags"])
            if existing is not None
            else await self._album_tags(release, owned)
        )
        result.album_tags = album_tags
        taken = await self._names_taken(folder)
        planned = []
        for track in to_create:
            # Never the name of a file already there (a retag keeps a placeholder's name,
            # so another track can come to want it).
            name = Layout.file_name(track)
            if name.casefold() in taken:
                digest = hashlib.sha256(str(track.ref).encode()).hexdigest()[:6]
                name = Layout.file_name(track, f" [{digest}].flac")
            taken.add(name.casefold())
            relative = f"{folder}/{name}"
            planned.append(
                _Planned(track, relative, tagging.placeholder_tags(album_tags, track, release))
            )
        try:  # whatever the catalog's names: nothing is written outside the folder
            await anyio.to_thread.run_sync(self._really_inside, [p.relative for p in planned])
        except LayoutError as exc:
            raise MaterializeError(f"{exc}: nothing written") from None
        if self.tag_hook is not None:
            for item in planned:
                self.tag_hook(item.comments)
        before = await self._album_state(owned_album_id or result.album_id or None)
        write_cover = cover is not None and owned is None and not existing
        ref = str(release.ref)
        pending = await self._intend(ref, folder, planned, write_cover, new_folder=not existing)
        started = time.monotonic()
        recorded = False
        moved: list[bool] = []  # (set once the first file moves into the library)
        try:
            await self._at("intended")
            await self._write(
                planned, folder, cover if write_cover else None, moving=lambda: moved.append(True)
            )
            await self._at("in place")
            album_id = await self._verify_new(planned, folder, owned_album_id, before, existing)
            await self._at("verified")
            result.scan_seconds = time.monotonic() - started
            result.album_id = album_id
            songs = {
                s["path"]: s["id"]
                for s in await self.navidrome.songs_under(folder)
                if not s.get("missing")
            }
            result.created = {item.track.ref: songs[item.relative] for item in planned}
            await self._record(release, result, planned, owned_album_id, existing is None)
            recorded = True
            self._ended(pending)
            await self._at("recorded")
        except BaseException as exc:
            # Nothing may stay in the library that Shijhon has no record of: the files go
            # again unless their rows were written (that transaction ended the pending
            # record too).
            with anyio.CancelScope(shield=True):
                if recorded or not await self._still_pending(ref):
                    self._ended(pending)
                    raise
                if moved:
                    await self._undo_pending(pending, bound=_bound(exc))
                else:  # nothing reached the library (refused, or failed while staging)
                    await self._forget_pending(pending)
            if isinstance(exc, MaterializeError):
                raise MaterializeError(exc.reason) from None
            if isinstance(exc, Exception):
                log.warning("materialize failed: %s", type(exc).__name__)
                raise MaterializeError(f"unexpected {type(exc).__name__}") from exc
            raise
        return result, planned, [*known.values(), *links.values()]

    async def back_placeholders(
        self,
        release: CatalogRelease,
        result: MaterializeResult | None,
        planned: list[_Planned] | list[tuple[CatalogTrack, str]],
        linked: list[str],
    ) -> None:
        """Back placeholders whose recordings the owner has on other albums: the new
        ones (``planned`` with the ``result``), or (track, placeholder) pairs. A failure
        leaves them to the add-ons."""
        if self.backing is None or not planned:
            return
        pairs = [
            (item.track, result.created[item.track.ref])
            if isinstance(item, _Planned) and result is not None
            else item
            for item in planned
        ]
        try:
            found = await self.backing.back(pairs, release.title, linked)  # type: ignore[arg-type]
            for placeholder, owned in found.items():
                await self.set_backing(placeholder, owned)
        except Exception as exc:
            log.warning("owned recordings not looked up: %s", type(exc).__name__)
            return
        if found:
            log.info(
                "%d new placeholder(s) of %s - %s play owned recordings of other albums",
                len(found),
                release.artist,
                release.title,
            )

    def _inside(self, relative: str) -> Path:
        """A placeholder's recorded path, for a write: inside the placeholder folder - one
        recorded elsewhere (a name that leads out of it, or another placeholder folder) is
        never written to (``ReplaceError``)."""
        try:
            return self.layout.contained(relative, resolve=True)
        except LayoutError as exc:
            raise ReplaceError(f"{exc}: not written") from None

    def _really_inside(self, paths: Iterable[str]) -> None:
        """Each of the paths lies inside the placeholder folder, also by where its folder
        really is (``LayoutError`` otherwise); reads the file system: in a worker thread."""
        for path in paths:
            self.layout.contained(path, resolve=True)

    def _own(self, relative: str, *, folder: bool = False) -> Path | None:
        """A recorded path, for taking a file away (a rollback, a removal) or - a release's
        ``folder`` - for its cover and the folder itself: None - it is left alone, and
        said - for one outside the placeholder folder: nothing there is Shijhon's to
        change."""
        try:
            return self.layout.contained(relative, resolve=True, folder=folder)
        except LayoutError as exc:
            log.error("a recorded placeholder path is left alone: %s", exc)
            return None

    async def _names_taken(self, folder: str) -> set[str]:
        """File names in the release's folder (on disk, and as recorded), case-folded."""
        target = self.layout.absolute(folder)
        names = await anyio.to_thread.run_sync(
            lambda: {p.name.casefold() for p in target.iterdir()} if target.is_dir() else set()
        )
        for row in await self.store.fetchall(
            "SELECT path, placeholder_path FROM placeholders WHERE path LIKE ? ESCAPE '\\'",
            [_like_prefix(folder) + "%"],
        ):
            for path in (row["path"], row["placeholder_path"]):
                names.add(str(path).rsplit("/", 1)[-1].casefold())
                names.add(Path(str(path)).with_suffix(".flac").name.casefold())
        return names

    async def _album_tags(
        self, release: CatalogRelease, owned: OwnedAlbum | None
    ) -> dict[str, list[str]]:
        if owned is None:
            return tagging.catalog_album_tags(release)
        first = sorted(owned.songs, key=lambda s: (s.get("discNumber", 0), s.get("trackNumber", 0)))
        path = self.layout.absolute(first[0]["path"])
        try:
            return await anyio.to_thread.run_sync(tagging.album_tags_of, path)
        except (tagging.UnsupportedFormat, OSError) as exc:
            raise MaterializeError(f"cannot read the owned album's tags ({exc})") from None

    async def _album_state(self, album_id: str | None) -> dict[str, str]:
        """song ID -> album ID of the album's current songs (present ones only)."""
        if not album_id:
            return {}
        songs = await self.navidrome.songs_of_album(album_id)
        return {s["id"]: s["albumId"] for s in songs if not s.get("missing")}

    async def _write(
        self,
        planned: list[_Planned],
        folder: str,
        cover: bytes | None,
        *,
        gated: bool = True,
        moving: Callable[[], None] | None = None,
    ) -> None:
        """Stage the files, then move them into the library. ``gated``: Navidrome is asked
        once more between the two - staging took a moment - and a refusal leaves nothing
        staged (not for a compensation, which puts back what was there). ``moving``: called
        by the move itself, right before its first change in the library: a failure before
        it left the library as it was (nothing to take out again, nothing to scan)."""
        self.layout.ensure()
        # The last check before anything is written: by its name, and where it really is.
        target = self.layout.contained(folder)
        await anyio.to_thread.run_sync(self._really_inside, [p.relative for p in planned])
        staged: list[tuple[Path, Path]] = []

        async def prepare(item: _Planned) -> None:
            stage = self.layout.staging / f"{uuid.uuid4().hex}.flac"
            await self.silence.write(item.track.duration_ms, stage)
            await anyio.to_thread.run_sync(tagging.write_flac, stage, item.comments)
            await anyio.to_thread.run_sync(durable.sync_file, stage)
            staged.append((stage, self.layout.absolute(item.relative)))

        try:
            async with anyio.create_task_group() as tg:
                for item in planned:
                    tg.start_soon(prepare, item)
            await self._at("staged")
            if gated:  # right before the files move
                await self._writable()
        except BaseException:
            for stage, _ in staged:
                stage.unlink(missing_ok=True)
            raise

        def move() -> None:
            # (said here, in the worker itself: a wait for a worker that is canceled
            # before this runs has moved nothing)
            if moving is not None:
                moving()
            made = durable.made_dirs(target)
            target.mkdir(parents=True, exist_ok=True)
            if cover is not None:
                cover_stage = self.layout.staging / f"{uuid.uuid4().hex}.jpg"
                cover_stage.write_bytes(cover)
                durable.sync_file(cover_stage)
                os.replace(cover_stage, target / "cover.jpg")
            for stage, final in staged:
                os.replace(stage, final)
            # On disk - the files' names, and the folders made for them - before anything
            # records them.
            durable.sync_dirs(target, *(d.parent for d in made), self.layout.staging)

        try:
            await anyio.to_thread.run_sync(move)
        except BaseException:  # what did not move does not stay staged
            for stage, _ in staged:
                stage.unlink(missing_ok=True)
            raise

    async def _verify_new(
        self,
        planned: list[_Planned],
        folder: str,
        owned_album_id: str | None,
        before: dict[str, str],
        existing: Any,
    ) -> str:
        paths = {item.relative for item in planned}

        async def indexed() -> bool:
            songs = await self.navidrome.songs_under(folder)
            present = {s["path"] for s in songs if not s.get("missing")}
            return paths <= present

        if not await self.scans.until([folder], indexed):
            raise MaterializeError("the new placeholders were not indexed")
        songs = [s for s in await self.navidrome.songs_under(folder) if not s.get("missing")]
        new_albums = {str(s["albumId"]) for s in songs if s["path"] in paths}
        if len(new_albums) != 1:
            raise MaterializeError("the placeholders were split across several albums")
        album_id = new_albums.pop()
        expected_album = owned_album_id or (existing["album_id"] if existing else None)
        if expected_album and album_id != expected_album:
            raise MaterializeError("the album split: placeholders formed a separate album")
        after = await self._album_state(album_id)
        changed = [sid for sid, aid in before.items() if after.get(sid) != aid]
        if changed:
            raise MaterializeError(f"{len(changed)} existing song(s) changed ID or album")
        in_folder = {s["id"] for s in songs}
        others = set(after) - set(before) - in_folder
        if others:
            raise MaterializeError("the placeholders joined another album")
        if len(after) != len(before) + len(planned):
            raise MaterializeError(
                f"expected {len(before) + len(planned)} songs in the album, found {len(after)}"
            )
        return album_id

    async def _roll_back(
        self,
        relative: Iterable[str],
        folder: str,
        cover: bool,
        *,
        removed_folder: bool,
        bound: float | None = None,
    ) -> bool:
        """New files go again (the folder too when it was new and is empty now), then a
        targeted scan until Navidrome lists none of them (``bound``: waited for so long at
        most). False when that scan failed, was not done in time or still lists them
        (logged: the files are gone, a later scan notices). Raises OSError when a file
        cannot go."""
        paths = set(relative)

        def remove() -> None:
            for path in paths:
                if (file := self._own(path)) is not None:
                    file.unlink(missing_ok=True)
            target = self._own(folder, folder=True)
            if target is None:
                return
            if cover:
                (target / "cover.jpg").unlink(missing_ok=True)
            if removed_folder and target.exists() and not any(target.iterdir()):
                target.rmdir()
            # Gone for good before the record of them goes.
            durable.sync_dirs(target, target.parent)

        await anyio.to_thread.run_sync(remove)

        async def gone() -> bool:
            songs = await self.navidrome.songs_under(folder)
            return not any(s["path"] in paths and not s.get("missing") for s in songs)

        parent = folder if not removed_folder else folder.rsplit("/", 1)[0]
        try:
            with anyio.move_on_after(bound):
                if await self.scans.until([parent], gone):
                    return True
            log.error("rolled-back placeholders are not confirmed gone from %s", folder)
        except Exception as exc:  # the files are gone; the next scan will notice
            log.error("rescan after rollback failed: %s", type(exc).__name__)
        return False

    async def _record(
        self,
        release: CatalogRelease,
        result: MaterializeResult,
        planned: list[_Planned],
        owned_album_id: str | None,
        new_release: bool,
    ) -> None:
        now = self.clock()
        async with self.store.transaction() as conn:
            if new_release:
                await conn.execute(
                    "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
                    " album_tags, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        str(release.ref),
                        result.folder,
                        result.album_id,
                        owned_album_id,
                        release.title,
                        release.artist,
                        json.dumps(result.album_tags),
                        release_json(release),
                        now,
                    ],
                )
            for ref, song_id in result.linked.items():
                await conn.execute(
                    "INSERT OR REPLACE INTO track_links (track_ref, song_id, release_ref, owned)"
                    " VALUES (?, ?, ?, 1)",
                    [str(ref), song_id, str(release.ref)],
                )
            if planned:  # recorded now: no longer pending
                await conn.execute(
                    "DELETE FROM pending_placeholders WHERE release_ref = ?", [str(release.ref)]
                )
            for item in planned:
                song_id = result.created[item.track.ref]
                track = item.track
                await conn.execute(
                    "INSERT OR REPLACE INTO track_links (track_ref, song_id, release_ref, owned)"
                    " VALUES (?, ?, ?, 0)",
                    [str(track.ref), song_id, str(release.ref)],
                )
                await conn.execute(
                    "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref,"
                    " release_ref, isrc, title, artist, album, duration_ms, disc, track, tags,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        song_id,
                        item.relative,
                        item.relative,
                        str(track.ref),
                        str(release.ref),
                        track.isrc,
                        track.title,
                        track.artist,
                        release.title,
                        track.duration_ms,
                        track.disc,
                        track.number,
                        json.dumps(item.comments),
                        now,
                    ],
                )

    # --- taking releases out again, and adding them back ----------------------------

    async def remove_release(
        self,
        ref: str,
        *,
        check: UsageCheck | None = None,
        also: Statements | None = None,
        record: bool = True,
    ) -> Removal:
        """Take a release's placeholders out of the library, leaving owned songs as they are.
        Nothing Shijhon has a use recorded of is taken out (delivered audio, a stream or
        download, a use a check saw: ``seen_uses``), and nothing ``check`` finds in use -
        asked under the release's and its songs' locks, and again once the files are gone
        (a use meanwhile puts them back). The release is marked first, then its files go
        (its cover waits in the staging folder) and a targeted scan confirms it; any failure
        puts it back (a stop in the middle: ``repair_removals`` at the next start). Then, in
        one transaction,
        Shijhon's rows go - kept as a record (``record``) under which its old IDs add it
        back - with ``also``'s statements."""
        await self._writable()
        async with self.lock_for(ref):
            await self._settle_left(ref)
            release = await self.store.fetchone("SELECT * FROM releases WHERE ref = ?", [ref])
            if release is None:
                return Removal()
            songs = await self.store.fetchall(
                "SELECT song_id FROM placeholders WHERE release_ref = ? ORDER BY song_id", [ref]
            )
            async with AsyncExitStack() as held:
                for song in songs:  # a swap or a download of one waits (or is waited for)
                    await held.enter_async_context(self.lock_for(f"song:{song['song_id']}"))
                await self._writable()  # asked again under the locks (they may take long)
                rows = await self._release_rows(ref)
                if not rows:
                    return Removal()
                # Shijhon's own records of a use keep it, whoever asks: delivered
                # audio, a stream or download it served, a use a check saw in Navidrome's
                # records - also one that ended since.
                if why := used_here(rows) + used_before(await self.seen_uses(ref)):
                    return Removal(kept=why)
                if check is not None and (why := await check(rows, False)):
                    return Removal(kept=why)
                await self._writable()  # ... and after the check, right before its files go
                await self.store.execute(
                    "UPDATE releases SET removing_at = ? WHERE ref = ?", [self.clock(), ref]
                )
                # A use of its IDs meanwhile waits for the end (``removing``), then adds the
                # release back if it went.
                marked = [str(r["song_id"]) for r in rows] + [str(release["album_id"])]
                self._removing.update(dict.fromkeys(marked, ref))
                self._removing_albums.add(str(release["album_id"]))
                try:
                    await self._take_out(release, rows)
                    if check is not None and (
                        why := await check(await self._release_rows(ref), True)
                    ):
                        raise _InUse(why)
                    links = await self.store.fetchall(
                        "SELECT * FROM track_links WHERE release_ref = ?", [ref]
                    )
                    async with self.store.transaction() as conn:
                        if why := await _used_meanwhile(conn, ref):
                            raise _InUse(why)
                        if record:
                            await self._keep_record(conn, release, links, rows)
                        await conn.execute("DELETE FROM placeholders WHERE release_ref = ?", [ref])
                        await conn.execute("DELETE FROM track_links WHERE release_ref = ?", [ref])
                        await conn.execute("DELETE FROM releases WHERE ref = ?", [ref])
                        if also is not None:
                            await also(conn)
                except BaseException as exc:
                    with anyio.CancelScope(shield=True):
                        back = await self._put_back(release, bound=_bound(exc))
                    if isinstance(exc, _InUse):
                        return Removal(kept=exc.reasons, unfinished=not back)
                    if isinstance(exc, MaterializeError):
                        raise
                    if isinstance(exc, Exception):
                        log.warning("taking %s out failed: %s", ref, type(exc).__name__)
                        raise MaterializeError(f"unexpected {type(exc).__name__}") from exc
                    raise
                finally:
                    for old in marked:
                        self._removing.pop(old, None)
                    self._removing_albums.discard(str(release["album_id"]))
                if record:
                    await self._removed_releases()  # loaded, then kept up to date here
                    assert self._removed is not None
                    self._removed_refs.add(ref)
                    ids = [str(r["song_id"]) for r in rows]
                    catalog = not release["owned_album_id"]
                    for old in [*ids, str(release["album_id"])] if catalog else ids:
                        self._removed[old] = ref
                    if catalog:
                        self._removed_albums.add(str(release["album_id"]))
        await anyio.to_thread.run_sync(self._discard_cover, str(release["folder"]))
        log.info("took out %d placeholder(s) of %s - %s (%s)", len(rows), release["artist"],
                 release["title"], ref)  # fmt: skip
        return Removal(removed=len(rows))

    async def seen_uses(self, ref: str) -> list[str]:
        """The uses of the release a check saw in Navidrome's records, the earliest
        first: they keep it, also once Navidrome no longer shows them."""
        rows = await self.store.fetchall(
            "SELECT reason FROM seen_uses WHERE release_ref = ? ORDER BY seen_at, reason", [ref]
        )
        return [str(r["reason"]) for r in rows]

    async def note_uses(self, seen: Mapping[str, Iterable[str]], at: float) -> None:
        """Record the uses a check saw, by release (those still in the library; a use
        recorded before keeps its first time)."""
        found = [(ref, reason, at, ref) for ref, reasons in seen.items() for reason in reasons]
        if not found:
            return
        async with self.store.transaction() as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO seen_uses (release_ref, reason, seen_at)"
                " SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM releases WHERE ref = ?)",
                found,
            )

    async def _release_rows(self, ref: str) -> list[Any]:
        return await self.store.fetchall(
            "SELECT * FROM placeholders WHERE release_ref = ? ORDER BY disc, track, song_id", [ref]
        )

    def _cover_aside(self, folder: str) -> Path:
        """Where a release's cover waits while it is taken out (the staging folder: never
        indexed; a stale one goes at a start after an hour)."""
        digest = hashlib.sha256(folder.encode()).hexdigest()[:16]
        return self.layout.staging / f"removing-{digest}.jpg"

    async def _take_out(self, release: Any, rows: list[Any]) -> None:
        """The files go (the cover aside), until Navidrome lists none of the songs."""
        folder = str(release["folder"])
        aside = self._cover_aside(folder)

        def remove() -> None:
            self.layout.ensure()
            for row in rows:
                for path in {row["path"], row["placeholder_path"]}:
                    if (file := self._own(str(path))) is not None:
                        file.unlink(missing_ok=True)
            target = self._own(folder, folder=True)
            if target is None:
                return
            cover = target / "cover.jpg"
            if cover.exists():
                os.replace(cover, aside)
            if target.exists() and not any(target.iterdir()):
                target.rmdir()
            durable.sync_dirs(target, target.parent, self.layout.staging)

        await anyio.to_thread.run_sync(remove)
        paths = {str(r["path"]) for r in rows}

        async def gone() -> bool:
            songs = await self.navidrome.songs_under(folder)
            return not any(s["path"] in paths and not s.get("missing") for s in songs)

        parent = folder.rsplit("/", 1)[0] if "/" in folder else folder
        if not await self.scans.until([parent], gone):
            raise MaterializeError("the placeholders taken out are still listed")

    async def _put_back(
        self, release: Any, *, bound: float | None = None, gated: bool = False
    ) -> bool:
        """A release whose removal did not finish: its files back from its rows (the same
        paths and tags, so the same song IDs), its mark cleared. False when that failed
        too, or Navidrome did not confirm it within ``bound`` seconds (it stays marked:
        tried again at the next start). ``gated``: a repair (not a removal's own
        compensation) - Navidrome is asked again once the files are staged, before they
        move and the scan; refused, nothing moved and it stays marked. The caller holds the
        release's lock and its songs' locks (a swap of one of its songs meanwhile - after a
        stop - leaves delivered audio, which stays as it is)."""
        ref = str(release["ref"])
        folder = str(release["folder"])
        rows = await self._release_rows(ref)
        if not rows:  # taken out after all (its transaction was done): nothing to put back
            await self.load_removed()
            return False
        planned = [
            _Planned(_row_track(r), str(r["placeholder_path"]), json.loads(r["tags"]))
            for r in rows
            if r["state"] == "placeholder"
            and not self.layout.absolute(str(r["placeholder_path"])).exists()
        ]
        aside = self._cover_aside(folder)
        try:
            await self._at("put back planned")
            if planned:  # (a compensation: back whatever Navidrome's setting is now)
                await self._write(planned, folder, None, gated=gated)
            elif gated:  # (nothing staged, so asked here: a scan follows)
                await self._writable()
            if aside.exists():

                def cover_back() -> None:
                    if (target := self._own(folder, folder=True)) is not None:
                        os.replace(aside, target / "cover.jpg")
                        durable.sync_dirs(target, self.layout.staging)

                await anyio.to_thread.run_sync(cover_back)
            expected = {str(r["path"]): str(r["song_id"]) for r in rows}
            back = False
            with anyio.move_on_after(bound):
                back = await self.scans.until([folder], lambda: self._listed(folder, expected))
            if not back:
                raise MaterializeError("the placeholders did not come back")
        except Exception as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            log.error("%s could not be put back (%s): tried again at the next start", ref, reason)
            return False
        await self.store.execute("UPDATE releases SET removing_at = NULL WHERE ref = ?", [ref])
        log.info("put back %s - %s: it stays in the library", release["artist"], release["title"])
        return True

    async def _listed(self, folder: str, expected: dict[str, str]) -> bool:
        """Whether Navidrome lists each path in ``folder`` as present with its song ID."""
        songs = await self.navidrome.songs_under(folder)
        present = {s["path"]: s["id"] for s in songs if not s.get("missing")}
        return all(present.get(path) == song for path, song in expected.items())

    def _discard_cover(self, folder: str) -> None:
        self._cover_aside(folder).unlink(missing_ok=True)

    async def _keep_record(
        self, conn: aiosqlite.Connection, release: Any, links: list[Any], rows: list[Any]
    ) -> None:
        data = {
            "release": dict(release),
            "links": [dict(r) for r in links],
            "placeholders": [dict(r) for r in rows],
        }
        await conn.execute(
            "INSERT OR REPLACE INTO removed_releases (ref, album_id, owned_album_id, title,"
            " artist, record, created_at, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [release["ref"], release["album_id"], release["owned_album_id"], release["title"],
             release["artist"], json.dumps(data), release["created_at"], self.clock()],
        )  # fmt: skip
        await conn.executemany(
            "INSERT OR REPLACE INTO removed_songs (song_id, release_ref) VALUES (?, ?)",
            [(r["song_id"], release["ref"]) for r in rows],
        )

    async def repair_removals(self) -> int:
        """Releases a stop left half taken out: put back (the next daily check decides
        again); releases a stop left half added back: their files go again (the next use
        adds them back); covers set aside by neither: gone. Returns how many releases.
        Nothing while the library may not be written."""
        if await self.refused():
            return 0
        marked = await self.store.fetchall("SELECT ref FROM releases WHERE removing_at IS NOT NULL")
        repaired = 0
        for row in marked:
            ref = str(row["ref"])
            # As a removal: the release's lock, then its songs' (a swap or a download of one
            # waits, or is waited for), and the mark read again under them.
            async with self.lock_for(ref), AsyncExitStack() as held:
                songs = await self.store.fetchall(
                    "SELECT song_id FROM placeholders WHERE release_ref = ? ORDER BY song_id",
                    [ref],
                )
                for song in songs:
                    await held.enter_async_context(self.lock_for(f"song:{song['song_id']}"))
                release = await self.store.fetchone(
                    "SELECT * FROM releases WHERE ref = ? AND removing_at IS NOT NULL", [ref]
                )
                if release is not None and not await self.refused():
                    repaired += await self._put_back(release, gated=True)
        restoring = await self.store.fetchall(
            "SELECT ref FROM removed_releases WHERE restoring_at IS NOT NULL"
        )
        for row in restoring:
            async with self.lock_for(str(row["ref"])):
                stored = await self.removed_record(str(row["ref"]))
                undo = stored is not None and stored["restoring_at"] is not None
                if undo and not await self.refused():
                    repaired += await self._undo_restore(stored)
        still = await self.store.fetchall(
            "SELECT folder FROM releases WHERE removing_at IS NOT NULL"
        )
        kept = {self._cover_aside(str(r["folder"])).name for r in still}

        def stale_covers() -> None:
            staging = self.layout.staging
            for path in staging.glob("removing-*.jpg") if staging.exists() else []:
                if path.name not in kept:
                    path.unlink(missing_ok=True)

        await anyio.to_thread.run_sync(stale_covers)
        return repaired

    async def _undo_restore(self, stored: Any) -> bool:
        """A restore a stop interrupted: its files go again, and its record stays. False
        when Navidrome did not confirm it: its mark stays (tried again a minute later, and
        at the next start), and its songs stay intercepted."""
        ref = str(stored["ref"])
        record = json.loads(stored["record"])
        rows, folder = record["placeholders"], str(record["release"]["folder"])
        catalog = not stored["owned_album_id"]
        paths = [str(r["placeholder_path"]) for r in rows]
        left = self._pending.get(ref)
        if left is None:  # (a caller that did not load them, e.g. a command)
            left = self._pending[ref] = _Pending(
                ref, folder, frozenset(paths), False, True, running=False, restoring=True
            )
        try:
            confirmed = await self._roll_back(paths, folder, catalog, removed_folder=True)
        except OSError as exc:  # a file that cannot go: as not confirmed
            log.error("%s: %s", ref, exc.strerror or type(exc).__name__)
            confirmed = False
        if not confirmed:
            log.error("the add-back of %s a stop interrupted is not confirmed taken out: tried"
                      " again later", ref)  # fmt: skip
            return False
        await self.store.execute(
            "UPDATE removed_releases SET restoring_at = NULL WHERE ref = ?", [ref]
        )
        if not left.running:
            self._ended(left)  # no longer intercepted: Navidrome has no file for its songs
        log.info("a stop interrupted adding %s back: its files are gone again", ref)
        return True

    async def _drop_record(self, ref: str, stored: Any, *, keep_ids: bool = False) -> None:
        """A removed release's record goes (the release is added anew, or its album is the
        owner's now) - after the files of an add-back a stop interrupted: no file may stay
        that no record names. ``keep_ids``: its old IDs stay known (until it is made anew)."""
        if stored["restoring_at"] is not None and not await self._undo_restore(stored):
            raise MaterializeError("an add-back a stop interrupted could not be taken out again")
        await self._forget_removed(ref, in_memory=not keep_ids)

    async def _removed_releases(self) -> set[str]:
        """The releases taken out (loaded on first use)."""
        if self._removed is None:
            await self.load_removed()
        return self._removed_refs

    async def load_removed(self) -> None:
        """Read the removed releases' old IDs (at startup: requests look them up)."""
        songs = await self.store.fetchall("SELECT song_id, release_ref FROM removed_songs")
        albums = await self.store.fetchall(
            "SELECT album_id, ref FROM removed_releases WHERE owned_album_id IS NULL"
        )
        self._removed = {str(r["song_id"]): str(r["release_ref"]) for r in songs}
        self._removed.update({str(r["album_id"]): str(r["ref"]) for r in albums})
        self._removed_albums = {str(r["album_id"]) for r in albums}
        self._removed_refs = set(self._removed.values())
        self._removed_refs |= {
            str(r["ref"]) for r in await self.store.fetchall("SELECT ref FROM removed_releases")
        }

    def removed_release(self, item_id: str) -> str | None:
        """The release taken out that ``item_id`` (a song's, or a catalog album's ID) was
        of - None also before ``load_removed``."""
        return None if self._removed is None else self._removed.get(item_id)

    def removing(self, item_id: str) -> str | None:
        """The release being taken out now that ``item_id`` is of (its lock is held until
        the removal ends)."""
        return self._removing.get(item_id)

    def removing_song(self, song_id: str) -> str | None:
        """As ``removing``, for one of its songs (not its album)."""
        return None if song_id in self._removing_albums else self._removing.get(song_id)

    def removed_album(self, album_id: str) -> str | None:
        """The catalog album taken out whose album ID ``album_id`` was."""
        return self.removed_release(album_id) if album_id in self._removed_albums else None

    def removed_song(self, song_id: str) -> str | None:
        """The release taken out that the song ``song_id`` was of."""
        return None if song_id in self._removed_albums else self.removed_release(song_id)

    async def removed_record(self, ref: str) -> Any:
        return await self.store.fetchone("SELECT * FROM removed_releases WHERE ref = ?", [ref])

    async def _record_rows(self, ref: str) -> list[dict[str, Any]]:
        """A removed release's placeholder rows as they were (by disc and track)."""
        stored = await self.removed_record(ref)
        if stored is None:
            return []
        rows: list[dict[str, Any]] = json.loads(stored["record"])["placeholders"]
        return rows

    async def still_removed(self, item_id: str) -> str | None:
        """The release taken out that the old ID ``item_id`` is of, while Navidrome has the
        item only as a missing file (its views, covers and streams come from the
        record). When the library has it present again - the owner's own copy of the
        album, whose same tags give it the same IDs (measured) - the record is dropped:
        those IDs are the owner's songs now, Navidrome's to answer. A missing item is
        believed for a minute."""
        ref = self.removed_release(item_id)
        if ref is None:
            return None
        seen = self._missing.get(item_id)
        if seen is not None and time.monotonic() - seen < MISSING_SECONDS:
            return ref
        stored = await self.removed_record(ref)
        if stored is None:
            return None
        inside = str(json.loads(stored["record"])["release"]["folder"]).rstrip("/") + "/"
        try:
            if item_id in self._removed_albums:
                taken = await self._album_taken(item_id, inside)
            else:
                song = await self.navidrome.song(item_id)
                # (the release's own file, a restore under way, is not the owner's)
                taken = (
                    song is not None
                    and not song.get("missing")
                    and not str(song.get("path") or "").startswith(inside)
                )
        except Exception as exc:  # Navidrome cannot be asked now: as it was (asked again)
            log.info("an old ID of %s: Navidrome not asked (%s)", ref, type(exc).__name__)
            return ref
        if taken:
            async with self.lock_for(ref):  # never under a restore of it
                stored = await self.removed_record(ref)
                if stored is None:
                    return None  # added back meanwhile
                if stored["restoring_at"] is not None:
                    return ref  # a restore under way decides
                await self._forget_removed(ref)
            log.info("the record of %s (taken out) was dropped: its IDs are the library's own"
                     " songs now", ref)  # fmt: skip
            return None
        if len(self._missing) > 10000:
            self._missing.clear()
        self._missing[item_id] = time.monotonic()
        return ref

    async def removed_songs(self, ref: str) -> list[str]:
        """A removed release's old song IDs, in its order (its views)."""
        return [str(r["song_id"]) for r in await self._record_rows(ref)]

    async def removed_row(self, song_id: str) -> dict[str, Any] | None:
        """An old song ID's placeholder row as the release's record keeps it (a plain
        stream plays it from the add-ons), or None."""
        ref = self.removed_song(song_id)
        if ref is None:
            return None
        return next((r for r in await self._record_rows(ref) if str(r["song_id"]) == song_id), None)

    def restore_failed(self, ref: str) -> None:
        """Adding ``ref`` back did not work now: not tried again for a while."""
        self._restore_failed[ref] = time.monotonic()

    def restore_due(self, ref: str) -> bool:
        """Whether adding ``ref`` back may be tried now (not just after a failure)."""
        failed = self._restore_failed.get(ref)
        return failed is None or time.monotonic() - failed > RETRY_RESTORE

    async def restore_release(
        self, ref: str, *, cover: bytes | None = None, also: Statements | None = None
    ) -> str | None:
        """Add a release taken out back from its record (a use of an old ID): its
        placeholders at the same paths with the same tags, so with the same song IDs, and a
        catalog album's cover (``cover``). Returns the album ID, or None when there is no
        record (added back already). Raises :class:`MaterializeError`."""
        await self._writable()
        async with self.lock_for(ref):
            await self._writable()  # again under the lock: a record that no longer fits is
            await self._settle_left(ref)  # dropped with the files a stop left of it
            return await self._restore_held(ref, cover, also=also)

    async def _restore_held(
        self,
        ref: str,
        cover: bytes | None,
        *,
        added_as: tuple[str | None] | None = None,
        owned_now: set[CatalogRef] | None = None,
        also: Statements | None = None,
        keep_ids: bool = False,
        anew: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None:
        """``restore_release`` under the release's lock. ``added_as`` (a commit or a fill of
        the release: the owned album it fills, or None): the record is used only if it was
        that - a catalog album, or a fill of that album -, else it is dropped (the release
        is added anew); so too when a track it had a placeholder for is owned now
        (``owned_now``: the tracks the album owns), and when a song of the album sits where
        one of its placeholders was (the album was renumbered or gained a song meanwhile).
        A record is dropped only where the release's tracks can be added anew instead:
        ``anew`` raises when they do not fit the album (``_fits_owned``), and the record
        then stays, with its old IDs. (The album is looked at again when they are added:
        one that changes between the two is refused there, the record gone.)"""
        stored = await self.removed_record(ref)
        if stored is None:
            return None
        recorded = str(stored["owned_album_id"] or "") or None
        record = json.loads(stored["record"])
        release, rows = record["release"], record["placeholders"]
        owned = {str(t) for t in owned_now or ()}
        if (added_as is not None and (added_as[0] or None) != recorded) or any(
            str(r["track_ref"]) in owned for r in rows
        ):
            if anew is not None:
                await anew()
            await self._drop_record(ref, stored, keep_ids=keep_ids)
            log.info("the record of %s (taken out) was dropped: it is added anew", ref)
            return None
        folder = str(release["folder"])
        planned = [
            _Planned(_row_track(r), str(r["placeholder_path"]), json.loads(r["tags"])) for r in rows
        ]
        before = await self._album_state(recorded)
        if recorded and not before:
            self._restore_failed[ref] = time.monotonic()
            raise MaterializeError("the owned album it filled is not in the library")
        if recorded and added_as is not None:
            taken = {
                (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0))
                for s in await self.navidrome.songs_of_album(recorded)
                if not s.get("missing")
            }
            if any((int(r["disc"]), int(r["track"])) in taken for r in rows):
                if anew is not None:
                    await anew()
                await self._drop_record(ref, stored, keep_ids=keep_ids)
                log.info(
                    "the record of %s (taken out) was dropped: a song of the album is where"
                    " one of its placeholders was",
                    ref,
                )
                return None
        if not recorded and await self._album_taken(str(stored["album_id"]), folder):
            # Its album ID is another album's now (the owner has the album itself, say).
            await self._drop_record(ref, stored, keep_ids=keep_ids)
            log.info("the record of %s (taken out) was dropped: its album is in the library", ref)
            return None
        write_cover = cover is not None and recorded is None
        # Its songs are intercepted until their rows are back, also under other IDs;
        # a stop is put right by its mark (``repair_pending``, ``repair_removals``).
        paths = frozenset(p.relative for p in planned)
        try:  # (as recorded - by this build, inside the folder)
            for path in (folder, *paths):
                self.layout.contained(path)
        except LayoutError as exc:
            raise MaterializeError(f"{exc}: nothing written") from None
        await self._after_listings()
        pending = self._pending.get(ref)
        if pending is None:  # else: left by a stop (the same entry: its waiters go on waiting)
            pending = self._pending[ref] = _Pending(
                ref, folder, paths, write_cover, True, running=True, restoring=True
            )
        was_left = not pending.running
        pending.running = True
        # Asked again here, after the waits (the release's lock, the archives being listed),
        # right before anything is written; registered first: a listing waits for this.
        try:
            await self._writable()
        except BaseException:  # refused, or canceled while asking: nothing was written
            if was_left:
                pending.running = False  # as a stop left it: taken out later
            else:
                self._ended(pending)
            raise
        moved: list[bool] = []  # (set once the first file moves into the library)
        try:
            await self.store.execute(
                "UPDATE removed_releases SET restoring_at = ? WHERE ref = ?", [self.clock(), ref]
            )
            await self._write(
                planned, folder, cover if write_cover else None, moving=lambda: moved.append(True)
            )
            await self._at("restored in place")
            album_id = await self._verify_new(planned, folder, recorded, before, None)
            present = {
                s["path"]: str(s["id"])
                for s in await self.navidrome.songs_under(folder)
                if not s.get("missing")
            }
            ids = {str(r["song_id"]): present[str(r["placeholder_path"])] for r in rows}
            async with self.store.transaction() as conn:
                await self._restore_rows(conn, record, album_id, ids)
                if also is not None:
                    await also(conn)
            self._ended(pending)
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                pending.running = False  # left, until its files are known to be gone
                if await self.removed_record(ref) is None:  # its transaction was done
                    self._ended(pending)
                    await self._forget_removed(ref, in_database=False)
                    raise
                if moved or was_left:
                    # (an add-back a stop left half done has its files in the library
                    # whether this one moved any or not: they go, and a scan confirms it -
                    # unless the library may not be written now: then it stays as the
                    # stop left it, for the repairs)
                    try:
                        # (the cover too when a catalog album's: the attempt a stop
                        # left may have written one, whatever this one was given)
                        cover_too = write_cover or (was_left and recorded is None)
                        gone = bool(moved or not await self.refused()) and await self._roll_back(
                            paths, folder, cover_too, removed_folder=True, bound=_bound(exc)
                        )
                    except OSError as failure:  # a file that cannot go: as not confirmed
                        log.error("%s: %s", ref, failure.strerror or type(failure).__name__)
                        gone = False
                else:  # nothing reached the library: nothing to take out, nothing to scan
                    gone = True
                if gone:
                    await self.store.execute(
                        "UPDATE removed_releases SET restoring_at = NULL WHERE ref = ?", [ref]
                    )
                    self._ended(pending)
                # (else its mark and its entry stay: taken out again a minute later)
            self._restore_failed[ref] = time.monotonic()
            if isinstance(exc, MaterializeError):
                raise MaterializeError(exc.reason) from None
            if isinstance(exc, Exception):
                log.warning("adding %s back failed: %s", ref, type(exc).__name__)
                raise MaterializeError(f"unexpected {type(exc).__name__}") from exc
            raise
        self._restore_failed.pop(ref, None)
        await self._forget_removed(ref, in_database=False)
        changed = sum(1 for old, new in ids.items() if old != new)
        if changed:
            log.warning("%s came back with %d other song ID(s)", ref, changed)
        log.info("added back %d placeholder(s) of %s - %s (%s)", len(rows), release["artist"],
                 release["title"], ref)  # fmt: skip
        return album_id

    async def _album_taken(self, album_id: str, folder: str) -> bool:
        """Whether Navidrome's album ``album_id`` has songs present outside ``folder`` (the
        release's own files there - a restore a stop interrupted - do not count)."""
        songs = await self.navidrome.songs_of_album(album_id)
        inside = folder.rstrip("/") + "/"
        return any(not s.get("missing") and not str(s["path"]).startswith(inside) for s in songs)

    async def _restore_rows(
        self, conn: aiosqlite.Connection, record: dict[str, Any], album_id: str, ids: dict[str, str]
    ) -> None:
        """A restored release's rows, as they were but for the time it was added (now) and
        its use (none: only unused releases are taken out)."""
        release, now = record["release"], self.clock()
        await conn.execute(
            "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
            " album_tags, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [release["ref"], release["folder"], album_id, release["owned_album_id"],
             release["title"], release["artist"], release["album_tags"], release["data"], now],
        )  # fmt: skip
        for link in record["links"]:
            song = ids.get(str(link["song_id"]), str(link["song_id"]))
            await conn.execute(
                "INSERT OR REPLACE INTO track_links (track_ref, song_id, release_ref, owned)"
                " VALUES (?, ?, ?, ?)",
                [link["track_ref"], song, release["ref"], link["owned"]],
            )
        for row in record["placeholders"]:
            await conn.execute(
                "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref,"
                " release_ref, isrc, title, artist, album, duration_ms, disc, track, tags,"
                " backing_song_id, created_at) VALUES"
                " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [ids[str(row["song_id"])], row["placeholder_path"], row["placeholder_path"],
                 row["track_ref"], release["ref"], row["isrc"], row["title"], row["artist"],
                 row["album"], row["duration_ms"], row["disc"], row["track"], row["tags"],
                 row["backing_song_id"], now],
            )  # fmt: skip
        await conn.execute("DELETE FROM removed_releases WHERE ref = ?", [release["ref"]])

    async def _forget_removed(
        self, ref: str, *, in_database: bool = True, in_memory: bool = True
    ) -> None:
        if in_database:
            await self.store.execute("DELETE FROM removed_releases WHERE ref = ?", [ref])
        if not in_memory:
            return
        self._removed_refs.discard(ref)
        if self._removed is not None:
            self._removed = {k: v for k, v in self._removed.items() if v != ref}
            self._removed_albums &= set(self._removed)

    async def retag(self, song_id: str, track: CatalogTrack, release: CatalogRelease) -> bool:
        """Rewrite a placeholder in place from a fresher catalog track (the refresh):
        its track tags - never the album's, which keep it in its album - and its silence
        when the length changed. The same path keeps the song ID (a targeted scan checks
        it; otherwise the old file is put back and :class:`ReplaceError` raised). Returns
        whether anything changed. Delivered audio is left as it is (``ReplaceError``)."""
        async with self.lock_for(f"song:{song_id}"):
            await self._recover(song_id)  # a swap of it a stop interrupted, first
            row = await self._placeholder(song_id)
            if row["state"] != "placeholder":
                raise ReplaceError("delivered audio is kept as it is")
            stored = await self.store.fetchone(
                "SELECT album_tags FROM releases WHERE ref = ?", [row["release_ref"]]
            )
            if stored is None:
                raise ReplaceError("the release is not recorded")
            comments = tagging.placeholder_tags(json.loads(stored["album_tags"]), track, release)
            if comments == json.loads(row["tags"]) and track.duration_ms == row["duration_ms"]:
                return False
            await self._swappable()
            path: str = row["placeholder_path"]
            try:
                before = await self.navidrome.song(song_id)
            except NavidromeError as exc:
                raise ReplaceError(f"Navidrome could not be asked ({exc})") from None
            album_id = before.get("albumId") if before else None
            target = self._inside(path)
            try:
                self.layout.ensure()
                await self._reconcile_placeholder(row, json.loads(row["tags"]))
                staged = await self._silent_file(
                    track.duration_ms, comments, not_size=target.stat().st_size
                )
            except (OSError, ValueError) as exc:
                raise ReplaceError(f"cannot write the placeholder ({type(exc).__name__})") from None
            backup = self._backup_path(song_id, ".flac")

            def swap() -> None:
                os.replace(target, backup)
                os.replace(staged, target)
                durable.sync_dirs(target.parent, self.layout.staging)

            await self._swap(
                song_id,
                "retag",
                swap=swap,
                at=path,
                album_id=album_id,
                staged=staged,
                backup=backup,
                update=(
                    "UPDATE placeholders SET title = ?, artist = ?, isrc = ?, disc = ?,"
                    " track = ?, duration_ms = ?, tags = ? WHERE song_id = ?",
                    [
                        track.title,
                        track.artist,
                        track.isrc,
                        track.disc,
                        track.number,
                        track.duration_ms,
                        json.dumps(comments),
                        song_id,
                    ],
                ),
                done=lambda r: (
                    json.loads(r["tags"]) == comments and r["duration_ms"] == track.duration_ms
                ),
                unconfirmed="the song ID or album changed; the placeholder was restored",
                failed="retagging failed ({}); the placeholder was restored",
            )
            return True

    async def set_backing(self, song_id: str, owned_song_id: str | None) -> None:
        """Back a placeholder with an owned recording of the same track (or clear it)."""
        await self._placeholder(song_id)
        await self.store.execute(
            "UPDATE placeholders SET backing_song_id = ? WHERE song_id = ?",
            [owned_song_id, song_id],
        )

    # --- delivered audio in place of a placeholder ---------------------------------

    async def replace_with_delivered(self, song_id: str, delivered: Path) -> str:
        """Put delivered audio where the placeholder is, keeping the delivered format.
        Returns the new library-relative path. On any failure before the row says so, the
        placeholder is restored and :class:`ReplaceError` raised."""
        async with self.lock_for(f"song:{song_id}"):
            await self._recover(song_id)  # a swap of it a stop interrupted, first
            row = await self._placeholder(song_id)
            if row["state"] != "placeholder":
                raise ReplaceError("already replaced")
            await self._swappable()
            comments: dict[str, list[str]] = json.loads(row["tags"])
            old: str = row["placeholder_path"]
            suffix = delivered.suffix.lower()
            new = str(Path(old).with_suffix(suffix)) if suffix != ".flac" else old
            self.layout.ensure()
            await self._reconcile_placeholder(row, comments)
            staged = self.layout.staging / f"{uuid.uuid4().hex}{suffix}"
            backup = self._backup_path(song_id, ".flac")
            source, target = self._inside(old), self._inside(new)
            await anyio.to_thread.run_sync(shutil.copyfile, delivered, staged)
            try:
                size = source.stat().st_size if new == old else None
                await anyio.to_thread.run_sync(
                    lambda: tagging.write_delivered(staged, comments, not_size=size)
                )
                await anyio.to_thread.run_sync(durable.sync_file, staged)
            except Exception as exc:
                staged.unlink(missing_ok=True)
                raise ReplaceError(
                    f"cannot tag the delivered file ({type(exc).__name__})"
                ) from None

            def swap() -> None:
                os.replace(source, backup)
                os.replace(staged, target)
                durable.sync_dirs(target.parent, self.layout.staging)

            await self._swap(
                song_id,
                "replacement",
                swap=swap,
                at=new,
                staged=staged,
                backup=backup,
                update=(
                    "UPDATE placeholders SET state = 'delivered', path = ?, delivered_at = ?"
                    " WHERE song_id = ?",
                    [new, self.clock(), song_id],
                ),
                done=lambda r: r["state"] == "delivered" and r["path"] == new,
                unconfirmed="the song ID changed; the placeholder was restored",
                failed="replacement failed ({}); the placeholder was restored",
            )
            return new

    async def revert_to_placeholder(
        self, song_id: str, *, unused_since: float | None = None, only_if_missing: bool = False
    ) -> bool:
        """Put the silent placeholder back in place of delivered audio (cache expiry).
        ``unused_since``: only if the audio was not used after then (checked under the
        song's lock, so a use meanwhile keeps it); ``only_if_missing``: only if the delivered
        file is gone (checked there too). Returns whether it was reverted."""
        if (await self._placeholder(song_id))["state"] != "delivered":
            return False  # (looked at again under the lock)
        # The silent file comes into the library while the row still says "delivered": as a
        # write of new placeholders, it waits for the archives being listed, and an archive
        # waits for it. Before the song's lock, never under it: an archive that puts
        # an interrupted swap of the song right waits for that lock while it is listing.
        try:
            await self._after_listings()
        except MaterializeError as exc:
            raise ReplaceError(exc.reason) from None
        # (An entry of its own: another revert of the song, waiting behind this one, never
        # takes this one's out of sight when it ends first.)
        key = f"revert:{uuid.uuid4().hex}"
        reverting = self._pending[key] = _Pending(key, "", frozenset(), False, False, True)
        try:
            async with self.lock_for(f"song:{song_id}"):
                row = await self._placeholder(song_id)
                if row["state"] != "delivered":
                    return False
                if unused_since is not None and float(row["last_used_at"] or 0) > unused_since:
                    return False
                # A swap of it a stop interrupted, first: its backup may hold the delivered
                # audio (never taken for missing, never overwritten by this swap's backup).
                if await self._recover(song_id):
                    row = await self._placeholder(song_id)
                present = self.layout.absolute(row["path"]).exists()
                if only_if_missing and present:
                    return False
                self.layout.ensure()
                return await self._revert(row, present)
        finally:
            self._ended(reverting)

    async def _revert(self, row: Any, present: bool) -> bool:
        """``revert_to_placeholder`` once it is decided (under the song's lock)."""
        song_id = str(row["song_id"])
        comments: dict[str, list[str]] = json.loads(row["tags"])
        current: str = row["path"]
        original: str = row["placeholder_path"]
        source, target = self._inside(current), self._inside(original)
        await self._swappable()
        if not present:
            # The delivered file is gone: the row says "placeholder" first - from then on
            # its plays come from the add-ons, never from a silent file under a delivered
            # row -, then its silent file is put in place as after an interrupted swap:
            # from a backup written before the row changed, which a stop leaves for the
            # next start to finish by.
            silent = await self._silent_file(row["duration_ms"], comments)
            # Asked once more now that the silent file is made (that took a moment):
            # Navidrome may have been restarted with another setting meanwhile. Refused,
            # nothing has changed: the row says "delivered", nothing is staged or scanned.
            await self._still_swappable(silent)
            backup = self._backup_path(song_id, ".flac")

            def keep() -> None:
                os.replace(silent, backup)
                durable.sync_dir(self.layout.staging)

            await anyio.to_thread.run_sync(keep)
            await self._at("revert of a missing file prepared")
            with anyio.CancelScope(shield=True):  # a canceled wait would still update it
                await self._mark_placeholder(song_id)
            await self._at("revert of a missing file committed")
            # (Gated as any repair: refused there, the backup stays for the next one - the
            # row says "placeholder" already - and nothing is moved or scanned.)
            await self._recover(song_id)
            return True
        if source.is_symlink():
            # A link someone put there is not Shijhon's file: never moved, never followed
            # (a swap undone would have to put its audio back through it).
            raise ReplaceError("the delivered file is a link: left as it is")
        size = source.stat().st_size if original == current else None
        staged = await self._silent_file(row["duration_ms"], comments, not_size=size)
        # A name of its own: "backup-<id>.flac" is always the silent placeholder.
        backup = self._backup_path(song_id, ".delivered" + Path(current).suffix)

        def swap() -> None:
            os.replace(source, backup)
            os.replace(staged, target)
            durable.sync_dirs(target.parent, self.layout.staging)

        await self._swap(
            song_id,
            "revert",
            swap=swap,
            at=original,
            staged=staged,
            backup=backup,
            update=(_MARK_PLACEHOLDER, [song_id]),
            done=lambda r: r["state"] == "placeholder",
            unconfirmed="the song ID changed on revert; the delivered file was restored",
            failed="revert failed ({}); the delivered file was restored",
        )
        return True

    async def _swap(
        self,
        song_id: str,
        what: str,
        *,
        swap: Callable[[], None],
        at: str,
        staged: Path,
        backup: Path,
        update: tuple[str, list[Any]],
        done: Callable[[Any], bool],
        unconfirmed: str,
        failed: str,
        album_id: str | None = None,
    ) -> None:
        """Swap a placeholder's file, under the song's lock: ``swap`` puts the new file in
        place (the old one in ``backup``), a targeted scan waits until Navidrome lists the
        song at ``at`` with the new file's size (and in ``album_id``) - a scan Navidrome
        skipped does not count -, then the row's ``update``: the commit point. Before
        it, any failure or cancel puts the file the row names back (``_recover``, as the
        next start would) and raises :class:`ReplaceError`; after it the new file stays
        whatever happens. The backup goes last, once nothing depends on it - or stays for
        the next start (also when a failed update leaves it unknown what the row says)."""
        # Asked once more right before the files move (the staged file took a moment to
        # make, the song's lock and the archives before it perhaps much longer): Navidrome
        # may have been restarted with another setting meanwhile.
        await self._still_swappable(staged)  # (refused: nothing was moved)
        committed: bool | None = False
        try:
            await anyio.to_thread.run_sync(swap)
            size = (await anyio.Path(self.layout.absolute(at)).stat()).st_size
            await self._at(f"{what} swapped")
            if not await self._song_at(song_id, at, album_id, size=size):
                raise _Unconfirmed(unconfirmed)
            await self._at(f"{what} verified")
            with anyio.CancelScope(shield=True):  # a canceled wait would still update it
                await self.store.execute(*update)
            committed = True
            await self._at(f"{what} committed")
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                if committed is False and not isinstance(exc, _Unconfirmed):
                    committed = await self._row_says(song_id, done)
                if committed is False:
                    await anyio.Path(staged).unlink(missing_ok=True)
                    try:
                        await self._recover(song_id, bound=_bound(exc), gated=False)
                    except Exception as failure:
                        log.error("the %s of %s is not undone yet (%s): put right at the next"
                                  " start", what, song_id, failure)  # fmt: skip
                elif committed:
                    await self._drop(backup)
                else:
                    log.error(
                        "the %s of %s may or may not be recorded: its files stay as they are"
                        " until the next start puts them right",
                        what,
                        song_id,
                    )
            if committed is True or not isinstance(exc, Exception):
                raise
            if isinstance(exc, _Unconfirmed):
                raise ReplaceError(exc.reason) from None
            if committed is None:
                raise ReplaceError(
                    f"{what} failed ({type(exc).__name__}): not known whether it is recorded;"
                    " put right at the next start"
                ) from None
            raise ReplaceError(failed.format(type(exc).__name__)) from None
        await self._drop(backup)

    async def _row_says(self, song_id: str, done: Callable[[Any], bool]) -> bool | None:
        """Whether the placeholder's row shows a swap's update (None: it cannot be read)."""
        try:
            row = await self.store.fetchone(
                "SELECT * FROM placeholders WHERE song_id = ?", [song_id]
            )
        except Exception as exc:
            log.error("the row of %s not read after a failed swap: %s", song_id,
                      type(exc).__name__)  # fmt: skip
            return None
        return row is not None and done(row)

    async def _drop(self, backup: Path) -> None:
        """A committed swap's backup goes (one that cannot stays for the startup's repair,
        which keeps the file the row names)."""
        try:
            await anyio.to_thread.run_sync(lambda: backup.unlink(missing_ok=True))
        except OSError as exc:
            log.warning("backup %s not removed (%s): the next start removes it", backup.name,
                        exc.strerror or type(exc).__name__)  # fmt: skip

    def interrupted(self) -> set[str]:
        """The songs with a swap of their file not finished: a backup in the staging folder."""
        staging = self.layout.staging
        backups = staging.glob("backup-*") if staging.exists() else []
        return {b.name.removeprefix("backup-").split(".", 1)[0] for b in backups}

    async def marked(self) -> bool:
        """Whether a release is marked as being taken out (a removal under way, or one a
        stop - or a put-back not confirmed - left)."""
        row = await self.store.fetchone(
            "SELECT 1 FROM releases WHERE removing_at IS NOT NULL LIMIT 1"
        )
        return row is not None

    async def settle_song(self, song_id: str) -> bool:
        """For a caller that holds the song's lock, before it trusts the song's row: a swap
        of its file that a stop interrupted is put right first (its row can say "delivered"
        while the silent file is still in place). The file is put in place at once;
        Navidrome's confirmation is waited for a few seconds only (a request waits here),
        the minute's repair finishes it. False when the file could not be put in place -
        the library may not be written now, or it failed."""
        try:
            await self._recover(song_id, bound=COMPENSATION_SECONDS)
        except NotConfirmed as exc:
            log.warning("an interrupted swap of %s: %s", song_id, exc)
        except Exception as exc:
            log.warning("an interrupted swap of %s is not put right: %s", song_id, exc)
            return False
        return True

    async def recover_swap(self, song_id: str) -> int:
        """Put right a swap of a placeholder's file that a stop interrupted (its backups are
        still in the staging folder); see ``_recover``. Returns 1 when a file was put back."""
        async with self.lock_for(f"song:{song_id}"):
            return await self._recover(song_id)

    async def _recover(
        self, song_id: str, *, bound: float | None = None, gated: bool = True
    ) -> int:
        """Under the song's lock: while backups of its file are in the staging folder - a
        swap that did not finish, here or before a stop - the file the row names ends up in
        place: the silent placeholder with the row's tags and length, or the delivered
        audio. It is kept where it is when it is there (a swap that got as far as its row's
        update); else copied back from its backup; else, for a placeholder, written anew
        from its row. Then the same song in another format beside it goes, a targeted scan
        confirms the song at its path with that file's size (waited for ``bound`` seconds
        at most), and only then the backups go: until Navidrome has confirmed it they are
        what the next start (and the next swap of the song) finds and finishes this by.
        Returns 1 when a file was put back, else 0; raises :class:`ReplaceError` when it
        could not be confirmed (or, ``gated``, the library may not be written now: asked
        before anything is read, and again once a silent file had to be made - refused, the
        backups stay as they are and nothing is moved or scanned; a swap's own compensation
        is not gated: its files have moved already; or a "delivered" row has its silent
        placeholder in place, its audio in no backup and the song in another format beside
        it: nothing is changed, the backups stay). A link - at the file's place or among
        the backups - is never the row's file: never read, written or touched through (it
        may lead to a file of the owner's outside the placeholder folder); the row's file
        replaces one in its place, and one among the backups goes with them."""
        backups = sorted(self.layout.staging.glob(f"backup-{glob.escape(song_id)}.*"))
        if not backups:
            return 0
        row = await self.store.fetchone("SELECT * FROM placeholders WHERE song_id = ?", [song_id])
        if row is None:  # no record: nothing to put back
            for backup in backups:
                backup.unlink(missing_ok=True)
            return 0
        if gated:
            await self._swappable()
        silent = row["state"] == "placeholder"
        target = self._inside(row["placeholder_path"] if silent else row["path"])
        linked = target.is_symlink()
        in_place = not linked and target.exists() and _names_row(target, row)
        files = [b for b in backups if not b.is_symlink()]  # (a link is never read)
        named = [b for b in files if _names_row(b, row)]
        source = None if in_place or not named else named[0]
        fresh = None
        if not in_place and source is None:
            if not silent:
                # Delivered audio that is neither in place nor in a backup: nothing to put
                # back (a file that is gone is the expiry's to see to). Only backups read
                # and found silent go (and links): one that cannot be read now may be the
                # audio.
                unread = [b for b in files if tagging.marker_of(b) is None]
                original = self._inside(row["placeholder_path"])
                as_placeholder = {**dict(row), "state": "placeholder"}
                if (
                    not unread
                    and not original.is_symlink()
                    and original.exists()
                    and _names_row(original, as_placeholder)
                ):
                    # Its silent placeholder is in place (a revert got that far) and its
                    # audio is not to be had: the row follows the file - a "delivered" row
                    # over silence would have its plays forwarded to the silence. Then as
                    # for any placeholder: confirmed by a scan before the backups go.
                    if _other_suffixes(original):
                        # Not with the song in another format beside it: that may be its
                        # audio, and a placeholder's repair would take it away. Nor is the
                        # song settled then: its backups stay (what every later repair
                        # finds it by) and this fails, so no play of the "delivered" row is
                        # forwarded to the silence. With the other file moved away, the
                        # next repair makes the song a placeholder again.
                        raise ReplaceError(
                            "its silent placeholder is in place with the song in another"
                            " format beside it: both left as they are, and the song is not"
                            " played until the other file is moved away"
                        )
                    with anyio.CancelScope(shield=True):
                        await self._mark_placeholder(song_id)
                    log.warning("the delivered audio of %s is in none of its backups: its"
                                " placeholder, in place, is its file again", song_id)  # fmt: skip
                    return await self._recover(song_id, bound=bound, gated=gated)
                for backup in backups:
                    if backup not in unread:
                        backup.unlink(missing_ok=True)
                if unread:
                    raise ReplaceError("a backup of its delivered audio cannot be read")
                if linked:
                    log.warning("the delivered file of %s is a link: left as it is", song_id)
                else:
                    log.error("the delivered audio of %s is in none of its backups", song_id)
                return 0
            fresh = await self._silent_file(row["duration_ms"], json.loads(row["tags"]))
            if gated:  # asked once more now that the silent file is made, before it moves
                await self._still_swappable(fresh)

        def place() -> None:
            if source is not None:
                _copy_in_place(source, target)  # the backup stays until Navidrome has it
            elif fresh is not None:
                os.replace(fresh, target)
            elif named:
                # Put back before, not confirmed yet: as modified now once more (a stop
                # between its rename and its new time would leave Navidrome never
                # reading it again).
                os.utime(target)
                durable.sync_file(target)
            for stray in _other_suffixes(target):
                stray.unlink(missing_ok=True)  # the other kind, another suffix
            durable.sync_dirs(target.parent, self.layout.staging)

        await anyio.to_thread.run_sync(place)
        await self._at("recovery placed")
        relative, size = self.layout.relative(target), target.stat().st_size
        confirmed = False
        try:
            with anyio.move_on_after(bound):
                confirmed = await self._song_at(song_id, relative, size=size)
        except Exception as exc:  # the file is in place: the confirmation is tried again
            raise NotConfirmed(f"its scan failed ({type(exc).__name__})") from None
        if not confirmed:
            raise NotConfirmed("the song is not confirmed back at its path")

        def clear() -> None:
            for backup in backups:
                backup.unlink(missing_ok=True)
            durable.sync_dir(self.layout.staging)

        await anyio.to_thread.run_sync(clear)
        if not in_place:
            log.info("put right an interrupted swap of %s", song_id)
        return int(not in_place)

    def _backup_path(self, song_id: str, suffix: str) -> Path:
        # Deterministic, so an interrupted swap can be found again (see _reconcile_placeholder).
        return self.layout.staging / f"backup-{song_id}{suffix}"

    async def _silent_file(
        self, duration_ms: int, comments: dict[str, list[str]], *, not_size: int | None = None
    ) -> Path:
        """A tagged silent file in the staging folder, on disk; ``not_size``: never of
        that size (the file it replaces at the same path: a scan that read it tells)."""
        staged = self.layout.staging / f"{uuid.uuid4().hex}.flac"
        await self.silence.write(duration_ms, staged)
        await anyio.to_thread.run_sync(
            lambda: tagging.write_flac(staged, comments, not_size=not_size)
        )
        await anyio.to_thread.run_sync(durable.sync_file, staged)
        return staged

    async def _reconcile_placeholder(self, row: Any, comments: dict[str, list[str]]) -> None:
        """Make sure the silent file exists where the database says it is (a swap a stop
        interrupted is put right before, ``_recover``): one that is missing is written anew
        from its row."""
        original = self._inside(row["placeholder_path"])
        if original.exists():
            return
        silent = await self._silent_file(row["duration_ms"], comments)
        await self._still_swappable(silent)  # (making it took a moment: asked once more)
        for stray in _other_suffixes(original):
            stray.unlink(missing_ok=True)
        os.replace(silent, original)
        durable.sync_dirs(original.parent, self.layout.staging)
        log.warning("restored the missing placeholder file of %s", row["song_id"])

    async def _mark_placeholder(self, song_id: str) -> None:
        await self.store.execute(_MARK_PLACEHOLDER, [song_id])

    async def _placeholder(self, song_id: str) -> Any:
        row = await self.store.fetchone("SELECT * FROM placeholders WHERE song_id = ?", [song_id])
        if row is None:
            raise ReplaceError("not a placeholder")
        return row

    async def _song_at(
        self, song_id: str, relative: str, album_id: str | None = None, *, size: int | None = None
    ) -> bool:
        """Targeted scan of the file's folder until the song ID is at ``relative`` (and in
        ``album_id``, when given) - with the file's ``size``, when given: only a scan that
        read the file there counts (one Navidrome skipped leaves the old file's record,
        which the same path and song ID alone would pass)."""
        folder = relative.rsplit("/", 1)[0]

        async def settled() -> bool:
            song = await self.navidrome.song(song_id)
            return (
                song is not None
                and not song.get("missing")
                and song["path"] == relative
                and (album_id is None or song.get("albumId") == album_id)
                and (size is None or song.get("size") == size)
            )

        return await self.scans.until([folder], settled)


class NotConfirmed(ReplaceError):
    """The file a row names was put back in place, but Navidrome did not confirm it (its
    backup stays: the next start, or the song's next swap, finishes it)."""


class _Unconfirmed(Exception):
    """A swap's targeted scan did not show the song at its new file."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _InUse(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


def used_here(rows: list[Any]) -> list[str]:
    """Uses Shijhon saw itself (its placeholders' rows)."""
    reasons = []
    if any(r["state"] != "placeholder" for r in rows):
        reasons.append("delivered audio in place")
    if any(r["delivered_at"] is not None or r["last_used_at"] is not None for r in rows):
        reasons.append("streamed or downloaded")
    return reasons


async def _used_meanwhile(conn: aiosqlite.Connection, ref: str) -> list[str]:
    """A use Shijhon recorded since the last check (in the removal's transaction): a
    stream or download it served, a use a check saw in Navidrome's records."""
    async with conn.execute(
        "SELECT 1 FROM placeholders WHERE release_ref = ?"
        " AND (last_used_at IS NOT NULL OR state != 'placeholder') LIMIT 1",
        [ref],
    ) as cursor:
        if await cursor.fetchone() is not None:
            return ["streamed or downloaded"]
    async with conn.execute(
        "SELECT reason FROM seen_uses WHERE release_ref = ? ORDER BY seen_at, reason", [ref]
    ) as cursor:
        return used_before(str(row[0]) for row in await cursor.fetchall())


# The delivery stays a use (the cleanup never takes a used release out).
_MARK_PLACEHOLDER = (
    "UPDATE placeholders SET state = 'placeholder', path = placeholder_path,"
    " last_used_at = COALESCE(last_used_at, delivered_at), delivered_at = NULL"
    " WHERE song_id = ?"
)


def _bound(exc: BaseException) -> float | None:
    """How long a compensation for ``exc`` waits for Navidrome's confirmation: without a
    bound after a failure, briefly after a cancellation (a shutdown must not hang on a scan;
    the files are put right either way)."""
    return COMPENSATION_SECONDS if isinstance(exc, anyio.get_cancelled_exc_class()) else None


def _other_suffixes(path: Path) -> list[Path]:
    """The audio files beside ``path`` with its exact name and another suffix (the same
    song in another format) - never a file whose name merely starts like it ("1-01 Mr"
    and "1-01 Mr. Jones" are two songs)."""
    return [
        other
        for other in path.parent.glob(f"{glob.escape(path.stem)}.*")
        if other != path and other.stem == path.stem and other.suffix.lower() in _AUDIO_SUFFIXES
    ]


def _copy_in_place(backup: Path, target: Path) -> None:
    """A backup's file into place again, the backup itself kept (a hard link to it, else a
    copy; never through a backup that is a symbolic link) - and
    as modified now: Navidrome 0.64.2 reads a file again only when it was modified after
    the one it knows at that path, and the backup is the older file
    (``scanner/phase_1_folders.go``)."""
    if backup.is_symlink():  # never followed: a link would share the linked file's times
        raise OSError(errno.ELOOP, "a backup that is a link is not put in place", str(backup))
    placing = backup.with_name(f"placing-{uuid.uuid4().hex}{backup.suffix}")
    try:
        os.link(backup, placing)
    except OSError:  # a file system without links
        shutil.copyfile(backup, placing)
    os.utime(placing)
    durable.sync_file(placing)  # its content and its new time, before it is in place
    os.replace(placing, target)


def _names_row(path: Path, row: Any) -> bool:
    """Whether ``path`` is the file ``row`` names: delivered audio for a delivered row; for
    a placeholder the silent file with the row's tags and length - a retag's old and new
    files are both silent, so the marker alone does not tell them apart (a placeholder of
    another length to the sample, as another version wrote it, is not it either: it is
    written anew from its row)."""
    marker = tagging.marker_of(path)
    if marker is None:
        return False
    if row["state"] != "placeholder":
        return not marker.silence
    if not marker.silence:
        return False
    try:
        return tagging.read_vorbis(path) == json.loads(row["tags"]) and tagging.flac_samples(
            path
        ) == samples_for(int(row["duration_ms"]))
    except (mutagen.MutagenError, OSError, ValueError):
        return False


def _row_track(row: Any) -> CatalogTrack:
    """A placeholder's track as its row records it (what its silent file needs)."""
    return CatalogTrack(
        ref=CatalogRef.parse(str(row["track_ref"])),
        title=str(row["title"]),
        artist=str(row["artist"]),
        duration_ms=int(row["duration_ms"]),
        disc=int(row["disc"]),
        number=int(row["track"]),
        isrc=row["isrc"],
    )


def _like_prefix(folder: str) -> str:
    """A LIKE pattern's literal prefix for the files in ``folder``."""
    escaped = folder.rstrip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/"


def release_json(release: CatalogRelease) -> str:
    return json.dumps(release_data(release))
