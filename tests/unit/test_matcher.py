"""Suite H (units) - matching owned albums to catalog releases."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    Twins,
)
from shijhon.matching.matcher import Matcher, Outcome, OwnedAlbum, OwnedSong, complete_by_tags

ARTIST = "Mara Vance"
TITLES = ["Open Door", "Quiet Harbor", "Paper Tides", "Low Light", "Echoing"]


def release(
    ident: str,
    title: str = "Harbor Lights",
    *,
    count: int = 5,
    year: str = "2010-01-01",
    explicit: bool = False,
    clean: bool = False,
    isrc_prefix: str = "ZZ",
    artist: str = ARTIST,
    names: list[str] | None = None,
) -> CatalogRelease:
    ref = CatalogRef("x", ident)
    names = names or TITLES + [f"Bonus {n}" for n in range(1, 10)]
    tracks = tuple(
        CatalogTrack(
            CatalogRef("x", f"{ident}-{n}"),
            names[n - 1],
            ARTIST,
            200_000 + n * 10_000,
            number=n,
            isrc=f"{isrc_prefix}{ident.upper():>3}{n:05d}",
            album=ref,
            album_title=title,
        )
        for n in range(1, count + 1)
    )
    return CatalogRelease(
        ref, title, artist, ReleaseKind.ALBUM, year, tracks, explicit=explicit, clean=clean
    )


def owned(numbers: list[int], *, title: str = "Harbor Lights", **kwargs: Any) -> OwnedAlbum:
    songs = tuple(
        OwnedSong(
            f"song-{n}",
            TITLES[n - 1],
            ARTIST,
            1,
            n,
            200_000 + n * 10_000 + 1_200,  # a little longer: whole seconds, other masters
        )
        for n in numbers
    )
    return OwnedAlbum("album-1", title, ARTIST, kwargs.pop("year", 2010), songs, **kwargs)


class Catalog:
    key = "x"
    region = "xx"

    def __init__(
        self, *releases: CatalogRelease, by_isrc: bool = True, albums: bool = True
    ) -> None:
        self.releases = {r.ref.id: r for r in releases}
        self.by_isrc = by_isrc
        self.albums = albums  # whether the search finds albums
        self.searches: list[str] = []
        self.fetched: list[str] = []

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        self.searches.append(term)
        releases = self.releases.values() if self.albums else ()
        albums = tuple(dataclasses.replace(r, tracks=()) for r in releases)
        songs = tuple(t for r in self.releases.values() for t in r.tracks)
        return SearchResults(albums=albums, songs=songs)

    async def album(self, album_id: str) -> CatalogRelease:
        self.fetched.append(album_id)
        if album_id not in self.releases:
            raise CatalogError("not_found", "test")
        return self.releases[album_id]

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        if not self.by_isrc:
            raise CatalogError("not_found", "test")
        return tuple(t for r in self.releases.values() for t in r.tracks if t.isrc == isrc)


async def match(album: OwnedAlbum, *releases: CatalogRelease, **options: Any) -> Any:
    twins = options.pop("twins", Twins.EXPLICIT)
    catalog = Catalog(*releases, **options)
    return await Matcher(catalog, twins=twins).match(album)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_a_partial_album_is_filled_from_its_release() -> None:
    found = await match(owned([1, 3]), release("a"), release("other", "Something Else"))
    assert found.outcome is Outcome.FILL and found.release.ref.id == "a"
    assert found.links == {CatalogRef("x", "a-1"): "song-1", CatalogRef("x", "a-3"): "song-3"}
    assert found.missing == 3


@pytest.mark.anyio
async def test_every_owned_track_must_be_on_the_release() -> None:
    album = owned([1, 2])
    wrong = dataclasses.replace(album.songs[1], duration_ms=album.songs[1].duration_ms + 9_000)
    found = await match(dataclasses.replace(album, songs=(album.songs[0], wrong)), release("a"))
    assert found.outcome is Outcome.NONE


def with_isrcs(album: OwnedAlbum, ident: str) -> OwnedAlbum:
    songs = tuple(
        dataclasses.replace(s, isrcs=(f"ZZ{ident.upper():>3}{s.number:05d}",)) for s in album.songs
    )
    return dataclasses.replace(album, songs=songs)


@pytest.mark.anyio
async def test_isrcs_find_a_release_the_album_search_misses() -> None:
    found = await match(with_isrcs(owned([1, 2]), "a"), release("a"), albums=False)
    assert found.outcome is Outcome.FILL and found.release.ref.id == "a"


@pytest.mark.anyio
async def test_compilations_with_the_recordings_never_fill_the_album() -> None:
    """A popular recording's ISRC is on many releases: only the album's title counts."""
    album = with_isrcs(owned([1, 2]), "a")
    hits = [
        dataclasses.replace(
            release("a", f"Summer Hits {n}", artist="Various Artists"),
            ref=CatalogRef("x", f"c{n}"),
        )
        for n in range(7)
    ]
    found = await match(album, *hits, release("a"))
    assert found.outcome is Outcome.FILL and found.release.ref.id == "a"
    found = await match(album, *hits)  # only compilations
    assert found.outcome is Outcome.REVIEW and "other titles" in found.reason


@pytest.mark.anyio
async def test_the_edition_of_the_owned_year_is_chosen() -> None:
    older = release("old", year="2001-05-05")
    newer = release("new", year="2010-02-02")
    found = await match(owned([1, 2], year=2010), older, newer)
    assert found.outcome is Outcome.FILL and found.release.ref.id == "new"


@pytest.mark.anyio
async def test_explicit_twin_unless_the_owned_files_are_clean() -> None:
    explicit = release("e", explicit=True)
    clean = release("c", clean=True)
    found = await match(owned([1]), clean, explicit)
    assert found.release.ref.id == "e"
    found = await match(owned([1], clean=True), explicit, clean)
    assert found.release.ref.id == "c"


@pytest.mark.anyio
async def test_a_complete_album_gets_no_bonus_tracks() -> None:
    standard = release("std")
    deluxe = release("dlx", "Harbor Lights (Deluxe)", count=9)
    found = await match(owned([1, 2, 3, 4, 5]), deluxe, standard)
    assert found.outcome is Outcome.COMPLETE and found.release.ref.id == "std"
    # Only the larger edition in the catalog: it may be bonus tracks only - review.
    found = await match(owned([1, 2, 3, 4, 5]), deluxe)
    assert found.outcome is Outcome.REVIEW


@pytest.mark.anyio
async def test_a_complete_album_stays_complete_beside_a_larger_reissue() -> None:
    """Completeness comes before the year: an unmarked reissue with more tracks does not
    add them."""
    original = release("orig", count=5, year="1999-01-01")
    reissue = release("re", count=9, year="2015-01-01")
    found = await match(owned([1, 2, 3, 4, 5], year=2015), reissue, original)
    assert found.outcome is Outcome.COMPLETE and found.release.ref.id == "orig"


@pytest.mark.anyio
async def test_a_remaster_fills_an_album_with_a_gap() -> None:
    remaster = release("rm", "Harbor Lights (2011 Remaster)")
    found = await match(owned([2, 4]), remaster)
    assert found.outcome is Outcome.FILL and found.release.ref.id == "rm"
    deluxe = release("dx", "Harbor Lights (Deluxe)", count=9)
    found = await match(owned([2, 4]), deluxe)  # tracks missing inside: not a whole album
    assert found.outcome is Outcome.FILL


@pytest.mark.anyio
async def test_a_release_numbered_differently_goes_to_review() -> None:
    """A new track would take an owned track's number."""
    names = ["Quiet Harbor", "Open Door", "Bonus 1", "Paper Tides"]
    shuffled = release("sh", count=4, names=names)
    album = owned([1, 2, 3])
    songs = tuple(
        dataclasses.replace(
            s, duration_ms=next(t.duration_ms for t in shuffled.tracks if t.title == s.title) + 500
        )
        for s in album.songs
    )
    found = await match(dataclasses.replace(album, songs=songs), shuffled)
    assert found.outcome is Outcome.REVIEW and "numbers" in found.reason


@pytest.mark.anyio
async def test_a_release_without_track_numbers_fills_only_files_numbered_like_its_order() -> None:
    """A catalog that sends no track numbers counts a release's tracks in the order it
    lists them (``numbered`` off): filled only when every owned song sits where that order
    puts it - else the added tracks would get places the owned numbering does not have."""
    listed = dataclasses.replace(release("un"), numbered=False)
    found = await match(owned([2, 4]), listed)
    assert found.outcome is Outcome.FILL  # the owned files agree with the order
    album = owned([2, 4])
    second_disc = tuple(dataclasses.replace(s, disc=2, number=s.number - 1) for s in album.songs)
    found = await match(dataclasses.replace(album, songs=second_disc), listed)
    assert found.outcome is Outcome.REVIEW and "without numbers" in found.reason
    # The same owned files and a release with the catalog's own numbers: as before.
    found = await match(dataclasses.replace(album, songs=second_disc), release("nu"))
    assert found.outcome is Outcome.FILL


@pytest.mark.anyio
async def test_an_incomplete_release_says_nothing_about_what_is_complete() -> None:
    """A release whose track list could not be read in full (a track left out): owning
    every track that was read does not make the album complete, and nothing is filled from
    it - it goes to review. The owned files' own totals still can say "complete"."""
    partial = dataclasses.replace(release("pt", count=2), incomplete=True, track_count=3)
    found = await match(owned([1, 2]), partial)
    assert found.outcome is Outcome.REVIEW and "could not be read in full" in found.reason
    found = await match(owned([1]), partial)
    assert found.outcome is Outcome.REVIEW
    whole = await match(owned([1, 2]), release("wh", count=2))
    assert whole.outcome is Outcome.COMPLETE
    album = owned([1, 2])
    tagged = tuple(dataclasses.replace(s, track_total=2) for s in album.songs)
    by_tags = await match(dataclasses.replace(album, songs=tagged), partial)
    assert by_tags.outcome is Outcome.COMPLETE and "track totals" in by_tags.reason


@pytest.mark.anyio
async def test_owned_files_without_a_rating_follow_the_twins_setting() -> None:
    explicit, clean = release("e", explicit=True), release("c", clean=True)
    found = await match(owned([1], clean=None), explicit, clean, twins=Twins.CLEAN)
    assert found.release.ref.id == "c"
    found = await match(owned([1], clean=None), clean, explicit)
    assert found.release.ref.id == "e"


@pytest.mark.anyio
async def test_several_plausible_editions_go_to_review() -> None:
    one, two = release("one"), release("two")  # identical as far as the album can tell
    found = await match(owned([1, 2]), one, two)
    assert found.outcome is Outcome.REVIEW and sorted(found.candidates) == ["x:one", "x:two"]


@pytest.mark.anyio
async def test_nothing_found_is_no_match() -> None:
    # Neither the album nor the first owned track is found.
    found = await match(owned([3]), release("a", "Another Album", count=2, isrc_prefix="YY"))
    assert found.outcome is Outcome.NONE


def with_totals(album: OwnedAlbum, tracks: int | None, discs: int | None = None) -> OwnedAlbum:
    songs = tuple(dataclasses.replace(s, track_total=tracks, disc_total=discs) for s in album.songs)
    return dataclasses.replace(album, songs=songs)


@pytest.mark.anyio
async def test_owned_files_whose_track_totals_are_all_owned_are_complete() -> None:
    """The owned files say the album is complete: no tracks are added, whatever the
    catalog has, and the catalog is not asked."""
    reissue = release("re", count=9)  # the same title, more tracks, no edition marker
    catalog = Catalog(reissue)
    found = await Matcher(catalog).match(with_totals(owned([1, 2, 3, 4, 5]), 5))  # type: ignore[arg-type]
    assert found.outcome is Outcome.COMPLETE and found.release is None
    assert catalog.searches == [] and catalog.fetched == []
    # Without the tags: filled, as before.
    found = await match(owned([1, 2, 3, 4, 5]), reissue)
    assert found.outcome is Outcome.FILL


@pytest.mark.anyio
async def test_track_totals_with_tracks_missing_keep_the_matching() -> None:
    reissue = release("re", count=9)
    for album in (
        with_totals(owned([1, 2, 4, 5]), 5),  # a gap
        with_totals(owned([1, 2, 3, 4, 5]), 7),  # the files say there are more
        with_totals(owned([1, 2, 3, 4, 5]), 5, 2),  # the second disc is missing
    ):
        assert (await match(album, reissue)).outcome is Outcome.FILL


def test_track_and_disc_totals() -> None:
    def album(*songs: tuple[int, int, int | None, int | None], tagged: bool = True) -> OwnedAlbum:
        return OwnedAlbum(
            "a",
            "T",
            ARTIST,
            None,
            tuple(
                OwnedSong(f"s{d}-{n}", f"t{n}", ARTIST, d, n, 1000, (), total, discs, tagged)
                for d, n, total, discs in songs
            ),
        )

    assert complete_by_tags(album((1, 1, 2, None), (1, 2, 2, None), tagged=False))
    # A disc number without a disc total: perhaps disc 1 of a set.
    assert not complete_by_tags(album((1, 1, 2, None), (1, 2, 2, None)))
    assert complete_by_tags(album((1, 1, 2, 1), (1, 2, 2, 1)))
    assert complete_by_tags(album((1, 1, 1, 2), (2, 1, 2, 2), (2, 2, 2, 2)))  # per disc
    assert not complete_by_tags(album((1, 1, 2, None), (1, 2, None, None)))  # one untagged
    assert not complete_by_tags(album((1, 1, 2, None), (1, 2, 3, None)))  # they disagree
    # Two discs without a disc total: it cannot tell whether there is a third.
    assert not complete_by_tags(album((1, 1, 1, None), (2, 1, 1, None)))
    assert not complete_by_tags(album((1, 1, 1, 3), (2, 1, 1, 3)))  # disc 3 missing
