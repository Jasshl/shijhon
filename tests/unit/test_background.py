"""Background work is contained: one task's failure ends that task alone (logged), never the
app's other background work; whatever ends the lifespan, what startup opened is closed; a
use of a placeholder is noted only once it is written."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import shijhon.app
from shijhon.app import ShijhonApp
from shijhon.catalog.artwork import ArtworkIndex
from shijhon.catalog.model import CatalogRef
from shijhon.config import Settings, load_settings
from shijhon.delivery.expiry import DeliveredAudio
from shijhon.navidrome.checks import StartupChecks
from shijhon.navidrome.client import NavidromeError
from shijhon.store import Store


def _settings(tmp_path: Path, **navidrome: Any) -> Settings:
    return load_settings(
        None,
        state_dir=tmp_path / "state",
        navidrome={"url": "http://127.0.0.1:9", "library_path": tmp_path, **navidrome},
    )


@pytest.fixture
def closed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Records the database's close (the last thing shutdown closes of what startup opened);
    a database a test left open is closed after it (its thread would keep pytest alive)."""
    found: list[str] = []
    opened: list[Store] = []
    real_open, real_close = Store.open, Store.close

    async def open_(path: Path) -> Store:
        store = await real_open(path)
        opened.append(store)
        return store

    async def close(self: Store) -> None:
        found.append("store")
        opened.remove(self)
        await real_close(self)

    monkeypatch.setattr(Store, "open", staticmethod(open_))
    monkeypatch.setattr(Store, "close", close)
    yield found
    for store in opened:
        anyio.run(real_close, store)


@pytest.mark.anyio
async def test_a_failing_background_task_leaves_the_others_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, closed: list[str]
) -> None:
    caplog.set_level(logging.DEBUG, logger="shijhon")
    app = ShijhonApp(_settings(tmp_path))
    to_app, messages = anyio.create_memory_object_stream[dict[str, Any]](4)
    sent: list[str] = []
    started = anyio.Event()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])
        if message["type"] == "lifespan.startup.complete":
            started.set()

    async def failing() -> None:
        token = "SEC" + "RET"  # (the frames logged show this line, not the value)
        raise RuntimeError(f"a message with https://addon.example.invalid/x?token={token}")

    running = anyio.Event()
    ran = anyio.Event()
    ended: list[str] = []

    async def long_running() -> None:  # e.g. the library pass, waiting for its next round
        running.set()
        try:
            await anyio.sleep_forever()
        finally:
            ended.append("stopping" if stopping else "too early")

    async def later() -> None:
        ran.set()

    stopping = False
    async with anyio.create_task_group() as group:
        group.start_soon(app._lifespan, messages.receive, send)
        await to_app.send({"type": "lifespan.startup"})
        with anyio.fail_after(5):
            await started.wait()
        app.spawn(long_running)
        await running.wait()
        app.spawn(failing)
        app.spawn(failing)  # again at once: one error line a minute
        with anyio.fail_after(2):
            while len([r for r in caplog.records if "failing failed" in r.message]) < 2:  # noqa: ASYNC110
                await anyio.sleep(0.01)
        assert app.try_spawn(later)  # the background group is still there
        with anyio.fail_after(2):
            await ran.wait()
        stopping = True
        await to_app.send({"type": "lifespan.shutdown"})
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert closed == ["store"]
    assert ended == ["stopping"]  # the other work ran on until the stop
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "background work test_a_failing_background_task_leaves_the_others_running.<locals>"
        ".failing failed: RuntimeError"
    ]
    assert "SECRET" not in caplog.text  # also not at debug level (where it failed: frames)
    assert "failing" in caplog.text and "test_background.py" in caplog.text


@pytest.mark.anyio
async def test_an_exceptional_end_of_the_lifespan_still_closes_everything(
    tmp_path: Path, closed: list[str]
) -> None:
    app = ShijhonApp(_settings(tmp_path))
    queue = [{"type": "lifespan.startup"}]

    async def receive() -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        raise RuntimeError("the server went away")

    async def send(message: dict[str, Any]) -> None:
        pass

    with pytest.raises(BaseException) as raised:
        await app._lifespan(receive, send)
    assert "the server went away" in repr(raised.value)
    assert closed == ["store"]

    async def nothing() -> None:
        pass

    assert app.try_spawn(nothing) is False  # the background work is over


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["hangs", "fails"])
async def test_a_close_that_hangs_or_fails_leaves_the_others_to_the_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed: list[str], how: str
) -> None:
    monkeypatch.setattr(shijhon.app, "CLOSE_SECONDS", 0.1)
    app = ShijhonApp(_settings(tmp_path))
    await app.startup()

    async def bad() -> None:
        if how == "fails":
            raise OSError("broken")
        await anyio.sleep_forever()

    monkeypatch.setattr(app.dashboard, "aclose", bad)  # closed first
    with anyio.fail_after(5):
        await app.shutdown()
    assert closed == ["store"]


@pytest.mark.anyio
async def test_a_failed_startup_closes_what_it_opened(tmp_path: Path, closed: list[str]) -> None:
    app = ShijhonApp(_settings(tmp_path, library_path=None))  # fails after the database
    queue = [{"type": "lifespan.startup"}]
    sent: list[str] = []

    async def receive() -> dict[str, Any]:
        return queue.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])

    await app._lifespan(receive, send)
    assert sent == ["lifespan.startup.failed"]
    assert closed == ["store"]


class FlakyStore:
    """A database whose writes fail (or hang) while ``failing`` is set."""

    def __init__(self) -> None:
        self.failing: str | None = "error"
        self.written: list[Any] = []

    async def execute(self, sql: str, params: list[Any]) -> None:
        if self.failing == "error":
            raise sqlite3.OperationalError("database is locked")
        if self.failing == "hang":
            await anyio.sleep_forever()
        self.written.append(params)


@pytest.mark.anyio
async def test_a_use_whose_write_failed_is_written_by_the_next_use() -> None:
    """Bookkeeping: a failed write never fails the request, and is not noted as written."""
    store = FlakyStore()
    now = [1000.0]
    expiry = DeliveredAudio(
        store,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        max_days=30,
        max_bytes=0,
        clock=lambda: now[0],
    )
    await expiry.used("song")  # logged: the request goes on
    store.failing = "hang"
    with anyio.move_on_after(0.05):  # a canceled write (the client left)
        await expiry.used("song")
    store.failing = None
    now[0] += 1
    await expiry.used("song")  # not a minute later: written all the same
    await expiry.used("song")  # now noted: not written again for a while
    assert [p[1] for p in store.written] == ["song"]


class SlowStore(FlakyStore):
    """A database whose first write waits to be let go, and then fails."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = None
        self.calls = 0
        self.started = anyio.Event()
        self.let_go = anyio.Event()

    async def execute(self, sql: str, params: list[Any]) -> None:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.let_go.wait()
            raise sqlite3.OperationalError("database is locked")
        self.written.append(params)


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["failed", "canceled"])
async def test_a_use_beside_one_whose_write_fails_is_recorded_all_the_same(how: str) -> None:
    """Two requests of one song at once. The second finds the
    first's write under way and waits for it; that write fails, or is canceled (its client
    left): the second writes its own use - else neither would be recorded, and the cleanup
    could take the song's release for unused."""
    store = SlowStore()
    expiry = DeliveredAudio(
        store,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        max_days=30,
        max_bytes=0,
        clock=lambda: 1000.0,
    )
    first = anyio.CancelScope()

    async def one() -> None:
        with first:
            await expiry.used("song")

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(one)
            await store.started.wait()
            group.start_soon(expiry.used, "song")  # while the first is being written
            await anyio.sleep(0.05)
            assert store.calls == 1 and store.written == []  # it waits for that write
            if how == "canceled":
                first.cancel()
            else:
                store.let_go.set()
    assert [p[1] for p in store.written] == ["song"]
    await expiry.used("song")  # noted now: not written again for a while
    assert len(store.written) == 1 and not expiry._writing


@pytest.mark.anyio
async def test_a_failing_sweep_after_a_delivery_is_contained() -> None:
    """The sweep started when delivered audio passes the size limit fails (Navidrome): it is
    logged, the next delivery past the limit starts one again, and nothing else ends."""
    swept: list[int] = []

    class Failing(DeliveredAudio):
        async def sweep(self) -> Any:
            swept.append(1)
            raise NavidromeError("Navidrome is restarting")

    finished: list[anyio.Event] = []

    async def run(work: Any) -> None:  # the app's background: a failure would end it
        await work()
        finished[-1].set()

    async with anyio.create_task_group() as group:
        expiry = Failing(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            max_days=0,
            max_bytes=10,
            spawn=lambda work: group.start_soon(run, work),
        )
        expiry._bytes = 5
        for _ in range(2):  # past the limit: a sweep in the background, each time
            finished.append(anyio.Event())
            expiry.delivered(10)
            with anyio.fail_after(2):
                await finished[-1].wait()
            assert not expiry._pending
    assert swept == [1, 1]


@pytest.mark.anyio
async def test_a_failing_write_of_the_artwork_index_ends_its_flush_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushes: list[str] = []
    index = ArtworkIndex(path=tmp_path / "index.sqlite3", spawn=lambda work: flushes.append("x"))
    assert index._kept is not None

    def put(batch: dict[str, str | None]) -> None:
        raise ValueError("unexpected")

    monkeypatch.setattr(index._kept, "put", put)
    index.note("al", CatalogRef("demo", "1"), "https://x.invalid/{w}x{h}a.jpg")
    await index.flush()  # logged, not raised
    assert index._flushing_since is None and not index._writing
    index.note("al", CatalogRef("demo", "2"), "https://x.invalid/{w}x{h}b.jpg")
    assert flushes == ["x", "x"]  # the next item shown starts a flush again


@pytest.mark.anyio
async def test_an_interrupted_swap_that_cannot_be_checked_leaves_the_others_to_the_repair(
    tmp_path: Path,
) -> None:
    """Startup: Navidrome fails while one song's interrupted swap is put right - the next
    song's is put right all the same."""
    staging = tmp_path / "staging"
    staging.mkdir()
    for song in ("a1", "b2"):
        (staging / f"backup-{song}.flac").write_bytes(b"x")
    tried: list[str] = []

    class Engine:
        layout = SimpleNamespace(staging=staging)

        async def recover_swap(self, song_id: str) -> int:
            tried.append(song_id)
            if song_id == "a1":
                raise NavidromeError("Navidrome is restarting")
            return 1

    checks = StartupChecks(None, Engine(), None, None)  # type: ignore[arg-type]
    await checks._staging()
    assert tried == ["a1", "b2"]
