"""Suite K — a person's own imported song below their minimum, played from the add-ons, is
upgraded with their audio afterwards (``music_imports/upgrade.py``): the first add-on's
audio below the minimum is passed over for the play and for the upgrade alike, the play
goes on from the add-ons' file - its later ranges are the bytes it began with, also once
the upgrade has replaced the song's file - and the next play is the upgraded file's.

Against a real, disposable Navidrome, through the dashboard's import API and the full app,
with invented music.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shijhon_demo_catalog import DemoCatalog
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.subsonic import SubsonicClient

IMPORTER = ("ember-importer", "invented-password-u")  # the default minimum (192)
ISRC = "XXQ0L2000101"
ALBUM = Album(
    "Quiet Hollow", "Ember Lane", (Track("Ember Three", 1, seconds=4.0, isrc=ISRC),), "mp3"
)
API = "/shijhon/collection/import/api"


@dataclass
class World:
    delivery: DeliveryWorld
    browser: Browser
    first: FakeAddon  # has the song as a 128 kbit/s MP3
    second: FakeAddon  # has it in FLAC
    flac: bytes
    sources: Path


def post(browser: Browser, path: str, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"X-CSRF-Token": browser.csrf(), "Origin": str(browser.http.base_url)}
    response = browser.http.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def settle(browser: Browser, job: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status: dict[str, Any] = browser.http.get(f"{API}/{job}?offset=0&limit=100").json()
        if status["state"] == state:
            return status
        assert status["state"] not in ("paused", "cancelled"), status
        time.sleep(0.1)
    pytest.fail(f"the import did not reach {state}")


def imported(world: World) -> str:
    """The importer's own import of ``ALBUM``: its song ID."""
    browser = world.browser
    paths = write_album(world.sources, ALBUM)
    job = post(browser, API, {"request_key": "ember"})
    path = f"{API}/{job['id']}"
    entries = [{"client_key": f"f{n}", "path": p.name, "size": p.stat().st_size}
               for n, p in enumerate(paths)]  # fmt: skip
    job = post(browser, path + "/manifest", {"page_key": "first", "entries": entries})
    post(browser, path + "/seal", {})
    by_key = {f"f{n}": p for n, p in enumerate(paths)}
    headers = {
        "X-CSRF-Token": browser.csrf(),
        "Origin": str(browser.http.base_url),
        "Content-Type": "application/octet-stream",
    }
    for item in job["items"]:
        response = browser.http.post(
            f"{path}/files/{item['id']}?attempt=0",
            content=by_key[item["client_key"]].read_bytes(),
            headers=headers,
        )
        assert response.status_code == 200, response.text
    post(browser, path + "/review", {})
    reviewed = settle(browser, job["id"], "review")
    post(browser, path + "/approve", {"revision": reviewed["revision"], "request_key": "go"})
    done = settle(browser, job["id"], "done")
    [item] = done["items"]
    assert item["state"] == "complete", item
    return str(item["song_id"])


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    (nd.music / "_imports").mkdir()
    nd.create_user(*IMPORTER)
    tmp = tmp_path_factory.mktemp("quality-upgrade")
    with delivery_world(
        nd,
        tmp,
        catalog=DemoCatalog([], region="xx", names=True),
        quality_default="192",
        music_imports={"enabled": True, "min_free_bytes": 0},
    ) as delivery:
        first, second = delivery.addon("First"), delivery.addon("Second")
        delivery.add_source(first)
        delivery.add_source(second)
        worse = delivery.audio("ember-worse", "mp3", 4)
        first.add(FakeTrack(isrc=ISRC, audio=worse, content_type="audio/mpeg"))
        flac = delivery.audio("ember-better", "flac", 4)
        second.add(FakeTrack(isrc=ISRC, audio=flac))
        browser = Browser(delivery.server.base_url)
        assert browser.sign_in(*IMPORTER).status_code == 303
        try:
            yield World(delivery, browser, first, second, flac.read_bytes(), tmp / "sources")
        finally:
            browser.close()


def test_a_play_goes_on_from_the_addons_file_after_the_upgrade(world: World) -> None:
    song = imported(world)
    native = world.delivery.nd.client()
    assert native.ok("getSong", {"id": song})["song"]["suffix"] == "mp3"
    client = SubsonicClient(world.delivery.server.base_url, *IMPORTER)
    try:
        # The first add-on's MP3 is below the minimum: the second's FLAC plays.
        first = client.request("stream", {"id": song})
        assert first.status_code == 200
        assert hashlib.sha256(first.content).digest() == hashlib.sha256(world.flac).digest()
        # The play upgrades the import with the second's FLAC (the first's MP3 is passed over
        # again), the song keeping its ID - and its quality is read anew.
        after = world.delivery.services.interceptor.upgrade_after
        assert after is not None
        deadline = time.monotonic() + 60
        while (
            native.ok("getSong", {"id": song})["song"]["suffix"] != "flac"
            or song in after.upgrading  # type: ignore[attr-defined]
        ):
            assert time.monotonic() < deadline, "the import was not upgraded"
            time.sleep(0.2)
        upgraded = native.request("stream", {"id": song}).content
        assert upgraded[:4] == b"fLaC" and upgraded != world.flac  # (the import's tags)
        # A later range of the play under way: the bytes it began with, not the new file's.
        for span, (start, end) in (("bytes=10-99", (10, 100)), ("bytes=20000-", (20000, None))):
            part = client.request("stream", {"id": song}, headers={"range": span})
            assert part.status_code == 206, span
            assert part.content == first.content[start:end], span
        # A new play: the upgraded file, which meets the minimum - nothing asked; its later
        # ranges are that file's too.
        asked = len(world.first.requests()) + len(world.second.requests())
        again = client.request("stream", {"id": song})
        assert again.content == upgraded
        part = client.request("stream", {"id": song}, headers={"range": "bytes=10-99"})
        assert part.status_code == 206 and part.content == upgraded[10:100]
        assert len(world.first.requests()) + len(world.second.requests()) == asked
    finally:
        client.close()
