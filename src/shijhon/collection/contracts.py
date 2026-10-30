"""Caller-verified collection boundary shared by the workspace and visibility adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from shijhon.catalog.model import CatalogRef

Mode = Literal["saved", "original", "all"]
# The selection of an account that gets real audio only ([placeholders]
# real_audio_accounts): every song whose audio is in the library, whatever the
# account's library choices.
REAL_AUDIO: Final = "real_audio"
Selection = Literal["saved", "original", "all", "real_audio"]
BrowseView = Literal["saved", "original", "all", "unsaved"]
Scope = Literal["song", "album", "artist"]
MODES: tuple[Mode, ...] = ("saved", "original", "all")
BROWSE_VIEWS: tuple[BrowseView, ...] = (*MODES, "unsaved")


class CollectionError(ValueError):
    """A safe, user-facing collection refusal."""


class NativeAbsent(CollectionError):
    """Navidrome, asked by a currently verified caller, says the song is not in the
    library (not found, or kept as missing): verified absence, not an access failure."""


@dataclass(frozen=True)
class Actor:
    owner_id: str
    library_ids: frozenset[str]


class Access(Protocol):
    """Revalidate immutable identity and current grants; fetch ONLY as that caller.

    Implementations must refuse unknown/recreated/revoked identities. An unavailable
    identity check must raise, never return remembered grants. ``songs`` returns native
    song dictionaries including id, albumId, artistId, title, album, artist and path.
    It must constrain the native request to library_id and use stable ID ordering.
    """

    async def authorize(self, owner_id: str) -> Actor: ...

    async def songs(
        self, owner_id: str, library_id: str, offset: int, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


class AssetLookup(Protocol):
    """Trusted ledger lookup, matching exact native library-relative asset paths.

    Returns native song ID -> durable asset ID for this bounded source page. Never
    derive these IDs from browser fields or arbitrary path prefixes. This only
    restores structural aliases; it neither records nor infers original ownership.
    """

    async def __call__(
        self, library_id: str, songs: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, str]: ...


class ArtworkAccess(Protocol):
    """Optional caller-authenticated, fixed-upstream cover fetch.

    Revalidate current grants, fetch only configured native getCoverArt IDs, cap the
    streamed response at 5 MiB and do not follow redirects or arbitrary addresses.
    The collection service independently verifies the ID, raster bytes and MIME.
    No reusable credentials enter browser URLs or the shared metadata cache.
    """

    async def artwork(self, owner_id: str, library_id: str, ident: str) -> tuple[bytes, str]: ...


class Restore(Protocol):
    """Restore/reuse at most 250 catalog tracks for an approved Save batch.

    Use the existing verified publisher, then fetch native songs as this caller in
    this library. Return str(catalog ref) -> native structural song dictionaries.
    Omitted refs remain unavailable. Never substitute service-account media reads.
    The collection verifies identities against its trusted placeholder ledger.
    Calls are retryable and must be idempotent; browsing/review never calls this.
    Compose ``authorize`` with fresh caller checks at every publisher write gate,
    after waits and before media effects. It revalidates the approved frozen intent;
    an earlier successful check does not authorize a later write.
    """

    async def __call__(
        self,
        actor: Actor,
        library_id: str,
        refs: Sequence[CatalogRef],
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> Mapping[str, Mapping[str, Any]]: ...


@dataclass(frozen=True)
class Predicate:
    """Parameterized SQL for a native-song ID expression chosen by trusted code.

    References only collection tables in the same SQLite connection (or an attached
    database whose alias the adapter supplies). Native permission predicates MUST be
    intersected by the caller. Never post-filter an already paginated native page.
    """

    sql: str
    params: tuple[str | None, ...]


@dataclass(frozen=True)
class IndexState:
    library_id: str
    generation: str
    state: str
    indexed: int
    updated_at: float
    error: str


@dataclass(frozen=True)
class Query:
    view: BrowseView = "saved"
    scope: Scope = "album"
    search: str = ""
    # The Collection page's sources: "imported" (an imported file, or a song the owner's
    # import was linked to) and "catalog" (every other song). 'original', 'own' and
    # 'shared' remain only for the recorded changes that used them.
    origin: Literal["all", "imported", "catalog", "original", "own", "shared"] = "all"
    sort: Literal["title", "artist"] = "artist"
    album_id: str = ""
    artist_id: str = ""
    offset: int = 0
    limit: int = 40
    # Old bookmarks and frozen operations omit this field and retain their global pool.
    # New dashboard requests explicitly choose personal independently of the profile.
    pool: Literal["personal", "server"] = "server"

    def __post_init__(self) -> None:
        if (
            self.view not in BROWSE_VIEWS
            or self.pool not in ("personal", "server")
            or self.scope not in ("song", "album", "artist")
            or self.origin not in ("all", "imported", "catalog", "original", "own", "shared")
            or self.sort not in ("title", "artist")
            or not 1 <= self.limit <= 100
            or not 0 <= self.offset <= 10_000_000
            or len(self.search) > 200
            or len(self.album_id) > 300
            or len(self.artist_id) > 300
        ):
            raise CollectionError("Choose a valid collection filter and page.")
