"""Artist lists of music imports' files, against a real Navidrome (``music_imports/lists.py``).

A file whose artist text names several artists is published with each of them when the
catalogs list them (an album of the import whose files all get one list also with its
album artists); a file published as it was gets its list later as an import operation and
gives it back when the setting is off - the song and its album keep their IDs, and the
import's records name the file that is published (``Storage.holds``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from shijhon.artist_lists.files import LibraryFiles
from shijhon.artist_lists.names import listed
from shijhon.artist_lists.repair import ArtistLists
from shijhon.artist_lists.sources import Resolution, Subject
from tests.harness.import_world import ImportWorld, open_world, published
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio

JOINED = "Lark Fen & Moss Hale"
NAMES = ["Lark Fen", "Moss Hale"]


class Listing:
    def __init__(self, lists: Mapping[str, Sequence[str]]) -> None:
        self.lists = dict(lists)

    async def resolve(
        self, subject: Subject, *, link: bool = True, skips: Any = None
    ) -> Resolution:
        spelled = listed(subject.artist, self.lists.get(subject.artist) or [])
        return (
            Resolution("resolved", tuple(spelled), "isrc") if spelled else Resolution("unresolved")
        )


async def world_of(nd: NavidromeInstance, tmp_path: Path) -> ImportWorld:
    return await open_world(
        nd.base_url,
        nd.music,
        tmp_path / "state.db",
        ("ana",),
        create=nd.create_user,
        login=lambda name, password: nd.login(name, password)["token"],
    )


def source_album(tmp_path: Path) -> list[Path]:
    album = Album(
        JOINED,
        "Shared Tide",
        (Track("Tide One", 1), Track("Tide Two", 2)),
        write_album_artist=False,
        folder="Shared Tide",
    )
    return write_album(tmp_path / "source", album)


def imported(nd: NavidromeInstance) -> dict[str, dict[str, Any]]:
    found = nd.native("GET", "song", params={"_start": 0, "_end": 1000}).json()
    return {
        s["title"]: s for s in found if s["path"].startswith("_imports/") and not s.get("missing")
    }


def people(song: Mapping[str, Any], role: str = "artist") -> list[str]:
    return [p["name"] for p in song.get("participants", {}).get(role, [])]


async def records_hold(w: ImportWorld) -> None:
    for item in await w.store.fetchall("SELECT * FROM music_import_items WHERE state = 'complete'"):
        assert w.imports.storage.holds(
            item["final_name"], item["staging_name"], item["expected_size"], item["digest"]
        )
        assert item["upgrade"] == ""


async def test_an_import_is_published_with_each_artist(navidrome_factory: Any, tmp_path: Path):
    nd = navidrome_factory()
    w = await world_of(nd, tmp_path)
    try:

        async def names(
            facts: Mapping[str, Any], skips: Any = None
        ) -> tuple[list[str], str] | None:
            return (NAMES, "isrc") if facts.get("artist") == JOINED else None

        w.lists.publish_names = names
        files = source_album(tmp_path)
        await published(w, w.users["ana"], files, tmp_path / "source", "listed")
        songs = imported(nd)
        assert set(songs) == {"Tide One", "Tide Two"}
        for song in songs.values():
            assert people(song) == NAMES
            assert people(song, "albumartist") == NAMES  # (a new album of one list)
            assert song["artist"] == JOINED and song["albumArtist"] == JOINED
        assert len({s["albumId"] for s in songs.values()}) == 1
        await records_hold(w)
        rows = await w.store.fetchall("SELECT * FROM artist_lists WHERE state = 'written'")
        assert len(rows) == 2
        for row in rows:  # (the received originals stay as they came)
            w.imports.storage.verify(row["staging_before"], row["size_before"], row["sha_before"])
    finally:
        await w.close()


async def test_a_published_import_gets_its_list_later_and_gives_it_back(
    navidrome_factory: Any, tmp_path: Path
):
    nd = navidrome_factory()
    w = await world_of(nd, tmp_path)
    try:
        files = source_album(tmp_path)
        await published(w, w.users["ana"], files, tmp_path / "source", "plain")
        before = imported(nd)
        assert all(people(s) == [JOINED] for s in before.values())
        reserved = {
            r["id"]: r["reserved_size"]
            for r in await w.store.fetchall("SELECT * FROM music_import_items")
        }
        worker = ArtistLists(
            w.native,
            w.store,
            Listing({JOINED: NAMES}),  # type: ignore[arg-type]
            LibraryFiles(nd.music, w.store, w.native, w.scans, not_in=("_shijhon", "_imports")),
            placeholder_folder="_shijhon",
            imports=w.lists,
            report=False,
            pause=0,
        )
        summary = await worker.once()
        assert summary.written["isrc"] == 2 and summary.albums == 1
        after = imported(nd)
        for title, song in before.items():
            assert (after[title]["id"], after[title]["albumId"]) == (song["id"], song["albumId"])
            assert people(after[title]) == NAMES
            assert people(after[title], "albumartist") == NAMES
        await records_hold(w)
        worker.enabled = False
        summary = await worker.once()
        assert summary.undone == 2
        back = imported(nd)
        for title, song in before.items():
            assert (back[title]["id"], back[title]["albumId"]) == (song["id"], song["albumId"])
            assert people(back[title]) == [JOINED]
        await records_hold(w)
        for row in await w.store.fetchall("SELECT * FROM music_import_items"):
            assert row["reserved_size"] == reserved[row["id"]]
        assert await w.store.fetchall("SELECT * FROM artist_lists WHERE state = 'written'") == []
    finally:
        await w.close()
