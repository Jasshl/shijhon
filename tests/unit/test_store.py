from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import anyio
import pytest

from shijhon.delivery.addon import addon_key
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


def test_addon_key_upgrade_gives_each_addon_the_key_its_name_made() -> None:
    """Schema 32: an add-on's key is stored. Each existing add-on gets the key its name
    made until now - so the IDs of its catalog's items, and references to that catalog,
    keep resolving - and of several with one name the one that answered for it keeps it."""
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    assert apply_migrations(conn, migrations[:31]) == 31
    for name, enabled, position in (
        ("Glass Garden", 1, 1),
        ("音楽", 1, 2),
        ("Twin", 0, 3),  # off: the enabled one of its name answered for it
        ("Twin", 1, 4),
        ("Twin", 1, 5),
    ):
        conn.execute(
            "INSERT INTO sources (name, base_url, enabled, position, created_at)"
            " VALUES (?, 'https://addon.example.invalid', ?, ?, 1)",
            (name, enabled, position),
        )
    before = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    assert apply_migrations(conn, migrations[:32]) == 32
    rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    assert [row[:-1] for row in rows] == before  # nothing else changed
    keys = [row[-1] for row in rows]
    assert keys[:2] == [addon_key("Glass Garden"), addon_key("音楽")]
    assert keys[3] == addon_key("Twin") and len(set(keys)) == len(keys)
    assert keys[2].startswith("addontwin") and keys[4].startswith("addontwin")
    with pytest.raises(sqlite3.IntegrityError):  # one key, one add-on
        conn.execute(
            "INSERT INTO sources (name, key, base_url, position, created_at)"
            " VALUES ('Other', ?, 'https://other.example.invalid', 6, 1)",
            (keys[0],),
        )


def test_addon_steps_upgrade_keeps_the_order_and_drops_the_routing_modes() -> None:
    """Schema 34: the add-ons' plan replaces the routing modes. Every add-on starts as a
    step of its own, in the order it had; the saved settings of the modes and their roles
    are deleted, and the primary's timeouts and misses are kept as every add-on's."""
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    assert apply_migrations(conn, migrations[:33]) == 33
    for name, position in (("First", 2), ("Second", 1)):
        conn.execute(
            "INSERT INTO sources (name, key, base_url, position, created_at)"
            " VALUES (?, ?, 'https://addon.example.invalid', ?, 1)",
            (name, addon_key(name), position),
        )
    removed = (
        "routing",
        "primary_source",
        "reliable_source",
        "primary_budget_seconds",
        "reliable_lookup_after_seconds",
        "max_attempts",
        "primary_cooldown_switches",
        "primary_release_miss_minutes",
    )
    kept = {"primary_cooldown_timeouts": "3", "primary_miss_hours": "12.0", "cooldown_seconds": "5"}
    for key, value in {**dict.fromkeys(removed, '"x"'), **kept}.items():
        conn.execute(
            "INSERT INTO saved_settings (section, key, value, saved_at, saved_by)"
            " VALUES ('delivery', ?, ?, 1, 'tester')",
            (key, value),
        )
    # Another section's setting of the same name is not the routing's.
    conn.execute(
        "INSERT INTO saved_settings (section, key, value, saved_at, saved_by)"
        " VALUES ('catalog', 'routing', ?, 1, 'tester')",
        ('"x"',),
    )
    assert apply_migrations(conn, migrations[:34]) == 34
    rows = conn.execute("SELECT name, position, step FROM sources ORDER BY position").fetchall()
    assert [tuple(row) for row in rows] == [("Second", 1, 1), ("First", 2, 2)]
    saved = conn.execute("SELECT section, key, value FROM saved_settings").fetchall()
    assert sorted(tuple(row) for row in saved) == [
        ("catalog", "routing", '"x"'),
        ("delivery", "cooldown_seconds", "5"),
        ("delivery", "cooldown_timeouts", "3"),
        ("delivery", "miss_hours", "12.0"),
    ]


@pytest.mark.parametrize("mode", ["primary_first", "ready_first", "ordered"])
def test_addon_steps_upgrade_keeps_what_the_routing_mode_did(mode: str) -> None:
    """Schema 34 converts the mode once: under primary-first the primary comes first and an
    add-on whose check says ready takes over after the primary's time; under ready-first
    such an add-on goes first; in order, the list stays as it is."""
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    assert apply_migrations(conn, migrations[:33]) == 33
    for name, position in (("One", 1), ("Two", 2), ("Three", 3)):
        conn.execute(
            "INSERT INTO sources (name, key, base_url, position, created_at)"
            " VALUES (?, ?, 'https://addon.example.invalid', ?, 1)",
            (name, addon_key(name), position),
        )
    for key, value in (
        ("routing", f'"{mode}"'),
        ("primary_source", '"Three"'),
        ("primary_budget_seconds", "3.0"),
    ):
        conn.execute(
            "INSERT INTO saved_settings (section, key, value, saved_at, saved_by)"
            " VALUES ('delivery', ?, ?, 1, 'tester')",
            (key, value),
        )
    assert apply_migrations(conn, migrations[:34]) == 34
    order = [r[0] for r in conn.execute("SELECT name FROM sources ORDER BY step, position")]
    saved = dict(
        conn.execute("SELECT key, value FROM saved_settings WHERE section = 'delivery'").fetchall()
    )
    if mode == "primary_first":
        assert order == ["Three", "One", "Two"]
        assert saved == {"ready_checks": "true", "ready_handover_seconds": "3.0"}
    elif mode == "ready_first":
        assert order == ["One", "Two", "Three"]
        assert saved == {"ready_checks": "true", "ready_handover_seconds": "0"}
    else:
        assert order == ["One", "Two", "Three"] and saved == {}


def test_step_times_upgrade_keeps_the_plan_and_the_download_times() -> None:
    """Schema 35 parts an add-on's one time in two: its step's time (the plan's, as it was)
    and its time for a download (the same value, kept); its own "slow after" is unset."""
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    conn = _conn()
    assert apply_migrations(conn, migrations[:34]) == 34
    for name, position, step, budget in (
        ("One", 1, 1, 4.5),
        ("Two", 2, 1, 4.5),
        ("Three", 3, 2, None),
    ):
        conn.execute(
            "INSERT INTO sources (name, key, base_url, position, step, budget_seconds, created_at)"
            " VALUES (?, ?, 'https://addon.example.invalid', ?, ?, ?, 1)",
            (name, addon_key(name), position, step, budget),
        )
    assert apply_migrations(conn, migrations[:35]) == 35
    rows = conn.execute(
        "SELECT name, step, step_seconds, budget_seconds, slow_after FROM sources ORDER BY position"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("One", 1, 4.5, 4.5, None),
        ("Two", 1, 4.5, 4.5, None),
        ("Three", 2, None, None, None),
    ]
