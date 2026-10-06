"""The length of delivered audio from its first bytes (``delivery/length.py``): read
where the first bytes tell it, and None - never a wrong length, which would reject a
correct recording - wherever they do not."""

from __future__ import annotations

import io
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from mutagen.mp3 import MP3

from shijhon.delivery.length import PEEK_BYTES, audio_kind, audio_length, complete, peek
from tests.harness.library import tone


def head_of(data: bytes, step: int = 16384) -> bytes:
    """The first bytes as ``peek`` takes them: chunk by chunk until they decide."""
    head = b""
    while not complete(head) and len(head) < PEEK_BYTES and len(head) < len(data):
        head += data[len(head) : len(head) + step]
    return head


def made(tmp: Path, name: str, *args: str, seconds: int = 9) -> bytes:
    out = tmp / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=330:duration={seconds}", *args, str(out)],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


def length(data: bytes) -> float | None:
    return audio_length(head_of(data), len(data))


@pytest.mark.parametrize("fmt", ["flac", "mp3"])
def test_the_length_of_flac_and_mp3(fmt: str) -> None:
    for seconds in (3, 12):
        data = tone(440, seconds, fmt).read_bytes()  # type: ignore[arg-type]
        assert abs((length(data) or 0) - seconds) < 0.1, (fmt, seconds)


def test_mp4_with_its_index_first_and_last(tmp_path: Path) -> None:
    fast = made(tmp_path, "fast.m4a", "-c:a", "aac", "-movflags", "+faststart")
    assert abs((length(fast) or 0) - 9) < 0.1
    last = tone(440, 3, "m4a").read_bytes()  # the audio before its index
    assert last.find(b"mdat") < last.find(b"moov")
    assert length(last) is None


def test_fragmented_mp4_has_no_length_to_read(tmp_path: Path) -> None:
    """Its "moov" says 0 (a DASH-style file): read as 0 s it rejected every correct
    recording in it."""
    for movflags in ("frag_keyframe+empty_moov", "+dash+frag_keyframe+empty_moov"):
        data = made(tmp_path, "frag.m4a", "-c:a", "aac", "-movflags", movflags)
        assert length(data) is None, movflags


def test_a_streamed_mp4_whose_boxes_have_no_size(tmp_path: Path) -> None:
    """An MP4 written to a pipe (an add-on transcoding on the fly) with a "moov" too large
    for the muxer's buffer keeps boxes of size 0: its first fragment's length (250 s of
    600 s) must not be read as the file's."""
    streamed = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
         "sine=frequency=330:duration=600", "-c:a", "aac", "-f", "mp4", "-movflags",
         "frag_keyframe", "-frag_size", "4000000", "pipe:1"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    assert length(streamed) is None


def test_mp4_behind_an_id3_tag(tmp_path: Path) -> None:
    fast = made(tmp_path, "fast.m4a", "-c:a", "aac", "-movflags", "+faststart")
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x20" + b"\x00" * 32
    assert abs((length(tag + fast) or 0) - 9) < 0.1


def test_mp3_without_a_frame_count(tmp_path: Path) -> None:
    """Only a Xing/Info or VBRI frame count tells an MP3's length: a VBR MP3 without one read
    by its first frame's bitrate was 50 s for 240 s, and a size-based guess for constant
    bitrate counts trailing tags or artwork as audio (a correct file rejected)."""
    vbr = made(tmp_path, "vbr.mp3", "-q:a", "5", "-write_xing", "0", seconds=30)
    assert length(vbr) is None
    cbr = made(tmp_path, "cbr.mp3", "-b:a", "128k", "-write_xing", "0", seconds=30)
    assert length(cbr) is None
    trailing = cbr + b"APETAGEX" + b"\x00" * 1_000_000  # an APE tag with a cover, at the end
    assert length(trailing) is None
    for name, args in (("xing.mp3", ("-q:a", "5")), ("info.mp3", ("-b:a", "128k"))):
        with_header = made(tmp_path, name, *args, seconds=30)
        assert abs((length(with_header) or 0) - 30) < 0.3, name


def test_mp3_behind_a_large_id3_tag_and_a_head_cut_short(tmp_path: Path) -> None:
    body = made(tmp_path, "vbr.mp3", "-q:a", "5")
    if body[:3] == b"ID3":  # ffmpeg's own small tag: replaced by a large one (a cover)
        own = 0
        for byte in body[6:10]:
            own = (own << 7) | (byte & 0x7F)
        body = body[10 + own :]
    size = 100_000
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    tagged = b"ID3\x04\x00\x00" + syncsafe + b"\x00" * size + body
    assert not complete(tagged[:50_000])  # still inside the tag
    assert abs((length(tagged) or 0) - 9) < 0.1
    # A client's range that ends right after the tag: no guess from what is there.
    assert audio_length(tagged[: size + 10 + 600], len(tagged)) is None


def test_other_formats_and_short_heads(tmp_path: Path) -> None:
    ogg = made(tmp_path, "tone.ogg", "-c:a", "libopus")
    assert complete(ogg[:64]) and length(ogg) is None
    wav = made(tmp_path, "tone.wav")
    assert length(wav) is None
    assert audio_length(b"fL", None) is None and not complete(b"fL")
    empty = tone(440, 3, "flac").read_bytes()
    streaminfo = bytearray(empty)
    streaminfo[21:26] = bytes([streaminfo[21] & 0xF0, 0, 0, 0, 0])  # an unknown total
    assert length(bytes(streaminfo)) is None


def test_peek_gives_back_every_byte() -> None:
    chunks = [b"fLaC", b"x" * 100, b"y" * 5000, b"z" * 7]

    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    async def run() -> tuple[bytes, bytes, bool]:
        head, again, ended = await peek(body())
        rest = b"".join([c async for c in again])
        return head, rest, ended

    head, rest, ended = anyio.run(run)
    assert head.startswith(b"fLaC") and len(head) >= 42 and not ended
    assert rest == b"".join(chunks)

    async def short() -> AsyncIterator[bytes]:
        yield b"fL"

    async def run_short() -> tuple[bytes, bool]:
        _, again, ended = await peek(short())
        return b"".join([c async for c in again]), ended

    assert anyio.run(run_short) == (b"fL", True)


def test_a_widened_answer_is_cut_back_to_what_the_source_sent() -> None:
    """A probe widened to the first 512 KB and cut back to the client's range: never past
    the bytes the source's answer holds; a 200 (its total unknown) goes as sent."""
    from shijhon.delivery.playback import ByteRange, Opened, _narrowed

    async def body() -> AsyncIterator[bytes]:
        yield b"x" * 65536

    async def close() -> None:
        return None

    asked, requested = ByteRange(0, PEEK_BYTES - 1), ByteRange(0, 100_000)
    short = Opened(206, [(b"content-range", b"bytes 0-65535/900000")], body(), close)
    cut = _narrowed(short, asked, requested, 900_000)
    headers = dict(cut.headers)
    assert headers[b"content-range"] == b"bytes 0-65535/900000"
    assert headers[b"content-length"] == b"65536"
    whole = Opened(200, [(b"content-type", b"audio/flac")], body(), close)
    assert _narrowed(whole, asked, requested, None) is whole


# --- the delivered audio's format and bitrate --------------------------------------


def kind(data: bytes) -> tuple[str, int | None] | None:
    head = head_of(data)
    return audio_kind(head, len(data), audio_length(head, len(data)))


def test_the_format_as_navidrome_names_a_delivered_file(tmp_path: Path) -> None:
    assert kind(tone(440, 3, "flac").read_bytes())[0] == "flac"  # type: ignore[index]
    assert kind(made(tmp_path, "a.m4a", "-c:a", "aac", "-movflags", "+faststart")) == ("m4a", None)
    assert kind(made(tmp_path, "a.opus", "-c:a", "libopus")) == ("opus", None)
    # An Ogg page holding a Vorbis identification header (this ffmpeg may lack the encoder).
    page = b"OggS" + bytes(22) + b"\x01\x1e" + b"\x01vorbis" + bytes(23)
    assert kind(page + bytes(4096)) == ("ogg", None)
    # Its first bytes coming in small pieces: read until the codec shows.
    split = head_of(page + bytes(4096), step=12)
    assert audio_kind(split, None, None) == ("ogg", None)
    assert kind(made(tmp_path, "a.wav", "-c:a", "pcm_s16le")) is None
    assert kind(b"x" * 5000) is None  # not audio


@pytest.mark.parametrize("bitrate", [128, 192, 320])
def test_a_constant_bitrate_mp3_s_bitrate(tmp_path: Path, bitrate: int) -> None:
    """As TagLib reads it (Navidrome's bitRate): from its Info header's byte and frame
    counts, or its first frame."""
    data = made(tmp_path, f"{bitrate}.mp3", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
                seconds=30)  # fmt: skip
    assert kind(data) == ("mp3", bitrate)
    bare = made(tmp_path, f"{bitrate}-bare.mp3", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k",
                "-write_xing", "0", seconds=30)  # fmt: skip
    assert kind(bare) == ("mp3", bitrate)  # no Info header: its first frame's


def test_a_variable_bitrate_mp3_s_bitrate(tmp_path: Path) -> None:
    data = made(tmp_path, "vbr.mp3", "-c:a", "libmp3lame", "-q:a", "0", seconds=30)
    found = kind(data)
    assert found is not None and found[0] == "mp3" and found[1] is not None
    assert abs(found[1] - MP3(io.BytesIO(data)).info.bitrate / 1000) <= 2


def test_a_flac_s_bitrate_is_never_below_its_audio_s(tmp_path: Path) -> None:
    data = tone(440, 12, "flac").read_bytes()
    found = kind(data)
    assert found is not None and found[1] is not None
    assert found[1] >= len(data) * 8 / 12 / 1000
