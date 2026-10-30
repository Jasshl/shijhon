"""Regression checks for dashboard request admission and bounded error responses."""

from __future__ import annotations

import functools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import anyio.lowlevel
import httpx
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from shijhon.access import AccessDenied, AccessService, AccessUnavailable, Principal
from shijhon.app import ShijhonApp
from shijhon.dashboard import app as dashboard_app
from shijhon.dashboard.app import HEADERS, SESSION_COOKIE, SIGNIN_COOKIE, Refused
from shijhon.dashboard.auth import Login
from shijhon.dashboard.collection import read_form as collection_form
from shijhon.dashboard.playlist_imports import read_form as import_form
from shijhon.dashboard.status import check_catalog
from shijhon.delivery.sources import SourceRegistry
from shijhon.store import Store
from tests.unit.test_dashboard import _settings

pytestmark = pytest.mark.anyio
PRINCIPAL = Principal("owner", "Listener", True, frozenset({1}))


@pytest.fixture
async def dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = ShijhonApp(_settings(tmp_path))
    store = await Store.open(tmp_path / "dashboard.db")
    sources = SourceRegistry(store)
    app.services = SimpleNamespace(
        navidrome=SimpleNamespace(library_id=1),
        playlists=None,
        collection_refresh=None,
        sources=sources,
    )
    dash = app.dashboard
    dash.attach(store, app.configured, {})
    dash._render = AsyncMock(return_value=PlainTextResponse("Dashboard", headers=HEADERS))
    monkeypatch.setattr(AccessService, "personal", AsyncMock(return_value=PRINCIPAL))
    token, session = await dash.sessions.create("Listener", "owner")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(dash),
        base_url="http://test",
        cookies={SESSION_COOKIE: token},
    ) as client:
        yield dash, client, session, token
    await dash.aclose()
    await sources.aclose()
    await store.close()


async def test_sign_out_during_native_outage_checks_only_cookie_and_form(dashboard, monkeypatch):
    dash, client, session, token = dashboard
    proof = AsyncMock(side_effect=AccessUnavailable)
    monkeypatch.setattr(AccessService, "personal", proof)
    for data, headers in (
        ({}, {}),
        ({"csrf": session.csrf}, {"sec-fetch-site": "cross-site"}),
    ):
        refused = await client.post("/shijhon/sign-out", data=data, headers=headers)
        assert refused.status_code == 403
        assert await dash.sessions.get(token) is not None
    response = await client.post("/shijhon/sign-out", data={"csrf": session.csrf})
    assert response.status_code == 303
    assert await dash.sessions.get(token) is None
    assert "Max-Age=0" in response.headers["set-cookie"]
    proof.assert_not_called()


async def test_reauthentication_ends_the_replaced_session_only_after_success(
    dashboard, monkeypatch
):
    dash, client, _session, previous = dashboard
    signin = "invented-signin-token"
    client.cookies.set(SIGNIN_COOKIE, signin)
    login = AsyncMock(return_value=Login("wrong"))
    monkeypatch.setattr(dashboard_app, "navidrome_login", login)
    fields = {"signin": signin, "username": "Listener", "password": "invented"}
    await client.post("/shijhon/sign-in", data=fields)
    assert await dash.sessions.get(previous) is not None
    login.return_value = Login("ok", "Listener", True, "owner")
    response = await client.post("/shijhon/sign-in", data=fields)
    assert response.status_code == 303
    assert await dash.sessions.get(previous) is None
    assert await dash.sessions.get(response.cookies[SESSION_COOKIE]) is not None


async def test_subresource_identity_cache_is_short_per_session_and_never_used_for_forms(
    dashboard,
    monkeypatch,
):
    dash, client, session, token = dashboard
    proof = AsyncMock(return_value=PRINCIPAL)
    monkeypatch.setattr(AccessService, "personal", proof)
    # The real collection routes opt in; no other route does.
    for path in ("source-progress", "progress/missing", "artwork/missing"):
        await client.get("/shijhon/collection/" + path)
    assert proof.await_count == 1
    dash._verified[session.token_hash] = (0, PRINCIPAL)
    await client.get("/shijhon/collection/artwork/missing")
    assert proof.await_count == 2
    await client.get("/shijhon/appearance")
    await client.post("/shijhon/appearance", data={"csrf": session.csrf, "accent": "green"})
    assert proof.await_count == 4
    # A different session gets its own proof, even for the same owner.
    other, _ = await dash.sessions.create("Listener", "owner")
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, other)
    await client.get("/shijhon/collection/source-progress")
    assert proof.await_count == 5
    proof.side_effect = AccessDenied
    await client.get("/shijhon/appearance")
    assert await dash.sessions.get(token) is None
    assert await dash.sessions.get(other) is None
    count = proof.await_count
    await client.get("/shijhon/collection/source-progress")
    assert proof.await_count == count


@pytest.mark.parametrize("reader", [collection_form, import_form])
@pytest.mark.parametrize("case,code", [("large", 413), ("kind", 415), ("unreadable", 400)])
async def test_refused_forms_keep_dashboard_security_headers(reader, case, code):
    body = {"large": b"x" * (7 * 1024 * 1024), "kind": b"{}", "unreadable": b"csrf=%FF"}[case]
    if case == "unreadable":
        body = b"\xff"
    kind = "application/json" if case == "kind" else "application/x-www-form-urlencoded"

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "headers": [(b"content-type", kind.encode())]}, receive
    )
    with pytest.raises(Refused) as refused:
        await (import_form(request, max_bytes=1024) if reader is import_form else reader(request))
    response = refused.value.response
    assert response.status_code == code
    for name, value in HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("secret-address"), KeyError("secret"), TypeError("secret")]
)
async def test_adapter_failures_become_a_safe_catalog_notice(error):
    result = await check_catalog(SimpleNamespace(check=AsyncMock(side_effect=error)))
    assert not result.ok and result.error == type(error).__name__


async def test_concurrent_dashboard_adds_check_names_in_the_same_transaction(
    dashboard, monkeypatch
):
    dash, _client, session, _token = dashboard
    monkeypatch.setattr(
        dashboard_app, "fetch_manifest", AsyncMock(return_value=SimpleNamespace(name="Synthetic"))
    )
    dash.checks.learn = lambda *args: None
    dash.addons_page = AsyncMock(return_value=PlainTextResponse("Already present", 400))
    request = Request({"type": "http", "method": "POST"})
    request.state.form = {"manifest": "https://addon.example.invalid/manifest.json"}
    responses = []

    async def add():
        responses.append(await dash.addon_add(request, session))

    async with anyio.create_task_group() as group:
        group.start_soon(add)
        group.start_soon(add)
    assert sorted(r.status_code for r in responses) == [303, 400]
    assert len(await dash.services.sources.stored()) == 1
    assert await dash.saved.addons_owner() is not None


@pytest.mark.parametrize("action", ["add", "enable", "enable_many", "reorder", "update", "remove"])
async def test_addon_mutations_roll_back_with_the_ownership_marker(dashboard, action):
    dash, _client, session, _token = dashboard
    sources = dash.services.sources
    one = await sources.add("One", "https://one.example.invalid")
    two = await sources.add("Two", "https://two.example.invalid")
    before = await sources.stored()

    async def stopped(conn):
        await dash._owned(session, conn)
        raise RuntimeError("stopped before commit")

    operations = {
        "add": functools.partial(sources.add, "Three", "https://three.example.invalid"),
        "enable": functools.partial(sources.set_enabled, one, False),
        "enable_many": functools.partial(sources.set_enabled_many, {one: False, two: False}),
        "reorder": functools.partial(sources.reorder, [two, one]),
        "update": functools.partial(sources.update, one, settings={"quality": "6"}),
        "remove": functools.partial(sources.remove, one),
    }
    with pytest.raises(RuntimeError, match="stopped"):
        await operations[action](also=stopped)
    assert await sources.stored() == before
    assert await dash.saved.addons_owner() is None


async def test_concurrent_collection_covers_share_one_native_verification(dashboard, monkeypatch):
    _dash, client, _session, _token = dashboard

    async def verify(*args, **kwargs):
        await anyio.lowlevel.checkpoint()
        return PRINCIPAL

    proof = AsyncMock(side_effect=verify)
    monkeypatch.setattr(AccessService, "personal", proof)

    async def cover(number):
        response = await client.get(f"/shijhon/collection/artwork/cover-{number}")
        assert response.status_code == 404

    async with anyio.create_task_group() as group:
        for number in range(40):
            group.start_soon(cover, number)
    proof.assert_awaited_once()
