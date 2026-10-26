"""Interception of placeholder audio for Navidrome 0.64.2's routes.

For a placeholder song (and only then) Shijhon does work, so it checks credentials first:

- ``stream`` (GET, POST, HEAD, with or without ``.view``): streamed from add-ons; requests
  for another format or a lower bitrate that the delivered audio needs converting for go
  download-first, then Navidrome transcodes the real file (one the audio already meets, or
  an offset alone, is a plain stream, as Navidrome serves such a file as it is);
- ``download``, ``getTranscodeDecision``, ``getTranscodeStream``, ``jukeboxControl``
  (``set``/``add``) and share links (``/share/s/…``, ``/share/d/…``): download-first,
  then forwarded - except a transcode decision at original quality that Navidrome answers
  with direct play, and a transcode stream with such a decision: plain streams;
- a placeholder backed by an owned recording is served as the owned song; share links
  carry a signed song ID that cannot be swapped, so there a copy of the owned file is put
  in place;
- placeholders already replaced by delivered audio are Navidrome's to serve - but a play
  that began at the add-ons keeps its file for its later ranges while the song's link
  is kept (the library's copy is another file);
- a song whose file (owned, or delivered audio) is below the listener's minimum quality
  plays from the add-ons when they have audio that meets it (``_from_addons``): a
  ``stream``, a ``download``, and a transcode stream whose decision at original quality
  was direct play (``_below_minimum``).

A catalog song that is not in the library yet is played from the add-ons without being
committed (``catalog_play``): the client's "now playing" report, or another action,
commits it. Requests for its native ID after the commit continue that play (same routing
and source). A request that needs the placeholder (another format or a lower bitrate that
its audio needs converting for) commits first.

Downloads of whole albums, artists or playlists that would include silent placeholders are
refused. Looking that up is work, so it happens only after the credential check.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx

from shijhon.catalog.model import CatalogTrack
from shijhon.delivery import pacing
from shijhon.delivery.ahead import AheadGate
from shijhon.delivery.download_first import (
    DownloadFirst,
    Turn,
    Unsettled,
    catalog_track,
    track_of,
    whole,
)
from shijhon.delivery.limits import Limited, UserLimits
from shijhon.delivery.listening import Listener
from shijhon.delivery.playback import (
    Accept,
    ByteRange,
    Deliverer,
    Departure,
    NoSource,
    Opened,
    PinBroken,
    Track,
)
from shijhon.delivery.quality_gate import LibrarySong, QualityGate, refusal
from shijhon.delivery.warm import WarmAhead
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.backing import OwnedRecordings
from shijhon.proxy.app import Forward, HandlerResult, Reply, RequestContext
from shijhon.proxy.auth import Caller, CheckFailed
from shijhon.proxy.params import RestCall, header
from shijhon.proxy.responses import music_send, not_implemented, subsonic_error
from shijhon.proxy.upstream import (
    Receive,
    Scope,
    Send,
    Upstream,
    relay,
    request_headers,
    response_headers,
    send_bytes,
    send_stream,
)
from shijhon.quality import ANY, Minimum
from shijhon.store import Store
from shijhon.views.answers import library_answer
from shijhon.views.ids import ABSENT

log = logging.getLogger(__name__)

# Removed from the share validation request: Navidrome must answer 200 for a valid link.
_CONDITIONAL = {
    b"range",
    b"if-range",
    b"if-match",
    b"if-none-match",
    b"if-modified-since",
    b"if-unmodified-since",
}
MAX_SHARE_TRACKS = 20
MAX_SHARE_COLLECTIONS = 20
# Listeners' latest plays of songs remembered (past it: the least recently asked for go) -
# more than the songs' links kept (``playback.MAX_PINS``), which a play's file needs too.
MAX_PLAYS = 20000
# Direct-play decisions of songs below their listener's minimum remembered (``_direct``).
MAX_DECISIONS = 4096
# How long a request for a new placeholder being written waits for its row.
WRITE_WAIT_SECONDS = 15.0
# How long an archive waits for Navidrome's answer to start while new placeholders wait for
# it (Navidrome lists the archive's songs before its first byte).
LISTING_ANSWER_SECONDS = 20.0
# Navidrome's HTTP error answer (Go's http.Error; getTranscodeStream also sets nosniff).
_HTTP_ERROR = [
    (b"content-type", b"text/plain; charset=utf-8"),
    (b"x-content-type-options", b"nosniff"),
]


def requested_processing(call: RestCall) -> tuple[str, int] | None:
    """The format and bitrate (kbit/s, 0: none) a stream asks for, when Navidrome may have to
    convert real audio for it; None when Navidrome 0.64.2 serves the file as it is whatever it
    is: ``format=raw`` whatever bitrate or offset it names, no format and no bitrate -
    an offset alone applies only to a conversion. Either way a plain stream, not paced
    by the downloads' limits."""
    fmt = (call.get("format") or "").lower()
    if fmt == "raw":
        return None
    try:
        bitrate = max(0, int(call.get("maxBitRate") or 0))
    except ValueError:  # Navidrome reads it as none
        bitrate = 0
    if not fmt and not bitrate:
        return None
    return fmt, bitrate


def converted(
    wanted: tuple[str, int], kind: str | None, kbps: int | None, downsampling: bool = True
) -> bool:
    """Whether Navidrome 0.64.2 would convert audio of this format (as it names a file of it)
    and bitrate for a request of ``wanted`` (format, bitrate) - as it decides for a file: the
    requested format is the file's own and its bitrate at most the requested one (or none
    requested), or no format and at most the bitrate - or any bitrate, when Navidrome has no
    downsampling format (``downsampling`` False): served as it is. What the first
    bytes did not tell counts as converted (download-first, as before)."""
    fmt, bitrate = wanted
    if fmt and fmt != kind:
        return True
    if not fmt and not downsampling:
        return False
    if not bitrate:
        return kind is None
    return kbps is None or kbps > bitrate


class _NoTurn(NoSource):
    """No routing turn among the user's lookups came in time (or the client left meanwhile)."""


class _BeingWritten(Exception):
    """A new placeholder whose write did not end in time: never forwarded to Navidrome,
    which would serve its silence as an owned song's audio."""


@dataclass
class _AsIs:
    """What a request for a format or bitrate gets: ``reply``, a plain stream (``served``:
    of audio opened) or its failure, or None: download-first (``resume``: continuing a
    routing that found no audio); ``at``: when its start was noted."""

    reply: Reply | None = None
    at: float | None = None
    resume: bool = False
    served: bool = False


@dataclass
class _Kept:
    """What a stream's first bytes left for its download-first fetch in the same
    request: the user's turn it took, and the answer those bytes were read from - the fetch
    reads on from it, its link not asked again (also across a catalog song's commit)."""

    turn: Turn
    opened: Opened | None = None


_KEPT = "download-first:"  # a request's kept fetch of a song (by its track key)
# A listener's play of a song: (listener, the song's key, the format and bitrate asked for
# - a transcode stream's: ``_TRANSCODE``), and its start: that, and its number.
_PlayKey = tuple[Listener, str, tuple[str, int] | None]
_Start = tuple[_PlayKey, int]
# A transcode stream's play (``getTranscodeStream``): one of its own, which goes on only
# with the decision it was accepted with (``Interceptor._decided``).
_TRANSCODE = ("transcode", -1)


def _processing(call: RestCall) -> tuple[str, int] | None:
    """A play's processing (``_PlayKey``): the format and bitrate a stream asks for
    (``requested_processing``); a transcode stream's own (``_TRANSCODE``)."""
    if call.name == "getTranscodeStream":
        return _TRANSCODE
    return requested_processing(call)


@dataclass
class _Opening:
    """A plain stream's opening: ``open`` routes (or uses the song's link), ``starts``: at
    byte zero, ``at``: when its start was noted."""

    open: Callable[[Departure], Awaitable[Opened]]
    starts: bool
    at: float


class Interceptor:
    def __init__(
        self,
        store: Store,
        deliverer: Deliverer,
        download_first: DownloadFirst,
        navidrome: NavidromeService,
        upstream: Upstream,
        *,
        warm: WarmAhead | None = None,
        ahead: AheadGate | None = None,
        recordings: OwnedRecordings | None = None,
        limits: UserLimits | None = None,
        quality: QualityGate | None = None,
    ) -> None:
        self.store = store
        # Each listener's minimum quality: a file below it plays from the add-ons
        # (``delivery/quality_gate.py``).
        self.quality = quality
        # After a play of a song whose file is below the listener's minimum went to the
        # add-ons: the listener's name and the song (an upgrade of their own import).
        self.upgrade_after: Callable[[str, LibrarySong], None] | None = None
        self.limits = limits  # per user: songs looked up at the add-ons at once
        # Recordings the owner has on other albums: a catalog song plays that file.
        self.recordings = recordings
        self.deliverer = deliverer
        self.download_first = download_first
        self.engine = download_first.engine
        self.navidrome = navidrome
        self.upstream = upstream
        self.warm = warm
        self.ahead = ahead  # a client's fetches ahead of the song it plays
        # A use of an old ID found here (a conversion its audio needs): adds its
        # release back; True when it is back (``cleanup.OldIds.use``).
        self.use_old_id: Callable[[RestCall, RequestContext, str], Awaitable[bool]] | None = None
        self._jukebox: tuple[float, bool] | None = None  # (valid until, enabled)
        self._downsampled: tuple[float, bool] | None = None  # (valid until, it converts)
        # Each listener's latest play of a song (its key), as a plain stream or for a
        # format and bitrate (``requested_processing``): when it began (a number) and
        # whether at the add-ons. Once the song's audio is in the library, a play that
        # began there keeps its file for its later ranges while the song's link is kept
        # (``_on_its_file``). The newest last. Changed only through ``_set_play`` and
        # ``_drop_play``, which keep ``_at_addons``.
        self._plays: dict[_PlayKey, tuple[int, bool]] = {}
        self._play_numbers = itertools.count()
        # The songs (keys) with plays among them that began at the add-ons, and how many:
        # such a song's requests are looked at even while no one can have a minimum
        # (``_going_on``).
        self._at_addons: dict[str, int] = {}
        # Navidrome's latest direct-play decisions at original quality for songs whose
        # file is below the listener's minimum, by (listener, client, token): the formats
        # the client plays as it is (``_below_minimum``) - for new plays only. The newest
        # last.
        self._direct: dict[tuple[str, str, str], DirectPlay] = {}
        # A transcode stream's play from the add-ons (``_TRANSCODE``): its number, the
        # decisions (tokens) it goes on with and the client profile they were accepted
        # with (``_from_addons``). Kept with its play, whatever later decisions say.
        self._decided: dict[_PlayKey, tuple[int, frozenset[str], DirectPlay]] = {}

    def handlers(self) -> dict[str, Any]:
        return {
            "stream": _guarded(self.stream),
            "download": _guarded(self.download),
            "getTranscodeDecision": _guarded(self.transcode),
            "getTranscodeStream": _guarded(self.transcode),
            "jukeboxControl": _guarded(self.jukebox),
        }

    async def placeholder(self, song_id: str | None, ctx: RequestContext | None = None) -> Any:
        """The placeholder's row; None for any other song. While new placeholders are being
        written, a song that is one of them - scanned before its row is recorded, or
        left half written by a stop - waits for its row, the caller's credentials checked
        first (``ctx``): forwarded, Navidrome would serve its silence as an owned song's
        audio. Raises :class:`_BeingWritten` when its write does not end in time."""
        if not song_id:
            return None
        row = await self._row(song_id)
        if row is not None or not self.engine.writing():
            return row
        if ctx is not None and await ctx.caller() is None:
            return None  # Navidrome answers with its own authentication error
        if await self.engine.written(song_id, wait=WRITE_WAIT_SECONDS) is False:
            raise _BeingWritten(song_id)
        # Read again: its write may have ended meanwhile (None: not a placeholder, or taken
        # out again - Navidrome finds no file).
        return await self._row(song_id)

    async def _row(self, song_id: str) -> Any:
        return await self.store.fetchone("SELECT * FROM placeholders WHERE song_id = ?", [song_id])

    async def silent_among(self, song_ids: list[str], *, state: str = "placeholder") -> list[str]:
        """The songs among ``song_ids`` that are still silent placeholders (or in another
        ``state``: delivered), one query per 500."""
        found: list[str] = []
        for start in range(0, len(song_ids), 500):
            chunk = song_ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = await self.store.fetchall(
                f"SELECT song_id FROM placeholders WHERE state = ?"  # noqa: S608
                f" AND song_id IN ({marks})",
                [state, *chunk],
            )
            found += [row["song_id"] for row in rows]
        return found

    @staticmethod
    def _swap(call: RestCall, key: str, mapping: dict[str, str]) -> Forward:
        return Forward(call.rewritten(lambda k, v: mapping.get(v) if k == key else None))

    # --- stream ----------------------------------------------------------------------

    async def stream(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        ident = call.get("id") or ""
        engine = self.engine
        # An old ID of a release taken out first: its record plays it, also while an
        # add-back of it is under way or was left half done - never waited for here.
        row = await self._row(ident) if ident else None
        if row is None and not engine.removed_song(ident):
            row = await self.placeholder(ident, ctx)
        if row is None and (ref := engine.removed_song(ident)):
            caller = await ctx.caller()  # before any work for it
            if caller is None:
                return None  # Navidrome answers with its own credential error
            # (an ID the owner's own song has now drops the record: Navidrome's song)
            record = await engine.removed_row(ident) if await engine.still_removed(ident) else None
            if record is not None:
                return await self._removed(call, ctx, caller, record)
            # Added back, or its release made anew, meanwhile: once that is done (the
            # release's lock), a placeholder again - never Navidrome's silent file before
            # (within the client's wait cap).
            with anyio.move_on_after(self.deliverer.settings.max_wait_seconds) as waited:
                async with engine.lock_for(ref):
                    row = await self.placeholder(ident)
            if waited.cancelled_caught:
                return subsonic_error(call, 0, "audio unavailable: its album is being added")
        if row is None:
            if (going := await self._going_on(call, ctx, ident)) is not None:
                return going  # a play from the add-ons: its later ranges stay on their file
            low = await self._below(ctx, ident)
            if low is not None:  # its file is below the listener's minimum: the add-ons
                caller, song, minimum = low
                return await self._from_addons(
                    call, ctx, caller, song.track(), minimum, upgrade=song
                )
            # An owned song: its start still tells fetches ahead apart.
            await self._note_start(call, ctx, call.get("id"))
            # (New placeholders written while that was noted - a credential check can take
            # a moment: looked at again, so that the last look comes just before Navidrome
            # gets the request.)
            if ident and engine.writing() and not engine.removed_song(ident):
                row = await self.placeholder(ident, ctx)
            if row is None:
                return None
        caller = await ctx.caller()  # before any work for the placeholder
        if caller is None:
            return None  # Navidrome answers with its own authentication error
        return await self._placeholder_stream(call, ctx, caller, row)

    async def _placeholder_stream(
        self, call: RestCall, ctx: RequestContext, caller: Caller, row: Any,
        as_is: _AsIs | None = None,
    ) -> HandlerResult:  # fmt: skip
        """A placeholder's stream (credentials checked). ``as_is``: what its request for a
        format or bitrate was found to need already (an old ID's, just added back)."""
        row = await self._settled(row)
        if row is None:
            return None
        who, track = (caller.username, call.client), track_of(row)
        start = self._starting(call, ctx, who, track.key)
        delivered = row["state"] == "delivered"
        if delivered and self._on_its_file(call, ctx, who, track):
            # A play that began at the add-ons, its audio put in the library meanwhile: its
            # later ranges stay on the file it plays, never at the same offsets of
            # Navidrome's copy (another file).
            return self._serve(call, track, ctx.head, who, start=start)
        backing = None if delivered else await self._backing(row)
        if self.quality is not None and (delivered or backing):
            minimum = await self.quality.minimum(caller.username)
            heard = str(row["song_id"]) if delivered else str(backing)
            if minimum != ANY and await self.quality.below(caller.username, heard) is not None:
                # Its file (delivered audio, or the owned recording it plays) is below the
                # listener's minimum: the add-ons, the file only when they have nothing.
                if delivered or requested_processing(call) is None:
                    fallback = (
                        None if delivered else self._swap(call, "id", {row["song_id"]: heard})
                    )
                    return await self._from_addons(call, ctx, caller, track, minimum, fallback)
                backing = None  # converted: download-first takes the add-ons' audio
        if delivered or backing:
            # Played from a file: its start still tells fetches ahead apart.
            await self._note_start(call, ctx, track.key)
            if delivered:
                return None
            assert backing is not None
            return self._swap(call, "id", {row["song_id"]: backing})
        wanted = requested_processing(call)
        if wanted is not None:
            if as_is is None:
                as_is = await self._as_is(call, ctx, track, who, wanted, start)
            if as_is.reply is not None:
                return as_is.reply  # the delivered audio already meets it
            kept: _Kept | None = ctx.kept.pop(_KEPT + track.key, None)
            failure = await self.download_first.ensure(
                row["song_id"], caller.username,
                playing=self._playing(call, who, track, as_is.at), resume=as_is.resume,
                turn=kept.turn if kept else None, handed=kept.opened if kept else None,
            )  # fmt: skip
            if failure is None:
                return None
            if as_is.resume:  # it continued a routing that found no audio: no third one
                return subsonic_error(call, 0, f"audio unavailable: {failure}")
            log.info("download-first failed (%s); streaming the source format", failure)
            # Its routing continues: the source it cut short first, never the ones it is
            # done with.
            return self._serve(call, track, ctx.head, who, resume=True, start=start)
        return self._serve(call, track, ctx.head, who, start=start)

    async def _removed(
        self, call: RestCall, ctx: RequestContext, caller: Caller, row: Any
    ) -> HandlerResult:
        """An old song ID of a release the cleanup took out, streamed: played from the
        add-ons as its record has the song, adding nothing back - as a catalog song not in
        the library plays: also another format or bitrate its audio already
        meets. One its audio needs converting for is a use: the release is added back first
        (``use_old_id``), then it goes the placeholder's way (download-first); one that could
        not be added back plays here all the same, in the source format. A backing owned
        recording plays that file."""
        who = (caller.username, call.client)
        start = self._starting(call, ctx, who, track_of(row).key)
        backing = await self._backing(row)
        if backing and not await self._stands_in(caller, backing):
            if requested_processing(call) is None:
                minimum = await self._minimum(caller)
                fallback = self._swap(call, "id", {str(row["song_id"]): backing})
                return await self._from_addons(call, ctx, caller, track_of(row), minimum, fallback)
            backing = None  # a conversion goes the placeholder's way (download-first)
        if backing:
            await self._note_start(call, ctx, track_of(row).key)
            return self._swap(call, "id", {str(row["song_id"]): backing})
        wanted = requested_processing(call)
        if wanted is not None:
            as_is = await self._as_is(call, ctx, track_of(row), who, wanted, start)
            if as_is.reply is not None:
                return as_is.reply  # no converting: a plain stream, nothing added back
            use = self.use_old_id
            song = str(row["song_id"])
            if not (as_is.resume or ctx.head or use is None) and await use(call, ctx, song):
                back = await self.placeholder(song)  # a placeholder again
                if back is None:
                    return None  # (added back under another ID: Navidrome's answer for this)
                return await self._placeholder_stream(call, ctx, caller, back, as_is)
            return self._serve(call, track_of(row), ctx.head, who, resume=as_is.resume, start=start)
        return self._serve(call, track_of(row), ctx.head, who, start=start)

    async def catalog_play(
        self, call: RestCall, ctx: RequestContext, track: CatalogTrack
    ) -> HandlerResult:
        """A catalog song that is not in the library, played (credentials checked): served
        from the add-ons, committing nothing - or, when the owner has its recording
        on another album, that file through Navidrome. None when the request needs the
        placeholder - another format or a lower bitrate its audio needs converting
        for: the caller commits first."""
        caller = await ctx.caller()
        if caller is None:
            return None
        who = (caller.username, call.client)
        start = self._starting(call, ctx, who, catalog_track(track).key)
        if self.recordings is not None and (ident := call.get("id")):
            try:
                owned = await self.recordings.find(track)
            except Exception as exc:  # the add-ons, then
                log.info("owned recording lookup failed: %s", type(exc).__name__)
                owned = None
            if owned is not None and not await self._stands_in(caller, owned):
                if requested_processing(call) is None:
                    minimum = await self._minimum(caller)
                    fallback = self._swap(call, "id", {ident: owned})
                    played = catalog_track(track)
                    return await self._from_addons(call, ctx, caller, played, minimum, fallback)
                owned = None  # converted: committed first (its placeholder is gated alike)
            if owned is not None:
                played = catalog_track(track)
                await self._note_start(call, ctx, played.key, played)
                return self._swap(call, "id", {ident: owned})
        wanted = requested_processing(call)
        if wanted is not None:  # needs converting: None (committed first, download-first)
            as_is = await self._as_is(call, ctx, catalog_track(track), who, wanted, start)
            if as_is.reply is None and as_is.resume:
                # No audio to tell by: the source-format stream continues that
                # routing, committing nothing.
                played = catalog_track(track)
                return self._serve(call, played, ctx.head, who, resume=True, start=start)
            return as_is.reply
        return self._serve(call, catalog_track(track), ctx.head, who, start=start)

    # --- each listener's minimum quality (delivery/quality_gate.py) -------------------

    async def _minimum(self, caller: Caller) -> Minimum:
        return ANY if self.quality is None else await self.quality.minimum(caller.username)

    async def _stands_in(self, caller: Caller, song_id: str) -> bool:
        """Whether the file of ``song_id`` may play for a catalog song (or a placeholder)
        this listener plays: not below their minimum."""
        return self.quality is None or await self.quality.stands_in(caller.username, song_id)

    async def _below(
        self, ctx: RequestContext, ident: str
    ) -> tuple[Caller, LibrarySong, Minimum] | None:
        """For a song with its own file (no placeholder): the caller, the song and their
        minimum when its file is below that minimum; None otherwise - Navidrome's file, with
        nothing more asked (no catalog or add-on work); also, when no one can have a
        minimum, no credential check or look-up of the listener."""
        if self.quality is None or not ident or not await self.quality.anyone():
            return None
        try:
            caller = await ctx.caller()
        except CheckFailed:
            return None  # Navidrome answers for its own file
        if caller is None:
            return None
        minimum = await self.quality.minimum(caller.username)
        if minimum == ANY:
            return None
        song = await self.quality.below(caller.username, ident)
        return None if song is None else (caller, song, minimum)

    async def _going_on(
        self, call: RestCall, ctx: RequestContext, ident: str, track: Track | None = None
    ) -> Reply | None:
        """A later range of the listener's play of a song with its own file that began at
        the add-ons - its file was below their minimum then (``_from_addons``): it stays on
        the add-ons' file whatever the file's quality is now (an upgrade, another file,
        another minimum, also no one's minimum at all) - never the same offsets of another
        file. An owned song's play goes on there (its link renewed when it expired); a
        catalog song's delivered audio (``track``) while the song's link is kept
        (``_on_its_file``, as its ``stream``). A transcode stream's later range goes on
        with a decision its play went on with (``_decided``), whatever decisions came
        after; another token is Navidrome's to accept first (``_below_minimum``), and one
        it refuses gets its answer. None otherwise: the request is looked at as a new one
        - a GET at byte zero begins the listener's next play of the song, wherever it is
        served from (``_starting``): the earlier play's file no longer counts. A transcode
        decision is no play. While no one can have a minimum, an owned song with no play
        at the add-ons under way (``_at_addons``, in memory) is Navidrome's at once, its
        credentials not checked."""
        if self.quality is None or not ident or ctx.head or not self._plays:
            return None
        if call.name == "getTranscodeDecision":
            return None
        if track is None and not self._at_addons.get(ident) and not await self.quality.anyone():
            return None  # (no one has a minimum: an owned song's credentials not checked)
        try:
            caller = await ctx.caller()
        except CheckFailed:
            return None  # Navidrome answers for its own file
        if caller is None:
            return None
        who = (caller.username, call.client)
        play = (who, track.key if track else ident, _processing(call))
        if self._starts(call, ctx.head):
            if play in self._plays:
                self._starting(call, ctx, who, play[1])
            return None
        found = self._plays.get(play)
        if found is None or not found[1]:
            return None
        if play[2] == _TRANSCODE and not self._goes_on(play, found[0], call):
            return None
        if track is None:
            song = await self.quality.song(ident)
            track = song.track() if song is not None else Track(ident, None, "", "", 0)
        elif not self._on_its_file(call, ctx, who, track):
            return None
        start = self._starting(call, ctx, who, track.key)
        http_errors = call.name == "getTranscodeStream"
        return self._serve(call, track, ctx.head, who, http_errors=http_errors, start=start)

    async def _from_addons(
        self,
        call: RestCall,
        ctx: RequestContext,
        caller: Caller,
        track: Track,
        minimum: Minimum,
        fallback: HandlerResult = None,
        *,
        plays: Callable[[str | None], bool] | None = None,
        upgrade: LibrarySong | None = None,
        decision: tuple[str, DirectPlay] | None = None,
    ) -> HandlerResult:
        """A song whose file is below the listener's minimum, played from the add-ons under
        the song's own ID - or ``fallback`` (None: Navidrome's file) when they have
        nothing, or only audio that told a quality below the minimum too: audio below it is
        passed over for the next add-on, in the routing's order and budget. Nothing is
        written: a plain stream, or a format and bitrate their audio meets as it is (one it
        needs converting for plays the file, converted by Navidrome). ``plays``: whether
        the app plays audio of a format as it is (a transcode stream's direct play: what it
        does not play is passed over too). A later range stays where its play began: the
        add-ons' audio is another file than the library's. ``upgrade``: a song with its own
        file, whose play from the start, served from the add-ons, may upgrade a person's
        own imported song with their audio afterwards (``music_imports/upgrade.py``).
        ``decision``: a transcode stream's - its token and client profile, accepted by
        Navidrome: its play goes on with it, and with another one of the same profile."""
        who = (caller.username, call.client)
        http_errors = call.name == "getTranscodeStream"  # as Navidrome answers it
        parsed = ByteRange.parse(header(call.headers, b"range") or None)
        if not (parsed is None or parsed.at_zero):
            play = (who, track.key, _processing(call))
            found = self._plays.get(play)
            if found is None or not found[1]:
                return fallback  # a play that began on the file goes on there
            if decision is not None and not self._accept_decision(play, found[0], decision):
                return fallback  # another client profile than its play's: the file's
            start = self._starting(call, ctx, who, track.key)
            return self._serve(call, track, ctx.head, who, http_errors=http_errors, start=start)
        start = self._starting(call, ctx, who, track.key)
        if decision is not None and start is not None:
            self._decided[start[0]] = (start[1], frozenset({decision[0]}), decision[1])

        def accept(kind: str | None, kbps: int | None) -> str | None:
            why = refusal(minimum, kind, kbps)
            if why is None and plays is not None and not plays(kind):
                why = f"the app does not play {kind or 'audio of an unknown format'} as it is"
            return why

        def played() -> None:
            # The add-ons had it well enough: a person's own imported song may be upgraded
            # with their audio after a play from the start.
            later = self.upgrade_after
            if upgrade is not None and later is not None and self._starts(call, ctx.head):
                later(caller.username, upgrade)

        wanted = requested_processing(call)
        if wanted is not None:
            as_is = await self._as_is(call, ctx, track, who, wanted, start, accept=accept)
            if as_is.served:
                played()
            return as_is.reply if as_is.reply is not None else fallback
        opening = self._opening(call, track, ctx.head, who, accept=accept)
        try:
            opened = await opening.open(Departure())
        except (NoSource, PinBroken) as exc:
            log.info("%r: no audio at the add-ons that does (%s); its file plays", track.title, exc)
            return fallback
        except Exception as exc:  # the file, never a 500
            log.warning("add-on audio failed: %s; the file plays", type(exc).__name__)
            return fallback
        played()
        return self._serve(
            call, track, ctx.head, who, http_errors=http_errors, opening=opening, opened=opened,
            start=start,
        )  # fmt: skip

    async def _note_start(
        self, call: RestCall, ctx: RequestContext, key: str | None, track: Track | None = None
    ) -> None:
        """A play of a file (owned, backed or delivered) started at byte zero: noted for the
        classification of fetches ahead, for a verified caller (cached check); a catalog
        song's (``track``) gets warm-ahead of the next ones, as a play from the add-ons."""
        if not key or self.warm is None:
            return
        parsed = ByteRange.parse(header(call.headers, b"range") or None)
        if call.http_method == "HEAD" or not (parsed is None or parsed.at_zero):
            return
        noting = self.ahead is not None and self.ahead.window > 0
        if not noting and track is None:
            return
        try:
            caller = await ctx.caller()
        except CheckFailed:
            return  # nothing noted: Navidrome answers for its own file
        if caller is None:
            return
        who = (caller.username, call.client)
        at = self.warm.listening.started(who, key)
        if track is not None:
            self.warm.started(who, track, at)

    @staticmethod
    def _starts(call: RestCall, head: bool) -> bool:
        """Whether the request begins a play: a GET (not a HEAD) at byte zero."""
        parsed = ByteRange.parse(header(call.headers, b"range") or None)
        return not head and (parsed is None or parsed.at_zero)

    def _starting(
        self, call: RestCall, ctx: RequestContext, who: Listener, key: str
    ) -> _Start | None:
        """The listener's play of the song (as a plain stream, or for its format and
        bitrate) that a GET belongs to, for ``_began`` (None: a HEAD). A GET at byte zero
        begins the next one, wherever it is served from: the earlier play's file no longer
        counts. A later range belongs to the play under way (one that began elsewhere, or
        before a restart, is new)."""
        if ctx.head:
            return None
        play = (who, key, _processing(call))
        found = self._plays.get(play)
        if found is None or self._starts(call, ctx.head):
            found = (next(self._play_numbers), False)
        self._set_play(play, found)
        while len(self._plays) > MAX_PLAYS:
            self._drop_play(next(iter(self._plays)))
        return play, found[0]

    def _set_play(self, play: _PlayKey, found: tuple[int, bool]) -> None:
        """The listener's play of the song is ``found`` (its number, whether it began at
        the add-ons), the newest (the oldest go first)."""
        self._drop_play(play, decided=False)
        self._plays[play] = found
        if found[1]:
            self._at_addons[play[1]] = self._at_addons.get(play[1], 0) + 1

    def _drop_play(self, play: _PlayKey, *, decided: bool = True) -> None:
        """The listener's play of the song is no longer remembered (``decided``: nor the
        decisions it went on with)."""
        found = self._plays.pop(play, None)
        if decided:
            self._decided.pop(play, None)
        if found is not None and found[1]:
            left = self._at_addons.get(play[1], 0) - 1
            if left > 0:
                self._at_addons[play[1]] = left
            else:
                self._at_addons.pop(play[1], None)

    def _goes_on(self, play: _PlayKey, number: int, call: RestCall) -> bool:
        """Whether a transcode stream's later range goes on with the play under way: its
        token one the play went on with - accepted by Navidrome for it, with its profile.
        Kept with the play, whatever decisions came after (the same token with another
        profile in the same second, a bitrate limit, older decisions forgotten): those
        decide the client's next plays (``_direct``)."""
        bound = self._decided.get(play)
        token = call.get("transcodeParams") or ""
        return bound is not None and bound[0] == number and token in bound[1]

    def _accept_decision(
        self, play: _PlayKey, number: int, decision: tuple[str, DirectPlay]
    ) -> bool:
        """Another decision Navidrome accepted for a transcode stream's play under way: the
        play goes on with it too when it has the play's profile."""
        bound = self._decided.get(play)
        token, profile = decision
        if bound is None or bound[0] != number or bound[2] != profile:
            return False
        self._decided[play] = (number, bound[1] | {token}, profile)
        return True

    def _began(self, start: _Start) -> None:
        """The play ``start`` was served from the add-ons (its first bytes are sent) -
        unless the listener began another play of the song meanwhile."""
        play, number = start
        found = self._plays.get(play)
        if found is not None and found[0] == number and not found[1]:
            self._plays[play] = (number, True)  # (in its place)
            self._at_addons[play[1]] = self._at_addons.get(play[1], 0) + 1

    def _on_its_file(
        self, call: RestCall, ctx: RequestContext, who: Listener, track: Track
    ) -> bool:
        """Whether a request for a song whose audio is in the library continues the
        listener's play from the add-ons: a later range (not a HEAD) of a play that began
        there with the same format and bitrate asked for (a plain stream: none), while the
        song has a link that has not expired (``pin_ttl_seconds``, or the link's own
        expiry; also one being renewed after it failed: ``Deliverer.holds``). After that,
        Navidrome's file."""
        if ctx.head or self._starts(call, ctx.head):
            return False
        play = (who, track.key, _processing(call))
        found = self._plays.get(play)
        if found is None or not found[1]:
            return False
        if self.deliverer.holds(track):
            return True
        self._drop_play(play)  # its link expired: Navidrome's file from now on
        return False

    def _playing(
        self, call: RestCall, who: Listener, track: Track, at: float | None = None
    ) -> bool:
        """Whether a download-first stream of the song is the song being played, not one
        the client fetches ahead of it: the song being played never waits
        behind them for a download turn. Without that rule (``ahead_window_seconds`` 0)
        nothing is told apart and every fetch waits its turn. A probe (HEAD) is no play (the
        client saying it plays the song still takes it out of the wait), and a later
        range only of a song being played from the add-ons (before, both went past
        the user's turns whatever they were). ``at``: its start, noted already."""
        if self.warm is None or self.ahead is None or self.ahead.window <= 0:
            return False
        if call.http_method == "HEAD":
            return False
        parsed = ByteRange.parse(header(call.headers, b"range") or None)
        if not (parsed is None or parsed.at_zero):
            return self.deliverer.played(track)
        since = self.warm.listening.started(who, track.key) if at is None else at
        return self.ahead.current(who, track.key, since)

    async def _as_is(
        self,
        call: RestCall,
        ctx: RequestContext,
        track: Track,
        who: Listener,
        wanted: tuple[str, int],
        start: _Start | None = None,
        *,
        accept: Accept | None = None,
    ) -> _AsIs:
        """A stream for a format or bitrate the delivered audio already meets - its own
        format, at or below the bitrate - needs no converting: Navidrome serves such a file
        as it is, so this one is a plain stream from the add-ons. What is known of
        the song's audio decides at once when it needs converting (the play's link, also
        under its catalog track before the commit, or the representation it started
        with); otherwise the song is opened as for a plain stream - at byte zero, or a
        later range of a play known so - and the audio opened decides (a link replaced
        meanwhile too). Needing converting, or not told: download-first, which uses the
        link found - after a routing that found no audio, continuing it; a request
        that got no routing turn goes download-first as it did before.

        At byte zero the song is opened in the user's turn among their fetches, within the
        hour's allowance - a bulk sync's lookups are paced like its downloads - unless
        it is the song being played; its download-first fetch follows in that turn, reading
        on from the answer opened (kept in ``ctx`` for it: its link is not asked again). A
        request served as it is takes nothing from the allowance. ``accept``: the request's
        own test of the audio opened (``Deliverer.open``): what it refuses is passed over
        for the next add-on; none that does is a routing that found no audio."""
        if _KEPT + track.key in ctx.kept:
            return _AsIs()  # opened already in this request (before its commit): fetched
        head = ctx.head
        deliverer = self.deliverer
        pin = deliverer.pinned(track.song_id) or (
            deliverer.pinned(track.ref) if track.ref else None
        )
        known = pin.kind if pin is not None and pin.checked else None
        kbps = pin.kbps if known is not None and pin is not None else None
        if known is None and (seen := deliverer.known(track.song_id, track.ref)) is not None:
            known, kbps = seen.kind, seen.kbps
        # Navidrome's downsampling format matters only when no format is named.
        downsampling = await self._downsampling() if not wanted[0] else True
        if known is not None and converted(wanted, known, kbps, downsampling):
            self._converting(track, wanted, known, kbps)
            return _AsIs()
        parsed = ByteRange.parse(header(call.headers, b"range") or None)
        at_zero = not head and (parsed is None or parsed.at_zero)
        if not at_zero and known is None:
            return _AsIs()  # nothing read to tell by: download-first, as before
        opening = self._opening(call, track, head, who, accept=accept)
        turn: Turn | None = None
        if at_zero:
            playing = self._playing(call, who, track, opening.at)
            turn = await ctx.closing.enter_async_context(
                self.download_first.turn(track.key, who[0], playing=playing)
            )
            if turn.waited:  # a repeat of a request of the song that fetched it: its outcome
                ctx.kept[_KEPT + track.key] = _Kept(turn)
                return _AsIs(at=opening.at)
            # The wait for the turn may have been long: what was so before it is looked at
            # again - the song's audio fetched meanwhile (Navidrome's to serve), its link
            # found, the client playing it now.
            # (Its row, read directly: an old ID of a release taken out has none, and must
            # not wait here for an add-back of it that is pending or was left half done -
            # its record plays it.)
            row = await self._row(track.song_id)
            if row is not None and row["state"] == "delivered":
                ctx.kept[_KEPT + track.key] = _Kept(turn)
                return _AsIs(at=opening.at)
            opening = self._opening(call, track, head, who, at=opening.at, accept=accept)
        try:
            # (In its download turn it is as urgent at the add-ons' limits as that turn.)
            with pacing.urgent(turn.urgency) if turn is not None else contextlib.nullcontext():
                opened = await opening.open(Departure())
        except _NoTurn:  # no routing turn: download-first, as before (it has limits of its own)
            self._keep(ctx, track, turn)
            return _AsIs(at=opening.at)
        except (NoSource, PinBroken) as exc:
            if not at_zero:  # a later range of a play whose link failed: its answer
                return _AsIs(subsonic_error(call, 0, f"audio unavailable: {exc}"), opening.at)
            # No first bytes: download-first continues that routing (the source its wait cap
            # cut short first, then those it did not try) - Navidrome decides then.
            log.info("no audio to tell by (%s); download-first, continuing the routing", exc)
            self._keep(ctx, track, turn)
            return _AsIs(at=opening.at, resume=True)
        except Exception as exc:  # never a 500 for a placeholder
            log.warning("stream failed: %s", type(exc).__name__)
            return _AsIs(subsonic_error(call, 0, "audio unavailable"), opening.at)
        if converted(wanted, opened.kind, opened.kbps, downsampling):
            self._converting(track, wanted, opened.kind, opened.kbps)
            if turn is not None and whole(opened):
                self._keep(ctx, track, turn, opened)  # its fetch reads on from here
            else:  # a later range, a short probe: nothing to fetch on from
                await opened.close()
                self._keep(ctx, track, turn)
            return _AsIs(at=opening.at)
        if at_zero:
            log.info(
                "%r needs no converting (%s at %s kbit/s for %s at %s): streamed as it is",
                track.title,
                opened.kind or "?",
                opened.kbps if opened.kbps is not None else "?",
                wanted[0] or "any format",
                wanted[1] or "any bitrate",
            )
        reply = self._serve(call, track, head, who, opening=opening, opened=opened, start=start)
        return _AsIs(reply, opening.at, served=True)

    @staticmethod
    def _keep(
        ctx: RequestContext, track: Track, turn: Turn | None, opened: Opened | None = None
    ) -> None:
        """The request's download-first fetch of the song follows: in ``turn``, reading on
        from ``opened`` (closed with the request if nothing reads it)."""
        if opened is not None:
            ctx.closing.push_async_callback(opened.close)
        if turn is not None:
            ctx.kept[_KEPT + track.key] = _Kept(turn, opened)

    @staticmethod
    def _converting(
        track: Track, wanted: tuple[str, int], kind: str | None, kbps: int | None
    ) -> None:
        log.info(
            "%r needs converting (%s at %s kbit/s for %s at %s): download-first",
            track.title,
            kind or "?",
            kbps if kbps is not None else "?",
            wanted[0] or "any format",
            wanted[1] or "any bitrate",
        )

    async def _downsampling(self) -> bool:
        """Whether Navidrome converts a request for a lower bitrate that names no format
        (its ``DefaultDownsamplingFormat`` is set, its default "opus"), remembered for ten
        minutes; unknown: yes (the request goes download-first, as before)."""
        now = time.monotonic()
        if self._downsampled is None or self._downsampled[0] < now:
            config = await self.navidrome.config()
            settings = config.get("config") if isinstance(config, dict) else None
            value = (
                settings.get("DefaultDownsamplingFormat") if isinstance(settings, dict) else None
            )
            self._downsampled = (now + 600, value != "")
        return self._downsampled[1]

    async def _settled(self, row: Any) -> Any:
        """A placeholder's row for a verified caller, its use recorded: a placeholder used
        once is never taken out as unused, and delivered audio - not in the middle of
        a revert, its use recorded under the song's lock - stays a while longer."""
        if row["state"] != "delivered":
            await self.download_first.used(row["song_id"])
            return row
        return await self.download_first.settled(row["song_id"], used=True)

    async def _backing(self, row: Any) -> str | None:
        """The owned song a placeholder plays, while it is in the library. While its file is
        missing the add-ons play the track; one Navidrome no longer knows is cleared."""
        if row is None or not row["backing_song_id"]:
            return None
        backing = str(row["backing_song_id"])
        state = "here" if self.recordings is None else await self.recordings.present(backing)
        if state == "here":
            return backing
        if state == "gone":
            await self.store.execute(
                "UPDATE placeholders SET backing_song_id = NULL WHERE backing_song_id = ?",
                [backing],
            )
            log.info("an owned song backing placeholders is gone; the add-ons play them again")
        return None

    def _opening(
        self,
        call: RestCall,
        track: Track,
        head: bool,
        who: Listener,
        *,
        resume: bool = False,
        at: float | None = None,
        accept: Accept | None = None,
    ) -> _Opening:
        """How a plain stream from the add-ons is opened: in the user's turn when it routes,
        a play first and its fetches ahead one at a time; its start is noted
        (``at``: noted already, then). Every plain stream that looks a song up at the
        add-ons takes one of the user's turns: a probe (HEAD) of a song without a
        link one of the lookups', like a GET - as a later range of a song never played; a
        later range of a play whose link is gone one of the plays'. ``accept``: the
        request's own test of the audio (``Deliverer.open``)."""
        range_header = header(call.headers, b"range") or None
        parsed = ByteRange.parse(range_header)
        starts = not head and (parsed is None or parsed.at_zero)
        if at is None:
            at = self.warm.listening.started(who, track.key) if self.warm and starts else 0.0
        cold = self.deliverer.pinned(track.song_id) is None  # it is looked up at the add-ons
        routes = starts and cold
        gate = self.ahead if routes else None
        current = gate is None or gate.current(who, track.key, at)

        async def routed(
            departure: Departure, waited: float = 0.0, purpose: str = "play", playing: bool = False
        ) -> Opened:
            """The routing, in one of the user's turns when it looks the song up (a routing
            limit per user; ``playing``: the song being played, told apart from fetches ahead,
            takes one of the plays' turns - as does a song the client says it plays, also
            while this waits: it is then routed as a play). The wait for a turn
            counts in the client's wait cap and ends with it; a client that left meanwhile
            gets no routing."""
            if not cold or self.limits is None:
                return await self.deliverer.open(
                    track, range_header, head=head, purpose=purpose, waited=waited,
                    departure=departure, resume=resume, accept=accept,
                )  # fmt: skip
            settings = self.deliverer.settings
            cap = settings.max_wait_seconds - waited
            # The client says it plays this song (a report, a saved queue - also one that
            # comes while the lookup waits for its turn): never behind its fetches.
            listening = self.warm.listening if self.warm is not None else None
            said = listening is not None and listening.current_for(who[0]) == track.key
            # A later range of a play whose link is gone is the song being played too; of a
            # song never played, a lookup like a probe's. Either has a seek's time in all,
            # the wait for its turn included.
            later = parsed is not None and not parsed.at_zero
            seeking = later and not head and self.deliverer.played(track)
            first = playing or said or seeking
            if later:
                cap = min(cap, settings.seek_timeout_seconds)
            if first:  # its probes waiting for a turn go with it (the song's one routing)
                self.limits.promote(who[0], track.key)
            try:
                async with self.limits.routing(
                    who[0], wait=cap, queue=not first, key=track.key
                ) as turn:
                    if departure.at is not None:
                        raise _NoTurn(["the client left while waiting for a turn"])
                    if listening is not None and listening.current_for(who[0]) == track.key:
                        purpose = "play"  # (a fetch ahead no more: fallbacks and all)
                        first = True
                    # At the add-ons' limits: the song being played first - also when
                    # the caller set a lower urgency before it was known to be (a queued
                    # stream the client then plays); a probe, or a later range of a song
                    # never played, after every play.
                    outer, level = pacing.current(), None
                    if outer is not None and first:
                        outer.raise_to(pacing.PLAY)
                    elif outer is None and not starts:
                        level = pacing.PLAY if first else pacing.QUEUED
                    budget = max(0.1, settings.seek_timeout_seconds - turn) if later else None
                    with pacing.urgent(level) if level is not None else contextlib.nullcontext():
                        return await self.deliverer.open(
                            track, range_header, head=head, purpose=purpose, budget=budget,
                            waited=waited + turn, departure=departure, resume=resume,
                            accept=accept,
                        )  # fmt: skip
            except Limited as exc:
                log.info("no audio for %r: %s", track.title, exc.reason)
                raise _NoTurn([exc.reason]) from None

        async def open_it(departure: Departure) -> Opened:
            if gate is None:
                return await routed(departure)
            # Only while fetches ahead are told apart does a play as such skip the user's
            # turns (a song the client reports as playing does either way).
            told_apart = gate.window > 0
            if current:  # the song being played: first, routed as always
                async with gate.playing(who):
                    return await routed(departure, playing=told_apart)
            async with gate.turn(who, track.key) as turn:  # a fetch ahead
                purpose = "play" if turn.promoted else "alone" if turn.alone else "ahead"
                return await routed(departure, turn.waited, purpose, turn.promoted and told_apart)

        return _Opening(open_it, starts, at)

    def _serve(
        self,
        call: RestCall,
        track: Track,
        head: bool,
        who: Listener,
        *,
        resume: bool = False,
        http_errors: bool = False,
        opening: _Opening | None = None,
        opened: Opened | None = None,
        start: _Start | None = None,
    ) -> Reply:
        """A plain stream from the add-ons. ``http_errors``: no audio is an HTTP error, not
        a Subsonic one (``getTranscodeStream``, as Navidrome answers it). ``opened``: its
        answer, opened already (by ``opening``). ``start``: the listener's play it belongs to
        (``_starting``), noted as one at the add-ons once its first bytes are sent."""
        if opening is None:
            opening = self._opening(call, track, head, who, resume=resume)
        starts, at = opening.starts, opening.at

        async def reply(receive: Receive, send: Send) -> None:
            # A client that leaves before its first byte is noted (the routing goes on: its
            # result serves the next request for the song).
            departure = Departure()
            outcome: list[Opened | BaseException] = []

            async def watch() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass
                departure.at = self.deliverer.clock()

            if opened is not None:
                outcome.append(opened)
            else:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(watch)
                    try:
                        outcome.append(await opening.open(departure))
                    except Exception as exc:
                        outcome.append(exc)
                    tg.cancel_scope.cancel()
            result = outcome[0]
            if isinstance(result, BaseException) and http_errors:
                if not isinstance(result, (NoSource, PinBroken)):
                    log.warning("stream failed: %s", type(result).__name__)
                await send_bytes(send, 500, b"Internal Server Error\n", _HTTP_ERROR)
                return
            if isinstance(result, (NoSource, PinBroken)):
                await subsonic_error(call, 0, f"audio unavailable: {result}")(receive, send)
                return
            if isinstance(result, BaseException):  # never a 500 for a placeholder
                log.warning("stream failed: %s", type(result).__name__)
                await subsonic_error(call, 0, "audio unavailable")(receive, send)
                return
            if departure.at is not None:  # nobody to send it to
                await result.close()
                return
            if result.capture is not None and result.status in (200, 206):
                # It goes to a client: its bytes are kept, and once the whole file is
                # there it replaces the song's placeholder (``delivery/retain.py``).
                result.capture.accept(track)
            if start is not None and result.status in (200, 206):
                self._began(start)
            if self.warm is not None and starts:
                self.warm.started(who, track, at)
            await send_stream(
                receive, send, result.status, result.headers, result.body, result.close
            )

        return reply

    # --- download-first paths ----------------------------------------------------------

    async def download(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        ident = call.get("id")
        if not ident:
            return None
        row = await self.placeholder(ident, ctx)
        if row is None:
            # Albums, artists and playlists are downloaded as archives; inspecting one is
            # work (also with no placeholder yet: the first may be written while Navidrome
            # lists it).
            if await ctx.caller() is None:
                return None
            song = None
            with contextlib.suppress(NavidromeError):
                song = await self.navidrome.song(ident)
            if song is None:
                return self._archive(call, ident)
            if self.engine.writing():  # (written while Navidrome was asked: looked at again)
                row = await self.placeholder(ident, ctx)
            if row is None:
                if (going := await self._going_on(call, ctx, ident)) is not None:
                    return going
                low = await self._below(ctx, ident)
                if low is not None:  # its file is below the listener's minimum: the add-ons
                    return await self._from_addons(call, ctx, low[0], low[1].track(), low[2])
                return None  # a song that is no placeholder: Navidrome's to serve
        caller = await ctx.caller()  # before any work for the placeholder
        if caller is None:
            return None
        row = await self._settled(row)
        if row is None:
            return None
        if row["state"] == "delivered":
            going = await self._going_on(call, ctx, row["song_id"], track_of(row))
            if going is not None:
                return going
            low = await self._below(ctx, row["song_id"])
            if low is None:
                return None
            return await self._from_addons(call, ctx, caller, track_of(row), low[2])
        if (backing := await self._backing(row)) and await self._stands_in(caller, backing):
            return self._swap(call, "id", {row["song_id"]: backing})
        failure = await self.download_first.ensure(row["song_id"], caller.username, bulk=True)
        if failure is None:
            return None
        return subsonic_error(call, 0, f"download unavailable: {failure}")

    def _archive(self, call: RestCall, ident: str) -> Reply:
        """An album's, artist's or playlist's archive: refused when it would hold silent
        placeholders, else forwarded - with no new placeholders moving into the library
        from the check until Navidrome's answer starts (Navidrome 0.64.2 lists an archive's
        songs before its first byte), and after the writes under way."""

        async def reply(receive: Receive, send: Send) -> None:
            refused: Reply | None = None
            response: httpx.Response | None = None
            async with self.engine.listing(wait=WRITE_WAIT_SECONDS) as ended:
                if not ended:
                    refused = _being_written(call)
                elif await self._collection_with_silence(ident):
                    refused = subsonic_error(
                        call,
                        0,
                        "downloading a whole album, artist or playlist with catalog tracks"
                        " is not supported",
                    )
                else:
                    headers = request_headers(call.headers)
                    response = await self._ask(
                        call.http_method, call.raw_path, call.query, headers, call.body
                    )
            if response is not None:
                await relay(response, receive, send, head=call.http_method == "HEAD")
            else:
                await (refused or _unreachable())(receive, send)

        return reply

    async def _ask(
        self,
        method: str,
        raw_path: bytes,
        query: bytes,
        headers: list[tuple[bytes, bytes]],
        body: bytes | None,
    ) -> httpx.Response | None:
        """Navidrome's answer as far as its headers, as the proxy forwards a request; None:
        unreachable, or no answer within ``LISTING_ANSWER_SECONDS`` (new placeholders wait
        for it)."""
        try:
            with anyio.fail_after(LISTING_ANSWER_SECONDS):
                return await self.upstream.send(method, raw_path, query, headers, body)
        except (httpx.HTTPError, TimeoutError) as exc:
            log.warning("navidrome unreachable: %s", type(exc).__name__)
            return None

    async def _may_hold_placeholders(self, *, delivered: bool = False) -> bool:
        """Whether any silent placeholder (``delivered``: or delivered audio) exists, or new
        placeholders are being written."""
        if self.engine.writing():
            return True
        states = "('placeholder', 'delivered')" if delivered else "('placeholder')"
        found = await self.store.fetchone(
            f"SELECT 1 FROM placeholders WHERE state IN {states} LIMIT 1"  # noqa: S608
        )
        return found is not None

    async def _collection_with_silence(self, ident: str) -> bool:
        """Whether an archive of ``ident`` (an album, an artist, a playlist) would hold a
        silent file: a placeholder's, by its row - or, while writes are unfinished (files
        left half written, swaps a stop interrupted), a file no row tells of. Then the
        collection's songs are listed, a song with an interrupted swap is put right first,
        and what cannot be listed is not forwarded."""
        unfinished = self.engine.left() or bool(self.engine.interrupted())
        album_ids = [ident]
        try:
            album_ids += await self._artist_albums(ident, strict=unfinished)
        except NavidromeError:
            return True
        marks = ",".join("?" * len(album_ids))
        album = await self.store.fetchone(
            "SELECT 1 FROM placeholders p JOIN releases r ON p.release_ref = r.ref"  # noqa: S608
            f" WHERE p.state = 'placeholder' AND (r.album_id IN ({marks})"
            f" OR r.owned_album_id IN ({marks})) LIMIT 1",
            album_ids * 2,
        )
        if album is not None:
            return True
        if unfinished:
            for album_id in album_ids:
                try:
                    listed = await self.navidrome.songs_of_album(album_id)
                except NavidromeError:
                    return True  # not known what it holds: not forwarded
                if await self._unfinished_among(listed):
                    return True
        try:
            songs = await self.navidrome.playlist_song_ids(ident)
        except NavidromeError as exc:
            # Not a playlist (or not visible): nothing to guard - unless that is not known.
            return unfinished and exc.status != 404
        if await self.silent_among(songs):
            return True
        return unfinished and await self._unfinished_among([{"id": s} for s in songs])

    async def _artist_albums(self, ident: str, *, strict: bool) -> list[str]:
        """The albums of the artist ``ident`` (none: not an artist); ``strict``: raises
        :class:`NavidromeError` when Navidrome could not say."""
        if not strict:
            return await self.navidrome.artist_album_ids(ident)
        try:
            artist = await self.navidrome.subsonic("getArtist", [("id", ident)])
        except NavidromeError as exc:
            if exc.code == 70:  # "not found": not an artist
                return []
            raise
        albums = (artist.get("artist") or {}).get("album") or []
        return [str(a["id"]) for a in albums if isinstance(a, dict) and "id" in a]

    async def _unfinished_among(self, songs: list[dict[str, Any]]) -> bool:
        """Whether one of the songs (Navidrome's records: ``id``, and ``path`` when known)
        is a file left half written, or its swap a stop interrupted cannot be put right
        now - or it cannot be told. A song with an interrupted swap is put right first."""
        left, interrupted = self.engine.left_paths(), self.engine.interrupted()
        ids = [str(song["id"]) for song in songs]
        if left and await self._left_among(ids, {str(s["id"]): s for s in songs if "path" in s}):
            return True
        for song_id in ids:
            if song_id in interrupted:
                try:
                    await self.download_first.settled(song_id)
                except Unsettled:
                    return True
        return False

    async def _left_among(
        self, song_ids: list[str], known: dict[str, dict[str, Any]] | None = None
    ) -> bool:
        """Whether one of the songs is a placeholder left half written (no row names it, its
        file may still be there) - or cannot be told from one. Navidrome is asked (for
        the songs not in ``known``) only while such files wait to be taken out."""
        left = self.engine.left_paths()
        if not left:
            return False
        recorded = set(await self.silent_among(song_ids))
        recorded |= set(await self.silent_among(song_ids, state="delivered"))
        for song_id in song_ids:
            if song_id in recorded:
                continue
            song = (known or {}).get(song_id)
            if song is None:
                try:
                    song = await self.navidrome.song(song_id)
                except NavidromeError:
                    return True
            if song is not None and str(song.get("path")) in left:
                return True
        return False

    async def transcode(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """``getTranscodeDecision``/``getTranscodeStream`` (the OpenSubsonic transcoding
        extension): download-first, so that Navidrome decides from the real file and signs
        its decision against it - except at original quality: a decision that
        asks for no bitrate limit and that Navidrome answers with direct play is answered at
        once (the client then streams the file as it is: a plain stream from the add-ons,
        like ``format=raw``), and a transcode stream whose decision was direct play is a
        plain stream. A song with its own file - owned, or a catalog song's delivered
        audio - is Navidrome's, unless the file is below the listener's minimum
        (``_below_minimum``)."""
        if call.get("mediaType") != "song":
            return None  # Navidrome's own answer (only songs; the parameter is required)
        ident = call.get("mediaId") or ""
        row = await self.placeholder(ident, ctx)
        if row is None:
            if (going := await self._going_on(call, ctx, ident)) is not None:
                return going
            low = await self._below(ctx, ident)
            if low is None:
                return None  # Navidrome's file, with nothing more asked
            return await self._below_minimum(call, ctx, low[0], low[1].track(), low[2], low[1])
        caller = await ctx.caller()  # before any work for the placeholder
        if caller is None:
            return None
        row = await self._settled(row)
        if row is None:
            return None
        if row["state"] == "delivered":
            going = await self._going_on(call, ctx, row["song_id"], track_of(row))
            if going is not None:
                return going
            low = await self._below(ctx, row["song_id"])
            if low is None:
                return None
            return await self._below_minimum(call, ctx, caller, track_of(row), low[2])
        if (backing := await self._backing(row)) and await self._stands_in(caller, backing):
            return self._swap(call, "mediaId", {row["song_id"]: backing})
        who = (caller.username, call.client)
        if call.name == "getTranscodeStream" and direct_play(call, row["song_id"]):
            accepted = await self._accepted(call, ctx.head)
            if accepted is None:  # Navidrome could not tell: its error, never the silence
                return _http_error()
            if accepted is False:
                return None  # Navidrome's own answer: an invalid or stale decision (410)
            if accepted is not True:
                return accepted  # Navidrome's Subsonic error, as it gave it
            return self._serve(call, track_of(row), ctx.head, who, http_errors=True)
        if call.name == "getTranscodeDecision" and original_quality(call):
            answer = await self._decision(call)
            if answer is not None:
                return answer
        failure = await self.download_first.ensure(
            row["song_id"], caller.username, playing=self._playing(call, who, track_of(row))
        )
        return None if failure is None else subsonic_error(call, 0, f"audio unavailable: {failure}")

    async def _below_minimum(
        self,
        call: RestCall,
        ctx: RequestContext,
        caller: Caller,
        track: Track,
        minimum: Minimum,
        song: LibrarySong | None = None,
    ) -> HandlerResult:
        """The transcoding extension for a song whose file is below the listener's minimum
        (credentials checked). Navidrome decides from the file and signs its decision, as
        always; its direct play is noted for the client (by its name: per device) with
        the formats it plays as it is (``DirectPlay``) when the decision was at original
        quality (``original_quality``) - Navidrome's token tells neither, and is the same
        for another decision of the song in the same second: the latest decides a new
        play, while a play under way keeps the decisions it went on with (``_goes_on``). A
        transcode stream with such a decision - Navidrome accepting it first
        (``_accepted``) - plays from the add-ons as a ``stream`` does (``_from_addons``),
        with audio the client plays as it is; otherwise, and for any other decision (a
        bitrate limit, a conversion), Navidrome serves the file. ``song``: an owned song,
        whose play may upgrade a person's import."""
        key = (caller.username, call.client, call.get("transcodeParams") or "")
        if call.name == "getTranscodeDecision":
            answer = await library_answer(self.upstream, call)
            if answer is None:
                return None  # (unreachable: the proxy's own answer)
            token = _decision_token(answer.body) if answer.status == 200 else ""
            key = (caller.username, call.client, token)
            direct = DirectPlay.of(call) if token and original_quality(call) else None
            self._direct.pop(key, None)  # newest last
            if direct is not None:
                self._direct[key] = direct
                while len(self._direct) > MAX_DECISIONS:
                    self._direct.pop(next(iter(self._direct)))

            async def reply(receive: Receive, send: Send) -> None:
                await send_bytes(send, answer.status, answer.body, answer.headers)

            return reply
        direct = self._direct.get(key)
        if direct is None or not direct_play(call, str(call.get("mediaId"))):
            return None  # Navidrome's file, converted or as it is
        accepted = await self._accepted(call, ctx.head)
        if accepted is not True:
            # Refused, or not told: Navidrome answers for its own file (a Subsonic error
            # it gave: as it gave it).
            return None if accepted is None or accepted is False else accepted
        token = str(call.get("transcodeParams") or "")
        return await self._from_addons(
            call, ctx, caller, track, minimum, plays=direct.plays, upgrade=song,
            decision=(token, direct),
        )  # fmt: skip

    async def _decision(self, call: RestCall) -> Reply | None:
        """Navidrome's transcode decision for the placeholder itself, when it is direct play
        (the client plays the file as it is, whatever it turns out to be); None otherwise
        (download-first, then Navidrome decides from the real file)."""
        answer = await library_answer(self.upstream, call)
        if answer is None or answer.status != 200 or not _direct_play_decision(answer.body):
            return None

        async def reply(receive: Receive, send: Send) -> None:
            await send_bytes(send, 200, answer.body, answer.headers)

        return reply

    async def _accepted(self, call: RestCall, head: bool = False) -> bool | Reply | None:
        """Whether Navidrome accepts a transcode stream's decision (its signature, song,
        file and age: it serves the placeholder's first byte for it), before the add-ons'
        audio is served for it: asked with all the request's parameters (a form's too, in
        the order Navidrome reads them). False: Navidrome answers the request itself (it
        refused it: 4xx); a reply: the Subsonic error Navidrome answered with (a request
        without ``c`` or ``v``: error 10), passed on as it came - never asked again, which
        could serve the silent file; None: it could not tell."""
        drop = _CONDITIONAL | {b"content-type"}
        headers = [(k, v) for k, v in request_headers(call.headers) if k.lower() not in drop]
        query = urlencode(call.params).encode()
        try:
            check = await self.upstream.send(
                "GET", call.raw_path, query, [*headers, (b"range", b"bytes=0-0")], None
            )
        except Exception as exc:
            log.info("transcode stream not checked: %s", type(exc).__name__)
            return None
        try:
            if check.status_code == 206:  # the placeholder's first byte, for this decision
                return True
            if 400 <= check.status_code < 500:
                return False
            kind = check.headers.get("content-type", "").lower()
            if check.status_code != 200 or kind.startswith(_NOT_AN_ERROR):
                return None
            body = await _first_bytes(check)
            if len(body) >= _ERROR_BYTES or not _subsonic_error(body):
                return None
            drop = {b"content-length", b"content-encoding", b"content-range", b"accept-ranges"}
            headers = [(k, v) for k, v in response_headers(check) if k.lower() not in drop]

            async def reply(receive: Receive, send: Send) -> None:
                await send_bytes(send, 200, body, headers, head=head)

            return reply
        except Exception as exc:  # its answer broke off: it could not tell
            log.info("transcode stream not checked: %s", type(exc).__name__)
            return None
        finally:
            await check.aclose()

    async def jukebox(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """jukeboxControl. While Navidrome's jukebox is off (its default), Shijhon gives
        Navidrome's own "not implemented" answer itself, after the (cached) credential
        check: a documented exception to forwarding, because Navidrome logs each such
        request with its full URL, credentials included, and clients poll it. Every request
        whose credentials Navidrome accepts is answered here then - whatever its HTTP
        method, also a browser's across origins (with the CORS headers Navidrome adds) -
        and while Navidrome cannot say whether it is on, a request gets an error, never a
        forward. With the jukebox on, placeholders are fetched first (download-first) - all
        of them before Navidrome gets the request, so none waits for the user's download
        turns or the hour's allowance (it is taken all the same: downloads after them wait
        longer). At the add-ons' limits only the first song of a queue that is set is the
        song being played; the others - and songs added to a queue - are queued downloads,
        after every listener's play."""
        placing = call.get("action") in ("set", "add")
        if not placing and self._jukebox_fresh() is True:
            return None  # the jukebox is on: Navidrome answers (no work here)
        caller = await ctx.caller()
        if caller is None:
            # Navidrome's own credential error: the check's answer; a request without
            # any credentials is forwarded (nothing in it for a log).
            return None
        enabled = await self.jukebox_state()
        if enabled is None:
            return _unavailable(ctx.head)
        if not enabled:
            return not_implemented(call)
        if not placing:
            return None
        pending, backed = [], {}
        for song_id in call.getall("id"):
            row = await self.placeholder(song_id, ctx)
            row = await self._settled(row) if row is not None else None
            if row is None or row["state"] == "delivered":
                continue
            if backing := await self._backing(row):
                backed[row["song_id"]] = backing
            else:
                pending.append(row["song_id"])
        if not pending:
            return self._swap(call, "id", backed) if backed else None
        # A queue that is set plays its first song. The upcoming ones are queued downloads
        # at the add-ons' limits, like an offline sync's - but the request is answered only
        # once they are all in place, so none waits for the user's own turns.
        current = call.getall("id")[0] if call.get("action") == "set" else None
        for song_id in pending:
            urgency = pacing.PLAY if song_id == current else pacing.QUEUED
            failure = await self.download_first.ensure(
                song_id, caller.username, playing=True, urgency=urgency
            )
            if failure is not None:
                return subsonic_error(call, 0, f"audio unavailable: {failure}")
        return self._swap(call, "id", backed) if backed else None

    # --- accounts that get real audio only ------------------------------------------------

    async def real_audio(
        self,
        call: RestCall,
        ctx: RequestContext,
        absent: Callable[[list[str]], Awaitable[set[str]]],
    ) -> HandlerResult:
        """An audio request - ``stream``, ``download``, ``getTranscodeDecision``,
        ``getTranscodeStream`` - of an account that gets real audio only
        (``[placeholders] real_audio_accounts``; its credentials checked): Navidrome's
        own file, never the add-ons. A song without its audio in the library - a silent
        placeholder (also one an owned recording plays for: that recording is the
        account's song), or one ``absent`` names (catalog songs, removed releases' songs)
        - goes to Navidrome as an ID it does not have, for its own answer to that ("not
        found"). Delivered audio has its use recorded first, so that no revert slips in
        before Navidrome serves it. A new placeholder still being written is waited for,
        as for everyone, and an archive of an album, artist or playlist with a silent file
        is refused. No delivery, preparation, fetch ahead or add-on request, ever."""
        try:
            return await self._real_audio(call, ctx, absent)
        except _BeingWritten:
            return _being_written(call)
        except Unsettled:
            return _being_written(call, "its file is being put right after a stop")

    async def _real_audio(
        self,
        call: RestCall,
        ctx: RequestContext,
        absent: Callable[[list[str]], Awaitable[set[str]]],
    ) -> HandlerResult:
        key = "mediaId" if call.name in ("getTranscodeDecision", "getTranscodeStream") else "id"
        if key == "mediaId" and call.get("mediaType") != "song":
            return None  # Navidrome's own answer (only songs)
        idents = list(dict.fromkeys(v for v in call.getall(key) if v))
        missing = set(await absent(idents))
        native: list[str] = []
        for ident in idents:
            if ident in missing:
                continue
            row = await self.placeholder(ident, ctx)
            if row is None:
                native.append(ident)  # an owned song, or an album, artist or playlist
            elif row["state"] != "delivered":
                missing.add(ident)
            else:
                row = await self.download_first.settled(ident, used=True)
                if row is None or row["state"] != "delivered":
                    missing.add(ident)  # reverted, or taken out, meanwhile
        if call.name == "download" and not missing and native:
            # Albums, artists and playlists are downloaded as archives: refused with a
            # silent file in them, as for everyone.
            song = None
            with contextlib.suppress(NavidromeError):
                song = await self.navidrome.song(native[0])
            if song is None:
                return self._archive(call, native[0])
            if self.engine.writing() and await self.placeholder(native[0], ctx) is not None:
                missing.add(native[0])  # a new placeholder, its row recorded meanwhile
        if missing:
            return Forward(
                call.rewritten(lambda k, v: ABSENT if k == key and v in missing else None)
            )
        return None

    def _jukebox_fresh(self) -> bool | None:
        """The jukebox's state while what was last learned still holds; None otherwise."""
        known = self._jukebox
        return known[1] if known is not None and known[0] >= time.monotonic() else None

    async def jukebox_enabled(self) -> bool:
        """Whether Navidrome's jukebox is known to be on (``jukebox_state``): not known is
        not on - nothing is placed or committed for it."""
        return await self.jukebox_state() is True

    async def jukebox_state(self) -> bool | None:
        """Whether Navidrome's jukebox is enabled (remembered for a minute): from its
        configuration as admins see it, else from the service account's jukebox role (an
        admin's is exactly that setting). Asking jukeboxControl itself would put the service
        account's credentials into Navidrome's log. While Navidrome cannot be asked, an
        "off" it said last stands (asked again after a few seconds: answering for it puts
        nothing in a log); None when it never said, or said "on" and that has expired: a
        request is not forwarded on a guess."""
        fresh = self._jukebox_fresh()
        if fresh is not None:
            return fresh
        now = time.monotonic()
        enabled: bool | None = None
        config = await self.navidrome.config()
        settings = config.get("config") if isinstance(config, dict) else None
        jukebox = settings.get("Jukebox") if isinstance(settings, dict) else None
        if isinstance(jukebox, dict) and isinstance(jukebox.get("Enabled"), bool):
            enabled = jukebox["Enabled"]
        else:
            try:
                user = await self.navidrome.subsonic("getUser", [("username", self.navidrome.user)])
                role = (user.get("user") or {}).get("jukeboxRole")
                enabled = role if isinstance(role, bool) else None
            except NavidromeError:
                enabled = None
        if enabled is not None:
            self._jukebox = (now + 60, enabled)
        elif self._jukebox is not None and not self._jukebox[1]:
            self._jukebox = (now + 5, False)
        else:
            return None
        return self._jukebox[1]

    # --- share links ------------------------------------------------------------------

    async def share(
        self, scope: Scope, receive: Receive, send: Send, *, deliver: bool = True
    ) -> bool:
        """``/share/s/<token>`` and ``/share/d/<id>``. Returns True if it answered.
        ``deliver`` False (a share of an account that gets real audio only): a silent
        placeholder in it is refused, never fetched."""
        path: str = scope["path"]
        if scope["method"] not in ("GET", "HEAD"):
            return False
        kind, _, ident = path.removeprefix("/share/").partition("/")
        if kind not in ("s", "d") or not ident:
            return False
        # (A shared collection's archive is looked at also with no placeholder yet: the
        # first may be written while Navidrome lists it.)
        if kind == "s" and not await self._may_hold_placeholders(delivered=True):
            return False
        answered = music_send(scope["method"], list(scope["headers"]), send)
        try:
            return await self._share(scope, receive, answered, kind, ident, deliver)
        except (_BeingWritten, Unsettled):
            await send_bytes(
                answered,
                503,
                b"Audio unavailable\n",
                [(b"content-type", b"text/plain")],
                head=scope["method"] == "HEAD",
            )
            return True

    async def _share(
        self, scope: Scope, receive: Receive, send: Send, kind: str, ident: str, deliver: bool
    ) -> bool:
        valid = False
        shared: list[str] = []
        if kind == "s":
            song = _jwt_claims(ident).get("id")
            if not isinstance(song, str):
                return False
            if self.engine.writing():
                # Looking a song up among the placeholders being written is work:
                # the link - the credential - is checked first.
                if not await self._valid_share(scope):
                    return False
                valid = True
            if not await self.placeholder(song):
                return False
            shared = [song]
        # The link is the credential: Navidrome validates it before any work is done.
        if not valid and not await self._valid_share(scope):
            return False
        songs = shared if kind == "s" else await self._shared_songs(ident)
        for song_id in await self.silent_among(songs, state="delivered"):
            await self.download_first.settled(song_id, used=True)  # played through the link
        silent = await self.silent_among(songs)
        if silent and not deliver:
            raise _BeingWritten(ident)  # (answered "Audio unavailable")
        for song_id in silent[:MAX_SHARE_TRACKS]:
            await self.download_first.used(song_id)  # played through the link
            # The link is the credential: its downloads count as the share's own.
            failure = await self.download_first.ensure(song_id, f"share {ident[:16]}")
            if failure is not None:
                await send_bytes(
                    send, 503, b"Audio unavailable\n", [(b"content-type", b"text/plain")]
                )
                return True
        if kind == "s":
            return False
        # The share's archive: forwarded with no new placeholders moving into the library
        # from this check until Navidrome's answer starts, and after the writes under
        # way - a song that became a silent placeholder meanwhile is not served.
        async with self.engine.listing(wait=WRITE_WAIT_SECONDS) as ended:
            if not ended:
                raise _BeingWritten(ident)
            try:  # An incomplete listing cannot establish that the archive holds no silence.
                shared_now = await self._shared_songs(ident, strict=True)
            except NavidromeError:
                raise _BeingWritten(ident) from None
            now = await self.silent_among(shared_now)
            if now:
                raise _BeingWritten(ident)
            if await self._unfinished_among([{"id": song} for song in shared_now]):
                raise _BeingWritten(ident)
            path: bytes = scope.get("raw_path") or scope["path"].encode()
            headers = request_headers(list(scope["headers"]), keep_length=True)
            response = await self._ask(
                scope["method"], path, scope.get("query_string", b""), headers, None
            )
        if response is None:
            await _unreachable()(receive, send)
        else:
            await relay(response, receive, send, head=scope["method"] == "HEAD")
        return True

    async def _valid_share(self, scope: Scope) -> bool:
        headers = [
            (k, v)
            for k, v in request_headers(list(scope["headers"]))
            if k.lower() not in _CONDITIONAL
        ]
        path: bytes = scope.get("raw_path") or scope["path"].encode()
        try:
            check = await self.upstream.send(
                "HEAD", path, scope.get("query_string", b""), headers, None
            )
        except Exception:
            # No verdict is not an invalid link: forwarding a subsequent GET could
            # succeed with an archive whose silent files were never checked.
            raise _BeingWritten("share") from None
        await check.aclose()
        if check.status_code >= 500:
            raise _BeingWritten("share")
        return check.status_code == 200

    @staticmethod
    def shared_song(token: str) -> str | None:
        """The song a share link's stream token (``/share/s/<token>``) names (unverified:
        Navidrome verifies the link itself)."""
        song = _jwt_claims(token).get("id")
        return song if isinstance(song, str) else None

    async def shared_songs(self, share_id: str) -> list[str]:
        """The songs of a share, completely listed (``NavidromeError`` otherwise)."""
        return await self._shared_songs(share_id, strict=True)

    async def _shared_songs(self, share_id: str, *, strict: bool = False) -> list[str]:
        """At most 5,000 entries, completely listed; strict failures refuse the archive."""
        try:
            share = await self.navidrome.share(share_id)
            resources = share.get("resourceIds") if share else None
            if not isinstance(resources, str) or not resources:
                raise NavidromeError("share: unavailable resources")
            # Bound both the number of native requests and their aggregate entries.
            ids = resources.split(",", 5000)
            if len(ids) > 5000 or any(not ident for ident in ids):
                raise NavidromeError("share: inspection limit exceeded")
            kind = share.get("resourceType") if share else None
            if kind == "media_file":
                return ids
            if kind not in ("album", "playlist"):
                raise NavidromeError("share: unsupported resource")
            if len(ids) > MAX_SHARE_COLLECTIONS:
                raise NavidromeError("share: collection inspection limit exceeded")
            songs: list[str] = []
            for resource in ids:
                songs += await self.navidrome.archive_song_ids(
                    kind, resource, limit=5000 - len(songs)
                )
            return songs
        except NavidromeError:
            if strict:
                raise
            return []


_NOT_AN_ERROR = ("audio/", "video/", "application/octet-stream")  # content types
_ERROR_BYTES = 65536  # a Subsonic error answer is far smaller


async def _first_bytes(response: httpx.Response, limit: int = _ERROR_BYTES) -> bytes:
    """The start of an answer's body, decoded (at most ``limit`` bytes)."""
    data = b""
    async for chunk in response.aiter_bytes():
        data += chunk
        if len(data) >= limit:
            break
    return data[:limit]


def _subsonic_error(body: bytes) -> bool:
    """Whether an answer's body is a Subsonic error (JSON, JSONP or XML)."""
    return re.search(rb'"status"\s*:\s*"failed"|\bstatus="failed"', body) is not None


def _guarded(handler: Callable[[RestCall, RequestContext], Awaitable[HandlerResult]]) -> Any:
    """A handler whose new placeholder did not get its row in time answers with an
    error, never with Navidrome's silence."""

    async def guarded(call: RestCall, ctx: RequestContext) -> HandlerResult:
        try:
            return await handler(call, ctx)
        except _BeingWritten:
            return _being_written(call)
        except Unsettled:
            return _being_written(call, "its file is being put right after a stop")

    return guarded


def _being_written(call: RestCall, why: str = "the song is being added to the library") -> Reply:
    """The answer while a new placeholder has no row yet (or a song's file is being put
    right): an error - as an HTTP error for ``getTranscodeStream``, which Navidrome answers
    so."""
    if call.name == "getTranscodeStream":

        async def reply(receive: Receive, send: Send) -> None:
            await send_bytes(send, 503, b"Service Unavailable\n", _HTTP_ERROR)

        return reply
    return subsonic_error(call, 0, f"audio unavailable: {why}; try again")


def _unreachable() -> Reply:
    """As the proxy answers when Navidrome cannot be reached."""

    async def reply(receive: Receive, send: Send) -> None:
        await send_bytes(
            send, 502, b"Navidrome is unreachable\n", [(b"content-type", b"text/plain")]
        )

    return reply


def _unavailable(head: bool) -> Reply:
    """A request Shijhon neither answers for Navidrome nor forwards: Navidrome could not be
    asked what it needs to know."""

    async def reply(receive: Receive, send: Send) -> None:
        headers = [(b"content-type", b"text/plain"), (b"retry-after", b"5")]
        await send_bytes(send, 503, b"Navidrome is not answering yet\n", headers, head=head)

    return reply


def _http_error() -> Reply:
    async def reply(receive: Receive, send: Send) -> None:
        await send_bytes(send, 500, b"Internal Server Error\n", _HTTP_ERROR)

    return reply


def original_quality(call: RestCall) -> bool:
    """A transcode decision (its JSON client profile) that sets no bitrate limit and no
    required codec limitation: the client asks for the file as it is, if it can play
    it. Anything unreadable: not."""
    try:
        info = json.loads(call.body or b"")
    except ValueError:
        return False
    if not isinstance(info, dict):
        return False
    limit = info.get("maxAudioBitrate")
    if limit not in (None, 0) and not (isinstance(limit, (int, float)) and limit <= 0):
        return False
    profiles = info.get("codecProfiles") or []
    if not isinstance(profiles, list):
        return False  # Navidrome's own answer (an invalid profile)
    for profile in profiles:
        limitations = profile.get("limitations") if isinstance(profile, dict) else None
        if not isinstance(limitations, list) and limitations is not None:
            return False
        for limitation in limitations or []:
            if not isinstance(limitation, dict) or limitation.get("required"):
                return False
    return True


# Navidrome 0.64.2's names of one container (``core/stream/aliases.go``), as far as the
# add-ons' audio needs them; and of one codec.
_CONTAINERS = (
    frozenset({"aac", "adts", "m4a", "mp4", "m4b", "m4p"}),
    frozenset({"mpeg", "mp3", "mp2"}),
    frozenset({"ogg", "oga", "opus"}),
)
_CODECS = (frozenset({"aac", "adts"}),)
# The add-ons' audio by its format (``Opened.kind``): its container, as Navidrome names a
# file of it, and its codecs - an M4A's is not read: AAC or ALAC.
_AUDIO = {
    "flac": ("flac", ("flac",)),
    "mp3": ("mp3", ("mp3",)),
    "m4a": ("m4a", ("aac", "alac")),
    "opus": ("opus", ("opus",)),
    "ogg": ("ogg", ("vorbis",)),
}


def _named(value: str, names: tuple[str, ...], groups: tuple[frozenset[str], ...]) -> bool:
    return value in names or any(value in g and not g.isdisjoint(names) for g in groups)


@dataclass(frozen=True)
class DirectPlay:
    """What a client plays as it is, by the direct-play profiles of its transcode decision
    (the OpenSubsonic transcoding extension), read as Navidrome 0.64.2 reads them for a
    file: a profile whose protocols name HTTP (or none), whose containers name the audio's
    (or none) and whose codecs name its codec (or none; an M4A, whose codec is not read,
    needs AAC and ALAC both), for two channels. Codec limitations do not matter: a
    decision at original quality has none that is required."""

    # Each profile: its protocols, containers and codecs (lower case), its channels (0: any).
    profiles: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], int], ...]

    @classmethod
    def of(cls, call: RestCall) -> DirectPlay | None:
        """The client profile of a ``getTranscodeDecision`` (its JSON body); None when it
        cannot be read."""
        try:
            info = json.loads(call.body or b"")
        except ValueError:
            return None
        profiles = info.get("directPlayProfiles") if isinstance(info, dict) else None
        if not isinstance(profiles, list):
            return None
        read: list[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], int]] = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            fields: list[tuple[str, ...]] = []
            for key in ("protocols", "containers", "audioCodecs"):
                value = profile.get(key) or []
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    break
                fields.append(tuple(v.lower() for v in value))
            channels = profile.get("maxAudioChannels") or 0
            if len(fields) == 3 and isinstance(channels, int):
                read.append((fields[0], fields[1], fields[2], channels))
        return cls(tuple(read))

    def plays(self, kind: str | None) -> bool:
        """Whether the client plays the add-ons' audio of this format as it is (not
        told: no)."""
        audio = _AUDIO.get(kind or "")
        if audio is None:
            return False
        container, codecs = audio
        for protocols, containers, names, channels in self.profiles:
            if protocols and "http" not in protocols:
                continue
            if containers and not _named(container, containers, _CONTAINERS):
                continue
            if names and not all(_named(codec, names, _CODECS) for codec in codecs):
                continue
            if 0 < channels < 2:
                continue
            return True
        return False


def _decision_token(body: bytes) -> str:
    """The token of Navidrome's getTranscodeDecision answer (JSON or XML) when it is direct
    play; "" otherwise."""
    if not _direct_play_decision(body):
        return ""
    try:
        data = json.loads(body)
    except ValueError:
        tag = re.search(rb"<transcodeDecision\b[^>]*>", body)
        found = re.search(rb'\btranscodeParams="([^"]+)"', tag.group(0)) if tag else None
        return found.group(1).decode("ascii", "replace") if found else ""
    answer = data.get("subsonic-response") if isinstance(data, dict) else None
    decision = answer.get("transcodeDecision") if isinstance(answer, dict) else None
    token = decision.get("transcodeParams") if isinstance(decision, dict) else None
    return token if isinstance(token, str) else ""


def _direct_play_decision(body: bytes) -> bool:
    """Whether Navidrome's getTranscodeDecision answer (JSON or XML) is direct play."""
    try:
        data = json.loads(body)
    except ValueError:
        return re.search(rb"<transcodeDecision\b[^>]*\bcanDirectPlay=\"true\"", body) is not None
    answer = data.get("subsonic-response") if isinstance(data, dict) else None
    decision = answer.get("transcodeDecision") if isinstance(answer, dict) else None
    return isinstance(decision, dict) and decision.get("canDirectPlay") is True


def direct_play(call: RestCall, song_id: str) -> bool:
    """Whether a ``getTranscodeStream`` request's decision (its ``transcodeParams`` token,
    read unverified - the caller's credentials are checked, and the song is one they may
    stream anyway) plays the file as it is: direct play, or no target format, for this
    song, not expired."""
    claims = _jwt_claims(call.get("transcodeParams") or "")
    if claims.get("mid") != song_id:
        return False
    expires = claims.get("exp")
    if isinstance(expires, (int, float)) and expires < time.time():
        return False  # Navidrome's own answer: gone
    return claims.get("dp") is True or not claims.get("f")


def _jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, unverified (Navidrome verifies the link itself)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}
