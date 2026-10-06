"""Suite E — ID survival.

Delivered audio written in place of a placeholder keeps the song ID and everything users
attached to it (stars, ratings, play counts, bookmarks, playlist order with repeats, play
queue) — for FLAC at the same path and for other formats under their own extension with
copied tags; reverting keeps them too. A replacement that would change the ID is
rolled back. Navidrome's own move rules are pinned here as well: moves keep IDs only when
album tags and the exact title are unchanged.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest
from mutagen.flac import FLAC

from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import ReplaceError
from tests.harness.engine import catalog_release, engine_for
from tests.harness.library import frequency_for, tone
from tests.harness.navidrome import NavidromeInstance

pytestmark = pytest.mark.anyio


@dataclass
class World:
    songs: list[str]
    playlist: str
    state: Path
    album_id: str


@pytest.fixture(scope="module")
def world(
    navidrome: NavidromeInstance, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    state = tmp_path_factory.mktemp("e-state")
    release = catalog_release("e-main", "Survivor", "Survivor Artist", 6)

    async def build() -> Any:
        async with engine_for(navidrome, state) as parts:
            return await parts.engine.materialize(release)

    result = anyio.run(build)
    songs = [result.created[t.ref] for t in release.tracks]
    admin = navidrome.client()
    s1, s2, s3, s4 = songs[:4]
    admin.ok("star", {"id": s1})
    admin.ok("setRating", {"id": s2, "rating": 5})
    admin.ok("scrobble", {"id": s1, "submission": "true", "time": int(time.time() * 1000)})
    admin.ok("createBookmark", {"id": s3, "position": 1000})
    playlist = admin.ok("createPlaylist", {"name": "E list", "songId": [s1, s2, s1, s3, s4]})
    admin.ok("savePlayQueue", {"id": [s4, s1, s4], "current": s1, "position": 500})
    yield World(songs, playlist["playlist"]["id"], state, result.album_id)


def user_state(nd: NavidromeInstance, world: World) -> dict[str, Any]:
    admin = nd.client()
    songs = {sid: admin.ok("getSong", {"id": sid})["song"] for sid in world.songs[:4]}
    starred = {s["id"] for s in admin.ok("getStarred2")["starred2"].get("song", [])}
    return {
        "starred": starred & set(world.songs),
        "rating": {sid: s.get("userRating") for sid, s in songs.items()},
        "plays": {sid: s.get("playCount") for sid, s in songs.items()},
        "album": {s["albumId"] for s in songs.values()},
        "bookmarks": [
            (b["entry"]["id"], b["position"])
            for b in admin.ok("getBookmarks")["bookmarks"].get("bookmark", [])
        ],
        "playlist": [
            e["id"] for e in admin.ok("getPlaylist", {"id": world.playlist})["playlist"]["entry"]
        ],
        "queue": [e["id"] for e in admin.ok("getPlayQueue")["playQueue"]["entry"]],
    }


def delivered(tmp_path: Path, fmt: str, seed: str) -> Path:
    path = tmp_path / f"delivered-{seed}.{fmt}"
    shutil.copyfile(tone(frequency_for(seed), 3, fmt), path)  # type: ignore[arg-type]
    return path


@pytest.mark.parametrize("index,fmt", [(0, "flac"), (1, "m4a"), (2, "mp3")])
async def test_replace_keeps_id_and_user_state_then_revert(
    navidrome: NavidromeInstance, world: World, tmp_path: Path, index: int, fmt: str
) -> None:
    song_id = world.songs[index]
    before = user_state(navidrome, world)
    async with engine_for(navidrome, world.state) as parts:
        new_path = await parts.engine.replace_with_delivered(song_id, delivered(tmp_path, fmt, fmt))
        assert new_path.endswith(f".{fmt}")
        song = navidrome.client().ok("getSong", {"id": song_id})["song"]
        assert song["suffix"] == fmt
        assert user_state(navidrome, world) == before
        on_disk = parts.layout.absolute(new_path).read_bytes()
        streamed = navidrome.client().request("stream", {"id": song_id, "format": "raw"})
        assert streamed.content == on_disk

        await parts.engine.revert_to_placeholder(song_id)
        song = navidrome.client().ok("getSong", {"id": song_id})["song"]
        assert song["suffix"] == "flac"
        assert not parts.layout.absolute(new_path).exists() or fmt == "flac"
        row = await parts.store.fetchone(
            "SELECT state, path FROM placeholders WHERE song_id = ?", [song_id]
        )
        assert row is not None and row["state"] == "placeholder" and row["path"].endswith(".flac")
    assert user_state(navidrome, world) == before


async def test_replacement_that_would_change_the_id_is_rolled_back(
    navidrome: NavidromeInstance, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    song_id = world.songs[4]
    original = tagging.write_delivered

    def wrong_title(path: Path, comments: dict[str, list[str]], **kwargs: Any) -> None:
        original(path, {**comments, "title": ["Some Other Title"]}, **kwargs)

    monkeypatch.setattr(tagging, "write_delivered", wrong_title)
    async with engine_for(navidrome, world.state) as parts:
        row = await parts.store.fetchone(
            "SELECT path FROM placeholders WHERE song_id = ?", [song_id]
        )
        assert row is not None
        with pytest.raises(ReplaceError, match="ID changed"):
            await parts.engine.replace_with_delivered(song_id, delivered(tmp_path, "m4a", "bad"))
        assert parts.layout.absolute(row["path"]).exists()
        assert not parts.layout.absolute(row["path"]).with_suffix(".m4a").exists()
        song = await parts.navidrome.song(song_id)
        assert song is not None and not song["missing"] and song["path"] == row["path"]


# --- Navidrome's own rules, pinned for the upgrade canary -----------------------------


@pytest.fixture(scope="module")
def movable(
    navidrome: NavidromeInstance, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, str]:
    """Placeholders of a separate release: track number -> song ID."""
    state = tmp_path_factory.mktemp("e-moves")
    release = catalog_release("e-moves", "Mover", "Mover Artist", 6)

    async def build() -> Any:
        async with engine_for(navidrome, state) as parts:
            result = await parts.engine.materialize(release)
            return {t.number: result.created[t.ref] for t in release.tracks}, result.folder

    ids, folder = anyio.run(build)
    return {"folder": folder, **{str(k): v for k, v in ids.items()}}


def song_path(nd: NavidromeInstance, song_id: str) -> Path:
    return nd.music / nd.native("GET", f"song/{song_id}").json()["path"]


def edit(path: Path, **changes: list[str]) -> None:
    audio = FLAC(path)
    for key, values in changes.items():
        audio[key] = values
    audio.save()


def moved_id(nd: NavidromeInstance, source: Path, target_dir: Path, **changes: list[str]) -> str:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    shutil.move(source, target)
    if changes:
        edit(target, **changes)
    rel_from = str(source.parent.relative_to(nd.music))
    rel_to = str(target_dir.relative_to(nd.music))
    nd.scan(targets=[rel_from, rel_to])
    songs = nd.native("GET", "song", params={"path": rel_to + "/", "_start": 0, "_end": 50}).json()
    [song] = [s for s in songs if s["path"] == f"{rel_to}/{target.name}"]
    return str(song["id"])


def test_in_place_with_changed_title_keeps_id(
    navidrome: NavidromeInstance, movable: dict[str, str]
) -> None:
    path = song_path(navidrome, movable["1"])
    edit(path, title=["Renamed In Place"])
    navidrome.scan(targets=[movable["folder"]])
    assert navidrome.native("GET", f"song/{movable['1']}").json()["title"] == "Renamed In Place"


def test_move_with_same_tags_keeps_id(
    navidrome: NavidromeInstance, movable: dict[str, str]
) -> None:
    source = song_path(navidrome, movable["2"])
    assert moved_id(navidrome, source, navidrome.music / "moved" / "same") == movable["2"]


def test_move_with_changed_track_number_keeps_id(
    navidrome: NavidromeInstance, movable: dict[str, str]
) -> None:
    source = song_path(navidrome, movable["3"])
    new_id = moved_id(navidrome, source, navidrome.music / "moved" / "number", tracknumber=["9"])
    assert new_id == movable["3"]


def test_move_with_changed_title_gets_new_id(
    navidrome: NavidromeInstance, movable: dict[str, str]
) -> None:
    source = song_path(navidrome, movable["4"])
    new_id = moved_id(navidrome, source, navidrome.music / "moved" / "title", title=["Changed"])
    assert new_id != movable["4"]


def test_move_with_added_release_track_id_gets_new_id(
    navidrome: NavidromeInstance, movable: dict[str, str]
) -> None:
    # Navidrome's track ID is "musicbrainz_trackid|albumid,discnumber,tracknumber,title",
    # where musicbrainz_trackid is the *release track* ID (Vorbis MUSICBRAINZ_RELEASETRACKID).
    source = song_path(navidrome, movable["5"])
    mbid = ["00000000-aaaa-4bbb-8ccc-000000000011"]
    new_id = moved_id(
        navidrome, source, navidrome.music / "moved" / "mbid", musicbrainz_releasetrackid=mbid
    )
    assert new_id != movable["5"]


def test_move_with_added_recording_id_keeps_id(
    navidrome: NavidromeInstance, movable: dict[str, str]
) -> None:
    # Vorbis MUSICBRAINZ_TRACKID is the recording ID, which is not part of the track ID.
    source = song_path(navidrome, movable["6"])
    mbid = ["00000000-aaaa-4bbb-8ccc-000000000022"]
    new_id = moved_id(
        navidrome, source, navidrome.music / "moved" / "recording", musicbrainz_trackid=mbid
    )
    assert new_id == movable["6"]


# --- regressions ------------------------------------------------------------------------


async def test_failed_scan_during_replacement_restores_the_placeholder(
    navidrome: NavidromeInstance, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shijhon.navidrome.client import NavidromeError

    song_id = world.songs[5]
    async with engine_for(navidrome, world.state) as parts:
        row = await parts.store.fetchone(
            "SELECT path FROM placeholders WHERE song_id = ?", [song_id]
        )
        assert row is not None
        real_until = parts.scans.until
        calls = {"n": 0}

        async def failing_once(*args: Any, **kwargs: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise NavidromeError("startScan: HTTP 500", status=500)
            return await real_until(*args, **kwargs)

        monkeypatch.setattr(parts.scans, "until", failing_once)
        with pytest.raises(ReplaceError, match="restored"):
            await parts.engine.replace_with_delivered(song_id, delivered(tmp_path, "mp3", "scan"))
        assert parts.layout.absolute(row["path"]).exists()
        assert not parts.layout.absolute(row["path"]).with_suffix(".mp3").exists()
        assert not list(parts.layout.staging.glob("backup-*"))
        state = await parts.store.fetchone(
            "SELECT state FROM placeholders WHERE song_id = ?", [song_id]
        )
        assert state is not None and state["state"] == "placeholder"
        # The next attempt works.
        await parts.engine.replace_with_delivered(song_id, delivered(tmp_path, "mp3", "scan2"))
        await parts.engine.revert_to_placeholder(song_id)


async def test_interrupted_swap_is_reconciled(
    navidrome: NavidromeInstance, world: World, tmp_path: Path
) -> None:
    song_id = world.songs[5]
    async with engine_for(navidrome, world.state) as parts:
        row = await parts.store.fetchone(
            "SELECT path FROM placeholders WHERE song_id = ?", [song_id]
        )
        assert row is not None
        placeholder = parts.layout.absolute(row["path"])
        # A crash between the two moves of a swap: silent file in staging, delivered in place.
        parts.layout.ensure()
        placeholder.rename(parts.layout.staging / f"backup-{song_id}.flac")
        shutil.copyfile(delivered(tmp_path, "m4a", "crash"), placeholder.with_suffix(".m4a"))
        new_path = await parts.engine.replace_with_delivered(
            song_id, delivered(tmp_path, "flac", "after")
        )
        assert new_path == row["path"]
        assert not placeholder.with_suffix(".m4a").exists()
        song = await parts.navidrome.song(song_id)
        assert song is not None and not song["missing"]
        await parts.engine.revert_to_placeholder(song_id)
