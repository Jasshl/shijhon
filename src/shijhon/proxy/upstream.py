"""Talking to Navidrome: forward requests byte for byte and relay responses as streams.

Response status, headers (including ``Content-Length``, ``ETag`` and ``Content-Range``)
and body pass through unchanged; only hop-by-hop headers are dropped. Bodies are never
buffered, and a client that goes away simply ends the relay.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from typing import Any

import anyio
import httpx

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

HOP_BY_HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"trailers",
    b"transfer-encoding",
    b"upgrade",
}
# The body is re-framed by httpx, so its length header is set there.
_REQUEST_DROP = HOP_BY_HOP | {b"content-length"}


def request_headers(
    headers: list[tuple[bytes, bytes]], *, keep_length: bool = False
) -> list[tuple[bytes, bytes]]:
    drop = HOP_BY_HOP if keep_length else _REQUEST_DROP
    return [(k, v) for k, v in headers if k.lower() not in drop]


def _escape(raw: bytes) -> bytes:
    """Percent-encode bytes that may not appear raw in a request target (non-ASCII,
    spaces, controls); everything else is passed on exactly as the client sent it."""
    return re.sub(rb"[^\x21-\x7e]", lambda m: b"%%%02X" % m.group(0)[0], raw)


class Upstream:
    """Pooled HTTP client for Navidrome."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base = httpx.URL(base_url.rstrip("/"))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=None),
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
            follow_redirects=False,
            trust_env=False,
        )

    def url(self, raw_path: bytes, query: bytes = b"") -> httpx.URL:
        prefix = self.base.raw_path.rstrip(b"/")
        target = prefix + _escape(raw_path) + (b"?" + _escape(query) if query else b"")
        return self.base.copy_with(raw_path=target)

    async def send(
        self,
        method: str,
        raw_path: bytes,
        query: bytes,
        headers: list[tuple[bytes, bytes]],
        body: bytes | AsyncIterator[bytes] | None,
    ) -> httpx.Response:
        """Send exactly these headers (no client defaults) and return a streaming response."""
        request = httpx.Request(method, self.url(raw_path, query), headers=headers, content=body)
        return await self.client.send(request, stream=True)

    async def aclose(self) -> None:
        await self.client.aclose()


async def stream_request_body(receive: Receive) -> AsyncIterator[bytes]:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise anyio.EndOfStream
        yield message.get("body", b"")
        if not message.get("more_body", False):
            return


async def read_body(receive: Receive, limit: int) -> tuple[bytes, bool]:
    """Read the body as far as ``limit`` bytes allow. Returns (bytes read, ended): the
    whole body when it ended within the limit; otherwise what was read when the limit was
    passed (more than ``limit`` bytes), the rest - if it has not ended - still to come."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise anyio.EndOfStream
        chunk = message.get("body", b"")
        chunks.append(chunk)
        size += len(chunk)
        ended = not message.get("more_body", False)
        if ended or size > limit:
            return b"".join(chunks), ended


async def relay(
    response: httpx.Response,
    receive: Receive,
    send: Send,
    *,
    head: bool = False,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Relay a streaming upstream response to the client."""
    await send_stream(
        receive,
        send,
        response.status_code,
        headers if headers is not None else response_headers(response),
        None if head else response.aiter_raw(),
        response.aclose,
    )


async def send_stream(
    receive: Receive,
    send: Send,
    status: int,
    headers: list[tuple[bytes, bytes]],
    body: AsyncIterator[bytes] | None,
    close: Callable[[], Awaitable[None]],
) -> None:
    """Send a response whose body is streamed. Stops quietly when the client leaves.

    uvicorn ignores sends after an HTTP client disconnects, so this watches for
    ``http.disconnect`` itself; otherwise an abandoned stream would be read to the end.
    """
    try:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        if body is None:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        async with anyio.create_task_group() as tg:
            tg.start_soon(_cancel_on_disconnect, receive, tg.cancel_scope)
            async for chunk in body:
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            tg.cancel_scope.cancel()
    except* OSError:
        log.debug("client disconnected")
    except* httpx.HTTPError as group:
        log.warning("upstream response ended early: %s", type(group.exceptions[0]).__name__)
    finally:
        await close()


async def _cancel_on_disconnect(receive: Receive, scope: anyio.CancelScope) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass
    scope.cancel()


def response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    return [(k, v) for k, v in response.headers.raw if k.lower() not in HOP_BY_HOP]


async def send_bytes(
    send: Send,
    status: int,
    body: bytes,
    headers: list[tuple[bytes, bytes]],
    *,
    head: bool = False,
) -> None:
    out = [(k, v) for k, v in headers if k.lower() not in HOP_BY_HOP | {b"content-length"}]
    out.append((b"content-length", str(len(body)).encode()))
    try:
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": b"" if head else body})
    except OSError:
        log.debug("client disconnected")
