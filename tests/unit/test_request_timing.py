"""The access log's timings of a request: its answer's start, its first body bytes and
the client's leaving, each told apart (``proxy/timing.py``)."""

from __future__ import annotations

from typing import Any

import pytest

from shijhon.proxy.timing import RequestTiming

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def setup() -> tuple[Clock, RequestTiming, list[dict[str, Any]]]:
    clock = Clock()
    timing = RequestTiming(clock)
    sent: list[dict[str, Any]] = []
    return clock, timing, sent


async def test_headers_long_before_the_first_body_bytes_are_two_numbers() -> None:
    """A ranged answer whose headers go out at once, its audio only later: the app waits
    for the second number."""
    clock, timing, sent = setup()

    async def send(message: Any) -> None:
        sent.append(message)

    out = timing.send(send)
    clock.now = 0.2
    await out({"type": "http.response.start", "status": 206, "headers": []})
    clock.now = 0.3
    await out({"type": "http.response.body", "body": b"", "more_body": True})  # not a byte
    clock.now = 4.5
    await out({"type": "http.response.body", "body": b"ID3", "more_body": True})
    clock.now = 9.0
    await out({"type": "http.response.body", "body": b"more", "more_body": False})
    assert timing.line() == (206, 9000.0, "start sent 200ms, first body sent 4500ms")


async def test_an_answer_without_a_body_has_no_first_body_byte() -> None:
    clock, timing, _ = setup()

    async def send(message: Any) -> None:
        pass

    out = timing.send(send)
    clock.now = 0.05
    await out({"type": "http.response.start", "status": 200, "headers": []})  # a HEAD
    await out({"type": "http.response.body", "body": b"", "more_body": False})
    assert timing.line()[2] == "start sent 50ms, no body sent"


async def test_a_send_that_failed_is_not_counted() -> None:
    clock, timing, _ = setup()

    async def send(message: Any) -> None:
        raise OSError("gone")

    out = timing.send(send)
    clock.now = 1.0
    with pytest.raises(OSError):
        await out({"type": "http.response.start", "status": 206, "headers": []})
    assert timing.line() == (0, 1000.0, "no start sent")


async def test_a_client_that_left_before_any_answer_is_told() -> None:
    """A routing the client gave up before its first byte: what is sent after it left (the
    server drops it without an error) is not an answer it got."""
    clock, timing, _ = setup()

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        pass

    clock.now = 3.0
    assert (await timing.receive(receive)())["type"] == "http.disconnect"
    clock.now = 5.0
    await timing.send(send)({"type": "http.response.start", "status": 200, "headers": []})
    assert timing.line() == (0, 5000.0, "no start sent, disconnect seen 3000ms")


async def test_a_client_that_left_while_reading_has_all_three() -> None:
    clock, timing, _ = setup()

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        pass

    out = timing.send(send)
    clock.now = 1.0
    await out({"type": "http.response.start", "status": 206, "headers": []})
    clock.now = 1.5
    await out({"type": "http.response.body", "body": b"x", "more_body": True})
    clock.now = 60.0
    await timing.receive(receive)()
    assert timing.line()[2] == "start sent 1000ms, first body sent 1500ms, disconnect seen 60000ms"
