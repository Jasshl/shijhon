from __future__ import annotations

from typing import Any

import anyio
import pytest

from shijhon.locks import KeyedLocks
from shijhon.navidrome.scans import ScanCoordinator

pytestmark = pytest.mark.anyio


async def test_keyed_locks_are_freed_and_exclusive() -> None:
    locks = KeyedLocks()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with locks.hold("k"):
            order.append(f"{name}+")
            await anyio.sleep(0.01)
            order.append(f"{name}-")

    async with anyio.create_task_group() as tg:
        for name in "abc":
            tg.start_soon(worker, name)
    assert len(locks) == 0
    assert all(order[i][0] == order[i + 1][0] for i in range(0, 6, 2))


class SlowNavidrome:
    """Scans take 0.2 s and, like Navidrome's, keep running if the requester goes away."""

    def __init__(self) -> None:
        self.started = 0
        self.busy_until = 0.0

    async def start_scan(self, folders: Any) -> dict[str, Any]:
        self.started += 1
        self.busy_until = anyio.current_time() + 0.2
        return {}

    async def scan_status(self) -> dict[str, Any]:
        return {"scanning": anyio.current_time() < self.busy_until}


async def test_a_canceled_scan_does_not_cancel_the_others() -> None:
    navidrome = SlowNavidrome()
    scans = ScanCoordinator(navidrome, poll_seconds=0.01)  # type: ignore[arg-type]
    results: list[bool] = []

    async def canceled_runner() -> None:
        with anyio.move_on_after(0.05):
            await scans.scan(["a"])  # takes the batch, then is canceled mid-scan

    async def other() -> None:
        results.append(await scans.until(["b"], _ready))  # joined the same batch

    async with anyio.create_task_group() as tg:
        tg.start_soon(canceled_runner)
        tg.start_soon(other)
    assert results == [True]
    assert navidrome.started == 2  # the interrupted batch, then b's own scan


async def _ready() -> bool:
    return True


class LateNavidrome:
    """Like Navidrome under load: ``startScan`` answers before the scan has begun, so the
    scanner reports idle for a moment; a scan then runs and moves ``lastScan`` on.
    ``drop``: the request is ignored (another scan was running)."""

    def __init__(self, *, drop: bool = False) -> None:
        self.drop = drop
        self.last_scan = "2026-09-28T10:00:00.000001Z"
        self.begins = self.ends = 0.0

    async def start_scan(self, folders: Any) -> dict[str, Any]:
        now = anyio.current_time()
        if not self.drop:
            self.begins, self.ends = now + 0.1, now + 0.2
        return {}

    async def scan_status(self) -> dict[str, Any]:
        now = anyio.current_time()
        if self.begins <= now < self.ends:
            return {"scanning": True, "lastScan": self.last_scan}
        if self.ends and now >= self.ends:
            self.last_scan, self.begins, self.ends = "2026-09-28T10:00:01.000001Z", 0.0, 0.0
        return {"scanning": False, "lastScan": self.last_scan}


async def test_a_scan_ends_only_with_a_newer_last_scan() -> None:
    navidrome = LateNavidrome()
    scans = ScanCoordinator(navidrome, poll_seconds=0.01)  # type: ignore[arg-type]
    started = anyio.current_time()
    await scans.scan(["a"])
    assert anyio.current_time() - started >= 0.2  # not at the first "idle"
    assert navidrome.last_scan.startswith("2026-09-28T10:00:01")


async def test_a_dropped_scan_request_ends_after_the_grace() -> None:
    navidrome = LateNavidrome(drop=True)
    scans = ScanCoordinator(navidrome, poll_seconds=0.01, start_grace_seconds=0.1)  # type: ignore[arg-type]
    with anyio.fail_after(2):
        assert not await scans.until(["a"], _not_ready, attempts=2)


async def _not_ready() -> bool:
    return False
