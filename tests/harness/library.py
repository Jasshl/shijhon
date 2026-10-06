"""Synthetic owned music: albums declared in code, audio generated with ffmpeg.

Every track gets its own tone, so tests can tell whose bytes were played. Tags are written
here directly with mutagen, independently of Shijhon's own tag code, in the styles real
libraries use (an original date beside a later one, version tags, MusicBrainz IDs, multiple
album artists, compilations) for FLAC, MP3 and M4A.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3,
    TALB,
    TCMP,
    TCON,
    TDOR,
    TDRC,
    TDRL,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TSRC,
    TXXX,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm

from tests.harness.paths import cache_dir, file_lock

Format = Literal["flac", "mp3", "m4a"]
_CODECS = {
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
    "m4a": ["-c:a", "aac", "-b:a", "128k"],
}


@dataclass(frozen=True)
class Track:
    title: str
    number: int
    disc: int = 1
    seconds: float = 3.0
    artists: tuple[str, ...] = ()  # default: the album artist
    isrc: str | None = None


@dataclass(frozen=True)
class Album:
    artist: str
    title: str
    tracks: tuple[Track, ...]
    fmt: Format = "flac"
    album_artists: tuple[str, ...] = ()  # multi-valued ALBUMARTISTS besides ALBUMARTIST
    recording_date: str | None = "2020"  # FLAC DATE, MP3 TDRC (M4A has none)
    release_date: str | None = None  # FLAC RELEASEDATE, MP3 TDRL, M4A ©day
    original_date: str | None = None  # FLAC ORIGINALDATE, MP3 TDOR, M4A freeform
    version: str | None = None  # ALBUMVERSION
    mb_album_id: str | None = None
    compilation: bool = False
    genre: str | None = None
    release_type: str | None = None
    cover_file: bool = False  # cover.jpg in the album folder
    embedded_cover: bool = False
    folder: str | None = None  # relative to the library root
    extra: dict[str, list[str]] = field(default_factory=dict)  # raw keys, format-specific
    write_album_artist: bool = True  # False: no ALBUMARTIST tag at all
    # Track total (per disc) and disc total, written only when given ("3/12" in MP3 and M4A).
    track_total: int | None = None
    disc_total: int | None = None

    @property
    def relative_folder(self) -> str:
        return self.folder or f"{_safe(self.artist)}/{_safe(self.title)}"


def _safe(name: str) -> str:
    return "".join("_" if ch in '/\\:*?"<>|' else ch for ch in name)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


def tone(frequency: int, seconds: float, fmt: Format) -> Path:
    """A cached sine tone; callers copy it before tagging."""
    folder = cache_dir() / "test-audio"
    path = folder / f"tone-{frequency}-{seconds:g}.{fmt}"
    if path.exists():
        return path
    with file_lock(folder / ".lock"):
        if not path.exists():
            partial = folder / f"partial-{uuid.uuid4().hex}.{fmt}"
            _ffmpeg(
                "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100",
                "-ac", "2", "-t", f"{seconds:g}", "-map_metadata", "-1",
                *_CODECS[fmt], str(partial),
            )  # fmt: skip
            partial.replace(path)
    return path


def cover_image(color: str = "red") -> Path:
    folder = cache_dir() / "test-audio"
    path = folder / f"cover-{color}.jpg"
    if not path.exists():
        with file_lock(folder / ".lock"):
            if not path.exists():
                partial = folder / f"partial-{uuid.uuid4().hex}.jpg"
                _ffmpeg(
                    "-f", "lavfi", "-i", f"color=c={color}:s=64x64",
                    "-frames:v", "1", str(partial),
                )  # fmt: skip
                partial.replace(path)
    return path


def frequency_for(*parts: object) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return 200 + int.from_bytes(digest[:2], "big") % 1800


def write_album(music_root: Path, album: Album) -> list[Path]:
    folder = music_root / album.relative_folder
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for track in album.tracks:
        freq = frequency_for(album.artist, album.title, track.disc, track.number)
        path = folder / f"{track.disc}-{track.number:02d} {_safe(track.title)}.{album.fmt}"
        shutil.copyfile(tone(freq, track.seconds, album.fmt), path)
        {"flac": _tag_flac, "mp3": _tag_mp3, "m4a": _tag_m4a}[album.fmt](path, album, track)
        paths.append(path)
    if album.cover_file:
        shutil.copyfile(cover_image(), folder / "cover.jpg")
    return paths


def _tag_flac(path: Path, album: Album, track: Track) -> None:
    audio = FLAC(path)
    tags: dict[str, list[str]] = {
        "title": [track.title],
        "artist": list(track.artists or (album.artist,)),
        "album": [album.title],
        "tracknumber": [str(track.number)],
        "discnumber": [str(track.disc)],
    }
    optional = {
        "tracktotal": [str(album.track_total)] if album.track_total else [],
        "disctotal": [str(album.disc_total)] if album.disc_total else [],
        "albumartist": [album.artist] if album.write_album_artist else [],
        "albumartists": list(album.album_artists),
        "date": [album.recording_date] if album.recording_date else [],
        "releasedate": [album.release_date] if album.release_date else [],
        "originaldate": [album.original_date] if album.original_date else [],
        "albumversion": [album.version] if album.version else [],
        "musicbrainz_albumid": [album.mb_album_id] if album.mb_album_id else [],
        "compilation": ["1"] if album.compilation else [],
        "genre": [album.genre] if album.genre else [],
        "releasetype": [album.release_type] if album.release_type else [],
        "isrc": [track.isrc] if track.isrc else [],
    }
    tags.update({k: v for k, v in optional.items() if v})
    tags.update(album.extra)
    for key, values in tags.items():
        audio[key] = values
    if album.embedded_cover:
        picture = Picture()
        picture.type, picture.mime = 3, "image/jpeg"
        picture.data = cover_image().read_bytes()
        audio.add_picture(picture)
    audio.save()


def _tag_mp3(path: Path, album: Album, track: Track) -> None:
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags.add(TIT2(encoding=3, text=[track.title]))
    tags.add(TPE1(encoding=3, text=list(track.artists or (album.artist,))))
    tags.add(TALB(encoding=3, text=[album.title]))
    if album.write_album_artist:
        tags.add(TPE2(encoding=3, text=[album.artist]))
    tags.add(TRCK(encoding=3, text=[_of(track.number, album.track_total)]))
    tags.add(TPOS(encoding=3, text=[_of(track.disc, album.disc_total)]))
    if album.album_artists:
        tags.add(TXXX(encoding=3, desc="ALBUMARTISTS", text=list(album.album_artists)))
    if album.recording_date:
        tags.add(TDRC(encoding=3, text=[album.recording_date]))
    if album.release_date:
        tags.add(TDRL(encoding=3, text=[album.release_date]))
    if album.original_date:
        tags.add(TDOR(encoding=3, text=[album.original_date]))
    if album.version:
        tags.add(TXXX(encoding=3, desc="ALBUMVERSION", text=[album.version]))
    if album.mb_album_id:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=[album.mb_album_id]))
    if album.compilation:
        tags.add(TCMP(encoding=3, text=["1"]))
    if album.genre:
        tags.add(TCON(encoding=3, text=[album.genre]))
    if album.release_type:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Album Type", text=[album.release_type]))
    if track.isrc:
        tags.add(TSRC(encoding=3, text=[track.isrc]))
    for key, values in album.extra.items():
        tags.add(TXXX(encoding=3, desc=key, text=values))
    audio.save(v2_version=4)


def _of(number: int, total: int | None) -> str:
    return f"{number}/{total}" if total else str(number)


def _freeform(values: list[str]) -> list[MP4FreeForm]:
    return [MP4FreeForm(v.encode()) for v in values]


def _tag_m4a(path: Path, album: Album, track: Track) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags["\xa9nam"] = [track.title]
    tags["\xa9ART"] = list(track.artists or (album.artist,))
    tags["\xa9alb"] = [album.title]
    if album.write_album_artist:
        tags["aART"] = [album.artist]
    tags["trkn"] = [(track.number, album.track_total or 0)]
    tags["disk"] = [(track.disc, album.disc_total or 0)]
    day = album.release_date or album.recording_date
    if day:
        tags["\xa9day"] = [day]
    itunes = "----:com.apple.iTunes:"
    if album.album_artists:
        tags[itunes + "ALBUMARTISTS"] = _freeform(list(album.album_artists))
    if album.original_date:
        tags[itunes + "ORIGINALDATE"] = _freeform([album.original_date])
    if album.version:
        tags[itunes + "ALBUMVERSION"] = _freeform([album.version])
    if album.mb_album_id:
        tags[itunes + "MusicBrainz Album Id"] = _freeform([album.mb_album_id])
    if album.compilation:
        tags["cpil"] = True
    if album.genre:
        tags["\xa9gen"] = [album.genre]
    if album.release_type:
        tags[itunes + "MusicBrainz Album Type"] = _freeform([album.release_type])
    if track.isrc:
        tags[itunes + "ISRC"] = _freeform([track.isrc])
    for key, values in album.extra.items():
        tags[key] = _freeform(values) if key.startswith("----:") else values
    audio.save()


def simple_album(
    artist: str, title: str, count: int, *, fmt: Format = "flac", seconds: float = 3.0, **kw: object
) -> Album:
    tracks = tuple(Track(f"{title} Song {n}", n, seconds=seconds) for n in range(1, count + 1))
    return Album(artist, title, tracks, fmt=fmt, **kw)  # type: ignore[arg-type]
