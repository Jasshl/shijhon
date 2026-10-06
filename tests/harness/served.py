"""``shijhon serve`` for the restart tests: the real command, with what a test names in the
environment - the restart's waits shortened (a stop that does not end is then seen in
seconds), and a pause at one step of a library write (the test gets the time to do
something at that step). A test that starts Shijhon again after a restart runs this
command again, with the same environment."""

from __future__ import annotations

import os
from typing import Any

import anyio

from shijhon import app, cli
from shijhon.placeholders.engine import PlaceholderEngine

if wait := os.environ.get("SHIJHON_TEST_WRITE_WAIT"):
    app.RESTART_WRITE_WAIT = float(wait)
if bound := os.environ.get("SHIJHON_TEST_RESTART_BOUND"):
    cli.RESTART_BOUND = float(bound)
if pause := os.environ.get("SHIJHON_TEST_PAUSE_STAGED"):
    created = PlaceholderEngine.__init__

    def paused(self: PlaceholderEngine, *args: Any, **kwargs: Any) -> None:
        created(self, *args, **kwargs)

        async def step(name: str) -> None:
            if name == "staged":
                await anyio.sleep(float(pause))

        self.step = step

    PlaceholderEngine.__init__ = paused  # type: ignore[method-assign]

if __name__ == "__main__":
    cli.main()
