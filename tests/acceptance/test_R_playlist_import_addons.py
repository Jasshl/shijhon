"""Imports and the catalogs of add-ons, against a real Navidrome: an entry that neither
the library nor the catalog has is found in the catalog of an enabled add-on, published
as that add-on's release, and played from that add-on by its own track ID; an add-on
switched on later completes a published import, without a restart; and a reviewed import
is published after the add-on's catalog forgot its songs (as after a restart).

The catalog is the made-up one; the add-on is the fake one with the harness's invented
catalog, next to an add-on without one."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.catalog.base import SearchResults
from shijhon.delivery.addon import catalog_key, tagged
from shijhon.delivery.netpolicy import Reach
from tests.acceptance.test_R_playlist_imports import submit
from tests.acceptance.test_R_playlists import SONGS, connected_admin, wait_state
from tests.conftest import NavidromeFactory
from tests.harness.addon_catalog import FakeCatalog
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay

NAME = "Fake"
KEY = catalog_key(NAME)
SERVICE = "test.fake.fake"  # the fake add-on's manifest ID
FOUND = f"Found in the catalog of add-on {NAME}"


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, Browser]]:
    nd = navidrome_factory()
    write_album(nd.music, Album("Glass Orchard", "Paper Garden", (Track("First Light", 1),)))
    nd.scan()
    catalog = Replay().catalog()

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(songs=tuple(s for s in SONGS if s.title in term))

    catalog.search = search  # type: ignore[method-assign]
    with delivery_world(nd, tmp_path_factory.mktemp("import-addons"), catalog=catalog) as w:
        held = FakeCatalog("plain", prefix="lib:")
        fake = w.addon(NAME, catalog=held)
        # Its covers are at its own address, on this machine: never fetched from there.
        held.images = f"{fake.base_url}/img"
        w.addon("Plain")  # audio only: no catalog
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        assert (
            browser.submit(
                "playlists", {"password": ADMIN_PASSWORD, "backend": "none"}, "playlists/connection"
            ).status_code
            == 303
        )
        yield w, browser
        browser.close()


def addons(w: DeliveryWorld) -> tuple[FakeAddon, FakeAddon]:
    return w.addons[0], w.addons[1]


def switch(w: DeliveryWorld, enabled: bool) -> None:
    """The add-on with the catalog on or off, as on the Add-ons page (both are added the
    first time, the one with the catalog first)."""

    async def change() -> None:
        sources = w.services.sources
        stored = {s.name: s for s in await sources.stored()}
        for addon in addons(w):
            if addon.name not in stored:
                await sources.add(addon.name, addon.base_url, reach=Reach.LOOPBACK)
        stored = {s.name: s for s in await sources.stored()}
        await sources.set_enabled(stored[NAME].id, enabled)

    w.server.call(change)


def own(item: str) -> str:
    """The catalog reference of the add-on's item ``item`` ("lib:" before it)."""
    return f"{KEY}:{tagged(SERVICE, 'lib:' + item)}"


def playlist(w: DeliveryWorld, ident: str) -> dict[str, Any]:
    service = w.services.playlists
    assert service

    async def read() -> dict[str, Any]:
        connection = await connected_admin(service)
        mix = await service.repo.mix(ident, connection.user_id)
        return await service.client.playlist(connection, mix["playlist_id"])

    return w.server.call(read)


def publish(world: tuple[DeliveryWorld, Browser], ident: str, run: str, **form: str) -> None:
    w, browser = world
    response = browser.submit(f"playlists/import/{ident}", {"action": "publish", **form})
    assert response.status_code == 303, response.text
    result = wait_state(w, run, ("published", "failed", "uncertain"))
    assert result["state"] == "published", result


def test_an_entry_only_an_add_on_s_catalog_has_is_published_and_plays_from_it(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    fake, plain = addons(w)
    switch(w, True)
    audio = w.audio("import-addon")
    fake.add(FakeTrack(isrc="ZZSHA0001202", audio=audio, track_id="lib:t1202"))
    before = w.placeholder_rows()
    plain.clear()
    ident, run = submit(world, lines="Glass Orchard — First Light\nSelma Roe — Counting Gulls")
    data = w.server.call(lambda: service.repo.run(run))["data"]
    first, found = data["rows"]
    assert first["state"] == "matched" and not first["track"]["id"].startswith("sh.")
    assert found["state"] == "matched" and found["note"] == FOUND
    assert found["track"]["id"].startswith(f"sh.tr.{KEY}.")
    assert found["track"]["release"] == own("al12")
    assert [(r["ref"], r["tracks"]) for r in data["releases"]] == [(own("al12"), 3)]
    assert FOUND in browser.get(f"playlists/import/{ident}").text
    assert w.placeholder_rows() == before  # a review adds nothing
    assert [r for r in plain.requests() if r["endpoint"] == "search"] == []

    publish(world, ident, run)
    saved = playlist(w, ident)
    assert [s["title"] for s in saved["entry"]] == ["First Light", "Counting Gulls"]
    added = saved["entry"][1]
    assert not added["id"].startswith("sh.") and added["album"] == "Harbor Arithmetic"
    assert w.placeholder_rows() == before + 3  # the whole release
    # Its audio comes from the add-on, asked for by its own track ID, without a lookup.
    fake.clear()
    client = w.client()
    try:
        played = client.request("stream", {"id": added["id"]})
    finally:
        client.close()
    assert played.status_code == 200 and played.content == audio.read_bytes()
    assert [r["key"] for r in fake.requests("stream")] == ["lib:t1202"]
    assert fake.requests("resolve-isrc") == []


def test_a_reviewed_import_is_published_after_the_add_on_s_catalog_forgot_its_songs(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    """The add-on's search names the song's album by its title only (its album is looked
    up); between review and publication everything its catalog was shown is forgotten,
    as after a restart: publication asks for the reviewed release again."""
    w, _ = world
    service = w.services.playlists
    assert service and service.addons
    fake, _ = addons(w)
    switch(w, True)
    ident, run = submit(world, lines="Ines Calder — Kite Season")
    (row,) = w.server.call(lambda: service.repo.run(run))["data"]["rows"]
    assert row["state"] == "matched" and row["note"] == FOUND
    assert row["track"]["release"] == own("al5")
    w.server.call(service.addons.aclose)
    fake.clear()
    publish(world, ident, run)
    assert sorted({r["key"] for r in fake.requests("album")}) == ["lib:al5"]
    (entry,) = playlist(w, ident)["entry"]
    assert entry["title"] == "Kite Season" and not entry["id"].startswith("sh.")
    album = w.nd.client().ok("getAlbum", {"id": entry["albumId"]})["album"]
    assert [s["title"] for s in album["song"]] == ["Paper Lanterns", "Kite Season", "Wax and Wire"]


def test_an_add_on_switched_on_later_completes_a_published_import(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    """Without the add-on the entry stays missing; with it switched on - no restart -
    "Recheck missing entries" finds it, and the same playlist is completed."""
    w, browser = world
    service = w.services.playlists
    assert service
    switch(w, False)
    ident, run = submit(world, lines="Glass Orchard — First Light\nOdile Brandt — Ash and Elm")
    rows = w.server.call(lambda: service.repo.run(run))["data"]["rows"]
    assert [r["state"] for r in rows] == ["matched", "unresolved"]
    publish(world, ident, run, excluded="1")
    before = playlist(w, ident)
    assert [s["title"] for s in before["entry"]] == ["First Light"]
    switch(w, True)
    response = browser.submit(f"playlists/import/{ident}", {"action": "recheck"})
    assert response.status_code == 303, response.text
    next_run = w.server.call(lambda: service.repo.runs(ident))[0]["id"]
    preview = wait_state(w, next_run, ("preview", "failed"))
    assert preview["state"] == "preview", preview
    assert [r["state"] for r in preview["data"]["rows"]] == ["matched", "matched"]
    assert preview["data"]["rows"][1]["note"] == FOUND
    publish(world, ident, next_run)
    after = playlist(w, ident)
    assert after["id"] == before["id"]
    assert [s["title"] for s in after["entry"]] == ["First Light", "Ash and Elm"]
    assert after["entry"][0]["id"] == before["entry"][0]["id"]
