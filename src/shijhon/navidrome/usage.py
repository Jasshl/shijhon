"""What users did with songs, from Navidrome's own records (read only).

Navidrome keeps every user's favorites, ratings, plays, playlists, play queues, bookmarks
and shares in its database; its APIs show each user only their own. Placeholders that any
user did anything with are never taken out, so the checks -
``shijhon fills-undo`` and the cleanup of unused releases (``cleanup.py``) - read those
records themselves, from one of two sources (``UsageSource``):

- **the usage export** (``[navidrome] usage_export_path``; ``export.py``): a small file
  with only the tables and columns read here, made beside Shijhon by a process without a
  network. Shijhon's own container then never sees Navidrome's database, which also holds
  its secrets and its users' passwords. An export is a snapshot: the checks act only on
  one newer than the moment they ask about, ask for one, and wait for it.
- **Navidrome's database itself** (``[navidrome] database_path``), opened read-only: for
  those who accept mounting it. Always current.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread

from shijhon.navidrome.export import (
    ExportError,
    NotNavidrome,
    Snapshot,
    export,
    read_meta,
    request_path,
)

log = logging.getLogger(__name__)
POLL_SECONDS = 0.5  # how often a wait looks for a newer export
SAID_AFTER_SECONDS = 5.0  # a wait longer than this is said in the log
COPIES = "shijhon-usage-"  # the folders of Shijhon's own copies (in the temporary folder)
COPY_ATTEMPTS = 3

_TIME = re.compile(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?")


def open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class WrongSource(sqlite3.DatabaseError):
    """The file is not what its setting says: an export named as Navidrome's database, or
    the other way round, an export of another version, or no Navidrome database at all."""


@dataclass(frozen=True)
class Mark:
    """A moment a check asks about: what it reads must show what happened before it.
    ``asker`` and ``number``: Shijhon's request for an export made after this moment (the
    export that answers it, or a later one of this asker's, was made after it - whatever
    the clocks say); 0: no request (Navidrome's database itself, copied as it is read: its
    copy's time is compared. An export is never taken by its time)."""

    at: float
    asker: str = ""
    number: int = 0


class NotFresh(Exception):
    """No usage export made after the moment asked about arrived in time."""

    def __init__(self, mark: Mark, last: Snapshot | None) -> None:
        self.mark, self.last = mark, last
        had = f"the last is from {_clock(last.at)}" if last is not None else "there is none yet"
        super().__init__(
            f"no usage export made after {_clock(mark.at)} arrived ({had}): is the"
            " exporter running?"
        )


class NotAsked(NotFresh):
    """Shijhon could not write its request for a usage export: nothing would say that an
    export it reads was made after the moment asked about (its time does not: the
    exporter's clock may be ahead, a clock may step back), so nothing is acted on."""

    def __init__(self, mark: Mark, reason: str) -> None:
        self.mark, self.last = mark, None
        Exception.__init__(
            self,
            f"the request for a usage export could not be written ({reason}): the export's"
            " folder must be writable by Shijhon",
        )


def _clock(at: float) -> str:
    """A time of day - with its date when that is not today's."""
    then = time.localtime(at)
    today = then[:3] == time.localtime()[:3]
    return time.strftime("%H:%M:%S" if today else "%Y-%m-%d %H:%M:%S", then)


class _Temporary(sqlite3.Connection):
    """A connection to a copy in a folder of its own: removed when it is closed."""

    folder: Path | None = None

    def close(self) -> None:
        super().close()
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)


class UsageSource:
    """Where the checks read who uses what: the usage export a process beside Shijhon
    makes (``export``), or Navidrome's database itself. Either way what is read is a
    snapshot with only the tables and columns the checks need: of Navidrome's database
    itself Shijhon makes that copy when it reads (as the exporter would: one read
    transaction, also through a read-only folder), so the checks' rules are the same for
    both.

    A check first asks for the moment it is about (``asked``: a request the exporter
    answers with its next export), then reads (``open``), and acts only if what it read
    ``covers`` that moment."""

    def __init__(self, path: Path, *, export: bool) -> None:
        self.path = path
        self.export = export
        self._seen: tuple[tuple[int, int, int], Snapshot | None] | None = None
        self._asker = uuid.uuid4().hex[:12]  # this process's requests
        self._asked = 0
        self._asking = threading.Lock()  # one request at a time: numbered in the order written

    def __repr__(self) -> str:
        return f"UsageSource({str(self.path)!r}, export={self.export})"

    @property
    def setting(self) -> str:
        return "[navidrome] usage_export_path" if self.export else "[navidrome] database_path"

    def open(self) -> tuple[sqlite3.Connection, Snapshot]:
        """A read-only connection to a snapshot, and what it says of itself (its time:
        what happened in Navidrome before then is in it). The export's: as its maker
        stamped it; Navidrome's database itself: copied now. Raises :class:`WrongSource`
        when the file is the other kind, or not Navidrome's."""
        if not self.export:
            return self._copied()
        conn = open_read_only(self.path)
        try:
            try:
                found = read_meta(conn)
            except ExportError as exc:
                raise WrongSource(str(exc)) from None
            if found is None:
                raise WrongSource(
                    f"{self.path} is not a usage export (`shijhon usage-export` makes one;"
                    " Navidrome's database itself goes by [navidrome] database_path)"
                )
        except BaseException:
            conn.close()
            raise
        return conn, found

    def _copied(self) -> tuple[sqlite3.Connection, Snapshot]:
        """Navidrome's database itself: what the checks read of it, copied now into a
        private folder (removed when the connection is closed; one a kill left is removed
        by a later copy). A database changing as it is read is read again."""
        if not self.path.is_file():
            raise FileNotFoundError(f"no database at {self.path}")
        _sweep_copies()
        folder = Path(tempfile.mkdtemp(prefix=COPIES))  # (0700)
        copy = folder / "usage.sqlite3"
        try:
            for attempt in range(COPY_ATTEMPTS):
                try:
                    taken = export(self.path, copy, mode=0o600, answers="", locked=True)
                    break
                except NotNavidrome as exc:
                    hint = " (name it as [navidrome] usage_export_path, --usage-export)"
                    wrong = "is a usage export" in str(exc)
                    raise WrongSource(f"{exc}{hint if wrong else ''}") from None
                except ExportError as exc:
                    if attempt + 1 == COPY_ATTEMPTS:
                        raise sqlite3.OperationalError(str(exc)) from None
                    time.sleep(0.5)
            conn = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True, factory=_Temporary)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        conn.folder = folder
        conn.row_factory = sqlite3.Row
        return conn, taken

    def snapshot(self) -> Snapshot | None:
        """What the export there now says of itself (None: none yet, or not readable just
        now); the file is opened only when it changed. Raises :class:`WrongSource` for a
        file that is no export of this version."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if self._seen is None or self._seen[0] != key:
            try:
                conn, found = self.open()
                conn.close()
            except WrongSource:
                raise
            except (OSError, sqlite3.Error):
                return None
            self._seen = (key, found)
        return self._seen[1]

    def _ask(self) -> Mark:
        """Write Shijhon's request for an export made from now on (the exporter looks at
        this file every second, and stamps its export with the request it read before it
        began). Where Shijhon cannot write it: :class:`NotAsked` - the check ends, and
        takes nothing out."""
        if not self.export:
            return Mark(time.time())
        request = request_path(self.path)
        staged = request.with_name(f"{request.name}.{self._asker}")
        # Numbered, written and counted under one lock (a dry run asks while the daily
        # check waits): the file only ever goes up, and no number is given twice.
        with self._asking:
            at = time.time()
            number = self._asked + 1
            try:
                staged.write_text(f"{self._asker}:{number}\n")
                os.replace(staged, request)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    staged.unlink(missing_ok=True)
                raise NotAsked(Mark(at), exc.strerror or type(exc).__name__) from None
            self._asked = number
            return Mark(at, self._asker, number)

    def covers(self, snapshot: Snapshot, mark: Mark) -> bool:
        """Whether ``snapshot`` shows what happened before ``mark``: it answers that
        request of this process's or a later one (it was begun after the request was
        written). An export is never taken by its time: without a request, no export
        covers the moment."""
        if not self.export:
            return snapshot.at >= mark.at  # (Shijhon's own copy, made as it was read)
        if not mark.number:
            return False
        asker, _, number = snapshot.answers.partition(":")
        return asker == mark.asker and number.isdigit() and int(number) >= mark.number

    async def asked(self, *, wait: float) -> Mark:
        """Mark this moment, and return once what this source shows covers it: at once for
        Navidrome's database (its copy is taken when it is read); for the export, when one
        made after the request is there - waited for ``wait`` seconds at most
        (:class:`NotFresh`)."""
        mark = await anyio.to_thread.run_sync(self._ask)
        await self.covered(mark, wait=wait)
        return mark

    async def covered(self, mark: Mark, *, wait: float) -> None:
        """Return once the export there covers ``mark`` (:class:`NotFresh` after ``wait``
        seconds)."""
        if not self.export:
            return
        started = time.monotonic()
        said = False
        while True:
            last = await anyio.to_thread.run_sync(self.snapshot)
            if last is not None and self.covers(last, mark):
                return
            waited = time.monotonic() - started
            if waited >= wait:
                raise NotFresh(mark, last)
            if waited > SAID_AFTER_SECONDS and not said:
                log.info("waiting for a usage export made after %s (asked for)", _clock(mark.at))
                said = True
            await anyio.sleep(POLL_SECONDS)


def _sweep_copies() -> None:
    """Copies of Navidrome's records a killed process left in the temporary folder (this
    user's, older than an hour) go."""
    try:
        old = time.time() - 3600
        for folder in Path(tempfile.gettempdir()).glob(COPIES + "*"):
            stat = folder.lstat()
            if folder.is_dir() and stat.st_uid == os.getuid() and stat.st_mtime < old:
                shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


def configured(usage_export_path: Path | None, database_path: Path | None) -> UsageSource | None:
    """The source the settings name (the export, when both are given)."""
    if usage_export_path is not None:
        return UsageSource(usage_export_path, export=True)
    if database_path is not None:
        return UsageSource(database_path, export=False)
    return None


NOT_THERE = "Navidrome's records do not have its songs at their paths"
NOT_GONE = "Navidrome's records do not show its songs as gone"
NOT_PRESENT = "Navidrome lists its songs as missing"
NOT_SAME = "Navidrome's records now come from another database file"
# The database read is not Navidrome's current one: nothing is taken out by it.
REFUSALS = ([NOT_THERE], [NOT_GONE], [NOT_SAME])
NO_EXPORT = "no usage export newer than its removal"
# How long a removal waits, its locks held, for a usage export made after its files went.
REMOVAL_WAIT_SECONDS = 600.0
# What the checks read (a database without them is not Navidrome's).
TABLES = ("annotation", "playlist_tracks", "bookmark", "playqueue", "share", "media_file")


def not_navidrome(navidrome: sqlite3.Connection) -> str:
    """Why this is not a Navidrome database the checks can read, or "": the tables they
    read must be there (an empty file, or some other database, has none of them)."""
    names = {str(r[0]) for r in navidrome.execute("SELECT name FROM sqlite_master")}
    missing = [table for table in TABLES if table not in names]
    return f"it has no {', '.join(missing)} table" if missing else ""


def not_there(
    navidrome: sqlite3.Connection, recorded: Mapping[str, str], *, missing: bool | None = None
) -> list[str]:
    """The songs (song ID -> the library path Shijhon recorded) that Navidrome's records do
    not have at that path. Any means the database read is not this library's Navidrome
    (another one's, an empty or an old copy): nothing in it says who uses these songs, so
    nothing may be taken out by it. ``missing``: also those whose record does not say
    that of its file - listed as missing (True), or as present (False)."""
    found: dict[str, tuple[str, bool]] = {}
    ids = list(recorded)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = navidrome.execute(
            "SELECT id, path, missing FROM media_file"  # noqa: S608
            f" WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        found.update({str(r[0]): (str(r[1]), bool(r[2])) for r in rows})
    return [
        song
        for song, path in recorded.items()
        if song not in found
        or found[song][0] != path
        or (missing is not None and found[song][1] != missing)
    ]


def current(navidrome: sqlite3.Connection, recorded: Mapping[str, str], gone: bool) -> list[str]:
    """What a removal's own reading of Navidrome's records must show before it is asked
    who uses the songs; [] when it does. Only Navidrome's current database shows what
    Shijhon does through Navidrome: before the release is touched its songs are there at
    their recorded paths, as present files (``NOT_PRESENT``: this release stays - a copy
    made while it was out, or files that went missing, say nothing about it); once their
    files are ``gone`` and Navidrome's own answers list them as missing, so do its
    records. A copy made at any other time fails one of the two (``NOT_THERE``,
    ``NOT_GONE``: by this database nothing is taken out)."""
    if not_navidrome(navidrome) or not_there(navidrome, recorded):
        return [NOT_THERE]
    if not_there(navidrome, recorded, missing=gone):
        return [NOT_GONE] if gone else [NOT_PRESENT]
    return []


class Usage:
    """One reading of Navidrome's records (a connection): what is in play queues and in
    shares is read once, for every release asked about."""

    def __init__(self, navidrome: sqlite3.Connection) -> None:
        self.navidrome = navidrome
        self._queued: set[str] | None = None
        self._shared: set[str] | None = None

    def queued(self) -> set[str]:
        if self._queued is None:
            self._queued = {
                item
                for (items,) in self.navidrome.execute("SELECT items FROM playqueue")
                for item in str(items or "").split(",")
            }
        return self._queued

    def shared(self) -> set[str]:
        if self._shared is None:
            self._shared = {
                item.strip()
                for (resources,) in self.navidrome.execute("SELECT resource_ids FROM share")
                for item in str(resources or "").split(",")
            }
        return self._shared

    def in_use(
        self,
        songs: list[str],
        album_id: str = "",
        *,
        album_since: float | None = None,
        album_plays: bool = False,
    ) -> list[str]:
        """Why these songs must stay: any user's favorite, rating or play of them (also
        one taken back: Navidrome keeps a song's record of it), a playlist entry, a play
        queue, a bookmark or a share (of the songs, their album, their artists, or a
        playlist holding them). ``album_since``: also a favorite or rating of their album
        given after then, also one taken back (0: any time, and any record of the album) -
        the album's ID as recorded and as Navidrome has the songs - and, with
        ``album_plays``, the album's plays (a catalog album's are its songs')."""
        if not songs:
            return []
        navidrome = self.navidrome
        navidrome.execute("CREATE TEMP TABLE IF NOT EXISTS usage_ids (id TEXT PRIMARY KEY)")
        navidrome.execute("DELETE FROM usage_ids")
        navidrome.executemany("INSERT OR IGNORE INTO usage_ids VALUES (?)", [(s,) for s in songs])
        annotated = (
            "SELECT 1 FROM annotation WHERE item_type = 'media_file'"
            " AND item_id IN (SELECT id FROM usage_ids)"
        )
        checks = (
            ("favorited", annotated + " AND starred LIMIT 1"),
            ("rated", annotated + " AND rating > 0 LIMIT 1"),
            ("played", annotated + " AND (play_count > 0 OR play_date IS NOT NULL) LIMIT 1"),
            (
                "in a playlist",
                "SELECT 1 FROM playlist_tracks WHERE media_file_id IN (SELECT id FROM usage_ids)"
                " LIMIT 1",
            ),
            (
                "bookmarked",
                "SELECT 1 FROM bookmark WHERE item_id IN (SELECT id FROM usage_ids) LIMIT 1",
            ),
        )
        reasons = [why for why, sql in checks if navidrome.execute(sql).fetchone()]
        if not reasons and navidrome.execute(annotated + " LIMIT 1").fetchone():
            reasons.append("favorited, rated or played once")
        if "played" not in reasons and _optional_rows(
            navidrome,
            "SELECT 1 FROM scrobbles WHERE media_file_id IN (SELECT id FROM usage_ids) LIMIT 1",
        ):
            reasons.append("played")
        if not self.queued().isdisjoint(songs):
            reasons.append("in a play queue")
        albums = _albums(navidrome, album_id)
        if album_since is not None and (
            why := _album_used(navidrome, albums, album_since, album_plays)
        ):
            reasons.append(why)
        playlists = {
            str(r[0])
            for r in navidrome.execute(
                "SELECT DISTINCT playlist_id FROM playlist_tracks"
                " WHERE media_file_id IN (SELECT id FROM usage_ids)"
            )
        }
        if not self.shared().isdisjoint(
            set(songs) | playlists | albums | _artists(navidrome, albums)
        ):
            reasons.append("shared")
        return reasons


def in_use(
    navidrome: sqlite3.Connection,
    songs: list[str],
    album_id: str = "",
    *,
    album_since: float | None = None,
    album_plays: bool = False,
) -> list[str]:
    """``Usage.in_use`` for one question."""
    return Usage(navidrome).in_use(
        songs, album_id, album_since=album_since, album_plays=album_plays
    )


def once(reason: str) -> str:
    """A use a check saw earlier and Navidrome no longer shows: "in a playlist once"."""
    return reason if reason.endswith(" once") else f"{reason} once"


# What Navidrome itself still says of a favorite, rating or play taken back.
_TAKEN_BACK = {
    "favorited": ("favorited, rated or played once",),
    "rated": ("favorited, rated or played once",),
    "played": ("favorited, rated or played once",),
    "its album favorited": (
        "its album favorited, rated or played once",
        "its album favorited or rated once",
    ),
    "its album rated": (
        "its album favorited, rated or played once",
        "its album favorited or rated once",
    ),
    "its album played": ("its album favorited, rated or played once",),
}


def past(seen: Iterable[str], now: Iterable[str]) -> list[str]:
    """The uses seen earlier that are not there now, worded as past ones - those that
    Navidrome's own records do not say already."""
    there = set(now)
    return [
        once(reason)
        for reason in seen
        if reason not in there
        and once(reason) not in there
        and there.isdisjoint(_TAKEN_BACK.get(reason, ()))
    ]


def used_before(seen: Iterable[str]) -> list[str]:
    """Recorded uses, for a caller that does not know whether they are still there."""
    reasons = list(seen)
    return [f"used before ({', '.join(reasons)})"] if reasons else []


def seen_uses(shijhon: sqlite3.Connection) -> dict[str, list[str]]:
    """The uses earlier checks saw in Navidrome's records, by release."""
    found: dict[str, list[str]] = {}
    rows = shijhon.execute(
        "SELECT release_ref, reason FROM seen_uses ORDER BY seen_at, reason"
    ).fetchall()
    for row in rows:
        found.setdefault(str(row[0]), []).append(str(row[1]))
    return found


def recorded_paths(rows: list[Any]) -> dict[str, str]:
    """Each placeholder's library path as its row records it."""
    return {str(r["song_id"]): str(r["path"]) for r in rows}


def _albums(navidrome: sqlite3.Connection, album_id: str) -> set[str]:
    """The songs' album: as recorded, and as Navidrome has the songs now."""
    found = {album_id} if album_id else set()
    found |= {
        str(r[0])
        for r in _optional_rows(
            navidrome,
            "SELECT DISTINCT album_id FROM media_file WHERE id IN (SELECT id FROM usage_ids)",
        )
        if r[0]
    }
    return found


def _album_used(
    navidrome: sqlite3.Connection, albums: set[str], since: float, plays: bool
) -> str | None:
    if not albums:
        return None
    marks = ",".join("?" * len(albums))
    rows = navidrome.execute(
        "SELECT starred, starred_at, rating, rated_at, play_count FROM annotation"  # noqa: S608
        f" WHERE item_type = 'album' AND item_id IN ({marks})",
        sorted(albums),
    ).fetchall()
    for row in rows:
        if row["starred"] and _after(row["starred_at"], since):
            return "its album favorited"
        if (row["rating"] or 0) > 0 and _after(row["rated_at"], since):
            return "its album rated"
        if plays and (row["play_count"] or 0) > 0:
            return "its album played"
    for row in rows:  # one taken back: Navidrome keeps the album's record and its times
        if since <= 0:
            return "its album favorited, rated or played once"
        if any(t is not None and _after(t, since) for t in (row["starred_at"], row["rated_at"])):
            return "its album favorited or rated once"
    return None


def _after(value: object, since: float) -> bool:
    """Whether Navidrome's time ``value`` is at or after ``since`` (0: any time); a time
    that cannot be read counts as after (kept)."""
    if since <= 0 or value is None:
        return True
    match = _TIME.match(str(value))
    if match is None:
        return True
    day, clock, fraction, zone = match.groups()
    text = f"{day}T{clock}{(fraction or '')[:7]}"
    if zone:
        text += "+00:00" if zone == "Z" else zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return True
    if moment.tzinfo is None:  # no zone: UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp() >= since


def _artists(navidrome: sqlite3.Connection, albums: set[str]) -> set[str]:
    """The albums' artists (album artists, and their songs' artists), for artist shares."""
    found: set[str] = set()
    for album_id in albums:
        for query in (
            "SELECT album_artist_id FROM album WHERE id = ?",
            "SELECT DISTINCT artist_id FROM media_file WHERE album_id = ?"
            " UNION SELECT DISTINCT album_artist_id FROM media_file WHERE album_id = ?",
        ):
            params = [album_id] * query.count("?")
            found |= {str(r[0]) for r in _optional_rows(navidrome, query, params) if r[0]}
    return found


def _optional_rows(
    navidrome: sqlite3.Connection, sql: str, params: list[str] | None = None
) -> list[sqlite3.Row]:
    """Rows of a table or column another Navidrome schema may not have (none then)."""
    try:
        return navidrome.execute(sql, params or []).fetchall()
    except sqlite3.OperationalError:
        return []
