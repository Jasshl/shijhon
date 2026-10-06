"""Runs a command tied to the process that started this one: when that process goes away -
also killed outright, as a pytest worker can be - the command is stopped too.

The starter holds the write end of this process's standard input and never writes to it:
the end of input means it is gone (the kernel closes its files, whatever ended it). The
command then gets SIGTERM and, if it is still running after a grace period, SIGKILL. A
SIGTERM to this process's group (the starter's own stop) reaches the command too; this
process waits for the command and exits with its status. Standard library only.

One case is not covered: this process itself killed outright (SIGKILL to it alone) leaves
the command running until the starter stops its process group.

With ``--alive-fd N`` the command inherits that descriptor too (the write end of a pipe of
the starter's): the pipe ends when this process, the command and everything it started are
gone - how the starter knows, without process IDs, that nothing of it is left.

    python tether.py [--grace SECONDS] [--alive-fd N] <command> [args...]
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

GRACE_SECONDS = 20.0


def main(argv: list[str]) -> int:
    grace, alive = GRACE_SECONDS, []
    while argv[:1] in (["--grace"], ["--alive-fd"]):
        if argv[0] == "--grace":
            grace = float(argv[1])
        else:
            alive = [int(argv[1])]
        argv = argv[2:]
    # The group's SIGTERM (the starter's stop) reaches the command as well: this process
    # waits for it. Set before the command starts - a stop at that moment must not end this
    # process alone, and is passed on to the command once it is there - and not inherited
    # by it (a handler is reset when a program starts).
    stopping: list[int] = []
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda signum, frame: stopping.append(signum))
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, pass_fds=alive)
    if stopping:  # stopped while the command was being started: it may have missed it
        child.terminate()

    def watch() -> None:
        while True:
            try:
                if not os.read(0, 4096):
                    break
            except InterruptedError:
                continue
            except OSError:
                break
        if child.poll() is None:  # the starter is gone: so is the command
            child.terminate()
            try:
                child.wait(grace)
            except subprocess.TimeoutExpired:
                child.kill()

    threading.Thread(target=watch, daemon=True).start()
    status = child.wait()
    return status if status >= 0 else 128 - status  # ended by a signal: as a shell says


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
