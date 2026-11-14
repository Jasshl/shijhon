"""Artist lists for library files, against a real Navidrome (``artist_lists``).

A file whose one artist text names several artists gets the multi-valued artists list
when the catalogs' names are exactly its text: each person becomes an artist, the song and
its album keep their IDs (an album without an album artist tag gets the one Navidrome
showed; a compilation keeps "Various Artists"), an album whose songs all get one list is
listed under each person - all its files or none. Turned off, the files go back to their
tags, unless they changed since. Files that cannot take a list are left as they are.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from shijhon.artist_lists.files import LibraryFiles, ListError, file_digest
from shijhon.artist_lists.names import listed
from shijhon.artist_lists.repair import ArtistLists
from shijhon.artist_lists.sources import Resolution, Subject
from shijhon.music_imports import tagmap
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.store import Store
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

pytestmark = pytest.mark.anyio


class Listing:
    """The catalogs, as far as these tests need them: the names each joined text has."""

    def __init__(self, lists: Mapping[str, Sequence[str]]) -> None:
        self.lists = dict(lists)
        self.asked: list[str] = []

    async def resolve(
        self, subject: Subject, *, link: bool = True, skips: Any = None
    ) -> Resolution:
        self.asked.append(subject.artist)
        names = self.lists.get(subject.artist)
        spelled = listed(subject.artist, names) if names else None
        if spelled is None:
            return Resolution("unresolved")
        return Resolution("resolved", tuple(spelled), "link")


@dataclass
class World:
    nd: NavidromeInstance
    navidrome: NavidromeService
    store: Store
    files: LibraryFiles
    lists: ArtistLists


@asynccontextmanager
async def world_for(
    nd: NavidromeInstance,
    state: Path,
    lists: Mapping[str, Sequence[str]],
    *,
    report: bool = False,
    library: bool = True,
) -> AsyncIterator[World]:
    service = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD, client_name="shijhon")
    store = await Store.open(state / "shijhon.sqlite3")
    scans = ScanCoordinator(service)
    files = LibraryFiles(nd.music, store, service, scans, not_in=("_shijhon", "_imports"))
    worker = ArtistLists(
        service,
        store,
        Listing(lists),  # type: ignore[arg-type]
        files,
        placeholder_folder="_shijhon",
        library=library,
        report=report,
        pause=0,
    )
    try:
        yield World(nd, service, store, files, worker)
    finally:
        await store.close()
        await service.aclose()


def songs(nd: NavidromeInstance, folder: str) -> dict[str, dict[str, Any]]:
    """Navidrome's songs under ``folder``, by file name."""
    found = nd.native("GET", "song", params={"_start": 0, "_end": 1000}).json()
    return {
        s["path"].rsplit("/", 1)[-1]: s
        for s in found
        if s["path"].startswith(folder + "/") and not s.get("missing")
    }


def people(song: Mapping[str, Any], role: str = "artist") -> list[str]:
    return [p["name"] for p in song.get("participants", {}).get(role, [])]


def artist_names(nd: NavidromeInstance) -> set[str]:
    found = nd.native("GET", "artist", params={"_start": 0, "_end": 1000, "missing": "false"})
    return {a["name"] for a in found.json()}


def raw_tags(path: Path) -> dict[str, list[str]]:
    with open(path, "rb") as handle:
        audio = tagmap.parse(handle, path.suffix)
        return tagmap.raw(audio, tagmap.tags_of(audio, handle))


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith(".shijhon-"))


async def test_song_and_album_ids_survive_and_each_person_becomes_an_artist(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    loose = Album(
        "Ash Vane, Bell Orrin",
        "Loose Night",
        (Track("Loose Night", 1),),
        write_album_artist=False,
        folder="ids/loose",
    )
    tagged = Album(
        "Cora Dune",
        "Harbor",
        (Track("Harbor One", 1, artists=("Cora Dune & Pell Rook",)), Track("Harbor Two", 2)),
        fmt="mp3",
        folder="ids/tagged",
    )
    compilation = Album(
        "Various Artists",
        "Mixed Tides",
        (Track("Tide", 1, artists=("Juno Reef x Kael Moss",)),),
        fmt="m4a",
        compilation=True,
        write_album_artist=False,
        folder="ids/compilation",
    )
    older = Album(
        "Lior Bask / Nell Quay",  # (Navidrome splits " / " itself: no candidate)
        "Older Tag",
        (Track("Older", 1, artists=("Odo Fell, Rin Vast + Sal Ume",)),),
        fmt="mp3",
        write_album_artist=False,
        folder="ids/older",
    )
    for album in (loose, tagged, compilation, older):
        write_album(navidrome.music, album)
    old = navidrome.music / "ids/older/1-01 Older.mp3"
    ID3(old).save(old, v2_version=3)  # an ID3v2.3 tag stays one
    single = navidrome.music / "ids/loose/1-01 Loose Night.flac"
    single.chmod(0o640)  # (its mode stays)
    navidrome.scan(full=True)
    before = songs(navidrome, "ids/loose") | songs(navidrome, "ids/tagged")
    before |= songs(navidrome, "ids/compilation") | songs(navidrome, "ids/older")
    lists = {
        "Ash Vane, Bell Orrin": ["Ash Vane", "Bell Orrin"],
        "Cora Dune & Pell Rook": ["Cora Dune", "Pell Rook"],
        "Juno Reef x Kael Moss": ["Juno Reef", "Kael Moss"],
        "Odo Fell, Rin Vast + Sal Ume": ["Odo Fell", "Rin Vast", "Sal Ume"],
    }
    async with world_for(navidrome, tmp_path, lists) as world:
        summary = await world.lists.once()
        rows = {r["path"]: r for r in await world.store.fetchall("SELECT * FROM artist_lists")}
    assert sum(summary.written.values()) == 4
    after = songs(navidrome, "ids/loose") | songs(navidrome, "ids/tagged")
    after |= songs(navidrome, "ids/compilation") | songs(navidrome, "ids/older")
    for name, song in before.items():
        assert after[name]["id"] == song["id"], name
        assert after[name]["albumId"] == song["albumId"], name
        assert after[name]["albumArtist"] == song["albumArtist"], name
        assert after[name]["artist"] == song["artist"], name  # (the text it shows)
    assert people(after["1-01 Loose Night.flac"]) == ["Ash Vane", "Bell Orrin"]
    # An album of one song with one list: listed under each person too.
    assert people(after["1-01 Loose Night.flac"], "albumartist") == ["Ash Vane", "Bell Orrin"]
    assert people(after["1-01 Harbor One.mp3"]) == ["Cora Dune", "Pell Rook"]
    assert people(after["1-01 Harbor One.mp3"], "albumartist") == ["Cora Dune"]
    assert people(after["1-01 Tide.m4a"]) == ["Juno Reef", "Kael Moss"]
    assert people(after["1-01 Tide.m4a"], "albumartist") == ["Various Artists"]
    assert people(after["1-01 Older.mp3"]) == ["Odo Fell", "Rin Vast", "Sal Ume"]
    assert ID3(old).version[:2] == (2, 3)
    assert stat.S_IMODE(single.stat().st_mode) == 0o640
    names = artist_names(navidrome)
    for person in ("Ash Vane", "Bell Orrin", "Pell Rook", "Juno Reef", "Kael Moss", "Sal Ume"):
        assert person in names
    for joined in ("Ash Vane, Bell Orrin", "Cora Dune & Pell Rook", "Juno Reef x Kael Moss"):
        assert joined not in names
    # Only the lists were added (and the album artist Navidrome showed, where none was).
    loose_tags = raw_tags(navidrome.music / "ids/loose/1-01 Loose Night.flac")
    assert loose_tags["albumartist"] == ["Ash Vane, Bell Orrin"]
    assert loose_tags["artist"] == ["Ash Vane, Bell Orrin"]
    record = rows["ids/loose/1-01 Loose Night.flac"]
    assert json.loads(record["previous"]) == {
        "artists": None,
        "albumartists": None,
        "albumartist": None,
    }
    assert json.loads(rows["ids/compilation/1-01 Tide.m4a"]["previous"]) == {"artists": None}
    for folder in ("loose", "tagged", "compilation", "older"):
        assert leftovers(navidrome.music / "ids" / folder) == []


async def test_an_album_is_listed_under_each_person_only_with_all_its_files(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    whole = Album(
        "Lark Fen & Moss Hale",
        "Shared Shore",
        (Track("Shore One", 1), Track("Shore Two", 2)),
        write_album_artist=False,
        folder="albums/whole",
    )
    partial = Album(
        "Rue Kell, Tam Bright",
        "Split Shore",
        (Track("Split One", 1), Track("Split Two", 2)),
        write_album_artist=False,
        folder="albums/partial",
    )
    write_album(navidrome.music, whole)
    paths = write_album(navidrome.music, partial)
    paths[1].chmod(0o444)  # one of its files cannot be written
    navidrome.scan(full=True)
    before = songs(navidrome, "albums/whole") | songs(navidrome, "albums/partial")
    lists = {
        "Lark Fen & Moss Hale": ["Lark Fen", "Moss Hale"],
        "Rue Kell, Tam Bright": ["Rue Kell", "Tam Bright"],
    }
    try:
        async with world_for(navidrome, tmp_path, lists) as world:
            summary = await world.lists.once()
    finally:
        paths[1].chmod(0o644)
    assert summary.albums == 1 and summary.skipped["read_only"] == 1
    after = songs(navidrome, "albums/whole") | songs(navidrome, "albums/partial")
    for name in ("1-01 Shore One.flac", "1-02 Shore Two.flac"):
        assert people(after[name], "albumartist") == ["Lark Fen", "Moss Hale"]
        assert after[name]["albumId"] == before[name]["albumId"]
    album = navidrome.client().ok("getAlbum", {"id": before["1-01 Shore One.flac"]["albumId"]})
    assert [a["name"] for a in album["album"]["artists"]] == ["Lark Fen", "Moss Hale"]
    # The partial album: its writable file gets its own list, not the album's.
    assert people(after["1-01 Split One.flac"]) == ["Rue Kell", "Tam Bright"]
    assert people(after["1-01 Split One.flac"], "albumartist") == ["Rue Kell, Tam Bright"]
    assert people(after["1-02 Split Two.flac"]) == ["Rue Kell, Tam Bright"]
    assert after["1-02 Split Two.flac"]["albumId"] == before["1-02 Split Two.flac"]["albumId"]


async def test_a_write_that_fails_in_an_album_takes_back_the_album_s_others(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    album = Album(
        "Vey Holm & Wyn Ash",
        "All Or None",
        (Track("None One", 1), Track("None Two", 2)),
        write_album_artist=False,
        folder="albums/none",
    )
    paths = write_album(navidrome.music, album)
    navidrome.scan(full=True)
    sums = {p.name: file_digest(p) for p in paths}
    tags = {p.name: raw_tags(p) for p in paths}
    before = songs(navidrome, "albums/none")
    async with world_for(navidrome, tmp_path, {"Vey Holm & Wyn Ash": ["Vey Holm", "Wyn Ash"]}) as w:
        writes = 0

        async def second_fails(step: str) -> None:
            nonlocal writes
            if step == "write staged":
                writes += 1
                if writes == 2:
                    raise ListError("not_confirmed")

        w.files.step = second_fails
        summary = await w.lists.once()
        rows = await w.store.fetchall(
            "SELECT state, reason FROM artist_lists WHERE path LIKE 'albums/none/%'"
        )
    assert summary.albums == 0 and summary.written["link"] == 0
    assert sorted((r["state"], r["reason"]) for r in rows) == [
        ("failed", "album"),
        ("failed", "not_confirmed"),
    ]
    after = songs(navidrome, "albums/none")
    for path in paths:
        assert raw_tags(path) == tags[path.name]  # (its tags as they were)
        assert people(after[path.name]) == ["Vey Holm & Wyn Ash"]
        assert after[path.name]["id"] == before[path.name]["id"]
        assert after[path.name]["albumId"] == before[path.name]["albumId"]
    assert file_digest(paths[1]) == sums[paths[1].name]  # never replaced
    assert leftovers(paths[0].parent) == []


async def test_turned_off_the_files_go_back_unless_they_changed(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    kept = Album(
        "Ilo Sand, Jem Rook",
        "Given Back",
        (Track("Given", 1),),
        write_album_artist=False,
        folder="undo/back",
    )
    changed = Album(
        "Kit Lume, Lev Narr",
        "Changed Since",
        (Track("Changed", 1),),
        write_album_artist=False,
        folder="undo/changed",
    )
    back = write_album(navidrome.music, kept)[0]
    other = write_album(navidrome.music, changed)[0]
    navidrome.scan(full=True)
    tags = raw_tags(back)
    before = songs(navidrome, "undo/back")["1-01 Given.flac"]
    lists = {
        "Ilo Sand, Jem Rook": ["Ilo Sand", "Jem Rook"],
        "Kit Lume, Lev Narr": ["Kit Lume", "Lev Narr"],
    }
    async with world_for(navidrome, tmp_path, lists) as world:
        assert sum((await world.lists.once()).written.values()) == 2
        assert raw_tags(back) != tags
        audio = FLAC(other)
        audio["comment"] = ["a person's own note"]
        audio.save()
        world.lists.enabled = False
        summary = await world.lists.once()
        assert (summary.undone, summary.declined) == (1, 1)
        rows = {r["path"]: r for r in await world.store.fetchall("SELECT * FROM artist_lists")}
        assert raw_tags(back) == tags
        after = songs(navidrome, "undo/back")["1-01 Given.flac"]
        assert (after["id"], after["albumId"]) == (before["id"], before["albumId"])
        assert people(after) == ["Ilo Sand, Jem Rook"]
        assert "undo/back/1-01 Given.flac" not in rows
        assert rows["undo/changed/1-01 Changed.flac"]["state"] == "declined"
        assert raw_tags(other)["comment"] == ["a person's own note"]
        assert raw_tags(other)["artists"] == ["Kit Lume", "Lev Narr"]
        world.lists.enabled = True
        again = await world.lists.once()
    # On again: the file given back is listed again; the changed one is left alone.
    assert sum(again.written.values()) == 1
    assert people(songs(navidrome, "undo/back")["1-01 Given.flac"]) == ["Ilo Sand", "Jem Rook"]


async def test_files_that_cannot_take_a_list_are_left_as_they_are(
    navidrome: NavidromeInstance, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    linked = write_album(
        navidrome.music,
        Album("Mara Quill, Nox Tern", "Linked", (Track("Linked", 1),), folder="skip/linked"),
    )[0]
    os.link(linked, navidrome.root / "elsewhere.flac")  # (outside the library)
    listed_already = write_album(
        navidrome.music,
        Album(
            "Oda Fenn, Pim Vale",
            "Listed",
            (Track("Listed", 1),),
            folder="skip/listed",
            extra={"artists": ["Oda Fenn, Pim Vale"]},
        ),
    )[0]
    identified = write_album(
        navidrome.music,
        Album(
            "Quin Arl, Ros Bey",
            "Identified",
            (Track("Identified", 1),),
            folder="skip/identified",
            extra={"musicbrainz_artistid": ["00000000-0000-4000-8000-000000000001"]},
        ),
    )[0]
    unknown = write_album(
        navidrome.music,
        Album("Sev Orr, Tal Ivo", "Unknown", (Track("Unknown", 1),), folder="skip/unknown"),
    )[0]
    navidrome.scan(full=True)
    sums = {p: file_digest(p) for p in (linked, listed_already, identified, unknown)}
    lists = {
        "Mara Quill, Nox Tern": ["Mara Quill", "Nox Tern"],
        "Oda Fenn, Pim Vale": ["Oda Fenn", "Pim Vale"],
        "Quin Arl, Ros Bey": ["Quin Arl", "Ros Bey"],
        "Sev Orr, Tal Ivo": ["Sev Orr", "Tal"],  # (not exactly its text)
    }
    async with world_for(navidrome, tmp_path, lists) as world:
        summary = await world.lists.once()
        rows = {r["path"]: r for r in await world.store.fetchall("SELECT * FROM artist_lists")}
        asked = list(world.lists.sources.asked)  # type: ignore[attr-defined]
        again = await world.lists.once()  # (nothing due: asked again only after a while)
    assert rows["skip/linked/1-01 Linked.flac"]["reason"] == "hard_link"
    assert rows["skip/listed/1-01 Listed.flac"]["reason"] == "listed"
    assert rows["skip/identified/1-01 Identified.flac"]["reason"] == "identified"
    assert rows["skip/unknown/1-01 Unknown.flac"]["state"] == "unresolved"
    assert summary.unresolved >= 1  # (and other tests' songs, in this module's library)
    assert "Mara Quill, Nox Tern" not in asked  # (a hard link is not asked about)
    assert again.looked == 0
    for path, digest in sums.items():
        assert file_digest(path) == digest
        assert leftovers(path.parent) == []


async def test_report_only_writes_and_records_nothing(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    path = write_album(
        navidrome.music,
        Album(
            "Uma Pell, Vix Orr",
            "Report",
            (Track("Report", 1),),
            write_album_artist=False,
            folder="report/one",
        ),
    )[0]
    navidrome.scan(full=True)
    digest = file_digest(path)
    lists = {"Uma Pell, Vix Orr": ["Uma Pell", "Vix Orr"]}
    async with world_for(navidrome, tmp_path, lists, report=True) as world:
        summary = await world.lists.once()
        rows = await world.store.fetchall("SELECT * FROM artist_lists")
    assert summary.written["link"] >= 1 and summary.albums >= 1  # (an album of one song)
    assert "would be given" in summary.line(True)
    assert rows == [] and file_digest(path) == digest


@pytest.mark.parametrize("step", ["intended", "staged", "linked", "swapped", "verified"])
@pytest.mark.parametrize("how", ["canceled", "failed"])
async def test_a_write_stopped_at_any_step_leaves_the_file_as_it_was(
    navidrome: NavidromeInstance, tmp_path: Path, step: str, how: str
) -> None:
    folder = f"stops/{how}-{step}"
    person = f"Wen Arc {how.title()} {step.title()}"  # (each test's own song)
    joined = f"{person}, Yul Bree"
    path = write_album(
        navidrome.music,
        Album(
            joined,
            f"Stopped {how} {step}",
            (Track("Stopped", 1),),
            write_album_artist=False,
            folder=folder,
        ),
    )[0]
    navidrome.scan(targets=[folder])
    digest = file_digest(path)
    before = songs(navidrome, folder)[path.name]
    async with world_for(navidrome, tmp_path, {joined: [person, "Yul Bree"]}) as w:
        with anyio.CancelScope() as scope:

            async def stop(name: str) -> None:
                if name != f"write {step}":
                    return
                if how == "failed":
                    raise RuntimeError("a failure")
                scope.cancel()
                await anyio.lowlevel.checkpoint()

            w.files.step = stop
            try:
                await w.lists.once()
            except RuntimeError:
                assert how == "failed"
        w.files.step = None
        query = "SELECT * FROM artist_lists WHERE path = ?", [f"{folder}/{path.name}"]
        row = await w.store.fetchone(*query)
        assert row is not None and row["state"] != "written"
        # Put back over the copy where no scan could be waited for (a stop, or a failure
        # after the scan): the row keeps that one is owed, and the recovery asks for it.
        owed = step == "verified" or (step == "swapped" and how == "canceled")
        if owed:
            assert row["phase"] == "write" and json.loads(row["work"])["reread"]
            assert await w.files.recover() == 1
            row = await w.store.fetchone(*query)
        assert row["phase"] == ""
        assert await w.files.recover() == 0
    assert file_digest(path) == digest
    assert leftovers(path.parent) == []
    navidrome.scan(targets=[folder])
    after = songs(navidrome, folder)[path.name]
    assert (after["id"], after["size"]) == (before["id"], path.stat().st_size)
    assert people(after) == [joined]


async def test_music_folder_files_are_changed_only_when_allowed_and_given_back_after(
    navidrome: NavidromeInstance, tmp_path: Path
) -> None:
    """``[catalog] artist_lists_library``: off (the default), a music folder file is left
    as it is and counted; on, it gets its list; off again, it is given back."""
    path = write_album(
        navidrome.music,
        Album("Nia Pell, Oto Varn", "Allowed", (Track("Allowed", 1),), write_album_artist=False,
              folder="allowed/one"),
    )[0]  # fmt: skip
    navidrome.scan(targets=["allowed/one"])
    tags = raw_tags(path)
    lists = {"Nia Pell, Oto Varn": ["Nia Pell", "Oto Varn"]}
    async with world_for(navidrome, tmp_path, lists, library=False) as world:
        summary = await world.lists.once()
        assert summary.skipped["not_allowed"] >= 1 and summary.written["link"] == 0
        assert world.lists.sources.asked == []  # type: ignore[attr-defined]  # (not asked)
        assert raw_tags(path) == tags
        assert await world.store.fetchall("SELECT * FROM artist_lists") == []
        world.lists.library = True
        assert world.lists._wake.is_set()  # (a pass follows at once)
        assert (await world.lists.once()).written["link"] >= 1
        assert raw_tags(path)["artists"] == ["Nia Pell", "Oto Varn"]
        world.lists.library = False
        assert (await world.lists.once()).undone >= 1
    assert raw_tags(path) == tags
    song = songs(navidrome, "allowed/one")[path.name]
    assert people(song) == ["Nia Pell, Oto Varn"]


@pytest.mark.parametrize("stop", [None, "linked", "swapped"])
async def test_without_hard_links_the_old_file_is_kept_as_a_copy(
    navidrome: NavidromeInstance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str | None
) -> None:
    """A file system without hard links (a union, say): the backup is a whole copy, and a
    write stopped before its commit still ends with the old file."""
    folder = f"copies/{stop or 'written'}"
    joined = f"Pim Arl {stop or 'Written'}, Qay Bo"
    path = write_album(
        navidrome.music,
        Album(joined, f"Copies {stop}", (Track("Copied", 1),), write_album_artist=False,
              folder=folder),
    )[0]  # fmt: skip
    navidrome.scan(targets=[folder])
    digest = file_digest(path)

    def no_links(*args: Any, **kwargs: Any) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    names = [f"Pim Arl {stop or 'Written'}", "Qay Bo"]
    async with world_for(navidrome, tmp_path, {joined: names}) as world:
        with anyio.CancelScope() as scope:

            async def at(name: str) -> None:
                if stop and name == f"write {stop}":
                    assert path.with_name(next(
                        p.name for p in path.parent.iterdir() if p.name.endswith(".keep")
                    )).read_bytes() == path.read_bytes() or stop == "swapped"  # fmt: skip
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

            world.files.step = at
            await world.lists.once()
        world.files.step = None
        # (Put back over the copy by a stop: a scan owed, which the recovery asks for.)
        assert await world.files.recover() == (1 if stop == "swapped" else 0)
        assert await world.files.recover() == 0
    assert leftovers(path.parent) == []
    if stop is None:
        assert raw_tags(path)["artists"] == names
    else:
        assert file_digest(path) == digest
