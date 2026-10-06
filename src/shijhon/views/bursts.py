"""Per-client bursts: how many different things one client asked for within a window.

Used for the search guard (many different searches in a short time) and for artist
page syncs (many artist pages without a fresh saved discography). A client is a user
and the client name it sends. A burst begins when the client reaches the limit within the
window, and lasts until the pause - at least the window - has passed since it last did.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum

Client = tuple[str, str]  # (user, client name)


class Burst(Enum):
    NO = "no"
    STARTED = "started"  # the client just went over the limit (worth a log line)
    GOING_ON = "going on"


class Bursts:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        pause_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = max(2, limit)
        self.window = window_seconds
        self.pause = pause_seconds
        self.clock = clock
        self._seen: dict[Client, dict[str, float]] = {}  # what, and when last asked for
        self._until: dict[Client, float] = {}  # the burst lasts until then

    def note(self, client: Client, what: str) -> Burst:
        """``client`` asked for ``what``: whether it is in a burst now."""
        now = self.clock()
        was = self.bursting(client)
        seen = self._seen.setdefault(client, {})
        seen.pop(what, None)
        seen[what] = now
        for key in [k for k, at in seen.items() if at < now - self.window]:
            del seen[key]
        if len(seen) >= self.limit:
            self._until[client] = now + max(self.pause, self.window)
        if len(self._seen) > 1024:  # forget clients that went quiet
            recent = now - self.window
            self._seen = {c: s for c, s in self._seen.items() if max(s.values()) >= recent}
            self._until = {c: t for c, t in self._until.items() if t > now}
        if not self.bursting(client):
            return Burst.NO
        return Burst.GOING_ON if was else Burst.STARTED

    def bursting(self, client: Client) -> bool:
        return self._until.get(client, 0.0) > self.clock()

    def count(self, client: Client) -> int:
        return len(self._seen.get(client, {}))
