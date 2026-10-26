"""Navidrome 0.64.2 native/web library visibility, as the actual caller.

Generic resources accept only flat filters; they cannot join a personal collection.
Enumerations therefore use complete, bounded background query generations. Direct
IDs, explicit album track lists, playlist tracks, queues, scrobbles and all writes
retain upstream semantics.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from http.cookies import SimpleCookie
from importlib.resources import files
from typing import Any
from urllib.parse import urlencode

import httpx

from shijhon.access import AccessDenied, AccessUnavailable, Principal
from shijhon.collection.query_index import (
    Binding,
    IndexFailed,
    IndexPending,
    SetupRequired,
    SourcePage,
)
from shijhon.collection.visibility import Visibility, number, query_pairs
from shijhon.proxy.app import ProxyApp
from shijhon.proxy.params import header, parse_pairs, unreadable
from shijhon.proxy.responses import music_send
from shijhon.proxy.upstream import Receive, Scope, Send, request_headers, send_bytes

_RESOURCES = frozenset({"song", "album", "artist", "genre", "tag"})
_BODY_BYTES = 32 << 20


def bearer(headers: list[tuple[bytes, bytes]], query: list[tuple[str, str]]) -> str:
    value = header(headers, b"x-nd-authorization")
    if value[:7].lower() == "bearer ":
        return value[7:]
    cookie = SimpleCookie()
    try:
        cookie.load(header(headers, b"cookie"))
    except Exception:
        return ""
    if "jwt" in cookie:
        return cookie["jwt"].value
    return next((v for k, v in query if k == "jwt"), "")


class NativeVisibility:
    def __init__(
        self,
        visibility: Visibility,
        *,
        verified: Callable[[Principal, str], Awaitable[None]] | None = None,
    ) -> None:
        self.visibility = visibility
        self.verified = verified
        self.warmed: dict[str, float] = {}
        self.index, self.access, self.upstream = (
            visibility.index,
            visibility.access,
            visibility.upstream,
        )
        visibility.prepare_foreign = self.prepare_foreign

    def attach(self, proxy: ProxyApp) -> None:
        proxy.path_interceptors["/api/"] = self.intercept
        proxy.path_interceptors["/app"] = self.browser

    async def browser(self, scope: Scope, receive: Receive, send: Send) -> bool:
        """Add our own transport helper to native app HTML; no bundled JS changes."""
        path = scope.get("raw_path") or scope["path"].encode()
        if scope["method"] not in ("GET", "HEAD"):
            return False
        if path == b"/app/shijhon-library.js":
            await send_bytes(
                send,
                200,
                files("shijhon.collection").joinpath("native_client.js").read_bytes(),
                [(b"content-type", b"text/javascript"), (b"cache-control", b"no-cache")],
                head=scope["method"] == "HEAD",
            )
            return True
        if path not in (b"/app/", b"/app/index.html"):
            return False
        headers = [
            (k, v)
            for k, v in request_headers(list(scope["headers"]))
            if k.lower() not in (b"accept-encoding", b"if-none-match", b"if-modified-since")
        ]
        response = await self.upstream.send(
            "GET", path, scope.get("query_string", b""), headers, None
        )
        try:
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 256 << 10:
                    await send_bytes(
                        send,
                        502,
                        b"Native app HTML exceeds its response budget.\n",
                        [],
                        head=scope["method"] == "HEAD",
                    )
                    return True
                chunks.append(chunk)
            body = b"".join(chunks)
            if response.status_code == 200 and "text/html" in response.headers.get(
                "content-type", ""
            ):
                body = body.replace(
                    b"</head>", b'<script src="shijhon-library.js"></script></head>', 1
                )
            kept = [
                (k, v)
                for k, v in response.headers.raw
                if k.lower()
                not in (b"content-length", b"content-encoding", b"etag", b"last-modified")
            ]
            await send_bytes(send, response.status_code, body, kept, head=scope["method"] == "HEAD")
            return True
        finally:
            await response.aclose()

    async def intercept(self, scope: Scope, receive: Receive, send: Send) -> bool:
        raw = scope.get("raw_path") or scope["path"].encode()
        parts = raw.removeprefix(b"/api/").rstrip(b"/").split(b"/")
        if not parts or parts[0].decode("ascii", errors="replace") not in _RESOURCES:
            return False
        kind = parts[0].decode()
        if scope["method"] not in ("GET", "HEAD") or len(parts) > 2:
            return False
        if len(parts) == 2 and kind not in ("artist", "tag"):
            return False  # Direct albums, songs and genres retain native permissions.
        query = scope.get("query_string", b"")
        if unreadable(query):
            await self._send(
                music_send(scope["method"], list(scope["headers"]), send),
                scope,
                400,
                {"error": "Invalid library query."},
            )
            return True
        pairs = parse_pairs(query)
        album_ids = [v for k, v in pairs if k == "album_id"]
        if kind == "song" and any(album_ids):
            # Native album filters are multi-valued. Keep every explicit reference,
            # its order and paging; discovery needs neither selection nor an index.
            return False
        if any(k == "id" and v for k, v in pairs) and all(v for k, v in pairs if k == "id"):
            return False  # explicit getMany references, including queue/playlist hydration
        headers = list(scope["headers"])
        sending = music_send(scope["method"], headers, send)
        try:
            token = bearer(headers, pairs)
            principal = await self.access.verify_native(token)
            if not principal.can_access(self.access.navidrome.library_id):
                return False  # other granted native libraries remain Navidrome's
            if not header(headers, b"x-nd-authorization"):
                # Native also accepts a jwt query/cookie. Keep it out of normalized
                # queries while sending that same proven caller token upstream.
                headers.append((b"x-nd-authorization", ("Bearer " + token).encode()))
            values = dict(reversed(pairs))
            requested = frozenset(v for k, v in pairs if k == "library_id") or None
            if requested and str(self.access.navidrome.library_id) not in requested:
                return False
            if self.verified is not None:
                await self.verified(principal, token)
            binding = await self.visibility.binding(principal, requested)
            await self.prepare_foreign(binding, principal, token)
            mode = None
            warm_key = binding.key("native-prewarm")
            if (
                time.monotonic() - self.warmed.get(warm_key, -10000)
                > self.index.budgets.fresh_seconds
            ):
                self.warmed[warm_key] = time.monotonic()
                if len(self.warmed) > 128:
                    self.warmed.pop(next(iter(self.warmed)))

                async def warm() -> None:
                    with suppress(AccessDenied, AccessUnavailable, IndexPending, IndexFailed):
                        await self.prewarm(principal, token)

                self.index.spawn(warm)

            async def validate() -> None:
                current = await self.access.verify_native(token)
                if (
                    current != principal
                    or await self.visibility.binding(current, requested) != binding
                ):
                    raise AccessDenied

            if len(parts) == 2:
                ident = parts[1].decode("ascii", errors="replace")
                stats = await self.index.stats(binding, mode, kind, ident)
                if not stats:
                    return False  # complete native discovery, including its own 404
                data, response_headers = await self._json(raw, pairs, headers)
                if not isinstance(data, dict):
                    raise IndexFailed("The native detail response is invalid.")
                if stats.get("songCount") != data.get("songCount"):
                    data.update({k: v for k, v in stats.items() if k in data})
                detail_refreshed = [
                    (k, v) for k, v in response_headers if k.lower() == b"x-nd-authorization"
                ]
                await self._send(sending, scope, 200, data, detail_refreshed)
                return True
            start = number(values.get("_start"), 0)
            unbounded = "_end" not in values or (
                values["_end"].lstrip("-").isdigit() and int(values["_end"]) <= 0
            )
            end = start + 5000 if unbounded else number(values.get("_end"), start + 25)
            if end < start or end - start > 5000:
                await self._send(
                    sending, scope, 400, {"error": "Choose a library page of at most 5000 entries."}
                )
                return True
            normal = [(k, v) for k, v in pairs if k not in ("_start", "_end", "jwt")]
            random = values.get("_sort") == "random"
            if random:
                normal = [(k, v) for k, v in normal if k != "_sort"] + [("_sort", "id")]
            normal = [(k, v) for k, v in normal if k not in ("seed", "_order")]
            normal.append(
                (
                    "_order",
                    "DESC" if not random and values.get("_order", "").upper() == "DESC" else "ASC",
                )
            )
            if not any(k == "_sort" for k, _ in normal):
                normal.append(("_sort", "id"))

            refreshed: list[tuple[bytes, bytes]] = []

            async def fetch(offset: int, limit: int) -> SourcePage:
                data, response_headers = await self._json(
                    raw, [*normal, ("_start", str(offset)), ("_end", str(offset + limit))], headers
                )
                refreshed[:] = [
                    (k, v) for k, v in response_headers if k.lower() == b"x-nd-authorization"
                ]
                if not isinstance(data, list) or any(not isinstance(i, dict) for i in data):
                    raise IndexFailed("The native list response is invalid.")
                try:
                    total = int(header(response_headers, b"x-total-count"))
                except ValueError:
                    raise IndexFailed("The native list has no trustworthy total.") from None
                return SourcePage(data, total)

            lane = (
                principal.user_id + ":" + kind
                if any(k in ("name", "title") and v for k, v in normal)
                else ""
            )
            key = await self.index.ensure(
                binding,
                ("native", kind, query_pairs(normal)),
                fetch,
                validate,
                lane=lane,
                wait=not scope.get("shijhon.prewarm", False),
                page_size=self.index.budgets.native_page_size,
            )
            if scope.get("shijhon.prewarm"):
                return True  # source warming needs no count/page/aggregate hydration
            album = values.get("album_id", "") if kind == "song" else ""
            count_sort = values.get("_sort", "")
            prefix = "maincredit_" if count_sort.startswith("maincredit_") else ""
            count_sort = prefix + {"song_count": "songCount", "album_count": "albumCount"}.get(
                count_sort.removeprefix(prefix) if prefix else count_sort,
                count_sort.removeprefix(prefix) if prefix else count_sort,
            )
            page = await self.index.page(
                key,
                binding,
                "tag" if kind == "genre" else kind,
                mode,
                start,
                end - start,
                random=random,
                random_seed=values.get("seed", ""),
                count_sort=count_sort,
                descending=values.get("_order", "").lower() == "desc",
                direct_album=album,
                artist_role=values.get("role", ""),
                preserve_album_metadata=kind == "album",
            )
            if unbounded and page.total > 5000:
                await self._send(
                    sending,
                    scope,
                    413,
                    {"error": "This library result requires pagination.", "total": page.total},
                )
                return True
            await self._send(
                sending,
                scope,
                200,
                page.rows,
                [
                    *refreshed,
                    (b"x-total-count", str(page.total).encode()),
                    (b"x-shijhon-indexed-at", str(page.indexed_at).encode()),
                ],
            )
            return True
        except AccessDenied:
            await self._send(
                sending, scope, 401, {"error": "Not authenticated or library access revoked."}
            )
        except IndexPending as exc:
            await self._send(
                sending,
                scope,
                503,
                {
                    "message": str(exc),
                    "error": str(exc),
                    "state": "indexing",
                    "indexed": exc.indexed,
                    "total": exc.total,
                },
                [(b"retry-after", b"1"), (b"x-shijhon-view-state", b"indexing")],
            )
        except SetupRequired as exc:
            await self._send(sending, scope, 503, {"error": str(exc), "message": str(exc)})
        except (
            AccessUnavailable,
            IndexFailed,
            httpx.HTTPError,
            ValueError,
            sqlite3.Error,
            OSError,
        ):
            await self._send(
                sending,
                scope,
                503,
                {"error": "The selected library view is unavailable. Retry shortly."},
                [(b"retry-after", b"1")],
            )
        return True

    async def prepare_foreign(
        self, binding: Binding, principal: Principal, token: str | None
    ) -> None:
        """Index only caller-authorized foreign structure; never touch the music source."""
        if not binding.foreign:
            return
        if not token:
            if await self.index.foreign_ready(binding):
                return
            raise SetupRequired
        if await self.access.verify_native(token) != principal:
            raise AccessDenied
        params = [
            ("_sort", "id"),
            ("_order", "ASC"),
            ("missing", "false"),
            *(("library_id", library) for library in sorted(binding.foreign)),
        ]
        headers = [(b"x-nd-authorization", ("Bearer " + token).encode())]

        async def fetch(offset: int, limit: int) -> SourcePage:
            data, returned = await self._json(
                b"/api/song",
                [*params, ("_start", str(offset)), ("_end", str(offset + limit))],
                headers,
            )
            if not isinstance(data, list):
                raise IndexFailed("The native source is invalid.")
            try:
                total = int(header(returned, b"x-total-count"))
            except ValueError:
                raise IndexFailed("The native source has no trustworthy total.") from None
            return SourcePage(data, total)

        async def validate() -> None:
            current = await self.access.verify_native(token)
            if (
                current != principal
                or await self.visibility.binding(current, binding.libraries) != binding
            ):
                raise AccessDenied

        await self.index.ensure(
            binding,
            ("native-foreign-source", sorted(binding.foreign)),
            fetch,
            validate,
            structural=True,
            page_size=self.index.budgets.native_page_size,
        )

    async def _json(
        self, path: bytes, pairs: list[tuple[str, str]], headers: list[tuple[bytes, bytes]]
    ) -> tuple[Any, list[tuple[bytes, bytes]]]:
        # Exact caller auth/header material, no service-account substitution. Encoding
        # and conditionals are removed because this request builds a complete generation.
        kept = [
            (k, v)
            for k, v in request_headers(headers)
            if k.lower() not in (b"accept-encoding", b"if-none-match", b"if-modified-since")
        ]
        response = await self.upstream.send("GET", path, urlencode(pairs).encode(), kept, None)
        try:
            size, chunks = 0, []
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _BODY_BYTES:
                    raise IndexFailed("The native library page exceeds its response budget.")
                chunks.append(chunk)
            if response.status_code != 200:
                if response.status_code in (401, 403):
                    raise AccessDenied
                raise IndexFailed("The native library query was refused.")
            try:
                data = json.loads(b"".join(chunks))
            except ValueError:
                raise IndexFailed("The native library query is not JSON.") from None
            return data, list(response.headers.raw)
        finally:
            await response.aclose()

    async def _send(
        self,
        send: Send,
        scope: Scope,
        status: int,
        data: Any,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        await send_bytes(
            send,
            status,
            json.dumps(data, separators=(",", ":")).encode(),
            [
                (b"content-type", b"application/json"),
                (b"cache-control", b"no-store"),
                *(headers or []),
            ],
            head=scope["method"] == "HEAD",
        )

    async def prewarm(self, principal: Principal, token: str) -> None:
        """Bounded common browsing warmup; call on verified connection/collection refresh."""
        if await self.access.verify_native(token) != principal:
            raise AccessDenied
        for resource, sort in (
            ("album", "recently_added"),
            ("album", "name"),
            ("song", "title"),
            ("artist", "name"),
            ("tag", "name"),
        ):
            scope: Scope = {
                "shijhon.prewarm": True,
                "type": "http",
                "method": "GET",
                "path": "/api/" + resource,
                "raw_path": ("/api/" + resource).encode(),
                "query_string": urlencode(
                    {
                        "_sort": sort,
                        "_order": "DESC" if sort == "recently_added" else "ASC",
                        "_start": 0,
                        "_end": 25,
                        "missing": "false",
                    }
                ).encode(),
                "headers": [(b"x-nd-authorization", ("Bearer " + token).encode())],
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.disconnect"}

            async def send(message: Any) -> None:
                pass

            await self.intercept(scope, receive, send)
