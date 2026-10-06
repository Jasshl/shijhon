"""Subsonic/OpenSubsonic JSON entries for catalog items, shaped like Navidrome 0.64.2's.

Durations are whole seconds as Navidrome reports them for the placeholder a track
becomes, so a client shows the same length before and after a commit.

``library`` (folded artist name -> native artist ID) links artists the library has to their
native IDs; other artists keep catalog IDs. Every entry carries the keys Navidrome
always emits for its type, with the same JSON types (strict OpenSubsonic clients
reject an answer otherwise): every artist reference has an ID - the library
artist's, else the catalog artist's, else a reference through the song or album that
credits it (``catalog.model.item_artist``), which the artist view resolves. A credit is
split only where the catalog lists as many artists, or on "feat.": a band such as "Salt,
Ash & Ember" stays one artist.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    artwork_url,
    item_artist,
)
from shijhon.matching.normalize import credit_names, featured_names, fold
from shijhon.views.ids import album_id, artist_id, song_id

log = logging.getLogger(__name__)
# Navidrome splits genre tags on these (its default), e.g. "Hip-Hop/Rap".
_GENRE_SPLIT = re.compile(r"\s*[;/,]\s*")
# A catalog artist's image (``artistImageUrl``): clients show it as a large banner, and the
# catalog's URL carries its size (Navidrome's own lets the client choose).
ARTIST_IMAGE_SIZE = 1200
Library = Mapping[str, str]  # folded artist name -> native artist ID
_NONE: Library = {}


_SKIPPED: set[tuple[str, str]] = set()  # items already logged as skipped


def each[T](
    items: Iterable[T], build: Callable[[T], dict[str, Any]], what: str
) -> list[dict[str, Any]]:
    """One entry for each item, each built on its own: an item that cannot be shown is
    skipped and logged (once: answers are built again from cached data on every request),
    never the whole answer."""
    entries: list[dict[str, Any]] = []
    for item in items:
        try:
            entries.append(build(item))
        except Exception as exc:
            ref = getattr(item, "ref", None)
            key = (what, str(ref) if ref is not None else type(exc).__name__)
            if key not in _SKIPPED:
                if len(_SKIPPED) > 4096:
                    _SKIPPED.clear()
                _SKIPPED.add(key)
                log.warning("catalog: skipped a malformed %s (%s)", what, type(exc).__name__)
    return entries


def seconds(duration_ms: int) -> int:
    return duration_ms // 1000


def _date(date: str | None) -> dict[str, int]:
    """A catalog date's valid parts (strict clients reject "2020-00-00")."""
    parts = [int(p) for p in (date or "").split("-")[:3] if p.isdigit()]
    out: dict[str, int] = {}
    if parts and 1 <= parts[0] <= 9999:
        out["year"] = parts[0]
        if len(parts) > 1 and 1 <= parts[1] <= 12:
            out["month"] = parts[1]
            if len(parts) > 2 and 1 <= parts[2] <= calendar.monthrange(parts[0], parts[1])[1]:
                out["day"] = parts[2]
    return out


def _created(date: str | None) -> str:
    parts = _date(date)
    if "year" not in parts:
        return "1970-01-01T00:00:00Z"
    return f"{parts['year']:04d}-{parts.get('month', 1):02d}-{parts.get('day', 1):02d}T00:00:00Z"


def _people(
    names: tuple[str, ...],
    refs: tuple[CatalogRef, ...],
    credit: str,
    library: Library,
    source: CatalogRef,
    kind: str,
    owner: tuple[str, tuple[CatalogRef, ...]] | None = None,
) -> list[dict[str, str]]:
    """Artist references, each with a name and an ID: the library artist of that name, else
    the catalog's artist item - paired by position when the catalog lists as many as
    the credit names, split on "feat." or else on every separator - else a reference through
    ``source``, the song ("t") or album ("a") crediting it. ``owner``: the album artist
    (credit and items) of a song, whose name gets its item."""

    def person(name: str, ref: CatalogRef | None, index: int) -> dict[str, str]:
        if ref is None and owner is not None and len(owner[1]) == 1:
            ref = owner[1][0] if fold(name) == fold(owner[0]) else None
        ident = library.get(fold(name)) or artist_id(ref or item_artist(source, kind, index))
        return {"id": ident, "name": name}

    if names:
        paired = len(refs) == len(names)
        return [person(n, refs[i] if paired else None, i) for i, n in enumerate(names)]
    if not credit:
        return [person("", refs[0], 0)] if refs else []
    featured = featured_names(credit) or [credit]
    for parts in (featured, credit_names(credit)):
        if len(parts) > 1 and len(parts) == len(refs):
            return [person(n, r, i) for i, (n, r) in enumerate(zip(parts, refs, strict=True))]
    if refs:  # one artist item for the credit ("Salt, Ash & Ember", or "A & B" as one)
        ident = library.get(fold(credit)) or library.get(fold(featured[0]))
        return [{"id": ident or artist_id(refs[0]), "name": credit}]
    return [person(name, None, i) for i, name in enumerate(featured)]


def _fake_path(artist: str, album: str, disc: int, number: int, title: str) -> str:
    """A path in Navidrome's style for a song without a file yet."""
    folder = "/".join(part.replace("/", "_") for part in (artist, album))
    return f"{folder}/{disc:02d}-{number:02d} - {title.replace('/', '_')}.flac"


def genre_names(genres: tuple[str, ...]) -> list[str]:
    """Genres as Navidrome lists them from the placeholder's tag."""
    out: list[str] = []
    for genre in genres:
        out += [g for g in _GENRE_SPLIT.split(genre) if g and g not in out]
    return out


def album_name(release: CatalogRelease) -> str:
    """The name Navidrome shows: a clean edition's version is appended."""
    return f"{release.title} (Clean)" if release.clean else release.title


def _explicit(explicit: bool, clean: bool) -> str:
    return "explicit" if explicit else "clean" if clean else ""


def album_entry(
    release: CatalogRelease, *, songs: bool = False, library: Library = _NONE
) -> dict[str, Any]:
    ident = album_id(release.ref)
    tracks = release.tracks
    artists = _people(
        release.artists, release.artist_refs, release.artist, library, release.ref, "a"
    )
    genres = genre_names(release.genres)
    entry: dict[str, Any] = {
        "id": ident,
        "name": album_name(release),
        "artist": release.artist,
        "coverArt": f"al-{ident}",
        "songCount": len(tracks) if tracks else release.track_count or 0,
        "duration": seconds(sum(t.duration_ms for t in tracks)),
        "created": _created(release.release_date),
        "year": release.year or 0,
        "genre": genres[0] if genres else "",
        "userRating": 0,
        "genres": [{"name": g} for g in genres],
        "musicBrainzId": "",
        "isCompilation": release.kind == ReleaseKind.COMPILATION,
        "sortName": release.title.lower(),
        "discTitles": [],
        "originalReleaseDate": {},
        "releaseDate": _date(release.release_date),
        "releaseTypes": [release.kind.value],  # as the placeholder's tag will say
        "recordLabels": [{"name": release.label}] if release.label else [],
        "moods": [],
        "artists": artists,
        "displayArtist": release.artist,
        "explicitStatus": _explicit(release.explicit, release.clean),
        "version": "Clean" if release.clean else "",
    }
    if artists:
        entry["artistId"] = artists[0]["id"]
    if songs:
        entry["song"] = each(tracks, lambda t: song_entry(t, release, library=library), "track")
        if len(entry["song"]) < len(tracks):  # the album as it is listed
            shown = {s["id"] for s in entry["song"]}
            entry["songCount"] = len(entry["song"])
            entry["duration"] = seconds(
                sum(t.duration_ms for t in tracks if song_id(t.ref) in shown)
            )
    return entry


def song_entry(
    track: CatalogTrack, release: CatalogRelease | None = None, *, library: Library = _NONE
) -> dict[str, Any]:
    """A song; ``release`` supplies album data (else the track's own album fields)."""
    album_ref = release.ref if release else track.album
    album_title = album_name(release) if release else track.album_title or ""
    album_artist = release.artist if release else ""
    genres = genre_names((release.genres if release else ()) or track.genres)
    date = release.release_date if release and release.release_date else track.release_date
    parts = _date(date)
    refs = track.artist_refs
    if not refs and release and track.artist == release.artist:
        refs = release.artist_refs
    owner = (release.artist, release.artist_refs) if release else None
    artists = _people(track.artists, refs, track.artist, library, track.ref, "t", owner)
    album_artists = (
        _people(release.artists, release.artist_refs, release.artist, library, release.ref, "a")
        if release
        else artists  # the album is not known here: its artist is most likely the song's
    )
    entry: dict[str, Any] = {
        "id": song_id(track.ref),
        "isDir": False,
        "title": track.title,
        "album": album_title,
        "artist": track.artist,
        "track": track.number,
        "year": parts.get("year", 0),
        "genre": genres[0] if genres else "",
        "size": 0,
        "contentType": "audio/flac",
        "suffix": "flac",
        "duration": seconds(track.duration_ms),
        "bitRate": 0,
        "path": _fake_path(
            album_artist or track.artist, album_title, track.disc, track.number, track.title
        ),
        "discNumber": track.disc,
        "created": _created(date),
        "type": "music",
        "bpm": 0,
        "comment": "",
        "sortName": track.title.lower(),
        "mediaType": "song",
        "musicBrainzId": "",
        "isrc": [track.isrc] if track.isrc else [],
        "genres": [{"name": g} for g in genres],
        "replayGain": {},
        "channelCount": 2,
        "samplingRate": 44100,
        "bitDepth": 16,
        "moods": [],
        "artists": artists,
        "displayArtist": track.artist,
        "albumArtists": album_artists,
        "displayAlbumArtist": album_artist or track.artist,
        "contributors": [],
        "displayComposer": "",
        "explicitStatus": _explicit(track.explicit, track.clean or bool(release and release.clean)),
        "groupings": [],
        "works": [],
        "movements": [],
    }
    if album_ref is not None:
        entry["parent"] = entry["albumId"] = album_id(album_ref)
        entry["coverArt"] = f"al-{album_id(album_ref)}"
    elif track.artwork_template:  # its album is not known: the song's own cover
        entry["coverArt"] = f"mf-{entry['id']}"
    if artists:
        entry["artistId"] = artists[0]["id"]
    return entry


def artist_entry(
    artist: CatalogArtist,
    releases: list[CatalogRelease] | None = None,
    *,
    library: Library = _NONE,
    addresses: bool = True,
) -> dict[str, Any]:
    """``addresses``: whether the artist's image address is given too (``artistImageUrl``;
    ``catalog.base.addresses_for_clients``) - the image itself is ``coverArt`` always."""
    ident = library.get(fold(artist.name)) or artist_id(artist.ref)
    entry: dict[str, Any] = {
        "id": ident,
        "name": artist.name,
        "coverArt": f"ar-{ident}",
        "albumCount": len(releases or []),
        "musicBrainzId": "",
        "sortName": artist.name.lower(),
        "roles": ["albumartist"],
    }
    image = artwork_url(artist.artwork_template, ARTIST_IMAGE_SIZE) if addresses else None
    if image:
        entry["artistImageUrl"] = image
    if releases is not None:
        entry["album"] = each(releases, lambda r: album_entry(r, library=library), "album")
    return entry


def image_urls(template: str | None) -> dict[str, str]:
    """``smallImageUrl``/``mediumImageUrl``/``largeImageUrl`` for artist and album info."""
    urls = {
        key: url
        for key, size in (("smallImageUrl", 300), ("mediumImageUrl", 600), ("largeImageUrl", 1200))
        if (url := artwork_url(template, size))
    }
    return urls


def owned_album_song(
    track: CatalogTrack,
    release: CatalogRelease,
    album: Mapping[str, Any],
    *,
    library: Library = _NONE,
) -> dict[str, Any]:
    """A catalog song shown in the owned album it will join: its album fields are
    the owned album's as Navidrome gives them (the placeholder copies them from an owned
    file), its own fields the catalog's."""
    entry = song_entry(track, release, library=library)
    ident = str(album.get("id") or "")
    name = str(album.get("name") or entry["album"])
    entry.update(parent=ident, albumId=ident, album=name)
    if isinstance(album.get("coverArt"), str):
        entry["coverArt"] = album["coverArt"]
    year = album.get("year")
    entry["year"] = year if isinstance(year, int) and not isinstance(year, bool) else 0
    genres = album.get("genres")
    if isinstance(genres, list) and all(isinstance(g, dict) for g in genres):
        entry["genres"] = [{"name": str(g.get("name") or "")} for g in genres]
        entry["genre"] = str(album.get("genre") or "")
    artists = album.get("artists")
    if isinstance(artists, list) and all(isinstance(a, dict) and a.get("id") for a in artists):
        entry["albumArtists"] = [{"id": str(a["id"]), "name": str(a.get("name") or "")}
                                 for a in artists]  # fmt: skip
    display = str(album.get("displayArtist") or album.get("artist") or "")
    if display:
        entry["displayAlbumArtist"] = display
    entry["path"] = _fake_path(display or track.artist, name, track.disc, track.number, track.title)
    return entry


def album_child(release: CatalogRelease, *, library: Library = _NONE) -> dict[str, Any]:
    """A catalog album as a directory entry (``getMusicDirectory`` of an artist), shaped
    like Navidrome's entries built from an album."""
    album = album_entry(release, library=library)
    artists = album["artists"]
    child: dict[str, Any] = {
        "id": album["id"],
        "parent": album.get("artistId", ""),
        "isDir": True,
        "title": album["name"],
        "name": album["name"],
        "album": album["name"],
        "artist": release.artist,
        "year": album["year"],
        "coverArt": album["coverArt"],
        "duration": album["duration"],
        "created": album["created"],
        "songCount": album["songCount"],
        "bpm": 0,
        "comment": "",
        "sortName": album["sortName"],
        "mediaType": "album",
        "musicBrainzId": "",
        "isrc": [],
        "genres": album["genres"],
        "replayGain": {},
        "channelCount": 0,
        "samplingRate": 0,
        "bitDepth": 0,
        "moods": [],
        "artists": artists,
        "displayArtist": release.artist,
        "albumArtists": artists,
        "displayAlbumArtist": release.artist,
        "contributors": [],
        "displayComposer": "",
        "explicitStatus": album["explicitStatus"],
        "groupings": [],
        "works": [],
        "movements": [],
    }
    if "artistId" in album:
        child["artistId"] = album["artistId"]
    if not child["year"]:
        del child["year"]  # Navidrome leaves out a zero year
    return child
