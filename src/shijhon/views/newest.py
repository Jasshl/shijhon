"""New releases of the artists an account follows, at the top of its newest albums.

``getAlbumList2`` with ``type=newest`` (JSON and XML), for an account that turned new
releases on (``new_releases.py``): the releases saved for it come first, the newest
release first, as catalog albums exactly like those a search adds (``views/entries.py``:
the same IDs and fields, so opening, playing, favoriting or adding one behaves as there).
Navidrome's own list follows unchanged, in its order.

The answer is made from what the daily check saved: a request asks nothing of the
catalog, and Navidrome once, as it would be asked anyway. Paging counts the saved
releases first: with ``n`` of them, ``offset`` and ``size`` address the list of those
``n`` followed by Navidrome's, so pages neither repeat nor skip an album, and
``X-Total-Count`` is the library's count plus ``n``.

A saved release leaves the list when it is older than the window, and when the library
has it: Navidrome then lists the album itself. As in a selected search
(``views/additions.py``), a release in the library whose album the account's selection
hides keeps its entry, and stands for that album.

Nothing is added for an account that did not turn it on (no work: one lookup by its
name), to another ``type``, to a list of another music folder, to JSONP or ``HEAD``
requests, or to the folder-based ``getAlbumList``: those stay Navidrome's, as for search
additions. A failure here leaves Navidrome's own list.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from shijhon.access import AccessService
from shijhon.catalog.model import CatalogRelease
from shijhon.collection.visibility import changed
from shijhon.matching.normalize import fold
from shijhon.new_releases import NewReleases, released
from shijhon.proxy.app import Handler, HandlerResult, RequestContext
from shijhon.proxy.auth import CheckFailed
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Upstream
from shijhon.views.additions import CatalogAdditions, _count
from shijhon.views.answers import Shown, accepts_gzip, library_answer
from shijhon.views.entries import album_entry, each

log = logging.getLogger(__name__)
METHOD = "getAlbumList2"
KEY = "albumList2"
MAX_SIZE = 500  # Navidrome's largest page
# An account's selection, by its name (``collection.visibility.Visibility.selection``).
Selection = Callable[[str], Awaitable[Shown | None]]


class NewestReleases:
    def __init__(
        self, additions: CatalogAdditions, access: AccessService, upstream: Upstream
    ) -> None:
        self.additions = additions
        self.access = access
        self.upstream = upstream
        # The accounts' saved releases, and what a caller's selection shows of library
        # albums: set once they exist (nothing is added before).
        self.releases: NewReleases | None = None
        self.selection: Selection | None = None

    def wrap(self, inner: Handler | None) -> Handler:
        """``getAlbumList2``: the newest list with the account's new releases, anything
        else as ``inner`` answers it, or Navidrome."""

        async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
            try:
                result = await self._view(call, ctx)
            except CheckFailed:  # no verdict on the credentials: the proxy's error
                raise
            except Exception as exc:  # the list as Navidrome has it
                log.warning("newest list additions failed: %s", type(exc).__name__)
                result = None
            if result is not None:
                return result
            return await inner(call, ctx) if inner is not None else None

        return handle

    async def _view(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        releases = self.releases
        if call.get("type") != "newest" or call.fmt == "jsonp" or ctx.head:
            return None
        if releases is None or not releases.available or not self.additions.this_library(call):
            return None
        size = min(_count(call.get("size"), 10), MAX_SIZE)
        offset = _count(call.get("offset"), 0)
        if size <= 0 or offset < 0:
            return None
        caller = await ctx.caller()
        if caller is None:
            return None  # Navidrome's own credential error
        owner = releases.account_of(caller.username)
        if owner is None:
            return None  # an account that did not turn it on: no work
        principal = await self.access.grants(caller.username)
        if principal.user_id != owner or not principal.can_access(self.access.navidrome.library_id):
            return None  # another account by that name now, or one without the library
        shown = await releases.listed(owner, caller.username)
        if not shown:
            return None
        library = await self.additions.library_ids()
        entries = each(shown, lambda r: album_entry(r, library=library), "album")
        mine = entries[offset : offset + size]
        room = size - len(mine)
        # Navidrome's part of the page (one album at least: its envelope and its count).
        asked = changed(
            call, {"size": str(max(room, 1)), "offset": str(max(0, offset - len(entries)))}
        )
        answer = await library_answer(self.upstream, asked, caller=caller)
        found = await answer.parsed(KEY) if answer is not None else None
        if answer is None or found is None:
            return None  # an error, or Navidrome unreachable: its answer to the request
        document, listed = found
        own = [a for a in listed.get("album") or [] if isinstance(a, dict)][: max(room, 0)]
        if mine or own:
            listed["album"] = [*mine, *own]
        else:
            listed.pop("album", None)
        answer.headers = [
            (key, _plus(value, len(entries))) if key.lower() == b"x-total-count" else (key, value)
            for key, value in answer.headers
        ]
        if mine:
            if self.additions.artwork is not None:  # their covers need no album request
                self.additions.artwork.releases(shown)
            if self.additions.prefetch is not None and offset == 0:
                self.additions.prefetch.page((caller.username, call.client), mine)
        return answer.reply(document=document, compress=accepts_gzip(call))

    async def missing(
        self, saved: tuple[CatalogRelease, ...], username: str
    ) -> list[CatalogRelease]:
        """Of an account's saved releases, those the library does not show it, the newest
        first: one in the library stays only while the account's selection hides its
        album (``NewReleases.missing``)."""
        selection: Shown | None = None
        if self.selection is not None:
            try:
                selection = await self.selection(username)
            except Exception as exc:  # not told: releases in the library stay out
                log.info("new releases: the selection not told (%s)", type(exc).__name__)
        kept = await self.additions.not_in_library(saved, [], selection)
        return sorted(
            kept,
            key=lambda r: (
                -(d.toordinal() if (d := released(r.release_date)) else 0),
                fold(r.title),
            ),
        )


def _plus(value: bytes, more: int) -> bytes:
    try:
        return str(int(value) + more).encode()
    except ValueError:
        return value
