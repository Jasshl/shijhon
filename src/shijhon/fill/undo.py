"""Undoing automatic fills the fill policy would not make now (after its thresholds were
raised): ``shijhon fills-undo``.

A dry run (the default) lists, from Shijhon's and Navidrome's databases read without
changing them, every filled album that the policy would not fill automatically now (the
owner has fewer than ``min_songs`` of its songs and less than ``min_share`` of it). The
policy counts a part's songs playing for the album's tracks as owned.

**Anything used even once is kept, by the cleanup's own rule**: a fill any of
whose placeholders was favorited, rated, played, in a playlist, in a play queue,
bookmarked or shared (the song, its artist or a playlist holding it), by any user; whose
album was favorited or rated after the fill; that has delivered audio in place, or a
stream or download Shijhon served; or of which an earlier check saw a use that has ended
since (``seen_uses``). And nothing is undone by a database that is not this library's
Navidrome's current one (its songs must be there at their recorded paths; once their
files are gone, shown as missing).

With ``--apply`` the listed albums lose their placeholders - as the cleanup takes a
release out: under the release's and its songs' locks, with the use looked for again
there and once more when the files are gone (a use found puts them back), and kept as a
record, so a use of one of its old IDs adds it back - and their match, in the same
transaction, so they are matched again when viewed and filled on first use. **Only with
Shijhon stopped:** one process writes the library at a time,
so ``--apply`` takes the running service's lock and refuses while it is held
(``store/writer.py``). ``--expect DIGEST`` refuses to apply unless the list is still the one
the dry run showed (its digest); an album whose removal fails is reported and the others
go on. Navidrome keeps the removed placeholders as missing files
(``Scanner.PurgeMissing=never``), so the album's song count in its lists stays as it was
(the complete album's) until they are purged; filled again, the placeholders come back at
the same paths with the same IDs.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
import anyio
import anyio.to_thread

from shijhon.fill.fills import FillPolicy
from shijhon.navidrome.export import Snapshot
from shijhon.navidrome.usage import (
    NO_EXPORT,
    NOT_SAME,
    NOT_THERE,
    REFUSALS,
    REMOVAL_WAIT_SECONDS,
    Mark,
    NotFresh,
    Usage,
    UsageSource,
    current,
    not_navidrome,
    not_there,
    open_read_only,
    past,
    recorded_paths,
    seen_uses,
)
from shijhon.placeholders.engine import PlaceholderEngine, used_here
from shijhon.store import Store

log = logging.getLogger(__name__)


@dataclass
class Undo:
    album_id: str
    title: str
    artist: str
    release_ref: str
    owned: int
    tracks: int
    placeholders: list[str]
    reason: str
    kept: list[str] = field(default_factory=list)  # why it stays (references)
    created_at: float = 0.0  # when the fill was made: its album's favorite counts after
    seen: list[str] = field(default_factory=list)  # its uses in Navidrome's records now


@dataclass
class Applied:
    """What ``apply`` did."""

    done: int = 0  # albums that lost their placeholders
    gone: int = 0  # listed fills whose release was gone already (only their match went)
    failed: list[str] = field(default_factory=list)  # albums not undone, with why
    stopped: str = ""  # why the rest was not tried
    untried: int = 0  # listed fills not tried after that


def plan(
    database: Path,
    navidrome_database: Path | UsageSource,
    policy: FillPolicy,
) -> list[Undo]:
    """The fills to undo, and those kept because they are in use (read only; from the
    usage export: the one there now - ``apply`` waits for newer ones)."""
    return plan_from(database, navidrome_database, policy)[0]


def plan_from(
    database: Path,
    navidrome_database: Path | UsageSource,
    policy: FillPolicy,
) -> tuple[list[Undo], Snapshot]:
    """``plan``, with what it read of Navidrome's records (when, from which database)."""
    usage_source = _source(navidrome_database)
    shijhon = open_read_only(database)
    try:
        navidrome, snapshot = usage_source.open()
    except BaseException:
        shijhon.close()
        raise
    try:
        if why := not_navidrome(navidrome):
            raise sqlite3.DatabaseError(f"{usage_source.path} is not Navidrome's database ({why})")
        usage, recorded = Usage(navidrome), seen_uses(shijhon)
        fills = shijhon.execute(
            "SELECT r.ref, r.owned_album_id, r.title, r.artist, r.created_at,"
            " (SELECT COUNT(*) FROM track_links t WHERE t.release_ref = r.ref AND t.owned = 1)"
            " AS owned,"
            " (SELECT COUNT(*) FROM placeholders p WHERE p.release_ref = r.ref"
            " AND p.backing_song_id IS NOT NULL) AS parts,"
            " (SELECT COUNT(*) FROM track_links t WHERE t.release_ref = r.ref) AS tracks"
            " FROM releases r WHERE r.owned_album_id IS NOT NULL AND r.removing_at IS NULL"
            " ORDER BY r.artist, r.title"  # (one half taken out is the repairs' first)
        ).fetchall()
        found: list[Undo] = []
        for row in fills:
            owned, tracks, parts = int(row["owned"]), int(row["tracks"]), int(row["parts"])
            if policy.allows(owned + parts, tracks):
                continue
            counted = f"{owned} + {parts} from another album" if parts else str(owned)
            reason = f"below the fill policy ({counted} of {tracks} owned)"
            rows = shijhon.execute(
                "SELECT song_id, path, state, delivered_at, last_used_at FROM placeholders"
                " WHERE release_ref = ?",
                [row["ref"]],
            ).fetchall()
            songs = [str(r["song_id"]) for r in rows]
            album = navidrome.execute(  # the owned album's own name, as the library shows it
                "SELECT name, album_artist FROM album WHERE id = ?", [row["owned_album_id"]]
            ).fetchone()
            undo = Undo(
                str(row["owned_album_id"]),
                str(album["name"] if album else row["title"]),
                str(album["album_artist"] if album else row["artist"]),
                str(row["ref"]),
                owned,
                tracks,
                songs,
                reason,
                created_at=float(row["created_at"]),
            )
            # The cleanup's rule: Shijhon's own records of a use, Navidrome's
            # (the album's favorite or rating only when given after the fill), and the
            # uses earlier checks saw that have ended since.
            there = usage.in_use(songs, undo.album_id, album_since=undo.created_at)
            undo.seen = there
            undo.kept = used_here(rows) + there + past(recorded.get(undo.release_ref, ()), there)
            if not undo.kept and not_there(navidrome, recorded_paths(rows)):
                undo.kept = [NOT_THERE]  # not this library's database: nothing by it
            found.append(undo)
        return found, snapshot
    finally:
        shijhon.close()
        navidrome.close()


def digest(items: Iterable[Undo]) -> str:
    """A short fingerprint of the albums a list would undo (for ``--expect``)."""
    rows = sorted(f"{u.album_id} {u.release_ref} {len(u.placeholders)}" for u in to_undo(items))
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:12]


def report(items: Iterable[Undo]) -> str:
    items = list(items)
    undo = [u for u in items if not u.kept]
    kept = [u for u in items if u.kept]
    lines = [
        f"Fills to undo: {len(undo)} album(s),"
        f" {sum(len(u.placeholders) for u in undo)} placeholder(s); kept: {len(kept)}"
        f" (list {digest(items)})"
    ]
    for heading, group in (("Undo", undo), ("Kept (in use)", kept)):
        if group:
            lines += ["", f"{heading}:"]
            for u in group:
                why = f" - kept: {', '.join(u.kept)}" if u.kept else ""
                lines.append(
                    f"  {u.artist} - {u.title} ({u.album_id}): {len(u.placeholders)}"
                    f" placeholder(s) from {u.release_ref}; {u.reason}{why}"
                )
    return "\n".join(lines)


def _source(navidrome_database: Path | UsageSource) -> UsageSource:
    if isinstance(navidrome_database, UsageSource):
        return navidrome_database
    return UsageSource(navidrome_database, export=False)


async def apply(
    items: Iterable[Undo],
    engine: PlaceholderEngine,
    store: Store,
    navidrome_database: Path | UsageSource,
    *,
    wait: float = REMOVAL_WAIT_SECONDS,
    listed: Snapshot | None = None,
) -> Applied:
    """Undo the listed fills that are not in use. A failure does not stop the others; what
    was read turning out not to be Navidrome's current records does, and so does a
    usage export that is not made anew. The uses the list saw are recorded first, as
    every check's are: a fill kept for a playlist entry stays kept once it is gone."""
    items = list(items)
    result = Applied()
    source = _source(navidrome_database)
    await engine.note_uses({u.release_ref: u.seen for u in items if u.seen}, engine.clock())
    wanted = [u for u in items if not u.kept]
    began: Mark | None = None
    for index, undo in enumerate(wanted):
        name = f"{undo.artist} - {undo.title} ({undo.album_id})"

        async def check(rows: list[Any], gone: bool, undo: Undo = undo) -> list[str]:
            """Under the release's locks, and again once its files are gone - from the
            usage export: by one made after this command began, and after the files went
            (asked for and waited for, as the cleanup does)."""
            nonlocal began
            try:
                if gone or began is None:
                    mark = await source.asked(wait=wait)
                    began = began or mark
                else:
                    mark = began
                why, known = await anyio.to_thread.run_sync(
                    _used_now, source, undo, rows, gone, mark, listed
                )
            except NotFresh as exc:
                log.error("fills-undo: %s", exc)
                return [NO_EXPORT]
            if known:  # seen now: recorded
                await engine.note_uses({undo.release_ref: why}, engine.clock())
            return why

        async def also(conn: aiosqlite.Connection, undo: Undo = undo) -> None:
            """With the release's rows, in one transaction: its match (never a match
            saying "filled" without its release) - filled, or matched, again later."""
            await conn.execute("DELETE FROM album_matches WHERE album_id = ?", [undo.album_id])

        try:
            removal = await engine.remove_release(undo.release_ref, check=check, also=also)
        except Exception as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            log.warning("undo of %s - %s failed: %s", undo.artist, undo.title, reason)
            result.failed.append(f"{name}: {reason}")
            continue
        if removal.kept:
            back = " (not confirmed back yet: Shijhon's next start finishes putting it back)"
            result.failed.append(
                f"{name}: kept - {', '.join(removal.kept)}{back if removal.unfinished else ''}"
            )
            if removal.kept in REFUSALS:
                result.stopped = (
                    "what was read is not Navidrome's current database (--navidrome-db, or"
                    " what the usage export is made from)"
                )
            elif removal.kept == [NO_EXPORT]:
                result.stopped = (
                    "no newer usage export arrived (is the exporter running, and can Shijhon"
                    " write its request into the export's folder?)"
                )
            if result.stopped:
                result.untried = len(wanted) - index - 1
                break
        elif removal.removed > 0:
            result.done += 1
        else:  # its release was gone already: only its match is left to go
            result.gone += 1
            async with store.transaction() as conn:
                await also(conn)
    return result


def _used_now(
    source: UsageSource,
    undo: Undo,
    rows: list[Any],
    gone: bool,
    mark: Mark,
    listed: Snapshot | None,
) -> tuple[list[str], bool]:
    """The removal's own reading of Navidrome's records, as the cleanup's: (why the fill
    stays, whether that is a use). They must be Navidrome's current ones - the songs
    there at their recorded paths as present files, and shown as missing once their files
    are ``gone``; from the export, one made after ``mark``; from the database file the
    list was made from (``listed``) - and nobody may use them."""
    navidrome, snapshot = source.open()
    try:
        if not source.covers(snapshot, mark):
            raise NotFresh(mark, snapshot)
        if listed is not None and listed.source and snapshot.source != listed.source:
            return [NOT_SAME], False
        paths = recorded_paths(rows)
        if why := current(navidrome, paths, gone):
            return why, False
        usage = Usage(navidrome)
        return usage.in_use(list(paths), undo.album_id, album_since=undo.created_at), True
    finally:
        navidrome.close()


def to_undo(items: Iterable[Undo]) -> list[Undo]:
    return [u for u in items if not u.kept]
