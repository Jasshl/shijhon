"""Dashboard sign-in: Navidrome's own login; sessions and CSRF tokens.

- Sign-in posts the username and password to Navidrome's ``/auth/login`` (the web UI's
  login). Sessions bind the immutable user ID; reusable Subsonic authorization may be
  refreshed in the existing private owner record. Passwords are never stored or logged.
- A session is a random cookie; the database keeps its SHA-256 only, with the session's
  CSRF token. Every form carries that token; a POST without it is refused.
- The shared access service checks current identity, role and library grants before
  personal work. Installation routes additionally require current admin rights.
- A session ends 7 days after sign-in, or after 12 hours without use.
- Sign-in attempts are limited per client address, as the proxy names it for Navidrome
  (``proxy/forwarding.py``): the peer's, or, when the peer is one of ``[server]
  trusted_proxies``, the last address in ``X-Forwarded-For`` that is not a trusted proxy
  (earlier entries are the client's own claims). IPv6 addresses count per /64. The login
  to Navidrome carries that address too, so Navidrome's own login limit (5 in 20 s per
  address) counts each client apart once Navidrome trusts Shijhon.

Sessions and CSRF are these few lines rather than Starlette's ``SessionMiddleware``: that
keeps the session in a signed cookie, so a sign-out or a lost admin right could not end it
on the server, and it needs a signing key to manage. Forms are parsed with the standard
library (``parse_qsl``): only ``application/x-www-form-urlencoded`` is accepted, so
python-multipart would be a dependency for nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr

from shijhon.access import Principal
from shijhon.proxy.forwarding import Address
from shijhon.store import Store

SESSION_SECONDS = 7 * 86400
IDLE_SECONDS = 12 * 3600
USE_NOTE_SECONDS = 300.0  # how often a session's use is written down


@dataclass(frozen=True)
class Session:
    token_hash: str
    username: str
    csrf: str
    expires_at: float
    user_id: str = ""
    principal: Principal | None = None

    @property
    def is_admin(self) -> bool:
        return self.principal is not None and self.principal.is_admin


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


class Sessions:
    def __init__(
        self,
        store: Store,
        *,
        lifetime: float = SESSION_SECONDS,
        idle: float = IDLE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.lifetime = lifetime
        self.idle = idle
        self.clock = clock

    async def create(self, username: str, user_id: str = "") -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(
            _hash(token), username, secrets.token_urlsafe(32), now + self.lifetime, user_id
        )
        await self.store.execute(
            "DELETE FROM dashboard_sessions WHERE expires_at <= ? OR used_at <= ?",
            [now, now - self.idle],
        )
        await self.store.execute(
            "INSERT INTO dashboard_sessions"
            " (token_hash, username, csrf, created_at, expires_at, used_at, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [session.token_hash, username, session.csrf, now, session.expires_at, now, user_id],
        )
        return token, session

    async def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        row = await self.store.fetchone(
            "SELECT token_hash, username, csrf, expires_at, used_at, user_id"
            " FROM dashboard_sessions"
            " WHERE token_hash = ?",
            [_hash(token)],
        )
        if row is None:
            return None
        now = self.clock()
        if float(row["expires_at"]) <= now or float(row["used_at"]) <= now - self.idle:
            await self.end(token)
            return None
        if now - float(row["used_at"]) >= USE_NOTE_SECONDS:
            await self.store.execute(
                "UPDATE dashboard_sessions SET used_at = ? WHERE token_hash = ?",
                [now, row["token_hash"]],
            )
        return Session(
            row["token_hash"],
            row["username"],
            row["csrf"],
            float(row["expires_at"]),
            row["user_id"],
        )

    async def end(self, token: str) -> None:
        await self.store.execute(
            "DELETE FROM dashboard_sessions WHERE token_hash = ?", [_hash(token)]
        )

    async def end_user(self, username: str) -> None:
        await self.store.execute("DELETE FROM dashboard_sessions WHERE username = ?", [username])

    async def end_owner(self, user_id: str) -> None:
        await self.store.execute("DELETE FROM dashboard_sessions WHERE user_id = ?", [user_id])


@dataclass(frozen=True)
class Login:
    outcome: str  # ok | wrong | limited | unreachable
    username: str = ""
    admin: bool = False
    user_id: str = ""
    token: SecretStr = field(default_factory=lambda: SecretStr(""))
    salt: SecretStr = field(default_factory=lambda: SecretStr(""))
    native_token: SecretStr = field(default_factory=lambda: SecretStr(""), repr=False)


async def navidrome_login(
    http: httpx.AsyncClient,
    base_url: str,
    user: str,
    password: str,
    client: Address | None = None,
) -> Login:
    """Check a username and password with Navidrome's own login, for the client at
    ``client`` (its address, which Navidrome's login limit counts once it trusts Shijhon)."""
    named = {"x-forwarded-for": str(client), "x-real-ip": str(client)} if client else {}
    try:
        response = await http.post(
            f"{base_url.rstrip('/')}/auth/login",
            json={"username": user, "password": password},
            headers=named,
        )
    except httpx.HTTPError:
        return Login("unreachable")
    if response.status_code == 401:
        return Login("wrong")
    if response.status_code == 429:
        return Login("limited")
    if response.status_code != 200:
        return Login("unreachable")
    try:
        body: Any = response.json()
    except ValueError:
        return Login("unreachable")
    if not isinstance(body, dict):
        return Login("unreachable")
    name = body.get("username")
    identity = body.get("id")
    if not isinstance(name, str) or not name or not isinstance(identity, str) or not identity:
        return Login("unreachable")
    return Login(
        "ok",
        name,
        body.get("isAdmin") is True,
        identity,
        SecretStr(body.get("subsonicToken", "")),
        SecretStr(body.get("subsonicSalt", "")),
        SecretStr(
            body["token"]
            if isinstance(body.get("token"), str) and len(body["token"]) <= 16_384
            else ""
        ),
    )


def client_key(address: Address | None, peer: str) -> str:
    """Who is signing in, for the limit: the client's address (``forwarding``), IPv6 per
    /64; without one, the peer as it was given."""
    if address is None:
        return peer[:64]
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class Attempts:
    """At most ``limit`` sign-in attempts within ``window`` seconds per client. At most
    ``clients`` are told apart; past that, new ones share one count."""

    def __init__(
        self,
        limit: int = 5,
        window: float = 60.0,
        clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit, self.window, self.clients, self.clock = limit, window, clients, clock
        self._times: OrderedDict[str, deque[float]] = OrderedDict()  # least recent first

    def allow(self, client: str) -> bool:
        now = self.clock()
        while self._times:
            first = next(iter(self._times.values()))
            if first[-1] > now - self.window:
                break
            self._times.popitem(last=False)  # quiet for a whole window: forgotten
        if client not in self._times and len(self._times) >= self.clients:
            client = "(others)"
        times = self._times.setdefault(client, deque())
        self._times.move_to_end(client)
        while times and times[0] <= now - self.window:
            times.popleft()
        if len(times) >= self.limit:
            return False
        times.append(now)
        return True
