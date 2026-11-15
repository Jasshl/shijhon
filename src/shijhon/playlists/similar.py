"""Similar songs and similar artists as Last.fm and ListenBrainz know them, for the radio of
catalog items (``views/radio.py``) and the agents' ``find_listened_together``.

Last.fm is asked with a person's Last.fm application key - the one their discovery mixes use
- through the discovery's HTTP client and at its pace, so both together stay within what
Last.fm asks of an application. ListenBrainz (``listenbrainz.py``) needs no key and is asked
for everyone unless it is turned off. A request names an artist, or an artist and a title,
and nothing else: no account, no listening history. The answer is names only; what they are
in the library or the catalog is found elsewhere.

For a caller with a Last.fm key, Last.fm is asked and ListenBrainz never waited for: its
answer joins when it is already known (kept from an earlier request), else the answer says
that ListenBrainz's part is still to come (``Listed.waiting``) and the radio adds it in the
background (``views/radio.py``). The lists are taken in turn - Last.fm's first, then
ListenBrainz's, then Last.fm's second... - each name once. For a caller without a key,
ListenBrainz is the only source and is waited for (it bounds its own requests).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from pydantic import SecretStr

from shijhon.matching.normalize import fold
from shijhon.playlists.history import ENDPOINT, History, items, text
from shijhon.playlists.listenbrainz import ListenBrainz, ListeningUnavailable
from shijhon.playlists.model import is_text

log = logging.getLogger(__name__)

SIMILAR_SECONDS = 5.0  # one Last.fm request
BODY_LIMIT = 1_000_000
NOT_FOUND = 6  # Last.fm's error for an artist or a song it does not know

LASTFM, LISTENBRAINZ = "lastfm", "listenbrainz"
Titles = Callable[[], Awaitable[Sequence[str]]]


class SimilarUnavailable(Exception):
    """The caller's source did not answer usably: nothing is known now."""


@dataclass(frozen=True)
class Listed[T]:
    """Similar songs or artists, and whether ListenBrainz's part is still to come (asked
    in the background, it joins later)."""

    items: list[T]
    waiting: bool = False
    # False: found while the ListenBrainz token was refused or changed - not to be saved.
    kept: bool = True


def _name(value: Any) -> str:
    """A name fit for a search term, or "" (controls and line breaks never are)."""
    name = " ".join(text(value).split())
    return name if is_text(name) else ""


def in_turn[T](lists: Sequence[Sequence[T] | None], key: Callable[[T], object]) -> list[T]:
    """The lists' items in turn (each list's first, then each one's second...), each
    ``key`` once."""
    present = [items for items in lists if items]
    longest = max((len(items) for items in present), default=0)
    found: dict[object, T] = {}
    for at in range(longest):
        for listed in present:
            if at < len(listed):
                found.setdefault(key(listed[at]), listed[at])
    return list(found.values())


def song_key(song: tuple[str, str]) -> tuple[str, str]:
    return fold(song[0]), fold(song[1])


class Similarity:
    def __init__(self, history: History) -> None:
        self.history = history  # its HTTP client and pace for Last.fm; its ListenBrainz
        self.requests = 0  # Last.fm requests, observable in tests

    @property
    def listening(self) -> ListenBrainz | None:
        """ListenBrainz, when it is on."""
        listening = self.history.listenbrainz
        return listening if listening is not None and listening.on else None

    async def tracks(
        self, key: SecretStr | None, names: Sequence[tuple[str, str]], limit: int
    ) -> Listed[tuple[str, str]]:
        """(artist, title) of the songs most similar to this one - none for a song the
        sources do not know. ``names``: the song's (artist, title), then variants of it -
        Last.fm is asked for each until it knows one, ListenBrainz looks them up together.
        :class:`SimilarUnavailable` when the caller's source does not answer."""
        listening = self.listening
        if key is None:
            if listening is None:
                raise SimilarUnavailable
            try:
                found, kept = await listening.similar_songs_kept(names, limit)
            except ListeningUnavailable:
                raise SimilarUnavailable from None
            return Listed([(a, t) for a, t, _ in found], kept=kept)
        pairs = await self.lastfm_tracks(key, names, limit)
        known = listening.known_songs(names) if listening is not None else None
        theirs = [(a, t) for a, t, _ in known or ()]
        waiting = listening is not None and known is None
        return Listed(in_turn([pairs, theirs], song_key)[:limit], waiting)

    async def artists(
        self, key: SecretStr | None, artist: str, limit: int, titles: Titles | None = None
    ) -> Listed[str]:
        """The names of the artists most similar to this one - none for an artist the
        sources do not know. ``titles``: some of the artist's songs, asked for only when
        ListenBrainz is waited for and needs them to tell who it is."""
        listening = self.listening
        if key is None:
            if listening is None or titles is None:
                raise SimilarUnavailable
            try:
                return Listed(await listening.similar_artists(artist, await titles(), limit))
            except ListeningUnavailable:
                raise SimilarUnavailable from None
        names = await self._lastfm_artists(key, artist, limit)
        known = listening.known_artists(artist) if listening is not None else None
        waiting = listening is not None and known is None
        return Listed(in_turn([names, list(known or ())], fold)[:limit], waiting)

    async def lastfm_tracks(
        self, key: SecretStr, names: Sequence[tuple[str, str]], limit: int
    ) -> list[tuple[str, str]]:
        """Last.fm's similar songs of the first of the names it knows."""
        pairs: list[tuple[str, str]] = []
        for artist, title in names:
            if not pairs:
                pairs = await self._lastfm_tracks(key, artist, title, limit)
        return pairs

    # --- Last.fm ----------------------------------------------------------------------------

    async def _lastfm_tracks(
        self, key: SecretStr, artist: str, title: str, limit: int
    ) -> list[tuple[str, str]]:
        data = await self._ask(key, "track.getSimilar", artist=artist, track=title, limit=limit)
        similar = data.get("similartracks") if data is not None else {}
        if not isinstance(similar, dict):
            raise SimilarUnavailable
        found: dict[tuple[str, str], None] = {}
        for song in items(similar.get("track", [])):
            pair = (_name(song.get("artist")), _name(song.get("name")))
            if all(pair):
                found.setdefault(pair)
        return list(found)[:limit]

    async def _lastfm_artists(self, key: SecretStr, artist: str, limit: int) -> list[str]:
        data = await self._ask(key, "artist.getSimilar", artist=artist, limit=limit)
        similar = data.get("similarartists") if data is not None else {}
        if not isinstance(similar, dict):
            raise SimilarUnavailable
        found: dict[str, None] = {}
        for entry in items(similar.get("artist", [])):
            if name := _name(entry.get("name")):
                found.setdefault(name)
        return list(found)[:limit]

    async def _ask(self, key: SecretStr, method: str, **params: Any) -> dict[str, Any] | None:
        """Last.fm's answer; None when it does not know the artist or the song."""
        async with self.history.pace:
            self.requests += 1
            try:
                with anyio.fail_after(SIMILAR_SECONDS):
                    async with self.history.http.stream(
                        "GET",
                        ENDPOINT,
                        params={
                            "api_key": key.get_secret_value(),
                            "method": method,
                            "format": "json",
                            "autocorrect": "1",
                            **params,
                        },
                    ) as response:
                        body = bytearray()
                        async for part in response.aiter_bytes():
                            body.extend(part)
                            if len(body) > BODY_LIMIT:
                                raise ValueError
                data = json.loads(body)
            except (httpx.HTTPError, ValueError, TimeoutError):
                raise SimilarUnavailable from None
        if not isinstance(data, dict):
            raise SimilarUnavailable
        if data.get("error") == NOT_FOUND:
            return None
        if data.get("error") or response.status_code != 200:
            raise SimilarUnavailable
        return data
