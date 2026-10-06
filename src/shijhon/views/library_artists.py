"""The library's artists by name: catalog albums, songs and artists credited to an
artist the library has link to that artist's native ID, so clients open the library
artist's page directly instead of a catalog artist ID. (A catalog artist ID of such
an artist still opens as the library artist's page: the fallback.)

The index comes from Navidrome's ``getArtists`` for Shijhon's library (the service
account). It is kept for a few minutes; an old index keeps answering while a new one is
fetched in the background, and a commit, which may add an artist, makes the next request
fetch it again. A name that two library artists share links to neither. Each artist's
entry is Navidrome's own, without the service account's per-user fields, so a linked
artist in search results is described by Navidrome, not by the catalog.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import anyio

from shijhon.matching.normalize import fold
from shijhon.navidrome.client import NavidromeError, NavidromeService

log = logging.getLogger(__name__)
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
# The service account's own marks, not the caller's.
_PER_USER = {"starred", "userRating"}


class LibraryArtists:
    def __init__(
        self,
        navidrome: NavidromeService,
        *,
        library_id: int = 1,
        ttl: float = 300.0,
        retry_seconds: float = 30.0,
        first_wait_seconds: float = 5.0,
        spawn: Spawn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.navidrome = navidrome
        self.library_id = library_id
        self.ttl = ttl
        self.retry_seconds = retry_seconds
        self.first_wait = first_wait_seconds
        self.spawn = spawn
        self.clock = clock
        self._entries: dict[str, dict[str, Any]] | None = None  # folded name -> entry
        self._until = 0.0
        self._generation = 0  # bumped by forget(): a fetch that overlapped is not kept
        self._lock = anyio.Lock()
        self.fetches = 0  # observable in tests

    async def ids(self) -> dict[str, str]:
        """Folded artist name -> native artist ID."""
        return {name: str(entry["id"]) for name, entry in (await self.entries()).items()}

    async def entries(self) -> dict[str, dict[str, Any]]:
        """Folded artist name -> Navidrome's entry for that artist."""
        if self._entries is not None and self.clock() < self._until:
            return self._entries
        if self._entries is not None and self.spawn is not None:
            if not self._lock.locked():
                self.spawn(self._refresh)
            return self._entries  # the old index while a new one is fetched
        with anyio.move_on_after(self.first_wait):
            await self._refresh()
        return self._entries or {}

    def forget(self) -> None:
        """The library may have a new artist (after a commit): fetch again next time."""
        self._generation += 1
        self._until = 0.0

    async def _refresh(self) -> None:
        async with self._lock:
            if self._entries is not None and self.clock() < self._until:
                return
            generation = self._generation
            try:
                self.fetches += 1
                found = await self.navidrome.subsonic(
                    "getArtists", [("musicFolderId", str(self.library_id))]
                )
            except NavidromeError as exc:
                log.info("library artists unavailable: %s", exc)
                self._until = self.clock() + self.retry_seconds  # keep the last index
                return
            entries: dict[str, dict[str, Any]] = {}
            ambiguous: set[str] = set()
            for group in found.get("artists", {}).get("index", []) or []:
                for artist in group.get("artist", []) or []:
                    name, ident = fold(artist.get("name")), artist.get("id")
                    if not name or not ident:
                        continue
                    if name in entries and entries[name]["id"] != ident:
                        ambiguous.add(name)
                    entries[name] = {k: v for k, v in artist.items() if k not in _PER_USER}
            for name in ambiguous:
                entries.pop(name, None)
            self._entries = entries
            # A commit during the fetch may have added an artist the answer lacks.
            fresh = generation == self._generation
            self._until = self.clock() + (self.ttl if fresh else 0.0)
