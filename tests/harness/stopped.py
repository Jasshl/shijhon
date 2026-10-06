"""``shijhon serve``'s own serving code (``cli.serve``) around a minimal application, in a
process of its own: what a stop and a restart do to the process, without a Navidrome. Run
by ``tests/unit/test_restart.py``:

    python tests/harness/stopped.py <case> <port> <state folder>

- ``executor``: at its start the application leaves a job in the event loop's default
  executor, as a name lookup that a stop cut off does. The test stops the process with a
  signal.
- ``restart``: a restart is asked for once the application has started.
- ``stuck``: the same, and the application's shutdown never ends: the restart's bound
  (a second here) ends the process.

The process says on its output how far it came.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

from shijhon import cli
from shijhon.config import load_settings


def say(line: str) -> None:
    print(line, flush=True)


class App:
    """A lifespan and nothing else; ``restarter`` is what ``serve`` gives the real one."""

    restarter: Any = None
    restart_check: Any = None

    def __init__(self, case: str) -> None:
        self.case = case

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            return
        loop = asyncio.get_running_loop()
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                if self.case == "executor":
                    loop.run_in_executor(None, time.sleep, 30)
                elif self.restarter is not None:
                    loop.call_later(0.3, self.restarter)
                await send({"type": "lifespan.startup.complete"})
                say("started")
            elif message["type"] == "lifespan.shutdown":
                say("closing")
                if self.case == "stuck":
                    await asyncio.Event().wait()
                await send({"type": "lifespan.shutdown.complete"})
                say("closed")
                return


if __name__ == "__main__":
    case, port, state = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    logging.basicConfig(level=logging.INFO)
    app = App(case)
    cli.create_app = lambda settings: app  # type: ignore[assignment, return-value]
    if case == "stuck":
        cli.RESTART_BOUND = 1.0
    cli.serve(
        load_settings(None, state_dir=state, server={"port": port, "restart_by_supervisor": True})
    )
    say("returned")
