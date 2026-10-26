"""Favorites made before (or past Shijhon) count also for accounts that use only the
Collection page or the native website - no app: their favorites are read with the
account's own native sign-in there.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

PASSWORD = "invented-native-password"
WEB = "website-only"
PAGE = catalog_release("native-page", "Page Favorite", "Invented Pager", 3)
SITE = catalog_release("native-site", "Site Favorite", "Invented Browser", 3)


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, dict[str, list[str]]]]:
    nd = navidrome_factory()
    nd.create_user(WEB, PASSWORD)
    with delivery_world(nd, tmp_path_factory.mktemp("native-favorites")) as w:
        songs = {}
        for release in (PAGE, SITE):
            created = w.materialize(release).created
            songs[release.title] = [created[t.ref] for t in release.tracks]
        prepare_collection(nd, w.server)  # signs in; reads no page and no list
        prepare_collection(nd, w.server, user=WEB, password=PASSWORD)
        yield w, songs


def visible(world: DeliveryWorld, user: str, songs: list[str]) -> set[str]:
    collection = world.services.collection
    assert collection is not None
    owner = str(world.nd.login(user, PASSWORD if user == WEB else ADMIN_PASSWORD)["id"])
    return set(world.server.call(lambda: collection.visible_song_ids(owner, "1", songs)))


def test_the_collection_page_reads_the_account_s_favorites(
    world: tuple[DeliveryWorld, dict[str, list[str]]],
) -> None:
    w, songs = world
    album = songs[PAGE.title]
    w.nd.client().ok("star", {"id": album[1]})  # at Navidrome, past Shijhon
    assert visible(w, ADMIN_USER, album) == set()
    browser = Browser(w.server.base_url)
    try:
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        assert browser.get("collection").status_code == 200
    finally:
        browser.close()
    assert visible(w, ADMIN_USER, album) == set(album)  # the whole album, in All


def test_the_native_website_reads_the_account_s_favorites(
    world: tuple[DeliveryWorld, dict[str, list[str]]],
) -> None:
    w, songs = world
    album = songs[SITE.title]
    w.nd.client(WEB, PASSWORD).ok("star", {"id": album[0]})  # at Navidrome, past Shijhon
    assert visible(w, WEB, album) == set()
    token = w.nd.login(WEB, PASSWORD)["token"]
    deadline = time.monotonic() + 15
    while True:
        listed = httpx.get(
            w.server.base_url + "/api/album",
            params={"_sort": "name", "_order": "ASC", "_start": 0, "_end": 50},
            headers={"x-nd-authorization": "Bearer " + token},
            timeout=10,
        )
        if listed.status_code == 200 and SITE.title in {a["name"] for a in listed.json()}:
            break
        assert time.monotonic() < deadline, listed.text
        time.sleep(0.2)
    assert visible(w, WEB, album) == set(album)
