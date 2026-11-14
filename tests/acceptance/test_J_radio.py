"""Suite J (continued) - radio from catalog items: similar songs and similar artists.

getSimilarSongs and getSimilarSongs2 of a catalog song, album or artist that is not in
the library, and the similarArtist list of a catalog artist's getArtistInfo(2), in JSON
and XML. Last.fm (a stand-in here) says what is similar, asked with the Last.fm key the
caller connected for discovery mixes: an artist and a title, no account. A song the
library has is the library's own entry, the others are catalog songs like search results;
none twice, up to the count. It is a view: lists are saved like discographies (an old one
answers at once and is refreshed in the background), a radio's wait is bounded (the other
views' budget) and ends with what is known so far, an artist's info does not wait for its
similar artists at all, nothing is asked while a client walks pages, and whenever nothing
can be shown - no key, the setting off, Last.fm or the catalog failing - the answer is
the one from before: Navidrome's own empty answer. What Navidrome answers about items it
has stays as it is.
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from shijhon.catalog.model import release_from_data
from shijhon.playlists.model import Connection
from shijhon.views import radio as radio_module
from shijhon.views.bursts import Bursts
from shijhon.views.radio import CatalogRadio
from tests.acceptance.test_J_contract import check
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.collection import prepare_collection
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_lastfm import KEY, FakeLastFm
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient
from tests.harness.xsd import same_data

HOST = {"host": "music.test"}
METHODS = (("getSimilarSongs", "similarSongs"), ("getSimilarSongs2", "similarSongs2"))


def item(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def songs_of(search: str, artist: str) -> dict[str, dict[str, Any]]:
    """The songs a recorded search has of an artist, by title (the first of each)."""
    found: dict[str, dict[str, Any]] = {}
    for song in fixture(search)["body"]["songs"]:
        if song["artist"] == artist:
            found.setdefault(song["title"], song)
    return found


def sid(song: dict[str, Any]) -> str:
    return f"sh.tr.demo.{song['ref']['id']}"


TERMS = {
    name: str(fixture(name)["params"]["term"])
    for name in ("search-anniversary", "search-ep", "search-covers", "search-soundtrack")
}
BAND, BAND_ID = "BrokenMeadow", item("artist-band")
BAND_SONGS = songs_of("search-anniversary", BAND)
FAIRBANKS = songs_of("search-ep", "Jonah Fairbanks")
PRESCOTT = songs_of("search-covers", "Silas Prescott")
OKAFOR, OKAFOR_ID = "Uma Okafor", item("artist-composer")
OKAFOR_SONGS = songs_of("search-soundtrack", OKAFOR)
SOUNDTRACK = fixture("album-soundtrack-expanded")["body"]  # in the library: placeholders
PLACED = {t["ref"]["id"] for t in SOUNDTRACK["tracks"]}
# The library owns this recording of the catalog's (the same ISRC).
OWNED_SONG = FAIRBANKS["Golden Rivers"]
OWNED = Album(
    "Jonah Fairbanks", "Own Rivers", (Track("Golden Rivers", 1, isrc=OWNED_SONG["isrc"]),)
)
# A song's radio starts here: a duo's song whose title names a featured artist.
SEED = fixture("song-duo-feat")["body"]
SEED_ID = f"sh.tr.demo.{SEED['ref']['id']}"
DUO, FIRST = SEED["artist"], "ÉMILE BRØNN 2"  # the credit, and its first artist
FIRST_ID = item("artist-diacritic")
# What Last.fm lists as similar to it, the most similar first.
BAND_TITLES = (
    "Distant",
    "Frozen Rivers",
    "Frozen Shadows",
    "Open 3",
    "Iron Parade",
    "Copper 2",
    "Velvet Letters",
    "Golden Colors",
)
A_PLACED = next(s for s in OKAFOR_SONGS.values() if s["ref"]["id"] in PLACED)
SIMILAR = [
    (BAND, "Burning Engines"),
    ("Jonah Fairbanks", "Golden Rivers"),  # the library owns it
    ("Nobody Known", "No Such Song"),  # not in the catalog
    (OKAFOR, A_PLACED["title"]),  # a placeholder in the library
    ("Silas Prescott", "Glass Stones"),
    (BAND, "Burning Engines (Remastered)"),  # the first one's recording again
    *((BAND, title) for title in BAND_TITLES),
]
EXPECTED = [
    sid(BAND_SONGS["Burning Engines"]),
    "owned",
    "placed",
    sid(PRESCOTT["Glass Stones"]),
    *(sid(BAND_SONGS[title]) for title in BAND_TITLES),
]
# Another song, of which Last.fm lists little: its artist's radio fills it up.
THIN = fixture("song-feat-album-version")["body"]
THIN_ID = f"sh.tr.demo.{THIN['ref']['id']}"
VERSIONS = "Open Lanterns (Faded 3)"  # three recordings of it, each with its own ISRC
OKAFOR_TOP = list(OKAFOR_SONGS.values())[:7]


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    for artist, title in SIMILAR:
        search = {
            BAND: "search-anniversary",
            "Jonah Fairbanks": "search-ep",
            OKAFOR: "search-soundtrack",
            "Silas Prescott": "search-covers",
        }.get(artist)
        if search:
            replay.aliases[f"{artist} {title}".lower()] = TERMS[search]
    replay.aliases[BAND.lower()] = TERMS["search-anniversary"]
    versions = str(fixture("search-single-vs-album")["params"]["term"])
    replay.aliases[f"{THIN['artist']} {VERSIONS}".lower()] = versions
    replay.aliases[FIRST.lower()] = str(fixture("search-duo")["params"]["term"])
    replay.aliases[OKAFOR.lower()] = TERMS["search-soundtrack"]
    # The catalog's top songs of an artist the library has too.
    path = f"artists/{OKAFOR_ID}/top-songs"
    replay.by_path[path] = {"path": path, "body": OKAFOR_TOP}
    return replay


@pytest.fixture(scope="module")
def lastfm() -> FakeLastFm:
    lastfm = FakeLastFm()
    # Last.fm knows the song without its title's "(feat. ...)", by its first artist.
    lastfm.tracks[(FIRST, "Amber 2")] = SIMILAR
    # ... a song the catalog has several recordings of under one title: the original.
    lastfm.tracks[(THIN["artist"], THIN["title"])] = [
        ("Silas Prescott", "Glass Stones"),
        (THIN["artist"], VERSIONS),
    ]
    lastfm.artists[BAND] = [OKAFOR, "Nobody Known", BAND, "Various Artists"]
    lastfm.artists[FIRST] = [OKAFOR, FIRST, BAND, "Nobody Known", "Various Artists"]
    lastfm.artists["Oren Garrow"] = [BAND]
    return lastfm


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
    lastfm: FakeLastFm,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    nd.scan(full=True)
    nd.create_user("listener", "invented-listener-password")  # connects no Last.fm key
    with delivery_world(nd, tmp_path_factory.mktemp("radio"), catalog=replay.catalog()) as w:
        w.materialize(release_from_data(SOUNDTRACK))
        nd.scan()
        prepare_collection(nd, w.server)
        playlists = w.services.playlists
        assert playlists is not None
        users = nd.native("GET", "user").json()
        admin = next(u["id"] for u in users if u["userName"] == ADMIN_USER)

        async def connect() -> None:
            await playlists.repo.connect(
                Connection(
                    user_id=admin,
                    username=ADMIN_USER,
                    token=SecretStr("invented-token"),
                    salt=SecretStr("invented-salt"),
                    history_key=SecretStr(KEY),
                )
            )
            await playlists.history.http.aclose()
            playlists.history.http = httpx.AsyncClient(transport=lastfm.transport())

        w.server.call(connect)
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client(headers=HOST, client="Radio")
    yield c
    c.close()


@pytest.fixture
def radio(world: DeliveryWorld) -> CatalogRadio:
    radio = world.services.radio
    assert radio is not None
    radio.resting_until = 0.0
    return radio


@pytest.fixture
def radio_log() -> Iterator[list[str]]:
    with collected("shijhon.views.radio") as lines:
        yield lines


def similar(client: SubsonicClient, method: str = "getSimilarSongs2", **params: Any) -> list[Any]:
    element = dict(METHODS)[method]
    found: list[Any] = client.ok(method, params)[element].get("song", [])
    return found


def empty(world: DeliveryWorld, method: str, fmt: str = "json") -> bytes:
    """Navidrome's own empty answer: its owned song has no similar songs either."""
    direct = world.nd.client(headers=HOST, client="Radio")
    return direct.request(method, {"id": native_song(world)}, fmt=fmt).content  # type: ignore[arg-type]


def native_song(world: DeliveryWorld) -> str:
    found = world.nd.client().ok("search3", {"query": "Golden Rivers", "artistCount": 0})
    songs = [s for s in found["searchResult3"]["song"] if s["artist"] == "Jonah Fairbanks"]
    return str(songs[0]["id"])


def placed_song(world: DeliveryWorld) -> str:
    async def read() -> str:
        row = await world.services.store.fetchone(
            "SELECT song_id FROM track_links WHERE track_ref = ?",
            [f"demo:{A_PLACED['ref']['id']}"],
        )
        assert row is not None
        return str(row["song_id"])

    return world.server.call(read)


def saved_at(world: DeliveryWorld, key: str) -> float | None:
    async def read() -> float | None:
        row = await world.services.store.fetchone(
            "SELECT fetched_at FROM discographies WHERE key = ?", [key]
        )
        return float(row["fetched_at"]) if row else None

    return world.server.call(read)


def saved_refs(world: DeliveryWorld, key: str) -> list[str]:
    async def read() -> list[str]:
        row = await world.services.store.fetchone(
            "SELECT releases FROM discographies WHERE key = ?", [key]
        )
        assert row is not None
        return [str(song["ref"]["id"]) for song in json.loads(row["releases"])]

    return world.server.call(read)


def forget(world: DeliveryWorld, *keys: str) -> None:
    """The lists as never saved, and the catalog's answers as never cached."""

    async def delete() -> None:
        for key in keys:
            await world.services.store.execute("DELETE FROM discographies WHERE key = ?", [key])

    world.server.call(delete)
    clear_cache(world)


def clear_cache(world: DeliveryWorld) -> None:
    cache = world.services.catalog
    assert cache is not None
    for lookup in (cache.top_songs, cache.search):  # type: ignore[attr-defined]
        lookup.cache_clear()


def until(done: Callable[[], bool], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        time.sleep(0.05)
    return done()


SONG_KEY = f"demo.xx:radio:song:demo:{SEED['ref']['id']}"
BAND_KEY = "demo.xx:radio:artist:brokenmeadow"
BAND_SIMILAR_KEY = "demo.xx:radio:similar:brokenmeadow"


# --- a song's radio ---------------------------------------------------------------------


def test_a_catalog_song_s_radio_is_what_lastfm_lists_as_the_library_and_the_catalog_have_it(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm, radio_log: list[str]
) -> None:
    songs = similar(client, id=SEED_ID)
    expected = [
        {"owned": native_song(world), "placed": placed_song(world)}.get(e, e) for e in EXPECTED
    ]
    # Last.fm's order; the song not in the catalog and the recording listed twice left out.
    assert [s["id"] for s in songs] == expected
    assert check("song", songs) == (2, len(expected) - 2)  # Navidrome's keys and types
    # A song the library has is the library's own entry for the caller.
    for native in (native_song(world), placed_song(world)):
        own = client.ok("getSong", {"id": native})["song"]
        assert next(s for s in songs if s["id"] == native) == own
    # A catalog song is the entry a search shows, and it opens.
    first = songs[0]
    search = client.ok("search3", {"query": TERMS["search-anniversary"], "songCount": 50})
    shown = next(s for s in search["searchResult3"]["song"] if s["id"] == first["id"])
    assert first == shown
    assert client.ok("getSong", {"id": first["id"]})["song"]["title"] == "Burning Engines"
    assert client.ok("getArtist", {"id": first["artistId"]})["artist"]["name"] == BAND
    # Last.fm was asked for the song as the catalog names it first, then without its
    # title's qualifiers and by its first artist; it got names and the key, no account.
    asked = lastfm.asked("track.getSimilar")
    assert [(r["artist"], r["track"]) for r in asked] == [
        (DUO, SEED["title"]),
        (DUO, "Amber 2"),
        (FIRST, SEED["title"]),
        (FIRST, "Amber 2"),
    ]
    for request in lastfm.requests:
        assert request["api_key"] == KEY
        assert set(request) <= {"api_key", "method", "format", "autocorrect", "artist", "track",
                                "limit"}  # fmt: skip
    # The saved list holds each recording once, and never the song itself.
    refs = saved_refs(world, SONG_KEY)
    assert len(refs) == len(expected) and len(set(refs)) == len(refs)
    assert SEED["ref"]["id"] not in refs
    # The log tells of the request without a name of it.
    lines = [line for line in radio_log if line.startswith("getSimilarSongs2 ")]
    assert lines and "a catalog song" in lines[0]
    logged = " ".join(radio_log).lower()
    assert not any(name.lower() in logged for name in (BAND, "amber", "émile", "prescott"))


def test_the_count_and_both_methods(client: SubsonicClient) -> None:
    whole = [s["id"] for s in similar(client, id=SEED_ID)]
    assert [s["id"] for s in similar(client, id=SEED_ID, count=3)] == whole[:3]
    assert [s["id"] for s in similar(client, "getSimilarSongs", id=SEED_ID, count=5)] == whole[:5]
    assert len(similar(client, id=SEED_ID, count=500)) == len(whole)  # no more than there are
    assert [s["id"] for s in similar(client, id=SEED_ID, count="many")] == whole  # the default


@pytest.mark.parametrize(("method", "element"), METHODS)
def test_the_same_songs_in_xml(client: SubsonicClient, method: str, element: str) -> None:
    params = {"id": SEED_ID, "count": 4}
    as_json = client.request(method, params, fmt="json").json()["subsonic-response"]
    as_xml = client.request(method, params, fmt="xml")
    assert as_xml.headers["content-type"] == "application/xml" and xsd.valid(as_xml.content)
    assert same_data(as_json, ET.fromstring(as_xml.content)) == []  # noqa: S314 - test data
    assert as_xml.content.count(b"<song ") == 4 and f"<{element}>".encode() in as_xml.content


def test_a_list_is_saved_and_answers_without_lastfm_or_the_catalog(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, lastfm: FakeLastFm
) -> None:
    songs = similar(client, id=SEED_ID)
    assert songs and saved_at(world, SONG_KEY) is not None
    clear_cache(world)
    before, sent = replay.api_requests, len(lastfm.requests)
    replay.failing["search"], lastfm.status = 503, 503
    try:
        assert similar(client, id=SEED_ID) == songs
        time.sleep(0.5)  # (a refresh behind the answer would have asked by now)
    finally:
        replay.failing.clear()
        lastfm.status = 200
    # (The seed itself is the catalog's cached song: nothing asked at all.)
    assert replay.api_requests == before and len(lastfm.requests) == sent

    async def age() -> None:
        await world.services.store.execute(
            "UPDATE discographies SET fetched_at = 0 WHERE key = ?", [SONG_KEY]
        )

    world.server.call(age)
    lastfm.delay = 1.0
    try:
        started = time.monotonic()
        assert similar(client, id=SEED_ID) == songs
        assert time.monotonic() - started < 0.8  # the old list at once
        assert until(lambda: (saved_at(world, SONG_KEY) or 0) > 0, 10)  # refreshed behind it
    finally:
        lastfm.delay = 0.0
    assert len(lastfm.requests) > sent


# --- when nothing can be shown: the answer from before ----------------------------------


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_without_a_key_or_with_the_setting_off_the_answer_is_navidrome_s_empty_one(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm, radio: CatalogRadio, fmt: str
) -> None:
    sent = len(lastfm.requests)
    listener = SubsonicClient(
        world.server.base_url, "listener", "invented-listener-password", headers=HOST,
        client="Radio",
    )  # fmt: skip
    try:
        for method, _ in METHODS:
            # A saved list is there, but this account connected no Last.fm key.
            ours = listener.request(method, {"id": SEED_ID}, fmt=fmt)  # type: ignore[arg-type]
            assert ours.status_code == 200 and ours.content == empty(world, method, fmt)
            radio.enabled = False
            try:
                off = client.request(method, {"id": SEED_ID}, fmt=fmt)  # type: ignore[arg-type]
            finally:
                radio.enabled = True
            assert off.content == empty(world, method, fmt)
            for ident in (SEED_ID, THIN_ID):  # (the second: no list saved yet)
                none = client.request(method, {"id": ident, "count": 0}, fmt=fmt)  # type: ignore[arg-type]
                assert none.content == empty(world, method, fmt)
    finally:
        listener.close()
    assert len(lastfm.requests) == sent


def test_a_failing_lastfm_leaves_navidrome_s_empty_answer_and_is_left_alone_for_a_while(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm, radio: CatalogRadio
) -> None:
    forget(world, SONG_KEY)
    lastfm.status = 503
    try:
        for method, _ in METHODS[:1]:
            ours = client.request(method, {"id": SEED_ID})
            assert ours.content == empty(world, method)
        assert saved_at(world, SONG_KEY) is None  # a failure is not saved
        sent = len(lastfm.requests)
        assert similar(client, id=SEED_ID) == []  # resting: not asked again
        assert len(lastfm.requests) == sent
    finally:
        lastfm.status = 200
    radio.resting_until = 0.0
    assert similar(client, id=SEED_ID)  # asked again, and saved
    assert saved_at(world, SONG_KEY) is not None


def test_a_slow_lastfm_is_waited_for_no_longer_than_the_budget(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm, radio: CatalogRadio
) -> None:
    forget(world, SONG_KEY)
    additions = world.services.additions
    assert additions is not None
    budget, additions.budget = additions.budget, 0.4
    lastfm.delay = 1.0
    try:
        started = time.monotonic()
        ours = client.request("getSimilarSongs2", {"id": SEED_ID})
        took = time.monotonic() - started
    finally:
        lastfm.delay = 0.0
        additions.budget = budget
    assert took < 1.2 and ours.content == empty(world, "getSimilarSongs2")
    # The list is made behind the answer: the next request has it.
    assert until(lambda: saved_at(world, SONG_KEY) is not None, 15)
    assert len(similar(client, id=SEED_ID)) == len(EXPECTED)


def test_a_slow_catalog_gives_what_is_known_so_far_and_the_rest_later(
    world: DeliveryWorld,
    client: SubsonicClient,
    replay: Replay,
    lastfm: FakeLastFm,
    radio: CatalogRadio,
) -> None:
    forget(world, SONG_KEY)
    additions = world.services.additions
    assert additions is not None
    budget, additions.budget = additions.budget, 2.0
    replay.slow["search"] = 0.3  # two at a time: a few of the songs within the budget
    lastfm.tracks[(DUO, SEED["title"])] = SIMILAR  # (known at once: one request)
    try:
        started = time.monotonic()
        first = similar(client, id=SEED_ID)
        took = time.monotonic() - started
    finally:
        additions.budget = budget
        del lastfm.tracks[(DUO, SEED["title"])]
    try:
        assert took < 2.5 and 0 < len(first) < len(EXPECTED)
        assert saved_at(world, SONG_KEY) is None  # still being made
        assert until(lambda: saved_at(world, SONG_KEY) is not None, 20)
    finally:
        replay.slow.clear()
    whole = similar(client, id=SEED_ID)
    assert len(whole) == len(EXPECTED)
    assert [s["id"] for s in first] == [s["id"] for s in whole][: len(first)]


def test_a_failing_catalog_leaves_navidrome_s_empty_answer(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, radio: CatalogRadio
) -> None:
    forget(world, SONG_KEY)
    replay.failing["search"] = 503
    try:
        ours = client.request("getSimilarSongs2", {"id": SEED_ID})
    finally:
        replay.failing.clear()
    assert ours.content == empty(world, "getSimilarSongs2")
    assert saved_at(world, SONG_KEY) is None
    additions = world.services.additions
    assert additions is not None and additions.resting_until == 0.0  # searches go on
    radio.resting_until = 0.0
    assert len(similar(client, id=SEED_ID)) == len(EXPECTED)


def test_jsonp_and_head_get_the_answer_from_before(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm
) -> None:
    sent = len(lastfm.requests)
    direct = world.nd.client(headers=HOST, client="Radio")
    params = {"f": "jsonp", "callback": "cb"}
    own = direct.request("getSimilarSongs2", {"id": native_song(world), **params}, fmt="xml")
    ours = client.request("getSimilarSongs2", {"id": SEED_ID, **params}, fmt="xml")
    assert ours.content == own.content and ours.content.startswith(b"cb(")
    head = client.request("getSimilarSongs2", {"id": SEED_ID}, http_method="HEAD")
    assert head.status_code == 200 and head.content == b""
    assert len(lastfm.requests) == sent


# --- an artist's and an album's radio ------------------------------------------------------


def test_an_artist_s_radio_is_its_top_songs_and_those_of_similar_artists_in_turn(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, lastfm: FakeLastFm
) -> None:
    band = [f"sh.tr.demo.{row['ref']['id']}" for row in fixture("top-songs-band")["body"]]
    theirs = [song["ref"]["id"] for song in OKAFOR_TOP[: radio_module.THEIR_SONGS]]
    songs = similar(client, id=f"sh.ar.demo.{BAND_ID}")
    ids = [s["id"] for s in songs]
    assert [i for i in ids if i in band] == band  # the artist's own, in the catalog's order
    assert len(ids) == len(band) + len(theirs) and len(set(ids)) == len(ids)
    assert ids[0] == band[0] and ids[2] == band[1]  # in turn
    # The similar artist is one the library has: its songs there are the library's own.
    natives = [s for s in songs if not s["id"].startswith("sh.")]
    placed = [ref for ref in theirs if ref in PLACED]
    assert len(natives) == len(placed) > 0
    assert {s["artist"] for s in natives} == {OKAFOR}
    assert check("song", songs) == (len(natives), len(songs) - len(natives))
    assert [r["artist"] for r in lastfm.asked("artist.getSimilar")] == [BAND]
    assert saved_at(world, BAND_KEY) is not None and saved_at(world, BAND_SIMILAR_KEY) is not None
    # An album's radio is its artist's: the saved list, nothing asked.
    before, sent = replay.api_requests, len(lastfm.requests)
    album = f"sh.al.demo.{item('album-anniversary-standard')}"
    assert [s["id"] for s in similar(client, "getSimilarSongs", id=album)] == ids
    assert len(lastfm.requests) == sent
    assert [r for r in replay.log[before:] if not r.startswith("albums/")] == []


def test_a_song_lastfm_lists_little_for_is_filled_up_with_its_artist_s_radio(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm
) -> None:
    songs = similar(client, id=THIN_ID)
    ids = [s["id"] for s in songs]
    band = [f"sh.tr.demo.{row['ref']['id']}" for row in fixture("top-songs-band")["body"]]
    assert ids[0] == sid(PRESCOTT["Glass Stones"])  # what Last.fm lists comes first
    # Of several recordings under one title, the earliest (as discovery mixes choose).
    titled = [
        s
        for s in fixture("search-single-vs-album")["body"]["songs"]
        if s["title"] == VERSIONS and s["artist"] == THIN["artist"]
    ]
    assert len({s["isrc"] for s in titled}) == len(titled) > 1
    assert ids[1] == sid(min(titled, key=lambda s: s["release_date"]))
    assert ids[2:] == band[: radio_module.THEIR_SONGS]  # then the similar artist's top songs
    assert THIN_ID not in ids


# --- similar artists ------------------------------------------------------------------------


def info(client: SubsonicClient, method: str, **params: Any) -> dict[str, Any]:
    element = "artistInfo2" if method == "getArtistInfo2" else "artistInfo"
    found: dict[str, Any] = client.ok(method, params)[element]
    return found


FIRST_SIMILAR_KEY = "demo.xx:radio:similar:emilebrønn2"


def similar_saved(world: DeliveryWorld, client: SubsonicClient) -> None:
    """The first artist's similar artists saved: its page asked for once, which answers at
    once and makes them in the background."""
    if saved_at(world, FIRST_SIMILAR_KEY) is None:
        info(client, "getArtistInfo2", id=f"sh.ar.demo.{FIRST_ID}")
        assert until(lambda: saved_at(world, FIRST_SIMILAR_KEY) is not None, 15)


def test_a_catalog_artist_s_similar_artists(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm, radio_log: list[str]
) -> None:
    forget(world, FIRST_SIMILAR_KEY)
    artist = f"sh.ar.demo.{FIRST_ID}"
    asked = len(lastfm.asked("artist.getSimilar"))
    # The first view does not wait for them; they are there from the next view on.
    assert info(client, "getArtistInfo2", id=artist)["similarArtist"] == []
    similar_saved(world, client)
    found = info(client, "getArtistInfo2", id=artist)
    similar_artists = found["similarArtist"]
    native = world.nd.client().ok("search3", {"query": OKAFOR, "albumCount": 0, "songCount": 0})
    own = next(a for a in native["searchResult3"]["artist"] if a["name"] == OKAFOR)
    # The library's artist as the library has it, the catalog's as a search shows it;
    # the name the catalog does not have and the one that names nobody are left out.
    assert [a["id"] for a in similar_artists] == [own["id"], f"sh.ar.demo.{BAND_ID}"]
    assert [a["name"] for a in similar_artists] == [OKAFOR, BAND]
    assert check("artist", similar_artists) == (1, 1)
    assert found["largeImageUrl"]  # the rest of the info as before
    for entry in similar_artists:  # each opens
        assert client.ok("getArtist", {"id": entry["id"]})["artist"]["name"] == entry["name"]
    # getArtistInfo's entries are the plain ones: an ID, a name, a picture.
    plain = info(client, "getArtistInfo", id=artist)["similarArtist"]
    assert [a["id"] for a in plain] == [a["id"] for a in similar_artists]
    assert all(set(a) <= {"id", "name", "coverArt", "artistImageUrl"} for a in plain)
    assert len(info(client, "getArtistInfo2", id=artist, count=1)["similarArtist"]) == 1
    assert info(client, "getArtistInfo2", id=artist, count=0)["similarArtist"] == []
    # Asked once, by the artist's name alone; saved for the other requests.
    assert [r["artist"] for r in lastfm.asked("artist.getSimilar")[asked:]] == [FIRST]
    logged = " ".join(radio_log).lower()
    assert "similar artists" in logged and not any(n.lower() in logged for n in (FIRST, BAND))


@pytest.mark.parametrize("method", ["getArtistInfo", "getArtistInfo2"])
def test_similar_artists_in_xml(world: DeliveryWorld, client: SubsonicClient, method: str) -> None:
    similar_saved(world, client)
    params = {"id": f"sh.ar.demo.{FIRST_ID}"}
    as_json = client.request(method, params, fmt="json").json()["subsonic-response"]
    as_xml = client.request(method, params, fmt="xml")
    assert xsd.valid(as_xml.content)
    assert same_data(as_json, ET.fromstring(as_xml.content)) == []  # noqa: S314 - test data
    assert as_xml.content.count(b"<similarArtist ") == 2


def test_an_artist_s_info_never_waits_for_similar_artists_and_has_them_next_time(
    world: DeliveryWorld,
    client: SubsonicClient,
    lastfm: FakeLastFm,
    radio: CatalogRadio,
    radio_log: list[str],
) -> None:
    key = FIRST_SIMILAR_KEY
    forget(world, key)
    artist = f"sh.ar.demo.{FIRST_ID}"
    listener = SubsonicClient(
        world.server.base_url, "listener", "invented-listener-password", headers=HOST,
        client="Radio",
    )  # fmt: skip
    lastfm.delay = 3.0
    try:
        started = time.monotonic()
        answered = info(client, "getArtistInfo2", id=artist)
        took = time.monotonic() - started
        assert answered["similarArtist"] == [] and answered["largeImageUrl"]
        # Not waited for at all (not even the moment it once was): made in the background.
        assert any("list made in the background" in line for line in radio_log)
        assert took < 2.5
        assert until(lambda: saved_at(world, key) is not None, 15)
    finally:
        lastfm.delay = 0.0
    assert len(info(client, "getArtistInfo2", id=artist)["similarArtist"]) == 2
    try:  # an account without a Last.fm key: none, as before
        assert info(listener, "getArtistInfo2", id=artist)["similarArtist"] == []
    finally:
        listener.close()
    radio.enabled = False
    try:
        assert info(client, "getArtistInfo2", id=artist)["similarArtist"] == []
    finally:
        radio.enabled = True


# --- guards, and what stays Navidrome's --------------------------------------------------


def test_nothing_is_asked_while_a_client_asks_for_many_lists_or_walks_pages(
    world: DeliveryWorld, lastfm: FakeLastFm, replay: Replay, radio: CatalogRadio
) -> None:
    forget(world, SONG_KEY, BAND_KEY, BAND_SIMILAR_KEY)
    additions = world.services.additions
    assert additions is not None
    previous, radio.sync = radio.sync, Bursts(2, 30.0)
    walker = world.client(headers=HOST, client="walker")
    try:
        assert similar(walker, id=f"sh.ar.demo.{BAND_ID}")  # the first list: made
        sent, before = len(lastfm.requests), replay.api_requests
        assert similar(walker, id=SEED_ID) == []  # the second within the window: a sync
        assert len(lastfm.requests) == sent
        assert [r for r in replay.log[before:] if r.startswith("search")] == []
        assert similar(walker, id=f"sh.ar.demo.{BAND_ID}")  # a saved list still answers
    finally:
        radio.sync = previous
        walker.close()
    # A client walking artist pages: the same.
    pages = Bursts(2, 30.0)
    for page in ("a", "b"):
        pages.note((ADMIN_USER, "pager"), page)
    previous_pages, additions.artist_sync = additions.artist_sync, pages
    pager = world.client(headers=HOST, client="pager")
    try:
        sent = len(lastfm.requests)
        assert similar(pager, id=SEED_ID) == []
        assert len(lastfm.requests) == sent
    finally:
        additions.artist_sync = previous_pages
        pager.close()


def test_radios_and_similar_artists_are_made_a_few_at_a_time_each_kind_apart(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm, radio: CatalogRadio
) -> None:
    forget(world, SONG_KEY)
    sent = len(lastfm.requests)
    radio._kinds.update({"one": "songs", "another": "songs"})  # two radios being made
    try:
        assert similar(client, id=SEED_ID) == []  # a third is not begun
        assert len(lastfm.requests) == sent
        # Similar artists made for the pages a client opened do not keep a radio waiting.
        radio._kinds.update({"one": "artists", "another": "artists"})
        assert len(similar(client, id=SEED_ID)) == len(EXPECTED)
    finally:
        for key in ("one", "another"):
            radio._kinds.pop(key, None)


def test_the_playlists_page_says_what_the_key_is_also_used_for(
    world: DeliveryWorld, radio: CatalogRadio
) -> None:
    note = "similar songs and similar artists for music that is only in the catalog"
    browser = Browser(world.server.base_url)
    try:
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        assert note in browser.get("playlists").text
        radio.enabled = False
        try:
            assert note not in browser.get("playlists").text
        finally:
            radio.enabled = True
    finally:
        browser.close()


def test_what_navidrome_has_stays_navidrome_s(
    world: DeliveryWorld, client: SubsonicClient, lastfm: FakeLastFm
) -> None:
    """An owned song, a placeholder, their albums and artists - by their own IDs and by
    the catalog IDs of items in the library - are answered by Navidrome, and Last.fm is
    asked nothing."""
    sent = len(lastfm.requests)
    direct = world.nd.client(headers=HOST, client="Radio")
    owned = direct.ok("getSong", {"id": native_song(world)})["song"]
    placed = direct.ok("getSong", {"id": placed_song(world)})["song"]
    for method, _ in METHODS:
        # Its artist's only song: Navidrome has no similar songs for it.
        for fmt in ("json", "xml"):
            ours = client.request(method, {"id": owned["id"]}, fmt=fmt)  # type: ignore[arg-type]
            assert ours.content == empty(world, method, fmt), (method, fmt)
        # Navidrome's own radio is the artist's songs in the library, shuffled: a
        # placeholder's too.
        idents = (owned["albumId"], owned["artistId"], placed["id"], placed["albumId"],
                  placed["artistId"])  # fmt: skip
        for ident in idents:
            mine = {s["id"] for s in similar(client, method, id=ident)}
            assert mine and mine == {s["id"] for s in similar(direct, method, id=ident)}
    for ident in (owned["artistId"], placed["artistId"]):
        for method in ("getArtistInfo", "getArtistInfo2"):
            ours = client.request(method, {"id": ident})
            assert ours.content == direct.request(method, {"id": ident}).content
    # The catalog's IDs of the placeholder's song, album and artist: the library's items.
    catalog_ids = (
        (f"sh.tr.demo.{A_PLACED['ref']['id']}", placed["id"]),
        (f"sh.al.demo.{item('album-soundtrack-expanded')}", placed["albumId"]),
        (f"sh.ar.demo.{OKAFOR_ID}", placed["artistId"]),
    )
    for catalog, native in catalog_ids:
        mine = {s["id"] for s in similar(client, id=catalog)}
        assert mine == {s["id"] for s in similar(direct, id=native)}
        assert not any(i.startswith("sh.") for i in mine)
    assert len(lastfm.requests) == sent
    # No release was added, no placeholder written by any of the radios above.
    assert world.placeholder_rows() == len(SOUNDTRACK["tracks"])
