"""Suite R - library writes survive stops, against a real Navidrome.

A write stopped between any two of its steps - killed (SIGKILL, in a process of its own: a
redeploy's kill, an OOM kill; no handler runs), canceled or failed (its compensations
run) - and then the next start leave the library and Shijhon's records in step:

- every audio file in the release's folder is one a row records, of its kind (the silent
  placeholder, or delivered audio), and every row's file is there; no backup is left;
- Navidrome lists exactly the recorded songs there, with their IDs;
- nothing is pending, and the release can be written again (no "expected N songs, found
  2N"; a filled album does not show tracks twice).

Meanwhile a new placeholder's song is never forwarded to Navidrome as an owned song
(Navidrome would serve its silence with status 200): a request waits for its row, and one
left by a stop is refused until the next start takes it out again. The startup repairs
hold the songs' locks, so a download meanwhile is never overwritten with silence.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from shijhon.catalog.model import CatalogRelease
from shijhon.delivery import intercept
from shijhon.navidrome.checks import TESTED_VERSION, PlaceholderWrites, StartupChecks
from shijhon.navidrome.client import NavidromeError
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.placeholders import durable
from shijhon.placeholders import engine as engine_module
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import MaterializeError, PlaceholderEngine, ReplaceError
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.store import Store
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import EngineParts, catalog_release, engine_for
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import Album, Track, frequency_for, tone, write_album
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
AUDIO = {".flac", ".m4a", ".mp4", ".mp3", ".ogg", ".opus"}
MATERIALIZE_STEPS = ["intended", "staged", "in place", "verified", "recorded"]


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("r-state")


def release_of(key: str, count: int = 2) -> CatalogRelease:
    return catalog_release(key, f"Album {key}", f"Artist {key}", count)


def folder_of(parts: EngineParts, release: CatalogRelease) -> str:
    return parts.layout.release_folder(release.ref, release.artist, release.title)


async def kill_at(nd: NavidromeInstance, state: Path, step: str, **action: Any) -> None:
    """The write in a process of its own, killed at ``step``."""
    spec = {
        "url": nd.base_url,
        "music": str(nd.music),
        "database": str(state / "shijhon.sqlite3"),
        "kill_at": step,
        **action,
    }
    with anyio.fail_after(180):
        done = await anyio.run_process(
            [sys.executable, "-m", "tests.harness.killed", json.dumps(spec)], cwd=ROOT, check=False
        )
    assert done.returncode == -signal.SIGKILL, done.stderr.decode()[-3000:]


@asynccontextmanager
async def restarted(nd: NavidromeInstance, state: Path) -> AsyncIterator[EngineParts]:
    """The next start: the engine on the same database and library, its startup repairs."""
    async with engine_for(nd, state) as parts:
        await parts.engine.load_pending()
        writes = PlaceholderWrites(parts.navidrome)
        await StartupChecks(parts.navidrome, parts.engine, parts.store, writes).run()
        yield parts


async def assert_in_step(parts: EngineParts, folder: str) -> dict[str, Any]:
    """The release's folder, its rows and Navidrome agree; returns the rows by path."""
    store, layout = parts.store, parts.layout
    assert await store.fetchall("SELECT * FROM pending_placeholders") == []
    assert not parts.engine.writing()
    rows = await store.fetchall(
        "SELECT * FROM placeholders WHERE path LIKE ?", [folder.rstrip("/") + "/%"]
    )
    recorded = {str(r["path"]): r for r in rows}
    target = layout.absolute(folder)
    on_disk = (
        {layout.relative(p) for p in target.iterdir() if p.suffix.lower() in AUDIO}
        if target.exists()
        else set()
    )
    assert on_disk == set(recorded)
    for path, row in recorded.items():
        marker = tagging.marker_of(layout.absolute(path))
        assert marker is not None and marker.silence == (row["state"] == "placeholder"), path
        if row["state"] == "placeholder":  # the silent file with the row's own tags
            assert tagging.read_vorbis(layout.absolute(path)) == json.loads(row["tags"]), path
    songs = [s for s in await parts.navidrome.songs_under(folder) if not s.get("missing")]
    listed = {str(s["path"]): str(s["id"]) for s in songs}
    assert listed == {path: str(row["song_id"]) for path, row in recorded.items()}
    for song in songs:  # Navidrome read the files that are there now
        assert song["size"] == layout.absolute(str(song["path"])).stat().st_size, song["path"]
    assert not list(layout.staging.glob("backup-*"))
    return recorded


async def written_again(parts: EngineParts, release: CatalogRelease) -> None:
    """The release's next commit works and gives it every track once."""
    result = await parts.engine.materialize(release)
    album = await parts.navidrome.album(result.album_id)
    assert album["songCount"] == len(release.tracks)
    recorded = await assert_in_step(parts, folder_of(parts, release))
    assert len(recorded) == len(release.tracks)


# --- materialization: killed, canceled, failed ------------------------------------------


@pytest.mark.parametrize("step", MATERIALIZE_STEPS)
async def test_a_commit_killed_at_any_step_is_undone_or_kept_at_the_next_start(
    navidrome: NavidromeInstance, state: Path, step: str
) -> None:
    key = "r-kill-" + step.replace(" ", "-")
    await kill_at(navidrome, state, step, action="materialize", key=key, count=2, cover=True)
    async with restarted(navidrome, state) as parts:
        release = release_of(key)
        folder = folder_of(parts, release)
        recorded = await assert_in_step(parts, folder)
        assert len(recorded) == (2 if step == "recorded" else 0)
        cover = parts.layout.absolute(folder) / "cover.jpg"
        assert cover.exists() == (step == "recorded")
        await written_again(parts, release)


@pytest.mark.parametrize("step", MATERIALIZE_STEPS)
@pytest.mark.parametrize("how", ["canceled", "failed", "cut"])
async def test_a_commit_canceled_or_failed_at_any_step_leaves_nothing_unrecorded(
    navidrome: NavidromeInstance, state: Path, step: str, how: str
) -> None:
    """``cut``: the request's task itself is canceled, as the server ends a request that
    is still under way when the seconds a restart gives it are over."""
    key = f"r-{how}-" + step.replace(" ", "-")
    release = release_of(key)
    request = asyncio.current_task()
    assert request is not None
    async with engine_for(navidrome, state) as parts:
        with anyio.CancelScope() as scope:

            async def stop(name: str) -> None:
                if name != step:
                    return
                if how == "failed":
                    raise RuntimeError("a failure")
                if how == "cut":
                    request.cancel()
                else:
                    scope.cancel()
                await anyio.lowlevel.checkpoint()

            parts.engine.step = stop
            try:
                await parts.engine.materialize(release)
            except (MaterializeError, RuntimeError):
                assert how == "failed"
            except asyncio.CancelledError:
                if how != "cut":
                    raise
                request.uncancel()  # (the test itself goes on)
            finally:
                parts.engine.step = None
        assert scope.cancelled_caught == (how == "canceled")
        recorded = await assert_in_step(parts, folder_of(parts, release))
        assert len(recorded) == (2 if step == "recorded" else 0)
        await written_again(parts, release)


async def test_placeholders_a_stop_left_go_before_their_release_is_written_again(
    navidrome: NavidromeInstance, state: Path
) -> None:
    """Without waiting for the start's repair: a commit of the release, or its removal,
    takes the left files out first (under the release's lock)."""
    await kill_at(navidrome, state, "verified", action="materialize", key="r-again", count=2)
    async with engine_for(navidrome, state) as parts:
        assert await parts.engine.load_pending() == 1 and parts.engine.left()
        await written_again(parts, release_of("r-again"))
    # A release with one of its two tracks, the other left half written: taken out whole.
    release = release_of("r-again-out")
    async with engine_for(navidrome, state) as parts:
        await parts.engine.materialize(release, only=[release.tracks[0].ref])
    await kill_at(
        navidrome, state, "verified", action="materialize", key="r-again-out", count=2, only=[2]
    )
    async with engine_for(navidrome, state) as parts:
        assert await parts.engine.load_pending() == 1
        folder = folder_of(parts, release)
        assert len(list(parts.layout.absolute(folder).glob("*.flac"))) == 2
        assert (await parts.engine.remove_release(str(release.ref))).removed == 1
        assert await assert_in_step(parts, folder) == {}
        assert not parts.layout.absolute(folder).exists()


async def test_placeholders_a_stop_left_stay_while_navidrome_would_purge(
    navidrome: NavidromeInstance, state: Path
) -> None:
    """Taking left files out is a library write: asked of Navidrome under the release's
    lock, right before it - not from the answer a removal got before it waited for the
    lock."""
    release = release_of("r-left-gated")
    ref = str(release.ref)
    async with engine_for(navidrome, state) as parts:
        await parts.engine.materialize(release, only=[release.tracks[0].ref])
    await kill_at(
        navidrome, state, "verified", action="materialize", key="r-left-gated", count=2, only=[2]
    )
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        await engine.load_pending()
        answers = [None, 'Scanner.PurgeMissing is "always"']  # allowed, then changed

        async def refused(*, fresh: bool = False) -> str | None:
            return answers.pop(0) if len(answers) > 1 else answers[0]

        engine.writes_refused = refused
        folder = parts.layout.absolute(folder_of(parts, release))
        with pytest.raises(MaterializeError, match="PurgeMissing"):
            await engine.remove_release(ref)
        assert len(list(folder.glob("*.flac"))) == 2 and engine.left()
        assert await engine.repair_pending() == 0  # the repairs wait too
        engine.writes_refused = None
        assert await engine.repair_pending() == 1
        assert len(await assert_in_step(parts, folder_of(parts, release))) == 1


async def test_a_rollback_navidrome_did_not_confirm_stays_pending_until_it_is(
    navidrome: NavidromeInstance, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files are gone, but Navidrome may still list their songs: the pending record
    stays (its songs are not forwarded) until a repair's scan confirms it."""
    release = release_of("r-unconfirmed")
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        until = parts.scans.until

        async def stop(name: str) -> None:
            if name == "verified":  # scanned in; from now on Navidrome's scans fail
                monkeypatch.setattr(parts.scans, "until", failing)
                raise RuntimeError("a failure")

        async def failing(*args: Any, **kwargs: Any) -> bool:
            raise NavidromeError("startScan: HTTP 500", status=500)

        engine.step = stop
        try:
            with pytest.raises(MaterializeError):
                await engine.materialize(release)
        finally:
            engine.step = None
        folder = folder_of(parts, release)
        assert not parts.layout.absolute(folder).exists()  # the files are gone
        assert engine.left() and len(await parts.navidrome.songs_under(folder)) == 2
        [song] = [s["id"] for s in await parts.navidrome.songs_under(folder)][:1]
        assert await engine.written(song, wait=0.05) is False
        monkeypatch.setattr(parts.scans, "until", until)
        assert await engine.repair_pending() == 1
        assert await assert_in_step(parts, folder) == {}
        await written_again(parts, release)


async def test_a_song_not_looked_up_is_never_forwarded_while_it_could_be_silence(
    navidrome: NavidromeInstance, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Navidrome not answering where a song is: refused while a write is under way, or a
    file left half written is still there (it could be that file); with those files gone,
    owned songs play."""
    await kill_at(navidrome, state, "in place", action="materialize", key="r-unseen", count=1)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        await engine.load_pending()

        async def unreachable(song_id: str) -> None:
            raise NavidromeError("song: ConnectError")

        song = parts.navidrome.song
        monkeypatch.setattr(parts.navidrome, "song", unreachable)
        assert await engine.written("any-song", wait=0.05) is False  # its file is there
        folder = parts.layout.absolute(folder_of(parts, release_of("r-unseen", 1)))
        for path in folder.glob("*.flac"):
            path.unlink()
        assert await engine.written("any-song", wait=0.05) is None  # nothing silent left
        next(iter(engine._pending.values())).running = True
        assert await engine.written("any-song", wait=0.05) is False  # a write under way
        next(iter(engine._pending.values())).running = False
        monkeypatch.setattr(parts.navidrome, "song", song)
        assert await engine.repair_pending() == 1


async def test_an_add_back_killed_after_its_scan_is_intercepted_and_taken_out_again(
    navidrome: NavidromeInstance, state: Path
) -> None:
    """A release taken out as unused, its add-back killed with the files indexed and no
    rows: its songs are not forwarded, the next start takes the files out again (its record
    stays), and an old ID then adds it back."""
    release = release_of("r-restore")
    ref = str(release.ref)
    async with engine_for(navidrome, state) as parts:
        result = await parts.engine.materialize(release)
        songs = [result.created[t.ref] for t in release.tracks]
        assert (await parts.engine.remove_release(ref)).removed == 2
    await kill_at(navidrome, state, "restored in place", action="restore", ref=ref)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        await engine.scans.scan([result.folder])  # as the killed process's scan would have
        assert await engine.load_pending() == 1 and engine.writing()
        assert await engine.written(songs[0], wait=0.05) is False
    async with restarted(navidrome, state) as parts:
        assert await assert_in_step(parts, result.folder) == {}
        stored = await parts.engine.removed_record(ref)
        assert stored is not None and stored["restoring_at"] is None
        await parts.engine.load_removed()
        assert await parts.engine.restore_release(ref) == result.album_id
        recorded = await assert_in_step(parts, result.folder)
        assert sorted(str(r["song_id"]) for r in recorded.values()) == sorted(songs)


async def test_a_record_dropped_after_a_killed_add_back_takes_its_files_out_first(
    navidrome: NavidromeInstance, state: Path
) -> None:
    """The add-back killed with its files in place; then the release is to be added anew
    (its record no longer fits): the files go before the record does - nothing stays that
    no record names."""
    release = release_of("r-restore-dropped")
    ref = str(release.ref)
    async with engine_for(navidrome, state) as parts:
        result = await parts.engine.materialize(release)
        assert (await parts.engine.remove_release(ref)).removed == 2
    await kill_at(navidrome, state, "restored in place", action="restore", ref=ref)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        await engine.scans.scan([result.folder])
        await engine.load_pending()
        await engine.load_removed()
        async with engine.lock_for(ref):  # as a fill of an owned album would ask for it
            assert await engine._restore_held(ref, None, added_as=("an-owned-album",)) is None
        assert await engine.removed_record(ref) is None
        assert await assert_in_step(parts, result.folder) == {}
        await written_again(parts, release)


async def test_an_add_back_whose_rollback_is_not_confirmed_keeps_its_mark(
    navidrome: NavidromeInstance, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = release_of("r-restore-unconfirmed")
    ref = str(release.ref)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        result = await engine.materialize(release)
        assert (await engine.remove_release(ref)).removed == 2
        until = parts.scans.until

        async def failing(*args: Any, **kwargs: Any) -> bool:
            raise NavidromeError("startScan: HTTP 500", status=500)

        async def stop(name: str) -> None:
            if name == "restored in place":
                monkeypatch.setattr(parts.scans, "until", failing)
                raise RuntimeError("a failure")

        engine.step = stop
        try:
            with pytest.raises(MaterializeError):
                await engine.restore_release(ref)
        finally:
            engine.step = None
        stored = await engine.removed_record(ref)
        assert stored is not None and stored["restoring_at"] is not None and engine.left()
        monkeypatch.setattr(parts.scans, "until", until)
        assert await engine.repair_pending() == 1
        stored = await engine.removed_record(ref)
        assert stored is not None and stored["restoring_at"] is None
        assert await assert_in_step(parts, result.folder) == {}


async def test_a_removal_put_back_leaves_audio_delivered_meanwhile_as_it_is(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path
) -> None:
    """A release a stop left half taken out, a download of one of its songs in another
    format before the start's repair: the repair writes no silent file beside it."""
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        release = release_of("r-put-back-m4a", 2)
        result = await engine.materialize(release)
        songs = [result.created[t.ref] for t in release.tracks]
        await parts.store.execute(
            "UPDATE releases SET removing_at = ? WHERE ref = ?", [time.time(), str(release.ref)]
        )
        for path in parts.layout.absolute(result.folder).glob("*.flac"):
            path.unlink()
        await engine.scans.scan([result.folder])
        delivered = tmp_path / "delivered.m4a"
        delivered.write_bytes(tone(frequency_for("r-put-back-m4a"), 3, "m4a").read_bytes())
        path = await engine.replace_with_delivered(songs[0], delivered)
        assert path.endswith(".m4a")
        assert await engine.repair_removals() == 1
        recorded = await assert_in_step(parts, result.folder)
        assert {str(r["song_id"]): r["state"] for r in recorded.values()} == {
            songs[0]: "delivered",
            songs[1]: "placeholder",
        }


async def test_new_placeholders_wait_for_a_collection_being_listed(
    navidrome: NavidromeInstance, state: Path
) -> None:
    """An archive's songs are listed by Navidrome between Shijhon's check and the answer's
    start: no new placeholder moves into the library in between."""
    release = release_of("r-listing", 1)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        folder = parts.layout.absolute(folder_of(parts, release))
        async with anyio.create_task_group() as tg, engine.listing(wait=1) as ended:
            assert ended
            tg.start_soon(engine.materialize, release)
            await anyio.sleep(0.5)
            assert not engine.writing() and not folder.exists()
        recorded = await assert_in_step(parts, folder_of(parts, release))
        assert len(recorded) == 1


async def test_new_placeholders_are_refused_rather_than_written_past_a_listing(
    navidrome: NavidromeInstance, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "LISTING_WAIT_SECONDS", 0.3)
    release = release_of("r-listing-long", 1)
    async with engine_for(navidrome, state) as parts:
        async with parts.engine.listing(wait=1):
            with pytest.raises(MaterializeError, match="archives are being downloaded"):
                await parts.engine.materialize(release)
            assert not parts.engine.writing()
        assert await assert_in_step(parts, folder_of(parts, release)) == {}
        await written_again(parts, release)


OWNED = Album("R Owner", "R Owned Album", (Track("First Owned", 1), Track("Third Owned", 3)))


async def test_a_fill_killed_after_its_scan_shows_no_track_twice(
    navidrome: NavidromeInstance, state: Path
) -> None:
    """A fill's placeholders scanned into the owned album, then the kill: the next start
    takes them out (the album is as owned), and the fill then gives each track once."""
    write_album(navidrome.music, OWNED)
    navidrome.scan(full=True)
    album_id = next(
        a["id"]
        for a in navidrome.client().ok(
            "getAlbumList2", {"type": "alphabeticalByName", "size": 500}
        )["albumList2"]["album"]
        if a["name"] == OWNED.title
    )
    await kill_at(
        navidrome, state, "verified", action="materialize", key="r-fill", count=3, owned=album_id
    )
    async with restarted(navidrome, state) as parts:
        songs = await parts.navidrome.songs_of_album(album_id)
        assert len([s for s in songs if not s.get("missing")]) == 2  # the owned songs only
        release = release_of("r-fill", 3)
        owned = await parts.engine.owned_album(album_id)
        tracks = {s["trackNumber"]: s["id"] for s in owned.songs}
        links = {t.ref: tracks[t.number] for t in release.tracks if t.number in tracks}
        result = await parts.engine.materialize(release, owned_album_id=album_id, links=links)
        assert len(result.created) == 1 and result.album_id == album_id
        present = [s for s in await parts.navidrome.songs_of_album(album_id) if not s["missing"]]
        assert sorted(s["trackNumber"] for s in present) == [1, 2, 3]


# --- interception while a placeholder is written ----------------------------------------


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("r-world")) as w:
        addon = w.addon("r-source")
        w.add_source(addon)
        yield w


def call(world: DeliveryWorld, work: Any) -> Any:
    return world.server.call(work)


def new_song(world: DeliveryWorld, folder: str) -> str:
    async def listed() -> list[dict[str, Any]]:
        return await world.services.navidrome.songs_under(folder)

    [song] = [s for s in call(world, listed) if not s.get("missing")]
    return str(song["id"])


ARCHIVED = Album("R Archivist", "R Archived Album", (Track("First", 1), Track("Third", 3)))


def test_an_album_archive_waits_for_the_fill_under_way_and_holds_no_silence(
    world: DeliveryWorld,
) -> None:
    """An archive of an owned album asked for while its fill is scanned but not recorded:
    it waits for the fill, and is then refused as one with catalog tracks - not
    forwarded with the new silent files in it. Also for the library's first placeholders
    (an archive is looked at whether or not placeholders exist yet)."""
    write_album(world.nd.music, ARCHIVED)
    world.nd.scan(full=True)
    albums = world.nd.client().ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
    album_id = next(a["id"] for a in albums["albumList2"]["album"] if a["name"] == ARCHIVED.title)
    world.materialize(
        release_of("r-archive-other", 1)
    )  # placeholders exist: archives are looked at
    owned = world.client().request("download", {"id": album_id})
    assert owned.status_code == 200 and owned.content.startswith(b"PK")  # as owned: a zip
    engine = world.services.engine
    release = release_of("r-archive", 3)
    reached = threading.Event()
    go: dict[str, anyio.Event] = {}

    async def make() -> None:
        go["event"] = anyio.Event()

    async def pause(name: str) -> None:
        if name == "verified":
            reached.set()
            await go["event"].wait()

    async def resume() -> None:
        go["event"].set()

    async def fill() -> Any:
        album = await engine.owned_album(album_id)
        numbers = {s["trackNumber"]: s["id"] for s in album.songs}
        links = {t.ref: numbers[t.number] for t in release.tracks if t.number in numbers}
        return await engine.materialize(release, owned_album_id=album_id, links=links)

    call(world, make)
    engine.step = pause
    try:
        assert world.app.loop is not None
        done = asyncio.run_coroutine_threadsafe(fill(), world.app.loop)
        assert reached.wait(60)
        with ThreadPoolExecutor(1) as pool:
            archive = pool.submit(lambda: world.client().request("download", {"id": album_id}))
            time.sleep(1.0)
            assert not archive.done()  # waiting for the fill, not forwarded
            call(world, resume)
            assert len(done.result(60).created) == 1
            response = archive.result(60)
    finally:
        engine.step = None
    body = response.json()["subsonic-response"]
    assert body["status"] == "failed" and "catalog tracks" in body["error"]["message"]


def test_a_new_placeholder_scanned_before_its_row_waits_for_it(world: DeliveryWorld) -> None:
    """A client reads the album while its placeholders are scanned but not recorded yet,
    and plays one: it is served as a placeholder (the add-on's audio) once recorded, never
    Navidrome's silence."""
    engine = world.services.engine
    release = release_of("r-window", 1)
    audio = world.audio("r-window")
    isrc = release.tracks[0].isrc
    assert isrc is not None
    world.addons[0].add(FakeTrack(isrc=isrc, audio=audio))
    folder = engine.layout.release_folder(release.ref, release.artist, release.title)
    reached = threading.Event()
    go: dict[str, anyio.Event] = {}

    async def make() -> None:
        go["event"] = anyio.Event()

    async def pause(name: str) -> None:
        if name == "verified":
            reached.set()
            await go["event"].wait()

    async def resume() -> None:
        go["event"].set()

    call(world, make)
    engine.step = pause
    try:
        assert world.app.loop is not None
        done = asyncio.run_coroutine_threadsafe(engine.materialize(release), world.app.loop)
        assert reached.wait(60)
        song = new_song(world, folder)
        with ThreadPoolExecutor(1) as pool:
            streamed = pool.submit(lambda: world.client().request("stream", {"id": song}))
            time.sleep(1.0)
            assert not streamed.done()  # waiting for the row, not forwarded
            call(world, resume)
            assert done.result(60).created[release.tracks[0].ref] == song
            response = streamed.result(60)
    finally:
        engine.step = None
    assert response.status_code == 200 and response.content == audio.read_bytes()


def test_delivered_audio_plays_after_a_revert_a_stop_interrupted_never_its_silence(
    world: DeliveryWorld,
) -> None:
    """A revert stopped with the silent file in place and the row still "delivered": a
    play before the start's repair reached the song puts the delivered audio back first -
    forwarded as it was, Navidrome would serve the silence."""
    engine = world.services.engine
    song, _, audio = world.placeholder_track("r-played-early", [world.addons[0]])
    path = call(world, lambda: engine.replace_with_delivered(song, audio))
    in_place = engine.layout.absolute(path)
    delivered = in_place.read_bytes()

    async def stop_after_the_swap() -> None:
        row = await world.services.store.fetchone(
            "SELECT * FROM placeholders WHERE song_id = ?", [song]
        )
        assert row is not None
        silent = await engine._silent_file(int(row["duration_ms"]), json.loads(row["tags"]))
        in_place.replace(engine.layout.staging / f"backup-{song}.delivered.flac")
        silent.replace(in_place)
        await engine.scans.scan([path.rsplit("/", 1)[0]])  # (Navidrome has the silent file)

    call(world, stop_after_the_swap)
    # The album's archive first: it holds the delivered audio, put back before it is
    # forwarded (its row alone, "delivered", would have let the silent file through).
    album = world.nd.native("GET", f"song/{song}").json()["albumId"]
    archive = world.client().request("download", {"id": album})
    assert archive.status_code == 200 and archive.content.startswith(b"PK")
    assert len(archive.content) > len(delivered) // 2
    assert not list(engine.layout.staging.glob(f"backup-{song}.*"))
    # The same stop again, and Navidrome's scans failing: a play still gets the delivered
    # audio (put back at once); its backup waits for the confirmation.
    call(world, stop_after_the_swap)
    until = engine.scans.until

    async def failing(*args: Any, **kwargs: Any) -> bool:
        raise NavidromeError("startScan: HTTP 503", status=503)

    engine.scans.until = failing  # type: ignore[method-assign]
    try:
        played = world.client().request("stream", {"id": song})
    finally:
        engine.scans.until = until  # type: ignore[method-assign]
    assert played.status_code == 200 and played.content == delivered
    assert list(engine.layout.staging.glob(f"backup-{song}.*"))
    assert call(world, lambda: engine.recover_swap(song)) == 0  # confirmed now
    assert not list(engine.layout.staging.glob(f"backup-{song}.*"))


def test_a_delivered_row_over_silence_is_not_played_while_the_song_is_beside_it(
    world: DeliveryWorld,
) -> None:
    """A row "delivered" whose path holds the silent
    placeholder, its audio in no backup, and the song in another format beside it (which
    may be its audio: nothing is taken away). The song is not settled then - its plays,
    its downloads and its album's archive answer with an error instead of Navidrome's
    silence, also after the next start's repairs (its backups stay: what they find it by).
    With the other file moved away it is a placeholder again, played from the add-ons."""
    services = world.services
    engine, staging = services.engine, services.engine.layout.staging
    song, _, audio = world.placeholder_track("r-beside", [world.addons[0]])
    path = call(world, lambda: engine.replace_with_delivered(song, audio))
    in_place = engine.layout.absolute(path)
    beside = in_place.with_suffix(".m4a")
    other = world.audio("r-beside", "m4a").read_bytes()
    assert in_place.suffix == ".flac"

    async def state() -> str:
        row = await services.store.fetchone("SELECT * FROM placeholders WHERE song_id = ?", [song])
        assert row is not None
        return str(row["state"])

    async def left_so() -> None:
        row = await services.store.fetchone("SELECT * FROM placeholders WHERE song_id = ?", [song])
        assert row is not None
        length, tags = int(row["duration_ms"]), json.loads(row["tags"])
        silent = await engine._silent_file(length, tags)
        again = await engine._silent_file(length, tags)
        silent.replace(in_place)
        again.replace(staging / f"backup-{song}.flac")
        await engine.scans.scan([path.rsplit("/", 1)[0]])  # (Navidrome has the silent file)
        beside.write_bytes(other)

    async def next_start() -> None:
        writes = PlaceholderWrites(services.navidrome)
        await StartupChecks(services.navidrome, engine, services.store, writes).run()

    call(world, left_so)
    album = world.nd.native("GET", f"song/{song}").json()["albumId"]
    for start in (None, next_start):
        if start is not None:
            call(world, start)
        for ident in (song, album):
            for method in ("stream", "download") if ident == song else ("download",):
                answer = world.client().request(method, {"id": ident})
                assert not answer.content.startswith((b"fLaC", b"PK")), (method, ident)
                assert answer.json()["subsonic-response"]["status"] == "failed"
        assert beside.read_bytes() == other and call(world, state) == "delivered"
        assert list(staging.glob(f"backup-{song}.*"))
    beside.unlink()  # the other file moved away: the next repair makes it a placeholder
    call(world, next_start)
    assert call(world, state) == "placeholder"
    assert not list(staging.glob(f"backup-{song}.*"))
    played = world.client().request("stream", {"id": song})
    assert played.status_code == 200 and played.content == audio.read_bytes()


PLAIN = Album("R Plain", "R Plain Album", (Track("Only", 1),))


def test_placeholders_a_stop_left_are_refused_until_the_start_takes_them_out(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As a stop leaves them: files in place and indexed, a pending record, no rows."""
    engine = world.services.engine
    release = release_of("r-left", 1)
    track = release.tracks[0]
    folder = engine.layout.release_folder(release.ref, release.artist, release.title)
    relative = f"{folder}/{Layout.file_name(track)}"

    async def stop_halfway() -> None:
        comments = tagging.placeholder_tags(tagging.catalog_album_tags(release), track, release)
        target = engine.layout.absolute(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        await engine.silence.write(track.duration_ms, target)
        tagging.write_flac(target, comments)
        await world.services.store.execute(
            "INSERT INTO pending_placeholders (release_ref, folder, paths, cover, new_folder,"
            " started_at) VALUES (?, ?, ?, 0, 1, ?)",
            [str(release.ref), folder, json.dumps([relative]), time.time()],
        )
        await engine.scans.scan([folder])
        await engine.load_pending()  # as the next start reads it

    call(world, stop_halfway)
    song = new_song(world, folder)
    monkeypatch.setattr(intercept, "WRITE_WAIT_SECONDS", 0.5)
    for method, params in (("stream", {"id": song}), ("download", {"id": song})):
        refused = world.client().request(method, params)
        body = refused.json()["subsonic-response"]
        assert body["status"] == "failed" and "being added" in body["error"]["message"]
    # A playlist holding it (added by its ID before the stop): its archive is refused too.
    playlist = world.nd.client().ok("createPlaylist", {"name": "r-left", "songId": song})
    archive = world.client().request("download", {"id": playlist["playlist"]["id"]})
    assert archive.json()["subsonic-response"]["status"] == "failed"
    # Its album's archive too - also when Navidrome cannot list the album just then.
    album = world.nd.native("GET", f"song/{song}").json()["albumId"]
    assert (
        world.client().request("download", {"id": album}).json()["subsonic-response"]["status"]
        == "failed"
    )

    async def unlisted(album_id: str) -> list[dict[str, Any]]:
        raise NavidromeError("song: HTTP 500", status=500)

    monkeypatch.setattr(world.services.navidrome, "songs_of_album", unlisted)
    unknown = world.client().request("download", {"id": album})
    assert unknown.json()["subsonic-response"]["status"] == "failed"
    monkeypatch.undo()
    # Another, owned album is downloaded as ever - unless Navidrome cannot say whether its
    # ID is a playlist's (what it holds is not known then).
    write_album(world.nd.music, PLAIN)
    world.nd.scan(full=True)
    albums = world.nd.client().ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
    plain = next(a["id"] for a in albums["albumList2"]["album"] if a["name"] == PLAIN.title)
    assert world.client().request("download", {"id": plain}).content.startswith(b"PK")

    async def unknown_playlist(playlist_id: str) -> list[str]:
        raise NavidromeError("native GET: HTTP 500", status=500)

    monkeypatch.setattr(world.services.navidrome, "playlist_song_ids", unknown_playlist)
    uncertain = world.client().request("download", {"id": plain})
    assert uncertain.json()["subsonic-response"]["status"] == "failed"
    monkeypatch.undo()
    monkeypatch.setattr(intercept, "WRITE_WAIT_SECONDS", 0.5)
    stream = world.client().request("getTranscodeStream", {"mediaId": song, "mediaType": "song"})
    assert stream.status_code == 503  # an HTTP error, as Navidrome answers that method
    assert call(world, engine.repair_pending) == 1
    assert not engine.layout.absolute(relative).exists()
    gone = world.client().request("stream", {"id": song})
    assert not gone.content.startswith(b"fLaC")  # Navidrome has no file for it now
    result = world.materialize(release)
    assert result.created[track.ref] == song  # the same path: the same song ID


# --- the startup repairs hold the songs' locks -------------------------------------------


async def test_a_removal_put_back_never_writes_silence_over_a_download(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path
) -> None:
    """A release a stop left half taken out, and a download of one of its songs while the
    start puts it back: the download waits for the repair, and its audio stays."""
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        release = release_of("r-put-back", 2)
        result = await engine.materialize(release)
        songs = [result.created[t.ref] for t in release.tracks]
        rows = await parts.store.fetchall(
            "SELECT path FROM placeholders WHERE release_ref = ?", [str(release.ref)]
        )
        await parts.store.execute(
            "UPDATE releases SET removing_at = ? WHERE ref = ?", [time.time(), str(release.ref)]
        )
        for row in rows:
            parts.layout.absolute(str(row["path"])).unlink()
        await engine.scans.scan([result.folder])
        delivered = tmp_path / "delivered.flac"
        delivered.write_bytes(tone(frequency_for("r-put-back"), 3, "flac").read_bytes())
        planned, go = anyio.Event(), anyio.Event()

        async def pause(name: str) -> None:
            if name == "put back planned":
                planned.set()
                await go.wait()

        engine.step = pause
        outcome: dict[str, Any] = {}
        try:
            async with anyio.create_task_group() as tg:

                async def repair() -> None:
                    outcome["repaired"] = await engine.repair_removals()

                async def download() -> None:
                    outcome["path"] = await engine.replace_with_delivered(songs[0], delivered)

                tg.start_soon(repair)
                await planned.wait()
                tg.start_soon(download)
                key = f"song:{songs[0]}"
                with anyio.fail_after(30):  # the download waits for the song's lock
                    while "path" not in outcome and engine._locks._locks[key][1] < 2:  # noqa: ASYNC110
                        await anyio.sleep(0.01)
                assert "path" not in outcome  # (it did not: the repair wrote silence over it)
                go.set()
        finally:
            engine.step = None
        assert outcome["repaired"] == 1
        recorded = await assert_in_step(parts, result.folder)
        row = recorded[outcome["path"]]
        assert row["state"] == "delivered" and str(row["song_id"]) == songs[0]
        marked = await parts.store.fetchone(
            "SELECT removing_at FROM releases WHERE ref = ?", [str(release.ref)]
        )
        assert marked is not None and marked["removing_at"] is None


async def test_the_startup_repairs_wait_for_navidrome_as_long_as_it_takes(tmp_path: Path) -> None:
    """Navidrome answering only after the wait (a long migration after an upgrade): the
    repairs still run - they never give up."""

    class Late:
        library_id = 1

        def __init__(self) -> None:
            self.pings = 0
            self.repaired = False

        async def subsonic(self, method: str, params: Any = ()) -> dict[str, Any]:
            if method == "ping":
                self.pings += 1
                if self.pings < 4:
                    raise NavidromeError("ping: ConnectError")
                return {"serverVersion": TESTED_VERSION}
            raise NavidromeError(f"{method}: not here")

        async def native_json(self, method: str, path: str, **kwargs: Any) -> Any:
            return {"config": {"Scanner": {"PurgeMissing": "never"}}}

    late = Late()
    store = await Store.open(tmp_path / "shijhon.sqlite3")
    try:
        engine = PlaceholderEngine(
            layout=Layout(tmp_path / "music", "_shijhon"),
            navidrome=late,  # type: ignore[arg-type]
            scans=ScanCoordinator(late),  # type: ignore[arg-type]
            silence=SilenceMaker(),
            store=store,
        )
        repair = engine.repair_pending

        async def repair_pending() -> int:
            late.repaired = True
            return await repair()

        engine.repair_pending = repair_pending  # type: ignore[method-assign]
        writes = PlaceholderWrites(late)  # type: ignore[arg-type]
        checks = StartupChecks(
            late,  # type: ignore[arg-type]
            engine,
            store,
            writes,
            wait_seconds=0,
            retry_seconds=(0, 0),
        )
        with anyio.fail_after(10):
            await checks.run()
    finally:
        await store.close()
    assert late.pings == 4 and late.repaired


# --- swaps: replacement, revert, retag (the retag's recovery, skipped scans) ------------


SWAP_STEPS = ["between renames", "swapped", "verified", "committed"]


def swap_step(what: str, step: str) -> str:
    return step if step == "between renames" else f"{what} {step}"


def delivered_audio(tmp_path: Path, fmt: str, seed: str) -> Path:
    path = tmp_path / f"delivered-{seed}.{fmt}"
    path.write_bytes(tone(frequency_for(seed), 3, fmt).read_bytes())  # type: ignore[arg-type]
    return path


async def one_placeholder(nd: NavidromeInstance, state: Path, key: str) -> tuple[str, str]:
    """A one-track catalog album: (its song ID, its folder)."""
    async with engine_for(nd, state) as parts:
        release = release_of(key, 1)
        result = await parts.engine.materialize(release)
        return result.created[release.tracks[0].ref], result.folder


async def row_of(parts: EngineParts, song: str) -> Any:
    row = await parts.store.fetchone("SELECT * FROM placeholders WHERE song_id = ?", [song])
    assert row is not None
    return row


@pytest.mark.parametrize("step", SWAP_STEPS)
@pytest.mark.parametrize("fmt", ["flac", "m4a"])
async def test_a_replacement_killed_at_any_step_ends_with_the_file_its_row_names(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, fmt: str, step: str
) -> None:
    key = f"r-replace-{fmt}-" + step.replace(" ", "-")
    song, folder = await one_placeholder(navidrome, state, key)
    audio = delivered_audio(tmp_path, fmt, key)
    await kill_at(
        navidrome, state, swap_step("replacement", step), action="replace", song=song,
        delivered=str(audio),
    )  # fmt: skip
    async with restarted(navidrome, state) as parts:
        await assert_in_step(parts, folder)
        row = await row_of(parts, song)
        assert row["state"] == ("delivered" if step == "committed" else "placeholder")
        # Swapped again from there: the replacement, then the revert.
        if row["state"] == "placeholder":
            await parts.engine.replace_with_delivered(song, audio)
        assert await parts.engine.revert_to_placeholder(song)
        await assert_in_step(parts, folder)


@pytest.mark.parametrize("step", SWAP_STEPS)
@pytest.mark.parametrize("fmt", ["flac", "m4a"])
async def test_a_revert_killed_at_any_step_ends_with_the_file_its_row_names(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, fmt: str, step: str
) -> None:
    key = f"r-revert-{fmt}-" + step.replace(" ", "-")
    song, folder = await one_placeholder(navidrome, state, key)
    async with engine_for(navidrome, state) as parts:
        await parts.engine.replace_with_delivered(song, delivered_audio(tmp_path, fmt, key))
    await kill_at(navidrome, state, swap_step("revert", step), action="revert", song=song)
    async with restarted(navidrome, state) as parts:
        await assert_in_step(parts, folder)
        row = await row_of(parts, song)
        assert row["state"] == ("placeholder" if step == "committed" else "delivered")


@pytest.mark.parametrize("step", SWAP_STEPS)
async def test_a_retag_killed_at_any_step_keeps_the_file_its_row_names(
    navidrome: NavidromeInstance, state: Path, step: str
) -> None:
    """Both files are silent: the recovery tells them apart by their tags (the row's)."""
    key = "r-retag-" + step.replace(" ", "-")
    song, folder = await one_placeholder(navidrome, state, key)
    await kill_at(
        navidrome, state, swap_step("retag", step), action="retag", song=song, key=key,
        count=1, title="A Fresher Title",
    )  # fmt: skip
    async with restarted(navidrome, state) as parts:
        await assert_in_step(parts, folder)
        row = await row_of(parts, song)
        title = "A Fresher Title" if step == "committed" else f"Album {key} Song 1"
        assert row["title"] == title
        listed = await parts.navidrome.song(song)
        assert listed is not None and listed["title"] == title


@pytest.mark.parametrize("step", ["swapped", "verified", "committed"])
@pytest.mark.parametrize("how", ["canceled", "failed"])
@pytest.mark.parametrize("what", ["replacement", "revert", "retag"])
async def test_a_swap_canceled_or_failed_at_any_step_keeps_file_and_row_together(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, what: str, how: str, step: str
) -> None:
    """After its row's update the new file stays, whatever happens next: a delivered
    row never gets the silent file back."""
    key = f"r-{what}-{how}-{step}"
    song, folder = await one_placeholder(navidrome, state, key)
    release = release_of(key, 1)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        audio = delivered_audio(tmp_path, "flac", key)
        if what == "revert":
            await engine.replace_with_delivered(song, audio)
        with anyio.CancelScope() as scope:

            async def stop(name: str) -> None:
                if name != f"{what} {step}":
                    return
                if how == "failed":
                    raise RuntimeError("a failure")
                scope.cancel()
                await anyio.lowlevel.checkpoint()

            engine.step = stop
            try:
                if what == "replacement":
                    await engine.replace_with_delivered(song, audio)
                elif what == "revert":
                    await engine.revert_to_placeholder(song)
                else:
                    track = dataclasses.replace(release.tracks[0], title="Retitled")
                    await engine.retag(song, track, release)
            except (ReplaceError, RuntimeError):
                assert how == "failed"
            finally:
                engine.step = None
        await assert_in_step(parts, folder)
        row = await row_of(parts, song)
        done = step == "committed"
        if what == "replacement":
            assert row["state"] == ("delivered" if done else "placeholder")
        elif what == "revert":
            assert row["state"] == ("placeholder" if done else "delivered")
        else:
            assert (row["title"] == "Retitled") == done


async def test_a_swap_before_the_starts_repair_finishes_the_interrupted_one_first(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path
) -> None:
    """A revert killed with the silent file in place and the delivered audio in its backup,
    the row still "delivered"; the expiry tries again before the start's repair reached the
    song: the interrupted swap is put right first - its backup is never overwritten, and a
    delivered file in its backup is never taken for missing."""
    song, folder = await one_placeholder(navidrome, state, "r-early-flac")
    async with engine_for(navidrome, state) as parts:
        audio = delivered_audio(tmp_path, "flac", "r-early-flac")
        path = await parts.engine.replace_with_delivered(song, audio)
        delivered = parts.layout.absolute(path).read_bytes()
    await kill_at(navidrome, state, "revert swapped", action="revert", song=song)
    async with engine_for(navidrome, state) as parts:  # (no startup repair yet)
        backup = parts.layout.staging / f"backup-{song}.delivered.flac"
        assert backup.read_bytes() == delivered
        assert await parts.engine.revert_to_placeholder(song)
        await assert_in_step(parts, folder)
        assert (await row_of(parts, song))["state"] == "placeholder"
    # Another format, killed between its renames: the delivered file is in its backup, not
    # gone - the expiry's repair of missing files puts it back instead of giving it up.
    song, folder = await one_placeholder(navidrome, state, "r-early-m4a")
    async with engine_for(navidrome, state) as parts:
        audio = delivered_audio(tmp_path, "m4a", "r-early-m4a")
        path = await parts.engine.replace_with_delivered(song, audio)
        delivered = parts.layout.absolute(path).read_bytes()
    await kill_at(navidrome, state, "between renames", action="revert", song=song)
    async with engine_for(navidrome, state) as parts:
        assert not parts.layout.absolute(path).exists()
        assert not await parts.engine.revert_to_placeholder(song, only_if_missing=True)
        await assert_in_step(parts, folder)
        assert (await row_of(parts, song))["state"] == "delivered"
        assert parts.layout.absolute(path).read_bytes() == delivered


async def test_a_repair_of_a_swap_killed_itself_is_finished_at_the_next_start(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path
) -> None:
    """The repair keeps the backup until Navidrome has confirmed the file put back: killed
    in between, the next start still finds the swap and finishes it (no silent file stays
    beside the delivered one)."""
    song, folder = await one_placeholder(navidrome, state, "r-repair-killed")
    async with engine_for(navidrome, state) as parts:
        audio = delivered_audio(tmp_path, "m4a", "r-repair-killed")
        await parts.engine.replace_with_delivered(song, audio)
    await kill_at(navidrome, state, "revert swapped", action="revert", song=song)
    await kill_at(navidrome, state, "recovery placed", action="recover", song=song)
    async with engine_for(navidrome, state) as parts:
        assert list(parts.layout.staging.glob(f"backup-{song}.*"))  # still the evidence
    async with restarted(navidrome, state) as parts:
        await assert_in_step(parts, folder)
        assert (await row_of(parts, song))["state"] == "delivered"


@pytest.mark.parametrize("step", ["prepared", "committed"])
@pytest.mark.parametrize("fmt", ["flac", "m4a"])
async def test_a_revert_of_missing_delivered_audio_killed_never_leaves_silence_under_its_row(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, fmt: str, step: str
) -> None:
    """Delivered audio whose file is gone gets its placeholder back: the row says so
    before the silent file is in place, so a stop in between leaves a placeholder's row
    (played from the add-ons) and a backup the next start puts in place - never a
    "delivered" row over the silent file."""
    key = f"r-missing-{fmt}-{step}"
    song, folder = await one_placeholder(navidrome, state, key)
    async with engine_for(navidrome, state) as parts:
        path = await parts.engine.replace_with_delivered(song, delivered_audio(tmp_path, fmt, key))
        parts.layout.absolute(path).unlink()
    await kill_at(navidrome, state, f"revert of a missing file {step}", action="revert", song=song)
    async with restarted(navidrome, state) as parts:
        row = await row_of(parts, song)
        if step == "prepared":  # nothing changed yet: the expiry's next look does it
            assert row["state"] == "delivered" and not parts.layout.absolute(path).exists()
            assert await parts.engine.revert_to_placeholder(song, only_if_missing=True)
        await assert_in_step(parts, folder)
        assert (await row_of(parts, song))["state"] == "placeholder"


def _stat(path: Path) -> tuple[int, int, bytes]:
    found = path.stat()
    return found.st_mtime_ns, found.st_size, path.read_bytes()


async def test_a_repair_never_follows_a_link_to_a_file_outside_the_placeholder_folder(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path
) -> None:
    """A delivered file someone replaced by a link to their own copy
    of it (a copy keeps Shijhon's marks). Neither a revert nor the repair of an interrupted
    swap reads, writes or touches the owner's file through the link - at the file's place
    or among the backups: its time and content stay as they were."""
    song, folder = await one_placeholder(navidrome, state, "r-linked")
    owned = tmp_path / "owned copy.flac"  # outside the placeholder folder
    async with engine_for(navidrome, state) as parts:
        engine, staging = parts.engine, parts.layout.staging
        audio = delivered_audio(tmp_path, "flac", "r-linked")
        path = await engine.replace_with_delivered(song, audio)
        in_place = parts.layout.absolute(path)
        owned.write_bytes(in_place.read_bytes())
        long_ago = time.time() - 400 * 86400
        os.utime(owned, (long_ago, long_ago))
        before = _stat(owned)
        marker = tagging.marker_of(owned)
        assert marker is not None and not marker.silence  # (it would pass for the row's file)
        regular = in_place.read_bytes()

        # 1. A link in place: a revert leaves it as it is (nothing to put back through it).
        in_place.unlink()
        in_place.symlink_to(owned)
        with pytest.raises(ReplaceError, match="is a link"):
            await engine.revert_to_placeholder(song)
        assert in_place.is_symlink() and _stat(owned) == before
        assert not list(staging.glob(f"backup-{song}.*"))

        # 2. The link among the backups, the silent file in place (a revert a stop
        # interrupted, as a build before this one would have left it): the link is not the
        # delivered audio's backup - it goes, the owner's file is not touched. The audio is
        # not to be had, so the row follows the file in place: a placeholder again, played
        # from the add-ons - never a "delivered" row over silence.
        row = await row_of(parts, song)
        silent = await engine._silent_file(int(row["duration_ms"]), json.loads(row["tags"]))
        backup = staging / f"backup-{song}.delivered.flac"
        in_place.rename(backup)
        silent.replace(in_place)
        assert await engine.recover_swap(song) == 0
        assert not backup.is_symlink() and _stat(owned) == before
        assert (await row_of(parts, song))["state"] == "placeholder"
        await assert_in_step(parts, folder)
        with pytest.raises(OSError, match="link"):  # (the copy itself never follows one)
            backup.symlink_to(owned)
            try:
                engine_module._copy_in_place(backup, in_place)
            finally:
                backup.unlink()
        assert _stat(owned) == before

        # 3. The link in place and the delivered audio in a backup (a repair that had put
        # it back before, not confirmed yet): the audio replaces the link itself - the
        # owner's file is not taken for the file in place, nor given a new time.
        assert await engine.replace_with_delivered(song, audio) == path
        regular = in_place.read_bytes()
        in_place.unlink()
        in_place.symlink_to(owned)
        backup.write_bytes(regular)
        assert await engine.recover_swap(song) == 1
        assert not in_place.is_symlink() and in_place.read_bytes() == regular
        assert _stat(owned) == before and not list(staging.glob(f"backup-{song}.*"))
        await assert_in_step(parts, folder)
        assert (await row_of(parts, song))["state"] == "delivered"

        # The row does not follow the silent file while the song is there in another
        # format beside it (that may be its audio: a placeholder's repair would remove it)
        # - and the song is not settled either: its backups stay, the repair fails.
        row = await row_of(parts, song)
        silent = await engine._silent_file(int(row["duration_ms"]), json.loads(row["tags"]))
        again = await engine._silent_file(int(row["duration_ms"]), json.loads(row["tags"]))
        beside = in_place.with_suffix(".mp3")
        beside.write_bytes(regular)
        silent.replace(in_place)
        again.replace(staging / f"backup-{song}.flac")
        with pytest.raises(ReplaceError, match="another format beside it"):
            await engine.recover_swap(song)
        async with engine.lock_for(f"song:{song}"):
            assert not await engine.settle_song(song)  # (a play is not forwarded)
    # The next start finds it by its backups and leaves it so: never a "delivered" row that
    # passes for settled over the silent file, with nothing left to find it by.
    async with restarted(navidrome, state) as parts:
        engine = parts.engine
        assert song in engine.interrupted()
        assert beside.read_bytes() == regular
        assert (await row_of(parts, song))["state"] == "delivered"
        async with engine.lock_for(f"song:{song}"):
            assert not await engine.settle_song(song)
        beside.unlink()  # the other file moved away: the row follows the silent file
        assert await engine.recover_swap(song) == 0
        assert (await row_of(parts, song))["state"] == "placeholder"
        await assert_in_step(parts, folder)


async def test_a_swap_whose_repair_navidrome_does_not_confirm_is_finished_a_minute_later(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Navidrome busy when the start's repairs run (its own startup scan): each interrupted
    swap is tried on its own, and those not confirmed again every minute - not only at the
    next start."""
    songs = []
    for key in ("r-later-1", "r-later-2"):
        song, folder = await one_placeholder(navidrome, state, key)
        async with engine_for(navidrome, state) as parts:
            await parts.engine.replace_with_delivered(song, delivered_audio(tmp_path, "flac", key))
        await kill_at(navidrome, state, "revert swapped", action="revert", song=song)
        songs.append((song, folder))
    async with engine_for(navidrome, state) as parts:
        until = parts.scans.until

        async def failing(*args: Any, **kwargs: Any) -> bool:
            raise NavidromeError("startScan: HTTP 503", status=503)

        monkeypatch.setattr(parts.scans, "until", failing)
        writes = PlaceholderWrites(parts.navidrome)
        checks = StartupChecks(
            parts.navidrome, parts.engine, parts.store, writes, repair_seconds=0.05
        )
        await checks.run()  # both tried (the first one's failure does not stop the second)
        for song, _ in songs:
            # The delivered file is back in place already; its backup waits for Navidrome.
            assert list(parts.layout.staging.glob(f"backup-{song}.*"))
            row = await row_of(parts, song)
            marker = tagging.marker_of(parts.layout.absolute(str(row["path"])))
            assert row["state"] == "delivered" and marker is not None and not marker.silence
        monkeypatch.setattr(parts.scans, "until", until)
        async with anyio.create_task_group() as tg:
            tg.start_soon(checks.keep_repairing)
            with anyio.fail_after(60):
                while list(parts.layout.staging.glob("backup-*")):  # noqa: ASYNC110
                    await anyio.sleep(0.05)
            tg.cancel_scope.cancel()
        for _, folder in songs:
            await assert_in_step(parts, folder)


async def test_a_revert_waits_for_archives_before_it_takes_its_songs_lock(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path
) -> None:
    """An archive being listed may need the song's lock (to put an interrupted swap of it
    right) while the revert waits for the archive: the revert waits before it takes the
    lock, never under it (each would wait for the other)."""
    song, folder = await one_placeholder(navidrome, state, "r-revert-order")
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        await engine.replace_with_delivered(song, delivered_audio(tmp_path, "flac", "r-order"))
        async with anyio.create_task_group() as tg, engine.listing(wait=1):
            tg.start_soon(engine.revert_to_placeholder, song)
            await anyio.sleep(0.3)
            with anyio.fail_after(2):
                async with engine.lock_for(f"song:{song}"):
                    assert (await row_of(parts, song))["state"] == "delivered"
        await assert_in_step(parts, folder)
        assert (await row_of(parts, song))["state"] == "placeholder"
        # Two reverts of one song, the second given up while it waits for the first: the
        # first stays a write archives wait for.
        await engine.replace_with_delivered(song, delivered_audio(tmp_path, "flac", "r-order-2"))
        swapped, go = anyio.Event(), anyio.Event()

        async def pause(name: str) -> None:
            if name == "revert swapped":
                swapped.set()
                await go.wait()

        engine.step = pause
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(engine.revert_to_placeholder, song)
                await swapped.wait()
                with anyio.move_on_after(0.3):
                    await engine.revert_to_placeholder(song)
                assert engine.writing()
                go.set()
        finally:
            engine.step = None
        assert not engine.writing()
        await assert_in_step(parts, folder)


async def test_a_scan_navidrome_skipped_does_not_pass_for_the_new_file(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path
) -> None:
    """Delivered FLAC in place of the silent FLAC (the same path, the same song ID): only a
    scan that read the new file confirms it - Navidrome then lists its size."""
    song, _ = await one_placeholder(navidrome, state, "r-skipped")
    async with engine_for(navidrome, state) as parts:
        scan = parts.scans.scan
        skipped: list[int] = []

        async def dropped(folders: Any) -> None:
            if not skipped:
                skipped.append(1)  # as Navidrome drops a request while another scan runs
                return
            await scan(folders)

        parts.scans.scan = dropped  # type: ignore[method-assign]
        try:
            path = await parts.engine.replace_with_delivered(
                song, delivered_audio(tmp_path, "flac", "r-skipped")
            )
        finally:
            parts.scans.scan = scan  # type: ignore[method-assign]
        assert skipped
        listed = await parts.navidrome.song(song)
        assert listed is not None
        assert listed["size"] == parts.layout.absolute(path).stat().st_size
        assert listed["duration"] > 2  # the delivered audio's, not the placeholder's second


async def test_files_reach_the_disk_before_their_rows(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new files and their folder are synced before anything records them, and a
    swap's before its row's update (a power loss cannot leave a recorded file empty)."""
    events: list[tuple[str, str]] = []
    real_file, real_dir = durable.sync_file, durable.sync_dir

    def sync_file(path: Path) -> None:
        events.append(("file", str(path)))
        real_file(path)

    def sync_dir(path: Path) -> None:
        events.append(("dir", str(path)))
        real_dir(path)

    monkeypatch.setattr(durable, "sync_file", sync_file)
    monkeypatch.setattr(durable, "sync_dir", sync_dir)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine

        async def note(name: str) -> None:
            events.append(("step", name))

        engine.step = note
        try:
            release = release_of("r-durable", 2)
            result = await engine.materialize(release)
            song = result.created[release.tracks[0].ref]
            path = await engine.replace_with_delivered(
                song, delivered_audio(tmp_path, "m4a", "r-durable")
            )
        finally:
            engine.step = None
    folder = str(parts.layout.absolute(result.folder))
    placed = events.index(("step", "in place"))
    before = events[:placed]
    assert sum(1 for kind, _ in before if kind == "file") >= 2
    assert ("dir", folder) in before
    swapped = events.index(("step", "replacement swapped"))
    between = events[placed:swapped]
    assert any(kind == "file" and p.endswith(".m4a") for kind, p in between)
    assert ("dir", str(parts.layout.absolute(path).parent)) in between
    assert ("dir", str(parts.layout.staging)) in between


async def test_a_backup_that_cannot_go_never_brings_the_old_file_back(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's update is the commit point. A failure after it - here the backup that
    cannot be removed - leaves the delivered file under its delivered row (the silent file
    is not put back under it); the next start removes the backup."""
    song, folder = await one_placeholder(navidrome, state, "r-backup-stuck")
    async with engine_for(navidrome, state) as parts:
        unlink = Path.unlink

        def stuck(self: Path, missing_ok: bool = False) -> None:
            if self.name.startswith("backup-"):
                raise PermissionError(13, "Permission denied")
            unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", stuck)
        path = await parts.engine.replace_with_delivered(
            song, delivered_audio(tmp_path, "flac", "r-backup-stuck")
        )
        monkeypatch.setattr(Path, "unlink", unlink)
        assert (await row_of(parts, song))["state"] == "delivered"
        marker = tagging.marker_of(parts.layout.absolute(path))
        assert marker is not None and not marker.silence
        assert list(parts.layout.staging.glob("backup-*"))
    async with restarted(navidrome, state) as parts:
        await assert_in_step(parts, folder)
        assert (await row_of(parts, song))["state"] == "delivered"


# --- compensations a cancellation starts do not wait long for Navidrome -----------


async def stuck(*args: Any, **kwargs: Any) -> bool:
    """A scan that does not end: Navidrome busy with a full scan, or not answering."""
    await anyio.sleep_forever()
    return False


async def test_a_canceled_commit_does_not_wait_for_navidrome_to_confirm_its_rollback(
    navidrome: NavidromeInstance, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown while Navidrome is busy: the files go at once, the confirming scan is cut
    short, and the pending record stays for the next start (or the minute's repair)."""
    monkeypatch.setattr(engine_module, "COMPENSATION_SECONDS", 0.3)
    release = release_of("r-bounded")
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        until = parts.scans.until
        canceled = 0.0
        with anyio.fail_after(60), anyio.CancelScope() as scope:

            async def stop(name: str) -> None:
                nonlocal canceled
                if name == "verified":
                    monkeypatch.setattr(parts.scans, "until", stuck)
                    canceled = time.monotonic()
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

            engine.step = stop
            try:
                await engine.materialize(release)
            finally:
                engine.step = None
        assert scope.cancelled_caught and time.monotonic() - canceled < 5
        folder = folder_of(parts, release)
        assert not parts.layout.absolute(folder).exists() and engine.left()
        monkeypatch.setattr(parts.scans, "until", until)
        assert await engine.repair_pending() == 1
        assert await assert_in_step(parts, folder) == {}


async def test_a_canceled_swap_does_not_wait_for_navidrome_to_confirm_its_undo(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "COMPENSATION_SECONDS", 0.3)
    song, folder = await one_placeholder(navidrome, state, "r-bounded-swap")
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        until = parts.scans.until
        canceled = 0.0
        with anyio.fail_after(60), anyio.CancelScope() as scope:

            async def stop(name: str) -> None:
                nonlocal canceled
                if name == "replacement swapped":
                    monkeypatch.setattr(parts.scans, "until", stuck)
                    canceled = time.monotonic()
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

            engine.step = stop
            try:
                await engine.replace_with_delivered(
                    song, delivered_audio(tmp_path, "m4a", "r-bounded-swap")
                )
            finally:
                engine.step = None
        assert scope.cancelled_caught and time.monotonic() - canceled < 5
        row = await row_of(parts, song)
        in_place = parts.layout.absolute(str(row["placeholder_path"]))
        marker = tagging.marker_of(in_place)  # the silent file is back under its row at once
        assert row["state"] == "placeholder" and marker is not None and marker.silence
        # ... and its backup stays until Navidrome has confirmed it: the next start's.
        assert list(parts.layout.staging.glob(f"backup-{song}.*"))
        monkeypatch.setattr(parts.scans, "until", until)
        assert await engine.recover_swap(song) == 0
        await assert_in_step(parts, folder)


async def test_a_canceled_removal_is_put_back_without_waiting_for_navidrome(
    navidrome: NavidromeInstance, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files are back at once; the mark stays until a repair's scan confirms them."""
    monkeypatch.setattr(engine_module, "COMPENSATION_SECONDS", 0.3)
    release = release_of("r-bounded-removal")
    ref = str(release.ref)
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        result = await engine.materialize(release)
        until = parts.scans.until
        canceled = 0.0
        with anyio.fail_after(60), anyio.CancelScope() as scope:

            async def canceling(*args: Any, **kwargs: Any) -> bool:
                nonlocal canceled
                if not canceled:
                    canceled = time.monotonic()
                    scope.cancel()
                return await stuck()

            monkeypatch.setattr(parts.scans, "until", canceling)
            await engine.remove_release(ref)
        assert scope.cancelled_caught and time.monotonic() - canceled < 5
        files = list(parts.layout.absolute(result.folder).glob("*.flac"))
        assert len(files) == 2  # back in place
        marked = await parts.store.fetchone("SELECT removing_at FROM releases WHERE ref = ?", [ref])
        assert marked is not None and marked["removing_at"] is not None
        monkeypatch.setattr(parts.scans, "until", until)
        assert await engine.repair_removals() == 1
        assert len(await assert_in_step(parts, result.folder)) == 2
