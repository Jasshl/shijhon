"""Source configuration changes (the dashboard's edits) take effect at once."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shijhon.delivery.netpolicy import Reach
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("cfg")) as w:
        yield w


def test_update_order_and_enablement(world: DeliveryWorld) -> None:
    first, second = world.addon("First"), world.addon("Second")
    a = world.add_source(first, reach=Reach.PUBLIC)  # the loopback add-on is refused at first
    b = world.add_source(second)
    song, _, audio = world.placeholder_track("cfg-1", [first, second])
    sources = world.services.sources

    assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
    assert not first.requests() and second.requests("audio")

    world.server.call(lambda: sources.update(a, reach=Reach.LOOPBACK, budget_seconds=5))
    world.services.deliverer.forget(song)
    second.clear()
    assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
    assert first.requests("audio") and not second.requests()

    world.server.call(lambda: sources.reorder([b, a]))
    world.services.deliverer.forget(song)
    first.clear()
    world.client().request("stream", {"id": song})
    assert second.requests("audio") and not first.requests()

    world.server.call(lambda: sources.set_enabled(b, False))
    world.services.deliverer.forget(song)
    second.clear()
    world.client().request("stream", {"id": song})
    assert first.requests("audio") and not second.requests()
    enabled = world.server.call(sources.enabled)
    assert [s.name for s in enabled] == ["First"] and enabled[0].budget == 5
