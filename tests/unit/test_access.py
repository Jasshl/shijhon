"""Caller proofs reuse connections without borrowing another caller's authority."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from shijhon.access import AccessDenied, AccessService, AccessUnavailable

pytestmark = pytest.mark.anyio


def bearer(owner: str) -> str:
    claims = (
        base64.urlsafe_b64encode(json.dumps({"uid": owner, "sub": "Listener " + owner}).encode())
        .decode()
        .rstrip("=")
    )
    return "invented." + claims + ".signature"


@pytest.mark.parametrize("status", [200, 302, 401, 403, 404, 500])
async def test_pooled_native_proof_uses_only_the_supplied_bearer(status):
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url == "http://native.invalid/api/user/a"
        assert request.headers["x-nd-authorization"] == "Bearer " + bearer("a")
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(
            status,
            headers={"location": "http://elsewhere.invalid/"},
            json={"id": "a", "userName": "Listener a"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        cookies={"jwt": "invented-admin-cookie"},
        headers={"x-nd-authorization": "Bearer invented-admin-header"},
        params={"jwt": "invented-admin-query"},
        auth=("invented-admin", "invented-password"),
        follow_redirects=True,
    ) as http:
        native = SimpleNamespace(
            http=http,
            base_url="http://native.invalid",
            native_json=AsyncMock(
                side_effect=[
                    {"id": "a", "userName": "Listener a", "isAdmin": False},
                    [{"id": 1}],
                ]
            ),
        )
        access = AccessService(native)
        if status == 200:
            principal = await access.verify_native(bearer("a"))
            assert principal.user_id == "a" and not principal.is_admin
            assert principal.library_ids == frozenset({1})
            assert native.native_json.await_count == 2
        else:
            error = AccessDenied if status in (401, 403, 404) else AccessUnavailable
            with pytest.raises(error):
                await access.verify_native(bearer("a"))
            native.native_json.assert_not_awaited()
        assert len(seen) == 1


async def test_each_pooled_proof_reads_current_grants_and_ignores_response_cookies():
    seen = []

    def upstream(request):
        owner = request.url.path.rsplit("/", 1)[-1]
        seen.append(owner)
        assert "cookie" not in request.headers
        assert request.headers["x-nd-authorization"] == "Bearer " + bearer(owner)
        return httpx.Response(
            200,
            headers={"set-cookie": "jwt=invented-response-cookie; Path=/"},
            json={"id": owner, "userName": "Listener " + owner},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as http:
        native = SimpleNamespace(
            http=http,
            base_url="http://native.invalid",
            native_json=AsyncMock(
                side_effect=[
                    {"id": "a", "userName": "Listener a", "isAdmin": False},
                    [{"id": 1}],
                    {"id": "b", "userName": "Listener b", "isAdmin": False},
                    [{"id": 2}],
                    {"id": "a", "userName": "Listener a", "isAdmin": False},
                    [],
                ]
            ),
        )
        access = AccessService(native)
        assert (await access.verify_native(bearer("a"))).library_ids == frozenset({1})
        assert (await access.verify_native(bearer("b"))).library_ids == frozenset({2})
        assert (await access.verify_native(bearer("a"))).library_ids == frozenset()
        assert seen == ["a", "b", "a"]
        assert not http.is_closed
