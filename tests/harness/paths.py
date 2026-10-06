"""Shared cache locations for downloaded binaries and generated audio."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def cache_dir() -> Path:
    root = Path(os.environ.get("SHIJHON_TEST_CACHE", "~/.cache/shijhon")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialize work across pytest-xdist workers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
