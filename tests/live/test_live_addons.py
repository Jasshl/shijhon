"""Live smoke test with real add-ons (opt-in).

Runs only with ``-m live`` and ``SHIJHON_LIVE_CONFIG`` pointing at the private development
configuration (see ``tools/devserver.example.toml``). For each add-on alone, and for the
first track of each configured release (``SHIJHON_LIVE_TRACKS`` per release): cold start
through Shijhon (time to the first 64 KiB), a seek to the
middle, a second play of the same track (pinned), and ``HEAD``. Results — timings, status
codes, delivered format; never URLs or settings — go to ``SHIJHON_LIVE_REPORT`` (default:
next to the configuration file, outside the repository).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from tools.devconfig import load

from tests.conftest import NavidromeFactory
from tests.harness.delivery import delivery_world

pytestmark = pytest.mark.live

CONFIG = os.environ.get("SHIJHON_LIVE_CONFIG")
MAGIC = {b"fLaC": "flac", b"ID3": "mp3", b"OggS": "ogg", b"\xff\xfb": "mp3", b"\xff\xf3": "mp3"}


def container(head: bytes) -> str:
    for magic, name in MAGIC.items():
        if head.startswith(magic):
            return name
    if head[4:8] == b"ftyp":
        return "mp4"
    return "unknown"


@pytest.mark.skipif(not CONFIG, reason="SHIJHON_LIVE_CONFIG is not set")
def test_real_addons(navidrome_factory: NavidromeFactory, tmp_path: Path) -> None:
    assert CONFIG is not None
    config = load(Path(CONFIG))
    report_path = Path(
        os.environ.get("SHIJHON_LIVE_REPORT", Path(CONFIG).with_suffix(".report.json"))
    )
    report: list[dict[str, Any]] = []
    nd = navidrome_factory()
    with delivery_world(
        nd, tmp_path, real_dns=True, budget_seconds=9.0, seek_timeout_seconds=15.0
    ) as world:
        # A few tracks only (the add-ons are real services with quotas): by default the
        # first track of each configured release.
        per_release = int(os.environ.get("SHIJHON_LIVE_TRACKS", "1"))
        songs = []
        for item in config.releases:
            result = world.materialize(item.release)
            chosen = [t for t in item.release.tracks if t.ref in result.created][:per_release]
            songs += [(t, result.created[t.ref]) for t in chosen]
        for addon in config.addons:
            world.clear_sources()
            world.server.call(
                lambda a=addon: world.services.sources.add(
                    a.name, a.base_url, a.settings, reach=a.reach, budget_seconds=a.budget_seconds
                )
            )
            for track, song in songs:
                world.services.deliverer.forget(song)
                entry: dict[str, Any] = {
                    "addon": addon.name,
                    "track": track.title,
                    "isrc": track.isrc,
                }
                client = world.client()
                started = time.monotonic()
                response = client.request("stream", {"id": song}, stream=True)
                try:
                    first = b""
                    for chunk in response.iter_bytes():
                        first += chunk
                        if len(first) >= 65536:
                            break
                    entry.update(
                        cold_status=response.status_code,
                        cold_first_64k_s=round(time.monotonic() - started, 3),
                        content_type=response.headers.get("content-type"),
                        size=response.headers.get("content-length"),
                        container=container(first[:12]),
                    )
                    if not entry["content_type"] or entry["content_type"].startswith(
                        "application/"
                    ):
                        try:  # the Subsonic error message: reasons per source, no URLs
                            entry["error"] = json.loads(first)["subsonic-response"]["error"][
                                "message"
                            ]
                        except (ValueError, KeyError, TypeError):
                            entry["error"] = "unreadable answer"
                finally:
                    response.close()
                if entry["cold_status"] == 200 and entry["size"]:
                    middle = int(entry["size"]) // 2
                    started = time.monotonic()
                    seek = client.request(
                        "stream",
                        {"id": song},
                        headers={"range": f"bytes={middle}-{middle + 65535}"},
                    )
                    entry.update(
                        seek_status=seek.status_code, seek_s=round(time.monotonic() - started, 3)
                    )
                    started = time.monotonic()
                    again = client.request("stream", {"id": song}, stream=True)
                    try:
                        next(again.iter_bytes(), b"")
                        entry.update(
                            replay_status=again.status_code,
                            replay_first_s=round(time.monotonic() - started, 3),
                        )
                    finally:
                        again.close()
                    head = client.request("stream", {"id": song}, http_method="HEAD")
                    entry.update(
                        head_status=head.status_code, head_length=head.headers.get("content-length")
                    )
                source = next(iter(world.services.sources._stats.values()), None)
                entry["last_failure"] = source.last_failure if source else None
                report.append(entry)
        world.clear_sources()
    report_path.write_text(json.dumps(report, indent=1))
    played = [e for e in report if e.get("cold_status") == 200 and e["container"] != "unknown"]
    assert played, f"no track played; see {report_path}"
    for entry in played:
        assert entry.get("seek_status") == 206, entry
