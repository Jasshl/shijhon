"""Suite G — virtual views and commits, with the demo catalog's records
replayed.

Prefetching catalog albums creates nothing; a commit materializes the whole release
exactly once, also under concurrent requests; the virtual view is what the library shows
after the commit (same tracks and whole-second durations); catalog-only albums get
the catalog's cover; artwork IDs in Navidrome's ``al-``/``ar-``/``mf-`` form work.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import cover_image
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient


def item(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def vid(kind: str, name: str) -> str:
    return f"sh.{kind}.demo.{item(name)}"


def fixture_tracks(name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = fixture(name)["body"]["tracks"]
    return rows


@pytest.fixture(scope="module")
def replay() -> Replay:
    return Replay()


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(), tmp_path_factory.mktemp("views"), catalog=replay.catalog()
    ) as w:
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client()
    yield c
    c.close()


def release_rows(world: DeliveryWorld, name: str) -> int:
    async def count() -> int:
        row = await world.services.store.fetchone(
            "SELECT COUNT(*) AS n FROM placeholders WHERE release_ref = ?", [f"demo:{item(name)}"]
        )
        return int(row["n"]) if row else 0

    return world.server.call(count)


def test_prefetching_catalog_items_creates_nothing(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    rows, files = world.placeholder_rows(), world.placeholder_files()
    for name in ("album-twins-clean", "album-soundtrack-compilation", "album-single-radio-edit"):
        album = client.ok("getAlbum", {"id": vid("al", name)})["album"]
        assert album["id"] == vid("al", name) and album["coverArt"] == f"al-{vid('al', name)}"
        assert len(album["song"]) == len(fixture_tracks(name))
        assert all(s["id"].startswith("sh.tr.demo.") for s in album["song"])
        client.ok("getAlbumInfo2", {"id": vid("al", name)})
    song = client.ok("getSong", {"id": vid("tr", "song-feat-album-version")})["song"]
    assert song["albumId"] == vid("al", "album-feat-standard")
    client.ok("getArtist", {"id": vid("ar", "artist-duo")})
    client.ok("getArtistInfo2", {"id": vid("ar", "artist-duo")})
    assert client.request("getCoverArt", {"id": f"al-{vid('al', 'album-twins-clean')}"}).is_success
    assert world.placeholder_rows() == rows and world.placeholder_files() == files
    assert release_rows(world, "album-twins-clean") == 0


def test_catalog_artist_views(client: SubsonicClient) -> None:
    """Runs before the commits below: once the library has an artist of that name, the
    catalog artist opens as the library artist's page (suite J)."""
    artist = client.ok("getArtist", {"id": vid("ar", "artist-duo")})["artist"]
    names = [a["name"] for a in artist["album"]]
    assert artist["albumCount"] == len(names) > 1
    assert any(n.endswith(" - Single") for n in names)
    assert "covers.demo.invalid" in artist["artistImageUrl"]
    assert artist["artistImageUrl"].endswith("1200x1200.png")  # a banner's size
    info = client.ok("getArtistInfo2", {"id": vid("ar", "artist-duo")})["artistInfo2"]
    assert info["largeImageUrl"].endswith("1200x1200.png")
    assert info["similarArtist"] == []


@pytest.mark.parametrize("name", ["album-feat-standard", "album-twins-clean"])
def test_the_virtual_album_is_what_the_library_shows_after_a_commit(
    world: DeliveryWorld, client: SubsonicClient, name: str
) -> None:
    album_vid = vid("al", name)
    virtual = client.ok("getAlbum", {"id": album_vid})["album"]
    client.ok("star", {"albumId": album_vid})
    native = client.ok("getAlbum", {"id": album_vid})["album"]  # now forwarded
    assert native["id"] != album_vid and "starred" in native
    same = ("name", "artist", "songCount", "duration", "year", "isCompilation", "releaseTypes")
    for key in (*same, "genre", "genres", "explicitStatus", "version"):
        assert virtual[key] == native[key], key

    def facts(song: dict[str, Any]) -> tuple[Any, ...]:
        keys = ("title", "album", "track", "discNumber", "duration", "isrc", "artist", "genres")
        return tuple(song[k] for k in (*keys, "explicitStatus"))

    assert [facts(s) for s in virtual["song"]] == [facts(s) for s in native["song"]]
    # Every field of the virtual view is one Navidrome itself gives.
    assert set(virtual) - {"song"} <= set(native)
    assert set(virtual["song"][0]) <= set(native["song"][0]) | {"size", "bitRate"}


def test_each_commit_materializes_once_under_concurrent_requests(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    name = "album-twins-explicit"
    songs = [f"sh.tr.demo.{t['ref']['id']}" for t in fixture_tracks(name)]
    before = world.services.commits.materializations  # type: ignore[union-attr]
    calls: list[tuple[str, Any]] = [
        ("star", {"id": songs[0]}),
        ("scrobble", {"id": songs[1], "submission": "false"}),
        ("setRating", {"id": songs[2], "rating": "3"}),
        ("star", {"albumId": vid("al", name)}),
        ("createBookmark", {"id": songs[3], "position": "1000"}),
        ("savePlayQueue", [("id", songs[0]), ("id", songs[1]), ("current", songs[1])]),
    ]
    with ThreadPoolExecutor(len(calls)) as pool:
        answers = list(pool.map(lambda c: world.client().ok(c[0], c[1]), calls))
    assert len(answers) == len(calls)
    assert world.services.commits.materializations == before + 1  # type: ignore[union-attr]
    assert release_rows(world, name) == len(songs)
    folder = {p.parent for p in world.placeholder_files() if p.suffix == ".flac"}
    covers = [p for p in world.placeholder_files() if p.name == "cover.jpg"]
    assert len(covers) == len(folder)  # one cover per catalog-only album


def test_catalog_only_albums_get_the_catalog_cover(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    name = "album-ep-without-suffix"
    song = f"sh.tr.demo.{fixture_tracks(name)[0]['ref']['id']}"
    client.ok("star", {"id": song})
    covers = [p for p in world.placeholder_files() if p.name == "cover.jpg"]
    assert any(p.read_bytes() == cover_image("blue").read_bytes() for p in covers)
    requests = replay.artwork_requests
    art = client.request("getCoverArt", {"id": f"al-{vid('al', name)}", "size": "300"})
    assert art.is_success and art.headers["content-type"].startswith("image/")
    assert replay.artwork_requests == requests  # served by Navidrome now


@pytest.mark.parametrize(
    "art",
    [
        "{album}",
        "al-{album}",
        "al-{album}_5f3a0c1e",
        "mf-{song}",
        "ar-{artist}",
    ],
)
def test_virtual_cover_art_in_every_form(client: SubsonicClient, art: str) -> None:
    ident = art.format(
        album=vid("al", "album-duo-deluxe-clean"),
        song=vid("tr", "song-editions-remastered"),
        artist=vid("ar", "artist-composer"),
    )
    answer = client.request("getCoverArt", {"id": ident})
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "image/jpeg"
    assert answer.content == cover_image("blue").read_bytes()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _answering(world: DeliveryWorld, replay: Replay, answer: tuple[bytes, str] | None) -> None:
    """What the catalog answers image requests with from now on (None: its cover)."""
    views = world.services.views
    assert views is not None
    replay.artwork_answer = answer
    if answer is None:
        return  # (nothing of an answer that was no image is kept anywhere: asked again)
    views.catalog.artwork.cache_clear()  # type: ignore[union-attr]  # (asked again)
    if views.covers is not None:  # ... and no cover kept from before
        for kept in views.covers.folder.glob("*/*.img"):
            kept.unlink()


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize(
    "answer",
    [
        (SVG, "image/svg+xml"),
        (SVG, "image/jpeg"),  # named a raster image: the bytes say otherwise
        (b"<!doctype html><script>alert(1)</script>", "image/png"),
        (b"\xff\xd8", "image/jpeg"),  # too short to be one
    ],
)
def test_catalog_artwork_that_is_no_raster_image_is_never_served(
    world: DeliveryWorld,
    client: SubsonicClient,
    replay: Replay,
    answer: tuple[bytes, str],
    cached: bool,
) -> None:
    """What a catalog sends as artwork is answered from Shijhon's
    origin only when its bytes are a JPEG, PNG, GIF or WebP - an SVG could carry script. The
    client gets what Navidrome answers for a cover it does not have, also without the cover
    cache, and nothing of it is kept."""
    views = world.services.views
    assert views is not None
    covers, ident = views.covers, f"al-{vid('al', 'album-duo-deluxe-clean')}"
    size = str(700 + len(answer[0]) + (1 if cached else 0))  # a size no test has asked for
    asked = replay.artwork_requests
    _answering(world, replay, answer)
    try:
        if not cached:
            views.covers = None
        got = client.request("getCoverArt", {"id": ident, "size": size})
    finally:
        views.covers = covers
        _answering(world, replay, None)
    assert replay.artwork_requests == asked + 1  # it was the catalog's answer
    assert got.content != answer[0] and b"<s" not in got.content
    assert "svg" not in got.headers.get("content-type", "")
    direct = world.nd.client().request("getCoverArt", {"id": ident, "size": size})
    assert (got.status_code, got.content) == (direct.status_code, direct.content)
    # Nothing of it was kept: the next request gets the real cover.
    again = client.request("getCoverArt", {"id": ident, "size": size})
    assert again.headers["content-type"] == "image/jpeg"
    assert again.content == cover_image("blue").read_bytes()
    assert again.headers["x-content-type-options"] == "nosniff"


def test_a_cover_that_is_no_jpeg_is_not_written_into_the_library(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """... nor written as an album's cover.jpg when the album is committed: a JPEG by its
    bytes, whatever the catalog calls it (here a PNG called a JPEG)."""
    before = set(world.placeholder_files())
    song = f"sh.tr.demo.{fixture_tracks('album-editions-mix')[0]['ref']['id']}"
    asked = replay.artwork_requests
    _answering(world, replay, (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/jpeg"))
    try:
        client.ok("star", {"id": song})
    finally:
        _answering(world, replay, None)
    written = set(world.placeholder_files()) - before
    assert replay.artwork_requests > asked  # its cover was asked for
    assert written and all(p.suffix == ".flac" for p in written)  # no cover.jpg with them


def test_a_raster_cover_is_served_as_what_its_bytes_are(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    """... and a real image under the type its bytes say, whatever the catalog calls it."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    _answering(world, replay, (png, "image/svg+xml"))
    try:
        ident = f"al-{vid('al', 'album-duo-deluxe-clean')}"
        got = client.request("getCoverArt", {"id": ident, "size": "1693"})
    finally:
        _answering(world, replay, None)
    assert got.content == png and got.headers["content-type"] == "image/png"


def test_editions_appear_once_on_the_artist_page(client: SubsonicClient) -> None:
    """The artist view is de-duplicated (the band's view happens to list one edition; the
    edition rules are covered by the unit tests)."""
    base = fixture("album-anniversary-standard")["body"]["title"]
    artist = client.ok("getArtist", {"id": vid("ar", "artist-band")})["artist"]
    editions = [a for a in artist["album"] if a["name"].startswith(base)]
    assert len(editions) == 1


def test_formats_and_catalogs_shijhon_does_not_serve_are_forwarded(
    client: SubsonicClient, replay: Replay
) -> None:
    before = replay.api_requests
    # JSONP (an album no test commits: a committed one is Navidrome's, in any format); XML is
    # answered like JSON (``test_J_xml.py``).
    jsonp = client.request(
        "getAlbum",
        {"id": vid("al", "album-soundtrack-compilation"), "f": "jsonp", "callback": "cb"},
        fmt="xml",
    )
    assert jsonp.text.startswith("cb(") and '"status":"failed"' in jsonp.text
    assert client.error_code("getAlbum", {"id": "sh.al.othercat.123"}) == 70
    assert replay.api_requests == before
    assert client.error_code("getAlbum", {"id": "sh.al.demo.1"}) == 70  # not in the catalog


def test_a_catalog_failure_is_a_subsonic_error(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay
) -> None:
    name = "album-soundtrack-expanded"
    replay.failing[f"albums/{item(name)}"] = 503
    try:
        body = client.request("getAlbum", {"id": vid("al", name)}).json()["subsonic-response"]
        assert body["status"] == "failed" and "catalog unavailable" in body["error"]["message"]
        answer = client.request("star", {"albumId": vid("al", name)}).json()["subsonic-response"]
        assert answer["status"] == "failed"
    finally:
        del replay.failing[f"albums/{item(name)}"]
    assert release_rows(world, name) == 0
