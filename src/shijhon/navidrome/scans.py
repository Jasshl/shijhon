"""One coordinator for all of Shijhon's scans.

Navidrome runs one scan at a time and silently ignores a request while another scan runs
(for example one started by its file watcher). So the coordinator waits for the scanner to
be idle, starts one targeted scan for every folder queued so far, and waits for it to end.
Callers still verify the outcome they need and ask again if it is missing
(:meth:`ScanCoordinator.until`).

"The scan ended" means idle **and** a newer ``lastScan``: Navidrome answers ``startScan``
after waiting up to 3 s for the scan to start, so under load it can still report "idle"
while the scan is about to begin. New songs are then already visible during the scan's
first phase, while the album's song count is refreshed only in a later one
(``scanner/phase_1_folders.go``, ``phase_3_refresh_albums.go``); a fill checked then looked
complete while clients still saw the old count. A request Navidrome dropped (another scan
was running) ends ``lastScan``'s wait after a short grace; the caller's check asks again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import anyio

from shijhon.navidrome.client import NavidromeService

log = logging.getLogger(__name__)


class ScanTimeout(RuntimeError):
    pass


class ScanInterrupted(RuntimeError):
    pass


@dataclass
class _Request:
    folders: set[str]
    done: anyio.Event = field(default_factory=anyio.Event)
    error: BaseException | None = None


class ScanCoordinator:
    def __init__(
        self,
        navidrome: NavidromeService,
        *,
        poll_seconds: float = 0.05,
        timeout_seconds: float = 120.0,
        start_grace_seconds: float = 5.0,
    ) -> None:
        self.navidrome = navidrome
        self.poll = poll_seconds
        self.timeout = timeout_seconds
        # Idle this long without a newer lastScan: the request was not taken up.
        self.start_grace = start_grace_seconds
        self._queue: list[_Request] = []
        self._lock = anyio.Lock()
        self.scans_started = 0  # observable in tests

    async def scan(self, folders: Iterable[str]) -> None:
        """Return once a targeted scan covering ``folders`` has run after this call."""
        request = _Request({f.strip("/") for f in folders})
        self._queue.append(request)
        async with self._lock:
            if not request.done.is_set():
                batch, self._queue = self._queue, []
                targets = sorted(set().union(*(r.folders for r in batch)))
                error: Exception | None = None
                try:
                    await self._run(targets)
                except Exception as exc:
                    error = exc
                except BaseException:
                    # This task was canceled: the others are not; let them try again.
                    for queued in batch:
                        if queued is not request:
                            queued.error = ScanInterrupted("the scan was interrupted")
                            queued.done.set()
                    raise
                for queued in batch:
                    queued.error = error
                    queued.done.set()
        await request.done.wait()
        if request.error is not None:
            raise request.error

    async def until(
        self,
        folders: Iterable[str],
        ready: Callable[[], Awaitable[bool]],
        *,
        attempts: int = 3,
    ) -> bool:
        """Scan until ``ready()`` holds; False if it still does not after ``attempts``."""
        folders = list(folders)
        for _ in range(attempts):
            try:
                await self.scan(folders)
            except ScanInterrupted:
                continue
            if await ready():
                return True
        return False

    async def _run(self, targets: list[str]) -> None:
        before = (await self._wait_idle()).get("lastScan")
        self.scans_started += 1
        started = time.monotonic()
        await self.navidrome.start_scan(targets)
        await self._wait_ended(before)
        log.debug(
            "targeted scan of %d folder(s) took %.2fs", len(targets), time.monotonic() - started
        )

    async def _wait_idle(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while (status := await self.navidrome.scan_status())["scanning"]:
            if time.monotonic() > deadline:
                raise ScanTimeout("Navidrome is still scanning")
            await anyio.sleep(self.poll)
        return status

    async def _wait_ended(self, before: Any) -> None:
        """Until a scan has ended since ``before`` (the last one's ``lastScan``), or the
        scanner stayed idle without one for the grace time."""
        deadline = time.monotonic() + self.timeout
        idle_since: float | None = None
        while True:
            status = await self.navidrome.scan_status()
            now = time.monotonic()
            if status["scanning"]:
                idle_since = None
            elif before is None or status.get("lastScan") != before:
                return  # ended (or a Navidrome that does not say when)
            else:
                idle_since = idle_since if idle_since is not None else now
                if now - idle_since > self.start_grace:
                    return
            if now > deadline:
                raise ScanTimeout("Navidrome is still scanning")
            await anyio.sleep(self.poll)
