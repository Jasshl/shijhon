"""Personal choices and shared reuse through the actual startup composition."""

from __future__ import annotations

import secrets
from pathlib import Path

import anyio
import httpx

from shijhon.catalog.model import release_from_data
from shijhon.collection.contracts import Query
from shijhon.collection.origins import Origins
from shijhon.store import Store
from shijhon.views.ids import song_id
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection, refresh_collection
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.replay import Replay, fixture


def change(
    world: DeliveryWorld,
    owner: str,
    key: str,
    *,
    saved: bool,
    profile: str = "all",
    changed: int = 1,
):
    async def apply():
        collection = world.services.collection
        assert collection is not None
        operation = await collection.prepare(
            owner,
            "1",
            Query(pool="personal", view="all", scope="song"),
            action="save" if saved else "remove",
            selected=[key],
            profile_id=profile,
            request_key=secrets.token_hex(16),
        )
        for _ in range(10):
            await collection.tick()
            frozen = await collection.operation(owner, operation)
            if frozen["state"] == "review":
                break
        assert frozen["state"] == "review" and frozen["total"] == 1, frozen
        await collection.approve(owner, operation)
        for _ in range(10):
            await collection.tick()
            result = await collection.operation(owner, operation)
            if result["state"] == "done":
                break
        assert result["state"] == "done" and result["changed"] == changed, result
        return result

    return world.server.call(apply)


def browse(
    world: DeliveryWorld,
    owner: str,
    *,
    pool: str = "personal",
    view: str = "all",
    profile: str = "all",
):
    async def listing():
        assert world.services.collection is not None
        return await world.services.collection.browse(
            owner, "1", Query(pool=pool, view=view, scope="song"), profile_id=profile
        )

    return world.server.call(listing)


def test_activity_hand_choices_new_profiles_and_shared_reuse_remain_account_scoped(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    nd = navidrome_factory()
    for user in ("first", "second"):
        nd.create_user(user, "invented-password")
    root = tmp_path / "actual-app"

    async def personal_setup():
        store = await Store.open(root / "state" / "shijhon.sqlite3")
        try:
            await Origins(store).initialize_personal("1")
        finally:
            await store.close()

    anyio.run(personal_setup)
    release = release_from_data(fixture("album-feat-standard")["body"])
    other = release_from_data(fixture("album-single-radio-edit")["body"])
    wanted = release.tracks[0]
    key = "catalog:" + str(wanted.ref)
    album = {"catalog:" + str(track.ref) for track in release.tracks}
    with delivery_world(nd, root, catalog=Replay().catalog()) as world:
        first = nd.client("first", "invented-password").with_base(world.server.base_url)
        second = nd.client("second", "invented-password").with_base(world.server.base_url)
        try:
            # A shared warm/materialization event is not a personal addition. A rating is the
            # account's activity: its whole album comes into its Shijhon library and All,
            # not into Original imported library (a favorite would bring it there too).
            world.materialize(other)
            first.ok("setRating", {"id": song_id(wanted.ref), "rating": "4"})
            a = prepare_collection(nd, world.server, user="first", password="invented-password")
            b = prepare_collection(nd, world.server, user="second", password="invented-password")
            personal = browse(world, a)
            assert {row["item_key"] for row in personal["rows"]} == album
            assert browse(world, b)["total"] == 0
            assert browse(world, a, pool="server")["total"] == len(release.tracks) + 1
            for profile, held in (("all", album), ("shijhon", album), ("original", set())):
                rows = browse(world, a, view="saved", profile=profile)["rows"]
                assert {row["item_key"] for row in rows} == held, profile
            assert browse(world, b, view="saved")["total"] == 0  # another account's rating

            before = world.services.commits.materializations
            second.ok("setRating", {"id": song_id(wanted.ref), "rating": "4"})
            assert {row["item_key"] for row in browse(world, b)["rows"]} == album
            assert world.services.commits.materializations == before
            # A hand removal wins in All; adding it back by hand changes it again.
            change(world, a, key, saved=False)
            assert {r["item_key"] for r in browse(world, a, view="saved")["rows"]} == album - {key}
            change(world, a, key, saved=True)
            assert {r["item_key"] for r in browse(world, a, view="saved")["rows"]} == album
            assert {row["item_key"] for row in browse(world, a)["rows"]} == album

            async def custom():
                collection = world.services.collection
                assert collection is not None
                profile = await collection.create_profile(a, "Quiet evenings", start="empty")
                await collection.activate_profile(a, profile)
                return profile

            custom_id = world.server.call(custom)
            change(world, a, key, saved=True, profile=custom_id)
            # The custom library: by hand - the song added, and so its whole album.
            rows = browse(world, a, view="saved", profile=custom_id)["rows"]
            assert {row["item_key"] for row in rows} == album
            assert {r["item_key"] for r in browse(world, a, view="saved")["rows"]} == album
            browser = Browser(world.server.base_url)
            try:
                assert browser.sign_in("first", "invented-password").status_code == 303
                page = browser.get("collection")
                assert page.status_code == 200
                assert 'name="show"' in page.text and "Not in this library" in page.text
                assert "Everything on the server" in page.text
                assert "Quiet evenings · in my apps" in page.text
            finally:
                browser.close()
        finally:
            first.http.close()
            second.http.close()


def test_personal_history_survives_real_removal_and_explicit_approved_restore(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    nd = navidrome_factory()
    nd.create_user("listener", "invented-password")
    release = release_from_data(fixture("album-single-radio-edit")["body"])
    wanted = release.tracks[0]
    key = "catalog:" + str(wanted.ref)
    with delivery_world(nd, tmp_path / "restore-app", catalog=Replay().catalog()) as world:
        client = nd.client("listener", "invented-password").with_base(world.server.base_url)
        try:
            client.ok("star", {"id": song_id(wanted.ref)})
            owner = prepare_collection(
                nd, world.server, user="listener", password="invented-password"
            )
            original = browse(world, owner)["rows"][0]
            client.ok("unstar", {"id": song_id(wanted.ref)})
            removed = world.server.call(
                lambda: world.services.engine.remove_release(str(release.ref))
            )
            assert removed.removed == 1
            refresh_collection(world.server, owner)
            absent = browse(world, owner)
            assert absent["total"] == 1
            assert absent["rows"][0]["unavailable_tracks"] == 1
            assert browse(world, owner, pool="server")["total"] == 0
            assert browse(world, owner, view="saved")["total"] == 0
            # Restored, the song is in All by the favorite's activity (Shijhon library)
            # again: the hand addition itself changes no visibility.
            change(world, owner, key, saved=True, changed=0)
            restored = browse(world, owner, view="saved")
            assert restored["total"] == 1
            assert restored["rows"][0]["item_key"] == original["item_key"]
            assert restored["rows"][0]["available_tracks"] == 1
        finally:
            client.http.close()


def test_preparing_a_recent_personal_addition_keeps_its_song_drilldown(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    nd = navidrome_factory()
    nd.create_user("recent-listener", "invented-password")
    release = release_from_data(fixture("album-single-radio-edit")["body"])
    wanted = release.tracks[0]
    with delivery_world(nd, tmp_path / "recent-app", catalog=Replay().catalog()) as world:
        owner = prepare_collection(
            nd, world.server, user="recent-listener", password="invented-password"
        )
        client = nd.client("recent-listener", "invented-password").with_base(world.server.base_url)
        try:
            # A rating: its album comes into Shijhon library and All, not into Original
            # imported library, where it is added by hand below.
            client.ok("setRating", {"id": song_id(wanted.ref), "rating": "4"})

            async def scopes():
                collection = world.services.collection
                assert collection is not None
                listing = await collection.browse(
                    owner, "1", Query(pool="personal", view="all", scope="album")
                )
                assert listing["total"] == 1
                return listing["rows"][0]["album_id"], listing["rows"][0]["artist_id"]

            album, artist = world.server.call(scopes)
            change(world, owner, "catalog:" + str(wanted.ref), saved=True, profile="original")

            async def drilldown():
                collection = world.services.collection
                assert collection is not None
                for scope in ({"album_id": album}, {"artist_id": artist}):
                    listing = await collection.browse(
                        owner,
                        "1",
                        Query(pool="personal", view="all", scope="song", **scope),
                    )
                    assert listing["total"] == 1, (scope, listing)

            world.server.call(drilldown)
        finally:
            client.http.close()


def test_native_playlist_addition_uses_the_production_hook_and_brings_its_whole_album_in(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    nd = navidrome_factory()
    nd.create_user("native-listener", "invented-password")
    release = release_from_data(fixture("album-twins-clean")["body"])
    with delivery_world(nd, tmp_path / "native-app", catalog=Replay().catalog()) as world:
        materialized = world.materialize(release)
        owner = prepare_collection(
            nd, world.server, user="native-listener", password="invented-password"
        )
        assert browse(world, owner)["total"] == 0
        login = nd.login("native-listener", "invented-password")
        with httpx.Client(
            base_url=world.server.base_url,
            headers={"x-nd-authorization": "Bearer " + login["token"]},
            timeout=30,
        ) as native:
            playlist = native.post("/api/playlist", json={"name": "Personal ordered music"}).json()
            ids = [materialized.created[track.ref] for track in release.tracks[:2]]
            response = native.post(
                "/api/playlist/" + playlist["id"] + "/tracks",
                json={"ids": [ids[1], ids[0], ids[1]]},
            )
            assert response.status_code == 200 and response.json()["added"] == 3
            # Put into a playlist: the account's activity, the whole album in its Shijhon
            # library and All (not in Original imported library), no hand choice made.
            whole = {"catalog:" + str(track.ref) for track in release.tracks}
            assert {row["item_key"] for row in browse(world, owner)["rows"]} == whole
            for profile, held in (("all", whole), ("shijhon", whole), ("original", set())):
                rows = browse(world, owner, view="saved", profile=profile)["rows"]
                assert {row["item_key"] for row in rows} == held, profile
            assert not world.server.call(
                lambda: world.services.store.fetchall("SELECT 1 FROM collection_membership")
            )
            listed = native.get("/api/playlist/" + playlist["id"] + "/tracks")
            assert [track["mediaFileId"] for track in listed.json()] == [ids[1], ids[0], ids[1]]
