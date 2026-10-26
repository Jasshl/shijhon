"""One album per name, one entry per recording: the rule music imports and catalog
additions share, so that apps never show the same album twice.

Two albums of an artist are **the same album** when their names match - letter case,
accents, spacing and punctuation aside, so "NightLetters" is "Night Letters", while an
edition's words count, so "Night Letters (Deluxe Edition)" is another album - and either

- they share a recording (``same_recording``), whatever their years or covers say: a
  reissue with a new cover and year is still the album; or
- sharing none (a second disc imported on its own), their years do not differ and their
  covers do not clearly differ. An artist's self-titled albums of different years, which
  share no song, stay apart.

An album without a name is never the same as another: there is nothing to go on.

Two songs are **the same recording** (``same_recording``) by the rules owned-recording
backing uses (``placeholders/backing.py``): a shared MusicBrainz recording ID or ISRC,
else the same recording title (``recording_key``: "(Live)", "(Remix)" and other version
markers make other audio, edition markers do not), a shared artist when both name one,
and no clean edit beside an unmarked or explicit one - with lengths within 2 seconds
whenever both are known. Two known recording IDs or ISRC sets that differ are two
recordings.

``pairs`` matches the songs of two albums one to one, unambiguously: a song that fits
several of the other album's songs equally well is left unmatched (in doubt, not the
same).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from shijhon.matching.normalize import (
    artist_names,
    close_duration,
    fold,
    recording_key,
    version_marker,
)

TOLERANCE_MS = 2000  # the same recording's lengths differ at most this much (backing's)


@dataclass(frozen=True)
class Song:
    title: str
    artist: str = ""
    duration_ms: int = 0
    disc: int | None = None
    number: int | None = None
    isrcs: tuple[str, ...] = ()
    recording_id: str = ""  # MusicBrainz recording ID
    clean: bool | None = None  # True: a clean edit; False: explicit; None: not known


@dataclass(frozen=True)
class Album:
    name: str
    artist: str
    year: int | None = None
    songs: tuple[Song, ...] = ()


@dataclass(frozen=True)
class Keys:
    """What ``same_recording`` compares of a song, worked out once (the title's
    normalization is the costly part): folded recording ID and ISRCs, the recording title
    (``recording_key``), the artist names, whether it is a clean edit, its length."""

    recording_id: str
    isrcs: frozenset[str]
    title: str
    artists: frozenset[str]
    clean: bool | None
    duration_ms: int


def keys(song: Song) -> Keys:
    mark = version_marker(song.title)
    return Keys(
        fold(song.recording_id),
        frozenset(fold(i) for i in song.isrcs if fold(i)),
        recording_key(song.title),
        frozenset(artist_names(song.artist)),
        mark == "clean" if mark is not None else song.clean,
        song.duration_ms,
    )


def _versions_agree(a: Keys, b: Keys) -> bool:
    """As backing has it: a clean edit is other audio, so when either side is clean
    both must be."""
    return not (a.clean or b.clean) or bool(a.clean and b.clean)


def same_keys(a: Keys, b: Keys, *, tolerance_ms: int = TOLERANCE_MS) -> bool:
    """``same_recording`` of two songs' ``keys``."""
    timed = bool(a.duration_ms and b.duration_ms)
    if timed and not close_duration(a.duration_ms, b.duration_ms, tolerance_ms):
        return False
    if a.recording_id and b.recording_id:
        return a.recording_id == b.recording_id and _versions_agree(a, b)
    if a.isrcs and b.isrcs:
        return bool(a.isrcs & b.isrcs)
    if not timed:
        return False
    if a.title != b.title or not _versions_agree(a, b):
        return False
    return not a.artists or not b.artists or bool(a.artists & b.artists)


def same_recording(a: Song, b: Song, *, tolerance_ms: int = TOLERANCE_MS) -> bool:
    """(Module docstring.) A song whose length is not known (raw AAC and Ogg FLAC files
    have none natively) is the same recording only by a shared recording ID or ISRC."""
    return same_keys(keys(a), keys(b), tolerance_ms=tolerance_ms)


class Recordings:
    """Songs indexed by what can make another song the same recording, so that finding
    a song's matches does not compare it with every song: two songs are the same
    recording only when they share a recording ID, else an ISRC, else a recording title
    (``same_keys``) - each a bucket here. ``matching`` gives the positions of the songs
    that are, in the order they were added."""

    def __init__(self, songs: Sequence[Keys] = ()) -> None:
        self.keys: list[Keys] = []
        self._buckets: dict[tuple[str, str], list[int]] = {}
        for song in songs:
            self.add(song)

    @staticmethod
    def _of(song: Keys) -> list[tuple[str, str]]:
        found = [("title", song.title)]
        if song.recording_id:
            found.append(("id", song.recording_id))
        found.extend(("isrc", isrc) for isrc in song.isrcs)
        return found

    def add(self, song: Keys) -> int:
        position = len(self.keys)
        self.keys.append(song)
        for bucket in self._of(song):
            self._buckets.setdefault(bucket, []).append(position)
        return position

    def matching(self, song: Keys) -> list[int]:
        candidates: set[int] = set()
        for bucket in self._of(song):
            candidates.update(self._buckets.get(bucket, ()))
        return [i for i in sorted(candidates) if same_keys(song, self.keys[i])]


def _position(a: Song, b: Song) -> bool:
    return a.number is not None and a.number == b.number and (a.disc or 1) == (b.disc or 1)


def pairs(incoming: Sequence[Song], existing: Sequence[Song]) -> dict[int, int]:
    """``incoming`` index -> ``existing`` index for each song that is unambiguously the
    same recording: the only candidate, or the only one at its disc and track position.
    Each existing song is used once; a song two incoming songs claim stays unmatched for
    both."""
    chosen: dict[int, int] = {}
    index = Recordings([keys(other) for other in existing])
    for i, song in enumerate(incoming):
        fits = index.matching(keys(song))
        if len(fits) > 1:
            placed = [j for j in fits if _position(song, existing[j])]
            fits = placed if len(placed) == 1 else []
        if len(fits) == 1:
            chosen[i] = fits[0]
    claimed: dict[int, int] = {}
    for j in chosen.values():
        claimed[j] = claimed.get(j, 0) + 1
    return {i: j for i, j in chosen.items() if claimed[j] == 1}


def same_name(a: Album, b: Album) -> bool:
    """The same album name and artist: letter case, accents, spacing and punctuation
    aside, edition words counted; artists that share a credited name."""
    name = fold(a.name)
    if not name or name != fold(b.name):
        return False
    artists_a, artists_b = artist_names(a.artist), artist_names(b.artist)
    return bool(artists_a & artists_b) or fold(a.artist) == fold(b.artist)


def same_album(
    a: Album, b: Album, *, covers_differ: Callable[[], bool | None] | None = None
) -> bool:
    """Whether ``a`` and ``b`` are one album (module docstring). ``covers_differ`` is
    asked only when they share no recording: True when their covers clearly differ, False
    when they match, None when either has none."""
    if not same_name(a, b):
        return False
    if pairs(a.songs, b.songs):
        return True
    if a.year and b.year and a.year != b.year:
        return False
    return (covers_differ() if covers_differ is not None else None) is not True
