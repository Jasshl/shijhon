"""Importing music below the importing person's minimum quality: the catalog's version is
added instead and plays from the add-ons (``music_imports/overlap.py``).

Against a real, disposable Navidrome, through the dashboard's import API and the full app,
with a catalog of invented releases and invented music. Every person's minimum is their
own: the installation's default here is 192 kbps (compressed audio), lossless at any bitrate.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from shijhon.catalog.model import CatalogRef, CatalogRelease, release_data
from shijhon_demo_catalog import DemoCatalog
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.library import Album, Format, Track, write_album

GATED = ("gate-importer", "invented-password-g")  # the default minimum (192)
EASY = ("easy-importer", "invented-password-e")  # chose any quality


def demo_release(key: str, title: str, artist: str, count: int) -> CatalogRelease:
    """An invented release of the demo catalog: songs of three seconds, as the files."""
    made = catalog_release(key, title, artist, count, seconds=3)
    ref = CatalogRef("demo", made.ref.id)
    tracks = tuple(replace(t, ref=CatalogRef("demo", t.ref.id), album=ref) for t in made.tracks)
    return replace(made, ref=ref, tracks=tracks)


IN_CATALOG = {
    "night": demo_release("9100001", "Night Letters", "Tide Paper", 3),
    "glass": demo_release("9100002", "Glass Hours", "Tide Paper", 2),
    "paper": demo_release("9100003", "Paper Garden", "Glass Orchard", 2),
    "salt": demo_release("9100004", "Salt Lines", "Glass Orchard", 2),
}
# Not in the catalog.
EMBER = Album("Quiet Hollow", "Ember Road",
              (Track("Ember One", 1, seconds=3.0), Track("Ember Two", 2, seconds=3.0)),
              "mp3")  # fmt: skip


def files_of(release: CatalogRelease, fmt: Format) -> Album:
    """The files of a catalog release, tagged as a person's copy of it."""
    songs = tuple(Track(t.title, t.number, seconds=3.0, isrc=t.isrc) for t in release.tracks)
    return Album(release.artist, release.title, songs, fmt)


@dataclass
class World:
    delivery: DeliveryWorld
    people: dict[str, tuple[str, Browser]]  # name -> (user ID, signed-in browser)
    sources: Path

    def importer(self, name: str) -> Importer:
        owner, browser = self.people[name]
        return Importer(self, owner, browser)


@dataclass
class Importer:
    world: World
    owner: str
    browser: Browser
    base: str = "/shijhon/collection/import/api"

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"X-CSRF-Token": self.browser.csrf(), "Origin": str(self.browser.http.base_url)}
        response = self.browser.http.post(path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result

    def status(self, job: str) -> dict[str, Any]:
        answer = self.browser.http.get(f"{self.base}/{job}?offset=0&limit=100")
        assert answer.status_code == 200, answer.text
        result: dict[str, Any] = answer.json()
        return result

    def settle(self, job: str, state: str, *, revision: int = 0) -> dict[str, Any]:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = self.status(job)
            if status["state"] == state and status["revision"] > revision:
                return status
            assert status["state"] not in ("paused", "cancelled"), status
            time.sleep(0.1)
        pytest.fail(f"the import did not reach {state}: {status}")

    def review(self, album: Album, key: str) -> dict[str, Any]:
        paths = write_album(self.world.sources / key, album)
        job = self.post(self.base, {"request_key": key})
        path = f"{self.base}/{job['id']}"
        entries = [
            {"client_key": f"f{n}", "path": p.name, "size": p.stat().st_size}
            for n, p in enumerate(paths)
        ]
        job = self.post(path + "/manifest", {"page_key": "first", "entries": entries})
        self.post(path + "/seal", {})
        by_key = {f"f{n}": p for n, p in enumerate(paths)}
        headers = {
            "X-CSRF-Token": self.browser.csrf(),
            "Origin": str(self.browser.http.base_url),
            "Content-Type": "application/octet-stream",
        }
        for item in job["items"]:
            response = self.browser.http.post(
                f"{path}/files/{item['id']}?attempt=0",
                content=by_key[item["client_key"]].read_bytes(),
                headers=headers,
            )
            assert response.status_code == 200, response.text
        self.post(path + "/review", {})
        return self.settle(job["id"], "review")

    def approve(self, reviewed: dict[str, Any]) -> dict[str, Any]:
        path = f"{self.base}/{reviewed['id']}"
        self.post(path + "/approve", {"revision": reviewed["revision"], "request_key": "go"})
        return self.settle(reviewed["id"], "done")

    def published(self, job: str) -> list[Path]:
        folder = self.world.delivery.nd.music / "_imports" / f"batch-{job}"
        return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []

    def own(self, song_ids: list[str]) -> set[str]:
        collection = self.world.delivery.app.services.collection
        assert collection is not None

        async def visible() -> set[str]:
            return set(
                await collection.visible_song_ids(self.owner, "1", song_ids, profile_id="original")
            )

        return self.world.delivery.server.call(visible)


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    (nd.music / "_imports").mkdir()
    ids = {name: nd.create_user(name, password) for name, password in (GATED, EASY)}
    records = [{"path": f"albums/{r.ref.id}", "body": release_data(r)} for r in IN_CATALOG.values()]
    tmp = tmp_path_factory.mktemp("import-quality")
    with delivery_world(
        nd,
        tmp,
        catalog=DemoCatalog(records, region="xx", names=True),
        quality_default="192",
        music_imports={"enabled": True, "min_free_bytes": 0},
    ) as delivery:
        quality = delivery.app.services.quality
        assert quality is not None
        delivery.server.call(lambda: quality.set(ids[EASY[0]], "any"))
        people = {}
        for name, password in (GATED, EASY):
            browser = Browser(delivery.server.base_url)
            assert browser.sign_in(name, password).status_code == 303
            people[name] = (ids[name], browser)
        try:
            yield World(delivery, people, tmp / "sources")
        finally:
            for _, browser in people.values():
                browser.close()


def test_a_low_quality_album_the_catalog_has_is_added_from_the_catalog(world: World) -> None:
    importer = world.importer(GATED[0])
    reviewed = importer.review(files_of(IN_CATALOG["night"], "mp3"), "night")
    assert reviewed["low_quality_files"] == 3 and reviewed["eligible_files"] == 0
    for item in reviewed["items"]:
        assert item["outcome"] == "low_quality" and item["state"] == "catalog", item
        assert item["match"]["route"] == "catalog"
        assert item["match"]["reason"].startswith("MP3 128 kbps is below your minimum")
    done = importer.approve(reviewed)
    assert done["low_quality_files"] == 3 and done["complete_files"] == 0
    assert {i["state"] for i in done["items"]} == {"linked"}
    assert importer.published(done["id"]) == []  # no file of the import was published
    # The catalog's album is in the library once (Navidrome's own answer), with its three
    # songs, and they count as the importer's own imports.
    native = world.delivery.nd.client()
    found = native.ok("search3", {"query": "Night Letters", "albumCount": 10, "songCount": 0})
    albums = [a for a in found["searchResult3"].get("album", []) if a["name"] == "Night Letters"]
    assert len(albums) == 1 and albums[0]["songCount"] == 3, albums
    song_ids = [i["match"]["song_id"] for i in done["items"]]
    detail = native.ok("getAlbum", {"id": albums[0]["id"]})["album"]
    assert {s["id"] for s in detail["song"]} == set(song_ids)
    assert importer.own(song_ids) == set(song_ids)


def test_a_low_quality_album_the_catalog_lacks_is_added_and_marked(world: World) -> None:
    importer = world.importer(GATED[0])
    reviewed = importer.review(EMBER, "ember")
    assert reviewed["below_minimum_files"] == 2 and reviewed["low_quality_files"] == 0
    for item in reviewed["items"]:
        assert item["outcome"] == "new" and item["state"] == "eligible"
        assert item["match"]["route"] == "files"
        assert item["match"]["reason"].endswith(
            "The catalog does not have it, so your file is added."
        )
    done = importer.approve(reviewed)
    assert done["complete_files"] == 2 and len(importer.published(done["id"])) == 2


def test_add_my_files_anyway(world: World) -> None:
    importer = world.importer(GATED[0])
    reviewed = importer.review(files_of(IN_CATALOG["glass"], "mp3"), "glass")
    assert reviewed["low_quality_files"] == 2
    first = reviewed["items"][0]
    path = f"{importer.base}/{reviewed['id']}"
    importer.post(path + "/keep", {"item": first["id"], "keep": True})
    again = importer.settle(reviewed["id"], "review", revision=reviewed["revision"])
    assert again["low_quality_files"] == 0 and again["eligible_files"] == 2
    for item in again["items"]:
        assert item["outcome"] == "new" and item["keep_files"] is True
        assert item["match"]["kept"] is True
    done = importer.approve(again)
    assert done["complete_files"] == 2 and len(importer.published(done["id"])) == 2


def test_audio_that_meets_the_minimum_is_not_gated(world: World) -> None:
    importer = world.importer(GATED[0])
    reviewed = importer.review(files_of(IN_CATALOG["paper"], "flac"), "paper")
    assert reviewed["eligible_files"] == 2 and reviewed["below_minimum_files"] == 0
    assert all("below_minimum" not in item["match"] for item in reviewed["items"])
    done = importer.approve(reviewed)
    assert done["complete_files"] == 2


def test_each_persons_own_minimum(world: World) -> None:
    importer = world.importer(EASY[0])  # any quality
    reviewed = importer.review(files_of(IN_CATALOG["salt"], "mp3"), "salt")
    assert reviewed["eligible_files"] == 2 and reviewed["low_quality_files"] == 0
    assert all(item["outcome"] == "new" for item in reviewed["items"])
    done = importer.approve(reviewed)
    assert done["complete_files"] == 2 and len(importer.published(done["id"])) == 2
