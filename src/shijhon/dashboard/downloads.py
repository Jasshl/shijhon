"""Prepare the connected owner's native playlists, without history or curation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from shijhon.dashboard.auth import Session
from shijhon.downloads import Preparation
from shijhon.downloads.model import ConnectionRequired, PreparationError
from shijhon.playlists.model import PlaylistError

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    @property
    def service(self) -> Preparation:
        service = getattr(self.dashboard.services, "downloads", None)
        if service is None:
            raise PreparationError("Download services are starting.")
        return cast(Preparation, service)

    def routes(self) -> list[Route]:
        page = self.dashboard._personal_page
        return [
            Route("/downloads", page(self.index), methods=["GET"]),
            Route("/downloads/connection", page(self.connect), methods=["POST"]),
            Route("/downloads/disconnect", page(self.disconnect), methods=["POST"]),
            Route("/downloads/prepare", page(self.prepare), methods=["POST"]),
            Route("/downloads/{batch}/action", page(self.action), methods=["POST"]),
        ]

    async def render(self, session: Session, error: str = "") -> Response:
        connected, playlists, batches = False, [], []
        try:
            service = self.service
            batches = await service.repo.batches(session.user_id)
            playlists = await service.playlists_for(session.user_id, session.username)
            connected = True
        except ConnectionRequired:
            pass
        except (PreparationError, PlaylistError) as exc:
            if not error:
                error = (
                    str(exc)
                    if isinstance(exc, PreparationError)
                    else "Your authorization could not be verified."
                )
        return await self.dashboard._render(
            "downloads.html",
            session=session,
            current="downloads",
            error=error,
            connected=connected,
            playlists=playlists,
            batches=batches,
        )

    async def index(self, request: Request, session: Session) -> Response:
        return await self.render(session)

    async def connect(self, request: Request, session: Session) -> Response:
        try:
            password = request.state.form.get("password", "")
            if not password or len(password) > 1024:
                raise PreparationError("Enter your Navidrome password to authorize preparation.")
            peer = request.client.host if request.client else ""
            address = self.dashboard.forwarding.client(peer, request.headers.raw)
            await self.service.connect(
                session.user_id, session.username, password, str(address or "127.0.0.1")
            )
            return self.dashboard._see("/downloads")
        except (PreparationError, PlaylistError) as exc:
            return await self.render(
                session,
                str(exc)
                if isinstance(exc, PreparationError)
                else "Your authorization could not be verified.",
            )

    async def disconnect(self, request: Request, session: Session) -> Response:
        try:
            await self.service.disconnect(session.user_id)
            return self.dashboard._see("/downloads")
        except PreparationError as exc:
            return await self.render(session, str(exc))

    async def prepare(self, request: Request, session: Session) -> Response:
        try:
            await self.service.prepare(
                session.user_id, session.username, request.state.form.get("playlist", "")
            )
            return self.dashboard._see("/downloads")
        except (PreparationError, PlaylistError) as exc:
            return await self.render(
                session,
                str(exc)
                if isinstance(exc, PreparationError)
                else "The playlist could not be read.",
            )

    async def action(self, request: Request, session: Session) -> Response:
        try:
            await self.service.action(
                session.user_id,
                session.username,
                request.path_params["batch"],
                request.state.form.get("action", ""),
            )
            return self.dashboard._see("/downloads")
        except (PreparationError, PlaylistError) as exc:
            return await self.render(
                session,
                str(exc)
                if isinstance(exc, PreparationError)
                else "Your authorization could not be verified.",
            )
