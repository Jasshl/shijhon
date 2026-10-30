"""Download-first: fetch the whole track, then put it in place of the placeholder
so Navidrome serves it — with ranges, transcoding, offline downloads, jukebox and shares —
exactly like an owned file. The delivered format is kept. The engine does the targeted
scan and the song-ID check, and rolls back if the ID would change.

Also used for owned-recording backing where playback cannot simply be redirected to the
owned song (shares, jukebox, transcoding): the owned file is copied in place.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import httpx
import mutagen
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.dsf import DSF
from mutagen.flac import FLAC
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

from shijhon.catalog.model import CatalogTrack
from shijhon.delivery import mpd, pacing
from shijhon.delivery.bulk import (
    Admission,
    BulkAcquisition,
    InPlace,
    Progress,
    ProgressError,
    report,
)
from shijhon.delivery.dash import DashError, copy_out, ffmpeg_path
from shijhon.delivery.expiry import DeliveredAudio
from shijhon.delivery.limits import Limited, UserLimits
from shijhon.delivery.playback import Deliverer, NoSource, Opened, PinBroken, Track
from shijhon.delivery.quality_gate import refusal
from shijhon.locks import KeyedLocks
from shijhon.music_imports import tagmap
from shijhon.placeholders.engine import PlaceholderEngine, ReplaceError
from shijhon.quality import Minimum
from shijhon.store import Store

log = logging.getLogger(__name__)

_SUFFIXES = ((FLAC, ".flac"), (MP4, ".m4a"), (MP3, ".mp3"), (OggOpus, ".opus"), (OggVorbis, ".ogg"))
# For a person's own imported song (``fetch``) and a stream kept (``keep_streamed``, with
# Matroska and raw ADTS AAC too: ``kept_suffix``): every container whose tags Shijhon can
# write with Mutagen and Navidrome reads (``music_imports/tagmap.py``,
# ``placeholders/tags.write_delivered``). Not raw ADTS AAC here: Navidrome reads no length for
# it (``docs/music-imports.md``).
_EVERY = (
    *_SUFFIXES,
    (OggFLAC, ".oga"),
    (OggSpeex, ".spx"),
    (WAVE, ".wav"),
    (AIFF, ".aiff"),
    (DSF, ".dsf"),
    (TrueAudio, ".tta"),
    (MonkeysAudio, ".ape"),
    (WavPack, ".wv"),
    (Musepack, ".mpc"),
    (ASF, ".wma"),
)


def audio_suffix(path: Path, *, every: bool = False) -> str | None:
    """The file's real container, from its content (never from the source's claims).
    ``every``: also the containers only an upgraded import takes (``_EVERY``)."""
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError:
        return None
    for kind, suffix in _EVERY if every else _SUFFIXES:
        if isinstance(audio, kind):
            return suffix
    return None


FFMPEG_SECONDS = 60  # a local probe or stream copy of one song, at most


def _probe(path: Path) -> dict[str, Any] | None:
    """ffprobe's format and streams of a local file (only the file protocol), or None."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-i", str(path),
             "-show_entries",
             "format=format_name,duration:stream=codec_type,codec_name:stream_disposition=attached_pic",
             "-of", "json"],
            capture_output=True, timeout=FFMPEG_SECONDS, check=True,
        )  # fmt: skip
        found = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def kept_suffix(path: Path) -> str | None:
    """A kept stream's container (``keep_streamed``): every one of ``_EVERY``, and two that
    Mutagen does not read, by ffprobe - Matroska audio (".mka", tagged by an ffmpeg stream
    copy) and raw ADTS AAC (".aac": Navidrome reads no length for it, so it goes into the
    library stream-copied into an M4A, ``_adts_in_m4a``)."""
    suffix = audio_suffix(path, every=True)
    if suffix is not None:
        return suffix
    probed = _probe(path)
    if probed is None:
        return None
    streams = probed.get("streams", [])
    # (A cover picture is a video stream marked as an attached picture: kept; a film not.)
    kinds = [
        "picture" if (s.get("disposition") or {}).get("attached_pic") else s.get("codec_type")
        for s in streams
    ]
    names = str(probed.get("format", {}).get("format_name", "")).split(",")
    if "audio" not in kinds or "video" in kinds:
        return None
    if "matroska" in names:
        return ".mka"
    if names == ["aac"] and kinds == ["audio"]:
        return ".aac"
    return None


# A Matroska file's audio, copied as it is into its own codec's native container - where
# Mutagen writes every value of every tag (``_out_of_matroska``): suffix, ffmpeg's output.
_NATIVE = {
    "flac": (".flac", ["-f", "flac"]),
    "aac": (".m4a", ["-movflags", "+faststart", "-f", "mp4"]),
    "alac": (".m4a", ["-movflags", "+faststart", "-f", "mp4"]),
    "mp3": (".mp3", ["-f", "mp3"]),
    "opus": (".opus", ["-f", "opus"]),
    "vorbis": (".ogg", ["-f", "ogg"]),
    "wavpack": (".wv", ["-f", "wv"]),
    **{
        pcm: (".wav", ["-f", "wav"]) for pcm in ("pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le")
    },
}


def _out_of_matroska(source: Path, out: Path) -> None:
    """A Matroska file's audio (its first audio stream) copied as it is - no re-encoding -
    into its codec's native container (``_NATIVE``): for a song whose tags Matroska cannot
    carry so that Navidrome reads every value (several artists: ``tagmap.matroska_carries``).
    Two local files only, under a deadline. Raises ``OSError`` for a codec without one."""
    probed = _probe(source) or {}
    codecs = [
        s.get("codec_name") for s in probed.get("streams", []) if s.get("codec_type") == "audio"
    ]
    native = _NATIVE.get(str(codecs[0])) if codecs else None
    ffmpeg = shutil.which("ffmpeg")
    if native is None or ffmpeg is None:
        raise OSError(f"no native container for {codecs[0] if codecs else 'its audio'}")
    subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file", "-f",
         "matroska", "-i", str(source), "-map", "0:a:0", "-c", "copy", "-map_metadata", "-1",
         *native[1], str(out)],
        capture_output=True, timeout=FFMPEG_SECONDS, check=True,
    )  # fmt: skip


def _adts_in_m4a(source: Path, out: Path) -> None:
    """Raw ADTS AAC copied as it is (no re-encoding) into a fast-start M4A, whose length
    Navidrome reads. Two local files only, under a deadline."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise OSError("ffmpeg was not found")
    subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file", "-f", "aac",
         "-i", str(source), "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart",
         "-f", "mp4", str(out)],
        capture_output=True, timeout=FFMPEG_SECONDS, check=True,
    )  # fmt: skip


def _out_of_ogg(source: Path, out: Path) -> None:
    """Ogg FLAC's audio copied as it is (no re-encoding) into a native FLAC, whose length
    Navidrome reads (none from Ogg FLAC): its total samples written in - ffmpeg's copy says
    none -, as many as Ogg's last page says. Two local files only, under a deadline."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise OSError("ffmpeg was not found")
    try:
        info = OggFLAC(source).info
    except mutagen.MutagenError as exc:
        raise OSError("its Ogg FLAC cannot be read") from exc
    subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file", "-f", "ogg",
         "-i", str(source), "-map", "0:a:0", "-c", "copy", "-map_metadata", "-1",
         "-f", "flac", str(out)],
        capture_output=True, timeout=FFMPEG_SECONDS, check=True,
    )  # fmt: skip
    with out.open("r+b") as flac:
        head = flac.read(26)
        found = mpd.flac_samples(head)
        if found is None or not info.sample_rate or info.length <= 0:
            raise OSError("no FLAC after the copy")
        if not found[1]:
            flac.seek(0)
            flac.write(mpd.with_samples(head, round(info.length * info.sample_rate)))


def _fragmented(path: Path) -> bool:
    """Whether the MP4 at ``path`` is in fragments, whose length Navidrome does not read
    (``mpd.fragmented``)."""
    try:
        with path.open("rb") as source:
            return mpd.fragmented(source)
    except OSError:
        return False


# A fragmented MP4's audio by its codec (ffprobe's name), as its fragments are copied out
# (``dash.copy_out``): as a DASH song's - FLAC, MP3, AAC, ALAC -, else in an ordinary MP4.
_KINDS = {"flac": "flac", "mp3": "mp3", "aac": "aac", "alac": "alac"}


def _kind_of(path: Path) -> str | None:
    """How a fragmented MP4 is copied out: by its one audio track's codec (``_KINDS``, else
    "mp4"); None for a file of several audio tracks - or one whose tracks ffprobe cannot
    tell -, whose audio tracks are all copied into an ordinary MP4 (``_every_track``)."""
    probed = _probe(path)
    if probed is None:
        return None
    codecs = [
        s.get("codec_name") for s in probed.get("streams", []) if s.get("codec_type") == "audio"
    ]
    if len(codecs) > 1:
        return None
    return _KINDS.get(str(codecs[0]) if codecs else "", "mp4")


def _every_track(source: Path, out: Path) -> float:
    """A fragmented MP4 of several audio tracks copied out of its fragments - every audio
    track as it is, nothing encoded anew (other tracks go, as from a DASH song's file) -
    into an ordinary fast-start MP4, whose length (its first audio track's) tag readers
    read: that length. Two local files only, under a deadline."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise OSError("ffmpeg was not found")
    subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file", "-f", "mp4",
         "-i", str(source), "-map", "0:a", "-c", "copy", "-map_metadata", "-1",
         "-movflags", "+faststart", "-f", "mp4", str(out)],
        capture_output=True, timeout=FFMPEG_SECONDS, check=True,
    )  # fmt: skip
    try:
        length = MP4(out).info.length
    except mutagen.MutagenError as exc:
        raise OSError("no MP4 after the copy") from exc
    if not length or length <= 0:
        raise OSError("no length after the copy")
    return float(length)


def needs_one_piece(path: Path) -> bool:
    """Whether the audio at ``path`` is in a container Navidrome reads no length from, which
    a delivery copies into one that tells it (``DownloadFirst._one_piece``): a fragmented
    MP4, Ogg FLAC."""
    if path.suffix.lower() == ".oga":
        return True
    return path.suffix.lower() in (".m4a", ".mp4") and _fragmented(path)


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


# ``DownloadFirst.keep_streamed``: the song has no placeholder (yet).
NOT_YET = "not in the library yet"


class Unsettled(Exception):
    """A swap of the song's file that a stop interrupted could not be put right now: its
    row is not to be trusted (it may say "delivered" over the silent file)."""


class _Again(Exception):
    """A background preparation's admitted attempt ended without audio: its plan has more
    to try, or a pause to wait for - outside the turn and the song's lock."""


class _Busy(_Again):
    """... or it did not start: another fetch holds the song's lock (and may wait for
    the slot this one holds). The slot goes back; it is admitted again a moment later."""


@dataclass(eq=False)
class _Interest:
    """One caller's still-live authorization/progress, separate from the shared file."""

    progress: Progress | None = None
    live: bool = True
    error: Exception | None = None
    waiting: _Waiting | None = None

    def join(self, waiting: _Waiting) -> None:
        if self.waiting is waiting:
            return
        if self.waiting is not None:
            self.waiting.interests.remove(self)
        self.waiting = waiting
        waiting.interests.append(self)

    def withdraw(self) -> None:
        self.live = False
        if self.waiting is not None:
            self.waiting.interests.remove(self)
            self.waiting = None


@dataclass
class _Waiting:
    """One user's request for a song in its turn, or waiting for it; ``promoted``: the
    client plays it now; ``done`` and ``result``: the outcome of its fetch, which a repeat
    of the request waits for (``decided``: it fetched - else the repeat goes on itself)."""

    promoted: anyio.Event = field(default_factory=anyio.Event)
    done: anyio.Event = field(default_factory=anyio.Event)
    result: str | None = "the request it waited for failed"
    decided: bool = False
    held: bool = False  # it acquired its user's turn before ending
    detached: bool = False  # its observer ended; another request may acquire on its own
    interests: list[_Interest] = field(default_factory=list)


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
    interest: _Interest | None = None
    # A background preparation's admitted attempt (``DownloadFirst._prepared``): its
    # slot, taken before this turn.
    admission: Admission | None = None

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
        bulk: BulkAcquisition | None = None,
    ) -> None:
        self.deliverer = deliverer
        self.expiry = expiry  # delivered audio goes back to placeholders
        self.limits = limits  # per user: fetches at once, and an hour's allowance
        self.bulk = bulk
        self.engine = engine
        self.store = store
        self.work_dir = work_dir
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self._locks = KeyedLocks()
        self._waiting: dict[tuple[str, str], _Waiting] = {}  # by user and song
        # Whether the user's client says it plays the song (its key) now, when known.
        self.current: Callable[[str, str], bool] | None = None
        # Whether an owned file may stand in for a catalog song the user wants (not below
        # their minimum quality: ``delivery/quality_gate.py``); None: always.
        self.stands_in: Callable[[str | None, str], Awaitable[bool]] | None = None
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
        bulk: bool = False,
        progress: Progress | None = None,
        background: bool = False,
    ) -> str | None:
        try:
            await report(progress, "waiting")
            result = await self._ensure(
                song_id,
                user,
                playing=playing,
                resume=resume,
                turn=turn,
                handed=handed,
                urgency=urgency,
                bulk=bulk,
                progress=progress,
                background=background,
            )
            if result is None:
                await report(progress, "done")
            return result
        except ProgressError as exc:
            raise exc.error from None

    async def _ensure(
        self,
        song_id: str,
        user: str | None = None,
        *,
        playing: bool = False,
        resume: bool = False,
        turn: Turn | None = None,
        handed: Opened | None = None,
        urgency: int | None = None,
        bulk: bool = False,
        progress: Progress | None = None,
        background: bool = False,
    ) -> str | None:
        """Make sure real audio is in place of this placeholder. Returns a failure reason,
        or None when Navidrome can now serve real audio for the song. ``user``: whose
        request it is (the per-user limits on fetches from the add-ons); ``playing``: the
        song being played, which does not wait behind the user's other fetches;
        ``urgency``: its place at the add-ons' limits when it is not what ``playing`` says
        (``pacing.QUEUED`` for a fetch that must not wait here, yet is no play);
        ``background``: a background preparation's scheduled fetch (``_prepared``).

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
                    return await self._in_turn(song_id, turn, resume, handed, bulk, user, progress)
                # Only an owned file actually there makes the add-ons unnecessary - one that
                # is not below the user's minimum quality.
                backed = (
                    bool(row["backing_song_id"])
                    and await self._owned_file(row) is not None
                    and await self._owned_stands_in(user, row)
                )
                if background and bulk and self.bulk is not None and not backed:
                    return await self._prepared(song_id, row, user, progress)
                key = track_of(row).key
                async with self.turn(
                    key, user, playing=playing, at_once=backed, progress=progress
                ) as mine:
                    mine.level = urgency
                    return await self._in_turn(song_id, mine, resume, handed, bulk, user, progress)
            except ProgressError:
                raise
            except Exception as exc:  # never a 500: the caller falls back or reports
                log.warning("download-first failed: %s", type(exc).__name__)
                return f"unexpected {type(exc).__name__}"
        finally:
            if handed is not None:
                with anyio.CancelScope(shield=True):
                    await handed.close()

    async def _prepared(
        self, song_id: str, row: Any, user: str | None, progress: Progress | None
    ) -> str | None:
        """A background preparation's scheduled fetch. Each attempt's waits - for its slot,
        and for paused add-ons - come first, heard by ``progress`` (a cancelled batch stops
        them), holding neither a turn of the user's nor the song's lock: no client's request
        waits behind them; and the song put in place by another fetch meanwhile ends them.
        An admitted attempt then takes a turn and the song's lock, each only when free at
        once - else its slot goes back, and it is admitted again a moment later - and looks
        at the song again first. A failure is reported only while the song is not in place."""
        assert self.bulk is not None
        track = track_of(row)

        def playing() -> bool:  # (the client plays it now: its admission goes first)
            return self.current is not None and user is not None and self.current(user, track.key)

        async def in_place() -> bool:
            try:
                row = await self.settled(song_id)
            except Unsettled:
                return False
            return row is not None and row["state"] == "delivered"

        plan = self.bulk.plan(
            track, user or "", progress, playing=playing, background=True, in_place=in_place
        )
        failure: str | None
        while True:
            try:
                with self.bulk.urgent(plan):
                    admission = await self.bulk.admit(plan)
            except InPlace:  # another fetch put it in place meanwhile
                await self.used(song_id)
                return None
            except NoSource as exc:
                failure = f"no source: {exc}"
                break
            if admission is None:
                failure = f"no source: {self.bulk.failed(plan)}"
                break
            again = False
            try:
                async with self.turn(track.key, user, progress=progress, now=True) as mine:
                    mine.admission = admission
                    try:
                        failure = await self._in_turn(
                            song_id, mine, False, None, True, user, progress
                        )
                        break
                    except _Again as exc:
                        # Nothing fetched: its allowance back; requests that joined it go
                        # on their own.
                        mine.spent = False
                        if mine.waiting is not None:
                            mine.waiting.detached, mine.waiting.decided = True, False
                        again = isinstance(exc, _Busy)
            except Limited:  # no turn free now, or the user's request for it goes first
                again = True
            finally:
                admission.release()
            if again:
                await report(progress, "waiting")  # (still wanted? a cancel stops it here)
                try:
                    await self.bulk.later(plan)
                except InPlace:
                    await self.used(song_id)
                    return None
            try:
                row = await self.settled(song_id)
            except Unsettled:
                return "an interrupted swap of its file is not put right yet"
            if row is None:
                return "not a placeholder"
            if row["state"] == "delivered":  # (another fetch put it in place meanwhile)
                await self.used(song_id)
                return None
        if failure is not None and await in_place():  # (another fetch put it in place)
            await self.used(song_id)
            return None
        return failure

    async def _in_turn(
        self,
        song_id: str,
        turn: Turn,
        resume: bool,
        handed: Opened | None,
        bulk: bool = False,
        user: str | None = None,
        progress: Progress | None = None,
    ) -> str | None:
        """The fetch in the request's turn - or, for a repeat of a request that fetched
        (``turn.waited``), that fetch's outcome: "fetched" only while the audio is in
        place."""
        caller = turn.interest or _Interest(progress)
        if progress is not None:
            if caller.progress is None:
                caller.progress = progress  # an explicit turn may precede its ensure callback
            elif caller.progress is not progress:
                error = ValueError("The reserved turn has a different progress callback.")
                caller.error = error
                if turn.waiting is not None and not turn.waited:
                    turn.waiting.detached = True
                    turn.waiting.decided = False
                raise ProgressError(error)
        if turn.waited:
            if turn.after is not None:
                return turn.after
            row = await self.settled(song_id, used=True)
            if row is not None and row["state"] == "delivered":
                return None
            return "its audio was fetched, but is not in place any more"
        admitted = handed is not None
        observer = caller

        async def observed(stage: str) -> None:
            nonlocal admitted, observer
            if stage == "verifying":
                admitted = True  # even ordinary acquisition has read its whole body by now
            while True:
                if observer.live and observer.error is None:
                    try:
                        if observer.progress is not None:
                            await observer.progress(stage)
                    except Exception as exc:
                        observer.error = exc
                        if not admitted:
                            raise  # no transfer to preserve: another caller may start its own
                    else:
                        if observer.live:
                            if stage in ("preparing", "fetching"):
                                admitted = True
                            return
                remaining = turn.waiting.interests if turn.waiting is not None else ()
                replacement = next(
                    (other for other in remaining if other.live and other.error is None), None
                )
                if replacement is None:
                    raise caller.error or observer.error or RuntimeError("no audio caller remains")
                observer = (
                    replacement  # its own callback must accept this same stage before writing
                )

        try:
            result = await self._ensure_held(
                song_id,
                turn,
                resume,
                handed,
                bulk,
                user,
                observed if caller.progress is not None else None,
            )
        except ProgressError:
            # Progress belongs to this caller: a cancelled batch or revoked authorization
            # says nothing about another caller's acquisition. Its waiters elect a new
            # leader with their own observer, retaining ordinary fetch failure sharing.
            if turn.waiting is not None:
                turn.waiting.detached = True
                turn.waiting.decided = False
            if not admitted:
                turn.spent = False  # no upstream work: return this caller's allowance
            raise
        except Exception as exc:  # a repeat waiting for it gets this outcome
            turn.decided(f"unexpected {type(exc).__name__}")
            raise
        # The shared result is already decided. Reject only this caller's interest, without
        # clearing the result or discarding bytes acquired for a surviving authorized caller.
        if caller.error is not None:
            raise ProgressError(caller.error)
        return result

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
        self,
        key: str,
        user: str | None,
        *,
        playing: bool = False,
        at_once: bool = False,
        progress: Progress | None = None,
        now: bool = False,
    ) -> AsyncIterator[Turn]:
        """A caller's turn and interest; leaving or cancelling it withdraws that interest."""
        interest = _Interest(progress)
        try:
            async with self._turn(
                key, user, interest, playing=playing, at_once=at_once, now=now
            ) as turn:
                yield turn
        finally:
            interest.withdraw()

    @asynccontextmanager
    async def _turn(
        self,
        key: str,
        user: str | None,
        interest: _Interest,
        *,
        playing: bool = False,
        at_once: bool = False,
        now: bool = False,
    ) -> AsyncIterator[Turn]:
        """The request's turn for a fetch of the song ``key`` (its track key, the same
        before and after its commit) among the user's fetches, and from the hour's
        allowance. It is waited for before the song's own lock is taken: a fetch
        waiting its turn never holds up a play of the same song - the user's play
        (``playing``) promotes it (it goes at once too), and the one that gets the lock
        first fetches the audio for both; ``at_once``: needing no add-on (an owned file). A
        repeat of a request that waits its turn or fetches (a client's retry) waits for that
        request's outcome instead of queueing again - and goes on at once itself when that
        request fetched nothing (it streamed the song as it is). ``now``: a background
        preparation's admitted attempt - a turn now or none (``Limited``): it waits neither
        for the user's turns nor behind the user's request for the song, which goes first."""
        if self.limits is None or user is None:
            yield Turn(interest=interest)
            return
        if not playing and self.current is not None and self.current(user, key):
            playing = True  # the client said so while this request was on its way here
        waiting = self._waiting.get((user, key))
        if now and waiting is not None and not playing:
            raise Limited("the user's request for the song goes first")
        while waiting is not None:
            interest.join(waiting)
            if playing:
                waiting.promoted.set()  # the client plays it now: its fetch goes at once
                break
            else:
                await waiting.done.wait()
                if interest.error is not None:
                    raise ProgressError(interest.error)
                if waiting.detached:
                    waiting = self._waiting.get((user, key))
                    continue  # one new leader; the other waiters join that request's result
                if waiting.decided:
                    yield Turn(waited=True, after=waiting.result, interest=interest)
                    return
                if not waiting.held:
                    waiting = self._waiting.get((user, key))
                    continue  # cancelled in the queue: a new leader must take its turn
                at_once = True  # it fetched nothing: this one goes on, without queueing again
                break
        if playing or at_once:
            async with self.limits.download(user, queue=False) as give_back:
                turn = Turn(give_back, playing=playing, interest=interest)
                try:
                    yield turn
                finally:
                    if not turn.spent:
                        give_back()
            return
        mine = self._waiting[(user, key)] = _Waiting()
        interest.join(mine)
        fetching = False
        try:
            async with self.limits.download(user, promoted=mine.promoted, now=now) as give_back:
                mine.held = True
                turn = Turn(give_back, waiting=mine, interest=interest)
                try:
                    yield turn
                finally:
                    fetching = turn.spent
                    if not turn.spent:
                        give_back()
        finally:
            if fetching and not mine.detached:  # an interrupted fetch is its repeats' failure
                mine.decided = True  # outcome, never going on at once past the user's turns
            mine.done.set()
            if self._waiting.get((user, key)) is mine:
                del self._waiting[(user, key)]

    async def _ensure_held(
        self,
        song_id: str,
        turn: Turn,
        resume: bool = False,
        handed: Opened | None = None,
        bulk: bool = False,
        user: str | None = None,
        progress: Progress | None = None,
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
        async with self._hold(song_id, now=turn.admission is not None):
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
                if owned is not None and await self._owned_stands_in(user, row):
                    turn.give_back()
                    failure = await self._place_owned(row, owned, progress=progress)
                else:  # the add-ons' audio (also while the owned file is missing)
                    turn.spent = True
                    failure = await self._download(
                        row,
                        resume=waited or resume,
                        handed=handed,
                        urgency=turn.urgency,
                        bulk=bulk,
                        user=user,
                        progress=progress,
                        playing=lambda: turn.urgency == pacing.PLAY,
                        admission=turn.admission,
                    )
                    if failure is not None and owned is not None:
                        # An owned file below the user's minimum, and the add-ons had no
                        # audio: the owned file after all.
                        failure = await self._place_owned(row, owned, progress=progress)
                if failure is None and self.expiry is not None:
                    done = await self.settled(song_id)
                    self.expiry.delivered(self._size(done["path"]) if done else 0)
            turn.decided(failure)
            return failure

    @asynccontextmanager
    async def _hold(self, song_id: str, *, now: bool = False) -> AsyncIterator[None]:
        """The song's lock. ``now`` (a background preparation's admitted attempt, holding a
        slot): only when it is free at once, else ``_Busy`` - its holder may be waiting for
        that slot."""
        if not now:
            async with self._locks.hold(song_id):
                yield
            return
        async with self._locks.hold_now(song_id) as held:
            if not held:
                raise _Busy
            yield

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
        bulk: bool = False,
        user: str | None = None,
        progress: Progress | None = None,
        playing: Callable[[], bool] | None = None,
        admission: Admission | None = None,
    ) -> str | None:
        """The add-ons' audio fetched and put in place of the placeholder - read on from
        ``handed`` when that answer holds the whole file (its link is not asked again) and
        comes from a source that does not ask not to be used for downloads; never from one
        that does (``Deliverer.not_for_downloads``). ``urgency``: its place at the add-ons'
        limits (a queued download after every play). ``admission``: a background
        preparation's attempt, admitted already - the download timeout bounds it from
        here; one that ends without audio raises ``_Again``."""
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
                        elif admission is not None and self.bulk is not None:
                            plan = admission.plan
                            # Played since its admission (its turn promoted, the client's
                            # report): its attempt goes first too.
                            if playing is not None and playing():
                                plan.urgency.raise_to(pacing.PLAY)
                            plan.promoted()
                            with self.bulk.urgent(plan):
                                attempted = await self.bulk.attempt(admission, progress)
                            if attempted is None:
                                raise _Again
                            opened = attempted
                        elif bulk and self.bulk is not None:
                            opened = await self.bulk.open(
                                track_of(row), user or "", progress, playing=playing
                            )
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
            return await self._put_in_place(row, partial, opened.remux, progress=progress)
        finally:
            partial.unlink(missing_ok=True)

    async def _put_in_place(
        self,
        row: Any,
        partial: Path,
        remux: str | None,
        *,
        progress: Progress | None = None,
        length: bool = False,
        every: bool = False,
    ) -> str | None:
        """The whole audio in ``partial`` (a file of the work folder, gone afterwards) put in
        place of the placeholder: a DASH link's file served at once (``remux``) copied out
        first, its real container read from its content, audio in a container Navidrome
        reads no length from copied into one that tells it (``_one_piece``) - and with
        ``length``, its length the catalog's (within the length tolerance), where its
        container tells one. A failure reason, or None. ``every``: any container of
        ``_EVERY`` (tagged as ``placeholders/tags.write_delivered`` does)."""
        final: Path | None = None
        try:
            await report(progress, "verifying")
            if remux is not None:  # a DASH link's file served at once: copied out
                failure = await self._copied_out(partial, remux, row["duration_ms"])
                if failure is not None:
                    return failure
            inspect = kept_suffix if every else audio_suffix
            suffix = await anyio.to_thread.run_sync(inspect, partial)
            if suffix == ".aac":  # (raw ADTS: into an M4A, which tells its length)
                copy = partial.with_name(f".{uuid.uuid4().hex}.part")
                try:
                    await anyio.to_thread.run_sync(_adts_in_m4a, partial, copy)
                    await anyio.Path(copy).replace(partial)
                except (OSError, subprocess.SubprocessError) as exc:
                    return f"the AAC could not be copied into an M4A ({type(exc).__name__})"
                finally:
                    with anyio.CancelScope(shield=True):
                        await anyio.Path(copy).unlink(missing_ok=True)
                suffix = await anyio.to_thread.run_sync(audio_suffix, partial)
            if suffix == ".mka" and not tagmap.matroska_carries(json.loads(row["tags"])):
                # (Several artists, say: the same audio in its codec's own container.)
                copy = partial.with_name(f".{uuid.uuid4().hex}.part")
                try:
                    await anyio.to_thread.run_sync(_out_of_matroska, partial, copy)
                    await anyio.Path(copy).replace(partial)
                except (OSError, subprocess.SubprocessError) as exc:
                    return (
                        "its tags do not fit in Matroska, nor its audio elsewhere "
                        f"({type(exc).__name__})"
                    )
                finally:
                    with anyio.CancelScope(shield=True):
                        await anyio.Path(copy).unlink(missing_ok=True)
                suffix = await anyio.to_thread.run_sync(
                    functools.partial(audio_suffix, partial, every=True)
                )
            if suffix is None:
                return "delivered data is not a supported audio file"
            suffix, failure = await self._one_piece(partial, suffix, row["duration_ms"])
            if failure is not None:
                return failure
            if length and remux is None:
                failure = await anyio.to_thread.run_sync(
                    self._other_length, partial, row["duration_ms"]
                )
                if failure is not None:
                    return failure
            final = partial.with_name(f"{uuid.uuid4().hex}{suffix}")
            await anyio.Path(partial).rename(final)
            try:
                # Inspection and local conversion can take time. A surviving caller must
                # still accept placement once they finish, before the engine starts its write.
                await report(progress, "verifying")
                await self.engine.replace_with_delivered(row["song_id"], final)
            except ReplaceError as exc:
                return str(exc)
            # (A play going on keeps its link: its later ranges stay on its file.)
            self.deliverer.in_library(track_of(row))
            return None
        finally:
            with anyio.CancelScope(shield=True):  # (also when cancelled)
                await anyio.Path(partial).unlink(missing_ok=True)
                if final is not None:
                    await anyio.Path(final).unlink(missing_ok=True)

    def _other_length(self, path: Path, duration_ms: int) -> str | None:
        """Why the audio at ``path`` is another recording than the catalog's - its length
        differs by more than the length tolerance - or None (also when it tells none)."""
        try:
            audio = mutagen.File(path)
        except mutagen.MutagenError:
            audio = None
        length = getattr(getattr(audio, "info", None), "length", None)
        if audio is None:  # (Matroska: as ffprobe reads it)
            probed = _probe(path) or {}
            try:
                length = float(probed.get("format", {}).get("duration") or 0)
            except (TypeError, ValueError):
                length = None
        return self._another_length(float(length or 0), duration_ms)

    async def keep_streamed(self, track: Track, path: Path, remux: str | None) -> str | None:
        """Audio a client was streamed in full (``delivery/retain.py``), in ``path`` (a file
        of the work folder, gone afterwards), put in place of the song's placeholder as a
        download's is - under the song's lock, its row looked at again there: only while
        it is a placeholder of this catalog track, never over audio another fetch put in
        place meanwhile. Nothing is asked of the add-ons. ``NOT_YET``: the song has no
        placeholder (yet: a catalog song played before it is in the library; the file is
        left as it is). Else a failure reason, or None."""
        row = await self.store.fetchone(
            "SELECT * FROM placeholders WHERE song_id = ?", [track.song_id]
        )
        if row is None and track.ref is not None:
            row = await self.store.fetchone(
                "SELECT * FROM placeholders WHERE track_ref = ?", [track.ref]
            )
        if row is None:
            return NOT_YET
        if track.ref is not None and str(row["track_ref"]) != track.ref:
            return "another catalog song now has its ID"
        song_id = str(row["song_id"])
        async with self._hold(song_id):
            try:
                row = await self.settled(song_id)
            except Unsettled:
                return "an interrupted swap of its file is not put right yet"
            if row is None:
                return "not a placeholder any more"
            if row["state"] != "placeholder":
                return "its audio is in place already"
            if refused := await self.engine.refused():
                return refused
            # (Every container Navidrome reads Shijhon's tags in: the add-on's format kept.)
            failure = await self._put_in_place(row, path, remux, length=True, every=True)
            if failure is None and self.expiry is not None:
                done = await self.settled(song_id)
                self.expiry.delivered(self._size(done["path"]) if done else 0)
            return failure

    async def in_one_piece(self, song_id: str, *, unused_since: float) -> bool:
        """Delivered audio from before, in a container Navidrome reads no length from
        (``needs_one_piece``), replaced by the same audio in one piece (``_one_piece``),
        whatever its length - it is the song's audio already (one that is not the catalog's
        is logged) -, under the song's lock: never beside another fetch of it. Only if it was
        not used after ``unused_since`` (``PlaceholderEngine.redeliver``). Whether it was
        replaced; raises ``ReplaceError`` when it could not be."""
        async with self._hold(song_id):
            try:
                row = await self.settled(song_id)
            except Unsettled:
                raise ReplaceError("an interrupted swap of its file is not put right yet") from None
            if row is None or row["state"] != "delivered" or row["import_item"]:
                return False
            source = self.engine.layout.absolute(row["path"])
            if not await anyio.to_thread.run_sync(needs_one_piece, source):
                return False
            self.work_dir.mkdir(parents=True, exist_ok=True)
            partial = self.work_dir / f".{uuid.uuid4().hex}.part"
            final: Path | None = None
            try:
                await anyio.to_thread.run_sync(shutil.copyfile, source, partial)
                found = await anyio.to_thread.run_sync(
                    functools.partial(audio_suffix, partial, every=True)
                )
                if found is None:
                    raise ReplaceError("its file is not audio Shijhon reads")
                suffix, failure = await self._one_piece(
                    partial, found, row["duration_ms"], checked=False
                )
                if failure is not None:
                    raise ReplaceError(failure)
                other = await anyio.to_thread.run_sync(
                    self._other_length, partial, row["duration_ms"]
                )
                if other is not None:
                    log.warning("the delivered audio of %s may be %s", song_id, other)
                final = partial.with_name(f"{uuid.uuid4().hex}{suffix}")
                await anyio.Path(partial).rename(final)
                return await self.engine.redeliver(song_id, final, unused_since=unused_since)
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.Path(partial).unlink(missing_ok=True)
                    if final is not None:
                        await anyio.Path(final).unlink(missing_ok=True)

    async def fetch(self, track: Track, minimum: Minimum | None = None) -> Path | str:
        """The add-ons' whole audio of ``track`` in a new file of the work folder (the
        caller removes it), or why not - bounded as a download is (its size limit and
        timeout, never a source that asks not to be used for downloads). Nothing is put in
        place: a person's own imported song is upgraded with it (``music_imports``).
        ``minimum``: an add-on whose audio is below it is passed over for the next one,
        as for the play the upgrade follows."""
        accept = None if minimum is None else functools.partial(refusal, minimum)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        partial = self.work_dir / f".{uuid.uuid4().hex}.part"
        kept = False
        try:
            try:
                with anyio.fail_after(self.timeout):
                    self.fetches += 1
                    with pacing.urgent(pacing.QUEUED):
                        opened = await self.deliverer.open(
                            track, None, resume=False, heard=False, download=True,
                            accept=accept,
                        )  # fmt: skip
                    size = 0
                    try:
                        if not whole(opened) or opened.body is None:
                            return f"source answered HTTP {opened.status} to a full request"
                        async with await anyio.open_file(partial, "wb") as out:
                            async for chunk in opened.body:
                                size += len(chunk)
                                if size > self.max_bytes:
                                    return "delivered file is too large"
                                await out.write(chunk)
                    finally:
                        await opened.close()
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
            if opened.remux is not None:
                failure = await self._copied_out(partial, opened.remux, track.duration_ms)
                if failure is not None:
                    return failure
            suffix = await anyio.to_thread.run_sync(lambda: audio_suffix(partial, every=True))
            if suffix is None:
                return "delivered data is not a supported audio file"
            suffix, failure = await self._one_piece(partial, suffix, track.duration_ms)
            if failure is not None:
                return failure
            final = partial.with_name(f"{uuid.uuid4().hex}{suffix}")
            partial.rename(final)
            kept = True
            return final
        finally:
            if not kept:
                partial.unlink(missing_ok=True)

    async def _copied_out(
        self, partial: Path, kind: str, duration_ms: int, *, checked: bool = True
    ) -> str | None:
        """A fragmented MP4 fetched into ``partial`` - a DASH link's file served at once (an
        MP4 of its segments), or a file in fragments an add-on's link gave -, its audio
        copied out in its place as a join's is - a native FLAC, an MP3 or a fast-start M4A,
        whose length Navidrome reads - when that length is the catalog's (within the length
        tolerance; ``checked`` False: whatever it is). A failure reason, or None."""
        dash = self.deliverer.dash
        ffmpeg = dash.ffmpeg if dash is not None else ffmpeg_path()
        if not ffmpeg:
            return "ffmpeg was not found"
        copy = partial.with_name(f".{uuid.uuid4().hex}.part")
        try:
            length = await copy_out(ffmpeg, partial, copy, kind)
            if checked and (other := self._another_length(length, duration_ms)) is not None:
                return other
            copy.replace(partial)
        except DashError as exc:
            return f"its audio could not be copied out of its fragments ({exc})"
        finally:
            copy.unlink(missing_ok=True)
        return None

    def _another_length(self, length: float, duration_ms: int) -> str | None:
        """Why audio of ``length`` seconds is another recording than the catalog's: it is
        off by more than the length tolerance (None: it is not, or nothing tells)."""
        settings, wanted = self.deliverer.settings, duration_ms / 1000
        tolerance = max(
            settings.length_tolerance_seconds, settings.length_tolerance_percent / 100 * wanted
        )
        if not length or tolerance <= 0 or wanted <= 0 or abs(length - wanted) <= tolerance:
            return None
        return f"another recording ({length:.1f}s, the catalog's {wanted:.1f}s)"

    async def _one_piece(
        self, partial: Path, suffix: str, duration_ms: int, *, checked: bool = True
    ) -> tuple[str, str | None]:
        """Audio in ``partial`` in a container Navidrome reads no length from (it lists the
        song as 0:00, and apps cannot seek in it) copied as it is - nothing encoded anew -
        into one that tells it: a fragmented MP4 out of its fragments as a DASH song's is
        (``_copied_out``: its length the catalog's, unless not ``checked``), Ogg FLAC into a
        native FLAC. (Its suffix now, a failure reason.)"""
        fragmented = suffix == ".m4a" and await anyio.to_thread.run_sync(_fragmented, partial)
        kind = await anyio.to_thread.run_sync(_kind_of, partial) if fragmented else None
        if fragmented and kind is not None:
            failure = await self._copied_out(partial, kind, duration_ms, checked=checked)
        elif fragmented:  # (several audio tracks: all of them, in an ordinary MP4)
            copy = partial.with_name(f".{uuid.uuid4().hex}.part")
            failure = None
            try:
                length = await anyio.to_thread.run_sync(_every_track, partial, copy)
                if checked:
                    failure = self._another_length(length, duration_ms)
                if failure is None:
                    await anyio.Path(copy).replace(partial)
            except (OSError, subprocess.SubprocessError) as exc:
                failure = (
                    f"its tracks could not be copied out of their fragments ({type(exc).__name__})"
                )
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.Path(copy).unlink(missing_ok=True)
        elif suffix == ".oga":
            copy = partial.with_name(f".{uuid.uuid4().hex}.part")
            failure = None
            try:
                await anyio.to_thread.run_sync(_out_of_ogg, partial, copy)
                await anyio.Path(copy).replace(partial)
            except (OSError, subprocess.SubprocessError) as exc:
                failure = f"its Ogg FLAC could not be copied into a FLAC ({type(exc).__name__})"
            finally:
                with anyio.CancelScope(shield=True):
                    await anyio.Path(copy).unlink(missing_ok=True)
        else:
            return suffix, None
        if failure is not None:
            return suffix, failure
        found = await anyio.to_thread.run_sync(functools.partial(audio_suffix, partial, every=True))
        if found is None:
            return suffix, "no audio after its copy into one piece"
        return found, None

    async def _owned_stands_in(self, user: str | None, row: Any) -> bool:
        check = self.stands_in
        return check is None or await check(user, str(row["backing_song_id"]))

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

    async def _place_owned(
        self, row: Any, source: Path, *, progress: Progress | None = None
    ) -> str | None:
        try:
            await report(progress, "verifying")
            await self.engine.replace_with_delivered(row["song_id"], source)
        except ReplaceError as exc:
            return str(exc)
        return None
