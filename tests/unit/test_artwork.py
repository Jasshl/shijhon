"""Catalog covers: the disk cache of resized covers, the artwork index (also
kept on disk) and the sizes covers are fetched at."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

from shijhon.catalog.artwork import ArtworkCache, ArtworkIndex
from shijhon.catalog.base import SearchResults
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
)
from shijhon.views.covers import CoverSizes

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 1000
URL = "al:demo:900000001:600"  # a cover: the item and the size


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Fetcher:
    def __init__(self, data: bytes = JPEG, delay: float = 0.0, kind: str = "image/jpeg") -> None:
        self.data, self.delay, self.kind, self.calls = data, delay, kind, 0

    async def __call__(self) -> tuple[bytes, str]:
        self.calls += 1
        await anyio.sleep(self.delay)
        return self.data, self.kind


def _cache(tmp_path: Path, clock: Clock, max_bytes: int = 10**6) -> ArtworkCache:
    return ArtworkCache(tmp_path / "artwork", max_bytes=max_bytes, max_age_seconds=30 * 86400,
                        clock=clock)  # fmt: skip


@pytest.mark.anyio
async def test_a_cover_is_fetched_once_and_kept_on_disk(tmp_path: Path) -> None:
    clock, fetch = Clock(), Fetcher()
    cache = _cache(tmp_path, clock)
    assert await cache.get(URL, fetch) == (JPEG, "image/jpeg")
    # Another process (a restart) finds it on disk; the type comes from the bytes.
    again = _cache(tmp_path, clock)
    assert await again.get(URL, fetch) == (JPEG, "image/jpeg")
    assert fetch.calls == 1
    assert len(list((tmp_path / "artwork").glob("*/*.img"))) == 1
    # Another size is another image.
    await cache.get(URL.replace(":600", ":300"), fetch)
    assert fetch.calls == 2


@pytest.mark.anyio
async def test_concurrent_requests_share_one_fetch(tmp_path: Path) -> None:
    fetch = Fetcher(delay=0.05)
    cache = _cache(tmp_path, Clock())
    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(cache.get, URL, fetch)
    assert fetch.calls == 1


@pytest.mark.anyio
async def test_an_old_cover_is_fetched_again(tmp_path: Path) -> None:
    clock, fetch = Clock(), Fetcher()
    cache = _cache(tmp_path, clock)
    await cache.get(URL, fetch)
    [path] = (tmp_path / "artwork").glob("*/*.img")
    os.utime(path, (clock.now - 31 * 86400,) * 2)
    await cache.get(URL, fetch)
    assert fetch.calls == 2
    assert clock.now - path.stat().st_mtime < 86400  # replaced


@pytest.mark.anyio
async def test_the_oldest_covers_go_past_the_size_limit(tmp_path: Path) -> None:
    clock, fetch = Clock(), Fetcher()
    cache = _cache(tmp_path, clock, max_bytes=5 * len(JPEG))
    for n in range(8):
        clock.now += 10
        await cache.get(URL.replace("demo:", f"demo:{n}"), fetch)
    kept = sorted((tmp_path / "artwork").glob("*/*.img"), key=lambda p: p.stat().st_mtime)
    assert 1 <= len(kept) <= 5
    assert sum(p.stat().st_size for p in kept) <= 5 * len(JPEG)
    newest = cache._path(URL.replace("demo:", "demo:7"))
    assert newest in kept


@pytest.mark.anyio
async def test_expired_covers_are_removed(tmp_path: Path) -> None:
    clock, fetch = Clock(), Fetcher()
    first = _cache(tmp_path, clock)
    await first.get(URL, fetch)
    clock.now += 31 * 86400
    later = _cache(tmp_path, clock)  # e.g. after a restart: its first save removes them
    await later.get(URL.replace("demo:", "demo:b"), fetch)
    assert [p.name for p in (tmp_path / "artwork").glob("*/*.img")] == [
        later._path(URL.replace("demo:", "demo:b")).name
    ]


@pytest.mark.anyio
async def test_what_is_not_a_known_image_is_passed_on_without_keeping_it(tmp_path: Path) -> None:
    fetch = Fetcher(data=b"\x00\x00\x00\x1cftypavif", kind="image/avif")
    cache = _cache(tmp_path, Clock())
    assert await cache.get(URL, fetch) == (fetch.data, "image/avif")
    await cache.get(URL, fetch)
    assert fetch.calls == 2
    assert list((tmp_path / "artwork").glob("*/*")) == []


@pytest.mark.anyio
async def test_the_type_comes_from_the_bytes(tmp_path: Path) -> None:
    fetch = Fetcher(kind="application/octet-stream")
    assert await _cache(tmp_path, Clock()).get(URL, fetch) == (JPEG, "image/jpeg")


@pytest.mark.anyio
async def test_a_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _cache(tmp_path, Clock())

    def full(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("shijhon.catalog.artwork.os.replace", full)
    assert await cache.get(URL, Fetcher()) == (JPEG, "image/jpeg")  # served all the same
    assert list((tmp_path / "artwork").glob("*/*")) == []


@pytest.mark.anyio
async def test_old_partial_files_are_removed(tmp_path: Path) -> None:
    clock = Clock()
    cache = _cache(tmp_path, clock)
    stray = tmp_path / "artwork" / "ab" / "abcdef.123.part"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    os.utime(stray, (clock.now - 7200,) * 2)
    await cache.get(URL, Fetcher())  # the first save trims
    assert not stray.exists()


@pytest.mark.anyio
async def test_the_trim_runs_in_the_background_when_it_can(tmp_path: Path) -> None:
    spawned: list[object] = []
    cache = ArtworkCache(tmp_path / "artwork", max_bytes=10**6, max_age_seconds=86400,
                         clock=Clock(), spawn=spawned.append)  # fmt: skip
    await cache.get(URL, Fetcher())
    assert len(spawned) == 1


def test_the_index_keeps_the_artwork_of_what_was_shown() -> None:
    index = ArtworkIndex(maxsize=3)
    track = CatalogTrack(CatalogRef("demo", "t1"), "S", "A", 1000,
                           artwork_template="https://x.invalid/{w}x{h}t.jpg")  # fmt: skip
    release = CatalogRelease(
        CatalogRef("demo", "a1"), "Al", "A", ReleaseKind.ALBUM, None,
        tracks=(track,), artwork_template="https://x.invalid/{w}x{h}a.jpg",
    )  # fmt: skip
    artist = CatalogArtist(CatalogRef("demo", "r1"), "A", "https://x.invalid/{w}x{h}r.jpg")
    index.results(SearchResults(artists=(artist,), albums=(release,)))
    assert index.template("al", CatalogRef("demo", "a1")) == "https://x.invalid/{w}x{h}a.jpg"
    assert index.template("tr", CatalogRef("demo", "t1")) == "https://x.invalid/{w}x{h}t.jpg"
    assert index.template("ar", "demo:r1") == "https://x.invalid/{w}x{h}r.jpg"
    index.note("al", "demo:a2", "https://x.invalid/{w}x{h}b.jpg")  # the oldest goes
    assert index.template("ar", "demo:r1") is None


@pytest.mark.anyio
async def test_albums_and_artists_artwork_is_kept_on_disk(tmp_path: Path) -> None:
    """An album or artist shown before a restart needs no catalog request for its
    cover; songs' artwork stays in memory (a song's cover is its album's)."""
    path = tmp_path / "artwork" / "index.sqlite3"
    index = ArtworkIndex(path=path)
    album = CatalogRef("demo", "1")
    index.note("al", album, "https://covers.example.invalid/a/{w}x{h}.jpg")
    index.note("ar", CatalogRef("demo", "2"), "https://covers.example.invalid/b/{w}x{h}.jpg")
    index.note("tr", CatalogRef("demo", "3"), "https://covers.example.invalid/c/{w}x{h}.jpg")
    assert await index.find("al", album) is not None  # not written yet: from memory
    await index.flush()
    restarted = ArtworkIndex(path=path)
    assert restarted.template("al", album) is None  # nothing in memory
    assert await restarted.find("al", album) == "https://covers.example.invalid/a/{w}x{h}.jpg"
    assert restarted.template("al", album) is not None  # remembered from then on
    assert await restarted.find("ar", CatalogRef("demo", "2")) is not None
    assert await restarted.find("tr", CatalogRef("demo", "3")) is None
    restarted.forget("al", album)  # it no longer loads: asked of the catalog again
    await restarted.flush()
    assert await ArtworkIndex(path=path).find("al", album) is None


@pytest.mark.anyio
async def test_an_unusable_index_file_leaves_covers_to_memory(tmp_path: Path) -> None:
    blocker = tmp_path / "artwork"
    blocker.write_text("not a folder")
    index = ArtworkIndex(path=blocker / "index.sqlite3")
    index.note("al", CatalogRef("demo", "1"), "https://covers.example.invalid/a/{w}x{h}.jpg")
    await index.flush()  # logged once, no error
    assert await index.find("al", CatalogRef("demo", "1")) is not None
    assert await index.find("al", CatalogRef("demo", "9")) is None


def test_a_song_s_artwork_stands_for_its_unknown_album() -> None:
    index = ArtworkIndex()
    album = CatalogRef("demo", "10")
    art = "https://covers.example.invalid/s/{w}x{h}.jpg"
    song = CatalogTrack(CatalogRef("demo", "11"), "Song", "Artist", 1000, 1, 1, album=album,
                          artwork_template=art)  # fmt: skip
    index.tracks([song])
    assert index.template("al", album) == "https://covers.example.invalid/s/{w}x{h}.jpg"
    index.note("al", album, "https://covers.example.invalid/album/{w}x{h}.jpg")
    index.tracks([song])  # a known album keeps its own
    assert index.template("al", album) == "https://covers.example.invalid/album/{w}x{h}.jpg"


def test_covers_are_fetched_at_the_next_common_size_up() -> None:
    sizes = CoverSizes([100, 150, 200, 250, 300, 400, 500, 600, 800, 1000, 1200])
    assert [sizes.fetched(s) for s in ("347", "400", "90", "1300", None, "abc", "10")] == [
        400, 400, 100, 1200, 600, 600, 100
    ]  # fmt: skip
    exact = CoverSizes([])
    assert [exact.fetched(s) for s in ("347", "1300", "10", "0")] == [347, 1200, 32, 1200]
    sizes.note(("ann", "Gridview"), "290")
    assert sizes.of(("ann", "Gridview")) == 300 and sizes.of(("ann", "Other")) is None
    # The size a client asks for most often, not its last one (an album header's).
    sizes.note(("ann", "Gridview"), "290")
    sizes.note(("ann", "Gridview"), "600")
    assert sizes.of(("ann", "Gridview")) == 300
    # A hint (a claimed name, before any credential check) never overrides a verified size.
    for _ in range(5):
        sizes.note(("ann", "Gridview"), "32", verified=False)
    assert sizes.of(("ann", "Gridview")) == 300
    sizes.note(("bob", "Gridview"), "600", verified=False)
    assert sizes.of(("bob", "Gridview")) == 600  # until a verified one exists
    # A hint's name is as the client wrote it; the caller's is Navidrome's.
    sizes.note(("CAROL", "Gridview"), "600", verified=False)
    assert sizes.of(("Carol", "Gridview")) == 600
