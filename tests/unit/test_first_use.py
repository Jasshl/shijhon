"""First use of an exact catalog release: which owned album it is added to, if any."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.fill.fills import (
    JOINED,
    ChoiceRefused,
    Decision,
    Fills,
    _would_group,
    album_facts,
    joined_links,
    release_facts,
)
from shijhon.matching.matcher import Matcher, OwnedAlbum, OwnedSong, confirm
from shijhon.placeholders.engine import ApprovalRefused, MaterializeError, PlaceholderEngine
from shijhon.store import Store

ARTIST = "Various Artists"
TITLES = ("Arrival", "Lanterns", "Crossing", "Afterglow", "Return")
RELEASE = CatalogRelease(
    CatalogRef("demo", "letters"),
    "Night Letters (Deluxe Edition)",
    ARTIST,
    ReleaseKind.COMPILATION,
    "2020",
    tracks=tuple(
        CatalogTrack(CatalogRef("demo", f"letters{n}"), title, f"Singer {n}", 3000, number=n)
        for n, title in enumerate(TITLES, 1)
    ),
)


def owned(
    ident: str, title: str, songs: Sequence[tuple[str, int]], artist: str = ARTIST
) -> OwnedAlbum:
    return OwnedAlbum(
        ident,
        title,
        artist,
        2020,
        tuple(OwnedSong(f"{ident}-{n}", name, "x", 1, n, 3000) for name, n in songs),
    )


FITS = owned("fits", "NightLetters", [("Lanterns", 2), ("Afterglow", 4)])  # compacted title
OTHER = owned("other", "Night Letters", [("Earlier Song", 1)])  # the same title, other songs


def fillers(count: int, artist: str = ARTIST) -> list[dict[str, Any]]:
    return [{"id": f"f{n}", "name": f"Aa Filler {n:04d}", "artist": artist} for n in range(count)]


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


class Library:
    """Navidrome's album search: every word of the query starts a word of the album's
    name or artist; the first ``albumCount`` albums (Shijhon sends no offset). Owned albums'
    entries describe files with a recording date only (``releaseDate`` {})."""

    def __init__(
        self,
        albums: list[OwnedAlbum],
        others: list[dict[str, Any]],
        entries: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.owned = {album.id: album for album in albums}
        self.albums = others + [
            {"id": a.id, "name": a.title, "artist": a.artist, "releaseDate": {}, "version": ""}
            | (entries or {}).get(a.id, {})
            for a in albums
        ]
        self.queries: list[tuple[str, int]] = []
        self.counts: list[int] = []
        self._found: dict[str, list[dict[str, Any]]] = {}

    async def subsonic(self, method: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        assert method == "search3"
        given = dict(params)
        assert "albumOffset" not in given  # one consistent answer per search, no pages
        query, count, offset = given["query"], int(given["albumCount"]), 0
        self.queries.append((query, offset))
        self.counts.append(count)
        if query not in self._found:
            self._found[query] = [
                album
                for album in self.albums
                if all(
                    any(w.startswith(q) for w in words(f"{album['name']} {album['artist']}"))
                    for q in words(query)
                )
            ]
        return {"searchResult3": {"album": self._found[query][offset : offset + count]}}


@asynccontextmanager
async def fills(tmp_path: Path, library: Library) -> AsyncIterator[Fills]:
    store = await Store.open(tmp_path / "db")
    try:

        async def album(ident: str) -> dict[str, Any]:
            return {"name": library.owned[ident].title, "artist": library.owned[ident].artist}

        result = Fills(
            store,
            SimpleNamespace(album=album, subsonic=library.subsonic),
            SimpleNamespace(),
            Matcher(SimpleNamespace()),
            scope="demo.xx",
        )

        async def owned_album(ident: str) -> OwnedAlbum | None:
            return library.owned.get(ident)

        async def materialize(
            album_id: str, owned: OwnedAlbum, decision: Decision, trigger: str | None = None
        ) -> None:
            library.queries.append((f"filled {album_id}", 0))

        result._owned = owned_album  # type: ignore[method-assign]
        result._materialize = materialize  # type: ignore[method-assign]
        yield result
    finally:
        await store.close()


def test_every_search_is_read_to_its_end(tmp_path: Path) -> None:
    """An owned album after a thousand others of the artist, its title written without a
    space ("NightLetters"), is found by the artist search; the title search is read too.
    Each search is one request for every album it finds, up to what the bound leaves."""
    library = Library([FITS], fillers(1200))

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            assert [o for _, o in await rig._same_title(RELEASE)] == [FITS]
        assert library.queries == [(ARTIST, 0), ("Night Letters", 0)]  # edition words aside
        assert library.counts == [20_001, 20_001 - 1201]

    anyio.run(run)


def test_an_artist_written_otherwise_is_found_by_the_title(tmp_path: Path) -> None:
    """The catalog credits "TidePaper", the owner's files "Tide Paper" (one artist when
    compared, no word in common for Navidrome's search): the title search finds the owned
    album. A compatible one is filled; another album of the title by that artist is not,
    and Navidrome keeps it apart from the release (another album artist tag), so the
    release is added as its own album."""
    release = replace(RELEASE, artist="TidePaper")
    fits = replace(FITS, title="Night Letters", artist="Tide Paper")
    other = replace(OTHER, artist="Tide Paper")
    dated = {other.id: {"releaseDate": {"year": 2020}}}  # the release's date, too

    async def run() -> None:
        library = Library([fits], fillers(30, "Tide Paper"))
        async with fills(tmp_path / "one", library) as rig:
            assert await rig.first_use(release, "star") == fits.id
        assert library.queries == [("TidePaper", 0), ("Night Letters", 0), ("filled fits", 0)]
        library = Library([other], fillers(30, "Tide Paper"), dated)
        async with fills(tmp_path / "two", library) as rig:
            assert [o for _, o in await rig._same_title(release)] == [other]
            assert await rig.first_use(release, "star") is None
            assert not await rig.store.fetchall("SELECT * FROM album_matches")

    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    anyio.run(run)


def test_each_credited_name_is_looked_up(tmp_path: Path) -> None:
    """A release credited "A feat. B": owned albums credited to A, or to B, share an
    artist with it - both are found, and two compatible ones are refused."""
    credited = replace(RELEASE, artist="Invented Harbor feat. Guest Singer")
    harbor = replace(FITS, artist="Invented Harbor")
    guest = owned("guest", "Night Letters (Remastered)", [("Arrival", 1)], "Guest Singer")

    async def run() -> None:
        library = Library([harbor], fillers(150, "Invented Harbor"))
        async with fills(tmp_path / "one", library) as rig:
            assert [o for _, o in await rig._same_title(credited)] == [harbor]
        names = [("Invented Harbor", 0), ("Guest Singer", 0), ("Night Letters", 0)]
        assert library.queries == names
        library = Library([harbor, guest], fillers(150, "Invented Harbor"))
        async with fills(tmp_path / "two", library) as rig:
            with pytest.raises(ChoiceRefused) as caught:
                await rig.first_use(credited, "star")
            assert caught.value.category == "owned_ambiguous"

    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    anyio.run(run)


@pytest.mark.parametrize("count", [19_990, 20_050])
def test_the_lookup_reads_up_to_20000_albums(tmp_path: Path, count: int) -> None:
    library = Library([], fillers(count))

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            assert rig.lookup_limit == 20_000
            if count < 20_000:
                assert await rig.first_use(RELEASE, "star") is None  # read: none fits
                assert library.queries == [(ARTIST, 0), ("Night Letters", 0)]
            else:
                with pytest.raises(ChoiceRefused) as caught:
                    await rig.first_use(RELEASE, "star")
                assert caught.value.category == "owned_ambiguous"
                assert library.queries == [(ARTIST, 0)]  # 20,001 albums: more than read

    anyio.run(run)


@pytest.mark.parametrize("titled", [10, 11])
def test_the_bound_is_for_all_searches_together(tmp_path: Path, titled: int) -> None:
    """20 albums of the artist and 10 of the title fill a bound of 30; one more is
    refused, also when it comes in the last, short answer."""
    crowd = [
        {"id": f"t{n}", "name": RELEASE.title, "artist": f"Other Singer {n}"} for n in range(titled)
    ]
    library = Library([], fillers(20) + crowd)

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            rig.lookup_limit = 30
            if titled == 10:
                assert await rig.first_use(RELEASE, "star") is None
            else:
                with pytest.raises(ChoiceRefused) as caught:
                    await rig.first_use(RELEASE, "star")
                assert caught.value.category == "owned_ambiguous"
            assert not await rig.store.fetchall("SELECT * FROM album_matches")
        assert library.counts == [31, 11]

    anyio.run(run)


@pytest.mark.parametrize("title", ["Night Letters", "1"])
def test_a_search_navidrome_ignores_is_not_taken_for_a_complete_one(
    tmp_path: Path, title: str
) -> None:
    """Navidrome answers a one-letter search with nothing: such a search is not sent, the
    others are; with none left to send, refused."""
    release = replace(RELEASE, artist="X", title=title)
    library = Library([], fillers(5, "X"))

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            if title == "1":
                with pytest.raises(ChoiceRefused) as caught:
                    await rig.first_use(release, "star")
                assert caught.value.category == "owned_ambiguous"
            else:
                assert await rig.first_use(release, "star") is None
        assert library.queries == ([] if title == "1" else [("Night Letters", 0)])

    anyio.run(run)


@pytest.mark.parametrize("count", [20, 40])
def test_a_common_title_is_read_within_the_bound_or_refused(tmp_path: Path, count: int) -> None:
    """A title many albums share ("Greatest Hits"): read to its end while the lookup's
    bound allows (none of them fits: an album of its own); beyond it, refused before
    anything is written."""
    hits = replace(RELEASE, title="Greatest Hits", artist="Invented Harbor")
    crowd = [
        {"id": f"g{n}", "name": "Greatest Hits", "artist": f"Other Singer {n}"}
        for n in range(count)
    ]
    library = Library([], crowd)

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            rig.lookup_limit = 30
            if count < 30:
                assert await rig.first_use(hits, "star") is None
                assert library.queries == [("Invented Harbor", 0), ("Greatest Hits", 0)]
            else:
                with pytest.raises(ChoiceRefused) as caught:
                    await rig.first_use(hits, "star")
                assert caught.value.category == "owned_ambiguous"
            assert not await rig.store.fetchall("SELECT * FROM album_matches")

    anyio.run(run)


def test_more_credited_names_than_looked_up_are_refused(tmp_path: Path) -> None:
    library = Library([FITS], [])
    crowd = replace(RELEASE, artist="Ann, Bo, Cy, Di, Ed, Flo, Gus, Hal & Ivy")

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            with pytest.raises(ChoiceRefused):
                await rig.first_use(crowd, "star")
        assert library.queries == []

    anyio.run(run)


@pytest.mark.parametrize("albums", [[], [OTHER]])
def test_no_compatible_owned_album_is_a_release_of_its_own(
    tmp_path: Path, albums: list[OwnedAlbum]
) -> None:
    """None of the artist's albums, or only another album of the release's title (that
    Navidrome tells apart by its release date): first use leaves the library as it is,
    and the caller adds the release as its own album."""
    library = Library(albums, fillers(150))

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            assert await rig.first_use(RELEASE, "star") is None
            assert not await rig.store.fetchall("SELECT * FROM album_matches")
        assert library.queries == [(ARTIST, 0), ("Night Letters", 0)]

    anyio.run(run)


@pytest.mark.parametrize(
    "entry",
    [
        {"name": RELEASE.title, "releaseDate": {"year": 2020}},
        {"name": RELEASE.title.lower(), "releaseDate": {}, "version": ""},
    ],
)
def test_an_owned_album_navidrome_would_group_the_release_with_is_refused(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    """No owned album fits, but Navidrome could put the release's songs into one (the
    same date, or none on either): refused before anything is written."""
    dateless = replace(RELEASE, release_date=None) if not entry["releaseDate"] else RELEASE
    library = Library([OTHER], [], {OTHER.id: entry})

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            with pytest.raises(ChoiceRefused) as caught:
                await rig.first_use(dateless, "star")
            assert caught.value.category == "album_collision"
            assert not await rig.store.fetchall("SELECT * FROM album_matches")

    anyio.run(run)


def test_which_albums_navidrome_would_group_a_release_with() -> None:
    clean = replace(RELEASE, clean=True)
    dated = {
        "name": RELEASE.title,
        "artist": ARTIST,
        "releaseDate": {"year": 2020},
        "version": "",
        "musicBrainzId": "",
    }
    assert _would_group(dated, RELEASE)
    assert _would_group(dated | {"name": RELEASE.title.upper()}, RELEASE)  # any letter case
    assert _would_group(dated | {"artist": ARTIST.lower()}, RELEASE)
    # Another album tag is another album, also with edition words title_key leaves aside;
    # so is another album artist tag, also one a comparison of names takes as the same.
    assert not _would_group(dated | {"name": "Night Letters"}, RELEASE)
    assert not _would_group(dated | {"artist": "VariousArtists"}, RELEASE)
    assert not _would_group(dated | {"musicBrainzId": "an-album-id"}, RELEASE)
    assert not _would_group(dated | {"releaseDate": {"year": 2019}}, RELEASE)
    assert not _would_group(dated | {"releaseDate": {}}, RELEASE)
    assert not _would_group(dated | {"version": "Deluxe"}, RELEASE)
    assert not _would_group(dated, clean)
    # Navidrome names a versioned album "Title (Version)".
    versioned = dated | {"name": f"{RELEASE.title} (Clean)", "version": "Clean"}
    assert _would_group(versioned, clean)
    assert _would_group(versioned | {"version": "clean"}, clean)  # any letter case: in doubt
    day = replace(RELEASE, release_date="2020-05-01")
    assert _would_group(dated | {"releaseDate": {"year": 2020, "month": 5, "day": 1}}, day)
    assert not _would_group(dated, day)
    # Navidrome's album identity: str.Clear(strings.ToLower(tag)), rune by rune.
    curly = replace(RELEASE, title="Owner's Letters", artist="Tide-Paper")
    plain = dated | {"name": "Owner's Letters", "artist": "Tide-Paper"}
    assert _would_group(plain | {"name": "OWNER\u2019S LETTERS"}, curly)  # a curly quote
    assert _would_group(plain | {"artist": "Tide\u2014Paper"}, curly)  # an em dash
    assert _would_group(plain | {"artist": "Tide\u2010Paper"}, curly)  # a hyphen sign
    assert not _would_group(plain | {"artist": "Tide Paper"}, curly)
    assert not _would_group(plain | {"name": "Owners Letters"}, curly)
    sigma = replace(curly, title="\u039f\u0394\u039f\u03a3")  # ΟΔΟΣ: no final-ς context
    assert _would_group(plain | {"name": "\u03bf\u03b4\u03bf\u03c3"}, sigma)
    unknown = {"name": RELEASE.title, "artist": ARTIST, "version": ""}
    assert _would_group(unknown, RELEASE)  # its date unknown
    assert _would_group(dated, replace(RELEASE, release_date="May 2020"))  # read otherwise
    assert _would_group(dated | {"releaseDate": {}}, replace(RELEASE, release_date=None))


def test_an_uncredited_release_is_looked_up_by_its_title(tmp_path: Path) -> None:
    library = Library([OTHER], [])
    uncredited = CatalogRelease(RELEASE.ref, RELEASE.title, "", RELEASE.kind, "2020")

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            assert await rig.first_use(uncredited, "star") is None
            library.albums[-1] |= {"name": RELEASE.title, "releaseDate": {"year": 2020}}
            with pytest.raises(ChoiceRefused) as caught:
                await rig.first_use(uncredited, "star")
            assert caught.value.category == "album_collision"
        assert library.queries == [("Night Letters", 0)] * 2  # every edition of the title

    anyio.run(run)


def test_several_compatible_owned_albums_are_refused(tmp_path: Path) -> None:
    second = owned("second", "Night Letters (Remastered)", [("Arrival", 1)])
    library = Library([FITS, second, OTHER], fillers(600))

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            with pytest.raises(ChoiceRefused) as caught:
                await rig.first_use(RELEASE, "star")
            assert caught.value.category == "owned_ambiguous"

    anyio.run(run)


ISRCS = replace(
    RELEASE,
    tracks=tuple(replace(t, isrc=f"ZZSHJ200000{t.number}") for t in RELEASE.tracks),
)
KNOWN = replace(  # its ISRCs as the release has them
    FITS,
    songs=tuple(replace(s, isrcs=(f"ZZSHJ200000{s.number}",)) for s in FITS.songs),
)


async def deferred(
    rig: Fills, album: OwnedAlbum, release: CatalogRelease = RELEASE
) -> list[tuple[str, CatalogRef]]:
    """A deferred plan of the release for the album, and the fills first use would write."""
    links = confirm(album, release)
    assert links
    ref = str(release.ref)
    await rig._record(album.id, Decision("deferred", "saved", release, links, [ref], ref), album)
    written: list[tuple[str, CatalogRef]] = []

    async def materialize(
        album_id: str, owned: OwnedAlbum, decision: Decision, trigger: str | None = None
    ) -> None:
        assert decision.release is not None
        written.append((album_id, decision.release.ref))

    rig._materialize = materialize  # type: ignore[method-assign]
    return written


@pytest.mark.parametrize("retag", ["year", "isrc", "same isrc", "conflict kept"])
def test_a_retag_that_keeps_an_album_shown_complete_keeps_it_fillable(
    tmp_path: Path, retag: str
) -> None:
    """Views and the library pass compare the owned songs as the plan saw them: a new
    year, an ISRC where the release has none or the release's own ISRC keep the album
    shown complete, and its first use fills it - as does an ISRC that differed from
    the release's already when the album was matched, the album unchanged since."""
    release = RELEASE if retag in ("year", "isrc") else ISRCS
    album = replace(FITS, songs=(replace(FITS.songs[0], isrcs=("ZZSHJ2999999",)), FITS.songs[1]))
    before = album if retag == "conflict kept" else FITS
    library = Library([before], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            written = await deferred(rig, before, release)
            library.owned[FITS.id] = {
                "year": replace(FITS, year=2021),
                "isrc": album,
                "same isrc": KNOWN,
                "conflict kept": album,
            }[retag]
            assert await rig._shown(FITS.id) is not None  # still shown complete
            assert await rig.first_use(release, "star") == FITS.id
            assert written == [(FITS.id, release.ref)]
            assert (await rig._row(FITS.id))["outcome"] == "filled"
        assert library.queries == []  # its saved plan: no lookup

    anyio.run(run)


@pytest.mark.parametrize("retag", ["replaced", "added", "numbered", "moved", "swapped", "legacy"])
def test_a_retag_to_another_recording_needs_a_new_match(tmp_path: Path, retag: str) -> None:
    """A linked song's known ISRC replaced by another (also where the catalog knows no
    ISRC), or an ISRC the release has for another of its tracks: not the recording the
    plan linked. Views and the library pass match the album again; first use refuses as
    before. So does a new number, and any change to a plan saved without the ISRCs it
    saw."""
    lanterns = FITS.songs[0]  # linked to track 2
    known = retag in ("replaced", "swapped")
    release = RELEASE if retag in ("swapped", "legacy") else ISRCS
    if retag == "moved":  # track 2 has no ISRC; track 3's ISRC is now on the owned song
        release = replace(
            ISRCS, tracks=tuple(replace(t, isrc=None) if t.number == 2 else t for t in ISRCS.tracks)
        )
    after = {
        "replaced": replace(lanterns, isrcs=("ZZSHJ2999999",)),
        "added": replace(lanterns, isrcs=("ZZSHJ2999999",)),
        "numbered": replace(lanterns, number=7),
        "moved": replace(lanterns, isrcs=("ZZSHJ2000003",)),
        "swapped": replace(lanterns, isrcs=("ZZSHJ2999999",)),
        "legacy": lanterns,
    }[retag]
    library = Library([KNOWN if known else FITS], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            written = await deferred(rig, KNOWN if known else FITS, release)
            if retag == "legacy":  # saved before plans kept their ISRCs
                row = await rig._row(FITS.id)
                plan = json.loads(row["plan"])
                del plan["evidence"]
                await rig.store.execute(
                    "UPDATE album_matches SET plan = ? WHERE album_id = ?",
                    [json.dumps(plan), FITS.id],
                )
            library.owned[FITS.id] = replace(
                FITS, year=2021 if retag == "legacy" else FITS.year, songs=(after, FITS.songs[1])
            )
            if retag != "moved":  # (its plan's track has no ISRC to differ from)
                assert await rig._shown(FITS.id) is None
            with pytest.raises(ChoiceRefused) as caught:
                await rig.first_use(release, "star")
            assert caught.value.category == "owned_plan" and written == []

    anyio.run(run)


@pytest.mark.parametrize("retag", ["kept", "more", "removed"])
def test_the_same_recording_keeps_its_plan(tmp_path: Path, retag: str) -> None:
    """A linked song's ISRC kept beside a new year, another ISRC added beside it, or its
    ISRC removed: no known ISRC replaced, the plan stays (shown complete, filled)."""
    songs = {
        "kept": KNOWN.songs,
        "more": (replace(KNOWN.songs[0], isrcs=("ZZSHJ2000002", "ZZSHJ2777777")), KNOWN.songs[1]),
        "removed": (replace(KNOWN.songs[0], isrcs=()), KNOWN.songs[1]),
    }[retag]
    library = Library([KNOWN], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            written = await deferred(rig, KNOWN, RELEASE)
            library.owned[FITS.id] = replace(KNOWN, year=2021, songs=songs)
            assert await rig._shown(FITS.id) is not None
            assert await rig.first_use(RELEASE, "star") == FITS.id
            assert written == [(FITS.id, RELEASE.ref)]

    anyio.run(run)


def test_the_engine_tells_an_approval_refusal_from_a_failed_write() -> None:
    engine: Any = SimpleNamespace(_approval=ContextVar("approval", default=None))
    changed = "the release differs from the saved selection"

    def check(release: CatalogRelease) -> None:
        raise MaterializeError(changed)

    PlaceholderEngine._approve(engine, RELEASE)  # no approval: nothing to refuse
    with (
        PlaceholderEngine.guard_materializations(engine, check),
        pytest.raises(ApprovalRefused) as caught,
    ):
        PlaceholderEngine._approve(engine, RELEASE)
    assert caught.value.reason == changed  # as callers tell it apart


@pytest.mark.parametrize("approval", [True, False])
def test_an_approval_refusal_is_no_failed_fill(tmp_path: Path, approval: bool) -> None:
    """A playlist's own approval refusing the release leaves the album's match as it was:
    still shown complete, no wait before its next first use. A failed write is a failure."""
    library = Library([FITS], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            await deferred(rig, FITS)
            before = dict(await rig._row(FITS.id))
            error = (ApprovalRefused if approval else MaterializeError)("refused")

            async def materialize(*args: Any, **kwargs: Any) -> None:
                raise error

            rig._materialize = materialize  # type: ignore[method-assign]
            with pytest.raises(MaterializeError):
                await rig.first_use(RELEASE, "playlist")
            row = dict(await rig._row(FITS.id))
            if approval:
                assert row == before and await rig._shown(FITS.id) is not None
            else:
                assert row["outcome"] == "failed" and row["attempts"] == 1

    anyio.run(run)


# The same album with an owned recording the release does not have: never a second album
# of one name.
TIDE = CatalogRelease(
    CatalogRef("demo", "tide"),
    "Night Letters",
    "Tide Paper",
    ReleaseKind.ALBUM,
    "2020",
    tracks=tuple(
        CatalogTrack(CatalogRef("demo", f"tide{n}"), title, "Tide Paper", 3000, number=n)
        for n, title in enumerate(("Arrival", "Lanterns", "Crossing"), 1)
    ),
)


def tide(ident: str, songs: Sequence[tuple[str, int]], year: int | None = 2020) -> OwnedAlbum:
    return OwnedAlbum(
        ident,
        "Night Letters",
        "Tide Paper",
        year,
        tuple(OwnedSong(f"{ident}-{n}", name, "Tide Paper", 1, n, 3000) for name, n in songs),
    )


LIVE = tide("live", [("Lanterns (Live)", 2), ("Crossing", 3)])  # one shared recording


async def joined(rig: Fills) -> list[Decision]:
    decisions: list[Decision] = []

    async def materialize(
        album_id: str, owned: OwnedAlbum, decision: Decision, trigger: str | None = None
    ) -> None:
        decisions.append(decision)

    rig._materialize = materialize  # type: ignore[method-assign]
    return decisions


def covered(rig: Fills, mine: int | None, theirs: int | None) -> list[str]:
    asked: list[str] = []

    async def owned_cover(owned: OwnedAlbum) -> int | None:
        asked.append(owned.id)
        return mine

    async def release_cover(release: CatalogRelease) -> int | None:
        return theirs

    rig._owned_cover = owned_cover  # type: ignore[method-assign]
    rig._release_cover = release_cover  # type: ignore[method-assign]
    return asked


def test_the_same_album_takes_the_release_songs_it_lacks(tmp_path: Path) -> None:
    """The owned album has its own "Lanterns (Live)", which the release does not: it is no
    fill (not every owned recording is on the release), but it is the release's album (the
    same name and the shared "Crossing"), so the release's other songs join it - the studio
    "Lanterns" beside the live one, at the release's numbers - never a second album."""
    library = Library([LIVE], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            decisions = await joined(rig)
            asked = covered(rig, None, None)
            assert confirm(LIVE, TIDE) is None
            assert await rig.first_use(TIDE, "star") == LIVE.id
            assert asked == []  # a shared recording: covers are not looked at
            [decision] = decisions
            assert decision.outcome == "filled" and decision.reason == JOINED
            assert decision.shares  # "Lanterns" takes number 2 beside "Lanterns (Live)"
            assert decision.links == {TIDE.tracks[2].ref: "live-3"}
            row = await rig._row(LIVE.id)
            assert row["outcome"] == "filled" and row["release_ref"] == str(TIDE.ref)

    anyio.run(run)


def test_of_several_albums_of_the_name_the_fullest_takes_the_songs(tmp_path: Path) -> None:
    fuller = tide("fuller", [("Lanterns (Live)", 2), ("Crossing", 3), ("Coda", 9)])
    library = Library([LIVE, fuller], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            await joined(rig)
            assert await rig.first_use(TIDE, "star") == fuller.id

    anyio.run(run)


@pytest.mark.parametrize(
    ("year", "mine", "theirs", "joins"),
    [
        (1994, None, None, False),  # an earlier album of the name: another album
        (2020, None, None, True),  # the same year, no cover to tell: one album
        (None, None, None, True),  # a year not known does not separate them
        (2020, 0, 0, True),  # the same cover
        (2020, 0, (1 << 64) - 1, False),  # clearly another cover: another album
    ],
)
def test_sharing_no_song_the_year_and_covers_decide(
    tmp_path: Path, year: int | None, mine: int | None, theirs: int | None, joins: bool
) -> None:
    """A second disc of the album on its own (no song in common) joins it; the band's
    other album of the same name - another year, or clearly another cover - stays an
    album of its own."""
    other = tide("other", [("Earlier Song", 1), ("Earlier Hymn", 2)], year)
    library = Library([other], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            decisions = await joined(rig)
            asked = covered(rig, mine, theirs)
            assert await rig.first_use(TIDE, "star") == (other.id if joins else None)
            assert len(decisions) == int(joins)
            # Asked when choosing the album, and again under its lock before writing.
            assert asked == ([] if year == 1994 else ["other"] * (2 if joins else 1))
            if joins:
                assert decisions[0].links == {}  # every song of the release is added

    anyio.run(run)


@pytest.mark.parametrize("decided", ["kept", "review", "filled"])
def test_the_same_album_settled_otherwise_is_refused_not_doubled(
    tmp_path: Path, decided: str
) -> None:
    """An owner's decision on the album (kept as it is, the review list, a filled match)
    stops the songs from joining it - and the release is not added beside
    it as a second album of that name either: the first use is refused."""
    library = Library([LIVE], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            decisions = await joined(rig)
            if decided == "kept":
                await rig._record(LIVE.id, Decision("kept", "kept as it is"), LIVE)
            elif decided == "filled":
                await rig.store.execute(
                    "INSERT INTO releases (ref, title, artist, folder, album_id,"
                    " owned_album_id, album_tags, data, created_at) VALUES (?,?,?,?,?,?,?,?,0)",
                    [
                        "demo:other",
                        "Night Letters",
                        "Tide Paper",
                        "x",
                        LIVE.id,
                        LIVE.id,
                        "{}",
                        "{}",
                    ],
                )
            else:
                await rig._record(LIVE.id, Decision("review", "decided"), LIVE)
            with pytest.raises(ChoiceRefused) as caught:
                await rig.first_use(TIDE, "star")
            assert caught.value.category in ("owned_decision", "owned_plan")
            assert decisions == []

    anyio.run(run)


def test_a_clean_or_explicit_edition_the_album_is_not_stays_apart(tmp_path: Path) -> None:
    clean = replace(LIVE, clean=True)  # the owned files say they are the clean edition
    library = Library([clean], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            await joined(rig)
            assert await rig.first_use(replace(TIDE, explicit=True), "star") is None

    anyio.run(run)


def test_the_compared_release_is_named_as_navidrome_names_its_album() -> None:
    assert release_facts(TIDE).name == "Night Letters"
    assert release_facts(replace(TIDE, clean=True)).name == "Night Letters (Clean)"
    assert joined_links(LIVE, TIDE) == {TIDE.tracks[2].ref: "live-3"}
    assert album_facts(LIVE).year == 2020


def test_songs_the_cleanup_took_out_of_the_same_album_come_back(tmp_path: Path) -> None:
    """The cleanup took the joined songs out as unused, leaving the album deferred with
    their plan: their next use adds them again, from that plan, at the release's numbers."""
    library = Library([LIVE], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            decisions = await joined(rig)
            left = Decision("deferred", "taken out", TIDE, {TIDE.tracks[2].ref: "live-3"})
            left.release_ref = str(TIDE.ref)
            await rig._record(LIVE.id, left, LIVE)
            assert await rig.first_use(TIDE, "star") == LIVE.id
            [decision] = decisions
            assert decision.outcome == "filled" and decision.shares
            assert decision.links == {TIDE.tracks[2].ref: "live-3"}

    anyio.run(run)


def complete(ident: str, title: str = "Night Letters") -> OwnedAlbum:
    """An owned album whose files' track totals say it is complete: "Arrival" and
    "Lanterns", tracks 1 and 2 of 2."""
    return OwnedAlbum(
        ident,
        title,
        "Tide Paper",
        2020,
        tuple(
            OwnedSong(f"{ident}-{n}", name, "Tide Paper", 1, n, 3000, track_total=2)
            for n, name in enumerate(("Arrival", "Lanterns"), 1)
        ),
    )


@pytest.mark.parametrize("found", ["tags", "match"])
def test_a_complete_album_of_the_name_takes_a_bigger_edition_s_songs(
    tmp_path: Path, found: str
) -> None:
    """The owned "Night Letters" is complete (its files' track totals, or a match that
    found it so); the catalog's "Night Letters" has a third song. Its first use adds that
    song into the owned album - never an album beside it, and no refusal."""
    owned = complete("whole")
    if found == "match":
        owned = replace(owned, songs=tuple(replace(s, track_total=None) for s in owned.songs))
    library = Library([owned], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            decisions = await joined(rig)
            if found == "match":
                await rig._record(owned.id, Decision("complete", "every track owned"), owned)
            assert await rig.first_use(TIDE, "star") == owned.id
            [decision] = decisions
            assert decision.outcome == "filled" and decision.reason == JOINED
            assert decision.links == {
                TIDE.tracks[0].ref: "whole-1",
                TIDE.tracks[1].ref: "whole-2",
            }  # "Crossing" is added
            assert (await rig._row(owned.id))["outcome"] == "filled"

    anyio.run(run)


def test_a_complete_album_is_not_padded_from_an_edition_of_another_name(tmp_path: Path) -> None:
    """A differently named edition ("... (Deluxe Edition)") is another album: a complete
    owned album never takes its bonus songs."""
    owned = complete("whole")
    deluxe = replace(
        TIDE, ref=CatalogRef("demo", "tide-deluxe"), title="Night Letters (Deluxe Edition)"
    )
    library = Library([owned], [])

    async def run() -> None:
        async with fills(tmp_path, library) as rig:
            decisions = await joined(rig)
            with pytest.raises(ChoiceRefused) as caught:
                await rig.first_use(deluxe, "star")
            assert caught.value.category == "owned_complete"
            assert decisions == []

    anyio.run(run)
