"""Suite I (continued) - automatic picks, against a real Navidrome with the demo catalog's
records replayed (copies of its releases under other IDs make several plausible editions).

An album the matcher would send to review for several plausible editions, or for a release
numbered differently from the owned files, is matched with the most likely release
instead: shown complete at once, filled on first use (the policy here fills nothing
automatically), the release's numbering kept. With the setting off it goes to review as
before; a review row for those reasons is matched again by a view or by the library pass
once the setting is on, and its album's name and counts are written as for a new match. An
album no release completes (here: its songs only on a release with another title) keeps
its owned songs only, and search results and artist pages show the catalog's editions of
it beside it; matched albums hide theirs as before. The Library page lists the picks, with
the review list's actions while they are not filled. A part of another owned album follows
the album its release is picked for.
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from shijhon.fill.fills import ChoiceRefused
from shijhon.matching.matcher import NUMBERED_DIFFERENTLY, OTHER_TITLES, SEVERAL
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay

ESME = "Esme Ashdown"
# Two songs of "CRIMSON.", which the catalog has twice here (the same explicit edition).
EDITIONS = Album(
    ESME,
    "CRIMSON.",
    (Track("NORTHERN.", 1, seconds=118), Track("AMBER.", 2, seconds=187)),
    recording_date="2010",
)
# Two songs of "PALE RIVERS.", numbered as "CRIMSON." numbers them: the release has them as
# 14 and 13, and its own 1 and 2 (COPPER., PAPER.) to add.
RENUMBERED = Album(
    ESME,
    "PALE RIVERS.",
    (Track("NORTHERN.", 1, seconds=118), Track("AMBER.", 2, seconds=187)),
    recording_date="2010",
)
# One song, found by its ISRC on "CRIMSON." only: the catalog's "Golden Hours" lacks it.
GOLDEN = Album(
    ESME,
    "Golden Hours",
    (Track("NORTHERN.", 1, seconds=118, isrc="ZZSHJ0000073"),),
    recording_date="2010",
)
# One song each of albums the catalog has twice here.
SALT = Album("BrokenMeadow", "Salt 2", (Track("Burning Engines", 1, seconds=301),),
             recording_date="1984")  # fmt: skip
STONES = Album("Jonah Fairbanks", "wild stones", (Track("FROZEN", 1, seconds=194),),
               recording_date="2010")  # fmt: skip
WOLVES = Album(
    "Uma Okafor",
    "Bright Wolves 2 (Original Motion Picture Soundtrack)",
    (Track("Northern Machines", 11, seconds=84),),
    recording_date="2012",
)
IRON = Album("Oren Garrow", "Iron Wolves", (Track("Distant Towers", 1, seconds=274),),
             recording_date="2006")  # fmt: skip
# One song tagged "Northern Letters", five "Northern Letters (Remastered)": one release.
PART = Album(
    "Neve Ashdown",
    "Northern Letters",
    (Track("Copper Station", 0, seconds=243),),
    recording_date="1962",
)
WHOLE = Album(
    "Neve Ashdown",
    "Northern Letters (Remastered)",
    (
        Track("Crimson Mirrors", 1, seconds=259),
        Track("Open", 2, seconds=181),
        Track("Faded Wolves", 3, seconds=208),
        Track("Open Mirrors", 4, seconds=207),
        Track("Pale Islands", 0, seconds=170),
    ),
    recording_date="1962",
)
# The catalog's records copied under these IDs (900009xxx: unused by the demo records).
COPIES = {"900000117": "900009117", "900000203": "900009203", "900000133": "900009133",
          "900000182": "900009182", "900000296": "900009296", "900000021": "900009021"}  # fmt: skip
GOLDEN_ID = "900009300"
NEVER = {"auto_min_songs": 100, "auto_min_share": 1.0}  # nothing is filled automatically


def _copy(replay: Replay, source: str, ident: str) -> dict[str, Any]:
    """An album record copied under another ID (its tracks too), the same otherwise."""
    record = copy.deepcopy(replay.by_path[f"albums/{source}"])
    body = record["body"]
    body["ref"]["id"] = ident
    body["upc"] = f"99{ident}"
    for n, track in enumerate(body["tracks"], 1):
        track["ref"]["id"] = f"{ident}{n:02d}"
        track["album"] = {"catalog": "demo", "id": ident}
    record.update(name=f"album-{ident}", path=f"albums/{ident}")
    replay.by_path[record["path"]] = record
    return record


def _search(replay: Replay, term: str, *ids: str, artists: tuple[dict[str, Any], ...] = ()) -> None:
    """A recorded search for ``term`` that finds these albums (and artists)."""
    albums = []
    for ident in ids:
        body = {k: v for k, v in replay.by_path[f"albums/{ident}"]["body"].items() if k != "tracks"}
        albums.append(body)
    replay.searches[term] = {
        "name": f"search-{term}",
        "path": "search",
        "status": 200,
        "params": {"term": term},
        "body": {"artists": list(artists), "albums": albums, "songs": []},
    }


def replay_for_picks() -> Replay:
    replay = Replay()
    for source, ident in COPIES.items():
        _copy(replay, source, ident)
    golden = _copy(replay, "900000117", GOLDEN_ID)["body"]  # without NORTHERN.
    golden["title"] = "Golden Hours"
    for n, track in enumerate(golden["tracks"], 1):
        track["isrc"] = f"ZZSHJ93{n:05d}"
        track["album_title"] = "Golden Hours"
    golden["tracks"][0]["title"] = "MORNING."
    esme = {"ref": {"catalog": "demo", "id": "900000086"}, "name": ESME}
    _search(replay, "esme ashdown crimson.", "900000117", "900009117")
    replay.aliases["esme ashdown pale rivers."] = "bright canyon"
    _search(replay, "esme ashdown golden hours", GOLDEN_ID)
    _search(replay, "golden hours", GOLDEN_ID)
    _search(replay, "esme ashdown", "900000117", "900000101", GOLDEN_ID, artists=(esme,))
    releases = [
        {k: v for k, v in replay.by_path[f"albums/{i}"]["body"].items() if k != "tracks"}
        for i in ("900000117", "900009117", "900000084", "900000101", GOLDEN_ID)
    ]
    path = "artists/900000086/releases"
    replay.by_path[path] = {"name": "releases-esme", "path": path, "status": 200, "body": releases}
    for term in ("brokenmeadow salt 2", "salt 2"):
        _search(replay, term, "900000203", "900009203")
    _search(replay, "jonah fairbanks wild stones", "900000133", "900009133")
    _search(replay, f"uma okafor {WOLVES.title.lower()}", "900000182", "900009182")
    _search(replay, "oren garrow iron wolves", "900000296", "900009296")
    for title in ("northern letters", "northern letters (remastered)"):
        _search(replay, f"neve ashdown {title}", "900000021", "900009021")
    return replay


@pytest.fixture(scope="module")
def replay() -> Replay:
    return replay_for_picks()


@contextmanager
def world_with(
    factory: NavidromeFactory, tmp: Path, albums: list[Album], replay: Replay
) -> Iterator[DeliveryWorld]:
    nd = factory()
    for album in albums:
        write_album(nd.music, album)
    nd.scan(full=True)
    fill = {"enabled": True, "open_budget_seconds": 15, "background_pause_seconds": 0,
            "library_pass": "off", "pass_requests_per_second": 50, **NEVER}  # fmt: skip
    with delivery_world(nd, tmp, catalog=replay.catalog(), fill=fill) as world:
        world.app.dashboard.attempts.limit = 10_000
        prepare_collection(nd, world.server)
        yield world


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    albums = [EDITIONS, RENUMBERED, GOLDEN, SALT, STONES, WOLVES, IRON]
    with world_with(navidrome_factory, tmp_path_factory.mktemp("picks"), albums, replay) as w:
        yield w


def album_id(world: DeliveryWorld, title: str) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == title]
    return str(ident)


def row(world: DeliveryWorld, ident: str) -> dict[str, Any] | None:
    async def read() -> dict[str, Any] | None:
        found = await world.services.store.fetchone(
            "SELECT outcome, release_ref, reason, candidates, planned, picked, title, artist,"
            " owned_songs, release_tracks FROM album_matches WHERE album_id = ?",
            [ident],
        )
        return dict(found) if found else None

    return world.server.call(read)


def wait_for(world: DeliveryWorld, ident: str, outcome: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = row(world, ident)
        if found is not None and found["outcome"] == outcome:
            return found
        time.sleep(0.1)
    raise AssertionError(f"{ident}: {row(world, ident)}")


def songs(world: DeliveryWorld, ident: str) -> list[dict[str, Any]]:
    return list(world.nd.client().ok("getAlbum", {"id": ident})["album"]["song"])


def view(world: DeliveryWorld, ident: str, client: str = "viewer") -> dict[str, Any]:
    viewer = world.client(client=client)
    return dict(viewer.ok("getAlbum", {"id": ident})["album"])


def review_row(world: DeliveryWorld, album: Album, reason: str) -> str:
    """A review row as any earlier match may have left it - without the album's name and
    counts, as some rows have them."""
    ident = album_id(world, album.title)

    async def write() -> None:
        await world.services.store.execute(
            "INSERT INTO album_matches (album_id, scope, outcome, reason, candidates,"
            " checked_at) VALUES (?, 'demo.xx', 'review', ?, '', ?)",
            [ident, reason, time.time()],
        )

    world.server.call(write)
    return ident


def test_several_editions_are_settled_by_a_pick(world: DeliveryWorld, replay: Replay) -> None:
    ident = album_id(world, EDITIONS.title)
    with collected("shijhon.fill.fills") as lines:
        album = view(world, ident)
    # Complete at once with the first edition found, nothing written.
    assert album["songCount"] == 14 and len(album["song"]) == 14
    assert len(songs(world, ident)) == 2
    found = wait_for(world, ident, "deferred")
    assert found["picked"] == SEVERAL and found["release_ref"] == "demo:900000117"
    assert sorted(found["candidates"].split(",")) == ["demo:900000117", "demo:900009117"]
    assert any(
        line.startswith(f"album matched by an automatic pick: {ESME} - CRIMSON. (2 owned):"
                        " demo:900000117, 1 other release(s) plausible")
        for line in lines
    )  # fmt: skip
    # Filled on first use, like any match.
    track = next(s for s in album["song"] if s["id"].startswith("sh.tr."))
    before = replay.api_requests
    world.client(client="viewer").ok("star", {"id": track["id"]})
    assert len(songs(world, ident)) == 14 and replay.api_requests - before <= 1
    filled = row(world, ident) or {}
    assert (filled["outcome"], filled["picked"]) == ("filled", SEVERAL)


def test_a_release_numbered_differently_is_picked_and_keeps_its_numbers(
    world: DeliveryWorld,
) -> None:
    ident = album_id(world, RENUMBERED.title)
    owned = {s["id"]: s["track"] for s in songs(world, ident)}
    album = view(world, ident)
    assert album["songCount"] == 14
    found = wait_for(world, ident, "deferred")
    assert (found["picked"], found["release_ref"]) == (NUMBERED_DIFFERENTLY, "demo:900000101")
    numbers = sorted((s["track"], s["title"]) for s in album["song"])
    assert numbers[:4] == [(1, "COPPER."), (1, "NORTHERN."), (2, "AMBER."), (2, "PAPER.")]
    world.client(client="viewer").ok("star", {"albumId": "sh.al.demo.900000101"})  # first use
    filled = songs(world, ident)
    assert len(filled) == 14 and (row(world, ident) or {}).get("outcome") == "filled"
    # The owned files as they were; the added songs with the release's numbers.
    assert {s["id"]: s["track"] for s in filled if s["id"] in owned} == owned
    added = sorted((s["track"], s["title"]) for s in filled if s["id"] not in owned)
    assert added[:2] == [(1, "COPPER."), (2, "PAPER.")] and len(added) == 12


@pytest.mark.parametrize(
    ("auto_pick", "duplicate_additions", "refusal"),
    [
        (True, False, None),
        (False, False, "owned track's position"),
        (True, True, "two tracks to add"),
    ],
)
def test_manual_numbering_follows_the_pick_setting_and_keeps_added_positions_unique(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    auto_pick: bool,
    duplicate_additions: bool,
    refusal: str | None,
) -> None:
    replay = replay_for_picks()
    if duplicate_additions:
        replay.by_path["albums/900000101"]["body"]["tracks"][1]["number"] = 1
    with world_with(navidrome_factory, tmp_path, [RENUMBERED], replay) as world:
        fills = world.services.fills
        assert fills is not None
        fills.auto_pick = auto_pick
        ident = album_id(world, RENUMBERED.title)
        owned = {s["id"]: s["track"] for s in songs(world, ident)}
        if refusal is not None:
            with pytest.raises(ChoiceRefused, match=refusal):
                world.server.call(lambda: fills.choose(ident, "demo:900000101"))
            assert {s["id"]: s["track"] for s in songs(world, ident)} == owned
            assert row(world, ident) is None
            return
        chosen = world.server.call(lambda: fills.choose(ident, "demo:900000101"))
        assert chosen.outcome == "filled" and chosen.shares and not chosen.picked
        filled = songs(world, ident)
        assert len(filled) == 14
        assert {s["id"]: s["track"] for s in filled if s["id"] in owned} == owned
        added = [(s.get("discNumber", 1), s["track"]) for s in filled if s["id"] not in owned]
        assert len(added) == len(set(added)) == 12


def test_keeping_a_pick_shows_catalog_editions_and_a_manual_fill_hides_them_again(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    replay = replay_for_picks()
    with world_with(navidrome_factory, tmp_path, [RENUMBERED], replay) as world:
        fills = world.services.fills
        assert fills is not None
        ident = album_id(world, RENUMBERED.title)
        assert view(world, ident)["songCount"] == 14
        assert len(songs(world, ident)) == 2
        world.server.call(lambda: fills.keep(ident))
        assert view(world, ident)["songCount"] == 2
        artists = world.nd.client().ok("search3", {"query": ESME})["searchResult3"]["artist"]
        [artist] = [a["id"] for a in artists if a["name"] == ESME]
        client = world.client(client="manual-choice")
        catalog_id = "sh.al.demo.900000101"
        for method, params, result in (
            ("search3", {"query": ESME}, "searchResult3"),
            ("getArtist", {"id": artist}, "artist"),
        ):
            ids = [a["id"] for a in client.ok(method, params)[result]["album"]]
            assert ident in ids and catalog_id in ids
        world.server.call(lambda: fills.choose(ident, "demo:900000101"))
        for method, params, result in (
            ("search3", {"query": ESME}, "searchResult3"),
            ("getArtist", {"id": artist}, "artist"),
        ):
            ids = [a["id"] for a in client.ok(method, params)[result]["album"]]
            assert ident in ids and catalog_id not in ids


def test_an_album_no_release_completes_shows_the_catalog_s_editions_beside_it(
    world: DeliveryWorld,
) -> None:
    golden, editions, renumbered = (
        album_id(world, a.title) for a in (GOLDEN, EDITIONS, RENUMBERED)
    )
    album = view(world, golden)
    assert album["songCount"] == 1  # the owned song only
    found = wait_for(world, golden, "review")
    assert found["reason"] == OTHER_TITLES and not found["picked"]
    for ident in (editions, renumbered):  # matched (picked): their editions stay hidden
        view(world, ident, "settler")
        assert (row(world, ident) or {}).get("outcome") in ("deferred", "filled")
    client = world.client(client="searcher")
    # A search: the owned album and the catalog's.
    found_albums = client.ok("search3", {"query": "golden hours"})["searchResult3"]["album"]
    ids = [a["id"] for a in found_albums]
    assert golden in ids and f"sh.al.demo.{GOLDEN_ID}" in ids
    # The artist page: the same, and no edition of the matched albums.
    artists = world.nd.client().ok("search3", {"query": ESME, "albumCount": 0})
    [artist] = [a["id"] for a in artists["searchResult3"]["artist"] if a["name"] == ESME]
    page = client.ok("getArtist", {"id": artist})["artist"]["album"]
    added = [a for a in page if a["id"].startswith("sh.")]
    assert [a["name"] for a in added] == ["Golden Hours"]
    # It plays as a catalog album.
    catalog_album = client.ok("getAlbum", {"id": f"sh.al.demo.{GOLDEN_ID}"})["album"]
    assert catalog_album["songCount"] == 14 and len(songs(world, golden)) == 1


def test_with_the_setting_off_such_an_album_goes_to_review_and_a_view_settles_it_later(
    world: DeliveryWorld,
) -> None:
    fills = world.services.fills
    assert fills is not None
    ident = album_id(world, SALT.title)
    client = world.client(client="salt")
    fills.auto_pick = False
    try:
        assert view(world, ident)["songCount"] == 1  # Navidrome's answer
        found = wait_for(world, ident, "review")
        assert (found["reason"], found["picked"]) == (SEVERAL, "")
        assert view(world, ident)["songCount"] == 1  # not matched again
        # Not matched: the catalog's edition shows beside it.
        listed = client.ok("search3", {"query": "salt 2"})["searchResult3"]["album"]
        assert [a["name"] for a in listed].count("Salt 2") == 2
    finally:
        fills.auto_pick = True
    album = view(world, ident)  # the review is settled now, at once
    assert album["songCount"] == 13
    found = wait_for(world, ident, "deferred")
    assert found["picked"] == SEVERAL
    listed = client.ok("search3", {"query": "salt 2"})["searchResult3"]["album"]
    assert [a["id"] for a in listed if a["name"] == "Salt 2"] == [ident]


def test_an_earlier_review_row_is_settled_when_the_album_is_viewed(world: DeliveryWorld) -> None:
    ident = review_row(world, STONES, SEVERAL)
    album = view(world, ident)
    assert album["songCount"] == 9
    found = wait_for(world, ident, "deferred")
    assert found["picked"] == SEVERAL
    # Written as for a new match.
    assert (found["title"], found["artist"]) == (STONES.title, STONES.artist)
    assert (found["owned_songs"], found["release_tracks"]) == (1, 9)


@pytest.fixture
def admin(world: DeliveryWorld) -> Iterator[Browser]:
    browser = Browser(world.server.base_url)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    yield browser
    browser.close()


def picks_row(page: str, title: str) -> str:
    section = page.split('id="picked"', 1)[1]
    found = [r for r in section.split("<tr>") if f'<span class="cell-title">{title}</span>' in r]
    assert found, title
    return found[0]


def act(admin: Browser, ident: str, **fields: str) -> None:
    sent = {"csrf": admin.csrf(), "page": "1", "list": "picked", **fields}
    answer = admin.post(f"library/review/{ident}", sent)
    assert answer.status_code == 303 and answer.headers["location"].endswith("#picked")


def settle(world: DeliveryWorld, ident: str) -> None:
    deadline = time.monotonic() + 90
    while world.app.dashboard.jobs.running(ident) is not None:
        assert time.monotonic() < deadline, "the background action did not finish"
        time.sleep(0.2)


def test_the_library_page_lists_the_picks_with_the_review_actions(
    world: DeliveryWorld, admin: Browser
) -> None:
    wolves, iron = album_id(world, WOLVES.title), album_id(world, IRON.title)
    for ident in (wolves, iron):
        view(world, ident, "lister")
        wait_for(world, ident, "deferred")
    page = admin.get("library").text
    assert '<h2 id="picked-title">Picked automatically</h2>' in page
    listed = picks_row(page, WOLVES.title)
    assert "Several releases fit the owned songs; this is the most likely." in listed
    assert "Shown complete; filled when used." in listed
    options = re.findall(r'<option value="(demo:\d+)">', listed)
    assert options == ["demo:900000182", "demo:900009182"]
    for action in ("fill", "keep", "rematch"):
        assert f'name="action" value="{action}"' in listed
    # Match again: picked again.
    act(admin, wolves, action="rematch")
    settle(world, wolves)
    assert (row(world, wolves) or {}).get("picked") == SEVERAL
    # Fill from the other edition: filled from it, no longer a pick.
    act(admin, wolves, action="fill", release="demo:900009182")
    settle(world, wolves)
    found = row(world, wolves) or {}
    assert (found["outcome"], found["release_ref"], found["picked"]) == (
        "filled", "demo:900009182", "")  # fmt: skip
    assert len(songs(world, wolves)) == 19
    # Keep as it is: kept, no longer a pick.
    act(admin, iron, action="keep")
    found = row(world, iron) or {}
    assert (found["outcome"], found["picked"]) == ("kept", "")
    page = admin.get("library").text
    assert f">{WOLVES.title}<" not in page.split('id="picked"', 1)[-1]
    assert f">{IRON.title}<" not in page.split('id="picked"', 1)[-1]


def run_pass(world: DeliveryWorld) -> None:
    """One run of the library pass as a dry run."""
    library_pass = world.services.library_pass
    assert library_pass is not None
    library_pass.mode = "dry_run"

    async def run() -> None:
        assert library_pass is not None
        assert await library_pass.run_once() is not None

    try:
        world.server.call(run)
    finally:
        library_pass.mode = "off"


def test_the_library_pass_settles_an_earlier_review_row(
    navidrome_factory: NavidromeFactory, tmp_path: Path, replay: Replay
) -> None:
    with world_with(navidrome_factory, tmp_path, [IRON, GOLDEN, STONES], replay) as world:
        fills = world.services.fills
        assert fills is not None
        ident = review_row(world, IRON, SEVERAL)
        other = review_row(world, GOLDEN, OTHER_TITLES)  # nothing a pick settles
        stones = album_id(world, STONES.title)
        fills.auto_pick = False  # a dry run with picks off: its plan is a review
        try:
            run_pass(world)
        finally:
            fills.auto_pick = True
        planned = row(world, stones) or {}
        assert (planned["outcome"], planned["planned"], planned["reason"]) == (
            "review", 1, SEVERAL)  # fmt: skip
        assert (row(world, ident) or {}).get("planned") == 0  # not looked at
        # Picks on: that plan is not followed - the first view picks.
        assert view(world, stones)["songCount"] == 9
        assert (row(world, stones) or {}).get("picked") == SEVERAL
        run_pass(world)
        found = row(world, ident) or {}
        # The dry run's plan (below the policy: to fill on first use).
        assert (found["outcome"], found["planned"], found["picked"]) == ("deferred", 1, SEVERAL)
        assert (found["title"], found["artist"]) == (IRON.title, IRON.artist)
        assert (found["owned_songs"], found["release_tracks"]) == (1, 13)
        assert (row(world, other) or {}).get("reason") == OTHER_TITLES  # left as it was
        album = view(world, ident)  # the dry run's plan: shown complete
        assert album["songCount"] == 13 and len(songs(world, ident)) == 1


def test_a_part_follows_the_album_its_release_is_picked_for(
    navidrome_factory: NavidromeFactory, tmp_path: Path, replay: Replay
) -> None:
    with world_with(navidrome_factory, tmp_path, [PART, WHOLE], replay) as world:
        fills = world.services.fills
        assert fills is not None
        part, whole = album_id(world, PART.title), album_id(world, WHOLE.title)
        album = view(world, whole)
        assert album["songCount"] == 17
        found = wait_for(world, whole, "deferred")
        assert (found["picked"], found["release_ref"]) == (SEVERAL, "demo:900000021")
        reviewed = wait_for(world, part, "review")
        assert reviewed["reason"] == (
            f"part of Neve Ashdown - Northern Letters (Remastered) (album {whole}):"
            " filled with it on first use"
        )
        world.client(client="viewer").ok("star", {"albumId": "sh.al.demo.900000021"})
        assert len(songs(world, whole)) == 17 and len(songs(world, part)) == 1
        assert (row(world, whole) or {}).get("outcome") == "filled"

        async def match_again() -> set[str]:  # the review list's action, on the part
            assert fills is not None
            await fills.rematch(part)
            await fills.fill(part, hold=True)
            return await fills.unsettled([part, whole])

        # Still its part, with its album's release: it hides the catalog's editions.
        assert world.server.call(match_again) == set()
        again = row(world, part) or {}
        assert again["reason"].startswith("part of ") and again["release_ref"] == ("demo:900000021")


def test_a_dry_run_settles_earlier_reviews_of_one_release_once(
    navidrome_factory: NavidromeFactory, tmp_path: Path, replay: Replay
) -> None:
    """Both albums of one release in review for several editions: one run makes the pick
    the album's plan and the other its part; the next run has nothing to do."""
    with world_with(navidrome_factory, tmp_path, [PART, WHOLE], replay) as world:
        whole, part = review_row(world, WHOLE, SEVERAL), review_row(world, PART, SEVERAL)
        run_pass(world)
        found = row(world, whole) or {}
        assert (found["outcome"], found["planned"], found["picked"]) == ("deferred", 1, SEVERAL)
        found = row(world, part) or {}
        assert (found["outcome"], found["planned"]) == ("review", 1)
        assert found["reason"].startswith("part of Neve Ashdown - Northern Letters (Remastered)")
        before = replay.api_requests
        run_pass(world)
        assert replay.api_requests == before
        fills = world.services.fills
        assert fills is not None

        async def kept() -> tuple[set[str], set[str]]:
            assert fills is not None
            settled = await fills.unsettled([whole, part])
            await fills.keep(whole)  # the owner keeps the album as it is
            return settled, await fills.unsettled([whole, part])

        # Matched, the album and its part hide the catalog's editions; kept as it is, the
        # release completes neither, and both show them.
        assert world.server.call(kept) == (set(), {whole, part})


def test_a_pick_fills_the_album_whose_new_tracks_keep_their_own_places(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """One release, two albums: the one with the closest title would get the fill, but two
    of the tracks it would add share a number on the release (the other album owns both) -
    the release fills the other album, and the closest-titled one is its part."""
    replay = replay_for_picks()
    for ident in ("900009521", "900009522"):  # two editions, its tracks 2 and 3 both at 2
        body = _copy(replay, "900000021", ident)["body"]
        body["tracks"][2]["number"] = 2
    for title in ("northern letters", "northern letters (remastered)"):
        _search(replay, f"neve ashdown {title}", "900009521", "900009522")
    closest = Album(  # one song, the release's title
        "Neve Ashdown",
        "Northern Letters (Remastered)",
        (Track("Copper Station", 0, seconds=243),),
        recording_date="1962",
    )
    most = Album(  # four songs, among them the two numbered 2 on the release
        "Neve Ashdown",
        "Northern Letters",
        (
            Track("Crimson Mirrors", 1, seconds=259),
            Track("Open", 2, seconds=181),
            Track("Faded Wolves", 3, seconds=208),
            Track("Open Mirrors", 4, seconds=207),
        ),
        recording_date="1962",
    )
    with world_with(navidrome_factory, tmp_path, [closest, most], replay) as world:
        target, part = album_id(world, most.title), album_id(world, closest.title)
        assert view(world, part)["songCount"] == 1  # a part: its own songs
        found = wait_for(world, target, "deferred")
        assert (found["picked"], found["release_ref"]) == (SEVERAL, "demo:900009521")
        assert (row(world, part) or {}).get("reason", "").startswith("part of ")
        assert view(world, target)["songCount"] == 17
        world.client(client="viewer").ok("star", {"albumId": "sh.al.demo.900009521"})
        assert len(songs(world, target)) == 17 and len(songs(world, part)) == 1
        assert (row(world, target) or {}).get("outcome") == "filled"
