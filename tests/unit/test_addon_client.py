"""The add-on client's manners: one manifest fetch for concurrent first uses, the
time a rate limit names, nothing sent while an add-on is left alone, and how Shijhon names
itself."""

from __future__ import annotations

from email.utils import formatdate
from typing import Any

import anyio
import httpx
import pytest

import shijhon
from shijhon.catalog.plugin import Context
from shijhon.delivery import netpolicy, pacing
from shijhon.delivery.addon import Addon, AddonError
from shijhon.delivery.netpolicy import Reach, policy_client

MANIFEST = {"id": "test.addon", "name": "Test", "resources": ["stream", "isrc"]}


class Server:
    """An add-on behind a mock transport: counts requests, answers as told."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.manifest: httpx.Response | None = None  # the next manifest answer (else the good one)
        self.release = anyio.Event()  # manifest answers wait for it
        self.release.set()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/manifest.json"):
            await self.release.wait()
            if self.manifest is not None:
                answer, self.manifest = self.manifest, None
                return answer
            return httpx.Response(200, json=MANIFEST)
        if request.url.path.endswith("/resolve-isrc"):
            return httpx.Response(200, json={"trackId": "t1"})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.requests]


def addon(server: Server, pace: pacing.AddonPace | None = None) -> Addon:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    return Addon("https://addon.example.invalid/cfg/manifest.json", None, http, pace)


@pytest.mark.anyio
async def test_concurrent_first_uses_read_the_manifest_once() -> None:
    server = Server()
    server.release = anyio.Event()
    client = addon(server)
    found: list[Any] = []

    async def use() -> None:
        found.append(await client.resolve_isrc("AA1234567890"))

    async with anyio.create_task_group() as tg:
        for _ in range(6):
            tg.start_soon(use)
        await anyio.sleep(0.02)
        assert server.paths() == ["manifest.json"]  # the others wait for that one fetch
        server.release.set()
    assert found == ["t1"] * 6
    assert server.paths().count("manifest.json") == 1 and len(server.requests) == 7
    await client.manifest()  # read: never asked for again
    assert server.paths().count("manifest.json") == 1


@pytest.mark.anyio
async def test_a_failed_manifest_read_is_shared_and_counted_once() -> None:
    server = Server()
    server.release = anyio.Event()
    server.manifest = httpx.Response(503)
    client = addon(server)
    errors: list[AddonError] = []

    async def use() -> None:
        try:
            await client.manifest()
        except AddonError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tg:
        for _ in range(4):
            tg.start_soon(use)
        await anyio.sleep(0.02)
        server.release.set()
    assert len(server.requests) == 1 and len(errors) == 4
    assert {e.kind for e in errors} == {"failed"}
    assert sorted(e.shared for e in errors) == [False, True, True, True]  # its failure: once
    # Nothing is remembered of a failure: the next use reads it.
    assert (await client.manifest()).name == "Test" and len(server.requests) == 2


@pytest.mark.anyio
async def test_a_manifest_read_cut_short_is_read_by_the_next_one_waiting() -> None:
    server = Server()
    server.release = anyio.Event()
    client = addon(server)
    names: list[str] = []

    async def use() -> None:
        names.append((await client.manifest()).name)

    async with anyio.create_task_group() as tg:
        with anyio.move_on_after(0.02):  # the first request's own time runs out
            await client.manifest()
        tg.start_soon(use)
        tg.start_soon(use)
        await anyio.sleep(0.02)
        server.release.set()
    assert names == ["Test", "Test"]
    assert server.paths() == ["manifest.json", "manifest.json"]  # the cut one, then one more


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("120", 120.0),
        ("0", 0.0),
        (None, None),
        ("-30", None),
        ("soon", None),
        ("864000", pacing.MAX_RETRY_AFTER),
    ],
)
async def test_a_rate_limit_carries_the_time_its_retry_after_names(
    header: str | None, seconds: float | None
) -> None:
    server = Server()
    headers = {} if header is None else {"retry-after": header}
    server.manifest = httpx.Response(429, headers=headers)
    with pytest.raises(AddonError) as limited:
        await addon(server).manifest()
    assert limited.value.kind == "rate_limited" and limited.value.retry_after == seconds


@pytest.mark.anyio
async def test_a_rate_limit_s_http_date_is_the_time_until_then() -> None:
    import time

    server = Server()
    date = formatdate(time.time() + 90, usegmt=True)
    server.manifest = httpx.Response(429, headers={"retry-after": date})
    with pytest.raises(AddonError) as limited:
        await addon(server).manifest()
    assert limited.value.retry_after == pytest.approx(90, abs=3)


@pytest.mark.anyio
async def test_an_add_on_left_alone_after_a_rate_limit_is_sent_nothing() -> None:
    server = Server()
    pace = pacing.AddonPace(pacing.Limits(0, 1, 0))
    client = addon(server, pace)
    assert await client.resolve_isrc("AA1234567890") == "t1"
    sent = len(server.requests)
    pace.block(30)
    with pytest.raises(AddonError) as refused:
        await client.resolve_isrc("BB1234567890")
    assert refused.value.kind == "cooling" and refused.value.reason == pacing.BLOCKED
    assert len(server.requests) == sent  # nothing went out


@pytest.mark.anyio
async def test_shijhon_names_itself_its_version_and_its_repository() -> None:
    """One User-Agent for everything sent to add-ons and catalogs, from one place."""
    expected = f"Shijhon/{shijhon.__version__} (+https://github.com/Jasshl/shijhon)"
    assert expected == netpolicy.USER_AGENT
    async with policy_client(Reach.PUBLIC) as addons_http, Context().http() as catalog_http:
        assert addons_http.headers["user-agent"] == expected
        assert catalog_http.headers["user-agent"] == expected
        built = addons_http.build_request("GET", "https://addon.example.invalid/manifest.json")
        assert built.headers["user-agent"] == expected


@pytest.mark.anyio
async def test_a_rate_limit_leaves_the_add_on_alone_whoever_got_the_answer() -> None:
    """The client itself sees to it (the time the answer names, else the cooldown): a
    request nobody waits for - a preparation request in the background - is honored like a
    play's lookup."""
    server = Server()
    pace = pacing.AddonPace(pacing.Limits(0, 1, 0))
    pace.cooldown = 45.0
    client = addon(server, pace)
    await client.manifest()

    async def limited(request: httpx.Request) -> httpx.Response:
        server.requests.append(request)
        return httpx.Response(429, headers={"retry-after": "120"})

    client.http = httpx.AsyncClient(transport=httpx.MockTransport(limited))
    with pytest.raises(AddonError) as refused:
        await client.resolve_isrc("AA1234567890")
    assert refused.value.kind == "rate_limited" and refused.value.retry_after == 120.0
    assert pace.blocked == pytest.approx(120, abs=1)
    sent = len(server.requests)
    with pytest.raises(AddonError, match="cooling"):
        await client.resolve_isrc("BB1234567890")
    assert len(server.requests) == sent

    # An answer that only says so in its body names no time: the cooldown.
    other = pacing.AddonPace(pacing.Limits(0, 1, 0))
    other.cooldown = 45.0

    async def in_the_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "Rate limit exceeded"})

    quiet = Addon(
        "https://addon.example.invalid/cfg",
        None,
        httpx.AsyncClient(transport=httpx.MockTransport(in_the_body)),
        other,
    )
    with pytest.raises(AddonError, match="rate_limited"):
        await quiet.manifest()
    assert other.blocked == pytest.approx(45, abs=1)


@pytest.mark.anyio
async def test_a_manifest_read_that_breaks_is_shared_too_whatever_broke() -> None:
    """Not only the add-on's errors: an answer nested too deeply to read is one failure for
    the requests waiting, not a fetch each."""
    server = Server()
    server.release = anyio.Event()
    nested = "[" * 100_000 + "]" * 100_000
    server.manifest = httpx.Response(200, content=nested.encode())
    client = addon(server)
    errors: list[AddonError] = []

    async def use() -> None:
        try:
            await client.manifest()
        except AddonError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(use)
        await anyio.sleep(0.02)
        server.release.set()
    assert len(server.requests) == 1 and len(errors) == 3
    assert {e.kind for e in errors} == {"broken"}
    assert sorted(e.shared for e in errors) == [False, True, True]


@pytest.mark.anyio
async def test_an_id_that_is_no_path_segment_is_not_asked_for() -> None:
    """ "." and ".." as an ID would name another address of the add-on."""
    server = Server()
    client = addon(server)
    await client.manifest()
    for call in (client.stream, client.album, client.artist):
        for own in (".", "..", ""):
            with pytest.raises(AddonError) as failed:
                await call(own)
            assert failed.value.kind == "invalid"
    assert server.paths() == ["manifest.json"]


@pytest.mark.parametrize(
    "manifest,downloads",
    [
        ({}, True),
        ({"allowDownloads": 1}, True),
        ({"allowDownloads": True}, True),
        ({"allowDownloads": "1"}, True),
        ({"allowDownloads": None}, True),
        ({"allowDownloads": 0}, False),
        ({"allowDownloads": 0.0}, False),
        ({"allowDownloads": False}, False),
        ({"allowDownloads": "0"}, False),
        ({"allowDownloads": " false "}, False),
        ({"allowDownloads": "FALSE"}, False),
    ],
)
def test_a_manifest_says_whether_the_add_on_is_for_downloads(
    manifest: dict[str, Any], downloads: bool
) -> None:
    from shijhon.dashboard.addons import parse_manifest
    from shijhon.delivery.addon import Manifest

    assert Manifest.parse({**MANIFEST, **manifest}).downloads is downloads
    assert parse_manifest({**MANIFEST, **manifest}).downloads is downloads


@pytest.mark.parametrize(
    "link",
    [
        "https://addon.example.invalid/cfg/manifest.json?sources=ab",
        "https://addon.example.invalid/cfg?sources=ab",
        "https://addon.example.invalid/cfg/?sources=ab",
    ],
)
@pytest.mark.anyio
async def test_a_link_with_a_query_keeps_it_on_every_request(link: str) -> None:
    """Some add-on links carry their configuration in a query after ``manifest.json``: the
    requests go to the add-on's base, each with that query."""
    server = Server()
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    client = Addon(link, None, http, None)
    assert await client.resolve_isrc("USAAA0000001") == "t1"
    urls = [request.url for request in server.requests]
    assert [url.path for url in urls] == ["/cfg/manifest.json", "/cfg/resolve-isrc"]
    assert all(url.params.get("sources") == "ab" for url in urls)


def test_the_manifest_of_a_link_with_a_query() -> None:
    from shijhon.dashboard.addons import manifest_url

    for link in (
        "https://a1.invalid/x/manifest.json?sources=ab",
        "https://a1.invalid/x?sources=ab",
    ):
        assert str(manifest_url(link)) == "https://a1.invalid/x/manifest.json?sources=ab"
    assert (
        str(manifest_url("https://a1.invalid/manifest.json")) == "https://a1.invalid/manifest.json"
    )
