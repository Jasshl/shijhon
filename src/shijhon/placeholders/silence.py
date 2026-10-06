"""Silent FLAC files at the catalog's exact duration.

Each placeholder gets its own file, exact to the sample: a placeholder rounded to whole
seconds makes clients show a different length once real audio replaces it.

The file is written here, not by an encoder: silence in FLAC is a STREAMINFO block and
frames whose two subframes are CONSTANT zero (a few bytes a frame), so there is nothing to
encode. One ffmpeg process per placeholder takes about a CPU-second each on a small server
and most of every fill's time; this takes a millisecond. 32768-sample frames: about 5 KB
for four minutes before the tags. The MD5 of the audio is left zero ("not computed",
allowed by the format).
"""

from __future__ import annotations

import os
import struct
import uuid
from pathlib import Path

import anyio
import anyio.to_thread

SAMPLE_RATE = 44100
CHANNELS = 2
BITS = 16
BLOCK = 32768  # samples per frame


class SilenceMaker:
    def __init__(self, *, parallel: int = 4) -> None:
        self._limit = anyio.CapacityLimiter(parallel)
        self.generated = 0  # observable in tests

    async def write(self, duration_ms: int, path: Path) -> None:
        """Write ``duration_ms`` of stereo silence (at least 1 s) to ``path``."""
        samples = samples_for(duration_ms)
        partial = path.with_name(f".partial-{uuid.uuid4().hex}.flac")
        try:
            async with self._limit:
                await anyio.to_thread.run_sync(_write, partial, samples)
            os.replace(partial, path)
            self.generated += 1
        finally:
            partial.unlink(missing_ok=True)


def samples_for(duration_ms: int) -> int:
    """The samples of a placeholder of ``duration_ms``: at least 1 s; at most what
    STREAMINFO's 36 bits hold (about 430 hours)."""
    return min(round(max(1000, duration_ms) * SAMPLE_RATE / 1000), (1 << 36) - 1)


def silent_flac(samples: int) -> bytes:
    """A FLAC stream of ``samples`` samples of 16-bit stereo silence at 44.1 kHz."""
    frames = [
        _frame(n, min(BLOCK, samples - start)) for n, start in enumerate(range(0, samples, BLOCK))
    ]
    sizes = [len(f) for f in frames]
    info = bytearray()
    info += struct.pack(">HH", min(BLOCK, samples), BLOCK)  # min and max block size
    info += min(sizes).to_bytes(3, "big") + max(sizes).to_bytes(3, "big")
    # Sample rate (20 bits), channels - 1 (3), bits - 1 (5), total samples (36).
    packed = (SAMPLE_RATE << 44) | ((CHANNELS - 1) << 41) | ((BITS - 1) << 36) | samples
    info += packed.to_bytes(8, "big")
    info += bytes(16)  # MD5 of the audio: not computed
    header = bytes([0x80]) + len(info).to_bytes(3, "big")  # last block, type 0: STREAMINFO
    return b"fLaC" + header + bytes(info) + b"".join(frames)


def _write(path: Path, samples: int) -> None:
    path.write_bytes(silent_flac(samples))


def _frame(number: int, size: int) -> bytes:
    """One frame of ``size`` samples: header, two CONSTANT-zero subframes, CRC-16."""
    # Sync code, fixed block size; block size code (32768, or 16 bits after the header),
    # 44.1 kHz (0b1001); two independent channels (0b0001), 16 bits (0b100).
    code = 0b1111 if size == BLOCK else 0b0111
    head = bytearray(b"\xff\xf8")
    head.append((code << 4) | 0b1001)
    head.append((0b0001 << 4) | (0b100 << 1))
    head += _utf8_number(number)
    if code == 0b0111:
        head += struct.pack(">H", size - 1)
    head.append(_crc8(head))
    subframe = b"\x00\x00\x00"  # CONSTANT, no wasted bits; the value 0 in 16 bits
    body = bytes(head) + subframe * CHANNELS
    return body + struct.pack(">H", _crc16(body))


def _utf8_number(value: int) -> bytes:
    """FLAC's frame number coding (UTF-8 style, up to 36 bits)."""
    if value < 0x80:
        return bytes([value])
    for length in range(2, 8):
        if value < 1 << (5 * length + 1):
            break
    out = [0x80 | ((value >> (6 * i)) & 0x3F) for i in range(length - 1)][::-1]
    first = ((0xFF << (8 - length)) & 0xFF) | (value >> (6 * (length - 1)))
    return bytes([first, *out])


def _crc8(data: bytes | bytearray) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
