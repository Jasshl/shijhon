"""AudioMuse-AI in a running Shijhon: the connection is made only when an address is set,
the first look at its analysis comes after startup without holding it up, the Sound page
(administrators only) shows what is connected and saves its settings - the token among them,
which never appears in a page or a log line - and a stop leaves no work behind."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from shijhon.app import ShijhonApp
from shijhon.audiomuse import AudioMuse
from shijhon.audiomuse import sync as sync_module
from shijhon.config import load_settings
from shijhon.log import quiet_libraries
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import PATH, Browser
from tests.harness.delivery import TEST_DELIVERY, DeliveryWorld
from tests.harness.engine import PLACEHOLDER_FOLDER
from tests.harness.fake_addon import fake_resolver
from tests.harness.fake_audiomuse import FakeAudioMuse, FakeSong
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.running import RunningServer

LISTENER, LISTENER_PASSWORD = "listener", "listener-password-for-tests"
TOKEN = "musetoken4c8e1b97a0"  # invented: it may appear in no page, redirect or log line
NEW_TOKEN = "musetoken-new-7f20d3c5"
SERVER = "Shijhon"


def songs() -> list[FakeSong]:
    return [
        FakeSong(
            ids={SERVER: f"song-{n}"},
            title=f"Title {n}",
            vector=(float(n), 0.0),
            energy=0.1 + n * 0.05,
            features={"danceable": 0.4 + n * 0.02, "sad": 0.6 - n * 0.02},
        )
        for n in range(1, 7)
    ]


@pytest.fixture(scope="module")
def muse() -> Iterator[FakeAudioMuse]:
    fake = FakeAudioMuse(TOKEN, servers={SERVER: "srv-1"}, default=SERVER)
    fake.add(*songs())
    fake.start()
    yield fake
    fake.stop()


def start(
    nd_factory: NavidromeFactory, tmp: Path, muse: FakeAudioMuse | None
) -> tuple[DeliveryWorld, pytest.MonkeyPatch]:
    nd = nd_factory()
    nd.create_user(LISTENER, LISTENER_PASSWORD)
    patch = pytest.MonkeyPatch()
    patch.setattr(sync_module, "START_DELAY", 0.0)  # (the first look comes at once)
    audiomuse = (
        {
            "url": muse.base_url,
            "token": TOKEN,
            "reach": "loopback",
            "server": SERVER,
            "sync_hours": 1,
            "timeout_seconds": 5,
        }
        if muse is not None
        else {}
    )
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
        audiomuse=audiomuse,
    )
    app = ShijhonApp(settings, resolver=fake_resolver({}))
    server = RunningServer(app)
    server.start()
    world = DeliveryWorld(nd, app, server, tmp, {})
    world.app.dashboard.attempts.limit = 10_000
    return world, patch


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    muse: FakeAudioMuse,
) -> Iterator[DeliveryWorld]:
    made, patch = start(navidrome_factory, tmp_path_factory.mktemp("sound"), muse)
    yield made
    made.stop()
    patch.undo()


@pytest.fixture
def browser(world: DeliveryWorld) -> Iterator[Browser]:
    b = Browser(world.server.base_url)
    yield b
    b.close()


@pytest.fixture
def admin(browser: Browser) -> Browser:
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    return browser


def sound_of(world: DeliveryWorld) -> AudioMuse:
    found = world.services.playlists.sound if world.services.playlists else None
    assert isinstance(found, AudioMuse)
    return found


def wait_for_facts(world: DeliveryWorld, count: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if world.server.call(sound_of(world).status).songs >= count:
            return
        time.sleep(0.1)
    raise AssertionError("the analysis was not read")


def forget(world: DeliveryWorld) -> None:
    async def clear() -> None:
        await world.services.store.execute("DELETE FROM saved_settings WHERE section = 'audiomuse'")

    world.server.call(clear)


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


# --- the connection -----------------------------------------------------------------------


def test_the_connection_is_made_and_the_analysis_is_read_after_startup(
    world: DeliveryWorld, muse: FakeAudioMuse
) -> None:
    sound = sound_of(world)  # the playlists have it
    wait_for_facts(world, 6)
    facts = world.server.call(lambda: sound.facts(["song-1", "song-6", "song-404"]))
    assert set(facts) == {"song-1", "song-6"}
    assert (facts["song-1"].energy, facts["song-6"].energy) == (8, 92)  # among the six
    found = world.server.call(lambda: sound.similar(["song-3"], count=2))
    assert found == ["song-2", "song-4"]
    # Everything it sent carried the token as a bearer token, and the user agent.
    sent = [r for r in muse.requests() if r["endpoint"] != "health"]
    assert sent and {r["authorization"] for r in sent} == {f"Bearer {TOKEN}"}
    assert all("Shijhon/" in (r["user_agent"] or "") for r in sent)


def test_nothing_is_connected_without_an_address(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> None:
    off, patch = start(navidrome_factory, tmp_path_factory.mktemp("sound-off"), None)
    try:
        assert off.services.playlists is not None and off.services.playlists.sound is None
        browser = Browser(off.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        page = browser.get("sound")
        assert page.status_code == 200 and "Off: no AudioMuse-AI address is set" in page.text
        browser.close()
    finally:
        off.stop()
        patch.undo()


def test_a_stop_ends_the_background_work(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    muse: FakeAudioMuse,
) -> None:
    other, patch = start(navidrome_factory, tmp_path_factory.mktemp("sound-stop"), muse)
    try:
        wait_for_facts(other, 6)
    finally:
        other.stop()  # uvicorn's shutdown: the sync is canceled, the client closed
        patch.undo()
    seen = len(muse.requests())
    time.sleep(0.3)
    assert len(muse.requests()) == seen  # nothing asks any more


# --- the page ------------------------------------------------------------------------------


def test_the_page_is_for_administrators(browser: Browser) -> None:
    assert browser.get("sound").status_code == 303  # to the sign-in
    assert browser.sign_in(LISTENER, LISTENER_PASSWORD).status_code == 303
    assert browser.get("sound").status_code == 403
    assert browser.post(
        "sound", {"url": "http://x.invalid", "csrf": browser.csrf()}
    ).status_code in (403,)


def test_the_page_shows_what_is_connected(
    world: DeliveryWorld, admin: Browser, muse: FakeAudioMuse
) -> None:
    wait_for_facts(world, 6)
    page = admin.get("sound")
    assert page.status_code == 200
    assert "<span>Sound analysis</span></a>" in admin.get("addons").text  # in the menu
    assert "Answering" in page.text and "6 songs have sound facts, read" in page.text
    assert "/blob/main/docs/sound-analysis.md" in page.text  # the setup guide
    assert f'value="{muse.base_url}"' in page.text and 'value="Shijhon"' in page.text
    assert TOKEN not in page.text  # the token is write-only
    assert "From the configuration" in row_of(page.text, "audiomuse-token")


def test_settings_are_saved_apply_after_a_restart_and_the_token_stays_secret(
    world: DeliveryWorld,
    admin: Browser,
    caplog: pytest.LogCaptureFixture,
    muse: FakeAudioMuse,
) -> None:
    quiet_libraries()  # (as Shijhon runs: these would print the statements' values)
    caplog.set_level(logging.DEBUG)
    done = admin.submit("sound", {"sync_hours": "12", "token": NEW_TOKEN, "server": "Shijhon"})
    assert done.status_code == 303 and done.headers["location"] == f"{PATH}/sound"
    rows = saved_rows(world, "audiomuse")
    assert set(rows) == {"sync_hours", "token", "(token for)"}  # (and where it was entered)
    assert rows["sync_hours"] == "12.0" and rows["token"] == f'"{NEW_TOKEN}"'
    page = admin.get("sound")
    assert "Restart needed" in page.text and "read the analysis every" in page.text.lower()
    assert NEW_TOKEN not in page.text and TOKEN not in page.text
    assert NEW_TOKEN not in caplog.text and TOKEN not in caplog.text
    # (It applies after a restart: the connection in use has the old token.)
    assert world.server.call(sound_of(world).status).reachable
    for response in admin.seen:
        assert NEW_TOKEN not in response.text and NEW_TOKEN not in str(response.headers)
    # A value equal to the configuration's removes the saved one; a token is removed on request.
    admin.submit("sound", {"sync_hours": "1", "token" + "-clear": "true"})
    assert saved_rows(world, "audiomuse") == {}


def test_the_address_moves_to_another_host_only_with_the_token_typed_with_it(
    world: DeliveryWorld, admin: Browser, muse: FakeAudioMuse
) -> None:
    refused = admin.submit("sound", {"url": "http://elsewhere.invalid:9000"})
    assert refused.status_code == 400 and "Enter the token again" in refused.text
    assert saved_rows(world, "audiomuse") == {}
    assert TOKEN not in refused.text
    # Another path on the same host sends the token to the same place.
    same = admin.submit("sound", {"url": muse.base_url + "/muse"})
    assert same.status_code == 303 and saved_rows(world, "audiomuse") == {
        "url": f'"{muse.base_url}/muse"'
    }
    moved = admin.submit("sound", {"url": "http://elsewhere.invalid:9000", "token": NEW_TOKEN})
    assert moved.status_code == 303
    assert set(saved_rows(world, "audiomuse")) == {"url", "token", "(token for)"}
    assert "it is for another address" not in admin.get("sound").text

    # A token entered for another place than the address in use is not used, and says so.
    async def elsewhere() -> None:
        await world.services.store.execute(
            "UPDATE saved_settings SET value = ? WHERE key = '(token for)'", ['"somewhere else"']
        )

    world.server.call(elsewhere)
    page = admin.get("sound")
    assert "Enter it again" in row_of(page.text, "audiomuse-token")
    assert NEW_TOKEN not in page.text
    forget(world)  # (back to the configuration's)


def test_invalid_settings_are_refused_and_nothing_is_saved(
    world: DeliveryWorld, admin: Browser
) -> None:
    refused = admin.submit("sound", {"url": "ftp://muse.invalid", "sync_hours": "0"})
    assert refused.status_code == 400
    assert "Not saved" in refused.text
    assert saved_rows(world, "audiomuse") == {}
