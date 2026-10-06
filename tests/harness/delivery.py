"""A running Shijhon in front of a test Navidrome, with fake add-ons as sources."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shijhon.app import Services, ShijhonApp
from shijhon.catalog.base import Catalog
from shijhon.catalog.model import CatalogRelease
from shijhon.config import load_settings
from shijhon.delivery.netpolicy import Reach
from shijhon.placeholders.engine import MaterializeResult
from tests.harness.engine import PLACEHOLDER_FOLDER, catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack, fake_resolver
from tests.harness.library import Format, frequency_for, tone
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance
from tests.harness.running import RunningServer
from tests.harness.subsonic import SubsonicClient

# Scaled-down timings (the 8-10 s byte-zero budget becomes 2.5 s in tests).
TEST_DELIVERY = {
    "budget_seconds": 2.5,
    "seek_timeout_seconds": 3.0,
    "cooldown_seconds": 1.5,
    "request_timeout_seconds": 5.0,
    "download_timeout_seconds": 30.0,
    # Tests play songs one after another: fetches ahead have tests of their own.
    "ahead_window_seconds": 0.0,
    # Test tones are a few seconds long, whatever the catalog says: the length check
    # has tests of its own.
    "length_tolerance_seconds": 0.0,
    "length_tolerance_percent": 0.0,
    # Tests ask their fake add-ons as fast as they run: the limits on what one add-on is
    # sent have tests of their own.
    "addon_requests_per_second": 0.0,
    "addon_audio_openings": 0,
    # ... and download the hour's allowance of songs in a row (its burst has tests too).
    "user_download_burst": 0,
}


@dataclass
class DeliveryWorld:
    nd: NavidromeInstance
    app: ShijhonApp
    server: RunningServer
    tmp: Path
    dns: dict[str, str]
    addons: list[FakeAddon] = field(default_factory=list)

    @property
    def services(self) -> Services:
        assert self.app.services is not None
        return self.app.services

    def client(self, **kwargs: Any) -> SubsonicClient:
        return SubsonicClient(self.server.base_url, ADMIN_USER, ADMIN_PASSWORD, **kwargs)

    def addon(self, name: str, **kwargs: Any) -> FakeAddon:
        addon = FakeAddon(name, **kwargs)
        addon.start()
        self.addons.append(addon)
        return addon

    def add_source(
        self,
        addon: FakeAddon,
        *,
        reach: Reach = Reach.LOOPBACK,
        settings: dict[str, Any] | None = None,
        base_url: str | None = None,
        budget_seconds: float | None = None,
    ) -> int:
        return self.server.call(
            lambda: self.services.sources.add(
                addon.name,
                base_url or addon.base_url,
                settings,
                reach=reach,
                budget_seconds=budget_seconds,
            )
        )

    def clear_sources(self) -> None:
        async def clear() -> None:
            for row in await self.services.store.fetchall("SELECT id FROM sources"):
                await self.services.sources.remove(row["id"])

        self.server.call(clear)

    def materialize(self, release: CatalogRelease, **kwargs: Any) -> MaterializeResult:
        return self.server.call(lambda: self.services.engine.materialize(release, **kwargs))

    def audio(self, seed: str, fmt: Format = "flac", seconds: float = 3) -> Path:
        path = self.tmp / f"{seed}-{uuid.uuid4().hex[:6]}.{fmt}"
        shutil.copyfile(tone(frequency_for(seed), seconds, fmt), path)
        return path

    def placeholder_track(
        self,
        key: str,
        addons: list[FakeAddon],
        *,
        fmt: Format = "flac",
        seconds: float = 3,
        **track: Any,
    ) -> tuple[str, dict[FakeAddon, FakeTrack], Path]:
        """Materialize a one-track catalog release and give each add-on its audio.
        Returns (song ID, fake tracks, the audio file)."""
        release = catalog_release(key, f"Title {key}", f"Artist {key}", 1, seconds=int(seconds))
        result = self.materialize(release)
        song_id = result.created[release.tracks[0].ref]
        audio = self.audio(key, fmt, seconds)
        isrc = release.tracks[0].isrc
        assert isrc is not None
        fakes = {a: a.add(FakeTrack(isrc=isrc, audio=audio, **track)) for a in addons}
        return song_id, fakes, audio

    def placeholder_files(self) -> list[Path]:
        """Files in the placeholder folder (not the hidden staging area)."""
        folder = self.nd.music / PLACEHOLDER_FOLDER
        if not folder.exists():
            return []
        return sorted(
            p
            for p in folder.rglob("*")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(folder).parts)
        )

    def placeholder_rows(self) -> int:
        async def count() -> int:
            row = await self.services.store.fetchone("SELECT COUNT(*) AS n FROM placeholders")
            return int(row["n"]) if row else 0

        return self.server.call(count)

    def stop(self) -> None:
        self.server.stop()
        for addon in self.addons:
            addon.stop()


@contextmanager
def delivery_world(
    nd: NavidromeInstance,
    tmp: Path,
    *,
    real_dns: bool = False,
    catalog: Catalog | None = None,
    fill: bool | dict[str, Any] = False,
    covers: dict[str, Any] | None = None,
    catalog_settings: dict[str, Any] | None = None,
    navidrome_database: Path | None = None,
    usage_export: Path | None = None,
    cleanup: dict[str, Any] | None = None,
    **delivery: Any,
) -> Iterator[DeliveryWorld]:
    """``fill``: match and fill partially owned albums (off by default: its background
    catalog requests would disturb suites that count them); a dict gives its settings.
    ``covers``: catalog cover settings (covers fetched ahead are off by default, for the
    same reason). ``catalog_settings``: the rest of the ``[catalog]`` section (its kind
    and the adapter's settings; the catalog itself is the one given).
    ``usage_export`` (the usage export) or ``navidrome_database`` (Navidrome's database
    itself, read only), and ``cleanup``: the cleanup of unused releases (none without one
    of the two)."""
    dns = {"private.fake.test": "10.20.30.40", "metadata.fake.test": "169.254.169.254"}
    settings = load_settings(
        None,
        state_dir=tmp / "state",
        navidrome={
            "url": nd.base_url,
            "user": ADMIN_USER,
            "password": ADMIN_PASSWORD,
            "library_path": nd.music,
            "database_path": navidrome_database,
            "usage_export_path": usage_export,
        },
        placeholders={"folder": PLACEHOLDER_FOLDER},
        cleanup=cleanup or {},
        delivery={**TEST_DELIVERY, **delivery},
        fill=fill if isinstance(fill, dict) else {"enabled": fill},
        catalog={"prefetch_covers": 0, **(covers or {}), **(catalog_settings or {})},
    )
    resolver = {} if real_dns else {"resolver": fake_resolver(dns)}
    app = ShijhonApp(settings, catalog=catalog, **resolver)
    server = RunningServer(app)
    server.start()
    world = DeliveryWorld(nd, app, server, tmp, dns)
    try:
        yield world
    finally:
        world.stop()
