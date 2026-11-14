"""The credit repair's choice of placeholders (``placeholders/credits.py``) without
Navidrome: what it lists, that it reads without the store lock, that an album's
``albumartists`` cut short are finished later, and that a failed pass leaves the worker."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, release_data
from shijhon.placeholders import credits
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.credits import CreditRepair
from shijhon.placeholders.engine import PlaceholderEngine, ReplaceError
from shijhon.store import Store
from tests.harness.engine import catalog_release

pytestmark = pytest.mark.anyio

DUO = (CatalogRef("test", "ash"), CatalogRef("test", "bell"))


async def record(store: Store, release: CatalogRelease, tags: dict[str, Any] | None = None) -> None:
    """The release and a placeholder of each of its tracks, as a write leaves them."""
    album_tags = tags or {"album": [release.title], "albumartist": [release.artist]}
    ref = str(release.ref)
    await store.execute(
        "INSERT INTO releases (ref, folder, album_id, title, artist, album_tags, data,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        [
            ref,
            ref,
            f"album-{ref}",
            release.title,
            release.artist,
            json.dumps(album_tags),
            json.dumps(release_data(release)),
        ],
    )
    for n, track in enumerate(release.tracks, 1):
        await store.execute(
            "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref, release_ref,"
            " title, artist, album, duration_ms, disc, track, tags, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0)",
            [
                f"{ref}-s{n}",
                f"{ref}/{n}.flac",
                f"{ref}/{n}.flac",
                str(track.ref),
                ref,
                track.title,
                track.artist,
                release.title,
                track.duration_ms,
                n,
                json.dumps(tagging.placeholder_tags(album_tags, track, release)),
            ],
        )


def engine(store: Store) -> PlaceholderEngine:
    return PlaceholderEngine(layout=None, navidrome=None, scans=None, silence=None, store=store)  # type: ignore[arg-type]


def listing(found: list[credits._Release]) -> dict[str, list[str]]:
    return {e.ref: [song for song, _ in e.songs] for e in found}


async def test_candidates_are_read_without_the_store_lock(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:
        album = replace(
            catalog_release("ca", "Two Rivers", "Ash Vane & Bell Orrin", 2), artist_refs=DUO
        )
        joined = catalog_release(
            "cj", "Lantern", "Cora Lind", 2, track_artist="Cora Lind & Dov Park"
        )
        # Its first track written with its artists already, the second asks for them.
        joined = replace(
            joined,
            tracks=(replace(joined.tracks[0], artist_refs=DUO), joined.tracks[1]),
        )
        # Written with each artist: nothing to do.
        done = catalog_release("cd", "Ember", "Rook Hale", 1, track_artist="Rook Hale & Sima Dune")
        done = replace(done, tracks=tuple(replace(t, artist_refs=DUO) for t in done.tracks))
        for release in (album, joined, done):
            await record(store, release)
        repair = CreditRepair(engine(store), store, catalog="test")
        with anyio.fail_after(5):
            async with store._lock:  # (another request holding the store)
                found = await repair.candidates()
    finally:
        await store.close()
    assert listing(found) == {"test:ca": ["test:ca-s1", "test:ca-s2"], "test:cj": ["test:cj-s2"]}
    assert found[0].album == ["Ash Vane", "Bell Orrin"]


async def test_an_album_whose_retags_were_cut_short_is_finished_by_a_later_pass(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "state.db")
    try:
        release = replace(
            catalog_release("cr", "Night", "Ash Vane & Bell Orrin", 3, track_artist="Ash Vane"),
            artist_refs=DUO,
        )
        await record(store, release)
        placeholders = engine(store)
        calls: list[str] = []
        stop = False

        async def recredit(song_id: str, track: CatalogTrack) -> bool:
            nonlocal stop
            calls.append(song_id)
            if len(calls) == 2:
                stop = True  # (and a stop after it: the third is not reached)
                raise ReplaceError("cannot write the placeholder (OSError)")
            row = await store.fetchone("SELECT album_tags FROM releases")
            song = await store.fetchone(
                "SELECT tags FROM placeholders WHERE song_id = ?", [song_id]
            )
            assert row is not None and song is not None
            tags = json.loads(song["tags"])  # (the song as it is, with the album's artists)
            tags["albumartists"] = json.loads(row["album_tags"])["albumartists"]
            await store.execute(
                "UPDATE placeholders SET tags = ? WHERE song_id = ?", [json.dumps(tags), song_id]
            )
            return True

        placeholders.recredit = recredit  # type: ignore[method-assign]
        repair = CreditRepair(placeholders, store, catalog="test", stopping=lambda: stop)
        assert await repair.repair() == 1
        stored = await store.fetchone("SELECT album_tags FROM releases")
        assert stored is not None
        assert json.loads(stored["album_tags"])["albumartists"] == ["Ash Vane", "Bell Orrin"]

        stop = False
        later = CreditRepair(placeholders, store, catalog="test")
        assert listing(await later.candidates()) == {"test:cr": ["test:cr-s2", "test:cr-s3"]}
        assert await later.repair() == 2
        rows = await store.fetchall("SELECT tags FROM placeholders ORDER BY song_id")
        assert await later.candidates() == []
    finally:
        await store.close()
    assert [json.loads(r["tags"])["albumartists"] for r in rows] == [["Ash Vane", "Bell Orrin"]] * 3


async def test_a_failed_pass_leaves_the_worker_for_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await Store.open(tmp_path / "state.db")
    passes: list[int] = []
    try:
        repair = CreditRepair(
            engine(store), store, catalog="test", stopping=lambda: len(passes) >= 2, delay_seconds=0
        )

        async def once() -> int:
            passes.append(1)
            if len(passes) == 1:
                raise RuntimeError("unexpected")
            return 0

        monkeypatch.setattr(credits, "REPEAT_SECONDS", 0)
        repair.repair = once  # type: ignore[method-assign]
        with anyio.fail_after(5):
            await repair.run()
    finally:
        await store.close()
    assert len(passes) == 2


async def test_a_track_whose_credit_changed_is_asked_for_its_new_credit(tmp_path: Path) -> None:
    """What was asked is remembered with the credit it had: the same joined credit is not
    asked for twice, another one of the same track is (a repair that found its song's
    credit changed leaves it for the next pass, which must be able to ask)."""
    store = await Store.open(tmp_path / "state.db")
    try:
        asked: list[tuple[str, str]] = []

        class Engine:
            async def credited(self, tracks: Any, seconds: float) -> list[CatalogTrack]:
                asked.extend((t.ref.id, t.artist) for t in tracks)
                return list(tracks)

        repair = CreditRepair(Engine(), store, catalog="test")  # type: ignore[arg-type]
        release = catalog_release("joined", "Lantern", "Wren Hollow", 1)
        first = replace(release.tracks[0], artist="Wren Hollow & Kestrel Day")
        later = replace(first, artist="Wren Hollow & Mira Sol")

        def entry(track: CatalogTrack) -> credits._Release:
            return credits._Release("test:joined", release, None, [("song-1", track)])

        await repair._ask([entry(first)])
        await repair._ask([entry(first)])
        await repair._ask([entry(later)])
        await repair._ask([entry(later)])
        assert asked == [
            (first.ref.id, "Wren Hollow & Kestrel Day"),
            (first.ref.id, "Wren Hollow & Mira Sol"),
        ]
    finally:
        await store.close()
