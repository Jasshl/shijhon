"""Suite D — tag matrix.

Owned FLAC/MP3/M4A albums tagged the ways real libraries are (year or full date, an
original date beside a later one, release dates, version tags, MusicBrainz album IDs, multiple album
artists, compilations, several discs) each stay **one album** after placeholders fill
their missing tracks. A forced split is detected and rolled back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from shijhon.catalog.model import CatalogRef, CatalogRelease
from shijhon.placeholders.engine import MaterializeError
from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio

MBID = "5b000000-aaaa-4bbb-8ccc-0123456789ab"  # invented

# name -> (format, Album keyword arguments); every owned album has tracks 1 and 2 of 4.
CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "flac-year": ("flac", {"recording_date": "2020"}),
    "flac-full-date": ("flac", {"recording_date": "2020-05-06"}),
    "flac-no-date": ("flac", {"recording_date": None}),
    "flac-original": ("flac", {"recording_date": "2012-03-01", "original_date": "1999"}),
    "flac-release-date": ("flac", {"recording_date": "2011", "release_date": "2012-03-01"}),
    "flac-year-alias": ("flac", {"recording_date": "2012", "extra": {"year": ["2013"]}}),
    "flac-version": ("flac", {"version": "Deluxe Edition"}),
    "flac-mbid": ("flac", {"mb_album_id": MBID}),
    "flac-album-artists": ("flac", {"album_artists": ("Lead", "Guest")}),
    "flac-separator": ("flac", {"artist_override": "Lead; Guest"}),
    "flac-compilation": ("flac", {"compilation": True, "artist_override": "Various Artists"}),
    "flac-compilation-no-albumartist": (
        "flac",
        {"compilation": True, "write_album_artist": False},
    ),
    "flac-release-type": ("flac", {"release_type": "album", "genre": "Rock"}),
    "flac-two-discs": ("flac", {"discs": True}),
    "mp3-year": ("mp3", {"recording_date": "2020"}),
    "mp3-original": ("mp3", {"recording_date": "2012-03-01", "original_date": "1999"}),
    "mp3-release-date": ("mp3", {"recording_date": "2011", "release_date": "2012-03-01"}),
    "mp3-version": ("mp3", {"version": "Remastered"}),
    "mp3-mbid": ("mp3", {"mb_album_id": MBID.replace("5b", "6b")}),
    "mp3-album-artists": ("mp3", {"album_artists": ("Lead", "Guest")}),
    "mp3-compilation": ("mp3", {"compilation": True, "artist_override": "Various Artists"}),
    "m4a-full-date": ("m4a", {"release_date": "2020-05-06"}),
    "m4a-year": ("m4a", {"release_date": "2020"}),
    "m4a-original": ("m4a", {"release_date": "2012-03-01", "original_date": "1999"}),
    "m4a-version": ("m4a", {"version": "Anniversary"}),
    "m4a-mbid": ("m4a", {"mb_album_id": MBID.replace("5b", "7b")}),
    "m4a-album-artists": ("m4a", {"album_artists": ("Lead", "Guest")}),
    "m4a-compilation": ("m4a", {"compilation": True, "artist_override": "Various Artists"}),
}


def owned_album(case: str) -> Album:
    fmt, kwargs = CASES[case]
    kwargs = dict(kwargs)
    artist = kwargs.pop("artist_override", f"Artist {case}")
    two_discs = kwargs.pop("discs", False)
    compilation = kwargs.get("compilation", False)
    tracks = tuple(
        Track(
            f"Album {case} Song {n}",
            1 if two_discs else n,
            disc=n if two_discs else 1,
            artists=(f"Performer {n}",) if compilation else (),
        )
        for n in (1, 2)
    )
    return Album(artist, f"Album {case}", tracks, fmt=fmt, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def library(navidrome: NavidromeInstance) -> dict[str, Album]:
    albums = {case: owned_album(case) for case in CASES}
    for album in albums.values():
        write_album(navidrome.music, album)
    navidrome.scan(full=True)
    return albums


def album_id_of(nd: NavidromeInstance, title: str) -> list[str]:
    found = nd.client().ok("search3", {"query": title, "albumCount": 20, "songCount": 0})
    # Navidrome appends the version tag to the displayed name: "Title (Version)".
    return [
        a["id"]
        for a in found["searchResult3"].get("album", [])
        if a["name"] == title or a["name"].startswith(title + " (")
    ]


def release_for(case: str, album: Album) -> CatalogRelease:
    two_discs = CASES[case][1].get("discs", False)
    return catalog_release(f"d-{case}", album.title, album.artist, 4, discs=2 if two_discs else 1)


def links_for(release: CatalogRelease, owned_songs: list[dict[str, Any]]) -> dict[CatalogRef, str]:
    by_position = {(s["discNumber"], s["trackNumber"]): s["id"] for s in owned_songs}
    return {
        t.ref: by_position[(t.disc, t.number)]
        for t in release.tracks
        if (t.disc, t.number) in by_position
    }


@pytest.mark.parametrize("case", list(CASES))
async def test_fill_keeps_one_album(
    navidrome: NavidromeInstance, library: dict[str, Album], tmp_path: Path, case: str
) -> None:
    album = library[case]
    [album_id] = album_id_of(navidrome, album.title)
    async with engine_for(navidrome, tmp_path) as parts:
        owned = await parts.engine.owned_album(album_id)
        owned_ids = {s["id"] for s in owned.songs}
        release = release_for(case, album)
        links = links_for(release, owned.songs)
        assert len(links) == 2
        result = await parts.engine.materialize(release, owned_album_id=album_id, links=links)

    assert result.album_id == album_id
    assert len(result.created) == 2
    assert album_id_of(navidrome, album.title) == [album_id]
    detail = navidrome.client().ok("getAlbum", {"id": album_id})["album"]
    assert detail["songCount"] == 4
    assert owned_ids <= {s["id"] for s in detail["song"]}


async def test_forced_split_is_rolled_back(
    navidrome: NavidromeInstance, library: dict[str, Album], tmp_path: Path
) -> None:
    album = owned_album("flac-year")
    album = Album(album.artist + " Split", album.title + " Split", album.tracks, fmt="flac")
    write_album(navidrome.music, album)
    navidrome.scan(targets=[album.relative_folder])
    [album_id] = album_id_of(navidrome, album.title)
    async with engine_for(navidrome, tmp_path) as parts:
        parts.engine.tag_hook = lambda comments: comments.__setitem__("releasedate", ["1901"])
        owned = await parts.engine.owned_album(album_id)
        release = catalog_release("d-split", album.title, album.artist, 4)
        with pytest.raises(MaterializeError, match="split"):
            await parts.engine.materialize(
                release, owned_album_id=album_id, links=links_for(release, owned.songs)
            )
        folder = parts.layout.release_folder(release.ref, release.artist, release.title)
        leftover = [s for s in await parts.navidrome.songs_under(folder) if not s["missing"]]
        assert leftover == []
        assert not parts.layout.absolute(folder).exists()
        assert await parts.store.fetchall("SELECT * FROM placeholders") == []
    assert album_id_of(navidrome, album.title) == [album_id]
    assert navidrome.client().ok("getAlbum", {"id": album_id})["album"]["songCount"] == 2


async def test_catalog_only_album_is_its_own_album(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release("d-only", "Only In Catalog", "Catalog Artist", 3)
    async with engine_for(navidrome, tmp_path) as parts:
        result = await parts.engine.materialize(release, cover=b"\xff\xd8fake-jpeg")
    detail = navidrome.client().ok("getAlbum", {"id": result.album_id})["album"]
    assert detail["songCount"] == 3
    assert detail["name"] == "Only In Catalog"
    assert detail.get("releaseTypes") == ["Album"] or detail.get("releaseTypes") == ["album"]


async def test_clean_edition_does_not_merge_with_explicit(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    explicit = catalog_release("d-explicit", "Twin Album", "Twin Artist", 2)
    clean = catalog_release("d-clean", "Twin Album", "Twin Artist", 2, clean=True)
    async with engine_for(navidrome, tmp_path) as parts:
        first = await parts.engine.materialize(explicit)
        second = await parts.engine.materialize(clean)
    assert first.album_id != second.album_id


async def test_incremental_materialization_joins_the_same_album(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release("d-incremental", "Step Album", "Step Artist", 4)
    async with engine_for(navidrome, tmp_path) as parts:
        first = await parts.engine.materialize(release, only=[release.tracks[0].ref])
        second = await parts.engine.materialize(release)
        again = await parts.engine.materialize(release)
    assert second.album_id == first.album_id
    assert len(first.created) == 1 and len(second.created) == 3
    assert again.created == {}
    assert navidrome.client().ok("getAlbum", {"id": first.album_id})["album"]["songCount"] == 4


async def test_later_additions_to_a_fill_join_the_owned_album(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    album = Album(
        "Later Artist",
        "Later Album",
        (Track("Later 1", 1), Track("Later 2", 2)),
        recording_date="2012-03-01",
        original_date="1999",
        version="Remaster",
    )
    write_album(navidrome.music, album)
    navidrome.scan(targets=[album.relative_folder])
    [album_id] = album_id_of(navidrome, album.title)
    release = catalog_release("d-later", "Later Album (Remaster)", "Later Artist", 4)
    async with engine_for(navidrome, tmp_path) as parts:
        owned = await parts.engine.owned_album(album_id)
        links = links_for(release, owned.songs)
        first = await parts.engine.materialize(
            release, owned_album_id=album_id, links=links, only=[release.tracks[2].ref]
        )
        second = await parts.engine.materialize(release)  # owned album not named again
    assert first.album_id == second.album_id == album_id
    assert navidrome.client().ok("getAlbum", {"id": album_id})["album"]["songCount"] == 4


async def test_unexpected_failure_after_writing_rolls_back(
    navidrome: NavidromeInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = catalog_release("d-record-fails", "Unrecorded", "Rollback Artist", 2)
    async with engine_for(navidrome, tmp_path) as parts:

        async def broken_record(*args: object, **kwargs: object) -> None:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(parts.engine, "_record", broken_record)
        with pytest.raises(MaterializeError, match="unexpected RuntimeError"):
            await parts.engine.materialize(release)
        folder = parts.layout.release_folder(release.ref, release.artist, release.title)
        assert not parts.layout.absolute(folder).exists()
        assert [s for s in await parts.navidrome.songs_under(folder) if not s["missing"]] == []
