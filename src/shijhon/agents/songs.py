"""Song entries and playlist profiles for the agent tools.

Entries carry what Navidrome itself knows about a song (year, genres, tempo and mood tags),
community tags from Last.fm when they are known and, when asked, sound facts from the
analyzer. Listening evidence (play counts, ratings, favorites) is never copied: library
membership is not listening evidence, and an agent reads history only through its own
permission.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from shijhon.playlists.sound import FEATURES, SongSound

# Keys copied as they are; library searches show a song's place in its album, playlists less.
LIBRARY_KEYS = ("id", "title", "artist", "album", "albumId", "artistId", "duration", "track")
COMPACT_KEYS = ("id", "title", "artist", "album", "duration")
MAX_GENRES = 8
MAX_MOODS = 8
TOP_GENRES = 8
TOP_LABELS = 8
TOP_TAGS = 8


def names(value: object, limit: int) -> list[str]:
    """Short names, once each whatever the capitals, from strings or ``{"name": ...}``."""
    found: dict[str, str] = {}
    for item in value if isinstance(value, list) else []:
        text = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(text, str) and text.strip():
            found.setdefault(text.strip()[:100].lower(), text.strip()[:100])
    return list(found.values())[:limit]


def genres(song: Mapping[str, Any]) -> list[str]:
    """OpenSubsonic ``genres``, or the single ``genre`` an older answer carries."""
    listed = names(song.get("genres"), MAX_GENRES)
    return listed or names([song.get("genre")], 1)


def entry(song: Mapping[str, Any], keys: Sequence[str] = COMPACT_KEYS) -> dict[str, Any]:
    """A bounded song entry; absent facts are left out instead of shown empty."""
    result: dict[str, Any] = {key: song[key] for key in keys if key in song}
    year, bpm = song.get("year"), song.get("bpm")
    if isinstance(year, int) and not isinstance(year, bool) and year > 0:
        result["year"] = year
    if found := genres(song):
        result["genres"] = found
    if isinstance(bpm, int) and not isinstance(bpm, bool) and bpm > 0:
        result["bpm"] = bpm
    if moods := names(song.get("moods"), MAX_MOODS):
        result["moods"] = moods
    return result


def sound_facts(sound: SongSound) -> dict[str, Any]:
    """Only the facts the analyzer has: tempo, key, energy, feature percentiles, labels."""
    return {
        key: value
        for key, value in sound.model_dump(mode="json").items()
        if value not in (None, "", [], {})
    }


def number(value: float) -> int | float:
    return int(value) if value == int(value) else round(value, 1)


def spread(values: Sequence[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {
        "songs": len(values),
        "min": number(min(values)),
        "median": number(statistics.median(values)),
        "max": number(max(values)),
    }


def top(counts: Counter[str], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [{"name": name, "songs": songs} for name, songs in ranked[:limit]]


def summary(
    entries: Sequence[Mapping[str, Any]], sounds: Mapping[str, SongSound]
) -> dict[str, Any]:
    """What a set of songs is like: spreads of year, tempo tags, genres and sound facts.

    Energy and the six features are percentiles among the analyzed songs of this library, so
    the median compares a playlist with the rest of the library, not with an absolute scale.
    Songs without facts are counted and never guessed.
    """
    result: dict[str, Any] = {"songs": len(entries)}
    for key, values in (
        ("year", [e["year"] for e in entries if "year" in e]),
        ("bpm", [e["bpm"] for e in entries if "bpm" in e]),
    ):
        if block := spread(values):
            result[key] = block
    counts: Counter[str] = Counter()
    seen: dict[str, str] = {}
    for item in entries:
        # A genre counts once per song, however it is capitalized.
        for lowered, shown in {g.lower(): g for g in item.get("genres", [])}.items():
            counts[seen.setdefault(lowered, shown)] += 1
    if counts:
        result["genres"] = top(counts, TOP_GENRES)
    tagged: Counter[str] = Counter()
    songs_with_tags = 0
    for item in entries:
        found = list(dict.fromkeys(item.get("tags", [])))
        songs_with_tags += bool(found)
        tagged.update(found)
    if tagged:
        result["with_tags"] = songs_with_tags
        result["tags"] = top(tagged, TOP_TAGS)
    covered = [sounds[e["id"]] for e in entries if e.get("id") in sounds]
    result["with_sound_facts"] = len(covered)
    if covered:
        sound: dict[str, Any] = {}
        for key, values in (
            ("energy", [s.energy for s in covered if s.energy is not None]),
            ("tempo", [s.tempo for s in covered if s.tempo is not None]),
        ):
            if block := spread(values):
                sound[key] = block
        features = {
            name: block
            for name in FEATURES
            if (block := spread([s.features[name] for s in covered if name in s.features]))
        }
        if features:
            sound["features"] = features
        labels = Counter(label for s in covered for label in dict.fromkeys(s.labels))
        if labels:
            sound["labels"] = top(labels, TOP_LABELS)
        result["sound"] = sound
    return result


def unique(ids: Iterable[str]) -> list[str]:
    """The IDs once each, in the order given."""
    return list(dict.fromkeys(ids))


def spaced(ids: Sequence[str], limit: int) -> list[str]:
    """At most ``limit`` IDs spread evenly over the whole list, so a long playlist is
    represented by its beginning, middle and end alike."""
    if len(ids) <= limit:
        return list(ids)
    return [ids[i * len(ids) // limit] for i in range(limit)]
