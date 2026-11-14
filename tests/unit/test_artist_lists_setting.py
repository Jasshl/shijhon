"""The artist lists' setting (``[catalog] artist_lists``) and report-only passes."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.artist_lists import repair
from shijhon.artist_lists.files import Entry, LibraryFiles, ListError
from shijhon.artist_lists.repair import REPORT, ArtistLists, report_only
from shijhon.artist_lists.sources import Resolution, Subject, Unavailable
from shijhon.dashboard.live import LIVE
from shijhon.store import Store

pytestmark = pytest.mark.anyio

FACTS = {"artist": "Ash Vane, Bell Orrin", "title": "Night", "duration_ms": 200_000}


class Sources:
    def __init__(self) -> None:
        self.asked: list[Subject] = []

    async def resolve(
        self, subject: Subject, *, link: bool = True, skips: Any = None
    ) -> Resolution:
        self.asked.append(subject)
        assert not link  # (a file being published has no linked catalog item)
        return Resolution("resolved", ("Ash Vane", "Bell Orrin"), "isrc")


def worker(sources: Any, **kwargs: Any) -> ArtistLists:
    return ArtistLists(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        sources,
        None,  # type: ignore[arg-type]
        placeholder_folder="_shijhon",
        library=True,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("value", "report"), [("1", True), ("true", True), ("", False), ("0", False), ("no", False)]
)
def test_report_only_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, value: str, report: bool
) -> None:
    monkeypatch.setenv(REPORT, value)
    assert report_only() is report


async def test_files_being_published_are_listed_only_while_the_setting_is_on() -> None:
    sources = Sources()
    lists = worker(sources, report=False)
    assert await lists.publish_names(FACTS) == (["Ash Vane", "Bell Orrin"], "isrc")
    lists.enabled = False
    assert lists._wake.is_set()  # (a pass follows at once)
    assert await lists.publish_names(FACTS) is None
    assert not lists.lookups()  # (the credit repair asks the add-on catalogs no more)
    assert len(sources.asked) == 1
    # One artist, or report only: nothing asked.
    lists.enabled = True
    assert await lists.publish_names({**FACTS, "artist": "Ash Vane"}) is None
    assert await worker(sources, report=True).publish_names(FACTS) is None
    assert len(sources.asked) == 1


def test_report_only_lets_the_credit_repair_ask_and_write_nothing() -> None:
    assert worker(Sources(), report=False).lookups()
    assert not worker(Sources(), report=True).lookups()
    off = worker(Sources(), report=False, enabled=False)
    assert not off.lookups()


async def test_a_slow_or_failing_catalog_is_a_miss_for_the_publication() -> None:
    class Failing(Sources):
        async def resolve(
            self, subject: Subject, *, link: bool = True, skips: Any = None
        ) -> Resolution:
            raise Unavailable("down")

    with pytest.raises(Unavailable):
        await worker(Failing(), report=False).publish_names(FACTS)


async def test_a_report_runs_once_a_start_and_a_failed_pass_ends_no_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lists = worker(Sources(), report=True)
    passes: list[str] = []

    async def recover() -> None:
        passes.append("recover")

    async def once() -> Any:
        passes.append("pass")
        if passes.count("pass") == 1:
            raise RuntimeError("a scan that timed out")  # (the worker goes on)

    monkeypatch.setattr(lists, "recover", recover)
    monkeypatch.setattr(lists, "once", once)
    monkeypatch.setattr(repair, "FIRST_PASS_SECONDS", 0.0)
    monkeypatch.setattr(repair, "PASS_SECONDS", 0.05)
    with anyio.move_on_after(0.5):
        await lists.run()
    assert passes == ["recover", "pass"]  # (one report a start)
    lists.report = False
    passes.clear()
    with anyio.move_on_after(0.3):
        await lists.run()
    assert passes.count("pass") >= 2  # (the failed first pass did not end it)


def test_the_dashboard_applies_the_setting_at_once() -> None:
    (target,) = LIVE[("catalog", "artist_lists")]
    assert target.path == "artist_lists.enabled"


async def test_an_undo_declines_only_a_file_confirmed_gone(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:
        root = tmp_path / "music"
        (root / "a").mkdir(parents=True)
        (root / "a" / "2.flac").write_bytes(b"another song of the album")
        files = LibraryFiles(root, store, None, None)  # type: ignore[arg-type]
        lists = ArtistLists(
            None,  # type: ignore[arg-type]
            store,
            Sources(),  # type: ignore[arg-type]
            files,
            placeholder_folder="_shijhon",
            library=True,
            report=False,
        )
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, size_after, sha_after,"
            " updated_at) VALUES ('s1', 'file', 'a/1.flac', 'A, B', 'written', 1, 'x', 0)"
        )
        row = await store.fetchone("SELECT * FROM artist_lists")
        # The library unreachable: not told now, asked again later.
        hidden = tmp_path / "elsewhere"
        root.rename(hidden)
        assert await lists._undoable(row) == "library_missing"
        assert (await store.fetchone("SELECT state FROM artist_lists"))["state"] == "written"
        hidden.rename(root)
        # A folder that cannot be read: not told either.
        (root / "a").chmod(0)
        try:
            assert await lists._undoable(row) == "PermissionError"
        finally:
            (root / "a").chmod(0o755)
        assert (await store.fetchone("SELECT state FROM artist_lists"))["state"] == "written"
        # Gone from a library that is there: left alone for good.
        assert await lists._undoable(row) == "declined"
        assert (await store.fetchone("SELECT state FROM artist_lists"))["state"] == "declined"
    finally:
        await store.close()


async def test_an_undo_waits_for_a_music_folder_that_is_not_mounted(tmp_path: Path) -> None:
    from shijhon.artist_lists.files import file_digest

    store = await Store.open(tmp_path / "state.db")
    try:
        root = tmp_path / "music"
        root.mkdir()  # (an empty mount point: the share is not there)
        files = LibraryFiles(root, store, None, None)  # type: ignore[arg-type]
        lists = ArtistLists(
            None,  # type: ignore[arg-type]
            store,
            Sources(),  # type: ignore[arg-type]
            files,
            placeholder_folder="_shijhon",
            library=False,
            report=False,
        )
        listed = b"the file as Shijhon left it"
        size, sha = file_digest_of(listed)
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, size_after, sha_after,"
            " updated_at) VALUES ('s1', 'file', 'Ash Vane/Night/01.flac', 'A, B', 'written',"
            " ?, ?, 0)",
            [size, sha],
        )
        row = await store.fetchone("SELECT * FROM artist_lists")
        assert await lists._undoable(row) == "library_missing"
        assert (await store.fetchone("SELECT state FROM artist_lists"))["state"] == "written"
        # Mounted again: the file is given back.
        (root / "Ash Vane" / "Night").mkdir(parents=True)
        (root / "Ash Vane" / "Night" / "01.flac").write_bytes(listed)
        assert file_digest(root / "Ash Vane" / "Night" / "01.flac") == (size, sha)
        assert await lists._undoable(row) == ""
        # Its folder there without it, the album's other song beside: gone for good.
        (root / "Ash Vane" / "Night" / "02.flac").write_bytes(b"another song")
        (root / "Ash Vane" / "Night" / "01.flac").unlink()
        assert await lists._undoable(row) == "declined"
    finally:
        await store.close()


@pytest.mark.parametrize("relative", ["disk2/album/song.flac", "disk2/song.flac", "song.flac"])
async def test_an_undo_waits_for_a_disk_of_the_library_that_is_not_mounted(
    tmp_path: Path, relative: str
) -> None:
    """(Another disk's folder in the library is no sign that this file's disk is there,
    also for a song at the library's top: the library's folder may join several disks.)"""
    store = await Store.open(tmp_path / "state.db")
    try:
        root = tmp_path / "music"
        root.mkdir()
        (root / "disk1").mkdir()
        (root / "disk1" / "other.flac").write_bytes(b"another disk's song")
        if relative != "song.flac":
            (root / "disk2").mkdir()  # (its mount point, empty)
        lists = ArtistLists(
            None,  # type: ignore[arg-type]
            store,
            Sources(),  # type: ignore[arg-type]
            LibraryFiles(root, store, None, None),  # type: ignore[arg-type]
            placeholder_folder="_shijhon",
            library=False,
            report=False,
        )
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, size_after, sha_after,"
            " updated_at) VALUES ('s1', 'file', ?, 'A, B', 'written', 1, 'x', 0)",
            [relative],
        )
        row = await store.fetchone("SELECT * FROM artist_lists")
        assert await lists._undoable(row) == "library_missing"
        assert (await store.fetchone("SELECT state FROM artist_lists"))["state"] == "written"
    finally:
        await store.close()


@pytest.mark.parametrize("other_disk", [False, True])
async def test_recovery_waits_for_a_music_folder_that_is_not_mounted(
    tmp_path: Path, other_disk: bool
) -> None:
    """(Also when another disk of the library is there.)"""
    import json

    from shijhon.artist_lists.files import file_digest

    class Scans:
        def __init__(self) -> None:
            self.scanned: list[list[str]] = []

        async def scan(self, folders: list[str]) -> None:
            self.scanned.append(folders)

    store = await Store.open(tmp_path / "state.db")
    try:
        root = tmp_path / "music"
        root.mkdir()  # (an empty mount point)
        if other_disk:
            (root / "Bell Orrin").mkdir()
            (root / "Bell Orrin" / "01.flac").write_bytes(b"another disk's song")
        old = b"the file before the write"
        size, sha = file_digest_of(old)
        work = {
            "temp": ".shijhon-ab.tmp",
            "backup": ".shijhon-ab.keep",
            "part": ".shijhon-ab.part",
            "size": str(size),
            "sha": sha,
        }
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, phase, work, updated_at)"
            " VALUES ('s1', 'file', 'Ash Vane/Night/01.flac', 'A, B', 'failed', 'write', ?, 0)",
            [json.dumps(work)],
        )
        scans = Scans()
        files = LibraryFiles(root, store, scans, scans)  # type: ignore[arg-type]
        assert await files.recover() == 0
        row = await store.fetchone("SELECT phase, work FROM artist_lists")
        assert row["phase"] == "write" and row["work"]  # (kept for the next start)
        # Mounted again, as the stop left it: the copy under the name, the old file beside.
        folder = root / "Ash Vane" / "Night"
        folder.mkdir(parents=True)
        (folder / "01.flac").write_bytes(b"the copy with the list")
        (folder / ".shijhon-ab.keep").write_bytes(old)
        assert await files.recover() == 1
        assert (folder / "01.flac").read_bytes() == old
        assert not (folder / ".shijhon-ab.keep").exists()
        assert scans.scanned == [["Ash Vane/Night"]]
        row = await store.fetchone("SELECT phase, work FROM artist_lists")
        assert (row["phase"], row["work"]) == ("", None)
        assert file_digest(folder / "01.flac") == (size, sha)
    finally:
        await store.close()


def file_digest_of(data: bytes) -> tuple[int, str]:
    import hashlib

    return len(data), hashlib.sha256(data).hexdigest()


async def test_a_file_that_cannot_be_read_is_a_list_error_not_a_crash(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:
        files = LibraryFiles(tmp_path, store, None, None)  # type: ignore[arg-type]
        entry = Entry("s1", "gone/1.flac", {}, lambda song, size: True, lambda made: ("", []))
        outcome = (await files.write_many([entry]))["s1"]
        assert isinstance(outcome, ListError) and outcome.reason == "missing"
    finally:
        await store.close()


class _Files:
    root = Path("/")

    def skip(self, relative: str) -> None:
        return None


async def test_one_dead_add_on_ends_no_pass_and_the_report_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:

        class Mixed(Sources):
            async def resolve(
                self, subject: Subject, *, link: bool = True, skips: Any = None
            ) -> Resolution:
                self.asked.append(subject)
                skips.failed("Dead Add-on", "HTTP 502")  # (one add-on that keeps failing)
                if subject.song_id == "s4":
                    return Resolution("ambiguous", source="one_artist", vetoed_by=("Odd Add-on",))
                if subject.song_id == "s5":
                    return Resolution("unresolved")
                raise Unavailable("one add-on down", answered=True)

        sources = Mixed()
        lists = ArtistLists(
            None,  # type: ignore[arg-type]
            store,
            sources,  # type: ignore[arg-type]
            _Files(),  # type: ignore[arg-type]
            placeholder_folder="_shijhon",
            library=True,
            report=True,
            pause=0,
        )

        async def candidates() -> list[repair.Candidate]:
            subject = Subject("s{}", "Ash Vane, Bell Orrin")
            return [
                repair.Candidate(
                    {}, replace(subject, song_id=f"s{n}"), "file", f"s{n}", f"a/{n}", f"al{n}", ""
                )
                for n in range(1, 6)
            ]

        monkeypatch.setattr(lists, "candidates", candidates)
        summary = repair.Summary()
        await lists._pass(summary)
        assert len(sources.asked) == 5  # (three failures that others answered end nothing)
        line = summary.line(True)
        assert "one_artist 1" in line and "no_list 1" in line and "catalog 3" in line
        assert summary.addons_skipped == 1 and "add-ons skipped for the pass: 1" in line
        assert "by source: Odd Add-on 1" in line  # (a misbehaving add-on shows)
    finally:
        await store.close()


def test_a_put_back_tells_the_swap_by_the_files_with_either_backup(tmp_path: Path) -> None:
    """(``LibraryFiles._restore``: a second link or a whole copy, a swap done or not - the
    copy still there means not done.)"""
    path = tmp_path / "1.flac"
    for backup_is_copy in (False, True):
        for swapped in (False, True):
            path.write_bytes(b"old")
            work = {"temp": ".t.tmp", "backup": ".b.keep", "part": ".b.part"}
            backup, temp = tmp_path / work["backup"], tmp_path / work["temp"]
            temp.write_bytes(b"new")
            if backup_is_copy:
                backup.write_bytes(b"old")
            else:
                os.link(path, backup)
            if swapped:
                os.replace(temp, path)
            assert LibraryFiles._restore(path, work) is swapped
            assert path.read_bytes() == b"old" and not backup.exists() and not temp.exists()


class Resolving:
    async def resolve(
        self, subject: Subject, *, link: bool = True, skips: Any = None
    ) -> Resolution:
        return Resolution("resolved", ("Ash Vane", "Bell Orrin"), "catalog_search")


async def test_a_report_tells_what_the_music_folder_setting_would_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lists = ArtistLists(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        Resolving(),  # type: ignore[arg-type]
        _Files(),  # type: ignore[arg-type]
        placeholder_folder="_shijhon",
        library=False,  # (the music folder's setting off)
        report=True,
        pause=0,
    )

    async def candidates() -> list[repair.Candidate]:
        return [
            repair.Candidate(
                {}, Subject(f"s{n}", "Ash Vane, Bell Orrin"), "file", f"s{n}", f"a/{n}", "", ""
            )
            for n in range(1, 4)
        ]

    async def no_records() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(lists, "candidates", candidates)
    monkeypatch.setattr(lists, "_records", no_records)
    summary = repair.Summary()
    await lists._pass(summary)
    assert sum(summary.written.values()) == 0 and summary.gated["catalog_search"] == 3
    assert "3 more would be given with the music folder setting on" in summary.line(True)
    assert not summary.skipped["not_allowed"]


async def test_a_pass_that_changes_nothing_says_it_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lists = worker(Sources(), pause=0)

    async def counted(summary: repair.Summary) -> None:
        summary.looked = 4
        summary.unresolved = 4

    async def nothing(*args: Any) -> None:
        return None

    monkeypatch.setattr(lists, "_pass", counted)
    monkeypatch.setattr(lists, "recover", nothing)

    class Quiet:
        async def execute(self, *args: Any) -> None:
            return None

        async def fetchone(self, *args: Any) -> None:
            return None

    lists.store = Quiet()  # type: ignore[assignment]
    with caplog.at_level("INFO", logger="shijhon.artist_lists.repair"):
        await lists.once()
        await lists.once()
    assert sum("4 song(s) with joined artists looked at" in r.message for r in caplog.records) == 1


async def test_an_undo_that_cannot_be_done_now_waits_before_the_next_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, updated_at)"
            " VALUES ('k1', 'file', 'a/1.flac', 'Ash Vane, Bell Orrin', 'written', 0)"
        )
        lists = ArtistLists(
            None,  # type: ignore[arg-type]
            store,
            Sources(),  # type: ignore[arg-type]
            _Files(),  # type: ignore[arg-type]
            placeholder_folder="_shijhon",
            library=False,
            pause=0,
        )
        tries: list[int] = []

        async def read_only(rows: Any) -> dict[str, str]:
            tries.append(len(rows))
            return {str(r["key"]): "read_only" for r in rows}

        monkeypatch.setattr(lists, "_undo_many", read_only)
        await lists._undo_all(repair.Summary(), ("file",))
        await lists._undo_all(repair.Summary(), ("file",))
        assert tries == [1]  # (not every pass: a while later)
    finally:
        await store.close()


async def test_a_removed_imports_undo_waits_a_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, updated_at) VALUES"
            " ('import:i1', 'import', '_imports/1.flac', 'Ash Vane, Bell Orrin', 'written', 0),"
            " ('import:i2', 'import', '_imports/2.flac', 'Ash Vane, Bell Orrin', 'written', 0)"
        )
        lists = ArtistLists(
            None,  # type: ignore[arg-type]
            store,
            Sources(),  # type: ignore[arg-type]
            _Files(),  # type: ignore[arg-type]
            placeholder_folder="_shijhon",
            enabled=False,
            pause=0,
            clock=lambda: 1000.0,
        )

        async def not_now(rows: Any) -> dict[str, str]:
            return {
                str(r["key"]): "import_removed" if r["key"] == "import:i1" else "import_busy"
                for r in rows
            }

        monkeypatch.setattr(lists, "_undo_many", not_now)
        await lists._undo_all(repair.Summary(), ("file", "import"))
        rows = {r["key"]: r["retry_at"] for r in await store.fetchall("SELECT * FROM artist_lists")}
    finally:
        await store.close()
    assert rows == {
        "import:i1": 1000.0 + repair.SKIPPED_SECONDS,
        "import:i2": 1000.0 + repair.FAILED_SECONDS,
    }


async def test_diagnostics_remembers_files_it_could_not_write_after_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(A file skipped as read-only is looked at again only after a while: a pass that
    does not look at it still counts it.)"""
    store = await Store.open(tmp_path / "state.db")
    try:
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, reason, updated_at)"
            " VALUES ('s1', 'file', 'a/1.flac', 'Ash Vane, Bell Orrin', 'skipped',"
            " 'read_only', 0)"
        )

        class Files(_Files):
            root = tmp_path  # (writable itself)

            async def recover(self) -> int:
                return 0

        lists = ArtistLists(
            None,  # type: ignore[arg-type]
            store,
            Sources(),  # type: ignore[arg-type]
            Files(),  # type: ignore[arg-type]
            placeholder_folder="_shijhon",
            library=True,
            report=False,
        )

        async def nothing(summary: repair.Summary) -> None:
            return None

        monkeypatch.setattr(lists, "_pass", nothing)
        await lists.once()
        assert lists.read_only()
        await store.execute("UPDATE artist_lists SET state = 'written', reason = ''")
        await lists.once()
        assert not lists.read_only()
    finally:
        await store.close()


def test_diagnostics_says_when_the_last_pass_could_not_write_files(tmp_path: Path) -> None:
    lists = worker(Sources())
    writable = _Files()
    writable.root = tmp_path
    lists.files = writable  # type: ignore[assignment]
    assert not lists.read_only()  # (the root is writable here)
    lists._blocked = 2
    assert lists.read_only()
    lists.library = False
    assert not lists.read_only()


async def test_a_joined_artists_songs_are_read_past_the_first_page() -> None:
    """(``NavidromeService.every_artist_song``: the songs after the first 500 too, here
    after 500 that name the artist only as their album artist.)"""
    import httpx

    from shijhon.navidrome.client import NavidromeService

    joined = "Ash Vane, Bell Orrin"
    songs = [
        {"id": f"s{i:04d}", "path": f"a/{i}.flac", "artist": "Ash Vane", "albumId": "al"}
        for i in range(500)
    ]
    songs.append(
        {
            "id": "s0500",
            "path": "b/1.flac",
            "artist": joined,
            "albumId": "al2",
            "participants": {"artist": [{"name": joined}]},
        }
    )
    pages: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "t", "isAdmin": True})
        params = dict(request.url.params)
        start, end = int(params["_start"]), int(params["_end"])
        if request.url.path == "/api/artist":
            return httpx.Response(200, json=[{"id": "ar1", "name": joined}][start:end])
        pages.append((start, end))
        assert params["artists_id"] == "ar1"
        return httpx.Response(200, json=songs[start:end], headers={"X-Total-Count": "501"})

    nd = NavidromeService("http://test.invalid", "svc", "invented")
    nd.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        lists = ArtistLists(
            nd,
            None,  # type: ignore[arg-type]
            Sources(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            placeholder_folder="_shijhon",
        )
        found = await lists.candidates()
    finally:
        await nd.aclose()
    assert [c.key for c in found] == ["s0500"]
    assert pages == [(0, 500), (500, 1000)]


async def test_recovery_of_a_song_at_the_librarys_top_waits_for_its_disk(tmp_path: Path) -> None:
    """(Another disk's folder at the library's top, where the song's disk joins it too.)"""
    import json

    class Scans:
        def __init__(self) -> None:
            self.scanned: list[list[str]] = []

        async def scan(self, folders: list[str]) -> None:
            self.scanned.append(folders)

    store = await Store.open(tmp_path / "state.db")
    try:
        root = tmp_path / "music"
        (root / "disk1").mkdir(parents=True)
        old = b"the file before the write"
        size, sha = file_digest_of(old)
        work = {"temp": ".shijhon-ab.tmp", "backup": ".shijhon-ab.keep",
                "part": ".shijhon-ab.part", "size": str(size), "sha": sha}  # fmt: skip
        await store.execute(
            "INSERT INTO artist_lists (key, kind, path, artist, state, phase, work, updated_at)"
            " VALUES ('s1', 'file', '01.flac', 'A, B', 'failed', 'write', ?, 0)",
            [json.dumps(work)],
        )
        scans = Scans()
        files = LibraryFiles(root, store, scans, scans)  # type: ignore[arg-type]
        assert await files.recover() == 0
        assert (await store.fetchone("SELECT phase FROM artist_lists"))["phase"] == "write"
        # Its disk back, as the stop left it.
        (root / "01.flac").write_bytes(b"the copy with the list")
        (root / ".shijhon-ab.keep").write_bytes(old)
        assert await files.recover() == 1
        assert (root / "01.flac").read_bytes() == old and scans.scanned == [[""]]
        assert (await store.fetchone("SELECT phase FROM artist_lists"))["phase"] == ""
    finally:
        await store.close()
