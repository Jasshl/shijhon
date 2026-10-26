"""Official MCP client over a real socket, backed by disposable native accounts."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from shijhon.playlists.model import Connection
from shijhon.playlists.prompt import Ordered, Proposals
from tests.acceptance.test_R_playlist_requests import world as world
from tests.acceptance.test_R_playlists import RELEASE, SONGS, wait_state
from tests.harness.agents import callback_target, connect_agent
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER


def test_official_client_discovers_oauth_and_completes_browser_consent(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, _ = world

    class Storage:
        tokens: OAuthToken | None = None
        client: OAuthClientInformationFull | None = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client

        async def set_client_info(self, client):
            self.client = client

    async def run() -> None:
        storage = Storage()
        callback: AuthorizationCodeResult | None = None

        async def redirect(url: str) -> None:
            nonlocal callback
            async with httpx.AsyncClient() as browser:
                authorized = await browser.get(url)
                assert authorized.status_code == 302, authorized.text
                location = authorized.headers["location"]
                page = await browser.get(location)
                csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
                assert csrf
                response = await browser.post(
                    location,
                    data={
                        "username": ADMIN_USER,
                        "password": ADMIN_PASSWORD,
                        "csrf": csrf[1],
                        "action": "allow",
                        "scope_create": "true",
                        "scope_status": "true",
                    },
                )
                query = parse_qs(urlsplit(callback_target(response)).query)
                callback = AuthorizationCodeResult(
                    **{key: values[0] for key, values in query.items()}
                )

        async def receive() -> AuthorizationCodeResult:
            assert callback
            return callback

        url = w.server.base_url + "/shijhon/mcp"
        auth = OAuthClientProvider(
            server_url=url,
            client_metadata=OAuthClientMetadata(
                client_name="Official SDK client",
                redirect_uris=["http://127.0.0.1/callback"],
                token_endpoint_auth_method="none",
            ),
            storage=storage,
            redirect_handler=redirect,
            callback_handler=receive,
        )
        async with (
            httpx2.AsyncClient(auth=auth) as http,
            streamable_http_client(
                url,
                http_client=http,
            ) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            info = await session.call_tool("get_capabilities")
            assert not info.is_error
            assert storage.tokens and storage.tokens.refresh_token
            assert info.structured_content["features"]["agent_curated_playlist"]["ready"]

    anyio.run(run)


def test_real_mcp_client_uses_partial_setup_and_publishes_exact_recordings(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    tokens = connect_agent(
        browser.http, w.server.base_url, ADMIN_USER, ADMIN_PASSWORD, "create status library"
    )

    async def create() -> dict[str, Any]:
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
            listing = await session.list_tools()
            assert "get_capabilities" in {tool.name for tool in listing.tools}
            info = await session.call_tool("get_capabilities")
            assert info.structured_content["features"]["library"]["ready"]
            assert not info.structured_content["features"]["description_playlist"]["ready"]
            library = await session.call_tool("search_library", {"query": "Still Water"})
            assert {s["title"] for s in library.structured_content["tracks"]} >= {
                "Still Water",
                "Still Water (Live)",
            }
            result = await session.call_tool(
                "create_playlist",
                {
                    "idempotency_key": "real-mcp-order",
                    "request": {
                        "name": "MCP lanterns",
                        "count": 2,
                        "tracks": [
                            {
                                "artist": "Inkwell & Reed",
                                "title": "Still Water",
                                "version": "Live",
                            },
                            {"artist": "Inkwell & Reed", "title": "Still Water"},
                        ],
                    },
                },
            )
            assert not result.is_error, result.content
            return result.structured_content["request"]

    request = anyio.run(create)
    finished = wait_state(w, request["run"], ("published", "failed", "uncertain"))
    assert finished["state"] == "published", finished["error"]
    assert [s["title"] for s in finished["data"]["selected"]] == [
        "Still Water (Live)",
        "Still Water",
    ]


def test_recurring_description_runs_after_the_mcp_client_disconnects(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    response = browser.submit(
        "playlists",
        {"backend": "api", "endpoint": "https://model.invalid/v1", "model": "synthetic"},
        "playlists/connection",
    )
    assert response.status_code == 303

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        data = json.loads(prompt)
        if schema is Proposals:
            return {
                "tracks": [
                    {"artist": s.artist, "title": s.title, "album": RELEASE.title, "version": ""}
                    for s in SONGS
                ]
            }
        assert schema is Ordered
        return {
            "goal": "Fits the description",
            "constraints_satisfied": True,
            "selections": [
                {"id": c["id"], "reason": "Fits", "fit": "meets"}
                for c in data["candidates"][: data["count"]]
            ],
        }

    async def install() -> None:
        service.curator.api = ai

    w.server.call(install)
    tokens = connect_agent(browser.http, w.server.base_url, ADMIN_USER, ADMIN_PASSWORD)

    async def submit() -> dict[str, Any]:
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
                "create_playlist",
                {
                    "idempotency_key": "real-mcp-weekly",
                    "request": {
                        "name": "Weekly lanterns",
                        "prompt": "Quiet synthetic music",
                        "count": 2,
                        "save_instructions": True,
                        "cadence": "weekly",
                    },
                },
            )
            assert not result.is_error, result.content
            return result.structured_content["request"]

    request = anyio.run(submit)
    first = wait_state(w, request["run"], ("published", "failed", "uncertain"))
    assert first["state"] == "published", first["error"]
    native_id = first["data"]["created_id"]

    async def refresh() -> list[dict[str, Any]]:
        await service.repo.store.execute(
            "UPDATE playlist_mixes SET due=? WHERE id=?", [time.time() - 1, request["id"]]
        )
        await service.tick()
        return await service.repo.runs(request["id"])

    runs = w.server.call(refresh)
    assert len(runs) == 2
    assert runs[0]["state"] == "published", runs[0]["error"]
    mix = w.server.call(
        lambda: service.repo.store.fetchone(
            "SELECT playlist_id FROM playlist_mixes WHERE id=?", [request["id"]]
        )
    )
    assert mix["playlist_id"] == native_id
