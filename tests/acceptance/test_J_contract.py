"""Suite J (continued) - catalog entries decode like Navidrome's own.

Strict OpenSubsonic clients reject a whole answer when one entry misses a key the
type always has or has a value of another type ("Data error: search failed"). For every
endpoint that returns catalog entries - search3, getAlbum, getSong, getArtist (catalog
artists and library artists' pages with additions) - each catalog entry must have the
keys Navidrome 0.64.2 always emits for its type and every key its own entries of that type
have in the same answer, with the same JSON types; every artist reference has an ID.
Lists (getAlbumList2, getRandomSongs, getStarred2) carry no catalog entries: Navidrome
answers them alone.

The schema is Navidrome 0.64.2's (``server/subsonic/responses/responses.go``: Child,
AlbumID3, ArtistID3, ArtistID3Ref; OpenSubsonic fields included, as Navidrome emits them
for clients that are not "legacy"); Navidrome's own entries are checked against it too.
Documented exceptions, keys a catalog entry may lack although Navidrome's have them:
the listener's annotations (starred, playCount, played, userRating on songs,
averageRating, bookmarkPosition) and transcoding hints (a player with a transcoding
profile sees catalog songs as FLAC until they are in the library). Every artist ID an
entry carries opens (getArtist), also when the catalog named no artist items.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient

HOST = {"host": "music.test"}
ARTIST = "Oren Garrow"  # the duo fixtures' artist; the library has some of their music
TERM = ARTIST.lower()
OWNED = [
    # With a cover: Navidrome drops an album's coverArt a few seconds after a scan when it
    # finds no artwork, and the directories are compared key for key.
    Album(
        ARTIST,
        "Hollow 2",
        (Track("One", 1), Track("Two", 2)),
        recording_date="1995",
        cover_file=True,
    ),
    Album(ARTIST, "Open Lanterns", (Track("Open Lanterns", 1, isrc="ZZSHJ0000228"),)),
]

STR, INT, BOOL, LIST, OBJ, NUM = str, int, bool, list, dict, (int, float)
REF = {"id": STR, "name": STR}
ALWAYS_REF = set(REF)
SONG: dict[str, Any] = {
    # always
    "id": STR,
    "isDir": BOOL,
    "title": STR,
    "bpm": INT,
    "comment": STR,
    "sortName": STR,
    "mediaType": STR,
    "musicBrainzId": STR,
    "isrc": LIST,
    "genres": LIST,
    "replayGain": OBJ,
    "channelCount": INT,
    "samplingRate": INT,
    "bitDepth": INT,
    "moods": LIST,
    "artists": LIST,
    "displayArtist": STR,
    "albumArtists": LIST,
    "displayAlbumArtist": STR,
    "contributors": LIST,
    "displayComposer": STR,
    "explicitStatus": STR,
    "groupings": LIST,
    "works": LIST,
    "movements": LIST,
    # omitted when empty
    "parent": STR,
    "name": STR,
    "album": STR,
    "artist": STR,
    "track": INT,
    "year": INT,
    "genre": STR,
    "coverArt": STR,
    "size": INT,
    "contentType": STR,
    "suffix": STR,
    "starred": STR,
    "transcodedContentType": STR,
    "transcodedSuffix": STR,
    "duration": INT,
    "bitRate": INT,
    "path": STR,
    "playCount": INT,
    "discNumber": INT,
    "created": STR,
    "albumId": STR,
    "artistId": STR,
    "type": STR,
    "userRating": INT,
    "averageRating": NUM,
    "songCount": INT,
    "isVideo": BOOL,
    "bookmarkPosition": INT,
    "played": STR,
}
ALWAYS_SONG = {
    "id",
    "isDir",
    "title",
    "bpm",
    "comment",
    "sortName",
    "mediaType",
    "musicBrainzId",
    "isrc",
    "genres",
    "replayGain",
    "channelCount",
    "samplingRate",
    "bitDepth",
    "moods",
    "artists",
    "displayArtist",
    "albumArtists",
    "displayAlbumArtist",
    "contributors",
    "displayComposer",
    "explicitStatus",
    "groupings",
    "works",
    "movements",
}
ALBUM: dict[str, Any] = {
    "id": STR,
    "name": STR,
    "songCount": INT,
    "duration": INT,
    "created": STR,
    "userRating": INT,
    "genres": LIST,
    "musicBrainzId": STR,
    "isCompilation": BOOL,
    "sortName": STR,
    "discTitles": LIST,
    "originalReleaseDate": OBJ,
    "releaseDate": OBJ,
    "releaseTypes": LIST,
    "recordLabels": LIST,
    "moods": LIST,
    "artists": LIST,
    "displayArtist": STR,
    "explicitStatus": STR,
    "version": STR,
    "artist": STR,
    "artistId": STR,
    "coverArt": STR,
    "playCount": INT,
    "starred": STR,
    "year": INT,
    "genre": STR,
    "played": STR,
    "averageRating": NUM,
    "song": LIST,
}
ALWAYS_ALBUM = {
    "id",
    "name",
    "songCount",
    "duration",
    "created",
    "userRating",
    "genres",
    "musicBrainzId",
    "isCompilation",
    "sortName",
    "discTitles",
    "originalReleaseDate",
    "releaseDate",
    "releaseTypes",
    "recordLabels",
    "moods",
    "artists",
    "displayArtist",
    "explicitStatus",
    "version",
}
ARTIST_TYPE: dict[str, Any] = {
    "id": STR,
    "name": STR,
    "albumCount": INT,
    "musicBrainzId": STR,
    "sortName": STR,
    "roles": LIST,
    "coverArt": STR,
    "starred": STR,
    "userRating": INT,
    "averageRating": NUM,
    "artistImageUrl": STR,
    "album": LIST,
}
ALWAYS_ARTIST = {"id", "name", "albumCount", "musicBrainzId", "sortName", "roles"}
# Keys a catalog entry may lack although Navidrome's own entries have them.
EXCEPTIONS = {
    "starred",
    "playCount",
    "played",
    "userRating",
    "averageRating",
    "bookmarkPosition",
    "transcodedContentType",
    "transcodedSuffix",
}
TYPES = {
    "song": (SONG, ALWAYS_SONG),
    "album": (ALBUM, ALWAYS_ALBUM),
    "artist": (ARTIST_TYPE, ALWAYS_ARTIST),
}


def item(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def catalog_entry(entry: dict[str, Any]) -> bool:
    return str(entry.get("id", "")).startswith("sh.")


def problems(kind: str, entry: dict[str, Any], expected: set[str] = frozenset()) -> list[str]:  # type: ignore[assignment]
    """What makes ``entry`` decode differently from Navidrome's entries of ``kind``."""
    schema, always = TYPES[kind]
    found = []
    for key in sorted((always | expected) - set(entry)):
        found.append(f"{kind} {entry.get('id')}: no {key}")
    for key, value in entry.items():
        wanted = schema.get(key)
        if wanted is None:
            found.append(f"{kind} {entry.get('id')}: unknown key {key}")
        elif isinstance(value, bool) != (wanted is BOOL) or not isinstance(value, wanted):
            found.append(f"{kind} {entry.get('id')}: {key} is {type(value).__name__}")
    for key in ("artists", "albumArtists"):
        for ref in entry.get(key) or []:
            if (
                set(ref) - set(REF)
                or ALWAYS_REF - set(ref)
                or not all(isinstance(ref.get(k), str) for k in REF)
                or not ref.get("id")
            ):
                found.append(f"{kind} {entry.get('id')}: {key} item {ref}")
    found += inner_types(kind, entry)
    nested = {"album": "song", "artist": "album"}.get(kind)  # getAlbum's songs, getArtist's albums
    if nested is not None:
        entries = entry.get(nested) or []
        for child in entries:
            found += problems(nested, child, expected_keys(entries, nested))
    return found


NESTED: dict[str, Any] = {
    # key: the type of each element (lists) or of each value (objects); dicts: their keys
    "isrc": STR, "moods": STR, "groupings": STR, "releaseTypes": STR, "roles": STR,
    "genres": {"name": STR}, "recordLabels": {"name": STR}, "works": {"name": STR},
    "movements": {"name": STR, "number": INT, "count": INT},
    "discTitles": {"disc": INT, "title": STR, "coverArt": STR},
    "contributors": {"role": STR, "subRole": STR, "artist": OBJ},
    "releaseDate": INT, "originalReleaseDate": INT, "replayGain": NUM,
}  # fmt: skip
DATE_KEYS = {"year", "month", "day"}


def inner_types(kind: str, entry: dict[str, Any]) -> list[str]:
    """Elements and values inside the entry's lists and objects have Navidrome's types."""
    found = []
    for key, wanted in NESTED.items():
        value = entry.get(key)
        items = value.values() if isinstance(value, dict) else value or []
        dated = key in ("releaseDate", "originalReleaseDate") and isinstance(value, dict)
        if dated and set(value) - DATE_KEYS:
            found.append(f"{kind} {entry.get('id')}: {key} keys {sorted(value)}")
        for item in items:
            if isinstance(wanted, dict):
                if not isinstance(item, dict) or any(
                    k not in wanted or not isinstance(v, wanted[k]) for k, v in item.items()
                ):
                    found.append(f"{kind} {entry.get('id')}: {key} item {item}")
            elif isinstance(item, bool) or not isinstance(item, wanted):
                found.append(f"{kind} {entry.get('id')}: {key} item {item!r}")
    created = entry.get("created")
    if isinstance(created, str):
        try:
            datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            found.append(f"{kind} {entry.get('id')}: created {created}")
    return found


def expected_keys(entries: list[dict[str, Any]], kind: str) -> set[str]:
    """Keys every one of Navidrome's own entries of this kind has here (the exceptions
    aside): a catalog entry must have them too."""
    own = [set(e) for e in entries if not catalog_entry(e)]
    common = set.intersection(*own) if own else set()
    return common - EXCEPTIONS - {"song", "album"}


def check(kind: str, entries: list[dict[str, Any]]) -> tuple[int, int]:
    """(Navidrome's entries, catalog entries) checked; fails with every problem."""
    expected = expected_keys(entries, kind)
    found: list[str] = []
    own = [e for e in entries if not catalog_entry(e)]
    added = [e for e in entries if catalog_entry(e)]
    for entry in own:
        found += [f"Navidrome's {p}" for p in problems(kind, entry)]  # the schema is right
    for entry in added:
        found += problems(kind, entry, expected)
    assert found == []
    return len(own), len(added)


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases[TERM] = str(fixture("search-single-vs-album")["params"]["term"])
    replay.aliases["wren loring"] = str(fixture("search-covers")["params"]["term"])
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
    with delivery_world(nd, tmp_path_factory.mktemp("contract"), catalog=replay.catalog()) as w:
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client(headers=HOST, client="strict-app")
    yield c
    c.close()


def library_album(world: DeliveryWorld, client: SubsonicClient) -> dict[str, Any]:
    found = world.nd.client().ok("search3", {"query": "Hollow 2", "artistCount": 0})
    ident = found["searchResult3"]["album"][0]["id"]
    return dict(client.ok("getAlbum", {"id": ident})["album"])


def test_search_results(client: SubsonicClient) -> None:
    found = client.ok("search3", {"query": TERM})["searchResult3"]
    counts = {kind: check(kind, found.get(kind, [])) for kind in ("artist", "album", "song")}
    assert counts["album"][0] and counts["album"][1]  # both kinds were compared
    assert counts["song"][0] and counts["song"][1]
    # Artists the library does not have: catalog artist entries.
    other = client.ok("search3", {"query": "wren loring"})["searchResult3"]
    assert check("artist", other.get("artist", []))[1]
    check("album", other.get("album", []))
    check("song", other.get("song", []))


def test_entries_without_the_catalog_s_artist_items(client: SubsonicClient, replay: Replay) -> None:
    """The artist lookup fails (rate limited): entries still decode, and their artists
    are referenced through the songs and albums."""
    replay.aliases["uma okafor"] = str(fixture("search-soundtrack")["params"]["term"])
    replay.failing.update({"songs": 429, "albums": 429})
    try:
        found = client.ok("search3", {"query": "uma okafor"})["searchResult3"]
    finally:
        replay.failing.clear()
    assert check("song", found.get("song", []))[1] and check("album", found.get("album", []))[1]
    ids = [p["id"] for s in found["song"] if catalog_entry(s) for p in s["artists"]]
    assert any(".t-" in i for i in ids)


def test_every_artist_id_handed_out_opens(client: SubsonicClient) -> None:
    album = client.ok("getAlbum", {"id": f"sh.al.demo.{item('album-feat-standard')}"})["album"]
    ids = {album["artistId"], *(p["id"] for p in album["artists"])}
    for song in album["song"]:
        ids |= {song["artistId"], *(p["id"] for p in song["artists"] + song["albumArtists"])}
    assert len(ids) > 1
    for ident in sorted(ids):
        assert client.ok("getArtist", {"id": ident})["artist"]["name"], ident


def test_catalog_albums_and_songs(world: DeliveryWorld, client: SubsonicClient) -> None:
    own = library_album(world, client)
    for name in ("album-feat-standard", "album-ep-without-suffix", "album-twins-clean"):
        album = client.ok("getAlbum", {"id": f"sh.al.demo.{item(name)}"})["album"]
        assert catalog_entry(album) and album["song"]
        check("album", [own, album])
        check("song", [*own["song"], *album["song"]])
    song = client.ok("getSong", {"id": f"sh.tr.demo.{item('song-duo-feat')}"})["song"]
    own_song = client.ok("getSong", {"id": own["song"][0]["id"]})["song"]
    assert check("song", [own_song, song]) == (1, 1)
    assert len(song["artists"]) == 2  # "A feat. B": two artists, each with an ID


def test_artist_pages(world: DeliveryWorld, client: SubsonicClient) -> None:
    own_artist = world.nd.client().ok("search3", {"query": TERM, "albumCount": 0})
    ident = own_artist["searchResult3"]["artist"][0]["id"]
    page = client.ok("getArtist", {"id": ident})["artist"]  # with catalog additions
    assert any(catalog_entry(a) for a in page["album"])
    check("album", page["album"])
    other = client.ok("getArtist", {"id": f"sh.ar.demo.{item('artist-band')}"})["artist"]
    assert catalog_entry(other) and other["album"]
    check("artist", [page, other])


def test_lists_carry_no_catalog_entries(client: SubsonicClient) -> None:
    for method, key, kind in (
        ("getAlbumList2", "albumList2", "album"),
        ("getRandomSongs", "randomSongs", "song"),
        ("getStarred2", "starred2", "song"),
    ):
        answer = client.ok(method, {"type": "newest", "size": 50})[key]
        assert not any(catalog_entry(e) for e in answer.get(kind, []))
        check(kind, answer.get(kind, []))


# --- catalog IDs in every method ---------------------------------------------------

EMPTY_METHODS = ("getLyricsBySongId", "getSimilarSongs", "getSimilarSongs2")


def own_song(world: DeliveryWorld) -> str:
    found = world.nd.client().ok("search3", {"query": "Hollow 2", "artistCount": 0})
    return str(found["searchResult3"]["song"][0]["id"])


@pytest.mark.parametrize("fmt", ["json", "xml", "jsonp"])
def test_read_only_methods_answer_a_catalog_song_like_navidrome_answers_empty(
    world: DeliveryWorld, client: SubsonicClient, fmt: str
) -> None:
    """No lyrics, no similar songs: Navidrome's own empty answer (its owned song here has
    none either), byte for byte, not its "not found" error."""
    song = own_song(world)
    catalog = f"sh.tr.demo.{item('song-duo-feat')}"
    navidrome = world.nd.client()
    extra = {"f": "jsonp", "callback": "cb"} if fmt == "jsonp" else {}
    fmt = "xml" if fmt == "jsonp" else fmt  # (the client's own "f" is left out for XML)
    for method in EMPTY_METHODS:
        own = navidrome.request(method, {"id": song, **extra}, fmt=fmt)  # type: ignore[arg-type]
        ours = client.request(method, {"id": catalog, **extra}, fmt=fmt)  # type: ignore[arg-type]
        assert ours.status_code == own.status_code == 200, method
        assert ours.content == own.content, method
        assert ours.headers["content-type"] == own.headers["content-type"], method


def test_a_catalog_artist_in_the_library_is_navidrome_s_in_any_method(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """The catalog artist ID of an artist the library has reaches the library artist."""
    found = world.nd.client().ok("search3", {"query": TERM, "albumCount": 0, "songCount": 0})
    native = found["searchResult3"]["artist"][0]["id"]
    catalog = f"sh.ar.demo.{item('artist-duo')}"
    ours = client.ok("getMusicDirectory", {"id": catalog})
    assert ours == client.ok("getMusicDirectory", {"id": native})
    similar = client.ok("getSimilarSongs2", {"id": catalog})["similarSongs2"]["song"]
    own = client.ok("getSimilarSongs2", {"id": native})["similarSongs2"]["song"]
    assert similar and {s["id"] for s in similar} == {s["id"] for s in own}  # Navidrome shuffles


def test_music_directories_of_catalog_items(world: DeliveryWorld, client: SubsonicClient) -> None:
    """A catalog album is a directory of its songs, a catalog artist one of its albums,
    shaped like Navidrome's directories of an owned album and artist."""
    navidrome = world.nd.client()
    own_album = navidrome.ok("search3", {"query": "Hollow 2", "artistCount": 0})
    own_dir = client.ok("getMusicDirectory", {"id": own_album["searchResult3"]["album"][0]["id"]})
    album = client.ok("getMusicDirectory", {"id": f"sh.al.demo.{item('album-feat-standard')}"})
    own_songs, songs = own_dir["directory"]["child"], album["directory"]["child"]
    assert songs and check("song", [*own_songs, *songs])[1] == len(songs)
    assert set(album["directory"]) == set(own_dir["directory"]) - EXCEPTIONS
    assert all(s["parent"] == album["directory"]["id"] for s in songs)

    artist_id = navidrome.ok("search3", {"query": TERM, "albumCount": 0})["searchResult3"]
    own_artist = client.ok("getMusicDirectory", {"id": artist_id["artist"][0]["id"]})
    artist = client.ok("getMusicDirectory", {"id": f"sh.ar.demo.{item('artist-band')}"})
    own_albums, albums = own_artist["directory"]["child"], artist["directory"]["child"]
    assert albums and all(a["isDir"] for a in albums)
    assert check("song", [*own_albums, *albums])[1] == len(albums)
    assert set(artist["directory"]) == set(own_artist["directory"]) - EXCEPTIONS
    assert all(a["id"].startswith("sh.al.demo.") for a in albums)
