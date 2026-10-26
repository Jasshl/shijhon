"""Durable native snapshots, bounded workers and authorization checkpoints."""

from __future__ import annotations

import json
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from shijhon.config import PlaylistSettings
from shijhon.downloads import Preparation
from shijhon.downloads.model import MAX_ENTRIES, Authorization, PreparationError
from shijhon.playlists.model import PlaylistError
from shijhon.store import Store


class Client:
    def __init__(self, root: Path) -> None:
        self.owner = "owner-id"
        self.expired = False
        self.entries = ["owned", "new", "new", "other"]
        self.library = 1
        self.native = SimpleNamespace(library_id=1, song=self.song)
        self.root = root
        self.foreign = False

    async def validate(self, auth: Authorization) -> None:
        if self.expired or auth.user_id != self.owner:
            raise PlaylistError("fixed auth error")

    async def login(self, username: str, password: str, address: str) -> dict[str, str]:
        return {"id": self.owner, "subsonicToken": "invented-token", "subsonicSalt": "salt"}

    async def aclose(self) -> None:
        pass

    async def song(self, ident: str) -> dict[str, Any]:
        return {"id": ident, "libraryId": self.library, "path": ident}

    async def call(self, auth: Authorization, method: str, params: Any = ()) -> dict[str, Any]:
        await self.validate(auth)
        if method == "getSong":
            return {"song": {"id": dict(params)["id"]}}
        if method == "getPlaylists":
            return {
                "playlists": {
                    "playlist": [
                        {"id": "native-list", "owner": "admin", "name": "Handmade"},
                        {"id": "foreign", "owner": "someone-else", "name": "Other"},
                    ]
                }
            }
        raise AssertionError(method)

    async def playlist(self, auth: Authorization, ident: str) -> dict[str, Any]:
        await self.validate(auth)
        if self.foreign or ident != "native-list":
            raise PlaylistError("fixed foreign owner error")
        return {
            "id": ident,
            "name": "Handmade",
            "owner": "admin",
            "songCount": len(self.entries),
            "entry": [{"id": ident, "title": ident} for ident in self.entries],
        }


class Fetcher:
    def __init__(self, root: Path) -> None:
        self.engine = SimpleNamespace(
            layout=SimpleNamespace(
                library_root=root,
                absolute=lambda value: root / value,
            )
        )
        self.calls: Counter[str] = Counter()
        self.rows = {
            ident: {"state": "placeholder", "track_ref": None, "path": ident}
            for ident in ("new", "other")
        }
        self.fail: set[str] = set()
        self.started = anyio.Event()
        self.admitted = anyio.Event()
        self.admission: anyio.Event | None = None
        self.release: anyio.Event | None = None
        self.before_verify: Any = None
        self.changed = anyio.Event()

    async def settled(self, ident: str) -> dict[str, Any] | None:
        return self.rows.get(ident)

    async def ensure(
        self, song: str, user: str, *, bulk: bool, progress: Any, background: bool
    ) -> str | None:
        assert bulk and background and user == "admin"
        self.calls[song] += 1
        self.changed.set()
        self.changed = anyio.Event()
        self.started.set()
        if self.admission:
            await self.admission.wait()
        await progress("preparing")
        await progress("fetching")
        self.admitted.set()
        if self.release:
            await self.release.wait()
        if song in self.fail:
            return "private source detail must not be persisted"
        if self.before_verify:
            await self.before_verify()
        await progress("verifying")
        self.rows[song]["state"] = "delivered"
        (self.engine.layout.library_root / song).write_bytes(b"invented audio")
        return None


@asynccontextmanager
async def world(tmp_path: Path) -> Any:
    store = await Store.open(tmp_path / "state")
    fetcher = Fetcher(tmp_path)
    service = Preparation(store, SimpleNamespace(base_url="", library_id=1), fetcher)
    await service.client.aclose()
    client = Client(tmp_path)
    service.client = client
    (tmp_path / "owned").write_bytes(b"owned audio")
    await service.connect("owner-id", "admin", "password", "127.0.0.1")
    try:
        yield service, client, fetcher
    finally:
        await service.aclose()
        await store.close()


async def drain(service: Preparation) -> None:
    while work := await service._claim():
        await service._work(*work)


def test_snapshot_deduplicates_and_reuses_owned_audio(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            assert [row["id"] for row in await service.playlists_for("owner-id", "admin")] == [
                "native-list"
            ]
            ident = await service.prepare("owner-id", "admin", "native-list")
            client.entries = ["unrequested"]  # native edits never expand a frozen batch
            await drain(service)
            batch = (await service.repo.batches("owner-id"))[0]
            assert batch["state"] == "complete"
            assert (batch["available"], batch["total"], batch["unique"]) == (4, 4, 3)
            assert fetcher.calls == {"new": 1, "other": 1}
            # The listing leaves the frozen snapshot out (it can hold an import's length).
            assert "snapshot" not in batch
            snapshot = (await service.repo.batch(ident, "owner-id"))["snapshot"]
            assert json.loads(snapshot) == ["owned", "new", "new", "other"]
            assert batch["id"] == ident
            assert (tmp_path / "owned").read_bytes() == b"owned audio"

    anyio.run(run)


def test_failed_retry_only_retries_failed_snapshot_songs(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, _client, fetcher):
            fetcher.fail.add("new")
            ident = await service.prepare("owner-id", "admin", "native-list")
            await drain(service)
            batch = (await service.repo.batches("owner-id"))[0]
            assert batch["failed"] == 2 and batch["state"] == "finished"
            assert "private source" not in json.dumps(batch)
            fetcher.fail.clear()
            await service.action("owner-id", "admin", ident, "retry")
            await drain(service)
            assert fetcher.calls == {"new": 2, "other": 1}
            assert (await service.repo.batch(ident, "owner-id"))["state"] == "complete"

    anyio.run(run)


def test_cancel_stops_new_songs_and_keeps_active_audio(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            client.entries = ["new", "other"]
            fetcher.release = anyio.Event()
            ident = await service.prepare("owner-id", "admin", "native-list")
            work = await service._claim()
            assert work
            async with anyio.create_task_group() as group:
                group.start_soon(service._work, *work)
                await fetcher.admitted.wait()
                await service.action("owner-id", "admin", ident, "cancel")
                with pytest.raises(PreparationError, match="active songs"):
                    await service.disconnect("owner-id")
                assert await service._claim() is None
                fetcher.release.set()
            assert (await service.repo.batch(ident, "owner-id"))["state"] == "cancelled"
            assert fetcher.calls == {"new": 1}
            await service.action("owner-id", "admin", ident, "resume")
            await drain(service)
            assert fetcher.calls == {"new": 1, "other": 1}

    anyio.run(run)


def test_cancel_waiting_claim_does_not_start_acquisition(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            client.entries = ["new"]
            fetcher.admission = anyio.Event()
            ident = await service.prepare("owner-id", "admin", "native-list")
            work = await service._claim()
            assert work
            async with anyio.create_task_group() as group:
                group.start_soon(service._work, *work)
                await fetcher.started.wait()
                await service.action("owner-id", "admin", ident, "cancel")
                fetcher.admission.set()
            batch = (await service.repo.batches("owner-id"))[0]
            assert batch["state"] == "cancelled" and batch["failed"] == 0
            assert batch["items"][0]["state"] == "paused"
            assert not fetcher.admitted.is_set() and not (tmp_path / "new").exists()

    anyio.run(run)


def test_cancel_stops_a_song_waiting_again_after_a_rate_limited_attempt(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            client.entries = ["new"]
            waiting, cancelled = anyio.Event(), anyio.Event()

            async def ensure(
                song: str, user: str, *, bulk: bool, progress: Any, background: bool
            ) -> str | None:
                assert background
                await progress("preparing")  # an attempt "too many requests" ended
                await progress("waiting")  # ... the song waits for that pause
                waiting.set()
                await cancelled.wait()
                await progress("waiting")  # the scheduler's next word while it waits
                raise AssertionError("a cancelled batch must stop a waiting song")

            fetcher.ensure = ensure  # type: ignore[method-assign]
            ident = await service.prepare("owner-id", "admin", "native-list")
            work = await service._claim()
            assert work
            async with anyio.create_task_group() as group:
                group.start_soon(service._work, *work)
                await waiting.wait()
                assert (await service.repo.batches("owner-id"))[0]["items"][0]["state"] == (
                    "waiting"
                )
                await service.action("owner-id", "admin", ident, "cancel")
                cancelled.set()
            batch = (await service.repo.batches("owner-id"))[0]
            assert batch["state"] == "cancelled" and batch["failed"] == 0
            assert batch["items"][0]["state"] == "paused"

    anyio.run(run)


def test_imported_length_playlists_prepare_in_bounded_reads(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            # Longer than the former fixed ceiling of 1,000 entries, with a repeat.
            client.entries = [f"song-{i}" for i in range(1500)] + ["song-0"]
            store = service.repo.store
            await store.execute(
                "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
                " VALUES ('demo:r','folder','album','Album','Artist','{}','{}',0)"
            )
            for song in ("song-10", "song-1200"):  # placeholders in two lookup rounds
                await store.execute(
                    "INSERT INTO placeholders(track_ref,release_ref,song_id,path,"
                    "placeholder_path,state,title,artist,album,duration_ms,disc,track,tags,"
                    "created_at) VALUES (?,'demo:r',?,?,?,'placeholder','T','A','Al',1000,1,1,"
                    "'{}',0)",
                    [f"demo:{song}", song, song, song],
                )
            ident = await service.prepare("owner-id", "admin", "native-list")
            refs = {
                row["song_id"]: row["track_ref"]
                for row in await store.fetchall(
                    "SELECT song_id,track_ref FROM audio_items WHERE track_ref IS NOT NULL"
                )
            }
            assert refs == {"song-10": "demo:song-10", "song-1200": "demo:song-1200"}
            batch = (await service.repo.batches("owner-id"))[0]
            assert batch["id"] == ident and (batch["total"], batch["unique"]) == (1501, 1500)
            assert len(batch["items"]) == 50 and batch["items"][-1]["song_id"] == "song-49"
            assert len(json.loads((await service.repo.batch(ident, "owner-id"))["snapshot"])) == (
                1501
            )
            # The limit is the installation's playlist import limit.
            service.playlists = SimpleNamespace(settings=PlaylistSettings(import_max_entries=1000))
            assert service.max_entries == 1000
            with pytest.raises(PreparationError, match="at most 1,000 entries"):
                await service.prepare("owner-id", "admin", "native-list")
            assert not fetcher.calls

    anyio.run(run)


def test_worker_pool_follows_live_global_capacity(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            client.entries = [f"new-{i}" for i in range(5)]
            fetcher.rows = {
                ident: {"state": "placeholder", "track_ref": None, "path": ident}
                for ident in client.entries
            }
            fetcher.bulk = SimpleNamespace(downloads=1)
            fetcher.release = anyio.Event()
            async with anyio.create_task_group() as group:
                group.start_soon(service.run)
                await anyio.sleep(0.01)
                await service.prepare("owner-id", "admin", "native-list")
                with anyio.fail_after(3):
                    while len(fetcher.calls) < 1:
                        await fetcher.changed.wait()
                assert len(fetcher.calls) == 1
                fetcher.bulk.downloads = 5
                service.notify()
                with anyio.fail_after(3):
                    while len(fetcher.calls) < 5:
                        await fetcher.changed.wait()
                assert len(fetcher.calls) == 5
                fetcher.release.set()
                group.cancel_scope.cancel()

    anyio.run(run)


@pytest.mark.parametrize("change", ["recreated", "expired", "library", "generation"])
def test_authorization_rechecked_after_wait_before_placement(tmp_path: Path, change: str) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            client.entries = ["new"]

            async def invalidate() -> None:
                if change == "recreated":
                    client.owner = "replacement-id"
                elif change == "expired":
                    client.expired = True
                elif change == "library":
                    client.library = 2
                else:
                    await service.repo.store.execute(
                        "UPDATE audio_owners SET generation='changed' WHERE id='owner-id'"
                    )

            fetcher.before_verify = invalidate
            await service.prepare("owner-id", "admin", "native-list")
            await drain(service)
            batch = (await service.repo.batches("owner-id"))[0]
            assert batch["failed"] == 1
            assert not (tmp_path / "new").exists()

    anyio.run(run)


def test_restart_reconciles_delivered_files_and_requires_resume(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            client.entries = ["new", "other"]
            ident = await service.prepare("owner-id", "admin", "native-list")
            await service.repo.progress(ident, "new", "verifying")
            await service.repo.store.execute(
                "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
                " VALUES ('demo:r','folder','album','Album','Artist','{}','{}',0)"
            )
            # Reconcile uses the authoritative engine table, never a remote URL.
            await service.repo.store.execute(
                "INSERT INTO placeholders(track_ref,release_ref,song_id,path,placeholder_path,"
                "state,title,artist,album,duration_ms,disc,track,tags,created_at)"
                " VALUES ('demo:t','demo:r','new','new','new','delivered',"
                " 'New','Artist','Album',1000,1,1,'{}',0)"
            )
            (tmp_path / "new").write_bytes(b"already committed")
            fetcher.rows["new"]["state"] = "delivered"
            await service.reconcile()
            assert await service._claim() is None
            batch = (await service.repo.batches("owner-id"))[0]
            assert batch["available"] == 1 and batch["state"] == "paused"
            await service.action("owner-id", "admin", ident, "resume")
            await drain(service)
            assert fetcher.calls == {"other": 1}

    anyio.run(run)


def test_owner_foreign_playlist_and_batch_bounds(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, client, fetcher):
            client.foreign = True
            with pytest.raises(PreparationError, match="owned by your account"):
                await service.prepare("owner-id", "admin", "native-list")
            client.foreign = False
            client.entries = ["new"] * (MAX_ENTRIES + 1)
            with pytest.raises(PreparationError, match="at most"):
                await service.prepare("owner-id", "admin", "native-list")
            client.entries = ["new"]
            ident = await service.prepare("owner-id", "admin", "native-list")
            with pytest.raises(PreparationError, match="unavailable"):
                await service.action("foreign-owner", "admin", ident, "cancel")
            assert not fetcher.calls

    anyio.run(run)


def test_disconnect_tombstone_prevents_automatic_reuse(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (service, _client, fetcher):
            auth = await service.connection("owner-id", "admin")

            async def saved(owner: str) -> Authorization:
                return auth

            service.playlists = SimpleNamespace(repo=SimpleNamespace(connection=saved))
            await service.disconnect("owner-id")
            with pytest.raises(PreparationError, match="Connect"):
                await service.connection("owner-id", "admin")
            assert not fetcher.calls

    anyio.run(run)
