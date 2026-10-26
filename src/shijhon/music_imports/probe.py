"""Read-only, killable metadata/header inspection; never a full audio decode.

Mutagen inspects local containers without URL/reference loaders. Matroska,
Shorten and TrueAudio channel counts use ffprobe with a capped stdin, explicit
demuxer and only the pipe protocol.
The parent passes an already opened read-only descriptor. Neither tool writes tags.
"""

from __future__ import annotations

import io
import json
import math
import os
import resource
import subprocess
import sys
from typing import Any

from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.dsf import DSF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.tak import TAK
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

from shijhon.matching import covers
from shijhon.music_imports import facts

READ_LIMIT = 16 * 1024**2
PARSERS: dict[str, tuple[Any, ...]] = {
    "mp3": (MP3,),
    "mp2": (MP3,),
    "flac": (FLAC,),
    "aac": (AAC,),
    "m4a": (MP4,),
    "m4b": (MP4,),
    "alac": (MP4,),
    "ogg": (OggVorbis, OggOpus, OggFLAC, OggSpeex),
    "oga": (OggVorbis, OggOpus, OggFLAC, OggSpeex),
    "opus": (OggOpus,),
    "wav": (WAVE,),
    "aif": (AIFF,),
    "aiff": (AIFF,),
    "aifc": (AIFF,),
    "spx": (OggSpeex,),
    "wma": (ASF,),
    "wv": (WavPack,),
    "wvp": (WavPack,),
    "ape": (MonkeysAudio,),
    "mpc": (Musepack,),
    "tak": (TAK,),
    "tta": (TrueAudio,),
    "dsf": (DSF,),
}


class HeaderReader(io.BufferedReader):
    """Bound parser reads and allocation even on platforms without RLIMIT_AS."""

    remaining = READ_LIMIT

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            size = max(0, os.fstat(self.fileno()).st_size - self.tell())
        if size > self.remaining:
            raise ValueError
        self.remaining -= size
        return super().read(size)


def _ffprobe(source: HeaderReader, fmt: str) -> tuple[float | None, int, int, Any, str]:
    # No filename, network, file protocol, playlist demuxer or child process input
    # references are available. Large originals remain on disk; at most 16 MiB
    # enter the parser. Require a recognized audio stream, sample rate and
    # channels; Shorten legitimately carries no duration available to ffprobe.
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-max_alloc",
            str(READ_LIMIT),
            "-probesize",
            str(READ_LIMIT),
            "-analyzeduration",
            "5000000",
            "-protocol_whitelist",
            "pipe",
            "-f",
            {"mka": "matroska", "shn": "shn", "tta": "tta"}[fmt],
            "-i",
            "pipe:0",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration:format=duration:"
            # Matroska: every tag (a copy that joins an album takes its album's).
            + ("format_tags" if fmt == "mka" else "format_tags=title,artist,album")
            + ":stream_tags=title,artist,album",
            "-of",
            "json",
        ],
        input=source.read(min(source.remaining, os.fstat(source.fileno()).st_size)),
        capture_output=True,
        timeout=10,
        check=True,
    )
    if len(result.stdout) > READ_LIMIT:
        raise ValueError
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    if not stream.get("codec_name") or stream["codec_name"] == "unknown":
        raise ValueError
    container = data.get("format", {})
    tags = {k.casefold(): v for k, v in container.get("tags", {}).items()}
    tags.update({k.casefold(): v for k, v in stream.get("tags", {}).items()})
    duration = container.get("duration", stream.get("duration"))
    return (
        float(duration) if duration is not None else None,
        int(stream["sample_rate"]),
        int(stream["channels"]),
        tags,
        str(stream["codec_name"]),
    )


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    # RLIMIT_AS is reliable on Linux; macOS does not implement an equivalent bound.
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    fd, fmt = int(sys.argv[1]), sys.argv[2]
    cover = len(sys.argv) > 3 and sys.argv[3] == "cover"
    parsed: Any = None
    codec = ""
    with HeaderReader(os.fdopen(fd, "rb", buffering=0)) as source:
        if fmt in ("mka", "shn"):
            length, sample_rate, channels, tags, codec = _ffprobe(source, fmt)
            keys = ("title", "artist", "album")
        else:
            for parser in PARSERS[fmt]:
                source.seek(0)
                try:
                    parsed = parser(source)
                    break
                except Exception:
                    parsed = None  # parser errors may contain private tags; never log them
            if parsed is None:
                raise ValueError
            info = parsed.info
            if fmt in ("mp3", "mp2") and info.layer != (3 if fmt == "mp3" else 2):
                raise ValueError
            length, sample_rate, channels = (
                info.length,
                getattr(info, "sample_rate", 48000 if isinstance(parsed, OggOpus) else 0),
                getattr(info, "channels", 0),
            )
            if isinstance(parsed, TrueAudio):
                # Mutagen provides TrueAudio duration/rate/ID3 but no channel
                # count. Reuse the bounded local probe rather than inventing it.
                source.seek(0)
                _, probed_rate, channels, _, _ = _ffprobe(source, "tta")
                if probed_rate != sample_rate:
                    raise ValueError
            tags = parsed.tags
            if isinstance(parsed, AAC):
                source.seek(0)
                try:
                    tags = ID3(source)
                except ID3NoHeaderError:
                    tags = None
            if isinstance(parsed, (MP3, WAVE, AIFF, DSF, AAC, TrueAudio)):
                keys = ("TIT2", "TPE1", "TALB")
            elif isinstance(parsed, MP4):
                keys = ("\xa9nam", "\xa9ART", "\xa9alb")
            elif isinstance(parsed, ASF):
                keys = ("Title", "Author", "WM/AlbumTitle")
            else:
                keys = ("title", "artist", "album")
        if (
            (length is not None and (not math.isfinite(length) or not 0 < length <= 7 * 24 * 3600))
            or not 0 < sample_rate <= 24576000  # includes native DSD sample rates
            or not 0 < channels <= 32
        ):
            raise ValueError
        warnings = []
        if tags is None or not tags.get(keys[0]):
            warnings.append(
                "Missing title tag; music apps use the preserved filename as the title."
            )
        if tags is None or any(not tags.get(key) for key in keys[1:]):
            warnings.append(
                "Missing artist or album tags. Add tags before importing for clearer music details."
            )
        if cover:
            # Only asked for an album the review compares by its cover (``overlap.py``).
            image = facts.picture(parsed) if parsed is not None else None
            print(json.dumps({"cover": covers.fingerprint_now(image) if image else None}))
            return
        found = (
            facts.of_mutagen(parsed, fmt, length, sample_rate, tags)
            if parsed is not None
            else facts.of_ffprobe(tags, fmt, length, sample_rate, codec)
        )
        print(json.dumps({"warnings": warnings, "facts": found}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Do not emit parser errors, tags or file details to logs/browser.
        sys.exit(1)
