"""Suite I (continued) - what the library pass's reading of a track list must not do,
against a real Navidrome.

- A track the catalog lists under a new ID - at another number, on another disc - is the
  song the album has: it is not added beside itself, however often the catalog changes
  its answer.
- A credits repair that read its tracks before the reading and writes after it keeps what
  the reading corrected: it changes the credits of the song as it is then.
- A picked album (its release numbered differently from the owned files) gains a new
  track, but none on an owned song's number; a placeholder a part's own file plays for
  keeps that file when its length is corrected.

The stops in the middle of these are in ``test_R_stops.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack
from shijhon.placeholders.credits import CreditRepair
from tests.acceptance.test_I_auto_pick import (
    ESME,
    PART,
    WHOLE,
    album_id,
    replay_for_picks,
    songs,
    view,
    wait_for,
    world_with,
)
from tests.acceptance.test_I_refresh import gained, read_again, reading
from tests.acceptance.test_R_stops import assert_in_step
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld
from tests.harness.engine import EngineParts, catalog_release, engine_for, track_lists_for
from tests.harness.library import Album, Track
from tests.harness.navidrome import NavidromeInstance
from tests.harness.replay import Replay

pytestmark = pytest.mark.anyio

RUN_SECONDS = 6 * 3600.0
NEW_ID = "a song of the album under a new ID at the catalog; it stays as it is"


def renamed(track: CatalogTrack, ident: str, **place: int) -> CatalogTrack:
    """The track as the catalog lists it under another ID (and at another place)."""
    return replace(track, ref=CatalogRef("test", ident), **place)


async def placeholders(parts: EngineParts, release: CatalogRelease) -> dict[str, Any]:
    rows = await parts.store.fetchall(
        "SELECT * FROM placeholders WHERE release_ref = ?", [str(release.ref)]
    )
    return {str(row["track_ref"]): row for row in rows}


async def listed_songs(parts: EngineParts, album: str) -> list[tuple[str, int, int]]:
    found = await parts.navidrome.songs_of_album(album)
    return sorted(
        (str(s["title"]), int(s["discNumber"]), int(s["trackNumber"]))
        for s in found
        if not s.get("missing")
    )


# --- a track under a new ID --------------------------------------------------------------


@pytest.mark.parametrize("how", ["reading", "action"])
@pytest.mark.parametrize("moved", ["another number", "another disc"])
async def test_a_track_under_a_new_id_is_not_added_beside_itself(
    navidrome: NavidromeInstance, tmp_path: Path, how: str, moved: str
) -> None:
    """The catalog gave the album's second track a new ID and another place: the same
    recording (its ISRC), so the album keeps its two songs - by the pass's reading and by
    the refresh as an action alike."""
    key = f"{how}-{moved[-4:]}"  # (an album of its own in the module's Navidrome)
    release = catalog_release(f"lists-id-{key}", f"Review Album {key}", "Reviewer", 2)
    first, second = release.tracks
    place = {"number": 3} if moved == "another number" else {"disc": 2, "number": 1}
    listed = replace(release, tracks=(first, renamed(second, f"{release.ref.id}-new", **place)))
    async with engine_for(navidrome, tmp_path) as parts:
        made = await parts.engine.materialize(release)
        before = await listed_songs(parts, made.album_id)
        lists = await track_lists_for(parts.engine, parts.store, parts.navidrome, listed)
        if how == "reading":
            counts = await lists.run(dry_run=False, run_seconds=RUN_SECONDS)
            assert counts == {"read": 1, "same": 1}
            row = await parts.store.fetchone(
                "SELECT left_out FROM release_refreshes WHERE ref = ?", [str(release.ref)]
            )
            assert row is not None and row["left_out"] == 1
        else:
            [result] = await lists.refresh.refresh(made.album_id)
            assert (result.added, result.updated) == (0, 0)
            assert result.skipped == [f"{second.title}: {NEW_ID}"]
        assert await listed_songs(parts, made.album_id) == before
        assert before == [(first.title, 1, 1), (second.title, 1, 2)]
        rows = await placeholders(parts, release)
        assert {ref: row["song_id"] for ref, row in rows.items()} == {
            str(track.ref): made.created[track.ref] for track in release.tracks
        }
        assert len(await assert_in_step(parts, made.folder)) == 2


async def test_a_catalog_that_alternates_between_two_answers_adds_nothing_twice(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """One answer has the second track under a new ID at another number, and a track
    more; the other is the release as it was added. Reading after reading, the album has
    the track more once and the second track once."""
    release = catalog_release("lists-alternating", "Turning Album", "Turner", 3)
    first, second, third = release.tracks
    more = replace(
        renamed(third, f"{release.ref.id}-more", number=5),
        title="Turning Album Song More",
        isrc="QZ99926099999",
    )
    other = replace(
        release, tracks=(first, renamed(second, f"{release.ref.id}-new", number=4), third, more)
    )
    async with engine_for(navidrome, tmp_path) as parts:
        made = await parts.engine.materialize(release)
        seen = []
        for month, answer in enumerate([other, release, other, release, other], 1):
            lists = await track_lists_for(
                parts.engine, parts.store, parts.navidrome, answer, days=31 * month
            )
            counts = await lists.run(dry_run=False, run_seconds=RUN_SECONDS)
            assert counts is not None and counts["read"] == 1
            seen.append((counts.get("added", 0), await listed_songs(parts, made.album_id)))
        expected = [
            (first.title, 1, 1),
            (second.title, 1, 2),
            (third.title, 1, 3),
            (more.title, 1, 5),
        ]
        assert seen == [(1, expected)] + [(0, expected)] * 4
        assert len(await assert_in_step(parts, made.folder)) == 4


# --- a credits repair under way -----------------------------------------------------------


def items_for(credit: str, count: int) -> Any:
    async def credits(
        tracks: Sequence[CatalogTrack],
    ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
        return {
            t.ref: tuple(CatalogRef("test", f"{t.ref.id}-a{i}") for i in range(count))
            for t in tracks
            if t.artist == credit
        }

    return credits


@pytest.mark.parametrize("how", ["reading", "action"])
async def test_a_credits_repair_under_way_keeps_what_a_refresh_wrote_meanwhile(
    navidrome: NavidromeInstance, tmp_path: Path, how: str
) -> None:
    """The repair reads its tracks, waits for the catalog's artist items - meanwhile the
    release is refreshed - and then writes: the credits of the songs as they are by then,
    not the tracks it read (their old lengths, and for the action their old titles)."""
    joined = "Wren Hollow & Kestrel Day"
    release = catalog_release(  # (an album of its own in the module's Navidrome)
        f"lists-credits-{how}", f"Quiet Engines {how}", "Wren Hollow", 2, track_artist=joined
    )
    listed = replace(
        release,
        tracks=tuple(
            replace(t, title=f"{t.title} (Corrected)", duration_ms=t.duration_ms + 2000)
            for t in release.tracks
        ),
    )
    async with engine_for(navidrome, tmp_path) as parts:
        made = await parts.engine.materialize(release)
        lists = await track_lists_for(parts.engine, parts.store, parts.navidrome, listed)
        lookup = items_for(joined, 2)
        refreshed: list[int] = []

        async def credits(
            tracks: Sequence[CatalogTrack],
        ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
            if not refreshed:  # the repair has read its tracks and waits here
                if how == "reading":
                    counts = await lists.run(dry_run=False, run_seconds=RUN_SECONDS)
                    assert counts is not None
                    refreshed.append(counts["updated"])
                else:
                    [result] = await lists.refresh.refresh(made.album_id)
                    refreshed.append(result.updated)
            return await lookup(tracks)  # type: ignore[no-any-return]

        parts.engine.credits = credits
        repair = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        assert await repair.repair() == 2
        assert refreshed == [2]
        rows = await placeholders(parts, release)
        for was, now in zip(release.tracks, listed.tracks, strict=True):
            row = rows[str(was.ref)]
            title = now.title if how == "action" else was.title  # (the reading: lengths only)
            assert (row["title"], row["duration_ms"]) == (title, now.duration_ms)
            assert json.loads(row["tags"])["artists"] == ["Wren Hollow", "Kestrel Day"]
            assert row["song_id"] == made.created[was.ref]
            song = await parts.navidrome.song(str(row["song_id"]))
            assert song is not None
            assert (song["title"], round(song["duration"])) == (title, now.duration_ms // 1000)
            credited = navidrome.client().ok("getSong", {"id": row["song_id"]})["song"]
            assert [a["name"] for a in credited["artists"]] == ["Wren Hollow", "Kestrel Day"]
        assert len(await assert_in_step(parts, made.folder)) == 2
        assert await repair.repair() == 0  # nothing left to do


async def test_a_credits_repair_leaves_a_song_whose_credit_is_another_by_then(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """The artists the repair asked for belong to the credit it read. A song whose credit
    the refresh changed meanwhile does not get them; the other does."""
    joined = "Mira Sol & Ansel Grey"
    release = catalog_release(
        "lists-credits-other", "Other Credit", "Mira Sol", 2, track_artist=joined
    )
    first, second = release.tracks
    listed = replace(release, tracks=(replace(first, artist="Mira Sol"), second))
    async with engine_for(navidrome, tmp_path) as parts:
        made = await parts.engine.materialize(release)
        lists = await track_lists_for(parts.engine, parts.store, parts.navidrome, listed)
        lookup = items_for(joined, 2)
        refreshed: list[int] = []

        async def credits(
            tracks: Sequence[CatalogTrack],
        ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
            if not refreshed:
                [result] = await lists.refresh.refresh(made.album_id)  # (the action)
                refreshed.append(result.updated)
            return await lookup(tracks)  # type: ignore[no-any-return]

        parts.engine.credits = credits
        repair = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        assert await repair.repair() == 1 and refreshed == [1]
        rows = await placeholders(parts, release)
        alone, both = rows[str(first.ref)], rows[str(second.ref)]
        assert alone["artist"] == "Mira Sol" and "artists" not in json.loads(alone["tags"])
        assert json.loads(both["tags"])["artists"] == ["Mira Sol", "Ansel Grey"]
        # Nor do names found for the credit it read (the add-on catalogs' lists).
        named = replace(first, artists=("Mira Sol", "Ansel Grey"))
        assert await parts.engine.recredit(str(alone["song_id"]), named) is False
        rows = await placeholders(parts, release)
        assert "artists" not in json.loads(rows[str(first.ref)]["tags"])
        assert len(await assert_in_step(parts, made.folder)) == 2


# --- picks and parts ------------------------------------------------------------------------

# Two songs of "PALE RIVERS.", which the release has as 14 and 13: one on a number of its
# own (20), one on the number of a track the release adds (2).
PICKED = Album(
    ESME,
    "PALE RIVERS.",
    (Track("NORTHERN.", 20, seconds=118), Track("AMBER.", 2, seconds=187)),
    recording_date="2010",
)
PICKED_RELEASE = "demo:900000101"
PART_RELEASE = "demo:900000021"


def parts_of(world: DeliveryWorld) -> EngineParts:
    services = world.services
    engine = services.engine
    return EngineParts(
        engine, services.navidrome, services.scans, engine.silence, services.store, engine.layout
    )


def in_step(world: DeliveryWorld, ref: str) -> dict[str, Any]:
    """The release's folder, its rows and Navidrome agree (``assert_in_step``)."""

    async def check() -> dict[str, Any]:
        store = world.services.store
        row = await store.fetchone("SELECT folder FROM releases WHERE ref = ?", [ref])
        assert row is not None
        return await assert_in_step(parts_of(world), str(row["folder"]))

    return world.server.call(check)


@contextmanager
def picked_album(
    factory: NavidromeFactory, tmp: Path, replay: Replay
) -> Iterator[tuple[DeliveryWorld, str]]:
    """A world whose album is filled from a picked release numbered differently from its
    files: (the world, the album)."""
    with world_with(factory, tmp, [PICKED], replay) as world:
        ident = album_id(world, PICKED.title)
        assert view(world, ident)["songCount"] == 14
        found = wait_for(world, ident, "deferred")
        assert (found["picked"], found["release_ref"]) != ("", "")
        assert found["release_ref"] == PICKED_RELEASE
        world.client(client="viewer").ok("star", {"albumId": "sh.al.demo.900000101"})
        assert len(songs(world, ident)) == 14
        yield world, ident


def listed_for_the_pick(replay: Replay) -> str:
    """The catalog's list of the picked release, later: the track on the owned "AMBER."'s
    number is longer, and it has a track more at 15 and one at 20, the owned
    "NORTHERN."'s number. Returns the longer track's title."""
    rows = replay.by_path["albums/900000101"]["body"]["tracks"]
    shared = next(row for row in rows if row["number"] == 2)
    shared["duration_ms"] += 5000
    rows.append(gained(rows[-1], "900000971", "Fifteenth River", 15))
    rows.append(gained(rows[-1], "900000972", "Twentieth River", 20))
    return str(shared["title"])


def test_a_picked_album_gains_a_track_but_none_on_an_owned_songs_number(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    replay = replay_for_picks()
    with picked_album(navidrome_factory, tmp_path, replay) as (world, album):
        before = {s["id"]: s for s in songs(world, album)}
        owned = [s for s in before.values() if s["title"] in ("NORTHERN.", "AMBER.")]
        assert sorted(s["track"] for s in owned) == [2, 20]
        longer = listed_for_the_pick(replay)
        assert read_again(world, PICKED_RELEASE) == {
            "read": 1, "changed": 1, "added": 1, "updated": 1
        }  # fmt: skip
        found = reading(world, PICKED_RELEASE)
        assert found is not None and found["left_out"] == 1  # "Twentieth River"
        after = {s["id"]: s for s in songs(world, album)}
        titles = [s["title"] for s in after.values()]
        assert len(after) == 15 and "Twentieth River" not in titles
        [added] = [s for s in after.values() if s["id"] not in before]
        assert (added["title"], added["track"], added["albumId"]) == ("Fifteenth River", 15, album)
        for ident, song in before.items():  # every song under its ID, where it was
            assert (after[ident]["title"], after[ident]["track"]) == (song["title"], song["track"])
        for song in owned:  # the owner's files as Navidrome had them
            assert after[song["id"]] == song
        # The placeholder on the owned song's number is corrected where it is.
        [shared] = [s for s in before.values() if s["title"] == longer]
        assert shared["track"] == 2
        assert after[shared["id"]]["duration"] == shared["duration"] + 5
        assert len(in_step(world, PICKED_RELEASE)) == 13


@contextmanager
def album_with_a_part(
    factory: NavidromeFactory, tmp: Path, replay: Replay
) -> Iterator[tuple[DeliveryWorld, str, dict[str, Any]]]:
    """A world with an album filled from its release and a part of it - another album
    whose one song is on that release: (the world, the filled album, the row of the
    placeholder the part's file plays for)."""
    with world_with(factory, tmp, [PART, WHOLE], replay) as world:
        part, whole = album_id(world, PART.title), album_id(world, WHOLE.title)
        assert view(world, whole)["songCount"] == 17
        wait_for(world, whole, "deferred")
        wait_for(world, part, "review")
        world.client(client="viewer").ok("star", {"albumId": "sh.al.demo.900000021"})
        assert len(songs(world, whole)) == 17
        [owned] = songs(world, part)
        backed = backed_row(world)
        assert backed["backing_song_id"] == owned["id"] and backed["title"] == owned["title"]
        yield world, whole, backed


def backed_row(world: DeliveryWorld) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        [row] = await world.services.store.fetchall(
            "SELECT * FROM placeholders WHERE release_ref = ? AND backing_song_id IS NOT NULL",
            [PART_RELEASE],
        )
        return dict(row)

    return world.server.call(read)


def listed_for_the_part(replay: Replay, backed: dict[str, Any]) -> None:
    """The catalog's list of the release, later: the track the part's file plays for is
    longer, and it has a track more."""
    rows = replay.by_path["albums/900000021"]["body"]["tracks"]
    ident = str(backed["track_ref"]).partition(":")[2]
    track = next(row for row in rows if row["ref"]["id"] == ident)
    track["duration_ms"] += 4000
    rows.append(gained(rows[-1], "900000973", "Eighteenth Letter", 18))


def test_a_placeholder_a_part_s_file_plays_for_keeps_it_when_its_length_is_corrected(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    replay = replay_for_picks()
    with album_with_a_part(navidrome_factory, tmp_path, replay) as (world, whole, backed):
        part = album_id(world, PART.title)
        song, owned = str(backed["song_id"]), str(backed["backing_song_id"])
        own_audio = world.nd.client().request("stream", {"id": owned}).content
        assert world.client().request("stream", {"id": song}).content == own_audio
        listed_for_the_part(replay, backed)
        assert read_again(world, PART_RELEASE) == {
            "read": 1, "changed": 1, "added": 1, "updated": 1
        }  # fmt: skip
        now = backed_row(world)  # the same song, the part's file still playing for it
        assert (now["song_id"], now["backing_song_id"]) == (song, owned)
        assert now["duration_ms"] == backed["duration_ms"] + 4000
        assert world.client().request("stream", {"id": song}).content == own_audio
        filled = songs(world, whole)
        assert len(filled) == 18 and "Eighteenth Letter" in [s["title"] for s in filled]
        assert [s["id"] for s in songs(world, part)] == [owned]  # the part as it was
        assert len(in_step(world, PART_RELEASE)) == 13
