"""Catalog entries' artist references (suite J, units): every one has an ID."""

from __future__ import annotations

import pytest

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    item_artist,
    parse_item_artist,
)
from shijhon.matching.normalize import fold
from shijhon.views.entries import song_entry

A, B = CatalogRef("demo", "1"), CatalogRef("demo", "2")
SONG = CatalogRef("demo", "t1")


def track(artist: str, refs: tuple[CatalogRef, ...] = ()) -> CatalogTrack:
    return CatalogTrack(SONG, "Song", artist, 180_000, artist_refs=refs)


def people(entry: dict[str, object]) -> list[tuple[str, str]]:
    return [(p["name"], p["id"]) for p in entry["artists"]]  # type: ignore[attr-defined,index]


def test_every_artist_reference_has_an_id() -> None:
    library = {fold("Mara Vance"): "lib-mara"}
    # One artist item for the credit: a duo or a band stays one artist.
    assert people(song_entry(track("Ana & Bo", (A,)))) == [("Ana & Bo", "sh.ar.demo.1")]
    # As many items as the credit names: paired, the library's own ID first.
    entry = song_entry(track("Mara Vance feat. Bo", (A, B)), library=library)
    assert people(entry) == [("Mara Vance", "lib-mara"), ("Bo", "sh.ar.demo.2")]
    assert entry["artistId"] == "lib-mara"
    # One item for a credit naming two: the library artist's ID when it is theirs.
    assert people(song_entry(track("Mara Vance feat. Bo", (A,)), library=library)) == [
        ("Mara Vance feat. Bo", "lib-mara")
    ]
    # No artist items at all: references through the song, never a split band name.
    assert people(song_entry(track("Salt, Ash & Ember"))) == [
        ("Salt, Ash & Ember", "sh.ar.demo.t-t1-0")
    ]
    entry = song_entry(track("Solo Singer feat. Guest"))
    assert people(entry) == [
        ("Solo Singer", "sh.ar.demo.t-t1-0"),
        ("Guest", "sh.ar.demo.t-t1-1"),
    ]
    assert entry["albumArtists"] == entry["artists"]
    assert entry["artistId"] == "sh.ar.demo.t-t1-0"
    for key in ("path", "groupings", "works", "movements"):
        assert key in entry
    # On its album, the album artist's name gets the album's artist item.
    album = CatalogRelease(
        CatalogRef("demo", "al"),
        "R",
        "Solo Singer",
        ReleaseKind.ALBUM,
        "2020-02-31",
        artist_refs=(A,),
    )
    entry = song_entry(track("Solo Singer feat. Guest"), album)
    assert people(entry)[0] == ("Solo Singer", "sh.ar.demo.1")
    assert entry["created"] == "2020-02-01T00:00:00Z"  # an impossible day is left out


def test_item_references_round_trip() -> None:
    ref = item_artist(CatalogRef("demo", "900000001"), "t", 2)
    assert parse_item_artist(ref.id) == ("t", "900000001", 2)
    assert parse_item_artist("900000001") is None and parse_item_artist("x-1-2") is None


class Inner:
    key, region = "demo", "xx"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        return SearchResults()

    async def song(self, song_id: str) -> CatalogTrack:
        ref = CatalogRef("demo", song_id)
        return CatalogTrack(ref, "Song", "X feat. Y", 1, artist_refs=(A, B))

    async def artist(self, artist_id: str) -> CatalogArtist:
        self.asked.append(artist_id)
        return CatalogArtist(CatalogRef("demo", artist_id), "Somebody")

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        self.asked.append(artist_id)
        return ()

    async def aclose(self) -> None:
        return None

    album = songs_by_isrc = artwork = aclose  # not used here


@pytest.mark.anyio
async def test_a_reference_through_a_song_opens_that_song_s_artist() -> None:
    inner = Inner()
    cache = CachedCatalog(inner, ttl=60)  # type: ignore[arg-type]
    assert (await cache.artist(item_artist(SONG, "t", 1).id)).ref == B
    await cache.artist_releases(item_artist(SONG, "t", 0).id)
    assert inner.asked == ["2", "1"]
    with pytest.raises(CatalogError):
        await cache.artist(item_artist(SONG, "t", 5).id)  # the song lists two


def test_a_song_whose_album_is_not_known_has_its_own_cover() -> None:
    """A catalog that names a song's album by title only: no album ID, and the
    song's own artwork as its cover, named as Navidrome names a song's."""
    ref = CatalogRef("demo", "900")
    track = CatalogTrack(ref, "Song", "Artist", 200_000, album_title="Album")
    bare = song_entry(track)
    assert "albumId" not in bare and "parent" not in bare and "coverArt" not in bare
    assert bare["album"] == "Album"
    shown = song_entry(
        CatalogTrack(ref, "Song", "Artist", 200_000, artwork_template="https://covers.invalid/a")
    )
    assert shown["coverArt"] == "mf-sh.tr.demo.900" and "albumId" not in shown
    with_album = song_entry(
        CatalogTrack(
            ref,
            "Song",
            "Artist",
            200_000,
            album=CatalogRef("demo", "800"),
            artwork_template="https://covers.invalid/a",
        )
    )
    assert with_album["coverArt"] == "al-sh.al.demo.800"  # its album's, as before
