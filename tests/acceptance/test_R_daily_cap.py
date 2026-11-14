"""The daily cap on adding at playlists: what a playlist adds to the library is added for
its account. Songs whose releases are over the cap are left out, with the reason, and the
rest is published - for explicit tracks (an agent's, over the API) and for a described
playlist alike; explicit tracks that must all be published fail whole, adding nothing. The
dashboard's Collection page and the agent's capabilities say where the account stands,
only while a cap is set.

The catalog is invented (``test_C_daily_cap``): small releases, found by their titles.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from pydantic import SecretStr

from shijhon.agents.playlists import next_step
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogRelease, CatalogTrack
from shijhon.playlists.model import Connection, PlaylistError, Track
from shijhon.playlists.prompt import Ordered, Proposals
from shijhon.playlists.requests import LIMIT_LEFT_OUT
from shijhon.playlists.service import POSTPONED, release_fingerprint
from tests.acceptance.test_C_daily_cap import RELEASES, SLOW, SONGS, ref
from tests.acceptance.test_R_playlists import wait_state
from tests.conftest import NavidromeFactory
from tests.harness.agents import connect_agent
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay

LENA = ("lena", "lena-password")
World = tuple[DeliveryWorld, Browser, str]


def track(number: int) -> CatalogTrack:
    return RELEASES[f"cap{number}"].tracks[0]


def asked(number: int) -> dict[str, str]:
    return {"artist": track(number).artist, "title": track(number).title}


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    account = nd.create_user(*LENA)
    catalog = Replay().catalog()

    async def by_album(ident: str) -> CatalogRelease:
        if ident in SLOW:  # a catalog that takes its time over this release
            await anyio.sleep(SLOW[ident])
        if ident not in RELEASES:
            raise CatalogError("not_found", "no such album")
        return RELEASES[ident]

    async def by_song(ident: str) -> CatalogTrack:
        if ident not in SONGS:
            raise CatalogError("not_found", "no such song")
        return SONGS[ident]

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(songs=tuple(s for s in SONGS.values() if s.title in term))

    catalog.album, catalog.song, catalog.search = by_album, by_song, search  # type: ignore[method-assign]
    with delivery_world(
        nd, tmp_path_factory.mktemp("capped-playlists"), catalog=catalog, agents=True
    ) as w:
        browser = Browser(w.server.base_url)
        assert browser.sign_in(*LENA).status_code == 303
        connected = browser.submit(
            "playlists",
            {"password": LENA[1], "history_user": ""},
            "playlists/connection",
        )
        assert connected.status_code == 303, [
            line for line in connected.text.splitlines() if "notice" in line or "error" in line
        ]
        yield w, browser, account
        browser.close()


def counted(w: DeliveryWorld) -> list[str]:
    rows = w.server.call(
        lambda: w.services.store.fetchall(
            "SELECT release_ref FROM release_additions ORDER BY release_ref"
        )
    )
    return [str(r["release_ref"]) for r in rows]


def in_library(w: DeliveryWorld, number: int) -> bool:
    row = w.server.call(
        lambda: w.services.store.fetchone("SELECT 1 FROM releases WHERE ref = ?", [ref(number)])
    )
    return row is not None


@pytest.fixture(autouse=True)
def cap(world: World) -> Iterator[Any]:
    w = world[0]
    limiter = w.services.daily_cap
    assert limiter is not None
    w.server.call(lambda: w.services.store.execute("DELETE FROM release_additions"))
    limiter.limit = 2
    yield limiter
    limiter.limit = 0
    assert not limiter._held


def request(
    world: World, key: str, token: str, tracks: list[int], **more: Any
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """An agent's explicit tracks, published: (the request, its run, its status)."""
    w, browser, _ = world
    auth = {"Authorization": "Bearer " + token, "Idempotency-Key": f"daily-cap-{key}"}
    payload = {
        "name": f"Capped {key}",
        "count": len(tracks),
        "tracks": [asked(n) for n in tracks],
        **more,
    }
    response = browser.http.post("/shijhon/api/playlists/requests", headers=auth, json=payload)
    assert response.status_code == 202, response.text
    ident, run = response.json()["id"], response.json()["run"]
    finished = wait_state(w, run, ("published", "failed", "uncertain"))
    status = browser.http.get(f"/shijhon/api/playlists/requests/{ident}", headers=auth).json()
    return ident, finished, status


def tool(browser: Browser, token: str, name: str) -> dict[str, Any]:
    """An agent's tool call, answered."""
    reply = browser.http.post(
        "/shijhon/mcp",
        headers={
            "authorization": "Bearer " + token,
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
    )
    assert reply.status_code == 200, reply.text
    answer: dict[str, Any] = reply.json()["result"]["structuredContent"]
    return answer


def test_explicit_tracks_over_the_cap_are_left_out_with_the_reason(world: World) -> None:
    w, browser, _ = world
    # A release an administrator added is in the library for everyone: it needs no room.
    w.client().ok("star", {"id": f"sh.tr.demo.{track(1).ref.id}"})
    token = connect_agent(
        browser.http, str(browser.http.base_url).rstrip("/"), *LENA, scopes="create status library"
    )["access_token"]
    ident, finished, status = request(world, "partial", token, [5, 1, 2, 4, 3])
    assert finished["state"] == "published", finished["error"]
    # The cap of 2 has room for the first two new releases in the playlist's order (5 and
    # 2); the library's song is published whatever the cap; the songs after them are out.
    published = [s["title"] for s in status["selected"]]
    assert published == [track(n).title for n in (5, 1, 2)]
    added = [n for n in (2, 3, 4, 5) if in_library(w, n)]
    assert added == [2, 5] and counted(w) == [ref(2), ref(5)]
    left = [t for t in status["tracks"] if t["outcome"] == "unresolved"]
    assert len(left) == 2 and status["resolution"]["requested"] == 5
    for entry in left:
        assert entry["reason"] == "library_refused"
        assert "daily limit of added releases is reached" in entry["detail"]
    assert status["daily_limit"] == {"left_out": 2, "detail": LIMIT_LEFT_OUT}
    native = w.nd.client(*LENA).ok("getPlaylist", {"id": status["playlist_id"]})["playlist"]
    assert [s["title"] for s in native["entry"]] == published
    # The dashboard says it on the request's page.
    page = browser.get(f"playlists/describe/{ident}")
    assert page.status_code == 200
    assert "2 songs were left out of this playlist because the daily limit" in page.text

    # Tracks that must all be published: the request fails whole, saying why, adding nothing.
    _, failed, status = request(world, "whole", token, [1, 4], require_all=True)
    assert failed["state"] == "failed"
    assert "daily limit of added releases is reached" in failed["error"]
    assert not in_library(w, 4) and len(counted(w)) == 2
    assert "daily_limit" not in status

    # The agent's capabilities say where the account stands, only while a cap applies.
    capabilities = tool(browser, token, "get_capabilities")
    stands = capabilities["limits"]["added_releases_per_day"]
    assert stands.pop("next_free_at").endswith("Z")
    assert stands == {"limit": 2, "added_in_last_24_hours": 2, "left": 0}
    assert any("limits.added_releases_per_day" in note for note in capabilities["notes"])
    assert w.services.daily_cap is not None
    w.services.daily_cap.limit = 0
    capabilities = tool(browser, token, "get_capabilities")
    assert "added_releases_per_day" not in capabilities["limits"]
    assert not any("added_releases_per_day" in note for note in capabilities["notes"])


def server_ai(world: World, chosen: list[list[int]]) -> None:
    """The server's AI, set up by an administrator and chosen by the account; each of its
    selections proposes the next of ``chosen`` (releases, by number), in that order."""
    w, browser, _ = world
    service = w.services.playlists
    assert service is not None
    rounds = iter(chosen)

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        payload = json.loads(prompt)
        if schema is Proposals:
            return {
                "tracks": [
                    {
                        "artist": track(n).artist,
                        "title": track(n).title,
                        "album": RELEASES[f"cap{n}"].title,
                        "version": "",
                    }
                    for n in next(rounds)
                ]
            }
        assert schema is Ordered
        return {
            "goal": "An invented evening",
            "constraints_satisfied": True,
            "selections": [
                {"id": c["id"], "reason": "Fits the invented evening", "fit": "meets"}
                for c in payload["candidates"]
            ],
        }

    async def install() -> None:
        service.curator.api = ai  # type: ignore[method-assign]

    w.server.call(install)
    admin = Browser(w.server.base_url)
    try:
        assert admin.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        shared = admin.submit(
            "playlists",
            {
                "backend": "api",
                "endpoint": "https://model.invalid/v1",
                "model": "synthetic",
                "api_key": "invented-host-key",
            },
            "playlists/host-ai",
        )
        assert shared.status_code == 303, shared.text
    finally:
        admin.close()
    connected = browser.submit("playlists", {"ai_mode": "shared"}, "playlists/connection")
    assert connected.status_code == 303


def describe(world: World, name: str, count: int, **more: str) -> tuple[str, dict[str, Any]]:
    """A described playlist asked for on the dashboard: (the request, its first run, done)."""
    w, browser, _ = world
    service = w.services.playlists
    assert service is not None
    response = browser.submit(
        "playlists/describe",
        {
            "name": name,
            "prompt": "An invented evening",
            "count": str(count),
            "max_per_artist": "3",
            "preview": "false",
            **more,
        },
    )
    assert response.status_code == 303
    ident = response.headers["location"].rsplit("/", 1)[1]
    run = w.server.call(lambda: service.repo.runs(ident))[0]
    return ident, wait_state(w, run["id"], ("published", "failed", "uncertain", "postponed"))


def test_a_described_playlist_is_published_without_the_songs_over_the_cap(
    world: World, cap: Any
) -> None:
    w, browser, account = world
    service = w.services.playlists
    assert service is not None
    chosen = [8, 6, 7]
    cap.limit = 1
    server_ai(world, [chosen])
    ident, result = describe(world, "Capped evening", 3)
    assert result["state"] == "published", result["error"]
    # The one place goes to the release of the playlist's first song.
    first = next(n for n in chosen if result["data"]["selected"][0]["title"] == track(n).title)
    assert [n for n in chosen if in_library(w, n)] == [first] and counted(w) == [ref(first)]
    assert len(result["data"]["selected"]) == 1 and len(result["data"]["native_ids"]) == 1
    assert result["data"]["daily_limit"] == 2
    page = browser.get(f"playlists/describe/{ident}")
    assert "2 songs were left out of this playlist because the daily limit" in page.text
    status = w.server.call(lambda: service.requests.describe(account, ident))
    assert status["daily_limit"] == {"left_out": 2, "detail": LIMIT_LEFT_OUT}


def test_a_scheduled_refresh_the_cap_leaves_nothing_of_is_put_off_not_failed(
    world: World, cap: Any
) -> None:
    """A playlist that refreshes daily: when every song of a refresh needs a place the
    account's daily cap does not have, the refresh is put off - not failed. The playlist
    keeps its songs, its schedule stays on, the run says why on its page and to agents,
    and the next scheduled refresh tries again."""
    w, browser, account = world
    service = w.services.playlists
    assert service is not None
    store = service.repo.store
    server_ai(world, [[15], [16], [16]])
    cap.limit = 1
    ident, first = describe(world, "Daily evening", 1, save_instructions="true", cadence="daily")
    assert first["state"] == "published", first["error"]
    assert counted(w) == [ref(15)]  # the account's one place

    def mix() -> Any:
        return w.server.call(
            lambda: store.fetchone("SELECT * FROM playlist_mixes WHERE id=?", [ident])
        )

    def refreshed(known: set[str]) -> dict[str, Any]:
        """The next refresh the schedule starts by itself, done."""

        async def due() -> None:
            await store.execute("UPDATE playlist_mixes SET due=0 WHERE id=?", [ident])
            service.notify()

        w.server.call(due)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            runs = w.server.call(lambda: service.repo.runs(ident))
            if runs[0]["id"] not in known:
                done = ("published", "failed", "uncertain", "postponed", "discarded")
                return wait_state(w, runs[0]["id"], done)
            time.sleep(0.05)
        raise AssertionError("the schedule started no refresh")

    before = mix()
    assert before["enabled"] and before["playlist_id"]
    native = w.nd.client(*LENA)
    songs = [
        s["title"]
        for s in native.ok("getPlaylist", {"id": before["playlist_id"]})["playlist"]["entry"]
    ]
    assert songs == [track(15).title]

    put_off = refreshed({first["id"]})
    assert put_off["automatic"] and put_off["state"] == "postponed", put_off["error"]
    assert put_off["error"] == POSTPONED and "daily limit of added releases" in POSTPONED
    after = mix()
    assert after["enabled"] == 1 and after["playlist_id"] == before["playlist_id"]
    assert after["due"] > time.time() + 86000  # its next refresh is scheduled
    again = native.ok("getPlaylist", {"id": before["playlist_id"]})["playlist"]["entry"]
    assert [s["title"] for s in again] == songs
    assert not in_library(w, 16) and counted(w) == [ref(15)]
    # Said as that on its page, not as an error, and to agents.
    page = browser.get(f"playlists/describe/{ident}").text
    assert ">Put off</span>" in page and POSTPONED in page
    assert "notice-error" not in page and "Needs attention" not in page
    status = w.server.call(lambda: service.requests.describe(account, ident))
    assert status["state"] == "postponed" and status["error"] == POSTPONED
    assert status["refreshes"] is True
    step = next_step(ident, "postponed", live=True)
    assert "put off" in step and "next scheduled refresh tries again" in step
    # The next scheduled refresh tries again; with room by then, it publishes.
    cap.limit = 3
    done = refreshed({first["id"], put_off["id"]})
    assert done["automatic"] and done["state"] == "published", done["error"]
    assert in_library(w, 16) and mix()["enabled"] == 1
    now = native.ok("getPlaylist", {"id": before["playlist_id"]})["playlist"]["entry"]
    assert [s["title"] for s in now] == [track(16).title]


def test_an_administrator_demoted_between_two_releases_of_a_publication_is_counted(
    world: World, cap: Any
) -> None:
    """One publication, two releases, the second slow at the catalog: its account, an
    administrator at the first, is demoted before the second is written - which is then
    counted for it, the first is not."""
    w, _, _ = world
    service = w.services.playlists
    assert service is not None
    created = w.nd.native(
        "POST",
        "user",
        json={"userName": "boss", "name": "boss", "password": "boss-password", "isAdmin": True},
    ).json()
    boss = str(created["id"])
    numbers = [18, 19]
    selected = [
        Track(
            id=f"sh.tr.demo.{track(n).ref.id}",
            artist=track(n).artist,
            title=track(n).title,
            album=RELEASES[f"cap{n}"].title,
            duration=track(n).seconds,
            isrc=track(n).isrc or "",
            release=ref(n),
            unfamiliar=True,
        )
        for n in numbers
    ]
    approved = {ref(n): release_fingerprint(RELEASES[f"cap{n}"]) for n in numbers}

    async def publish() -> list[str]:
        auth = await service.client.login("boss", "boss-password", "127.0.0.1")
        connection = Connection(
            user_id=auth["id"],
            username=auth["username"],
            token=SecretStr(auth["subsonicToken"]),
            salt=SecretStr(auth["subsonicSalt"]),
        )
        await service.repo.connect(connection)
        # (not bound to the library: a demoted account has no library grant of its own)
        return await service.check_tracks(connection, selected, approved, partly=True)

    seconds, cap.role_seconds = cap.role_seconds, 0.3
    SLOW["cap19"] = 2.0
    try:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(lambda: w.server.call(publish))
            deadline = time.monotonic() + 20
            while not in_library(w, 18) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert in_library(w, 18) and not in_library(w, 19)
            user = w.nd.native("GET", f"user/{boss}").json()
            w.nd.native("PUT", f"user/{boss}", json={**user, "isAdmin": False})
            assert len(waiting.result(60)) == 2
        assert in_library(w, 19)
        rows = w.server.call(
            lambda: w.services.store.fetchall(
                "SELECT release_ref FROM release_additions WHERE user_id = ?", [boss]
            )
        )
        assert [str(r["release_ref"]) for r in rows] == [ref(19)]
    finally:
        SLOW.clear()
        cap.role_seconds = seconds


def test_a_playlist_import_stops_at_the_cap_and_goes_on_when_retried(
    world: World, cap: Any
) -> None:
    """An import prepares every reviewed recording or stops: at the account's daily cap it
    stops, saying why, with what fitted added; retried with room, it goes on from there."""
    w, _, account = world
    service = w.services.playlists
    assert service is not None
    numbers = [9, 10, 11]
    selected = [
        Track(
            id=f"sh.tr.demo.{track(n).ref.id}",
            artist=track(n).artist,
            title=track(n).title,
            album=RELEASES[f"cap{n}"].title,
            duration=track(n).seconds,
            isrc=track(n).isrc or "",
            release=ref(n),
            unfamiliar=True,
        )
        for n in numbers
    ]
    approved = {ref(n): release_fingerprint(RELEASES[f"cap{n}"]) for n in numbers}

    async def prepare() -> list[str]:
        connection = await service.connection(account)
        return await service.check_tracks(  # as an import's publication asks
            connection, selected, approved, library_bound=True, addons=True, partly=True
        )

    with pytest.raises(PlaylistError, match="daily limit of added releases is reached"):
        w.server.call(prepare)
    # What fitted is there, in the order of the import's songs.
    assert counted(w) == [ref(10), ref(9)] and not in_library(w, 11)
    cap.limit = 3
    ids = w.server.call(prepare)
    assert len(ids) == 3 and all(in_library(w, n) for n in numbers)
    assert counted(w) == [ref(n) for n in sorted(numbers, key=str)]


def test_the_collection_page_says_where_the_account_stands_only_under_a_cap(
    world: World, cap: Any
) -> None:
    w, browser, account = world

    async def add(count: int) -> None:
        for number in range(count):
            await w.services.store.execute(
                "INSERT INTO release_additions (user_id, release_ref, added_at) VALUES (?, ?, ?)",
                [account, f"demo:shown{number}", time.time() - 600],
            )

    cap.limit = 0
    w.server.call(lambda: add(1))
    assert "data-daily-limit" not in browser.get("collection").text  # no cap: nothing said
    cap.limit = 3
    page = browser.get("collection").text
    assert "You have added 1 of the 3 catalog releases an account may add within 24 hours." in page
    assert "You can add more from" not in page
    w.server.call(lambda: w.services.store.execute("DELETE FROM release_additions"))
    w.server.call(lambda: add(3))
    page = browser.get("collection").text
    assert "You have added 3 of the 3 catalog releases" in page and "You can add more from" in page
    # An administrator is not limited: nothing to say.
    admin = Browser(w.server.base_url)
    try:
        assert admin.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        assert "data-daily-limit" not in admin.get("collection").text
    finally:
        admin.close()
