from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import anyio
import pytest

from shijhon.store.db import Store, apply_migrations, load_migrations


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:", isolation_level=None)


def test_applies_in_order_and_is_idempotent() -> None:
    conn = _conn()
    migrations = [
        (1, "0001_a.sql", "CREATE TABLE a (x INTEGER);"),
        (2, "0002_b.sql", "CREATE TABLE b (y TEXT);\nINSERT INTO a VALUES (1);"),
    ]
    assert apply_migrations(conn, migrations) == 2
    assert apply_migrations(conn, migrations) == 2
    assert conn.execute("SELECT count(*) FROM a").fetchone()[0] == 1


def test_failed_migration_rolls_back() -> None:
    conn = _conn()
    migrations = [(1, "0001_a.sql", "CREATE TABLE a (x INTEGER);\nINSERT INTO nope VALUES (1);")]
    with pytest.raises(RuntimeError):
        apply_migrations(conn, migrations)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None


def test_rejects_gaps_and_newer_databases() -> None:
    with pytest.raises(RuntimeError):
        apply_migrations(_conn(), [(2, "0002_a.sql", "SELECT 1;")])
    conn = _conn()
    conn.execute("PRAGMA user_version = 5")
    with pytest.raises(RuntimeError):
        apply_migrations(conn, [(1, "0001_a.sql", "SELECT 1;")])


def test_database_file_is_private(tmp_path: Path) -> None:
    async def main() -> None:
        store = await Store.open(tmp_path / "state" / "db.sqlite3")
        await store.close()

    anyio.run(main)
    mode = stat.S_IMODE((tmp_path / "state" / "db.sqlite3").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_match_freshness_upgrade_preserves_saved_plans_and_owner_decisions() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    assert apply_migrations(conn, migrations[:6]) == 6
    for outcome in ("none", "deferred", "kept", "review", "failed"):
        conn.execute(
            "INSERT INTO album_matches(album_id,scope,outcome,checked_at,plan) VALUES(?,?,?,?,?)",
            (outcome, "demo.xx", outcome, 123, '{"owned": [["native", 1, 2, "Song", 3000]]}'),
        )
    before = conn.execute("SELECT * FROM album_matches ORDER BY album_id").fetchall()
    assert apply_migrations(conn) == migrations[-1][0]
    after = conn.execute("SELECT * FROM album_matches ORDER BY album_id").fetchall()
    assert [row[:-2] for row in after] == before
    assert all(row[-2:] == (0, None) for row in after)


def test_audio_agent_upgrade_preserves_batches_and_granted_scopes() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    assert apply_migrations(conn, migrations[:8]) == 8
    conn.execute("INSERT INTO audio_owners VALUES ('owner','admin','saved-auth','generation',1)")
    conn.execute(
        "INSERT INTO audio_batches VALUES"
        " ('batch','owner','generation',1,'native-list','Handmade','[\"song\"]',1,'paused','')"
    )
    conn.execute(
        "INSERT INTO playlist_owners(id,username,connection,generation)"
        " VALUES ('owner','admin','{}','generation')"
    )
    conn.execute(
        "INSERT INTO playlist_agent_tokens(id,owner,library,name,scopes,created)"
        " VALUES ('grant','owner','1','Agent','[\"create\",\"status\"]',1)"
    )
    before = conn.execute("SELECT * FROM audio_batches").fetchone()
    assert apply_migrations(conn) == migrations[-1][0]
    after = conn.execute("SELECT * FROM audio_batches").fetchone()
    assert after[:-1] == before and after[-1] is None
    assert (
        conn.execute("SELECT scopes FROM playlist_agent_tokens").fetchone()[0]
        == '["create","status"]'
    )
    assert conn.execute("SELECT COUNT(*) FROM audio_agent_requests").fetchone()[0] == 0


def test_audio_disconnect_upgrade_preserves_existing_work_until_disconnect() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    conn.execute("PRAGMA foreign_keys=ON")
    assert apply_migrations(conn, migrations[:11]) == 11
    conn.execute(
        "INSERT INTO playlist_owners(id,username,connection,generation)"
        " VALUES ('owner','admin','{}','g')"
    )
    conn.execute(
        "INSERT INTO playlist_agent_tokens(id,owner,library,name,scopes,created)"
        " VALUES ('grant','owner','1','Agent','[\"downloads\"]',1)"
    )
    conn.execute("INSERT INTO audio_owners VALUES ('owner','admin','saved-auth','g',1)")
    conn.execute(
        "INSERT INTO audio_batches"
        "(id,owner,generation,library_id,playlist_id,title,snapshot,created,state,creator)"
        " VALUES ('batch','owner','g',1,'playlist','Handmade','[]',1,'queued','grant')"
    )
    conn.execute(
        "INSERT INTO audio_agent_requests VALUES ('grant','owner',1,'key','playlist','batch')"
    )
    conn.execute(
        "INSERT INTO audio_agent_requests VALUES ('grant','owner',1,'tombstone','playlist',NULL)"
    )
    before = conn.execute("SELECT * FROM audio_batches").fetchall()
    assert apply_migrations(conn) == migrations[-1][0]
    assert conn.execute("SELECT * FROM audio_batches").fetchall() == before
    assert conn.execute("SELECT COUNT(*) FROM audio_agent_requests").fetchone()[0] == 2
    conn.execute("DELETE FROM playlist_owners WHERE id='owner'")
    for sql in (
        "SELECT COUNT(*) FROM playlist_agent_tokens",
        "SELECT COUNT(*) FROM audio_batches",
        "SELECT COUNT(*) FROM audio_agent_requests",
    ):
        assert conn.execute(sql).fetchone()[0] == 0
    assert conn.execute("SELECT authorization FROM audio_owners").fetchone()[0] is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_release_index_upgrade_preserves_rows_and_indexes_cascades() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    conn.execute("PRAGMA foreign_keys=ON")
    assert apply_migrations(conn, migrations[:25]) == 25
    conn.execute(
        "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
        " VALUES ('demo:r','r','album','Glass Garden','Ivo Lane','{}','{}',1)"
    )
    conn.execute("INSERT INTO track_links VALUES ('demo:t','native','demo:r',1)")
    before = conn.execute("SELECT * FROM track_links").fetchall()
    assert apply_migrations(conn, migrations[:26]) == 26
    assert conn.execute("SELECT * FROM track_links").fetchall() == before
    for table in ("placeholders", "track_links"):
        plan = conn.execute(
            f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE release_ref = ?",  # noqa: S608 - fixed tables
            ("demo:r",),
        ).fetchall()
        assert any(f"USING INDEX {table}_release" in row[3] for row in plan)
    deletion = conn.execute("EXPLAIN QUERY PLAN DELETE FROM releases WHERE ref='demo:r'").fetchall()
    assert all("SCAN" not in row[3] for row in deletion)
    conn.execute("DELETE FROM releases WHERE ref='demo:r'")
    assert conn.execute("SELECT * FROM track_links").fetchall() == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # Index-only rollback leaves every table intact and lets the previous build open it.
    conn.execute("DROP INDEX placeholders_release")
    conn.execute("DROP INDEX track_links_release")
    conn.execute("PRAGMA user_version=25")
    assert apply_migrations(conn, migrations[:25]) == 25


def test_received_cleanup_upgrade_keeps_receipts_and_reservations_until_deleted() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    conn.execute("PRAGMA foreign_keys=ON")
    assert apply_migrations(conn, migrations[:26]) == 26
    conn.execute(
        "INSERT INTO music_import_jobs"
        " (id,owner_id,library_id,root_id,config_id,request_key,state,created)"
        " VALUES ('job','owner','1','root','old-binding','request','cancelled',1)"
    )
    for state in ("excluded", "complete", "placing", "joined"):
        conn.execute(
            "INSERT INTO music_import_items"
            " (id,asset_id,job_id,client_key,display_path,expected_size,reserved_size,"
            "state,received_size,staging_name,final_name) VALUES (?,?, 'job',?,?,4,4,?,4,?,?)",
            (state, state, state, state, state, state, state),
        )
    before = conn.execute("SELECT * FROM music_import_items ORDER BY id").fetchall()
    assert apply_migrations(conn) == migrations[-1][0]
    after = conn.execute("SELECT * FROM music_import_items ORDER BY id").fetchall()
    assert [r[:-3] for r in after] == before
    assert all(r[-3:-1] == ("", "") for r in after)
    assert all(r[-1] == ("batch-job/" + r[4] if r[8] == "joined" else r[13]) for r in after)
    assert conn.execute("SELECT config_id FROM music_import_jobs").fetchone()[0] == "old-binding"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
