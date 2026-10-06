"""Suite M - the cleanup of unused releases and startup checks,
against a real Navidrome: its own database (read only) says who used what.

- A catalog album nobody used is taken out ``unused_days`` after it was added (whole
  releases only); anything any user did with it even once keeps it: a favorite (also
  one taken back), a rating or play of a song or of the album, a playlist entry, a play
  queue, a bookmark, delivered audio (an offline download), a stream Shijhon served. A
  dry run takes nothing out; ``shijhon cleanup`` lists the same.
- Taken out, it is kept as a record: a view of an old ID shows it as it was, its
  cover comes from the record and a plain stream plays from the add-ons, writing nothing;
  a use (what commits a catalog album, or a stream its audio needs converting for) or
  a commit adds it back with the same song IDs, and it plays.
- A fill taken out leaves its album shown complete, never filled automatically; its
  next use (an old ID) fills it again with the same song IDs. A favorite of the album
  given before the fill does not keep it; one given after does.
- A use found once the files are gone, or a failure, puts the release back; so does the
  next start after a stop in the middle.
- Placeholders are written only while Navidrome keeps missing files (``PurgeMissing``
  "never", from its configuration, else the stated setting); while Navidrome does not
  answer, the refusal is not kept for long.
- At startup an interrupted swap is put right (a file already right is kept).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.cleanup import Swept
from shijhon.cli import main
from shijhon.delivery.expiry import DeliveredAudio
from shijhon.navidrome.checks import PlaceholderWrites, StartupChecks
from shijhon.navidrome.client import NavidromeError
from shijhon.navidrome.usage import NOT_PRESENT, UsageSource, open_read_only
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import MaterializeError, ReplaceError, _Planned, _row_track
from shijhon.views.ids import CatalogId
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, cover_image, write_album
from tests.harness.logs import collected
from tests.harness.replay import Replay
from tests.harness.subsonic import SubsonicClient

DAY = 86400.0
LISTENER = ("listener", "listener-password")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    nd.create_user(*LISTENER)
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("cleanup"),
        warm_ahead_depth=0,
        navidrome_database=nd.data / "navidrome.db",
        cleanup={"mode": "on"},
    ) as w:
        yield w


def call(world: DeliveryWorld, work: Any) -> Any:
    return world.server.call(work)


def listener(world: DeliveryWorld) -> SubsonicClient:
    return SubsonicClient(world.server.base_url, *LISTENER)


def made(world: DeliveryWorld, key: str, count: int = 2) -> tuple[str, list[str], str]:
    """A catalog album added to the library: (release, song IDs, album ID)."""
    release = catalog_release(key, f"Album {key}", f"Artist {key}", count)
    result = world.materialize(release)
    return str(release.ref), [result.created[t.ref] for t in release.tracks], result.album_id


def sweep(world: DeliveryWorld, *, days: float = 31, **settings: Any) -> Swept:
    """The daily check, ``days`` from now."""
    cleanup = world.services.cleanup
    assert cleanup is not None
    saved = {k: getattr(cleanup, k) for k in ("clock", *settings)}
    cleanup.clock = lambda: time.time() + days * DAY
    for key, value in settings.items():
        setattr(cleanup, key, value)
    try:
        done: Swept = call(world, cleanup.sweep)
    finally:
        for key, value in saved.items():
            setattr(cleanup, key, value)
    return done


def in_library(world: DeliveryWorld, release: str) -> bool:
    async def find() -> bool:
        row = await world.services.store.fetchone("SELECT 1 FROM releases WHERE ref = ?", [release])
        return row is not None

    return bool(call(world, find))


def listed(world: DeliveryWorld, release: str) -> Any:
    """The release's entry in the last check's listing (None: not due)."""
    cleanup = world.services.cleanup
    assert cleanup is not None and cleanup.last is not None and cleanup.last.listed is not None
    return next((c for c in cleanup.last.listed.due if c.ref == release), None)


def test_unused_catalog_albums_are_taken_out_and_anything_used_keeps_one(
    world: DeliveryWorld,
) -> None:
    admin, other = world.client(), listener(world)
    unused, unused_songs, unused_album = made(world, "m-unused")
    uses: dict[str, str] = {}
    release, songs, album = made(world, "m-starred")
    other.ok("star", {"id": songs[1]})  # another user's favorite
    uses[release] = "favorited"
    release, songs, _ = made(world, "m-once")
    admin.ok("star", {"id": songs[0]})
    admin.ok("unstar", {"id": songs[0]})
    uses[release] = "favorited, rated or played once"
    release, songs, _ = made(world, "m-played")
    other.ok("scrobble", {"id": songs[0], "submission": "true"})
    uses[release] = "played"
    release, songs, _ = made(world, "m-listed")
    admin.ok("createPlaylist", {"name": "m-list", "songId": songs[0]})
    uses[release] = "in a playlist"
    release, _, album = made(world, "m-album-rated")
    other.ok("setRating", {"id": album, "rating": 4})
    uses[release] = "its album rated"
    release, _, album = made(world, "m-album-once")
    other.ok("star", {"albumId": album})
    other.ok("unstar", {"albumId": album})
    uses[release] = "its album favorited, rated or played once"
    release, songs, _ = made(world, "m-queued")
    other.ok("savePlayQueue", {"id": songs[0]})
    uses[release] = "in a play queue"
    release, songs, _ = made(world, "m-bookmarked")
    admin.ok("createBookmark", {"id": songs[1], "position": 1000})
    uses[release] = "bookmarked"
    addon = world.addon("Cleanup source")
    world.add_source(addon)
    try:
        streamed, _, _ = world.placeholder_track("m-streamed", [addon])
        # A stream Shijhon served (clients that never report plays).
        assert other.request("stream", {"id": streamed}).status_code == 200
        downloaded, _, _ = world.placeholder_track("m-downloaded", [addon])
        assert admin.request("download", {"id": downloaded}).status_code == 200
    finally:
        world.clear_sources()
    young, _, _ = made(world, "m-young")

    async def younger() -> None:
        await world.services.store.execute(
            "UPDATE releases SET created_at = ? WHERE ref = ?", [time.time() + 20 * DAY, young]
        )

    call(world, younger)
    done = sweep(world)  # (other tests' unused albums may go too)
    assert "Artist m-unused - Album m-unused" in done.removed
    assert done.failed == 0 and done.kept == 0
    assert not in_library(world, unused)
    for release, why in uses.items():
        assert in_library(world, release), release
        assert why in listed(world, release).kept, (release, listed(world, release).kept)
    for song, why in ((streamed, "streamed or downloaded"), (downloaded, "delivered audio")):
        row = call(world, lambda s=song: world.services.store.fetchone(
            "SELECT release_ref FROM placeholders WHERE song_id = ?", [s]))  # fmt: skip
        assert why in " ".join(listed(world, row["release_ref"]).kept)
    assert in_library(world, young) and listed(world, young) is None
    # Gone from the library (Navidrome keeps them as missing files) ...
    for song in unused_songs:
        assert world.nd.native("GET", f"song/{song}").json()["missing"] is True
    assert unused_album not in {
        a["id"]
        for a in admin.ok("getAlbumList2", {"type": "newest", "size": 500})["albumList2"].get(
            "album", []
        )
    }


def test_a_dry_run_takes_nothing_out_and_the_command_lists_the_same(
    world: DeliveryWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    release, _, _ = made(world, "m-dry")
    fresh, _, _ = made(world, "m-dry-fresh")

    async def added_long_ago() -> None:
        await world.services.store.execute(
            "UPDATE releases SET created_at = ? WHERE ref = ?", [time.time() - 40 * DAY, release]
        )

    call(world, added_long_ago)
    done = sweep(world, days=0, mode="dry_run")
    assert done.listed is not None
    assert release in {c.ref for c in done.listed.unused}
    assert fresh not in {c.ref for c in done.listed.due}  # added just now: not due
    assert done.removed == [] and in_library(world, release)
    assert sweep(world, mode="off").listed is None  # off: not even listed
    # The command reads both databases (read only) and lists what a check does now.
    state = world.tmp / "state"
    main(["cleanup", "--database", str(state / "shijhon.sqlite3"),
          "--navidrome-db", str(world.nd.data / "navidrome.db")])  # fmt: skip
    out = capsys.readouterr().out
    assert out.startswith("Cleanup (off: nothing is taken out)")
    taken = out.split("Take out:")[1].split("Kept (in use):")[0] if "Take out:" in out else ""
    listed_now = {c.ref for c in done.listed.unused}
    assert {line.rsplit(", ", 1)[1] for line in taken.strip().splitlines()} == listed_now
    assert "Artist m-dry-fresh" not in out and "not due yet:" in out


def database_copy(world: DeliveryWorld, name: str) -> Path:
    """A copy of Navidrome's database as it is now (SQLite's own backup)."""
    target = world.tmp / name
    target.unlink(missing_ok=True)
    source = open_read_only(world.nd.data / "navidrome.db")
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        source.close()
        copy.close()
    return target


@contextmanager
def reading(world: DeliveryWorld, database: Path) -> Iterator[None]:
    """The cleanup reading ``database`` as Navidrome's."""
    cleanup = world.services.cleanup
    assert cleanup is not None
    real = cleanup.usage
    cleanup.usage = UsageSource(database, export=False)
    try:
        yield
    finally:
        cleanup.usage = real


def aged(world: DeliveryWorld, *releases: str, days: float = 40) -> None:
    async def added_long_ago() -> None:
        for n, release in enumerate(releases):  # (in this order at a listing)
            await world.services.store.execute(
                "UPDATE releases SET created_at = ? WHERE ref = ?",
                [time.time() - days * DAY + n, release],
            )

    call(world, added_long_ago)


@contextmanager
def listing_only(world: DeliveryWorld, *releases: str, then: Any = None) -> Iterator[None]:
    """The cleanup's listings hold only ``releases`` (other tests' are left alone); ``then``
    runs once a listing is made, before anything is taken out."""
    cleanup = world.services.cleanup
    assert cleanup is not None
    real_listed = cleanup._listed

    async def listed(**settings: Any) -> Any:
        found = await real_listed(**settings)
        found.due = [c for c in found.due if c.ref in releases]
        if then is not None:
            await anyio.to_thread.run_sync(then)  # (it may ask the server, as a client)
        return found

    cleanup._listed = listed  # type: ignore[method-assign]
    try:
        yield
    finally:
        cleanup._listed = real_listed  # type: ignore[method-assign]


def navidrome_rows(world: DeliveryWorld, songs: list[str]) -> set[tuple[str, str, bool]]:
    conn = open_read_only(world.nd.data / "navidrome.db")
    try:
        rows = conn.execute(
            "SELECT id, path, missing FROM media_file WHERE id IN (?, ?)", songs
        ).fetchall()
    finally:
        conn.close()
    return {(str(r["id"]), str(r["path"]), bool(r["missing"])) for r in rows}


@pytest.mark.canary  # (how Navidrome records a song's path and a missing file is its own)
def test_navidromes_records_have_the_placeholders_where_shijhon_recorded_them(
    world: DeliveryWorld,
) -> None:
    """What the cleanup's checks rest on: Navidrome's ``media_file`` has each placeholder under
    its song ID at the library path Shijhon recorded, and keeps that record - marked
    missing - once the file is taken out."""
    release, songs, _ = made(world, "m-recorded")
    recorded = call(world, lambda: world.services.store.fetchall(
        "SELECT song_id, path FROM placeholders WHERE release_ref = ?", [release]))  # fmt: skip
    there = {(str(r["song_id"]), str(r["path"])) for r in recorded}
    assert navidrome_rows(world, songs) == {(song, path, False) for song, path in there}
    removal = call(world, lambda: world.services.engine.remove_release(release))
    assert removal.removed == 2
    assert navidrome_rows(world, songs) == {(song, path, True) for song, path in there}


def test_a_database_that_is_not_this_librarys_takes_nothing_out(
    world: DeliveryWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    """In another Navidrome's database - or an old copy, or an empty one - nobody uses
    anything, so every release would look unused. The check refuses when the songs of a
    release that looks unused are not in it at their recorded paths: nothing goes, and it
    says so."""
    release, songs, _ = made(world, "m-foreign")
    other, _, _ = made(world, "m-foreign-other")
    aged(world, release, other)
    files = world.placeholder_files()
    # A database from before the album was added (an old copy; another Navidrome's alike).
    old = database_copy(world, "old-navidrome.db")
    edit = sqlite3.connect(old)
    edit.execute("DELETE FROM media_file WHERE id IN (?, ?)", songs)
    edit.commit()
    edit.close()
    with reading(world, old), collected("shijhon.cleanup") as lines:
        done = sweep(world, days=0)
        assert done.removed == [] and done.outcomes == {}
        assert "do not have the songs of" in done.refused
        assert "Artist m-foreign - Album m-foreign" in done.refused
        assert "not this library's current ones" in done.refused
        assert any(
            line.startswith("cleanup: nothing is taken out: Navidrome's records do not")
            for line in lines
        )
        dry = sweep(world, days=0, mode="dry_run")  # a dry run says the same
        assert dry.refused == done.refused
        [stranger] = [c for c in dry.listed.due if c.stranger]  # type: ignore[union-attr]
        assert stranger.ref == release
        state = world.tmp / "state"
        with pytest.raises(SystemExit):
            main(["cleanup", "--database", str(state / "shijhon.sqlite3"),
                  "--navidrome-db", str(old)])  # fmt: skip
        out = capsys.readouterr().out
        assert "Refused - nothing is taken out: Navidrome's records do not have" in out
        assert "Not taken out (refused):" in out and "Take out:" not in out
        assert "not known to be used" in out and "its songs are not in Navidrome's records" in out
    assert in_library(world, release) and world.placeholder_files() == files
    # The right database at the listing, the other one once the removal checks again (its
    # locks held): refused there, and the check ends - the next release, whose songs that
    # database does have, stays too.
    cleanup = world.services.cleanup
    assert cleanup is not None

    def swap() -> None:
        cleanup.usage = UsageSource(old, export=False)

    with (
        reading(world, world.nd.data / "navidrome.db"),
        listing_only(world, release, other, then=swap),
    ):
        done = sweep(world, days=0)
    assert done.removed == [] and done.kept == 0 and done.outcomes == {}
    # (told at once: what is read now is another file than the listing was read from)
    assert "came from another database file as Artist m-foreign - Album m-foreign" in done.refused
    assert in_library(world, release) and in_library(world, other)
    assert world.placeholder_files() == files
    assert not any(world.nd.native("GET", f"song/{s}").json()["missing"] for s in songs)
    # Something else than a Navidrome database: refused, not an error.
    empty = world.tmp / "empty.db"
    sqlite3.connect(empty).close()
    with reading(world, empty):
        done = sweep(world, days=0)
    assert "what is read as Navidrome's records is not: " in done.refused
    assert "is not a Navidrome database this version can read" in done.refused
    assert done.removed == []


def test_an_old_copy_of_this_librarys_database_takes_nothing_out(world: DeliveryWorld) -> None:
    """A copy of this library's own database - a backup of Navidrome's, say - made
    after an album was added has its songs at their paths, and none of the uses since. Only
    Navidrome's current database shows what Shijhon does through Navidrome: once the
    files of the first release are taken out, its songs must be shown as missing there.
    They are not in a copy: the release is put back, and the check ends."""
    release, songs, _ = made(world, "m-copy")
    other, _, _ = made(world, "m-copy-other")
    aged(world, release, other)
    copy = database_copy(world, "copied-navidrome.db")
    world.client().ok("star", {"id": songs[0]})  # a use the copy does not know of
    world.client().ok("unstar", {"id": songs[0]})  # (taken back: still a use)
    files = world.placeholder_files()
    with (
        reading(world, copy),
        listing_only(world, release, other),
        collected("shijhon.cleanup") as lines,
    ):
        done = sweep(world, days=0)
    assert done.removed == [] and done.kept == 0 and done.outcomes == {}
    assert "still show the songs of Artist m-copy - Album m-copy as present" in done.refused
    assert "the release was put back" in done.refused
    assert any(line.startswith("cleanup: nothing more is taken out") for line in lines)
    assert in_library(world, release) and in_library(world, other)
    assert world.placeholder_files() == files
    assert not any(world.nd.native("GET", f"song/{s}").json()["missing"] for s in songs)
    row = call(world, lambda: world.services.store.fetchone(
        "SELECT removing_at FROM releases WHERE ref = ?", [release]))  # fmt: skip
    assert row["removing_at"] is None
    # The current database: the first is kept (its favorite, taken back), the other goes.
    with listing_only(world, release, other):
        done = sweep(world, days=0)
    assert done.refused == "" and done.removed == ["Artist m-copy-other - Album m-copy-other"]
    assert in_library(world, release) and not in_library(world, other)


def test_a_copy_made_while_a_release_was_out_takes_nothing_out(world: DeliveryWorld) -> None:
    """A copy of Navidrome's database made while a release was taken out has its songs
    as missing files - as they are once its files are gone. The release was added back and
    favorited since: before it is touched its songs must be there as present files, so by
    that copy it stays; and the next release is found out as its files go."""
    release, songs, _ = made(world, "m-out-copy")
    other, _, _ = made(world, "m-out-copy-other")
    assert call(world, lambda: world.services.engine.remove_release(release)).removed == 2
    copy = database_copy(world, "copied-while-out.db")
    again = catalog_release("m-out-copy", "Album m-out-copy", "Artist m-out-copy", 2)
    world.materialize(again)  # added back (a commit), with the same song IDs
    assert in_library(world, release)
    world.client().ok("star", {"id": songs[0]})  # used since
    aged(world, release, other)
    files = world.placeholder_files()
    with reading(world, copy), listing_only(world, release, other):
        done = sweep(world, days=0)
    assert done.removed == [] and done.outcomes[release] == f"kept: {NOT_PRESENT}"
    assert "still show the songs of Artist m-out-copy-other" in done.refused
    assert in_library(world, release) and in_library(world, other)
    assert world.placeholder_files() == files
    assert world.client().ok("getSong", {"id": songs[0]})["song"].get("starred")


def test_a_song_swapped_while_it_is_listed_does_not_refuse_the_check(
    world: DeliveryWorld,
) -> None:
    """A listing that finds a song elsewhere than its row said is made once more before it
    is believed: a delivery in another format finishing while the rows were read."""
    release, _, _ = made(world, "m-settling")
    aged(world, release)
    cleanup = world.services.cleanup
    assert cleanup is not None
    real_listing = cleanup._listing
    listings: list[str] = []

    def listing(newer_than: float, **settings: Any) -> Any:
        found = real_listing(newer_than, **settings)
        if not listings:  # as if one of its songs was elsewhere at that moment
            found.refused = "Navidrome's records do not have the songs of ..."
        listings.append(found.refused)
        return found

    cleanup._listing = listing  # type: ignore[method-assign]
    try:
        with listing_only(world, release):
            done = sweep(world, days=0)
    finally:
        cleanup._listing = real_listing  # type: ignore[method-assign]
    assert len(listings) == 2 and listings[1] == ""
    assert done.refused == "" and done.removed == ["Artist m-settling - Album m-settling"]


@pytest.fixture(scope="module")
def sharing(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    """A Navidrome with sharing on (it is off by default), the cleanup a dry run."""
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    nd.create_user(*LISTENER)
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("cleanup-seen"),
        warm_ahead_depth=0,
        navidrome_database=nd.data / "navidrome.db",
        cleanup={"mode": "dry_run"},
    ) as w:
        yield w


def seen(world: DeliveryWorld, release: str) -> list[str]:
    return list(call(world, lambda: world.services.engine.seen_uses(release)))


def test_a_use_removed_later_still_keeps_its_release(sharing: DeliveryWorld) -> None:
    """A playlist entry, a play queue, a bookmark or a share that was
    removed again still counts as a use - Navidrome's database shows only what is there
    now, so every check records what it sees, in any mode, of releases not due yet too.
    What begins and ends between two checks is not seen (the limit)."""
    world = sharing
    user = listener(world)
    uses: dict[str, tuple[str, Any]] = {}
    release, songs, _ = made(world, "m-seen-playlist")
    playlist = user.ok("createPlaylist", {"name": "m-seen", "songId": songs[0]})["playlist"]["id"]
    uses[release] = ("in a playlist", lambda: user.ok("deletePlaylist", {"id": playlist}))
    release, songs, _ = made(world, "m-seen-queue")
    user.ok("savePlayQueue", {"id": songs[1], "current": songs[1]})
    uses[release] = ("in a play queue", lambda: user.ok("savePlayQueue", {}))
    release, songs, _ = made(world, "m-seen-bookmark")
    user.ok("createBookmark", {"id": songs[0], "position": 1000})
    uses[release] = ("bookmarked", lambda s=songs[0]: user.ok("deleteBookmark", {"id": s}))
    release, songs, _ = made(world, "m-seen-share")
    share = user.ok("createShare", {"id": songs[0]})["shares"]["share"][0]["id"]
    uses[release] = ("shared", lambda: user.ok("deleteShare", {"id": share}))
    brief, songs, _ = made(world, "m-seen-brief")
    never, _, _ = made(world, "m-seen-never")
    # A check - off, it lists nothing and takes nothing out, and still notes the uses.
    assert sweep(world, days=0, mode="off").listed is None
    for release, (why, _) in uses.items():
        assert seen(world, release) == [why], release
    assert seen(world, brief) == [] and seen(world, never) == []
    # The uses end; one begins and ends with no check between (not seen: the limit).
    for _, remove in uses.values():
        remove()
    user.ok("createBookmark", {"id": songs[0], "position": 1000})
    user.ok("deleteBookmark", {"id": songs[0]})
    conn = open_read_only(world.nd.data / "navidrome.db")
    try:  # Navidrome shows no trace of them
        for table in ("playlist_tracks", "bookmark", "share"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
        assert [str(r[0] or "") for r in conn.execute("SELECT items FROM playqueue")] in ([], [""])
    finally:
        conn.close()
    done = sweep(world, mode="on")  # 31 days on
    for release, (why, _) in uses.items():
        assert in_library(world, release), release
        assert listed(world, release).kept == [f"{why} once"], release
    assert not in_library(world, brief) and not in_library(world, never)
    assert len(done.removed) == 2 and done.kept == 0 and done.failed == 0
    # The engine keeps to it whoever asks (a command, a later caller without a check).
    engine = world.services.engine
    [kept] = [r for r, (why, _) in uses.items() if why == "shared"]
    removal = call(world, lambda: engine.remove_release(kept))
    assert removal.kept == ["used before (shared)"] and removal.removed == 0
    assert in_library(world, kept)


def test_a_dry_run_notes_uses_and_one_noted_as_the_files_go_puts_them_back(
    sharing: DeliveryWorld,
) -> None:
    """The dashboard's dry run notes what it sees too (also of releases not due). And a use
    noted while a release is being taken out - by a dry run beside the daily check, say -
    is found in the removal's own transaction: the release is put back."""
    world = sharing
    user = listener(world)
    release, songs, _ = made(world, "m-seen-dry")
    user.ok("createBookmark", {"id": songs[0], "position": 5})
    cleanup = world.services.cleanup
    assert cleanup is not None
    done = call(world, cleanup.dry_run)
    assert done.listed is not None and release not in {c.ref for c in done.listed.due}
    assert seen(world, release) == ["bookmarked"]
    other, songs, _ = made(world, "m-seen-racing")
    engine = world.services.engine

    async def check(rows: list[Any], gone: bool) -> list[str]:
        if gone:  # nothing found by this check - and a use noted by another meanwhile
            await engine.note_uses({other: ["in a play queue"]}, time.time())
        return []

    removal = call(world, lambda: engine.remove_release(other, check=check))
    assert removal.kept == ["used before (in a play queue)"] and removal.removed == 0
    assert in_library(world, other)
    assert not any(world.nd.native("GET", f"song/{s}").json()["missing"] for s in songs)
    # Nothing is noted of a release that is gone (its record would outlive it).
    gone, _, _ = made(world, "m-seen-gone")
    assert call(world, lambda: engine.remove_release(gone)).removed == 2
    call(world, lambda: engine.note_uses({gone: ["shared"]}, time.time()))
    assert seen(world, gone) == []


def test_a_use_seen_as_a_release_is_taken_out_is_recorded_too(sharing: DeliveryWorld) -> None:
    """A use that appears between the listing and the removal's own check (its locks held)
    keeps the release then, and from then on once it is removed again."""
    world = sharing
    user = listener(world)
    release, songs, _ = made(world, "m-seen-late")
    made_lists: list[str] = []

    def use() -> None:
        made_lists.append(
            user.ok("createPlaylist", {"name": "late", "songId": songs[1]})["playlist"]["id"]
        )

    with listing_only(world, release, then=use):
        done = sweep(world, mode="on")
    assert done.outcomes[release] == "kept: in a playlist" and in_library(world, release)
    assert seen(world, release) == ["in a playlist"]
    user.ok("deletePlaylist", {"id": made_lists[0]})
    done = sweep(world, mode="on")
    assert in_library(world, release) and listed(world, release).kept == ["in a playlist once"]


def release_of(world: DeliveryWorld, song: str) -> str:
    async def read() -> str:
        row = await world.services.store.fetchone(
            "SELECT release_ref FROM placeholders WHERE song_id = ?", [song]
        )
        assert row is not None
        return str(row["release_ref"])

    return str(call(world, read))


def views(client: SubsonicClient, album: str, song: str) -> list[Any]:
    """What a client that synced an album sees when it opens it again (JSON and XML),
    without the album's ``coverArt``: Navidrome sets that when its artwork scan gets to the
    album, which may come between two looks; the cover itself is checked on its own."""
    return [
        without_cover_art(client.request("getAlbum", {"id": album}).json()),
        COVER_ART.sub("", client.request("getAlbum", {"id": album}, fmt="xml").text),
        without_cover_art(client.request("getMusicDirectory", {"id": album}).json()),
        COVER_ART.sub("", client.request("getMusicDirectory", {"id": album}, fmt="xml").text),
        without_cover_art(client.request("getSong", {"id": song}).json()),
    ]


COVER_ART = re.compile(r' coverArt="[^"]*"')
COVER_ART_JSON = re.compile(r'"coverArt":"[^"]*",?')


def raw_texts(client: SubsonicClient, album: str) -> list[str]:
    """The album's JSON answers byte for byte, but for its ``coverArt`` (as in ``views``)."""
    return [
        COVER_ART_JSON.sub("", client.request(method, {"id": album}).text)
        for method in ("getMusicDirectory", "getAlbum")
    ]


def without_cover_art(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: without_cover_art(v) for k, v in value.items() if k != "coverArt"}
    if isinstance(value, list):
        return [without_cover_art(v) for v in value]
    return value


def test_a_view_of_an_old_id_shows_the_album_as_it_was_and_writes_nothing(
    world: DeliveryWorld,
) -> None:
    """A client that synced an album taken out opens it again: it is shown as it was
    (Navidrome's own answers, byte for byte, JSON and XML), its cover and a plain stream
    answer - and nothing comes back, so syncing never undoes the cleanup."""
    addon = world.addon("Old IDs")
    world.add_source(addon)
    try:
        song, _, audio = world.placeholder_track("m-old", [addon])
        release = release_of(world, song)
        admin = world.client()
        album = admin.ok("getSong", {"id": song})["song"]["albumId"]
        before = views(admin, album, song)
        # An album of three songs on two discs: the songs in Navidrome's order, as before.
        three = catalog_release("m-old-3", "Album m-old-3", "Artist m-old-3", 3, discs=2)
        made3 = world.materialize(three)
        songs3 = [made3.created[t.ref] for t in three.tracks]
        before3 = views(admin, made3.album_id, songs3[2])
        texts3 = raw_texts(admin, made3.album_id)
        removed_now = sweep(world).removed
        assert "Artist m-old - Title m-old" in removed_now
        assert "Artist m-old-3 - Album m-old-3" in removed_now
        assert views(admin, made3.album_id, songs3[2]) == before3
        assert raw_texts(admin, made3.album_id) == texts3
        removed, old_ids = world.services.removed, world.services.old_ids
        assert removed is not None and old_ids is not None
        shown, added = removed.shown, old_ids.added_back
        files = world.placeholder_files()
        assert views(admin, album, song) == before
        assert removed.shown == shown + 4  # (getSong is Navidrome's own answer)
        # Another user sees it too; wrong credentials get Navidrome's error, nothing shown.
        assert listener(world).ok("getAlbum", {"id": album})["album"]["song"]
        wrong = SubsonicClient(world.server.base_url, "admin", "not-the-password")
        refused = wrong.request("getAlbum", {"id": album}).json()["subsonic-response"]
        assert refused["status"] == "failed" and removed.shown == shown + 5
        # Covers answer (Navidrome's, without a catalog), in any form a client sends.
        for cover in (f"al-{album}", album, f"mf-{song}", song, f"dc-{album}:1"):
            assert admin.request("getCoverArt", {"id": cover}).status_code == 200
        # A plain stream plays from the add-on; a HEAD answers.
        streamed = admin.request("stream", {"id": song})
        assert streamed.status_code == 200 and streamed.content == audio.read_bytes()
        head = admin.request("stream", {"id": song}, http_method="HEAD")
        assert head.status_code == 200
        assert not in_library(world, release) and old_ids.added_back == added
        assert world.placeholder_files() == files
        assert world.services.engine.removed_release(song) == release
    finally:
        world.clear_sources()


def test_a_view_of_an_old_id_is_the_whole_album_or_an_error(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While Navidrome fails for one of the album's songs,
    the view is an error the client asks again after - never "ok" with fewer songs than
    the album's count, which a client would keep in place of the whole album. Only a
    song Navidrome says it no longer has is left out, and then the album's count and length
    are those of the songs shown. And a format Navidrome answers in XML ("f=XML") is
    answered as "f=xml" is."""
    from shijhon.views import removed as removed_views

    release = catalog_release("m-old-whole", "Album m-old-whole", "Artist m-old-whole", 3)
    made3 = world.materialize(release)
    songs = [made3.created[t.ref] for t in release.tracks]
    album = made3.album_id
    admin = world.client()
    whole = admin.ok("getAlbum", {"id": album})["album"]
    assert "Artist m-old-whole - Album m-old-whole" in sweep(world).removed
    as_xml = admin.request("getAlbum", {"id": album}, fmt="xml").text
    assert as_xml.count("<song ") == 3
    for other in ("XML", "anything"):  # XML for Navidrome, so for the record's view
        assert admin.request("getAlbum", {"id": album, "f": other}, fmt="xml").text == as_xml
    real = removed_views.library_answer
    how = ["unreachable"]

    async def failing(upstream: Any, call: Any) -> Any:
        if call.name != "getSong" or (call.get("id") != songs[1] and how[0] != "all gone"):
            return await real(upstream, call)
        if how[0] == "unreachable":
            return None
        gone = call.rewritten(lambda k, v: "no-such-song" if k == "id" else None)
        answer = await real(upstream, gone)
        if how[0] == "failing":
            return dataclasses.replace(answer, status=503)
        return answer  # "not found": Navidrome no longer has it

    monkeypatch.setattr(removed_views, "library_answer", failing)
    for how[0] in ("unreachable", "failing"):
        for method in ("getAlbum", "getMusicDirectory"):
            failed = admin.request(method, {"id": album}).json()["subsonic-response"]
            assert failed["status"] == "failed" and failed["error"]["code"] == 0, how
            assert "album" not in failed and "directory" not in failed
            text = admin.request(method, {"id": album}, fmt="xml").text
            assert 'status="failed"' in text and "<song " not in text and "<child " not in text
    how[0] = "not found"
    left = admin.ok("getAlbum", {"id": album})["album"]
    assert [s["id"] for s in left["song"]] == [songs[0], songs[2]]
    assert left["songCount"] == 2 < whole["songCount"]
    assert left["duration"] == sum(s["duration"] for s in left["song"])
    text = admin.request("getAlbum", {"id": album}, fmt="xml").text
    assert text.count("<song ") == 2 and ' songCount="2"' in text
    assert f' duration="{left["duration"]}"' in text.split(">")[1]  # (the album's element)
    # None of its songs is Navidrome's any more: no album to show - an error, not the
    # album with a count and no songs.
    how[0] = "all gone"
    for fmt in ("json", "xml"):
        text = admin.request("getAlbum", {"id": album}, fmt=fmt).text  # type: ignore[arg-type]
        assert "failed" in text and "songCount" not in text, fmt
    monkeypatch.undo()
    assert admin.ok("getAlbum", {"id": album})["album"] == whole  # whole again


def test_an_old_id_plays_from_its_record_while_navidrome_cannot_be_asked(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether the owner's own song has the old ID now is asked of Navidrome; while that
    fails, the record stands: a plain stream plays from the add-ons (never a 500)."""
    addon = world.addon("Unasked")
    world.add_source(addon)
    try:
        song, _, audio = world.placeholder_track("m-unasked", [addon])
        assert "Artist m-unasked - Title m-unasked" in sweep(world).removed
        engine = world.services.engine

        async def failing(song_id: str) -> Any:
            raise NavidromeError("native GET: ConnectError")

        monkeypatch.setattr(engine.navidrome, "song", failing)
        engine._missing.clear()
        streamed = world.client().request("stream", {"id": song})
        assert streamed.status_code == 200 and streamed.content == audio.read_bytes()
        assert engine.removed_release(song) is not None  # the record stands
    finally:
        monkeypatch.undo()
        world.clear_sources()


# (label, method, parameters): the uses of a song ("S") or album ("A") ID.
USES = [
    ("now playing", "scrobble", {"id": "S", "submission": "false"}),
    ("a playback report", "reportPlayback", {"mediaId": "S", "mediaType": "song",
                                             "state": "playing", "positionMs": "0"}),
    ("a favorite", "star", {"id": "S"}),
    ("the album's favorite", "star", {"albumId": "A"}),
    ("a rating", "setRating", {"id": "S", "rating": "4"}),
    ("a playlist", "createPlaylist", {"name": "m-uses", "songId": "S"}),
    ("a queue's current song", "savePlayQueue", {"id": "S", "current": "S"}),
    ("a bookmark", "createBookmark", {"id": "S", "position": "1000"}),
    ("a share", "createShare", {"id": "A"}),
    ("a download", "download", {"id": "S"}),
]  # fmt: skip
# ... and requests that are no use: they add nothing back.
NO_USES = [
    ("getSong", {"id": "S"}),
    ("getAlbum", {"id": "A"}),
    ("getMusicDirectory", {"id": "A"}),
    ("getAlbumInfo2", {"id": "A"}),
    ("getCoverArt", {"id": "al-A"}),
    ("getLyricsBySongId", {"id": "S"}),
    ("getSimilarSongs2", {"id": "S"}),
    ("unstar", {"id": "S"}),
    ("setRating", {"id": "S", "rating": "0"}),
    ("download", {"id": "A"}),  # an album's archive: refused anyway
    ("savePlayQueue", {"id": "S"}),  # not its current song
    ("stream", {"id": "S"}),  # a plain stream plays from the add-ons
    ("jukeboxControl", {"action": "set", "id": "S"}),  # the jukebox is off
]


def test_only_a_use_of_an_old_id_adds_it_back_with_the_same_ids(world: DeliveryWorld) -> None:
    """The actions that commit a catalog album are the uses that add a release taken
    out back - with its song IDs; views, covers, plain streams and the rest do not."""
    albums = {label: made(world, f"m-use-{n}", count=1) for n, (label, _, _) in enumerate(USES)}
    idle = made(world, "m-no-use", count=1)
    done = sweep(world)
    assert {f"Artist m-use-{n} - Album m-use-{n}" for n in range(len(USES))} <= set(done.removed)
    client = world.client()

    def params(given: dict[str, str], songs: list[str], album: str) -> dict[str, str]:
        return {k: {"S": songs[0], "A": album, "al-A": f"al-{album}"}.get(v, v)
                for k, v in given.items()}  # fmt: skip

    release, songs, album = idle
    for method, given in NO_USES:
        client.request(method, params(given, songs, album))
        client.request(method, params(given, songs, album), http_method="POST")
    client.request("stream", {"id": songs[0], "format": "mp3"}, http_method="HEAD")  # asks
    assert not in_library(world, release), "no use added it back"
    for label, method, given in USES:
        release, songs, album = albums[label]
        client.request(method, params(given, songs, album))
        assert in_library(world, release), label
        assert world.services.engine.removed_release(songs[0]) is None, label
        assert client.ok("getSong", {"id": songs[0]})["song"]["id"] == songs[0], label
        native = world.nd.native("GET", f"song/{songs[0]}").json()
        assert native["missing"] is False, label  # its file back: the same song ID


def test_a_stream_of_an_old_id_adds_it_back_only_when_its_audio_is_converted(
    world: DeliveryWorld,
) -> None:
    """A stream for a format its audio already is plays from the add-ons as it
    is, adding nothing back; one that needs converting is a use (download-first)."""
    addon = world.addon("Converting")
    world.add_source(addon)
    try:
        song, _, audio = world.placeholder_track("m-convert", [addon])  # FLAC
        release = release_of(world, song)
        assert "Artist m-convert - Title m-convert" in sweep(world).removed
        same = world.client().request("stream", {"id": song, "format": "flac"})
        assert same.status_code == 200 and same.content == audio.read_bytes()
        assert not in_library(world, release)
        converted = world.client().request("stream", {"id": song, "format": "mp3"})
        assert converted.status_code == 200 and converted.content[:3] in (b"ID3", b"\xff\xfb")
        assert in_library(world, release)
        assert world.services.engine.removed_release(song) is None
    finally:
        world.clear_sources()


def test_a_use_is_refused_nothing_while_placeholders_may_not_be_written(
    world: DeliveryWorld,
) -> None:
    """A use while the write gate refuses, or with wrong credentials: nothing comes back,
    and the next use does not try again at once (a use of it then succeeds)."""
    release, songs, album = made(world, "m-gated", count=1)
    assert "Artist m-gated - Album m-gated" in sweep(world).removed
    wrong = SubsonicClient(world.server.base_url, "admin", "not-the-password")
    wrong.request("star", {"id": songs[0]})
    assert not in_library(world, release)
    engine = world.services.engine
    gate = engine.writes_refused

    async def refused(*, fresh: bool = False) -> str:
        return 'Navidrome\'s Scanner.PurgeMissing is "always"'

    engine.writes_refused = refused
    try:
        world.client().request("star", {"albumId": album})
        assert not in_library(world, release) and not engine.restore_due(release)
    finally:
        engine.writes_refused = gate
        engine._restore_failed.clear()
    world.client().ok("scrobble", {"id": songs[0], "submission": "false"})
    assert in_library(world, release)


def test_a_commit_of_an_album_taken_out_brings_back_its_ids(world: DeliveryWorld) -> None:
    release, songs, album = made(world, "m-commit", count=3)
    assert "Artist m-commit - Album m-commit" in sweep(world).removed
    again = catalog_release("m-commit", "Album m-commit", "Artist m-commit", 3)
    result = world.materialize(again)  # as a commit of its catalog ID does
    assert [result.created.get(t.ref) for t in again.tracks] in ([None] * 3, songs)
    assert result.album_id == album and in_library(world, release)

    async def links() -> list[str]:
        rows = await world.services.store.fetchall(
            "SELECT song_id FROM track_links WHERE release_ref = ? ORDER BY track_ref", [release]
        )
        return [str(r["song_id"]) for r in rows]

    assert call(world, links) == songs
    assert world.services.engine.removed_release(songs[0]) is None


def test_a_use_while_its_release_is_taken_out_adds_it_back_after(world: DeliveryWorld) -> None:
    """A use of a release in the middle of being taken out (after its last check, before
    its record is written) waits for the removal's end, then adds it back: the favorite is
    not lost."""
    release, songs, _ = made(world, "m-racing", count=1)
    engine = world.services.engine
    starred: list[int] = []
    checks: list[int] = []

    async def check(rows: list[Any], gone: bool) -> list[str]:
        checks.append(len(rows))
        if len(checks) == 2:  # the files are gone, the record not written yet: a use
            user = threading.Thread(
                target=lambda: starred.append(
                    world.client().request("star", {"id": songs[0]}).status_code
                )
            )
            user.start()
            await anyio.sleep(0.5)  # it waits for the removal's end
            assert not starred
        return []

    removal = call(world, lambda: engine.remove_release(release, check=check))
    assert removal.removed == 1
    deadline = time.monotonic() + 30
    while not starred:
        assert time.monotonic() < deadline, "the use did not finish"
        time.sleep(0.1)
    assert starred == [200] and in_library(world, release)
    assert world.client().ok("getSong", {"id": songs[0]})["song"].get("starred")


def test_a_use_found_once_the_files_are_gone_puts_the_album_back(world: DeliveryWorld) -> None:
    release, songs, album = made(world, "m-race")
    engine = world.services.engine
    checks: list[int] = []

    async def check(rows: list[Any], gone: bool) -> list[str]:
        checks.append(len(rows))
        return ["favorited"] if len(checks) == 2 else []  # a star meanwhile

    removal = call(world, lambda: engine.remove_release(release, check=check))
    assert removal.kept == ["favorited"] and removal.removed == 0 and checks == [2, 2]
    assert in_library(world, release)
    found = world.nd.client().ok("getAlbum", {"id": album})["album"]
    assert sorted(s["id"] for s in found["song"]) == sorted(songs)
    assert engine.removed_release(songs[0]) is None


def test_a_failed_removal_puts_the_album_back(world: DeliveryWorld) -> None:
    release, songs, album = made(world, "m-failing")
    engine = world.services.engine
    until = engine.scans.until
    calls: list[int] = []

    async def failing(folders: Any, ready: Any, **kwargs: Any) -> bool:
        calls.append(1)
        if len(calls) == 1:
            return False  # the scan never confirms the files gone
        return await until(folders, ready, **kwargs)

    engine.scans.until = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(MaterializeError, match="still listed"):
            call(world, lambda: engine.remove_release(release))
    finally:
        engine.scans.until = until  # type: ignore[method-assign]
    assert in_library(world, release)
    found = world.nd.client().ok("getAlbum", {"id": album})["album"]
    assert sorted(s["id"] for s in found["song"]) == sorted(songs)

    async def marked() -> Any:
        row = await world.services.store.fetchone(
            "SELECT removing_at FROM releases WHERE ref = ?", [release]
        )
        return row["removing_at"] if row else "gone"

    assert call(world, marked) is None


def test_a_removal_a_stop_interrupted_is_put_back_at_the_next_start(
    world: DeliveryWorld,
) -> None:
    release, songs, album = made(world, "m-stopped")
    engine = world.services.engine

    async def stop_halfway() -> list[Path]:
        # Marked, its files gone - and then the process stopped.
        await world.services.store.execute(
            "UPDATE releases SET removing_at = ? WHERE ref = ?", [time.time(), release]
        )
        rows = await world.services.store.fetchall(
            "SELECT path FROM placeholders WHERE release_ref = ?", [release]
        )
        return [engine.layout.absolute(str(r["path"])) for r in rows]

    files = call(world, stop_halfway)
    for path in files:
        path.unlink()
    world.nd.scan(full=True)
    assert call(world, engine.repair_removals) == 1
    assert all(path.exists() for path in files)
    found = world.nd.client().ok("getAlbum", {"id": album})["album"]
    assert sorted(s["id"] for s in found["song"]) == sorted(songs)
    assert in_library(world, release)


def test_a_cover_set_aside_comes_back_with_its_album(world: DeliveryWorld) -> None:
    """A stop after the cover was set aside: the next start puts the album back with it."""
    release = catalog_release("m-cover", "Album m-cover", "Artist m-cover", 1)
    result = world.materialize(release, cover=b"\xff\xd8\xff\xe0 a cover")
    engine = world.services.engine
    folder = engine.layout.absolute(result.folder)
    aside = engine._cover_aside(result.folder)

    async def stop_halfway() -> None:
        await world.services.store.execute(
            "UPDATE releases SET removing_at = ? WHERE ref = ?", [time.time(), str(release.ref)]
        )

    call(world, stop_halfway)
    (folder / "cover.jpg").replace(aside)
    for path in folder.iterdir():
        path.unlink()
    folder.rmdir()
    stray = engine.layout.staging / "removing-0000000000000000.jpg"  # nobody's
    stray.write_bytes(b"x")
    assert call(world, engine.repair_removals) == 1
    assert (folder / "cover.jpg").read_bytes() == b"\xff\xd8\xff\xe0 a cover"
    assert not aside.exists() and not stray.exists()
    song = result.created[release.tracks[0].ref]
    native = world.nd.native("GET", f"song/{song}").json()
    assert native["path"].startswith(result.folder) and not native["missing"]


def test_an_add_back_a_stop_interrupted_is_finished_or_undone(world: DeliveryWorld) -> None:
    """Files written and indexed, the process stopped before the records: an old ID finishes
    it (the files are the release's own), or the next start takes them out again."""
    engine = world.services.engine
    halfway: dict[str, tuple[str, list[str], str]] = {}
    for key in ("m-half-asked", "m-half-started"):
        halfway[key] = made(world, key)
        assert f"Artist {key} - Album {key}" in sweep(world).removed

    async def stop_halfway(ref: str) -> None:
        stored = await engine.removed_record(ref)
        assert stored is not None
        record = json.loads(stored["record"])
        rows = record["placeholders"]
        planned = [
            _Planned(_row_track(r), str(r["placeholder_path"]), json.loads(r["tags"])) for r in rows
        ]
        await world.services.store.execute(
            "UPDATE removed_releases SET restoring_at = ? WHERE ref = ?", [time.time(), ref]
        )
        await engine._write(planned, str(record["release"]["folder"]), None)
        await engine.scans.scan([str(record["release"]["folder"])])

    for ref, _, _ in halfway.values():
        call(world, lambda r=ref: stop_halfway(r))
    # A client uses the first one before the start's repair: it is added back.
    ref, songs, album = halfway["m-half-asked"]
    world.client().ok("scrobble", {"id": songs[0], "submission": "false"})
    found = world.client().ok("getAlbum", {"id": album})["album"]
    assert sorted(s["id"] for s in found["song"]) == sorted(songs) and in_library(world, ref)
    assert call(world, lambda: engine.removed_record(ref)) is None
    # The start's repair takes the other one's files out again; its record stays.
    ref, songs, _ = halfway["m-half-started"]
    assert call(world, engine.repair_removals) == 1
    for song in songs:
        assert world.nd.native("GET", f"song/{song}").json()["missing"] is True
    stored = call(world, lambda: engine.removed_record(ref))
    assert stored is not None and stored["restoring_at"] is None
    assert not in_library(world, ref) and engine.removed_release(songs[0]) == ref


def test_an_old_ids_stream_does_not_wait_for_an_add_back_left_half_done(
    world: DeliveryWorld,
) -> None:
    """An add-back of a release taken out was left half done by a stop (its
    files in the library, its mark set: its songs are intercepted until the repair). A
    stream of one of its old IDs for a format its audio already is plays from the add-ons
    by its record at once: it neither waits for that add-back nor adds anything back."""
    engine = world.services.engine
    addon = world.addon("Left half done")
    world.add_source(addon)
    try:
        song, _, audio = world.placeholder_track("m-left-stream", [addon])  # FLAC
        release = release_of(world, song)
        aged(world, release)
        with listing_only(world, release):
            assert sweep(world, days=0).removed == ["Artist m-left-stream - Title m-left-stream"]

        async def stop_halfway() -> None:
            stored = await engine.removed_record(release)
            assert stored is not None
            record = json.loads(stored["record"])
            planned = [
                _Planned(_row_track(r), str(r["placeholder_path"]), json.loads(r["tags"]))
                for r in record["placeholders"]
            ]
            await world.services.store.execute(
                "UPDATE removed_releases SET restoring_at = ? WHERE ref = ?",
                [time.time(), release],
            )
            await engine._write(planned, str(record["release"]["folder"]), None)
            await engine.scans.scan([str(record["release"]["folder"])])
            await engine.load_pending()  # as the next start reads what the stop left

        call(world, stop_halfway)
        assert engine.left() and engine.writing()
        assert world.nd.native("GET", f"song/{song}").json()["missing"] is False
        started = time.monotonic()
        same = world.client().request("stream", {"id": song, "format": "flac"})
        took = time.monotonic() - started
        assert same.status_code == 200 and same.content == audio.read_bytes()
        assert took < 8, took  # (the wait for a pending write is 15 s, then an error)
        assert not in_library(world, release) and engine.left()  # nothing added back
        plain = world.client().request("stream", {"id": song})
        assert plain.status_code == 200 and plain.content == audio.read_bytes()
    finally:
        world.clear_sources()
        call(world, engine.repair_removals)  # what the stop left goes again
    assert not engine.left() and not in_library(world, release)
    assert world.nd.native("GET", f"song/{song}").json()["missing"] is True


def test_an_old_album_id_the_owner_now_has_brings_nothing_back(world: DeliveryWorld) -> None:
    """The owner gets the album itself (the same album tags: the same album ID): its old
    ID shows the owner's album, and the record is dropped."""
    release, [song], album = made(world, "m-taken", count=1)

    async def tags() -> dict[str, list[str]]:
        row = await world.services.store.fetchone(
            "SELECT tags FROM placeholders WHERE song_id = ?", [song]
        )
        assert row is not None
        return dict(json.loads(row["tags"]))

    comments = {k: v for k, v in call(world, tags).items() if not k.startswith("shijhon_")}
    assert "Artist m-taken - Album m-taken" in sweep(world).removed
    owned = world.nd.music / "Owned" / "m-taken" / "01 Real.flac"
    owned.parent.mkdir(parents=True)
    world.audio("m-taken-real").replace(owned)
    tagging.write_flac(owned, comments)
    world.nd.scan(full=True)
    # A use while placeholders may not be written: nothing is added back, and the record
    # goes (not a failure): the IDs are the owner's songs now.
    engine = world.services.engine
    gate = engine.writes_refused

    async def refused(*, fresh: bool = False) -> str:
        return 'Navidrome\'s Scanner.PurgeMissing is "always"'

    engine.writes_refused = refused
    try:
        world.client().ok("star", {"albumId": album})
    finally:
        engine.writes_refused = gate
    assert not in_library(world, release) and engine.restore_due(release)
    assert engine.removed_release(song) is None
    # Its views, covers and streams are Navidrome's, as for any owned album.
    found = world.client().ok("getAlbum", {"id": album})["album"]
    # (Navidrome even gives the owned file the placeholder's song ID: the same tags.)
    paths = [world.nd.native("GET", f"song/{s['id']}").json()["path"] for s in found["song"]]
    assert paths == ["Owned/m-taken/01 Real.flac"]
    direct = world.nd.client()
    assert world.client().request("stream", {"id": song}).content == owned.read_bytes()
    for cover in (f"al-{album}", f"mf-{song}"):
        mine = world.client().request("getCoverArt", {"id": cover}).content
        assert mine == direct.request("getCoverArt", {"id": cover}).content, cover
    assert not in_library(world, release)


def test_a_view_of_an_album_the_owner_now_has_drops_its_record(world: DeliveryWorld) -> None:
    """The same, found by a view (or a stream, or a cover): the record goes, and the owner's
    album is answered as any other."""
    release, [song], album = made(world, "m-taken-view", count=1)
    tags = call(world, lambda: world.services.store.fetchone(
        "SELECT tags FROM placeholders WHERE song_id = ?", [song]))["tags"]  # fmt: skip
    comments = {k: v for k, v in json.loads(tags).items() if not k.startswith("shijhon_")}
    assert "Artist m-taken-view - Album m-taken-view" in sweep(world).removed
    owned = world.nd.music / "Owned" / "m-taken-view" / "01 Real.flac"
    owned.parent.mkdir(parents=True)
    world.audio("m-taken-view-real").replace(owned)
    tagging.write_flac(owned, comments)
    world.nd.scan(full=True)
    view = world.client().request("getAlbum", {"id": album}, fmt="xml").text
    assert view == world.nd.client().request("getAlbum", {"id": album}, fmt="xml").text
    assert world.services.engine.removed_release(song) is None and not in_library(world, release)


def checks(world: DeliveryWorld) -> StartupChecks:
    services = world.services
    return StartupChecks(
        services.navidrome, services.engine, services.store, PlaceholderWrites(services.navidrome)
    )


def test_an_interrupted_swap_is_put_right_at_startup(world: DeliveryWorld) -> None:
    _, [song], _ = made(world, "m-swap", count=1)
    engine = world.services.engine

    async def path() -> str:
        row = await world.services.store.fetchone(
            "SELECT path FROM placeholders WHERE song_id = ?", [song]
        )
        assert row is not None
        return str(row["path"])

    silent = engine.layout.absolute(call(world, path))
    silent_bytes = silent.read_bytes()
    # A replacement stopped after its swap: the silent file in the backup, other audio in
    # place, the database still saying "placeholder".
    backup = engine.layout.staging / f"backup-{song}.flac"
    silent.replace(backup)
    world.audio("m-swap-delivered").replace(silent)
    stale = engine.layout.staging / "left-over.flac"
    stale.write_bytes(b"x")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    startup = checks(world)
    call(world, startup._staging)
    call(world, startup._stale)
    assert silent.read_bytes() == silent_bytes
    assert not backup.exists() and not stale.exists()
    # A retag stopped after its update: the new file is in place, the old one in the
    # backup - the file in place stays.
    backup.write_bytes(silent_bytes)
    call(world, startup._staging)
    assert not backup.exists() and silent.read_bytes() == silent_bytes
    assert world.client().ok("getSong", {"id": song})["song"]["id"] == song


# --- fills of owned albums --------------------------------------------------------------

# Two tracks of a 17-track remastered edition (the replayed catalog's).
OWNED = Album(
    "Neve Ashdown",
    "Northern Letters (Remastered)",
    (Track("Velvet Tides", 11, seconds=67), Track("Crimson Canyon", 17, seconds=24)),
    recording_date="1962",
)


@pytest.fixture(scope="module")
def fills_world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    nd = navidrome_factory()
    write_album(nd.music, OWNED)
    nd.scan(full=True)
    fill = {
        "enabled": True,
        "open_budget_seconds": 15,
        "background_pause_seconds": 0,
        "auto_min_songs": 1,  # the policy fills every album automatically
        "library_pass": "off",
        "pass_start_seconds": 3600,
    }
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("cleanup-fills"),
        catalog=replay.catalog(),
        fill=fill,
        navidrome_database=nd.data / "navidrome.db",
        cleanup={"mode": "on"},
    ) as w:
        yield w


def match_row(world: DeliveryWorld, album: str) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        row = await world.services.store.fetchone(
            "SELECT outcome, planned, cleaned_at, plan FROM album_matches WHERE album_id = ?",
            [album],
        )
        assert row is not None
        return dict(row)

    return dict(call(world, read))


def fill_songs(world: DeliveryWorld, album: str) -> list[str]:
    async def read() -> list[str]:
        rows = await world.services.store.fetchall(
            "SELECT p.song_id FROM placeholders p JOIN releases r ON p.release_ref = r.ref"
            " WHERE r.owned_album_id = ? ORDER BY p.song_id",
            [album],
        )
        return [str(r["song_id"]) for r in rows]

    return list(call(world, read))


def test_a_fill_taken_out_is_shown_complete_and_filled_again_on_its_next_use(
    fills_world: DeliveryWorld,
) -> None:
    world = fills_world
    client = world.client()
    # Navidrome's own search: one through Shijhon would queue the album for a fill.
    found = world.nd.client().ok("search3", {"query": OWNED.title, "songCount": 0})
    [album] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == OWNED.title]
    client.ok("star", {"albumId": album})  # before the fill: no use of it
    fills = world.services.fills
    assert fills is not None
    call(world, lambda: fills.fill(album))
    added = fill_songs(world, album)
    assert len(added) == 15
    done = sweep(world)
    assert len(done.removed) == 1 and done.placeholders == 15
    assert fill_songs(world, album) == []
    row = match_row(world, album)
    assert row["outcome"] == "deferred" and row["cleaned_at"] is not None
    # Never filled automatically again (the pass, a view's background fill), also once its
    # match failed or became a dry run's plan ...
    assert not call(world, lambda: fills.due(album))
    for change in (
        "outcome = 'failed', attempts = 1, checked_at = 0",
        "outcome = 'filled', planned = 1",
        "outcome = 'deferred', planned = 0",
    ):

        async def changed(change: str = change) -> None:
            await world.services.store.execute(
                f"UPDATE album_matches SET {change} WHERE album_id = ?",  # noqa: S608
                [album],
            )

        call(world, changed)
        assert not call(world, lambda: fills.due(album)), change
    view = client.ok("getAlbum", {"id": album})["album"]
    # ... but shown complete: the owned songs and the release's other tracks.
    assert view["songCount"] == 17 and len(view["song"]) == 17
    call(world, fills.wait_idle)
    assert fill_songs(world, album) == []
    # An XML client opens the album: a view writes nothing, and an album whose fill was
    # taken out is not filled after it either.
    assert client.request("getAlbum", {"id": album}, fmt="xml").status_code == 200
    assert fill_songs(world, album) == []
    # A client that synced it opens an old song ID (a view) or its cover: nothing again ...
    old = world.services.old_ids
    assert old is not None and world.services.engine.removed_release(added[0]) is not None
    assert client.ok("getSong", {"id": added[0]})["song"]["albumId"] == album
    cover = client.request("getCoverArt", {"id": f"mf-{added[0]}"})
    assert cover.content == client.request("getCoverArt", {"id": f"al-{album}"}).content
    assert fill_songs(world, album) == []
    # ... its next use (the "now playing" report of an old song ID) fills it again, with
    # the same song IDs.
    client.ok("scrobble", {"id": added[0], "submission": "false"})
    assert fill_songs(world, album) == added
    row = match_row(world, album)
    assert row["outcome"] == "filled" and row["cleaned_at"] is None
    # A favorite of the album given after the fill keeps it.
    client.ok("unstar", {"albumId": album})
    client.ok("star", {"albumId": album})
    done = sweep(world)
    assert done.removed == [] and fill_songs(world, album) == added
    assert done.listed is not None
    [entry] = done.listed.due
    assert entry.fill and "its album favorited" in entry.kept


# One more song of that release, in an album of its own: a part of it.
PART = Album(
    "Neve Ashdown",
    "Northern Letters",
    (Track("Copper Station", 0, seconds=243),),
    recording_date="1962",
)


def test_a_part_of_an_album_whose_fill_was_taken_out_does_not_fill_it(
    navidrome_factory: NavidromeFactory, tmp_path: Path
) -> None:
    """An automatic fill of another album of the same release (matched again, or new in
    the library) fills nothing: the release's album is the one the cleanup emptied, and
    only its own use fills it. The other is its part, not matched at each occasion."""
    replay = Replay()
    for title in ("northern letters", "northern letters (remastered)"):
        replay.aliases[f"neve ashdown {title}"] = "tender rivers"
    replay.hidden = {"900000001", "900000042"}  # the remaster alone
    nd = navidrome_factory()
    for owned in (OWNED, PART):
        write_album(nd.music, owned)
    nd.scan(full=True)
    fill = {"enabled": True, "background_pause_seconds": 0, "auto_min_songs": 1,
            "library_pass": "off", "pass_start_seconds": 3600}  # fmt: skip
    with delivery_world(
        nd,
        tmp_path,
        catalog=replay.catalog(),
        fill=fill,
        navidrome_database=nd.data / "navidrome.db",
        cleanup={"mode": "on"},
    ) as world:
        found = nd.client().ok("search3", {"query": "Northern Letters", "songCount": 0})
        ids = {a["name"]: str(a["id"]) for a in found["searchResult3"]["album"]}
        album, part = ids[OWNED.title], ids[PART.title]
        fills = world.services.fills
        assert fills is not None
        call(world, lambda: fills.fill(album))
        assert len(fill_songs(world, album)) == 15  # (one of them plays the part's song)
        assert len(sweep(world).removed) == 1 and fill_songs(world, album) == []

        async def forgotten() -> None:  # "Match again" (or an album new in the library)
            await world.services.store.execute(
                "DELETE FROM album_matches WHERE album_id = ?", [part]
            )

        call(world, forgotten)
        assert call(world, lambda: fills.due(part))
        assert call(world, lambda: fills.fill(part, auto=True)) is None
        assert fill_songs(world, album) == []
        row = match_row(world, album)
        assert row["outcome"] == "deferred" and row["cleaned_at"] is not None
        assert not call(world, lambda: fills.due(part))  # no match at every occasion

        async def reason() -> str:
            found = await world.services.store.fetchone(
                "SELECT reason FROM album_matches WHERE album_id = ?", [part]
            )
            assert found is not None
            return str(found["reason"])

        assert call(world, reason).startswith(f"part of Neve Ashdown - {OWNED.title}")


def test_a_removed_catalog_albums_cover_comes_from_its_record(
    fills_world: DeliveryWorld,
) -> None:
    """A cover of a catalog album taken out - by its album's or a song's ID, in any
    form - is the catalog's (its record's artwork), and brings nothing back."""
    world = fills_world
    commits = world.services.commits
    assert commits is not None
    wanted = CatalogId.parse("sh.al.demo.900000133")
    assert wanted is not None
    album = call(world, lambda: commits.native(wanted, "a test's commit"))
    release = "demo:900000133"
    assert in_library(world, release)
    song = world.client().ok("getAlbum", {"id": album})["album"]["song"][0]["id"]
    assert "Jonah Fairbanks - wild stones" in sweep(world).removed
    blue = cover_image("blue").read_bytes()
    client = world.client()
    for cover in (f"al-{album}", album, f"mf-{song}", song, f"dc-{album}:1", f"al-{album}_0a1b"):
        answer = client.request("getCoverArt", {"id": cover, "size": "300"})
        assert answer.status_code == 200, cover
        assert answer.headers["content-type"] == "image/jpeg" and answer.content == blue, cover
    assert not in_library(world, release)


# --- the write gate ---------------------------------------------------------------------


def test_placeholders_are_written_only_while_navidrome_keeps_missing_files(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> None:
    nd = navidrome_factory({"ND_SCANNER_PURGEMISSING": "always"})
    with delivery_world(nd, tmp_path_factory.mktemp("purging")) as world:
        release = catalog_release("m-purging", "Purging", "Purging Artist", 2)
        with pytest.raises(MaterializeError, match=r'Scanner\.PurgeMissing is "always"'):
            world.materialize(release)
        assert world.placeholder_files() == []
    # Configuration not shown (DevUIShowConfig off): the stated value counts.
    hidden = navidrome_factory({"ND_DEVUISHOWCONFIG": "false"})
    with delivery_world(hidden, tmp_path_factory.mktemp("hidden")) as world:
        release = catalog_release("m-hidden", "Hidden", "Hidden Artist", 1)
        with pytest.raises(MaterializeError, match="DevUIShowConfig is off"):
            world.materialize(release)
        writes = world.services.engine.writes_refused
        assert writes is not None
        gate: PlaceholderWrites = writes.__self__  # type: ignore[attr-defined]
        gate.stated = "never"
        assert call(world, lambda: gate.refused(fresh=True)) is None
        assert world.materialize(release).created


@pytest.mark.anyio
async def test_a_refusal_while_navidrome_does_not_answer_is_not_kept() -> None:
    """Navidrome may start after Shijhon: "not answering" is asked again within seconds,
    a value read is kept ten minutes, and a value it shows wins over the stated one."""
    now = [0.0]

    class Starting:
        def __init__(self) -> None:
            self.answers: list[Any] = [NavidromeError("native GET: ConnectError")]

        async def native_json(self, method: str, path: str) -> Any:
            answer = self.answers.pop(0) if self.answers else {"config": {"Scanner": {}}}
            if isinstance(answer, Exception):
                raise answer
            return answer

    navidrome = Starting()
    writes = PlaceholderWrites(navidrome, stated="never", clock=lambda: now[0])  # type: ignore[arg-type]
    assert "does not answer" in (await writes.refused() or "")
    now[0] += 6
    assert await writes.refused() is None  # answered: its configuration names no value
    navidrome.answers = [{"config": {"Scanner": {"PurgeMissing": "always"}}}]
    now[0] += 60
    assert await writes.refused() is None  # the value read is kept a while ...
    assert '"always"' in (await writes.refused(fresh=True) or "")  # ... unless asked anew
    navidrome.answers = [NavidromeError("native GET: HTTP 404", status=404)]
    assert await writes.refused(fresh=True) is None  # not shown: the stated value

    class Broken:
        async def native_json(self, method: str, path: str) -> Any:
            raise KeyError("unexpected")

    broken = PlaceholderWrites(Broken(), stated="never")  # type: ignore[arg-type]
    assert "does not answer" in (await broken.refused() or "")  # never an error


def test_navidromes_database_is_read_without_writing(world: DeliveryWorld) -> None:
    """Opened read-only: the check works while Navidrome runs, and writes nothing."""
    conn = open_read_only(world.nd.data / "navidrome.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE shijhon_probe (x)")
    finally:
        conn.close()


# --- the write gate before every library write ------------------------------------


@pytest.fixture(scope="module")
def gated(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    """Navidrome hiding its configuration: the stated setting is what the gate reads, so a
    test can change it as a restart of Navidrome with another setting would."""
    nd = navidrome_factory({"ND_DEVUISHOWCONFIG": "false"})
    with delivery_world(nd, tmp_path_factory.mktemp("gated"), warm_ahead_depth=0) as world:
        gate(world).stated = "never"
        yield world


def gate(world: DeliveryWorld) -> PlaceholderWrites:
    writes = world.services.engine.writes_refused
    assert writes is not None
    found: PlaceholderWrites = writes.__self__  # type: ignore[attr-defined]
    return found


@contextmanager
def purging(world: DeliveryWorld) -> Iterator[None]:
    """Navidrome "restarted" with PurgeMissing on: every write asks it again, none waits
    for a cached answer."""
    gate(world).stated = "always"
    try:
        yield
    finally:
        gate(world).stated = "never"


def test_no_swap_retag_or_revert_while_navidrome_would_purge(
    gated: DeliveryWorld, tmp_path: Path
) -> None:
    world = gated
    engine = world.services.engine
    release = catalog_release("m-gated-swaps", "Gated", "Gated Artist", 2)
    result = world.materialize(release)
    first, second = (result.created[t.ref] for t in release.tracks)
    audio = world.audio("m-gated", "m4a")
    call(world, lambda: engine.replace_with_delivered(second, audio))
    addon = world.addon("m-gated-source")
    world.add_source(addon)
    source = world.audio("m-gated-source")
    isrc = release.tracks[0].isrc
    assert isrc is not None
    addon.add(FakeTrack(isrc=isrc, audio=source))
    fetched = world.services.download_first.fetches
    files = {p: p.read_bytes() for p in world.placeholder_files()}
    with purging(world):
        with pytest.raises(ReplaceError, match="PurgeMissing"):
            call(world, lambda: engine.replace_with_delivered(first, audio))
        with pytest.raises(ReplaceError, match="PurgeMissing"):
            call(world, lambda: engine.revert_to_placeholder(second))
        track = dataclasses.replace(release.tracks[0], title="Retitled")
        with pytest.raises(ReplaceError, match="PurgeMissing"):
            call(world, lambda: engine.retag(first, track, release))
        with pytest.raises(MaterializeError, match="PurgeMissing"):  # no 600 s cache
            world.materialize(catalog_release("m-gated-new", "New", "New Artist", 1))
        # A stream that needs converting fetches nothing: the source format from the add-on.
        streamed = world.client().request(
            "stream", {"id": first, "format": "mp3", "maxBitRate": 128}
        )
        assert streamed.status_code == 200 and streamed.content == source.read_bytes()
        assert world.services.download_first.fetches == fetched
        expiry = DeliveredAudio(
            world.services.store,
            engine,
            max_days=1,
            max_bytes=0,
            clock=lambda: time.time() + 9 * DAY,
        )
        assert call(world, expiry.sweep).expired == 0
    assert {p: p.read_bytes() for p in world.placeholder_files()} == files
    assert call(world, lambda: engine.revert_to_placeholder(second))  # allowed again
    # A delivered file that is gone: its placeholder is not recreated either while refused.
    path = call(world, lambda: engine.replace_with_delivered(first, audio))
    engine.layout.absolute(path).unlink()
    with purging(world), pytest.raises(ReplaceError, match="PurgeMissing"):
        call(world, lambda: engine.revert_to_placeholder(first, only_if_missing=True))
    assert call(world, lambda: engine.revert_to_placeholder(first, only_if_missing=True))


def during(world: DeliveryWorld, held: Any, work: Any) -> str:
    """Run ``work`` (a library write) while ``held`` (an async context manager: a lock, an
    archive being listed) keeps it waiting; Navidrome is "restarted" with PurgeMissing on
    during that wait. Returns why the write was refused ("" when it went through)."""

    async def scenario() -> str:
        refused: list[str] = []

        async def write() -> None:
            try:
                await work()
            except (MaterializeError, ReplaceError) as exc:
                refused.append(str(exc))

        async with anyio.create_task_group() as group, held:
            group.start_soon(write)
            await anyio.sleep(0.4)  # the write waits behind what is held
            assert not refused, refused
            gate(world).stated = "always"
        return refused[0] if refused else ""

    try:
        return str(call(world, scenario))
    finally:
        gate(world).stated = "never"


def test_the_gate_is_asked_again_after_each_wait_right_before_writing(
    gated: DeliveryWorld,
) -> None:
    """A write that waited - for its release's or its song's lock, for archives being
    downloaded through Navidrome - asks Navidrome again before it writes: a Navidrome
    restarted with purging on during the wait gets no placeholder written, taken out,
    added back or swapped."""
    world = gated
    engine = world.services.engine
    release = catalog_release("m-gate-waits", "Waits", "Waiting Artist", 2)
    result = world.materialize(release)
    ref = str(release.ref)
    first, second = (result.created[t.ref] for t in release.tracks)
    audio = world.audio("m-gate-waits", "m4a")
    call(world, lambda: engine.replace_with_delivered(second, audio))
    files = {p: p.read_bytes() for p in world.placeholder_files()}

    def listing() -> Any:
        return engine.listing(wait=0.05)  # an archive being listed: new placeholders wait

    # New placeholders: behind the release's lock, and behind an archive.
    new = catalog_release("m-gate-new", "New", "Waiting Artist", 1)
    for held in (engine.lock_for(str(new.ref)), listing()):
        assert "PurgeMissing" in during(world, held, lambda: engine.materialize(new))
    # A swap in, a retag, a swap out: behind the song's lock; a swap out behind an archive.
    track = dataclasses.replace(release.tracks[0], title="Retitled")
    swaps = (
        (first, lambda: engine.replace_with_delivered(first, audio)),
        (first, lambda: engine.retag(first, track, release)),
        (second, lambda: engine.revert_to_placeholder(second)),
    )
    for song, swap in swaps:
        assert "PurgeMissing" in during(world, engine.lock_for(f"song:{song}"), swap)
    assert "PurgeMissing" in during(world, listing(), lambda: engine.revert_to_placeholder(second))
    # A removal: behind the release's lock, and during its own check (which can wait for
    # a usage export).
    assert "PurgeMissing" in during(world, engine.lock_for(ref), lambda: engine.remove_release(ref))
    unused, songs, _ = made(world, "m-gate-removal")

    async def check(rows: list[Any], gone: bool) -> list[str]:
        gate(world).stated = "always"  # ... while the check looked
        return []

    try:
        with pytest.raises(MaterializeError, match="PurgeMissing"):
            call(world, lambda: engine.remove_release(unused, check=check))
    finally:
        gate(world).stated = "never"
    assert in_library(world, unused)
    row = call(world, lambda: world.services.store.fetchone(
        "SELECT removing_at FROM releases WHERE ref = ?", [unused]))  # fmt: skip
    assert row["removing_at"] is None
    # An add-back from its record: behind the release's lock, and behind an archive.
    assert call(world, lambda: engine.remove_release(unused)).removed == 2
    for held in (engine.lock_for(unused), listing()):
        assert "PurgeMissing" in during(world, held, lambda: engine.restore_release(unused))
    stored = call(world, lambda: engine.removed_record(unused))
    assert stored is not None and stored["restoring_at"] is None and not engine.writing()
    for song in songs:
        assert world.nd.native("GET", f"song/{song}").json()["missing"] is True
    # Nothing was written by any of them, and nothing is left pending or staged.
    assert {p: p.read_bytes() for p in world.placeholder_files() if p in files} == files
    assert set(world.placeholder_files()) == set(files)
    assert not in_library(world, str(new.ref)) and not engine.writing()
    staging = engine.layout.staging
    assert [p.name for p in staging.iterdir() if p.name != ".ndignore"] == []
    # A swap asks once more right before its files move (its staged file took a moment to
    # make): allowed when it began, refused then - nothing moved, nothing staged left.
    for swap, asks in (
        (lambda: engine.replace_with_delivered(first, audio), 1),
        (lambda: engine.retag(first, track, release), 1),
        (lambda: engine.revert_to_placeholder(second), 1),
    ):
        with after_asks(world, asks), pytest.raises(ReplaceError, match="PurgeMissing"):
            call(world, swap)
    assert {p: p.read_bytes() for p in world.placeholder_files() if p in files} == files
    assert [p.name for p in staging.iterdir() if p.name != ".ndignore"] == []

    # New placeholders and an add-back ask once more between staging their files and
    # moving them in: refused there, nothing moved, nothing staged or pending left.
    # (No file moved: nothing is taken out again, and Navidrome is asked for no scan - a
    # scan while it purges missing files is what the gate is there to avoid.)
    scans: list[Any] = []
    real_until = engine.scans.until

    async def until(folders: Any, *args: Any, **kwargs: Any) -> Any:
        scans.append(folders)
        return await real_until(folders, *args, **kwargs)

    engine.scans.until = until  # type: ignore[method-assign]
    real_run = anyio.to_thread.run_sync

    async def no_worker(func: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(func, "__name__", "") == "move":  # (as a wait for a worker that is cut)
            raise RuntimeError("no worker ran the move")
        return await real_run(func, *args, **kwargs)

    try:
        with after_asks(world, 2), pytest.raises(MaterializeError, match="PurgeMissing"):
            world.materialize(new)
        assert not in_library(world, str(new.ref)) and not engine.writing()
        with after_asks(world, 3), pytest.raises(MaterializeError, match="PurgeMissing"):
            call(world, lambda: engine.restore_release(unused))
        # The same when the move itself never ran (staged, allowed, and then no worker).
        anyio.to_thread.run_sync = no_worker
        try:
            with pytest.raises(MaterializeError, match="RuntimeError"):
                world.materialize(new)
        finally:
            anyio.to_thread.run_sync = real_run
        assert not in_library(world, str(new.ref)) and not engine.writing()
    finally:
        engine.scans.until = real_until  # type: ignore[method-assign]
    assert scans == []
    stored = call(world, lambda: engine.removed_record(unused))
    assert stored is not None and stored["restoring_at"] is None and not engine.writing()
    pending = call(
        world, lambda: world.services.store.fetchall("SELECT * FROM pending_placeholders")
    )
    assert pending == []
    assert set(world.placeholder_files()) == set(files)
    assert [p.name for p in staging.iterdir() if p.name != ".ndignore"] == []

    # An add-back a stop left half done (its files in the library, its mark set), asked
    # for again while Navidrome would purge: refused, and it stays as the stop left it -
    # for the repairs, once the library may be written.
    async def stop_halfway() -> None:
        stored = await engine.removed_record(unused)
        record = json.loads(stored["record"])
        planned = [
            _Planned(_row_track(r), str(r["placeholder_path"]), json.loads(r["tags"]))
            for r in record["placeholders"]
        ]
        await world.services.store.execute(
            "UPDATE removed_releases SET restoring_at = ? WHERE ref = ?", [time.time(), unused]
        )
        await engine._write(planned, str(record["release"]["folder"]), None)
        await engine.scans.scan([str(record["release"]["folder"])])
        await engine.load_pending()

    call(world, stop_halfway)
    assert engine.left()
    for asks in (2, 3):  # (before its record is marked; between staging and the move)
        with after_asks(world, asks), pytest.raises(MaterializeError, match="PurgeMissing"):
            call(world, lambda: engine.restore_release(unused))
        assert engine.left() and not any(p.running for p in engine._pending.values())
        stored = call(world, lambda: engine.removed_record(unused))
        assert stored is not None and stored["restoring_at"] is not None
        assert not any(world.nd.native("GET", f"song/{s}").json()["missing"] for s in songs)
    # Allowed again: each goes through (the add-back finishes what the stop left).
    assert call(world, lambda: engine.restore_release(unused)) is not None
    assert not engine.left() and in_library(world, unused)
    assert call(world, lambda: engine.retag(first, track, release)) is True


@contextmanager
def after_asks(world: DeliveryWorld, allowed: int) -> Iterator[None]:
    """Navidrome "restarted" with PurgeMissing on after ``allowed`` questions of the gate:
    the questions a write asks before its last one are answered "never"."""
    engine = world.services.engine
    writes = gate(world)
    real = engine.writes_refused
    assert real is not None
    asked = 0

    async def refused(**kwargs: Any) -> str | None:
        nonlocal asked
        asked += 1
        if asked > allowed:
            writes.stated = "always"
        return await real(**kwargs)

    engine.writes_refused = refused
    try:
        yield
    finally:
        engine.writes_refused = real
        writes.stated = "never"


def test_a_repair_asks_the_gate_again_once_its_silent_file_is_made(
    gated: DeliveryWorld,
) -> None:
    """A repair that has to make a silent file - delivered audio
    whose file is gone, a placeholder whose file an interrupted swap left out of place -
    asks Navidrome again once the file is made (that takes a moment), right before the row
    changes, the file moves and the scan. A Navidrome restarted with purging on meanwhile:
    nothing moves, no scan is asked for (it would purge every missing song's record), and
    what the repair goes by is kept for when the library may be written again."""
    world = gated
    engine = world.services.engine
    store = world.services.store
    release = catalog_release("m-gate-repair", "Repairs", "Repairing Artist", 2)
    result = world.materialize(release)
    first, second = (result.created[t.ref] for t in release.tracks)
    path = call(world, lambda: engine.replace_with_delivered(first, world.audio("m-gate-repair")))
    engine.layout.absolute(path).unlink()  # delivered audio whose file is gone
    staging = engine.layout.staging

    def staged() -> list[str]:
        return sorted(p.name for p in staging.iterdir() if p.name != ".ndignore")

    def row(song: str) -> Any:
        return call(
            world, lambda: store.fetchone("SELECT * FROM placeholders WHERE song_id = ?", [song])
        )

    scans: list[Any] = []
    real_until, real_silent = engine.scans.until, engine._silent_file

    async def until(folders: Any, *args: Any, **kwargs: Any) -> Any:
        scans.append(folders)
        return await real_until(folders, *args, **kwargs)

    async def silent_then_restarted(*args: Any, **kwargs: Any) -> Path:
        made = await real_silent(*args, **kwargs)
        gate(world).stated = "always"  # Navidrome restarted while the file was made
        return made

    engine.scans.until = until  # type: ignore[method-assign]
    engine._silent_file = silent_then_restarted  # type: ignore[method-assign]
    try:
        with pytest.raises(ReplaceError, match="PurgeMissing"):
            call(world, lambda: engine.revert_to_placeholder(first, only_if_missing=True))
        # Nothing changed: the row says "delivered", nothing staged, no scan.
        assert row(first)["state"] == "delivered" and staged() == [] and scans == []
        gate(world).stated = "never"

        # A placeholder whose file is out of place after an interrupted swap, with a backup
        # that is not its file (another length): written anew from its row.
        in_place = engine.layout.absolute(str(row(second)["placeholder_path"]))

        async def interrupted() -> None:
            other = await real_silent(int(row_now["duration_ms"]) + 1000, {})
            other.replace(staging / f"backup-{second}.flac")
            in_place.unlink()

        row_now = row(second)
        call(world, interrupted)
        with pytest.raises(ReplaceError, match="PurgeMissing"):
            call(world, lambda: engine.recover_swap(second))
        # Its backup stays for the next repair; nothing was put in place or scanned.
        assert staged() == [f"backup-{second}.flac"] and not in_place.exists() and scans == []
    finally:
        engine._silent_file = real_silent  # type: ignore[method-assign]
        engine.scans.until = real_until  # type: ignore[method-assign]
        gate(world).stated = "never"
    # Allowed again: both are put right.
    assert call(world, lambda: engine.recover_swap(second)) == 1
    assert in_place.exists() and staged() == []
    # A placeholder's file that is simply gone is written anew before a swap of it: asked
    # again once it is made, too (allowed before, refused then: nothing moved or staged).
    in_place.unlink()
    with after_asks(world, 1), pytest.raises(ReplaceError, match="PurgeMissing"):
        call(world, lambda: engine.replace_with_delivered(second, world.audio("m-gate-repair")))
    assert not in_place.exists() and staged() == [] and row(second)["state"] == "placeholder"
    current = row(second)
    call(world, lambda: engine._reconcile_placeholder(current, json.loads(current["tags"])))
    assert in_place.exists() and staged() == []
    assert call(world, lambda: engine.revert_to_placeholder(first, only_if_missing=True))
    assert row(first)["state"] == "placeholder" and staged() == []
    for song in (first, second):
        assert world.nd.native("GET", f"song/{song}").json()["missing"] is False

    # The row of a missing delivered file already says "placeholder" (a stop after that):
    # its repair is gated like any - refused, its backup stays and nothing is scanned.
    path = call(world, lambda: engine.replace_with_delivered(first, world.audio("m-gate-repair")))
    engine.layout.absolute(path).unlink()
    scans.clear()
    engine.scans.until = until  # type: ignore[method-assign]
    try:
        # (Allowed: before its silence, after it, and then no more.)
        with after_asks(world, 2), pytest.raises(ReplaceError, match="PurgeMissing"):
            call(world, lambda: engine.revert_to_placeholder(first, only_if_missing=True))
    finally:
        engine.scans.until = real_until  # type: ignore[method-assign]
    assert row(first)["state"] == "placeholder" and scans == []
    assert staged() == [f"backup-{first}.flac"]
    assert call(world, lambda: engine.recover_swap(first)) == 1 and staged() == []


def test_the_startup_repairs_wait_while_navidrome_would_purge(gated: DeliveryWorld) -> None:
    world = gated
    engine = world.services.engine
    services = world.services
    release, _, _ = made(world, "m-gated-repair")

    async def stop_halfway() -> list[Path]:
        await services.store.execute(
            "UPDATE releases SET removing_at = ? WHERE ref = ?", [time.time(), release]
        )
        rows = await services.store.fetchall(
            "SELECT path FROM placeholders WHERE release_ref = ?", [release]
        )
        return [engine.layout.absolute(str(r["path"])) for r in rows]

    files = call(world, stop_halfway)
    for path in files:
        path.unlink()
    checks = StartupChecks(
        services.navidrome, engine, services.store, gate(world), retry_seconds=(0.05, 0.05)
    )
    # Allowed when the repair asks, purging on once its files are staged (that takes a
    # moment for an album): nothing moves, nothing stays staged, no scan - it stays marked.
    scans: list[Any] = []
    real_until = engine.scans.until

    async def until(folders: Any, *args: Any, **kwargs: Any) -> Any:
        scans.append(folders)
        return await real_until(folders, *args, **kwargs)

    engine.scans.until = until  # type: ignore[method-assign]
    try:
        with after_asks(world, 2):  # (asked at its start, and under the release's locks)
            assert call(world, engine.repair_removals) == 0
    finally:
        engine.scans.until = real_until  # type: ignore[method-assign]
    assert not any(path.exists() for path in files) and scans == []
    assert [p.name for p in engine.layout.staging.iterdir() if p.name != ".ndignore"] == []
    assert call(world, engine.marked)
    with purging(world):
        assert call(world, engine.repair_removals) == 0  # the cleanup's own call too
        assert not any(path.exists() for path in files)
        assert world.app.loop is not None
        running = asyncio.run_coroutine_threadsafe(checks.run(), world.app.loop)
        time.sleep(0.5)
        assert not running.done() and not any(path.exists() for path in files)  # waiting
    running.result(60)  # allowed again: the repairs ran
    assert all(path.exists() for path in files) and in_library(world, release)
