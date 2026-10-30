"""An import's audio in catalog songs and upgrades of a person's own imports: the
decisions, with fakes (the real swaps are in tests/acceptance/test_music_import_audio.py)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import anyio
import mutagen
import pytest

from shijhon.delivery.quality_gate import Minimums
from shijhon.music_imports import retag
from shijhon.music_imports.catalog_audio import CatalogSongAudio
from shijhon.music_imports.upgrade import AfterPlay
from shijhon.placeholders.engine import ReplaceError, _revertible
from shijhon.placeholders.tags import raw_tags_of
from shijhon.quality import DEFAULT, Minimum, Quality
from shijhon.store import Store
from tests.harness.library import Album, Track, write_album
from tests.unit.test_quality_gate import FakeNavidrome


def test_an_imports_own_audio_is_reverted_only_for_its_import() -> None:
    cache = {"state": "delivered", "import_item": None}
    own = {"state": "delivered", "import_item": "item-1"}
    assert _revertible(cache, None, False)
    assert not _revertible({"state": "placeholder", "import_item": None}, None, False)
    assert not _revertible(own, None, False)  # expiry, the size limit: never
    assert not _revertible(own, "item-2", False)  # another import: never
    assert _revertible(own, "item-1", False)  # its own import, removed
    assert _revertible(own, None, True)  # its file is gone


class FakeEngine:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, Any]] = []
        self.refuse: str | None = None

    @contextmanager
    def guard_writes(self, authorize):
        self.authorize = authorize
        yield

    async def placeholder_row(self, song_id: str) -> Any:
        return self.row

    async def recover_swap(self, song_id: str) -> int:
        self.calls.append(("recover", song_id))
        return 0

    async def replace_with_delivered(self, song_id: str, delivered: Any = None, **kw: Any) -> str:
        if getattr(self, "authorize", None):
            await self.authorize()
        self.calls.append(("replace", kw))
        if self.refuse:
            raise ReplaceError(self.refuse)
        assert self.row is not None
        self.row = {"state": "delivered", "import_item": kw["pin"]}
        return "x" + kw["suffix"]

    async def revert_to_placeholder(self, song_id: str, **kw: Any) -> bool:
        if getattr(self, "authorize", None):
            await self.authorize()
        self.calls.append(("revert", kw))
        assert self.row is not None
        if not _revertible(self.row, kw.get("pinned"), kw.get("only_if_missing", False)):
            return False
        self.row = {"state": "placeholder", "import_item": None}
        return True


def test_putting_an_imports_audio_in_never_overwrites_what_is_there() -> None:
    async def main() -> None:
        async def authorize():
            pass

        silent = FakeEngine({"state": "placeholder", "import_item": None})
        assert (
            await CatalogSongAudio(silent).put("s", "item", 7, ".mp3", authorize=authorize)
            == "placed"
        )
        assert silent.calls == [("replace", {"source_fd": 7, "suffix": ".mp3", "pin": "item"})]
        assert (
            await CatalogSongAudio(silent).put("s", "item", 7, ".mp3", authorize=authorize)
            == "placed"
        )  # again
        assert len(silent.calls) == 1
        # Downloaded audio is a cache: back to the placeholder, then the import's audio.
        cached = FakeEngine({"state": "delivered", "import_item": None})
        assert (
            await CatalogSongAudio(cached).put("s", "item", 7, ".flac", authorize=authorize)
            == "placed"
        )
        assert [c[0] for c in cached.calls] == ["revert", "replace"]
        # Another import's own audio stays.
        other = FakeEngine({"state": "delivered", "import_item": "earlier"})
        assert (
            await CatalogSongAudio(other).put("s", "item", 7, ".mp3", authorize=authorize) == "kept"
        )
        assert other.calls == []
        # Not a catalog song any more; a refused swap; a download that got there first.
        assert (
            await CatalogSongAudio(FakeEngine(None)).put(
                "s", "item", 7, ".mp3", authorize=authorize
            )
            == "failed"
        )
        refused = FakeEngine({"state": "placeholder", "import_item": None})
        refused.refuse = "the song ID changed; the placeholder was restored"
        assert (
            await CatalogSongAudio(refused).put("s", "item", 7, ".mp3", authorize=authorize)
            == "failed"
        )
        raced = FakeEngine({"state": "placeholder", "import_item": None})
        raced.refuse = "already replaced"
        assert (
            await CatalogSongAudio(raced).put("s", "item", 7, ".mp3", authorize=authorize)
            == "pending"
        )
        # Taking it out: only this import's own audio.
        assert not await CatalogSongAudio(other).take("s", "item")
        assert await CatalogSongAudio(silent).take("s", "item")
        assert silent.row == {"state": "placeholder", "import_item": None}

    anyio.run(main)


def test_whether_a_catalog_song_plays_an_imports_audio_is_read_after_its_swaps_repair() -> None:
    async def main() -> None:
        own = FakeEngine({"state": "delivered", "import_item": "item"})
        assert await CatalogSongAudio(own).holds("s", "item")
        assert own.calls == [("recover", "s")]  # an interrupted swap put right first
        assert not await CatalogSongAudio(own).holds("s", "other")
        for row in (None, {"state": "placeholder", "import_item": None},
                    {"state": "delivered", "import_item": None}):  # fmt: skip
            assert not await CatalogSongAudio(FakeEngine(row)).holds("s", "item")

    anyio.run(main)


def test_upgrades_are_switched_on_until_a_person_switches_them_off(tmp_path: Path) -> None:
    async def main() -> None:
        store = await Store.open(tmp_path / "state.db")
        try:
            minimums = Minimums(store, FakeNavidrome({}), default="192")  # type: ignore[arg-type]
            assert await minimums.upgrades("u1")
            await minimums.set_upgrades("u1", False)
            assert not await minimums.upgrades("u1") and await minimums.upgrades("u2")
            again = Minimums(store, FakeNavidrome({}), default="192")  # type: ignore[arg-type]
            assert not await again.upgrades("u1")
            await again.set_upgrades("u1", True)
            assert await again.upgrades("u1")
        finally:
            await store.close()

    anyio.run(main)


@dataclass(frozen=True)
class Song:
    song_id: str
    quality: Quality

    def track(self) -> str:
        return "track:" + self.song_id


class FakeChoices:
    def __init__(self, *, on: bool = True, minimum: Minimum = DEFAULT) -> None:
        self.on, self.minimum = on, minimum

    async def user_id(self, username: str) -> str | None:
        return {"ana": "u-ana", "ben": "u-ben"}.get(username)

    async def upgrades(self, user_id: str) -> bool:
        return self.on

    async def minimum_for(
        self, *, user_id: str | None = None, username: str | None = None
    ) -> Minimum:
        return self.minimum


class FakeUpgrades:
    def __init__(self) -> None:
        self.done: list[tuple[str, str, Quality]] = []

    async def own_import(self, user_id: str, song_id: str) -> dict[str, Any] | None:
        return {"id": "item"} if (user_id, song_id) == ("u-ana", "song") else None

    async def upgrade(self, user_id: str, song_id: str, fetched: Path, quality: Quality) -> str:
        assert await anyio.Path(fetched).exists()
        self.done.append((user_id, song_id, quality))
        return ""


def test_only_the_importers_own_play_upgrades_and_only_to_better_audio(tmp_path: Path) -> None:
    flac = write_album(tmp_path / "f", Album("A", "B", (Track("C", 1, seconds=2.0),)))[0]
    mp3 = write_album(tmp_path / "m", Album("A", "B", (Track("C", 1, seconds=2.0),), fmt="mp3"))[0]
    low = Song("song", Quality("mp3", kbps=96, sample_rate=44100))

    def run(choices: FakeChoices, song: Song, name: str, source: Path | str) -> tuple[str, Any]:
        upgrades, forgotten, fetched = FakeUpgrades(), [], []

        async def fetch(track: Any, minimum: Minimum) -> Path | str:
            assert minimum == choices.minimum  # the add-ons' audio below it is passed over
            if isinstance(source, str):
                return source
            copy = tmp_path / f"fetched-{len(fetched)}{source.suffix}"
            await anyio.Path(copy).write_bytes(await anyio.Path(source).read_bytes())
            fetched.append(copy)
            return copy

        after = AfterPlay(upgrades, choices, fetch, forgotten.append, lambda work: True)  # type: ignore[arg-type]
        why = anyio.run(after.run, name, song)
        assert all(not f.exists() for f in fetched)  # the fetched file never stays
        return why, (upgrades.done, forgotten)

    why, (done, forgotten) = run(FakeChoices(), low, "ana", flac)
    assert why == "" and done == [("u-ana", "song", done[0][2])] and forgotten == ["song"]
    assert done[0][2].lossless
    assert run(FakeChoices(on=False), low, "ana", flac)[1] == ([], [])
    assert run(FakeChoices(), low, "ben", flac)[1] == ([], [])  # someone else's import
    assert run(FakeChoices(), Song("other", low.quality), "ana", flac)[1] == ([], [])
    assert run(FakeChoices(), low, "ana", "no source: none")[1] == ([], [])
    # The add-ons' audio no better than the file, or below the person's minimum.
    lossless = Song(
        "song", Quality("flac", lossless=True, kbps=900, sample_rate=44100, bit_depth=16)
    )
    assert run(FakeChoices(), lossless, "ana", flac)[1] == ([], [])
    assert run(FakeChoices(minimum=Minimum(lossless_only=True)), low, "ana", mp3)[1] == ([], [])


@pytest.mark.parametrize(("original", "addon"), [("mp3", "flac"), ("flac", "mp3"), ("m4a", "flac")])
def test_an_upgraded_file_carries_the_imported_files_tags(
    tmp_path: Path, original: str, addon: str
) -> None:
    album = Album(
        "Quiet Hollow",
        "Salt Lines",
        (Track("Brine (feat. Glass Orchard)", 3, disc=2, seconds=2.0, isrc="XXA0L2000001"),),
        fmt=original,  # type: ignore[arg-type]
        release_date="2021-04-05",
        mb_album_id="00000000-0000-4000-8000-00000000abcd",
        embedded_cover=original != "m4a",
        extra={"MUSICBRAINZ_RELEASETRACKID": ["release-track-1"]} if original == "flac" else {},
    )
    [source] = write_album(tmp_path / "o", album)
    other = replace(album, fmt=addon, title="Salt Lines (Remastered)", mb_album_id=None,  # type: ignore[arg-type]
                    tracks=(Track("Brine", 1, seconds=2.0),), extra={})  # fmt: skip
    [audio] = write_album(tmp_path / "a", other)
    target = tmp_path / "upgraded"
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(source, "rb") as s, open(audio, "rb") as a:
            retag.carry(s, a, fd)
    finally:
        os.close(fd)
    upgraded, imported = mutagen.File(target), mutagen.File(source)
    assert type(upgraded) is type(mutagen.File(audio))  # the add-on's format, unconverted
    assert retag._identity(upgraded) == retag._identity(imported)
    tags = raw_tags_of(upgraded)
    flat = " ".join(" ".join(v) for v in tags.values())
    assert "Brine (feat. Glass Orchard)" in flat and "XXA0L2000001" in flat
    assert "Remastered" not in flat
    if original == "flac":  # the cover came along (the harness embeds one in FLAC only)
        assert upgraded.tags.getall("APIC")
