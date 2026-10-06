"""Delivered audio expires back to placeholders.

Download-first puts real audio in place of a placeholder so that Navidrome can serve
it - a lower bitrate, another format, an offline download, a share or the jukebox. That
audio is a cache: it goes back to the silent placeholder (a targeted scan, the song ID
checked, as for any replacement) once it has not been used for a while (30 days), and the
least recently used goes first when all of it together passes a size limit. The
placeholder - and everything users did with it - stays; its next play streams from the
add-ons again. Audio used in the last couple of hours is never reverted: a paused play may
still seek in it. Delivered audio whose file is gone (a restore from a backup that leaves
delivered audio out) gets its placeholder back at once, used or not.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import anyio
import anyio.to_thread

from shijhon.navidrome.client import NavidromeError
from shijhon.navidrome.scans import ScanTimeout
from shijhon.placeholders.engine import PlaceholderEngine, ReplaceError
from shijhon.store import Store

log = logging.getLogger(__name__)
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
TOUCH_SECONDS = 600.0  # a use is recorded at most this often per song
_UNAVAILABLE = (NavidromeError, ScanTimeout)  # a sweep ends: the next one tries again


@dataclass
class Swept:
    expired: int = 0
    over_size: int = 0
    missing: int = 0
    failed: int = 0
    kept_bytes: int = 0


class DeliveredAudio:
    def __init__(
        self,
        store: Store,
        engine: PlaceholderEngine,
        *,
        max_days: float,
        max_bytes: int,
        recent_seconds: float = 7200.0,
        interval_seconds: float = 3600.0,
        start_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
        spawn: Spawn | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.max_age = max_days * 86400  # 0: no expiry by age
        self.max_bytes = max_bytes  # 0: no size limit
        self.recent = recent_seconds
        self.interval = interval_seconds
        self.start = start_seconds
        self.clock = clock
        self.spawn = spawn
        self._lock = anyio.Lock()
        self._pending = False
        self._touched: dict[str, float] = {}  # song -> when its last use was recorded
        # Songs whose use is being recorded now, each with what ends when that is over.
        self._writing: dict[str, anyio.Event] = {}
        self._bytes: int | None = None  # delivered audio in all, as the last sweep counted

    @property
    def enabled(self) -> bool:
        return self.max_age > 0 or self.max_bytes > 0

    @property
    def kept_bytes(self) -> int | None:
        """Delivered audio in all, as the last sweep counted it and deliveries since
        (None: not counted yet; the first sweep runs a few minutes after startup)."""
        return self._bytes

    async def used(self, song_id: str) -> None:
        """A placeholder or its delivered audio was asked for (a play, a download...) by a
        verified caller: noted, a few times an hour at most (remembered here, so most
        requests write nothing). Delivered audio stays a while longer; a placeholder used
        once is never taken out as unused."""
        while True:
            now = self.clock()
            if now - self._touched.get(song_id, float("-inf")) < TOUCH_SECONDS:
                return
            writing = self._writing.get(song_id)
            if writing is None:
                break
            # Being written by another request of it: this one waits for that write, and
            # writes its own use when that one failed or was canceled (it looks again) -
            # else neither would be recorded.
            await writing.wait()
        done = self._writing[song_id] = anyio.Event()
        try:
            await self.store.execute(
                "UPDATE placeholders SET last_used_at = ? WHERE song_id = ?"
                " AND (last_used_at IS NULL OR last_used_at < ?)",
                [now, song_id, now - TOUCH_SECONDS],
            )
        except Exception as exc:  # bookkeeping: the request goes on, the next use writes it
            log.warning("a use of %s not recorded: %s", song_id, type(exc).__name__)
            return
        finally:
            del self._writing[song_id]
            done.set()
        # Noted once written: a write that failed or was canceled leaves the next use to
        # write it (else the use would be missing for minutes, to expiry and the cleanup).
        self._touched[song_id] = now
        if len(self._touched) > 20000:
            self._touched = {k: v for k, v in self._touched.items() if now - v < TOUCH_SECONDS}

    def delivered(self, size: int) -> None:
        """New audio of ``size`` bytes is in place: past the size limit (as far as known),
        the least recently used goes, in the background."""
        if self._bytes is not None:
            self._bytes += size
        over = self.max_bytes > 0 and self._bytes is not None and self._bytes > self.max_bytes
        if not over or self.spawn is None or self._pending:
            return
        self._pending = True

        async def sweep() -> None:
            try:
                await self.sweep()
            except Exception as exc:  # the hourly sweep tries again
                log.warning("delivered audio not swept: %s", type(exc).__name__)
            finally:
                self._pending = False

        self.spawn(sweep)

    async def run(self) -> None:
        """Look now and then (the app's background task)."""
        if not self.enabled:
            return
        await anyio.sleep(self.start)
        while True:
            try:
                await self.sweep()
            except Exception as exc:  # the next round tries again
                log.warning("delivered audio not swept: %s", type(exc).__name__)
            await anyio.sleep(self.interval)

    async def sweep(self) -> Swept:
        """Put placeholders back where the delivered file is gone, then revert what
        expired, then the least recently used past the size limit."""
        async with self._lock:
            rows = await self.store.fetchall(
                "SELECT song_id, path, delivered_at, last_used_at FROM placeholders"
                " WHERE state = 'delivered'"
            )
            paths = [str(r["path"]) for r in rows]
            sizes, gone = await anyio.to_thread.run_sync(self._sizes, paths)
            swept = Swept()
            # Nothing is put back or reverted while the library may not be written
            # (Navidrome would purge a song gone for the moment of its swap); asked once a
            # sweep, and again by each write.
            refused = await self.engine.refused() if rows else None
            if refused:
                log.warning("delivered audio stays as it is: %s", refused)
            missing = [] if refused else [r for r in rows if str(r["path"]) in gone]
            if missing and not await anyio.to_thread.run_sync(self._mounted, len(missing), rows):
                missing = []  # most of it at once: a disk away, not files deleted
            for row in missing:
                try:  # nothing to keep: whatever its use, its placeholder is served again
                    song = str(row["song_id"])
                    if await self.engine.revert_to_placeholder(song, only_if_missing=True):
                        swept.missing += 1
                except _UNAVAILABLE as exc:  # Navidrome: the next sweep tries again
                    swept.failed += 1
                    return self._ended(swept, exc)
                except Exception as exc:  # the others still get their turn
                    swept.failed += 1
                    log.warning("missing delivered audio of %s not repaired: %s",
                                row["song_id"], _why(exc))  # fmt: skip
            now = self.clock()
            files = sorted(
                (
                    max(float(r["delivered_at"] or 0), float(r["last_used_at"] or 0)),
                    str(r["song_id"]),
                    sizes.get(str(r["path"]), 0),
                )
                for r in rows
                if str(r["path"]) not in gone
            )
            swept.kept_bytes = sum(size for _, _, size in files)
            for last, song_id, size in files:  # the least recently used first
                if now - last < self.recent:
                    break  # this one and every later one were used just now
                expired = self.max_age > 0 and now - last > self.max_age
                over = self.max_bytes > 0 and swept.kept_bytes > self.max_bytes
                if refused or (not expired and not over):
                    continue
                try:
                    # Only if still unused (checked again under the song's lock).
                    if not await self.engine.revert_to_placeholder(song_id, unused_since=last):
                        continue
                except _UNAVAILABLE as exc:  # Navidrome: the next sweep tries again
                    swept.failed += 1
                    return self._ended(swept, exc)
                except Exception as exc:  # the others still get their turn
                    swept.failed += 1
                    log.warning("delivered audio of %s not reverted: %s", song_id, _why(exc))
                    continue
                swept.kept_bytes -= size
                if expired:
                    swept.expired += 1
                else:
                    swept.over_size += 1
            self._bytes = swept.kept_bytes
            if swept.expired or swept.over_size or swept.missing or swept.failed:
                log.info(
                    "delivered audio: %d back to placeholders (%d unused for %g days, %d over"
                    " the size limit, %d whose file was gone), %d failed; %d MB kept",
                    swept.expired + swept.over_size + swept.missing,
                    swept.expired,
                    self.max_age / 86400,
                    swept.over_size,
                    swept.missing,
                    swept.failed,
                    swept.kept_bytes // 2**20,
                )
            return swept

    @staticmethod
    def _ended(swept: Swept, exc: Exception) -> Swept:
        """A sweep ended early: Navidrome is unavailable or busy (every other song would wait
        for it too); what it did so far stands."""
        log.warning(
            "delivered audio: the sweep stopped, Navidrome unavailable (%s); %d back to"
            " placeholders so far",
            type(exc).__name__,
            swept.expired + swept.over_size + swept.missing,
        )
        return swept

    def _sizes(self, paths: list[str]) -> tuple[dict[str, int], set[str]]:
        """(sizes of the files there, the files that are gone); a file that cannot be read
        now (another error) is neither."""
        found, gone = {}, set()
        for path in paths:
            try:
                found[path] = self.engine.layout.absolute(path).stat().st_size
            except FileNotFoundError:
                gone.add(path)
            except OSError:
                continue
        return found, gone

    def _mounted(self, missing: int, rows: list[Any]) -> bool:
        """Whether missing files look deleted rather than a whole disk away: the placeholder
        folder is there, and not most of the delivered audio is gone at once (logged)."""
        folder = self.engine.layout.absolute(self.engine.layout.folder)
        if folder.is_dir() and not (missing > 10 and missing > len(rows) // 2):
            return True
        log.warning(
            "delivered audio: %d of %d files look missing (the library not mounted?);"
            " not repaired now",
            missing,
            len(rows),
        )
        return False


def _why(exc: Exception) -> str:
    """A failure for the log: a swap's own reason, else only its kind."""
    return str(exc) if isinstance(exc, (ReplaceError, OSError)) else type(exc).__name__
