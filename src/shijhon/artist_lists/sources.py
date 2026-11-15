"""Where a joined artist text's list of artists comes from, most reliable first.

1. ``link``: the catalog item linked to the song (``track_links``, the release Shijhon
   recorded): the names the catalog lists for the track - or for its album, when the track
   is credited as its album with none of its own -, else the names of the artist items it
   lists, asked of the catalog (not while the catalog is skipped for the pass; its failure
   counts toward that, as its other lookups' do).
2. ``isrc``: the configured catalog's songs of the song's ISRC (``catalog_isrc``), those of
   the catalogs asked for artist lists only (``[catalog] artist_lists_catalogs``, built by
   ``catalog.setup.artist_list_catalogs``: ``<kind>_isrc``), and those of every add-on that
   is a catalog only (``isrc``; an add-on that delivers audio is never asked), those whose
   answers list their artists (a catalog's own
   ``artists`` list; a credit split on "feat." is not one).
3. ``search``: only when none gave a list: the songs those catalogs find by title and
   artist (``catalog_search``, ``<kind>_search``, ``search``) that are the song by
   Shijhon's add-on matching rules (title, artist, length within 3 seconds, version;
   ``delivery/addon.mismatches``), a few of them, each asked for again when the search did
   not list its artists.

A list counts only when it is exactly the song's text (``names.listed``). The link and the
ISRC are both asked: two lists that both count but differ - from two sources, or two songs
of one - are ambiguous, and nothing is written; so is a song that a catalog names as one
artist whose name is the whole text (a band such as "Salt, Ash & Ember"). When a catalog
could not be asked, the song is asked about again later (``Unavailable``), also when an
add-on gave a list: the one that failed might differ. Only the linked catalog item's list
counts whatever an add-on or another catalog that failed would have said, and so does the
configured catalog's own ISRC or search list (an answering source with another list, or the
one-artist veto, still makes it ambiguous); a failure of the configured catalog leaves
a song without a linked list a later question. A list of another catalog (one asked for
artist lists only) waits for every source, like an add-on's.

The catalogs of a song are asked at the same time (``AT_ONCE`` at most, each at its own
pace and limits; the configured catalog and the others are given at the library pass's
pace, ``app.py``), and their lists are taken in their order (the configured catalog, the
others in the setting's order, the add-ons in theirs) whatever answered first. A catalog
or an add-on that failed ``SKIP_AFTER`` times in a row is not asked again for the rest of
a pass (``Skips``; logged once). From then on it counts as absent, neither failed nor
answered: the lists of the sources that did answer count among themselves by the rules
above (a list that differs or the one-artist veto still makes the song ambiguous), so one
catalog that is down does not make every later song of the pass a later question (a
song that gets no list is one, though: the skipped source might have one). A source that
fails for one song and is not skipped (yet) still makes it one. Any error of a
catalog counts as its failure, not only a ``CatalogError``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import anyio

from shijhon.artist_lists.names import listed
from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, release_from_data
from shijhon.delivery.addon import Found, Wanted, mismatches
from shijhon.matching.normalize import core_title, credit_names, fold
from shijhon.store import Store

log = logging.getLogger(__name__)

CANDIDATES = 3  # search results asked for again, at most, in each catalog
ISRCS = 2  # of a song's ISRCs, at most
AT_ONCE = 4  # catalogs asked at the same time for one song
MAIN = "the catalog"  # the configured catalog among the sources asked (``Skips`` too)
_MAIN_SOURCES = frozenset({"link", "catalog_isrc", "catalog_search"})
SKIP_AFTER = 2  # failures in a row after which a source is skipped for the rest of a pass


def other(kind: str) -> str:
    """The name of a catalog asked for artist lists only, by its adapter's kind."""
    return f"the {kind} catalog"


@dataclass
class Skips:
    """The catalogs and add-ons a pass (or a publication batch) asks no more: those that
    failed ``SKIP_AFTER`` times in a row."""

    failures: Counter[str] = field(default_factory=Counter)
    skipped: dict[str, str] = field(default_factory=dict)
    addons: set[str] = field(default_factory=set)  # those of ``skipped`` that are add-ons

    def skips(self, name: str) -> bool:
        return name in self.skipped

    def answered(self, name: str) -> None:
        self.failures[name] = 0

    def failed(self, name: str, reason: str, *, addon: bool = True) -> None:
        self.failures[name] += 1
        if self.failures[name] >= SKIP_AFTER and name not in self.skipped:
            self.skipped[name] = reason
            if addon:
                self.addons.add(name)
            shown = f"add-on {name}" if addon else name
            log.info("artist lists: %s skipped for this pass: %s", shown, reason)


class AddonCatalogList(Protocol):
    async def catalogs(self, *, catalog_only: bool = ...) -> Sequence[tuple[str, Catalog]]: ...


@dataclass(frozen=True)
class Asked:
    """A catalog asked for a song's list."""

    name: str  # ``MAIN``, ``other(kind)``, or the add-on's name (``Skips``, vetoes)
    catalog: Catalog
    prefix: str = ""  # its lists' source: "catalog_", "<kind>_", or "" for an add-on

    @property
    def addon(self) -> bool:
        return not self.prefix


@dataclass(frozen=True)
class Subject:
    """A song whose file names several artists in one text."""

    song_id: str
    artist: str  # the joined text
    title: str = ""
    duration_ms: int = 0
    isrcs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    state: str  # "resolved", "unresolved" or "ambiguous"
    names: tuple[str, ...] = ()  # the file's spelling of each artist
    source: str = ""
    vetoed_by: tuple[str, ...] = ()  # who named one artist with the whole text


class Unavailable(Exception):
    """A catalog could not be asked, and what the others said does not settle it: asked
    again later. ``answered``: another source did answer (one add-on down, not all)."""

    def __init__(self, reason: str, *, answered: bool = False) -> None:
        super().__init__(reason)
        self.answered = answered


def _named(names: Sequence[str]) -> tuple[str, ...] | None:
    """A catalog's own list of names (none left unnamed): several, or the one artist of
    the song (which may veto a split)."""
    names = tuple(n for n in names)
    return names if names and all(n.strip() for n in names) else None


def _one_artist(text: str, names: Sequence[str]) -> bool:
    """Whether a catalog names the song's one artist with the whole text."""
    return len(names) == 1 and fold(names[0]) == fold(text) and bool(fold(text))


class Sources:
    def __init__(
        self,
        store: Store,
        catalog: Catalog | None,
        addons: AddonCatalogList | None,
        others: Sequence[tuple[str, Catalog]] = (),
    ) -> None:
        """``others``: the catalogs asked for artist lists only, by their adapters' kinds
        (``[catalog] artist_lists_catalogs``), in the setting's order."""
        self.store = store
        self.catalog = catalog
        self.addons = addons
        self.others = [Asked(other(kind), built, f"{kind}_") for kind, built in others]

    async def resolve(
        self, subject: Subject, *, link: bool = True, skips: Skips | None = None
    ) -> Resolution:
        """The song's list (module docstring). ``link``: also the linked catalog item;
        ``skips``: the pass's catalogs and add-ons asked no more (one of its own when not
        given)."""
        skips = skips if skips is not None else Skips()
        counted: dict[tuple[str, ...], str] = {}
        main_failed = other_failed = answered = False
        vetoes: list[str] = []
        main_item = Asked(MAIN, self.catalog) if self.catalog is not None else None

        def count(kind: str, lists: Sequence[tuple[Asked | None, tuple[str, ...]]]) -> None:
            for who, names in lists:
                name = who.name if who is not None else MAIN
                if _one_artist(subject.artist, names):
                    if name not in vetoes:
                        vetoes.append(name)
                    continue
                spelled = listed(subject.artist, names)
                if spelled is not None:
                    prefix = who.prefix if who is not None else ""
                    counted.setdefault(tuple(spelled), prefix + kind)

        def heard(failed: Sequence[str], answer: bool) -> None:
            nonlocal main_failed, other_failed, answered
            main_failed = main_failed or MAIN in failed
            other_failed = other_failed or any(name != MAIN for name in failed)
            answered = answered or answer

        if link:
            try:
                found = await self.linked(subject, skips)
                count("link", [(main_item, names) for names in found])
                answered = bool(found)  # (a song without a linked item: no answer)
            except CatalogError as exc:
                log.info("artist lists: the catalog was not asked now: %s", exc.reason)
                skips.failed(MAIN, exc.reason, addon=False)
                main_failed = not skips.skips(MAIN)  # (skipped for the pass: absent)
        lists, failed, answer = await self.by_isrc(subject, skips)
        count("isrc", lists)
        heard(failed, answer)
        if not counted and not vetoes:
            lists, failed, answer = await self.searched(subject, skips)
            count("search", lists)
            heard(failed, answer)
        if vetoes:
            return Resolution("ambiguous", source="one_artist", vetoed_by=tuple(vetoes))
        if len(counted) > 1:
            return Resolution("ambiguous", source="lists_differ")
        source = next(iter(counted.values()), "")
        # The linked item's list counts whatever a failed source would have said, and the
        # configured catalog's own whatever a failed add-on or other catalog would have; an
        # add-on's or another catalog's list waits for every source.
        if source != "link" and (main_failed or (other_failed and source not in _MAIN_SOURCES)):
            raise Unavailable("a catalog could not be asked", answered=answered)
        consulted = bool(subject.isrcs) or (bool(subject.title) and subject.duration_ms > 0)
        if not counted and skips.skipped and consulted:
            # (No list among those that answered, and a skipped one might have one.)
            raise Unavailable("a catalog is skipped for this pass", answered=answered)
        if counted:
            names, source = next(iter(counted.items()))
            return Resolution("resolved", names, source)
        return Resolution("unresolved")

    # --- 1. the linked catalog item -----------------------------------------------------

    async def linked(self, subject: Subject, skips: Skips | None = None) -> list[tuple[str, ...]]:
        """The lists of the catalog items linked to the song: those recorded, and those
        asked of the catalog by their artist items - unless it is skipped for the pass."""
        rows = await self.store.fetchall(
            "SELECT l.track_ref, r.data FROM track_links l JOIN releases r"
            " ON r.ref = l.release_ref WHERE l.song_id = ?",
            [subject.song_id],
        )
        found: list[tuple[str, ...]] = []
        for row in rows:
            try:
                release = release_from_data(json.loads(row["data"]))
            except (ValueError, KeyError, TypeError):
                continue
            track = next((t for t in release.tracks if str(t.ref) == row["track_ref"]), None)
            if track is None:
                continue
            names = await self._names_of(track, release, skips)
            if names is not None:
                found.append(names)
        return found

    async def _names_of(
        self, track: CatalogTrack, release: CatalogRelease, skips: Skips | None = None
    ) -> tuple[str, ...] | None:
        """The track's names as the catalog lists them, else its artist items' names;
        a track credited as its album with none of its own: the album's."""
        credits: list[tuple[tuple[str, ...], tuple[CatalogRef, ...]]] = [
            (track.artists, track.artist_refs)
        ]
        if not track.artists and not track.artist_refs and track.artist == release.artist:
            credits.append((release.artists, release.artist_refs))
        for names, refs in credits:
            if names:
                return _named(names)
            if refs:
                return await self._items(refs, skips)
        return None

    async def _items(
        self, refs: Sequence[CatalogRef], skips: Skips | None = None
    ) -> tuple[str, ...] | None:
        if self.catalog is None or any(r.catalog != self.catalog.key for r in refs):
            return None
        if skips is not None and skips.skips(MAIN):
            return None  # (not asked again in this pass)
        names = []
        for ref in refs:
            try:
                names.append((await self.catalog.artist(ref.id)).name)
            except CatalogError as exc:
                if exc.kind == "not_found":
                    return None
                raise
        return _named(names)

    async def _catalogs(self) -> list[Asked]:
        """The configured catalog, the others asked for artist lists, and the catalogs of
        the add-ons that are a catalog only, in that order. An add-on that delivers audio is
        never asked: its search can reach a service that acts on it (a bot, a download)."""
        main = [Asked(MAIN, self.catalog, "catalog_")] if self.catalog is not None else []
        addons = await self.addons.catalogs(catalog_only=True) if self.addons is not None else ()
        return [*main, *self.others, *(Asked(name, built) for name, built in addons)]

    async def _each(
        self,
        skips: Skips,
        ask: Callable[[Catalog], Awaitable[list[tuple[str, ...]]]],
    ) -> tuple[list[tuple[Asked, tuple[str, ...]]], list[str], bool]:
        """``ask`` of each catalog (``_catalogs``) not skipped, ``AT_ONCE`` at a time; the
        lists in that order, each with its catalog. Returns them, the names of those that
        failed and are not skipped for the pass (module docstring), whether one answered."""
        catalogs = await self._catalogs()
        answers: list[list[tuple[str, ...]] | None] = [None] * len(catalogs)
        limit = anyio.CapacityLimiter(AT_ONCE)

        async def one(index: int, asked: Asked) -> None:
            async with limit:
                try:
                    answers[index] = await ask(asked.catalog)
                except CatalogError as exc:
                    log.debug("artist lists: %s not asked now: %s", asked.name, exc.reason)
                    skips.failed(asked.name, exc.reason, addon=asked.addon)
                    return
                except Exception as exc:
                    # (Outside the catalog contract, or a bug reading its answer: that
                    # catalog failed, not the pass.)
                    log.warning("artist lists: %s failed", asked.name, exc_info=True)
                    skips.failed(asked.name, type(exc).__name__, addon=asked.addon)
                    return
            skips.answered(asked.name)

        async with anyio.create_task_group() as group:
            for index, asked in enumerate(catalogs):
                if not skips.skips(asked.name):
                    group.start_soon(one, index, asked)
        found = [
            (asked, names)
            for asked, answer in zip(catalogs, answers, strict=True)
            if answer
            for names in answer
        ]
        failed = [
            a.name
            for a, answer in zip(catalogs, answers, strict=True)
            if answer is None and not skips.skips(a.name)  # (skipped: absent)
        ]
        return found, failed, any(answer is not None for answer in answers)

    # --- 2. the ISRC in the catalogs ----------------------------------------------------

    async def by_isrc(
        self, subject: Subject, skips: Skips | None = None
    ) -> tuple[list[tuple[Asked, tuple[str, ...]]], list[str], bool]:
        """The lists found (with their sources' names), the sources that failed, whether
        one answered."""
        if not subject.isrcs:
            return [], [], False

        async def ask(catalog: Catalog) -> list[tuple[str, ...]]:
            found: list[tuple[str, ...]] = []
            for isrc in subject.isrcs[:ISRCS]:
                tracks = await catalog.songs_by_isrc(isrc)
                found += [names for t in tracks if (names := _named(t.artists))]
            return found

        return await self._each(skips if skips is not None else Skips(), ask)

    # --- 3. a search in the catalogs ----------------------------------------------------

    async def searched(
        self, subject: Subject, skips: Skips | None = None
    ) -> tuple[list[tuple[Asked, tuple[str, ...]]], list[str], bool]:
        """The lists found (with their sources' names), the sources that failed, whether
        one answered."""
        if not subject.title or subject.duration_ms <= 0:
            return [], [], False
        wanted = Wanted(
            isrc=subject.isrcs[0] if subject.isrcs else None,
            title=subject.title,
            artist=subject.artist,
            duration_ms=subject.duration_ms,
        )
        first = (credit_names(subject.artist) or [subject.artist])[0]
        term = f"{core_title(subject.title)} {first}".strip()

        async def ask(catalog: Catalog) -> list[tuple[str, ...]]:
            found: list[tuple[str, ...]] = []
            try:
                results = await catalog.search(term, limit=10)
                same = [t for t in results.songs if not mismatches(_found(t), wanted)]
                for track in same[:CANDIDATES]:
                    names = _named(track.artists)
                    if names is None:
                        names = _named((await catalog.song(track.ref.id)).artists)
                    if names is not None:
                        found.append(names)
            except CatalogError as exc:
                if exc.kind != "not_found":
                    raise
            return found

        return await self._each(skips if skips is not None else Skips(), ask)


def _found(track: CatalogTrack) -> Found:
    return Found(
        track.ref.id,
        track.title,
        track.artist,
        float(track.duration_ms),
        track.isrc,
        None,
        track.album_title,
    )


def song_subject(song: dict[str, Any]) -> Subject:
    """A Navidrome song (native API) as a subject."""
    tags = song.get("tags") if isinstance(song.get("tags"), dict) else {}
    isrcs = tags.get("isrc") if isinstance(tags, dict) else None
    return Subject(
        song_id=str(song.get("id") or ""),
        artist=str(song.get("artist") or ""),
        title=str(song.get("title") or ""),
        duration_ms=int(float(song.get("duration") or 0) * 1000),
        isrcs=tuple(str(i).upper() for i in isrcs if str(i).strip())
        if isinstance(isrcs, list)
        else (),
    )
