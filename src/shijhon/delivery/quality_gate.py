"""Each person's minimum audio quality, and what it does to the songs that have their own
files.

Each person chooses on the dashboard's Audio quality page: any quality, compressed audio of
at least 128, 160, 192, 256 or 320 kbit/s (lossless at any bitrate counts as above them), or
lossless only; every choice but "any" also asks for at least 44.1 kHz. Without a choice
the installation's default applies (``[delivery] quality_default``, 192). The choice is
saved with the dashboard's per-person values (``saved_settings``, section
``quality:<user ID>``), by Navidrome's immutable user ID.

A song whose file - an owned or imported one, or a catalog song's delivered audio - is
below the listener's minimum plays from the add-ons instead, under its own ID, when they
have the recording (the same strict lookup as any catalog song's: its ISRC, else title,
artist and length) and their audio is not known to be below the minimum too (an add-on's
audio below it is passed over for the next add-on's: ``refusal``); otherwise its file
plays. A file below someone's minimum never stands in for a catalog song they play
(``placeholders/backing.py``): the add-ons play it, the file only when they have nothing.

The quality of a song's file is Navidrome's (its native song: format, bitrate, sample rate,
bit depth), read with the service account and remembered for ten minutes; a song with
unknown quality meets every minimum (``quality.Minimum``). Accounts that get real audio only
(``[placeholders] real_audio_accounts``) are never sent to the add-ons.

Not here: importing (the import gate asks ``Minimums.minimum_for(user_id=...)``) and
upgrading a person's own imported file from the add-ons after a play (it would follow a
play this gate sent to the add-ons: ``Interceptor._from_addons``).
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from shijhon.access import AccessService
from shijhon.delivery.playback import Track
from shijhon.navidrome.client import NavidromeService
from shijhon.quality import ANY, LOSSLESS_CODECS, LOSSLESS_FORMATS, Minimum, Quality
from shijhon.store import Store

log = logging.getLogger(__name__)

CHOICES = ("any", "128", "160", "192", "256", "320", "lossless")
SECTION = "quality"  # saved_settings section prefix: "quality:<user ID>"
KEY = "minimum"
UPGRADE_KEY = "upgrade"  # "off": the person's own imports are not upgraded after a play
SECONDS = 600.0  # a file's quality, and a name's user ID, are remembered this long
MAX_REMEMBERED = 20000


def minimum_of(choice: str) -> Minimum:
    """The minimum a choice names (an unknown one: any quality)."""
    if choice == "lossless":
        return Minimum(kbps=0, lossless_only=True)
    if choice.isdigit() and choice in CHOICES:
        return Minimum(kbps=int(choice))
    return ANY


class Minimums:
    """Each person's chosen minimum, by user ID; ``default`` is the installation's choice
    (kept current by the dashboard's live settings). Whether anyone can have a minimum
    at all (``anyone``) is kept in memory, asked again when a choice or the default
    changes."""

    def __init__(
        self,
        store: Store,
        navidrome: NavidromeService,
        *,
        default: str = "192",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.access = AccessService(navidrome)
        self._anyone: bool | None = None  # (not known yet)
        # Counts the changes of a choice or the default: a reading of ``anyone`` begun
        # before one is not kept.
        self._changes = 0
        self.default = default
        self.clock = clock
        self._choices: dict[str, str | None] = {}  # user ID -> own choice (None: default)
        self._upgrades: dict[str, bool] = {}  # user ID -> upgrade own imports after a play
        self._ids: OrderedDict[str, tuple[float, str]] = OrderedDict()  # name -> (until, ID)

    @property
    def default(self) -> str:
        return self._default

    @default.setter
    def default(self, value: str) -> None:
        self._default = value
        self._changed()

    def _changed(self) -> None:
        self._anyone = None
        self._changes += 1

    async def anyone(self) -> bool:
        """Whether anyone can have a minimum: the installation's default is not "any",
        or someone chose one that is not. (Read once, then kept until a choice or the
        default changes; a reading that a change overtook is read again, never kept.)"""
        while self._anyone is None:
            changes = self._changes
            if (self.default if self.default in CHOICES else "any") != "any":
                found = True
            else:
                row = await self.store.fetchone(
                    "SELECT 1 FROM saved_settings WHERE section GLOB ? AND key = ?"
                    " AND value IN (SELECT value FROM json_each(?)) LIMIT 1",
                    [
                        f"{SECTION}:*",
                        KEY,
                        json.dumps([json.dumps(c) for c in CHOICES if c != "any"]),
                    ],
                )
                found = row is not None
            if changes == self._changes:
                self._anyone = found
                return found
        return self._anyone

    async def choice(self, user_id: str) -> str | None:
        """The person's own choice; None: the installation's default applies."""
        if user_id not in self._choices:
            row = await self.store.fetchone(
                "SELECT value FROM saved_settings WHERE section = ? AND key = ?",
                [f"{SECTION}:{user_id}", KEY],
            )
            try:
                value = json.loads(row["value"]) if row is not None else None
            except ValueError:
                value = None
            self._choices[user_id] = value if value in CHOICES else None
        return self._choices[user_id]

    async def set(self, user_id: str, choice: str | None, *, by: str = "") -> None:
        """Save the person's choice (None: back to the installation's default)."""
        if choice is not None and choice not in CHOICES:
            raise ValueError("unknown quality choice")
        section = f"{SECTION}:{user_id}"
        async with self.store.transaction() as conn:
            if choice is None:
                await conn.execute(
                    "DELETE FROM saved_settings WHERE section = ? AND key = ?", [section, KEY]
                )
            else:
                await conn.execute(
                    "INSERT INTO saved_settings (section, key, value, saved_at, saved_by)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (section, key) DO UPDATE SET"
                    " value = excluded.value, saved_at = excluded.saved_at,"
                    " saved_by = excluded.saved_by",
                    [section, KEY, json.dumps(choice), time.time(), by or user_id],
                )
        self._choices[user_id] = choice
        self._changed()

    async def upgrades(self, user_id: str) -> bool:
        """Whether the person's own imported songs below their minimum are upgraded from
        the add-ons after they play them (``music_imports/upgrade.py``): on unless they
        switched it off."""
        if user_id not in self._upgrades:
            row = await self.store.fetchone(
                "SELECT value FROM saved_settings WHERE section = ? AND key = ?",
                [f"{SECTION}:{user_id}", UPGRADE_KEY],
            )
            try:
                value = json.loads(row["value"]) if row is not None else None
            except ValueError:
                value = None
            self._upgrades[user_id] = value != "off"
        return self._upgrades[user_id]

    async def set_upgrades(self, user_id: str, on: bool, *, by: str = "") -> None:
        async with self.store.transaction() as conn:
            await conn.execute(
                "INSERT INTO saved_settings (section, key, value, saved_at, saved_by)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (section, key) DO UPDATE SET"
                " value = excluded.value, saved_at = excluded.saved_at,"
                " saved_by = excluded.saved_by",
                [
                    f"{SECTION}:{user_id}",
                    UPGRADE_KEY,
                    json.dumps("on" if on else "off"),
                    time.time(),
                    by or user_id,
                ],
            )
        self._upgrades[user_id] = on

    async def user_id(self, username: str) -> str | None:
        """The user ID of a name Navidrome authenticated (remembered for a while)."""
        return await self._user_id(username)

    async def effective(self, user_id: str) -> str:
        """The choice that applies to the person: their own, else the default."""
        return await self.choice(user_id) or (self.default if self.default in CHOICES else "any")

    async def minimum_for(
        self, *, user_id: str | None = None, username: str | None = None
    ) -> Minimum:
        """The person's minimum, by user ID or by the name Navidrome authenticated. A name
        that cannot be resolved now gets no minimum: plain Navidrome behavior."""
        if user_id is None and username:
            user_id = await self._user_id(username)
        if not user_id:
            return ANY
        return minimum_of(await self.effective(user_id))

    async def _user_id(self, username: str) -> str | None:
        key = username.casefold()
        now = self.clock()
        found = self._ids.get(key)
        if found is not None and found[0] > now:
            return found[1]
        try:
            principal = await self.access.verify(username)
        except Exception as exc:  # unreachable or unknown: no minimum for now
            log.info("quality minimum: the account could not be looked up (%s)", type(exc).__name__)
            return None
        self._ids[key] = (now + SECONDS, principal.user_id)
        self._ids.move_to_end(key)
        while len(self._ids) > MAX_REMEMBERED:
            self._ids.popitem(last=False)
        return principal.user_id


@dataclass(frozen=True)
class LibrarySong:
    """A song with its own file, as the gate needs it: its file's quality, and what the
    add-ons are asked for (its recording)."""

    song_id: str
    quality: Quality
    title: str
    artist: str
    duration_ms: int
    isrc: str | None = None
    disc: int = 1
    number: int = 0
    version: str | None = None  # "clean" or "explicit", when its tags say

    def track(self) -> Track:
        """The recording for the add-ons, keyed by the song's own ID (it has no catalog
        track: the add-ons look it up by its ISRC, else its title, artist and length)."""
        return Track(
            self.song_id,
            self.isrc,
            self.title,
            self.artist,
            self.duration_ms,
            disc=self.disc,
            number=self.number,
            version=self.version,
        )


def native_quality(song: Mapping[str, Any]) -> Quality:
    """A native song's file quality (Navidrome's own JSON: ``sampleRate``, ``codec``)."""
    fmt = str(song.get("suffix") or "").lower()
    codec = str(song.get("codec") or "").lower()
    depth = _number(song.get("bitDepth"))
    lossless = fmt in LOSSLESS_FORMATS or codec in LOSSLESS_CODECS or depth > 0
    rate = _number(song.get("sampleRate") or song.get("samplingRate"))
    return Quality(fmt, lossless, _number(song.get("bitRate")), rate, depth)


def addon_quality(kind: str | None, kbps: int | None) -> Quality:
    """The quality the add-on's audio told when it was opened (``Opened.kind``/``kbps``);
    nothing told: not known (it meets every minimum)."""
    fmt = (kind or "").lower()
    return Quality(fmt, fmt in LOSSLESS_FORMATS or fmt in LOSSLESS_CODECS, kbps or 0)


def refusal(minimum: Minimum, kind: str | None, kbps: int | None) -> str | None:
    """Why the add-ons' audio of this format and bitrate (as its first bytes told:
    ``Opened.kind``/``kbps``) does not do for a listener with this minimum; None when it
    does - also when nothing is told."""
    if minimum.met_by(addon_quality(kind, kbps)):
        return None
    told = f"{(kind or 'audio').upper()} at {kbps} kbit/s" if kbps else (kind or "audio").upper()
    return f"below the listener's minimum ({told})"


def library_song(song_id: str, song: Mapping[str, Any]) -> LibrarySong:
    tags = song.get("tags")
    isrcs = tags.get("isrc") if isinstance(tags, Mapping) else None
    isrc = str(isrcs[0]) if isinstance(isrcs, list) and isrcs and isrcs[0] else None
    explicit = str(song.get("explicitStatus") or "")
    return LibrarySong(
        song_id,
        native_quality(song),
        str(song.get("title") or ""),
        str(song.get("artist") or ""),
        int(float(song.get("duration") or 0) * 1000),
        isrc,
        _number(song.get("discNumber")) or 1,
        _number(song.get("trackNumber")),
        {"e": "explicit", "c": "clean"}.get(explicit),
    )


class QualityGate:
    """What a listener's minimum does to a song with its own file (module docstring)."""

    def __init__(
        self,
        minimums: Minimums,
        navidrome: NavidromeService,
        *,
        library_id: int,
        exempt: Callable[[str], bool] = lambda username: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.minimums = minimums
        self.navidrome = navidrome
        self.library_id = library_id
        self.exempt = exempt
        self.clock = clock
        self._songs: OrderedDict[str, tuple[float, LibrarySong | None]] = OrderedDict()

    async def anyone(self) -> bool:
        """Whether anyone can have a minimum (``Minimums.anyone``): when no one can, a song
        with its own file is Navidrome's, with nothing asked - not even who is listening -
        unless a play of it from the add-ons that began earlier is under way
        (``Interceptor._going_on``). Not known: maybe."""
        try:
            return await self.minimums.anyone()
        except Exception as exc:
            log.warning("quality minimums unavailable: %s", type(exc).__name__)
            return True

    async def minimum(self, username: str) -> Minimum:
        """The listener's minimum; none (plain Navidrome behavior) when it cannot be told."""
        if self.exempt(username):
            return ANY
        try:
            return await self.minimums.minimum_for(username=username)
        except Exception as exc:  # the file plays, as without a minimum
            log.warning("quality minimum unavailable: %s", type(exc).__name__)
            return ANY

    async def song(self, song_id: str) -> LibrarySong | None:
        """The song's file as Navidrome has it (remembered); None: not a present song of the
        library, or Navidrome could not tell."""
        now = self.clock()
        found = self._songs.get(song_id)
        if found is not None and found[0] > now:
            return found[1]
        try:
            native = await self.navidrome.song(song_id)
        except Exception as exc:  # (not remembered: asked again next time) the file plays
            log.info("quality: a song could not be looked up (%s)", type(exc).__name__)
            return None
        known = (
            library_song(song_id, native)
            if native is not None
            and not native.get("missing")
            and _number(native.get("libraryId")) == self.library_id
            else None
        )
        self._songs[song_id] = (now + SECONDS, known)
        self._songs.move_to_end(song_id)
        while len(self._songs) > MAX_REMEMBERED:
            self._songs.popitem(last=False)
        return known

    async def below(self, username: str, song_id: str) -> LibrarySong | None:
        """The song, when its file is below the listener's minimum; None when it meets it
        (or cannot be told): its file plays, nothing more is asked."""
        minimum = await self.minimum(username)
        if minimum == ANY:
            return None
        song = await self.song(song_id)
        if song is None or minimum.met_by(song.quality):
            return None
        return song

    async def stands_in(self, username: str | None, song_id: str) -> bool:
        """Whether the file of ``song_id`` may stand in for a catalog song this listener
        plays (not below their minimum; no listener: yes)."""
        return username is None or await self.below(username, song_id) is None

    def forget(self, song_id: str) -> None:
        """The song's file changed (its quality is read again)."""
        self._songs.pop(song_id, None)


def _number(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0
