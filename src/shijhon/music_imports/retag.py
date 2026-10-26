"""Make the published copy of a received file that joins an existing album.

Run as a bounded subprocess like the probe: the received original is read through one
descriptor and never written; the copy is written through another. The copy keeps the
original's audio and track tags (title, artists, numbers, ISRC) and takes the album's
album-level tags (``placeholders/tags.py`` ``ALBUM_TAGS``, without the track and disc
totals): every name Navidrome reads one of them under goes, the album's values come in
under one name each - in every format Mutagen can write and Navidrome reads
(``tagmap.py``). When the album has no album artist tag (Navidrome then uses the track
artist, or "Various Artists" for a compilation), the copy gets the album's artist as its
album artist. The copy is accepted only when Navidrome's album identity of its tags
(``matching/grouping.py``) is the album's. Input on stdin: ``{"tags": {name: [values]},
"artist": album artist}``. Output: the copy's size and SHA-256.

With ``{"mode": "carry"}`` and a third descriptor, it makes an upgraded file instead
(``music_imports/upgrade.py``): the add-on audio read through that descriptor, in its own
format, with the received original's whole metadata carried over - item by item when
both are of one tag family (``tagmap.carry_same``: comments with their descriptions and
languages, every other frame or atom), else every text tag through the formats' own
names (``tagmap.metadata``: lyrics, BPM, numbers with their totals, sort names,
MusicBrainz identifiers, ReplayGain, any other); never what describes the original's own
encoding (``tagmap.ENCODING``) - and its first picture, in place of the add-on's own.
The upgraded file is accepted only when what Navidrome makes the song's identity of (its
album identity, title, disc and track numbers, MusicBrainz release track ID) reads back
the same; the swap then checks Navidrome's own read.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from mutagen.id3 import ID3

from shijhon.matching.grouping import album_key
from shijhon.music_imports import tagmap
from shijhon.placeholders import tags as tagging

COPIED = tagmap.COPIED
CHUNK = 1024 * 1024


def retag(
    source: Any, target: Any, wanted: Mapping[str, Sequence[str]], artist: str, suffix: str = ""
) -> None:
    while chunk := source.read(CHUNK):
        target.write(chunk)
    target.flush()
    target.seek(0)
    audio = tagmap.parse(target, suffix)
    kind = tagmap.family(audio)
    if audio is None or not kind:
        raise ValueError
    tags = {name: list(v) for name, v in wanted.items() if name in COPIED and v}
    if not tags.get("albumartist") and artist:
        tags["albumartist"] = [artist]
    found = tagmap.ensure_tags(audio, target)
    tagmap.replace_album(audio, found, kind, tags)
    tagmap.save(audio, found, target)
    copied = tagmap.parse(target, suffix)
    mine = tagmap.album_tags(copied, tagmap.tags_of(copied, target))
    if album_key(mine, "") != album_key(tags, artist):
        raise ValueError


def retag_matroska(
    source_fd: int, target_fd: int, wanted: Mapping[str, Sequence[str]], artist: str
) -> None:
    """``retag`` of a Matroska file, which Mutagen cannot write (``tagmap.matroska_copy``)."""
    tags = {name: list(v) for name, v in wanted.items() if name in COPIED and v}
    if not tags.get("albumartist") and artist:
        tags["albumartist"] = [artist]
    tagmap.matroska_copy(source_fd, target_fd, tags)
    mine = tagmap.matroska_album_tags(f"/dev/fd/{target_fd}")
    if album_key(mine, "") != album_key(tags, artist):
        raise ValueError


def _identity(audio: Any, tags: Any = None) -> tuple[Any, ...]:
    """What Navidrome makes a song's persistent identity of, as these tags read
    (``tags``: a raw AAC file's, which are not ``audio.tags``)."""
    from shijhon.music_imports.facts import _numbers

    raw = tagmap.raw(audio, tags)
    album = {name: tagging.mapped(raw, aliases) for name, aliases in COPIED.items()}
    title = tagging.mapped(raw, ("tit2", "title", "\xa9nam", "wm/title"))
    release_track = tagging.mapped(
        raw,
        (
            "txxx:musicbrainz release track id",
            "musicbrainz_releasetrackid",
            "----:com.apple.itunes:musicbrainz release track id",
            "musicbrainz/release track id",
        ),
    )
    artist = tagging.mapped(raw, ("tpe1", "artist", "\xa9art", "author"))
    return (
        album_key(album, artist[0] if artist else ""),
        title[0] if title else "",
        _numbers(audio, raw),
        release_track[0] if release_track else "",
    )


def carry(
    source: Any, audio: Any, target_fd: int, source_suffix: str = "", audio_suffix: str = ""
) -> None:
    """The upgraded file (module docstring) written through ``target_fd``."""
    from shijhon.music_imports.facts import picture

    original = tagmap.parse(source, source_suffix)
    if original is None:
        raise ValueError
    original_tags = tagmap.tags_of(original, source)
    values = tagmap.metadata(original, original_tags)
    image = picture(original)
    if image is None and isinstance(original_tags, ID3):  # a raw AAC file's own ID3
        frames = original_tags.getall("APIC")
        image = bytes(frames[0].data) if frames else None
    with os.fdopen(os.dup(target_fd), "r+b") as out:
        while chunk := audio.read(CHUNK):
            out.write(chunk)
        out.flush()
        out.seek(0)
        upgraded = tagmap.parse(out, audio_suffix)
        kind = tagmap.family(upgraded)
        if upgraded is None or not kind:
            raise ValueError
        found = tagmap.ensure_tags(upgraded, out)
        if not tagmap.carry_same(original, original_tags, upgraded, found, kind):
            tagmap.write(upgraded, found, kind, values)
        tagmap.set_picture(upgraded, found, kind, image)
        tagmap.save(upgraded, found, out)
        upgraded = tagmap.parse(out, audio_suffix)
        if upgraded is None or _identity(upgraded, tagmap.tags_of(upgraded, out)) != _identity(
            original, original_tags
        ):
            raise ValueError
        out.flush()
        os.fsync(out.fileno())


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    request = json.loads(sys.stdin.read(256 * 1024))
    if request.get("mode") == "carry":
        target_fd = int(sys.argv[2])
        with (
            os.fdopen(int(sys.argv[1]), "rb", buffering=0) as source,
            os.fdopen(int(sys.argv[3]), "rb", buffering=0) as audio,
        ):
            carry(
                source,
                audio,
                target_fd,
                str(request.get("source") or ""),
                str(request.get("audio") or ""),
            )
        os.lseek(target_fd, 0, os.SEEK_SET)
        with os.fdopen(target_fd, "rb") as target:
            hashed, size = hashlib.sha256(), 0
            while chunk := target.read(CHUNK):
                hashed.update(chunk)
                size += len(chunk)
        print(json.dumps({"size": size, "sha256": hashed.hexdigest()}))
        return
    wanted, artist = request["tags"], str(request.get("artist") or "")
    suffix = str(request.get("suffix") or "")
    if not isinstance(wanted, dict):
        raise ValueError
    with (
        os.fdopen(int(sys.argv[1]), "rb", buffering=0) as source,
        os.fdopen(int(sys.argv[2]), "r+b") as target,
    ):
        if suffix == "mka":
            retag_matroska(source.fileno(), target.fileno(), wanted, artist)
        else:
            retag(source, target, wanted, artist, suffix)
        os.fsync(target.fileno())
        target.seek(0)
        hashed, size = hashlib.sha256(), 0
        while chunk := target.read(CHUNK):
            hashed.update(chunk)
            size += len(chunk)
    print(json.dumps({"size": size, "sha256": hashed.hexdigest()}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)  # never emit tags or file details
