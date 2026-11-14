"""Suite G — pictures for library artists Navidrome has none for (``ArtistPictures``).

Against a real Navidrome (its external services off, so its artists end without a picture):
the catalog artist's picture is uploaded through Navidrome's own artist image upload and
served by Navidrome; the catalog artist comes from the catalog items of the artist's songs
in the library, else from a search checked against the artist's titles; a picture changed
in Navidrome is left alone for good; with the setting off, Shijhon's pictures go and others
stay.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from shijhon.artist_pictures import ArtistPictures
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogArtist, CatalogRef, CatalogRelease
from shijhon.matching.normalize import fold
from shijhon.navidrome.client import NavidromeService
from shijhon.store import Store
from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

pytestmark = pytest.mark.anyio

TEMPLATE = "https://pictures.invalid/{w}x{h}.png"


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shijhon.artist_pictures.PAUSE_SECONDS", 0.0)


def png(rgb: tuple[int, int, int], size: int = 8) -> bytes:
    """A small solid PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class PictureCatalog:
    """Artists with pictures and their releases; counts what it is asked."""

    key = "test"
    region = ""

    def __init__(self, artists: list[tuple[CatalogArtist, list[str]]]) -> None:
        self.artists = {a.ref.id: (a, titles) for a, titles in artists}
        self.fetched: list[str] = []
        self.searched: list[str] = []

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        self.searched.append(term)
        found = tuple(a for a, _ in self.artists.values() if fold(a.name) == fold(term))
        return SearchResults(artists=found[:limit])

    async def artist(self, artist_id: str) -> CatalogArtist:
        if artist_id not in self.artists:
            raise CatalogError("not_found", "no such artist")
        return self.artists[artist_id][0]

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        artist, titles = self.artists[artist_id]
        return tuple(
            catalog_release(f"{artist_id}-{n}", t, artist.name, 1) for n, t in enumerate(titles)
        )

    async def artwork(self, url: str) -> tuple[bytes, str]:
        self.fetched.append(url)
        return png((200, 40, 40)), "image/png"


def artist_in(nd: NavidromeInstance, name: str) -> dict[str, Any]:
    found = nd.client().ok("search3", {"query": name, "artistCount": 20, "songCount": 0})
    [artist] = [a for a in found["searchResult3"].get("artist", []) if a["name"] == name]
    return artist


async def native_artist(service: NavidromeService, ident: str) -> dict[str, Any]:
    artist = await service.artist(ident)
    assert artist is not None
    return artist


def library_album(nd: NavidromeInstance, artist: str, title: str) -> str:
    write_album(nd.music, Album(artist, title, (Track(f"{title} Song", 1),), fmt="flac"))
    nd.scan()
    nd.wait_artwork()
    return str(artist_in(nd, artist)["id"])


@pytest.fixture
async def parts(tmp_path: Path, navidrome: NavidromeInstance) -> Any:
    service = NavidromeService(navidrome.base_url, ADMIN_USER, ADMIN_PASSWORD)
    store = await Store.open(tmp_path / "shijhon.sqlite3")
    try:
        yield service, store
    finally:
        await store.close()
        await service.aclose()


async def test_an_artist_without_a_picture_gets_the_catalogs(
    navidrome: NavidromeInstance, parts: Any
) -> None:
    service, store = parts
    ident = library_album(navidrome, "Quill Marrow", "Harbor Glass")
    assert (await native_artist(service, ident)).get("imageAbsent")
    catalog = PictureCatalog(
        [(CatalogArtist(CatalogRef("test", "qm"), "Quill Marrow", TEMPLATE), ["Harbor Glass"])]
    )
    pictures = ArtistPictures(service, catalog, store)  # type: ignore[arg-type]

    assert await pictures.once() == 1
    assert catalog.fetched == ["https://pictures.invalid/1200x1200.png"]
    navidrome.wait_artwork()
    artist = await native_artist(service, ident)
    assert artist.get("uploadedImage") and not artist.get("imageAbsent")
    cover = navidrome.client().request("getCoverArt", {"id": f"ar-{ident}"})
    assert cover.status_code == 200 and cover.headers["content-type"].startswith("image/")
    [row] = await store.fetchall("SELECT * FROM artist_pictures WHERE artist_id = ?", [ident])
    assert (row["state"], row["catalog_ref"]) == ("set", "test:qm")

    assert await pictures.once() == 0  # kept, not set again
    assert len(catalog.fetched) == 1


async def test_an_artist_of_the_same_name_without_shared_titles_is_not_used(
    navidrome: NavidromeInstance, parts: Any
) -> None:
    service, store = parts
    ident = library_album(navidrome, "Tamsin Reed", "Northern Rooms")
    catalog = PictureCatalog(
        [(CatalogArtist(CatalogRef("test", "tr"), "Tamsin Reed", TEMPLATE), ["Other Things"])]
    )
    pictures = ArtistPictures(service, catalog, store)  # type: ignore[arg-type]

    assert await pictures.once() == 0
    assert catalog.fetched == []
    [row] = await store.fetchall("SELECT * FROM artist_pictures WHERE artist_id = ?", [ident])
    assert row["state"] == "unmatched" and row["retry_at"] > row["updated_at"]
    assert await pictures.once() == 0
    assert catalog.searched.count("Tamsin Reed") == 1  # not asked again before the retry


async def test_a_catalog_artist_with_the_names_words_in_another_order_is_found(
    navidrome: NavidromeInstance, parts: Any
) -> None:
    service, store = parts
    ident = library_album(navidrome, "Sato Lumi", "Paper Moons")
    # (the song credits that a catalog item under another name is paired with)
    [song] = await service.artist_songs(ident)
    assert (song.get("artist"), song.get("albumArtist")) == ("Sato Lumi", "Sato Lumi")
    catalog = PictureCatalog(
        [(CatalogArtist(CatalogRef("test", "ls"), "Lumi Sato", TEMPLATE), ["Paper Moons"])]
    )
    catalog.search = lambda term, limit=20: _found(catalog, "Lumi Sato")  # type: ignore[method-assign]
    assert await ArtistPictures(service, catalog, store).once() == 1  # type: ignore[arg-type]
    [row] = await store.fetchall("SELECT * FROM artist_pictures WHERE artist_id = ?", [ident])
    assert (row["state"], row["catalog_ref"]) == ("set", "test:ls")


async def _found(catalog: PictureCatalog, name: str) -> SearchResults:
    return SearchResults(artists=tuple(a for a, _ in catalog.artists.values() if a.name == name))


async def test_the_catalog_items_of_the_artists_songs_find_it(
    navidrome: NavidromeInstance, parts: Any, tmp_path: Path
) -> None:
    service, _ = parts
    ref = CatalogRef("test", "ov")
    release = replace(
        catalog_release("pic-ids", "Lantern Year", "Orla Vesk", 2), artist_refs=(ref,)
    )
    async with engine_for(navidrome, tmp_path / "engine") as engine_parts:
        await engine_parts.engine.materialize(release)
        navidrome.wait_artwork()
        catalog = PictureCatalog([(CatalogArtist(ref, "Orla Vesk", TEMPLATE), [])])
        pictures = ArtistPictures(navidrome=service, catalog=catalog, store=engine_parts.store)  # type: ignore[arg-type]
        assert await pictures.once() == 1
    assert "Orla Vesk" not in catalog.searched  # found by its item, not by its name
    ident = str(artist_in(navidrome, "Orla Vesk")["id"])
    assert (await native_artist(service, ident)).get("uploadedImage")


async def test_a_picture_changed_in_navidrome_is_left_alone(
    navidrome: NavidromeInstance, parts: Any
) -> None:
    service, store = parts
    ident = library_album(navidrome, "Brisk Avalon", "Copper Leaves")
    catalog = PictureCatalog(
        [(CatalogArtist(CatalogRef("test", "ba"), "Brisk Avalon", TEMPLATE), ["Copper Leaves"])]
    )
    pictures = ArtistPictures(service, catalog, store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    navidrome.wait_artwork()

    await service.set_artist_image(ident, png((10, 200, 10)), "image/png")  # the person's own
    navidrome.wait_artwork()
    assert await pictures.once() == 0
    [row] = await store.fetchall("SELECT state FROM artist_pictures WHERE artist_id = ?", [ident])
    assert row["state"] == "declined"

    await service.remove_artist_image(ident)  # and then none at all
    navidrome.wait_artwork()
    assert (await native_artist(service, ident)).get("imageAbsent")
    assert await pictures.once() == 0  # never set again
    assert len(catalog.fetched) == 1


async def test_turned_off_only_shijhons_pictures_go(
    navidrome: NavidromeInstance, parts: Any
) -> None:
    service, store = parts
    mine = library_album(navidrome, "Ilse Marrow", "Salt Hours")
    theirs = library_album(navidrome, "Tobin Quay", "Driftwood")
    catalog = PictureCatalog(
        [(CatalogArtist(CatalogRef("test", "im"), "Ilse Marrow", TEMPLATE), ["Salt Hours"])]
    )
    await service.set_artist_image(theirs, png((10, 10, 200)), "image/png")
    assert await ArtistPictures(service, catalog, store).once() == 1  # type: ignore[arg-type]
    navidrome.wait_artwork()

    off = ArtistPictures(service, catalog, store, enabled=False)  # type: ignore[arg-type]
    assert await off.once() == 0
    navidrome.wait_artwork()
    assert not (await native_artist(service, mine)).get("uploadedImage")
    assert (await native_artist(service, theirs)).get("uploadedImage")
    assert await store.fetchall("SELECT * FROM artist_pictures") == []
