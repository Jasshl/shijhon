"""One durable playlist worker. Preparation and publication have separate checkpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

import aiosqlite
import anyio
from pydantic import SecretStr

from shijhon.access import Principal
from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CatalogRef, CatalogRelease, release_data
from shijhon.config import PlaylistSettings
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.engine import MaterializeError
from shijhon.playlists.ai import Curator, validate
from shijhon.playlists.client import OwnerClient
from shijhon.playlists.edits import Edits
from shijhon.playlists.edits import learn as learn_edits
from shijhon.playlists.edits import remembered as remembered_edits
from shijhon.playlists.facts import Tags
from shijhon.playlists.history import History
from shijhon.playlists.imports import Imports
from shijhon.playlists.model import (
    AIModels,
    AIPreferences,
    Connection,
    Definition,
    PlaylistError,
    Track,
    fingerprint,
    music_key,
)
from shijhon.playlists.prompt import generate as generate_prompt
from shijhon.playlists.repository import AI_FIELDS, Repository
from shijhon.playlists.requests import Requests, SetupRequired
from shijhon.playlists.resolve import Resolver, listed, native_track, same_recording
from shijhon.store import Store
from shijhon.views.commits import CommitError, Commits
from shijhon.views.ids import CatalogId, song_id

log = logging.getLogger(__name__)
if TYPE_CHECKING:
    from shijhon.agents.oauth import OAuth
    from shijhon.catalog.addon import AddonCatalogs
    from shijhon.playlists.sound import Sound

_PREPARATION_ERRORS = {
    "owned_decision": (
        "A selected album is kept as it is or has a settled Library decision. "
        "Review that choice in Library, or generate a different selection."
    ),
    "owned_ambiguous": (
        "A selected release cannot be linked to one owned album with confidence. "
        "Generate again or change the selected release."
    ),
    "owned_incompatible": (
        "A selected release cannot safely complete the owned album. "
        "Choose a different release or generate again."
    ),
    "owned_plan": (
        "A selected release conflicts with the owned album's saved match. "
        "Review its match in Library or generate again."
    ),
    "owned_complete": (
        "A selected owned album is complete and will not be expanded. "
        "Choose a different recording or generate again."
    ),
    "owned_retry": (
        "A previous album fill failed and is waiting before retry. "
        "Retry this selection later, or review the album in Library."
    ),
    "selection_changed": "A selected release changed after it was saved. Generate again.",
}


def _with_track(message: str, track: Track) -> str:
    """Only frozen user-visible metadata may identify the affected selection."""

    def visible(value: str) -> str:
        return "".join(ch for ch in value[:160] if ch.isprintable())

    return (
        f'{message} Selected track: "{visible(track.title)}" by '
        f'"{visible(track.artist)}", album "{visible(track.album)}".'
    )


def release_fingerprint(release: CatalogRelease) -> str:
    data = release_data(release)
    data.pop("artwork_template", None)
    for song in data["tracks"]:
        song.pop("artwork_template", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class Playlists:
    def __init__(
        self,
        store: Store,
        native: NavidromeService,
        catalog: Catalog | None,
        commits: Commits | None,
        settings: PlaylistSettings,
        *,
        addons: AddonCatalogs | None = None,
    ) -> None:
        self.repo = Repository(store)
        self.client = OwnerClient(native)
        self.history = History(store, settings)
        self.tags = Tags(store, self.history)
        self.curator = Curator(settings, authorize=self.ai_connection)
        # The catalogs of the add-ons: imports ask them for what the catalog lacks.
        self.addons = addons
        self.resolver = Resolver(self.client, catalog, addons)
        self.catalog, self.commits, self.settings = catalog, commits, settings
        self.requests = Requests(self)
        self.imports = Imports(self)
        self.oauth: OAuth | None = None
        self.library_context: Callable[[str, int], Awaitable[list[dict[str, str]]]] | None = None
        self.sound: Sound | None = None  # sound analysis (AudioMuse-AI), when configured
        self.published: (
            Callable[[Principal, list[Track], list[str], aiosqlite.Connection], Awaitable[None]]
            | None
        ) = None
        # An import's publication: (account, import, its songs' native IDs, transaction).
        self.imported: (
            Callable[[Principal, str, list[str], aiosqlite.Connection], Awaitable[None]] | None
        ) = None
        self.changes = anyio.Lock()
        self.worker = anyio.Lock()
        self.wake = anyio.Event()

    async def aclose(self) -> None:
        await self.history.aclose()
        await self.client.aclose()

    async def connection(self, owner_id: str) -> Connection:
        connection = await self.repo.connection(owner_id)
        if connection is None:
            raise PlaylistError("Connect your playlist account first.")
        await self.client.validate(connection)
        if await self.repo.connection(owner_id) != connection:
            raise PlaylistError("The saved playlist authorization changed.")
        return connection

    async def save_connection(
        self,
        connection: Connection,
        *,
        configure_host: bool = False,
        allowed_models: list[str] | None = None,
    ) -> None:
        """Save personal data; host AI changes always require a current administrator.

        A demoted owner keeps prior data but cannot execute or edit a private host
        connection. Personal model choices never update host configuration.
        Callers serialize this with ``changes``, as with other connection updates.
        """
        await self.client.validate(connection)
        principal = await self.client.principal(connection)
        old = await self.repo.connection(connection.user_id)
        if not principal.is_admin:
            disabled_ai = (
                connection.backend == "none"
                and not connection.endpoint
                and not connection.model
                and not connection.api_key.get_secret_value()
            )
            changed_host = (
                not connection.use_shared_ai
                and not disabled_ai
                and (
                    old is None or any(getattr(old, k) != getattr(connection, k) for k in AI_FIELDS)
                )
            )
            if configure_host or changed_host or allowed_models is not None:
                raise PlaylistError("Only administrators can configure the host AI service.")
        if allowed_models is not None:
            allowed_models = AIModels(allowed_models=allowed_models).allowed_models
            for model in allowed_models:
                Connection.model_validate({**connection.model_dump(), "model": model})
        if configure_host and connection.use_shared_ai:
            raise PlaylistError("Configure a host connection before sharing it.")
        await self.repo.connect(connection)
        if configure_host:
            await self.repo.save_shared_ai(connection, allowed_models=allowed_models)

    async def ai_choices(self, owner: str) -> dict[str, Any]:
        """Only approved model names are public; never return the host config."""
        config = await self.repo.shared_ai()
        preferences = await self.repo.ai_preferences(owner)
        models = AIModels(allowed_models=config.get("allowed_models", [])).allowed_models
        default = str(config.get("model", ""))
        return {
            "available": bool(config and config.get("backend") != "none"),
            "default_model": default,
            "allowed_models": list(dict.fromkeys([*([default] if default else []), *models])),
            "model": preferences.model,
        }

    async def configure_shared_ai(
        self, connection: Connection, *, allowed_models: list[str]
    ) -> None:
        await self.client.validate(connection)
        if not (await self.client.principal(connection)).is_admin:
            raise PlaylistError("Only administrators can configure the host AI service.")
        models = AIModels(allowed_models=allowed_models).allowed_models
        for model in models:
            Connection.model_validate({**connection.model_dump(), "model": model})
        await self.repo.save_shared_ai(connection, allowed_models=models)

    async def choose_ai_model(
        self, owner: str, model: str, *, use_shared_ai: bool | None = None
    ) -> None:
        connection = await self.connection(owner)
        preferences = AIPreferences(model=model)
        change_service = use_shared_ai is not None and (
            connection.use_shared_ai != use_shared_ai
            or (not use_shared_ai and connection.backend != "none")
        )
        if change_service:
            connection = connection.model_copy(
                update={
                    "use_shared_ai": use_shared_ai,
                    "backend": "none",
                    "endpoint": "",
                    "model": "",
                    "api_key": SecretStr(""),
                }
            )
        if use_shared_ai is False:
            if change_service:
                await self.save_connection(connection)
            await self.repo.save_ai_preferences(owner, AIPreferences())
            return
        choices = await self.ai_choices(owner)
        if not connection.use_shared_ai or not choices["available"]:
            raise SetupRequired(
                "server_ai", "Select the shared AI service before choosing a personal model."
            )
        if model and model not in choices["allowed_models"]:
            raise PlaylistError("Choose a model approved by the administrator or use the default.")
        if change_service:
            await self.save_connection(connection)
        else:
            self.curator.require_connection(connection)
        await self.repo.save_ai_preferences(owner, preferences)

    async def ai_connection(self, connection: Connection) -> Connection:
        """Recheck credentials, role, service and model before every inference request."""
        async with self.changes:
            return await self._ai_connection(connection)

    async def _ai_connection(self, connection: Connection) -> Connection:
        current = await self.connection(connection.user_id)
        if current != connection:
            raise PlaylistError("The saved AI connection changed. Generate the playlist again.")
        principal = await self.client.principal(current)
        if current.use_shared_ai:
            choices = await self.ai_choices(current.user_id)
            if not choices["available"]:
                raise PlaylistError("The administrator has disabled the shared AI service.")
            if choices["model"] and choices["model"] not in choices["allowed_models"]:
                raise PlaylistError(
                    "Your selected AI model is no longer approved. Choose another model "
                    "or use the administrator's default."
                )
            current = Connection.model_validate(
                {**current.model_dump(), "model": choices["model"] or current.model}
            )
        elif current.backend != "none" and not principal.is_admin:
            raise PlaylistError(
                "Only administrators can use a private host AI connection. "
                "Select the shared AI service in Playlists."
            )
        self.curator.require_connection(current)
        return current

    def notify(self) -> None:
        self.wake.set()

    async def run(self) -> None:
        # Selection was not frozen: restart cannot safely claim its AI result survived.
        interrupted = await self.repo.store.fetchall(
            "SELECT mix,automatic,data FROM playlist_runs WHERE state='generating'"
        )
        for row in interrupted:
            if row["automatic"] or json.loads(row["data"]).get("publish_after_generation"):
                await self.repo.store.execute(
                    "UPDATE playlist_mixes SET enabled=0 WHERE id=?", [row["mix"]]
                )
        await self.repo.store.execute(
            "UPDATE playlist_runs SET state='failed',"
            "error='Selection was interrupted. Generate the playlist again.'"
            " WHERE state='generating'"
        )
        while True:
            self.wake = anyio.Event()
            try:
                await self.tick()
            except Exception as exc:
                # Database/downstream failures never terminate the scheduler permanently.
                log.warning("playlist scheduler: %s", type(exc).__name__)
            with anyio.move_on_after(30):
                await self.wake.wait()

    async def tick(self) -> None:
        # The application writer lock excludes another process; a single worker excludes
        # competing playlist writes. A missed interval causes one refresh, never a backlog.
        async with self.changes:
            due = await self.repo.store.fetchall(
                "SELECT * FROM playlist_mixes WHERE enabled=1 AND due<=?", [time.time()]
            )
            for row in due:
                definition = Definition.model_validate_json(row["definition"])
                if not definition.interval:
                    continue
                try:
                    await self.repo.enqueue(row["id"], automatic=True)
                except PlaylistError:
                    continue
                await self.repo.store.execute(
                    "UPDATE playlist_mixes SET due=? WHERE id=?",
                    [time.time() + definition.interval, row["id"]],
                )
        # Each waiting run gets one turn per round, oldest first: an import that requeues
        # itself after a bounded batch (slow add-on catalogs) cannot hold up the others.
        while pending := await self.repo.store.fetchall(
            "SELECT id FROM playlist_runs WHERE state IN ('queued','publishing') ORDER BY created"
        ):
            for row in pending:
                await self.process(row["id"])

    async def process(self, ident: str) -> None:
        async with self.worker:
            await self._process(ident)

    async def _process(self, ident: str) -> None:
        run = await self.repo.run(ident)
        if run["state"] not in ("queued", "publishing"):
            return
        mix_row = await self.repo.store.fetchone(
            "SELECT * FROM playlist_mixes WHERE id=?", [run["mix"]]
        )
        if not mix_row:
            return
        mix = dict(mix_row)
        data = run["data"]
        stage = "authorization"
        try:
            with anyio.fail_after(self.settings.run_seconds):
                connection = await self.connection(mix["owner"])
                await self.requests.guard(mix)
                if run["state"] == "queued":
                    async with self.changes:
                        # Authorization can wait while a dashboard action pauses or
                        # discards this run. Preserve its current choices and state.
                        run = await self.repo.run(ident)
                        if run["state"] != "queued":
                            return
                        mix = await self.repo.mix(mix["id"], connection.user_id)
                        data = run["data"]
                        if run["automatic"] and not mix["enabled"]:
                            await self.repo.update(ident, "discarded", data)
                            return
                        await self.repo.update(ident, "generating", data)
                    stage = "selection"
                    if Definition.model_validate_json(mix["definition"]).mode == "import":
                        data = await self.imports.resolve(connection, mix, ident, data)
                        if data is None:
                            await self.imports.requeue(ident)
                            return
                    else:
                        data = await self.generate(connection, mix)
                    async with self.changes:
                        current = await self.repo.run(ident)
                        connected = await self.repo.connection(connection.user_id)
                        if current["state"] != "generating" or connected != connection:
                            if current["state"] == "generating":
                                raise PlaylistError(
                                    "The connection changed. Generate the playlist again.",
                                )
                            return
                        # Also the grants the new selection relied on, before it is saved.
                        await self.requests.guard(mix, data)
                        mix = await self.repo.mix(mix["id"], connection.user_id)
                        if run["automatic"] and not mix["enabled"]:
                            await self.repo.update(ident, "discarded", data)
                            return
                        # Freeze both the music and the owner's current publication /
                        # refresh choices before entering the existing publisher.
                        publish = current["data"].get("publish_after_generation") is True
                        data["publish_after_generation"] = publish
                        data["enable_after"] = current["data"].get("enable_after", False)
                        if run["automatic"] and "publish_after_generation" not in current["data"]:
                            await self.repo.store.execute(
                                "UPDATE playlist_mixes SET enabled=0 WHERE id=?", [mix["id"]]
                            )
                        async with self.repo.store.transaction() as db:
                            # The generated title and selections become durable together.
                            # Never change the original request input or its replay hash.
                            if Definition.model_validate_json(mix["definition"]).auto_name:
                                await db.execute(
                                    "UPDATE playlist_mixes SET definition=? WHERE id=?",
                                    [json.dumps(data["definition"]), mix["id"]],
                                )
                            await db.execute(
                                "UPDATE playlist_runs SET state=?,data=?,error='' WHERE id=?",
                                ["publishing" if publish else "preview", json.dumps(data), ident],
                            )
                    if not publish:
                        return
                if Definition.model_validate_json(mix["definition"]).mode == "import":
                    stage = "publication"
                    await self.imports.publish(connection, mix, ident, data)
                    return
                async with self.changes:
                    current = await self.repo.run(ident)
                    connected = await self.repo.connection(connection.user_id)
                    if current["state"] != "publishing":
                        return
                    if connected != connection:
                        raise PlaylistError(
                            "The connection changed. Retry with the current connection."
                        )
                    mix = await self.repo.mix(mix["id"], connection.user_id)
                    data = current["data"]
                    stage = "publication"
                    await self.requests.guard(mix, data)
                    await self.publish(connection, mix, ident, data)
        except Exception as exc:
            log.warning("playlist run failed: stage=%s error=%s", stage, type(exc).__name__)
            # A stored intent may have reached Navidrome. Do not describe that as an
            # unchanged playlist or issue a fresh creation after an ambiguous answer.
            async with self.changes:
                try:
                    current = await self.repo.run(ident)
                except PlaylistError:
                    return  # Explicit disconnect deleted this owner's private state.
                if current["state"] not in ("generating", "publishing", "queued"):
                    return
                data = current["data"]
                uncertain = bool(data.get("intent"))
                error = (
                    str(exc)
                    if isinstance(exc, PlaylistError)
                    else "The run could not finish. Check the connections and retry."
                )
                await self.repo.update(ident, "uncertain" if uncertain else "failed", data, error)
                if run["automatic"] or data.get("publish_after_generation"):
                    await self.repo.store.execute(
                        "UPDATE playlist_mixes SET enabled=0 WHERE id=?", [mix["id"]]
                    )

    async def generate(self, connection: Connection, mix: dict[str, Any]) -> dict[str, Any]:
        definition = Definition.model_validate_json(mix["definition"])
        baseline = ""
        original_name = ""
        edits: Edits | None = None  # a refresh: the owner's edits of its playlist
        if mix["playlist_id"]:
            playlist = await self.client.playlist(connection, mix["playlist_id"])
            baseline = fingerprint(playlist)
            original_name = playlist["name"]
            learned: tuple[list[Track], list[Track]] = ([], [])
            if mix["published"] and baseline != mix["published"]:
                learned = await learn_edits(self, connection, mix, playlist)
            edits = await remembered_edits(self.repo.store, mix["id"])
            edits.learned_removed, edits.learned_added = learned
        if definition.mode != "discovery":
            if definition.mode == "prompt":
                self.requests.usable_ai(connection)

            async def authorize() -> None:
                if await self.repo.connection(connection.user_id) != connection:
                    raise PlaylistError(
                        "The connection changed. Retry with the current connection."
                    )
                await self.requests.guard(mix)

            async def library() -> bool:
                return await self.requests.reads_library(mix["id"])

            curated = await generate_prompt(
                self,
                connection,
                definition,
                authorize=authorize,
                own=mix["playlist_id"] or "",
                edits=edits,
                library=library,
            )
            if definition.auto_name:
                definition = definition.model_copy(
                    update={"name": curated.name, "auto_name": False}
                )
            data = await self.freeze(
                definition,
                curated.selected,
                baseline,
                original_name,
                history_from=0,
                history_count=0,
                skipped=curated.skipped,
            )
            if curated.curation:
                data["curation"] = curated.curation
            if edits is not None:
                data["edits"] = edits.summary()
            return data
        listens, first = await self.history.sync(connection)
        heard = {music_key(s["artist"], s["title"]) for s in listens}
        recent = {
            music_key(s["artist"], s["title"])
            for s in listens
            if s["at"] >= time.time() - definition.exclude_days * 86400
        }
        previous = (
            await self.repo.store.fetchall(
                "SELECT data FROM playlist_runs WHERE mix=? AND state='published' AND created>=?",
                [mix["id"], time.time() - definition.repeat_days * 86400],
            )
            if definition.repeat_days
            else []
        )
        repeats = {
            Track.model_validate(s).key
            for row in previous
            for s in json.loads(row["data"]).get("selected", [])
        }
        candidates = await self.history.candidates(connection, listens)
        pool: dict[str, Track] = {}
        skipped = 0
        for wanted in candidates:
            key = music_key(wanted["artist"], wanted["title"])
            if key in recent or key in repeats:
                continue
            if edits is not None and edits.leaves_out(key):
                continue
            detailed = await self.history.info(connection, wanted)
            found = await self.resolver.resolve(connection, detailed)
            if found is not None:
                found = await self.matched_edition(found)
            if found is None or found.key in recent or found.key in repeats:
                skipped += 1
                continue
            if edits is not None and edits.leaves_out(found.key, found.id):
                continue
            found = found.model_copy(update={"unfamiliar": found.key not in heard})
            if found.key not in pool or pool[found.key].score < found.score:
                pool[found.key] = found
        selected = await self.curator.select(
            connection,
            definition,
            sorted(pool.values(), key=lambda s: -s.score),
            edits=edits.shown() if edits is not None else {},
        )
        data = await self.freeze(
            definition,
            selected,
            baseline,
            original_name,
            history_from=first,
            history_count=len(listens),
            skipped=skipped,
        )
        if edits is not None:
            data["edits"] = edits.summary()
        return data

    async def freeze(
        self,
        definition: Definition,
        selected: list[Track],
        baseline: str,
        original_name: str,
        *,
        history_from: int,
        history_count: int,
        skipped: int,
        addons: bool = False,
    ) -> dict[str, Any]:
        """``addons`` (imports): releases of the add-ons' catalogs too."""
        releases = []
        for ref in sorted({s.release for s in selected if s.release}):
            catalog = await self.catalog_of(CatalogRef.parse(ref).catalog, addons=addons)
            if catalog is None:
                if addons:
                    raise PlaylistError(
                        "The catalog of a matched release is unavailable. Retry matching."
                    )
                continue
            album = await catalog.album(CatalogRef.parse(ref).id)
            releases.append(
                {
                    "ref": ref,
                    "title": album.title,
                    "artist": album.artist,
                    "tracks": len(album.tracks),
                    "fingerprint": release_fingerprint(album),
                }
            )
        return {
            "definition": definition.model_dump(),
            "selected": [s.model_dump() for s in selected],
            "baseline": baseline,
            "original_name": original_name,
            "releases": releases,
            "history_from": history_from,
            "history_count": history_count,
            "skipped": skipped,
            "native_ids": [],
            "intent": "",
            "created_id": "",
        }

    async def catalog_of(self, key: str, *, addons: bool = False) -> Catalog | None:
        """The catalog of the items with the key ``key``: the installation's, else - for
        imports (``addons``) - the catalog of the enabled add-on with that key."""
        if self.catalog is not None and self.catalog.key == key:
            return self.catalog
        if addons and self.addons is not None:
            return await self.addons.by_key(key)
        return None

    async def matched_edition(self, track: Track) -> Track | None:
        """Prefer a confidently equivalent recording in the existing album plan.

        Freeze that edition during selection, so publication's release approval
        still covers exactly the release the normal fill path will add. Owned album
        plans are the catalog's: a release of an add-on's catalog stays as it is.
        """
        if not track.release or not self.catalog or not self.commits or not self.commits.owned:
            return track
        if CatalogRef.parse(track.release).catalog != self.catalog.key:
            return track
        release = await self.catalog.album(CatalogRef.parse(track.release).id)
        planned = await self.commits.owned.matched_release(release)
        if planned is None or planned.ref == release.ref:
            return track
        fresh = await self.catalog.album(planned.ref.id)
        if release_fingerprint(fresh) != release_fingerprint(planned):
            return None
        matches = []
        for song in fresh.tracks:
            candidate = track.model_copy(
                update={
                    "id": song_id(song.ref),
                    "release": str(fresh.ref),
                    "album": fresh.title,
                    "artist": song.artist,
                    "title": song.title,
                    "duration": song.seconds,
                    "isrc": song.isrc or "",
                    "explicit": song.explicit,
                    "clean": song.clean,
                }
            )
            if (
                track.isrc
                and candidate.isrc == track.isrc
                and candidate.explicit == track.explicit
                and candidate.clean == track.clean
                and same_recording(track.model_dump(), candidate)
            ):
                matches.append(candidate)
        # A release's duplicate or uncorroborated recording is not a safe substitute.
        return matches[0] if len(matches) == 1 else None

    async def check_tracks(
        self,
        connection: Connection,
        selected: list[Track],
        approved: dict[str, str],
        *,
        library_bound: bool = False,
        authorize: Callable[[], Awaitable[None]] | None = None,
        addons: bool = False,
    ) -> list[str]:
        """``addons`` (imports): recordings of the add-ons' catalogs too, added from
        those catalogs."""
        mapping: dict[str, str] = {}
        catalog_ids: set[str] = set()
        others: dict[str, Catalog] = {}  # the add-ons' catalogs, by key
        for song in selected:
            cid = CatalogId.parse(song.id)
            if cid:
                catalog = await self.catalog_of(cid.ref.catalog, addons=addons)
                if catalog is None or not self.commits:
                    raise PlaylistError("The catalog connection is unavailable.")
                try:
                    if catalog is self.catalog:
                        track = await catalog.song(cid.ref.id)
                    else:
                        # No request for one song (and none remembered after a restart):
                        # the song as its release lists it now.
                        answer = await listed(catalog, cid.ref, song.release)
                        if answer is None:
                            raise CatalogError("not_found", "the release does not list it")
                        track = answer[0]
                        others[catalog.key] = catalog
                except CatalogError:
                    log.warning("playlist preparation failed: category=recording_unavailable")
                    raise PlaylistError(
                        _with_track(
                            "A selected catalog recording is no longer available. Generate again.",
                            song,
                        )
                    ) from None
                # Never quietly publish a new version under the same catalog identifier.
                if (
                    track.artist != song.artist
                    or track.title != song.title
                    or track.seconds != song.duration
                    or (track.isrc or "") != song.isrc
                    or track.explicit != song.explicit
                    or track.clean != song.clean
                    or str(track.album or "") != song.release
                ):
                    log.warning("playlist preparation failed: category=selection_changed")
                    raise PlaylistError(
                        _with_track("A selected recording changed. Generate again.", song)
                    )
                catalog_ids.add(song.id)
        # Keep release approval and fresh authorization task-local through commits,
        # first-use fills and restorations; the engine checks again after its waits.
        if catalog_ids:
            assert self.commits is not None
            if library_bound:
                await self.client.validate(connection)

            def check(release: CatalogRelease) -> None:
                if approved.get(str(release.ref)) != release_fingerprint(release):
                    raise MaterializeError("the release differs from the saved selection")

            authorization = (
                self.commits.engine.guard_writes(
                    authorize or (lambda: self.client.validate(connection))
                )
                if library_bound
                else nullcontext()
            )
            with self.commits.engine.guard_materializations(check), authorization:
                try:
                    mapping = await self.commits.natives(catalog_ids, catalogs=others)
                except CommitError as exc:
                    if exc.category in _PREPARATION_ERRORS:
                        category = exc.category
                        error = _PREPARATION_ERRORS[category]
                    elif exc.category == "album_collision" or exc.message.endswith(
                        "the placeholders joined another album"
                    ):
                        category = "album_collision"
                        error = (
                            "A selected release conflicts with an existing library album. "
                            "Check that album in Library. For No match, choose Match again, "
                            "then retry this selection."
                        )
                    elif exc.category == "owned_fill" or exc.message == (
                        "could not add the album's missing songs"
                    ):
                        category = "owned_fill"
                        error = (
                            "A selected track belongs to an album whose missing tracks could "
                            "not be added. Retry this selection later. If Library lists the "
                            "album under No match, choose Match again before retrying."
                        )
                    else:
                        category = "library_preparation"
                        error = (
                            "Some selected tracks could not be prepared in the library. "
                            "Retry this selection after checking the catalog and album matches."
                        )
                    # The original error can contain private adapter details. Log only
                    # our fixed category and retain the frozen selection for recovery.
                    log.warning("playlist preparation failed: category=%s", category)
                    affected = next((s for s in selected if s.id == exc.item), None)
                    if affected is None and len(selected) == 1:
                        affected = selected[0]
                    if affected is not None:
                        error = _with_track(error, affected)
                    raise PlaylistError(error) from None
        ids = [mapping.get(s.id, s.id) for s in selected]
        for song, native_id in zip(selected, ids, strict=True):
            native = await self.native_track(connection, native_id, library_bound=library_bound)
            if not same_recording(song.model_dump(), native):
                raise PlaylistError(
                    "A resolved library track differs from the selection. The playlist was kept."
                )
        return ids

    async def native_track(
        self,
        connection: Connection,
        ident: str,
        *,
        library_bound: bool,
    ) -> Track:
        # The owner-authenticated API verifies accessibility. Native metadata verifies
        # the installation's exact library too, including on recovery after a move.
        answer = (await self.client.call(connection, "getSong", [("id", ident)]))["song"]
        if answer.get("id") != ident:
            raise PlaylistError("The native recording identity could not be verified.")
        if library_bound:
            try:
                metadata = await self.client.native.song(ident)
            except NavidromeError:
                raise PlaylistError("The recording's library could not be verified.") from None
            if not metadata or metadata.get("libraryId") != self.client.native.library_id:
                raise PlaylistError("A selected recording is no longer in this library.")
        return native_track(answer)

    async def publish(
        self, connection: Connection, mix: dict[str, Any], ident: str, data: dict[str, Any]
    ) -> None:
        await self.client.validate(connection)
        definition = Definition.model_validate(data["definition"])
        if definition != Definition.model_validate_json(mix["definition"]):
            raise PlaylistError("This selection uses previous mix rules. Generate again.")
        selected = [Track.model_validate(s) for s in data["selected"]]
        if definition.mode == "import":
            await self.imports.publish(connection, mix, ident, data)
            return
        validate(selected, definition)
        target = data.get("created_id") or mix["playlist_id"]
        current: dict[str, Any] | None = None
        if target:
            current = await self.client.playlist(connection, target)
        elif data.get("intent") == "create":
            # Creating a playlist has no idempotency key. Only the persisted random
            # provisional name + owner + exact contents can conclusively reconcile it.
            listing = await self.client.call(connection, "getPlaylists")
            matches = [
                p
                for p in listing.get("playlists", {}).get("playlist", [])
                if p.get("name") == data["provisional"] and p.get("owner") == connection.username
            ]
            if len(matches) != 1:
                raise PlaylistError(
                    "Playlist creation has an uncertain result. Recheck after Navidrome recovers;"
                    " no second playlist will be created."
                )
            target = matches[0]["id"]
            current = await self.client.playlist(connection, target)
            data["created_id"] = target
            await self.repo.update(ident, "publishing", data)
        ids = data.get("native_ids", [])
        if data.get("intent"):
            if current and [s["id"] for s in current.get("entry", [])] == ids:
                await self.finish(connection, mix, ident, data, current)
                return
            if not target or not current or fingerprint(current) != data["baseline"]:
                raise PlaylistError(
                    "The saved playlist differs from both the selection and the publication intent."
                    " Shijhon has stopped to preserve it."
                )
        elif current and fingerprint(current) != data["baseline"]:
            raise PlaylistError(
                "The playlist changed after selection. Shijhon has kept those edits."
            )
        if not ids:
            if self.catalog:
                fresh = (
                    self.catalog.inner if isinstance(self.catalog, CachedCatalog) else self.catalog
                )
                for expected in data.get("releases", []):
                    album = await fresh.album(CatalogRef.parse(expected["ref"]).id)
                    if release_fingerprint(album) != expected["fingerprint"]:
                        raise PlaylistError(
                            "A selected release changed. Generate again before adding it."
                        )
            approved = {r["ref"]: r["fingerprint"] for r in data.get("releases", [])}

            async def authorize() -> None:
                if await self.repo.connection(connection.user_id) != connection:
                    raise PlaylistError(
                        "The connection changed. Retry with the current connection."
                    )
                await self.requests.guard(mix, data)

            ids = await self.check_tracks(
                connection,
                selected,
                approved,
                library_bound=definition.mode != "discovery",
                authorize=authorize,
            )
            data["native_ids"] = ids
            await self.repo.update(ident, "publishing", data)
        else:
            # Previously prepared IDs can disappear between a stop and its recovery.
            for song, native_id in zip(selected, ids, strict=True):
                native = await self.native_track(
                    connection, native_id, library_bound=definition.mode != "discovery"
                )
                if not same_recording(song.model_dump(), native):
                    raise PlaylistError("A prepared track is no longer the selected recording.")
        await self.client.validate(connection)
        if target:
            current = await self.client.playlist(connection, target)
            if fingerprint(current) != data["baseline"]:
                raise PlaylistError(
                    "The playlist changed while tracks were prepared. Shijhon has kept those edits."
                )
            data["intent"] = "replace"
            params = [("playlistId", target)]
        else:
            data["provisional"] = f"Shijhon preparing {secrets.token_hex(16)}"
            data["intent"] = "create"
            params = [("name", data["provisional"])]
        # Intent is durable BEFORE the remote mutation. Recovery never chooses new music.
        await self.repo.update(ident, "publishing", data)
        reply = await self.client.call(
            connection, "createPlaylist", [*params, *(("songId", i) for i in ids)]
        )
        if not target:
            target = reply["playlist"]["id"]
            data["created_id"] = target
            await self.repo.update(ident, "publishing", data)
        current = await self.client.playlist(connection, target)
        if [s["id"] for s in current.get("entry", [])] != ids:
            raise PlaylistError(
                "Navidrome's saved track order did not match the intended playlist."
                " Recheck this run before continuing."
            )
        await self.finish(connection, mix, ident, data, current)

    async def finish(
        self,
        connection: Connection,
        mix: dict[str, Any],
        ident: str,
        data: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        # Reconciliation of matching IDs must still confirm the frozen recordings.
        definition = Definition.model_validate(data["definition"])
        selected = [Track.model_validate(s) for s in data["selected"]]
        for song, native_id in zip(selected, data["native_ids"], strict=True):
            native = await self.native_track(
                connection, native_id, library_bound=definition.mode != "discovery"
            )
            if not same_recording(song.model_dump(), native):
                raise PlaylistError("A saved playlist track is no longer the selected recording.")
        expected = data["definition"]["name"]
        if current["name"] != expected:
            original = (
                data.get("provisional") if data["intent"] == "create" else data.get("original_name")
            )
            if current["name"] == original:
                await self.client.call(
                    connection,
                    "updatePlaylist",
                    [("playlistId", current["id"]), ("name", expected)],
                )
                current = await self.client.playlist(connection, current["id"])
            if current["name"] != expected:
                raise PlaylistError(
                    "The playlist was renamed outside Shijhon. Recheck its state before continuing."
                )
        if [s["id"] for s in current.get("entry", [])] != data["native_ids"]:
            raise PlaylistError("The playlist changed before publication could be verified.")
        principal = None
        if self.published is not None:
            await self.client.validate(connection)
            principal = await self.client.principal(connection)
        async with self.repo.store.transaction() as db:
            if principal is not None and self.published is not None:
                await self.published(principal, selected, data["native_ids"], db)
            await db.execute(
                "UPDATE playlist_mixes SET playlist_id=?,published=?,due=?,"
                "enabled=CASE WHEN ? THEN 1 ELSE enabled END WHERE id=?",
                [
                    current["id"],
                    fingerprint(current),
                    time.time() + definition.interval,
                    bool(data.get("enable_after")) and bool(definition.interval),
                    mix["id"],
                ],
            )
            await db.execute(
                "UPDATE playlist_runs SET state='published',data=?,error='' WHERE id=?",
                [json.dumps(data), ident],
            )
