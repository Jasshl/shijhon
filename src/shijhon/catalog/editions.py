"""Edition de-duplication: the editions of one album become one card.

- Editions share the normalized title (edition markers, "feat." credits and kind suffixes
  removed), the release type and the artist. Singles and EPs keep their names: "Song -
  Single" and "Song (Remix) - Single" are different releases.
- Identical titles merge only within the same release year, so a self-titled series stays
  separate. A title with an edition marker (remastered, deluxe, anniversary, expanded,
  bonus tracks and similar) joins its album whatever the year: the one of the same year,
  else the latest earlier one. Other suffixes ("(Artist's Version)", "(2019 Mix)") make a
  different title.
- The card shows the standard edition or an edition with a few more tracks: the explicit
  one first, then the fuller one. An edition with more than about 1.5 times the standard
  edition's tracks (a 40-track super deluxe of a 13-track album) keeps a card of its own.

Clean and explicit twins: a card shows the explicit edition, or the clean one, as
the ``twins`` setting says; "both" keeps clean editions on cards of their own. Songs follow
the same setting (``merge_twins``): the same title, artist and length within 2 s are one
song, whatever their flags say - a catalog without flags keeps one of them.

Albums already in the library take part as well (search results and artist pages must not
show them twice): a card with a library album in it is left out. A library album joins its
album whatever its year, as a marked edition does (owned tags and the catalog often
disagree on years). It never sets a card's size, since the library may hold only some of
its tracks: it belongs to the card of the catalog edition with the same title, else to
the standard edition's card. Its release type counts only when known.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from shijhon.catalog.model import CatalogRelease, CatalogTrack, ReleaseKind, Twins
from shijhon.matching.normalize import (
    artist_names,
    close_duration,
    edition_marked,
    fold,
    name_key,
    same_title,
    title_key,
)

LARGER_EDITION = 1.5  # more tracks than this times the standard edition's: a card of its own
TWIN_SECONDS = 2.0  # clean and explicit versions of a song differ at most this much
_NAMED = (ReleaseKind.SINGLE, ReleaseKind.EP)  # kinds that keep their names
_ALBUMS = {ReleaseKind.ALBUM, ReleaseKind.COMPILATION}

Key = tuple[str, ReleaseKind, str]


@dataclass(frozen=True)
class LibraryAlbum:
    """An album in the library, as far as edition de-duplication needs it."""

    title: str
    artist: str
    year: int | None = None
    tracks: int | None = None  # how many of its tracks the library holds (informational)
    kind: ReleaseKind | None = None  # None: unknown (no release-type tag)
    clean: bool = False  # a clean edition (its version says so)


@dataclass(eq=False)
class _Member:
    index: int  # position in the catalog's list (library albums: after all of them)
    year: int | None
    marked: bool
    title: str  # folded full title, to find a library album's own edition
    release: CatalogRelease | None = None  # None: a library album
    tracks: int | None = field(default=None)


def edition_key(release: CatalogRelease) -> Key:
    title = name_key(release.title) if release.kind in _NAMED else title_key(release.title)
    return title, release.kind, fold(release.artist)


def marked(release: CatalogRelease) -> bool:
    """An edition by name (singles and EPs keep their names, so never)."""
    return release.kind not in _NAMED and edition_marked(release.title)


def dedupe_editions(
    releases: Iterable[CatalogRelease],
    library: Iterable[LibraryAlbum] = (),
    *,
    twins: Twins = Twins.EXPLICIT,
) -> list[CatalogRelease]:
    """One release per album, in the order its first edition appeared; none for an album
    the ``library`` already has. ``twins``: which of a clean/explicit pair a card shows,
    or both on cards of their own."""
    releases, library = list(releases), list(library)
    if twins is not Twins.BOTH:
        return _dedupe(releases, library, twins)
    order = {id(r): i for i, r in enumerate(releases)}
    clean = _dedupe([r for r in releases if r.clean], [a for a in library if a.clean], twins)
    other = [r for r in releases if not r.clean]
    kept = clean + _dedupe(other, [a for a in library if not a.clean], Twins.EXPLICIT)
    return sorted(kept, key=lambda r: order[id(r)])


def _dedupe(
    releases: list[CatalogRelease], library: list[LibraryAlbum], twins: Twins
) -> list[CatalogRelease]:
    groups: dict[Key, list[_Member]] = {}
    credits: dict[Key, set[str]] = {}
    by_title: dict[str, list[Key]] = {}
    count = 0
    for index, release in enumerate(releases):
        key = edition_key(release)
        if key not in groups:
            groups[key] = []
            credits[key] = artist_names(release.artist)
            by_title.setdefault(key[0], []).append(key)
        groups[key].append(
            _Member(
                index,
                release.year,
                marked(release),
                fold(release.title),
                release,
                release.track_count or len(release.tracks) or None,
            )
        )
        count = index + 1
    for offset, album in enumerate(library):
        names = artist_names(album.artist)
        titles = {title_key(album.title), name_key(album.title)}
        for key in {k for t in titles for k in by_title.get(t, [])}:
            if _belongs(album, key, names, credits[key]):
                member = _Member(
                    count + offset, album.year, edition_marked(album.title), fold(album.title)
                )
                groups[key].append(member)
    cards: list[tuple[int, CatalogRelease]] = []
    for members in groups.values():
        for same_album in _by_year(members):
            cards += _by_size(same_album, twins)
    return [release for _, release in sorted(cards, key=lambda card: card[0])]


def _belongs(album: LibraryAlbum, key: Key, names: set[str], credit: set[str]) -> bool:
    title, kind, _ = key
    if album.kind is not None and album.kind != kind and {album.kind, kind} - _ALBUMS:
        return False
    named = name_key(album.title) if kind in _NAMED else title_key(album.title)
    return named == title and bool(names & credit)


def _by_year(members: list[_Member]) -> list[list[_Member]]:
    """Same-titled releases per year; marked editions and library albums join the album
    they belong to."""
    albums: dict[int | None, list[_Member]] = {}
    for member in members:
        if not _attaches(member):
            albums.setdefault(member.year, []).append(member)
    if not albums:
        return [members]  # only marked editions (and library albums): one album
    for member in members:
        if _attaches(member):
            albums[_album_year(member.year, list(albums))].append(member)
    return list(albums.values())


def _attaches(member: _Member) -> bool:
    return member.marked or member.release is None


def _album_year(year: int | None, years: list[int | None]) -> int | None:
    if year in years:
        return year
    known = [y for y in years if y is not None]
    if year is None or not known:
        return years[0]
    earlier = [y for y in known if y <= year]
    return max(earlier) if earlier else min(known, key=lambda y: abs(y - year))


def _by_size(members: list[_Member], twins: Twins) -> list[tuple[int, CatalogRelease]]:
    """Cards of one album: the standard edition with the editions that are at most about
    1.5 times its size, then the same for what is left (catalog editions only). A card
    that a library album belongs to is left out."""
    cards: list[list[_Member]] = []
    rest = [m for m in members if m.release is not None]
    while rest:
        standard = [m for m in rest if not m.marked] or rest
        sizes = [m.tracks for m in standard if m.tracks]
        limit = min(sizes) * LARGER_EDITION if sizes else None
        # Never empty: the smallest standard edition always fits.
        card = [m for m in rest if limit is None or m.tracks is None or m.tracks <= limit]
        rest = [m for m in rest if m not in card]
        cards.append(card)
    owned: set[int] = set()
    for album in (m for m in members if m.release is None):
        same = [i for i, card in enumerate(cards) if any(m.title == album.title for m in card)]
        owned.add(same[0] if same else 0)
    chosen = []
    for number, card in enumerate(cards):
        if number in owned:
            continue  # the library has this album
        best = max(card, key=lambda m: (_preferred(m.release, twins), m.tracks or 0))
        assert best.release is not None
        chosen.append((min(m.index for m in card), best.release))
    return chosen


def _preferred(item: CatalogRelease | CatalogTrack | None, twins: Twins) -> int:
    """How much ``twins`` wants this version: 2 its kind, 1 unflagged, 0 the other kind."""
    if item is None:
        return 0
    if twins is Twins.CLEAN:
        return 2 if item.clean else 0 if item.explicit else 1
    return 2 if item.explicit else 0 if item.clean else 1


def merge_twins(tracks: Iterable[CatalogTrack], twins: Twins) -> list[CatalogTrack]:
    """One song per clean/explicit pair as ``twins`` says (they have their own ISRCs): the
    same recording title (``same_title``: "(Clean)" and reissue words dropped), a shared
    artist and a length within 2 s. When the catalog flags versions, only a clean song
    and one that is not clean are twins (other look-alikes stay; ISRCs de-duplicate those);
    without flags such songs are merged and either is kept. Kept in the order the first of
    each appeared; "both" keeps flagged twins apart."""
    tracks = list(tracks)
    flagged = any(t.clean or t.explicit for t in tracks)
    groups: list[list[CatalogTrack]] = []
    for track in tracks:
        for group in groups:
            first = group[0]
            if (
                same_title(first.title, track.title)
                and artist_names(first.artist) & artist_names(track.artist)
                and close_duration(first.duration_ms, track.duration_ms, int(TWIN_SECONDS * 1000))
                and (not flagged or (twins is not Twins.BOTH and first.clean != track.clean))
                and len(group) < (2 if flagged else len(tracks))
            ):
                group.append(track)
                break
        else:
            groups.append([track])
    prefer = Twins.EXPLICIT if twins is Twins.BOTH else twins
    return [max(group, key=lambda t: _preferred(t, prefer)) for group in groups]
