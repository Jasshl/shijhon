"""Audio quality: what a file or an add-on's audio is, and whether it meets a person's
minimum.

Each person sets their own minimum: lossless audio, or compressed audio of at least a
bitrate, and in either case at least a sample rate. Audio below it is
not imported, does not stand in for catalog songs and plays from the add-ons when they
have it well enough; what is at or above it is kept as it is, lossless or not.

A quality that is not known (a raw AAC file reports no bitrate) meets every minimum:
nothing is turned away for missing information. Lossless audio is told by its codec or
format, or by a bit depth, which compressed formats do not have.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Formats whose audio is always lossless; ".m4a" (ALAC or AAC) and Ogg (FLAC, Vorbis, Opus)
# can be either, so their codec or bit depth decides.
LOSSLESS_FORMATS = frozenset(
    {
        "flac",
        "alac",
        "wav",
        "aif",
        "aiff",
        "aifc",
        "wv",
        "wvp",
        "ape",
        "tta",
        "shn",
        "dsf",
        "dff",
        "tak",
    }
)
LOSSLESS_CODECS = frozenset({"flac", "alac", "pcm", "wavpack", "ape", "tta", "shorten", "dsd"})
CHOICES_KBPS = (0, 128, 160, 192, 256, 320)  # the minimums a person can choose (0: any)


@dataclass(frozen=True)
class Quality:
    format: str = ""  # file suffix or container, lower case
    lossless: bool = False
    kbps: int = 0  # average bitrate; 0: not known
    sample_rate: int = 0  # Hz; 0: not known
    bit_depth: int = 0  # 0: not known or compressed

    @classmethod
    def of_song(cls, song: Mapping[str, Any]) -> Quality:
        """From an OpenSubsonic song entry (``suffix``, ``bitRate``, ``samplingRate``,
        ``bitDepth``; ``codec`` where given)."""
        fmt = str(song.get("suffix") or "").lower()
        codec = str(song.get("codec") or "").lower()
        depth = _number(song.get("bitDepth"))
        lossless = fmt in LOSSLESS_FORMATS or codec in LOSSLESS_CODECS or depth > 0
        return cls(
            fmt, lossless, _number(song.get("bitRate")), _number(song.get("samplingRate")), depth
        )

    def known(self) -> bool:
        return self.lossless or self.kbps > 0

    def rank(self) -> tuple[int, int, int, int]:
        """Higher is better: lossless first, then bitrate, sample rate and bit depth."""
        return (
            int(self.lossless),
            self.kbps if not self.lossless else 0,
            self.sample_rate,
            self.bit_depth,
        )

    def describe(self) -> str:
        """For people: "FLAC 24-bit 96 kHz", "MP3 320 kbps"."""
        name = (self.format or "audio").upper()
        rate = f" {self.sample_rate / 1000:g} kHz" if self.sample_rate else ""
        if self.lossless:
            depth = f" {self.bit_depth}-bit" if self.bit_depth else ""
            return f"{name}{depth}{rate}".strip()
        bitrate = f" {self.kbps} kbps" if self.kbps else ""
        return f"{name}{bitrate}".strip()


@dataclass(frozen=True)
class Minimum:
    kbps: int = 192  # compressed audio needs at least this (0: any)
    lossless_only: bool = False
    sample_rate: int = 44100  # Hz, for every kind of audio (0: any)

    def met_by(self, quality: Quality) -> bool:
        if not quality.known():
            return True
        if self.sample_rate and quality.sample_rate and quality.sample_rate < self.sample_rate:
            return False
        if quality.lossless:
            return True
        return not self.lossless_only and quality.kbps >= self.kbps

    def describe(self) -> str:
        """For people, the two kinds of audio apart (a minimum bitrate is no lossless
        one): "compressed at 192 kbps or more; lossless at any bitrate; 44.1 kHz or more"."""
        rate = f"; {self.sample_rate / 1000:g} kHz or more" if self.sample_rate else ""
        if self.lossless_only:
            return f"lossless only{rate}"
        if not self.kbps:
            return "any quality"
        return f"compressed at {self.kbps} kbps or more; lossless at any bitrate{rate}"


ANY = Minimum(kbps=0, lossless_only=False, sample_rate=0)
DEFAULT = Minimum()


def _number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
