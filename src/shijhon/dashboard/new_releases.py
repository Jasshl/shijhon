"""Each person's New releases page: follow your artists, and what is new from them."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from shijhon.catalog.model import CatalogRelease, ReleaseKind
from shijhon.dashboard.auth import Session
from shijhon.new_releases import PLAYED_DAYS, Followed, NewReleases, released
from shijhon.views.entries import album_name

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard

log = logging.getLogger(__name__)
PAGE = "new-releases"
KINDS = {
    ReleaseKind.ALBUM: "Album",
    ReleaseKind.SINGLE: "Single",
    ReleaseKind.EP: "EP",
    ReleaseKind.COMPILATION: "Compilation",
}
ARTISTS_SHOWN = 500  # followed artists listed on the page (their number is always said)


def day_words(day: date, today: date) -> str:
    """ "9 November", with the year when it is another one."""
    words = f"{day.day} {day:%B}"
    return words if day.year == today.year else f"{words} {day.year}"


def release_row(release: CatalogRelease, today: date) -> dict[str, Any]:
    day = released(release.release_date)
    return {
        "title": album_name(release),
        "artist": release.artist,
        "kind": KINDS[release.kind],
        "day": day_words(day, today) if day is not None else "",
        "date": day.isoformat() if day is not None else "",
        "tracks": release.track_count or len(release.tracks),
    }


def reason(artist: Followed) -> str:
    if artist.favorite and artist.played:
        return "favorite and most played"
    return "favorite" if artist.favorite else "most played"


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    @property
    def service(self) -> NewReleases | None:
        services = self.dashboard.services
        return services.new_releases if services is not None else None

    def routes(self) -> list[Route]:
        page = self.dashboard._personal_page
        return [
            Route(f"/{PAGE}", page(self.index), methods=["GET"]),
            Route(f"/{PAGE}/follow", page(self.follow), methods=["POST"]),
        ]

    async def index(self, request: Request, session: Session) -> Response:
        service = self.service
        status = await service.status(session.user_id) if service is not None else None
        releases: list[dict[str, Any]] = []
        artists: list[Followed] = []
        if service is not None and status is not None and status.enabled:
            await service.seen(session.user_id, session.username)
            today = service.today()
            listed = await service.listed(session.user_id, session.username)
            releases = [release_row(r, today) for r in listed]
            artists = await service.artists(session.user_id)
        return await self.dashboard._render(
            "new_releases.html",
            session=session,
            current=PAGE,
            started=service is not None,
            available=service is not None and service.available,
            status=status,
            releases=releases,
            artists=[(a.name, reason(a)) for a in artists[:ARTISTS_SHOWN]],
            artist_count=len(artists),
            days=service.days if service is not None else 0,
            most_played=service.most_played if service is not None else 0,
            played_days=PLAYED_DAYS,
        )

    async def follow(self, request: Request, session: Session) -> Response:
        service = self.service
        wanted = request.state.form.get("follow", "")
        if service is not None and wanted == "off":
            await service.turn_off(session.user_id)
            log.info("dashboard: an account turned new releases off")
        elif service is not None and wanted == "on" and service.available:
            await service.turn_on(session.user_id, session.username)
            log.info("dashboard: an account turned new releases on")
        return self.dashboard._see(f"/{PAGE}")
