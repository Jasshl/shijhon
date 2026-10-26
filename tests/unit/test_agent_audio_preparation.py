"""Agent audio jobs reuse durable native snapshots with independent, revocable consent."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from jinja2 import Environment, PackageLoader
from pydantic import SecretStr

from shijhon.access import Principal
from shijhon.agents.app import Agents
from shijhon.collection.history import queue_pending
from shijhon.downloads import Preparation
from shijhon.downloads.model import PreparationError
from shijhon.proxy.forwarding import Forwarding
from tests.unit.test_agents import BASE, begin, call, finish
from tests.unit.test_audio_preparation import Client, Fetcher, drain
from tests.unit.test_playlist_requests import service_at


@asynccontextmanager
async def world(path: Path):
    async with service_at(path / "db", ai=False) as playlists:
        playlists.client.login = AsyncMock(
            return_value={
                "id": "owner",
                "username": "owner",
                "subsonicToken": "native-secret",
                "subsonicSalt": "salt",
            }
        )
        fetcher = Fetcher(path)
        preparation = Preparation(
            playlists.repo.store, playlists.client.native, fetcher, playlists=playlists
        )
        await preparation.client.aclose()
        client = Client(path)
        client.owner = "owner"
        preparation.client = client
        original = fetcher.ensure

        async def ensure(song, user, **kwargs):
            assert user == "owner"
            return await original(song, "admin", **kwargs)

        fetcher.ensure = ensure
        (path / "owned").write_bytes(b"owned audio")
        host = SimpleNamespace(
            services=SimpleNamespace(downloads=preparation),
            forwarding=Forwarding(),
            dashboard=SimpleNamespace(
                env=Environment(
                    loader=PackageLoader("shijhon.dashboard", "templates"),
                    autoescape=True,
                )
            ),
        )
        app = Agents(host, playlists, BASE)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(app.run)
                await app.ready.wait()
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=BASE
                ) as http:
                    yield app, http, preparation, client, fetcher
                group.cancel_scope.cancel()
        finally:
            await preparation.aclose()


async def submit(http, tokens, playlist="native-list", key="prepare-native-1"):
    return await call(
        http,
        tokens["access_token"],
        "prepare_playlist_audio",
        {
            "playlist_id": playlist,
            "idempotency_key": key,
        },
    )


@pytest.mark.parametrize("stage", ["queued", "claimed", "forgotten"])
@pytest.mark.parametrize("remove", ["account", "grant"])
def test_deleting_agent_authorization_removes_jobs_and_tombstones_atomically(
    tmp_path: Path, stage: str, remove: str
) -> None:
    async def run() -> None:
        async with world(tmp_path) as (app, http, prep, _client, fetcher):
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")
            token = await app.oauth.verify_token(tokens["access_token"])
            assert token
            creator = token.claims["grant_id"]
            result = await submit(http, tokens)
            ident = result["batch"]["id"]
            store = prep.repo.store
            auth = await prep.repo.owner("owner")
            assert auth and auth["authorization"]
            work = await prep._claim() if stage == "claimed" else None
            if stage == "forgotten":
                await prep.action("owner", "owner", ident, "cancel")
                await prep.action("owner", "owner", ident, "forget")
                assert (await prep.repo.replay(creator, "prepare-native-1"))["batch"] is None

            # An unrelated owner's authorization and jobs must survive either delete.
            await store.execute(
                "INSERT INTO playlist_owners(id,username,connection,generation)"
                " VALUES ('other','other','{}','other-generation')"
            )
            await store.execute(
                "INSERT INTO playlist_agent_tokens(id,owner,library,name,scopes,created)"
                " VALUES ('other-grant','other','1','Other','[]',1)"
            )
            await store.execute(
                "INSERT INTO audio_owners VALUES"
                " ('other','other','other-auth','other-generation',1)"
            )
            other = await prep.repo.owner("other")
            other_batch = await prep.repo.enqueue(
                other,
                {"id": "other-list"},
                [],
                [],
                creator="other-grant",
                idempotency_key="other-request",
            )
            table, key = (
                ("playlist_owners", "owner")
                if remove == "account"
                else ("playlist_agent_tokens", creator)
            )

            async def delete(db):
                if table == "playlist_owners":
                    await db.execute("DELETE FROM playlist_owners WHERE id=?", [key])
                else:
                    await db.execute("DELETE FROM playlist_agent_tokens WHERE id=?", [key])

            # A failed transaction must retain both the credentials and their work.
            with pytest.raises(RuntimeError, match="rollback"):
                async with store.transaction() as db:
                    await delete(db)
                    raise RuntimeError("rollback")
            assert await prep.repo.owner("owner") == auth
            assert await prep.repo.replay(creator, "prepare-native-1")
            assert await app.oauth.verify_token(tokens["access_token"])

            async with store.transaction() as db:
                await delete(db)
            assert not await prep.repo.batches("owner")
            assert not await prep.repo.replay(creator, "prepare-native-1")
            assert not await store.fetchone("SELECT 1 FROM audio_items WHERE batch=?", [ident])
            assert not await app.oauth.verify_token(tokens["access_token"])
            client = await app.oauth.get_client(tokens["client_id"])
            assert not await app.oauth.load_refresh_token(client, tokens["refresh_token"])
            if remove == "account":
                disconnected = await prep.repo.owner("owner")
                assert disconnected["authorization"] is None
                assert disconnected["generation"] != auth["generation"]
                assert not await app.service.repo.connection("owner")
            assert await prep.repo.owner("other") == other
            assert (await prep.repo.batch(other_batch, "other"))["creator"] == "other-grant"
            assert await prep.repo.replay("other-grant", "other-request")
            if work:
                await prep._work(*work)
            assert not fetcher.calls  # Deleted/claimed jobs cannot acquire audio.
            assert not await store.fetchall("PRAGMA foreign_key_check")

    anyio.run(run)


def test_optional_scope_and_explicit_readback_replay_conflict_and_tombstone(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (app, http, prep, client, fetcher):
            flow = await begin(http, "status downloads")
            page = await http.get(flow["location"])
            assert (
                'name="scope_downloads" value="true" aria-describedby="downloads-help"' in page.text
            )
            assert 'name="scope_downloads" value="true" checked' not in page.text
            tokens = await finish(http, flow, "status downloads")
            capabilities = await call(http, tokens["access_token"], "get_capabilities")
            assert capabilities["features"]["audio_preparation"]["ready"]
            assert not capabilities["features"]["description_playlist"]["ready"]
            response = await submit(http, tokens)
            assert response["ok"], response
            batch = response["batch"]
            assert batch["location"] == "server" and batch["total"] == 4
            assert batch["unique_songs"] == 3 and batch["state"] == "queued"
            assert not fetcher.calls
            client.entries = ["changed-snapshot"]
            client.foreign = True
            replay = await submit(http, tokens)
            assert replay["batch"]["id"] == batch["id"]
            conflict = await submit(http, tokens, playlist="other-native")
            assert conflict["error"]["code"] == "conflict"
            client.foreign = False
            client.entries = ["owned", "new", "new", "other"]
            await drain(prep)
            status = await call(
                http,
                tokens["access_token"],
                "get_audio_preparation",
                {
                    "batch_id": batch["id"],
                    "offset": 1,
                    "limit": 1,
                },
            )
            assert status["batch"]["state"] == "complete"
            assert status["batch"]["available"] == 4
            assert status["batch"]["items"][0]["song_id"] == "new"
            assert status["batch"]["has_more"]
            assert fetcher.calls == {"new": 1, "other": 1}
            assert "generation" not in json.dumps(status) and "native-secret" not in json.dumps(
                status
            )
            await prep.action("owner", "owner", batch["id"], "forget")
            removed = await submit(http, tokens)
            assert not removed["ok"]
            assert not await prep.repo.batches("owner")
            token = await app.oauth.verify_token(tokens["access_token"])
            assert await prep.repo.replay(token.claims["grant_id"], "prepare-native-1")

    anyio.run(run)


def test_playlist_disconnect_clears_credentials_and_pauses_personal_preparation(tmp_path: Path):
    async def run():
        async with world(tmp_path) as (_app, http, prep, _client, fetcher):
            # Connect the playlist account, then prepare without an agent creator.
            await finish(http, await begin(http, "status downloads"), "status downloads")
            ident = await prep.prepare("owner", "owner", "native-list")
            before = await prep.repo.batch(ident, "owner")
            await prep.repo.store.execute("DELETE FROM playlist_owners WHERE id='owner'")
            after = await prep.repo.batch(ident, "owner")
            assert after["creator"] is None
            assert after["snapshot"] == before["snapshot"]
            assert after["state"] == "paused"
            assert (await prep.repo.owner("owner"))["authorization"] is None
            assert await prep._claim() is None and not fetcher.calls
            with pytest.raises(PreparationError, match="Connect your Navidrome"):
                await prep.action("owner", "owner", ident, "resume")

    anyio.run(run)


def test_existing_grants_gain_no_downloads_and_missing_permission_challenges(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with world(tmp_path) as (_app, http, _prep, _client, _fetcher):
            flow = await begin(http, "create status library")
            tokens = await finish(http, flow, "create status library")
            row = await _app.oauth.store.fetchone(
                "SELECT data FROM agent_oauth_clients WHERE id=?", [tokens["client_id"]]
            )
            old = json.loads(row["data"])
            old["scope"] = "create status library history settings"
            await _app.oauth.store.execute(
                "UPDATE agent_oauth_clients SET data=? WHERE id=?",
                [json.dumps(old), tokens["client_id"]],
            )
            denied = await submit(http, tokens)
            assert denied["error"]["missing_permissions"] == ["downloads"]
            reply = await http.post(
                "/shijhon/mcp",
                headers={
                    "authorization": "Bearer " + tokens["access_token"],
                    "accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "prepare_playlist_audio",
                        "arguments": {
                            "playlist_id": "native-list",
                            "idempotency_key": "challenge-1",
                        },
                    },
                },
            )
            assert "downloads" in reply.json()["result"]["_meta"]["mcp/www_authenticate"][0]
            await begin(http, "status downloads", tokens["client_id"])
            assert not (await submit(http, tokens))["ok"]  # A flow is not consent.

    anyio.run(run)


@pytest.mark.parametrize("stage", ["queued", "preparing", "verifying"])
@pytest.mark.parametrize("change", ["narrow", "revoke"])
def test_permission_change_pauses_at_existing_worker_preflights(
    tmp_path: Path,
    stage: str,
    change: str,
) -> None:
    async def run() -> None:
        async with world(tmp_path) as (app, http, prep, client, fetcher):
            client.entries = ["owned", "new", "other"]
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")
            token = await app.oauth.verify_token(tokens["access_token"])
            creator = token.claims["grant_id"]
            response = await submit(http, tokens)
            ident = response["batch"]["id"]
            owned = await prep._claim()
            await prep._work(*owned)

            async def invalidate():
                if change == "narrow":
                    await prep.repo.store.execute(
                        "UPDATE playlist_agent_tokens SET scopes='[\"status\"]' WHERE id=?",
                        [creator],
                    )
                else:
                    await app.oauth.revoke_token(token)

            if stage == "queued":
                await invalidate()
                assert await prep._claim() is None
            elif stage == "preparing":
                work = await prep._claim()
                await invalidate()
                await prep._work(*work)
            else:
                fetcher.before_verify = invalidate
                work = await prep._claim()
                await prep._work(*work)
            batch = (await prep.repo.batches("owner"))[0]
            assert batch["state"] == "paused" and batch["available"] == 1
            assert not (tmp_path / "new").exists() and not (tmp_path / "other").exists()
            with pytest.raises(PreparationError, match="Agent authorization changed"):
                await prep.action("owner", "owner", ident, "resume")
            if change == "narrow":
                status = await call(
                    http, tokens["access_token"], "get_audio_preparation", {"batch_id": ident}
                )
                assert status["ok"] and status["batch"]["state"] == "paused"
                narrowed = await submit(http, tokens)
                assert narrowed["error"]["code"] == "permission_required"
                renewed = await finish(
                    http,
                    await begin(http, "status downloads", tokens["client_id"]),
                    "status downloads",
                )
                fetcher.before_verify = None
                result = await call(
                    http,
                    renewed["access_token"],
                    "manage_audio_preparation",
                    {
                        "batch_id": ident,
                        "action": "resume",
                    },
                )
                assert result["ok"], result
                await drain(prep)
                assert (await prep.repo.batch(ident, "owner"))["state"] == "complete"

    anyio.run(run)


def test_fresh_permission_after_playlist_await_and_isolated_job_reads(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (app, http, prep, client, _fetcher):
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")
            token = await app.oauth.verify_token(tokens["access_token"])
            creator = token.claims["grant_id"]
            original = client.playlist

            async def narrowed(*args):
                result = await original(*args)
                await prep.repo.store.execute(
                    "UPDATE playlist_agent_tokens SET scopes='[\"status\"]' WHERE id=?", [creator]
                )
                return result

            client.playlist = narrowed
            response = await submit(http, tokens)
            assert not response["ok"] and not await prep.repo.batches("owner")
            client.playlist = original
            renewed = await finish(
                http, await begin(http, "status downloads", tokens["client_id"]), "status downloads"
            )
            first = await submit(http, renewed)
            ident = first["batch"]["id"]
            other = await finish(http, await begin(http, "status downloads"), "status downloads")
            listing = await call(http, other["access_token"], "list_audio_preparations")
            assert listing["batches"] == []
            status = await call(
                http, other["access_token"], "get_audio_preparation", {"batch_id": ident}
            )
            assert not status["ok"]
            action = await call(
                http,
                other["access_token"],
                "manage_audio_preparation",
                {
                    "batch_id": ident,
                    "action": "cancel",
                },
            )
            assert not action["ok"]
            for args in ({"limit": 101}, {"offset": -1}):
                assert not (
                    await call(http, renewed["access_token"], "list_audio_preparations", args)
                )["ok"]

    anyio.run(run)


def test_manage_retry_cancel_restart_and_native_reauthorization(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (app, http, prep, client, fetcher):
            client.entries = ["new", "other"]
            tokens = await finish(http, await begin(http, "downloads"), "downloads")
            fetcher.fail.add("new")
            result = await submit(http, tokens)
            ident = result["batch"]["id"]
            await drain(prep)
            denied = await call(
                http, tokens["access_token"], "get_audio_preparation", {"batch_id": ident}
            )
            assert denied["error"]["missing_permissions"] == ["status"]
            fetcher.fail.clear()
            retried = await call(
                http,
                tokens["access_token"],
                "manage_audio_preparation",
                {
                    "batch_id": ident,
                    "action": "retry",
                },
            )
            assert retried["ok"]
            cancelled = await call(
                http,
                tokens["access_token"],
                "manage_audio_preparation",
                {
                    "batch_id": ident,
                    "action": "cancel",
                },
            )
            assert cancelled["batch"]["state"] == "cancelled"
            assert (tmp_path / "other").is_file()
            assert not (tmp_path / "new").exists()
            connection = await app.service.repo.connection("owner")

            await app.service.repo.connect(
                connection.model_copy(update={"token": SecretStr("new-native-token")})
            )
            resumed = await call(
                http,
                tokens["access_token"],
                "manage_audio_preparation",
                {
                    "batch_id": ident,
                    "action": "resume",
                },
            )
            assert resumed["ok"], resumed
            assert "new-native-token" in (await prep.repo.owner("owner"))["authorization"]
            await prep.reconcile()
            assert await prep._claim() is None
            resumed = await call(
                http,
                tokens["access_token"],
                "manage_audio_preparation",
                {
                    "batch_id": ident,
                    "action": "resume",
                },
            )
            assert resumed["ok"]
            await drain(prep)
            assert fetcher.calls == {"new": 2, "other": 1}
            assert (await prep.repo.batch(ident, "owner"))["state"] == "complete"

    anyio.run(run)


def test_explicit_disconnect_reports_setup_but_keeps_status_and_replay(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (_app, http, prep, client, _fetcher):
            client.entries = ["owned"]
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")
            first = await submit(http, tokens)
            ident = first["batch"]["id"]
            await drain(prep)
            await prep.disconnect("owner")
            caps = await call(http, tokens["access_token"], "get_capabilities")
            assert not caps["features"]["audio_preparation"]["configured"]
            assert caps["features"]["audio_preparation_status"]["ready"]
            assert (await submit(http, tokens))["batch"]["id"] == ident
            missing = await submit(http, tokens, key="different-native-job")
            assert missing["error"]["code"] == "setup_required"
            assert missing["error"]["setup_url"] == BASE + "/shijhon/downloads"
            assert (
                await call(
                    http, tokens["access_token"], "get_audio_preparation", {"batch_id": ident}
                )
            )["ok"]

    anyio.run(run)


def test_agent_preparation_takes_playlists_as_long_as_the_import_limit(tmp_path: Path) -> None:
    async def run() -> None:
        async with world(tmp_path) as (_app, http, prep, client, _fetcher):
            assert prep.playlists is not None
            prep.playlists.settings = prep.playlists.settings.model_copy(
                update={"import_max_entries": 2000}
            )
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")
            caps = await call(http, tokens["access_token"], "get_capabilities")
            assert caps["limits"]["audio_preparation_entries"] == 2000
            listed = await http.post(
                "/shijhon/mcp",
                headers={
                    "authorization": "Bearer " + tokens["access_token"],
                    "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            tool = next(
                t for t in listed.json()["result"]["tools"] if t["name"] == "prepare_playlist_audio"
            )
            assert "1000" not in tool["description"]
            assert "audio_preparation_entries" in tool["description"]
            client.entries = [f"song-{i}" for i in range(1500)]
            submitted = await submit(http, tokens)
            assert submitted["ok"] and submitted["batch"]["total"] == 1500
            ident = submitted["batch"]["id"]
            last = await call(
                http,
                tokens["access_token"],
                "get_audio_preparation",
                {"batch_id": ident, "offset": 1499, "limit": 1},
            )
            assert last["ok"] and last["batch"]["items"][0]["position"] == 1499
            assert not last["batch"]["has_more"]
            beyond = await call(
                http,
                tokens["access_token"],
                "get_audio_preparation",
                {"batch_id": ident, "offset": 2001, "limit": 1},
            )
            assert not beyond["ok"]

    anyio.run(run)


@pytest.mark.parametrize("operation", ["prepare", "cancel", "resume"])
def test_revocation_while_waiting_for_change_lock_blocks_mutation(
    tmp_path: Path, operation: str
) -> None:
    async def run() -> None:
        async with world(tmp_path) as (app, http, prep, _client, _fetcher):
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")
            first = await submit(http, tokens)
            ident = first["batch"]["id"]
            if operation == "resume":
                await prep.action("owner", "owner", ident, "cancel")
            before = await prep.repo.batch(ident, "owner")
            responses = []

            async def waiting():
                if operation == "prepare":
                    responses.append(await submit(http, tokens, key="another-preparation"))
                else:
                    responses.append(
                        await call(
                            http,
                            tokens["access_token"],
                            "manage_audio_preparation",
                            {
                                "batch_id": ident,
                                "action": operation,
                            },
                        )
                    )

            async with anyio.create_task_group() as group, prep.changes:
                group.start_soon(waiting)
                with anyio.fail_after(3):
                    while not prep.changes.statistics().tasks_waiting:  # noqa: ASYNC110
                        await anyio.sleep(0.005)
                token = await app.oauth.verify_token(tokens["access_token"])
                await app.oauth.revoke_token(token)
            assert responses and not responses[0]["ok"]
            assert await prep.repo.batch(ident, "owner") == before
            assert len(await prep.repo.batches("owner")) == 1

    anyio.run(run)


def test_lost_submission_response_and_concurrent_replays_do_not_duplicate_work(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with world(tmp_path) as (_app, http, prep, _client, _fetcher):
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")
            original = prep.repo.agent_status

            async def lost(*args, **kwargs):
                raise TimeoutError

            prep.repo.agent_status = lost
            response = await submit(http, tokens)
            assert not response["ok"]
            prep.repo.agent_status = original
            replies = []

            async def replay():
                replies.append(await submit(http, tokens))

            async with anyio.create_task_group() as group:
                group.start_soon(replay)
                group.start_soon(replay)
            assert replies[0]["batch"]["id"] == replies[1]["batch"]["id"]
            assert len(await prep.repo.batches("owner")) == 1
            for key in ("short", "contains spaces", "x" * 129):
                assert not (await submit(http, tokens, key=key))["ok"]

    anyio.run(run)


class KeptAttribution:
    """The recorder of accepted actions (``CollectionRequests``): keeps them in the queue,
    in the transaction it is given; counts its wake-ups."""

    def __init__(self, fail: bool = False) -> None:
        self.notified, self.fail = 0, fail

    async def pending(self, db: Any, owner: str, items: Any) -> None:
        if self.fail:
            raise RuntimeError("the queue could not be written")
        await queue_pending(db, owner, "1", items)

    def notify(self) -> None:
        self.notified += 1


def test_a_preparation_keeps_its_attribution_with_its_batch_and_a_replay_retries_it(
    tmp_path: Path,
) -> None:
    """Review finding S1: the songs to record go into the queue in the batch's own
    transaction - durable with it, never lost to a stop or a failure after it - and an agent
    replaying its request wakes the recorder again."""

    async def run() -> None:
        async with world(tmp_path) as (_app, http, prep, client, _fetcher):
            tokens = await finish(http, await begin(http, "status downloads"), "status downloads")

            async def recordings(items: dict[str, dict[str, Any]]) -> None:
                items["new"]["track_ref"] = "test:invented"

            prep._recordings = recordings  # type: ignore[method-assign]
            client.principal = AsyncMock(
                return_value=Principal("owner", "owner", False, frozenset({1}))
            )
            # A queue that cannot be written fails the preparation, with no batch kept.
            prep.attribution = KeptAttribution(fail=True)
            refused = await submit(http, tokens, key="prepare-refused-1")
            assert "batch" not in refused
            store = prep.repo.store
            assert not await store.fetchone("SELECT 1 FROM audio_batches")
            kept = KeptAttribution()
            prep.attribution = kept
            first = await submit(http, tokens)
            rows = await store.fetchall("SELECT owner_id,param,item FROM collection_pending")
            assert [tuple(r) for r in rows] == [("owner", "songId", "new")]
            assert kept.notified == 1
            again = await submit(http, tokens)
            assert again["batch"]["id"] == first["batch"]["id"]
            assert kept.notified == 2

    anyio.run(run)
