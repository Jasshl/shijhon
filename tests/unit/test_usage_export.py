"""The usage export: only the tables and columns the checks read, stamped with its
snapshot time, written whole or not at all; and the two sources - the export, Navidrome's
database itself - are never taken for each other."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from shijhon.cli import main
from shijhon.config import load_settings
from shijhon.navidrome import export as export_module
from shijhon.navidrome.export import (
    COPIED,
    META,
    ExportError,
    Snapshot,
    export,
    request_path,
    writing,
)
from shijhon.navidrome.usage import (
    Mark,
    NotAsked,
    NotFresh,
    UsageSource,
    WrongSource,
    configured,
    in_use,
)

NAVIDROME = """
CREATE TABLE user (id TEXT, user_name TEXT, password TEXT);
CREATE TABLE property (id TEXT, value TEXT);
CREATE TABLE annotation (user_id TEXT, item_id TEXT, item_type TEXT, play_count INTEGER,
                         play_date TEXT, rating INTEGER, rated_at TEXT, starred INTEGER,
                         starred_at TEXT);
CREATE TABLE playlist (id TEXT, name TEXT, owner_id TEXT);
CREATE TABLE playlist_tracks (id INTEGER, playlist_id TEXT, media_file_id TEXT);
CREATE TABLE bookmark (user_id TEXT, item_id TEXT, comment TEXT, position INTEGER);
CREATE TABLE playqueue (id TEXT, user_id TEXT, current TEXT, items TEXT);
CREATE TABLE share (id TEXT, user_id TEXT, description TEXT, resource_ids TEXT);
CREATE TABLE scrobbles (media_file_id TEXT, user_id TEXT, submission_time INTEGER);
CREATE TABLE media_file (id TEXT, path TEXT, title TEXT, missing INTEGER, album_id TEXT,
                         artist_id TEXT, album_artist_id TEXT, lyrics TEXT);
CREATE TABLE album (id TEXT, name TEXT, album_artist TEXT, album_artist_id TEXT, notes TEXT);
INSERT INTO user VALUES ('user-1', 'listener-name', 'ENCRYPTED-PASSWORD');
INSERT INTO property VALUES ('JWTSecret', 'SIGNING-SECRET');
INSERT INTO annotation VALUES ('user-1', 's1', 'media_file', 3, '2026-09-01', 0, NULL, 1,
                               '2026-09-01 10:00:00');
INSERT INTO playlist VALUES ('pl1', 'a private playlist name', 'user-1');
INSERT INTO playlist_tracks VALUES (1, 'pl1', 's2');
INSERT INTO bookmark VALUES ('user-1', 's3', 'a private comment', 7);
INSERT INTO playqueue VALUES ('q1', 'user-1', 's4', 's4,s5');
INSERT INTO share VALUES ('SHARE-LINK-KEY', 'user-1', 'a private description', 'pl1');
INSERT INTO scrobbles VALUES ('s6', 'user-1', 1), ('s6', 'user-1', 2);
INSERT INTO media_file VALUES ('s1', 'a/1.flac', 'One', 0, 'al1', 'ar1', 'ar1', 'words');
INSERT INTO album VALUES ('al1', 'Album', 'Artist', 'ar1', 'private notes');
"""
PRIVATE = (b"user-1", b"listener-name", b"ENCRYPTED-PASSWORD", b"SIGNING-SECRET",
           b"SHARE-LINK-KEY", b"a private", b"private notes", b"words")  # fmt: skip


def navidrome(tmp_path: Path, script: str = NAVIDROME) -> Path:
    path = tmp_path / "navidrome.db"
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.close()
    return path


def test_the_export_holds_only_what_the_checks_read(tmp_path: Path) -> None:
    source, out = navidrome(tmp_path), tmp_path / "usage" / "usage.sqlite3"
    out.parent.mkdir()
    before = time.time()
    taken = export(source, out)
    assert before <= taken.at <= time.time() and taken.answers == ""
    inode = source.stat()
    assert taken.source == f"{inode.st_dev}:{inode.st_ino}"  # the file it was read from
    # (nothing staged left: the export and its writer's lock)
    assert sorted(p.name for p in out.parent.iterdir()) == ["usage.sqlite3", "usage.sqlite3.lock"]
    assert out.stat().st_mode & 0o777 == 0o640
    conn = sqlite3.connect(out)
    tables = {
        str(r[0]): [str(c[1]) for c in conn.execute(f"PRAGMA table_info({r[0]})")]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables.pop(META) == ["format", "snapshot_at", "answers", "source"]
    assert tables == {table: list(columns) for table, (columns, _) in COPIED.items()}
    assert conn.execute("SELECT * FROM shijhon_usage_export").fetchall() == [
        (1, taken.at, "", taken.source)
    ]
    assert conn.execute("SELECT * FROM scrobbles").fetchall() == [("s6",)]  # each song once
    conn.close()
    content = out.read_bytes()
    for secret in PRIVATE:  # no users, no secrets, no share keys, no names
        assert secret not in content, secret
    # The checks' own queries run on it as on Navidrome's database.
    usage, at = UsageSource(out, export=True).open()
    assert at == taken
    assert in_use(usage, ["s1"]) == ["favorited", "played"]
    assert in_use(usage, ["s2"]) == ["in a playlist", "shared"]
    assert in_use(usage, ["s3"]) == ["bookmarked"]
    assert in_use(usage, ["s5"]) == ["in a play queue"] and in_use(usage, ["s6"]) == ["played"]
    assert in_use(usage, ["s9"]) == []
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        usage.execute("DELETE FROM annotation")
    usage.close()


def test_a_failed_export_leaves_the_one_before(tmp_path: Path) -> None:
    source, out = navidrome(tmp_path), tmp_path / "usage.sqlite3"
    first = export(source, out)
    content = out.read_bytes()
    conn = sqlite3.connect(source)
    conn.execute("DROP TABLE playqueue")  # (as another program's database: not exported)
    conn.execute("DROP TABLE scrobbles")  # (a Navidrome without it is fine)
    conn.close()
    with pytest.raises(ExportError, match="not a Navidrome database this version can read"):
        export(source, out)
    assert out.read_bytes() == content and UsageSource(out, export=True).snapshot() == first
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "navidrome.db",
        "usage.sqlite3",
        "usage.sqlite3.lock",
    ]
    with pytest.raises(ExportError, match="no database at"):
        export(tmp_path / "missing.db", out)
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE playqueue (items TEXT)")
    conn.close()
    assert export(source, out).at > first.at  # without scrobbles
    tables = {str(r[0]) for r in sqlite3.connect(out).execute("SELECT name FROM sqlite_master")}
    assert "scrobbles" not in tables and "playqueue" in tables
    # The command: one export, its exit status.
    main(["usage-export", "--navidrome-db", str(source), "--out", str(out)])
    with pytest.raises(SystemExit) as failed:
        main(["usage-export", "--navidrome-db", str(tmp_path / "missing.db"), "--out", str(out)])
    assert failed.value.code == 1


def test_the_export_never_replaces_anything_but_an_export(tmp_path: Path) -> None:
    """Given the same path twice it would replace Navidrome's database with the export of
    it. It writes over an export, or nothing: never the database it reads, a file beside
    it, or any other file. And one exporter writes a given export at a time."""
    source = navidrome(tmp_path)
    content = source.read_bytes()
    for name in ("navidrome.db", "navidrome.db-wal", "navidrome.db-shm"):
        with pytest.raises(ExportError, match="is Navidrome's database itself: not written"):
            export(source, tmp_path / name)
    link = tmp_path / "linked.db"
    link.symlink_to(source)
    with pytest.raises(ExportError, match="is Navidrome's database itself"):
        export(source, link)
    assert source.read_bytes() == content
    other = tmp_path / "notes.sqlite3"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE notes (text)")
    conn.close()
    kept = other.read_bytes()
    with pytest.raises(ExportError, match="exists and is not a usage export: not overwritten"):
        export(source, other)
    plain = tmp_path / "plain.txt"
    plain.write_text("something else")
    with pytest.raises(ExportError, match="not a usage export"):
        export(source, plain)
    assert other.read_bytes() == kept and plain.read_text() == "something else"
    out = tmp_path / "usage.sqlite3"
    out.touch()  # (an empty file is nothing)
    first = export(source, out)
    # (another exporter of this file, at work)
    with writing(out), pytest.raises(ExportError, match="another usage-export is writing"):
        export(source, out)
    assert UsageSource(out, export=True).snapshot() == first
    assert export(source, out).at > first.at  # (an export is replaced by the next)


def test_the_export_names_the_database_it_read_and_requests_are_numbered_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database put in the place of the one being read would be exported under the
    other's name: the file is looked at again once it is read. And requests written at
    the same time (a dry run while the daily check waits) never share a number, and the
    request file ends at the highest."""
    source, out = navidrome(tmp_path), tmp_path / "usage.sqlite3"
    first = export(source, out)
    names = iter(["1:1", "1:2"])  # (another file at that path by the time it is read)
    monkeypatch.setattr(export_module, "_identity", lambda path: next(names))
    with pytest.raises(ExportError, match="was replaced while it was read"):
        export(source, out)
    monkeypatch.undo()
    assert UsageSource(out, export=True).snapshot() == first
    usage = UsageSource(out, export=True)
    marks: list[Mark] = []
    askers = [threading.Thread(target=lambda: marks.append(usage._ask())) for _ in range(24)]
    for asker in askers:
        asker.start()
    for asker in askers:
        asker.join()
    assert sorted(mark.number for mark in marks) == list(range(1, 25))
    assert request_path(out).read_text().strip() == f"{usage._asker}:24"
    assert [m.number for m in sorted(marks, key=lambda m: m.at)] == list(range(1, 25))


def test_an_idle_navidromes_database_is_read_through_a_read_only_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Navidrome's database is in WAL mode: its ``-wal`` and ``-shm`` files exist only while
    a program has it open, and nobody can make them in a folder mounted read-only. An idle
    Navidrome's database is then read as the file is - and the export is kept only if
    that file did not change meanwhile. Only then: any other reason it cannot be opened
    is an error, tried again later."""
    monkeypatch.setattr(export_module, "SETTLED_NS", 300_000_000)
    folder = tmp_path / "navidrome-data"
    folder.mkdir()
    source, out = navidrome(folder), tmp_path / "usage.sqlite3"
    conn = sqlite3.connect(source)
    assert conn.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    conn.execute("INSERT INTO bookmark VALUES ('user-1', 's7', '', 1)")
    conn.commit()
    conn.close()  # (the last connection: everything is in the database file, no -wal left)
    assert [p.name for p in folder.iterdir()] == ["navidrome.db"]
    old = time.time() - 60
    os.utime(source, (old, old))
    folder.chmod(0o555)
    try:
        with pytest.raises(sqlite3.OperationalError):  # the usual way does not work there
            sqlite3.connect(f"file:{source}?mode=ro", uri=True).execute("SELECT * FROM bookmark")
        export(source, out)
        usage, _ = UsageSource(out, export=True).open()
        assert in_use(usage, ["s7"]) == ["bookmarked"]
        usage.close()
        assert [p.name for p in folder.iterdir()] == ["navidrome.db"]  # nothing made there
        # Written a moment ago: waited out first (a change in the same tick of the file
        # system's clock would not show afterwards).
        os.utime(source)
        started = time.monotonic()
        export(source, out)
        assert time.monotonic() - started >= 0.2
        # Changed while it was read: not one state of it - the export before stays.
        os.utime(source, (old, old))
        before = out.read_bytes()
        opened = export_module._open

        def open_then_change(path: Path) -> Any:
            conn, _ = opened(path)
            return conn, lambda: False

        monkeypatch.setattr(export_module, "_open", open_then_change)
        with pytest.raises(ExportError, match="changed while it was read"):
            export(source, out)
        assert out.read_bytes() == before
        monkeypatch.setattr(export_module, "_open", opened)
        # In use by a program whose files cannot be read, or left with a journal: not
        # read as it is.
        folder.chmod(0o755)
        (folder / "navidrome.db-wal").write_bytes(b"")
        folder.chmod(0o555)
        with pytest.raises(ExportError, match="cannot be read now"):
            export(source, out)
        assert out.read_bytes() == before
    finally:
        folder.chmod(0o755)
    # A database that is not in WAL mode is never read that way.
    (folder / "navidrome.db-wal").unlink()
    conn = sqlite3.connect(source)
    assert conn.execute("PRAGMA journal_mode = DELETE").fetchone() == ("delete",)
    conn.close()
    (folder / "navidrome.db-journal").write_bytes(b"not a journal")
    os.utime(source, (old, old))
    folder.chmod(0o555)
    try:
        with pytest.raises(ExportError):
            export(source, out)
    finally:
        folder.chmod(0o755)
    assert out.read_bytes() == before


def test_navidromes_database_itself_is_read_as_a_copy_of_what_the_checks_need(
    tmp_path: Path,
) -> None:
    """``database_path``: what the checks read of Navidrome's database is copied as it is
    read (one read transaction, as the exporter's), and the copy goes when it is closed."""
    source, out = navidrome(tmp_path), tmp_path / "usage.sqlite3"
    export(source, out)
    left = Path(tempfile.gettempdir()) / "shijhon-usage-left-by-a-kill"
    left.mkdir(exist_ok=True)
    (left / "usage.sqlite3").write_bytes(b"")
    os.utime(left, (time.time() - 7200, time.time() - 7200))
    began = time.time()
    direct = UsageSource(source, export=False)
    conn, taken = direct.open()
    assert began <= taken.at <= time.time() and in_use(conn, ["s3"]) == ["bookmarked"]
    assert direct.covers(taken, Mark(began)) and not direct.covers(taken, Mark(time.time() + 9))
    [copied] = [str(r[2]) for r in conn.execute("PRAGMA database_list") if r[1] == "main"]
    assert Path(copied).exists() and Path(copied) != source
    # Private to this user, in a folder of its own; what a kill left earlier is gone.
    assert Path(copied).stat().st_mode & 0o777 == 0o600
    assert Path(copied).parent.stat().st_mode & 0o777 == 0o700
    assert not left.exists()
    assert {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master")}.isdisjoint(
        {"user", "property", "playlist"}
    )
    conn.close()
    assert not Path(copied).parent.exists()
    with pytest.raises(WrongSource, match="is a usage export, not Navidrome's database"):
        UsageSource(out, export=False).open()
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(WrongSource, match="not a Navidrome database this version can read"):
        UsageSource(empty, export=False).open()
    with pytest.raises(FileNotFoundError):
        UsageSource(tmp_path / "missing.db", export=False).open()


def unwritable(usage: UsageSource, folder: Path, ask: Any = None) -> Mark:
    """Ask while the export's folder cannot be written."""
    folder.chmod(0o555)
    try:
        found: Mark = (ask or usage._ask)()
        return found
    finally:
        folder.chmod(0o755)


@pytest.mark.anyio
async def test_an_export_covers_a_moment_only_when_it_answers_the_request_made_then(
    tmp_path: Path,
) -> None:
    """The cleanup acts only on an export made after the moment it asks about. Shijhon
    writes a request; the exporter stamps its export with the request it read before it
    began; an export covers the moment when it answers that request or a later one of the
    same process - whatever the two clocks say."""
    source, out = navidrome(tmp_path), tmp_path / "usage.sqlite3"
    export(source, out)  # (as the exporter's first, before anything was asked)
    with pytest.raises(WrongSource, match="is not a usage export"):
        UsageSource(source, export=True).open()
    await UsageSource(source, export=False).asked(wait=0)  # the database itself: no wait
    usage = UsageSource(out, export=True)
    with pytest.raises(NotFresh, match=r"no usage export made after .* is the exporter running"):
        await usage.asked(wait=0.6)
    request = request_path(out).read_text().strip()
    assert request == f"{usage._asker}:1"  # asked for
    answered = export(source, out)  # (as the exporter does when asked)
    assert answered.answers == request
    first = Mark(time.time(), usage._asker, 1)
    await usage.covered(first, wait=0)
    second = usage._ask()
    assert second.number == 2 and not usage.covers(answered, second)
    with pytest.raises(NotFresh):
        await usage.covered(second, wait=0)
    later = export(source, out)
    assert usage.covers(later, second) and usage.covers(later, first)  # a later one too
    # Not by the clock: an export stamped in the future that answers nothing of ours does
    # not cover it, and one stamped long ago that answers the request does.
    assert not usage.covers(Snapshot(time.time() + 3600, "another:9", later.source), second)
    assert not usage.covers(Snapshot(time.time() + 3600, "", later.source), second)
    assert usage.covers(Snapshot(0.0, f"{usage._asker}:7", later.source), second)
    # Another process (a restart, the fills-undo command) asks under its own name.
    other = UsageSource(out, export=True)
    theirs = other._ask()
    assert theirs.asker != usage._asker and not other.covers(later, theirs)
    assert other.covers(export(source, out), theirs)
    # Where Shijhon cannot write the request, nothing covers the moment:
    # an export is never taken by its time - the exporter's clock may be
    # ahead, a clock may step back - and the check ends there.
    asked = usage._asked
    with pytest.raises(NotAsked, match=r"could not be written .*must be writable by Shijhon"):
        unwritable(usage, tmp_path)
    assert usage._asked == asked
    assert request_path(out).read_text().strip() == f"{other._asker}:1"  # (as it was)
    real_ask = usage._ask
    usage._ask = lambda: unwritable(usage, tmp_path, real_ask)  # type: ignore[method-assign]
    with pytest.raises(NotFresh, match="could not be written"):
        await usage.asked(wait=5)  # (at once: there is nothing to wait for)
    usage._ask = real_ask  # type: ignore[method-assign]
    unasked = Mark(time.time())
    assert not usage.covers(Snapshot(unasked.at + 3600), unasked)
    assert not usage.covers(Snapshot(unasked.at + 3600, later.answers, later.source), unasked)
    with pytest.raises(NotFresh, match="there is none yet"):
        await UsageSource(tmp_path / "none.sqlite3", export=True).asked(wait=0)
    conn = sqlite3.connect(out)
    conn.execute("UPDATE shijhon_usage_export SET format = 99")
    conn.commit()
    conn.close()
    with pytest.raises(WrongSource, match="another version"):
        UsageSource(out, export=True).open()
    with pytest.raises(WrongSource, match="another version"):  # (not "none yet")
        await UsageSource(out, export=True).asked(wait=0)


def test_the_export_is_read_when_both_sources_are_configured(tmp_path: Path) -> None:
    """A deployment moving from the mount to the export may name both for a while: that
    does not keep Shijhon from starting, and the export is the one read."""
    settings = load_settings(
        None, navidrome={"usage_export_path": tmp_path / "u", "database_path": tmp_path / "n"}
    )
    nd = settings.navidrome
    found = configured(nd.usage_export_path, nd.database_path)
    assert found is not None and found.export and found.path == tmp_path / "u"
    assert configured(None, tmp_path / "n").export is False  # type: ignore[union-attr]
    assert configured(None, None) is None
