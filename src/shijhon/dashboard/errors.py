"""Recent errors for Diagnostics: Shijhon's own warnings and errors of the last day, the
same message counted once with its last time. Messages are redacted like the log."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from shijhon.log import clean

KEEP_SECONDS = 86400.0
MAX_KINDS = 200

# Where a message came from, by logger name (the rest: the module's name).
_WHERE = {
    "shijhon.delivery": "Playback",
    "shijhon.catalog": "Catalog",
    "shijhon.views": "Views",
    "shijhon.fill": "Albums",
    "shijhon.matching": "Albums",
    "shijhon.placeholders": "Placeholders",
    "shijhon.navidrome": "Navidrome",
    "shijhon.proxy": "Proxy",
    "shijhon.dashboard": "Dashboard",
}


@dataclass
class Recent:
    where: str
    message: str
    last: float
    times: int


def where(name: str) -> str:
    for prefix, label in _WHERE.items():
        if name == prefix or name.startswith(prefix + "."):
            return label
    return name.removeprefix("shijhon.") or "Shijhon"


class RecentErrors(logging.Handler):
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        super().__init__(logging.WARNING)
        self.clock = clock
        self._seen: dict[tuple[str, str], Recent] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = clean(record.getMessage())  # one line, redacted, as in the log
        except Exception:  # a malformed record must not break logging
            return
        message = message[:300] if message else record.levelname.lower()
        key = (where(record.name), message)
        now = self.clock()
        seen = self._seen.get(key)
        if seen is None:
            self._seen[key] = Recent(key[0], message, now, 1)
        else:
            seen.last, seen.times = now, seen.times + 1
        if len(self._seen) > MAX_KINDS:
            self._prune(now, keep=MAX_KINDS // 2)

    def _prune(self, now: float, keep: int | None = None) -> None:
        items = sorted(self._seen.items(), key=lambda item: item[1].last, reverse=True)
        items = [item for item in items if now - item[1].last < KEEP_SECONDS]
        self._seen = dict(items[:keep] if keep is not None else items)

    def recent(self, limit: int = 50) -> list[Recent]:
        self._prune(self.clock())
        return sorted(self._seen.values(), key=lambda r: r.last, reverse=True)[:limit]
