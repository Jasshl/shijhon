"""The dashboard's Library page: the fill policy, the library pass's
mode and progress, and the review list's actions - fill from a chosen release (in the
background), keep as is, match again - against a real Navidrome with the demo catalog's
records replayed (the albums of suite I's review list).

Each test uses albums of its own and does not depend on the others' order."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from shijhon.fill.fills import ChoiceRefused, Decision, FillPolicy
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
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
UNMATCHED = Album(
    "Fable Lantern",
    "Quiet Constellation",
    (Track("Amber Lines", 1), Track("Soft Arc", 3)),
)


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in [*ALBUMS, UNMATCHED]:
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
        prepare_collection(nd, w.server)
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


@pytest.fixture
def negative(world: DeliveryWorld) -> Iterator[str]:
    """A current negative row for a real synthetic owned album, restored after each test."""
    ident = album_id(world, UNMATCHED.title)

    async def seed() -> None:
        await world.services.store.execute(
            "INSERT INTO album_matches (album_id, scope, outcome, reason, checked_at,"
            " title, artist, owned_songs) VALUES (?, 'demo.xx', 'none', ?, ?, ?, ?, 2)",
            [ident, "no release contains every owned song", time.time(), UNMATCHED.title,
             UNMATCHED.artist],
        )  # fmt: skip

    world.server.call(seed)
    yield ident
    wait_for_result(world, UNMATCHED)
    world.server.call(
        lambda: world.services.store.execute(
            "DELETE FROM album_matches WHERE album_id = ?", [ident]
        )
    )


def negative_action(admin: Browser, ident: str, **fields: str) -> Any:
    return admin.post(
        f"library/no-match/{ident}",
        {"csrf": admin.csrf(), "page": "1", "action": "recheck", **fields},
    )


def negative_section(page: str) -> str:
    return page.split('id="no-match">', 1)[1].split("</section>", 1)[0]


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
        assert "after Shijhon restarts: library pass (Library)." in page  # the restart banner
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
        assert "after Shijhon restarts: library pass (Library)." in page  # off still waits
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
        assert (
            "<strong>Off</strong> stops the pass: albums are matched, and filled if they meet"
            " the policy, when an app views them" in page
        )
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


def test_no_match_lists_the_album_artist_reason_and_owned_count(
    world: DeliveryWorld, admin: Browser, negative: str
) -> None:
    page = admin.get("library").text
    section = negative_section(page)
    row = review_row(section, UNMATCHED.title)
    assert UNMATCHED.artist in row and 'data-label="Owned">2 songs' in row
    assert "No release contains every owned song" in row
    assert f'action="{PATH}/library/no-match/{negative}"' in row
    assert 'name="action" value="recheck">Match again' in row
    assert 'value="fill"' not in row and 'value="keep"' not in row
    assert "this adds no files" in section
    assert LISTED.title not in section and LISTED.title in page


def test_no_match_pages_are_scoped_and_clamp_to_the_last_page(
    world: DeliveryWorld, admin: Browser, negative: str
) -> None:
    async def seed() -> None:
        for index in range(27):
            await world.services.store.execute(
                "INSERT INTO album_matches (album_id, scope, outcome, checked_at, title, artist)"
                " VALUES (?, 'demo.xx', 'none', ?, ?, 'Aster Quill')",
                [f"no-match-page-{index}", time.time(), f"Absent {index:02}"],
            )
        await world.services.store.execute(
            "INSERT INTO album_matches (album_id, scope, outcome, checked_at, title, artist)"
            " VALUES (?, 'demo.yy', 'none', ?, 'Another region', 'Aster Quill')",
            [negative, time.time()],
        )

    world.server.call(seed)
    try:
        first = negative_section(admin.get("library").text)
        assert "Absent 00" in first and "Absent 24" in first and "Absent 25" not in first
        assert "Albums 1\u201325 of 28" in first and "?no_match=2#no-match" in first
        assert "Another region" not in first
        for page in ("2", "999999"):
            last = negative_section(admin.get(f"library?no_match={page}").text)
            assert "Absent 25" in last and "Absent 26" in last and UNMATCHED.title in last
            assert "Absent 24" not in last and "Another region" not in last
            assert "Albums 26\u201328 of 28" in last and "?no_match=1#no-match" in last
        for page in ("0", "abc", str(10**20)):
            assert "Absent 00" in negative_section(admin.get(f"library?no_match={page}").text)
    finally:
        world.server.call(
            lambda: world.services.store.execute(
                "DELETE FROM album_matches WHERE album_id LIKE 'no-match-page-%'"
            )
        )


def test_legacy_negative_rows_show_the_native_id_without_fetching_more_metadata(
    world: DeliveryWorld, admin: Browser, negative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.server.call(
        lambda: world.services.store.execute(
            "UPDATE album_matches SET title = '', artist = '', owned_songs = 0, reason = ''"
            " WHERE album_id = ?",
            [negative],
        )
    )

    async def forbidden(ident: str) -> None:
        pytest.fail("the Library page must not look up metadata for a legacy negative row")

    monkeypatch.setattr(world.services.navidrome, "album", forbidden)
    page = admin.get("library")
    assert page.status_code == 200
    row = review_row(negative_section(page.text), f"Unknown album ({negative})")
    assert "Unknown artist" in row and "No reason given" in row
    assert 'data-label="Owned">Unknown' in row

    async def unavailable(ident: str) -> None:
        return None

    monkeypatch.setattr(world.services.fills, "recheck", unavailable, raising=False)
    assert negative_action(admin, negative).status_code == 303
    wait_for_result(world, UNMATCHED)
    result = admin.get("library").text
    assert f"Unknown album ({negative}) was not matched" in result
    assert outcome(world, UNMATCHED) == "none"


def test_no_match_action_waits_while_busy_or_the_catalog_rests(
    world: DeliveryWorld, admin: Browser, negative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fills = world.services.fills
    assert fills is not None

    async def forbidden(ident: str) -> None:
        pytest.fail("a busy or resting album must not be rechecked now")

    monkeypatch.setattr(fills, "recheck", forbidden, raising=False)
    fills.resting_until = fills.clock() + 600
    try:
        assert negative_action(admin, negative).status_code == 303
        assert "the catalog is resting" in admin.get("library").text
        assert world.app.dashboard.jobs.running(negative) is None
    finally:
        fills.resting_until = 0.0
    jobs = world.app.dashboard.jobs
    job = jobs.claim(negative, UNMATCHED.title, "recheck")
    assert job is not None
    try:
        row = review_row(negative_section(admin.get("library").text), UNMATCHED.title)
        assert "Matching again since" in row and 'value="recheck"' not in row
        assert negative_action(admin, negative).status_code == 303
        assert "busy with the last action" in admin.get("library").text
        assert outcome(world, UNMATCHED) == "none"
    finally:
        jobs.drop(job)


@pytest.mark.parametrize("new_outcome", ["none", "review", "complete", "deferred"])
def test_no_match_action_reports_the_new_result_without_adding_files(
    world: DeliveryWorld,
    admin: Browser,
    negative: str,
    monkeypatch: pytest.MonkeyPatch,
    new_outcome: str,
) -> None:
    fills = world.services.fills
    assert fills is not None
    calls = []

    async def recheck(ident: str) -> Decision:
        calls.append(ident)
        await world.services.store.execute(
            "UPDATE album_matches SET outcome = ?, reason = ? WHERE album_id = ? AND scope = ?",
            [new_outcome, "several plausible editions", ident, fills.scope],
        )
        return Decision(new_outcome, "several plausible editions")

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a negative recheck must use only Fills.recheck")

    monkeypatch.setattr(fills, "recheck", recheck, raising=False)
    monkeypatch.setattr(fills, "rematch", forbidden)
    monkeypatch.setattr(fills, "fill", forbidden)
    before = _library_files(world)
    placeholders = _placeholder_count(world)
    started = negative_action(admin, negative, page="2")
    assert started.status_code == 303
    assert started.headers["location"].endswith("?no_match=2#no-match")
    wait_for_result(world, UNMATCHED)
    page = html.unescape(admin.get("library").text)
    expected = {
        "none": f"No release matches {UNMATCHED.artist} \u2013 {UNMATCHED.title}.",
        "review": "still needs review: several releases match.",
        "complete": "is complete as it is.",
        "deferred": "it matches a release now. It is shown complete",
    }
    assert expected[new_outcome] in page
    assert calls == [negative] and outcome(world, UNMATCHED) == new_outcome
    assert (UNMATCHED.title in negative_section(page)) == (new_outcome == "none")
    assert _library_files(world) == before and _placeholder_count(world) == placeholders


def _library_files(world: DeliveryWorld) -> list[tuple[str, int, int]]:
    return sorted(
        (str(path.relative_to(world.nd.music)), path.stat().st_size, path.stat().st_mtime_ns)
        for path in world.nd.music.rglob("*")
        if path.is_file()
    )


def _placeholder_count(world: DeliveryWorld) -> int:
    async def count() -> int:
        row = await world.services.store.fetchone("SELECT COUNT(*) AS n FROM placeholders")
        assert row is not None
        return int(row["n"])

    return world.server.call(count)


@pytest.mark.parametrize("new_outcome", ["kept", "deferred", "review", "complete", "filled"])
def test_no_match_post_refuses_an_album_whose_state_changed(
    world: DeliveryWorld,
    admin: Browser,
    negative: str,
    monkeypatch: pytest.MonkeyPatch,
    new_outcome: str,
) -> None:
    async def forbidden(ident: str) -> None:
        pytest.fail("the POST guard must reject other outcomes before starting a recheck")

    monkeypatch.setattr(world.services.fills, "recheck", forbidden, raising=False)
    world.server.call(
        lambda: world.services.store.execute(
            "UPDATE album_matches SET outcome = ? WHERE album_id = ? AND scope = 'demo.xx'",
            [new_outcome, negative],
        )
    )
    assert negative_action(admin, negative).status_code == 303
    assert "match has changed" in html.unescape(admin.get("library").text)
    assert outcome(world, UNMATCHED) == new_outcome
    assert world.app.dashboard.jobs.running(negative) is None


def test_no_match_post_refuses_a_negative_in_another_scope(
    world: DeliveryWorld, admin: Browser, negative: str
) -> None:
    world.server.call(
        lambda: world.services.store.execute(
            "UPDATE album_matches SET scope = 'demo.yy' WHERE album_id = ?", [negative]
        )
    )
    assert UNMATCHED.title not in negative_section(admin.get("library").text)
    assert negative_action(admin, negative).status_code == 303
    assert "match has changed" in html.unescape(admin.get("library").text)
    assert world.app.dashboard.jobs.running(negative) is None


@pytest.mark.parametrize("action_name", ["fill", "keep", "rematch"])
def test_negative_rows_cannot_use_the_review_actions(
    world: DeliveryWorld, admin: Browser, negative: str, action_name: str
) -> None:
    assert negative_action(admin, negative, action=action_name).status_code == 303
    assert "Choose Match again" in admin.get("library").text
    response = action(admin, world, UNMATCHED, action=action_name, release="demo:900000101")
    assert response.status_code == 303
    assert "no longer in the review list" in admin.get("library").text
    assert outcome(world, UNMATCHED) == "none"
    assert world.app.dashboard.jobs.running(negative) is None


@pytest.mark.parametrize("state", ["kept", "deferred", "another scope"])
def test_no_match_job_checks_the_current_row_before_rechecking(
    world: DeliveryWorld,
    admin: Browser,
    negative: str,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    pending: list[Callable[[], Awaitable[None]]] = []

    def queue(work: Callable[[], Awaitable[None]]) -> bool:
        pending.append(work)
        return True

    async def forbidden(ident: str) -> None:
        pytest.fail("the job must check current-scope negative eligibility again")

    monkeypatch.setattr(world.app.dashboard.host, "try_spawn", queue)
    monkeypatch.setattr(world.services.fills, "recheck", forbidden, raising=False)
    assert negative_action(admin, negative).status_code == 303
    assert len(pending) == 1
    world.server.call(
        lambda: world.services.store.execute(
            "UPDATE album_matches SET outcome = ?, scope = ? WHERE album_id = ?",
            ["none" if state == "another scope" else state,
             "demo.yy" if state == "another scope" else "demo.xx", negative],
        )
    )  # fmt: skip
    world.server.call(pending[0])
    assert "the match has changed" in admin.get("library").text
    assert outcome(world, UNMATCHED) == (None if state == "another scope" else state)


@pytest.mark.parametrize("new_outcome", ["kept", "deferred"])
def test_no_match_job_reports_atomic_state_change_refusal_without_overwriting_it(
    world: DeliveryWorld,
    admin: Browser,
    negative: str,
    monkeypatch: pytest.MonkeyPatch,
    new_outcome: str,
) -> None:
    async def changed(ident: str) -> Decision:
        # A concurrent decision wins after the dashboard's job eligibility check.
        await world.services.store.execute(
            "UPDATE album_matches SET outcome = ? WHERE album_id = ? AND scope = 'demo.xx'",
            [new_outcome, ident],
        )
        raise ChoiceRefused("the album is no longer a negative match")

    monkeypatch.setattr(world.services.fills, "recheck", changed, raising=False)
    assert negative_action(admin, negative).status_code == 303
    wait_for_result(world, UNMATCHED)
    assert "the match has changed" in admin.get("library").text
    assert outcome(world, UNMATCHED) == new_outcome


def test_no_match_actions_need_admin_authentication_and_csrf(
    world: DeliveryWorld, admin: Browser, negative: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = f"library/no-match/{negative}"
    assert admin.get(endpoint).status_code == 405
    assert admin.post(endpoint, {"action": "recheck", "csrf": "bad"}).status_code == 403
    assert admin.post(endpoint, {"action": "recheck"}).status_code == 403
    token = admin.csrf()
    assert (
        admin.post(
            endpoint, {"action": "recheck", "csrf": token}, headers={"sec-fetch-site": "cross-site"}
        ).status_code
        == 403
    )
    anonymous = Browser(world.server.base_url)
    try:
        response = anonymous.post(endpoint, {"action": "recheck", "csrf": token})
        assert response.status_code == 303 and "/sign-in" in response.headers["location"]
    finally:
        anonymous.close()

    from shijhon.access import AccessDenied, AccessService

    async def revoked(*args: Any, **kwargs: Any) -> None:
        raise AccessDenied

    monkeypatch.setattr(AccessService, "personal", revoked)
    response = admin.post(endpoint, {"action": "recheck", "csrf": token})
    assert response.status_code == 303 and "reason=access" in response.headers["location"]
    assert outcome(world, UNMATCHED) == "none"
    assert world.app.dashboard.jobs.running(negative) is None
