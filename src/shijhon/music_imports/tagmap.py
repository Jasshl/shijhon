"""Tags of every audio format Mutagen can write and Navidrome 0.64.2 reads, for music
imports: reading them as Navidrome does, replacing a file's album-level tags (a copy that
joins an album, ``retag.py``) and carrying a file's whole metadata into other audio (an
upgraded file, ``upgrade.py``).

The families:

- **Vorbis comments**: FLAC, Ogg Vorbis, Opus, Ogg FLAC, Ogg Speex.
- **ID3**: MP3 and MPEG layer II, WAVE, AIFF, DSF, TrueAudio, and raw ADTS AAC (a
  stand-alone ID3 tag before the audio).
- **MPEG-4** atoms: AAC and ALAC in MPEG-4.
- **APEv2**: Monkey's Audio, WavPack, Musepack. Navidrome reads TagLib's names for them:
  upper-case keys, with "Year" read as the recording date, "Track" and "Disc" as the
  numbers and "Album Artist" as the album artist.
- **ASF**: WMA. Navidrome reads both the attributes' own names ("WM/Year", read as the
  release date) and TagLib's names for them ("WM/Year" again as the recording date).

- **Matroska**: its tags as ffprobe lists them; Navidrome reads "ALBUM_ARTIST" as the
  album artist, "DATE_RELEASED" as the release date and "DATE_RECORDED" as the recording
  date. Mutagen has no writer for it: a copy that joins an album is made by ffmpeg as a
  stream copy with the album's tags (``matroska_copy``).

An upgraded file of the same family as the imported one carries its tags item by item
(``carry_same``: ID3 frames with their descriptions and languages, Vorbis comments,
MPEG-4 atoms with freeform ones under their own names, APEv2 items); across families,
by logical name (``metadata``, ``write``). Neither carries what describes the imported
file's own encoding (``ENCODING``: its encoder and settings, length, gapless and
normalization data; ID3 private frames known to describe its audio or a player's state of
it, ``_PRIV_ENCODING``): it would be wrong for the new audio.

Measured with the pinned release (``tests/acceptance/test_music_import_tags.py``). Shorten
has no tags at all. Not read or written: RIFF INFO chunks of WAVE files (Mutagen does not
read them; Navidrome reads their album and dates after ID3's).
"""

from __future__ import annotations

import base64
import copy
import struct
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.apev2 import BINARY, APETextValue, APEValue
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.dsf import DSF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TXXX, USLT, Frames, ID3NoHeaderError
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggvorbis import OggVorbis
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

from shijhon.placeholders import tags as tagging

Tags = dict[str, list[str]]

VORBIS = (FLAC, OggVorbis, OggOpus, OggFLAC, OggSpeex)
APE = (MonkeysAudio, WavPack, Musepack)
ID3_FILES = (MP3, WAVE, AIFF, DSF, TrueAudio, AAC)
_ITUNES = "----:com.apple.iTunes:"

# TagLib's names for APEv2 keys that differ from the key itself (upper case).
_APE_NAMES = {
    "YEAR": "DATE",
    "TRACK": "TRACKNUMBER",
    "DISC": "DISCNUMBER",
    "ALBUM ARTIST": "ALBUMARTIST",
    "MIXARTIST": "REMIXER",
    "MUSICBRAINZ_ALBUMSTATUS": "RELEASESTATUS",
    "MUSICBRAINZ_ALBUMTYPE": "RELEASETYPE",
}
# TagLib's names for ASF attributes (Navidrome reads these and the attributes' own names).
_ASF_NAMES = {
    "Title": "TITLE",
    "Author": "ARTIST",
    "Copyright": "COPYRIGHT",
    "Description": "COMMENT",
    "WM/AlbumTitle": "ALBUM",
    "WM/AlbumArtist": "ALBUMARTIST",
    "WM/Composer": "COMPOSER",
    "WM/Writer": "LYRICIST",
    "WM/Conductor": "CONDUCTOR",
    "WM/ModifiedBy": "REMIXER",
    "WM/Year": "DATE",
    "WM/OriginalReleaseYear": "ORIGINALDATE",
    "WM/Producer": "PRODUCER",
    "WM/ContentGroupDescription": "GROUPING",
    "WM/SubTitle": "SUBTITLE",
    "WM/SetSubTitle": "DISCSUBTITLE",
    "WM/TrackNumber": "TRACKNUMBER",
    "WM/PartOfSet": "DISCNUMBER",
    "WM/Genre": "GENRE",
    "WM/BeatsPerMinute": "BPM",
    "WM/Mood": "MOOD",
    "WM/ISRC": "ISRC",
    "WM/Lyrics": "LYRICS",
    "WM/Media": "MEDIA",
    "WM/Publisher": "LABEL",
    "WM/CatalogNo": "CATALOGNUMBER",
    "WM/Barcode": "BARCODE",
    "WM/EncodedBy": "ENCODEDBY",
    "WM/AlbumSortOrder": "ALBUMSORT",
    "WM/AlbumArtistSortOrder": "ALBUMARTISTSORT",
    "WM/ArtistSortOrder": "ARTISTSORT",
    "WM/TitleSortOrder": "TITLESORT",
    "WM/Script": "SCRIPT",
    "WM/Language": "LANGUAGE",
    "WM/ARTISTS": "ARTISTS",
    "MusicBrainz/Track Id": "MUSICBRAINZ_TRACKID",
    "MusicBrainz/Artist Id": "MUSICBRAINZ_ARTISTID",
    "MusicBrainz/Album Id": "MUSICBRAINZ_ALBUMID",
    "MusicBrainz/Album Artist Id": "MUSICBRAINZ_ALBUMARTISTID",
    "MusicBrainz/Album Release Country": "RELEASECOUNTRY",
    "MusicBrainz/Album Status": "RELEASESTATUS",
    "MusicBrainz/Album Type": "RELEASETYPE",
    "MusicBrainz/Release Group Id": "MUSICBRAINZ_RELEASEGROUPID",
    "MusicBrainz/Release Track Id": "MUSICBRAINZ_RELEASETRACKID",
    "MusicBrainz/Work Id": "MUSICBRAINZ_WORKID",
}

# A file's metadata by logical (Vorbis comment) name, and where each family keeps it:
# an ID3 frame ("TXXX:<description>" for a user text frame), an MPEG-4 atom, an APEv2
# key, an ASF attribute. "" where a family has no place for it.
_N = ("vorbis", "id3", "mp4", "ape", "asf")
_TABLE: dict[str, tuple[str, str, str, str, str]] = {
    "title": ("title", "TIT2", "\xa9nam", "Title", "Title"),
    "artist": ("artist", "TPE1", "\xa9ART", "Artist", "Author"),
    "artists": ("artists", "TXXX:ARTISTS", _ITUNES + "ARTISTS", "Artists", "WM/ARTISTS"),
    "album": ("album", "TALB", "\xa9alb", "Album", "WM/AlbumTitle"),
    "albumartist": ("albumartist", "TPE2", "aART", "ALBUMARTIST", "WM/AlbumArtist"),
    "albumartists": (
        "albumartists", "TXXX:ALBUMARTISTS", _ITUNES + "ALBUMARTISTS", "ALBUMARTISTS",
        "ALBUMARTISTS",
    ),
    "date": ("date", "TDRC", "", "Year", ""),
    "releasedate": ("releasedate", "TDRL", "\xa9day", "RELEASEDATE", "WM/Year"),
    "originaldate": (
        "originaldate", "TDOR", _ITUNES + "ORIGINALDATE", "ORIGINALDATE",
        "WM/OriginalReleaseTime",
    ),
    "genre": ("genre", "TCON", "\xa9gen", "Genre", "WM/Genre"),
    "composer": ("composer", "TCOM", "\xa9wrt", "Composer", "WM/Composer"),
    "lyricist": ("lyricist", "TEXT", _ITUNES + "LYRICIST", "Lyricist", "WM/Writer"),
    "originalartist": (
        "originalartist", "TOPE", _ITUNES + "ORIGINALARTIST", "ORIGINALARTIST",
        "WM/OriginalArtist",
    ),
    "originalalbum": (
        "originalalbum", "TOAL", _ITUNES + "ORIGINALALBUM", "ORIGINALALBUM",
        "WM/OriginalAlbumTitle",
    ),
    "originallyricist": (
        "originallyricist", "TOLY", _ITUNES + "ORIGINALLYRICIST", "ORIGINALLYRICIST",
        "WM/OriginalLyricist",
    ),
    "initialkey": ("initialkey", "TKEY", _ITUNES + "INITIALKEY", "INITIALKEY", "WM/InitialKey"),
    "conductor": ("conductor", "TPE3", _ITUNES + "CONDUCTOR", "Conductor", "WM/Conductor"),
    "remixer": ("remixer", "TPE4", _ITUNES + "REMIXER", "MixArtist", "WM/ModifiedBy"),
    "lyrics": ("lyrics", "USLT", "\xa9lyr", "Lyrics", "WM/Lyrics"),
    "comment": ("comment", "COMM", "\xa9cmt", "Comment", "Description"),
    "bpm": ("bpm", "TBPM", "tmpo", "BPM", "WM/BeatsPerMinute"),
    "isrc": ("isrc", "TSRC", _ITUNES + "ISRC", "ISRC", "WM/ISRC"),
    "compilation": ("compilation", "TCMP", "cpil", "Compilation", "WM/IsCompilation"),
    "albumsort": ("albumsort", "TSOA", "soal", "ALBUMSORT", "WM/AlbumSortOrder"),
    "albumartistsort": (
        "albumartistsort", "TSO2", "soaa", "ALBUMARTISTSORT", "WM/AlbumArtistSortOrder",
    ),
    "artistsort": ("artistsort", "TSOP", "soar", "ARTISTSORT", "WM/ArtistSortOrder"),
    "titlesort": ("titlesort", "TSOT", "sonm", "TITLESORT", "WM/TitleSortOrder"),
    "composersort": ("composersort", "TSOC", "soco", "COMPOSERSORT", ""),
    "grouping": ("grouping", "GRP1", "\xa9grp", "Grouping", "WM/ContentGroupDescription"),
    "subtitle": ("subtitle", "TIT3", _ITUNES + "SUBTITLE", "Subtitle", "WM/SubTitle"),
    "discsubtitle": (
        "discsubtitle", "TSST", _ITUNES + "DISCSUBTITLE", "DISCSUBTITLE", "WM/SetSubTitle",
    ),
    "mood": ("mood", "TMOO", _ITUNES + "MOOD", "Mood", "WM/Mood"),
    "label": ("label", "TPUB", _ITUNES + "LABEL", "Label", "WM/Publisher"),
    "media": ("media", "TMED", _ITUNES + "MEDIA", "Media", "WM/Media"),
    "catalognumber": (
        "catalognumber", "TXXX:CATALOGNUMBER", _ITUNES + "CATALOGNUMBER", "CatalogNumber",
        "WM/CatalogNo",
    ),
    "barcode": ("barcode", "TXXX:BARCODE", _ITUNES + "BARCODE", "Barcode", "WM/Barcode"),
    "copyright": ("copyright", "TCOP", "cprt", "Copyright", "Copyright"),
    "language": ("language", "TLAN", _ITUNES + "LANGUAGE", "Language", "WM/Language"),
    "script": ("script", "TXXX:SCRIPT", _ITUNES + "SCRIPT", "Script", "WM/Script"),
    "albumversion": (
        "albumversion", "TXXX:ALBUMVERSION", _ITUNES + "ALBUMVERSION", "ALBUMVERSION",
        "ALBUMVERSION",
    ),
    "releasetype": (
        "releasetype", "TXXX:MusicBrainz Album Type", _ITUNES + "MusicBrainz Album Type",
        "RELEASETYPE", "MusicBrainz/Album Type",
    ),
    "releasestatus": (
        "releasestatus", "TXXX:MusicBrainz Album Status", _ITUNES + "MusicBrainz Album Status",
        "RELEASESTATUS", "MusicBrainz/Album Status",
    ),
    "releasecountry": (
        "releasecountry", "TXXX:MusicBrainz Album Release Country",
        _ITUNES + "MusicBrainz Album Release Country", "RELEASECOUNTRY",
        "MusicBrainz/Album Release Country",
    ),
    "musicbrainz_trackid": (
        "musicbrainz_trackid", "TXXX:MusicBrainz Track Id", _ITUNES + "MusicBrainz Track Id",
        "MUSICBRAINZ_TRACKID", "MusicBrainz/Track Id",
    ),
    "musicbrainz_releasetrackid": (
        "musicbrainz_releasetrackid", "TXXX:MusicBrainz Release Track Id",
        _ITUNES + "MusicBrainz Release Track Id", "MUSICBRAINZ_RELEASETRACKID",
        "MusicBrainz/Release Track Id",
    ),
    "musicbrainz_albumid": (
        "musicbrainz_albumid", "TXXX:MusicBrainz Album Id", _ITUNES + "MusicBrainz Album Id",
        "MUSICBRAINZ_ALBUMID", "MusicBrainz/Album Id",
    ),
    "musicbrainz_artistid": (
        "musicbrainz_artistid", "TXXX:MusicBrainz Artist Id", _ITUNES + "MusicBrainz Artist Id",
        "MUSICBRAINZ_ARTISTID", "MusicBrainz/Artist Id",
    ),
    "musicbrainz_albumartistid": (
        "musicbrainz_albumartistid", "TXXX:MusicBrainz Album Artist Id",
        _ITUNES + "MusicBrainz Album Artist Id", "MUSICBRAINZ_ALBUMARTISTID",
        "MusicBrainz/Album Artist Id",
    ),
    "musicbrainz_releasegroupid": (
        "musicbrainz_releasegroupid", "TXXX:MusicBrainz Release Group Id",
        _ITUNES + "MusicBrainz Release Group Id", "MUSICBRAINZ_RELEASEGROUPID",
        "MusicBrainz/Release Group Id",
    ),
    "musicbrainz_workid": (
        "musicbrainz_workid", "TXXX:MusicBrainz Work Id", _ITUNES + "MusicBrainz Work Id",
        "MUSICBRAINZ_WORKID", "MusicBrainz/Work Id",
    ),
    "itunesadvisory": (
        "itunesadvisory", "TXXX:ITUNESADVISORY", "rtng", "ITUNESADVISORY", "ITUNESADVISORY",
    ),
}  # fmt: skip
# The numbers and their totals: "n/t" in ID3 TRCK/TPOS and APEv2 Track/Disc, pairs in
# MPEG-4 trkn/disk, separate entries in Vorbis comments and ASF.
_NUMBERS = {
    "tracknumber": ("TRCK", "trkn", "Track", "WM/TrackNumber", "tracktotal"),
    "discnumber": ("TPOS", "disk", "Disc", "WM/PartOfSet", "disctotal"),
}
_TOTALS = {"tracktotal": "totaltracks", "disctotal": "totaldiscs"}
# ReplayGain and similar free-text values every family keeps under their own name.
_FREE_ALWAYS = ("replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain",
                "replaygain_album_peak")  # fmt: skip

# What describes the imported file's own encoding, by (lower-case) name in every family -
# its encoder and settings, gapless and normalization data -, and the ID3 frames and
# MPEG-4 atoms that do (length, file type, layout, encryption, the encoding tool, gapless
# playback): never carried into other audio.
ENCODING = frozenset({"encoder", "encodersettings", "encoder settings", "encoding",
                      "encodedby", "encoded_by", "encoded-by", "itunsmpb", "itunnorm",
                      "itunpgap", "encoding params"})  # fmt: skip
_ID3_ENCODING = frozenset({"TLEN", "TFLT", "TSSE", "TENC", "TDEN", "MLLT", "ASPI", "SEEK",
                           "POSS", "AENC", "ENCR", "GRID", "SIGN", "APIC"})  # fmt: skip
# ID3 private frames (PRIV) whose owners are known to describe the imported file's own
# audio or a player's state of it, not the recording - every other private frame (an
# application's notes, a store's or service's identifiers of the recording or album) is
# carried as it is:
# - "com.apple.streaming.transportStreamTimestamp": the timestamp of the first sample of an
#   HTTP Live Streaming audio segment (RFC 8216, section 3.4);
# - "PeakValue" and "AverageLevel": a media player's volume-leveling measurements of the
#   file's audio (as the "iTunNORM" comment, in ``ENCODING``);
# - "mailto:uits-info@umusic.com": a UITS purchase record, signed over the hash of the
#   file's own audio.
_PRIV_ENCODING = frozenset({"com.apple.streaming.transportStreamTimestamp", "PeakValue",
                            "AverageLevel", "mailto:uits-info@umusic.com"})  # fmt: skip
_MP4_ENCODING = frozenset({"\xa9too", "pgap", "covr"})
_VORBIS_PICTURES = frozenset({"metadata_block_picture", "coverart", "coverartmime"})

_REVERSE: dict[str, dict[str, str]] = {
    family: {
        (names[i].lower() if family != "mp4" else names[i]): logical
        for logical, names in _TABLE.items()
        if names[i]
    }
    for i, family in enumerate(_N)
}


def family(audio: Any) -> str:
    """The tag family of a parsed file ("" when it has none Shijhon can write)."""
    if isinstance(audio, VORBIS):
        return "vorbis"
    if isinstance(audio, MP4):
        return "mp4"
    if isinstance(audio, ASF):
        return "asf"
    if isinstance(audio, APE):
        return "ape"
    if isinstance(audio, ID3_FILES):
        return "id3"
    return ""


# The parsers for a file's suffix, as the probe reads it (a raw AAC file with an ID3 tag
# would otherwise be taken for MPEG audio).
PARSERS: dict[str, tuple[Any, ...]] = {
    "mp3": (MP3,), "mp2": (MP3,), "flac": (FLAC,), "aac": (AAC,), "m4a": (MP4,),
    "m4b": (MP4,), "alac": (MP4,), "ogg": (OggVorbis, OggOpus, OggFLAC, OggSpeex),
    "oga": (OggVorbis, OggOpus, OggFLAC, OggSpeex), "opus": (OggOpus,), "wav": (WAVE,),
    "aif": (AIFF,), "aiff": (AIFF,), "aifc": (AIFF,), "spx": (OggSpeex,), "wma": (ASF,),
    "wv": (WavPack,), "wvp": (WavPack,), "ape": (MonkeysAudio,), "mpc": (Musepack,),
    "tta": (TrueAudio,), "dsf": (DSF,),
}  # fmt: skip


def parse(fileobj: Any, suffix: str = "") -> Any:
    """The parsed file (None: not one Mutagen reads), by its suffix's parsers first."""
    import mutagen

    for parser in PARSERS.get(suffix.lower().lstrip("."), ()):
        fileobj.seek(0)
        try:
            return parser(fileobj)
        except Exception:  # noqa: S112 - the next parser for this suffix, else by content
            continue
    fileobj.seek(0)
    found = mutagen.File(fileobj)
    fileobj.seek(0)
    return found


def standalone_id3(fileobj: Any) -> ID3:
    """A raw AAC file's ID3 tag (an empty one when it has none)."""
    fileobj.seek(0)
    try:
        return ID3(fileobj)
    except ID3NoHeaderError:
        return ID3()


def tags_of(audio: Any, fileobj: Any = None) -> Any:
    """The file's tag object (``fileobj``: a raw AAC file's, read from it)."""
    if isinstance(audio, AAC):
        return standalone_id3(fileobj) if fileobj is not None else None
    return getattr(audio, "tags", None)


# --- reading as Navidrome does ---------------------------------------------------------


def _text(value: Any) -> list[str]:
    if isinstance(value, APETextValue):
        return [str(v) for v in value]
    if type(value).__module__ == "mutagen.apev2":  # binary or locator: not text
        return []
    value = getattr(value, "value", value)
    if isinstance(value, bytes):
        return []
    return [str(value)]


def raw(audio: Any, tags: Any = None) -> Tags:
    """The file's tags as lower-case keys, as Navidrome 0.64.2 gets them from TagLib
    (``placeholders/tags.raw_tags_of`` for the families it reads; here every family)."""
    tags = tags if tags is not None else getattr(audio, "tags", None)
    if isinstance(tags, ID3):
        return tagging._id3_raw(tags)
    if tags is None:
        return {}
    out: Tags = {}
    if isinstance(audio, VORBIS):
        for key, value in tags:
            out.setdefault(key.lower(), []).append(value)
        return out
    if isinstance(audio, MP4):
        return tagging._mp4_raw(tags)
    if isinstance(audio, ASF):
        for name, values in tags.items():
            texts = [t for v in values for t in _text(v) if t]
            if not texts:
                continue
            out.setdefault(name.lower(), []).extend(texts)
            translated = _ASF_NAMES.get(name)
            if translated and translated.lower() != name.lower():
                out.setdefault(translated.lower(), []).extend(texts)
        return out
    if isinstance(audio, APE):
        for key, value in tags.items():
            texts = [t for t in _text(value) if t]
            if not texts:
                continue
            name = _APE_NAMES.get(key.upper(), key.upper())
            out.setdefault(name.lower(), []).extend(texts)
        return out
    raise tagging.UnsupportedFormat(type(audio).__name__)


def album_tags(audio: Any, tags: Any = None) -> Tags:
    """Album-level tags, logical name -> merged values (``placeholders/tags.ALBUM_TAGS``)."""
    found = raw(audio, tags)
    merged = {name: tagging.mapped(found, aliases) for name, aliases in tagging.ALBUM_TAGS.items()}
    return {name: values for name, values in merged.items() if values}


def album_tags_at(path: Any) -> Tags:
    """``album_tags`` of the file at ``path`` (an owned file the review compares with)."""
    if str(path).lower().endswith(".mka"):
        return matroska_album_tags(str(path))
    with open(path, "rb") as fileobj:
        audio = parse(fileobj, str(path).rsplit(".", 1)[-1])
        if audio is None or not family(audio):
            raise tagging.UnsupportedFormat(str(path))
        return album_tags(audio, tags_of(audio, fileobj))


# --- a file's whole metadata ----------------------------------------------------------


def _split(value: str) -> tuple[str, str]:
    number, _, total = value.partition("/")
    return number.strip(), total.strip()


def metadata(audio: Any, tags: Any = None) -> Tags:
    """Every text tag of the file by logical name (``_TABLE``; others by their own name,
    lower case), for carrying them into other audio. Pictures are separate."""
    tags = tags if tags is not None else getattr(audio, "tags", None)
    out: Tags = {}

    def put(name: str, values: Iterable[str]) -> None:
        for value in values:
            if value and value not in out.setdefault(name, []):
                out[name].append(value)

    if tags is None:
        return out
    kind = family(audio) or ("id3" if isinstance(tags, ID3) else "")
    reverse = _REVERSE.get(kind, {})
    if kind == "vorbis":
        for key, value in tags:
            key = key.lower()
            if key in ("metadata_block_picture", "coverart", "coverartmime"):
                continue
            put(_TOTALS_IN.get(key, key), [value])
    elif kind == "id3":
        for frame in tags.values():
            frame_id = frame.FrameID
            if frame_id in ("TRCK", "TPOS"):
                logical = "tracknumber" if frame_id == "TRCK" else "discnumber"
                number, total = _split(str(frame.text[0]) if frame.text else "")
                put(logical, [number])
                put(_NUMBERS[logical][4], [total])
            elif frame_id == "TXXX":
                desc = frame.desc
                logical = reverse.get(("txxx:" + desc).lower()) or desc.lower()
                put(logical, [str(t) for t in frame.text])
            elif frame_id == "USLT":
                put("lyrics", [str(frame.text)])
            elif frame_id == "COMM":
                # A comment with a description: by it ("comment:<description>"), as
                # TagLib names it.
                desc = frame.desc.lower()
                put("comment:" + desc if desc else "comment", [str(t) for t in frame.text])
            elif frame_id == "UFID":
                if frame.owner == "http://musicbrainz.org":
                    put("musicbrainz_trackid", [frame.data.decode("utf-8", "replace")])
            elif hasattr(frame, "text") and frame_id.lower() in reverse:
                put(reverse[frame_id.lower()], [str(t) for t in frame.text])
    elif kind == "mp4":
        for key, values in tags.items():
            values = values if isinstance(values, list) else [values]
            if key in ("trkn", "disk"):
                logical = "tracknumber" if key == "trkn" else "discnumber"
                number, total = values[0]
                put(logical, [str(number)] if number else [])
                put(_NUMBERS[logical][4], [str(total)] if total else [])
            elif key == "covr":
                continue
            elif key == "cpil":
                put("compilation", ["1"] if values[0] else [])
            elif key in reverse:
                put(reverse[key], [tagging._mp4_text(v) for v in values])
            elif key.startswith("----:"):
                name = key.split(":", 2)[-1]
                put(name.lower(), [tagging._mp4_text(v) for v in values])
    elif kind in ("ape", "asf"):
        for key, value in tags.items():
            values = value if isinstance(value, list) else [value]
            texts = [t for v in values for t in _text(v)]
            lowered = key.lower()
            if kind == "ape" and lowered in ("track", "disc"):
                logical = "tracknumber" if lowered == "track" else "discnumber"
                number, total = _split(texts[0] if texts else "")
                put(logical, [number])
                put(_NUMBERS[logical][4], [total])
            elif kind == "asf" and key in ("WM/TrackNumber", "WM/PartOfSet"):
                logical = "tracknumber" if key == "WM/TrackNumber" else "discnumber"
                number, total = _split(texts[0] if texts else "")
                put(logical, [number])
                put(_NUMBERS[logical][4], [total])
            elif not texts or key == "WM/Picture" or lowered.startswith("cover art"):
                continue
            elif kind == "ape":
                named = _APE_NAMES.get(key.upper(), key.upper()).lower()
                put(reverse.get(lowered) or reverse.get(named) or _TOTALS_IN.get(named, named),
                    texts)  # fmt: skip
            elif lowered in reverse:
                put(reverse[lowered], texts)
            elif "/" not in key:  # ASF's own technical attributes ("WM/...") stay behind
                put(_TOTALS_IN.get(lowered, lowered), texts)
    return {
        k: v
        for k, v in out.items()
        if v and k not in ENCODING and k.removeprefix("comment:") not in ENCODING
    }


_TOTALS_IN = {alias: total for total, alias in _TOTALS.items()}


def carry_same(original: Any, source: Any, audio: Any, target: Any, kind: str) -> bool:
    """Carry ``source`` (the imported file's tags) into ``target`` (the tags of ``audio``,
    of family ``kind``) item by item when both are of one family: ID3 frames with their
    descriptions and languages (comments, lyrics, user text and URL frames, ratings,
    private frames and the rest), Vorbis comments, MPEG-4 atoms (freeform ones under their
    own mean and name), APEv2 items - every one but pictures (``set_picture``) and what
    describes the imported file's encoding (``ENCODING``, ``_ID3_ENCODING``,
    ``_PRIV_ENCODING``). False when the families differ (or are ASF, carried by logical
    name): nothing is changed then."""
    if source is None:
        return False
    mine = family(original) or ("id3" if isinstance(source, ID3) else "")
    if mine != kind or kind not in ("id3", "vorbis", "mp4", "ape"):
        return False
    _clear(audio, target, kind)
    if kind == "id3":
        target.delall("APIC")
        for frame in source.values():
            if frame.FrameID in _ID3_ENCODING:
                continue
            if frame.FrameID in ("COMM", "TXXX") and frame.desc.lower() in ENCODING:
                continue
            if frame.FrameID == "PRIV" and frame.owner in _PRIV_ENCODING:
                continue
            target.add(copy.deepcopy(frame))
    elif kind == "vorbis":
        grouped: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for key, value in source:
            if key.lower() in _VORBIS_PICTURES or key.lower() in ENCODING:
                continue
            grouped.setdefault(names.setdefault(key.lower(), key), []).append(value)
        for key, values in grouped.items():
            target[key] = values
    elif kind == "mp4":
        for key, value in source.items():
            if key in _MP4_ENCODING or (
                key.startswith("----:") and key.split(":", 2)[-1].lower() in ENCODING
            ):
                continue
            target[key] = copy.deepcopy(value)
    else:
        for key, value in source.items():
            if key.lower().startswith("cover art") or key.lower() in ENCODING:
                continue
            target[key] = copy.deepcopy(value)
    return True


# --- writing --------------------------------------------------------------------------


def _clear(audio: Any, tags: Any, kind: str) -> None:
    if kind == "vorbis":
        tags.clear()
    elif kind == "id3":
        for key in [k for k in tags.keys() if not k.startswith("APIC")]:  # noqa: SIM118 - ASF tags iterate as pairs
            del tags[key]
    elif kind == "mp4":
        for key in [k for k in tags.keys() if k != "covr"]:  # noqa: SIM118 - ASF tags iterate as pairs
            del tags[key]
    elif kind == "asf":
        for key in [k for k in tags.keys() if k != "WM/Picture"]:  # noqa: SIM118 - ASF tags iterate as pairs
            del tags[key]
    elif kind == "ape":
        for key in [k for k in tags.keys() if not k.lower().startswith("cover art")]:  # noqa: SIM118 - ASF tags iterate as pairs
            del tags[key]


def _name(logical: str, kind: str) -> str:
    """Where ``kind`` keeps ``logical`` ("" when it has no place for it)."""
    names = _TABLE.get(logical)
    if names is not None:
        return names[_N.index(kind)]
    if kind == "vorbis":
        return logical
    if kind == "id3":
        return "TXXX:" + logical.upper()
    if kind == "mp4":
        return _ITUNES + logical.upper()
    return logical.upper()


def write(audio: Any, tags: Any, kind: str, values: Mapping[str, Sequence[str]]) -> None:
    """Put ``values`` (logical names, as ``metadata`` gives them) into ``tags`` of family
    ``kind``, keeping only pictures of what was there."""
    _clear(audio, tags, kind)
    numbers = {name: [str(v) for v in values.get(name, [])] for name in ("tracknumber",
               "tracktotal", "discnumber", "disctotal")}  # fmt: skip
    for logical, wanted in values.items():
        texts = [str(v) for v in wanted if str(v)]
        if not texts or logical in numbers:
            continue
        name = _name(logical, kind)
        if not name:
            continue
        if kind == "vorbis":
            tags[name] = texts
        elif kind == "id3":
            if name == "USLT":
                tags.add(USLT(encoding=3, lang="eng", desc="", text="\n".join(texts)))
            elif name == "COMM":
                tags.add(COMM(encoding=3, lang="eng", desc="", text=texts))
            elif logical.startswith("comment:"):
                tags.add(COMM(encoding=3, lang="eng", desc=logical[8:], text=texts))
            elif name.startswith("TXXX:"):
                tags.add(TXXX(encoding=3, desc=name[5:], text=texts))
            else:
                tags.add(Frames[name](encoding=3, text=texts))
        elif kind == "mp4":
            if name in ("tmpo", "rtng"):
                if texts[0].strip().isdigit():
                    tags[name] = [int(texts[0])]
            elif name == "cpil":
                tags[name] = texts[0] not in ("", "0")
            elif name.startswith("----:"):
                tags[name] = [MP4FreeForm(t.encode()) for t in texts]
            else:
                tags[name] = texts
        else:
            tags[name] = texts
    for logical, (frame, atom, ape, asf, total_name) in _NUMBERS.items():
        number = (numbers[logical] or [""])[0]
        total = (numbers[total_name] or [""])[0]
        if not number and not total:
            continue
        if kind == "vorbis":
            if number:
                tags[logical] = [number]
            if total:
                tags[total_name] = [total]
        elif kind in ("id3", "ape"):
            text = number + (f"/{total}" if total else "")
            if kind == "id3":
                tags.add(Frames[frame](encoding=3, text=[text]))
            else:
                tags[ape] = [text]
        elif kind == "mp4":
            tags[atom] = [(_int(number), _int(total))]
        elif kind == "asf":
            if number:
                tags[asf] = [number]
            if total:
                tags[total_name.upper()] = [total]


def _int(value: str) -> int:
    return int(value) if value.isdigit() else 0


# The album-level tags a copy that joins an album takes from it (track and disc totals are
# the file's own), and every name Navidrome reads them under.
COPIED = {name: aliases for name, aliases in tagging.ALBUM_TAGS.items()
          if name not in ("tracktotal", "disctotal")}  # fmt: skip
ALIASES = frozenset(alias for aliases in COPIED.values() for alias in aliases)


def replace_album(audio: Any, tags: Any, kind: str, wanted: Mapping[str, Sequence[str]]) -> None:
    """A copy that joins an album: every tag Navidrome reads as an album-level name
    (``COPIED``) goes, ``wanted``'s values come in under one name each."""
    aliases = ALIASES
    if kind == "id3":
        for frame in list(tags.values()):
            names = {frame.FrameID.lower()}
            if isinstance(frame, TXXX):
                names = {"txxx:" + frame.desc.lower(), frame.desc.lower()}
            if names & aliases:
                del tags[frame.HashKey]
    elif kind == "vorbis":
        for key in list(tags.keys()):
            if key.lower() in aliases:
                del tags[key]
    elif kind == "mp4":
        for key in list(tags.keys()):
            lowered = key.lower()
            if lowered in aliases or (
                lowered.startswith("----:") and lowered.rsplit(":", 1)[-1] in aliases
            ):
                del tags[key]
    elif kind == "asf":
        for key in list(tags.keys()):
            translated = _ASF_NAMES.get(key, "").lower()
            if key.lower() in aliases or translated in aliases:
                del tags[key]
    elif kind == "ape":
        for key in list(tags.keys()):
            if _APE_NAMES.get(key.upper(), key.upper()).lower() in aliases:
                del tags[key]
    for name, values in wanted.items():
        texts = [str(v) for v in values if str(v)]
        if not texts:
            continue
        logical = tagging.VORBIS_KEY.get(name, name)
        if kind == "mp4" and logical in ("date", "releasedate"):
            continue  # written together below, using the delivery writer's date mapping
        if kind == "asf" and logical == "date":
            continue  # ASF has no separate recording date; its year is also the release date
        place = _name(logical, kind)
        if not place:
            continue
        if kind == "vorbis":
            tags[place] = texts
        elif kind == "id3":
            if place.startswith("TXXX:"):
                tags.add(TXXX(encoding=3, desc=place[5:], text=texts))
            else:
                tags.add(Frames[place](encoding=3, text=texts))
        elif kind == "mp4":
            if place == "cpil":
                tags[place] = texts[0] not in ("", "0")
            elif place.startswith("----:"):
                tags[place] = [MP4FreeForm(t.encode()) for t in texts]
            else:
                tags[place] = texts
        else:
            tags[place] = texts

    if kind == "mp4":
        tagging.write_mp4_dates(tags, {tagging.VORBIS_KEY.get(k, k): v for k, v in wanted.items()})


def save(audio: Any, tags: Any, fileobj: Any) -> None:
    """Write the changed tags into ``fileobj`` (the whole file, open for reading and
    writing)."""
    fileobj.seek(0)
    if isinstance(audio, AAC):
        tags.save(fileobj, v2_version=4)
    elif isinstance(tags, ID3):
        audio.save(fileobj, v2_version=4)
    else:
        audio.save(fileobj)
    fileobj.flush()
    fileobj.seek(0)


def ensure_tags(audio: Any, fileobj: Any = None) -> Any:
    """The file's tag object, made when it has none."""
    if isinstance(audio, AAC):
        return standalone_id3(fileobj)
    if audio.tags is None:
        audio.add_tags()
    return audio.tags


def set_picture(audio: Any, tags: Any, kind: str, data: bytes | None) -> None:
    """The file's pictures replaced by ``data`` (a JPEG or PNG; None: none)."""
    mime = "image/png" if data and data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    if isinstance(audio, FLAC):
        audio.clear_pictures()
        if data:
            picture = Picture()
            picture.type, picture.mime, picture.data = 3, mime, data
            audio.add_picture(picture)
    elif kind == "vorbis":
        for key in ("metadata_block_picture", "coverart", "coverartmime"):
            if key in tags:
                del tags[key]
        if data:
            picture = Picture()
            picture.type, picture.mime, picture.data = 3, mime, data
            tags["metadata_block_picture"] = [base64.b64encode(picture.write()).decode()]
    elif kind == "id3":
        tags.delall("APIC")
        if data:
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))
    elif kind == "mp4":
        if "covr" in tags:
            del tags["covr"]
        if data:
            image = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(data, imageformat=image)]
    elif kind == "asf":
        if "WM/Picture" in tags:
            del tags["WM/Picture"]
        if data:
            # Type, size, MIME type and description (UTF-16, ended by a nul), then the data.
            packed = struct.pack("<bi", 3, len(data))
            packed += mime.encode("utf-16-le") + b"\0\0" + b"\0\0" + data
            tags["WM/Picture"] = [ASFByteArrayAttribute(packed)]
    elif kind == "ape":
        for key in [k for k in tags.keys() if k.lower().startswith("cover art")]:  # noqa: SIM118 - ASF tags iterate as pairs
            del tags[key]
        if data:
            tags["Cover Art (Front)"] = APEValue(b"cover\0" + data, BINARY)


# --- Matroska (ffmpeg) ------------------------------------------------------------------

# Navidrome's names for Matroska tags that differ from the tag's own (lower case).
_MATROSKA_NAMES = {"date_released": "releasedate", "date_recorded": "recordingdate"}
# Where a copy puts each album-level tag (others: the logical name in upper case).
_MATROSKA_PLACES = {"album": "ALBUM", "albumartist": "ALBUM_ARTIST",
                    "releasedate": "DATE_RELEASED", "recordingdate": "DATE_RECORDED"}  # fmt: skip
FFMPEG_SECONDS = 60


def matroska_raw(tags: Mapping[str, Any]) -> Tags:
    """Matroska tags (ffprobe's format tags) as lower-case names, as Navidrome gets them."""
    out: Tags = {}
    for key, value in tags.items():
        if not isinstance(value, str) or not value:
            continue
        name = key.lower()
        out.setdefault(_MATROSKA_NAMES.get(name, name), []).append(value)
    return out


def _ffprobe_tags(path: str) -> dict[str, str]:
    """ffprobe's format tags of a Matroska file (``/dev/fd/<n>``: that open descriptor)."""
    import json
    import subprocess

    fds = (int(path.rsplit("/", 1)[-1]),) if path.startswith("/dev/fd/") else ()
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", "matroska",
         "-i", path, "-show_entries", "format_tags", "-of", "json"],
        pass_fds=fds,
        capture_output=True,
        timeout=FFMPEG_SECONDS,
        check=True,
    )  # fmt: skip
    if len(result.stdout) > 1024**2:
        raise ValueError
    tags = json.loads(result.stdout).get("format", {}).get("tags", {})
    return {str(k): str(v) for k, v in tags.items()} if isinstance(tags, dict) else {}


def matroska_album_tags(path: str) -> Tags:
    """``album_tags`` of a Matroska file (a path Shijhon names: a library file, or
    ``/dev/fd/<n>`` of an open one)."""
    found = matroska_raw(_ffprobe_tags(path))
    merged = {name: tagging.mapped(found, aliases) for name, aliases in tagging.ALBUM_TAGS.items()}
    return {name: values for name, values in merged.items() if values}


def matroska_tags_at(path: str) -> Tags:
    """A Matroska file's tags as Navidrome gets them (``matroska_raw``), at ``path``."""
    return matroska_raw(_ffprobe_tags(path))


def matroska_place(name: str) -> str:
    """Where a Matroska file keeps the tag of logical (Vorbis) name ``name``."""
    if name == "date":  # (Vorbis's date is the recording date)
        return "DATE_RECORDED"
    return _MATROSKA_PLACES.get(name, tagging.VORBIS_KEY.get(name, name).upper())


# Tags of several values Navidrome 0.64.2 splits again from one Matroska value (its genre
# separators include ";"); ffmpeg writes one value of a name only (a repeated -metadata, or
# an FFMETADATA file's, keeps the last), and Navidrome splits no artists. Measured with the
# pinned release (``tests/acceptance/test_placeholder_engine.py``).
MATROSKA_JOINED = {"genre": ";"}


def matroska_carries(tags: Mapping[str, Sequence[str]]) -> bool:
    """Whether a Matroska file can carry ``tags`` (Vorbis keys) so that Navidrome reads every
    value: none but those of ``MATROSKA_JOINED`` has several."""
    return all(
        len([v for v in values if str(v)]) <= 1 or name in MATROSKA_JOINED
        for name, values in tags.items()
    )


def matroska_tagged(source: str, target: str, tags: Mapping[str, Sequence[str]]) -> None:
    """Delivered audio in Matroska (``placeholders/tags.write_delivered``): ffmpeg copies
    its streams as they are (no re-encoding) from ``source`` into ``target`` with ``tags``
    (Vorbis keys; several genres joined as Navidrome splits them) and none of its own - its
    audio only, as other delivered audio keeps no picture (a cover: Navidrome's own). Two
    local files only (the file protocol, Matroska in and out), under a deadline. Raises
    ``ValueError`` for tags it cannot carry (``matroska_carries``)."""
    import subprocess

    if not matroska_carries(tags):
        raise ValueError("Matroska cannot carry several values of these tags")
    metadata: list[str] = []
    for name, values in tags.items():
        texts = [str(v) for v in values if str(v)]
        if texts:
            joined = MATROSKA_JOINED.get(name, "").join(texts)
            metadata += ["-metadata", f"{matroska_place(name)}={joined}"]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file",
         "-f", "matroska", "-i", source, "-map", "0:a", "-c", "copy", "-map_metadata", "-1",
         "-map_chapters", "-1", *metadata, "-f", "matroska", target],
        capture_output=True,
        timeout=FFMPEG_SECONDS,
        check=True,
    )  # fmt: skip


def matroska_copy(source_fd: int, target_fd: int, wanted: Mapping[str, Sequence[str]]) -> None:
    """The copy of a Matroska file that joins an album: ffmpeg copies its streams as they
    are (no re-encoding) into ``target_fd``, every tag Navidrome reads as an album-level
    name gone, ``wanted``'s first values in their place. Only the two descriptors are open
    to ffmpeg (the file protocol on ``/dev/fd/<n>``), under a deadline."""
    import subprocess

    source, target = f"/dev/fd/{source_fd}", f"/dev/fd/{target_fd}"
    metadata: list[str] = []
    for key in _ffprobe_tags(source):
        if _MATROSKA_NAMES.get(key.lower(), key.lower()) in ALIASES:
            metadata += ["-metadata", f"{key}="]  # an empty value removes the tag
    for name, values in wanted.items():
        texts = [str(v) for v in values if str(v)]
        if texts and name in COPIED:
            place = _MATROSKA_PLACES.get(name, tagging.VORBIS_KEY.get(name, name).upper())
            metadata += ["-metadata", f"{place}={texts[0]}"]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file",
         "-f", "matroska", "-i", source, "-map", "0", "-c", "copy", "-map_metadata", "0",
         *metadata, "-f", "matroska", target],
        pass_fds=(source_fd, target_fd),
        capture_output=True,
        timeout=FFMPEG_SECONDS,
        check=True,
    )  # fmt: skip
