"""Suite I (continued) - refresh from the current catalog.

A filled album is refreshed from its release as the catalog lists it now: tracks it
gained are added to the same album, changed tracks are rewritten in place (song IDs and
favorites stay), nothing is removed - tracks the release no longer lists stay - and a
track that would take another song's position is left out. A release of another catalog
is not refreshed.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.fill.refresh import Refreshed
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.replay import Replay

OWNED = Album(
    "Neve Ashdown",
    "Northern Letters (Remastered)",
    (Track("Velvet Tides", 11, seconds=67), Track("Crimson Canyon", 17, seconds=24)),
    recording_date="1962",
)
RELEASE_PATH = "albums/900000021"  # the fixture release that fills it
CATALOG_ONLY = "albums/900000296"  # a 13-track album nobody owns a song of


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    nd.scan(full=True)
    fill = {"enabled": True, "auto_min_songs": 1, "library_pass": "off",
            "background_pause_seconds": 0, "open_budget_seconds": 15}  # fmt: skip
    with delivery_world(
        nd, tmp_path_factory.mktemp("refresh"), catalog=replay.catalog(), fill=fill
    ) as w:
        yield w


def album_id(world: DeliveryWorld) -> str:
    found = world.nd.client().ok("search3", {"query": OWNED.title, "artistCount": 0})
    return str(found["searchResult3"]["album"][0]["id"])


def songs(world: DeliveryWorld, album: str) -> dict[str, dict[str, Any]]:
    """The album's songs by title."""
    return {s["title"]: s for s in world.client().ok("getAlbum", {"id": album})["album"]["song"]}


def refresh(world: DeliveryWorld, album: str) -> list[Refreshed]:
    assert world.services.refresh is not None
    return world.server.call(lambda: world.services.refresh.refresh(album))  # type: ignore[union-attr]


def test_a_refresh_adds_updates_and_removes_nothing(world: DeliveryWorld, replay: Replay) -> None:
    album = album_id(world)
    assert world.services.fills is not None
    decision = world.server.call(lambda: world.services.fills.fill_album(album, "test"))  # type: ignore[union-attr]
    assert decision is not None and decision.outcome == "filled"
    before = songs(world, album)
    assert len(before) == 17
    client = world.client()
    renamed, lengthened, dropped = "Crimson Mirrors", list(before)[1], list(before)[2]
    client.ok("star", {"id": before[renamed]["id"]})
    client.ok("scrobble", {"id": before[dropped]["id"], "submission": "true"})

    # The catalog now lists the release differently.
    record = replay.by_path[RELEASE_PATH]
    original = copy.deepcopy(record)
    rows = record["body"]["tracks"]
    by_name = {r["title"]: r for r in rows}
    by_name[renamed]["title"] = "Crimson Mirrors (Single Edit)"
    by_name[lengthened]["duration_ms"] += 4000
    rows.remove(by_name[dropped])
    gained = copy.deepcopy(by_name[renamed])
    gained["ref"]["id"] = "900000990"
    gained |= {"title": "Bonus Letter", "number": 18, "isrc": "ZZSHJ0000990"}
    clash = copy.deepcopy(gained)
    clash["ref"]["id"] = "900000991"
    clash |= {"title": "Clashing Letter", "number": 11, "isrc": "ZZSHJ0000991"}
    rows += [gained, clash]
    try:
        [result] = refresh(world, album)
        [again] = refresh(world, album)  # the same release again: nothing to do
    finally:
        replay.by_path[RELEASE_PATH] = original
    assert (again.added, again.updated) == (0, 0)
    assert result.refused is None
    assert (result.added, result.updated) == (1, 2)
    assert result.kept == [dropped]
    assert result.skipped == ["Clashing Letter: its position is another song's"]

    after = songs(world, album)
    assert len(after) == 18  # one album: 17 as before, the dropped one kept, one added
    assert "Bonus Letter" in after and dropped in after
    assert after["Crimson Mirrors (Single Edit)"]["id"] == before[renamed]["id"]  # in place
    assert after["Crimson Mirrors (Single Edit)"].get("starred")
    assert after[lengthened]["id"] == before[lengthened]["id"]
    assert after[lengthened]["duration"] == before[lengthened]["duration"] + 4
    assert after[dropped]["id"] == before[dropped]["id"]
    assert after[dropped].get("playCount") == 1
    for owned in ("Velvet Tides", "Crimson Canyon"):
        assert after[owned]["id"] == before[owned]["id"]


def test_a_release_of_another_catalog_is_not_refreshed(world: DeliveryWorld) -> None:
    release = catalog_release("i-refresh-other", "Other Catalog", "Other Artist", 2)
    made = world.materialize(release)
    [result] = refresh(world, made.album_id)
    assert result.refused == "a release of another catalog (test)"
    assert refresh(world, "no-such-album") == []


def test_a_catalog_album_s_renumbering_is_applied_as_a_whole(
    world: DeliveryWorld, replay: Replay
) -> None:
    """A track inserted at position 2: every later placeholder moves one down (positions
    the release's own placeholders free are taken by the others), the new one is added,
    song IDs stay; delivered audio is kept as it is."""
    record = replay.by_path[CATALOG_ONLY]
    rows = record["body"]["tracks"]
    client = world.client()
    client.ok("star", {"id": f"sh.tr.demo.{rows[4]['ref']['id']}"})  # a commit adds the album

    async def album_of() -> str:
        row = await world.services.store.fetchone(
            "SELECT album_id FROM releases WHERE ref = 'demo:900000296'"
        )
        assert row is not None
        return str(row["album_id"])

    album = world.server.call(album_of)
    before = {s["title"]: s for s in client.ok("getAlbum", {"id": album})["album"]["song"]}
    assert len(before) == 13
    first = rows[0]["title"]
    addon = world.addon("Deliverer")
    world.add_source(addon)
    try:
        isrc = rows[0]["isrc"]
        addon.add(FakeTrack(isrc=isrc, audio=world.audio("i-refresh-delivered")))
        assert client.request("download", {"id": before[first]["id"]}).status_code == 200
    finally:
        world.clear_sources()
    original = copy.deepcopy(record)
    inserted = copy.deepcopy(rows[1])
    inserted["ref"]["id"] = "900000992"
    inserted |= {"title": "Inserted Letter", "isrc": "ZZSHJ0000992"}
    for row in rows[1:]:
        row["number"] += 1
    rows[0]["title"] = first + " (Renamed)"  # delivered: kept as it is
    rows.insert(1, inserted)
    try:
        [result] = refresh(world, album)
    finally:
        replay.by_path[CATALOG_ONLY] = original
    assert (result.added, result.updated) == (1, 12)
    assert result.skipped == [f"{first} (Renamed): delivered audio is kept as it is"]
    after = client.ok("getAlbum", {"id": album})["album"]["song"]
    assert [s["track"] for s in after] == list(range(1, 15))
    assert after[1]["title"] == "Inserted Letter"
    for song in after[2:]:
        assert song["id"] == before[song["title"]]["id"]
        assert song["track"] == before[song["title"]]["track"] + 1
    assert after[0]["id"] == before[first]["id"] and after[0]["title"] == first
