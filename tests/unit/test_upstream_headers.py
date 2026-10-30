"""Connection-nominated fields never cross the proxy in either direction."""

from unittest.mock import AsyncMock

import httpx
import pytest

from shijhon.proxy.upstream import request_headers, response_headers, send_bytes

HEADERS = [
    (b"Connection", b"keep-alive, X-Transient"),
    (b"cOnNeCtIoN", b" X-Other , , x-transient "),
    (b"X-Transient", b"one"),
    (b"x-OTHER", b"two"),
    (b"Keep-Alive", b"timeout=5"),
    (b"Content-Length", b"3"),
    (b"Content-Range", b"bytes 0-2/3"),
    (b"ETag", b"synthetic"),
]


def test_request_filters_every_connection_token_with_case_insensitive_names() -> None:
    assert request_headers(HEADERS) == HEADERS[-2:]
    assert request_headers(HEADERS, keep_length=True) == HEADERS[-3:]


def test_response_filters_connection_tokens_and_preserves_media_headers() -> None:
    response = httpx.Response(206, headers=HEADERS)
    assert response_headers(response) == HEADERS[-3:]


@pytest.mark.anyio
async def test_buffered_response_also_filters_nominated_headers() -> None:
    send = AsyncMock()
    await send_bytes(send, 206, b"abc", HEADERS)
    assert send.await_args_list[0].args[0]["headers"] == [*HEADERS[-2:], (b"content-length", b"3")]
