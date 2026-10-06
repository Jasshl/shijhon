"""The dashboard: Jinja2 pages and plain forms under ``/shijhon`` on Shijhon's own address.

``/shijhon`` cannot collide with Navidrome, whose paths are ``/app``, ``/api``, ``/rest``,
``/auth``, ``/share``, ``/jellyfin``, ``/backgrounds``, ``/debug`` and its metrics path
(everything else redirects to ``/app``), unless Navidrome's own ``BaseURL`` is set to
``/shijhon``. Server-rendered pages and forms: every change is a form post answered with a
redirect. One small script, on the restart's waiting page only (``static/restart.js``).
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qsl, quote

import anyio
import httpx
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from shijhon import __copyright__, __license__, __version__
from shijhon.catalog import plugin
from shijhon.catalog.addon import AddonCatalog
from shijhon.catalog.base import CatalogError
from shijhon.cleanup import PreviewFailed, why_failed
from shijhon.config import Settings
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
)
from shijhon.dashboard.auth import (
    AdminCheck,
    Attempts,
    Session,
    Sessions,
    client_key,
    navidrome_login,
    same,
)
from shijhon.dashboard.errors import RecentErrors
from shijhon.dashboard.fields import Group, Invalid, format_number, parse_number, spoken_unit
from shijhon.dashboard.saved import (
    ACCENTS,
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
from shijhon.delivery.netpolicy import PROJECT_URL, Reach, Resolver, system_resolver
from shijhon.delivery.pacing import AddonPace, Limits, origin
from shijhon.delivery.sources import MAX_LIMIT, StoredSource
from shijhon.fill.fills import ChoiceRefused
from shijhon.locks import KeyedLocks
from shijhon.navidrome.client import NavidromeError
from shijhon.placeholders.engine import MaterializeError
from shijhon.proxy.forwarding import Forwarding
from shijhon.store import Store

if TYPE_CHECKING:
    import aiosqlite

    from shijhon.app import Services

log = logging.getLogger(__name__)

PATH = "/shijhon"
SESSION_COOKIE = "shijhon_session"
SIGNIN_COOKIE = "shijhon_signin"
MAX_FORM_BYTES = 64 * 1024
NAV = (
    ("addons", "Add-ons"),
    ("playback", "Playback"),
    ("catalog", "Catalog"),
    ("library", "Library"),
    ("cleanup", "Cleanup"),
    ("diagnostics", "Diagnostics"),
)
ACCENT_NAMES = {
    "green": "Deep green",
    "teal": "Teal",
    "slate": "Slate blue",
    "graphite": "Graphite",
    "purple": "Purple",
}
PAGE_SECTIONS = {"playback": "delivery", "catalog": "catalog"}
SECTION_WORDS = {
    "playback": (
        "Playback",
        "How a catalog song's audio is found: which add-ons are asked, and how long a"
        " client waits.",
    ),
    "catalog": (
        "Catalog",
        "Where the catalog comes from: a catalog adapter you install, or one of your"
        " add-ons that has a catalog.",
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
FAILING_IN_A_ROW = 3  # failures without a success before an add-on shows "Error"
# Downloaded audio's limits: on the Cleanup page, not with Playback's other settings.
DOWNLOAD_LIMITS = ("delivered_days", "delivered_gb")
# Of [search], on the Catalog page (a form of its own): the wait for the catalog.
SEARCH_WAIT = ("budget_seconds",)
SEARCH_WAIT_FORM = "wait"
CONFIRM_SECONDS = 900.0  # how long the cleanup's confirmation stands (from its dry run)
ADDON_BUDGET = {"low": 0.0, "high": 600.0}  # SourceRegistry's limits for its own budget


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


ADDON_LIMITS = (
    _OwnLimit(
        "requests_per_second",
        "addon_requests_per_second",
        "Requests a second",
        "/s",
        "requests a second",
        False,
        0,
        "Requests sent to this add-on a second, for everyone together. Raise it only for an"
        " add-on of your own. 0: no limit.",
    ),
    _OwnLimit(
        "request_burst",
        "addon_request_burst",
        "Requests at once",
        "requests",
        "requests",
        True,
        1,
        "How many may go at once after a quiet moment, before the rate applies.",
    ),
    _OwnLimit(
        "audio_openings",
        "addon_audio_openings",
        "Songs opened at once",
        "songs",
        "songs",
        True,
        0,
        "Songs whose audio this add-on is asked for at the same time; the song being played"
        " never waits. 0: no limit.",
    ),
)
# An add-on's "More settings": its own time and limits, in place of the installation's.
ADDON_MORE = Group(
    "limits",
    "Time and limits",
    "Empty: as set on Playback. Add-ons at one address share their limits; the strictest apply.",
    more=True,
)
# Where an add-on runs (its settings, and the form that adds one).
REACH_HELP = (
    "Where the add-on runs; one on this machine or the local network is refused unless chosen"
    " here. In a container, the Docker host is on the local network."
)


class Host(Protocol):
    """What the dashboard needs of the running Shijhon (``ShijhonApp``)."""

    configured: Settings
    locked: Locked  # the dashboard's settings the environment sets
    services: Services | None
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


def hm(at: float) -> str:
    return time.strftime("%H:%M", time.localtime(at))


def day(at: float) -> str:
    moment = time.localtime(at)
    return f"{moment.tm_mday} {time.strftime('%B', moment)}"


def when(at: float) -> str:
    """14:05 today, else 12 September, 14:05."""
    today = time.localtime().tm_yday == time.localtime(at).tm_yday and abs(time.time() - at) < 86400
    return hm(at) if today else f"{day(at)}, {hm(at)}"


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
        self.admins = AdminCheck()
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
            Route("/sign-out", page(self.sign_out), methods=["POST"]),
            Route("/addons", page(self.addons_page), methods=["GET"]),
            Route("/addons", page(self.addon_add), methods=["POST"]),
            Route("/addons/configuration", page(self.addons_hand_back), methods=["POST"]),
            Route("/addons/{addon:int}/enabled", page(self.addon_enabled), methods=["POST"]),
            Route("/addons/{addon:int}/move", page(self.addon_move), methods=["POST"]),
            Route("/addons/{addon:int}/settings", page(self.addon_settings), methods=["POST"]),
            Route("/addons/{addon:int}/remove", page(self.addon_remove), methods=["POST"]),
            Route("/playback", page(self.section_page), methods=["GET", "POST"]),
            Route("/catalog", page(self.section_page), methods=["GET", "POST"]),
            Route("/catalog/check", page(self.catalog_check_now), methods=["POST"]),
            Route("/catalog/wait", page(self.catalog_wait_save), methods=["POST"]),
            Route("/library", page(self.library_page), methods=["GET"]),
            Route("/library/policy", page(self.library_save), methods=["POST"]),
            Route("/library/pass", page(self.library_save), methods=["POST"]),
            Route("/library/settings", page(self.library_save), methods=["POST"]),
            Route("/library/review/{album}", page(self.library_review), methods=["POST"]),
            Route("/cleanup", page(self.cleanup_page), methods=["GET"]),
            Route("/cleanup/settings", page(self.cleanup_save), methods=["POST"]),
            Route("/cleanup/downloads", page(self.cleanup_save), methods=["POST"]),
            Route("/cleanup/check", page(self.cleanup_check), methods=["POST"]),
            Route("/diagnostics", page(self.diagnostics_page), methods=["GET"]),
            Route("/restart", page(self.restart_now), methods=["POST"]),
            Route("/restarting", page(self.restarting_page), methods=["GET"]),
            Route("/alive", self.alive, methods=["GET"]),
            Route("/appearance", page(self.appearance_page), methods=["GET", "POST"]),
            Mount("/static", StaticFiles(packages=[("shijhon.dashboard", "static")])),
        ]
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
        self._http = httpx.AsyncClient(timeout=10.0, trust_env=False)

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
        accent = await self.saved.accent() if self.saved is not None else DEFAULT_ACCENT
        html = self.env.get_template(template).render(
            prefix=PATH,
            accent=accent,
            user=session.username if session else "",
            csrf=session.csrf if session else "",
            current=current,
            nav=NAV,
            version=__version__,
            copyright=__copyright__,
            license=__license__,
            source_url=PROJECT_URL,
            pending=await self._pending() if session else [],
            # "Restart Shijhon" is offered where something starts Shijhon again.
            can_restart=bool(session) and self.host.can_restart,
            **context,
        )
        return HTMLResponse(html, status_code, headers=HEADERS)

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

    async def _session(self, request: Request) -> Session:
        """The signed-in admin, or :class:`Refused` with a redirect to sign-in."""
        if self.sessions is None:
            raise Refused(PlainTextResponse("Shijhon is starting.\n", 503, headers=HEADERS))
        token = request.cookies.get(SESSION_COOKIE)
        session = await self.sessions.get(token)
        if session is None:
            where = request.url.path if request.method == "GET" else f"{PATH}/addons"
            raise Refused(self._see(f"/sign-in?next={quote(where, safe='/')}"))
        services = self.services
        verdict = await self.admins.admin(
            services.navidrome if services else None, session.username
        )
        if verdict is False:
            await self.sessions.end_user(session.username)
            self.admins.forget(session.username)
            log.info(
                "dashboard: %s signed out: not a Navidrome admin any more",
                printable(session.username),
            )
            raise Refused(self._see("/sign-in?reason=admin"))
        if verdict is None:
            raise Refused(
                await self._render(
                    "unverified.html",
                    session=None,
                    status_code=503,
                    title="Admin rights can't be checked",
                )
            )
        return session

    def _page(
        self, handler: Callable[[Request, Session], Awaitable[Response]]
    ) -> Callable[[Request], Awaitable[Response]]:
        """Pages for signed-in admins; posts need the session's CSRF token."""

        async def endpoint(request: Request) -> Response:
            try:
                session = await self._session(request)
                if request.method == "POST":
                    if request.headers.get("sec-fetch-site", "same-origin") == "cross-site":
                        raise Refused(
                            PlainTextResponse("Cross-site form refused.\n", 403, headers=HEADERS)
                        )
                    form = await self._form(request)
                    if not same(form.get("csrf", ""), session.csrf):
                        log.warning("dashboard: a form without its session's token was refused")
                        raise Refused(
                            PlainTextResponse(
                                "The form has expired. Go back, reload the page and try again.\n",
                                403,
                                headers=HEADERS,
                            )
                        )
                    request.state.form = form
                return await handler(request, session)
            except Refused as refused:
                return refused.response

        return endpoint

    async def home(self, request: Request) -> Response:
        return self._see("/addons")

    async def sign_in_page(self, request: Request, **context: Any) -> Response:
        if self.sessions is not None and await self.sessions.get(
            request.cookies.get(SESSION_COOKIE)
        ):
            return self._see("/addons")
        token = request.cookies.get(SIGNIN_COOKIE) or os.urandom(24).hex()
        reason = request.query_params.get("reason")
        context.setdefault("error", None)
        context.setdefault("error_title", None)
        if reason == "admin" and context["error"] is None:
            context["error"] = "Signed out: this account no longer has admin rights in Navidrome."
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
        if not login.admin:
            log.info(
                "dashboard: sign-in refused for %s: not a Navidrome admin", printable(username)
            )
            return await again(
                "This account has no admin rights in Navidrome. The dashboard is for admins only.",
                code=403,
            )
        token, _ = await self.sessions.create(login.username)
        self.admins.signed_in(login.username)
        log.info("dashboard: %s signed in", printable(login.username))
        target = form.get("next", "")
        if not target.startswith(PATH + "/") or target.startswith(PATH + "//") or "\\" in target:
            target = f"{PATH}/addons"
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

    async def sign_out(self, request: Request, session: Session) -> Response:
        assert self.sessions is not None
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            await self.sessions.end(token)
        log.info("dashboard: %s signed out", printable(session.username))
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
                waiting.append({"label": f.meta.label, "at": item.saved_at if item else None})
        if self.addons_handed_back:
            waiting.append({"label": "Add-on list from the configuration file", "at": None})
        return waiting

    async def _addon_names(self) -> list[str]:
        return [addon.name for addon in await self._stored()]

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
        names = await self._addon_names()
        extra: dict[str, Any] = {}
        shown = dict(problems or {})
        # (The second form's flash is its own: not the main form's status.)
        waited = flash if flash is not None and flash.part == SEARCH_WAIT_FORM else None
        flash = None if waited is not None else flash
        if page == "catalog":
            extra = await self._catalog_status(saved)
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
        if page == "playback":
            extra = {"missing": self._missing_sources(saved, names)}
        groups = sections.rows(
            section,
            self.host.configured,
            saved,
            addon_names=names,
            form=form,
            problems=shown,
            just_saved=list(flash.keys) if flash else None,
            date=day,
            locked=self.host.locked,
            unmarked=first_step,
        )
        if page == "playback":  # downloaded audio's limits are on the Cleanup page
            groups = [
                (group, [row for row in members if row["name"] not in DOWNLOAD_LIMITS])
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
            "problem": self.problems.get(section),
            "extra": extra,
            # The catalog whose adapter settings the form holds (sections.SHOWN_KIND).
            "shown_kind": (sections.SHOWN_KIND, kind) if section == "catalog" else None,
            "first_step": first_step,
        }
        return await self._render(
            "section.html", session=session, current=page, status_code=status_code, **context
        )

    def _missing_sources(self, saved: Any, names: list[str]) -> list[tuple[str, str]]:
        """(what is wrong, the environment variable that sets it or "")."""
        delivery = effective(self.host.configured, saved, locked=self.host.locked)[0].delivery
        env = self.host.locked.get("delivery", {})
        missing = []
        roles = (("primary_source", "primary add-on"), ("reliable_source", "preferred fallback"))
        for key, label in roles:
            name = getattr(delivery, key)
            if name and name not in names:
                missing.append(
                    (f"The {label} “{name}” is not in the add-on list.", env.get(key, ""))
                )
        return missing

    async def _section_save(
        self, request: Request, session: Session, page: str, section: str
    ) -> Response:
        form: dict[str, str] = request.state.form
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
    ) -> tuple[sections.Submitted, sections.Written | None, list[str]]:
        """Check and save a form's settings of one section, and apply the live ones: (the
        submission, what was written or None when it has problems, the keys applied)."""
        assert self.saved is not None
        async with self._section_saves.hold(section):
            saved = await self.saved.all()
            names = await self._addon_names()
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

    async def _owned(self, session: Session) -> None:
        """Any change in the dashboard makes the stored add-on list the one that applies."""
        assert self.saved is not None
        await self.saved.take_addons(session.username)
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
            addon.base_url, str(addon.reach), addon.settings, addon.budget, declared, marks, own
        ]  # fmt: skip
        text = json.dumps(content, sort_keys=True, default=str)
        return hmac.new(self._form_key, text.encode(), hashlib.sha256).hexdigest()[:32]

    def _health(self, addon: StoredSource) -> dict[str, Any]:
        """Light and words: off; the manifest unreachable; cooling down; failing (3 failures
        in a row without a success: one missing song is not an error); OK."""
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
        }
        if not addon.enabled:
            return {**health, "light": "off", "words": "Off"}
        if check is not None and check.error is not None:
            health.update(
                light="error",
                words="Manifest unreachable:",
                code=check.error,
                attention=True,
                detail=f"Checked {hm(check.at)}",
            )
        elif until is not None:
            health.update(light="cool", words=f"Cooling down until {when(until)}")
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
                detail=f"{found.failures_since_success:,} failures in a row, the last at"
                f" {when(found.last_failure_at or time.time())}",
            )
        elif check is not None:
            health.update(light="ok", words="OK")
        if found is not None and not health["detail"]:
            parts = []
            if found.last_success_at is not None:  # its audio's first byte, not a link
                parts.append(f"Last audio delivered {when(found.last_success_at)}")
            if found.successes or found.failures:
                parts.append(
                    f"{found.successes:,} found, {found.failures:,} failed since"
                    f" {hm(self.started_at)}"
                )
            if found.last_failure and found.last_failure_at:
                parts.append(f"last failure {when(found.last_failure_at)}: {found.last_failure}")
            health["detail"] = "; ".join(parts)
        return health

    async def _pace(self, url: str) -> AddonPace | None:
        """The limits of the add-on origin ``url`` is at: the dashboard's manifest
        requests count there like any request, after everything a listener waits for."""
        return None if self.services is None else await self.services.sources.pace_at(url)

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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The rows of an add-on's settings form: its manifest's, then where it runs and its
        address; and those under its "More settings": its time and its limits."""
        running = self.running or self.host.configured
        prefix = f"addon-{addon.id}"
        if form is not None:
            budget_text = form.get("budget_seconds", "")
        else:
            budget_text = format_number(addon.budget) if addon.budget is not None else ""
        reach = form.get("reach", addon.reach) if form is not None else addon.reach
        total = format_number(running.delivery.budget_seconds)
        check = self.checks.known(addon)
        declared = check.manifest.declared if check is not None and check.manifest else ()
        rows: list[dict[str, Any]] = [
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
                help="The add-on's address, which often holds its key; never shown. Empty: the"
                " current one is kept.",
                problem=_row(problems.get("manifest")),
                clear_label="",
            ),
        ]
        more = [
            _setting(
                id=f"{prefix}-budget",
                name="budget_seconds",
                kind="decimal",
                label="Time to first audio",
                spoken="seconds",
                unit="s",
                text=budget_text,
                help="How long this add-on has to start the audio. Empty: it shares the time to"
                " first audio set on Playback.",
                default=f"Default: shares Playback's, {total} s",
                problem=_row(problems.get("budget_seconds")),
                modified=addon.budget is not None,
            ),
            *(self._limit_row(addon, limit, form, problems) for limit in ADDON_LIMITS),
        ]
        return rows, more

    def _limit_row(
        self,
        addon: StoredSource,
        limit: _OwnLimit,
        form: dict[str, str] | None,
        problems: dict[str, sections.Problem],
    ) -> dict[str, Any]:
        """One of the add-on's own limits on what Shijhon sends it; empty: the
        installation's (Playback, "Limits per add-on")."""
        running = self.running or self.host.configured
        own = getattr(addon.limits, limit.name)
        if form is not None:
            text = form.get(limit.name, "")
        else:
            text = format_number(own) if own is not None else ""
        shared = format_number(getattr(running.delivery, limit.setting))
        return _setting(
            id=f"addon-{addon.id}-{_slug(limit.name)}",
            name=limit.name,
            kind="whole" if limit.whole else "decimal",
            label=limit.label,
            spoken=limit.spoken,
            unit=limit.unit,
            text=text,
            help=limit.help,
            default=f"Default: as every add-on, {shared}",
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
        flash = self._flash_for(session, "addons")
        items: list[dict[str, Any]] = []
        for index, addon in enumerate(addons):
            check = self.checks.known(addon)
            mine = form_for == addon.id
            rows, more = self._addon_rows(
                addon, form if mine else None, problems or {} if mine else {}
            )
            _, fold = sections.folded([(ADDON_MORE, more)])
            if flash is not None and flash.addon == addon.id and set(flash.keys):
                fold = dataclasses.replace(fold, open=True)  # (its time or limits saved now)
            items.append(
                {
                    "addon": addon,
                    "host": host_of(addon.base_url),
                    "order": index + 1,
                    "first": index == 0,
                    "last": index == len(addons) - 1,
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
                    "roles": self._roles(addon.name),
                    "form_version": self._addon_version(addon),
                }
            )
        errors = sum(1 for item in items if item["health"]["light"] == "error")
        cooling = sum(1 for item in items if item["health"]["light"] == "cool")
        owner = await self.saved.addons_owner()
        in_file = self.host.configured.addons
        differs: list[str] = []
        if in_file is not None and owner is not None and self.services is not None:
            differs = await self.services.sources.differs(in_file)  # the file's, not applied
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
            enabled=sum(1 for a in addons if a.enabled),
            errors=errors,
            cooling=cooling,
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
            differs=differs,
            stale=stale,
            handed_back=self.addons_handed_back,
            dash_off=dash_off,
        )

    def _roles(self, name: str) -> list[str]:
        running = self.running or self.host.configured
        roles = []
        if running.delivery.primary_source == name:
            roles.append("the primary add-on")
        if running.delivery.reliable_source == name:
            roles.append("the preferred fallback")
        return roles

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
        addons = await self._stored()
        name = manifest.name or httpx.URL(url).host or "Add-on"
        if any(addon.name == name for addon in addons):
            return await refuse(f"Nothing added: an add-on named “{name}” is in the list already.")
        added = await self.services.sources.add(name, url, {}, reach=Reach(reach_name))
        for addon in await self._stored():
            if addon.id == added:  # its manifest was read a moment ago: not asked for again
                self.checks.learn(addon, manifest)
        await self._owned(session)
        log.info("dashboard: %s added the add-on %s", printable(session.username), printable(name))
        self._remember(session, Flash("addons", words=f"{name} added at the end of the list."))
        return self._see("/addons")

    async def addon_enabled(self, request: Request, session: Session) -> Response:
        self._editable_list()
        assert self.services is not None
        addon = await self._addon(int(request.path_params["addon"]))
        wanted = request.state.form.get("enabled") == "true"
        if wanted != addon.enabled:
            await self.services.sources.set_enabled(addon.id, wanted)
            await self._owned(session)
            log.info(
                "dashboard: %s turned the add-on %s %s",
                printable(session.username),
                printable(addon.name),
                "on" if wanted else "off",
            )
        return self._see(f"/addons#addon-{addon.id}")

    async def addon_move(self, request: Request, session: Session) -> Response:
        self._editable_list()
        assert self.services is not None and self.store is not None
        addon = await self._addon(int(request.path_params["addon"]))
        ids = [item.id for item in await self._stored()]
        index = ids.index(addon.id)
        step = -1 if request.state.form.get("direction") == "up" else 1
        if 0 <= index + step < len(ids):
            ids[index], ids[index + step] = ids[index + step], ids[index]
            await self.services.sources.reorder(ids)
            await self._owned(session)
            log.info(
                "dashboard: %s moved the add-on %s",
                printable(session.username),
                printable(addon.name),
            )
        return self._see(f"/addons#addon-{addon.id}")

    async def addon_remove(self, request: Request, session: Session) -> Response:
        self._editable_list()
        assert self.services is not None
        addon = await self._addon(int(request.path_params["addon"]))
        await self.services.sources.remove(addon.id)
        self.checks.forget(addon.id)
        if self.saved is not None:
            await self.saved.put(SHOWN, {str(addon.id): REMOVE}, session.username)
        await self._owned(session)
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
        reach = form.get("reach", addon.reach)
        if reach not in REACHES:
            problem("reach", "Choose where the add-on runs.", "must be one of the listed places.")
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
        # a secret typed here is never stored without its "not shown" mark.
        also = None
        if marked != marks and self.saved is not None:
            also = functools.partial(
                self.saved.write,
                section=SHOWN,
                changes={str(addon.id): marked or REMOVE},
                by=session.username,
            )
        await self.services.sources.update(
            addon.id,
            base_url=new_url or None,
            settings=settings,
            reach=Reach(reach),
            budget_seconds=None if keep_budget else budget,
            clear_budget=not keep_budget and budget is None,
            limits=Limits(**own),  # type: ignore[arg-type]  # (whole numbers where they are)
            also=also,
        )
        if fresh is not None:  # read a moment ago at its address: not asked for again
            self.checks.learn(await self._addon(addon.id), fresh)
        await self._owned(session)
        log.info(
            "dashboard: %s saved the add-on %s's settings%s",
            printable(session.username),
            printable(addon.name),
            f"; its address is on another host: {len(dropped)} setting(s) removed"
            if dropped
            else "",
        )
        words = ""
        if dropped:
            words = (
                f"{addon.name}'s address is on another host now, so {len(dropped)} setting(s)"
                " its manifest does not declare plain were removed: they are not sent there."
                " Its secret settings can be entered again below if the new address needs them."
            )
        # (Its time or limits changed: their "More settings" is open on the page that follows.)
        before = {"budget_seconds": addon.budget, **dataclasses.asdict(addon.limits)}
        after = {"budget_seconds": budget if not keep_budget else addon.budget, **own}
        self._remember(
            session,
            Flash(
                "addons",
                words=words,
                keys=tuple(key for key in before if before[key] != after[key]),
                addon=addon.id,
                status=f"Saved at {hm(time.time())}",
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
        flash = self._flash_for(session, "library")
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
        context: dict[str, Any] = {
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
            "jobs": self.jobs.recent(session.token_hash),
            "page": page_number,
        }
        if fills is not None:
            scope = fills.scope
            rows, total, page_number = await library.review(self.store, scope, page_number)
            albums: set[str] | None = None
            # The pass's own cached catalog, when there is one (not the listeners' cache);
            # nothing is asked while the catalog rests after errors.
            own = getattr(getattr(passing, "matcher", None), "catalog", None)
            describing = getattr(own, "inner", None) or (services.catalog if services else None)

            async def count() -> None:
                nonlocal albums
                albums = await self._library_albums()

            async def describe() -> None:
                if not fills.resting:
                    await self.describer.describe(describing, [r for row in rows for r in row.refs])

            async with anyio.create_task_group() as group:
                group.start_soon(count)
                group.start_soon(describe)
            context.update(
                page=page_number,
                counts=await library.counts(self.store, scope, albums, fills.policy),
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
                running=self._pass_running(passing),
                resting=_wall(fills.resting_until, fills.clock) if fills.resting else None,
                pass_mode=passing.mode if passing is not None else "",
                interval=(passing.interval_seconds / 3600) if passing is not None else 0,
            )
        return await self._render(
            "library.html", session=session, current="library", status_code=status_code, **context
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
        back = f"/library?page={quote(form.get('page', '1'))}#review"
        if fills is None:
            raise Refused(PlainTextResponse("Filling albums is off.\n", 409, headers=HEADERS))

        def note(words: str, tone: str = "info") -> Response:
            self._remember(session, Flash("library", words=words, tone=tone))
            return self._see(back)

        found = await self._review_row(fills, album_id)
        if found is None:
            return note("That album is no longer in the review list.")
        name = f"{found['artist'] or 'Unknown artist'} \u2013 {found['title'] or 'Unknown album'}"
        action = form.get("action", "")
        if action not in ("fill", "keep", "rematch"):
            return note("Choose Fill, Keep as is or Match again.", "error")
        if action == "rematch" and fills.resting:
            until = when(_wall(fills.resting_until, fills.clock))
            return note(
                f"{name} is not matched again now: the catalog is resting after errors."
                f" Try again after {until}.",
                "error",
            )
        refs = [found["release_ref"]] if found["release_ref"] else []
        refs += [r for r in (found["candidates"] or "").split(",") if r and r not in refs]
        ref = form.get("release", "")
        if action == "fill":
            if library.is_part(found["reason"] or ""):
                return note(
                    f"{name} is part of another album's release: it is not filled on its own.",
                    "error",
                )
            if ref not in refs:
                return note(f"{name}: choose one of the listed releases.", "error")
        # Claimed before anything is awaited: one action at a time for an album.
        job = self.jobs.claim(album_id, name, action)
        if job is None:
            return note(f"{name} is busy with the last action; wait for its result.")
        who = printable(session.username)
        if action == "keep":
            try:
                await fills.keep(album_id)
            except ChoiceRefused as exc:
                self.jobs.drop(job)
                return note(f"{name} could not be kept as it is: {exc.reason}.", "error")
            except NavidromeError as exc:
                self.jobs.drop(job)
                return note(f"{name} could not be kept as it is: {exc}.", "error")
            self.jobs.drop(job)
            log.info("dashboard: %s kept %s as it is", who, printable(name))
            return note(f"{name} is kept as it is: it is not matched again in this catalog.", "ok")
        if action == "fill":
            label = self.describer.label(ref) or ref
            log.info("dashboard: %s chose %s for %s", who, ref, printable(name))
            started = self.host.try_spawn(lambda: self._fill(fills, job, ref, label))
            words = (
                f"Filling {name} from {label}. This can take a minute; reload to see how it went."
            )
        else:
            log.info("dashboard: %s asked to match %s again", who, printable(name))
            started = self.host.try_spawn(lambda: self._match(fills, job))
            words = f"Matching {name} again. Reload to see the result."
        if not started:
            self.jobs.finish(job, ok=False, message=f"{name}: Shijhon is stopping; nothing done.")
            return note(f"{name}: Shijhon is stopping; nothing done.", "error")
        return note(words)

    async def _review_row(self, fills: Any, album_id: str) -> Any:
        assert self.store is not None
        return await self.store.fetchone(
            "SELECT title, artist, reason, candidates, release_ref FROM album_matches"
            " WHERE album_id = ? AND scope = ? AND outcome = 'review'",
            [album_id, fills.scope],
        )

    async def _fill(self, fills: Any, job: library.Job, ref: str, label: str) -> None:
        """One fill at a time (two albums may list the same release: the second must see
        the first's result)."""
        try:
            async with self.jobs.one_fill:
                if await self._review_row(fills, job.album_id) is None:
                    self.jobs.finish(
                        job, ok=False, message=f"{job.title} is no longer in the review list."
                    )
                    return
                decision = await fills.choose(job.album_id, ref)
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

    async def _match(self, fills: Any, job: library.Job) -> None:
        """Forget the album's match and match it again at the pass's pace, without filling;
        the result is read from the album's own row (the match of a release shared by
        several owned albums may record the result on another one)."""
        services = self.services
        paced = services.library_pass.matcher if services and services.library_pass else None
        try:
            await fills.rematch(job.album_id)
            await fills.fill(job.album_id, hold=True, matcher=paced)
            assert self.store is not None
            row = await self.store.fetchone(
                "SELECT outcome, reason FROM album_matches WHERE album_id = ? AND scope = ?",
                [job.album_id, fills.scope],
            )
        except BaseException as exc:
            self.jobs.finish(
                job, ok=False, message=f"{job.title} could not be matched ({type(exc).__name__})."
            )
            if not isinstance(exc, Exception):
                raise
            log.exception("dashboard: matching %s again failed", printable(job.title))
            return
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
            words, ok = (
                f"{job.title} matches a release now: it is shown complete, and filled"
                " automatically or when used, as the fill policy says.",
                True,
            )
        elif outcome == "review":
            headline, _, _ = library.explain(reason)
            words, ok = f"{job.title} still needs review: {headline.lower()}.", False
        elif outcome == "complete":
            words, ok = f"{job.title} is complete as it is.", True
        elif outcome == "none":
            words, ok = f"No release matches {job.title}.", False
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
            return note(f"A dry run ran a moment ago: another can start at {hm(wait)}.")

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
            }
            for index, addon in enumerate(addons)
        ]
        running = self.running or self.host.configured
        return await self._render(
            "diagnostics.html",
            session=session,
            current="diagnostics",
            checked_at=time.time(),
            nd=results["navidrome"],
            nd_url=_origin(self.host.configured.navidrome.url),
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
            database_private=status.private_mode(self.host.configured.database_path),
            errors=self.errors.recent(),
        )

    async def appearance_page(self, request: Request, session: Session) -> Response:
        assert self.saved is not None
        saved_now = False
        if request.method == "POST":
            accent = request.state.form.get("accent", "")
            if accent in ACCENTS:
                await self.saved.put(
                    APPEARANCE,
                    {"accent": REMOVE if accent == DEFAULT_ACCENT else accent},
                    session.username,
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
            chosen=await self.saved.accent(),
            saved_now=saved_now,
        )


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


def _saved(written: sections.Written) -> str:
    gone = [key for key, _ in written.displaced if key not in written.changed]
    count = len(written.changed) + len(written.removed) + len(gone)
    if not count:
        return "Nothing to save: no setting changed"
    return f"Saved {count} change{'s' if count != 1 else ''} at {hm(time.time())}"


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
