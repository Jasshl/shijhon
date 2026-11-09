"""``navidrome-accounts`` with answers no real Navidrome gives on cue: refused logins for a
moment, answers that are no JSON or not the expected shape. They are tried again until
the wait ends and then said as such - never a traceback, never "another account". And a
Navidrome that has accounts, whose admin creates the service account: what stops that is
said at once, never tried again."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from shijhon.navidrome import accounts

Handler = Callable[[httpx.Request], httpx.Response]


def served(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[str]:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return handler(request)

    def client(socket: Path | None, **kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(accounts.connect, "client", client)
    monkeypatch.setattr(accounts.time, "sleep", lambda _: None)
    return seen


def ensure(tmp_path: Path, **kwargs: Any) -> None:
    own = tmp_path / "own"
    own.write_text("an invented password")
    accounts.ensure(
        "http://navidrome",
        "shijhon",
        tmp_path / "navidrome-password",
        account="alex",
        account_password_file=own,
        wait=kwargs.pop("wait", 0.2),
        say=lambda _: None,
        **kwargs,
    )


def test_refused_logins_are_tried_again_and_not_taken_for_another_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200)
        return httpx.Response(429, text="Too Many Requests")

    seen = served(monkeypatch, handler)
    with pytest.raises(accounts.Unsettled, match="too many attempts"):
        ensure(tmp_path)
    assert "POST /auth/createAdmin" not in seen
    assert seen.count("POST /auth/login") >= 2  # (again, until the wait ended)


@pytest.mark.parametrize(
    "listed",
    [
        httpx.Response(200, text="<html>not JSON</html>"),
        httpx.Response(200, json={"not": "a list"}),
        httpx.Response(200, content=b"\xff\xfe"),
    ],
)
def test_answers_that_cannot_be_read_are_said(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, listed: httpx.Response
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200)
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "t"})
        return listed

    served(monkeypatch, handler)
    with pytest.raises(accounts.Unsettled, match="could not be read"):
        ensure(tmp_path)


def test_a_login_answer_that_is_no_json_is_said(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200)
        return httpx.Response(200, text="<html>a proxy's page</html>")

    served(monkeypatch, handler)
    with pytest.raises(accounts.Unsettled, match="could not be read"):
        ensure(tmp_path)


def test_list_items_that_are_no_accounts_are_passed_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200)
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "t"})
        if request.method == "GET":
            return httpx.Response(200, json=["junk", None, {"userName": "shijhon"}])
        return httpx.Response(200, json={"id": "1"})

    seen = served(monkeypatch, handler)
    ensure(tmp_path)
    assert seen[-1] == "POST /api/user"  # (alex was not there: made)


def test_a_password_made_by_an_earlier_try_goes_when_it_fits_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    logins = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200)
        if request.url.path == "/auth/login":
            logins["n"] += 1
            return httpx.Response(503 if logins["n"] == 1 else 401)
        if request.url.path == "/auth/createAdmin":
            return httpx.Response(403)  # (someone else's accounts)
        raise AssertionError(request.url)

    served(monkeypatch, handler)
    with pytest.raises(accounts.AccountsError, match="already has accounts"):
        ensure(tmp_path, wait=30)
    assert not (tmp_path / "navidrome-password").exists()


def test_a_moment_s_trouble_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = {"login": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200)
        if request.url.path == "/auth/login":
            calls["login"] += 1
            if calls["login"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"token": "t"})
        if request.method == "GET":
            return httpx.Response(200, json=[{"userName": "shijhon"}, {"userName": "Alex"}])
        raise AssertionError(request.url)

    served(monkeypatch, handler)
    ensure(tmp_path, wait=30)
    assert calls["login"] == 2


def test_messages_name_what_the_service_calls_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200)
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(200, json=[{"userName": "shijhon"}])

    served(monkeypatch, handler)
    names = accounts.Names(
        password_file="/var/lib/shijhon/navidrome-password",
        account="services.shijhon.account",
        account_password="services.shijhon.account.passwordFile",
    )
    with pytest.raises(accounts.AccountsError, match=r"set services\.shijhon\.account$"):
        accounts.ensure(
            "http://navidrome", "shijhon", tmp_path / "p", wait=1, say=print, names=names
        )
    with pytest.raises(accounts.AccountsError, match=r"services\.shijhon\.account\.passwordFile"):
        accounts.ensure(
            "http://navidrome",
            "shijhon",
            tmp_path / "p",
            account="alex",
            wait=1,
            say=print,
            names=names,
        )


class Existing:
    """A Navidrome that has accounts: ``{name: (password, admin)}``."""

    def __init__(self, users: dict[str, tuple[str, bool]]) -> None:
        self.users = dict(users)
        self.created: list[dict[str, Any]] = []
        self.logins: list[str] = []
        self.fail_creation = 0  # (answers 503 to so many creations first)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ping":
            return httpx.Response(200)
        if path == "/auth/createAdmin":
            return httpx.Response(403)
        if path == "/auth/login":
            body = json.loads(request.content)
            self.logins.append(body["username"])
            known = self.users.get(body["username"])
            if known is None or known[0] != body["password"]:
                return httpx.Response(401)
            return httpx.Response(200, json={"token": body["username"], "isAdmin": known[1]})
        signed_in = request.headers.get("X-ND-Authorization", "").removeprefix("Bearer ")
        if not self.users.get(signed_in, ("", False))[1]:
            return httpx.Response(403)
        if request.method == "GET":
            return httpx.Response(200, json=[{"userName": name} for name in self.users])
        body = json.loads(request.content)
        if self.fail_creation:
            self.fail_creation -= 1
            return httpx.Response(503)
        if body["userName"] in self.users:
            return httpx.Response(400, text='{"errors":{"userName":"ra.validation.unique"}}')
        self.created.append(body)
        self.users[body["userName"]] = (body["password"], body["isAdmin"])
        return httpx.Response(200, json={"id": "1"})


def test_an_admin_account_navidrome_has_creates_the_service_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    navidrome = Existing({"alex": ("an invented password", True), "robin": ("other", False)})
    served(monkeypatch, navidrome)
    ensure(tmp_path)
    service_password = (tmp_path / "navidrome-password").read_text().strip()
    assert navidrome.created == [
        {"userName": "shijhon", "name": "shijhon", "password": service_password, "isAdmin": True}
    ]
    assert navidrome.users["alex"] == ("an invented password", True)  # (untouched)

    # Again: the service account signs in; the person's password is not needed.
    navidrome.logins.clear()
    (tmp_path / "own").unlink()
    accounts.ensure(
        "http://navidrome",
        "shijhon",
        tmp_path / "navidrome-password",
        account="alex",
        wait=0.2,
        say=lambda _: None,
    )
    assert navidrome.logins == ["shijhon"]
    assert (tmp_path / "navidrome-password").read_text().strip() == service_password


@pytest.mark.parametrize(
    ("users", "says"),
    [
        ({"alex": ("another password", True)}, "does not sign in with the password"),
        ({"robin": ("an invented password", True)}, "does not sign in with the password"),
        ({"alex": ("an invented password", False)}, '"alex" is not an admin there'),
        (
            {"alex": ("an invented password", True), "shijhon": ("not ours", True)},
            'has the account "shijhon", and the password in',
        ),
    ],
)
def test_what_keeps_the_service_account_from_being_created_is_said_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    users: dict[str, tuple[str, bool]],
    says: str,
) -> None:
    navidrome = Existing(users)
    served(monkeypatch, navidrome)
    with pytest.raises(accounts.AccountsError, match=says) as stopped:
        ensure(tmp_path, wait=30)
    assert not isinstance(stopped.value, accounts.Unsettled)  # (not tried again)
    assert navidrome.logins.count("alex") == 1
    assert navidrome.created == []
    assert navidrome.users == users
    assert not (tmp_path / "navidrome-password").exists()  # (the password made fits nothing)


def test_without_a_person_s_account_the_options_are_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    navidrome = Existing({"alex": ("an invented password", True)})
    served(monkeypatch, navidrome)
    names = accounts.Names(
        account="services.shijhon.account",
        account_password="services.shijhon.account.passwordFile",
    )
    for account in (None, "alex"):  # (the second without its password file)
        with pytest.raises(accounts.AccountsError) as stopped:
            accounts.ensure(
                "http://navidrome",
                "shijhon",
                tmp_path / "p",
                account=account,
                wait=1,
                say=lambda _: None,
                names=names,
            )
        assert "Set services.shijhon.account to an admin account" in str(stopped.value)
        assert "services.shijhon.account.passwordFile" in str(stopped.value)
        assert not (tmp_path / "p").exists()
    assert navidrome.logins == ["shijhon", "shijhon"] * 2
    assert navidrome.created == []


def test_a_password_file_the_person_wrote_stays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "navidrome-password").write_text("written by the person\n")
    served(monkeypatch, Existing({"alex": ("another password", True)}))
    with pytest.raises(accounts.AccountsError, match="does not sign in"):
        ensure(tmp_path)
    assert (tmp_path / "navidrome-password").read_text() == "written by the person\n"


def test_a_moment_s_trouble_creating_it_keeps_the_password_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    navidrome = Existing({"alex": ("an invented password", True)})
    navidrome.fail_creation = 1
    served(monkeypatch, navidrome)
    ensure(tmp_path, wait=30)
    service_password = (tmp_path / "navidrome-password").read_text().strip()
    assert navidrome.users["shijhon"] == (service_password, True)
    assert len(navidrome.created) == 1


def test_a_service_account_that_is_no_admin_is_said_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "navidrome-password").write_text("ours\n")
    navidrome = Existing({"shijhon": ("ours", False), "alex": ("an invented password", True)})
    served(monkeypatch, navidrome)
    with pytest.raises(accounts.AccountsError, match="must be an admin") as stopped:
        ensure(tmp_path, wait=30)
    assert not isinstance(stopped.value, accounts.Unsettled)  # (not tried again)
    assert "Repairing the service account" in str(stopped.value)
    assert navidrome.logins == ["shijhon"]
    assert (tmp_path / "navidrome-password").read_text() == "ours\n"
