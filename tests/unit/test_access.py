"""Caller proofs reuse connections without borrowing another caller's authority."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from shijhon.access import (
    REQUEST_SECONDS,
    AccessDenied,
    AccessService,
    AccessUnavailable,
    Principal,
)
from shijhon.navidrome.client import NavidromeError

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


async def test_grants_are_shared_while_asked_and_within_a_request_only() -> None:
    asked: list[str] = []
    release = anyio.Event()
    release.set()
    fails = {"now": False}

    class Native:
        library_id = 1

        async def native_json(self, method: str, path: str, **kwargs: Any) -> Any:
            asked.append(path)
            await release.wait()
            if fails["now"]:
                raise NavidromeError("unavailable", status=500)
            if path == "user":
                return [{"id": "u1", "userName": "listener", "isAdmin": False}]
            return [{"id": 1}]

    access = AccessService(Native())  # type: ignore[arg-type]
    answers: list[Principal] = []

    async def request(name: str = "listener", steps: int = 1) -> None:
        # (anyio gives each started task a context of its own, as each request has)
        for _ in range(steps):
            answers.append(await access.grants(name))

    # One request's steps ask once.
    async with anyio.create_task_group() as group:
        group.start_soon(request, "listener", 3)
    assert len(answers) == 3 and len(asked) == 2

    # Requests at the same moment share one question.
    asked.clear()
    release = anyio.Event()
    async with anyio.create_task_group() as group:
        for name in ("listener", "listener", "listener"):
            group.start_soon(request, name)
        await anyio.wait_all_tasks_blocked()
        release.set()
    assert len(asked) == 2

    # A later request asks again: nothing is kept for it, so a revocation counts at once.
    asked.clear()
    async with anyio.create_task_group() as group:
        group.start_soon(request)
    assert len(asked) == 2

    # A failure is its own: the next one asks.
    fails["now"] = True

    async def failing() -> None:
        with pytest.raises(AccessUnavailable):
            await access.grants("listener")

    async with anyio.create_task_group() as group:
        group.start_soon(failing)
    fails["now"] = False
    asked.clear()
    async with anyio.create_task_group() as group:
        group.start_soon(request)
    assert len(asked) == 2


async def test_grants_never_stand_in_for_another_spelling_or_for_a_later_step() -> None:
    """Names that fold alike in Python can be two accounts in Navidrome, and a request can
    wait long between its steps while the account's access changes."""
    grants = {"straße": [1], "strasse": [2]}
    release = anyio.Event()
    asked: list[str] = []

    class Native:
        library_id = 1

        async def native_json(self, method: str, path: str, **kwargs: Any) -> Any:
            asked.append(path)
            await release.wait()
            name = (kwargs.get("params") or {}).get("user_name")
            if name is not None:
                return [{"id": "id-" + name, "userName": name, "isAdmin": False}]
            owner = path.split("/")[1].removeprefix("id-")
            return [{"id": library} for library in grants[owner]]

    access = AccessService(Native())  # type: ignore[arg-type]
    found: dict[str, Principal] = {}

    async def ask(name: str) -> None:
        # Verified under the name asked for, as a real account's own lookup would be.
        found[name] = await access.grants(name)

    async def by_name(username: str, **kwargs: Any) -> Principal:
        users = await access.navidrome.native_json("GET", "user", params={"user_name": username})
        libraries = await access.navidrome.native_json("GET", f"user/{users[0]['id']}/library")
        return Principal(users[0]["id"], username, False, frozenset(i["id"] for i in libraries))

    access.verify = by_name  # type: ignore[method-assign]
    async with anyio.create_task_group() as group:
        group.start_soon(ask, "straße")
        group.start_soon(ask, "strasse")
        await anyio.wait_all_tasks_blocked()
        release.set()
    assert found["straße"].user_id == "id-straße" and found["strasse"].user_id == "id-strasse"
    assert found["straße"].library_ids == frozenset({1})

    # One request, its steps apart: a change of access in between counts.
    async def request() -> None:
        assert (await access.grants("straße")).library_ids == frozenset({1})
        grants["straße"] = [2]
        assert (await access.grants("straße")).library_ids == frozenset({1})  # right after
        await anyio.sleep(REQUEST_SECONDS + 0.01)
        assert (await access.grants("straße")).library_ids == frozenset({2})

    async with anyio.create_task_group() as group:
        group.start_soon(request)
