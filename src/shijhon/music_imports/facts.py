"""Bounded facts about a received file, read in the probe's subprocess.

Its tags as Navidrome reads them (the album-level tags through Navidrome's aliases,
``placeholders/tags.py``), the numbers and identifiers that tell recordings apart, and its
audio quality. Every text is capped; nothing is logged. The review compares these facts
with the library (``overlap.py``); they never change the file.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from shijhon.music_imports import tagmap
from shijhon.placeholders.tags import ALBUM_TAGS, mapped
from shijhon.quality import LOSSLESS_CODECS, LOSSLESS_FORMATS

MAX_TEXT = 300
MAX_VALUES = 5
MAX_PICTURE = 16 * 1024**2
_NUMBER = re.compile(r"\s*(\d{1,4})")
_YEAR = re.compile(r"(\d{4})")
_ISRC = ("tsrc", "isrc", "----:com.apple.itunes:isrc")
_RECORDING = (
    "txxx:musicbrainz track id",
    "musicbrainz_trackid",
    "musicbrainz track id",
    "----:com.apple.itunes:musicbrainz track id",
    "musicbrainz/track id",
)
_ADVISORY = ("itunesadvisory", "txxx:itunesadvisory", "rtng")
# ffprobe's tags (Matroska, Shorten): title, artist, album, album artist, track, disc,
# ISRC, recording ID.
_PLAIN_KEYS = ("title", "artist", "album", "album_artist", "track", "disc", "isrc",
               "musicbrainz_trackid")  # fmt: skip


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).strip()[:MAX_TEXT]


def _texts(values: Any) -> list[str]:
    values = values if isinstance(values, list | tuple) else [values]
    out = [_text(v) for v in values if _text(v)]
    return list(dict.fromkeys(out))[:MAX_VALUES]


def _number(value: Any) -> int | None:
    if isinstance(value, tuple):
        value = value[0]
    match = _NUMBER.match(_text(value))
    number = int(match.group(1)) if match else 0
    return number or None


def year(album_tags: dict[str, list[str]]) -> int | None:
    for name in ("releasedate", "recordingdate", "originaldate"):
        for value in album_tags.get(name, []):
            found = _YEAR.search(value)
            if found and 1000 <= int(found.group(1)) <= 2999:
                return int(found.group(1))
    return None


def _first(raw: dict[str, list[str]], *keys: str) -> str:
    values = mapped(raw, keys)
    return values[0] if values else ""


def _plain(tags: Any) -> dict[str, Any]:
    """Facts from ffprobe's tags (formats Shijhon cannot write): the identifying basics
    only, never copied tags."""
    keys = _PLAIN_KEYS
    found: dict[str, list[str]] = {}
    if tags is not None:
        try:
            items = tags.items() if hasattr(tags, "items") else list(tags)
        except Exception:
            items = []
        lowered: dict[str, list[str]] = {}
        for key, value in items:
            values = value if isinstance(value, list) else [value]
            texts = [_text(getattr(v, "value", v)) for v in values]
            lowered.setdefault(str(key).casefold(), []).extend(t for t in texts if t)
        found = {key: lowered.get(key.casefold(), []) for key in keys}

    def first(index: int) -> str:
        values = found.get(keys[index], [])
        return values[0] if values else ""

    return {
        "title": first(0),
        "artist": first(1),
        "album": first(2),
        "albumartist": first(3),
        "track": _number(first(4)),
        "disc": _number(first(5)),
        "isrcs": _texts(found.get(keys[6], [])),
        "recording_id": first(7),
        "clean": None,
        "album_tags": {},
        "taggable": False,
    }


def _picture(parsed: Any) -> bytes | None:
    tags = getattr(parsed, "tags", None)
    try:
        if isinstance(parsed, FLAC) and parsed.pictures:
            return bytes(parsed.pictures[0].data)
        if isinstance(tags, ID3):
            frames = tags.getall("APIC")
            return bytes(frames[0].data) if frames else None
        if isinstance(parsed, MP4) and tags is not None and tags.get("covr"):
            return bytes(tags["covr"][0])
        if isinstance(parsed, (OggVorbis, OggOpus, OggFLAC)) and tags is not None:
            blocks = tags.get("metadata_block_picture") or []
            if blocks:
                return bytes(Picture(base64.b64decode(blocks[0])).data)
        if isinstance(parsed, ASF) and tags is not None and tags.get("WM/Picture"):
            raw = bytes(tags["WM/Picture"][0].value)
            # WM/Picture: type, size, MIME and description strings, then the data.
            start = raw.find(b"\0\0", 5 + 2)
            start = raw.find(b"\0\0", start + 2) if start >= 0 else -1
            return raw[start + 2 :] if start >= 0 else None
        if tags is not None and hasattr(tags, "get") and tags.get("Cover Art (Front)"):
            raw = bytes(tags["Cover Art (Front)"].value)
            return raw.split(b"\0", 1)[1] if b"\0" in raw else None
    except Exception:
        return None
    return None


def picture(parsed: Any) -> bytes | None:
    data = _picture(parsed)
    return data if data and len(data) <= MAX_PICTURE else None


def _recording_id(tags: Any, raw: dict[str, list[str]]) -> str:
    found = _first(raw, *_RECORDING)
    if found or not isinstance(tags, ID3):
        return found
    for frame in tags.getall("UFID"):
        if frame.owner == "http://musicbrainz.org":
            return _text(frame.data)
    return ""


def _clean(raw: dict[str, list[str]]) -> bool | None:
    value = _first(raw, *_ADVISORY)
    return {"1": False, "4": False, "2": True}.get(value.strip())


def _numbers(parsed: Any, raw: dict[str, list[str]]) -> tuple[int | None, int | None]:
    if isinstance(parsed, MP4) and parsed.tags is not None:
        track = parsed.tags.get("trkn")
        disc = parsed.tags.get("disk")
        return (_number(track[0]) if track else None, _number(disc[0]) if disc else None)
    return (
        _number(_first(raw, "tracknumber", "trck", "track")),
        _number(_first(raw, "discnumber", "tpos", "disc")),
    )


def quality(parsed: Any, fmt: str, sample_rate: int, codec: str = "") -> dict[str, Any]:
    info = getattr(parsed, "info", None)
    codec = (codec or _text(getattr(info, "codec", "") or getattr(info, "codec_name", ""))).lower()
    bitrate = int(getattr(info, "bitrate", 0) or 0)
    depth = int(getattr(info, "bits_per_sample", 0) or 0)
    lossless = (
        fmt in LOSSLESS_FORMATS
        or isinstance(parsed, OggFLAC)
        or codec in LOSSLESS_CODECS
        or codec.startswith("pcm")
        or (isinstance(parsed, MP4) and codec == "alac")
    )
    return {
        "format": fmt,
        "codec": codec[:40],
        "lossless": lossless,
        "kbps": round(bitrate / 1000) if bitrate > 0 else 0,
        "sample_rate": int(sample_rate or 0),
        "bit_depth": depth if lossless else 0,
    }


def of_mutagen(
    parsed: Any, fmt: str, length: float | None, sample_rate: int, tags: Any = None
) -> dict[str, Any]:
    """``tags``: the file's tags when they are not ``parsed.tags`` (raw AAC's ID3)."""
    tags = tags if tags is not None else getattr(parsed, "tags", None)
    writable = bool(tagmap.family(parsed))
    raw = tagmap.raw(parsed, tags) if writable else None
    if raw is not None:
        album_tags = {
            name: mapped(raw, aliases)[:MAX_VALUES] for name, aliases in ALBUM_TAGS.items()
        }
        album_tags = {k: [_text(v) for v in vs] for k, vs in album_tags.items() if vs}
        track, disc = _numbers(parsed, raw)
        facts: dict[str, Any] = {
            "title": _first(raw, "title", "tit2", "©nam"),
            "artist": _first(raw, "artist", "tpe1", "©art"),
            "album": _first(raw, "album", "talb", "©alb"),
            "albumartist": (album_tags.get("albumartist") or [""])[0],
            "track": track,
            "disc": disc,
            "isrcs": _texts(mapped(raw, _ISRC)),
            "recording_id": _recording_id(tags, raw),
            "clean": _clean(raw),
            "album_tags": album_tags,
            # A copy of it can carry an album's tags (``retag.py``, ``tagmap.py``).
            "taggable": True,
        }
    else:
        facts = _plain(None)
    facts = {k: _text(v) if isinstance(v, str) else v for k, v in facts.items()}
    facts["duration_ms"] = int((length or 0) * 1000)
    facts["quality"] = quality(parsed, fmt, sample_rate)
    facts["picture"] = picture(parsed) is not None
    return facts


def of_ffprobe(tags: dict[str, Any], fmt: str, length: float | None, sample_rate: int,
               codec: str) -> dict[str, Any]:  # fmt: skip
    """Matroska and Shorten. Matroska's tags are read as Navidrome reads them, and a
    copy can carry an album's (``tagmap.matroska``); Shorten has no tags."""
    facts = _plain(list(tags.items()))
    if fmt == "mka":
        raw = tagmap.matroska_raw(tags)
        album_tags = {
            name: mapped(raw, aliases)[:MAX_VALUES] for name, aliases in ALBUM_TAGS.items()
        }
        facts["album_tags"] = {k: [_text(v) for v in vs] for k, vs in album_tags.items() if vs}
        facts["albumartist"] = (facts["album_tags"].get("albumartist") or [""])[0]
        facts["taggable"] = True
    facts["duration_ms"] = int((length or 0) * 1000)
    facts["quality"] = quality(None, fmt, sample_rate, codec)
    facts["picture"] = False
    return facts
