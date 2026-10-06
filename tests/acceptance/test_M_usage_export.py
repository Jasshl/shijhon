"""Suite M (continued) - the cleanup by the usage export, against a real Navidrome
whose database only the exporter reads: ``shijhon usage-export`` as a process of its own
(as the deployment's "usage-export" service), Shijhon reading its file.

- The export holds only the tables and columns the checks read: none of Navidrome's
  secrets, users, user IDs, share keys or playlist names are in the file.
- The cleanup acts only on an export newer than the moment it asks about: its listing on
  one made after the check began, a removal on one made after the files went (asked for,
  waited for) - a use made in between is found there and puts the release back. Without a
  newer export nothing is taken out, and it says so.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from shijhon.cleanup import NO_EXPORT, Cleanup, Swept
from shijhon.cli import main
from shijhon.navidrome.export import COPIED, META, lock_path, request_path
from shijhon.navidrome.usage import NotFresh, UsageSource, open_read_only
from tests.acceptance.test_M_cleanup import LISTENER, aged, in_library, listing_only, sweep
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.exporter import Exporter, exporting
from tests.harness.subsonic import SubsonicClient


@dataclass
class Exported:
    world: DeliveryWorld
    exporter: Exporter

    @property
    def cleanup(self) -> Cleanup:
        found = self.world.services.cleanup
        assert found is not None
        return found


@pytest.fixture(scope="module")
def exported(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Exported]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    nd.create_user(*LISTENER)
    tmp = tmp_path_factory.mktemp("usage-export")
    out = tmp / "usage" / "usage.sqlite3"
    with (
        exporting(nd.data / "navidrome.db", out) as exporter,
        delivery_world(
            nd, tmp, warm_ahead_depth=0, usage_export=out, cleanup={"mode": "on"}
        ) as world,
    ):
        yield Exported(world, exporter)


def made(world: DeliveryWorld, key: str) -> tuple[str, list[str], str]:
    release = catalog_release(key, f"Album {key}", f"Artist {key}", 2)
    result = world.materialize(release)
    return str(release.ref), [result.created[t.ref] for t in release.tracks], result.album_id


@pytest.mark.canary  # (the tables and columns exported are Navidrome's own)
def test_the_export_of_a_real_navidrome_holds_no_users_secrets_or_share_keys(
    exported: Exported,
) -> None:
    world, cleanup = exported.world, exported.cleanup
    user = SubsonicClient(world.server.base_url, *LISTENER)
    _, songs, album = made(world, "x-private")
    user.ok("star", {"id": songs[0]})
    user.ok("scrobble", {"id": songs[1], "submission": "true"})
    playlist = user.ok("createPlaylist", {"name": "x-private-list", "songId": songs[0]})
    share = user.ok("createShare", {"id": album, "description": "x-private-share"})
    user.ok("createBookmark", {"id": songs[1], "position": 9, "comment": "x-private-note"})
    user.ok("savePlayQueue", {"id": songs[0], "current": songs[0]})
    assert cleanup.usage is not None and cleanup.usage.export
    out = cleanup.usage.path
    world.server.call(lambda: cleanup.usage.asked(wait=30))  # type: ignore[union-attr]
    # What Navidrome's own database holds beside it, read here only to look for it.
    navidrome = open_read_only(world.nd.data / "navidrome.db")
    try:
        secrets = [
            str(r[0])
            for r in navidrome.execute("SELECT value FROM property WHERE id LIKE '%Secret%'")
        ]
        users = [str(v) for r in navidrome.execute("SELECT id, password FROM user") for v in r]
        shares = [str(r[0]) for r in navidrome.execute("SELECT id FROM share")]
        before = navidrome.execute("PRAGMA data_version").fetchone()[0]
    finally:
        navidrome.close()
    assert len(secrets) >= 1 and len(users) == 4  # (its signing secrets; two users)
    assert shares == [share["shares"]["share"][0]["id"]]
    private = [*secrets, *users, *shares, LISTENER[0]]
    private += ["x-private-list", "x-private-share", "x-private-note"]
    assert all(len(value) >= 8 for value in private)
    content = out.read_bytes()
    for value in private:
        assert value.encode() not in content, "the export holds something private"
    conn = sqlite3.connect(out)
    try:
        tables = {
            str(r[0]): [str(c[1]) for c in conn.execute(f"PRAGMA table_info({r[0]})")]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert tables.pop(META) == ["format", "snapshot_at", "answers", "source"]
        # (the pinned Navidrome has every table, the optional one too)
        assert tables == {table: list(columns) for table, (columns, _) in COPIED.items()}
        assert conn.execute("SELECT COUNT(*) FROM media_file").fetchone()[0] >= 2
        assert conn.execute("SELECT COUNT(*) FROM share").fetchone()[0] == 1
        held = playlist["playlist"]["id"]
        assert conn.execute("SELECT playlist_id FROM playlist_tracks").fetchall() == [(held,)]
    finally:
        conn.close()
    assert sorted(p.name for p in out.parent.iterdir()) == [
        out.name,
        lock_path(out).name,
        request_path(out).name,
    ]
    # Navidrome's database is only read.
    navidrome = open_read_only(world.nd.data / "navidrome.db")
    try:
        assert navidrome.execute("PRAGMA data_version").fetchone()[0] == before
    finally:
        navidrome.close()
    # The two are not taken for each other.
    with pytest.raises(sqlite3.DatabaseError, match="is a usage export"):
        UsageSource(out, export=False).open()
    with pytest.raises(sqlite3.DatabaseError, match="is not a usage export"):
        UsageSource(world.nd.data / "navidrome.db", export=True).open()


def test_the_cleanup_takes_out_by_the_export_and_keeps_what_anyone_uses(
    exported: Exported, capsys: pytest.CaptureFixture[str]
) -> None:
    world = exported.world
    user = SubsonicClient(world.server.base_url, *LISTENER)
    unused, unused_songs, _ = made(world, "x-unused")
    starred, songs, _ = made(world, "x-starred")
    user.ok("star", {"id": songs[1]})
    listed, songs, _ = made(world, "x-listed")
    playlist = user.ok("createPlaylist", {"name": "x-list", "songId": songs[0]})["playlist"]
    aged(world, unused, starred, listed)
    began = time.time()
    with listing_only(world, unused, starred, listed):
        done: Swept = sweep(world, days=0)
    assert done.refused == "" and done.removed == ["Artist x-unused - Album x-unused"]
    assert done.listed is not None and done.listed.snapshot_at is not None
    assert done.listed.snapshot_at > began  # an export made after the check began
    kept = {c.ref: c.kept for c in done.listed.due if c.kept}
    assert kept == {starred: ["favorited"], listed: ["in a playlist"]}
    assert not in_library(world, unused) and in_library(world, starred)
    for song in unused_songs:
        assert world.nd.native("GET", f"song/{song}").json()["missing"] is True
    # Seen in the export, recorded: the playlist entry keeps its release once it is gone.
    user.ok("deletePlaylist", {"id": playlist["id"]})
    with listing_only(world, listed):
        done = sweep(world, days=0)
    assert done.removed == [] and in_library(world, listed)
    assert [c.kept for c in done.listed.due] == [["in a playlist once"]]  # type: ignore[union-attr]
    # The command lists by the export there now, and says of when it is.
    assert exported.cleanup.usage is not None
    state = world.tmp / "state"
    main(["cleanup", "--database", str(state / "shijhon.sqlite3"),
          "--usage-export", str(exported.cleanup.usage.path)])  # fmt: skip
    out = capsys.readouterr().out
    assert "kept (in use): 2" in out and "(who uses what: the usage export of 20" in out


def test_a_use_made_after_the_listing_is_found_in_the_export_made_after_the_removal(
    exported: Exported,
) -> None:
    """The listing's export does not have it; the removal looks again, once the files are
    gone, at an export made after that - asked for, waited for - and puts the release back."""
    world = exported.world
    release, songs, _ = made(world, "x-late")
    aged(world, release)
    files = world.placeholder_files()

    def use() -> None:  # as Navidrome's own web player would: not through Shijhon
        world.nd.client().ok("setRating", {"id": songs[0], "rating": 5})

    with listing_only(world, release, then=use):
        done = sweep(world, days=0)
    assert done.removed == [] and done.outcomes == {release: "kept: rated"}
    assert in_library(world, release) and world.placeholder_files() == files
    assert not any(world.nd.native("GET", f"song/{s}").json()["missing"] for s in songs)
    assert world.server.call(lambda: world.services.engine.seen_uses(release)) == ["rated"]


def test_without_a_newer_export_nothing_is_taken_out(exported: Exported) -> None:
    """The cleanup acts only on an export made after the moment it asks about. The
    exporter stopped: the check waits, then says so and lists nothing; stopped between the
    listing and a removal: the release is put back, and the check ends."""
    world, cleanup, exporter = exported.world, exported.cleanup, exported.exporter
    release, songs, _ = made(world, "x-stale")
    other, _, _ = made(world, "x-stale-other")
    aged(world, release, other)
    files = world.placeholder_files()
    waits = (cleanup.listing_wait, cleanup.removal_wait)
    cleanup.listing_wait, cleanup.removal_wait = 1.5, 1.5
    try:
        with listing_only(world, release, other, then=exporter.stop):
            done = sweep(world, days=0)
        assert done.removed == [] and done.outcomes == {} and done.kept == 0
        assert "no usage export made after" in done.refused
        assert "is the exporter running?" in done.refused
        assert done.refused.endswith("Artist x-stale - Album x-stale was put back")
        assert in_library(world, release) and in_library(world, other)
        assert world.placeholder_files() == files
        assert not any(world.nd.native("GET", f"song/{s}").json()["missing"] for s in songs)
        with pytest.raises(NotFresh, match="is the exporter running"):
            sweep(world, days=0)  # (the daily loop logs it, and tries again within the hour)
        with pytest.raises(NotFresh):
            sweep(world, days=0, mode="dry_run")
        assert world.placeholder_files() == files
    finally:
        cleanup.listing_wait, cleanup.removal_wait = waits
        exporter.start()
    with listing_only(world, release, other):
        done = sweep(world, days=0)
    assert sorted(done.removed) == [
        "Artist x-stale - Album x-stale",
        "Artist x-stale-other - Album x-stale-other",
    ]


def test_a_request_that_cannot_be_written_takes_nothing_out(exported: Exported) -> None:
    """Shijhon cannot write its request for an export (the export's
    folder is not writable for it). No export then says it was made after the check began -
    its time does not count -, so the check ends at once, takes nothing out and says why."""
    world, cleanup = exported.world, exported.cleanup
    release, _, _ = made(world, "x-unasked")
    aged(world, release)
    files = world.placeholder_files()
    assert cleanup.usage is not None
    folder = cleanup.usage.path.parent
    folder.chmod(0o555)
    try:
        for mode in ("on", "dry_run"):
            began = time.monotonic()
            with pytest.raises(NotFresh, match="could not be written"):
                sweep(world, days=0, mode=mode)
            assert time.monotonic() - began < 5  # (not waited for: nothing was asked)
    finally:
        folder.chmod(0o755)
    assert in_library(world, release) and world.placeholder_files() == files
    with listing_only(world, release):  # asked again, answered: it goes
        assert sweep(world, days=0).removed == ["Artist x-unasked - Album x-unasked"]


def test_no_export_is_no_use(exported: Exported) -> None:
    """(The reason a removal gives for waiting in vain is never recorded as a use.)"""
    assert NO_EXPORT == "no usage export newer than its removal"
    world = exported.world
    rows: list[Any] = world.server.call(
        lambda: world.services.store.fetchall("SELECT reason FROM seen_uses")
    )
    assert all("export" not in str(r["reason"]) for r in rows)
