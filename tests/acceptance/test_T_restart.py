"""The dashboard's "Restart Shijhon", for real: ``shijhon serve`` as a process of its own,
in front of a real Navidrome, with the test in the place of whatever runs Shijhon.

Shown: one press stops the process in order and it exits with the restart's status;
started again (as a supervisor does), the port is taken again, the state's lock and the
database are opened again, the saved setting that waited for the restart applies, and the
session still holds. A stop signal after the press is an ordinary stop. A request that
does not end is cut off; a press during a commit that cannot end leaves what the next
start repairs.
"""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest

from shijhon import app as app_module
from shijhon.app import ShijhonApp
from shijhon.config import load_settings
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import PATH, Browser
from tests.harness.engine import catalog_release, engine_for
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance, free_port
from tests.harness.subsonic import SubsonicClient

ROOT = Path(__file__).resolve().parents[2]
STOPPING = "restart: stopping at the dashboard's request"
CLOSED = "Application shutdown complete."
EXITING = "restart: exiting with status 75 at the dashboard's request"
CUT = "timeout graceful shutdown exceeded"
RESTART_STATUS = 75
posix = pytest.mark.skipif(os.name != "posix", reason="signals as POSIX has them")

CONFIG = """
state_dir = "{state}"

[server]
host = "127.0.0.1"
port = {port}
restart_by_supervisor = true

[navidrome]
url = "{url}"
user = "{user}"
password = "{password}"
library_path = "{music}"
"""


class Served:
    """``shijhon serve`` in a process of its own, its log in a file. ``more``: more of the
    configuration; ``waits``: the restart's waits, shortened (``tests/harness/served.py``:
    the same command, started through a script that sets them)."""

    def __init__(self, tmp: Path, nd: NavidromeInstance, *, more: str = "", **waits: float) -> None:
        self.nd = nd
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log = tmp / "shijhon.log"
        self.database = tmp / "state" / "shijhon.sqlite3"
        self.config = tmp / "shijhon.toml"
        self.config.write_text(
            CONFIG.format(
                state=tmp / "state",
                port=self.port,
                url=nd.base_url,
                user=ADMIN_USER,
                password=ADMIN_PASSWORD,
                music=nd.music,
            )
            + more
        )
        self.tmp = tmp
        self.command = [str(Path(sys.executable).parent / "shijhon")]
        self.env = dict(os.environ)
        if waits:
            self.command = [sys.executable, str(ROOT / "tests" / "harness" / "served.py")]
            self.env |= {
                f"SHIJHON_TEST_{name.upper()}": str(value) for name, value in waits.items()
            }
        self.command += ["--config", str(self.config), "serve"]
        self.start()

    def start(self) -> None:
        """Start the process - also again, as whatever runs Shijhon does after it exited."""
        with self.log.open("ab") as out:
            self.process = subprocess.Popen(
                self.command, stdout=out, stderr=subprocess.STDOUT, cwd=self.tmp, env=self.env
            )

    def run(self, *, other_than: str = "", within: float = 30.0) -> str:
        """Which run answers (its ``alive`` text), once one other than ``other_than``
        does."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            assert self.process.poll() is None, self.log.read_text()
            try:
                answer = httpx.get(f"{self.base_url}{PATH}/alive", timeout=2)
                if answer.status_code == 200 and answer.text != other_than:
                    return answer.text
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise AssertionError(self.log.read_text())

    def stop(self) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        return self.ended()

    def ended(self, within: float = 30.0) -> int:
        """The process's exit status, once it has ended."""
        try:
            return self.process.wait(timeout=within)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise

    def said(self, line: str, within: float = 30.0) -> str:
        """The log, once it holds ``line``."""
        deadline = time.monotonic() + within
        while line not in (text := self.log.read_text()):
            assert time.monotonic() < deadline and self.process.poll() is None, text
            time.sleep(0.05)
        return text

    def signed_in(self) -> Browser:
        browser = Browser(self.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        return browser

    def held_request(self) -> socket.socket:
        """A request under way that does not end by itself: a form whose body is not
        sent in full."""
        held = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        held.sendall(
            f"POST {PATH}/sign-in HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            "Content-Length: 64\r\n\r\nusername=".encode()
        )
        return held


@pytest.fixture
def served(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Served]:
    process = Served(tmp_path_factory.mktemp("restart"), navidrome_factory())
    try:
        yield process
    finally:
        process.stop()


def test_one_press_stops_shijhon_in_order_and_the_next_start_has_the_saved_settings(
    served: Served,
) -> None:
    first = served.run()
    browser = Browser(served.base_url)
    try:
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        # A setting that applies after a restart (the catalog's cache time), saved.
        assert browser.submit("catalog", {"cache_seconds": "1234"}).status_code == 303
        page = browser.get("catalog").text
        assert "Restart needed" in page and ">Restart Shijhon</button>" in page
        assert "started again by Docker or your service manager" in browser.get("diagnostics").text
        pressed = browser.submit("diagnostics", {}, "restart")
        assert pressed.status_code == 303 and "/restarting?boot=" in pressed.headers["location"]
        # The process closes in order and exits with the restart's status ...
        assert served.ended() == RESTART_STATUS
        log = served.log.read_text()
        assert log.index(STOPPING) < log.index(CLOSED) < log.index(EXITING)
        # ... and whatever runs Shijhon starts it again: here, the test.
        served.start()
        second = served.run(other_than=first)
        assert second != first
        # The session holds, the page goes back to where the press came from, and the
        # setting that waited applies: nothing waits for a restart any more.
        back = browser.get(pressed.headers["location"])
        assert back.status_code == 303 and back.headers["location"] == f"{PATH}/diagnostics"
        page = browser.get("diagnostics").text
        assert "Restart needed" not in page and "no saved setting is waiting for a restart" in page
        assert 'value="1234"' in browser.get("catalog").text
        # A second restart works like the first (nothing was left behind by the first).
        assert browser.submit("diagnostics", {}, "restart").status_code == 303
        assert served.ended() == RESTART_STATUS
        served.start()
        assert served.run(other_than=second) not in (first, second)
    finally:
        browser.close()
    log = served.log.read_text()
    assert log.count(STOPPING) == 2 and log.count(EXITING) == 2
    assert "Traceback" not in log and "startup failed" not in log
    assert "Address already in use" not in log and "already running" not in log.lower()
    # An ordinary stop afterwards ends the process as it always did.
    assert served.stop() in (0, -signal.SIGTERM, 128 + signal.SIGTERM)
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{served.base_url}{PATH}/alive", timeout=2)


# --- a stop signal after the press, a request that does not end --------------------------


@posix
def test_a_stop_signal_right_after_the_press_is_an_ordinary_stop(served: Served) -> None:
    """Within the moment the restart gives its answer to reach the browser: the signal
    ends the process as a stop does, not with the restart's status."""
    served.run()
    browser = served.signed_in()
    try:
        assert browser.submit("diagnostics", {}, "restart").status_code == 303
        served.process.send_signal(signal.SIGTERM)
        assert served.ended() == -signal.SIGTERM
    finally:
        browser.close()
    assert EXITING not in served.log.read_text()


@posix
def test_a_stop_signal_while_requests_get_their_seconds_is_an_ordinary_stop(
    served: Served,
) -> None:
    """The server has begun to stop for the restart and a request is still under way: a
    stop signal then ends the process as a stop does."""
    served.run()
    browser = served.signed_in()
    held = served.held_request()
    try:
        assert browser.submit("diagnostics", {}, "restart").status_code == 303
        served.said(STOPPING)
        time.sleep(1.0)  # the request under way keeps the server: its seconds run
        assert served.process.poll() is None
        served.process.send_signal(signal.SIGTERM)
        assert served.ended() == -signal.SIGTERM
    finally:
        held.close()
        browser.close()
    assert EXITING not in served.log.read_text()


def test_a_request_that_does_not_end_is_cut_off_after_the_restart_s_seconds(
    served: Served,
) -> None:
    served.run()
    browser = served.signed_in()
    held = served.held_request()
    try:
        began = time.monotonic()
        assert browser.submit("diagnostics", {}, "restart").status_code == 303
        assert served.ended() == RESTART_STATUS
        assert 5 <= time.monotonic() - began < 25
        while held.recv(4096):  # (whatever it was answered:) closed by the server
            pass
    finally:
        held.close()
        browser.close()
    log = served.log.read_text()
    assert CUT in log and log.index(CUT) < log.index(CLOSED) < log.index(EXITING)


# --- a press while the library is being written --------------------------------------------


@pytest.mark.anyio
async def test_the_restart_waits_for_new_placeholders_being_written(
    navidrome_factory: NavidromeFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app's own wait, with the engine that writes: the server is not stopped while
    new placeholders are on their way into the library, and is stopped once they are in."""
    monkeypatch.setattr(app_module, "RESTART_AFTER", 0.01)
    nd = navidrome_factory()
    release = catalog_release("t-wait", "Waited Album", "Waited Artist", 2)
    app = ShijhonApp(load_settings(None, state_dir=tmp_path / "app", server={"port": 1}))
    stopped: list[bool] = []
    reached, go_on = anyio.Event(), anyio.Event()

    async def pause(step: str) -> None:
        if step == "staged":
            reached.set()
            await go_on.wait()

    async with engine_for(nd, tmp_path) as parts:
        engine = parts.engine
        with anyio.fail_after(5):
            await engine.idle()  # nothing is being written: no wait
        app.services = SimpleNamespace(engine=engine)  # type: ignore[assignment]
        app.restarter = lambda: stopped.append(engine.writing())
        engine.step = pause
        try:
            with anyio.fail_after(120):
                async with anyio.create_task_group() as background:
                    app._background = background
                    background.start_soon(engine.materialize, release)
                    await reached.wait()
                    assert engine.writing() and app.begin_restart()
                    await anyio.sleep(1.5)  # (longer than the engine looks again)
                    assert stopped == [] and app.restarting
                    go_on.set()
        finally:
            engine.step = None
        assert stopped == [False]  # stopped only once nothing was being written
        rows = await parts.store.fetchall("SELECT song_id FROM placeholders")
        assert len(rows) == 2
        assert await parts.store.fetchall("SELECT * FROM pending_placeholders") == []


DEMO = """
[catalog]
kind = "demo"
"""


def pending(database: Path) -> int:
    """How many releases have placeholders that are being written, or were left so."""
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as db:
        return int(db.execute("SELECT count(*) FROM pending_placeholders").fetchone()[0])


def until(what: Any, within: float = 60.0) -> None:
    deadline = time.monotonic() + within
    while not what():
        assert time.monotonic() < deadline
        time.sleep(0.01)


@posix
def test_a_press_during_a_commit_that_cannot_end_leaves_the_library_in_step(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A song of the catalog is being added (a favorite: its album's placeholders are
    on their way into the library) when Navidrome stops answering, and Shijhon is
    restarted. The restart waits for the write, but not for long; the request is cut off
    with the others; the process exits within the stop's bound - and the next start takes
    out what was left half written, so that the song is added whole when it is asked for
    again."""
    nd = navidrome_factory()
    served = Served(
        tmp_path_factory.mktemp("restart-commit"),
        nd,
        more=DEMO,
        write_wait=1.0,  # (seconds the restart waits for the write: 30 otherwise)
        restart_bound=20.0,
        pause_staged=1.0,  # (the write stays at one step long enough to be met there)
    )
    client = SubsonicClient(served.base_url, ADMIN_USER, ADMIN_PASSWORD)
    answers: list[Any] = []
    try:
        first = served.run()
        browser = served.signed_in()
        found = client.ok("search3", {"query": "restless islands", "albumCount": 0})
        song = next(s for s in found["searchResult3"]["song"] if s["id"].startswith("sh.tr."))

        def star() -> None:
            try:
                answers.append(client.error_code("star", {"id": song["id"]}))
            except (httpx.HTTPError, ValueError) as exc:  # (cut off: no Subsonic answer)
                answers.append(type(exc).__name__)

        adding = threading.Thread(target=star)
        adding.start()
        until(lambda: pending(served.database) > 0)  # the files are being written
        assert nd.process is not None
        navidrome = nd.process.pid  # (its process group: the harness starts it so)
        os.killpg(navidrome, signal.SIGSTOP)  # Navidrome answers nothing from here
        try:
            assert browser.submit("diagnostics", {}, "restart").status_code == 303
            served.said("restart: a library write did not end within 1s")
            assert served.ended(within=25) == RESTART_STATUS  # (the bound is 20 s)
        finally:
            os.killpg(navidrome, signal.SIGCONT)
        adding.join(60)
        assert not adding.is_alive() and answers != [None]
        log = served.log.read_text()
        assert CUT in log  # the request was cut off
        served.start()  # (as whatever runs Shijhon does)
        second = served.run(other_than=first, within=60)
        assert second != first
        assert "a stop left half written" in served.log.read_text()[len(log) :]
        # The next start: what was left half written is taken out, and the song is added.
        until(lambda: pending(served.database) == 0)
        assert client.error_code("star", {"id": song["id"]}) is None
        assert pending(served.database) == 0
        starred = client.ok("getStarred2")["starred2"]["song"]
        assert [s["title"] for s in starred] == [song["title"]]
        album = client.ok("getAlbum", {"id": starred[0]["albumId"]})["album"]
        titles = [s["title"] for s in album["song"]]
        assert len(titles) == len(set(titles)) == album["songCount"]
        browser.close()
    finally:
        client.close()
        served.stop()
