"""Navidrome's own answer to a client's request, read in full so that Shijhon can add to it
(search results, artist pages, albums shown complete, their counts in lists) and pass it on
with its status and headers. A JSON answer is its document; an XML answer is read into the
same JSON-shaped document (``proxy/xmlform.py``), so every addition is made once, and
written back as XML with Navidrome's own elements as they came."""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio.to_thread
import httpx

from shijhon.proxy import xmlform
from shijhon.proxy.app import Reply
from shijhon.proxy.auth import Caller
from shijhon.proxy.params import RestCall, header
from shijhon.proxy.upstream import (
    Receive,
    Send,
    Upstream,
    request_headers,
    response_headers,
    send_bytes,
)

log = logging.getLogger(__name__)
# A selected view's check of library items: (kind - "song" or "album" -, IDs) -> those of
# them the caller's selection shows.
Shown = Callable[[str, Sequence[str]], Awaitable[frozenset[str]]]
# Headers that no longer describe a body Shijhon changed.
_CHANGED_BODY = {b"content-length", b"content-encoding", b"etag", b"last-modified"}
MIN_COMPRESSED = 1024  # smaller answers are sent as they are
IN_THREAD = 256 * 1024  # larger JSON answers are read and written in a worker thread
XML_IN_THREAD = 16 * 1024  # ... XML ones: reading and writing XML takes about 30 times longer


def accepts_gzip(call: RestCall) -> bool:
    """Whether the client takes a gzip-compressed answer (as Navidrome would send it):
    "gzip" or "*" without ``q=0``."""
    for part in header(call.headers, b"accept-encoding").lower().split(","):
        name, _, params = part.partition(";")
        if name.strip() not in ("gzip", "*"):
            continue
        q = params.strip().removeprefix("q=")
        try:
            if not params.strip().startswith("q=") or float(q) > 0:
                return True
        except ValueError:
            return True
    return False


def _dumps(document: dict[str, Any], xml: bool) -> bytes:
    """The document in the answer's format."""
    if xml:
        return xmlform.dumps(document)
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()


@dataclass
class _Reply:
    send_response: Reply
    selected_view: bool
    original_songs: Mapping[str, frozenset[str]] | None = None

    async def __call__(self, receive: Receive, send: Send) -> None:
        await self.send_response(receive, send)


@dataclass
class LibraryAnswer:
    """Navidrome's own answer to the client's request, read in full. ``selected_view``: the
    selected library's answer instead (``collection/visibility.py``); ``original_songs``:
    its album list's albums listed with all of their original songs, and those songs;
    ``shown``: for a selected search, which songs or albums the caller's selection shows
    (None: the answer is no selection's - nothing is hidden from it)."""

    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    selected_view: bool = False
    original_songs: Mapping[str, frozenset[str]] | None = None
    shown: Shown | None = field(default=None, repr=False, compare=False)
    _document: Any = field(default=None, init=False, repr=False, compare=False)
    _read: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def xml(self) -> bool:
        return "xml" in header(self.headers, b"content-type").lower()

    @property
    def _in_thread(self) -> bool:
        return len(self.body) >= (XML_IN_THREAD if self.xml else IN_THREAD)

    def _load(self) -> Any:
        return xmlform.load(self.body) if self.xml else json.loads(self.body)

    async def parsed(self, key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """(whole document, ``subsonic-response[key]``) of a successful answer (JSON, or
        XML read into the same shape). Read once, in a worker thread when large: later
        calls get the same document, with any changes made to it."""
        if self.status != 200:
            return None
        return _found(await self._loaded(), key)

    async def document(self) -> dict[str, Any] | None:
        """The whole document of a successful answer, read as ``parsed`` reads it (for an
        answer whose content is no single element: a list at its top)."""
        if self.status != 200:
            return None
        document = await self._loaded()
        response = document.get("subsonic-response") if isinstance(document, dict) else None
        ok = isinstance(response, dict) and response.get("status") == "ok"
        return document if ok else None

    async def _loaded(self) -> Any:
        if not self._read:
            try:
                if self._in_thread:
                    self._document = await anyio.to_thread.run_sync(self._load)
                else:
                    self._document = self._load()
            except ValueError:
                self._document = None
            self._read = True
        return self._document

    def reply(
        self, *, head: bool = False, document: dict[str, Any] | None = None, compress: bool = False
    ) -> Reply:
        """Navidrome's answer as it came, or with ``document`` as its body (in the answer's
        format, written in a worker thread when large); ``compress``: gzip-compressed (the
        client accepts it), unless small or already encoded."""
        headers = self.headers
        if document is not None:
            headers = [(k, v) for k, v in headers if k.lower() not in _CHANGED_BODY]
        encoded = any(k.lower() == b"content-encoding" for k, _ in headers)

        async def reply(receive: Receive, send: Send) -> None:
            out, sent = self.body, headers
            if document is not None and self._in_thread:
                out = await anyio.to_thread.run_sync(_dumps, document, self.xml)
            elif document is not None:
                out = _dumps(document, self.xml)
            if compress and not encoded and len(out) >= MIN_COMPRESSED:
                out = await anyio.to_thread.run_sync(gzip.compress, out, 5)
                sent = [(k, v) for k, v in headers if k.lower() not in (b"etag", b"vary")]
                sent += [(b"content-encoding", b"gzip"), (b"vary", b"Accept-Encoding")]
            await send_bytes(send, self.status, out, sent, head=head)

        return _Reply(reply, self.selected_view, self.original_songs)


def _found(document: Any, key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(the document, ``subsonic-response[key]``) of an "ok" answer."""
    response = document.get("subsonic-response") if isinstance(document, dict) else None
    if not isinstance(response, dict) or response.get("status") != "ok":
        return None
    found = response.get(key)
    return (document, found) if isinstance(found, dict) else None


async def captured(reply: Reply) -> LibraryAnswer:
    """A ready answer (a handler's ``Reply``) read in full, so that it can be changed; one
    compressed for the client is read uncompressed (compressed again when sent)."""
    status: int = 200
    headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status, headers = message["status"], list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body") or b""))

    await reply(receive, send)
    body = b"".join(chunks)
    if header(headers, b"content-encoding").lower() == "gzip":  # compressed for the client
        body = await anyio.to_thread.run_sync(gzip.decompress, body)
        drop = (b"content-encoding", b"content-length")
        headers = [(k, v) for k, v in headers if k.lower() not in drop]
    return LibraryAnswer(
        status,
        headers,
        body,
        selected_view=bool(getattr(reply, "selected_view", False)),
        original_songs=getattr(reply, "original_songs", None),
    )


async def library_answer(
    upstream: Upstream, call: RestCall, *, caller: Caller | None = None
) -> LibraryAnswer | None:
    if upstream.library_answer is not None:
        selected = await upstream.library_answer(call, caller=caller)
        if selected is not None:
            return selected
    return await raw_library_answer(upstream, call)


async def raw_library_answer(
    upstream: Upstream, call: RestCall, *, max_bytes: int | None = None
) -> LibraryAnswer | None:
    """The client's request as Navidrome answers it, uncompressed and read in full; None
    when Navidrome cannot be reached."""
    headers = [(k, v) for k, v in request_headers(call.headers) if k.lower() != b"accept-encoding"]
    try:
        response = await upstream.send(
            call.http_method, call.raw_path, call.query, headers, call.body
        )
        try:
            if max_bytes is None:
                body = await response.aread()
            else:
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("The library response exceeds its index budget.")
                    chunks.append(chunk)
                body = b"".join(chunks)
        finally:
            await response.aclose()
    except httpx.HTTPError as exc:
        log.warning("navidrome unreachable: %s", type(exc).__name__)
        return None
    headers = response_headers(response)
    return LibraryAnswer(response.status_code, headers, body)
