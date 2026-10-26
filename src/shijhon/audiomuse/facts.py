"""AudioMuse-AI's analysis of a song as Shijhon keeps it, and the percentiles it is shown as.

What AudioMuse-AI stores for a song (its ``score`` table, v3.6; ``database.py`` and
``tasks/analysis/song.py`` there):

- ``tempo``: BPM, folded into 40-200.
- ``key`` ("C", "C#" ... "B") and ``scale`` ("major" or "minor"): a Krumhansl-Schmuckler
  estimate.
- ``energy``: the mean loudness of the song, 0-1 (RMS in dB from -60 to 0). Digital silence
  is 0 - a silent placeholder looks like that, so a song with exactly 0 has no facts.
- ``mood_vector``: the top five MusiCNN labels, ``"rock:0.612,pop:0.301,..."`` - mostly
  genres and decades, strongest first.
- ``other_features``: ``"danceable:0.62,aggressive:0.51,..."``: how close the audio is to six
  words in CLAP's embedding space, squeezed to 0-1 and all well above 0. They tell songs
  apart only as a ranking, which is what the percentiles give. Until its CLAP step has run
  they are all 0: no facts.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from shijhon.audiomuse.client import Track
from shijhon.playlists.sound import FEATURES, SongSound

LABELS = 5
MAX_TEXT = 2000  # AudioMuse-AI's own limit for these columns


@dataclass(frozen=True)
class Analysis:
    """The raw facts of one song (what the ``sound_facts`` row holds)."""

    song_id: str
    fingerprint: str  # AudioMuse-AI's, of the analysis: it changes when the analysis does
    tempo: float | None
    key: str  # "A minor"
    energy: float | None
    features: dict[str, float]  # the six, raw; empty until they are scored
    labels: list[str]


def analysis(track: Track) -> Analysis | None:
    """The facts of a song from AudioMuse-AI's payload; None when it has none worth keeping:
    silent audio (a placeholder that was analyzed), or nothing but a name."""
    energy = _number(track.energy)
    if energy is not None:
        energy = min(max(energy, 0.0), 1.0)
        if energy == 0.0:
            return None
    tempo = _number(track.tempo)
    if tempo is not None and tempo <= 0:
        tempo = None
    facts = Analysis(
        track.id,
        track.fp,
        tempo,
        _key(track.key, track.scale),
        energy,
        _features(track.other_features),
        _labels(track.mood_vector),
    )
    if facts.tempo is None and facts.energy is None and not facts.features and not facts.labels:
        return None
    return facts


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


_KEYS = frozenset({"C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"})


def _key(note: Any, scale: Any) -> str:
    if not isinstance(note, str) or note.strip() not in _KEYS:
        return ""
    mode = scale.strip().lower() if isinstance(scale, str) else ""
    return f"{note.strip()} {mode}" if mode in ("major", "minor") else note.strip()


def scores(text: Any) -> dict[str, float]:
    """``"label:0.5,label:0.25"`` as a mapping, in its order; what is not a score is left out."""
    found: dict[str, float] = {}
    if not isinstance(text, str):
        return found
    for part in text[:MAX_TEXT].split(","):
        label, _, value = part.rpartition(":")
        label = label.strip()
        try:
            number = float(value)
        except ValueError:
            continue
        if label and math.isfinite(number):
            found.setdefault(label, number)
    return found


def _features(text: Any) -> dict[str, float]:
    found = {name: value for name, value in scores(text).items() if name in FEATURES}
    return found if any(value > 0 for value in found.values()) else {}


def _labels(text: Any) -> list[str]:
    ranked = sorted(scores(text).items(), key=lambda item: item[1], reverse=True)
    return [label for label, _ in ranked[:LABELS]]


# --- percentiles --------------------------------------------------------------------------


def percentile(ordered: list[float], value: float) -> int:
    """Where ``value`` stands among the sorted ``ordered`` values, 0-100: the share of values
    below it, a tie counting half. The only one: 50."""
    if not ordered:
        return 50
    below, through = bisect_left(ordered, value), bisect_right(ordered, value)
    return round(100 * (below + through) / (2 * len(ordered)))


class Distribution:
    """The energies and the six mood-like scores of the synced songs, to place one song among
    them. Built from all the songs' rows; compare songs of this library with each other."""

    def __init__(self, energies: list[float | None], features: list[Mapping[str, float]]) -> None:
        self.energy = sorted(value for value in energies if value is not None)
        self.features = {name: sorted(f[name] for f in features if name in f) for name in FEATURES}

    def sound(self, row: Analysis) -> SongSound:
        return SongSound(
            tempo=round(row.tempo, 1) if row.tempo is not None else None,
            key=row.key,
            energy=percentile(self.energy, row.energy) if row.energy is not None else None,
            features={
                name: percentile(self.features[name], value)
                for name, value in row.features.items()
                if self.features.get(name)
            },
            labels=row.labels[:LABELS],
        )
