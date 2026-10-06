"""Credentials first: before Shijhon does any work for a request — a catalog call,
an image fetch, a file write, a scan or an add-on request — Navidrome must accept the
request's credentials. Requests Shijhon merely forwards are authenticated by Navidrome.

The check is a ``getUser`` for the caller itself, which Navidrome answers as it would
answer the request itself up to the method: with the caller's own credentials, the
request's own ``v``, ``c``, ``f`` and ``callback``, and its client headers (the client's
address as the proxy names it; ``Origin``; the content coding of its
``Accept-Encoding``). Its answer names the user Navidrome authenticated - whichever ``u``
it read, in whatever letter case it was sent, or the reverse-proxy header's user - and
that is the caller's name: per-user limits and state count the same user Navidrome does. A
positive result is reused briefly, keyed by a hash of the credentials and that address.

A refusal - wrong credentials (also while Navidrome's login limit holds), or a request
without ``u``, ``v`` or ``c`` - is Navidrome's own answer, given before it looks at the
method: its status, headers and body, byte for byte. The request is answered with it
instead of being forwarded to fail a second time, because Navidrome counts every failure
toward its login limit of 5 in 20 seconds per client address and user. Refusals are
not cached: each request asks Navidrome once, as without Shijhon. Concurrent identical
checks share one request.

A check without a verdict is neither: Navidrome did not answer, or not with a Subsonic
answer (``CheckFailed``: the request gets a clean error, shared with the requests that
waited for that check), or the request that asked was canceled (those that waited ask
again). Never "refused", and never "forward it": a forwarded placeholder stream would
play its silence.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import anyio
import httpx

from shijhon.proxy.params import RestCall, header
from shijhon.proxy.responses import callback_refused
from shijhon.proxy.upstream import HOP_BY_HOP, Upstream

CREDENTIAL_PARAMS = ("u", "p", "t", "s", "jwt", "apiKey")
# The request's own parameters that shape Navidrome's answer: ``v`` and ``c`` (without
# them its error 10, before it looks at the credentials) and the format.
_SHAPE_PARAMS = ("v", "c", "f", "callback")
# Headers that shape it: who the client is - its address as Shijhon names it (the proxy sets
# these on every request), the reverse-proxy user header below - and what Navidrome's
# outer middlewares answer to: ``Origin`` (CORS headers), its web client's ID (a cookie).
# ``Accept-Encoding`` goes as the one coding Navidrome would choose for it (``_coding``).
_PASS_HEADERS = {
    b"host",
    b"user-agent",
    b"x-forwarded-for",
    b"x-real-ip",
    b"origin",
    b"x-nd-client-unique-id",
}
_SERVER_FIELDS = ("version", "type", "serverVersion", "openSubsonic")


@dataclass(frozen=True)
class Caller:
    username: str


@dataclass(frozen=True)
class Refusal:
    """Navidrome's answer to a check it refused, as it would answer the request itself:
    status, headers (hop-by-hop ones left out) and body as it sent them."""

    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes


Verdict = Caller | Refusal


class CheckFailed(Exception):
    """Navidrome gave no verdict on a request's credentials: it does not answer, or not
    with a Subsonic answer that accepts or refuses them."""


@dataclass
class _Shared:
    """A check under way; its verdict, or its failure, for the requests that wait for it
    (neither: the request that asked was canceled)."""

    done: anyio.Event
    verdict: Verdict | None = None
    failed: CheckFailed | None = None


# Request headers Navidrome's answer depends on besides the coding (the share of a refusal).
_ANSWER_HEADERS = (b"origin", b"x-nd-client-unique-id")


def _coding(call: RestCall) -> str:
    """The content coding Navidrome 0.64.2 chooses for the request's ``Accept-Encoding``
    (chi's rule: the first of gzip and deflate that any entry names, whatever its weight),
    "" for none: the check asks for exactly that one, so that its answer is the one the
    client would get, and one Shijhon can read."""
    accepted = header(call.headers, b"accept-encoding").lower().split(",")
    return next((c for c in ("gzip", "deflate") if any(c in part for part in accepted)), "")


def _names(material: list[tuple[str, str]]) -> list[bytes]:
    """The user names a request may be authenticated as, in Navidrome's order: the
    reverse-proxy header's (its bytes as they came: Navidrome reads them as UTF-8), then
    the first ``u`` (as Go reads a repeated parameter)."""
    header_names = [v.encode("latin-1") for k, v in material if k == "header"][:1]
    named = header_names + [v.encode() for k, v in material if k == "u"][:1]
    return [n for i, n in enumerate(named) if n and n.lower() not in [m.lower() for m in named[:i]]]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


class CredentialChecker:
    def __init__(
        self,
        upstream: Upstream,
        *,
        ttl: float = 60.0,
        proxy_user_header: str = "Remote-User",
        timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.upstream = upstream
        self.ttl = ttl
        self.timeout = timeout  # how long Navidrome may take to answer a check
        self.proxy_user_header = proxy_user_header.lower().encode()
        self.clock = clock
        self._cache: dict[str, tuple[float, Caller]] = {}
        self._inflight: dict[str, _Shared] = {}
        self.pings = 0  # observable in tests
        # Navidrome's envelope fields (version, type, serverVersion, openSubsonic), learned
        # from the last successful check: Shijhon's own answers look like Navidrome's.
        self.server: dict[str, object] = {}

    def _material(self, call: RestCall) -> list[tuple[str, str]]:
        material = [(k, v) for k, v in call.params if k in CREDENTIAL_PARAMS]
        proxy_user = header(call.headers, self.proxy_user_header)
        if proxy_user:
            material.append(("header", proxy_user))
        return material

    def _who(self, call: RestCall) -> tuple[list[Any], str] | None:
        """(who asks, the cache's key); None for a request without credentials."""
        material = self._material(call)
        if not material:
            return None
        # Per client address too: Navidrome may refuse one client's login (its limit counts
        # per address) while it accepts another's with the same credentials. And per
        # presence of ``v`` and ``c``: without them Navidrome refuses whatever the password.
        who = [material, header(call.headers, b"x-forwarded-for")]
        return who, _digest([who, bool(call.get("v")), bool(call.get("c"))])

    def known(self, call: RestCall) -> Caller | None:
        """The caller of a request whose credentials Navidrome accepted moments ago (the
        cache); None otherwise. Navidrome is not asked."""
        found = self._who(call)
        hit = self._cache.get(found[1]) if found is not None else None
        return hit[1] if hit and hit[0] > self.clock() else None

    async def check(self, call: RestCall) -> Verdict | None:
        """The caller Navidrome accepts, or its refusal; None when the request carries no
        credentials (nothing to check: Navidrome answers it). ``CheckFailed`` when
        Navidrome gave no verdict."""
        found = self._who(call)
        if found is None:
            return None
        who, key = found
        material: list[tuple[str, str]] = who[0]
        # One request for those asking the same at once: a refusal answers only requests
        # it fits byte for byte (its format, compression, CORS headers and cookie).
        shape = [call.get(k) for k in _SHAPE_PARAMS]
        sent = {k.lower(): v.decode("latin-1") for k, v in reversed(call.headers)}
        shape += [_coding(call), *(sent.get(k) for k in _ANSWER_HEADERS)]  # None: not sent
        ask = _digest([who, shape])
        while True:
            hit = self._cache.get(key)
            if hit and hit[0] > self.clock():
                return hit[1]
            shared = self._inflight.get(ask)
            if shared is None:
                break
            await shared.done.wait()
            if shared.verdict is not None:
                return shared.verdict
            if shared.failed is not None:
                raise CheckFailed(str(shared.failed))
            # The request that asked was canceled: that is no verdict. Asked again.
        shared = self._inflight[ask] = _Shared(anyio.Event())
        try:
            shared.verdict = await self._ping(call, material)
            if isinstance(shared.verdict, Caller):
                self._cache[key] = (self.clock() + self.ttl, shared.verdict)
                self._prune()
            return shared.verdict
        except CheckFailed as exc:
            shared.failed = exc
            raise
        except Exception as exc:  # no verdict either: not asked again by each who waited
            shared.failed = CheckFailed(type(exc).__name__)
            raise
        finally:
            del self._inflight[ask]
            shared.done.set()

    async def _ping(self, call: RestCall, material: list[tuple[str, str]]) -> Verdict:
        params = [(k, v) for k, v in material if k != "header"]
        # A JSONP callback name Navidrome refuses hides its verdict (its answer is "invalid
        # callback" whatever the credentials): asked without the format.
        plain = callback_refused(call)
        shape = ("v", "c") if plain else _SHAPE_PARAMS
        params += [(k, v) for k in shape if (v := call.get(k)) is not None]
        headers = [(k, v) for k, v in call.headers if k.lower() in _PASS_HEADERS]
        if coding := _coding(call):
            headers.append((b"accept-encoding", coding.encode()))
        user = [(k, v) for k, v in call.headers if k.lower() == self.proxy_user_header]
        # Navidrome reads the reverse-proxy header's user first, when it takes it from
        # Shijhon; else the first ``u``. ``getUser`` answers only for the user it
        # authenticated (error 50 for another name): asked for each in turn.
        names = _names(material)
        for position, name in enumerate(names or [b""]):
            asked: list[tuple[str, str | bytes]] = [*params]
            if name:
                asked.append(("username", name))
            self.pings += 1
            answer, refusal = await self._ask(b"/rest/getUser", asked, headers + user)
            if answer["status"] == "ok":
                self.server = {k: answer[k] for k in _SERVER_FIELDS if k in answer}
                told = answer.get("user")
                username = told.get("username") if isinstance(told, dict) else None
                if not isinstance(username, str) or not username:
                    # No verdict: the caller is never the client's own word.
                    raise CheckFailed("Navidrome named no user")
                return Caller(username)
            error = answer["error"]
            if error.get("code") != 50:
                break
            if position + 1 == len(names):  # accepted, as someone else than it was asked for
                raise CheckFailed("Navidrome named no user")
        if "'username'" in str(error.get("message")):  # getUser's own complaint: no refusal
            raise CheckFailed("Navidrome named no user")
        if plain:
            # ... and answered as Navidrome answers that request: its "invalid callback"
            # answer, which it gives to any method - asked of one that needs no login.
            form = [(k, v) for k in ("f", "callback") if (v := call.get(k)) is not None]
            answer, refusal = await self._ask(b"/rest/getOpenSubsonicExtensions", form, headers)
            if answer["status"] != "failed":
                raise CheckFailed("Navidrome accepted a callback name it refuses")
        return refusal

    async def _ask(
        self,
        path: bytes,
        params: Sequence[tuple[str, str | bytes]],
        headers: list[tuple[bytes, bytes]],
    ) -> tuple[dict[str, Any], Refusal]:
        """Navidrome's answer: its ``subsonic-response`` and the whole of it as it came.
        ``CheckFailed`` when it gives none."""
        try:
            with anyio.fail_after(self.timeout):  # the proxy's own requests wait without end
                response = await self.upstream.send(
                    "GET", path, urlencode(params).encode(), headers, None
                )
                try:
                    body = b"".join([chunk async for chunk in response.aiter_raw()])
                finally:
                    await response.aclose()
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, TimeoutError) as exc:
            raise CheckFailed(f"Navidrome does not answer ({type(exc).__name__})") from None
        answer = subsonic_answer(response, body)
        if answer is None:
            raise CheckFailed(f"no Subsonic answer from Navidrome (HTTP {response.status_code})")
        kept = [(k, v) for k, v in response.headers.raw if k.lower() not in HOP_BY_HOP]
        return answer, Refusal(response.status_code, kept, body)

    def _prune(self) -> None:
        if len(self._cache) > 4096:
            now = self.clock()
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}


def subsonic_answer(response: httpx.Response, body: bytes) -> dict[str, Any] | None:
    """The ``subsonic-response`` of a Navidrome answer as it came (``body``: not decoded) -
    its envelope and, when failed, its error - in any of its formats (JSON, JSONP, XML);
    None when it is no such answer."""
    if response.status_code != 200:
        return None
    coding = response.headers.get("content-encoding", "").strip().lower()
    kind = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    try:
        if coding == "gzip":
            body = gzip.decompress(body)
        elif coding == "deflate":  # Navidrome's (chi's) is a raw stream
            body = zlib.decompress(body, -zlib.MAX_WBITS)
        elif coding not in ("", "identity"):
            return None
        if kind == "application/json":
            found = json.loads(body)["subsonic-response"]
        elif kind == "application/javascript":  # callback({...})
            text = body.decode()
            found = json.loads(text[text.index("(") + 1 : text.rindex(")")])["subsonic-response"]
        elif kind in ("application/xml", "text/xml"):
            root = ElementTree.fromstring(body)  # noqa: S314 - Navidrome's own answer
            if root.tag.rpartition("}")[2] != "subsonic-response":
                return None
            found = dict(root.attrib)
            if "openSubsonic" in found:
                found["openSubsonic"] = found["openSubsonic"] == "true"
            user = next((e for e in root if e.tag.rpartition("}")[2] == "user"), None)
            if user is not None:
                found["user"] = dict(user.attrib)
            error = next((e for e in root if e.tag.rpartition("}")[2] == "error"), None)
            if error is not None:
                code, message = int(error.get("code", "0")), error.get("message", "")
                found["error"] = {"code": code, "message": message}
        else:
            return None
    except (ValueError, KeyError, TypeError, OSError, EOFError, zlib.error, ElementTree.ParseError):
        return None
    if not isinstance(found, dict):
        return None
    failed = found.get("status") == "failed" and isinstance(found.get("error"), dict)
    return found if failed or found.get("status") == "ok" else None
