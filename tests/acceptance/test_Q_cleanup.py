"""The dashboard's Cleanup page against a real Navidrome
whose database the cleanup reads (read only): what Shijhon added to the library by origin,
the cleanup's last check and what it listed (due, kept in use and why, not due yet), a
dry run from the page (paced), the cleanup settings - switching it on first shows what the
next check would take out - and the downloaded audio's limits, with the environment's
locks. The page itself never reads Navidrome's database."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator
from html import unescape
from typing import Any

import anyio
import pytest

from shijhon import cleanup as cleanup_module
from shijhon.cleanup import Cleanup, Swept
from shijhon.navidrome.usage import UsageSource
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

DAY = 86400.0


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("cleanup-page"),
        warm_ahead_depth=0,
        navidrome_database=nd.data / "navidrome.db",
        cleanup={"mode": "off"},
    ) as w:
        w.app.dashboard.attempts.limit = 10_000
        yield w


@pytest.fixture
def admin(world: DeliveryWorld) -> Iterator[Browser]:
    browser = Browser(world.server.base_url)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    yield browser
    browser.close()


def cleanup(world: DeliveryWorld) -> Cleanup:
    found = world.services.cleanup
    assert found is not None
    return found


def made(world: DeliveryWorld, key: str, *, days_ago: float = 40) -> tuple[str, list[str], str]:
    """A catalog album added ``days_ago``: (release, song IDs, album ID)."""
    release = catalog_release(key, f"Album {key}", f"Artist {key}", 2)
    result = world.materialize(release)
    ref = str(release.ref)

    async def added() -> None:
        await world.services.store.execute(
            "UPDATE releases SET created_at = ? WHERE ref = ?", [time.time() - days_ago * DAY, ref]
        )

    world.server.call(added)
    return ref, [result.created[t.ref] for t in release.tracks], result.album_id


def in_library(world: DeliveryWorld, ref: str) -> bool:
    async def find() -> bool:
        row = await world.services.store.fetchone("SELECT 1 FROM releases WHERE ref = ?", [ref])
        return row is not None

    return bool(world.server.call(find))


def saved_rows(world: DeliveryWorld, section: str) -> dict[str, str]:
    async def read() -> dict[str, str]:
        rows = await world.services.store.fetchall(
            "SELECT key, value FROM saved_settings WHERE section = ?", [section]
        )
        return {row["key"]: row["value"] for row in rows}

    return dict(world.server.call(read))


def wait_for_listing(world: DeliveryWorld, since: float) -> Swept:
    deadline = time.monotonic() + 30
    while True:
        last = cleanup(world).last_dry_run
        if last is not None and last.listed is not None and last.listed.at >= since:
            return last
        assert time.monotonic() < deadline, "the dry run did not finish"
        time.sleep(0.1)


@pytest.fixture
def no_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every read of a database by the cleanup (a page load must do none)."""
    opened: list[str] = []
    real = cleanup_module.open_read_only

    def counted(path: Any) -> Any:
        opened.append(str(path))
        return real(path)

    monkeypatch.setattr(cleanup_module, "open_read_only", counted)
    return opened


def test_the_page_shows_what_was_added_without_reading_navidromes_database(
    world: DeliveryWorld, admin: Browser, no_reads: list[str]
) -> None:
    made(world, "q-shown")
    page = admin.get("cleanup")
    assert page.status_code == 200
    html = page.text
    assert "<h1>Cleanup</h1>" in html
    assert "Catalog albums</dt><dd>" in html and "added when one of their songs was used" in html
    assert "Off: nothing is listed or taken out; who uses what is still noted" in html
    assert "Nothing listed yet" in html or "Would be taken out" in html
    assert 'name="mode"' in html and 'name="unused_days"' in html
    assert 'name="delivered_days"' in html and 'name="delivered_gb"' in html
    assert no_reads == []  # a cheap view: Shijhon's own records only


def test_a_dry_run_from_the_page_lists_what_would_go_and_why_the_rest_stays(
    world: DeliveryWorld, admin: Browser
) -> None:
    unused, _, _ = made(world, "q-unused")
    starred, songs, _ = made(world, "q-starred")
    world.client().ok("star", {"id": songs[0]})
    young, _, _ = made(world, "q-young", days_ago=1)
    started = time.time()
    pressed = admin.post("cleanup/check", {"csrf": admin.csrf()})
    assert pressed.status_code == 303
    swept = wait_for_listing(world, started)
    assert swept.manual and swept.mode == "dry_run"
    page = admin.get("cleanup").text
    assert "A dry run from here at" in page
    rows = page.split("<tbody>")[1].split("</tbody>")[0].split("<tr>")
    [out] = [r for r in rows if "Album q-unused" in r]
    assert "Would be taken out" in out and "catalog album" in out
    [kept] = [r for r in rows if "Album q-starred" in r]
    assert "Kept, in use: favorited" in kept
    assert "Album q-young" not in page  # not due: counted only
    assert "<dt>Not due yet</dt><dd>" in page and "becomes due on" in page
    # A dry run writes nothing, whatever the mode.
    assert all(in_library(world, ref) for ref in (unused, starred, young))
    # Paced: another press within the minute is refused, and the button says when.
    again = admin.post("cleanup/check", {"csrf": admin.csrf()})
    assert again.status_code == 303
    page = admin.get("cleanup").text
    assert "A dry run ran a moment ago: another can start at" in page
    assert "Again from" in page


def test_a_database_that_is_not_this_librarys_is_said_on_the_page(
    world: DeliveryWorld, admin: Browser, tmp_path: Any
) -> None:
    """Read from a database that does not know the library's songs, a dry run says
    that nothing is taken out and why, and the cleanup cannot be switched on by it."""
    unused, _, _ = made(world, "q-foreign")
    running = cleanup(world)
    foreign = tmp_path / "foreign-navidrome.db"
    assert running.usage is not None
    source = sqlite3.connect(f"file:{running.usage.path}?mode=ro", uri=True)
    copy = sqlite3.connect(foreign)
    source.backup(copy)
    source.close()
    copy.execute("DELETE FROM media_file")
    copy.commit()
    copy.close()
    real = running.usage
    running.usage = UsageSource(foreign, export=False)
    try:
        running.manual_at = None
        started = time.time()
        assert admin.post("cleanup/check", {"csrf": admin.csrf()}).status_code == 303
        swept = wait_for_listing(world, started)
        assert "do not have the songs of" in swept.refused
        page = admin.get("cleanup").text
        assert "Nothing is taken out: Navidrome&#39;s records do not have the songs of" in page
        rows = page.split("<tbody>")[1].split("</tbody>")[0].split("<tr>")
        [row] = [r for r in rows if "Album q-foreign" in r]
        assert "Not taken out" in row and "Would be taken out" not in page.split("<tbody>")[1]
        asked = admin.submit("cleanup", {"mode": "on", "unused_days": "33"}, "cleanup/settings")
        assert asked.status_code == 200
        assert "Nothing would be taken out: Navidrome&#39;s records do not" in asked.text
        assert "Nothing was saved." in asked.text and 'name="confirm"' not in asked.text
        assert running.mode == "off" and saved_rows(world, "cleanup") == {}
    finally:
        running.usage = real
        running.manual_at = None
        running.last_dry_run = None
    assert in_library(world, unused)


def test_switching_the_cleanup_on_first_shows_what_it_would_take_out(
    world: DeliveryWorld, admin: Browser
) -> None:
    made(world, "q-confirm")
    running = cleanup(world)
    try:
        asked = admin.submit("cleanup", {"mode": "on", "unused_days": "35"}, "cleanup/settings")
        assert asked.status_code == 200  # a question, nothing saved
        html = asked.text
        assert "Switch the cleanup on?" in html and "Artist q-confirm - Album q-confirm" in html
        assert "with the settings sent" in html
        assert running.mode == "off" and saved_rows(world, "cleanup") == {}
        # Confirmed: saved and live from the next check.
        # The confirmation sends the same settings again (a browser: its hidden fields),
        # with the token this confirmation was shown with.
        sent = dict(_hidden(html, 'id="confirm"'))
        assert sent["mode"] == "on" and sent["unused_days"] == "35" and len(sent["confirm"]) > 16
        # Not for other settings, nor without it: asked again.
        other = admin.post("cleanup/settings", sent | {"unused_days": "20"})
        assert other.status_code == 200 and "Switch the cleanup on?" in other.text
        assert running.mode == "off"
        forged = admin.post("cleanup/settings", sent | {"confirm": "on"})
        assert forged.status_code == 200 and running.mode == "off"
        html = forged.text
        sent = dict(_hidden(html, 'id="confirm"'))
        confirmed = admin.post("cleanup/settings", sent)
        assert confirmed.status_code == 303
        assert running.mode == "on" and running.unused_days == 35
        assert saved_rows(world, "cleanup") == {"mode": '"on"', "unused_days": "35.0"}
        page = admin.get("cleanup").text
        assert "The cleanup is on: its next check takes out" in page
        assert "Restart needed" not in page  # live
        # While on, fewer days take out more: asked first; more days are not.
        more = admin.submit("cleanup", {"unused_days": "10"}, "cleanup/settings")
        assert more.status_code == 200 and "Let the cleanup take out more?" in more.text
        assert running.unused_days == 35
        fewer = admin.submit("cleanup", {"unused_days": "40"}, "cleanup/settings")
        assert fewer.status_code == 303 and running.unused_days == 40
        # Off, or a dry run, needs no confirmation.
        off = admin.submit("cleanup", {"mode": "off"}, "cleanup/settings")
        assert off.status_code == 303 and running.mode == "off"
    finally:
        admin.submit("cleanup", {"mode": "off", "unused_days": "30"}, "cleanup/settings")
    assert saved_rows(world, "cleanup") == {}
    assert running.mode == "off" and running.unused_days == 30


def _hidden(html: str, after: str) -> list[tuple[str, str]]:
    """The hidden fields of the first form after ``after`` (what a browser sends)."""
    start = html.index(after)
    end = html.index("</form>", start)
    fields = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html[start:end])
    return [(unescape(name), unescape(value)) for name, value in fields]


def test_a_mode_the_environment_sets_is_locked(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = cleanup(world)
    monkeypatch.setattr(world.app, "locked", {"cleanup": {"mode": "SHIJHON_CLEANUP__MODE"}})
    try:
        page = admin.get("cleanup").text
        assert "Set by the environment" in page and "SHIJHON_CLEANUP__MODE" in page
        forged = admin.post("cleanup/settings", {"csrf": admin.csrf(), "mode": "on"})
        assert forged.status_code == 303  # nothing asked, nothing changed
        assert running.mode == "off" and saved_rows(world, "cleanup") == {}
    finally:
        monkeypatch.undo()


def test_the_downloaded_audio_limits_are_set_here(world: DeliveryWorld, admin: Browser) -> None:
    try:
        saved = admin.submit("cleanup", {"delivered_days": "14"}, "cleanup/downloads")
        assert saved.status_code == 303
        assert saved_rows(world, "delivery") == {"delivered_days": "14.0"}
        page = admin.get("cleanup").text
        assert "after Shijhon restarts: kept for." in page  # the expiry is built at startup
        assert 'name="delivered_days"' not in admin.get("playback").text
        bad = admin.submit("cleanup", {"delivered_gb": "-1"}, "cleanup/downloads")
        assert bad.status_code == 400 and "Not saved" in bad.text
    finally:
        admin.submit("cleanup", {"delivered_days": "30"}, "cleanup/downloads")
    assert saved_rows(world, "delivery") == {}


def test_the_daily_checks_result_is_shown(world: DeliveryWorld, admin: Browser) -> None:
    ref, _, _ = made(world, "q-daily")
    running = cleanup(world)
    clock, mode = running.clock, running.mode
    running.clock = lambda: time.time() + DAY  # a day on
    running.mode = "on"
    try:
        done = world.server.call(running.sweep)
    finally:
        running.clock, running.mode = clock, mode
    assert "Artist q-daily - Album q-daily" in done.removed
    page = admin.get("cleanup").text
    assert "The daily check at" in page
    [row] = [r for r in page.split("<tr>") if "Album q-daily" in r]
    assert "Taken out" in row
    assert "<dt>Taken out</dt>" in page and not in_library(world, ref)
    # A dry run from the page afterwards: listed, and the daily check's outcome still said.
    assert done.listed is not None
    done.listed.at = time.time() - 60  # (its clock ran a day ahead)
    running.manual_at = None
    started = time.time()
    assert admin.post("cleanup/check", {"csrf": admin.csrf()}).status_code == 303
    deadline = time.monotonic() + 30
    while (
        running.last_dry_run is None
        or running.last_dry_run.listed is None
        or (running.last_dry_run.listed.at < started)
    ):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    page = admin.get("cleanup").text
    assert "A dry run from here at" in page and "The daily check before, at" in page
    assert "taken out" in page.split("The daily check before, at")[1].split("</p>")[0]


def test_a_dry_run_that_is_not_done_cannot_be_confirmed(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count must be shown before the cleanup takes out more: a preview still running
    (or failing) offers no confirmation, and nothing is saved."""
    running = cleanup(world)

    async def slow(**settings: Any) -> None:
        return None  # not done within the wait

    monkeypatch.setattr(running, "preview", slow)
    asked = admin.submit("cleanup", {"mode": "on"}, "cleanup/settings")
    assert asked.status_code == 200 and "The dry run is taking a while" in asked.text
    assert 'name="confirm"' not in asked.text and "Nothing was saved." in asked.text
    assert running.mode == "off" and saved_rows(world, "cleanup") == {}


def test_a_refused_save_says_so_by_its_form(world: DeliveryWorld, admin: Browser) -> None:
    refused = admin.submit("cleanup", {"mode": "on", "unused_days": "0"}, "cleanup/settings")
    assert refused.status_code == 400 and "Switch the cleanup on?" not in refused.text
    settings = refused.text.split('id="settings-title"')[1].split('id="downloads-title"')[0]
    assert "Not saved" in settings and 'aria-invalid="true"' in settings
    assert cleanup(world).mode == "off" and saved_rows(world, "cleanup") == {}


def test_a_dry_run_waits_for_a_check_under_way(world: DeliveryWorld, admin: Browser) -> None:
    running = cleanup(world)
    running.checking_since = time.time()
    try:
        page = admin.get("cleanup").text
        assert "A check is under way since" in page
        assert admin.post("cleanup/check", {"csrf": admin.csrf()}).status_code == 303
        assert "A check is under way: its result shows here" in admin.get("cleanup").text
    finally:
        running.checking_since = None


def test_a_check_keeps_the_mode_it_started_with(world: DeliveryWorld) -> None:
    """Switched on while a dry run lists: that check still takes nothing out (only the
    next one does, with the settings confirmed)."""
    ref, _, _ = made(world, "q-snapshot")
    running = cleanup(world)
    listed = running._listed

    async def switched(**settings: Any) -> Any:
        running.mode = "on"  # the owner confirms "on" meanwhile
        return await listed(**settings)

    running.mode = "dry_run"
    running._listed = switched  # type: ignore[method-assign]
    try:
        done = world.server.call(running.sweep)
    finally:
        running._listed = listed  # type: ignore[method-assign]
        running.mode = "off"
    assert done.mode == "dry_run" and done.removed == [] and in_library(world, ref)


def test_a_failed_or_old_dry_run_cannot_be_confirmed(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview that failed says so (not as a failed check), and one older than a few
    minutes is not shown: a new one is made."""
    running = cleanup(world)

    async def failing(**settings: Any) -> Any:
        raise OSError("the database went away")

    monkeypatch.setattr(running, "_listed", failing)
    asked = admin.submit("cleanup", {"mode": "on", "unused_days": "33"}, "cleanup/settings")
    assert asked.status_code == 200 and "The dry run failed (OSError)" in asked.text
    assert 'name="confirm"' not in asked.text and "The last check failed" not in asked.text
    monkeypatch.undo()
    # An old preview for the same settings is never offered.
    from shijhon.cleanup import Listing

    running._preview = ((33.0, True, True), Listing(time.time() - 3600, []))
    fresh = admin.submit("cleanup", {"mode": "on", "unused_days": "33"}, "cleanup/settings")
    assert fresh.status_code == 200 and "Switch the cleanup on?" in fresh.text
    assert running._preview is not None and running._preview[1].at > time.time() - 60
    assert running.mode == "off" and saved_rows(world, "cleanup") == {}


def test_a_dry_run_that_fails_after_its_wait_says_so_at_the_next_save(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow preview goes on in the background; its failure is said when the settings are
    sent again, before another is made (never "taking a while" for good)."""
    running = cleanup(world)

    async def failing(**settings: Any) -> Any:
        await anyio.sleep(0.4)
        raise OSError("the database went away")

    monkeypatch.setattr(running, "_listed", failing)
    monkeypatch.setattr("shijhon.dashboard.cleanup.PREVIEW_SECONDS", 0.05)
    send = {"mode": "on", "unused_days": "34"}
    asked = admin.submit("cleanup", send, "cleanup/settings")
    assert asked.status_code == 200 and "The dry run is taking a while" in asked.text
    deadline = time.monotonic() + 10
    while running._previewing is not None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    again = admin.submit("cleanup", send, "cleanup/settings")
    assert "The dry run failed (OSError)" in again.text and 'name="confirm"' not in again.text
    # Said: sent once more, a new dry run is made.
    monkeypatch.undo()
    fresh = admin.submit("cleanup", send, "cleanup/settings")
    assert fresh.status_code == 200 and "Switch the cleanup on?" in fresh.text
    assert running.mode == "off" and saved_rows(world, "cleanup") == {}
