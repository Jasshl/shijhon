"""Read-only virtual views of catalog items.

``getAlbum``, ``getSong``, ``getArtist``, ``getArtistInfo(2)``, ``getAlbumInfo(2)``,
``getMusicDirectory`` and ``getCoverArt`` answer for catalog IDs (``views/ids.py``)
without creating anything:
clients prefetch every album they list. An item that has been materialized is Navidrome's:
its ID is rewritten to the native one and the request forwarded. Everything else is
forwarded untouched, and the caller's credentials are checked before any work.
Catalog views answer in JSON and in XML, written from the same entries; JSONP is
forwarded. Artists the library has are linked by their native IDs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import anyio

from shijhon.catalog.artwork import ArtworkCache, ArtworkIndex, raster_type
from shijhon.catalog.base import Catalog, CatalogError, addresses_for_clients
from shijhon.catalog.editions import dedupe_editions
from shijhon.catalog.model import CatalogRef, Twins, artwork_url
from shijhon.proxy.app import Forward, Handler, HandlerResult, RequestContext
from shijhon.proxy.auth import CheckFailed, CredentialChecker
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import image, subsonic_error, subsonic_ok
from shijhon.store import Store
from shijhon.views.covers import CoverPrefetch, CoverSizes
from shijhon.views.entries import (
    album_child,
    album_entry,
    artist_entry,
    each,
    image_urls,
    owned_album_song,
    song_entry,
)
from shijhon.views.ids import CatalogId
from shijhon.views.library_artists import LibraryArtists
from shijhon.views.shown import ShownArtists

log = logging.getLogger(__name__)
cover_log = logging.getLogger("shijhon.covers")  # one debug line per catalog cover
ALBUM_BUDGET = 2.0  # seconds getSong of a catalog song waits for its album's fields

NOT_FOUND = 70


def rewrite(call: RestCall, keys: set[str], mapping: dict[str, str]) -> RestCall:
    return call.rewritten(lambda k, v: mapping.get(v) if k in keys else None)


def catalog_error(
    call: RestCall, exc: CatalogError, server: dict[str, object] | None = None
) -> HandlerResult:
    if exc.kind == "not_found":
        return subsonic_error(call, NOT_FOUND, "catalog item not found", server)
    return subsonic_error(call, 0, f"catalog unavailable: {exc.reason}", server)


class Materialized:
    """Which catalog items are in the library, and as what."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def album(self, ref: CatalogRef) -> str | None:
        row = await self.store.fetchone("SELECT album_id FROM releases WHERE ref = ?", [str(ref)])
        return str(row["album_id"]) if row else None

    async def song(self, ref: CatalogRef) -> str | None:
        row = await self.store.fetchone(
            "SELECT song_id FROM track_links WHERE track_ref = ?", [str(ref)]
        )
        return str(row["song_id"]) if row else None


class VirtualViews:
    def __init__(
        self,
        catalog: Catalog | None,
        store: Store,
        checker: CredentialChecker,
        *,
        library_artists: LibraryArtists | None = None,
        twins: Twins = Twins.EXPLICIT,
        artwork: ArtworkIndex | None = None,
        covers: ArtworkCache | None = None,
        shown: ShownArtists | None = None,
    ) -> None:
        self.catalog = catalog
        self.shown = shown  # catalog artists shown as pages, by name (getTopSongs)
        # The artwork of items already shown: their covers need no album request; covers
        # are kept on disk.
        self.artwork = artwork
        self.covers = covers
        self.sizes = CoverSizes()  # the sizes covers are fetched at
        self.prefetch: CoverPrefetch | None = None  # covers fetched ahead
        self.materialized = Materialized(store)
        self.checker = checker
        self.library_artists = library_artists
        self.twins = twins
        # The native ID of a catalog item in the library (``views/catalog_ids.py``).
        self.native: Callable[[CatalogId], Awaitable[str | None]] | None = None
        # A release shown with an owned album before its fill: that album's ID, and
        # Navidrome's album entry for it.
        self.owner: Callable[[str], Awaitable[str | None]] | None = None
        self.album_of: Callable[[str], Awaitable[dict[str, Any]]] | None = None

    def handlers(self) -> dict[str, Any]:
        views = {
            "getAlbum": self.get_album,
            "getSong": self.get_song,
            "getArtist": self.get_artist,
            "getArtistInfo": self.get_artist_info,
            "getArtistInfo2": self.get_artist_info,
            "getAlbumInfo": self.get_album_info,
            "getAlbumInfo2": self.get_album_info,
            "getMusicDirectory": self.get_music_directory,
            "getCoverArt": self.get_cover_art,
        }
        return {name: _never_500(view, self.checker) for name, view in views.items()}

    def _serves(self, cid: CatalogId) -> Catalog | None:
        catalog = self.catalog
        return catalog if catalog is not None and cid.ref.catalog == catalog.key else None

    async def _target(
        self, call: RestCall, ctx: RequestContext, kind: str
    ) -> tuple[CatalogId | None, bool]:
        """(catalog ID, whether to proceed): no work unless the credentials are good."""
        cid = CatalogId.parse(call.get("id"))
        if cid is None or cid.kind != kind:
            return None, False
        return cid, await ctx.caller() is not None

    def _ok(self, call: RestCall, payload: dict[str, Any]) -> HandlerResult:
        return subsonic_ok(call, payload, self.checker.server)

    async def _library(self) -> Mapping[str, str]:
        if self.library_artists is None:
            return {}
        return await self.library_artists.ids()

    # --- albums and songs --------------------------------------------------------------

    async def get_album(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        cid, proceed = await self._target(call, ctx, "al")
        if cid is None or not proceed:
            return None
        native = await self.materialized.album(cid.ref)
        if native is not None:
            return Forward(rewrite(call, {"id"}, {str(cid): native}))
        catalog = self._serves(cid)
        if catalog is None or call.fmt == "jsonp":
            return None
        try:
            release = await catalog.album(cid.ref.id)
        except CatalogError as exc:
            return catalog_error(call, exc, self.checker.server)
        entry = album_entry(release, songs=True, library=await self._library())
        return self._ok(call, {"album": entry})

    async def get_song(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        cid, proceed = await self._target(call, ctx, "tr")
        if cid is None or not proceed:
            return None
        native = await self.materialized.song(cid.ref)
        if native is not None:
            return Forward(rewrite(call, {"id"}, {str(cid): native}))
        catalog = self._serves(cid)
        if catalog is None or call.fmt == "jsonp":
            return None
        try:
            track = await catalog.song(cid.ref.id)
        except CatalogError as exc:
            return catalog_error(call, exc, self.checker.server)
        release = None
        if track.album is not None:  # its album's artist and fields, as a library song has
            try:
                with anyio.move_on_after(ALBUM_BUDGET):
                    release = await catalog.album(track.album.id)
            except CatalogError as exc:
                log.info("album of a catalog song unavailable: %s", exc.reason)
        library = await self._library()
        entry = song_entry(track, release, library=library)
        owned = await self._owned_album(str(track.album)) if track.album else None
        if owned is not None and release is not None:  # as its album shows it
            entry = owned_album_song(track, release, owned, library=library)
        return self._ok(call, {"song": entry})

    async def _owned_album(self, release_ref: str) -> dict[str, Any] | None:
        """Navidrome's entry of the owned album a release is shown with, if any."""
        if self.owner is None or self.album_of is None:
            return None
        album_id = await self.owner(release_ref)
        if album_id is None:
            return None
        try:
            return await self.album_of(album_id)
        except Exception as exc:  # the catalog's own fields then
            log.info("owned album unavailable: %s", type(exc).__name__)
            return None

    async def _owner(self, cid: CatalogId) -> str | None:
        return await self.owner(str(cid.ref)) if self.owner is not None else None

    async def get_album_info(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        cid, proceed = await self._target(call, ctx, "al")
        if cid is None or not proceed:
            return None
        native = await self.materialized.album(cid.ref) or await self._owner(cid)
        if native is not None:
            return Forward(rewrite(call, {"id"}, {str(cid): native}))
        catalog = self._serves(cid)
        if catalog is None or call.fmt == "jsonp":
            return None
        try:
            release = await catalog.album(cid.ref.id)
        except CatalogError as exc:
            return catalog_error(call, exc, self.checker.server)
        info = {"notes": "", "musicBrainzId": "", "lastFmUrl": ""}
        if addresses_for_clients(catalog):  # (else: the cover by its ID, through Shijhon)
            info.update(image_urls(release.artwork_template))
        return self._ok(call, {"albumInfo": info})

    # --- artists -------------------------------------------------------------------------

    async def get_artist(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        cid, proceed = await self._target(call, ctx, "ar")
        catalog = self._serves(cid) if cid else None
        if cid is None or not proceed or catalog is None or call.fmt == "jsonp":
            return None
        try:
            artist = await catalog.artist(cid.ref.id)
            releases = dedupe_editions(await catalog.artist_releases(cid.ref.id), twins=self.twins)
        except CatalogError as exc:
            return catalog_error(call, exc, self.checker.server)
        entry = artist_entry(
            artist,
            releases,
            library=await self._library(),
            addresses=addresses_for_clients(catalog),
        )
        # The page asked for, whatever its albums link to.
        entry.update({"id": str(cid), "coverArt": f"ar-{cid}"})
        caller = await ctx.caller()
        if self.prefetch is not None and caller is not None:
            self.prefetch.page((caller.username, call.client), [entry, *entry.get("album", [])])
        if self.shown is not None:  # shown by name, once the page is ready (getTopSongs)
            self.shown.shown([artist])
        return self._ok(call, {"artist": entry})

    async def get_artist_info(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        cid, proceed = await self._target(call, ctx, "ar")
        catalog = self._serves(cid) if cid else None
        if cid is None or not proceed or catalog is None or call.fmt == "jsonp":
            return None
        try:
            artist = await catalog.artist(cid.ref.id)
        except CatalogError as exc:
            return catalog_error(call, exc, self.checker.server)
        info: dict[str, Any] = {"biography": "", "musicBrainzId": "", "lastFmUrl": ""}
        if addresses_for_clients(catalog):
            info.update(image_urls(artist.artwork_template))
        info["similarArtist"] = []
        key = "artistInfo2" if call.name == "getArtistInfo2" else "artistInfo"
        return self._ok(call, {key: info})

    # --- directories ----------------------------------------------------------------------

    async def get_music_directory(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """A catalog album as a directory of its songs, a catalog artist as a directory
        of its albums, shaped like Navidrome's; one in the library is Navidrome's."""
        cid = CatalogId.parse(call.get("id"))
        if cid is None or cid.kind == "tr" or await ctx.caller() is None:
            return None
        native = await self.native(cid) if self.native is not None else None
        if native is not None:
            return Forward(rewrite(call, {"id"}, {str(cid): native}))
        catalog = self._serves(cid)
        if catalog is None or call.fmt == "jsonp":
            return None
        library = await self._library()
        try:
            if cid.kind == "al":
                release = await catalog.album(cid.ref.id)
                album = album_entry(release, library=library)
                songs = each(
                    release.tracks, lambda t: song_entry(t, release, library=library), "track"
                )
                directory: dict[str, Any] = {
                    "child": songs,
                    "id": str(cid),
                    "name": album["name"],
                    "parent": album.get("artistId", ""),
                    "coverArt": album["coverArt"],
                    "songCount": len(songs),
                }
            else:
                artist = await catalog.artist(cid.ref.id)
                releases = dedupe_editions(
                    await catalog.artist_releases(cid.ref.id), twins=self.twins
                )
                children = each(releases, lambda r: album_child(r, library=library), "album")
                directory = {
                    "child": children,
                    "id": str(cid),
                    "name": artist.name,
                    "albumCount": len(children),
                }
        except CatalogError as exc:
            return catalog_error(call, exc, self.checker.server)
        return self._ok(call, {"directory": directory})

    # --- artwork ---------------------------------------------------------------------------

    async def get_cover_art(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        cid = CatalogId.parse_artwork(call.get("id"))
        if cid is None:
            # Navidrome's cover: only the size this client asks for is noted, as a hint for
            # covers fetched ahead of a verified caller's pages (no work, no request here).
            if (known := self.checker.known(call)) is not None:  # checked moments ago
                self.sizes.note((known.username, call.client), call.get("size"))
            elif user := call.get("u"):
                self.sizes.note((user, call.client), call.get("size"), verified=False)
            return None
        caller = await ctx.caller()
        if caller is None:
            return None
        self.sizes.note((caller.username, call.client), call.get("size"))
        original = call.get("id") or ""
        if cid.kind == "al" and (
            native := await self.materialized.album(cid.ref) or await self._owner(cid)
        ):
            return Forward(rewrite(call, {"id"}, {original: f"al-{native}"}))
        if cid.kind == "tr" and (native := await self.materialized.song(cid.ref)):
            return Forward(rewrite(call, {"id"}, {original: f"mf-{native}"}))
        catalog = self._serves(cid)
        if catalog is None:
            return None
        size = self.sizes.fetched(call.get("size"))  # the next common size up
        started = time.perf_counter()
        steps: dict[str, Any] = {}

        async def fetch() -> tuple[bytes, str]:
            return await self._cover(catalog, cid, size, steps)

        try:
            if self.covers is not None:  # kept per cover and size
                data, content_type = await self.covers.get(f"{cid.kind}:{cid.ref}:{size}", fetch)
            else:
                data, content_type = await fetch()
        except _NoArtwork:
            return None  # Navidrome answers with its own placeholder
        except CatalogError as exc:
            log.info("catalog artwork unavailable: %s", exc.reason)
            return None
        # Only a raster image, by its bytes, is answered from here, under the type the bytes
        # say (never the catalog's claim): an SVG could carry script into this origin.
        verified = raster_type(data)
        if verified is None:
            log.info(
                "catalog artwork not served: not a JPEG, PNG, GIF or WebP image (sent as %s)",
                _printable(content_type)[:60],
            )
            return None  # Navidrome answers with its own placeholder
        content_type = verified
        if cover_log.isEnabledFor(logging.DEBUG):
            cover_log.debug(
                "cover %s c=%s size %s->%d: %s; %d bytes in %.3fs",
                cid.kind,
                _printable(call.client),
                _printable(call.get("size") or "-"),
                size,
                "; ".join(f"{k} {v}" for k, v in steps.items()) or "kept on disk",
                len(data),
                time.perf_counter() - started,
            )
        return image(call, data, content_type)

    async def _cover(
        self, catalog: Catalog, cid: CatalogId, size: int, steps: dict[str, Any]
    ) -> tuple[bytes, str]:
        """The item's cover: from the artwork shown with it (kept on disk), else
        asked of the catalog - also when the remembered artwork is gone (404, 410).
        ``steps``: what it took, for the log."""
        started = time.perf_counter()
        remembered = await self.artwork.find(cid.kind, cid.ref) if self.artwork else None
        if remembered is not None and (url := artwork_url(remembered, size)):
            steps["address"] = f"known {time.perf_counter() - started:.3f}s"
            try:
                return await self._image(catalog, url, steps)
            except CatalogError as exc:
                if exc.kind != "not_found":
                    raise  # a passing failure: the address stays, the client tries again
                assert self.artwork is not None
                self.artwork.forget(cid.kind, cid.ref)  # it no longer loads: asked again
        started = time.perf_counter()
        url = artwork_url(await self._artwork_template(catalog, cid), size)
        steps["address"] = f"catalog request {time.perf_counter() - started:.3f}s"
        if url is None:
            raise _NoArtwork
        return await self._image(catalog, url, steps)

    @staticmethod
    async def _image(catalog: Catalog, url: str, steps: dict[str, Any]) -> tuple[bytes, str]:
        started = time.perf_counter()
        found = await catalog.artwork(url)
        steps["image"] = f"{time.perf_counter() - started:.3f}s"
        return found

    @staticmethod
    async def _artwork_template(catalog: Catalog, cid: CatalogId) -> str | None:
        """The item's artwork, asked of the catalog (an item not shown before)."""
        if cid.kind == "al":
            return (await catalog.album(cid.ref.id)).artwork_template
        if cid.kind == "tr":
            return (await catalog.song(cid.ref.id)).artwork_template
        return (await catalog.artist(cid.ref.id)).artwork_template


def _printable(text: str) -> str:
    """A client-given value fit for one log line."""
    return "".join(ch if ch.isprintable() else "?" for ch in text[:32])


class _NoArtwork(Exception):
    """The catalog has no artwork for the item."""


def _never_500(view: Handler, checker: CredentialChecker) -> Handler:
    """A broken catalog answer must not become a server error for the client."""

    async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
        try:
            return await view(call, ctx)
        except CheckFailed:  # no verdict on the credentials: the proxy's error
            raise
        except Exception as exc:
            log.warning("%s failed: %s", call.name, type(exc).__name__)
            return subsonic_error(call, 0, "catalog unavailable", checker.server)

    return handle
