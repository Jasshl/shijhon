"""Suite C - accounts that get real audio only (``[placeholders] real_audio_accounts``).

An analyzer signs in like any app, lists the library and downloads every song. Its account
sees only songs whose audio is in the library - owned files and delivered audio - in every
list, search and page, with counts that agree; a placeholder, a catalog item and an album
or artist without real audio are what Navidrome does not have; its audio requests never
reach the add-ons or the catalog. An ordinary account keeps the catalog, the placeholders
and their delivery.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from defusedxml import ElementTree

from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.library import simple_album, write_album
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import AuthStyle, SubsonicClient

ANALYZER, PASSWORD = "analyzer", "analyzer-password"
SEARCHED = str(fixture("search-single-vs-album")["params"]["term"])  # catalog results
NS = "{http://subsonic.org/restapi}"


@dataclass
class Library:
    world: DeliveryWorld
    replay: Replay
    addon: FakeAddon
    owned: list[str]  # Owned Record: all owned
    fill_owned: list[str]  # Fill Record's own two songs ...
    fill_added: list[str]  # ... and the two placeholders filled in
    delivered: str  # Catalog Record's first song, its audio delivered
    silent: list[str]  # ... its other two, placeholders
    lone: list[str]  # Silent Record: placeholders only (the second backed by an owned song)
    albums: dict[str, str] = field(default_factory=dict)  # title -> album ID
    artists: dict[str, str] = field(default_factory=dict)  # name -> artist ID
    catalog_song: str = ""  # a catalog song's ID an ordinary search showed

    @property
    def real(self) -> set[str]:
        return {*self.owned, *self.fill_owned, self.delivered}

    @property
    def placeholders(self) -> list[str]:
        return [*self.fill_added, *self.silent, *self.lone]

    def analyzer(self, auth: AuthStyle = "token", **kwargs: Any) -> SubsonicClient:
        # The setting names the account in other letter case: Navidrome's sign-in rule.
        return SubsonicClient(self.world.server.base_url, ANALYZER, PASSWORD, auth=auth, **kwargs)

    def states(self) -> dict[str, str]:
        async def read() -> dict[str, str]:
            rows = await self.world.services.store.fetchall(
                "SELECT song_id, state FROM placeholders"
            )
            return {str(r["song_id"]): str(r["state"]) for r in rows}

        return self.world.server.call(read)


@pytest.fixture(scope="module")
def library(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Library]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    write_album(nd.music, simple_album("Owned Band", "Owned Record", 2))
    write_album(nd.music, simple_album("Fill Band", "Fill Record", 2))
    nd.scan(full=True)
    nd.create_user(ANALYZER, PASSWORD)
    replay = Replay()
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("real-audio"),
        catalog=replay.catalog(),
        real_audio_accounts=["Analyzer"],
    ) as world:
        addon = world.addon("Real source")
        world.add_source(addon)
        found = nd.client().ok("search3", {"query": "Record", "songCount": 50})["searchResult3"]
        albums = {a["name"]: a["id"] for a in found["album"]}
        owned = sorted(s["id"] for s in found["song"] if s["album"] == "Owned Record")
        engine = world.services.engine

        fill = catalog_release("real-fill", "Fill Record", "Fill Band", 4)

        async def filled() -> list[str]:
            album = await engine.owned_album(albums["Fill Record"])
            links = {t.ref: album.songs[t.number - 1]["id"] for t in fill.tracks[:2]}
            result = await engine.materialize(
                fill, owned_album_id=albums["Fill Record"], links=links
            )
            return [result.created[t.ref] for t in fill.tracks[2:]]

        fill_added = world.server.call(filled)
        fill_owned = sorted(s["id"] for s in found["song"] if s["album"] == "Fill Record")
        record = catalog_release("real-catalog", "Catalog Record", "Catalog Band", 3)
        created = world.materialize(record).created
        songs = [created[t.ref] for t in record.tracks]
        for n, track in enumerate(record.tracks[:2]):
            assert track.isrc is not None
            addon.add(FakeTrack(isrc=track.isrc, audio=world.audio(f"real-catalog-{n}")))
        downloaded = world.client().request("download", {"id": songs[0]})  # an ordinary use
        assert downloaded.status_code == 200 and downloaded.content[:4] == b"fLaC"
        lone = catalog_release("real-lone", "Silent Record", "Silent Band", 2)
        lone_created = world.materialize(lone).created
        lone_songs = [lone_created[t.ref] for t in lone.tracks]
        world.server.call(lambda: engine.set_backing(lone_songs[1], owned[0]))
        prepare_collection(nd, world.server)  # the library index, with all of them
        found = nd.client().ok("search3", {"query": "Record", "songCount": 50})["searchResult3"]
        albums = {a["name"]: a["id"] for a in found["album"]}
        artists = {
            a["name"]: a["id"]
            for a in nd.client().ok("search3", {"query": "Band"})["searchResult3"]["artist"]
        }
        shown = world.client().ok("search3", {"query": SEARCHED})["searchResult3"]
        catalog_song = next(s["id"] for s in shown["song"] if s["id"].startswith("sh.tr."))
        library = Library(
            world,
            replay,
            addon,
            owned,
            fill_owned,
            fill_added,
            songs[0],
            songs[1:],
            lone_songs,
            albums,
            artists,
            catalog_song,
        )
        assert library.states()[songs[0]] == "delivered"
        addon.clear()
        replay.log.clear()  # (the ordinary search above)
        yield library


def ok(client: SubsonicClient, method: str, params: Any = (), **kwargs: Any) -> dict[str, Any]:
    """The answer to one request, as an analyzer sends it (it does not ask again after an
    answer that the view is being prepared)."""
    response = client.request(method, params, **kwargs)
    body: dict[str, Any] = response.json()["subsonic-response"]
    assert body["status"] == "ok", (method, body.get("error"))
    return body


def error(client: SubsonicClient, method: str, params: Any = (), **kwargs: Any) -> dict[str, Any]:
    response = client.request(method, params, **kwargs)
    assert response.status_code == 200, (method, response.status_code)
    body: dict[str, Any] = response.json()["subsonic-response"]
    assert body["status"] == "failed", (method, body)
    return dict(body["error"])


def ids(entries: list[dict[str, Any]] | None) -> set[str]:
    return {str(e["id"]) for e in entries or []}


@pytest.mark.parametrize("auth", ["token", "password", "hex"])
def test_listing_every_song_finds_only_real_audio(library: Library, auth: AuthStyle) -> None:
    """The analyzer's listing: an empty search3, page by page (``hex``: ``p=enc:``, as one
    analyzer signs in), then its albums, as it reads them."""
    client, catalog = library.analyzer(auth), len(library.replay.log)
    songs: list[dict[str, Any]] = []
    while True:
        page = ok(
            client,
            "search3",
            {"query": "", "songCount": 2, "songOffset": len(songs), "artistCount": 0},
        )["searchResult3"].get("song", [])
        songs += page
        if len(page) < 2:
            break
    assert ids(songs) == library.real and len(songs) == len(library.real)
    albums = ok(client, "getAlbumList2", {"type": "newest", "size": 500})["albumList2"]["album"]
    assert {a["name"]: a["songCount"] for a in albums} == {
        "Owned Record": 2,
        "Fill Record": 2,
        "Catalog Record": 1,
    }
    for album in albums:
        detail = ok(client, "getAlbum", {"id": album["id"]})["album"]
        assert detail["songCount"] == len(detail["song"]) == album["songCount"]
        assert detail["duration"] == album["duration"]
        assert ids(detail["song"]) <= library.real
    assert library.addon.requests() == [] and len(library.replay.log) == catalog


def test_pages_lists_and_counts_agree(library: Library) -> None:
    client = library.analyzer()
    fill = ok(client, "getAlbum", {"id": library.albums["Fill Record"]})["album"]
    assert ids(fill["song"]) == set(library.fill_owned) and fill["songCount"] == 2
    ordinary = library.world.client()
    whole = ok(ordinary, "getAlbum", {"id": library.albums["Fill Record"]})["album"]
    assert ids(whole["song"]) == {*library.fill_owned, *library.fill_added}  # unchanged
    catalog = ok(client, "getAlbum", {"id": library.albums["Catalog Record"]})["album"]
    assert ids(catalog["song"]) == {library.delivered}
    for kind, ident in (("getAlbum", library.albums["Silent Record"]),
                        ("getArtist", library.artists["Silent Band"]),
                        ("getMusicDirectory", library.albums["Silent Record"])):  # fmt: skip
        assert error(client, kind, {"id": ident})["code"] == 70
    names = {
        a["name"]: a["albumCount"]
        for index in ok(client, "getArtists")["artists"]["index"]
        for a in index["artist"]
    }
    assert names == {"Owned Band": 1, "Fill Band": 1, "Catalog Band": 1}
    artist = ok(client, "getArtist", {"id": library.artists["Catalog Band"]})["artist"]
    assert artist["albumCount"] == 1 and artist["album"][0]["songCount"] == 1
    for kind, ident in (("getAlbumInfo2", library.albums["Silent Record"]),
                        ("getArtistInfo2", library.artists["Silent Band"])):  # fmt: skip
        assert error(client, kind, {"id": ident})["code"] == 70
    ok(client, "getArtistInfo2", {"id": library.artists["Catalog Band"]})
    indexes = ok(client, "getIndexes")["indexes"]["index"]
    assert {a["name"] for i in indexes for a in i["artist"]} == set(names)
    directory = ok(client, "getMusicDirectory", {"id": library.albums["Fill Record"]})
    assert ids(directory["directory"]["child"]) == set(library.fill_owned)
    assert directory["directory"]["songCount"] == 2
    folder = ok(client, "getMusicDirectory", {"id": library.artists["Catalog Band"]})
    assert [(a["title"], a.get("songCount", 1)) for a in folder["directory"]["child"]] == [
        ("Catalog Record", 1)
    ]
    shuffled = ok(client, "getRandomSongs", {"size": 50})["randomSongs"]["song"]
    assert ids(shuffled) == library.real
    genres = ok(client, "getGenres")["genres"].get("genre", [])
    assert sum(g["songCount"] for g in genres) <= len(library.real)
    assert ok(client, "getScanStatus")["scanStatus"]["count"] == len(library.real)
    albums = ok(client, "getAlbumList", {"type": "alphabeticalByName"})["albumList"]["album"]
    assert {a.get("album") or a["title"] for a in albums} == {
        "Owned Record",
        "Fill Record",
        "Catalog Record",
    }
    found = ok(client, "search2", {"query": "Record"})["searchResult2"]
    assert ids(found.get("song")) == library.real
    assert library.addon.requests() == []


def test_searches_get_no_catalog_additions(library: Library) -> None:
    client = library.analyzer()
    before = len(library.replay.log)
    found = ok(client, "search3", {"query": SEARCHED})["searchResult3"]
    assert not any(str(e["id"]).startswith("sh.") for k in found for e in found[k])
    assert ok(client, "search3", {"query": "Silent"})["searchResult3"] == {}
    assert len(library.replay.log) == before  # no catalog request
    ordinary = ok(library.world.client(), "search3", {"query": SEARCHED})["searchResult3"]
    assert any(str(e["id"]).startswith("sh.") for e in ordinary["song"])  # unchanged


def test_placeholders_and_catalog_songs_are_not_found_and_never_delivered(
    library: Library,
) -> None:
    client, catalog = library.analyzer(), len(library.replay.log)
    states = library.states()
    for song in [*library.placeholders, library.catalog_song]:
        for method in ("stream", "download", "getSong", "getLyricsBySongId"):
            assert error(client, method, {"id": song})["code"] == 70, (method, song)
        transcoded = client.request(
            "getTranscodeStream", {"mediaId": song, "mediaType": "song", "transcodeParams": "x"}
        )
        assert transcoded.status_code == 404  # Navidrome's own, for a song it does not have
    assert error(client, "stream", {"id": library.silent[0]}, fmt="json")["message"] == (
        "data not found"
    )
    xml = client.request("stream", {"id": library.silent[0]}, fmt="xml")
    assert b'<error code="70"' in xml.content
    assert library.addon.requests() == [] and len(library.replay.log) == catalog
    assert library.states() == states
    # The ordinary account plays a placeholder from the add-ons, as before (one of a release
    # of its own: no fetch ahead of the next songs reaches the other tests' add-on log).
    song, _, audio = library.world.placeholder_track("real-ordinary", [library.addon])
    played = library.world.client().request("stream", {"id": song})
    assert played.content == audio.read_bytes() and library.addon.requests() != []
    library.addon.clear()


@pytest.mark.parametrize("auth", ["token", "password", "hex"])
def test_real_audio_streams(library: Library, auth: AuthStyle) -> None:
    client = library.analyzer(auth)
    for song in (library.owned[0], library.delivered):
        streamed = client.request("stream", {"id": song})
        direct = library.world.nd.client().request("stream", {"id": song})
        assert streamed.status_code == 200 and streamed.content == direct.content
        assert streamed.content[:4] == b"fLaC"
        part = client.request("stream", {"id": song}, headers={"range": "bytes=10-99"})
        assert part.status_code == 206 and part.content == direct.content[10:100]
        assert client.request("download", {"id": song}).content == direct.content
        assert ok(client, "getSong", {"id": song})["song"]["id"] == song
    assert library.addon.requests() == []


def test_playlists_favorites_and_queues_hold_only_real_audio(library: Library) -> None:
    own = library.world.nd.client(ANALYZER, PASSWORD)  # Navidrome itself: no filter
    silent, real = library.silent, library.owned
    entries = [real[0], silent[0], library.delivered, library.fill_added[0]]
    playlist = own.ok("createPlaylist", {"name": "Mixed", "songId": entries})["playlist"]["id"]
    for song in (real[1], silent[1]):
        own.ok("star", {"id": song})
    albums = [library.albums["Silent Record"], library.albums["Fill Record"]]
    artists = [library.artists["Silent Band"], library.artists["Catalog Band"]]
    own.ok("star", {"albumId": albums, "artistId": artists})
    own.ok("savePlayQueue", {"id": [silent[1], real[1]], "current": silent[1], "position": 5})
    own.ok("createBookmark", {"id": silent[0], "position": 1000})
    own.ok("createBookmark", {"id": real[0], "position": 2000})
    library.world.nd.client().ok("scrobble", {"id": silent[1], "submission": "false"})
    client = library.analyzer()
    try:
        found = ok(client, "getPlaylist", {"id": playlist})["playlist"]
        assert [e["id"] for e in found["entry"]] == [real[0], library.delivered]
        assert found["songCount"] == 2
        assert found["duration"] == sum(e["duration"] for e in found["entry"])
        listed = ok(client, "getPlaylists")["playlists"]["playlist"]
        assert next(p for p in listed if p["id"] == playlist)["songCount"] == 2
        starred = ok(client, "getStarred2")["starred2"]
        assert ids(starred["song"]) == {real[1]}
        assert [(a["name"], a["songCount"]) for a in starred["album"]] == [("Fill Record", 2)]
        assert [(a["name"], a["albumCount"]) for a in starred["artist"]] == [("Catalog Band", 1)]
        old_style = ok(client, "getStarred")["starred"]
        assert ids(old_style["song"]) == {real[1]}
        assert ids(old_style["album"]) == {library.albums["Fill Record"]}
        assert [a["name"] for a in old_style["artist"]] == ["Catalog Band"]
        queue = ok(client, "getPlayQueue")["playQueue"]
        assert ids(queue["entry"]) == {real[1]}
        assert "current" not in queue and "position" not in queue
        bookmarks = ok(client, "getBookmarks")["bookmarks"]["bookmark"]
        assert [b["entry"]["id"] for b in bookmarks] == [real[0]]
        playing = ok(client, "getNowPlaying")["nowPlaying"]
        assert silent[1] not in ids(playing.get("entry"))
        own.ok("savePlayQueueByIndex", {"id": [silent[1], real[1]], "currentIndex": 1})
        by_index = ok(client, "getPlayQueueByIndex")["playQueueByIndex"]
        assert ids(by_index["entry"]) == {real[1]} and by_index["currentIndex"] == 0
        # Removing its second entry as the account counts them: the delivered song.
        ok(client, "updatePlaylist", {"playlistId": playlist, "songIndexToRemove": 1})
        left = own.ok("getPlaylist", {"id": playlist})["playlist"]["entry"]
        assert [e["id"] for e in left] == [real[0], silent[0], library.fill_added[0]]
        body = client.request("getPlaylist", {"id": playlist}, fmt="xml").content
        element = ElementTree.fromstring(body).find(NS + "playlist")
        assert element is not None and element.get("songCount") == "1"
        assert [e.get("id") for e in element.findall(NS + "entry")] == [real[0]]
        body = client.request("getBookmarks", fmt="xml").content
        marks = ElementTree.fromstring(body).find(NS + "bookmarks")
        assert marks is not None and len(marks.findall(NS + "bookmark")) == 1
        asked = {"id": playlist, "f": "jsonp", "callback": "cb"}
        jsonp = client.request("getPlaylist", asked, fmt="xml")  # (no f=json of its own)
        assert jsonp.text.startswith("cb(")
        document = json.loads(jsonp.text[3:-1])["subsonic-response"]["playlist"]
        assert [e["id"] for e in document["entry"]] == [real[0]]
    finally:
        own.ok("deletePlaylist", {"id": playlist})
        own.ok("unstar", {"id": [real[1], silent[1]], "albumId": albums, "artistId": artists})
        for song in (silent[0], real[0]):
            own.ok("deleteBookmark", {"id": song})
    assert library.addon.requests() == []


def test_uses_of_absent_songs_change_nothing(library: Library) -> None:
    """A favorite, a rating, a playlist entry of a placeholder or catalog song: as for a
    song Navidrome does not have - and no release is added to the library."""
    client, catalog = library.analyzer(), len(library.replay.log)
    files = library.world.placeholder_rows()
    ok(client, "star", {"id": library.silent[0]})
    ok(client, "star", {"id": library.catalog_song})
    assert error(client, "setRating", {"id": library.silent[0], "rating": 3})["code"] == 70
    made = ok(
        client,
        "createPlaylist",
        {"name": "Absent", "songId": [library.catalog_song, library.owned[0]]},
    )
    playlist = made["playlist"]
    try:
        assert playlist["songCount"] == 1
        native = library.world.nd.client(ANALYZER, PASSWORD).ok("getStarred2")["starred2"]
        assert native == {}  # nothing starred at Navidrome
    finally:
        ok(client, "deletePlaylist", {"id": playlist["id"]})
    assert library.world.placeholder_rows() == files
    assert library.addon.requests() == [] and len(library.replay.log) == catalog


def test_xml_answers_hold_only_real_audio(library: Library) -> None:
    client = library.analyzer(fmt="xml")
    body = client.request("getAlbum", {"id": library.albums["Fill Record"]}).content
    album = ElementTree.fromstring(body).find(NS + "album")
    assert album is not None and album.get("songCount") == "2"
    assert {s.get("id") for s in album.findall(NS + "song")} == set(library.fill_owned)
    body = client.request("search3", {"query": "", "songCount": 50}).content
    result = ElementTree.fromstring(body).find(NS + "searchResult3")
    assert result is not None
    assert {s.get("id") for s in result.findall(NS + "song")} == library.real


def silence(response: httpx.Response) -> bool:
    """Whether an answer carries audio (a placeholder's silence, or any other)."""
    return response.status_code in (200, 206) and response.content[:4] == b"fLaC"


@pytest.mark.parametrize("auth", ["token", "password", "hex"])
def test_no_parameter_spelling_or_route_reaches_placeholder_audio(
    library: Library, auth: AuthStyle
) -> None:
    """A folder parameter (another library's, one that does not exist, one Navidrome reads
    as this library), the ``.view`` route, a form, a repeated ID, a query Navidrome
    refuses: no placeholder's audio, and nothing asked of the add-ons."""
    client = library.analyzer(auth)
    song = library.silent[0]
    for folder in ("999", "1", "01", "abc", ""):
        params = {"id": song, "musicFolderId": folder}
        for method in ("stream", "download"):
            answer = client.request(method, params)
            assert not silence(answer), (method, folder)
            assert answer.json()["subsonic-response"]["error"]["code"] == 70
        assert error(client, "getSong", params)["code"] == 70
    assert not silence(client.request("stream", {"id": song}, view_suffix=True))
    assert not silence(client.request("stream", {"id": song}, http_method="POST"))
    assert not silence(client.request("stream", [("id", song), ("id", library.owned[0])]))
    owned_first = client.request("stream", [("id", library.owned[0]), ("id", song)])
    assert silence(owned_first)  # Navidrome reads the first: the owned song
    refused = client.request("stream", {"id": song, "x": "a;b"})
    assert not silence(refused)
    assert library.addon.requests() == []


def test_folder_parameters_do_not_reach_the_native_lists(library: Library) -> None:
    client = library.analyzer()
    real_albums = {"Owned Record", "Fill Record", "Catalog Record"}
    for folder in ("1", "01", "+1", "abc"):
        query = {"type": "newest", "size": 500, "musicFolderId": folder}
        albums = ok(client, "getAlbumList2", query)["albumList2"]["album"]
        assert {a["name"] for a in albums} == real_albums, folder
        found = ok(client, "search3", {"query": "", "songCount": 50, "musicFolderId": folder})
        assert ids(found["searchResult3"]["song"]) == library.real, folder
    other = client.request("getAlbumList2", {"type": "newest", "musicFolderId": "999"})
    assert other.json()["subsonic-response"]["status"] == "failed"  # Navidrome's own
    direct = library.world.nd.client(ANALYZER, PASSWORD, salt="mixed", headers={"host": "x"})
    through = direct.with_base(library.world.server.base_url)
    for method in ("getArtists", "getIndexes", "search3", "getAlbumList2"):
        asked = {"type": "newest", "musicFolderId": ["01", "999"]}
        own, mixed = direct.request(method, asked), through.request(method, asked)
        assert mixed.content == own.content and b'"failed"' in own.content, method
    fill = ok(client, "getAlbum", {"id": library.albums["Fill Record"], "musicFolderId": "999"})
    assert ids(fill["album"]["song"]) == set(library.fill_owned)
    # Past 64 bits (Navidrome reads none: every library), and library 1 with 5,000 zeros:
    # an answer like any other, in a query or a form.
    for folder in ("9" * 4301, "0" * 5000 + "1"):
        for method in ("GET", "POST"):
            asked = {"query": "", "songCount": 50, "musicFolderId": folder}
            found = ok(client, "search3", asked, http_method=method)
            assert ids(found["searchResult3"]["song"]) == library.real, (len(folder), method)


def test_a_body_shijhon_does_not_read_gets_the_same_view(library: Library) -> None:
    """A POST whose body is no form and too long to be read: Navidrome reads only its
    query - and the account gets the same list."""
    client = library.analyzer()
    query = [*client.auth_params(), ("f", "json"), ("type", "newest"), ("size", "500")]
    response = httpx.post(
        library.world.server.base_url + "/rest/getAlbumList2",
        params=query,
        content=b"{" + b" " * (2 << 20) + b"}",
        headers={"content-type": "application/json"},
        timeout=30,
    )
    albums = response.json()["subsonic-response"]["albumList2"]["album"]
    assert {a["name"] for a in albums} == {"Owned Record", "Fill Record", "Catalog Record"}


def share_tokens(base_url: str, share: str) -> list[str]:
    """The stream tokens of a share's page."""
    page = httpx.get(f"{base_url}/share/{share}", timeout=30)
    info = re.search(r"__SHARE_INFO__\s*=\s*(\".*?\")\s*</script>", page.text, re.S)
    assert info is not None, page.status_code
    return [str(t["id"]) for t in json.loads(json.loads(info.group(1)))["tracks"]]


def test_sharing_is_off_and_its_old_shares_reach_no_silence(library: Library) -> None:
    client = library.analyzer()
    for method, params in (("getShares", {}), ("createShare", {"id": library.owned[0]}),
                           ("deleteShare", {"id": "x"})):  # fmt: skip
        refused = client.request(method, params)
        assert refused.status_code == 501, method  # as Navidrome with sharing off
    # Shares the account made before (at Navidrome itself).
    own = library.world.nd.client(ANALYZER, PASSWORD)
    album = own.ok("createShare", {"id": library.albums["Fill Record"], "downloadable": "true"})
    silent = own.ok("createShare", {"id": library.silent[1]})
    real = own.ok("createShare", {"id": library.owned[0], "downloadable": "true"})
    album_id, silent_id, real_id = (s["shares"]["share"][0]["id"] for s in (album, silent, real))
    token = share_tokens(library.world.nd.base_url, silent_id)[0]  # (from Navidrome itself)
    base = library.world.server.base_url
    try:
        for path in (f"/share/{album_id}", f"/share/{album_id}/m3u", f"/share/d/{album_id}"):
            answer = httpx.get(base + path, timeout=30)
            assert answer.status_code == 404 and b"Share not found" in answer.content, path
        streamed = httpx.get(f"{base}/share/s/{token}", timeout=30)
        assert not silence(streamed) and streamed.status_code >= 400
        assert httpx.get(f"{base}/share/{real_id}", timeout=30).status_code == 200
        archive = httpx.get(f"{base}/share/d/{real_id}", timeout=30)
        assert archive.status_code == 200 and archive.content[:2] == b"PK"
        assert silence(httpx.get(f"{base}/share/s/{share_tokens(base, real_id)[0]}", timeout=30))
        delivered = own.ok("createShare", {"id": library.delivered})["shares"]["share"][0]["id"]
        try:
            link = f"{base}/share/s/{share_tokens(base, delivered)[0]}"
            played = httpx.get(link, timeout=30)
            direct = library.world.nd.client().request("stream", {"id": library.delivered})
            assert played.status_code == 200 and played.content == direct.content
        finally:
            own.ok("deleteShare", {"id": delivered})
        assert library.addon.requests() == []
        assert library.states()[library.silent[1]] == "placeholder"
        # An ordinary account's share link still gets its audio from the add-ons.
        song, _, _ = library.world.placeholder_track("real-share", [library.addon])
        made = library.world.nd.client().ok("createShare", {"id": song})["shares"]["share"][0]
        played = httpx.get(f"{base}/share/s/{share_tokens(base, made['id'])[0]}", timeout=60)
        assert silence(played) and len(played.content) > 10_000  # (its audio, retagged)
        assert library.addon.requests() != []
    finally:
        for share in (album_id, silent_id, real_id):
            own.ok("deleteShare", {"id": share})
        library.addon.clear()


def token_of(tokens: list[str], song: str) -> str:
    """The stream token of ``song`` among a share page's."""
    for token in tokens:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims.get("id") == song:
            return token
    raise AssertionError(f"no token for {song}")


def test_a_shared_playlist_that_gains_a_placeholder_starts_no_delivery(
    library: Library,
) -> None:
    """The account's share of its playlist, read by a link just before (an ordinary
    account's share link of a placeholder asks which songs these shares hold); the
    playlist then gains a placeholder at Navidrome itself, and the share page gives its
    stream token at once: that stream reaches neither the add-ons nor the silence."""
    own = library.world.nd.client(ANALYZER, PASSWORD)
    base, silent = library.world.server.base_url, library.silent[1]
    playlist = own.ok("createPlaylist", {"name": "Shared", "songId": [library.owned[0]]})
    playlist_id = playlist["playlist"]["id"]
    share = own.ok("createShare", {"id": playlist_id})["shares"]["share"][0]["id"]
    try:
        song, _, _ = library.world.placeholder_track("real-warm", [library.addon])
        made = library.world.nd.client().ok("createShare", {"id": song})["shares"]["share"][0]
        warmed = httpx.get(f"{base}/share/s/{share_tokens(base, made['id'])[0]}", timeout=60)
        assert silence(warmed)  # (an ordinary link: its delivery, as before)
        library.addon.clear()
        own.ok("updatePlaylist", {"playlistId": playlist_id, "songIdToAdd": silent})
        token = token_of(share_tokens(library.world.nd.base_url, share), silent)
        for _ in range(2):
            streamed = httpx.get(f"{base}/share/s/{token}", timeout=60)
            assert not silence(streamed) and streamed.status_code >= 400
        assert library.addon.requests() == []
        assert library.states()[silent] == "placeholder"
    finally:
        own.ok("deleteShare", {"id": share})
        own.ok("deletePlaylist", {"id": playlist_id})
        library.addon.clear()


def test_the_jukebox_is_off_for_the_account(library: Library) -> None:
    interceptor = library.world.services.interceptor
    interceptor._jukebox = (time.monotonic() + 600, True)  # as if Navidrome's were on
    try:
        for action in ("get", "start", "skip", "set"):
            answer = library.analyzer().request(
                "jukeboxControl", {"action": action, "id": library.silent[0], "index": 0}
            )
            assert answer.status_code == 501, action
    finally:
        interceptor._jukebox = None
    assert library.addon.requests() == []
