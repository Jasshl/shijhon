"""Owned albums shown complete from their first view.

``getAlbum`` (JSON and XML) for an owned album that a matched release completes and
that is not filled yet: Navidrome's answer, with the release's missing tracks added as
catalog songs of this album (``sh.tr`` IDs; the album's ID, name, cover and year), among
the owned songs in the release's disc and track order. The album's ``songCount`` and
``duration`` become the complete album's: the one documented change to an answer about an
owned item. The owned songs' entries and every other field stay Navidrome's. Nothing is
written: the album is filled on the first action on it or one of those songs
(``views/commits.py``), or automatically when the fill policy allows (``fill/fills.py``).

The same album in lists - ``getArtist``, ``search3``, ``getAlbumList2``, ``getStarred2``
(JSON and XML; the ID3 methods, whose albums open with ``getAlbum``) - carries the
same complete ``songCount`` and ``duration`` (clients file an album with few songs under
singles), from a local lookup of the saved plans; its name, type, year and everything else
stay Navidrome's. Only while Navidrome counts exactly the songs the plan links: an album whose
songs changed since keeps Navidrome's counts. The folder-based lists (``getAlbumList``,
``getStarred``, ``search2``, ``getMusicDirectory``) stay Navidrome's, like the album's
directory view.

A catalog album ID of a release matched to an owned album opens that album. A match that
is not ready within the open budget (a slow or unavailable catalog) leaves Navidrome's
answer as it is, and the reason is logged. A view writes nothing in any format (XML
views too); JSONP and HEAD requests get Navidrome's own answer, nothing looked at.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from shijhon.fill.fills import Decision, Fills
from shijhon.proxy.app import Forward, Handler, HandlerResult, RequestContext
from shijhon.proxy.auth import CheckFailed
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Upstream
from shijhon.views.answers import LibraryAnswer, accepts_gzip, captured, library_answer
from shijhon.views.entries import each, owned_album_song, seconds
from shijhon.views.ids import CatalogId, song_id
from shijhon.views.library_artists import LibraryArtists
from shijhon.views.virtual import rewrite

log = logging.getLogger(__name__)
# Album lists (method -> its answer's key) whose owned albums shown complete carry the
# complete album's counts.
LISTS: dict[str, str] = {
    "getArtist": "artist",
    "search3": "searchResult3",
    "getAlbumList2": "albumList2",
    "getStarred2": "starred2",
}


class CompleteAlbums:
    def __init__(
        self, fills: Fills, upstream: Upstream, *, library_artists: LibraryArtists | None = None
    ) -> None:
        self.fills = fills
        self.upstream = upstream
        self.library_artists = library_artists

    def wrap(self, inner: Handler | None) -> Handler:
        """``getAlbum``: owned albums shown complete, the rest as ``inner`` answers them
        (catalog albums' virtual views) or Navidrome."""

        async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
            try:
                result = await self._view(call, ctx)
            except CheckFailed:  # no verdict on the credentials: the proxy's error
                raise
            except Exception as exc:  # the album opens as Navidrome has it
                log.warning("album view failed: %s", type(exc).__name__)
                result = None
            if result is not None:
                return result
            return await inner(call, ctx) if inner is not None else None

        return handle

    def wrap_list(self, method: str, inner: Handler | None) -> Handler:
        """An album list (``LISTS``): as ``inner`` answers it (catalog additions) or
        Navidrome, with the complete counts of owned albums shown complete. Navidrome's own
        answer being "ok" is the credential check: a failed one is passed on as it came."""
        key = LISTS[method]

        async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
            try:
                result = await inner(call, ctx) if inner is not None else None
            except Exception as exc:  # Navidrome's answer, then
                log.warning("%s failed: %s", call.name, type(exc).__name__)
                result = None
            if call.fmt == "jsonp" or ctx.head:
                return result
            if (method == "search3" and call.get("albumCount") == "0") or not (
                await self.fills.any_shown()
            ):
                return result  # no album entries, or none shown complete: nothing to count
            if isinstance(result, Forward | None):
                if ctx.refusal is not None:
                    return result  # Navidrome refused the credentials: its answer
                answer = await library_answer(self.upstream, result.call if result else call)
                if answer is None:
                    return result  # Navidrome unreachable: the forward says so
            else:
                answer = await captured(result)
            try:
                document = await self._counted(answer, key)
            except Exception as exc:  # the list as it came
                log.warning("album list counts failed: %s", type(exc).__name__)
                document = None
            # Compressed as Navidrome compresses it (a sync's pages are large).
            return answer.reply(document=document, compress=accepts_gzip(call))

        return handle

    async def _counted(self, answer: LibraryAnswer, key: str) -> dict[str, Any] | None:
        """The answer's document with the complete counts, or None when nothing changed."""
        found = await answer.parsed(key)
        if found is None:
            return None
        document, container = found
        albums = [a for a in container.get("album") or [] if isinstance(a, dict)]
        shown = await self.fills.shown(str(a.get("id") or "") for a in albums)
        changed = False
        for album in albums:
            plan = shown.get(str(album.get("id") or ""))
            # Navidrome counts exactly the plan's owned songs, as the view requires.
            if plan is None or _int(album.get("songCount"), -1) != plan.owned:
                continue
            album["songCount"] = plan.owned + len(plan.missing)
            album["duration"] = _int(album.get("duration"), 0) + sum(map(seconds, plan.missing))
            changed = True
        return document if changed else None

    async def _view(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        album_id = call.get("id") or ""
        cid = CatalogId.parse(album_id)
        if cid is not None:
            if cid.kind != "al":
                return None
            owner = await self.fills.owner(str(cid.ref))  # no work: one query
            if owner is None or await ctx.caller() is None:
                return None  # a catalog album (virtual view), or Navidrome's error
            call = rewrite(call, {"id"}, {album_id: owner})
            album_id = owner
        elif call.fmt == "jsonp" or ctx.head or not await self.fills.viewable(album_id):
            return None
        # Navidrome's own answer, as it is (nothing looked at): JSONP and HEAD.
        as_it_is = Forward(call) if cid is not None else None
        caller = await ctx.caller()
        if caller is None or call.fmt == "jsonp" or ctx.head:
            return as_it_is  # (without a caller: Navidrome's own credential error)
        decision, why = await self.fills.view(album_id, (caller.username, call.client))
        if (decision is None or decision.release is None) and not why:
            return as_it_is  # nothing to add or to log: the answer is not read
        answer = await library_answer(self.upstream, call)
        if answer is None:
            return as_it_is
        found = await answer.parsed("album")
        if decision is None or decision.release is None or found is None:
            if why and found is not None:
                log.info("album view: %s: %s; the owned songs only", _named(found[1]), why)
            return answer.reply(compress=accepts_gzip(call))
        document, album = found
        library: Mapping[str, str] = (
            await self.library_artists.ids() if self.library_artists is not None else {}
        )
        if not complete(album, decision, library):
            return answer.reply(compress=accepts_gzip(call))
        return answer.reply(document=document, compress=accepts_gzip(call))


def complete(album: dict[str, Any], decision: Decision, library: Mapping[str, str]) -> bool:
    """Add the release's missing tracks to Navidrome's album entry (in place). False when
    the answer does not hold the owned songs the plan links (then it is left as it is)."""
    release = decision.release
    assert release is not None
    songs = album.get("song")
    if not isinstance(songs, list):
        return False
    # Exactly the owned songs the plan links: any other song (a placeholder of a fill
    # running at this moment, a file added since) means Navidrome's answer has moved on.
    owned_ids = {str(s.get("id")) for s in songs if isinstance(s, dict)}
    if owned_ids != set(decision.links.values()) or len(owned_ids) != len(songs):
        return False
    missing = [t for t in release.tracks if t.ref not in decision.links]
    added = each(missing, lambda t: owned_album_song(t, release, album, library=library), "track")
    if not added:
        return False
    shown = {e["id"] for e in added}
    missing = [t for t in missing if song_id(t.ref) in shown]

    def place(entry: dict[str, Any]) -> tuple[int, int, int]:
        # By disc and track number as Navidrome orders the album once it is filled (an
        # owned song keeps its own tags, even a missing number), owned songs first on a tie.
        ident = str(entry.get("id"))
        return (_int(entry.get("discNumber"), 1), _int(entry.get("track"), 0),
                int(ident.startswith("sh.")))  # fmt: skip

    album["song"] = sorted([*songs, *added], key=place)
    # Navidrome's album row can lag behind its songs (after placeholders were removed): the
    # count is the list's, the length its own unless the row disagrees with the list.
    extra = sum(seconds(t.duration_ms) for t in missing)
    if _int(album.get("songCount"), -1) == len(songs):
        album["duration"] = _int(album.get("duration"), 0) + extra
    else:
        album["duration"] = sum(_int(s.get("duration"), 0) for s in songs) + extra
    album["songCount"] = len(songs) + len(added)
    return True


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _named(album: Mapping[str, Any]) -> str:
    return f"{album.get('artist') or album.get('displayArtist') or '?'} - {album.get('name')}"
