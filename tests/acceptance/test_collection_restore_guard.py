"""Approved personal restores revalidate their target after real publisher waits."""

from __future__ import annotations

from pathlib import Path

import anyio

from shijhon.catalog.model import release_from_data
from shijhon.collection.contracts import Query
from shijhon.views.ids import song_id
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection, refresh_collection
from tests.harness.delivery import delivery_world
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient


def test_archiving_approved_target_while_engine_waits_publishes_nothing_and_can_retry(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    nd = navidrome_factory()
    nd.create_user("restore-guard-listener", "invented-password")
    release = release_from_data(fixture("album-single-radio-edit")["body"])
    wanted = release.tracks[0]
    key = "catalog:" + str(wanted.ref)
    query = Query(pool="personal", view="all", scope="song")
    with delivery_world(nd, tmp_path / "restore-guard", catalog=Replay().catalog()) as world:
        client: SubsonicClient = nd.client("restore-guard-listener", "invented-password").with_base(
            world.server.base_url
        )
        try:
            client.ok("star", {"id": song_id(wanted.ref)})
            owner = prepare_collection(
                nd, world.server, user="restore-guard-listener", password="invented-password"
            )
            client.ok("unstar", {"id": song_id(wanted.ref)})
            removed = world.server.call(
                lambda: world.services.engine.remove_release(str(release.ref))
            )
            assert removed.removed == 1
            refresh_collection(world.server, owner)
            assert not world.placeholder_files() and world.placeholder_rows() == 0

            async def race() -> None:
                collection, engine = world.services.collection, world.services.engine
                assert collection is not None
                store = world.services.store
                with anyio.fail_after(30):
                    async with (
                        anyio.create_task_group() as group,
                        engine.lock_for(str(release.ref)),
                    ):
                        async with collection.worker:
                            target = await collection.create_profile(
                                owner, "Guard target", start="empty"
                            )
                            assert (await collection.profile(owner))["selection_id"] == "all"
                            operation = await collection.prepare(
                                owner,
                                "1",
                                query,
                                profile_id=target,
                                action="save",
                                selected=[key],
                                request_key="restore-guard",
                            )
                            await collection.repo.freeze(
                                await collection.operation(owner, operation)
                            )
                            frozen = await collection.operation(owner, operation)
                            assert frozen["state"] == "review" and frozen["affected"] == 1
                            await collection.approve(owner, operation)
                            generation = (await collection.index_state(owner, "1")).generation
                            retained = dict(await engine.removed_record(str(release.ref)) or {})
                            assert retained
                        group.start_soon(collection.tick)
                        lock = engine._locks._locks[str(release.ref)][0]
                        # Wait for the actual publisher to reach the release lock.
                        while lock.statistics().tasks_waiting == 0:  # noqa: ASYNC110
                            await anyio.sleep(0.01)
                        await collection.archive_profile(owner, target)

                stopped = await collection.repo.operation(owner, operation)
                assert stopped is not None and stopped["state"] == "failed"
                assert stopped["processed"] == stopped["changed"] == 0
                assert not await store.fetchone("SELECT 1 FROM placeholders")
                assert not await store.fetchone("SELECT 1 FROM releases")
                assert not await store.fetchone("SELECT 1 FROM collection_membership")
                assert not world.placeholder_files()
                assert dict(await engine.removed_record(str(release.ref)) or {}) == retained
                assert (await collection.index_state(owner, "1")).generation == generation
                assert not await store.fetchone(
                    "SELECT 1 FROM collection_tracks WHERE track_key=? AND present=1", [key]
                )
                assert (await collection.browse(owner, "1", query))["total"] == 1
                assert not world.placeholder_files()

                # Explicitly make the target eligible and retry the same frozen intent.
                await collection.archive_profile(owner, target, archived=False)
                await collection.retry(owner, operation)
                await collection.tick()
                completed = await collection.operation(owner, operation)
                assert completed["state"] == "done" and completed["changed"] == 1
                listing = await collection.browse(owner, "1", query, profile_id=target)
                assert listing["rows"][0]["item_key"] == key
                assert listing["rows"][0]["available_tracks"] == 1
                assert listing["rows"][0]["saved"] == 1
                assert (await collection.profile(owner))["selection_id"] == "all"
                count = await store.fetchone("SELECT count(*) n FROM placeholders")
                assert count is not None and count["n"] == 1
                assert len(world.placeholder_files()) == 1

            world.server.call(race)
        finally:
            client.close()
