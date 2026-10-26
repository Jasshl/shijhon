"""AudioMuse-AI's HTTP API, as Shijhon uses it (v3.6 and later).

Every request carries the bearer token and the ``server`` that names this Shijhon among
AudioMuse-AI's media servers, so the song IDs on both sides are Navidrome's. A failure is a
``SoundUnavailable`` with one of a few fixed messages: never what the other side answered,
never the address, never the token.

The connection goes through the network policy (``delivery.netpolicy``): AudioMuse-AI
usually runs on this machine or the local network, which the ``reach`` setting allows
explicitly. At most ``MAX_AT_ONCE`` requests are under way at once, each bounded in time.
"""

from __future__ import annotations

import json as jsonlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from shijhon.delivery import netpolicy
from shijhon.playlists.sound import SoundUnavailable

log = logging.getLogger(__name__)

MAX_AT_ONCE = 4
MAX_BODY_BYTES = 32 * 1024 * 1024
# AudioMuse-AI runs on gunicorn, whose request line stops at 4094 bytes: song IDs are asked
# for in groups that fit.
IDS_PER_REQUEST = 100
MANIFEST_PAGE = 1000  # the most it lists on a page (app_sync.py: _MAX_MANIFEST_LIMIT)
MAX_RESULTS = 200  # what its own pages offer at most (config.py: ALCHEMY_MAX_N_RESULTS)
MAX_PATH_STEPS = 200  # config.py: PATH_MAX_LENGTH

NOT_SET_UP = "AudioMuse-AI is not set up."
UNREACHABLE = "AudioMuse-AI cannot be reached."
SLOW = "AudioMuse-AI took too long to answer."
DENIED = "The network setting does not allow this address."
TOKEN_MOVED = (
    "The token was entered for another address: enter it again on the Sound analysis page."  # noqa: S105
)
TOKEN_REFUSED = "AudioMuse-AI did not accept the token."  # noqa: S105 (not a password)
NOT_READY = "AudioMuse-AI cannot answer yet (is the analysis finished?)."
REFUSED = "AudioMuse-AI refused the request."
UNREADABLE = "AudioMuse-AI sent an answer Shijhon cannot read."
FAILED = "AudioMuse-AI failed to answer."
TOO_OLD = "This AudioMuse-AI does not offer what Shijhon asks (it needs version 3.6 or later)."


@dataclass(frozen=True)
class Track:
    """One song of AudioMuse-AI's ``/api/sync`` payload (a row of its ``score`` table).
    ``mood_vector`` and ``other_features`` are its ``label:value,label:value`` texts."""

    id: str
    fp: str
    tempo: Any
    key: Any
    scale: Any
    energy: Any
    mood_vector: Any
    other_features: Any


@dataclass(frozen=True)
class Concepts:
    """The words ``/api/clap/concepts`` allows for steering a search."""

    available: bool
    terms: tuple[str, ...]
    most: int


class NotFound(Exception):
    """AudioMuse-AI has no such song (or no path, no neighbours)."""


class Refused(SoundUnavailable):
    """AudioMuse-AI refused the request itself (HTTP 400, or a login page instead of an
    answer)."""


class AudioMuseClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        server: str = "",
        reach: netpolicy.Reach = netpolicy.Reach.PUBLIC,
        timeout: float = 20.0,
        resolver: netpolicy.Resolver = netpolicy.system_resolver,
        http: httpx.AsyncClient | None = None,
        token_withheld: bool = False,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        # A token exists that was entered for another address (dashboard.saved.bound_token):
        # it is not sent here, and nothing is asked until it is entered again.
        self._withheld = token_withheld
        self._server = server
        self._timeout = timeout
        self._slots = anyio.Semaphore(MAX_AT_ONCE)
        self._http = http or netpolicy.policy_client(
            reach, timeout=httpx.Timeout(timeout), resolver=resolver
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(server={self._server!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- the calls ---------------------------------------------------------------------

    async def version(self) -> str:
        """``/api/version``: needs the token, so it shows that the token works too."""
        body = await self._call("GET", "/api/version", within=min(self._timeout, 5.0))
        value = body.get("app_version") if isinstance(body, dict) else None
        return value if isinstance(value, str) else ""

    async def manifest(self, page: int) -> tuple[list[tuple[str, str]], bool]:
        """``/api/sync?fields=index``: a page of (song ID, fingerprint of its analysis), and
        whether there is a next page. The fingerprint changes when the analysis does."""
        body = await self._call(
            "GET", "/api/sync", {"fields": "index", "limit": MANIFEST_PAGE, "page": page}
        )
        rows = _rows(body, "tracks")
        found = []
        for row in rows:
            ident, fp = _text(row.get("id")), _text(row.get("fp"))
            if ident and fp:
                found.append((ident, fp))
        return found, body.get("has_more") is True

    async def tracks(self, ids: Sequence[str]) -> list[Track]:
        """``/api/sync?ids=``: the analysis of up to ``IDS_PER_REQUEST`` songs, without the
        embeddings. Songs it does not have are missing from the answer."""
        if not ids:
            return []
        if len(ids) > IDS_PER_REQUEST:
            raise ValueError("too many songs for one request")
        body = await self._call(
            "GET", "/api/sync", {"ids": ",".join(ids), "include_embeddings": "false"}
        )
        found = []
        for row in _rows(body, "tracks"):
            ident = _text(row.get("id"))
            if ident:
                found.append(
                    Track(
                        ident,
                        _text(row.get("fp")),
                        row.get("tempo"),
                        row.get("key"),
                        row.get("scale"),
                        row.get("energy"),
                        row.get("mood_vector"),
                        row.get("other_features"),
                    )
                )
        return found

    async def similar_tracks(self, song_id: str, count: int) -> list[str]:
        """``/api/similar_tracks``: the songs closest to one song, nearest first."""
        try:
            body = await self._call(
                "GET",
                "/api/similar_tracks",
                {"item_id": song_id, "n": _bounded(count)},
                none_if_absent=True,
            )
        except NotFound:
            return []
        if not isinstance(body, list):
            raise SoundUnavailable(UNREADABLE)
        return _ids(body)

    async def alchemy(self, add: Sequence[str], subtract: Sequence[str], count: int) -> list[str]:
        """``/api/alchemy``: songs near the ADD songs' centroid and away from the SUBTRACT
        songs. ``temperature`` 0 makes it a plain nearest-first ranking (otherwise it samples)."""
        items = [{"id": i, "op": "ADD", "type": "song"} for i in add]
        items += [{"id": i, "op": "SUBTRACT", "type": "song"} for i in subtract]
        try:
            body = await self._call(
                "POST",
                "/api/alchemy",
                json={"items": items, "n": _bounded(count), "temperature": 0},
                none_if_absent=True,
            )
        except NotFound:
            return []
        return _ids(_rows(body, "results"))

    async def search(
        self, text: str, count: int, steering: Sequence[Mapping[str, str]] = ()
    ) -> list[str]:
        """``/api/clap/search``: songs whose sound matches the words, best first; ``steering``
        is a list of ``{"term", "direction"}`` from ``concepts()``."""
        payload: dict[str, Any] = {"query": text, "limit": _bounded(count)}
        if steering:
            payload["steering"] = [dict(item) for item in steering]
        try:
            body = await self._call("POST", "/api/clap/search", json=payload, none_if_absent=True)
        except Refused:
            if steering:  # (it refuses a steering list it holds no valid term of)
                return await self.search(text, count)
            raise
        except NotFound:
            return []
        return _ids(_rows(body, "results"))

    async def concepts(self) -> Concepts:
        """``/api/clap/concepts``: the steering vocabulary (``available`` false: none)."""
        body = await self._call("GET", "/api/clap/concepts")
        if not isinstance(body, dict):
            raise SoundUnavailable(UNREADABLE)
        terms: list[str] = []
        categories = body.get("categories")
        for category in categories if isinstance(categories, list) else ():
            members = category.get("terms") if isinstance(category, dict) else None
            for member in members if isinstance(members, list) else ():
                term = member.get("term") if isinstance(member, dict) else None
                if isinstance(term, str) and term:
                    terms.append(term)
        most = body.get("max_terms")
        return Concepts(
            body.get("available") is True,
            tuple(terms),
            most if isinstance(most, int) and most > 0 else 10,
        )

    async def path(self, start: str, end: str, steps: int) -> list[str]:
        """``/api/find_path``: songs moving gradually from one to the other, both included
        (``max_steps`` is the whole path's length)."""
        try:
            body = await self._call(
                "GET",
                "/api/find_path",
                {
                    "start_song_id": start,
                    "end_song_id": end,
                    "max_steps": max(2, min(steps, MAX_PATH_STEPS)),
                },
                none_if_absent=True,
            )
        except NotFound:
            return []
        return _ids(_rows(body, "path"))

    # --- the request --------------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        json: Any = None,
        within: float | None = None,
        none_if_absent: bool = False,
    ) -> Any:
        """One request; its JSON answer. Fixed messages only: ``SoundUnavailable``, except
        for a 404 where ``none_if_absent`` says it means "nothing found" (``NotFound``)."""
        if self._withheld:
            raise SoundUnavailable(TOKEN_MOVED)
        query = dict(params or {})
        if self._server:
            if json is not None:
                json = {**json, "server": self._server}
            else:
                query["server"] = self._server
        headers = {"accept": "application/json"}
        if self._token:
            headers["authorization"] = "Bearer " + self._token
        limit = within or self._timeout
        try:
            with anyio.fail_after(limit):  # (waiting for a turn counts)
                async with self._slots:
                    return await self._send(
                        method, self._url + path, query, json, headers, none_if_absent
                    )
        except (TimeoutError, httpx.TimeoutException):
            raise SoundUnavailable(SLOW) from None
        except (httpx.HTTPError, OSError) as exc:
            raise SoundUnavailable(DENIED if netpolicy.denied(exc) else UNREACHABLE) from None

    async def _send(
        self,
        method: str,
        url: str,
        query: Mapping[str, Any],
        json: Any,
        headers: Mapping[str, str],
        none_if_absent: bool,
    ) -> Any:
        request = self._http.build_request(method, url, params=query, json=json, headers=headers)
        # (No redirects: a login page is not an answer, and the token goes nowhere else.)
        response = await self._http.send(request, stream=True, follow_redirects=False)
        try:
            status = response.status_code
            if status == 404:
                if none_if_absent:
                    raise NotFound
                raise SoundUnavailable(TOO_OLD)
            if status in (401, 403):
                raise SoundUnavailable(TOKEN_REFUSED)
            if status in (502, 503, 504):
                raise SoundUnavailable(NOT_READY)
            if status >= 500:
                raise SoundUnavailable(FAILED)
            if status != 200:  # (a refusal, a redirect to its login page)
                raise Refused(REFUSED)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > MAX_BODY_BYTES:
                    raise SoundUnavailable(UNREADABLE)
        finally:
            await response.aclose()
        try:
            return jsonlib.loads(data)
        except ValueError:
            raise SoundUnavailable(UNREADABLE) from None


def _bounded(count: int) -> int:
    return max(1, min(count, MAX_RESULTS))


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    return str(value) if isinstance(value, (str, int)) else ""


def _rows(body: Any, key: str) -> list[dict[str, Any]]:
    rows = body.get(key) if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise SoundUnavailable(UNREADABLE)
    return [row for row in rows if isinstance(row, dict)]


def _ids(rows: Sequence[Any]) -> list[str]:
    """The song IDs of result rows, in their order, each once."""
    found: dict[str, None] = {}
    for row in rows:
        ident = _text(row.get("item_id")) if isinstance(row, dict) else ""
        if ident:
            found.setdefault(ident)
    return list(found)
