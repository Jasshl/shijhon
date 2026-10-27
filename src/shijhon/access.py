"""Current Navidrome identity and grants shared by personal access adapters.

``verify`` resolves an already authenticated username; it is not a credential check.
Durable owners must also supply ``expected_user_id``. ``verify_native`` authenticates
the original bearer with Navidrome, never with decoded JWT claims. No stale answer or
admin grace authorizes a personal operation. Neither method returns credentials.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import quote

import anyio
import httpx

from shijhon.navidrome.client import NavidromeError, NavidromeService


class AccessDenied(Exception):
    """The identity is gone, changed, or has no required current library grant."""


class AccessUnavailable(Exception):
    """Navidrome did not provide a trustworthy current authorization answer."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    is_admin: bool
    library_ids: frozenset[int]

    def can_access(self, library_id: int) -> bool:
        return library_id in self.library_ids


class AccessService:
    def __init__(self, navidrome: NavidromeService) -> None:
        self.navidrome = navidrome

    async def verify(self, username: str, *, expected_user_id: str | None = None) -> Principal:
        """Resolve an authenticated name and current grants; never adopt a new owner ID.

        Names are Navidrome's canonical names, compared without case. An explicitly
        empty expected ID fails closed, including sessions from older installations.
        Callers separately require the relevant library and, for host settings, admin.
        """
        if not username or expected_user_id == "":
            raise AccessDenied
        try:
            with anyio.fail_after(5):
                if expected_user_id is None:
                    # Navidrome's user-name filter is exact and case insensitive.
                    users = await self.navidrome.native_json(
                        "GET", "user", params={"user_name": username, "_start": 0, "_end": 2}
                    )
                    if not isinstance(users, list):
                        raise AccessUnavailable
                    found = [
                        u
                        for u in users
                        if isinstance(u, dict)
                        and isinstance(u.get("userName"), str)
                        and u["userName"].casefold() == username.casefold()
                    ]
                    if len(found) != 1:
                        raise AccessDenied
                    user = found[0]
                else:
                    user = await self.navidrome.native_json(
                        "GET", "user/" + quote(expected_user_id, safe="")
                    )
                if not isinstance(user, dict):
                    raise AccessUnavailable
                ident, name, admin = user.get("id"), user.get("userName"), user.get("isAdmin")
                if not isinstance(ident, str) or not ident or not isinstance(name, str) or not name:
                    raise AccessUnavailable
                if type(admin) is not bool:
                    raise AccessUnavailable
                if name.casefold() != username.casefold() or (
                    expected_user_id is not None and ident != expected_user_id
                ):
                    raise AccessDenied
                libraries = await self.navidrome.native_json(
                    "GET", "user/" + quote(ident, safe="") + "/library"
                )
                if not isinstance(libraries, list) or any(
                    not isinstance(lib, dict) or type(lib.get("id")) is not int for lib in libraries
                ):
                    raise AccessUnavailable
                return Principal(ident, name, admin, frozenset(lib["id"] for lib in libraries))
        except NavidromeError as exc:
            if exc.status == 404 and expected_user_id:
                raise AccessDenied from None
            raise AccessUnavailable from None
        except (TimeoutError, ValueError, KeyError, TypeError):
            raise AccessUnavailable from None

    async def verify_native(self, token: str) -> Principal:
        """Verify a native bearer by a protected upstream self read, then current grants.

        Navidrome 0.64.2 includes immutable ``uid`` and username ``sub`` claims. The
        decoded uid below is only a bounded locator. Navidrome verifies signature,
        expiry, token version and the current user before permitting
        ``GET /api/user/{id}`` (self or admin).
        """
        try:
            if not token or len(token) > 16_384:
                raise ValueError
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            username = claims["sub"]
            user_id = claims["uid"]
            if not isinstance(username, str) or not username or len(username) > 200:
                raise ValueError
            if not isinstance(user_id, str) or not user_id or len(user_id) > 200:
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise AccessDenied from None
        try:
            # Reuse the transport, but build the request without the client's cookies,
            # headers or query defaults. Only the caller's bearer may authenticate it.
            with anyio.fail_after(5):
                response = await self.navidrome.http.send(
                    httpx.Request(
                        "GET",
                        self.navidrome.base_url + "/api/user/" + quote(user_id, safe=""),
                        headers={"x-nd-authorization": "Bearer " + token},
                    ),
                    auth=None,
                    follow_redirects=False,
                )
            if response.status_code in (401, 403, 404):
                raise AccessDenied
            if response.status_code != 200:
                raise AccessUnavailable
            user = response.json()
            if not isinstance(user, dict) or (
                user.get("id") != user_id or user.get("userName") != username
            ):
                raise AccessDenied
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise AccessUnavailable from None
        return await self.verify(username, expected_user_id=user_id)

    async def personal(self, username: str, *, expected_user_id: str) -> Principal:
        """Verify an existing owner and the configured Shijhon library grant."""
        principal = await self.verify(username, expected_user_id=expected_user_id)
        if not principal.can_access(self.navidrome.library_id):
            raise AccessDenied
        return principal
