"""A stand-in for ListenBrainz (its Labs lookups and similarity lists, and the metadata
lookup that needs a token), with invented names and IDs: what a test says is listened
together, in the shapes ListenBrainz answers in. It records every request, and a test can
make it slow, failing or rate limited. By default (the whole suite's, ``tests/conftest.py``)
it knows nothing, so no test reaches the real service.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx

from shijhon.matching.normalize import fold

TOKEN = "invented-listenbrainz-token"


def mbid(*names: str) -> str:
    """A made-up MusicBrainz ID, the same for the same names."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "invented:" + "/".join(names)))


@dataclass
class Asked:
    method: str
    host: str
    path: str
    params: dict[str, str]
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


class FakeListenBrainz:
    def __init__(self) -> None:
        # (artist, title) as Labs finds them exactly -> its canonical recording.
        self.recordings: dict[tuple[str, str], str] = {}
        # ... and as only the metadata lookup (with the token) finds them.
        self.loose: dict[tuple[str, str], str] = {}
        self.credits: dict[str, tuple[str, list[str]]] = {}  # recording -> credit, artists
        self.similar_songs: dict[str, list[tuple[str, str]]] = {}  # recording -> songs
        self.similar_artists: dict[str, list[str]] = {}  # artist ID -> names
        # What Labs' recording search finds, in its order: (credit, title, recording).
        self.searchable: list[tuple[str, str, str]] = []
        self.requests: list[Asked] = []
        self.status = 200
        self.delay = 0.0
        self.headers: dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    # --- what a test says ---------------------------------------------------------------------

    def song(
        self,
        artist: str,
        title: str,
        similar: list[tuple[str, str]] | None = None,
        *,
        artists: list[str] | None = None,
        loose: bool = False,
    ) -> str:
        """A song ListenBrainz knows (``loose``: only its metadata lookup finds it), with the
        songs listened together with it, the most similar first; its recording's ID."""
        recording = mbid("recording", artist, title)
        (self.loose if loose else self.recordings)[(fold(artist), fold(title))] = recording
        self.credits[recording] = (artist, artists or [mbid("artist", artist)])
        if similar is not None:
            self.similar_songs[recording] = similar
        return recording

    def searched(
        self, credit: str, title: str, similar: list[tuple[str, str]] | None = None
    ) -> str:
        """A recording only Labs' recording search finds (another credit, a compilation's
        copy), with its similar songs; its ID."""
        recording = mbid("recording", credit, title, "searched")
        self.searchable.append((credit, title, recording))
        self.credits[recording] = (credit, [mbid("artist", credit)])
        if similar is not None:
            self.similar_songs[recording] = similar
        return recording

    def artist(self, name: str, titles: list[str], similar: list[str]) -> str:
        """An artist ListenBrainz knows by some of its songs, with its similar artists."""
        for title in titles:
            self.song(name, title)
        self.similar_artists[mbid("artist", name)] = similar
        return mbid("artist", name)

    def asked(self, path: str) -> list[Asked]:
        return [r for r in self.requests if r.path.startswith(path)]

    # --- answers ------------------------------------------------------------------------------

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        asked = Asked(
            request.method,
            request.url.host,
            request.url.path,
            dict(request.url.params),
            body,
            dict(request.headers),
        )
        self.requests.append(asked)
        if self.delay:
            await anyio.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, headers=self.headers, json={"error": "scripted"})
        answer = self._answer(asked)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, headers=self.headers, json=answer)

    def _answer(self, asked: Asked) -> Any:
        if asked.host == "labs.api.listenbrainz.org" and asked.path == "/acr-lookup/json":
            rows = []
            for index, row in enumerate(asked.body):
                wanted = (fold(row["artist_credit_name"]), fold(row["recording_name"]))
                if (recording := self.recordings.get(wanted)) is not None:
                    rows.append(self._row(recording, index, row))
            return rows[::-1]  # (not in the order asked)
        if asked.host == "labs.api.listenbrainz.org" and asked.path == "/similar-recordings/json":
            reference = asked.params["recording_mbids"]
            songs = self.similar_songs.get(reference, [])
            return [
                {
                    "recording_mbid": mbid("recording", artist, title),
                    "recording_name": title,
                    "artist_credit_name": artist,
                    "artist_credit_mbids": None,
                    "release_name": "Invented Release",
                    "score": 1000 - 10 * at,
                    "reference_mbid": reference,
                }
                for at, (artist, title) in enumerate(songs)
            ][::-1]  # (ordered by score only after reading)
        if asked.host == "labs.api.listenbrainz.org" and asked.path == "/recording-search/json":
            query = fold(asked.params["query"])
            return [
                {
                    "recording_name": title,
                    "recording_mbid": recording,
                    "release_name": "Invented Release",
                    "release_mbid": mbid("release", recording),
                    "artist_credit_name": credit,
                    "artist_credit_id": 1,
                }
                for credit, title, recording in self.searchable
                if fold(title.split(" (")[0]) in query
            ]
        if asked.host == "labs.api.listenbrainz.org" and asked.path == "/similar-artists/json":
            reference = asked.params["artist_mbids"]
            return [
                {
                    "artist_mbid": mbid("artist", name),
                    "name": name,
                    "comment": "",
                    "type": "Group",
                    "gender": None,
                    "score": 5000 - 10 * at,
                    "reference_mbid": reference,
                }
                for at, name in enumerate(self.similar_artists.get(reference, []))
            ]
        if asked.host == "api.listenbrainz.org" and asked.path == "/1/metadata/lookup/":
            if asked.headers.get("authorization") != f"Token {TOKEN}":
                return httpx.Response(401, json={"code": 401, "error": "Unauthorized"})
            wanted = (fold(asked.params["artist_name"]), fold(asked.params["recording_name"]))
            recording = self.loose.get(wanted) or self.recordings.get(wanted)
            if recording is None:
                return {}
            row = self._row(recording, 0, {})
            row.pop("index")
            return row
        return httpx.Response(404, text="not found")

    def _row(self, recording: str, index: int, asked: dict[str, str]) -> dict[str, Any]:
        credit, artists = self.credits[recording]
        return {
            "index": index,
            "artist_credit_arg": asked.get("artist_credit_name", ""),
            "recording_arg": asked.get("recording_name", ""),
            "artist_credit_name": credit,
            "release_name": "Invented Release",
            "recording_name": "(as MusicBrainz writes it)",
            "artist_mbids": artists,
            "release_mbid": mbid("release", recording),
            "recording_mbid": recording,
        }
