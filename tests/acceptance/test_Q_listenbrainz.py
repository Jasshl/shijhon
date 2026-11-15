"""The ListenBrainz page in a running Shijhon (administrators only): ListenBrainz is asked by
default, without setup; the page turns it off and on at once, and saves the optional token -
a secret that never appears in a page, a redirect or a log line."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from shijhon.app import ShijhonApp
from shijhon.config import load_settings
from shijhon.log import quiet_libraries
from shijhon.playlists.listenbrainz import ListenBrainz
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import PATH, Browser
from tests.harness.delivery import TEST_DELIVERY, DeliveryWorld
from tests.harness.engine import PLACEHOLDER_FOLDER
from tests.harness.fake_addon import fake_resolver
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.running import RunningServer

LISTENER, LISTENER_PASSWORD = "listener", "listener-password-for-tests"
TOKEN = "invented-listenbrainz-token-7f3a"


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    nd.create_user(LISTENER, LISTENER_PASSWORD)
    tmp: Path = tmp_path_factory.mktemp("listenbrainz")
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


def listening(world: DeliveryWorld) -> ListenBrainz:
    found = world.services.listenbrainz
    assert found is not None
    return found


def test_the_page_is_for_administrators(browser: Browser) -> None:
    assert browser.get("listenbrainz").status_code == 303  # to the sign-in
    assert browser.sign_in(LISTENER, LISTENER_PASSWORD).status_code == 303
    assert browser.get("listenbrainz").status_code == 403
    refused = browser.post("listenbrainz", {"enabled": "", "csrf": browser.csrf()})
    assert refused.status_code == 403


def test_on_by_default_turned_off_and_on_at_once_and_the_token_stays_secret(
    world: DeliveryWorld, browser: Browser, caplog: pytest.LogCaptureFixture
) -> None:
    quiet_libraries()  # (as Shijhon runs)
    caplog.set_level(logging.DEBUG)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    page = browser.get("listenbrainz")
    assert page.status_code == 200
    assert "<span>ListenBrainz</span></a>" in page.text  # in the menu
    assert re.search(r'id="listenbrainz-enabled"[^>]*checked', page.text)
    assert listening(world).on  # without any setup
    # Off, at once.
    done = browser.submit("listenbrainz", {"enabled": ""})
    assert done.status_code == 303 and done.headers["location"] == f"{PATH}/listenbrainz"
    assert not listening(world).on
    assert "Restart needed" not in browser.get("listenbrainz").text
    # On again with a token: used at once, shown nowhere.
    browser.submit("listenbrainz", {"enabled": "true", "token": TOKEN})
    assert listening(world).on
    assert listening(world).settings.api_token() == TOKEN
    browser.get("listenbrainz")
    for response in browser.seen:
        assert TOKEN not in response.text and TOKEN not in str(response.headers)
    assert TOKEN not in caplog.text
