"""Full catalogs through the dashboard and app API, over a real disposable Navidrome."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from shijhon.catalog.addon import AddonCatalog
from shijhon.catalog.cache import CachedCatalog
from shijhon.delivery.playback import Track
from tests.acceptance.test_J_xml import both
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
from tests.harness.dashboard import PATH, Browser, forms
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeTrack
from tests.harness.full_catalog import DECLARATION, RESOURCES, FullCatalog
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("full-catalog"),
        catalog_settings={"kind": "addon", "addon": "Full", "identity": "examplemusic.us"},
        warm_ahead_depth=0,
    ) as w:
        held = FullCatalog()
        artist = held.artist("ar1")
        assert artist is not None
        held.answers["artist/ar1"] = artist | {"releasesNext": "second"}
        held.pages["second"] = {
            "albums": [{"id": "al20", "title": "Later Rooms", "kind": "single"}]
        }
        addon = w.addon("Full", catalog=held, resources=RESOURCES)
        addon.manifest_extra["fullCatalog"] = dict(DECLARATION)
        w.add_source(addon)
        prepare_collection(nd, w.server)
        yield w


def test_full_metadata_and_paged_releases_reach_json_and_xml(world: DeliveryWorld) -> None:
    client = world.client(headers={"host": "music.test"})
    try:
        # Cold track lookup: there was no search or album request in this process.
        song = both(client, "getSong", {"id": "sh.tr.examplemusic.t102"})[0]["song"]
        assert song["albumId"] == "sh.al.examplemusic.al1"
        assert song["discNumber"] == 2 and song["track"] == 2
        album = both(client, "getAlbum", {"id": song["albumId"]})[0]["album"]
        assert [t["track"] for t in album["song"]] == [1, 2, 3, 4]
        assert all(t["discNumber"] == 2 for t in album["song"])
        artist = both(client, "getArtist", {"id": "sh.ar.examplemusic.ar1"})[0]["artist"]
        assert [r["name"] for r in artist["album"]] == [
            "Glass Rivers",
            "Low Tide - Single",
            "Later Rooms",
        ]
        assert artist["album"][1]["releaseTypes"] == ["ep"]
        assert artist["album"][2]["releaseTypes"] == ["single"]
        # The fixture's image host is unresolved: clients get the cover ID alone.
        assert "artistImageUrl" not in artist
    finally:
        client.close()


@pytest.mark.parametrize("suffix,kind", [("Single", "single"), ("EP", "ep")])
def test_album_title_kind_reaches_artist_and_album_answers(
    navidrome_factory: NavidromeFactory, tmp_path: Path, suffix: str, kind: str
) -> None:
    with delivery_world(
        navidrome_factory(),
        tmp_path,
        catalog_settings={"kind": "addon", "addon": "Full", "identity": "examplemusic.us"},
        warm_ahead_depth=0,
    ) as w:
        held = FullCatalog()
        album = held.album("al2")
        artist = held.artist("ar1")
        assert album is not None and artist is not None
        album.update(title=f"Evening - {suffix}", kind="album")
        held.answers["album/al2"] = album
        held.answers["artist/ar1"] = artist | {"albums": [album]}
        addon = w.addon("Full", catalog=held, resources=RESOURCES)
        addon.manifest_extra["fullCatalog"] = dict(DECLARATION)
        w.add_source(addon)
        client = w.client()
        try:
            albums = both(client, "getArtist", {"id": "sh.ar.examplemusic.ar1"})[0]["artist"][
                "album"
            ]
            assert albums[0]["releaseTypes"] == [kind]
            answer = both(client, "getAlbum", {"id": "sh.al.examplemusic.al2"})[0]["album"]
            assert answer["releaseTypes"] == [kind]
        finally:
            client.close()


def test_catalog_identity_requires_explicit_confirmation_after_review(
    world: DeliveryWorld,
) -> None:
    addon = world.addon("Second", catalog=FullCatalog(), resources=RESOURCES)
    addon.manifest_extra["fullCatalog"] = {"key": "othermusic", "region": "gb"}
    world.add_source(addon)
    browser = Browser(world.server.base_url)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    try:
        page = browser.get("catalog")
        assert "This add-on says its catalog is examplemusic.us." in page.text
        # Changing the add-on first presents its claim, without writing the old identity.
        first = browser.submit("catalog", {"addon": "Second"})
        assert first.status_code == 200
        assert "This add-on says its catalog is othermusic.gb." in first.text
        data = forms(first.text)[f"{PATH}/catalog"]
        assert data["identity-addon"] == "Second" and data["identity"] == "examplemusic.us"
        # An unrelated save preserves the saved identity, even when the claim differs.
        data["timeout_seconds"] = "17"
        assert "confirm-identity" not in data
        assert browser.post("catalog", data).status_code == 303
        assert browser.form("catalog")["identity"] == "examplemusic.us"
        for checked in (True, False):
            data = browser.form("catalog")
            data["timeout_seconds"] = "invalid"
            if checked:
                data["confirm-identity"] = "othermusic.gb"
            refused = browser.post("catalog", data)
            assert refused.status_code == 400
            rendered = forms(refused.text)[f"{PATH}/catalog"]
            assert (rendered.get("confirm-identity") == "othermusic.gb") is checked
            assert rendered["identity"] == "examplemusic.us"
        assert browser.submit("catalog", {"confirm-identity": "othermusic.gb"}).status_code == 303
        again = browser.get("catalog").text
        assert '<option value="Second" selected>' in again
        assert forms(again)[f"{PATH}/catalog"]["identity"] == "othermusic.gb"
    finally:
        # Put the saved settings back; the running catalog is still the original one.
        browser.submit(
            "catalog",
            {
                "addon": "Full",
                "confirm-identity": "examplemusic.us",
                "identity-addon": "Full",
            },
        )
        browser.http.close()


def test_changed_identity_fails_without_rekeying_the_running_catalog(world: DeliveryWorld) -> None:
    catalog = world.services.catalog
    assert isinstance(catalog, CachedCatalog) and isinstance(catalog.inner, AddonCatalog)
    addon = world.addons[0]
    original = dict(DECLARATION)
    addon.manifest_extra["fullCatalog"] = {"key": "changedmusic"}
    client = world.client(headers={"host": "music.test"})
    try:

        async def reread() -> None:
            source = await catalog.inner._source()
            await source.addon.reread()

        world.server.call(reread)
        answer = client.request("getSong", {"id": "sh.tr.examplemusic.t102"}).json()[
            "subsonic-response"
        ]
        assert answer["status"] == "failed"
        assert catalog.key == "examplemusic" and catalog.region == "us"
        browser = Browser(world.server.base_url)
        browser.sign_in(ADMIN_USER, ADMIN_PASSWORD)
        assert "This add-on says its catalog is changedmusic." in browser.get("catalog").text
        browser.http.close()
    finally:
        addon.manifest_extra["fullCatalog"] = original
        world.server.call(reread)
        client.close()


@pytest.mark.parametrize(
    "kind,name,identity,declaration,lookup",
    [
        ("addon", "Full", "examplemusic.us", DECLARATION, False),
        ("addon", "Other", "examplemusic.us", DECLARATION, True),
        ("addon", "Full", "", DECLARATION, True),
        ("addon", "Full", "examplemusic.us", {"key": "changedmusic"}, True),
        ("addon", "Full", "examplemusic.gb", DECLARATION, True),
        ("demo", "", "", DECLARATION, True),
    ],
    ids=["confirmed", "other-addon", "unconfirmed", "changed-key", "changed-region", "adapter"],
)
def test_direct_audio_ids_require_the_configured_confirmed_catalog(
    world: DeliveryWorld,
    tmp_path: Path,
    kind: str,
    name: str,
    identity: str,
    declaration: dict[str, object],
    lookup: bool,
) -> None:
    settings = {"kind": kind}
    if kind == "addon":
        settings.update(addon=name, identity=identity)
    with delivery_world(world.nd, tmp_path, catalog_settings=settings, warm_ahead_depth=0) as w:
        addon = w.addon("Full", resources=RESOURCES)
        addon.manifest_extra["fullCatalog"] = dict(declaration)
        audio = w.audio("confirmed-catalog")
        # Both IDs work: the request log must prove that unsafe direct IDs were avoided.
        addon.add(FakeTrack(isrc="ZZSHA0000101", audio=audio, track_id="t101"))
        addon.add(FakeTrack(isrc="ZZSHA0000101", audio=audio, track_id="matched-recording"))
        w.add_source(addon)

        async def play() -> bytes:
            opened = await w.services.deliverer.open(
                Track(
                    "sh.tr.examplemusic.t101",
                    "ZZSHA0000101",
                    "Glass Rivers",
                    "Mara Venn",
                    4125,
                    ref="examplemusic:t101",
                ),
                None,
            )
            try:
                assert opened.status == 200 and opened.body is not None
                return b"".join([chunk async for chunk in opened.body])
            finally:
                await opened.close()

        assert w.server.call(play) == audio.read_bytes()
        assert bool(addon.requests("resolve-isrc")) is lookup
        assert [r["key"] for r in addon.requests("stream")] == [
            "matched-recording" if lookup else "t101"
        ]


def test_unrelated_save_cannot_confirm_an_unconfirmed_catalog(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    nd = navidrome_factory()
    with delivery_world(
        nd, tmp_path, catalog_settings={"kind": "addon", "addon": "Full"}, warm_ahead_depth=0
    ) as w:
        addon = w.addon("Full", catalog=FullCatalog(), resources=RESOURCES)
        addon.manifest_extra["fullCatalog"] = dict(DECLARATION)
        w.add_source(addon)
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        try:
            data = browser.form("catalog")
            assert data["identity"] == "" and "confirm-identity" not in data
            data.update(timeout_seconds="17", identity="examplemusic.us")
            assert browser.post("catalog", data).status_code == 303
            assert browser.form("catalog")["identity"] == ""
            # A checkbox bound to an older claim cannot confirm the current claim.
            assert (
                browser.submit("catalog", {"confirm-identity": "oldercatalog"}).status_code == 303
            )
            assert browser.form("catalog")["identity"] == ""
            assert (
                browser.submit("catalog", {"confirm-identity": "examplemusic.us"}).status_code
                == 303
            )
            assert browser.form("catalog")["identity"] == "examplemusic.us"
        finally:
            browser.close()
