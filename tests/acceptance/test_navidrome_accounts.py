"""``shijhon navidrome-accounts`` against a real, new Navidrome: Shijhon's service account
with a random password it writes first, and a person's own account, both admins; a second
run keeps both, with their passwords. A Navidrome that has accounts without Shijhon's gets
it from the person's admin account there; without one that signs in as an admin it is
refused at once, and the password made for it removed. So is a service account Navidrome
has with another password, or that is no admin: both are left as they are."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from shijhon import cli
from shijhon.navidrome import accounts
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

OWN = "alex"
OWN_PASSWORD = "an invented password "  # (its trailing space is part of it)


@pytest.fixture
def fresh(tmp_path: Path) -> Iterator[NavidromeInstance]:
    """A Navidrome without any account."""
    instance = NavidromeInstance(tmp_path / "navidrome")
    try:
        instance.start()
        yield instance
    finally:
        instance.stop()


def signs_in(nd: NavidromeInstance, user: str, password: str) -> bool:
    answer = httpx.post(
        f"{nd.base_url}/auth/login", json={"username": user, "password": password}, timeout=10
    )
    return answer.status_code == 200 and bool(answer.json().get("isAdmin"))


def test_new_navidrome_gets_both_accounts_and_keeps_them(
    fresh: NavidromeInstance, tmp_path: Path
) -> None:
    password_file = tmp_path / "state" / "navidrome-password"
    own_file = tmp_path / "own-password"
    own_file.write_text(OWN_PASSWORD + "\n")
    said: list[str] = []
    accounts.ensure(
        fresh.base_url,
        "shijhon",
        password_file,
        account=OWN,
        account_password_file=own_file,
        wait=10,
        say=said.append,
    )
    service_password = password_file.read_text().strip()
    assert len(service_password) == 48
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert signs_in(fresh, "shijhon", service_password)
    assert signs_in(fresh, OWN, OWN_PASSWORD)
    assert not any(service_password in line or OWN_PASSWORD in line for line in said)
    assert [p.name for p in password_file.parent.iterdir()] == ["navidrome-password"]

    # Again, with the person's password changed and its file gone: nothing changes.
    own_file.unlink()
    said.clear()
    accounts.ensure(
        fresh.base_url,
        "shijhon",
        password_file,
        account=OWN.upper(),
        account_password_file=own_file,
        wait=10,
        say=said.append,
    )
    assert password_file.read_text().strip() == service_password
    assert signs_in(fresh, OWN, OWN_PASSWORD)
    assert said == [f'Navidrome has the account "{OWN.upper()}": kept, with the password it has']


def test_cli_reads_the_configuration(fresh: NavidromeInstance, tmp_path: Path) -> None:
    password_file = tmp_path / "navidrome-password"
    own_file = tmp_path / "own-password"
    own_file.write_text(OWN_PASSWORD + "\n")
    config = tmp_path / "shijhon.toml"
    config.write_text(
        f'state_dir = "{tmp_path / "state"}"\n[navidrome]\nurl = "{fresh.base_url}"\n'
        f'user = "shijhon"\npassword_file = "{password_file}"\n'
    )
    argv = ["--config", str(config), "navidrome-accounts", "--wait", "10"]
    cli.main([*argv, "--account", OWN, "--account-password-file", str(own_file)])
    assert signs_in(fresh, "shijhon", password_file.read_text().strip())
    assert signs_in(fresh, OWN, OWN_PASSWORD)
    cli.main(argv)  # (a person's account is there: none needs naming)


def test_navidrome_with_other_accounts_is_refused(fresh: NavidromeInstance, tmp_path: Path) -> None:
    fresh.create_admin()  # someone else's first account
    password_file = tmp_path / "navidrome-password"
    with pytest.raises(accounts.AccountsError, match="already has accounts"):
        accounts.ensure(fresh.base_url, "shijhon", password_file, account=OWN, wait=10)
    assert not password_file.exists()  # (the password made for it fits nothing)


def test_an_existing_admin_account_creates_the_service_account(
    fresh: NavidromeInstance, tmp_path: Path
) -> None:
    fresh.create_admin()  # the person's own, and their library's other accounts
    fresh.create_user("robin", "robin's invented password")
    password_file = tmp_path / "state" / "navidrome-password"
    own_file = tmp_path / "own-password"
    own_file.write_text(ADMIN_PASSWORD + "\n")
    said: list[str] = []
    accounts.ensure(
        fresh.base_url,
        "shijhon",
        password_file,
        account=ADMIN_USER,
        account_password_file=own_file,
        wait=10,
        say=said.append,
    )
    service_password = password_file.read_text().strip()
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert signs_in(fresh, "shijhon", service_password)
    assert signs_in(fresh, ADMIN_USER, ADMIN_PASSWORD)
    assert fresh.login("robin", "robin's invented password")["isAdmin"] is False
    assert f'by your account "{ADMIN_USER}"' in said[1]
    assert not any(service_password in line or ADMIN_PASSWORD in line for line in said)

    # Again, with the person's password file gone: the service account signs in.
    own_file.unlink()
    said.clear()
    accounts.ensure(
        fresh.base_url,
        "shijhon",
        password_file,
        account=ADMIN_USER,
        account_password_file=own_file,
        wait=10,
        say=said.append,
    )
    assert password_file.read_text().strip() == service_password
    assert said == [f'Navidrome has the account "{ADMIN_USER}": kept, with the password it has']


@pytest.mark.parametrize(
    ("account", "password", "says"),
    [
        ("robin", "robin's invented password", '"robin" is not an admin there'),
        (ADMIN_USER, "not the admin's password", "does not sign in with the password"),
    ],
)
def test_an_account_that_cannot_create_it_stops_the_command(
    fresh: NavidromeInstance,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    account: str,
    password: str,
    says: str,
) -> None:
    fresh.create_admin()
    fresh.create_user("robin", "robin's invented password")
    password_file = tmp_path / "navidrome-password"
    own_file = tmp_path / "own-password"
    own_file.write_text(password + "\n")
    config = tmp_path / "shijhon.toml"
    config.write_text(
        f'state_dir = "{tmp_path / "state"}"\n[navidrome]\nurl = "{fresh.base_url}"\n'
        f'user = "shijhon"\npassword_file = "{password_file}"\n'
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main(
            [
                *("--config", str(config), "navidrome-accounts", "--wait", "10"),
                *("--account", account, "--account-password-file", str(own_file)),
            ]
        )
    assert stopped.value.code == 2  # (what needs changing first: not tried again)
    message = capsys.readouterr().err
    assert says in message
    assert "--account to an admin account Navidrome has" in message
    assert password not in message
    assert not password_file.exists()
    assert signs_in(fresh, ADMIN_USER, ADMIN_PASSWORD)  # (nothing changed, no lockout)


def test_a_person_s_account_is_needed(fresh: NavidromeInstance, tmp_path: Path) -> None:
    password_file = tmp_path / "navidrome-password"
    with pytest.raises(accounts.AccountsError, match="no account for a person"):
        accounts.ensure(fresh.base_url, "shijhon", password_file, wait=10)
    # The service account was made; with a person's account named, the next run goes on.
    own_file = tmp_path / "own-password"
    own_file.write_text(OWN_PASSWORD)
    accounts.ensure(
        fresh.base_url,
        "shijhon",
        password_file,
        account=OWN,
        account_password_file=own_file,
        wait=10,
        say=lambda _: None,
    )
    assert signs_in(fresh, OWN, OWN_PASSWORD)


def test_missing_navidrome_is_said(tmp_path: Path) -> None:
    with pytest.raises(accounts.AccountsError, match="does not answer"):
        accounts.ensure("http://127.0.0.1:9", "shijhon", tmp_path / "p", wait=1)


def test_a_service_account_with_another_password_stops_the_command(
    fresh: NavidromeInstance, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh.create_admin()
    service = {"userName": "shijhon", "name": "shijhon", "isAdmin": True}
    fresh.native("POST", "user", json={**service, "password": "a password nobody has"})
    password_file = tmp_path / "navidrome-password"
    own_file = tmp_path / "own-password"
    own_file.write_text(ADMIN_PASSWORD + "\n")
    config = tmp_path / "shijhon.toml"
    config.write_text(
        f'state_dir = "{tmp_path / "state"}"\n[navidrome]\nurl = "{fresh.base_url}"\n'
        f'user = "shijhon"\npassword_file = "{password_file}"\n'
    )
    argv = ["--config", str(config), "navidrome-accounts", "--wait", "10"]
    with pytest.raises(SystemExit) as stopped:
        cli.main([*argv, "--account", ADMIN_USER, "--account-password-file", str(own_file)])
    assert stopped.value.code == 2
    message = capsys.readouterr().err
    assert 'Navidrome has the account "shijhon", and the password in' in message
    assert "Repairing the service account" in message
    assert ADMIN_PASSWORD not in message
    assert not password_file.exists()  # (the password made fits nothing)
    assert signs_in(fresh, "shijhon", "a password nobody has")  # (untouched, not locked out)
    assert signs_in(fresh, ADMIN_USER, ADMIN_PASSWORD)


def test_a_service_account_that_is_no_admin_stops_the_command(
    fresh: NavidromeInstance, tmp_path: Path
) -> None:
    fresh.create_admin()
    fresh.create_user("shijhon", "the service password")  # (an admin no more)
    password_file = tmp_path / "navidrome-password"
    password_file.write_text("the service password\n")
    with pytest.raises(accounts.AccountsError, match="must be an admin") as stopped:
        accounts.ensure(fresh.base_url, "shijhon", password_file, account=ADMIN_USER, wait=10)
    assert not isinstance(stopped.value, accounts.Unsettled)
    assert password_file.read_text() == "the service password\n"
