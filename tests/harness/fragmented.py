"""Fragmented MP4 files of a synthetic tone, as some add-ons serve a whole song: a movie
header that tells no length (``empty_moov``), then the audio in movie fragments of about
two seconds, each with its own index (``moof``) - and, as some packagers write, a ``mehd``
box with the whole length (``mehd=True``). Built from the cached tones, with ffmpeg."""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from shijhon.delivery import mpd
from tests.harness.library import tone

CODECS = {  # the audio of a fragmented file, as ffmpeg encodes it
    "aac": ["-c:a", "aac", "-b:a", "128k"],
    "alac": ["-c:a", "alac"],
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    "opus": ["-c:a", "libopus", "-b:a", "96k"],
}


def fragmented_mp4(
    out: Path,
    frequency: int,
    seconds: float,
    codec: str = "aac",
    *,
    mehd: bool = True,
    tracks: int = 1,
    held: bool = False,
) -> Path:
    """``seconds`` of a tone at ``frequency`` as a fragmented MP4 at ``out``: ``tracks``
    audio tracks of it (the same tone); ``held``: the first fragment's samples in the movie
    header itself (no ``empty_moov``: its header then tells that fragment's length only)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    flags = "+frag_keyframe+default_base_moof+skip_trailer" + ("" if held else "+empty_moov")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(tone(frequency, seconds, "flac")),
         *(["-map", "0:a"] * tracks), *CODECS[codec], "-map_metadata", "-1",
         "-movflags", flags, "-frag_duration", "2000000", "-f", "mp4", str(out)],
        check=True, capture_output=True,
    )  # fmt: skip
    if mehd:
        _with_mehd(out)
    return out


def boxes(data: bytes, start: int = 0, end: int | None = None) -> list[tuple[bytes, int, int]]:
    """The boxes between ``start`` and ``end``: (type, start, size)."""
    found, at, end = [], start, len(data) if end is None else end
    while at + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, at)
        if size == 1:
            size = struct.unpack_from(">Q", data, at + 8)[0]
        elif size == 0:
            size = end - at
        found.append((kind, at, size))
        at += size
    return found


def is_fragmented(path: Path) -> bool:
    data = path.read_bytes()
    return any(kind == b"moof" for kind, _, _ in boxes(data))


def _with_mehd(path: Path) -> None:
    """A ``mehd`` box with the fragments' whole length (in the movie's timescale) put first
    into ``mvex``; the moov and mvex sizes grow by its size. The fragments' data offsets
    count from their own ``moof`` (``default_base_moof``): nothing else moves."""
    data = path.read_bytes()
    moov = next((at, size) for kind, at, size in boxes(data) if kind == b"moov")
    inner = boxes(data, moov[0] + 8, moov[0] + moov[1])
    mvhd = next(at for kind, at, _ in inner if kind == b"mvhd")
    mvex = next((at, size) for kind, at, size in inner if kind == b"mvex")
    version = data[mvhd + 8]
    scale = struct.unpack_from(">I", data, mvhd + (28 if version == 1 else 20))[0]
    found = mpd.inspect(data)
    assert found.timescale and found.units
    length = round(found.units * scale / found.timescale)
    box = struct.pack(">I4sIQ", 20, b"mehd", 1 << 24, length)  # version 1: 64-bit
    moov_size = struct.pack(">I", moov[1] + len(box))
    mvex_size = struct.pack(">I", mvex[1] + len(box))
    out = bytearray(data[: mvex[0] + 8])
    out[moov[0] : moov[0] + 4] = moov_size
    out[mvex[0] : mvex[0] + 4] = mvex_size
    out += box + data[mvex[0] + 8 :]
    path.write_bytes(bytes(out))
