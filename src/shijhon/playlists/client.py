"""Fresh owner authorization and owner-authenticated native playlist calls."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from shijhon.access import AccessDenied, AccessService, AccessUnavailable, Principal
from shijhon.navidrome.client import NavidromeService
from shijhon.playlists.model import PlaylistError


class OwnerAuthorization(Protocol):
    """Native credentials without a dependency on listening history or curation."""

    @property
    def user_id(self) -> str: ...

    @property
    def username(self) -> str: ...

    @property
    def token(self) -> SecretStr: ...

    @property
    def salt(self) -> SecretStr: ...


class OwnerClient:
    def __init__(self, native: NavidromeService) -> None:
        self.native = native
        self.access = AccessService(native)
        self.http = httpx.AsyncClient(timeout=30, trust_env=False)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def login(self, username: str, password: str, address: str) -> dict[str, Any]:
        try:
            reply = await self.http.post(
                f"{self.native.base_url}/auth/login",
                json={"username": username, "password": password},
                headers={"x-forwarded-for": address, "x-real-ip": address},
            )
            body = reply.json()
            if reply.status_code != 200 or not isinstance(body, dict):
                raise ValueError
            if not isinstance(body.get("username"), str) or (
                body["username"].casefold() != username.casefold()
            ):
                raise ValueError
            if not all(
                isinstance(body.get(k), str) and body[k]
                for k in ("id", "subsonicToken", "subsonicSalt")
            ):
                raise ValueError
            await self.access.personal(body["username"], expected_user_id=body["id"])
            return body
        except (httpx.HTTPError, ValueError, AccessDenied, AccessUnavailable):
            raise PlaylistError(
                "The playlist account could not be connected. Check your password."
            ) from None

    async def principal(self, connection: OwnerAuthorization) -> Principal:
        # Check immutable identity too: a deleted/recreated username is another owner.
        try:
            return await self.access.personal(
                connection.username, expected_user_id=connection.user_id
            )
        except AccessDenied:
            raise PlaylistError("The connected owner cannot access this library.") from None
        except AccessUnavailable:
            raise PlaylistError("The connected owner's identity could not be verified.") from None

    async def validate(self, connection: OwnerAuthorization) -> None:
        # Credential proof comes before service-account identity/grant work, including
        # when saved authorization was revoked or its username was recreated.
        reply = await self.call(connection, "getUser", [("username", connection.username)])
        user = reply.get("user", {})
        if user.get("username") != connection.username or self.native.library_id not in user.get(
            "folder", []
        ):
            raise PlaylistError("The connected owner cannot access this library.")
        await self.principal(connection)

    async def history_key(self, jwt: str) -> SecretStr:
        """Read the installed application key with a temporary owner authorization.

        The native response also contains a callback token; neither it nor the JWT
        is saved. This GET does not link an account or contact the history service.
        """
        try:
            if not isinstance(jwt, str) or not jwt:
                raise ValueError
            reply = await self.http.get(
                f"{self.native.base_url}/api/lastfm/link",
                headers={"x-nd-authorization": f"Bearer {jwt}"},
            )
            body = reply.json()
            if reply.status_code != 200 or not isinstance(body, dict):
                raise ValueError
            key = body.get("apiKey")
            if not isinstance(key, str) or not key.strip() or len(key) > 2048:
                raise ValueError
            return SecretStr(key.strip())
        except (httpx.HTTPError, ValueError):
            raise PlaylistError(
                "Navidrome's Last.fm API key is unavailable. Enable Last.fm in Navidrome "
                "or choose Enter an API key."
            ) from None

    async def call(
        self, connection: OwnerAuthorization, method: str, params: Iterable[tuple[str, str]] = ()
    ) -> dict[str, Any]:
        auth = [
            ("u", connection.username),
            ("t", connection.token.get_secret_value()),
            ("s", connection.salt.get_secret_value()),
            ("v", "1.16.1"),
            ("c", "shijhon-playlists"),
            ("f", "json"),
        ]
        try:
            reply = await self.http.post(
                f"{self.native.base_url}/rest/{method}",
                content=urlencode([*auth, *params]),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            envelope = reply.json()["subsonic-response"]
            if reply.status_code != 200 or envelope.get("status") != "ok":
                raise ValueError
            if not isinstance(envelope, dict):
                raise ValueError
            return envelope
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise PlaylistError(
                "Navidrome could not complete the owner-authenticated request."
            ) from None

    async def playlist(self, connection: OwnerAuthorization, ident: str) -> dict[str, Any]:
        data: dict[str, Any] = (await self.call(connection, "getPlaylist", [("id", ident)]))[
            "playlist"
        ]
        if data.get("owner") != connection.username:
            raise PlaylistError(
                "Shijhon can only refresh a playlist owned by the connected account."
            )
        return data
