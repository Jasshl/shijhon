"""A cancelled preparation observer detaches from a shared real app download."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from shijhon.delivery.download_first import track_of
from tests.conftest import NavidromeFactory
from tests.harness.agents import connect_agent
from tests.harness.delivery import delivery_world
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER


def test_cancelled_preparation_leaves_shared_app_download_running(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
) -> None:
    with delivery_world(navidrome_factory(), tmp_path, bulk_downloads=1) as world:
        addon = world.addon("Local handoff audio")
        song, tracks, _audio = world.placeholder_track("prepare-handoff", [addon])
        world.add_source(addon)
        service = world.services.downloads
        assert service
        fetcher = world.services.download_first

        async def prepare() -> tuple[str, str, str]:
            login = await service.client.login(ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
            owner = login["id"]
            await service.connect(owner, ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
            auth = await service.connection(owner, ADMIN_USER)
            playlist = await service.client.call(
                auth, "createPlaylist", [("name", "Native handoff playlist"), ("songId", song)]
            )
            bulk = fetcher.bulk
            assert bulk
            release = bulk.observe_playback((await world.services.sources.enabled())[0])
            ident = await service.prepare(owner, ADMIN_USER, playlist["playlist"]["id"])
            row = await fetcher.settled(song)
            assert row
            with anyio.fail_after(10):
                while not bulk._waiting:  # the preparation waits for the slot
                    wake = service.wake
                    with anyio.move_on_after(0.02):
                        await wake.wait()
            # ... holding no turn of the user's: an app's request does not wait behind it.
            assert (ADMIN_USER, track_of(row).key) not in fetcher._waiting
            held.append(release)
            return owner, ident, track_of(row).key

        held = []
        owner, ident, key = world.server.call(prepare)

        async def joined() -> bool:
            # The app's download has its own turn, and waits for the slot after the
            # preparation (it joins no preparation that waits).
            bulk = fetcher.bulk
            assert bulk
            return (ADMIN_USER, key) in fetcher._waiting and len(bulk._waiting) == 2

        async def cancel() -> None:
            await service.action(owner, ADMIN_USER, ident, "cancel")
            held.pop()()

        client = world.client()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(client.request, "download", {"id": song})
                deadline = time.monotonic() + 10
                while not world.server.call(joined):
                    assert time.monotonic() < deadline, "app did not wait for its own slot"
                    time.sleep(0.01)
                world.server.call(cancel)
                answer = response.result(timeout=30)
                assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
        finally:
            client.close()
            for release in held:
                release()
        assert addon.stream_calls[tracks[addon].key()] == 1

        async def check() -> None:
            batch = (await service.repo.batches(owner))[0]
            assert batch["id"] == ident and batch["state"] == "cancelled"
            assert batch["failed"] == 0 and batch["items"][0]["state"] == "paused"
            assert (await fetcher.settled(song))["state"] == "delivered"
            assert fetcher._waiting == {} and not fetcher._locks.held(song)

        world.server.call(check)
        assert not list(fetcher.work_dir.glob("*.part"))


@pytest.mark.parametrize("change", ["generation", "narrow", "revoke"])
def test_revoked_batch_at_eof_hands_file_to_valid_app_with_one_hourly_turn(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    with delivery_world(
        navidrome_factory(),
        tmp_path,
        user_downloads_per_hour=1,
        user_download_burst=1,
        agents=change != "generation",
    ) as world:
        addon = world.addon("Local acquired audio")
        song, tracks, _audio = world.placeholder_track("verify-handoff", [addon])
        world.add_source(addon)
        service = world.services.downloads
        assert service
        fetcher = world.services.download_first
        original = fetcher.ensure
        token = None
        if change != "generation":
            with httpx.Client() as http:
                tokens = connect_agent(
                    http, world.server.base_url, ADMIN_USER, ADMIN_PASSWORD, "status downloads"
                )
            assert service.playlists and service.playlists.oauth
            token = world.server.call(
                lambda: service.playlists.oauth.verify_token(tokens["access_token"])
            )
            assert token

        async def prepare() -> tuple[str, str, anyio.Event, anyio.Event, str]:
            assert fetcher.limits
            fetcher.limits.clock = lambda: 0.0
            at_eof, validate = anyio.Event(), anyio.Event()

            async def guarded(*args: Any, progress: Any = None, **kwargs: Any) -> str | None:
                if progress is None:
                    return await original(*args, progress=None, **kwargs)

                async def pause(stage: str) -> None:
                    if stage == "verifying":
                        at_eof.set()
                        await validate.wait()
                    await progress(stage)

                return await original(*args, progress=pause, **kwargs)

            monkeypatch.setattr(fetcher, "ensure", guarded)
            login = await service.client.login(ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
            owner = login["id"]
            await service.connect(owner, ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
            auth = await service.connection(owner, ADMIN_USER)
            playlist = await service.client.call(
                auth, "createPlaylist", [("name", "Acquired native playlist"), ("songId", song)]
            )
            ident = await service.prepare(
                owner,
                ADMIN_USER,
                playlist["playlist"]["id"],
                creator=token.claims["grant_id"] if token else None,
                idempotency_key="native-handoff-1" if token else "",
            )
            with anyio.fail_after(10):
                await at_eof.wait()
            row = await fetcher.settled(song)
            assert row and row["state"] == "placeholder"
            return owner, ident, at_eof, validate, track_of(row).key

        owner, ident, _eof, validate, key = world.server.call(prepare)

        async def joined() -> bool:
            waiting = fetcher._waiting.get((ADMIN_USER, key))
            return bool(waiting and waiting.done.statistics().tasks_waiting)

        async def revoke_batch() -> None:
            if change == "generation":
                await service.repo.store.execute(
                    "UPDATE audio_owners SET generation='revoked' WHERE id=?", [owner]
                )
            elif change == "narrow":
                await service.repo.store.execute(
                    "UPDATE playlist_agent_tokens SET scopes='[\"status\"]' WHERE id=?",
                    [token.claims["grant_id"]],
                )
            else:
                await service.playlists.oauth.revoke_token(token)
            validate.set()

        client = world.client()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(client.request, "download", {"id": song})
                deadline = time.monotonic() + 10
                while not world.server.call(joined):
                    assert time.monotonic() < deadline, "app did not join the acquired song"
                    time.sleep(0.01)
                world.server.call(revoke_batch)
                answer = response.result(timeout=15)
                assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
        finally:
            client.close()
            validate.set()
        assert addon.stream_calls[tracks[addon].key()] == 1 and fetcher.fetches == 1

        async def check() -> None:
            batch = (await service.repo.batches(owner))[0]
            assert batch["id"] == ident
            if change == "generation":
                assert batch["failed"] == 1
                assert "Authorization or library changed" in batch["items"][0]["error"]
            else:
                assert batch["failed"] == 0 and batch["state"] == "paused"
                assert "Agent authorization changed" in batch["error"]
            assert (await fetcher.settled(song))["state"] == "delivered"
            assert fetcher.limits and fetcher.limits._users[ADMIN_USER].allowance == 0
            assert fetcher._waiting == {} and not fetcher._locks.held(song)

        world.server.call(check)
        assert not list(fetcher.work_dir.glob("*.part"))
