"""Subsonic responses Shijhon produces itself (errors, catalog views), shaped like
Navidrome's: HTTP 200 with a ``subsonic-response`` in the requested format (JSON, or XML
written from the same document, ``xmlform``)."""

from __future__ import annotations

import json
import re
from email.utils import formatdate
from typing import TYPE_CHECKING, Any

from shijhon.proxy import xmlform
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Receive, Send, send_bytes

if TYPE_CHECKING:  # the credential check writes answers too: no import cycle at run time
    from shijhon.proxy.app import Reply

VERSION = "1.16.1"
# Navidrome's check of a JSONP callback name (``validJSIdentifier``).
_CALLBACK = re.compile(r"[a-zA-Z_$][a-zA-Z0-9_$.]*")


def callback_refused(call: RestCall) -> bool:
    """A JSONP request whose callback name Navidrome refuses: it answers such a request,
    whatever its outcome, with its "invalid callback parameter" error."""
    return call.fmt == "jsonp" and not _CALLBACK.fullmatch(call.get("callback") or "")


def fields(status: str, server: dict[str, object] | None = None) -> dict[str, object]:
    """The envelope's fields in Navidrome's order, with its own values (version, type,
    serverVersion, openSubsonic) when known."""
    found: dict[str, object] = {"status": status, "version": VERSION, "type": "shijhon"}
    found.update({k: v for k, v in (server or {}).items() if k != "openSubsonic"})
    found["openSubsonic"] = (server or {}).get("openSubsonic", True)
    return found


def encoded(
    call: RestCall, status: str, payload: dict[str, Any], server: dict[str, object] | None = None
) -> tuple[bytes, bytes]:
    """(Content-Type, body) of an answer in the format asked for, as Navidrome writes it:
    JSON, JSONP (``callback(...)``; a callback name it refuses: its JSON error), or XML
    written from the same document."""
    envelope = fields(status, server)
    if call.fmt in ("json", "jsonp"):
        document = {"subsonic-response": {**envelope, **payload}}
        callback = call.get("callback") or ""
        if call.fmt == "jsonp" and not _CALLBACK.fullmatch(callback):
            error = {"code": 0, "message": "invalid callback parameter"}
            document = {"subsonic-response": {**fields("failed", server), "error": error}}
        body = json.dumps(document, separators=(",", ":")).encode()
        if call.fmt == "jsonp" and _CALLBACK.fullmatch(callback):
            return b"application/javascript", callback.encode() + b"(" + body + b")"
        return b"application/json", body
    return xmlform.CONTENT_TYPE, xmlform.answer(envelope, payload)


def _answer(call: RestCall, content_type: bytes, body: bytes) -> Reply:
    async def reply(receive: Receive, send: Send) -> None:
        await send_bytes(
            send, 200, body, [(b"content-type", content_type)], head=call.http_method == "HEAD"
        )

    return reply


def subsonic_error(
    call: RestCall, code: int, message: str, server: dict[str, object] | None = None
) -> Reply:
    """A Subsonic error in the format asked for, with Navidrome's envelope fields when
    known."""
    error = {"error": {"code": code, "message": message}}
    return _answer(call, *encoded(call, "failed", error, server))


def subsonic_ok(
    call: RestCall, payload: dict[str, Any], server: dict[str, object] | None = None
) -> Reply:
    """An answer in the format asked for - JSON, JSONP, or XML written from the same
    document - with Navidrome's envelope fields when known."""
    return _answer(call, *encoded(call, "ok", payload, server))


# Navidrome 0.64.2's answer to an endpoint it does not implement - jukeboxControl while its
# jukebox is off - headers and body as it sends them (captured in suite K).
NOT_IMPLEMENTED = b"This endpoint is not implemented, but may be in future releases"
_NOT_IMPLEMENTED_HEADERS = [
    (b"cache-control", b"no-cache"),
    (b"permissions-policy", b"autoplay=(), camera=(), microphone=(), usb=()"),
    (b"referrer-policy", b"same-origin"),
    (b"vary", b"Origin"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-type", b"text/plain; charset=utf-8"),
]


# What Navidrome 0.64.2 adds to its answer to a browser's request across origins (a request
# with an ``Origin`` header, by one of the methods its CORS settings name; any origin is allowed).
_CORS_METHODS = ("HEAD", "GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_CORS_HEADERS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-expose-headers", b"X-Content-Duration, X-Total-Count, X-Nd-Authorization"),
]


def not_implemented(call: RestCall) -> Reply:
    """Navidrome's "not implemented" (501), answered by Shijhon: for any HTTP method, with
    the CORS headers Navidrome adds for a request across origins."""
    named = any(key.lower() == b"origin" for key, _ in call.headers)  # even an empty one
    cors = named and call.http_method.upper() in _CORS_METHODS

    async def reply(receive: Receive, send: Send) -> None:
        date = formatdate(usegmt=True).encode()
        headers = [*(_CORS_HEADERS if cors else []), *_NOT_IMPLEMENTED_HEADERS, (b"date", date)]
        await send_bytes(send, 501, NOT_IMPLEMENTED, headers, head=call.http_method == "HEAD")

    return reply


def image(call: RestCall, data: bytes, content_type: str) -> Reply:
    async def reply(receive: Receive, send: Send) -> None:
        headers = [
            (b"content-type", content_type.encode()),
            (b"cache-control", b"public, max-age=86400"),
            # The bytes come from a catalog's image server: shown as the image they are
            # named, never read as another type (Navidrome's answers carry it too).
            (b"x-content-type-options", b"nosniff"),
        ]
        await send_bytes(send, 200, data, headers, head=call.http_method == "HEAD")

    return reply


def empty_answer(call: RestCall, key: str, server: dict[str, object] | None = None) -> Reply:
    """Navidrome's own answer when it has nothing for a read-only method (no lyrics, no
    similar songs): ``key`` as an empty element, in the requested format, with Navidrome's
    envelope in its order."""
    return _answer(call, *encoded(call, "ok", {key: {}}, server))
