"""Native authorization and playlist snapshots on disposable stock Navidrome."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anyio
import pytest

from shijhon.downloads import Preparation
from shijhon.downloads.model import PreparationError
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, Album("Invented owner", "Native audio", (Track("Owned song", 1),)))
    nd.scan()
    with delivery_world(nd, tmp_path_factory.mktemp("audio-preparation")) as world:
        yield world


@pytest.fixture
def preparation(world: DeliveryWorld) -> Iterator[tuple[Preparation, str]]:
    async def setup() -> tuple[Preparation, str]:
        service = world.services.downloads
        assert service is not None
        login = await service.client.login(ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
        await service.connect(login["id"], ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
        return service, login["id"]

    service, owner = world.server.call(setup)
    yield service, owner


async def create_playlist(service: Preparation, owner: str, songs: list[str]) -> str:
    auth = await service.connection(owner, ADMIN_USER)
    reply = await service.client.call(
        auth,
        "createPlaylist",
        [("name", "Handmade native playlist"), *[("songId", s) for s in songs]],
    )
    return str(reply["playlist"]["id"])


async def drain(service: Preparation) -> None:
    while work := await service._claim():
        await service._work(*work)


async def wait_batch(service: Preparation, ident: str, owner: str, states: tuple[str, ...]) -> None:
    with anyio.fail_after(30):
        while True:
            batch = await service.repo.batch(ident, owner)
            if batch["state"] in states:
                return
            wake = service.wake
            with anyio.move_on_after(0.05):
                await wake.wait()


def test_native_only_connection_and_owned_duplicates_need_no_audio_fetch(
    world: DeliveryWorld, preparation: tuple[Preparation, str]
) -> None:
    service, owner = preparation
    owned = world.nd.native("GET", "song").json()[0]
    path = world.nd.music / owned["path"]
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    fetches = world.services.download_first.fetches

    async def run() -> None:
        # No history username/key or AI connection is ever created.
        assert (
            await service.repo.store.fetchone("SELECT id FROM playlist_owners WHERE id=?", [owner])
            is None
        )
        playlist = await create_playlist(service, owner, [owned["id"], owned["id"]])
        ident = await service.prepare(owner, ADMIN_USER, playlist)
        await wait_batch(service, ident, owner, ("complete", "finished"))
        batch = (await service.repo.batches(owner))[0]
        assert batch["id"] == ident and batch["state"] == "complete", batch
        assert (batch["total"], batch["unique"], batch["available"]) == (2, 1, 2)
        auth = await service.connection(owner, ADMIN_USER)
        current = await service.client.playlist(auth, playlist)
        assert [s["id"] for s in current["entry"]] == [owned["id"], owned["id"]]

    world.server.call(run)
    assert world.services.download_first.fetches == fetches
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_real_native_foreign_playlist_and_recreated_owner_are_refused(
    world: DeliveryWorld, preparation: tuple[Preparation, str]
) -> None:
    service, owner = preparation
    helper = world.nd.native(
        "POST",
        "user",
        json={
            "userName": "audio-helper",
            "name": "Audio helper",
            "password": "invented-audio-helper",
            "isAdmin": True,
        },
    ).json()["id"]
    other = world.nd.client("audio-helper", "invented-audio-helper")
    try:
        playlist = other.request("createPlaylist", {"name": "Foreign playlist"}).json()[
            "subsonic-response"
        ]["playlist"]["id"]
    finally:
        other.close()

    async def run() -> None:
        with pytest.raises(PreparationError, match="owned by your account"):
            await service.prepare(owner, ADMIN_USER, playlist)
        with pytest.raises(PreparationError, match="account changed"):
            await service.connect(helper, ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
        await service.connect(helper, "audio-helper", "invented-audio-helper", "127.0.0.1")

    world.server.call(run)
    world.nd.native("DELETE", f"user/{helper}")
    world.nd.native(
        "POST",
        "user",
        json={
            "userName": "audio-helper",
            "name": "Replacement",
            "password": "invented-audio-helper",
            "isAdmin": True,
        },
    )

    async def recreated() -> None:
        with pytest.raises(PreparationError, match="expired"):
            await service.connection(helper, "audio-helper")

    world.server.call(recreated)


def test_dashboard_snapshot_and_app_download_join_one_real_acquisition(
    world: DeliveryWorld, preparation: tuple[Preparation, str]
) -> None:
    service, owner = preparation
    addon = world.addon("Local audio preparation")
    song, tracks, _audio = world.placeholder_track("prepare-join", [addon], body_delay=0.8)
    world.add_source(addon)

    async def run() -> str:
        playlist = await create_playlist(service, owner, [song, song])
        ident = await service.prepare(owner, ADMIN_USER, playlist)
        return ident

    ident = world.server.call(run)
    fetches = world.services.download_first.fetches
    client = world.client()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(client.request, "download", {"id": song})
            world.server.call(lambda: wait_batch(service, ident, owner, ("complete", "finished")))
            downloaded = response.result(timeout=45)
            assert downloaded.status_code == 200 and downloaded.headers["content-type"].startswith(
                "audio/"
            )
    finally:
        client.close()
    batch = world.server.call(lambda: service.repo.batch(ident, owner))
    assert batch["state"] == "complete"
    assert world.services.download_first.fetches - fetches == 1
    assert addon.stream_calls[tracks[addon].key()] == 1


def test_real_native_password_reset_stales_saved_authorization(
    world: DeliveryWorld, preparation: tuple[Preparation, str]
) -> None:
    service, _owner = preparation
    user = world.nd.native(
        "POST",
        "user",
        json={
            "userName": "audio-stale",
            "name": "Audio stale",
            "password": "invented-audio-old",
            "isAdmin": True,
        },
    ).json()["id"]

    async def setup() -> None:
        await service.connect(user, "audio-stale", "invented-audio-old", "127.0.0.1")

    world.server.call(setup)
    current = world.nd.native("GET", f"user/{user}").json()
    world.nd.native("PUT", f"user/{user}", json={**current, "password": "invented-audio-new"})

    async def check() -> None:
        with pytest.raises(PreparationError, match="expired"):
            await service.connection(user, "audio-stale")

    world.server.call(check)


def test_cancel_waiting_native_batch_never_requests_audio(
    world: DeliveryWorld, preparation: tuple[Preparation, str]
) -> None:
    service, owner = preparation
    world.clear_sources()
    addon = world.addon("Local queued audio")
    song, tracks, _audio = world.placeholder_track("prepare-cancel", [addon])
    world.add_source(addon)

    async def run() -> None:
        bulk = world.services.download_first.bulk
        assert bulk
        before = bulk.downloads
        bulk.downloads = 1
        source = (await world.services.sources.enabled())[0]
        release = bulk.observe_playback(source)
        try:
            playlist = await create_playlist(service, owner, [song])
            ident = await service.prepare(owner, ADMIN_USER, playlist)
            with anyio.fail_after(10):
                while True:
                    items = await service.repo.store.fetchall(
                        "SELECT state FROM audio_items WHERE batch=?", [ident]
                    )
                    if items[0]["state"] == "waiting":
                        break
                    wake = service.wake
                    with anyio.move_on_after(0.02):
                        await wake.wait()
            await service.action(owner, ADMIN_USER, ident, "cancel")
            release()
            await wait_batch(service, ident, owner, ("cancelled",))
            batch = (await service.repo.batches(owner))[0]
            assert batch["failed"] == 0 and batch["available"] == 0
            assert not addon.stream_calls.get(tracks[addon].key())
        finally:
            release()
            bulk.downloads = before
            bulk.changed()

    world.server.call(run)


def test_a_rate_limited_add_on_pauses_its_batch_instead_of_failing_it(
    world: DeliveryWorld, preparation: tuple[Preparation, str]
) -> None:
    service, owner = preparation
    world.clear_sources()
    addon = world.addon("Local paced audio")  # its 429s name "Retry-After: 1"
    placed = [
        world.placeholder_track(f"prepare-paced-{i}", [addon], rate_limit_streams=int(i == 0))
        for i in range(3)
    ]
    world.add_source(addon)

    async def run() -> None:
        playlist = await create_playlist(service, owner, [song for song, _, _ in placed])
        ident = await service.prepare(owner, ADMIN_USER, playlist)
        await wait_batch(service, ident, owner, ("complete", "finished", "paused"))
        shown = (await service.repo.batches(owner))[0]
        assert shown["id"] == ident and shown["state"] == "complete", shown
        assert (shown["available"], shown["failed"]) == (3, 0)

    world.server.call(run)
    first = placed[0][1][addon].key()
    assert addon.stream_calls[first] == 2  # asked again once its time had passed
    assert all(addon.stream_calls[tracks[addon].key()] == 1 for _, tracks, _ in placed[1:])


def test_real_stop_pauses_snapshot_until_explicit_resume(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    nd = navidrome_factory()
    with delivery_world(nd, tmp_path) as first:
        addon = first.addon("Local before restart")
        one, _tracks, _one_audio = first.placeholder_track("restart-one", [addon])
        two, tracks, two_audio = first.placeholder_track("restart-two", [addon])
        first.add_source(addon)
        client = first.client()
        try:
            assert client.request("download", {"id": one}).status_code == 200
        finally:
            client.close()

        async def enqueue() -> tuple[str, str]:
            service = first.services.downloads
            assert service
            login = await service.client.login(ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
            owner = login["id"]
            await service.connect(owner, ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
            playlist = await create_playlist(service, owner, [one, two])
            bulk = first.services.download_first.bulk
            assert bulk
            bulk.downloads = 1
            bulk.observe_playback((await first.services.sources.enabled())[0])
            ident = await service.prepare(owner, ADMIN_USER, playlist)
            with anyio.fail_after(10):
                while True:
                    rows = await service.repo.store.fetchall(
                        "SELECT song_id,state FROM audio_items WHERE batch=?", [ident]
                    )
                    states = {row["song_id"]: row["state"] for row in rows}
                    if states.get(one) == "available" and states.get(two) == "waiting":
                        return owner, ident
                    wake = service.wake
                    with anyio.move_on_after(0.02):
                        await wake.wait()

        owner, ident = first.server.call(enqueue)
        isrc = tracks[addon].isrc
    with delivery_world(nd, tmp_path) as second:
        service = second.services.downloads
        assert service
        second.clear_sources()
        replacement = second.addon("Local after restart")
        replacement.add(FakeTrack(isrc=isrc, audio=two_audio))
        second.add_source(replacement)

        async def resume() -> None:
            batch = await service.repo.batch(ident, owner)
            assert batch["state"] == "paused"
            assert not replacement.stream_calls
            shown = (await service.repo.batches(owner))[0]
            assert shown["available"] == 1
            await service.action(owner, ADMIN_USER, ident, "resume")
            await wait_batch(service, ident, owner, ("complete", "finished"))
            assert (await service.repo.batch(ident, owner))["state"] == "complete"

        second.server.call(resume)
        assert replacement.stream_calls[isrc] == 1
