"""Bounded Last.fm history imports and similarity evidence, with no audio/catalog role."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import anyio.lowlevel
import httpx
from aiolimiter import AsyncLimiter

from shijhon.config import PlaylistSettings
from shijhon.playlists.model import Connection, PlaylistError, music_key
from shijhon.store import Store

ENDPOINT = "https://ws.audioscrobbler.com/2.0/"


def items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("#text") or value.get("name") or "")[:300]
    return str(value or "")[:300]


class History:
    def __init__(self, store: Store, settings: PlaylistSettings) -> None:
        self.store, self.settings = store, settings
        self.http = httpx.AsyncClient(timeout=20, trust_env=False)
        self.pace = AsyncLimiter(2, 1)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def ask(
        self,
        connection: Connection,
        method: str,
        *,
        authorize: Callable[[], Awaitable[None]] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        async with self.pace:
            if authorize:
                await authorize()
            try:
                async with self.http.stream(
                    "GET",
                    ENDPOINT,
                    params={
                        "api_key": connection.history_key.get_secret_value(),
                        "method": method,
                        "format": "json",
                        **params,
                    },
                ) as response:
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body) > 2_000_000:
                            raise ValueError
                    import json

                    data = json.loads(body)
                    if (
                        response.status_code != 200
                        or not isinstance(data, dict)
                        or data.get("error")
                    ):
                        raise ValueError
                    return data
            except (httpx.HTTPError, ValueError):
                raise PlaylistError(
                    "Last.fm did not return usable data. Check the profile and API key."
                ) from None

    async def sync(
        self,
        connection: Connection,
        *,
        authorize: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if authorize:
            await authorize()
        connection.require_history()
        row = await self.store.fetchone(
            "SELECT * FROM playlist_owners WHERE id=?", [connection.user_id]
        )
        assert row is not None
        until = int(time.time()) - 1  # fixed ceiling: pagination cannot chase new scrobbles
        first = int(row["history_from"]) or until - self.settings.history_days * 86400
        start = max(first, int(row["history_until"]) - 7 * 86400)
        pending: list[tuple[Any, ...]] = []
        total_pages: int | None = None
        total_count: int | None = None
        page_size: int | None = None
        seen: set[tuple[Any, ...]] = set()
        returned = 0
        params: dict[str, Any] = {"from": start, "to": until}
        if authorize:
            params["authorize"] = authorize
        for page in range(1, self.settings.history_pages + 1):
            data = await self.ask(
                connection,
                "user.getRecentTracks",
                user=connection.history_user,
                limit=200,
                page=page,
                **params,
            )
            recent = data.get("recenttracks")
            if not isinstance(recent, dict):
                raise PlaylistError("Last.fm returned an incomplete listening-history page.")
            try:
                if "track" not in recent or not isinstance(recent["track"], (list, dict)):
                    raise ValueError
                attrs = recent["@attr"]
                declared_pages = int(attrs["totalPages"])
                pages = max(1, declared_pages)
                count = int(attrs["total"])
                per_page = int(attrs["perPage"])
                if (
                    int(attrs["page"]) != page
                    or declared_pages < 0
                    or count < 0
                    or not 1 <= per_page <= 200
                    or pages != max(1, (count + per_page - 1) // per_page)
                    or (page_size is not None and page_size != per_page)
                    or (total_count is not None and total_count != count)
                ):
                    raise ValueError
                page_size, total_count = per_page, count
                if total_pages is not None and total_pages != pages:
                    raise ValueError
                total_pages = pages
                if pages > 1 and not items(recent["track"]):
                    raise ValueError
                raw = recent["track"] if isinstance(recent["track"], list) else [recent["track"]]
                if any(not isinstance(item, dict) for item in raw):
                    raise ValueError
                played = sum(s.get("@attr", {}).get("nowplaying") != "true" for s in raw)
                if played != min(per_page, max(0, count - (page - 1) * per_page)):
                    raise ValueError
                returned += played
                for song in items(recent.get("track", [])):
                    if song.get("@attr", {}).get("nowplaying") == "true":
                        continue
                    at = int(song["date"]["uts"])
                    artist, title = text(song.get("artist")), text(song.get("name"))
                    if not artist or not title or not start <= at <= until:
                        raise ValueError
                    listen = (connection.user_id, at, artist, title, text(song.get("album")))
                    if listen in seen:
                        raise ValueError
                    seen.add(listen)
                    pending.append(listen)
            except (ValueError, KeyError, TypeError):
                raise PlaylistError(
                    "History changed during import or contains incomplete data. Retry the run."
                ) from None
            if page >= pages:
                break
        else:
            raise PlaylistError(
                "History exceeds this installation's page limit. Increase playlists.history_pages."
            )
        if total_count is not None and returned != total_count:
            raise PlaylistError("Last.fm returned an incomplete history import. Retry the run.")
        if authorize:
            await authorize()
        # Only a complete import advances the cursor. An overlap catches delayed scrobbles.
        async with self.store.transaction() as db:
            cursor = await db.execute(
                "SELECT connection,generation FROM playlist_owners WHERE id=?", [connection.user_id]
            )
            current = await cursor.fetchone()
            if (
                not current
                or Connection.model_validate_json(current["connection"]) != connection
                or current["generation"] != row["generation"]
            ):
                raise PlaylistError("The history connection changed during import. Generate again.")
            await db.executemany(
                "INSERT OR IGNORE INTO playlist_listens VALUES (?,?,?,?,?)", pending
            )
            await db.execute(
                "UPDATE playlist_owners SET history_until=?,history_from=? WHERE id=?",
                [until, first, connection.user_id],
            )
        if authorize:
            await authorize()
        rows = await self.store.fetchall(
            "SELECT at,artist,title,album FROM playlist_listens WHERE owner=? ORDER BY at DESC",
            [connection.user_id],
        )
        return [dict(r) for r in rows], first

    async def candidates(
        self, connection: Connection, listens: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not listens:
            raise PlaylistError("The imported listening history is empty.")
        now = time.time()
        weights: Counter[str] = Counter()
        examples: dict[str, dict[str, Any]] = {}
        for listen in listens:
            key = music_key(listen["artist"], listen["title"])
            weights[key] += 1 / (1 + max(0, now - listen["at"]) / (30 * 86400))
            examples.setdefault(key, listen)
        pool: dict[str, dict[str, Any]] = {}
        for key, weight in weights.most_common(12):
            seed = examples[key]
            data = await self.ask(
                connection,
                "track.getSimilar",
                artist=seed["artist"],
                track=seed["title"],
                limit=30,
                autocorrect=0,
            )
            similar = data.get("similartracks")
            if not isinstance(similar, dict):
                raise PlaylistError("Last.fm returned incomplete similarity data.")
            for song in items(similar.get("track", [])):
                artist, title = text(song.get("artist")), text(song.get("name"))
                if not artist or not title:
                    continue
                candidate = music_key(artist, title)
                try:
                    score = min(1, max(0, float(song.get("match", 0)))) * weight
                except (ValueError, TypeError):
                    continue
                entry = pool.setdefault(
                    candidate,
                    {
                        "artist": artist,
                        "title": title,
                        "album": "",
                        "duration": 0,
                        "score": 0.0,
                        "reason": "Similar to tracks in your imported history",
                    },
                )
                entry["score"] += score
            await anyio.lowlevel.checkpoint()
        # Familiar older songs can supply the requested familiar share too.
        for key, weight in weights.most_common(100):
            seed = examples[key]
            pool.setdefault(
                key,
                {
                    "artist": seed["artist"],
                    "title": seed["title"],
                    "album": seed["album"],
                    "duration": 0,
                    "score": weight * 0.1,
                    "reason": "A familiar track from your imported history",
                },
            )
        return sorted(pool.values(), key=lambda x: -x["score"])[: self.settings.candidate_limit]

    async def info(self, connection: Connection, song: dict[str, Any]) -> dict[str, Any]:
        data = await self.ask(
            connection, "track.getInfo", artist=song["artist"], track=song["title"], autocorrect=0
        )
        track = data.get("track", {})
        if not isinstance(track, dict):
            return song
        try:
            duration = max(0, int(track.get("duration") or 0) // 1000)
        except (ValueError, TypeError):
            duration = 0
        album = track.get("album", {})
        return {
            **song,
            "duration": duration,
            "album": text(album.get("title")) if isinstance(album, dict) else song["album"],
        }
