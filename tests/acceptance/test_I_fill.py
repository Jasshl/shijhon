"""Suite I (and suite H's end-to-end part) - filling partially owned albums
against a real Navidrome, with the demo catalog's records replayed.

An owned album a client views is shown complete at once - Navidrome's answer with the
release's missing tracks as catalog songs of the album - and nothing is written by the
view; these albums meet the fill policy (every album does here), so they are then filled in
the background. An owned album a search result shows is filled in the background without
slowing the search. The owned tracks, the album's ID, name and cover stay as they were; the
placeholders join the album. Each album is matched once; a client viewing many never-matched
albums in a short time (a sync) gets them matched in the background; a view does not wait
beyond its budget for a slow catalog. An XML view is a view like a JSON one: the
album complete at once, nothing written by the view.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.views.bursts import Bursts
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.replay import Replay

# Two tracks of the remastered edition (17 tracks) of a catalog album with several.
OPENED = Album(
    "Neve Ashdown",
    "Northern Letters (Remastered)",
    (Track("Velvet Tides", 11, seconds=67), Track("Crimson Canyon", 17, seconds=24)),
    recording_date="1962",
)
# One track of a 19-track soundtrack.
EXPOSED = Album(
    "Uma Okafor",
    "Bright Wolves 2 (Original Motion Picture Soundtrack)",
    (Track("Northern Machines", 11, seconds=84),),
    recording_date="2012",
)
# One track of an album the catalog has in explicit and clean editions.
ON_ARTIST_PAGE = Album(
    "Esme Ashdown", "CRIMSON.", (Track("NORTHERN.", 1, seconds=118),), recording_date="2010"
)
# One track, with its ISRC (written with hyphens), of an album the search does not find;
# the recording is on another album too.
BY_ISRC = Album(
    "Esme Ashdown",
    "PALE RIVERS.",
    (Track("NORTHERN.", 14, seconds=118, isrc="ZZ-SHJ-00-00059"),),
    recording_date="2010",
)
# Two tracks of a 13-track album, viewed in XML.
IN_XML = Album(
    "BrokenMeadow",
    "Salt 2",
    (Track("Burning Engines", 1, seconds=301), Track("Wild Cities", 2, seconds=256)),
    recording_date="1984",
)
# Albums the catalog does not have.
UNKNOWN = [Album(f"Walker {n}", f"Walked Album {n}", (Track("Only", 1),)) for n in range(3)]
LISTED = [Album(f"Lister {n}", f"Listed Album {n}", (Track("Only", 1),)) for n in range(2)]
# Whole albums by their track totals (in each tag style), and one the totals say is partial.
WHOLE = [
    Album(
        f"Whole {fmt}",
        f"Whole Album {fmt}",
        tuple(Track(f"Part {n}", n, disc=d) for d in (1, 2) for n in (1, 2)),
        fmt=fmt,
        track_total=2,
        disc_total=2,
    )
    for fmt in ("flac", "mp3", "m4a")
]
PARTIAL = Album("Whole flac", "Partial Album", (Track("Part 1", 1),), track_total=3)
NS = "{http://subsonic.org/restapi}"


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    editions = "tender rivers"  # the recorded search with the album's three editions
    replay.aliases["neve ashdown northern letters (remastered)"] = editions
    soundtracks = "restless voices"
    replay.aliases["uma okafor"] = soundtracks
    replay.aliases[f"uma okafor {EXPOSED.title.lower()}"] = soundtracks
    twins = "bright canyon"  # the recorded search with both editions of the album
    replay.aliases["esme ashdown"] = twins
    replay.aliases["esme ashdown crimson."] = twins
    replay.aliases["brokenmeadow salt 2"] = "restless islands"
    replay.aliases["brokenmeadow"] = "restless islands"
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    albums = (OPENED, EXPOSED, ON_ARTIST_PAGE, BY_ISRC, IN_XML, *UNKNOWN, *LISTED, *WHOLE, PARTIAL)
    for album in albums:
        write_album(nd.music, album)
    nd.scan(full=True)
    # A generous budget for opening (fills take about a second; the budget itself is not
    # what these tests are about), and no pause between background matches.
    fill = {
        "enabled": True,
        "open_budget_seconds": 15,
        "background_pause_seconds": 0,
        "auto_min_songs": 1,  # the mechanics, for every album (the policy: test_I_policy.py)
        "library_pass": "off",  # its own tests: test_I_library_pass.py
    }
    with delivery_world(
        nd, tmp_path_factory.mktemp("fill"), catalog=replay.catalog(), fill=fill
    ) as w:
        yield w


def album_id(world: DeliveryWorld, title: str) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == title]
    return str(ident)


def matched(world: DeliveryWorld, ident: str) -> dict[str, Any] | None:
    async def read() -> dict[str, Any] | None:
        row = await world.services.store.fetchone(
            "SELECT outcome, release_ref, reason FROM album_matches WHERE album_id = ?", [ident]
        )
        return dict(row) if row else None

    return world.server.call(read)


def wait_matched(world: DeliveryWorld, ident: str, outcome: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = matched(world, ident)
        if found is not None and found["outcome"] == outcome:
            return found
        time.sleep(0.1)
    raise AssertionError(f"{ident}: {matched(world, ident)}")


def test_a_viewed_album_is_complete_at_once_and_filled_in_the_background(
    world: DeliveryWorld, replay: Replay
) -> None:
    ident = album_id(world, OPENED.title)
    before = world.nd.client().ok("getAlbum", {"id": ident})["album"]
    owned = [s["id"] for s in before["song"]]
    viewer = world.client(client="viewer")
    album = viewer.ok("getAlbum", {"id": ident})["album"]
    # The complete album at once: the owned songs as they are, the others from the catalog.
    assert album["id"] == ident and album["songCount"] == 17
    assert [s["track"] for s in album["song"]] == list(range(1, 18))
    assert [s["id"] for s in album["song"] if s["track"] in (11, 17)] == owned  # kept
    assert all(s["id"].startswith("sh.tr.demo.") for s in album["song"] if s["id"] not in owned)
    for key in ("name", "coverArt", "artistId", "year", "artists", "genre"):
        assert album.get(key) == before.get(key), key
    assert album["duration"] > before["duration"]
    for song in album["song"]:  # the catalog songs belong to this album
        assert (song["albumId"], song["parent"], song["album"]) == (ident, ident, before["name"])
        if song["id"] not in owned:
            assert song["coverArt"] == before["coverArt"]
            assert song["albumArtists"] == before["artists"]
    # The fill follows in the background (the policy allows it here).
    filled = wait_matched(world, ident, "filled")
    assert filled == {"outcome": "filled", "release_ref": "demo:900000021", "reason": ""}
    native = world.nd.client().ok("getAlbum", {"id": ident})["album"]
    assert native["songCount"] == 17
    requests = replay.api_requests
    again = viewer.ok("getAlbum", {"id": ident})["album"]
    assert replay.api_requests == requests  # matched once
    assert not any(s["id"].startswith("sh.") for s in again["song"])  # Navidrome's own now
    assert [s["title"] for s in again["song"]] == [s["title"] for s in album["song"]]


def test_an_album_a_search_shows_is_filled_in_the_background(
    world: DeliveryWorld, replay: Replay
) -> None:
    ident = album_id(world, EXPOSED.title)
    replay.slow["albums/900000182"] = 1.5  # the match takes a while
    try:
        started = time.monotonic()
        found = world.client().ok("search3", {"query": "uma okafor"})["searchResult3"]
        took = time.monotonic() - started
        assert ident in [a["id"] for a in found["album"]]
        deadline = time.monotonic() + 15
        while matched(world, ident) is None and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        replay.slow.clear()
    assert took < 1.5  # the search did not wait for the fill
    assert (matched(world, ident) or {}).get("outcome") == "filled"
    album = world.nd.client().ok("getAlbum", {"id": ident})["album"]
    assert album["songCount"] == 19


def test_an_album_an_artist_page_shows_is_filled_with_the_owned_kind_of_edition(
    world: DeliveryWorld,
) -> None:
    ident = album_id(world, ON_ARTIST_PAGE.title)
    found = world.nd.client().ok("search3", {"query": "esme ashdown", "albumCount": 0})
    [artist] = [a["id"] for a in found["searchResult3"]["artist"] if a["name"] == "Esme Ashdown"]
    world.client().ok("getArtist", {"id": artist})
    deadline = time.monotonic() + 15
    while matched(world, ident) is None and time.monotonic() < deadline:
        time.sleep(0.1)
    # The owned files are not clean: the explicit edition, not its clean twin.
    assert matched(world, ident) == {"outcome": "filled", "release_ref": "demo:900000117",
                                     "reason": ""}  # fmt: skip
    assert world.nd.client().ok("getAlbum", {"id": ident})["album"]["songCount"] == 14


def test_a_client_viewing_many_never_matched_albums_gets_them_matched_in_the_background(
    world: DeliveryWorld, replay: Replay
) -> None:
    fills = world.services.fills
    assert fills is not None
    previous = fills.syncs
    fills.syncs = Bursts(2, 30.0)
    walker = world.client(client="album-walker")
    replay.slow["search"] = 1.0
    took: list[float] = []
    try:
        with collected("shijhon.fill.fills") as lines:
            for album in UNKNOWN:
                started = time.monotonic()
                answer = walker.ok("getAlbum", {"id": album_id(world, album.title)})
                took.append(time.monotonic() - started)
                assert answer["album"]["songCount"] == len(album.tracks)
            idents = [album_id(world, a.title) for a in UNKNOWN]
            for ident in idents:  # matched in the background, one at a time
                wait_matched(world, ident, "none")
    finally:
        replay.slow.clear()
        fills.syncs = previous
        walker.close()
    assert took[0] >= 1.0 and max(took[1:]) < 1.0  # the first waited for its match only
    assert any(
        line.startswith("albums: album-walker viewed 2 never-matched albums") for line in lines
    )
    # Nothing was written for them.
    for album in UNKNOWN:
        songs = world.nd.client().ok("getAlbum", {"id": album_id(world, album.title)})
        assert songs["album"]["songCount"] == len(album.tracks)


def test_the_owned_files_isrcs_find_an_album_the_search_misses(world: DeliveryWorld) -> None:
    """The ISRC is on two albums; only the one with the owned album's title fills it."""
    ident = album_id(world, BY_ISRC.title)
    album = world.client(client="isrc-viewer").ok("getAlbum", {"id": ident})["album"]
    assert album["songCount"] == 14  # complete at once
    assert wait_matched(world, ident, "filled") == {
        "outcome": "filled", "release_ref": "demo:900000101", "reason": ""
    }  # fmt: skip


def test_a_view_does_not_wait_beyond_its_budget_for_a_slow_catalog(
    world: DeliveryWorld, replay: Replay
) -> None:
    """The owned songs only, the reason logged; the match goes on in the background."""
    fills = world.services.fills
    assert fills is not None
    ident = album_id(world, LISTED[0].title)
    viewer = world.client(client="slow-viewer")
    previous = fills.budget
    fills.budget = 0.5
    replay.slow["search"] = 2.0
    try:
        with collected("shijhon.views.complete") as lines:
            started = time.monotonic()
            album = viewer.ok("getAlbum", {"id": ident})["album"]
            took = time.monotonic() - started
        assert matched(world, ident) is None  # not ready yet
        wait_matched(world, ident, "none")  # it went on
    finally:
        fills.budget = previous
        replay.slow.clear()
        viewer.close()
    assert took < 1.5 and album["songCount"] == 1
    assert lines == [
        "album view: Lister 0 - Listed Album 0: no match within 0.5s (the match goes on);"
        " the owned songs only"
    ]


def test_an_xml_view_is_complete_at_once_like_a_json_view(world: DeliveryWorld) -> None:
    """The album complete at once, in Navidrome's XML, its own songs as
    Navidrome wrote them; the view writes nothing, and the fill follows in the background
    (the policy allows it here), as after a JSON view."""
    ident = album_id(world, IN_XML.title)
    direct = world.nd.client().request("getAlbum", {"id": ident}, fmt="xml").content
    viewer = world.client(client="xml-viewer")
    answer = viewer.request("getAlbum", {"id": ident}, fmt="xml")
    assert answer.status_code == 200 and xsd.valid(answer.content)
    album = ET.fromstring(answer.content).find(f"{NS}album")  # noqa: S314 - test data
    assert album is not None and album.get("songCount") == "13"
    songs = album.findall(f"{NS}song")
    assert [s.get("track") for s in songs] == [str(n) for n in range(1, 14)]
    assert sum((s.get("id") or "").startswith("sh.tr.demo.") for s in songs) == 11  # not filled
    owned = re.findall(rb'<song id="[^"]*".*?</song>', direct, re.DOTALL)
    assert len(owned) == 2 and all(song in answer.content for song in owned)
    assert wait_matched(world, ident, "filled")["release_ref"] == "demo:900000203"


def test_owned_files_that_say_the_album_is_whole_are_complete(
    world: DeliveryWorld, replay: Replay
) -> None:
    """Every track the track and disc totals name is owned: complete, nothing added and the
    catalog not asked (FLAC totals, MP3 "1/2", M4A number pairs)."""
    for album in WHOLE:
        ident = album_id(world, album.title)
        before = len(replay.log)
        # One client each: albums opened back to back are matched in the background.
        answer = world.client(client=f"whole-{album.fmt}").ok("getAlbum", {"id": ident})["album"]
        assert answer["songCount"] == 4
        found = matched(world, ident)
        assert found is not None and found["outcome"] == "complete", album.fmt
        assert "track totals" in found["reason"] and found["release_ref"] is None
        assert replay.log[before:] == [], album.fmt
    # Totals naming more tracks than are owned: matched as before.
    ident = album_id(world, PARTIAL.title)
    before = len(replay.log)
    world.client(client="partial").ok("getAlbum", {"id": ident})
    assert (matched(world, ident) or {}).get("outcome") == "none"
    assert len(replay.log) > before


def test_an_album_navidrome_does_not_know_is_left_to_navidrome_quietly(
    world: DeliveryWorld,
) -> None:
    """A client's stale album ID ("Album not found"): Navidrome's own error, nothing
    matched, no warning."""
    stale = world.client(client="stale-cache")
    with collected("shijhon") as lines:
        code = stale.error_code("getAlbum", {"id": "0000000000000000000000"})
        xml = stale.request("getAlbum", {"id": "0000000000000000000001"}, fmt="xml")
        time.sleep(0.5)  # anything started in the background has ended
    assert code == 70 and b'code="70"' in xml.content
    assert matched(world, "0000000000000000000000") is None
    assert not any("RuntimeError" in line or "fill of an album failed" in line for line in lines)
