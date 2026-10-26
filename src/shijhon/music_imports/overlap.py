"""What a reviewed import does with music the library already has.

Importing never shows a song or an album twice in apps. Each received file gets an
outcome:

- ``linked``: the same recording is already in the library with its own audio - an
  original, any account's import, also the importer's own - on the same album
  (``matching/albums.py``). Nothing is published; the import links to that song and it
  counts as the importer's own import. Nothing in the library is overwritten.
- ``catalog_song``: the same recording is a catalog song Shijhon added (a placeholder,
  silent or with temporary downloaded audio). For now it is linked like ``linked``; the
  received file stays retained so that a later stage can put its audio into that song.
- ``new``: published as it is - a song of a new album, another edition (another album
  name, such as "(Deluxe Edition)"), an album of the same name that is another album
  (a different year and no song in common), or a song the library's album lacks whose
  tags put it into that album anyway.
- ``copy``: a song the library's same-named album lacks, whose tags would make Navidrome
  show a second album of that name (another release date, MusicBrainz album ID, version
  tag or spelling). A copy carrying that album's album-level tags is published instead,
  so the song joins the album; the received file stays unchanged and retained. With
  several same-named albums in the library, the one with the most songs is used.
- ``held``: such a song whose copy cannot carry the album's tags. Not published, never
  as an album of its own (it would show as a second album of that name); the review and
  the report say why.
- ``duplicate``: the same song twice in this selection; the better copy
  (``shijhon.quality``) goes on.
- ``unsure``: close to a song of the album but not clearly the same: published like a
  song the album lacks (as a copy carrying the album's tags when its own would make a
  second album of that name; ``held`` when it cannot be one), and the review says it may
  already be in the library.

A person who says a match is wrong ("Not the same song", ``distinct``) gets the file
published like any song the album lacks: never linked, but into the album of its name
(held when it cannot be). A file without an album name (an untagged file, Shorten's,
which has no tags) never joins an album.

Incoming albums come together by the same rule as an incoming and a library album: the
same name and a shared song, or no other year and no clearly different cover.

**Each person's minimum quality** (``shijhon.quality.Minimum``) is decided per album, so
an album never ends up half the person's files and half catalog songs. When a file of an
album that would be published (``new``, ``copy``, ``unsure``) or put into a catalog song
(``catalog_song``) is below it, the album's catalog version is used instead:

- ``low_quality``: the catalog has the album (every such song is on one release): the
  file is not published; at publication that release is added to the library (into the
  library's same-named album when there is one) and the file is linked to its catalog
  song, which plays from the add-ons and counts as the person's import. Without an
  answer from the catalog (``CatalogUnreachable``) the file waits in the review (route
  ``unchecked``): the person checks again or adds the files anyway; approved like that,
  it is not added.
- a ``catalog_song`` of such an album stays linked to that song without its audio
  (``audio`` false): the song keeps playing from the add-ons.
- the catalog does not have the album: its files are published as they would have been,
  marked ``below_minimum`` in the review and report.

Files already in the library stay ``linked`` whatever their quality; a quality that is
not known never counts as below. "Add my files anyway" (``keep_files`` on any file of the
album) publishes the album's files as they would have been, marked.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any

import anyio
import anyio.lowlevel

from shijhon.matching.albums import Album, Keys, Recordings, Song, keys, same_keys, same_name
from shijhon.matching.grouping import album_artist, album_key, identity, same_native_album
from shijhon.matching.normalize import fold
from shijhon.music_imports.facts import year as tag_year
from shijhon.quality import LOSSLESS_FORMATS, Minimum, Quality

NEW, COPY, LINKED, CATALOG_SONG = "new", "copy", "linked", "catalog_song"
HELD, DUPLICATE, UNSURE = "held", "duplicate", "unsure"
LOW_QUALITY = "low_quality"  # below the person's minimum: the catalog's version instead
PUBLISHED = frozenset({NEW, COPY, UNSURE})  # outcomes whose song is published
LINKS = frozenset({LINKED, CATALOG_SONG})  # outcomes linked to a library song
# The outcomes the quality gate looks at: a file that would be published or would give
# its audio to a catalog song.
GATED = frozenset({NEW, COPY, UNSURE, CATALOG_SONG})
CATALOG, UNCHECKED, FILES = "catalog", "unchecked", "files"  # a low-quality album's route
# The reason of a file that is exactly the bytes an earlier import published (``linked``).
SAME_BYTES = "Exactly this file is already in the library."
STEP = 200  # songs compared between two turns of the event loop

Tags = Mapping[str, Sequence[str]]


@dataclass(frozen=True)
class Incoming:
    id: str
    facts: Mapping[str, Any]
    digest: str = ""
    distinct: bool = False

    @property
    def album(self) -> str:
        return str(self.facts.get("album") or "")

    @property
    def artist(self) -> str:
        return str(self.facts.get("artist") or "")

    @cached_property
    def album_artist(self) -> str:
        """As Navidrome has it (``matching/grouping.py``): the album artist tag, else
        "Various Artists" for a compilation, else the track artist."""
        return album_artist(self.tags, self.artist)

    @cached_property
    def tags(self) -> Tags:
        """Its album-level tags as Navidrome reads them; for formats Shijhon does not read
        that way, just its album and album artist."""
        tags = self.facts.get("album_tags")
        if isinstance(tags, Mapping) and tags:
            return tags
        plain = {"album": [self.album], "albumartist": [str(self.facts.get("albumartist") or "")]}
        return {k: v for k, v in plain.items() if v[0]}

    @cached_property
    def year(self) -> int | None:
        return tag_year({k: list(v) for k, v in self.tags.items()})

    @property
    def quality(self) -> Quality:
        return quality_of(self.facts.get("quality"))

    @property
    def keep_files(self) -> bool:
        """The person chose "Add my files anyway" for this file's album."""
        return self.facts.get("keep_files") is True

    @property
    def taggable(self) -> bool:
        """Whether a copy of it can carry another album's tags."""
        return bool(self.facts.get("taggable"))

    @cached_property
    def song(self) -> Song:
        isrcs = self.facts.get("isrcs") or []
        return Song(
            str(self.facts.get("title") or ""),
            artist=self.artist,
            duration_ms=int(self.facts.get("duration_ms") or 0),
            disc=_int(self.facts.get("disc")),
            number=_int(self.facts.get("track")),
            isrcs=tuple(str(i) for i in isrcs if i),
            recording_id=str(self.facts.get("recording_id") or ""),
            clean=self.facts.get("clean") if isinstance(self.facts.get("clean"), bool) else None,
        )

    @cached_property
    def keys(self) -> Keys:
        return keys(self.song)


@dataclass(frozen=True)
class LibrarySong:
    song_id: str
    track_key: str
    album_id: str
    title: str
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: int | None = None
    disc: int | None = None
    number: int | None = None
    duration_ms: int = 0
    isrcs: tuple[str, ...] = ()
    recording_id: str = ""
    clean: bool | None = None  # True: a clean edit; False: explicit; None: not known
    quality: Quality = field(default_factory=Quality)
    placeholder: bool = False
    cover: str = ""  # a cover ID of the song's own picture, "" when it has none
    album_mbid: str = ""

    @cached_property
    def song(self) -> Song:
        return Song(
            self.title,
            artist=self.artist,
            duration_ms=self.duration_ms,
            disc=self.disc,
            number=self.number,
            isrcs=self.isrcs,
            recording_id=self.recording_id,
            clean=self.clean,
        )

    @cached_property
    def keys(self) -> Keys:
        return keys(self.song)

    @classmethod
    def of_index(
        cls, row: Mapping[str, Any], metadata: Mapping[str, Any], *, placeholder: bool
    ) -> LibrarySong:
        """From a ``collection_tracks`` row and its structural metadata: Navidrome's
        native song fields (``collection/index.py``)."""
        tags = metadata.get("tags")
        isrc = tags.get("isrc") if isinstance(tags, Mapping) else None
        isrcs = [i for i in isrc if isinstance(i, str)] if isinstance(isrc, list) else []
        year = _int(metadata.get("year") or row.get("year"))
        suffix = str(metadata.get("suffix") or "").lower()
        song_id = str(row["song_id"])
        return cls(
            song_id=song_id,
            track_key=str(row["track_key"]),
            album_id=str(row["album_id"]),
            title=str(metadata.get("title") or row.get("title") or ""),
            artist=str(metadata.get("artist") or row.get("artist") or ""),
            album=str(metadata.get("album") or row.get("album") or ""),
            # Navidrome's own album artist: "Various Artists" for a compilation without one.
            album_artist=str(metadata.get("albumArtist") or metadata.get("artist") or ""),
            year=year,
            disc=_int(metadata.get("discNumber")),
            number=_int(metadata.get("trackNumber")),
            duration_ms=int(float(metadata.get("duration") or row.get("duration") or 0) * 1000),
            isrcs=tuple(isrcs),
            recording_id=str(metadata.get("mbzRecordingID") or ""),
            clean={"c": True, "e": False}.get(str(metadata.get("explicitStatus") or "")),
            quality=Quality(
                format=suffix,
                lossless=suffix in LOSSLESS_FORMATS,
                kbps=_int(metadata.get("bitRate")) or 0,
                sample_rate=_int(metadata.get("sampleRate")) or 0,
                bit_depth=_int(metadata.get("bitDepth")) or 0,
            ),
            placeholder=placeholder,
            cover="mf-" + song_id if metadata.get("hasCoverArt") is True else "",
            album_mbid=str(metadata.get("mbzAlbumId") or ""),
        )


@dataclass(frozen=True)
class LibraryAlbum:
    album_id: str
    songs: tuple[LibrarySong, ...]

    @property
    def name(self) -> str:
        return self.songs[0].album

    @property
    def artist(self) -> str:
        return self.songs[0].album_artist

    @cached_property
    def year(self) -> int | None:
        years = [s.year for s in self.songs if s.year]
        return min(years) if years else None

    @property
    def cover(self) -> str:
        return next((s.cover for s in self.songs if s.cover), "")

    @cached_property
    def index(self) -> Recordings:
        return Recordings([s.keys for s in self.songs])

    def album(self) -> Album:
        return Album(self.name, self.artist, self.year)


@dataclass(frozen=True)
class Decision:
    outcome: str
    song: LibrarySong | None = None  # the library song linked to
    album: LibraryAlbum | None = None  # the library album a new song or copy joins
    tags_from: str = ""  # "album:<id>" or "item:<id>": whose album-level tags a copy takes
    # Navidrome's album identity a copy is to have (``matching/grouping.album_key``).
    target_key: tuple[str, ...] = ()
    album_item: str = ""  # an incoming item of the album a song joins (no library album)
    of: str = ""  # duplicate: the better item in this selection
    reason: str = ""
    # The quality gate's evidence: ``below_minimum`` (the minimum, described), ``route``,
    # the catalog ``release`` (its ``fingerprint``) and ``track``, ``audio``, ``kept``.
    gate: Mapping[str, Any] = field(default_factory=dict)

    def evidence(self, incoming: Incoming) -> dict[str, Any]:
        """For the review, the report and later stages (``music_import_items.match``)."""
        found: dict[str, Any] = {"reason": self.reason, "quality": incoming.quality.describe()}
        if self.song is not None:
            found.update(
                song_id=self.song.song_id,
                track_key=self.song.track_key,
                album_id=self.song.album_id,
                title=self.song.title,
                album=self.song.album,
                artist=self.song.artist,
                library_quality=self.song.quality.describe() if self.song.quality.known() else "",
            )
        if self.album is not None:
            found.update(album_id=self.album.album_id, album=self.album.name)
        if self.tags_from:
            found["tags_from"] = self.tags_from
        if self.target_key:
            found["target_key"] = list(self.target_key)
        if self.album_item:
            found["album_item"] = self.album_item
        if self.of:
            found["of"] = self.of
        found.update(self.gate)
        return found


@dataclass(frozen=True)
class CatalogFound:
    """The catalog release that has every song of a low-quality album."""

    release: str  # the release's catalog ID ("catalog:id")
    fingerprint: str  # what was reviewed: publication adds only exactly this release
    tracks: Mapping[str, str]  # incoming item ID -> its catalog track ("catalog:id")


class CatalogUnreachable(Exception):
    """The catalog could not answer: whether it has the album is not known."""


class CatalogRefused(Exception):
    """A reviewed catalog version cannot be added: a refusal a retry does not change (the
    release would join an album that cannot take it, the catalog no longer has it or
    changed it)."""


# Whether the covers of these incoming files and of another album (one of the library's,
# or other incoming files) clearly differ: True, False, or None when either has none.
CoversDiffer = Callable[
    [Sequence[Incoming], "LibraryAlbum | Sequence[Incoming]"], Awaitable[bool | None]
]
AlbumTags = Callable[[LibraryAlbum], Awaitable[Tags | None]]
# The catalog release with every song of these files' album, or None when it has none;
# raises ``CatalogUnreachable`` when the catalog does not answer.
CatalogLookup = Callable[[Sequence[Incoming]], Awaitable[CatalogFound | None]]


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def quality_of(data: Any) -> Quality:
    if not isinstance(data, Mapping):
        return Quality()
    return Quality(
        format=str(data.get("format") or ""),
        lossless=bool(data.get("lossless")),
        kbps=int(data.get("kbps") or 0),
        sample_rate=int(data.get("sample_rate") or 0),
        bit_depth=int(data.get("bit_depth") or 0),
    )


def _album_of(items: Sequence[Incoming]) -> Album:
    first = items[0]
    years = [i.year for i in items if i.year]
    return Album(first.album, first.album_artist, min(years) if years else None)


def _positions_agree(a: Song, b: Song) -> bool:
    if a.number is None or b.number is None:
        return True
    return a.number == b.number and (a.disc or 1) == (b.disc or 1)


def _best_library(candidates: Sequence[LibrarySong]) -> LibrarySong:
    return sorted(
        candidates, key=lambda s: (s.placeholder, tuple(-n for n in s.quality.rank()), s.song_id)
    )[0]


def _match(item: Incoming, album: LibraryAlbum) -> tuple[LibrarySong | None, bool]:
    """The library song ``item`` is, and whether there was any candidate at all. Several
    candidates that are one recording (the library already has copies) are one match;
    otherwise the disc and track position decides; else nothing (unsure)."""
    song = item.song
    candidates = [album.songs[i] for i in album.index.matching(item.keys)]
    if not candidates:
        return None, False
    first = candidates[0]
    if all(same_keys(first.keys, other.keys) for other in candidates[1:]):
        placed = [s for s in candidates if _positions_agree(song, s.song)]
        return _best_library(placed or candidates), True
    placed = [
        s
        for s in candidates
        if song.number is not None and s.number == song.number and (s.disc or 1) == (song.disc or 1)
    ]
    if placed and all(same_keys(placed[0].keys, o.keys) for o in placed[1:]):
        return _best_library(placed), True
    return None, True


def _groups(items: Iterable[Incoming]) -> list[list[Incoming]]:
    """Incoming files by the album Navidrome would put them in; a file without an album
    name stands alone."""
    groups: dict[tuple[str, ...], list[Incoming]] = {}
    for item in items:
        key = album_key(item.tags, item.artist) if fold(item.album) else ("", item.id)
        groups.setdefault(key, []).append(item)
    return sorted(groups.values(), key=lambda g: (-len(g), min(i.id for i in g)))


@dataclass
class _Prepared:
    groups: list[list[Incoming]]
    albums: dict[str, list[LibraryAlbum]]  # folded album name -> the library's albums


def _prepare(incoming: Sequence[Incoming], library: Sequence[LibrarySong]) -> _Prepared:
    """The comparisons' costly groundwork, done once and off the event loop: every song's
    recording keys, the incoming albums, the library's albums by name and their indexes."""
    for item in incoming:
        _ = item.keys, item.album_artist, item.year
    by_album: dict[str, list[LibrarySong]] = {}
    for song in library:
        _ = song.keys
        by_album.setdefault(song.album_id, []).append(song)
    albums: dict[str, list[LibraryAlbum]] = {}
    for album_id, songs in sorted(by_album.items()):
        album = LibraryAlbum(album_id, tuple(songs))
        _ = album.index, album.year
        albums.setdefault(fold(album.name), []).append(album)
    return _Prepared(_groups(incoming), albums)


def _shares(items: Sequence[Incoming], index: Recordings) -> bool:
    return any(index.matching(item.keys) for item in items)


async def _clusters(
    groups: list[list[Incoming]], covers_differ: CoversDiffer | None
) -> list[list[list[Incoming]]]:
    """Incoming albums that are one album come together, by the rule between imports and
    library albums (``matching/albums.py``): the same name and a shared song, or no year
    that differs and no clearly different cover. The first group of each cluster is its
    biggest."""
    clusters: list[list[list[Incoming]]] = []
    by_name: dict[str, list[tuple[list[list[Incoming]], Album, Recordings]]] = {}
    for n, group in enumerate(groups):
        if n % STEP == STEP - 1:
            await anyio.lowlevel.checkpoint()
        album = _album_of(group)
        name = fold(album.name)
        joined = False
        for cluster, primary, index in by_name.get(name, []) if name else []:
            if not same_name(album, primary):
                continue
            if not _shares(group, index):
                if album.year and primary.year and album.year != primary.year:
                    continue
                differ = await covers_differ(group, cluster[0]) if covers_differ else None
                if differ is True:
                    continue
            cluster.append(group)
            joined = True
            break
        if not joined:
            cluster = [group]
            clusters.append(cluster)
            if name:
                by_name.setdefault(name, []).append(
                    (cluster, album, Recordings([i.keys for i in group]))
                )
    return clusters


def _rank(item: Incoming) -> tuple[Any, ...]:
    return (tuple(-n for n in item.quality.rank()), item.id)


async def classify(
    incoming: Sequence[Incoming],
    library: Sequence[LibrarySong],
    *,
    imported: Mapping[str, LibrarySong] | None = None,
    covers_differ: CoversDiffer | None = None,
    album_tags: AlbumTags | None = None,
    minimum: Minimum | None = None,
    catalog: CatalogLookup | None = None,
) -> dict[str, Decision]:
    """``incoming`` item ID -> its decision. ``library``: present songs of albums named
    like the incoming ones (more do no harm). ``imported``: content digest -> the present
    song of an earlier import with exactly those bytes. ``minimum``: the importing
    person's minimum quality (None: no gate); ``catalog``: where a low-quality album's
    catalog version is looked for (None: no catalog).

    The work grows with the number of songs, not with its square (``Recordings``); its
    groundwork runs in a worker thread, and the rest gives the event loop a turn every
    ``STEP`` songs."""
    imported = imported or {}
    prepared = await anyio.to_thread.run_sync(_prepare, list(incoming), list(library))
    decisions: dict[str, Decision] = {}
    tags_cache: dict[str, Tags | None] = {}
    compared = 0

    async def tags_of(album: LibraryAlbum) -> Tags | None:
        if album.album_id not in tags_cache:
            tags_cache[album.album_id] = await album_tags(album) if album_tags else None
        return tags_cache[album.album_id]

    async def step(count: int) -> None:
        nonlocal compared
        compared += count
        if compared >= STEP:
            compared = 0
            await anyio.lowlevel.checkpoint()

    for cluster in await _clusters(prepared.groups, covers_differ):
        items = [i for group in cluster for i in group]
        named = bool(fold(items[0].album))
        kept: list[Incoming] = []
        kept_index = Recordings()
        for item in sorted((i for i in items if not i.distinct), key=_rank):
            await step(1)
            better = next(
                (
                    kept[j]
                    for j in kept_index.matching(item.keys)
                    if _positions_agree(item.song, kept[j].song)
                ),
                None,
            )
            if better is not None:
                decisions[item.id] = Decision(
                    DUPLICATE,
                    of=better.id,
                    reason="The same song is in this selection in the same or better quality.",
                )
            else:
                kept.append(item)
                kept_index.add(item.keys)
        for item in list(kept):
            exact = imported.get(item.digest) if item.digest else None
            if exact is not None:
                decisions[item.id] = Decision(LINKED, song=exact, reason=SAME_BYTES)
                kept.remove(item)
        distinct = [i for i in items if i.distinct]
        if not named:
            for item in kept + distinct:
                decisions[item.id] = Decision(NEW, reason=_DISTINCT if item.distinct else "")
            if minimum is not None:
                await _gate(items, decisions, minimum, catalog)
            continue
        mine = _album_of(items)
        target: LibraryAlbum | None = None
        matches: dict[str, tuple[LibrarySong | None, bool]] = {}
        best: tuple[int, int] = (-1, -1)
        for album in prepared.albums.get(fold(mine.name), []):
            if not same_name(mine, album.album()):
                continue
            found: dict[str, tuple[LibrarySong | None, bool]] = {}
            for item in items:
                await step(1)
                found[item.id] = _match(item, album)
            shared = sum(1 for song, _ in found.values() if song is not None)
            if not shared:
                if mine.year and album.year and mine.year != album.year:
                    continue
                if covers_differ is not None and await covers_differ(items, album) is True:
                    continue
            if (len(album.songs), shared) > best:
                target, best, matches = album, (len(album.songs), shared), found
        primary = {i.id for i in cluster[0]}
        first = cluster[0][0]
        claimed: dict[str, list[Incoming]] = {}
        for item in kept:
            await step(1)
            if target is not None:
                song, close = matches[item.id]
                if song is not None:
                    claimed.setdefault(song.song_id, []).append(item)
                    decisions[item.id] = Decision(
                        CATALOG_SONG if song.placeholder else LINKED,
                        song=song,
                        album=target,
                        reason=(
                            "Already in the library as a song Shijhon added from the catalog."
                            if song.placeholder
                            else "Already in the library."
                        ),
                    )
                elif close:
                    decisions[item.id] = await _unsure(item, target, target.name, tags_of)
                else:
                    decisions[item.id] = await _joining(item, target, tags_of)
            elif item.id in primary:
                decisions[item.id] = Decision(NEW)
            else:
                decisions[item.id] = _joining_incoming(item, first)
        # "Not the same song": never linked, but published like any new song - into the
        # album of its name, as a copy when its own tags would make a second one.
        for item in distinct:
            if target is not None:
                decisions[item.id] = await _joining(item, target, tags_of)
            elif item.id in primary:
                decisions[item.id] = Decision(NEW, reason=_DISTINCT)
            else:
                decisions[item.id] = _joining_incoming(item, first)
        # One library song for two incoming files (two different tracks the library
        # has once): only one whose position agrees keeps the match.
        for claims in claimed.values():
            if len(claims) < 2:
                continue
            decision = decisions[claims[0].id]
            assert decision.song is not None and target is not None
            keep = [c for c in claims if _positions_agree(c.song, decision.song.song)]
            for item in claims:
                if len(keep) != 1 or item is not keep[0]:
                    decisions[item.id] = await _unsure(item, target, decision.song.album, tags_of)
        if minimum is not None:
            await _gate(items, decisions, minimum, catalog)
    return decisions


_DISTINCT = "You said this is not the same song."


async def _unsure(
    item: Incoming,
    album: LibraryAlbum,
    name: str,
    tags_of: Callable[[LibraryAlbum], Awaitable[Tags | None]],
) -> Decision:
    """Close to a song of the library's album but not clearly the same: published and
    flagged, into that album like any new song (as a copy when its own tags would make a
    second album of that name; held when it cannot be one)."""
    unsure = (
        f'Possibly already in the library: it is close to a song of "{name}", but Shijhon'
        " cannot tell for sure."
    )
    joined = await _joining(item, album, tags_of)
    if joined.outcome == NEW:
        return Decision(UNSURE, album=album, reason=unsure)
    if joined.outcome == COPY:
        return replace(joined, outcome=UNSURE, reason=f"{unsure} {joined.reason}")
    return replace(joined, reason=f"{unsure} {joined.reason}")


def _below(item: Incoming, wanted: str) -> str:
    return f"{item.quality.describe()} is below your minimum ({wanted})."


async def _gate(
    items: Sequence[Incoming],
    decisions: dict[str, Decision],
    minimum: Minimum,
    catalog: CatalogLookup | None,
) -> None:
    """The quality gate for one album (module docstring), after its comparison with the
    library."""
    considered = [i for i in items if decisions[i.id].outcome in GATED]
    low = {i.id for i in considered if not minimum.met_by(i.quality)}
    if not low:
        return
    wanted = minimum.describe()

    def why(item: Incoming) -> str:
        if item.id in low:
            return _below(item, wanted)
        return f"Other files of this album are below your minimum ({wanted})."

    if any(i.keep_files for i in items):
        for item in considered:
            if item.id in low:
                decisions[item.id] = replace(
                    decisions[item.id],
                    gate={
                        "below_minimum": wanted,
                        "route": FILES,
                        "kept": True,
                        "reason": f"{why(item)} Added as you chose.",
                    },
                )
        return
    for item in considered:
        if decisions[item.id].outcome == CATALOG_SONG:
            decisions[item.id] = replace(
                decisions[item.id],
                gate={
                    "below_minimum": wanted,
                    "audio": False,
                    "reason": (
                        f"{why(item)} Already in the library as a song Shijhon added from the"
                        " catalog: it is linked and keeps playing from the add-ons."
                    ),
                },
            )
    route = [i for i in items if decisions[i.id].outcome in (NEW, COPY, UNSURE, HELD)]
    if not route:
        return
    try:
        found = await catalog(route) if catalog is not None else None
    except CatalogUnreachable:
        for item in route:
            decisions[item.id] = Decision(
                LOW_QUALITY,
                album=decisions[item.id].album,
                gate={
                    "below_minimum": wanted,
                    "route": UNCHECKED,
                    "reason": (
                        f"{why(item)} Shijhon could not reach the catalog to look for this"
                        " album. Check again, or add your files anyway; otherwise it is not"
                        " added."
                    ),
                },
            )
        return
    if found is None or any(i.id not in found.tracks for i in route):
        missing = "No catalog is set up" if catalog is None else "The catalog does not have it"
        for item in route:
            if item.id in low:
                decisions[item.id] = replace(
                    decisions[item.id],
                    gate={
                        "below_minimum": wanted,
                        "route": FILES,
                        "reason": f"{why(item)} {missing}, so your file is added.",
                    },
                )
        return
    for item in route:
        decisions[item.id] = Decision(
            LOW_QUALITY,
            album=decisions[item.id].album,
            gate={
                "below_minimum": wanted,
                "route": CATALOG,
                "release": found.release,
                "fingerprint": found.fingerprint,
                "track": found.tracks[item.id],
                "reason": (
                    f"{why(item)} The catalog's version is added instead; it plays from the"
                    " add-ons and counts as your import."
                ),
            },
        )


async def _joining(
    item: Incoming, album: LibraryAlbum, tags_of: Callable[[LibraryAlbum], Awaitable[Tags | None]]
) -> Decision:
    """A song the library's album lacks: published as it is when Navidrome puts it into
    that album anyway, else as a copy with the album's tags, else held."""
    tags = await tags_of(album)
    joined = f'Adds a song to the library\'s album "{album.name}".'
    said = _DISTINCT + " " if item.distinct else ""
    if tags is not None:
        if same_native_album(item.tags, tags, a_artist=item.artist, b_artist=album.artist):
            return Decision(NEW, album=album, reason=said + joined)
    elif (
        identity(item.facts.get("album")) == identity(album.name)
        and identity(item.album_artist) == identity(album.artist)
        and str((item.tags.get("musicbrainz_albumid") or [""])[0]).lower()
        == album.songs[0].album_mbid.lower()
    ):
        # The library album's tags cannot be read: its name, artist and MusicBrainz ID
        # agree, so Navidrome most likely puts the song there.
        return Decision(NEW, album=album, reason=said + joined)
    if tags is not None and item.taggable:
        return Decision(
            COPY,
            album=album,
            tags_from="album:" + album.album_id,
            target_key=album_key(tags, album.artist),
            reason=(
                f'{said}Added to the library\'s album "{album.name}" as a copy carrying that '
                "album's details. Your file stays unchanged."
            ),
        )
    return Decision(
        HELD,
        album=album,
        reason=(
            f"Not added: Shijhon cannot give this file the details of the library's album "
            f'"{album.name}", and on its own it would show as a second album of that name.'
        ),
    )


def _joining_incoming(item: Incoming, primary: Incoming) -> Decision:
    """A song of a second copy of an album in this selection: joins the first copy's
    album the same way."""
    said = _DISTINCT + " " if item.distinct else ""
    if same_native_album(
        item.tags, primary.tags, a_artist=item.artist, b_artist=primary.artist
    ) or not primary.facts.get("album_tags"):
        return Decision(NEW, album_item=primary.id, reason=said.strip())
    if item.taggable:
        return Decision(
            COPY,
            album_item=primary.id,
            tags_from="item:" + primary.id,
            target_key=album_key(primary.tags, primary.artist),
            reason=(
                f'{said}Added to the album "{primary.album}" of this import as a copy carrying '
                "that album's details. Your file stays unchanged."
            ),
        )
    return Decision(
        HELD,
        album_item=primary.id,
        reason=(
            f"Not added: Shijhon cannot give this file the details of the album "
            f'"{primary.album}" of this import, and on its own it would show as a second '
            "album of that name."
        ),
    )
