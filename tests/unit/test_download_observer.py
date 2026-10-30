"""A caller's progress failure must not become every same-song request's outcome."""

from __future__ import annotations

from pathlib import Path
from threading import Event as ThreadEvent
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import shijhon.delivery.download_first as download_module
from shijhon.delivery.bulk import report
from shijhon.delivery.download_first import DownloadFirst, track_of
from shijhon.delivery.limits import UserLimits


class BatchStopped(Exception):
    pass


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["preparing", "fetching", "verifying"])
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("quota", [1, 10])
async def test_cancelled_observer_detaches_and_joiners_share_one_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    fails: bool,
    quota: int,
) -> None:
    limits = UserLimits(downloads=1, downloads_per_hour=quota, clock=lambda: 0.0)
    row: dict[str, Any] = {
        "song_id": "native-song",
        "state": "placeholder",
        "track_ref": "demo:track",
        "release_ref": "demo:release",
        "title": "Invented song",
        "artist": "Invented artist",
        "isrc": "invented-isrc",
        "duration_ms": 3000,
        "disc": 1,
        "track": 1,
        "tags": "{}",
        "backing_song_id": None,
    }

    async def allowed() -> None:
        return None

    fetcher = DownloadFirst(None, SimpleNamespace(refused=allowed), None, tmp_path, limits=limits)

    async def settled(song: str, **kwargs: Any) -> dict[str, Any]:
        return row

    monkeypatch.setattr(fetcher, "settled", settled)
    entered, capacity, transfer = anyio.Event(), anyio.Event(), anyio.Event()
    acquired = 0

    async def download(item: Any, *, progress: Any, **kwargs: Any) -> str | None:
        nonlocal acquired
        entered.set()
        await capacity.wait()
        await report(progress, "fetching" if stage == "fetching" else "preparing")
        acquired += 1
        await report(progress, "fetching")
        if progress is None:
            await transfer.wait()  # give every app waiter time to join the new leader
        await report(progress, "verifying")
        if fails:
            return "no source: unavailable"
        row["state"] = "delivered"
        return None

    monkeypatch.setattr(fetcher, "_download", download)
    cancelled = False
    observed: list[str] = []

    async def progress(value: str) -> None:
        observed.append(value)
        if cancelled and value == stage:
            raise BatchStopped("the batch was cancelled")

    async def batch() -> None:
        with pytest.raises(BatchStopped):
            await fetcher.ensure("native-song", "owner", bulk=True, progress=progress)

    results = []

    async def app() -> None:
        results.append(await fetcher.ensure("native-song", "owner", bulk=True))

    async def joined(number: int) -> None:
        with anyio.fail_after(2):
            while True:
                waiting = fetcher._waiting.get(("owner", track_of(row).key))
                if waiting and waiting.done.statistics().tasks_waiting == number:
                    return
                await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as group:
        group.start_soon(batch)
        await entered.wait()
        for _ in range(3):
            group.start_soon(app)
        await joined(3)
        cancelled = True
        capacity.set()
        if stage != "verifying":
            await joined(2)  # before admission a new app leader acquires for all three
        transfer.set()
    assert results == ["no source: unavailable" if fails else None] * 3
    assert acquired == 1
    assert observed[-1] == stage
    assert fetcher._waiting == {} and not fetcher._locks.held("native-song")
    record = limits._users["owner"]
    assert record.downloads is not None and record.downloads.value == 1
    assert record.allowance == quota - 1


@pytest.mark.anyio
@pytest.mark.parametrize("subscriber", ["valid", "rejected", "cancelled", "cancel_during_check"])
async def test_inflight_handoff_uses_only_live_interests_and_separates_each_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subscriber: str,
) -> None:
    limits = UserLimits(downloads=1, downloads_per_hour=1, clock=lambda: 0.0)
    row: dict[str, Any] = {
        "song_id": "native-song",
        "state": "placeholder",
        "track_ref": "demo:track",
        "release_ref": "demo:release",
        "title": "Invented song",
        "artist": "Invented artist",
        "isrc": "invented-isrc",
        "duration_ms": 3000,
        "disc": 1,
        "track": 1,
        "tags": "{}",
        "backing_song_id": None,
    }
    fetcher = DownloadFirst(None, None, None, tmp_path, limits=limits)
    entered, complete = anyio.Event(), anyio.Event()
    checking, checked, withdrawn = anyio.Event(), anyio.Event(), anyio.Event()
    acquisitions = 0
    subscriber_checks = []
    outcomes = []

    async def settled(song: str, **kwargs: Any) -> dict[str, Any]:
        return row

    async def acquire(song: str, turn: Any, *args: Any) -> None:
        nonlocal acquisitions
        progress = args[-1]
        turn.spent = True
        await report(progress, "preparing")
        acquisitions += 1
        entered.set()
        await complete.wait()
        await report(progress, "verifying")
        row["state"] = "delivered"
        turn.decided(None)

    monkeypatch.setattr(fetcher, "settled", settled)
    monkeypatch.setattr(fetcher, "_ensure_held", acquire)

    async def revoked(stage: str) -> None:
        if stage == "verifying":
            raise BatchStopped("original grant revoked")

    async def subscriber_progress(stage: str) -> None:
        if stage == "verifying":
            subscriber_checks.append(stage)
            if subscriber == "rejected":
                raise BatchStopped("subscriber grant revoked")
            if subscriber == "cancel_during_check":
                checking.set()
                await checked.wait()

    async def owner() -> None:
        with pytest.raises(BatchStopped, match="original grant"):
            await fetcher.ensure("native-song", "owner", bulk=True, progress=revoked)

    async def interested(scope: anyio.CancelScope) -> None:
        try:
            with scope:
                if subscriber == "rejected":
                    with pytest.raises(BatchStopped, match="subscriber grant"):
                        await fetcher.ensure(
                            "native-song", "owner", bulk=True, progress=subscriber_progress
                        )
                else:
                    outcomes.append(
                        await fetcher.ensure(
                            "native-song", "owner", bulk=True, progress=subscriber_progress
                        )
                    )
        finally:
            withdrawn.set()

    async def app() -> None:
        outcomes.append(await fetcher.ensure("native-song", "owner", bulk=True))

    scope = anyio.CancelScope()
    async with anyio.create_task_group() as group:
        group.start_soon(owner)
        await entered.wait()
        group.start_soon(interested, scope)
        group.start_soon(app)
        with anyio.fail_after(2):
            while (
                fetcher._waiting[("owner", track_of(row).key)].done.statistics().tasks_waiting != 2
            ):
                await anyio.lowlevel.checkpoint()
            if subscriber == "cancelled":
                scope.cancel()
                await withdrawn.wait()
            complete.set()
            if subscriber == "cancel_during_check":
                await checking.wait()
                scope.cancel()
                await withdrawn.wait()
                checked.set()
    assert acquisitions == 1 and row["state"] == "delivered"
    assert outcomes == [None] * (2 if subscriber == "valid" else 1)
    assert subscriber_checks == ([] if subscriber == "cancelled" else ["verifying"])
    assert limits._users["owner"].allowance == 0 and fetcher._waiting == {}


@pytest.mark.anyio
@pytest.mark.parametrize("callback", ["ensure", "turn", "same", "conflict"])
@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("owned", [False, True])
async def test_explicit_turn_keeps_validation_and_rejects_conflicting_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callback: str,
    reject: bool,
    owned: bool,
) -> None:
    row: dict[str, Any] = {
        "song_id": "native-song",
        "state": "placeholder",
        "track_ref": "demo:track",
        "release_ref": "demo:release",
        "title": "Invented song",
        "artist": "Invented artist",
        "isrc": "invented-isrc",
        "duration_ms": 3000,
        "disc": 1,
        "track": 1,
        "tags": "{}",
        "backing_song_id": "owned-song" if owned else None,
    }
    writes, callbacks = [], []
    source = tmp_path / "owned.flac"
    source.write_bytes(b"owned fixture stays unchanged")

    async def settled(song: str, **kwargs: Any) -> dict[str, Any]:
        return row

    async def allowed() -> None:
        return None

    async def replace(song: str, path: Path) -> None:
        writes.append(song)
        row["state"] = "delivered"

    async def backed(item: Any) -> Path:
        return source

    async def download(item: Any, *, progress: Any, **kwargs: Any) -> None:
        await report(progress, "verifying")
        await replace(item["song_id"], source)

    async def guard(stage: str) -> None:
        callbacks.append(stage)
        if reject and stage == "verifying":
            raise BatchStopped("grant revoked")

    async def different(stage: str) -> None:
        callbacks.append(f"different:{stage}")

    fetcher = DownloadFirst(
        None,
        SimpleNamespace(refused=allowed, replace_with_delivered=replace),
        None,
        tmp_path,
        limits=UserLimits(downloads=1, downloads_per_hour=1, clock=lambda: 0.0),
    )
    monkeypatch.setattr(fetcher, "settled", settled)
    monkeypatch.setattr(fetcher, "_owned_file", backed)
    monkeypatch.setattr(fetcher, "_download", download)
    reserved = guard if callback in ("turn", "same", "conflict") else None
    supplied = (
        guard if callback in ("ensure", "same") else different if callback == "conflict" else None
    )
    async with fetcher.turn(track_of(row).key, "owner", progress=reserved) as turn:
        if callback == "conflict":
            with pytest.raises(ValueError, match="different progress callback"):
                await fetcher.ensure("native-song", "owner", turn=turn, progress=supplied)
        elif reject:
            with pytest.raises(BatchStopped, match="grant revoked"):
                await fetcher.ensure("native-song", "owner", turn=turn, progress=supplied)
        else:
            assert (
                await fetcher.ensure("native-song", "owner", turn=turn, progress=supplied) is None
            )
    assert writes == ([] if reject or callback == "conflict" else ["native-song"])
    if callback != "conflict":
        assert "verifying" in callbacks
    assert source.read_bytes() == b"owned fixture stays unchanged"
    assert fetcher._waiting == {} and not fetcher._locks.held("native-song")


@pytest.mark.anyio
async def test_final_surviving_interest_withdrawn_during_inspection_prevents_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row: dict[str, Any] = {
        "song_id": "native-song",
        "state": "placeholder",
        "track_ref": "demo:track",
        "release_ref": "demo:release",
        "title": "Invented song",
        "artist": "Invented artist",
        "isrc": "invented-isrc",
        "duration_ms": 3000,
        "disc": 1,
        "track": 1,
        "tags": "{}",
        "backing_song_id": None,
    }
    entered, eof, inspecting, withdrawn = (anyio.Event() for _ in range(4))
    inspected = ThreadEvent()
    writes = []
    acquisitions = 0

    async def allowed() -> None:
        return None

    async def replace(song: str, path: Path) -> None:
        writes.append(song)
        row["state"] = "delivered"

    async def settled(song: str, **kwargs: Any) -> dict[str, Any]:
        return row

    async def opening(track: Any, user: str, progress: Any, **kwargs: Any) -> Any:
        nonlocal acquisitions
        await report(progress, "preparing")
        await report(progress, "fetching")
        acquisitions += 1
        entered.set()

        async def body() -> Any:
            yield b"fLaC synthetic audio"
            await eof.wait()

        async def close() -> None:
            pass

        return SimpleNamespace(status=200, headers=[], body=body(), remux=None, close=close)

    def inspect(path: Path) -> str:
        anyio.from_thread.run_sync(inspecting.set)
        assert inspected.wait(3), "inspection was never released"
        return ".flac"

    async def revoked(stage: str) -> None:
        if stage == "verifying":
            raise BatchStopped("batch grant revoked")

    limits = UserLimits(downloads=1, downloads_per_hour=1, clock=lambda: 0.0)
    fetcher = DownloadFirst(
        SimpleNamespace(in_library=lambda _: None),
        SimpleNamespace(refused=allowed, replace_with_delivered=replace),
        None,
        tmp_path,
        limits=limits,
        bulk=SimpleNamespace(open=opening),
    )
    monkeypatch.setattr(fetcher, "settled", settled)
    monkeypatch.setattr(download_module, "audio_suffix", inspect)

    async def owner() -> None:
        with pytest.raises(BatchStopped, match="batch grant revoked"):
            await fetcher.ensure("native-song", "owner", bulk=True, progress=revoked)

    scope = anyio.CancelScope()

    async def app() -> None:
        try:
            with scope:
                await fetcher.ensure("native-song", "owner", bulk=True)
        finally:
            withdrawn.set()

    try:
        with anyio.fail_after(3):
            async with anyio.create_task_group() as group:
                group.start_soon(owner)
                await entered.wait()
                group.start_soon(app)
                while (
                    fetcher._waiting[("owner", track_of(row).key)].done.statistics().tasks_waiting
                    != 1
                ):
                    await anyio.lowlevel.checkpoint()
                eof.set()
                await inspecting.wait()
                scope.cancel()
                await withdrawn.wait()
                inspected.set()
    finally:
        inspected.set()
    assert acquisitions == 1 and writes == [] and row["state"] == "placeholder"
    assert limits._users["owner"].allowance == 0
    assert fetcher._waiting == {} and not fetcher._locks.held("native-song")
    assert [path async for path in anyio.Path(tmp_path).iterdir()] == []


@pytest.mark.anyio
@pytest.mark.parametrize("quota", [False, True])
async def test_a_duplicate_requeues_if_its_leader_was_cancelled_before_its_turn(
    tmp_path: Path, quota: bool
) -> None:
    limits = UserLimits(downloads=0 if quota else 1, downloads_per_hour=1 if quota else 0)
    fetcher = DownloadFirst(None, SimpleNamespace(), None, tmp_path, limits=limits)
    scopes: list[anyio.CancelScope] = []
    leader_done, duplicate_in = anyio.Event(), anyio.Event()

    async def leader() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            async with fetcher.turn("song", "owner"):
                raise AssertionError("the leader must stay queued")
        leader_done.set()

    async def duplicate() -> None:
        async with fetcher.turn("song", "owner"):
            duplicate_in.set()

    with anyio.fail_after(2):
        async with anyio.create_task_group() as group:
            async with limits.download("owner") as give_back:
                group.start_soon(leader)
                while (waiting := fetcher._waiting.get(("owner", "song"))) is None:
                    await anyio.lowlevel.checkpoint()
                group.start_soon(duplicate)
                while waiting.done.statistics().tasks_waiting == 0:
                    await anyio.lowlevel.checkpoint()
                scopes[0].cancel()
                await leader_done.wait()
                with anyio.move_on_after(0.03) as blocked:
                    await duplicate_in.wait()
                assert blocked.cancelled_caught
                give_back()
            await duplicate_in.wait()
    assert not fetcher._waiting
