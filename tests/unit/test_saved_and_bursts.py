"""Units: per-client bursts, the library-artist index, saved discographies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    release_data,
    release_from_data,
)
from shijhon.navidrome.client import NavidromeError
from shijhon.store import Store
from shijhon.views.bursts import Burst, Bursts
from shijhon.views.discographies import Discographies
from shijhon.views.library_artists import LibraryArtists


def test_a_burst_starts_once_and_ends_after_the_pause() -> None:
    now = [0.0]
    bursts = Bursts(3, 10.0, pause_seconds=30.0, clock=lambda: now[0])
    one = ("user", "app")
    assert [bursts.note(one, w) for w in ("a", "b", "a")] == [Burst.NO] * 3  # "a" twice
    assert bursts.note(one, "c") is Burst.STARTED
    assert bursts.note(one, "d") is Burst.GOING_ON
    assert bursts.note(("user", "other app"), "e") is Burst.NO  # per client
    now[0] = 25.0  # the window is over, the pause is not
    assert bursts.bursting(one) and bursts.note(one, "f") is Burst.GOING_ON
    now[0] = 41.0
    assert not bursts.bursting(one)
    assert bursts.note(one, "g") is Burst.NO


def test_without_a_pause_a_burst_lasts_a_window() -> None:
    now = [0.0]
    bursts = Bursts(2, 10.0, clock=lambda: now[0])
    one = ("user", "app")
    bursts.note(one, "a")
    assert bursts.note(one, "b") is Burst.STARTED
    now[0] = 9.0
    assert bursts.bursting(one)
    now[0] = 10.5
    assert not bursts.bursting(one)  # not stuck after the client went quiet


class FakeNavidrome:
    def __init__(self, artists: list[dict[str, Any]]) -> None:
        self.artists = artists
        self.calls: list[list[tuple[str, str]]] = []
        self.gate: anyio.Event | None = None
        self.failing = False

    async def subsonic(self, method: str, params: Any = ()) -> dict[str, Any]:
        assert method == "getArtists"
        self.calls.append(list(params))
        if self.gate is not None:
            await self.gate.wait()
        if self.failing:
            raise NavidromeError("getArtists: down")
        return {"artists": {"index": [{"name": "X", "artist": list(self.artists)}]}}


def index(navidrome: FakeNavidrome, **kwargs: Any) -> LibraryArtists:
    return LibraryArtists(navidrome, library_id=3, **kwargs)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_the_library_artist_index() -> None:
    navidrome = FakeNavidrome(
        [
            {"id": "a1", "name": "Mara Vance", "albumCount": 2, "starred": "2026-01-01"},
            {"id": "b1", "name": "Twin"},
            {"id": "b2", "name": "TWIN"},  # the same folded name: ambiguous
        ]
    )
    artists = index(navidrome)
    assert await artists.ids() == {"maravance": "a1"}
    entry = (await artists.entries())["maravance"]
    assert entry == {"id": "a1", "name": "Mara Vance", "albumCount": 2}  # no per-user fields
    assert navidrome.calls == [[("musicFolderId", "3")]]  # Shijhon's library only
    await artists.ids()
    assert len(navidrome.calls) == 1  # kept
    artists.forget()
    navidrome.artists.append({"id": "c1", "name": "New Artist"})
    assert "newartist" in await artists.ids()


@pytest.mark.anyio
async def test_a_commit_during_a_fetch_is_not_lost() -> None:
    navidrome = FakeNavidrome([{"id": "a1", "name": "Old"}])
    artists = index(navidrome)
    navidrome.gate = anyio.Event()
    async with anyio.create_task_group() as tg:
        tg.start_soon(artists.ids)
        await anyio.sleep(0.01)
        artists.forget()  # a commit while getArtists is on its way
        navidrome.artists.append({"id": "n1", "name": "New"})
        navidrome.gate.set()
    navidrome.gate = None
    assert "new" in await artists.ids()  # fetched again, not kept for the TTL


@pytest.mark.anyio
async def test_an_old_index_answers_while_a_new_one_is_fetched() -> None:
    now = [0.0]
    navidrome = FakeNavidrome([{"id": "a1", "name": "First"}])
    started: list[Any] = []
    artists = index(navidrome, ttl=10.0, clock=lambda: now[0], spawn=started.append)
    assert await artists.ids() == {"first": "a1"}
    now[0] = 11.0
    navidrome.artists = [{"id": "b1", "name": "Second"}]
    assert await artists.ids() == {"first": "a1"}  # at once, from the old index
    assert len(started) == 1
    await started[0]()
    assert await artists.ids() == {"second": "b1"}
    navidrome.failing = True
    artists.forget()
    await artists._refresh()
    assert await artists.ids() == {"second": "b1"}  # Navidrome failed: the last index


def release_with_tracks() -> CatalogRelease:
    ref = CatalogRef("demo", "1")
    track = CatalogTrack(
        CatalogRef("demo", "11"),
        "Song",
        "Mara Vance feat. Guest",
        200_500,
        disc=2,
        number=3,
        isrc="ZZ0000000001",
        explicit=True,
        album=ref,
        artists=("Mara Vance", "Guest"),
        artist_refs=(CatalogRef("demo", "7"),),
        genres=("Pop",),
    )
    return CatalogRelease(
        ref,
        "Album",
        "Mara Vance",
        ReleaseKind.EP,
        "2020-01-02",
        tracks=(track,),
        clean=True,
        track_count=1,
        artist_refs=(CatalogRef("demo", "7"),),
        artwork_template="https://example.invalid/{w}x{h}.jpg",
    )


def test_releases_survive_a_round_trip() -> None:
    release = release_with_tracks()
    assert release_from_data(release_data(release)) == release


@pytest.mark.anyio
async def test_saved_discographies(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "db.sqlite3")
    now = [1000.0]
    saved = Discographies(store, max_age_seconds=60.0, clock=lambda: now[0])
    try:
        assert await saved.get("k") is None
        await saved.save("k", (release_with_tracks(),))
        found = await saved.get("k")
        assert found is not None and found.fresh and found.releases == (release_with_tracks(),)
        now[0] += 61.0
        old = await saved.get("k")
        assert old is not None and not old.fresh
        await store.execute("UPDATE discographies SET releases = '[{\"x\": 1}]'")
        assert await saved.get("k") is None  # unreadable: as if not saved
    finally:
        await store.close()
