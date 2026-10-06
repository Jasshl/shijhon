"""Suite J (continued) - catalog covers fetched ahead.

When a client is answered an artist page or search results with catalog items, the
covers of the first of them are fetched in the background, a few at a time, at the size
this client asks for covers (known from its cover requests), from the artwork the answer
carried - never with a catalog request - into the disk cache, where the client's own
requests then find them. Nothing is fetched ahead for a client whose size is not known,
nor for a song-only search.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator

import pytest

from shijhon.catalog.model import CatalogRef
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_USER
from tests.harness.replay import Replay, fixture

HOST = {"host": "music.test"}
ARTIST = "Oren Garrow"  # the fixtures' artist with several releases; the library has one
AHEAD = 5  # covers fetched ahead per answer (the setting here)


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases[ARTIST.lower()] = str(fixture("search-single-vs-album")["params"]["term"])
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, Album(ARTIST, "Hollow 2", (Track("One", 1),), recording_date="1995"))
    nd.scan(full=True)
    covers = {"prefetch_covers": AHEAD, "prefetch_parallel": 2}
    with delivery_world(
        nd, tmp_path_factory.mktemp("covers"), catalog=replay.catalog(), covers=covers
    ) as w:
        yield w


def artist_id(world: DeliveryWorld) -> str:
    found = world.nd.client().ok("search3", {"query": ARTIST, "albumCount": 0, "songCount": 0})
    [artist] = [a["id"] for a in found["searchResult3"]["artist"] if a["name"] == ARTIST]
    return str(artist)


def settled(world: DeliveryWorld) -> int:
    """Until nothing is fetched ahead any more; how many were."""
    prefetch = world.services.views.prefetch  # type: ignore[union-attr]
    assert prefetch is not None
    deadline = time.monotonic() + 10
    while prefetch._queued and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not prefetch._queued
    return prefetch.fetched


def test_an_artist_page_s_covers_are_fetched_ahead_at_the_client_s_size(
    world: DeliveryWorld, replay: Replay
) -> None:
    client = world.client(headers=HOST, client="ahead")
    client.request("getCoverArt", {"id": "al-unknown", "size": "290"})  # its size: 300
    before, images = settled(world), replay.artwork_requests
    asked = len(replay.log)
    page = client.ok("getArtist", {"id": artist_id(world)})["artist"]
    added = [a for a in page["album"] if str(a["id"]).startswith("sh.al.")]
    assert len(added) > AHEAD
    assert settled(world) - before == AHEAD  # the first ones only
    assert replay.artwork_requests == images + AHEAD
    assert all("/300x300.png" in u for u in replay.artwork_urls[-AHEAD:])
    requests_for_page = len(replay.log)
    assert not [r for r in replay.log[asked:] if r.startswith("albums/")]  # no album request
    for album in added[:AHEAD]:  # the client's own requests find them on disk
        cover = client.request("getCoverArt", {"id": album["coverArt"], "size": "290"})
        assert cover.status_code == 200
        assert cover.headers["x-content-type-options"] == "nosniff"  # an image, as named
    assert replay.artwork_requests == images + AHEAD
    assert len(replay.log) == requests_for_page


def test_nothing_is_fetched_ahead_for_a_client_whose_size_is_not_known(
    world: DeliveryWorld, replay: Replay
) -> None:
    client = world.client(headers=HOST, client="new-client")
    before, images = settled(world), replay.artwork_requests
    client.ok("getArtist", {"id": artist_id(world)})
    assert settled(world) == before and replay.artwork_requests == images


def test_a_song_only_search_fetches_nothing_ahead(world: DeliveryWorld, replay: Replay) -> None:
    client = world.client(headers=HOST, client="songs")
    client.request("getCoverArt", {"id": "al-unknown", "size": "600"})
    before = settled(world)
    found = client.ok(
        "search3", {"query": ARTIST.lower(), "artistCount": 0, "albumCount": 0, "songCount": 20}
    )["searchResult3"]
    assert any(str(s["id"]).startswith("sh.tr.") for s in found.get("song", []))
    assert settled(world) == before


def test_a_client_walking_pages_gets_nothing_fetched_ahead(
    world: DeliveryWorld, replay: Replay
) -> None:
    client = world.client(headers=HOST, client="walker")
    client.request("getCoverArt", {"id": "al-unknown", "size": "150"})
    views = world.services.views
    assert views is not None and views.prefetch is not None and views.artwork is not None

    async def walk() -> None:  # four other pages with covers in a moment: walking pages
        assert views is not None and views.prefetch is not None and views.artwork is not None
        for n in range(4):
            art = f"https://covers.demo.invalid/walked{n}/{{w}}x{{h}}.png"
            views.artwork.note("al", CatalogRef("demo", f"walked-{n}"), art)
            page = [{"coverArt": f"al-sh.al.demo.walked-{n}"}]
            views.prefetch.page((ADMIN_USER, "walker"), page)

    world.server.call(walk)
    before = settled(world)
    client.ok("getArtist", {"id": artist_id(world)})
    assert settled(world) == before


def test_each_catalog_cover_request_can_be_logged(world: DeliveryWorld, replay: Replay) -> None:
    """At debug level (``log_debug = ["shijhon.covers"]``): the client, the size asked for
    and fetched at, how the address was learned, the image's time and bytes."""
    client = world.client(headers=HOST, client="logged")
    page = world.client(headers=HOST, client="other").ok("getArtist", {"id": artist_id(world)})
    album = [a for a in page["artist"]["album"] if str(a["id"]).startswith("sh.al.")][-1]
    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger("shijhon.covers")
    handler, level = Keep(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        for _ in range(2):
            client.request("getCoverArt", {"id": album["coverArt"], "size": "777"})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    fetched, kept = (line for line in lines if line.startswith("cover al c=logged"))
    assert re.fullmatch(
        r"cover al c=logged size 777->800: address known \d\.\d{3}s; image \d\.\d{3}s;"
        r" \d+ bytes in \d\.\d{3}s",
        fetched,
    ), fetched
    assert re.fullmatch(r"cover al c=logged size 777->800: kept on disk; \d+ bytes in .*", kept)
