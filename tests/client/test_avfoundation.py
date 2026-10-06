"""Headless AVFoundation checks (macOS): what an AVPlayer-based client (most iOS and macOS
apps) does with Shijhon's URLs — play, seek, download. Server responses alone do not
establish client behavior; these checks are automated and do not replace trying real apps.

Runs on macOS with the Swift toolchain (``tools/avcheck.swift`` is compiled once and
cached); skipped elsewhere.

A Mac whose screen is locked (or whose lid is closed, without a display) plays no audio:
AVPlayer then plays nothing at all, and these tests fail - they are not skipped, since a
skip would hide a real failure. Their message says so first (``why``): it asks AVPlayer for
Navidrome's own track from Navidrome directly, and when that does not play either, the
failure is the machine's, not the code's.
"""

from __future__ import annotations

import hashlib
import json
import platform
import plistlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.dash_fixtures import dash_audio
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.library import simple_album, write_album
from tests.harness.paths import cache_dir, file_lock

pytestmark = [
    pytest.mark.client,
    pytest.mark.skipif(
        platform.system() != "Darwin" or shutil.which("swiftc") is None,
        reason="needs macOS with the Swift toolchain",
    ),
]

SOURCE = Path(__file__).resolve().parents[2] / "tools" / "avcheck.swift"


@pytest.fixture(scope="module")
def avcheck() -> Path:
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]
    binary = cache_dir() / "avcheck" / digest / "avcheck"
    with file_lock(cache_dir() / "avcheck" / ".lock"):
        if not binary.exists():
            binary.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["swiftc", "-O", "-parse-as-library", str(SOURCE), "-o", str(binary)], check=True
            )
    return binary


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Client Band", "Client Owned", 1, seconds=20))
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("avf"), budget_seconds=8.0) as w:
        w.add_source(w.addon("Source"))
        yield w


@pytest.fixture
def addon(world: DeliveryWorld) -> FakeAddon:
    return world.addons[0]


def url(world: DeliveryWorld, method: str, **params: Any) -> str:
    query = httpx.QueryParams([*world.client().auth_params(), *params.items()])
    return f"{world.server.base_url}/rest/{method}?{query}"


def run(binary: Path, *args: str) -> dict[str, Any]:
    """avcheck's result; one that gave none (or never ended) is a result too: the test's
    assertion fails with its reason (``why``), not this."""
    result: dict[str, Any]
    try:
        done = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"exit": -1, "error": "avcheck did not end within 120 s"}
    try:
        result = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        result = {"error": "avcheck gave no result", "stderr": done.stderr[-300:]}
    result["exit"] = done.returncode
    print("avcheck:", json.dumps(result, sort_keys=True))  # measurements, shown with -s
    return result


_MACHINE: dict[str, str] = {}  # whether this Mac plays audio at all, asked once


def mac_state() -> str:
    """What macOS says of its screen (a hint for the message, never decisive)."""
    try:
        listed = subprocess.run(
            ["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=10, check=False
        )
        root = plistlib.loads(listed.stdout)
        users = root.get("IOConsoleUsers") or []
        locked = root.get("IOConsoleLocked") or any(
            isinstance(user, dict) and user.get("CGSSessionScreenIsLocked") for user in users
        )
        return "its screen is locked" if locked else "its screen is not locked"
    except Exception:
        return "its screen's state is not known"


def why(world: DeliveryWorld, binary: Path, result: Any) -> str:
    """A failing check's message - first of all whether this Mac can play audio now: when
    AVPlayer does not play Navidrome's own track from Navidrome itself either, the failure
    is the machine's (its screen locked, its lid closed), not the code's."""
    if "verdict" not in _MACHINE:
        direct = world.nd.client()
        owned = direct.ok("search3", {"query": "Client Owned"})["searchResult3"]["song"][0]["id"]
        query = httpx.QueryParams([*direct.auth_params(), ("id", owned)])
        control = run(binary, "--stream", f"{world.nd.base_url}/rest/stream?{query}")
        if control.get("play"):  # it plays: the machine is not the reason (whatever else)
            _MACHINE["verdict"] = ""
        elif "play" not in control:
            _MACHINE["verdict"] = (
                "Whether this Mac can play audio now could not be told"
                f" ({control.get('error', 'no result')}; macOS says: {mac_state()}). "
            )
        else:
            _MACHINE["verdict"] = (
                "THIS MAC CANNOT PLAY AUDIO NOW - is its screen locked, or its lid closed?"
                f" (macOS says: {mac_state()}.) AVPlayer does not play Navidrome's own track"
                " from Navidrome directly either, so this failure is the machine's, not the"
                " code's: unlock the Mac and run tests/client again. "
            )
    return f"{_MACHINE['verdict']}avcheck: {result}"


def test_placeholder_plays_seeks_and_downloads(
    avcheck: Path, world: DeliveryWorld, addon: FakeAddon
) -> None:
    song, _, _ = world.placeholder_track("avf-flac", [addon], seconds=20)
    result = run(
        avcheck,
        "--stream",
        url(world, "stream", id=song),
        "--download",
        url(world, "download", id=song),
    )
    assert result["exit"] == 0, why(world, avcheck, result)
    played = result.get("play") and result.get("seek") and result.get("download")
    assert played, why(world, avcheck, result)
    assert abs(result["duration"] - 20) < 1.5, why(world, avcheck, result)


def test_owned_track_through_the_proxy(avcheck: Path, world: DeliveryWorld) -> None:
    owned = world.client().ok("search3", {"query": "Client Owned"})["searchResult3"]["song"][0][
        "id"
    ]
    result = run(avcheck, "--stream", url(world, "stream", id=owned))
    assert result["exit"] == 0, why(world, avcheck, result)


def test_lower_bitrate_request_behaves_like_an_owned_track(
    avcheck: Path, world: DeliveryWorld, addon: FakeAddon
) -> None:
    # Download-first, then Navidrome transcodes. Navidrome's transcoded streams are
    # progressive (no length, no ranges), so AVPlayer cannot seek them - for owned tracks
    # too. The check is parity with an owned track.
    song, _, _ = world.placeholder_track(
        "avf-mp3", [addon], seconds=20, fmt="m4a", content_type="audio/mp4"
    )
    owned = world.client().ok("search3", {"query": "Client Owned"})["searchResult3"]["song"][0][
        "id"
    ]
    results = [
        run(avcheck, "--stream", url(world, "stream", id=i, maxBitRate="96", format="mp3"))
        for i in (song, owned)
    ]
    placeholder, own = results
    assert placeholder.get("play") and own.get("play"), why(world, avcheck, results)
    assert placeholder.get("seek") == own.get("seek"), why(world, avcheck, results)


@pytest.mark.parametrize("quality", ["lossless", "aac"])
def test_a_dash_song_served_at_once_plays_seeks_and_tells_its_length(
    avcheck: Path, world: DeliveryWorld, addon: FakeAddon, quality: str
) -> None:
    """An MP4 of a DASH link's segments (FLAC in MP4, or AAC), served from its first
    segments on with its index: AVPlayer plays it, reads its length and seeks."""
    key = f"avf-dash-{quality}"
    song, _, _ = world.placeholder_track(key, [], seconds=20)
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT isrc FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert row is not None
    folder = dash_audio(key, 20)
    addon.add(FakeTrack(isrc=str(row["isrc"]), audio=folder / "out.mpd", dash=folder))
    settings = world.services.deliverer.settings
    if quality == "aac":
        settings.dash_quality_from, settings.dash_quality_to = "192", "320"
    try:
        result = run(avcheck, "--stream", url(world, "stream", id=song))
    finally:
        settings.dash_quality_from, settings.dash_quality_to = "any", "lossless"
    assert result["exit"] == 0, why(world, avcheck, result)
    assert result.get("play") and result.get("seek"), why(world, avcheck, result)
    assert abs(result["duration"] - 20) < 1.0, why(world, avcheck, result)
