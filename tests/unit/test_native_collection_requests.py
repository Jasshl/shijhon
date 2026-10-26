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
