"""Each account's built-in libraries, as apps see them: whole albums, for each account.

Original imported library holds every whole album - its songs as the library has them,
placeholders with or without audio included - with one of the account's own imports, a
song of a playlist it imported or a song or album it favorited, plus what it added by hand
on the Collection page, less what it removed there. Shijhon library holds the whole
albums of the catalog songs its apps played (a scrobble), favorited, rated, put into a
playlist, bookmarked or downloaded, the audio it prepared on the Downloads page and the
playlists Shijhon published for it (imported or generated). All, the default, holds the
two. Streams, browsing and opening albums bring nothing in, and another account's
activity never counts for an account.

The library is shaped like the owner's: an owned album completed with placeholders (one
delivered), an owned album, a catalog album a playlist import brought in, one a discovery
mix brought in, catalog albums to favorite - through Shijhon and at Navidrome itself -,
a second account, an account that gets real audio only, an account that enabled
everything (its All is the whole library, as before), and one account for each app
action with an album of its own. The owner and the second account use Original imported
library in their apps, for the tests of its rule.
"""

from __future__ import annotations

import html
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import pytest
from aiolimiter import AsyncLimiter

from shijhon.catalog.base import SearchResults
from shijhon.catalog.model import CatalogRef, CatalogRelease, release_from_data
from shijhon.collection.contracts import Query
from tests.acceptance.test_R_playlist_imports import submit
from tests.acceptance.test_R_playlists import create_mix, wait_state
from tests.conftest import NavidromeFactory
from tests.harness import xsd
from tests.harness.collection import prepare_collection, select_catalog
from tests.harness.collection_app import prepared_searches
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, simple_album, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient

NS = "{http://subsonic.org/restapi}"
PASSWORD = "invented-reasons-password"
SECOND, ANALYZER, WHOLE = "second", "analyzer", "whole"
# One account per app action, each with a catalog album of its own.
PLAYER, RATER, LISTER, ADDER, MARKER, DOWNLOADER, FAN, PREPARER = (
    "player",
    "rater",
    "lister",
    "adder",
    "marker",
    "downloader",
    "fan",
    "preparer",
)
# More accounts.
BIG, NATIVE_BIG, REPORTER, SHARER = "big", "nativebig", "reporter", "sharer"
ACTION_ACCOUNTS = (
    PLAYER,
    RATER,
    LISTER,
    ADDER,
    MARKER,
    DOWNLOADER,
    FAN,
    PREPARER,
    BIG,
    NATIVE_BIG,
    REPORTER,
    SHARER,
)
OWNED = Album(
    "Jonah Fairbanks",
    "wild stones",
    (Track("FROZEN", 1, isrc="ZZSHJ0000087"), Track("violet", 2, isrc="ZZSHJ0000088")),
    recording_date="2010",
)
OWNED_ONLY = simple_album("Owned Band", "Owned Record", 2)
FILLED = release_from_data(fixture("album-ep-without-suffix")["body"])  # wild stones, 9
IMPORTED = release_from_data(fixture("album-feat-standard")["body"])  # Iron Wolves
GENERATED = release_from_data(fixture("album-twins-explicit")["body"])  # CRIMSON.
FAVORITE = release_from_data(fixture("album-anniversary-standard")["body"])  # Salt 2
AT_NAVIDROME = catalog_release("reasons-native", "Quiet Harbor", "Invented Lantern", 3)
SECOND_CHOICE = catalog_release("reasons-second", "Second Choice", "Invented Second", 2)


def action_release(key: str, title: str, artist: str) -> CatalogRelease:
    """A two-song catalog album whose recordings can be attributed (catalog item IDs have
    no hyphens)."""
    made = catalog_release(key, title, artist, 2)
    tracks = tuple(replace(t, ref=CatalogRef("test", f"{key}{t.number}")) for t in made.tracks)
    return replace(made, tracks=tracks)


PLAYED = action_release("libplayed", "Played Record", "Invented Player")
BROWSED = action_release("libbrowsed", "Browsed Record", "Invented Browser")
RATED = action_release("librated", "Rated Record", "Invented Rater")
LISTED = action_release("liblisted", "Listed Record", "Invented Lister")
ADDED = action_release("libadded", "Added Record", "Invented Adder")
MARKED = action_release("libmarked", "Marked Record", "Invented Marker")
DOWNLOADED = action_release("libdownloaded", "Downloaded Record", "Invented Downloader")
FAVED = action_release("libfaved", "Faved Record", "Invented Fan")
PREPARED = action_release("libprepared", "Prepared Record", "Invented Preparer")
BIG_LISTED = action_release("libbiglisted", "Long Playlist Record", "Invented Big")
NATIVE_LISTED = action_release("libnativelisted", "Website Playlist Record", "Invented Site")
REPORTED = action_release("libreported", "Reported Record", "Invented Reporter")
SHARED = action_release("libshared", "Shared Record", "Invented Sharer")
ACTION_RELEASES = (
    PLAYED,
    BROWSED,
    RATED,
    LISTED,
    ADDED,
    MARKED,
    DOWNLOADED,
    FAVED,
    PREPARED,
    BIG_LISTED,
    NATIVE_LISTED,
    REPORTED,
    SHARED,
)
MIXED = GENERATED.tracks[:3]  # the discovery mix's songs
Format = Literal["json", "xml"]


@dataclass
class Library:
    world: DeliveryWorld
    owner: str
    albums: dict[str, str]  # title -> album ID
    songs: dict[str, list[str]]  # title -> its songs, as Navidrome has them
    native: dict[str, str]  # catalog song ref -> its library song
    delivered: str
    owned: list[str]  # the original files' songs
    second: str = ""
    ids: dict[str, str] = field(default_factory=dict)  # account name -> its ID
    clients: list[SubsonicClient] = field(default_factory=list)

    def client(self, user: str = ADMIN_USER, password: str = ADMIN_PASSWORD) -> SubsonicClient:
        c = SubsonicClient(self.world.server.base_url, user, password)
        self.clients.append(c)
        return c

    def all_songs(self, *titles: str) -> set[str]:
        return {s for t in titles for s in self.songs[t]}

    def whole(self, *titles: str) -> dict[str, int]:
        """These albums, whole: album ID -> its song count (``listed``)."""
        return {self.albums[t]: len(self.songs[t]) for t in titles}

    def use(self, user: str, profile: str) -> None:
        """The library ``user``'s apps use (``Use in my apps``; an account that never signed
        in to the dashboard has no sign-in to check it with, so set directly)."""

        async def activate() -> None:
            collection = self.world.services.collection
            assert collection is not None
            await collection.profiles.activate(self.ids[user], profile)

        self.world.server.call(activate)


def ok(client: SubsonicClient, method: str, params: Any = (), **kwargs: Any) -> dict[str, Any]:
    """The answer to one request, as an app sends it (apps do not ask again after an
    answer that the view is being prepared)."""
    response = client.request(method, params, **kwargs)
    body: dict[str, Any] = response.json()["subsonic-response"]
    assert body.get("status") == "ok", body
    return body


def entries(client: SubsonicClient, method: str, params: dict[str, Any], fmt: Format) -> Any:
    """The answer's song and album entries by kind (XML: each element's attributes)."""
    container = {
        "getAlbumList2": "albumList2",
        "getAlbumList": "albumList",
        "getRandomSongs": "randomSongs",
    }.get(method.removesuffix(".view"), "searchResult3")
    if fmt == "json":
        found = ok(client, method, params)[container]
        return {kind: list(found.get(kind) or []) for kind in ("album", "song", "artist")}
    answer = client.request(method, params, fmt="xml")
    assert answer.status_code == 200 and xsd.valid(answer.content), answer.text
    root = ET.fromstring(answer.content)  # noqa: S314 - test data
    result = root.find(NS + container)
    assert result is not None, answer.text
    return {
        kind: [dict(e.attrib) for e in result.findall(NS + kind)]
        for kind in ("album", "song", "artist")
    }


def listed(client: SubsonicClient, fmt: Format = "json") -> dict[str, int]:
    """The library's albums (``getAlbumList2``): album ID -> its song count."""
    found = entries(client, "getAlbumList2", {"type": "alphabeticalByName", "size": 500}, fmt)
    return {str(a["id"]): int(a["songCount"]) for a in found["album"]}


def synced(client: SubsonicClient, fmt: Format = "json") -> set[str]:
    """The library's songs (an empty-query ``search3`` sync)."""
    params = {"query": "", "artistCount": 0, "albumCount": 0, "songCount": 500}
    return {str(s["id"]) for s in entries(client, "search3", params, fmt)["song"]}


def shuffled(client: SubsonicClient, fmt: Format = "json") -> set[str]:
    return {str(s["id"]) for s in entries(client, "getRandomSongs", {"size": 500}, fmt)["song"]}


def searched(client: SubsonicClient, term: str, fmt: Format = "json") -> list[dict[str, Any]]:
    params = {"query": term, "artistCount": 5, "albumCount": 5, "songCount": 50}
    songs: list[dict[str, Any]] = entries(client, "search3", params, fmt)["song"]
    return songs


def choose(
    library: Library, refs: list[str], *, saved: bool, user: str = "", profile: str = "original"
) -> str:
    """A hand change of these catalog songs on the Collection page, in ``profile``; its ID
    (for Undo)."""
    owner = user or library.owner

    async def change() -> str:
        collection = library.world.services.collection
        assert collection is not None
        operation = await collection.prepare(
            owner,
            "1",
            Query(view="all", scope="song", origin="catalog"),
            action="save" if saved else "remove",
            selected=["catalog:" + ref for ref in refs],
            request_key="reasons-" + secrets.token_hex(8),
            profile_id=profile,
        )
        await collection.tick()
        await collection.approve(owner, operation)
        for _ in range(10):
            await collection.tick()
            if (await collection.operation(owner, operation))["state"] == "done":
                return operation
        raise AssertionError(await collection.operation(owner, operation))

    return library.world.server.call(change)


def undo(library: Library, operation: str, user: str = "") -> None:
    owner = user or library.owner

    async def back() -> None:
        collection = library.world.services.collection
        assert collection is not None
        ident = await collection.undo(owner, operation)
        for _ in range(10):
            await collection.tick()
            if (await collection.operation(owner, ident))["state"] == "done":
                return
        raise AssertionError(await collection.operation(owner, ident))

    library.world.server.call(back)


def refs(release: CatalogRelease) -> list[str]:
    return [str(t.ref) for t in release.tracks]


def history_mock(world: DeliveryWorld) -> None:
    """A synthetic listening history whose similar songs are the discovery mix's."""
    service = world.services.playlists
    assert service

    async def install() -> None:
        await service.history.http.aclose()

        def response(request: httpx.Request) -> httpx.Response:
            method = request.url.params["method"]
            if method == "user.getRecentTracks":
                body: dict[str, Any] = {
                    "recenttracks": {
                        "@attr": {"totalPages": "1", "total": "1", "perPage": "200", "page": "1"},
                        "track": [
                            {
                                "artist": {"#text": "Invented seed artist"},
                                "name": "Invented seed",
                                "album": {"#text": "Seed release"},
                                "date": {"uts": str(int(time.time()) - 86400)},
                            }
                        ],
                    }
                }
            elif method == "track.getSimilar":
                body = {
                    "similartracks": {
                        "track": [
                            {"name": s.title, "artist": {"name": s.artist}, "match": "1"}
                            for s in MIXED
                        ]
                    }
                }
            else:
                song = next((s for s in MIXED if s.title == request.url.params["track"]), None)
                body = {
                    "track": {
                        "duration": str(song.duration_ms if song else 0),
                        "album": {"title": GENERATED.title if song else "Seed release"},
                    }
                }
            return httpx.Response(200, json=body)

        service.history.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
        service.history.pace = AsyncLimiter(10000, 1)

    world.server.call(install)


@pytest.fixture(scope="module")
def library(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Library]:
    nd = navidrome_factory(env={"ND_ENABLESHARING": "true"})  # for an app's share
    write_album(nd.music, OWNED)
    write_album(nd.music, OWNED_ONLY)
    nd.scan(full=True)
    ids = {user: nd.create_user(user, PASSWORD) for user in (SECOND, ANALYZER, WHOLE)}
    ids |= {user: nd.create_user(user, PASSWORD) for user in ACTION_ACCOUNTS}
    catalog = Replay().catalog()
    searchable = [*IMPORTED.tracks, *GENERATED.tracks]

    async def search(term: str, limit: int = 20) -> SearchResults:
        folded = term.casefold()
        return SearchResults(songs=tuple(s for s in searchable if s.title.casefold() in folded))

    catalog.search = search  # type: ignore[method-assign]
    with delivery_world(
        nd, tmp_path_factory.mktemp("reasons"), catalog=catalog, real_audio_accounts=[ANALYZER]
    ) as world:
        world.app.dashboard.attempts.limit = 10000
        native = nd.client()
        found = native.ok("search3", {"query": OWNED.title, "artistCount": 0, "songCount": 0})
        [album_id] = [a["id"] for a in found["searchResult3"]["album"]]
        engine = world.services.engine

        async def filled() -> None:
            album = await engine.owned_album(album_id)
            links = {t.ref: album.songs[t.number - 1]["id"] for t in FILLED.tracks[:2]}
            await engine.materialize(FILLED, owned_album_id=album_id, links=links)

        world.server.call(filled)
        for release in (FAVORITE, AT_NAVIDROME, SECOND_CHOICE, *ACTION_RELEASES):
            world.materialize(release)
        # A placeholder of the completed album with its audio delivered.
        addon = world.addon("Reasons source")
        world.add_source(addon)
        for track in (FILLED.tracks[2], MIXED[0], DOWNLOADED.tracks[0]):
            assert track.isrc is not None
            addon.add(FakeTrack(isrc=track.isrc, audio=world.audio(track.title)))
        # The playlists: an import of one catalog song, a discovery mix of three others.
        history_mock(world)
        browser = Browser(world.server.base_url)
        try:
            assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
            connected = browser.submit(
                "playlists",
                {
                    "password": ADMIN_PASSWORD,
                    "backend": "none",
                    "history_user": "invented-history",
                    "history_key": "invented-history-key-907",
                    "history_source": "manual",
                },
                "playlists/connection",
            )
            assert connected.status_code == 303, connected.text
            imported = IMPORTED.tracks[0]
            ident, run = submit(
                (world, browser),
                csv=f"artist,title,album,version\n{imported.artist},{imported.title},"
                f"{IMPORTED.title},\n",
            )
            published = browser.submit(f"playlists/import/{ident}", {"action": "publish"})
            assert published.status_code == 303, published.text
            result = wait_state(world, run, ("published", "failed", "uncertain"))
            assert result["state"] == "published", result
            create_mix(world, browser)
        finally:
            browser.close()
        # From now on the catalog also finds the completed album's songs (in the library).
        searchable.extend(FILLED.tracks)
        owner = prepare_collection(nd, world.server)  # the library index, with all of them
        second = prepare_collection(nd, world.server, user=SECOND, password=PASSWORD)
        prepare_collection(nd, world.server, user=WHOLE, password=PASSWORD)
        everything = [
            *refs(FILLED)[2:],
            *refs(IMPORTED),
            *refs(GENERATED),
            *refs(FAVORITE),
            *refs(AT_NAVIDROME),
            *refs(SECOND_CHOICE),
            *(ref for release in ACTION_RELEASES for ref in refs(release)),
        ]
        select_catalog(nd, world.server, everything, user=WHOLE, password=PASSWORD)
        albums = {
            a["name"]: a["id"]
            for a in native.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})[
                "albumList2"
            ]["album"]
        }
        songs = {
            title: [s["id"] for s in native.ok("getAlbum", {"id": ident})["album"]["song"]]
            for title, ident in albums.items()
        }
        assert len(songs[FILLED.title]) == len(FILLED.tracks)

        async def linked() -> dict[str, str]:
            rows = await world.services.store.fetchall("SELECT track_ref, song_id FROM track_links")
            return {str(r["track_ref"]): str(r["song_id"]) for r in rows}

        links = world.server.call(linked)
        downloaded = world.client().request("download", {"id": links[str(FILLED.tracks[2].ref)]})
        assert downloaded.status_code == 200 and downloaded.content[:4] == b"fLaC"
        lib = Library(
            world,
            owner,
            albums,
            songs,
            links,
            links[str(FILLED.tracks[2].ref)],
            [*songs[OWNED_ONLY.title], *songs[FILLED.title][:2]],
            second,
            ids | {ADMIN_USER: owner},
        )
        # The tests of Original imported library's rule list it in apps.
        lib.use(ADMIN_USER, "original")
        lib.use(SECOND, "original")
        yield lib
        for c in lib.clients:
            c.close()


def all_titles() -> tuple[str, ...]:
    """The albums in the owner's Original imported library at the start: those of its
    original files and the one its playlist import brought in."""
    return (FILLED.title, OWNED_ONLY.title, IMPORTED.title)


# The originals (shared until import ownership is reviewed): every account's imports.
ORIGINALS = (FILLED.title, OWNED_ONLY.title)
# The owner's Shijhon library at the start: its imported playlist's and its discovery
# mix's albums (both published for it). (Its download of the completed album's delivered
# song is not attributed: the test audio is shorter than the catalog's recording.)
OWNER_SHIJHON = (IMPORTED.title, GENERATED.title)


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_every_song_of_every_album_in_original_is_listed_found_and_shuffled(
    library: Library, fmt: Format
) -> None:
    client = library.client()
    wanted = {library.albums[t]: len(library.songs[t]) for t in all_titles()}
    assert listed(client, fmt) == wanted  # whole albums, and no other
    songs = library.all_songs(*all_titles())
    assert synced(client, fmt) == songs
    assert shuffled(client, fmt) == songs  # placeholders without audio too
    for title in all_titles():
        album = ok(client, "getAlbum", {"id": library.albums[title]})["album"]
        assert [s["id"] for s in album["song"]] == library.songs[title]
        found = searched(client, title, fmt)
        ids = [s["id"] for s in found]
        assert set(library.songs[title]) <= set(ids), title
        assert len(ids) == len(set(ids))
    # The Collection page counts the same library.
    collection = library.world.services.collection
    assert collection is not None

    async def counted() -> int:
        assert collection is not None
        listing = await collection.browse(
            library.owner,
            "1",
            Query(pool="personal", view="saved", scope="song"),
            profile_id="original",
        )
        return int(listing["total"])

    assert library.world.server.call(counted) == len(songs)


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize(
    "profile,titles",
    [
        ("original", all_titles()),
        ("shijhon", OWNER_SHIJHON),
        ("all", (*all_titles(), GENERATED.title)),
    ],
)
def test_each_library_lists_finds_and_shuffles_every_song_of_its_whole_albums(
    library: Library, profile: str, titles: tuple[str, ...], fmt: Format
) -> None:
    client = library.client()
    library.use(ADMIN_USER, profile)
    try:
        listing = entries(client, "getAlbumList2", {"type": "alphabeticalByName", "size": 500}, fmt)
        assert {str(a["id"]): int(a["songCount"]) for a in listing["album"]} == library.whole(
            *titles
        )
        songs = library.all_songs(*titles)
        assert synced(client, fmt) == songs
        assert shuffled(client, fmt) == songs  # placeholders without audio too
        # The artist index lists the artists of these albums, and no other.
        index = ok(client, "getArtists")["artists"].get("index") or []
        artists = {str(a["id"]) for i in index for a in i.get("artist") or []}
        assert artists == {str(a["artistId"]) for a in listing["album"]}
        for title in titles:
            album = ok(client, "getAlbum", {"id": library.albums[title]})["album"]
            assert [s["id"] for s in album["song"]] == library.songs[title]
            ids = [s["id"] for s in searched(client, title, fmt)]
            assert set(library.songs[title]) <= set(ids), title
            assert len(ids) == len(set(ids))
    finally:
        library.use(ADMIN_USER, "original")


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_a_generated_playlist_s_songs_stay_out_of_original_but_are_found_once_and_play(
    library: Library, fmt: Format
) -> None:
    client = library.client()
    generated = library.all_songs(GENERATED.title)
    assert library.albums[GENERATED.title] not in listed(client, fmt)
    assert not generated & synced(client, fmt)
    assert not generated & shuffled(client, fmt)
    mixed = MIXED[0]
    found = searched(client, mixed.title, fmt)
    named = [s["id"] for s in found if s["title"] == mixed.title]
    assert named == [f"sh.tr.demo.{mixed.ref.id}"]  # its catalog entry, once
    # Songs in All - a placeholder and a song of the imported playlist's album - are found
    # as the library's, with no catalog entry beside them.
    answers = [searched(client, mixed.title, fmt)]
    for song in (FILLED.tracks[4], IMPORTED.tracks[0]):
        found = searched(client, song.title, fmt)
        assert [s["id"] for s in found if s["title"] == song.title] == [
            library.native[str(song.ref)]
        ], song.title
        answers.append(found)
    for answer in answers:
        keys = [(s["title"], s.get("artist")) for s in answer]
        assert len(keys) == len(set(keys))
    played = client.request("stream", {"id": named[0]})
    assert played.status_code == 200 and played.content[:4] == b"fLaC"
    assert not generated & synced(client)  # a play brings nothing in


def test_a_favorite_brings_its_album_in_and_taking_it_back_takes_it_out(
    library: Library,
) -> None:
    client = library.client()
    album, songs = library.albums[FAVORITE.title], library.all_songs(FAVORITE.title)
    one = library.native[str(FAVORITE.tracks[3].ref)]
    assert album not in listed(client)
    client.ok("star", {"id": one})
    try:
        assert listed(client)[album] == len(FAVORITE.tracks)
        assert songs <= synced(client) and songs <= shuffled(client)
        assert listed(client, "xml")[album] == len(FAVORITE.tracks)
    finally:
        client.ok("unstar", {"id": one})
    assert album not in listed(client)
    assert not songs & synced(client)
    # The album itself favorited, by its catalog ID.
    client.ok("star", {"albumId": f"sh.al.demo.{FAVORITE.ref.id}"})
    try:
        assert listed(client)[album] == len(FAVORITE.tracks)
    finally:
        client.ok("unstar", {"albumId": f"sh.al.demo.{FAVORITE.ref.id}"})
    assert album not in listed(client)
    # Taken back, a favorite leaves an album that has another reason.
    imported = library.native[str(IMPORTED.tracks[5].ref)]
    client.ok("star", {"id": imported})
    client.ok("unstar", {"id": imported})
    assert listed(client)[library.albums[IMPORTED.title]] == len(IMPORTED.tracks)


def test_a_favorite_made_at_navidrome_counts_after_the_next_read_of_favorites(
    library: Library,
) -> None:
    reasons = library.world.services.reasons
    assert reasons is not None
    client = library.client()
    album = library.albums[AT_NAVIDROME.title]
    one = library.native[str(AT_NAVIDROME.tracks[0].ref)]
    direct = library.world.nd.client()
    assert album not in listed(client)
    interval = reasons.interval

    async def often(seconds: float) -> None:
        assert reasons is not None
        reasons.interval = seconds
        reasons._due.clear()

    library.world.server.call(lambda: often(0.2))
    try:
        direct.ok("star", {"id": one})  # past Shijhon
        deadline = time.monotonic() + 15
        while album not in listed(client):
            assert time.monotonic() < deadline, "the favorite was not learned"
            time.sleep(0.3)
        assert listed(client)[album] == len(AT_NAVIDROME.tracks)
        direct.ok("unstar", {"id": one})
        deadline = time.monotonic() + 15
        while album in listed(client):
            assert time.monotonic() < deadline, "the taken back favorite was not learned"
            time.sleep(0.3)
    finally:
        direct.ok("unstar", {"id": one})
        library.world.server.call(lambda: often(interval))


def test_an_explicit_removal_wins_over_every_reason(library: Library) -> None:
    client = library.client()
    filled = FILLED.tracks[6]
    gone = library.native[str(filled.ref)]
    removal = choose(library, [str(filled.ref)], saved=False)
    try:
        assert gone not in synced(client) and gone not in shuffled(client)
        assert library.albums[FILLED.title] in listed(client)
        assert synced(client) >= library.all_songs(FILLED.title) - {gone}
        album = ok(client, "getAlbum", {"id": library.albums[FILLED.title]})["album"]
        assert gone in [s["id"] for s in album["song"]]  # the album page stays complete
    finally:
        undo(library, removal)
    assert gone in synced(client)
    # A favorite album removed: it stays out.
    removed = choose(library, refs(FAVORITE), saved=False)
    one = library.native[str(FAVORITE.tracks[0].ref)]
    client.ok("star", {"id": one})
    try:
        assert library.albums[FAVORITE.title] not in listed(client)
        assert not library.all_songs(FAVORITE.title) & synced(client)
    finally:
        client.ok("unstar", {"id": one})
        undo(library, removed)


def test_another_account_has_its_own_reasons(library: Library) -> None:
    owner, second = library.client(), library.client(SECOND, PASSWORD)
    shared = (FILLED.title, OWNED_ONLY.title)  # the library's originals are shared
    one = library.native[str(FAVORITE.tracks[1].ref)]
    owner.ok("star", {"id": one})
    mine = library.native[str(SECOND_CHOICE.tracks[0].ref)]
    second.ok("star", {"id": mine})
    try:
        assert listed(second) == {library.albums[t]: len(library.songs[t]) for t in shared} | {
            library.albums[SECOND_CHOICE.title]: len(SECOND_CHOICE.tracks)
        }
        assert library.albums[SECOND_CHOICE.title] not in listed(owner)
        assert library.albums[FAVORITE.title] in listed(owner)
    finally:
        owner.ok("unstar", {"id": one})
        second.ok("unstar", {"id": mine})
    assert library.albums[SECOND_CHOICE.title] not in listed(second)


def test_an_account_that_gets_real_audio_only_is_unchanged(library: Library) -> None:
    analyzer = library.client(ANALYZER, PASSWORD)
    real = {*library.owned, library.delivered}
    assert synced(analyzer) == real
    assert listed(analyzer) == {
        library.albums[FILLED.title]: 3,
        library.albums[OWNED_ONLY.title]: len(OWNED_ONLY.tracks),
    }
    found = searched(analyzer, MIXED[0].title)
    assert not [s for s in found if s["id"].startswith("sh.")]


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_an_account_whose_all_is_the_whole_library_gets_navidrome_s_lists(
    library: Library, fmt: Format
) -> None:
    whole = library.client(WHOLE, PASSWORD)
    direct = library.world.nd.client(WHOLE, PASSWORD)
    assert listed(whole, fmt) == listed(direct, fmt)
    assert synced(whole, fmt) == synced(direct, fmt)
    assert shuffled(whole, fmt) == synced(direct, fmt)


def test_a_custom_library_holds_what_it_was_made_with(library: Library) -> None:
    client = library.client()
    collection = library.world.services.collection
    assert collection is not None

    async def custom() -> str:
        assert collection is not None
        return await collection.create_profile(library.owner, "Made from Original")

    profile = library.world.server.call(custom)
    library.use(ADMIN_USER, profile)
    one = library.native[str(FAVORITE.tracks[4].ref)]
    try:
        assert synced(client) == library.all_songs(*all_titles())
        client.ok("star", {"id": one})  # a favorite changes Original, not the copy
        assert synced(client) == library.all_songs(*all_titles())
    finally:
        client.ok("unstar", {"id": one})
        library.use(ADMIN_USER, "original")
    assert synced(client) == library.all_songs(*all_titles())


def test_the_collection_page_says_what_each_library_holds(library: Library) -> None:
    browser = Browser(library.world.server.base_url)
    try:
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        page = browser.get("collection")
    finally:
        browser.close()
    assert page.status_code == 200, page.text
    help_text = re.search(r"<summary>How libraries work</summary>(.*?)</details>", page.text, re.S)
    assert help_text is not None
    said = " ".join(re.sub(r"<[^>]+>", "", help_text.group(1)).split())
    assert "All: everything you collected - Original imported library and Shijhon library" in said
    assert (
        "Original imported library: the music you imported, the albums of the playlists you"
        " imported and your favorites." in said
    )
    assert "what your apps played, favorited, rated, put into a playlist, downloaded" in said
    assert "Albums are always whole." in said
    assert "add nothing" not in said and "Your music" not in said


def titles_on(browser: Browser, **params: str) -> list[str]:
    """The album titles the Collection page lists with these filters (its first page)."""
    page = browser.get("collection?" + urlencode({"scope": "album", **params}))
    assert page.status_code == 200, page.text
    return [
        html.unescape(t) for t in re.findall(r'<h3 id="collection-item-\d+">(.*?)</h3>', page.text)
    ]


def test_the_collection_page_filters_show_and_source(library: Library) -> None:
    browser = Browser(library.world.server.base_url)
    try:
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        page = browser.get("collection")
        assert page.status_code == 200
        text = page.text
        for label in ("In this library", "Not in this library", "Everything on the server"):
            assert f">{label}</option>" in text
        for label in ("Any", "Imported", "From the catalog"):
            assert f">{label}</option>" in text
        assert "Original imported library · in my apps</option>" in text
        assert "Used in my apps" in text and "All music on this server" not in text
        assert ">Use in my apps</button>" in browser.get("collection?profile_id=all").text
        # In this library: the library's whole albums.
        shijhon = set(titles_on(browser, profile_id="shijhon"))
        assert set(OWNER_SHIJHON) <= shijhon and OWNED_ONLY.title not in shijhon
        assert set(titles_on(browser, profile_id="original")) == set(all_titles())
        # Not in this library: what the account collected that the library lacks - the
        # generated playlist's album is the owner's, another account's play is not.
        unsaved = set(titles_on(browser, profile_id="original", show="unsaved"))
        assert GENERATED.title in unsaved
        assert not unsaved & {PLAYED.title, SECOND_CHOICE.title, *all_titles()}
        assert set(titles_on(browser, profile_id="shijhon", show="unsaved")) == set(ORIGINALS)
        # Everything on the server: also music only other accounts collected.
        everything = titles_on(browser, profile_id="original", show="server")
        assert PLAYED.title in everything and SECOND_CHOICE.title in everything
        # Source: imported files (and songs an import was linked to), or the catalog.
        imported = set(titles_on(browser, profile_id="original", show="server", origin="imported"))
        assert imported == set(ORIGINALS)
        catalog = titles_on(browser, profile_id="original", show="server", origin="catalog")
        assert OWNED_ONLY.title not in catalog and PLAYED.title in catalog
        assert FILLED.title in catalog  # the songs Shijhon added to complete it
    finally:
        browser.close()


def test_a_real_app_s_library_sequence_lists_every_album_of_its_songs(library: Library) -> None:
    """The library requests a real app made (from its request log, credentials left
    out): it counts the albums by probing single-album pages, reads the album
    lists, the artists and its songs through Navidrome's own API, opens an album and
    favorites a catalog song by its catalog ID. Every album with a song in the song
    listing is in every album listing, before and after; and two searches sent together
    that differ only in a parameter Navidrome ignores are both answered (neither is
    canceled as an outdated typing step)."""
    world = library.world
    app = SubsonicClient(world.server.base_url, ADMIN_USER, ADMIN_PASSWORD, client="Listening app")
    library.clients.append(app)

    def albums(method: str, params: dict[str, Any]) -> set[str]:
        return {str(a["id"]) for a in entries(app, method, params, "json")["album"]}

    listings = [
        ("getAlbumList2.view", {"type": "alphabeticalByName", "size": 50, "offset": 0}),
        ("getAlbumList2.view", {"type": "alphabeticalByName", "size": 5000, "offset": 0}),
        ("getAlbumList2.view", {"type": "newest", "size": 50, "offset": 0}),
        ("getAlbumList2.view", {"type": "random", "size": 20, "offset": 0}),
        ("getAlbumList2.view", {"type": "byYear", "fromYear": 0, "toYear": 3000, "size": 500}),
        ("getAlbumList.view", {"type": "alphabeticalByName", "size": 500}),
        ("search3.view", {"query": "", "artistCount": 0, "albumCount": 500, "songCount": 0}),
    ]
    login = httpx.post(
        world.server.base_url + "/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    native = {"x-nd-authorization": "Bearer " + login.json()["token"]}

    def native_rows(resource: str) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 20
        while True:
            answer = httpx.get(
                world.server.base_url + "/api/" + resource,
                params={"_start": 0, "_end": 500, "_sort": "title" if resource == "song"
                        else "name", "_order": "ASC", "missing": "false"},
                headers=native,
                timeout=10,
            )  # fmt: skip
            if answer.status_code != 503 or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        assert answer.status_code == 200, answer.text
        rows: list[dict[str, Any]] = answer.json()
        return rows

    def invariant() -> set[str]:
        songs = native_rows("song")
        of_songs = {str(s["albumId"]) for s in songs}
        sync = entries(app, "search3.view", {"query": "", "artistCount": 0, "albumCount": 0,
                                             "songCount": 500}, "json")["song"]  # fmt: skip
        assert {str(s["albumId"]) for s in sync} == of_songs
        for method, params in listings:
            assert albums(method, params) == of_songs, (method, params)
        assert {str(a["id"]) for a in native_rows("album")} == of_songs
        # The album count, as the app finds it: single-album pages until one is empty.
        for offset in (1000, 500, 250, 125, 62, 31, 15, 7, 3, 1, 2):
            page = albums("getAlbumList2.view", {"type": "alphabeticalByName", "size": 1,
                                                 "offset": offset})  # fmt: skip
            assert len(page) == (1 if offset < len(of_songs) else 0), offset
        return of_songs

    before = invariant()
    assert before == {library.albums[t] for t in all_titles()}
    album = library.albums[IMPORTED.title]
    assert len(ok(app, "getAlbum.view", {"id": album})["album"]["song"]) == len(IMPORTED.tracks)
    ok(app, "getAlbumInfo2.view", {"id": album})
    assert invariant() == before
    # Two searches at once, one with a parameter Navidrome does not read, answered
    # directly and then prepared (as results of 500 or more are): the second arrives
    # while the first one's lists are still being prepared, and joins them.
    written = world.services.written
    assert written is not None and written.query_index is not None
    term = {"query": "Oren Garrow Slow Voices", "artistCount": 30, "albumCount": 30,
            "songCount": 50}  # fmt: skip
    index = written.query_index

    def until(check: Any) -> None:
        deadline = time.monotonic() + 10
        while not check():
            assert time.monotonic() < deadline, "the searches did not wait together"
            time.sleep(0.01)

    def waiters(built: list[str], count: int) -> bool:
        return len(built) == 3 and all(len(index.waiting.get(k, ())) == count for k in built)

    for prepared in (False, True):
        hold = threading.Event()
        with (
            prepared_searches(index, hold=hold) if prepared else nullcontext([]) as built,
            ThreadPoolExecutor(2) as pool,
        ):
            skip = {**term, "appSkipExternal": "true"}
            try:
                first = pool.submit(app.request, "search3.view", skip)
                if prepared:  # its three kinds are being prepared when the second arrives
                    until(lambda: waiters(built, 1))
                else:
                    time.sleep(0.05)
                second = pool.submit(app.request, "search3.view", term)
                if prepared:  # and it waits for the same three
                    until(lambda: waiters(built, 2))
            finally:
                hold.set()
            answers = [first.result(), second.result()]
        assert [a.status_code for a in answers] == [200, 200], [a.text[:200] for a in answers]
        found = [{s["id"] for s in a.json()["subsonic-response"]["searchResult3"]["song"]}
                 for a in answers]  # fmt: skip
        assert found[0] == found[1] and library.native[str(IMPORTED.tracks[1].ref)] in found[0]
        assert len(built) == len(set(built)) == (3 if prepared else 0)
    # A catalog song favorited by its catalog ID brings its album into every listing.
    favorite = f"sh.tr.demo.{FAVORITE.tracks[2].ref.id}"
    assert ok(app, "star.view", {"id": favorite})["status"] == "ok"
    try:
        assert invariant() == before | {library.albums[FAVORITE.title]}
    finally:
        ok(app, "unstar.view", {"id": library.native[str(FAVORITE.tracks[2].ref)]})
    assert invariant() == before


# --- what each app action brings in ----------------------------------------------------


def libraries_of(library: Library, user: str, fmt: Format = "json") -> dict[str, dict[str, int]]:
    """What each built-in library holds for ``user``, as its apps list it."""
    client = library.client(user, PASSWORD)
    held = {}
    try:
        for profile in ("original", "shijhon", "all"):
            library.use(user, profile)
            held[profile] = listed(client, fmt)
    finally:
        library.use(user, "all")
    return held


def test_a_play_brings_its_whole_album_in_and_a_stream_or_browsing_does_not(
    library: Library,
) -> None:
    player = library.client(PLAYER, PASSWORD)
    # Streamed without a play reported, opened, looked up, searched: nothing comes in.
    streamed = player.request("stream", {"id": f"sh.tr.demo.{MIXED[0].ref.id}"})
    assert streamed.status_code == 200 and streamed.content[:4] == b"fLaC"
    ok(player, "getAlbum", {"id": library.albums[BROWSED.title]})
    ok(player, "getSong", {"id": library.native[str(BROWSED.tracks[0].ref)]})
    for fmt in ("json", "xml"):
        searched(player, BROWSED.title, fmt)
        assert listed(player, fmt) == library.whole(*ORIGINALS)
    # A play the app reports: the whole album, in Shijhon library and All.
    ok(player, "scrobble", {"id": library.native[str(PLAYED.tracks[1].ref)], "submission": "true"})
    for fmt in ("json", "xml"):
        assert libraries_of(library, PLAYER, fmt) == {
            "original": library.whole(*ORIGINALS),
            "shijhon": library.whole(PLAYED.title),
            "all": library.whole(*ORIGINALS, PLAYED.title),
        }
        assert synced(player, fmt) == library.all_songs(*ORIGINALS, PLAYED.title)
        assert shuffled(player, fmt) == library.all_songs(*ORIGINALS, PLAYED.title)
        # Another account's play changes nothing for the others.
        for other in (RATER, SECOND):
            assert library.albums[PLAYED.title] not in listed(library.client(other, PASSWORD), fmt)


ACTIONS = {
    "rating": (RATER, RATED, "setRating", {"rating": "4"}),
    "playlist": (LISTER, LISTED, "createPlaylist", {"name": "Made in an app"}),
    "bookmark": (MARKER, MARKED, "createBookmark", {"position": "1000"}),
}


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_an_app_s_rating_playlist_or_bookmark_brings_its_whole_album_in(
    library: Library, action: str
) -> None:
    user, release, method, params = ACTIONS[action]
    client = library.client(user, PASSWORD)
    song = library.native[str(release.tracks[0].ref)]
    key = "songId" if method == "createPlaylist" else "id"
    ok(client, method, {**params, key: song})
    assert libraries_of(library, user) == {
        "original": library.whole(*ORIGINALS),
        "shijhon": library.whole(release.title),
        "all": library.whole(*ORIGINALS, release.title),
    }


def test_a_song_added_to_an_app_s_playlist_brings_its_whole_album_in(library: Library) -> None:
    client = library.client(ADDER, PASSWORD)
    made = ok(client, "createPlaylist", {"name": "Empty at first"})["playlist"]["id"]
    assert library.albums[ADDED.title] not in listed(client)
    song = library.native[str(ADDED.tracks[1].ref)]
    ok(client, "updatePlaylist", {"playlistId": made, "songIdToAdd": song})
    assert libraries_of(library, ADDER)["shijhon"] == library.whole(ADDED.title)
    assert listed(client) == library.whole(*ORIGINALS, ADDED.title)


def test_a_download_brings_its_whole_album_in(library: Library) -> None:
    client = library.client(DOWNLOADER, PASSWORD)
    song = library.native[str(DOWNLOADED.tracks[0].ref)]
    downloaded = client.request("download", {"id": song})
    assert downloaded.status_code == 200 and downloaded.content[:4] == b"fLaC"
    assert libraries_of(library, DOWNLOADER) == {
        "original": library.whole(*ORIGINALS),
        "shijhon": library.whole(DOWNLOADED.title),
        "all": library.whole(*ORIGINALS, DOWNLOADED.title),
    }


def test_a_favorite_leaves_original_when_taken_back_and_stays_in_shijhon_library(
    library: Library,
) -> None:
    fan = library.client(FAN, PASSWORD)
    one = library.native[str(FAVED.tracks[0].ref)]
    fan.ok("star", {"id": one})
    try:
        assert libraries_of(library, FAN) == {
            "original": library.whole(*ORIGINALS, FAVED.title),
            "shijhon": library.whole(FAVED.title),
            "all": library.whole(*ORIGINALS, FAVED.title),
        }
    finally:
        fan.ok("unstar", {"id": one})
    # Taken back: out of Original imported library; Shijhon library keeps what it added.
    assert libraries_of(library, FAN) == {
        "original": library.whole(*ORIGINALS),
        "shijhon": library.whole(FAVED.title),
        "all": library.whole(*ORIGINALS, FAVED.title),
    }


def test_audio_prepared_on_the_downloads_page_brings_its_album_in(library: Library) -> None:
    world = library.world
    service = world.services.downloads
    assert service is not None
    owner = library.ids[PREPARER]
    song = library.native[str(PREPARED.tracks[0].ref)]
    # A playlist made at Navidrome itself: only the preparation is Shijhon's to see.
    made = world.nd.client(PREPARER, PASSWORD).ok(
        "createPlaylist", {"name": "To prepare", "songId": song}
    )["playlist"]["id"]
    assert library.albums[PREPARED.title] not in listed(library.client(PREPARER, PASSWORD))

    async def prepare() -> None:
        assert service is not None
        await service.connect(owner, PREPARER, PASSWORD, "127.0.0.1")
        await service.prepare(owner, PREPARER, made)

    world.server.call(prepare)
    assert libraries_of(library, PREPARER) == {
        "original": library.whole(*ORIGINALS),
        "shijhon": library.whole(PREPARED.title),
        "all": library.whole(*ORIGINALS, PREPARED.title),
    }


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_a_hand_removal_wins_in_its_library_and_in_all(library: Library, fmt: Format) -> None:
    client = library.client()
    gone = library.native[str(GENERATED.tracks[1].ref)]
    removal = choose(library, [str(GENERATED.tracks[1].ref)], saved=False, profile="shijhon")
    try:
        for profile in ("shijhon", "all"):
            library.use(ADMIN_USER, profile)
            assert gone not in synced(client, fmt) and gone not in shuffled(client, fmt)
            assert library.albums[GENERATED.title] in listed(client, fmt)
            album = ok(client, "getAlbum", {"id": library.albums[GENERATED.title]})["album"]
            assert gone in [s["id"] for s in album["song"]]  # the album page stays complete
    finally:
        library.use(ADMIN_USER, "original")
        undo(library, removal)
    library.use(ADMIN_USER, "all")
    try:
        assert gone in synced(client, fmt)
    finally:
        library.use(ADMIN_USER, "original")


# --- playlist additions larger than one request ------------------------------------------


def wait_for(check: Any, seconds: float = 20) -> None:
    """Until ``check()`` holds: the kept actions are recorded beside the request."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "the kept action was not recorded"
        time.sleep(0.2)


def test_an_accepted_playlist_of_501_entries_is_collected(library: Library) -> None:
    """Navidrome accepts a playlist of 501 entries (here one song, 501
    times); its album joins the account's Shijhon library."""
    client = library.client(BIG, PASSWORD)
    song = library.native[str(BIG_LISTED.tracks[0].ref)]
    made = ok(client, "createPlaylist", [("name", "Large invented playlist")]
              + [("songId", song)] * 501)  # fmt: skip
    assert made["playlist"]["songCount"] == 501
    assert libraries_of(library, BIG)["shijhon"] == library.whole(BIG_LISTED.title)


def test_a_native_website_addition_of_501_songs_is_collected(library: Library) -> None:
    """On the native website: an addition larger than one request
    confirms is kept and recorded once Navidrome added every song."""
    world = library.world
    client = library.client(NATIVE_BIG, PASSWORD)
    song = library.native[str(NATIVE_LISTED.tracks[1].ref)]
    login = world.nd.login(NATIVE_BIG, PASSWORD)
    headers = {"x-nd-authorization": "Bearer " + login["token"]}
    made = client.http.post(
        client.base_url + "/api/playlist", headers=headers, json={"name": "Website playlist"}
    ).json()
    added = client.http.post(
        client.base_url + "/api/playlist/" + made["id"] + "/tracks",
        headers=headers,
        json={"ids": [song] * 501},
    )
    assert added.status_code == 200 and added.json()["added"] == 501
    library.use(NATIVE_BIG, "shijhon")
    try:
        wait_for(lambda: listed(client) == library.whole(NATIVE_LISTED.title))
    finally:
        library.use(NATIVE_BIG, "all")


def test_a_playback_report_and_a_share_bring_their_whole_albums_in(library: Library) -> None:
    reporter = library.client(REPORTER, PASSWORD)
    song = library.native[str(REPORTED.tracks[0].ref)]
    ok(reporter, "reportPlayback",
       {"mediaId": song, "mediaType": "song", "positionMs": "0", "state": "playing"})  # fmt: skip
    assert libraries_of(library, REPORTER)["shijhon"] == library.whole(REPORTED.title)
    sharer = library.client(SHARER, PASSWORD)
    ok(sharer, "createShare", {"id": library.albums[SHARED.title]})
    assert libraries_of(library, SHARER)["shijhon"] == library.whole(SHARED.title)
