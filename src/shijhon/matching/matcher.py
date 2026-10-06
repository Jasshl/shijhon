"""Matching an owned album to a catalog release.

Candidates: releases the album search finds under the owned album's title (normalized:
case, accents, punctuation, spacing, edition words, "feat." credits; any credited artist
counts), then the releases of the owned files' ISRCs, then - when neither finds the title
- those of a track search (the first owned track). A candidate is plausible only when
**every** owned track is on it: the same ISRC, or the same disc and track position (else
anywhere on the release), a title naming the same recording and a length within 3 s.

An album whose files say it is complete - every file carries a track total, every track
of every disc is owned (discs 1..the disc total when the files carry one; without a disc
total, only files without a disc number: a disc 1 alone may be half of a set) - is
**complete** without asking the catalog: tracks are never added to it.

Only a release with the owned album's title may fill or complete it: when every owned
track is found only under other titles (a compilation, a single, a greatest-hits album),
the album goes to review. If a plausible release has nothing to add, the album is
**complete** - checked before any preference, so a complete owned album never gets bonus
tracks from a larger edition. Otherwise the edition choice narrows by the exact owned
title, the owned year, and the owned files' kind of a clean/explicit pair (unknown: the
``twins`` setting, explicit by default).

Outcomes: ``fill`` (one release, tracks to add), ``complete``, ``review`` (several
plausible editions; found only under other titles; a release whose numbering would put a
new track on an owned track's position; or a larger edition - deluxe, expanded, bonus… -
the owned title does not name while the owned tracks run 1..n without a gap, so the album
may be the complete standard edition), ``none``. Owned files are never changed by
matching.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from shijhon.catalog.base import Catalog, CatalogError, SearchResults
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, Twins
from shijhon.matching.normalize import (
    close_duration,
    fold,
    same_artist,
    same_title,
    title_key,
)

log = logging.getLogger(__name__)

DURATION_MS = 3000  # an owned track and its catalog version differ at most this much
MAX_CANDIDATES = 6  # releases with the album's title fetched in detail
MAX_ELSEWHERE = 3  # releases under other titles fetched, when none with the title fits
SEARCH_LIMIT = 25
# Edition words that say the release is larger than the standard album.
_LARGER = re.compile(
    r"\b(deluxe|expanded|anniversary|bonus|super|special|collector'?s|complete)\b", re.I
)


class Outcome(StrEnum):
    FILL = "fill"
    COMPLETE = "complete"
    REVIEW = "review"
    NONE = "none"


@dataclass(frozen=True)
class OwnedSong:
    id: str
    title: str
    artist: str
    disc: int
    number: int
    duration_ms: int
    isrcs: tuple[str, ...] = ()
    track_total: int | None = None  # the file's track total (of its disc), if tagged
    disc_total: int | None = None
    disc_tagged: bool = False  # the file carries a disc number


@dataclass(frozen=True)
class OwnedAlbum:
    id: str
    title: str
    artist: str
    year: int | None
    songs: tuple[OwnedSong, ...]
    clean: bool | None = None  # None: the owned files do not say


@dataclass
class Match:
    outcome: Outcome
    reason: str
    release: CatalogRelease | None = None
    # Catalog track -> the owned song it is (the fill creates the others).
    links: dict[CatalogRef, str] = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)  # plausible releases (for review)

    @property
    def missing(self) -> int:
        return len(self.release.tracks) - len(self.links) if self.release else 0


class Matcher:
    def __init__(self, catalog: Catalog, *, twins: Twins = Twins.EXPLICIT) -> None:
        self.catalog = catalog
        self.twins = twins  # which of a clean/explicit pair, when the owned files do not say

    async def match(self, owned: OwnedAlbum) -> Match:
        if not owned.songs:
            return Match(Outcome.NONE, "no owned songs")
        if complete_by_tags(owned):
            return Match(Outcome.COMPLETE, "the owned files' track totals say every track is there")
        titled, elsewhere = await self._candidates(owned)
        plausible = await self._plausible(owned, titled[:MAX_CANDIDATES])
        if not plausible:
            others = await self._plausible(owned, elsewhere[:MAX_ELSEWHERE])
            if others:
                names = [str(r.ref) for r, _ in others]
                reason = "the owned tracks are only on releases with other titles"
                return Match(Outcome.REVIEW, reason, candidates=names)
            looked = len(titled) + len(elsewhere)
            return Match(Outcome.NONE, f"no release has every owned track ({looked} looked at)")
        names = [str(r.ref) for r, _ in plausible]
        # (A release whose track list could not be read in full says nothing about what is
        # complete: only the owned files' own totals do, above.)
        complete = [p for p in plausible if not p[0].incomplete and len(p[0].tracks) == len(p[1])]
        if complete:
            release, links = self._choose(owned, complete)[0]
            return Match(Outcome.COMPLETE, "every track is owned", release, links, names)
        chosen = self._choose(owned, plausible)
        if len(chosen) != 1:
            return Match(Outcome.REVIEW, "several plausible editions", candidates=names)
        release, links = chosen[0]
        match = Match(Outcome.FILL, "", release, links, names)
        if release.incomplete:
            # Neither complete nor to be filled from: tracks are missing from what was read.
            match.outcome = Outcome.REVIEW
            match.reason = "the catalog's track list could not be read in full"
        elif clashes(owned, release, links):
            match.outcome = Outcome.REVIEW
            match.reason = "the release numbers its tracks differently from the owned files"
        elif not release.numbered and displaced(owned, release, links):
            # The catalog sent no track numbers (they are its list's order): the tracks a
            # fill adds would get places the owned files' numbering does not have.
            match.outcome = Outcome.REVIEW
            match.reason = (
                "the catalog lists the release's tracks without numbers, and the owned"
                " files are numbered differently from its order"
            )
        elif _LARGER.search(release.title) and not _LARGER.search(owned.title) and _gapless(owned):
            match.outcome = Outcome.REVIEW
            match.reason = "only a larger edition matches; the owned album may be complete"
        return match

    async def _plausible(
        self, owned: OwnedAlbum, refs: list[CatalogRef]
    ) -> list[tuple[CatalogRelease, dict[CatalogRef, str]]]:
        found = []
        for ref in refs:
            try:
                release = await self.catalog.album(ref.id)
            except CatalogError as exc:
                if exc.kind == "not_found":
                    continue
                raise
            links = confirm(owned, release)
            if links is not None:
                found.append((release, links))
        return found

    async def _candidates(self, owned: OwnedAlbum) -> tuple[list[CatalogRef], list[CatalogRef]]:
        """(Releases with the owned album's title, releases under other titles), each in
        the order found: by album, by ISRC, by track."""
        wanted = title_key(owned.title)
        titled: list[CatalogRef] = []
        elsewhere: list[CatalogRef] = []

        def add(ref: CatalogRef | None, title: str, artist: str) -> None:
            if ref is None or ref in titled:
                return
            if title_key(title) == wanted and same_artist(artist, owned.artist):
                if ref in elsewhere:
                    elsewhere.remove(ref)
                titled.append(ref)
            elif ref not in elsewhere:
                elsewhere.append(ref)

        found = await self._search(f"{owned.artist} {owned.title}")
        for release in found.albums:
            add(release.ref, release.title, release.artist)
        for isrc in [i for s in owned.songs for i in s.isrcs[:1]][:3]:
            try:
                for track in await self.catalog.songs_by_isrc(isrc):
                    add(track.album, track.album_title or "", track.artist)
            except CatalogError as exc:
                if exc.kind != "not_found":
                    raise
        if not titled:
            first = min(owned.songs, key=lambda s: (s.disc, s.number))
            found = await self._search(f"{owned.artist} {first.title}")
            for track in found.songs:
                if same_title(track.title, first.title) and same_artist(track.artist, first.artist):
                    add(track.album, track.album_title or "", track.artist)
        return titled, elsewhere

    async def _search(self, term: str) -> SearchResults:
        try:
            return await self.catalog.search(term, SEARCH_LIMIT)
        except CatalogError as exc:
            if exc.kind == "not_found":
                return SearchResults()
            raise

    def _choose(
        self,
        owned: OwnedAlbum,
        plausible: list[tuple[CatalogRelease, dict[CatalogRef, str]]],
    ) -> list[tuple[CatalogRelease, dict[CatalogRef, str]]]:
        """The edition choice: narrow down while a preference applies."""
        wants_clean = owned.clean if owned.clean is not None else self.twins is Twins.CLEAN

        def exact_title(release: CatalogRelease) -> bool:
            return fold(release.title) == fold(owned.title)

        def same_year(release: CatalogRelease) -> bool:
            return owned.year is not None and release.year == owned.year

        def same_kind(release: CatalogRelease) -> bool:
            return release.clean if wants_clean else not release.clean

        def explicit(release: CatalogRelease) -> bool:
            return release.explicit or wants_clean

        rules: list[Callable[[CatalogRelease], bool]] = [
            exact_title,
            same_year,
            same_kind,
            explicit,
        ]
        chosen = plausible
        for rule in rules:
            preferred = [p for p in chosen if rule(p[0])]
            if preferred:
                chosen = preferred
        return chosen


def complete_by_tags(owned: OwnedAlbum) -> bool:
    """The owned files say the album is complete: each carries a track total, the files
    of a disc agree on it, and every track of every disc is owned - discs 1..the disc total
    when the files carry one; without a disc total, only files without a disc number (a
    disc 1 of a set, ripped alone, is matched normally so its disc 2 is found)."""
    if not owned.songs or any(s.track_total is None for s in owned.songs):
        return False
    numbers: dict[int, set[int]] = {}
    totals: dict[int, set[int]] = {}
    for song in owned.songs:
        numbers.setdefault(song.disc, set()).add(song.number)
        totals.setdefault(song.disc, set()).add(song.track_total or 0)
    disc_totals = {s.disc_total for s in owned.songs if s.disc_total}
    if len(disc_totals) > 1:
        return False
    if not disc_totals and any(s.disc_tagged for s in owned.songs):
        return False
    discs = disc_totals.pop() if disc_totals else 1
    if sorted(numbers) != list(range(1, discs + 1)):
        return False
    for disc, owned_numbers in numbers.items():
        if len(totals[disc]) != 1:  # the files of one disc disagree
            return False
        if owned_numbers != set(range(1, min(totals[disc]) + 1)):
            return False
    return True


def confirm(owned: OwnedAlbum, release: CatalogRelease) -> dict[CatalogRef, str] | None:
    """Catalog track -> owned song for every owned song, or None if one is not on the
    release: the same ISRC, else the same position, else anywhere - with a title naming the
    same recording and a length within 3 s."""
    links: dict[CatalogRef, str] = {}
    free = list(release.tracks)
    for song in sorted(owned.songs, key=lambda s: (s.disc, s.number)):
        found = _find(song, free)
        if found is None:
            return None
        links[found.ref] = song.id
        free.remove(found)
    return links


def _find(song: OwnedSong, tracks: list[CatalogTrack]) -> CatalogTrack | None:
    for track in tracks:
        if track.isrc and track.isrc in song.isrcs:
            return track

    def same(track: CatalogTrack) -> bool:
        return same_title(track.title, song.title) and close_duration(
            track.duration_ms, song.duration_ms, DURATION_MS
        )

    at = [t for t in tracks if (t.disc, t.number) == (song.disc, song.number)]
    return next((t for t in at if same(t)), None) or next((t for t in tracks if same(t)), None)


def clashes(owned: OwnedAlbum, release: CatalogRelease, links: dict[CatalogRef, str]) -> bool:
    """A track the fill would add sits on an owned track's disc and number."""
    taken = {(s.disc, s.number) for s in owned.songs}
    return any((t.disc, t.number) in taken for t in release.tracks if t.ref not in links)


def displaced(owned: OwnedAlbum, release: CatalogRelease, links: dict[CatalogRef, str]) -> bool:
    """An owned song is not at the disc and number of the catalog track it is."""
    at = {song.id: (song.disc, song.number) for song in owned.songs}
    return any(
        at.get(links[track.ref]) != (track.disc, track.number)
        for track in release.tracks
        if track.ref in links
    )


def _gapless(owned: OwnedAlbum) -> bool:
    """The owned tracks run 1..n on each disc (and the discs 1..m): perhaps a whole album."""
    discs: dict[int, list[int]] = {}
    for song in owned.songs:
        discs.setdefault(song.disc, []).append(song.number)
    if sorted(discs) != list(range(1, len(discs) + 1)):
        return False
    return all(sorted(n) == list(range(1, len(n) + 1)) for n in discs.values())
