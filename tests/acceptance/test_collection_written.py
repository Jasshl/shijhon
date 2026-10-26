"""Songs Shijhon writes join the library lists from apps' own requests.

The library index was prepared once, at a sign-in, as an owner's was long ago. Then only
apps are used: Shijhon completes an album and adds catalog albums a listener favorited,
and the new songs come into the lists, search and shuffle from the apps' requests alone -
no dashboard, no native website - within the documented time; a bulk write is one
addition to the index, not one per release.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from shijhon.catalog.model import release_from_data
from tests.acceptance.test_collection_reasons import listed, searched, shuffled, synced
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.library import Album, Track, write_album
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient

OWNED = Album(
    "Jonah Fairbanks",
    "wild stones",
    (Track("FROZEN", 1, isrc="ZZSHJ0000087"), Track("violet", 2, isrc="ZZSHJ0000088")),
    recording_date="2010",
)
FILLED = release_from_data(fixture("album-ep-without-suffix")["body"])
BULK = [
    catalog_release(f"written-{n}", f"Written {n}", f"Invented Writer {n}", 3) for n in (1, 2, 3)
]
SECONDS = 60.0  # the documented time, at most: a check, then one addition


@dataclass
class World:
    world: DeliveryWorld
    album: str  # the owned album the catalog completes

    def written(self, *, check: float = 0.2, gap: float = SECONDS) -> None:
        """The checks' timing for a test: frequent checks, the production gap."""
        written = self.world.services.written
        assert written is not None

        async def set_timing() -> None:
            assert written is not None
            written.check_seconds, written.gap_seconds = check, gap
            written._checked = written._added = -float("inf")

        self.world.server.call(set_timing)

    @property
    def additions(self) -> int:
        written = self.world.services.written
        assert written is not None
        return written.additions


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    nd.scan(full=True)
    found = nd.client().ok("search3", {"query": OWNED.title, "artistCount": 0, "songCount": 0})
    [album] = [a["id"] for a in found["searchResult3"]["album"]]
    with delivery_world(nd, tmp_path_factory.mktemp("written"), catalog=Replay().catalog()) as w:
        prepare_collection(nd, w.server)  # once, long ago: the index's first snapshot
        yield World(w, album)


def until(client: SubsonicClient, wanted: set[str], deadline: float) -> None:
    """Sync until ``wanted`` is listed; every answer meanwhile is a list (no "try again":
    the lists cached before the addition stay valid)."""
    params = {"query": "", "artistCount": 0, "albumCount": 0, "songCount": 500}
    while True:
        answer = client.request("search3", params)
        assert answer.status_code == 200, answer.text
        body = answer.json()["subsonic-response"]
        assert body["status"] == "ok", body
        if wanted <= {s["id"] for s in body["searchResult3"].get("song", [])}:
            return
        assert time.monotonic() < deadline, "the new songs did not come into the lists"
        time.sleep(0.2)


def test_a_completed_album_s_new_songs_come_from_app_requests_alone(world: World) -> None:
    client = world.world.client()
    try:
        world.written()
        before = synced(client)
        engine = world.world.services.engine

        async def fill() -> list[str]:
            album = await engine.owned_album(world.album)
            links = {t.ref: album.songs[t.number - 1]["id"] for t in FILLED.tracks[:2]}
            result = await engine.materialize(FILLED, owned_album_id=world.album, links=links)
            return [result.created[t.ref] for t in FILLED.tracks[2:]]

        added = set(world.world.server.call(fill))
        assert len(added) == 7 and not added & before
        additions = world.additions
        until(client, added, time.monotonic() + SECONDS)
        assert world.additions == additions + 1
        assert added <= shuffled(client)
        assert world.album in listed(client)
        title = FILLED.tracks[4].title
        album = client.ok("getAlbum", {"id": world.album})["album"]["song"]
        song = next(s["id"] for s in album if s["title"] == title)
        assert song in added
        assert [s["id"] for s in searched(client, title) if s["title"] == title] == [song]
        assert world.additions == additions + 1  # nothing left to add
    finally:
        client.close()


def test_a_bulk_write_is_one_addition(world: World) -> None:
    client = world.world.client()
    try:
        world.written()
        synced(client)  # the lists, as before the writes
        additions = world.additions
        new: set[str] = set()
        for release in BULK:  # one after another, as a playlist's releases are added
            created = world.world.materialize(release).created
            songs = [created[t.ref] for t in release.tracks]
            client.ok("star", {"id": songs[0]})  # a favorite: the album belongs in All
            new |= set(songs)
        until(client, new, time.monotonic() + SECONDS)
        for _ in range(5):
            synced(client)
        assert world.additions == additions + 1
    finally:
        client.close()
