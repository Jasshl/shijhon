"""How long a request took, for its line in the access log.

Three moments of the answer as Shijhon handed it to the server (an ASGI send that
completed) are told apart: its start sent (status and headers), its first body bytes sent,
and the client's disconnect as Shijhon saw it (an ``http.disconnect`` it read; nothing
reads one while a request waits for a lookup, so a client may be gone earlier). A send
that completed is not proof that the client received it: once the connection is closed,
the server drops sends without an error - so none counts after a disconnect seen. An app
that shows a song as paused waits for the first body bytes: headers can go out long before
any audio. A HEAD, or an answer without a body, sends no body bytes.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from shijhon.proxy.upstream import Message, Receive, Send


class RequestTiming:
    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self.clock = clock
        self.started = clock()
        self.status = 0  # the status sent (0: none)
        self.answered: float | None = None
        self.body: float | None = None
        self.left: float | None = None

    def send(self, send: Send) -> Send:
        async def timed(message: Message) -> None:
            await send(message)
            if self.left is not None:
                return
            kind = message["type"]
            if kind == "http.response.start" and self.answered is None:
                self.status = int(message["status"])
                self.answered = self.clock()
            elif kind == "http.response.body" and message.get("body") and self.body is None:
                self.body = self.clock()

        return timed

    def receive(self, receive: Receive) -> Receive:
        async def timed() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect" and self.left is None:
                self.left = self.clock()
            return message

        return timed

    def line(self) -> tuple[int, float, str]:
        """The status sent (0: none), the whole request's milliseconds, and its moments."""
        end = self.clock()

        def ms(at: float) -> float:
            return (at - self.started) * 1000

        if self.answered is None:
            parts = ["no start sent"]
        else:
            parts = [f"start sent {ms(self.answered):.0f}ms"]
            parts.append(f"first body sent {ms(self.body):.0f}ms" if self.body else "no body sent")
        if self.left is not None:
            parts.append(f"disconnect seen {ms(self.left):.0f}ms")
        return self.status, ms(end), ", ".join(parts)
