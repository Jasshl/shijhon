"""The placeholder engine wired to a test Navidrome (used inside async tests)."""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
)
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.store import Store
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

PLACEHOLDER_FOLDER = "_shijhon"


@dataclass
class EngineParts:
    engine: PlaceholderEngine
    navidrome: NavidromeService
    scans: ScanCoordinator
    silence: SilenceMaker
    store: Store
    layout: Layout


@asynccontextmanager
async def engine_for(nd: NavidromeInstance, state: Path) -> AsyncIterator[EngineParts]:
    service = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD, client_name="shijhon")
    store = await Store.open(state / "shijhon.sqlite3")
    scans = ScanCoordinator(service)
    silence = SilenceMaker()
    layout = Layout(nd.music, PLACEHOLDER_FOLDER)
    engine = PlaceholderEngine(
        layout=layout, navidrome=service, scans=scans, silence=silence, store=store
    )
    try:
        yield EngineParts(engine, service, scans, silence, store, layout)
    finally:
        await store.close()
        await service.aclose()


def catalog_release(
    key: str,
    title: str,
    artist: str,
    count: int,
    *,
    kind: ReleaseKind = ReleaseKind.ALBUM,
    date: str | None = "2020-05-06",
    seconds: int = 3,
    discs: int = 1,
    clean: bool = False,
    track_artist: str | None = None,
) -> CatalogRelease:
    ref = CatalogRef("test", key)
    per_disc = -(-count // discs)
    tracks = tuple(
        CatalogTrack(
            ref=CatalogRef("test", f"{key}-{n}"),
            title=f"{title} Song {n}",
            artist=track_artist or artist,
            duration_ms=seconds * 1000,
            disc=(n - 1) // per_disc + 1,
            number=(n - 1) % per_disc + 1,
            isrc=f"QZ{zlib.crc32(key.encode()) % 1000:03d}26{n:05d}",
            album=ref,
        )
        for n in range(1, count + 1)
    )
    return CatalogRelease(
        ref=ref,
        title=title,
        artist=artist,
        kind=kind,
        release_date=date,
        tracks=tracks,
        clean=clean,
    )
