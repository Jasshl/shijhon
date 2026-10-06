"""Owned recordings on other releases (suite L).

A catalog release often contains a recording the owner already has elsewhere - the
single, the original album of a reissue. Its placeholder is then backed by the owned file:
playback serves the local audio instead of an add-on, while user actions stay on the
placeholder. A catalog song not in the library yet plays that file too.

The same recording is, strictly (a wrong backing plays the wrong audio for good):

- the same ISRC (owned files that carry one); two known ISRCs that differ are two
  recordings; or
- when either side has none: the same recording title (``recording_key``:
  edition markers aside; a version marker on one side only - "(Live)", "(Remix)", "(Album
  Version)", "(Mara's Version)" - is another take), a shared artist, the same length
  within about 2 s, no
  clean/explicit contradiction (a clean edit is other audio) - and the owned song on
  the single of that title or on an edition of the same album (a title shared by songs of
  different albums, "Intro", is not enough), the only owned song that fits.

Owned songs are found with Navidrome's own search (the service account, by the title's
core: "Stay (feat. X)" finds "Stay"), never placeholders or songs of the release itself -
nor any file in the placeholder folder, recorded or not: a play during its release's commit
would find the release's new placeholder, scanned but not recorded yet, by its ISRC and play
silence. A lookup for a play waits a moment at most; one that fails or runs late leaves the
song to the add-ons.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any

import anyio

from shijhon.catalog.model import CatalogTrack
from shijhon.locks import KeyedLocks
from shijhon.matching.normalize import (
    core_title,
    name_key,
    recording_key,
    same_artist,
    title_key,
    version_marker,
)
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.store import Store

log = logging.getLogger(__name__)

TOLERANCE_MS = 2000  # from the middle of Subsonic's whole (truncated) second: about ±2 s
SEARCH_COUNT = 50
CACHE_SECONDS = 300.0
PLAY_WAIT_SECONDS = 1.0  # a play's lookup; then the add-ons
PRESENT_SECONDS = 60.0  # an owned song's presence is checked again after this


def _isrc(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def _version(track: CatalogTrack) -> str | None:
    return "clean" if track.clean else "explicit" if track.explicit else None


class OwnedRecordings:
    def __init__(
        self,
        navidrome: NavidromeService,
        store: Store,
        *,
        library_id: int = 1,
        placeholder_folder: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.navidrome = navidrome
        self.store = store
        self.library_id = library_id
        # Library-relative; its files are Shijhon's, never owned recordings.
        self.placeholder_folder = (
            PurePosixPath(placeholder_folder).as_posix() if placeholder_folder else None
        )
        self.clock = clock
        # Catalog track -> (until, owned song or None): a play's requests (probe, range,
        # whole file) and its seeks ask once.
        self._found: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        self._present: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        # Song ID -> whether its file is in the placeholder folder: Navidrome derives a
        # song's ID from its path, so the answer never changes for an ID.
        self._in_folder: OrderedDict[str, bool] = OrderedDict()
        self._locks = KeyedLocks()

    async def find(self, track: CatalogTrack) -> str | None:
        """An owned song of the same recording as ``track`` for a play (a moment at most;
        a failure or no answer in time: None, remembered briefly)."""
        key = str(track.ref)
        async with self._locks.hold(key):  # a play's concurrent requests ask once
            cached = self._found.get(key)
            if cached is not None and cached[0] > self.clock():
                if cached[1] is None or await self.present(cached[1]) == "here":
                    return cached[1]
                return None
            found: str | None = None
            keep = CACHE_SECONDS
            with anyio.move_on_after(PLAY_WAIT_SECONDS) as scope:
                try:
                    found = await self._search(track, track.album_title, set())
                except NavidromeError as exc:
                    log.info("owned recording lookup failed: %s", exc)
                    keep = 30.0
            if scope.cancelled_caught:
                keep = 30.0
            _remember(self._found, key, (self.clock() + keep, found))
            return found

    async def present(self, song_id: str) -> str:
        """Whether an owned song is in the library (checked now and then, a moment at most):
        "here", "missing" (Navidrome keeps it, its file is gone - perhaps for a moment: a
        disk away, files being moved) or "gone" (Navidrome does not know it). Unknown
        (Navidrome slow or failing): "here" - Navidrome answers the play itself."""
        cached = self._present.get(song_id)
        if cached is not None and cached[0] > self.clock():
            return str(cached[1])
        state = "here"
        with anyio.move_on_after(PLAY_WAIT_SECONDS):
            try:
                song = await self.navidrome.song(song_id)
            except NavidromeError:
                return "here"
            state = "gone" if song is None else "missing" if song.get("missing") else "here"
        _remember(self._present, song_id, (self.clock() + PRESENT_SECONDS, state))
        return state

    async def back(
        self,
        tracks: Iterable[tuple[CatalogTrack, str]],
        release_title: str,
        exclude: Iterable[str] = (),
    ) -> dict[str, str]:
        """Backing for new placeholders (track, placeholder song) of a release titled
        ``release_title``: placeholder -> owned song. A few searches at a time; a failing
        search leaves its track unbacked."""
        excluded = set(exclude)
        found: dict[str, str] = {}
        limit = anyio.Semaphore(4)

        async def one(track: CatalogTrack, placeholder: str) -> None:
            async with limit:
                try:
                    song = await self._search(track, release_title, excluded | {placeholder})
                except NavidromeError as exc:
                    log.info("owned recording lookup failed: %s", exc)
                    return
            if song is not None:
                found[placeholder] = song

        async with anyio.create_task_group() as tg:
            for track, placeholder in tracks:
                tg.start_soon(one, track, placeholder)
        return found

    async def _search(
        self, track: CatalogTrack, release_title: str | None, excluded: set[str]
    ) -> str | None:
        term = core_title(track.title)
        if not term:
            return None
        answer = await self.navidrome.subsonic(
            "search3",
            [
                ("query", term),
                ("songCount", str(SEARCH_COUNT)),
                ("artistCount", "0"),
                ("albumCount", "0"),
                ("musicFolderId", str(self.library_id)),
            ],
        )
        songs = [
            s
            for s in (answer.get("searchResult3") or {}).get("song") or []
            if isinstance(s, dict) and s.get("id") and str(s["id"]) not in excluded
        ]
        wanted = _isrc(track.isrc)
        by_isrc = [s for s in songs if wanted and _isrcs(s) and _same_recording(track, s, None)]
        by_title = [
            s for s in songs if not (wanted and _isrcs(s)) and _same_recording(track, s,
                                                                                release_title)
        ]  # fmt: skip
        candidates = by_isrc or by_title
        if not candidates:
            return None
        owned = await self._owned(candidates)
        if by_isrc:
            owned.sort(key=lambda s: _distance(track, s))
            return str(owned[0]["id"]) if owned else None
        # By title: only when it is the one owned song that fits.
        return str(owned[0]["id"]) if len(owned) == 1 else None

    async def _owned(self, songs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Those that are not placeholders - recorded ones, and files in the placeholder
        folder that are not recorded yet (their release's commit under way). Navidrome's
        own record gives the real path (Subsonic entries may not)."""
        ids = [str(s["id"]) for s in songs]
        placeholders = {
            str(row["song_id"])
            for row in await self.store.fetchall(
                "SELECT song_id FROM placeholders WHERE song_id IN"  # noqa: S608
                f" ({', '.join('?' for _ in ids)})",
                ids,
            )
        }
        owned = [s for s in songs if str(s["id"]) not in placeholders]
        if self.placeholder_folder is None or not owned:
            return owned
        prefix = self.placeholder_folder + "/"

        async def check(song_id: str) -> None:
            try:
                record = await self.navidrome.song(song_id)
            except NavidromeError:  # not known to be owned: left out now (the add-ons play)
                return
            if record is not None:  # one Navidrome no longer knows: left out
                _remember(self._in_folder, song_id, str(record.get("path", "")).startswith(prefix))

        async with anyio.create_task_group() as tg:  # a few at once, within the play's moment
            for song in owned:
                if str(song["id"]) not in self._in_folder:
                    tg.start_soon(check, str(song["id"]))
        return [s for s in owned if self._in_folder.get(str(s["id"])) is False]


def _remember(cache: OrderedDict[str, Any], key: str, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > 4096:
        cache.popitem(last=False)


def _isrcs(song: dict[str, Any]) -> set[str]:
    return {code for i in song.get("isrc") or [] if (code := _isrc(i))}


def _distance(track: CatalogTrack, song: dict[str, Any]) -> int:
    try:
        return abs(int(song.get("duration") or 0) * 1000 + 500 - track.duration_ms)
    except (TypeError, ValueError):
        return 1 << 30


def _same_recording(track: CatalogTrack, song: dict[str, Any], release_title: str | None) -> bool:
    """Whether an owned song (a Subsonic entry) is the catalog track's recording: by ISRC
    when both have one (``release_title`` None), else by title, artist, length, version and
    album (the single of that title, or an edition of the release)."""
    if _distance(track, song) > TOLERANCE_MS:
        return False
    wanted, owned = _isrc(track.isrc), _isrcs(song)
    if wanted and owned:
        return wanted in owned
    if recording_key(str(song.get("title") or "")) != recording_key(track.title):
        return False
    if not same_artist(str(song.get("artist") or ""), track.artist) and not same_artist(
        str(song.get("displayArtist") or ""), track.artist
    ):
        return False
    if not _versions_agree(track, song):
        return False
    album = str(song.get("album") or "")
    related = {title_key(track.title), name_key(track.title)}
    if release_title:
        related.add(title_key(release_title))
    return title_key(album) in related or name_key(album) in related


def _versions_agree(track: CatalogTrack, song: dict[str, Any]) -> bool:
    """No clean/explicit contradiction: a clean edit is other audio."""
    status = str(song.get("explicitStatus") or "")
    owned = version_marker(str(song.get("title") or "")) or {
        "explicit": "explicit",
        "clean": "clean",
    }.get(status)
    wanted = version_marker(track.title) or _version(track)
    if owned == "clean" or wanted == "clean":
        return owned == wanted
    return True
