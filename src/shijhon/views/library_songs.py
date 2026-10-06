"""Which of a catalog's songs the library has, and as which song.

A list of catalog songs shown beside the library's own - an artist's top songs - shows a
song the library has as the library's entry, never a second time as a catalog song:

- by Shijhon's own links (``track_links``: placeholders, and the owned songs of releases it
  matched): a database read, always;
- else, for an artist the library has, by asking Navidrome for the song's title: the same
  ISRC, or the same recording title, a shared artist and the same length (the rules search
  results are de-duplicated by). What was found - or not - is remembered for a while,
  so a view asks Navidrome once per song, not each time it is shown.

The entries themselves are Navidrome's own ``getSong`` answers for the client (its
credentials: its favorites, ratings and play counts), asked for in JSON.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlencode

import anyio

from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.locks import KeyedLocks
from shijhon.matching.normalize import (
    close_duration,
    core_title,
    same_artist,
    same_title,
    version_marker,
)
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.proxy.auth import CREDENTIAL_PARAMS
from shijhon.proxy.params import FORM_TYPE, RestCall
from shijhon.proxy.upstream import Upstream
from shijhon.store import Store
from shijhon.views.answers import library_answer

log = logging.getLogger(__name__)

PARALLEL = 4  # requests to Navidrome at a time, for all views together
CANDIDATES = 50  # songs of a title looked at
REMEMBER_SECONDS = 600.0  # how long a match by title (or none) is remembered
REMEMBERED = 20_000
# What makes a request the caller's: its credentials, protocol version and client name.
_CALLER = (*CREDENTIAL_PARAMS, "v", "c")
_OWN_BODY = {b"content-type", b"content-length", b"range", b"if-range", b"if-match",
             b"if-none-match", b"if-modified-since", b"if-unmodified-since"}  # fmt: skip


class Unknown(Exception):
    """Navidrome could not say which songs the library has."""


def isrcs(song: dict[str, Any]) -> list[str]:
    value = song.get("isrc")
    values = value if isinstance(value, list) else [value]
    return [str(v).upper() for v in values if isinstance(v, str) and v]


def same_song(track: CatalogTrack, song: dict[str, Any]) -> bool:
    """The same recording title, a shared artist and the same length (Navidrome's
    whole seconds)."""
    seconds = song.get("duration")
    if not isinstance(seconds, int) or not same_title(song.get("title"), track.title):
        return False
    if not same_artist(str(song.get("artist") or ""), track.artist):
        return False
    return close_duration(seconds * 1000 + 500, track.duration_ms, 3500)


def same_recording(track: CatalogTrack, song: dict[str, Any]) -> bool:
    """``same_song``, and nothing says the two are different recordings: a clean edit is
    other audio - a clean track (the catalog's flag, or its title's "(Clean)") and
    a clean library song (Navidrome's advisory status, or its title's) go only together -
    and two known ISRCs that differ are two recordings. Only such a library song stands for
    a catalog track in a list (top songs): the client would play another recording
    otherwise. (A shared ISRC is the same recording whatever the titles: asked before.)"""
    if not same_song(track, song):
        return False
    clean = song.get("explicitStatus") == "clean" or version_marker(song.get("title")) == "clean"
    if clean != (track.clean or version_marker(track.title) == "clean"):
        return False
    known = isrcs(song)
    return not (track.isrc and known and track.isrc.upper() not in known)


def as_caller(call: RestCall, name: str, params: Iterable[tuple[str, str]]) -> RestCall:
    """Another request of the same caller, answered in JSON: its credentials, version and
    client name with ``params``, sent as a form (credentials never in an address), with the
    headers that say who the client is."""
    kept = [(k, v) for k, v in call.params if k in _CALLER]
    body = urlencode([*kept, ("f", "json"), *params]).encode()
    headers = [(k, v) for k, v in call.headers if k.lower() not in _OWN_BODY]
    headers.append((b"content-type", FORM_TYPE.encode()))
    return RestCall.build(name, "POST", b"/rest/" + name.encode(), b"", headers, body)


NOT_FOUND = 70  # Navidrome's error code for a song it does not have (for this caller)


def _song(body: bytes) -> dict[str, Any] | int | None:
    """A ``getSong`` answer's entry; its error's code when it failed; None when it is
    neither."""
    try:
        document = json.loads(body)["subsonic-response"]
        if document.get("status") == "ok":
            entry = document["song"]
            return entry if isinstance(entry, dict) else None
        code = document["error"]["code"]
        return code if isinstance(code, int) else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class LibrarySongs:
    def __init__(
        self,
        store: Store,
        navidrome: NavidromeService | None,
        upstream: Upstream,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.navidrome = navidrome  # service account: which songs the library has
        self.upstream = upstream  # the client's own requests: the entries as it sees them
        self.clock = clock
        # Catalog song -> (the library's song found by its title, or None; until when).
        self._found: dict[CatalogRef, tuple[str | None, float]] = {}
        self._asking = KeyedLocks()  # one search for a song at a time, its answer shared
        self._limiter: anyio.CapacityLimiter | None = None  # ... a few at a time, all views
        self.searches = 0  # observable in tests

    def _few(self) -> anyio.CapacityLimiter:
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(PARALLEL)
        return self._limiter

    async def linked(self, tracks: Iterable[CatalogTrack]) -> dict[CatalogRef, str]:
        """The songs Shijhon's own records link to a library song."""
        refs = {str(t.ref): t.ref for t in tracks}
        found: dict[CatalogRef, str] = {}
        names = sorted(refs)
        for start in range(0, len(names), 500):
            chunk = names[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = await self.store.fetchall(
                f"SELECT track_ref, song_id FROM track_links WHERE track_ref IN ({marks})",  # noqa: S608
                chunk,
            )
            found.update({refs[str(row["track_ref"])]: str(row["song_id"]) for row in rows})
        return found

    async def native(
        self, tracks: Iterable[CatalogTrack], *, search: bool
    ) -> dict[CatalogRef, str]:
        """The library's song for each of ``tracks`` the library has; ``search``: also
        asked of Navidrome by title (an artist the library has). :class:`Unknown` when
        Navidrome cannot say."""
        wanted = list(tracks)
        found = await self.linked(wanted)
        if not search or self.navidrome is None:
            return found
        now = self.clock()
        asking = []
        for track in wanted:
            if track.ref in found:
                continue
            known = self._found.get(track.ref)
            if known is not None and known[1] > now:
                if known[0] is not None:
                    found[track.ref] = known[0]
            else:
                asking.append(track)
        if not asking:
            return found
        failed: list[str] = []
        limiter = self._few()

        async def one(track: CatalogTrack) -> None:
            async with self._asking.hold(str(track.ref)), limiter:
                known = self._found.get(track.ref)
                if known is not None and known[1] > self.clock():
                    song = known[0]  # another view asked meanwhile
                else:
                    try:
                        song = await self._by_title(track)
                    except NavidromeError as exc:
                        failed.append(type(exc).__name__)
                        return
                    self._remember(track.ref, song)
            if song is not None:
                found[track.ref] = song

        async with anyio.create_task_group() as group:
            for track in asking:
                group.start_soon(one, track)
        if failed:
            raise Unknown(failed[0])
        return found

    async def _by_title(self, track: CatalogTrack) -> str | None:
        assert self.navidrome is not None
        title = core_title(track.title) or track.title
        self.searches += 1
        answer = await self.navidrome.subsonic(
            "search3",
            [
                ("query", title),
                ("songCount", str(CANDIDATES)),
                ("artistCount", "0"),
                ("albumCount", "0"),
            ],
        )
        songs = (answer.get("searchResult3") or {}).get("song") or []
        songs = [song for song in songs if isinstance(song, dict) and song.get("id")]
        for song in songs:  # the same recording first, whatever its title's form
            if track.isrc and track.isrc.upper() in isrcs(song):
                return str(song["id"])
        return next((str(song["id"]) for song in songs if same_recording(track, song)), None)

    def _remember(self, ref: CatalogRef, song: str | None) -> None:
        now = self.clock()
        if len(self._found) >= REMEMBERED:
            self._found = {k: v for k, v in self._found.items() if v[1] > now}
            if len(self._found) >= REMEMBERED:
                self._found.clear()
        self._found[ref] = (song, now + REMEMBER_SECONDS)

    async def backing(self, songs: Iterable[str]) -> dict[str, str]:
        """The owned song each placeholder among ``songs`` plays (its backing): the
        same recording under another ID."""
        ids = sorted(set(songs))
        found: dict[str, str] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = await self.store.fetchall(
                "SELECT song_id, backing_song_id FROM placeholders"  # noqa: S608
                f" WHERE backing_song_id IS NOT NULL AND song_id IN ({marks})",
                chunk,
            )
            found.update({str(row["song_id"]): str(row["backing_song_id"]) for row in rows})
        return found

    async def entries(self, call: RestCall, songs: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Navidrome's ``getSong`` entries for the caller (JSON documents), by song ID. A
        song Navidrome says it does not have for the caller (not found) is left out;
        :class:`Unknown` when it gives no answer for one."""
        found: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        limiter = self._few()

        async def one(song: str) -> None:
            async with limiter:
                answer = await library_answer(
                    self.upstream, as_caller(call, "getSong", [("id", song)])
                )
            told = _song(answer.body) if answer is not None and answer.status == 200 else None
            if isinstance(told, dict) and str(told.get("id")) == song:
                found[song] = told
            elif told != NOT_FOUND:  # no answer, or an error other than "not found"
                failed.append(song)

        async with anyio.create_task_group() as group:
            for song in dict.fromkeys(songs):
                group.start_soon(one, song)
        if failed:
            raise Unknown(f"{len(failed)} song(s) not answered")
        return found
