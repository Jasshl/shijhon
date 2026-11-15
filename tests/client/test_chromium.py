"""Headless Chromium checks: what a web player (an HTML audio element, as in Electron apps)
does with Shijhon's answers. Server responses alone do not establish client behavior; these
checks are automated and do not replace trying real apps.

Playwright is no dependency of the project: these run only where it is installed -
``uv run --with playwright pytest tests/client/test_chromium.py``, its browser installed
with ``uv run --with playwright python -m playwright install chromium --only-shell`` (and
``PLAYWRIGHT_BROWSERS_PATH`` where it went) -; skipped elsewhere.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack

pw = pytest.importorskip("playwright.sync_api")
pytestmark = [pytest.mark.client]

SECONDS = 60  # pink noise: several megabytes, more than the browser reads ahead at once

PAGE = """
window.events = [];
const el = new Audio(); el.preload = 'auto'; document.body.appendChild(el);
for (const e of ['loadstart','stalled','waiting','playing','pause','error','ended','seeking',
                 'seeked','loadedmetadata','canplay','suspend'])
  el.addEventListener(e, () => window.events.push([e, Math.round(el.currentTime * 10) / 10,
    el.error ? el.error.code : null]));
window.el = el; 0;
"""


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(), tmp_path_factory.mktemp("chromium"), warm_ahead_depth=0
    ) as w:
        yield w


@pytest.fixture
def a(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("A")
    world.add_source(addon)
    yield addon
    world.clear_sources()
    world.services.deliverer.settings.pin_ttl_seconds = 1800.0


def noise(world: DeliveryWorld, key: str) -> Path:
    path = world.tmp / f"{key}.flac"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=d={SECONDS}:c=pink:a=0.3:r=44100", "-ac", "2", "-c:a", "flac", str(path)],
        check=True,
    )  # fmt: skip
    return path


def song(world: DeliveryWorld, a: FakeAddon, key: str) -> tuple[str, FakeTrack]:
    found, _, _ = world.placeholder_track(key, [], seconds=SECONDS)
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT isrc FROM placeholders WHERE song_id = ?", [found]
        )
    )
    assert row is not None
    fake = a.add(FakeTrack(isrc=str(row["isrc"]), audio=noise(world, key), chunk_delay=0.002))
    return found, fake


def url(world: DeliveryWorld, song_id: str) -> str:
    client = world.client(client="web-player")
    query = "&".join(f"{k}={v}" for k, v in client.auth_params())
    return f"{world.server.base_url}/rest/stream.view?id={song_id}&{query}"


def loaded_ahead_then_played(world: DeliveryWorld, a: FakeAddon, key: str) -> list[Any]:
    """A web player loads a song ahead (not playing it), the song's link then expires and
    the add-on gives no new one; the player plays it, and the listener seeks past what the
    player has read. Returns the audio element's events."""
    song_id, fake = song(world, a, key)
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        try:
            page = browser.new_page()
            page.goto(f"{world.server.base_url}/rest/ping.view")
            page.evaluate(PAGE)
            page.evaluate(f"el.src = {json.dumps(url(world, song_id))};")
            time.sleep(4)  # loaded ahead, while another song plays
            world.services.deliverer.settings.pin_ttl_seconds = 0.0  # its link expired
            fake.stream_status = 503  # and the add-on gives no new one
            fake.expire_after = 0  # (nor does the old one work)
            page.evaluate("el.play();")
            time.sleep(1)
            page.evaluate(f"el.currentTime = {SECONDS * 0.7};")
            time.sleep(4)
            events: list[Any] = page.evaluate("window.events")
            events.append(["at", page.evaluate("el.currentTime"), None])
            return events
        finally:
            browser.close()


def test_a_song_loaded_ahead_plays_on_after_its_link_expired(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """Shijhon read the song on while the player waited, and serves the seek from that: no
    media error (which a web player retries from the song's start, then pauses on)."""
    events = loaded_ahead_then_played(world, a, "chromium-ahead")
    assert not [e for e in events if e[0] == "error"], events
    assert events[-1][1] > SECONDS * 0.7 + 1, events  # it plays on after the seek
