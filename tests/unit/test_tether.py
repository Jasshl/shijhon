"""The harness's processes end with the process that started them - also one killed outright
(a pytest worker): its test Navidromes used to stay behind for good. And a stop returns only
once the command itself is gone, whatever became of its tether."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.harness.navidrome import TETHER, start_tethered, stop_group


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(condition: Callable[[], object], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.05)


@contextlib.contextmanager
def cleaned(process: subprocess.Popen[bytes], pidfile: Path, pipe: int = -1) -> Iterator[list[int]]:
    """Whatever the test finds: its helper and the command it started are gone after, and
    the pipe closed (yielded in a list: taken out by a test that hands it to a stop)."""
    pipes = [pipe] if pipe >= 0 else []
    try:
        yield pipes
    finally:
        if process.returncode is None:  # (in a session of its own: its group, then)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.kill()
            process.wait()
        text = pidfile.read_text().strip() if pidfile.exists() else ""
        if text and int(text) > 1 and alive(int(text)):
            os.kill(int(text), signal.SIGKILL)
        for left in pipes:
            os.close(left)
        if process.stdin is not None:
            process.stdin.close()


def tethered(
    tmp_path: Path, script: str, grace: float | None = None
) -> tuple[subprocess.Popen[bytes], int, Path]:
    """A Python ``script`` (given the path to write its process ID to) run through the
    tether as the harness runs Navidrome; the tether, its "alive" pipe, the ID's file."""
    pidfile = tmp_path / "pid"
    command = tmp_path / "command.py"
    command.write_text(textwrap.dedent(script))
    tether, pipe = start_tethered([sys.executable, str(command), str(pidfile)], grace=grace)
    return tether, pipe, pidfile


def started(pidfile: Path) -> int:
    wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
    return int(pidfile.read_text())


IDLE = """
    import os, sys, time
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(600)
"""
STUBBORN = """
    import os, signal, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(600)
"""
SLOW = """
    import os, signal, sys, time
    def stop(*_):
        time.sleep(1.0)  # e.g. closing its database
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(600)
"""


def test_a_tethered_command_ends_when_its_starter_is_killed(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    command = tmp_path / "command.py"
    command.write_text(textwrap.dedent(IDLE))
    script = tmp_path / "starter.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys, time
            started = subprocess.Popen(
                [sys.executable, {str(TETHER)!r}, sys.executable, {str(command)!r},
                 {str(pidfile)!r}],
                stdin=subprocess.PIPE,
                start_new_session=True,
            )
            time.sleep(600)
            """
        )
    )
    starter = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    with cleaned(starter, pidfile):
        child = started(pidfile)
        starter.kill()  # as a pytest worker is killed: nothing of its own runs
        starter.wait()
        wait_for(lambda: not alive(child))


def test_a_tethered_command_that_ignores_the_stop_is_killed(tmp_path: Path) -> None:
    tether, pipe, pidfile = tethered(tmp_path, STUBBORN, grace=0.5)
    with cleaned(tether, pidfile, pipe):
        child = started(pidfile)
        assert tether.stdin is not None
        tether.stdin.close()  # the starter is gone
        assert tether.wait(timeout=10) == 128 + signal.SIGKILL
        wait_for(lambda: not alive(child))


def test_the_tether_ends_with_its_command_and_passes_its_status_on() -> None:
    tether = subprocess.Popen(
        [sys.executable, str(TETHER), "sh", "-c", "exit 7"], stdin=subprocess.PIPE
    )
    try:
        assert tether.wait(timeout=10) == 7
    finally:
        tether.kill()
        tether.wait()


def test_a_stop_waits_for_the_command_itself(tmp_path: Path) -> None:
    """The command takes a moment to end (Navidrome closing its database): the stop returns
    only once it is gone - its data folder may be used again at once."""
    tether, pipe, pidfile = tethered(tmp_path, SLOW)
    with cleaned(tether, pidfile, pipe) as pipes:
        child = started(pidfile)
        began = time.monotonic()
        stop_group(tether, pipes[0])
        pipes.clear()  # (closed by the stop)
        assert not alive(child) and time.monotonic() - began >= 0.9
        assert tether.returncode == 0


def test_a_stop_ends_the_command_also_when_its_tether_died(tmp_path: Path) -> None:
    tether, pipe, pidfile = tethered(tmp_path, IDLE)
    with cleaned(tether, pidfile, pipe) as pipes:
        child = started(pidfile)
        os.kill(tether.pid, signal.SIGKILL)  # the tether alone (not waited for)
        time.sleep(0.3)
        assert alive(child)
        stop_group(tether, pipes[0])
        pipes.clear()  # (closed by the stop)
        wait_for(lambda: not alive(child), 5.0)  # (its tether is gone: collected by init)


def test_a_stop_signals_nobody_once_the_tether_was_waited_for(tmp_path: Path) -> None:
    """Its process ID - the group's - may be another process's by then: a command that
    outlived such a tether is reported, never signaled by that ID."""
    tether, pipe, pidfile = tethered(tmp_path, IDLE)
    with cleaned(tether, pidfile, pipe):
        child = started(pidfile)
        tether.kill()
        tether.wait()
        with pytest.raises(RuntimeError):
            stop_group(tether, pipe)
        assert alive(child)  # (the cleanup ends it, by its own process ID)


def test_a_stop_kills_a_command_that_does_not_end(tmp_path: Path) -> None:
    tether, pipe, pidfile = tethered(tmp_path, STUBBORN, grace=60)
    with cleaned(tether, pidfile, pipe) as pipes:
        child = started(pidfile)
        stop_group(tether, pipes[0], grace=0.5)
        pipes.clear()  # (closed by the stop)
        wait_for(lambda: not alive(child), 5.0)  # (killed: collected a moment later)


def test_a_stop_of_a_command_that_ended_already_signals_nobody(tmp_path: Path) -> None:
    """The command ended by itself and its tether was waited for: the group's ID may be
    another process's by now - nothing is signaled, the stop just returns."""
    tether, pipe, pidfile = tethered(tmp_path, "import sys  # (it ends at once)")
    with cleaned(tether, pidfile, pipe) as pipes:
        assert tether.wait(timeout=10) == 0
        signaled: list[int] = []
        real = os.killpg
        os.killpg = lambda group, number: signaled.append(group)  # type: ignore[assignment]
        try:
            stop_group(tether, pipes[0])
            pipes.clear()  # (closed by the stop)
        finally:
            os.killpg = real
        assert signaled == []


def test_a_stop_while_the_command_is_being_started_leaves_nothing_behind(tmp_path: Path) -> None:
    """A stop of the group in the tether's first moments - before the command is in it -
    must not end the tether alone: the command would run on with nothing to stop it."""
    for attempt in range(25):
        tether, pipe, pidfile = tethered(tmp_path, IDLE)
        with cleaned(tether, pidfile, pipe):
            time.sleep(0.004 * attempt)  # from at once to after the command has started
            os.killpg(tether.pid, signal.SIGTERM)
            readable, _, _ = select.select([pipe], [], [], 10.0)  # ends when all are gone
            assert readable and os.read(pipe, 1) == b""
        pidfile.unlink(missing_ok=True)
