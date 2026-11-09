"""Owner-bound creation requests and revocable, narrowly scoped agent grants."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator, model_validator

from shijhon.matching.normalize import fold
from shijhon.playlists.edits import forget
from shijhon.playlists.model import (
    EDITED,
    LIBRARY_LOST,
    REFERENCE_PLAYLISTS,
    REFERENCE_SONGS,
    Connection,
    Definition,
    Model,
    NativeId,
    PlaylistError,
    Suggestion,
    distinct,
    fingerprint,
)
from shijhon.playlists.prompt import private, summary

if TYPE_CHECKING:
    from shijhon.playlists.service import Playlists


class RequestError(PlaylistError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Busy(RequestError):
    """Another playlist change (usually a publication) held the changes for too long.
    Nothing was changed, so the same call can be sent again."""

    def __init__(self) -> None:
        super().__init__(
            "Shijhon is busy publishing a playlist and nothing was changed. Send the same call "
            "again in a minute, with the same idempotency key if it has one.",
            503,
        )


class SetupRequired(RequestError):
    def __init__(self, feature: str, message: str) -> None:
        super().__init__(message, 424)
        self.feature = feature


class PlaylistRequest(Model):
    # Keep the historical default and canonical request input stable for replays.
    # Only an explicitly empty name opts a prompt into automatic naming.
    name: str = Field(default="Described playlist", max_length=100)
    prompt: str = Field(default="", max_length=2000)
    count: int = Field(default=30, ge=1, le=100, strict=True)
    max_per_artist: int = Field(default=2, ge=1, le=10, strict=True)
    use_history: bool = Field(default=False, strict=True)
    use_library: bool = Field(default=False, strict=True)
    tracks: list[Suggestion] = Field(default_factory=list, max_length=100)
    save_instructions: bool = Field(default=False, strict=True)
    cadence: Literal["manual", "daily", "weekly"] = "manual"
    preview: bool = Field(default=False, strict=True)
    reference_playlists: list[NativeId] = Field(
        default_factory=list,
        max_length=REFERENCE_PLAYLISTS,
        description="Navidrome playlist IDs the description relates to.",
    )
    reference_songs: list[NativeId] = Field(
        default_factory=list,
        max_length=REFERENCE_SONGS,
        description="Navidrome song IDs the description relates to.",
    )
    exclude_reference: bool = Field(
        default=True,
        strict=True,
        description="Leave the reference songs out of the new playlist.",
    )
    require_all: bool = Field(
        default=False,
        strict=True,
        description=(
            "Explicit tracks: publish only if every track resolves. By default the tracks "
            "that resolve are published and the others are reported."
        ),
    )

    @field_validator("reference_playlists", "reference_songs")
    @classmethod
    def distinct_references(cls, values: list[str]) -> list[str]:
        return distinct(values)

    @model_validator(mode="after")
    def rules(self) -> PlaylistRequest:
        if not self.prompt.strip() and not self.tracks:
            raise ValueError("describe the playlist or supply explicit tracks")
        if self.require_all and not self.tracks:
            raise ValueError("require_all is for explicit tracks")
        if self.tracks and (self.reference_playlists or self.reference_songs):
            raise ValueError("references work with a description, not explicit tracks")
        if self.tracks and (
            len(self.tracks) != self.count
            or self.use_history
            or self.use_library
            or self.cadence != "manual"
            or self.save_instructions
        ):
            raise ValueError("explicit tracks must match count and cannot use context or recur")
        if (
            self.tracks
            and max(Counter(fold(s.artist) for s in self.tracks).values()) > self.max_per_artist
        ):
            raise ValueError("explicit tracks exceed max_per_artist")
        if self.cadence != "manual" and not self.save_instructions:
            raise ValueError("refreshes need explicit saved instructions")
        self.definition()  # Validate the native playlist name too.
        return self

    def definition(self) -> Definition:
        return Definition(
            name="Described playlist" if self.name == "" and not self.tracks else self.name,
            auto_name=self.name == "" and not self.tracks,
            count=self.count,
            max_per_artist=self.max_per_artist,
            unfamiliar_percent=0,
            exclude_days=0,
            repeat_days=0,
            cadence=self.cadence,
            # Explicit tracks keep the prompt as the playlist's brief: what it is for.
            brief=self.prompt.strip(),
            ai=not bool(self.tracks),
            mode="tracks" if self.tracks else "prompt",
            use_history=self.use_history,
            use_library=self.use_library,
            tracks=self.tracks,
            save_instructions=self.save_instructions,
            review_before_publishing=self.preview,
            reference_playlists=self.reference_playlists,
            reference_songs=self.reference_songs,
            exclude_reference=self.exclude_reference,
            require_all=self.require_all,
        )

    def canonical(self) -> str:
        """The input as its replay hash covers it. Fields added later are left out at their
        defaults, so an earlier request replays with the hash it was saved with."""
        data = self.model_dump(mode="json")
        for key, default in ADDED_DEFAULTS.items():
            if data[key] == default:
                del data[key]
        return json.dumps(data, sort_keys=True, ensure_ascii=False)


def found_by_name(data: dict[str, Any]) -> bool:
    """Whether a run used a playlist found by its name in the instructions."""
    playlists = data.get("curation", {}).get("references", {}).get("playlists", [])
    return any(p.get("detected") for p in playlists)


ADDED_DEFAULTS: dict[str, object] = {
    "reference_playlists": [],
    "reference_songs": [],
    "exclude_reference": True,
    "require_all": False,
}


class TrackChange(Suggestion):
    """A track added to an explicit playlist: in the place of the left-out track at
    position ``replaces`` (as the request's tracks number them), else at the end."""

    replaces: int | None = Field(default=None, ge=1, le=100, strict=True)


class TrackAddition(Model):
    tracks: list[TrackChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_places(self) -> TrackAddition:
        places = [t.replaces for t in self.tracks if t.replaces is not None]
        if len(set(places)) != len(places):
            raise ValueError("two tracks replace the same position")
        return self

    def canonical(self) -> str:
        """Fields at their defaults are left out: one added later keeps earlier hashes."""
        data = self.model_dump(mode="json", exclude_defaults=True)
        return json.dumps(data, sort_keys=True, ensure_ascii=False)


# Additions one explicit playlist takes; more changes are a new playlist.
ADDITIONS = 10


class ContextRequest(Model):
    use_history: bool = Field(default=False, strict=True)
    use_library: bool = Field(default=False, strict=True)
    limit: int = Field(default=30, ge=1, le=100, strict=True)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Requests:
    def __init__(self, service: Playlists) -> None:
        self.service = service
        self.store = service.repo.store

    @property
    def library(self) -> str:
        return str(self.service.client.native.library_id)

    async def mint(self, owner: str, name: str, *, history: bool, library: bool) -> str:
        await self.service.connection(owner)
        if not name.strip() or len(name) > 100 or not name.isprintable():
            raise RequestError("Enter a short name for this agent connection.")
        active = await self.store.fetchone(
            "SELECT count(*) AS n FROM playlist_agent_tokens WHERE owner=? AND revoked=0", [owner]
        )
        if active and active["n"] >= 20:
            raise RequestError("Revoke an existing agent connection before creating another.")
        raw = "sjp_" + secrets.token_urlsafe(48)
        scopes = [
            "create",
            "status",
            *(["history"] if history else []),
            *(["library"] if library else []),
        ]
        await self.store.execute(
            "INSERT INTO playlist_agent_tokens(id,owner,library,name,scopes,created)"
            " VALUES (?,?,?,?,?,?)",
            [digest(raw), owner, self.library, name.strip(), json.dumps(scopes), time.time()],
        )
        return raw

    async def tokens(self, owner: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in await self.store.fetchall(
                "SELECT id,name,scopes,created,revoked FROM playlist_agent_tokens"
                " WHERE owner=? ORDER BY (revoked=0) DESC, created DESC LIMIT 100",
                [owner],
            )
        ]

    async def revoke(self, owner: str, ident: str) -> None:
        await self.service.connection(owner)
        await self.store.execute(
            "UPDATE playlist_agent_tokens SET revoked=? WHERE id=? AND owner=?",
            [time.time(), ident, owner],
        )

    async def grant(self, ident: str, scope: str) -> tuple[Connection, dict[str, Any]]:
        row = await self.store.fetchone("SELECT * FROM playlist_agent_tokens WHERE id=?", [ident])
        if not row or row["revoked"] or row["library"] != self.library:
            raise RequestError("The agent connection is invalid or revoked.", 401)
        if scope not in json.loads(row["scopes"]):
            raise RequestError("This agent connection does not grant that operation.", 403)
        connection = await self.service.connection(row["owner"])
        # Native validation can wait while this grant is revoked.
        fresh = await self.store.fetchone("SELECT * FROM playlist_agent_tokens WHERE id=?", [ident])
        if not fresh or dict(fresh) != dict(row):
            raise RequestError("The agent connection is invalid or revoked.", 401)
        return connection, dict(fresh)

    async def guard(self, mix: dict[str, Any], data: dict[str, Any] | None = None) -> None:
        """Current authorization for the mix's request. ``data``: the run about to be saved
        or published; playlists it found by name keep needing the library permission. A run
        someone asked for after the request was made (``data["actor"]``: an agent's grant,
        or "" for the owner on the dashboard) is that one's work: its own authority
        authorizes it, not the grant that made the request."""
        actor = (data or {}).get("actor")
        await self._guard(mix, actor, context=not (data or {}).get("agent"))
        if data is not None and found_by_name(data):
            allowed = (
                await self.grants_library(actor)
                if actor is not None
                else await self.reads_library(mix["id"])
            )
            if not allowed:
                raise RequestError(LIBRARY_LOST, 403)

    async def _guard(
        self, mix: dict[str, Any], actor: str | None = None, *, context: bool = True
    ) -> None:
        """``context``: the run reads the context the request's saved instructions use
        (not so for tracks an agent chose)."""
        if Definition.model_validate_json(mix["definition"]).mode == "import":
            await self.service.imports.guard(mix)
            return
        row = await self.store.fetchone("SELECT * FROM playlist_requests WHERE id=?", [mix["id"]])
        if row and row["library"] != self.library:
            raise RequestError("This request belongs to a different library.", 403)
        if actor is not None:
            await self.acting(mix["owner"], actor)
            if actor and row and context:
                _, grant = await self.grant(actor, "create")
                self.context_scopes(PlaylistRequest.model_validate_json(row["input"]), grant)
            return
        if not row:
            if Definition.model_validate_json(mix["definition"]).mode != "discovery":
                raise RequestError("This creation request is no longer available.", 403)
            return  # Legacy discovery mix.
        spec = PlaylistRequest.model_validate_json(row["input"])
        if row["creator"]:
            connection, grant = await self.grant(row["creator"], "create")
            if connection.user_id != mix["owner"]:
                raise RequestError("This request belongs to another owner.", 403)
            self.context_scopes(spec, {"scopes": row["scopes"]})
            self.context_scopes(spec, grant)
        else:
            await self.service.connection(mix["owner"])

    async def acting(self, owner: str, actor: str) -> Connection:
        """The owner's connection while ``actor`` - an agent grant, or "" for the owner
        on the dashboard - may still change the owner's playlists."""
        if not actor:
            return await self.service.connection(owner)
        connection, _ = await self.grant(actor, "create")
        if connection.user_id != owner:
            raise RequestError("This request belongs to another owner.", 403)
        return connection

    async def grants_library(self, actor: str) -> bool:
        """Whether ``actor`` (an agent grant; "" for the owner) may now read the library."""
        if not actor:
            return True
        grant = await self.store.fetchone("SELECT * FROM playlist_agent_tokens WHERE id=?", [actor])
        return bool(
            grant
            and not grant["revoked"]
            and grant["library"] == self.library
            and "library" in json.loads(grant["scopes"])
        )

    async def reads_library(self, ident: str) -> bool:
        """Whether request ``ident`` may now read the library beyond what it names: the
        owner's own requests always; an agent's while its grant includes library."""
        row = await self.store.fetchone(
            "SELECT creator,scopes FROM playlist_requests WHERE id=?", [ident]
        )
        if row is None:
            return False
        if not row["creator"]:
            return True
        grant = await self.store.fetchone(
            "SELECT * FROM playlist_agent_tokens WHERE id=?", [row["creator"]]
        )
        return bool(
            "library" in json.loads(row["scopes"])
            and grant
            and not grant["revoked"]
            and grant["library"] == self.library
            and "library" in json.loads(grant["scopes"])
        )

    @staticmethod
    def context_scopes(spec: PlaylistRequest | ContextRequest, grant: dict[str, Any]) -> None:
        scopes = json.loads(grant["scopes"])
        # Chosen references are library reads, like the library context.
        reads_library = spec.use_library or (
            isinstance(spec, PlaylistRequest)
            and bool(spec.reference_playlists or spec.reference_songs)
        )
        if (spec.use_history and "history" not in scopes) or (
            reads_library and "library" not in scopes
        ):
            raise RequestError(
                "The requested context needs a separate agent connection grant.", 403
            )

    def usable_ai(self, connection: Connection) -> None:
        try:
            self.service.curator.require_connection(connection)
        except PlaylistError as exc:
            raise SetupRequired("server_ai", str(exc)) from None

    async def create(
        self,
        owner: str,
        key: str,
        spec: PlaylistRequest,
        *,
        creator: str = "",
        scopes: list[str] | None = None,
    ) -> tuple[str, bool]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise RequestError("Send an Idempotency-Key of 8 to 128 plain characters.")
        canonical = spec.canonical()
        # The caller holds changes: token revocation, connection edits and enqueue are ordered.
        connection = await self.service.connection(owner)
        if creator:
            _, grant = await self.grant(creator, "create")
            if grant["owner"] != owner:
                raise RequestError("This request belongs to another owner.", 403)
            if scopes is None or "create" not in scopes:
                raise RequestError("The submitting token must grant playlist creation.", 403)
            self.context_scopes(spec, {"scopes": json.dumps(scopes)})
            self.context_scopes(spec, grant)
        old = await self.store.fetchone(
            "SELECT * FROM playlist_requests WHERE owner=? AND request_key=?", [owner, key]
        )
        if old:
            if old["creator"] != creator or old["library"] != self.library:
                raise RequestError("This request key is unavailable for this agent.", 403)
            if old["input_hash"] != digest(canonical):
                raise RequestError("This request key was already used with different input.", 409)
            return old["id"], False
        if not spec.tracks:
            self.usable_ai(connection)
        if spec.use_history:
            try:
                connection.require_history()
            except PlaylistError as exc:
                raise SetupRequired("listening_history", str(exc)) from None
        await self.room(owner)
        ident, run = secrets.token_hex(12), secrets.token_hex(12)
        async with self.store.transaction() as db:
            await db.execute(
                "INSERT INTO playlist_mixes(id,owner,definition) VALUES (?,?,?)",
                [ident, owner, spec.definition().model_dump_json()],
            )
            await db.execute(
                "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,'queued',?)",
                [
                    run,
                    ident,
                    time.time(),
                    json.dumps(
                        {
                            "publish_after_generation": not spec.preview,
                            "enable_after": bool(spec.definition().interval),
                        }
                    ),
                ],
            )
            await db.execute(
                "INSERT INTO playlist_requests"
                " (id,owner,creator,library,request_key,input_hash,input,first_run,scopes)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ident,
                    owner,
                    creator,
                    self.library,
                    key,
                    digest(canonical),
                    canonical,
                    run,
                    json.dumps(scopes or []),
                ],
            )
        self.service.notify()
        return ident, True

    async def room(self, owner: str) -> None:
        """At most ten of the owner's runs at work at once."""
        active = await self.store.fetchone(
            "SELECT count(*) AS n FROM playlist_runs r JOIN playlist_mixes m ON m.id=r.mix"
            " WHERE m.owner=? AND r.state IN ('queued','generating','publishing')",
            [owner],
        )
        if active and active["n"] >= 10:
            raise RequestError(
                "Too many playlist requests are still running. Try again later.", 429
            )

    async def owned(self, owner: str, ident: str, creator: str | None) -> dict[str, Any]:
        row = await self.store.fetchone(
            "SELECT * FROM playlist_requests WHERE id=? AND owner=?", [ident, owner]
        )
        if (
            not row
            or row["library"] != self.library
            or (creator is not None and row["creator"] != creator)
        ):
            raise RequestError("This request is unavailable for this agent.", 404)
        return dict(row)

    async def status(self, owner: str, ident: str, *, creator: str | None = None) -> dict[str, Any]:
        await self.service.connection(owner)
        await self.owned(owner, ident, creator)
        return await self.describe(owner, ident)

    async def describe(self, owner: str, ident: str) -> dict[str, Any]:
        """The latest run of the owner's playlist ``ident`` (a request's, or a discovery
        mix's), as its status shows it; the caller has checked who may see it."""
        mix = await self.service.repo.mix(ident, owner)
        runs = await self.service.repo.runs(ident)
        # (A mix saved without its first run has none yet.)
        run: dict[str, Any] = runs[0] if runs else {"id": "", "state": "", "error": "", "data": {}}
        spec = Definition.model_validate_json(mix["definition"])
        curation = run["data"].get("curation", {})
        songs = curation.get("songs", {})
        selected = run["data"].get("selected", [])
        # Published songs carry their Navidrome IDs, to edit the playlist by.
        native = run["data"].get("native_ids", []) if run["state"] == "published" else []
        ids = native if len(native) == len(selected) else [""] * len(selected)
        return {
            "id": ident,
            "run": run["id"],
            "state": run["state"],
            "error": run["error"],
            "playlist_id": mix["playlist_id"],
            "name": spec.name,
            "count": spec.count,
            "refreshes": bool(mix["enabled"]),
            "selected": [
                {
                    **{k: s.get(k, "") for k in ("artist", "title", "album", "reason")},
                    **{
                        k: songs.get(s.get("id"), {}).get(k, default)
                        for k, default in (("fit", ""), ("source", ""), ("facts", {}))
                    },
                    **({"song_id": native_id} if native_id else {}),
                }
                for s, native_id in zip(selected, ids, strict=True)
            ],
            # A refresh: what it learned from the owner's edits of the playlist.
            "edits": run["data"].get("edits", {}),
            # Explicit tracks: each one's outcome, or why it was left out.
            **(await self.report(ident, curation.get("tracks", []))),
            **{
                key: curation.get(key, default)
                for key, default in (
                    ("goal", ""),
                    ("references", {}),
                    ("reference_profile", {}),
                    ("revision", ""),
                    ("coverage", {}),
                    ("sound_pool", {}),
                    ("left_out", 0),
                )
            },
        }

    async def add_tracks(
        self,
        owner: str,
        ident: str,
        key: str,
        addition: TrackAddition,
        *,
        creator: str | None = None,
        actor: str | None = None,
    ) -> bool:
        """Add tracks to explicit playlist ``ident`` - in the places of left-out tracks, or
        at its end - as a new run of the same request; its other tracks are resolved
        again. False: this key already added the same tracks. ``actor``: the agent grant
        whose work the run is (else the request's own). The caller holds changes."""
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise RequestError("Send an Idempotency-Key of 8 to 128 plain characters.")
        await self.owned(owner, ident, creator)
        mix = await self.service.repo.mix(ident, owner)
        definition = Definition.model_validate_json(mix["definition"])
        if definition.mode != "tracks":
            raise RequestError("Only a playlist of explicit tracks takes added tracks.")
        acting = {} if actor is None else {"actor": actor}
        await self.guard(mix, acting)
        runs = await self.service.repo.runs(ident)
        done = [r for r in runs if r["data"].get("addition")]
        for run in done:
            if run["data"]["addition"]["key"] == key:
                if run["data"]["addition"]["hash"] != digest(addition.canonical()):
                    raise RequestError("This request key was already used with other tracks.", 409)
                return False
        latest = runs[0]
        # A run that published, or one that left the playlist as it was: a failed
        # selection, or a preview discarded.
        if not (
            latest["state"] in ("published", "discarded")
            or (latest["state"] == "failed" and not latest["data"].get("selected"))
        ):
            raise RequestError(
                "Add tracks once the request is published, or after its selection failed.",
                409,
            )
        if len(done) >= ADDITIONS:
            raise RequestError(
                "This playlist has taken all the additions it can. Create a new playlist.", 409
            )
        # The latest report: a later run may have failed or been discarded before one. An
        # agent's replacement or edit that was not published reports another list.
        reported = next(
            (
                r
                for r in runs
                if r["data"].get("curation", {}).get("tracks")
                and (r["state"] == "published" or not r["data"].get("agent"))
            ),
            None,
        )
        report = {
            t["position"]: t for t in (reported["data"]["curation"]["tracks"] if reported else [])
        }
        tracks = list(definition.tracks)
        for change in addition.tracks:
            track = Suggestion.model_validate(change.model_dump(exclude={"replaces"}))
            if change.replaces is None:
                tracks.append(track)
            elif report.get(change.replaces, {}).get("outcome") == "unresolved":
                tracks[change.replaces - 1] = track
            else:
                raise RequestError(
                    f"Position {change.replaces} is not a left-out track of this playlist."
                )
        if len(tracks) > 100:
            raise RequestError("A playlist of explicit tracks has at most 100 tracks.")
        if max(Counter(fold(t.artist) for t in tracks).values()) > definition.max_per_artist:
            raise RequestError("The tracks would exceed max_per_artist.")
        connection = await self.service.connection(owner)
        await self.room(owner)
        if mix["playlist_id"] and mix["published"]:
            current = await self.service.client.playlist(connection, mix["playlist_id"])
            if fingerprint(current) != mix["published"]:
                raise RequestError(EDITED, 409)
        changed = definition.model_copy(update={"tracks": tracks, "count": len(tracks)})
        Definition.model_validate(changed.model_dump())
        queued = secrets.token_hex(12)
        data = {
            "publish_after_generation": not definition.review_before_publishing,
            "enable_after": False,
            "addition": {"key": key, "hash": digest(addition.canonical())},
            **acting,
        }
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE playlist_mixes SET definition=? WHERE id=?",
                [changed.model_dump_json(), ident],
            )
            await db.execute(
                "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,'queued',?)",
                [queued, ident, max(time.time(), latest["created"] + 1e-6), json.dumps(data)],
            )
        self.service.notify()
        return True

    async def report(self, ident: str, tracks: list[dict[str, Any]]) -> dict[str, Any]:
        """An explicit-track report as the request may now see it: without the library
        permission (lost since, too), no song is told apart as the library's."""
        if not tracks:
            return {}
        if not await self.reads_library(ident):
            tracks = private(tracks)
        elif any(t.get("outcome") == "resolved" for t in tracks):
            tracks = [t | {"outcome": "resolved"} if t.get("song") else t for t in tracks]
        return {"resolution": summary(tracks), "tracks": tracks}

    async def forget_edits(self, owner: str, ident: str, *, creator: str | None = None) -> int:
        """Forget what this request's refreshes learned from the owner's edits of its
        playlist; returns how many songs that was. Only the owner's own request."""
        await self.owned(owner, ident, creator)
        return await forget(self.store, ident)

    async def retry(self, owner: str, ident: str, *, creator: str | None = None) -> None:
        """``creator``: the agent grant retrying its own request (the HTTP API); None for
        the owner on the dashboard, who takes over a run an agent asked for (also when
        that agent was disconnected since)."""
        await self.owned(owner, ident, creator)
        await self.rerun(owner, ident, actor=creator or "", taking_over=False)

    async def rerun(self, owner: str, ident: str, *, actor: str, taking_over: bool = True) -> None:
        """Continue the latest failed or uncertain run of the owner's playlist ``ident``.
        It reconciles a stored publication intent; it never chooses new music for a
        frozen selection. The run becomes ``actor``'s work (an agent grant; "" for the
        owner); without ``taking_over``, only a run someone asked for after the request
        was made changes hands - others keep the request's own authority."""
        mix = await self.service.repo.mix(ident, owner)
        run = (await self.service.repo.runs(ident))[0]
        if taking_over or "actor" in run["data"]:
            run["data"]["actor"] = actor
        await self.guard(mix, run["data"])
        if run["state"] not in ("failed", "uncertain"):
            raise RequestError("Only a failed or uncertain run can be retried.", 409)
        # No new selection after the first one was frozen; uncertain creates reconcile.
        state = "publishing" if run["data"].get("selected") else "queued"
        # Explicit retries are manual continuations of this same run. An automatic
        # failure paused its schedule; retrying must neither be discarded as paused
        # background work nor carry pending permission to enable refreshes.
        if run["automatic"]:
            run["data"]["enable_after"] = False
        if state == "queued":
            run["data"].pop("curation", None)  # a new selection reports anew
        await self.store.execute(
            "UPDATE playlist_runs SET state=?,automatic=0,data=?,error='' WHERE id=?",
            [state, json.dumps(run["data"]), run["id"]],
        )
        self.service.notify()
