"""MCP audio preparation against disposable native owners and synthetic recordings."""

from __future__ import annotations

from typing import Any

import anyio
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.acceptance.test_R_playlist_requests import world as world
from tests.harness.agents import connect_agent
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER


def test_mcp_native_only_preparation_replays_and_reads_verified_availability(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    tokens = connect_agent(
        browser.http, w.server.base_url, ADMIN_USER, ADMIN_PASSWORD, "status downloads"
    )
    prep = w.services.downloads
    assert prep
    owned = w.nd.native("GET", "song").json()[0]

    async def create() -> tuple[str, str]:
        login = await prep.client.login(ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
        # OAuth's native/library connection is sufficient; no Downloads-page setup.
        auth = await prep.connection(login["id"], ADMIN_USER)
        reply = await prep.client.call(
            auth,
            "createPlaylist",
            [
                ("name", "Native MCP preparation"),
                ("songId", owned["id"]),
                ("songId", owned["id"]),
            ],
        )
        return login["id"], reply["playlist"]["id"]

    owner, playlist = w.server.call(create)

    async def run() -> dict[str, Any]:
        async with (
            httpx2.AsyncClient(
                headers={"authorization": "Bearer " + tokens["access_token"]}
            ) as http,
            streamable_http_client(w.server.base_url + "/shijhon/mcp", http_client=http) as (
                read,
                write,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names >= {
                "prepare_playlist_audio",
                "get_audio_preparation",
                "list_audio_preparations",
                "manage_audio_preparation",
            }
            caps = (await session.call_tool("get_capabilities")).structured_content
            assert caps["features"]["audio_preparation"]["ready"]
            assert not caps["features"]["description_playlist"]["ready"]
            args = {"playlist_id": playlist, "idempotency_key": "real-native-audio-1"}
            first = await session.call_tool("prepare_playlist_audio", args)
            assert not first.is_error, first.content
            ident = first.structured_content["batch"]["id"]
            replay = await session.call_tool("prepare_playlist_audio", args)
            assert replay.structured_content["batch"]["id"] == ident
            with anyio.fail_after(15):
                while True:
                    status = await session.call_tool("get_audio_preparation", {"batch_id": ident})
                    assert not status.is_error, status.content
                    if status.structured_content["batch"]["state"] == "complete":
                        break
                    await anyio.sleep(0.05)
            batch = status.structured_content["batch"]
            assert batch["total"] == batch["available"] == 2 and batch["unique_songs"] == 1
            listing = await session.call_tool("list_audio_preparations")
            assert [b["id"] for b in listing.structured_content["batches"]] == [ident]
            conflict = await session.call_tool(
                "prepare_playlist_audio",
                {
                    **args,
                    "playlist_id": "different-native-input",
                },
            )
            assert conflict.structured_content["error"]["code"] == "conflict"
            return batch

    result = anyio.run(run)

    async def verify() -> None:
        auth = await prep.connection(owner, ADMIN_USER)
        current = await prep.client.playlist(auth, playlist)
        assert [song["id"] for song in current["entry"]] == [owned["id"], owned["id"]]
        assert result["location"] == "server"
        assert w.services.download_first.fetches == 0

    w.server.call(verify)


@pytest.mark.parametrize("stage", ["queued", "forgotten"])
def test_account_disconnect_removes_agent_audio_and_revokes_credentials(
    world: tuple[DeliveryWorld, Browser], monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    w, browser = world
    tokens = connect_agent(
        browser.http, w.server.base_url, ADMIN_USER, ADMIN_PASSWORD, "status downloads"
    )
    prep, playlists = w.services.downloads, w.services.playlists
    assert prep and playlists and playlists.oauth
    oauth = playlists.oauth
    if stage == "queued":
        # Pause only worker scheduling; credentials, MCP, SQLite and native reads are real.
        async def no_claim():
            return None

        monkeypatch.setattr(prep, "_claim", no_claim)
    owned = w.nd.native("GET", "song").json()[0]["id"]
    playlist = w.nd.client().ok(
        "createPlaylist", {"name": "Disconnect audio " + stage, "songId": [owned, owned]}
    )["playlist"]["id"]

    async def submit_and_wait() -> tuple[str, str, str]:
        token = await oauth.verify_token(tokens["access_token"])
        assert token
        saved = await prep.repo.owner(token.subject)
        if saved and not saved["authorization"]:
            # A previous disconnect deliberately leaves a reconnect tombstone.
            await prep.connect(token.subject, ADMIN_USER, ADMIN_PASSWORD, "127.0.0.1")
        async with (
            httpx2.AsyncClient(
                headers={"authorization": "Bearer " + tokens["access_token"]}
            ) as http,
            streamable_http_client(w.server.base_url + "/shijhon/mcp", http_client=http) as (
                read,
                write,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "prepare_playlist_audio",
                {"playlist_id": playlist, "idempotency_key": "disconnect-audio-" + stage},
            )
            assert not result.is_error, result.content
            batch = result.structured_content["batch"]["id"]
            if stage == "forgotten":
                with anyio.fail_after(15):
                    while True:
                        if (await prep.repo.batch(batch, token.subject))["state"] == "complete":
                            break
                        await anyio.sleep(0.05)
                await prep.action(token.subject, ADMIN_USER, batch, "forget")
            else:
                assert (await prep.repo.batch(batch, token.subject))["state"] == "queued"
            return token.subject, token.claims["grant_id"], batch

    owner, grant, batch = w.server.call(submit_and_wait)

    async def before_disconnect() -> None:
        row = await prep.repo.replay(grant, "disconnect-audio-" + stage)
        assert row and row["batch"] == (None if stage == "forgotten" else batch)
        assert (await prep.repo.owner(owner))["authorization"]

    w.server.call(before_disconnect)
    assert browser.post("playlists/forget", {"csrf": browser.csrf()}).status_code == 303

    async def verify_removed() -> None:
        store = prep.repo.store
        assert not await playlists.repo.connection(owner)
        assert (await prep.repo.owner(owner))["authorization"] is None
        assert not await prep.repo.replay(grant, "disconnect-audio-" + stage)
        assert not await store.fetchone("SELECT 1 FROM audio_batches WHERE id=?", [batch])
        assert not await store.fetchone("SELECT 1 FROM audio_items WHERE batch=?", [batch])
        assert not await store.fetchone("SELECT 1 FROM playlist_agent_tokens WHERE id=?", [grant])
        assert not await store.fetchone(
            "SELECT 1 FROM agent_oauth_tokens WHERE grant_id=?", [grant]
        )
        assert not await oauth.verify_token(tokens["access_token"])
        assert not await store.fetchall("PRAGMA foreign_key_check")

    w.server.call(verify_removed)
    denied = browser.http.post(
        "/shijhon/mcp", headers={"authorization": "Bearer " + tokens["access_token"]}
    )
    assert denied.status_code == 401
    refreshed = browser.http.post(
        "/shijhon/agents/token",
        data={
            "grant_type": "refresh_token",
            "client_id": tokens["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": w.server.base_url + "/shijhon/mcp",
        },
    )
    assert refreshed.status_code == 400 and refreshed.json()["error"] == "invalid_grant"
