"""Suite B — credentials first.

This part checks the credential checker itself against a real Navidrome. The per-path
assertions (no catalog call, image fetch, file write or scan without valid
credentials) are in the suites of the paths that do such work.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anyio
import pytest

from shijhon.proxy.auth import Caller, CredentialChecker, Refusal
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Upstream
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance
from tests.harness.subsonic import SubsonicClient


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def nd(navidrome: NavidromeInstance) -> Iterator[NavidromeInstance]:
    navidrome.create_user("listener", "listener-password")
    yield navidrome


def call_for(client: SubsonicClient, headers: list[tuple[bytes, bytes]] | None = None) -> RestCall:
    query = "&".join(f"{k}={v}" for k, v in client.auth_params()).encode()
    return RestCall.build("getAlbum", "GET", b"/rest/getAlbum", query, headers or [], None)


def run(checker: CredentialChecker, call: RestCall) -> str | None:
    async def main() -> str | None:
        try:
            caller = await checker.check(call)
            return caller.username if isinstance(caller, Caller) else None
        finally:
            await checker.upstream.aclose()

    return anyio.run(main)


def make_checker(nd: NavidromeInstance, clock: Clock | None = None) -> CredentialChecker:
    return CredentialChecker(Upstream(nd.base_url), ttl=60, clock=clock or Clock())


@pytest.mark.parametrize("auth", ["token", "password", "hex"])
def test_valid_credentials_accepted(nd: NavidromeInstance, auth: str) -> None:
    client = SubsonicClient(nd.base_url, "listener", "listener-password", auth=auth)  # type: ignore[arg-type]
    assert run(make_checker(nd), call_for(client)) == "listener"


def test_jwt_accepted(nd: NavidromeInstance) -> None:
    # Navidrome requires ``u`` alongside ``jwt``.
    token = nd.login()["token"]
    query = f"u={ADMIN_USER}&jwt={token}&v=1.16.1&c=t".encode()
    call = RestCall.build("getAlbum", "GET", b"/rest/getAlbum", query, [], None)
    assert run(make_checker(nd), call) == ADMIN_USER


@pytest.mark.parametrize(
    "user,password", [("listener", "wrong"), ("nobody", "x"), (ADMIN_USER, ADMIN_PASSWORD + "x")]
)
def test_wrong_credentials_rejected_and_not_cached(
    nd: NavidromeInstance, user: str, password: str
) -> None:
    checker = make_checker(nd)
    call = call_for(SubsonicClient(nd.base_url, user, password, salt="abc123"))

    async def main() -> None:
        for _ in range(2):
            refusal = await checker.check(call)
            assert isinstance(refusal, Refusal)  # Navidrome's own answer, kept for the client
            assert refusal.status == 200 and b'status="failed"' in refusal.body
            assert b'<error code="40"' in refusal.body
        await checker.upstream.aclose()

    anyio.run(main)
    assert checker.pings == 2


def test_no_credentials_means_no_ping(nd: NavidromeInstance) -> None:
    checker = make_checker(nd)
    call = RestCall.build("getAlbum", "GET", b"/rest/getAlbum", b"v=1.16.1&c=x&id=1", [], None)
    assert run(checker, call) is None
    assert checker.pings == 0


def test_positive_result_cached_until_ttl(nd: NavidromeInstance) -> None:
    clock = Clock()
    checker = make_checker(nd, clock)
    call = call_for(SubsonicClient(nd.base_url, "listener", "listener-password", salt="s1"))

    async def main() -> None:
        assert isinstance(await checker.check(call), Caller)
        assert isinstance(await checker.check(call), Caller)
        assert checker.pings == 1
        clock.now += 61
        assert isinstance(await checker.check(call), Caller)
        assert checker.pings == 2
        await checker.upstream.aclose()

    anyio.run(main)


def test_concurrent_checks_share_one_ping(nd: NavidromeInstance) -> None:
    checker = make_checker(nd)
    call = call_for(SubsonicClient(nd.base_url, "listener", "listener-password", salt="s2"))
    results: list[object] = []

    async def main() -> None:
        async def one() -> None:
            results.append(await checker.check(call))

        async with anyio.create_task_group() as tg:
            for _ in range(10):
                tg.start_soon(one)
        await checker.upstream.aclose()

    anyio.run(main)
    assert checker.pings == 1
    assert all(isinstance(r, Caller) for r in results)


def test_positive_result_cached_per_client_address(nd: NavidromeInstance) -> None:
    """Navidrome's login limit counts per client address: one client's accepted check says
    nothing about another's with the same credentials."""
    checker = make_checker(nd)
    client = SubsonicClient(nd.base_url, "listener", "listener-password", salt="s3")
    one = call_for(client, [(b"x-forwarded-for", b"203.0.113.1")])
    other = call_for(client, [(b"x-forwarded-for", b"203.0.113.2")])

    async def main() -> None:
        assert isinstance(await checker.check(one), Caller)
        assert isinstance(await checker.check(one), Caller)
        assert checker.pings == 1
        assert isinstance(await checker.check(other), Caller)
        assert checker.pings == 2
        await checker.upstream.aclose()

    anyio.run(main)


def test_concurrent_refused_checks_share_one_ping(nd: NavidromeInstance) -> None:
    """Ten requests at once with one wrong password are one failure for Navidrome."""
    checker = make_checker(nd)
    call = call_for(SubsonicClient(nd.base_url, "listener", "wrong", salt="s4"))
    results: list[object] = []

    async def main() -> None:
        async def one() -> None:
            results.append(await checker.check(call))

        async with anyio.create_task_group() as tg:
            for _ in range(10):
                tg.start_soon(one)
        await checker.upstream.aclose()

    anyio.run(main)
    assert checker.pings == 1
    assert len(results) == 10 and all(isinstance(r, Refusal) for r in results)


@pytest.mark.parametrize("missing", ["v", "c"])
def test_a_request_without_v_or_c_is_refused_as_navidrome_refuses_it(
    nd: NavidromeInstance, missing: str
) -> None:
    """The check carries the request's own ``v`` and ``c``: without one, Navidrome's error
    10, whatever the password - never Shijhon's work for a request Navidrome refuses."""
    checker = make_checker(nd)
    client = SubsonicClient(nd.base_url, "listener", "listener-password", salt="s5")
    query = "&".join(f"{k}={v}" for k, v in client.auth_params() if k != missing).encode()
    call = RestCall.build("getAlbum", "GET", b"/rest/getAlbum", query, [], None)

    async def main() -> None:
        refusal = await checker.check(call)
        assert isinstance(refusal, Refusal) and b'<error code="10"' in refusal.body
        await checker.upstream.aclose()

    anyio.run(main)


# --- the caller's name is the user Navidrome authenticated ----------------------------


def named(nd: NavidromeInstance, query: str, **build: Any) -> tuple[str | None, int]:
    """(the caller's name, checks made) for a request with this query."""
    checker = make_checker(nd)
    call = RestCall.build(
        "getAlbum",
        build.get("method", "GET"),
        b"/rest/getAlbum",
        query.encode(),
        build.get("headers", []),
        build.get("body"),
    )
    return run(checker, call), checker.pings


def test_the_first_u_is_the_caller_as_navidrome_reads_it(nd: NavidromeInstance) -> None:
    """Navidrome reads the first of a repeated parameter: so does the caller's name, not
    the last one (whose limits and saved queue would be someone else's)."""
    query = f"u=listener&u={ADMIN_USER}&p=listener-password&v=1.16.1&c=t"
    assert named(nd, query) == ("listener", 1)
    # ... and a form body's before the query's.
    form = [(b"content-type", b"application/x-www-form-urlencoded")]
    body = b"u=listener&p=listener-password"
    posted = named(nd, f"u={ADMIN_USER}&v=1.16.1&c=t", method="POST", headers=form, body=body)
    assert posted == ("listener", 1)


@pytest.mark.parametrize("sent", ["LISTENER", "Listener", "listener"])
@pytest.mark.parametrize("fmt", ["", "&f=json", "&f=jsonp&callback=cb", "&f=jsonp&callback=%28"])
def test_the_callers_name_is_navidromes_whatever_the_letter_case(
    nd: NavidromeInstance, sent: str, fmt: str
) -> None:
    """Navidrome finds users whatever the letter case: one user is one caller - read from
    its answer in the request's format (XML, JSON, JSONP; a callback it refuses: asked
    without)."""
    assert named(nd, f"u={sent}&p=listener-password&v=1.16.1&c=t{fmt}") == ("listener", 1)


def test_a_caller_known_from_moments_ago_without_asking(nd: NavidromeInstance) -> None:
    clock = Clock()
    checker = make_checker(nd, clock)
    client = SubsonicClient(nd.base_url, "Listener", "listener-password", salt="s6")
    here = call_for(client, [(b"x-forwarded-for", b"203.0.113.1")])
    elsewhere = call_for(client, [(b"x-forwarded-for", b"203.0.113.2")])

    async def main() -> None:
        assert checker.known(here) is None  # not checked yet
        assert await checker.check(here) == Caller("listener")
        assert checker.known(here) == Caller("listener")
        assert checker.known(elsewhere) is None  # another address: not known there
        clock.now += 61
        assert checker.known(here) is None  # too long ago
        assert checker.pings == 1
        await checker.upstream.aclose()

    anyio.run(main)


def test_an_api_key_alone_names_no_one(nd: NavidromeInstance) -> None:
    """Navidrome 0.64.2 knows no ``apiKey``: such a request lacks ``u`` for it (error 10),
    and is no caller named ""."""
    checker = make_checker(nd)
    call = RestCall.build("getAlbum", "GET", b"/rest/getAlbum", b"apiKey=k&v=1.16.1&c=t", [], None)

    async def main() -> None:
        refusal = await checker.check(call)
        assert isinstance(refusal, Refusal) and b'<error code="10"' in refusal.body
        await checker.upstream.aclose()

    anyio.run(main)


def test_a_user_header_navidrome_does_not_read_names_no_one(nd: NavidromeInstance) -> None:
    """A reverse-proxy user header passed on (header sign-in) to a Navidrome that does not
    take it from Shijhon: the caller is the user of the credentials Navidrome did read."""
    header = [(b"Remote-User", b"listener")]
    query = f"u={ADMIN_USER}&p={ADMIN_PASSWORD}&v=1.16.1&c=t"
    assert named(nd, query, headers=header) == (ADMIN_USER, 2)  # asked for each name
