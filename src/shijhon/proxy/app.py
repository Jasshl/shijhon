"""The client-facing proxy.

Everything is forwarded to Navidrome unchanged unless a handler for that Subsonic method
decides Shijhon has something to add or serve. Handlers ask for the caller's credentials
before doing any work; if Navidrome refuses them, the client gets Navidrome's own answer
to that check - what it would answer the request itself - instead of a forward that fails
a second time. A request Shijhon does not check is forwarded untouched.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from shijhon.proxy.auth import Caller, CheckFailed, CredentialChecker, Refusal
from shijhon.proxy.forwarding import Forwarding
from shijhon.proxy.params import (
    BODY_LIMIT,
    MAX_PARAMS,
    RestCall,
    body_kind,
    header,
    unreadable,
)
from shijhon.proxy.upstream import (
    Receive,
    Scope,
    Send,
    Upstream,
    read_body,
    relay,
    request_headers,
    send_bytes,
    stream_request_body,
)

access_log = logging.getLogger("shijhon.access")
log = logging.getLogger(__name__)

CLOSE_SECONDS = 10.0  # the longest a request's kept resources take to close
_PARSED_INLINE = 256 * 1024  # a longer body's parameters are parsed in a worker thread
_LABEL_CLIENT = 64  # characters of a client's name in the access log
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
# A ready response: called with the ASGI receive/send pair.
Reply = Callable[[Receive, Send], Awaitable[None]]


@dataclass
class Forward:
    """Forward this (possibly rewritten) call to Navidrome."""

    call: RestCall


HandlerResult = Forward | Reply | None


class RequestContext:
    def __init__(self, checker: CredentialChecker, call: RestCall, scope: Scope) -> None:
        self._checker = checker
        self._call = call
        self.scope = scope
        self._checked = False
        self._caller: Caller | None = None
        # What one handler keeps for a later one of the same request (a catalog song's
        # play, then its stream once committed), and what is closed when the request's
        # handlers are done - before its answer is sent: nothing kept outlives the request.
        self.kept: dict[str, Any] = {}
        self.closing = contextlib.AsyncExitStack()
        # Navidrome's own answer when the check found the credentials refused: the request
        # is answered with it, never forwarded to fail again.
        self.refusal: Refusal | None = None
        self._failed: CheckFailed | None = None

    @property
    def head(self) -> bool:
        return self._call.http_method == "HEAD"

    async def caller(self) -> Caller | None:
        """The authenticated caller, checked with Navidrome once per request; None when
        Navidrome refused the credentials (``refusal``) or there are none. ``CheckFailed``
        when Navidrome gave no verdict: the request is then neither worked for nor passed
        on as if refused."""
        if self._failed is not None:
            raise self._failed
        if not self._checked:
            try:
                verdict = await self._checker.check(self._call)
            except CheckFailed as exc:
                self._failed = exc
                raise
            self._caller = verdict if isinstance(verdict, Caller) else None
            self.refusal = verdict if isinstance(verdict, Refusal) else None
            self._checked = True
        return self._caller

    async def close(self) -> None:
        """The request's handlers are done: what they kept is closed (also when canceled)."""
        self.kept.clear()
        with anyio.move_on_after(CLOSE_SECONDS, shield=True):
            try:
                await self.closing.aclose()
            except Exception as exc:  # the answer goes out all the same
                log.warning("closing a request's resources failed: %s", type(exc).__name__)


Handler = Callable[[RestCall, RequestContext], Awaitable[HandlerResult]]
# Work before any handler, on every parsed REST call (a release taken out comes back).
Before = Callable[[RestCall, RequestContext], Awaitable[None]]
# Intercepts a non-Subsonic path; returns True if it answered, False to forward.
PathInterceptor = Callable[[Scope, Receive, Send], Awaitable[bool]]


class ProxyApp:
    def __init__(
        self,
        upstream: Upstream,
        checker: CredentialChecker,
        *,
        max_parsed_body: int = 1024 * 1024,
        handlers: dict[str, Handler] | None = None,
        mounts: dict[str, ASGIApp] | None = None,
        path_interceptors: dict[str, PathInterceptor] | None = None,
        forwarding: Forwarding | None = None,
    ) -> None:
        self.upstream = upstream
        self.checker = checker
        # Who the client is, for Navidrome: the headers every request takes there.
        self.forwarding = forwarding or Forwarding()
        self.max_parsed_body = max_parsed_body
        self.handlers: dict[str, Handler] = dict(handlers or {})
        self.mounts = dict(mounts or {})
        self.path_interceptors: dict[str, PathInterceptor] = dict(path_interceptors or {})
        # For Subsonic methods without a handler of their own (catalog IDs).
        self.fallback: Handler | None = None
        self.before: Before | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1003})
            return
        started = time.perf_counter()
        status: list[int] = [0]

        async def tracking_send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        path: str = scope["path"]
        label = ""
        try:
            for prefix, mounted in self.mounts.items():
                if path == prefix or path.startswith(prefix + "/"):
                    label = f"mount {prefix}"
                    await mounted(scope, receive, tracking_send)
                    return
            # Everything below may reach Navidrome: with the client's address, and without
            # what an untrusted client claims about itself.
            if not await self.forwarding.settled():
                label = "http (starting)"
                await send_bytes(
                    tracking_send,
                    503,
                    b"Navidrome is not answering yet\n",
                    [(b"content-type", b"text/plain"), (b"retry-after", b"5")],
                )
                return
            scope = self.forwarding.scope(scope)
            # The method as Navidrome gets it: the upstream client sends it in capitals, so
            # a "post" is a POST for Shijhon too (its form read, its audio intercepted).
            scope["method"] = str(scope["method"]).upper()
            if path.startswith("/rest/"):
                label = await self._rest(scope, receive, tracking_send)
                return
            label = "http /" + path.lstrip("/").split("/", 1)[0]
            for prefix, intercept in self.path_interceptors.items():
                if path.startswith(prefix) and await intercept(scope, receive, tracking_send):
                    return
            await self._forward_raw(scope, receive, tracking_send)
        except anyio.EndOfStream:
            label = label or "http"  # client left while sending its request
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            access_log.info("%s %s %s %.0fms", label, scope["method"], status[0], elapsed)

    async def _rest(self, scope: Scope, receive: Receive, send: Send) -> str:
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode()
        name = scope["path"][len("/rest/") :].removesuffix(".view")
        method: str = scope["method"]
        headers: list[tuple[bytes, bytes]] = list(scope["headers"])
        query: bytes = scope.get("query_string", b"")
        asked = {k.lower() for k, _ in headers}
        if (
            method == "OPTIONS"
            and b"origin" in asked  # even an empty one
            and header(headers, b"access-control-request-method")
        ):
            # A CORS preflight: Navidrome answers it before it looks at the method or the
            # credentials. Nothing to check, nothing to do.
            await self._forward_raw(scope, receive, send)
            return f"rest {name} (preflight)"

        body: bytes | None = None
        streamed: AsyncIterator[bytes] | None = None
        # A request Navidrome answers with an error before it looks at any parameter - a
        # form longer, or with more parameters, than it reads; a query or a form its parser
        # refuses (a semicolon, a broken escape); a Content-Type its parser refuses - is
        # forwarded for that answer, with no handler: nothing in it is read here either,
        # and no work done for it.
        kind = body_kind(method, headers)
        refused = unreadable(query) or kind == "refused"
        if method not in ("GET", "HEAD"):
            # What Navidrome reads as parameters, Shijhon reads too - whatever its size: a
            # form, as far as Navidrome reads one. Another body holds no parameters: a
            # small one is read for the handlers (the JSON client profile of
            # getTranscodeDecision), a larger one streams through behind them.
            form = kind == "form"
            limit = BODY_LIMIT if form else min(self.max_parsed_body, BODY_LIMIT)
            data, ended = await read_body(receive, limit)
            refused = refused or (form and (len(data) > limit or not ended or unreadable(data)))
            if ended and len(data) <= limit and not refused:
                body = data
            else:
                streamed = _prepend(data, None if ended else stream_request_body(receive))

        async def built(query: bytes, body: bytes | None) -> RestCall:
            if body is not None and len(body) > _PARSED_INLINE:  # a long form: in a thread
                return await anyio.to_thread.run_sync(
                    RestCall.build, name, method, raw_path, query, headers, body
                )
            return RestCall.build(name, method, raw_path, query, headers, body)

        call = await built(b"" if refused else query, body)
        if not refused and call.form and len(call.params) > MAX_PARAMS:
            # Navidrome puts a form and the query together and reads them once more: more
            # parameters than it reads then, and it finds none of them.
            refused, streamed = True, _prepend(body or b"", None)
            call = await built(b"", None)
        if refused:
            call.query = query  # (forwarded as it came)
        label = f"rest {name}" + (f" c={call.client[:_LABEL_CLIENT]}" if call.client else "")
        handler = None if refused else self.handlers.get(name, self.fallback)
        ctx = RequestContext(self.checker, call, scope)
        try:
            try:
                if self.before is not None and not refused:
                    await self.before(call, ctx)
                result = await handler(call, ctx) if handler is not None else None
            finally:
                await ctx.close()
        except CheckFailed as exc:
            # No verdict on the credentials, and a handler that cannot go on without one (a
            # placeholder's audio): an error, never a forward as if they were refused -
            # Navidrome would play the placeholder's silence.
            log.warning("credential check failed (%s): %s answered with an error", exc, name)
            await send_bytes(
                send,
                502,
                b"Navidrome could not check the request's credentials\n",
                [(b"content-type", b"text/plain"), (b"retry-after", b"5")],
                head=ctx.head,
            )
            return label
        if isinstance(result, Forward):
            call = result.call
        elif result is not None:
            await result(receive, send)
            return label
        if ctx.refusal is not None:
            # Navidrome refused these credentials when Shijhon checked them: its answer,
            # not a second failure toward its login limit.
            await _send_refusal(ctx.refusal, send, head=ctx.head)
            return label
        await self.forward_call(call, receive, send, streamed=streamed)
        return label

    async def forward_call(
        self,
        call: RestCall,
        receive: Receive,
        send: Send,
        *,
        streamed: AsyncIterator[bytes] | None = None,
    ) -> None:
        body: bytes | AsyncIterator[bytes] | None = streamed if streamed is not None else call.body
        headers = request_headers(call.headers, keep_length=streamed is not None)
        await self._send_upstream(
            call.http_method, call.raw_path, call.query, headers, body, receive, send
        )

    async def _forward_raw(self, scope: Scope, receive: Receive, send: Send) -> None:
        method: str = scope["method"]
        headers = list(scope["headers"])
        has_body = method not in ("GET", "HEAD") and (
            header(headers, b"content-length") not in ("", "0")
            or header(headers, b"transfer-encoding") != ""
        )
        body = stream_request_body(receive) if has_body else None
        raw_path = scope.get("raw_path") or scope["path"].encode()
        await self._send_upstream(
            method,
            raw_path,
            scope.get("query_string", b""),
            request_headers(headers, keep_length=True),
            body,
            receive,
            send,
        )

    async def _send_upstream(
        self,
        method: str,
        raw_path: bytes,
        query: bytes,
        headers: list[tuple[bytes, bytes]],
        body: bytes | AsyncIterator[bytes] | None,
        receive: Receive,
        send: Send,
    ) -> None:
        try:
            response = await self.upstream.send(method, raw_path, query, headers, body)
        except httpx.HTTPError as exc:
            log.warning("navidrome unreachable: %s", type(exc).__name__)
            await send_bytes(
                send, 502, b"Navidrome is unreachable\n", [(b"content-type", b"text/plain")]
            )
            return
        await relay(response, receive, send, head=method == "HEAD")


async def _send_refusal(refusal: Refusal, send: Send, *, head: bool) -> None:
    """Navidrome's answer as it sent it: with a Content-Length only when it gave one (a
    long answer comes without)."""
    if any(k.lower() == b"content-length" for k, _ in refusal.headers):
        await send_bytes(send, refusal.status, refusal.body, refusal.headers, head=head)
        return
    start = {"type": "http.response.start", "status": refusal.status, "headers": refusal.headers}
    try:
        await send(start)
        await send({"type": "http.response.body", "body": b"" if head else refusal.body})
    except OSError:
        log.debug("client disconnected")


async def _prepend(first: bytes, rest: AsyncIterator[bytes] | None) -> AsyncIterator[bytes]:
    yield first
    if rest is not None:
        async for chunk in rest:
            yield chunk
