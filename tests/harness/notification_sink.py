"""A stand-in for a notification service: an HTTP server on loopback that Apprise's
``json://`` addresses post to. It keeps what it received (path, headers, JSON body) and
answers with ``status``. No test sends to a real service."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# An invented token, in the address's path as services put theirs: it may appear in no
# page, log line or notification text.
TOKEN = "sinktoken-5d1f0c3a9b77e2"


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


class NotificationSink:
    def __init__(self) -> None:
        self.received: list[Received] = []
        self.status = 200
        self.arrived = threading.Event()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"raw": raw.decode(errors="replace")}
                sink.received.append(Received(self.path, dict(self.headers), body))
                sink.arrived.set()
                self.send_response(sink.status)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def address(self, token: str = TOKEN) -> str:
        """An Apprise address that posts here, with ``token`` in its path."""
        return f"json://127.0.0.1:{self.port}/hook/{token}"

    def start(self) -> NotificationSink:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def wait(self, count: int, seconds: float = 10.0) -> list[Received]:
        """Until ``count`` notifications arrived (AssertionError after ``seconds``)."""
        import time

        deadline = time.monotonic() + seconds
        while len(self.received) < count:
            if time.monotonic() > deadline:
                raise AssertionError(f"{len(self.received)} of {count} notifications arrived")
            time.sleep(0.02)
        return self.received

    def clear(self) -> None:
        self.received.clear()
        self.arrived.clear()
