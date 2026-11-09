"""Navidrome's accounts for Shijhon (``shijhon navidrome-accounts``, which the NixOS
service runs before Shijhon starts): Shijhon's service account and a person's own, as
``setup.sh`` makes them in a new installation. On a Navidrome that has accounts already,
the service account is created by signing in with the person's own, an admin. An account
Navidrome has already is kept, with the password it has. Passwords are read from files
and sent in request bodies only; nothing here prints one.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from shijhon.navidrome import connect

# Where a person reads how to repair the service account with Navidrome's own command.
REPAIR = (
    "with Navidrome's command \"navidrome user edit\", as the NixOS guide's"
    ' "Repairing the service account" shows'
)


class AccountsError(RuntimeError):
    """Why the accounts could not be set up, in words for the person: a lasting reason,
    which starting again does not change."""


class Unsettled(AccountsError):
    """Navidrome did not answer as it should just now (it is starting, it refuses logins
    for a moment, an answer could not be read): tried again until the wait ends."""


@dataclass(frozen=True)
class Names:
    """How the messages name what the person sets: the command line's words by default;
    a service that sets them otherwise names its own (the NixOS service: its options and
    the password file's path outside its sandbox)."""

    password_file: str | None = None  # default: the path given
    account: str = "--account"
    account_password: str = "--account-password-file"  # noqa: S105 - an option's name


def ensure(
    url: str,
    user: str,
    password_file: Path,
    *,
    socket: Path | None = None,
    account: str | None = None,
    account_password_file: Path | None = None,
    wait: float = 120.0,
    say: Callable[[str], None] = print,
    names: Names | None = None,
) -> None:
    """Shijhon's admin account ``user`` exists with the password in ``password_file`` (a
    random one, written there first when the file is missing), and so does ``account``,
    an admin account with the password in ``account_password_file``. A Navidrome without
    accounts gets ``user`` as its first; one with accounts gets it from ``account``, which
    must be an admin there. Navidrome at ``url``, or through ``socket``. What goes wrong
    for a moment is tried again for ``wait`` seconds; each request may take 10 more.
    Raises :class:`AccountsError`."""
    names = names or Names()
    deadline = time.monotonic() + wait
    made = [False]  # (a password written by an earlier try is this run's too)
    with connect.client(socket, base_url=url.rstrip("/"), timeout=10) as http:
        while True:
            try:
                _wait(http, deadline, wait)
                _ensure(http, user, password_file, account, account_password_file, say, names, made)
                return
            except httpx.HTTPError:
                problem: AccountsError = Unsettled(
                    f"Navidrome does not answer as it should after {wait:g} seconds"
                )
            except Unsettled as exc:
                problem = exc
            if time.monotonic() > deadline:
                raise problem
            time.sleep(2)


def _ensure(
    http: httpx.Client,
    user: str,
    password_file: Path,
    account: str | None,
    account_password_file: Path | None,
    say: Callable[[str], None],
    names: Names,
    made: list[bool],
) -> None:
    shown = names.password_file or str(password_file)
    if _new_password(password_file):
        made[0] = True
        say(f"a password for Shijhon's account written to {shown}")
    password = _read(password_file, f"the service account's password file ({shown})")
    token = _login(http, user, password)
    if token is None:
        created = http.post("/auth/createAdmin", json={"username": user, "password": password})
        if created.status_code == 200:
            token = _token(created)
            say(f'Navidrome\'s account "{user}" created for Shijhon')
        elif created.status_code == 403:
            # Navidrome has accounts. If this one is among them now, another run made it
            # meanwhile; otherwise the person's admin account creates it.
            token = _login(http, user, password)
            if token is None:
                try:
                    token = _through(
                        http, user, password, account, account_password_file, shown, names
                    )
                except Unsettled:
                    raise  # (tried again with the same password)
                except AccountsError:
                    if made[0]:  # (a password made in this run fits nothing)
                        password_file.unlink(missing_ok=True)
                    raise
                say(
                    f'Navidrome\'s account "{user}" created for Shijhon, an admin, by your'
                    f' account "{account}"'
                )
        else:
            raise Unsettled(
                f"Navidrome answered {created.status_code} when its first account was created"
            )
    headers = {"X-ND-Authorization": f"Bearer {token}"}
    listed = http.get("/api/user", headers=headers)
    if listed.status_code in (401, 403):
        raise AccountsError(
            f"Navidrome's account \"{user}\" signs in but may not list the accounts: Shijhon's"
            f" service account must be an admin. Make it one again ({REPAIR}), then start"
            " again."
        )
    if listed.status_code != 200:
        raise Unsettled(f"Navidrome answered {listed.status_code} when asked for its accounts")
    users = _json(listed)
    if not isinstance(users, list):
        raise Unsettled("Navidrome's list of accounts could not be read")
    found = {str(u.get("userName", "")).casefold() for u in users if isinstance(u, dict)}
    if account is None:
        if not found - {user.casefold()}:
            raise AccountsError(f"Navidrome has no account for a person yet: set {names.account}")
        return
    if account.casefold() in found:
        say(f'Navidrome has the account "{account}": kept, with the password it has')
        return
    if account_password_file is None:
        raise AccountsError(f'creating the account "{account}" needs {names.account_password}')
    own = _read(
        account_password_file,
        f'the password file of "{account}" ({names.account_password})',
        strip=False,
    )
    body = {"userName": account, "name": account, "password": own, "isAdmin": True}
    made_own = http.post("/api/user", headers=headers, json=body)
    if made_own.status_code == 200:
        say(f'your account "{account}" created in Navidrome (an admin)')
    elif made_own.status_code == 400 and "validation.unique" in made_own.text:
        say(f'Navidrome has the account "{account}": kept, with the password it has')
    else:
        raise Unsettled(
            f'Navidrome answered {made_own.status_code} when the account "{account}" was created'
        )


def _through(
    http: httpx.Client,
    user: str,
    password: str,
    account: str | None,
    account_password_file: Path | None,
    shown: str,
    names: Names,
) -> str:
    """Create Shijhon's account ``user`` in a Navidrome that has accounts, signed in as the
    person's own admin account: that account's token. Raises :class:`AccountsError` with
    what to change when that cannot be done."""
    has = f'Navidrome already has accounts, and "{user}" with the password in {shown} is not'
    either = (
        f'Or create "{user}" in Navidrome as an admin and put its password in {shown}'
        " (readable by Shijhon only). Then start again."
    )
    if account is None or account_password_file is None:
        raise AccountsError(
            f"{has} one of them. Set {names.account} to an admin account Navidrome has, with"
            f" its password in {names.account_password}: Shijhon signs in with it to create"
            f' "{user}". {either}'
        )
    own = _read(
        account_password_file,
        f'the password file of "{account}" ({names.account_password})',
        strip=False,
    )
    answer = http.post("/auth/login", json={"username": account, "password": own})
    if answer.status_code == 429:
        raise Unsettled("Navidrome refuses logins for a moment (too many attempts)")
    if answer.status_code in (401, 403):
        raise AccountsError(
            f'{has} one of them, and "{account}" does not sign in with the password in'
            f" {names.account_password} (no such account, or another password). Set"
            f" {names.account} to an admin account Navidrome has, with its password in"
            f" {names.account_password}. {either}"
        )
    if answer.status_code != 200:
        raise Unsettled(f"Navidrome answered {answer.status_code} at its login")
    body = _json(answer)
    if not isinstance(body, dict) or body.get("isAdmin") is not True:
        if isinstance(body, dict) and body.get("isAdmin") is False:
            raise AccountsError(
                f'{has} one of them, and "{account}" is not an admin there: only an admin'
                f" can create it. Set {names.account} to an admin account Navidrome has,"
                f" with its password in {names.account_password}. {either}"
            )
        raise Unsettled("Navidrome's answer could not be read")
    token = _token(answer)
    headers = {"X-ND-Authorization": f"Bearer {token}"}
    service = {"userName": user, "name": user, "password": password, "isAdmin": True}
    created = http.post("/api/user", headers=headers, json=service)
    if created.status_code == 400 and "validation.unique" in created.text:
        raise AccountsError(
            f'Navidrome has the account "{user}", and the password in {shown} does not sign'
            f" in to it. Put that account's password in {shown} (readable by Shijhon only),"
            f" or give the account a new one ({REPAIR}), then start again."
        )
    if created.status_code != 200:
        raise Unsettled(
            f'Navidrome answered {created.status_code} when the account "{user}" was created'
        )
    return token


def _wait(http: httpx.Client, deadline: float, wait: float) -> None:
    while True:
        try:
            if http.get("/ping", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise Unsettled(f"Navidrome does not answer after {wait:g} seconds")
        time.sleep(1)


def _new_password(path: Path) -> bool:
    """Write a random password to ``path`` (mode 0600) unless it is there: whether it was
    written. Whole or not at all, and never over a file another run wrote meanwhile."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_hex(24) + "\n")
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(staged, path)
        except FileExistsError:
            return False
    finally:
        with contextlib.suppress(OSError):
            staged.unlink()
    return True


def _read(path: Path, what: str, *, strip: bool = True) -> str:
    try:
        text = path.read_text()
    except OSError as exc:
        raise AccountsError(
            f"{what} cannot be read ({exc.strerror or type(exc).__name__})"
        ) from None
    # Shijhon reads its own password stripped ([navidrome] password_file); a person's
    # keeps its spaces, without the line's end.
    text = text.strip() if strip else text.rstrip("\r\n")
    if not text:
        raise AccountsError(f"{what} is empty")
    return text


def _login(http: httpx.Client, user: str, password: str) -> str | None:
    """The account's token; None when Navidrome refuses these credentials."""
    answer = http.post("/auth/login", json={"username": user, "password": password})
    if answer.status_code == 200:
        return _token(answer)
    if answer.status_code == 429:
        raise Unsettled("Navidrome refuses logins for a moment (too many attempts)")
    if answer.status_code in (401, 403):
        return None
    raise Unsettled(f"Navidrome answered {answer.status_code} at its login")


def _json(answer: httpx.Response) -> Any:
    try:
        return answer.json()
    except ValueError:  # (not JSON, or not text)
        raise Unsettled("Navidrome's answer could not be read") from None


def _token(answer: httpx.Response) -> str:
    body = _json(answer)
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise Unsettled("Navidrome's answer could not be read")
    return token
