"""``shijhon matches``: the dry run's plans and the review list, read without writing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from shijhon.cli import main
from shijhon.store.db import prepare_database


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "shijhon.sqlite3"
    prepare_database(path)
    plan = {
        "owned": ["s1", "s2"],
        "release": {"title": "Harbor Lights (Deluxe)", "tracks": [{}] * 12},
        "links": {"x:1": "s1", "x:2": "s2"},
    }
    rows = [
        ("a1", "x.us", "filled", 1, "x:9", "", "", json.dumps(plan), "Harbor Lights", "Mara"),
        ("a2", "x.us", "review", 0, None, "several plausible editions", "x:3,x:4", None, "Tides",
         "Mara"),
        ("a3", "x.us", "none", 1, None, "no release has every owned track", "", None, "B", "C"),
        ("a4", "x.us", "complete", 0, "x:5", "every track is owned", "", None, "", ""),
    ]  # fmt: skip
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO album_matches (album_id, scope, outcome, planned, release_ref, reason,"
        " candidates, plan, title, artist, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        rows,
    )
    conn.execute(  # the dry run's plan: 3 owned songs, as the fill policy allows
        "UPDATE album_matches SET owned_songs = 3, release_tracks = 12 WHERE album_id = 'a1'"
    )
    conn.commit()
    conn.close()
    return path


def test_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _database(tmp_path)
    before = path.stat().st_mtime_ns
    main(["matches", "--database", str(path)])
    out = capsys.readouterr().out
    assert out.startswith("x.us: 4 album(s) checked\n")
    assert "  complete 1, for review 1\n" in out
    assert "  dry run, not acted on yet: filled 1, without a match 1; 10 placeholder(s)" in out
    assert "Would fill (dry run):\n  Mara - Harbor Lights: 10 of 12 tracks to add from x:9" in out
    assert "For review:\n  Mara - Tides: several plausible editions [candidates: x:3, x:4]" in out
    assert "Without a match" not in out  # only counted
    main(["matches", "--database", str(path), "--all"])
    everything = capsys.readouterr().out
    assert "Without a match:\n  C - B: no release has every owned track" in everything
    assert "Complete:\n  album a4: every track is owned" in everything
    assert path.stat().st_mtime_ns == before  # read only


def test_databases_it_cannot_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "empty #1.sqlite3"  # characters a file URI must quote
    sqlite3.connect(empty).execute("PRAGMA user_version = 1").connection.close()
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database" * 100)
    for path, message in (
        (empty, "no such table: album_matches"),
        (tmp_path / "missing.sqlite3", "no database at"),
        (broken, "cannot read"),
    ):
        with pytest.raises(SystemExit) as stopped:
            main(["matches", "--database", str(path)])
        assert stopped.value.code == 1
        assert message in capsys.readouterr().err


def test_what_would_be_filled_is_what_the_fill_policy_allows_now(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """As the Library page counts it: albums kept to fill on first use that the policy
    allows now would be filled too (not one whose fill the cleanup took out), and a plan it
    no longer allows would not."""
    path = _database(tmp_path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO album_matches (album_id, scope, outcome, planned, release_ref, title,"
        " artist, owned_songs, release_tracks, cleaned_at, checked_at)"
        " VALUES (?, 'x.us', 'deferred', 0, 'x:7', ?, 'Mara', ?, 12, ?, 1)",
        [
            ("a5", "Allowed Now", 4, None),
            ("a6", "Below", 1, None),
            ("a7", "Cleaned", 5, 1.0),
            ("a8", "Filled Elsewhere", 6, None),
        ],
    )
    conn.execute(  # a8 was filled under another catalog: filled, whatever its match
        "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist, album_tags,"
        " data, created_at) VALUES ('y:1', 'f', 'a8', 'a8', 'T', 'A', '{}', '{}', 0)"
    )
    conn.commit()
    conn.close()
    main(["matches", "--database", str(path), "--all"])
    out = capsys.readouterr().out
    assert "an automatic fill would fill 2 album(s) now (the fill policy: 3 songs or 25 %)" in out
    earlier = out.split("Would fill (matched earlier; the fill policy allows it now):\n")[1]
    assert [line.split(":")[0] for line in earlier.split("\n\n")[0].splitlines()] == [
        "  Mara - Allowed Now"
    ]
    first_use = out.split("To fill on first use:\n")[1].split("\n\n")[0]
    assert "Below" in first_use and "Cleaned" in first_use and "Allowed Now" not in first_use
    filled = out.split("Filled:\n")[1].split("\n\n")[0]
    assert "Filled Elsewhere" in filled and "Filled Elsewhere" not in first_use
    config = tmp_path / "strict.toml"
    config.write_text("[fill]\nauto_min_songs = 5\nauto_min_share = 0.5\n")
    main(["--config", str(config), "matches", "--database", str(path), "--all"])
    strict = capsys.readouterr().out
    assert "Would fill (dry run)" not in strict and "matched earlier" not in strict
    assert "Harbor Lights" in strict.split("To fill on first use:\n")[1]
