from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

import httpx
import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Mount

from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.origins import Origins
from shijhon.collection.service import Collection
from shijhon.dashboard.auth import Session
from shijhon.dashboard.collection import Pages
from shijhon.store import Store

pytestmark = pytest.mark.anyio


class Access:
    allowed = True
    image = b"\x89PNG\r\n\x1a\n" + b"synthetic"
    mime = "image/png"

    async def authorize(self, owner):
        if not self.allowed:
            raise CollectionError("Access revoked")
        return Actor(owner, frozenset({"1"}))

    async def songs(self, owner, library, offset, limit):
        rows = [
            {
                "id": f"s{i}",
                "title": f"Song {i}",
                "album": "Cloud House",
                "albumId": "a1",
                "artist": "Glass Orchard",
                "artistId": "ar1",
                "path": f"_shijhon/{i}.flac",
                "duration": 180,
                "size": 100,
                "year": 2020,
                "coverArt": "al-a1",
            }
            for i in range(12)
        ]
        return rows[offset : offset + limit]

    async def artwork(self, owner, library, ident):
        return self.image, self.mime


class Dashboard:
    def __init__(self, service, owner="one"):
        self.owner = owner
        self.services = SimpleNamespace(collection=service, navidrome=SimpleNamespace(library_id=1))
        self.env = Environment(
            loader=FileSystemLoader(Path(__file__).parents[2] / "src/shijhon/dashboard/templates"),
            autoescape=True,
            undefined=StrictUndefined,
        )
        self.host = SimpleNamespace(try_spawn=lambda work: False)

    def _personal_page(self, handler, form_reader=None, cache_identity=False):
        async def wrapped(request):
            if request.method == "POST":
                from shijhon.dashboard.app import Refused

                try:
                    request.state.form = (
                        await form_reader(request)
                        if form_reader
                        else dict(parse_qsl((await request.body()).decode()))
                    )
                except Refused as refused:
                    return refused.response
            return await handler(
                request, Session("test", "Listener", "csrf", 9999999999, self.owner)
            )

        return wrapped

    async def _render(self, name, session, current, **context):
        return HTMLResponse(
            self.env.get_template(name).render(
                prefix="/shijhon",
                accent="green",
                csrf="csrf",
                user="Listener",
                nav=[],
                pending=[],
                can_restart=False,
                version="test",
                copyright="2026",
                license="AGPL-3.0",
                source_url="https://example.invalid",
                current=current,
                **context,
            )
        )

    def _see(self, path):
        return RedirectResponse("/shijhon" + path, status_code=303)


@pytest.fixture
async def workspace(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    access = Access()
    service = Collection(store, access)
    await Origins(store).initialize_legacy("1")
    await service.refresh("one", "1")
    app = Starlette(routes=[Mount("/shijhon", routes=Pages(Dashboard(service)).routes())])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield service, access, client
    await store.close()


async def test_builtin_picker_grid_and_credential_free_artwork(workspace):
    service, access, client = workspace
    response = await client.get("/shijhon/collection?view=all")
    assert response.status_code == 200
    text = response.text
    # All first, marked as the library the apps use, then the other two built-in ones.
    positions = [
        text.index(label)
        for label in (
            ">All · in my apps</option>",
            ">Original imported library</option>",
            ">Shijhon library</option>",
        )
    ]
    assert positions == sorted(positions)
    assert "Used in my apps" in text
    assert '<option value="__create">' not in text
    assert 'class="btn collection-create"' in text
    assert "collection-grid" in text and "12 songs" in text
    assert "/collection/artwork/al-a1" in text
    assert all(secret not in text for secret in ("subsonicToken", "subsonicSalt", "Bearer"))
    art = await client.get("/shijhon/collection/artwork/al-a1")
    assert art.status_code == 200 and art.headers["content-type"] == "image/png"
    artist = await client.get("/shijhon/collection?scope=artist&view=all")
    assert "/collection/artwork/ar-ar1" in artist.text
    assert (await client.get("/shijhon/collection/artwork/ar-ar1")).status_code == 200
    assert (await client.get("/shijhon/collection/artwork/ar-foreign")).status_code == 404
    access.image = b"<svg>not a raster</svg>"
    assert (await client.get("/shijhon/collection/artwork/al-a1")).status_code == 404
    access.allowed = False
    assert (await client.get("/shijhon/collection/artwork/al-a1")).status_code == 404
    assert await service.store.fetchone("SELECT count(*) n FROM collection_membership")


async def test_source_preparation_has_personal_progress_without_losing_last_complete_rows(
    workspace,
):
    service, access, client = workspace
    await service.store.execute(
        "UPDATE collection_index SET state='indexing',indexed=7 WHERE library_id='1'"
    )
    page = await client.get("/shijhon/collection?view=all")
    assert "data-source-progress" in page.text and "/collection/source-progress" in page.text
    progress = await client.get("/shijhon/collection/source-progress")
    assert progress.json() == {"state": "indexing", "indexed": 7, "ready": True, "failed": False}
    assert progress.headers["cache-control"] == "no-store"
    access.allowed = False
    assert (await client.get("/shijhon/collection/source-progress")).status_code == 403


async def test_native_album_ids_supply_artwork_when_song_payload_has_no_cover_art(workspace):
    service, _access, client = workspace
    await service.store.execute(
        "UPDATE collection_tracks SET metadata=json_remove(metadata,'$.coverArt')"
    )
    page = await client.get("/shijhon/collection?view=all")
    assert "/collection/artwork/al-a1" in page.text
    assert (await client.get("/shijhon/collection/artwork/al-a1")).status_code == 200
    assert (await client.get("/shijhon/collection/artwork/al-foreign")).status_code == 404
    await service.store.execute("UPDATE collection_tracks SET present=0")
    assert (await client.get("/shijhon/collection/artwork/al-a1")).status_code == 404


async def test_custom_creation_and_direct_whole_album_action_do_not_activate(workspace):
    service, _access, client = workspace
    # A previous personal addition remains available when building another library.
    await client.post(
        "/shijhon/collection",
        data={"single_save": "a1", "request_key": "personal-before-custom", "show": "server"},
    )
    await service.tick()
    browsing = await client.get("/shijhon/collection?scope=song&layout=list")
    create_link = next(
        row["href"] for row in links(browsing.text) if row.get("class") == "btn collection-create"
    )
    form = await client.get(create_link)
    assert 'id="collection-custom" class="collection-custom" open' in form.text
    assert 'placeholder="e.g. Workout" required autofocus' in form.text
    assert 'name="layout" value="list"' in form.text
    assert await service.list_profiles("one") == []
    assert (await service.profile("one"))["selection_id"] == "all"
    created = await client.post(
        "/shijhon/collection",
        data={"action": "create_profile", "name": "Workout", "start": "empty"},
    )
    assert created.status_code == 303
    custom = (await service.list_profiles("one"))[0]["id"]
    assert (await service.profile("one"))["selection_id"] == "all"
    # An empty library opens on the music you collected that it does not hold yet.
    assert dict(parse_qsl(created.headers["location"].split("?", 1)[1]))["view"] == "unsaved"
    page = await client.get(created.headers["location"])
    assert "Use in my apps" in page.text and "Add whole album" in page.text
    assert "12 songs" in page.text
    action = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": custom,
            "view": "all",
            "scope": "album",
            "single_save": "a1",
            "request_key": "direct",
        },
    )
    assert action.status_code == 303
    await service.tick()
    detail = await client.get(action.headers["location"])
    assert "12 songs processed; 12 changed" in detail.text and "Undo this change" in detail.text
    assert (await service.profile("one"))["selection_id"] == "all"
    await client.post("/shijhon/collection", data={"action": "activate", "profile_id": custom})
    assert (await service.profile("one"))["selection_id"] == custom
    # Browsing another library (here the empty Original) leaves the active one in place.
    original = await client.get("/shijhon/collection?profile_id=original&scope=song")
    assert "collection-list" in original.text
    assert "Choose music for this library" in original.text
    assert "Your apps use <strong>Workout</strong>" in original.text
    assert (await service.profile("one"))["selection_id"] == custom


async def test_collection_form_rejects_duplicate_or_oversized_selections(workspace):
    _service, _access, client = workspace
    bad = await client.post(
        "/shijhon/collection",
        content="action=save&action=remove",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert bad.status_code == 400
    big = await client.post(
        "/shijhon/collection",
        content=b"selected=" + b"a" * (256 * 1024),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert big.status_code == 413


@pytest.fixture
async def mixed_workspace(workspace, monkeypatch):
    service, access, client = workspace
    original_songs = access.songs

    async def songs(owner, library, offset, limit):
        rows = await original_songs(owner, library, 0, 500)
        for i, row in enumerate(rows):
            if i < 3:
                row["path"] = f"Originals/{i}.flac"
            if i >= 6:
                row.update(album="Second Pressing", albumId="a2")
            if i in (0, 6):
                row["title"] = "Echo"
        return rows[offset : offset + limit]

    monkeypatch.setattr(access, "songs", songs)
    await service.refresh("one", "1")
    return service, access, client


def links(text):
    from html import unescape
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.result = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.result.append(dict(attrs))

    parser = Parser()
    parser.feed(text)
    return [
        {key: unescape(value) if value else value for key, value in row.items()}
        for row in parser.result
    ]


@pytest.mark.parametrize("layout", ["grid", "list"])
async def test_song_drilldown_keeps_preferred_layout_and_names_versions(mixed_workspace, layout):
    _service, _access, client = mixed_workspace
    albums = await client.get("/shijhon/collection", params={"layout": layout, "view": "all"})
    choose = next(
        row["href"]
        for row in links(albums.text)
        if row.get("aria-label") == "Choose songs in Cloud House by Glass Orchard"
    )
    songs = await client.get(choose)
    assert "collection-list" in songs.text
    assert f'name="layout" value="{layout}"' in songs.text
    assert "Cloud House · Glass Orchard" in songs.text
    assert "Select Song 3 by Glass Orchard from Cloud House" in songs.text
    # The album's imported songs are choices too (a removal wins in every library).
    assert "Select Echo by Glass Orchard from Cloud House" in songs.text
    assert "Select Echo by Glass Orchard from Second Pressing" not in songs.text
    assert '<p class="collection-album">Cloud House</p>' in songs.text
    back = next(
        row["href"]
        for row in links(songs.text)
        if "scope=album" in row.get("href", "") and row.get("class") != "collection-back"
    )
    returned = await client.get(back)
    assert f"collection-{layout}" in returned.text
    assert "Second Pressing" in returned.text
    # A non-JavaScript Browse submission cannot retain a hidden song restriction.
    artists = await client.get(
        "/shijhon/collection", params={"scope": "artist", "album_id": "a1", "view": "all"}
    )
    assert 'name="album_id" value=""' in artists.text
    assert "12 songs" in artists.text
    all_songs = next(
        row["href"]
        for row in links(songs.text)
        if "scope=song" in row.get("href", "")
        and "album_id=&" in row.get("href", "")
        and "origin=all" in row.get("href", "")
        and "pool=server" in row.get("href", "")  # All songs, still everything on the server
    )
    versions = await client.get(all_songs)
    assert "Select Echo by Glass Orchard from Second Pressing" in versions.text


@pytest.mark.parametrize("public_profile", ["all", "shijhon", "original"])
async def test_single_change_and_undo_preserve_browse_context_not_mutation_filter(
    mixed_workspace, public_profile
):
    service, _access, client = mixed_workspace
    data = {
        "profile_id": public_profile,
        "view": "all",
        "scope": "album",
        "origin": "all",
        "search": "Echo",
        "sort": "title",
        "layout": "list",
        "offset": "40",
        "single_save": "a1",
        "request_key": "context-" + public_profile,
    }
    response = await client.post("/shijhon/collection", data=data)
    location = response.headers["location"]
    from urllib.parse import urlsplit

    context = dict(parse_qsl(urlsplit(location).query))
    assert {
        key: context[key] for key in ("profile_id", "search", "origin", "layout", "offset")
    } == {
        "profile_id": public_profile,
        "search": "Echo",
        "origin": "all",
        "layout": "list",
        "offset": "40",
    }
    ident = urlsplit(location).path.rsplit("/", 1)[-1]
    op = await service.operation("one", ident)
    import json

    # The whole album in every library, its imported and catalog songs alike, also those
    # outside the search.
    assert json.loads(op["query"])["origin"] == "all"
    assert json.loads(op["query"])["search"] == ""
    assert op["total"] == 6
    assert op["profile_id"] == (await service.profile("one", public_profile))["id"]
    await service.tick()
    page = await client.get(location)
    assert f'value="{public_profile}" selected' in page.text
    assert "collection-list" in page.text and "Cloud House" in page.text
    assert (
        "This library is used in your apps."
        if public_profile == "all"
        else "Your apps use All, which holds "
        + ("Shijhon library" if public_profile == "shijhon" else "Original imported library")
        + " as you leave it."
    ) in page.text
    browse = next(row["href"] for row in links(page.text) if row.get("class") == "collection-back")
    # This deliberately out-of-range page is clamped without changing its filters.
    assert dict(parse_qsl(urlsplit(browse).query)) == {**context, "offset": "0"}
    undo = await client.post(location, data={"action": "undo"})
    assert dict(parse_qsl(urlsplit(undo.headers["location"]).query)) == context
    await service.tick()
    assert "Undo complete" in (await client.get(undo.headers["location"])).text
    assert (await service.profile("one"))["selection_id"] == "all"
    assert not await service.store.fetchone("SELECT 1 FROM collection_membership WHERE saved=1")


async def test_return_context_cannot_change_operation_target_or_review_scope(workspace):
    service, _access, client = workspace
    custom = await service.create_profile("one", "Workout", start="empty")
    other = await service.create_profile("one", "Other", start="empty")
    response = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": custom,
            "view": "all",
            "scope": "album",
            "action": "save",
            "selection": "matching",
            "request_key": "review",
        },
    )
    await service.tick()
    location = response.headers["location"]
    # Presentation scope may change, but the immutable reviewed set and group unit may not.
    forged_scope = location.replace("scope=album", "scope=song")
    review = await client.get(forged_scope)
    assert "in 1 album," in review.text and "12 matching songs" in review.text
    wrong_target = location.replace(custom, other)
    refused = await client.post(wrong_target, data={"action": "approve"})
    assert "different library" in refused.text
    ident = location.split("/changes/")[1].split("?")[0]
    assert (await service.operation("one", ident))["state"] == "review"
    assert (await client.post(location, data={"action": "approve"})).status_code == 303
    await service.tick()
    assert await service.visible_song_ids("one", "1", ["s0"], profile_id=custom) == frozenset(
        {"s0"}
    )
    assert await service.visible_song_ids("one", "1", ["s0"], profile_id=other) == frozenset()


@pytest.mark.parametrize(
    "params",
    [
        {"profile_id": "original"},
        {"profile_id": "all", "origin": "imported"},
    ],
)
async def test_original_and_imported_music_can_be_chosen_like_any_other(mixed_workspace, params):
    """Every library, Original imported library too, takes hand choices, also of the
    account's own imports: a removal wins over every reason that brings them in."""
    _service, _access, client = mixed_workspace
    page = await client.get("/shijhon/collection", params={**params, "scope": "song"})
    assert "data-row-select" in page.text and "data-selection-tools" in page.text
    assert "Select Echo by Glass Orchard from Cloud House" in page.text
    assert 'name="single_remove"' in page.text
    assert "Your imports stay included" not in page.text
    assert "to choose a subset" not in page.text
    if "origin" in params:
        assert '<option value="imported" selected>Imported</option>' in page.text
        assert "<h2>3 songs</h2>" in page.text  # Cloud House's imported songs
    else:
        assert "<h2>6 songs</h2>" in page.text  # Cloud House, whole


async def test_filtered_whole_actions_are_explicit_and_reset_clears_all_filters(mixed_workspace):
    _service, _access, client = mixed_workspace
    page = await client.get(
        "/shijhon/collection?search=Echo&origin=imported&show=unsaved&scope=album&layout=list"
    )
    assert "Search: Echo · Not in this library · Imported" in page.text
    reset = next(
        row["href"]
        for row in links(page.text)
        if "search=&" in row.get("href", "") and "origin=all" in row.get("href", "")
    )
    assert dict(parse_qsl(reset.split("?", 1)[1]))["view"] == "saved"
    reset_page = await client.get(reset)
    assert "Filters:" not in reset_page.text
    assert "Cloud House" in reset_page.text and "Second Pressing" not in reset_page.text
    available = await client.get(
        "/shijhon/collection?show=server&search=Echo&origin=catalog&scope=album"
    )
    assert "Search: Echo · Everything on the server · From the catalog" in available.text
    assert "Add whole album" in available.text and "From the catalog: 6" in available.text
    assert "Second Pressing" in available.text and "Cloud House" not in available.text
    assert (
        "Add song"
        in (await client.get("/shijhon/collection?view=all&scope=song&origin=catalog")).text
    )


@pytest.mark.parametrize("active", ["original", "custom"])
async def test_builtin_operation_names_the_actual_active_library(workspace, active):
    service, _access, client = workspace
    ident = (
        await service.create_profile("one", "Quiet Mornings", start="empty")
        if active == "custom"
        else active
    )
    await service.activate_profile("one", ident)
    saved = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "shijhon",
            "scope": "album",
            "single_save": "a1",
            "request_key": "inactive-builtin",
        },
    )
    await service.tick()
    text = (await client.get(saved.headers["location"])).text
    name = "Quiet Mornings" if active == "custom" else "Original imported library"
    assert f"Your apps use {name}. Choose Use in my apps to use Shijhon library." in text
    assert "which holds" not in text and "catalog choices" not in text
    if active == "custom":
        assert "Your apps use Original imported library" not in text
    assert (await service.profile("one"))["selection_id"] == ident


async def test_each_built_in_library_lists_its_own_recent_changes(workspace):
    service, _access, client = workspace
    saved = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "all",
            "scope": "album",
            "show": "server",
            "single_save": "a1",
            "request_key": "history",
        },
    )
    await service.tick()
    assert "Recent changes" in (await client.get("/shijhon/collection?profile_id=all")).text
    for other in ("shijhon", "original"):
        page = await client.get(f"/shijhon/collection?profile_id={other}")
        assert "Recent changes" not in page.text
    original = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "original",
            "scope": "album",
            "show": "server",
            "single_save": "a1",
            "request_key": "original-history",
        },
    )
    await service.tick()
    page = await client.get("/shijhon/collection?profile_id=original")
    assert "Recent changes" in page.text and "Add · Original imported library" in page.text
    # A change opens only under its own library.
    wrong = original.headers["location"].replace("profile_id=original", "profile_id=all")
    assert "different library" in (await client.get(wrong)).text
    # Existing direct bookmarks remain usable under their actual target.
    assert "Songs added" in (await client.get(saved.headers["location"].split("?")[0])).text


async def test_collection_asset_urls_change_when_shipped_assets_change(
    workspace, monkeypatch, tmp_path
):
    import re
    import shutil

    from shijhon.dashboard import collection

    assets = tmp_path / "static"
    assets.mkdir()
    source = Path(collection.__file__).parent / "static"
    for name in ("collection.js", "collection.css"):
        shutil.copy(source / name, assets / name)
    monkeypatch.setattr(collection, "files", lambda _package: tmp_path, raising=False)
    _service, _access, client = workspace

    async def urls():
        text = (await client.get("/shijhon/collection?view=all")).text
        return re.findall(r'(?:src|href)="([^"]*/static/collection\.(?:js|css)[^"]*)"', text)

    first = await urls()
    assert len(first) == 2 and all("?v=" in url for url in first)
    assert await urls() == first
    with (assets / "collection.js").open("a") as output:
        output.write("\n// Invented upgrade for cache regression.\n")
    second = await urls()
    assert all(a != b for a, b in zip(first, second, strict=True))
    with (assets / "collection.css").open("a") as output:
        output.write("\n/* Invented stylesheet upgrade. */\n")
    assert await urls() != second


async def enable_catalog_song(
    service, client, title="Song 3", *, action="single_save", profile="all"
):
    from shijhon.collection.contracts import Query

    listing = await service.browse(
        "one", "1", Query(view="all", scope="song", search=title), profile_id=profile
    )
    assert len(listing["rows"]) == 1
    response = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": profile,
            "scope": "song",
            "show": "server",
            action: listing["rows"][0]["item_key"],
            "request_key": f"{action}-one-{profile}-{title}",
        },
    )
    assert response.status_code == 303
    await service.tick()


async def collect(service, owner="one"):
    """Every song in the library becomes music the account collected (its history, as a
    choice on the Collection page records it), in none of its libraries."""
    await service.store.execute(
        "INSERT OR IGNORE INTO collection_history(owner_id,library_id,track_key,added_at)"
        " SELECT ?,library_id,track_key,1 FROM collection_tracks WHERE present=1",
        [owner],
    )


async def test_missing_filter_counts_partial_groups_and_preserves_song_drilldown(mixed_workspace):
    service, _access, client = mixed_workspace
    await enable_catalog_song(service, client)
    # Cloud House holds originals, so it is whole in All: the songs it misses there are
    # the ones explicitly removed.
    for title in ("Song 4", "Song 5"):
        await enable_catalog_song(service, client, title, action="single_remove")
    page = await client.get("/shijhon/collection?view=unsaved&profile_id=all&layout=grid")
    assert 'value="unsaved" selected' in page.text
    assert "Not in this library" in page.text
    assert "2 matching songs · 6 in album" in page.text
    # Only the music the account collected: Second Pressing has nothing of it.
    assert "<h2>1 album</h2>" in page.text and "Second Pressing" not in page.text
    assert "Only music you collected that is not in All is shown" in page.text
    assert "4 songs in All · 2 more you collected" in page.text
    server = await client.get("/shijhon/collection?show=server&profile_id=all")
    assert "<h2>2 albums</h2>" in server.text and "Second Pressing" in server.text
    assert 'name="action" value="remove" data-bulk-action' not in page.text
    assert "Add whole album" in page.text
    choose = next(
        row["href"]
        for row in links(page.text)
        if row.get("aria-label") == "Choose songs in Cloud House by Glass Orchard"
    )
    songs = await client.get(choose)
    assert "view=unsaved" in choose
    assert "<h2>2 songs</h2>" in songs.text
    assert "Select Song 3 by Glass Orchard" not in songs.text
    assert "Select Echo by Glass Orchard from Cloud House" not in songs.text
    assert 'name="layout" value="grid"' in songs.text


async def test_missing_bulk_review_only_contains_unsaved_songs_and_return_view(mixed_workspace):
    from urllib.parse import urlsplit

    from shijhon.collection.contracts import Query

    service, _access, client = mixed_workspace
    # Original imported library holds Cloud House whole (it has imports): its songs 4 and
    # 5 are out of it only when removed by hand; song 3 is chosen there by hand too.
    await enable_catalog_song(service, client, profile="original")
    for title in ("Song 4", "Song 5"):
        await enable_catalog_song(
            service, client, title, action="single_remove", profile="original"
        )
    already = await service.store.fetchall("SELECT * FROM collection_membership WHERE saved=1")
    assert len(already) == 1
    response = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "original",
            "view": "unsaved",
            "scope": "album",
            "search": "Cloud House",
            "layout": "list",
            "action": "save",
            "selection": "matching",
            "request_key": "missing-bulk",
        },
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert dict(parse_qsl(urlsplit(location).query))["view"] == "unsaved"
    await service.tick()
    review = await client.get(location)
    assert "Add 2 matching songs" in review.text and "in 1 album," in review.text
    approved = await client.post(location, data={"action": "approve"})
    await service.tick()
    done = await client.get(approved.headers["location"])
    assert 'value="original" selected' in done.text and 'value="unsaved" selected' in done.text
    assert "2 songs processed; 2 changed" in done.text
    assert (
        await service.browse(
            "one", "1", Query(view="unsaved", search="Cloud House"), profile_id="original"
        )
    )["total"] == 0
    # The neighboring song was not rewritten merely because its album was selected.
    now = await service.store.fetchall(
        "SELECT * FROM collection_membership WHERE track_key=?", [already[0]["track_key"]]
    )
    assert [dict(row) for row in now] == [dict(row) for row in already]
    undone = await client.post(approved.headers["location"], data={"action": "undo"})
    await service.tick()
    assert "view=unsaved" in undone.headers["location"]
    returned = await client.get(undone.headers["location"])
    assert "2 matching songs · 6 in album" in returned.text
    assert (await service.profile("one"))["selection_id"] == "all"


async def test_missing_filter_empty_original_and_custom_libraries(mixed_workspace):
    service, _access, client = mixed_workspace
    original = await client.get("/shijhon/collection?profile_id=original&view=unsaved")
    assert "<h2>0 albums</h2>" in original.text
    assert "<h3>Nothing more to add</h3>" in original.text
    assert "Everything you collected is in Original imported library." in original.text
    assert "data-row-select" not in original.text
    custom = await service.create_profile("one", "Quiet Mornings", start="empty")
    page = await client.get(
        "/shijhon/collection", params={"profile_id": custom, "view": "unsaved", "scope": "song"}
    )
    # The music collected: Cloud House, whole (it has imports); not Second Pressing.
    assert "<h2>6 songs</h2>" in page.text and "from Second Pressing" not in page.text
    assert "Select Echo by Glass Orchard from Cloud House" in page.text
    assert "Add song" in page.text
    assert 'name="action" value="remove" data-bulk-action' not in page.text
    assert (await service.profile("one"))["selection_id"] == "all"


async def test_missing_music_of_an_empty_source_is_filtered_empty_not_complete(workspace):
    service, _access, client = workspace
    await collect(service)
    page = await client.get(
        "/shijhon/collection?profile_id=shijhon&view=unsaved&scope=song&origin=imported"
    )
    assert "<h2>0 songs</h2>" in page.text
    assert "No matching music outside Shijhon library." in page.text
    assert "Everything you collected is in" not in page.text
    available = await client.get(
        "/shijhon/collection?profile_id=shijhon&view=unsaved&scope=song&origin=catalog"
    )
    assert "<h2>12 songs</h2>" in available.text
    await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "shijhon",
            "show": "unsaved",
            "single_save": "a1",
            "request_key": "source-empty",
        },
    )
    await service.tick()
    complete = await client.get("/shijhon/collection?profile_id=shijhon&view=unsaved")
    assert "Everything you collected is in Shijhon library." in complete.text


@pytest.mark.parametrize("offset", [0, 40])
async def test_missing_filter_empty_state_distinguishes_filtered_results(workspace, offset):
    service, _access, client = workspace
    saved = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "all",
            "scope": "album",
            "show": "server",
            "single_save": "a1",
            "request_key": "enable-all-for-empty",
        },
    )
    assert saved.status_code == 303
    await service.tick()
    complete = await client.get(
        "/shijhon/collection", params={"profile_id": "all", "view": "unsaved", "offset": offset}
    )
    assert "Everything you collected is in All." in complete.text
    assert "Page 1 of 1" in complete.text and "← Previous" not in complete.text
    assert 'name="offset" value="0"' in complete.text
    filtered = await client.get(
        "/shijhon/collection?profile_id=all&view=unsaved&search=NoSuchAlbum"
    )
    assert "No matching music outside All." in filtered.text
    assert "Everything you collected is in" not in filtered.text
    reset = next(
        row["href"]
        for row in links(filtered.text)
        if "view=saved" in row.get("href", "")
        and "search=&" in row.get("href", "")
        and "origin=all" in row.get("href", "")
    )
    assert "Cloud House" in (await client.get(reset)).text


async def test_missing_filter_waits_for_source_before_claiming_everything_is_included(workspace):
    service, _access, client = workspace
    await service.store.execute(
        "UPDATE collection_index SET generation='',state='indexing',indexed=0"
    )
    page = await client.get("/shijhon/collection?view=unsaved")
    assert page.status_code == 200
    assert "Preparing your library" in page.text
    assert "Your music will appear once your library is prepared." in page.text
    assert "<h3>Waiting for your library</h3>" in page.text
    assert "Nothing more to add" not in page.text
    assert "Everything you collected is in" not in page.text


async def test_missing_filter_failed_first_browse_does_not_claim_completeness(
    workspace, monkeypatch
):
    from shijhon.collection.contracts import Query

    service, access, client = workspace
    browse = service.browse

    async def revoked_browse(*args, **kwargs):
        access.allowed = False
        return await browse(*args, **kwargs)

    monkeypatch.setattr(service, "browse", revoked_browse)
    page = await client.get("/shijhon/collection?view=unsaved&scope=song&offset=40")
    assert page.status_code == 200 and "Access revoked" in page.text
    assert "<h3>Library view unavailable</h3>" in page.text
    assert "Try loading this view again." in page.text
    assert "Everything you collected is in" not in page.text
    assert "Nothing more to add" not in page.text
    assert "0 songs in All" not in page.text
    assert "<h2>0 songs</h2>" not in page.text
    assert "data-bulk-action" not in page.text and "Collection pages" not in page.text
    retry = next(row["href"] for row in links(page.text) if "data-retry-library" in row)
    assert dict(parse_qsl(retry.split("?", 1)[1]))["view"] == "unsaved"
    access.allowed = True
    assert (await browse("one", "1", Query(view="unsaved", scope="song")))["total"] == 12


async def test_missing_filter_valid_listing_survives_unrelated_form_error(workspace):
    service, _access, client = workspace
    await collect(service)
    page = await client.post(
        "/shijhon/collection",
        data={"view": "unsaved", "scope": "song", "selected": "[invalid"},
    )
    assert page.status_code == 200 and "Select rows and try again." in page.text
    assert "<h2>12 songs</h2>" in page.text and "Song 0" in page.text
    assert "data-bulk-action" in page.text
    assert "Library view unavailable" not in page.text


async def test_missing_last_page_add_returns_to_a_valid_page_with_filters_preserved(
    workspace, monkeypatch
):
    import json
    from urllib.parse import urlsplit

    from shijhon.collection.contracts import Query

    service, access, client = workspace
    original = (await access.songs("one", "1", 0, 40))[0]

    async def songs(owner, library, offset, limit):
        rows = [
            {
                **original,
                "id": f"page-{i}",
                "title": f"Page Song {i:02}",
                "path": f"_shijhon/page-{i}.flac",
                # Each in an album of its own: a song added by hand brings its album.
                "albumId": f"page-album-{i}",
            }
            for i in range(45)
        ]
        return rows[offset : offset + limit]

    monkeypatch.setattr(access, "songs", songs)
    await service.refresh("one", "1")
    await collect(service)
    last = await service.browse(
        "one",
        "1",
        Query(view="unsaved", scope="song", offset=40, pool="personal"),
        profile_id="all",
    )
    assert last["total"] == 45 and len(last["rows"]) == 5
    result = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "all",
            "view": "unsaved",
            "scope": "song",
            "sort": "title",
            "offset": "40",
            "layout": "grid",
            "selection": "selected",
            "selected": json.dumps([r["item_key"] for r in last["rows"]]),
            "action": "save",
            "request_key": "last-page-add",
        },
    )
    assert result.status_code == 303
    location = result.headers["location"]
    await service.tick()
    approved = await client.post(location, data={"action": "approve"})
    await service.tick()
    done = await client.get(approved.headers["location"])
    assert "5 songs processed; 5 changed" in done.text
    assert "<h2>40 songs</h2>" in done.text
    assert "data-row-select" in done.text
    assert "Page 1 of 1" in done.text and "Page 2 of 1" not in done.text
    assert "Showing the last page with matching music." in done.text
    assert "Everything you collected is in" not in done.text
    back = next(r["href"] for r in links(done.text) if r.get("class") == "collection-back")
    context = dict(parse_qsl(urlsplit(back).query))
    assert {k: context[k] for k in ("view", "scope", "profile_id", "offset", "sort", "layout")} == {
        "view": "unsaved",
        "scope": "song",
        "profile_id": "all",
        "offset": "0",
        "sort": "title",
        "layout": "grid",
    }
    assert "data-row-select" in (await client.get(back)).text


async def test_new_personal_default_brings_music_and_every_library_is_editable(
    mixed_workspace,
):
    from shijhon.collection.origins import record_verified_original

    service, _access, client = mixed_workspace
    await service.store.execute("UPDATE collection_library_policy SET mode='personal'")
    empty = await client.get("/shijhon/collection")
    assert 'value="saved" selected' in empty.text
    assert "Bring your music" in empty.text
    assert "Import music" in empty.text and "Browse everything on the server" in empty.text
    assert "Browse shared music" not in empty.text
    assert "data-row-select" not in empty.text
    track = await service.store.fetchone(
        "SELECT track_key FROM collection_tracks WHERE song_id='s0'"
    )
    async with service.store.transaction() as conn:
        await record_verified_original(
            conn,
            owner_id="one",
            library_id="1",
            track_key=track["track_key"],
            song_id="s0",
            provenance_id="receipt-one",
            asset_id="asset-one",
        )
    # Sources: Imported (anyone's imported files, and the account's linked imports) and
    # From the catalog.
    available = await client.get("/shijhon/collection?show=server&scope=album")
    assert "Imported: 3 · From the catalog: 3" in available.text
    assert "From the catalog: 6" in available.text
    assert "Your imports stay included" not in available.text
    assert 'value="imported"' in available.text and 'value="catalog"' in available.text
    assert 'value="own"' not in available.text and 'value="shared"' not in available.text
    # Another account's imports are choices on the server.
    shared = await client.get("/shijhon/collection?view=all&origin=imported&scope=song")
    assert '<option value="imported" selected>Imported</option>' in shared.text
    assert "data-row-select" in shared.text and "Add to All" in shared.text
    assert "Select Song 1 by Glass Orchard" in shared.text
    assert "Select Echo by Glass Orchard from Cloud House" in shared.text
    assert "from Second Pressing" not in shared.text
    # Removed source names are refused rather than mapped to another filter.
    physical = await client.get("/shijhon/collection?view=original&origin=original&scope=song")
    assert "Choose a valid collection filter and page." in physical.text
    # The account's own import brings its album into All whole (the album's other imported
    # files too), and can be removed by hand.
    own = await client.get("/shijhon/collection?origin=imported&scope=song")
    assert "data-row-select" in own.text and "<h2>3 songs</h2>" in own.text
    assert f'name="single_remove" value="{track["track_key"]}"' in own.text
    original = await client.get("/shijhon/collection?profile_id=original&scope=song")
    assert "data-row-select" in original.text and "<h2>6 songs</h2>" in original.text
    catalog = await client.get("/shijhon/collection?profile_id=shijhon&show=server&scope=song")
    assert "<h2>12 songs</h2>" in catalog.text and "Select Song 1 by Glass Orchard" in catalog.text
    assert "0 songs in Shijhon library · 12 on the server" in catalog.text
    assert (await service.profile("one"))["selection_id"] == "all"


async def test_change_notes_name_the_library_your_apps_use(mixed_workspace):
    service, _access, client = mixed_workspace
    await service.store.execute("UPDATE collection_library_policy SET mode='personal'")
    custom = await service.create_profile("one", "Quiet Mornings", start="empty")
    for profile, note in (
        ("all", "This library is used in your apps."),
        ("original", "Your apps use All, which holds Original imported library as you leave it."),
        ("shijhon", "Your apps use All, which holds Shijhon library as you leave it."),
        (custom, "Your apps use All. Choose Use in my apps to use Quiet Mornings."),
    ):
        result = await client.post(
            "/shijhon/collection",
            data={
                "profile_id": profile,
                "show": "server",
                "scope": "album",
                "single_save": "a1",
                "request_key": "note-" + profile,
            },
        )
        await service.tick()
        text = (await client.get(result.headers["location"])).text
        assert "Songs added" in text and note in text, profile
        assert "catalog choices" not in text
    shared = await service.store.fetchone(
        "SELECT track_key FROM collection_tracks WHERE song_id='s1'"
    )
    only_shared = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": "all",
            "show": "server",
            "scope": "song",
            "single_remove": shared["track_key"],
            "request_key": "only-shared-note",
        },
    )
    await service.tick()
    text = (await client.get(only_shared.headers["location"])).text
    assert "Songs removed" in text and "This library is used in your apps." in text


async def choose_personal_tracks(service, owner, song_ids, *, action="save", profile_id="all"):
    from shijhon.collection.contracts import Query

    keys = [
        (
            await service.store.fetchone(
                "SELECT track_key FROM collection_tracks WHERE song_id=?", [s]
            )
        )["track_key"]
        for s in song_ids
    ]
    ident = await service.prepare(
        owner,
        "1",
        Query(view="all", scope="song", pool="server"),
        action=action,
        request_key=f"personal-{owner}-{action}-{profile_id}-{'-'.join(song_ids)}",
        selected=keys,
        profile_id=profile_id,
    )
    await service.tick()
    await service.approve(owner, ident)
    await service.tick()
    assert (await service.operation(owner, ident))["state"] == "done"
    return keys


async def test_personal_default_and_explicit_server_pool_keep_other_accounts_separate(workspace):
    service, access, client = workspace
    # Song 0 in an album of its own: a song added by hand brings its whole album, and the
    # two accounts' albums must not touch.
    every = access.songs

    async def songs(owner, library, offset, limit):
        rows = [
            dict(r, albumId="a0") if r["id"] == "s0" else r
            for r in await every(owner, library, 0, 100)
        ]
        return rows[offset : offset + limit]

    access.songs = songs
    await service.refresh("one", "1")
    await service.store.execute("UPDATE collection_library_policy SET mode='personal'")
    await choose_personal_tracks(service, "one", ["s0"])
    await choose_personal_tracks(service, "two", ["s1"])
    history = [
        dict(row) for row in await service.store.fetchall("SELECT * FROM collection_history")
    ]
    personal = await client.get("/shijhon/collection?scope=song")
    assert '<option value="saved" selected>In this library</option>' in personal.text
    assert "<h2>1 song</h2>" in personal.text
    assert '<h3 id="collection-item-1">Song 0</h3>' in personal.text
    assert ">Song 1</h3>" not in personal.text
    # What the account collected outside All: nothing (the other account's choice is not).
    missing = await client.get("/shijhon/collection?show=unsaved&scope=song")
    assert "<h2>0 songs</h2>" in missing.text and ">Song 1</h3>" not in missing.text
    # Existing query-bearing bookmarks of all music open everything on the server.
    historical = await client.get("/shijhon/collection?view=all&scope=song")
    assert '<option value="server" selected>Everything on the server</option>' in historical.text
    assert "<h2>12 songs</h2>" in historical.text
    global_page = await client.get("/shijhon/collection?show=server&scope=song")
    assert ">Song 1</h3>" in global_page.text
    # Looking at the global pool does not claim another person's music.
    assert [
        dict(row) for row in await service.store.fetchall("SELECT * FROM collection_history")
    ] == (history)
    app = Starlette(routes=[Mount("/shijhon", routes=Pages(Dashboard(service, "two")).routes())])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as second:
        other = await second.get("/shijhon/collection?scope=song")
        other_missing = await second.get("/shijhon/collection?show=unsaved&scope=song")
    assert ">Song 1</h3>" in other.text and ">Song 0</h3>" not in other.text
    assert "<h2>11 songs</h2>" in other.text  # its hand-added song's whole album
    assert ">Song 0</h3>" not in other_missing.text
    assert (await service.profile("one"))["selection_id"] == "all"


async def test_disabled_music_is_available_when_creating_another_empty_library(workspace):
    from urllib.parse import urlsplit

    service, _access, client = workspace
    await service.store.execute("UPDATE collection_library_policy SET mode='personal'")
    await choose_personal_tracks(service, "one", ["s0"])
    await choose_personal_tracks(service, "one", ["s0"], action="remove")
    saved_view = await client.get("/shijhon/collection?scope=song")
    assert "Choose music for this library" in saved_view.text
    assert "Choose Show Not in this library to add music you collected to All." in saved_view.text
    missing = next(row["href"] for row in links(saved_view.text) if "view=unsaved" in row["href"])
    assert "1 more you collected" in saved_view.text
    assert ">Song 0</h3>" in (await client.get(missing)).text
    created = await client.post(
        "/shijhon/collection",
        data={
            "action": "create_profile",
            "name": "Second selection",
            "start": "empty",
            "pool": "server",
            "view": "saved",
            "scope": "song",
            "layout": "grid",
        },
    )
    context = dict(parse_qsl(urlsplit(created.headers["location"]).query))
    assert {k: context[k] for k in ("pool", "view", "scope", "layout")} == {
        "pool": "personal",
        "view": "unsaved",
        "scope": "song",
        "layout": "grid",
    }
    page = await client.get(created.headers["location"])
    assert "<h2>1 song</h2>" in page.text and ">Song 0</h3>" in page.text
    assert "0 songs in Second selection · 1 more you collected" in page.text
    assert "Not in Second selection" in page.text and "Use in my apps" in page.text
    assert (await service.profile("one"))["selection_id"] == "all"


@pytest.mark.parametrize("pool", ["personal", "server"])
@pytest.mark.parametrize("layout", ["grid", "list"])
async def test_pool_survives_drilldown_filters_creation_and_whole_group_actions(
    workspace, pool, layout
):
    import json
    from urllib.parse import urlsplit

    service, _access, client = workspace
    await service.store.execute("UPDATE collection_library_policy SET mode='personal'")
    # Song 0 collected and then removed: in the account's music by its history alone (an
    # addition still in place would bring its whole album).
    await choose_personal_tracks(service, "one", ["s0"])
    await choose_personal_tracks(service, "one", ["s0"], action="remove")
    custom = await service.create_profile("one", "Choose songs", start="empty")
    show, view = ("unsaved", "unsaved") if pool == "personal" else ("server", "all")
    page = await client.get(
        "/shijhon/collection",
        params={"profile_id": custom, "show": show, "layout": layout},
    )
    assert f"|{custom}|{pool}|{view}|album|" in page.text
    choose = next(
        row["href"]
        for row in links(page.text)
        if row.get("aria-label", "").startswith("Choose songs in")
    )
    songs = await client.get(choose)
    assert dict(parse_qsl(urlsplit(choose).query))["pool"] == pool
    assert "collection-list" in songs.text
    assert f'name="layout" value="{layout}"' in songs.text
    back = next(row["href"] for row in links(songs.text) if "scope=album" in row.get("href", ""))
    assert dict(parse_qsl(urlsplit(back).query))["pool"] == pool
    assert f"collection-{layout}" in (await client.get(back)).text
    create = next(
        row["href"] for row in links(page.text) if row.get("class") == "btn collection-create"
    )
    creation = await client.get(create)
    assert f'name="pool" value="{pool}"' in creation.text
    assert f'value="{custom}" selected' in creation.text
    changed = await client.post(
        "/shijhon/collection",
        data={
            "profile_id": custom,
            "show": show,
            "scope": "album",
            "layout": layout,
            "single_save": "a1",
            "request_key": f"whole-{pool}-{layout}",
        },
    )
    location = changed.headers["location"]
    ident = urlsplit(location).path.rsplit("/", 1)[-1]
    op = await service.operation("one", ident)
    assert json.loads(op["query"])["pool"] == pool
    assert op["total"] == (1 if pool == "personal" else 12)
    await service.tick()
    result = await client.get(location)
    back = next(row["href"] for row in links(result.text) if row.get("class") == "collection-back")
    assert dict(parse_qsl(urlsplit(back).query))["pool"] == pool
    # A direct change bookmark has no return query, so use the frozen pool (a whole-album
    # change was made from all of that pool's music: within your music, In this library).
    bookmarked = await client.get(location.split("?")[0])
    assert f'name="pool" value="{pool}"' in bookmarked.text
    frozen_show = "server" if pool == "server" else "saved"
    assert f'<option value="{frozen_show}" selected>' in bookmarked.text


async def test_switching_to_or_from_the_server_resets_song_scope_and_page_without_javascript():
    from shijhon.collection.contracts import CollectionError
    from shijhon.dashboard.collection import parse_query

    changed = parse_query(
        {
            "show": "server",
            "previous_pool": "personal",
            "scope": "song",
            "album_id": "private-album",
            "artist_id": "private-artist",
            "offset": "80",
        }
    )
    assert (changed.view, changed.pool) == ("all", "server")
    assert not changed.album_id and not changed.artist_id and changed.offset == 0
    back = parse_query(
        {"show": "saved", "previous_pool": "server", "scope": "song", "album_id": "a"}
    )
    assert (back.view, back.pool, back.album_id) == ("saved", "personal", "")
    # In this library and Not in this library are one pool: the song scope stays.
    same = parse_query(
        {"show": "unsaved", "previous_pool": "personal", "scope": "song", "album_id": "album"}
    )
    assert (same.view, same.pool, same.album_id) == ("unsaved", "personal", "album")
    # Links of recorded changes and old bookmarks: the closest Show and Source.
    assert (parse_query({"view": "all"}).view, parse_query({"view": "all"}).pool) == (
        "all",
        "server",
    )
    assert parse_query({"view": "original", "pool": "personal"}).pool == "server"
    assert parse_query({"view": "saved", "pool": "server"}).pool == "personal"
    assert parse_query({"view": "unsaved", "pool": "server"}).pool == "personal"
    for fields in ({"origin": o} for o in ("own", "shared", "original", "unknown")):
        with pytest.raises(CollectionError):
            parse_query(fields)
    with pytest.raises(CollectionError):
        parse_query({"sort": "newest"})
    from shijhon.collection.contracts import Query

    with pytest.raises(CollectionError):
        Query(sort="newest")
    with pytest.raises(CollectionError):
        parse_query({"show": "everything"})


async def test_personal_page_stays_bounded_with_a_hundred_thousand_shared_tracks(workspace):
    service, _access, client = workspace
    await service.store.execute("UPDATE collection_library_policy SET mode='personal'")
    await service.store.execute(
        "WITH RECURSIVE numbers(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM numbers WHERE n<99999)"
        " INSERT INTO collection_tracks"
        " SELECT '1','scale:'||n,'scale-native-'||n,'','scale-album-'||(n/10),"
        "'scale-artist-'||(n/100),'Invented song '||n,'Invented album '||(n/10),"
        "'Invented artist '||(n/100),'invented scale song '||n,180,100,2020,'{}',0,1,'fixture',1"
        " FROM numbers"
    )
    await service.store.execute(
        "INSERT INTO collection_history(owner_id,library_id,track_key,added_at)"
        " SELECT 'one',library_id,track_key,1 FROM collection_tracks"
        " WHERE song_id LIKE 'scale-native-%' AND CAST(substr(song_id,14) AS INTEGER)<85"
    )
    page = await client.get("/shijhon/collection?show=unsaved&scope=song")
    assert "<h2>85 songs</h2>" in page.text
    assert page.text.count('<article class="collection-row"') == 40
    assert "0 songs in All · 85 more you collected" in page.text
    assert "Page 1 of 3" in page.text
    assert len(page.content) < 150_000
    next_page = next(row["href"] for row in links(page.text) if "offset=40" in row.get("href", ""))
    assert "pool=personal" in next_page
    assert (await client.get(next_page)).text.count('<article class="collection-row"') == 40
    global_page = await client.get("/shijhon/collection?show=server&scope=song")
    assert "<h2>100,012 songs</h2>" in global_page.text
    assert global_page.text.count('<article class="collection-row"') == 40
    assert "0 songs in All · 100,012 on the server" in global_page.text
    assert len(global_page.content) < 150_000


async def test_unavailable_personal_catalog_history_offers_explicit_restoration(workspace):
    import json

    from shijhon.catalog.model import CatalogRef, CatalogTrack
    from shijhon.collection.contracts import Actor
    from shijhon.collection.history import History

    service, _access, client = workspace
    await service.store.execute("UPDATE collection_library_policy SET mode='personal'")
    await History(service.store).record_catalog(
        Actor("one", frozenset({"1"})),
        "1",
        [
            CatalogTrack(
                CatalogRef("demo", "retained1"),
                "Retained Song",
                "Velvet Fields",
                180_000,
                album=CatalogRef("demo", "retainedalbum"),
                album_title="Second Horizon",
            )
        ],
    )
    page = await client.get("/shijhon/collection?show=unsaved&scope=song")
    assert "Retained Song" in page.text and "1 song needs preparation" in page.text
    assert "Prepare and add song" in page.text
    assert "Not in All" in page.text and "0 songs in All · 1 more you collected" in page.text
    assert (
        "Retained Song" not in (await client.get("/shijhon/collection?show=server&scope=song")).text
    )
    key = (await service.store.fetchone("SELECT track_key FROM collection_history"))["track_key"]
    prepared = await client.post(
        "/shijhon/collection",
        data={
            "show": "unsaved",
            "scope": "song",
            "selection": "selected",
            "selected": json.dumps([key]),
            "action": "save",
            "request_key": "review-unavailable",
        },
    )
    await service.tick()
    review = await client.get(prepared.headers["location"])
    assert "prepared only after approval" in review.text
    assert "1 song needs preparation" in review.text
    assert (
        await service.store.fetchone(
            "SELECT present FROM collection_tracks WHERE track_key=?", [key]
        )
    )["present"] == 0


async def test_legacy_personal_pool_explains_existing_shared_originals(mixed_workspace):
    _service, _access, client = mixed_workspace
    page = await client.get("/shijhon/collection")
    assert (
        "Existing shared originals count as everyone's imports until personal ownership is"
        " reviewed." in page.text
    )
    # The shared originals bring their album into All whole; the album without one stays
    # out of the account's music.
    assert "<h2>1 album</h2>" in page.text
    assert "Imported: 3 · From the catalog: 3" in page.text
    assert "Second Pressing" not in page.text
    missing = await client.get("/shijhon/collection?show=unsaved")
    assert "<h2>0 albums</h2>" in missing.text


async def test_old_frozen_changes_and_undo_bookmarks_keep_server_semantics(workspace):
    import json
    from urllib.parse import urlsplit

    service, _access, client = workspace
    saved = await client.post(
        "/shijhon/collection",
        data={"single_save": "a1", "request_key": "historical-query"},
    )
    ident = urlsplit(saved.headers["location"]).path.rsplit("/", 1)[-1]
    op = await service.operation("one", ident)
    old_query = json.loads(op["query"])
    del old_query["pool"]
    await service.store.execute(
        "UPDATE collection_operations SET query=? WHERE id=?", [json.dumps(old_query), ident]
    )
    await service.tick()
    bookmark = saved.headers["location"].split("?")[0]
    page = await client.get(bookmark)
    assert 'name="pool" value="server"' in page.text
    assert '<option value="server" selected>' in page.text
    undone = await client.post(bookmark, data={"action": "undo"})
    assert dict(parse_qsl(urlsplit(undone.headers["location"]).query))["pool"] == "server"
    await service.tick()
    direct_undo = await client.get(undone.headers["location"].split("?")[0])
    assert 'name="pool" value="server"' in direct_undo.text


async def test_archived_profile_restore_keeps_personal_browse_context(workspace):
    from urllib.parse import urlsplit

    service, _access, client = workspace
    custom = await service.create_profile("one", "Later", start="empty")
    archived = await client.post(
        "/shijhon/collection",
        data={
            "action": "archive",
            "profile_id": custom,
            "pool": "personal",
            "scope": "artist",
            "layout": "list",
        },
    )
    context = dict(parse_qsl(urlsplit(archived.headers["location"]).query))
    assert context["pool"] == "personal" and context["scope"] == "artist"
    page = await client.get(archived.headers["location"])
    from html.parser import HTMLParser

    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = []
            self.current = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.current = {}
            elif self.current is not None and "name" in attrs:
                self.current[attrs["name"]] = attrs.get("value", "")

        def handle_endtag(self, tag):
            if tag == "form":
                self.forms.append(self.current)
                self.current = None

    parsed = Forms()
    parsed.feed(page.text)
    form = next(form for form in parsed.forms if form.get("action") == "restore")
    assert form["pool"] == "personal" and form["profile_id"] == custom
    restored = await client.post("/shijhon/collection", data=form)
    context = dict(parse_qsl(urlsplit(restored.headers["location"]).query))
    assert {k: context[k] for k in ("profile_id", "pool", "scope", "layout")} == {
        "profile_id": custom,
        "pool": "personal",
        "scope": "artist",
        "layout": "list",
    }


@pytest.mark.parametrize(
    "action", ["create_profile", "activate", "rename", "archive", "restore", "refresh"]
)
async def test_head_collection_query_never_performs_actions(workspace, monkeypatch, action):
    from unittest.mock import AsyncMock

    service, _access, client = workspace
    writes = {}
    for method in (
        "create_profile",
        "activate_profile",
        "rename_profile",
        "archive_profile",
        "refresh",
    ):
        writes[method] = AsyncMock(side_effect=AssertionError("HEAD performed an action"))
        monkeypatch.setattr(service, method, writes[method])
    response = await client.head(
        "/shijhon/collection", params={"action": action, "profile_id": "all", "name": "Unwanted"}
    )
    assert response.status_code == 200
    for write in writes.values():
        write.assert_not_called()


@pytest.mark.parametrize("depth", [2, 2000])
async def test_deeply_nested_selection_shows_the_collection_error(workspace, depth):
    _service, _access, client = workspace
    response = await client.post(
        "/shijhon/collection",
        data={"action": "save", "selected": "[" * depth + "]" * depth},
    )
    assert response.status_code == 200
    assert "Select rows and try again." in response.text
