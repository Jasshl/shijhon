"""Download-first: fetch the whole track, then put it in place of the placeholder
so Navidrome serves it — with ranges, transcoding, offline downloads, jukebox and shares —
exactly like an owned file. The delivered format is kept. The engine does the targeted
scan and the song-ID check, and rolls back if the ID would change.

Also used for owned-recording backing where playback cannot simply be redirected to the
owned song (shares, jukebox, transcoding): the owned file is copied in place.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import httpx
import mutagen
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from shijhon.catalog.model import CatalogTrack
from shijhon.delivery import pacing
from shijhon.delivery.dash import DashError
from shijhon.delivery.expiry import DeliveredAudio
from shijhon.delivery.limits import UserLimits
from shijhon.delivery.playback import Deliverer, NoSource, Opened, PinBroken, Track
from shijhon.locks import KeyedLocks
from shijhon.placeholders.engine import PlaceholderEngine, ReplaceError
from shijhon.store import Store

log = logging.getLogger(__name__)

_SUFFIXES = ((FLAC, ".flac"), (MP4, ".m4a"), (MP3, ".mp3"), (OggOpus, ".opus"), (OggVorbis, ".ogg"))


def audio_suffix(path: Path) -> str | None:
    """The file's real container, from its content (never from the source's claims)."""
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError:
        return None
    for kind, suffix in _SUFFIXES:
        if isinstance(audio, kind):
            return suffix
    return None


def whole(opened: Opened) -> bool:
    """Whether an answer holds the whole file: a request without a range, or one from byte
    zero to its end."""
    if opened.body is None:
        return False
    if opened.status == 200:
        return True
    found = re.fullmatch(rb"bytes 0-(\d+)/(\d+)", dict(opened.headers).get(b"content-range", b""))
    return opened.status == 206 and found is not None and int(found[1]) + 1 == int(found[2])


def track_of(row: Any) -> Track:
    """A placeholder as delivery sees it."""
    return Track(
        row["song_id"],
        row["isrc"],
        row["title"],
        row["artist"],
        row["duration_ms"],
        ref=row["track_ref"],
        release=row["release_ref"],
        disc=row["disc"],
        number=row["track"],
        version=_advisory(row["tags"]),
    )


def _advisory(tags: str | None) -> str | None:
    """The version a placeholder's advisory tag names (1 explicit, 2 clean)."""
    try:
        value = (json.loads(tags or "{}").get("itunesadvisory") or [None])[0]
    except (ValueError, AttributeError, TypeError, IndexError):
        return None
    return {"1": "explicit", "2": "clean"}.get(str(value))


def catalog_track(track: CatalogTrack) -> Track:
    """A catalog song that is not in the library, keyed by its catalog track."""
    ref = str(track.ref)
    album = str(track.album) if track.album is not None else None
    return Track(
        ref,
        track.isrc,
        track.title,
        track.artist,
        track.duration_ms,
        ref=ref,
        release=album,
        disc=track.disc,
        number=track.number,
        version="clean" if track.clean else "explicit" if track.explicit else None,
    )


# A download's files in the work folder: ".<32 hex>.part" while it runs, "<32 hex>.<ext>"
# once its format is known (until it is in place).
_LEFT_OVER = re.compile(r"\.[0-9a-f]{32}\.part|[0-9a-f]{32}\.[a-z0-9]{1,5}")


class Unsettled(Exception):
    """A swap of the song's file that a stop interrupted could not be put right now: its
    row is not to be trusted (it may say "delivered" over the silent file)."""


@dataclass
class _Waiting:
    """One user's request for a song in its turn, or waiting for it; ``promoted``: the
    client plays it now; ``done`` and ``result``: the outcome of its fetch, which a repeat
    of the request waits for (``decided``: it fetched - else the repeat goes on itself)."""

    promoted: anyio.Event = field(default_factory=anyio.Event)
    done: anyio.Event = field(default_factory=anyio.Event)
    result: str | None = "the request it waited for failed"
    decided: bool = False


def _nothing_back() -> None:
    return None


@dataclass
class Turn:
    """A request's turn among its user's fetches from the add-ons, within the hour's
    allowance - taken before a stream's first bytes tell whether it needs converting,
    and its download's then. The allowance goes back at the turn's end unless the
    add-ons' audio was fetched in it (``spent``): a stream served as it is takes none.
    ``waited``: a repeat of the request waited for that request's fetch instead - its
    outcome is ``after`` (None: fetched)."""

    give_back: Callable[[], None] = _nothing_back
    waiting: _Waiting | None = None
    waited: bool = False
    after: str | None = None
    spent: bool = False
    playing: bool = False  # the song being played: it did not queue
    level: int | None = None  # how urgent it is at the add-ons' limits, when the caller says

    @property
    def urgency(self) -> int:
        """How urgent the request's add-on work is at the add-ons' limits: the song
        being played (also a queued fetch the client plays now) first, a queued download
        after every play - or as the caller said (``level``: a jukebox queue's upcoming
        songs do not wait for the user's turns, and are no song being played)."""
        if self.level is not None:
            return self.level
        promoted = self.waiting is not None and self.waiting.promoted.is_set()
        return pacing.PLAY if self.playing or promoted else pacing.QUEUED

    def decided(self, result: str | None) -> None:
        """The fetch's outcome, for a repeat of the request waiting for it."""
        if self.waiting is not None:
            self.waiting.result, self.waiting.decided = result, True


class DownloadFirst:
    def __init__(
        self,
        deliverer: Deliverer,
        engine: PlaceholderEngine,
        store: Store,
        work_dir: Path,
        *,
        timeout_seconds: float = 180.0,
        max_bytes: int = 1024 * 1024 * 1024,
        expiry: DeliveredAudio | None = None,
        limits: UserLimits | None = None,
    ) -> None:
        self.deliverer = deliverer
        self.expiry = expiry  # delivered audio goes back to placeholders
        self.limits = limits  # per user: fetches at once, and an hour's allowance
        self.engine = engine
        self.store = store
        self.work_dir = work_dir
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self._locks = KeyedLocks()
        self._waiting: dict[tuple[str, str], _Waiting] = {}  # by user and song
        # Whether the user's client says it plays the song (its key) now, when known.
        self.current: Callable[[str, str], bool] | None = None
        self.fetches = 0  # observable in tests

    def clear_left_over(self) -> int:
        """Downloads a stop left behind in the work folder - up to a gigabyte each, and no
        download removes another's - go (at startup, before any request: none is running).
        Only files named as downloads are; returns how many."""
        if not self.work_dir.is_dir():
            return 0
        removed = 0
        for path in self.work_dir.iterdir():
            if path.is_file() and _LEFT_OVER.fullmatch(path.name):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    async def ensure(
        self,
        song_id: str,
        user: str | None = None,
        *,
        playing: bool = False,
        resume: bool = False,
        turn: Turn | None = None,
        handed: Opened | None = None,
        urgency: int | None = None,
    ) -> str | None:
        """Make sure real audio is in place of this placeholder. Returns a failure reason,
        or None when Navidrome can now serve real audio for the song. ``user``: whose
        request it is (the per-user limits on fetches from the add-ons); ``playing``: the
        song being played, which does not wait behind the user's other fetches;
        ``urgency``: its place at the add-ons' limits when it is not what ``playing`` says
        (``pacing.QUEUED`` for a fetch that must not wait here, yet is no play).

        The turn among the user's fetches (and the hour's allowance) is waited for before
        the song's own lock is taken (``turn``: the request's own, taken already - for its
        first bytes); ``handed``: the song's whole audio opened already (the answer its
        first bytes were read from): fetched on, not asked for again (closed if unused).
        ``resume``: its routing continues one that found no audio a moment ago."""
        try:
            try:
                row = await self.settled(song_id)
            except Unsettled:
                return "an interrupted swap of its file is not put right yet"
            if row is None:
                return "not a placeholder"
            if row["state"] == "delivered":
                await self.used(song_id)
                return None
            try:
                if turn is not None:  # in the request's turn already
                    return await self._in_turn(song_id, turn, resume, handed)
                # Only an owned file actually there makes the add-ons unnecessary.
                backed = bool(row["backing_song_id"]) and await self._owned_file(row) is not None
                key = track_of(row).key
                async with self.turn(key, user, playing=playing, at_once=backed) as mine:
                    mine.level = urgency
                    return await self._in_turn(song_id, mine, resume, handed)
            except Exception as exc:  # never a 500: the caller falls back or reports
                log.warning("download-first failed: %s", type(exc).__name__)
                return f"unexpected {type(exc).__name__}"
        finally:
            if handed is not None:
                with anyio.CancelScope(shield=True):
                    await handed.close()

    async def _in_turn(
        self, song_id: str, turn: Turn, resume: bool, handed: Opened | None
    ) -> str | None:
        """The fetch in the request's turn - or, for a repeat of a request that fetched
        (``turn.waited``), that fetch's outcome: "fetched" only while the audio is in
        place."""
        if turn.waited:
            if turn.after is not None:
                return turn.after
            row = await self.settled(song_id, used=True)
            if row is not None and row["state"] == "delivered":
                return None
            return "its audio was fetched, but is not in place any more"
        try:
            return await self._ensure_held(song_id, turn, resume, handed)
        except Exception as exc:  # a repeat waiting for it gets this outcome
            turn.decided(f"unexpected {type(exc).__name__}")
            raise

    def promote(self, user: str, key: str) -> None:
        """The user's client plays the song ``key`` now (its report, its saved queue): a
        request for it waiting for its turn goes at once - for its fetch, and for
        its lookup's turn (a request naming a format or bitrate looks the song up first)."""
        waiting = self._waiting.get((user, key))
        if waiting is not None:
            waiting.promoted.set()
        if self.limits is not None:
            self.limits.promote(user, key)

    @asynccontextmanager
    async def turn(
        self, key: str, user: str | None, *, playing: bool = False, at_once: bool = False
    ) -> AsyncIterator[Turn]:
        """The request's turn for a fetch of the song ``key`` (its track key, the same
        before and after its commit) among the user's fetches, and from the hour's
        allowance. It is waited for before the song's own lock is taken: a fetch
        waiting its turn never holds up a play of the same song - the user's play
        (``playing``) promotes it (it goes at once too), and the one that gets the lock
        first fetches the audio for both; ``at_once``: needing no add-on (an owned file). A
        repeat of a request that waits its turn or fetches (a client's retry) waits for that
        request's outcome instead of queueing again - and goes on at once itself when that
        request fetched nothing (it streamed the song as it is)."""
        if self.limits is None or user is None:
            yield Turn()
            return
        if not playing and self.current is not None and self.current(user, key):
            playing = True  # the client said so while this request was on its way here
        waiting = self._waiting.get((user, key))
        if waiting is not None:
            if playing:
                waiting.promoted.set()  # the client plays it now: its fetch goes at once
            else:
                await waiting.done.wait()
                if waiting.decided:
                    yield Turn(waited=True, after=waiting.result)
                    return
                at_once = True  # it fetched nothing: this one goes on, without queueing again
        if playing or at_once:
            async with self.limits.download(user, queue=False) as give_back:
                turn = Turn(give_back, playing=playing)
                try:
                    yield turn
                finally:
                    if not turn.spent:
                        give_back()
            return
        mine = self._waiting[(user, key)] = _Waiting()
        fetching = False
        try:
            async with self.limits.download(user, promoted=mine.promoted) as give_back:
                turn = Turn(give_back, waiting=mine)
                try:
                    yield turn
                finally:
                    fetching = turn.spent
                    if not turn.spent:
                        give_back()
        finally:
            if fetching:  # a fetch that was interrupted failed: its repeats get that
                mine.decided = True  # outcome, never going on at once past the user's turns
            mine.done.set()
            if self._waiting.get((user, key)) is mine:
                del self._waiting[(user, key)]

    async def _ensure_held(
        self, song_id: str, turn: Turn, resume: bool = False, handed: Opened | None = None
    ) -> str | None:
        """``ensure`` once the fetch may start (in ``turn``: its allowance goes back when the
        add-ons turn out not to be needed). A fetch that waited for another fetch of the
        song, which failed, continues that fetch's routing - the source its wait cap
        cut short first, then those it did not try - instead of a full routing of its own."""
        waited = self._locks.held(song_id)  # another fetch of the song holds it
        if waited and handed is not None:  # not kept open, unread, for that fetch's time
            with anyio.CancelScope(shield=True):
                await handed.close()
            handed = None
        async with self._locks.hold(song_id):
            row = await self.settled(song_id)
            failure: str | None
            if row is None:
                turn.give_back()
                failure = "not a placeholder"
            elif row["state"] == "delivered":  # fetched meanwhile (by a play of it)
                turn.give_back()
                await self.used(song_id)
                failure = None
            elif refused := await self.engine.refused():
                turn.give_back()
                failure = refused  # nothing is fetched that could not be put in place
            else:
                owned = await self._owned_file(row) if row["backing_song_id"] else None
                if owned is not None:
                    turn.give_back()
                    failure = await self._place_owned(row, owned)
                else:  # the add-ons' audio (also while the owned file is missing)
                    turn.spent = True
                    failure = await self._download(
                        row, resume=waited or resume, handed=handed, urgency=turn.urgency
                    )
                if failure is None and self.expiry is not None:
                    done = await self.settled(song_id)
                    self.expiry.delivered(self._size(done["path"]) if done else 0)
            turn.decided(failure)
            return failure

    async def settled(self, song_id: str, *, used: bool = False) -> Any:
        """The placeholder's row once no swap of its file is under way (a revert shows the
        silent file before its row says so). ``used``: delivered audio a verified caller
        asked for - its use is recorded before the song's lock is released, so that a revert
        due now cannot slip in between and leave the request the silent file."""
        async with self.engine.lock_for(f"song:{song_id}"):
            # A swap of its file a stop interrupted is put right first: until then its row
            # can say "delivered" over the silent file.
            if not await self.engine.settle_song(song_id):
                raise Unsettled(song_id)
            row = await self.store.fetchone(
                "SELECT * FROM placeholders WHERE song_id = ?", [song_id]
            )
            if used and row is not None and row["state"] == "delivered":
                await self.used(song_id)
            return row

    def _size(self, relative: str) -> int:
        try:
            return self.engine.layout.absolute(relative).stat().st_size
        except OSError:
            return 0

    async def used(self, song_id: str) -> None:
        """A placeholder or its delivered audio was asked for by a verified caller: in
        use."""
        if self.expiry is not None:
            await self.expiry.used(song_id)

    async def _download(
        self,
        row: Any,
        *,
        resume: bool = False,
        handed: Opened | None = None,
        urgency: int = pacing.QUEUED,
    ) -> str | None:
        """The add-ons' audio fetched and put in place of the placeholder - read on from
        ``handed`` when that answer holds the whole file (its link is not asked again) and
        comes from a source that does not ask not to be used for downloads; never from one
        that does (``Deliverer.not_for_downloads``). ``urgency``: its place at the add-ons'
        limits (a queued download after every play)."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        partial = self.work_dir / f".{uuid.uuid4().hex}.part"
        try:
            try:
                with anyio.fail_after(self.timeout):
                    self.fetches += 1
                    kept = handed if handed is not None and whole(handed) else None
                    if kept is not None:
                        with pacing.urgent(urgency):
                            left_out = await self.deliverer.not_for_downloads()
                        if kept.source in {source.name for source, _ in left_out}:
                            # Its first bytes came from a source a download does not use:
                            # the song is fetched from another.
                            with anyio.CancelScope(shield=True):
                                await kept.close()
                            kept = None
                    while True:
                        if kept is not None:
                            opened = kept
                        else:
                            with pacing.urgent(urgency):
                                opened = await self.deliverer.open(
                                    track_of(row), None, resume=resume, heard=False,
                                    download=True,
                                )  # fmt: skip
                        size = 0
                        try:
                            if not whole(opened) or opened.body is None:  # (a body: for mypy)
                                return f"source answered HTTP {opened.status} to a full request"
                            async with await anyio.open_file(partial, "wb") as out:
                                async for chunk in opened.body:
                                    size += len(chunk)
                                    if size > self.max_bytes:
                                        return "delivered file is too large"
                                    await out.write(chunk)
                        except httpx.HTTPError:
                            if kept is None:
                                raise
                            kept = None  # kept open since its first bytes, it broke off:
                            continue  # the link is asked again, once
                        finally:
                            await opened.close()
                        break
            except (NoSource, PinBroken) as exc:
                return f"no source: {exc}"
            except TimeoutError:
                return "download timed out"
            except httpx.HTTPError as exc:
                return f"download failed ({type(exc).__name__})"
            except OSError as exc:
                return f"cannot store the download ({exc.strerror or type(exc).__name__})"
            expected = dict(opened.headers).get(b"content-length")
            if expected is not None and int(expected) != size:
                return "download incomplete"
            if opened.remux is not None:  # a DASH link's file served at once: copied out
                failure = await self._copied_out(partial, opened.remux, row["duration_ms"])
                if failure is not None:
                    return failure
            suffix = await anyio.to_thread.run_sync(audio_suffix, partial)
            if suffix is None:
                return "delivered data is not a supported audio file"
            final = partial.with_name(f"{uuid.uuid4().hex}{suffix}")
            partial.rename(final)
            partial = final
            try:
                await self.engine.replace_with_delivered(row["song_id"], final)
            except ReplaceError as exc:
                return str(exc)
            # (A play going on keeps its link: its later ranges stay on its file.)
            self.deliverer.in_library(track_of(row))
            return None
        finally:
            partial.unlink(missing_ok=True)

    async def _copied_out(self, partial: Path, kind: str, duration_ms: int) -> str | None:
        """A DASH link's file served at once (an MP4 of its segments, fetched into
        ``partial``), its audio copied out in its place as a join's is - a native FLAC, an
        MP3 or a fast-start M4A, whose length Navidrome reads - when that length is the
        catalog's (within the length tolerance). A failure reason, or None."""
        dash = self.deliverer.dash
        if dash is None:
            return "DASH is off"
        copy = partial.with_name(f".{uuid.uuid4().hex}.part")
        try:
            length = await dash.copied_out(partial, copy, kind)
            settings, wanted = self.deliverer.settings, duration_ms / 1000
            tolerance = max(
                settings.length_tolerance_seconds,
                settings.length_tolerance_percent / 100 * wanted,
            )
            if tolerance > 0 and wanted > 0 and abs(length - wanted) > tolerance:
                return f"another recording ({length:.1f}s, the catalog's {wanted:.1f}s)"
            copy.replace(partial)
        except DashError as exc:
            return f"the DASH audio could not be copied out ({exc})"
        finally:
            copy.unlink(missing_ok=True)
        return None

    async def _owned_file(self, row: Any) -> Path | None:
        """The owned recording backing the placeholder, while its file is there; a backing
        Navidrome no longer knows is cleared."""
        song = await self.engine.navidrome.song(row["backing_song_id"])
        if song is None:
            await self.store.execute(
                "UPDATE placeholders SET backing_song_id = NULL WHERE backing_song_id = ?",
                [row["backing_song_id"]],
            )
            return None
        return None if song.get("missing") else self.engine.layout.absolute(song["path"])

    async def _place_owned(self, row: Any, source: Path) -> str | None:
        try:
            await self.engine.replace_with_delivered(row["song_id"], source)
        except ReplaceError as exc:
            return str(exc)
        return None
