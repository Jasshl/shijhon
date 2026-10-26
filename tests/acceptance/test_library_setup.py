# ruff: noqa: S608 -- The shared predicate contains bound parameters and validated SQL.
"""Reviewed personal setup against real disposable native accounts and music."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest

from shijhon.collection.origins import Origins
from shijhon.collection.policy import default_profile_id, predicate
from shijhon.dashboard.library_setup import Pages
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser, forms
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import PLACEHOLDER_FOLDER
from tests.harness.library import simple_album, write_album

PASSWORD = "invented-setup-password"
SETUP = "collection/setup"
ACTION = "/shijhon/collection/setup"


@pytest.fixture
def world(navidrome_factory: NavidromeFactory, tmp_path) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    nd.native(
        "POST",
        "user",
        json={
            "userName": "owner",
            "name": "owner",
            "password": PASSWORD,
            "isAdmin": True,
        },
    )
    nd.create_user("listener", PASSWORD)
    nd.create_user("never-opened", PASSWORD)
    blocked = nd.create_user("blocked", PASSWORD)
    other = nd.root / "other-music"
    other.mkdir()
    other_id = nd.native(
        "POST",
        "library",
        json={
            "name": "Other library",
            "path": str(other),
            "defaultNewUsers": False,
        },
    ).json()["id"]
    nd.native("PUT", f"user/{blocked}/library", json={"libraryIds": [int(other_id)]})
    write_album(nd.music, simple_album("Glass Orchard", "Existing originals", 2))
    write_album(nd.music / PLACEHOLDER_FOLDER, simple_album("Tideline", "Catalog songs", 1))
    nd.scan(full=True)
    with delivery_world(nd, tmp_path) as w:
        # Exercise this adapter with the real dashboard auth/CSRF and services while
        # its installation in the parent composition is integrated separately.
        routes = w.app.dashboard.app.routes[0].routes
        if not any(getattr(r, "path", "") == "/collection/setup" for r in routes):
            routes[0:0] = Pages(w.app.dashboard).routes()
        w.app.dashboard.attempts.limit = 1000
        w.server.call(lambda: Origins(w.services.store).initialize_legacy("1"))
        yield w


def ids(world):
    return {u["userName"]: u["id"] for u in world.nd.native("GET", "user").json()}


def effective(world, owner):
    async def read():
        selected = predicate(owner, "1", song_expression="t.song_id")
        return {
            r["song_id"]
            for r in await world.services.store.fetchall(
                "SELECT song_id FROM collection_tracks t WHERE " + selected.sql, selected.params
            )
        }

    return world.server.call(read)


def test_setup_form_freezes_native_roster_and_preserves_exact_active_music(world):
    users = ids(world)
    browser = Browser(world.server.base_url)
    listener = world.nd.client("listener", PASSWORD)
    try:
        assert browser.sign_in("owner", PASSWORD).status_code == 303
        world.server.call(lambda: world.services.collection.refresh(users["owner"], "1"))
        tracks = world.nd.native("GET", "song", params={"_start": 0, "_end": 500}).json()
        originals = {s["id"] for s in tracks if s["album"] == "Existing originals"}
        catalog = next(s["id"] for s in tracks if s["album"] == "Catalog songs")
        ordered = sorted(originals)
        listener.ok("star", {"id": ordered[0]})
        listener.ok("scrobble", {"id": ordered[0], "submission": "true"})
        playlist = listener.ok(
            "createPlaylist",
            {"name": "Preserved repeated order", "songId": [ordered[1], catalog, ordered[1]]},
        )["playlist"]["id"]
        before_song = listener.ok("getSong", {"id": ordered[0]})["song"]
        hashes = {
            p: hashlib.sha256(p.read_bytes()).hexdigest() for p in world.nd.music.rglob("*.flac")
        }

        async def select():
            collection = world.services.collection
            await collection.profiles.activate(users["listener"], "original")
            await world.services.store.execute(
                "INSERT INTO collection_membership VALUES (?,?, '1',?,1,4,'prior')",
                [users["listener"], default_profile_id(users["listener"]), "catalog:" + catalog],
            )

        world.server.call(select)
        before = effective(world, users["listener"])
        assert before == originals and catalog not in before
        page = browser.get(SETUP)
        assert page.status_code == 200
        fields = forms(page.text)[ACTION]
        assert fields["legacy_owner_id"] == users["owner"]
        assert users["never-opened"] in page.text
        assert users["admin"] not in page.text and users["blocked"] not in page.text
        assert not world.server.call(
            lambda: world.services.store.fetchone("SELECT 1 FROM collection_originals")
        )
        review = browser.post(SETUP, {**fields, "action": "review"})
        assert review.status_code == 200, review.text
        assert "2 existing original songs" in review.text
        assert "New accounts start empty" in review.text
        assert not world.server.call(
            lambda: world.services.store.fetchone("SELECT 1 FROM collection_originals")
        )
        approval = forms(review.text)[ACTION]
        late = world.nd.create_user("after-review", PASSWORD)
        result = browser.post(
            SETUP,
            {
                **approval,
                "action": "approve",
                "legacy_owner_id": users["never-opened"],
                "actor_id": users["never-opened"],
            },
        )
        assert result.status_code == 303, result.text
        assert effective(world, users["listener"]) == before
        assert effective(world, users["never-opened"]) == originals
        assert effective(world, late) == set()
        assert browser.post(SETUP, {**approval, "action": "approve"}).status_code == 303
        completed = browser.get(SETUP)
        assert "Setup complete" in completed.text and 'name="legacy_owner_id"' not in completed.text
        assert browser.post(SETUP, {**fields, "action": "review"}).status_code == 409
        credited = world.server.call(
            lambda: world.services.store.fetchall(
                "SELECT DISTINCT owner_id FROM collection_originals"
            )
        )
        assert [r["owner_id"] for r in credited] == [users["owner"]]
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes} == hashes
        assert {s["id"] for s in world.nd.native("GET", "song").json()} == {s["id"] for s in tracks}
        after_song = listener.ok("getSong", {"id": ordered[0]})["song"]
        assert after_song["starred"] == before_song["starred"]
        assert after_song["playCount"] == before_song["playCount"]
        assert [
            s["id"] for s in listener.ok("getPlaylist", {"id": playlist})["playlist"]["entry"]
        ] == [ordered[1], catalog, ordered[1]]
        assert "script-src" not in review.headers["content-security-policy"]
        assert PASSWORD not in "".join(r.text for r in browser.seen)
    finally:
        browser.close()


def test_regular_users_and_csrf_refused_and_native_role_rechecked(world):
    users = ids(world)
    regular, admin = Browser(world.server.base_url), Browser(world.server.base_url)
    try:
        assert regular.sign_in("listener", PASSWORD).status_code == 303
        assert regular.get(SETUP).status_code == 403
        assert regular.post(SETUP, {"action": "review"}).status_code == 403
        assert admin.sign_in("owner", PASSWORD).status_code == 303
        world.server.call(lambda: world.services.collection.refresh(users["owner"], "1"))
        fields = forms(admin.get(SETUP).text)[ACTION]
        assert admin.post(SETUP, {**fields, "csrf": "wrong", "action": "review"}).status_code == 403
        review = admin.post(SETUP, {**fields, "action": "review"})
        assert review.status_code == 200, review.text
        approval = forms(review.text)[ACTION]
        world.nd.native(
            "PUT",
            "user/" + users["owner"],
            json={
                "userName": "owner",
                "name": "owner",
                "isAdmin": False,
            },
        )
        denied = admin.post(SETUP, {**approval, "action": "approve"})
        assert denied.status_code == 403
        assert not world.server.call(
            lambda: world.services.store.fetchone("SELECT 1 FROM collection_originals")
        )
    finally:
        regular.close()
        admin.close()


def test_setup_requires_prepared_source_and_fresh_exact_target_grants(world):
    users = ids(world)
    browser = Browser(world.server.base_url)
    try:
        assert browser.sign_in("owner", PASSWORD).status_code == 303
        world.server.call(lambda: world.services.collection.refresh(users["owner"], "1"))
        world.server.call(
            lambda: world.services.store.execute(
                "UPDATE collection_index SET generation='',updated_at=0 WHERE library_id='1'"
            )
        )
        page = browser.get(SETUP)
        assert "Open Collection to prepare music" in page.text
        assert 'name="legacy_owner_id"' not in page.text
        csrf = forms(page.text)["/shijhon/sign-out"]["csrf"]
        fields = {"csrf": csrf, "action": "review", "legacy_owner_id": users["owner"]}
        refused = browser.post(SETUP, fields)
        assert refused.status_code == 409 and "Prepare the music index" in refused.text
        assert not world.server.call(
            lambda: world.services.store.fetchone("SELECT 1 FROM collection_original_setups")
        )
        world.server.call(lambda: world.services.collection.refresh(users["owner"], "1"))
        # Posted owner IDs can only select an eligible native account; actor IDs
        # never replace the immutable session actor.
        assert browser.post(SETUP, {**fields, "legacy_owner_id": users["admin"]}).status_code == 409
        assert (
            browser.post(SETUP, {**fields, "action": "approve", "revision": "²"}).status_code == 409
        )
        review = browser.post(SETUP, fields)
        assert review.status_code == 200, review.text
        approval = {**forms(review.text)[ACTION], "action": "approve"}
        other = next(
            int(lib["id"])
            for lib in world.nd.native("GET", "library").json()
            if int(lib["id"]) != 1
        )
        world.nd.native("PUT", f"user/{users['listener']}/library", json={"libraryIds": [other]})
        assert browser.post(SETUP, approval).status_code == 409
        world.nd.native("DELETE", "user/" + users["listener"])
        replacement = world.nd.create_user("listener", PASSWORD)
        assert replacement != users["listener"]
        assert browser.post(SETUP, approval).status_code == 503
        assert not world.server.call(
            lambda: world.services.store.fetchone("SELECT 1 FROM collection_originals")
        )
    finally:
        browser.close()
