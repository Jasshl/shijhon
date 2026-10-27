"""The file-import dashboard preserves bounded requests and the service approval boundary."""

from __future__ import annotations

from dataclasses import asdict
from types import MethodType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from shijhon.dashboard.app import Dashboard as RealDashboard
from shijhon.dashboard.auth import Session
from shijhon.dashboard.music_imports import MAX_JSON_BYTES, Pages
from shijhon.music_imports import ImportError, ImportLimits
from tests.unit.test_collection_workspace import Dashboard

pytestmark = pytest.mark.anyio


class Imports:
    def __init__(self):
        self.calls = []
        self.refuse_receive = False
        self.job = dict(
            id="private-job",
            state="review",
            revision=7,
            total_files=2,
            total_bytes=42,
            received_files=2,
            received_bytes=42,
            eligible_files=1,
            eligible_bytes=21,
            complete_files=0,
            rejected_files=1,
            excluded_files=0,
            skipped_files=1,
            error="",
            next_offset=None,
            items=[
                dict(
                    id="item",
                    client_key="hint",
                    path="Album/<script>.flac",
                    size=21,
                    state="eligible",
                    attempt=0,
                    warnings=[],
                    error="",
                    sha256="",
                    song_id="",
                )
            ],
        )

    async def capabilities(self, owner):
        self.calls.append(("capabilities", owner))
        return dict(
            enabled=True,
            ready=True,
            formats=["mp3", "flac"],
            manifest_page_size=500,
            limits=asdict(ImportLimits()),
        )

    async def listing(self, owner, *, limit):
        self.calls.append(("listing", owner, limit))
        return [self.job]

    async def status(
        self, owner, ident, *, offset=0, limit=100, problems_only=False, overlap_only=False
    ):
        self.calls.append(("status", owner, ident, offset, limit, problems_only, overlap_only))
        if ident != "private-job":
            raise ImportError("not_found", "Import not found.")
        return self.job

    async def create(self, owner, request_key):
        self.calls.append(("create", owner, request_key))
        return self.job

    async def declare(self, owner, ident, page_key, entries):
        self.calls.append(("declare", owner, ident, page_key, entries))
        return self.job

    async def approve(self, owner, ident, revision, request_key):
        self.calls.append(("approve", owner, ident, revision, request_key))
        return {**self.job, "state": "approved"}

    async def review(self, owner, ident):
        self.calls.append(("review", owner, ident))
        return {**self.job, "state": "checking"}

    async def distinct(self, owner, ident, item, *, value=True):
        self.calls.append(("distinct", owner, ident, item, value))
        return {**self.job, "state": "checking"}

    async def receive(self, owner, ident, item, attempt, stream):
        self.calls.append(("receive", owner, ident, item, attempt))
        if self.refuse_receive:
            raise ImportError("access", "Access changed.")
        async for chunk in stream:
            self.calls.append(("chunk", bytes(chunk)))
        return self.job


@pytest.fixture
async def imports_dashboard():
    dashboard = Dashboard(None)
    service = Imports()
    dashboard.services.music_imports = service
    # Exercise the real parent's admission wrapper, with a fixed verified synthetic session.
    dashboard._personal_api = MethodType(RealDashboard._personal_api, dashboard)

    original_render = dashboard._render

    async def render(name, **context):
        status = context.pop("status_code", 200)
        result = await original_render(name, **context)
        result.status_code = status
        return result

    dashboard._render = render

    async def session(self, request):
        return Session("test", "Listener", "csrf", 9999999999, "immutable-owner")

    dashboard._session = MethodType(session, dashboard)
    dashboard._personal_page = MethodType(RealDashboard._personal_page, dashboard)
    app = Starlette(routes=[Mount("/shijhon", routes=Pages(dashboard).routes())])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield service, dashboard, client


async def test_render_disclosure_disabled_setup_and_owner_only_history(imports_dashboard):
    service, dashboard, client = imports_dashboard
    response = await client.get("/shijhon/collection/import/private-job")
    assert response.status_code == 200
    assert "Imported music is shared" in response.text
    assert 'accept=".mp3,.flac"' in response.text
    assert "Upload for review" in response.text
    assert "Changed source files need a new import" in response.text
    assert "Album/&lt;script&gt;.flac" in response.text
    assert "Album/<script>.flac" not in response.text
    assert all(call[1] == "immutable-owner" for call in service.calls)
    dashboard.services.music_imports = None
    disabled = await client.get("/shijhon/collection/import")
    assert "Music imports are not set up" in disabled.text
    assert 'type="file" multiple accept="" disabled' in disabled.text
    assert "Browse imported music" in disabled.text
    assert "show=server&amp;origin=imported" in disabled.text
    unavailable = await client.post(
        "/shijhon/collection/import/api",
        json={"request_key": "key"},
        headers={"x-csrf-token": "csrf"},
    )
    assert unavailable.status_code == 503 and unavailable.json()["code"] == "unavailable"


async def test_import_page_requests_reauthentication_without_signing_out_or_losing_batch(
    imports_dashboard,
):
    service, dashboard, client = imports_dashboard
    bearers = set()
    dashboard.services.collection_refresh = SimpleNamespace(
        reader=SimpleNamespace(has_bearer=lambda owner: owner in bearers)
    )
    path = "/shijhon/collection/import/private-job"
    response = await client.get(path)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/shijhon/sign-in"
    assert parse_qs(target.query) == {"reauth": ["1"], "next": [path]}
    assert "set-cookie" not in response.headers
    assert not any(call[0] in ("receive", "review", "approve") for call in service.calls)
    bearers.add("immutable-owner")
    response = await client.get(path)
    assert response.status_code == 200 and "private-job" in response.text


async def test_create_manifest_review_and_approval_keep_current_owner_and_revision(
    imports_dashboard,
):
    service, _dashboard, client = imports_dashboard
    headers = {"x-csrf-token": "csrf"}
    base = "/shijhon/collection/import/api"
    result = await client.post(
        base, json={"request_key": "same-request", "owner": "forged"}, headers=headers
    )
    assert result.json()["id"] == "private-job"
    rows = [dict(client_key="hint", path="Album/song.flac", size=21)]
    declared = await client.post(
        base + "/private-job/manifest",
        json={"page_key": "page-0", "entries": rows},
        headers=headers,
    )
    assert declared.status_code == 200
    call = service.calls[-1]
    assert call[:4] == ("declare", "immutable-owner", "private-job", "page-0")
    assert asdict(call[4][0]) == {**rows[0], "sha256": ""}
    checking = await client.post(base + "/private-job/review", json={}, headers=headers)
    assert checking.json()["state"] == "checking"
    assert not any(call[0] == "approve" for call in service.calls)
    approved = await client.post(
        base + "/private-job/approve",
        json={"revision": 7, "request_key": "approval"},
        headers=headers,
    )
    assert approved.json()["state"] == "approved"
    assert service.calls[-1] == ("approve", "immutable-owner", "private-job", 7, "approval")
    # "Not the same song": the owner's own job and one file, a strict boolean.
    distinct = await client.post(
        base + "/private-job/distinct", json={"item": "item", "distinct": True}, headers=headers
    )
    assert distinct.json()["state"] == "checking"
    assert service.calls[-1] == ("distinct", "immutable-owner", "private-job", "item", True)
    refused = await client.post(
        base + "/private-job/distinct", json={"item": "item", "distinct": "yes"}, headers=headers
    )
    assert refused.status_code == 400 and service.calls[-1][0] == "distinct"


@pytest.mark.parametrize(
    "body",
    [
        {
            "entries": [dict(client_key="hint", path="song.flac", size=21)] * 501,
            "page_key": "large",
        },
        {"entries": [dict(client_key="hint", path="song.flac", size=True)], "page_key": "bool"},
        {
            "entries": [dict(client_key="hint", path="song.flac", size=21, owner="forged")],
            "page_key": "extra",
        },
    ],
)
async def test_manifest_rejects_unbounded_or_ambiguous_entries(imports_dashboard, body):
    service, _dashboard, client = imports_dashboard
    response = await client.post(
        "/shijhon/collection/import/api/private-job/manifest",
        json=body,
        headers={"x-csrf-token": "csrf"},
    )
    assert response.status_code == 400
    assert not service.calls


@pytest.mark.parametrize(
    "body", ['{"request_key":"one","request_key":"two"}', "x" * (MAX_JSON_BYTES + 1)]
)
async def test_controls_reject_duplicate_and_oversized_json(imports_dashboard, body):
    service, _dashboard, client = imports_dashboard
    response = await client.post(
        "/shijhon/collection/import/api",
        content=body,
        headers={"x-csrf-token": "csrf", "content-type": "application/json"},
    )
    assert response.status_code == 400 and not service.calls


async def test_raw_upload_is_never_form_parsed_and_admission_precedes_stream(imports_dashboard):
    service, _dashboard, client = imports_dashboard
    events = []

    async def chunks():
        events.append("read")
        yield b"csrf=not-a-form&owner=forged"
        yield b"second-chunk"

    path = "/shijhon/collection/import/api/private-job/files/item?attempt=2"
    headers = {"x-csrf-token": "csrf", "content-type": "application/octet-stream"}
    service.refuse_receive = True
    refused = await client.post(path, content=chunks(), headers=headers)
    assert refused.status_code == 403 and events == []
    service.refuse_receive = False
    admitted = await client.post(path, content=chunks(), headers=headers)
    assert admitted.status_code == 200
    assert service.calls[-4] == ("receive", "immutable-owner", "private-job", "item", 2)
    assert [call[1] for call in service.calls if call[0] == "chunk"] == [
        b"csrf=not-a-form&owner=forged",
        b"second-chunk",
        b"",
    ]
    assert events == ["read"]


async def test_routes_return_bounded_private_json_without_reading_foreign_jobs(imports_dashboard):
    service, _dashboard, client = imports_dashboard
    base = "/shijhon/collection/import/api"
    assert (await client.get(base + "/private-job?limit=101")).status_code == 400
    assert (await client.get(base + "/private-job?offset=-1")).status_code == 400
    assert not service.calls
    assert (await client.get(base + "/private-job?problems_only=true")).status_code == 400
    assert not service.calls
    problems = await client.get(base + "/private-job?problems_only=1")
    assert problems.status_code == 200
    assert service.calls[-1] == ("status", "immutable-owner", "private-job", 0, 100, True, False)
    assert (await client.get(base + "/private-job?overlap_only=yes")).status_code == 400
    overlap = await client.get(base + "/private-job?overlap_only=1")
    assert overlap.status_code == 200
    assert service.calls[-1] == ("status", "immutable-owner", "private-job", 0, 100, False, True)
    assert (await client.get(base + "/foreign-job")).status_code == 404
    result = await client.get(base + "/private-job?offset=100&limit=40")
    assert result.headers["cache-control"] == "no-store"
    assert service.calls[-1] == ("status", "immutable-owner", "private-job", 100, 40, False, False)


async def test_later_failed_access_check_does_not_render_previously_fetched_receipts(
    imports_dashboard,
):
    from types import SimpleNamespace

    from shijhon.collection.contracts import CollectionError

    _service, dashboard, client = imports_dashboard

    async def profile(owner):
        raise CollectionError("Access changed")

    dashboard.services.collection = SimpleNamespace(profile=profile)
    response = await client.get("/shijhon/collection/import/private-job")
    assert response.status_code == 503
    assert "Your current account access could not be checked" in response.text
    assert "Album/&lt;script&gt;.flac" not in response.text
    assert "private-job" not in response.text
