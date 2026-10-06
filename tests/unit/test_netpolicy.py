from __future__ import annotations

import pytest

from shijhon.delivery.netpolicy import Reach, allowed


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
