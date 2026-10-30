"""Refresh retry grace preserves credentials without persisting replay secrets."""

from __future__ import annotations

import json
import time
from pathlib import Path

import anyio
import pytest
from mcp.server.auth.provider import TokenError

from shijhon.agents import oauth
from shijhon.agents.oauth import REFRESH_REPLAY_SECONDS, OAuth
from shijhon.playlists.requests import digest
from tests.unit.test_agents import BASE, ISSUER, RESOURCE, app_at, begin, finish


def refresh_form(tokens):
    return {
        "grant_type": "refresh_token",
        "client_id": tokens["client_id"],
        "refresh_token": tokens["refresh_token"],
        "resource": RESOURCE,
    }


def test_concurrent_http_refresh_and_retry_do_not_extend_window(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            replies = []

            async def refresh():
                replies.append(await http.post(ISSUER + "/token", data=refresh_form(tokens)))

            async with anyio.create_task_group() as group:
                group.start_soon(refresh)
                group.start_soon(refresh)
            assert len(replies) == 2
            assert all(reply.status_code == 200 for reply in replies)
            assert replies[0].json() == replies[1].json()
            rotated = replies[0].json()
            ident = digest(tokens["refresh_token"])
            deadline, pair = app.oauth._refresh_replays[ident]
            used = await app.oauth.store.fetchone(
                "SELECT used FROM agent_oauth_tokens WHERE id=?", [ident]
            )
            retry = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert retry.status_code == 200 and retry.json() == rotated
            assert app.oauth._refresh_replays[ident] == (deadline, pair)
            assert dict(used) == dict(
                await app.oauth.store.fetchone(
                    "SELECT used FROM agent_oauth_tokens WHERE id=?", [ident]
                )
            )
            rows = await app.oauth.store.fetchall("SELECT * FROM agent_oauth_tokens")
            persisted = json.dumps([dict(row) for row in rows])
            assert rotated["access_token"] not in persisted
            assert rotated["refresh_token"] not in persisted
            assert await app.oauth.verify_token(rotated["access_token"])

    anyio.run(run)


@pytest.mark.parametrize("preloaded", [False, True])
@pytest.mark.parametrize("reduced_scopes", [False, True])
def test_refresh_reuse_after_window_commits_revocation(
    tmp_path: Path, preloaded: bool, reduced_scopes: bool
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            client = await app.oauth.get_client(tokens["client_id"])
            credential = await app.oauth.load_refresh_token(client, tokens["refresh_token"])
            replacement = await app.oauth.exchange_refresh_token(
                client, credential, credential.scopes
            )
            # Keep the memory entry: the persisted rotation deadline must also apply.
            await app.oauth.store.execute(
                "UPDATE agent_oauth_tokens SET used=? WHERE id=?",
                [time.time() - REFRESH_REPLAY_SECONDS, digest(tokens["refresh_token"])],
            )
            if reduced_scopes:
                await app.oauth.store.execute(
                    "UPDATE playlist_agent_tokens SET scopes=?", [json.dumps(["status"])]
                )
            if preloaded:
                with pytest.raises(TokenError) as error:
                    await app.oauth.exchange_refresh_token(client, credential, credential.scopes)
                assert error.value.error == "invalid_grant"
            else:
                reply = await http.post(ISSUER + "/token", data=refresh_form(tokens))
                assert reply.status_code == 400 and reply.json()["error"] == "invalid_grant"
            grant = await app.oauth.store.fetchone("SELECT revoked FROM playlist_agent_tokens")
            assert grant["revoked"]
            assert await app.oauth.verify_token(replacement.access_token) is None
            successor = await app.oauth.load_refresh_token(client, replacement.refresh_token)
            with pytest.raises(TokenError):
                await app.oauth.exchange_refresh_token(client, successor, successor.scopes)

    anyio.run(run)


def test_restart_loses_replay_and_reuse_revokes(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            reply = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert reply.status_code == 200
            restarted = OAuth(app.service, BASE)
            client = await restarted.get_client(tokens["client_id"])
            credential = await restarted.load_refresh_token(client, tokens["refresh_token"])
            assert credential is None
            assert await app.oauth.verify_token(reply.json()["access_token"]) is None

    anyio.run(run)


@pytest.mark.parametrize("during_validation", [False, True])
def test_revoked_connection_cannot_replay(tmp_path: Path, during_validation: bool) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            first = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert first.status_code == 200

            async def revoke(*args):
                await app.oauth.store.execute(
                    "UPDATE playlist_agent_tokens SET revoked=?", [time.time()]
                )

            if during_validation:
                app.service.client.validate = revoke
            else:
                await revoke()
            retry = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert retry.status_code == 400 and retry.json()["error"] == "invalid_grant"
            assert await app.oauth.verify_token(first.json()["access_token"]) is None

    anyio.run(run)


def test_replay_checks_current_client_and_preserves_scopes(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            form = {**refresh_form(tokens), "scope": "status"}
            reply = await http.post(ISSUER + "/token", data=form)
            assert reply.status_code == 200 and reply.json()["scope"] == "status"
            wrong_scopes = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert wrong_scopes.status_code == 400
            assert wrong_scopes.json()["error"] == "invalid_scope"
            assert await app.oauth.verify_token(reply.json()["access_token"])
            other = await begin(http)
            wrong_client = await http.post(
                ISSUER + "/token", data={**form, "client_id": other["client_id"]}
            )
            assert wrong_client.status_code == 400
            retry = await http.post(ISSUER + "/token", data=form)
            assert retry.status_code == 200 and retry.json() == reply.json()

            client = await app.oauth.get_client(tokens["client_id"])
            credential = await app.oauth.load_refresh_token(client, tokens["refresh_token"])
            await app.oauth.store.execute(
                "DELETE FROM agent_oauth_clients WHERE id=?", [client.client_id]
            )
            with pytest.raises(TokenError) as error:
                await app.oauth.exchange_refresh_token(client, credential, ["status"])
            assert error.value.error == "invalid_grant"

    anyio.run(run)


def test_replay_checks_current_connection_scopes(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            first = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert first.status_code == 200
            await app.oauth.store.execute(
                "UPDATE playlist_agent_tokens SET scopes=?", [json.dumps(["status"])]
            )
            retry = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert retry.status_code == 400 and retry.json()["error"] == "invalid_grant"
            narrower = await http.post(
                ISSUER + "/token", data={**refresh_form(tokens), "scope": "status"}
            )
            assert narrower.status_code == 400
            assert narrower.json()["error"] == "invalid_scope"
            current = await app.oauth.verify_token(first.json()["access_token"])
            assert current and current.scopes == ["status"]

    anyio.run(run)


def test_plaintext_replay_is_discarded_without_more_requests(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "create status library")
            monkeypatch.setattr(oauth, "REFRESH_REPLAY_SECONDS", 0.02)
            reply = await http.post(ISSUER + "/token", data=refresh_form(tokens))
            assert reply.status_code == 200
            await anyio.sleep(0.04)
            assert not app.oauth._refresh_replays

    anyio.run(run)


@pytest.mark.parametrize("preloaded", [False, True])
def test_spent_refresh_with_wider_scope_still_revokes(tmp_path: Path, preloaded: bool) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            tokens = await finish(http, await begin(http), "status")
            client = await app.oauth.get_client(tokens["client_id"])
            credential = await app.oauth.load_refresh_token(client, tokens["refresh_token"])
            pair = await app.oauth.exchange_refresh_token(client, credential, ["status"])
            await app.oauth.store.execute(
                "UPDATE agent_oauth_tokens SET used=? WHERE id=?",
                [time.time() - REFRESH_REPLAY_SECONDS, digest(tokens["refresh_token"])],
            )
            if preloaded:
                with pytest.raises(TokenError) as error:
                    await app.oauth.exchange_refresh_token(client, credential, ["status", "create"])
                assert error.value.error == "invalid_grant"
            else:
                response = await http.post(
                    ISSUER + "/token", data={**refresh_form(tokens), "scope": "status create"}
                )
                assert response.status_code == 400
                assert response.json()["error"] == "invalid_grant"
            grant = await app.oauth.store.fetchone("SELECT revoked FROM playlist_agent_tokens")
            assert grant["revoked"]
            assert await app.oauth.verify_token(pair.access_token) is None

    anyio.run(run)


def test_stale_retry_after_successor_rotates_refuses_without_revoking(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            original = await finish(http, await begin(http), "create status library")
            first = await http.post(ISSUER + "/token", data=refresh_form(original))
            assert first.status_code == 200
            next_tokens = {**first.json(), "client_id": original["client_id"]}
            second = await http.post(ISSUER + "/token", data=refresh_form(next_tokens))
            assert second.status_code == 200
            retry = await http.post(ISSUER + "/token", data=refresh_form(original))
            assert retry.status_code == 400 and retry.json()["error"] == "invalid_grant"
            grant = await app.oauth.store.fetchone("SELECT revoked FROM playlist_agent_tokens")
            assert not grant["revoked"]
            assert await app.oauth.verify_token(second.json()["access_token"])
            # The immediate predecessor can still recover the live pair.
            retry_current = await http.post(ISSUER + "/token", data=refresh_form(next_tokens))
            assert retry_current.status_code == 200
            assert retry_current.json() == second.json()

    anyio.run(run)
