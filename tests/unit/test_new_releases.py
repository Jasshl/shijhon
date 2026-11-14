"""New releases of followed artists (``new_releases.py``): who is followed, what a check
asks and saves, and what stays when it cannot finish - with stand-ins for Navidrome and
the catalog (the newest list itself: ``test_G_new_releases``)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogArtist, CatalogRef, CatalogRelease, ReleaseKind
from shijhon.navidrome.client import NavidromeError
from shijhon.new_releases import (
    CHECK_SECONDS,
    RETRY_SECONDS,
    NewReleases,
    NotSignedIn,
    Read,
    Unavailable,
    people,
    released,
)
from shijhon.store import Store
from shijhon.views.additions import CatalogAdditions
from shijhon.views.discographies import Discographies

pytestmark = pytest.mark.anyio

TODAY = date(2031, 5, 20)
NOW = datetime(2031, 5, 20, 12, tzinfo=UTC).timestamp()


def release(ident: str, title: str, artist: str, day: str, tracks: int = 10) -> CatalogRelease:
    return CatalogRelease(
        CatalogRef("test", ident), title, artist, ReleaseKind.ALBUM, day, track_count=tracks
    )


class Catalog:
    """Artists by name, each with releases; counts what it is asked."""

    key = "test"
    region = ""

    def __init__(self) -> None:
        self.releases: dict[str, list[CatalogRelease]] = {}
        self.searches: list[str] = []
        self.lists: list[str] = []
        self.failing: CatalogError | None = None

    def add(self, name: str, *releases: CatalogRelease) -> None:
        self.releases[name] = list(releases)

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        if self.failing is not None:
            raise self.failing
        self.searches.append(term)
        found = [n for n in self.releases if n.lower() == term.lower()]
        return SearchResults(
            artists=tuple(
                CatalogArtist(CatalogRef("test", f"a.{n}".replace(" ", "")), n) for n in found
            )
        )

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        if self.failing is not None:
            raise self.failing
        self.lists.append(artist_id)
        name = next(n for n in self.releases if f"a.{n}".replace(" ", "") == artist_id)
        return tuple(self.releases[name])


class Navidrome:
    """The service account's view: each artist's albums in the library."""

    library_id = 1

    def __init__(self) -> None:
        self.albums: dict[str, list[dict[str, Any]]] = {}
        self.down = False

    async def subsonic(self, method: str, params: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
        assert method == "getArtist"
        if self.down:
            raise NavidromeError("getArtist: ConnectError")
        ident = dict(params)["id"]
        if ident not in self.albums:
            raise NavidromeError("getArtist: Artist not found", 70)
        return {"artist": {"id": ident, "album": self.albums[ident]}}


class Account:
    """One account's favorites and recently played albums, as Navidrome answers it."""

    def __init__(self) -> None:
        self.starred: dict[str, list[dict[str, Any]]] = {}
        self.recent: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.refused = False
        self.down = False

    async def reader(self) -> Read:
        if self.refused:
            raise NotSignedIn
        if self.down:
            raise Unavailable

        async def read(method: str, params: Sequence[tuple[str, str]]) -> dict[str, Any]:
            self.calls.append(method)
            if method == "getStarred2":
                return {"starred2": self.starred}
            assert method == "getAlbumList2" and dict(params)["type"] == "recent"
            return {"albumList2": {"album": self.recent}}

        return read


class World:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.catalog = Catalog()
        self.navidrome = Navidrome()
        self.accounts: dict[str, Account] = {}
        self.now = NOW
        self.today = TODAY
        additions = CatalogAdditions(self.catalog, store, None)  # type: ignore[arg-type]
        self.releases = NewReleases(
            store,
            self.navidrome,  # type: ignore[arg-type]
            self.catalog,  # type: ignore[arg-type]
            additions,
            Discographies(store, max_age_seconds=7 * 86400, clock=lambda: self.now),
            lambda owner: self.accounts[owner].reader(),
            clock=lambda: self.now,
            today=lambda: self.today,
        )

    def account(self, owner: str) -> Account:
        return self.accounts.setdefault(owner, Account())

    def artist(self, ident: str, name: str, *albums: str) -> dict[str, str]:
        """An artist of the library, with the albums it has of them."""
        self.navidrome.albums[ident] = [
            {"id": f"{ident}.{n}", "name": title, "artist": name} for n, title in enumerate(albums)
        ]
        return {"id": ident, "name": name}

    def later(self, seconds: float) -> None:
        self.now += seconds
        self.today = datetime.fromtimestamp(self.now, UTC).date()

    async def titles(self, owner: str) -> list[str]:
        return [r.title for r in await self.releases.releases(owner)]

    async def row(self, owner: str) -> dict[str, Any]:
        found = await self.store.fetchone(
            "SELECT * FROM new_release_accounts WHERE owner_id = ?", [owner]
        )
        assert found is not None
        return dict(found)


@pytest.fixture
async def world(tmp_path: Path) -> Any:
    opened = await Store.open(tmp_path / "shijhon.sqlite3")
    try:
        yield World(opened)
    finally:
        await opened.close()


def days_ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def played(days: int) -> str:
    return datetime.fromtimestamp(NOW - days * 86400, UTC).isoformat().replace("+00:00", "Z")


def test_a_date_needs_a_month_to_be_a_day() -> None:
    assert released("2031-05-18") == date(2031, 5, 18)
    assert released("2031-05") == date(2031, 5, 1)
    assert released("2031") is None  # a year alone is never new
    assert released("2031-02-30") is None and released(None) is None and released("soon") is None


def test_the_artists_of_an_entry_are_those_it_lists_else_the_one_it_names() -> None:
    listed = {
        "artistId": "x",
        "artist": "A & B",
        "artists": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
    }
    assert people(listed) == [("a", "A"), ("b", "B")]
    assert people({"artistId": "x", "artist": "A & B"}) == [("x", "A & B")]
    assert people({"artist": "No ID"}) == []


async def test_favorites_and_the_most_played_are_followed(world: World) -> None:
    account = world.account("u1")
    account.starred = {
        "artist": [world.artist("a1", "Mira Quill")],
        "album": [{"id": "al", "artistId": "a2", "artist": "Tobin Vale"}],
        "song": [
            {
                "id": "s1",
                "artists": [world.artist("a3", "Ines Marrow"), {"id": "a1", "name": "Mira Quill"}],
            },
            {"id": "s2", "artistId": "va", "artist": "Various Artists"},
        ],
    }
    account.recent = [
        {"id": "p1", "artistId": "a4", "artist": "Halden Row", "playCount": 5, "played": played(3)},
        {
            "id": "p2",
            "artistId": "a5",
            "artist": "Sable Finch",
            "playCount": 9,
            "played": played(40),
        },
        {
            "id": "p3",
            "artistId": "a5",
            "artist": "Sable Finch",
            "playCount": 2,
            "played": played(80),
        },
        {
            "id": "p4",
            "artistId": "a6",
            "artist": "Corin Ash",
            "playCount": 50,
            "played": played(200),
        },
        {"id": "p5", "artistId": "a2", "artist": "Tobin Vale", "playCount": 7, "played": played(1)},
        {"id": "p6", "artistId": "a7", "artist": "Never Played", "playCount": 0},
        {
            "id": "p7",
            "artistId": "va",
            "artist": "Various Artists",
            "playCount": 99,
            "played": played(1),
        },
    ]
    world.releases.most_played = 2
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    followed = {f.name: (f.favorite, f.played) for f in await world.releases.artists("u1")}
    assert followed == {
        "Mira Quill": (True, False),
        "Tobin Vale": (True, True),  # a favorite album's artist, and the second most played
        "Ines Marrow": (True, False),
        "Sable Finch": (False, True),  # 11 plays of albums played within 90 days
    }  # (the most plays of all were long ago; "Various Artists" is nobody)
    assert account.calls == ["getStarred2", "getAlbumList2"]  # two requests as the account
    world.releases.most_played = 0  # favorites alone: the play list is not read
    account.calls.clear()
    world.later(CHECK_SECONDS)
    await world.releases.check("u1")
    assert account.calls == ["getStarred2"]
    assert "Sable Finch" not in {f.name for f in await world.releases.artists("u1")}


async def test_a_check_saves_what_is_new_and_missing(world: World) -> None:
    world.account("u1").starred = {"artist": [world.artist("a1", "Mira Quill", "Tin Lantern")]}
    world.catalog.add(
        "Mira Quill",
        release("r1", "Harbor Lights", "Mira Quill", days_ago(2)),
        release("r2", "Tin Lantern", "Mira Quill", days_ago(5)),  # the library has it
        release("r3", "Old Rivers", "Mira Quill", days_ago(31)),  # past the window
        release("r4", "Next Winter", "Mira Quill", days_ago(-4)),  # announced
        release("r5", "Paper Kites", "Mira Quill", "2031"),  # a year alone
        release("r6", "Glass Hours", "Mira Quill", days_ago(20)),
    )
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    assert await world.titles("u1") == ["Harbor Lights", "Glass Hours"]  # the newest first
    assert (world.catalog.searches, world.catalog.lists) == (["mira quill"], ["a.MiraQuill"])
    status = await world.releases.status("u1")
    assert (status.enabled, status.checked_at, status.state, status.artists) == (True, NOW, "", 1)
    assert (await world.row("u1"))["next_at"] == NOW + CHECK_SECONDS
    # The announced release shows from its day on, and what is past the window goes -
    # from what was saved: the catalog is not asked again within a day's check.
    world.later(5 * 3600)
    await world.releases.check("u1")
    assert len(world.catalog.lists) == 1
    world.later(4 * 86400)
    assert await world.titles("u1") == ["Next Winter", "Harbor Lights", "Glass Hours"]
    world.later(27 * 86400)
    assert await world.titles("u1") == ["Next Winter"]


async def test_a_day_asks_about_so_many_artists_those_waiting_longest_first(world: World) -> None:
    names = ["Ada North", "Bo Sand", "Cy Reed"]
    world.account("u1").starred = {
        "artist": [world.artist(f"a{n}", name) for n, name in enumerate(names)]
    }
    for name in names:
        world.catalog.add(name, release(f"r.{name[:2]}", f"{name} Live", name, days_ago(1)))
    world.releases.artists_per_day = 2
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    assert world.catalog.searches == ["ada north", "bo sand"]
    world.later(CHECK_SECONDS)
    await world.releases.check("u1")
    # The one never looked at first, then the one looked at longest ago.
    assert world.catalog.searches[2:] == ["cy reed", "ada north"]
    assert sorted(await world.titles("u1")) == [f"{name} Live" for name in names]


async def test_a_catalog_that_fails_leaves_what_was_saved(world: World) -> None:
    world.account("u1").starred = {"artist": [world.artist("a1", "Mira Quill")]}
    world.catalog.add("Mira Quill", release("r1", "Harbor Lights", "Mira Quill", days_ago(2)))
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    world.later(CHECK_SECONDS)
    world.catalog.failing = CatalogError("unavailable", "no answer")
    await world.releases.check("u1")
    assert await world.titles("u1") == ["Harbor Lights"]
    row = await world.row("u1")
    assert (row["state"], row["next_at"]) == ("unavailable", world.now + RETRY_SECONDS)
    assert row["checked_at"] == NOW  # the last check that finished
    world.catalog.failing = None
    world.later(RETRY_SECONDS)
    assert await world.releases.check_due() == 1
    assert (await world.row("u1"))["state"] == ""


async def test_navidrome_down_ends_the_check_without_losing_anything(world: World) -> None:
    world.account("u1").starred = {"artist": [world.artist("a1", "Mira Quill")]}
    world.catalog.add("Mira Quill", release("r1", "Harbor Lights", "Mira Quill", days_ago(2)))
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    world.later(CHECK_SECONDS)
    world.account("u1").down = True
    await world.releases.check("u1")
    assert (await world.row("u1"))["state"] == "unavailable"
    assert await world.titles("u1") == ["Harbor Lights"]
    assert [f.name for f in await world.releases.artists("u1")] == ["Mira Quill"]


async def test_an_account_whose_sign_in_is_refused_is_not_checked(world: World) -> None:
    world.account("u1").refused = True
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    row = await world.row("u1")
    assert (row["state"], row["checked_at"]) == ("sign_in", None)
    assert world.catalog.searches == [] and world.releases.checks == 0


async def test_an_artist_no_longer_followed_takes_its_releases_along(world: World) -> None:
    account = world.account("u1")
    account.starred = {
        "artist": [world.artist("a1", "Mira Quill"), world.artist("a2", "Tobin Vale")]
    }
    world.catalog.add("Mira Quill", release("r1", "Harbor Lights", "Mira Quill", days_ago(2)))
    world.catalog.add("Tobin Vale", release("r2", "Low Tide", "Tobin Vale", days_ago(3)))
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    assert await world.titles("u1") == ["Harbor Lights", "Low Tide"]
    account.starred = {"artist": [{"id": "a2", "name": "Tobin Vale"}]}
    world.later(CHECK_SECONDS)
    await world.releases.check("u1")
    assert await world.titles("u1") == ["Low Tide"]
    assert [f.name for f in await world.releases.artists("u1")] == ["Tobin Vale"]


async def test_each_account_has_its_own_list_and_shares_the_catalogs_answers(world: World) -> None:
    mira, tobin = world.artist("a1", "Mira Quill"), world.artist("a2", "Tobin Vale")
    world.account("u1").starred = {"artist": [mira]}
    world.account("u2").starred = {"artist": [mira, tobin]}
    world.catalog.add("Mira Quill", release("r1", "Harbor Lights", "Mira Quill", days_ago(2)))
    world.catalog.add("Tobin Vale", release("r2", "Low Tide", "Tobin Vale", days_ago(3)))
    await world.releases.turn_on("u1", "First")
    await world.releases.turn_on("u2", "second")
    assert await world.releases.check_due() == 2
    assert await world.titles("u1") == ["Harbor Lights"]
    assert await world.titles("u2") == ["Harbor Lights", "Low Tide"]
    assert world.catalog.lists == ["a.MiraQuill", "a.TobinVale"]  # each artist asked once
    assert await world.titles("u3") == []
    assert (await world.releases.status("u3")).enabled is False
    # Requests name accounts: without case, and only those that turned it on.
    assert world.releases.account_of("FIRST") == "u1"
    assert world.releases.account_of("Second") == "u2"
    assert world.releases.account_of("third") is None
    await world.releases.seen("u1", "renamed")
    assert world.releases.account_of("renamed") == "u1"
    assert world.releases.account_of("first") is None
    await world.releases.load()  # as after a restart
    assert world.releases.account_of("Renamed") == "u1"


async def test_turning_it_off_removes_what_was_kept(world: World) -> None:
    world.account("u1").starred = {"artist": [world.artist("a1", "Mira Quill")]}
    world.catalog.add("Mira Quill", release("r1", "Harbor Lights", "Mira Quill", days_ago(2)))
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    await world.releases.turn_off("u1")
    assert await world.titles("u1") == []
    assert world.releases.account_of("first") is None
    for table in ("new_release_accounts", "new_release_artists", "new_releases"):
        assert await world.store.fetchall(f"SELECT * FROM {table}") == []  # noqa: S608
    await world.releases.check("u1")  # a check of an account that turned it off: nothing
    assert await world.store.fetchall("SELECT * FROM new_release_artists") == []


async def test_the_window_of_zero_turns_it_off_for_everyone(world: World) -> None:
    world.account("u1").starred = {"artist": [world.artist("a1", "Mira Quill")]}
    world.catalog.add("Mira Quill", release("r1", "Harbor Lights", "Mira Quill", days_ago(2)))
    await world.releases.turn_on("u1", "First")
    await world.releases.check("u1")
    world.releases.days = 0
    assert world.releases.available is False
    assert await world.titles("u1") == []
    world.later(CHECK_SECONDS)
    await world.releases.check("u1")
    assert world.catalog.searches == ["mira quill"]  # nothing asked again
    world.releases.days = 30
    assert await world.titles("u1") == ["Harbor Lights"]


async def test_one_accounts_failure_does_not_hold_back_the_others(world: World) -> None:
    mira = world.artist("a1", "Mira Quill")
    world.account("u1").starred = {"artist": [mira]}
    world.account("u2").starred = {"artist": [mira]}
    world.catalog.add("Mira Quill", release("r1", "Harbor Lights", "Mira Quill", days_ago(2)))
    await world.releases.turn_on("u1", "First")
    await world.releases.turn_on("u2", "Second")
    inner = world.releases._keep_artists

    async def broken(owner: str, followed: Any) -> None:
        if owner == "u1":
            raise RuntimeError("unexpected")
        await inner(owner, followed)

    world.releases._keep_artists = broken  # type: ignore[method-assign]
    assert await world.releases.check_due() == 2
    assert await world.titles("u2") == ["Harbor Lights"]
    row = await world.row("u1")
    assert (row["state"], row["next_at"]) == ("unavailable", NOW + RETRY_SECONDS)
    world.releases._keep_artists = inner  # type: ignore[method-assign]
    world.later(RETRY_SECONDS)
    assert await world.releases.check_due() == 1  # only the one that failed is due
    assert await world.titles("u1") == ["Harbor Lights"]
