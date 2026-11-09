"""Changes an agent makes to the owner's Shijhon playlists: whatever made them (the
dashboard, this agent or another one), with the agent's own grant. Each change is a run
of the playlist, published like its other runs; Navidrome's playlist keeps its ID."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections import Counter
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, model_validator

from shijhon.matching.normalize import fold
from shijhon.playlists.model import (
    Definition,
    Model,
    NativeId,
    PlaylistError,
    Suggestion,
    fingerprint,
)
from shijhon.playlists.references import entries
from shijhon.playlists.requests import RequestError, SetupRequired, digest

if TYPE_CHECKING:
    from shijhon.playlists.service import Playlists

# Runs that have not finished: a change waits for them (or a preview's review).
ACTIVE = ("queued", "generating", "preview", "publishing", "uncertain")
KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}")


class Replacement(Model):
    """A new ordered list for a playlist, in place of all its songs."""

    tracks: list[Suggestion] = Field(min_length=1, max_length=100)
    max_per_artist: int | None = Field(
        default=None,
        ge=1,
        le=10,
        strict=True,
        description="Tracks per artist at most; by default the playlist's own limit.",
    )
    require_all: bool = Field(
        default=False,
        strict=True,
        description="Publish only if every track resolves; by default what resolves.",
    )
    preview: bool = Field(
        default=False, strict=True, description="Wait for review instead of publishing."
    )
    brief: str | None = Field(
        default=None,
        max_length=2000,
        description="Agent-curated playlists: what the playlist is for, saved with it.",
    )

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_defaults=True), sort_keys=True)


class TrackEdit(Model):
    """Songs to take out of a published playlist and/or its new order, by Navidrome ID."""

    remove: list[NativeId] = Field(default_factory=list, max_length=100)
    order: list[NativeId] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def something(self) -> TrackEdit:
        if not self.remove and self.order is None:
            raise ValueError("send songs to remove, a new order, or both")
        return self

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_defaults=True), sort_keys=True)


class DiscoveryMix(Model):
    """A playlist of songs similar to the owner's Last.fm listening history."""

    name: str = Field(default="Discovery mix", min_length=1, max_length=100)
    count: int = Field(default=30, ge=1, le=100, strict=True)
    unfamiliar_percent: int = Field(
        default=70, ge=0, le=100, strict=True, description="Share of songs not heard before."
    )
    max_per_artist: int = Field(default=2, ge=1, le=10, strict=True)
    exclude_days: int = Field(
        default=14, ge=0, le=365, strict=True, description="Leave out songs heard this recently."
    )
    repeat_days: int = Field(
        default=28,
        ge=0,
        le=365,
        strict=True,
        description="Leave out songs this mix published this recently.",
    )
    cadence: Literal["manual", "daily", "weekly"] = "manual"
    instructions: str = Field(
        default="",
        max_length=2000,
        description="A brief for Shijhon's server AI, which then chooses; needs server AI.",
    )
    preview: bool = Field(default=False, strict=True)
    enable_refreshes: bool = Field(
        default=False, strict=True, description="Refresh daily or weekly; needs that cadence."
    )

    def definition(self) -> Definition:
        if self.enable_refreshes and self.cadence == "manual":
            raise RequestError("Choose a daily or weekly cadence to enable refreshes.")
        return Definition(
            name=self.name,
            count=self.count,
            unfamiliar_percent=self.unfamiliar_percent,
            max_per_artist=self.max_per_artist,
            exclude_days=self.exclude_days,
            repeat_days=self.repeat_days,
            cadence=self.cadence,
            brief=self.instructions.strip(),
            ai=bool(self.instructions.strip()),
            review_before_publishing=self.preview,
        )


def most_per_artist(tracks: list[Suggestion]) -> int:
    return max(Counter(fold(t.artist) for t in tracks).values())


class Changes:
    def __init__(self, service: Playlists) -> None:
        self.service = service
        self.store = service.repo.store

    async def mix(self, owner: str, ident: str) -> tuple[dict[str, Any], Definition]:
        """The owner's playlist ``ident`` in this library, whatever made it."""
        if not ident or len(ident) > 100:
            raise RequestError("Use a playlist request ID.")
        try:
            mix = await self.service.repo.mix(ident, owner)
        except PlaylistError:
            raise RequestError("This playlist is unavailable for this account.", 404) from None
        library = self.service.requests.library
        for table in ("playlist_requests", "playlist_imports"):
            row = await self.store.fetchone(
                f"SELECT library FROM {table} WHERE id=?",  # noqa: S608 (fixed names)
                [ident],
            )
            if row and row["library"] != library:
                raise RequestError("This playlist is unavailable for this account.", 404)
        return mix, Definition.model_validate_json(mix["definition"])

    async def changeable(
        self, owner: str, ident: str, key: str, payload: str
    ) -> tuple[dict[str, Any], Definition, bool]:
        """The playlist an agent's change goes to, and whether ``key`` already made it."""
        if not KEY.fullmatch(key):
            raise RequestError("Send an Idempotency-Key of 8 to 128 plain characters.")
        mix, definition = await self.mix(owner, ident)
        if definition.mode == "import":
            raise RequestError(
                "An imported playlist changes through its import review: recheck it, or "
                "import the list again."
            )
        done = await self.store.fetchone(
            "SELECT data FROM playlist_runs WHERE mix=? AND json_extract(data,'$.agent.key')=?",
            [ident, key],
        )
        if done:
            if json.loads(done["data"])["agent"]["hash"] != digest(payload):
                raise RequestError("This request key was already used with other input.", 409)
            return mix, definition, True
        runs = await self.service.repo.runs(ident)
        if any(r["state"] in ACTIVE for r in runs):
            raise RequestError(
                "The playlist has a run that has not finished. Wait for it, or publish or "
                "discard its preview, then send this again.",
                409,
            )
        return mix, definition, False

    async def queue(
        self, mix: dict[str, Any], definition: Definition, rules: Definition, data: dict[str, Any]
    ) -> None:
        """A new run of the playlist with the agent's change. The run keeps the tracks it
        publishes (``rules``); an agent-curated playlist takes them as its own definition
        only when the run is published."""
        agent = data["agent"]
        agent["rules"] = rules.model_dump(mode="json")
        if definition.mode == "tracks":
            agent["definition"] = rules.model_dump(mode="json")
        await self.service.requests.room(mix["owner"])
        latest = await self.store.fetchone(
            "SELECT max(created) AS at FROM playlist_runs WHERE mix=?", [mix["id"]]
        )
        created = max(time.time(), ((latest and latest["at"]) or 0) + 1e-6)
        await self.store.execute(
            "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,'queued',?)",
            [secrets.token_hex(12), mix["id"], created, json.dumps(data)],
        )
        self.service.notify()

    @staticmethod
    def rules(definition: Definition, tracks: list[Suggestion], **update: Any) -> Definition:
        """The tracks a run publishes, as the definition the run is checked by."""
        return Definition.model_validate(
            {
                **(
                    definition.model_dump()
                    if definition.mode == "tracks"
                    else {
                        "name": definition.name,
                        "mode": "tracks",
                        "cadence": "manual",
                        "unfamiliar_percent": 0,
                        "exclude_days": 0,
                        "repeat_days": 0,
                        "max_per_artist": definition.max_per_artist,
                    }
                ),
                "tracks": [t.model_dump() for t in tracks],
                "count": len(tracks),
                **update,
            }
        )

    async def replace(
        self, owner: str, ident: str, key: str, replacement: Replacement, *, actor: str
    ) -> bool:
        """Replace every song of the owner's playlist ``ident`` with ``replacement``'s
        tracks, resolved as explicit tracks are. Songs the owner removed from it stay out.
        False: this key already asked for the same. The caller holds changes."""
        mix, definition, done = await self.changeable(owner, ident, key, replacement.canonical())
        if done:
            return False
        await self.service.requests.acting(owner, actor)
        if replacement.brief is not None and definition.mode != "tracks":
            raise RequestError(
                "Only an agent-curated playlist keeps a brief. A described playlist's "
                "instructions stay as they were saved."
            )
        limit = replacement.max_per_artist or definition.max_per_artist
        if most_per_artist(replacement.tracks) > limit:
            raise RequestError("The tracks would exceed max_per_artist.")
        update: dict[str, Any] = {"max_per_artist": limit, "require_all": replacement.require_all}
        if replacement.brief is not None:
            update["brief"] = replacement.brief.strip()
        data = {
            "publish_after_generation": not replacement.preview,
            "enable_after": False,
            "actor": actor,
            # ``by``: who chose these songs; ``actor`` can change hands on a retry.
            "agent": {
                "kind": "replace",
                "key": key,
                "hash": digest(replacement.canonical()),
                "by": actor,
            },
        }
        await self.queue(
            mix, definition, self.rules(definition, replacement.tracks, **update), data
        )
        return True

    async def edit(
        self, owner: str, ident: str, key: str, change: TrackEdit, *, actor: str
    ) -> bool:
        """Remove songs from the owner's published playlist ``ident`` and/or reorder it, as
        Navidrome has it now. Each song ID listed in ``remove`` takes out one copy of that
        song (its first one). Songs no longer in the playlist afterwards are remembered as
        the owner's removals, so refreshes and replacements leave them out. Songs the
        playlist has more than once (the user's choice) may stay so. The caller holds
        changes."""
        mix, definition, done = await self.changeable(owner, ident, key, change.canonical())
        if done:
            return False
        connection = await self.service.requests.acting(owner, actor)
        if not mix["playlist_id"] or not mix["published"]:
            raise RequestError("Edit a playlist once Shijhon has published it.", 409)
        playlist = await self.service.client.playlist(connection, mix["playlist_id"])
        listed = [e for e in playlist.get("entry", []) if isinstance(e, dict) and e.get("id")]
        current = [str(e["id"]) for e in listed]
        kept = list(current)
        for ident_ in change.remove:
            if ident_ not in kept:
                raise RequestError(
                    f"Song {ident_} is not in the playlist now (as often as remove lists it). "
                    "Read the playlist again."
                )
            kept.remove(ident_)
        order = kept if change.order is None else change.order
        if Counter(order) != Counter(kept):
            raise RequestError(
                f"List the playlist's {len(kept)} remaining songs in order, each as often as "
                "it stays in the playlist. Read the playlist again if it changed."
            )
        if not order:
            raise RequestError("Keep at least one song. To start again, replace the songs.")
        if order == current:
            raise RequestError("This leaves the playlist as it is.")
        songs = {t.id: t for t in entries(listed)}
        try:
            tracks = [
                Suggestion(artist=songs[i].artist, title=songs[i].title, album=songs[i].album)
                for i in order
            ]
        except (KeyError, ValueError):
            raise RequestError(
                "A song without readable artist and title tags cannot be kept by an edit."
            ) from None
        # Removing and reordering add no song: a repeat the user made stays as it was.
        limit = max(definition.max_per_artist, most_per_artist(tracks))
        if limit > 10:
            raise RequestError("The playlist has more than 10 songs of one artist.")
        rules = self.rules(definition, tracks, max_per_artist=limit, require_all=False)
        data = {
            "publish_after_generation": True,
            "enable_after": False,
            "actor": actor,
            "agent": {
                "kind": "edit",
                "key": key,
                "hash": digest(change.canonical()),
                "order": order,
                # Only songs no longer in the playlist count as removed.
                "removed": sorted(set(change.remove) - set(order)),
                "baseline": fingerprint(playlist),
            },
        }
        await self.queue(mix, definition, rules, data)
        return True

    async def review(
        self,
        owner: str,
        ident: str,
        run_id: str,
        action: Literal["publish", "discard"],
        *,
        enable_refreshes: bool | None = None,
        actor: str,
    ) -> None:
        """Publish or discard the preview of the owner's playlist ``ident``, as the
        dashboard does. A discovery mix's refreshes start with this publication when
        ``enable_refreshes``; None keeps what the mix's creation asked for. The
        publication is ``actor``'s work (``Requests.guard``). The caller holds changes."""
        mix, definition = await self.mix(owner, ident)
        if definition.mode == "import":
            raise RequestError("Review an import with review_playlist_import.")
        run = (await self.service.repo.runs(ident))[0]
        if run["id"] != run_id:
            raise RequestError("This preview is no longer current. Read the playlist again.", 409)
        if run["state"] != "preview":
            raise RequestError("This playlist has no preview waiting for review.", 409)
        if action == "discard":
            await self.service.repo.update(run["id"], "discarded", run["data"])
            return
        run["data"]["actor"] = actor
        await self.service.requests.guard(mix, run["data"])
        if (
            definition.mode == "discovery"
            and not run["data"].get("agent")
            and enable_refreshes is not None
        ):
            run["data"]["enable_after"] = enable_refreshes and bool(definition.interval)
            if not run["data"]["enable_after"]:
                await self.store.execute("UPDATE playlist_mixes SET enabled=0 WHERE id=?", [ident])
        await self.service.repo.update(run["id"], "publishing", run["data"])
        self.service.notify()

    async def discovery(self, owner: str) -> None:
        """Discovery mixes read the owner's Last.fm listening history."""
        connection = await self.service.connection(owner)
        try:
            connection.require_history()
        except PlaylistError as exc:
            raise SetupRequired("listening_history", str(exc)) from None

    async def discover(self, owner: str, key: str, mix: DiscoveryMix) -> tuple[str, bool]:
        """Create a discovery mix and its first run. The caller holds changes."""
        if not KEY.fullmatch(key):
            raise RequestError("Send an Idempotency-Key of 8 to 128 plain characters.")
        definition = mix.definition()
        old = await self.store.fetchone(
            "SELECT id FROM playlist_discovery_requests WHERE owner=? AND request_key=?",
            [owner, key],
        )
        await self.discovery(owner)
        if definition.ai:
            self.service.requests.usable_ai(await self.service.connection(owner))
        if not old:
            await self.service.requests.room(owner)
        try:
            ident = await self.service.repo.create_discovery(
                owner, key, definition, enable_after=mix.enable_refreshes
            )
        except PlaylistError as exc:
            raise RequestError(str(exc), 409) from None
        self.service.notify()
        return ident, not old

    async def rediscover(self, owner: str, ident: str, mix: DiscoveryMix) -> None:
        """New rules for a discovery mix: as on the dashboard, pending runs are discarded
        and a new one starts. The caller holds changes."""
        _, definition = await self.mix(owner, ident)
        if definition.mode != "discovery":
            raise RequestError("Only a discovery mix takes discovery rules.")
        spec = mix.definition()
        await self.discovery(owner)
        if spec.ai:
            self.service.requests.usable_ai(await self.service.connection(owner))
        runs = await self.service.repo.runs(ident)
        if any(r["state"] in ("publishing", "uncertain") for r in runs):
            raise RequestError(
                "Finish checking the current publication before changing the mix.", 409
            )
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE playlist_runs SET state='discarded' WHERE mix=?"
                " AND state IN ('queued','generating','preview')",
                [ident],
            )
            await db.execute(
                "UPDATE playlist_mixes SET definition=?,enabled=0 WHERE id=?",
                [spec.model_dump_json(), ident],
            )
        await self.service.repo.enqueue(ident, enable_after=mix.enable_refreshes)
        self.service.notify()
