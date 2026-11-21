"""Caller-owned native reads (temporary bearer tokens never enter durable state), and the
library index's source read with Shijhon's service account (``ServiceLibrary``)."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from pydantic import SecretStr

from shijhon.access import AccessDenied, AccessService, AccessUnavailable, Principal
from shijhon.collection.contracts import Actor, CollectionError, NativeAbsent
from shijhon.music_imports.model import NativeFile
from shijhon.navidrome import connect
from shijhon.navidrome.client import NavidromeError, NavidromeService, ServiceRefused
from shijhon.playlists.model import Connection, PlaylistError
from shijhon.playlists.service import Playlists

PAGE_BYTES = 32 * 1024 * 1024
COVER_BYTES = 5 * 1024 * 1024
TOKEN_SECONDS = 3600.0
MAX_OWNERS = 128
MAX_STARRED = 20_000  # an account's favorite songs or albums read at most, of each
log = logging.getLogger(__name__)


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
    credentials for a caller's reads here. (Shijhon's own scans of the library index, with
    its service account, are ``ServiceLibrary``'s.)
    """

    def __init__(self, playlists: Playlists) -> None:
        self.playlists = playlists
        self.access = AccessService(playlists.client.native)
        self.library_id = playlists.client.native.library_id
        self.base_url = playlists.client.native.base_url
        self.http = connect.async_client(
            playlists.client.native.socket, timeout=10, follow_redirects=False
        )
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
            total = _page_total(reply, offset, self._totals.get(owner_id))
            self._totals[owner_id] = total

        raw, _ = await self._read(
            "/api/song",
            params=_page_params(library_id, offset, limit),
            headers={"x-nd-authorization": "Bearer " + token},
            maximum=PAGE_BYTES,
            response_check=page_headers,
        )
        return _library_page(raw, offset, limit, total, self.library_id)

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


def _page_params(library_id: str, offset: int, limit: int) -> dict[str, str]:
    """One page of the library's present songs, in a stable order (``Access.songs``)."""
    return {
        "library_id": library_id,
        "_start": str(offset),
        "_end": str(offset + limit),
        "_sort": "id",
        "_order": "ASC",
        "missing": "false",
    }


def _page_total(reply: httpx.Response, offset: int, previous: int | None) -> int:
    """A library page's total; a later page of a total that changed is refused."""
    value = reply.headers.get("x-total-count", "")
    if not value.isdecimal() or not 0 <= int(value) <= 10_000_000:
        raise ValueError
    total = int(value)
    if offset and previous is not None and previous != total:
        raise ValueError
    return total


def _library_page(
    raw: bytes, offset: int, limit: int, total: int, library_id: int
) -> Sequence[Mapping[str, Any]]:
    """A complete page of the configured library's songs, or a refusal."""
    try:
        rows = json.loads(raw)
        if (
            not isinstance(rows, list)
            or len(rows) > limit
            or offset + len(rows) > total
            or (len(rows) < limit and offset + len(rows) != total)
            or any(not isinstance(row, dict) or row.get("libraryId") != library_id for row in rows)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise CollectionError("Navidrome returned an invalid library page.") from None
    return rows


class ServiceProblem(CollectionError):
    """Navidrome refuses Shijhon's service account the library index's reads: what is wrong,
    in words for the person who set Shijhon up (no credentials in it). Asking again changes
    nothing until the configuration or the account is put right."""


class ServiceLibrary:
    """The library index's source read as Shijhon's own service account (``[navidrome]
    user``, a Navidrome admin), so that apps get their library lists with nobody signing
    in first: after a first start, a new library or a takeover of a Navidrome in use.

    The index it fills is the one a caller's refresh fills (``Index.scan``): the configured
    library's songs, structural fields only (``structural_metadata``: no favorites,
    ratings or plays of the account reading them), in pages of at most 500 in ID order.
    Navidrome lists a library's songs alike to every account that may read the library, so
    whose reads fill it changes nothing in it. What each account sees stays decided per
    request, by its own current grants and library choices (``Visibility``); this reads
    no account's songs, choices or covers. Its sign-in is the service account's own, as for
    scans and every other call of Shijhon's (``NavidromeService``); nothing is kept here.
    """

    def __init__(self, navidrome: NavidromeService, access: AccessService | None = None) -> None:
        self.navidrome = navidrome
        self.access = access or AccessService(navidrome)
        self.library_id = navidrome.library_id
        self._total: int | None = None

    async def principal(self) -> Principal:
        """The service account as Navidrome has it now: an admin that may read the
        configured library. Raises ``ServiceProblem`` when it is not (or Navidrome refuses
        its sign-in), else ``CollectionError`` when that cannot be told now."""
        try:
            await self.navidrome.sign_in()
        except ServiceRefused as exc:
            raise ServiceProblem(refusal(exc)) from None
        except NavidromeError:
            raise CollectionError(UNREACHABLE) from None
        try:
            principal = await self.access.verify(self.navidrome.user)
        except AccessDenied:
            raise ServiceProblem(SERVICE_SIGN_IN) from None
        except AccessUnavailable:
            raise CollectionError(UNREACHABLE) from None
        if not principal.is_admin:
            raise ServiceProblem(SERVICE_ADMIN)
        if not principal.can_access(self.library_id):
            raise ServiceProblem(SERVICE_LIBRARY)
        return principal

    async def authorize(self, owner_id: str) -> Actor:
        principal = await self.principal()
        if principal.user_id != owner_id:
            raise CollectionError("Shijhon's service account changed. Retry the refresh.")
        return Actor(principal.user_id, frozenset(str(i) for i in principal.library_ids))

    async def songs(
        self, owner_id: str, library_id: str, offset: int, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        if library_id != str(self.library_id):
            raise CollectionError("Choose Shijhon's configured music library.")
        if not 0 <= offset <= 10_000_000 or not 1 <= limit <= 500:
            raise CollectionError("Choose a bounded library page.")
        if not offset:
            self._total = None
        try:
            with anyio.fail_after(10):
                reply = await self.navidrome.native(
                    "GET", "song", params=_page_params(library_id, offset, limit)
                )
            total = _page_total(reply, offset, self._total)
            if len(reply.content) > PAGE_BYTES:
                raise ValueError
        except ServiceRefused as exc:
            raise ServiceProblem(refusal(exc)) from None
        except (NavidromeError, TimeoutError, ValueError):
            raise CollectionError(
                "Navidrome could not return this library item. Try again."
            ) from None
        self._total = total
        return _library_page(reply.content, offset, limit, total, self.library_id)


SETTINGS = (
    "Check the name and password of Shijhon's Navidrome account in its configuration"
    " ([navidrome] user and password_file, or the NixOS module's navidrome options; see"
    " docs/configuration.md)."
)
SERVICE_SIGN_IN = (
    "Shijhon's service account could not sign in to Navidrome, so the library view can't be"
    " prepared. " + SETTINGS
)
SERVICE_ADMIN = (
    "Shijhon's service account is not a Navidrome admin, so the library view can't be"
    " prepared. Make it an admin in Navidrome."
)
SERVICE_LIBRARY = (
    "Shijhon's service account can't read the configured music library ([navidrome]"
    " library_id), so the library view can't be prepared."
)
UNREACHABLE = "Navidrome could not be reached to prepare the library view. Try again."
POLL_SECONDS = 30.0  # how often Navidrome's library is looked at for changes
RETRY_SECONDS = (30.0, 600.0)  # the wait after a failed preparation: the first, the longest
# The longest a change made only by Shijhon's own writes waits for a full refresh: their
# songs join the lists from apps' requests meanwhile (``written.py``).
OWN_SECONDS = 3600.0
# An outside change (someone's music added, changed or gone): read once the changes have
# settled - a look that saw none new, so a scan of an album is one read, not one per song -
# and at least GAP_SECONDS after the last read ended, or GAP_READS times as long as that read
# took, whichever is longer, so that a large library is never read back to back (a read took
# 6 s at 12,000 songs, 56 s at 50,000: 60 s and 112 s apart, at most a third of the time).
# Changes that go on and on are read at least every ``interval`` (five minutes).
GAP_SECONDS = 60.0
GAP_READS = 2.0
CHANGED_AT_MOST = 10_000  # changed songs looked at one by one; more: a full refresh
CHANGED_PAGE = 500
_SUBSONIC_REFUSED = (40, 41)  # wrong credentials; token authentication not supported


def refusal(exc: ServiceRefused) -> str:
    """What a refusal of the service account means, in words for whoever set Shijhon up."""
    return SERVICE_SIGN_IN if exc.status in (401, 403) else SERVICE_ADMIN


def _instant(value: Any) -> datetime | None:
    """A Navidrome timestamp ("2026-11-21T09:38:41.474069594+01:00": nanoseconds, any
    offset), comparable; None when it is none."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", value)
    if match is None:
        return None
    whole, fraction, zone = match.groups()
    try:
        moment = datetime.fromisoformat(
            whole
            + "."
            + (fraction or "0")[:6].ljust(6, "0")
            + ("+00:00" if zone in (None, "Z") else zone)
        )
    except ValueError:
        return None
    return moment if moment.year > 1 else None


def _within(path: str, folder: str) -> bool:
    """Whether a library-relative path is inside ``folder``, a folder at the library's
    root ("Rock/_imports/x.flac" is not inside "_imports")."""
    return bool(folder) and path.replace("\\", "/").lstrip("/").startswith(folder + "/")


@dataclass(frozen=True)
class _Status:
    scanning: bool
    mark: tuple[Any, ...]  # the configured library's last scan and counts, as Navidrome has them
    created: Any  # when the library was made (another one under the same ID: another value)


class SourceRefresh:
    """Coalesce bounded source refresh work: triggered by verified active callers with
    their own native sign-in, and - with ``service`` - by ``run``, which prepares the
    library with Shijhon's service account when no one signs in (``ServiceLibrary``).

    What changed at Navidrome is told by the songs themselves: the configured library's
    songs whose ``updatedAt`` is newer than the last refresh's start, newest first. A
    change only in Shijhon's own folders waits up to ``OWN_SECONDS``: its placeholders
    (``placeholder_folder``), which join the lists from apps' requests, and music imports
    added or changed (``imports_folder``), which refresh the index themselves - an import
    deleted or gone missing is the person's music going, so it does not. Any other change
    - a song added, changed or gone missing elsewhere, or counts that changed with no song
    changed (a Navidrome restored from a backup) - is read once the changes settle
    (``GAP_SECONDS``): usually within a minute or two, at least every ``interval`` while
    they go on. Telling them
    apart by where the songs are, not by which scan found them, cannot take a scan of
    someone else's music for one of Shijhon's.

    In automatic mode a verified caller's native sign-in is only remembered (for its own
    reads); its requests start no refresh of their own: ``run`` decides by what changed.
    """

    def __init__(
        self,
        reader: CallerLibrary,
        refresh: Callable[..., Awaitable[None]],
        spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], bool],
        *,
        interval: float = 300,
        service: ServiceLibrary | None = None,
        prepared: Callable[[str], Awaitable[bool]] | None = None,
        poll: float = POLL_SECONDS,
        placeholder_folder: str = "",
        imports_folder: str = "",
        saved: Path | None = None,
    ) -> None:
        """``refresh(owner, library)`` refreshes as a caller; ``refresh(owner, library,
        access=service)`` as the service account. ``prepared(library)``: whether the
        library has a complete snapshot. ``saved``: where what was last seen of Navidrome's
        library is kept across restarts (a small JSON file; none: a refresh after each
        start)."""
        self.reader, self.refresh, self.spawn, self.interval = reader, refresh, spawn, interval
        self.service, self.prepared, self.poll = service, prepared, poll
        self.placeholder_folder = placeholder_folder.strip("/")
        self.imports_folder = imports_folder.strip("/")
        self.saved = saved
        self._created: Any = None  # the library's ``createdAt`` the kept look was of
        self._running = False
        # Never (not 0.0: the monotonic clock counts from the machine's start, so a sign-in
        # in its first minutes would be taken for one right after a refresh).
        self._updated_at = -math.inf
        self._attempted_at = -math.inf
        # The configured library as last seen, and as of the last complete refresh's start;
        # whether that refresh began while Navidrome scanned; what changed since it.
        self._seen: tuple[Any, ...] | None = None
        self._mark: tuple[Any, ...] | None = None
        self._partial = False
        self._pending: str | None = None  # "own" | "external"
        # The newest song change the last refresh began after, and the newest looked at.
        self._since: datetime | None = None
        self._checked: datetime | None = None
        self._scanning = False
        self._reachable = True
        self._retry_at = -math.inf
        self._failures = 0
        self._again = ""  # why a refresh was asked for while one ran ("": none was)
        # When an outside change was first and last seen since the last read, and how long
        # that read took (``_due``).
        self._pending_since = -math.inf
        self._changed_at = -math.inf
        self._read_seconds = 0.0
        # Why the service account cannot prepare the library ("": nothing known).
        self.problem = ""

    @property
    def running(self) -> bool:
        return self._running

    @property
    def automatic(self) -> bool:
        """Whether the library is prepared without anyone signing in."""
        return self.service is not None and self.prepared is not None

    async def signed_in(self, principal: Principal, token: str) -> None:
        """A dashboard sign-in: the library is read again at once - in automatic mode with
        the service account and its bookkeeping (``requested``), the sign-in remembered for
        the account's own reads."""
        if self.automatic:
            self.reader.remember(principal, token)
            self.requested("dashboard sign-in")
            return
        self._updated_at = self._attempted_at = -math.inf
        await self.verified(principal, token)

    async def verified(self, principal: Principal, token: str) -> None:
        self.reader.remember(principal, token)
        if self.automatic:
            return  # (``run`` refreshes by what changed, as the service account)
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

    def requested(self, reason: str = "Refresh music") -> bool:
        """A refresh asked for now (Refresh music, a dashboard sign-in): with the service
        account, started in the background - or right after the one under way, which may
        have read the library before what is asked for. False when there is no service
        account to use, or the work could not be started."""
        if not self.automatic:
            return False
        if self._running:
            self._again = reason  # (one at a time; this one right after)
            return True

        async def run() -> None:
            try:
                await self._service_refresh(
                    str(self.reader.library_id), self._seen, False, reason=reason
                )
            except CollectionError:
                pass  # (the state and ``problem`` say why)
            finally:
                self._finished()

        self._running = True
        self._attempted_at = time.monotonic()
        if not self.spawn(run):
            self._running = False
            return False
        return True

    async def run(self) -> None:
        """Prepare and refresh the library index with the service account, so that apps
        get their library lists with no one signing in: at once when the library has no
        complete snapshot (a first start, a new library), also while Navidrome scans and
        again as soon as that scan ends; soon after a start when the library changed while
        Shijhon was stopped (each one, when nothing was kept of the last look); and after
        Navidrome's library changed (``SourceRefresh``). A failure is tried again after 30
        seconds, doubling up to ten minutes - but at once when Navidrome answers again or a
        scan has ended, and at the poll's pace, without doubling, while a library without a
        snapshot is being scanned."""
        if not self.automatic:
            return
        assert self.prepared is not None
        try:
            if await self.prepared(
                str(self.reader.library_id)
            ) and not await anyio.to_thread.run_sync(self._restore):
                # A library apps can use already, and nothing kept of the last look: its
                # refresh after the start waits until the start's own work and the apps'
                # reconnecting are past.
                await anyio.sleep(self.poll)
        except Exception:
            log.exception("library preparation failed")
        while True:
            try:
                await self.round()
            except CollectionError:
                pass  # (logged; the state and ``problem`` say why)
            except Exception:
                log.exception("library preparation failed")
            await anyio.sleep(self.poll)

    async def _status(self) -> _Status:
        """Whether Navidrome scans, and the configured library's last scan and counts.
        Raises ``ServiceProblem`` when Navidrome refuses the service account, else
        ``NavidromeError``."""
        assert self.service is not None
        navidrome = self.service.navidrome
        try:
            scan = await navidrome.scan_status()
            library = await navidrome.native_json("GET", f"library/{self.reader.library_id}")
        except ServiceRefused as exc:
            raise ServiceProblem(refusal(exc)) from None
        except NavidromeError as exc:
            if exc.code in _SUBSONIC_REFUSED:
                raise ServiceProblem(SERVICE_SIGN_IN) from None
            if exc.status in (401, 403):
                # An admin-only record refused under a kept sign-in: the account lost its
                # admin rights. Its next sign-in tells (``ServiceLibrary.principal``).
                navidrome.forget_sign_in()
                raise ServiceProblem(SERVICE_ADMIN) from None
            raise
        if not isinstance(library, dict):
            raise NavidromeError("library: unexpected answer")
        mark = tuple(
            library.get(key)
            for key in (
                "lastScanAt",
                "totalSongs",
                "totalFiles",
                "totalMissingFiles",
                "totalFolders",
            )
        )
        return _Status(scan.get("scanning") is True, mark, library.get("createdAt"))

    async def round(self) -> None:
        """One look at Navidrome's library, and a refresh with the service account when
        one is due (``run``)."""
        assert self.service is not None and self.prepared is not None
        if self._running:
            return
        library = str(self.reader.library_id)
        prepared = await self.prepared(library)
        status: _Status | None = None
        try:
            status = await self._status()
        except ServiceProblem as exc:
            self._problem(str(exc))
            if prepared:
                return  # (the last snapshot answers meanwhile)
        except NavidromeError:
            self._reachable = False
            if prepared:
                return  # (looked at again next time)
        if status is not None:
            if not self._reachable or (self._scanning and not status.scanning):
                # Navidrome answers again, or a scan ended: what failed is tried at once.
                self._failures, self._retry_at = 0, -math.inf
            self._reachable, self._scanning = True, status.scanning
            self._seen = status.mark
            if self._created is not None and self._created != status.created:
                # What was kept is of another library under this ID: as if nothing was.
                self._mark = self._since = self._checked = self._pending = None
                self._updated_at = -math.inf
            self._created = status.created
            self._problem("")  # (Navidrome takes the account as it is)
        now = time.monotonic()
        if now < self._retry_at or now - self._attempted_at < 10:
            return
        scanning = status is not None and status.scanning
        reason = "first preparation" if not prepared else "start"
        if prepared and self._mark is not None:
            if scanning:
                return  # once the scan has ended
            reason = "Navidrome's scan ended"
            if not self._partial:
                if self._seen != self._mark:
                    found = "external" if self._pending == "external" else await self._changes()
                    if found == "none" and (self._seen or ())[1:] != (self._mark or ())[1:]:
                        found = "external"  # counts changed with no song changed
                    self._mark = self._seen
                    if found in ("external", "own"):
                        self._changed_at = now
                    if found == "external" and self._pending != "external":
                        self._pending, self._pending_since = "external", now
                    elif found == "own" and self._pending is None:
                        self._pending = "own"
                    await anyio.to_thread.run_sync(self._keep)
                if not self._due(now):
                    return
                reason = "outside change" if self._pending == "external" else "own change"
        if self._running:  # (one began while this looked)
            return
        self._running = True
        self._attempted_at = now
        try:
            await self._service_refresh(
                library, self._seen, scanning, grow=not scanning, reason=reason
            )
        finally:
            self._finished()

    def _due(self, now: float) -> bool:
        """Whether what changed since the last read is to be read now (``SourceRefresh``)."""
        if self._pending == "own":
            return now - self._updated_at >= OWN_SECONDS
        if self._pending != "external":
            return False
        if now - self._updated_at < max(GAP_SECONDS, GAP_READS * self._read_seconds):
            return False
        settled = now - self._changed_at >= self.poll * 0.8  # (a look since saw none new)
        return settled or now - self._pending_since >= self.interval

    def _finished(self) -> None:
        """A service refresh ended: the one asked for meanwhile begins."""
        self._running = False
        if self._again:
            reason, self._again = self._again, ""
            self.requested(reason)

    async def _changes(self) -> str:
        """What changed in the configured library since the last refresh began: "none",
        "own" (only songs inside ``own_folders``) or "external". Newest first, in pages, at
        most ``CHANGED_AT_MOST`` songs (more: "external")."""
        assert self.service is not None
        stop = self._checked or self._since
        if stop is None:
            return "external"
        newest: datetime | None = None
        try:
            for offset in range(0, CHANGED_AT_MOST, CHANGED_PAGE):
                rows = await self.service.navidrome.native_json(
                    "GET",
                    "song",
                    params={
                        "library_id": str(self.reader.library_id),
                        "_sort": "updated_at",
                        "_order": "DESC",
                        "_start": str(offset),
                        "_end": str(offset + CHANGED_PAGE),
                    },
                )
                if not isinstance(rows, list):
                    return "external"
                for row in rows:
                    at = _instant(row.get("updatedAt")) if isinstance(row, dict) else None
                    if at is None:
                        return "external"
                    if at <= stop:
                        break
                    newest = newest or at
                    if not self._own(row):
                        return "external"
                else:
                    if len(rows) == CHANGED_PAGE:
                        continue
                break
            else:
                return "external"  # (too many to look at one by one)
        except NavidromeError:
            return "external"
        if newest is None:
            return "none"
        self._checked = newest
        return "own"

    def _own(self, row: Mapping[str, Any]) -> bool:
        """Whether a changed song is Shijhon's own change: a placeholder, or an import
        added or changed (one deleted or gone missing is the person's music going)."""
        path = str(row.get("path", ""))
        if _within(path, self.placeholder_folder):
            return True
        return _within(path, self.imports_folder) and row.get("missing") is not True

    async def _newest(self) -> datetime | None:
        """The newest song change of the configured library (the epoch: none yet)."""
        assert self.service is not None
        try:
            rows = await self.service.navidrome.native_json(
                "GET",
                "song",
                params={
                    "library_id": str(self.reader.library_id),
                    "_sort": "updated_at",
                    "_order": "DESC",
                    "_start": "0",
                    "_end": "1",
                },
            )
        except NavidromeError:
            return None
        if not isinstance(rows, list):
            return None
        if not rows:
            return datetime(1970, 1, 1, tzinfo=UTC)
        return _instant(rows[0].get("updatedAt")) if isinstance(rows[0], dict) else None

    async def _service_refresh(
        self,
        library: str,
        seen: tuple[Any, ...] | None,
        scanning: bool,
        *,
        grow: bool = True,
        reason: str = "",
    ) -> None:
        assert self.service is not None
        began = time.monotonic()
        log.info("library read (%s) started", reason or "asked for")
        try:
            principal = await self.service.principal()
            since = await self._newest()
            await self.refresh(principal.user_id, library, access=self.service)
        except CollectionError as exc:
            self._failed(exc, grow=grow)
            raise
        except Exception:
            log.exception("library preparation failed")
            self._failed(CollectionError("the refresh failed"), grow=grow)
            raise CollectionError("The library refresh failed. Try again.") from None
        if self.problem:
            log.info("library preparation: Shijhon's service account works again")
        self.problem = ""
        self._failures = 0
        self._retry_at = -math.inf
        self._updated_at = time.monotonic()
        self._read_seconds = self._updated_at - began
        log.info("library read (%s) finished in %.1fs", reason or "asked for", self._read_seconds)
        self._mark, self._partial, self._pending = seen, scanning, None
        self._since = self._checked = since
        await anyio.to_thread.run_sync(self._keep)

    def _failed(self, exc: CollectionError, *, grow: bool = True) -> None:
        """A failed preparation: tried again after the backoff (``grow``: one step longer;
        else at the poll's pace, while a library without a snapshot is being scanned)."""
        if grow:
            delay = min(RETRY_SECONDS[1], RETRY_SECONDS[0] * 2**self._failures)
            self._failures += 1
        else:
            delay = self.poll
        self._retry_at = time.monotonic() + delay
        if isinstance(exc, ServiceProblem):
            self._problem(str(exc))
        else:
            log.info("library preparation stopped (%s); tried again in %.0fs", exc, delay)

    def _problem(self, problem: str) -> None:
        if problem and self.problem != problem:
            log.warning("library preparation: %s", problem)
        self.problem = problem

    def _keep(self) -> None:
        """What was last seen of Navidrome's library, kept for the next start (``saved``)."""
        if self.saved is None or self._mark is None or self._since is None:
            return
        data = {
            "library": self.reader.library_id,
            "created": self._created,
            "mark": list(self._mark),
            "since": self._since.isoformat(),
            "checked": (self._checked or self._since).isoformat(),
            "pending": self._pending,
            "refreshed": time.time() - (time.monotonic() - self._updated_at),
        }
        try:
            self.saved.parent.mkdir(parents=True, exist_ok=True)
            partial = self.saved.with_suffix(".partial")
            partial.write_text(json.dumps(data, allow_nan=False))
            partial.replace(self.saved)
        except (OSError, ValueError):
            pass  # (the next start refreshes once)

    def _restore(self) -> bool:
        """What the last run saw of Navidrome's library (``_keep``); False: nothing kept."""
        if self.saved is None:
            return False
        try:
            data = json.loads(self.saved.read_text())
            mark = tuple(data["mark"])
            since = datetime.fromisoformat(data["since"])
            checked = datetime.fromisoformat(data["checked"])
            pending = data.get("pending")
            refreshed = float(data["refreshed"])
            if (
                data.get("library") != self.reader.library_id
                or data.get("created") is None
                or since.tzinfo is None
                or checked.tzinfo is None
                or pending not in (None, "own", "external")
                or not math.isfinite(refreshed)
            ):
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self._mark, self._since, self._checked, self._pending = mark, since, checked, pending
        self._created = data["created"]
        self._updated_at = time.monotonic() - max(0.0, time.time() - refreshed)
        return True
