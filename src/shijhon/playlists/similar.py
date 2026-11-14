"""Similar songs and similar artists as Last.fm knows them, for the radio of catalog items
(``views/radio.py``).

Asked with a person's Last.fm application key - the one their discovery mixes use - through
the discovery's HTTP client and at its pace, so both together stay within what Last.fm asks
of an application. A request names an artist, or an artist and a title, and nothing else:
no account, no listening history. The answer is names only; what they are in the library or
the catalog is found elsewhere.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
from pydantic import SecretStr

from shijhon.playlists.history import ENDPOINT, History, items, text
from shijhon.playlists.model import is_text

SIMILAR_SECONDS = 5.0  # one Last.fm request
BODY_LIMIT = 1_000_000
NOT_FOUND = 6  # Last.fm's error for an artist or a song it does not know


class SimilarUnavailable(Exception):
    """Last.fm did not answer usably: nothing is known now."""


def _name(value: Any) -> str:
    """A name fit for a search term, or "" (controls and line breaks never are)."""
    name = " ".join(text(value).split())
    return name if is_text(name) else ""


class Similarity:
    def __init__(self, history: History) -> None:
        self.history = history  # its HTTP client, and its pace for Last.fm
        self.requests = 0  # observable in tests

    async def tracks(
        self, key: SecretStr, artist: str, title: str, limit: int
    ) -> list[tuple[str, str]]:
        """(artist, title) of the songs most similar to this one, the most similar first;
        none for a song Last.fm does not know. :class:`SimilarUnavailable` when it gives
        no usable answer."""
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

    async def artists(self, key: SecretStr, artist: str, limit: int) -> list[str]:
        """The names of the artists most similar to this one, the most similar first; none
        for an artist Last.fm does not know. :class:`SimilarUnavailable` when it gives no
        usable answer."""
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
