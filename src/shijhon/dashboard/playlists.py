"""Personal playlist/profile forms; host AI configuration requires an administrator."""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from shijhon.dashboard.auth import Session
from shijhon.dashboard.playlist_imports import Pages as ImportPages
from shijhon.dashboard.playlist_requests import Pages as RequestPages
from shijhon.dashboard.playlist_requests import edits_view
from shijhon.playlists.edits import forget, remembered
from shijhon.playlists.model import Connection, Definition, PlaylistError
from shijhon.playlists.repository import AI_FIELDS
from shijhon.playlists.service import Playlists

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard


def version(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def definition(form: dict[str, str]) -> Definition:
    spec = Definition.model_validate(
        {
            "name": form.get("name", ""),
            "count": form.get("count", "30"),
            "unfamiliar_percent": form.get("unfamiliar_percent", "70"),
            "max_per_artist": form.get("max_per_artist", "2"),
            "exclude_days": form.get("exclude_days", "14"),
            "repeat_days": form.get("repeat_days", "28"),
            "cadence": form.get("cadence", "weekly"),
            "brief": form.get("brief", "") if form.get("ai") == "true" else "",
            "ai": form.get("ai") == "true",
            # Direct publication needs the controls shown by this form. A page
            # promising a preview must keep that intent when submitted later.
            "review_before_publishing": form.get("publication_mode") != "direct"
            or form.get("review_before_publishing") == "true",
        }
    )
    if form.get("enable") == "true" and not spec.interval:
        raise PlaylistError("Choose every day or every week to enable regular refreshes.")
    return spec


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    def routes(self) -> list[Route]:
        page = self.dashboard._personal_page
        return [
            Route("/playlists", page(self.index), methods=["GET", "POST"]),
            Route("/playlists/connection", page(self.connect), methods=["POST"]),
            Route("/playlists/ai-preferences", page(self.preferences), methods=["POST"]),
            Route("/playlists/host-ai", page(self.host_ai), methods=["POST"]),
            Route("/playlists/private-ai", page(self.private_ai), methods=["POST"]),
            Route("/playlists/forget", page(self.forget), methods=["POST"]),
            *RequestPages(self.dashboard).routes(),
            *ImportPages(self.dashboard).routes(),
            Route("/playlists/{mix}", page(self.detail), methods=["GET", "POST"]),
            Route("/playlists/{mix}/action", page(self.action), methods=["POST"]),
        ]

    @property
    def service(self) -> Playlists:
        services = self.dashboard.services
        if not services or not services.playlists:
            raise PlaylistError("Playlist services are starting.")
        return services.playlists

    async def owned_connection(self, session: Session) -> Connection:
        connection = await self.service.connection(session.user_id)
        if not session.user_id or connection.user_id != session.user_id:
            raise PlaylistError(
                "These playlist settings belong to a previous account."
                " Forget them before reconnecting."
            )
        return connection

    async def render(
        self,
        session: Session,
        *,
        error: str = "",
        mix: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        rules_form: dict[str, str] | None = None,
    ) -> Response:
        service = self.service
        connection = await service.repo.connection(session.user_id)
        if connection and connection.user_id != session.user_id:
            connection = None
            error = (
                error
                or "A previous account's playlist settings must be forgotten before reconnecting."
            )
        mixes = await service.repo.mixes(connection.user_id) if connection else []
        shown = []
        for row in mixes:
            spec = Definition.model_validate_json(row["definition"])
            if spec.mode == "discovery":
                shown.append({**row, "definition": spec})
        runs = await service.repo.runs(mix["id"]) if mix else []
        learning = (
            edits_view(
                runs[0]["data"].get("edits") if runs else None,
                await remembered(service.repo.store, mix["id"]),
            )
            if mix
            else None
        )
        fields: dict[str, Any] = {
            "history_user": connection.history_user if connection else "",
            "history_source": connection.history_source if connection else "navidrome",
            "backend": connection.backend if connection else "none",
            "model": connection.model if connection else "",
            "endpoint": connection.endpoint if connection else "",
        }
        shared = await service.repo.shared_ai()
        if session.is_admin and shared:
            fields.update({key: shared[key] for key in ("backend", "model", "endpoint")})
        if form is not None:
            # Preserve only non-secret choices after a refused save.
            fields.update({key: form[key] for key in fields if key in form})
        if not session.is_admin:
            # Even a formerly administrative profile's endpoint/model is host state.
            fields.update(backend="none", model="", endpoint="")
        choices = await service.ai_choices(session.user_id)
        spec = Definition.model_validate_json(mix["definition"]) if mix else Definition()
        rules_values = spec.model_dump()
        rules_enabled = bool(mix and mix["enabled"])
        if rules_form is not None:
            for key in rules_values:
                if key in rules_form:
                    rules_values[key] = rules_form[key]
            rules_values["ai"] = rules_form.get("ai") == "true"
            rules_values["review_before_publishing"] = (
                rules_form.get("publication_mode") != "direct"
                or rules_form.get("review_before_publishing") == "true"
            )
            rules_enabled = rules_form.get("enable") == "true"
        return await self.dashboard._render(
            "playlists.html",
            session=session,
            current="playlists",
            error=error,
            connected=bool(connection),
            **fields,
            saved_history_source=(
                connection.history_source
                if connection and connection.history_key.get_secret_value()
                else ""
            ),
            ai_mode=(
                "shared"
                if connection and connection.use_shared_ai
                else "keep"
                if connection and connection.backend != "none"
                else "none"
            ),
            shared_ai_available=bool(shared and shared.get("backend") != "none"),
            ai_choices=choices,
            private_ai=(
                {key: getattr(connection, key) for key in ("backend", "model", "endpoint")}
                if session.is_admin
                and connection
                and not connection.use_shared_ai
                and connection.backend != "none"
                else None
            ),
            allowed_models="\n".join(shared.get("allowed_models", [])) if session.is_admin else "",
            mixes=shown,
            mix=mix,
            learning=learning,
            definition=spec,
            rules_definition=rules_values,
            rules_enabled=rules_enabled,
            request_key=(rules_form or {}).get("request_key") or secrets.token_hex(16),
            mix_version=version(mix["definition"]) if mix else "",
            runs=runs,
            pending_refresh=any(
                r["data"].get("enable_after")
                and r["state"] in ("queued", "generating", "preview", "publishing", "uncertain")
                for r in runs
            ),
            cli_enabled=service.settings.allow_cli,
            history_days=service.settings.history_days,
        )

    async def index(self, request: Request, session: Session) -> Response:
        if request.method != "POST":
            return await self.render(session)
        try:
            service = self.service
            connection = await self.owned_connection(session)
            connection.require_history()
            spec = definition(request.state.form)
            async with service.changes:
                ident = await service.repo.create_discovery(
                    connection.user_id,
                    request.state.form.get("request_key", ""),
                    spec,
                    enable_after=request.state.form.get("enable") == "true",
                )
                service.notify()
            return self.dashboard._see(f"/playlists/{ident}")
        except (PlaylistError, ValidationError) as exc:
            error = (
                str(exc)
                if isinstance(exc, PlaylistError)
                else "Check the mix rules. A brief requires AI curation."
            )
            return await self.render(session, error=error, rules_form=request.state.form)

    async def connect(self, request: Request, session: Session) -> Response:
        form = request.state.form
        service = self.service
        try:
            if not session.is_admin and (
                any(key in form for key in (*AI_FIELDS, "allowed_models")) or "share_ai" in form
            ):
                raise PlaylistError("Only administrators can configure the host AI service.")
            async with service.changes:
                old = await service.repo.connection(session.user_id)
                password = form.get("password", "")
                jwt = ""
                if password:
                    peer = request.client.host if request.client else ""
                    client = self.dashboard.forwarding.client(peer, request.headers.raw)
                    login = await service.client.login(
                        session.username, password, str(client or "127.0.0.1")
                    )
                    owner_id, token, salt = (
                        login["id"],
                        login["subsonicToken"],
                        login["subsonicSalt"],
                    )
                    if owner_id != session.user_id:
                        raise PlaylistError("Your account changed. Sign out and sign in again.")
                    jwt = login.get("token", "")
                elif old:
                    owner_id, token, salt = (
                        old.user_id,
                        old.token.get_secret_value(),
                        old.salt.get_secret_value(),
                    )
                else:
                    raise PlaylistError(
                        "Enter your Navidrome password to authorize playlist refreshes."
                    )
                history_source = form.get(
                    "history_source", old.history_source if old else "navidrome"
                )
                history_user = form.get("history_user", old.history_user if old else "").strip()
                if not history_user:
                    history_key = old.history_key if old else SecretStr("")
                elif history_source == "navidrome":
                    if password:
                        history_key = await service.client.history_key(jwt)
                    elif old and old.history_source == "navidrome":
                        history_key = old.history_key
                    else:
                        raise PlaylistError(
                            "Enter your Navidrome password to read its Last.fm API key."
                        )
                else:
                    history_key = SecretStr(
                        form.get("history_key", "")
                        or (
                            old.history_key.get_secret_value()
                            if old and old.history_source == "manual"
                            else ""
                        )
                    )
                ai_mode = form.get("ai_mode", "keep" if old else "shared")
                if ai_mode not in ("keep", "shared", "none"):
                    raise PlaylistError("Choose a saved AI service or no AI.")
                shared_ai = ai_mode == "shared" or bool(
                    ai_mode == "keep" and old and old.use_shared_ai
                )
                if session.is_admin and "backend" in form:
                    shared_ai = False
                    backend = form["backend"]
                    endpoint = form.get("endpoint", "").strip() if backend == "api" else ""
                    model = form.get("model", "").strip() if backend != "none" else ""
                elif old and ai_mode == "keep":
                    backend, endpoint, model = old.backend, old.endpoint, old.model
                else:
                    backend, endpoint, model = "none", "", ""
                if backend == "api" and (not endpoint or not model):
                    raise PlaylistError("Enter an API base URL and model for the API connection.")
                connection = Connection(
                    user_id=owner_id,
                    username=session.username,
                    token=SecretStr(token),
                    salt=SecretStr(salt),
                    history_user=history_user,
                    history_key=history_key,
                    history_source=history_source,
                    backend=backend,
                    endpoint=endpoint,
                    model=model,
                    use_shared_ai=shared_ai,
                    api_key=SecretStr(
                        (form.get("api_key", "") if backend == "api" else "")
                        or (
                            old.api_key.get_secret_value()
                            if old and old.backend == backend == "api" and old.endpoint == endpoint
                            else ""
                        )
                    ),
                )
                if history_user and not connection.history_key.get_secret_value():
                    raise PlaylistError("Enter a Last.fm API key.")
                await service.save_connection(
                    connection,
                    configure_host=form.get("share_ai") == "true",
                    allowed_models=(
                        [
                            line.strip()
                            for line in form["allowed_models"].splitlines()
                            if line.strip()
                        ]
                        if session.is_admin and "allowed_models" in form
                        else None
                    ),
                )
            return self.dashboard._see("/playlists")
        except (PlaylistError, ValidationError) as exc:
            error = str(exc) if isinstance(exc, PlaylistError) else "Check the connection fields."
            return await self.render(session, error=error, form=form)

    async def private_ai(self, request: Request, session: Session) -> Response:
        if not session.is_admin:
            return await self.render(
                session, error="Only administrators can configure the host AI service."
            )
        return await self.connect(request, session)

    async def host_ai(self, request: Request, session: Session) -> Response:
        form = request.state.form
        try:
            if not session.is_admin:
                raise PlaylistError("Only administrators can configure the host AI service.")
            async with self.service.changes:
                old = await self.owned_connection(session)
                shared = await self.service.repo.shared_ai()
                backend = form.get("backend", "none")
                endpoint = form.get("endpoint", "").strip() if backend == "api" else ""
                model = form.get("model", "").strip() if backend != "none" else ""
                if backend == "api" and (not endpoint or not model):
                    raise PlaylistError("Enter an API base URL and model for the API connection.")
                previous = shared or {**old.model_dump(), "api_key": old.api_key.get_secret_value()}
                api_key = (
                    form.get("api_key", "")
                    or (
                        previous.get("api_key", "")
                        if previous.get("backend") == "api" and previous.get("endpoint") == endpoint
                        else ""
                    )
                    if backend == "api"
                    else ""
                )
                configured = Connection.model_validate(
                    {
                        **old.model_dump(),
                        "backend": backend,
                        "endpoint": endpoint,
                        "model": model,
                        "api_key": api_key,
                        "use_shared_ai": False,
                    }
                )
                models = [
                    line.strip()
                    for line in form.get("allowed_models", "").splitlines()
                    if line.strip()
                ]
                await self.service.configure_shared_ai(configured, allowed_models=models)
            return self.dashboard._see("/playlists")
        except (PlaylistError, ValidationError) as exc:
            message = str(exc) if isinstance(exc, PlaylistError) else "Check the host AI fields."
            return await self.render(session, error=message, form=form)

    async def preferences(self, request: Request, session: Session) -> Response:
        try:
            form = request.state.form
            if set(form) - {"csrf", "ai_mode", "preferred_model"}:
                raise PlaylistError("Personal AI preferences accept a service and approved model.")
            mode = form.get("ai_mode", "shared")
            if mode not in ("shared", "none"):
                raise PlaylistError("Choose the shared AI service or no AI.")
            async with self.service.changes:
                await self.service.choose_ai_model(
                    session.user_id,
                    form.get("preferred_model", ""),
                    use_shared_ai=mode == "shared",
                )
            return self.dashboard._see("/playlists")
        except (PlaylistError, ValidationError) as exc:
            message = str(exc) if isinstance(exc, PlaylistError) else "Choose an approved model."
            return await self.render(session, error=message)

    async def forget(self, request: Request, session: Session) -> Response:
        service = self.service
        async with service.changes:
            await service.repo.disconnect(session.user_id)
        return self.dashboard._see("/playlists")

    async def detail(self, request: Request, session: Session) -> Response:
        mix: dict[str, Any] | None = None
        try:
            service = self.service
            connection = await service.repo.connection(session.user_id)
            if not connection:
                raise PlaylistError("Connect your Navidrome account first.")
            if connection.user_id != session.user_id:
                raise PlaylistError("This mix is unavailable for this account.")
            if request.method != "POST":
                mix = await service.repo.mix(request.path_params["mix"], connection.user_id)
                if Definition.model_validate_json(mix["definition"]).mode != "discovery":
                    return self.dashboard._see(f"/playlists/describe/{mix['id']}")
                return await self.render(session, mix=mix)
            connection = await self.owned_connection(session)
            async with service.changes:
                mix = await service.repo.mix(request.path_params["mix"], connection.user_id)
                if Definition.model_validate_json(mix["definition"]).mode != "discovery":
                    raise PlaylistError("Use this request's Describe a playlist page.")
                if request.method == "POST":
                    if request.state.form.get("version") != version(mix["definition"]):
                        raise PlaylistError("This mix changed. Reload the page before saving.")
                    active = await service.repo.runs(mix["id"])
                    if any(r["state"] in ("publishing", "uncertain") for r in active):
                        raise PlaylistError(
                            "Finish checking the current publication before changing the mix."
                        )
                    spec = definition(request.state.form)
                    await service.repo.store.execute(
                        "UPDATE playlist_runs SET state='discarded' WHERE mix=?"
                        " AND state IN ('queued','generating','preview')",
                        [mix["id"]],
                    )
                    await service.repo.store.execute(
                        "UPDATE playlist_mixes SET definition=?,enabled=0 WHERE id=?",
                        [spec.model_dump_json(), mix["id"]],
                    )
                    await service.repo.enqueue(
                        mix["id"], enable_after=request.state.form.get("enable") == "true"
                    )
                    service.notify()
                    return self.dashboard._see(f"/playlists/{mix['id']}")
            return await self.render(session, mix=mix)
        except (PlaylistError, ValidationError) as exc:
            error = (
                str(exc)
                if isinstance(exc, PlaylistError)
                else "Check the mix rules. A brief requires AI curation."
            )
            return await self.render(
                session,
                error=error,
                mix=mix,
                rules_form=request.state.form if request.method == "POST" else None,
            )

    async def action(self, request: Request, session: Session) -> Response:
        service = self.service
        mix: dict[str, Any] | None = None
        try:
            connection = await self.owned_connection(session)
            async with service.changes:
                mix = await service.repo.mix(request.path_params["mix"], connection.user_id)
                if Definition.model_validate_json(mix["definition"]).mode != "discovery":
                    raise PlaylistError("Use this request's Describe a playlist page.")
                form = request.state.form
                action = form.get("action")
                if action in ("generate", "preview"):
                    await service.repo.enqueue(
                        mix["id"], review=True if action == "preview" else None
                    )
                elif action in ("pause", "enable"):
                    if action == "enable" and (not mix["playlist_id"] or not mix["published"]):
                        raise PlaylistError("Publish the playlist before enabling refreshes.")
                    spec = Definition.model_validate_json(mix["definition"])
                    if action == "enable" and not spec.interval:
                        raise PlaylistError(
                            "Choose a daily or weekly cadence before enabling refreshes."
                        )
                    if action == "pause":
                        await service.repo.pause(mix["id"])
                    else:
                        await service.repo.store.execute(
                            "UPDATE playlist_mixes SET enabled=1,due=? WHERE id=?",
                            [time.time() + spec.interval, mix["id"]],
                        )
                elif action == "forget_edits":
                    await forget(service.repo.store, mix["id"])
                else:
                    run = await service.repo.run(form.get("run", ""))
                    if run["mix"] != mix["id"]:
                        raise PlaylistError("That run does not belong to this mix.")
                    if action == "publish" and run["state"] == "preview":
                        run["data"]["enable_after"] = form.get("enable") == "true"
                        if not run["data"]["enable_after"]:
                            await service.repo.store.execute(
                                "UPDATE playlist_mixes SET enabled=0 WHERE id=?", [mix["id"]]
                            )
                        await service.repo.update(run["id"], "publishing", run["data"])
                    elif (
                        action == "recheck"
                        and run["state"] in ("failed", "uncertain")
                        and run["data"].get("selected")
                    ):
                        if Definition.model_validate(
                            run["data"]["definition"]
                        ) != Definition.model_validate_json(mix["definition"]):
                            raise PlaylistError(
                                "This selection uses previous mix rules. Generate again."
                            )
                        active = await service.repo.runs(mix["id"])
                        if any(
                            r["id"] != run["id"]
                            and r["state"]
                            in ("queued", "generating", "preview", "publishing", "uncertain")
                            for r in active
                        ):
                            raise PlaylistError(
                                "Finish the current run before retrying an older selection."
                            )
                        await service.repo.update(run["id"], "publishing", run["data"])
                    elif action == "discard" and run["state"] == "preview":
                        await service.repo.update(run["id"], "discarded", run["data"])
                    else:
                        raise PlaylistError("That action is no longer available. Reload this page.")
                service.notify()
            return self.dashboard._see(f"/playlists/{mix['id']}")
        except PlaylistError as exc:
            return await self.render(session, error=str(exc), mix=mix)
