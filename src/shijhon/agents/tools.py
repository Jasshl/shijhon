"""Account-scoped music tools with explicit readiness and bounded personal reads."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, Field, ValidationError

from shijhon.agents.oauth import OAuth
from shijhon.agents.songs import (
    LIBRARY_KEYS,
    entry,
    sound_facts,
    spaced,
    summary,
    unique,
)
from shijhon.downloads import Preparation
from shijhon.downloads.model import (
    MAX_AGENT_REQUESTS,
    MAX_BATCHES,
    MAX_ENTRIES,
    ConnectionRequired,
    PreparationError,
)
from shijhon.playlists.facts import Facts, tag_key
from shijhon.playlists.model import Connection, Definition, Model, PlaylistError
from shijhon.playlists.prompt import context
from shijhon.playlists.requests import PlaylistRequest, RequestError, SetupRequired
from shijhon.playlists.resolve import native_track
from shijhon.playlists.sound import SongSound, Sound, SoundUnavailable

log = logging.getLogger(__name__)

MAX_SONG_IDS = 100
MAX_SEEDS = 20
MAX_AVOID = 20
MAX_RESULTS = 100
MAX_STEPS = 30
MAX_STEERING = 10
PLAYLIST_SEEDS = 30
PROFILE_SONGS = 500
LOOKUPS = 8
SOUND_SECONDS = 10
FACTS_SECONDS = 5
STATUS_SECONDS = 3
TAG_SECONDS = 5  # the longest one call waits for Last.fm tags
TAG_MARGIN = 3  # what the call keeps for the rest of its work
TAG_LEAST = 1.0  # a shorter wait asks Last.fm for nothing
TAG_REQUESTS = 40  # Last.fm requests in one call; tags already known cost none
SOUND_TOOLS = ["find_similar_songs", "search_by_sound", "find_sound_path"]
SOUND_SETUP = (
    "A Navidrome administrator can connect AudioMuse-AI on the Sound analysis page of Shijhon's"
    " settings."
)


class Choices(Model):
    model: str = Field(
        max_length=100,
        description="Your personal choice among administrator-approved models on the shared "
        "AI service. Blank uses the administrator's default. Does not change the host service "
        "or the external agent's model.",
    )


class PermissionRequired(RequestError):
    def __init__(self, scopes: list[str], requested: list[str]) -> None:
        super().__init__("Reconnect and grant: " + ", ".join(scopes) + ".", 403)
        self.scopes = scopes
        self.requested = requested


class Tools:
    def __init__(self, oauth: OAuth, preparation: Preparation | None = None) -> None:
        self.oauth, self.service = oauth, oauth.service
        self.preparation = preparation
        self.setup_url = oauth.base + "/shijhon/playlists"
        self.sound_url = oauth.base + "/shijhon/sound"

    async def auth(self, *scopes: str) -> AccessToken:
        current = get_access_token()
        token = await self.oauth.verify_token(current.token) if current else None
        if not token:
            raise RequestError("Reconnect this agent to Shijhon.", 401)
        if not set(scopes) <= set(token.scopes):
            raise PermissionRequired(
                [scope for scope in scopes if scope not in token.scopes],
                list(dict.fromkeys([*token.scopes, *scopes])),
            )
        return token

    @staticmethod
    def grant(token: AccessToken) -> str:
        return str((token.claims or {})["grant_id"])

    async def guarded(self, operation: Callable[[], Awaitable[dict[str, Any]]]) -> CallToolResult:
        meta = None
        try:
            with anyio.fail_after(20):
                data = {"ok": True, **await operation()}
            failed = False
        except (PlaylistError, PreparationError, ValidationError, TimeoutError) as exc:
            status = exc.status if isinstance(exc, RequestError) else 400
            data = {
                "ok": False,
                "error": {
                    "code": {
                        401: "authentication_required",
                        403: "permission_required",
                        404: "not_found",
                        409: "conflict",
                        424: "setup_required",
                    }.get(status, "request_unavailable"),
                    "message": str(exc)
                    if isinstance(exc, (PlaylistError, PreparationError))
                    else "Check the input or try again later.",
                    "setup_url": self.setup_url,
                },
            }
            if isinstance(exc, SetupRequired):
                data["error"]["missing_configuration"] = [exc.feature]
                if exc.feature == "audio_preparation":
                    data["error"]["setup_url"] = self.oauth.base + "/shijhon/downloads"
                if exc.feature == "sound_analysis":
                    data["error"]["setup_url"] = self.sound_url
            if isinstance(exc, PermissionRequired):
                data["error"]["missing_permissions"] = exc.scopes
                metadata = build_resource_metadata_url(AnyHttpUrl(self.oauth.resource))
                scope = " ".join(exc.requested)
                meta = {
                    "mcp/www_authenticate": [
                        f'Bearer error="insufficient_scope", resource_metadata="{metadata}",'
                        f' scope="{scope}"',
                    ]
                }
            failed = True
        except SoundUnavailable:
            data = {
                "ok": False,
                "error": {
                    "code": "temporarily_unavailable",
                    "message": "Sound analysis is temporarily unavailable. Try again later, or "
                    "choose songs from your own musical knowledge.",
                    "feature": "sound_analysis",
                },
            }
            failed = True
        except Exception:
            data = {
                "ok": False,
                "error": {
                    "code": "temporarily_unavailable",
                    "message": "Request unavailable. Check its status before retrying.",
                },
            }
            failed = True
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
            structured_content=data,
            is_error=failed,
            _meta=meta,
        )

    async def capabilities(self) -> dict[str, Any]:
        token = await self.auth()
        connection = await self.service.connection(str(token.subject))
        choices = await self.service.ai_choices(connection.user_id)
        ai_missing: list[str] = []
        try:
            await self.service.ai_connection(connection)
        except PlaylistError as exc:
            ai_missing = [str(exc)]
        history_missing = (
            []
            if connection.history_user and connection.history_key.get_secret_value()
            else ["Connect a Last.fm profile and application key."]
        )
        audio_missing = [] if self.preparation else ["Audio services are starting."]
        if self.preparation:
            saved = await self.preparation.repo.owner(str(token.subject))
            if saved and (
                not saved["authorization"]
                or saved["library_id"] != self.service.client.native.library_id
            ):
                audio_missing = ["Reconnect your Navidrome account on the Downloads page."]

        sound_missing, sound_reachable, sound_songs = [SOUND_SETUP], False, 0
        if self.service.sound is not None:
            try:
                with anyio.fail_after(STATUS_SECONDS):
                    status = await self.service.sound.status()
                if status.configured:
                    sound_missing = []
                    sound_reachable, sound_songs = status.reachable, status.songs
            except Exception:  # a hint source never fails the capabilities answer
                sound_missing = []  # connected; only its answer failed

        def feature(scopes: tuple[str, ...], missing: list[str]) -> dict[str, Any]:
            permissions = [scope for scope in scopes if scope not in token.scopes]
            return {
                "configured": not missing,
                "permitted": not permissions,
                "ready": not missing and not permissions,
                "missing_configuration": missing,
                "missing_permissions": permissions,
            }

        sound_feature = feature(("library",), sound_missing)
        sound_feature["ready"] = sound_feature["ready"] and sound_reachable
        return {
            "permissions": token.scopes,
            "features": {
                "library": feature(("library",), []),
                "native_playlists": feature(("library",), []),
                "song_facts": {
                    **feature(("library",), []),
                    "tools": ["get_song_facts", "get_playlist_profile"],
                    "tags": bool(connection.history_key.get_secret_value()),
                },
                "sound_analysis": {
                    **sound_feature,
                    "reachable": sound_reachable,
                    "analyzed_songs": sound_songs,
                    "tools": SOUND_TOOLS,
                    "setup_url": self.sound_url,
                },
                "listening_history": feature(("history",), history_missing),
                "agent_curated_playlist": feature(("create",), []),
                "description_playlist": feature(("create",), ai_missing),
                "recurring_description_playlist": feature(("create",), ai_missing),
                "request_status": feature(("status",), []),
                "connection_choices": feature(
                    ("settings",),
                    []
                    if choices["available"] and connection.use_shared_ai
                    else ["Select the administrator-configured shared AI service in Playlists."],
                ),
                "audio_preparation": {
                    **feature(("downloads",), audio_missing),
                    "setup_url": self.oauth.base + "/shijhon/downloads",
                },
                "audio_preparation_status": feature(
                    ("status",), [] if self.preparation else ["Audio services are starting."]
                ),
            },
            "server_ai": {
                "configured": not ai_missing,
                "health": "not_checked",
                "personal_model": choices["model"],
                "default_model": choices["default_model"],
                "allowed_models": choices["allowed_models"],
            },
            "recurrence": {
                "executor": "shijhon",
                "cadences": ["daily", "weekly"],
                "requires_server_ai": True,
            },
            "setup": {
                "url": self.setup_url,
                "agent_configurable": [
                    "your model choice among administrator-approved shared AI models",
                ],
                "browser_required": [
                    "AI connection credentials and endpoint",
                    "Last.fm account and history application key",
                    "AudioMuse-AI connection (Navidrome administrators, Sound analysis page)",
                ],
                "installation_required": [
                    "install and authenticate a server CLI runner",
                    "enable private API access",
                ],
            },
            "limits": {
                "playlist_tracks": 100,
                "read_page": 100,
                "song_facts_songs": MAX_SONG_IDS,
                "sound_seed_songs": MAX_SEEDS,
                "sound_avoid_songs": MAX_AVOID,
                "sound_results": MAX_RESULTS,
                "sound_path_steps": MAX_STEPS,
                "audio_preparation_entries": self.audio_entries,
                "audio_preparation_jobs": MAX_BATCHES,
                "audio_preparation_keys": MAX_AGENT_REQUESTS,
            },
            "notes": [
                "Read permissions do not imply consent to use personal context for every task.",
                "Library membership is not listening evidence. Entries can include placeholders.",
                "Song facts, Last.fm tags and sound analysis are approximate hints and many "
                "songs have none. Your own musical knowledge comes first; a missing fact is "
                "never a reason to drop a song.",
                "Configured AI has not been tested by a model call. Failures report an error.",
                "OAuth authorizes music access, without delegating the agent's AI subscription.",
            ],
        }

    @property
    def audio_entries(self) -> int:
        """The longest playlist an audio preparation takes (the playlist import limit)."""
        return self.preparation.max_entries if self.preparation else MAX_ENTRIES

    def audio(self) -> Preparation:
        if self.preparation is None:
            raise SetupRequired("audio_preparation", "Audio services are starting.")
        return self.preparation

    async def prepare_audio(self, playlist_id: str, key: str) -> dict[str, Any]:
        token = await self.auth("downloads")
        owner, creator = str(token.subject), self.grant(token)
        connection = await self.service.connection(owner)

        async def check() -> None:
            fresh = await self.auth("downloads")
            if str(fresh.subject) != owner or self.grant(fresh) != creator:
                raise RequestError("Reconnect this agent to Shijhon.", 401)

        service = self.audio()
        try:
            ident = await service.prepare(
                owner,
                connection.username,
                playlist_id,
                creator=creator,
                idempotency_key=key,
                authorize=check,
            )
        except ConnectionRequired as exc:
            raise SetupRequired("audio_preparation", str(exc)) from None
        result = await service.repo.agent_status(owner, creator, ident)
        await check()
        return {"batch": result}

    async def audio_status(self, ident: str, offset: int, limit: int) -> dict[str, Any]:
        token = await self.auth("status")
        entries = self.audio_entries
        if not ident or len(ident) > 100 or not 0 <= offset <= entries or not 1 <= limit <= 100:
            raise RequestError(f"Use a batch ID, an offset up to {entries} and 1 to 100 songs.")
        result = await self.audio().repo.agent_status(
            str(token.subject), self.grant(token), ident, offset, limit
        )
        await self.auth("status")
        return {"batch": result}

    async def audio_list(self, offset: int, limit: int) -> dict[str, Any]:
        token = await self.auth("status")
        if not 0 <= offset <= 1000 or not 1 <= limit <= 100:
            raise RequestError("Use an offset up to 1000 and 1 to 100 batches.")
        rows = await self.audio().repo.store.fetchall(
            "SELECT id,playlist_id,title,state,error,created FROM audio_batches"
            " WHERE owner=? AND creator=? ORDER BY created DESC,id LIMIT ? OFFSET ?",
            [str(token.subject), self.grant(token), limit + 1, offset],
        )
        await self.auth("status")
        return {
            "batches": [dict(row) for row in rows[:limit]],
            "offset": offset,
            "has_more": len(rows) > limit,
            "location": "server",
        }

    async def audio_manage(self, ident: str, action: str) -> dict[str, Any]:
        token = await self.auth("downloads")
        if not ident or len(ident) > 100 or action not in ("retry", "resume", "cancel"):
            raise RequestError("Use a batch ID and retry, resume or cancel.")
        owner, creator = str(token.subject), self.grant(token)
        connection = await self.service.connection(owner)

        async def check() -> None:
            await self.auth("downloads")

        service = self.audio()
        try:
            await service.action(
                owner, connection.username, ident, action, creator=creator, authorize=check
            )
        except ConnectionRequired as exc:
            raise SetupRequired("audio_preparation", str(exc)) from None
        result = await service.repo.agent_status(owner, creator, ident)
        await check()
        return {"batch": result}

    def sound_source(self) -> Sound:
        if self.service.sound is None:
            raise SetupRequired(
                "sound_analysis",
                f"Sound analysis is not set up. {SOUND_SETUP} Meanwhile choose songs from your "
                "own musical knowledge.",
            )
        return self.service.sound

    @staticmethod
    async def bounded[T](work: Awaitable[T]) -> T:
        """One call to the analyzer, which may be slow or away; never past its deadline."""
        try:
            with anyio.fail_after(SOUND_SECONDS):
                return await work
        except TimeoutError:
            raise SoundUnavailable("The analyzer did not answer in time.") from None

    async def facts(self, ids: list[str]) -> tuple[dict[str, SongSound], str]:
        """Sound facts as hints: a missing or failing analyzer only means there are none."""
        sound = self.service.sound
        if sound is None:
            return {}, "not_configured"
        if not ids:
            return {}, "ok"
        try:
            with anyio.fail_after(FACTS_SECONDS):
                found = await sound.facts(ids)
        except Exception:
            return {}, "unavailable"
        wanted = set(ids)
        return {key: value for key, value in found.items() if key in wanted}, "ok"

    async def add_sound(self, entries: list[dict[str, Any]]) -> str:
        """Put each entry's sound facts on it, if it has some; the state of the analyzer."""
        sounds, state = await self.facts([e["id"] for e in entries if "id" in e])
        for item in entries:
            if item.get("id") in sounds:
                item["sound"] = sound_facts(sounds[item["id"]])
        return state

    @staticmethod
    def tag_wait() -> float:
        """Seconds a call can still give Last.fm: its own limit, less what the call's deadline
        needs for the rest of its work."""
        left = anyio.current_effective_deadline() - anyio.current_time()
        return max(0.0, min(TAG_SECONDS, left - TAG_MARGIN))

    async def add_tags(
        self,
        facts: Facts,
        songs: Iterable[Mapping[str, Any]],
        entries: Sequence[dict[str, Any]],
        *,
        fetch: bool,
    ) -> str:
        """Put each entry's Last.fm tags on it and say how that went: ``not_configured`` (no
        Last.fm key), ``cached_only`` (nothing was asked for), ``ok``, ``partial`` (time or
        the request limit ran out first: ask again for more) or ``unavailable``. Tags known
        from earlier are used first; a missing, slow or failing Last.fm never fails the call."""
        if facts.tag_status == "off":
            return "not_configured"
        tracks = []
        for raw in songs:
            try:
                tracks.append(native_track(dict(raw)))
            except (ValueError, TypeError, KeyError):
                continue  # no artist or title to look up
        wait = self.tag_wait() if fetch else 0.0
        facts.requests = TAG_REQUESTS
        try:
            await facts.lookup_tags(tracks, cached_only=wait < TAG_LEAST, budget=wait)
        except Exception as exc:  # a hint source: the tool still answers
            log.warning("agent tags unavailable: %s", type(exc).__name__)
            return "unavailable"
        for item in entries:
            if tags := facts.song_tags.get(str(item.get("id"))):
                item["tags"] = tags
        if facts.tag_status != "used":
            return "unavailable"
        if not fetch:
            return "cached_only"
        unasked = any(tag_key(t.artist, t.title) not in facts.tags for t in tracks)
        return "partial" if unasked else "ok"

    async def cached_tags(
        self,
        connection: Connection,
        songs: Iterable[Mapping[str, Any]],
        entries: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Tags already known, on list entries: nothing is asked of Last.fm, so lists stay
        fast. ``{"tags": "cached_only"}`` when a Last.fm key makes that worth saying."""
        state = await self.add_tags(Facts(self.service, connection), songs, entries, fetch=False)
        return {} if state == "not_configured" else {"tags": state}

    @staticmethod
    def song_ids(values: list[str], most: int, name: str) -> list[str]:
        if len(values) > most or not all(
            isinstance(v, str) and 0 < len(v) <= 100 and v.isprintable() for v in values
        ):
            raise RequestError(
                f"Use up to {most} Navidrome song IDs in {name}, "
                "each a printable string of at most 100 characters."
            )
        return unique(values)

    async def read_songs(
        self, connection: Connection, ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Songs as this account's library shows them, and the IDs it cannot read."""
        found: dict[str, dict[str, Any]] = {}
        limiter = anyio.CapacityLimiter(LOOKUPS)

        async def one(ident: str) -> None:
            async with limiter:
                try:
                    reply = await self.service.client.call(connection, "getSong", [("id", ident)])
                except PlaylistError:
                    return
            song = reply.get("song")
            if isinstance(song, dict) and song.get("id") == ident:
                found[ident] = song

        async with anyio.create_task_group() as group:
            for ident in ids:
                group.start_soon(one, ident)
        if ids and not found:
            # Unknown IDs are not an outage: if Navidrome itself is away, say so.
            await self.service.client.call(connection, "ping")
        return found, [ident for ident in ids if ident not in found]

    async def owned_playlist(self, connection: Connection, playlist_id: str) -> dict[str, Any]:
        if not playlist_id or len(playlist_id) > 200:
            raise RequestError("Use a native playlist ID.")
        playlist = await self.service.client.playlist(connection, playlist_id)
        if playlist.get("owner") != connection.username:
            raise RequestError("This playlist belongs to another owner.", 404)
        return playlist

    async def with_sound(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        state = await self.add_sound(entries)
        return {
            "sound_analysis": state,
            "songs_without_sound_facts": sum(1 for item in entries if "sound" not in item),
        }

    async def library(
        self, query: str, offset: int, limit: int, include_sound: bool = False
    ) -> dict[str, Any]:
        token = await self.auth("library")
        connection = await self.service.connection(str(token.subject))
        if len(query) > 300 or not 0 <= offset <= 100_000 or not 1 <= limit <= 100:
            raise RequestError(
                "Use a query up to 300 characters, a bounded offset and 1 to 100 results."
            )
        reply = await self.service.client.call(
            connection,
            "search3",
            [
                ("query", query),
                ("songCount", str(limit)),
                ("songOffset", str(offset)),
                ("artistCount", "0"),
                ("albumCount", "0"),
                ("musicFolderId", self.service.requests.library),
            ],
        )
        songs = reply.get("searchResult3", {}).get("song", [])[:limit]
        entries = [entry(song, LIBRARY_KEYS) for song in songs]
        extra = await self.with_sound(entries) if include_sound else {}
        extra.update(await self.cached_tags(connection, songs, entries))
        await self.auth("library")
        return {
            "tracks": entries,
            "offset": offset,
            "limit": limit,
            "has_more": len(songs) == limit,
            **extra,
        }

    async def history(self, limit: int) -> dict[str, Any]:
        token = await self.auth("history")
        if not 1 <= limit <= 100:
            raise RequestError("Read 1 to 100 history entries at a time.")
        connection = await self.service.connection(str(token.subject))
        try:
            connection.require_history()
        except PlaylistError as exc:
            raise SetupRequired("listening_history", str(exc)) from None

        async def check() -> None:
            await self.auth("history")
            if await self.service.repo.connection(str(token.subject)) != connection:
                raise RequestError(
                    "The history connection changed. Read its current settings again."
                )

        data = await context(
            self.service, connection, history=True, library=False, limit=limit, authorize=check
        )
        return {
            **data,
            "source": "connected_lastfm_history",
            "window_days": self.service.settings.history_days,
            "format": "recording_listen_counts",
        }

    async def playlists(
        self, playlist_id: str, offset: int, limit: int, include_sound: bool = False
    ) -> dict[str, Any]:
        token = await self.auth("library")
        if not 0 <= offset <= 100_000 or not 1 <= limit <= 100:
            raise RequestError("Use a bounded offset and 1 to 100 results.")
        connection = await self.service.connection(str(token.subject))
        if playlist_id:
            playlist = await self.owned_playlist(connection, playlist_id)
            tracks = playlist.get("entry", [])[offset : offset + limit]
            entries = [entry(song) for song in tracks]
            result = {
                "playlist": {
                    key: playlist[key]
                    for key in ("id", "name", "songCount", "duration")
                    if key in playlist
                },
                "tracks": entries,
                "offset": offset,
                "has_more": offset + limit < len(playlist.get("entry", [])),
                **(await self.with_sound(entries) if include_sound else {}),
                **await self.cached_tags(connection, tracks, entries),
            }
        else:
            reply = await self.service.client.call(connection, "getPlaylists")
            playlists = [
                p
                for p in reply.get("playlists", {}).get("playlist", [])
                if p.get("owner") == connection.username
            ]
            result = {
                "playlists": [
                    {key: p[key] for key in ("id", "name", "songCount", "duration") if key in p}
                    for p in playlists[offset : offset + limit]
                ],
                "offset": offset,
                "has_more": offset + limit < len(playlists),
            }
        await self.auth("library")
        return result

    async def song_facts(self, song_ids: list[str]) -> dict[str, Any]:
        token = await self.auth("library")
        ids = self.song_ids(song_ids, MAX_SONG_IDS, "song_ids")
        if not ids:
            raise RequestError("Give at least one song ID.")
        connection = await self.service.connection(str(token.subject))
        found, missing = await self.read_songs(connection, ids)
        readable = [found[ident] for ident in ids if ident in found]
        entries = [entry(song) for song in readable]
        state = await self.add_sound(entries)
        tags = await self.add_tags(Facts(self.service, connection), readable, entries, fetch=True)
        await self.auth("library")
        return {
            "songs": entries,
            "unreadable_song_ids": missing,
            "without_sound_facts": [item["id"] for item in entries if "sound" not in item],
            "sound_analysis": state,
            "tags": tags,
        }

    async def playlist_profile(self, playlist_id: str, limit: int) -> dict[str, Any]:
        token = await self.auth("library")
        if not 1 <= limit <= 100:
            raise RequestError("List 1 to 100 songs.")
        connection = await self.service.connection(str(token.subject))
        playlist = await self.owned_playlist(connection, playlist_id)
        everything = [song for song in playlist.get("entry", []) if isinstance(song, dict)]
        entries = [entry(song) for song in everything[:PROFILE_SONGS]]
        sounds, state = await self.facts([item["id"] for item in entries if "id" in item])
        listed = entries[:limit]
        for item in listed:
            if item.get("id") in sounds:
                item["sound"] = sound_facts(sounds[item["id"]])
        # The listed songs get their tags looked up; the rest of the summary uses the ones
        # already known, so a long playlist does not wait for Last.fm.
        facts = Facts(self.service, connection)
        tags = await self.add_tags(facts, everything[: len(listed)], listed, fetch=True)
        await self.add_tags(
            facts, everything[len(listed) : len(entries)], entries[len(listed) :], fetch=False
        )
        await self.auth("library")
        return {
            "playlist": {
                key: playlist[key]
                for key in ("id", "name", "songCount", "duration")
                if key in playlist
            },
            "summary": {**summary(entries, sounds), "playlist_songs": len(everything)},
            "sound_analysis": state,
            "tags": tags,
            "songs": listed,
        }

    async def sound_songs(
        self, connection: Connection, ids: list[str], limit: int
    ) -> dict[str, Any]:
        """Songs an analyzer suggested, as this account sees them with their sound facts."""
        ids = unique(ids)[:limit]
        found, missing = await self.read_songs(connection, ids)
        readable = [found[ident] for ident in ids if ident in found]
        entries = [entry(song) for song in readable]
        state = await self.add_sound(entries)
        tags = await self.add_tags(Facts(self.service, connection), readable, entries, fetch=True)
        return {
            "songs": entries,
            "unreadable_song_ids": missing,
            "sound_analysis": state,
            "tags": tags,
        }

    async def similar(
        self, seeds: list[str], playlist_id: str, avoid: list[str], count: int
    ) -> dict[str, Any]:
        token = await self.auth("library")
        seed_ids = self.song_ids(seeds, MAX_SEEDS, "seed_song_ids")
        avoid_ids = self.song_ids(avoid, MAX_AVOID, "avoid_song_ids")
        if not 1 <= count <= MAX_RESULTS:
            raise RequestError(f"Ask for 1 to {MAX_RESULTS} songs.")
        if not seed_ids and not playlist_id:
            raise RequestError("Give seed_song_ids, a playlist_id, or both.")
        if set(seed_ids) & set(avoid_ids):
            raise RequestError("A song cannot be both a seed and one to avoid.")
        sound = self.sound_source()
        connection = await self.service.connection(str(token.subject))
        members: list[str] = []
        if playlist_id:
            playlist = await self.owned_playlist(connection, playlist_id)
            members = unique(
                str(song["id"])
                for song in playlist.get("entry", [])
                if isinstance(song, dict) and song.get("id")
            )
            if not members:
                raise RequestError("This playlist has no songs to use as seeds.")
        readable, unreadable = await self.read_songs(connection, seed_ids)
        if seed_ids and not readable and not members:
            raise RequestError("None of the seed songs could be read.", 404)
        sampled = spaced(members, PLAYLIST_SEEDS)
        used = unique([*readable, *sampled])
        # A reference playlist is the starting point: its own songs are not news.
        skip = {*members, *avoid_ids, *used}
        ids = await self.bounded(
            sound.similar(used, avoid=avoid_ids, count=min(count + len(members), MAX_RESULTS))
        )
        result = await self.sound_songs(
            connection, [ident for ident in unique(ids) if ident not in skip], count
        )
        await self.auth("library")
        return {
            "seeds": [entry(song) for song in readable.values()],
            "playlist_seed_songs": len(sampled),
            "unreadable_seed_ids": unreadable,
            **result,
        }

    async def by_sound(
        self, text: str, more: list[str], less: list[str], count: int
    ) -> dict[str, Any]:
        token = await self.auth("library")
        if not text.strip() or len(text) > 300:
            raise RequestError("Describe the sound in 1 to 300 characters.")
        if (
            len(more) > MAX_STEERING
            or len(less) > MAX_STEERING
            or not all(v.strip() and len(v) <= 100 for v in [*more, *less])
        ):
            raise RequestError(
                f"Use up to {MAX_STEERING} short phrases (1 to 100 characters) in each of "
                "more and less."
            )
        if not 1 <= count <= MAX_RESULTS:
            raise RequestError(f"Ask for 1 to {MAX_RESULTS} songs.")
        sound = self.sound_source()
        connection = await self.service.connection(str(token.subject))
        ids = await self.bounded(sound.search(text.strip(), more=more, less=less, count=count))
        result = await self.sound_songs(connection, ids, count)
        await self.auth("library")
        return result

    async def sound_path(self, start: str, end: str, steps: int) -> dict[str, Any]:
        token = await self.auth("library")
        ends = self.song_ids([start, end], 2, "start_song_id and end_song_id")
        if len(ends) != 2:
            raise RequestError("Choose two different songs.")
        if not 2 <= steps <= MAX_STEPS:
            raise RequestError(f"Use 2 to {MAX_STEPS} steps.")
        sound = self.sound_source()
        connection = await self.service.connection(str(token.subject))
        _, unreadable = await self.read_songs(connection, ends)
        if unreadable:
            raise RequestError("The start or end song could not be read.", 404)
        ids = await self.bounded(sound.path(start, end, steps=steps))
        result = await self.sound_songs(connection, ids, steps)
        await self.auth("library")
        return result

    async def create(self, request: PlaylistRequest, idempotency_key: str) -> dict[str, Any]:
        token = await self.auth(
            "create",
            *(("history",) if request.use_history else ()),
            # Chosen references are library reads, like the library context.
            *(
                ("library",)
                if request.use_library or request.reference_playlists or request.reference_songs
                else ()
            ),
        )
        async with self.service.changes:
            ident, created = await self.service.requests.create(
                str(token.subject), idempotency_key, request, creator=self.grant(token)
            )
            return {
                "created": created,
                "request": await self.service.requests.status(
                    str(token.subject), ident, creator=self.grant(token)
                ),
            }

    async def request(self, ident: str, action: str = "status") -> dict[str, Any]:
        token = await self.auth("status" if action == "status" else "create")
        owner, grant_id = str(token.subject), self.grant(token)
        if not ident or len(ident) > 100:
            raise RequestError("Use a playlist request ID.")
        async with self.service.changes:
            token = await self.auth("status" if action == "status" else "create")
            owner, grant_id = str(token.subject), self.grant(token)
            await self.service.requests.owned(owner, ident, grant_id)
            mix = await self.service.repo.mix(ident, owner)
            definition = Definition.model_validate_json(mix["definition"])
            if action not in ("status", "pause", "forget_edits"):
                await self.auth(
                    "create",
                    *(("history",) if definition.use_history else ()),
                    *(("library",) if definition.use_library else ()),
                )
                await self.service.requests.guard(mix)
            if action == "retry":
                await self.service.requests.retry(owner, ident, creator=grant_id)
            elif action == "forget_edits":
                # Like a pause: it reads nothing and calls no model.
                forgotten = await self.service.requests.forget_edits(owner, ident, creator=grant_id)
                status = await self.service.requests.status(owner, ident, creator=grant_id)
                return {"request": status, "forgotten": forgotten}
            elif action in ("pause", "resume", "refresh"):
                if action == "pause":
                    await self.service.repo.pause(ident)
                else:
                    connection = await self.service.connection(owner)
                    self.service.requests.usable_ai(connection)
                    if not definition.save_instructions:
                        raise RequestError("This playlist has no saved refresh instructions.")
                    if action == "resume":
                        if not definition.interval or not mix["published"]:
                            raise RequestError(
                                "Publish a playlist with daily or weekly instructions first."
                            )
                        await self.service.repo.store.execute(
                            "UPDATE playlist_mixes SET enabled=1 WHERE id=?", [ident]
                        )
                    else:
                        await self.service.repo.enqueue(ident)
                self.service.notify()
            return {"request": await self.service.requests.status(owner, ident, creator=grant_id)}

    async def listing(self, limit: int) -> dict[str, Any]:
        token = await self.auth("status")
        if not 1 <= limit <= 100:
            raise RequestError("List 1 to 100 requests at a time.")
        rows = await self.service.repo.store.fetchall(
            "SELECT id FROM playlist_requests WHERE owner=? AND creator=?"
            " ORDER BY rowid DESC LIMIT ?",
            [token.subject, self.grant(token), limit],
        )
        return {
            "requests": [
                await self.service.requests.status(
                    str(token.subject), r["id"], creator=self.grant(token)
                )
                for r in rows
            ]
        }

    async def configure(self, choices: Choices) -> dict[str, Any]:
        token = await self.auth("settings")
        async with self.service.changes:
            await self.auth("settings")
            await self.service.choose_ai_model(str(token.subject), choices.model)
        return {"updated": ["model"], "capabilities": await self.capabilities()}


def register_tools(mcp: MCPServer, oauth: OAuth, preparation: Preparation | None = None) -> None:
    tools = Tools(oauth, preparation)
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

    @mcp.tool(annotations=read)
    async def get_capabilities() -> CallToolResult:
        """Report configured features, granted permissions, limits and exact setup steps, including
        whether sound analysis answers. Does not read personal music or call a model.
        """
        return await tools.guarded(tools.capabilities)

    @mcp.tool(annotations=read)
    async def search_library(
        query: str = "", offset: int = 0, limit: int = 30, include_sound: bool = False
    ) -> CallToolResult:
        """Search or browse this user's library. Use only when the user requests library
        data/context. Library entries may include placeholders; membership is not
        listening evidence. Entries show year, genres and, when the files have them, bpm
        and moods, and Last.fm tags the server already knows (none are looked up here; use
        get_song_facts for that). include_sound adds approximate sound-analysis hints (tempo,
        key, energy and feature percentiles within this library, labels) to the songs that
        have them; many songs have none, and that says nothing against a song.
        """
        return await tools.guarded(lambda: tools.library(query, offset, limit, include_sound))

    @mcp.tool(annotations=read)
    async def get_listening_history(limit: int = 30) -> CallToolResult:
        """Read bounded recording/listen-count metadata from the connected history source. Requires
        the user's request to use history; library data cannot substitute for it.
        """
        return await tools.guarded(lambda: tools.history(limit))

    @mcp.tool(annotations=read)
    async def get_native_playlists(
        playlist_id: str = "", offset: int = 0, limit: int = 30, include_sound: bool = False
    ) -> CallToolResult:
        """List the user's native playlists, or read one owned playlist's ordered tracks with
        year, genres and (when the files have them) bpm and moods, plus Last.fm tags the
        server already knows. include_sound adds approximate sound-analysis hints to the
        songs that have them. No server AI is
        needed. To relate a new request to a playlist, pass its ID as a reference to
        create_playlist; get_playlist_profile summarizes what it sounds like.
        """
        return await tools.guarded(
            lambda: tools.playlists(playlist_id, offset, limit, include_sound)
        )

    @mcp.tool(annotations=read)
    async def get_song_facts(song_ids: list[str]) -> CallToolResult:
        """Facts about up to 100 library songs by Navidrome song ID (from search_library or
        get_native_playlists): year, genres, bpm and moods when present, up to five Last.fm
        tags (when the user connected a Last.fm key; looked up for these songs within a few
        seconds, so a long list may be partly tagged and a second call adds more), and
        approximate sound-analysis hints (tempo, key, energy and feature percentiles within
        this library, labels) for the songs that have them. Songs without sound facts are
        listed; most libraries have many. Facts are hints, never a reason to reject a song:
        your own knowledge of the music comes first. Works without tags or sound analysis,
        with fewer facts.
        """
        return await tools.guarded(lambda: tools.song_facts(song_ids))

    @mcp.tool(annotations=read)
    async def get_playlist_profile(playlist_id: str, limit: int = 30) -> CallToolResult:
        """Describe one owned native playlist: its first songs (1 to 100, default 30) with facts,
        and a summary over up to 500 songs: year range, top genres, medians and spreads of
        energy, tempo and feature percentiles (percentiles compare songs within this library),
        top sound labels and Last.fm tags, and how many songs have sound facts or tags. Use
        it to see what an existing
        playlist is like, for example before building something more energetic. Hints only;
        works without sound analysis, with less in the summary.
        """
        return await tools.guarded(lambda: tools.playlist_profile(playlist_id, limit))

    @mcp.tool(annotations=read)
    async def find_similar_songs(
        seed_song_ids: list[str] | None = None,
        playlist_id: str = "",
        avoid_song_ids: list[str] | None = None,
        count: int = 30,
    ) -> CallToolResult:
        """Find library songs that sound like seed songs (up to 20 Navidrome song IDs) and/or an
        owned playlist, and unlike up to 20 songs to avoid. Returns up to 100 songs, best
        first, each with artist, title, album, sound facts and Last.fm tags when known; seeds
        and the playlist's own songs are left out. Needs sound analysis; says so when it is
        not set up or not reachable. Suggestions are hints: judge them with your own
        knowledge of the music and submit exact artist/title tracks to create_playlist.
        """
        return await tools.guarded(
            lambda: tools.similar(seed_song_ids or [], playlist_id, avoid_song_ids or [], count)
        )

    @mcp.tool(annotations=read)
    async def search_by_sound(
        text: str,
        more: list[str] | None = None,
        less: list[str] | None = None,
        count: int = 30,
    ) -> CallToolResult:
        """Find library songs from an English description of their sound (for example "slow
        dreamy piano"), steered with up to 10 short phrases each to push toward (more) and away
        from (less), such as more ["energetic"], less ["acoustic"]. Returns up to 100 songs,
        best first, each with artist, title, album and sound facts. Needs sound analysis;
        says so when it is not set up or not reachable. Results are hints, not a filter.
        """
        return await tools.guarded(lambda: tools.by_sound(text, more or [], less or [], count))

    @mcp.tool(annotations=read)
    async def find_sound_path(
        start_song_id: str, end_song_id: str, steps: int = 12
    ) -> CallToolResult:
        """Find library songs that move gradually in sound from one Navidrome song to another
        (2 to 30 songs, both ends included), for example to order or bridge a playlist. Each has
        artist, title, album and sound facts. Needs sound analysis; says so when it is not set
        up or not reachable. Treat the path as a suggestion for your own ordering.
        """
        return await tools.guarded(lambda: tools.sound_path(start_song_id, end_song_id, steps))

    @mcp.tool(annotations=write)
    async def prepare_playlist_audio(playlist_id: str, idempotency_key: str) -> CallToolResult:
        """Prepare server audio for an owned native playlist only on explicit user request.
        Requires downloads permission, no AI/history. Snapshots up to the installation's
        playlist entry limit (limits.audio_preparation_entries in get_capabilities);
        returns a durable asynchronous batch. This does not copy files onto a device
        or change playlist membership. Keep the same key and playlist ID after a lost
        response; changed input conflicts. Inspect progress to verify completion.
        """
        return await tools.guarded(lambda: tools.prepare_audio(playlist_id, idempotency_key))

    @mcp.tool(annotations=read)
    async def get_audio_preparation(
        batch_id: str, offset: int = 0, limit: int = 30
    ) -> CallToolResult:
        """Read this connection's preparation progress and bounded song outcomes with status
        permission. complete confirms server audio availability; finished may contain
        failures. Neither confirms copies on a device. Poll a bounded number of times.
        """
        return await tools.guarded(lambda: tools.audio_status(batch_id, offset, limit))

    @mcp.tool(annotations=read)
    async def list_audio_preparations(offset: int = 0, limit: int = 30) -> CallToolResult:
        """List bounded preparation batches submitted by this OAuth connection, with status
        permission. Retains access after downloads permission is removed.
        """
        return await tools.guarded(lambda: tools.audio_list(offset, limit))

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=True
        )
    )
    async def manage_audio_preparation(
        batch_id: str, action: Literal["retry", "resume", "cancel"]
    ) -> CallToolResult:
        """Manage this connection's server preparation only when explicitly requested, with
        downloads permission. retry requeues failed songs; resume requeues paused songs
        after reauthorization or restart. cancel stops new songs and retains completed
        audio; bounded active writes and shared app downloads may finish. Read back
        progress. Does not remove files or change native playlist membership.
        """
        return await tools.guarded(lambda: tools.audio_manage(batch_id, action))

    @mcp.tool(annotations=write)
    async def create_playlist(request: PlaylistRequest, idempotency_key: str) -> CallToolResult:
        """Create and publish exactly the requested playlist. Send a description with saved server
        AI; send ordered artist/title tracks for agent curation without server AI.
        Preserve constraints in prompt and versions in version. When the user relates the
        request to a playlist or songs they have ("more energetic than my Evening mix"),
        find them with get_native_playlists or search_library and pass their IDs in
        request.reference_playlists or request.reference_songs instead of pasting names
        into the prompt. Song facts and sound analysis are approximate optional hints;
        use your own musical knowledge first and never drop a song only for missing facts.
        Daily/weekly cadence requires explicit user intent, saved instructions and server
        AI. Context defaults off. Keep one key and identical input through recovery.
        Publication may add library placeholders, not download audio. preview=true waits
        for human dashboard approval.
        """
        return await tools.guarded(lambda: tools.create(request, idempotency_key))

    @mcp.tool(annotations=read)
    async def get_playlist_request(request_id: str) -> CallToolResult:
        """Check this connection's request. Only published confirms verified native publication.
        Retain the request ID and use this after uncertain submissions.
        """
        return await tools.guarded(lambda: tools.request(request_id))

    @mcp.tool(annotations=read)
    async def list_playlist_requests(limit: int = 30) -> CallToolResult:
        """List bounded requests belonging to this OAuth connection, including refresh state."""
        return await tools.guarded(lambda: tools.listing(limit))

    @mcp.tool(annotations=write)
    async def retry_playlist_request(request_id: str) -> CallToolResult:
        """Retry the same failed/uncertain request after diagnosis, preserving frozen selections.
        Do not create another playlist or retry indefinitely.
        """
        return await tools.guarded(lambda: tools.request(request_id, "retry"))

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=False
        )
    )
    async def manage_playlist_refresh(
        request_id: str, action: Literal["pause", "resume", "refresh", "forget_edits"]
    ) -> CallToolResult:
        """Pause/resume explicitly saved recurring instructions or refresh them once, only when
        requested. The server executes refreshes while this agent is offline. Does not
        approve previews. `forget_edits` makes refreshes forget what they learned from the
        user's edits of the published playlist (removed songs may be suggested again; the
        result's `forgotten` counts the songs); use it only when the user asks. Does not
        change the Navidrome playlist.
        """
        return await tools.guarded(lambda: tools.request(request_id, action))

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        )
    )
    async def configure_playlist_connection(choices: Choices) -> CallToolResult:
        """Save this user's choice among administrator-approved shared AI models only when the
        user requests it, with settings permission. Blank uses the administrator's default.
        Shijhon uses it for description-based and recurring
        playlists; this does not change the external agent's model. Last.fm account changes,
        credentials and endpoints use the signed-in setup page; runner installation/authentication
        belongs on the server. Never ask for credentials in chat.
        """
        return await tools.guarded(lambda: tools.configure(choices))
