"""Add-on clients share the system's certificates, read once per process: building a
client after a change of the add-on settings never reads them again in the event loop."""

from __future__ import annotations

import os
import ssl
from typing import Any

import pytest

from shijhon.delivery import netpolicy
from shijhon.delivery.netpolicy import Reach, policy_client


def test_clients_built_after_the_first_read_no_certificates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    netpolicy._tls()  # (read once already, as by the first client)
    reads: list[Any] = []
    original = ssl.SSLContext.load_default_certs

    def counted(self: ssl.SSLContext, *args: Any, **kwargs: Any) -> None:
        reads.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", counted)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", lambda *a, **k: reads.append(a))
    clients = [policy_client(Reach.PUBLIC) for _ in range(5)]
    assert reads == []
    assert all(c._transport._pool._ssl_context is netpolicy._tls() for c in clients)  # type: ignore[attr-defined]


def _certificates() -> list[str]:
    """Two certificates in PEM, from the CA bundle httpx brings."""
    import certifi

    with open(certifi.where()) as bundle:
        text = bundle.read()
    blocks = text.split("-----END CERTIFICATE-----")
    pems = [b[b.index("-----BEGIN CERTIFICATE-----") :] + "-----END CERTIFICATE-----\n"
            for b in blocks if "-----BEGIN CERTIFICATE-----" in b]  # fmt: skip
    return pems[:2]


def _subjects(context: ssl.SSLContext) -> set[str]:
    return {
        str(dict(part for rdn in cert["subject"] for part in rdn))  # type: ignore[misc]
        for cert in context.get_ca_certs()
    }


@pytest.mark.anyio
async def test_a_changed_trust_store_is_read_again_into_a_new_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An updated CA bundle: the clients built after it use a context read from it, off the
    event loop; the context the earlier clients use is left as it was."""
    first, second = _certificates()
    bundle = tmp_path / "ca.pem"
    bundle.write_text(first)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(netpolicy, "_TLS", None)
    await netpolicy.refresh_tls()
    before = netpolicy._tls()
    old_client = policy_client(Reach.PUBLIC)
    await netpolicy.refresh_tls()
    assert netpolicy._tls() is before  # nothing changed: nothing read again
    bundle.write_text(second + "\n")
    stamp = bundle.stat().st_mtime + 10
    os.utime(bundle, (stamp, stamp))
    await netpolicy.refresh_tls()
    after = netpolicy._tls()
    assert after is not before
    assert _subjects(after) != _subjects(before) and len(_subjects(after)) == 1
    assert old_client._transport._pool._ssl_context is before  # type: ignore[attr-defined]
    assert policy_client(Reach.PUBLIC)._transport._pool._ssl_context is after  # type: ignore[attr-defined]
