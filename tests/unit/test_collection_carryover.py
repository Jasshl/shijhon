# ruff: noqa: S608 -- SQL comes from the validated shared policy.
"""The catalog albums carried over at the upgrade to schema 24: in no account's library
until the owner gives them, once, to one named account's Shijhon library (and so its All).
Another account - one that already uses Shijhon too - never gets them unless they are
given to it instead, explicitly. (The command's own path, with Navidrome naming the
account: ``tests/acceptance/test_collection_carryover.py``.)"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shijhon.cli import main
from shijhon.collection import carryover
from shijhon.collection.policy import builtin_profile_id, default_profile_id, predicate
from shijhon.store import apply_migrations
from tests.unit.test_collection_reasons import schema23

OWNER, SECOND, LATER = "owner-id", "second-id", "later-id"


def upgraded() -> sqlite3.Connection:
    """A database like the owner's at schema 23, upgraded: two catalog albums no record
    explains (``old`` and ``gone``, the second being taken out at the upgrade - not carried
    over), one the second account played (``played``) and an owned album completed by a
    fill. The second account uses Shijhon already (its All profile, its play)."""
    conn, migrations = schema23()
    run = conn.execute
    run("INSERT INTO collection_library_policy VALUES ('1','personal',1,NULL)")
    run("INSERT INTO collection_index VALUES ('1','g','ready',7,1,'')")
    tracks = [
        ("native:o1", "o1", "native:own", "own", 1),
        ("catalog:f1", "f1", "fill", "own", 0),
        ("catalog:c1", "c1", "rel-old", "old", 0),
        ("catalog:c2", "c2", "rel-old", "old", 0),
        ("catalog:c3", "c3", "rel-old", "old", 0),
        ("catalog:p1", "p1", "rel-played", "played", 0),
        ("catalog:g1", "g1", "rel-gone", "gone", 0),
    ]
    for key, ident, release, album, original in tracks:
        run(
            "INSERT INTO collection_tracks(library_id,track_key,song_id,release_key,album_id,"
            "artist_id,title,album,artist,search_text,original,generation,indexed_at)"
            " VALUES ('1',?,?,?,?,'r','t','a','r','t',?,'g',0)",
            [key, ident, release, album, original],
        )
    for release, album, owned, removing in (
        ("fill", "own", "own", None),
        ("rel-old", "old", None, None),
        ("rel-played", "played", None, None),
        ("rel-gone", "gone", None, 1.0),
    ):
        run(
            "INSERT INTO releases(ref,folder,album_id,owned_album_id,title,artist,album_tags,"
            "data,created_at,removing_at) VALUES (?,?,?,?,'t','a','{}','{}',0,?)",
            [release, release, album, owned, removing],
        )
    for key, ident, release, _album, original in tracks:
        if not original:
            run("INSERT INTO track_links VALUES (?,?,?,0)", [key[8:], ident, release])
    for who in (OWNER, SECOND):
        run(
            "INSERT INTO collection_profiles(id,owner_id,kind,name,created)"
            " VALUES (?,?,'builtin','All',0)",
            [default_profile_id(who), who],
        )
    run("INSERT INTO collection_history VALUES (?,'1','catalog:p1',5,'{}')", [SECOND])
    assert apply_migrations(conn, migrations) == migrations[-1][0]
    return conn


def visible(conn: sqlite3.Connection, who: str, mode: str) -> set[str]:
    pred = predicate(who, "1", song_expression="t.song_id", mode=mode)  # type: ignore[arg-type]
    rows = conn.execute("SELECT t.song_id FROM collection_tracks t WHERE " + pred.sql, pred.params)
    return {r[0] for r in rows}


CARRIED = {"c1", "c2", "c3"}


def test_the_upgrade_records_the_carried_over_albums_for_no_account() -> None:
    conn = upgraded()
    assert conn.execute("SELECT release_ref FROM collection_carryover").fetchall() == [("rel-old",)]
    assert carryover.state(conn) == carryover.CarryOver(albums=1, songs=3, owner=None)
    for who in (OWNER, SECOND, LATER):
        assert visible(conn, who, "saved").isdisjoint(CARRIED)
        assert visible(conn, who, "all").isdisjoint(CARRIED)
    assert visible(conn, SECOND, "saved") == {"p1"}  # its own play still counts


def test_the_carried_over_albums_go_to_the_one_account_named() -> None:
    conn = upgraded()
    given = carryover.give(conn, OWNER, "owner", now=100.0)
    owner = carryover.Owner(OWNER, "owner", 100.0)
    assert given == carryover.Given(carryover.CarryOver(1, 3, owner), True, None)
    assert carryover.state(conn).owner == owner
    assert visible(conn, OWNER, "saved") == CARRIED
    assert visible(conn, OWNER, "all") == CARRIED
    assert visible(conn, OWNER, "original") == set()
    # The second account (it uses Shijhon already) and a later one get nothing of it.
    assert visible(conn, SECOND, "saved") == {"p1"}
    assert visible(conn, SECOND, "all") == {"p1"}
    assert visible(conn, LATER, "saved") == visible(conn, LATER, "all") == set()
    # A hand removal in the owner's Shijhon library still wins there.
    conn.execute(
        "INSERT INTO collection_membership VALUES (?,?,'1','catalog:c2',0,1,'')",
        [OWNER, builtin_profile_id(OWNER, "shijhon")],
    )
    assert visible(conn, OWNER, "saved") == {"c1", "c3"}


def test_giving_them_again_to_the_same_account_changes_nothing() -> None:
    conn = upgraded()
    carryover.give(conn, OWNER, "owner", now=100.0)
    dump = list(conn.iterdump())
    again = carryover.give(conn, OWNER, "renamed", replace=True, now=200.0)
    assert again.changed is False and again.previous is None
    assert again.carried.owner == carryover.Owner(OWNER, "owner", 100.0)
    assert list(conn.iterdump()) == dump
    assert not conn.in_transaction


def test_another_account_gets_them_only_when_told_to_replace() -> None:
    conn = upgraded()
    carryover.give(conn, OWNER, "owner", now=100.0)
    dump = list(conn.iterdump())
    with pytest.raises(carryover.GivenAlready) as refused:
        carryover.give(conn, SECOND, "second", now=200.0)
    assert refused.value.owner == carryover.Owner(OWNER, "owner", 100.0)
    assert list(conn.iterdump()) == dump and not conn.in_transaction
    assert visible(conn, SECOND, "saved") == {"p1"}
    # Replaced: the second account has them, the first one no longer.
    moved = carryover.give(conn, SECOND, "second", replace=True, now=300.0)
    assert moved.changed and moved.previous == carryover.Owner(OWNER, "owner", 100.0)
    assert visible(conn, SECOND, "saved") == CARRIED | {"p1"}
    assert visible(conn, OWNER, "saved") == set()
    rows = conn.execute("SELECT owner_id FROM collection_carryover_owner").fetchall()
    assert rows == [(SECOND,)]  # one account at a time


def test_the_count_leaves_out_albums_taken_out_since() -> None:
    conn = upgraded()
    conn.execute("DELETE FROM releases WHERE ref='rel-old'")  # (the cleanup took it out)
    assert carryover.state(conn) == carryover.CarryOver(albums=0, songs=0, owner=None)


# --- the command, where Navidrome need not answer --------------------------------------


def config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "shijhon.toml"
    path.write_text(
        f'state_dir = "{tmp_path}"\n[navidrome]\nurl = "http://127.0.0.1:9"\nuser = "svc"\n'
        f'password = "invented-service-password"\n{extra}'
    )
    return path


def test_the_command_never_upgrades_the_database_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before this Shijhon has started on it, the database is left as it is."""
    conn, _ = schema23(tmp_path / "shijhon.sqlite3")
    conn.close()
    with pytest.raises(SystemExit) as refused:
        main(["--config", str(config(tmp_path)), "carry-over", "--account", "owner"])
    assert refused.value.code == 1
    err = capsys.readouterr().err
    assert "is at schema 23, this Shijhon's is" in err and "start this Shijhon once" in err
    assert err.rstrip().endswith("nothing done")
    check = sqlite3.connect(tmp_path / "shijhon.sqlite3")
    assert check.execute("PRAGMA user_version").fetchone() == (23,)
    check.close()
    (tmp_path / "none").mkdir()
    with pytest.raises(SystemExit) as missing:
        main(["--config", str(config(tmp_path / "none")), "carry-over"])
    assert missing.value.code == 1 and "no database at" in capsys.readouterr().err


def test_the_command_shows_them_and_refuses_an_account_that_gets_real_audio_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "shijhon.sqlite3"
    conn = upgraded()
    conn.execute("VACUUM INTO ?", [str(path)])
    conn.close()
    configured = config(tmp_path, '[placeholders]\nreal_audio_accounts = ["Analyzer"]\n')
    main(["--config", str(configured), "carry-over"])
    assert capsys.readouterr().out == (
        "carried over at the upgrade: 1 album(s), 3 song(s), in no account's library"
        " (--account NAME gives them to one account's Shijhon library)\n"
    )
    # Refused before Navidrome is asked (it does not answer here).
    with pytest.raises(SystemExit) as refused:
        main(["--config", str(configured), "carry-over", "--account", "analyzer"])
    assert refused.value.code == 1
    assert capsys.readouterr().err == (
        "shijhon: analyzer gets real audio only ([placeholders] real_audio_accounts):"
        " nothing done\n"
    )
    with pytest.raises(SystemExit) as alone:
        main(["--config", str(configured), "carry-over", "--replace"])
    assert alone.value.code == 2
    assert "--replace goes with --account" in capsys.readouterr().err
    # Navidrome unreachable: nothing done, no credential said.
    with pytest.raises(SystemExit) as unreachable:
        main(["--config", str(configured), "carry-over", "--account", "owner"])
    assert unreachable.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("shijhon: Navidrome did not say who owner is")
    assert "invented-service-password" not in err
    check = sqlite3.connect(path)
    assert check.execute("SELECT * FROM collection_carryover_owner").fetchall() == []
    check.close()
