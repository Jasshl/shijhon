"""Suite I (continued) - refresh from the current catalog.

A filled album is refreshed from its release as the catalog lists it now: tracks it
gained are added to the same album, changed tracks are rewritten in place (song IDs and
favorites stay), nothing is removed - tracks the release no longer lists stay - and a
track that would take another song's position is left out. A release of another catalog
is not refreshed.

The library pass does less of this by itself for the releases in the library, each from
time to time: tracks the catalog added become placeholders where no song is, and a silent
placeholder gets a length the catalog corrected. Nothing else changes - titles, credits,
ISRCs, track numbers, song IDs, owned files and delivered audio stay, and what the catalog
lists differently is counted as left alone. Its dry run only counts; a catalog that fails
leaves everything as it is.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.fill.refresh import Refreshed
from shijhon.fill.track_lists import DAY, TrackLists
from tests.conftest import NavidromeFactory
from tests.harness.collection import prepare_collection
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
# The library pass's readings: an owned album a 19-track release fills, and catalog albums.
SOUNDTRACK = Album(
    "Uma Okafor",
    "Bright Wolves 2 (Original Motion Picture Soundtrack)",
    (Track("Northern Machines", 11, seconds=84),),
    recording_date="2012",
)
SOUNDTRACK_PATH = "albums/900000182"
ADDED = "albums/900000203"  # 13 tracks
DRY = "albums/900000133"  # 9 tracks
SINGLE = "albums/900000145"  # one track
SECOND = "albums/900000101"  # 14 tracks


@pytest.fixture(scope="module")
def replay() -> Replay:
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    replay.aliases[f"uma okafor {SOUNDTRACK.title.lower()}"] = "restless voices"
    return replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    write_album(nd.music, SOUNDTRACK)
    nd.scan(full=True)
    fill = {"enabled": True, "auto_min_songs": 1, "library_pass": "off",
            "background_pause_seconds": 0, "open_budget_seconds": 15,
            "pass_requests_per_second": 50}  # fmt: skip
    with delivery_world(
        nd, tmp_path_factory.mktemp("refresh"), catalog=replay.catalog(), fill=fill
    ) as w:
        prepare_collection(nd, w.server)
        yield w


def album_id(world: DeliveryWorld, title: str = OWNED.title) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0})
    return str(found["searchResult3"]["album"][0]["id"])


def songs(world: DeliveryWorld, album: str) -> dict[str, dict[str, Any]]:
    """The album's songs by title."""
    return {s["title"]: s for s in world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]}


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
    before = songs(world, album)
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
    after = world.nd.client().ok("getAlbum", {"id": album})["album"]["song"]
    assert [s["track"] for s in after] == list(range(1, 15))
    assert after[1]["title"] == "Inserted Letter"
    for song in after[2:]:
        assert song["id"] == before[song["title"]]["id"]
        assert song["track"] == before[song["title"]]["track"] + 1
    assert after[0]["id"] == before[first]["id"] and after[0]["title"] == first


# --- the library pass reads added releases' track lists again ---------------------------


def track_lists(world: DeliveryWorld) -> TrackLists:
    library_pass = world.services.library_pass
    assert library_pass is not None and library_pass.track_lists is not None
    return library_pass.track_lists


def only_due(world: DeliveryWorld, *refs: str, days: float = 31.0) -> None:
    """The clock ``days`` on, and every release but ``refs`` read again just then (the
    module's other releases are not this test's)."""
    lists = track_lists(world)
    at = time.time() + days * DAY
    lists.clock = lambda: at

    async def settle() -> None:
        await lists.store.execute(
            "INSERT INTO release_refreshes (ref, checked_at, outcome)"  # noqa: S608
            f" SELECT ref, ?, 'same' FROM releases WHERE ref NOT IN ({', '.join('?' * len(refs))})"
            " ON CONFLICT (ref) DO UPDATE SET checked_at = excluded.checked_at,"
            " outcome = 'same'",
            [at, *refs],
        )

    world.server.call(settle)


def read_again(
    world: DeliveryWorld, *refs: str, dry_run: bool = False, days: float = 31.0
) -> dict[str, int] | None:
    """A run of the track lists ``days`` from now, with only ``refs`` due."""
    only_due(world, *refs, days=days)
    lists = track_lists(world)
    return world.server.call(lambda: lists.run(dry_run=dry_run, run_seconds=6 * 3600))


def run_pass(world: DeliveryWorld, mode: str) -> None:
    library_pass = world.services.library_pass
    assert library_pass is not None
    library_pass.mode = mode  # type: ignore[assignment]

    async def run() -> None:
        assert await library_pass.run_once() is not None

    try:
        world.server.call(run)
    finally:
        library_pass.mode = "off"


def commit(world: DeliveryWorld, replay: Replay, path: str) -> tuple[str, str]:
    """A catalog album added by a star of its first song: (its release, its album ID)."""
    rows = replay.by_path[path]["body"]["tracks"]
    world.client().ok("star", {"id": f"sh.tr.demo.{rows[0]['ref']['id']}"})
    ref = f"demo:{path.partition('/')[2]}"
    return ref, str(release_row(world, ref)["album_id"])


def release_row(world: DeliveryWorld, ref: str) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        row = await world.services.store.fetchone("SELECT * FROM releases WHERE ref = ?", [ref])
        assert row is not None
        return dict(row)

    return world.server.call(read)


def reading(world: DeliveryWorld, ref: str) -> dict[str, Any] | None:
    async def read() -> dict[str, Any] | None:
        row = await world.services.store.fetchone(
            "SELECT outcome, added, updated, left_out, reason FROM release_refreshes WHERE ref = ?",
            [ref],
        )
        return dict(row) if row else None

    return world.server.call(read)


def gained(row: dict[str, Any], ident: str, title: str, number: int) -> dict[str, Any]:
    """A track the catalog lists now, made from another of the release."""
    track = copy.deepcopy(row)
    track["ref"]["id"] = ident
    return track | {"title": title, "number": number, "isrc": f"ZZSHJ{ident[-7:]}"}


def test_the_pass_adds_tracks_and_corrects_lengths_and_nothing_else(
    world: DeliveryWorld, replay: Replay
) -> None:
    ref, album = commit(world, replay, ADDED)
    before = songs(world, album)
    assert len(before) == 13
    record = replay.by_path[ADDED]
    rows = record["body"]["tracks"]
    titles = [row["title"] for row in rows]
    delivered, renamed, lengthened, recoded = titles[0], titles[2], titles[3], titles[4]
    client = world.client()
    addon = world.addon("Reader")
    world.add_source(addon)
    try:
        addon.add(FakeTrack(isrc=rows[0]["isrc"], audio=world.audio("i-read-delivered")))
        assert client.request("download", {"id": before[delivered]["id"]}).status_code == 200
    finally:
        world.clear_sources()
    client.ok("star", {"id": before[renamed]["id"]})

    # Not before its time: the catalog is not asked.
    asked = replay.log.count(ADDED)
    assert read_again(world, ref, days=29) == {}
    assert replay.log.count(ADDED) == asked and reading(world, ref) is None

    original = copy.deepcopy(record)
    rows[0]["title"] = f"{delivered} (Renamed)"  # has audio: kept as it is
    rows[0]["duration_ms"] += 7000
    rows[2]["title"] = f"{renamed} (Corrected)"  # not a length: left as it is
    rows[3]["duration_ms"] += 5000
    rows[4]["isrc"] = "ZZSHJ0000981"  # not a length either
    for row in rows[1:]:  # the catalog numbers them anew around a track in the middle
        row["number"] += 1
    rows.insert(1, gained(rows[1], "900000993", "Inserted Song", 2))
    rows.append(gained(rows[-1], "900000994", "Bonus Song", 15))
    try:
        counts = read_again(world, ref)
        asked = replay.log.count(ADDED)
        again = read_again(world, ref)  # read a moment ago: not asked
        assert replay.log.count(ADDED) == asked
        later = read_again(world, ref, days=62)  # a month on: read, nothing to do
        assert replay.log.count(ADDED) == asked + 1
    finally:
        replay.by_path[ADDED] = original
    assert counts == {"read": 1, "changed": 1, "added": 1, "updated": 1}
    assert again == {} and later == {"read": 1, "same": 1}
    # Left as they are: the song with audio, the twelve the catalog numbers (two of them
    # also titles or identifies) differently now, and the track put where another song is.
    assert reading(world, ref) == {
        "outcome": "same", "added": 0, "updated": 0, "left_out": 14, "reason": None
    }  # fmt: skip

    after = songs(world, album)
    assert len(after) == 14 and "Inserted Song" not in after  # (its place is another song's)
    assert after["Bonus Song"]["track"] == 15
    assert f"{renamed} (Corrected)" not in after and after[renamed].get("starred")
    assert after[lengthened]["duration"] == before[lengthened]["duration"] + 5
    assert after[delivered]["duration"] == 3  # its audio's, not the catalog's new length
    for title, song in before.items():  # every song where it was, under its ID and title
        assert (after[title]["id"], after[title]["track"]) == (song["id"], song["track"]), title

    async def recorded() -> tuple[str | None, dict[str, int]]:
        store = world.services.store
        row = await store.fetchone(
            "SELECT isrc FROM placeholders WHERE song_id = ?", [before[recoded]["id"]]
        )
        assert row is not None
        places = await store.fetchall(
            "SELECT track_ref, track FROM placeholders WHERE release_ref = ?", [ref]
        )
        return row["isrc"], {str(r["track_ref"]): int(r["track"]) for r in places}

    isrc, places = world.server.call(recorded)
    assert isrc == original["body"]["tracks"][4]["isrc"]
    # The release's recorded data has what was applied, not what the catalog says besides:
    # the tracks where the library has them, with its titles, and the one that was added.
    data = json.loads(release_row(world, ref)["data"])
    listed = {f"demo:{t['ref']['id']}": t["number"] for t in data["tracks"]}
    assert {key: listed[key] for key in places} == places
    recorded_titles = {t["title"] for t in data["tracks"]}
    assert renamed in recorded_titles and "Bonus Song" in recorded_titles
    assert "Inserted Song" not in recorded_titles
    assert f"{renamed} (Corrected)" not in recorded_titles


def test_a_filled_album_gains_tracks_and_its_owned_files_stay(
    world: DeliveryWorld, replay: Replay
) -> None:
    album = album_id(world, SOUNDTRACK.title)
    assert world.services.fills is not None
    world.server.call(lambda: world.services.fills.fill_album(album, "test"))  # type: ignore[union-attr]
    assert len(songs(world, album)) == 19
    ref = f"demo:{SOUNDTRACK_PATH.partition('/')[2]}"
    assert release_row(world, ref)["owned_album_id"] == album
    # A file the owner adds to the album afterwards: a song the release does not know.
    extra = Album(SOUNDTRACK.artist, SOUNDTRACK.title, (Track("Hidden Letter", 25, seconds=5),),
                  recording_date="2012")  # fmt: skip
    write_album(world.nd.music, extra)
    world.nd.scan()
    before = songs(world, album)
    assert len(before) == 20
    files = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (world.nd.music / SOUNDTRACK.relative_folder).iterdir()
        if path.suffix == ".flac"
    }
    assert len(files) == 2

    record = replay.by_path[SOUNDTRACK_PATH]
    original = copy.deepcopy(record)
    rows = record["body"]["tracks"]
    by_number = {row["number"]: row for row in rows}
    placeholder, retitled = by_number[3]["title"], by_number[5]["title"]
    assert len({placeholder, retitled, by_number[11]["title"]}) == 3
    by_number[11]["title"] += " (Retitled)"  # the owner's file is this track
    by_number[11]["duration_ms"] += 9000
    by_number[3]["duration_ms"] += 6000
    by_number[5]["title"] = f"{retitled} (Corrected)"  # not a length: left as it is
    rows.append(gained(rows[-1], "900000995", "Bonus Machines", 20))
    rows.append(gained(rows[-1], "900000996", "Hidden Letter", 21))
    try:
        counts = read_again(world, ref)
    finally:
        replay.by_path[SOUNDTRACK_PATH] = original
    assert counts == {"read": 1, "changed": 1, "added": 1, "updated": 1}
    found = reading(world, ref)
    assert found is not None and found["left_out"] == 2  # the title, the owner's "Hidden Letter"

    after = songs(world, album)
    assert len(after) == 21 and after["Bonus Machines"]["albumId"] == album
    assert after[placeholder]["duration"] == before[placeholder]["duration"] + 6
    for owned in ("Northern Machines", "Hidden Letter"):  # as Navidrome had them
        assert after[owned] == before[owned]
    assert files == {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in files
    }
    for title, song in before.items():
        assert (after[title]["id"], after[title]["track"]) == (song["id"], song["track"]), title


def test_a_dry_run_counts_and_the_pass_switched_on_applies(
    world: DeliveryWorld, replay: Replay
) -> None:
    ref, album = commit(world, replay, DRY)
    before = songs(world, album)
    record = replay.by_path[DRY]
    original = copy.deepcopy(record)
    rows = record["body"]["tracks"]
    lengthened = rows[1]["title"]
    rows[1]["duration_ms"] += 4000
    rows.append(gained(rows[-1], "900000997", "Bonus Stone", len(rows) + 1))
    try:
        only_due(world, ref)
        run_pass(world, "dry_run")
        assert songs(world, album) == before  # nothing written
        assert reading(world, ref) == {
            "outcome": "pending", "added": 1, "updated": 1, "left_out": 0, "reason": None
        }  # fmt: skip
        asked = replay.log.count(DRY)
        run_pass(world, "dry_run")  # read a moment ago: a dry run does not ask again
        assert replay.log.count(DRY) == asked
        only_due(world, ref)  # (the pass's own albums' releases aside)
        run_pass(world, "on")  # ... and the pass switched on applies it at once
        assert replay.log.count(DRY) == asked + 1
    finally:
        replay.by_path[DRY] = original
    found = reading(world, ref)
    assert found is not None and (found["outcome"], found["added"], found["updated"]) == (
        "changed", 1, 1
    )  # fmt: skip
    after = songs(world, album)
    assert len(after) == len(before) + 1 and "Bonus Stone" in after
    assert after[lengthened]["id"] == before[lengthened]["id"]
    assert after[lengthened]["duration"] == before[lengthened]["duration"] + 4


def test_the_setting_off_asks_the_catalog_nothing(world: DeliveryWorld, replay: Replay) -> None:
    ref, _ = commit(world, replay, DRY)
    lists = track_lists(world)
    asked = len(replay.log)
    lists.enabled = False
    try:
        assert read_again(world, ref, days=95) == {}
    finally:
        lists.enabled = True
    assert len(replay.log) == asked


def test_a_failing_catalog_leaves_everything_as_it_is(world: DeliveryWorld, replay: Replay) -> None:
    first, album = commit(world, replay, SINGLE)
    second, _ = commit(world, replay, SECOND)
    before = songs(world, album)
    fills = world.services.fills
    assert fills is not None
    replay.failing[SINGLE] = 503
    try:
        counts = read_again(world, first, second)
        resting = fills.resting
    finally:
        del replay.failing[SINGLE]
        fills.resting_until = 0.0
    # The catalog rests, the run ends there: the next release is not asked for.
    assert counts == {} and resting
    assert replay.log.count(SECOND) == 1  # (its commit)
    found = reading(world, first)
    assert found is not None and found["outcome"] == "failed" and reading(world, second) is None
    assert songs(world, album) == before
    # Not asked again within the day, then again.
    asked = replay.log.count(SINGLE)
    assert read_again(world, first) == {} and replay.log.count(SINGLE) == asked
    assert read_again(world, first, days=32.5) == {"read": 1, "same": 1}
    # A release the catalog no longer has stays as it is.
    replay.failing[SINGLE] = 404
    try:
        assert read_again(world, first, days=64) == {"gone": 1}
    finally:
        del replay.failing[SINGLE]
    found = reading(world, first)
    assert found is not None and found["outcome"] == "gone"
    assert songs(world, album) == before and not fills.resting


def test_another_release_under_the_same_id_adds_nothing(
    world: DeliveryWorld, replay: Replay
) -> None:
    """An answer that lists none of the release's tracks is not this release."""
    ref, album = commit(world, replay, SINGLE)
    before = songs(world, album)
    record = replay.by_path[SINGLE]
    original = copy.deepcopy(record)
    row = record["body"]["tracks"][0]
    record["body"]["tracks"] = [
        gained(row, "900000998", "Someone Else's Song", 2),
        gained(row, "900000999", "Someone Else's Other Song", 3),
    ]
    try:
        assert read_again(world, ref, days=130) == {"read": 1, "refused": 1}
    finally:
        replay.by_path[SINGLE] = original
    found = reading(world, ref)
    assert found is not None and found["reason"] == (
        "the catalog lists none of the release's tracks any more"
    )
    assert songs(world, album) == before
