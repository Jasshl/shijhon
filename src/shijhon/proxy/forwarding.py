"""Who the client is, as Navidrome sees it.

Navidrome 0.64.2 limits failed logins per client address (on ``/rest`` together with the
user name), and takes the client's address from forwarding headers only when the request
comes from one of its trusted sources (``ExtAuth.TrustedSources``) - which also makes it
accept a reverse-proxy user header (``Remote-User``) from them. Behind Shijhon every
client would otherwise be Shijhon's address: one device with a stale password, or anyone
at all, could lock out every device of a user, Shijhon's service account and every login.
So, for every request that goes on to Navidrome:

- the client's address is the peer's; when the peer is one of ``[server]
  trusted_proxies``, it is the last address in ``X-Forwarded-For`` that is not itself a
  trusted proxy (earlier entries are the client's own claims), else the proxy's own: a
  trusted proxy must append the client to ``X-Forwarded-For`` (``X-Real-IP`` is not read -
  a proxy that sets only that passes a client's own ``X-Forwarded-For`` on);
- the addresses a client sent (``X-Forwarded-For``, ``X-Real-IP``, ``True-Client-IP``,
  ``Forwarded``) never reach Navidrome: ``X-Forwarded-For`` and ``X-Real-IP`` go with the
  client's address alone, never a chain Navidrome could take a claim from;
- a reverse-proxy user header never reaches Navidrome - ``Remote-User`` (Navidrome's
  default ``ExtAuth.UserHeader``), ``[server] reverse_proxy_user_header`` and the one
  Navidrome's configuration names - unless ``[server] reverse_proxy_auth`` is on: then
  the configured one goes on from trusted proxies (header sign-in through Shijhon). A
  proxy trusted for addresses is not trusted to name users by default: common proxies
  pass a client's own headers on.

The dashboard's sign-in limit counts the same address. uvicorn's own ``proxy_headers``
stay off (``cli.py``): this is the one place that reads these headers. What a proxy says
about the original request (``X-Forwarded-Proto``, ``-Host``) is passed on as before:
Navidrome reads it from anyone, trusted or not.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

import anyio
import httpx

from shijhon.navidrome.client import NavidromeError

if TYPE_CHECKING:
    from shijhon.navidrome.client import NavidromeService

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Headers = Sequence[tuple[bytes, bytes]]

# The client's address, as clients or proxies state it: replaced on every request.
_ADDRESS_HEADERS = {b"x-forwarded-for", b"x-real-ip", b"true-client-ip", b"forwarded"}
LOOPBACK = ("127.0.0.0/8", "::1/128")
SETTLE_SECONDS = 10.0  # the longest a request waits for Navidrome's user header at startup
RETRY_SECONDS = 1.0  # between asks while Navidrome does not answer at all


def networks(items: Sequence[str]) -> list[Network]:
    """Networks; IPv4-mapped IPv6 ones as the IPv4 networks they are (peers are read so)."""
    found: list[Network] = []
    for item in items:
        net = ipaddress.ip_network(item, strict=False)
        if isinstance(net, ipaddress.IPv6Network) and net.prefixlen >= 96:
            mapped = net.network_address.ipv4_mapped
            if mapped is not None:
                net = ipaddress.IPv4Network(f"{mapped}/{net.prefixlen - 96}", strict=False)
        found.append(net)
    return found


def parse_address(text: str) -> Address | None:
    """An address as proxies write it: plain, ``[v6]``, ``[v6]:port`` or ``v4:port``;
    without an IPv6 zone, and an IPv4-mapped one as IPv4."""
    text = text.strip()
    if text.startswith("["):
        text = text[1:].partition("]")[0]
    elif text.count(":") == 1:
        text = text.partition(":")[0]  # IPv4 with a port
    try:
        address = ipaddress.ip_address(text.partition("%")[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _trusted(address: Address | None, trusted: Sequence[Network]) -> bool:
    return address is not None and any(address in net for net in trusted)


def client_address(peer: str, headers: Headers, trusted: Sequence[Network]) -> Address | None:
    """The client's address: the peer's, or, from a trusted proxy, the one it names."""
    address = parse_address(peer)
    if not _trusted(address, trusted):
        return address
    chain = b",".join(v for k, v in headers if k.lower() == b"x-forwarded-for")
    if not chain.strip():
        return address  # the proxy named no one: its own address
    entries = [parse_address(part) for part in chain.decode("latin-1").split(",")]
    for entry in reversed(entries):
        if entry is None:
            return address  # a chain it cannot read: the proxy's own address
        if not _trusted(entry, trusted):
            return entry
    return entries[0]  # proxies all the way: the first one


class Forwarding:
    def __init__(
        self,
        trusted: Sequence[str] = LOOPBACK,
        user_header: str = "Remote-User",
        *,
        user_auth: bool = False,
    ) -> None:
        self.trusted = networks(trusted)
        self.user_header = user_header.strip().lower().encode()
        self.user_auth = user_auth  # the user header goes on from trusted proxies
        self._dropped = {b"remote-user", self.user_header}
        # Set while Navidrome's own user header is being read at startup (``learning``).
        self._known: anyio.Event | None = None

    def learning(self) -> None:
        """Navidrome's own user header is about to be read: requests wait for it."""
        self._known = anyio.Event()

    def learned(self) -> None:
        if self._known is not None:
            self._known.set()

    async def settled(self) -> bool:
        """Whether Navidrome's own user header is known (or not read at all): waits for it
        at most ``SETTLE_SECONDS``. False: still being read - requests are not forwarded
        meanwhile, since a header of that name could still pass (Navidrome is starting
        then: it answered nothing yet, or not its configuration)."""
        known = self._known
        if known is None or known.is_set():
            return True
        with anyio.move_on_after(SETTLE_SECONDS):
            await known.wait()
        return known.is_set()

    def navidrome_user_header(self, name: str) -> bool:
        """Navidrome's own ``ExtAuth.UserHeader``, from its configuration: never passed on
        unless it is the configured one. True when it is not."""
        header = name.strip().lower().encode()
        if not header or header == self.user_header:
            return False
        self._dropped.add(header)
        return True

    def client(self, peer: str, headers: Headers) -> Address | None:
        return client_address(peer, headers, self.trusted)

    def headers(self, peer: str, headers: Headers) -> list[tuple[bytes, bytes]]:
        """The request's headers as they go to Navidrome."""
        naming = self.user_auth and _trusted(parse_address(peer), self.trusted)
        out = [
            (k, v)
            for k, v in headers
            if k.lower() not in _ADDRESS_HEADERS
            and (k.lower() not in self._dropped or (naming and k.lower() == self.user_header))
        ]
        address = self.client(peer, headers)
        if address is not None:
            named = str(address).encode()
            out += [(b"x-forwarded-for", named), (b"x-real-ip", named)]
        return out

    def scope(self, scope: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        """A copy of an ASGI scope with the headers that go to Navidrome."""
        return {**scope, "headers": self.headers(peer(scope), list(scope.get("headers") or []))}


def peer(scope: MutableMapping[str, Any]) -> str:
    client = scope.get("client")
    return str(client[0]) if client else ""


async def learn_user_header(forwarding: Forwarding, navidrome: NavidromeService) -> None:
    """Navidrome's own ``ExtAuth.UserHeader``, from its configuration once it answers (it
    shows it while its ``DevUIShowConfig`` is on, the default): never passed on either, so
    that a name Shijhon's setting does not know cannot sign anyone in. Requests wait
    for it at startup (``Forwarding.settled``)."""
    try:
        config = await _navidrome_config(navidrome)
        ext = config.get("ExtAuth") if config else None
        name = ext.get("UserHeader") if isinstance(ext, dict) else None
        other = isinstance(name, str) and forwarding.navidrome_user_header(name)
    finally:
        forwarding.learned()  # after the header is added: requests go on from here
    if not config:
        log.warning(
            "Navidrome's configuration %s: if its ExtAuth.UserHeader is not Remote-User, set"
            " [server] reverse_proxy_user_header to it - Shijhon drops that header from clients",
            "is not shown (DevUIShowConfig)" if config is False else "could not be read",
        )
    elif other and forwarding.user_auth:  # header sign-in cannot work under another name
        log.error(
            "Navidrome's ExtAuth.UserHeader is not [server] reverse_proxy_user_header:"
            " Navidrome's is dropped, so no one signs in through the proxy; set the setting"
            " to Navidrome's header name"
        )
    elif other:
        log.info("Navidrome's ExtAuth.UserHeader is dropped from every request too")


async def _navidrome_config(navidrome: NavidromeService) -> dict[str, Any] | Literal[False] | None:
    """Navidrome's configuration; False when it does not show it, None when it cannot be
    read. Waits while Navidrome does not answer at all (requests could not be answered
    then either)."""
    while True:
        if await _answers(navidrome):
            break
        await anyio.sleep(RETRY_SECONDS)
    try:
        body = await navidrome.native_json("GET", "config/")
    except NavidromeError as exc:
        return False if exc.status in (403, 404) else None
    except Exception as exc:  # background work never fails the others
        log.info("Navidrome's configuration not read: %s", type(exc).__name__)
        return None
    config = body.get("config") if isinstance(body, dict) else None
    return config if isinstance(config, dict) else False


async def _answers(navidrome: NavidromeService) -> bool:
    """Whether Navidrome answers HTTP at all (its heartbeat; any answer will do)."""
    try:
        await navidrome.http.get(f"{navidrome.base_url}/ping")
    except httpx.HTTPError:
        return False
    return True
