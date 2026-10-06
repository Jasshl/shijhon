"""The dashboard's Library page: the fill policy, the library pass's
mode and progress, and the review list's actions - fill from a chosen release (in the
background), keep as is, match again - against a real Navidrome with the demo catalog's
records replayed (the albums of suite I's review list).

Each test uses albums of its own and does not depend on the others' order."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.fill.fills import FillPolicy
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import PATH, Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay


def _reviewed(title: str) -> Album:
    # The recording is on two catalog albums, neither with this title: the review list.
    return Album(
        "Esme Ashdown",
        title,
        (Track("NORTHERN.", 14, seconds=118, isrc="ZZ-SHJ-00-00059"),),
        recording_date="2010",
    )


FILLED = _reviewed("Northern Hits")
REFUSED = _reviewed("Northern Hits Two")
REMATCHED = _reviewed("Northern Hits Three")
KEPT = _reviewed("Northern Hits Four")
LISTED = _reviewed("Northern Hits Five")  # left as it is
PART = _reviewed("Northern Hits Six")  # made a "part of" row in its test
BUSY = _reviewed("Northern Hits Seven")
RESTING = _reviewed("Northern Hits Eight")
ALBUMS = [FILLED, REFUSED, REMATCHED, KEPT, LISTED, PART, BUSY, RESTING]


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in ALBUMS:
        write_album(nd.music, album)
    nd.scan(full=True)
    fill = {
        "enabled": True,
        "open_budget_seconds": 15,
        "background_pause_seconds": 0,
        "auto_min_songs": 1,
        "library_pass": "off",  # its mode is changed in a test
        "pass_requests_per_second": 50,
    }
    replay = Replay()
    with delivery_world(
        nd, tmp_path_factory.mktemp("library"), catalog=replay.catalog(), fill=fill
    ) as w:
        w.app.dashboard.attempts.limit = 10_000
        for index, album in enumerate(ALBUMS):  # viewed: matched, to the review list
            w.client(client=f"viewer-{index}").ok("getAlbum", {"id": album_id(w, album.title)})
        deadline = time.monotonic() + 60  # a slow machine: the matches may go on after a view
        while any(outcome(w, album) != "review" for album in ALBUMS):
            assert time.monotonic() < deadline, "the albums did not reach the review list"
            time.sleep(0.2)
        yield w


@pytest.fixture
def admin(world: DeliveryWorld) -> Iterator[Browser]:
    browser = Browser(world.server.base_url)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    yield browser
    browser.close()


def album_id(world: DeliveryWorld, title: str) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == title]
    return str(ident)


def outcome(world: DeliveryWorld, album: Album) -> str | None:
    ident = album_id(world, album.title)

    async def read() -> str | None:
        found = await world.services.store.fetchone(
            "SELECT outcome FROM album_matches WHERE album_id = ? AND scope = 'demo.xx'",
            [ident],
        )
        return found["outcome"] if found else None

    return world.server.call(read)


def review_row(page: str, title: str) -> str:
    rows = page.split("<tr>")
    found = [row for row in rows if f'<span class="cell-title">{title}</span>' in row]
    assert found, title
    return found[0]


def action(admin: Browser, world: DeliveryWorld, album: Album, **fields: str) -> Any:
    ident = album_id(world, album.title)
    return admin.post(f"library/review/{ident}", {"csrf": admin.csrf(), "page": "1", **fields})


def wait_for_result(world: DeliveryWorld, album: Album, seconds: float = 90) -> None:
    ident = album_id(world, album.title)
    deadline = time.monotonic() + seconds
    while world.app.dashboard.jobs.running(ident) is not None:
        assert time.monotonic() < deadline, "the background action did not finish"
        time.sleep(0.2)


def test_the_review_list_shows_albums_and_their_releases(
    world: DeliveryWorld, admin: Browser
) -> None:
    page = admin.get("library").text
    assert "<h1>Library</h1>" in page and 'id="review"' in page
    row = review_row(page, LISTED.title)
    assert "Esme Ashdown" in row and "Only on releases with other titles" in row
    assert 'name="action" value="fill"' in row and 'name="action" value="keep"' in row
    # The releases considered, described by the catalog (title, year, songs).
    assert re.search(r'<option value="demo:\d+">[^<]+, \d{4}, \d+ songs', row), row
    assert "To review" in page and re.search(r"<dd>\d+</dd>", page)


def test_the_meter_counts_albums_checked_with_or_without_a_match(admin: Browser) -> None:
    page = admin.get("library").text
    meter = re.search(r'<span class="meter-value">([\d,]+) of ([\d,]+)</span> albums checked', page)
    assert meter, page
    counts = re.search(r'<dl class="counts">(.*?)</dl>', page, re.S)
    assert counts
    outcomes = re.findall(r"<dt>[^<]+</dt><dd>([\d,]+)</dd>", counts.group(1))
    # Albums to review have no confident match: checked, and not called matched.
    assert _count(page, "To review") >= 1 and "</span> albums matched" not in page
    assert int(meter.group(1).replace(",", "")) == sum(int(n.replace(",", "")) for n in outcomes)


def test_fill_from_a_chosen_release_in_the_background(world: DeliveryWorld, admin: Browser) -> None:
    started = action(admin, world, FILLED, action="fill", release="demo:900000101")
    assert started.status_code == 303 and started.headers["location"].endswith("#review")
    assert "This can take a minute" in html.unescape(admin.get("library").text)
    wait_for_result(world, FILLED)
    assert outcome(world, FILLED) == "filled"
    page = html.unescape(admin.get("library").text)
    assert re.search(r"Northern Hits filled from .+: 13 songs added\.", page)
    assert f'<span class="cell-title">{FILLED.title}</span>' not in page  # off the list
    # The same release for another album: refused, with the reason, and it stays listed.
    action(admin, world, REFUSED, action="fill", release="demo:900000101")
    wait_for_result(world, REFUSED)
    page = html.unescape(admin.get("library").text)
    assert (
        "Northern Hits Two was not filled: the release is in the library as another album." in page
    )
    assert outcome(world, REFUSED) == "review"


def test_only_a_listed_release_can_be_chosen(world: DeliveryWorld, admin: Browser) -> None:
    refused = action(admin, world, LISTED, action="fill", release="demo:999999999")
    assert refused.status_code == 303
    assert "choose one of the listed releases" in admin.get("library").text
    assert outcome(world, LISTED) == "review"


def test_keep_as_is(world: DeliveryWorld, admin: Browser) -> None:
    kept = action(admin, world, KEPT, action="keep")
    assert kept.status_code == 303
    assert "is kept as it is" in admin.get("library").text
    assert outcome(world, KEPT) == "kept"
    assert f'<span class="cell-title">{KEPT.title}</span>' not in admin.get("library").text


def test_match_again(world: DeliveryWorld, admin: Browser) -> None:
    again = action(admin, world, REMATCHED, action="rematch")
    assert again.status_code == 303
    wait_for_result(world, REMATCHED)
    page = html.unescape(admin.get("library").text)
    assert "Northern Hits Three still needs review: only on releases with other titles." in page
    assert outcome(world, REMATCHED) == "review"


def test_the_fill_policy_applies_at_once(world: DeliveryWorld, admin: Browser) -> None:
    fills = world.services.fills
    assert fills is not None
    try:
        saved = admin.submit(
            "library", {"auto_min_songs": "2", "auto_min_share": "40"}, action="library/policy"
        )
        assert saved.status_code == 303
        assert fills.policy == FillPolicy(2, 0.4)
        page = admin.get("library").text
        assert 'name="auto_min_share"' in page and 'value="40"' in page
        assert "Saved 2 changes at" in page
        bad = admin.submit(
            "library", {"auto_min_songs": "2", "auto_min_share": "150"}, action="library/policy"
        )
        assert bad.status_code == 400
        assert "Enter a percentage from 0 to 100, e.g. 25." in bad.text and "Not saved" in bad.text
        # The other forms keep their values after a failed save.
        advanced = bad.text[bad.text.index('action="/shijhon/library/settings"') :]
        assert (
            'name="open_budget_seconds" type="text" inputmode="decimal" autocomplete="off"'
            ' spellcheck="false" value="15"' in advanced
        )
        assert re.search(r'name="library_pass" value="off" checked', bad.text)
        comma = admin.submit(
            "library", {"auto_min_songs": "2", "auto_min_share": "12,5"}, action="library/policy"
        )
        assert comma.status_code == 303
        assert fills.policy == FillPolicy(2, 0.125)
        assert 'value="12.5"' in admin.get("library").text
    finally:
        admin.submit(
            "library", {"auto_min_songs": "1", "auto_min_share": "25"}, action="library/policy"
        )
    assert fills.policy == FillPolicy(1, 0.25)


def test_the_pass_mode_changes_from_its_next_run_or_after_a_restart(
    world: DeliveryWorld, admin: Browser
) -> None:
    passing = world.services.library_pass
    dashboard = world.app.dashboard
    assert passing is not None and dashboard.running is not None
    running = dashboard.running
    try:
        # It started off: switching it on needs a restart (its loop is not running).
        started = admin.submit("library", {"library_pass": "dry_run"}, action="library/pass")
        assert started.status_code == 303
        page = admin.get("library").text
        assert "becomes a dry run when Shijhon restarts" in page
        assert "after Shijhon restarts: library pass." in page  # the restart banner
        assert passing.mode == "off"
        # Between dry run and on it changes from the next run.
        passing.mode = "dry_run"
        dashboard.running = running.model_copy(
            update={"fill": running.fill.model_copy(update={"library_pass": "dry_run"})}
        )
        assert (
            admin.submit("library", {"library_pass": "on"}, action="library/pass").status_code
            == 303
        )
        assert passing.mode == "on"
        assert "The library pass is on from its next run." in admin.get("library").text
        # Off while it is on: off at the restart, and no fills from its next run (a dry run).
        admin.submit("library", {"library_pass": "off"}, action="library/pass")
        assert passing.mode == "dry_run"
        page = admin.get("library").text
        assert "fills nothing from its next run: dry runs until Shijhon restarts" in page
        assert "after Shijhon restarts: library pass." in page  # off still waits
        # On again before the restart: nothing waits any more.
        admin.submit("library", {"library_pass": "on"}, action="library/pass")
        assert passing.mode == "on"
        assert "Restart needed" not in admin.get("library").text
    finally:
        passing.mode = "off"
        dashboard.running = running
        admin.submit("library", {"library_pass": "off"}, action="library/pass")


def test_advanced_fill_settings_are_generated(world: DeliveryWorld, admin: Browser) -> None:
    fills = world.services.fills
    assert fills is not None
    page = admin.get("library").text
    from shijhon.config import FillSettings

    for key in FillSettings.model_fields:
        assert f'name="{key}"' in page, key
    try:
        saved = admin.submit("library", {"open_budget_seconds": "4,5"}, action="library/settings")
        assert saved.status_code == 303
        assert fills.budget == 4.5
    finally:
        admin.submit("library", {"open_budget_seconds": "15"}, action="library/settings")


def test_review_actions_need_the_session_token(world: DeliveryWorld, admin: Browser) -> None:
    ident = album_id(world, REFUSED.title)
    assert admin.post(f"library/review/{ident}", {"action": "keep", "csrf": "x"}).status_code == 403
    other = Browser(world.server.base_url)
    anonymous = other.post(f"library/review/{ident}", {"action": "keep"})
    assert anonymous.status_code == 303 and f"{PATH}/sign-in" in anonymous.headers["location"]
    other.close()
    assert outcome(world, REFUSED) == "review"


def test_a_part_of_another_album_is_not_filled_on_its_own(
    world: DeliveryWorld, admin: Browser
) -> None:
    ident = album_id(world, PART.title)

    async def make_part() -> None:
        await world.services.store.execute(
            "UPDATE album_matches SET reason = ? WHERE album_id = ?",
            ["part of Esme Ashdown - Northern Hits (album x1): filled there", ident],
        )

    world.server.call(make_part)
    row = review_row(admin.get("library").text, PART.title)
    assert "Part of another album" in row and 'value="fill"' not in row
    refused = action(admin, world, PART, action="fill", release="demo:900000101")
    assert refused.status_code == 303
    assert "is not filled on its own" in admin.get("library").text
    assert outcome(world, PART) == "review" and world.app.dashboard.jobs.running(ident) is None


def test_one_action_at_a_time_for_an_album(world: DeliveryWorld, admin: Browser) -> None:
    ident = album_id(world, BUSY.title)
    jobs = world.app.dashboard.jobs
    job = jobs.claim(ident, BUSY.title, "fill")
    assert job is not None
    try:
        row = review_row(admin.get("library").text, BUSY.title)
        assert "Filling since" in row and 'value="keep"' not in row
        action(admin, world, BUSY, action="keep")
        assert "is busy with the last action" in admin.get("library").text
        assert outcome(world, BUSY) == "review"
    finally:
        jobs.drop(job)


def test_matching_again_waits_for_the_catalogs_rest(world: DeliveryWorld, admin: Browser) -> None:
    fills = world.services.fills
    assert fills is not None
    fills.resting_until = fills.clock() + 600
    try:
        action(admin, world, RESTING, action="rematch")
        assert "is not matched again now: the catalog is resting" in admin.get("library").text
        assert outcome(world, RESTING) == "review"  # still listed
    finally:
        fills.resting_until = 0.0


def test_a_page_past_the_end_shows_the_last_one(admin: Browser) -> None:
    for page in ("999", str(10**20), "abc"):
        response = admin.get(f"library?page={page}")
        assert response.status_code == 200
        assert "Nothing to review" not in response.text
        assert 'class="cell-title"' in response.text


def test_a_pass_mode_set_by_the_environment_is_locked(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        world.app, "locked", {"fill": {"library_pass": "SHIJHON_FILL__LIBRARY_PASS"}}
    )
    page = admin.get("library").text
    assert "SHIJHON_FILL__LIBRARY_PASS" in page and 'name="library_pass"' not in page
    assert "Set by the environment" in page


def _count(page: str, label: str) -> int:
    found = re.search(rf"<dt>{label}</dt><dd>([\d,]+)</dd>", page)
    return int(found.group(1).replace(",", "")) if found else 0


def test_deferred_albums_the_policy_allows_count_as_would_fill(
    world: DeliveryWorld, admin: Browser
) -> None:
    """An album kept to fill on first use under an earlier policy that
    the current one allows is filled as soon as automatic fills run (the pass on; views
    while it is off): it counts as "would fill", and "off" says it fills."""
    ident = album_id(world, LISTED.title)

    async def as_deferred(outcome: str, owned: int, tracks: int) -> Any:
        store = world.services.store
        row = await store.fetchone(
            "SELECT outcome, owned_songs, release_tracks FROM album_matches WHERE album_id = ?",
            [ident],
        )
        await store.execute(
            "UPDATE album_matches SET outcome = ?, owned_songs = ?, release_tracks = ?"
            " WHERE album_id = ?",
            [outcome, owned, tracks, ident],
        )
        return row

    before = admin.get("library").text
    old = world.server.call(lambda: as_deferred("deferred", 1, 12))
    try:
        page = admin.get("library").text  # the policy: 1 song
        assert _count(page, "Would fill") == _count(before, "Would fill") + 1
        assert _count(page, "To fill when used") == _count(before, "To fill when used")
        assert "those that meet the policy are filled then" in page  # the pass is off
        assert "<strong>Off</strong> stops the pass: such albums are filled once viewed" in page
        admin.submit(
            "library", {"auto_min_songs": "2", "auto_min_share": "25"}, action="library/policy"
        )
        page = admin.get("library").text  # 1 of 12: below the policy now
        assert _count(page, "Would fill") == _count(before, "Would fill")
        assert _count(page, "To fill when used") == _count(before, "To fill when used") + 1
    finally:
        admin.submit(
            "library", {"auto_min_songs": "1", "auto_min_share": "25"}, action="library/policy"
        )
        world.server.call(
            lambda: as_deferred(old["outcome"], old["owned_songs"], old["release_tracks"])
        )
