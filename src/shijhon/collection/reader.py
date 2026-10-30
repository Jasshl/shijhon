"""Caller-owned native reads; temporary bearer tokens never enter durable state."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from pydantic import SecretStr

from shijhon.access import AccessDenied, AccessService, AccessUnavailable, Principal
from shijhon.collection.contracts import Actor, CollectionError, NativeAbsent
from shijhon.music_imports.model import NativeFile
from shijhon.playlists.model import Connection, PlaylistError
from shijhon.playlists.service import Playlists

PAGE_BYTES = 32 * 1024 * 1024
COVER_BYTES = 5 * 1024 * 1024
TOKEN_SECONDS = 3600.0
MAX_OWNERS = 128
MAX_STARRED = 20_000  # an account's favorite songs or albums read at most, of each


class _NotFound(Exception):
    """Navidrome answered 404 (``_read`` with ``absent``)."""


@dataclass(frozen=True)
class _Bearer:
    token: SecretStr
    used_at: float


class CallerLibrary:
    """Refresh shared structural metadata using only an authorized caller's reads.

    Native callers supply their original verified bearer. Dashboard password sign-in
    supplies the bearer from that same login. A restart drops every bearer, retaining
    the last complete structural index and saved per-user membership. Saved Subsonic
    credentials can still authorize membership actions, but never substitute service
    credentials for a native library scan.
    """

    def __init__(self, playlists: Playlists) -> None:
        self.playlists = playlists
        self.access = AccessService(playlists.client.native)
        self.library_id = playlists.client.native.library_id
        self.base_url = playlists.client.native.base_url
        self.http = httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False)
        self._bearers: OrderedDict[str, _Bearer] = OrderedDict()
        self._totals: dict[str, int] = {}

    async def aclose(self) -> None:
        self._bearers.clear()
        self._totals.clear()
        await self.http.aclose()

    def remember(self, principal: Principal, token: str) -> None:
        """Called only after the original bearer or login has been verified upstream."""
        if not principal.can_access(self.library_id) or not token or len(token) > 16_384:
            return
        now = time.monotonic()
        self._bearers = OrderedDict(
            (owner, entry)
            for owner, entry in self._bearers.items()
            if now - entry.used_at < TOKEN_SECONDS and owner != principal.user_id
        )
        self._bearers[principal.user_id] = _Bearer(SecretStr(token), now)
        while len(self._bearers) > MAX_OWNERS:
            self._bearers.popitem(last=False)
        self._totals = {
            owner: total for owner, total in self._totals.items() if owner in self._bearers
        }

    def _token(self, owner: str) -> str:
        entry = self._bearers.get(owner)
        if entry and time.monotonic() - entry.used_at < TOKEN_SECONDS:
            return entry.token.get_secret_value()
        self._bearers.pop(owner, None)
        return ""

    async def _native(self, owner: str) -> tuple[Principal, str]:
        token = self._token(owner)
        if not token:
            raise CollectionError("Sign in again to refresh your music.")
        try:
            principal = await self.access.verify_native(token)
            if principal.user_id != owner or not principal.can_access(self.library_id):
                raise AccessDenied
        except AccessDenied:
            entry = self._bearers.get(owner)
            if entry is not None and entry.token.get_secret_value() == token:
                self._bearers.pop(owner, None)
            raise CollectionError("Your current library access could not be verified.") from None
        except AccessUnavailable:
            raise CollectionError(
                "Navidrome could not verify your library access. Try again."
            ) from None
        # Expire idle authorization, not an import that is still proving access.
        # A concurrent sign-in/eviction must not be overwritten by this older proof.
        entry = self._bearers.get(owner)
        if entry is not None and entry.token.get_secret_value() == token:
            self._bearers[owner] = _Bearer(entry.token, time.monotonic())
            self._bearers.move_to_end(owner)
        return principal, token

    async def token_for(self, principal: Principal) -> str | None:
        """A currently verified original bearer for bounded caller-owned query work."""
        if not self._token(principal.user_id):
            return None
        current, token = await self._native(principal.user_id)
        if current != principal:
            return None
        return token

    async def song(self, owner_id: str, library_id: str, ident: str) -> Mapping[str, Any]:
        """One exact native song, authenticated by the owner's original bearer.

        Used after an approved collection restoration; no service credential or
        saved structural row can stand in for current native accessibility.
        """
        self._library(library_id)
        if not ident or len(ident) > 300:
            raise CollectionError("Choose a valid library recording.")
        principal, token = await self._native(owner_id)
        absent, row = False, dict[str, Any]()
        try:
            raw, _ = await self._read(
                "/api/song/" + quote(ident, safe=""),
                params={},
                maximum=128 * 1024,
                headers={"x-nd-authorization": "Bearer " + token},
                absent=True,
            )
        except _NotFound:
            absent, raw = True, b""
        if not absent:
            try:
                answer = json.loads(raw)
                if (
                    not isinstance(answer, dict)
                    or answer.get("id") != ident
                    or answer.get("libraryId") != self.library_id
                    or not isinstance(answer.get("missing", False), bool)
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise CollectionError("The native recording could not be verified.") from None
            row = answer
            absent = row.get("missing", False) is True
        current, _ = await self._native(owner_id)
        if current != principal:
            raise CollectionError("Your library access changed. Retry this selection.")
        if absent:
            # The same verified caller before and after: the song is not there.
            raise NativeAbsent("The native recording is not in the library.")
        return row

    def has_bearer(self, owner_id: str) -> bool:
        """Whether the account's own native sign-in is at hand (not yet verified again)."""
        return bool(self._token(owner_id))

    async def starred(self, owner_id: str) -> set[tuple[str, str]]:
        """The songs and albums the account favorited - ("song" | "album", ID) -, read as
        the account with its verified native sign-in, in pages of 500. More than
        ``MAX_STARRED`` of either is refused: a partial set would take favorites away."""
        principal, token = await self._native(owner_id)
        found: set[tuple[str, str]] = set()
        for kind, path in (("song", "/api/song"), ("album", "/api/album")):
            offset, expected = 0, None
            seen: set[str] = set()
            while True:
                total = 0

                def counted(reply: httpx.Response) -> None:
                    nonlocal total
                    value = reply.headers.get("x-total-count", "")
                    if not value.isdecimal():
                        raise ValueError
                    total = int(value)

                raw, _ = await self._read(
                    path,
                    params={
                        "starred": "true",
                        "_start": str(offset),
                        "_end": str(offset + 500),
                        "_sort": "id",
                        "_order": "ASC",
                    },
                    headers={"x-nd-authorization": "Bearer " + token},
                    maximum=PAGE_BYTES,
                    response_check=counted,
                )
                if total > MAX_STARRED:
                    raise CollectionError("Too many favorites to read them all.")
                try:
                    rows = json.loads(raw)
                    if not isinstance(rows, list) or len(rows) > 500:
                        raise ValueError
                except ValueError:
                    raise CollectionError("Navidrome did not list the favorites.") from None
                if expected is not None and total != expected:
                    raise CollectionError("The favorites changed while they were read. Try again.")
                expected = total
                if len(rows) != min(500, total - offset):
                    raise CollectionError("Navidrome did not list all the favorites. Try again.")
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or row.get("starred") is not True
                        or not isinstance(row.get("id"), str)
                        or not 0 < len(row["id"]) <= 300
                        or row["id"] in seen
                    ):
                        raise CollectionError("Navidrome returned invalid favorites. Try again.")
                    seen.add(row["id"])
                    found.add((kind, row["id"]))
                offset += len(rows)
                if offset == total:
                    break
        current, _ = await self._native(owner_id)
        if current != principal:
            raise CollectionError("Your library access changed.")
        return found

    async def native_actor(self, owner_id: str, library_id: str) -> Actor:
        """Require an original native bearer before authorizing a media restoration."""
        self._library(library_id)
        principal, _ = await self._native(owner_id)
        return Actor(principal.user_id, frozenset(str(i) for i in principal.library_ids))

    async def _connection(self, owner: str) -> Connection:
        try:
            return await self.playlists.connection(owner)
        except PlaylistError:
            raise CollectionError("Your current library access could not be verified.") from None

    async def authorize(self, owner_id: str) -> Actor:
        if self._token(owner_id):
            try:
                principal, _ = await self._native(owner_id)
                return Actor(principal.user_id, frozenset(str(i) for i in principal.library_ids))
            except CollectionError:
                # An expired temporary bearer does not revoke independently valid
                # saved credentials. An unavailable proof still fails closed.
                if self._token(owner_id):
                    raise
        connection = await self._connection(owner_id)
        try:
            principal = await self.playlists.client.principal(connection)
        except PlaylistError:
            raise CollectionError("Your current library access could not be verified.") from None
        return Actor(principal.user_id, frozenset(str(i) for i in principal.library_ids))

    def _library(self, library_id: str) -> None:
        if library_id != str(self.library_id):
            raise CollectionError("Choose Shijhon's configured music library.")

    async def songs(
        self, owner_id: str, library_id: str, offset: int, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        self._library(library_id)
        if not 0 <= offset <= 10_000_000 or not 1 <= limit <= 500:
            raise CollectionError("Choose a bounded library page.")
        token = self._token(owner_id)
        if not token:
            raise CollectionError("Sign in again to refresh your music.")
        total = 0

        def page_headers(reply: httpx.Response) -> None:
            nonlocal total
            value = reply.headers.get("x-total-count", "")
            if not value.isdecimal() or not 0 <= int(value) <= 10_000_000:
                raise ValueError
            total = int(value)
            previous = self._totals.get(owner_id)
            if offset and previous is not None and previous != total:
                raise ValueError
            self._totals[owner_id] = total

        raw, _ = await self._read(
            "/api/song",
            params={
                "library_id": library_id,
                "_start": str(offset),
                "_end": str(offset + limit),
                "_sort": "id",
                "_order": "ASC",
                "missing": "false",
            },
            headers={"x-nd-authorization": "Bearer " + token},
            maximum=PAGE_BYTES,
            response_check=page_headers,
        )
        try:
            rows = json.loads(raw)
            if (
                not isinstance(rows, list)
                or len(rows) > limit
                or offset + len(rows) > total
                or (len(rows) < limit and offset + len(rows) != total)
                or any(
                    not isinstance(row, dict) or row.get("libraryId") != self.library_id
                    for row in rows
                )
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise CollectionError("Navidrome returned an invalid library page.") from None
        return rows

    async def artwork(self, owner_id: str, library_id: str, ident: str) -> tuple[bytes, str]:
        self._library(library_id)
        # The service also checks that this structural cover ID belongs to the index.
        connection = await self._connection(owner_id)
        return await self._read(
            "/rest/getCoverArt",
            params={
                "u": connection.username,
                "t": connection.token.get_secret_value(),
                "s": connection.salt.get_secret_value(),
                "v": "1.16.1",
                "c": "shijhon",
                "id": ident,
                "size": "300",
            },
            maximum=COVER_BYTES,
        )

    async def imported_file(
        self, owner_id: str, library_id: str, relative_path: str
    ) -> NativeFile | None:
        """Find one retained file using its owner's original native credentials.

        Navidrome's path filter is a prefix filter. A tiny bounded answer is checked
        for an exact path, current library, regular native ID and size before it can
        serve as evidence for an import receipt. No service-account metadata is used.
        """
        self._library(library_id)
        if (
            not relative_path
            or len(relative_path.encode("utf-8")) > 4096
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in ("", ".", "..") for part in relative_path.split("/"))
            or any(ord(char) < 32 for char in relative_path)
        ):
            raise CollectionError("Choose a valid imported file.")
        principal, token = await self._native(owner_id)

        def bounded(reply: httpx.Response) -> None:
            count = reply.headers.get("x-total-count", "")
            if not count.isdecimal() or not 0 <= int(count) <= 2:
                raise ValueError

        raw, _ = await self._read(
            "/api/song",
            params={
                "library_id": library_id,
                "path": relative_path,
                "missing": "false",
                "_start": "0",
                "_end": "2",
                "_sort": "id",
                "_order": "ASC",
            },
            headers={"x-nd-authorization": "Bearer " + token},
            maximum=128 * 1024,
            response_check=bounded,
        )
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) > 2:
                raise ValueError
            matches = []
            for row in rows:
                if not isinstance(row, dict) or row.get("libraryId") != self.library_id:
                    raise ValueError
                if row.get("path") != relative_path:
                    continue
                if (
                    not isinstance(row.get("id"), str)
                    or not 0 < len(row["id"]) <= 256
                    or type(row.get("size")) is not int
                    or row["size"] <= 0
                    or row.get("missing", False) is not False
                ):
                    raise ValueError
                matches.append(NativeFile(row["id"], relative_path, row["size"]))
            if len(matches) > 1:
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise CollectionError("Navidrome returned invalid imported-file evidence.") from None
        current, _ = await self._native(owner_id)
        if current != principal:
            raise CollectionError("Your library access changed. Check this import again.")
        return matches[0] if matches else None

    async def imported_file_absent(
        self, owner_id: str, library_id: str, relative_path: str
    ) -> bool:
        """Prove exact native nonpresence, including retained missing-file rows.

        An ordinary import lookup excludes missing rows, so its None is never
        negative evidence. Read the entire tiny prefix result with the same caller
        and library grant before and after it.
        """
        self._library(library_id)
        if (
            not relative_path
            or len(relative_path.encode("utf-8")) > 4096
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in ("", ".", "..") for part in relative_path.split("/"))
            or any(ord(char) < 32 for char in relative_path)
        ):
            raise CollectionError("Choose a valid imported file.")
        principal, token = await self._native(owner_id)
        total = -1

        def bounded(reply: httpx.Response) -> None:
            nonlocal total
            count = reply.headers.get("x-total-count", "")
            if not count.isdecimal() or not 0 <= int(count) <= 2:
                raise ValueError
            total = int(count)

        raw, _ = await self._read(
            "/api/song",
            params={
                "library_id": library_id,
                "path": relative_path,
                "_start": "0",
                "_end": "2",
                "_sort": "id",
                "_order": "ASC",
            },
            headers={"x-nd-authorization": "Bearer " + token},
            maximum=128 * 1024,
            response_check=bounded,
        )
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) != total:
                raise ValueError
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("libraryId") != self.library_id
                    or not isinstance(row.get("path"), str)
                    or not row["path"].startswith(relative_path)
                    or not isinstance(row.get("id"), str)
                    or not 0 < len(row["id"]) <= 256
                    or type(row.get("missing", False)) is not bool
                ):
                    raise ValueError
        except (TypeError, ValueError, KeyError):
            raise CollectionError("Navidrome returned invalid imported-file evidence.") from None
        current, _ = await self._native(owner_id)
        if current != principal:
            raise CollectionError("Your library access changed. Check this import again.")
        return not any(row["path"] == relative_path for row in rows)

    async def _read(
        self,
        path: str,
        *,
        params: dict[str, str],
        maximum: int,
        headers: dict[str, str] | None = None,
        response_check: Callable[[httpx.Response], None] | None = None,
        absent: bool = False,
    ) -> tuple[bytes, str]:
        """``absent``: a 404 raises ``_NotFound`` (the caller tells absence apart)."""
        try:
            with anyio.fail_after(10):
                # No ambient cookies or auth may supplement the caller's credentials.
                request = httpx.Request("GET", self.base_url + path, params=params, headers=headers)
                reply = await self.http.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
                try:
                    if absent and reply.status_code == 404:
                        raise _NotFound
                    if reply.status_code != 200:
                        raise ValueError
                    if response_check:
                        response_check(reply)
                    body = bytearray()
                    async for chunk in reply.aiter_bytes():
                        if len(body) + len(chunk) > maximum:
                            raise ValueError
                        body.extend(chunk)
                    return bytes(body), reply.headers.get("content-type", "").split(";", 1)[0]
                finally:
                    await reply.aclose()
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise CollectionError(
                "Navidrome could not return this library item. Try again."
            ) from None


class SourceRefresh:
    """Coalesce bounded source refresh work triggered by verified active callers."""

    def __init__(
        self,
        reader: CallerLibrary,
        refresh: Callable[[str, str], Awaitable[None]],
        spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], bool],
        *,
        interval: float = 300,
    ) -> None:
        self.reader, self.refresh, self.spawn, self.interval = reader, refresh, spawn, interval
        self._running = False
        self._updated_at = 0.0
        self._attempted_at = 0.0

    @property
    def running(self) -> bool:
        return self._running

    async def signed_in(self, principal: Principal, token: str) -> None:
        self._updated_at = self._attempted_at = 0.0
        await self.verified(principal, token)

    async def verified(self, principal: Principal, token: str) -> None:
        self.reader.remember(principal, token)
        now = time.monotonic()
        if self._running or now - self._updated_at < self.interval or now - self._attempted_at < 10:
            return
        owner, library = principal.user_id, str(self.reader.library_id)

        async def run() -> None:
            try:
                await self.refresh(owner, library)
                self._updated_at = time.monotonic()
            finally:
                self._running = False

        self._running = True
        self._attempted_at = now
        if not self.spawn(run):
            self._running = False
