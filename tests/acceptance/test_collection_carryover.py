"""``shijhon carry-over``: the catalog albums carried over at the upgrade to schema 24 go to
the one account the owner names, as Navidrome knows it (its immutable user ID, the name
compared without case) - also while Shijhon runs on the database. An unknown account, one
that gets real audio only and a second account once they were given are refused; the same
account again changes nothing. (What the libraries hold then:
``tests/unit/test_collection_carryover.py``.)"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from shijhon.app import create_app
from shijhon.cli import main
from shijhon.config import load_settings
from shijhon.store.db import prepare_database
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance
from tests.harness.running import running

PASSWORD = "invented-carryover-password"


@pytest.fixture
def quiet_logging() -> Iterator[None]:
    """The command sets up its own logging: put back for the tests after this one."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def seeded(state: Path) -> Path:
    """Shijhon's database at this schema, with one carried-over catalog album of two songs."""
    database = state / "shijhon.sqlite3"
    prepare_database(database)
    conn = sqlite3.connect(database)
    with conn:
        conn.execute(
            "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
            " VALUES ('test:carried','_shijhon/carried','al-carried','Carried Record',"
            "'Invented Band','{}','{}',0)"
        )
        for n in (1, 2):
            conn.execute(
                "INSERT INTO track_links VALUES (?,?,'test:carried',0)", [f"t{n}", f"s{n}"]
            )
        conn.execute("INSERT INTO collection_carryover VALUES ('test:carried')")
    conn.close()
    return database


def owner_row(database: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(database)
    try:
        return conn.execute("SELECT owner_id,username FROM collection_carryover_owner").fetchall()
    finally:
        conn.close()


def test_the_carried_over_albums_go_to_the_account_navidrome_names(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    quiet_logging: None,
) -> None:
    owner = navidrome.create_user("owner", PASSWORD)
    second = navidrome.create_user("second", PASSWORD)
    navidrome.create_user("analyzer", PASSWORD)
    state = tmp_path / "state"
    database = seeded(state)
    config = tmp_path / "shijhon.toml"
    config.write_text(
        f'state_dir = "{state}"\n'
        f'[navidrome]\nurl = "{navidrome.base_url}"\nuser = "{ADMIN_USER}"\n'
        f'password = "{ADMIN_PASSWORD}"\nlibrary_path = "{navidrome.music}"\n'
        '[placeholders]\nreal_audio_accounts = ["analyzer"]\n'
    )

    # Another library than the one the accounts have (Navidrome gives each at least one).
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text(
        config.read_text().replace("[placeholders]", "library_id = 2\n[placeholders]")
    )

    def command(*args: str, configured: Path = config) -> tuple[int, str, str]:
        try:
            main(["--config", str(configured), "carry-over", *args])
            code = 0
        except SystemExit as exc:
            code = int(exc.code or 0)
        out, err = capsys.readouterr()
        assert PASSWORD not in out + err and ADMIN_PASSWORD not in out + err
        return code, out, err

    app = create_app(load_settings(config))
    with running(app) as server:  # Shijhon runs on the database meanwhile
        assert command() == (
            0,
            "carried over at the upgrade: 1 album(s), 2 song(s), in no account's library"
            " (--account NAME gives them to one account's Shijhon library)\n",
            "",
        )
        assert command("--account", "nobody") == (
            1,
            "",
            "shijhon: Navidrome has no account named nobody: nothing done\n",
        )
        code, _, err = command("--account", "Analyzer")
        assert code == 1 and "gets real audio only" in err
        assert command("--account", "owner", configured=elsewhere) == (
            1,
            "",
            "shijhon: owner has no access to the library Shijhon serves ([navidrome]"
            " library_id 2): nothing done\n",
        )
        assert owner_row(database) == []
        # The name as Navidrome compares it; the account by its ID.
        code, out, err = command("--account", "OWNER")
        assert code == 0 and "carry-over: 1 album(s) given to the Shijhon library" in err
        assert out == (
            f"given: 1 album(s), 2 song(s) to owner (user ID {owner}), in its Shijhon library"
            " and All now\n"
        )
        assert owner_row(database) == [(owner, "owner")]
        store = app.services.store
        seen = server.call(
            lambda: store.fetchone("SELECT owner_id FROM collection_carryover_owner")
        )
        assert seen is not None and seen["owner_id"] == owner  # the service reads it as it is
        code, out, _ = command("--account", "owner")
        assert code == 0 and out.startswith(f"already given to owner (user ID {owner}) on ")
        assert out.endswith(": 1 album(s), 2 song(s); nothing changed\n")
        # A second account: refused, unless told to replace.
        code, out, err = command("--account", "second")
        assert code == 1 and out == ""
        assert err.startswith(
            f"shijhon: the carried-over albums were given to owner (user ID {owner}) on "
        )
        assert err.endswith("; --replace gives them to second instead: nothing done\n")
        assert owner_row(database) == [(owner, "owner")]
        code, out, _ = command("--account", "second", "--replace")
        assert code == 0 and f"to second (user ID {second})" in out
        assert f"; taken from owner (user ID {owner}) on " in out
        assert owner_row(database) == [(second, "second")]
        code, out, _ = command("--account", "owner", "--replace")
        assert code == 0 and owner_row(database) == [(owner, "owner")]
        code, out, _ = command()
        assert code == 0 and f", given to owner (user ID {owner}) on " in out
