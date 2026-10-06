"""Suite J (continued) - an artist's top songs, as an artist's page shows them.

getTopSongs names an artist (Navidrome 0.64.2 also takes its ID). The answer is the
catalog's top songs of the artist of that name - up to the requested count, in JSON and
XML - for an artist the library has songs of too: a song the library has (an owned one, a
placeholder) is the library's own entry for the client, the others are catalog songs
like search results; Navidrome's own top songs, when it has any, stay first as it gives
them. The name is the catalog artist shown most recently with exactly that name, else
the first exact match of a catalog search. It is a view: saved like discographies
(an old list answers at once and is refreshed in the background), a bounded wait (the
artist pages' budget), nothing asked while a client walks artist pages or trips the search
guard, and Navidrome's own answer, unchanged, when the catalog has no list, fails or is
too slow.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.navidrome.client import NavidromeError
from shijhon.views.bursts import Bursts
from shijhon.views.library_songs import Unknown
from shijhon.views.shown import ShownArtists
from tests.acceptance.test_J_contract import check
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.navidrome import ADMIN_USER
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient
from tests.harness.xsd import same_data

HOST = {"host": "music.test"}
OWNED = Album(
    "Oren Garrow",
    "Hollow 2",
    (
        Track("One", 1),
        Track("Two", 2, artists=("Oren Garrow", "Guest Voice")),
        # A guest the catalog also has as an artist (the composer fixture's).
        Track("Three", 3, artists=("Oren Garrow", "Uma Okafor")),
    ),
)
BAND = "BrokenMeadow"  # the catalog's; not in the library
TOP = fixture("top-songs-band")
BAND_ID = str(TOP["path"]).split("/")[1]
TOP_IDS = [f"sh.tr.demo.{row['ref']['id']}" for row in TOP["body"]]
# Catalog artists without top songs in the fixtures.
OTHERS = ["Felix Fairbanks", "FrozenPilots", "Wren Norrell"]
FAILING = "Leon Loring 2"
UNSHOWN = ["Mara Merritt", "Zeno Brandt"]  # in a recorded search no other test makes


def item(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def artist_of(name: str) -> str:
    rows = [
        row
        for search in ("search-covers", "search-ep")
        for row in fixture(search)["body"]["artists"]
    ]
    return str(next(r["ref"]["id"] for r in rows if r["name"] == name))


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases["brokenmeadow"] = str(fixture("search-anniversary")["params"]["term"])
    for name in (*OTHERS, FAILING):
        replay.aliases[name.lower()] = str(fixture("search-covers")["params"]["term"])
    for name in UNSHOWN:
        replay.aliases[name.lower()] = str(fixture("search-ep")["params"]["term"])
    # The library's artists: one the catalog has (without a top-songs list), one not.
    replay.aliases["oren garrow"] = str(fixture("search-duo")["params"]["term"])
    replay.aliases["guest voice"] = str(fixture("search-covers")["params"]["term"])
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("top"), catalog=replay.catalog()) as w:
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client(headers=HOST, client="Toplister")
    yield c
    c.close()


def top(client: SubsonicClient, **params: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = client.ok("getTopSongs", params)["topSongs"].get("song", [])
    return found


def asked(replay: Replay, artist: str) -> int:
    return replay.log.count(f"artists/{artist}/top-songs")


def saved_at(world: DeliveryWorld, key: str) -> float | None:
    async def read() -> float | None:
        row = await world.services.store.fetchone(
            "SELECT fetched_at FROM discographies WHERE key = ?", [key]
        )
        return float(row["fetched_at"]) if row else None

    return world.server.call(read)


def navidrome_s(world: DeliveryWorld, params: dict[str, Any], fmt: str = "json") -> bytes:
    direct = world.nd.client(headers=HOST, client="Toplister")
    return direct.request("getTopSongs", params, fmt=fmt).content  # type: ignore[arg-type]


def test_an_artist_not_in_the_library_gets_the_catalog_s_top_songs(
    client: SubsonicClient, replay: Replay, search_log: list[str]
) -> None:
    songs = top(client, artist=BAND, count=5)
    assert [s["id"] for s in songs] == TOP_IDS[:5]  # the catalog's order, up to the count
    assert check("song", songs) == (0, 5)  # Navidrome's keys and types
    assert {s["artistId"] for s in songs} == {f"sh.ar.demo.{BAND_ID}"}
    assert all(s["albumId"].startswith("sh.al.demo.") for s in songs)
    for song in songs:  # every artist ID opens
        assert client.ok("getArtist", {"id": song["artistId"]})["artist"]["name"] == BAND
    assert [s["id"] for s in top(client, artist="  brokenmeadow ")] == TOP_IDS  # the name folded
    assert asked(replay, BAND_ID) == 1  # the catalog asked once
    lines = [line for line in search_log if line.startswith("getTopSongs ")]
    assert lines and not any("brokenmeadow" in line.lower() for line in lines)  # no names


def test_the_same_songs_in_xml(client: SubsonicClient) -> None:
    params = {"artist": BAND, "count": 3}
    as_json = client.request("getTopSongs", params, fmt="json").json()["subsonic-response"]
    as_xml = client.request("getTopSongs", params, fmt="xml")
    assert as_xml.headers["content-type"] == "application/xml" and xsd.valid(as_xml.content)
    assert same_data(as_json, ET.fromstring(as_xml.content)) == []  # noqa: S314 - test data
    assert as_xml.content.count(b"<song ") == 3


def test_a_library_artist_the_catalog_has_no_list_for_gets_navidrome_s_answer(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """The catalog is asked for a library artist's top songs too; without a list
    there, the answer is Navidrome's own, byte for byte - and nothing is asked again."""
    for name in ("Oren Garrow", "Guest Voice"):  # the album's artist, and a guest on a song
        for fmt in ("json", "xml"):
            ours = client.request("getTopSongs", {"artist": name}, fmt=fmt)  # type: ignore[arg-type]
            assert ours.content == navidrome_s(world, {"artist": name}, fmt), (name, fmt)
    before = replay.api_requests
    for name in ("Oren Garrow", "Guest Voice"):
        ours = client.request("getTopSongs", {"artist": name})
        assert ours.content == navidrome_s(world, {"artist": name})
    assert replay.api_requests == before, replay.log[before:]  # the empty lists are saved


def native_artist(world: DeliveryWorld, name: str) -> str:
    found = world.nd.client().ok("search3", {"query": name, "albumCount": 0, "songCount": 0})
    return str(next(a["id"] for a in found["searchResult3"]["artist"] if a["name"] == name))


def test_by_a_catalog_artist_s_id(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """Navidrome 0.64.2 takes an artist's ID too: a catalog artist's is that artist, or -
    its name one the library has, a guest's too - the library artist's."""
    assert [s["id"] for s in top(client, id=f"sh.ar.demo.{BAND_ID}", count=2)] == TOP_IDS[:2]
    # The catalog's Oren Garrow: the library artist's top songs (a star makes some).
    native = native_artist(world, "Oren Garrow")
    album = world.nd.client().ok("getArtist", {"id": native})["artist"]["album"][0]["id"]
    played = world.nd.client().ok("getAlbum", {"id": album})["album"]["song"][0]["id"]
    world.nd.client().ok("star", {"id": played})
    own = navidrome_s(world, {"id": native})
    assert b'"song":[' in own  # not empty: an unmapped ID would get Navidrome's empty answer
    duo = f"sh.ar.demo.{item('artist-duo')}"
    assert client.request("getTopSongs", {"id": duo}).content == own
    # A guest on an owned song, the catalog's by its ID: the library's answer (the
    # catalog has no list for it).
    guest = item("artist-composer")
    answer = client.request("getTopSongs", {"id": f"sh.ar.demo.{guest}"}).content
    assert answer == navidrome_s(world, {"id": native_artist(world, "Uma Okafor")})
    assert asked(replay, guest) <= 1


def test_top_songs_are_saved_like_discographies(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """Saved under the name asked for (the artist not shown: after a restart too), and a
    saved list answers when the catalog cannot."""
    key = "demo.xx:top:name:brokenmeadow"
    additions = world.services.additions
    assert additions is not None
    shown, additions.shown = additions.shown, ShownArtists()  # as after a restart
    try:
        saved_and_refreshed(world, client, replay, key)
    finally:
        additions.shown = shown


def saved_and_refreshed(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, key: str
) -> None:
    songs = top(client, artist=BAND)
    assert saved_at(world, key) is not None
    cache = world.services.catalog
    assert cache is not None
    for lookup in (cache.top_songs, cache.search):  # type: ignore[attr-defined]
        lookup.cache_clear()
    before = replay.api_requests
    replay.failing.update({"search": 503, f"artists/{BAND_ID}/top-songs": 503})
    try:
        assert top(client, artist=BAND) == songs
    finally:
        replay.failing.clear()
    assert replay.api_requests == before  # the songs saved, nothing asked

    async def age() -> None:
        await world.services.store.execute(
            "UPDATE discographies SET fetched_at = 0 WHERE key = ?", [key]
        )

    world.server.call(age)
    replay.slow[f"artists/{BAND_ID}/top-songs"] = 2.0
    try:
        started = time.monotonic()
        assert top(client, artist=BAND) == songs
        assert time.monotonic() - started < 1.5  # the old list at once
        deadline = time.monotonic() + 5
        while (saved_at(world, key) or 0) == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        replay.slow.clear()
    assert (saved_at(world, key) or 0) > 0  # refreshed in the background


def test_nothing_is_asked_while_a_client_walks_pages_or_searches_in_a_burst(
    world: DeliveryWorld, replay: Replay
) -> None:
    additions = world.services.additions
    assert additions is not None
    previous = additions.top_sync, additions.search_guard
    additions.top_sync = Bursts(3, 30.0)
    walker = world.client(headers=HOST, client="walker")
    try:
        saved = top(world.client(headers=HOST), artist=BAND)  # (another client: saved)
        for name in OTHERS:  # no top songs in the catalog: each asks it, until the third
            assert top(walker, artist=name) == []
        assert [asked(replay, artist_of(n)) for n in OTHERS] == [1, 1, 0]
        assert top(walker, artist=BAND) == saved  # a saved list still answers
        # A client searching in a burst: nothing asked, not even the name's search.
        additions.search_guard = Bursts(2, 30.0)
        burster = world.client(headers=HOST, client="burster")
        for term in ("first search", "second search"):
            burster.ok("search3", {"query": term})
        before = replay.api_requests
        assert top(burster, artist=UNSHOWN[0]) == []
        assert replay.api_requests == before
    finally:
        additions.top_sync, additions.search_guard = previous
        walker.close()
    # A client walking artist pages: the same.
    pages = Bursts(2, 30.0)
    for page in ("a", "b"):
        pages.note((ADMIN_USER, "pager"), page)
    previous_pages, additions.artist_sync = additions.artist_sync, pages
    try:
        before = replay.api_requests
        assert top(world.client(headers=HOST, client="pager"), artist=UNSHOWN[1]) == []
        assert replay.api_requests == before
    finally:
        additions.artist_sync = previous_pages


def test_a_failing_or_slow_catalog_leaves_navidrome_s_empty_answer(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    additions = world.services.additions
    assert additions is not None
    replay.failing[f"artists/{artist_of(FAILING)}/top-songs"] = 503
    try:
        for fmt in ("json", "xml"):
            ours = client.request("getTopSongs", {"artist": FAILING}, fmt=fmt)  # type: ignore[arg-type]
            assert ours.content == navidrome_s(world, {"artist": FAILING}, fmt)
    finally:
        replay.failing.clear()
    assert asked(replay, artist_of(FAILING)) == 1  # (asked once: failures are not saved)
    additions.resting_until = 0.0  # the failure rested the catalog lookups
    budget, additions.budget = additions.budget, 0.5
    slow = "Wren Norrell"  # not looked up yet
    replay.slow[f"artists/{artist_of(slow)}/top-songs"] = 2.0
    try:
        started = time.monotonic()
        answer = client.request("getTopSongs", {"artist": slow})
        took = time.monotonic() - started
    finally:
        replay.slow.clear()
        additions.budget = budget
    assert took < 1.5 and answer.content == navidrome_s(world, {"artist": slow})


@pytest.fixture
def search_log() -> Iterator[list[str]]:
    with collected("shijhon.views.additions") as lines:
        yield lines


def test_the_artist_a_search_showed_is_the_one_named(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """A name a search result showed is that artist: no catalog search for it, and its
    songs saved under the artist."""
    found = client.ok("search3", {"query": "brokenmeadow"})["searchResult3"]
    assert any(a["name"] == BAND for a in found.get("artist", []))
    searches = replay.log.count(f"search?term={fixture('search-anniversary')['params']['term']}")
    assert [s["id"] for s in top(client, artist=BAND)] == TOP_IDS
    assert saved_at(world, f"demo.xx:top:artist:demo:{BAND_ID}") is not None
    after = replay.log.count(f"search?term={fixture('search-anniversary')['params']['term']}")
    assert after == searches


def test_a_library_artist_s_catalog_id_is_mapped_also_during_a_burst(
    world: DeliveryWorld, replay: Replay
) -> None:
    """While the client is quiet (searching in a burst), top songs ask nothing, and a
    catalog ID is mapped as in any method."""
    additions = world.services.additions
    assert additions is not None
    native = native_artist(world, "Oren Garrow")
    album = world.nd.client().ok("getArtist", {"id": native})["artist"]["album"][0]["id"]
    song = world.nd.client().ok("getAlbum", {"id": album})["album"]["song"][0]["id"]
    world.nd.client().ok("star", {"id": song})  # (Navidrome's own top songs: not empty)
    guard = Bursts(2, 30.0)
    for term in ("a", "b"):
        guard.note((ADMIN_USER, "quiet"), term)
    previous, additions.search_guard = additions.search_guard, guard
    try:
        quiet = world.client(headers=HOST, client="quiet")
        duo = f"sh.ar.demo.{item('artist-duo')}"
        answer = quiet.request("getTopSongs", {"id": duo}).content
    finally:
        additions.search_guard = previous
    assert answer == navidrome_s(world, {"id": native}) and b'"song":[' in answer


def test_after_a_restart_saved_lists_answer_by_name_and_by_id(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """Saved lists answer without the catalog once the memory of shown artists is gone:
    under the name asked for, and under the artist (its name from its own songs)."""
    by_id = {"id": f"sh.ar.demo.{BAND_ID}"}
    songs = top(client, **by_id)
    assert saved_at(world, f"demo.xx:top:artist:demo:{BAND_ID}") is not None
    additions, cache = world.services.additions, world.services.catalog
    assert additions is not None and cache is not None
    shown, additions.shown = additions.shown, ShownArtists()
    for lookup in (cache.top_songs, cache.artist, cache.search):  # type: ignore[attr-defined]
        lookup.cache_clear()
    failing = {"search": 503, f"artists/{BAND_ID}": 503, f"artists/{BAND_ID}/top-songs": 503}
    replay.failing.update(failing)
    try:
        before = replay.api_requests
        assert top(client, **by_id) == songs
        assert replay.api_requests == before
    finally:
        replay.failing.clear()
        additions.shown = shown


def test_names_that_stand_for_no_one_artist_and_no_library_writes(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    before = replay.api_requests
    ours = client.request("getTopSongs", {"artist": "Various Artists"}).content
    assert ours == navidrome_s(world, {"artist": "Various Artists"})
    assert replay.api_requests == before
    assert world.placeholder_rows() == 0 and world.placeholder_files() == []  # views only


# --- an artist the library has songs of ------------------------------------------------

TOP_ROWS = TOP["body"]
# The library's own songs by the catalog's band: one of its top songs by its ISRC, one by
# its title and length, one of the same title but another length, and one the catalog's
# list does not have.
MEADOW = Album(
    BAND,
    "Owned Meadow",
    (
        Track("Frozen Rivers (Live at Home)", 1, isrc=TOP_ROWS[1]["isrc"]),
        Track("Distant", 2, seconds=round(TOP_ROWS[2]["duration_ms"] / 1000)),
        Track("Wild Cities", 3),
        Track("Something Else", 4),
    ),
)
SALT = "album-anniversary-standard"  # the catalog album of six of the ten top songs


@pytest.fixture(scope="module")
def mixed(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, Replay]]:
    replay = Replay()
    replay.aliases["brokenmeadow"] = str(fixture("search-anniversary")["params"]["term"])
    nd = navidrome_factory()
    write_album(nd.music, MEADOW)
    nd.scan(full=True)
    folder = tmp_path_factory.mktemp("top-mixed")
    with delivery_world(nd, folder, catalog=replay.catalog()) as w:
        yield w, replay


def owned(world: DeliveryWorld) -> dict[str, dict[str, Any]]:
    """The library's songs by title, as the test client's user sees them."""
    found = world.nd.client(headers=HOST, client="Toplister").ok(
        "search3", {"query": "", "songCount": 500, "artistCount": 0, "albumCount": 0}
    )
    return {str(song["title"]): song for song in found["searchResult3"].get("song", [])}


def test_a_library_artist_s_top_songs_are_the_catalog_s_with_the_library_s_entries(
    mixed: tuple[DeliveryWorld, Replay],
) -> None:
    world, replay = mixed
    client = world.client(headers=HOST, client="Toplister")
    mine = owned(world)
    # Navidrome itself has none for the artist: nothing starred.
    assert b'"topSongs":{}' in navidrome_s(world, {"artist": BAND})
    with collected("shijhon.views.additions") as lines:
        songs = top(client, artist=BAND)
    expected = list(TOP_IDS)
    expected[1] = mine["Frozen Rivers (Live at Home)"]["id"]  # the same recording (its ISRC)
    expected[2] = mine["Distant"]["id"]  # the same title, artist and length
    assert [s["id"] for s in songs] == expected  # the catalog's order; "Wild Cities" its own
    assert songs[1] == mine["Frozen Rivers (Live at Home)"] and songs[2] == mine["Distant"]
    assert check("song", songs) == (2, 8)  # the library's two, eight catalog songs
    line = next(line for line in lines if line.startswith("getTopSongs "))
    assert "in the library: yes" in line and "10 of the catalog's songs added (2 as" in line
    assert BAND.lower() not in line.lower()
    # A view: the next one asks neither the catalog nor Navidrome for the songs again.
    asked_before, searched = replay.api_requests, world.services.additions.library_songs.searches  # type: ignore[union-attr]
    assert [s["id"] for s in top(client, artist=BAND, count=3)] == expected[:3]
    assert replay.api_requests == asked_before
    assert world.services.additions.library_songs.searches == searched  # type: ignore[union-attr]
    # By the catalog artist's ID, and by the library artist's own: the same list.
    assert [s["id"] for s in top(client, id=f"sh.ar.demo.{BAND_ID}")] == expected
    assert [s["id"] for s in top(client, id=native_artist(world, BAND))] == expected
    unknown = client.request("getTopSongs", {"id": "no-such-artist"})  # Navidrome's answer
    assert unknown.content == navidrome_s(world, {"id": "no-such-artist"})
    # Only as many songs as the answer takes are asked about.
    library_songs = world.services.additions.library_songs  # type: ignore[union-attr]
    library_songs._found.clear()
    searched = library_songs.searches
    assert [s["id"] for s in top(client, artist=BAND, count=2)] == expected[:2]
    assert library_songs.searches - searched == 2
    # The same in XML: valid, the same data, the library's entries as Navidrome writes them.
    as_json = client.request("getTopSongs", {"artist": BAND}).json()["subsonic-response"]
    as_xml = client.request("getTopSongs", {"artist": BAND}, fmt="xml")
    assert xsd.valid(as_xml.content)
    assert same_data(as_json, ET.fromstring(as_xml.content)) == []  # noqa: S314 - test data
    direct = world.nd.client(headers=HOST, client="Toplister")
    own_xml = direct.request("getSong", {"id": expected[2]}, fmt="xml").text
    element = own_xml[own_xml.index("<song ") : own_xml.index("</subsonic-response>")]
    assert element in as_xml.text
    # No library writes: a view.
    assert world.placeholder_rows() == 0


def test_navidrome_s_own_top_songs_stay_first_and_the_catalog_s_follow(
    mixed: tuple[DeliveryWorld, Replay],
) -> None:
    """Navidrome's own answer (here: the artist's favorites, its local ranking) is kept as
    it gives it; the catalog's top songs follow, none twice, up to the count."""
    world, _ = mixed
    client = world.client(headers=HOST, client="Toplister")
    mine = owned(world)
    direct = world.nd.client(headers=HOST, client="Toplister")
    for title in ("Something Else", "Distant"):
        direct.ok("star", {"id": mine[title]["id"]})
    try:
        own = direct.ok("getTopSongs", {"artist": BAND})["topSongs"]["song"]
        assert {s["id"] for s in own} == {mine["Something Else"]["id"], mine["Distant"]["id"]}
        songs = top(client, artist=BAND)
        assert songs[:2] == own  # as Navidrome gives them, starred
        rest = [s["id"] for s in songs[2:]]
        assert rest == [TOP_IDS[0], mine["Frozen Rivers (Live at Home)"]["id"], *TOP_IDS[3:]]
        assert len({s["id"] for s in songs}) == len(songs) == 11
        assert [s["id"] for s in top(client, artist=BAND, count=3)] == [
            *(s["id"] for s in own),
            TOP_IDS[0],
        ]
        # Navidrome's own fill the count: its answer, byte for byte.
        for fmt in ("json", "xml"):
            ours = client.request("getTopSongs", {"artist": BAND, "count": 2}, fmt=fmt)  # type: ignore[arg-type]
            assert ours.content == navidrome_s(world, {"artist": BAND, "count": 2}, fmt)
        as_xml = client.request("getTopSongs", {"artist": BAND}, fmt="xml")
        assert xsd.valid(as_xml.content) and as_xml.content.count(b"<song ") == 11
        own_xml = direct.request("getTopSongs", {"artist": BAND}, fmt="xml").text
        start = own_xml.index("<song ")
        assert own_xml[start : own_xml.rindex("</song>")] in as_xml.text  # its elements, kept
    finally:
        for title in ("Something Else", "Distant"):
            direct.ok("unstar", {"id": mine[title]["id"]})


def test_a_failing_catalog_or_navidrome_leaves_navidrome_s_answer(
    mixed: tuple[DeliveryWorld, Replay],
) -> None:
    world, replay = mixed
    client = world.client(headers=HOST, client="Toplister")
    additions = world.services.additions
    assert additions is not None and additions.discographies is not None
    assert len(top(client, artist=BAND)) == 10  # (saved by now)

    async def forget() -> None:
        await world.services.store.execute("DELETE FROM discographies WHERE key LIKE '%:top:%'")

    # The library's songs cannot be told (Navidrome's search failing): its own answer.
    searching = additions.library_songs._by_title

    async def failing(track: object) -> str | None:
        raise NavidromeError("unavailable")

    additions.library_songs._found.clear()
    additions.library_songs._by_title = failing  # type: ignore[method-assign]
    try:
        ours = client.request("getTopSongs", {"artist": BAND})
        assert ours.content == navidrome_s(world, {"artist": BAND})
    finally:
        additions.library_songs._by_title = searching  # type: ignore[method-assign]
    assert len(top(client, artist=BAND)) == 10
    # ... or its own entry for a song cannot be had: never that song as a catalog song.
    reading = additions.library_songs.entries

    async def unanswered(call: object, songs: object) -> dict[str, dict[str, Any]]:
        raise Unknown("1 song(s) not answered")

    additions.library_songs.entries = unanswered  # type: ignore[method-assign]
    try:
        ours = client.request("getTopSongs", {"artist": BAND})
        assert ours.content == navidrome_s(world, {"artist": BAND})
    finally:
        additions.library_songs.entries = reading  # type: ignore[method-assign]
    # A song Navidrome does not give this caller is left out, not shown as the catalog's.
    mine = owned(world)
    hidden = mine["Distant"]["id"]

    async def without(call: Any, songs: Any) -> dict[str, dict[str, Any]]:
        found = await reading(call, [song for song in songs if song != hidden])
        return found

    additions.library_songs.entries = without  # type: ignore[method-assign]
    try:
        ids = [s["id"] for s in top(client, artist=BAND)]
    finally:
        additions.library_songs.entries = reading  # type: ignore[method-assign]
    assert hidden not in ids and TOP_IDS[2] not in ids and len(ids) == 9
    # The catalog failing, nothing saved: Navidrome's own answer.
    world.server.call(forget)
    shown, additions.shown = additions.shown, ShownArtists()
    cache = world.services.catalog
    assert cache is not None
    for lookup in (cache.top_songs, cache.artist, cache.search):  # type: ignore[attr-defined]
        lookup.cache_clear()
    replay.failing.update({"search": 503, f"artists/{BAND_ID}/top-songs": 503})
    try:
        for fmt in ("json", "xml"):
            ours = client.request("getTopSongs", {"artist": BAND}, fmt=fmt)  # type: ignore[arg-type]
            assert ours.content == navidrome_s(world, {"artist": BAND}, fmt)
    finally:
        replay.failing.clear()
        additions.shown = shown
        additions.resting_until = 0.0


def test_a_played_song_is_the_library_s_entry_and_the_list_stays(
    mixed: tuple[DeliveryWorld, Replay],
) -> None:
    """A play commits the song's album (placeholders), and
    the artist's top songs stay the catalog's - the songs now in the library as the
    library's own entries, the others as catalog songs."""
    world, _ = mixed
    client = world.client(headers=HOST, client="Toplister")
    before = [s["id"] for s in top(client, artist=BAND)]
    assert before[0] == TOP_IDS[0]
    client.ok("scrobble", {"id": TOP_IDS[0], "submission": "false"})  # "now playing": commits
    assert world.placeholder_rows() > 0
    songs = top(client, artist=BAND)
    assert [s["title"] for s in songs] == [row["title"] for row in TOP_ROWS]  # the same list
    on_album = {row["ref"]["id"] for row in fixture(SALT)["body"]["tracks"]}
    for song, row in zip(songs, TOP_ROWS, strict=True):
        if row["ref"]["id"] in on_album:
            assert "." not in song["id"], song["title"]  # the library's own song
            assert client.ok("getSong", {"id": song["id"]})["song"] == song
        else:
            assert song["id"] == f"sh.tr.demo.{row['ref']['id']}"
    assert len({s["id"] for s in songs}) == 10
    as_xml = client.request("getTopSongs", {"artist": BAND}, fmt="xml")
    as_json = client.request("getTopSongs", {"artist": BAND}).json()["subsonic-response"]
    assert xsd.valid(as_xml.content)
    assert same_data(as_json, ET.fromstring(as_xml.content)) == []  # noqa: S314 - test data
    # The same recording is there once: the owned song Navidrome itself lists (a
    # favorite), not the placeholder of the committed album that plays it as well.
    mine = owned(world)["Frozen Rivers (Live at Home)"]
    direct = world.nd.client(headers=HOST, client="Toplister")
    direct.ok("star", {"id": mine["id"]})
    try:
        starred = top(client, artist=BAND)
        assert starred[0]["id"] == mine["id"] and len(starred) == 10
        assert [s["title"] for s in starred].count("Frozen Rivers") == 0
        assert len({s["id"] for s in starred}) == 10
    finally:
        direct.ok("unstar", {"id": mine["id"]})
