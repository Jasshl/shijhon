"""Upload admission must finish before any bytes reach a handler."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import MethodType

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from shijhon.dashboard.app import Dashboard, Refused
from shijhon.dashboard.auth import Session

pytestmark = pytest.mark.anyio


def application(failure=None, *, require_admin=False):
    dashboard = object.__new__(Dashboard)
    events = []

    async def session(self, request):
        events.append("session-current-access")
        if failure is not None:
            raise Refused(failure)
        return Session("session", "Listener", "synthetic-csrf", 9999999999, "immutable-owner")

    dashboard._session = MethodType(session, dashboard)

    async def receive(request: Request, owner: Session):
        events.append("handler")
        body = await request.body()
        return JSONResponse({"owner": owner.user_id, "bytes": len(body)})

    app = Starlette(
        routes=[
            Route(
                "/upload",
                dashboard._personal_api(receive, require_admin=require_admin),
                methods=["GET", "POST"],
            )
        ]
    )
    return app, events


async def payload(events: list[str]) -> AsyncIterator[bytes]:
    events.append("body-consumed")
    yield b"synthetic-audio"


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("x-csrf-token", "wrong")],
        [("x-csrf-token", "synthetic-csrf"), ("x-csrf-token", "synthetic-csrf")],
        [("x-csrf-token", "synthetic-csrf"), ("origin", "https://other.invalid")],
        [("x-csrf-token", "synthetic-csrf"), ("origin", "null")],
        [("x-csrf-token", "synthetic-csrf"), ("origin", "https://music.invalid/path")],
        [("x-csrf-token", "synthetic-csrf"), ("sec-fetch-site", "cross-site")],
        [
            ("x-csrf-token", "synthetic-csrf"),
            ("origin", "https://music.invalid"),
            ("origin", "https://other.invalid"),
        ],
    ],
)
async def test_rejected_admission_consumes_no_upload_bytes(headers):
    app, events = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://music.invalid"
    ) as client:
        response = await client.post("/upload", headers=headers, content=payload(events))
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert events == ["session-current-access"]


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (RedirectResponse("/shijhon/sign-in", 303), 401, "sign_in_required"),
        (RedirectResponse("/shijhon/sign-in?reason=access", 303), 403, "access_changed"),
        (HTMLResponse("private-native-diagnostic", 503), 503, "access_unavailable"),
    ],
)
async def test_session_failure_is_json_and_never_reads_the_body(failure, status, code):
    app, events = application(failure)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://music.invalid"
    ) as client:
        response = await client.post(
            "/upload", headers={"x-csrf-token": "synthetic-csrf"}, content=payload(events)
        )
    assert response.status_code == status
    assert response.json()["error"] == code
    assert "location" not in response.headers
    assert "private-native-diagnostic" not in response.text
    assert events == ["session-current-access"]


@pytest.mark.parametrize("origin", [None, "https://music.invalid", "https://music.invalid:443"])
async def test_accepted_binary_reaches_handler_after_current_access(origin):
    app, events = application()
    headers = {"x-csrf-token": "synthetic-csrf", "sec-fetch-site": "same-origin"}
    if origin:
        headers["origin"] = origin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://music.invalid"
    ) as client:
        response = await client.post("/upload", headers=headers, content=payload(events))
    assert response.status_code == 200
    assert response.json() == {"owner": "immutable-owner", "bytes": 15}
    assert events == ["session-current-access", "handler", "body-consumed"]


async def test_status_read_needs_current_session_but_no_mutation_token():
    app, events = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://music.invalid"
    ) as client:
        response = await client.get("/upload")
    assert response.status_code == 200
    assert events == ["session-current-access", "handler"]


async def test_regular_owner_cannot_reach_administrator_setup_body():
    app, events = application(require_admin=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://music.invalid"
    ) as client:
        response = await client.post(
            "/upload", headers={"x-csrf-token": "synthetic-csrf"}, content=payload(events)
        )
    assert response.status_code == 403
    assert response.json()["error"] == "admin_required"
    assert events == ["session-current-access"]
