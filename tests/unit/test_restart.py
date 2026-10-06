"""The dashboard's "Restart Shijhon": what the press starts (``ShijhonApp.begin_restart``),
how ``shijhon serve`` ends afterwards (``cli.serve``, ``cli.Restart``: an exit status of
its own, for whatever runs Shijhon to start it again), where it is offered, and the
configuration check before it. The tests at the end run ``serve`` in a process of its own
(``tests/harness/stopped.py``); ``tests/acceptance/test_T_restart.py`` restarts the real
``shijhon serve``."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import anyio
import pytest
import uvicorn
from pydantic import BaseModel, ValidationError

from shijhon import app as app_module
from shijhon import cli
from shijhon.app import ShijhonApp
from shijhon.config import load_settings
from tests.harness.navidrome import free_port


def settings(tmp_path: Path) -> Any:
    return load_settings(None, state_dir=tmp_path, server={"port": 1})


class Engine:
    """As far as a restart looks at the placeholder engine: a write under way."""

    def __init__(self) -> None:
        self.ended = anyio.Event()
        self.waited = False

    async def idle(self) -> None:
        self.waited = True
        await self.ended.wait()


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "RESTART_AFTER", 0.01)
    monkeypatch.setattr(app_module, "RESTART_WRITE_WAIT", 0.3)


@pytest.mark.anyio
async def test_a_restart_is_begun_once_and_only_where_shijhon_can_restart(
    tmp_path: Path, quick: None
) -> None:
    app = ShijhonApp(settings(tmp_path))
    assert not app.can_restart and not app.begin_restart()  # run in a way that cannot
    stopped: list[str] = []
    app.restarter = lambda: stopped.append("stop")
    assert app.can_restart
    assert not app.begin_restart() and not app.restarting  # not running: nothing begun
    async with anyio.create_task_group() as background:
        app._background = background
        assert app.begin_restart() and app.restarting
        assert not app.begin_restart()  # pressed again: nothing more
        assert stopped == []  # the answer to the press goes out first
        await anyio.sleep(0.1)
        assert stopped == ["stop"] and app.restarting
        assert not app.begin_restart() and stopped == ["stop"]


@pytest.mark.anyio
async def test_a_library_write_under_way_ends_before_the_stop(tmp_path: Path, quick: None) -> None:
    app = ShijhonApp(settings(tmp_path))
    stopped: list[str] = []
    app.restarter = lambda: stopped.append("stop")
    engine = Engine()
    app.services = SimpleNamespace(engine=engine)  # type: ignore[assignment]
    async with anyio.create_task_group() as background:
        app._background = background
        assert app.begin_restart()
        await anyio.sleep(0.1)
        assert engine.waited and stopped == []  # new placeholders are being written
        engine.ended.set()
        await anyio.sleep(0.05)
        assert stopped == ["stop"]
    # A write that does not end is not waited for longer than a stop would wait.
    app.restarting = False
    stuck = Engine()
    app.services = SimpleNamespace(engine=stuck)  # type: ignore[assignment]
    async with anyio.create_task_group() as background:
        app._background = background
        assert app.begin_restart()
        await anyio.sleep(0.1)
        assert stopped == ["stop"]
        await anyio.sleep(0.4)
        assert stopped == ["stop", "stop"]


@pytest.mark.anyio
async def test_a_stop_that_came_during_the_wait_is_no_restart(tmp_path: Path, quick: None) -> None:
    app = ShijhonApp(settings(tmp_path))
    stopped: list[str] = []
    told: list[bool] = [False]
    app.restarter = lambda: stopped.append("stop")
    app.stopping = lambda: told[0]
    async with anyio.create_task_group() as background:
        app._background = background
        assert app.begin_restart()
        told[0] = True  # a stop signal, within the moment before the press's stop
        await anyio.sleep(0.1)
        assert stopped == [] and not app.restarting
        assert not app.begin_restart()  # ... and none is begun while it stops
        # The last step itself can find the server stopping, or fail: nothing is begun
        # then either (the app is not left "restarting": its lock is released at the stop).
        told[0] = False
        app.restarter = lambda: False
        assert app.begin_restart()
        await anyio.sleep(0.1)
        assert not app.restarting

    def fail() -> None:
        raise RuntimeError("can't start new thread")

    app.restarter = fail
    async with anyio.create_task_group() as background:
        app._background = background
        assert app.begin_restart()
        await anyio.sleep(0.1)
        assert not app.restarting


@pytest.mark.anyio
async def test_the_writer_s_lock_is_kept_until_the_process_is_gone_at_a_restart(
    tmp_path: Path,
) -> None:
    """A restart cuts requests off, and a file operation one of them left in a thread may
    still run: the lock is not released for another writer while the process lives. An
    ordinary stop releases it as before."""
    closed: list[str] = []

    async def lock() -> None:
        closed.append("lock")

    async def store() -> None:
        closed.append("store")

    for restarting, expected in ((False, ["store", "lock"]), (True, ["store"])):
        app = ShijhonApp(settings(tmp_path))
        app._opened, app._lock, app.restarting = [lock, store], lock, restarting
        closed.clear()
        await app.shutdown()
        assert closed == expected


@pytest.mark.anyio
async def test_a_restart_that_is_not_begun_can_be_asked_for_again(
    tmp_path: Path, quick: None
) -> None:
    app = ShijhonApp(settings(tmp_path))
    app.restarter = lambda: None
    app.services = SimpleNamespace(engine=Engine())  # type: ignore[assignment]
    async with anyio.create_task_group() as background:
        app._background = background
        assert app.begin_restart()
        await anyio.sleep(0.05)
        background.cancel_scope.cancel()  # Shijhon is stopped meanwhile (a stop signal)
    assert not app.restarting


class Bound:
    """The restart's timer, as far as a test lets it go: noted, never run."""

    made: ClassVar[list[Bound]] = []

    def __init__(self, seconds: float, end: Any) -> None:
        self.seconds, self.end = seconds, end
        self.daemon = False
        self.started = False
        Bound.made.append(self)

    def start(self) -> None:
        self.started = True


@pytest.fixture(autouse=True)
def bounds(monkeypatch: pytest.MonkeyPatch) -> list[Bound]:
    """(The real one would end the test run a minute after a test that asked for a
    restart.)"""
    monkeypatch.setattr(cli, "Timer", Bound)
    monkeypatch.setattr(Bound, "made", [])
    return Bound.made


def supervised(tmp_path: Path) -> Any:
    return load_settings(
        None, state_dir=tmp_path, server={"port": 1, "restart_by_supervisor": True}
    )


def run_as(
    started: bool, press: bool, signals: tuple[int, ...] = (), interrupt: bool = False
) -> Any:
    """``uvicorn.Server.run`` for a test: no server - it "starts", the restart is asked
    for (or not), and it returns as it does once the server has stopped. ``signals``: the
    stop signals it took meanwhile; ``interrupt``: it ends with an interrupt."""

    def run(self: uvicorn.Server, sockets: Any = None) -> None:
        self.started = started
        if press:
            self.config.app.restarter()
            assert self.should_exit and self.config.timeout_graceful_shutdown == cli.RESTART_GRACE
        self._captured_signals.extend(signals)
        if interrupt:
            raise KeyboardInterrupt

    return run


def test_serve_exits_with_the_restart_s_status_after_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bounds: list[Bound],
) -> None:
    monkeypatch.setattr(uvicorn.Server, "run", run_as(True, True))
    with caplog.at_level(logging.INFO, logger="shijhon.cli"), pytest.raises(SystemExit) as ended:
        cli.serve(supervised(tmp_path))
    assert ended.value.code == cli.RESTART_STATUS == 75
    assert "restart: exiting with status 75 at the dashboard's request" in caplog.text
    # The stop has a bound from when it is asked for: then the process ends as it is, with
    # the same status - by a timer that keeps nothing alive.
    (bound,) = bounds
    assert bound.started and bound.daemon and bound.seconds == cli.RESTART_BOUND == 60
    ended_with: list[int] = []
    monkeypatch.setattr(os, "_exit", ended_with.append)
    bound.end()
    assert ended_with == [cli.RESTART_STATUS]
    # ... unless a stop signal came meanwhile: that stop is not a restart's any more.
    bound.end.__self__.server._captured_signals.append(signal.SIGTERM)
    bound.end()
    assert ended_with == [cli.RESTART_STATUS]


def test_serve_ends_as_before_when_it_is_stopped_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounds: list[Bound]
) -> None:
    """As ``uvicorn.run`` ends: quietly after a stop or an interrupt, with status 3 after
    a start that failed - also when a restart was asked for and a stop signal came."""
    monkeypatch.setattr(uvicorn.Server, "run", run_as(True, False))
    cli.serve(supervised(tmp_path))
    monkeypatch.setattr(uvicorn.Server, "run", run_as(True, False, interrupt=True))
    cli.serve(supervised(tmp_path))
    assert bounds == []
    monkeypatch.setattr(uvicorn.Server, "run", run_as(False, True))
    with pytest.raises(SystemExit) as failed:
        cli.serve(supervised(tmp_path))
    assert failed.value.code == 3
    assert hasattr(uvicorn.Server(uvicorn.Config(lambda: None)), "_captured_signals")
    monkeypatch.setattr(uvicorn.Server, "run", run_as(True, True, (signal.SIGTERM,)))
    cli.serve(supervised(tmp_path))  # a stop: no status of a restart


def test_no_restart_is_begun_while_the_server_is_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounds: list[Bound]
) -> None:
    """A stop signal first, then the press's last step: the stop stays a stop - no status,
    no bound, no shorter time for the requests under way."""

    def run(self: uvicorn.Server, sockets: Any = None) -> None:
        self.started = True
        self.should_exit = True  # (as the server's handler of a stop signal sets it)
        assert self.config.app.stopping() and not self.config.app.begin_restart()
        assert self.config.app.restarter() is False
        assert self.config.timeout_graceful_shutdown is None

    monkeypatch.setattr(uvicorn.Server, "run", run)
    cli.serve(supervised(tmp_path))
    assert bounds == []


def test_a_bound_that_cannot_be_started_begins_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No thread can be started: the server is not told to stop - no restart without its
    bound, and nothing half begun."""

    def refuse(self: Bound) -> None:
        raise RuntimeError("can't start new thread")

    def run(self: uvicorn.Server, sockets: Any = None) -> None:
        self.started = True
        with pytest.raises(RuntimeError):
            self.config.app.restarter()
        assert not self.should_exit and self.config.timeout_graceful_shutdown is None

    monkeypatch.setattr(Bound, "start", refuse)
    monkeypatch.setattr(uvicorn.Server, "run", run)
    cli.serve(supervised(tmp_path))  # (no status of a restart)


def test_serve_refuses_workers_as_uvicorn_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setattr(uvicorn.Server, "run", run_as(True, False))
    with pytest.raises(SystemExit) as refused:
        cli.serve(supervised(tmp_path))
    assert refused.value.code == 3


@pytest.mark.parametrize(
    ("started", "should_reload", "workers", "restart_requested", "stop_captured", "status"),
    [
        (True, False, 1, False, False, None),  # stopped, or interrupted
        (False, False, 1, False, False, 3),  # the start failed
        (False, False, 0, False, False, None),  # ... as uvicorn.run: only with one worker
        (False, False, -1, False, False, None),
        (False, True, 1, False, False, None),  # ... and not while reloading
        (True, False, 1, True, False, 75),  # the dashboard's restart
        (True, False, 0, True, False, 75),
        (True, False, 1, True, True, None),  # a stop signal came: a stop
        (True, False, 1, False, True, None),
        (False, False, 1, True, False, 3),  # (a start that failed is said first)
        (False, False, 0, True, True, None),
    ],
)
def test_the_status_serve_ends_with(
    started: bool,
    should_reload: bool,
    workers: int,
    restart_requested: bool,
    stop_captured: bool,
    status: int | None,
) -> None:
    ended = cli.final_status(
        started=started,
        should_reload=should_reload,
        workers=workers,
        restart_requested=restart_requested,
        stop_captured=stop_captured,
    )
    assert ended == status


@pytest.mark.parametrize("workers", [None, "1", "0", "-1"])
def test_a_start_that_never_happened_ends_as_uvicorn_run_ends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: str | None
) -> None:
    """An interrupt before the server has started, no restart asked for: ``serve`` ends
    with what ``uvicorn.run`` itself ends with - status 3 with one worker, quietly with
    ``WEB_CONCURRENCY`` 0 or below (which both take as one server)."""
    if workers is None:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", workers)
    monkeypatch.setattr(uvicorn.Server, "run", run_as(False, False, interrupt=True))

    def ended(run: Any) -> int | str | None:
        try:
            run()
        except SystemExit as exc:
            return exc.code
        return None

    async def nothing(scope: Any, receive: Any, send: Any) -> None:
        return None

    theirs = ended(lambda: uvicorn.run(nothing, port=1, log_config=None, lifespan="on"))
    ours = ended(lambda: cli.serve(supervised(tmp_path)))
    assert ours == theirs == (None if workers in ("0", "-1") else 3)


def test_serve_leaves_the_signals_to_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No handler of its own, before, while or after the server runs."""
    seen: list[Any] = []
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def run(self: uvicorn.Server, sockets: Any = None) -> None:
        self.started = True
        seen.append({sig: signal.getsignal(sig) for sig in handlers})

    monkeypatch.setattr(uvicorn.Server, "run", run)
    cli.serve(supervised(tmp_path))
    assert seen == [handlers] and handlers == {sig: signal.getsignal(sig) for sig in handlers}


def test_the_restart_is_offered_only_where_something_starts_shijhon_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the first process of a container (its restart policy), or where the
    configuration says a supervisor does."""
    plain = settings(tmp_path)
    assert not plain.server.restart_by_supervisor  # off unless said
    assert not cli.started_again(plain) and not cli.started_again(plain, pid=4321)
    assert cli.started_again(plain, pid=1)
    assert cli.started_again(supervised(tmp_path), pid=4321)
    offered: list[bool] = []

    def run(self: uvicorn.Server, sockets: Any = None) -> None:
        self.started = True
        offered.append(self.config.app.can_restart)

    monkeypatch.setattr(uvicorn.Server, "run", run)
    cli.serve(plain)
    cli.serve(supervised(tmp_path))
    monkeypatch.setattr(os, "getpid", lambda: 1)
    cli.serve(plain)
    assert offered == [False, True, True]


# --- the configuration, read before anything is stopped ----------------------------------

FILE = """
state_dir = "{state}"
[server]
port = {port}
{server}
[navidrome]
url = "http://127.0.0.1:1"
user = "service"
password = "a-test-password"
library_path = "{state}"
{more}
"""


def test_the_configuration_is_read_before_a_restart_stops_anything(tmp_path: Path) -> None:
    """As the next start reads it: one that would fail there, or bring Shijhon back at
    another address, is said - without the values, which may be secrets."""
    config = tmp_path / "shijhon.toml"

    def written(port: int = 4000, more: str = "", server: str = "") -> None:
        config.write_text(FILE.format(state=tmp_path, port=port, more=more, server=server))

    written()
    running = load_settings(config)
    assert cli.restart_problem(config, running) is None
    written(more="[search]\nbudget_seconds = 4000")
    problem = cli.restart_problem(config, running) or ""
    assert problem.startswith("the configuration has an error - in search.budget_seconds:")
    assert "4000" not in problem
    # What a setting holds is never repeated, also not by a check that quotes it.
    written(server='trusted_proxies = ["a-secret-token.example"]')
    problem = cli.restart_problem(config, running) or ""
    assert problem.startswith("the configuration has an error - in server.trusted_proxies")
    assert "secret" not in problem and "not accepted" in problem
    written()  # ... nor a name that is the file's own
    config.write_text('a_private_name = "x"\n' + config.read_text())
    problem = cli.restart_problem(config, running) or ""
    assert problem == "the configuration has an error - a setting that does not exist"
    written(port=4001)
    problem = cli.restart_problem(config, running) or ""
    assert "another address or port" in problem and "4001" not in problem
    config.write_text("[server\nport = ")
    assert cli.restart_problem(config, running) == (
        "the configuration file cannot be read (TOMLDecodeError)"
    )
    written()
    assert cli.restart_problem(config, running) is None


def test_a_refusal_names_no_key_of_a_table_either() -> None:
    """Below the section and the setting, a name can be the configuration's own."""

    class Section(BaseModel):
        headers: dict[str, int] = {}

    class Configured(BaseModel):
        catalog: Section

    with pytest.raises(ValidationError) as refused:
        Configured.model_validate({"catalog": {"headers": {"A-SECRET-NAME": "x", "B": "y"}}})
    assert cli._problems(refused.value, own=False) == [
        "in catalog.headers: Input should be a valid integer, unable to parse string as an integer"
    ]
    assert "A-SECRET-NAME" in " ".join(cli._problems(refused.value))  # (a start's own words)


@pytest.mark.anyio
async def test_the_app_asks_what_serve_gave_it(tmp_path: Path) -> None:
    app = ShijhonApp(settings(tmp_path))
    assert app.restart_problem() is None  # (nothing to ask: embedded)
    app.restart_check = lambda: "a reason"
    assert app.restart_problem() == "a reason"


# --- serve in a process of its own -------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


class Stopped:
    """``tests/harness/stopped.py``, running; its output line by line."""

    def __init__(self, case: str, tmp_path: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "tests/harness/stopped.py", case, str(free_port()), str(tmp_path)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def said(self, line: str) -> None:
        assert self.process.stdout is not None
        assert self.process.stdout.readline().strip() == line

    def ended(self, within: float) -> tuple[int, list[str], str]:
        """(Its exit status, what else it said, its log.)"""
        try:
            out, log = self.process.communicate(timeout=within)
        except subprocess.TimeoutExpired:
            self.process.kill()
            out, log = self.process.communicate()
            raise AssertionError(f"still running: {out!r} {log[-2000:]!r}") from None
        return self.process.returncode, out.split(), log


@pytest.mark.skipif(os.name != "posix", reason="signals as POSIX has them")
def test_a_termination_signal_ends_the_process_also_with_work_left_in_the_executor(
    tmp_path: Path,
) -> None:
    """A name lookup that a stop cut off stays in the event loop's executor until the
    system answers it. A termination signal ends the process once the application has
    closed, as under ``uvicorn.run``: it does not wait for that job (30 seconds here)."""
    served = Stopped("executor", tmp_path)
    served.said("started")
    time.sleep(0.5)
    began = time.monotonic()
    served.process.send_signal(signal.SIGTERM)
    status, said, log = served.ended(within=15)
    assert said == ["closing", "closed"], log  # closed in order - and gone at once
    assert status == -signal.SIGTERM and time.monotonic() - began < 10


def test_a_restart_ends_the_process_with_its_status_after_an_orderly_close(
    tmp_path: Path,
) -> None:
    served = Stopped("restart", tmp_path)
    served.said("started")
    status, said, log = served.ended(within=30)
    assert status == cli.RESTART_STATUS and said == ["closing", "closed"]
    assert "restart: exiting with status 75 at the dashboard's request" in log


def test_a_stop_for_a_restart_that_does_not_end_is_ended_by_its_bound(tmp_path: Path) -> None:
    """The application's shutdown hangs: after the bound (a second here) the process ends
    as it is, with the restart's status."""
    served = Stopped("stuck", tmp_path)
    served.said("started")
    began = time.monotonic()
    status, said, _ = served.ended(within=30)
    assert status == cli.RESTART_STATUS and said == ["closing"]
    assert 1.0 <= time.monotonic() - began < 15


@pytest.mark.skipif(os.name != "posix", reason="signals as POSIX has them")
def test_a_stop_signal_takes_the_bound_away_from_a_restart(tmp_path: Path) -> None:
    """The same, and a termination signal comes while the shutdown hangs: the stop is an
    ordinary one from then on - the bound does not end the process with the restart's
    status (whoever sent the signal ends a stop that hangs)."""
    served = Stopped("stuck", tmp_path)
    served.said("started")
    served.said("closing")
    served.process.send_signal(signal.SIGTERM)
    time.sleep(3.0)  # (the bound is a second)
    assert served.process.poll() is None
    served.process.kill()
    assert served.process.wait(timeout=30) == -signal.SIGKILL
