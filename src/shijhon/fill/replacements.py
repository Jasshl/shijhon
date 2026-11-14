"""The record of a filled album filled from another release (``Fills.change``), and what
it is for: uses of the old release's song IDs, and finishing a replacement that stopped.

The old release's songs go in one transaction with the album's new plan and this record
(``release_replacements``, ``replaced_songs``); Navidrome keeps each song as a missing one
and apps may still hold its ID. The guard is for the requests that record a use on a song
(``USES``: a favorite, a rating, a play report, a playlist or queue entry, a bookmark, a
share - in JSON, XML or JSONP, by GET or as a form), and only for those: every other
request - a stream, a download, a transcode, the jukebox, a favorite or rating taken back,
a view - is Navidrome's to answer, with its own errors and validation; none of them
records anything for a song that is gone. For a request that names a recorded ID:

- an ID that is a song again goes on at once, as that song - Navidrome gives the chosen
  release's song the old one's ID where both have it under the same number and title;
- while the replacement is under way in this process, or unfinished (a stop, a fill that
  failed: the request starts its fill, the album's next use), the request waits for it,
  at most ``wait_seconds`` - the replacement goes on by itself after that;
- then an ID that no song has is answered with the API's "not found", and one whose
  album is still not filled - or that Navidrome could not be asked about - with an error
  to try again later. Neither is forwarded: Navidrome would record the use on a missing
  song.

An account without a library grant is the exception: its requests are Navidrome's, not
looked at here, as everywhere in the proxy.

The IDs are kept for as long as no song has them: Navidrome keeps the missing songs for
good (``Scanner.PurgeMissing`` is "never"), so a use forwarded a year later would land on
one just the same, and a row is a few bytes. A use made at Navidrome itself, not through
Shijhon, is not seen.

A replacement left unfinished is finished by Shijhon: at its next start, and after a
failed attempt - whatever failed in it - a few times more (``RETRY_SECONDS``); when that
keeps failing, the album stays a plan filled on its next use.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiosqlite
import anyio

from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.proxy.app import Reply, RequestContext
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import subsonic_error
from shijhon.store import Store

log = logging.getLogger(__name__)

# The requests that record a use on a song, and the parameters that name it. Taking a use
# back (``unstar``, a rating of 0) is none: an app must be able to clear what a missing
# song still carries. Audio requests and the jukebox record nothing for a missing song
# (checked against the tested Navidrome) and keep its own answers.
USES: dict[str, tuple[str, ...]] = {
    "star": ("id",),
    "setRating": ("id",),  # (above 0)
    "scrobble": ("id",),
    "reportPlayback": ("mediaId",),
    "createPlaylist": ("songId",),
    "updatePlaylist": ("songIdToAdd",),
    "savePlayQueue": ("id", "current"),
    "savePlayQueueByIndex": ("id",),
    "createBookmark": ("id",),
    "createShare": ("id",),
}
# After a fill from the chosen release failed: how long until Shijhon tries again, each
# time; after the last, only the album's next use - or the next start - fills it.
RETRY_SECONDS = (60.0, 600.0, 3600.0)
START_SECONDS = 5.0  # after a start, before the replacements a stop left are finished
POLL_SECONDS = 30.0  # how often the retries are looked for
NOT_FOUND = 70  # the Subsonic API's "the requested data was not found"
# How long a request waits for its album's replacement, unless the application says
# otherwise (it gives the wait a request for audio gets: ``[delivery] max_wait_seconds``).
WAIT_SECONDS = 30.0
UNDER_WAY = "its album is being filled from another release: try again later"
NOT_ASKED = "Navidrome could not be asked about the song: try again later"


RATING = re.compile(r"[+-]?[0-9]+")


def _rates(text: str | None) -> bool:
    """Whether ``setRating``'s rating gives a song a rating: a whole number other than 0,
    as Navidrome reads it ("0", "00", "+0" and "-0" take a rating back). One that is
    missing or no number records nothing: Navidrome answers it."""
    value = (text or "").strip()
    return bool(RATING.fullmatch(value)) and int(value) != 0


@dataclass(frozen=True)
class Unfinished:
    """A replacement whose album is not filled from the chosen release yet."""

    album_id: str
    old_ref: str
    new_ref: str
    attempts: int
    retry_at: float | None
    reason: str


class Replacements:
    def __init__(
        self,
        store: Store,
        engine: PlaceholderEngine,
        navidrome: NavidromeService,
        *,
        clock: Callable[[], float] = time.time,
        wait_seconds: float = WAIT_SECONDS,
        spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], bool] | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.navidrome = navidrome
        self.clock = clock
        # The longest a request waits for a replacement (``waits``: read at each wait, a
        # setting that applies at once), and how the fill it starts runs: in the
        # background, so that it goes on when the request has stopped waiting.
        self.wait_seconds = wait_seconds
        self.waits: Callable[[], float] | None = None
        self.spawn = spawn
        self._starting: dict[str, anyio.Event] = {}  # finishes a request started, by album
        # Finishes an album's unfinished replacement (``Fills.finish_change``): None
        # while albums are not filled at all (no catalog) - the IDs are refused all the same.
        self.finish: Callable[[str], Awaitable[str]] | None = None
        self.start_seconds = START_SECONDS
        self.poll_seconds = POLL_SECONDS
        self._songs: dict[str, str] | None = None  # a replaced song's ID -> its album
        self._open: set[str] = set()  # albums whose replacement is unfinished
        # Albums whose replacement (or its fill) is under way in this process, and the
        # song IDs it is taking out, known before its record is written.
        self._under_way: dict[str, anyio.Event] = {}
        self._going: dict[str, str] = {}

    # --- the record -------------------------------------------------------------------------

    async def load(self) -> None:
        """Read the record (at startup: requests look IDs up)."""
        songs = await self.store.fetchall("SELECT song_id, album_id FROM replaced_songs")
        albums = await self.store.fetchall("SELECT album_id FROM release_replacements")
        self._songs = {str(r["song_id"]): str(r["album_id"]) for r in songs}
        self._open = {str(r["album_id"]) for r in albums}

    async def _loaded(self) -> dict[str, str]:
        if self._songs is None:
            await self.load()
        assert self._songs is not None
        return self._songs

    @asynccontextmanager
    async def under_way(self, album_id: str, songs: Iterable[str] = ()) -> AsyncIterator[None]:
        """A replacement of the album, or the fill that finishes one, runs in this process:
        uses of the old release's ``songs`` and of the album's replaced songs wait for
        its end, whatever that is."""
        await self._loaded()
        done = self._under_way[album_id] = anyio.Event()
        going = list(songs)
        self._going.update(dict.fromkeys(going, album_id))
        try:
            yield
        finally:
            for song in going:
                self._going.pop(song, None)
            if self._under_way.get(album_id) is done:
                del self._under_way[album_id]
            done.set()

    async def record(
        self,
        conn: aiosqlite.Connection,
        album_id: str,
        scope: str,
        old_ref: str,
        new_ref: str,
        songs: Iterable[str],
        *,
        unfinished: bool,
    ) -> None:
        """Statements for the transaction that takes the old release's songs out: their
        IDs, and - unless the chosen release adds nothing, so the album is done with that
        transaction - the replacement to finish."""
        now = self.clock()
        await conn.executemany(
            "INSERT OR REPLACE INTO replaced_songs (song_id, album_id, release_ref,"
            " replaced_at) VALUES (?, ?, ?, ?)",
            [(song, album_id, old_ref, now) for song in songs],
        )
        await conn.execute("DELETE FROM release_replacements WHERE album_id = ?", [album_id])
        if unfinished:
            await conn.execute(
                "INSERT INTO release_replacements (album_id, scope, old_ref, new_ref,"
                " started_at, retry_at) VALUES (?, ?, ?, ?, ?, ?)",
                [album_id, scope, old_ref, new_ref, now, now],
            )

    async def recorded(self, album_id: str) -> None:
        """The removal's transaction is done: its rows, read into memory."""
        loaded = await self._loaded()
        rows = await self.store.fetchall(
            "SELECT song_id FROM replaced_songs WHERE album_id = ?", [album_id]
        )
        loaded.update({str(r["song_id"]): album_id for r in rows})
        row = await self.store.fetchone(
            "SELECT 1 FROM release_replacements WHERE album_id = ?", [album_id]
        )
        if row is not None:
            self._open.add(album_id)

    async def finished(self, album_id: str) -> None:
        """The album is filled from the chosen release, or its match was decided otherwise:
        nothing is left to finish. The IDs a song has again go; the others stay."""
        loaded = await self._loaded()
        async with self.store.transaction() as conn:
            await conn.execute("DELETE FROM release_replacements WHERE album_id = ?", [album_id])
            await conn.execute(
                "DELETE FROM replaced_songs WHERE song_id IN (SELECT song_id FROM placeholders)"
            )
        self._open.discard(album_id)
        rows = await self.store.fetchall("SELECT song_id FROM replaced_songs")
        kept = {str(r["song_id"]) for r in rows}
        for song in [s for s in loaded if s not in kept]:
            del loaded[song]

    async def failed(self, album_id: str, reason: str) -> Unfinished | None:
        """A fill from the chosen release failed: tried again later, a few times."""
        row = await self.store.fetchone(
            "SELECT attempts FROM release_replacements WHERE album_id = ?", [album_id]
        )
        if row is None:
            return None
        attempts = int(row["attempts"]) + 1
        wait = RETRY_SECONDS[attempts - 1] if attempts <= len(RETRY_SECONDS) else None
        await self.store.execute(
            "UPDATE release_replacements SET attempts = ?, retry_at = ?, reason = ?"
            " WHERE album_id = ?",
            [attempts, None if wait is None else self.clock() + wait, reason, album_id],
        )
        return await self.unfinished(album_id)

    async def unfinished(self, album_id: str) -> Unfinished | None:
        row = await self.store.fetchone(
            "SELECT * FROM release_replacements WHERE album_id = ?", [album_id]
        )
        if row is None:
            return None
        retry = row["retry_at"]
        return Unfinished(
            str(row["album_id"]),
            str(row["old_ref"]),
            str(row["new_ref"]),
            int(row["attempts"]),
            None if retry is None else float(retry),
            str(row["reason"]),
        )

    async def all_unfinished(self) -> list[Unfinished]:
        rows = await self.store.fetchall(
            "SELECT album_id FROM release_replacements ORDER BY started_at"
        )
        found = [await self.unfinished(str(r["album_id"])) for r in rows]
        return [item for item in found if item is not None]

    # --- finishing what a stop or a failure left ---------------------------------------------

    async def run(self) -> None:
        """The background task: the replacements a stop left, a moment after the start;
        then those whose fill failed, when their wait is over."""
        await anyio.sleep(self.start_seconds)
        await self._loaded()
        for album_id in sorted(self._open):  # at a start: each once, whatever it waits for
            await self.settle(album_id)
        while True:
            await anyio.sleep(self.poll_seconds)
            await self.retry()

    async def retry(self) -> int:
        """Finish the replacements whose wait is over; returns how many were tried."""
        rows = await self.store.fetchall(
            "SELECT album_id FROM release_replacements WHERE retry_at IS NOT NULL"
            " AND retry_at <= ? ORDER BY retry_at",
            [self.clock()],
        )
        for row in rows:
            await self.settle(str(row["album_id"]))
        return len(rows)

    async def settle(self, album_id: str) -> None:
        """Until the album's replacement is not under way: the one running is waited for;
        one left unfinished is finished now, if that works. Whatever fails in it - getting
        the album's group, reading the album, the fill, recording it - counts as a failed
        attempt, with its reason and the time of the next."""
        await self._loaded()
        running = self._under_way.get(album_id)
        if running is not None:
            await running.wait()
            return
        if album_id not in self._open or self.finish is None:
            return
        async with self.under_way(album_id):
            try:
                await self.finish(album_id)
            except Exception as exc:
                reason = _why(exc)
                try:
                    left = await self.failed(album_id, reason)
                except Exception as failure:  # (not even counted: the next poll tries)
                    log.warning("a release change is not counted: %s", type(failure).__name__)
                    return
                again = left is not None and left.retry_at is not None
                log.warning(
                    "the release change of album %s was not finished (%s): %s",
                    album_id,
                    reason,
                    "tried again later" if again else "its album is filled on its next use",
                )
                return
        self._open.discard(album_id)

    # --- uses of the old release's songs ------------------------------------------------------

    def used(self, call: RestCall) -> list[str]:
        """The song IDs a request records a use on, as it names them."""
        keys = USES.get(call.name)
        if keys is None:
            return []
        if call.name == "setRating" and not _rates(call.get("rating")):
            return []  # a rating taken back, or one Navidrome will not read
        if call.name == "reportPlayback" and (call.get("mediaType") or "song") != "song":
            return []
        return [value for key, value in call.params if key in keys and value]

    async def __call__(self, call: RestCall, ctx: RequestContext) -> Reply | None:
        """Before a request is handled: one that records a use on a replaced song is
        answered here - never forwarded - unless a song has that ID again. None: the
        request goes on. Credentials first: without a caller - none given, refused, or an
        account without a library grant - the request is Navidrome's."""
        if call.name not in USES:
            return None
        songs = await self._loaded()
        hit = [s for s in dict.fromkeys(self.used(call)) if s in songs or s in self._going]
        if not hit:
            return None
        if await ctx.caller() is None:
            return None
        states = [await self._state(song) for song in hit]
        if _WAIT in states:  # (never for an ID that is a song again)
            albums = {
                songs.get(s) or self._going.get(s)
                for s, state in zip(hit, states, strict=True)
                if state == _WAIT
            }
            await self._within(sorted(album for album in albums if album is not None))
            states = [await self._state(song) for song in hit]
        if _GONE in states:
            log.info("%s of a song a release change took out: not found", call.name)
            return subsonic_error(call, NOT_FOUND, "Song not found")
        if _WAIT in states:
            log.info("%s of a song whose album is not filled from its new release yet", call.name)
            return subsonic_error(call, 0, UNDER_WAY)
        if _UNKNOWN in states:
            log.info("%s of a replaced song: Navidrome could not be asked", call.name)
            return subsonic_error(call, 0, NOT_ASKED)
        return None

    async def _state(self, song_id: str) -> str:
        """What a use of the ID gets now: it goes on (``_SONG``), "not found" (``_GONE``),
        or an error to try again - its album's replacement is not finished (``_WAIT``),
        Navidrome could not be asked (``_UNKNOWN``)."""
        songs = await self._loaded()
        album = songs.get(song_id)
        if album is None:  # not recorded: being taken out now, or its release stayed
            return _WAIT if song_id in self._going else _SONG
        live = await self._live(song_id)
        if live is None:
            return _UNKNOWN
        if live:
            await self._forget(song_id)
            return _SONG
        return _WAIT if album in self._open or album in self._under_way else _GONE

    async def _within(self, albums: list[str]) -> None:
        """Wait for the albums' replacements - the one under way, or the fill of one left
        unfinished, started here in the background - for ``wait_seconds`` in all at most.
        They go on by themselves after that."""
        waited = []
        for album_id in albums:
            running = self._under_way.get(album_id) or self._start(album_id)
            if running is not None:
                waited.append(running)
        with anyio.move_on_after(self.waits() if self.waits else self.wait_seconds):
            for running in waited:
                await running.wait()

    def _start(self, album_id: str) -> anyio.Event | None:
        """Finish the album's unfinished replacement in the background (once, whoever
        asks); None when there is nothing to start, or nothing to start it with."""
        if album_id not in self._open or self.finish is None or self.spawn is None:
            return None
        started = self._starting.get(album_id)
        if started is not None:
            return started
        done = self._starting[album_id] = anyio.Event()

        async def work() -> None:
            try:
                await self.settle(album_id)
            finally:
                self._starting.pop(album_id, None)
                done.set()

        if not self.spawn(work):  # (Shijhon is stopping)
            del self._starting[album_id]
            return None
        return done

    async def _live(self, song_id: str) -> bool | None:
        """Whether the ID is a song now: a placeholder of the chosen release that took it
        over, or a file of the owner's. None: Navidrome could not be asked - the request
        is then answered with an error to try again, never forwarded on a guess."""
        if await self.engine.placeholder_row(song_id) is not None:
            return True
        try:
            song = await self.navidrome.song(song_id)
        except Exception as exc:
            log.info("a replaced song's ID: Navidrome not asked (%s)", type(exc).__name__)
            return None
        return song is not None and not song.get("missing")

    async def _forget(self, song_id: str) -> None:
        await self.store.execute("DELETE FROM replaced_songs WHERE song_id = ?", [song_id])
        if self._songs is not None:
            self._songs.pop(song_id, None)


_SONG, _GONE, _WAIT, _UNKNOWN = "song", "gone", "wait", "unknown"


def _why(exc: Exception) -> str:
    """Why an attempt to finish a replacement failed, as it is saved and shown: the
    failure's own reason (never an address or a secret), else its type."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    if isinstance(exc, NavidromeError) and str(exc):
        return str(exc)
    return f"unexpected {type(exc).__name__}"


def first_reply(*hooks: Callable[[RestCall, RequestContext], Awaitable[Any]]) -> Any:
    """The proxy's work before a request's handler, of several: each in turn, until one
    answers the request itself."""

    async def before(call: RestCall, ctx: RequestContext) -> Reply | None:
        for hook in hooks:
            reply = await hook(call, ctx)
            if reply is not None:
                return reply  # type: ignore[no-any-return]
        return None

    return before
