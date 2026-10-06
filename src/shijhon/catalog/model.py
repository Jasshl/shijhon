"""Catalog data as Shijhon uses it, independent of any catalog's API."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Twins(StrEnum):
    """Which of a clean/explicit pair to show (albums and songs alike)."""

    EXPLICIT = "explicit"
    CLEAN = "clean"
    BOTH = "both"


class ReleaseKind(StrEnum):
    ALBUM = "album"
    SINGLE = "single"
    EP = "ep"
    COMPILATION = "compilation"


# A catalog's key: the catalog part of every reference and ID (``sh.al.<key>.<id>``).
CATALOG_KEY = re.compile(r"[a-z][a-z0-9]*")
# An item's ID, the catalog's own: letters, digits and dots (an ID with other characters
# is one the adapter must translate). The length leaves room for a reference through the
# item (``item_artist``) inside an ID clients are given.
ITEM_ID = re.compile(r"[A-Za-z0-9.]{1,190}")


@dataclass(frozen=True)
class CatalogRef:
    """An item in a catalog, e.g. ``CatalogRef("demo", "900000001")``."""

    catalog: str
    id: str

    def __str__(self) -> str:
        return f"{self.catalog}:{self.id}"

    @classmethod
    def parse(cls, text: str) -> CatalogRef:
        catalog, _, item = text.partition(":")
        if not catalog or not item:
            raise ValueError(f"not a catalog reference: {text!r}")
        return cls(catalog, item)


_ITEM_ARTIST = re.compile(r"([ta])-([A-Za-z0-9.]+)-(\d{1,3})")


def item_artist(item: CatalogRef, kind: str, index: int) -> CatalogRef:
    """The artist credited ``index``-th on a song (``kind`` "t") or an album ("a") whose
    catalog item named no artist item here (search results carry none): resolved from
    that item when used (``catalog.cache``)."""
    return CatalogRef(item.catalog, f"{kind}-{item.id}-{index}")


def parse_item_artist(artist_id: str) -> tuple[str, str, int] | None:
    """(kind, item ID, index) of a reference made by ``item_artist``; None otherwise."""
    match = _ITEM_ARTIST.fullmatch(artist_id)
    return (match.group(1), match.group(2), int(match.group(3))) if match else None


@dataclass(frozen=True)
class CatalogTrack:
    ref: CatalogRef
    title: str
    artist: str  # display credit as the catalog gives it, e.g. "A feat. B"
    duration_ms: int
    disc: int = 1
    number: int = 0
    isrc: str | None = None
    explicit: bool = False
    clean: bool = False  # a clean version of an explicit recording
    album: CatalogRef | None = None
    artists: tuple[str, ...] = ()  # individual artists, when the catalog lists them
    artist_refs: tuple[CatalogRef, ...] = ()  # the artists' catalog items, when known
    album_title: str | None = None
    genres: tuple[str, ...] = ()
    release_date: str | None = None
    # The cover's address with ``{w}`` and ``{h}`` where its size in pixels goes
    # (``artwork_url``); an address without them is used as it is.
    artwork_template: str | None = field(default=None, repr=False)

    @property
    def seconds(self) -> int:
        """Whole seconds for the placeholder's length (at least one)."""
        return max(1, round(self.duration_ms / 1000))


@dataclass(frozen=True)
class CatalogRelease:
    ref: CatalogRef
    title: str
    artist: str  # album artist credit as the catalog gives it
    kind: ReleaseKind
    release_date: str | None  # "YYYY" or "YYYY-MM-DD" as the catalog gives it
    tracks: tuple[CatalogTrack, ...] = ()
    explicit: bool = False
    clean: bool = False  # a clean edition of an explicit release
    genres: tuple[str, ...] = ()
    label: str | None = None
    upc: str | None = None
    track_count: int | None = None
    artists: tuple[str, ...] = ()  # individual album artists, when listed
    artist_refs: tuple[CatalogRef, ...] = ()  # the album artists' catalog items
    artwork_template: str | None = field(default=None, repr=False)
    # A track of it could not be read (skipped): shown, never filled into an owned album.
    incomplete: bool = False
    # Its tracks' disc and track numbers are the catalog's own. False: the catalog sent
    # none, and they are counted in the order it listed the tracks - filled into an owned
    # album only when the owned files are numbered the same way (else: review).
    numbered: bool = True

    @property
    def year(self) -> int | None:
        return _year(self.release_date)


@dataclass(frozen=True)
class CatalogArtist:
    ref: CatalogRef
    name: str
    artwork_template: str | None = field(default=None, repr=False)


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple | list):
        return [_encode(v) for v in value]
    return value


def release_data(release: CatalogRelease) -> dict[str, Any]:
    """A release as plain JSON-ready data (``release_from_data`` reads it back)."""
    data: dict[str, Any] = _encode(release)
    return data


def track_data(track: CatalogTrack) -> dict[str, Any]:
    """A track as plain JSON-ready data (``track_from_data`` reads it back)."""
    data: dict[str, Any] = _encode(track)
    return data


def artist_data(artist: CatalogArtist) -> dict[str, Any]:
    """An artist as plain JSON-ready data (``artist_from_data`` reads it back)."""
    data: dict[str, Any] = _encode(artist)
    return data


def _ref(value: Any) -> CatalogRef:
    return CatalogRef(str(value["catalog"]), str(value["id"]))


def _refs(values: Any) -> tuple[CatalogRef, ...]:
    return tuple(_ref(v) for v in values or ())


def track_from_data(value: dict[str, Any]) -> CatalogTrack:
    known = {f.name for f in dataclasses.fields(CatalogTrack)}
    fields = {k: v for k, v in value.items() if k in known}
    fields["ref"] = _ref(value["ref"])
    fields["album"] = _ref(value["album"]) if value.get("album") else None
    fields["artist_refs"] = _refs(value.get("artist_refs"))
    fields["artists"] = tuple(value.get("artists") or ())
    fields["genres"] = tuple(value.get("genres") or ())
    return CatalogTrack(**fields)


def release_from_data(data: dict[str, Any]) -> CatalogRelease:
    known = {f.name for f in dataclasses.fields(CatalogRelease)}
    fields = {k: v for k, v in data.items() if k in known}
    fields["ref"] = _ref(data["ref"])
    fields["kind"] = ReleaseKind(data["kind"])
    fields["tracks"] = tuple(track_from_data(t) for t in data.get("tracks") or ())
    fields["artist_refs"] = _refs(data.get("artist_refs"))
    fields["artists"] = tuple(data.get("artists") or ())
    fields["genres"] = tuple(data.get("genres") or ())
    return CatalogRelease(**fields)


def artist_from_data(data: dict[str, Any]) -> CatalogArtist:
    return CatalogArtist(_ref(data["ref"]), str(data["name"]), data.get("artwork_template"))


def _year(date: str | None) -> int | None:
    try:
        return int(date[:4]) if date else None
    except ValueError:
        return None


def artwork_url(template: str | None, size: int) -> str | None:
    """A catalog artwork template (``{w}``/``{h}`` placeholders) at ``size`` pixels: the
    address ``Catalog.artwork`` is asked for, and the one clients are given for an
    artist's image. Anything else a catalog's addresses need is its adapter's to fill in
    before it hands the template out."""
    if not template:
        return None
    return template.replace("{w}", str(size)).replace("{h}", str(size))
