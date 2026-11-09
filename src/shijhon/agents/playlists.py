"""The owner's Shijhon playlists as an agent sees and changes them: whatever made them
(the dashboard, this agent or another one), within the agent's own permissions."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import anyio
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
    from shijhon.playlists.imports import Imports

KINDS = {
    "prompt": "described",
    "tracks": "agent_curated",
    "discovery": "discovery",
    "import": "import",
}
MODES = {kind: mode for mode, kind in KINDS.items()}
CHOICES_SHOWN = 10  # recordings offered per import entry
IMPORT_TEXT = 100_000  # characters of pasted lines
UNFINISHED = ("queued", "generating", "publishing")  # a run the worker is still working on
MATCHING = ("queued", "generating")  # an import's review_key still changes in these
WRITE_WAIT = 8.0  # the longest a change's answer waits for its run
WRITE_MARGIN = 6.0  # what the call's deadline keeps for the rest of its work
WRITE_POLL = 0.2


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


def next_step(ident: str, state: str, *, live: bool) -> str:
    """What an agent does next about a playlist's latest run; empty when nothing waits."""
    keeps = " The published playlist keeps its songs until then." if live else ""
    stays = " The published playlist keeps its songs." if live else ""
    if state in UNFINISHED:
        return (
            f"This work is still running. Call get_playlist_request with request_id {ident} "
            "until state is published, preview, failed or uncertain."
            + (keeps or " playlist_id stays empty until the first publication.")
        )
    if state == "preview":
        return (
            "A preview waits for review. When the user asks, call review_playlist_preview with "
            "this run as run_id and the action publish or discard." + keeps
        )
    if state == "failed":
        return "This run failed; error says why. Call retry_playlist_request to retry it." + stays
    if state == "uncertain":
        return (
            "The result of this publication is not known. Call retry_playlist_request to check "
            "and finish it, and do not send the change again under a new key." + stays
        )
    if state == "discarded":
        return "This run was discarded and nothing was published."
    return ""


def import_key(imports: Imports, state: str, data: dict[str, Any]) -> str:
    """The key that acts on this review. While matching runs there is none: the entries
    are still changing, and so would the key."""
    return "" if state in MATCHING else imports.review_key(data)


def import_next(ident: str, state: str, data: dict[str, Any]) -> str:
    """What an agent does next about an import's run; empty when nothing waits."""
    if state in MATCHING:
        return (
            "Matching runs in the background and can take a while for a long list or a slow "
            f"catalog. Call get_playlist_import with request_id {ident} until state is preview, "
            "then use the run and review_key it returns with review_playlist_import. There is "
            "no review_key while matching runs."
        )
    if state == "preview":
        missing = len(missing_entries(data.get("rows", [])))
        text = (
            "The matches are ready. Read them with get_playlist_import. Send run and the current "
            "review_key to review_playlist_import; the key changes after each choice. "
        )
        if not missing:
            return text + "To publish, use the action publish with missing_count 0."
        text += (
            f"To publish without the {missing} entries that have no song, use the action publish "
            f"with missing_count {missing}."
        )
        if names := data.get("unanswered"):
            text += (
                f" Add-on {', '.join(names)} did not answer, so it could not check some entries. "
                "The action retry asks it again."
            )
        return text
    if state == "publishing":
        return (
            f"Publication is running. Call get_playlist_import with request_id {ident} until "
            "state is published, failed or uncertain."
        )
    if state in ("failed", "uncertain"):
        return "This run did not finish; error says why. The review action retry continues it."
    return ""


def entry_note(row: dict[str, Any], state: str) -> str:
    """An import entry's note. Nothing matched an entry an add-on could not check, which
    is not the same as a song that does not exist: say so, and what can be done."""
    names = row.get("unanswered")
    if row["state"] != "unresolved" or not names:
        return str(row.get("note", ""))
    again = "retry" if state == "preview" else "recheck"
    return (
        f"Nothing matched this entry, and add-on {', '.join(names)} did not answer, so it could "
        "not check it. It may have no match at all. Publish without this entry by counting it "
        f"in missing_count, or use the action {again} to ask again."
    )


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
        discarded: dict[str, Any] | None = None
        if status["state"] == "discarded" and mix["playlist_id"]:
            # A discarded run changed nothing: the playlist is still its last publication.
            live_run = await self.service.imports.publication(ident)
            if live_run:
                discarded = status
                status = await self.service.requests.describe(owner, ident, live_run)
        latest = await self.service.repo.store.fetchone(
            "SELECT data FROM playlist_runs WHERE id=?", [status["run"]]
        )
        live = bool(mix["playlist_id"])
        status |= {
            "live": live,
            "kind": kind,
            "made_by": made_by(creator, grant),
            "brief": definition.brief,
            "cadence": definition.cadence,
        }
        if step := next_step(ident, status["state"], live=live):
            status["next"] = step
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

        if discarded:
            run = await self.service.repo.run(discarded["run"])
            agent = run["data"].get("agent") or {}
            status["discarded_run"] = {
                "run": discarded["run"],
                "selected": [
                    {k: song[k] for k in ("artist", "title", "album")}
                    for song in discarded["selected"]
                ]
                if shown(run["data"])
                else [],
                **(
                    {"brief": agent["definition"]["brief"]}
                    if agent.get("definition", {}).get("brief") and shown(run["data"])
                    else {}
                ),
            }
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

    async def settle(self, ident: str) -> None:
        """Give the playlist's latest run a few seconds to finish, within the call's deadline.
        This only reads: the run is durable, and one that takes longer goes on regardless. The
        caller does not hold the changes, which the worker needs."""
        if not self.service.working:
            return
        left = anyio.current_effective_deadline() - anyio.current_time()
        with anyio.move_on_after(max(0.0, min(WRITE_WAIT, left - WRITE_MARGIN))):
            while True:
                row = await self.service.repo.store.fetchone(
                    "SELECT state FROM playlist_runs WHERE mix=? ORDER BY created DESC LIMIT 1",
                    [ident],
                )
                if not row or row["state"] not in UNFINISHED:
                    return
                await anyio.sleep(WRITE_POLL)

    async def answer(self, ident: str) -> dict[str, Any]:
        """The answer of a change: the playlist as it is once its run finished, or as far as
        it got. Until the run is published its songs are not the result of the change, so the
        previously published songs stay out of this answer (get_playlist_request has them)."""
        await self.settle(ident)
        view = await self.view(await self.tools.auth(), ident)
        if view["state"] != "published":
            view.pop("published_songs", None)
        return view

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
            "live": bool(mix["playlist_id"]),
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
            "review_key": import_key(imports, run["state"], data),
            "completing": bool(completion),
            "recheck_available": bool(published_missing)
            and (
                run["state"] in ("published", "discarded")
                or (run["state"] in ("preview", "failed") and bool(completion))
            ),
            "unfinished_update": imports.unfinished(run["state"], data),
            **({"next": step} if (step := import_next(ident, run["state"], data)) else {}),
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
            "  ORDER BY created DESC LIMIT 1) AS state,"
            " EXISTS(SELECT 1 FROM playlist_runs WHERE mix=m.id AND state='published')"
            "  AS has_publication"
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
            state = row["state"] or ""
            # As in ``view``: a discarded run changed nothing. An import acts on its latest
            # run, whose state it keeps showing beside ``live``.
            outlived = (
                state == "discarded"
                and row["playlist_id"]
                and row["has_publication"]
                and spec.mode != "import"
            )
            playlists.append(
                {
                    "id": row["id"],
                    "kind": KINDS[spec.mode],
                    "name": spec.name,
                    "state": "published" if outlived else state,
                    **({"latest_run_state": state} if outlived else {}),
                    "live": bool(row["playlist_id"]),
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
                    "note": entry_note(row, run["state"]),
                    "song": song(row.get("track")),
                    "choices": [song(c["track"]) for c in choices[:CHOICES_SHOWN]],
                    "more_choices": max(len(choices) - CHOICES_SHOWN, 0)
                    + (1 if row.get("overflow") else 0),
                }
            )
        return {
            "run": run["id"],
            "state": run["state"],
            "review_key": import_key(self.service.imports, run["state"], run["data"]),
            "checked": min(run["data"].get("cursor", 0), len(original)),
            "total": len(original),
            "entries": listed,
            "offset": offset,
            "has_more": offset + limit < len(numbered),
            **({"next": step} if (step := import_next(ident, run["state"], run["data"])) else {}),
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
        await self.tools.auth("create")
        async with self.service.changing():
            owner, grant = await self.changing("create")
            changed = await self.changes.replace(owner, ident, key, replacement, actor=grant)
        return {"replaced": changed, "request": await self.answer(ident)}

    async def edit(self, ident: str, change: TrackEdit, key: str) -> dict[str, Any]:
        await self.tools.auth("create")
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
        return {"edited": changed, "request": await self.answer(ident)}

    async def review(
        self,
        ident: str,
        run_id: str,
        action: Literal["publish", "discard"],
        enable: bool | None,
    ) -> dict[str, Any]:
        await self.tools.auth("create")
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
        return {"request": await self.answer(ident)}

    async def discover(self, mix: DiscoveryMix, key: str) -> dict[str, Any]:
        await self.tools.auth("create", "history")
        async with self.service.changing():
            owner, _ = await self.changing("create", "history")
            ident, created = await self.changes.discover(owner, key, mix)
        return {"created": created, "request": await self.answer(ident)}

    async def rediscover(self, ident: str, mix: DiscoveryMix) -> dict[str, Any]:
        await self.tools.auth("create", "history")
        async with self.service.changing():
            owner, _ = await self.changing("create", "history")
            await self.changes.rediscover(owner, ident, mix)
        return {"request": await self.answer(ident)}

    async def start_import(
        self, name: str, key: str, lines: str, entries: list[ImportEntry] | None
    ) -> dict[str, Any]:
        await self.tools.auth("create", "library")
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
        return {"request": await self.answer(ident)}

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
        await self.tools.auth("create", "library")
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
        return {"request": await self.answer(ident)}


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
        is for. A described playlist keeps its instructions and schedule. max_per_artist
        defaults to the playlist's own limit (2 unless it was set) and goes up to 10; pass a
        higher value when your tracks repeat an artist. Use a new key for each replacement
        and keep it through recovery. The answer waits a few seconds for the work to finish.
        Songs in its `published_songs` are the finished result; otherwise follow its `next`.
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
        when the mix was made. Discarding leaves the published playlist as it was.
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
        library and the catalogs in the background, which can take a while. Call
        get_playlist_import until its `state` is `preview`, then publish with
        review_playlist_import using the `run` and `review_key` from that answer. There is
        no `review_key` while matching runs. Needs library permission. Keep the key and
        input through recovery.
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
        `choices`. While `state` is `queued` or `generating`, matching runs in the
        background (`checked` of `total`) and `review_key` is empty, so call this again.
        From `preview` on, use `review_key` and `run` from here with
        review_playlist_import, and read the key again after each choice.
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
        them; `retry` matches again before publication; `recheck` looks again for entries
        missing from the published playlist; `stop`, `resume` and `keep` handle an update
        that stopped.
        """
        return await tools.guarded(
            lambda: playlists.review_import(
                request_id, run_id, review_key, action, entry, choice, missing_count
            )
        )
