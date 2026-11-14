"""An agent adds catalog albums and an artist's releases to its account's library, over a
disposable Navidrome: through the commits and the collection's record of the account's
activity, as the account's own action is, with the daily cap on adding counted for the
account. The request is kept and worked on in the background; revoking the agent, or
taking its permission away, stops what is left.

The catalog is invented (``test_C_daily_cap``): small releases served by ID, some of them
listed for an invented artist. Each test uses releases of its own."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import anyio
import pytest

from shijhon.agents.library import MAX_OPEN, MAX_RELEASES
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
)
from tests.acceptance.test_C_daily_cap import RELEASES, SONGS, album, ref
from tests.conftest import NavidromeFactory
from tests.harness.agents import call_tool, connect_agent, consent_page
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay

LENA = ("lena", "lena-password")
MILO = ("milo", "milo-password")
ARTIST = CatalogArtist(CatalogRef("demo", "capartist"), "Invented Collective")
ARTIST_ID = "sh.ar.demo.capartist"
# The artist's releases as the catalog lists them: (release, type, date).
LISTED: list[tuple[int, ReleaseKind, str | None]] = [
    (30, ReleaseKind.ALBUM, "2019"),
    (31, ReleaseKind.ALBUM, "2024-05-01"),
    (32, ReleaseKind.EP, "2024-06-01"),
    (33, ReleaseKind.ALBUM, "2023-02-01"),
    (34, ReleaseKind.SINGLE, "2025-01-01"),
    (35, ReleaseKind.ALBUM, None),
    (36, ReleaseKind.COMPILATION, "2022-03-01"),
]
World = tuple[DeliveryWorld, Browser, dict[str, str]]


def of_artist() -> tuple[CatalogRelease, ...]:
    return tuple(
        replace(RELEASES[f"cap{n}"], tracks=(), track_count=2, kind=kind, release_date=date)
        for n, kind, date in LISTED
    )


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    users = {LENA[0]: nd.create_user(*LENA), MILO[0]: nd.create_user(*MILO)}
    catalog = Replay().catalog()

    async def by_album(ident: str) -> CatalogRelease:
        if ident not in RELEASES:
            raise CatalogError("not_found", "no such album")
        return RELEASES[ident]

    async def by_song(ident: str) -> CatalogTrack:
        if ident not in SONGS:
            raise CatalogError("not_found", "no such song")
        return SONGS[ident]

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(
            albums=tuple(
                replace(r, tracks=(), track_count=2) for r in RELEASES.values() if r.title in term
            ),
            artists=(ARTIST,) if ARTIST.name in term else (),
        )

    async def artist(ident: str) -> CatalogArtist:
        if ident != ARTIST.ref.id:
            raise CatalogError("not_found", "no such artist")
        return ARTIST

    async def artist_releases(ident: str) -> tuple[CatalogRelease, ...]:
        if ident != ARTIST.ref.id:
            raise CatalogError("not_found", "no such artist")
        return of_artist()

    catalog.album, catalog.song, catalog.search = by_album, by_song, search  # type: ignore[method-assign]
    catalog.artist, catalog.artist_releases = artist, artist_releases  # type: ignore[method-assign]
    with delivery_world(
        nd, tmp_path_factory.mktemp("agent-additions"), catalog=catalog, agents=True
    ) as w:
        # (More agents connect here than one address may within the limits' window.)
        for attempts in ("register_attempts", "authorize_attempts", "login_attempts"):
            getattr(w.app.agents, attempts).limit = 10_000
        browser = Browser(w.server.base_url)
        yield w, browser, users
        browser.close()


@pytest.fixture(autouse=True)
def cap(world: World) -> Iterator[Any]:
    """No daily cap unless a test sets one; nothing counted from another test."""
    w = world[0]
    limiter = w.services.daily_cap
    assert limiter is not None
    w.server.call(lambda: w.services.store.execute("DELETE FROM release_additions"))
    limiter.limit = 0
    yield limiter
    limiter.limit = 0


def connect(world: World, account: tuple[str, str], scopes: str, name: str) -> str:
    w, browser, _ = world
    token: str = connect_agent(browser.http, w.server.base_url, *account, scopes, client_name=name)[
        "access_token"
    ]
    return token


def tool(world: World, token: str, name: str, **arguments: Any) -> dict[str, Any]:
    w, browser, _ = world
    status, answer = call_tool(browser.http, w.server.base_url, token, name, arguments)
    assert status == 200
    return answer


def finished(world: World, token: str, answer: dict[str, Any]) -> dict[str, Any]:
    """An accepted addition once the worker is done with it."""
    assert answer["ok"], answer
    addition = answer["addition"]
    deadline = time.monotonic() + 120
    while addition["state"] in ("queued", "running"):
        assert "get_library_addition" in addition["next"]
        assert time.monotonic() < deadline, "the addition did not finish"
        time.sleep(0.2)
        addition = tool(world, token, "get_library_addition", request_id=addition["id"])["addition"]
    return dict(addition)


def in_library(w: DeliveryWorld, number: int) -> bool:
    row = w.server.call(
        lambda: w.services.store.fetchone("SELECT 1 FROM releases WHERE ref = ?", [ref(number)])
    )
    return row is not None


def recorded(w: DeliveryWorld, owner: str) -> set[str]:
    """The recordings recorded as the account's activity (its Shijhon library's reasons)."""
    rows = w.server.call(
        lambda: w.services.store.fetchall(
            "SELECT a.alias FROM collection_history h JOIN collection_track_aliases a"
            " ON a.library_id=h.library_id AND a.track_key=h.track_key"
            " WHERE h.owner_id=? AND h.activity=1 AND a.alias LIKE 'catalog:%'",
            [owner],
        )
    )
    return {str(row["alias"]).removeprefix("catalog:") for row in rows}


def requests(w: DeliveryWorld) -> int:
    row = w.server.call(
        lambda: w.services.store.fetchone("SELECT COUNT(*) AS n FROM library_additions")
    )
    return int(row["n"])


def songs(number: int) -> set[str]:
    return {str(track.ref) for track in RELEASES[f"cap{number}"].tracks}


def test_an_agent_finds_previews_and_adds_albums_for_its_account(world: World) -> None:
    w, _, users = world
    token = connect(world, LENA, "add", "Adder")
    found = tool(world, token, "search_catalog", query="Capped Letters 3")
    assert found["ok"] and found["artists"] == []
    assert found["albums"] == [
        {
            "album_id": album(3),
            "title": "Capped Letters 3",
            "artist": "Invented Limit 3",
            "type": "album",
            "release_date": "2021",
            "tracks": 2,
        }
    ]
    named = tool(world, token, "search_catalog", query=ARTIST.name)
    assert named["artists"] == [{"artist_id": ARTIST_ID, "name": ARTIST.name}]

    # A preview writes nothing and keeps nothing; an album the catalog lacks is said.
    before = requests(w)
    preview = tool(
        world, token, "add_to_library", album_ids=[album(3), "sh.al.demo.nosuch"], preview=True
    )
    assert preview["ok"] and preview["preview"] is True
    assert preview["releases"][0]["title"] == "Capped Letters 3"
    assert preview["releases"][1] == {
        "album_id": "sh.al.demo.nosuch",
        "state": "not_found",
        "detail": "The catalog has no such album.",
    }
    assert preview["album_ids"] == [album(3)]  # what can be added of it, to name again
    own = songs(3) | songs(4) | songs(5) | songs(6)  # (this test's releases)
    assert not in_library(w, 3) and requests(w) == before
    assert recorded(w, users["lena"]) & own == set()

    # Added: in the library, shown to the account, and recorded as its own activity.
    asked = {"album_ids": [album(3), album(4), "sh.al.demo.nosuch"]}
    answer = tool(world, token, "add_to_library", idempotency_key="add-three-four", **asked)
    assert answer["created"] is True
    addition = finished(world, token, answer)
    assert addition["state"] == "finished"
    assert [r["state"] for r in addition["releases"]] == ["added", "added", "not_added"]
    assert addition["releases"][0]["title"] == "Capped Letters 3"
    assert addition["releases"][2]["reason"] == "not_found"
    assert (addition["added"], addition["not_added"], addition["pending"]) == (2, 1, 0)
    assert "daily_limit" not in addition and "next" not in addition
    assert in_library(w, 3) and in_library(w, 4)
    assert recorded(w, users["lena"]) & own == songs(3) | songs(4)
    assert recorded(w, users["milo"]) & own == set()
    mine = w.nd.client(*LENA).ok("search3", {"query": "Capped Letters 3", "songCount": 0})
    shown = [a["name"] for a in mine["searchResult3"]["album"]]
    assert shown.count("Capped Letters 3") == 1  # (the search finds longer titles too)

    # The same key and input: the same request, nothing asked or added again.
    kept = requests(w)
    again = tool(world, token, "add_to_library", idempotency_key="add-three-four", **asked)
    assert again["ok"] and again["created"] is False
    assert again["addition"]["id"] == addition["id"] and requests(w) == kept
    # The same key with other input is a conflict; a key is needed to add.
    other = tool(
        world, token, "add_to_library", idempotency_key="add-three-four", album_ids=[album(5)]
    )
    assert other["ok"] is False and other["error"]["code"] == "conflict"
    assert tool(world, token, "add_to_library", album_ids=[album(5)])["ok"] is False
    assert not in_library(w, 5) and requests(w) == kept

    # An album the library has joins another account's library without being written again.
    second = connect(world, MILO, "add", "Second adder")
    theirs = finished(
        world,
        second,
        tool(
            world, second, "add_to_library", album_ids=[album(3)], idempotency_key="add-three-too"
        ),
    )
    assert [r["state"] for r in theirs["releases"]] == ["already_in_library"]
    assert (theirs["added"], theirs["already_in_library"]) == (0, 1)
    assert recorded(w, users["milo"]) & own == songs(3)

    # A request is its connection's own: not another account's, nor another agent's.
    for stranger in (second, connect(world, LENA, "add status", "Other agent")):
        unknown = tool(world, stranger, "get_library_addition", request_id=addition["id"])
        assert unknown["ok"] is False and unknown["error"]["code"] == "not_found"
        assert "Capped Letters" not in str(unknown)

    # What is not a catalog album of this catalog is refused before anything is kept.
    for wrong in (["sh.tr.demo.cap6song1"], ["sh.al.other.cap6"], ["cap6"], [album(6)] * 2):
        refused = tool(
            world, token, "add_to_library", album_ids=wrong, idempotency_key="add-wrong-ids"
        )
        assert refused["ok"] is False, wrong
    many = [album(n) for n in range(1, MAX_RELEASES + 2)]
    assert tool(world, token, "add_to_library", album_ids=many, preview=True)["ok"] is False
    assert not in_library(w, 6) and requests(w) == kept + 1


def test_additions_count_for_the_accounts_daily_limit_and_say_so(world: World, cap: Any) -> None:
    w, _, users = world
    cap.limit = 2
    token = connect(world, LENA, "add", "Capped adder")
    preview = tool(world, token, "add_to_library", album_ids=[album(10)], preview=True)
    stands = preview["added_releases_per_day"]
    assert stands["limit"] == 2 and stands["left"] == 2
    answer = tool(
        world, token, "add_to_library", idempotency_key="capped-addition",
        album_ids=[album(n) for n in (10, 11, 12, 13)],
    )  # fmt: skip
    addition = finished(world, token, answer)
    assert addition["state"] == "finished"
    states = [(r["state"], r.get("reason", "")) for r in addition["releases"]]
    assert states == [
        ("added", ""),
        ("added", ""),
        ("not_added", "daily_limit"),
        ("not_added", "daily_limit"),
    ]
    assert (addition["added"], addition["not_added"]) == (2, 2)
    limit = addition["daily_limit"]
    assert limit["reached"] is True and limit["left_out"] == 2
    assert "daily limit of added releases is reached" in limit["message"].lower()
    assert "2 of 4 releases were added" in limit["message"]
    assert "daily limit" in addition["releases"][2]["detail"].lower()
    assert [in_library(w, n) for n in (10, 11, 12, 13)] == [True, True, False, False]
    assert recorded(w, users["lena"]) >= songs(10) | songs(11)
    assert not recorded(w, users["lena"]) & (songs(12) | songs(13))
    counted = w.server.call(
        lambda: w.services.store.fetchall(
            "SELECT release_ref FROM release_additions WHERE user_id=? ORDER BY release_ref",
            [users["lena"]],
        )
    )
    assert [str(r["release_ref"]) for r in counted] == [ref(10), ref(11)]
    # An administrator's additions are not limited, as on every other path.
    admin = connect(world, (ADMIN_USER, ADMIN_PASSWORD), "add", "Admin adder")
    theirs = finished(
        world,
        admin,
        tool(
            world,
            admin,
            "add_to_library",
            idempotency_key="uncapped-addition",
            album_ids=[album(n) for n in (12, 13, 14)],
        ),
    )
    assert [r["state"] for r in theirs["releases"]] == ["added"] * 3
    assert "daily_limit" not in theirs


def test_an_artist_stands_for_a_bounded_number_of_its_releases(world: World) -> None:
    w, _, users = world
    token = connect(world, LENA, "add", "Artist adder")
    # Albums only, the newest first, one without a date last.
    preview = tool(world, token, "add_to_library", artist_id=ARTIST_ID, preview=True)
    assert preview["ok"] and preview["preview"] is True and preview["artist"] == ARTIST.name
    assert [r["album_id"] for r in preview["releases"]] == [album(n) for n in (31, 33, 30, 35)]
    assert {r["type"] for r in preview["releases"]} == {"album"}
    assert preview["release_types"] == ["album"]
    assert (preview["releases_of_these_types"], preview["left_out_by_max_releases"]) == (4, 0)
    assert preview["album_ids"] == [album(n) for n in (31, 33, 30, 35)]
    assert "album_ids" in preview["note"] and "not with the artist_id" in preview["note"]
    # Other types only when asked for; never more than the call allows.
    wider = tool(
        world, token, "add_to_library", artist_id=ARTIST_ID, preview=True, max_releases=3,
        release_types=["album", "ep", "single", "compilation"],
    )  # fmt: skip
    assert [r["album_id"] for r in wider["releases"]] == [album(n) for n in (34, 32, 31)]
    assert (wider["releases_of_these_types"], wider["left_out_by_max_releases"]) == (7, 4)
    albums = [n for n, kind, _ in LISTED if kind is ReleaseKind.ALBUM]  # (this test's own)
    assert not any(in_library(w, n) for n in albums)
    for wrong in (
        {"max_releases": MAX_RELEASES + 1},
        {"max_releases": 0},
        {"release_types": ["mixtape"]},
        {"album_ids": [album(31)]},
    ):
        refused = tool(world, token, "add_to_library", artist_id=ARTIST_ID, preview=True, **wrong)
        assert refused.get("ok") is not True, wrong
    none = tool(world, token, "add_to_library", artist_id="sh.ar.demo.nobody", preview=True)
    assert none["ok"] is False and none["error"]["code"] == "not_found"

    # Added: exactly the releases the answer lists, and no others of the artist.
    answer = tool(
        world, token, "add_to_library", artist_id=ARTIST_ID, max_releases=2,
        idempotency_key="add-the-collective",
    )  # fmt: skip
    assert answer["addition"]["artist"] == ARTIST.name
    assert answer["addition"]["left_out_by_max_releases"] == 2
    addition = finished(world, token, answer)
    assert [(r["album_id"], r["state"]) for r in addition["releases"]] == [
        (album(31), "added"),
        (album(33), "added"),
    ]
    assert [n for n in albums if in_library(w, n)] == [31, 33]
    assert recorded(w, users["lena"]) >= songs(31) | songs(33)
    # Asked again, the releases the library has by now are the account's already: the
    # artist's page shows them, and they are listed with the others.
    after = tool(world, token, "add_to_library", artist_id=ARTIST_ID, preview=True)
    assert {r["album_id"] for r in after["releases"]} == {album(n) for n in (31, 33, 30, 35)}


def test_adding_needs_its_own_permission(world: World) -> None:
    w, browser, _ = world
    base = w.server.base_url
    before = requests(w)
    # Not the permissions for playlists or for reading the library.
    narrow = connect(world, LENA, "create status library history downloads settings", "Narrow")
    for name, arguments in (
        ("search_catalog", {"query": "Capped Letters 20"}),
        ("add_to_library", {"album_ids": [album(20)], "preview": True}),
        ("add_to_library", {"album_ids": [album(20)], "idempotency_key": "not-allowed-add"}),
        ("add_to_library", {"artist_id": ARTIST_ID, "idempotency_key": "not-allowed-artist"}),
    ):
        answer = tool(world, narrow, name, **arguments)
        assert answer["ok"] is False and answer["error"]["code"] == "permission_required", name
        assert answer["error"]["missing_permissions"] == ["add"]
        assert "Capped Letters" not in str(answer)
    assert not in_library(w, 20) and requests(w) == before
    feature = tool(world, narrow, "get_capabilities")["features"]["library_additions"]
    assert feature["permitted"] is False and feature["missing_permissions"] == ["add"]
    # Consent offers it cleared, with what it means, to any account.
    _, page, _ = consent_page(browser.http, base, *LENA, "create status add", "Offer")
    offered = page.split('name="scope_add"', 1)[1].split(">", 1)[0]
    assert "checked" not in offered and "changes your library" in page
    assert "checked" in page.split('name="scope_create"', 1)[1].split(">", 1)[0]
    # A connection that has it keeps it selected when it connects again.
    connect(world, LENA, "create status add", "Offer")
    _, page, _ = consent_page(browser.http, base, *LENA, "create status add", "Offer")
    feature = tool(world, connect(world, LENA, "add", "Granted"), "get_capabilities")
    assert feature["features"]["library_additions"]["ready"] is True
    assert feature["limits"]["library_addition_releases"] == MAX_RELEASES


@pytest.mark.parametrize("how", ["revoked", "permission removed", "client removed"])
def test_an_addition_stops_when_its_agent_may_no_longer_add(world: World, how: str) -> None:
    w, browser, users = world
    name = f"Stopped {how}"
    first, second, third = {
        "revoked": (21, 22, 23),
        "permission removed": (24, 25, 26),
        "client removed": (7, 8, 9),
    }[how]
    token = connect(world, LENA, "add status", name)
    commits = w.services.commits
    assert commits is not None
    native = commits.native
    reached, go = threading.Event(), threading.Event()

    async def held(cid: Any, trigger: str = "commit", **more: Any) -> str:
        if str(cid) == album(second):  # the first is added; the agent's access ends here
            reached.set()
            await anyio.to_thread.run_sync(go.wait)
        return await native(cid, trigger, **more)

    async def end() -> None:
        if how == "revoked":
            await w.services.store.execute(
                "UPDATE playlist_agent_tokens SET revoked=? WHERE name=?", [time.time(), name]
            )
        elif how == "client removed":
            await w.services.store.execute(
                "DELETE FROM agent_oauth_clients WHERE id IN"
                " (SELECT oauth_client FROM playlist_agent_tokens WHERE name=?)",
                [name],
            )
        else:
            await w.services.store.execute(
                "UPDATE playlist_agent_tokens SET scopes=? WHERE name=?",
                [json.dumps(["status"]), name],
            )

    commits.native = held  # type: ignore[method-assign]
    try:
        answer = tool(
            world, token, "add_to_library", idempotency_key=f"stopped-{first}",
            album_ids=[album(first), album(second), album(third)],
        )  # fmt: skip
        assert answer["ok"] and answer["addition"]["state"] in ("queued", "running"), answer
        ident = answer["addition"]["id"]
        assert reached.wait(60)
        w.server.call(end)
        go.set()
        deadline = time.monotonic() + 60
        while True:
            row = w.server.call(
                lambda: w.services.store.fetchone(
                    "SELECT state,reason,items FROM library_additions WHERE id=?", [ident]
                )
            )
            if row["state"] not in ("queued", "running"):
                break
            assert time.monotonic() < deadline, "the addition did not stop"
            time.sleep(0.1)
    finally:
        go.set()
        commits.native = native  # type: ignore[method-assign]
    assert row["state"] == "stopped" and row["reason"]
    states = [(item["state"], item.get("reason", "")) for item in json.loads(row["items"])]
    assert states == [("added", ""), ("not_added", "stopped"), ("not_added", "stopped")]
    assert [in_library(w, n) for n in (first, second, third)] == [True, False, False]
    assert not recorded(w, users["lena"]) & (songs(second) | songs(third))
    status, read = call_tool(
        browser.http, w.server.base_url, token, "get_library_addition", {"request_id": ident}
    )
    if how != "permission removed":
        assert status == 401 and read == {}
    else:  # (the status permission still reads it)
        assert read["addition"]["state"] == "stopped"
        assert "permission" in read["addition"]["stopped_because"]
        again = tool(world, token, "add_to_library", album_ids=[album(second)], preview=True)
        assert again["error"]["code"] == "permission_required"


def test_requests_a_restart_left_are_finished_and_an_account_has_a_few_at_once(
    world: World,
) -> None:
    w, _, users = world
    token = connect(world, LENA, "add", "Restarted")
    adder = w.app.agents.additions
    grant = w.server.call(
        lambda: w.services.store.fetchone(
            "SELECT id,library FROM playlist_agent_tokens WHERE name='Restarted'"
        )
    )

    async def leave(ident: str, state: str, numbers: list[int]) -> None:
        items = [{"album_id": album(n), "state": "pending"} for n in numbers]
        await w.services.store.execute(
            "INSERT INTO library_additions"
            "(id,owner,creator,library,request_key,input_hash,items,state,created,updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [ident, users["lena"], grant["id"], grant["library"], "key-" + ident, "",
             json.dumps(items), state, time.time(), time.time()],
        )  # fmt: skip

    # As many unfinished requests as an account may have: another is refused, unkept.
    held = threading.Event()
    tick = adder.tick

    async def waiting() -> float:
        await anyio.to_thread.run_sync(held.wait)
        return await tick()

    adder.tick = waiting  # type: ignore[method-assign]
    try:
        adder.notify()  # (the worker's next look waits for the test)
        for index in range(MAX_OPEN):
            w.server.call(
                lambda i=index: leave(f"left{i}", "running" if i == 0 else "queued", [27])
            )
        refused = tool(
            world, token, "add_to_library", album_ids=[album(28)], idempotency_key="one-too-many"
        )
        assert refused["ok"] is False and refused["error"]["code"] == "conflict"
        assert not in_library(w, 27) and not in_library(w, 28)
    finally:
        adder.tick = tick  # type: ignore[method-assign]
        held.set()
    adder.notify()
    deadline = time.monotonic() + 120
    while True:
        rows = w.server.call(
            lambda: w.services.store.fetchall(
                "SELECT state,items FROM library_additions WHERE id LIKE 'left%' ORDER BY id"
            )
        )
        if all(row["state"] == "finished" for row in rows):
            break
        assert time.monotonic() < deadline, [row["state"] for row in rows]
        time.sleep(0.2)
    done = [json.loads(row["items"])[0]["state"] for row in rows]
    assert done == ["added", "already_in_library", "already_in_library"]
    assert in_library(w, 27) and recorded(w, users["lena"]) >= songs(27)


def test_what_a_preview_listed_is_what_is_added_by_its_album_ids(world: World) -> None:
    """The catalog's list of an artist can change between a preview and the addition. An
    agent adds what the person agreed to by the preview's ``album_ids``; naming the artist
    again would take the list as it is then."""
    w, _, _ = world
    token = connect(world, LENA, "add", "Agreed adder")
    preview = tool(
        world, token, "add_to_library", artist_id=ARTIST_ID, preview=True, max_releases=1,
        release_types=["compilation", "ep"],
    )  # fmt: skip
    assert preview["album_ids"] == [album(32)]  # the newer of the two
    listing = w.services.playlists.catalog.artist_releases  # (kept for a while, then asked anew)
    LISTED.append((37, ReleaseKind.EP, "2026-01-01"))  # a newer release since the preview
    listing.cache_clear()
    try:
        now = tool(
            world, token, "add_to_library", artist_id=ARTIST_ID, preview=True, max_releases=1,
            release_types=["compilation", "ep"],
        )  # fmt: skip
        assert now["album_ids"] == [album(37)]  # what naming the artist again would add
        answer = tool(
            world, token, "add_to_library", album_ids=preview["album_ids"],
            idempotency_key="as-the-preview-listed",
        )  # fmt: skip
        addition = finished(world, token, answer)
    finally:
        LISTED.pop()
        listing.cache_clear()
    assert [(r["album_id"], r["state"]) for r in addition["releases"]] == [(album(32), "added")]
    assert in_library(w, 32) and not in_library(w, 37) and not in_library(w, 36)
