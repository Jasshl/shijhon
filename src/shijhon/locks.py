"""Per-key locks that disappear when nobody holds or waits for them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio


class KeyedLocks:
    def __init__(self) -> None:
        self._locks: dict[str, tuple[anyio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (anyio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    def held(self, key: str) -> bool:
        """Whether someone holds (or waits for) ``key``."""
        return key in self._locks

    def __len__(self) -> int:
        return len(self._locks)
