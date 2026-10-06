"""A small Subsonic client for tests: every login style, JSON or XML, GET or form POST,
repeated parameters, raw responses."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

Params = Mapping[str, Any] | Iterable[tuple[str, Any]]
AuthStyle = Literal["token", "password", "hex", "none"]


def expand(params: Params) -> list[tuple[str, str]]:
    """Mapping or pairs → pairs; list/tuple values become repeated parameters."""
    items = params.items() if isinstance(params, Mapping) else params
    out: list[tuple[str, str]] = []
    for key, value in items:
        values: Sequence[Any] = value if isinstance(value, (list, tuple)) else [value]
        for one in values:
            if isinstance(one, bool):
                one = "true" if one else "false"
            out.append((key, str(one)))
    return out


class SubsonicError(AssertionError):
    pass


class SubsonicClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        auth: AuthStyle = "token",
        client: str = "shijhon-tests",
        version: str = "1.16.1",
        fmt: Literal["json", "xml"] = "json",
        salt: str | None = None,
        timeout: float = 60.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user, self.password = user, password
        self.auth, self.client_name, self.version, self.fmt = auth, client, version, fmt
        self.salt = salt
        self.default_headers = dict(headers or {})
        self.http = httpx.Client(timeout=timeout)

    def with_base(self, base_url: str) -> SubsonicClient:
        """The same identity against another server (e.g. the proxy instead of Navidrome)."""
        return SubsonicClient(
            base_url,
            self.user,
            self.password,
            auth=self.auth,
            client=self.client_name,
            version=self.version,
            fmt=self.fmt,
            salt=self.salt,
            headers=self.default_headers,
        )

    def auth_params(self) -> list[tuple[str, str]]:
        if self.auth == "none":
            creds: list[tuple[str, str]] = []
        elif self.auth == "token":
            salt = self.salt or secrets.token_hex(6)
            token = hashlib.md5((self.password + salt).encode()).hexdigest()  # noqa: S324
            creds = [("u", self.user), ("t", token), ("s", salt)]
        elif self.auth == "password":
            creds = [("u", self.user), ("p", self.password)]
        else:
            creds = [("u", self.user), ("p", "enc:" + self.password.encode().hex())]
        return [*creds, ("v", self.version), ("c", self.client_name)]

    def request(
        self,
        method: str,
        params: Params = (),
        *,
        http_method: str = "GET",
        fmt: Literal["json", "xml"] | None = None,
        headers: Mapping[str, str] | None = None,
        view_suffix: bool = False,
        stream: bool = False,
    ) -> httpx.Response:
        fmt = fmt or self.fmt
        items = self.auth_params() + ([("f", "json")] if fmt == "json" else []) + expand(params)
        url = f"{self.base_url}/rest/{method}{'.view' if view_suffix else ''}"
        headers = {**self.default_headers, **(headers or {})}
        if http_method == "POST":
            request = self.http.build_request(
                "POST",
                url,
                content=urlencode(items).encode(),
                headers={"content-type": "application/x-www-form-urlencoded", **headers},
            )
        else:
            request = self.http.build_request(http_method, url, params=items, headers=headers)
        return self.http.send(request, stream=stream)

    def ok(self, method: str, params: Params = (), **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, params, **kwargs)
        body: dict[str, Any] = response.json()["subsonic-response"]
        if body.get("status") != "ok":
            raise SubsonicError(f"{method}: {body.get('error')}")
        return body

    def error_code(self, method: str, params: Params = (), **kwargs: Any) -> int | None:
        body = self.request(method, params, **kwargs).json()["subsonic-response"]
        error = body.get("error")
        return None if error is None else int(error["code"])

    def close(self) -> None:
        self.http.close()
