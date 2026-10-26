"""Whether Navidrome 0.64.2 would put two files into one album (its persistent album ID).

Navidrome tells albums apart by the first MusicBrainz album ID, else by the album artist
and album tags (letter case and Unicode quotation marks and dashes aside), the album
version tag (letter case counted) and the release date tag as written (a file with only a
recording date has none) - measured with the pinned release (``fill/fills.py``
``_would_group``). The album artist is the album artist tag, else - for
a compilation (its compilation tag true) - "Various Artists", else the track artist. Tags
are logical names with merged alias values, as ``placeholders/tags.py`` reads them.
"""

from __future__ import annotations

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


def album_artist(tags: Mapping[str, Sequence[str]], track_artist: str = "") -> str:
    """The album artist Navidrome gives a file with these album-level tags."""
    return _first(tags, "albumartist") or (VARIOUS_ARTISTS if compilation(tags) else track_artist)


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
