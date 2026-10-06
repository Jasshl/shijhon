"""Suite I (continued) - owned albums waiting for their fill while the library pass is a dry
run, against a real Navidrome with the demo catalog's
records replayed.

During a dry run nothing is filled automatically, whatever the fill policy says (every
album qualifies here): a view shows an album complete; a search or an artist page showing
it, and a client syncing its albums, only match - each recorded as the dry run's plan
(``shijhon matches`` lists them as it would fill them). The first use of the album or of
one of its catalog songs fills it; a view in XML is a view like one in JSON.

Lists - ``getArtist``, ``search3``, ``getAlbumList2``, ``getStarred2`` - carry such an
album's complete song count and length, as its view does (clients file an album of two
songs under singles); its name, year and everything else stay Navidrome's, and an album
whose songs changed since its plan keeps Navidrome's counts until it is matched again.

A fill that a star or a rating starts never fails the star or the rating (it answered HTTP
500 when the fill raised something unexpected).
"""

from __future__ import annotations

import sqlite3
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.fill.fills import FillPolicy
from shijhon.views.bursts import Bursts
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.navidrome import ADMIN_USER
from tests.harness.replay import Replay
from tests.harness.subsonic import SubsonicClient

# Two tracks of a 17-track remastered edition: the lists' album.
LISTED = Album(
    "Neve Ashdown",
    "Northern Letters (Remastered)",
    (Track("Velvet Tides", 11, seconds=67), Track("Crimson Canyon", 17, seconds=24)),
    recording_date="1962",
)
# One track of a 13-track album: shown by a search and an artist page.
SHOWN = Album("BrokenMeadow", "Salt 2", (Track("Burning Engines", 1, seconds=301),),
              recording_date="1984")  # fmt: skip
# One track each of three albums: a client syncing them.
SYNCED = [
    Album("Oren Garrow", "Iron Wolves", (Track("Distant Towers", 1, seconds=274),),
          recording_date="2006"),
    Album("Jonah Fairbanks", "wild stones", (Track("FROZEN", 1, seconds=194),),
          recording_date="2010"),
    Album("Uma Okafor", "Bright Wolves 2 (Original Motion Picture Soundtrack)",
          (Track("Northern Machines", 11, seconds=84),), recording_date="2012"),
]  # fmt: skip
# One track of a 14-track album, viewed in XML.
IN_XML = Album("Esme Ashdown", "CRIMSON.", (Track("NORTHERN.", 1, seconds=118),),
               recording_date="2010")  # fmt: skip
# One track (by its ISRC) of another 14-track album: its fill fails.
FAILING = Album("Esme Ashdown", "PALE RIVERS.",
                (Track("NORTHERN.", 14, seconds=118, isrc="ZZ-SHJ-00-00059"),),
                recording_date="2010")  # fmt: skip
# One track of a 30-track soundtrack; the owner adds another after its plan.
CHANGED = Album(
    "Uma Okafor",
    "Open 2 (Original Motion Picture Soundtrack) [Expanded Edition]",
    (Track("Golden Bridges", 1, seconds=235),),
    recording_date="2012",
)
ADDED = Track("Last Rivers 2", 2, seconds=126)


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    for key in ("brokenmeadow", "brokenmeadow salt 2"):
        replay.aliases[key] = "restless islands"
    for key in ("oren garrow", "oren garrow iron wolves"):
        replay.aliases[key] = "crimson towers"
    for key in ("jonah fairbanks", "jonah fairbanks wild stones"):
        replay.aliases[key] = "northern horizon"
    for album in ("uma okafor", f"uma okafor {SYNCED[2].title.lower()}",
                  f"uma okafor {CHANGED.title.lower()}"):  # fmt: skip
        replay.aliases[album] = "restless voices"
    for key in ("esme ashdown", "esme ashdown crimson.", "esme ashdown pale rivers."):
        replay.aliases[key] = "bright canyon"  # PALE RIVERS. is found by its ISRC
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in (LISTED, SHOWN, *SYNCED, IN_XML, FAILING, CHANGED):
        write_album(nd.music, album)
    nd.scan(full=True)
    fill = {
        "enabled": True,
        "open_budget_seconds": 15,
        "background_pause_seconds": 0,
        "auto_min_songs": 1,  # without the dry run, every album would be filled
        "library_pass": "dry_run",
        "pass_start_seconds": 3600,  # the pass itself: test_I_library_pass.py
    }
    with delivery_world(
        nd, tmp_path_factory.mktemp("pending"), catalog=replay.catalog(), fill=fill
    ) as w:
        yield w


def album_id(world: DeliveryWorld, title: str) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == title]
    return str(ident)


def artist_id(world: DeliveryWorld, name: str) -> str:
    found = world.nd.client().ok("search3", {"query": name, "albumCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["artist"] if a["name"] == name]
    return str(ident)


def row(world: DeliveryWorld, ident: str) -> dict[str, Any] | None:
    async def read() -> dict[str, Any] | None:
        found = await world.services.store.fetchone(
            "SELECT outcome, reason, planned FROM album_matches WHERE album_id = ?", [ident]
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


def settled(world: DeliveryWorld) -> None:
    """Until no background match or fill is queued or running."""
    fills = world.services.fills
    assert fills is not None
    world.server.call(fills.wait_idle)


def songs(world: DeliveryWorld, ident: str) -> int:
    return int(world.nd.client().ok("getAlbum", {"id": ident})["album"]["songCount"])


def entry(albums: list[dict[str, Any]], ident: str) -> dict[str, Any]:
    [found] = [a for a in albums if a["id"] == ident]
    return found


def listed(client: Any, ident: str, artist: str) -> list[dict[str, Any]]:
    """The album's entry in each list, through ``client`` (Shijhon or Navidrome)."""
    albums = client.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
    found = client.ok("search3", {"query": "northern letters", "songCount": 0})
    page = client.ok("getArtist", {"id": artist})
    starred = client.ok("getStarred2")
    return [
        entry(albums["albumList2"]["album"], ident),
        entry(found["searchResult3"]["album"], ident),
        entry(page["artist"]["album"], ident),
        entry(starred["starred2"]["album"], ident),
    ]


def test_lists_carry_the_complete_counts_of_an_album_shown_complete(
    world: DeliveryWorld,
) -> None:
    ident = album_id(world, LISTED.title)
    artist = artist_id(world, LISTED.artist)
    viewer = world.client(client="lister")
    album = viewer.ok("getAlbum", {"id": ident})["album"]
    assert album["songCount"] == 17  # shown complete
    settled(world)
    assert wait_for(world, ident, "filled")["planned"] == 1  # the dry run's plan
    assert songs(world, ident) == 2  # the dry run filled nothing, the policy aside
    navidrome = world.nd.client()
    navidrome.ok("star", {"albumId": ident})  # starred before (e.g. in Navidrome's own UI)
    try:
        ours = listed(viewer, ident, artist)
        theirs = listed(navidrome, ident, artist)
        in_xml = xml_listed(viewer, ident, artist)  # the same counts in XML
        own_xml = xml_listed(navidrome, ident, artist)
    finally:
        navidrome.ok("unstar", {"albumId": ident})
    for mine, own in zip(ours, theirs, strict=True):
        assert (mine["songCount"], mine["duration"]) == (17, album["duration"])
        assert own["songCount"] == 2 and own["duration"] < mine["duration"]
        # (coverArt aside: Navidrome sets it when its artwork scan gets to the album, which
        # may fall between the two lists' requests; Shijhon passes it through.)
        rest = {k: v for k, v in own.items() if k not in ("songCount", "duration", "coverArt")}
        assert {
            k: v for k, v in mine.items() if k not in ("songCount", "duration", "coverArt")
        } == rest
    for mine, own in zip(in_xml, own_xml, strict=True):
        assert (mine["songCount"], mine["duration"]) == ("17", str(album["duration"]))
        # the attributes in Navidrome's order (coverArt aside, as below)
        assert [k for k in mine if k != "coverArt"] == [k for k in own if k != "coverArt"]
        # (coverArt aside: Navidrome sets it when its artwork scan gets to the album, which
        # may fall between the two lists' requests; Shijhon passes it through.)
        rest = {k: v for k, v in own.items() if k not in ("songCount", "duration", "coverArt")}
        assert {
            k: v for k, v in mine.items() if k not in ("songCount", "duration", "coverArt")
        } == rest
    assert songs(world, ident) == 2  # nothing written by the lists either


def xml_listed(client: Any, ident: str, artist: str) -> list[dict[str, str]]:
    """The album's element's attributes in each list, in XML (each answer valid)."""
    found = []
    for method, params in (
        ("getAlbumList2", {"type": "alphabeticalByName", "size": 500}),
        ("search3", {"query": "northern letters", "songCount": 0}),
        ("getArtist", {"id": artist}),
        ("getStarred2", {}),
    ):
        answer = client.request(method, params, fmt="xml")
        assert xsd.valid(answer.content)
        document = ET.fromstring(answer.content)  # noqa: S314 - test data
        [shown] = [e for e in document.iter() if e.get("id") == ident]
        found.append(dict(shown.attrib))
    return found


def test_lists_answer_as_navidrome_does_to_head_and_wrong_credentials(
    world: DeliveryWorld,
) -> None:
    """Navidrome's own answer decides (its "ok" is the credential check); nothing is looked
    up for a failed one. A client that takes gzip gets the list compressed, as from
    Navidrome."""
    params = {"type": "alphabeticalByName", "size": 500}
    fills = world.services.fills
    assert fills is not None
    looked: list[int] = []
    shown = fills.shown

    async def spy(album_ids: Any) -> Any:
        looked.append(1)
        return await shown(album_ids)

    fills.shown = spy  # type: ignore[method-assign]
    try:
        stranger = SubsonicClient(world.server.base_url, ADMIN_USER, "not-the-password")
        assert stranger.error_code("getAlbumList2", params) == 40
        stranger.close()
        assert looked == []
        head = world.client().request("getAlbumList2", params, http_method="HEAD")
        assert head.status_code == 200 and head.content == b"" and looked == []

        async def none_shown() -> bool:
            return False

        async def some_shown() -> bool:
            return True

        for shown_now, counted in ((none_shown, []), (some_shown, [1])):
            # None shown complete: Navidrome's own answer, streamed; else read and counted.
            fills.any_shown = shown_now  # type: ignore[method-assign]
            zipped = world.client().request(
                "getAlbumList2", params, headers={"accept-encoding": "gzip"}
            )
            assert zipped.headers.get("content-encoding") == "gzip"
            assert zipped.json()["subsonic-response"]["albumList2"]["album"]  # decoded
            assert looked == counted
    finally:
        fills.__dict__.pop("shown", None)
        fills.__dict__.pop("any_shown", None)


def test_a_search_or_artist_page_showing_an_album_fills_nothing_during_a_dry_run(
    world: DeliveryWorld,
) -> None:
    ident = album_id(world, SHOWN.title)
    browser = world.client(client="browser")
    found = browser.ok("search3", {"query": "brokenmeadow"})["searchResult3"]
    assert any(str(a["id"]).startswith("sh.al.") for a in found["album"])  # additions kept
    assert wait_for(world, ident, "filled")["planned"] == 1  # shown: matched, as a plan
    browser.ok("getArtist", {"id": artist_id(world, SHOWN.artist)})
    settled(world)
    assert songs(world, ident) == 1
    # The first use still fills it: a star of the album itself.
    with collected("shijhon.fill.fills") as lines:
        browser.ok("star", {"albumId": ident})
    assert songs(world, ident) == 13
    assert any(line.endswith("(first use: star)") for line in lines)
    assert wait_for(world, ident, "filled")["planned"] == 0


def test_a_sync_matches_albums_and_fills_nothing_during_a_dry_run(world: DeliveryWorld) -> None:
    fills = world.services.fills
    assert fills is not None
    previous = fills.syncs
    fills.syncs = Bursts(2, 30.0)
    syncer = world.client(client="syncer")
    idents = [album_id(world, album.title) for album in SYNCED]
    try:
        with collected("shijhon.fill.fills") as lines:
            for ident in idents:
                syncer.ok("getAlbum", {"id": ident})
            for ident in idents:  # the first by its view, the others in the background
                assert wait_for(world, ident, "filled")["planned"] == 1
            settled(world)
    finally:
        fills.syncs = previous
    assert any(line.startswith("albums: syncer viewed 2 never-matched albums") for line in lines)
    assert not any(line.startswith("filled ") for line in lines)
    for album, ident in zip(SYNCED, idents, strict=True):
        assert songs(world, ident) == len(album.tracks)


def test_an_xml_view_shows_the_album_complete_and_writes_nothing(world: DeliveryWorld) -> None:
    """Shown complete, the same data as in JSON, and
    only the dry run's plan recorded."""
    ident = album_id(world, IN_XML.title)
    viewer = world.client(client="xml-viewer")
    answer = viewer.request("getAlbum", {"id": ident}, fmt="xml")
    assert answer.status_code == 200 and xsd.valid(answer.content)
    assert answer.content.count(b"<song ") == 14 and answer.content.count(b'id="sh.tr.') == 13
    as_json = viewer.request("getAlbum", {"id": ident}, fmt="json").json()["subsonic-response"]
    root = ET.fromstring(answer.content)  # noqa: S314 - test data
    assert xsd.same_data(as_json, root) == []
    settled(world)
    assert wait_for(world, ident, "filled")["planned"] == 1  # the dry run's plan only
    assert songs(world, ident) == 1
    # JSONP and HEAD: Navidrome's own answers, nothing looked at or written.
    jsonp = {"id": ident, "f": "jsonp", "callback": "cb"}
    own = world.nd.client(client="xml-viewer").request("getAlbum", jsonp, fmt="xml")
    assert viewer.request("getAlbum", jsonp, fmt="xml").content == own.content
    head = viewer.request("getAlbum", {"id": ident}, fmt="xml", http_method="HEAD")
    assert head.status_code == 200 and head.content == b""
    assert songs(world, ident) == 1


def test_a_star_or_rating_goes_on_when_the_fill_it_starts_fails(world: DeliveryWorld) -> None:
    """Whatever the mode: the album waits for its first use (a policy that fills nothing
    automatically), which fails with an error the fill does not expect."""
    ident = album_id(world, FAILING.title)
    fills = world.services.fills
    assert fills is not None
    policy, fills.policy = fills.policy, FillPolicy(100, 1.0)

    async def locked(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")  # e.g. a busy timeout

    viewer = world.client(client="failing")
    try:
        assert viewer.ok("getAlbum", {"id": ident})["album"]["songCount"] == 14
        settled(world)
        fills.fill = locked  # type: ignore[method-assign]
        with collected("shijhon.views.commits") as lines:
            starred = viewer.request("star", {"id": ident})
            rated = viewer.request("setRating", {"id": ident, "rating": "3"})
    finally:
        fills.__dict__.pop("fill", None)
        fills.policy = policy
    for answer in (starred, rated):
        assert answer.status_code == 200
        assert answer.json()["subsonic-response"]["status"] == "ok"
    assert lines.count("fill on first use (star) failed: OperationalError") == 1
    assert lines.count("fill on first use (setRating) failed: OperationalError") == 1
    navidrome = world.nd.client()
    album = navidrome.ok("getAlbum", {"id": ident})["album"]
    assert album["songCount"] == 1 and album["userRating"] == 3 and album.get("starred")
    navidrome.ok("unstar", {"albumId": ident})


def test_an_album_whose_songs_changed_keeps_navidrome_s_counts_until_matched_again(
    world: DeliveryWorld,
) -> None:
    ident = album_id(world, CHANGED.title)
    viewer = world.client(client="changer")
    assert viewer.ok("getAlbum", {"id": ident})["album"]["songCount"] == 30
    settled(world)

    def counted() -> int:
        albums = viewer.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
        return int(entry(albums["albumList2"]["album"], ident)["songCount"])

    assert counted() == 30
    write_album(world.nd.music, Album(CHANGED.artist, CHANGED.title, (ADDED,),
                                      recording_date=CHANGED.recording_date))  # fmt: skip
    world.nd.scan(targets=[CHANGED.relative_folder])
    assert songs(world, ident) == 2
    assert counted() == 2  # the plan links one owned song: Navidrome's count
    assert viewer.ok("getAlbum", {"id": ident})["album"]["songCount"] == 30  # matched again
    assert counted() == 30
