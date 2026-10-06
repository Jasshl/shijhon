"""Suite I (continued) - a later addition to an album that was filled before keeps the rules
its first fill kept.

An owned album is filled from a release; later the catalog lists that release with one
more track, and the song is added - by a commit (a favorite of the new catalog song,
through the Subsonic API) or by a refresh. Whoever asks, nothing is added to the owned
album when the release's track list could not be read in full, when its track numbers are
only the order of the catalog's list and the album's songs sit elsewhere than that order
puts them, or when the new track's disc and number are another song's. Otherwise the song
joins the album.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack
from shijhon.placeholders.engine import MaterializeError
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.library import Album, Track, write_album
from tests.harness.subsonic import SubsonicClient

CASES = ("incomplete", "reordered", "occupied", "placeholder", "fits", "restored", "kept")
OWNED = {
    name: Album(
        f"Addition Artist {name}", f"Addition Album {name}", (Track("Two", 2), Track("Four", 4))
    )
    for name in CASES
}


class Catalog:
    """A catalog whose releases the tests set: the answer to ``album`` is what is there
    now."""

    key = "test"
    region = ""

    def __init__(self) -> None:
        self.releases: dict[str, CatalogRelease] = {}

    async def album(self, album_id: str) -> CatalogRelease:
        if album_id not in self.releases:
            raise CatalogError("not_found", "test")
        return self.releases[album_id]

    async def song(self, song_id: str) -> CatalogTrack:
        for release in self.releases.values():
            for track in release.tracks:
                if track.ref.id == song_id:
                    return track
        raise CatalogError("not_found", "test")

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        return SearchResults()

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        return ()

    async def artists_of(self, songs: Any, albums: Any) -> dict[str, tuple[CatalogRef, ...]]:
        return {}

    async def artwork(self, url: str) -> tuple[bytes, str]:
        raise CatalogError("invalid", "test")

    async def aclose(self) -> None:
        return None


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog()


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    catalog: Catalog,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    for album in OWNED.values():
        write_album(nd.music, album)
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("additions"), catalog=catalog) as w:  # type: ignore[arg-type]
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client()
    yield c
    c.close()


def songs(world: DeliveryWorld, album: str) -> dict[int, str]:
    """The album's songs: track number -> title."""
    found = world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]
    return {int(s["track"]): str(s["title"]) for s in found}


def filled(
    world: DeliveryWorld, catalog: Catalog, name: str, **changes: Any
) -> tuple[str, CatalogRelease]:
    """The case's owned album, filled from a four-track release: its songs are tracks 2
    and 4, tracks 1 and 3 become placeholders. Returns (the album's ID, the release)."""
    found = world.nd.client().ok("search3", {"query": OWNED[name].title, "artistCount": 0})
    album = str(found["searchResult3"]["album"][0]["id"])
    release = catalog_release(f"add-{name}", OWNED[name].title, OWNED[name].artist, 4)
    release = dataclasses.replace(release, **changes)
    owned = {
        int(s["track"]): str(s["id"])
        for s in world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]
    }
    links = {t.ref: owned[t.number] for t in release.tracks if t.number in owned}
    catalog.releases[release.ref.id] = release
    world.materialize(release, owned_album_id=album, links=links)
    assert sorted(songs(world, album)) == [1, 2, 3, 4]
    return album, release


def grown(
    release: CatalogRelease, number: int = 5, **changes: Any
) -> tuple[CatalogRelease, CatalogTrack]:
    """The release as the catalog lists it later: one more track, at ``number``."""
    extra = dataclasses.replace(
        release.tracks[0],
        ref=CatalogRef("test", f"{release.ref.id}-new"),
        title="Later Song",
        number=number,
        isrc=None,
    )
    return dataclasses.replace(release, tracks=(*release.tracks, extra), **changes), extra


def listed(world: DeliveryWorld, catalog: Catalog, release: CatalogRelease) -> None:
    """The catalog lists ``release`` from now on (answers kept before are dropped)."""
    catalog.releases[release.ref.id] = release
    kept = world.services.catalog

    async def clear() -> None:
        kept.album.cache_clear()  # type: ignore[union-attr]
        kept._song.cache_clear()  # type: ignore[union-attr]

    world.server.call(clear)


def refresh(world: DeliveryWorld, album: str) -> Any:
    assert world.services.refresh is not None
    [result] = world.server.call(lambda: world.services.refresh.refresh(album))  # type: ignore[union-attr]
    return result


def star(client: SubsonicClient, track: CatalogTrack) -> int | None:
    """Favorite the catalog song: the error's code, or None when it was added."""
    return client.error_code("star", {"id": f"sh.tr.test.{track.ref.id}"})


def test_an_incomplete_answer_adds_nothing_to_an_album_filled_before(
    world: DeliveryWorld, catalog: Catalog, client: SubsonicClient
) -> None:
    album, release = filled(world, catalog, "incomplete")
    later, extra = grown(release, incomplete=True)  # a track of it could not be read
    listed(world, catalog, later)
    rows = world.placeholder_rows()
    assert star(client, extra) == 0
    assert refresh(world, album).refused == "the catalog's track list of the release is incomplete"
    assert sorted(songs(world, album)) == [1, 2, 3, 4] and world.placeholder_rows() == rows
    listed(world, catalog, dataclasses.replace(later, incomplete=False))  # read in full again
    assert star(client, extra) is None
    assert songs(world, album)[5] == "Later Song"


def test_a_list_in_another_order_than_the_owned_files_adds_nothing(
    world: DeliveryWorld, catalog: Catalog, client: SubsonicClient
) -> None:
    """A release whose numbers are only its list's order: when the list changes, the owned
    songs are no longer where it puts them."""
    album, release = filled(world, catalog, "reordered", numbered=False)
    first, second, third, fourth = release.tracks
    moved = (  # the catalog lists the second track first now: every number shifts
        dataclasses.replace(second, number=1),
        dataclasses.replace(first, number=2),
        third,
        fourth,
    )
    later, extra = grown(dataclasses.replace(release, tracks=moved))
    listed(world, catalog, later)
    rows = world.placeholder_rows()
    assert star(client, extra) == 0
    assert "without numbers" in refresh(world, album).refused
    assert sorted(songs(world, album)) == [1, 2, 3, 4] and world.placeholder_rows() == rows
    # In the order the album was filled in, the new track joins it.
    later, extra = grown(release)
    listed(world, catalog, later)
    assert star(client, extra) is None
    assert songs(world, album)[5] == "Later Song"


def test_a_new_track_never_takes_an_owned_song_s_position(
    world: DeliveryWorld, catalog: Catalog, client: SubsonicClient
) -> None:
    album, release = filled(world, catalog, "occupied")
    later, extra = grown(release, number=2)  # where the owned "Two" is
    listed(world, catalog, later)
    rows = world.placeholder_rows()
    assert star(client, extra) == 0
    assert songs(world, album)[2] == "Two" and world.placeholder_rows() == rows
    assert len(world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]) == 4


def test_a_new_track_never_takes_a_placeholder_s_position(
    world: DeliveryWorld, catalog: Catalog, client: SubsonicClient
) -> None:
    album, release = filled(world, catalog, "placeholder")
    later, extra = grown(release, number=3)  # where the release's own third track is
    listed(world, catalog, later)
    rows = world.placeholder_rows()
    assert star(client, extra) == 0
    assert world.placeholder_rows() == rows
    assert len(world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]) == 4


def test_a_later_track_of_the_release_joins_the_album(
    world: DeliveryWorld, catalog: Catalog, client: SubsonicClient
) -> None:
    """The path itself: a numbered, whole release with one more track at a free position."""
    album, release = filled(world, catalog, "fits")
    later, extra = grown(release)
    listed(world, catalog, later)
    assert star(client, extra) is None
    assert songs(world, album) == {
        1: f"{release.title} Song 1",
        2: "Two",
        3: f"{release.title} Song 3",
        4: "Four",
        5: "Later Song",
    }
    starred = world.nd.client().ok("getStarred2")["starred2"]["song"]
    assert "Later Song" in [s["title"] for s in starred]


def test_a_fill_taken_out_comes_back_only_where_its_places_are_still_free(
    world: DeliveryWorld, catalog: Catalog
) -> None:
    """A fill that was taken out as unused is put back from its record when the album is
    filled again: the same files at the same places. When the album has a song at one of
    those places by then, the record is not used. The release's tracks are added anew
    where they pass the checks of every addition; where they do not, nothing is written
    and the record stays, with the old IDs it keeps alive."""
    album, release = filled(world, catalog, "restored")
    ref = str(release.ref)
    assert world.server.call(lambda: world.services.engine.remove_release(ref)).removed == 2
    assert sorted(songs(world, album)) == [2, 4]
    owned = {
        int(s["track"]): str(s["id"])
        for s in world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]
    }
    links = {t.ref: owned[t.number] for t in release.tracks if t.number in owned}
    # The album gains a song at track 1 that is not the release's first track.
    write_album(
        world.nd.music, dataclasses.replace(OWNED["restored"], tracks=(Track("An Intro", 1),))
    )
    world.nd.scan()
    assert songs(world, album) == {1: "An Intro", 2: "Two", 4: "Four"}
    rows = world.placeholder_rows()
    engine = world.services.engine
    with pytest.raises(MaterializeError, match="another song's place"):
        world.materialize(release, owned_album_id=album, links=links)
    assert world.placeholder_rows() == rows
    assert songs(world, album) == {1: "An Intro", 2: "Two", 4: "Four"}
    assert len(world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]) == 3
    assert world.server.call(lambda: engine.removed_record(ref)) is not None  # kept
    # The catalog lists the release's first track elsewhere now: added anew, it fits.
    first, *others = release.tracks
    moved = dataclasses.replace(release, tracks=(dataclasses.replace(first, number=5), *others))
    world.materialize(moved, owned_album_id=album, links=links)
    assert world.server.call(lambda: engine.removed_record(ref)) is None
    assert world.placeholder_rows() == rows + 2
    assert songs(world, album) == {
        1: "An Intro",
        2: "Two",
        3: f"{release.title} Song 3",
        4: "Four",
        5: f"{release.title} Song 1",
    }


def test_a_record_stays_when_the_release_cannot_be_added_anew(
    world: DeliveryWorld, catalog: Catalog
) -> None:
    """A track the taken-out fill had a placeholder for is owned by now, so its record no
    longer fits - but the catalog's list of the release is incomplete, and nothing is
    added anew from that: the record is not given up for a fill that is refused."""
    album, release = filled(world, catalog, "kept")
    ref = str(release.ref)
    engine = world.services.engine
    assert world.server.call(lambda: engine.remove_release(ref)).removed == 2
    first = release.tracks[0]
    write_album(world.nd.music, dataclasses.replace(OWNED["kept"], tracks=(Track(first.title, 1),)))
    world.nd.scan()
    owned = {
        int(s["track"]): str(s["id"])
        for s in world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]
    }
    links = {t.ref: owned[t.number] for t in release.tracks if t.number in owned}
    assert len(links) == 3
    rows = world.placeholder_rows()
    with pytest.raises(MaterializeError, match="incomplete"):
        world.materialize(
            dataclasses.replace(release, incomplete=True), owned_album_id=album, links=links
        )
    assert world.server.call(lambda: engine.removed_record(ref)) is not None
    assert world.placeholder_rows() == rows and sorted(songs(world, album)) == [1, 2, 4]
    # Read in full, the release is added anew: its third track alone is missing now.
    world.materialize(release, owned_album_id=album, links=links)
    assert world.server.call(lambda: engine.removed_record(ref)) is None
    assert world.placeholder_rows() == rows + 1 and sorted(songs(world, album)) == [1, 2, 3, 4]
