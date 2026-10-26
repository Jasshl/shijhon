"""Optional per-song sound facts and sound-based suggestions for playlist curation.

An analyzer (AudioMuse-AI) describes songs whose real audio it has analyzed. Curation
treats everything here as approximate hints beside the AI's own musical knowledge:
missing facts never exclude a song, and an unavailable analyzer never fails a playlist.
Song IDs are Navidrome song IDs as Shijhon's library answers them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import Field

from shijhon.playlists.model import Model

# Mood-like scores the analyzer gives every song, shown as percentiles in this library.
FEATURES = ("danceable", "aggressive", "happy", "party", "relaxed", "sad")


class SoundUnavailable(Exception):
    """The analyzer is not configured, unreachable or answered unusably."""


class SongSound(Model):
    """Approximate hints; ``energy`` and ``features`` are percentiles (0-100) among the
    songs of this library that have facts, so values compare songs with each other."""

    tempo: float | None = None
    key: str = ""
    energy: int | None = Field(default=None, ge=0, le=100)
    features: dict[str, int] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list, max_length=5)


class SoundStatus(Model):
    configured: bool
    reachable: bool = False
    songs: int = 0
    synced: float = 0
    error: str = ""


class Sound(Protocol):
    async def facts(self, song_ids: Sequence[str]) -> dict[str, SongSound]:
        """Facts for the given songs that have them; others are absent."""
        ...

    async def similar(
        self, seeds: Sequence[str], *, avoid: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        """Songs that sound like the seeds and unlike ``avoid``, best first, seeds excluded."""
        ...

    async def search(
        self, text: str, *, more: Sequence[str] = (), less: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        """Songs matching an English description of their sound, best first."""
        ...

    async def path(self, start: str, end: str, *, steps: int = 12) -> list[str]:
        """A sequence of songs moving gradually from ``start`` to ``end``, both included."""
        ...

    async def status(self) -> SoundStatus: ...
