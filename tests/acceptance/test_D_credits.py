"""Suite D — credits naming several artists.

A catalog song credited "A & B" whose catalog names two artist items (in the track, or
asked for: ``PlaceholderEngine.credits``) is written with each artist, so Navidrome credits
A and B and makes no artist "A & B". A band whose name has a separator, with one artist
item, stays one artist. Placeholders written before with the joined credit are rewritten in
place (``CreditRepair``), their song IDs kept.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from shijhon.artist_lists.names import listed
from shijhon.artist_lists.sources import Resolution, Subject
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, release_data
from shijhon.placeholders.credits import CreditRepair
from shijhon.placeholders.engine import MaterializeResult, ReplaceError
from tests.harness.engine import catalog_release, engine_for
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio


def artist_names(nd: NavidromeInstance, query: str) -> set[str]:
    """The artists Navidrome has whose names match ``query`` (track artists too)."""
    found = nd.client().ok("search3", {"query": query, "artistCount": 50, "songCount": 0})
    return {a["name"] for a in found["searchResult3"].get("artist", [])}


def song_artists(nd: NavidromeInstance, song_id: str) -> list[str]:
    song = nd.client().ok("getSong", {"id": song_id})["song"]
    return [a["name"] for a in song.get("artists", [])]


def song_of(result: MaterializeResult, release: CatalogRelease) -> str:
    return result.created[release.tracks[0].ref]


def items_for(names: Mapping[str, int]) -> object:
    """A ``credits`` lookup: each track gets as many artist items as ``names`` says for
    its credit (none for a credit it does not know)."""

    async def credits(
        tracks: Sequence[CatalogTrack],
    ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
        return {
            t.ref: tuple(CatalogRef("test", f"{t.ref.id}-a{i}") for i in range(names[t.artist]))
            for t in tracks
            if names.get(t.artist)
        }

    return credits


async def test_a_joined_credit_is_listed_as_each_artist_after_the_write(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release(
        "cr-joined", "Harbor Lights", "Lumen Vale", 2, track_artist="Lumen Vale & Orrin Pike"
    )
    async with engine_for(navidrome, tmp_path) as parts:
        parts.engine.credits = items_for({"Lumen Vale & Orrin Pike": 2})  # type: ignore[assignment]
        result = await parts.engine.materialize(release)  # (a write asks nothing more)
        row = await parts.engine.placeholder_row(song_of(result, release))
        assert "artists" not in json.loads(row["tags"])
        repair = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        assert await repair.repair() == 2
        row = await parts.engine.placeholder_row(song_of(result, release))
    assert json.loads(row["tags"])["artists"] == ["Lumen Vale", "Orrin Pike"]
    assert song_artists(navidrome, song_of(result, release)) == ["Lumen Vale", "Orrin Pike"]
    assert artist_names(navidrome, "Orrin") == {"Orrin Pike"}
    assert artist_names(navidrome, "Lumen") == {"Lumen Vale"}


async def test_items_in_the_catalog_answer_need_no_lookup(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release(
        "cr-listed", "Tidewater", "Corva Lune", 1, track_artist="Corva Lune, Brisk Avalon"
    )
    refs = (CatalogRef("test", "corva"), CatalogRef("test", "brisk"))
    release = replace(release, tracks=tuple(replace(t, artist_refs=refs) for t in release.tracks))
    asked: list[int] = []

    async def credits(
        tracks: Sequence[CatalogTrack],
    ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
        asked.append(len(tracks))
        return {}

    async with engine_for(navidrome, tmp_path) as parts:
        parts.engine.credits = credits
        result = await parts.engine.materialize(release)
    assert asked == []
    assert song_artists(navidrome, song_of(result, release)) == ["Corva Lune", "Brisk Avalon"]


async def test_one_band_with_a_separator_stays_one_artist(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release(
        "cr-band", "Embers", "Salt, Ash & Ember", 1, track_artist="Salt, Ash & Ember"
    )
    async with engine_for(navidrome, tmp_path) as parts:
        parts.engine.credits = items_for({"Salt, Ash & Ember": 1})  # type: ignore[assignment]
        result = await parts.engine.materialize(release)
        row = await parts.engine.placeholder_row(song_of(result, release))
    assert "artists" not in json.loads(row["tags"])
    assert song_artists(navidrome, song_of(result, release)) == ["Salt, Ash & Ember"]


async def test_earlier_placeholders_are_rewritten_with_each_artist(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    joined = "Wren Hollow & Kestrel Day"
    uneven = "Pale Arcade, Mossline & Juno Reef"  # three names, two items: left as it is
    release = catalog_release("cr-repair", "Quiet Engines", "Wren Hollow", 2, track_artist=joined)
    release = replace(
        release, tracks=(release.tracks[0], replace(release.tracks[1], artist=uneven))
    )
    async with engine_for(navidrome, tmp_path) as parts:
        made = await parts.engine.materialize(release)  # before: no lookup, joined credits
        first, second = (made.created[t.ref] for t in release.tracks)
        assert song_artists(navidrome, first) == [joined]

        parts.engine.credits = items_for({joined: 2, uneven: 2})  # type: ignore[assignment]
        asked: list[str] = []
        lookup = items_for({joined: 2, uneven: 2})

        async def counted(
            tracks: Sequence[CatalogTrack],
        ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
            asked.extend(t.artist for t in tracks)
            return await lookup(tracks)  # type: ignore[operator, no-any-return]

        parts.engine.credits = counted
        repair = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        assert await repair.repair() == 1
        assert await repair.repair() == 0  # nothing left to do
        assert sorted(asked) == sorted([joined, uneven])  # the uneven one not asked again

    assert song_artists(navidrome, first) == ["Wren Hollow", "Kestrel Day"]  # the same ID
    assert song_artists(navidrome, second) == [uneven]
    assert artist_names(navidrome, "Kestrel") == {"Kestrel Day"}
    assert artist_names(navidrome, "Wren") == {"Wren Hollow"}


async def test_other_catalogs_items_are_not_asked_for(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release(
        "cr-other", "Far Shore", "Ilse Marrow", 1, track_artist="Ilse Marrow & Tobin Quay"
    )
    asked: list[int] = []

    async def credits(
        tracks: Sequence[CatalogTrack],
    ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
        asked.append(len(tracks))
        return {}

    async with engine_for(navidrome, tmp_path) as parts:
        await parts.engine.materialize(release)
        parts.engine.credits = credits
        repair = CreditRepair(parts.engine, parts.store, catalog="another", delay_seconds=0)
        assert await repair.repair() == 0
    assert asked == []


async def test_an_earlier_albums_joined_artists_are_listed_and_it_stays_one_album(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    credit = "Mira Sol & Ansel Grey"
    release = catalog_release("cr-album", "Two Rivers", credit, 2)
    async with engine_for(navidrome, tmp_path) as parts:
        made = await parts.engine.materialize(release)  # before: the album's credit joined
        songs = [made.created[t.ref] for t in release.tracks]
        album_id = navidrome.client().ok("getSong", {"id": songs[0]})["song"]["albumId"]
        await parts.engine.set_backing(songs[0], "owned-recording")
        # The catalog's record names the album's two artist items (the earlier code did
        # not list them).
        refs = (CatalogRef("test", "mira"), CatalogRef("test", "ansel"))
        data = release_data(replace(release, artist_refs=refs))
        await parts.store.execute(
            "UPDATE releases SET data = ? WHERE ref = ?", [json.dumps(data), str(release.ref)]
        )
        repair = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        assert await repair.repair() == 2
        assert await repair.repair() == 0
        row = await parts.engine.placeholder_row(songs[0])
    assert row["backing_song_id"] == "owned-recording"  # the same recording: still backed
    album = navidrome.client().ok("getAlbum", {"id": album_id})["album"]
    assert {s["id"] for s in album["song"]} == set(songs)  # one album, the same IDs
    assert [a["name"] for a in album.get("artists", [])] == ["Mira Sol", "Ansel Grey"]
    assert artist_names(navidrome, "Ansel") == {"Ansel Grey"}


async def test_an_albums_artists_a_failed_retag_left_out_are_listed_by_the_next_pass(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    credit = "Tove Lark & Pell Ardent"
    release = catalog_release("cr-album-cut", "Salt Roads", credit, 3, track_artist="Tove Lark")
    async with engine_for(navidrome, tmp_path) as parts:
        made = await parts.engine.materialize(release)
        songs = [made.created[t.ref] for t in release.tracks]
        album_id = navidrome.client().ok("getSong", {"id": songs[0]})["song"]["albumId"]
        refs = (CatalogRef("test", "tove"), CatalogRef("test", "pell"))
        data = release_data(replace(release, artist_refs=refs))
        await parts.store.execute(
            "UPDATE releases SET data = ? WHERE ref = ?", [json.dumps(data), str(release.ref)]
        )
        recredit = parts.engine.recredit

        async def failing(song_id: str, track: CatalogTrack) -> bool:
            if song_id == songs[1]:
                raise ReplaceError("cannot write the placeholder (OSError)")
            return await recredit(song_id, track)

        parts.engine.recredit = failing  # type: ignore[method-assign]
        repair = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        assert await repair.repair() == 2
        parts.engine.recredit = recredit  # type: ignore[method-assign]
        later = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        assert await later.repair() == 1
        assert await later.repair() == 0
    album = navidrome.client().ok("getAlbum", {"id": album_id})["album"]
    assert {s["id"] for s in album["song"]} == set(songs)  # one album, the same IDs
    assert [a["name"] for a in album.get("artists", [])] == ["Tove Lark", "Pell Ardent"]
    assert artist_names(navidrome, "Pell") == {"Pell Ardent"}


async def test_a_featured_artist_in_the_title_keeps_the_credit_whole(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    release = catalog_release("cr-feat", "Tall Grass", "Fen & Rook", 1, track_artist="Fen & Rook")
    release = replace(
        release, tracks=tuple(replace(t, title="Tall Grass (feat. Lio)") for t in release.tracks)
    )
    asked: list[int] = []

    async def credits(
        tracks: Sequence[CatalogTrack],
    ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
        asked.append(len(tracks))
        return {}

    async with engine_for(navidrome, tmp_path) as parts:
        parts.engine.credits = credits
        result = await parts.engine.materialize(release)
    assert asked == []  # (the count would prove nothing: not asked)
    assert song_artists(navidrome, song_of(result, release)) == ["Fen & Rook"]


async def test_artist_items_answered_in_time_are_kept_when_others_are_late(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    import anyio

    release = catalog_release(
        "cr-slow", "Long Night", "Dale Winter", 12, track_artist="Dale Winter & Iva Brook"
    )

    async def credits(
        tracks: Sequence[CatalogTrack],
    ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
        if tracks[0].ref != release.tracks[0].ref:
            await anyio.sleep(30)  # the second batch never comes in time
        refs = (CatalogRef("test", "dw"), CatalogRef("test", "ib"))
        return {t.ref: refs for t in tracks}

    async with engine_for(navidrome, tmp_path) as parts:
        parts.engine.credits = credits
        credited = await parts.engine.credited(release.tracks, 1.0)
    assert [bool(t.artist_refs) for t in credited] == [True] * 10 + [False] * 2


class AddonLists:
    """The add-on catalogs' lists (``artist_lists/sources.py``), as these tests need them."""

    def __init__(self, lists: Mapping[str, Sequence[str]]) -> None:
        self.lists = dict(lists)
        self.asked: list[str] = []

    async def resolve(
        self, subject: Subject, *, link: bool = True, skips: Any = None
    ) -> Resolution:
        assert not link  # (the catalog was asked: its items are the credit repair's)
        self.asked.append(subject.artist)
        names = self.lists.get(subject.artist)
        spelled = listed(subject.artist, names) if names else None
        if spelled is None:
            return Resolution("unresolved")
        return Resolution("resolved", tuple(spelled), "isrc")


async def test_a_credit_the_catalog_cannot_split_takes_the_add_on_catalogs_list(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """While the artist lists' setting is on; what they said is kept, not asked again."""
    joined, unknown = "Pale Arc & Quin Fell", "Rook Hale & Sima Dune"
    first = catalog_release("cr-addon-a", "Lantern", "Pale Arc", 1, track_artist=joined)
    second = catalog_release("cr-addon-b", "Ember", "Rook Hale", 1, track_artist=unknown)
    async with engine_for(navidrome, tmp_path) as parts:
        parts.engine.credits = items_for({})  # type: ignore[assignment]  # (the catalog: none)
        a = song_of(await parts.engine.materialize(first), first)
        b = song_of(await parts.engine.materialize(second), second)
        lists = AddonLists({joined: ["Pale Arc", "Quin Fell"]})
        repair = CreditRepair(parts.engine, parts.store, catalog="test", delay_seconds=0)
        repair.lists = lists  # type: ignore[assignment]
        repair.pause = 0
        assert await repair.repair() == 0 and lists.asked == []  # (the setting off)
        repair.lookups = lambda: True
        assert await repair.repair() == 1
        assert sorted(lists.asked) == sorted([joined, unknown])
        assert await repair.repair() == 0
        assert len(lists.asked) == 2  # (the credit without a list: asked again later)
        rows = {r["key"]: r for r in await parts.store.fetchall("SELECT * FROM artist_lists")}
    assert song_artists(navidrome, a) == ["Pale Arc", "Quin Fell"]
    assert song_artists(navidrome, b) == [unknown]
    assert (rows[a]["kind"], rows[a]["state"], rows[a]["source"]) == (
        "placeholder",
        "written",
        "isrc",
    )
    assert rows[b]["state"] == "unresolved" and rows[b]["retry_at"] > 0
