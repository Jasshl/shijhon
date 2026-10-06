"""Catalog IDs in every method.

Clients keep the IDs they were given and use them anywhere: a catalog song from an album
view, a search result or a saved queue comes back in lyrics, similar songs, directories,
bookmarks. Methods with handlers of their own (views, commits, search) translate catalog
IDs themselves; for every other method a request carrying a catalog ID in any ID
parameter is rewritten here: an item in the library gets its native ID, whatever the
method. For an item that is not in the library, the read-only methods whose answer may be
empty get Navidrome's own empty answer (no lyrics, no similar songs); any other method goes
to Navidrome as it is and gets Navidrome's answer (its error). Credentials first.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from shijhon.catalog.base import Catalog, CatalogError
from shijhon.matching.normalize import fold
from shijhon.proxy.app import Forward, HandlerResult, RequestContext
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import empty_answer
from shijhon.views.ids import CatalogId
from shijhon.views.library_artists import LibraryArtists
from shijhon.views.virtual import Materialized

log = logging.getLogger(__name__)
# Read-only methods and the element Navidrome answers with when it has nothing.
EMPTY = {
    "getLyricsBySongId": "lyricsList",
    "getSimilarSongs": "similarSongs",
    "getSimilarSongs2": "similarSongs2",
}


def id_key(key: str) -> bool:
    """A parameter that carries an item ID (``id``, ``albumId``, ``songIdToAdd``,
    ``current``...), not text (a query, a name, a comment)."""
    return key in ("id", "current") or key.endswith(("Id", "IdToAdd"))


class CatalogIds:
    def __init__(
        self,
        materialized: Materialized,
        catalog: Catalog | None,
        *,
        library_artists: LibraryArtists | None = None,
        server: Callable[[], dict[str, object]] = dict,
    ) -> None:
        self.materialized = materialized
        self.catalog = catalog
        self.library_artists = library_artists
        self.server = server  # Navidrome's envelope fields
        # A release matched to an owned album shown complete: that album's ID.
        self.owner: Callable[[str], Awaitable[str | None]] | None = None

    async def handle(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        wanted = {v for k, v in call.params if id_key(k) and CatalogId.parse(v)}
        if not wanted:
            return None
        if await ctx.caller() is None:
            return None  # Navidrome answers with its own credential error
        mapping: dict[str, str] = {}
        for text in sorted(wanted):
            cid = CatalogId.parse(text)
            assert cid is not None
            native = await self.native(cid)
            if native is not None:
                mapping[text] = native
        if len(mapping) < len(wanted) and call.name in EMPTY:
            return empty_answer(call, EMPTY[call.name], self.server())
        if not mapping:
            return None
        return Forward(call.rewritten(lambda k, v: mapping.get(v) if id_key(k) else None))

    async def native(self, cid: CatalogId) -> str | None:
        """The native ID of a catalog item in the library, else None."""
        if cid.kind == "tr":
            return await self.materialized.song(cid.ref)
        if cid.kind == "al":
            found = await self.materialized.album(cid.ref)
            if found is None and self.owner is not None:
                found = await self.owner(str(cid.ref))
            return found
        catalog = self.catalog
        if catalog is None or cid.ref.catalog != catalog.key:
            return None
        if self.library_artists is None:
            return None
        try:
            artist = await catalog.artist(cid.ref.id)
        except CatalogError as exc:
            log.info("catalog artist unavailable: %s", exc.reason)
            return None
        return (await self.library_artists.ids()).get(fold(artist.name))
