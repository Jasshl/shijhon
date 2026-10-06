"""The typed catalog interface that every catalog adapter implements.

Adapters only fetch and translate; caching, request coalescing and edition
de-duplication are shared (``cache.py``, ``editions.py``). Shijhon ships no adapter for a
third-party catalog, no catalog credentials and no token service: adapters are
separate packages (``plugin.py``), and users configure a catalog themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
)


class CatalogError(Exception):
    """kind: not_found | unauthorized | rate_limited | unavailable | invalid"""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind}: {reason}")
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True)
class SearchResults:
    artists: tuple[CatalogArtist, ...] = ()
    albums: tuple[CatalogRelease, ...] = ()  # without tracks
    songs: tuple[CatalogTrack, ...] = ()


class Catalog(Protocol):
    """One catalog, in one of its regions when it has several. IDs are the catalog's
    own: letters, digits and dots (``model.ITEM_ID``).

    ``key`` is the catalog part of every reference and ID Shijhon keeps (``CatalogRef``,
    ``sh.al.<key>.<id>``): lower-case letters and digits, starting with a letter, and never
    changed once a library holds its items. ``region`` is the market the answers are for
    (what is available differs between them), "" for a catalog without regions; matches
    and saved lists are kept per key and region (``scope``).

    Every failure is a ``CatalogError`` with its kind and a short reason - never an
    address, a token or another secret (reasons are logged and shown). A catalog may also
    have ``async def check(self) -> None``: one small request past any cache, for the
    dashboard's "Check now" (without it, a search is made); and, when its songs do not all
    name their album (``CatalogTrack.album`` None), ``async def album_of(self, song_id,
    album) -> CatalogRef | None``: the album of such a song, asked for when the song is
    added to the library (it may cost requests, within a time of its own; ``album``: how to
    open an album meanwhile - the cache's ``album``, so that the answer it checks is the
    one then written; None: not found - the song cannot be added).
    """

    key: str
    region: str

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        """Artists, albums (without tracks) and songs for ``term``, at most ``limit`` of
        each, in the catalog's own order."""
        ...

    async def album(self, album_id: str) -> CatalogRelease:
        """The release with all its tracks (ISRC, disc and track numbers, durations)."""
        ...

    async def song(self, song_id: str) -> CatalogTrack:
        """One track, with its album reference."""
        ...

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        """The tracks with this ISRC (on any release), with their album references; none
        for an ISRC nobody has, and from a catalog that cannot look songs up by ISRC
        (owned albums are then matched by their titles alone)."""
        ...

    async def artist(self, artist_id: str) -> CatalogArtist: ...

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        """The artist's albums, singles and EPs (without tracks), as the catalog lists
        them; edition de-duplication happens elsewhere."""
        ...

    async def top_songs(self, artist_id: str, limit: int = 10) -> tuple[CatalogTrack, ...]:
        """The artist's most popular songs, as the catalog ranks them, with their album
        references (at most ``limit``, fewer when the catalog offers fewer; none for an
        artist without any, and from a catalog without such a ranking)."""
        ...

    async def artists_of(
        self, songs: tuple[str, ...], albums: tuple[str, ...]
    ) -> dict[str, tuple[CatalogRef, ...]]:
        """The artist items of these songs and albums, keyed "songs:<id>" and
        "albums:<id>"; items without any are left out. Asked for what a search showed
        without artist items (``artist_refs`` empty); a catalog whose search answers
        name them is never asked, and may answer with nothing."""
        ...

    async def artwork(self, url: str) -> tuple[bytes, str]:
        """Image bytes and content type of a catalog artwork URL: an artwork template of
        one of its items with the size filled in (``model.artwork_url``). An address that
        is not the catalog's own image address is refused ("invalid"), never fetched.
        A raster image - JPEG, PNG, GIF or WebP: Shijhon reads the type from the bytes and
        serves nothing else (``artwork.raster_type``); a cover written into the library is a
        JPEG."""
        ...

    async def aclose(self) -> None: ...


def addresses_for_clients(catalog: Catalog) -> bool:
    """Whether clients may be given the catalog's artwork addresses as they are
    (``artistImageUrl``, the image fields of the album and artist info answers). A
    catalog that says ``client_artwork = False`` (optional; True without it) names
    addresses that only Shijhon's own artwork path may fetch (``Catalog.artwork``: its
    checks, its limits): its images reach clients by their cover art IDs alone."""
    return bool(getattr(catalog, "client_artwork", True))


def scope(catalog: Catalog) -> str:
    """What saved matches, plans and lists belong to: the catalog and its region
    ("<key>.<region>"; the key alone for a catalog without regions)."""
    return f"{catalog.key}.{catalog.region}" if catalog.region else catalog.key
