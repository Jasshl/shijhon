"""The Library page's search and its actions for several albums, against a real Navidrome
with the demo catalog's records replayed (the automatic picks' world: albums the catalog
has in two editions, picked and not filled, and review rows written as a match leaves them).

A search narrows every list to the albums whose title and artist hold each of its words.
The albums ticked in a list - or all it held under the search when the page was rendered -
get one action, one album after another in the background, each through the action it
would get alone; the page shows how far it is, and stopping leaves the others as they are.
The action acts on what the page showed: its form carries a token for the list as it was
(kept on the server, the viewer's own, short-lived, good for one action), and an album
whose match changed since is left alone and said.

Each test uses albums of its own and does not depend on the others' order."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest

from shijhon.dashboard import library
from tests.acceptance.test_I_auto_pick import (
    IRON,
    SALT,
    STONES,
    WOLVES,
    album_id,
    replay_for_picks,
    row,
    songs,
    view,
    wait_for,
    world_with,
)
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import PATH, Browser
from tests.harness.delivery import DeliveryWorld
from tests.harness.library import Album, Track
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

SCOPE = "demo.xx"
NOT_JOINED = "the placeholders did not join the album"  # a review no pick settles


def _owned(artist: str, title: str) -> Album:
    """An owned album no catalog release matches: its row is written by its test."""
    return Album(artist, title, (Track("Quiet Harbor Road", 1, seconds=201),))


# Review rows of their own for the tests that keep, match again or only list.
KEPT_ONE = _owned("Åsa Lindqvist", "Paper Lanterns")
KEPT_TWO = _owned("Åsa Lindqvist", "Paper Boats")
LEFT = _owned("Åsa Lindqvist", "Glass Harbour")
FOUND_ONE = _owned("Milo Tanager", "Signal Fires (Live)")
FOUND_TWO = _owned("Milo Tanager", "Signal Flags")
OTHER = _owned("Petra Voss", "Night Signals")
STOP_ONE = _owned("Corin Vale", "Tide Tables One")
STOP_TWO = _owned("Corin Vale", "Tide Tables Two")
STOP_THREE = _owned("Corin Vale", "Tide Tables Three")
UNCHOSEN = _owned("Ines Calder", "Low Winter Sun")
NEGATIVE_ONE = _owned("Rhea Marsh", "Salt Flats")
NEGATIVE_TWO = _owned("Rhea Marsh", "Salt Roads")
PAGED = [_owned("Hollis Brandt", f"Field Notes {number:02d}") for number in range(1, 28)]
OWNED = [KEPT_ONE, KEPT_TWO, LEFT, FOUND_ONE, FOUND_TWO, OTHER, STOP_ONE, STOP_TWO, STOP_THREE,
         UNCHOSEN, NEGATIVE_ONE, NEGATIVE_TWO, *PAGED]  # fmt: skip
PICKED = [SALT, STONES, WOLVES, IRON]


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    tmp = tmp_path_factory.mktemp("library-tools")
    with world_with(navidrome_factory, tmp, [*PICKED, *OWNED], replay_for_picks()) as w:
        for album in PICKED:  # viewed: picked, shown complete, not filled
            view(w, album_id(w, album.title), f"viewer-{album.title}")
        for album in PICKED:
            wait_for(w, album_id(w, album.title), "deferred")
        yield w


@pytest.fixture
def admin(world: DeliveryWorld) -> Iterator[Browser]:
    browser = Browser(world.server.base_url)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    yield browser
    browser.close()


def seed(
    world: DeliveryWorld,
    album: Album,
    outcome: str = "review",
    *,
    reason: str = NOT_JOINED,
    release: str | None = None,
) -> str:
    """The album's match as an earlier match left it (replacing any it has)."""
    ident = album_id(world, album.title)

    async def write() -> None:
        await world.services.store.execute(
            "INSERT OR REPLACE INTO album_matches (album_id, scope, outcome, reason,"
            " release_ref, candidates, checked_at, title, artist, owned_songs)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            [ident, SCOPE, outcome, reason, release, release or "", time.time(), album.title,
             album.artist],
        )  # fmt: skip

    world.server.call(write)
    return ident


def forget(world: DeliveryWorld, *albums: Album) -> None:
    async def delete() -> None:
        for album in albums:
            await world.services.store.execute(
                "DELETE FROM album_matches WHERE album_id = ?", [album_id(world, album.title)]
            )

    world.server.call(delete)


def outcome(world: DeliveryWorld, ident: str) -> str | None:
    found = row(world, ident)
    return found["outcome"] if found else None


def section(page: str, anchor: str) -> str:
    return page.split(f'id="{anchor}">', 1)[1].split("</section>", 1)[0]


def titles(page: str, anchor: str) -> list[str]:
    return re.findall(r'<span class="cell-title">([^<]+)</span>', section(page, anchor))


def selection(admin: Browser, name: str, q: str = "") -> str:
    """The token of the list as the page shows it now ("": the list has no such form)."""
    page = admin.get("library", params={"q": q} if q else None).text
    found = re.search(rf'id="bulk-{name}".*?name="selection" value="([^"]+)"', page, re.DOTALL)
    return found.group(1) if found else ""


def bulk(admin: Browser, name: str, action: str, *idents: str, **fields: str) -> Any:
    """The list's form as the page shows it now (or with the ``selection`` given), sent."""
    fields.setdefault("selection", selection(admin, name, fields.get("q", "")))
    sent = {"csrf": admin.csrf(), "list": name, "page": "1", "action": action, **fields}
    sent.update({f"album:{ident}": "1" for ident in idents})
    return admin.post("library/bulk", sent)


def change(world: DeliveryWorld, ident: str, assignment: str, *values: Any) -> None:
    """The album's match changed behind the page (as a match or another session does)."""

    async def write() -> None:
        await world.services.store.execute(
            f"UPDATE album_matches SET {assignment} WHERE album_id = ?",  # noqa: S608
            [*values, ident],
        )

    world.server.call(write)


def finished(world: DeliveryWorld, seconds: float = 120) -> library.Batch:
    jobs = world.app.dashboard.jobs
    deadline = time.monotonic() + seconds
    while jobs.batch is None or jobs.batch.finished is None:
        assert time.monotonic() < deadline, "the action for several albums did not finish"
        time.sleep(0.1)
    return jobs.batch


# --- the search ------------------------------------------------------------------------


def test_a_search_narrows_every_list(world: DeliveryWorld, admin: Browser) -> None:
    for album in (FOUND_ONE, FOUND_TWO, OTHER):
        seed(world, album)
    seed(world, NEGATIVE_ONE, "none", reason="no release has every owned track (2 looked at)")
    try:
        page = admin.get("library?q=signal").text
        assert sorted(titles(page, "review")) == sorted(
            [FOUND_ONE.title, FOUND_TWO.title, OTHER.title]
        )
        assert "3 albums found" in section(page, "review")
        # Every word, in the title or the artist, whatever its case; a word is found inside
        # a word, never across two.
        page = admin.get("library?q=TANAGER+sig").text
        assert sorted(titles(page, "review")) == sorted([FOUND_ONE.title, FOUND_TWO.title])
        assert titles(admin.get("library?q=milo+(live)").text, "review") == [FOUND_ONE.title]
        assert titles(admin.get("library?q=signalf").text, "review") == []
        assert titles(admin.get("library?q=nightsignals").text, "review") == []
        # The other lists are narrowed by the same words.
        page = admin.get("library?q=salt+flats").text
        assert titles(page, "no-match") == [NEGATIVE_ONE.title]
        assert "No album in review matches “salt flats”" in section(page, "review")
        page = admin.get("library?q=wolves").text
        assert sorted(titles(page, "picked")) == sorted([WOLVES.title, IRON.title])
        assert "2 albums found" in section(page, "picked")
        # The search is kept by the page's forms, and shown in its box.
        page = admin.get("library?q=milo+%22signal%22").text
        assert 'name="q" value="milo &#34;signal&#34;"' in page
        assert page.count('<input type="hidden" name="q" value="milo &#34;signal&#34;">') >= 3
        assert f'href="{PATH}/library#albums"' in page  # "Show all"
    finally:
        forget(world, FOUND_ONE, FOUND_TWO, OTHER, NEGATIVE_ONE)


def test_a_search_ignores_accents_and_punctuation(world: DeliveryWorld, admin: Browser) -> None:
    seed(world, LEFT)
    try:
        for words in ("asa glass", "ÅSA harbour", "lindqvist, glass!"):
            assert titles(admin.get("library", params={"q": words}).text, "review") == [LEFT.title]
        # Punctuation alone is no search: the whole list.
        assert LEFT.title in titles(admin.get("library", params={"q": "!!"}).text, "review")
    finally:
        forget(world, LEFT)


def test_a_searched_list_is_paged_with_its_search(world: DeliveryWorld, admin: Browser) -> None:
    for album in PAGED:
        seed(world, album)
    seed(world, OTHER)
    try:
        page = admin.get("library?q=field+notes").text
        assert titles(page, "review") == [album.title for album in PAGED[:25]]
        assert "Albums 1\u201325 of 27" in section(page, "review")
        assert 'href="?q=field+notes&amp;page=2#review"' in page
        second = admin.get("library?q=field+notes&page=2").text
        assert titles(second, "review") == [album.title for album in PAGED[25:]]
        assert 'href="?q=field+notes&amp;page=1#review"' in second
        # A page past the end of what is found shows its last page.
        assert titles(admin.get("library?q=field+notes&page=99").text, "review") == [
            album.title for album in PAGED[25:]
        ]
    finally:
        forget(world, OTHER, *PAGED)


def test_the_lists_alone_use_up_no_notice(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = seed(world, LEFT)
    counted: list[str] = []
    counts = library.counts

    async def counting(*args: Any, **kwargs: Any) -> Any:
        counted.append("counts")
        return await counts(*args, **kwargs)

    monkeypatch.setattr(library, "counts", counting)
    try:
        kept = admin.post(
            f"library/review/{ident}",
            {"csrf": admin.csrf(), "page": "1", "q": "glass", "action": "keep"},
        )
        # The action returns to the list as it was searched.
        assert kept.headers["location"] == f"{PATH}/library?q=glass&page=1#review"
        lists = admin.get("library?q=glass&part=lists")
        assert lists.status_code == 200 and lists.text.lstrip().startswith('<div id="albums"')
        assert "<html" not in lists.text and "Fill policy" not in lists.text
        assert "script-src 'self'" in lists.headers["content-security-policy"]
        # The lists alone (each word of a search as it is typed) read no counts they do
        # not show; the page does.
        assert counted == []
        # The notice of the action is still there for the page itself.
        assert "is kept as it is" in admin.get("library").text
        assert "is kept as it is" not in admin.get("library").text
        assert counted
        # The page with its lists loads the script that fetches them (its own, no other).
        page = admin.get("library?q=glass")
        assert page.text.count("<script") == 1
        assert f'<script src="{PATH}/static/library.js" defer></script>' in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert admin.get("static/library.js").status_code == 200
    finally:
        forget(world, LEFT)


# --- actions for several albums ------------------------------------------------------------


def test_the_ticked_albums_are_kept_one_after_another(world: DeliveryWorld, admin: Browser) -> None:
    one, two, left = (seed(world, album) for album in (KEPT_ONE, KEPT_TWO, LEFT))
    seed(world, FOUND_ONE)
    try:
        page = admin.get("library").text
        review = section(page, "review")
        assert 'id="bulk-review"' in review and 'name="all" value="1"' in review
        assert f'form="bulk-review" name="album:{one}" value="1"' in review
        # The form names the list by a token, never by its albums.
        assert re.search(r'name="selection" value="[\w-]{20,}"', review)
        assert review.split("<table", 1)[0].count(one) == 0
        for action in library.BULK["review"]:
            assert f'name="action" value="{action}"' in review.split("<table", 1)[0]
        answer = bulk(admin, "review", "keep", one, two)
        assert answer.headers["location"] == f"{PATH}/library?page=1#review"
        batch = finished(world)
        assert (batch.total, batch.ok, batch.failures) == (2, 2, 0)
        assert [outcome(world, i) for i in (one, two, left)] == ["kept", "kept", "review"]
        assert "2 of 2 albums kept as they are." in admin.get("library").text
        # An ID that is not in the list is not acted on.
        answer = bulk(admin, "review", "keep", "no-such-album")
        assert "Select the albums first." in admin.get("library").text
        assert outcome(world, left) == "review"
    finally:
        forget(world, KEPT_ONE, KEPT_TWO, LEFT, FOUND_ONE)


def test_all_of_a_searched_list(world: DeliveryWorld, admin: Browser) -> None:
    one, two, other = (seed(world, album) for album in (FOUND_ONE, FOUND_TWO, OTHER))
    try:
        answer = bulk(admin, "review", "keep", q="tanager", all="1")
        assert answer.headers["location"] == f"{PATH}/library?q=tanager&page=1#review"
        batch = finished(world)
        assert sorted(batch.albums) == sorted([one, two])
        assert [outcome(world, i) for i in (one, two, other)] == ["kept", "kept", "review"]
    finally:
        forget(world, FOUND_ONE, FOUND_TWO, OTHER)


def test_all_means_the_albums_the_page_showed(world: DeliveryWorld, admin: Browser) -> None:
    one, two = seed(world, FOUND_ONE), seed(world, FOUND_TWO)
    try:
        shown = selection(admin, "review", "signal")  # the page as the owner sees it
        other = seed(world, OTHER)  # another album the search finds joins the list
        bulk(admin, "review", "keep", q="signal", all="1", selection=shown)
        batch = finished(world)
        assert sorted(batch.albums) == sorted([one, two])
        assert [outcome(world, i) for i in (one, two, other)] == ["kept", "kept", "review"]
    finally:
        forget(world, FOUND_ONE, FOUND_TWO, OTHER)


def test_a_match_changed_since_the_page_was_loaded_is_left_alone(
    world: DeliveryWorld, admin: Browser
) -> None:
    one, two, three, four = (seed(world, a) for a in (KEPT_ONE, KEPT_TWO, LEFT, UNCHOSEN))
    try:
        shown = selection(admin, "review")
        # Behind the page: another reason, another list, a release where there was none.
        change(world, two, "reason = ?", "several plausible editions")
        change(world, three, "outcome = 'none'")
        change(world, four, "release_ref = ?, candidates = ?", "demo:900000117", "demo:900000117")
        bulk(admin, "review", "keep", selection=shown, all="1")
        batch = finished(world)
        assert (batch.total, batch.ok, batch.changed, batch.failures) == (4, 1, 3, 0)
        assert [outcome(world, i) for i in (one, two, three, four)] == [
            "kept", "review", "none", "review"]  # fmt: skip
        page = admin.get("library").text
        assert "1 of 4 albums kept as they are. 3 have changed since the page was loaded" in page
        assert "and were left as they are." in page
        # Shown again as they are now, they can be acted on.
        bulk(admin, "review", "keep", two, four)
        assert finished(world).ok == 2
    finally:
        forget(world, KEPT_ONE, KEPT_TWO, LEFT, UNCHOSEN)


def test_a_pick_changed_since_the_page_was_loaded_is_not_filled(
    world: DeliveryWorld, admin: Browser
) -> None:
    wolves = album_id(world, WOLVES.title)
    before = row(world, wolves) or {}
    assert (before["outcome"], before["release_ref"]) == ("deferred", "demo:900000182")
    fills = world.services.fills
    assert fills is not None
    count = fills.fills
    shown = selection(admin, "picked")
    try:
        # The pick is another release now than the one the page offers to fill from.
        change(world, wolves, "release_ref = ?", "demo:900009182")
        bulk(admin, "picked", "fill", wolves, selection=shown)
        batch = finished(world)
        assert (batch.ok, batch.changed, batch.failures) == (0, 1, 0), batch.failed
        assert fills.fills == count and len(songs(world, wolves)) == 1
        assert (row(world, wolves) or {})["outcome"] == "deferred"
    finally:
        change(world, wolves, "release_ref = ?", "demo:900000182")


def test_a_token_stands_for_the_matches_the_page_showed(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A match that changes while the page is made - right after its lists were read - is
    not on the page, and so not in what its token stands for: the rows shown and the
    selection come from one read of the database."""
    one, two = seed(world, KEPT_ONE), seed(world, KEPT_TWO)
    store = world.app.dashboard.store
    assert store is not None
    snapshot = store.snapshot
    reads: list[str] = []

    class Watched:
        """The read's connection: the match changes right after its lists are read, before
        it reads anything else."""

        def __init__(self, conn: Any) -> None:
            self.conn = conn

        def __getattr__(self, name: str) -> Any:
            return getattr(self.conn, name)

        @asynccontextmanager
        async def execute(self, sql: str, *args: Any) -> AsyncIterator[Any]:
            async with self.conn.execute(sql, *args) as cursor:
                found = await cursor.fetchall()
                if "FROM album_matches" in sql and "ORDER BY artist" in sql:
                    if not reads:  # (behind the page's back)
                        await store.execute(
                            "UPDATE album_matches SET reason = ? WHERE album_id = ?",
                            ["the release is in the library as another album", one],
                        )
                    reads.append(sql)
                yield Rows(found)

    class Rows:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        async def fetchall(self) -> list[Any]:
            return self.rows

        async def fetchone(self) -> Any:
            return self.rows[0] if self.rows else None

    @asynccontextmanager
    async def watched() -> AsyncIterator[Any]:
        async with snapshot() as conn:
            yield Watched(conn)

    try:
        monkeypatch.setattr(store, "snapshot", watched)
        page = admin.get("library").text
        monkeypatch.undo()
        assert len(reads) == 1  # the lists are read once, all of them
        review = section(page, "review")
        assert review.count("The fill did not join the album") == 2
        assert "Already in the library" not in review
        found = re.search(r'id="bulk-review".*?name="selection" value="([^"]+)"', page, re.DOTALL)
        assert found
        bulk(admin, "review", "keep", selection=found.group(1), all="1")
        batch = finished(world)
        # The album whose match is not the one shown is left; the other is kept.
        assert (batch.total, batch.ok, batch.changed) == (2, 1, 1)
        assert [outcome(world, i) for i in (one, two)] == ["review", "kept"]
    finally:
        monkeypatch.undo()
        forget(world, KEPT_ONE, KEPT_TWO)


def test_a_tokens_life_ends_half_an_hour_after_it_was_issued(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Showing the same list again - another tab, the page's own refresh - does not renew
    a token: the list keeps it for the first half of its life, then gets a new one, and
    the old one ends when its own half hour is over."""
    one, two = seed(world, KEPT_ONE), seed(world, KEPT_TWO)
    selections = world.app.dashboard.jobs.selections
    start = time.time()
    minutes = [0.0]
    monkeypatch.setattr(selections, "clock", lambda: start + minutes[0] * 60)
    try:
        first = selection(admin, "review")
        minutes[0] = 10
        assert selection(admin, "review") == first
        minutes[0] = 20
        second = selection(admin, "review")
        assert second and second != first  # (a page just rendered: one with life left)
        minutes[0] = 31
        assert selection(admin, "review") == second
        bulk(admin, "review", "keep", one, two, selection=first)
        assert "loaded too long ago to act on its lists" in admin.get("library").text
        assert [outcome(world, i) for i in (one, two)] == ["review", "review"]
        minutes[0] = 49
        bulk(admin, "review", "keep", one, two, selection=second)
        assert finished(world).ok == 2
    finally:
        monkeypatch.undo()
        forget(world, KEPT_ONE, KEPT_TWO)


def test_a_selection_starts_one_action_only(world: DeliveryWorld, admin: Browser) -> None:
    one, two, three = (seed(world, album) for album in (KEPT_ONE, KEPT_TWO, LEFT))
    try:
        shown = selection(admin, "review")
        # The page's own refresh shows the same list: the form on screen stays good.
        assert selection(admin, "review") == shown
        assert admin.get("library?part=lists").text.count(f'value="{shown}"') == 1
        bulk(admin, "review", "keep", one, selection=shown)
        first = finished(world)
        assert first.albums == [one] and outcome(world, one) == "kept"
        # Sent again (a second press, the browser's "back"): nothing is started.
        bulk(admin, "review", "keep", two, three, selection=shown)
        assert "That selection was used already" in admin.get("library").text
        assert world.app.dashboard.jobs.batch is first
        assert [outcome(world, i) for i in (two, three)] == ["review", "review"]
        assert selection(admin, "review") != shown  # the page has a new one
    finally:
        forget(world, KEPT_ONE, KEPT_TWO, LEFT)


def test_a_selection_is_its_viewers_own_and_short_lived(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = seed(world, KEPT_ONE), seed(world, KEPT_TWO)
    jobs = world.app.dashboard.jobs
    before = jobs.batch
    other = Browser(world.server.base_url)
    try:
        shown = selection(admin, "review")
        assert shown and len(shown) >= 20
        # Another session - also the same account's - cannot use it.
        assert other.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        bulk(other, "review", "keep", one, two, selection=shown)
        assert "loaded too long ago to act on its lists" in other.get("library").text
        # Nor is it good for another list, or without a token at all.
        bulk(admin, "picked", "keep", one, two, selection=shown)
        assert "loaded too long ago to act on its lists" in admin.get("library").text
        bulk(admin, "review", "keep", one, two, selection="")
        assert "loaded too long ago to act on its lists" in admin.get("library").text
        # After half an hour the page is loaded anew.
        monkeypatch.setattr(jobs.selections, "clock", lambda: time.time() + 1801)
        bulk(admin, "review", "keep", one, two, selection=shown)
        assert "loaded too long ago to act on its lists" in admin.get("library").text
        monkeypatch.undo()
        assert jobs.batch is before
        assert [outcome(world, i) for i in (one, two)] == ["review", "review"]
        # Many pages shown to one viewer: only the newest are kept.
        for number in range(library.SELECTIONS_KEPT + 5):
            jobs.selections.issue("someone", "review", {f"album-{number}": library.Shown("s")})
        kept = [s for s in jobs.selections._kept.values() if s.viewer == "someone"]
        assert len(kept) == library.SELECTIONS_KEPT
    finally:
        other.close()
        forget(world, KEPT_ONE, KEPT_TWO)


def test_picked_albums_are_filled_from_their_picks(world: DeliveryWorld, admin: Browser) -> None:
    salt, stones = album_id(world, SALT.title), album_id(world, STONES.title)
    picked = section(admin.get("library").text, "picked")
    assert "Fill from the picked release" in picked
    assert f'form="bulk-picked" name="album:{salt}"' in picked
    fills = world.services.fills
    assert fills is not None
    before = fills.fills
    bulk(admin, "picked", "fill", salt, stones)
    batch = finished(world)
    assert (batch.ok, batch.failures, batch.unchosen) == (2, 0, 0), batch.failed
    assert fills.fills == before + 2
    for ident, release in ((salt, "demo:900000203"), (stones, "demo:900000133")):
        found = row(world, ident) or {}
        assert (found["outcome"], found["release_ref"]) == ("filled", release)
        assert len(songs(world, ident)) == found["release_tracks"]
    page = admin.get("library").text
    assert "2 of 2 albums filled." in page
    # Filled from the release the owner took: no longer a pick to decide.
    assert f'name="album:{salt}"' not in page


def test_a_review_without_a_most_likely_release_is_not_filled(
    world: DeliveryWorld, admin: Browser
) -> None:
    unchosen = seed(world, UNCHOSEN, reason="several plausible editions")
    part = seed(
        world,
        LEFT,
        reason="part of Ines Calder - Low Winter Sun (album x1): filled there",
        release="demo:900000117",
    )
    try:
        fills = world.services.fills
        assert fills is not None
        before = fills.fills
        bulk(admin, "review", "fill", unchosen, part)
        batch = finished(world)
        assert (batch.done, batch.ok, batch.unchosen, batch.failures) == (2, 0, 2, 0)
        assert fills.fills == before
        assert [outcome(world, i) for i in (unchosen, part)] == ["review", "review"]
        page = admin.get("library").text
        assert "0 of 2 albums filled. 2 have no most likely release: choose one for each." in page
    finally:
        forget(world, UNCHOSEN, LEFT)


def test_a_refused_fill_is_said_with_its_album(world: DeliveryWorld, admin: Browser) -> None:
    # The owned song is not on the release its row names: the fill refuses it.
    one, two = seed(world, KEPT_ONE, release="demo:900000117"), seed(world, KEPT_TWO)
    try:
        bulk(admin, "review", "fill", one, two)
        batch = finished(world)
        assert (batch.ok, batch.failures, batch.unchosen) == (0, 1, 1)
        assert outcome(world, one) == "review"
        page = admin.get("library").text
        assert "1 could not be done." in page and "What went wrong" in page
        assert "Paper Lanterns was not filled: an owned track is not on this release." in page
    finally:
        forget(world, KEPT_ONE, KEPT_TWO)


def test_an_action_shows_how_far_it_is_and_can_be_stopped(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    idents = [seed(world, album) for album in (STOP_ONE, STOP_TWO, STOP_THREE)]
    fills = world.services.fills
    assert fills is not None
    keep = fills.keep

    async def events() -> tuple[anyio.Event, anyio.Event]:
        return anyio.Event(), anyio.Event()

    entered, release = world.server.call(events)

    async def slow(album_id: str, **how: Any) -> None:
        entered.set()
        await release.wait()
        await keep(album_id, **how)

    monkeypatch.setattr(fills, "keep", slow)
    try:
        bulk(admin, "review", "keep", *idents)
        world.server.call(entered.wait)
        page = admin.get("library").text
        assert "Keeping 3 albums" in page and "0 of 3 done. Now: Corin Vale" in page
        assert f'action="{PATH}/library/bulk/stop"' in page
        assert "data-running" in admin.get("library?part=lists").text
        # One at a time: another is refused while this one runs.
        bulk(admin, "review", "keep", idents[2])
        assert "Another action for several albums is running" in admin.get("library").text
        stopped = admin.post("library/bulk/stop", {"csrf": admin.csrf()})
        assert stopped.headers["location"] == f"{PATH}/library?page=1#albums"
        assert "It stops after this album." in admin.get("library").text
        world.server.call(lambda: _set(release))
        batch = finished(world)
        # The album under way was finished; the others are as they were.
        assert (batch.done, batch.ok, batch.stopped) == (1, 1, "you stopped it")
        assert [outcome(world, i) for i in idents] == ["kept", "review", "review"]
        page = admin.get("library").text
        assert "1 of 3 albums kept as they are. Stopped before the other 2: you stopped it." in page
        assert "data-running" not in page
        # Started again, it goes on with what is left: the album done is no longer listed.
        monkeypatch.setattr(fills, "keep", keep)
        bulk(admin, "review", "keep", *idents)
        batch = finished(world)
        assert (sorted(batch.albums), batch.ok) == (sorted(idents[1:]), 2)
        assert [outcome(world, i) for i in idents] == ["kept", "kept", "kept"]
    finally:
        world.server.call(lambda: _set(release))
        forget(world, STOP_ONE, STOP_TWO, STOP_THREE)


async def _set(event: anyio.Event) -> None:
    event.set()


def test_albums_without_a_match_are_matched_again(world: DeliveryWorld, admin: Browser) -> None:
    why = "no release has every owned track (2 looked at)"
    one, two = (seed(world, album, "none", reason=why) for album in (NEGATIVE_ONE, NEGATIVE_TWO))
    try:
        listed = section(admin.get("library").text, "no-match")
        assert 'id="bulk-none"' in listed and 'name="action" value="recheck"' in listed
        assert 'value="keep"' not in listed and 'value="fill"' not in listed
        before = len(list(world.nd.music.rglob("*.flac")))
        bulk(admin, "none", "recheck", all="1", q="rhea")
        batch = finished(world)
        # Matched again without a match, and without a file added: as before.
        assert (batch.done, batch.same, batch.failures) == (2, 2, 0), batch.failed
        assert [outcome(world, i) for i in (one, two)] == ["none", "none"]
        assert len(list(world.nd.music.rglob("*.flac"))) == before
        assert (
            "2 albums matched again: 0 now match a release, 2 stay as before."
            in admin.get("library").text
        )
        # An action of another list is not offered here.
        assert bulk(admin, "none", "keep", one).status_code == 303
        assert "Choose one of the actions for the selected albums." in admin.get("library").text
        assert outcome(world, one) == "none"
    finally:
        forget(world, NEGATIVE_ONE, NEGATIVE_TWO)


def test_matching_several_again_waits_for_the_catalogs_rest(
    world: DeliveryWorld, admin: Browser
) -> None:
    one = seed(world, NEGATIVE_ONE, "none", reason="no release has every owned track")
    two = seed(world, NEGATIVE_TWO, "none", reason="no release has every owned track")
    fills = world.services.fills
    assert fills is not None
    fills.resting_until = fills.clock() + 600
    try:
        bulk(admin, "none", "recheck", one, two)
        assert "Nothing is matched again now: the catalog is resting" in admin.get("library").text
        batch = world.app.dashboard.jobs.batch
        assert batch is None or batch.finished is not None  # none was started
    finally:
        fills.resting_until = 0.0
        forget(world, NEGATIVE_ONE, NEGATIVE_TWO)


def test_actions_for_several_albums_need_the_session_token(
    world: DeliveryWorld, admin: Browser
) -> None:
    ident = seed(world, OTHER)
    try:
        sent = {"list": "review", "action": "keep", f"album:{ident}": "1"}
        assert admin.post("library/bulk", {**sent, "csrf": "x"}).status_code == 403
        assert admin.post("library/bulk/stop", {"csrf": "x"}).status_code == 403
        other = Browser(world.server.base_url)
        anonymous = other.post("library/bulk", sent)
        assert anonymous.status_code == 303 and f"{PATH}/sign-in" in anonymous.headers["location"]
        other.close()
        assert admin.post("library/bulk", {"csrf": admin.csrf(), "list": "x"}).status_code == 400
        assert outcome(world, ident) == "review"
    finally:
        forget(world, OTHER)
