"""OAuth boundaries and partial configuration, with synthetic accounts and music."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urljoin, urlsplit

import anyio
import httpx
import pytest
from jinja2 import Environment, PackageLoader
from mcp.server.auth.provider import TokenError
from pydantic import SecretStr

from shijhon.agents.app import Agents
from shijhon.agents.oauth import SCOPES, OAuth
from shijhon.config import AgentSettings
from shijhon.playlists import edits as owner_edits
from shijhon.playlists.model import Definition, Track
from shijhon.playlists.requests import RequestError
from shijhon.proxy.forwarding import Forwarding
from tests.harness.agents import callback_target
from tests.unit.test_playlist_requests import service_at

BASE = "https://test.invalid"
ISSUER = BASE + "/shijhon/agents"
RESOURCE = BASE + "/shijhon/mcp"


@asynccontextmanager
async def app_at(path: Path):
    async with service_at(path, ai=False) as service:
        service.client.login = AsyncMock(
            return_value={
                "id": "owner",
                "username": "owner",
                "subsonicToken": "native-secret",
                "subsonicSalt": "salt",
            }
        )
        host = SimpleNamespace(
            forwarding=Forwarding(),
            dashboard=SimpleNamespace(
                env=Environment(
                    loader=PackageLoader("shijhon.dashboard", "templates"), autoescape=True
                )
            ),
        )
        app = Agents(host, service, BASE)
        async with anyio.create_task_group() as group:
            group.start_soon(app.run)
            await app.ready.wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=BASE) as http:
                yield app, http
            group.cancel_scope.cancel()


async def begin(
    http: httpx.AsyncClient,
    scopes: str = "create status library",
    client_id: str = "",
) -> dict[str, Any]:
    if not client_id:
        reply = await http.post(
            ISSUER + "/register",
            json={
                "redirect_uris": ["http://127.0.0.1/callback"],
                "token_endpoint_auth_method": "none",
                "client_name": "Synthetic client",
                "scope": scopes,
            },
        )
        assert reply.status_code == 201, reply.text
        client_id = reply.json()["client_id"]
    client = client_id
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    params = {
        "client_id": client,
        "response_type": "code",
        "redirect_uri": "http://127.0.0.1/callback",
        "resource": RESOURCE,
        "state": "state-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": scopes,
    }
    authorized = await http.get(ISSUER + "/authorize", params=params)
    assert authorized.status_code == 302, authorized.text
    location = authorized.headers["location"]
    page = await http.get(location)
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf
    return {
        "client_id": client,
        "verifier": verifier,
        "location": location,
        "csrf": csrf[1],
        "params": params,
    }


async def finish(http: httpx.AsyncClient, flow: dict[str, Any], scopes: str) -> dict[str, Any]:
    approved = await http.post(
        flow["location"],
        data={
            "username": "owner",
            "password": "synthetic-password",
            "csrf": flow["csrf"],
            "action": "allow",
            **{"scope_" + s: "true" for s in scopes.split()},
        },
    )
    query = parse_qs(urlsplit(callback_target(approved)).query)
    assert query["state"] == ["state-123"] and query["iss"] == [ISSUER]
    exchange = {
        "grant_type": "authorization_code",
        "client_id": flow["client_id"],
        "code": query["code"][0],
        "code_verifier": flow["verifier"],
        "redirect_uri": "http://127.0.0.1/callback",
        "resource": RESOURCE,
    }
    tokens = await http.post(ISSUER + "/token", data=exchange)
    assert tokens.status_code == 200, tokens.text
    return {**tokens.json(), "exchange": exchange, "client_id": flow["client_id"]}


async def call(http: httpx.AsyncClient, token: str, tool: str, args: dict[str, Any] | None = None):
    reply = await http.post(
        "/shijhon/mcp",
        headers={
            "authorization": "Bearer " + token,
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        },
    )
    assert reply.status_code == 200, reply.text
    data = reply.json()
    assert "result" in data, data
    return data["result"]["structuredContent"]


@pytest.mark.parametrize(
    "value",
    [
        "http://remote.invalid",
        "https://u:p@host.invalid",
        "https://host.invalid/?key=x",
        "https://host.invalid/#x",
        "https://host.invalid/music",
    ],
)
def test_public_identity_refuses_insecure_or_credential_urls(value: str) -> None:
    with pytest.raises(ValueError):
        AgentSettings(public_url=value)


def test_browser_form_origin_and_private_callback_handoff(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            flow = await begin(http)
            page = await http.get(flow["location"])
            assert page.headers["referrer-policy"] == "same-origin"
            assert "form-action 'self';" in page.headers["content-security-policy"]
            form = {"csrf": flow["csrf"], "action": "deny"}
            for origin in ("null", "https://else.invalid"):
                refused = await http.post(flow["location"], data=form, headers={"origin": origin})
                assert refused.status_code == 403
            response = await http.post(flow["location"], data=form, headers={"origin": BASE})
            # Every page of the connection has the dashboard's look: its stylesheet is
            # found from the page's own address (the error page and the handoff page too).
            for shown in (page, refused, response):
                sheet = re.search(r'href="([^"]*shijhon\.css)"', shown.text)
                assert sheet
                found = await http.get(urljoin(str(shown.url), sheet[1]))
                assert found.status_code == 200
                assert found.headers["content-type"].startswith("text/css")
            target = urlsplit(callback_target(response))
            assert target.scheme == "http" and target.netloc == "127.0.0.1"
            assert parse_qs(target.query) == {
                "error": ["access_denied"],
                "iss": [ISSUER],
                "state": ["state-123"],
            }
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["cache-control"] == "no-store"
            assert "form-action 'self';" in response.headers["content-security-policy"]
            assert "<form" not in response.text and "<input" not in response.text
            assert "Max-Age=0" in response.headers["set-cookie"]
            app.service.client.login.assert_not_awaited()

    anyio.run(run)


def test_discovery_consent_pkce_resource_and_legacy_key_boundaries(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            denied = await http.post("/shijhon/mcp", json={})
            assert denied.status_code == 401
            assert (
                "/.well-known/oauth-protected-resource/shijhon/mcp"
                in denied.headers["www-authenticate"]
            )
            metadata = await http.get("/.well-known/oauth-authorization-server/shijhon/agents")
            assert metadata.json()["issuer"] == ISSUER
            assert metadata.json()["authorization_response_iss_parameter_supported"] is True
            assert metadata.json()["code_challenge_methods_supported"] == ["S256"]
            resource = await http.get("/.well-known/oauth-protected-resource/shijhon/mcp")
            assert set(resource.json()["scopes_supported"]) == set(SCOPES)
            flow = await begin(http)
            bad = await http.post(flow["location"], data={"action": "allow"})
            assert bad.status_code == 403
            app.service.client.login.assert_not_awaited()
            assert (
                await http.get(
                    ISSUER + "/authorize",
                    params={**flow["params"], "resource": "https://else.invalid/mcp"},
                )
            ).status_code == 400
            assert (
                await http.get(
                    ISSUER + "/authorize",
                    params={**flow["params"], "redirect_uri": "http://127.0.0.1/other"},
                )
            ).status_code == 400
            token = await finish(http, flow, "create status library")
            replay = await http.post(ISSUER + "/token", data=token["exchange"])
            assert replay.status_code == 400
            wrong_resource = await http.post(
                ISSUER + "/token", data={**token["exchange"], "resource": "https://else.invalid"}
            )
            assert wrong_resource.status_code == 400
            legacy = await app.service.requests.mint(
                "owner", "Old key", history=False, library=False
            )
            denied = await http.post(
                "/shijhon/mcp", headers={"authorization": "Bearer " + legacy}, json={}
            )
            assert denied.status_code == 401
            code_row = await app.oauth.store.fetchone("SELECT count(*) AS n FROM agent_oauth_codes")
            assert code_row["n"] == 0
            rows = await app.oauth.store.fetchall("SELECT id FROM agent_oauth_tokens")
            assert token["access_token"] not in json.dumps([dict(r) for r in rows])

    anyio.run(run)


def test_missing_ai_does_not_block_reads_or_ordered_creation_and_refresh_keeps_owner(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            flow = await begin(http)
            tokens = await finish(http, flow, "create status library")
            raw = tokens["access_token"]
            capabilities = await call(http, raw, "get_capabilities")
            assert capabilities["features"]["library"]["ready"]
            assert capabilities["features"]["agent_curated_playlist"]["ready"]
            assert not capabilities["features"]["description_playlist"]["ready"]
            assert not capabilities["features"]["listening_history"]["configured"]
            assert "native-secret" not in json.dumps(capabilities)
            app.service.client.call = AsyncMock(
                return_value={
                    "searchResult3": {
                        "song": [
                            {"id": "song", "title": "First Lantern", "path": "/private/audio.flac"}
                        ]
                    }
                }
            )
            library = await call(http, raw, "search_library")
            assert library["tracks"] == [{"id": "song", "title": "First Lantern"}]
            missing = await call(
                http,
                raw,
                "create_playlist",
                {"idempotency_key": "description-test", "request": {"prompt": "quiet music"}},
            )
            assert missing["error"]["code"] == "setup_required"
            assert missing["error"]["missing_configuration"] == ["server_ai"]
            payload = {
                "idempotency_key": "ordered-test",
                "request": {
                    "name": "Lanterns",
                    "count": 1,
                    "tracks": [{"artist": "Glass Orchard", "title": "First Lantern"}],
                },
            }
            created = await call(http, raw, "create_playlist", payload)
            assert created["ok"], created
            ident = created["request"]["id"]
            refresh = {
                "grant_type": "refresh_token",
                "client_id": tokens["client_id"],
                "refresh_token": tokens["refresh_token"],
                "resource": RESOURCE,
            }
            reply = await http.post(ISSUER + "/token", data=refresh)
            assert reply.status_code == 200, reply.text
            new = reply.json()["access_token"]
            status = await call(http, new, "get_playlist_request", {"request_id": ident})
            assert status["request"]["id"] == ident
            replay = await call(http, new, "create_playlist", payload)
            assert replay["request"]["id"] == ident and not replay["created"]
            assert await app.oauth.verify_token(raw) is None
            # Reusing a rotated refresh token ends that connection, including its new token.
            assert (await http.post(ISSUER + "/token", data=refresh)).status_code == 400
            assert await app.oauth.verify_token(new) is None

    anyio.run(run)


def test_scope_reduction_and_revocation_during_native_validation(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http, " ".join(SCOPES)), " ".join(SCOPES))
            refresh = {
                "grant_type": "refresh_token",
                "client_id": tokens["client_id"],
                "refresh_token": tokens["refresh_token"],
                "resource": RESOURCE,
            }
            reply = await http.post(ISSUER + "/token", data={**refresh, "scope": "status"})
            assert reply.status_code == 200
            raw = reply.json()["access_token"]
            denied = await call(http, raw, "search_library")
            assert denied["error"]["code"] == "permission_required"
            app.service.client.call.assert_not_called() if isinstance(
                app.service.client.call, AsyncMock
            ) else None
            token = await app.oauth.verify_token(raw)
            assert token and token.claims

            async def revoke(connection) -> None:
                await app.oauth.store.execute(
                    "UPDATE playlist_agent_tokens SET revoked=? WHERE id=?",
                    [time.time(), token.claims["grant_id"]],
                )

            app.service.client.validate = revoke
            assert await app.oauth.verify_token(raw) is None

    anyio.run(run)


def test_preloaded_refresh_replay_revokes_replacement_without_rollback(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            client = await app.oauth.get_client(tokens["client_id"])
            first = await app.oauth.load_refresh_token(client, tokens["refresh_token"])
            concurrent = await app.oauth.load_refresh_token(client, tokens["refresh_token"])
            assert first and concurrent
            replacement = await app.oauth.exchange_refresh_token(client, first, first.scopes)
            assert await app.oauth.verify_token(replacement.access_token)
            with pytest.raises(TokenError) as exc:
                await app.oauth.exchange_refresh_token(client, concurrent, concurrent.scopes)
            assert exc.value.error == "invalid_grant"
            assert await app.oauth.verify_token(replacement.access_token) is None

    anyio.run(run)


def test_idempotent_replay_recovers_after_ai_is_removed(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            connection = await app.service.repo.connection("owner")
            await app.service.repo.connect(
                connection.model_copy(
                    update={
                        "backend": "api",
                        "endpoint": "https://model.invalid/v1",
                        "model": "synthetic",
                    }
                )
            )
            args = {"idempotency_key": "lost-description", "request": {"prompt": "Quiet music"}}
            first = await call(http, tokens["access_token"], "create_playlist", args)
            assert first["created"]
            await app.service.repo.connect(connection)
            recovered = await call(http, tokens["access_token"], "create_playlist", args)
            assert recovered["ok"] and not recovered["created"]
            assert recovered["request"]["id"] == first["request"]["id"]

    anyio.run(run)


def test_new_agent_connection_defaults_to_shared_ai_even_for_admin(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            await app.service.repo.disconnect("owner")
            tokens = await finish(http, await begin(http), "create status library")
            assert tokens["access_token"]
            saved = await app.service.connection("owner")
            assert saved.use_shared_ai and saved.backend == "none"

    anyio.run(run)


def test_mcp_settings_only_change_personal_approved_model(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            scopes = "create status settings"
            tokens = await finish(http, await begin(http, scopes), scopes)
            token = tokens["access_token"]
            capabilities = await call(http, token, "get_capabilities")
            choices = capabilities["features"]["connection_choices"]
            assert choices["permitted"] and not choices["configured"] and not choices["ready"]
            missing = await call(
                http, token, "configure_playlist_connection", {"choices": {"model": "synthetic-v2"}}
            )
            assert missing["error"]["code"] == "setup_required"
            connection = await app.service.repo.connection("owner")
            assert connection
            connection = connection.model_copy(
                update={
                    "backend": "api",
                    "endpoint": "https://model.invalid/v1",
                    "model": "synthetic",
                    "history_user": "synthetic-listener",
                    "history_key": SecretStr("synthetic-history-key"),
                }
            )
            await app.service.configure_shared_ai(connection, allowed_models=["synthetic-v2"])
            await app.service.repo.connect(connection.model_copy(update={"use_shared_ai": True}))
            connection = await app.service.connection("owner")
            host = await app.service.repo.shared_ai()
            store = app.service.repo.store
            await store.execute(
                "INSERT INTO playlist_listens(owner,at,artist,title,album) VALUES (?,?,?,?,?)",
                ["owner", 123, "Glass Orchard", "First Lantern", "Small Lights"],
            )
            mix = await app.service.repo.create("owner", Definition(name="Existing weekly mix"))
            await store.execute("UPDATE playlist_mixes SET enabled=1 WHERE id=?", [mix])
            listens = [dict(row) for row in await store.fetchall("SELECT * FROM playlist_listens")]
            mixes = [dict(row) for row in await store.fetchall("SELECT * FROM playlist_mixes")]
            assert mixes[0]["enabled"] == 1
            unpermitted = await finish(http, await begin(http, "create status"), "create status")
            denied = await call(
                http,
                unpermitted["access_token"],
                "configure_playlist_connection",
                {"choices": {"model": "synthetic-v2"}},
            )
            assert denied["error"]["code"] == "permission_required"
            changed = await call(
                http, token, "configure_playlist_connection", {"choices": {"model": "synthetic-v2"}}
            )
            assert changed["updated"] == ["model"]
            assert changed["capabilities"]["features"]["connection_choices"]["ready"]
            assert await app.service.repo.connection("owner") == connection
            assert (await app.service.repo.ai_preferences("owner")).model == "synthetic-v2"
            assert await app.service.repo.shared_ai() == host

            async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
                response = await http.post(
                    "/shijhon/mcp",
                    headers={
                        "authorization": "Bearer " + token,
                        "accept": "application/json, text/event-stream",
                        "mcp-protocol-version": "2025-11-25",
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                assert response.status_code == 200
                return response.json()["result"]

            tools = (await rpc("tools/list", {}))["tools"]
            model_tool = next(t for t in tools if t["name"] == "configure_playlist_connection")
            assert "history_user" not in json.dumps(model_tool["inputSchema"])
            for values in ({}, {"model": "synthetic-v3", "history_user": "another-listener"}):
                rejected = await rpc(
                    "tools/call",
                    {"name": "configure_playlist_connection", "arguments": {"choices": values}},
                )
                assert rejected["isError"]
            assert await app.service.repo.connection("owner") == connection
            assert (await app.service.repo.ai_preferences("owner")).model == "synthetic-v2"
            assert await app.service.repo.shared_ai() == host
            reset = await call(
                http, token, "configure_playlist_connection", {"choices": {"model": ""}}
            )
            assert reset["ok"] and (await app.service.repo.ai_preferences("owner")).model == ""
            assert [
                dict(r) for r in await store.fetchall("SELECT * FROM playlist_listens")
            ] == listens
            assert [dict(r) for r in await store.fetchall("SELECT * FROM playlist_mixes")] == mixes

    anyio.run(run)


def test_additional_consent_preserves_requests_and_permission_removal_is_effective(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            first = await finish(http, await begin(http, "create status"), "create status")
            original = await app.oauth.verify_token(first["access_token"])
            args = {
                "idempotency_key": "reauth-playlist",
                "request": {
                    "name": "Lantern",
                    "count": 1,
                    "tracks": [{"artist": "Glass Orchard", "title": "First Lantern"}],
                },
            }
            created = await call(http, first["access_token"], "create_playlist", args)
            assert created["ok"]
            expanded = "create status library"
            second = await finish(
                http,
                await begin(http, expanded, first["client_id"]),
                expanded,
            )
            current = await app.oauth.verify_token(second["access_token"])
            assert current.claims["grant_id"] == original.claims["grant_id"]
            status = await call(
                http,
                second["access_token"],
                "get_playlist_request",
                {
                    "request_id": created["request"]["id"],
                },
            )
            assert status["request"]["id"] == created["request"]["id"]
            # Permission removal must affect outstanding credentials as soon as
            # consent changes, including before an old credential expires.
            await app.oauth.store.execute(
                "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
                [
                    json.dumps(["create", "status"]),
                    current.claims["grant_id"],
                ],
            )
            reduced = await app.oauth.verify_token(second["access_token"])
            assert "library" not in reduced.scopes
            denied = await call(http, second["access_token"], "search_library")
            assert denied["error"]["missing_permissions"] == ["library"]
            await app.oauth.store.execute(
                "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
                [json.dumps(["status"]), current.claims["grant_id"]],
            )
            readable = await call(
                http,
                second["access_token"],
                "get_playlist_request",
                {
                    "request_id": created["request"]["id"],
                },
            )
            assert readable["ok"] and readable["request"]["id"] == created["request"]["id"]
            count = await app.oauth.store.fetchone(
                "SELECT count(*) AS n FROM playlist_agent_tokens"
            )
            assert count["n"] == 1

    anyio.run(run)


def test_browser_preflight_registration_recovery_and_pending_resource_binding(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            preflight = await http.options(
                ISSUER + "/token",
                headers={
                    "origin": "https://client.invalid",
                    "access-control-request-method": "POST",
                    "access-control-request-headers": "authorization,content-type",
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == "*"
            flow = await begin(http)
            raw_flow = parse_qs(urlsplit(flow["location"]).query)["flow"][0]
            other = OAuth(app.service, "https://changed.invalid")
            with pytest.raises(RequestError, match="address changed"):
                await other.approve(raw_flow, "owner", ["create"])
            client = await app.oauth.get_client(flow["client_id"])
            # A client with a pending flow is preserved while abandoned anonymous
            # registrations can be replaced, so the storage bound cannot be exhausted forever.
            async with app.oauth.store.transaction() as db:
                for i in range(511):
                    data = client.model_copy(update={"client_id": f"abandoned-{i}"})
                    await db.execute(
                        "INSERT INTO agent_oauth_clients VALUES (?,?,?)",
                        [data.client_id, data.model_dump_json(), 1],
                    )
            await app.oauth.register_client(client.model_copy(update={"client_id": "replacement"}))
            assert await app.oauth.get_client("replacement")
            assert await app.oauth.get_client(flow["client_id"])
            count = await app.oauth.store.fetchone("SELECT count(*) AS n FROM agent_oauth_clients")
            assert count["n"] == 512

    anyio.run(run)


def test_an_agent_forgets_learned_edits_of_its_own_request_with_create_permission(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http, "create status"), "create status")
            token = tokens["access_token"]
            created = await call(
                http,
                token,
                "create_playlist",
                {
                    "idempotency_key": "forget-edits",
                    "request": {
                        "name": "Lantern",
                        "count": 1,
                        "tracks": [{"artist": "Glass Orchard", "title": "First Lantern"}],
                    },
                },
            )
            ident = created["request"]["id"]
            store = app.service.repo.store
            songs = [
                Track(id=f"s-{i}", artist="Glass Orchard", title=f"Lantern {i}") for i in range(3)
            ]
            await owner_edits.remember(store, ident, "run-1", songs[:2], songs[2:])

            async def remembered() -> int:
                row = await store.fetchone("SELECT count(*) AS n FROM playlist_edits")
                return int(row["n"])

            forget = {"request_id": ident, "action": "forget_edits"}
            # Another connection of the same account has no access to this request.
            other = await finish(http, await begin(http, "create status"), "create status")
            denied = await call(http, other["access_token"], "manage_playlist_refresh", forget)
            assert denied["error"]["code"] == "not_found" and await remembered() == 3
            # It is a change, so read-only permission is not enough.
            grant = (await app.oauth.verify_token(token)).claims["grant_id"]
            scopes = "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?"
            await app.oauth.store.execute(scopes, [json.dumps(["status"]), grant])
            denied = await call(http, token, "manage_playlist_refresh", forget)
            assert denied["error"]["code"] == "permission_required" and await remembered() == 3
            await app.oauth.store.execute(scopes, [json.dumps(["create", "status"]), grant])
            # With it, no history or library permission and no AI are needed.
            done = await call(http, token, "manage_playlist_refresh", forget)
            assert done["ok"] and done["forgotten"] == 3 and done["request"]["id"] == ident
            assert await remembered() == 0
            again = await call(http, token, "manage_playlist_refresh", forget)
            assert again["ok"] and again["forgotten"] == 0

    anyio.run(run)


@pytest.mark.parametrize("action", ["status", "pause"])
def test_revocation_while_waiting_for_the_change_lock_is_rechecked(
    tmp_path: Path, action: str
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            token = await app.oauth.verify_token(tokens["access_token"])
            args = {
                "idempotency_key": "lock-revoke",
                "request": {
                    "name": "Lantern",
                    "count": 1,
                    "tracks": [{"artist": "Glass Orchard", "title": "First Lantern"}],
                },
            }
            created = await call(http, tokens["access_token"], "create_playlist", args)
            assert created["ok"]
            entered, release = anyio.Event(), anyio.Event()

            class Gate:
                async def __aenter__(self):
                    entered.set()
                    await release.wait()

                async def __aexit__(self, *exc):
                    return False

            app.service.changes = Gate()
            result = {}

            async def waiting() -> None:
                arguments = {"request_id": created["request"]["id"]}
                tool = "get_playlist_request"
                if action == "pause":
                    tool = "manage_playlist_refresh"
                    arguments["action"] = action
                result.update(await call(http, tokens["access_token"], tool, arguments))

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(waiting)
                await entered.wait()
                await app.oauth.store.execute(
                    "UPDATE playlist_agent_tokens SET revoked=? WHERE id=?",
                    [time.time(), token.claims["grant_id"]],
                )
                release.set()
            assert result["error"]["code"] == "authentication_required"

    anyio.run(run)
