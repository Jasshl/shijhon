"""A new installation answers apps' library lists with nobody signing in.

Shijhon prepares the library index with its own service account as it starts: a household
member's app and the administrator's get their artists, albums and searches without
anyone opening the dashboard or the native website - while that runs, only a short
"indexing" answer, never one that asks someone to sign in. Each account still sees its own
library choice, and music added to the folder later joins the lists by itself.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import PLACEHOLDER_FOLDER, DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay
from tests.harness.subsonic import SubsonicClient

LISTENER = ("household", "household-password")  # a household member, no admin
FIRST = Album(
    "Invented Lantern", "Glass Weather", (Track("Copper Morning", 1), Track("Slate Noon", 2))
)
SECOND = Album("Invented Marrow", "Tidewater Signals", (Track("Rust Waltz", 1),), "mp3")
LATER = Album("Invented Quarry", "Velvet Hours", (Track("Late Ember", 1),))
SECONDS = 60.0


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, FIRST)
    write_album(nd.music, SECOND)
    nd.scan(full=True)
    nd.create_user(*LISTENER)
    with delivery_world(nd, tmp_path_factory.mktemp("ready"), catalog=Replay().catalog()) as w:
        yield w


def answered(client: SubsonicClient, method: str, params: dict[str, Any], fmt: str) -> bytes:
    """The answer once the library is prepared; every answer before is a short "indexing"
    one (never one telling anyone to sign in)."""
    deadline = time.monotonic() + SECONDS
    while True:
        answer: httpx.Response = client.request(method, params, fmt=fmt)
        if answer.status_code == 200:
            return answer.content
        assert answer.status_code == 503, answer.text
        assert answer.headers.get("x-shijhon-view-state") == "indexing", answer.text
        assert "sign" not in answer.text.lower()
        assert time.monotonic() < deadline, "the library was not prepared in time"
        time.sleep(0.2)


def album_titles(client: SubsonicClient) -> set[str]:
    found = client.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
    return {a["name"] for a in found["albumList2"].get("album", [])}


def test_apps_get_library_lists_without_anyone_signing_in(world: DeliveryWorld) -> None:
    refresher = world.services.collection_refresh
    assert refresher is not None and refresher.automatic
    for user, password in (LISTENER, (ADMIN_USER, ADMIN_PASSWORD)):
        client = SubsonicClient(world.server.base_url, user, password)
        try:
            for fmt in ("json", "xml"):
                artists = answered(client, "getArtists", {}, fmt)
                assert b"Invented Lantern" in artists and b"Invented Marrow" in artists
                albums = answered(client, "getAlbumList2", {"type": "newest"}, fmt)
                assert b"Glass Weather" in albums and b"Tidewater Signals" in albums
                found = answered(client, "search3", {"query": "Rust Waltz"}, fmt)
                assert b"Rust Waltz" in found
        finally:
            client.close()
    # Nobody signed in: no account's own native sign-in was ever at hand.
    assert not refresher.reader._bearers
    assert refresher.problem == ""


def test_each_account_keeps_its_own_library_choice(world: DeliveryWorld) -> None:
    """The index the service account prepared is the library's; what each account sees is
    still its own choice: the household member's Shijhon library holds nothing yet."""
    listener_id = str(world.nd.login(*LISTENER)["id"])

    async def choose(profile: str) -> None:
        await world.services.store.execute(
            "INSERT INTO collection_preferences VALUES (?,?,0) ON CONFLICT(owner_id)"
            " DO UPDATE SET active_profile_id=excluded.active_profile_id",
            [listener_id, profile],
        )

    listener = SubsonicClient(world.server.base_url, *LISTENER)
    admin = world.client()
    try:
        answered(listener, "getAlbumList2", {"type": "newest"}, "json")
        world.server.call(lambda: choose("shijhon"))
        assert album_titles(listener) == set()
        assert {"Glass Weather", "Tidewater Signals"} <= album_titles(admin)
        world.server.call(lambda: choose("all"))
        assert {"Glass Weather", "Tidewater Signals"} <= album_titles(listener)
    finally:
        listener.close()
        admin.close()


def test_music_added_to_the_folder_joins_the_lists_by_itself(world: DeliveryWorld) -> None:
    refresher = world.services.collection_refresh
    assert refresher is not None
    write_album(world.nd.music, LATER)
    world.nd.scan()

    async def look() -> None:  # (the next look, without waiting for its time)
        assert refresher is not None
        refresher.interval = 0
        refresher._attempted_at = -float("inf")
        refresher._updated_at = min(refresher._updated_at, time.monotonic() - 600)
        await refresher.round()

    client = SubsonicClient(world.server.base_url, *LISTENER)
    try:
        deadline = time.monotonic() + SECONDS
        while "Velvet Hours" not in album_titles(client):
            assert time.monotonic() < deadline, "the new album did not join the lists"
            world.server.call(look)
            time.sleep(0.2)
    finally:
        client.close()


def test_shijhon_s_own_files_start_no_full_refresh_and_someone_else_s_do(
    world: DeliveryWorld,
) -> None:
    """Navidrome's scan of files in Shijhon's own folder (its placeholders) is told apart
    from one of someone else's music by the changed songs' paths, as Navidrome lists them."""
    refresher = world.services.collection_refresh
    assert refresher is not None
    refreshes: list[float] = []

    async def look() -> None:  # (the minimum gap and the settling are over)
        assert refresher is not None
        refresher.interval = 0
        refresher._attempted_at = -float("inf")
        refresher._updated_at = min(refresher._updated_at, time.monotonic() - 600)
        before = refresher._updated_at
        await refresher.round()
        if refresher._updated_at != before:
            refreshes.append(refresher._updated_at)

    world.server.call(look)  # (whatever is pending from earlier tests)
    refreshes.clear()
    write_album(
        world.nd.music / PLACEHOLDER_FOLDER,
        Album("Invented Placeholder", "Own Writes", (Track("Own One", 1),)),
    )
    world.nd.scan()
    world.server.call(look)
    assert not refreshes and refresher._pending == "own"
    theirs = write_album(
        world.nd.music, Album("Invented Neighbor", "Their Album", (Track("Theirs", 1),))
    )
    world.nd.scan()
    world.server.call(look)
    assert len(refreshes) == 1 and refresher._pending is None
    # A file deleted outside Shijhon's folders (Navidrome keeps it as missing): a change.
    theirs[0].unlink()
    world.nd.scan()
    world.server.call(look)
    assert len(refreshes) == 2 and refresher._pending is None


def test_a_refresh_that_finds_nothing_changed_keeps_the_snapshot_and_its_lists(
    world: DeliveryWorld,
) -> None:
    refresher = world.services.collection_refresh
    query_index = world.services.collection.query_index
    assert refresher is not None and refresher.service is not None and query_index is not None

    async def refresh() -> None:
        assert refresher is not None and refresher.service is not None
        principal = await refresher.service.principal()
        await world.services.collection.refresh(principal.user_id, "1", access=refresher.service)

    async def state() -> tuple[str, list[tuple[Any, ...]]]:
        row = await world.services.store.fetchone("SELECT generation FROM collection_index")
        with query_index._reader() as conn:
            lists = [
                tuple(r)
                for r in conn.execute(
                    "SELECT key,facts,updated,state FROM queries ORDER BY key"
                ).fetchall()
            ]
        return str(row["generation"]), lists

    world.server.call(refresh, timeout=120)  # (what earlier tests changed)
    client = SubsonicClient(world.server.base_url, *LISTENER)
    try:
        answered(client, "search3", {"query": "", "songCount": 500}, "json")
        answered(client, "getAlbumList2", {"type": "newest"}, "json")
        before = world.server.call(state)
        world.server.call(refresh, timeout=120)
        assert world.server.call(state) == before
        assert answered(client, "getAlbumList2", {"type": "newest"}, "json")
    finally:
        client.close()
