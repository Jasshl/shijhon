"""Notifications in a running Shijhon: the Notifications page (administrators only) saves
the Apprise addresses - a secret that never appears in a page, a redirect, a log line or a
notification - and applies them at once; "Send a test notification" says whether it went out
without naming an address; a state found where Shijhon finds it (an add-on that needs
sign-in) reaches a local stand-in once; each kind can be switched off."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from shijhon.app import ShijhonApp
from shijhon.config import load_settings
from shijhon.log import quiet_libraries
from shijhon.notifications import Notices
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import PATH, Browser
from tests.harness.delivery import TEST_DELIVERY, DeliveryWorld
from tests.harness.engine import PLACEHOLDER_FOLDER
from tests.harness.fake_addon import fake_resolver
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.notification_sink import TOKEN, NotificationSink
from tests.harness.running import RunningServer

LISTENER, LISTENER_PASSWORD = "listener", "listener-password-for-tests"
PAGE = "https://addon.example.invalid/sign-in"


@pytest.fixture(scope="module")
def sink() -> Iterator[NotificationSink]:
    made = NotificationSink().start()
    yield made
    made.stop()


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    nd.create_user(LISTENER, LISTENER_PASSWORD)
    tmp: Path = tmp_path_factory.mktemp("notifications")
    settings = load_settings(
        None,
        state_dir=tmp / "state",
        navidrome={
            "url": nd.base_url,
            "user": ADMIN_USER,
            "password": ADMIN_PASSWORD,
            "library_path": nd.music,
        },
        placeholders={"folder": PLACEHOLDER_FOLDER},
        delivery=TEST_DELIVERY,
        fill={"enabled": False},
        catalog={"prefetch_covers": 0},
    )
    app = ShijhonApp(settings, resolver=fake_resolver({}))
    server = RunningServer(app)
    server.start()
    made = DeliveryWorld(nd, app, server, tmp, {})
    made.app.dashboard.attempts.limit = 10_000
    yield made
    made.stop()


@pytest.fixture
def browser(world: DeliveryWorld) -> Iterator[Browser]:
    b = Browser(world.server.base_url)
    yield b
    b.close()


@pytest.fixture
def admin(browser: Browser) -> Browser:
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    return browser


def notices_of(world: DeliveryWorld) -> Notices:
    found = world.services.notices
    assert found is not None
    return found


def saved_rows(world: DeliveryWorld) -> dict[str, str]:
    async def read() -> dict[str, str]:
        rows = await world.services.store.fetchall(
            "SELECT key, value FROM saved_settings WHERE section = 'notifications'"
        )
        return {row["key"]: row["value"] for row in rows}

    return world.server.call(read)


def forget(world: DeliveryWorld) -> None:
    async def clear() -> None:
        await world.services.store.execute(
            "DELETE FROM saved_settings WHERE section = 'notifications'"
        )

    world.server.call(clear)
    notices_of(world).settings = world.app.configured.notifications


def needs_sign_in(world: DeliveryWorld, source_id: int, page: str | None) -> None:
    """What a play does when an add-on answers ``needs_sign_in`` (the K suite plays it)."""

    async def found() -> None:
        world.services.sources.needs(source_id, "sign_in", page, 0)

    world.server.call(found)


def delivered_again(world: DeliveryWorld, source_id: int) -> None:
    async def answered() -> None:
        world.services.sources.answered(source_id)

    world.server.call(answered)


def test_the_page_is_for_administrators(browser: Browser) -> None:
    assert browser.get("notifications").status_code == 303  # to the sign-in
    assert browser.sign_in(LISTENER, LISTENER_PASSWORD).status_code == 303
    assert browser.get("notifications").status_code == 403
    csrf = browser.csrf()
    assert browser.post("notifications", {"urls": "ntfy://x", "csrf": csrf}).status_code == 403
    assert browser.post("notifications/test", {"csrf": csrf}).status_code == 403


def test_nothing_is_sent_until_an_address_is_set(world: DeliveryWorld, admin: Browser) -> None:
    page = admin.get("notifications")
    assert page.status_code == 200
    assert "<span>Notifications</span></a>" in admin.get("addons").text  # in the menu
    assert "Off: no address is set" in page.text
    assert re.search(r"<button[^>]*disabled[^>]*>Send a test notification", page.text)
    # The kinds are a quiet group, all on.
    assert "What is reported" in page.text
    assert "An upgrade went on without a snapshot" in page.text
    for key in (
        "addon_sign_in",
        "cleanup_blocked",
        "usage_export",
        "import_review",
        "playlist_refresh",
        "snapshot_skipped",
    ):
        field = "notifications-" + key.replace("_", "-")
        assert re.search(rf'id="{field}"[^>]*checked', page.text), key
    refused = admin.post("notifications/test", {"csrf": admin.csrf()})
    assert refused.status_code == 303
    assert "No address is set" in admin.get("notifications").text


def test_an_address_is_saved_applied_at_once_and_stays_secret(
    world: DeliveryWorld,
    admin: Browser,
    sink: NotificationSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_libraries()  # (as Shijhon runs)
    caplog.set_level(logging.DEBUG)
    sink.clear()
    try:
        done = admin.submit("notifications", {"urls": sink.address()})
        assert done.status_code == 303 and done.headers["location"] == f"{PATH}/notifications"
        rows = saved_rows(world)
        assert set(rows) == {"urls"} and TOKEN in rows["urls"]
        page = admin.get("notifications")
        assert "On: notifications go to 1 address" in page.text
        assert "Restart needed" not in page.text  # (it applies at once)
        assert "Saved" in page.text
        assert notices_of(world).settings.addresses() == [sink.address()]
        # A test notification goes out, and the page says so.
        sent = admin.post("notifications/test", {"csrf": admin.csrf()})
        assert sent.status_code == 303
        [received] = sink.wait(1)
        assert received.body["title"] == "Shijhon: a test notification"
        assert "The test notification was sent" in admin.get("notifications").text
        # The address is in no page, redirect or log line, nor in what was sent.
        for response in admin.seen:
            assert TOKEN not in response.text and TOKEN not in str(response.headers)
        assert TOKEN not in caplog.text
        assert "notifications.urls (secret)" in caplog.text
        assert TOKEN not in received.body["title"] + received.body["message"]
    finally:
        forget(world)


def test_a_failure_is_said_without_the_address(
    world: DeliveryWorld,
    admin: Browser,
    sink: NotificationSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_libraries()  # (as Shijhon runs)
    caplog.set_level(logging.DEBUG)
    sink.clear()
    sink.status = 500
    try:
        assert admin.submit("notifications", {"urls": sink.address()}).status_code == 303
        admin.post("notifications/test", {"csrf": admin.csrf()})
        sink.wait(1)
        page = admin.get("notifications").text
        assert "The test notification could not be sent: not accepted by its address" in page
        assert "The last notification could not be sent" in page
        assert TOKEN not in page and str(sink.port) not in page
        assert TOKEN not in caplog.text and str(sink.port) not in caplog.text
    finally:
        sink.status = 200
        notices_of(world).last_failed = None
        forget(world)


def test_an_address_apprise_cannot_use_is_refused_without_repeating_it(
    world: DeliveryWorld, admin: Browser
) -> None:
    refused = admin.submit("notifications", {"urls": f"ntfy://topic unknown://{TOKEN}@host"})
    assert refused.status_code == 400 and "Not saved" in refused.text
    assert "Address 2 of 2 is not one Apprise can send to" in refused.text
    assert TOKEN not in refused.text
    assert saved_rows(world) == {}


def test_a_state_found_is_sent_once_and_its_kind_can_be_switched_off(
    world: DeliveryWorld, admin: Browser, sink: NotificationSink
) -> None:
    sink.clear()
    try:
        assert admin.submit("notifications", {"urls": sink.address()}).status_code == 303
        needs_sign_in(world, 9001, PAGE)
        [received] = sink.wait(1)
        assert received.body["title"] == "Shijhon: an add-on needs sign-in"
        assert PAGE in received.body["message"]  # its page, for its operator
        needs_sign_in(world, 9001, PAGE)  # every later play that finds it: nothing more
        delivered_again(world, 9001)
        # The kind switched off: a sign-in another add-on needs goes nowhere.
        done = admin.submit("notifications", {"addon_sign_in": "false"})
        assert done.status_code == 303 and set(saved_rows(world)) == {"urls", "addon_sign_in"}
        needs_sign_in(world, 9002, None)
        assert notices_of(world).states["addon:9002"].active
        # (A test after them all: had anything else been sent, it would have come first.)
        admin.post("notifications/test", {"csrf": admin.csrf()})
        assert [r.body["title"] for r in sink.wait(2)] == [
            "Shijhon: an add-on needs sign-in",
            "Shijhon: a test notification",
        ]
    finally:
        forget(world)
