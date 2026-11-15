"""The dashboard: Jinja2 pages and plain forms under ``/shijhon`` on Shijhon's own address.

``/shijhon`` cannot collide with Navidrome, whose paths are ``/app``, ``/api``, ``/rest``,
``/auth``, ``/share``, ``/jellyfin``, ``/backgrounds``, ``/debug`` and its metrics path
(everything else redirects to ``/app``), unless Navidrome's own ``BaseURL`` is set to
``/shijhon``. Server-rendered pages and forms: every change is a form post answered with a
redirect. Small scripts improve the restart waiting page and playlist forms.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import SplitResult, parse_qsl, quote, urlencode, urlsplit

import anyio
import httpx
from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from shijhon import __version__
from shijhon.access import AccessDenied, AccessService, AccessUnavailable, Principal
from shijhon.catalog import plugin
from shijhon.catalog.addon import AddonCatalog
from shijhon.catalog.base import CatalogError
from shijhon.cleanup import PreviewFailed, why_failed
from shijhon.config import LOOPBACK_HOSTS, Settings
from shijhon.dashboard import cleanup as cleaning
from shijhon.dashboard import library, live, sections, status
from shijhon.dashboard.addons import (
    KEPT,
    REACHES,
    Busy,
    Declared,
    ManifestChecks,
    fetch_manifest,
    hidden_mark,
    host_of,
    mark,
    plain_keys,
    representable,
    setting_text,
    shown,
    transfer_limit,
)
from shijhon.dashboard.auth import (
    Attempts,
    Session,
    Sessions,
    client_key,
    navidrome_login,
    same,
)
from shijhon.dashboard.collection import Pages as CollectionPages
from shijhon.dashboard.context import legal_context
from shijhon.dashboard.downloads import Pages as DownloadPages
from shijhon.dashboard.errors import RecentErrors
from shijhon.dashboard.fields import (
    Group,
    Invalid,
    counted,
    format_number,
    parse_number,
    spoken_unit,
)
from shijhon.dashboard.library_setup import Pages as LibrarySetupPages
from shijhon.dashboard.music_imports import Pages as MusicImportPages
from shijhon.dashboard.new_releases import Pages as NewReleasePages
from shijhon.dashboard.playlists import Pages as PlaylistPages
from shijhon.dashboard.saved import (
    ACCENTS,
    ADDONS,
    APPEARANCE,
    DEFAULT_ACCENT,
    REMOVE,
    SECTIONS,
    SHOWN,
    Locked,
    SavedSettings,
    catalog_problem,
    effective,
    out_of_place,
    withheld,
)
from shijhon.dashboard.times import day, hm, instant, when
from shijhon.delivery import pacing
from shijhon.delivery.addon import NEEDS_SIGN_IN, AddonError
from shijhon.delivery.bulk import effective_capacity
from shijhon.delivery.netpolicy import Reach, Resolver, system_resolver
from shijhon.delivery.pacing import AddonPace, Limits, origin
from shijhon.delivery.quality_gate import CHOICES as QUALITY_CHOICES
from shijhon.delivery.sources import MAX_LIMIT, USUAL_STARTS, NameTaken, StoredSource, usual_seconds
from shijhon.fill.fills import IN_LIBRARY, Changed, ChoiceRefused, FillPending, InUse
from shijhon.locks import KeyedLocks
from shijhon.navidrome import connect
from shijhon.navidrome.client import NavidromeError
from shijhon.navidrome.usage import NotFresh
from shijhon.placeholders.engine import MaterializeError
from shijhon.playlists.model import Connection
from shijhon.proxy.forwarding import Forwarding
from shijhon.store import Store, skipped
from shijhon.store.skipped import Skipped

if TYPE_CHECKING:
    import aiosqlite

    from shijhon.agents.app import Agents
    from shijhon.app import Services

log = logging.getLogger(__name__)

PATH = "/shijhon"
# A person's lowest audio quality (the Audio quality page), as the choices read.
# A number is for compressed audio alone: lossless audio is accepted at any bitrate, except
# with "Lossless only"; every choice but "Any" also asks for 44.1 kHz.
QUALITY_LABELS = {
    "any": "Any",
    "128": "128 kb/s",
    "160": "160 kb/s",
    "192": "192 kb/s",
    "256": "256 kb/s",
    "320": "320 kb/s",
    "lossless": "Lossless only",
}
# What a choice asks for, both kinds of audio apart.
QUALITY_WORDS = {
    "any": "Any quality.",
    "lossless": "Lossless audio only (FLAC, ALAC…), at 44.1 kHz or more.",
    **{
        kbps: f"Compressed audio (MP3, AAC, Opus…): at least {kbps} kb/s."
        " Lossless audio (FLAC, ALAC…) at any bitrate. Both at 44.1 kHz or more."
        for kbps in ("128", "160", "192", "256", "320")
    },
}
SESSION_COOKIE = "shijhon_session"
SIGNIN_COOKIE = "shijhon_signin"
MAX_FORM_BYTES = 64 * 1024
STATUS_SECONDS = 8.0  # an agent's server status waits this long for the checks it shares
# The sidebar: (heading, ((page, label), ...)). Everyone sees "Music"; administrators
# also see "Server", the installation's settings and health.
MUSIC_NAV = (
    "Music",
    (
        ("collection", "Collection"),
        ("playlists", "Playlists"),
        ("downloads", "Downloads"),
        ("new-releases", "New releases"),
    ),
)
NAV = (
    MUSIC_NAV,
    (
        "Server",
        (
            ("addons", "Add-ons"),
            ("playback", "Playback"),
            ("catalog", "Catalog"),
            ("library", "Library"),
            ("agent-access", "Agent access"),
            ("sound", "Sound analysis"),
            ("listenbrainz", "ListenBrainz"),
            ("cleanup", "Cleanup"),
            ("notifications", "Notifications"),
            ("diagnostics", "Diagnostics"),
        ),
    ),
)
PERSONAL_NAV = (MUSIC_NAV,)
# The pages every account may open after signing in (the others are administrators').
PERSONAL_PAGES = (*(page for page, _ in MUSIC_NAV[1]), "appearance", "quality")
ACCENT_NAMES = {
    "green": "Deep green",
    "teal": "Teal",
    "slate": "Slate blue",
    "graphite": "Graphite",
    "purple": "Purple",
}
PAGE_SECTIONS = {
    "playback": "delivery",
    "catalog": "catalog",
    "sound": "audiomuse",
    "listenbrainz": "listenbrainz",
    "agent-access": "agents",
    "notifications": "notifications",
}
SECTION_WORDS = {
    "playback": (
        "Playback",
        "How Shijhon finds audio for songs you don't own, and how long apps wait for it.",
    ),
    "catalog": (
        "Catalog",
        "The music Shijhon shows beyond your own, in searches, artist pages and complete albums.",
    ),
    "sound": (
        "Sound analysis",
        "AudioMuse-AI analyzes how your songs sound; described playlists use it as a hint.",
    ),
    "listenbrainz": (
        "ListenBrainz",
        "Songs and artists that ListenBrainz's listeners play together: radio from songs,"
        " albums and artists you don't own, discovery mixes and agents use them next to"
        " Last.fm's similar songs. Only artist and song names are sent, never an account or"
        " what anyone listened to.",
    ),
    "notifications": (
        "Notifications",
        "Shijhon tells you when something needs a person, through Apprise: on your phone, in a"
        " chat or by email, without opening the dashboard.",
    ),
    "agent-access": (
        "Agent access",
        "People connect the AI agents they already use over MCP, sign in with their"
        " Navidrome account, and ask for playlists in their own words.",
    ),
}
HEADERS = {
    "cache-control": "no-store",
    "content-security-policy": (
        "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self' data:;"
        " form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}
# The page that waits for a restart: its script file and what that asks, nothing more.
RESTARTING_POLICY = HEADERS["content-security-policy"] + "; script-src 'self'; connect-src 'self'"
# Times are <time> elements (``times``); a page with one has the script that writes them in
# the browser's zone, at the end of its body.
TIME_ELEMENT = "<time datetime="
TIME_SCRIPT = '<script src="{prefix}/static/times.js" defer></script>\n'
FAILING_IN_A_ROW = 3  # failures without a success before an add-on shows "Error"
# Saved audio: whether streams are kept, and its limits - on the Cleanup page, not with
# Playback's other settings.
DOWNLOAD_LIMITS = ("keep_streamed_audio", "delivered_days", "delivered_gb")
# Of [search], on the Catalog page (a form of its own): the wait for the catalog.
SEARCH_WAIT = ("budget_seconds",)
SEARCH_WAIT_FORM = "wait"
CONFIRM_SECONDS = 900.0  # how long the cleanup's confirmation stands (from its dry run)
ADDON_BUDGET = {"low": 0.0, "high": 600.0}  # SourceRegistry's limits for its own budget


def _page_of(section: str, key: str) -> str:
    """The name of the page a section's setting is on (for the restart banner)."""
    if section == "delivery":
        if key in PLAN_SETTINGS:
            return "Add-ons"
        return "Cleanup" if key in DOWNLOAD_LIMITS else "Playback"
    if section == "search":
        return "Catalog"
    pages = {
        "catalog": "Catalog",
        "fill": "Library",
        "cleanup": "Cleanup",
        "agents": "Agent access",
        "playlists": "Playlists",
        "notifications": "Notifications",
        "listenbrainz": "ListenBrainz",
    }
    return pages.get(section, "Sound analysis")


def _wants_json(request: Request) -> bool:
    """A page script's request (it saves without leaving the page), not a form's."""
    return request.headers.get("accept", "").startswith("application/json")


class _Static(StaticFiles):
    """The stylesheet and scripts, checked again on every page (a 304 when unchanged), so a
    browser never pairs a new page with a file it kept from before an upgrade."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


@dataclass(frozen=True)
class _OwnLimit:
    """A row of an add-on's settings form for one of its own limits."""

    name: str  # its field in ``Limits``, and on the form
    setting: str  # the installation's, in [delivery]
    label: str
    unit: str
    spoken: str
    whole: bool
    low: int
    help: str
    transfer: bool = False  # stored on the source, rather than its API pacing limits


ADDON_LIMITS = (
    _OwnLimit(
        "requests_per_second",
        "addon_requests_per_second",
        "Requests a second",
        "/s",
        "requests a second",
        False,
        0,
        "Requests sent to this add-on each second, for all users together. Raise it only for"
        " an add-on you run yourself. 0: no limit.",
    ),
    _OwnLimit(
        "request_burst",
        "addon_request_burst",
        "Requests at once",
        "requests",
        "requests",
        True,
        1,
        "How many requests may go at once after a quiet moment, before the rate above applies.",
    ),
    _OwnLimit(
        "audio_openings",
        "addon_audio_openings",
        "Songs opened at once",
        "songs",
        "songs",
        True,
        0,
        "Songs whose audio this add-on is asked for at the same time. The song being played"
        " never waits. 0: no limit.",
    ),
)
ADDON_TRANSFER_LIMITS = (
    _OwnLimit(
        "downloads",
        "addon_downloads",
        "Songs fetched at once",
        "songs",
        "songs",
        True,
        0,
        "Whole songs fetched from this add-on at the same time, for all users together."
        " Each counts until its transfer ends. Empty: Playback's setting applies. 0: no"
        " limit of its own, though a limit of the add-ons it shares with still applies.",
        transfer=True,
    ),
    _OwnLimit(
        "preparations",
        "addon_preparations",
        "New preparations at once",
        "songs",
        "songs",
        True,
        0,
        "Songs this add-on may be asked to prepare at the same time, when it hasn't"
        " confirmed their audio is ready. Empty: Playback's setting applies. 0: no limit of"
        " its own, though a limit of the add-ons it shares with still applies.",
        transfer=True,
    ),
)
# An add-on's "Advanced settings": its own time and limits, in place of the installation's.
ADDON_MORE = Group(
    "limits",
    "Time and limits",
    "An empty field uses Playback's setting. Each add-on has its own limits; add-ons with the"
    " same shared group label share their limits on fetched songs and new preparations, and"
    " the strictest applies.",
    more=True,
)
# The same for an add-on that is a catalog only: no audio, so its request limits alone.
ADDON_CATALOG_MORE = Group(
    "limits",
    "Time and request limits",
    "An empty field uses the Catalog page's request timeout or Playback's limits per add-on.",
    more=True,
)
# The limits that count for a catalog only: what is sent to it, not its audio.
CATALOG_LIMITS = ("requests_per_second", "request_burst")
MAX_ADDON_NAME = 100
# The plan's own delivery settings: saved with the plan on Add-ons, not on Playback.
PLAN_SETTINGS = ("ready_checks", "ready_handover_seconds")
NEEDS_SIGN_IN_WORDS = "Needs sign-in"  # an add-on that said its operator must sign in
RECENT_STARTS = USUAL_STARTS  # an add-on's latest attempts that the plan shows its speed from
# What a routing at byte zero was for, in Diagnostics' words.
PURPOSES = {
    "play": "Play",
    "ahead": "App's fetch ahead",
    "alone": "App's fetch ahead",
    "warm": "Next song opened",
    "download": "Download",
}
# Where an add-on runs (its settings, and the form that adds one).
REACH_HELP = (
    "Where the add-on runs. An address on this machine or your local network is refused"
    " unless chosen here. In a container, the Docker host is on the local network."
)


class Host(Protocol):
    """What the dashboard needs of the running Shijhon (``ShijhonApp``)."""

    configured: Settings
    locked: Locked  # the dashboard's settings the environment sets
    services: Services | None
    agents: Agents | None  # agent access, when an agent address is set
    forwarding: Forwarding  # who the client is
    restarting: bool  # a restart was asked for and is under way

    @property
    def can_restart(self) -> bool:
        """Whether a restart can be asked for: something starts Shijhon again here."""

    def restart_problem(self) -> str | None:
        """Why a restart now would not bring Shijhon back where it is (None: nothing)."""

    def begin_restart(self) -> bool:
        """Stop in order and start again (False: not possible here, or under way)."""

    def try_spawn(self, work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
        """Run work in the background for the app's lifetime (False: not started)."""


class Refused(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


def _library_search(text: str) -> str:
    """A search of the Library page's lists as it is kept in links and forms."""
    return " ".join(text[: library.SEARCH_LENGTH].split())


def _library_back(form: dict[str, str], pages: str, anchor: str) -> str:
    """Where a Library action returns to: the list it came from, at its page, under the
    search it was shown with."""
    search = _library_search(form.get("q", ""))
    found = {"q": search} if search else {}
    number = form.get("page", "1")
    found[pages] = number if number.isdigit() and len(number) < 7 else "1"
    return f"/library?{urlencode(found)}#{anchor}"


def printable(text: str) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:64]


@dataclass
class Flash:
    page: str
    words: str = ""  # an ok notice
    keys: tuple[str, ...] = ()  # rows saved just now
    status: str = ""  # the form's status line
    addon: int | None = None  # the add-on the flash is about
    part: str = ""  # the form on the page it is about (the Library's three)
    tone: str = "ok"  # the notice's: ok | info | error


class Dashboard:
    def __init__(
        self,
        host: Host,
        *,
        resolver: Resolver = system_resolver,
    ) -> None:
        self.host = host
        self.resolver = resolver
        self.env = Environment(
            loader=PackageLoader("shijhon.dashboard", "templates"),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters.update(
            hm=hm,
            day=day,
            when=when,
            number=format_number,
            count=lambda n: f"{n:,}",
            size=status.size_text,
            spoken_unit=spoken_unit,
        )
        self.attempts = Attempts()
        # The client's address, as the proxy names it for Navidrome.
        self.forwarding = host.forwarding
        # (Its manifest requests take their turn at each add-on's request limit.)
        self.checks = ManifestChecks(resolver=resolver, pace=self._pace)
        self.errors = RecentErrors()
        self.started_at = time.time()
        self.store: Store | None = None
        self.saved: SavedSettings | None = None
        self.sessions: Sessions | None = None
        self.running: Settings | None = None  # what the services use now
        self.problems: dict[str, str] = {}
        self.addons_handed_back = False
        # An upgrade that went on without its snapshot, read once at startup (only a start
        # writes it): shown to administrators until one dismisses it.
        self.skipped: Skipped | None = None
        self.catalog_check: status.CatalogCheck | None = None
        self.describer = library.Describer()
        self.jobs = library.Jobs()
        self._albums: tuple[float, set[str] | None] | None = None
        self._progress: tuple[tuple[int, int], float] | None = None  # (done, todo), since
        self._flash: dict[str, Flash] = {}
        self._http: httpx.AsyncClient | None = None
        self._storage: status.Storage | None = None
        self._addon_saves = KeyedLocks()
        # One save of a section at a time: what it writes follows from what is saved (the
        # catalog's kind and whose settings are saved with it).
        self._section_saves = KeyedLocks()
        self._cleanup_saves = anyio.Lock()
        # The cleanup's confirmations shown, by session: (nonce, the settings, when).
        self._confirmations: dict[str, tuple[str, tuple[Any, ...], float]] = {}
        self._verified: dict[str, tuple[float, Principal]] = {}
        self._verification = KeyedLocks()
        self._form_key = os.urandom(32)  # add-on forms' versions (``_addon_version``)
        # This run of Shijhon: another one after a restart (the page that waits for a
        # restart to end asks for it; it says nothing else).
        self.boot = os.urandom(8).hex()
        # Each add-on's settings saved through a visible field (``saved.SHOWN``), as last
        # read: at every page and before every save.
        self._marks: dict[int, dict[str, str]] = {}
        page = self._page
        routes = [
            Route("/", self.home),
            Route("/sign-in", self.sign_in_page, methods=["GET"]),
            Route("/sign-in", self.sign_in, methods=["POST"]),
            Route("/sign-out", self.sign_out, methods=["POST"]),
            Route("/addons", page(self.addons_page), methods=["GET"]),
            Route("/addons", page(self.addon_add), methods=["POST"]),
            Route("/addons/configuration", page(self.addons_hand_back), methods=["POST"]),
            Route("/addons/plan", page(self.addons_plan), methods=["POST"]),
            Route("/addons/enabled", page(self.addons_enabled), methods=["POST"]),
            Route("/addons/{addon:int}/enabled", page(self.addon_enabled), methods=["POST"]),
            Route(
                "/addons/{addon:int}/check-again", page(self.addon_check_again), methods=["POST"]
            ),
            Route("/addons/{addon:int}/settings", page(self.addon_settings), methods=["POST"]),
            Route("/addons/{addon:int}/remove", page(self.addon_remove), methods=["POST"]),
            Route("/playback", page(self.section_page), methods=["GET", "POST"]),
            Route("/catalog", page(self.section_page), methods=["GET", "POST"]),
            Route("/catalog/check", page(self.catalog_check_now), methods=["POST"]),
            Route("/catalog/wait", page(self.catalog_wait_save), methods=["POST"]),
            Route("/sound", page(self.section_page), methods=["GET", "POST"]),
            Route("/listenbrainz", page(self.section_page), methods=["GET", "POST"]),
            Route("/agent-access", page(self.section_page), methods=["GET", "POST"]),
            Route("/notifications", page(self.section_page), methods=["GET", "POST"]),
            Route("/notifications/test", page(self.notification_test), methods=["POST"]),
            Route("/library", page(self.library_page), methods=["GET"]),
            Route("/library/policy", page(self.library_save), methods=["POST"]),
            Route("/library/pass", page(self.library_save), methods=["POST"]),
            Route("/library/settings", page(self.library_save), methods=["POST"]),
            Route("/library/review/{album}", page(self.library_review), methods=["POST"]),
            Route("/library/no-match/{album}", page(self.library_recheck), methods=["POST"]),
            Route("/library/change/{album}", page(self.library_change), methods=["POST"]),
            Route("/library/bulk", page(self.library_bulk), methods=["POST"]),
            Route("/library/bulk/stop", page(self.library_bulk_stop), methods=["POST"]),
            Route("/cleanup", page(self.cleanup_page), methods=["GET"]),
            Route("/cleanup/settings", page(self.cleanup_save), methods=["POST"]),
            Route("/cleanup/downloads", page(self.cleanup_save), methods=["POST"]),
            Route("/cleanup/check", page(self.cleanup_check), methods=["POST"]),
            Route("/diagnostics", page(self.diagnostics_page), methods=["GET"]),
            Route("/restart", page(self.restart_now), methods=["POST"]),
            Route("/upgrade-alert", page(self.upgrade_alert_dismiss), methods=["POST"]),
            Route("/restarting", page(self.restarting_page), methods=["GET"]),
            Route("/alive", self.alive, methods=["GET"]),
            Route(
                "/appearance", self._personal_page(self.appearance_page), methods=["GET", "POST"]
            ),
            Route("/quality", self._personal_page(self.quality_page), methods=["GET", "POST"]),
            Mount("/static", _Static(packages=[("shijhon.dashboard", "static")])),
        ]
        routes.extend(DownloadPages(self).routes())
        routes.extend(NewReleasePages(self).routes())
        routes.extend(PlaylistPages(self).routes())
        routes.extend(LibrarySetupPages(self).routes())
        routes.extend(MusicImportPages(self).routes())
        routes.extend(CollectionPages(self).routes())
        self.app = Starlette(routes=[Mount(PATH, routes=routes)])

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)

    # --- lifecycle ----------------------------------------------------------------------

    def listen(self) -> None:
        logging.getLogger("shijhon").addHandler(self.errors)

    def attach(self, store: Store, running: Settings, problems: dict[str, str]) -> None:
        """Called at the end of Shijhon's startup with what its services were built from."""
        self.store = store
        self.saved = SavedSettings(store)
        self.sessions = Sessions(store)
        self.running = running
        self.problems = dict(problems)
        self._http = connect.async_client(self.host.configured.navidrome.socket, timeout=10.0)

    async def aclose(self) -> None:
        logging.getLogger("shijhon").removeHandler(self.errors)
        if self._http is not None:
            await self._http.aclose()

    @property
    def services(self) -> Services | None:
        return self.host.services

    # --- responses ----------------------------------------------------------------------

    def _see(self, where: str) -> Response:
        return RedirectResponse(f"{PATH}{where}", status_code=303, headers=HEADERS)

    async def _render(
        self,
        template: str,
        *,
        session: Session | None,
        current: str = "",
        status_code: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        accent = (
            await self.saved.accent(session.user_id if session else "")
            if self.saved is not None
            else DEFAULT_ACCENT
        )
        html = self.env.get_template(template).render(
            prefix=PATH,
            accent=accent,
            user=session.username if session else "",
            csrf=session.csrf if session else "",
            current=current,
            nav=NAV if session and session.is_admin else PERSONAL_NAV,
            is_admin=bool(session and session.is_admin),
            home="addons" if session and session.is_admin else "collection",
            **legal_context(),
            pending=await self._pending() if session and session.is_admin else [],
            # "Restart Shijhon" is offered where something starts Shijhon again.
            can_restart=bool(session and session.is_admin) and self.host.can_restart,
            upgraded=self.skipped if session and session.is_admin else None,
            attention=await self._attention() if session and session.is_admin else {},
            **context,
        )
        headers = dict(HEADERS)
        # A page that shows a time has the script that writes it in the browser's zone.
        timed = TIME_ELEMENT in html
        if timed:
            html = html.replace("</body>", TIME_SCRIPT.format(prefix=PATH) + "</body>", 1)
        if template in {
            "addons.html",
            "collection.html",
            "library.html",
            "_library_lists.html",
            "music_imports.html",
            "music_import_manage.html",
        }:
            headers["content-security-policy"] += "; script-src 'self'; connect-src 'self'"
        elif timed or template == "playlists.html":
            headers["content-security-policy"] += "; script-src 'self'"
        return HTMLResponse(html, status_code, headers=headers)

    async def _attention(self) -> dict[str, tuple[int, str]]:
        """The sidebar's counts, by page, with their words: albums in the Library's review
        list and add-ons whose operator must sign in (as the Add-ons page shows them). Read
        from Shijhon's own database and memory, nothing asked elsewhere; a count that can't
        be read is left out, and none is shown for nothing."""
        services, store = self.services, self.store
        found: dict[str, tuple[int, str]] = {}
        if services is None or store is None:
            return found

        async def review() -> int:
            fills = services.fills
            return 0 if fills is None else await library.review_count(store, fills.scope)

        async def signing_in() -> int:
            return sum(self._signing_in(addon) for addon in await services.sources.stored())

        async def number(page: str, count: Callable[[], Awaitable[int]]) -> int:
            try:
                return await count()
            except Exception:  # (the page shows without it)
                log.debug("the sidebar's count for %s could not be read", page, exc_info=True)
                return 0

        if albums := await number("library", review):
            found["library"] = (albums, f"{albums:,} album{'s' if albums != 1 else ''} to review")
        if addons := await number("addons", signing_in):
            needs = "add-ons need" if addons != 1 else "add-on needs"
            found["addons"] = (addons, f"{addons:,} {needs} sign-in")
        return found

    async def _form(self, request: Request) -> dict[str, str]:
        """A submitted form (URL-encoded; an empty body is an empty form). A field sent twice
        keeps its last value (a checkbox after its hidden "false")."""
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                raise Refused(PlainTextResponse("The form is too large.\n", 413, headers=HEADERS))
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if body and kind != "application/x-www-form-urlencoded":
            raise Refused(PlainTextResponse("Send the form from the page.\n", 415, headers=HEADERS))
        try:
            pairs = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=500)
        except (UnicodeDecodeError, ValueError):
            raise Refused(PlainTextResponse("Unreadable form.\n", 400, headers=HEADERS)) from None
        return dict(pairs)

    # --- sign-in ------------------------------------------------------------------------

    async def _session(self, request: Request, *, cached: bool = False) -> Session:
        """Verify the owner; only collection sub-resources reuse a proof for three seconds."""
        session = await self._local_session(request)
        async with self._verification.hold(session.token_hash):
            known = self._verified.get(session.token_hash) if cached else None
            if known and known[0] > time.monotonic():
                return replace(session, username=known[1].username, principal=known[1])
            self._verified.pop(session.token_hash, None)
            return await self._verify_session(session, cached=cached)

    async def _local_session(self, request: Request) -> Session:
        """The request's signed-in, unexpired session as this installation knows it, before
        any check with Navidrome."""
        if self.sessions is None:
            raise Refused(PlainTextResponse("Shijhon is starting.\n", 503, headers=HEADERS))
        session = await self.sessions.get(request.cookies.get(SESSION_COOKIE))
        if session is None:
            where = request.url.path if request.method == "GET" else f"{PATH}/collection"
            raise Refused(self._see(f"/sign-in?next={quote(where, safe='/')}"))
        return session

    async def _verify_session(self, session: Session, *, cached: bool) -> Session:
        assert self.sessions is not None
        services = self.services
        try:
            if not services or not services.navidrome:
                raise AccessUnavailable
            principal = await AccessService(services.navidrome).personal(
                session.username, expected_user_id=session.user_id
            )
        except AccessDenied:
            self._verified.clear()
            await self.sessions.end_owner(session.user_id)
            log.info("dashboard: a session ended because identity or library access changed")
            raise Refused(self._see("/sign-in?reason=access")) from None
        except AccessUnavailable:
            self._verified.pop(session.token_hash, None)
            raise Refused(
                await self._render(
                    "unverified.html",
                    session=None,
                    status_code=503,
                    title="Account access can't be checked",
                )
            ) from None
        if cached:
            if len(self._verified) >= 1000:
                self._verified.pop(next(iter(self._verified)))
            self._verified[session.token_hash] = (time.monotonic() + 3, principal)
        return replace(session, username=principal.username, principal=principal)

    def _page(
        self,
        handler: Callable[[Request, Session], Awaitable[Response]],
        *,
        form_reader: Callable[[Request], Awaitable[dict[str, str]]] | None = None,
    ) -> Callable[[Request], Awaitable[Response]]:
        """Installation pages require a currently verified administrator."""
        return self._personal_page(handler, form_reader=form_reader, require_admin=True)

    def _personal_page(
        self,
        handler: Callable[[Request, Session], Awaitable[Response]],
        *,
        form_reader: Callable[[Request], Awaitable[dict[str, str]]] | None = None,
        require_admin: bool = False,
        cache_identity: bool = False,
        verify: bool = True,
    ) -> Callable[[Request], Awaitable[Response]]:
        """Own-account pages; identity/library verification precedes form reads/work.

        Collection adapters can wrap their routes with this callback. Session.user_id
        is immutable; session.principal contains this request's current native grants.
        ``verify`` False: the signed-in session and its form token are the authority, with
        no check with Navidrome (and no current grants), for a form that can only take the
        account's own access away, so that it works while Navidrome is down.
        """
        if not verify and require_admin:
            raise ValueError("an administrator's page is always verified")

        async def endpoint(request: Request) -> Response:
            try:
                session = (
                    await self._local_session(request)
                    if not verify
                    else await self._session(request, cached=True)
                    if cache_identity and request.method in ("GET", "HEAD")
                    else await self._session(request)
                )
                if not verify and not session.user_id:  # (verification fails closed too)
                    raise Refused(self._see("/sign-in?reason=access"))
                if require_admin and not session.is_admin:
                    raise Refused(
                        PlainTextResponse(
                            "This page requires a Navidrome administrator.\n", 403, headers=HEADERS
                        )
                    )
                if request.method == "POST":
                    request.state.form = await self._checked_form(request, session, form_reader)
                elif request.method == "HEAD":
                    # Starlette adds HEAD to GET routes. Keep every page handler on its
                    # read path, including a newly added handler that tests for GET.
                    request = Request({**request.scope, "method": "GET"}, request.receive)
                return await handler(request, session)
            except Refused as refused:
                return refused.response

        return endpoint

    async def _checked_form(
        self,
        request: Request,
        session: Session,
        reader: Callable[[Request], Awaitable[dict[str, str]]] | None = None,
    ) -> dict[str, str]:
        if request.headers.get("sec-fetch-site", "same-origin") == "cross-site":
            raise Refused(PlainTextResponse("Cross-site form refused.\n", 403, headers=HEADERS))
        form = await (reader or self._form)(request)
        if not same(form.get("csrf", ""), session.csrf):
            log.warning("dashboard: a form without its session's token was refused")
            raise Refused(
                PlainTextResponse(
                    "The form has expired. Go back, reload the page and try again.\n",
                    403,
                    headers=HEADERS,
                )
            )
        return form

    def _personal_api(
        self,
        handler: Callable[[Request, Session], Awaitable[Response]],
        *,
        require_admin: bool = False,
    ) -> Callable[[Request], Awaitable[Response]]:
        """Authorize private JSON/binary requests before their bodies are consumed.

        The handler owns bounded JSON parsing or binary streaming. CSRF travels in a
        header so an upload never needs parsing or staging before authorization.
        """

        def refused(code: str, message: str, status: int) -> JSONResponse:
            return JSONResponse({"error": code, "message": message}, status, headers=HEADERS)

        async def endpoint(request: Request) -> Response:
            try:
                session = await self._session(request)
                if require_admin and not session.is_admin:
                    return refused("admin_required", "This action requires an administrator.", 403)
                if request.headers.get("sec-fetch-site") == "cross-site":
                    return refused("origin_refused", "Send this request from Shijhon.", 403)
                origins = request.headers.getlist("origin")
                if origins:
                    try:
                        supplied = urlsplit(origins[0])
                        expected = urlsplit(
                            ("https" if _secure(request) else request.url.scheme)
                            + "://"
                            + request.url.netloc
                        )

                        def authority(url: SplitResult) -> tuple[str, str | None, int | None]:
                            return (
                                url.scheme,
                                url.hostname,
                                url.port or (443 if url.scheme == "https" else 80),
                            )

                        valid = (
                            len(origins) == 1
                            and supplied.scheme in ("http", "https")
                            and supplied.username is None
                            and supplied.password is None
                            and not supplied.path
                            and not supplied.query
                            and not supplied.fragment
                            and authority(supplied) == authority(expected)
                        )
                    except ValueError:
                        valid = False
                    if not valid:
                        return refused("origin_refused", "Send this request from Shijhon.", 403)
                if request.method not in ("GET", "HEAD"):
                    tokens = request.headers.getlist("x-csrf-token")
                    if len(tokens) != 1 or not same(tokens[0], session.csrf):
                        return refused("csrf_refused", "Reload the import page and try again.", 403)
                return await handler(request, session)
            except Refused as failure:
                status = failure.response.status_code
                if status >= 500:
                    return refused(
                        "access_unavailable",
                        "Account access could not be verified. Try again.",
                        503,
                    )
                if status in (301, 302, 303, 307, 308):
                    if "reason=access" in failure.response.headers.get("location", ""):
                        return refused(
                            "access_changed", "Your account or music-library access changed.", 403
                        )
                    return refused("sign_in_required", "Sign in to continue this import.", 401)
                return refused("request_refused", "This request was refused.", status)

        return endpoint

    async def home(self, request: Request) -> Response:
        try:
            session = await self._session(request)
            return self._see("/addons" if session.is_admin else "/collection")
        except Refused as refused:
            return refused.response

    async def sign_in_page(self, request: Request, **context: Any) -> Response:
        if (
            not context.get("error")
            and request.query_params.get("reauth") != "1"
            and self.sessions is not None
            and await self.sessions.get(request.cookies.get(SESSION_COOKIE))
        ):
            try:
                session = await self._session(request)
                return self._see("/addons" if session.is_admin else "/collection")
            except Refused:
                pass
        token = request.cookies.get(SIGNIN_COOKIE) or os.urandom(24).hex()
        reason = request.query_params.get("reason")
        context.setdefault("error", None)
        context.setdefault("error_title", None)
        if reason == "access" and context["error"] is None:
            context["error"] = "Signed out: your account or library access changed in Navidrome."
        context.setdefault(
            "info",
            "Sign in again to refresh your music. Your library choices are kept."
            if request.query_params.get("reauth") == "1"
            else None,
        )
        response = await self._render(
            "sign_in.html",
            session=None,
            signin_token=token,
            next=context.pop("next", request.query_params.get("next", "")),
            username=context.pop("username", ""),
            status_code=context.pop("status_code", 200),
            **context,
        )
        response.set_cookie(
            SIGNIN_COOKIE,
            token,
            path=PATH,
            httponly=True,
            samesite="strict",
            secure=_secure(request),
        )
        return response

    async def sign_in(self, request: Request) -> Response:
        try:
            form = await self._form(request)
        except Refused as refused:
            return refused.response
        username = form.get("username", "").strip()
        password = form.get("password", "")

        async def again(error: str, title: str | None = None, code: int = 400) -> Response:
            return await self.sign_in_page(
                request,
                error=error,
                error_title=title,
                username=username,
                status_code=code,
                next=form.get("next", ""),
            )

        if not same(form.get("signin", ""), request.cookies.get(SIGNIN_COOKIE, "")):
            return await again(
                "The sign-in form has expired, or this browser keeps no cookies for this site."
                " Try again; cookies must be allowed."
            )
        if self.sessions is None or self._http is None:
            return await again("Shijhon is starting. Try again in a moment.", code=503)
        if not username or not password:
            return await again("Enter a username and a password.")
        peer = request.client.host if request.client else ""
        address = self.forwarding.client(peer, request.headers.raw)
        if not self.attempts.allow(client_key(address, peer)):
            return await again("Too many sign-in attempts. Wait a minute and try again.", code=429)
        login = await navidrome_login(
            self._http, self.host.configured.navidrome.url, username, password, address
        )
        if login.outcome == "unreachable":
            return await again(
                "Sign-in is checked with Navidrome, so it has to be running and reachable. Try"
                " again once it answers.",
                "Navidrome is not answering",
                503,
            )
        if login.outcome == "limited":
            return await again(
                "Navidrome refused more sign-in attempts for now. Wait a minute.", code=429
            )
        if login.outcome == "wrong":
            # Without the username: people type passwords there too.
            log.info("dashboard: a sign-in failed (wrong username or password)")
            return await again(
                "Wrong username or password. Check them in Navidrome if you are unsure."
            )
        services = self.services
        try:
            if not services or not services.navidrome:
                raise AccessUnavailable
            principal = await AccessService(services.navidrome).personal(
                login.username, expected_user_id=login.user_id
            )
        except AccessDenied:
            return await again("This account cannot access Shijhon's library.", code=403)
        except AccessUnavailable:
            return await again("Account access could not be verified. Try again later.", code=503)
        if services.playlists and login.token.get_secret_value() and login.salt.get_secret_value():
            # A normal sign-in also authorizes caller-owned library reads, without
            # another password form or any history/AI configuration. Keep prior settings.
            async with services.playlists.changes:
                old = await services.playlists.repo.connection(login.user_id)
                values: dict[str, Any] = dict(
                    user_id=login.user_id,
                    username=principal.username,
                    token=login.token,
                    salt=login.salt,
                )
                connected = (
                    old.model_copy(update=values)
                    if old
                    else Connection(**values, history_source="navidrome", use_shared_ai=True)
                )
                await services.playlists.repo.connect(connected)
        token, _ = await self.sessions.create(login.username, login.user_id)
        previous = request.cookies.get(SESSION_COOKIE)
        if previous:
            await self.sessions.end(previous)
        if services.collection_refresh and login.native_token.get_secret_value():
            await services.collection_refresh.signed_in(
                principal, login.native_token.get_secret_value()
            )
        log.info("dashboard: %s signed in", printable(login.username))
        target = form.get("next", "")
        if not target.startswith(PATH + "/") or target.startswith(PATH + "//") or "\\" in target:
            target = f"{PATH}/{'addons' if principal.is_admin else 'collection'}"
        if not principal.is_admin and not any(
            target.split("?", 1)[0] == f"{PATH}/{p}" or target.startswith(f"{PATH}/{p}/")
            for p in PERSONAL_PAGES
        ):
            target = f"{PATH}/collection"
        response = RedirectResponse(target, status_code=303, headers=HEADERS)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=int(self.sessions.lifetime),
            path=PATH,
            httponly=True,
            samesite="lax",
            secure=_secure(request),
        )
        response.delete_cookie(SIGNIN_COOKIE, path=PATH)
        return response

    async def sign_out(self, request: Request) -> Response:
        try:
            if self.sessions is None:
                raise Refused(PlainTextResponse("Shijhon is starting.\n", 503, headers=HEADERS))
            token = request.cookies.get(SESSION_COOKIE)
            session = await self.sessions.get(token)
            if session is not None:
                await self._checked_form(request, session)
                assert token is not None
                await self.sessions.end(token)
                self._verified.pop(session.token_hash, None)
                log.info("dashboard: %s signed out", printable(session.username))
        except Refused as refused:
            return refused.response
        response = self._see("/sign-in")
        response.delete_cookie(SESSION_COOKIE, path=PATH)
        return response

    # --- shared -------------------------------------------------------------------------

    def _flash_for(self, session: Session, page: str) -> Flash | None:
        flash = self._flash.get(session.token_hash)
        if flash is not None and flash.page == page:
            del self._flash[session.token_hash]
            return flash
        return None

    def _remember(self, session: Session, flash: Flash) -> None:
        self._flash[session.token_hash] = flash
        if len(self._flash) > 1000:
            self._flash.pop(next(iter(self._flash)))

    async def _pending(self) -> list[dict[str, Any]]:
        """Saved settings that wait for a restart, with when they were saved: those that
        apply after a restart, and any live one that could not be applied."""
        if self.saved is None or self.running is None:
            return []
        saved = await self.saved.all()
        applying, _ = effective(self.host.configured, saved, locked=self.host.locked)
        waiting = []
        for section in SECTIONS:
            if section in self.problems:
                continue  # its saved values were refused at startup (its page says why)
            now, running = getattr(applying, section), getattr(self.running, section)
            for f in sections.fields_of(section, sections.kind_of(section, now)):
                # (A setting the running catalog's adapter does not have: at its default.)
                if getattr(now, f.key, f.builtin) == getattr(running, f.key, f.builtin):
                    continue
                item = saved.get(section, {}).get(f.key)
                waiting.append(
                    {
                        "label": f.meta.label,
                        "page": _page_of(section, f.key),
                        "at": item.saved_at if item else None,
                    }
                )
        if self.addons_handed_back:
            waiting.append(
                {"label": "Add-on list from the configuration file", "page": "Add-ons", "at": None}
            )
        return waiting

    async def _addon_names(self, section: str = "") -> list[str]:
        """The add-ons a setting that names one may choose."""
        return [addon.name for addon in await self._stored()]

    async def _catalog_only_names(self) -> list[str]:
        addons = await self._stored()
        audio = self._audio_ids(addons)
        return [addon.name for addon in addons if addon.id not in audio]

    async def _running_section(
        self, section: str, keys: list[str], values: Any, interim: list[str] | None = None
    ) -> list[str]:
        """Apply the live ones of ``keys`` to the services; returns those applied (keys given
        a value meanwhile are added to ``interim``)."""
        services = self.services
        if services is None or self.running is None or not keys:
            return []
        try:
            applied = await live.apply(services, section, keys, values, interim)
        except Exception:
            log.exception(
                "dashboard: applying %s settings failed; they apply after a restart", section
            )
            return []
        part = getattr(self.running, section).model_copy(
            update={key: getattr(values, key) for key in applied}
        )
        self.running = self.running.model_copy(update={section: part})
        return applied

    # --- settings sections (Playback, Catalog) ------------------------------------------

    async def section_page(self, request: Request, session: Session) -> Response:
        page = request.url.path.removeprefix(PATH + "/")
        section = PAGE_SECTIONS[page]
        assert self.saved is not None
        if request.method == "POST":
            return await self._section_save(request, session, page, section)
        flash = self._flash_for(session, page)
        return await self._section_render(session, page, section, flash=flash)

    async def _section_render(
        self,
        session: Session,
        page: str,
        section: str,
        *,
        flash: Flash | None = None,
        form: dict[str, str] | None = None,
        problems: dict[str, sections.Problem] | None = None,
        status_code: int = 200,
        wait: tuple[dict[str, str], dict[str, sections.Problem]] | None = None,
        first_step: bool = False,
    ) -> Response:
        """``wait``: the Catalog page's second form (the wait for the catalog) as it
        was sent, with its problems. ``first_step``: the form chose another catalog,
        whose settings this page now has to be entered - the page a refused save shows,
        with a notice in place of the refusal and its marks."""
        assert self.saved is not None
        saved = await self.saved.all()
        notes: dict[str, str] = {}
        if page == "playback":  # (which add-ons are a catalog only: their manifests say)
            await self.checks.check(await self._stored())
            notes = {name: "catalog only" for name in await self._catalog_only_names()}
        names = await self._addon_names(section)
        extra: dict[str, Any] = {}
        shown = dict(problems or {})
        # (The second form's flash is its own: not the main form's status.)
        waited = flash if flash is not None and flash.part == SEARCH_WAIT_FORM else None
        flash = None if waited is not None else flash
        if page == "catalog":
            extra = await self._catalog_status(saved)
            applying_catalog = effective(self.host.configured, saved, locked=self.host.locked)[
                0
            ].catalog
            shown_catalog = sections.form_kind(
                section, applying_catalog, form, self.host.locked.get(section, {})
            )
            if shown_catalog == "addon":
                identity_info, form = await self._catalog_identity_form(applying_catalog, form)
                extra.update(identity_info)
            missing = extra["problem"]
            if missing is not None and form is None:  # the row too, as the page notice says
                shown[missing["key"]] = sections.Problem(missing["row"], "")
            # How long searches and artist pages wait for the catalog ([search]).
            rows = sections.rows(
                "search",
                self.host.configured,
                saved,
                addon_names=[],
                form=wait[0] if wait else None,
                problems=wait[1] if wait else None,
                just_saved=list(waited.keys) if waited else None,
                date=day,
                locked=self.host.locked,
            )
            extra["wait"] = [r for _, group in rows for r in group if r["name"] in SEARCH_WAIT]
            extra["wait_group"] = Group("wait", "Searches and artist pages", more=True)
            extra["wait_status"] = (
                waited.status if waited else "Not saved" if wait and wait[1] else ""
            )
            extra["wait_saved"] = bool(waited and waited.status)
            # (A saved value that no longer fits is not in use: said, like the page's own.)
            extra["wait_problem"] = self.problems.get("search")
        if page == "sound":
            extra = await self._sound_status(saved)
        if page == "agent-access":
            extra = self._agent_status(saved)
        if page == "notifications":
            extra = self._notification_status()
        groups = sections.rows(
            section,
            self.host.configured,
            saved,
            addon_names=names,
            addon_notes=notes,
            form=form,
            problems=shown,
            just_saved=list(flash.keys) if flash else None,
            date=day,
            locked=self.host.locked,
            unmarked=first_step,
        )
        if page == "catalog" and "identity_addon" in extra:
            for _, identity_rows in groups:
                for identity_row in identity_rows:
                    if identity_row["name"] != "identity":
                        continue
                    identity_row["help"] = extra["identity_notice"]
                    if identity_row["kind"] != "locked":
                        identity_row.update(
                            kind="confirmed_identity",
                            claim=extra["identity_confirm"],
                            checked=bool(
                                form
                                and form.get("identity-addon") == extra["identity_addon"]
                                and form.get("confirm-identity") == extra["identity_confirm"]
                            ),
                        )
        if page == "playback":  # downloaded audio's limits are on Cleanup, the plan's on Add-ons
            elsewhere = (*DOWNLOAD_LIMITS, *PLAN_SETTINGS)
            groups = [
                (group, [row for row in members if row["name"] not in elsewhere])
                for group, members in groups
            ]
        # The rows the notices above the form link to are shown, under "More settings" too;
        # so is the Catalog page's wait (its own form, in the fold) after a failed save.
        linked = {extra["problem"]["id"]} if extra.get("problem") else set()
        linked |= {item["id"] for key in ("withheld", "displaced") for item in extra.get(key, ())}
        groups, fold = sections.folded(
            groups, problems=shown, linked=frozenset(linked), also=extra.get("wait")
        )
        if extra.get("wait_problem"):
            fold = dataclasses.replace(fold, open=True)
        applying = getattr(
            effective(self.host.configured, saved, locked=self.host.locked)[0], section
        )
        kind = sections.form_kind(section, applying, form, self.host.locked.get(section, {}))
        labels = {f.key: (f.id, f.meta.label) for f in sections.fields_of(section, kind)}
        context: dict[str, Any] = {
            "title": SECTION_WORDS[page][0],
            "summary": SECTION_WORDS[page][1],
            "page": page,
            "groups": groups,
            "fold": fold,
            "notices": [
                (labels[k][0], labels[k][1], p.notice)
                for k, p in (problems or {}).items()
                if not first_step
            ],
            "form_status": flash.status
            if flash
            else ("Not saved" if problems and not first_step else ""),
            "saved_ok": bool(flash and flash.status),
            "words": flash.words if flash else "",
            "tone": flash.tone if flash else "info",
            "problem": self.problems.get(section),
            "extra": extra,
            # The catalog whose adapter settings the form holds (sections.SHOWN_KIND).
            "shown_kind": (sections.SHOWN_KIND, kind) if section == "catalog" else None,
            "first_step": first_step,
        }
        return await self._render(
            "section.html", session=session, current=page, status_code=status_code, **context
        )

    async def _section_save(
        self, request: Request, session: Session, page: str, section: str
    ) -> Response:
        form: dict[str, str] = request.state.form
        if page == "catalog":
            assert self.saved is not None
            saved = await self.saved.all()
            applying = effective(self.host.configured, saved, locked=self.host.locked)[0].catalog
            env = self.host.locked.get("catalog", {})
            if sections.form_kind(section, applying, form, env) == "addon":
                chosen = (
                    str(getattr(applying, "addon", ""))
                    if "addon" in env
                    else form.get("addon", str(getattr(applying, "addon", "")))
                )
                declared = await self._addon_identity(chosen)
                if (declared or form.get("identity")) and form.get("identity-addon") != chosen:
                    return await self._section_render(
                        session, page, section, form=form, first_step=True
                    )
                if "identity" not in env:
                    # A posted text value cannot confirm a claim. Only the control
                    # bound to the declaration reviewed for this add-on can do that.
                    form["identity"] = str(getattr(applying, "identity", ""))
                    if (
                        declared is not None
                        and form.get("identity-addon") == chosen
                        and form.get("confirm-identity") == declared
                    ):
                        form["identity"] = declared
        submitted, written, _ = await self._save(session, section, form)
        if written is None and submitted.first_step:
            # Another catalog was chosen, and its settings are needed: the page for it,
            # as a refused save shows it, without the refusal - nothing was wrong with the
            # form, and nothing is saved before those settings are entered.
            return await self._section_render(
                session, page, section, form=form, problems=submitted.problems, first_step=True
            )
        if written is None:
            return await self._section_render(
                session, page, section, form=form, problems=submitted.problems, status_code=400
            )
        words = ""
        if written.displaced:  # (the catalog's: a saved value its new address does not get)
            kind = sections.kind_of(section, submitted.candidate)
            labels = {f.key: f.meta.label for f in sections.fields_of(section, kind)}
            names = ", ".join(f"“{labels.get(key, key)}”" for key, _ in written.displaced)
            places = sorted({labels.get(address, address) for _, address in written.displaced})
            words = (
                f"Removed, as saved for another address: {names}. What is saved for one"
                f" {' or '.join(places).lower()} is not sent to another: enter it again if"
                " the address in use now needs it."
            )
        self._remember(
            session,
            Flash(
                page, words=words, keys=tuple(written.changed), status=_saved(written), tone="info"
            ),
        )
        return self._see(f"/{page}")

    async def _save(
        self,
        session: Session,
        section: str,
        form: dict[str, str],
        interim: list[str] | None = None,
        *,
        whole: bool = True,
        held: bool = False,
    ) -> tuple[sections.Submitted, sections.Written | None, list[str]]:
        """Check and save a form's settings of one section, and apply the live ones: (the
        submission, what was written or None when it has problems, the keys applied).
        ``held``: the caller holds the section's saves (``_section_saves``)."""
        assert self.saved is not None
        async with contextlib.nullcontext() if held else self._section_saves.hold(section):
            saved = await self.saved.all()
            names = await self._addon_names(section)
            submitted = await sections.submit(
                section,
                self.host.configured,
                saved,
                form,
                addon_names=names,
                locked=self.host.locked,
            )
            if submitted.problems:
                return submitted, None, []
            written = sections.writes(
                section, self.host.configured, saved, submitted, locked=self.host.locked
            )
            if written.writes:
                await self.saved.put(section, written.writes, session.username)
            if whole:  # a form with all of the section's settings replaces what did not fit
                self.problems.pop(section, None)
            # (Applied before another save of the section - an add-on's rename - may write:
            # what runs is what was saved last.)
            applied = await self._running_section(
                section, written.changed, submitted.candidate, interim
            )
        if written.changed:
            kind = sections.kind_of(section, submitted.candidate)
            secret = {f.key for f in sections.fields_of(section, kind) if f.kind == "secret"}
            log.info(
                "dashboard: %s saved %s",
                printable(session.username),
                ", ".join(
                    f"{section}.{key}"
                    + (" (secret)" if key in secret else "")
                    + ("" if key in applied else " (applies after a restart)")
                    for key in written.changed
                ),
            )
        if written.displaced:
            log.info(
                "dashboard: %s moved an address to another host: the saved %s was removed",
                printable(session.username),
                ", ".join(f"{section}.{key}" for key, _ in written.displaced),
            )
        if written.removed:
            log.info(
                "dashboard: %s removed the saved %s (the environment sets it)",
                printable(session.username),
                ", ".join(f"{section}.{key}" for key in written.removed),
            )
        return submitted, written, applied

    # --- restart ------------------------------------------------------------------------

    async def restart_now(self, request: Request, session: Session) -> Response:
        """ "Restart Shijhon": the orderly stop a stop signal gives; the process exits, and
        whatever runs it starts it again (``ShijhonApp.begin_restart``). Pressed again
        while one is under way: the waiting page, nothing more. Refused, with the reason
        on a page, where nothing would start Shijhon again, where the configuration as it
        is now would not bring it back here (it is read first, as the next start reads
        it), and while it stops."""
        target = _own_page(request.state.form.get("next", ""))
        if not self.host.restarting:
            if not self.host.can_restart:
                why = (
                    "Nothing here starts Shijhon again after it stops. Restart it where it is run."
                )
            elif (problem := self.host.restart_problem()) is not None:
                log.warning("dashboard: not restarted: %s", problem)
                why = f"Nothing was stopped: {problem}. Correct it, then restart Shijhon."
            elif self.host.begin_restart():
                log.info("dashboard: %s restarts Shijhon", printable(session.username))
                why = ""
            else:
                why = "Shijhon is stopping. It was not restarted."
            if why:
                return await self._render(
                    "restarting.html",
                    session=session,
                    title="Not restarted",
                    status_code=409,
                    refused=why,
                    next=target,
                )
        return self._see(f"/restarting?boot={self.boot}&next={quote(target, safe='/')}")

    async def upgrade_alert_dismiss(self, request: Request, session: Session) -> Response:
        """Dismiss the alert about an upgrade without its snapshot: its record goes, and the
        state its notification was sent for ends."""
        target = _own_page(request.state.form.get("next", ""))
        if self.skipped is not None and self.store is not None:
            await skipped.dismiss(self.store)
            self.skipped = None
            notices = self.services.notices if self.services is not None else None
            if notices is not None:
                notices.upgraded_without_snapshot(None)
            log.info(
                "dashboard: %s dismissed the alert about the upgrade without a snapshot",
                printable(session.username),
            )
        return RedirectResponse(target, status_code=303, headers=HEADERS)

    async def restarting_page(self, request: Request, session: Session) -> Response:
        """The page shown while Shijhon restarts. Asked of the run that is restarting: the
        page, which waits for the next run (a small script asks ``alive``); asked of any
        other run - the next one, or one that is not restarting - : on to where the restart
        was asked from."""
        target = _own_page(request.query_params.get("next", ""))
        if request.query_params.get("boot") != self.boot or not self.host.restarting:
            return RedirectResponse(target, status_code=303, headers=HEADERS)
        response = await self._render(
            "restarting.html",
            session=session,
            title="Restarting",
            boot=self.boot,
            next=target,
            refused="",
        )
        # The one page with a script (its own file): it asks this address until the next
        # run answers.
        response.headers["content-security-policy"] = RESTARTING_POLICY
        return response

    async def alive(self, request: Request) -> Response:
        """Which run of Shijhon answers (``boot``, nothing else): open, like the sign-in
        page - the waiting page asks it while nobody may be signed in yet."""
        return PlainTextResponse(self.boot, headers={"cache-control": "no-store"})

    # --- catalog ----------------------------------------------------------------------

    async def _catalog_identity_form(
        self, applying: Any, form: dict[str, str] | None
    ) -> tuple[dict[str, Any], dict[str, str] | None]:
        env = self.host.locked.get("catalog", {})
        values = form or {}
        chosen = (
            str(getattr(applying, "addon", ""))
            if "addon" in env
            else values.get("addon", str(getattr(applying, "addon", "")))
        )
        confirmed = str(getattr(applying, "identity", ""))
        declared = await self._addon_identity(chosen)
        notice = ""
        if declared:
            notice = f"This add-on says its catalog is {declared}."
            if confirmed and confirmed != declared:
                notice += " Its declared identity changed."
            if confirmed != declared:
                notice += (
                    f" Change {env['identity']} to confirm it."
                    if "identity" in env
                    else " Select Confirm this identity and save to use its catalog IDs."
                )
                notice += (
                    " Confirming a different catalog key starts matches and saved catalog"
                    " lists over; music in your library stays."
                )
        elif declared is None:
            notice = (
                "The add-on's catalog identity could not be confirmed. Catalog requests"
                " stop if its declaration changed. Check the add-on before changing"
                " the confirmed identity."
            )
        else:
            notice = (
                "No catalog identity is confirmed. Shijhon keeps this catalog's items"
                " under the add-on's name. Confirming a declared identity later starts"
                " matches and saved catalog lists over; music in your library stays."
            )
            if confirmed:
                notice = (
                    "The chosen add-on declares no catalog identity."
                    + (
                        f" Change {env['identity']} to use items under its name."
                        if "identity" in env
                        else " Select Use the add-on's name and save to use items under its name."
                    )
                    + " Changing the catalog key starts matches and saved catalog"
                    " lists over; music in your library stays."
                )
        if form is not None:
            form = {
                **values,
                "kind": "addon",
                sections.SHOWN_KIND: "addon",
                "addon": chosen,
                "identity": confirmed,
            }
        return {
            "identity_addon": chosen,
            "identity_notice": notice,
            "identity_confirm": declared
            if declared != confirmed and "identity" not in env
            else None,
        }, form

    async def _addon_identity(self, name: str) -> str | None:
        if self.services is None or not name:
            return None
        try:
            with pacing.urgent(pacing.CHECK), anyio.fail_after(2):
                for source in await self.services.sources.enabled():
                    if source.name == name:
                        manifest = await source.addon.manifest()
                        declared = manifest.full_catalog
                        return declared.identity if declared else ""
        except (AddonError, TimeoutError):
            pass
        return None

    async def _catalog_status(self, saved: Any) -> dict[str, Any]:
        services = self.services
        catalog = services.catalog if services else None
        applying = effective(self.host.configured, saved, locked=self.host.locked)[0].catalog
        adapter = plugin.find(applying.kind)
        labels = {f.key: (f.id, f.meta.label) for f in sections.fields_of("catalog", applying.kind)}
        info: dict[str, Any] = {
            "kind": applying.kind,
            "running": catalog is not None,
            "label": adapter.label if adapter is not None else "",
            "notice": adapter.notice if adapter is not None else "",
        }
        if catalog is not None and (
            self.catalog_check is None or time.time() - self.catalog_check.at > 60
        ):
            self.catalog_check = await status.check_catalog(catalog)
        info["check"] = self.catalog_check if catalog is not None else None
        resting = None
        additions = services.additions if services else None
        if additions is not None and additions.clock() < additions.resting_until:
            resting = time.time() + (additions.resting_until - additions.clock())
        info["resting"] = resting
        # What keeps the catalog from being built (its adapter says; it may read a file).
        found = await anyio.to_thread.run_sync(catalog_problem, applying)
        key = found[0] if found is not None and found[0] in labels else "kind"
        info["problem"] = (
            None
            if found is None
            else {
                "key": key,
                "id": labels[key][0],
                "label": labels[key][1],
                "row": found[1],
                "notice": found[2],
            }
        )
        # The configuration's settings not sent to an address saved here on another host.
        info["withheld"] = [
            {"label": labels[key][1], "id": labels[address][0], "address": labels[address][1]}
            for key, address in withheld(self.host.configured.catalog, applying)
        ]
        # Values saved here for another address than the one in use: not sent there either.
        info["displaced"] = [
            {"label": labels[key][1], "id": labels[key][0], "address": labels[address][1]}
            for key, address in out_of_place(self.host.configured, saved, self.host.locked)
            if key in labels and address in labels
        ]
        return info

    async def _sound_status(self, saved: Any) -> dict[str, Any]:
        """The Sound page's status: whether AudioMuse-AI is connected in this run, whether it
        answers, how many songs have facts, when they were read, and what went wrong."""
        applying = effective(self.host.configured, saved, locked=self.host.locked)[0].audiomuse
        services = self.services
        sound = services.playlists.sound if services and services.playlists else None
        info: dict[str, Any] = {
            "configured": bool(applying.url),
            "running": sound is not None,
            "status": None,
        }
        if sound is not None:
            try:
                info["status"] = await sound.status()
            except Exception:
                log.exception("dashboard: the sound analysis status could not be read")
        return info

    def agent_access(self) -> tuple[str, bool]:
        """The MCP address agents reach in this run ("" when agent access is not serving),
        and whether an agent address is set but serving failed (the log says why)."""
        agents = self.host.agents
        if agents is None:
            return "", False
        return (agents.oauth.resource, False) if agents.serving else ("", True)

    def _agent_status(self, saved: Any) -> dict[str, str]:
        """The Agent access page's status: the MCP address agents use in this run ("" when
        agent access is off or failed to start), and the one the saved settings give after
        a restart."""
        applying = effective(self.host.configured, saved, locked=self.host.locked)[0].agents
        running, failed = self.agent_access()
        host = urlsplit(running).hostname
        return {
            "running": running,
            "failed": "true" if failed else "",
            "configured": applying.public_url + "/shijhon/mcp" if applying.public_url else "",
            "plain": "true" if running.startswith("http://") and host not in LOOPBACK_HOSTS else "",
        }

    def _notification_status(self) -> dict[str, Any]:
        """The Notifications page's status: how many addresses are in use, and the outcome
        of the last notification when it could not be sent (never an address)."""
        services = self.services
        notices = services.notices if services is not None else None
        if notices is None:
            return {"running": False, "addresses": 0, "failed": None, "sent": None}
        failed = notices.last_failed
        sent = notices.last_sent
        return {
            "running": True,
            "addresses": len(notices.settings.addresses()),
            # (Said only while no notification went out after it.)
            "failed": failed if failed is not None and (sent is None or sent < failed[0]) else None,
            "sent": sent,
        }

    async def notification_test(self, request: Request, session: Session) -> Response:
        """ "Send a test notification": sent now to the addresses in use, and whether each
        took it - in words that never name an address."""
        services = self.services
        notices = services.notices if services is not None else None

        def note(words: str, tone: str) -> Response:
            self._remember(session, Flash("notifications", words=words, tone=tone))
            return self._see("/notifications")

        if notices is None:
            return note("Shijhon is starting: try again in a moment.", "error")
        if not notices.settings.addresses():
            return note("No address is set: enter one and save, then send a test.", "error")
        outcome = await notices.test()
        log.info(
            "dashboard: %s sent a test notification: %s",
            printable(session.username),
            outcome.words(),
        )
        if outcome.ok:
            return note(
                "The test notification was sent. If it did not arrive, check the address with"
                " its service.",
                "ok",
            )
        return note(
            f"The test notification could not be sent: {outcome.words()}. Check the address"
            " and its token with the service; the log says no more than this.",
            "error",
        )

    async def catalog_wait_save(self, request: Request, session: Session) -> Response:
        """The Catalog page's second form: how long a search or an artist page waits for
        the catalog (``[search] budget_seconds``, applied at once)."""
        form = {
            key: value
            for key, value in request.state.form.items()
            if key.removesuffix(sections.CLEAR) in SEARCH_WAIT or key == "csrf"
        }
        # (The wait is the one setting of [search] the dashboard saves: saving it replaces
        # a saved value that did not fit.)
        submitted, written, _ = await self._save(session, "search", form)
        if written is None:
            return await self._section_render(
                session,
                "catalog",
                "catalog",
                wait=(form, submitted.problems),
                status_code=400,
            )
        flash = Flash(
            "catalog",
            keys=tuple(written.changed),
            status=_saved(written),
            part=SEARCH_WAIT_FORM,
        )
        self._remember(session, flash)
        return self._see("/catalog")

    async def catalog_check_now(self, request: Request, session: Session) -> Response:
        services = self.services
        if services is not None and services.catalog is not None:
            self.catalog_check = await status.check_catalog(services.catalog)
        return self._see("/catalog")

    # --- add-ons ------------------------------------------------------------------------

    async def _addon(self, addon_id: int) -> StoredSource:
        for addon in await self._stored():
            if addon.id == addon_id:
                return addon
        raise Refused(PlainTextResponse("No such add-on.\n", 404, headers=HEADERS))

    def _env_list(self) -> str | None:
        """The variable that sets the add-on list, if the environment does."""
        return self.host.configured.from_environment.get("addons", {}).get("list")

    def _editable_list(self) -> None:
        variable = self._env_list()
        if variable is not None:
            raise Refused(
                PlainTextResponse(
                    f"The add-on list is set by the environment ({variable}); change it there.\n",
                    409,
                    headers=HEADERS,
                )
            )

    async def _owned(self, session: Session, conn: aiosqlite.Connection) -> None:
        """Any change in the dashboard makes the stored add-on list the one that applies."""
        assert self.saved is not None
        if "dashboard" not in await self.saved.section(ADDONS, conn):
            await self.saved.write(conn, ADDONS, {"dashboard": True}, session.username)
        self.addons_handed_back = False

    async def _load_marks(self, conn: aiosqlite.Connection | None = None) -> None:
        marks: dict[int, dict[str, str]] = {}
        if self.saved is not None:
            for key, item in (await self.saved.section(SHOWN, conn)).items():
                if key.isdigit() and isinstance(item.value, dict):
                    marks[int(key)] = {str(k): str(v) for k, v in item.value.items()}
        self._marks = marks

    async def _stored_marked(self) -> list[StoredSource]:
        """The stored add-ons with the marks of what their forms may show (``_marks``), read
        in one transaction: a save writes an add-on's settings and its marks in one, so a
        page never reads the settings of one save with the marks of another."""
        services = self.services
        if services is None:
            raise Refused(PlainTextResponse("Shijhon is starting.\n", 503, headers=HEADERS))
        async with services.store.transaction() as conn:
            addons = await services.sources.stored(conn)
            await self._load_marks(conn)
        return addons

    def _addon_version(self, addon: StoredSource) -> str:
        """What an add-on's settings form was made from - its address, network, settings,
        budget and the manifest's declared settings - keyed (secrets' values cannot be
        guessed from it): a form of another version is not saved."""
        check = self.checks.known(addon)
        declared = (
            [[d.key, d.kind, d.default, list(d.options)] for d in check.manifest.declared]
            if check is not None and check.manifest is not None
            else None
        )
        marks = self._marks.get(addon.id, {})
        own = dataclasses.astuple(addon.limits)
        content = [
            addon.name, addon.base_url, str(addon.reach), addon.settings, addon.budget, declared,
            marks, own, addon.downloads, addon.preparations, addon.capacity_group
        ]  # fmt: skip
        text = json.dumps(content, sort_keys=True, default=str)
        return hmac.new(self._form_key, text.encode(), hashlib.sha256).hexdigest()[:32]

    def _signing_in(self, addon: StoredSource) -> bool:
        """Whether the add-on shows "Needs sign-in" (``_health``): on, and it or its manifest
        said its operator must sign in."""
        if not addon.enabled:
            return False
        if self.services is not None and self.services.sources.attention(addon.id) is not None:
            return True
        check = self.checks.known(addon)
        return check is not None and check.error == NEEDS_SIGN_IN

    def _health(self, addon: StoredSource) -> dict[str, Any]:
        """Light and words: off; needing its operator to sign in (it said so: with its page
        for it, ``link``); the manifest unreachable; cooling down; failing (3 failures in a
        row without a success: one missing song is not an error); OK."""
        services = self.services
        check = self.checks.known(addon)
        found = services.sources.stats(addon.id) if services is not None else None
        until = services.sources.cooling_until(addon.id) if services is not None else None
        health: dict[str, Any] = {
            "light": "",
            "words": "Not checked yet",
            "code": None,
            "attention": False,
            "detail": "",
            "link": None,
            "state": "not_checked",  # the same in one word, for an agent's status
        }
        if not addon.enabled:
            return {**health, "light": "off", "words": "Off", "state": "off"}
        needs = services.sources.attention(addon.id) if services is not None else None
        if needs is not None:
            if self._catalog_only(addon):  # (not passed over: its catalog is asked as before)
                again = "shown so until its catalog answers again"
            elif until is not None:
                again = Markup("passed over until {}, then asked again").format(when(until))
            else:
                again = "asked again at the next play"
            return {
                **health,
                "light": "error",
                "words": NEEDS_SIGN_IN_WORDS,
                "attention": True,
                "link": needs.url,
                "detail": Markup("It said so at {}; {}.").format(when(needs.since), again),
                "state": "needs_sign_in",
            }
        if check is not None and check.error == NEEDS_SIGN_IN:  # (its manifest's answer)
            return {
                **health,
                "light": "error",
                "words": NEEDS_SIGN_IN_WORDS,
                "attention": True,
                "link": check.page,
                "detail": Markup("Its manifest said so at {}.").format(hm(check.at)),
                "state": "needs_sign_in",
            }
        if check is not None and check.error is not None:
            health.update(
                light="error",
                words="Manifest unreachable:",
                code=check.error,
                attention=True,
                detail=Markup("Checked {}").format(hm(check.at)),
                state="manifest_unreachable",
            )
        elif until is not None:
            health.update(
                light="cool",
                words=Markup("Cooling down until {}").format(when(until)),
                state="cooling_down",
            )
        elif (
            found is not None
            and found.failures_since_success >= FAILING_IN_A_ROW
            and not _rate_limit(found.last_failure)  # shown as cooling down while it lasts
        ):
            health.update(
                light="error",
                words="Error:",
                code=found.last_failure,
                attention=True,
                detail=Markup("{:,} failures in a row, the last at {}").format(
                    found.failures_since_success, when(found.last_failure_at or time.time())
                ),
                state="failing",
            )
        elif check is not None:
            health.update(light="ok", words="OK", state="ok")
        if found is not None and not health["detail"]:
            parts: list[Markup] = []
            if found.last_success_at is not None:  # its audio's first byte, not a link
                parts.append(Markup("Last audio delivered {}").format(when(found.last_success_at)))
            if found.successes or found.failures:
                parts.append(
                    Markup("{:,} found, {:,} failed since {}").format(
                        found.successes, found.failures, hm(self.started_at)
                    )
                )
            if found.last_failure and found.last_failure_at:
                parts.append(
                    Markup("last failure {}: {}").format(
                        when(found.last_failure_at), found.last_failure
                    )
                )
            health["detail"] = Markup("; ").join(parts)
        return health

    async def _pace(self, url: str, base_url: str) -> AddonPace | None:
        """The limits of the add-on origin ``url`` is at: the dashboard's manifest
        requests count there like any request, after everything a listener waits for."""
        return None if self.services is None else await self.services.sources.pace_at(url, base_url)

    async def _stored(self) -> list[StoredSource]:
        services = self.services
        if services is None:
            raise Refused(PlainTextResponse("Shijhon is starting.\n", 503, headers=HEADERS))
        return await services.sources.stored()

    def _addon_rows(
        self,
        addon: StoredSource,
        form: dict[str, str] | None,
        problems: dict[str, sections.Problem],
        capacity: tuple[int, int] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The rows of an add-on's settings form: its name, its manifest's, then where it
        runs and its address; and those under its "Advanced settings": its time and its
        limits - of an add-on that is a catalog only, its request limits alone."""
        running = self.running or self.host.configured
        prefix = f"addon-{addon.id}"
        catalog_only = self._catalog_only(addon)
        if form is not None:
            budget_text = form.get("budget_seconds", "")
        else:
            budget_text = format_number(addon.budget) if addon.budget is not None else ""
        reach = form.get("reach", addon.reach) if form is not None else addon.reach
        total = format_number(running.delivery.budget_seconds)
        check = self.checks.known(addon)
        declared = check.manifest.declared if check is not None and check.manifest else ()
        rows: list[dict[str, Any]] = [
            _setting(
                id=f"{prefix}-name",
                name="name",
                kind="text",
                label="Name",
                text=form.get("name", addon.name) if form is not None else addon.name,
                max_length=MAX_ADDON_NAME,
                help="How the add-on is shown here and in the settings that choose one. A new"
                " name changes only that: its catalog, its songs and the settings that name it"
                " stay with it.",
                problem=_row(problems.get("name")),
            ),
            *(self._declared_row(addon, item, form, problems) for item in declared),
            _setting(
                id=f"{prefix}-reach",
                name="reach",
                kind="select",
                label="Network",
                options=list(REACHES.items()),
                chosen=reach,
                help=REACH_HELP,
                default="Default Internet",
                problem=_row(problems.get("reach")),
                modified=addon.reach != "public",
            ),
            _setting(
                id=f"{prefix}-manifest",
                name="manifest",
                kind="secret",
                label="Manifest URL",
                state=("set", f"at {host_of(addon.base_url)}"),
                help="The add-on's address. It often contains a key, so it is never shown."
                " Leave it empty to keep the current one.",
                problem=_row(problems.get("manifest")),
                clear_label="",
            ),
        ]
        more = [
            _setting(
                id=f"{prefix}-budget",
                name="budget_seconds",
                kind="decimal",
                label="Time for a download",
                spoken="seconds",
                unit="s",
                text=budget_text,
                help="How long this add-on has when nobody waits for the song: a download, or"
                " a song prepared ahead, when the add-ons are asked one at a time. Give more to"
                " one that prepares whole files first. Its time in a song's start is its step's,"
                " set in the plan above. Playlist imports give its catalog at least this long"
                " too. Empty: Playback's setting applies.",
                default=f"Default {total} s, from Playback",
                problem=_row(problems.get("budget_seconds")),
                modified=addon.budget is not None,
            ),
            slow_after_row(
                prefix,
                running.delivery.slow_after,
                form.get("slow_after", "")
                if form is not None
                else (format_number(addon.slow_after) if addon.slow_after is not None else ""),
                problem=_row(problems.get("slow_after")),
                modified=addon.slow_after is not None,
            ),
            *(self._limit_row(addon, limit, form, problems) for limit in ADDON_LIMITS),
            *(
                self._limit_row(
                    addon,
                    limit,
                    form,
                    problems,
                    effective=capacity[index] if capacity is not None else None,
                )
                for index, limit in enumerate(ADDON_TRANSFER_LIMITS)
            ),
            _setting(
                id=f"{prefix}-capacity-group",
                name="capacity_group",
                kind="text",
                label="Shared account or worker group",
                text=form.get("capacity_group", addon.capacity_group)
                if form is not None
                else addon.capacity_group,
                max_length=100,
                help="Give add-ons that use one account or worker the same label, at one"
                " address or at different ones: Shijhon can't tell that from the address. They"
                " then share their limits on fetched songs and new preparations, and the"
                " strictest applies. The label is not sent to the add-on.",
                default="Default none: each add-on has its own limits",
                problem=_row(problems.get("capacity_group")),
                modified=bool(addon.capacity_group),
            ),
        ]
        if catalog_only:  # (what is not shown is kept as it is: a save leaves it alone)
            # Its own time is a catalog's too: playlist imports wait that long for it.
            waits = format_number(running.catalog.timeout_seconds)
            timed = _setting(
                id=f"{prefix}-budget",
                name="budget_seconds",
                kind="decimal",
                label="Time for catalog answers",
                spoken="seconds",
                unit="s",
                text=budget_text,
                help="How long playlist imports wait for this add-on's catalog, when longer"
                " than the Catalog page's request timeout. Give more to a slow add-on. Empty:"
                " the request timeout applies.",
                default=f"Default {waits} s, from Catalog",
                problem=_row(problems.get("budget_seconds")),
                modified=addon.budget is not None,
            )
            more = [timed, *(row for row in more if row["name"] in CATALOG_LIMITS)]
        return rows, more

    def _catalog_only(self, addon: StoredSource) -> bool:
        """Whether the add-on is a catalog only: its manifest, as last read, declares no
        ``stream`` (``Manifest.streams``, as the routing's ``Source.streams``) - Shijhon
        never asks it for audio. One whose manifest could not be read is not (it stays among
        the others, with its problem)."""
        check = self.checks.known(addon)
        return bool(check is not None and check.manifest and not check.manifest.streams)

    def _audio(self, addons: list[StoredSource]) -> list[StoredSource]:
        """The add-ons in the order audio is asked for: all but those that are a catalog
        only, by their steps, then in the list's order - as plays ask them
        (``SourceRegistry.streaming``): one outside the plan whose manifest is not known
        comes after the plan's steps, never between the add-ons of one."""
        audio = [addon for addon in addons if not self._catalog_only(addon)]
        return sorted(audio, key=lambda addon: (addon.step, addon.position, addon.id))

    def _step_numbers(self, addons: list[StoredSource]) -> dict[int, int]:
        """Each audio add-on's step of the plan, counted from 1 (``_audio``'s order)."""
        steps: dict[int, int] = {}
        before: StoredSource | None = None
        for addon in self._audio(addons):
            begins = before is None or addon.step != before.step
            steps[addon.id] = max(steps.values(), default=0) + (1 if begins else 0)
            before = addon
        return steps

    def _audio_ids(self, addons: list[StoredSource]) -> list[int]:
        """The IDs of ``_audio``."""
        return [addon.id for addon in self._audio(addons)]

    def _limit_row(
        self,
        addon: StoredSource,
        limit: _OwnLimit,
        form: dict[str, str] | None,
        problems: dict[str, sections.Problem],
        *,
        effective: int | None = None,
    ) -> dict[str, Any]:
        """One of the add-on's own limits on what Shijhon sends it; empty: the
        installation's (Playback, "Limits per add-on", or "Downloads and playlist
        preparation" for a transfer limit)."""
        running = self.running or self.host.configured
        own = getattr(addon if limit.transfer else addon.limits, limit.name)
        if form is not None:
            text = form.get(limit.name, "")
        else:
            text = format_number(own) if own is not None else ""
        shared = format_number(getattr(running.delivery, limit.setting))
        default = f"Default {counted(shared, limit.unit)}, from Playback"
        if limit.transfer:
            inherited = int(getattr(running.delivery, limit.setting))
            alone = own if own is not None else inherited
            effective = effective if effective is not None else alone

            def amount(value: int) -> str:
                return "no limit" if value == 0 else counted(str(value), limit.unit)

            # What applies: its own or Playback's, or a stricter one of the add-ons it
            # shares its limits with (at its address, or in its shared group).
            applies = f"In effect: {amount(effective)}"
            if effective != alone:
                applies += ", from the add-ons it shares limits with"
            default = f"Default {amount(inherited)}, from Playback. {applies}."
            if not addon.enabled:
                default = f"Default {amount(inherited)}, from Playback. Not in effect while off."
        return _setting(
            id=f"addon-{addon.id}-{_slug(limit.name)}",
            name=limit.name,
            kind="whole" if limit.whole else "decimal",
            label=limit.label,
            spoken=limit.spoken,
            unit=limit.unit,
            text=text,
            help=limit.help,
            default=default,
            problem=_row(problems.get(limit.name)),
            modified=own is not None,
        )

    def _declared_row(
        self,
        addon: StoredSource,
        item: Declared,
        form: dict[str, str] | None,
        problems: dict[str, sections.Problem],
    ) -> dict[str, Any]:
        name = f"setting.{item.key}"
        value = addon.settings.get(item.key)
        # A stored value is shown only when it cannot be a secret (``shown``): one stored
        # under an earlier declaration - a token whose manifest now offers choices, a key
        # declared a number - is kept and replaced, never rendered.
        plain = shown(item, value, self._marks.get(addon.id, {}))
        current = setting_text(value) if value is not None and plain else (item.default or "")
        shown_default = dict(item.options).get(item.default or "", item.default or "empty")
        row = _setting(
            id=f"addon-{addon.id}-s-{_slug(item.key)}",
            name=name,
            label=item.label,
            help=item.help,
            problem=_row(problems.get(name)),
            modified=value is not None and setting_text(value) != (item.default or ""),
            default=f"Default {shown_default}" if item.kind != "secret" else "",
        )
        if not representable(value):  # a list or table from the configuration file
            row.update(
                kind="mapping",
                text="Set in the configuration file as a list or a table: change it there."
                " It is not sent to the add-on (its own default applies).",
                default="",
            )
        elif item.kind == "secret":
            row.update(
                kind="secret",
                state=("set" if value else "unset", ""),
                clear_label="Remove" if value else "",
            )
        elif item.kind == "select":
            options = list(item.options)
            if plain and current and current not in dict(options):  # the manifest's default
                options.append((current, current))
            if not plain and KEPT not in dict(options):
                options.insert(0, (KEPT, "A stored value that is none of these (kept)"))
            if item.default is None:  # no default: a first option would be saved
                options.insert(0, ("", "The add-on's default"))
            chosen = current if plain else KEPT
            row.update(
                kind="select",
                options=options,
                chosen=form.get(name, chosen) if form is not None else chosen,
            )
        elif item.kind == "switch":
            on = (form.get(name) == "true") if form is not None else current == "true"
            row.update(kind="switch", checked=on)
        elif not plain:  # a number or a text not saved through this field: write-only
            row.update(
                kind="secret",
                state=("set", "(not shown: it was not entered in this field)"),
                clear_label="Remove",
            )
        else:
            row.update(
                kind="decimal" if item.kind == "number" else "text",
                text=form.get(name, "") if form is not None else current,
            )
        return row

    async def addons_page(
        self,
        request: Request,
        session: Session,
        *,
        form_for: int | None = None,
        form: dict[str, str] | None = None,
        problems: dict[str, sections.Problem] | None = None,
        add_error: str | None = None,
        add_code: str | None = None,
        stale: str | None = None,
        status_code: int = 200,
    ) -> Response:
        assert self.saved is not None
        await self.checks.check(await self._stored())
        # (Nothing waits from here to the rows: the marks are these add-ons'.)
        addons = await self._stored_marked()
        running = self.running or self.host.configured
        capacities = effective_capacity(
            addons, running.delivery.addon_downloads, running.delivery.addon_preparations
        )
        flash = self._flash_for(session, "addons")
        items: list[dict[str, Any]] = []
        audio = self._audio_ids(addons)
        steps = self._step_numbers(addons)
        for addon in addons:
            check = self.checks.known(addon)
            mine = form_for == addon.id
            catalog_only = addon.id not in audio
            rows, more = self._addon_rows(
                addon,
                form if mine else None,
                problems or {} if mine else {},
                capacity=capacities.get(addon.id),
            )
            group = ADDON_CATALOG_MORE if catalog_only else ADDON_MORE
            _, fold = sections.folded([(group, more)])
            if flash is not None and flash.addon == addon.id and set(flash.keys):
                fold = dataclasses.replace(fold, open=True)  # (its time or limits saved now)
            items.append(
                {
                    "addon": addon,
                    "host": host_of(addon.base_url),
                    # Its place in the order audio is asked for (none: a catalog only).
                    "catalog_only": catalog_only,
                    "order": 0 if catalog_only else steps[addon.id],
                    "first": not catalog_only and audio.index(addon.id) == 0,
                    "last": not catalog_only and audio.index(addon.id) == len(audio) - 1,
                    "version": check.manifest.version if check and check.manifest else "",
                    "no_downloads": bool(check and check.manifest and not check.manifest.downloads),
                    "health": self._health(addon),
                    "rows": rows,
                    "more": fold,
                    "open": mine or bool(flash and flash.addon == addon.id),
                    "status": (
                        "Not saved"
                        if mine and problems
                        else flash.status
                        if flash and flash.addon == addon.id
                        else ""
                    ),
                    "manifest_error": check.error if check is not None else None,
                    # Settings the add-on is not sent (lists or tables from the file).
                    "unsent": sorted(k for k, v in addon.settings.items() if not representable(v)),
                    "form_version": self._addon_version(addon),
                }
            )
        signing_in = sum(1 for item in items if item["health"]["words"] == NEEDS_SIGN_IN_WORDS)
        errors = sum(1 for item in items if item["health"]["light"] == "error") - signing_in
        cooling = sum(1 for item in items if item["health"]["light"] == "cool")
        owner = await self.saved.addons_owner()
        in_file = self.host.configured.addons
        # DASH links not played for want of ffmpeg (turned off in the settings: not said).
        deliverer = self.services.deliverer if self.services is not None else None
        running = self.running or self.host.configured
        dash_off = (
            deliverer.dash_off
            if deliverer is not None and deliverer.dash is None and running.delivery.dash
            else None
        )
        notices = []
        if form_for is not None and problems:
            edited = next((a for a in addons if a.id == form_for), None)
            labels = {
                row["name"]: (row["id"], row["label"])
                for item in items
                if item["addon"].id == form_for
                for row in [*item["rows"], *(r for _, rs in item["more"].groups for r in rs)]
            }
            notices = [
                (labels.get(k, ("", k))[0], labels.get(k, ("", k))[1], p.notice)
                for k, p in problems.items()
            ]
            title = f"{edited.name if edited else 'Add-on'} settings not saved"
        else:
            title = ""
        return await self._render(
            "addons.html",
            session=session,
            current="addons",
            status_code=status_code,
            items=items,
            audio=[item for item in items if not item["catalog_only"]],
            catalogs=[item for item in items if item["catalog_only"]],
            enabled=sum(1 for a in addons if a.enabled),
            errors=errors,
            cooling=cooling,
            signing_in=signing_in,
            notice_title=title,
            notices=notices,
            flash=flash,
            add_error=add_error,
            add_code=add_code,
            add_reach=(form or {}).get("reach", "public") if form_for is None else "public",
            reaches=list(REACHES.items()),
            reach_help=REACH_HELP,
            from_file=in_file is not None,
            env_list=self._env_list(),
            owner=owner,
            stale=stale,
            handed_back=self.addons_handed_back,
            dash_off=dash_off,
            plan=self._plan(addons, session),
        )

    async def _renamed(self, addon: StoredSource, name: str, naming: list[tuple[str, str]]) -> None:
        """After a rename: the settings that named it, saved with the new name, apply at
        once - the running catalog's (its catalog is the add-on's key, which the rename
        kept, so it is the same catalog: no restart waits)."""
        services, running = self.services, self.running
        if services is None or running is None or running.catalog.kind != "addon":
            return
        inner = getattr(services.catalog, "inner", services.catalog)
        if (
            getattr(running.catalog, "addon", "") == addon.name
            and isinstance(inner, AddonCatalog)
            and inner.addon_key == addon.key
        ):
            catalog = running.catalog.model_copy(update={"addon": name})
            self.running = running.model_copy(update={"catalog": catalog})
            inner.name = name
            if services.deliverer.settings.catalog_addon == addon.name:
                services.deliverer.settings.catalog_addon = name

    async def addon_add(self, request: Request, session: Session) -> Response:
        self._editable_list()
        assert self.store is not None and self.services is not None
        form: dict[str, str] = request.state.form
        url = form.get("manifest", "").strip()
        reach_name = form.get("reach", "public")

        async def refuse(message: str, code: str | None = None) -> Response:
            return await self.addons_page(
                request, session, form=form, add_error=message, add_code=code, status_code=400
            )

        if reach_name not in REACHES:
            return await refuse("Choose where the add-on runs.")
        if not url.lower().startswith(("https://", "http://")) or len(url) < 10:
            return await refuse("Enter a full URL starting with https:// or http://.")
        try:
            manifest = await fetch_manifest(
                url, Reach(reach_name), resolver=self.resolver, pace=self._pace
            )
        except Busy as exc:  # not asked: its turn did not come, or it said to wait
            return await refuse("Nothing added: the add-on can't be asked right now.", str(exc))
        except ValueError as exc:
            return await refuse("Nothing added: no add-on answers at this address.", str(exc))
        name = manifest.name or httpx.URL(url).host or "Add-on"
        try:
            added = await self.services.sources.add(
                name,
                url,
                {},
                reach=Reach(reach_name),
                unique_name=True,
                also=functools.partial(self._owned, session),
            )
        except ValueError:
            return await refuse(f"Nothing added: an add-on named “{name}” is in the list already.")
        for addon in await self._stored():
            if addon.id == added:  # its manifest was read a moment ago: not asked for again
                self.checks.learn(addon, manifest)
        log.info("dashboard: %s added the add-on %s", printable(session.username), printable(name))
        self._remember(session, Flash("addons", words=f"{name} added at the end of the list."))
        return self._see("/addons")

    async def addon_enabled(self, request: Request, session: Session) -> Response:
        self._editable_list()
        assert self.services is not None
        addon = await self._addon(int(request.path_params["addon"]))
        wanted = request.state.form.get("enabled") == "true"
        if wanted != addon.enabled:
            await self.services.sources.set_enabled(
                addon.id, wanted, also=functools.partial(self._owned, session)
            )
            log.info(
                "dashboard: %s turned the add-on %s %s",
                printable(session.username),
                printable(addon.name),
                "on" if wanted else "off",
            )
        return self._see(f"/addons#addon-{addon.id}")

    async def addon_check_again(self, request: Request, session: Session) -> Response:
        """The add-on's operator signed in again: it is no longer shown as needing sign-in,
        nor passed over for it, and its manifest is read again - nothing about the list
        changes (also one the environment sets)."""
        assert self.services is not None
        addon = await self._addon(int(request.path_params["addon"]))
        self.services.sources.answered(addon.id)
        self.checks.again(addon.id)
        log.info(
            "dashboard: %s said the add-on %s is signed in again",
            printable(session.username),
            printable(addon.name),
        )
        self._remember(session, Flash("addons", words=f"{addon.name} is asked again."))
        return self._see(f"/addons#addon-{addon.id}")

    async def addons_plan(self, request: Request, session: Session) -> Response:
        """The plan addon_plan.js saves after a change: its steps (each with its add-ons and
        its time), whether add-ons are asked which have the song ready, and when one that
        has takes over; answered as JSON. Every add-on asked for audio is in it once.
        Refused (the list changed meanwhile, or a value does not fit), the page reloads and
        its notice says so."""
        self._editable_list()
        assert self.services is not None
        refused = "Plan not saved: the add-ons changed meanwhile. Here it is as it is now."
        try:
            sent = json.loads(request.state.form.get("plan", ""))
            steps = [
                ([int(i) for i in step["addons"]], float(step["seconds"])) for step in sent["steps"]
            ]
            checks, handover = bool(sent["checks"]), float(sent["handover"])
        except (ValueError, TypeError, KeyError, RecursionError):  # (deeply nested JSON)
            steps, checks, handover = [], False, 0.0
        ids = self._audio_ids(await self._stored())
        listed = [source_id for addons, _ in steps for source_id in addons]
        fits = all(addons for addons, _ in steps) and all(
            ADDON_BUDGET["low"] < seconds <= ADDON_BUDGET["high"] for _, seconds in steps
        )
        if sorted(listed) != sorted(ids) or len(set(listed)) != len(listed) or not fits:
            self._remember(session, Flash("addons", words=refused, tone="error"))
            return JSONResponse({"saved": False}, 409, headers=HEADERS)
        # The settings first: one that does not fit refuses the plan before its steps are
        # saved, so nothing of it is.
        running = self.running or self.host.configured
        wanted = {"ready_checks": checks, "ready_handover_seconds": handover}
        env = self.host.locked.get("delivery", {})
        form = {
            key: ("true" if value else "false") if isinstance(value, bool) else format_number(value)
            for key, value in wanted.items()
            if key not in env and getattr(running.delivery, key) != value
        }
        if form:
            _, written, _ = await self._save(session, "delivery", form, whole=False)
            if written is None:
                self._remember(session, Flash("addons", words=refused, tone="error"))
                return JSONResponse({"saved": False}, 409, headers=HEADERS)
        try:
            await self.services.sources.arrange(steps, also=functools.partial(self._owned, session))
        except ValueError:  # an add-on removed meanwhile
            if form:  # (the two settings are saved and applied: say so)
                refused = (
                    "Plan's steps not saved: the add-ons changed meanwhile. Its ready checks "
                    "were saved. Here it is as it is now."
                )
            self._remember(session, Flash("addons", words=refused, tone="error"))
            return JSONResponse({"saved": False}, 409, headers=HEADERS)
        log.info("dashboard: %s changed the add-ons' plan", printable(session.username))
        return JSONResponse({"saved": True}, headers=HEADERS)

    def _plan(self, addons: list[StoredSource], session: Session) -> dict[str, Any] | None:
        """The plan for addon_plan.js: the add-ons asked for audio in their steps, each with
        its light and its recent starts."""
        audio = self._audio(addons)
        if not audio:
            return None
        running = self.running or self.host.configured
        delivery = running.delivery
        steps: list[dict[str, Any]] = []
        for index, addon in enumerate(audio):
            entry = self._plan_addon(addon)
            seconds = (
                addon.step_seconds if addon.step_seconds is not None else delivery.budget_seconds
            )
            if index and addon.step == audio[index - 1].step:
                steps[-1]["addons"].append(entry)
                steps[-1]["seconds"] = max(steps[-1]["seconds"], seconds)
            else:
                steps.append({"seconds": seconds, "addons": [entry]})
        env = self.host.locked.get("delivery", {})
        # The add-ons whose manifests declare the ready check (Shijhon's own extension).
        checking = [
            addon.name
            for addon in audio
            if (c := self.checks.known(addon))
            and c.manifest
            and "availability" in c.manifest.resources
        ]
        return {
            "steps": steps,
            "checking": checking,
            "count": len(audio),
            "wait": delivery.max_wait_seconds,
            "default": delivery.budget_seconds,
            "checks": delivery.ready_checks,
            "handover": delivery.ready_handover_seconds,
            "checks_locked": any(key in env for key in PLAN_SETTINGS),
            "action": f"{PATH}/addons/plan",
            "csrf": session.csrf,
            "readonly": self._env_list() is not None,
        }

    def _plan_addon(self, addon: StoredSource) -> dict[str, Any]:
        """One add-on of the plan: its light, and its latest starts - how many sent audio,
        and the middle of their times."""
        stats = self.services.sources.stats(addon.id) if self.services is not None else None
        recent = list(stats.recent)[-RECENT_STARTS:] if stats is not None else []
        times = [attempt for attempt in recent if attempt.delivered]
        usual = usual_seconds(stats)
        return {
            "id": addon.id,
            "name": addon.name,
            "light": (health := self._health(addon))["light"],
            # What it needs from its operator (it said so), shown instead of its speed; and
            # whether plays pass it over for it now (the plan's run leaves it out then).
            "attention": health["words"] if health["words"] == NEEDS_SIGN_IN_WORDS else None,
            "passed": self.services is not None
            and self.services.sources.passed_over_for_sign_in(addon.id),
            "off": not addon.enabled,
            "usual": None if usual is None else round(usual, 1),
            "played": len(times),
            "tried": len(recent),
        }

    async def addons_enabled(self, request: Request, session: Session) -> Response:
        """Every add-on on, or every one off."""
        self._editable_list()
        assert self.services is not None
        wanted = request.state.form.get("enabled") == "true"
        changes = {addon.id: wanted for addon in await self._stored() if addon.enabled != wanted}
        if changes:
            await self.services.sources.set_enabled_many(
                changes, also=functools.partial(self._owned, session)
            )
            log.info(
                "dashboard: %s turned every add-on %s",
                printable(session.username),
                "on" if wanted else "off",
            )
        words = "Every add-on is on." if wanted else "Every add-on is off."
        self._remember(session, Flash("addons", words=words))
        return self._see("/addons")

    async def addon_remove(self, request: Request, session: Session) -> Response:
        self._editable_list()
        assert self.services is not None
        addon = await self._addon(int(request.path_params["addon"]))

        async def also(conn: aiosqlite.Connection) -> None:
            await self._owned(session, conn)
            assert self.saved is not None
            await self.saved.write(conn, SHOWN, {str(addon.id): REMOVE}, session.username)

        await self.services.sources.remove(addon.id, also=also)
        self.checks.forget(addon.id)
        log.info(
            "dashboard: %s removed the add-on %s",
            printable(session.username),
            printable(addon.name),
        )
        self._remember(session, Flash("addons", words=f"{addon.name} removed."))
        return self._see("/addons")

    async def addon_settings(self, request: Request, session: Session) -> Response:
        self._editable_list()
        addon_id = int(request.path_params["addon"])
        # One save of an add-on at a time, each reading what the last one wrote: secrets a
        # move to another host removed must not come back with a save begun before it.
        async with self._addon_saves.hold(str(addon_id)):
            return await self._addon_settings(request, session, addon_id)

    async def _addon_settings(self, request: Request, session: Session, addon_id: int) -> Response:
        assert self.services is not None
        addon = await self._addon(addon_id)
        form: dict[str, str] = request.state.form
        problems: dict[str, sections.Problem] = {}
        if self.checks.known(addon) is None:  # e.g. after a move, or a restart: read it now
            await self.checks.check([addon])
        await self._load_marks()  # (this add-on's: no other save of it runs meanwhile)
        marks = dict(self._marks.get(addon.id, {}))  # as the form was made with; then kept
        marked = dict(marks)
        # A form made before a change of the add-on (a save, a move, another manifest) is
        # not saved: its fields may say what is no longer true (a secret shown as plain).
        # The manifest the version was checked with, kept for the whole save (a page loading
        # meanwhile may read it again).
        check = self.checks.known(addon)
        manifest = check.manifest if check is not None else None
        if not hmac.compare_digest(form.get("version", ""), self._addon_version(addon)):
            log.info(
                "dashboard: %s's settings form for %s was older than the add-on: not saved",
                printable(session.username),
                printable(addon.name),
            )
            return await self.addons_page(
                request, session, form_for=addon.id, stale=addon.name, status_code=409
            )

        def problem(name: str, row: str, notice: str) -> None:
            problems[name] = sections.Problem(row, notice)

        # Fields missing from the form are kept as they are (a page from before a change).
        budget: float | None = addon.budget
        keep_budget = "budget_seconds" not in form
        text = form.get("budget_seconds", "").strip()
        if not keep_budget:
            budget = None
        if text:
            try:
                budget = float(parse_number(text, whole=False))
                if not ADDON_BUDGET["low"] < budget <= ADDON_BUDGET["high"]:
                    raise Invalid("Enter a number above 0, up to 600.")
            except Invalid as exc:
                problem("budget_seconds", str(exc), "must be a number above 0, up to 600.")
        slow_after: float | None = addon.slow_after
        keep_slow_after = "slow_after" not in form
        text = form.get("slow_after", "").strip()
        if not keep_slow_after:
            slow_after = None
        if text:
            try:
                slow_after = float(parse_number(text, whole=False))
                if not 1 <= slow_after <= 10:
                    raise Invalid("Enter a number from 1 to 10.")
            except Invalid as exc:
                problem("slow_after", str(exc), "must be a number from 1 to 10.")
        own: dict[str, float | None] = dataclasses.asdict(addon.limits)
        for limit in ADDON_LIMITS:
            if limit.name not in form:
                continue  # not on the form sent: kept
            text = form[limit.name].strip()
            own[limit.name] = None  # empty: the installation's
            if not text:
                continue
            rule = f"a {'whole ' if limit.whole else ''}number from {limit.low} to {MAX_LIMIT}"
            try:
                value = parse_number(text, whole=limit.whole)
                if not limit.low <= value <= MAX_LIMIT:
                    raise Invalid(f"Enter {rule}.")
                own[limit.name] = value
            except Invalid:
                problem(limit.name, f"Enter {rule}.", f"must be {rule}.")
        transfers = {limit.name: getattr(addon, limit.name) for limit in ADDON_TRANSFER_LIMITS}
        for limit in ADDON_TRANSFER_LIMITS:
            if limit.name not in form:
                continue
            try:
                transfers[limit.name] = transfer_limit(form[limit.name])
            except Invalid as exc:
                rule = f"a whole number from 0 to {MAX_LIMIT}"
                problem(limit.name, str(exc), f"must be {rule}.")
        capacity_group = form.get("capacity_group", addon.capacity_group).strip()
        if len(capacity_group) > 100:
            problem(
                "capacity_group", "Enter at most 100 characters.", "must be at most 100 characters."
            )
        reach = form.get("reach", addon.reach)
        if reach not in REACHES:
            problem("reach", "Choose where the add-on runs.", "must be one of the listed places.")
        # Another name: only what people see changes (its key stays), and the settings
        # that name the add-on change with it - not one the environment sets.
        typed = form.get("name", addon.name)
        new_name = addon.name if typed == addon.name else typed.strip()  # (as the file wrote it)
        if new_name != addon.name:
            others = {item.name for item in await self._stored() if item.id != addon.id}
            assert self.saved is not None
            configured, locked = self.host.configured, self.host.locked
            found = sections.naming(configured, await self.saved.all(), locked, addon.name)
            fixed = list(sections.set_by_environment(configured, locked, found).values())
            rule = f"up to {MAX_ADDON_NAME} characters, on one line"
            if not new_name:
                problem("name", "Enter a name.", "is needed.")
            elif len(new_name) > MAX_ADDON_NAME or not new_name.isprintable():
                problem("name", f"Enter {rule}.", f"must be {rule}.")
            elif new_name in others:
                problem("name", "Another add-on has this name. Choose another.", "is taken.")
            elif fixed:
                variables = ", ".join(fixed)
                problem(
                    "name",
                    f"The environment names this add-on ({variables}). Change it there first.",
                    f"can't change: the environment names this add-on ({variables}).",
                )
        new_url = form.get("manifest", "").strip()
        if new_url and not new_url.lower().startswith(("https://", "http://")):
            problem(
                "manifest",
                "Enter a full URL starting with https:// or http://.",
                "must be a full URL starting with https:// or http://.",
            )
        fresh = None  # the manifest at the new address
        if not problems and (new_url or reach != addon.reach):
            try:
                fresh = await fetch_manifest(
                    new_url or addon.base_url,
                    Reach(reach),
                    resolver=self.resolver,
                    pace=self._pace,
                )
            except Busy as exc:  # not asked: its turn did not come, or it said to wait
                field_name = "manifest" if new_url else "reach"
                problem(
                    field_name,
                    f"The add-on can't be asked right now ({exc}). Try again in a moment.",
                    f"was not checked: the add-on can't be asked right now ({exc}).",
                )
            except ValueError as exc:
                field_name = "manifest" if new_url else "reach"
                problem(
                    field_name,
                    f"No add-on answers there ({exc}).",
                    f"can't be used: no add-on answers there ({exc}).",
                )
        declared = manifest.declared if manifest is not None else ()
        settings = dict(addon.settings)
        typed_secrets: set[str] = set()
        for item in declared:
            name = f"setting.{item.key}"
            stored = addon.settings.get(item.key)
            if not representable(stored):
                continue  # a list or a table from the file: no field on the form, kept
            if item.kind == "secret" or (
                item.kind in ("number", "text") and not shown(item, stored, marks)
            ):
                # A write-only row: what was typed replaces the value, and stays unshown.
                typed = form.get(name, "").strip()
                if typed and item.kind == "number":
                    try:
                        number = parse_number(typed, whole=False)
                        settings[item.key] = int(number) if float(number).is_integer() else number
                        typed_secrets.add(item.key)  # entered, unshown: kept at a move too
                        marked[item.key] = hidden_mark(settings[item.key])
                    except Invalid as exc:
                        problem(name, str(exc), "must be a number.")
                elif typed:
                    settings[item.key] = typed
                    typed_secrets.add(item.key)
                    marked[item.key] = hidden_mark(typed)
                elif form.get(name + sections.CLEAR) == "true":
                    settings.pop(item.key, None)
                    marked.pop(item.key, None)
                continue
            if name not in form:
                continue  # not on the form sent: kept
            try:
                changed, value = _addon_value(item, stored, form[name], marks)
            except _Unfit as exc:
                problem(name, exc.row, exc.notice)
                continue
            if not changed:
                continue  # only what was changed is saved: the rest stays as it was
            if value is None or setting_text(value) == (item.default or ""):
                settings.pop(item.key, None)  # the add-on's own default applies
                marked.pop(item.key, None)
            else:
                settings[item.key] = value
                marked[item.key] = mark(value)  # saved through its visible field
        # A new host must not receive the old one's secrets (keys, passwords): they go -
        # and so does whatever no manifest read says is plain - unless typed again here.
        # (What was typed into a write-only field goes along: it carries no mark, so the
        # new host's manifest cannot have it shown, whatever it declares.)
        moved = bool(new_url) and origin(new_url) != origin(addon.base_url)
        # (Plain: declared so, and a value the form shows - one it only keeps may be a secret.)
        plain = {
            item.key
            for item in declared
            if item.key in plain_keys(manifest, fresh)
            and shown(item, settings.get(item.key), marked)
        }
        dropped = sorted(set(settings) - plain - typed_secrets) if moved else []
        for key in dropped:
            settings.pop(key, None)
        marked = {key: value for key, value in marked.items() if key in settings}
        if problems:
            return await self.addons_page(
                request, session, form_for=addon.id, form=form, problems=problems, status_code=400
            )

        # The settings and the marks of which may be shown are saved together or not at all:
        # a secret typed here is never stored without its "not shown" mark. A new name and
        # the settings that name the add-on, too.
        renamed = new_name != addon.name
        naming: list[tuple[str, str]] = []

        async def also(conn: aiosqlite.Connection) -> None:
            await self._owned(session, conn)
            if marked != marks and self.saved is not None:
                await self.saved.write(
                    conn, SHOWN, {str(addon.id): marked or REMOVE}, session.username
                )
            if renamed:
                assert self.saved is not None
                naming[:] = await sections.rename_writes(
                    self.saved, conn, self.host.configured, self.host.locked,
                    addon.name, new_name, session.username,
                )  # fmt: skip

        # (What the settings that name it say is read, written and applied with no other
        # save of them under way.)
        async with contextlib.AsyncExitStack() as held:
            if renamed:
                for section in sorted({section for section, _ in sections.NAMING}):
                    await held.enter_async_context(self._section_saves.hold(section))
            try:
                await self.services.sources.update(
                    addon.id,
                    name=new_name if renamed else None,
                    base_url=new_url or None,
                    settings=settings,
                    reach=Reach(reach),
                    budget_seconds=None if keep_budget else budget,
                    clear_budget=not keep_budget and budget is None,
                    slow_after=None if keep_slow_after else slow_after,
                    clear_slow_after=not keep_slow_after and slow_after is None,
                    limits=Limits(**own),  # type: ignore[arg-type]  # (whole numbers where they are)
                    downloads=transfers["downloads"],
                    preparations=transfers["preparations"],
                    clear_downloads="downloads" in form and transfers["downloads"] is None,
                    clear_preparations=(
                        "preparations" in form and transfers["preparations"] is None
                    ),
                    capacity_group=capacity_group,
                    also=also,
                )
            except NameTaken:  # (added or renamed meanwhile)
                taken = True
            else:
                taken = False
                # (Its settings saved: perhaps its operator signed in - asked again.)
                self.services.sources.answered(addon.id)
                if renamed:  # (applied before another save of them may write)
                    await self._renamed(addon, new_name, naming)
        if taken:
            problem("name", "Another add-on has this name. Choose another.", "is taken.")
            return await self.addons_page(
                request, session, form_for=addon.id, form=form, problems=problems, status_code=400
            )
        if renamed:
            log.info(
                "dashboard: %s renamed the add-on %s to %s",
                printable(session.username),
                printable(addon.name),
                printable(new_name),
            )
        if fresh is not None:  # read a moment ago at its address: not asked for again
            self.checks.learn(await self._addon(addon.id), fresh)
        log.info(
            "dashboard: %s saved the add-on %s's settings%s",
            printable(session.username),
            printable(new_name),
            f"; its address is on another host: {len(dropped)} setting(s) removed"
            if dropped
            else "",
        )
        words = f"{addon.name} renamed to {new_name}." if renamed else ""
        if dropped:
            words += (" " if words else "") + (
                f"{new_name}'s address is on another host now. {len(dropped)} setting(s)"
                " that may be secret were removed, so they are not sent there. Enter them"
                " again below if the new address needs them."
            )
        # (Its time or limits changed: their "Advanced settings" is open on the next page.)
        before = {
            "budget_seconds": addon.budget,
            **dataclasses.asdict(addon.limits),
            "downloads": addon.downloads,
            "preparations": addon.preparations,
            "capacity_group": addon.capacity_group,
        }
        after = {
            "budget_seconds": budget if not keep_budget else addon.budget,
            **own,
            **transfers,
            "capacity_group": capacity_group,
        }
        self._remember(
            session,
            Flash(
                "addons",
                words=words,
                keys=tuple(key for key in before if before[key] != after[key]),
                addon=addon.id,
                status=Markup("Saved at {}").format(hm(time.time())),
                tone="info",
            ),
        )
        return self._see(f"/addons#addon-{addon.id}")

    async def addons_hand_back(self, request: Request, session: Session) -> Response:
        self._editable_list()
        assert self.saved is not None
        await self.saved.hand_back_addons(session.username)
        self.addons_handed_back = True
        log.info(
            "dashboard: %s handed the add-on list back to the configuration file",
            printable(session.username),
        )
        self._remember(
            session,
            Flash(
                "addons",
                words="The configuration file's add-on list applies after Shijhon restarts.",
            ),
        )
        return self._see("/addons")

    # --- library, diagnostics, appearance ---------------------------------------------------

    # --- library (fill policy, library pass, review list) ----------------------------------

    async def library_page(
        self,
        request: Request,
        session: Session,
        *,
        form: dict[str, str] | None = None,
        problems: dict[str, sections.Problem] | None = None,
        status_code: int = 200,
    ) -> Response:
        assert self.saved is not None and self.store is not None
        services = self.services
        fills = services.fills if services is not None else None
        passing = services.library_pass if services is not None else None
        saved = await self.saved.all()
        # The lists alone (the page's script: a search as it is typed, an action's
        # progress) show no notice, so none is used up by them.
        lists_only = request.query_params.get("part") == "lists"
        flash = None if lists_only else self._flash_for(session, "library")
        groups = sections.rows(
            "fill",
            self.host.configured,
            saved,
            addon_names=[],
            form=form,
            problems=problems,
            just_saved=list(flash.keys) if flash else None,
            date=day,
            locked=self.host.locked,
        )
        by_name = {row["name"]: row for _, rows in groups for row in rows}
        songs, share = by_name["auto_min_songs"], by_name["auto_min_share"]
        _, fold = sections.folded(groups, problems=problems)  # "More settings": its own form
        labels = {f.key: (f.id, f.meta.label) for f in sections.fields_of("fill")}
        text = request.query_params.get("page", "1")
        page_number = int(text) if text.isdigit() and len(text) < 7 else 1
        text = request.query_params.get("picks", "1")
        picks_page = int(text) if text.isdigit() and len(text) < 7 else 1
        text = request.query_params.get("no_match", "1")
        no_match_page = int(text) if text.isdigit() and len(text) < 7 else 1
        search = _library_search(request.query_params.get("q", ""))
        # The filled albums are listed when asked for (few look for a wrong edition).
        text = request.query_params.get("filled", "")
        filled_shown = bool(text)
        filled_page = int(text) if text.isdigit() and len(text) < 7 else 1

        def page_url(name: str, number: int) -> str:
            """The page with one list at another of its pages (the search kept, and the
            filled albums if they are shown)."""
            found = {"q": search} if search else {}
            if filled_shown and name != "filled":
                found["filled"] = str(filled_page)
            return "?" + urlencode({**found, name: str(number)})

        context: dict[str, Any] = {
            "q": search,
            "filled_shown": filled_shown,
            "filled_page": filled_page,
            "page_url": page_url,
            "batch": self.jobs.shown_batch(),
            "bulk": library.BULK,
            "fills_on": fills is not None,
            "songs": songs,
            "share": share,
            "policy_default": _policy_default(self.host.configured.fill),
            "mode": by_name["library_pass"],
            "fold": fold,
            "flash": flash,
            "notices": [
                (labels[k][0], labels[k][1], p.notice) for k, p in (problems or {}).items()
            ],
            "problem": self.problems.get("fill"),
            "form_for": (form or {}).get("form", ""),
            "jobs": [] if lists_only else self.jobs.recent(session.token_hash),
            "page": page_number,
            "picks_page": picks_page,
            "no_match_page": no_match_page,
        }
        if fills is not None:
            scope = fills.scope
            # One read of the database for the rows this page shows and for what its
            # forms stand for: each list as shown is kept for the actions on several
            # albums, and its form carries the token, never the albums themselves.
            lists = await library.read(
                self.store, scope, search, filled_page if filled_shown else None
            )
            rows, total, page_number = lists.review(page_number)
            picked, picks_total, picks_page = lists.picks(picks_page)
            unmatched, no_match_total, no_match_page = lists.no_matches(no_match_page)
            if filled_shown:
                filled, filled_total, filled_page = lists.filled()
            else:  # (counted only: the filled albums are listed when asked for)
                filled, filled_total = [], lists.filled_total
            selections = {}
            for name in library.BULK:
                listed = lists.shown(name)
                token = self.jobs.selections.issue(session.token_hash, name, listed)
                selections[name] = {"token": token, "albums": listed}
            context["selections"] = selections
            # Albums whose old release is out and whose chosen one is not in yet.
            context["replacing"] = await self._replacing(fills)
            albums: set[str] | None = None
            # The pass's own cached catalog, when there is one (not the listeners' cache);
            # nothing is asked while the catalog rests after errors.
            own = getattr(getattr(passing, "matcher", None), "catalog", None)
            describing = getattr(own, "inner", None) or (services.catalog if services else None)

            async def count() -> None:
                nonlocal albums
                # The lists alone (a search as it is typed, an action's progress every
                # two seconds) do not show the counts above them: not read.
                if not lists_only:
                    albums = await self._library_albums()

            async def describe() -> None:
                # What is not described within the wait is described in the background:
                # the next view has it. The lists alone wait a moment only, so a search
                # as it is typed is not held up by the catalog.
                if not fills.resting:
                    refs = [r for row in rows for r in row.refs]
                    refs += [r for pick in picked for r in pick.refs]
                    refs += [r for row in filled for r in row.refs]
                    await self.describer.describe(
                        describing,
                        refs,
                        wait=library.LISTS_DESCRIBE_SECONDS if lists_only else None,
                        spawn=self.host.try_spawn,
                    )

            async with anyio.create_task_group() as group:
                group.start_soon(count)
                group.start_soon(describe)
            context.update(
                page=page_number,
                counts=None
                if lists_only
                else await library.counts(self.store, scope, albums, fills.policy),
                albums=len(albums) if albums is not None else None,
                review=[
                    {
                        "row": row,
                        "options": [
                            (ref, self.describer.label(ref) or f"Release {ref.partition(':')[2]}")
                            for ref in row.refs
                        ],
                        "job": self.jobs.running(row.album_id),
                    }
                    for row in rows
                ],
                total=total,
                pages=max(1, -(-total // library.PAGE)),
                first=(page_number - 1) * library.PAGE + 1,
                last=min(total, page_number * library.PAGE),
                picks=[
                    {
                        "row": row,
                        "label": self.describer.label(row.refs[0]) if row.refs else None,
                        "options": [
                            (ref, self.describer.label(ref) or f"Release {ref.partition(':')[2]}")
                            for ref in row.refs
                        ],
                        "job": self.jobs.running(row.album_id),
                    }
                    for row in picked
                ],
                filled=[
                    {
                        "row": row,
                        "label": self.describer.label(row.refs[0]) if row.refs else None,
                        "options": [
                            (ref, self.describer.label(ref) or f"Release {ref.partition(':')[2]}")
                            for ref in row.refs
                        ],
                        "job": self.jobs.running(row.album_id),
                    }
                    for row in filled
                ],
                filled_total=filled_total,
                filled_page=filled_page,
                filled_pages=max(1, -(-filled_total // library.PAGE)),
                filled_first=(filled_page - 1) * library.PAGE + 1,
                filled_last=min(filled_total, filled_page * library.PAGE),
                picks_total=picks_total,
                picks_page=picks_page,
                picks_pages=max(1, -(-picks_total // library.PAGE)),
                picks_first=(picks_page - 1) * library.PAGE + 1,
                picks_last=min(picks_total, picks_page * library.PAGE),
                no_matches=[
                    {"row": row, "job": self.jobs.running(row.album_id)} for row in unmatched
                ],
                no_match_total=no_match_total,
                no_match_page=no_match_page,
                no_match_pages=max(1, -(-no_match_total // library.PAGE)),
                no_match_first=(no_match_page - 1) * library.PAGE + 1,
                no_match_last=min(no_match_total, no_match_page * library.PAGE),
                running=self._pass_running(passing),
                resting=_wall(fills.resting_until, fills.clock) if fills.resting else None,
                pass_mode=passing.mode if passing is not None else "",
                interval=(passing.interval_seconds / 3600) if passing is not None else 0,
                track_lists=await library.track_lists(self.store)
                if not lists_only
                and passing is not None
                and passing.mode != "off"
                and passing.track_lists is not None
                and passing.track_lists.enabled
                else None,
            )
        return await self._render(
            "_library_lists.html" if lists_only else "library.html",
            session=session,
            current="library",
            status_code=status_code,
            **context,
        )

    async def _library_albums(self) -> set[str] | None:
        """The library's owned albums (Navidrome's list, kept ten minutes; a failure half a
        minute)."""
        services = self.services
        if services is None or self.store is None:
            return None
        if self._albums is not None:
            at, found = self._albums
            if time.time() - at < (600 if found is not None else 30):
                return found
        found = await library.library_albums(services.navidrome, self.store)
        self._albums = (time.time(), found)
        return found

    def _pass_running(self, passing: Any) -> dict[str, Any] | None:
        """The library pass's run under way (its ``running`` flag): albums done and to do,
        and about how long it has left; ``moving``: its count moved in the last minutes
        (a run waiting, e.g. for the catalog's rest, does not)."""
        if passing is None or not getattr(passing, "running", False):
            return None
        done, todo = getattr(passing, "progress", (0, 0))
        if not todo or done >= todo:
            return None
        now = time.time()
        if self._progress is None or self._progress[0] != (done, todo):
            self._progress = ((done, todo), now)
        moving = now - self._progress[1] < 180
        pace = getattr(getattr(passing.matcher, "catalog", None), "per_second", None) or 1.0
        minutes = round((todo - done) * 3 / pace / 60)  # about three requests an album
        return {"done": done, "todo": todo, "minutes": max(1, minutes), "moving": moving}

    async def library_save(self, request: Request, session: Session) -> Response:
        """The fill policy, the library pass's mode, or the advanced [fill] settings."""
        form: dict[str, str] = request.state.form
        services = self.services
        passing = services.library_pass if services is not None else None
        interim: list[str] = []
        submitted, written, applied = await self._save(session, "fill", form, interim)
        if written is None:
            return await self.library_page(
                request, session, form=form, problems=submitted.problems, status_code=400
            )
        words = ""
        if "library_pass" in written.changed:
            chosen = str(getattr(submitted.candidate, "library_pass", ""))
            mode = {"off": "off", "dry_run": "a dry run", "on": "on"}[chosen]
            if "library_pass" in applied:
                words = f"The library pass is {mode} from its next run."
            elif "library_pass" in interim:
                words = (
                    "The library pass fills nothing from its next run: dry runs until Shijhon"
                    " restarts, then off. A run under way finishes as it started."
                )
            elif (
                self.running is not None
                and self.running.fill.library_pass == chosen
                and (passing is None or passing.mode == chosen)
            ):
                words = f"The library pass stays {mode}."
            else:
                words = f"The library pass becomes {mode} when Shijhon restarts."
            if chosen == "off":  # off stops the pass, not the fill policy
                words += (
                    " Off, albums that meet the fill policy are filled when they are viewed or"
                    " shown in a search or on an artist page."
                )
        self._remember(
            session,
            Flash(
                "library",
                keys=tuple(written.changed),
                status=_saved(written),
                words=words,
                part=form.get("form", ""),
                tone="info",
            ),
        )
        return self._see("/library")

    async def library_review(self, request: Request, session: Session) -> Response:
        """Fill from a chosen release, keep as is, or match again (the review list's actions)."""
        assert self.store is not None
        services = self.services
        fills = services.fills if services is not None else None
        form: dict[str, str] = request.state.form
        album_id = request.path_params["album"]
        if form.get("list") == "picked":
            back = _library_back(form, "picks", "picked")
        else:
            back = _library_back(form, "page", "review")
        if fills is None:
            raise Refused(PlainTextResponse("Filling albums is off.\n", 409, headers=HEADERS))

        def note(words: str, tone: str = "info") -> Response:
            self._remember(session, Flash("library", words=words, tone=tone))
            return self._see(back)

        settled = await self.settle_review(
            fills,
            album_id,
            form.get("action", ""),
            form.get("release", ""),
            printable(session.username),
        )
        return note(settled.words, settled.tone)

    async def settle_review(
        self,
        fills: Any,
        album_id: str,
        action: str,
        ref: str,
        who: str,
        *,
        authorize: Callable[[], Awaitable[None]] | None = None,
    ) -> library.Settled:
        """One album of the review list filled from a named release, kept as it is, or
        matched again: the Library page's action, and an administrator's agent's. A fill
        and a match run in the background (``Settled.job``); a keep is done at once.
        The action for several albums runs the same ``_fill``, ``_keep`` and ``_match``.
        ``who``: for the log. ``authorize`` (an agent's): asked again before the
        background work starts, where files are written, and after every wait immediately
        before a decision is deleted or recorded (``Fills.guard_decisions``); it refuses
        with a ``MaterializeError`` whose reason says why, and the action then ends with
        it, leaving that decision as it was."""
        services = self.services
        found = await self._review_row(fills, album_id)
        if found is None:
            return library.Settled("That album is no longer in the review list.", state="gone")
        name = f"{found['artist'] or 'Unknown artist'} \u2013 {found['title'] or 'Unknown album'}"
        if action not in ("fill", "keep", "rematch"):
            return library.Settled("Choose Fill, Keep as is or Match again.", "error")
        if action == "rematch" and fills.resting:
            until = when(_wall(fills.resting_until, fills.clock))
            return library.Settled(
                Markup(
                    "{} is not matched again now: the catalog is resting after errors."
                    " Try again after {}."
                ).format(name, until),
                "error",
                "resting",
            )
        refs = [found["release_ref"]] if found["release_ref"] else []
        refs += [r for r in (found["candidates"] or "").split(",") if r and r not in refs]
        if action == "fill":
            if library.is_part(found["reason"] or ""):
                return library.Settled(
                    f"{name} is part of another album's release: it is not filled on its own.",
                    "error",
                    "refused",
                )
            if ref not in refs:
                return library.Settled(
                    f"{name}: choose one of the listed releases.", "error", "refused"
                )
        # Claimed before anything is awaited: one action at a time for an album.
        job = self.jobs.claim(album_id, name, action)
        if job is None:
            return library.Settled(
                f"{name} is busy with the last action; wait for its result.", state="busy"
            )

        @contextlib.contextmanager
        def guarded() -> Iterator[None]:
            """An agent's action, carried out only while it may still act: asked again
            where files are written (the engine) and immediately before a decision is
            deleted or recorded (the fills), after every wait."""
            if authorize is None or services is None:
                yield
                return
            with services.engine.guard_writes(authorize), fills.guard_decisions(authorize):
                yield

        if action == "keep":
            try:
                with guarded():
                    await self._keep(fills, job)
            finally:
                self.jobs.drop(job)  # answered here: nothing to show later
            if not job.ok:
                return library.Settled(job.message, "error", "refused")
            log.info("dashboard: %s kept %s as it is", who, printable(name))
            return library.Settled(
                f"{name} is kept as it is: it is not matched again in this catalog.", "ok", "kept"
            )

        async def work() -> None:
            """The action in the background; an agent's is asked about before it starts too."""
            if authorize is not None:
                try:
                    await authorize()
                except MaterializeError as exc:
                    self.jobs.finish(job, ok=False, message=f"{name}: {exc.reason}; nothing done.")
                    return
            with guarded():
                if action == "fill":
                    await self._fill(fills, job, ref, label)
                else:
                    await self._match(fills, job)

        label = self.describer.label(ref) or ref
        if action == "fill":
            log.info("dashboard: %s chose %s for %s", who, ref, printable(name))
            words = (
                f"Filling {name} from {label}. This can take a minute; reload to see how it went."
            )
        else:
            log.info("dashboard: %s asked to match %s again", who, printable(name))
            words = f"Matching {name} again. Reload to see the result."
        if not self.host.try_spawn(work):
            self.jobs.finish(job, ok=False, message=f"{name}: Shijhon is stopping; nothing done.")
            return library.Settled(
                f"{name}: Shijhon is stopping; nothing done.", "error", "stopping"
            )
        return library.Settled(words, state="started", job=job)

    async def review_albums(self, fills: Any, page: int) -> dict[str, Any]:
        """One page of the review list as the Library page shows it, in plain values for an
        administrator's agent: each album, why it needs a decision, and the releases
        offered (the chosen one first), described as the page describes them."""
        assert self.store is not None
        services = self.services
        # The page's own read of its lists (without a search): one state of the database.
        rows, total, page = (await library.read(self.store, fills.scope)).review(page)
        if not fills.resting:  # (nothing is asked while the catalog rests after errors)
            passing = services.library_pass if services is not None else None
            own = getattr(getattr(passing, "matcher", None), "catalog", None)
            describing = getattr(own, "inner", None) or (services.catalog if services else None)
            await self.describer.describe(describing, [ref for row in rows for ref in row.refs])
        albums = []
        for row in rows:
            job = self.jobs.running(row.album_id)
            albums.append(
                {
                    "album_id": row.album_id,
                    "title": row.title,
                    "artist": row.artist,
                    "owned_songs": row.owned,
                    "release_tracks": row.tracks,
                    "why": row.headline,
                    "detail": row.sentence,
                    "part_of_another_album": row.part,
                    "from_a_dry_run": row.planned,
                    "releases": [
                        {"release": ref, "description": self.describer.label(ref) or ""}
                        for ref in row.refs
                    ],
                    **({"action_running": job.action} if job is not None else {}),
                }
            )
        return {
            "albums": albums,
            "total": total,
            "page": page,
            "pages": max(1, -(-total // library.PAGE)),
        }

    async def server_status(self) -> dict[str, Any]:
        """What Diagnostics says that helps tell a person what needs them, in plain values
        for an administrator's agent. Read-only. No address (but an add-on's sign-in page,
        when that is a plain page address), no token, no setting's value, nothing of any
        account. The checks are Diagnostics' own, at their pace; what does not answer
        within a few seconds is reported as not known."""
        assert self.store is not None and self.saved is not None
        services, store = self.services, self.store
        if services is None:
            raise Refused(PlainTextResponse("Shijhon is starting.\n", 503, headers=HEADERS))
        addons = await services.sources.stored()
        found: dict[str, Any] = {}

        async def navidrome() -> None:
            found["navidrome"] = await status.navidrome_status(services.navidrome)

        async def manifests() -> None:
            await self.checks.check(addons)

        async def catalog() -> None:
            assert self.saved is not None
            found["catalog"] = await self._catalog_status(await self.saved.all())

        with anyio.move_on_after(STATUS_SECONDS):
            async with anyio.create_task_group() as group:
                for work in (navidrome, manifests, catalog):
                    group.start_soon(work)

        def at(moment: float | None) -> str | None:
            return instant(moment) if moment else None

        nd: status.NavidromeStatus | None = found.get("navidrome")
        attention: list[str] = []
        if nd is None:
            navidrome_part: dict[str, Any] = {"state": "not_known"}
        elif not nd.answering:
            navidrome_part = {"state": "not_answering"}
            attention.append("Navidrome does not answer.")
        else:
            navidrome_part = {
                "state": "answering",
                "songs": nd.songs,
                "albums": nd.albums,
                "last_scan": at(nd.last_scan),
            }
        info = found.get("catalog")
        if info is None:
            catalog_part: dict[str, Any] = {"state": "not_known"}
        elif not info["running"] and info["kind"] == "none":
            catalog_part = {"state": "not_chosen"}
        elif not info["running"]:
            catalog_part = {"state": "not_running"}
            attention.append(
                "The catalog is not running: the Catalog page says what it is missing."
                if info["problem"] is not None
                else "The catalog is not running until Shijhon restarts."
            )
        else:
            check = info["check"]
            resting = info["resting"]
            catalog_part = {
                "state": "resting" if resting else "ok" if check is None or check.ok else "failing",
                "checked_at": at(check.at) if check is not None else None,
                "resting_until": at(resting),
            }
            if check is not None and not check.ok:
                # The kind of failure only: a reason is the adapter's own text.
                catalog_part["error"] = (check.error or "").split(":", 1)[0][:40]
                attention.append("The catalog's last check failed.")
            elif resting:
                attention.append("The catalog is resting after errors.")
        listed = []
        for addon in addons:
            health = self._health(addon)
            item: dict[str, Any] = {"name": addon.name, "state": health["state"]}
            if health["state"] == "needs_sign_in":
                page = _plain_page(health["link"])
                if page is not None:
                    item["sign_in_page"] = page
                attention.append(f"The add-on {addon.name} needs its operator to sign in.")
            elif health["state"] in ("failing", "manifest_unreachable"):
                attention.append(f"The add-on {addon.name} is not working.")
            listed.append(item)
        fills = services.fills
        review = await library.review_count(store, fills.scope) if fills is not None else 0
        if review:
            attention.append(
                f"{review:,} album{'s' if review != 1 else ''} wait in the review list."
            )
        cleanup = services.cleanup
        running = self.running or self.host.configured
        swept = cleanup.last if cleanup is not None else None
        failed = cleanup.failed if cleanup is not None else None
        cleanup_part: dict[str, Any] = {
            "mode": cleanup.mode if cleanup is not None else running.cleanup.mode,
            "last_check": at(swept.listed.at)
            if swept is not None and swept.listed is not None
            else None,
            "next_check": at(cleanup.next_at) if cleanup is not None else None,
        }
        if swept is not None:
            cleanup_part.update(
                releases_taken_out=len(swept.removed),
                failed=swept.failed,
                stopped=bool(swept.stopped or swept.refused),
            )
        checked = swept.listed.at if swept is not None and swept.listed is not None else 0.0
        if failed and failed[0] > checked:  # (a check since then went through)
            cleanup_part["last_failure"] = at(failed[0])
            attention.append("The cleanup's last check failed: the Cleanup page says more.")
        pending = await self._pending()
        if pending:
            attention.append("Saved settings wait for a restart of Shijhon.")
        return {
            "versions": {
                "shijhon": __version__,
                "navidrome": nd.version if nd is not None else None,
                "navidrome_tested": status.PINNED_NAVIDROME,
                "database_schema": await status.schema(store),
                "database_schema_expected": status.bundled_schema(),
            },
            "started": at(self.started_at),
            "navidrome": navidrome_part,
            "catalog": catalog_part,
            "addons": listed,
            "review_list": {"albums": review},
            "cleanup": cleanup_part,
            "restart": {
                "pending": bool(pending),
                # The settings' names as their pages show them: never their values.
                "settings": [str(item["label"]) for item in pending][:50],
            },
            "needs_attention": attention,
        }

    async def library_recheck(self, request: Request, session: Session) -> Response:
        """Match one current negative result again, without adding files."""
        services = self.services
        fills = services.fills if services is not None else None
        form: dict[str, str] = request.state.form
        album_id = request.path_params["album"]
        back = _library_back(form, "no_match", "no-match")
        if fills is None:
            raise Refused(PlainTextResponse("Filling albums is off.\n", 409, headers=HEADERS))

        def note(words: str, tone: str = "info") -> Response:
            self._remember(session, Flash("library", words=words, tone=tone))
            return self._see(back)

        found = await self._negative_row(fills, album_id)
        if found is None:
            return note("That album's match has changed or it is no longer in No match.")
        title = found["title"] or f"Unknown album ({album_id})"
        name = f"{found['artist'] or 'Unknown artist'} \u2013 {title}"
        if form.get("action") != "recheck":
            return note("Choose Match again.", "error")
        if fills.resting:
            until = when(_wall(fills.resting_until, fills.clock))
            return note(
                Markup(
                    "{} is not matched again now: the catalog is resting after errors."
                    " Try again after {}."
                ).format(name, until),
                "error",
            )
        job = self.jobs.claim(album_id, name, "recheck")
        if job is None:
            return note(f"{name} is busy with the last action; wait for its result.")
        log.info("dashboard: %s asked to recheck %s", printable(session.username), printable(name))
        if not self.host.try_spawn(lambda: self._recheck(fills, job)):
            words = f"{name}: Shijhon is stopping; nothing done."
            self.jobs.finish(job, ok=False, message=words)
            return note(words, "error")
        return note(f"Matching {name} again. No files are added. Reload to see the result.")

    async def library_change(self, request: Request, session: Session) -> Response:
        """Fill a filled album from another of the releases its match listed (the wrong
        edition was filled): in the background."""
        assert self.store is not None
        services = self.services
        fills = services.fills if services is not None else None
        cleanup = services.cleanup if services is not None else None
        form: dict[str, str] = request.state.form
        album_id = request.path_params["album"]
        if form.get("list") == "picked":
            back = _library_back(form, "picks", "picked")
        else:
            back = _library_back(form, "filled", "filled")
        if fills is None or cleanup is None:
            raise Refused(PlainTextResponse("Filling albums is off.\n", 409, headers=HEADERS))

        def note(words: str, tone: str = "info") -> Response:
            self._remember(session, Flash("library", words=words, tone=tone))
            return self._see(back)

        found = await self._filled_row(fills, album_id)
        if found is None:
            return note("That album is no longer filled.")
        name = f"{found['artist'] or 'Unknown artist'} \u2013 {found['title'] or 'Unknown album'}"
        refs = [found["release_ref"], *(found["candidates"] or "").split(",")]
        ref = form.get("release", "")
        if not ref or ref not in refs or ref == found["filled_ref"]:
            return note(f"{name}: choose one of the other listed releases.", "error")
        job = self.jobs.claim(album_id, name, "change")
        if job is None:
            return note(f"{name} is busy with the last action; wait for its result.")
        label = self.describer.label(ref) or ref
        log.info(
            "dashboard: %s chose %s for %s in place of %s",
            printable(session.username),
            ref,
            printable(name),
            found["filled_ref"],
        )
        if not self.host.try_spawn(lambda: self._change(fills, cleanup, job, ref, label)):
            self.jobs.finish(job, ok=False, message=f"{name}: Shijhon is stopping; nothing done.")
            return note(f"{name}: Shijhon is stopping; nothing done.", "error")
        return note(
            f"Filling {name} from {label} instead. This can take a few minutes; reload to see"
            " how it went."
        )

    async def _replacing(self, fills: Any) -> list[dict[str, Any]]:
        """The release changes left unfinished (a fill that failed, a stop), for the page:
        the album, why, and whether Shijhon tries again by itself."""
        assert self.store is not None
        found = []
        for left in await fills.replacements.all_unfinished():
            if self.jobs.running(left.album_id) is not None:
                continue  # (under way now: its own notice follows)
            row = await self.store.fetchone(
                "SELECT title, artist FROM album_matches WHERE album_id = ? AND scope = ?",
                [left.album_id, fills.scope],
            )
            artist = (row["artist"] if row else "") or "Unknown artist"
            title = (row["title"] if row else "") or "Unknown album"
            found.append(
                {
                    "name": f"{artist} \u2013 {title}",
                    "release": self.describer.label(left.new_ref) or left.new_ref,
                    "reason": left.reason,
                    "again": left.retry_at is not None,
                }
            )
        return found

    async def _filled_row(self, fills: Any, album_id: str) -> Any:
        """The album's match and the release it is filled from, while it is filled."""
        assert self.store is not None
        return await self.store.fetchone(
            "SELECT m.title, m.artist, m.candidates, m.release_ref, r.ref AS filled_ref"
            " FROM album_matches m"
            " JOIN releases r ON r.owned_album_id = m.album_id"
            " WHERE m.album_id = ? AND m.scope = ? AND m.outcome IN ('filled', 'deferred')",
            [album_id, fills.scope],
        )

    async def _change(
        self, fills: Any, cleanup: Any, job: library.Job, ref: str, label: str
    ) -> None:
        """One library write at a time, as a fill (the second must see the first's result)."""

        def songs(count: int) -> str:
            return f"{count} song{'s' if count != 1 else ''}"

        try:
            async with self.jobs.one_fill:
                if await self._filled_row(fills, job.album_id) is None:
                    self.jobs.finish(job, ok=False, message=f"{job.title} is no longer filled.")
                    return
                decision = await fills.change(job.album_id, ref, cleanup.take_out_now)
        except InUse as exc:
            self.jobs.finish(
                job,
                ok=False,
                message=f"{job.title} stays as it is: songs its fill added are in use"
                f" ({', '.join(exc.uses)}). Nothing was changed.",
            )
            return
        except ChoiceRefused as exc:
            self.jobs.finish(job, ok=False, message=f"{job.title} was not changed: {exc.reason}.")
            return
        except FillPending as exc:
            self.jobs.finish(
                job,
                ok=False,
                message=f"{job.title}: the {songs(exc.removed)} its old release added"
                f" {'was' if exc.removed == 1 else 'were'} taken out, but it was not filled"
                f" from {label} now ({exc.reason}). It is shown complete with {label}, and"
                " Shijhon tries the fill again by itself, also after a restart. If that keeps"
                " failing, the album is filled when one of its songs is next used.",
            )
            return
        except (CatalogError, NavidromeError, MaterializeError, NotFresh) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            self.jobs.finish(job, ok=False, message=f"{job.title} was not changed: {reason}.")
            return
        except BaseException as exc:
            message = f"{job.title} was not changed ({type(exc).__name__})."
            self.jobs.finish(job, ok=False, message=message)
            if not isinstance(exc, Exception):
                raise  # canceled (Shijhon stopping): the job is finished all the same
            log.exception("dashboard: changing the release of %s failed", printable(job.title))
            return
        went = f"the {songs(decision.removed)} its old release added"
        went += " was taken out" if decision.removed == 1 else " were taken out"
        if decision.outcome == "filled":
            self.jobs.finish(
                job,
                ok=True,
                message=f"{job.title} is filled from {label} now: {went}, {decision.added} added.",
            )
        elif decision.outcome == "complete":
            self.jobs.finish(
                job,
                ok=True,
                message=f"{job.title}: {went}. {label} adds nothing; the album is complete.",
            )
        else:
            why = (
                f"{label} was added to the library as an album of its own meanwhile"
                if decision.reason == IN_LIBRARY
                else f"the songs of {label} did not join the album"
            )
            self.jobs.finish(
                job,
                ok=False,
                message=f"{job.title}: {went}, but {why}; it is in the review list.",
            )

    async def library_bulk(self, request: Request, session: Session) -> Response:
        """One action for the albums selected in a list - those ticked, or all the list
        holds under the search: started in the background, one album after another."""
        assert self.store is not None
        services = self.services
        fills = services.fills if services is not None else None
        form: dict[str, str] = request.state.form
        name = form.get("list", "")
        if fills is None:
            raise Refused(PlainTextResponse("Filling albums is off.\n", 409, headers=HEADERS))
        if name not in library.BULK:
            raise Refused(PlainTextResponse("No such list.\n", 400, headers=HEADERS))
        pages, anchor = {"review": "page", "picked": "picks", "none": "no_match"}[name], name
        back = _library_back(form, pages, "no-match" if anchor == "none" else anchor)

        def note(words: str, tone: str = "info") -> Response:
            self._remember(session, Flash("library", words=words, tone=tone))
            return self._see(back)

        action = form.get("action", "")
        if action not in library.BULK[name]:
            return note("Choose one of the actions for the selected albums.", "error")
        # What the page showed, by the token its form carries: never the list as it is now.
        selection = self.jobs.selections.find(session.token_hash, name, form.get("selection", ""))
        if selection == library.USED:
            return note(
                "That selection was used already: its action was started before, and nothing"
                " was started again. Select the albums anew.",
            )
        if isinstance(selection, str):
            return note(
                "The page was loaded too long ago to act on its lists. It is loaded anew:"
                " select the albums again.",
                "error",
            )
        listed = list(selection.albums)
        if form.get("all") != "1":  # those ticked, in the list's order
            ticked = {key[len("album:") :] for key in form if key.startswith("album:")}
            listed = [album_id for album_id in listed if album_id in ticked]
        if not listed:
            return note("Select the albums first.", "error")
        if action in ("rematch", "recheck") and fills.resting:
            until = when(_wall(fills.resting_until, fills.clock))
            return note(
                "Nothing is matched again now: the catalog is resting after errors. Try again"
                f" after {until}.",
                "error",
            )
        batch = self.jobs.begin(action, selection, listed)
        if batch is None:
            return note("Another action for several albums is running; wait for it or stop it.")
        who = printable(session.username)
        log.info("dashboard: %s started %s for %d album(s)", who, action, len(listed))
        if not self.host.try_spawn(lambda: self._bulk(fills, batch)):
            batch.stopped, batch.finished = "Shijhon is stopping", self.jobs.clock()
            return note("Shijhon is stopping; nothing done.", "error")
        return self._see(back)

    async def library_bulk_stop(self, request: Request, session: Session) -> Response:
        """Stop the action for several albums after the album under way."""
        form: dict[str, str] = request.state.form
        batch = self.jobs.batch
        if batch is not None and batch.finished is None:
            batch.stopping = True
            log.info("dashboard: %s stopped the action for several albums",
                     printable(session.username))  # fmt: skip
        return self._see(_library_back(form, "page", "albums"))

    async def _bulk(self, fills: Any, batch: library.Batch) -> None:
        """The batch's albums, one after another - each through the action it would get
        alone, with its own checks under its own locks, and only while its match is still
        as the page showed it."""
        try:
            for album_id in batch.albums:
                if batch.stopping:
                    batch.stopped = "you stopped it"
                    break
                if batch.action in ("rematch", "recheck") and fills.resting:
                    batch.stopped = "the catalog is resting after errors"
                    break
                await self._bulk_one(fills, batch, album_id)
        except Exception as exc:
            batch.stopped = f"an unexpected error ({type(exc).__name__})"
            log.exception("dashboard: the action for several albums failed")
        except BaseException:  # canceled (Shijhon stopping): it ends here all the same
            batch.stopped = "Shijhon is stopping"
            raise
        finally:
            batch.current = ""
            batch.finished = self.jobs.clock()
            log.info("dashboard: %s", batch.summary())

    async def _bulk_one(self, fills: Any, batch: library.Batch, album_id: str) -> None:
        assert self.store is not None
        name, action, shown = batch.name, batch.action, batch.shown[album_id]

        async def listed() -> Any:
            if name == "none":
                return await self._negative_row(fills, album_id)
            return await self._review_row(fills, album_id)

        found = await self.store.fetchone(
            "SELECT title, artist FROM album_matches WHERE album_id = ? AND scope = ?",
            [album_id, fills.scope],
        )
        if found is None:  # (no match at all any more)
            batch.note("changed")
            return
        title = found["title"] or (f"Unknown album ({album_id})" if name == "none" else "")
        title = f"{found['artist'] or 'Unknown artist'} \u2013 {title or 'Unknown album'}"
        ref = shown.release  # the release the page offered, never one the match took since
        if action == "fill" and not ref:
            batch.note("unchosen")
            return
        batch.current = title
        job = self.jobs.claim(album_id, title, action)
        if job is None:
            batch.note("failed", f"{title} is busy with another action.")
            return
        # Each action checks, under its own locks, that the match is still as shown.
        try:
            if action == "fill":
                label = self.describer.label(ref) or ref
                await self._fill(fills, job, ref, label, expected=shown.state)
            elif action == "keep":
                await self._keep(fills, job, expected=shown.state)
            elif action == "rematch":
                await self._match(fills, job, expected=shown.state)
            else:
                await self._recheck(fills, job, expected=shown.state)
        finally:
            self.jobs.drop(job)  # the batch says how it went: no notice of its own
        if job.changed:
            batch.note("changed")
        elif job.ok:
            batch.note("ok")
        elif action in ("rematch", "recheck") and await listed() is not None:
            batch.note("same")  # matched again with the result it had
        else:
            batch.note("failed", job.message or f"{title}: nothing done.")

    async def _negative_row(self, fills: Any, album_id: str) -> Any:
        """The current scope's negative result; other outcomes never use this action."""
        assert self.store is not None
        return await self.store.fetchone(
            "SELECT title, artist FROM album_matches"
            " WHERE album_id = ? AND scope = ? AND outcome = 'none'",
            [album_id, fills.scope],
        )

    async def _recheck(self, fills: Any, job: library.Job, *, expected: str | None = None) -> None:
        """Check eligibility again in the job; Fills guards the rematch atomically.
        ``expected``: the match's state as the page showed it (an action for several
        albums) - checked under the action's locks, and a match changed since is left."""
        changed = f"{job.title}: the match has changed. Reload to see its current result."
        try:
            if await self._negative_row(fills, job.album_id) is None:
                job.changed = expected is not None
                self.jobs.finish(job, ok=False, message=changed)
                return
            if expected is None:
                decision = await fills.recheck(job.album_id)
            else:
                decision = await fills.recheck(job.album_id, expected=expected)
            if decision is None:
                reason = (
                    "the catalog is resting" if fills.resting else "it could not be checked now"
                )
                self.jobs.finish(
                    job,
                    ok=False,
                    message=f"{job.title} was not matched: {reason}. Reload and try again.",
                )
                return
            await self._match_result(fills, job)
        except ChoiceRefused as exc:
            job.changed = isinstance(exc, Changed)
            self.jobs.finish(job, ok=False, message=changed)
        except BaseException as exc:
            self.jobs.finish(
                job, ok=False, message=f"{job.title} could not be matched ({type(exc).__name__})."
            )
            if not isinstance(exc, Exception):
                raise
            log.exception("dashboard: rechecking %s failed", printable(job.title))

    async def _review_row(self, fills: Any, album_id: str) -> Any:
        """The album's row in the review list: in review, or matched by an automatic pick
        and not filled yet."""
        assert self.store is not None
        return await self.store.fetchone(
            "SELECT title, artist, reason, candidates, release_ref FROM album_matches"
            " WHERE album_id = ? AND scope = ? AND (outcome = 'review' OR (picked != ''"
            " AND outcome IN ('filled', 'deferred') AND album_id NOT IN"
            " (SELECT owned_album_id FROM releases WHERE owned_album_id IS NOT NULL)))",
            [album_id, fills.scope],
        )

    async def _keep(self, fills: Any, job: library.Job, *, expected: str | None = None) -> None:
        """Keep the album as it is; the job says how it went (``ok``, or its ``message``:
        why not). ``expected``: the match's state as the page showed it (an action for
        several albums); a match changed since is left (``changed``)."""
        try:
            await fills.keep(job.album_id, expected=expected)
        except Changed:
            job.changed = True
            job.message = f"{job.title} was not kept as it is: its match has changed."
        except (ChoiceRefused, NavidromeError) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            job.message = f"{job.title} could not be kept as it is: {reason}."
        else:
            job.ok = True

    async def _fill(
        self, fills: Any, job: library.Job, ref: str, label: str, *, expected: str | None = None
    ) -> None:
        """One fill at a time (two albums may list the same release: the second must see
        the first's result). ``expected``: the match's state as the page showed it (an
        action for several albums); a match changed since is not filled."""
        try:
            async with self.jobs.one_fill:
                if await self._review_row(fills, job.album_id) is None:
                    job.changed = expected is not None
                    self.jobs.finish(
                        job, ok=False, message=f"{job.title} is no longer in the review list."
                    )
                    return
                decision = await fills.choose(job.album_id, ref, expected=expected)
        except Changed as exc:
            job.changed = True
            self.jobs.finish(job, ok=False, message=f"{job.title} was not filled: {exc.reason}.")
            return
        except ChoiceRefused as exc:
            self.jobs.finish(job, ok=False, message=f"{job.title} was not filled: {exc.reason}.")
            return
        except (CatalogError, NavidromeError, MaterializeError) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            self.jobs.finish(job, ok=False, message=f"{job.title} was not filled: {reason}.")
            return
        except BaseException as exc:
            message = f"{job.title} was not filled ({type(exc).__name__})."
            self.jobs.finish(job, ok=False, message=message)
            if not isinstance(exc, Exception):
                raise  # canceled (Shijhon stopping): the job is finished all the same
            log.exception("dashboard: filling %s failed", printable(job.title))
            return
        if decision.outcome == "filled":
            added = decision.added
            self.jobs.finish(
                job,
                ok=True,
                message=f"{job.title} filled from {label}: {added} song{'s' if added != 1 else ''}"
                " added.",
            )
        elif decision.outcome == "complete":
            message = f"{job.title}: {label} adds nothing; it is complete."
            self.jobs.finish(job, ok=True, message=message)
        else:
            self.jobs.finish(
                job,
                ok=False,
                message=f"{job.title}: the added songs did not join the album; it stays in the"
                " review list.",
            )

    async def _match(self, fills: Any, job: library.Job, *, expected: str | None = None) -> None:
        """Forget the album's match and match it again at the pass's pace, without filling;
        the result is read from the album's own row (the match of a release shared by
        several owned albums may record the result on another one). ``expected``: the
        match's state as the page showed it (an action for several albums); a match
        changed since is kept."""
        services = self.services
        paced = services.library_pass.matcher if services and services.library_pass else None
        try:
            try:
                await fills.rematch(job.album_id, expected=expected)
            except Changed as exc:
                job.changed = True
                message = f"{job.title} was not matched again: {exc.reason}."
                self.jobs.finish(job, ok=False, message=message)
                return
            await fills.fill(job.album_id, hold=True, matcher=paced)
            await self._match_result(fills, job)
        except ChoiceRefused as exc:  # (an agent's action it may no longer have carried out)
            message = f"{job.title} was not matched again: {exc.reason}."
            self.jobs.finish(job, ok=False, message=message)
        except BaseException as exc:
            self.jobs.finish(
                job, ok=False, message=f"{job.title} could not be matched ({type(exc).__name__})."
            )
            if not isinstance(exc, Exception):
                raise
            log.exception("dashboard: matching %s again failed", printable(job.title))
            return

    async def _match_result(self, fills: Any, job: library.Job) -> None:
        """Report the album's own saved result, including when a group match moved it."""
        assert self.store is not None
        row = await self.store.fetchone(
            "SELECT outcome, reason, picked FROM album_matches WHERE album_id = ? AND scope = ?",
            [job.album_id, fills.scope],
        )
        outcome, reason = (row["outcome"], row["reason"] or "") if row else (None, "")
        if outcome is None:
            if fills.resting:
                words = (
                    f"{job.title} will be matched when it is viewed or at the library pass's"
                    " next run: the catalog is resting."
                )
            else:
                words = f"{job.title} was not matched: it is filled or no longer in the library."
            self.jobs.finish(job, ok=outcome is None and fills.resting, message=words)
            return
        if outcome in ("deferred", "filled"):
            picked = row is not None and row["picked"]
            how = "the most likely release is picked" if picked else "it matches a release"
            words, ok = (
                f"{job.title}: {how} now. It is shown complete, and filled automatically or"
                " when used, as the fill policy says.",
                True,
            )
        elif outcome == "review":
            headline, _, _ = library.explain(reason)
            words, ok = f"{job.title} still needs review: {headline.lower()}.", False
        elif outcome == "complete":
            words, ok = f"{job.title} is complete as it is.", True
        elif outcome == "none":
            words, ok = f"No release matches {job.title}.", False
        elif outcome == "kept":
            words, ok = f"{job.title} is kept as it is.", True
        else:
            words, ok = f"{job.title} could not be matched: {reason}.", False
        self.jobs.finish(job, ok=ok, message=words)

    # --- cleanup (placeholders by origin, the cleanup, downloaded audio) -------------------

    async def cleanup_page(
        self,
        request: Request,
        session: Session,
        *,
        section: str = "",
        form: dict[str, str] | None = None,
        problems: dict[str, sections.Problem] | None = None,
        confirm: cleaning.Preview | None = None,
        nonce: str = "",
        switching: bool = True,
        status_code: int = 200,
    ) -> Response:
        """A cheap view: Shijhon's own records and the last check's result; it never
        reads Navidrome's database."""
        assert self.saved is not None and self.store is not None
        services = self.services
        cleanup = services.cleanup if services is not None else None
        saved = await self.saved.all()
        flash = self._flash_for(session, "cleanup")
        just_saved = list(flash.keys) if flash else None

        def rows(of: str) -> list[tuple[Any, list[dict[str, Any]]]]:
            mine = section == of
            return sections.rows(
                of,
                self.host.configured,
                saved,
                addon_names=[],
                form=form if mine else None,
                problems=problems if mine else None,
                just_saved=just_saved if flash and flash.part == of else None,
                date=day,
                locked=self.host.locked,
            )

        downloads = [
            row for _, group in rows("delivery") for row in group if row["name"] in DOWNLOAD_LIMITS
        ]
        labels = {f.key: (f.id, f.meta.label) for f in sections.fields_of(section or "cleanup")}
        # The newer of the daily check and the dashboard's dry run is listed; the daily
        # check's outcome stays said when a dry run is newer.
        daily = cleanup.last if cleanup is not None else None
        manual = cleanup.last_dry_run if cleanup is not None else None
        swept = daily
        if (
            manual is not None
            and manual.listed is not None
            and (daily is None or daily.listed is None or manual.listed.at > daily.listed.at)
        ):
            swept = manual
        listed = cleaning.rows(swept) if swept is not None else []
        text = request.query_params.get("page", "1")
        shown, page_number, pages = cleaning.page_of(
            listed, int(text) if text.isdigit() and len(text) < 7 else 1
        )
        expiry = services.download_first.expiry if services is not None else None
        running = self.running or self.host.configured
        context: dict[str, Any] = {
            "flash": flash,
            "notices": [
                (labels[k][0], labels[k][1], p.notice)
                for k, p in (problems or {}).items()
                if k in labels
            ],
            "form_for": section,
            "problem": self.problems.get("cleanup"),
            "overview": await cleaning.overview(self.store),
            "cleanup": cleanup,
            "can_list": cleanup is not None and cleanup.can_list,
            "mode": cleanup.mode if cleanup is not None else running.cleanup.mode,
            "swept": swept,
            "listing": swept.listed if swept is not None else None,
            "daily": daily if swept is not daily else None,
            "daily_counts": cleaning.counts(cleaning.rows(daily)) if daily is not None else None,
            "failed": cleanup.failed if cleanup is not None else None,
            "nonce": nonce,
            "switching": switching,
            "delivery_problem": self.problems.get("delivery"),
            "listed": shown,
            "counts": cleaning.counts(listed),
            "total": len(listed),
            "page": page_number,
            "pages": pages,
            "first": (page_number - 1) * cleaning.PAGE + 1,
            "last": min(len(listed), page_number * cleaning.PAGE),
            "next_dry_run": cleaning.next_dry_run(cleanup) if cleanup is not None else None,
            "settings": [row for _, group in rows("cleanup") for row in group],
            "downloads": downloads,
            "kept_gb": (
                round(expiry.kept_bytes / 2**30, 2)
                if expiry is not None and expiry.kept_bytes is not None
                else None
            ),
            "limits": running.delivery,
            "confirm": confirm,
            "confirm_form": [
                (k, v) for k, v in (form or {}).items() if k not in ("csrf", "confirm")
            ]
            if confirm is not None
            else [],
        }
        return await self._render(
            "cleanup.html", session=session, current="cleanup", status_code=status_code, **context
        )

    async def cleanup_save(self, request: Request, session: Session) -> Response:
        """The cleanup's settings, or the downloaded audio's limits. A change that lets the
        cleanup take out more - switching it on, or while it is on fewer days or more kinds
        of release - first shows what its next check would take out (a dry run with the
        settings sent), and is saved only when that is confirmed: by this session, for
        those settings, the dry run done."""
        assert self.saved is not None
        form: dict[str, str] = request.state.form
        section = "cleanup" if request.url.path.endswith("/settings") else "delivery"
        if section == "delivery":  # this page's delivery settings only (and their removal)
            form = {
                k: v
                for k, v in form.items()
                if k.removesuffix(sections.CLEAR) in DOWNLOAD_LIMITS or k == "csrf"
            }
            submitted, written, _ = await self._save(session, section, form, whole=False)
            return await self._cleanup_saved(request, session, section, form, submitted, written)
        async with self._cleanup_saves:  # the check and the save together
            asked, key, switching = await self._cleanup_asks(session, form)
            if asked is None:
                submitted, written, _ = await self._save(session, section, form)
        if asked is not None:
            # A dry run with the settings sent - outside the lock: switching the cleanup
            # off never waits behind one - then its confirmation, bound to them.
            preview = await self._cleanup_preview(asked)
            nonce = ""
            if preview.listing is not None:  # only a dry run shown can be confirmed
                nonce = os.urandom(16).hex()
                shown = min(time.time(), preview.listing.at)  # (one kept a few minutes)
                self._confirmations[session.token_hash] = (nonce, key, shown)
                if len(self._confirmations) > 1000:
                    self._confirmations.pop(next(iter(self._confirmations)))
            return await self.cleanup_page(
                request, session, section="cleanup", form=form, confirm=preview, nonce=nonce,
                switching=switching,
            )  # fmt: skip
        return await self._cleanup_saved(request, session, section, form, submitted, written)

    async def _cleanup_asks(
        self, session: Session, form: dict[str, str]
    ) -> tuple[Any, tuple[Any, ...], bool]:
        """(the settings sent, when they let the cleanup take out more and were not
        confirmed for this session: to be asked first - else None; their key; whether they
        switch it on). A confirmation is used once."""
        assert self.saved is not None
        saved = await self.saved.all()
        submitted = await sections.submit(
            "cleanup", self.host.configured, saved, form, addon_names=[], locked=self.host.locked
        )
        applying = effective(self.host.configured, saved, locked=self.host.locked)[0].cleanup
        candidate: Any = submitted.candidate
        if submitted.problems or candidate is None or not _widens(candidate, applying):
            return None, (), False
        key = (candidate.mode, candidate.unused_days, candidate.catalog_albums, candidate.fills)
        asked = self._confirmations.pop(session.token_hash, None)
        if (
            asked is not None
            and same(form.get("confirm", ""), asked[0])
            and asked[1] == key
            and time.time() - asked[2] < CONFIRM_SECONDS
        ):
            return None, key, False  # confirmed: saved now
        return candidate, key, applying.mode != "on"

    async def _cleanup_saved(
        self,
        request: Request,
        session: Session,
        section: str,
        form: dict[str, str],
        submitted: sections.Submitted,
        written: sections.Written | None,
    ) -> Response:
        if written is None:
            return await self.cleanup_page(
                request,
                session,
                section=section,
                form=form,
                problems=submitted.problems,
                status_code=400,
            )
        words = ""
        cleanup = self.services.cleanup if self.services is not None else None
        if section == "cleanup" and "mode" in written.changed:
            mode = str(getattr(submitted.candidate, "mode", ""))
            if cleanup is not None and not cleanup.can_list and mode != "off":
                words = (
                    "Saved, but nothing is checked or taken out: Shijhon needs the usage"
                    " export ([navidrome] usage_export_path) to see who uses what."
                )
            elif mode == "on":
                words = "The cleanup is on: its next check takes out the releases unused by then."
            elif mode == "dry_run":
                words = "The cleanup is a dry run: its next check lists what it would take out."
            else:
                words = "The cleanup is off: nothing is listed or taken out."
            log.info("dashboard: %s set the cleanup %s", printable(session.username), mode)
        self._remember(
            session,
            Flash(
                "cleanup",
                keys=tuple(written.changed),
                status=_saved(written),
                words=words,
                part=section,
                tone="info",
            ),
        )
        return self._see("/cleanup")

    async def _cleanup_preview(self, candidate: Any) -> cleaning.Preview:
        """What a check with the settings sent would take out now (the library is not
        written; bounded: it goes on in the background when slow)."""
        services = self.services
        cleanup = services.cleanup if services is not None else None
        if cleanup is None or not cleanup.can_list:
            return cleaning.Preview(
                problem="Shijhon can't see who uses what without the usage export"
                " ([navidrome] usage_export_path): set it first, then switch the cleanup on"
                " here."
            )
        try:
            listing = await cleanup.preview(
                unused_days=candidate.unused_days,
                catalog_albums=candidate.catalog_albums,
                fills=candidate.fills,
                wait=cleaning.PREVIEW_SECONDS,
            )
        except PreviewFailed as exc:  # said on the page: nothing is saved
            return cleaning.Preview(problem=f"The dry run failed ({exc}); the log says more.")
        except Exception as exc:
            log.warning("cleanup preview failed: %s", type(exc).__name__)
            return cleaning.Preview(problem=f"The dry run failed ({type(exc).__name__}).")
        if listing is None:
            return cleaning.Preview(
                problem="The dry run is taking a while (or another is running): it goes on,"
                " and saving these settings again in a minute shows what it found."
            )
        if listing.refused:  # by this database nothing is taken out: not switched on
            return cleaning.Preview(problem=f"Nothing would be taken out: {listing.refused}.")
        return cleaning.Preview(listing)

    async def cleanup_check(self, request: Request, session: Session) -> Response:
        """Run a dry run now (paced: one at a time, one a minute), in the background."""
        services = self.services
        cleanup = services.cleanup if services is not None else None

        def note(words: str, tone: str = "info") -> Response:
            self._remember(session, Flash("cleanup", words=words, tone=tone))
            return self._see("/cleanup")

        if cleanup is None or not cleanup.can_list:
            return note(
                "Nothing can be listed: Shijhon needs the usage export ([navidrome]"
                " usage_export_path) to see who uses what.",
                "error",
            )
        if cleanup.checking_since is not None:
            return note("A check is under way: its result shows here when it is done.")
        wait = cleaning.next_dry_run(cleanup)
        if wait is not None:
            return note(
                Markup("A dry run ran a moment ago: another can start at {}.").format(hm(wait))
            )

        async def run() -> None:
            try:
                done = await cleanup.dry_run()
            except Exception as exc:
                log.warning("cleanup (dry run from the dashboard) failed: %s", why_failed(exc))
                return
            if done is not None and done.listed is not None and not done.refused:
                unused = done.listed.unused
                log.info(
                    "cleanup (dry run from the dashboard): %d release(s) would be taken out"
                    " (%d placeholders); %d in use kept; %d not due yet",
                    len(unused),
                    sum(len(c.songs) for c in unused),
                    len(done.listed.due) - len(unused),
                    done.listed.waiting,
                )

        cleanup.manual_at = time.time()  # paced from the press (the run sets it again)
        if not self.host.try_spawn(run):
            return note("Shijhon is stopping; nothing done.", "error")
        log.info("dashboard: %s ran the cleanup's dry run", printable(session.username))
        return note("Checking what the cleanup would take out now. Reload in a moment to see it.")

    async def diagnostics_page(self, request: Request, session: Session) -> Response:
        assert self.store is not None and self.saved is not None
        services = self.services
        addons = await self._stored()
        results: dict[str, Any] = {}

        async def navidrome() -> None:
            results["navidrome"] = await status.navidrome_status(
                services.navidrome if services else None
            )

        async def manifests() -> None:
            await self.checks.check(addons)

        async def catalog() -> None:
            results["catalog"] = (
                await self._catalog_status(await self.saved.all()) if self.saved else {}
            )

        async def storage() -> None:
            if self._storage is None or time.time() - self._storage.at > 300:
                assert self.store is not None
                self._storage = await status.storage(
                    self.store, self.host.configured.navidrome.library_path
                )

        async with anyio.create_task_group() as group:
            for work in (navidrome, manifests, catalog, storage):
                group.start_soon(work)
        chain = [
            {
                "addon": addon,
                "order": index + 1,
                "health": self._health(addon),
                "version": (
                    c.manifest.version if (c := self.checks.known(addon)) and c.manifest else ""
                ),
                "starts": self._plan_addon(addon),
            }
            for index, addon in enumerate(addons)
        ]
        deliverer = services.deliverer if services is not None else None
        starts = list(reversed(deliverer.recent)) if deliverer is not None else []
        running = self.running or self.host.configured
        return await self._render(
            "diagnostics.html",
            session=session,
            current="diagnostics",
            checked_at=time.time(),
            nd=results["navidrome"],
            nd_url=(
                f"{self.host.configured.navidrome.socket} (socket)"
                if self.host.configured.navidrome.socket is not None
                else _origin(self.host.configured.navidrome.url)
            ),
            pinned=status.PINNED_NAVIDROME,
            chain=chain,
            catalog=results["catalog"],
            catalog_label=_catalog_label(running.catalog.kind),
            # The add-on a catalog is taken from.
            catalog_addon=_catalog_addon(self.services and self.services.catalog),
            catalog_region=getattr(self.services and self.services.catalog, "region", ""),
            schema=await status.schema(self.store),
            bundled=status.bundled_schema(),
            started=self.started_at,
            storage=self._storage,
            # The music folder's files are to get their artist lists, but Shijhon cannot write.
            library_read_only=bool(
                services is not None
                and services.artist_lists is not None
                and await anyio.to_thread.run_sync(services.artist_lists.read_only)
            ),
            database_private=status.private_mode(self.host.configured.database_path),
            errors=self.errors.recent(),
            starts=[
                {**dataclasses.asdict(start), "purpose": PURPOSES.get(start.purpose, start.purpose)}
                for start in starts
            ],
        )

    async def appearance_page(self, request: Request, session: Session) -> Response:
        assert self.saved is not None
        saved_now = False
        if request.method == "POST":
            accent = request.state.form.get("accent", "")
            if accent in ACCENTS:
                await self.saved.put(
                    f"{APPEARANCE}:{session.user_id}",
                    {"accent": REMOVE if accent == DEFAULT_ACCENT else accent},
                    session.user_id,
                )
                log.info("dashboard: %s chose the accent %s", printable(session.username), accent)
                self._remember(session, Flash("appearance", words="Accent color saved."))
            return self._see("/appearance")
        flash = self._flash_for(session, "appearance")
        saved_now = flash is not None
        return await self._render(
            "appearance.html",
            session=session,
            current="appearance",
            accents=[(key, ACCENT_NAMES[key]) for key in ACCENTS],
            chosen=await self.saved.accent(session.user_id),
            saved_now=saved_now,
        )

    async def quality_page(self, request: Request, session: Session) -> Response:
        """Each person's lowest audio quality (``delivery/quality_gate.py``)."""
        services = self.services
        minimums = services.quality if services is not None else None
        if request.method == "POST":
            if minimums is not None:
                form = request.state.form
                if form.get("reset"):
                    await minimums.set(session.user_id, None, by=session.user_id)
                    words = "You use the server's default again."
                elif (choice := form.get("minimum", "")) in QUALITY_CHOICES:
                    await minimums.set(session.user_id, choice, by=session.user_id)
                    await minimums.set_upgrades(
                        session.user_id, form.get("upgrade") == "on", by=session.user_id
                    )
                    words = "Lowest quality saved."
                else:
                    words = ""
                if words:
                    log.info(
                        "dashboard: %s chose the lowest quality %s",
                        printable(session.username),
                        "default" if form.get("reset") else form.get("minimum", ""),
                    )
                    self._remember(session, Flash("quality", words=words))
            return self._see("/quality")
        flash = self._flash_for(session, "quality")
        own = await minimums.choice(session.user_id) if minimums is not None else None
        upgrades = await minimums.upgrades(session.user_id) if minimums is not None else True
        default = minimums.default if minimums is not None else "any"
        return await self._render(
            "quality.html",
            session=session,
            current="quality",
            available=minimums is not None,
            choices=[(value, QUALITY_LABELS[value]) for value in QUALITY_CHOICES],
            chosen=own or default,
            own=own is not None,
            default_words=QUALITY_WORDS.get(default, ""),
            upgrades=upgrades,
            words=QUALITY_WORDS,
            saved_now=flash.words if flash is not None else "",
        )


def _plain_page(url: Any) -> str | None:
    """An add-on's sign-in page as an agent may be told it: an http(s) page address without
    a query string, a fragment or a user part - what such parts carry is not for an agent.
    Any other is left out (the Add-ons page links it)."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
        named = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not named:
        return None
    if parts.query or parts.fragment or "?" in url or "#" in url or "@" in parts.netloc:
        return None
    return url


def _wall(until: float, clock: Callable[[], float]) -> float:
    """A time on another clock (a monotonic one) as wall-clock time."""
    return time.time() + (until - clock())


def _policy_default(fill: Any) -> str:
    """ "Default 3 songs or 25 %" (the configuration's policy)."""
    from shijhon.config import FillSettings

    share = format_number(round(fill.auto_min_share * 100, 9))
    words = f"Default {fill.auto_min_songs} songs or {share} %"
    builtin = FillSettings()
    if (fill.auto_min_songs, fill.auto_min_share) != (
        builtin.auto_min_songs,
        builtin.auto_min_share,
    ):
        words += ", from the configuration"
    return words


def _catalog_label(kind: str) -> str:
    """The catalog of ``kind`` in words: its adapter's label ("" for none)."""
    adapter = plugin.find(kind)
    return adapter.label if adapter is not None else ("" if kind == plugin.NONE else kind)


def _own_page(target: str) -> str:
    """A dashboard page's path as given (where to go after a restart); Diagnostics for
    anything else."""
    if not target.startswith(PATH + "/") or target.startswith(PATH + "//") or "\\" in target:
        return f"{PATH}/diagnostics"
    if any(ch.isspace() or not ch.isprintable() for ch in target) or len(target) > 200:
        return f"{PATH}/diagnostics"
    return target


def _catalog_addon(catalog: Any) -> str:
    """The name of the add-on the running catalog is taken from ("": from none)."""
    inner = getattr(catalog, "inner", catalog)
    return inner.name if isinstance(inner, AddonCatalog) else ""


def _saved(written: sections.Written) -> str | Markup:
    gone = [key for key, _ in written.displaced if key not in written.changed]
    count = len(written.changed) + len(written.removed) + len(gone)
    if not count:
        return "Nothing to save: no setting changed"
    return Markup("Saved {} change{} at {}").format(
        count, "s" if count != 1 else "", hm(time.time())
    )


def _widens(candidate: Any, applying: Any) -> bool:
    """Whether cleanup settings let it take out more than those that apply: switched on,
    or while on fewer days or another kind of release."""
    if candidate.mode != "on":
        return False
    if applying.mode != "on":
        return True
    return (
        candidate.unused_days < applying.unused_days
        or (candidate.catalog_albums and not applying.catalog_albums)
        or (candidate.fills and not applying.fills)
    )


class _Unfit(Exception):
    """A value a setting does not take: what to enter (its row), and the notice's rule."""

    def __init__(self, row: str, notice: str) -> None:
        super().__init__(row)
        self.row = row
        self.notice = notice


def _addon_value(
    item: Declared, stored: Any, sent: str, marks: Mapping[str, str] | None = None
) -> tuple[bool, Any]:
    """An add-on's declared setting as its form sent it: whether it differs from what the
    form showed (the stored value, else the add-on's default), and the value to keep, of
    the setting's kind (a switch on or off, a number a number; None: empty, the add-on's
    default). Raises :class:`_Unfit`."""
    # (Not plain: the form showed "kept", or the default - not the value.)
    plain = shown(item, stored, marks)
    showed = setting_text(stored) if stored is not None and plain else (item.default or "")
    if item.kind == "switch":  # a checkbox after a hidden "false" of the same name
        on = sent == "true"
        return on != (showed == "true"), on
    text = sent.strip()
    if not plain and text == KEPT:
        return False, stored
    if not text:
        return showed != "" or not plain, None
    if plain and text == showed:
        return False, stored  # as shown: kept as it is, whatever it holds
    if item.kind == "number":
        try:
            number = parse_number(text, whole=False)
        except Invalid as exc:
            raise _Unfit(str(exc), "must be a number.") from None
        try:
            same = plain and parse_number(showed, whole=False) == number
        except Invalid:
            same = False
        return not same, int(number) if float(number).is_integer() else number
    if item.kind == "select" and text not in dict(item.options) and text != (item.default or ""):
        raise _Unfit("Choose one of the listed values.", "must be one of the listed values.")
    return True, text


def _rate_limit(reason: str | None) -> bool:
    text = (reason or "").lower()
    return "429" in text or "rate limit" in text


def _row(problem: sections.Problem | None) -> str | None:
    return problem.row if problem else None


def _placed(ids: list[int], ordered: list[int]) -> list[int]:
    """``ids`` with the add-ons of ``ordered`` in its order, in the places they had among
    them; the others stay where they are."""
    chosen, moved = set(ordered), iter(ordered)
    return [next(moved) if source_id in chosen else source_id for source_id in ids]


def slow_after_row(prefix: str, general: float, text: str = "", **more: Any) -> dict[str, Any]:
    """An add-on's own "Slow after": empty, the installation's."""
    return _setting(
        id=f"{prefix}-slow-after",
        name="slow_after",
        kind="decimal",
        label="Slow after",
        spoken="times its usual time",
        unit="times usual",
        text=text,
        help="When another add-on has sent the song while this one is still trying, it counts"
        " as slow, toward its cooldown, once it is past its step's time and this many times its"
        " usual time to first audio. Give a higher value to one that is often a little late but"
        " works. Empty: Playback's setting applies.",
        default=f"Default {format_number(general)} times usual, from Playback",
        **more,
    )


def _setting(**values: Any) -> dict[str, Any]:
    """A row for the ``setting`` macro, with the keys a row may leave out."""
    row: dict[str, Any] = {
        "help": "",
        "unit": "",
        "spoken": "",
        "short": False,
        "locked": "",
        "problem": None,
        "modified": False,
        "restart": False,
        "saved": False,
        "default": "",
    }
    row.update(values)
    return row


def _slug(key: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in key.lower())


def _origin(url: str) -> str:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError):
        return "?"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.host}{port}"


def _secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"
