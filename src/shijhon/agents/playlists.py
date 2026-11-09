"""The owner's Shijhon playlists as an agent sees and changes them: whatever made them
(the dashboard, this agent or another one), within the agent's own permissions."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from shijhon.playlists.changes import Changes, DiscoveryMix, Replacement, TrackEdit
from shijhon.playlists.edits import SHOWN, music, remembered
from shijhon.playlists.import_completion import missing_entries
from shijhon.playlists.import_formats import MAX_ROWS, Entry
from shijhon.playlists.model import Definition, Model, Suggestion
from shijhon.playlists.prompt import private, summary
from shijhon.playlists.requests import RequestError

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken

    from shijhon.agents.tools import Tools

KINDS = {
    "prompt": "described",
    "tracks": "agent_curated",
    "discovery": "discovery",
    "import": "import",
}
MODES = {kind: mode for mode, kind in KINDS.items()}
CHOICES_SHOWN = 10  # recordings offered per import entry
IMPORT_TEXT = 100_000  # characters of pasted lines


class ImportEntry(Model):
    artist: str = Field(default="", max_length=300)
    title: str = Field(default="", max_length=300)
    album: str = Field(default="", max_length=300)
    version: str = Field(default="", max_length=100)
    isrc: str = Field(default="", max_length=20)
    song_id: str = Field(default="", max_length=256, description="A Navidrome song ID.")

    def entry(self) -> Entry:
        return Entry(
            artist=self.artist,
            title=self.title,
            album=self.album,
            version=self.version,
            isrc=self.isrc,
            native_id=self.song_id,
        )


def context(definition: Definition) -> tuple[str, ...]:
    """The permissions a selection of this playlist reads personal context with."""
    history = definition.use_history or definition.mode == "discovery"
    return (*(("history",) if history else ()), *(("library",) if definition.use_library else ()))


def made_by(creator: str | None, grant: str) -> str:
    if creator is None:
        return "account"  # a discovery mix or an import
    return "dashboard" if not creator else "this_agent" if creator == grant else "another_agent"


class PlaylistTools:
    def __init__(self, tools: Tools) -> None:
        self.tools = tools
        self.service = tools.service
        self.changes = Changes(tools.service)

    # --- reading -----------------------------------------------------------------------

    async def user_edits(self, ident: str) -> dict[str, Any]:
        kept = await remembered(self.service.repo.store, ident)
        return {
            "removed": [{"song_id": t.id, **music(t)} for t in kept.removed[:SHOWN]],
            "added": [{"song_id": t.id, **music(t)} for t in kept.added[:SHOWN]],
            "remembered": {"removed": len(kept.removed), "added": len(kept.added)},
        }

    @staticmethod
    def permitted(scopes: set[str], definition: Definition) -> bool:
        """Whether these permissions read the songs of any of the owner's playlists:
        library, and history too when listening history chose them."""
        history = definition.use_history or definition.mode == "discovery"
        return "library" in scopes and ("history" in scopes or not history)

    @staticmethod
    def chose(data: dict[str, Any], grant: str) -> bool:
        """Whether this agent chose every song of the run: its own replacement. Not an
        edit or addition (they keep songs others chose), and not a retry, refresh or
        publication of someone else's selection."""
        agent = data.get("agent") or {}
        return agent.get("kind") == "replace" and agent.get("by") == grant

    async def published_songs(
        self, ident: str, shown: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]] | None:
        """The songs of the playlist's last publication, with their Navidrome IDs (also
        while a later preview waits or after a later change failed); None when ``shown``
        does not allow that run's songs."""
        row = await self.service.repo.store.fetchone(
            "SELECT data FROM playlist_runs WHERE mix=? AND state='published'"
            " ORDER BY created DESC LIMIT 1",
            [ident],
        )
        if row is None:
            return []
        data = json.loads(row["data"])
        if not shown(data):
            return None
        selected, ids = data.get("selected", []), data.get("native_ids", [])
        if len(selected) != len(ids):
            return []
        return [
            {"song_id": i, **{k: s.get(k, "") for k in ("artist", "title", "album")}}
            for s, i in zip(selected, ids, strict=True)
        ]

    async def view(self, token: AccessToken, ident: str) -> dict[str, Any]:
        """One of the owner's playlists. Each run's songs show only to the agent that made
        the request, to one with the library permission (and history too when listening
        history chose them: a discovery mix, or a description using history), or to the
        agent whose own replacement chose them. Details that tell library songs apart
        (outcomes, sources, references, the user's edits) need the library permission
        unless this agent made the request."""
        owner, grant = str(token.subject), self.tools.grant(token)
        scopes = set(token.scopes)
        await self.service.connection(owner)
        mix, definition = await self.changes.mix(owner, ident)
        kind = KINDS[definition.mode]
        if kind == "import":
            return await self.import_view(owner, ident, mix)  # counts only; entries: library
        row = await self.service.repo.store.fetchone(
            "SELECT creator FROM playlist_requests WHERE id=?", [ident]
        )
        creator = row["creator"] if row else None
        status = await self.service.requests.describe(owner, ident)
        latest = await self.service.repo.store.fetchone(
            "SELECT data FROM playlist_runs WHERE id=?", [status["run"]]
        )
        status |= {
            "kind": kind,
            "made_by": made_by(creator, grant),
            "brief": definition.brief,
            "cadence": definition.cadence,
        }
        if kind == "discovery":
            status["rules"] = {
                key: getattr(definition, key)
                for key in (
                    "unfamiliar_percent",
                    "max_per_artist",
                    "exclude_days",
                    "repeat_days",
                    "review_before_publishing",
                )
            }
        mine = creator == grant
        allowed = mine or self.permitted(scopes, definition)

        def shown(data: dict[str, Any]) -> bool:
            return allowed or self.chose(data, grant)

        published = await self.published_songs(ident, shown)
        if published is not None:
            status["published_songs"] = published
        if not allowed:
            history = definition.use_history or kind == "discovery"
            status["songs_hidden"] = (
                "Reading the songs of this playlist that you did not choose needs the "
                "library permission" + (" and the history permission." if history else ".")
            )
        if not shown(json.loads(latest["data"]) if latest else {}):
            status.pop("tracks", None)
            status.pop("resolution", None)
            status |= {
                "selected": [],
                "goal": "",
                "references": {},
                "reference_profile": {},
                "edits": {},
            }
            return status
        if mine or "library" in scopes:
            status["user_edits"] = await self.user_edits(ident)
        if not mine and "library" not in scopes:
            if "tracks" in status:
                status["tracks"] = private(status["tracks"])
                status["resolution"] = summary(status["tracks"])
            for song in status["selected"]:
                song["source"] = ""
            status |= {"references": {}, "reference_profile": {}, "edits": {}}
        return status

    async def import_view(self, owner: str, ident: str, mix: dict[str, Any]) -> dict[str, Any]:
        imports = self.service.imports
        evidence = await imports.evidence(owner, ident)
        original = json.loads(evidence["input"])
        runs = await self.service.repo.runs(ident)
        if not runs:
            raise RequestError("This import has no run yet.", 404)
        run = runs[0]
        data = run["data"]
        rows = data.get("rows", [])
        total = len(original["rows"])
        matched = sum(r["state"] == "matched" for r in rows)
        removed = sum(r["state"] == "removed" for r in rows)
        previous = await imports.publication(ident) if mix["playlist_id"] else None
        published_missing = (
            len(previous["data"].get("import_review", {}).get("excluded", [])) if previous else 0
        )
        completion = data.get("completion")
        return {
            "id": ident,
            "kind": "import",
            "made_by": "account",
            "run": run["id"],
            "state": run["state"],
            "error": run["error"],
            "playlist_id": mix["playlist_id"],
            "name": original["name"],
            "entries": total,
            "checked": min(data.get("cursor", 0), total),
            "matched": matched,
            "removed": removed,
            "missing": total - matched - removed,
            "missing_to_confirm": len(missing_entries(rows)) if len(rows) == total else None,
            "review_key": imports.review_key(data),
            "completing": bool(completion),
            "recheck_available": bool(published_missing)
            and (
                run["state"] in ("published", "discarded")
                or (run["state"] in ("preview", "failed") and bool(completion))
            ),
            "unfinished_update": imports.unfinished(run["state"], data),
        }

    async def listing(
        self, token: AccessToken, limit: int, offset: int, kind: str
    ) -> dict[str, Any]:
        owner, grant = str(token.subject), self.tools.grant(token)
        if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
            raise RequestError("List 1 to 100 playlists at a time, from a bounded offset.")
        await self.service.connection(owner)
        library = self.service.requests.library
        mode = MODES.get(kind, "")
        rows = await self.service.repo.store.fetchall(
            "SELECT m.id,m.definition,m.playlist_id,m.enabled,r.creator,"
            " (SELECT state FROM playlist_runs WHERE mix=m.id"
            "  ORDER BY created DESC LIMIT 1) AS state"
            " FROM playlist_mixes m"
            " LEFT JOIN playlist_requests r ON r.id=m.id"
            " LEFT JOIN playlist_imports i ON i.id=m.id"
            " WHERE m.owner=? AND (r.id IS NULL OR r.library=?)"
            " AND (i.id IS NULL OR i.library=?)"
            " AND (?='' OR json_extract(m.definition,'$.mode')=?)"
            " ORDER BY m.rowid DESC LIMIT ? OFFSET ?",
            [owner, library, library, mode, mode, limit + 1, offset],
        )
        playlists = []
        for row in rows[:limit]:
            spec = Definition.model_validate_json(row["definition"])
            playlists.append(
                {
                    "id": row["id"],
                    "kind": KINDS[spec.mode],
                    "name": spec.name,
                    "state": row["state"] or "",
                    "playlist_id": row["playlist_id"] or "",
                    "cadence": spec.cadence if spec.mode != "import" else "manual",
                    "refreshes": bool(row["enabled"]),
                    "made_by": made_by(
                        row["creator"] if spec.mode in ("prompt", "tracks") else None, grant
                    ),
                }
            )
        return {"playlists": playlists, "offset": offset, "has_more": len(rows) > limit}

    async def import_entries(
        self, token: AccessToken, ident: str, offset: int, limit: int, only_missing: bool
    ) -> dict[str, Any]:
        owner = str(token.subject)
        if not 1 <= limit <= 100 or not 0 <= offset <= MAX_ROWS:
            raise RequestError("Read 1 to 100 entries at a time, from a bounded offset.")
        await self.service.connection(owner)
        _, definition = await self.changes.mix(owner, ident)
        if definition.mode != "import":
            raise RequestError("This playlist is not an import.")
        evidence = await self.service.imports.evidence(owner, ident)
        original = json.loads(evidence["input"])["rows"]
        run = (await self.service.repo.runs(ident))[0]
        rows = run["data"].get("rows", [])
        pending: dict[str, Any] = {"state": "pending", "track": None}
        numbered: list[tuple[int, dict[str, Any], dict[str, Any]]] = [
            (n, value, rows[n] if n < len(rows) else pending) for n, value in enumerate(original)
        ]
        if only_missing:
            numbered = [item for item in numbered if item[2]["state"] not in ("matched", "removed")]

        def song(track: dict[str, Any] | None) -> dict[str, Any] | None:
            if not track:
                return None
            return {
                "song_id": track.get("id", ""),
                **{k: track.get(k, "") for k in ("artist", "title", "album")},
            }

        listed = []
        for n, value, row in numbered[offset : offset + limit]:
            choices = row.get("choices") or []
            listed.append(
                {
                    "entry": n + 1,
                    "input": {
                        k: value.get(k, "")
                        for k in ("artist", "title", "album", "version", "isrc", "native_id")
                        if value.get(k)
                    },
                    "state": row["state"],
                    "note": row.get("note", ""),
                    "song": song(row.get("track")),
                    "choices": [song(c["track"]) for c in choices[:CHOICES_SHOWN]],
                    "more_choices": max(len(choices) - CHOICES_SHOWN, 0)
                    + (1 if row.get("overflow") else 0),
                }
            )
        return {
            "run": run["id"],
            "state": run["state"],
            "review_key": self.service.imports.review_key(run["data"]),
            "entries": listed,
            "offset": offset,
            "has_more": offset + limit < len(numbered),
        }

    async def read_import(
        self, ident: str, offset: int, limit: int, only_missing: bool
    ) -> dict[str, Any]:
        token = await self.tools.auth("status", "library")
        result = await self.import_entries(token, ident, offset, limit, only_missing)
        await self.tools.auth("status", "library")
        return result

    # --- changes -------------------------------------------------------------------------

    async def changing(self, *scopes: str) -> tuple[str, str]:
        """Under the changes: the owner and grant, with the permissions checked again."""
        token = await self.tools.auth(*scopes)
        return str(token.subject), self.tools.grant(token)

    async def replace(self, ident: str, replacement: Replacement, key: str) -> dict[str, Any]:
        token = await self.tools.auth("create")
        async with self.service.changing():
            owner, grant = await self.changing("create")
            changed = await self.changes.replace(owner, ident, key, replacement, actor=grant)
        return {"replaced": changed, "request": await self.view(token, ident)}

    async def edit(self, ident: str, change: TrackEdit, key: str) -> dict[str, Any]:
        token = await self.tools.auth("create")
        async with self.service.changing():
            owner, grant = await self.changing("create")
            _, definition = await self.changes.mix(owner, ident)
            row = await self.service.repo.store.fetchone(
                "SELECT creator FROM playlist_requests WHERE id=?", [ident]
            )
            if not (row and row["creator"] == grant):
                # An edit works on every song of the playlist, so it needs the
                # permissions that read them (``view``).
                history = definition.use_history or definition.mode == "discovery"
                await self.changing("create", "library", *(("history",) if history else ()))
            changed = await self.changes.edit(owner, ident, key, change, actor=grant)
        return {"edited": changed, "request": await self.view(token, ident)}

    async def review(
        self,
        ident: str,
        run_id: str,
        action: Literal["publish", "discard"],
        enable: bool | None,
    ) -> dict[str, Any]:
        token = await self.tools.auth("create")
        async with self.service.changing():
            owner, grant = await self.changing("create")
            _, definition = await self.changes.mix(owner, ident)
            if action == "publish":
                # Publishing a selection made from personal context needs that permission,
                # as a refresh of it does.
                await self.changing("create", *context(definition))
            await self.changes.review(
                owner, ident, run_id, action, enable_refreshes=enable, actor=grant
            )
        return {"request": await self.view(token, ident)}

    async def discover(self, mix: DiscoveryMix, key: str) -> dict[str, Any]:
        token = await self.tools.auth("create", "history")
        async with self.service.changing():
            owner, _ = await self.changing("create", "history")
            ident, created = await self.changes.discover(owner, key, mix)
        return {"created": created, "request": await self.view(token, ident)}

    async def rediscover(self, ident: str, mix: DiscoveryMix) -> dict[str, Any]:
        token = await self.tools.auth("create", "history")
        async with self.service.changing():
            owner, _ = await self.changing("create", "history")
            await self.changes.rediscover(owner, ident, mix)
        return {"request": await self.view(token, ident)}

    async def start_import(
        self, name: str, key: str, lines: str, entries: list[ImportEntry] | None
    ) -> dict[str, Any]:
        token = await self.tools.auth("create", "library")
        if bool(lines.strip()) == bool(entries):
            raise RequestError("Send either pasted lines or entries.")
        if len(lines) > IMPORT_TEXT:
            raise RequestError(f"Send at most {IMPORT_TEXT:,} characters of lines.")
        rows = [e.entry() for e in entries] if entries else None
        async with self.service.changing():
            owner, _ = await self.changing("create", "library")
            ident = await self.service.imports.create(
                owner, key, name, lines, format="lines", entries=rows
            )
        return {"request": await self.view(token, ident)}

    async def review_import(
        self,
        ident: str,
        run_id: str,
        review_key: str,
        action: str,
        entry: int,
        choice: str,
        missing_count: int | None,
    ) -> dict[str, Any]:
        token = await self.tools.auth("create", "library")
        if len(review_key) > 100 or len(choice) > 256 or not 0 <= entry <= MAX_ROWS:
            raise RequestError("Use the import's review_key, an entry number and a song ID.")
        async with self.service.changing():
            owner, _ = await self.changing("create", "library")
            _, definition = await self.changes.mix(owner, ident)
            if definition.mode != "import":
                raise RequestError("This playlist is not an import.")
            await self.service.imports.action(
                owner,
                ident,
                run_id,
                action,
                "" if missing_count is None else str(missing_count),
                review_key,
                entry=str(entry) if entry else "",
                choice=choice,
            )
        return {"request": await self.view(token, ident)}


def register_playlist_tools(mcp: MCPServer, tools: Tools) -> None:
    playlists = tools.managed
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
    replace = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

    @mcp.tool(annotations=replace)
    async def replace_playlist_tracks(
        request_id: str,
        tracks: list[Suggestion],
        idempotency_key: str,
        max_per_artist: int | None = None,
        require_all: bool = False,
        preview: bool = False,
        brief: str | None = None,
    ) -> CallToolResult:
        """Replace all songs of one of the user's Shijhon playlists (any kind but an import)
        with your new ordered artist/title tracks, keeping its Navidrome playlist ID: how
        you refresh an agent-curated playlist. Tracks resolve as in create_playlist; songs
        the user removed from the playlist stay out (status `tracks` says
        `removed_by_user`; see `user_edits`). `brief` saves what an agent-curated playlist
        is for. A described playlist keeps its instructions and schedule. Use a new key for
        each replacement and keep it through recovery; confirm with get_playlist_request.
        """
        return await tools.guarded(
            lambda: playlists.replace(
                request_id,
                Replacement(
                    tracks=tracks,
                    max_per_artist=max_per_artist,
                    require_all=require_all,
                    preview=preview,
                    brief=brief,
                ),
                idempotency_key,
            )
        )

    @mcp.tool(annotations=replace)
    async def edit_playlist_tracks(
        request_id: str,
        idempotency_key: str,
        remove: list[str] | None = None,
        order: list[str] | None = None,
    ) -> CallToolResult:
        """Remove songs from a published Shijhon playlist (not an import) and/or reorder it,
        by Navidrome song ID (`song_id` in get_playlist_request's `published_songs`). Each
        ID in `remove` takes out one copy of that song (list it twice for two copies).
        `order` lists every remaining song in its new place, as often as it stays in the
        playlist. Songs no longer in the playlist are remembered like the user's own
        removals: refreshes and replacements leave them out (forget_edits with their
        song_ids puts them back). On a playlist you did not make, it needs the permissions
        that read its songs. Only on the user's request.
        """
        return await tools.guarded(
            lambda: playlists.edit(
                request_id, TrackEdit(remove=remove or [], order=order), idempotency_key
            )
        )

    @mcp.tool(annotations=replace)
    async def review_playlist_preview(
        request_id: str,
        run_id: str,
        action: Literal["publish", "discard"],
        enable_refreshes: bool | None = None,
    ) -> CallToolResult:
        """Publish or discard a playlist's waiting preview (state `preview`; `run` from
        get_playlist_request) when the user has seen it and asks. Publishing a discovery
        mix, or a description that used history or library, needs that permission too.
        Discovery mixes: `enable_refreshes` true or false turns their daily/weekly
        refreshes on or off with this publication; left out, it keeps what was asked
        when the mix was made.
        """
        return await tools.guarded(
            lambda: playlists.review(request_id, run_id, action, enable_refreshes)
        )

    @mcp.tool(annotations=write)
    async def create_discovery_mix(mix: DiscoveryMix, idempotency_key: str) -> CallToolResult:
        """Create a discovery mix: Shijhon picks songs similar to the user's Last.fm
        listening history, mostly ones they have not heard, on the server. Needs history
        permission and a connected Last.fm profile; `instructions` also need server AI.
        Refreshes only with an explicit user request (`enable_refreshes` and a cadence).
        """
        return await tools.guarded(lambda: playlists.discover(mix, idempotency_key))

    @mcp.tool(annotations=replace)
    async def update_discovery_mix(request_id: str, mix: DiscoveryMix) -> CallToolResult:
        """Save new rules for a discovery mix and make a new selection with them; a
        selection still waiting is discarded and refreshes stay off unless
        `enable_refreshes`. Only on the user's request.
        """
        return await tools.guarded(lambda: playlists.rediscover(request_id, mix))

    @mcp.tool(annotations=write)
    async def import_playlist(
        name: str,
        idempotency_key: str,
        lines: str = "",
        entries: list[ImportEntry] | None = None,
    ) -> CallToolResult:
        """Import a playlist list the user gives you, in its order: pasted `lines` ("Artist
        - Title" per line) or structured `entries`. Shijhon matches each entry with the
        library and the catalogs in the background; review the matches with
        get_playlist_import and publish with review_playlist_import. Needs library
        permission. Keep the key and input through recovery.
        """
        return await tools.guarded(
            lambda: playlists.start_import(name, idempotency_key, lines, entries)
        )

    @mcp.tool(annotations=read)
    async def get_playlist_import(
        request_id: str, offset: int = 0, limit: int = 30, only_missing: bool = False
    ) -> CallToolResult:
        """Read an import's entries in order: each one's state (`matched`, `ambiguous`,
        `unresolved`, `removed`, `pending`), its song, and for an ambiguous one up to 10
        `choices`. Use `review_key` and `run` from here with review_playlist_import.
        """
        return await tools.guarded(
            lambda: playlists.read_import(request_id, offset, limit, only_missing)
        )

    @mcp.tool(annotations=replace)
    async def review_playlist_import(
        request_id: str,
        run_id: str,
        review_key: str,
        action: Literal[
            "choose", "publish", "discard", "retry", "recheck", "stop", "resume", "keep"
        ],
        entry: int = 0,
        choice: str = "",
        missing_count: int | None = None,
    ) -> CallToolResult:
        """Act on an import as its dashboard page does, only as the user asks. `choose`
        sets entry `entry` to `choice` (an offered choice's song_id or a Navidrome song
        ID); `publish` needs `missing_count`, the number of entries left out, to confirm
        them; `retry` matches again; `recheck` looks again for entries missing from the
        published playlist; `stop`, `resume` and `keep` handle an update that stopped.
        """
        return await tools.guarded(
            lambda: playlists.review_import(
                request_id, run_id, review_key, action, entry, choice, missing_count
            )
        )
