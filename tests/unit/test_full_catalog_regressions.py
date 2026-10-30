"""Regression checks for the full-catalog extension."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from shijhon.catalog.addon import AddonCatalog
from shijhon.catalog.base import (
    CatalogError,
    PartialReleases,
    SearchResults,
    client_artwork_template,
)
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CatalogArtist, CatalogRef
from shijhon.dashboard.app import Dashboard
from shijhon.delivery.addon import AddonError
from shijhon.views.additions import CatalogAdditions
from tests.unit.test_addon_catalog import NAME
from tests.unit.test_full_catalog import FullWorld

pytest_plugins = ["tests.unit.test_full_catalog"]

pytestmark = pytest.mark.anyio


async def test_unsubmitted_identity_field_keeps_saved_value() -> None:
    dashboard = Dashboard.__new__(Dashboard)
    dashboard.host = SimpleNamespace(locked={})
    dashboard._addon_identity = AsyncMock(return_value="examplemusic.us")  # type: ignore[method-assign]
    info, form = await dashboard._catalog_identity_form(
        SimpleNamespace(addon="Full", identity=""), None
    )
    assert form is None
    assert info["identity_confirm"] == "examplemusic.us"
    assert "save to use its catalog IDs" in info["identity_notice"]


async def test_stale_identity_is_refreshed_once_before_cached_requests(full: FullWorld) -> None:
    cached = CachedCatalog(full.catalog, ttl=3600)
    await cached.search("Mara Venn")
    source = full.world.registry.sources[0]
    source.addon.manifest_read_at -= 299
    full.declaration = {"key": "anothercatalog"}
    await cached.search("Mara Venn")
    assert sum(r.url.path.endswith("manifest.json") for r in full.requests) == 1
    source.addon.manifest_read_at -= 2

    async def ask() -> None:
        with pytest.raises(CatalogError, match="catalog identity changed"):
            await cached.search("Mara Venn")

    async with anyio.create_task_group() as tasks:
        for _ in range(10):
            tasks.start_soon(ask)
    assert sum(r.url.path.endswith("manifest.json") for r in full.requests) == 2
    assert full.catalog.key == "examplemusic"


async def test_failed_manifest_refresh_preserves_known_manifest(full: FullWorld) -> None:
    cached = CachedCatalog(full.catalog, ttl=3600)
    found = await cached.search("Mara Venn")
    addon = full.world.registry.sources[0].addon
    addon.manifest_read_at -= 301
    addon._read_manifest = AsyncMock(side_effect=AddonError("unavailable", "HTTP 503"))  # type: ignore[method-assign]
    assert await cached.search("Mara Venn") == found
    assert await cached.search("Mara Venn") == found
    assert addon._read_manifest.await_count == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["song", "album", "artist", "artists_of"])
async def test_wrong_item_forces_identity_refresh(full: FullWorld, kind: str) -> None:
    await full.catalog.search("Mara Venn")
    full.declaration = {"key": "changedcatalog"}
    if kind in ("song", "artists_of"):
        full.held.answers["catalog/track/t101"] = full.held.track("t102")
    else:
        data = getattr(full.held, kind)("al1" if kind == "album" else "ar1")
        assert data is not None
        data["id"] = "anotheritem"
        full.held.answers[f"{kind}/{'al1' if kind == 'album' else 'ar1'}"] = data
    with pytest.raises(CatalogError, match="catalog identity changed"):
        if kind == "artists_of":
            await full.catalog.artists_of(("t101",), ())
        else:
            await getattr(full.catalog, kind)(
                {"song": "t101", "album": "al1", "artist": "ar1"}[kind]
            )
    assert sum(r.url.path.endswith("manifest.json") for r in full.requests) == 2


async def test_artwork_hosts_are_coalesced_and_cached(full: FullWorld) -> None:
    resolve = AsyncMock(return_value=["93.184.216.34"])
    full.catalog.resolver = resolve

    async def check(i: int) -> None:
        assert await client_artwork_template(full.catalog, f"https://images.fake.test/{i}.jpg")

    async with anyio.create_task_group() as tasks:
        for i in range(100):
            tasks.start_soon(check, i)
    await check(101)
    assert resolve.await_count == 1


async def test_search_checks_only_shown_non_library_artists(full: FullWorld) -> None:
    resolve = AsyncMock(return_value=["93.184.216.34"])
    full.catalog.resolver = resolve
    library = SimpleNamespace(
        entries=AsyncMock(return_value={"native": {"id": "native", "name": "Native"}})
    )
    additions = CatalogAdditions(full.catalog, None, None, library_artists=library)  # type: ignore[arg-type]
    artists = tuple(
        CatalogArtist(
            CatalogRef("examplemusic", f"a{i}"),
            "Native" if i == 0 else f"Person {i}",
            f"https://host{i}.fake.test/a.jpg",
        )
        for i in range(100)
    )
    shown = await additions._search_additions(
        "person", {"artist": 3}, {}, SearchResults(artists=artists)
    )
    assert len(shown["artist"]) == 3
    assert resolve.await_count == 2
    await additions._search_additions("person", {"artist": 3}, {}, SearchResults(artists=artists))
    assert resolve.await_count == 2


def pages(full: FullWorld) -> None:
    data = full.held.artist("ar1")
    assert data is not None
    full.held.answers["artist/ar1"] = data | {"releasesNext": "second"}
    full.held.pages["second"] = {
        "albums": [{"id": "extra1", "title": "Later Rooms"}],
        "next": "third",
    }
    full.held.pages["third"] = {"albums": [{"id": "extra2", "title": "Last Rooms"}]}


async def test_mid_paging_failure_resumes_without_refetching_pages(
    full: FullWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages(full)
    addon = full.world.registry.sources[0].addon
    original = addon.catalog_releases
    calls: list[str] = []

    async def read(own: str, cursor: str) -> Any:
        calls.append(cursor)
        if cursor == "third" and calls.count("third") == 1:
            raise AddonError("broken", "invented page failure")
        return await original(own, cursor)

    addon.catalog_releases = read  # type: ignore[method-assign]
    cached = CachedCatalog(full.catalog, ttl=3600)
    # Exercise the actual partial-cache expiry without a minute-long test.
    monkeypatch.setattr(PartialReleases, "retry_after", 0.03)
    assert len(await cached.artist_releases("ar1")) == 3
    assert len(await cached.artist_releases("ar1")) == 3
    await anyio.sleep(0.05)
    assert len(await cached.artist_releases("ar1")) == 4
    assert calls == ["second", "third", "third"]
    assert sum(r.url.path.endswith("/artist/ar1") for r in full.requests) == 1


async def test_release_deadline_returns_partial_and_resumes(full: FullWorld, caplog: Any) -> None:
    pages(full)
    addon = full.world.registry.sources[0].addon
    original = addon.catalog_releases
    held = anyio.Event()
    done = anyio.Event()
    calls: list[str] = []

    async def read(own: str, cursor: str) -> Any:
        calls.append(cursor)
        if cursor == "third":
            await held.wait()
        answer = await original(own, cursor)
        if cursor == "third":
            done.set()
        return answer

    addon.catalog_releases = read  # type: ignore[method-assign]
    full.catalog.timeout = 0.08
    partial = await full.catalog.artist_releases("ar1")
    assert len(partial) == 3 and isinstance(partial, PartialReleases)
    assert partial.retry_after == 60
    assert "incomplete" in caplog.text
    held.set()
    # async_lru may cancel an unobserved page; either resuming or finishing it is valid.
    with anyio.move_on_after(0.03):
        await done.wait()
    assert len(await full.catalog.artist_releases("ar1")) == 4
    assert calls.count("second") == 1


async def test_first_full_artist_page_keeps_five_hundred_releases(full: FullWorld) -> None:
    data = full.held.artist("ar1")
    assert data is not None
    full.held.answers["artist/ar1"] = data | {
        "albums": [{"id": f"a{i}", "title": "Invented Release"} for i in range(600)]
    }
    assert len(await full.catalog.artist_releases("ar1")) == 500


async def test_disabled_legacy_addon_does_no_identity_requests(full: FullWorld) -> None:
    legacy = AddonCatalog(NAME, full.world.registry, full.catalog.http)  # type: ignore[arg-type]
    full.world.registry.sources.clear()
    assert await legacy.songs_by_isrc("ZZSHA0000101") == ()
    assert await legacy.artists_of(("track",), ("album",)) == {}
    assert await legacy.album_of("missing") is None
    assert not full.requests


async def test_remembered_album_restores_disc_with_track_number(full: FullWorld) -> None:
    album = await full.catalog.album("al1")
    data = full.held.track("t101")
    assert data is not None
    data["album"] = album.title
    data.pop("discNumber", None)
    data.pop("trackNumber", None)
    full.held.answers["search"] = {"tracks": [data]}
    song = (await full.catalog.search("invented")).songs[0]
    assert (song.disc, song.number) == (2, 1)


async def test_partial_discography_is_not_saved_as_complete(full: FullWorld) -> None:
    pages(full)
    addon = full.world.registry.sources[0].addon
    original = addon.catalog_releases

    async def read(own: str, cursor: str) -> Any:
        if cursor == "third":
            raise AddonError("broken", "invented page failure")
        return await original(own, cursor)

    addon.catalog_releases = read  # type: ignore[method-assign]
    saved = SimpleNamespace(get=AsyncMock(return_value=None), save=AsyncMock())
    cached = CachedCatalog(full.catalog, ttl=3600)
    additions = CatalogAdditions(cached, None, None, discographies=saved)  # type: ignore[arg-type]
    found = await additions._discography_for(
        "artist:ar1", lambda: cached.artist_releases("ar1"), ("invented", "test")
    )
    assert isinstance(found, PartialReleases) and len(found) == 3
    saved.save.assert_not_awaited()


async def test_saved_discography_obeys_the_current_identity(full: FullWorld) -> None:
    cached = CachedCatalog(full.catalog, ttl=3600)
    releases = await cached.artist_releases("ar1")
    saved = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(releases=releases, fresh=True)), save=AsyncMock()
    )
    additions = CatalogAdditions(cached, None, None, discographies=saved)  # type: ignore[arg-type]
    full.declaration = {"key": "changedcatalog"}
    full.world.registry.sources[0].addon.manifest_read_at -= 301
    fetch = AsyncMock(return_value=releases)
    assert await additions._discography_for("artist:ar1", fetch, ("invented", "test")) is None
    fetch.assert_not_awaited()


@pytest.mark.parametrize("scenario", ["wrong-full", "best-partial", "both-zero"])
async def test_same_name_discography_preserves_partial_evidence(
    full: FullWorld, scenario: str
) -> None:
    full.held.answers["search"] = {
        "artists": [{"id": own, "name": "Mara Venn"} for own in ("ar1", "ar2")]
    }
    for own in ("ar1", "ar2"):
        matches = (scenario == "wrong-full" and own == "ar2") or (
            scenario == "best-partial" and own == "ar1"
        )
        full.held.answers[f"artist/{own}"] = {
            "id": own,
            "name": "Mara Venn",
            "albums": [{"id": f"{own}release", "title": "Glass Rivers" if matches else "Other"}],
            "releasesNext": f"{own}-later" if own == "ar1" or scenario == "both-zero" else None,
        }
    pending: list[str] = []

    async def unread(own: str, cursor: str, service: str) -> Any:
        pending.append(own)
        # The matching title may be on this unread page of the right artist.
        await anyio.sleep_forever()

    full.catalog._release_page = unread  # type: ignore[method-assign]
    full.catalog.timeout = 0.03
    saved = SimpleNamespace(get=AsyncMock(return_value=None), save=AsyncMock())
    cached = CachedCatalog(full.catalog, ttl=3600)
    additions = CatalogAdditions(cached, None, None, discographies=saved)  # type: ignore[arg-type]
    found = await additions._discography_for(
        "library:artist",
        lambda: additions._discography(cached, "Mara Venn", [{"name": "Glass Rivers"}]),
        ("invented", "test"),
    )
    assert isinstance(found, PartialReleases)
    assert [r.ref.id for r in found] == (
        []
        if scenario == "both-zero"
        else ["ar2release" if scenario == "wrong-full" else "ar1release"]
    )
    assert pending == (["ar1", "ar2"] if scenario == "both-zero" else ["ar1"])
    saved.save.assert_not_awaited()


@pytest.mark.parametrize("what", ["discography", "top songs"])
@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize("outage", ["unavailable", "cooling", "deadline"])
async def test_saved_lists_survive_an_unknown_identity_after_restart(
    full: FullWorld, what: str, fresh: bool, outage: str
) -> None:
    items = (
        await full.catalog.artist_releases("ar1")
        if what == "discography"
        else await full.catalog.top_songs("ar1")
    )
    addon = full.world.registry.sources[0].addon
    addon._manifest = None
    if outage == "cooling":
        full.world.registry.cooled.add(1)
    elif outage == "deadline":
        full.world.server.wait = anyio.Event()
    else:
        addon._read_manifest = AsyncMock(side_effect=AddonError("unavailable", "HTTP 503"))  # type: ignore[method-assign]
    restarted = AddonCatalog(
        NAME, full.world.registry, full.catalog.http, identity="examplemusic.us"
    )  # type: ignore[arg-type]
    additions = CatalogAdditions(
        CachedCatalog(restarted, ttl=3600), None, None, budget_seconds=0.03
    )  # type: ignore[arg-type]
    fetch, save = AsyncMock(), AsyncMock()
    found, _ = await additions._saved_for(
        "artist:ar1", fetch, (items, fresh), save, lambda: False, what
    )
    assert found == items
    fetch.assert_not_awaited()
    save.assert_not_awaited()
