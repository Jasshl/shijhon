from __future__ import annotations

import anyio
import httpcore
import pytest

from shijhon.delivery.netpolicy import PolicyBackend, Reach, allowed


@pytest.mark.anyio
async def test_connect_deadline_includes_dns() -> None:
    async def stalled(host: str, port: int) -> list[str]:
        await anyio.sleep_forever()
        return []

    backend = PolicyBackend(Reach.PUBLIC, stalled)
    with anyio.fail_after(1), pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("demo.example.invalid", 443, timeout=0.02)


@pytest.mark.anyio
async def test_connect_attempts_share_the_time_left_after_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        await anyio.sleep(0.02)
        return ["8.8.8.8", "8.8.4.4", "1.1.1.1"]

    backend = PolicyBackend(Reach.PUBLIC, resolve)
    remaining: list[float] = []

    async def connect(host: str, port: int, seconds: float, *args: object) -> None:
        remaining.append(seconds)
        if len(remaining) == 1:
            await anyio.sleep(0.02)
            raise httpcore.ConnectError("no connection")
        await anyio.sleep_forever()

    monkeypatch.setattr(backend.inner, "connect_tcp", connect)
    with anyio.fail_after(1), pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("demo.example.invalid", 443, timeout=0.2)
    assert len(remaining) == 3
    assert all(0 < seconds < 0.2 for seconds in remaining)


@pytest.mark.anyio
@pytest.mark.parametrize("connect_timeout", [None, 1.0])
async def test_connect_can_try_another_address(
    monkeypatch: pytest.MonkeyPatch, connect_timeout: float | None
) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["8.8.8.8", "8.8.4.4"]

    backend = PolicyBackend(Reach.PUBLIC, resolve)
    connected = object()

    async def connect(host: str, port: int, remaining: float | None, *args: object) -> object:
        assert (remaining is None) == (connect_timeout is None)
        if host == "8.8.8.8":
            raise httpcore.ConnectError("no connection")
        return connected

    monkeypatch.setattr(backend.inner, "connect_tcp", connect)
    assert (
        await backend.connect_tcp("demo.example.invalid", 443, timeout=connect_timeout) is connected
    )


@pytest.mark.parametrize(
    "address,public,loopback,private",
    [
        ("8.8.8.8", True, True, True),
        ("2001:4860::8888", True, True, True),
        ("127.0.0.1", False, True, True),
        ("::1", False, True, True),
        ("::ffff:127.0.0.1", False, True, True),
        ("10.1.2.3", False, False, True),
        ("192.168.1.10", False, False, True),
        ("100.64.1.1", False, False, True),
        ("fd00::1", False, False, True),
        ("fec0::1", False, False, True),  # site-local (deprecated): never public
        ("feff:ffff::9", False, False, True),
        ("169.254.169.254", False, False, False),
        ("fe80::1", False, False, False),
        ("0.0.0.0", False, False, False),  # noqa: S104
        ("224.0.0.1", False, False, False),
        ("240.0.0.1", False, False, False),
    ],
)
def test_reach(address: str, public: bool, loopback: bool, private: bool) -> None:
    assert allowed(address, Reach.PUBLIC) is public
    assert allowed(address, Reach.LOOPBACK) is loopback
    assert allowed(address, Reach.PRIVATE) is private


@pytest.mark.parametrize("address", ["fd00:ec2::254", "100.100.100.200"])
def test_cloud_metadata_never_allowed(address: str) -> None:
    assert not allowed(address, Reach.PRIVATE)


def test_upstream_escapes_bytes_httpx_rejects() -> None:
    from shijhon.proxy.upstream import Upstream

    url = Upstream("http://navidrome.test:4533").url("/rest/x é".encode(), "a=é b".encode())
    assert url.raw_path == b"/rest/x%20%C3%A9?a=%C3%A9%20b"


@pytest.mark.anyio
async def test_blackholed_ipv6_leaves_time_for_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["2001:4860::8888", "8.8.8.8"]

    backend = PolicyBackend(Reach.PUBLIC, resolve)
    connected = object()
    attempts = []

    async def connect(host: str, port: int, seconds: float, *args: object) -> object:
        attempts.append((host, seconds))
        if ":" in host:
            await anyio.sleep_forever()
        return connected

    monkeypatch.setattr(backend.inner, "connect_tcp", connect)
    with anyio.fail_after(1):
        assert await backend.connect_tcp("demo.example.invalid", 443, timeout=0.1) is connected
    assert len(attempts) == 2 and attempts[1][1] > 0
