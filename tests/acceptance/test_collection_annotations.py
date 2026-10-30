"""Accepted native annotation writes show in cached library lists: stars and ratings at
once, play counts once the views playback leaves warm are refreshed."""

from __future__ import annotations

import time

import httpx
import pytest

from shijhon.collection.contracts import Query
from tests.harness.collection import prepare_collection
from tests.harness.delivery import delivery_world
from tests.harness.library import Album, Track, write_album


@pytest.mark.canary
def test_cached_annotations_and_annotation_filters_follow_accepted_writes(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    for title in ("Cloud House", "Paper Garden"):
        write_album(nd.music, Album("Glass Orchard", title, (Track("First Light", 1),)))
    nd.scan()
    songs = nd.native("GET", "song", params={"_sort": "album", "_end": 10}).json()
    selected = songs[0]
    with delivery_world(nd, tmp_path) as world:
        owner = prepare_collection(nd, world.server)
        collection = world.services.collection
        assert collection is not None

        async def choose():
            profile = await collection.create_profile(owner, "One album", start="empty")
            operation = await collection.prepare(
                owner,
                "1",
                Query(pool="server", scope="song", view="all"),
                profile_id=profile,
                selected=["native:" + selected["id"]],
                action="save",
                request_key="one-album",
            )
            for _ in range(10):
                await collection.tick()
                if (await collection.operation(owner, operation))["state"] == "review":
                    break
            await collection.approve(owner, operation)
            for _ in range(10):
                await collection.tick()
                if (await collection.operation(owner, operation))["state"] == "done":
                    break
            assert (await collection.operation(owner, operation))["state"] == "done"
            await collection.activate_profile(owner, profile)

        world.server.call(choose)
        client = nd.client().with_base(world.server.base_url)
        token = nd.login()["token"]
        headers = {"x-nd-authorization": "Bearer " + token}

        def native(starred=False):
            result = httpx.get(
                world.server.base_url + "/api/song",
                headers=headers,
                params={"starred": "true"} if starred else {"_sort": "title"},
            )
            assert result.status_code == 200, result.text
            return result.json()

        def albums(kind):
            return client.ok("getAlbumList2", {"type": kind})["albumList2"].get("album", [])

        assert not albums("starred") and not native(starred=True)
        assert [s["id"] for s in native()] == [selected["id"]]
        baseline = albums("alphabeticalByName")
        assert len(baseline) == 1
        client.ok("star", {"albumId": selected["albumId"], "id": selected["id"]})
        assert [a["id"] for a in albums("starred")] == [selected["albumId"]]
        assert albums("alphabeticalByName")[0]["starred"]
        assert native(starred=True)[0]["id"] == selected["id"]
        assert native()[0]["starred"] is True
        client.ok("setRating", {"id": selected["albumId"], "rating": 4})
        assert albums("alphabeticalByName")[0]["userRating"] == 4
        assert albums("highest")[0]["userRating"] == 4
        client.ok("setRating", {"id": selected["albumId"], "rating": 0})
        assert albums("alphabeticalByName")[0].get("userRating", 0) == 0
        assert not albums("highest")
        assert not albums("recent") and not albums("frequent")
        client.ok("scrobble", {"id": selected["id"], "submission": True})
        # Playback keeps other views warm: their play counts follow by a refresh.
        for _ in range(50):
            if albums("alphabeticalByName")[0].get("playCount") == 1:
                break
            time.sleep(0.1)
        assert albums("alphabeticalByName")[0]["playCount"] == 1
        assert albums("recent")[0]["id"] == selected["albumId"]
        assert albums("frequent")[0]["id"] == selected["albumId"]
        assert native()[0]["playCount"] == 1
        client.ok("unstar", {"albumId": selected["albumId"], "id": selected["id"]})
        assert not albums("starred") and not native(starred=True)
        assert not albums("alphabeticalByName")[0].get("starred")
        assert not native()[0].get("starred")
