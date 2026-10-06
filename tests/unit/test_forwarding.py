"""Who the client is, as Navidrome sees it: the address Shijhon names, and the
headers every request takes to Navidrome."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import anyio
import httpx
import pytest

from shijhon.dashboard.auth import navidrome_login
from shijhon.navidrome.client import NavidromeError
from shijhon.proxy import forwarding as forwarding_module
from shijhon.proxy.app import ProxyApp
from shijhon.proxy.auth import CredentialChecker
from shijhon.proxy.forwarding import (
    Forwarding,
    client_address,
    learn_user_header,
    networks,
    parse_address,
)
from shijhon.proxy.upstream import Upstream

TRUSTED = networks(["127.0.0.0/8", "10.0.0.0/8"])


def xff(*lines: str) -> list[tuple[bytes, bytes]]:
    return [(b"X-Forwarded-For", line.encode()) for line in lines]


def named(peer: str, headers: list[tuple[bytes, bytes]]) -> str:
    return str(client_address(peer, headers, TRUSTED))


@pytest.mark.parametrize(
    ("text", "address"),
    [
        ("203.0.113.5", "203.0.113.5"),
        (" 203.0.113.5 ", "203.0.113.5"),
        ("203.0.113.5:4711", "203.0.113.5"),
        ("2001:db8::7", "2001:db8::7"),
        ("[2001:db8::7]", "2001:db8::7"),
        ("[2001:db8::7]:443", "2001:db8::7"),
        ("::ffff:203.0.113.5", "203.0.113.5"),  # a dual-stack socket's IPv4 peer
        ("fe80::1%eth0", "fe80::1"),  # a zone is no part of the client's address
        ("[fe80::1%anything]:80", "fe80::1"),
        ("unknown", None),
        ("", None),
    ],
)
def test_addresses_as_proxies_write_them(text: str, address: str | None) -> None:
    parsed = parse_address(text)
    assert (None if parsed is None else str(parsed)) == address


def test_an_untrusted_peer_is_the_client_whatever_it_claims() -> None:
    claims = [*xff("1.2.3.4"), (b"x-real-ip", b"1.2.3.4"), (b"true-client-ip", b"1.2.3.4")]
    assert named("203.0.113.7", claims) == "203.0.113.7"


def test_a_trusted_proxy_names_the_client() -> None:
    # The last address it added; earlier entries are the client's own claims.
    assert named("10.0.0.9", xff("6.6.6.6, 203.0.113.5")) == "203.0.113.5"
    assert named("10.0.0.9", xff("6.6.6.6", "203.0.113.5")) == "203.0.113.5"  # header lines
    # Proxies behind proxies: the last address that is not a trusted proxy.
    assert named("127.0.0.1", xff("6.6.6.6, 203.0.113.5, 10.1.2.3")) == "203.0.113.5"
    # No X-Forwarded-For: the proxy itself. X-Real-IP is not read: a proxy that sets only
    # that passes a client's own X-Forwarded-For on, and a client can repeat it.
    assert named("10.0.0.9", [(b"x-real-ip", b"203.0.113.5")]) == "10.0.0.9"
    assert named("10.0.0.9", []) == "10.0.0.9"
    # A chain it cannot read: the proxy itself, not an earlier claim.
    assert named("10.0.0.9", xff("6.6.6.6, unknown")) == "10.0.0.9"
    # All proxies: the first one.
    assert named("10.0.0.9", xff("10.0.0.1, 10.0.0.2")) == "10.0.0.1"
    assert named("::ffff:127.0.0.1", xff("203.0.113.5")) == "203.0.113.5"


def test_the_headers_that_go_to_navidrome() -> None:
    sent = [
        (b"Host", b"music.test"),
        (b"X-Forwarded-For", b"6.6.6.6"),
        (b"X-Real-IP", b"6.6.6.6"),
        (b"True-Client-IP", b"6.6.6.6"),
        (b"Forwarded", b"for=6.6.6.6"),
        (b"Remote-User", b"admin"),
        (b"X-Forwarded-Proto", b"https"),
    ]
    untrusted = Forwarding([]).headers("203.0.113.7", sent)
    assert untrusted == [
        (b"Host", b"music.test"),
        (b"X-Forwarded-Proto", b"https"),  # Navidrome reads it from anyone: unchanged
        (b"x-forwarded-for", b"203.0.113.7"),
        (b"x-real-ip", b"203.0.113.7"),
    ]
    trusted = Forwarding(["203.0.113.0/24"]).headers("203.0.113.7", sent)
    # A proxy trusted for addresses does not name users: proxies pass clients' headers on.
    assert (b"Remote-User", b"admin") not in trusted
    assert [v for k, v in trusted if k.lower() == b"x-forwarded-for"] == [b"6.6.6.6"]
    assert [v for k, v in trusted if k.lower() == b"x-real-ip"] == [b"6.6.6.6"]
    assert not [k for k, _ in trusted if k.lower() in (b"true-client-ip", b"forwarded")]


def test_user_headers_go_on_only_with_header_sign_in_from_trusted_proxies() -> None:
    sent = [(b"X-Auth-User", b"admin"), (b"Remote-User", b"admin"), (b"X-Other", b"1")]
    kept = [b"X-Other", b"x-forwarded-for", b"x-real-ip"]
    for auth in (False, True):
        untrusted = Forwarding([], "X-Auth-User", user_auth=auth)
        assert [k for k, _ in untrusted.headers("203.0.113.7", sent)] == kept
    assert [k for k, _ in Forwarding(["203.0.113.7/32"], "X-Auth-User").headers(
        "203.0.113.7", sent
    )] == kept  # fmt: skip
    naming = Forwarding(["203.0.113.7/32"], "X-Auth-User", user_auth=True)
    # The configured header only; Navidrome's default is never passed on besides it.
    assert [k for k, _ in naming.headers("203.0.113.7", sent)] == [b"X-Auth-User", *kept]


def test_navidromes_own_user_header_is_dropped_too() -> None:
    forwarding = Forwarding(["203.0.113.7/32"], user_auth=True)
    assert not forwarding.navidrome_user_header("Remote-User")  # the configured one
    assert forwarding.navidrome_user_header("X-Forwarded-User")
    sent = [(b"X-Forwarded-User", b"admin"), (b"Remote-User", b"admin")]
    for peer in ("203.0.113.7", "198.51.100.1"):
        assert (b"X-Forwarded-User", b"admin") not in forwarding.headers(peer, sent)
    assert (b"Remote-User", b"admin") in forwarding.headers("203.0.113.7", sent)


def test_ipv4_mapped_trusted_networks() -> None:
    assert networks(["::ffff:127.0.0.1/128"]) == networks(["127.0.0.1/32"])
    assert networks(["::ffff:10.0.0.0/104"]) == networks(["10.0.0.0/8"])
    assert (
        str(client_address("127.0.0.1", xff("203.0.113.5"), networks(["::ffff:127.0.0.1/128"])))
        == "203.0.113.5"
    )


def test_no_peer_no_address() -> None:
    forwarding = Forwarding()
    assert forwarding.client("", xff("203.0.113.5")) is None
    assert forwarding.headers("", xff("203.0.113.5")) == []
    scope = forwarding.scope({"type": "http", "headers": xff("203.0.113.5"), "client": None})
    assert scope["headers"] == []


def test_ipv6_clients() -> None:
    address = client_address("::1", xff("2001:db8::7"), networks(["::1/128"]))
    assert address == ipaddress.ip_address("2001:db8::7")
    assert client_address("::1", xff("2001:db8::7"), TRUSTED) == ipaddress.ip_address("::1")


def test_the_dashboards_login_names_the_client() -> None:
    """The dashboard's sign-in goes to Navidrome's /auth/login with the client's address,
    which Navidrome's login limit counts once it trusts Shijhon."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    async def main() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            address = parse_address("203.0.113.5")
            assert (
                await navidrome_login(http, "http://navidrome.test", "a", "b", address)
            ).outcome == "wrong"
            await navidrome_login(http, "http://navidrome.test", "a", "b", None)

    anyio.run(main)
    assert seen[0].headers["x-forwarded-for"] == "203.0.113.5"
    assert seen[0].headers["x-real-ip"] == "203.0.113.5"
    assert "x-forwarded-for" not in seen[1].headers


class StartingNavidrome:
    """Navidrome as the startup read meets it: not answering for ``down`` asks, then its
    configuration (a dict, or the error it answers with) after ``slow`` seconds."""

    base_url = "http://navidrome.test"

    def __init__(self, config: Any, *, down: int = 0, slow: float = 0.0) -> None:
        self.config, self.down, self.slow = config, down, slow
        self.http = self

    async def get(self, url: str) -> httpx.Response:
        if self.down > 0:
            self.down -= 1
            raise httpx.ConnectError("not listening yet")
        return httpx.Response(200)

    async def native_json(self, method: str, path: str) -> Any:
        await anyio.sleep(self.slow)
        if isinstance(self.config, Exception):
            raise self.config
        return {"config": self.config}


SENT = [(b"X-Auth-User", b"admin")]


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forwarding_module, "SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(forwarding_module, "RETRY_SECONDS", 0.02)


def test_requests_wait_for_navidromes_user_header(quick: None) -> None:
    """A Navidrome that starts late and shows its configuration slowly: until its own
    user header is read, requests are not let through (a header of that name would pass)."""
    navidrome: Any = StartingNavidrome({"ExtAuth": {"UserHeader": "X-Auth-User"}}, down=5, slow=0.2)

    async def main() -> None:
        forwarding = Forwarding()
        assert await forwarding.settled()  # nothing is being read: no wait
        forwarding.learning()
        async with anyio.create_task_group() as tg:
            tg.start_soon(learn_user_header, forwarding, navidrome)
            for _ in range(3):  # down, then reading: not settled, however long it takes
                assert not await forwarding.settled()
                assert SENT[0] in forwarding.headers("203.0.113.7", SENT)
        assert await forwarding.settled()
        assert SENT[0] not in forwarding.headers("203.0.113.7", SENT)

    anyio.run(main)


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (NavidromeError("native GET: HTTP 404", status=404), "is not shown"),
        (NavidromeError("service login failed (401)"), "could not be read"),
    ],
)
def test_a_configuration_that_is_not_shown_is_said(
    quick: None, caplog: pytest.LogCaptureFixture, answer: Exception, said: str
) -> None:
    navidrome: Any = StartingNavidrome(answer)

    async def main() -> None:
        forwarding = Forwarding()
        forwarding.learning()
        await learn_user_header(forwarding, navidrome)
        assert await forwarding.settled()  # requests go on: the settings' names are dropped

    with caplog.at_level(logging.WARNING, logger="shijhon.proxy.forwarding"):
        anyio.run(main)
    assert said in caplog.text and "reverse_proxy_user_header" in caplog.text


def test_nothing_is_forwarded_while_navidromes_user_header_is_read(quick: None) -> None:
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def main() -> None:
        upstream = Upstream("http://navidrome.test")
        proxy = ProxyApp(upstream, CredentialChecker(upstream))
        proxy.forwarding.learning()
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/rest/ping",
            "query_string": b"",
            "headers": SENT,
            "client": ("203.0.113.7", 4711),
        }
        await proxy(scope, receive, send)
        await upstream.aclose()

    anyio.run(main)
    assert sent[0]["status"] == 503 and (b"retry-after", b"5") in sent[0]["headers"]
