"""The library index prepared with Shijhon's service account, with nobody signing in."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import shijhon.collection.reader as reader_module
from shijhon.access import AccessUnavailable, Principal
from shijhon.collection.contracts import CollectionError, IndexState
from shijhon.collection.query_index import IndexPending, SetupRequired
from shijhon.collection.reader import (
    OWN_SECONDS,
    SERVICE_ADMIN,
    SERVICE_LIBRARY,
    SERVICE_SIGN_IN,
    CallerLibrary,
    ServiceLibrary,
    ServiceProblem,
    SourceRefresh,
    _instant,
)
from shijhon.collection.service import Collection
from shijhon.collection.visibility import Visibility
from shijhon.navidrome.client import NavidromeError, ServiceRefused
from shijhon.store import Store

pytestmark = pytest.mark.anyio
SERVICE = Principal("service-id", "shijhon", True, frozenset({1}))
LISTENER = Principal("listener-id", "Listener", False, frozenset({1}))


def at(second: int) -> str:
    """A Navidrome timestamp, ``second`` seconds into an invented day (nanoseconds, as
    Navidrome writes them)."""
    return f"2026-11-21T09:{second // 60:02d}:{second % 60:02d}.123456789+01:00"


class FakeNavidrome:
    """Shijhon's service account at Navidrome: its sign-in, the library's songs in pages
    (``songs``, each with its path and ``updatedAt``), the library's counts and the scan
    status. ``grow``: songs added while a page is read (a scan under way)."""

    user = "shijhon"
    library_id = 1
    socket = None
    base_url = "http://native"

    def __init__(self, songs: list[dict[str, Any]] | None = None) -> None:
        self.songs = songs if songs is not None else []
        self.refused: Exception | None = None
        self.scanning = False
        self.last_scan = at(0)
        self.asked: list[dict[str, Any]] = []
        self.grow: list[dict[str, Any]] = []
        self.changes_asked = 0
        self.created = "2026-11-01T08:00:00Z"
        self.library_status: int | None = None
        self.forgotten = 0

    async def sign_in(self) -> None:
        if self.refused is not None:
            raise self.refused

    async def native(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        assert (method, path) == ("GET", "song")
        params = kwargs["params"]
        self.asked.append(params)
        start, end = int(params["_start"]), int(params["_end"])
        rows = sorted((s for s in self.songs if not s.get("missing")), key=lambda s: str(s["id"]))
        if self.grow:
            self.songs.append(self.grow.pop(0))
        return httpx.Response(200, headers={"x-total-count": str(len(rows))}, json=rows[start:end])

    def forget_sign_in(self) -> None:
        self.forgotten += 1

    async def native_json(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.refused is not None:
            raise self.refused
        if path == "library/1" and self.library_status is not None:
            raise NavidromeError("native GET: HTTP 403", status=self.library_status)
        if path == "library/1":
            present = [s for s in self.songs if not s.get("missing")]
            return {
                "lastScanAt": self.last_scan,
                "totalSongs": len(present),
                "totalFiles": len(present),
                "totalMissingFiles": len(self.songs) - len(present),
                "totalFolders": 1,
                "createdAt": self.created,
            }
        assert path == "song" and kwargs["params"]["_sort"] == "updated_at"
        self.changes_asked += 1
        start, end = int(kwargs["params"]["_start"]), int(kwargs["params"]["_end"])
        rows = sorted(self.songs, key=lambda s: str(s.get("updatedAt", "")), reverse=True)
        return rows[start:end]

    async def scan_status(self) -> dict[str, Any]:
        if isinstance(self.refused, NavidromeError) and self.refused.code == 40:
            raise self.refused
        if self.refused is not None and not isinstance(self.refused, ServiceRefused):
            raise self.refused
        return {"scanning": self.scanning, "lastScan": self.last_scan}


def service(navidrome: FakeNavidrome, principal: Principal = SERVICE) -> ServiceLibrary:
    access = SimpleNamespace(verify=AsyncMock(return_value=principal))
    return ServiceLibrary(navidrome, access)  # type: ignore[arg-type]


def song(ident: str, library: int = 1, path: str = "", second: int = 1) -> dict[str, Any]:
    return {
        "id": ident,
        "libraryId": library,
        "title": ident,
        "albumId": "album-" + ident[:1],
        "artist": "Invented",
        "path": path or f"Invented/{ident}.flac",
        "updatedAt": at(second),
        "starred": True,
    }


async def test_the_service_account_reads_the_configured_library_in_pages():
    navidrome = FakeNavidrome([song("a"), song("b"), song("c")])
    source = service(navidrome)
    assert (await source.authorize("service-id")).owner_id == "service-id"
    assert [s["id"] for s in await source.songs("service-id", "1", 0, 2)] == ["a", "b"]
    assert [s["id"] for s in await source.songs("service-id", "1", 2, 2)] == ["c"]
    assert navidrome.asked[0] == {
        "library_id": "1",
        "_start": "0",
        "_end": "2",
        "_sort": "id",
        "_order": "ASC",
        "missing": "false",
    }
    # Only the configured library, in bounded pages; a song of another library is refused.
    with pytest.raises(CollectionError):
        await source.songs("service-id", "2", 0, 2)
    with pytest.raises(CollectionError):
        await source.songs("service-id", "1", 0, 501)
    navidrome.songs = [song("a"), song("b", library=2)]
    with pytest.raises(CollectionError, match="invalid library page"):
        await source.songs("service-id", "1", 0, 2)
    # A total that changes between the pages of one read is refused.
    navidrome.songs = [song("a"), song("b"), song("c")]
    await source.songs("service-id", "1", 0, 2)
    navidrome.songs.append(song("d"))
    with pytest.raises(CollectionError):
        await source.songs("service-id", "1", 2, 2)
    # Another account under the service account's name is no authorization of the first.
    with pytest.raises(CollectionError):
        await source.authorize("someone-else")


@pytest.mark.parametrize(
    "refused,principal,problem",
    [
        (ServiceRefused("service login failed (401)", status=401), SERVICE, SERVICE_SIGN_IN),
        (ServiceRefused("service login failed (403)", status=403), SERVICE, SERVICE_SIGN_IN),
        (ServiceRefused("the Shijhon service account must be a Navidrome admin"), SERVICE,
         SERVICE_ADMIN),
        (None, Principal("service-id", "shijhon", False, frozenset({1})), SERVICE_ADMIN),
        (None, Principal("service-id", "shijhon", True, frozenset({2})), SERVICE_LIBRARY),
    ],
)  # fmt: skip
async def test_what_is_wrong_with_the_service_account_is_said(
    refused: Exception | None, principal: Principal, problem: str
):
    navidrome = FakeNavidrome()
    navidrome.refused = refused
    with pytest.raises(ServiceProblem) as raised:
        await service(navidrome, principal).principal()
    assert str(raised.value) == problem
    assert "password_file" in SERVICE_SIGN_IN and "NixOS" in SERVICE_SIGN_IN
    assert "sign in once" not in problem.lower()


async def test_a_navidrome_that_cannot_be_reached_is_no_problem_of_the_account():
    navidrome = FakeNavidrome()
    navidrome.refused = NavidromeError("service login: ConnectError", unsent=True)
    with pytest.raises(CollectionError) as raised:
        await service(navidrome).principal()
    assert not isinstance(raised.value, ServiceProblem)
    navidrome.refused = None
    source = service(navidrome)
    source.access.verify.side_effect = AccessUnavailable  # type: ignore[attr-defined]
    with pytest.raises(CollectionError) as raised:
        await source.principal()
    assert not isinstance(raised.value, ServiceProblem)


class Clock:
    def __init__(self) -> None:
        self.now = 12.0  # (soon after the machine started)

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_800_000_000.0 + self.now


def refresher(
    monkeypatch: pytest.MonkeyPatch,
    navidrome: FakeNavidrome,
    prepared: list[bool],
    *,
    clock: Clock | None = None,
    saved: Path | None = None,
) -> tuple[SourceRefresh, AsyncMock, Clock]:
    clock = clock or Clock()
    monkeypatch.setattr(reader_module, "time", clock)
    playlists = SimpleNamespace(client=SimpleNamespace(native=navidrome))
    reader = CallerLibrary(playlists)  # type: ignore[arg-type]

    async def is_prepared(library: str) -> bool:
        assert library == "1"
        return prepared[0]

    refresh = AsyncMock()

    async def refreshed(*args: Any, **kwargs: Any) -> None:
        await refresh(*args, **kwargs)
        prepared[0] = True

    work = SourceRefresh(
        reader,
        refreshed,
        lambda job: False,
        service=service(navidrome),
        prepared=is_prepared,
        placeholder_folder="_shijhon",
        imports_folder="_imports",
        saved=saved,
    )
    return work, refresh, clock


async def test_a_new_library_is_prepared_at_once_and_refreshed_after_navidrome_changes(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome([song("a"), song("b")])
    work, refresh, clock = refresher(monkeypatch, navidrome, [False])
    assert work.automatic
    # No snapshot: prepared at once, as the service account, with nobody signed in.
    await work.round()
    refresh.assert_awaited_once_with("service-id", "1", access=work.service)
    assert not work.running and work.problem == ""
    # Nothing changed at Navidrome: nothing is read again, however long it lasts.
    clock.now += 3600
    await work.round()
    assert refresh.await_count == 1
    # Someone's music was added: refreshed - not while Navidrome scans, and not before the
    # interval since the last refresh.
    clock.now = 100
    work._updated_at = 50
    navidrome.scanning = True
    navidrome.songs.append(song("c", second=5))
    await work.round()
    assert refresh.await_count == 1
    navidrome.scanning, navidrome.last_scan = False, at(6)
    await work.round()
    assert refresh.await_count == 1  # (50 s after the last refresh)
    clock.now = 50 + work.interval
    await work.round()
    assert refresh.await_count == 2
    # A song gone missing is a change too.
    clock.now += work.interval
    navidrome.songs[0] = {**navidrome.songs[0], "missing": True, "updatedAt": at(9)}
    navidrome.last_scan = at(10)
    await work.round()
    clock.now += work.poll  # (read once a look saw no new change)
    await work.round()
    assert refresh.await_count == 3


async def test_shijhon_s_own_writes_wait_for_the_longer_interval(monkeypatch: pytest.MonkeyPatch):
    """Placeholders Shijhon wrote and music imports (inside their folders) start no full
    refresh within the hour: their songs join the lists from apps' requests, imports
    refresh the index themselves. Someone else's change among them still does."""
    navidrome = FakeNavidrome([song("a"), song("b")])
    work, refresh, clock = refresher(monkeypatch, navidrome, [False])
    await work.round()
    assert refresh.await_count == 1
    for second in range(5, 50, 5):  # a fill: a targeted scan after each release
        clock.now += 300
        navidrome.songs.append(song(f"p{second}", path=f"_shijhon/x/{second}.flac", second=second))
        navidrome.songs.append(song(f"i{second}", path=f"_imports/{second}.flac", second=second))
        navidrome.last_scan = at(second + 1)
        await work.round()
    assert refresh.await_count == 1 and work._pending == "own"
    # Each look reads only what changed since the one before.
    asked = navidrome.changes_asked
    clock.now += 30
    navidrome.songs.append(song("p99", path="_shijhon/y/1.flac", second=99))
    navidrome.last_scan = at(100)
    await work.round()
    assert navidrome.changes_asked == asked + 1 and refresh.await_count == 1
    clock.now = work._updated_at + OWN_SECONDS
    await work.round()
    assert refresh.await_count == 2 and work._pending is None
    # Someone else's file among Shijhon's: read once the changes settle.
    navidrome.songs.append(song("p120", path="_shijhon/z/1.flac", second=120))
    navidrome.songs.append(song("theirs", path="Someone/new.flac", second=119))
    navidrome.last_scan = at(121)
    clock.now += work.interval
    await work.round()
    clock.now += work.poll  # (read once a look saw no new change)
    await work.round()
    assert refresh.await_count == 3


async def test_the_first_preparation_follows_navidrome_s_first_scan_as_it_grows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """During Navidrome's first scan, the library grows while it is read: a read is refused
    (its total changed). It is tried again at the poll's pace, never with a longer wait,
    and at once when the scan ends."""
    store = await Store.open(tmp_path / "state.db")
    try:
        collection = Collection(store, SimpleNamespace())  # type: ignore[arg-type]
        navidrome = FakeNavidrome([song(f"s{n:04}") for n in range(600)])
        navidrome.scanning = True
        navidrome.grow = [song(f"t{n:04}") for n in range(10)]
        clock = Clock()
        monkeypatch.setattr(reader_module, "time", clock)
        reader = CallerLibrary(SimpleNamespace(client=SimpleNamespace(native=navidrome)))  # type: ignore[arg-type]
        work = SourceRefresh(
            reader,
            collection.refresh,
            lambda job: False,
            service=service(navidrome),
            prepared=collection.prepared,
        )
        for _ in range(3):
            with pytest.raises(CollectionError):
                await work.round()
            assert work._failures == 0 and work._retry_at == clock.now + work.poll
            assert not await collection.prepared("1")
            clock.now += work.poll
            navidrome.grow = [song(f"u{clock.now:.0f}{n}") for n in range(3)]
        navidrome.grow, navidrome.scanning = [], False
        navidrome.last_scan = at(30)
        clock.now += 5  # (before the poll's wait is over: the scan's end ends it)
        await work.round()
        assert await collection.prepared("1")
        row = await store.fetchone(
            "SELECT count(*) n FROM collection_tracks WHERE library_id='1' AND present=1"
        )
        assert row["n"] == len(navidrome.songs)
    finally:
        await store.close()


async def test_a_prepared_library_is_not_read_while_navidrome_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome()
    navidrome.refused = NavidromeError("library: ConnectError")
    work, refresh, _ = refresher(monkeypatch, navidrome, [True])
    await work.round()
    assert not refresh.await_count and work.problem == ""


async def test_a_failure_is_tried_at_once_when_navidrome_answers_again(
    monkeypatch: pytest.MonkeyPatch,
):
    """Shijhon started before Navidrome: the wait after failures does not outlast it."""
    navidrome = FakeNavidrome([song("a")])
    work, refresh, clock = refresher(monkeypatch, navidrome, [False])
    refresh.side_effect = CollectionError("Navidrome could not return this library item.")
    waits = []
    for _ in range(7):
        before = clock.now
        with pytest.raises(CollectionError):
            await work.round()
        waits.append(work._retry_at - before)
        clock.now = work._retry_at
    assert waits == [30, 60, 120, 240, 480, 600, 600]
    assert work.problem == ""  # (a failure that is no problem of the account)
    navidrome.refused = NavidromeError("library: ConnectError")
    clock.now += 30
    with pytest.raises(CollectionError):
        await work.round()  # (not prepared: tried, and it fails)
    navidrome.refused = None
    refresh.side_effect = None
    clock.now += 30  # (long before the ten minutes are over)
    await work.round()
    assert refresh.await_count == 8 and work._failures == 0


async def test_after_a_start_nothing_is_read_again_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    saved = tmp_path / "library-source.json"
    navidrome = FakeNavidrome([song("a"), song("b")])
    work, refresh, clock = refresher(monkeypatch, navidrome, [False], saved=saved)
    await work.round()
    assert refresh.await_count == 1 and saved.exists()
    # The next start: the same library, so no full read.
    clock.now += 600
    again, refresh_again, _ = refresher(monkeypatch, navidrome, [True], clock=clock, saved=saved)
    assert again._restore()
    await again.round()
    assert not refresh_again.await_count
    # Someone's music added while Shijhon was stopped: read once the changes settle.
    navidrome.songs.append(song("c", second=40))
    navidrome.last_scan = at(41)
    await again.round()
    clock.now += again.poll  # (read once a look saw no new change)
    await again.round()
    assert refresh_again.await_count == 1
    # Without anything kept, a start reads the library once.
    saved.unlink()
    third, refresh_third, _ = refresher(monkeypatch, navidrome, [True], clock=clock, saved=saved)
    assert not third._restore()
    await third.round()
    assert refresh_third.await_count == 1


async def test_a_library_prepared_during_navidrome_s_first_scan_is_read_again_when_it_ends(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome([song("a")])
    navidrome.scanning = True
    work, refresh, clock = refresher(monkeypatch, navidrome, [False])
    await work.round()  # what Navidrome has so far, rather than nothing
    assert refresh.await_count == 1
    clock.now += 15
    navidrome.songs.append(song("b", second=10))
    await work.round()
    assert refresh.await_count == 1
    navidrome.scanning, navidrome.last_scan = False, at(11)
    await work.round()  # at once, not after the interval
    assert refresh.await_count == 2
    clock.now += 15
    await work.round()
    assert refresh.await_count == 2


async def test_a_refused_service_account_is_said_also_with_a_snapshot(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome([song("a")])
    navidrome.refused = ServiceRefused("service login failed (401)", status=401)
    work, refresh, clock = refresher(monkeypatch, navidrome, [False])
    with pytest.raises(ServiceProblem):
        await work.round()
    assert work.problem == SERVICE_SIGN_IN and not refresh.await_count
    clock.now += 20  # (the first wait: 30 seconds)
    await work.round()
    assert not refresh.await_count
    navidrome.refused = None
    clock.now += 11
    await work.round()
    assert refresh.await_count == 1 and work.problem == ""
    # A password changed later: Navidrome refuses getScanStatus (code 40). The last
    # snapshot keeps answering, and the problem is said.
    navidrome.refused = NavidromeError("getScanStatus: Wrong username or password", 40)
    clock.now += 60
    await work.round()
    assert work.problem == SERVICE_SIGN_IN and refresh.await_count == 1
    navidrome.refused = None
    await work.round()
    assert work.problem == ""


async def test_no_second_refresh_starts_while_one_began_during_the_look(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome([song("a")])
    work, refresh, _ = refresher(monkeypatch, navidrome, [False])

    async def busy(library: str) -> bool:
        work._running = True  # (Refresh music, pressed meanwhile)
        return False

    work.prepared = busy
    await work.round()
    assert not refresh.await_count


async def test_refresh_music_without_a_sign_in_uses_the_service_account(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome()
    work, refresh, _ = refresher(monkeypatch, navidrome, [True])
    queued = []
    work.spawn = lambda job: queued.append(job) or True
    assert work.requested() and work.running
    assert work.requested()  # (one under way: this one right after it)
    await queued.pop()()
    refresh.assert_awaited_once_with("service-id", "1", access=work.service)
    assert work.running and len(queued) == 1
    await queued.pop()()
    assert refresh.await_count == 2 and not work.running and not queued
    work.service = None
    assert not work.requested()


async def test_what_counts_as_shijhon_s_own_change(monkeypatch: pytest.MonkeyPatch):
    """Placeholders and imports added or changed are Shijhon's own; an import deleted or
    gone missing, and a folder of that name deeper in the library, are not. A large fill
    (more than a thousand placeholders between two looks) is still Shijhon's own."""
    navidrome = FakeNavidrome([song("a")])
    work, _refresh, clock = refresher(monkeypatch, navidrome, [False])
    await work.round()

    async def look(*rows: dict[str, Any]) -> str | None:
        navidrome.songs.extend(rows)
        navidrome.last_scan = at(int(clock.now) % 3000 + 1)
        clock.now += 30
        work._pending = None
        work._updated_at = clock.now  # (no refresh: only what is pending is looked at)
        await work.round()
        return work._pending

    fill = [song(f"f{n:05}", path=f"_shijhon/a/{n}.flac", second=100) for n in range(1500)]
    assert await look(*fill) == "own"
    assert await look(song("i1", path="_imports/1.flac", second=200)) == "own"
    gone = {**song("i1", path="_imports/1.flac", second=300), "missing": True}
    assert await look(gone) == "external"
    assert await look(song("r1", path="Rock/_imports/x.flac", second=400)) == "external"
    assert await look(song("r2", path="Rock/_shijhon/x.flac", second=500)) == "external"
    # Pending already: the changed songs are not read again.
    asked = navidrome.changes_asked
    navidrome.songs.append(song("x", second=600))
    navidrome.last_scan = at(601)
    clock.now += 30
    await work.round()
    assert navidrome.changes_asked == asked


async def test_an_outside_change_is_read_once_changes_settle(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """An album added: read once a look saw no new change - about a minute after its scan -
    but never sooner than a minute after the last read ended (or twice as long as it took,
    for a large library), and at least every five minutes while changes go on."""
    navidrome = FakeNavidrome([song("a")])
    work, refresh, clock = refresher(monkeypatch, navidrome, [False])
    await work.round()  # (the first preparation, at 12 s)
    # An album added 20 s after it: seen at the next look, read at the one after.
    clock.now = 42
    navidrome.songs.append(song("b", second=20))
    navidrome.last_scan = at(21)
    await work.round()
    assert refresh.await_count == 1
    caplog.set_level("INFO", logger="shijhon.collection.reader")
    clock.now = 72
    await work.round()
    assert refresh.await_count == 2
    logged = [r.getMessage() for r in caplog.records]
    assert "library read (outside change) started" in logged
    assert any(m.startswith("library read (outside change) finished in ") for m in logged)
    # A change seen right after a read that ended late: not within a minute of its end.
    clock.now = work._updated_at = 100.0
    navidrome.songs.append(song("c", second=90))
    navidrome.last_scan = at(91)
    for moment, reads in ((102, 2), (132, 2), (162, 3)):
        clock.now = moment
        await work.round()
        assert refresh.await_count == reads, moment
    # After a read of a large library that took 56 s: not within 112 s of its end.
    work._read_seconds = 56.0
    navidrome.songs.append(song("e", second=150))
    navidrome.last_scan = at(151)
    for moment, reads in ((192, 3), (222, 3), (252, 3), (282, 4)):
        clock.now = moment
        await work.round()
        assert refresh.await_count == reads, moment
    # Changes at every look: read at least every five minutes, from the first one seen.
    work._read_seconds = 6.0
    first = None
    for n in range(20):
        clock.now += 30
        navidrome.songs.append(song(f"d{n:02}", second=300 + n))
        navidrome.last_scan = at(400 + n)
        await work.round()
        first = first or clock.now
        if refresh.await_count == 5:
            break
    assert refresh.await_count == 5 and clock.now - first >= work.interval
    assert clock.now - first <= work.interval + work.poll


async def test_counts_that_change_with_no_song_changed_are_a_change(
    monkeypatch: pytest.MonkeyPatch,
):
    """A Navidrome restored from a backup: fewer songs, none changed since."""
    navidrome = FakeNavidrome([song("a"), song("b")])
    work, refresh, clock = refresher(monkeypatch, navidrome, [False])
    await work.round()
    navidrome.songs.pop()
    navidrome.last_scan = at(9)
    clock.now += work.interval
    await work.round()
    clock.now += work.poll  # (read once a look saw no new change)
    await work.round()
    assert refresh.await_count == 2


async def test_a_service_account_that_lost_its_admin_rights_is_said(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome([song("a")])
    work, _refresh, clock = refresher(monkeypatch, navidrome, [False])
    await work.round()
    navidrome.library_status = 403  # (its kept sign-in, no admin any more)
    clock.now += 30
    await work.round()
    assert work.problem == SERVICE_ADMIN and navidrome.forgotten == 1
    navidrome.library_status = None
    clock.now += 30
    await work.round()
    assert work.problem == ""


async def test_in_automatic_mode_native_callers_start_no_refresh_of_their_own(
    monkeypatch: pytest.MonkeyPatch,
):
    navidrome = FakeNavidrome([song("a")])
    work, _refresh, _ = refresher(monkeypatch, navidrome, [True])
    started = []
    work.spawn = lambda job: started.append(job) or True
    await work.verified(LISTENER, "listener-bearer")
    assert not started and work.reader.has_bearer("listener-id")
    # A dashboard sign-in reads the library again, as the service account.
    await work.signed_in(LISTENER, "listener-bearer")
    assert len(started) == 1
    await started.pop()()
    _refresh.assert_awaited_once_with("service-id", "1", access=work.service)


async def test_what_was_kept_is_not_taken_for_another_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    saved = tmp_path / "library-source.json"
    navidrome = FakeNavidrome([song("a")])
    work, _, clock = refresher(monkeypatch, navidrome, [False], saved=saved)
    await work.round()
    kept = json.loads(saved.read_text())
    again, refresh, _ = refresher(monkeypatch, navidrome, [True], clock=clock, saved=saved)
    for changed in (
        {"since": "2026-11-21T09:00:00"},  # (no offset: not comparable)
        {"library": 2},
        {"created": None},
    ):
        saved.write_text(json.dumps({**kept, **changed}))
        assert not again._restore()
    # Another library under the same ID (made anew): read again at once.
    saved.write_text(json.dumps(kept))
    assert again._restore()
    navidrome.created = "2026-11-20T08:00:00Z"
    await again.round()
    assert refresh.await_count == 1


async def test_navidrome_timestamps_compare_as_moments():
    assert _instant("2026-11-21T09:38:41.474069594+01:00") < _instant("2026-11-21T09:38:41.6+01:00")
    assert _instant("2026-11-21T08:38:41Z") == _instant("2026-11-21T09:38:41+01:00")
    assert _instant("0001-01-01T00:00:00Z") is None and _instant("soon") is None


def visibility(state: IndexState) -> Visibility:
    access = SimpleNamespace(navidrome=SimpleNamespace(library_id=1))
    index = SimpleNamespace(source_state=AsyncMock(return_value=state))
    return Visibility(None, None, access, None, index)  # type: ignore[arg-type]


async def test_apps_are_never_told_to_sign_in_while_the_library_is_prepared_for_them():
    missing = IndexState("1", "", "missing", 0, 0, "")
    view = visibility(missing)
    with pytest.raises(SetupRequired, match="Sign in once"):  # (no automatic preparation)
        await view.binding(LISTENER)
    problem = [""]
    view.source_problem = lambda: problem[0]
    with pytest.raises(IndexPending):
        await view.binding(LISTENER)
    problem[0] = SERVICE_SIGN_IN
    with pytest.raises(SetupRequired) as raised:
        await view.binding(LISTENER)
    assert str(raised.value) == SERVICE_SIGN_IN
    # A complete snapshot answers, whatever the service account's state.
    view.index.source_state.return_value = IndexState("1", "g1", "ready", 3, 1.0, "")  # type: ignore[attr-defined]
    view.index.usable_generation = AsyncMock(return_value="g1")  # type: ignore[attr-defined]
    assert (await view.binding(LISTENER)).generations == (("1", "g1"),)
