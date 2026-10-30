"""Unavailable native mutation results never trigger a second remote write."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from shijhon.views.native_collection_requests import NativeCollectionRequests

pytestmark = pytest.mark.anyio


async def test_native_append_connection_failure_is_a_gateway_error_without_retry() -> None:
    upstream = SimpleNamespace(send=AsyncMock(side_effect=httpx.ConnectError("unavailable")))
    requests = SimpleNamespace(upstream=upstream, _tracks=AsyncMock(return_value=[]))
    adapter = NativeCollectionRequests(requests, SimpleNamespace())
    body = b'{"ids":["synthetic"]}'
    scope = {
        "method": "POST",
        "path": "/api/playlist/synthetic/tracks",
        "headers": [
            (b"content-length", str(len(body)).encode()),
            (b"content-type", b"application/json"),
        ],
    }
    receive = AsyncMock(return_value={"type": "http.request", "body": body, "more_body": False})
    send = AsyncMock()
    assert await adapter.intercept(scope, receive, send)
    upstream.send.assert_awaited_once()
    assert send.await_args_list[0].args[0]["status"] == 502


async def test_active_native_user_prewarms_less_often_than_query_freshness(monkeypatch):
    from shijhon.access import Principal
    from shijhon.collection import native as native_module
    from shijhon.collection.native import NativeVisibility
    from shijhon.collection.query_index import Binding, Budgets

    now = [0.0]
    monkeypatch.setattr(native_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    principal = Principal("one", "Listener", False, frozenset({1}))
    binding = Binding("one", frozenset({"1"}), (("1", "gen"),))
    queued = []
    index = SimpleNamespace(
        budgets=Budgets(), spawn=queued.append, stats=AsyncMock(return_value={})
    )
    visibility = SimpleNamespace(
        index=index,
        upstream=None,
        binding=AsyncMock(return_value=binding),
        access=SimpleNamespace(
            navidrome=SimpleNamespace(library_id=1), verify_native=AsyncMock(return_value=principal)
        ),
    )
    adapter = NativeVisibility(visibility)
    adapter.prepare_foreign = AsyncMock()
    scope = {
        "method": "GET",
        "path": "/api/artist/artist",
        "query_string": b"",
        "headers": [(b"x-nd-authorization", b"Bearer invented")],
    }
    for moment in (0.0, 60.0, 61.0, 299.0):
        now[0] = moment
        assert not await adapter.intercept(scope, AsyncMock(), AsyncMock())
    assert len(queued) == 1
    now[0] = 301.0
    assert not await adapter.intercept(scope, AsyncMock(), AsyncMock())
    assert len(queued) == 2
