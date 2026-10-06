"""Suite I (continued) - undoing past automatic fills:
the fills the fill policy would not make now, unless their placeholders are in use
(favorites, ratings, plays, playlists, play queues, bookmarks); a dry run lists them first.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.app import ShijhonApp
from shijhon.cli import main
from shijhon.fill import undo
from shijhon.fill.fills import FillPolicy
from shijhon.navidrome.usage import UsageSource
from shijhon.store.writer import AlreadyRunning
from tests.acceptance.test_I_policy import (
    FEW,
    IRON,
    RIVERS,
    SALT,
    STONES,
    replay_for,
    world_with,
)
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld
from tests.harness.engine import PLACEHOLDER_FOLDER
from tests.harness.exporter import exporting
from tests.harness.library import Album, Track
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay
from tests.harness.running import RunningServer
from tests.harness.subsonic import SubsonicClient

STARRED = Album(
    "Esme Ashdown", "CRIMSON.", (Track("NORTHERN.", 1, seconds=118),), recording_date="2010"
)


@pytest.fixture(scope="module")
def undo_world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, Replay]]:
    replay = replay_for()
    albums = [FEW, STARRED, SALT, IRON, STONES, RIVERS]
    with world_with(navidrome_factory, tmp_path_factory.mktemp("undo"), albums, replay) as w:
        yield w, replay


def album_id(world: DeliveryWorld, title: str) -> str:
    found = world.nd.client().ok("search3", {"query": title, "artistCount": 0, "songCount": 0})
    [ident] = [a["id"] for a in found["searchResult3"]["album"] if a["name"] == title]
    return str(ident)


def songs(world: DeliveryWorld, ident: str) -> list[dict[str, Any]]:
    return list(world.nd.client().ok("getAlbum", {"id": ident})["album"]["song"])


def plan(world: DeliveryWorld, source: UsageSource | None = None) -> list[undo.Undo]:
    return undo.plan(
        world.app.settings.database_path,
        source or world.nd.data / "navidrome.db",
        FillPolicy(),
    )


def apply(
    world: DeliveryWorld, items: Any, database: Path | UsageSource | None = None
) -> tuple[int, list[str]]:
    """The undo, by the service's own engine (one writer): what was undone, and what was
    not and why (the reason nothing more was tried last)."""
    return outcome(
        world.server.call(
            lambda: undo.apply(
                list(items),
                world.services.engine,
                world.services.store,
                database or world.nd.data / "navidrome.db",
            )
        )
    )


def outcome(result: undo.Applied) -> tuple[int, list[str]]:
    stopped = [f"nothing more is undone: {result.stopped}"] if result.stopped else []
    return result.done, result.failed + stopped


def filled(world: DeliveryWorld, album: Album) -> tuple[str, list[str]]:
    """An album below the policy, filled on a first use: (its ID, its placeholders)."""
    fills = world.services.fills
    assert fills is not None
    ident = album_id(world, album.title)
    world.server.call(lambda: fills.fill_album(ident, "a first use"))
    owned = {t.title for t in album.tracks}
    added = [str(s["id"]) for s in songs(world, ident) if s["title"] not in owned]
    assert added, "not filled"
    return ident, added


def test_past_fills_are_listed_then_undone(
    undo_world: tuple[DeliveryWorld, Replay],
) -> None:
    world, _ = undo_world
    fills = world.services.fills
    assert fills is not None
    few, starred = (album_id(world, a.title) for a in (FEW, STARRED))
    for ident in (few, starred):  # filled on first use, whatever the policy

        async def use(ident: str = ident) -> None:
            await fills.fill_album(ident, "a first use")

        world.server.call(use)
    assert len(songs(world, few)) == 19 and len(songs(world, starred)) == 14
    placeholder = next(s["id"] for s in songs(world, starred) if s["title"] != "NORTHERN.")
    world.client().ok("star", {"id": placeholder})  # in use: kept

    listed = {u.album_id: u for u in plan(world)}  # the dry run
    assert set(listed) == {few, starred}
    assert listed[few].reason == "below the fill policy (1 of 19 owned)" and not listed[few].kept
    assert listed[starred].kept == ["favorited"]
    text = undo.report(listed.values())
    assert text.startswith("Fills to undo: 1 album(s), 18 placeholder(s); kept: 1")
    assert len(songs(world, few)) == 19  # the dry run changed nothing

    done = apply(world, listed.values())
    assert done == (1, [])
    assert len(songs(world, few)) == 1
    assert len(songs(world, starred)) == 14  # in use: kept
    assert plan(world) == [listed[starred]]
    # Viewed again: complete at once; the album below the policy stays as it is until its
    # first use.
    view = world.client(client="again-1").ok("getAlbum", {"id": few})["album"]
    assert view["songCount"] == 19 and len(songs(world, few)) == 1


def test_an_undo_keeps_what_is_used_by_the_time_it_applies(
    undo_world: tuple[DeliveryWorld, Replay],
) -> None:
    """The undo keeps to the cleanup's rule, and looks for use again when it applies -
    under the release's locks, and once the files are gone: a favorite given between the
    list and the apply keeps the fill, and so does a stream Shijhon served since."""
    world, _ = undo_world
    album, added = filled(world, SALT)
    count = len(songs(world, album))
    [item] = [u for u in plan(world) if u.album_id == album]
    assert item.kept == [] and len(item.placeholders) == len(added)
    # As Navidrome's own web player would: not through Shijhon.
    world.nd.client().ok("star", {"id": added[0]})
    assert apply(world, [item]) == (0, [
        f"{item.artist} - {item.title} ({album}): kept - favorited"
    ])  # fmt: skip
    assert len(songs(world, album)) == count
    world.nd.client().ok("unstar", {"id": added[0]})  # taken back: still a use
    [item] = [u for u in plan(world) if u.album_id == album]
    assert item.kept == ["favorited, rated or played once"]
    assert world.server.call(lambda: world.services.engine.seen_uses(item.release_ref)) == [
        "favorited"
    ]  # (seen as it applied: recorded)


def test_a_stream_since_the_list_keeps_the_fill(
    undo_world: tuple[DeliveryWorld, Replay],
) -> None:
    world, _ = undo_world
    album, added = filled(world, STONES)
    count = len(songs(world, album))
    [item] = [u for u in plan(world) if u.album_id == album]
    assert item.kept == []

    async def streamed() -> None:  # as a stream Shijhon served records it
        await world.services.store.execute(
            "UPDATE placeholders SET last_used_at = ? WHERE song_id = ?", [time.time(), added[1]]
        )

    world.server.call(streamed)
    assert apply(world, [item]) == (0, [
        f"{item.artist} - {item.title} ({album}): kept - streamed or downloaded"
    ])  # fmt: skip
    assert len(songs(world, album)) == count
    [item] = [u for u in plan(world) if u.album_id == album]
    assert item.kept == ["streamed or downloaded"]


def test_an_undone_fill_comes_back_by_a_use_of_an_old_id_and_a_copy_undoes_nothing(
    undo_world: tuple[DeliveryWorld, Replay],
) -> None:
    """An undone fill is kept as a record, as one the cleanup took out: a use of an old ID
    fills the album again with the same song IDs. And a copy of Navidrome's database
    - which does not show the songs as missing once their files are gone - undoes nothing:
    the fill is put back, and the undo ends."""
    world, _ = undo_world
    album, added = filled(world, IRON)
    count = len(songs(world, album))
    copy = world.tmp / "copied-navidrome.db"
    source = sqlite3.connect(f"file:{world.nd.data / 'navidrome.db'}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    source.backup(target)
    source.close()
    target.close()
    items = [u for u in plan(world) if u.album_id == album]
    done, failed = apply(world, items, copy)
    assert done == 0 and len(failed) == 2
    assert failed[0].endswith("kept - Navidrome's records do not show its songs as gone")
    assert failed[1].startswith("nothing more is undone")
    assert sorted(s["id"] for s in songs(world, album) if s["id"] in added) == sorted(added)
    assert len(songs(world, album)) == count
    # By the current database: undone, and kept as a record.
    assert apply(world, items) == (1, [])
    assert len(songs(world, album)) == 1
    engine = world.services.engine
    assert all(engine.removed_song(song) == items[0].release_ref for song in added)
    world.client().ok("star", {"id": added[0]})  # a use of an old ID
    again = songs(world, album)
    assert len(again) == count and {s["id"] for s in again} >= set(added)
    assert world.client().ok("getSong", {"id": added[0]})["song"].get("starred")


def test_apply_is_refused_beside_the_running_service(
    undo_world: tuple[DeliveryWorld, Replay], capsys: pytest.CaptureFixture[str]
) -> None:
    """One writer at a time: the running service holds a lock beside its
    database; ``fills-undo --apply`` refuses while it is held (a dry run only reads), and
    a second service on the same state does not start."""
    world, _ = undo_world
    state = world.app.configured.state_dir
    config = world.tmp / "undo.toml"
    config.write_text(f'state_dir = "{state}"\n[navidrome]\nlibrary_path = "{world.nd.music}"\n')
    argv = ["--config", str(config), "fills-undo", "--navidrome-db",
            str(world.nd.data / "navidrome.db")]  # fmt: skip
    main(argv)
    assert "Fills to undo:" in capsys.readouterr().out
    files = world.placeholder_files()
    with pytest.raises(SystemExit) as refused:
        main([*argv, "--apply"])
    assert refused.value.code == 1
    assert "is running on this database: stop it first" in capsys.readouterr().err
    assert world.placeholder_files() == files
    second = ShijhonApp(world.app.configured)
    with pytest.raises(AlreadyRunning, match="another Shijhon process is using"):
        anyio.run(second.startup)
    anyio.run(second.shutdown)


def test_an_undo_by_the_usage_export_waits_for_one_made_after_each_removal(
    undo_world: tuple[DeliveryWorld, Replay],
) -> None:
    """Read from the usage export, the undo keeps to the cleanup's rule too - a use
    made after its list is found in the export made after the files went (asked for,
    waited for), and puts the fill back; and without a newer export nothing is undone."""
    world, _ = undo_world
    album, added = filled(world, RIVERS)
    count = len(songs(world, album))
    out = world.tmp / "usage" / "usage.sqlite3"
    with exporting(world.nd.data / "navidrome.db", out):
        source = UsageSource(out, export=True)
        items = [u for u in plan(world, source) if u.album_id == album]
        assert [u.kept for u in items] == [[]]
        world.nd.client().ok("star", {"id": added[0]})  # since the list, not through Shijhon
        done, failed = apply(world, items, source)
        assert done == 0 and failed[0].endswith("kept - favorited") and len(failed) == 1
        assert len(songs(world, album)) == count
        world.nd.client().ok("unstar", {"id": added[0]})
        # (the list reads the export there: one made after this)
        world.server.call(lambda: source.asked(wait=30))
        assert [u.kept for u in plan(world, source) if u.album_id == album] == [
            ["favorited, rated or played once"]
        ]
    # The exporter gone: no export made after the command began arrives, and it says so.
    engine, store = world.services.engine, world.services.store
    done, failed = outcome(
        world.server.call(lambda: undo.apply(items, engine, store, source, wait=1.0))
    )
    assert done == 0 and len(songs(world, album)) == count
    assert failed[0].endswith("kept - used before (favorited)")  # (the engine's own record)

    async def forget() -> None:  # as if nobody had used it
        await store.execute("DELETE FROM seen_uses WHERE release_ref = ?", [items[0].release_ref])

    world.server.call(forget)
    done, failed = outcome(
        world.server.call(lambda: undo.apply(items, engine, store, source, wait=1.0))
    )
    assert done == 0 and failed[0].endswith("kept - no usage export newer than its removal")
    assert failed[1].startswith("nothing more is undone: no newer usage export arrived")
    assert len(songs(world, album)) == count


def test_the_command_undoes_with_the_service_stopped_and_the_next_start_knows_it(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command's own path: with Shijhon stopped it takes the lock, builds an engine of
    its own, undoes the fill by Navidrome's records and keeps it as a record; the service
    started afterwards knows the old IDs, and a use of one fills the album again."""
    replay = replay_for()
    tmp = tmp_path_factory.mktemp("undo-command")
    with world_with(navidrome_factory, tmp, [SALT], replay) as world:
        nd, configured = world.nd, world.app.configured
        album, added = filled(world, SALT)
        count = len(songs(world, album))
        ref = world.server.call(lambda: world.services.store.fetchone("SELECT ref FROM releases"))[
            "ref"
        ]
    # Stopped: its lock is free.
    config = tmp / "undo.toml"
    config.write_text(
        f'state_dir = "{configured.state_dir}"\n'
        f'[navidrome]\nurl = "{nd.base_url}"\nuser = "{ADMIN_USER}"\n'
        f'password = "{ADMIN_PASSWORD}"\nlibrary_path = "{nd.music}"\n'
        f'[placeholders]\nfolder = "{PLACEHOLDER_FOLDER}"\n'
    )
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:  # (the command sets up its own logging: put back for the tests after this one)
        main(["--config", str(config), "fills-undo", "--navidrome-db",
              str(nd.data / "navidrome.db"), "--apply"])  # fmt: skip
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    out = capsys.readouterr().out
    assert "Fills to undo: 1 album(s)" in out and "undone: 1 album(s)" in out
    listed = nd.client().ok("getAlbum", {"id": album})["album"]["song"]
    assert len(listed) == 1 and listed[0]["id"] not in added
    again = sqlite3.connect(configured.database_path)
    try:
        assert again.execute("SELECT ref FROM removed_releases").fetchall() == [(ref,)]
        assert again.execute("SELECT COUNT(*) FROM album_matches").fetchone()[0] == 0
        assert again.execute("SELECT COUNT(*) FROM pending_placeholders").fetchone()[0] == 0
    finally:
        again.close()
    # Started again on that state: the old IDs are known, and a use of one fills it again.
    app = ShijhonApp(configured, catalog=replay.catalog())
    server = RunningServer(app)
    server.start()
    try:
        assert app.services is not None
        assert all(app.services.engine.removed_song(song) == ref for song in added)
        client = SubsonicClient(server.base_url, ADMIN_USER, ADMIN_PASSWORD)
        client.ok("star", {"id": added[0]})
        filled_again = nd.client().ok("getAlbum", {"id": album})["album"]["song"]
        assert len(filled_again) == count and {s["id"] for s in filled_again} >= set(added)
    finally:
        server.stop()
