"""The credential check without a verdict: Navidrome does not answer, answers
something that is no verdict, or the request that asked is canceled. That is never
"refused" - a request passed on as refused would play a placeholder's silence."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest

from shijhon.delivery.intercept import Interceptor
from shijhon.proxy.app import HandlerResult, ProxyApp, RequestContext
from shijhon.proxy.auth import Caller, CheckFailed, CredentialChecker, Refusal
from shijhon.proxy.params import RestCall

QUERY = b"u=listener&p=secret&v=1.16.1&c=tests&f=json&id=1"


def answer(status: str = "ok", **payload: Any) -> httpx.Response:
    body = {"subsonic-response": {"status": status, "version": "1.16.1", **payload}}
    return httpx.Response(
        200, headers={"content-type": "application/json"}, content=json.dumps(body)
    )


OK = {"user": {"username": "listener"}}
WRONG = {"error": {"code": 40, "message": "Wrong username or password"}}


class Body(httpx.AsyncByteStream):
    """A response body that is read as a stream, as Navidrome's are."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


class Navidrome:
    """A stand-in for Navidrome: answers in turn (the last one again after that), an
    exception is raised. ``hold`` (made inside the event loop): the first request waits
    for ``release``; ``asked`` is set once it is under way."""

    def __init__(self, *answers: Any, hold: bool = False) -> None:
        self.answers = list(answers)
        self.paths: list[bytes] = []
        self.release = anyio.Event() if hold else None
        self.asked = anyio.Event() if hold else None

    async def send(self, method: str, path: bytes, *rest: Any) -> httpx.Response:
        self.paths.append(path)
        if self.release is not None and self.asked is not None and len(self.paths) == 1:
            self.asked.set()
            await self.release.wait()
        found = self.answers[min(len(self.paths), len(self.answers)) - 1]
        if isinstance(found, Exception):
            raise found
        return httpx.Response(found.status_code, headers=found.headers, stream=Body(found.content))


def call() -> RestCall:
    return RestCall.build("stream", "GET", b"/rest/stream", QUERY, [], None)


def checker(navidrome: Navidrome) -> CredentialChecker:
    upstream: Any = navidrome
    return CredentialChecker(upstream)


def test_waiters_of_a_canceled_check_ask_again() -> None:
    """The request whose check others wait for is canceled (its client left, its search
    was superseded): the others are not refused - one of them asks, all get its verdict."""
    verdicts: list[object] = []
    paths: list[bytes] = []

    async def main() -> None:
        navidrome = Navidrome(answer(**OK), hold=True)
        credentials = checker(navidrome)
        assert navidrome.asked is not None

        async def waiter() -> None:
            verdicts.append(await credentials.check(call()))

        async with anyio.create_task_group() as tg:
            owner = anyio.CancelScope()

            async def first() -> None:
                with owner:
                    await credentials.check(call())

            tg.start_soon(first)
            await navidrome.asked.wait()
            for _ in range(3):
                tg.start_soon(waiter)
            await anyio.wait_all_tasks_blocked()  # they wait for the first one's check
            owner.cancel()
        paths.extend(navidrome.paths)

    anyio.run(main)
    assert verdicts == [Caller("listener")] * 3
    assert len(paths) == 2  # the canceled check, and one for all who waited


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), RuntimeError("anything")])
def test_waiters_of_a_failed_check_get_its_failure(error: Exception) -> None:
    """Navidrome does not answer: no verdict for the request that asked, none for those
    that waited (not asked again, one after the other), and no one is "refused"."""
    outcomes: list[object] = []
    paths: list[bytes] = []

    async def main() -> None:
        navidrome = Navidrome(error, hold=True)
        credentials = checker(navidrome)
        assert navidrome.asked is not None and navidrome.release is not None

        async def one() -> None:
            try:
                outcomes.append(await credentials.check(call()))
            except (CheckFailed, RuntimeError) as exc:
                outcomes.append(exc)

        async with anyio.create_task_group() as tg:
            tg.start_soon(one)
            await navidrome.asked.wait()
            for _ in range(3):
                tg.start_soon(one)
            await anyio.wait_all_tasks_blocked()
            navidrome.release.set()
        paths.extend(navidrome.paths)

    anyio.run(main)
    assert len(outcomes) == 4 and all(isinstance(o, Exception) for o in outcomes)
    assert sum(isinstance(o, CheckFailed) for o in outcomes) >= 3  # all who waited
    assert len(paths) == 1


def test_a_check_navidrome_never_answers_ends() -> None:
    """Navidrome takes the connection and says nothing: the check, and those waiting for
    it, end without a verdict instead of waiting for good."""

    async def main() -> None:
        navidrome = Navidrome(answer(**OK), hold=True)  # never released
        upstream: Any = navidrome
        credentials = CredentialChecker(upstream, timeout=0.05)
        with pytest.raises(CheckFailed, match="TimeoutError"):
            await credentials.check(call())

    anyio.run(main)


@pytest.mark.parametrize(
    "said",
    [
        httpx.Response(500, content=b"Internal Server Error"),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>"),
        answer(),  # accepted, but no user named
        answer("failed", error={"code": 50, "message": "User is not authorized"}),
        answer("failed", error={"code": 10, "message": "missing parameter: 'username'"}),
        httpx.ReadTimeout("slow"),
    ],
)
def test_an_answer_that_is_no_verdict_is_a_failed_check(said: Any) -> None:
    credentials = checker(Navidrome(said))

    async def main() -> None:
        with pytest.raises(CheckFailed):
            await credentials.check(call())
        # Nothing is remembered of it: the next request asks again.
        with pytest.raises(CheckFailed):
            await credentials.check(call())

    anyio.run(main)
    assert credentials.pings == 2


def test_a_refusal_is_a_verdict() -> None:
    credentials = checker(Navidrome(answer("failed", **WRONG)))
    assert isinstance(anyio.run(credentials.check, call()), Refusal)


async def through_the_proxy(
    navidrome: Navidrome, handler: Any
) -> tuple[list[dict[str, Any]], RequestContext | None]:
    sent: list[dict[str, Any]] = []
    seen: list[RequestContext] = []
    received: list[bool] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        if received:
            await anyio.sleep_forever()  # the client stays connected
        received.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
        seen.append(ctx)
        return await handler(call, ctx)

    upstream: Any = navidrome
    proxy = ProxyApp(upstream, CredentialChecker(upstream), handlers={"stream": handle})
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/rest/stream",
        "query_string": QUERY,
        "headers": [],
        "client": ("203.0.113.7", 4711),
    }
    await proxy(scope, receive, send)
    return sent, seen[0] if seen else None


def test_a_request_that_needs_a_caller_is_not_forwarded_without_a_verdict() -> None:
    """A placeholder's stream: with no verdict on its credentials it is neither served nor
    forwarded (Navidrome would play the silent file) - a clean error."""
    navidrome = Navidrome(httpx.Response(503, content=b"busy"))

    async def placeholder_stream(call: RestCall, ctx: RequestContext) -> HandlerResult:
        if await ctx.caller() is None:
            return None  # refused: Navidrome's own answer
        raise AssertionError("no caller, no work")

    sent, _ = anyio.run(through_the_proxy, navidrome, placeholder_stream)
    assert sent[0]["status"] == 502
    assert navidrome.paths == [b"/rest/getUser"]  # asked once; the stream never went on


def test_a_failed_check_is_asked_once_a_request() -> None:
    """A handler that goes on without a caller (bookkeeping only) may: the request is
    forwarded, and Navidrome is not asked to check again for the same request."""
    navidrome = Navidrome(httpx.Response(503, content=b"busy"), answer())

    async def bookkeeping(call: RestCall, ctx: RequestContext) -> HandlerResult:
        for _ in range(2):
            with pytest.raises(CheckFailed):
                await ctx.caller()
        return None

    sent, ctx = anyio.run(through_the_proxy, navidrome, bookkeeping)
    assert navidrome.paths == [b"/rest/getUser", b"/rest/stream"]  # one check, then forwarded
    assert sent[0]["status"] == 200 and ctx is not None and ctx.refusal is None


def test_a_play_of_an_owned_song_goes_on_without_a_verdict() -> None:
    """Noting a play's start (fetches ahead) needs a caller; the stream of an owned
    song does not: Navidrome answers for its own file."""

    class Context:
        async def caller(self) -> Caller | None:
            raise CheckFailed("Navidrome does not answer")

    interceptor: Any = SimpleNamespace(warm=object(), ahead=SimpleNamespace(window=5.0))
    context: Any = Context()
    assert anyio.run(Interceptor._note_start, interceptor, call(), context, "song") is None
