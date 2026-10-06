"""The private development configuration shared by ``tools/devserver.py`` and the live
smoke tests: add-ons, and hand-written catalog releases to materialize.

It lives outside the repository (it holds add-on URLs and settings). See
``tools/devserver.example.toml`` for the format.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.delivery.netpolicy import Reach


@dataclass
class AddonConfig:
    name: str
    base_url: str = field(repr=False)
    reach: Reach = Reach.PUBLIC
    settings: dict[str, Any] = field(default_factory=dict, repr=False)
    budget_seconds: float | None = None  # own byte-zero budget


@dataclass
class ReleaseConfig:
    release: CatalogRelease
    owned: list[int]  # track numbers (disc 1) to create as synthetic owned files
    # False: only the owned files are written; the album is left to Shijhon's matching
    # and fill with the configured catalog.
    fill: bool = True


@dataclass
class DevConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    addons: list[AddonConfig]
    releases: list[ReleaseConfig]
    delivery: dict[str, Any] = field(default_factory=dict)  # Shijhon's [delivery] settings
    catalog: dict[str, Any] = field(default_factory=dict, repr=False)  # [catalog]
    search: dict[str, Any] = field(default_factory=dict)  # Shijhon's [search] settings
    fill: dict[str, Any] = field(default_factory=dict)  # Shijhon's [fill] settings
    cleanup: dict[str, Any] = field(default_factory=dict)  # Shijhon's [cleanup] settings
    commit_albums: list[str] = field(default_factory=list)  # catalog album IDs to add
    # Loggers at debug level (Shijhon's top-level log_debug); None: the environment's.
    log_debug: list[str] | None = None


def load(path: Path) -> DevConfig:
    data = tomllib.loads(path.read_text())
    server = data.get("server", {})
    navidrome = data.get("navidrome", {})
    addons = [
        AddonConfig(
            a["name"],
            a["base_url"],
            Reach(a.get("reach", "public")),
            a.get("settings", {}),
            a.get("budget_seconds"),
        )
        for a in data.get("addons", [])
    ]
    releases = []
    for index, item in enumerate(data.get("releases", []), start=1):
        ref = CatalogRef("dev", str(item.get("id", index)))
        tracks = tuple(
            CatalogTrack(
                ref=CatalogRef("dev", f"{ref.id}-{n}"),
                title=t["title"],
                artist=t.get("artist", item["artist"]),
                duration_ms=int(float(t["duration"]) * 1000),
                disc=int(t.get("disc", 1)),
                number=int(t.get("number", n)),
                isrc=t.get("isrc"),
                album=ref,
            )
            for n, t in enumerate(item["tracks"], start=1)
        )
        release = CatalogRelease(
            ref=ref,
            title=item["title"],
            artist=item["artist"],
            kind=ReleaseKind(item.get("kind", "album")),
            release_date=item.get("date"),
            tracks=tracks,
        )
        owned = [int(n) for n in item.get("owned", [])]
        releases.append(ReleaseConfig(release, owned, bool(item.get("fill", True))))
    return DevConfig(
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 4747)),
        user=navidrome.get("user", "admin"),
        password=navidrome["password"],
        addons=addons,
        releases=releases,
        delivery=dict(data.get("delivery", {})),
        catalog=dict(data.get("catalog", {})),
        search=dict(data.get("search", {})),
        fill=dict(data.get("fill", {})),
        cleanup=dict(data.get("cleanup", {})),
        commit_albums=[str(i) for i in data.get("commits", {}).get("albums", [])],
        log_debug=[str(n) for n in data["log_debug"]] if "log_debug" in data else None,
    )
