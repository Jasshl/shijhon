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
from contextlib import asynccontextmanager, contextmanager
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
from tests.harness.fragmented import fragmented_mp4, is_fragmented
from tests.harness.library import Album, Track, frequency_for, tone, write_album
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
AUDIO = tagging.DELIVERED_SUFFIXES  # (every container delivered audio may be)
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


@pytest.mark.parametrize("step", ["swapped", "verified"])
def test_a_kept_stream_whose_library_write_stops_stays_a_placeholder_and_is_kept_later(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """A streamed song handed over to be kept, its swap stopped part way (its
    compensation runs): its row and file stay together as a placeholder, the work folder
    keeps nothing of it, and a later play in full keeps it."""
    retention = world.services.deliverer.retention
    assert retention is not None
    monkeypatch.setattr(retention, "enabled", True)
    engine = world.services.engine
    song, _, audio = world.placeholder_track(f"r-kept-{step}", [world.addons[0]])
    stopped: list[str] = []

    async def stop(name: str) -> None:
        if name == f"replacement {step}":
            stopped.append(name)
            raise RuntimeError("a stop")

    engine.step = stop
    try:
        assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
        deadline = time.monotonic() + 20
        while not stopped or retention._held:
            assert time.monotonic() < deadline, "the kept stream was not handed over"
            time.sleep(0.1)
    finally:
        engine.step = None

    async def rows() -> Any:
        return await world.services.store.fetchone(
            "SELECT * FROM placeholders WHERE song_id = ?", [song]
        )

    row = call(world, rows)
    assert row["state"] == "placeholder"
    silent = engine.layout.absolute(str(row["placeholder_path"]))
    marker = tagging.marker_of(silent)
    assert marker is not None and marker.silence
    assert not list(engine.layout.staging.glob("backup-*"))
    work = world.services.download_first.work_dir
    assert [p.name for p in work.iterdir()] == []
    assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
    deadline = time.monotonic() + 20
    while call(world, rows)["state"] != "delivered":
        assert time.monotonic() < deadline, "the later play was not kept"
        time.sleep(0.1)


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
@pytest.mark.parametrize("key", ["wavpack", "wma", "wav-pcm", "matroska-flac"])
async def test_a_replacement_in_another_container_killed_at_any_step_is_put_right(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, key: str, step: str
) -> None:
    """Kept streams keep their add-on's container (APEv2, ASF, ID3 in WAVE, Matroska): its
    marker tells the start's repair which file the row names, as for the others."""
    name = f"r-replace-{key}-" + step.replace(" ", "-")
    song, folder = await one_placeholder(navidrome, state, name)
    formats = {f.key: f for f in IMPORT_FORMATS}
    sample = import_tone(tmp_path / name, formats[key], tagged=False)
    await kill_at(
        navidrome, state, swap_step("replacement", step), action="replace", song=song,
        delivered=str(sample),
    )  # fmt: skip
    async with restarted(navidrome, state) as parts:
        recorded = await assert_in_step(parts, folder)
        row = await row_of(parts, song)
        assert row["state"] == ("delivered" if step == "committed" else "placeholder")
        if row["state"] == "delivered":
            assert str(row["path"]).endswith(sample.suffix) and row["path"] in recorded
        else:
            await parts.engine.replace_with_delivered(song, sample)
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
@pytest.mark.parametrize("codec", ["aac", "flac"])
async def test_delivered_audio_put_in_one_piece_killed_at_any_step_is_put_right(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, codec: str, step: str
) -> None:
    """Delivered audio in fragments replaced by the same audio in one piece
    (``redeliver``): at the same path (AAC, in an M4A both) the file in place stays - both
    are its delivered audio -; to another (FLAC: .m4a to .flac) the file the row names, the
    other one beside it going. Either way the next start ends with one file of the song, its
    row and Navidrome in step, and the song can be swapped again."""
    key = f"r-one-piece-{codec}-" + step.replace(" ", "-")
    song, folder = await one_placeholder(navidrome, state, key)
    pieces = fragmented_mp4(tmp_path / "fragments.m4a", frequency_for(key), 3, codec)
    async with engine_for(navidrome, state) as parts:
        await parts.engine.replace_with_delivered(song, pieces)  # (as delivered before)
    whole = tmp_path / f"whole.{'flac' if codec == 'flac' else 'm4a'}"
    await anyio.run_process(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(pieces), "-map", "0:a:0", "-c",
         "copy", *(["-f", "flac"] if codec == "flac" else ["-f", "mp4"]), str(whole)],
    )  # fmt: skip
    await kill_at(
        navidrome, state, swap_step("redelivery", step), action="redeliver", song=song,
        delivered=str(whole),
    )  # fmt: skip
    async with restarted(navidrome, state) as parts:
        recorded = await assert_in_step(parts, folder)
        row = await row_of(parts, song)
        assert row["state"] == "delivered" and row["path"] in recorded
        moved = codec == "flac" and step == "committed"
        assert str(row["path"]).endswith(".flac" if moved else ".m4a")
        listed = await parts.navidrome.song(song)
        assert listed is not None and listed["path"] == row["path"]
        if not str(row["path"]).endswith(whole.suffix) or is_fragmented(
            parts.layout.absolute(str(row["path"]))
        ):
            assert await parts.engine.redeliver(song, whole, unused_since=float("inf"))
        assert await parts.engine.revert_to_placeholder(song)
        await assert_in_step(parts, folder)


@pytest.mark.parametrize("how", ["second rename", "scan", "swapped", "verified", "committed"])
@pytest.mark.parametrize("codec", ["aac", "flac"])
async def test_a_redelivery_that_fails_in_the_app_keeps_the_song_s_audio(
    navidrome: NavidromeInstance, state: Path, tmp_path: Path, codec: str, how: str
) -> None:
    """A redelivery failing in the running app - its second rename, Navidrome's scan, or a
    failure at a step: the files are put right at once from the backup, which goes only once
    Navidrome confirms the file the row names - never the backup of the only audio while its
    path is empty (at the same path the row reads the same before and after the swap)."""
    key = f"r-one-piece-fails-{codec}-" + how.replace(" ", "-")
    song, folder = await one_placeholder(navidrome, state, key)
    pieces = fragmented_mp4(tmp_path / "fragments.m4a", frequency_for(key), 3, codec)
    whole = tmp_path / f"whole.{'flac' if codec == 'flac' else 'm4a'}"
    await anyio.run_process(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(pieces), "-map", "0:a:0", "-c",
         "copy", *(["-f", "flac"] if codec == "flac" else ["-f", "mp4"]), str(whole)],
    )  # fmt: skip
    async with engine_for(navidrome, state) as parts:
        engine = parts.engine
        await engine.replace_with_delivered(song, pieces)  # (as delivered before)
        rename, scanned = engine_module.os.replace, engine._song_at
        failures = []

        def replace(src: Any, dst: Any) -> None:
            staged = Path(src).parent == parts.layout.staging
            if how == "second rename" and not failures and staged and "backup-" not in str(dst):
                failures.append(dst)
                raise OSError("a failure")
            rename(src, dst)

        async def song_at(*args: Any, **kwargs: Any) -> bool:
            if how == "scan" and not failures:
                failures.append("scan")
                raise NavidromeError("unavailable")
            return await scanned(*args, **kwargs)

        async def stop(name: str) -> None:
            if name == f"redelivery {how}":
                raise RuntimeError("a failure")

        engine_module.os.replace = replace  # type: ignore[assignment]
        engine._song_at = song_at  # type: ignore[method-assign]
        engine.step = stop
        try:
            with pytest.raises((ReplaceError, RuntimeError)) if how != "committed" else _never():
                await engine.redeliver(song, whole, unused_since=float("inf"))
        except RuntimeError:
            assert how == "committed"  # (raised after its row's update: the new file stays)
        finally:
            engine_module.os.replace = rename  # type: ignore[assignment]
            engine._song_at = scanned  # type: ignore[method-assign]
            engine.step = None
        recorded = await assert_in_step(parts, folder)
        row = await row_of(parts, song)
        assert row["state"] == "delivered" and row["path"] in recorded
        path = parts.layout.absolute(str(row["path"]))
        assert path.exists() and tagging.marker_of(path) is not None
        assert list(parts.layout.staging.glob(f"backup-{song}.*")) == []
        if how == "committed":
            assert not is_fragmented(path) and path.suffix == whole.suffix
        elif codec == "flac" or how == "second rename":
            assert is_fragmented(path)  # (the row's file, from its backup)
        # Swapped again from there, as the next sweep does.
        assert await engine.redeliver(song, whole, unused_since=float("inf"))
        now = parts.layout.absolute(str((await row_of(parts, song))["path"]))
        assert now.suffix == whole.suffix and not is_fragmented(now)
        await assert_in_step(parts, folder)


@contextmanager
def _never() -> Iterator[None]:
    yield


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
@pytest.mark.parametrize("fmt,silent_present", [("flac", False), ("m4a", False), ("m4a", True)])
async def test_a_revert_of_missing_delivered_audio_killed_never_leaves_silence_under_its_row(
    navidrome: NavidromeInstance,
    state: Path,
    tmp_path: Path,
    fmt: str,
    step: str,
    silent_present: bool,
) -> None:
    """Delivered audio whose file is gone gets its placeholder back: the row says so
    before the silent file is in place, so a stop in between leaves a placeholder's row
    (played from the add-ons) and a backup the next start puts in place - never a
    "delivered" row over the silent file."""
    key = f"r-missing-{fmt}-{step}-{silent_present}"
    song, folder = await one_placeholder(navidrome, state, key)
    async with engine_for(navidrome, state) as parts:
        row = await row_of(parts, song)
        silent = parts.layout.absolute(row["placeholder_path"]).read_bytes()
        path = await parts.engine.replace_with_delivered(song, delivered_audio(tmp_path, fmt, key))
        parts.layout.absolute(path).unlink()
        if silent_present:
            parts.layout.absolute(row["placeholder_path"]).write_bytes(silent)
    await kill_at(navidrome, state, f"revert of a missing file {step}", action="revert", song=song)
    async with restarted(navidrome, state) as parts:
        row = await row_of(parts, song)
        if step == "prepared" and not silent_present:
            # With no silent file already in place, the expiry's next look does it.
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


# --- music imports: the copy that joins an album, and the link to a library song -------


class _Stopped(BaseException):
    """A stop between two steps (the worker's own interruption path)."""


async def _restart(w: Any) -> None:
    """Close the import service and open a new one over the same state and root."""
    from shijhon.music_imports import MusicImports

    old = w.imports
    await old.aclose()
    w.imports = MusicImports(
        old.store,
        old.access,
        library_id=old.library_id,
        library_path=old.storage.library,
        limits=old.limits,
        scan=old.scan,
        native_lookup=old.native_lookup,
        refresh=old.refresh,
        native_song=old.native_song,
    )
    await w.imports.initialize()


async def _finish(w: Any, name: str, job_id: str, state: str) -> dict[str, Any]:
    from tests.acceptance.test_music_import_overlap import settle

    owner = w.users[name][0]
    status = await w.imports.status(owner, job_id)
    assert status["state"] == "paused" and status["error"] == "interrupted", status
    await w.imports.resume(owner, job_id)
    return await settle(w, name, job_id, state)


def _incoming(w: Any) -> set[str]:
    return {
        p.name for p in (w.nd.music / "_imports" / ".incoming").iterdir() if p.name != ".ndignore"
    }


@pytest.mark.parametrize("when", ["planned", "after-copy", "in-transaction"])
async def test_an_import_copy_stopped_at_any_step_is_made_once(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """A copy is recorded, with its reservation, before its bytes are written: a stop at
    any step leaves a record of every copy file there is, and the import ends with one
    copy."""
    from shijhon.music_imports import service as service_module
    from tests.acceptance.test_music_import_overlap import LANTERNS, LIBRARY, world

    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    dated = write_album(
        source,
        dataclasses.replace(LIBRARY, tracks=LANTERNS[4:5], release_date="2020-03-01", folder="D"),
    )
    job = await w.imports.create(ben, "copy-stop")
    from shijhon.music_imports import ManifestEntry

    job = await w.imports.declare(
        ben, job["id"], "page", [ManifestEntry("f0", "D/" + dated[0].name, dated[0].stat().st_size)]
    )
    await w.imports.seal(ben, job["id"])
    data = dated[0].read_bytes()

    async def stream() -> Any:
        yield data

    await w.imports.receive(ben, job["id"], job["items"][0]["id"], 0, stream())
    await w.imports.review(ben, job["id"])
    if when == "planned":

        async def stop_before(self: Any, *args: Any) -> Any:
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_retag", stop_before)
    elif when == "after-copy":
        made = service_module.MusicImports._retag

        async def stop_after(self: Any, *args: Any) -> Any:
            await made(self, *args)
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_retag", stop_after)
    else:
        copied = service_module.MusicImports._copied

        async def stop_in(self: Any, *args: Any) -> None:
            await copied(self, *args)
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_copied", stop_in)
    with pytest.raises(_Stopped):
        await w.imports.tick()
    monkeypatch.undo()
    # Every copy file there is has its record, reserving its storage.
    [planned] = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from<>''")
    assert planned["state"] == "discarding" and planned["reserved_size"] > 0
    original = await w.store.fetchone("SELECT * FROM music_import_items WHERE derived_from=''")
    assert original["state"] == "copying"
    assert _incoming(w) <= {original["staging_name"], planned["staging_name"]}
    assert (planned["staging_name"] in _incoming(w)) == (when != "planned")
    await _restart(w)
    assert not await w.store.fetchone("SELECT 1 FROM music_import_items WHERE derived_from<>''")
    assert _incoming(w) == {original["staging_name"]}
    review = await _finish(w, "ben", job["id"], "review")
    [item] = review["items"]
    assert item["outcome"] == "copy"
    copies = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from<>''")
    assert len(copies) == 1
    original = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert _incoming(w) == {original["staging_name"], copies[0]["staging_name"]}
    from tests.acceptance.test_music_import_overlap import approve

    done = await approve(w, "ben", review)
    assert done["complete_files"] == 1
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("when", ["in-transaction", "after-commit"])
async def test_an_import_link_stopped_at_any_step_links_once(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    from shijhon.music_imports import service as service_module
    from tests.acceptance.test_music_import_overlap import LANTERNS, LIBRARY, settle, upload, world

    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    files = write_album(
        source, dataclasses.replace(LIBRARY, tracks=LANTERNS[:1], fmt="mp3", folder="S")
    )
    review = await upload(w, "ben", files, source, "link-stop")
    assert [i["outcome"] for i in review["items"]] == ["linked"]
    await w.imports.approve(ben, review["id"], review["revision"], "go")
    if when == "in-transaction":
        credit = service_module.record_link_credit

        async def stop_in(*args: Any, **kwargs: Any) -> bool:
            await credit(*args, **kwargs)
            raise _Stopped

        monkeypatch.setattr(service_module, "record_link_credit", stop_in)
        with pytest.raises(_Stopped):
            await w.imports.tick()
        assert await w.store.fetchone("SELECT 1 FROM music_import_links") is None
        assert (
            await w.store.fetchone(
                "SELECT 1 FROM collection_originals WHERE provenance_id GLOB 'link:*'"
            )
            is None
        )
    else:
        assert await w.imports.tick()  # the link commits

        async def stop(*args: Any) -> None:
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_release_linked", stop)
        with pytest.raises(_Stopped):
            await w.imports.tick()
        [row] = await w.store.fetchall("SELECT * FROM music_import_items")
        assert row["state"] == "linked" and row["reserved_size"] > 0
    monkeypatch.undo()
    await _restart(w)
    await w.imports.resume(ben, review["id"])
    done = await settle(w, "ben", review["id"], "done")
    assert done["linked_files"] == 1
    assert len(await w.store.fetchall("SELECT * FROM music_import_links")) == 1
    credits = await w.store.fetchall(
        "SELECT * FROM collection_originals WHERE provenance_id GLOB 'link:*'"
    )
    assert len(credits) == 1
    [row] = await w.store.fetchall("SELECT * FROM music_import_items")
    assert row["reserved_size"] == 0 and row["staging_name"] not in _incoming(w)
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("when", ["in-transaction", "after-commit"])
async def test_an_import_sent_back_to_review_when_stopped_at_any_step_is_reviewed_once(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """Another import made the reviewed plan wrong (a second album of one name): the
    import goes back to review once, whatever the stop."""
    from shijhon.music_imports import service as service_module
    from tests.acceptance.test_music_import_overlap import (
        LANTERNS,
        album_songs,
        approve,
        settle,
        upload,
        world,
    )

    nd = navidrome_factory()
    w = await world(nd, tmp_path, ("ana", "ben"))
    album = Album("Glass Orchard", "Paper Garden", LANTERNS[:4], folder="Paper Garden")
    first = write_album(
        tmp_path / "ana",
        dataclasses.replace(album, tracks=LANTERNS[:2], release_date="2020-01-01"),
    )
    second = write_album(
        tmp_path / "ben",
        dataclasses.replace(album, tracks=LANTERNS[2:4], release_date="2020-06-01"),
    )
    review_a = await upload(w, "ana", first, tmp_path / "ana", "ana-import")
    review_b = await upload(w, "ben", second, tmp_path / "ben", "ben-import")
    await approve(w, "ana", review_a)
    ben = w.users["ben"][0]
    await w.imports.approve(ben, review_b["id"], review_b["revision"], "go-b")
    if when == "in-transaction":
        reset = service_module.MusicImports._reset_overlap

        async def stop_in(self: Any, conn: Any, job_id: str) -> None:
            await reset(self, conn, job_id)
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_reset_overlap", stop_in)
    else:
        back = service_module.MusicImports._back_to_review

        async def stop_after(self: Any, *args: Any) -> None:
            await back(self, *args)
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_back_to_review", stop_after)
    with pytest.raises(_Stopped):
        for _ in range(20):
            await w.imports.tick()
    monkeypatch.undo()
    job = await w.store.fetchone("SELECT * FROM music_import_jobs WHERE id=?", [review_b["id"]])
    assert (job["approved_revision"] is None) == (when == "after-commit")
    assert not await w.store.fetchone(
        "SELECT 1 FROM music_import_items WHERE job_id=? AND state<>'eligible'", [review_b["id"]]
    )
    await _restart(w)
    await w.imports.resume(ben, review_b["id"])
    again = await settle(w, "ben", review_b["id"], "review")
    assert {i["outcome"] for i in again["items"]} == {"copy"}
    await approve(w, "ben", again)
    songs = await album_songs(w, "ben", "Paper Garden")
    assert sorted(s["title"] for s in songs) == [f"Lantern {n}" for n in range(1, 5)]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("finished", ["at the start", "when checked", "when idle"])
async def test_copies_a_new_review_gives_up_keep_their_reservation_until_deleted(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finished: str,
) -> None:
    """Reviewing again gives up the review's copies: a stop after that is recorded, before
    their private bytes are deleted, leaves each copy's row and reservation until its bytes
    are gone - finished at the next start, before the import is checked again, or by the
    idle worker for an import stopped meanwhile."""
    from tests.acceptance.test_music_import_overlap import LANTERNS, LIBRARY, settle, upload, world

    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    dated = write_album(
        source,
        dataclasses.replace(LIBRARY, tracks=LANTERNS[4:6], release_date="2020-03-01", folder="D"),
    )
    review = await upload(w, "ben", dated, source, "copies-stop")
    assert [i["outcome"] for i in review["items"]] == ["copy", "copy"]
    copies = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from<>''")
    reserved = await w.store.fetchone("SELECT sum(reserved_size) AS n FROM music_import_items")

    def stop(name: str) -> None:
        raise _Stopped

    monkeypatch.setattr(w.imports.storage, "discard_attempt", stop)
    with pytest.raises(_Stopped):
        await w.imports.review(ben, review["id"], exclude=[review["items"][0]["id"]])
    monkeypatch.undo()
    left = await w.store.fetchall(
        "SELECT * FROM music_import_items WHERE derived_from<>'' ORDER BY id"
    )
    assert [r["state"] for r in left] == ["discarding", "discarding"]
    assert sorted(r["reserved_size"] for r in left) == sorted(c["reserved_size"] for c in copies)
    assert (await w.store.fetchone("SELECT sum(reserved_size) AS n FROM music_import_items"))[
        "n"
    ] == reserved["n"]
    assert {c["staging_name"] for c in copies} <= _incoming(w)
    if finished == "at the start":
        await _restart(w)
        assert not await w.store.fetchone("SELECT 1 FROM music_import_items WHERE derived_from<>''")
        assert not {c["staging_name"] for c in copies} & _incoming(w)
        again = await _finish(w, "ben", review["id"], "review")
    elif finished == "when checked":
        again = await settle(w, "ben", review["id"], "review")
    else:
        await w.imports.cancel(ben, review["id"])
        while await w.imports.tick():
            pass
        assert not await w.store.fetchone("SELECT 1 FROM music_import_items WHERE derived_from<>''")
        assert not {c["staging_name"] for c in copies} & _incoming(w)
        await w.caller.aclose()
        await w.imports.aclose()
        await w.store.close()
        return
    assert sorted(i["state"] for i in again["items"]) == ["excluded", "joined"]
    [copy] = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from<>''")
    originals = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from=''")
    assert _incoming(w) == {copy["staging_name"], *(o["staging_name"] for o in originals)}
    total = await w.store.fetchone("SELECT sum(reserved_size) AS n FROM music_import_items")
    assert total["n"] == copy["reserved_size"] + sum(o["reserved_size"] for o in originals)
    assert again["copy_files"] == 1
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("finished", ["at the start", "when checked", "when idle"])
async def test_a_copy_stopped_while_made_is_deleted_also_when_its_file_is_excluded(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finished: str,
) -> None:
    """A stop after a copy's bytes are written, before it is complete; the import resumed
    and reviewed again without that file (no longer to be copied); a stop before the
    copy's bytes are deleted: its record keeps the copy and its reservation until its bytes
    are gone - at the next start, before the import is checked again, or by the idle
    worker for an import stopped meanwhile. No private file is left without a record."""
    from shijhon.music_imports import ManifestEntry
    from shijhon.music_imports import service as service_module
    from tests.acceptance.test_music_import_overlap import LANTERNS, LIBRARY, settle, world

    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    dated = write_album(
        source,
        dataclasses.replace(LIBRARY, tracks=LANTERNS[4:5], release_date="2020-03-01", folder="D"),
    )
    job = await w.imports.create(ben, "copy-excluded")
    job = await w.imports.declare(
        ben, job["id"], "page", [ManifestEntry("f0", "D/" + dated[0].name, dated[0].stat().st_size)]
    )
    await w.imports.seal(ben, job["id"])
    data = dated[0].read_bytes()

    async def stream() -> Any:
        yield data

    item_id = job["items"][0]["id"]
    await w.imports.receive(ben, job["id"], item_id, 0, stream())
    await w.imports.review(ben, job["id"])
    made = service_module.MusicImports._retag

    async def stop_after(self: Any, *args: Any) -> Any:
        await made(self, *args)
        raise _Stopped

    monkeypatch.setattr(service_module.MusicImports, "_retag", stop_after)
    with pytest.raises(_Stopped):
        await w.imports.tick()
    monkeypatch.undo()
    [copy] = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from<>''")
    assert copy["staging_name"] in _incoming(w)
    await w.imports.resume(ben, job["id"])

    def stop(name: str) -> None:
        raise _Stopped

    monkeypatch.setattr(w.imports.storage, "discard_attempt", stop)
    with pytest.raises(_Stopped):
        await w.imports.review(ben, job["id"], exclude=[item_id])
    monkeypatch.undo()
    [left] = await w.store.fetchall("SELECT * FROM music_import_items WHERE derived_from<>''")
    assert left["id"] == copy["id"] and left["state"] == "discarding"
    assert left["reserved_size"] == copy["reserved_size"] > 0
    original = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item_id])
    assert original["state"] == "received" and original["review_excluded"] == 1
    assert _incoming(w) == {original["staging_name"], copy["staging_name"]}
    if finished == "at the start":
        await _restart(w)
        again = await _finish(w, "ben", job["id"], "review")
    elif finished == "when checked":
        again = await settle(w, "ben", job["id"], "review")
    else:
        await w.imports.cancel(ben, job["id"])
        kept = await w.store.fetchone(
            "SELECT reserved_size FROM music_import_items WHERE id=?", [copy["id"]]
        )
        assert kept["reserved_size"] == copy["reserved_size"]  # (until its bytes are gone)
        while await w.imports.tick():
            pass
        again = await w.imports.status(ben, job["id"])
    assert not await w.store.fetchone("SELECT 1 FROM music_import_items WHERE derived_from<>''")
    assert _incoming(w) == ({original["staging_name"]} if finished != "when idle" else set())
    if finished != "when idle":
        assert [i["state"] for i in again["items"]] == ["excluded"]
        assert again["copy_files"] == 0
    total = await w.store.fetchone("SELECT sum(reserved_size) AS n FROM music_import_items")
    kept = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item_id])
    assert total["n"] == kept["reserved_size"]
    if finished == "when idle":
        assert kept["received_cleanup"] == "deleted" and kept["reserved_size"] == 0
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("when", ["probed", "compared"])
async def test_a_legacy_approved_import_stopped_at_any_step_is_compared_once(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    from shijhon.music_imports import service as service_module
    from tests.acceptance.test_music_import_overlap import (
        LANTERNS,
        LIBRARY,
        album_songs,
        approve,
        upload,
        world,
    )

    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    source = tmp_path / "ben"
    files = write_album(
        source, dataclasses.replace(LIBRARY, tracks=LANTERNS[:2], fmt="mp3", folder="S")
    )
    review = await upload(w, "ben", files, source, "legacy")
    await w.store.execute(
        "UPDATE music_import_items SET state='eligible',facts='',outcome='',match=''"
    )
    await w.store.execute(
        "UPDATE music_import_jobs SET state='approved',approved_revision=revision,"
        "approval_key='old'"
    )
    if when == "probed":
        probe = service_module.MusicImports._probe
        calls = 0

        async def stop_after(self: Any, item: Any) -> Any:
            nonlocal calls
            calls += 1
            found = await probe(self, item)
            if calls == 2:
                raise _Stopped
            return found

        monkeypatch.setattr(service_module.MusicImports, "_probe", stop_after)
    else:
        decide = service_module.MusicImports._decide

        async def stop_in(self: Any, *args: Any, **kwargs: Any) -> Any:
            await decide(self, *args, **kwargs)
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_decide", stop_in)
    with pytest.raises(_Stopped):
        for _ in range(10):
            await w.imports.tick()
    monkeypatch.undo()
    rows = await w.store.fetchall("SELECT * FROM music_import_items")
    assert all(r["state"] == "eligible" and r["outcome"] == "" for r in rows)
    await _restart(w)
    again = await _finish(w, "ben", review["id"], "review")
    assert {i["outcome"] for i in again["items"]} == {"linked"}
    done = await approve(w, "ben", again)
    assert done["linked_files"] == 2 and len(await album_songs(w, "ben")) == 4
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("when", ["stopped", "deleted"])
async def test_a_stopped_imports_linked_files_keep_their_reservation_until_deleted(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    from shijhon.music_imports import service as service_module
    from tests.acceptance.test_music_import_overlap import LANTERNS, LIBRARY, upload, world

    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "ben"
    files = write_album(
        source, dataclasses.replace(LIBRARY, tracks=LANTERNS[:1], fmt="mp3", folder="S")
    )
    review = await upload(w, "ben", files, source, "link-cancel")
    await w.imports.approve(ben, review["id"], review["revision"], "go")
    assert await w.imports.tick()  # the link commits; its bytes are still there
    await w.imports.cancel(ben, review["id"])
    [row] = await w.store.fetchall("SELECT * FROM music_import_items")
    assert row["state"] == "linked" and row["reserved_size"] == row["received_size"] > 0
    assert row["staging_name"] in _incoming(w)
    if when == "deleted":
        discard = w.imports.storage.discard_attempt

        def stop_after(name: str) -> None:
            discard(name)
            raise _Stopped

        monkeypatch.setattr(w.imports.storage, "discard_attempt", stop_after)
        with pytest.raises(_Stopped):
            await w.imports.tick()
        monkeypatch.undo()
        [row] = await w.store.fetchall("SELECT * FROM music_import_items")
        assert row["reserved_size"] > 0 and row["staging_name"] not in _incoming(w)
    await _restart(w)
    while await w.imports.tick():
        pass
    [row] = await w.store.fetchall("SELECT * FROM music_import_items")
    assert row["reserved_size"] == 0 and row["staging_name"] not in _incoming(w)
    await w.imports.aclose()
    await w.store.close()
    del service_module


# --- music imports: an import's audio in catalog songs, and upgrades of own imports ---------


@pytest.mark.parametrize("terminal", ["done", "cancelled"])
@pytest.mark.parametrize("when", ["before intent", "intent", "unlinked", "synced", "released"])
async def test_unpublished_import_cleanup_stopped_at_any_step_frees_only_unused_bytes(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
    when: str,
) -> None:
    from tests.acceptance.test_music_import_overlap import LIBRARY, settle, upload, world

    nd = navidrome_factory()
    w = await world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    source = tmp_path / "source"
    files = write_album(source, dataclasses.replace(LIBRARY, tracks=LIBRARY.tracks[:2]))
    review = await upload(w, "ben", files, source, "unused-stop")
    excluded = review["items"][0]["id"]
    await w.imports.review(ben, review["id"], exclude=[excluded])
    review = await settle(w, "ben", review["id"], "review")
    await w.imports.approve(ben, review["id"], review["revision"], "go")
    if terminal == "cancelled":
        credit = w.imports._credit

        async def stop_after_credit(*args: Any) -> None:
            await credit(*args)
            await w.imports.cancel(ben, review["id"])

        monkeypatch.setattr(w.imports, "_credit", stop_after_credit)
    assert await w.imports.tick()
    monkeypatch.undo()
    assert (await w.imports.status(ben, review["id"]))["state"] == terminal
    [published] = await w.store.fetchall("SELECT * FROM music_import_items WHERE state='complete'")
    unused = await w.imports._item(review["id"], excluded)
    receipt = await w.store.fetchall("SELECT * FROM collection_originals")
    retained = (nd.music / "_imports/.incoming" / published["staging_name"]).read_bytes()
    incoming = nd.music / "_imports/.incoming" / unused["staging_name"]

    if when in ("before intent", "released"):
        execute = w.store.execute

        async def stop_at_record(sql: str, params: Any = ()) -> Any:
            if when == "before intent" and "SET received_cleanup='pending'" in sql:
                raise _Stopped
            result = await execute(sql, params)
            if (
                when == "released"
                and "SET received_cleanup='deleted'" in sql
                and "WHERE id=? AND received_cleanup='pending'" in sql
            ):
                raise _Stopped
            return result

        monkeypatch.setattr(w.store, "execute", stop_at_record)
    elif when == "unlinked":
        fsync = os.fsync

        def stop_at_sync(fd: int) -> None:
            if fd == w.imports.storage.incoming and not incoming.exists():
                raise _Stopped
            fsync(fd)

        monkeypatch.setattr(os, "fsync", stop_at_sync)
    else:
        discard = w.imports.storage.discard_unpublished

        def stop_at_delete(name: str) -> None:
            if when == "synced":
                discard(name)
            raise _Stopped

        monkeypatch.setattr(w.imports.storage, "discard_unpublished", stop_at_delete)
    with pytest.raises(_Stopped):
        await w.imports.tick()
    monkeypatch.undo()
    interrupted = await w.imports._item(review["id"], excluded)
    assert interrupted["received_cleanup"] == (
        "" if when == "before intent" else "deleted" if when == "released" else "pending"
    )
    assert interrupted["reserved_size"] == (0 if when == "released" else unused["reserved_size"])
    assert incoming.exists() == (when in ("before intent", "intent"))

    await _restart(w)
    while await w.imports.tick():
        pass
    cleaned = await w.imports._item(review["id"], excluded)
    assert cleaned["received_cleanup"] == "deleted" and cleaned["reserved_size"] == 0
    assert cleaned["state"] == "excluded" and cleaned["received_size"] == unused["received_size"]
    assert not incoming.exists()
    assert _incoming(w) == {published["staging_name"]}
    assert (nd.music / "_imports" / published["final_name"]).read_bytes() == retained
    assert (nd.music / "_imports/.incoming" / published["staging_name"]).read_bytes() == retained
    assert [dict(r) for r in await w.store.fetchall("SELECT * FROM collection_originals")] == [
        dict(r) for r in receipt
    ]
    native = await w.imports.native_lookup(ben, "1", "_imports/" + published["final_name"])
    assert native and native.song_id == published["song_id"]
    assert len(await w.caller.songs(ben, "1", 0, 10)) == 1
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def _audio_ready(nd: Any, tmp_path: Path) -> tuple[Any, PlaceholderEngine, str, Any]:
    """A catalog song and Ben's approved import of it, its audio still to go in."""
    from tests.acceptance.test_music_import_audio import (
        RELEASE,
        approve,
        audio_world,
        refresh_all,
        tracks_of,
        upload,
    )

    w, engine = await audio_world(nd, tmp_path, ("ben",))
    release = dataclasses.replace(RELEASE, tracks=RELEASE.tracks[:1])
    made = await engine.materialize(release)
    [song] = made.created.values()
    await refresh_all(w)
    source = tmp_path / "ben"
    files = write_album(source, dataclasses.replace(tracks_of(release, "mp3")))
    review = await upload(w, "ben", files, source, "audio-stop")
    done = await approve(w, "ben", review)
    return w, engine, song, done


async def _repaired(engine: PlaceholderEngine) -> None:
    """The engine's repair of swaps a stop interrupted, as the next start does it."""
    for song in engine.interrupted():
        await engine.recover_swap(song)


@pytest.mark.parametrize(
    "when", ["replacement swapped", "replacement verified", "replacement committed", "recorded"]
)
async def test_an_imports_audio_stopped_at_any_step_goes_into_its_catalog_song_once(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    from shijhon.music_imports import service as service_module
    from tests.acceptance.test_music_import_audio import drain

    nd = navidrome_factory()
    w, engine, song, _ = await _audio_ready(nd, tmp_path)
    if when == "recorded":

        async def stop(*args: Any) -> None:
            raise _Stopped

        monkeypatch.setattr(service_module.MusicImports, "_audio_is", stop)
    else:

        async def step(at: str) -> None:
            if at == when:
                raise _Stopped

        engine.step = step
    with pytest.raises(_Stopped):
        await drain(w)
    monkeypatch.undo()
    engine.step = None
    await _repaired(engine)
    await drain(w)
    [row] = await w.store.fetchall("SELECT * FROM placeholders")
    [link] = await w.store.fetchall("SELECT * FROM music_import_links")
    assert row["song_id"] == song and row["state"] == "delivered"
    assert row["import_item"] == link["item_id"] and link["audio"] == "placed"
    folder = (nd.music / row["path"]).parent
    assert sorted(p.suffix for p in folder.iterdir()) == [".mp3"]  # no other file of it
    assert not engine.interrupted()
    native = await w.call("ben", "getSong", {"id": song})
    assert native["song"]["suffix"] == "mp3"
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("left", ["pending", "low_quality"])
async def test_an_imports_audio_in_place_before_its_record_stays_its_own_when_the_minimum_rises(
    navidrome_factory: NavidromeFactory, tmp_path: Path, left: str
) -> None:
    """A stop after the engine put the import's audio in but before the link recorded it,
    then the importer's minimum rises above the file: the song keeps playing the import's
    audio, so the link records it as placed and keeps its copy reserved - and the import's
    removal takes it out again. ``low_quality``: such a link without its reservation is
    put right too."""
    from shijhon.quality import Minimum
    from tests.acceptance.test_music_import_audio import advance, drain

    nd = navidrome_factory()
    w, engine, song, done = await _audio_ready(nd, tmp_path)
    ben = w.users["ben"][0]

    async def step(at: str) -> None:
        if at == "replacement committed":
            raise _Stopped

    engine.step = step
    with pytest.raises(_Stopped):
        await drain(w)
    engine.step = None
    [link] = await w.store.fetchall("SELECT * FROM music_import_links")
    [row] = await w.store.fetchall("SELECT * FROM placeholders")
    assert link["audio"] == "pending" and link["reserved"] > 0
    assert row["state"] == "delivered" and row["import_item"] == link["item_id"]
    if left == "low_quality":
        async with w.store.transaction() as conn:
            await conn.execute(
                "UPDATE music_import_items SET reserved_size=reserved_size-? WHERE id=?",
                [link["reserved"], link["item_id"]],
            )
            await conn.execute("UPDATE music_import_links SET audio='low_quality',reserved=0")

    async def lossless_only(user_id: str) -> Minimum:
        return Minimum(kbps=0, lossless_only=True)

    w.imports.minimum = lossless_only  # the import's MP3 is below it now
    await _repaired(engine)
    await drain(w)
    [link] = await w.store.fetchall("SELECT * FROM music_import_links")
    [item] = await w.store.fetchall(
        "SELECT * FROM music_import_items WHERE id=?", [link["item_id"]]
    )
    assert link["audio"] == "placed" and link["reserved"] > 0
    assert item["reserved_size"] == item["received_size"] + link["reserved"]
    [row] = await w.store.fetchall("SELECT * FROM placeholders")
    assert row["state"] == "delivered" and row["import_item"] == link["item_id"]
    # Removing the import takes its audio out of the catalog song, then its reservation.
    await w.removals.review(ben, done["id"])
    status = await advance(w.removals, ben, done["id"], "review")
    await w.removals.approve(ben, done["id"], status["fingerprint"], "remove")
    await advance(w.removals, ben, done["id"], "removed")
    await drain(w)
    [row] = await w.store.fetchall("SELECT * FROM placeholders")
    [link] = await w.store.fetchall("SELECT * FROM music_import_links")
    assert row["state"] == "placeholder" and row["import_item"] is None
    assert link["active"] == 0 and link["audio"] == "pending" and link["reserved"] == 0
    assert (await w.call("ben", "getSong", {"id": song}))["song"]["suffix"] == "flac"
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("when", ["revert swapped", "revert verified", "revert committed"])
async def test_a_removed_imports_audio_stopped_at_any_step_leaves_its_catalog_song(
    navidrome_factory: NavidromeFactory, tmp_path: Path, when: str
) -> None:
    from tests.acceptance.test_music_import_audio import advance, drain

    nd = navidrome_factory()
    w, engine, song, done = await _audio_ready(nd, tmp_path)
    ben = w.users["ben"][0]
    await drain(w)
    await w.removals.review(ben, done["id"])
    status = await advance(w.removals, ben, done["id"], "review")
    await w.removals.approve(ben, done["id"], status["fingerprint"], "remove")
    await advance(w.removals, ben, done["id"], "removed")

    async def step(at: str) -> None:
        if at == when:
            raise _Stopped

    engine.step = step
    with pytest.raises(_Stopped):
        await drain(w)
    engine.step = None
    await _repaired(engine)
    await drain(w)
    [row] = await w.store.fetchall("SELECT * FROM placeholders")
    [link] = await w.store.fetchall("SELECT * FROM music_import_links")
    assert row["state"] == "placeholder" and row["import_item"] is None
    assert link["active"] == 0 and link["audio"] == "pending"
    assert (await w.call("ben", "getSong", {"id": song}))["song"]["suffix"] == "flac"
    await w.removals.restore(ben, done["id"])
    await advance(w.removals, ben, done["id"], "restored")
    await drain(w)
    [row] = await w.store.fetchall("SELECT * FROM placeholders")
    assert row["state"] == "delivered" and row["import_item"] == link["item_id"]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("when", ["swapped", "verified", "committed"])
async def test_an_upgrade_stopped_at_any_step_keeps_one_song(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    from shijhon.music_imports import upgrade as upgrade_module
    from shijhon.music_imports.upgrade import Upgrades, file_quality
    from shijhon.quality import DEFAULT
    from tests.acceptance.test_music_import_audio import audio_world, own_import, refresh_all

    nd = navidrome_factory()
    w, _ = await audio_world(nd, tmp_path, ("ana",), minimum=DEFAULT)
    ana = w.users["ana"][0]
    album = Album("Quiet Hollow", "Salt Lines", (Track("Brine", 1, seconds=4.0),), fmt="mp3",
                  folder="Mine")  # fmt: skip
    done, _ = await own_import(w, tmp_path, "ana", album)
    [item] = done["items"]
    song = item["song_id"]
    await refresh_all(w)
    await w.call("ana", "star", {"id": song})
    fetched = write_album(tmp_path / "addon", dataclasses.replace(album, fmt="flac", folder="A"))[0]
    got = file_quality(fetched)

    # A hard stop: nothing after the step runs, not even the roll back.
    async def gone(*args: Any) -> None:
        raise _Stopped

    if when == "swapped":
        swap = Upgrades._swap_files

        def swapped(self: Any, *args: Any) -> None:
            swap(self, *args)
            raise _Stopped

        monkeypatch.setattr(Upgrades, "_swap_files", swapped)
    elif when == "verified":
        monkeypatch.setattr(Upgrades, "_commit", gone)
    else:
        commit = Upgrades._commit

        async def committed(self: Any, *args: Any) -> None:
            await commit(self, *args)
            raise _Stopped

        monkeypatch.setattr(Upgrades, "_commit", committed)
    monkeypatch.setattr(Upgrades, "_roll_back", gone)
    with pytest.raises(_Stopped):
        await w.imports.upgrades.upgrade(ana, song, fetched, got)
    monkeypatch.undo()
    assert upgrade_module.Upgrades._roll_back is not gone
    # The next start: the worker rolls back an upgrade it did not finish.
    while await w.imports.tick():
        pass
    row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert row["upgrade"] == ""
    incoming = {p.name for p in (nd.music / "_imports" / ".incoming").iterdir()}
    detail = (await w.call("ana", "getSong", {"id": song}))["song"]
    assert detail["id"] == song and detail.get("starred")
    if when == "committed":
        assert row["final_name"].endswith(".flac") and detail["suffix"] == "flac"
        assert json.loads(row["upgraded_from"])["staging"] in incoming
    else:
        assert row["final_name"].endswith(".mp3") and detail["suffix"] == "mp3"
        assert row["upgraded_from"] == "" and incoming == {row["staging_name"], ".ndignore"}
        assert not (nd.music / "_imports" / row["final_name"]).with_suffix(".flac").exists()
        # It can be upgraded after all.
        assert await w.imports.upgrades.upgrade(ana, song, fetched, got) == ""
        assert (await w.call("ana", "getSong", {"id": song}))["song"]["suffix"] == "flac"
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize(
    "when",
    ["reserved", "carried", "same format before the swap", "same format within the swap",
     "roll back unscanned", "rollback restored before record",
     "new format within the swap", "new format rollback before record"],
)  # fmt: skip
async def test_an_upgrade_stopped_before_or_within_its_swap_is_undone_once(
    navidrome_factory: NavidromeFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """Before its bytes exist, while they are made, and during a swap of a file of the same
    format (the upgraded MP3 at the imported MP3's own name): recovery puts the imported
    file back (or keeps it), releases what the upgrade reserved and leaves no bytes. A
    roll back whose scan did not happen is finished by a later one."""
    from shijhon.music_imports import upgrade as upgrade_module
    from shijhon.music_imports.upgrade import Upgrades, file_quality
    from shijhon.quality import DEFAULT
    from tests.acceptance.test_music_import_audio import audio_world, own_import, refresh_all

    nd = navidrome_factory()
    w, _ = await audio_world(nd, tmp_path, ("ana",), minimum=DEFAULT)
    ana = w.users["ana"][0]
    album = Album("Quiet Hollow", "Salt Lines", (Track("Brine", 1, seconds=4.0),), fmt="mp3",
                  folder="Mine")  # fmt: skip
    done, _ = await own_import(w, tmp_path, "ana", album)
    [item] = done["items"]
    song = item["song_id"]
    await refresh_all(w)
    before = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    fetched = tmp_path / "addon.mp3"  # the add-ons' audio: an MP3 at a higher bitrate
    await anyio.run_process(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=44100", "-t", "4", "-c:a", "libmp3lame", "-b:a",
         "320k", str(fetched)],
    )  # fmt: skip
    if when.startswith("new format"):
        fetched = write_album(
            tmp_path / "addon", dataclasses.replace(album, fmt="flac", folder="A")
        )[0]
    got = file_quality(fetched)

    async def gone(*args: Any) -> None:
        raise _Stopped

    if when == "reserved":
        monkeypatch.setattr(Upgrades, "_carry", gone)
    elif when == "carried":
        carry = Upgrades._carry

        async def carried(self: Any, *args: Any) -> Any:
            await carry(self, *args)
            raise _Stopped

        monkeypatch.setattr(Upgrades, "_carry", carried)
    elif when == "same format before the swap":

        def stopped(*args: Any) -> None:  # the swap runs in a worker thread
            raise _Stopped

        monkeypatch.setattr(Upgrades, "_swap_files", stopped)
    elif when.startswith("new format"):

        def before_unlink(*args: Any, **kwargs: Any) -> bool:
            # The distinct upgraded path exists; the original link has not gone.
            raise _Stopped

        monkeypatch.setattr(upgrade_module, "_unlink_ours", before_unlink)
    elif when == "same format within the swap":
        unlink = upgrade_module._unlink_ours

        def within(*args: Any, **kwargs: Any) -> bool:
            unlink(*args, **kwargs)
            raise _Stopped

        monkeypatch.setattr(upgrade_module, "_unlink_ours", within)
    else:
        swap = Upgrades._swap_files

        def swapped(self: Any, *args: Any) -> None:
            swap(self, *args)
            raise _Stopped

        monkeypatch.setattr(Upgrades, "_swap_files", swapped)
    monkeypatch.setattr(Upgrades, "_roll_back", gone)
    with pytest.raises(_Stopped):
        await w.imports.upgrades.upgrade(ana, song, fetched, got)
    monkeypatch.undo()
    row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert row["upgrade"] and row["reserved_size"] > before["reserved_size"]  # reserved first
    if when.startswith("new format"):
        intent = json.loads(row["upgrade"])
        assert (nd.music / "_imports" / row["final_name"]).exists()
        assert (nd.music / "_imports" / intent["final"]).exists()
        await w.imports.scan(["_imports"])  # Native may observe both paths before recovery.
    if when in ("rollback restored before record", "new format rollback before record"):
        from shijhon.music_imports.storage import Storage

        discard = Storage.discard_attempt

        def restored(self: Any, name: str) -> None:
            discard(self, name)
            raise _Stopped

        monkeypatch.setattr(Storage, "discard_attempt", restored)
        with pytest.raises(_Stopped):
            await w.imports.upgrades.recover()
        monkeypatch.undo()
        row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
        assert json.loads(row["upgrade"])["verify"] is True
    if when in (
        "roll back unscanned",
        "rollback restored before record",
        "new format within the swap",
        "new format rollback before record",
    ):
        scan = w.imports.scan

        async def unscanned(folders: Any) -> None:
            raise OSError("the scan did not run")

        w.imports.scan = unscanned
        assert await w.imports.upgrades.recover() == 0  # the files went back; not verified yet
        row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
        assert json.loads(row["upgrade"]) == {"phase": "verify"}
        assert row["reserved_size"] == before["reserved_size"]
        w.imports.scan = scan
    while await w.imports.tick():
        pass
    row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert row["upgrade"] == "" and row["reserved_size"] == before["reserved_size"]
    assert row["final_name"] == before["final_name"] and row["digest"] == before["digest"]
    incoming = {p.name for p in (nd.music / "_imports" / ".incoming").iterdir()}
    assert incoming == {row["staging_name"], ".ndignore"}
    detail = (await w.call("ana", "getSong", {"id": song}))["song"]
    assert detail["id"] == song and detail["size"] == before["expected_size"]
    if when.startswith("new format"):
        # Native retains the replacement's separate missing ID after observing both
        # paths. Reusing it must refuse rather than replace the import's original ID.
        assert await w.imports.upgrades.upgrade(ana, song, fetched, got) == (
            "Navidrome did not keep the song's identity for the upgraded file"
        )
        # The original container can still be upgraded after the verified rollback.
        fetched = tmp_path / "addon.mp3"
        got = file_quality(fetched)
    assert await w.imports.upgrades.upgrade(ana, song, fetched, got) == ""
    row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [item["id"]])
    assert row["reserved_size"] == before["reserved_size"] + row["expected_size"]
    detail = (await w.call("ana", "getSong", {"id": song}))["song"]
    assert detail["id"] == song and detail["bitRate"] >= 300
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


@pytest.mark.parametrize("revoke_at", ["before work", "before swap"])
async def test_catalog_import_audio_rechecks_the_importers_access_at_the_swap(
    navidrome_factory, tmp_path, monkeypatch, revoke_at
):
    from tests.acceptance.test_music_import_audio import drain

    nd = navidrome_factory()
    w, engine, song, _ = await _audio_ready(nd, tmp_path)
    ben = w.users["ben"][0]

    async def revoke():
        await anyio.to_thread.run_sync(lambda: nd.native("DELETE", f"user/{ben}"))

    if revoke_at == "before work":
        await revoke()
    else:
        gate = engine._still_swappable

        async def revoked(staged):
            await revoke()
            await gate(staged)

        monkeypatch.setattr(engine, "_still_swappable", revoked)
    try:
        await drain(w)
        [row] = await w.store.fetchall("SELECT * FROM placeholders")
        [link] = await w.store.fetchall("SELECT * FROM music_import_links")
        assert row["song_id"] == song and row["state"] == "placeholder"
        assert not row["import_item"] and link["audio"] == "pending"
        assert not engine.interrupted()
        assert {p.name for p in engine.layout.staging.iterdir()} == {".ndignore"}
    finally:
        await w.caller.aclose()
        await w.imports.aclose()
        await w.store.close()


@pytest.mark.parametrize("owner_keeps_access", [False, True])
async def test_catalog_import_audio_restored_by_admin_accepts_owner_or_admin_access(
    navidrome_factory, tmp_path, owner_keeps_access
):
    from tests.acceptance.test_music_import_audio import advance, drain

    nd = navidrome_factory()
    w, engine, song, done = await _audio_ready(nd, tmp_path)
    ben = w.users["ben"][0]
    admin = nd.native(
        "POST",
        "user",
        json={
            "userName": "restorer",
            "name": "restorer",
            "password": "invented-admin-password",
            "isAdmin": True,
        },
    ).json()["id"]
    login = nd.login("restorer", "invented-admin-password")
    w.caller.remember(await w.caller.access.verify_native(login["token"]), login["token"])

    async def administrator(owner):
        row = await anyio.to_thread.run_sync(lambda: nd.native("GET", f"user/{owner}"))
        return row.json()["isAdmin"]

    w.removals.administrator = administrator
    try:
        await drain(w)
        await w.removals.review(ben, done["id"])
        status = await advance(w.removals, ben, done["id"], "review")
        await w.removals.approve(ben, done["id"], status["fingerprint"], "remove")
        await advance(w.removals, ben, done["id"], "removed")
        await drain(w)
        if not owner_keeps_access:
            await anyio.to_thread.run_sync(lambda: nd.native("DELETE", f"user/{ben}"))
        await w.removals.restore(admin, done["id"])
        await advance(w.removals, admin, done["id"], "restored")
        await drain(w)
        [row] = await w.store.fetchall("SELECT * FROM placeholders")
        [link] = await w.store.fetchall("SELECT * FROM music_import_links")
        assert row["song_id"] == song and row["state"] == "delivered"
        assert row["import_item"] == link["item_id"] and link["audio"] == "placed"
        assert link["owner_id"] == ben
        # A demoted restorer cannot block an owner who still has access.
        await engine.revert_to_placeholder(song, pinned=link["item_id"])
        user = (await anyio.to_thread.run_sync(lambda: nd.native("GET", f"user/{admin}"))).json()
        await anyio.to_thread.run_sync(
            lambda: nd.native("PUT", f"user/{admin}", json={**user, "isAdmin": False})
        )
        await drain(w)
        assert (await engine.placeholder_row(song))["state"] == (
            "delivered" if owner_keeps_access else "placeholder"
        )
    finally:
        await w.caller.aclose()
        await w.imports.aclose()
        await w.store.close()


@pytest.mark.parametrize("how", ["failed", "canceled"])
async def test_materialization_cleans_a_stage_when_preparation_stops_after_silence(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    async with engine_for(navidrome, tmp_path) as p:
        p.layout.ensure()
        before = {f.name for f in p.layout.staging.iterdir()}
        write = p.silence.write

        async def stop(duration: int, path: Path) -> None:
            await write(duration, path)
            if how == "failed":
                raise OSError("tag preparation stopped")
            raise asyncio.CancelledError

        monkeypatch.setattr(p.silence, "write", stop)
        if how == "failed":
            with pytest.raises(MaterializeError):
                await p.engine.materialize(release_of("prepare-" + how, 2))
        else:
            # A child canceled on its own need not cancel its task group, so use an
            # enclosing scope as the running request would.
            with anyio.CancelScope() as scope:

                async def cancel(duration: int, path: Path) -> None:
                    await write(duration, path)
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

                monkeypatch.setattr(p.silence, "write", cancel)
                await p.engine.materialize(release_of("prepare-" + how, 2))
        assert {f.name for f in p.layout.staging.iterdir()} == before
        assert await p.store.fetchall("SELECT * FROM pending_placeholders") == []
        assert await p.store.fetchall("SELECT * FROM placeholders") == []


@pytest.mark.parametrize("fail_recovery", [False, True])
async def test_removal_recovers_a_delivery_stopped_between_renames_first(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    fail_recovery: bool,
) -> None:
    state = tmp_path / "state"
    key = "remove-interrupted-" + str(fail_recovery).lower()
    song, folder = await one_placeholder(navidrome, state, key)
    audio = delivered_audio(tmp_path, "m4a", key)
    await kill_at(
        navidrome, state, "between renames", action="replace", song=song, delivered=str(audio)
    )
    async with engine_for(navidrome, state) as p:
        scan = p.engine._song_at
        if fail_recovery:

            async def unconfirmed(*args: Any, **kwargs: Any) -> bool:
                return False

            p.engine._song_at = unconfirmed  # type: ignore[method-assign]
        removed = await p.engine.remove_release("test:" + key)
        p.engine._song_at = scan  # type: ignore[method-assign]
        if fail_recovery:
            assert removed.kept and removed.unfinished
            assert await p.store.fetchone("SELECT * FROM releases WHERE ref = ?", ["test:" + key])
            assert p.engine.interrupted() == {song}
            assert await p.engine.recover_swap(song) >= 0
            removed = await p.engine.remove_release("test:" + key)
        assert removed.removed == 1
        await assert_in_step(p, folder)


@pytest.mark.parametrize("step", ["swapped", "verified", "committed"])
async def test_recording_identity_and_backing_commit_together_during_a_retag(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    step: str,
) -> None:
    release = release_of("backing-" + step, 1)
    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        song = next(iter(made.created.values()))
        await p.engine.set_backing(song, "old-owned-recording")
        original_recover = p.engine._recover

        async def no_compensation(*args: Any, **kwargs: Any) -> int:
            return 0

        # Simulate a hard stop: leave the swap's files for a fresh engine's recovery.
        async def stopped(at: str) -> None:
            if at == "retag " + step:
                p.engine._recover = no_compensation  # type: ignore[method-assign]
                raise _Stopped

        p.engine.step = stopped
        track = dataclasses.replace(release.tracks[0], isrc="ZZSHJ2600999")
        with pytest.raises(_Stopped):
            await p.engine.retag(song, track, release)
        p.engine.step = None
        p.engine._recover = original_recover  # type: ignore[method-assign]
        await p.engine.recover_swap(song)
        row = await row_of(p, song)
        assert row["isrc"] == (track.isrc if step == "committed" else release.tracks[0].isrc)
        assert row["backing_song_id"] == (None if step == "committed" else "old-owned-recording")
        await assert_in_step(p, made.folder)


@pytest.mark.parametrize("cycle", ["chain", "two", "three", "three-late-failure"])
@pytest.mark.parametrize("how", ["failed", "stopped"])
async def test_refresh_renumbering_never_moves_onto_a_position_not_yet_freed(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cycle: str,
    how: str,
) -> None:
    from shijhon.fill.refresh import Refresh

    release = release_of(f"refresh-order-{cycle}-{how}", 3)
    numbers = [2, 3, 1] if cycle.startswith("three") else [2, 1, 3] if cycle == "two" else [2, 3, 4]
    fresh = dataclasses.replace(
        release,
        tracks=tuple(
            dataclasses.replace(t, number=n) for t, n in zip(release.tracks, numbers, strict=True)
        ),
    )

    class Catalog:
        key = "test"

        async def album(self, key: str) -> CatalogRelease:
            return fresh

    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        refresh = Refresh(p.store, p.navidrome, p.engine, Catalog())  # type: ignore[arg-type]
        retag = p.engine.retag
        if how == "failed":

            async def fail_second(song: str, *args: Any) -> bool:
                if song == made.created[release.tracks[2 if cycle == "three" else 1].ref]:
                    raise ReplaceError("track could not be rewritten")
                return await retag(song, *args)

            monkeypatch.setattr(p.engine, "retag", fail_second)
            [result] = await refresh.refresh(made.album_id)
            assert result.skipped
            # The failure persists across refreshes: a temporary cycle-break position
            # must not become the song's permanent number.
            await refresh.refresh(made.album_id)
            if cycle != "chain":
                for track in release.tracks:
                    row = await row_of(p, made.created[track.ref])
                    assert row["track"] == track.number
        else:

            async def stop(at: str) -> None:
                if at == "retag committed":
                    raise _Stopped

            p.engine.step = stop
            with pytest.raises(_Stopped):
                await refresh.refresh(made.album_id)
            p.engine.step = None
        await assert_in_step(p, made.folder)
        rows = await p.store.fetchall("SELECT song_id, disc, track FROM placeholders")
        positions = {(r["disc"], r["track"]) for r in rows}
        assert len(positions) == 3
        for row in rows:
            song = await p.navidrome.song(row["song_id"])
            assert song is not None and song["trackNumber"] == row["track"]
        monkeypatch.undo()
        await refresh.refresh(made.album_id)
        for track in fresh.tracks:
            row = await row_of(p, made.created[track.ref])
            assert row["track"] == track.number
        await assert_in_step(p, made.folder)


@pytest.mark.parametrize("step", ["swapped", "verified", "committed"])
async def test_restoring_a_blocked_cycle_mover_remains_in_step_when_stopped(
    navidrome: NavidromeInstance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    from shijhon.fill.refresh import Refresh

    release = release_of("refresh-compensate-" + step, 3)
    fresh = dataclasses.replace(
        release,
        tracks=tuple(
            dataclasses.replace(t, number=n) for t, n in zip(release.tracks, [2, 3, 1], strict=True)
        ),
    )

    class Catalog:
        key = "test"

        async def album(self, key: str) -> CatalogRelease:
            return fresh

    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        first = made.created[release.tracks[0].ref]
        third = made.created[release.tracks[2].ref]
        retag = p.engine.retag

        async def stopped(at: str) -> None:
            if at == "retag " + step:
                raise _Stopped

        async def fail_and_stop(song: str, track: Any, *args: Any) -> bool:
            if song == third:
                raise ReplaceError("third track persistently failed")
            if song == first and track.number == 1:
                p.engine.step = stopped  # stop only during the compensating retag
            return await retag(song, track, *args)

        monkeypatch.setattr(p.engine, "retag", fail_and_stop)
        refresh = Refresh(p.store, p.navidrome, p.engine, Catalog())  # type: ignore[arg-type]
        with pytest.raises(_Stopped):
            await refresh.refresh(made.album_id)
        p.engine.step = None
        await p.engine.recover_swap(first)
        await assert_in_step(p, made.folder)
        monkeypatch.undo()
        await refresh.refresh(made.album_id)
        for track in fresh.tracks:
            assert (await row_of(p, made.created[track.ref]))["track"] == track.number
        await assert_in_step(p, made.folder)


async def test_refresh_resolves_backing_again_after_the_recording_changes(
    navidrome: NavidromeInstance,
    tmp_path: Path,
) -> None:
    from shijhon.fill.refresh import Refresh

    release = release_of("refresh-reback", 1)
    fresh = dataclasses.replace(
        release, tracks=(dataclasses.replace(release.tracks[0], isrc="ZZSHJ2600998"),)
    )

    class Catalog:
        key = "test"

        async def album(self, key: str) -> CatalogRelease:
            return fresh

    async with engine_for(navidrome, tmp_path) as p:
        made = await p.engine.materialize(release)
        song = next(iter(made.created.values()))
        await p.engine.set_backing(song, "old-owned-recording")

        class Backing:
            async def back(self, pairs: Any, *args: Any) -> dict[str, str]:
                assert (await row_of(p, song))["backing_song_id"] is None
                assert pairs == [(fresh.tracks[0], song)]
                return {song: "new-owned-recording"}

        p.engine.backing = Backing()  # type: ignore[assignment]
        refresh = Refresh(p.store, p.navidrome, p.engine, Catalog())  # type: ignore[arg-type]
        [result] = await refresh.refresh(made.album_id)
        assert result.updated == 1
        assert (await row_of(p, song))["backing_song_id"] == "new-owned-recording"


async def test_materialization_killed_after_exclusive_publication_is_recovered(
    navidrome: NavidromeInstance,
    tmp_path: Path,
) -> None:
    key = "r-exclusive-link"
    release = release_of(key, 2)
    state = tmp_path / "state"
    staging = Layout(navidrome.music, "_shijhon").staging
    before = set(staging.glob("*.flac"))
    await kill_at(navidrome, state, "placeholder linked", action="materialize", key=key, count=2)
    # Startup deliberately keeps recent stages: another writer may still use them.
    # Once stale, even the link left between publication and unlink is swept.
    for path in set(staging.glob("*.flac")) - before:
        os.utime(path, (time.time() - 7200, time.time() - 7200))
    async with restarted(navidrome, state) as p:
        folder = folder_of(p, release)
        assert await assert_in_step(p, folder) == {}
        assert set(staging.glob("*.flac")) <= before
        made = await p.engine.materialize(release)
        assert len(made.created) == 2
        await assert_in_step(p, folder)
