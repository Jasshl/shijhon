"""Run an ASGI app under uvicorn on a real socket in a background thread.

A real socket matters: header framing (``Content-Length`` vs chunked) and client
disconnects are only observable over HTTP, not through an in-process transport.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import uvicorn

T = TypeVar("T")


class RunningServer:
    def __init__(
        self, app: Any, *, lifespan: str = "on", host: str = "127.0.0.1", port: int | None = None
    ) -> None:
        self.host = host
        # Bound here and handed to the server: a port picked now and bound later could be
        # taken meanwhile by another test process's connection or server.
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((host, port or 0))
        self.port = int(self.socket.getsockname()[1])
        self.config = uvicorn.Config(
            app,
            host=host,
            port=self.port,
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
            proxy_headers=False,
            lifespan=lifespan,
            timeout_graceful_shutdown=5,
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 15.0) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)

    def call(self, make: Callable[[], Coroutine[Any, Any, T]], timeout: float = 60.0) -> T:
        """Run a coroutine on the served app's own event loop (it must expose ``loop``)."""
        loop = self.config.app.loop
        return asyncio.run_coroutine_threadsafe(make(), loop).result(timeout)

    def stop(self) -> None:
        self.server.should_exit = True
        try:
            self.thread.join(timeout=15)
        finally:
            self.socket.close()  # (the server closes it too, when it ran)


@contextmanager
def running(app: Any, *, lifespan: str = "on") -> Iterator[RunningServer]:
    server = RunningServer(app, lifespan=lifespan)
    server.start()
    try:
        yield server
    finally:
        server.stop()
