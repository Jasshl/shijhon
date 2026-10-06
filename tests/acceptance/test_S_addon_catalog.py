"""Suite S - the catalog of an add-on: ``[catalog] kind = "addon"`` through the
Subsonic API, in JSON and XML, against a real Navidrome.

The add-on is the fake one with the harness's invented catalog (IDs that need
translating, ``lib:...``), next to a second, audio-only add-on. Shown: a search, an album,
artist pages (the add-on's own artist items and artists credited by name) and top songs as
catalog entries; a commit of a song whose album the add-on names by title only; a play of
a catalog song from the same add-on and from another; catalog requests at the add-on's
request limit, with its lookups; an add-on that fails, answers slowly or says "too many
requests" (the library answers, nothing fails loudly); an add-on without a catalog
(refused, with the reason); covers (public addresses only); credentials first.
"""

from __future__ import annotations

import html
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from shijhon.catalog.addon import NO_CATALOG, AddonCatalog
from shijhon.catalog.cache import CachedCatalog
from shijhon.delivery.addon import catalog_key, tagged
from shijhon.delivery.pacing import Limits
from tests.acceptance.test_J_contract import problems
from tests.acceptance.test_J_xml import both
from tests.conftest import NavidromeFactory
from tests.harness.addon_catalog import FakeCatalog
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.subsonic import SubsonicClient

NAME = "Fake"
KEY = catalog_key(NAME)
SERVICE = "test.fake.fake"  # the fake add-on's manifest ID
HOST = {"host": "music.test"}
# The library has one album of an artist the add-on's catalog has too.
OWNED = Album("Odile Brandt", "Early Rooms", (Track("First Room", 1), Track("Second Room", 2)))
CATALOG = ("search", "album", "artist")
API = ("manifest", "resolve-isrc", "resolve", "stream", "availability", *CATALOG)


def ident(kind: str, own: str, tag: str = "") -> str:
    """The ID clients see for the add-on's item ``own`` ("lib:" before it; ``tag``: "i" for
    an artist): the catalog's key, the service's tag, and the add-on's own ID."""
    return f"sh.{kind}.{KEY}.{tag}{tagged(SERVICE, 'lib:' + own)}"


@pytest.fixture(scope="module")
def held() -> FakeCatalog:
    return FakeCatalog("plain", prefix="lib:")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    held: FakeCatalog,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    nd.scan(full=True)
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("addon-catalog"),
        catalog_settings={"kind": "addon", "addon": NAME},
        warm_ahead_depth=0,
    ) as w:
        main = w.addon(NAME, catalog=held)
        # Its covers are at its own address: on this machine, where no cover is fetched from.
        held.images = f"{main.base_url}/img"
        w.add_source(main)
        w.add_source(w.addon("Other"))
        yield w


@pytest.fixture
def main(world: DeliveryWorld) -> FakeAddon:
    return world.addons[0]


@pytest.fixture
def other(world: DeliveryWorld) -> FakeAddon:
    return world.addons[1]


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client(headers=HOST, client="AddonCatalog")
    yield c
    c.close()


def added(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if str(e.get("id", "")).startswith("sh.")]


def forget(world: DeliveryWorld) -> None:
    """The catalog's answers kept so far are dropped: the next view asks the add-on (the
    tests run in any order, and each counts its own requests)."""
    catalog = world.services.catalog
    assert isinstance(catalog, CachedCatalog)
    inner = catalog.inner
    assert isinstance(inner, AddonCatalog)

    async def clear() -> None:
        for kept in (catalog.search, catalog.album, catalog.artist,
                     catalog.artist_releases, catalog.top_songs,
                     inner._found, inner._page):  # fmt: skip
            kept.cache_clear()  # type: ignore[attr-defined]
        inner._songs.clear()
        inner._artists.clear()

    world.server.call(clear)


def asked(addon: FakeAddon, *endpoints: str) -> list[str]:
    """The catalog requests the add-on got: "search <term>", "album <id>"."""
    return [
        f"{r['endpoint']} {r['term'] or r['key']}"
        for r in addon.requests()
        if r["endpoint"] in (endpoints or CATALOG)
    ]


# --- views --------------------------------------------------------------------------------------


def test_a_search_adds_the_add_on_s_catalog(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    forget(world)
    main.clear()
    answer, _ = both(client, "search3", {"query": "venn"})
    found = answer["searchResult3"]
    artists, albums, songs = (added(found.get(k, [])) for k in ("artist", "album", "song"))
    assert [(a["id"], a["name"]) for a in artists] == [(ident("ar", "ar1", "i"), "Mara Venn")]
    assert {a["id"]: a["name"] for a in albums} == {
        ident("al", "al1"): "Glass Rivers",
        ident("al", "al2"): "Low Tide - Single",
    }
    by_title = {s["title"]: s for s in songs}
    assert by_title["Salt Meadow"]["id"] == ident("tr", "t102")
    assert (
        by_title["Salt Meadow"]["isrc"] == ["ZZSHA0000102"] and by_title["Slow Thaw"]["isrc"] == []
    )
    assert by_title["Salt Meadow"]["duration"] == 5 and by_title["Salt Meadow"]["album"]
    # Entries decode like Navidrome's. A song of a search has no album ID - the add-on
    # names its album by title only - and its own cover.
    for kind, entries in (("artist", artists), ("album", albums), ("song", songs)):
        assert [p for e in entries for p in problems(kind, e)] == []
    assert all("albumId" not in s and s["coverArt"] == f"mf-{s['id']}" for s in songs)
    single = next(a for a in albums if a["name"].startswith("Low Tide"))
    assert single["releaseTypes"] == ["single"] and single["songCount"] == 1
    # The library's artist of the same name is the library's.
    brandt = both(client, "search3", {"query": "brandt"})[0]["searchResult3"]
    assert [a["name"] for a in brandt["artist"]] == ["Odile Brandt"]
    assert not added(brandt["artist"]) and added(brandt["album"])
    assert asked(main) == ["search venn", "search brandt"]  # one request a search


def test_an_album_opens_with_its_tracks_in_the_add_on_s_order(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    forget(world)
    main.clear()
    album_id = ident("al", "al1")
    answer, _ = both(client, "getAlbum", {"id": album_id})
    album = answer["album"]
    assert album["name"] == "Glass Rivers" and album["year"] == 2019 and album["songCount"] == 4
    assert [(s["discNumber"], s["track"], s["title"]) for s in album["song"]] == [
        (1, 1, "Glass Rivers"),
        (1, 2, "Salt Meadow"),
        (1, 3, "Harbor Lights"),
        (1, 4, "Slow Thaw"),
    ]
    assert all(s["albumId"] == album_id and s["parent"] == album_id for s in album["song"])
    assert problems("album", album) == []
    featured = album["song"][2]
    assert [p["name"] for p in featured["artists"]] == ["Mara Venn", "Odile Brandt"]
    song, _ = both(client, "getSong", {"id": album["song"][1]["id"]})
    assert song["song"]["title"] == "Salt Meadow" and song["song"]["albumId"] == album_id
    directory, _ = both(client, "getMusicDirectory", {"id": album_id})
    assert [c["title"] for c in directory["directory"]["child"]] == [
        s["title"] for s in album["song"]
    ]
    assert asked(main) == ["album lib:al1"]  # one request, however it is looked at


def test_artist_pages_by_the_add_on_s_item_and_by_a_credited_name(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    forget(world)
    main.clear()
    answer, _ = both(client, "getArtist", {"id": ident("ar", "ar1", "i")})
    page = answer["artist"]
    assert page["name"] == "Mara Venn" and problems("artist", page) == []
    assert [a["name"] for a in page["album"]] == ["Glass Rivers", "Low Tide - Single"]
    # One request for the page - none when its discography was saved by an earlier view.
    assert asked(main) in (["artist lib:ar1"], [])
    # Every artist ID an album hands out opens - an artist credited by name too.
    album = client.ok("getAlbum", {"id": ident("al", "al1")})["album"]
    ids = {album["artistId"], *(p["id"] for s in album["song"] for p in s["artists"])}
    names = {client.ok("getArtist", {"id": i})["artist"]["name"] for i in sorted(ids)}
    assert names == {"Mara Venn", "Odile Brandt"}
    assert any(f".{KEY}.n" in i for i in ids)  # (by name)
    # The library's artist: its own page, with the catalog's albums it lacks.
    native = next(i for i in ids if not i.startswith("sh."))
    page = both(client, "getArtist", {"id": native})[0]["artist"]
    assert page["name"] == "Odile Brandt"
    assert [a["name"] for a in added(page["album"])] == ["North Window"]


def test_top_songs_are_the_artist_answer_s(client: SubsonicClient) -> None:
    answer, _ = both(client, "getTopSongs", {"artist": "Mara Venn", "count": 3})
    songs = answer["topSongs"]["song"]
    assert [s["title"] for s in songs] == ["Salt Meadow", "Glass Rivers", "Low Tide"]
    assert all(s["id"].startswith(f"sh.tr.{KEY}.") for s in songs)


# --- commits and plays ----------------------------------------------------------------------------


def test_a_song_of_a_search_is_added_with_its_album(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    """The add-on names a song's album by title only: adding the song looks the album up
    (a search, then the album of that title that lists the song)."""
    forget(world)
    found = client.ok("search3", {"query": "kite season"})["searchResult3"]
    (song,) = added(found["song"])
    assert "albumId" not in song and song["album"] == "Paper Lanterns"
    rows = world.placeholder_rows()
    main.clear()
    client.ok("star", {"id": song["id"]})
    starred = world.nd.client().ok("getStarred2")["starred2"]["song"]
    mine = [s for s in starred if s["title"] == "Kite Season"]
    assert [(s["album"], s["artist"]) for s in mine] == [("Paper Lanterns", "Ines Calder")]
    native = world.nd.client().ok("getAlbum", {"id": mine[0]["albumId"]})["album"]
    assert [(s["track"], s["title"]) for s in native["song"]] == [
        (1, "Paper Lanterns"),
        (2, "Kite Season"),
        (3, "Wax and Wire"),
    ]
    assert asked(main) == ["search Paper Lanterns Ines Calder", "album lib:al5"]
    assert world.placeholder_rows() == rows + 3
    # From here on the song is the library's, by either ID.
    again = client.ok("search3", {"query": "kite season"})["searchResult3"]["song"]
    assert [s["id"] for s in again] == [mine[0]["id"]]
    client.ok("star", {"id": song["id"]})  # (again: nothing more is written)
    assert world.placeholder_rows() == rows + 3


def test_an_album_not_read_in_full_is_added_as_far_as_it_was_read(
    world: DeliveryWorld, client: SubsonicClient, held: FakeCatalog
) -> None:
    """One track of the album has no length: the album is short of it. Added to the
    library, it has the tracks that were read, at their places in the add-on's list, and
    its files name the catalog's own number of tracks - the album is not called whole.
    Once the add-on lists the track with a length, adding that song puts it in its place."""
    forget(world)
    whole = held.album(held.id("al12")) or {}
    first, second, third = whole["tracks"]
    short = {**whole, "tracks": [first, {**second, "duration": None}, third]}
    held.answers[f"album/{held.id('al12')}"] = short
    try:
        album = client.ok("getAlbum", {"id": ident("al", "al12")})["album"]
        assert [s["title"] for s in album["song"]] == ["First Ferry", "Last Ferry"]
        client.ok("star", {"id": album["song"][0]["id"]})

        async def totals() -> set[str]:
            rows = await world.services.store.fetchall(
                "SELECT tags FROM placeholders WHERE release_ref = ?",
                [f"{KEY}:{tagged(SERVICE, 'lib:al12')}"],
            )
            return {json.loads(row["tags"])["tracktotal"][0] for row in rows}

        assert world.server.call(totals) == {"3"}  # of three, although two are there
        starred = world.nd.client().ok("getStarred2")["starred2"]["song"]
        native = next(s["albumId"] for s in starred if s["title"] == "First Ferry")
        songs = world.nd.client().ok("getAlbum", {"id": native})["album"]["song"]
        assert [(s["track"], s["title"]) for s in songs] == [(1, "First Ferry"), (3, "Last Ferry")]
    finally:
        del held.answers[f"album/{held.id('al12')}"]
    # The add-on lists the track now: found in a search and added, it joins the album.
    forget(world)
    (song,) = added(client.ok("search3", {"query": "counting gulls"})["searchResult3"]["song"])
    client.ok("star", {"id": song["id"]})
    songs = world.nd.client().ok("getAlbum", {"id": native})["album"]["song"]
    assert [(s["track"], s["title"]) for s in songs] == [
        (1, "First Ferry"),
        (2, "Counting Gulls"),
        (3, "Last Ferry"),
    ]


def test_a_song_whose_album_is_not_found_is_not_added(
    world: DeliveryWorld, client: SubsonicClient, held: FakeCatalog
) -> None:
    """No album of the title the song names lists it: an error, and nothing is written."""
    stray = {"id": "lib:t900", "title": "Stray", "artist": "Mara Venn", "duration": 3}
    held.answers["search"] = {"tracks": [{**stray, "album": "Low Tide - Single"}]}
    try:
        (song,) = added(client.ok("search3", {"query": "stray"})["searchResult3"]["song"])
        del held.answers["search"]
        rows = world.placeholder_rows()
        assert client.error_code("star", {"id": song["id"]}) == 70
        assert world.placeholder_rows() == rows
    finally:
        held.answers.pop("search", None)


def test_a_catalog_song_plays_from_its_own_add_on_by_its_id_and_from_another(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon, other: FakeAddon
) -> None:
    """A song of the add-on's catalog carries that add-on's track ID: asked for by it,
    without a lookup - also a song without an ISRC, which no lookup would find. Other
    add-ons are found as always; when the ID is gone there, the song is looked up."""
    forget(world)
    own_audio, bare, others = world.audio("own"), world.audio("bare"), world.audio("others")
    main.add(FakeTrack(isrc="ZZSHA0000102", audio=own_audio, track_id="lib:t102"))
    main.add(FakeTrack(isrc="no-isrc-t104", audio=bare, track_id="lib:t104"))
    other.add(FakeTrack(isrc="ZZSHA0000103", audio=others))  # the first add-on lacks it
    client.ok("search3", {"query": "glass rivers"})
    rows = world.placeholder_rows()
    main.clear()
    other.clear()
    played = client.request("stream", {"id": ident("tr", "t102")})
    assert played.status_code == 200 and played.content == own_audio.read_bytes()
    played = client.request("stream", {"id": ident("tr", "t104")})  # "Slow Thaw": no ISRC
    assert played.status_code == 200 and played.content == bare.read_bytes()
    assert [r["key"] for r in main.requests("stream")] == ["lib:t102", "lib:t104"]
    assert main.requests("resolve-isrc") == [] and other.requests() == []  # no lookup
    played = client.request("stream", {"id": ident("tr", "t103")})
    assert played.status_code == 200 and played.content == others.read_bytes()
    # Its own ID was asked for first, then - not there - the recording was looked up.
    assert [r["key"] for r in main.requests("stream")][2:] == ["lib:t103"]
    assert [r["isrc"] for r in main.requests("resolve-isrc")] == ["ZZSHA0000103"]
    assert [r["isrc"] for r in other.requests("resolve-isrc")] == ["ZZSHA0000103"]
    assert world.placeholder_rows() == rows  # a plain play commits nothing
    # The add-on has the song under another ID now: found by the lookup, and played.
    moved = world.audio("moved")
    main.add(FakeTrack(isrc="ZZSHA0000101", audio=moved, track_id="moved-101"))
    main.clear()
    played = client.request("stream", {"id": ident("tr", "t101")})
    assert played.status_code == 200 and played.content == moved.read_bytes()
    assert [r["key"] for r in main.requests("stream")] == ["lib:t101", "moved-101"]


def test_the_lookup_meanwhile_knows_the_add_on_s_own_id(
    world: DeliveryWorld, tmp_path_factory: pytest.TempPathFactory, held: FakeCatalog
) -> None:
    """Primary first: while the primary's lookup keeps waiting, the likely fallback is
    looked up - the catalog's add-on needs no lookup for a song of its catalog."""
    with delivery_world(
        world.nd,
        tmp_path_factory.mktemp("primary-first"),
        catalog_settings={"kind": "addon", "addon": NAME},
        warm_ahead_depth=0,
        routing="primary_first",
        primary_source="Slow",
        reliable_lookup_after_seconds=0.2,
    ) as second:
        slow = second.addon("Slow")
        fake = second.addon(NAME, catalog=held)
        second.add_source(slow)
        second.add_source(fake)
        audio = second.audio("meanwhile")
        slow.add(FakeTrack(isrc="ZZSHA0000301", audio=audio, available=False, lookup_delay=0.8))
        fake.add(FakeTrack(isrc="ZZSHA0000301", audio=audio, track_id="lib:t301"))
        client = second.client(headers=HOST)
        try:
            client.ok("search3", {"query": "north window"})
            fake.clear()
            with collected("shijhon") as lines:
                played = client.request("stream", {"id": ident("tr", "t301")})
            assert played.status_code == 200 and played.content == audio.read_bytes()
            assert [r["key"] for r in fake.requests("stream")] == ["lib:t301"]
            assert fake.requests("resolve-isrc") == []  # its ID was known without asking
            assert any("its catalog's track, no lookup" in line for line in lines)
        finally:
            client.close()


def test_a_song_in_the_library_still_plays_by_the_add_on_s_own_id(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    """The placeholder of a song that came from the add-on's catalog keeps its track ID."""
    audio = world.audio("placeholder")
    main.add(FakeTrack(isrc="ZZSHA0000601", audio=audio, track_id="lib:t601"))
    client.ok("star", {"albumId": ident("al", "al6")})  # the single, added by its ID
    album = client.ok("getAlbum", {"id": ident("al", "al6")})["album"]
    (song,) = album["song"]
    assert not song["id"].startswith("sh.") and song["title"] == "Tin Roof"
    main.clear()
    played = client.request("stream", {"id": song["id"]})
    assert played.status_code == 200 and played.content == audio.read_bytes()
    assert [r["key"] for r in main.requests("stream")] == ["lib:t601"]
    assert main.requests("resolve-isrc") == []


# --- the add-on's request limit -------------------------------------------------------------------


def test_catalog_requests_and_lookups_share_the_add_on_s_request_limit(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    """One count for everything Shijhon asks the add-on: three views and a play
    together are sent a few at once, then at the limit's rate."""
    source = world.server.call(world.services.sources.enabled)[0]
    assert source.name == NAME

    def limit(limits: Limits) -> None:
        world.server.call(lambda: world.services.sources.update(source.id, limits=limits))
        world.server.call(world.services.sources.enabled)

    main.add(FakeTrack(isrc="ZZSHA0000201", audio=world.audio("limit"), track_id="lib:t201"))
    client.ok("search3", {"query": "low tide"})
    limit(Limits(4.0, 2, 0))
    try:
        main.clear()
        started = time.monotonic()
        views = [
            ("getAlbum", {"id": ident("al", "al4")}),
            ("getArtist", {"id": ident("ar", "ar3", "i")}),
            ("search3", {"query": "ash and elm"}),
            ("search3", {"query": "harbor lights"}),
        ]
        with ThreadPoolExecutor(8) as pool:
            jobs = [pool.submit(client.request, method, params) for method, params in views]
            play = pool.submit(client.request, "stream", {"id": ident("tr", "t201")})
            assert all(job.result().status_code == 200 for job in jobs)
            assert play.result().status_code == 200
        took = time.monotonic() - started
        times = sorted(r["at"] for r in main.requests() if r["endpoint"] in API)
        assert len(times) >= 5  # the four views and the play's link
        # Two at once, then four a second: never more than six within any second.
        assert max(sum(1 for t in times if first <= t < first + 1.0) for first in times) <= 6
        assert took >= (len(times) - 2) / 4.0 - 0.3
    finally:
        limit(Limits())


# --- failures -------------------------------------------------------------------------------------


def test_an_add_on_that_fails_or_is_slow_leaves_the_library_s_answer(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon, held: FakeCatalog
) -> None:
    additions = world.services.additions
    assert additions is not None
    own = world.nd.client().ok("search3", {"query": "room"})["searchResult3"]
    held.status["search"] = 503
    try:
        with collected("shijhon") as lines:
            found = client.ok("search3", {"query": "room"})["searchResult3"]
        assert found == own  # nothing fails loudly: the library's answer
        assert any("catalog lookup unavailable" in line for line in lines)
    finally:
        held.status.clear()
        additions.resting_until = 0.0
    held.delay["search"] = 1.0
    budget, additions.budget = additions.budget, 0.3
    try:
        started = time.monotonic()
        found = client.ok("search3", {"query": "rooms"})["searchResult3"]
        assert time.monotonic() - started < 0.9 and not added(found.get("song", []))
        time.sleep(1.2)  # the slow answer still arrived: the next search has it
        main.clear()
        found = client.ok("search3", {"query": "rooms"})["searchResult3"]
        assert [s["title"] for s in added(found["song"])] == ["Northern Rooms"]
        assert asked(main) == []
    finally:
        held.delay.clear()
        additions.budget = budget
        additions.resting_until = 0.0


def test_an_add_on_that_says_too_many_requests_is_left_alone(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon, held: FakeCatalog
) -> None:
    """Its "too many requests" to a catalog request stops its catalog requests and its
    lookups alike, for the time it names."""
    additions = world.services.additions
    assert additions is not None
    main.add(FakeTrack(isrc="ZZSHA0000401", audio=world.audio("alone"), track_id="lib:t401"))
    main.retry_after = "2"
    held.status["search"] = 429
    try:
        found = client.ok("search3", {"query": "northern rooms"})["searchResult3"]
        assert not added(found.get("song", []))
        held.status.clear()
        additions.resting_until = 0.0
        main.clear()
        with collected("shijhon") as lines:
            found = client.ok("search3", {"query": "northern"})["searchResult3"]
        assert not added(found.get("song", [])) and main.requests() == []  # not asked
        assert any("the add-on said to wait" in line for line in lines)
        time.sleep(2.1)
        additions.resting_until = 0.0
        found = client.ok("search3", {"query": "northern"})["searchResult3"]
        assert [s["title"] for s in added(found["song"])] == ["Northern Rooms"]
        # ... and its audio is not asked for either, meanwhile.
        held.status["search"] = 429
        client.ok("search3", {"query": "northern roo"})
        held.status.clear()
        main.clear()
        refused = client.request("stream", {"id": ident("tr", "t401")})
        assert refused.status_code != 200 or b"failed" in refused.content
        assert main.requests() == []
        time.sleep(2.1)
        played = client.request("stream", {"id": ident("tr", "t401")})
        assert played.status_code == 200 and [r["key"] for r in main.requests("stream")] == [
            "lib:t401"
        ]
    finally:
        held.status.clear()
        main.retry_after = "1"
        additions.resting_until = 0.0


def test_an_add_on_without_a_catalog_is_refused_with_the_reason(
    world: DeliveryWorld, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The add-on named has audio only: searches are the library's, and the log says why."""
    with delivery_world(
        world.nd,
        tmp_path_factory.mktemp("no-catalog"),
        catalog_settings={"kind": "addon", "addon": "Audio only"},
    ) as second:
        plain = second.addon("Audio only")
        second.add_source(plain)
        client = second.client(headers=HOST)
        try:
            own = world.nd.client().ok("search3", {"query": "room"})["searchResult3"]
            with collected("shijhon") as lines:
                assert client.ok("search3", {"query": "room"})["searchResult3"] == own
            assert any(NO_CATALOG in line for line in lines)
            assert {r["endpoint"] for r in plain.requests()} == {"manifest"}  # never asked
            album = f"sh.al.{catalog_key('Audio only')}.{tagged('any', 'x')}"
            code = client.error_code("getAlbum", {"id": album})
            assert code == 0
        finally:
            client.close()


def test_the_dashboard_names_the_add_on_and_checks_it(
    world: DeliveryWorld, main: FakeAddon, held: FakeCatalog
) -> None:
    browser = Browser(world.server.base_url)
    try:
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        diagnostics = html.unescape(browser.get("diagnostics").text)
        assert f"An add-on's catalog: {NAME}" in diagnostics
        page = browser.get("catalog").text
        assert "Answering, checked" in page
        assert f'<option value="{NAME}" selected>{NAME}</option>' in page
        # "Check now" reads the add-on's manifest again; one that lost its catalog says so.
        before = len(main.requests("manifest"))
        main.resources = ("stream", "isrc")
        try:
            assert browser.post("catalog/check", {"csrf": browser.csrf()}).status_code == 303
            assert len(main.requests("manifest")) == before + 1
            assert NO_CATALOG in html.unescape(browser.get("catalog").text)
        finally:
            main.resources = ("stream", "isrc", "search", "catalog")
            browser.post("catalog/check", {"csrf": browser.csrf()})
        assert "Answering, checked" in browser.get("catalog").text
    finally:
        browser.close()


# --- covers, credentials --------------------------------------------------------------------------


def test_a_cover_is_fetched_from_public_addresses_only(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    """The add-on is on this machine and so are its covers: its own reach lets Shijhon ask
    it, and never fetch a cover from there."""
    client.ok("getAlbum", {"id": ident("al", "al1")})
    source = world.server.call(world.services.sources.enabled)[0]
    turns = source.pace.sent
    with collected("shijhon") as lines:
        answer = client.request("getCoverArt", {"id": f"al-{ident('al', 'al1')}", "size": 300})
    assert answer.status_code == 200  # Navidrome's own answer for a cover it lacks
    assert main.requests("image") == []
    assert any("network policy" in line for line in lines)
    assert source.pace.sent == turns  # and no turn at its request limit was spent on it


def test_no_address_the_add_on_names_is_given_to_a_client(
    world: DeliveryWorld, client: SubsonicClient, main: FakeAddon
) -> None:
    """An add-on may name any address for a cover - one inside the network too. Shijhon
    fetches a cover itself, through its checks; no answer hands such an address to an app
    (``artistImageUrl``, the info answers' image fields): the image is its cover art ID."""
    artist, album = ident("ar", "ar1", "i"), ident("al", "al1")
    views = [
        ("search3", {"query": "venn"}),
        ("getArtist", {"id": artist}),
        ("getArtistInfo", {"id": artist}),
        ("getArtistInfo2", {"id": artist}),
        ("getAlbum", {"id": album}),
        ("getAlbumInfo", {"id": album}),
        ("getAlbumInfo2", {"id": album}),
        ("getTopSongs", {"artist": "Mara Venn"}),
    ]
    for fmt in ("json", "xml"):
        for method, params in views:
            answer = client.request(method, {**params, "f": fmt})
            assert answer.status_code == 200, method
            assert "/img/" not in answer.text and "ImageUrl" not in answer.text, (method, fmt)
            assert "#sh=" not in answer.text and "sh%3D" not in answer.text, (method, fmt)
    found = client.ok("search3", {"query": "venn"})["searchResult3"]
    shown = [a for a in found["artist"] if a["name"] == "Mara Venn"]
    assert shown and all(a["coverArt"] == f"ar-{a['id']}" for a in shown)
    page = client.ok("getArtist", {"id": artist})["artist"]
    assert page["coverArt"] == f"ar-{artist}"


def test_no_catalog_request_before_the_credentials_are_checked(
    world: DeliveryWorld, main: FakeAddon
) -> None:
    main.clear()
    stranger = SubsonicClient(world.server.base_url, "admin", "wrong", headers=HOST)
    try:
        assert stranger.error_code("search3", {"query": "salt meadow"}) == 40
        assert stranger.error_code("getAlbum", {"id": ident("al", "al4")}) == 40
        assert stranger.error_code("star", {"id": ident("tr", "t401")}) == 40
    finally:
        stranger.close()
    assert main.requests() == []
