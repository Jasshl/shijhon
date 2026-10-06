"""``shijhon usage-export --every`` as a process of its own, as the deployment's
"usage-export" service runs it beside Shijhon: it reads Navidrome's database, writes the
export, and makes one whenever Shijhon asks (the request file)."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tests.harness.navidrome import start_tethered, stop_group

COMMAND = [sys.executable, "-c", "from shijhon.cli import main; main()", "usage-export"]


class Exporter:
    def __init__(self, navidrome_db: Path, out: Path, every: float) -> None:
        self.navidrome_db, self.out, self.every = navidrome_db, out, every
        self._running: tuple[object, int] | None = None

    def start(self) -> None:
        if self._running is not None:
            return
        self.out.parent.mkdir(parents=True, exist_ok=True)
        before = self.out.stat().st_mtime_ns if self.out.exists() else None
        command = [*COMMAND, "--navidrome-db", str(self.navidrome_db), "--out", str(self.out),
                   "--every", str(self.every)]  # fmt: skip
        self._running = start_tethered(command)
        deadline = time.monotonic() + 30
        while not self.out.exists() or self.out.stat().st_mtime_ns == before:
            assert time.monotonic() < deadline, "no usage export was written"
            time.sleep(0.05)

    def stop(self) -> None:
        if self._running is not None:
            process, alive = self._running
            self._running = None
            stop_group(process, alive, grace=5.0)  # type: ignore[arg-type]


@contextmanager
def exporting(navidrome_db: Path, out: Path, *, every: float = 3600.0) -> Iterator[Exporter]:
    exporter = Exporter(navidrome_db, out, every)
    exporter.start()
    try:
        yield exporter
    finally:
        exporter.stop()
