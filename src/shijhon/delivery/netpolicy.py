"""Outgoing network policy for add-ons and the audio they point to.

Every connection is checked where it is made: the host name is resolved, each address is
checked, and the connection goes to a checked address (TLS still verifies the original
host name). Redirect hops open new connections and so are checked the same way, and a DNS
answer cannot change between check and use.

By default only public addresses are allowed. An add-on can be allowed loopback (a worker
on the same machine) or private networks (a LAN or VPN host) explicitly; link-local
addresses (cloud metadata services) are never allowed.

This is custom because no maintained library enforces a resolve-then-connect policy for
httpx; it hooks into httpcore's documented network-backend interface.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum
from typing import Any

import anyio
import httpcore
import httpx

from shijhon import __version__

Resolver = Callable[[str, int], Awaitable[list[str]]]

# How Shijhon names itself to add-ons and catalogs, on every request it sends them:
# what it is, its version, and where to read about it - the project's own repository, the
# only address this code names. An add-on's operator can tell its requests apart by it.
PROJECT_URL = "https://github.com/Jasshl/shijhon"
USER_AGENT = f"Shijhon/{__version__} (+{PROJECT_URL})"


class Reach(StrEnum):
    PUBLIC = "public"
    LOOPBACK = "loopback"  # public + this machine
    PRIVATE = "private"  # public + loopback + private and shared (CGNAT) ranges


class PolicyDenied(httpcore.ConnectError):
    """The destination is not allowed for this add-on."""


def denied(exc: BaseException) -> bool:
    """True if the policy refused the connection (httpx wraps httpcore's exception)."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, PolicyDenied):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


# Cloud metadata services outside link-local space (AWS IPv6, Alibaba Cloud).
_METADATA = {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}


def allowed(address: str, reach: Reach) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA:
        return False
    if ip.is_loopback:  # before is_reserved: ::1 lies in the reserved ::/8
        return reach in (Reach.LOOPBACK, Reach.PRIVATE)
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    # (Site-local IPv6, fec0::/10: deprecated, but routed inside a site where it is used -
    # not "global", whatever the address library says of it.)
    if ip.is_global and not getattr(ip, "is_site_local", False):
        return True
    return reach is Reach.PRIVATE  # private, unique-local, site-local and shared space


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class PolicyBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, reach: Reach, resolver: Resolver = system_resolver) -> None:
        self.reach = reach
        self.resolver = resolver
        self.inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await self.resolver(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve host ({exc.__class__.__name__})") from exc
        permitted = [a for a in addresses if allowed(a, self.reach)]
        if not permitted:
            raise PolicyDenied("destination address not allowed by the network policy")
        last: Exception | None = None
        for address in permitted:
            try:
                return await self.inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last = exc
        raise httpcore.ConnectError("could not connect") from last

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:
        raise PolicyDenied("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class PolicyTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connections go through :class:`PolicyBackend`."""

    def __init__(
        self,
        reach: Reach,
        *,
        resolver: Resolver = system_resolver,
        limits: httpx.Limits | None = None,
    ) -> None:
        limits = limits or httpx.Limits(max_connections=32, max_keepalive_connections=8)
        super().__init__(limits=limits, trust_env=False)
        # httpx does not expose httpcore's network_backend; build the pool it would build.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=PolicyBackend(reach, resolver),
        )


def policy_client(
    reach: Reach,
    *,
    timeout: httpx.Timeout | float = 10.0,
    resolver: Resolver = system_resolver,
    user_agent: str = USER_AGENT,
) -> httpx.AsyncClient:
    """An httpx client for one add-on (or a catalog): policy-checked, redirects followed
    (each hop checked), no environment proxies, identity encoding for audio - and Shijhon's
    ``User-Agent`` on every request (an audio request alone may carry another: the headers
    its add-on handed over with the link)."""
    return httpx.AsyncClient(
        transport=PolicyTransport(reach, resolver=resolver),
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
        headers={"user-agent": user_agent},
    )
