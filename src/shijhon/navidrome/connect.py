"""How Shijhon reaches Navidrome: at its address (``[navidrome] url``) over TCP, or through
its Unix socket (``[navidrome] socket``), where ``url`` then gives only the ``Host``
header of Shijhon's own requests (forwarded app requests keep the app's). Every HTTP
client Shijhon points at Navidrome is made here, so that all of them - the forwards,
Navidrome's own API and login page, the credential check, the dashboard's sign-in, the
service account's calls - take the same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

# httpx's own defaults, given to a transport made here (a client's ``limits`` do not
# reach a transport given to it).
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def async_client(
    socket: Path | None, *, limits: httpx.Limits | None = None, **kwargs: Any
) -> httpx.AsyncClient:
    """An async client for Navidrome; ``kwargs`` as for ``httpx.AsyncClient``."""
    limits = limits or DEFAULT_LIMITS
    if socket is None:
        return httpx.AsyncClient(limits=limits, trust_env=False, **kwargs)
    transport = httpx.AsyncHTTPTransport(uds=str(socket), limits=limits)
    return httpx.AsyncClient(transport=transport, trust_env=False, **kwargs)


def client(socket: Path | None, **kwargs: Any) -> httpx.Client:
    """A blocking client for Navidrome (the command line's); ``kwargs`` as for
    ``httpx.Client``."""
    if socket is None:
        return httpx.Client(trust_env=False, **kwargs)
    transport = httpx.HTTPTransport(uds=str(socket))
    return httpx.Client(transport=transport, trust_env=False, **kwargs)
