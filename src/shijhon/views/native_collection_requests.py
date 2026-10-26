"""Observe the pinned native web app's explicit, ordered playlist song additions."""

from __future__ import annotations

import json
import logging
import re
import zlib
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx

from shijhon.collection.contracts import CollectionError
from shijhon.collection.history import MAX_PENDING
from shijhon.collection.native import bearer
from shijhon.collection.reader import CallerLibrary
from shijhon.proxy.app import ProxyApp
from shijhon.proxy.params import header, parse_pairs
from shijhon.proxy.upstream import (
    Receive,
    Scope,
    Send,
    read_body,
    relay,
    request_headers,
    response_headers,
    send_bytes,
    send_stream,
    stream_request_body,
)
from shijhon.views.collection_requests import CollectionRequests, _actor, _matches

log = logging.getLogger(__name__)
_ROUTE = re.compile(rb"/api/playlist/[A-Za-z0-9_-]{1,128}/tracks")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_BODY = 128 * 1024
_RESPONSE = 128 * 1024
_READ = 4 * 1024 * 1024


def _json(body: bytes, headers: list[tuple[bytes, bytes]]) -> Any:
    coding = header(headers, b"content-encoding").lower()
    if coding in ("gzip", "deflate"):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if coding == "gzip" else -zlib.MAX_WBITS)
        body = decoder.decompress(body, _READ + 1)
        if not decoder.eof:
            raise ValueError
    elif coding not in ("", "identity"):
        raise ValueError
    if len(body) > _READ:
        raise ValueError
    return json.loads(body)


class NativeCollectionRequests:
    def __init__(self, requests: CollectionRequests, reader: CallerLibrary) -> None:
        self.requests, self.reader, self.upstream = requests, reader, requests.upstream

    def attach(self, proxy: ProxyApp) -> None:
        # Register after NativeVisibility: it declines writes, then this more
        # specific prefix sees only the native playlist publication route.
        proxy.path_interceptors["/api/playlist/"] = self.intercept

    async def _get(self, path: bytes, token: str, query: bytes = b"") -> Any:
        response = await self.upstream.send(
            "GET", path, query, [(b"x-nd-authorization", ("Bearer " + token).encode())], None
        )
        try:
            if response.status_code != 200:
                raise CollectionError("The native playlist could not be verified.")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _READ:
                    raise CollectionError("The native playlist exceeds its verification budget.")
                body.extend(chunk)
            return json.loads(body)
        finally:
            await response.aclose()

    async def _songs(self, owner: str, ids: Sequence[str]) -> dict[str, Any]:
        songs = {}
        limiter = anyio.CapacityLimiter(4)

        async def one(ident: str) -> None:
            async with limiter:
                songs[ident] = await self.reader.song(owner, self.requests.library_id, ident)

        async with anyio.create_task_group() as group:
            for ident in dict.fromkeys(ids):
                group.start_soon(one, ident)
        return songs

    async def intercept(self, scope: Scope, receive: Receive, send: Send) -> bool:
        path = scope.get("raw_path") or scope["path"].encode()
        headers = list(scope["headers"])
        if scope["method"] != "POST" or not _ROUTE.fullmatch(path):
            return False
        try:
            length = int(header(headers, b"content-length"))
        except ValueError:
            return False
        if (
            not 0 < length <= _BODY
            or header(headers, b"content-type").split(";", 1)[0] != "application/json"
        ):
            return False
        try:
            with anyio.fail_after(10):
                body, ended = await read_body(receive, _BODY)
        except TimeoutError:
            await send_bytes(send, 408, b"Native playlist request timed out.\n", [])
            return True

        async def remaining() -> AsyncIterator[bytes]:
            yield body
            async for chunk in stream_request_body(receive):
                yield chunk

        query = scope.get("query_string", b"")
        if not ended:
            try:
                partial_response = await self.upstream.send(
                    "POST", path, query, request_headers(headers), remaining()
                )
            except httpx.HTTPError:
                await send_bytes(send, 502, b"Native playlist request unavailable.\n", [])
                return True
            await relay(partial_response, receive, send)
            return True

        # After consuming a body, this adapter always forwards it itself exactly
        # once, including unknown/malformed forms and refused credentials.
        # Up to 500 songs are confirmed with the request itself; a larger addition is kept
        # to record from the ledger once Navidrome added every one (``large``).
        ids: list[str] = []
        large: list[str] = []
        try:
            data = json.loads(body)
            if isinstance(data, dict) and set(data) == {"ids"}:
                values = data["ids"]
                if (
                    isinstance(values, list)
                    and 0 < len(values) <= MAX_PENDING
                    and all(isinstance(value, str) and _ID.fullmatch(value) for value in values)
                ):
                    if len(values) <= 500:
                        ids = values
                    else:
                        large = values
        except ValueError:
            pass
        try:
            tracks = (
                (await self.requests._tracks([("songId", ident) for ident in ids]) or [])
                if ids
                else []
            )
        except Exception as exc:
            tracks = []
            log.warning("native playlist applicability skipped: %s", type(exc).__name__)
        principal, token, before = None, "", -1
        if large:
            try:
                with anyio.fail_after(10):
                    token = bearer(headers, parse_pairs(query))
                    principal = await self.reader.access.verify_native(token)
                    if not principal.can_access(self.reader.library_id):
                        principal = None
            except Exception as exc:
                principal = None
                log.info("native playlist attribution precheck skipped: %s", type(exc).__name__)
        if tracks:
            try:
                with anyio.fail_after(10):
                    token = bearer(headers, parse_pairs(query))
                    principal = await self.reader.access.verify_native(token)
                    if not principal.can_access(self.reader.library_id):
                        raise CollectionError("The playlist uses another library.")
                    self.reader.remember(principal, token)
                    # Prove the actual native songs belong to this library before
                    # attributing anything. Other libraries retain raw semantics.
                    await self._songs(principal.user_id, ids)
                    playlist = await self._get(path.removesuffix(b"/tracks"), token)
                    before = playlist.get("songCount", -1)
                    if type(before) is not int or not 0 <= before <= 10_000_000:
                        raise ValueError
            except Exception as exc:
                principal = None
                log.info("native playlist attribution precheck skipped: %s", type(exc).__name__)

        response = None
        try:
            chunks, size, overflow = [], 0, False
            try:
                with anyio.fail_after(10):
                    response = await self.upstream.send(
                        "POST", path, query, request_headers(headers), body
                    )
                    if principal is not None:
                        iterator = response.aiter_raw()
                        async for chunk in iterator:
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > _RESPONSE:
                                overflow = True
                                break
            except (TimeoutError, httpx.HTTPError):
                # An uncertain remote mutation is never retried.
                await send_bytes(send, 502, b"Native playlist response unavailable.\n", [])
                return True
            if principal is None:
                await relay(response, receive, send)
                return True
            if overflow:

                async def rest() -> AsyncIterator[bytes]:
                    for kept in chunks:
                        yield kept
                    async for kept in iterator:
                        yield kept

                await send_stream(
                    receive,
                    send,
                    response.status_code,
                    response_headers(response),
                    rest(),
                    response.aclose,
                )
                return True
            raw, kept_headers = b"".join(chunks), response_headers(response)
            if large and principal is not None and response.status_code == 200:
                try:
                    result = _json(raw, kept_headers)
                    if isinstance(result, dict) and result.get("added") == len(large):
                        await self.requests._queue(
                            principal.user_id, [("songId", ident) for ident in large]
                        )
                except Exception as exc:
                    log.warning("native playlist attribution skipped: %s", type(exc).__name__)
            elif principal is not None and response.status_code == 200:
                stage = "response"
                try:
                    with anyio.fail_after(10):
                        result = _json(raw, kept_headers)
                        if not isinstance(result, dict) or result.get("added") != len(ids):
                            raise ValueError
                        stage = "count"
                        playlist = await self._get(path.removesuffix(b"/tracks"), token)
                        if playlist.get("songCount") != before + len(ids):
                            raise ValueError
                        stage = "order"
                        tail = await self._get(
                            path,
                            token,
                            urlencode({"_start": before, "_end": before + len(ids)}).encode(),
                        )
                        if (
                            not isinstance(tail, list)
                            or [row.get("mediaFileId") for row in tail] != ids
                        ):
                            raise ValueError
                        stage = "recording"
                        native_ids = {
                            str(track.ref): await self.requests.commits.materialized.song(track.ref)
                            for track in tracks
                        }
                        songs = await self._songs(
                            principal.user_id, [ident or "" for ident in native_ids.values()]
                        )
                        for track in tracks:
                            if not _matches(track, songs[native_ids[str(track.ref)] or ""]):
                                raise ValueError
                        actor = await self.reader.native_actor(
                            principal.user_id, self.requests.library_id
                        )
                        if actor != _actor(principal):
                            raise ValueError
                        await self.requests.history.record_catalog(
                            actor, self.requests.library_id, tracks
                        )
                except Exception as exc:
                    # The mutation already happened. A failed confirmation must
                    # neither retry it nor change its original native answer.
                    log.warning(
                        "native playlist attribution skipped (%s): %s", stage, type(exc).__name__
                    )
            await send_bytes(send, response.status_code, raw, kept_headers)
        finally:
            if response is not None:
                await response.aclose()
        return True
