"""The optional full-catalog extension: metadata, identity, requests and their bounds."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from shijhon.catalog import contract
from shijhon.catalog.addon import (
    IDENTITY_CHANGED,
    AddonCatalog,
    AddonCatalogs,
    Settings,
    Space,
    read_album,
    read_release,
    read_track,
)
from shijhon.catalog.base import CatalogError, client_artwork_template, scope
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CatalogRef, ReleaseKind, artwork_url, item_artist
from shijhon.delivery.addon import FullCatalog as Declaration
from shijhon.delivery.addon import Manifest, catalog_key, service_tag
from shijhon.delivery.pacing import Limits
from shijhon.delivery.playback import PlaybackSettings, Track, _own_id
from tests.harness.full_catalog import DECLARATION, RESOURCES, FullCatalog
from tests.unit.test_addon_catalog import NAME, SERVICE, World, world

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "artists,names,ids",
    [
        (None, (), ()),
        ([{"id": "ar1"}, {"id": "ar2"}], (), ("ar1", "ar2")),
        ([{"id": "ar1", "name": "Mara Venn"}], ("Mara Venn",), ("ar1",)),
        ([{"name": "Mara Venn"}], ("Mara Venn",), ()),
        ([{"id": "ar1"}, {"id": "ar2", "name": "Ivo Reed"}], ("", "Ivo Reed"), ("ar1", "ar2")),
        ([{}, {"id": None}], ("Mara Venn", "Ivo Reed"), ()),
    ],
)
def test_artist_credit_names_and_references_keep_their_positions(
    full: bool, direct: bool, artists: Any, names: tuple[str, ...], ids: tuple[str, ...]
) -> None:
    space = Space("examplemusic", "invented", full=full, direct=direct)
    data = {
        "id": "t1",
        "title": "Glass Rivers",
        "durationMs": 4125,
        "artist": "Mara Venn feat. Ivo Reed",
    }
    if artists is not None:
        data["artists"] = artists
    for item in (read_track(space, data), read_release(space, data)):
        assert item is not None and item.artist == data["artist"]
        assert item.artists == (names if full else ())
        if full and ids:
            assert item.artist_refs == tuple(space.ref(ident, "i") for ident in ids)
        elif direct and full:
            assert item.artist_refs == ()
        else:
            assert all(ref.id.startswith("n") for ref in item.artist_refs)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("verdict", ["absent", True, False, None, 0, 1, "false"])
@pytest.mark.parametrize("shape", ["complete", "count", "skipped", "empty", "bounded"])
def test_explicit_album_completeness_is_optional_and_full_catalog_only(
    full: bool, verdict: Any, shape: str
) -> None:
    track = {"id": "t1", "title": "Glass Rivers", "durationMs": 4125}
    tracks = [track]
    if shape == "skipped":
        tracks.append({"id": "t2", "title": "Unreadable"})
    elif shape == "empty":
        tracks = []
    elif shape == "bounded":
        tracks = [dict(track, id=f"t{i}") for i in range(501)]
    count = 2 if shape == "count" else len(tracks)
    data = {"id": "al1", "title": "Glass Rivers", "tracks": tracks, "trackCount": count}
    if verdict != "absent":
        data["incomplete"] = verdict
    album = read_album(Space("examplemusic", "invented", full=full, direct=True), "al1", data)
    assert album.track_count == count
    if shape in {"skipped", "empty", "bounded"}:
        assert album.incomplete
    elif full and type(verdict) is bool:
        assert album.incomplete is verdict
    else:
        assert album.incomplete is (shape == "count")


@pytest.mark.parametrize(
    "declared,title,expected",
    [
        ("album", "Evening - Single", ReleaseKind.SINGLE),
        ("album", "Evening - EP", ReleaseKind.EP),
        ("album", "Evening", ReleaseKind.ALBUM),
        ("single", "Evening - EP", ReleaseKind.SINGLE),
        ("ep", "Evening - Single", ReleaseKind.EP),
        ("compilation", "Evening - EP", ReleaseKind.COMPILATION),
        (None, "Evening - Single", ReleaseKind.SINGLE),
        ("unknown", "Evening - EP", ReleaseKind.EP),
        ("album", "Evening - ep  ", ReleaseKind.EP),
        ("album", "Evening EP", ReleaseKind.ALBUM),
    ],
)
def test_album_kind_keeps_more_specific_title_kind(
    declared: str | None, title: str, expected: ReleaseKind
) -> None:
    release = read_release(
        Space("examplemusic", "invented", full=True, direct=True),
        {"id": "release1", "title": title, "kind": declared},
    )
    assert release is not None and release.kind == expected


@dataclass
class FullWorld:
    world: World
    held: FullCatalog
    declaration: dict[str, Any] | None = field(default_factory=lambda: dict(DECLARATION))
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def catalog(self) -> AddonCatalog:
        return self.world.catalog


@pytest.fixture
async def full() -> AsyncIterator[FullWorld]:
    w = world(resources=RESOURCES, limits=Limits(10000, 1000, 0))
    f = FullWorld(w, FullCatalog())
    w.server.held = f.held
    addon = w.registry.sources[0].addon
    ordinary = w.server.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        f.requests.append(request)
        path = request.url.path.removeprefix("/cfg/")
        if path == "manifest.json":
            response = await ordinary(request)
            data = response.json()
            if f.declaration is not None:
                data["fullCatalog"] = f.declaration
            return httpx.Response(200, json=data)
        if path.startswith("catalog/"):
            w.server.asked.append(path)
            if path in w.server.status:
                return w.server.status[path]
            if path in f.held.answers:
                return httpx.Response(200, json=f.held.answers[path])
            parts = path.split("/")
            if parts[1] == "track":
                answer = f.held.track(parts[2])
            elif parts[1] == "isrc":
                answer = f.held.isrc(parts[2])
            else:
                answer = f.held.releases(parts[2], request.url.params.get("cursor", ""))
            return httpx.Response(404) if answer is None else httpx.Response(200, json=answer)
        return await ordinary(request)

    await addon.http.aclose()
    addon.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    w.catalog = AddonCatalog(
        NAME, w.registry, w.catalog.http, identity="examplemusic.us", resolver=w.catalog.resolver
    )  # type: ignore[arg-type]
    yield f
    await addon.http.aclose()
    await w.catalog.aclose()


async def test_full_catalog_passes_the_adapter_contract(full: FullWorld) -> None:
    await contract.check_catalog(full.catalog, contract.Sample(search="Mara Venn"))
    assert scope(full.catalog) == "examplemusic.us"


async def test_rich_fields_on_search_album_and_artist_answers(full: FullWorld) -> None:
    result = await full.catalog.search("Mara Venn", 3)
    song = result.songs[0]
    assert song.ref == CatalogRef("examplemusic", "t101")
    assert song.duration_ms == 4125 and (song.disc, song.number) == (2, 1)
    assert song.album == result.albums[0].ref and song.album_title == "Glass Rivers"
    assert song.artists == ("Mara Venn",) and song.artist_refs == (result.artists[0].ref,)
    assert song.explicit and not song.clean and song.genres == ("Invented",)
    assert song.release_date == "2019-03-08"
    album = await full.catalog.album("al1")
    assert album.numbered and not album.incomplete
    assert [(t.disc, t.number) for t in album.tracks] == [(2, i) for i in range(1, 5)]
    assert album.label == "Imaginary Records" and album.upc == "000000000001"
    assert album.artist_refs == song.artist_refs and album.explicit
    assert album.genres == ("Invented",) and album.release_date == "2019-03-08"
    releases = await full.catalog.artist_releases("ar1")
    assert releases[1].kind == ReleaseKind.EP  # declared kind beats the title suffix
    assert (await full.catalog.top_songs("ar1", 1))[0].album is not None
    assert full.requests[1].url.params["limit"] == "3"


async def test_cold_song_isrc_album_and_artist_lookups(full: FullWorld) -> None:
    song = await full.catalog.song("t102")  # no preceding search or album
    assert song.album == CatalogRef("examplemusic", "al1")
    assert await full.catalog.album_of("t102") == song.album
    assert (await full.catalog.songs_by_isrc("zzsha0000102")) == (song,)
    assert await full.catalog.songs_by_isrc("invalid") == ()
    assert await full.catalog.artists_of(("t102",), ("al1",)) == {
        "songs:t102": song.artist_refs,
        "albums:al1": song.artist_refs,
    }
    assert await full.catalog.artists_of((), ()) == {}
    full.world.server.status["catalog/isrc/ZZSHA0000102"] = httpx.Response(404)
    assert await full.catalog.songs_by_isrc("ZZSHA0000102") == ()


async def test_unconfirmed_and_import_catalogs_keep_name_keys(full: FullWorld) -> None:
    c = AddonCatalog(NAME, full.world.registry, full.catalog.http)  # type: ignore[arg-type]
    found = await c.search("Mara Venn")
    assert c.key == catalog_key(NAME) and c.region == ""
    assert found.songs[0].ref.id == service_tag(SERVICE) + "t101"
    assert found.songs[0].explicit and (await c.song(found.songs[0].ref.id)).album is not None
    assert (await c.songs_by_isrc("ZZSHA0000102"))[0].album is not None
    imports = AddonCatalogs(full.world.registry, skip=NAME)  # type: ignore[arg-type]
    assert await imports.catalogs() == []
    assert await imports.by_key(catalog_key(NAME)) is None
    imports.skip = None
    [(name, imported)] = await imports.catalogs()
    assert name == NAME and imported.key == catalog_key(NAME) and imported.region == ""
    await imports.aclose()


@pytest.mark.parametrize(
    "changed", [None, {}, {"key": "othermusic"}, {"key": "examplemusic", "region": "gb"}]
)
async def test_changed_identity_refuses_cached_and_fresh_answers(
    full: FullWorld, changed: Any
) -> None:
    cached = CachedCatalog(full.catalog, ttl=60)
    result = await cached.search("Mara Venn")
    await cached.album("al1")
    await cached.song("t101")
    await cached.artist("ar1")
    await cached.artist_releases("ar1")
    await cached.top_songs("ar1")
    full.declaration = changed
    if changed is None:
        full.world.server.resources = tuple(r for r in RESOURCES if r != "fullcatalog")
    await full.world.registry.sources[0].addon.reread()
    for ask in (
        lambda: cached.search("Mara Venn"),
        lambda: cached.album("al1"),
        lambda: cached.song("t101"),
        lambda: cached.artist("ar1"),
        lambda: cached.artist_releases("ar1"),
        lambda: cached.top_songs("ar1"),
        lambda: cached.songs_by_isrc("ZZSHA0000101"),
        lambda: full.catalog.check(),
        lambda: full.catalog.album_of("t101"),
    ):
        with pytest.raises(CatalogError) as failed:
            await ask()
        assert failed.value.kind == "invalid" and failed.value.reason == IDENTITY_CHANGED
    assert await client_artwork_template(cached, result.artists[0].artwork_template) is None


@pytest.mark.parametrize(
    "identity",
    ["demo", "none", "addonx", "Upper", "x-us", "x.", "x.US", "x.a.b", "a" * 33, "x." + "r" * 17],
)
async def test_invalid_confirmed_identities_are_rejected(identity: str) -> None:
    with pytest.raises(ValidationError):
        Settings(identity=identity)


async def test_manifest_declaration_is_optional_and_validated() -> None:
    assert Manifest.parse({"fullCatalog": DECLARATION}).full_catalog is None
    for raw in (
        None,
        [],
        "x",
        {"key": "addonx"},
        {"key": "demo"},
        {"key": "x", "region": "/private"},
    ):
        parsed = Declaration.parse(raw)
        assert not parsed.identity
    assert Declaration.parse({"key": "examplemusic"}).identity == "examplemusic"
    assert not Declaration.parse({"clientArtwork": "true"}).client_artwork
    assert Settings(identity="examplemusic").identity == "examplemusic"


async def test_bad_ids_are_skipped_without_escaping_and_album_is_incomplete(
    full: FullWorld,
) -> None:
    data = full.held.album("al1")
    assert data is not None
    data["tracks"][1]["id"] = "bad/id"
    data["tracks"][2]["artists"] = [{"id": "bad/id", "name": "Mara Venn"}]
    full.held.answers["album/al1"] = data
    album = await full.catalog.album("al1")
    assert album.incomplete and len(album.tracks) == 3
    assert all(t.ref.id != "bad.2Fid" for t in album.tracks)
    full.held.answers["catalog/track/t101"] = full.held.track("t102")
    with pytest.raises(CatalogError, match="another song"):
        await full.catalog.song("t101")


async def test_numbers_fall_back_as_a_whole_and_fields_require_declaration(full: FullWorld) -> None:
    data = full.held.album("al1")
    assert data is not None
    data["tracks"][1]["discNumber"] = True
    rich = read_album(Space("examplemusic", SERVICE, True, True), "al1", data)
    assert not rich.numbered and [(t.disc, t.number) for t in rich.tracks] == [
        (1, i) for i in range(1, 5)
    ]
    old = read_album(Space(catalog_key(NAME), SERVICE), "al1", data)
    assert not old.numbered and not old.explicit and old.label is None and not old.genres
    assert old.tracks[0].duration_ms == 4125  # durationMs was already supported
    assert not old.tracks[0].explicit and not old.tracks[0].artists
    assert old.tracks[0].album_title == "Glass Rivers"


async def test_artist_ids_beat_name_lookup_and_missing_ids_use_names(full: FullWorld) -> None:
    refs = await full.catalog.artists_of(("t101",), ())
    assert refs["songs:t101"] == (CatalogRef("examplemusic", "ar1"),)
    assert not any(r.url.path.endswith("/search") for r in full.requests)
    data = full.held.track("t101")
    assert data is not None
    data["artists"] = [{"name": "Mara Venn"}]
    full.held.answers["catalog/track/t101"] = data
    cached = CachedCatalog(full.catalog, ttl=60)
    artist = await cached.artist(item_artist(CatalogRef("examplemusic", "t101"), "t", 0).id)
    assert artist.ref.id == "ar1"


async def test_release_pages_preserve_order_deduplicate_and_encode_cursor(full: FullWorld) -> None:
    data = full.held.artist("ar1")
    assert data is not None
    cursor = "opaque +/?&=cursor"
    data["releasesNext"] = cursor
    full.held.answers["artist/ar1"] = data
    full.held.pages[cursor] = {
        "albums": [data["albums"][0], {"id": "extra", "title": "Later Rooms", "kind": "single"}],
        "next": None,
    }
    releases = await full.catalog.artist_releases("ar1")
    assert [r.ref.id for r in releases] == ["al1", "al2", "extra"]
    assert releases[-1].artist == "Mara Venn" and releases[-1].kind == ReleaseKind.SINGLE
    assert full.requests[-1].url.params["cursor"] == cursor
    assert full.world.pace is not None and full.world.pace.sent == len(full.requests)


@pytest.mark.parametrize("cursor", ["x" * 513, 1, [], {}, "\ud800"])
async def test_invalid_cursors_end_the_list_without_echoing_them(
    full: FullWorld, cursor: Any, caplog: Any
) -> None:
    data = full.held.artist("ar1")
    assert data is not None
    full.held.answers["artist/ar1"] = json.dumps(data | {"releasesNext": cursor}).encode()
    assert len(await full.catalog.artist_releases("ar1")) == 2
    assert (await full.catalog.artist("ar1")).name == "Mara Venn"
    assert await full.catalog.top_songs("ar1")
    assert "invalid cursor" in caplog.text


@pytest.mark.parametrize("mode", ["repeat", "pages", "releases"])
async def test_release_paging_bounds_cut_and_log(full: FullWorld, mode: str, caplog: Any) -> None:
    data = full.held.artist("ar1")
    assert data is not None
    data["albums"] = (
        [{"id": f"first{i}", "title": "Invented Release"} for i in range(100)]
        if mode == "releases"
        else []
    )
    data["releasesNext"] = "0"
    full.held.answers["artist/ar1"] = data
    for i in range(60):
        full.held.pages[str(i)] = {
            "albums": [{"id": f"r{i}.{j}", "title": "Invented Release"} for j in range(100)]
            if mode == "releases"
            else [],
            "next": "0" if mode == "repeat" else str(i + 1),
        }
    releases = await full.catalog.artist_releases("ar1")
    assert len([r for r in full.requests if "/releases" in r.url.path]) == (
        1 if mode == "repeat" else 49
    )
    assert len(releases) == (5000 if mode == "releases" else 0)
    assert "paging bound" in caplog.text


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "unavailable"),
    ],
)
async def test_new_request_errors_use_catalog_kinds(
    full: FullWorld, status: int, kind: str
) -> None:
    full.world.server.status["catalog/track/t101"] = httpx.Response(
        status, headers={"retry-after": "40"}
    )
    with pytest.raises(CatalogError) as failed:
        await full.catalog.song("t101")
    assert failed.value.kind == kind and "://" not in str(failed.value)
    if status == 429:
        assert full.world.pace is not None and full.world.pace.blocked > 39


@pytest.mark.parametrize(
    "address,permitted",
    [
        ("https://images.fake.test/{w}x{h}.jpg", True),
        ("http://images.fake.test/{w}x{h}.jpg", False),
        ("https://127.0.0.1/{w}x{h}.jpg", False),
        ("https://10.1.2.3/{w}x{h}.jpg", False),
        ("https://[::ffff:127.0.0.1]/{w}x{h}.jpg", False),
    ],
)
async def test_client_artwork_is_opted_in_per_public_https_template(
    full: FullWorld, address: str, permitted: bool
) -> None:
    data = full.held.album("al1")
    assert data is not None
    data["artworkTemplate"] = address
    full.held.answers["album/al1"] = data
    cached = CachedCatalog(full.catalog, ttl=60)
    template = (await cached.album("al1")).artwork_template
    assert await client_artwork_template(cached, template) == (address if permitted else None)
    if permitted:
        url = artwork_url(template, 300)
        assert url is not None
        await cached.artwork(url)
        assert full.world.images.asked[-1] == address.replace("{w}", "300").replace("{h}", "300")
        with pytest.raises(CatalogError):
            await cached.artwork(url.replace("/300x300", "/other/300x300"))
    full.declaration = {**DECLARATION, "clientArtwork": False}
    await full.world.registry.sources[0].addon.reread()
    assert await client_artwork_template(cached, template) is None


async def test_private_or_unresolved_image_hosts_do_not_reach_clients(full: FullWorld) -> None:
    template = (await full.catalog.artist("ar1")).artwork_template
    full.world.hosts["images.fake.test"] = ["10.1.2.3"]
    assert await client_artwork_template(full.catalog, template) is None
    full.world.hosts["images.fake.test"] = []
    assert await client_artwork_template(full.catalog, template) is None


async def test_empty_cursor_is_opaque_and_is_sent_unchanged(full: FullWorld) -> None:
    data = full.held.artist("ar1")
    assert data is not None
    full.held.answers["artist/ar1"] = data | {"releasesNext": ""}
    full.held.pages[""] = {"albums": [{"id": "al30", "title": "Empty Cursor"}]}
    assert (await full.catalog.artist_releases("ar1"))[-1].ref.id == "al30"
    assert full.requests[-1].url.params["cursor"] == ""


async def test_partial_artist_ids_are_kept_and_only_missing_ones_are_searched(
    full: FullWorld,
) -> None:
    data = full.held.track("t101")
    assert data is not None
    data["artists"] = [
        {"id": "ar77", "name": "A Name Not Searchable"},
        {"name": "Mara Venn"},
    ]
    full.held.answers["catalog/track/t101"] = data
    assert (await full.catalog.artists_of(("t101",), ()))["songs:t101"] == (
        CatalogRef("examplemusic", "ar77"),
        CatalogRef("examplemusic", "ar1"),
    )
    assert [r.url.params["q"] for r in full.requests if r.url.path.endswith("/search")] == [
        "Mara Venn"
    ]


async def test_artwork_templates_survive_the_persistent_index(
    full: FullWorld, tmp_path: Any
) -> None:
    from shijhon.catalog.artwork import ArtworkIndex

    path = tmp_path / "artwork.sqlite"
    first = CachedCatalog(full.catalog, ttl=60, index=ArtworkIndex(path=path))
    album = await first.album("al1")
    await first.index.flush()
    # A fresh catalog has no in-memory answer or artwork template.
    restarted = AddonCatalog(
        NAME,
        full.world.registry,
        full.catalog.http,
        identity="examplemusic.us",
        resolver=full.catalog.resolver,
    )  # type: ignore[arg-type]
    cached = CachedCatalog(restarted, ttl=60, index=ArtworkIndex(path=path))
    template = await cached.index.find("al", album.ref)
    assert template == album.artwork_template
    url = artwork_url(template, 1200)
    assert url is not None
    await cached.artwork(url)
    assert full.world.images.asked[-1].endswith("/1200x1200.jpg")


@pytest.mark.parametrize("bad", [None, [], {}, {"id": "t101", "title": "No Length"}])
async def test_unreadable_single_songs_fail_safely(full: FullWorld, bad: Any) -> None:
    full.held.answers["catalog/track/t101"] = bad
    with pytest.raises(CatalogError) as failed:
        await full.catalog.song("t101")
    assert failed.value.kind == "invalid" and "://" not in str(failed.value)


async def test_declared_catalog_ids_can_be_used_for_audio_without_lookup(full: FullWorld) -> None:
    source = full.world.registry.sources[0]
    track = Track(
        "sh.tr.examplemusic.t101", None, "Glass Rivers", "Mara Venn", 4125, ref="examplemusic:t101"
    )
    settings = PlaybackSettings(catalog_addon=NAME, catalog_identity="examplemusic.us")
    assert await _own_id(source, track, settings) == "t101"  # type: ignore[arg-type]
    assert await _own_id(source, replace(track, ref="othermusic:t101"), settings) is None  # type: ignore[arg-type]
    assert await _own_id(source, replace(track, ref="examplemusic:bad/id"), settings) is None  # type: ignore[arg-type]
    assert all(r.url.path.endswith("/manifest.json") for r in full.requests)


@pytest.mark.parametrize(
    "declared,locked,changed,expected",
    [
        ("examplemusic.us", False, False, "examplemusic.us"),
        ("othermusic.gb", False, False, "examplemusic.us"),
        ("othermusic.gb", True, False, None),
        ("", False, True, "examplemusic.us"),
        (None, False, True, "examplemusic.us"),
    ],
)
async def test_dashboard_identity_review_preserves_locks_and_unreadable_claims(
    declared: str | None, locked: bool, changed: bool, expected: str | None
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shijhon.dashboard.app import Dashboard

    dashboard = Dashboard.__new__(Dashboard)
    dashboard.host = SimpleNamespace(
        locked={"catalog": {"identity": "TEST_CATALOG_IDENTITY"} if locked else {}}
    )
    dashboard._addon_identity = AsyncMock(return_value=declared)  # type: ignore[method-assign]
    applying = SimpleNamespace(addon="Fake", identity="examplemusic.us")
    form = (
        {"addon": "Other", "identity": "examplemusic.us", "identity-addon": "Fake"}
        if changed
        else None
    )
    info, rendered = await dashboard._catalog_identity_form(applying, form)
    if expected is None:
        assert rendered is None
        assert "TEST_CATALOG_IDENTITY" in info["identity_notice"]
        assert "Save this page to confirm" not in info["identity_notice"]
    else:
        assert (rendered or {"identity": applying.identity})["identity"] == expected
    if declared is None:
        assert "could not be confirmed" in info["identity_notice"]
    if declared == "":
        assert "declares no catalog identity" in info["identity_notice"]


async def test_cached_artwork_from_a_previous_adapter_is_refreshed(full: FullWorld) -> None:
    from shijhon.catalog.artwork import ArtworkIndex
    from shijhon.views.ids import CatalogId
    from shijhon.views.virtual import VirtualViews

    index = ArtworkIndex()
    ref = CatalogRef("examplemusic", "al1")
    old = "https://previous.fake.test/al1/{w}x{h}.jpg"
    index.note("al", ref, old)
    cached = CachedCatalog(full.catalog, ttl=60, index=index)
    views = VirtualViews.__new__(VirtualViews)
    views.artwork = index
    data, kind = await views._cover(cached, CatalogId("al", ref), 300, {})
    assert data and kind == "image/jpeg"
    assert index.template("al", ref) != old
    assert full.world.images.asked == ["https://images.fake.test/al1/300x300.jpg"]


async def test_identity_changed_during_song_lookup_cannot_use_a_remembered_song(
    full: FullWorld,
) -> None:
    cached = CachedCatalog(full.catalog, ttl=60)
    await cached.search("Mara Venn")

    async def changed(song_id: str) -> Any:
        full.declaration = {"key": "othermusic"}
        await full.world.registry.sources[0].addon.reread()
        raise CatalogError("invalid", IDENTITY_CHANGED)

    cached._song = changed
    with pytest.raises(CatalogError, match="catalog identity changed"):
        await cached.song("t101")


async def test_isrc_lookup_supplies_the_optional_code_when_tracks_omit_it(full: FullWorld) -> None:
    data = full.held.track("t101")
    assert data is not None
    del data["isrc"]
    full.held.answers["catalog/isrc/ZZSHA0000101"] = {"tracks": [data]}
    [song] = await full.catalog.songs_by_isrc("ZZSHA0000101")
    assert song.isrc == "ZZSHA0000101" and song.album == CatalogRef("examplemusic", "al1")


async def test_clean_compilation_fields_are_kept_on_the_release_and_tracks(full: FullWorld) -> None:
    data = full.held.album("al1")
    assert data is not None
    data.update(kind="compilation", explicit=False, clean=True)
    for track in data["tracks"]:
        track.update(explicit=False, clean=True)
    full.held.answers["album/al1"] = data
    album = await full.catalog.album("al1")
    assert album.kind == ReleaseKind.COMPILATION and album.clean and not album.explicit
    assert all(t.clean and not t.explicit for t in album.tracks)


async def test_confirmed_cached_metadata_remains_usable_while_requests_are_paused(
    full: FullWorld,
) -> None:
    cached = CachedCatalog(full.catalog, ttl=60)
    found = await cached.search("Mara Venn")
    album = await cached.album("al1")
    assert full.world.pace is not None
    full.world.pace.limited(40)
    sent = len(full.requests)
    assert await cached.search("Mara Venn") == found
    assert await cached.album("al1") == album
    assert (await cached.song("t101")).ref.id == "t101"
    assert len(full.requests) == sent


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("missing_ref", [False, True])
def test_unnamed_artist_never_steals_the_next_artists_name(direct: bool, missing_ref: bool) -> None:
    from shijhon.views.entries import album_entry, song_entry
    from shijhon.views.ids import artist_id

    space = Space("examplemusic", "invented", full=True, direct=direct)
    data = {
        "id": "t1",
        "title": "Glass Rivers",
        "durationMs": 4125,
        "artists": [{"id": "ar1"}, {"id": "ar2", "name": "Ivo Lane"}],
    }
    if missing_ref:
        data["artists"].append({"name": "Ada Reed"})
    track = read_track(space, data)
    album = read_release(space, data)
    assert track is not None and album is not None
    for entry in (song_entry(track), album_entry(album)):
        artists = entry["artists"]
        assert artists[0]["name"] == ""
        assert artists[1]["name"] == "Ivo Lane"
        assert artists[1]["id"] != artist_id(space.ref("ar1", "i"))
        if not missing_ref or not direct:
            assert artists[1]["id"] == artist_id(space.ref("ar2", "i"))
