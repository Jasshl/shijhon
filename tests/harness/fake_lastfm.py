"""A stand-in for Last.fm's similarity answers (``track.getSimilar``, ``artist.getSimilar``),
with invented names: what a test says is similar, in the shape Last.fm answers in. It
records every request's parameters, and a test can make it slow or failing.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx

KEY = "invented-application-key"


class FakeLastFm:
    def __init__(self) -> None:
        # (artist, title) -> the similar songs, as (artist, title); artist -> similar names.
        self.tracks: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.artists: dict[str, list[str]] = {}
        self.requests: list[dict[str, str]] = []
        self.status = 200
        self.delay = 0.0
        self.body: bytes | None = None  # answered as it is, when set

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def asked(self, method: str) -> list[dict[str, str]]:
        return [r for r in self.requests if r["method"] == method]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        if self.delay:
            await anyio.sleep(self.delay)
        if self.body is not None:
            return httpx.Response(self.status, content=self.body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": 11, "message": "Service Offline"})
        limit = int(params.get("limit", "100"))
        answer: dict[str, Any]
        if params["method"] == "track.getSimilar":
            songs = self.tracks.get((params["artist"], params["track"]))
            if songs is None:
                return httpx.Response(200, json={"error": 6, "message": "Track not found"})
            listed = [
                {"name": title, "match": round(1 - at / 100, 2), "artist": {"name": artist}}
                for at, (artist, title) in enumerate(songs[:limit])
            ]
            answer = {"similartracks": {"track": listed, "@attr": {"artist": params["artist"]}}}
        elif params["method"] == "artist.getSimilar":
            names = self.artists.get(params["artist"])
            if names is None:
                message = "The artist you supplied could not be found"
                return httpx.Response(200, json={"error": 6, "message": message})
            people = [
                {"name": name, "match": str(round(1 - at / 100, 2))}
                for at, name in enumerate(names[:limit])
            ]
            answer = {"similarartists": {"artist": people, "@attr": {"artist": params["artist"]}}}
        else:
            return httpx.Response(200, json={"error": 3, "message": "Invalid Method"})
        return httpx.Response(200, json=answer)
