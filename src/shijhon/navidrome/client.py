"""Shijhon's own calls to Navidrome, as its service account (a Navidrome admin).

Two APIs are used:

- Subsonic, for scans (``startScan`` with folder targets, ``getScanStatus``) and reads;
- Navidrome's native API (the one its web UI uses), for exact file-to-song mapping: its
  song resource carries the path relative to the library root and can be filtered by path
  prefix. It is not a public contract, so the canary suite covers it on every upgrade.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import anyio
import httpx


class NavidromeError(RuntimeError):
    """A failed call to Navidrome. Messages never contain URLs (they carry credentials)."""

    def __init__(self, message: str, code: int | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class NavidromeService:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        client_name: str = "shijhon",
        library_id: int = 1,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user, self._password = user, password
        self.client_name = client_name
        self.library_id = library_id
        self.http = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._jwt: str | None = None
        self._login_lock = anyio.Lock()

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- Subsonic --------------------------------------------------------------------

    def _auth(self) -> list[tuple[str, str]]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()  # noqa: S324 - Subsonic
        return [
            ("u", self.user),
            ("t", token),
            ("s", salt),
            ("v", "1.16.1"),
            ("c", self.client_name),
            ("f", "json"),
        ]

    async def subsonic(self, method: str, params: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
        try:
            response = await self.http.get(
                f"{self.base_url}/rest/{method}", params=[*self._auth(), *params]
            )
        except httpx.HTTPError as exc:
            raise NavidromeError(f"{method}: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise NavidromeError(
                f"{method}: HTTP {response.status_code}", status=response.status_code
            )
        try:
            body: dict[str, Any] = response.json()["subsonic-response"]
        except (ValueError, KeyError, TypeError):
            raise NavidromeError(f"{method}: not a Subsonic response") from None
        if body.get("status") != "ok":
            error = body.get("error") or {}
            raise NavidromeError(f"{method}: {error.get('message')}", error.get("code"))
        return body

    async def start_scan(self, folders: Iterable[str], *, full: bool = False) -> dict[str, Any]:
        params = [("fullScan", "true" if full else "false")]
        params += [("target", f"{self.library_id}:{folder}") for folder in folders]
        status: dict[str, Any] = (await self.subsonic("startScan", params))["scanStatus"]
        return status

    async def scan_status(self) -> dict[str, Any]:
        status: dict[str, Any] = (await self.subsonic("getScanStatus"))["scanStatus"]
        return status

    async def album(self, album_id: str) -> dict[str, Any]:
        album: dict[str, Any] = (await self.subsonic("getAlbum", [("id", album_id)]))["album"]
        return album

    # --- native API ------------------------------------------------------------------

    async def _login(self) -> str:
        async with self._login_lock:
            if self._jwt is None:
                try:
                    response = await self.http.post(
                        f"{self.base_url}/auth/login",
                        json={"username": self.user, "password": self._password},
                    )
                except httpx.HTTPError as exc:
                    raise NavidromeError(f"service login: {type(exc).__name__}") from None
                if response.status_code != 200:
                    raise NavidromeError(f"service login failed ({response.status_code})")
                body = response.json()
                if not body.get("isAdmin"):
                    raise NavidromeError("the Shijhon service account must be a Navidrome admin")
                self._jwt = body["token"]
            assert self._jwt is not None
            return self._jwt

    async def native(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Navidrome's native API as the service account. ``path`` must already be quoted
        (use :func:`segment` for IDs that come from requests)."""
        for attempt in range(2):
            token = await self._login()
            try:
                response = await self.http.request(
                    method,
                    f"{self.base_url}/api/{path}",
                    headers={"x-nd-authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise NavidromeError(f"native {method}: {type(exc).__name__}") from None
            if response.status_code == 401 and attempt == 0:
                self._jwt = None
                continue
            refreshed = response.headers.get("x-nd-authorization", "")
            if refreshed.startswith("Bearer "):
                self._jwt = refreshed.removeprefix("Bearer ")
            if response.status_code >= 400:
                raise NavidromeError(
                    f"native {method}: HTTP {response.status_code}", status=response.status_code
                )
            return response
        raise NavidromeError("native API authentication failed")

    async def native_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.native(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise NavidromeError(f"native {method}: not JSON") from None

    async def songs_under(self, folder: str) -> list[dict[str, Any]]:
        """Songs whose library-relative path is inside ``folder`` (present or missing)."""
        prefix = folder.rstrip("/") + "/"
        songs: list[dict[str, Any]] = []
        start = 0
        while True:
            page = await self.native_json(
                "GET",
                "song",
                params={
                    "path": prefix,
                    "library_id": self.library_id,
                    "_start": start,
                    "_end": start + 500,
                    "_sort": "path",
                    "_order": "ASC",
                },
            )
            if not isinstance(page, list):
                raise NavidromeError("native song list: unexpected answer")
            # The filter is a SQL LIKE prefix; re-check exactly.
            songs += [s for s in page if s.get("path", "").startswith(prefix)]
            if len(page) < 500:
                return songs
            start += 500

    async def album_ids(self) -> list[str]:
        """Every present album of the library, most recently added first."""
        return [ident for ident, _, _ in await self.album_counts()]

    async def album_counts(self) -> list[tuple[str, int, int | None]]:
        """Every present album of the library, most recently added first: its ID, its
        number of songs and the track total its files carry (if any)."""
        found: dict[str, tuple[str, int, int | None]] = {}
        start = 0
        while True:
            page = await self.native_json(
                "GET",
                "album",
                params={
                    "library_id": self.library_id,
                    "missing": "false",
                    "_start": start,
                    "_end": start + 500,
                    "_sort": "recently_added",
                    "_order": "DESC",
                },
            )
            if not isinstance(page, list):
                raise NavidromeError("native album list: unexpected answer")
            for album in page:
                if isinstance(album, dict) and album.get("id"):
                    ident = str(album["id"])
                    found.setdefault(ident, (ident, _count(album.get("songCount")), _total(album)))
            if len(page) < 500:
                return list(found.values())  # a page boundary may repeat one
            start += 500

    async def songs_of_album(self, album_id: str) -> list[dict[str, Any]]:
        songs = await self.native_json(
            "GET",
            "song",
            params={"album_id": album_id, "_start": 0, "_end": 5000, "_sort": "path"},
        )
        if not isinstance(songs, list):
            raise NavidromeError("native song list: unexpected answer")
        return [s for s in songs if isinstance(s, dict)]

    async def song(self, song_id: str) -> dict[str, Any] | None:
        try:
            song = await self.native_json("GET", f"song/{segment(song_id)}")
        except NavidromeError as exc:
            if exc.status == 404:
                return None
            raise
        return song if isinstance(song, dict) else None

    async def playlist_song_ids(self, playlist_id: str) -> list[str]:
        tracks = await self.native_json(
            "GET", f"playlist/{segment(playlist_id)}/tracks", params={"_end": 5000}
        )
        if not isinstance(tracks, list):
            return []
        return [
            str(t.get("mediaFileId") or t.get("id"))
            for t in tracks
            if isinstance(t, dict) and (t.get("mediaFileId") or t.get("id"))
        ]

    async def share(self, share_id: str) -> dict[str, Any] | None:
        share = await self.native_json("GET", f"share/{segment(share_id)}")
        return share if isinstance(share, dict) else None

    async def artist_album_ids(self, artist_id: str) -> list[str]:
        try:
            artist = (await self.subsonic("getArtist", [("id", artist_id)]))["artist"]
        except (NavidromeError, KeyError):
            return []
        return [str(a["id"]) for a in artist.get("album", []) if isinstance(a, dict)]

    async def config(self) -> dict[str, Any] | None:
        """Navidrome's configuration as shown to admins, if this version exposes it."""
        try:
            body = await self.native_json("GET", "config/")
        except NavidromeError:
            return None
        return body if isinstance(body, dict) else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _total(album: dict[str, Any]) -> int | None:
    """The album's track total tag (Navidrome keeps it as an album-level tag)."""
    tags = album.get("tags")
    values = tags.get("tracktotal") if isinstance(tags, dict) else None
    first = values[0] if isinstance(values, list) and values else None
    try:
        total = int(str(first))
    except ValueError:
        return None
    return total if total > 0 else None


def segment(value: str) -> str:
    """One path segment, quoted so that request input cannot address another resource."""
    return quote(value, safe="")
