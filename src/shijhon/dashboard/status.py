"""What Diagnostics and the Catalog page report: Navidrome, the catalog, storage and
the database. Read-only; every call has a short time limit and failures become short
reasons (never URLs)."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

import anyio

from shijhon.catalog.base import CatalogError
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.store import Store

PINNED_NAVIDROME = "0.64.2"  # packaging/compose.yaml pins the same version
LIMIT = 5.0


@dataclass
class NavidromeStatus:
    answering: bool
    took_ms: float | None = None
    error: str | None = None
    version: str | None = None
    songs: int | None = None
    albums: int | None = None
    last_scan: float | None = None  # wall clock
    purge_missing: str | None = None


async def navidrome_status(navidrome: NavidromeService | None) -> NavidromeStatus:
    if navidrome is None:
        return NavidromeStatus(False, error="Shijhon has not started")
    started = time.perf_counter()
    try:
        with anyio.fail_after(LIMIT):
            ping = await navidrome.subsonic("ping")
    except TimeoutError:
        return NavidromeStatus(False, error="no answer within 5 s")
    except NavidromeError as exc:
        return NavidromeStatus(False, error=str(exc))
    except (KeyError, TypeError, ValueError) as exc:  # an unexpected answer
        return NavidromeStatus(False, error=f"unexpected answer ({type(exc).__name__})")
    status = NavidromeStatus(True, took_ms=(time.perf_counter() - started) * 1000)
    version = ping.get("serverVersion")
    status.version = str(version).split(" ")[0] if version else None
    try:
        with anyio.fail_after(LIMIT):
            scan = await navidrome.scan_status()
            status.songs = int(scan.get("count") or 0)
            status.last_scan = scan_time(scan.get("lastScan"))
            albums = await navidrome.native("GET", "album", params={"_start": 0, "_end": 1})
            total = albums.headers.get("x-total-count")
            status.albums = int(total) if total and total.isdigit() else None
            config = await navidrome.config()
    except (TimeoutError, NavidromeError, KeyError, TypeError, ValueError):
        return status  # the rest is optional
    values = config.get("config") if isinstance(config, dict) else None
    scanner = values.get("Scanner") if isinstance(values, dict) else None
    purge = scanner.get("PurgeMissing") if isinstance(scanner, dict) else None
    status.purge_missing = str(purge) if purge is not None else None
    return status


def scan_time(text: Any) -> float | None:
    """Navidrome's ``lastScan`` (RFC 3339 with up to nanoseconds, e.g.
    ``2025-03-14T08:05:12.123456789Z``) as a wall-clock time; None when it has none (also
    Go's zero time, before any scan) or it cannot be read."""
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if at.tzinfo is None or at.year < 2000:
        return None
    return at.timestamp()


@dataclass
class CatalogCheck:
    at: float  # wall clock
    ok: bool
    took_ms: float
    error: str | None = None


async def check_catalog(catalog: Any) -> CatalogCheck:
    """One small request past the cache: the adapter's own check when it has one (an
    optional ``check()`` of the catalog), else a search."""
    inner = getattr(catalog, "inner", catalog)
    started = time.perf_counter()
    error = None
    try:
        with anyio.fail_after(10):
            check = getattr(inner, "check", None)
            if callable(check):
                await check()
            else:
                await inner.search("test", 1)
    except TimeoutError:
        error = "timeout: no answer within 10 s"
    except CatalogError as exc:
        error = f"{exc.kind}: {exc.reason}"
    except (OSError, ValueError, RuntimeError) as exc:
        error = type(exc).__name__
    return CatalogCheck(time.time(), error is None, (time.perf_counter() - started) * 1000, error)


@dataclass
class Storage:
    placeholders: int
    placeholder_bytes: int
    delivered: int
    delivered_bytes: int
    at: float


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def storage(store: Store, library: Path | None) -> Storage:
    rows = await store.fetchall("SELECT path, state FROM placeholders")
    paths = [(row["path"], row["state"]) for row in rows]

    def measure() -> tuple[int, int]:
        placeholder = delivered = 0
        for path, state in paths:
            size = _size(library / path) if library is not None else 0
            if state == "delivered":
                delivered += size
            else:
                placeholder += size
        return placeholder, delivered

    placeholder_bytes, delivered_bytes = await anyio.to_thread.run_sync(measure)
    delivered = sum(1 for _, state in paths if state == "delivered")
    return Storage(
        len(paths) - delivered, placeholder_bytes, delivered, delivered_bytes, time.time()
    )


def bundled_schema() -> int:
    folder = resources.files("shijhon.store") / "migrations"
    return sum(1 for entry in folder.iterdir() if re.match(r"^\d{4}_[a-z0-9_]+\.sql$", entry.name))


async def schema(store: Store) -> int:
    row = await store.fetchone("PRAGMA user_version")
    return int(row[0]) if row is not None else 0


def size_text(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "bytes":
                return f"{int(value):,} bytes"
            return f"{value:,.1f} {unit}".replace(".0 ", " ")
        value /= 1000
    return f"{size} bytes"


def private_mode(path: Path) -> bool:
    """Whether a file is readable by its owner only (0600 or stricter)."""
    try:
        return os.stat(path).st_mode & 0o077 == 0
    except OSError:
        return True
