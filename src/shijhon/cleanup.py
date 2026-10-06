"""Taking unused releases out of the library again.

**What:** whole releases only - a catalog album added to the library (a commit), or the
songs added to a partly owned album (a fill) - never single tracks. **When:** once nobody
has used it since it was added, ``unused_days`` (30) after it was added; checked daily.
**Anything used even once is kept** - also when the use ended: Navidrome's database
shows only the playlists, queues, bookmarks and shares there are now, so every check - the
daily one in any mode, a dry run - records the uses it sees of every release (``seen_uses``),
and a use recorded keeps its release from then on. What begins and ends between two checks
is not seen. By any user: a favorite, rating or play of one of
its songs (also one taken back), a playlist entry, a play queue, a bookmark, a share (of
a song, the album, its artist or a playlist holding it), delivered audio or an offline
download, a stream Shijhon served; for a catalog album also a favorite, rating or play
of the album, for a fill a favorite or rating of the album given after the fill (the
album's plays are its owned songs' too). The action that added it (a commit) is not
itself a use: what counts is what is recorded afterwards. Every user's records come from
Navidrome's own database: by the usage export a process beside Shijhon makes of it
(``[navidrome] usage_export_path``), or read from it directly (``database_path``); without
either, no cleanup (``navidrome/usage.py``). The cleanup acts only on what was read
after the moment it asks about: its listing on a snapshot taken after the check began,
each removal - once its files are gone - on one taken after that; otherwise it waits, and
says so.

``mode``: ``off`` (the default), ``dry_run`` (the daily check logs what it would take
out; ``shijhon cleanup`` lists it at any time, read only) or ``on``. Taken out, a release
is kept as a record (``PlaceholderEngine.remove_release``): **a use of an old ID adds it
back** - the actions that commit a catalog album (``USES``) carrying one of its
song IDs (or a catalog album's album ID), or a commit or fill of it, put it back at the
same paths, with the same song IDs, before the request goes on. A view of an old ID shows
the release as it was, a cover is the release's and a plain stream plays from the
add-ons, all writing nothing (``views/removed.py``, ``delivery/intercept.py``): a client
syncing its old IDs never undoes the cleanup. A fill taken out leaves its album shown
complete, filled on its next use, never automatically. Downloaded audio has its own
limits (``delivery/expiry.py``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread

from shijhon.catalog.model import release_from_data
from shijhon.navidrome.export import Snapshot
from shijhon.navidrome.usage import (
    NO_EXPORT,
    NOT_SAME,
    REFUSALS,
    REMOVAL_WAIT_SECONDS,
    Mark,
    NotFresh,
    Usage,
    UsageSource,
    WrongSource,
    current,
    not_navidrome,
    not_there,
    open_read_only,
    past,
    recorded_paths,
    seen_uses,
)
from shijhon.placeholders.engine import PlaceholderEngine, used_here
from shijhon.proxy.app import RequestContext
from shijhon.proxy.auth import CheckFailed
from shijhon.proxy.params import RestCall

if TYPE_CHECKING:
    from shijhon.fill.fills import Fills
    from shijhon.navidrome.checks import PlaceholderWrites
    from shijhon.views.commits import Commits

log = logging.getLogger(__name__)
DAY = 86400.0
PREVIEW_KEPT = 300.0  # seconds a preview for the dashboard's confirmation is kept
# How long a check waits for a usage export newer than the moment it asks about (asked
# for: the exporter looks every second; one made by a timer comes in its turn): the daily
# check before it lists (nothing is held meanwhile; it tries again within the hour), a dry
# run from the dashboard, and a removal once its files are gone (its locks held: then the
# release is put back and the check ends).
LISTING_WAIT_SECONDS = 3600.0
MANUAL_WAIT_SECONDS = 90.0


@dataclass
class Candidate:
    """A release due for the cleanup: unused for long enough - unless ``kept`` says why
    it stays."""

    ref: str
    album_id: str  # a catalog album's own; a fill's: the owned album
    fill: bool
    title: str
    artist: str
    created_at: float
    songs: list[str]
    kept: list[str] = field(default_factory=list)
    # Each song's library path as Shijhon recorded it: Navidrome's records must have it
    # there, or they are not this library's.
    paths: dict[str, str] = field(default_factory=dict)
    stranger: bool = False  # they do not: by this database, nothing is taken out


@dataclass
class Listing:
    at: float
    due: list[Candidate]  # due, with those kept (in use)
    waiting: int = 0  # releases not due yet (added within ``unused_days``)
    next_due: float | None = None  # when the first of those becomes due
    unused_days: float = 30.0  # the settings it was made with
    # Why nothing may be taken out by this listing: the database read is not this library's
    # Navidrome. Its "unused" releases are then not known to be unused.
    refused: str = ""
    # The uses this listing saw in Navidrome's records, by release - of every release, due
    # or not: a check records them.
    seen: dict[str, list[str]] = field(default_factory=dict)
    # What it read of Navidrome's records: when that was taken, from which database file
    # (a removal's own looks must come from the same one), and whether by the usage
    # export (else by Shijhon, as it listed).
    snapshot: Snapshot | None = None
    mark: Mark | None = None  # the moment it asked about
    exported: bool = False

    @property
    def snapshot_at(self) -> float | None:
        return self.snapshot.at if self.snapshot is not None else None

    @property
    def unused(self) -> list[Candidate]:
        return [c for c in self.due if not c.kept]


def listing(
    shijhon: sqlite3.Connection,
    navidrome: sqlite3.Connection,
    *,
    now: float,
    unused_days: float,
    catalog_albums: bool = True,
    fills: bool = True,
) -> Listing:
    """What the cleanup would take out now, and what it keeps and why (read only). The
    uses it sees are those of every release with placeholders (``seen``) - also of the
    kinds the settings leave alone, and of those not due."""
    found = Listing(now, [], unused_days=unused_days)
    if why := not_navidrome(navidrome):
        found.refused = f"what is read as Navidrome's records is not its database ({why})"
        return found
    since = now - unused_days * DAY
    usage, recorded = Usage(navidrome), seen_uses(shijhon)
    releases = shijhon.execute(
        "SELECT ref, album_id, owned_album_id, title, artist, created_at"
        " FROM releases WHERE removing_at IS NULL ORDER BY created_at, ref"
    ).fetchall()
    for release in releases:
        rows = shijhon.execute(
            "SELECT song_id, path, state, delivered_at, last_used_at FROM placeholders"
            " WHERE release_ref = ?",
            [release["ref"]],
        ).fetchall()
        if not rows:
            continue  # links to owned songs only: nothing to take out
        fill = release["owned_album_id"] is not None
        candidate = Candidate(
            str(release["ref"]),
            str(release["owned_album_id"] or release["album_id"]),
            fill,
            str(release["title"]),
            str(release["artist"]),
            float(release["created_at"]),
            [str(r["song_id"]) for r in rows],
            paths=recorded_paths(rows),
        )
        # Every release's uses, due or not: one seen now is recorded and keeps it, also
        # once it is removed again.
        there = used_in_navidrome(usage, candidate)
        if there:
            found.seen[candidate.ref] = there
        if not (fills if fill else catalog_albums):
            continue  # a kind the settings leave alone: its uses are noted all the same
        known = float(release["created_at"])
        if known >= since:
            found.waiting += 1
            due = known + unused_days * DAY
            found.next_due = due if found.next_due is None else min(found.next_due, due)
            continue
        candidate.kept = used_here(rows) + there + past(recorded.get(candidate.ref, ()), there)
        found.due.append(candidate)
    found.refused = foreign(navidrome, found.unused)
    return found


def foreign(navidrome: sqlite3.Connection, unused: list[Candidate]) -> str:
    """Why the database read cannot say that these releases are unused, or "": the songs
    of each must be in it at their recorded paths. A database of another Navidrome,
    an empty one or an old copy has no use of them to show, and every release would look
    unused. (A copy that has them all passes here; the removal's check once the files are
    gone does not pass it: ``not_there`` with ``gone``.)"""
    for candidate in unused:
        candidate.stranger = bool(not_there(navidrome, candidate.paths))
    strangers = [c for c in unused if c.stranger]
    if not strangers:
        return ""
    names = "; ".join(f"{c.artist} - {c.title}" for c in strangers[:5])
    more = f" and {len(strangers) - 5} more" if len(strangers) > 5 else ""
    return (
        f"Navidrome's records do not have the songs of {len(strangers)} of the"
        f" {len(unused)} release(s) that look unused at their recorded paths ({names}{more}):"
        " they are not this library's current ones"
        + (
            " - or Shijhon's records of those releases are out of date"
            * (len(strangers) < len(unused))
        )
        + ", so they cannot say who uses what"
    )


PUT_BACK = "the release was put back"
NOT_BACK_YET = "the release is put back by the repairs (not confirmed yet)"


def stale(candidate: Candidate, gone: bool, why: list[str]) -> str:
    """Why a removal's own check refuses what it reads (and with it the whole check)."""
    name = f"{candidate.artist} - {candidate.title}"
    back = f"; {PUT_BACK}" if gone else ""
    if why == [NOT_SAME]:
        return (
            f"Navidrome's records came from another database file as {name} was looked at"
            " again than when it was listed: they cannot say who uses what" + back
        )
    if not gone:
        return (
            f"Navidrome's records do not have the songs of {name} at their recorded"
            " paths: they are not this library's current ones, so they cannot say who uses"
            " what"
        )
    return (
        f"Navidrome's records still show the songs of {name} as present after their"
        " files were taken out: they are not Navidrome's current ones (an old copy?), so"
        f" they cannot say who uses what; {PUT_BACK}"
    )


def used_in_navidrome(navidrome: sqlite3.Connection | Usage, candidate: Candidate) -> list[str]:
    """Uses in Navidrome's records: a catalog album's own too, a fill's album's
    favorite or rating given after the fill."""
    usage = navidrome if isinstance(navidrome, Usage) else Usage(navidrome)
    return usage.in_use(
        candidate.songs,
        candidate.album_id,
        album_since=candidate.created_at if candidate.fill else 0.0,
        album_plays=not candidate.fill,
    )


def report(found: Listing, *, mode: str, unused_days: float) -> str:
    unused, kept = found.unused, [c for c in found.due if c.kept]
    what = {"off": "off: nothing is taken out", "dry_run": "a dry run: nothing is taken out"}
    lines = [
        f"Cleanup ({what.get(mode, 'on')}): {len(unused)} release(s)"
        f" {'not known to be used' if found.refused else 'unused'} for"
        f" {unused_days:g} days, {sum(len(c.songs) for c in unused)} placeholder(s);"
        f" kept (in use): {len(kept)}; not due yet: {found.waiting}"
    ]
    if found.refused:
        lines += ["", f"Refused - nothing is taken out: {found.refused}"]
    take_out = "Not taken out (refused)" if found.refused else "Take out"
    for heading, group in ((take_out, unused), ("Kept (in use)", kept)):
        if group:
            lines += ["", f"{heading}:"]
            for c in group:
                kind = f"fill of album {c.album_id}" if c.fill else f"catalog album {c.album_id}"
                added = time.strftime("%Y-%m-%d", time.localtime(c.created_at))
                why = f" - kept: {', '.join(c.kept)}" if c.kept else ""
                why += " - its songs are not in Navidrome's records" if c.stranger else ""
                lines.append(
                    f"  {c.artist} - {c.title} ({kind}, added {added}): {len(c.songs)}"
                    f" placeholder(s), {c.ref}{why}"
                )
    return "\n".join(lines)


@dataclass
class Swept:
    listed: Listing | None = None
    removed: list[str] = field(default_factory=list)  # "<artist> - <title>"
    placeholders: int = 0
    kept: int = 0  # found in use when it was taken out
    failed: int = 0
    mode: str = "dry_run"  # the mode it ran in ("dry_run" for the dashboard's dry run)
    manual: bool = False  # the dashboard's dry run, not the daily check
    # What became of each release due (its ref): "taken out", "kept: <why>", "failed".
    outcomes: dict[str, str] = field(default_factory=dict)
    refused: str = ""  # why nothing was taken out (placeholders may not be written now)
    stopped: str = ""  # why it stopped before the end ("switched off", an error's type)


class Cleanup:
    """The daily check (the app's background task). Its settings are read at every check
    (a change applies from the next one)."""

    def __init__(
        self,
        engine: PlaceholderEngine,
        *,
        database: Path,
        usage: UsageSource | None,
        mode: str = "off",
        unused_days: float = 30.0,
        catalog_albums: bool = True,
        fills: bool = True,
        filler: Fills | None = None,
        writes: PlaceholderWrites | None = None,
        changed: Callable[[], None] | None = None,
        interval_seconds: float = DAY,
        start_seconds: float = 600.0,
        settle_seconds: float = 2.0,
        clock: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], bool] | None = None,
    ) -> None:
        self.engine = engine
        self.database = database  # Shijhon's, read on its own connection (WAL)
        # Who uses what: the usage export, or Navidrome's database itself (None: no
        # cleanup).
        self.usage = usage
        self.listing_wait = LISTING_WAIT_SECONDS
        self.manual_wait = MANUAL_WAIT_SECONDS
        self.removal_wait = REMOVAL_WAIT_SECONDS
        self.waiting_for: float | None = None  # an export newer than this is waited for
        self.mode = mode
        self.unused_days = unused_days
        self.catalog_albums = catalog_albums
        self.fills = fills
        self.filler = filler  # a fill taken out: its album's match (None: fills stay)
        self.writes = writes
        self.changed = changed  # after releases were taken out (the library changed)
        self.interval = interval_seconds
        self.start = start_seconds
        # Before a listing is refused: listed once more after this long (a song being
        # swapped as it was read is not where its row says for a moment).
        self.settle = settle_seconds
        self.clock = clock
        self.spawn = spawn  # background work (the dashboard's dry runs and previews)
        # For the dashboard: the last daily check that listed (published once it is done),
        # and the dashboard's last dry run, apart.
        self.last: Swept | None = None
        self.last_dry_run: Swept | None = None
        self.failed: tuple[float, str] | None = None  # the last failed check: when, why
        self.next_at: float | None = None  # when the next daily check runs (wall clock)
        self.checking_since: float | None = None  # a check or dry run under way
        self.manual_at: float | None = None  # when the dashboard's last dry run began
        # One listing at a time, until its thread ends and what it saw is recorded - and
        # never beside a removal: a use a listing saw is recorded before any removal looks.
        self._listing_lock = anyio.Lock()
        # The dashboard's confirmation before "on": a listing with the settings sent, made
        # in the background, kept a few minutes (settings, listing), and the one running.
        self._preview: tuple[tuple[Any, ...], Listing] | None = None
        self._previewing: tuple[tuple[Any, ...], anyio.Event] | None = None
        self._preview_failed: tuple[tuple[Any, ...], str] | None = None
        self._failure_said = False  # (said once at least, before another is made)
        self._running_checks = 0

    async def run(self) -> None:
        if self.usage is None:
            if self.mode != "off":
                log.warning(
                    "cleanup: [cleanup] mode is %s, but there is no [navidrome]"
                    " usage_export_path (or database_path) to see whether anyone uses a"
                    " release: nothing is taken out",
                    self.mode,
                )
            return
        self.next_at = time.time() + self.start
        await anyio.sleep(self.start)
        while True:
            again = self.interval
            try:
                await self.sweep()
            except NotFresh as exc:  # no export to act on: it waits, and says so
                self.failed = (time.time(), str(exc))
                log.warning("cleanup: nothing is checked: %s", exc)
                again = min(again, self.listing_wait)
            except Exception as exc:  # the next check tries again
                self.failed = (time.time(), why_failed(exc))
                log.warning("cleanup failed: %s", why_failed(exc))
            self.next_at = time.time() + again
            await anyio.sleep(again)

    def _listing(
        self,
        mark: Mark,
        unused_days: float | None = None,
        catalog_albums: bool | None = None,
        fills: bool | None = None,
    ) -> Listing:
        """What a check would take out now: with its settings, or the ones given. From the
        usage export only one that covers ``mark`` (made after the check asked)."""
        assert self.usage is not None
        try:
            navidrome, snapshot = self.usage.open()
        except WrongSource as exc:  # not Navidrome's: refused, and said
            return Listing(
                self.clock(), [], refused=f"what is read as Navidrome's records is not: {exc}"
            )
        try:
            shijhon = open_read_only(self.database)
        except BaseException:
            navidrome.close()
            raise
        try:
            if not self.usage.covers(snapshot, mark):
                raise NotFresh(mark, snapshot)
            found = listing(
                shijhon,
                navidrome,
                now=self.clock(),
                unused_days=self.unused_days if unused_days is None else unused_days,
                catalog_albums=(self.catalog_albums if catalog_albums is None else catalog_albums),
                fills=(self.fills if fills is None else fills) and self.filler is not None,
            )
            found.snapshot, found.mark, found.exported = snapshot, mark, self.usage.export
            return found
        finally:
            shijhon.close()
            navidrome.close()

    async def _listed(self, *, wait: float | None = None, **settings: Any) -> Listing:
        """A listing, one at a time: it reads both databases in a worker thread, and the
        next waits until that thread is done. From the usage export, only from one made
        after this moment: asked for, and waited for ``wait`` seconds at most
        (:class:`NotFresh`) - before the listing's turn, so a dry run does not queue
        behind the daily check's wait. What it saw anyone use is recorded before anything
        else happens: a playlist entry, a queue, a bookmark or a share removed later
        still keeps its release - also from a listing that is refused (a use seen only
        ever keeps more), and from one made again."""
        assert self.usage is not None
        wait = self.manual_wait if wait is None else wait
        mark = await self._asked(wait)
        async with self._listing_lock:
            found = await self._read(mark, settings)
        if found.refused:
            # Read again before it is believed: a delivery or a swap under way while the
            # rows were read leaves a song elsewhere than its row said, for a moment, in
            # the right database too.
            await anyio.sleep(self.settle)
            mark = await self._asked(wait)
            async with self._listing_lock:
                found = await self._read(mark, settings)
        return found

    async def _read(self, mark: Mark, settings: dict[str, Any]) -> Listing:
        """One listing (the listing lock held), what it saw recorded."""
        # (only a stop cancels a listing now: its thread is then left to end alone)
        found = await anyio.to_thread.run_sync(
            lambda: self._listing(mark, **settings), abandon_on_cancel=True
        )
        await self.engine.note_uses(found.seen, found.at)
        return found

    async def _asked(self, wait: float) -> Mark:
        """Mark this moment and wait until the usage source can show it (the export: one
        made after it, asked for; said on the page meanwhile)."""
        assert self.usage is not None
        self.waiting_for = time.time() if self.usage.export else None
        try:
            return await self.usage.asked(wait=wait)
        finally:
            self.waiting_for = None

    @property
    def can_list(self) -> bool:
        """Whether a check can see who uses what (Navidrome's database is given)."""
        return self.usage is not None

    async def dry_run(self) -> Swept | None:
        """The dashboard's dry run, whatever the mode: what a check would take out now,
        listed and kept apart from the daily check's result - the library is not written
        (no repair either); the uses it sees are noted, as every check's are. None
        without Navidrome's database."""
        if self.usage is None:
            return None
        self.manual_at = time.time()
        with self._checking():
            try:
                done = Swept(await self._listed(), mode="dry_run", manual=True)
            except Exception as exc:
                self.failed = (time.time(), why_failed(exc))
                raise
        assert done.listed is not None
        done.refused = done.listed.refused
        if done.refused:
            log.error("cleanup (dry run): nothing would be taken out: %s", done.refused)
        self.last_dry_run = done
        return done

    async def preview(
        self, *, unused_days: float, catalog_albums: bool, fills: bool, wait: float
    ) -> Listing | None:
        """What a check with these settings would take out now, for the dashboard's
        confirmation before the cleanup takes out more (the library is not written, and it
        is not kept as a check; the uses it sees are noted, as every listing's are):
        made in the background, one at a time, and kept a few minutes for the same
        settings; None when it is not done within ``wait`` seconds (it goes on). Raises
        :class:`PreviewFailed` when the listing failed - also one that failed after its
        wait: said at the next request, before another is made."""
        key = (unused_days, catalog_albums, fills)
        found = self._fresh_preview(key)
        if found is not None:
            return found
        running = self._previewing
        if running is not None and running[0] != key:
            return None  # one at a time: another's is still running (asked again later)
        if running is None:
            failed = self._preview_failed
            if failed is not None and failed[0] == key and not self._failure_said:
                self._failure_said = True
                raise PreviewFailed(failed[1])
            done = anyio.Event()
            self._previewing = (key, done)
            self._preview = None  # never an older one for these settings

            async def make() -> None:
                try:
                    listing = await self._listed(
                        unused_days=unused_days, catalog_albums=catalog_albums, fills=fills
                    )
                    self._preview = (key, listing)
                except Exception as exc:
                    self._preview_failed = (key, why_failed(exc))
                    self._failure_said = False
                    log.warning("cleanup preview failed: %s", why_failed(exc))
                finally:
                    if self._previewing is not None and self._previewing[1] is done:
                        self._previewing = None
                    done.set()

            self._preview_failed = None
            if self.spawn is None or not self.spawn(make):
                self._previewing = None
                return None
            running = self._previewing
        assert running is not None
        with anyio.move_on_after(wait):
            await running[1].wait()
        failed = self._preview_failed
        if failed is not None and failed[0] == key:
            self._failure_said = True  # (to everyone who waited for it)
            raise PreviewFailed(failed[1])
        return self._fresh_preview(key)

    def _fresh_preview(self, key: tuple[Any, ...]) -> Listing | None:
        found = self._preview
        if found is None or found[0] != key or time.time() - found[1].at >= PREVIEW_KEPT:
            return None
        return found[1]

    @contextmanager
    def _checking(self) -> Iterator[None]:
        """A check or a dry run under way (for the page; several may overlap)."""
        if not self._running_checks:
            self.checking_since = time.time()
        self._running_checks += 1
        try:
            yield
        finally:
            self._running_checks -= 1
            if not self._running_checks:
                self.checking_since = None

    async def sweep(self) -> Swept:
        """One check: in a dry run it logs what it would take out; on, it takes it out; off,
        it only notes the uses it sees (as every check does). Its
        mode and settings are those it started with (a switch to "on" meanwhile takes out
        nothing now; a switch away from "on" stops it). Published for the dashboard once done."""
        mode = self.mode
        done = Swept(mode=mode)
        if self.usage is None:
            return done
        if mode == "off":
            # Nothing is listed or taken out; who uses what is still noted: a use
            # that ends before the cleanup is switched on keeps its release then.
            with self._checking():
                found = await self._listed(wait=self.listing_wait)
            if found.refused:
                log.warning("cleanup (off): the uses of releases are not all noted: %s",
                            found.refused)  # fmt: skip
            return done
        # All its settings as they were when it started (a change applies from the next).
        settings = {
            "unused_days": self.unused_days,
            "catalog_albums": self.catalog_albums,
            "fills": self.fills,
        }
        with self._checking():
            try:
                await self._sweep(done, settings)
            except BaseException as exc:
                done.stopped = done.stopped or type(exc).__name__
                raise
            finally:
                if done.listed is not None:
                    self.last = done
        return done

    async def _sweep(self, done: Swept, settings: dict[str, Any]) -> None:
        await self.engine.repair_removals()
        done.listed = await self._listed(wait=self.listing_wait, **settings)
        unused = done.listed.unused
        if done.listed.refused:  # not this library's Navidrome: by it, nothing goes
            done.refused = done.listed.refused
            log.error("cleanup: nothing is taken out: %s", done.refused)
            return
        if done.mode != "on":
            if unused:
                log.info(
                    "cleanup (dry run): %d release(s) unused for %g days would be taken out"
                    " (%d placeholders: %s); %d in use kept; `shijhon cleanup` lists them",
                    len(unused),
                    settings["unused_days"],
                    sum(len(c.songs) for c in unused),
                    _names(unused),
                    len(done.listed.due) - len(unused),
                )
            return
        if unused and self.writes is not None and (why := await self.writes.refused(fresh=True)):
            log.warning("cleanup: nothing taken out: %s", why)
            done.refused = why
            return
        try:
            for candidate in unused:
                if self.mode != "on":
                    done.stopped = "switched off"
                    break  # switched off meanwhile
                await self._take_out(candidate, done)
                if done.refused:  # the database read is not this library's current one
                    log.error("cleanup: nothing more is taken out: %s", done.refused)
                    break
        finally:
            if done.removed and self.changed is not None:
                self.changed()
        if done.removed or done.failed or done.kept:
            log.info(
                "cleanup: %d release(s) unused for %g days taken out (%d placeholders: %s);"
                " %d found in use kept, %d failed",
                len(done.removed),
                settings["unused_days"],
                done.placeholders,
                ", ".join(done.removed[:5]) + (" ..." if len(done.removed) > 5 else ""),
                done.kept,
                done.failed,
            )

    async def _take_out(self, candidate: Candidate, done: Swept) -> None:
        also = None
        if candidate.fill:
            assert self.filler is not None
            try:
                also = await self.filler.taken_out(candidate.album_id, candidate.ref)
            except Exception as exc:  # e.g. Navidrome: the others still get their turn
                done.failed += 1
                done.outcomes[candidate.ref] = "failed"
                log.warning("cleanup of %s - %s failed: %s", candidate.artist, candidate.title,
                            type(exc).__name__)  # fmt: skip
                return
            if also is None:
                done.outcomes[candidate.ref] = "gone"
                return  # not there as a fill any more

        refused: list[str] = []
        listed = done.listed
        assert listed is not None
        name = f"{candidate.artist} - {candidate.title}"

        async def check(rows: list[Any], gone: bool) -> list[str]:
            """Checked again under the release's locks, and once its files are gone."""
            candidate.songs = [str(r["song_id"]) for r in rows]
            candidate.paths = recorded_paths(rows)
            if why := used_here(rows) or past(await self.engine.seen_uses(candidate.ref), ()):
                return why
            # What the listing was read from shows the release before it is touched; once
            # its files are gone, only what is read after that (the export: one made
            # after this moment, asked for and waited for with the release's locks held).
            try:
                mark = await self._asked(self.removal_wait) if gone else listed.mark
                assert mark is not None
                why, known = await anyio.to_thread.run_sync(
                    self._navidrome, candidate, gone, mark, listed.snapshot
                )
            except NotFresh as exc:
                refused.append(str(exc) + (f"; {name} was put back" if gone else ""))
                return [NO_EXPORT]
            if why in REFUSALS:
                # Not this library's current database: this release stays (put back when
                # its files are gone already), and the check ends here.
                refused.append(stale(candidate, gone, why))
            elif known:  # seen now: recorded, as a listing's are
                await self.engine.note_uses({candidate.ref: why}, self.clock())
            return why

        try:
            # (never beside a listing: what one saw is recorded before this looks)
            async with self._listing_lock:
                removal = await self.engine.remove_release(candidate.ref, check=check, also=also)
        except Exception as exc:  # the others still get their turn
            done.failed += 1
            done.outcomes[candidate.ref] = "failed"
            log.warning("cleanup of %s failed: %s", name, getattr(exc, "reason", None)
                        or type(exc).__name__)  # fmt: skip
            return
        if refused:  # (not a use found: what was read is refused)
            done.refused = refused[0]
            if removal.unfinished:  # (its files were gone, and are not confirmed back)
                done.refused = done.refused.replace(PUT_BACK, NOT_BACK_YET).replace(
                    f"{name} was put back", f"{name} {NOT_BACK_YET.removeprefix('the release ')}"
                )
            return
        if removal.kept:
            done.kept += 1
            done.outcomes[candidate.ref] = "kept: " + ", ".join(removal.kept)
            log.info("cleanup: %s is kept: %s", name, ", ".join(removal.kept))
        elif removal.removed:
            done.removed.append(name)
            done.placeholders += removal.removed
            done.outcomes[candidate.ref] = "taken out"
        else:
            done.outcomes[candidate.ref] = "gone"  # no longer in the library

    def _navidrome(
        self, candidate: Candidate, gone: bool, mark: Mark, listed: Snapshot | None
    ) -> tuple[list[str], bool]:
        """The removal's own reading of Navidrome's records: (why the release stays, whether
        that is a use). What is read must be Navidrome's current records (``current``),
        cover ``mark`` (the export: one made after it), and come from the database file
        the listing was read from."""
        assert self.usage is not None
        navidrome, snapshot = self.usage.open()
        try:
            if not self.usage.covers(snapshot, mark):
                raise NotFresh(mark, snapshot)
            if listed is not None and snapshot.source != listed.source:
                return [NOT_SAME], False
            if why := current(navidrome, candidate.paths, gone):
                return why, False
            return used_in_navidrome(navidrome, candidate), True
        finally:
            navidrome.close()


def why_failed(exc: BaseException) -> str:
    """A failed check, for the page and the log: what it waits for, or what is wrong with
    what it reads as Navidrome's records (a file's path, never its content); else the
    error's type."""
    if isinstance(exc, (NotFresh, WrongSource, FileNotFoundError)):
        return str(exc)
    return type(exc).__name__


def _names(candidates: list[Candidate]) -> str:
    names = [f"{c.artist} - {c.title}" for c in candidates[:5]]
    return ", ".join(names) + (" ..." if len(candidates) > 5 else "")


# Actions that use a release taken out, and the parameters that may carry its old IDs (the
# actions that commit a catalog album). A view, a cover and a plain stream
# are no use (``views/removed.py``, ``delivery/intercept.py``); nor is taking a favorite
# or a rating back.
USES: dict[str, tuple[str, ...]] = {
    "scrobble": ("id",),  # "now playing", and a play
    "reportPlayback": ("mediaId",),
    "star": ("id", "albumId"),
    "setRating": ("id",),
    "createPlaylist": ("songId",),
    "updatePlaylist": ("songIdToAdd",),
    "savePlayQueue": ("current",),  # the queue's current song's release
    "savePlayQueueByIndex": ("id",),  # ... the one at currentIndex
    "createBookmark": ("id",),
    "createShare": ("id",),
    "download": ("id",),  # a song (an album's archive is refused anyway)
    "getTranscodeDecision": ("mediaId",),
    "getTranscodeStream": ("mediaId",),
    "jukeboxControl": ("id",),  # set and add, while the jukebox is on
}  # (a stream is one only when its audio needs converting: the stream decides, ``use``)


MEDIA = ("download", "getTranscodeStream")  # a HEAD of these only asks


class PreviewFailed(Exception):
    """The dashboard's preview failed (its exception's type)."""


class OldIds:
    """A use of an old ID of a release the cleanup took out - one of its song IDs, or a
    catalog album's album ID - adds the release back first, then the request
    goes on as usual. A use is what commits a catalog album (``USES``): the "now playing"
    report, a star, a rating, a playlist or queue entry, a bookmark, a share, a download...
    A view, a cover and a plain stream are answered from the release's record instead,
    writing nothing (``views/removed.py``, ``delivery/intercept.py``), so a client syncing
    its old IDs never undoes the cleanup. Credentials first; a failure leaves the
    request to Navidrome."""

    def __init__(
        self,
        engine: PlaceholderEngine,
        *,
        commits: Commits | None = None,
        fills: Fills | None = None,
        changed: Callable[[], None] | None = None,
        jukebox_enabled: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.engine = engine
        self.commits = commits  # a catalog album's cover, from the catalog
        self.fills = fills  # a fill comes back through its album's plan
        self.changed = changed
        self.jukebox_enabled = jukebox_enabled
        self.added_back = 0  # observable in tests

    def used(self, call: RestCall) -> set[str]:
        """The IDs a request uses (``USES``), as it names them - the jukebox's aside."""
        keys = USES.get(call.name)
        if keys is None or (call.http_method == "HEAD" and call.name in MEDIA):
            return set()  # a HEAD of audio only asks (Navidrome carries out the others)
        if call.name == "setRating" and (call.get("rating") or "0") == "0":
            return set()  # a rating taken back
        if call.name.startswith("getTranscode") and (call.get("mediaType") or "song") != "song":
            return set()
        if call.name == "jukeboxControl" and call.get("action") not in ("set", "add"):
            return set()  # nothing placed
        if call.name == "savePlayQueueByIndex":
            ids, index = call.getall("id"), call.get("currentIndex") or ""
            if not (index.isascii() and index.isdigit() and len(index) < 10):
                return set()
            if int(index) >= len(ids):
                return set()
            return {ids[int(index)]}
        found = {value for key, value in call.params if key in keys and value}
        if call.name == "download":  # songs (being taken out too), not an album's archive
            engine = self.engine
            found = {v for v in found if engine.removed_song(v) or engine.removing_song(v)}
        return found

    async def __call__(self, call: RestCall, ctx: RequestContext) -> None:
        if call.name not in USES:
            return
        engine = self.engine
        wanted = sorted(
            v for v in self.used(call) if engine.removed_release(v) or engine.removing(v)
        )
        if not wanted:
            return
        try:
            if await ctx.caller() is None:
                return  # Navidrome answers with its own credential error
            if call.name == "jukeboxControl" and (
                self.jukebox_enabled is not None and not await self.jukebox_enabled()
            ):
                return  # the jukebox is off: Shijhon answers 501, nothing is placed
            for value in wanted:  # being taken out now: its end decides (the release's lock)
                if (ref := engine.removing(value)) is not None:
                    async with engine.lock_for(ref):
                        pass
            # (an ID the owner's own song has now drops its record: nothing to add back;
            # one ID of each release is asked)
            decided: dict[str, str | None] = {}
            for value in wanted:
                release = engine.removed_release(value)
                if release is not None and release not in decided:
                    decided[release] = await engine.still_removed(value)
            refs = {ref for ref in decided.values() if ref is not None}
        except CheckFailed:  # no verdict on the credentials: the proxy's error
            raise
        except Exception:  # Navidrome unreachable: the proxy answers
            return
        for ref in sorted(refs):
            if not self.engine.restore_due(ref):
                continue
            try:
                await self._add_back(ref, call.name)
            except Exception as exc:  # never a 500: Navidrome answers the request
                self.engine.restore_failed(ref)  # not tried again for a while
                reason = getattr(exc, "reason", None) or type(exc).__name__
                log.warning("an old ID of %s (%s): not added back: %s", ref, call.name, reason)

    async def use(self, call: RestCall, ctx: RequestContext, song_id: str) -> bool:
        """A use a handler found (a stream its audio needs converting for): its
        release added back first, as ``__call__`` does. True when it is back."""
        engine = self.engine
        try:
            if await ctx.caller() is None:
                return False
            if (ref := engine.removing_song(song_id)) is not None:
                async with engine.lock_for(ref):
                    pass
            ref = await engine.still_removed(song_id)
        except Exception:
            return False
        if ref is None or not engine.restore_due(ref):
            return False
        try:
            await self._add_back(ref, call.name)
        except Exception as exc:  # never a 500: it plays from the record
            engine.restore_failed(ref)
            reason = getattr(exc, "reason", None) or type(exc).__name__
            log.warning("an old ID of %s (%s): not added back: %s", ref, call.name, reason)
            return False
        return engine.removed_release(song_id) is None

    async def _add_back(self, ref: str, method: str) -> None:
        stored = await self.engine.removed_record(ref)
        if stored is None:
            return  # added back meanwhile
        name = f"{stored['artist']} - {stored['title']}"
        owned = stored["owned_album_id"]
        if owned and self.fills is not None:
            await self.fills.fill_album(str(owned), f"an old ID ({method})")
            if await self.engine.removed_record(ref) is not None:
                self.engine.restore_failed(ref)
                log.info("an old ID of %s (%s): its album is not filled with it now", name, method)
                return
        else:
            cover = None
            if not owned and self.commits is not None:
                record = json.loads(stored["record"])
                release = release_from_data(json.loads(record["release"]["data"]))
                cover = await self.commits.cover_for(release)
            await self.engine.restore_release(ref, cover=cover)
        self.added_back += 1
        if self.changed is not None:
            self.changed()
        log.info("added back %s: an old ID (%s)", name, method)
