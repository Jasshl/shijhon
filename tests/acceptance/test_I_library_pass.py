"""Suite I (continued) - the paced pass over the whole library, its dry run, the review
list's actions and a switch to another catalog, against a real
Navidrome with the demo catalog's records replayed.

The dry run records what the pass would do and writes nothing; switched on, the pass
follows those plans without asking the catalog again. Its catalog requests keep their
pace, and it waits while the catalog rests. The owner can fill an album on the review list
from a release of their choice (only one that has every owned track), keep it as it is, or
have it matched again. Filled albums are reused after a switch to another catalog, never
refilled.

Each test uses albums of its own and does not depend on the others' order (pytest-xdist
may split the module); a pass run by one test also looks at the others' albums, so tests
assert on their own albums only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.catalog.paced import PacedCatalog
from shijhon.fill.fills import ChoiceRefused, Decision, Fills
from shijhon.fill.library_pass import LibraryPass
from shijhon.fill.report import report
from shijhon.matching.matcher import Matcher
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.replay import Replay

# The dry run and the pass.
REMASTERED = Album(
    "Neve Ashdown",
    "Northern Letters (Remastered)",
    (Track("Velvet Tides", 11, seconds=67), Track("Crimson Canyon", 17, seconds=24)),
    recording_date="1962",
)
SOUNDTRACK = Album(
    "Uma Okafor",
    "Bright Wolves 2 (Original Motion Picture Soundtrack)",
    (Track("Northern Machines", 11, seconds=84),),
    recording_date="2012",
)
WHOLE = Album(
    "Tag Keeper", "Whole Record", (Track("One", 1), Track("Two", 2)), track_total=2, disc_total=1
)
UNKNOWN = Album("Nobody Known", "Unheard Album", (Track("Only", 1),))
PASSED = [REMASTERED, SOUNDTRACK, WHOLE, UNKNOWN]
# The pace and the rest.
PACED = [Album(f"Pacer {n}", f"Paced Album {n}", (Track("Only", 1),)) for n in range(3)]
RESTED = Album("Rest Taker", "Rested Album", (Track("Only", 1),))
CHANGED = Album("Retagger", "Changed Album", (Track("Only", 1),))
# The review list: the recording is on two catalog albums, neither with this title.
REVIEWED = [
    Album(
        "Esme Ashdown",
        title,
        (Track("NORTHERN.", 14, seconds=118, isrc="ZZ-SHJ-00-00059"),),
        recording_date="2010",
    )
    for title in ("Northern Hits", "Northern Hits Two")
]
KEPT = Album("Keeper Artist", "Kept Album", (Track("Only", 1),))
# The catalog switch.
SWITCHED = Album(
    "Esme Ashdown", "CRIMSON.", (Track("NORTHERN.", 1, seconds=118),), recording_date="2010"
)
UNMATCHED = Album("Nobody Else", "Another Unheard Album", (Track("Only", 1),))
ALBUMS = [*PASSED, *PACED, RESTED, CHANGED, *REVIEWED, KEPT, SWITCHED, UNMATCHED]


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    replay.aliases[f"uma okafor {SOUNDTRACK.title.lower()}"] = "restless voices"
    replay.aliases["esme ashdown crimson."] = "bright canyon"
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in ALBUMS:
        write_album(nd.music, album)
    nd.scan(full=True)
    fill = {
        "enabled": True,
        "open_budget_seconds": 15,
        "background_pause_seconds": 0,
        "auto_min_songs": 1,  # the mechanics, for every album (the policy: test_I_policy.py)
        "library_pass": "off",  # run by hand below
        "pass_requests_per_second": 50,
    }
    with delivery_world(
        nd, tmp_path_factory.mktemp("pass"), catalog=replay.catalog(), fill=fill
    ) as w:
        yield w


def services(world: DeliveryWorld) -> tuple[Fills, LibraryPass]:
    fills, library_pass = world.services.fills, world.services.library_pass
    assert fills is not None and library_pass is not None
    return fills, library_pass


def album_id(world: DeliveryWorld, title: str) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == title]
    return str(ident)


def row(world: DeliveryWorld, album: Album) -> dict[str, Any] | None:
    ident = album_id(world, album.title)

    async def read() -> dict[str, Any] | None:
        found = await world.services.store.fetchone(
            "SELECT outcome, planned, release_ref, reason FROM album_matches"
            " WHERE album_id = ? AND scope = 'demo.xx'",
            [ident],
        )
        return dict(found) if found else None

    return world.server.call(read)


def songs(world: DeliveryWorld, album: Album) -> int:
    answer = world.nd.client().ok("getAlbum", {"id": album_id(world, album.title)})
    return int(answer["album"]["songCount"])


def run_pass(world: DeliveryWorld, mode: str) -> None:
    _, library_pass = services(world)
    library_pass.mode = mode  # type: ignore[assignment]

    async def run() -> None:
        assert await library_pass.run_once() is not None

    try:
        world.server.call(run)
    finally:
        library_pass.mode = "off"


def rematch(world: DeliveryWorld, album: Album) -> None:
    fills, _ = services(world)
    ident = album_id(world, album.title)

    async def forget() -> None:
        await fills.rematch(ident)

    world.server.call(forget)


def forget_albums(world: DeliveryWorld) -> None:
    """The album answers kept are dropped: the next one is the record as it is now."""
    catalog = world.services.catalog

    async def clear() -> None:
        catalog.album.cache_clear()  # type: ignore[union-attr]

    world.server.call(clear)


def choose(world: DeliveryWorld, ident: str, release: str) -> Decision:
    fills, _ = services(world)

    async def chosen() -> Decision:
        return await fills.choose(ident, release)

    return world.server.call(chosen)


def keep(world: DeliveryWorld, ident: str) -> None:
    fills, _ = services(world)

    async def kept() -> None:
        await fills.keep(ident)

    world.server.call(kept)


def paced_requests(world: DeliveryWorld) -> int:
    catalog = services(world)[1].matcher.catalog
    assert isinstance(catalog, PacedCatalog)
    return catalog.requests


def test_the_dry_run_lists_what_the_pass_would_do_and_the_pass_follows_it(
    world: DeliveryWorld, replay: Replay
) -> None:
    files = world.placeholder_files()
    run_pass(world, "dry_run")
    assert world.placeholder_files() == files  # nothing written
    assert [songs(world, a) for a in PASSED] == [2, 1, 2, 1]
    expected = {
        REMASTERED.title: ("filled", "demo:900000021"),
        SOUNDTRACK.title: ("filled", "demo:900000182"),
        WHOLE.title: ("complete", None),
        UNKNOWN.title: ("none", None),
    }
    for album in PASSED:
        found = row(world, album)
        assert found is not None and found["planned"] == 1, album.title
        assert (found["outcome"], found["release_ref"]) == expected[album.title], album.title
    listed = report(world.app.settings.database_path)
    assert "Would fill (dry run):" in listed
    assert (
        "Neve Ashdown - Northern Letters (Remastered): 15 of 17 tracks to add"
        " from demo:900000021" in listed
    )
    before = paced_requests(world), replay.api_requests
    run_pass(world, "dry_run")  # planned once: nothing asked again
    assert (paced_requests(world), replay.api_requests) == before

    # Switched on: the plans are followed without asking the catalog again.
    run_pass(world, "on")
    assert (paced_requests(world), replay.api_requests) == before
    for album in PASSED:
        found = row(world, album)
        assert found is not None and found["planned"] == 0, album.title
        assert (found["outcome"], found["release_ref"]) == expected[album.title], album.title
    assert [songs(world, a) for a in PASSED] == [17, 19, 2, 1]


def test_the_pass_keeps_its_pace(world: DeliveryWorld, replay: Replay) -> None:
    _, library_pass = services(world)
    wired = library_pass.matcher.catalog  # the application's own: its setting's pace
    assert isinstance(wired, PacedCatalog) and wired.per_second == 50
    for album in PACED:
        rematch(world, album)
    catalog = world.services.catalog
    assert catalog is not None
    library_pass.matcher = Matcher(PacedCatalog(catalog, 4.0))
    start = len(replay.times)
    try:
        run_pass(world, "dry_run")
    finally:
        library_pass.matcher = Matcher(wired)
    times = replay.times[start:]
    assert len(times) >= 2 * len(PACED)  # an album search and a track search each
    # Four a second at most: measured where the requests arrive, so over the whole run.
    assert times[-1] - times[0] >= 0.9 * (len(times) - 1) / 4.0
    for album in PACED:
        assert (row(world, album) or {}).get("outcome") == "none"


def test_the_pass_waits_while_the_catalog_rests(world: DeliveryWorld, replay: Replay) -> None:
    fills, _ = services(world)
    rematch(world, RESTED)
    start = len(replay.times)
    began = time.monotonic()
    fills.resting_until = fills.clock() + 1.0  # an open's catalog failure a moment ago
    run_pass(world, "dry_run")
    assert (row(world, RESTED) or {}).get("outcome") == "none"  # matched, not skipped
    assert replay.times[start:] and replay.times[start] - began >= 0.9


def test_a_plan_for_an_album_that_changed_is_not_followed(
    world: DeliveryWorld, replay: Replay
) -> None:
    """A plan remembers the owned songs it was made from (IDs, positions, titles, lengths,
    totals); after a retag the album is matched again when the plan is acted on (here the
    pass's own step, as with the pass on; a view, in JSON or XML, leaves a plan with nothing
    to add to the pass)."""
    ident = album_id(world, CHANGED.title)
    rematch(world, CHANGED)
    run_pass(world, "dry_run")
    assert (row(world, CHANGED) or {}).get("planned") == 1

    async def retagged() -> None:  # as if its track number had been changed since
        found = await world.services.store.fetchone(
            "SELECT plan FROM album_matches WHERE album_id = ? AND scope = 'demo.xx'", [ident]
        )
        assert found is not None
        plan = json.loads(found["plan"])
        plan["owned"][0][2] = 7
        await world.services.store.execute(
            "UPDATE album_matches SET plan = ? WHERE album_id = ?", [json.dumps(plan), ident]
        )

    world.server.call(retagged)
    before = replay.api_requests
    viewer = world.client(client="retagger")
    viewer.ok("getAlbum", {"id": ident})  # nothing to show, the plan left alone
    assert viewer.request("getAlbum", {"id": ident}, fmt="xml").status_code == 200
    assert replay.api_requests == before and (row(world, CHANGED) or {}).get("planned") == 1
    fills, _ = services(world)

    async def pass_step() -> None:
        await fills.fill(ident, auto=True)

    world.server.call(pass_step)
    assert replay.api_requests > before  # matched again, not the plan
    assert row(world, CHANGED) == {
        "outcome": "none",
        "planned": 0,
        "release_ref": None,
        "reason": row(world, CHANGED)["reason"],  # type: ignore[index]
    }


def test_the_review_list(world: DeliveryWorld, replay: Replay) -> None:
    first, second = (album_id(world, a.title) for a in REVIEWED)
    for index, ident in enumerate((first, second)):  # one client each: not back to back
        world.client(client=f"reviewer-{index}").ok("getAlbum", {"id": ident})
    for album in REVIEWED:
        found = row(world, album)
        assert found is not None and found["outcome"] == "review", album.title
        assert "other titles" in found["reason"]
    for ident, release, why in (
        (first, "demo:900000021", "an owned track is not on this release"),
        (first, "other:900000101", "another catalog"),
        (first, "demo:999999999", "no such release"),
        (album_id(world, WHOLE.title), "demo:900000101", "track totals"),
    ):
        with pytest.raises(ChoiceRefused, match=why):
            choose(world, ident, release)
    # Chosen or not: never from a release whose track list could not be read in full, nor
    # from one without track numbers of its own (they are its list's order) when the owned
    # files are numbered differently.
    record = replay.by_path["albums/900000101"]["body"]
    last = record["tracks"][13]
    for change, why in (
        ({"incomplete": True}, "track list of this release is incomplete"),
        ({"numbered": False}, "without numbers"),
    ):
        forget_albums(world)
        record.update(change)
        last["number"] = 15 if "numbered" in change else 14  # (the owned track is number 14)
        try:
            with pytest.raises(ChoiceRefused, match=why):
                choose(world, first, "demo:900000101")
        finally:
            for key in change:
                del record[key]
            last["number"] = 14
            forget_albums(world)
    assert songs(world, REVIEWED[0]) == 1  # nothing was added
    decision = choose(world, first, "demo:900000101")
    assert decision.outcome == "filled"
    assert songs(world, REVIEWED[0]) == 14
    assert row(world, REVIEWED[0]) == {
        "outcome": "filled",
        "planned": 0,
        "release_ref": "demo:900000101",
        "reason": "chosen from the review list",
    }
    with pytest.raises(ChoiceRefused, match="already filled"):
        choose(world, first, "demo:900000101")
    with pytest.raises(ChoiceRefused, match="already filled"):
        keep(world, first)
    with pytest.raises(ChoiceRefused, match="in the library as another album"):
        choose(world, second, "demo:900000101")

    # Kept as it is: an album never matched is then not matched when opened.
    kept = album_id(world, KEPT.title)
    rematch(world, KEPT)
    keep(world, kept)
    before = replay.api_requests
    world.client(client="keeper").ok("getAlbum", {"id": kept})
    assert replay.api_requests == before
    assert (row(world, KEPT) or {}).get("outcome") == "kept"
    # Matched again on request.
    rematch(world, REVIEWED[1])
    world.client(client="reviewer-3").ok("getAlbum", {"id": second})
    assert replay.api_requests > before
    assert (row(world, REVIEWED[1]) or {}).get("outcome") == "review"
    assert songs(world, REVIEWED[1]) == 1


def test_a_catalog_switch_reuses_filled_albums(world: DeliveryWorld, replay: Replay) -> None:
    filled, unmatched = album_id(world, SWITCHED.title), album_id(world, UNMATCHED.title)
    world.client(client="switch-1").ok("getAlbum", {"id": filled})  # filled, if not yet
    world.client(client="switch-2").ok("getAlbum", {"id": unmatched})
    deadline = time.monotonic() + 20  # the view's fill runs in the background
    while songs(world, SWITCHED) != 14 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert songs(world, SWITCHED) == 14
    placeholders = world.placeholder_rows()
    catalog = world.services.catalog
    assert catalog is not None
    # Another catalog or region: its matches are kept apart.
    other = Fills(
        world.services.store,
        world.services.navidrome,
        world.services.engine,
        Matcher(catalog),
        scope="demo.zz",
    )

    async def fill(ident: str) -> Decision | None:
        return await other.fill(ident)

    before = replay.api_requests
    assert world.server.call(lambda: fill(filled)) is None  # reused, not refilled
    assert replay.api_requests == before and world.placeholder_rows() == placeholders
    decision = world.server.call(lambda: fill(unmatched))  # matched again
    assert decision is not None and decision.outcome == "none"
    assert replay.api_requests > before
    # Its placeholders keep playing: audio is found by ISRC, not through the catalog.

    async def one_placeholder() -> tuple[str, str]:
        found = await world.services.store.fetchone(
            "SELECT song_id, isrc FROM placeholders WHERE release_ref = 'demo:900000117' LIMIT 1"
        )
        assert found is not None
        return str(found["song_id"]), str(found["isrc"])

    song, isrc = world.server.call(one_placeholder)
    addon = world.addon("Switch")
    world.add_source(addon)
    try:
        audio = world.audio("switch")
        addon.add(FakeTrack(isrc=isrc, audio=audio))
        answer = world.client().request("stream", {"id": song})
        assert answer.status_code == 200 and answer.content == audio.read_bytes()
    finally:
        world.clear_sources()


def test_the_pass_says_when_it_is_running(world: DeliveryWorld) -> None:
    """The dashboard reads it (a run's progress alone cannot tell a stopped run)."""
    fills, library_pass = services(world)
    seen: list[bool] = []
    due = fills.due

    async def watching(album: str, **kwargs: Any) -> bool:
        seen.append(library_pass.running)
        return await due(album, **kwargs)

    fills.due = watching  # type: ignore[method-assign]
    try:
        assert not library_pass.running
        run_pass(world, "dry_run")
    finally:
        fills.__dict__.pop("due", None)
    assert seen and all(seen) and not library_pass.running
