"""Search and artist-page additions: query handling and de-duplication against the library
(with the edition rules)."""

from __future__ import annotations

import dataclasses
from typing import Any

import anyio
import pytest

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.editions import LibraryAlbum, dedupe_editions, merge_twins
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    Twins,
)
from shijhon.proxy.params import RestCall
from shijhon.views.additions import (
    CatalogAdditions,
    _count,
    _same_song,
    _term,
    _wanted,
    library_album,
)


def release(
    ref: str, title: str, date: str, tracks: int, kind: ReleaseKind = ReleaseKind.ALBUM
) -> CatalogRelease:
    return CatalogRelease(
        CatalogRef("demo", ref), title, "Mara Vance", kind, date, track_count=tracks
    )


def cards(releases: list[CatalogRelease], *library: LibraryAlbum) -> list[str]:
    return [r.ref.id for r in dedupe_editions(releases, library)]


def call(query: str) -> RestCall:
    return RestCall.build("search3", "GET", b"/rest/search3", query.encode(), [], None)


def test_search_terms() -> None:
    assert _term("  Salt   Harbor ") == "salt harbor"
    assert _term('""') == _term("") == ""
    assert _term('"quoted"') == "quoted"


def test_requested_counts_and_first_pages() -> None:
    assert _wanted(call("query=x")) == {"artist": 20, "album": 20, "song": 20}
    assert _wanted(call("query=x&albumOffset=20&songCount=0")) == {"artist": 20}
    assert _wanted(call("query=x&songCount=5000")) == {}  # a sync, not a search
    assert _wanted(call("query=x&albumCount=many")) == {}


def test_a_library_album_hides_its_card_whatever_its_year_and_size() -> None:
    """Owned tags and the catalog often disagree on years; a partly owned album is still
    that album."""
    catalog = [release("std", "Salt Harbor", "1984-05-01", 12)]
    owned = LibraryAlbum("Salt Harbor", "Mara Vance", 1986, 3)
    assert cards(catalog, owned) == []
    # A self-titled series: the library album covers the album of its year (or the latest
    # earlier one), not the others.
    series = [
        release("s1", "Mara Vance", "2001-05-01", 10),
        release("s2", "Mara Vance", "2004-05-01", 11),
    ]
    assert cards(series, LibraryAlbum("Mara Vance", "Mara Vance", 2005, 11)) == ["s1"]


def test_much_larger_editions_stay_next_to_a_library_album() -> None:
    catalog = [
        release("std", "Salt Harbor", "1984-05-01", 12),
        release("big", "Salt Harbor (Super Deluxe Edition)", "1984-05-01", 40),
    ]
    assert cards(catalog, LibraryAlbum("Salt Harbor", "Mara Vance", 1984, 5)) == ["big"]
    # The library has the super deluxe: the standard edition still shows.
    owned = LibraryAlbum("Salt Harbor (Super Deluxe Edition)", "Mara Vance", 1984, 40)
    assert cards(catalog, owned) == ["std"]


def test_a_partly_owned_edition_never_sets_a_card_s_size() -> None:
    """The library may hold only some tracks of an edition."""
    remaster = [release("rm", "X (Remastered)", "1990-05-01", 12)]
    assert cards(remaster, LibraryAlbum("X", "Mara Vance", 1990, 3)) == []
    deluxe = [
        release("dx", "X (Deluxe Edition)", "1990-05-01", 18),
        release("sd", "X (Super Deluxe Edition)", "1990-05-01", 40),
    ]
    assert cards(deluxe, LibraryAlbum("X (Deluxe Edition)", "Mara Vance", 1990, 5)) == ["sd"]
    both = [release("std", "X", "1990-05-01", 12), deluxe[1]]
    owned = LibraryAlbum("X (Super Deluxe Edition)", "Mara Vance", 1990, 15)
    assert cards(both, owned) == ["std"]


def test_release_types_count_only_when_known() -> None:
    single = [release("sg", "Salt Harbor - Single", "1984-05-01", 1, ReleaseKind.SINGLE)]
    assert cards(single, LibraryAlbum("Salt Harbor", "Mara Vance")) == []
    assert cards(single, LibraryAlbum("Salt Harbor", "Mara Vance", kind=ReleaseKind.ALBUM)) == [
        "sg"
    ]
    # Albums and compilations are the same kind of card.
    soundtrack = [release("st", "Film", "2001-05-01", 20, ReleaseKind.COMPILATION)]
    assert cards(soundtrack, LibraryAlbum("Film", "Mara Vance", kind=ReleaseKind.ALBUM)) == []


def test_other_artists_and_titles_are_not_hidden() -> None:
    catalog = [release("std", "Salt Harbor", "1984-05-01", 12)]
    assert cards(catalog, LibraryAlbum("Salt Harbor", "Somebody Else")) == ["std"]
    assert cards(catalog, LibraryAlbum("Salt Harbor (Live)", "Mara Vance")) == ["std"]
    assert cards(catalog, LibraryAlbum("Salt Harbor", "Mara Vance & Guest")) == []


def test_navidrome_album_entries() -> None:
    entry = {"name": "A", "artist": "B", "year": 1999, "songCount": 7, "releaseTypes": ["EP"]}
    assert library_album(entry) == LibraryAlbum("A", "B", 1999, 7, ReleaseKind.EP)
    plain = {"name": "A", "artist": "B", "year": 0, "songCount": 7, "releaseTypes": []}
    assert library_album(plain).kind is None and library_album(plain).year is None
    assert library_album({**plain, "isCompilation": True}).kind == ReleaseKind.COMPILATION


def test_the_same_song_by_title_artist_and_length() -> None:
    track = CatalogTrack(CatalogRef("demo", "1"), "Open Door (Remastered)", "Mara Vance", 201_900)
    song = {"title": "Open Door", "artist": "Mara Vance feat. Guest", "duration": 201}
    assert _same_song(track, song)
    assert not _same_song(track, {**song, "duration": 196})
    assert not _same_song(track, {**song, "title": "Open Door (Live)"})
    assert not _same_song(track, {**song, "artist": "Somebody Else"})


class Stub:
    """A catalog with one artist of the searched name."""

    key = "demo"
    region = "xx"

    def __init__(self, *, delay: float = 0.0, error: str | None = None) -> None:
        self.delay, self.error = delay, error
        self.searches: list[str] = []

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        self.searches.append(term)
        await anyio.sleep(self.delay)
        if self.error:
            raise CatalogError(self.error, "test")
        return SearchResults(artists=(CatalogArtist(CatalogRef("demo", "1"), term.upper()),))

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        return (release("r1", "Some Album", "2001-01-01", 10),)


def additions(stub: Stub, **kwargs: Any) -> CatalogAdditions:
    return CatalogAdditions(stub, None, None, **kwargs)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_short_artist_names_get_artist_page_additions() -> None:
    stub = Stub()
    found = await additions(stub)._discography(stub, "Xo", [])  # type: ignore[arg-type]
    assert [r.ref.id for r in found] == ["r1"]
    assert await additions(stub)._discography(stub, "Various Artists", []) == ()  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_catalog_lookups_are_shared_bounded_and_rest_after_failures() -> None:
    stub = Stub(delay=0.2)
    shared = additions(stub, budget_seconds=1.0)

    async def look(key: str) -> Any:
        return await shared._within_budget(key, lambda: stub.search(key))

    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(look, "same")
    assert stub.searches == ["same"]  # one lookup for three requests
    failing = Stub(error="unavailable")
    resting = additions(failing, rest_seconds=60.0)
    assert await resting._within_budget("a", lambda: failing.search("a")) == (None, "failed")
    assert await resting._within_budget("b", lambda: failing.search("b")) == (
        None,
        "skipped: resting after a failure",
    )
    assert failing.searches == ["a"]  # resting after the failure


def test_a_count_is_read_as_navidrome_reads_it() -> None:
    """Go's ParseInt, base 10, 64 bits - anything else is the
    default (Python's int() also takes spaces, underscores and other scripts' digits)."""
    assert [_count(text, 50) for text in ("5", "+7", "-1", "007")] == [5, 7, -1, 7]
    assert _count(str(2**63 - 1), 50) == 2**63 - 1 and _count(str(-(2**63)), 50) == -(2**63)
    for text in (None, "", " 1 ", "1_0", "1.0", "ten", "0x10", "\u0663", str(2**63)):
        assert _count(text, 50) == 50, text
    # However long: zeros before a number are read, digits past 64 bits are not.
    assert _count("0" * 5000 + "12", 50) == 12 and _count("-" + "0" * 5000, 50) == 0
    assert _count("9" * 5000, 50) == 50 and _count("-" + "9" * 5000, 50) == 50


class Naming:
    """Navidrome, as the service account: an artist by its ID, after a while."""

    def __init__(self, delay: float) -> None:
        self.delay, self.calls = delay, 0

    async def subsonic(self, method: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        assert method == "getArtist"
        self.calls += 1
        await anyio.sleep(self.delay)
        return {"artist": {"id": dict(params)["id"], "name": "A Guest"}}


@pytest.mark.anyio
async def test_a_library_artists_name_is_looked_up_once_and_within_the_budget() -> None:
    """``getTopSongs`` by a library artist's own ID. The artist's name
    is looked up within the request's one budget - when that ends, Navidrome's own answer
    (None: forwarded) - and requests at the same time share one lookup, remembered."""
    slow = Naming(delay=1.0)
    views = CatalogAdditions(
        Stub(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        navidrome=slow,  # type: ignore[arg-type]
        budget_seconds=0.1,
    )

    class Caller:
        async def caller(self) -> Any:
            return dataclasses.make_dataclass("Who", ["username"])("someone")

    asked = RestCall.build(
        "getTopSongs", "GET", b"/rest/getTopSongs", b"id=guest-1&f=json&c=t", [], None
    )
    started = anyio.current_time()
    assert await views.top_songs(asked, Caller()) is None  # type: ignore[arg-type]
    assert anyio.current_time() - started < 0.6  # (its budget, not the lookup's second)
    quick = Naming(delay=0.05)
    views.navidrome = quick  # type: ignore[assignment]
    names: list[str | None] = []

    async def look() -> None:
        names.append(await views._library_name("guest-1"))

    async with anyio.create_task_group() as group:
        for _ in range(5):
            group.start_soon(look)
    assert names == ["A Guest"] * 5 and quick.calls == 1
    assert await views._library_name("guest-1") == "A Guest" and quick.calls == 1  # remembered
    assert await views._library_name("guest-2") == "A Guest" and quick.calls == 2


def song(ident: str, title: str, artist: str, ms: int, **flags: bool) -> CatalogTrack:
    return CatalogTrack(CatalogRef("x", ident), title, artist, ms, **flags)


def test_songs_without_flags_merge_on_title_artist_and_length() -> None:
    """A catalog that does not flag clean and explicit versions."""
    tracks = [
        song("1", "Open Door", "Mara Vance", 200_000),
        song("2", "Open Door", "Mara Vance feat. Guest", 201_500),  # within 2 s
        song("3", "Open Door", "Mara Vance", 205_000),  # another length
        song("4", "Open Door (Live)", "Mara Vance", 200_000),
        song("5", "Open Door", "Somebody Else", 200_000),
    ]
    assert [t.ref.id for t in merge_twins(tracks, Twins.EXPLICIT)] == ["1", "3", "4", "5"]


def test_clean_and_explicit_songs_follow_the_setting() -> None:
    twins = [
        song("c", "Open Door", "Mara Vance", 200_000, clean=True),
        song("e", "Open Door", "Mara Vance", 200_400, explicit=True),
    ]
    assert [t.ref.id for t in merge_twins(twins, Twins.EXPLICIT)] == ["e"]
    assert [t.ref.id for t in merge_twins(twins, Twins.CLEAN)] == ["c"]
    assert [t.ref.id for t in merge_twins(twins, Twins.BOTH)] == ["c", "e"]


def test_clean_and_explicit_albums_follow_the_setting() -> None:
    explicit = dataclasses.replace(release("e", "Open Door", "2020-01-01", 10), explicit=True)
    clean = dataclasses.replace(release("c", "Open Door", "2020-01-01", 10), clean=True)
    assert dedupe_editions([clean, explicit]) == [explicit]
    assert dedupe_editions([clean, explicit], twins=Twins.CLEAN) == [clean]
    assert dedupe_editions([clean, explicit], twins=Twins.BOTH) == [clean, explicit]
    # With both, a library album hides only the card of its own kind.
    owned = LibraryAlbum("Open Door", explicit.artist, 2020, clean=True)
    assert dedupe_editions([clean, explicit], [owned], twins=Twins.BOTH) == [explicit]
    assert dedupe_editions([clean, explicit], [owned]) == []


def test_look_alikes_that_are_not_twins_stay() -> None:
    """With flags, only a clean song and one that is not clean are twins; other suffixes
    name other recordings."""
    tracks = [
        song("e", "Open Door", "Mara Vance", 200_000, explicit=True),
        song("k", "Open Door (Karaoke Version)", "Mara Vance", 200_300),
        song("c", "Open Door", "Mara Vance", 200_100, clean=True),
        song("e2", "Open Door", "Mara Vance", 200_000, explicit=True),  # another release
    ]
    assert [t.ref.id for t in merge_twins(tracks, Twins.EXPLICIT)] == ["e", "k", "e2"]
