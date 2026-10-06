"""``shijhon fills-undo``: what counts as in use -
shares and delivered audio too - how a part's songs count toward the fill policy, the order
of the undo's steps, the count it reports, and ``--expect``."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from shijhon.cli import main
from shijhon.fill import undo
from shijhon.fill.fills import FillPolicy
from shijhon.navidrome.usage import in_use
from shijhon.placeholders.engine import MaterializeError, Removal
from shijhon.store import Store
from shijhon.store.db import prepare_database
from shijhon.store.writer import AlreadyRunning, WriterLock

FOLDER = "_shijhon"
NAVIDROME_TABLES = """
CREATE TABLE album (id TEXT PRIMARY KEY, name TEXT, album_artist TEXT, album_artist_id TEXT);
CREATE TABLE media_file (id TEXT PRIMARY KEY, album_id TEXT, path TEXT, missing INTEGER,
                         artist_id TEXT, album_artist_id TEXT);
CREATE TABLE annotation (item_id TEXT, item_type TEXT, starred INTEGER, rating INTEGER,
                         play_count INTEGER, play_date TEXT, starred_at TEXT, rated_at TEXT);
CREATE TABLE playlist_tracks (playlist_id TEXT, media_file_id TEXT);
CREATE TABLE bookmark (item_id TEXT);
CREATE TABLE playqueue (items TEXT);
CREATE TABLE share (id TEXT, resource_ids TEXT, resource_type TEXT);
"""


def shijhon_db(tmp_path: Path) -> Path:
    """Four filled albums of one owned song each (1 of 10 tracks: below the policy); album
    "a4" also has two songs of other albums playing for its tracks (1 + 2 of 10: allowed)."""
    path = tmp_path / "state" / "shijhon.sqlite3"
    prepare_database(path)
    conn = sqlite3.connect(path)
    for n in range(1, 5):
        ref = f"x:{n}"
        conn.execute(
            "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
            " album_tags, data, created_at) VALUES (?, ?, ?, ?, ?, 'Artist', '{}', '{}', 1)",
            [ref, f"{FOLDER}/r{n}", f"a{n}", f"a{n}", f"Album {n}"],
        )
        conn.execute("INSERT INTO track_links VALUES (?, ?, ?, 1)", [f"x:{n}-0", f"own{n}", ref])
        for t in range(1, 10):
            song = f"p{n}-{t}"
            conn.execute("INSERT INTO track_links VALUES (?, ?, ?, 0)", [f"x:{n}-{t}", song, ref])
            conn.execute(
                "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref,"
                " release_ref, title, artist, album, duration_ms, disc, track, tags,"
                " created_at, backing_song_id) VALUES (?, ?, ?, ?, ?, 'T', 'A', 'B', 1000, 1,"
                " ?, '{}', 1, ?)",
                [song, f"{FOLDER}/r{n}/{t}.flac", f"{FOLDER}/r{n}/{t}.flac", f"x:{n}-{t}", ref,
                 t, f"part{t}" if n == 4 and t <= 2 else None],
            )  # fmt: skip
    conn.execute("UPDATE placeholders SET state = 'delivered' WHERE song_id = 'p2-5'")
    conn.commit()
    conn.close()
    return path


def navidrome_db(tmp_path: Path) -> Path:
    path = tmp_path / "navidrome.db"
    if path.exists():
        return path
    conn = sqlite3.connect(path)
    conn.executescript(NAVIDROME_TABLES)
    for n in range(1, 5):
        conn.execute(
            "INSERT INTO album (id, name, album_artist) VALUES (?, ?, 'Artist')",
            [f"a{n}", f"Album {n}"],
        )
        for t in range(1, 10):  # the placeholders, where Shijhon recorded them
            conn.execute(
                "INSERT INTO media_file (id, album_id, path, missing) VALUES (?, ?, ?, 0)",
                [f"p{n}-{t}", f"a{n}", f"{FOLDER}/r{n}/{t}.flac"],
            )
    conn.execute("INSERT INTO playlist_tracks VALUES ('pl1', 'p3-7')")
    conn.execute("INSERT INTO share VALUES ('s1', 'pl1', 'playlist')")  # shares p3-7
    conn.commit()
    conn.close()
    return path


def planned(tmp_path: Path) -> dict[str, undo.Undo]:
    items = undo.plan(shijhon_db(tmp_path), navidrome_db(tmp_path), FillPolicy())
    return {u.album_id: u for u in items}


def test_shares_and_delivered_audio_keep_an_album_and_parts_count_as_owned(
    tmp_path: Path,
) -> None:
    found = planned(tmp_path)
    assert set(found) == {"a1", "a2", "a3"}  # a4: 1 owned + 2 from another album, of 10
    assert found["a1"].kept == [] and found["a1"].reason == "below the fill policy (1 of 10 owned)"
    assert found["a2"].kept == ["delivered audio in place"]
    assert found["a3"].kept == ["in a playlist", "shared"]


def test_a_share_of_the_album_or_a_song_counts(tmp_path: Path) -> None:
    conn = sqlite3.connect(navidrome_db(tmp_path))
    songs = ["p1-1", "p1-2"]
    conn.executescript("DELETE FROM share; DELETE FROM playlist_tracks;")
    conn.execute("INSERT INTO share VALUES ('s2', 'zz,a1', 'album')")
    conn.commit()
    assert in_use(conn, songs, "a1") == ["shared"]
    conn.execute("UPDATE share SET resource_ids = 'p1-2', resource_type = 'media_file'")
    assert in_use(conn, songs, "a1") == ["shared"]
    conn.execute("UPDATE share SET resource_ids = 'p9-9'")
    assert in_use(conn, songs, "a1") == []
    conn.close()


def test_a_fill_is_kept_by_the_cleanups_rule(tmp_path: Path) -> None:
    """What keeps a release from the cleanup keeps a fill from the undo - a stream or a
    download Shijhon served, a use an earlier check saw that ended since, a favorite of
    the album given after the fill - and a database that does not know its songs undoes
    nothing."""
    database = shijhon_db(tmp_path)
    conn = sqlite3.connect(database)
    conn.execute("UPDATE placeholders SET last_used_at = 5 WHERE song_id = 'p1-3'")
    conn.execute("UPDATE placeholders SET state = 'placeholder' WHERE song_id = 'p2-5'")
    conn.execute("INSERT INTO seen_uses VALUES ('x:2', 'in a play queue', 7)")
    conn.commit()
    conn.close()
    navidrome = navidrome_db(tmp_path)

    def found() -> dict[str, undo.Undo]:
        items = undo.plan(database, navidrome, FillPolicy())
        return {u.album_id: u for u in items}

    assert found()["a1"].kept == ["streamed or downloaded"]
    assert found()["a2"].kept == ["in a play queue once"]
    conn = sqlite3.connect(navidrome)
    conn.executescript("DELETE FROM share; DELETE FROM playlist_tracks;")
    conn.execute(  # the album itself, favorited after the fill (made at 1)
        "INSERT INTO annotation (item_id, item_type, starred, starred_at)"
        " VALUES ('a3', 'album', 1, '2026-09-01 10:00:00')"
    )
    conn.commit()
    assert found()["a3"].kept == ["its album favorited"]
    conn.execute("DELETE FROM annotation")
    conn.execute("UPDATE media_file SET path = 'elsewhere' WHERE id = 'p3-4'")
    conn.commit()
    conn.close()
    assert found()["a3"].kept == ["Navidrome's records do not have its songs at their paths"]
    # Not a Navidrome database at all: an error, nothing listed as undoable.
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(sqlite3.DatabaseError, match="is not a Navidrome database"):
        undo.plan(database, empty, FillPolicy())


class Engine:
    """The engine's removal: records the matches it found at that moment; ``gone``:
    releases already removed, ``broken``: releases whose removal fails, ``used``: what its
    own records keep."""

    def __init__(
        self,
        store: Store,
        gone: set[str],
        broken: set[str] | None = None,
        used: dict[str, list[str]] | None = None,
        navidrome: Path | None = None,
    ) -> None:
        self.store = store
        self.gone = gone
        self.broken = broken or set()
        self.used = used or {}
        self.navidrome = navidrome  # Navidrome's own database: it shows what is taken out
        self.matches_seen: list[int] = []
        self.checks: list[tuple[str, bool]] = []
        self.noted: dict[str, list[str]] = {}

    def clock(self) -> float:
        return 9.0

    async def note_uses(self, seen: dict[str, list[str]], at: float) -> None:
        self.noted.update(seen)

    async def remove_release(self, ref: str, *, check: Any, also: Any) -> Removal:
        # (the engine's default: kept as a record, so an old ID adds it back)
        row = await self.store.fetchone("SELECT COUNT(*) AS n FROM album_matches")
        assert row is not None
        self.matches_seen.append(int(row["n"]))
        if ref in self.broken:
            raise MaterializeError("the removed placeholders are still listed")
        if ref in self.gone:
            return Removal()
        if ref in self.used:
            return Removal(kept=self.used[ref])
        rows = await self.store.fetchall("SELECT * FROM placeholders WHERE release_ref = ?", [ref])
        for gone in (False, True):  # under its locks, and once the files are gone
            if gone and self.navidrome is not None:
                conn = sqlite3.connect(self.navidrome)
                conn.execute(
                    "UPDATE media_file SET missing = 1 WHERE album_id = ?", [f"a{ref[-1]}"]
                )
                conn.commit()
                conn.close()
            self.checks.append((ref, gone))
            if why := await check(rows, gone):
                return Removal(kept=why)
        async with self.store.transaction() as conn:
            await also(conn)
        return Removal(removed=9)


def outcome(result: undo.Applied) -> tuple[int, list[str]]:
    """What was undone, and what was not and why (the reason nothing more was tried last)."""
    stopped = [f"nothing more is undone: {result.stopped}"] if result.stopped else []
    return result.done, result.failed + stopped


async def matched(store: Store, albums: list[str]) -> None:
    for album in albums:
        await store.execute(
            "INSERT INTO album_matches (album_id, scope, outcome, release_ref, checked_at)"
            " VALUES (?, 'x.us', 'filled', ?, 1)",
            [album, f"x:{album[1]}"],
        )


async def matches(store: Store) -> list[str]:
    rows = await store.fetchall("SELECT album_id FROM album_matches ORDER BY album_id")
    return [str(r["album_id"]) for r in rows]


def unused(tmp_path: Path) -> Path:
    """Navidrome's database with nothing in use."""
    path = navidrome_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.executescript("DELETE FROM share; DELETE FROM playlist_tracks;")
    conn.commit()
    conn.close()
    return path


@pytest.mark.anyio
async def test_the_match_goes_with_the_release_and_only_removed_albums_count(
    tmp_path: Path,
) -> None:
    database, navidrome = shijhon_db(tmp_path), navidrome_db(tmp_path)
    items = undo.plan(database, navidrome, FillPolicy())
    store = await Store.open(database)
    try:
        await matched(store, ["a1", "a2", "a3"])
        engine = Engine(store, gone={"x:1"})
        done = outcome(await undo.apply(items, engine, store, navidrome))  # type: ignore[arg-type]
        left = await matches(store)
    finally:
        await store.close()
    assert done == (0, [])  # a1's release was already gone; a2 and a3 are kept
    assert engine.matches_seen == [3]  # (the match goes in the removal's transaction)
    assert left == ["a2", "a3"]  # a release already gone: its match still goes
    # What the list saw anyone use is recorded, as every check's sightings are.
    assert engine.noted == {"x:3": ["in a playlist", "shared"]}


@pytest.mark.anyio
async def test_a_failed_removal_is_reported_and_the_others_go_on(tmp_path: Path) -> None:
    database = shijhon_db(tmp_path)
    navidrome = unused(tmp_path)  # a3 not in use
    items = undo.plan(database, navidrome, FillPolicy())
    store = await Store.open(database)
    try:
        await matched(store, ["a1", "a3"])
        engine = Engine(store, gone=set(), broken={"x:1"}, navidrome=navidrome)
        done = outcome(await undo.apply(items, engine, store, navidrome))  # type: ignore[arg-type]
        left = await matches(store)
    finally:
        await store.close()
    assert done == (1, ["Artist - Album 1 (a1): the removed placeholders are still listed"])
    assert left == ["a1"]  # a failed removal keeps its match


@pytest.mark.anyio
async def test_use_is_looked_for_again_when_it_applies(tmp_path: Path) -> None:
    """The list is not trusted at apply time - the engine's own records (a stream
    since), Navidrome's as they are then (a playlist entry since), and its database must be
    the current one (the songs shown as missing once their files are gone)."""
    database = shijhon_db(tmp_path)
    navidrome = unused(tmp_path)
    items = undo.plan(database, navidrome, FillPolicy())
    assert [u.album_id for u in undo.to_undo(items)] == ["a1", "a3"]
    store = await Store.open(database)
    try:
        await matched(store, ["a1", "a3"])
        engine = Engine(store, gone=set(), used={"x:1": ["streamed or downloaded"]})
        conn = sqlite3.connect(navidrome)
        conn.execute("INSERT INTO playlist_tracks VALUES ('pl9', 'p3-2')")  # since the list
        conn.commit()
        done = outcome(await undo.apply(items, engine, store, navidrome))  # type: ignore[arg-type]
        assert done == (0, [
            "Artist - Album 1 (a1): kept - streamed or downloaded",
            "Artist - Album 3 (a3): kept - in a playlist",
        ])  # fmt: skip
        assert engine.checks == [("x:3", False)] and engine.noted == {"x:3": ["in a playlist"]}
        assert await matches(store) == ["a1", "a3"]
        # Nobody uses a3 now - but the database read still shows its songs as present once
        # their files are gone: a copy, not Navidrome's current one. Nothing more is undone.
        conn.execute("DELETE FROM playlist_tracks")
        conn.commit()
        conn.close()
        engine = Engine(store, gone=set())  # (a copy: it does not show the removal)
        items = list(reversed(items))  # a3 first, then a1
        done = outcome(await undo.apply(items, engine, store, navidrome))  # type: ignore[arg-type]
        assert done == (0, [
            "Artist - Album 3 (a3): kept - Navidrome's records do not show its songs as gone",
            "nothing more is undone: what was read is not Navidrome's current database"
            " (--navidrome-db, or what the usage export is made from)",
        ])  # fmt: skip
        assert engine.checks == [("x:3", False), ("x:3", True)]
        assert await matches(store) == ["a1", "a3"]
        # A copy made while a3 was out (its songs missing there): a3 stays - and that says
        # nothing of a1, which is undone by the same, current, database.
        conn = sqlite3.connect(navidrome)
        conn.execute("UPDATE media_file SET missing = 1 WHERE album_id = 'a3'")
        conn.commit()
        conn.close()
        engine = Engine(store, gone=set(), navidrome=navidrome)
        done = outcome(await undo.apply(items, engine, store, navidrome))  # type: ignore[arg-type]
        assert done == (1, ["Artist - Album 3 (a3): kept - Navidrome lists its songs as missing"])
        assert await matches(store) == ["a3"]
    finally:
        await store.close()


def test_an_artist_share_keeps_the_album(tmp_path: Path) -> None:
    conn = sqlite3.connect(navidrome_db(tmp_path))
    conn.executescript(
        "DELETE FROM share;"
        " UPDATE album SET album_artist_id = 'ar1' WHERE id = 'a1';"
        " INSERT INTO share VALUES ('s3', 'ar1', 'artist');"
    )
    assert in_use(conn, ["p1-1"], "a1") == ["shared"]
    conn.close()


def run_undo(
    tmp_path: Path, database: Path, *extra: str, result: undo.Applied | None = None
) -> list[Any]:
    config = tmp_path / "shijhon.toml"
    config.write_text(
        f'state_dir = "{tmp_path / "state"}"\n[navidrome]\nlibrary_path = "{tmp_path}"\n'
    )
    applied: list[Any] = []

    async def apply(
        settings: Any, items: Any, used: Path, navidrome: Any, listed: Any
    ) -> undo.Applied:
        applied.append(used)  # the database the list was made from
        return result or undo.Applied(done=1)

    argv = ["--config", str(config), "fills-undo", "--navidrome-db", str(navidrome_db(tmp_path)),
            "--database", str(database), *extra]  # fmt: skip
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("shijhon.cli._apply_undo", apply)
        main(argv)
    return applied


def test_apply_goes_ahead_only_for_the_dry_run_s_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = shijhon_db(tmp_path)
    shown = run_undo(tmp_path, database)  # the dry run
    assert shown == []
    listed = capsys.readouterr().out.split("(list ")[1].split(")")[0]
    with pytest.raises(SystemExit) as exit_info:
        run_undo(tmp_path, database, "--apply", "--expect", "000000000000")
    assert exit_info.value.code == 1
    assert "the list changed since the dry run" in capsys.readouterr().err
    assert run_undo(tmp_path, database, "--apply", "--expect", listed) == [database]
    assert "undone: 1 album(s)" in capsys.readouterr().out


def test_apply_is_refused_while_shijhon_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One process writes the library at a time - the running service holds a lock
    beside its database, and ``--apply`` refuses while it is held; a dry run only reads."""
    database = shijhon_db(tmp_path)
    service = WriterLock(database)
    service.acquire()
    try:
        assert run_undo(tmp_path, database) == []  # the dry run: while it runs too
        assert "Fills to undo:" in capsys.readouterr().out
        with pytest.raises(SystemExit) as exit_info:
            run_undo(tmp_path, database, "--apply")
        assert exit_info.value.code == 1
        said = capsys.readouterr()
        assert "is running on this database: stop it first" in said.err
        assert "Fills to undo" not in said.out  # refused before anything is read
        with pytest.raises(AlreadyRunning):  # a second service on this state alike
            WriterLock(database).acquire()
    finally:
        service.release()
    assert run_undo(tmp_path, database, "--apply") == [database]  # stopped: it applies
    again = WriterLock(database)  # and its lock is given back afterwards
    again.acquire()
    again.release()


def test_apply_works_on_the_configured_database_only_and_says_what_it_left(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lock is the configured state's, and so are the records of what was used: a copy
    of the database has neither, so ``--apply`` on one is refused (a dry run may read
    it). A link to the database is the database."""
    database = shijhon_db(tmp_path)
    copy = tmp_path / "copy.sqlite3"
    copy.write_bytes(database.read_bytes())
    assert run_undo(tmp_path, copy) == []  # the dry run reads it
    capsys.readouterr()
    with pytest.raises(SystemExit) as refused:
        run_undo(tmp_path, copy, "--apply")
    assert refused.value.code == 2
    assert "--apply works on the configured state's database only" in capsys.readouterr().err
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(database)
    assert WriterLock(link).path == WriterLock(database).path
    service = WriterLock(database)
    service.acquire()
    try:
        with pytest.raises(SystemExit):
            run_undo(tmp_path, link, "--apply")  # the same database: the same lock
        assert "is running on this database" in capsys.readouterr().err
    finally:
        service.release()
    # What was not undone, and what was not tried after a refusal, is counted as that.
    stopped = undo.Applied(done=1, gone=1, failed=["Album 3: kept - x"], stopped="why", untried=2)
    with pytest.raises(SystemExit) as failed:
        run_undo(tmp_path, link, "--apply", result=stopped)
    assert failed.value.code == 1
    out = capsys.readouterr().out
    assert "undone: 1 album(s), 1 already gone, 1 not undone, 2 not tried" in out
    assert "  not undone: Album 3: kept - x" in out and "  nothing more was tried: why" in out
