"""Whether two album covers are clearly different pictures (``matching/albums.py`` asks
only for same-named albums that share no song).

A cover's fingerprint is the difference hash of its picture: ffmpeg - already on the
package's path for imports and add-on audio - scales it to 9x8 grey pixels, and each bit
says whether a pixel is brighter than its right neighbour. A smaller, recompressed or
slightly cropped copy of the same cover keeps nearly every bit; another picture changes
about half of them. Input reaches ffmpeg on stdin only (the pipe protocol, at most 16 MiB),
under a wall deadline.
"""

from __future__ import annotations

import subprocess

import anyio

MAX_IMAGE = 16 * 1024**2
DEADLINE_SECONDS = 10.0
DIFFERENT_BITS = 12  # more than this many of the 64 bits differ: another picture


def fingerprint_now(image: bytes) -> int | None:
    """``fingerprint`` in the calling thread (a subprocess that has the picture)."""
    if not image or len(image) > MAX_IMAGE:
        return None
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "pipe",
                "-f",
                "image2pipe",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=9:8:flags=area,format=gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            input=image,
            capture_output=True,
            timeout=DEADLINE_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    pixels = result.stdout
    if result.returncode != 0 or len(pixels) != 72:
        return None
    bits = 0
    for row in range(8):
        for column in range(8):
            left, right = pixels[row * 9 + column], pixels[row * 9 + column + 1]
            bits = (bits << 1) | (left > right)
    return bits


async def fingerprint(image: bytes) -> int | None:
    """The cover's fingerprint, or None when it cannot be read as a picture."""
    return await anyio.to_thread.run_sync(fingerprint_now, image)


def differ(a: int | None, b: int | None) -> bool | None:
    """True when the covers are clearly different pictures, False when they are the same
    picture, None when either is not known."""
    if a is None or b is None:
        return None
    return (a ^ b).bit_count() > DIFFERENT_BITS
