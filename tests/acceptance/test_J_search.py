"""Suite J — the search contract and artist-page additions.

Navidrome's own answer comes first and unchanged; catalog results fill what is left of
each requested count on the first page only; an exact artist match comes first, and that
artist's albums before other artists' singles; nothing the library has is added twice.
Empty and short queries, other formats and a failing or slow catalog give Navidrome's
answer alone. Repeated and concurrent searches share one catalog request. Every search is
logged without its text; a client's burst of different searches gets the library's answers
alone for a while, and a client's newer search ends the older one's wait. Catalog
items credited to a library artist link to it. Artist pages use saved discographies,
and a client walking many artist pages without one gets the library's pages alone.

The catalog is the replayed demo records; searches for the library's (invented) names
are answered with recorded searches (``Replay.aliases``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from shijhon.catalog.model import Twins
from shijhon.views.bursts import Bursts
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.logs import collected
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient

# Navidrome builds image URLs from the request's host: the same host both ways.
HOST = {"host": "music.test"}
ARTIST = "Oren Garrow"  # the duo fixtures' artist; the library has some of their music
TERM = ARTIST.lower()
RECORDED = str(fixture("search-single-vs-album")["params"]["term"])
OWNED = [
    # The catalog's "Hollow 2" is dated 1994; the owned files say 1995.
    Album(ARTIST, "Hollow 2", (Track("One", 1), Track("Two", 2)), recording_date="1995"),
    # An owned single of a recording the catalog lists five times (one ISRC).
    Album(ARTIST, "Open Lanterns", (Track("Open Lanterns", 1, isrc="ZZSHJ0000228"),)),
]


def item(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases[TERM] = RECORDED
    replay.aliases["wren loring"] = str(fixture("search-covers")["params"]["term"])
    replay.aliases["uma okafor"] = str(fixture("search-soundtrack")["params"]["term"])
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in OWNED:
        write_album(nd.music, album)
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("search"), catalog=replay.catalog()) as w:
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client(headers=HOST)
    yield c
    c.close()


@pytest.fixture
def search_log() -> Iterator[list[str]]:
    with collected("shijhon.views.additions") as lines:
        yield lines


def stable(value: Any) -> Any:
    """Without Navidrome's one documented nondeterminism: the order of artist roles."""
    if isinstance(value, dict):
        return {k: sorted(v) if k == "roles" else stable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def direct(world: DeliveryWorld, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Navidrome's own answer, without Shijhon."""
    body: dict[str, Any] = stable(world.nd.client(headers=HOST).ok(method, params))
    return body


def search(client: SubsonicClient, query: str, **counts: Any) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = stable(
        client.ok("search3", {"query": query, **counts})["searchResult3"]
    )
    return found


def catalog(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries if str(e["id"]).startswith("sh.")]


def searches(replay: Replay, term: str) -> int:
    return sum(1 for line in replay.log if line == f"search?term={term}")


def test_the_library_s_answer_comes_first_and_unchanged(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    own = direct(world, "search3", {"query": TERM})["searchResult3"]
    found = search(client, TERM)
    for kind in ("artist", "album", "song"):
        owned = own.get(kind, [])
        assert owned, kind
        assert found[kind][: len(owned)] == owned, kind
        assert all(str(e["id"]).startswith("sh.") for e in found[kind][len(owned) :]), kind
    assert catalog(found["album"]) and catalog(found["song"])


def test_nothing_the_library_has_is_added_again(client: SubsonicClient) -> None:
    found = search(client, TERM)
    names = [(a["name"], a["artist"]) for a in catalog(found["album"])]
    assert ("Hollow 2", ARTIST) not in names  # owned, though its year differs by one
    assert all(a["name"] != ARTIST for a in catalog(found["artist"]))  # the owned artist
    isrcs = [i for s in catalog(found["song"]) for i in s["isrc"]]
    assert "ZZSHJ0000228" not in isrcs  # the owned recording, from any album
    assert len(isrcs) == len(set(isrcs))  # one version of each recording


def test_the_searched_artist_first_then_albums_before_singles(client: SubsonicClient) -> None:
    albums = catalog(search(client, TERM)["album"])
    exact = [a["artist"] == ARTIST for a in albums]
    assert exact[0] and exact == sorted(exact, reverse=True)
    others = [a for a in albums if a["artist"] != ARTIST]
    singles = [a["releaseTypes"] == ["single"] for a in others]
    assert singles == sorted(singles) and True in singles and False in singles
    songs = catalog(search(client, TERM)["song"])
    by_artist = [ARTIST in s["artist"] for s in songs]
    assert by_artist == sorted(by_artist, reverse=True)
    # An exact artist match is the first catalog artist, with its image.
    artists = catalog(search(client, "wren loring")["artist"])
    assert artists[0]["name"] == "Wren Loring" and len(artists) > 1
    assert artists[0]["artistImageUrl"].startswith("https://")
    assert artists[0]["coverArt"] == f"ar-{artists[0]['id']}"


def test_counts_are_respected_and_additions_are_on_the_first_page_only(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    owned = len(direct(world, "search3", {"query": TERM})["searchResult3"]["album"])
    found = search(client, TERM, albumCount=owned + 1, songCount=0, artistCount=1)
    assert len(found["album"]) == owned + 1 and len(catalog(found["album"])) == 1
    assert "song" not in found
    assert len(found["artist"]) == 1  # the owned artist fills the only place
    later = search(client, TERM, albumOffset=1)
    assert later["album"] == direct(world, "search3", {"query": TERM, "albumOffset": 1})[
        "searchResult3"
    ].get("album", [])
    assert catalog(later["song"])  # other types' first pages still get additions


@pytest.mark.parametrize("query", ["", '""', "or"])
def test_empty_and_short_queries_pass_through(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, query: str
) -> None:
    before = replay.api_requests
    found = search(client, query, songCount=5, albumCount=5, artistCount=5)
    assert found == direct(world, "search3", {"query": query, "songCount": 5, "albumCount": 5,
                                              "artistCount": 5})["searchResult3"]  # fmt: skip
    assert replay.api_requests == before


def test_xml_gets_the_additions_and_jsonp_is_forwarded(
    client: SubsonicClient, replay: Replay
) -> None:
    """XML answers carry the same additions as JSON (the XML form itself: suite J's
    ``test_J_xml.py``); JSONP is Navidrome's own answer."""
    found = search(client, "uma okafor")
    xml = client.request("search3", {"query": "uma okafor"}, fmt="xml")
    assert xml.headers["content-type"] == "application/xml"
    for entry in catalog(found.get("album", [])) + catalog(found.get("song", [])):
        assert f'id="{entry["id"]}"' in xml.text
    before = replay.api_requests
    jsonp = client.request("search3", {"query": "uma okafor", "f": "jsonp", "callback": "cb"},
                           fmt="xml")  # fmt: skip
    assert jsonp.text.startswith("cb(") and "sh.al." not in jsonp.text
    assert replay.api_requests == before


def test_repeated_and_concurrent_searches_share_one_catalog_request(
    world: DeliveryWorld, replay: Replay
) -> None:
    term = "uma okafor"
    clients = [world.client(headers=HOST) for _ in range(5)]
    replay.slow["search"] = 0.5
    try:
        with ThreadPoolExecutor(5) as pool:
            answers = list(pool.map(lambda c: search(c, term), clients))
        for _ in range(3):
            search(clients[0], term.upper())  # the same search, typed differently
    finally:
        replay.slow.clear()
        for c in clients:
            c.close()
    assert all(catalog(a["album"]) for a in answers)
    assert searches(replay, term) == 1


def test_a_failing_or_slow_catalog_leaves_the_library_s_answer(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    additions = world.services.additions
    assert additions is not None
    own = direct(world, "search3", {"query": "hollow"})["searchResult3"]
    replay.aliases["hollow"] = RECORDED
    replay.aliases["hollow two"] = RECORDED
    replay.failing["search"] = 503
    try:
        assert search(client, "hollow") == own
        # After a failure the additions rest a while: no catalog call at all.
        before = replay.api_requests
        search(client, "hollow two")
        assert replay.api_requests == before
    finally:
        replay.failing.clear()
        additions.resting_until = 0.0
    own = direct(world, "search3", {"query": "hollow two"})["searchResult3"]
    replay.slow["search"] = 1.0
    budget, additions.budget = additions.budget, 0.3
    try:
        started = time.monotonic()
        assert search(client, "hollow two") == own
        assert time.monotonic() - started < 0.9
        # The catalog's answer, finished in the background, serves the next request.
        time.sleep(1.5)
        assert catalog(search(client, "hollow two")["album"])
        assert searches(replay, "hollow two") == 1
    finally:
        additions.budget = budget
        replay.slow.clear()


def test_no_wait_for_the_catalog_when_the_library_fills_every_count(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    replay.aliases["hollow three"] = RECORDED
    replay.slow["search"] = 2.0
    try:
        started = time.monotonic()
        found = search(client, TERM, artistCount=1, albumCount=1, songCount=1)
        assert time.monotonic() - started < 1.5
    finally:
        replay.slow.clear()
    assert not any(catalog(found.get(k, [])) for k in ("artist", "album", "song"))


def test_form_posts_and_music_folders(world: DeliveryWorld, client: SubsonicClient) -> None:
    posted = stable(client.ok("search3", {"query": TERM}, http_method="POST")["searchResult3"])
    assert catalog(posted["album"])
    assert catalog(search(client, TERM, musicFolderId=1)["album"])
    # Not this library: Navidrome's own answer (here its error for an unknown library).
    params = {"query": TERM, "musicFolderId": 2}
    assert client.error_code("search3", params) == 70
    assert world.nd.client(headers=HOST).error_code("search3", params) == 70


def test_a_reply_with_additions_has_right_headers(client: SubsonicClient) -> None:
    answer = client.request("search3", {"query": TERM}, headers={"accept-encoding": "identity"})
    assert answer.headers["content-type"].startswith("application/json")
    assert int(answer.headers["content-length"]) == len(answer.content)
    assert "etag" not in answer.headers and "content-encoding" not in answer.headers
    assert catalog(answer.json()["subsonic-response"]["searchResult3"]["album"])
    # Compressed for a client that takes it, as Navidrome compresses its answers.
    compressed = client.request("search3", {"query": TERM})
    assert compressed.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in compressed.headers["vary"].lower()
    assert stable(compressed.json()) == stable(answer.json())


# --- artist pages ----------------------------------------------------------------------------


def library_artist(world: DeliveryWorld) -> str:
    found = direct(world, "search3", {"query": TERM, "albumCount": 0, "songCount": 0})
    [artist] = [a["id"] for a in found["searchResult3"]["artist"] if a["name"] == ARTIST]
    return str(artist)


def test_an_artist_page_adds_the_releases_the_library_lacks(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    ident = library_artist(world)
    own = direct(world, "getArtist", {"id": ident})["artist"]
    page = stable(client.ok("getArtist", {"id": ident})["artist"])
    assert {k: v for k, v in page.items() if k != "album"} == {
        k: v for k, v in own.items() if k != "album"
    }
    assert page["album"][: len(own["album"])] == own["album"]
    added = page["album"][len(own["album"]) :]
    assert added and all(a["id"].startswith("sh.al.demo.") for a in added)
    names = [a["name"] for a in added]
    assert "Hollow 2" not in names  # owned
    # Editions: the standard "Iron Wolves" (13 tracks) with its much larger anniversary
    # edition as a card of its own, and a differently named release.
    iron = fixture("album-feat-standard")["body"]["title"]
    assert names.count(iron) == 1
    assert any(n.startswith(f"{iron} (") and "Anniversary" in n for n in names)
    singles = [a for a in added if a["releaseTypes"] in (["single"], ["ep"])]
    assert singles  # the singles view too
    # An addition opens as a virtual album (one the fixtures have in detail).
    standard = f"sh.al.demo.{item('album-feat-standard')}"
    assert standard in [a["id"] for a in added]
    assert client.ok("getAlbum", {"id": standard})["album"]["id"] == standard


def test_an_artist_page_s_covers_need_no_album_requests(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """The page already carries each release's artwork, so a cover costs no album
    detail request; covers are kept on disk per cover and size, so they are served from
    there again - also once the page's artwork is forgotten (a restart)."""
    page = client.ok("getArtist", {"id": library_artist(world)})["artist"]
    added = [a for a in page["album"] if str(a["id"]).startswith("sh.al.")]
    assert len(added) >= 3

    def albums_asked() -> list[str]:
        return [line for line in replay.log if line.startswith("albums/")]

    before, images = albums_asked(), replay.artwork_requests
    for album in added:
        cover = client.request("getCoverArt", {"id": album["coverArt"], "size": "123"})
        assert cover.status_code == 200 and cover.headers["content-type"] == "image/jpeg"
    assert albums_asked() == before  # no album request for any of them
    assert replay.artwork_requests == images + len(added)
    assert len(list((world.tmp / "state" / "artwork").glob("*/*.img"))) >= len(added)

    async def forget() -> None:  # as after a restart: nothing remembered in memory
        views = world.services.views
        assert views is not None and views.artwork is not None
        views.artwork._templates.clear()
        world.services.catalog.artwork.cache_clear()  # type: ignore[union-attr]

    world.server.call(forget)
    for album in added:
        client.request("getCoverArt", {"id": album["coverArt"], "size": "123"})
    assert replay.artwork_requests == images + len(added)  # from the disk
    assert albums_asked() == before


def test_covers_are_fetched_at_the_next_common_size_up(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """The catalog's image server renders an unusual size first (0.35-0.6 s): a
    cover is fetched, and kept, at the next common size up; other sizes in between are
    served from that copy."""
    page = client.ok("getArtist", {"id": library_artist(world)})["artist"]
    album = next(a for a in page["album"] if str(a["id"]).startswith("sh.al."))
    images = replay.artwork_requests
    first = client.request("getCoverArt", {"id": album["coverArt"], "size": "347"})
    assert first.status_code == 200 and replay.artwork_requests == images + 1
    assert "/400x400.png" in replay.artwork_urls[-1]
    again = client.request("getCoverArt", {"id": album["coverArt"], "size": "360"})
    assert again.content == first.content and replay.artwork_requests == images + 1


def test_covers_of_items_shown_before_a_restart_need_no_album_request(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """Albums' and artists' artwork is kept on disk too: after a restart (nothing in
    memory), a cover of an album shown before needs no album request, at any size."""
    page = client.ok("getArtist", {"id": library_artist(world)})["artist"]
    added = [a for a in page["album"] if str(a["id"]).startswith("sh.al.")][:3]
    views = world.services.views
    assert views is not None and views.artwork is not None
    world.server.call(views.artwork.flush)

    async def restart() -> None:  # nothing remembered in memory
        assert views.artwork is not None
        views.artwork._templates.clear()
        catalog = world.services.catalog
        catalog.artwork.cache_clear()  # type: ignore[union-attr]
        catalog.album.cache_clear()  # type: ignore[union-attr]

    world.server.call(restart)
    asked = [line for line in replay.log if line.startswith("albums/")]
    images = replay.artwork_requests
    for album in added:  # a size not asked for before: not on disk yet
        cover = client.request("getCoverArt", {"id": album["coverArt"], "size": "1000"})
        assert cover.status_code == 200
    assert [line for line in replay.log if line.startswith("albums/")] == asked
    assert replay.artwork_requests == images + len(added)


def test_a_catalog_artist_the_library_has_opens_as_the_library_artist(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """Search results and catalog cards link to catalog artists; one the library has
    shows the library's albums with the missing ones, not the whole catalog again."""
    ident = library_artist(world)
    catalog_artist = f"sh.ar.demo.{item('artist-duo')}"
    page = stable(client.ok("getArtist", {"id": catalog_artist})["artist"])
    library_page = stable(client.ok("getArtist", {"id": ident})["artist"])
    assert page == library_page
    assert page["id"] == ident


def test_an_artist_the_catalog_does_not_know_is_unchanged(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    write_album(world.nd.music, Album("Nobody Known", "Quiet Room", (Track("Room", 1),)))
    world.nd.scan(full=False)
    found = direct(world, "search3", {"query": "nobody known", "albumCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["artist"]]
    replay.aliases["nobody known"] = RECORDED  # no artist of that name in the answer
    answer = client.request("getArtist", {"id": ident})
    assert stable(json.loads(answer.content)) == stable(
        json.loads(world.nd.client(headers=HOST).request("getArtist", {"id": ident}).content)
    )


def test_catalog_items_by_a_library_artist_link_to_it(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """Clients open the library artist directly, not a catalog artist ID."""
    ident = library_artist(world)
    found = search(client, TERM)
    albums = [a for a in catalog(found["album"]) if a["artist"] == ARTIST]
    assert albums and all(a["artistId"] == ident for a in albums)
    assert all(p["id"] == ident for a in albums for p in a["artists"] if p["name"] == ARTIST)
    songs = [s for s in catalog(found["song"]) if s["artist"] == ARTIST]
    assert songs and all(s["artistId"] == ident for s in songs)
    # A catalog album opened by itself.
    standard = f"sh.al.demo.{item('album-feat-standard')}"
    album = client.ok("getAlbum", {"id": standard})["album"]
    assert album["artistId"] == ident
    assert [s["albumArtists"][0].get("id") for s in album["song"]][:1] == [ident]
    # Artists the library does not have keep catalog IDs.
    other = client.ok("getAlbum", {"id": f"sh.al.demo.{item('album-ep-without-suffix')}"})
    assert other["album"]["artistId"].startswith("sh.ar.demo.")


def library_artist_page(world: DeliveryWorld, client: SubsonicClient) -> list[str]:
    return [
        a["id"] for a in client.ok("getArtist", {"id": library_artist(world)})["artist"]["album"]
    ]


def saved_at(world: DeliveryWorld, key: str) -> float | None:
    async def read() -> float | None:
        row = await world.services.store.fetchone(
            "SELECT fetched_at FROM discographies WHERE key = ?", [key]
        )
        return float(row["fetched_at"]) if row else None

    return world.server.call(read)


def test_artist_pages_answer_from_a_saved_discography(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """Saved in the database; an old list answers at once and is refreshed."""
    key = "demo.xx:name:orengarrow"  # per catalog and region
    page = library_artist_page(world, client)
    assert saved_at(world, key) is not None
    cache = world.services.catalog
    for lookup in (cache.search, cache.artist_releases):  # type: ignore[union-attr]
        lookup.cache_clear()
    before = replay.api_requests
    assert library_artist_page(world, client) == page
    assert replay.api_requests == before  # nothing asked: the saved list

    async def age() -> None:
        await world.services.store.execute(
            "UPDATE discographies SET fetched_at = 0 WHERE key = ?", [key]
        )

    world.server.call(age)
    replay.slow["search"] = 2.0
    try:
        started = time.monotonic()
        assert library_artist_page(world, client) == page
        assert time.monotonic() - started < 1.5  # did not wait for the refresh
        deadline = time.monotonic() + 5
        while (saved_at(world, key) or 0) == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        replay.slow.clear()
    assert (saved_at(world, key) or 0) > 0  # refreshed in the background


def test_a_client_walking_artist_pages_gets_the_library_s_pages(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, search_log: list[str]
) -> None:
    """Many artist pages without a saved discography in a short time are a sync."""
    names = [f"Walker Number {n}" for n in range(5)]
    for name in names:
        write_album(world.nd.music, Album(name, f"{name} Songs", (Track("Only", 1),)))
    world.nd.scan(full=False)
    ids = []
    for name in names:
        found = direct(world, "search3", {"query": name, "albumCount": 0, "songCount": 0})
        ids += [a["id"] for a in found["searchResult3"]["artist"] if a["name"] == name]
    assert len(ids) == len(names)
    additions = world.services.additions
    assert additions is not None
    previous = additions.artist_sync
    assert previous is not None and (previous.limit, previous.window) == (10, 10.0)
    additions.artist_sync = Bursts(3, 30.0)
    walker = world.client(headers=HOST, client="walker")
    try:
        for ident in ids:
            walker.ok("getArtist", {"id": ident})
        asked = [searches(replay, name.lower()) for name in names]
        assert asked == [1, 1, 0, 0, 0]  # the third unsaved page in the window is a sync
        started = [line for line in search_log if line.startswith("artist pages: walker")]
        assert len(started) == 1 and "opened 3 pages" in started[0]
        client.ok("getArtist", {"id": ids[4]})  # another client is not syncing
        assert searches(replay, names[4].lower()) == 1
    finally:
        additions.artist_sync = previous
        walker.close()


def test_during_a_sync_saved_discographies_answer_as_they_are(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """During a sync an artist's saved discography answers as it
    is - old or not - without a refresh; only artists without one get the library's page."""
    key = "demo.xx:name:orengarrow"
    page = library_artist_page(world, client)  # saved
    names = [f"Pacer Number {n}" for n in range(2)]
    for name in names:
        write_album(world.nd.music, Album(name, f"{name} Songs", (Track("Only", 1),)))
    world.nd.scan(full=False)
    ids = []
    for name in names:
        found = direct(world, "search3", {"query": name, "albumCount": 0, "songCount": 0})
        ids += [a["id"] for a in found["searchResult3"]["artist"] if a["name"] == name]
    assert len(ids) == len(names)

    async def age() -> None:
        await world.services.store.execute(
            "UPDATE discographies SET fetched_at = 0 WHERE key = ?", [key]
        )

    world.server.call(age)
    additions = world.services.additions
    assert additions is not None
    previous = additions.artist_sync
    additions.artist_sync = Bursts(3, 30.0)
    pacer = world.client(headers=HOST, client="pacer")
    try:
        for ident in ids:  # two pages without a saved discography
            pacer.ok("getArtist", {"id": ident})
        before = replay.api_requests
        answer = pacer.ok("getArtist", {"id": library_artist(world)})  # the third: a sync
        time.sleep(0.3)
    finally:
        additions.artist_sync = previous
        pacer.close()
    assert [a["id"] for a in answer["artist"]["album"]] == page  # the old saved list
    assert replay.api_requests == before and saved_at(world, key) == 0  # not refreshed


def test_every_search_is_logged_without_its_text(
    client: SubsonicClient, search_log: list[str]
) -> None:
    search(client, TERM)
    search(client, "", songCount=500)
    search(client, "or")
    lines = [line for line in search_log if line.startswith("search3 ")]
    assert len(lines) == 3
    assert not any(word in line for line in lines for word in ("oren", "garrow", " or "))
    first, empty, short = lines
    assert first.startswith("search3 c=shijhon-tests: 11 characters;")
    assert "artist 20@0, album 20@0, song 20@0" in first
    assert any(f"catalog {outcome};" in first for outcome in ("asked", "cached", "joined"))
    assert "empty query" in empty and "song 500@0" in empty
    assert "catalog skipped: empty query" in empty
    assert "2 characters" in short and "catalog skipped: short query" in short


def test_the_search_guard_answers_a_burst_from_the_library(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, search_log: list[str]
) -> None:
    additions = world.services.additions
    assert additions is not None
    previous = additions.search_guard
    assert previous is not None  # the defaults from the settings: generous
    assert (previous.limit, previous.window, previous.pause) == (60, 20.0, 60.0)
    now = [0.0]
    additions.search_guard = Bursts(4, 20.0, pause_seconds=60.0, clock=lambda: now[0])
    bursty = world.client(headers=HOST, client="bursty")
    try:
        for n in range(6):
            search(bursty, f"burst term {n}")
        asked = [searches(replay, f"burst term {n}") for n in range(6)]
        assert asked == [1, 1, 1, 0, 0, 0]
        guard = [line for line in search_log if line.startswith("search guard: ")]
        assert guard == [
            "search guard: bursty sent 4 different searches within 20s;"
            " answering those from the library alone for at least 60s"
        ]
        assert sum("catalog skipped: search guard" in line for line in search_log) == 3
        search(client, "burst term 5")  # another client is not held back
        assert searches(replay, "burst term 5") == 1
        now[0] += 30.0  # the window is over, the pause is not
        search(bursty, "burst term 6")
        assert searches(replay, "burst term 6") == 0
        now[0] += 61.0  # the pause is over
        search(bursty, "burst term 7")
        assert searches(replay, "burst term 7") == 1
    finally:
        additions.search_guard = previous
        bursty.close()


SONGS_ONLY = {"artistCount": 0, "albumCount": 0, "songCount": 50}


@pytest.fixture
def song_additions(world: DeliveryWorld) -> Iterator[None]:
    """The advanced setting on (the default): isolated song-only searches get
    catalog additions."""
    additions = world.services.additions
    assert additions is not None and additions.song_only_additions is True
    yield


def test_an_isolated_song_only_search_gets_catalog_songs(
    world: DeliveryWorld, search_log: list[str]
) -> None:
    """A client's "Tracks" search (the same term twice, as one client sends it) gets
    catalog songs, one catalog lookup at a time."""
    tracks = world.client(headers=HOST, client="tracks-tab")
    try:
        first = search(tracks, TERM, artistCount=0, albumCount=0, songCount=500)
        again = search(tracks, TERM, artistCount=0, albumCount=0, songCount=100)
    finally:
        tracks.close()
    assert catalog(first["song"]) and catalog(again["song"])
    assert not any("song search guard: tracks-tab" in line for line in search_log)


def test_with_the_setting_off_song_only_searches_ask_the_catalog_nothing(
    world: DeliveryWorld, replay: Replay, search_log: list[str]
) -> None:
    """The advanced setting off: the library answers every song-only search."""
    additions = world.services.additions
    assert additions is not None
    additions.song_only_additions = False
    looker = world.client(headers=HOST, client="default-looker")
    try:
        found = search(looker, TERM, **SONGS_ONLY)
        search(looker, "a track lookup", **SONGS_ONLY)
    finally:
        additions.song_only_additions = True
        looker.close()
    assert not catalog(found.get("song", []))
    assert searches(replay, "a track lookup") == 0
    assert any("catalog skipped: song-only search" in line for line in search_log)


def test_a_burst_of_song_only_searches_is_answered_from_the_library_at_once(
    world: DeliveryWorld, replay: Replay, search_log: list[str]
) -> None:
    """An app can send 68 in 0.163 s after an album is opened: a few different
    song-only searches within a fraction of a second trip the guard at once; the burst's
    first ones are superseded while they settle, so none of them gets catalog songs or
    waits for the catalog."""
    replay.aliases.update({f"burst lookup {n}": RECORDED for n in range(12)})

    def lookup(n: int) -> tuple[dict[str, Any], float]:
        looking = world.client(headers=HOST, client="album-opener")
        try:
            started = time.monotonic()
            return search(looking, f"burst lookup {n}", **SONGS_ONLY), time.monotonic() - started
        finally:
            looking.close()

    with ThreadPoolExecutor(12) as pool:
        answers = list(pool.map(lookup, range(12)))
    assert not any(catalog(found.get("song", [])) for found, _ in answers)
    assert max(took for _, took in answers) < 1.0
    assert sum(searches(replay, f"burst lookup {n}") for n in range(12)) <= 1
    guard = [line for line in search_log if line.startswith("song search guard: album-opener")]
    assert guard == [
        "song search guard: album-opener sent 4 different song-only searches within 0.5s;"
        " answering those from the library alone for at least 2s"
    ]


@pytest.mark.parametrize("gap", [0.1, 0.25])
def test_song_only_searches_typed_one_after_another_get_catalog_songs(
    world: DeliveryWorld, replay: Replay, search_log: list[str], gap: float
) -> None:
    """Typing in a "Tracks" search, each term extending the one before: quickly (0.1 s,
    more terms than the burst's limit within its window) it never trips the song guard -
    they are one search; slowly (0.25 s, every term past its settle) with a slow catalog
    the last term still gets its catalog songs - the earlier terms' lookups go on for the
    cache without taking its slot."""
    typed = ["typ", "type", "typed", "typed t", "typed tr", "typed tracks"]
    replay.aliases.update({t: RECORDED for t in typed})
    replay.slow["search"] = 0.8  # a lookup outlasts several terms
    typing = world.client(headers=HOST, client=f"tracks-typing-{gap}")
    try:
        with ThreadPoolExecutor(len(typed)) as pool:
            pending = []
            for term in typed:
                pending.append(pool.submit(search, typing, term, **SONGS_ONLY))
                time.sleep(gap)
            found = [p.result() for p in pending]
    finally:
        replay.slow.clear()
        typing.close()
    assert catalog(found[-1]["song"])
    assert not any(line.startswith(f"song search guard: tracks-typing-{gap}")
                   for line in search_log)  # fmt: skip


@pytest.mark.usefixtures("song_additions")
def test_a_burst_of_song_only_searches_leaves_the_client_s_searches_alone(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, search_log: list[str]
) -> None:
    """A client looking up each track of an album it opened sends
    a burst of song-only searches; those are answered from the library once the guard trips,
    and the listener's own search from that client still gets its additions."""
    additions = world.services.additions
    assert additions is not None
    previous = additions.search_guard, additions.song_search_guard
    assert previous[1] is not None  # the same settings as the search guard
    assert (previous[1].limit, previous[1].window, previous[1].pause) == (4, 0.5, 2.0)
    now = [0.0]
    additions.search_guard = Bursts(4, 20.0, pause_seconds=60.0, clock=lambda: now[0])
    additions.song_search_guard = Bursts(4, 20.0, pause_seconds=60.0, clock=lambda: now[0])
    looker = world.client(headers=HOST, client="looker")
    exposed: list[list[str]] = []
    additions.exposed = lambda client, ids: exposed.append(list(ids))
    try:
        for n in range(6):
            search(looker, f"track lookup {n}", **SONGS_ONLY)
        assert [searches(replay, f"track lookup {n}") for n in range(6)] == [1, 1, 1, 0, 0, 0]
        guard = [line for line in search_log if "guard: looker" in line]
        assert guard == [
            "song search guard: looker sent 4 different song-only searches within 20s;"
            " answering those from the library alone for at least 60s"
        ]
        assert sum("catalog skipped: song search guard" in line for line in search_log) == 3
        assert exposed == []  # track lookups are not answers a listener sees
        found = search(looker, TERM)  # the listener's own search
        assert catalog(found["album"]) and catalog(found["song"])
        assert not any(line.startswith("search guard: looker") for line in search_log)
        assert exposed  # its albums are queued for matching
    finally:
        additions.search_guard, additions.song_search_guard = previous
        additions.exposed = None
        looker.close()


@pytest.mark.usefixtures("song_additions")
def test_a_song_only_burst_leaves_catalog_lookups_for_a_typed_search(
    world: DeliveryWorld, replay: Replay
) -> None:
    """A client's parallel track lookups take at most one catalog lookup of their
    own (a burst is superseded while it settles and trips the song guard), so a search typed
    meanwhile still gets its additions."""
    replay.aliases["typed after a burst"] = RECORDED
    replay.slow["search"] = 1.0
    typing = world.client(headers=HOST, client="burst-typing")

    def lookup(n: int) -> None:
        looking = world.client(headers=HOST, client="burst-typing")
        try:
            looking.ok("search3", {"query": f"parallel lookup {n}", **SONGS_ONLY})
        finally:
            looking.close()

    try:
        with ThreadPoolExecutor(8) as pool:
            lookups = [pool.submit(lookup, n) for n in range(8)]
            time.sleep(0.05)  # the burst is under way
            found = search(typing, "typed after a burst")
            for lookup in lookups:
                lookup.result()
    finally:
        replay.slow.clear()
        typing.close()
    assert catalog(found["album"])
    assert sum(searches(replay, f"parallel lookup {n}") for n in range(8)) <= 1


@pytest.mark.usefixtures("song_additions")
def test_a_song_only_search_does_not_end_a_typed_search_s_wait(
    world: DeliveryWorld, replay: Replay
) -> None:
    """A client's track lookup arriving while a search it typed waits for the
    catalog does not end that wait (only a newer search of the same kind does)."""
    replay.aliases["typed alone"] = RECORDED
    replay.slow["search"] = 1.0
    typing = world.client(headers=HOST, client="typing-too")
    looking = world.client(headers=HOST, client="typing-too")
    try:
        with ThreadPoolExecutor(1) as pool:
            typed = pool.submit(lambda: search(typing, "typed alone"))
            deadline = time.monotonic() + 2
            while not searches(replay, "typed alone") and time.monotonic() < deadline:
                time.sleep(0.02)  # the typed search is waiting for the catalog
            search(looking, "track lookup while typing", **SONGS_ONLY)
            found = typed.result()
    finally:
        replay.slow.clear()
        typing.close()
        looking.close()
    assert catalog(found["album"])  # not superseded


def test_a_newer_search_ends_the_older_one_s_wait(
    world: DeliveryWorld, replay: Replay, search_log: list[str]
) -> None:
    """Search-as-you-type: results never arrive for a query the client has moved past."""
    replay.aliases["typed once"] = RECORDED
    replay.aliases["typed twice"] = RECORDED
    replay.slow["search"] = 1.5
    typing = world.client(headers=HOST, client="typing")
    later = world.client(headers=HOST, client="typing")
    try:
        with ThreadPoolExecutor(1) as pool:
            started = time.monotonic()
            older = pool.submit(lambda: (search(typing, "typed once"), time.monotonic()))
            deadline = time.monotonic() + 2
            while not searches(replay, "typed once") and time.monotonic() < deadline:
                time.sleep(0.02)  # the older search is waiting for the catalog
            newer = search(later, "typed twice")
            found, answered = older.result()
    finally:
        replay.slow.clear()
        typing.close()
        later.close()
    assert answered - started < 1.0
    assert not catalog(found.get("album", []))  # the library's answer
    assert catalog(newer["album"])
    assert any("catalog superseded" in line for line in search_log)


def twin_search(replay: Replay, term: str) -> None:
    """A search that finds both versions of four songs: the twin albums' first tracks."""
    songs: list[dict[str, Any]] = []
    for name in ("album-twins-explicit", "album-twins-clean"):
        songs += fixture(name)["body"]["tracks"][:4]  # each names its album
    replay.searches[term] = {"path": "search", "status": 200, "body": {"songs": songs}}


@pytest.mark.usefixtures("song_additions")  # it searches songs only
def test_clean_and_explicit_twins_show_as_the_setting_says(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """Songs before anything is committed, and albums, alike."""
    twin_search(replay, "twin songs")
    additions = world.services.additions
    assert additions is not None and additions.twins is Twins.EXPLICIT  # the default

    def songs() -> list[tuple[str, str]]:
        found = search(client, "twin songs", artistCount=0, albumCount=0)
        return [(s["title"], s["explicitStatus"]) for s in catalog(found["song"])]

    def crimson() -> list[str]:
        albums = catalog(search(client, "bright canyon", artistCount=0, songCount=0)["album"])
        return [a["name"] for a in albums if a["name"].startswith("CRIMSON.")]

    titles = ["NORTHERN.", "AMBER.", "AMBER. 2", "RESTLESS."]
    assert songs() == [(t, "explicit") for t in titles]
    assert crimson() == ["CRIMSON."]
    try:
        additions.twins = Twins.CLEAN
        assert songs() == [(t, "clean") for t in titles]
        assert crimson() == ["CRIMSON. (Clean)"]
        additions.twins = Twins.BOTH
        assert sorted(songs()) == sorted([(t, v) for t in titles for v in ("explicit", "clean")])
        assert sorted(crimson()) == ["CRIMSON.", "CRIMSON. (Clean)"]
    finally:
        additions.twins = Twins.EXPLICIT


def test_a_committed_catalog_album_is_not_added_again(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """Runs last: it commits an album (star)."""
    iron = f"sh.al.demo.{item('album-feat-standard')}"
    assert iron in [a["id"] for a in search(client, TERM)["album"]]
    client.ok("star", {"albumId": iron})
    found = search(client, TERM)
    assert iron not in [a["id"] for a in found["album"]]
    name = fixture("album-feat-standard")["body"]["title"]
    assert [a["name"] for a in found["album"]].count(name) == 1  # the library's own
    page = client.ok("getArtist", {"id": library_artist(world)})["artist"]
    assert [a["name"] for a in page["album"]].count(name) == 1


def test_one_entry_that_cannot_be_built_leaves_the_others(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog song whose entry cannot be built is left out (and logged); the
    answer keeps the other additions, and an album view its other tracks."""
    from shijhon.views import additions as additions_module
    from shijhon.views import entries as entries_module

    for module in (additions_module, entries_module):
        real = module.song_entry
        calls = [0]

        def failing(*args: Any, _real: Any = real, _calls: list[int] = calls, **kw: Any) -> Any:
            _calls[0] += 1
            if _calls[0] == 1:
                raise ValueError("unbuildable")
            return _real(*args, **kw)

        monkeypatch.setattr(module, "song_entry", failing)
    client = world.client(headers=HOST, client="broken-entry")
    try:
        with collected("shijhon.views.entries") as lines:
            found = search(client, TERM)
            album_id = str(fixture("album-anniversary-standard")["path"]).split("/")[1]
            album = client.ok("getAlbum", {"id": f"sh.al.demo.{album_id}"})["album"]
    finally:
        client.close()
    assert catalog(found["song"]) and catalog(found["album"])
    tracks = fixture("album-anniversary-standard")["body"]["tracks"]
    assert len(album["song"]) == album["songCount"] == len(tracks) - 1
    assert lines.count("catalog: skipped a malformed song (ValueError)") == 1
    assert lines.count("catalog: skipped a malformed track (ValueError)") == 1
