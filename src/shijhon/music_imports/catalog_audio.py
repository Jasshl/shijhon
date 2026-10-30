"""An import's own audio in place of a catalog song it matched.

A reviewed file that is a catalog song Shijhon added (a placeholder, silent or with
temporary downloaded audio) is linked to that song (``overlap.CATALOG_SONG``); its received
audio then goes into the song through the placeholder engine's verified delivered-audio
swap: the song keeps its ID, its entry, favorites, plays and playlist entries, and plays
the import's audio from then on. Apps get a copy carrying the catalog song's tags; the
received original stays unchanged and retained privately in the imports root.

Nothing that is already there is overwritten: a catalog song whose delivered audio is
another import's own audio keeps it (``kept``). Downloaded audio is a cache
(``delivery/expiry.py``): it is put back to its placeholder first, then the import's audio
goes in. The import's audio is pinned (``placeholders.import_item``): never expired as a
cache, counted as a use by the cleanup, reverted only for its import - when the import is
removed - or when its file is gone.

Only formats the placeholder engine's delivered-audio swap can tag carry the song (FLAC,
Ogg Vorbis/Opus, MP3 and MPEG-4: ``SUFFIXES``); any other file stays a plain link to the
catalog song. Each step is idempotent and starts from what the song plays (``holds``): a
swap of its file that a stop interrupted is put right first, and a placeholder pinned to
the item - a stop between the engine's commit and the link's record - is the link's audio
in place, whatever else has changed meanwhile (the importer's minimum, say). Its
reservation goes only once the song is known not to play it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from shijhon.placeholders.engine import ReplaceError

log = logging.getLogger(__name__)

PENDING, PLACED, KEPT = "pending", "placed", "kept"
# What the engine's swap tags (``placeholders/tags.write_delivered``), by the import's
# published suffix.
SUFFIXES = frozenset({".flac", ".ogg", ".opus", ".mp3", ".m4a", ".m4b", ".alac"})
UNTAGGABLE, LOW_QUALITY, FAILED = "untaggable", "low_quality", "failed"


class Engine(Protocol):
    def guard_writes(
        self, authorize: Callable[[], Awaitable[None]]
    ) -> AbstractContextManager[None]: ...

    async def placeholder_row(self, song_id: str) -> Any: ...

    async def recover_swap(self, song_id: str) -> int: ...

    async def replace_with_delivered(
        self,
        song_id: str,
        delivered: Any = None,
        *,
        source_fd: int | None = None,
        suffix: str | None = None,
        pin: str | None = None,
    ) -> str: ...

    async def revert_to_placeholder(
        self,
        song_id: str,
        *,
        unused_since: float | None = None,
        only_if_missing: bool = False,
        pinned: str | None = None,
    ) -> bool: ...


class CatalogSongAudio:
    """The music import service's way into the placeholder engine for its catalog songs."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def holds(self, song_id: str, item_id: str) -> bool:
        """Whether the catalog song plays this import's audio now: a swap of its file that
        a stop interrupted is put right first (the file its row names ends up in place).
        Raises when that cannot be done now - it is not known then."""
        await self.engine.recover_swap(song_id)
        row = await self.engine.placeholder_row(song_id)
        return row is not None and row["state"] == "delivered" and row["import_item"] == item_id

    async def put(
        self,
        song_id: str,
        item_id: str,
        source_fd: int,
        suffix: str,
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> str:
        with self.engine.guard_writes(authorize):
            return await self._put(song_id, item_id, source_fd, suffix)

    async def _put(self, song_id: str, item_id: str, source_fd: int, suffix: str) -> str:
        """Put the import's audio in place of the catalog song: ``placed`` (also when it
        is there already), ``kept`` (the song has audio of its own), ``pending`` (a
        download got there meanwhile: tried again) or ``failed``."""
        row = await self.engine.placeholder_row(song_id)
        if row is None:
            return FAILED  # not a catalog song any more (taken out meanwhile)
        if row["state"] == "delivered":
            if row["import_item"] == item_id:
                return PLACED
            if row["import_item"]:
                return KEPT  # another import's own audio: never overwritten
            try:  # downloaded audio, a cache: back to its placeholder first
                if not await self.engine.revert_to_placeholder(song_id):
                    return PENDING
            except ReplaceError as exc:
                log.info("catalog song %s keeps its downloaded audio for now: %s", song_id, exc)
                return PENDING
        try:
            await self.engine.replace_with_delivered(
                song_id, source_fd=source_fd, suffix=suffix, pin=item_id
            )
        except ReplaceError as exc:
            if str(exc) == "already replaced":
                return PENDING  # a download got there first: its cache goes next time
            log.info("an import's audio not put in place of %s: %s", song_id, exc)
            return FAILED
        return PLACED

    async def take(self, song_id: str, item_id: str) -> bool:
        """Put the catalog song back to its placeholder when it plays this import's audio
        (its import was removed). False when it does not (nothing to do)."""
        row = await self.engine.placeholder_row(song_id)
        if row is None or row["state"] != "delivered" or row["import_item"] != item_id:
            return False
        return await self.engine.revert_to_placeholder(song_id, pinned=item_id)
