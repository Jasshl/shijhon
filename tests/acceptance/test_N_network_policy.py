"""Suite N — network policy.

Private, loopback and link-local destinations are denied — checked after DNS resolution
and on every redirect hop — unless an add-on is explicitly allowed; an explicitly allowed
loopback add-on works (every other delivery suite relies on that).
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from shijhon.delivery.netpolicy import Reach
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("n")) as w:
        yield w


def failed(world: DeliveryWorld, song: str) -> str:
    body = world.client().request("stream", {"id": song}).json()["subsonic-response"]
    assert body["status"] == "failed"
    return str(body["error"]["message"])


def test_public_only_addon_on_loopback_is_denied(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon("Loopy")
    world.add_source(addon, reach=Reach.PUBLIC)
    song, _, _ = world.placeholder_track("n-public", [addon])
    assert "not allowed" in failed(world, song)
    assert addon.requests() == []  # never contacted


def test_explicit_loopback_allowance_works(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon("Local")
    world.add_source(addon, reach=Reach.LOOPBACK)
    song, _, audio = world.placeholder_track("n-loopback", [addon])
    assert world.client().request("stream", {"id": song}).content == audio.read_bytes()


@pytest.mark.parametrize(
    "host,reach",
    [
        ("private.fake.test", Reach.LOOPBACK),  # 10.20.30.40
        ("metadata.fake.test", Reach.PRIVATE),  # 169.254.169.254, never allowed
    ],
)
def test_audio_url_to_forbidden_address_is_denied(
    world: DeliveryWorld, host: str, reach: Reach
) -> None:
    world.clear_sources()
    addon = world.addon(f"Pointing-{host}")
    world.add_source(addon, reach=reach)
    song, _, _ = world.placeholder_track(f"n-url-{host}", [addon], url_host=host)
    started = time.monotonic()
    assert "not allowed" in failed(world, song)
    assert time.monotonic() - started < 2.0  # refused before any connection attempt


def test_redirect_to_forbidden_address_is_denied(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon("Redirecting")
    world.add_source(addon, reach=Reach.LOOPBACK)
    song, _, _ = world.placeholder_track(
        "n-redirect", [addon], redirect=True, redirect_host="metadata.fake.test"
    )
    assert "not allowed" in failed(world, song)
    assert addon.requests("redirect") and not addon.requests("audio")


def test_addon_host_resolving_to_forbidden_address_is_denied(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon("Hidden")
    base = addon.base_url.replace("addon.fake.test", "metadata.fake.test")
    world.add_source(addon, reach=Reach.PRIVATE, base_url=base)
    song, _, _ = world.placeholder_track("n-hidden", [addon])
    assert "not allowed" in failed(world, song)
    assert addon.requests() == []
