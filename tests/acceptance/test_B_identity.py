"""Suite B — who the client is, for Navidrome.

Navidrome 0.64.2 limits failed logins per client address and user name, reading the
address from forwarding headers only from its trusted sources, which then may also name
the user in a ``Remote-User`` header. Shijhon names each client's address itself and
drops what an untrusted client claims (its addresses, a user header), so that Shijhon can
be one of Navidrome's trusted sources.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, MutableMapping
from typing import Any
from urllib.parse import parse_qsl

import anyio
import httpx
import pytest

from shijhon.proxy.app import HandlerResult, ProxyApp, RequestContext
from shijhon.proxy.auth import Caller, CredentialChecker
from shijhon.proxy.forwarding import Forwarding
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Upstream
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance
from tests.harness.replay import Replay, fixture
from tests.harness.running import RunningServer

CLAIMS = {
    "X-Forwarded-For": "198.51.100.66",
    "X-Real-IP": "198.51.100.66",
    "True-Client-IP": "198.51.100.66",
    "Forwarded": "for=198.51.100.66",
    "Remote-User": "admin",
    "X-Forwarded-Proto": "https",
}
CREDENTIALS = {"u": "listener", "p": "listener-password", "v": "1.16.1", "c": "tests", "f": "json"}


class Capture:
    """A stand-in for Navidrome that records every request reaching it and accepts every
    credential check."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, list[str]]]] = []

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        headers: dict[str, list[str]] = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.decode().lower(), []).append(value.decode())
        self.seen.append((scope["path"], headers))
        answer: dict[str, Any] = {"status": "ok", "version": "1.16.1", "type": "navidrome"}
        query = dict(parse_qsl(scope["query_string"].decode()))
        if scope["path"] == "/rest/getUser":
            answer["user"] = {"username": query.get("username", "")}
        body = json.dumps({"subsonic-response": answer}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def headers(self, path: str) -> dict[str, list[str]]:
        return next(h for p, h in self.seen if p == path)


def proxy_in_front(capture: RunningServer, trusted: list[str], user_auth: bool) -> ProxyApp:
    upstream = Upstream(capture.base_url)

    async def checked(call: RestCall, ctx: RequestContext) -> HandlerResult:
        await ctx.caller()  # work for this request: its credentials are checked first
        return None

    return ProxyApp(
        upstream,
        CredentialChecker(upstream),
        handlers={"getAlbum": checked},
        forwarding=Forwarding(trusted, user_auth=user_auth),
    )


@pytest.fixture
def capture() -> Iterator[tuple[Capture, RunningServer]]:
    app = Capture()
    server = RunningServer(app, lifespan="off")
    server.start()
    yield app, server
    server.stop()


def requests_through(
    capture: tuple[Capture, RunningServer], trusted: list[str], *, user_auth: bool = False
) -> Capture:
    app, upstream = capture
    proxy = RunningServer(proxy_in_front(upstream, trusted, user_auth), lifespan="off")
    proxy.start()
    try:
        with httpx.Client(base_url=proxy.base_url, headers=CLAIMS) as http:
            http.get("/rest/getAlbum", params={**CREDENTIALS, "id": "1"})
            http.get("/rest/getSong", params={**CREDENTIALS, "id": "1"})
            http.get("/api/song")
    finally:
        proxy.stop()
    return app


def test_an_untrusted_client_claims_nothing(capture: tuple[Capture, RunningServer]) -> None:
    """From a peer that is no trusted proxy: the check, a forwarded call and a forwarded
    request to Navidrome's own API all name the peer, and carry no user header."""
    seen = requests_through(capture, trusted=[])
    paths = [path for path, _ in seen.seen]
    checks = [p for p in paths if p in ("/rest/ping", "/rest/getUser")]
    assert len(checks) == 1 and "/rest/getAlbum" in paths
    for path in {*paths}:
        headers = seen.headers(path)
        assert headers["x-forwarded-for"] == ["127.0.0.1"], path
        assert headers["x-real-ip"] == ["127.0.0.1"], path
        for dropped in ("true-client-ip", "forwarded", "remote-user"):
            assert dropped not in headers, (path, dropped)
    # What a proxy says about the request goes on: Navidrome reads it from anyone.
    assert seen.headers("/rest/getSong")["x-forwarded-proto"] == ["https"]


def test_a_trusted_proxy_names_the_client(capture: tuple[Capture, RunningServer]) -> None:
    """From a trusted proxy (loopback, the default): the client its X-Forwarded-For names,
    alone; its user header only with header sign-in on."""
    seen = requests_through(capture, trusted=["127.0.0.0/8"])
    for path in {path for path, _ in seen.seen}:
        headers = seen.headers(path)
        assert headers["x-forwarded-for"] == ["198.51.100.66"], path
        assert headers["x-real-ip"] == ["198.51.100.66"], path
        for dropped in ("true-client-ip", "forwarded", "remote-user"):
            assert dropped not in headers, (path, dropped)


def test_header_sign_in_from_a_trusted_proxy(capture: tuple[Capture, RunningServer]) -> None:
    seen = requests_through(capture, trusted=["127.0.0.0/8"], user_auth=True)
    for path in {path for path, _ in seen.seen}:
        assert seen.headers(path)["remote-user"] == ["admin"], path


@pytest.fixture(scope="module")
def trusting(navidrome_factory: NavidromeFactory) -> NavidromeInstance:
    """A Navidrome that trusts loopback, where Shijhon runs in these tests (as the
    recommended deployment trusts Shijhon's address)."""
    return navidrome_factory({"ND_EXTAUTH_TRUSTEDSOURCES": "127.0.0.1/32"})


PING = {"v": "1.16.1", "c": "tests", "f": "json"}


def pinged(base_url: str, headers: dict[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = httpx.get(f"{base_url}/rest/ping", params=PING, headers=headers).json()
    return body["subsonic-response"]


def test_a_client_cannot_sign_in_with_a_user_header(
    trusting: NavidromeInstance, shijhon_factory: Any
) -> None:
    """Navidrome takes a Remote-User header from Shijhon as the user it names: from a
    client, even one behind a trusted proxy, it never gets there - unless header sign-in is
    turned on, and then only from a trusted proxy."""
    header = {"Remote-User": ADMIN_USER}
    assert pinged(trusting.base_url, header)["status"] == "ok"  # Navidrome honors it

    for server in (
        {"trusted_proxies": []},
        {},
        {"trusted_proxies": [], "reverse_proxy_auth": True},
    ):
        refused = pinged(shijhon_factory(trusting, server=server).base_url, header)
        assert refused["status"] == "failed" and refused["error"]["code"] == 10, server  # no u

    signing_in = shijhon_factory(trusting, server={"reverse_proxy_auth": True})
    assert pinged(signing_in.base_url, header)["status"] == "ok"


def test_the_caller_is_the_user_of_the_header_navidrome_reads(
    trusting: NavidromeInstance,
) -> None:
    """Header sign-in: Navidrome takes the header's user, whatever ``u`` and ``p`` say. So
    does the caller's name."""
    trusting.create_user("listener", "listener-password")
    checker = CredentialChecker(Upstream(trusting.base_url))
    query = f"u={ADMIN_USER}&p=wrong&v=1.16.1&c=tests".encode()
    headers = [(b"Remote-User", b"LISTENER")]
    call = RestCall.build("getAlbum", "GET", b"/rest/getAlbum", query, headers, None)

    async def main() -> Any:
        try:
            return await checker.check(call)
        finally:
            await checker.upstream.aclose()

    assert anyio.run(main) == Caller("listener") and checker.pings == 1


def test_a_header_user_with_letters_beyond_ascii(trusting: NavidromeInstance) -> None:
    """A header's bytes are the name as Navidrome reads it (UTF-8), also when asking it who
    that is."""
    trusting.create_user("Jörg", "another-password")
    checker = CredentialChecker(Upstream(trusting.base_url))
    headers = [(b"Remote-User", "Jörg".encode())]
    call = RestCall.build("getAlbum", "GET", b"/rest/getAlbum", b"v=1.16.1&c=tests", headers, None)

    async def main() -> Any:
        try:
            return await checker.check(call)
        finally:
            await checker.upstream.aclose()

    assert anyio.run(main) == Caller("Jörg") and checker.pings == 1


def test_navidromes_own_user_header_is_dropped(
    navidrome_factory: NavidromeFactory, shijhon_factory: Any
) -> None:
    """A user header Navidrome reads under another name than Shijhon's setting: learned from
    Navidrome's configuration at startup, before the first request goes on, and dropped from
    every request."""
    nd = navidrome_factory(
        {"ND_EXTAUTH_TRUSTEDSOURCES": "127.0.0.1/32", "ND_EXTAUTH_USERHEADER": "X-Auth-User"}
    )
    header = {"X-Auth-User": ADMIN_USER}
    assert pinged(nd.base_url, header)["status"] == "ok"
    shijhon = shijhon_factory(nd)  # its first requests wait until that is read
    refused = pinged(shijhon.base_url, header)
    assert refused["status"] == "failed" and refused["error"]["code"] == 10


# --- a refusal is Navidrome's own answer, and one failure for its login limit ---------------

ORIGIN = {"Origin": "https://web.example"}
# Differences between an answer through Shijhon and Navidrome's own (suite A): two clocks,
# and what belongs to each connection.
PER_CONNECTION = {"date", "connection", "keep-alive", "transfer-encoding"}


@pytest.fixture(scope="module")
def replay() -> Replay:
    return Replay()


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[tuple[DeliveryWorld, str]]:
    """Shijhon with a catalog and one placeholder song (its ID), in front of Navidrome."""
    tmp = tmp_path_factory.mktemp("b-identity")
    with delivery_world(navidrome_factory(), tmp, catalog=replay.catalog()) as w:
        release = catalog_release("refused", "Refused", "Nobody Home", 1)
        song = w.materialize(release).created[release.tracks[0].ref]
        yield w, song


def failures(nd: NavidromeInstance) -> int:
    """Failed logins as Navidrome counted them (it logs each one)."""
    return nd.log_path.read_text(errors="replace").count("Invalid login")


def checked_calls(song: str) -> list[tuple[str, dict[str, str]]]:
    """Requests Shijhon checks the credentials of (it does work for them)."""
    album = f"sh.al.demo.{str(fixture('album-twins-clean')['path']).split('/')[1]}"
    return [
        ("stream", {"id": song}),
        ("download", {"id": song}),
        ("getAlbum", {"id": album}),
        ("search3", {"query": fixture("search-duo")["params"]["term"]}),
        ("star", {"albumId": album}),
        ("jukeboxControl", {"action": "status"}),
        ("getCoverArt", {"id": f"al-{album}"}),
        ("scrobble", {"id": song, "submission": "false"}),
        ("getArtist", {"id": f"sh.ar.demo.{str(fixture('artist-duo')['path']).split('/')[1]}"}),
        ("getTopSongs", {"artist": "Nobody Home"}),
    ]


FORMATS: list[dict[str, str]] = [
    {"f": "json"},
    {},  # XML, Navidrome's default
    {"f": "jsonp", "callback": "window.answer"},
    {"f": "jsonp", "callback": "not a name"},  # Navidrome's "invalid callback" answer
]
WRONG: list[dict[str, str]] = [
    {"u": ADMIN_USER, "p": "wrong", "v": "1.16.1", "c": "tests"},
    {"u": "nobody", "t": "0" * 32, "s": "abc", "v": "1.16.1", "c": "tests"},
    {"u": ADMIN_USER, "p": "wrong", "c": "tests"},  # no v: Navidrome's error 10
    {"u": ADMIN_USER, "p": "wrong", "v": "1.16.1"},  # no c
]


def answered(
    base_url: str,
    http_method: str,
    method: str,
    params: dict[str, str],
    coding: str = "identity",
    **more: str,
) -> Any:
    url = f"{base_url}/rest/{method}"
    headers = {**ORIGIN, "Accept-Encoding": coding, **more}
    if http_method == "POST":
        return httpx.post(url, data=params, headers=headers)
    return httpx.request(http_method, url, params=params, headers=headers)


def comparable(response: httpx.Response) -> tuple[int, dict[str, str], bytes]:
    headers = {k: v for k, v in response.headers.items() if k.lower() not in PER_CONNECTION}
    return response.status_code, headers, response.content


@pytest.mark.parametrize("http_method", ["GET", "POST", "HEAD"])
@pytest.mark.parametrize("fmt", FORMATS, ids=["json", "xml", "jsonp", "bad-callback"])
@pytest.mark.parametrize("wrong", WRONG, ids=["password", "user", "no-v", "no-c"])
def test_a_refusal_is_navidromes_own_answer(
    world: tuple[DeliveryWorld, str],
    replay: Replay,
    http_method: str,
    fmt: dict[str, str],
    wrong: dict[str, str],
) -> None:
    """A request whose credentials Shijhon checks and Navidrome refuses gets what Navidrome
    answers that request itself - status, headers, body, in the request's format - and is
    one failed login for Navidrome, not two; no work is done for it."""
    w, song = world
    counted = "v" in wrong and "c" in wrong  # error 10 comes before any login
    for method, params in checked_calls(song):
        query = {**wrong, **fmt, **params}
        own = comparable(answered(w.nd.base_url, http_method, method, query))
        before, work = failures(w.nd), (replay.api_requests, w.placeholder_rows())
        through = comparable(answered(w.server.base_url, http_method, method, query))
        assert through == own, (method, through, own)
        assert failures(w.nd) - before == (1 if counted else 0), method
        assert (replay.api_requests, w.placeholder_rows()) == work, method
    assert own[0] == 200 and (http_method == "HEAD" or b"failed" in own[2])


@pytest.mark.parametrize("http_method", ["GET", "HEAD"])
@pytest.mark.parametrize("coding", ["gzip, deflate, br", "deflate", "gzip;q=0, deflate", "br"])
@pytest.mark.parametrize("fmt", FORMATS, ids=["json", "xml", "jsonp", "bad-callback"])
def test_a_refusal_is_compressed_as_navidrome_compresses_it(
    world: tuple[DeliveryWorld, str], http_method: str, coding: str, fmt: dict[str, str]
) -> None:
    w, song = world
    query = {**WRONG[0], **fmt, "id": song}
    own = answered(w.nd.base_url, http_method, "stream", query, coding)
    through = answered(w.server.base_url, http_method, "stream", query, coding)
    assert comparable(through) == comparable(own)  # the same headers, the same content
    assert ("content-encoding" in own.headers) == (coding != "br")


def test_a_refusal_carries_navidromes_cookie_for_its_web_client(
    world: tuple[DeliveryWorld, str],
) -> None:
    w, song = world
    query = {**WRONG[0], "f": "json", "id": song}
    ident = {"X-ND-Client-Unique-Id": "0123456789abcdef"}
    own = answered(w.nd.base_url, "GET", "stream", query, **ident)
    through = answered(w.server.base_url, "GET", "stream", query, **ident)
    assert "set-cookie" in own.headers and comparable(through) == comparable(own)


def test_a_cors_preflight_is_navidromes(world: tuple[DeliveryWorld, str]) -> None:
    """A browser's preflight carries no login for Navidrome: it is forwarded as it is, also
    for a request Shijhon would check - no check, no failure counted."""
    w, song = world
    query = {**WRONG[0], "f": "json", "id": song}
    asking = {"Access-Control-Request-Method": "GET"}
    pings, before = w.app.checker.pings, failures(w.nd)
    own = answered(w.nd.base_url, "OPTIONS", "stream", query, **asking)
    through = answered(w.server.base_url, "OPTIONS", "stream", query, **asking)
    assert comparable(through) == comparable(own)
    assert "access-control-allow-methods" in through.headers and through.content == b""
    assert w.app.checker.pings == pings and failures(w.nd) == before
    # Without an Origin header it is no preflight for Navidrome: a request like any other.
    headers = {"Access-Control-Request-Method": "GET", "Accept-Encoding": "identity"}
    url, before = "/rest/stream", failures(w.nd)
    own = httpx.request("OPTIONS", w.nd.base_url + url, params=query, headers=headers)
    through = httpx.request("OPTIONS", w.server.base_url + url, params=query, headers=headers)
    assert comparable(through) == comparable(own) and b'"code":40' in through.content
    assert failures(w.nd) - before == 2  # one each


def test_a_long_refusal_comes_without_a_length_as_navidromes(
    world: tuple[DeliveryWorld, str],
) -> None:
    w, song = world
    query = {**WRONG[0], "f": "jsonp", "callback": "a" * 3000, "id": song}
    own = answered(w.nd.base_url, "GET", "stream", query)
    through = answered(w.server.base_url, "GET", "stream", query)
    assert "content-length" not in own.headers and comparable(through) == comparable(own)


def test_album_lists_with_complete_counts_ask_navidrome_once(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With albums shown complete, Shijhon reads Navidrome's answer to an album list itself
    (the complete counts): not after Navidrome refused the request's credentials."""
    tmp = tmp_path_factory.mktemp("b-identity-lists")
    with delivery_world(navidrome_factory(), tmp, catalog=replay.catalog(), fill=True) as w:
        fills = w.services.fills
        assert fills is not None

        async def shown() -> bool:
            return True

        monkeypatch.setattr(fills, "any_shown", shown)
        artist = f"sh.ar.demo.{str(fixture('artist-duo')['path']).split('/')[1]}"
        search = {"query": fixture("search-duo")["params"]["term"]}
        for method, params in (("search3", search), ("getArtist", {"id": artist})):
            for fmt in FORMATS[:2]:
                query = {**WRONG[0], **fmt, **params}
                own = comparable(answered(w.nd.base_url, "GET", method, query))
                before = failures(w.nd)
                assert comparable(answered(w.server.base_url, "GET", method, query)) == own
                assert failures(w.nd) - before == 1, (method, fmt)


def test_a_placeholder_is_never_forwarded_without_a_verdict(
    world: tuple[DeliveryWorld, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Navidrome fails the credential check (no verdict): a placeholder's stream, download
    and transcode stream are answered with an error by the real handlers - never passed on
    to Navidrome, which would play the silent file."""
    w, song = world
    asked: list[bytes] = []
    send = w.app.upstream.send

    async def failing(method: str, raw_path: bytes, *rest: Any) -> httpx.Response:
        asked.append(raw_path)
        if raw_path == b"/rest/getUser":
            return httpx.Response(503, headers={"content-type": "text/plain"}, stream=Busy())
        return await send(method, raw_path, *rest)

    monkeypatch.setattr(w.app.upstream, "send", failing)
    good = {"u": ADMIN_USER, "p": ADMIN_PASSWORD, "v": "1.16.1", "c": "no-verdict", "f": "json"}
    calls = [
        ("stream", {"id": song}),
        ("download", {"id": song}),
        ("getTranscodeStream", {"mediaId": song, "mediaType": "song", "transcodeParams": "x"}),
    ]
    for method, params in calls:
        for http_method in ("GET", "HEAD"):
            answer = answered(w.server.base_url, http_method, method, {**good, **params})
            assert answer.status_code == 502 and "retry-after" in answer.headers, method
    assert set(asked) == {b"/rest/getUser"} and len(asked) == 6  # one check each, no forward


class Busy(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"busy"


def test_an_unchecked_request_is_forwarded_untouched(world: tuple[DeliveryWorld, str]) -> None:
    """A request Shijhon does no work for: no check, Navidrome answers it (one failure)."""
    w, _ = world
    pings, before = w.app.checker.pings, failures(w.nd)
    query = {**WRONG[0], "f": "json"}
    through = comparable(answered(w.server.base_url, "GET", "getArtists", query))
    assert w.app.checker.pings == pings and failures(w.nd) - before == 1
    assert through[1] == comparable(answered(w.nd.base_url, "GET", "getArtists", query))[1]
    assert json.loads(through[2])["subsonic-response"]["error"]["code"] == 40
