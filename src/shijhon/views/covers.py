"""Catalog covers for clients: the sizes they are fetched at, and covers fetched
ahead of a client's requests.

- **Sizes**: the catalog's image server answers the sizes its own apps ask for at once
  (10-30 ms) and renders any other size first (measured 0.35-0.6 s a cover, the second
  request of it fast). A cover is therefore fetched (and kept) at the next of a few common
  sizes up from the one a client asks for; the client gets that image (it scales it, as it
  does for any cover). An empty list of sizes: exactly the size asked for.
- **Fetched ahead**: some clients ask for every cover of a page at once, a few at a time
  and in no particular order, so a large catalog artist page took long on its first
  visit. When a client is answered an artist page or search results with catalog items,
  the covers of the first of them (a setting, 50) are fetched in the background, a few at
  a time, at the size this client usually asks for covers (per-device state, keyed by user
  and client name); from the artwork the answer carried, never with a catalog request;
  only into the disk cache, where the client's own requests find them (or share the fetch
  under way). Nothing is fetched ahead for a client whose size is not known yet, nor for
  answers without catalog items (syncs, and bursts the search guards answer from the
  library), nor for a client shown many such pages in a short time (walking pages, not
  looking at one; settings).
"""

from __future__ import annotations

import logging
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Coroutine, Iterable
from typing import Any

import anyio

from shijhon.catalog.artwork import ArtworkCache, ArtworkIndex
from shijhon.catalog.base import Catalog
from shijhon.catalog.model import artwork_url
from shijhon.views.bursts import Burst, Bursts, Client
from shijhon.views.ids import CatalogId

log = logging.getLogger("shijhon.covers")
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
DEFAULT_SIZE = 600  # a cover request without a size
LARGEST = 1200  # larger requests (and size 0: Navidrome's original) get this size
RECENT = 20  # a client's usual size: the most frequent of its last requests
CLIENTS = 1024  # clients whose sizes are remembered (each kind)
STALE_SECONDS = 120.0  # a cover queued this long was not fetched (the background stopped)


def _folded(client: Client) -> Client:
    return (client[0].lower(), client[1])


class CoverSizes:
    """The size a cover is fetched at, and the size each client usually asks for:
    verified (the client's credentials were checked) apart from hints (a claimed user
    name, before any check), which never override a verified size."""

    def __init__(self, sizes: Iterable[int] = ()) -> None:
        self.sizes = sorted({s for s in sizes if 0 < s <= LARGEST})
        self._verified: OrderedDict[Client, deque[int]] = OrderedDict()
        self._hints: OrderedDict[Client, deque[int]] = OrderedDict()

    def fetched(self, asked: str | None) -> int:
        """The size a cover asked for at ``asked`` (the request's ``size``) is fetched at."""
        try:
            size = int(asked) if asked not in (None, "") else DEFAULT_SIZE
        except ValueError:
            size = DEFAULT_SIZE
        size = LARGEST if size <= 0 else max(32, min(size, LARGEST))
        return next((s for s in self.sizes if s >= size), size)

    def note(self, client: Client, asked: str | None, *, verified: bool = True) -> None:
        """A client asked for a cover at ``asked``: its covers are fetched ahead at the size
        it asks for most often."""
        sizes = self._verified if verified else self._hints
        if not verified:  # the name as the client wrote it: Navidrome's, but for its case
            client = _folded(client)
        recent = sizes.setdefault(client, deque(maxlen=RECENT))
        recent.append(self.fetched(asked))
        sizes.move_to_end(client)
        while len(sizes) > CLIENTS:
            sizes.popitem(last=False)

    def of(self, client: Client) -> int | None:
        recent = self._verified.get(client) or self._hints.get(_folded(client))
        return Counter(recent).most_common(1)[0][0] if recent else None


class CoverPrefetch:
    def __init__(
        self,
        covers: ArtworkCache,
        catalog: Catalog,
        artwork: ArtworkIndex,
        sizes: CoverSizes,
        *,
        items: int = 50,
        parallel: int = 4,
        burst_pages: int = 4,
        burst_seconds: float = 10.0,
        spawn: Spawn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.covers = covers
        self.catalog = catalog
        self.artwork = artwork
        self.sizes = sizes
        self.items = items
        self.spawn = spawn
        self.clock = clock
        self._limiter = anyio.CapacityLimiter(parallel)
        self._pages = Bursts(burst_pages, burst_seconds)
        self._queued: dict[str, float] = {}  # cover -> when it was queued
        self.fetched = 0  # observable in tests: covers fetched ahead

    def page(self, client: Client, entries: Iterable[dict[str, Any]]) -> None:
        """An answer with catalog entries was just given to ``client``: the covers of the
        first of them are fetched ahead, in the background."""
        size = self.sizes.of(client)
        if self.items <= 0 or self.spawn is None or size is None:
            return
        now = self.clock()
        for key in [k for k, at in self._queued.items() if now - at > STALE_SECONDS]:
            del self._queued[key]  # never ran (the background stopped): not queued any more
        wanted: dict[str, str] = {}
        for entry in entries:
            cid = CatalogId.parse_artwork(entry.get("coverArt"))
            if cid is None or cid.kind not in ("al", "ar"):
                continue
            url = artwork_url(self.artwork.template(cid.kind, cid.ref), size)
            key = f"{cid.kind}:{cid.ref}:{size}"
            if url is None or key in self._queued or key in wanted:
                continue
            wanted[key] = url
            if len(wanted) >= self.items:
                break
        if not wanted:
            return
        if self._pages.note(client, ",".join(list(wanted)[:3])) is not Burst.NO:
            return  # walking pages: its own requests only
        for key, url in wanted.items():
            if len(self._queued) >= 10 * self.items:
                break
            self._queued[key] = now
            self.spawn(self._fetcher(key, url))

    def _fetcher(self, key: str, url: str) -> Callable[[], Coroutine[Any, Any, None]]:
        async def fetch_ahead() -> None:
            try:
                async with self._limiter:
                    if await self.covers.has(key):
                        return
                    await self.covers.get(key, lambda: self.catalog.artwork(url))
                    self.fetched += 1
            except Exception as exc:  # the client's own request tries again
                log.debug("cover %s not fetched ahead: %s", key, type(exc).__name__)
            finally:
                self._queued.pop(key, None)

        return fetch_ahead
