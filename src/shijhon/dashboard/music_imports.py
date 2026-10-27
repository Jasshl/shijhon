"""Personal audio imports: bounded JSON controls and raw, streamed file bytes."""

from __future__ import annotations

import json
from hashlib import sha256
from importlib.resources import files
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from shijhon.collection.contracts import CollectionError
from shijhon.dashboard.auth import Session
from shijhon.music_imports import ImportError, ManifestEntry
from shijhon.music_imports.removal import ImportRemovals

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard
    from shijhon.music_imports import MusicImports

MAX_JSON_BYTES = 1024 * 1024


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


async def read_json(request: Request) -> dict[str, Any]:
    """Called only after the central API wrapper has checked identity and CSRF."""
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise ImportError("invalid", "Send JSON from the import page.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_JSON_BYTES:
            raise ImportError("invalid", "The import request is too large. Use smaller pages.")
        body.extend(chunk)
    try:
        value = json.loads(body, object_pairs_hook=_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ImportError("invalid", "Reload the import page and try again.") from None


def _text(value: Any, *, limit: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ImportError("invalid", "Reload the import page and try again.")
    return value


def _number(value: Any, *, maximum: int = 10_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ImportError("invalid", "Choose a valid import page or revision.")
    return value


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    @property
    def service(self) -> MusicImports | None:
        return getattr(self.dashboard.services, "music_imports", None)

    def routes(self) -> list[Route]:
        page, api = self.dashboard._personal_page, self.dashboard._personal_api
        return [
            Route("/collection/import/api", api(self.create), methods=["POST"]),
            Route("/collection/import/api/{ident}", api(self.status)),
            Route("/collection/import/api/{ident}/removal", api(self.removal_status)),
            Route(
                "/collection/import/api/{ident}/removal/{action}",
                api(self.removal_control),
                methods=["POST"],
            ),
            Route(
                "/collection/import/api/{ident}/files/{item}",
                api(self.receive),
                methods=["POST"],
            ),
            Route(
                "/collection/import/api/{ident}/{action}",
                api(self.control),
                methods=["POST"],
            ),
            Route("/collection/import", page(self.index)),
            Route("/collection/import/manage", page(self.manage, require_admin=True)),
            Route("/collection/import/{ident}", page(self.index)),
        ]

    @staticmethod
    def response(value: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(value, status, headers={"cache-control": "no-store"})

    def failure(self, exc: ImportError) -> JSONResponse:
        status = {
            "not_found": 404,
            "access": 403,
            "unavailable": 503,
            "storage": 503,
            "personal_setup_required": 409,
        }.get(exc.code, 400)
        return self.response({"error": str(exc), "code": exc.code}, status)

    def available(self) -> MusicImports:
        if self.service is None:
            raise ImportError(
                "unavailable", "Music imports are not set up. Ask your administrator."
            )
        return self.service

    async def index(self, request: Request, session: Session) -> Response:
        caps: dict[str, Any] = {"enabled": False, "ready": False, "formats": [], "limits": {}}
        history, job, error = (
            [],
            None,
            str(getattr(self.dashboard.services, "music_imports_problem", "") or ""),
        )
        setup_required = False
        try:
            if self.service:
                caps = await self.service.capabilities(session.user_id)
                history = await self.service.listing(session.user_id, limit=20)
                if request.path_params.get("ident"):
                    job = await self.service.status(session.user_id, request.path_params["ident"])
        except ImportError as exc:
            error = str(exc)
            setup_required = exc.code == "personal_setup_required"
        collection = getattr(self.dashboard.services, "collection", None)
        active, status_code = {"name": "your current library"}, 200
        try:
            if collection:
                active = await collection.profile(session.user_id)
        except CollectionError:
            # A later failed access check must not render private receipts fetched earlier.
            history, job, status_code = [], None, 503
            caps = {"enabled": False, "ready": False, "formats": [], "limits": {}}
            error = "Your current account access could not be checked. Reload to try again."
        refresher = getattr(self.dashboard.services, "collection_refresh", None)
        if (
            self.service
            and refresher is not None
            and not refresher.reader.has_bearer(session.user_id)
        ):
            return self.dashboard._see(
                "/sign-in?reauth=1&next=" + quote(request.url.path, safe="/")
            )
        revision = sha256(
            b"".join(
                files("shijhon.dashboard").joinpath("static", name).read_bytes()
                for name in ("music_imports.js", "music_imports.css", "music_import_removals.js")
            )
        ).hexdigest()[:16]
        return await self.dashboard._render(
            "music_imports.html",
            session=session,
            current="collection",
            title="Import music",
            status_code=status_code,
            capabilities=caps,
            setup_required=setup_required,
            job=job,
            history=history,
            active=active,
            error=error,
            asset_revision=revision,
        )

    async def create(self, request: Request, session: Session) -> Response:
        try:
            service = self.available()
            body = await read_json(request)
            return self.response(
                await service.create(session.user_id, _text(body.get("request_key")))
            )
        except ImportError as exc:
            return self.failure(exc)

    async def status(self, request: Request, session: Session) -> Response:
        try:
            try:
                offset = int(request.query_params.get("offset", "0"))
                limit = int(request.query_params.get("limit", "100"))
            except ValueError:
                raise ImportError("invalid", "Choose a valid import page.") from None
            problems = request.query_params.get("problems_only", "0")
            overlap = request.query_params.get("overlap_only", "0")
            if problems not in ("0", "1") or overlap not in ("0", "1"):
                raise ImportError("invalid", "Choose a valid import file filter.")
            return self.response(
                await self.available().status(
                    session.user_id,
                    request.path_params["ident"],
                    offset=_number(offset),
                    limit=max(1, _number(limit, maximum=100)),
                    problems_only=problems == "1",
                    overlap_only=overlap == "1",
                )
            )
        except ImportError as exc:
            return self.failure(exc)

    async def control(self, request: Request, session: Session) -> Response:
        try:
            service, ident = self.available(), request.path_params["ident"]
            action = request.path_params["action"]
            if action not in {
                "manifest",
                "seal",
                "review",
                "approve",
                "resume",
                "cancel",
                "distinct",
                "keep",
                "check_again",
            }:
                return self.response({"error": "Unknown import action."}, 404)
            body = await read_json(request)
            if action == "manifest":
                rows = body.get("entries")
                if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
                    raise ImportError("invalid", "An import page can contain at most 500 files.")
                entries = []
                for row in rows:
                    if not isinstance(row, dict) or set(row) != {"client_key", "path", "size"}:
                        raise ImportError("invalid", "Choose the files again.")
                    entries.append(
                        ManifestEntry(
                            client_key=_text(row["client_key"]),
                            path=_text(row["path"], limit=2048),
                            size=_number(row["size"], maximum=2**63 - 1),
                        )
                    )
                result = await service.declare(
                    session.user_id, ident, _text(body.get("page_key")), entries
                )
            elif action == "approve":
                result = await service.approve(
                    session.user_id,
                    ident,
                    _number(body.get("revision")),
                    _text(body.get("request_key")),
                )
            elif action == "review":
                result = await service.review(session.user_id, ident)
            elif action == "distinct":
                value = body.get("distinct", True)
                if type(value) is not bool:
                    raise ImportError("invalid", "Choose whether this file is a different song.")
                result = await service.distinct(
                    session.user_id, ident, _text(body.get("item")), value=value
                )
            elif action == "keep":
                value = body.get("keep", True)
                if type(value) is not bool:
                    raise ImportError("invalid", "Choose whether to add your files anyway.")
                result = await service.keep_files(
                    session.user_id, ident, _text(body.get("item")), value=value
                )
            else:
                result = await getattr(service, action)(session.user_id, ident)
            return self.response(result)
        except ImportError as exc:
            return self.failure(exc)

    async def receive(self, request: Request, session: Session) -> Response:
        """Never read a form or buffer the audio. Service admission precedes iteration."""
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0]
                != "application/octet-stream"
            ):
                raise ImportError("invalid", "Send the selected audio file from the import page.")
            try:
                attempt = int(request.query_params.get("attempt", ""))
            except ValueError:
                raise ImportError("invalid", "Reload the file's upload status.") from None
            return self.response(
                await self.available().receive(
                    session.user_id,
                    request.path_params["ident"],
                    request.path_params["item"],
                    _number(attempt),
                    request.stream(),
                )
            )
        except ImportError as exc:
            return self.failure(exc)

    def removals(self) -> ImportRemovals:
        service = getattr(self.dashboard.services, "music_import_removals", None)
        if not isinstance(service, ImportRemovals):
            raise ImportError(
                "unavailable", "Import removal is unavailable. Ask your administrator."
            )
        return service

    async def manage(self, request: Request, session: Session) -> Response:
        error, batches, job = "", [], None
        try:
            offset = int(request.query_params.get("offset", "0"))
            batches = await self.removals().listing(session.user_id, offset=offset)
            ident = request.query_params.get("job", "")
            if ident:
                await self.removals().status(session.user_id, ident)
                job = {"id": ident}
        except (ImportError, ValueError) as exc:
            error = str(exc) if isinstance(exc, ImportError) else "Choose a valid history page."
            offset = 0
        revision = sha256(
            b"".join(
                files("shijhon.dashboard").joinpath("static", name).read_bytes()
                for name in ("music_import_removals.js", "music_imports.css")
            )
        ).hexdigest()[:16]
        return await self.dashboard._render(
            "music_import_manage.html",
            session=session,
            current="collection",
            title="Manage music imports",
            error=error,
            batches=batches,
            job=job,
            offset=offset,
            asset_revision=revision,
        )

    async def removal_status(self, request: Request, session: Session) -> Response:
        try:
            try:
                offset = int(request.query_params.get("offset", "0"))
            except ValueError:
                raise ImportError("invalid", "Choose a valid removal page.") from None
            return self.response(
                await self.removals().status(
                    session.user_id, request.path_params["ident"], offset=_number(offset)
                )
            )
        except ImportError as exc:
            return self.failure(exc)

    async def removal_control(self, request: Request, session: Session) -> Response:
        try:
            service, owner, job = self.removals(), session.user_id, request.path_params["ident"]
            action = request.path_params["action"]
            body = await read_json(request)
            if action == "review":
                result = await service.review(
                    owner, job, include_used=body.get("include_used", False)
                )
            elif action == "approve":
                result = await service.approve(
                    owner, job, _text(body.get("fingerprint")), _text(body.get("request_key"))
                )
            elif action in ("restore", "retry", "stop"):
                result = await getattr(service, action)(owner, job)
            else:
                return self.response({"error": "Unknown removal action."}, 404)
            return self.response(result)
        except ImportError as exc:
            return self.failure(exc)
