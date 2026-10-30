"""Playlist and agent access regressions, using invented owners/music and local HTTP transports."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

from shijhon.dashboard.auth import Session
from shijhon.dashboard.playlist_requests import Pages
from shijhon.playlists.api import API
from shijhon.playlists.model import PlaylistError, UpstreamUnavailable
from shijhon.playlists.requests import PlaylistRequest, RequestError, digest
from tests.harness.agents import agent_token
from tests.unit.test_agents import ISSUER, RESOURCE, app_at, begin, call, finish
from tests.unit.test_playlist_requests import connected, service_at


@pytest.mark.parametrize("reference", ["reference_playlists", "reference_songs"])
def test_json_references_require_submitting_token_library_scope(
    tmp_path: Path, reference: str
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            raw = await agent_token(service, "owner", "Invented agent", history=False, library=True)
            await service.repo.store.execute(
                "UPDATE agent_oauth_tokens SET scopes=? WHERE id=?",
                [json.dumps(["create", "status"]), digest(raw)],
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(Starlette(routes=API(lambda: service).routes())),
                base_url="https://test.invalid",
            ) as http:
                reply = await http.post(
                    "/api/playlists/requests",
                    headers={
                        "authorization": "Bearer " + raw,
                        "idempotency-key": "scoped-reference",
                    },
                    json={"prompt": "Like my Driftwood playlist", reference: ["native-reference"]},
                )
                assert reply.status_code == 403
                assert not await service.repo.store.fetchone("SELECT id FROM playlist_requests")

    anyio.run(run)


def test_background_requests_keep_token_ceiling_and_current_grant(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            raw = await service.requests.mint("owner", "Invented agent", history=True, library=True)
            creator = digest(raw)
            ident, _ = await service.requests.create(
                "owner",
                "scope-ceiling",
                PlaylistRequest(prompt="Like my Driftwood playlist"),
                creator=creator,
                scopes=["create", "status"],
            )
            assert not await service.requests.reads_library(ident)
            with pytest.raises(RequestError, match="separate"):
                await service.requests.create(
                    "owner",
                    "scope-context",
                    PlaylistRequest(prompt="quiet", use_library=True),
                    creator=creator,
                    scopes=["create", "status"],
                )
            wide, _ = await service.requests.create(
                "owner",
                "scope-wide",
                PlaylistRequest(prompt="quiet", use_library=True),
                creator=creator,
                scopes=["create", "status", "library"],
            )
            await service.repo.store.execute(
                "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
                ['["create","status"]', creator],
            )
            assert not await service.requests.reads_library(wide)
            with pytest.raises(RequestError, match="separate"):
                await service.requests.guard(await service.repo.mix(wide, "owner"))

    anyio.run(run)


@pytest.mark.parametrize("changed", [False, True])
def test_shared_ai_history_import_checks_stored_profile(tmp_path: Path, changed: bool) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            owner = connected(
                history_user="invented-history", history_key="synthetic-key", use_shared_ai=True
            )
            await service.repo.connect(owner)
            await service.repo.save_shared_ai(
                connected(backend="api", endpoint="https://model.invalid/v1", model="synthetic")
            )
            effective = await service.connection("owner")
            now = int(time.time())
            await service.repo.store.execute(
                "INSERT INTO playlist_listens VALUES (?,?,?,?,?)",
                [
                    "owner",
                    now - (service.settings.history_days + 1) * 86400,
                    "Old Arbor",
                    "Old Lantern",
                    "",
                ],
            )

            async def ask(*args: Any, **kw: Any) -> dict[str, Any]:
                if changed:  # a setting, not only fresh credentials (same_settings)
                    await service.repo.connect(
                        owner.model_copy(update={"history_user": "changed-history"})
                    )
                return {
                    "recenttracks": {
                        "track": [
                            {
                                "artist": "Glass Orchard",
                                "name": "First Lantern",
                                "date": {"uts": now - 10},
                            }
                        ],
                        "@attr": {"page": 1, "totalPages": 1, "total": 1, "perPage": 200},
                    }
                }

            service.history.ask = ask
            if changed:
                with pytest.raises(PlaylistError, match="changed during import"):
                    await service.history.sync(effective)
            else:
                listens, _ = await service.history.sync(effective)
                assert [s["title"] for s in listens] == ["First Lantern", "Old Lantern"]
                # Retaining older evidence does not make it part of a declared window.
                assert len(await service.repo.store.fetchall("SELECT * FROM playlist_listens")) == 2

    anyio.run(run)


@pytest.mark.parametrize("action", ["pause", "discard", "publish", "enable", "retry", "refresh"])
def test_owner_can_stop_revoked_agent_requests_but_cannot_start_work(
    tmp_path: Path, action: str
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            raw = await service.requests.mint(
                "owner", "Invented agent", history=False, library=False
            )
            ident, _ = await service.requests.create(
                "owner",
                "revoked-preview",
                PlaylistRequest(prompt="quiet", preview=True),
                creator=digest(raw),
                scopes=["create", "status"],
            )
            job = (await service.repo.runs(ident))[0]
            await service.repo.update(job["id"], "preview", {})
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET enabled=1 WHERE id=?", [ident]
            )
            await service.requests.revoke("owner", digest(raw))
            pages = Pages(
                SimpleNamespace(
                    services=SimpleNamespace(playlists=service), _see=lambda _: Response()
                )
            )
            pages.render = AsyncMock(return_value=Response())
            request = Request({"type": "http", "method": "POST", "path_params": {"ident": ident}})
            request.state.form = {"action": action, "run": job["id"]}
            await pages.detail(request, Session("hash", "owner", "csrf", 0, "owner"))
            if action in ("pause", "discard"):
                pages.render.assert_not_awaited()
                if action == "pause":
                    assert not (await service.repo.mix(ident, "owner"))["enabled"]
                else:
                    assert (await service.repo.run(job["id"]))["state"] == "discarded"
            else:
                assert "agent connection" in pages.render.call_args.kwargs["error"]
                assert (await service.repo.run(job["id"]))["state"] == "preview"

    anyio.run(run)


def test_step_up_consent_preselects_existing_and_requested_permissions(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            first = await begin(http, "create status library history")
            await finish(http, first, "create status library history")
            flow = await begin(http, "settings", first["client_id"])
            consent = await http.post(
                flow["location"],
                data={
                    "csrf": flow["csrf"],
                    "action": "allow",
                    "username": "owner",
                    "password": "synthetic-password",
                },
            )
            assert consent.status_code == 200
            for scope in ("create", "status", "library", "history", "settings"):
                assert re.search(r'name="scope_' + scope + r'"[^>]* checked', consent.text)
            assert "Administrators only" not in consent.text
            assert "administrator-approved models" in consent.text
            assert 'class="signin-footer"' in consent.text and "AGPL" in consent.text
            raw = await app.oauth.flow(httpx.URL(flow["location"]).params["flow"])
            assert raw["owner"] == "owner"
            # Explicitly removing a permission is still supported.
            await app.oauth.approve(
                httpx.URL(flow["location"]).params["flow"],
                "owner",
                ["create", "status", "settings"],
            )
            grant = await app.oauth.store.fetchone(
                "SELECT scopes FROM playlist_agent_tokens WHERE oauth_client=?",
                [first["client_id"]],
            )
            assert set(json.loads(grant["scopes"])) == {"create", "status", "settings"}

    anyio.run(run)


def test_native_outage_keeps_refresh_credential_and_returns_retryable_errors(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            flow = await begin(http)
            tokens = await finish(http, flow, "create status library")
            original = app.service.client.validate
            app.service.client.validate = AsyncMock(
                side_effect=UpstreamUnavailable("Navidrome is temporarily unavailable.")
            )
            failed = await http.post(
                ISSUER + "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": tokens["client_id"],
                    "refresh_token": tokens["refresh_token"],
                    "resource": RESOURCE,
                },
            )
            assert failed.status_code == 503 and failed.json()["error"] == "temporarily_unavailable"
            denied = await http.post(
                "/shijhon/mcp",
                headers={"authorization": "Bearer " + tokens["access_token"]},
                json={},
            )
            assert denied.status_code == 503 and denied.json()["error"] == "temporarily_unavailable"
            assert "www-authenticate" not in denied.headers
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(Starlette(routes=API(lambda: app.service).routes())),
                base_url="https://test.invalid",
            ) as api:
                reply = await api.get(
                    "/api/playlists/requests/unknown",
                    headers={"authorization": "Bearer " + tokens["access_token"]},
                )
                assert reply.status_code == 503
            app.service.client.validate = original
            recovered = await http.post(
                ISSUER + "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": tokens["client_id"],
                    "refresh_token": tokens["refresh_token"],
                    "resource": RESOURCE,
                },
            )
            assert recovered.status_code == 200
            app.service.client.validate = AsyncMock(
                side_effect=PlaylistError("The connected owner cannot access this library.")
            )
            revoked = await http.post(
                ISSUER + "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": tokens["client_id"],
                    "refresh_token": recovered.json()["refresh_token"],
                    "resource": RESOURCE,
                },
            )
            assert revoked.status_code == 400 and revoked.json()["error"] == "invalid_grant"

    anyio.run(run)


def test_explicit_tracks_validate_normalized_artist_limit_before_enqueue() -> None:
    with pytest.raises(ValidationError, match="max_per_artist"):
        PlaylistRequest(
            count=2,
            max_per_artist=1,
            tracks=[
                {"artist": "Glass Orchard", "title": "First Lantern"},
                {"artist": "GLASS ORCHARD", "title": "Paper Sky"},
            ],
        )
    assert (
        PlaylistRequest(
            count=2,
            max_per_artist=2,
            tracks=[
                {"artist": "Glass Orchard", "title": "First Lantern"},
                {"artist": "GLASS ORCHARD", "title": "Paper Sky"},
            ],
        ).count
        == 2
    )


def test_agent_history_counts_only_the_reported_window(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            owner = connected(history_user="invented-history", history_key="synthetic-key")
            await app.service.repo.connect(owner)
            now = int(time.time())
            days = app.service.settings.history_days
            await app.oauth.store.execute(
                "INSERT INTO playlist_listens VALUES (?,?,?,?,?)",
                ["owner", now - (days + 1) * 86400, "Old Arbor", "Old Lantern", ""],
            )
            app.service.history.ask = AsyncMock(
                return_value={
                    "recenttracks": {
                        "track": [
                            {
                                "artist": "Glass Orchard",
                                "name": "First Lantern",
                                "date": {"uts": now - 10},
                            }
                        ],
                        "@attr": {"page": 1, "totalPages": 1, "total": 1, "perPage": 200},
                    }
                }
            )
            flow = await begin(http, "status history")
            tokens = await finish(http, flow, "status history")
            result = await call(
                http, tokens["access_token"], "get_listening_history", {"limit": 30}
            )
            assert result["ok"] and result["window_days"] == days
            assert result["history"] == [
                {"artist": "Glass Orchard", "title": "First Lantern", "listens": 1}
            ]
            assert len(await app.oauth.store.fetchall("SELECT * FROM playlist_listens")) == 2

    anyio.run(run)


@pytest.mark.parametrize("failure", ["network", "http503", "http429", "malformed", "revoked"])
def test_owner_client_distinguishes_upstream_failure_from_native_rejection(
    tmp_path: Path, failure: str
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:

            async def respond(request: httpx.Request) -> httpx.Response:
                if failure == "network":
                    raise httpx.ConnectError("invented private address", request=request)
                if failure.startswith("http"):
                    return httpx.Response(int(failure[4:]), text="private upstream body")
                if failure == "malformed":
                    return httpx.Response(200, text="not JSON")
                return httpx.Response(
                    200,
                    json={
                        "subsonic-response": {
                            "status": "failed",
                            "error": {"code": 40, "message": "wrong credentials"},
                        }
                    },
                )

            await service.client.http.aclose()
            service.client.native.base_url = "http://native.invalid"
            service.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            expected = PlaylistError if failure == "revoked" else UpstreamUnavailable
            with pytest.raises(expected) as caught:
                await service.client.call(connected(), "getUser")
            if failure == "revoked":
                assert not isinstance(caught.value, UpstreamUnavailable)
            assert "private" not in str(caught.value)

    anyio.run(run)


def test_migration_captures_existing_request_grants_and_keeps_pending_signin(
    tmp_path: Path,
) -> None:
    import sqlite3

    from shijhon.store.db import _bundled_migrations, apply_migrations

    with sqlite3.connect(tmp_path / "migration.sqlite") as db:
        apply_migrations(db, _bundled_migrations()[:24])
        db.execute(
            "INSERT INTO playlist_owners(id,username,connection,generation)"
            " VALUES (?,?,?,'original')",
            ["owner", "owner", connected().stored()],
        )
        db.execute(
            "INSERT INTO playlist_agent_tokens(id,owner,library,name,scopes,created)"
            " VALUES (?,?,?,?,?,0)",
            ["agent", "owner", "1", "Invented agent", '["create","status","library"]'],
        )
        db.execute("INSERT INTO playlist_mixes(id,owner,definition) VALUES ('mix','owner','{}')")
        db.execute(
            "INSERT INTO playlist_runs(id,mix,created,state) VALUES ('run','mix',0,'queued')"
        )
        db.execute(
            "INSERT INTO playlist_requests"
            " VALUES ('mix','owner','agent','1','old-key','hash','{}','run')"
        )
        db.execute("INSERT INTO agent_oauth_clients VALUES ('client','{}',0)")
        db.execute("INSERT INTO agent_oauth_flows VALUES ('flow','client','{}','csrf',9999999999)")
        db.commit()
        apply_migrations(db)
        assert (
            db.execute("SELECT scopes FROM playlist_requests WHERE id='mix'").fetchone()[0]
            == '["create","status","library"]'
        )
        assert db.execute(
            "SELECT owner,csrf FROM agent_oauth_flows WHERE id='flow'"
        ).fetchone() == ("", "csrf")


def test_stale_history_connection_cannot_import_into_a_new_profile(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            previous = connected(history_user="first-invented-profile", history_key="synthetic-key")
            await service.repo.connect(previous)
            await service.repo.connect(
                previous.model_copy(update={"history_user": "second-invented-profile"})
            )
            service.history.ask = AsyncMock(side_effect=AssertionError("No stale profile reads"))
            with pytest.raises(PlaylistError, match="connection changed"):
                await service.history.sync(previous)
            service.history.ask.assert_not_awaited()
            assert not await service.repo.store.fetchall("SELECT * FROM playlist_listens")

    anyio.run(run)
