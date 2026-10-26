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
if pause := os.environ.get("SHIJHON_TEST_PAUSE_IN_PLACE"):
    created = PlaceholderEngine.__init__
    marker = anyio.Path(os.environ["SHIJHON_TEST_PAUSE_MARKER"])

    def paused(self: PlaceholderEngine, *args: Any, **kwargs: Any) -> None:
        created(self, *args, **kwargs)

        async def step(name: str) -> None:
            if name == "in place" and not await marker.exists():
                await marker.write_text("paused\n")
                print("test: placeholders in place", flush=True)
                await anyio.sleep(float(pause))

        self.step = step

    PlaceholderEngine.__init__ = paused  # type: ignore[method-assign]

if __name__ == "__main__":
    cli.main()
