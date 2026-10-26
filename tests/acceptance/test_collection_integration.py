"""Real caller source metadata, default selection and explicit catalog enables."""

from __future__ import annotations

import time

from shijhon.collection.contracts import Query
from tests.harness.dashboard import Browser
from tests.harness.delivery import delivery_world
from tests.harness.engine import catalog_release
from tests.harness.library import Album, Track, write_album


def test_password_sign_in_scans_as_caller_and_keeps_catalog_disabled(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    password = "invented-collection-password"
    nd.create_user("listener", password)
    write_album(nd.music, Album("Glass Orchard", "Paper Garden", (Track("First Light", 1),)))
    nd.scan()
    with delivery_world(nd, tmp_path) as world:
        release = catalog_release("invented-catalog", "Cloud House", "Glass Orchard", 1)
        added = world.materialize(release)
        catalog_id = next(iter(added.created.values()))
        owner = next(
            u["id"] for u in nd.native("GET", "user").json() if u["userName"] == "listener"
        )
        browser = Browser(world.server.base_url)
        try:
            assert browser.sign_in("listener", password).status_code == 303
            collection = world.services.collection
            assert collection is not None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = world.server.call(lambda: collection.index_state(owner, "1"))
                if state.state == "ready":
                    break
                time.sleep(0.05)
            assert state.state == "ready", state
            rows = world.server.call(
                lambda: world.services.store.fetchall(
                    "SELECT * FROM collection_tracks WHERE present=1"
                )
            )
            assert len(rows) == 2
            original = next(r for r in rows if r["song_id"] != catalog_id)
            catalog = next(r for r in rows if r["song_id"] == catalog_id)
            assert original["original"] == 1 and catalog["original"] == 0
            assert world.server.call(
                lambda: collection.visible_song_ids(owner, "1", [original["song_id"], catalog_id])
            ) == {original["song_id"]}
            assert world.server.call(lambda: collection.context_names(owner, "1")) == [
                {"artist": "Glass Orchard", "title": "First Light", "album": "Paper Garden"}
            ]
            operation = world.server.call(
                lambda: collection.prepare(
                    owner,
                    "1",
                    Query(view="all", scope="song"),
                    action="save",
                    selected=[catalog["track_key"]],
                    request_key="explicit-enable",
                    profile_id="shijhon",
                )
            )
            for _ in range(5):
                world.server.call(collection.tick)
            assert (
                world.server.call(lambda: collection.operation(owner, operation))["state"]
                == "review"
            )
            world.server.call(lambda: collection.approve(owner, operation))
            for _ in range(5):
                world.server.call(collection.tick)
            assert (
                world.server.call(lambda: collection.operation(owner, operation))["state"] == "done"
            )
            assert world.server.call(
                lambda: collection.visible_song_ids(owner, "1", [original["song_id"], catalog_id])
            ) == {original["song_id"], catalog_id}
            # The password sign-in's native bearer lives only in the reader's memory.
            token = collection.access._token(owner)
            assert token
            for row in world.server.call(
                lambda: world.services.store.fetchall("SELECT connection FROM playlist_owners")
            ):
                assert token not in row["connection"]
        finally:
            browser.close()
