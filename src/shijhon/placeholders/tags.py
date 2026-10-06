"""Tags for placeholders, following how Navidrome 0.64.2 reads them.

Navidrome maps every tag through ``resources/mappings.yaml``: for each tag it collects the
values of *all* aliases present in the file, in the order the aliases are listed, splits
and de-duplicates them. Its album ID is built from the first value of
``musicbrainz_albumid``, or else from the album artist, ``album``, ``albumversion`` and
``releasedate``.

So a placeholder that completes an owned album copies the owned file's album-level tags
as Navidrome sees them: for each tag, the merged values of all its aliases, written under
one Vorbis key. Nothing the owned file lacks is added (no release type, no images). Track
data (title, artists, numbers, ISRC) comes from the catalog. The mandatory check after
the scan (the album did not split) remains the final authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX, Frames
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from shijhon.catalog.model import CatalogRelease, CatalogTrack, ReleaseKind

Tags = dict[str, list[str]]

# Album-level tags Shijhon copies: logical name -> Navidrome 0.64.2 aliases, in order.
ALBUM_TAGS: dict[str, tuple[str, ...]] = {
    "album": ("talb", "album", "©alb", "wm/albumtitle", "iprd"),
    "albumsort": ("tsoa", "albumsort", "soal", "wm/albumsortorder"),
    "albumartist": (
        "tpe2", "albumartist", "album artist", "album_artist", "aart", "wm/albumartist",
    ),
    "albumartistsort": (
        "tso2", "txxx:albumartistsort", "albumartistsort", "soaa", "wm/albumartistsortorder",
    ),
    "albumartists": ("txxx:album artists", "albumartists"),
    "albumartistssort": ("albumartistssort",),
    "albumversion": ("albumversion", "musicbrainz_albumcomment", "musicbrainz album comment"),
    "releasedate": ("tdrl", "releasedate", "©day", "wm/year", "year"),
    "recordingdate": ("tdrc", "date", "recordingdate", "icrd", "record date"),
    "originaldate": (
        "tdor", "originaldate", "----:com.apple.itunes:originaldate", "wm/originalreleasetime",
        "tory", "originalyear", "----:com.apple.itunes:originalyear",
        "wm/originalreleaseyear", "origyear", "----:com.apple.itunes:origyear",
    ),
    "musicbrainz_albumid": (
        "txxx:musicbrainz album id", "musicbrainz_albumid", "musicbrainz album id",
        "----:com.apple.itunes:musicbrainz album id", "musicbrainz/album id",
    ),
    "musicbrainz_albumartistid": (
        "txxx:musicbrainz album artist id", "musicbrainz_albumartistid",
        "musicbrainz album artist id", "----:com.apple.itunes:musicbrainz album artist id",
        "musicbrainz/album artist id",
    ),
    "musicbrainz_releasegroupid": (
        "txxx:musicbrainz release group id", "musicbrainz_releasegroupid",
        "----:com.apple.itunes:musicbrainz release group id", "musicbrainz/release group id",
    ),
    "compilation": ("tcmp", "compilation", "cpil", "wm/iscompilation"),
    "releasetype": (
        "txxx:musicbrainz album type", "releasetype", "musicbrainz_albumtype",
        "----:com.apple.itunes:musicbrainz album type", "musicbrainz/album type",
    ),
    "genre": ("tcon", "genre", "©gen", "wm/genre", "ignr"),
    "mood": ("tmoo", "mood", "----:com.apple.itunes:mood", "wm/mood"),
    "disctotal": ("disctotal", "totaldiscs"),
    "tracktotal": ("tracktotal", "totaltracks"),
    "catalognumber": (
        "txxx:catalognumber", "catalognumber", "----:com.apple.itunes:catalognumber",
        "wm/catalogno",
    ),
    "recordlabel": (
        "tpub", "label", "publisher", "----:com.apple.itunes:label", "wm/publisher",
        "organization",
    ),
    "releasecountry": (
        "txxx:musicbrainz album release country", "releasecountry",
        "----:com.apple.itunes:musicbrainz album release country",
        "musicbrainz/album release country", "icnt",
    ),
    "releasestatus": (
        "txxx:musicbrainz album status", "releasestatus", "musicbrainz_albumstatus",
        "----:com.apple.itunes:musicbrainz album status", "musicbrainz/album status",
    ),
    "media": ("tmed", "media", "----:com.apple.itunes:media", "wm/media", "imed"),
    "grouping": ("grp1", "grouping", "©grp", "wm/contentgroupdescription"),
}  # fmt: skip

# The Vorbis key a placeholder uses for each logical tag (each is one of its aliases).
VORBIS_KEY = {name: name for name in ALBUM_TAGS} | {
    "recordingdate": "date",
    "recordlabel": "label",
}

# Marker tags: which catalog item a placeholder stands for, and whether the file is the
# silent placeholder itself (delivered audio carries the same markers without it).
MARKER_TRACK = "shijhon_ref"
MARKER_RELEASE = "shijhon_release"
MARKER_SILENCE = "shijhon_silence"


class UnsupportedFormat(ValueError):
    """Shijhon cannot read this file's tags the way Navidrome does."""


# --- reading ---------------------------------------------------------------------------


def raw_tags(path: Path) -> Tags:
    """The file's tags as lower-case keys, roughly as TagLib hands them to Navidrome."""
    audio = mutagen.File(path)
    if audio is None or audio.tags is None:
        if isinstance(audio, (FLAC, OggVorbis, OggOpus, MP3, MP4)):
            return {}
        raise UnsupportedFormat(path.suffix)
    if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        out: Tags = {}
        for key, value in audio.tags:
            out.setdefault(key.lower(), []).append(value)
        return out
    if isinstance(audio.tags, ID3):
        return _id3_raw(audio.tags)
    if isinstance(audio, MP4):
        return _mp4_raw(audio.tags)
    raise UnsupportedFormat(path.suffix)


def _id3_raw(tags: ID3) -> Tags:
    out: Tags = {}
    for frame in tags.values():
        texts = [str(t) for t in getattr(frame, "text", [])]
        if not texts:
            continue
        if isinstance(frame, TXXX):
            out.setdefault(f"txxx:{frame.desc.lower()}", []).extend(texts)
            out.setdefault(frame.desc.lower(), []).extend(texts)
            continue
        out.setdefault(frame.FrameID.lower(), []).extend(texts)
        if frame.FrameID in ("TRCK", "TPOS"):
            _, _, total = texts[0].partition("/")
            if total:
                key = "tracktotal" if frame.FrameID == "TRCK" else "disctotal"
                out.setdefault(key, []).append(total)
    return out


def _mp4_raw(tags: Mapping[str, object]) -> Tags:
    out: Tags = {}
    for key, raw_values in tags.items():
        lowered = key.lower()
        values = raw_values if isinstance(raw_values, list) else [raw_values]  # cpil is a bool
        if lowered in ("trkn", "disk"):
            _, total = values[0]  # type: ignore[misc]
            name = "tracktotal" if lowered == "trkn" else "disctotal"
            if total:
                out.setdefault(name, []).append(str(total))
            continue
        texts = [_mp4_text(v) for v in values]
        texts = [t for t in texts if t]
        if not texts:
            continue
        out.setdefault(lowered, []).extend(texts)
        if lowered.startswith("----:"):
            out.setdefault(lowered.rsplit(":", 1)[-1], []).extend(texts)
    return out


def _mp4_text(value: object) -> str:
    if isinstance(value, MP4FreeForm):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value)


def mapped(raw: Tags, aliases: Iterable[str]) -> list[str]:
    """Navidrome's merge: values of every alias present, in alias order, de-duplicated."""
    values: list[str] = []
    for alias in aliases:
        for value in raw.get(alias, []):
            if value.strip() and value not in values:
                values.append(value)
    return values


def album_tags_of(path: Path) -> Tags:
    """The album-level tags of an owned file, as logical name -> merged values."""
    raw = raw_tags(path)
    tags = {name: mapped(raw, aliases) for name, aliases in ALBUM_TAGS.items()}
    return {name: values for name, values in tags.items() if values}


# --- building placeholder tags ---------------------------------------------------------


def catalog_album_tags(release: CatalogRelease) -> Tags:
    """Album tags for a catalog-only album (no owned files to copy from)."""
    tags: Tags = {"album": [release.title], "albumartist": [release.artist]}
    if len(release.artists) > 1:
        tags["albumartists"] = list(release.artists)
    if release.release_date:
        tags["releasedate"] = [release.release_date]
        tags["recordingdate"] = [release.release_date]
    tags["releasetype"] = [release.kind.value]
    if release.kind is ReleaseKind.COMPILATION:
        tags["compilation"] = ["1"]
    if release.clean:
        # Otherwise a clean edition would share the explicit edition's album ID.
        tags["albumversion"] = ["Clean"]
    genres = [g for g in release.genres if g.lower() != "music"]
    if genres:
        tags["genre"] = genres
    if release.label:
        tags["recordlabel"] = [release.label]
    if release.tracks:
        total = len(release.tracks)
        if release.incomplete and release.track_count:
            # A track of it could not be read: the album has as many as the catalog says.
            total = max(total, release.track_count)
        tags["tracktotal"] = [str(total)]
        tags["disctotal"] = [str(max(t.disc for t in release.tracks))]
    return tags


def placeholder_tags(
    album_tags: Tags, track: CatalogTrack, release: CatalogRelease
) -> dict[str, list[str]]:
    """Vorbis comments for one placeholder: album tags plus catalog track data."""
    comments: dict[str, list[str]] = {
        VORBIS_KEY[name]: values for name, values in album_tags.items() if name in VORBIS_KEY
    }
    comments["title"] = [track.title]
    comments["artist"] = [track.artist]
    if len(track.artists) > 1:
        comments["artists"] = list(track.artists)
    comments["tracknumber"] = [str(track.number)]
    comments["discnumber"] = [str(track.disc)]
    if track.isrc:
        comments["isrc"] = [track.isrc]
    if track.explicit:
        comments["itunesadvisory"] = ["1"]
    elif release.clean:  # clients show the clean edition's tracks as clean
        comments["itunesadvisory"] = ["2"]
    comments[MARKER_TRACK] = [str(track.ref)]
    comments[MARKER_RELEASE] = [str(release.ref)]
    comments[MARKER_SILENCE] = ["1"]
    return comments


def write_flac(
    path: Path, comments: Mapping[str, list[str]], *, not_size: int | None = None
) -> None:
    """Write the comments (nothing else stays). ``not_size``: the file never ends up that
    size - the size of the file it will replace at the same path, so that a scan which
    read the new file can be told from one that did not."""
    audio = FLAC(path)
    audio.clear_pictures()
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.clear()
    for key, values in comments.items():
        audio[key] = list(values)
    audio.save(deleteid3=True)
    _another_size(path, not_size)


def _another_size(path: Path, not_size: int | None) -> None:
    """A FLAC file of ``not_size`` gets a byte more padding (its tags and audio as they are)."""
    if not_size is not None and path.stat().st_size == not_size:
        FLAC(path).save(padding=lambda info: max(info.padding, 0) + 1)


def flac_samples(path: Path) -> int:
    """The length of a FLAC file in samples."""
    return int(FLAC(path).info.total_samples)


def read_vorbis(path: Path) -> dict[str, list[str]]:
    audio = FLAC(path)
    out: dict[str, list[str]] = {}
    for key, value in audio.tags or []:
        out.setdefault(key.lower(), []).append(value)
    return out


@dataclass(frozen=True)
class Marker:
    track: str
    release: str
    silence: bool  # the silent placeholder, not delivered audio
    raw: Tags


def marker_of(path: Path) -> Marker | None:
    """Shijhon's markers in a placeholder or delivered file, if any."""
    try:
        raw = raw_tags(path)
    except (UnsupportedFormat, mutagen.MutagenError):
        return None
    track = raw.get(MARKER_TRACK)
    release = raw.get(MARKER_RELEASE)
    if not track or not release:
        return None
    return Marker(track[0], release[0], bool(raw.get(MARKER_SILENCE)), raw)


def placeholder_comments_from(raw: Tags, song: Mapping[str, object]) -> dict[str, list[str]]:
    """Rebuild a placeholder's Vorbis comments from a delivered file's tags and Navidrome's
    record of the song (used by recovery when the silent file is not on disk)."""
    comments: dict[str, list[str]] = {}
    for name, aliases in ALBUM_TAGS.items():
        values = mapped(raw, aliases)
        if values:
            comments[VORBIS_KEY[name]] = values
    comments["title"] = [str(song.get("title", ""))]
    comments["artist"] = [str(song.get("artist", ""))]
    comments["tracknumber"] = [str(song.get("trackNumber") or 0)]
    comments["discnumber"] = [str(song.get("discNumber") or 1)]
    isrc = mapped(raw, ("tsrc", "isrc", "----:com.apple.itunes:isrc"))
    if isrc:
        comments["isrc"] = isrc
    for marker in (MARKER_TRACK, MARKER_RELEASE):
        if raw.get(marker):
            comments[marker] = raw[marker][:1]
    comments[MARKER_SILENCE] = ["1"]
    return comments


# --- tags for delivered audio written in place of a placeholder -------------------

# Vorbis key -> MP4 atom. Keys without an entry become iTunes freeform atoms.
_MP4_ATOMS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "albumartist": "aART",
    "releasedate": "\xa9day",
    "genre": "\xa9gen",
    "albumsort": "soal",
    "albumartistsort": "soaa",
    "grouping": "\xa9grp",
}
_MP4_FREEFORM = {
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "releasetype": "MusicBrainz Album Type",
    "releasecountry": "MusicBrainz Album Release Country",
    "releasestatus": "MusicBrainz Album Status",
    "originaldate": "ORIGINALDATE",
    "isrc": "ISRC",
}
_MP4_SKIP = {"tracknumber", "tracktotal", "discnumber", "disctotal", "compilation", "date"}


def write_delivered(
    path: Path, comments: Mapping[str, list[str]], *, not_size: int | None = None
) -> None:
    """Replace a delivered file's own tags with the placeholder's (same logical values).
    ``not_size``: as for :func:`write_flac` (a FLAC file that replaces the placeholder at
    its path)."""
    comments = {k: v for k, v in comments.items() if k != MARKER_SILENCE}
    audio = mutagen.File(path)
    if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        if isinstance(audio, FLAC):
            audio.clear_pictures()
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        audio.tags.clear()
        for key, values in comments.items():
            audio[key] = list(values)
        audio.save()
        if isinstance(audio, FLAC):
            _another_size(path, not_size)
        return
    if isinstance(audio, MP4):
        _write_mp4(audio, comments)
        return
    if isinstance(audio, MP3):
        _write_id3(audio, comments)
        return
    raise UnsupportedFormat(path.suffix)


def _write_mp4(audio: MP4, comments: Mapping[str, list[str]]) -> None:
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags.clear()
    for key, values in comments.items():
        if key in _MP4_SKIP:
            continue
        if key == "itunesadvisory":
            tags["rtng"] = [int(values[0])]
            continue
        atom = _MP4_ATOMS.get(key)
        if atom is not None:
            tags[atom] = list(values)
        else:
            name = _MP4_FREEFORM.get(key, key.upper())
            tags[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(v.encode()) for v in values]
    number = int((comments.get("tracknumber") or ["0"])[0] or 0)
    total = int((comments.get("tracktotal") or ["0"])[0] or 0)
    tags["trkn"] = [(number, total)]
    disc = int((comments.get("discnumber") or ["0"])[0] or 0)
    discs = int((comments.get("disctotal") or ["0"])[0] or 0)
    tags["disk"] = [(disc, discs)]
    if comments.get("compilation", ["0"])[0] not in ("", "0"):
        tags["cpil"] = True
    audio.save()


_ID3_FRAMES = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "releasedate": "TDRL",
    "date": "TDRC",
    "originaldate": "TDOR",
    "genre": "TCON",
    "compilation": "TCMP",
    "isrc": "TSRC",
    "albumsort": "TSOA",
    "albumartistsort": "TSO2",
    "label": "TPUB",
    "media": "TMED",
    "grouping": "GRP1",
}
_ID3_TXXX = {
    "musicbrainz_albumid": "MusicBrainz Album Id",
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "releasetype": "MusicBrainz Album Type",
    "releasecountry": "MusicBrainz Album Release Country",
    "releasestatus": "MusicBrainz Album Status",
}


def _write_id3(audio: MP3, comments: Mapping[str, list[str]]) -> None:
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags.clear()
    for key, values in comments.items():
        if key in ("tracknumber", "tracktotal", "discnumber", "disctotal"):
            continue
        frame_id = _ID3_FRAMES.get(key)
        if frame_id is not None:
            tags.add(Frames[frame_id](encoding=3, text=list(values)))
        else:
            desc = _ID3_TXXX.get(key, key.upper())
            tags.add(TXXX(encoding=3, desc=desc, text=list(values)))
    for key, total_key, frame_id in (
        ("tracknumber", "tracktotal", "TRCK"),
        ("discnumber", "disctotal", "TPOS"),
    ):
        if comments.get(key):
            text = comments[key][0] + (
                f"/{comments[total_key][0]}" if comments.get(total_key) else ""
            )
            tags.add(Frames[frame_id](encoding=3, text=[text]))
    audio.save(v2_version=4)
