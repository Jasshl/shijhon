"""Whether Navidrome 0.64.2 would put two files into one album (its persistent album ID).

Navidrome tells albums apart by the first MusicBrainz album ID, else by the album artist
and album tags (letter case and Unicode quotation marks and dashes aside), the album
version tag (letter case counted) and the release date tag as written (a file with only a
recording date has none) - measured with the pinned release (``fill/fills.py``
``_would_group``). The album artist is what Navidrome shows as the album's artist: the
album artist tag's values (several joined by " \u2022 "), else the multi-valued album artists
tag's, else - for a compilation (its compilation tag true) - "Various Artists", else the
first of the track's artists: the first value of the multi-valued artists tag, else of the
artist tag, which Navidrome splits when it has one value (" / ", " feat. ", "; " and the
like, in any letter case: ``ARTIST_SPLIT``), while the first value of an artists list is
taken whole. So a file that gains an artists list without an album artist tag joins
another album ("A, B" becomes "A"), unless its album artist is set to what Navidrome
showed. Measured with the pinned release (``tests/unit/test_grouping.py`` states it,
``tests/acceptance/test_D_album_artist.py`` and the artist lists' acceptance tests check
it). Tags are logical names with merged alias values,
as ``placeholders/tags.py`` reads them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Navidrome 0.64.2's str.Clear: the quotation marks and dashes it makes plain.
_SINGLE = (0x2018, 0x2019, 0x201B, 0x2032)
_DOUBLE = (0xFF02, 0x3003, 0x02EE, 0x05F2, 0x1CD3, 0x2033, 0x2036, 0x02F6, 0x02BA, 0x201C,
           0x201D, 0x02DD, 0x201F)  # fmt: skip
_DASHES = (0x2010, 0x2013, 0x2014, 0x2212, 0x2015)
_CLEAR = {c: "'" for c in _SINGLE} | {c: '"' for c in _DOUBLE} | {c: "-" for c in _DASHES}
# The album artist Navidrome gives a compilation without an album artist tag.
VARIOUS_ARTISTS = "Various Artists"
# How Navidrome joins several album artists into the name it shows.
ARTIST_JOINER = " \u2022 "
# What Navidrome 0.64.2 splits an artist tag of one value on (its default configuration),
# in any letter case ("Feat.", "FT" split as "feat.", "ft" do: measured).
ARTIST_SPLIT = (" / ", " feat. ", " feat ", " ft. ", " ft ", "; ")
_SPLIT = re.compile("|".join(map(re.escape, ARTIST_SPLIT)), re.IGNORECASE)


def identity(tag: Any) -> str:
    """An album or album artist tag as Navidrome's album identity compares it:
    str.Clear(strings.ToLower(tag)), lower case rune by rune; spaces at either end
    aside."""
    lowered = "".join(ch.lower()[:1] or ch for ch in str(tag or ""))
    return lowered.translate(_CLEAR).strip()


def _first(tags: Mapping[str, Sequence[str]], name: str) -> str:
    values = tags.get(name) or []
    return str(values[0]) if values else ""


def compilation(tags: Mapping[str, Sequence[str]]) -> bool:
    """Whether the compilation tag says so (Navidrome reads it as Go's ParseBool does)."""
    return _first(tags, "compilation").strip() in ("1", "t", "T", "true", "TRUE", "True")


def first_artist(artist: Sequence[str], artists: Sequence[str] = ()) -> str:
    """The first of a file's track artists as Navidrome reads them: the first value of
    its multi-valued artists tag, else of its artist tag - split on ``ARTIST_SPLIT`` when
    it has one value."""
    listed = [str(v) for v in artists if str(v).strip()]
    if listed:
        return listed[0]
    values = [str(v) for v in artist if str(v).strip()]
    if len(values) != 1:
        return values[0] if values else ""
    return _split_first(values[0])


def _split_first(value: str) -> str:
    """The first part of an artist tag's one value, as Navidrome splits it: empty parts are
    dropped ("; Bell" is "Bell", measured)."""
    parts = _SPLIT.split(value)
    if len(parts) == 1:
        return value
    return next((p.strip() for p in parts if p.strip()), value)


def album_artist(tags: Mapping[str, Sequence[str]], track_artist: str = "") -> str:
    """The album artist Navidrome gives a file with these album-level tags
    (``track_artist``: the file's first track artist as ``first_artist`` gives it, or the
    album artist Navidrome shows; used as it is, not split again: the first value of an
    artists list such as "Ash / Bell" stays whole)."""
    for name in ("albumartist", "albumartists"):
        values = [str(v) for v in tags.get(name) or [] if str(v)]
        if values:
            return ARTIST_JOINER.join(values)
    if compilation(tags):
        return VARIOUS_ARTISTS
    return track_artist


def album_key(tags: Mapping[str, Sequence[str]], track_artist: str = "") -> tuple[str, ...]:
    """What Navidrome's album ID is made of, for these album-level tags."""
    mbid = _first(tags, "musicbrainz_albumid").strip()
    if mbid:
        return ("mbid", mbid.lower())
    artist = album_artist(tags, track_artist)
    return (
        "tags",
        identity(artist),
        identity(_first(tags, "album")),
        _first(tags, "albumversion"),
        _first(tags, "releasedate"),
    )


def same_native_album(
    a: Mapping[str, Sequence[str]],
    b: Mapping[str, Sequence[str]],
    *,
    a_artist: str = "",
    b_artist: str = "",
) -> bool:
    return album_key(a, a_artist) == album_key(b, b_artist)
