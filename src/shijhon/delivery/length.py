"""The length of delivered audio, read from its first bytes.

A source's lookup can hand over another recording of a song (an add-on's ISRC lookup may find
a remix album's extended version of a much shorter track). The first bytes
of the audio tell its real length for the common formats, without fetching the rest:

- FLAC: the STREAMINFO block (total samples and sample rate) right after ``fLaC``;
- MP3: the first frame's Xing/Info or VBRI header (its frame count); without one there is
  no length to read (a constant-bitrate guess from the size would count trailing tags or
  artwork as audio);
- MP4/M4A: the ``mvhd`` box, when the whole ``moov`` box comes before the audio ("fast
  start") and the file is not fragmented (a fragmented file's ``moov`` says nothing about
  its length, unless its ``mehd`` box does).

Anything else - an MP4 with its index at the end, Ogg, WAV, an MP3 without a frame count,
a head cut short - has no length to read: None (the audio is used as before). A wrong
length would reject a correct recording, so every doubt is None.

The same first bytes tell the audio's format and, for FLAC and MP3, its bitrate as Navidrome
reads it: a request for a format and bitrate the audio already meets is served as it
is, as Navidrome serves a file that needs no converting.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator

PEEK_BYTES = 512 * 1024  # at most this much is read before the audio goes on (an ID3 tag)
MP3_BYTES = 2048  # an MP3's first frame (at most 1.7 KB), with its Xing/VBRI header
MP4_BYTES = 256 * 1024  # an MP4 whose "moov" has not ended by then: no length to read

_MPEG_BITRATES = {  # kbit/s by (version, layer): index 1-14
    (1, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MPEG_RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 25: (11025, 12000, 8000)}
_UNKNOWN_DURATION = {0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}
_TO_THE_END = 1 << 62  # a box of size 0 runs to the file's end: never all in a head
# Formats known not to tell their length in their first bytes (decided at once).
_OTHERS = (b"RIFF", b"FORM", b"caff", b"wvpk", b"\x1a\x45\xdf\xa3", b"MAC ")


def audio_length(head: bytes, total: int | None) -> float | None:
    """Seconds of audio, from the first bytes ``head`` of a file of ``total`` bytes (when
    known); None when they do not tell."""
    start = _id3_size(head)
    if start is None:
        return None
    data = head[start:]
    if data[:4] == b"fLaC":
        return _flac(data)
    if data[4:8] == b"ftyp":
        return _mp4(data)
    return _mp3(head, start, total)


def complete(head: bytes) -> bool:
    """Whether ``head`` holds enough to decide (a length, or that there is none to read)."""
    start = _id3_size(head)
    if start is None:
        return False
    data = head[start:]
    if len(data) < 12:
        return False
    if data[:4] == b"fLaC":
        return len(data) >= 42
    if data[4:8] == b"ftyp":
        return _mp4_decided(data)
    if data[:4] == b"OggS":  # no length; its first packet tells its codec
        return len(data) >= 27 and len(data) >= 27 + data[26] + 8
    if data[:4] in _OTHERS:
        return True
    at = _after_padding(data, 0)
    if at is None:
        return len(data) >= 4096
    if not _sync(data, at):
        return True  # not MPEG audio: nothing to read
    return len(data) >= at + MP3_BYTES


async def peek(
    body: AsyncIterator[bytes], limit: int = PEEK_BYTES
) -> tuple[bytes, AsyncIterator[bytes], bool]:
    """The first bytes of ``body`` (enough to read a length, or ``limit``), the whole body
    again (those bytes included), and whether the body ended within them."""
    taken: list[bytes] = []
    head = bytearray()
    ended = False
    while not complete(bytes(head)) and len(head) < limit:
        try:
            chunk = await body.__anext__()
        except StopAsyncIteration:
            ended = True
            break
        taken.append(chunk)
        head += chunk

    async def again() -> AsyncIterator[bytes]:
        for chunk in taken:
            yield chunk
        if not ended:
            async for chunk in body:
                yield chunk

    return bytes(head), again(), ended


def _id3_size(head: bytes) -> int | None:
    """Where the audio starts after an ID3v2 tag (0 without one); None when the tag's
    header is not all there yet."""
    if head[:3] != b"ID3":
        return 0 if len(head) >= 3 else None
    if len(head) < 10:
        return None
    size = 0
    for byte in head[6:10]:
        size = (size << 7) | (byte & 0x7F)
    footer = 10 if head[5] & 0x10 else 0
    return 10 + size + footer


def _flac(data: bytes) -> float | None:
    if len(data) < 42 or data[4] & 0x7F != 0:  # STREAMINFO comes first
        return None
    packed = int.from_bytes(data[18:26], "big")
    rate = packed >> 44
    samples = packed & ((1 << 36) - 1)
    if rate <= 0 or samples <= 0:  # an unknown total
        return None
    return samples / rate


def _boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """MP4 boxes (type, payload start, box end) between ``start`` and ``end``, as far as
    their headers are in ``data`` (a box may end beyond it)."""
    found = []
    at = start
    while at + 8 <= min(end, len(data)):
        size = int.from_bytes(data[at : at + 4], "big")
        kind = data[at + 4 : at + 8]
        header = 8
        if size == 1:
            if at + 16 > len(data):
                break
            size, header = int.from_bytes(data[at + 8 : at + 16], "big"), 16
        elif size == 0:  # to the end of the file (a streamed MP4): never complete here
            size = _TO_THE_END
        if size < header:
            break
        found.append((kind, at + header, at + size))
        at += size
    return found


def _full_box(data: bytes, payload: int, v0: int, v1: int) -> tuple[int, int] | None:
    """(version, the payload offset of its fields) of a full box, when its fields are in
    ``data`` (``v0``/``v1``: the bytes its fields take in version 0 and 1)."""
    if payload >= len(data):
        return None
    version = data[payload]
    if payload + 4 + (v1 if version == 1 else v0) > len(data):
        return None
    return version, payload + 4


def _mp4(data: bytes) -> float | None:
    for kind, payload, end in _boxes(data, 0, len(data)):
        if kind in (b"mdat", b"moof"):
            return None  # the audio before the index: nothing to read
        if kind != b"moov":
            continue
        if end > len(data):
            return None  # the whole "moov" is needed to know it is not fragmented
        boxes = _boxes(data, payload, end)
        if any(e >= _TO_THE_END for _, _, e in boxes):
            return None  # a box inside of no stated size: its "mvex" could hide behind it
        inner = {k: (p, e) for k, p, e in boxes}
        if b"mvhd" not in inner:
            return None
        fields = _full_box(data, inner[b"mvhd"][0], 16, 28)
        if fields is None:
            return None
        version, at = fields
        if version == 1:
            scale = int.from_bytes(data[at + 16 : at + 20], "big")
            duration = int.from_bytes(data[at + 20 : at + 28], "big")
        else:
            scale = int.from_bytes(data[at + 8 : at + 12], "big")
            duration = int.from_bytes(data[at + 12 : at + 16], "big")
        if b"mvex" in inner:  # fragmented: only "mehd" tells the whole length
            mvex = inner[b"mvex"]
            mehd = {k: p for k, p, _ in _boxes(data, mvex[0], mvex[1])}.get(b"mehd")
            found = None if mehd is None else _full_box(data, mehd, 4, 8)
            if found is None:
                return None
            version, at = found
            size = 8 if version == 1 else 4
            duration = int.from_bytes(data[at : at + size], "big")
        if scale <= 0 or duration in _UNKNOWN_DURATION:
            return None
        return duration / scale
    return None


def _mp4_decided(data: bytes) -> bool:
    for kind, _, end in _boxes(data, 0, len(data)):
        if kind in (b"mdat", b"moof"):
            return True
        if kind == b"moov":
            return end <= len(data) or end >= _TO_THE_END or len(data) >= MP4_BYTES
    return len(data) >= MP4_BYTES


def _after_padding(data: bytes, start: int) -> int | None:
    """The first byte after zero padding (some files pad after their tag), within 4 KB."""
    at = start
    while at < len(data) and data[at] == 0:
        at += 1
        if at - start > 4096:
            return None
    return at if at < len(data) else None


def _sync(data: bytes, at: int) -> bool:
    return at + 1 < len(data) and data[at] == 0xFF and data[at + 1] & 0xE0 == 0xE0


def _frame(data: bytes, at: int) -> tuple[int, int, int, int, bool, int] | None:
    """An MPEG audio frame header at ``at``: (version, layer, bitrate, sample rate, mono,
    frame length); None when there is none."""
    if at + 4 > len(data) or not _sync(data, at):
        return None
    b1, b2, b3 = data[at + 1], data[at + 2], data[at + 3]
    version = {3: 1, 2: 2, 0: 25}.get((b1 >> 3) & 0x03)
    layer = {3: 1, 2: 2, 1: 3}.get((b1 >> 1) & 0x03)
    rate_index, bitrate_index = (b2 >> 2) & 0x03, (b2 >> 4) & 0x0F
    if version is None or layer is None or rate_index == 3 or bitrate_index in (0, 15):
        return None
    rate = _MPEG_RATES[version][rate_index]
    table = _MPEG_BITRATES[(1, layer)] if version == 1 else _MPEG_BITRATES[(2, min(layer, 2))]
    bitrate = table[bitrate_index - 1] * 1000
    padding = (b2 >> 1) & 0x01
    if layer == 1:
        length = (12 * bitrate // rate + padding) * 4
    elif layer == 3 and version != 1:
        length = 72 * bitrate // rate + padding
    else:
        length = 144 * bitrate // rate + padding
    return version, layer, bitrate, rate, (b3 >> 6) & 0x03 == 3, length


def _mp3(head: bytes, start: int, total: int | None) -> float | None:
    """Only a frame right where the audio starts (after zero padding): other data is not
    searched for something that looks like a frame. Only its frame count tells the length
    (``total`` is not used: a size would count trailing tags as audio)."""
    at = _after_padding(head, start)
    if at is None or len(head) < at + MP3_BYTES:
        return None  # its first frames are not all here: no guess
    first = _frame(head, at)
    if first is None:
        return None
    version, layer, _, rate, mono, _ = first
    samples = 384 if layer == 1 else 1152 if layer == 2 or version == 1 else 576
    side = (17 if mono else 32) if version == 1 else (9 if mono else 17)
    marker = head[at + 4 + side : at + 8 + side]
    if marker in (b"Xing", b"Info"):
        flags = int.from_bytes(head[at + 8 + side : at + 12 + side], "big")
        if not flags & 1:
            return None  # no frame count: not decided
        frames = int.from_bytes(head[at + 12 + side : at + 16 + side], "big")
        return frames * samples / rate if frames else None
    if head[at + 36 : at + 40] == b"VBRI":
        frames = int.from_bytes(head[at + 50 : at + 54], "big")
        return frames * samples / rate if frames else None
    return None  # no frame count


def audio_kind(
    head: bytes, total: int | None, length: float | None
) -> tuple[str, int | None] | None:
    """The delivered audio's format - as Navidrome names a file of it (its extension once
    delivered: "flac", "mp3", "m4a", "opus", "ogg") - and its bitrate in kbit/s as Navidrome
    reads it from the file (TagLib), when the first bytes ``head`` tell. The bitrate
    of FLAC is counted from the size (``total``) and the length, tags included: never less
    than Navidrome's (a request it may not meet is converted, as before); an MP4's, Opus's
    and Vorbis's is not read (None: a bitrate limit is never taken as met). None when the
    head does not tell the format."""
    start = _id3_size(head)
    if start is None:
        return None
    data = head[start:]
    if data[:4] == b"fLaC":
        return "flac", _by_size(total, length)
    if data[4:8] == b"ftyp":
        return "m4a", None  # its stated rate may be Navidrome's: not read, never met by a limit
    if data[:4] == b"OggS":
        if len(data) < 36:
            return None
        packet = data[27 + data[26] :] if len(data) > 27 + data[26] else b""
        if packet[:8] == b"OpusHead":
            return "opus", None
        if packet[:7] == b"\x01vorbis":
            return "ogg", None
        return None
    if data[:4] in _OTHERS:
        return None
    return _mp3_kind(head, start)


def _by_size(total: int | None, length: float | None) -> int | None:
    if not total or not length or length <= 0:
        return None
    return math.ceil(total * 8 / length / 1000)


def _mp3_kind(head: bytes, start: int) -> tuple[str, int | None] | None:
    """An MP3's bitrate as TagLib reads it (Navidrome's): from a Xing/Info header anywhere in
    its first frame with both its frame and byte counts, else a VBRI header there, else its
    first frame's own (a constant bitrate)."""
    at = _after_padding(head, start)
    if at is None or len(head) < at + MP3_BYTES:
        return None
    first = _frame(head, at)
    if first is None:
        return None
    version, layer, bitrate, rate, _, length = first
    if len(head) < at + length:
        return None  # its whole first frame is needed to look for the header
    frame = head[at : at + length]
    samples = 384 if layer == 1 else 1152 if layer == 2 or version == 1 else 576
    frames = size = 0
    offset = frame.find(b"Xing")
    if offset < 0:
        offset = frame.find(b"Info")
    if offset >= 0:
        if len(frame) >= offset + 16 and frame[offset + 7] & 0x03 == 0x03:
            frames = int.from_bytes(frame[offset + 8 : offset + 12], "big")
            size = int.from_bytes(frame[offset + 12 : offset + 16], "big")
    elif (offset := frame.find(b"VBRI")) >= 0 and len(frame) >= offset + 32:
        size = int.from_bytes(frame[offset + 10 : offset + 14], "big")
        frames = int.from_bytes(frame[offset + 14 : offset + 18], "big")
    if frames and size:
        milliseconds = frames * samples * 1000 / rate
        return "mp3", int(size * 8 / milliseconds + 0.5)
    return "mp3", bitrate // 1000
