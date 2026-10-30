"""An import's audio in catalog songs, and upgrading a person's own imported song.

Against a real, disposable Navidrome with invented music: the placeholder engine and the
music import service share one state database, as in the application.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from shijhon.collection.origins import Origins
from shijhon.collection.reader import CallerLibrary
from shijhon.collection.service import Collection
from shijhon.music_imports import ImportLimits, MusicImports
from shijhon.music_imports.catalog_audio import CatalogSongAudio
from shijhon.music_imports.removal import ImportRemovals
from shijhon.music_imports.upgrade import file_quality
from shijhon.navidrome.checks import PlaceholderWrites
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.navidrome.usage import UsageSource
from shijhon.placeholders.engine import PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.quality import ANY, DEFAULT, Minimum
from shijhon.store import Store
from tests.acceptance.test_music_import_overlap import World, advance, approve, upload
from tests.harness.engine import catalog_release
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, NavidromeInstance

pytestmark = pytest.mark.anyio

ARTIST, TITLE = "Tide Paper", "Ember Road"
RELEASE = catalog_release("ember", TITLE, ARTIST, 3)


async def audio_world(
    nd: NavidromeInstance,
    tmp_path: Path,
    names: tuple[str, ...],
    *,
    minimum: Minimum = ANY,
) -> tuple[World, PlaceholderEngine]:
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD, client_name="shijhon")
    caller = CallerLibrary(SimpleNamespace(client=SimpleNamespace(native=native)))
    users = {}
    for name in names:
        ident = nd.create_user(name, "invented-password-" + name)
        login = nd.login(name, "invented-password-" + name)
        caller.remember(await caller.access.verify_native(login["token"]), login["token"])
        users[name] = (ident, "invented-password-" + name)
    store = await Store.open(tmp_path / "state.db")
    scans = ScanCoordinator(native)
    engine = PlaceholderEngine(
        layout=Layout(nd.music, "_shijhon"),
        navidrome=native,
        scans=scans,
        silence=SilenceMaker(),
        store=store,
    )
    collection = Collection(store, caller)
    (nd.music / "_imports").mkdir(exist_ok=True)

    async def minimum_for(user_id: str) -> Minimum:
        return minimum

    imports = MusicImports(
        store,
        caller,
        library_id="1",
        library_path=nd.music,
        limits=replace(ImportLimits(), min_free_bytes=0),
        scan=scans.scan,
        native_lookup=caller.imported_file,
        refresh=collection.refresh,
        native_song=caller.song,
        catalog_audio=CatalogSongAudio(engine),
        minimum=minimum_for,
    )
    collection.index.asset_lookup = imports.asset_lookup
    writes = PlaceholderWrites(native)

    async def gate() -> None:
        assert await writes.refused(fresh=True) is None

    async def administrator(ident: str) -> bool:
        return False

    removals = ImportRemovals(
        imports,
        usage=UsageSource(nd.data / "navidrome.db", export=False),
        gate=gate,
        administrator=administrator,
        native_absent=caller.imported_file_absent,
    )
    await Origins(store).initialize_personal("1")
    await imports.initialize()
    return World(nd, store, caller, collection, imports, removals, users), engine


async def refresh_all(w: World) -> None:
    for ident, _ in w.users.values():
        await w.collection.refresh(ident, "1")


async def drain(w: World) -> None:
    """The import worker's catalog-audio steps until nothing is left to do."""
    for _ in range(20):
        if not await w.imports.tick():
            return
    pytest.fail("the import worker kept finding work")


def pcm(data: bytes, tmp_path: Path) -> str:
    """The decoded audio's SHA-256 (tags aside)."""
    source = tmp_path / f"decode-{hashlib.sha256(data).hexdigest()[:12]}"
    source.write_bytes(data)
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(source), "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    source.unlink()
    return hashlib.sha256(out).hexdigest()


def tracks_of(release: Any, fmt: str) -> Album:
    return Album(
        ARTIST,
        TITLE,
        tuple(
            Track(t.title, t.number, seconds=t.duration_ms / 1000, isrc=t.isrc)
            for t in release.tracks
        ),
        fmt=fmt,  # type: ignore[arg-type]
        folder="Mine",
    )


async def test_an_import_plays_for_the_catalog_songs_it_matched(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    w, engine = await audio_world(nd, tmp_path, ("ben",))
    ben = w.users["ben"][0]
    made = await engine.materialize(RELEASE)
    silent, cached, others = (made.created[t.ref] for t in RELEASE.tracks)
    # A downloaded cache on the second song, another import's own audio on the third.
    spare = write_album(tmp_path / "spare", replace(tracks_of(RELEASE, "flac"), folder="S"))
    await engine.replace_with_delivered(cached, spare[1])
    await engine.replace_with_delivered(others, spare[2], pin="another-import")
    await refresh_all(w)
    await w.call("ben", "star", {"id": silent})
    playlist = (await w.call("ben", "createPlaylist", {"name": "Road", "songId": silent}))[
        "playlist"
    ]["id"]

    source = tmp_path / "ben"
    files = write_album(source, tracks_of(RELEASE, "mp3"))
    review = await upload(w, "ben", files, source, "ben-import")
    assert {i["outcome"] for i in review["items"]} == {"catalog_song"}
    done = await approve(w, "ben", review)
    assert done["linked_files"] == 3
    forgotten: list[str] = []
    w.imports.forget = forgotten.append  # the playback quality gate's cached facts
    await drain(w)

    rows = {r["song_id"]: r for r in await w.store.fetchall("SELECT * FROM placeholders")}
    links = {r["song_id"]: r for r in await w.store.fetchall("SELECT * FROM music_import_links")}
    for song in (silent, cached):
        assert rows[song]["state"] == "delivered" and rows[song]["import_item"]
        assert rows[song]["import_item"] == links[song]["item_id"]
        assert links[song]["audio"] == "placed"
        # The copy in the managed folder counts against Ben's storage.
        item = await w.store.fetchone(
            "SELECT reserved_size,received_size FROM music_import_items WHERE id=?",
            [links[song]["item_id"]],
        )
        assert links[song]["reserved"] > 0
        assert item["reserved_size"] == item["received_size"] + links[song]["reserved"]
    assert links[others]["reserved"] == 0  # kept: no copy made
    assert {silent, cached} <= set(forgotten)
    # Never overwritten: another import's own audio stays.
    assert rows[others]["import_item"] == "another-import" and links[others]["audio"] == "kept"
    # The same song: its ID, favorite and playlist entry; it plays Ben's audio.
    detail = (await w.call("ben", "getSong", {"id": silent}))["song"]
    assert detail["suffix"] == "mp3" and detail.get("starred")
    entries = (await w.call("ben", "getPlaylist", {"id": playlist}))["playlist"]["entry"]
    assert [e["id"] for e in entries] == [silent]
    client = w.client("ben")
    try:
        downloaded = await anyio.to_thread.run_sync(
            lambda: client.request("download", {"id": silent}).content
        )
    finally:
        client.close()
    assert pcm(downloaded, tmp_path) == pcm(files[0].read_bytes(), tmp_path)
    # The import's audio is no cache: expiry does not take it.
    assert not await engine.revert_to_placeholder(silent)
    # The report says so.
    status = await w.imports.status(ben, done["id"], overlap_only=True)
    assert sorted(i["audio"] for i in status["items"]) == ["kept", "placed", "placed"]

    # Removing the import puts the catalog songs back as they were; restoring, Ben's audio.
    await w.removals.review(ben, done["id"])
    status = await advance(w.removals, ben, done["id"], "review")
    await w.removals.approve(ben, done["id"], status["fingerprint"], "remove-ben")
    await advance(w.removals, ben, done["id"], "removed")
    await drain(w)
    rows = {r["song_id"]: r for r in await w.store.fetchall("SELECT * FROM placeholders")}
    assert rows[silent]["state"] == rows[cached]["state"] == "placeholder"
    reserved = await w.store.fetchall("SELECT reserved FROM music_import_links")
    assert {r["reserved"] for r in reserved} == {0}  # the copies are gone: released
    assert (await w.removals.status(ben, done["id"]))["audio_withdrawing"] == 0
    assert rows[silent]["import_item"] is None and rows[others]["import_item"] == "another-import"
    assert (await w.call("ben", "getSong", {"id": silent}))["song"]["suffix"] == "flac"
    await w.removals.restore(ben, done["id"])
    await advance(w.removals, ben, done["id"], "restored")
    await drain(w)
    rows = {r["song_id"]: r for r in await w.store.fetchall("SELECT * FROM placeholders")}
    assert rows[silent]["state"] == rows[cached]["state"] == "delivered"
    assert (await w.call("ben", "getSong", {"id": silent}))["song"].get("starred")
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_an_import_below_the_minimum_does_not_play_for_catalog_songs(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    w, engine = await audio_world(nd, tmp_path, ("ben",), minimum=Minimum(lossless_only=True))
    made = await engine.materialize(RELEASE)
    await refresh_all(w)
    source = tmp_path / "ben"
    files = write_album(source, tracks_of(RELEASE, "mp3"))
    review = await upload(w, "ben", files, source, "ben-import")
    await approve(w, "ben", review)
    await drain(w)
    links = await w.store.fetchall("SELECT song_id,audio FROM music_import_links")
    # The review's quality gate already said so (``match.audio`` False): no audio is due.
    assert {r["audio"] for r in links} == {""}
    items = await w.store.fetchall("SELECT match FROM music_import_items")
    assert all(json.loads(r["match"]).get("audio") is False for r in items)
    assert {r["song_id"] for r in links} == set(made.created.values())
    rows = await w.store.fetchall("SELECT state FROM placeholders")
    assert {r["state"] for r in rows} == {"placeholder"}
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def own_import(
    w: World, tmp_path: Path, name: str, album: Album
) -> tuple[dict[str, Any], list[Path]]:
    source = tmp_path / name
    files = write_album(source, album)
    review = await upload(w, name, files, source, name + "-import")
    done = await approve(w, name, review)
    return done, files


async def test_upgrading_an_own_import_keeps_the_song(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    w, _ = await audio_world(nd, tmp_path, ("ana", "ben"), minimum=DEFAULT)
    ana, ben = w.users["ana"][0], w.users["ben"][0]
    tracks = (Track("Brine", 1, seconds=4.0), Track("Shoal", 2, seconds=5.0))
    album = Album("Quiet Hollow", "Salt Lines", tracks, fmt="mp3", folder="Mine",
                  embedded_cover=True)  # fmt: skip
    done, files = await own_import(w, tmp_path, "ana", album)
    items = sorted(done["items"], key=lambda i: i["path"])
    song, unused = items[0]["song_id"], items[1]["song_id"]
    await refresh_all(w)
    await w.call("ana", "star", {"id": song})
    playlist = (await w.call("ana", "createPlaylist", {"name": "Tide", "songId": song}))[
        "playlist"
    ]["id"]
    before = (await w.call("ana", "getSong", {"id": song}))["song"]
    # The add-ons' audio: the same recordings as FLAC, with tags of their own.
    fetched = write_album(
        tmp_path / "addon", replace(album, fmt="flac", title="Salt Lines (Remastered)", folder="A")
    )
    got = file_quality(fetched[0])
    assert got.lossless

    # Someone else, or a song that is not the person's own import: never.
    assert await w.imports.upgrades.own_import(ben, song) is None
    assert await w.imports.upgrades.upgrade(ben, song, fetched[0], got)
    assert await w.imports.upgrades.upgrade(ana, song, fetched[0], got) == ""
    assert await w.imports.upgrades.upgrade(ana, unused, fetched[1], got) == ""

    after = (await w.call("ana", "getSong", {"id": song}))["song"]
    assert after["id"] == song and after["suffix"] == "flac" and after.get("starred")
    assert after["album"] == before["album"] and after["title"] == before["title"]
    assert after["albumId"] == before["albumId"]
    entries = (await w.call("ana", "getPlaylist", {"id": playlist}))["playlist"]["entry"]
    assert [e["id"] for e in entries] == [song]
    row = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [items[0]["id"]])
    replaced = json.loads(row["upgraded_from"])
    assert row["final_name"].endswith(".flac") and replaced["final"].endswith(".mp3")
    assert row["reserved_size"] >= row["expected_size"] + replaced["size"]
    # The replaced file stays retained privately, unchanged.
    retained = nd.music / "_imports" / ".incoming" / replaced["staging"]
    assert (
        hashlib.sha256(retained.read_bytes()).digest()
        == hashlib.sha256(files[0].read_bytes()).digest()
    )
    status = await w.imports.status(ana, done["id"], overlap_only=True)
    assert all(i["upgraded"]["to"].startswith("FLAC") for i in status["items"])
    # A later upgrade keeps the received original's receipt, as well as the first upgrade.
    first_upgrade = await w.store.fetchone(
        "SELECT * FROM music_import_items WHERE id=?", [items[1]["id"]]
    )
    assert await w.imports.upgrades.upgrade(ana, unused, fetched[0], got) == ""
    second_upgrade = await w.store.fetchone(
        "SELECT * FROM music_import_items WHERE id=?", [items[1]["id"]]
    )
    history = json.loads(second_upgrade["upgraded_from"])
    assert history["staging"] == json.loads(first_upgrade["upgraded_from"])["staging"]
    assert history["upgrades"][0]["staging"] == first_upgrade["staging_name"]
    assert (await w.call("ana", "getSong", {"id": unused}))["song"]["id"] == unused
    # Removal takes the unused upgraded file out (the favorite keeps the other); restoring
    # puts the upgraded file back.
    other = await w.store.fetchone("SELECT * FROM music_import_items WHERE id=?", [items[1]["id"]])
    await w.removals.review(ana, done["id"])
    status = await advance(w.removals, ana, done["id"], "review")
    await w.removals.approve(ana, done["id"], status["fingerprint"], "remove-ana")
    await advance(w.removals, ana, done["id"], "removed")
    assert not (nd.music / "_imports" / other["final_name"]).exists()
    assert (nd.music / "_imports" / row["final_name"]).exists()
    await w.removals.restore(ana, done["id"])
    await advance(w.removals, ana, done["id"], "restored")
    assert (nd.music / "_imports" / other["final_name"]).exists()
    assert (await w.call("ana", "getSong", {"id": unused}))["song"]["suffix"] == "flac"
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_a_linked_song_is_not_upgraded_by_the_one_who_linked_it(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    w, _ = await audio_world(nd, tmp_path, ("ana", "ben"), minimum=DEFAULT)
    ben = w.users["ben"][0]
    album = Album("Quiet Hollow", "Salt Lines", (Track("Brine", 1, seconds=4.0),), fmt="mp3",
                  folder="Mine")  # fmt: skip
    done, _ = await own_import(w, tmp_path, "ana", album)
    song = done["items"][0]["song_id"]
    await refresh_all(w)
    linked, _ = await own_import(w, tmp_path, "ben", album)
    assert linked["linked_files"] == 1
    # Ben's link is to Ana's file: only she upgrades it.
    assert await w.imports.upgrades.own_import(ben, song) is None
    # Ana importing it again links to her own earlier import, which stays hers to upgrade.
    files = write_album(tmp_path / "again", replace(album, fmt="flac"))
    review = await upload(w, "ana", files, tmp_path / "again", "ana-again")
    again = await approve(w, "ana", review)
    assert again["linked_files"] == 1
    mine = await w.imports.upgrades.own_import(w.users["ana"][0], song)
    assert mine is not None and mine["job_id"] == done["id"]
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()


async def test_installed_catalog_audio_is_not_pending_free_space(
    navidrome_factory, tmp_path, monkeypatch
):
    from tests.acceptance.test_R_stops import _audio_ready

    w, engine, song, _ = await _audio_ready(navidrome_factory(), tmp_path)
    try:
        await drain(w)
        [link] = await w.store.fetchall("SELECT * FROM music_import_links")
        assert link["reserved"] > 0 and (await engine.placeholder_row(song))["state"] == "delivered"
        w.imports.limits = replace(w.imports.limits, min_free_bytes=10)
        monkeypatch.setattr(w.imports.storage, "free", lambda: 15)
        await w.imports._capacity(link["owner_id"], 5)
        # Once taken out, the still-held reservation covers a future copy again.
        await engine.revert_to_placeholder(song, pinned=link["item_id"])
        from shijhon.music_imports import ImportError

        with pytest.raises(ImportError, match="unreserved space"):
            await w.imports._capacity(link["owner_id"], 5)
    finally:
        await w.caller.aclose()
        await w.imports.aclose()
        await w.store.close()
