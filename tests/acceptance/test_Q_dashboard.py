"""The dashboard: sign-in through Navidrome for its admins only, CSRF
on every form, settings pages generated from the definitions that take effect (or say they
wait for a restart), add-ons, appearance, and secrets that never appear in a response or a
log line."""

from __future__ import annotations

import html
import importlib.metadata
import logging
import re
import threading
import time
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from shijhon import __version__
from shijhon.config import AddonSettings
from shijhon.dashboard.status import scan_time
from shijhon.delivery.netpolicy import PROJECT_URL, USER_AGENT
from shijhon.delivery.pacing import Limits
from shijhon.delivery.sources import StoredSource
from shijhon.log import quiet_libraries
from tests.conftest import NavidromeFactory
from tests.harness.addon_catalog import FakeCatalog
from tests.harness.dashboard import PATH, Browser, forms
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay

LISTENER, LISTENER_PASSWORD = "listener", "listener-password-for-tests"
HELPER, HELPER_PASSWORD = "helper", "helper-password-for-tests"
# Invented secrets: none of them may appear in a page, a redirect or a log line.
SECRET_KEY = "addonkey7f3a9e"  # in an add-on's URL
SECRET_KEY_2 = "addonkey2b81c0"
SECRET_TOKEN = "catalogtoken5b1e44"
SECRET_SETTING = "settingsecret9c4d2a"
SECRETS = (SECRET_KEY, SECRET_KEY_2, SECRET_TOKEN, SECRET_SETTING)
PAGES = {
    "addons": "Add-ons",
    "playback": "Playback",
    "catalog": "Catalog",
    "library": "Library",
    "cleanup": "Cleanup",
    "diagnostics": "Diagnostics",
    "appearance": "Appearance",
}
QUALITY = {
    "key": "quality",
    "label": "Quality",
    "type": "select",
    "default": "6",
    "options": [{"value": "5", "label": "MP3 320"}, {"value": "6", "label": "FLAC CD"}],
}
# Declared as a plain string: its key name makes it write-only.
API_KEY = {"key": "apiKey", "label": "API key", "type": "string"}


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    nd.create_user(LISTENER, LISTENER_PASSWORD)
    # A second admin (Navidrome gives admins every library, so none are set).
    nd.native(
        "POST",
        "user",
        json={"userName": HELPER, "name": HELPER, "password": HELPER_PASSWORD, "isAdmin": True},
    )
    replay = Replay()
    tmp = tmp_path_factory.mktemp("dash")
    # The configuration names the catalog and where its token is (the adapter's settings).
    (tmp / "token").write_text("configured-token")
    settings = {"kind": "sample", "token_file": tmp / "token"}
    with delivery_world(nd, tmp, catalog=replay.catalog(), catalog_settings=settings) as w:
        # Tests sign in often; the limit itself has a unit test (as Navidrome's is raised).
        w.app.dashboard.attempts.limit = 10_000
        yield w


@pytest.fixture
def browser(world: DeliveryWorld) -> Iterator[Browser]:
    b = Browser(world.server.base_url)
    yield b
    b.close()


@pytest.fixture
def admin(browser: Browser) -> Browser:
    signed = browser.sign_in(ADMIN_USER, ADMIN_PASSWORD)
    assert signed.status_code == 303, signed.text
    return browser


def saved_rows(world: DeliveryWorld, section: str) -> dict[str, str]:
    async def read() -> dict[str, str]:
        rows = await world.services.store.fetchall(
            "SELECT key, value FROM saved_settings WHERE section = ?", [section]
        )
        return {row["key"]: row["value"] for row in rows}

    return world.server.call(read)


def row_of(html: str, field_id: str) -> str:
    """The setting row holding the field ``field_id``."""
    rows = re.split(r'(?=<div class="setting[ "])', html)
    found = [row for row in rows if f'id="{field_id}"' in row]
    assert found, field_id
    return found[0]


class _Folds(HTMLParser):
    """A page's field ids and names, under a "More settings" and outside it, and whether
    each "More settings" is open."""

    def __init__(self) -> None:
        super().__init__()
        self.inside: set[str] = set()
        self.outside: set[str] = set()
        self.open: list[bool] = []
        self._details: list[bool] = []  # per <details> around: whether it is one

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "details":
            more = "more-settings" in a.get("class", "").split()
            self._details.append(more)
            if more:
                self.open.append("open" in a)
        found = {a[k] for k in ("id", "name") if a.get(k)}
        (self.inside if any(self._details) else self.outside).update(found)

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self._details.pop()


def folds(page: str) -> _Folds:
    parser = _Folds()
    parser.feed(page)
    return parser


def units_said_twice(page: str) -> list[str]:
    """Setting labels whose unit for screen readers only repeats words of the label."""
    labels = re.findall(
        r'<label class="setting-label"[^>]*>([^<]*)'
        r'<span class="visually-hidden"> \(([^)]*)\)</span></label>',
        page,
    )
    return [
        f"{label} ({unit})"
        for label, unit in labels
        if set(re.findall(r"\w+", html.unescape(unit).lower()))
        <= set(re.findall(r"\w+", html.unescape(label).lower()))
    ]


# --- sign-in and admin rights ----------------------------------------------------------


def test_pages_need_a_signed_in_admin(browser: Browser, world: DeliveryWorld) -> None:
    for page in PAGES:
        response = browser.get(page)
        assert response.status_code == 303
        assert response.headers["location"] == f"{PATH}/sign-in?next={PATH}/{page}"
    before = world.services.deliverer.settings.primary_budget_seconds
    posted = browser.post("playback", {"primary_budget_seconds": "1", "csrf": "x"})
    assert posted.status_code == 303 and "sign-in" in posted.headers["location"]
    assert world.services.deliverer.settings.primary_budget_seconds == before
    assert browser.get("static/shijhon.css").headers["content-type"].startswith("text/css")
    assert browser.get("static/fonts/OFL.txt").status_code == 200
    page = browser.get("sign-in")
    assert page.status_code == 200 and 'data-accent="' in page.text
    assert "Use a Navidrome account with admin rights." in page.text


def test_non_admins_and_wrong_passwords_are_refused(browser: Browser) -> None:
    refused = browser.sign_in(LISTENER, LISTENER_PASSWORD)
    assert refused.status_code == 403
    assert "no admin rights in Navidrome" in refused.text
    assert "shijhon_session" not in refused.headers.get("set-cookie", "")
    assert browser.get("addons").status_code == 303
    wrong = browser.sign_in(ADMIN_USER, "not-the-password")
    assert wrong.status_code == 400 and "Wrong username or password" in wrong.text
    assert 'value="admin"' in wrong.text  # the username is kept, the password is not
    assert browser.get("addons").status_code == 303


def test_sign_in_needs_its_own_form(browser: Browser) -> None:
    """A sign-in posted from elsewhere (no sign-in cookie and token) is refused."""
    response = browser.post(
        "sign-in", {"signin": "made-up", "username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 400 and "shijhon_session" not in str(response.headers)
    assert browser.get("addons").status_code == 303


def test_an_admin_who_loses_admin_rights_is_signed_out(
    world: DeliveryWorld, browser: Browser
) -> None:
    assert browser.sign_in(HELPER, HELPER_PASSWORD).status_code == 303
    assert browser.get("addons").status_code == 200
    users = {u["userName"]: u for u in world.nd.native("GET", "user").json()}
    helper = users[HELPER]
    world.nd.native("PUT", f"user/{helper['id']}", json={**helper, "isAdmin": False})
    try:
        world.app.dashboard.admins.forget(HELPER)  # instead of waiting a minute
        response = browser.get("addons")
        assert response.status_code == 303
        assert response.headers["location"] == f"{PATH}/sign-in?reason=admin"
        again = browser.get("sign-in?reason=admin")
        assert "no longer has admin rights" in again.text
        assert browser.get("addons").status_code == 303  # the session is gone
    finally:
        world.nd.native("PUT", f"user/{helper['id']}", json={**helper, "isAdmin": True})


def test_signing_out_ends_the_session(admin: Browser) -> None:
    assert admin.get("addons").status_code == 200
    out = admin.post("sign-out", {"csrf": admin.csrf()})
    assert out.status_code == 303 and out.headers["location"] == f"{PATH}/sign-in"
    assert admin.get("addons").status_code == 303


def test_forms_need_the_session_token(world: DeliveryWorld, admin: Browser) -> None:
    before = saved_rows(world, "appearance")
    assert admin.post("appearance", {"accent": "purple", "csrf": "forged"}).status_code == 403
    assert admin.post("appearance", {"accent": "purple"}).status_code == 403
    cross = admin.post(
        "appearance",
        {"accent": "purple", "csrf": admin.csrf()},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert cross.status_code == 403
    assert saved_rows(world, "appearance") == before


# --- the pages --------------------------------------------------------------------------


def test_every_page_names_its_author_its_license_and_its_source(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The footer of every page, the sign-in page and the page of an admin whose rights
    cannot be checked too: the version, the author, the license and a link to the
    source - the address Shijhon names itself to add-ons by, the one place that holds it."""
    legal = (
        f'<p class="legal">Shijhon <code>{__version__}</code> \u00b7 \u00a9 2026 Jasshl \u00b7'
        f' AGPL-3.0-or-later \u00b7 <a href="{PROJECT_URL}" rel="noreferrer">Source</a></p>'
    )
    assert PROJECT_URL == "https://github.com/Jasshl/shijhon" and PROJECT_URL in USER_AGENT
    assert importlib.metadata.version("shijhon") == __version__ == "0.1.0b1"
    for page in PAGES:
        assert admin.get(page).text.count(legal) == 1, page

    async def unknown(navidrome: object, username: str) -> None:
        return None

    monkeypatch.setattr(world.app.dashboard.admins, "admin", unknown)
    unverified = admin.get("addons")
    assert unverified.status_code == 503 and unverified.text.count(legal) == 1
    monkeypatch.undo()
    stranger = Browser(world.server.base_url)
    try:
        assert stranger.get("sign-in").text.count(legal) == 1
        wrong = stranger.sign_in(ADMIN_USER, "not-the-password")
        assert wrong.status_code != 303 and wrong.text.count(legal) == 1
    finally:
        stranger.close()


def test_every_page_renders(admin: Browser) -> None:
    assert admin.get("").headers["location"] == f"{PATH}/addons"
    for page, heading in PAGES.items():
        response = admin.get(page)
        assert response.status_code == 200, (page, response.text)
        html = response.text
        assert f"<h1>{heading}</h1>" in html
        assert f"<title>{heading} \u2013 Shijhon</title>" in html
        assert re.search(r'<html lang="en" data-accent="[a-z]+">', html)
        if page != "appearance":
            assert f'<a href="{PATH}/{page}" aria-current="page">' in html
        else:
            assert f'<a href="{PATH}/appearance" aria-current="page">Appearance</a>' in html
        assert f'href="{PATH}/static/shijhon.css"' in html
        assert "Signed in as admin" in html
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "<script" not in html
        assert "built-in method" not in html and "bound method" not in html  # no Python objects
        # Every form that changes something carries the session's token.
        for action, fields in forms(html).items():
            assert fields.get("csrf"), (page, action)
        assert not units_said_twice(html), page
    library = admin.get("library").text  # this world matches no albums (fill is off)
    assert "Albums are not matched now" in library and 'id="review"' not in library
    assert 'name="auto_min_songs"' in library and 'name="library_pass"' in library


def test_the_playback_page_is_generated_from_the_settings(admin: Browser) -> None:
    html = admin.get("playback").text
    from shijhon.config import DeliverySettings

    cleanup = admin.get("cleanup").text
    for key in DeliverySettings.model_fields:
        if key in ("delivered_days", "delivered_gb"):  # downloaded audio: on Cleanup
            assert f'name="{key}"' not in html and f'name="{key}"' in cleanup, key
        else:
            assert f'name="{key}"' in html, key
    budget = row_of(html, "delivery-budget-seconds")
    # The harness configures 2.5 s: the configuration's value is the default here.
    assert 'value="2.5"' in budget and "Default 2.5 s, from the configuration" in budget
    assert 'inputmode="decimal"' in budget and 'type="text"' in budget
    assert 'type="number"' not in html
    timeouts = row_of(html, "delivery-primary-cooldown-timeouts")
    assert 'inputmode="numeric"' in timeouts and "Default 2" in timeouts
    routing = row_of(html, "delivery-routing")
    assert '<option value="primary_first">Primary first</option>' in routing
    assert "tag-restart" not in row_of(html, "delivery-ahead-window-seconds")  # live
    # A screen reader hears a unit the label does not say already, and only then.
    assert '>Time to first audio<span class="visually-hidden"> (seconds)</span></label>' in html
    rate = row_of(html, "delivery-addon-requests-per-second")
    assert ">Requests a second</label>" in rate and 'class="unit" aria-hidden="true">/s<' in rate


def test_the_settings_most_people_change_come_first(world: DeliveryWorld, admin: Browser) -> None:
    """Each settings page starts with the settings most people change; the others are
    under one collapsed "More settings" (their group's ``more``), and none is left out. It
    opens for a row that has to be seen: a failed save's, or one saved just now."""
    from shijhon.dashboard import sections
    from shijhon.dashboard.app import DOWNLOAD_LIMITS
    from shijhon.dashboard.fields import GROUPS

    on_cleanup = folds(admin.get("cleanup").text)
    pages = {"playback": "delivery", "catalog": "catalog", "library": "fill"}
    for page, section in {**pages, "cleanup": "cleanup"}.items():
        found = folds(admin.get(page).text)
        assert found.open == ([False] if page in pages else []), page  # one, collapsed
        groups = {group.key: group for group in GROUPS[section]}
        for f in sections.fields_of(section, "sample" if section == "catalog" else "none"):
            here = {f.id, f.key}
            if page == "playback" and f.key in DOWNLOAD_LIMITS:  # downloaded audio: Cleanup
                assert here & on_cleanup.outside and not here & found.outside, f.key
                continue
            more = groups.get(f.meta.group, GROUPS[section][-1]).more
            shown, hidden = (found.inside, found.outside) if more else (found.outside, found.inside)
            assert here & shown and not here & hidden, (page, f.key, more)
    assert "search-budget-seconds" in folds(admin.get("catalog").text).inside  # the wait
    # An add-on's settings: its own first, then where it runs and its address; its time and
    # limits under its "More settings".
    world.clear_sources()
    try:
        source = world.add_source(world.addon("Folded", settings=[QUALITY]))
        page = admin.get("addons").text
        found, prefix = folds(page), f"addon-{source}"
        shown = {f"{prefix}-s-quality", f"{prefix}-reach", f"{prefix}-manifest"}
        assert shown <= found.outside and found.open == [False]
        assert page.index(f'id="{prefix}-s-quality"') < page.index(f'id="{prefix}-reach"')
        limits = ("budget", "requests-per-second", "request-burst", "audio-openings")
        assert {f"{prefix}-{name}" for name in limits} <= found.inside
        # Open once after a change of its time or limits, not of the settings shown first.
        action = f"addons/{source}/settings"
        assert admin.submit("addons", {"budget_seconds": "3"}, action).status_code == 303
        page = admin.get("addons").text
        assert folds(page).open == [True] and ">1 changed</span>" in page
        assert folds(admin.get("addons").text).open == [False]
        assert admin.submit("addons", {"setting.quality": "5"}, action).status_code == 303
        assert folds(admin.get("addons").text).open == [False]
    finally:
        world.clear_sources()
    # Open for a failed save of one of its settings, not of one shown first.
    assert folds(admin.submit("playback", {"primary_budget_seconds": "abc"}).text).open == [True]
    assert folds(admin.submit("playback", {"budget_seconds": "abc"}).text).open == [False]
    try:  # and for one saved just now, once; it says how many are not at their default
        assert admin.submit("playback", {"warm_ahead_depth": "3"}).status_code == 303
        saved = admin.get("playback").text
        assert folds(saved).open == [True] and ">1 changed</span>" in saved
        again = admin.get("playback").text
        assert folds(again).open == [False] and ">1 changed</span>" in again
    finally:
        assert admin.submit("playback", {"warm_ahead_depth": "2"}).status_code == 303
    assert "changed</span>" not in admin.get("playback").text


def test_number_fields_take_a_comma_or_a_dot(world: DeliveryWorld, admin: Browser) -> None:
    settings = world.services.deliverer.settings
    try:
        saved = admin.submit("playback", {"primary_budget_seconds": "1,5"})
        assert saved.status_code == 303
        assert settings.primary_budget_seconds == 1.5
        page = admin.get("playback").text
        row = row_of(page, "delivery-primary-budget-seconds")
        assert 'value="1.5"' in row and "tag-saved" in row and "is-modified" in row
        assert "Saved 1 change at" in page
        assert admin.submit("playback", {"primary_budget_seconds": " 1.25 "}).status_code == 303
        assert settings.primary_budget_seconds == 1.25
        assert admin.submit("playback", {"warm_ahead_depth": "3"}).status_code == 303
        assert settings.warm_ahead_depth == 3
        assert admin.submit("playback", {"warm_ahead_depth": "3,5"}).status_code == 400
        assert settings.warm_ahead_depth == 3
    finally:
        admin.submit("playback", {"primary_budget_seconds": "4.5", "warm_ahead_depth": "2"})
    assert "primary_budget_seconds" not in saved_rows(world, "delivery")  # the default again


def test_invalid_values_save_nothing_and_say_why(world: DeliveryWorld, admin: Browser) -> None:
    settings = world.services.deliverer.settings
    before = (settings.primary_budget_seconds, settings.max_wait_seconds)
    bad = admin.submit("playback", {"primary_budget_seconds": "abc", "cooldown_seconds": "5"})
    assert bad.status_code == 400
    assert "Nothing saved: 1 setting needs a different value" in bad.text
    row = row_of(bad.text, "delivery-primary-budget-seconds")
    assert "is-invalid" in row and 'value="abc"' in row and 'aria-invalid="true"' in row
    assert "Enter a number, e.g. 4.5." in row
    assert 'href="#delivery-primary-budget-seconds"' in bad.text
    assert "Not saved" in bad.text
    across = admin.submit("playback", {"max_wait_seconds": "2"})  # below the 2.5 s budget
    assert across.status_code == 400
    wait = row_of(across.text, "delivery-max-wait-seconds")
    assert "Must be at least the time to first audio (2.5 s)" in wait
    assert "is below the time to first audio." in across.text
    low = admin.submit("playback", {"primary_cooldown_timeouts": "0"})
    assert "Enter a whole number of at least 1." in low.text
    assert (settings.primary_budget_seconds, settings.max_wait_seconds) == before
    assert settings.cooldown_seconds == 1.5  # the valid change in the failed form: not saved


def test_routing_changes_take_effect_at_once(world: DeliveryWorld, admin: Browser) -> None:
    world.clear_sources()
    first, second = world.addon("RouteFirst"), world.addon("RouteSecond")
    world.add_source(first)
    world.add_source(second)
    song, _, audio = world.placeholder_track("dash-route", [first, second])
    try:
        assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
        assert first.requests("audio") and not second.requests("audio")
        saved = admin.submit(
            "playback", {"routing": "primary_first", "primary_source": "RouteSecond"}
        )
        assert saved.status_code == 303
        assert world.services.deliverer.settings.routing == "primary_first"
        world.services.deliverer.forget(song)
        first.clear()
        assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
        assert second.requests("audio") and not first.requests("audio")
    finally:
        admin.submit("playback", {"routing": "ordered", "primary_source": ""})
        world.clear_sources()
    assert world.services.deliverer.settings.routing == "ordered"


def test_the_limits_per_addon_take_effect_at_once(world: DeliveryWorld, admin: Browser) -> None:
    """What one add-on is sent in all - set on the Playback page for every add-on
    without limits of its own, from the next request on."""
    world.clear_sources()
    addon = world.addon("Paced")
    world.add_source(addon)
    sources = world.services.sources
    (source,) = world.server.call(sources.enabled)
    before = sources.paces.defaults
    assert source.pace is not None and source.pace.limits == before
    page = admin.get("playback").text
    assert "Limits per add-on" in page
    for row in ("addon-requests-per-second", "addon-request-burst", "addon-audio-openings"):
        assert "tag-restart" not in row_of(page, f"delivery-{row}"), row
    try:
        saved = admin.submit(
            "playback",
            {
                "addon_requests_per_second": "1,5",
                "addon_request_burst": "3",
                "addon_audio_openings": "2",
            },
        )
        assert saved.status_code == 303
        assert sources.paces.defaults == Limits(1.5, 3, 2)
        assert source.pace.limits == Limits(1.5, 3, 2)  # the add-on's count goes on with them
        assert admin.submit("playback", {"addon_request_burst": "0"}).status_code == 400
        assert admin.submit("playback", {"addon_requests_per_second": "-2"}).status_code == 400
        assert source.pace.limits == Limits(1.5, 3, 2)
    finally:
        back = {
            "addon_requests_per_second": str(before.requests_per_second),
            "addon_request_burst": str(before.request_burst),
            "addon_audio_openings": str(before.audio_openings),
        }
        assert admin.submit("playback", back).status_code == 303
        world.clear_sources()
    assert source.pace.limits == before


def test_a_rate_limited_manifest_check_leaves_the_addon_alone(
    world: DeliveryWorld, admin: Browser
) -> None:
    """The dashboard's own requests honor "too many requests" like every other - the
    add-on is left alone for the time it names, by playback too - and are not sent while
    it is."""
    world.clear_sources()
    addon = world.addon("Busy")
    source = world.add_source(addon)
    sources = world.services.sources
    try:
        addon.manifest_status, addon.retry_after = 429, "40"
        page = admin.get("addons").text
        assert "Manifest unreachable:" in page and "HTTP 429" in page
        assert len(addon.requests("manifest")) == 1
        world.server.call(sources.enabled)
        until = sources.cooling_until(source)
        assert until is not None and 30 < until - time.time() <= 40.5
        # While it is left alone the page asks nothing (what it read before stands).
        addon.manifest_status = None
        world.app.dashboard.checks.every = 0
        assert "HTTP 429" in admin.get("addons").text
        assert len(addon.requests("manifest")) == 1
        # An answer that names no time: the configured cooldown, as for playback.
        quiet = world.addon("Quiet")
        other = world.add_source(quiet)
        quiet.manifest_status, quiet.retry_after = 429, None
        admin.get("addons")
        world.server.call(sources.enabled)
        until = sources.cooling_until(other)
        cooldown = world.services.deliverer.settings.cooldown_seconds
        assert until is not None and 0 < until - time.time() <= cooldown + 0.1
    finally:
        world.app.dashboard.checks.every = 60.0
        world.clear_sources()


def test_the_download_burst_and_the_jobs_at_once_take_effect_at_once(
    world: DeliveryWorld, admin: Browser
) -> None:
    """How many downloads a user may start in a row, and how many warm-ahead jobs and DASH
    joins run at once for everyone."""
    limits = world.services.download_first.limits
    warm = world.services.interceptor.warm
    dash = world.services.deliverer.dash
    assert limits is not None and warm is not None and dash is not None
    before = (limits.burst, warm.jobs, dash.joins_at_once)
    assert (warm.jobs, dash.joins_at_once) == (2, 3)  # the built-in defaults
    page = admin.get("playback").text
    assert "tag-restart" not in row_of(page, "delivery-user-download-burst")
    assert "tag-restart" not in row_of(page, "delivery-warm-ahead-jobs")
    assert "tag-restart" not in row_of(page, "delivery-dash-joins-at-once")
    try:
        saved = admin.submit(
            "playback",
            {"user_download_burst": "6", "warm_ahead_jobs": "1", "dash_joins_at_once": "1"},
        )
        assert saved.status_code == 303
        assert (limits.burst, warm.jobs, dash.joins_at_once) == (6, 1, 1) and limits.most == 6
        assert admin.submit("playback", {"warm_ahead_jobs": "0"}).status_code == 400
        assert admin.submit("playback", {"dash_joins_at_once": "0"}).status_code == 400
        assert (warm.jobs, dash.joins_at_once) == (1, 1)
    finally:
        back = {
            "user_download_burst": str(before[0]),
            "warm_ahead_jobs": str(before[1]),
            "dash_joins_at_once": str(before[2]),
        }
        assert admin.submit("playback", back).status_code == 303
    assert (limits.burst, warm.jobs, dash.joins_at_once) == before


def test_restart_only_settings_say_so(world: DeliveryWorld, admin: Browser) -> None:
    page = admin.get("catalog").text
    assert "tag-restart" in row_of(page, "catalog-region")
    assert "tag-restart" not in row_of(page, "catalog-artwork-size")
    try:
        assert admin.submit("catalog", {"region": "GB"}).status_code == 303
        assert saved_rows(world, "catalog")["region"] == '"gb"'
        page = admin.get("addons").text
        assert "<strong>Restart needed.</strong> 1 saved setting applies after Shijhon" in page
        assert "restarts: region." in page
        assert "Region, saved" in admin.get("diagnostics").text
        assert admin.submit("catalog", {"artwork_size": "600"}).status_code == 303
        assert world.services.commits is not None
        assert world.services.commits.artwork_size == 600  # live
    finally:
        admin.submit("catalog", {"region": "xx", "artwork_size": "1200"})
    assert saved_rows(world, "catalog") == {}
    assert "Restart needed" not in admin.get("addons").text


def test_the_catalog_page_checks_and_keeps_its_token_secret(
    world: DeliveryWorld, admin: Browser
) -> None:
    page = admin.get("catalog").text
    assert "Answering, checked" in page
    assert admin.post("catalog/check", {"csrf": admin.csrf()}).status_code == 303
    token_row = row_of(page, "catalog-token")
    assert 'type="password"' in token_row and 'autocomplete="new-password"' in token_row
    assert "Not set" in token_row
    assert "Catalog not connected" not in page
    missing = admin.submit("catalog", {"kind": "sample", "token_file": ""})
    assert missing.status_code == 400
    assert "Set a token, a token file or a token service" in missing.text
    unreadable = admin.submit("catalog", {"kind": "sample", "token_file": "/nonexistent/x"})
    assert "can't read a token from this file" in html.unescape(unreadable.text)
    assert saved_rows(world, "catalog") == {}  # nothing of a refused save is kept
    # Another catalog cannot be chosen unless it is installed.
    unknown = admin.submit("catalog", {"kind": "elsewhere"})
    assert unknown.status_code == 400 and "Choose one of the listed values" in unknown.text


def test_choosing_another_catalog_shows_its_settings_and_what_it_says(
    world: DeliveryWorld, admin: Browser
) -> None:
    """The Catalog page's settings are the chosen adapter's: its form says whose
    settings it holds, another catalog's page has none of them, and an adapter's notice
    (the demo: its data is invented) is on the page."""
    page = admin.get("catalog").text
    assert 'name="shown-kind" value="sample"' in page
    assert "Sample catalog, region <code>xx</code>" in admin.get("diagnostics").text
    assert ">Demo (invented data)</option>" in page and "are invented" not in page
    try:
        assert admin.submit("catalog", {"kind": "demo"}).status_code == 303
        assert saved_rows(world, "catalog") == {"kind": '"demo"'}
        page = admin.get("catalog").text
        assert 'name="shown-kind" value="demo"' in page
        assert 'id="catalog-token"' not in page and 'id="catalog-region"' not in page
        assert "its artists, albums and songs are invented" in page
        assert "restarts: catalog." in admin.get("addons").text
        # The sample catalog's token typed into the demo's page is not saved as anything.
        admin.submit("catalog", {"token": SECRET_TOKEN})
        assert saved_rows(world, "catalog") == {"kind": '"demo"'}
    finally:
        assert admin.submit("catalog", {"kind": "sample"}).status_code == 303
    assert saved_rows(world, "catalog") == {}
    assert 'id="catalog-token"' in admin.get("catalog").text


def test_an_add_on_s_catalog_is_a_kind_with_the_add_ons_to_choose_from(
    world: DeliveryWorld, admin: Browser
) -> None:
    """The kind is built in, and its one setting names one of the list's add-ons. Choosing
    the kind is the first of two steps: the page then has its setting to enter, with
    nothing refused and nothing saved; the save with the add-on stores both."""
    world.clear_sources()
    world.add_source(world.addon("Holder", catalog=FakeCatalog()))
    assert ">An add-on's catalog</option>" in html.unescape(admin.get("catalog").text)
    before = saved_rows(world, "catalog")
    try:
        first = admin.submit("catalog", {"kind": "addon"})
        assert first.status_code == 200
        page = html.unescape(first.text)
        assert "Nothing is saved yet. Enter this catalog's settings, then save." in page
        assert "Not saved" not in page and "Nothing saved" not in page
        assert "is-invalid" not in page and "field-error" not in page
        assert "aria-invalid" not in page and "Enter the name of the add-on" not in page
        assert 'name="shown-kind" value="addon"' in page
        row = row_of(first.text, "catalog-addon")
        assert "<select" in row and '<option value="Holder">Holder</option>' in row
        assert "It needs a catalog of its own" in row
        assert saved_rows(world, "catalog") == before == {}
        assert "Nothing is saved yet" not in admin.get("catalog").text  # (that answer only)
        # The second step, with the kind's setting on the form: without the add-on, or
        # with one that is not in the list, it is refused and the setting marked.
        chosen = {"kind": "addon", "shown-kind": "addon"}
        empty = admin.submit("catalog", {**chosen, "addon": ""})
        assert empty.status_code == 400 and "Not saved" in empty.text
        assert "Nothing is saved yet" not in empty.text
        row = row_of(empty.text, "catalog-addon")
        assert (
            "is-invalid" in row and "Enter the name of the add-on to take the catalog from." in row
        )
        unknown = admin.submit("catalog", {**chosen, "addon": "Nobody"})
        assert unknown.status_code == 400 and "Choose one of the listed add-ons." in unknown.text
        assert saved_rows(world, "catalog") == {}
        assert admin.submit("catalog", {**chosen, "addon": "Holder"}).status_code == 303
        # (With the kind its settings are for, as every adapter's saved settings.)
        assert saved_rows(world, "catalog") == {
            "kind": '"addon"',
            "addon": '"Holder"',
            "(adapter)": '"addon"',
        }
        page = admin.get("catalog").text
        assert '<option value="Holder" selected>Holder</option>' in page
        assert "Restart needed" in admin.get("addons").text  # the catalog starts then
    finally:
        assert admin.submit("catalog", {"kind": "sample"}).status_code == 303
    assert saved_rows(world, "catalog") == {}


def test_the_wait_for_the_catalog_is_set_on_the_catalog_page(
    world: DeliveryWorld, admin: Browser
) -> None:
    """How long a search or an artist page waits for the catalog (``[search]
    budget_seconds``): 8 seconds unless changed, a form of its own on the Catalog
    page, applied at once."""
    additions = world.services.additions
    assert additions is not None and additions.budget == 8.0
    page = admin.get("catalog").text
    row = row_of(page, "search-budget-seconds")
    assert "Wait for the catalog" in row and 'value="8"' in row and "Default 8 s" in row
    assert "goes out as soon as it has replied" in row and "Applies after restart" not in row
    try:
        for bad in ("0", "-1", "soon", "500"):
            refused = admin.submit("catalog", {"budget_seconds": bad}, "catalog/wait")
            assert refused.status_code == 400 and "Not saved" in refused.text, bad
        assert saved_rows(world, "search") == {} and additions.budget == 8.0
        saved = admin.submit("catalog", {"budget_seconds": "5,5"}, "catalog/wait")
        assert saved.status_code == 303
        assert saved_rows(world, "search") == {"budget_seconds": "5.5"}
        assert additions.budget == 5.5  # at once: nothing waits for a restart
        page = admin.get("catalog").text
        assert "Restart needed" not in page and 'value="5.5"' in page
        # The catalog's own form leaves it alone, and it leaves the catalog's settings.
        assert admin.submit("catalog", {"cache_seconds": "3600"}).status_code == 303
        assert saved_rows(world, "search") == {"budget_seconds": "5.5"}
    finally:
        back = admin.submit("catalog", {"budget_seconds": "8"}, "catalog/wait")
        assert back.status_code == 303
    assert saved_rows(world, "search") == {} and additions.budget == 8.0


# --- restart --------------------------------------------------------------------------------


@pytest.fixture
def restarts(world: DeliveryWorld) -> Iterator[list[str]]:
    """Shijhon as ``shijhon serve`` runs it where something starts it again: the restart
    is offered - here its last step only notes that it was asked for."""
    asked: list[str] = []
    world.app.restarter = lambda: asked.append("restart")
    try:
        yield asked
    finally:
        world.app.restarter = None
        world.app.restarting = False


def test_restart_is_one_press_for_a_signed_in_admin_only(
    world: DeliveryWorld, browser: Browser, restarts: list[str]
) -> None:
    """A POST with the session and its token, like every action: no GET, no form from
    elsewhere, nobody signed out."""
    assert browser.get("restart").status_code in (303, 405)  # no GET restarts anything
    signed_out = browser.post("restart", {"csrf": "x", "next": f"{PATH}/diagnostics"})
    assert signed_out.status_code == 303 and "/sign-in" in signed_out.headers["location"]
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    assert browser.get("restart").status_code == 405
    assert browser.post("restart", {"csrf": "not-the-token"}).status_code == 403
    cross = browser.post(
        "restart", {"csrf": browser.csrf()}, headers={"sec-fetch-site": "cross-site"}
    )
    assert cross.status_code == 403
    time.sleep(0.7)
    assert restarts == [] and not world.app.restarting

    page = browser.get("diagnostics").text
    assert ">Restart Shijhon</button>" in page and "docker compose restart" not in page
    assert "Songs that are playing may stop" in page
    pressed = browser.submit("diagnostics", {}, "restart")
    assert pressed.status_code == 303
    where = pressed.headers["location"]
    assert where.startswith(f"{PATH}/restarting?boot=") and where.endswith("diagnostics")
    waiting = browser.get(where)
    assert waiting.status_code == 200 and "Shijhon is restarting" in waiting.text
    assert "script-src 'self'" in waiting.headers["content-security-policy"]
    assert f'src="{PATH}/static/restart.js"' in waiting.text
    assert browser.get("static/restart.js").status_code == 200
    # Which run answers: asked by the waiting page, open like the sign-in page.
    run = Browser(world.server.base_url)
    try:
        alive = run.get("alive")
        assert alive.status_code == 200 and f'data-boot="{alive.text}"' in waiting.text
    finally:
        run.close()
    # Pressed again while it is under way: the waiting page, and nothing more.
    again = browser.post("restart", {"csrf": browser.csrf(), "next": f"{PATH}/catalog"})
    assert again.status_code == 303 and "/restarting?" in again.headers["location"]
    time.sleep(0.8)
    assert restarts == ["restart"] and world.app.restarting
    # The next run - or a run that is not restarting - sends the page on to where it began.
    world.app.restarting = False
    on = browser.get(where)
    assert on.status_code == 303 and on.headers["location"] == f"{PATH}/diagnostics"
    other = browser.get("restarting?boot=0000&next=https://elsewhere.example/")
    assert other.status_code == 303 and other.headers["location"] == f"{PATH}/diagnostics"


def test_restart_is_offered_where_a_restart_is_asked_for(
    world: DeliveryWorld, admin: Browser, restarts: list[str]
) -> None:
    """In the "Restart needed" banner and next to a catalog that waits for a restart -
    and nowhere where nothing would start Shijhon again after it stopped."""
    world.clear_sources()
    world.add_source(world.addon("Holder", catalog=FakeCatalog()))
    chosen = {"kind": "addon", "shown-kind": "addon", "addon": "Holder"}
    try:
        assert admin.submit("catalog", chosen).status_code == 303
        page = admin.get("catalog").text
        banner = page[page.index('class="banner"') : page.index("</header>") + 2000]
        assert "Restart needed" in banner and ">Restart Shijhon</button>" in banner
        assert f'name="next" value="{PATH}/catalog"' in banner
        assert "How to restart" not in page
        world.app.restarter = None  # as from source, with nothing that starts it again
        page = admin.get("catalog").text
        assert "How to restart" in page and "Restart Shijhon" not in page
        assert "docker compose restart" in admin.get("diagnostics").text
        refused = admin.post("restart", {"csrf": admin.csrf()})
        assert refused.status_code == 409
        assert "Nothing here starts Shijhon again after it stops" in refused.text
        assert "Shijhon was not restarted" in refused.text and "restart.js" not in refused.text
    finally:
        assert admin.submit("catalog", {"kind": "sample"}).status_code == 303
    assert restarts == []


def test_a_restart_that_would_not_come_back_is_refused_with_the_reason(
    world: DeliveryWorld, admin: Browser, restarts: list[str]
) -> None:
    """The configuration is read before anything is stopped: an error in it, or another
    address for Shijhon, is said on a page, and nothing is restarted. While Shijhon is
    stopping, a press says so."""
    reason = "the configuration has an error - in search.budget_seconds: too large"
    world.app.restart_check = lambda: reason
    try:
        refused = admin.post("restart", {"csrf": admin.csrf(), "next": f"{PATH}/catalog"})
        assert refused.status_code == 409 and "Shijhon was not restarted" in refused.text
        assert f"Nothing was stopped: {reason}. Correct it, then restart Shijhon." in refused.text
        assert f'<a href="{PATH}/catalog">Back</a>' in refused.text
        assert "restart.js" not in refused.text
        time.sleep(0.7)
        assert restarts == [] and not world.app.restarting
        assert admin.get("diagnostics").status_code == 200  # (the session holds)
        world.app.restart_check = lambda: None
        background, world.app._background = world.app._background, None  # as while it stops
        try:
            stopping = admin.post("restart", {"csrf": admin.csrf()})
        finally:
            world.app._background = background
        assert stopping.status_code == 409
        assert "Shijhon is stopping. It was not restarted." in stopping.text
        assert restarts == [] and not world.app.restarting
        # ... also while the server still lets open requests end (a stop signal came).
        world.app.stopping = lambda: True
        try:
            draining = admin.post("restart", {"csrf": admin.csrf()})
        finally:
            world.app.stopping = None
        assert draining.status_code == 409
        assert "Shijhon is stopping. It was not restarted." in draining.text
        time.sleep(0.7)
        assert restarts == [] and not world.app.restarting
    finally:
        world.app.restart_check = None


def test_a_catalog_chosen_on_the_dashboard_starts_with_one_press(
    world: DeliveryWorld, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No catalog yet: an add-on is added, its catalog chosen, and the page offers the
    restart that starts it, next to "Not connected yet"."""
    with delivery_world(world.nd, tmp_path_factory.mktemp("first-catalog")) as fresh:
        asked: list[str] = []
        fresh.app.restarter = lambda: asked.append("restart")
        fresh.add_source(fresh.addon("Holder", catalog=FakeCatalog()))
        admin = Browser(fresh.server.base_url)
        try:
            assert admin.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
            assert "Restart Shijhon" not in admin.get("catalog").text
            # (The first step: the page with the kind's setting; nothing saved yet.)
            assert admin.submit("catalog", {"kind": "addon"}).status_code == 200
            chosen = {"kind": "addon", "shown-kind": "addon", "addon": "Holder"}
            assert admin.submit("catalog", chosen).status_code == 303
            page = admin.get("catalog").text
            status = page[page.index('id="status-title"') :]
            status = status[: status.index("</section>")]
            assert "Not connected yet: the catalog starts when Shijhon restarts" in status
            assert ">Restart Shijhon</button>" in status and "Check now" not in status
            pressed = admin.submit("catalog", {}, "restart")
            assert pressed.status_code == 303 and pressed.headers["location"].endswith("catalog")
            time.sleep(0.8)
            assert asked == ["restart"]
        finally:
            admin.close()


# --- add-ons ------------------------------------------------------------------------------


def test_an_addon_not_for_downloads_says_so_on_its_row(
    world: DeliveryWorld, admin: Browser
) -> None:
    """The manifest's ``allowDownloads: 0``: a short note on that add-on's row."""
    world.clear_sources()
    shy, plain = world.addon("NotForDownloads"), world.addon("ForDownloads")
    shy.manifest_extra = {"allowDownloads": 0}
    try:
        first, second = world.add_source(shy), world.add_source(plain)
        page = admin.get("addons").text
        mine, others = page.split(f'id="addon-{first}"', 1)[1].split(f'id="addon-{second}"', 1)
        note = "Not used for downloads (the add-on asks this)"
        assert note in mine and note not in others
    finally:
        world.clear_sources()


def test_addons_are_added_ordered_switched_and_set(world: DeliveryWorld, admin: Browser) -> None:
    world.clear_sources()
    keep = world.addon("Keeper")
    world.add_source(keep)
    addon = world.addon("Dashy", settings=[QUALITY])
    sources = world.services.sources
    try:
        refused = admin.submit("addons", {"manifest": addon.base_url, "reach": "public"})
        assert refused.status_code == 400 and "no add-on answers" in refused.text
        assert admin.submit("addons", {"manifest": "not a url"}).status_code == 400
        added = admin.submit("addons", {"manifest": addon.base_url, "reach": "loopback"})
        assert added.status_code == 303
        page = admin.get("addons").text
        assert "Dashy added at the end of the list." in page
        assert "Version <code>1.0.0</code>" in page
        # Its manifest was read when it was added: the page does not ask for it again.
        assert len(addon.requests("manifest")) == 1
        names = [s.name for s in world.server.call(sources.enabled)]
        assert names == ["Keeper", "Dashy"]
        again = admin.submit("addons", {"manifest": addon.base_url, "reach": "loopback"})
        assert "is in the list already" in again.text
        dashy = next(s for s in world.server.call(sources.enabled) if s.name == "Dashy")
        action = f"addons/{dashy.id}/settings"
        fields = admin.form("addons", action)
        assert fields["setting.quality"] == "6"  # the manifest's default
        quality = row_of(admin.get("addons").text, f"addon-{dashy.id}-s-quality")
        assert '<option value="5">MP3 320</option>' in quality
        saved = admin.submit("addons", {"setting.quality": "5", "budget_seconds": "3,5"}, action)
        assert saved.status_code == 303
        dashy = next(s for s in world.server.call(sources.enabled) if s.name == "Dashy")
        assert dashy.budget == 3.5 and dashy.addon.settings == {"quality": "5"}
        bad = admin.submit("addons", {"budget_seconds": "0"}, action)
        assert bad.status_code == 400 and "Dashy settings not saved" in bad.text
        assert "Enter a number above 0, up to 600." in bad.text
        # Its own limits on what Shijhon sends it: empty is the installation's.
        fields = admin.form("addons", action)
        own = ("requests_per_second", "request_burst", "audio_openings")
        assert [fields[name] for name in own] == ["", "", ""]
        addons_page = admin.get("addons").text
        limits_row = row_of(addons_page, f"addon-{dashy.id}-requests-per-second")
        assert "Default: as every add-on" in limits_row and "is-modified" not in limits_row
        assert ">Requests a second</label>" in limits_row
        assert not units_said_twice(addons_page)
        saved = admin.submit(
            "addons", {"requests_per_second": "7,5", "request_burst": "12"}, action
        )
        assert saved.status_code == 303
        dashy = next(s for s in world.server.call(sources.enabled) if s.name == "Dashy")
        assert dashy.budget == 3.5 and dashy.addon.settings == {"quality": "5"}  # kept
        shared = sources.paces.defaults
        assert dashy.pace is not None
        assert dashy.pace.limits == Limits(7.5, 12, shared.audio_openings)
        fields = admin.form("addons", action)
        assert [fields[name] for name in own] == ["7.5", "12", ""]
        for name, value in (
            ("requests_per_second", "-1"),
            ("request_burst", "0"),
            ("request_burst", "2.5"),
            ("audio_openings", "1001"),
        ):
            bad = admin.submit("addons", {name: value}, action)
            assert bad.status_code == 400 and "Dashy settings not saved" in bad.text, name
        cleared = admin.submit(
            "addons",
            {"requests_per_second": "", "request_burst": "", "audio_openings": "0"},
            action,
        )
        assert cleared.status_code == 303
        dashy = next(s for s in world.server.call(sources.enabled) if s.name == "Dashy")
        assert dashy.pace is not None
        assert dashy.pace.limits == Limits(shared.requests_per_second, shared.request_burst, 0)
        up = admin.post(f"addons/{dashy.id}/move", {"direction": "up", "csrf": admin.csrf()})
        assert up.status_code == 303
        assert [s.name for s in world.server.call(sources.enabled)] == ["Dashy", "Keeper"]
        off = admin.post(f"addons/{dashy.id}/enabled", {"enabled": "false", "csrf": admin.csrf()})
        assert off.status_code == 303
        assert [s.name for s in world.server.call(sources.enabled)] == ["Keeper"]
        assert 'aria-checked="false" aria-label="Dashy enabled"' in admin.get("addons").text
        admin.post(f"addons/{dashy.id}/enabled", {"enabled": "true", "csrf": admin.csrf()})
        removed = admin.post(f"addons/{dashy.id}/remove", {"csrf": admin.csrf()})
        assert removed.status_code == 303
        assert [s.name for s in world.server.call(sources.enabled)] == ["Keeper"]
        assert "Dashy removed." in admin.get("addons").text
    finally:
        world.clear_sources()


# Declared settings of every kind, some without a default.
BITRATE = {"key": "bitrate", "label": "Bitrate", "type": "number", "default": 320}
LOSSLESS = {"key": "lossless", "label": "Lossless", "type": "toggle"}
CODEC = {
    "key": "codec",
    "label": "Codec",
    "type": "select",
    "options": [{"value": "flac", "label": "FLAC"}, {"value": "opus", "label": "Opus"}],
}
MIRRORS = {"key": "mirrors", "label": "Mirrors", "type": "text"}


def stored_addon(world: DeliveryWorld, source_id: int) -> StoredSource:
    return next(s for s in world.server.call(world.services.sources.stored) if s.id == source_id)


def test_a_new_host_gets_none_of_the_old_ones_secrets(world: DeliveryWorld, admin: Browser) -> None:
    """Changing an add-on's address to another host keeps only the settings its manifests
    declare plain: its secrets (declared so, or named like a key) and anything no manifest
    declares go, unless typed again in the same form - also when the old manifest cannot be
    read."""
    world.clear_sources()
    badge = {"key": "badge", "label": "Badge", "type": "password"}  # a secret by its type only
    level = {"key": "level", "label": "Level", "type": "number"}
    old = world.addon("Mover", settings=[QUALITY, API_KEY, badge, level])
    # The same add-on, elsewhere - whose manifest calls the badge plain text.
    plain = {**badge, "type": "text", "secret": False}
    new = world.addon("Mover", settings=[QUALITY, API_KEY, plain, level])
    stored = {
        "quality": "5",
        "region": "eu",  # declared by neither
        "apiKey": SECRET_SETTING,
        "session": SECRET_KEY_2,  # named like a secret
        "badge": "4711",
        "level": 256,  # not entered on the form: kept unshown
    }
    source = world.add_source(old, settings=stored)
    action = f"addons/{source}/settings"
    try:
        # Another address at the same host: all stay.
        same = admin.submit("addons", {"manifest": f"{old.base_url}/?v=2"}, action)
        assert same.status_code == 303
        assert stored_addon(world, source).settings == stored
        # Another host (here another port): only what is plain stays, and what is typed
        # again in the same form - which stays unshown there, whatever the new manifest
        # declares it (it was typed into a write-only field).
        moved = admin.submit(
            "addons",
            {
                "manifest": new.base_url,
                "setting.apiKey": "typed-again-7",
                "setting.badge": "9911",
                "setting.level": "192",  # entered again in the same form: it goes along
            },
            action,
        )
        assert moved.status_code == 303
        assert stored_addon(world, source).settings == {
            "quality": "5",
            "apiKey": "typed-again-7",
            "badge": "9911",
            "level": 192,
        }
        page = admin.get("addons").text
        assert "2 setting(s) its manifest does not declare plain were removed" in page
        assert "9911" not in page and 'value="192"' not in page
        assert 'type="password"' in row_of(page, f"addon-{source}-s-badge")
        # ... also when its manifest then offers it as one of its choices.
        new.settings = [QUALITY, API_KEY, {**badge, "type": "select", "options": ["9911", "x"]}]
        world.app.dashboard.checks.forget(source)
        chosen = row_of(admin.get("addons").text, f"addon-{source}-s-badge")
        assert "none of these (kept)" in chosen
        assert '<option value="9911" selected>' not in chosen
        assert admin.submit("addons", {"budget_seconds": "2"}, action).status_code == 303
        assert stored_addon(world, source).settings["badge"] == "9911"
        new.settings = [QUALITY, API_KEY, plain, level]
        world.app.dashboard.checks.forget(source)
        # Its manifest unreadable now: moved again, nothing is known to be plain.
        new.stop()
        world.app.dashboard.checks.forget(source)
        back = admin.submit("addons", {"manifest": old.base_url}, action)
        assert back.status_code == 303
        assert stored_addon(world, source).settings == {}
        for response in admin.seen:
            assert SECRET_SETTING not in response.text and SECRET_KEY_2 not in response.text
    finally:
        world.clear_sources()


def test_a_typed_secret_is_never_stored_without_its_mark(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An add-on's settings and the marks of which may be shown are
    saved in one transaction. A save whose marks cannot be written stores nothing - so a
    later manifest offering the secret among its choices never finds it stored unmarked."""
    from shijhon.dashboard.saved import SavedSettings

    world.clear_sources()
    badge = {"key": "badge", "label": "Badge", "type": "password"}
    addon = world.addon("Marked", settings=[QUALITY, badge])
    source = world.add_source(addon, settings={"quality": "5", "badge": "4711"})
    action = f"addons/{source}/settings"

    async def fails(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    try:
        with monkeypatch.context() as patched:
            patched.setattr(SavedSettings, "write", fails)
            patched.setattr(SavedSettings, "put", fails)
            refused = admin.submit("addons", {"setting.badge": "9911"}, action)
        assert refused.status_code == 500
        assert stored_addon(world, source).settings == {"quality": "5", "badge": "4711"}
        assert str(source) not in saved_rows(world, "addon-shown")
        # The same save, written: the secret and its mark are both there.
        assert admin.submit("addons", {"setting.badge": "9911"}, action).status_code == 303
        assert stored_addon(world, source).settings["badge"] == "9911"
        assert "hidden:" in saved_rows(world, "addon-shown")[str(source)]
        addon.settings = [QUALITY, {**badge, "type": "select", "options": ["9911", "x"]}]
        world.app.dashboard.checks.forget(source)
        chosen = row_of(admin.get("addons").text, f"addon-{source}-s-badge")
        assert '<option value="9911" selected>' not in chosen
    finally:
        world.clear_sources()


def test_a_form_older_than_the_addon_is_not_saved(world: DeliveryWorld, admin: Browser) -> None:
    """Saves of one add-on run one at a time, and a form made before a change of the
    add-on (a save, a move, another manifest) is not saved: a budget from a page loaded
    before a move brings back none of the settings the move removed, nor does a field a
    new manifest calls secret."""
    world.clear_sources()
    tier_text = {"key": "tier", "label": "Tier", "type": "text", "secret": False}
    tier_secret = {"key": "tier", "label": "Tier", "type": "password"}
    old = world.addon("Racer", settings=[QUALITY, API_KEY, tier_text])
    new = world.addon("Racer", settings=[QUALITY, API_KEY, tier_secret])
    new.manifest_delay = 1.0  # the move reads the new host's manifest for a while
    stored = {"quality": "5", "apiKey": SECRET_SETTING}
    source = world.add_source(old, settings=stored)
    action = f"addons/{source}/settings"
    try:
        # (Typed into its plain field: shown from then on.)
        assert admin.submit("addons", {"setting.tier": "gold-7"}, action).status_code == 303
        stale = admin.form("addons", action)  # a page loaded before the move
        assert stale["setting.tier"] == "gold-7" and stale["version"]
        moving = admin.form("addons", action) | {"manifest": new.base_url}
        results: list[int] = []
        mover = threading.Thread(
            target=lambda: results.append(admin.post(action, moving).status_code)
        )
        mover.start()
        time.sleep(0.3)  # the move is reading the new manifest, holding the add-on
        refused = admin.post(action, stale | {"budget_seconds": "5"})
        mover.join()
        assert results == [303] and refused.status_code == 409
        assert "Racer settings not saved" in refused.text
        saved = stored_addon(world, source)
        assert saved.budget is None and saved.settings == {"quality": "5"}
        # The old form once more, the move done: still refused (its tier field was plain).
        again = admin.post(action, stale)
        assert again.status_code == 409 and stored_addon(world, source).settings == {"quality": "5"}
        # A form made now saves.
        assert admin.submit("addons", {"budget_seconds": "5"}, action).status_code == 303
        assert stored_addon(world, source).budget == 5
        for response in admin.seen:
            assert SECRET_SETTING not in response.text
        assert "gold-7" not in admin.get("addons").text  # a secret there now: never shown
    finally:
        world.clear_sources()


def test_a_form_older_than_its_manifest_is_not_saved(world: DeliveryWorld, admin: Browser) -> None:
    """A new version of the add-on's manifest (here another default) read since the form
    was made: the form is not saved, so an old default is not saved as a choice."""
    world.clear_sources()
    quality = dict(QUALITY)
    addon = world.addon("Versioned", settings=[quality])
    source = world.add_source(addon)
    action = f"addons/{source}/settings"
    checks = world.app.dashboard.checks
    try:
        old = admin.form("addons", action)
        quality["default"] = "5"  # the add-on's new version
        checks.every = 0
        admin.get("addons")  # another page reads the manifest again
        refused = admin.post(action, old | {"budget_seconds": "4"})
        assert refused.status_code == 409
        assert (
            stored_addon(world, source).settings == {}
            and stored_addon(world, source).budget is None
        )
    finally:
        checks.every = 60
        world.clear_sources()


def test_a_form_save_keeps_what_was_not_changed(world: DeliveryWorld, admin: Browser) -> None:
    """A save stores only the settings changed on the form, as their kind (a number, on or
    off): defaults are neither dropped nor frozen, lists and tables are kept."""
    world.clear_sources()
    addon = world.addon("Typed", settings=[QUALITY, BITRATE, LOSSLESS, CODEC, MIRRORS])
    stored = {
        "quality": "6",  # the manifest's default, set explicitly (the configuration file)
        "bitrate": 256,
        "lossless": True,
        "mirrors": ["a.example.invalid", "b.example.invalid"],  # a list on a text setting
        "extra": {"depth": 2},  # not declared
    }
    source = world.add_source(addon, settings=stored)
    action = f"addons/{source}/settings"
    try:
        page = admin.get("addons").text
        mirrors = row_of(page, f"addon-{source}-s-mirrors")
        assert "Set in the configuration file" in mirrors and "not sent to the add-on" in mirrors
        assert 'name="setting.mirrors"' not in mirrors
        assert "Not sent to the add-on: extra, mirrors (a list or a table" in page
        assert "The add-on&#39;s default" in row_of(page, f"addon-{source}-s-codec")
        assert admin.submit("addons", {"budget_seconds": "4"}, action).status_code == 303
        typed = stored_addon(world, source)
        assert typed.budget == 4 and typed.settings == stored  # codec not frozen either
        assert type(typed.settings["bitrate"]) is int and typed.settings["lossless"] is True
        # The bitrate came from the file, not through this form: kept, not shown.
        bitrate = row_of(page, f"addon-{source}-s-bitrate")
        assert 'type="password"' in bitrate and "256" not in bitrate
        changes = {"setting.bitrate": "192", "setting.lossless": "false", "setting.codec": "opus"}
        assert admin.submit("addons", changes, action).status_code == 303
        assert stored_addon(world, source).settings == {
            **stored,
            "bitrate": 192,
            "lossless": False,
            "codec": "opus",
        }
        # Removed, its field is an ordinary one: a number typed there is shown again.
        assert admin.submit("addons", {"setting.bitrate-clear": "true"}, action).status_code == 303
        assert "bitrate" not in stored_addon(world, source).settings
        assert 'value="320"' in row_of(admin.get("addons").text, f"addon-{source}-s-bitrate")
        assert admin.submit("addons", {"setting.bitrate": "128"}, action).status_code == 303
        assert stored_addon(world, source).settings["bitrate"] == 128
        assert 'value="128"' in row_of(admin.get("addons").text, f"addon-{source}-s-bitrate")
        # Changed behind the form (the configuration file's list applied): not shown again.
        world.server.call(
            lambda: world.services.sources.update(source, settings={**stored, "bitrate": 96})
        )
        hidden = row_of(admin.get("addons").text, f"addon-{source}-s-bitrate")
        assert 'type="password"' in hidden and 'value="96"' not in hidden
        world.server.call(
            lambda: world.services.sources.update(source, settings={**stored, "bitrate": 128})
        )
        # Set back to the add-on's default: its own default applies.
        assert admin.submit("addons", {"setting.bitrate": "320"}, action).status_code == 303
        assert "bitrate" not in stored_addon(world, source).settings
    finally:
        world.clear_sources()


def test_a_file_list_that_is_not_applied_says_so(world: DeliveryWorld, admin: Browser) -> None:
    """Once the dashboard keeps the add-on list, the page says the file's is not
    applied - as a warning, naming what differs, when the file's is another."""
    world.clear_sources()
    addon = world.addon("Filed")
    source = world.add_source(addon)
    app = world.app
    configured = app.configured
    dashboard = app.dashboard
    assert dashboard.saved is not None
    saved = dashboard.saved
    listed = AddonSettings(name="Filed", base_url=addon.base_url, reach="loopback")
    try:
        app.configured = configured.model_copy(update={"addons": [listed]})
        world.server.call(lambda: saved.take_addons("tester"))
        page = admin.get("addons").text
        assert "so the file's list is not applied" in page
        assert "are not applied</p>" not in page  # the same list: no warning
        world.server.call(lambda: world.services.sources.update(source, budget_seconds=7))
        page = admin.get("addons").text
        assert "The configuration file's add-ons are not applied" in page
        assert "differs from this one (Filed)" in page
    finally:
        app.configured = configured
        world.server.call(lambda: saved.hand_back_addons("tester"))
        world.clear_sources()


def test_the_token_service_headers_stay_with_its_host(world: DeliveryWorld, admin: Browser) -> None:
    app = world.app
    configured = app.configured
    catalog = configured.catalog.model_copy(
        update={
            "token_file": None,
            "token_url": SecretStr("https://tokens.example.invalid/issue"),
            "token_headers": {"x-api-key": SecretStr(SECRET_TOKEN)},
        }
    )
    try:
        app.configured = configured.model_copy(update={"catalog": catalog})
        assert "Not sent:" not in admin.get("catalog").text
        other = "https://other.example.invalid/issue"
        assert admin.submit("catalog", {"token_url": other}).status_code == 303
        page = admin.get("catalog").text
        assert "Not sent: token service headers" in page and SECRET_TOKEN not in page
    finally:
        admin.submit("catalog", {"token_url-clear": "true"})
        app.configured = configured
    assert saved_rows(world, "catalog") == {}


def test_a_saved_key_does_not_follow_its_service_to_another_host(
    world: DeliveryWorld, admin: Browser
) -> None:
    """A key saved here for a service is removed when
    the service's address is saved on another host without the key, and the page says so."""
    from pydantic import BaseModel

    from shijhon.catalog import plugin
    from shijhon.dashboard.saved import BOUND

    class KeyedSettings(BaseModel):
        service: str | None = None
        service_key: SecretStr | None = None

    plugin.register(
        "keyed",
        plugin.Adapter(
            label="Keyed",
            build=lambda settings, context: None,  # type: ignore[arg-type, return-value]
            settings=KeyedSettings,
            words={
                "service": plugin.Words("Service"),
                "service_key": plugin.Words("Service key"),
            },
            bound={"service_key": "service"},
        ),
    )
    try:
        form = {"kind": "keyed", "shown-kind": "keyed", "service_key": SECRET_TOKEN}
        one = "https://one.example.invalid/api"
        assert admin.submit("catalog", {**form, "service": one}).status_code == 303
        rows = saved_rows(world, "catalog")
        assert SECRET_TOKEN in rows["service_key"] and "example" not in rows[BOUND]
        assert "Saved" in row_of(admin.get("catalog").text, "catalog-service-key")
        moved = admin.submit("catalog", {"service": "https://two.example.invalid/api"})
        assert moved.status_code == 303
        rows = saved_rows(world, "catalog")
        assert "service_key" not in rows and BOUND not in rows and "two.example" in rows["service"]
        page = html.unescape(admin.get("catalog").text)
        assert "Removed, as saved for another address: “Service key”." in page
        assert "Not set" in row_of(page, "catalog-service-key") and SECRET_TOKEN not in page
    finally:
        assert admin.submit("catalog", {"kind": "sample"}).status_code == 303
        plugin.unregister("keyed")
    assert saved_rows(world, "catalog") == {}


def test_turning_an_addon_off_changes_who_serves(world: DeliveryWorld, admin: Browser) -> None:
    world.clear_sources()
    first, second = world.addon("ServeFirst"), world.addon("ServeSecond")
    a = world.add_source(first)
    world.add_source(second)
    song, _, audio = world.placeholder_track("dash-serve", [first, second])
    try:
        assert admin.post(f"addons/{a}/enabled", {"enabled": "false", "csrf": admin.csrf()})
        assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
        assert second.requests("audio") and not first.requests("audio")
    finally:
        world.clear_sources()


def test_the_addon_chain_shows_health(world: DeliveryWorld, admin: Browser) -> None:
    world.clear_sources()
    good, gone = world.addon("Healthy"), world.addon("Gone")
    world.add_source(good)
    world.add_source(gone)
    world.add_source(world.addon("Resting"))
    cooling = world.add_source(world.addon("Cooling"))
    world.add_source(world.addon("Failing"))
    world.add_source(world.addon("Limited"))
    try:
        gone.stop()
        world.services.sources.cool_down(cooling, 600)
        enabled = {s.name: s for s in world.server.call(world.services.sources.enabled)}
        registry = world.services.sources
        registry.succeeded(enabled["Healthy"], 0.2)
        registry.failed(enabled["Healthy"], "not found (HTTP 404)")  # one song it lacks
        for _ in range(3):
            registry.failed(enabled["Failing"], "timeout")
            registry.failed(enabled["Limited"], "HTTP 429")  # a rate limit is not an error
        admin.get("addons")
        resting = next(
            a for a in world.server.call(world.services.sources.enabled) if a.name == "Resting"
        )
        admin.post(f"addons/{resting.id}/enabled", {"enabled": "false", "csrf": admin.csrf()})
        world.app.dashboard.checks.every = 0  # read the manifests again
        page = admin.get("addons").text
        assert re.search(
            r'led-ok" aria-hidden="true"></span>\s*<div class="source-name">\s*<h3>Healthy', page
        )
        assert "source source-attention" in page and "Manifest unreachable:" in page
        assert re.search(r"Cooling down until \d\d:\d\d", page)
        assert "source source-off" in page
        assert "6 add-ons, 5 on" in page
        # The list is not a strict order: as Playback says it.
        assert "top down" not in page
        assert "primary first starts with the primary, then tries the others fastest first" in page
        failing = page[page.index("<h3>Failing</h3>") - 400 : page.index("<h3>Failing</h3>") + 900]
        assert "led-error" in failing and "Error: <code>timeout</code>" in failing
        limited = page[page.index("<h3>Limited</h3>") - 400 : page.index("<h3>Limited</h3>") + 900]
        assert "led-error" not in limited
        diagnostics = admin.get("diagnostics").text
        assert "chain chain-readonly" in diagnostics and "Manifest unreachable:" in diagnostics
        assert re.search(r"Last audio delivered \d\d:\d\d", diagnostics)
        assert "3 failures in a row, the last at" in diagnostics
    finally:
        world.app.dashboard.checks.every = 60
        world.clear_sources()


def test_diagnostics_shows_the_last_scan_as_a_time(world: DeliveryWorld, admin: Browser) -> None:
    raw = world.nd.scan_status().last_scan
    at = scan_time(raw)  # Navidrome's own format is read
    assert raw and at is not None and abs(time.time() - at) < 86400, raw
    diagnostics = admin.get("diagnostics").text
    assert re.search(r"songs; last scan (\d{1,2} \w+, )?\d\d:\d\d</dd>", diagnostics)
    assert raw not in diagnostics


# --- appearance ---------------------------------------------------------------------------


def test_the_accent_is_saved_for_everyone(world: DeliveryWorld, admin: Browser) -> None:
    try:
        assert admin.submit("appearance", {"accent": "teal"}).status_code == 303
        page = admin.get("appearance").text
        assert '<html lang="en" data-accent="teal">' in page
        assert "Accent color saved." in page
        assert 'value="teal" checked' in page
        assert 'data-accent="teal"' in admin.get("diagnostics").text
        other = Browser(world.server.base_url)
        assert 'data-accent="teal"' in other.get("sign-in").text
        other.close()
        assert admin.submit("appearance", {"accent": "vermilion"}).status_code == 303
        assert 'data-accent="teal"' in admin.get("appearance").text
    finally:
        admin.submit("appearance", {"accent": "green"})
    assert saved_rows(world, "appearance") == {}  # the default is not stored


# --- secrets ------------------------------------------------------------------------------


def test_secrets_never_appear_in_responses_or_logs(world: DeliveryWorld, admin: Browser) -> None:
    collected: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(self.format(record))

    handler = Collect(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    quiet_libraries()  # as configure_logging does: they would print URLs and statements
    world.clear_sources()
    addon = world.addon("Keyed", settings=[QUALITY, API_KEY])
    try:
        url = f"{addon.base_url}/?key={SECRET_KEY}"
        assert admin.submit("addons", {"manifest": url, "reach": "loopback"}).status_code == 303
        keyed = next(s for s in world.server.call(world.services.sources.enabled))
        action = f"addons/{keyed.id}/settings"
        page = admin.get("addons").text
        key_row = row_of(page, f"addon-{keyed.id}-s-apikey")
        assert 'type="password"' in key_row and "Not set" in key_row
        assert "-clear" not in key_row  # nothing to remove
        assert "-clear" not in row_of(page, f"addon-{keyed.id}-manifest")  # replaced only
        # A failed save: the typed secrets are not shown back.
        failed = admin.submit(
            "addons",
            {
                "setting.apiKey": SECRET_SETTING,
                "manifest": f"{addon.base_url}/?key={SECRET_KEY_2}",
                "budget_seconds": "-1",
            },
            action,
        )
        assert failed.status_code == 400
        saved = admin.submit(
            "addons",
            {"setting.apiKey": SECRET_SETTING, "manifest": f"{addon.base_url}/?key={SECRET_KEY_2}"},
            action,
        )
        assert saved.status_code == 303
        keyed = next(s for s in world.server.call(world.services.sources.enabled))
        assert keyed.addon.settings["apiKey"] == SECRET_SETTING  # stored, and used
        key_row = row_of(admin.get("addons").text, f"addon-{keyed.id}-s-apikey")
        assert "Set" in key_row
        assert 'name="setting.apiKey-clear" value="true"> Remove</label>' in key_row
        assert admin.submit("catalog", {"token": SECRET_TOKEN}).status_code == 303
        token_row = row_of(admin.get("catalog").text, "catalog-token")
        assert "tag-set" in token_row and "Saved " in token_row
        assert 'name="token-clear" value="true"> Remove</label>' in token_row
        world.client().request("ping")
        for page in PAGES:
            admin.get(page)
        bodies = [r.text for r in admin.seen] + [str(r.headers) for r in admin.seen]
        for secret in SECRETS:
            assert not [b for b in bodies if secret in b], secret
            assert not [line for line in collected if secret in line], secret
        assert any("catalog.token (secret)" in line for line in collected)
        database = world.app.configured.database_path
        assert database.stat().st_mode & 0o777 == 0o600
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
        admin.submit("catalog", {"token-clear": "true"})
        world.clear_sources()
    assert "token" not in saved_rows(world, "catalog")


def test_a_stored_secret_is_never_shown_whatever_its_manifest_declares_now(
    world: DeliveryWorld, admin: Browser
) -> None:
    """An add-on's manifest is read again every minute and may
    declare a setting anew - a token as a number or as a choice, free text under a name
    that says nothing. The form never shows what is stored unless it cannot be a secret,
    and saving it as shown changes nothing."""
    world.clear_sources()
    secrets = {
        "apiKey": SECRET_SETTING,  # named like a key
        "access": SECRET_KEY_2,  # a secret by its declared type
        "tier": "gold-7-stored",  # free text under a name that says nothing
        "userId": "u-551-stored",
        "undeclared": "und-9-stored",
        "accountnumber": "471199331",  # digits, under a name that says nothing
    }
    choices = ["a", "b"]
    declarations: list[list[dict[str, Any]]] = [
        [  # as first declared
            {"key": "apiKey", "type": "string"},
            {"key": "access", "type": "password"},
            {"key": "tier", "type": "text"},
            {"key": "userId"},
        ],
        [{"key": key, "type": "integer", "default": 5} for key in secrets],
        [{"key": key, "type": "select", "options": choices} for key in secrets],
        [{"key": key, "type": "select", "options": choices, "default": "b"} for key in secrets],
        [{"key": key, "type": "toggle", "default": True} for key in secrets],
        [{"key": key, "type": "text", "default": "plain-default"} for key in secrets],
        # ... and when its manifest now calls it plain text: it was not entered as such.
        [{"key": key, "type": "text", "secret": False} for key in secrets],
    ]
    addon = world.addon("Shifty", settings=declarations[0])
    source = world.add_source(addon, settings=secrets)
    action = f"addons/{source}/settings"
    try:
        for declared in declarations:
            addon.settings = declared
            world.app.dashboard.checks.forget(source)
            page = admin.get("addons").text
            for key in (d["key"] for d in declared):
                assert f'id="addon-{source}-s-{key.lower()}"' in page, (key, declared)
            # Saved as shown (and with another budget): every stored value stays.
            assert admin.submit("addons", {"budget_seconds": "3"}, action).status_code == 303
            assert stored_addon(world, source).settings == secrets, declared
        for response in admin.seen:
            for value in secrets.values():
                assert value not in response.text
        # A choice made, a number typed, a value removed: the stored one is replaced.
        addon.settings = [
            {"key": "tier", "type": "select", "options": choices},
            {"key": "undeclared", "type": "number"},
            {"key": "access", "type": "select", "options": choices},
        ]
        world.app.dashboard.checks.forget(source)
        page = admin.get("addons").text
        assert "A stored value that is none of these (kept)" in row_of(
            page, f"addon-{source}-s-tier"
        )
        number_row = row_of(page, f"addon-{source}-s-undeclared")
        assert 'type="password"' in number_row and "not shown" in number_row
        refused = admin.submit("addons", {"setting.undeclared": "seven"}, action)
        assert refused.status_code == 400
        assert stored_addon(world, source).settings == secrets
        changes = {"setting.tier": "b", "setting.undeclared": "7", "setting.access": ""}
        assert admin.submit("addons", changes, action).status_code == 303
        changed = {**secrets, "tier": "b", "undeclared": 7}
        del changed["access"]
        assert stored_addon(world, source).settings == changed
        page = admin.get("addons").text
        assert '<option value="b" selected>' in row_of(page, f"addon-{source}-s-tier")
        # The number was typed into a write-only field: it stays unshown; removed, and
        # typed into its plain field, it is shown.
        assert 'value="7"' not in row_of(page, f"addon-{source}-s-undeclared")
        cleared = admin.submit("addons", {"setting.undeclared-clear": "true"}, action)
        assert cleared.status_code == 303
        assert admin.submit("addons", {"setting.undeclared": "8"}, action).status_code == 303
        assert stored_addon(world, source).settings["undeclared"] == 8
        page = admin.get("addons").text
        assert 'value="8"' in row_of(page, f"addon-{source}-s-undeclared")
        for response in admin.seen:
            for value in secrets.values():
                assert value not in response.text
    finally:
        world.clear_sources()


def test_the_database_is_made_private(tmp_path: Path) -> None:
    from shijhon.store.db import prepare_database

    path = tmp_path / "shijhon.sqlite3"
    prepare_database(path)
    path.chmod(0o644)
    prepare_database(path)
    assert path.stat().st_mode & 0o777 == 0o600


# --- every form, redirects, failures ------------------------------------------------------


def _state(world: DeliveryWorld) -> tuple[object, ...]:
    async def read() -> tuple[object, ...]:
        store = world.services.store
        sources = await store.fetchall(
            "SELECT id, name, base_url, settings, enabled, position, reach, budget_seconds"
            " FROM sources ORDER BY id"
        )
        saved = await store.fetchall("SELECT section, key, value FROM saved_settings")
        return tuple(tuple(r) for r in sources), tuple(sorted(tuple(r) for r in saved))

    return world.server.call(read)


POSTS = [
    ("addons", {"manifest": "https://addon.example.invalid/manifest.json"}),
    ("addons/configuration", {}),
    ("addons/{id}/enabled", {"enabled": "false"}),
    ("addons/{id}/move", {"direction": "down"}),
    ("addons/{id}/settings", {"budget_seconds": "7"}),
    ("addons/{id}/remove", {}),
    ("playback", {"cooldown_seconds": "7"}),
    ("catalog", {"region": "gb"}),
    ("catalog/check", {}),
    ("appearance", {"accent": "purple"}),
    ("sign-out", {}),
]


@pytest.mark.parametrize(("route", "data"), POSTS, ids=[r for r, _ in POSTS])
def test_every_form_needs_an_admin_session_and_its_token(
    world: DeliveryWorld, browser: Browser, route: str, data: dict[str, str]
) -> None:
    world.clear_sources()
    first = world.add_source(world.addon("GuardOne"))
    world.add_source(world.addon("GuardTwo"))
    path = route.format(id=first)
    before = _state(world)
    anonymous = browser.post(path, data)
    assert anonymous.status_code == 303 and "/sign-in" in anonymous.headers["location"]
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    token = browser.csrf()
    assert browser.post(path, {**data, "csrf": "forged"}).status_code == 403
    assert browser.post(path, data).status_code == 403
    cross = browser.post(path, {**data, "csrf": token}, headers={"sec-fetch-site": "cross-site"})
    assert cross.status_code == 403
    assert _state(world) == before
    assert browser.get("addons").status_code == 200  # still signed in (sign-out refused too)
    world.clear_sources()


@pytest.mark.parametrize(
    ("target", "lands"),
    [
        (f"{PATH}/diagnostics", f"{PATH}/diagnostics"),
        ("//evil.example/x", f"{PATH}/addons"),
        ("https://evil.example/", f"{PATH}/addons"),
        (f"{PATH}//evil.example", f"{PATH}/addons"),
        (f"{PATH}\\evil", f"{PATH}/addons"),
        ("/app/", f"{PATH}/addons"),
    ],
)
def test_sign_in_returns_only_within_the_dashboard(
    browser: Browser, target: str, lands: str
) -> None:
    signed = browser.sign_in(ADMIN_USER, ADMIN_PASSWORD, next=target)
    assert signed.status_code == 303 and signed.headers["location"] == lands


def test_a_failed_sign_in_keeps_where_it_was_going(browser: Browser) -> None:
    failed = browser.sign_in(ADMIN_USER, "wrong", next=f"{PATH}/catalog")
    assert f'name="next" value="{PATH}/catalog"' in failed.text


def test_sign_in_while_navidrome_is_down(world: DeliveryWorld, browser: Browser) -> None:
    import httpx

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    dashboard = world.app.dashboard
    real = dashboard._http
    dashboard._http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    try:
        down = browser.sign_in(ADMIN_USER, ADMIN_PASSWORD)
        assert down.status_code == 503
        assert "Navidrome is not answering" in down.text and "notice-error" in down.text
        assert browser.get("addons").status_code == 303
    finally:
        dashboard._http = real


def test_rights_that_cannot_be_checked_refuse_the_page(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unknown(navidrome: object, username: str) -> None:
        return None

    monkeypatch.setattr(world.app.dashboard.admins, "admin", unknown)
    page = admin.get("addons")
    assert page.status_code == 503 and "Admin rights can" in page.text
    assert "Signed in as" not in page.text
    monkeypatch.undo()
    assert admin.get("addons").status_code == 200  # the session was kept


def test_fields_missing_from_a_form_are_kept(world: DeliveryWorld, admin: Browser) -> None:
    settings = world.services.deliverer.settings
    try:
        assert admin.submit("playback", {"prepare_when_not_ready": "true"}).status_code == 303
        assert settings.prepare_when_not_ready is True
        only = admin.post("playback", {"csrf": admin.csrf(), "cooldown_seconds": "2"})
        assert only.status_code == 303
        assert settings.prepare_when_not_ready is True and settings.cooldown_seconds == 2
        # A checkbox left off sends its hidden "false".
        off = admin.form("playback")
        assert off["prepare_when_not_ready"] == "true"
        off["prepare_when_not_ready"] = "false"
        assert admin.post("playback", off).status_code == 303
        assert settings.prepare_when_not_ready is False
    finally:
        admin.submit("playback", {"prepare_when_not_ready": "false", "cooldown_seconds": "1.5"})
    assert saved_rows(world, "delivery") == {}


def test_handing_the_addon_list_back_waits_for_a_restart(
    world: DeliveryWorld, admin: Browser
) -> None:
    world.clear_sources()
    a = world.add_source(world.addon("HandOne"))
    try:
        admin.post(f"addons/{a}/enabled", {"enabled": "false", "csrf": admin.csrf()})
        assert "dashboard" in saved_rows(world, "addons")
        back = admin.post("addons/configuration", {"csrf": admin.csrf()})
        assert back.status_code == 303
        assert saved_rows(world, "addons") == {}
        page = admin.get("addons").text
        assert "applies after Shijhon restarts" in page
        assert "add-on list from the configuration file" in page  # the restart banner
    finally:
        world.app.dashboard.addons_handed_back = False
        world.clear_sources()


def test_more_secrets_stay_hidden(world: DeliveryWorld, admin: Browser) -> None:
    token_url = f"https://tokens.example.invalid/issue?key={SECRET_TOKEN}"
    addon = world.addon("Hidden")
    try:
        saved = admin.submit("catalog", {"token_url": token_url})
        assert saved.status_code == 303
        page = admin.get("catalog").text
        assert "tag-set" in row_of(page, "catalog-token-url")
        # A failed save re-renders the page: the typed secret is not in it.
        failed = admin.submit("catalog", {"token": SECRET_SETTING, "region": "g1"})
        assert failed.status_code == 400
        # An add-on refused (public reach for a loopback address) with a key in its URL.
        keyed = f"{addon.base_url}/?key={SECRET_KEY}"
        refused = admin.submit("addons", {"manifest": keyed, "reach": "public"})
        assert refused.status_code == 400
        # A warning with a key in a URL reaches Recent errors only redacted.
        logging.getLogger("shijhon.delivery.test").warning(
            "stream failed at %s/stream?key=%s", addon.base_url, SECRET_KEY_2
        )
        diagnostics = admin.get("diagnostics").text
        assert "stream failed at" in diagnostics
        for response in admin.seen:
            for secret in SECRETS:
                assert secret not in response.text and secret not in str(response.headers)
    finally:
        admin.submit("catalog", {"token_url-clear": "true", "token-clear": "true"})
    assert saved_rows(world, "catalog") == {}


def test_settings_set_by_the_environment_are_locked_on_the_page(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment wins over the dashboard: its settings are shown
    as text and a form cannot change them. (The app reads the environment when it is built;
    here its record of it is replaced, after a value was saved for that setting.)"""
    from shijhon.dashboard.saved import REMOVE, SavedSettings

    settings = world.services.deliverer.settings
    store = world.services.store
    try:
        # Saved before the variable was set: kept, not used.
        world.server.call(
            lambda: SavedSettings(store).put("delivery", {"cooldown_seconds": 4.0}, "earlier")
        )
        monkeypatch.setattr(
            world.app,
            "locked",
            {"delivery": {"cooldown_seconds": "SHIJHON_DELIVERY__COOLDOWN_SECONDS"}},
        )
        page = admin.get("playback").text
        row = row_of(page, "delivery-cooldown-seconds")
        assert "Set by the environment" in row and "SHIJHON_DELIVERY__COOLDOWN_SECONDS" in row
        assert 'name="cooldown_seconds"' not in row  # no control, only its value
        assert "<p>1.5 s</p>" in row  # the configuration's value, not the saved 4
        assert "A value saved here is kept but not used" in row
        assert "tag-restart" not in row and "Restart needed" not in page
        forged = admin.post("playback", {"csrf": admin.csrf(), "cooldown_seconds": "9"})
        assert forged.status_code == 303
        assert saved_rows(world, "delivery") == {"cooldown_seconds": "4.0"}  # untouched
        # A failed save of another field keeps showing the environment's value.
        failed = admin.submit("playback", {"seek_timeout_seconds": "abc"})
        assert failed.status_code == 400
        assert "<p>1.5 s</p>" in row_of(failed.text, "delivery-cooldown-seconds")
        removed = admin.post("playback", {"csrf": admin.csrf(), "cooldown_seconds-clear": "true"})
        assert removed.status_code == 303
        assert saved_rows(world, "delivery") == {}
        assert settings.cooldown_seconds == 1.5  # the running value never changed
    finally:
        monkeypatch.undo()
        world.server.call(
            lambda: SavedSettings(store).put("delivery", {"cooldown_seconds": REMOVE}, "t")
        )
    assert saved_rows(world, "delivery") == {}


def test_an_addon_list_from_the_environment_is_read_only(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.clear_sources()
    one = world.add_source(world.addon("EnvOne"))
    try:
        monkeypatch.setattr(
            world.app.configured, "_from_environment", {"addons": {"list": "SHIJHON_ADDONS"}}
        )
        page = admin.get("addons").text
        assert "set by the environment: <code>SHIJHON_ADDONS</code>" in page
        assert "chain chain-readonly" in page and "Add add-on" not in page
        assert 'name="enabled"' not in page and "EnvOne" in page
        refused = admin.post(f"addons/{one}/enabled", {"enabled": "false", "csrf": admin.csrf()})
        assert refused.status_code == 409 and "SHIJHON_ADDONS" in refused.text
        added = admin.post("addons", {"manifest": "https://x.invalid/m", "csrf": admin.csrf()})
        assert added.status_code == 409
        assert [s.name for s in world.server.call(world.services.sources.enabled)] == ["EnvOne"]
    finally:
        monkeypatch.undo()
        world.clear_sources()


def test_a_pass_mode_without_a_pass_waits_for_a_restart(
    world: DeliveryWorld, admin: Browser
) -> None:
    """This world matches no albums: a mode saved now applies when a pass exists."""
    try:
        saved = admin.submit("library", {"library_pass": "on"}, action="library/pass")
        assert saved.status_code == 303
        page = admin.get("library").text
        assert "The library pass becomes on when Shijhon restarts." in page
    finally:
        admin.submit("library", {"library_pass": "dry_run"}, action="library/pass")
    assert "library_pass" not in saved_rows(world, "fill")
