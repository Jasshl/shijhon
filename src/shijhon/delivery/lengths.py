"""Delivered audio whose length Navidrome reads as zero, put in one piece.

Add-on audio in a fragmented MP4 (some add-ons serve whole songs so) or in Ogg FLAC went
into the library as it came, and Navidrome reads no length from either: apps showed the
song as 0:00 and could not seek in it. Delivered audio now goes in one piece
(``DownloadFirst._one_piece``); this puts right what is in the library from before, in
the background - a few minutes after startup, then every hour while songs were left for
later: each such file is replaced by the same encoded audio in a container whose length
Navidrome reads, tagged from its row, its song ID checked by a targeted scan
(``PlaceholderEngine.redeliver``). Audio used in the last couple of hours is left for later
(a paused play may still seek in its file), as is everything while the library may not be
written. A music import's own audio is a person's file: never touched. A song whose file
cannot be put right is tried three times, then left as it is until the next start.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import anyio
import anyio.to_thread

from shijhon.delivery.download_first import DownloadFirst, needs_one_piece
from shijhon.navidrome.client import NavidromeError
from shijhon.navidrome.scans import ScanTimeout
from shijhon.placeholders.engine import ReplaceError
from shijhon.store import Store

log = logging.getLogger(__name__)
TRIES = 3  # a song whose file cannot be put right, at most (until the next start)
_UNAVAILABLE = (NavidromeError, ScanTimeout)  # a sweep ends: the next one tries again


@dataclass
class Swept:
    done: int = 0
    later: int = 0  # used in the last couple of hours, or the library may not be written now
    failed: int = 0


class OnePiece:
    def __init__(
        self,
        store: Store,
        download_first: DownloadFirst,
        *,
        recent_seconds: float = 7200.0,
        interval_seconds: float = 3600.0,
        start_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.download_first = download_first
        self.recent = recent_seconds
        self.interval = interval_seconds
        self.start = start_seconds
        self.clock = clock
        self._failures: dict[str, int] = {}  # song -> failed tries

    async def run(self) -> None:
        """Look a while after startup, then every hour while songs were left for later
        (the app's background task); it ends once nothing is left to try."""
        await anyio.sleep(self.start)
        while True:
            try:
                swept = await self.sweep()
            except Exception as exc:  # the next round tries again
                log.warning("delivered audio not put in one piece: %s", type(exc).__name__)
                swept = Swept(failed=1)
            if not swept.later and not swept.failed:
                return
            await anyio.sleep(self.interval)

    async def sweep(self) -> Swept:
        """Every delivered file Navidrome reads no length from, put in one piece (or left for
        later)."""
        rows = await self.store.fetchall(
            "SELECT song_id, path, delivered_at, last_used_at FROM placeholders"
            " WHERE state = 'delivered' AND import_item IS NULL"
        )
        layout = self.download_first.engine.layout

        def found() -> list[tuple[str, float]]:
            return [
                (str(r["song_id"]), max(float(r[k] or 0) for k in ("delivered_at", "last_used_at")))
                for r in rows
                if self._failures.get(str(r["song_id"]), 0) < TRIES
                and needs_one_piece(layout.absolute(str(r["path"])))
            ]

        songs = await anyio.to_thread.run_sync(found)
        swept = Swept()
        if not songs:
            return swept
        # Nothing is written while the library may not be (asked once a sweep, and again by
        # each swap).
        refused = await self.download_first.engine.refused()
        if refused:
            log.warning("delivered audio is not put in one piece now: %s", refused)
            swept.later = len(songs)
            return swept
        now = self.clock()
        for song_id, last in songs:
            if now - last < self.recent:
                swept.later += 1  # (used just now: a paused play may still seek in its file)
                continue
            try:
                if await self.download_first.in_one_piece(song_id, unused_since=last):
                    swept.done += 1
                else:
                    swept.later += 1  # used meanwhile
            except _UNAVAILABLE as exc:  # Navidrome: the next round tries again
                swept.failed += 1
                log.warning(
                    "delivered audio: putting it in one piece stopped, Navidrome unavailable (%s)",
                    type(exc).__name__,
                )
                break
            except Exception as exc:  # the others still get their turn
                swept.failed += 1
                tries = self._failures[song_id] = self._failures.get(song_id, 0) + 1
                why = str(exc) if isinstance(exc, (ReplaceError, OSError)) else type(exc).__name__
                log.warning(
                    "the delivered audio of %s is not put in one piece (try %d of %d): %s",
                    song_id,
                    tries,
                    TRIES,
                    why,
                )
        log.info(
            "delivered audio Navidrome read no length from: %d put in one piece, %d left for"
            " later, %d failed",
            swept.done,
            swept.later,
            swept.failed,
        )
        return swept
