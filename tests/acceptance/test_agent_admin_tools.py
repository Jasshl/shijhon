"""An administrator's agent: the Library's review list read and settled through the
dashboard's own actions, and the server's status, over a disposable Navidrome with the demo
catalog's records replayed (the albums of suite I's review list, as in ``test_Q_library``).

Above all, who may: an account that is a Navidrome administrator at the moment of the
call, whose grant holds the permission. Each test uses albums of its own."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest

from shijhon.agents.admin import NOT_ADMIN
from shijhon.delivery.sources import Attention
from tests.acceptance.test_Q_library import _reviewed, album_id, outcome, wait_for_result
from tests.conftest import NavidromeFactory
from tests.harness.agents import call_tool, connect_agent, consent_page
from tests.harness.collection import prepare_collection
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay

FILLED = _reviewed("Northern Hits")
KEPT = _reviewed("Northern Hits Two")
REMATCHED = _reviewed("Northern Hits Three")
LISTED = _reviewed("Northern Hits Four")  # left as it is
REVOKED = _reviewed("Northern Hits Five")
DEMOTED = _reviewed("Northern Hits Six")
WAITING = _reviewed("Northern Hits Seven")  # its actions wait; left in the list
RACED = _reviewed("Northern Hits Eight")
RACED_OTHER = _reviewed("Northern Hits Nine")
REPLAYED = _reviewed("Northern Hits Ten")
UNREGISTERED = _reviewed("Northern Hits Eleven")
ALBUMS = [
    FILLED,
    KEPT,
    REMATCHED,
    LISTED,
    REVOKED,
    DEMOTED,
    WAITING,
    RACED,
    RACED_OTHER,
    REPLAYED,
    UNREGISTERED,
]
RELEASE = "demo:900000101"
LISTENER = ("listener", "listener-password")
DEPUTY = ("deputy", "deputy-password")
World = tuple[DeliveryWorld, Browser, dict[str, str]]


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    for album in ALBUMS:
        write_album(nd.music, album)
    nd.scan(full=True)
    users = {LISTENER[0]: nd.create_user(*LISTENER), DEPUTY[0]: nd.create_user(*DEPUTY)}
    # A second administrator, to demote and promote (made one after it has its library).
    deputy = nd.native("GET", f"user/{users[DEPUTY[0]]}").json()
    nd.native("PUT", f"user/{users[DEPUTY[0]]}", json={**deputy, "isAdmin": True})
    fill = {
        "enabled": True,
        "open_budget_seconds": 15,
        "background_pause_seconds": 0,
        "auto_min_songs": 1,
        "library_pass": "off",
        "pass_requests_per_second": 50,
    }
    with delivery_world(
        nd, tmp_path_factory.mktemp("agent-admin"), catalog=Replay().catalog(), fill=fill,
        agents=True,
    ) as w:  # fmt: skip
        w.app.dashboard.attempts.limit = 10_000
        prepare_collection(nd, w.server)
        for index, album in enumerate(ALBUMS):  # viewed: matched, to the review list
            w.client(client=f"viewer-{index}").ok("getAlbum", {"id": album_id(w, album.title)})
        deadline = time.monotonic() + 60
        while any(outcome(w, album) != "review" for album in ALBUMS):
            assert time.monotonic() < deadline, "the albums did not reach the review list"
            time.sleep(0.2)
        # (More agents connect here than one address may within the limits' window.)
        for attempts in ("register_attempts", "authorize_attempts", "login_attempts"):
            getattr(w.app.agents, attempts).limit = 10_000
        browser = Browser(w.server.base_url)
        yield w, browser, users
        browser.close()


def connect(world: World, account: tuple[str, str], scopes: str, name: str) -> dict[str, Any]:
    w, browser, _ = world
    return connect_agent(browser.http, w.server.base_url, *account, scopes, client_name=name)


def tool(world: World, token: str, name: str, **arguments: Any) -> dict[str, Any]:
    w, browser, _ = world
    status, answer = call_tool(browser.http, w.server.base_url, token, name, arguments)
    assert status == 200
    return answer


def tool_later(
    pool: ThreadPoolExecutor, world: World, token: str, name: str, **arguments: Any
) -> Future[dict[str, Any]]:
    """A tool call under way in a thread of its own (with its own connection)."""
    w = world[0]

    def call() -> dict[str, Any]:
        with httpx.Client(timeout=60) as http:
            status, answer = call_tool(http, w.server.base_url, token, name, arguments)
        return {"status": status, **answer}

    return pool.submit(call)


class LockHeld:
    """One of the fills' locks held in the app's own loop until released: what an action
    for that album waits for."""

    def __init__(self, w: DeliveryWorld, key: str) -> None:
        fills = w.services.fills
        assert fills is not None
        self.release = threading.Event()
        taken = threading.Event()

        async def hold() -> None:
            async with fills._locks.hold(key):
                taken.set()
                await anyio.to_thread.run_sync(self.release.wait)

        self.task = asyncio.run_coroutine_threadsafe(hold(), w.server.config.app.loop)
        assert taken.wait(30)

    def free(self) -> None:
        self.release.set()
        self.task.result(30)


def end_access(world: World, how: str, name: str) -> None:
    """The agent connection ``name`` may no longer act, from now on."""
    w = world[0]
    store = w.services.store
    if how == "demoted":
        set_admin(world, DEPUTY[0], False)
        return
    sql = {
        "revoked": "UPDATE playlist_agent_tokens SET revoked=1 WHERE name=?",
        "permission removed": "UPDATE playlist_agent_tokens SET scopes='[\"status\"]' WHERE name=?",
        "client removed": "DELETE FROM agent_oauth_clients WHERE id IN"
        " (SELECT oauth_client FROM playlist_agent_tokens WHERE name=?)",
    }[how]
    w.server.call(lambda: store.execute(sql, [name]))


def match_row(w: DeliveryWorld, ident: str) -> dict[str, Any] | None:
    row = w.server.call(
        lambda: w.services.store.fetchone(
            "SELECT * FROM album_matches WHERE album_id = ? AND scope = 'demo.xx'", [ident]
        )
    )
    return dict(row) if row is not None else None


def settled(world: World, token: str, album: str) -> dict[str, Any]:
    """The album's last action once it is no longer running."""
    deadline = time.monotonic() + 90
    while True:
        answer = tool(world, token, "get_review_list", album_id=album)
        if answer.get("last_action", {}).get("state") != "running":
            return answer
        assert time.monotonic() < deadline, "the background action did not finish"
        time.sleep(0.2)


def set_admin(world: World, user: str, admin: bool) -> None:
    w, _, users = world
    account = w.nd.native("GET", f"user/{users[user]}").json()
    w.nd.native("PUT", f"user/{users[user]}", json={**account, "isAdmin": admin})


def test_an_administrators_agent_reads_and_settles_the_review_list(world: World) -> None:
    w, _, _ = world
    token = connect(world, (ADMIN_USER, ADMIN_PASSWORD), "admin", "Settler")["access_token"]
    listed = tool(world, token, "get_review_list")
    assert listed["ok"] and listed["total"] >= 4 and listed["page"] == 1
    by_title = {album["title"]: album for album in listed["albums"]}
    row = by_title[LISTED.title]
    assert row["artist"] == "Esme Ashdown" and row["album_id"] == album_id(w, LISTED.title)
    assert row["why"] == "Only on releases with other titles" and row["detail"]
    assert row["owned_songs"] == 1 and not row["part_of_another_album"]
    # The releases offered, described as the page describes them (title, year, songs).
    assert RELEASE in [r["release"] for r in row["releases"]]
    assert all(", " in r["description"] and "song" in r["description"] for r in row["releases"])

    # Keep as is: done at once, through the page's own action.
    kept = album_id(w, KEPT.title)
    answer = tool(
        world, token, "settle_review_album", album_id=kept, action="keep",
        idempotency_key="keep-northern-two",
    )  # fmt: skip
    assert answer["ok"] and answer["result"]["state"] == "kept", answer
    assert "is kept as it is" in answer["result"]["message"]
    assert outcome(w, KEPT) == "kept"
    assert tool(world, token, "get_review_list", album_id=kept)["in_review_list"] is False

    # Fill from a named release: in the background, with the page's own result.
    filled = album_id(w, FILLED.title)
    answer = tool(
        world, token, "settle_review_album", album_id=filled, action="fill", release=RELEASE,
        idempotency_key="fill-northern-hits",
    )  # fmt: skip
    assert answer["ok"] and answer["result"]["state"] in ("running", "finished"), answer
    if answer["result"]["state"] == "running":
        assert "get_review_list" in answer["next"]
    after = settled(world, token, filled)
    assert after["in_review_list"] is False
    assert after["last_action"]["state"] == "finished"
    assert "filled from" in after["last_action"]["message"]
    assert "13 songs added" in after["last_action"]["message"]
    assert outcome(w, FILLED) == "filled"
    # The same key does not start the action again; other input under it is refused.
    again = tool(
        world, token, "settle_review_album", album_id=filled, action="fill", release=RELEASE,
        idempotency_key="fill-northern-hits",
    )  # fmt: skip
    assert again["ok"] and again["replayed"] is True and "next" not in again
    assert again["result"] == after["last_action"]  # the result of the action it started
    other = tool(
        world, token, "settle_review_album", album_id=filled, action="keep",
        idempotency_key="fill-northern-hits",
    )  # fmt: skip
    assert other["ok"] is False and other["error"]["code"] == "conflict"

    # Only a release the album lists; a fill needs one, a keep takes none.
    wrong = tool(
        world, token, "settle_review_album", album_id=album_id(w, LISTED.title), action="fill",
        release="demo:999999999", idempotency_key="fill-unlisted-one",
    )  # fmt: skip
    assert wrong["ok"] and wrong["result"]["state"] == "refused"
    assert "choose one of the listed releases" in wrong["result"]["message"]
    for arguments in (
        {"action": "fill"},
        {"action": "keep", "release": RELEASE},
        {"action": "remove"},
    ):
        refused = tool(
            world, token, "settle_review_album", album_id=album_id(w, LISTED.title),
            idempotency_key="invalid-input-1", **arguments,
        )  # fmt: skip
        assert refused.get("ok") is not True
    assert outcome(w, LISTED) == "review"

    # Match again: no files added, and the page's sentence about the new result.
    rematched = album_id(w, REMATCHED.title)
    tool(
        world, token, "settle_review_album", album_id=rematched, action="rematch",
        idempotency_key="rematch-northern-three",
    )  # fmt: skip
    after = settled(world, token, rematched)
    assert "still needs review" in after["last_action"]["message"]
    assert outcome(w, REMATCHED) == "review"


def test_the_status_says_what_needs_an_administrator_and_nothing_private(world: World) -> None:
    w, _, _ = world
    token = connect(world, (ADMIN_USER, ADMIN_PASSWORD), "admin", "Reader")["access_token"]
    addon = w.addon("status-addon")
    source = w.add_source(addon)
    answer = tool(world, token, "get_server_status")
    assert answer["ok"], answer
    assert answer["versions"]["shijhon"] and answer["versions"]["navidrome"]
    assert answer["versions"]["database_schema"] == answer["versions"]["database_schema_expected"]
    assert answer["navidrome"]["state"] == "answering" and answer["navidrome"]["albums"] >= 1
    assert answer["catalog"]["state"] in ("ok", "resting")
    assert answer["review_list"]["albums"] >= 1
    assert any("review list" in line for line in answer["needs_attention"])
    assert answer["cleanup"]["mode"] in ("off", "dry_run", "on")
    assert answer["restart"] == {"pending": False, "settings": []}
    assert [a["name"] for a in answer["addons"]] == ["status-addon"]
    # No address, credential or account: not the add-on's, Navidrome's or the catalog's.
    text = json.dumps(answer)
    assert addon.base_url not in text and "127.0.0.1" not in text and "://" not in text
    assert ADMIN_USER not in text and ADMIN_PASSWORD not in text
    assert LISTENER[0] not in text and DEPUTY[0] not in text

    # An add-on that needs its operator names its page only when that is a plain address.
    sources = w.services.sources

    def needing(page: str | None) -> dict[str, Any]:
        sources._attention[source] = Attention("sign_in", page, time.time())
        [item] = tool(world, token, "get_server_status")["addons"]
        return dict(item)

    try:
        plain = needing("https://addon.invalid/sign-in")
        assert plain == {
            "name": "status-addon",
            "state": "needs_sign_in",
            "sign_in_page": "https://addon.invalid/sign-in",
        }
        for private in (
            "https://addon.invalid/sign-in?key=secret-value",
            "https://addon.invalid/sign-in#secret-value",
            "https://someone:secret-value@addon.invalid/sign-in",
            "https://someone@addon.invalid/sign-in",
            "https://addon.invalid/sign-in?",
        ):
            item = needing(private)
            assert item == {"name": "status-addon", "state": "needs_sign_in"}, private
        said = tool(world, token, "get_server_status")
        assert any("status-addon needs its operator" in line for line in said["needs_attention"])
        assert "secret-value" not in json.dumps(said)
    finally:
        sources._attention.pop(source, None)


def test_administration_needs_the_permission_and_an_administrator_now(world: World) -> None:
    w, browser, users = world
    base = w.server.base_url
    store = w.services.store
    review = album_id(w, DEMOTED.title)
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_review_list", {}),
        ("get_review_list", {"album_id": review}),
        ("settle_review_album", {"album_id": review, "action": "keep",
                                 "idempotency_key": "not-allowed-keep"}),
        ("get_server_status", {}),
    ]  # fmt: skip

    def refused(token: str, code: str) -> None:
        for name, arguments in calls:
            answer = tool(world, token, name, **arguments)
            assert answer["ok"] is False and answer["error"]["code"] == code, (name, answer)
            # Nothing of the installation in a refusal.
            assert "Esme Ashdown" not in str(answer) and "versions" not in answer
        assert outcome(w, DEMOTED) == "review"

    # An administrator's grant without the permission: said, with what is missing.
    narrow = connect(world, (ADMIN_USER, ADMIN_PASSWORD), "create status library", "Narrow")
    refused(narrow["access_token"], "permission_required")
    missing = tool(world, narrow["access_token"], "get_server_status")["error"]
    assert missing["missing_permissions"] == ["admin"]
    feature = tool(world, narrow["access_token"], "get_capabilities")["features"]
    assert feature["server_administration"]["permitted"] is False
    assert feature["server_administration"]["ready"] is False

    # An account that is no administrator is not offered the permission, and cannot take
    # it by sending the field anyway.
    location, page, csrf = consent_page(
        browser.http, base, *LISTENER, "create status admin", "Listener's agent"
    )
    assert 'name="scope_create"' in page and 'name="scope_admin"' not in page
    forced = browser.http.post(
        location,
        data={"csrf": csrf, "action": "allow", "scope_create": "true", "scope_admin": "true"},
    )
    assert forced.status_code == 400 and "agent-return" not in forced.text
    # One whose grant says so all the same (as if it had been an administrator): refused
    # at the call, by what Navidrome says now.
    guest = connect(world, LISTENER, "create status", "Guest")

    async def widen() -> None:
        scopes = json.dumps(["create", "status", "admin"])
        await store.execute(
            "UPDATE playlist_agent_tokens SET scopes=? WHERE owner=?", [scopes, users["listener"]]
        )
        await store.execute(
            "UPDATE agent_oauth_tokens SET scopes=? WHERE grant_id IN"
            " (SELECT id FROM playlist_agent_tokens WHERE owner=?)",
            [scopes, users["listener"]],
        )

    w.server.call(widen)
    assert "admin" in tool(world, guest["access_token"], "get_capabilities")["permissions"]
    refused(guest["access_token"], "administrator_required")
    feature = tool(world, guest["access_token"], "get_capabilities")["features"]
    assert feature["server_administration"]["administrator"] is False
    assert feature["server_administration"]["ready"] is False

    # An administrator is offered it, cleared; with it, the tools work - until the account
    # is demoted in Navidrome, with no new sign-in in between, and again once it is
    # promoted back.
    _, page, _ = consent_page(browser.http, base, *DEPUTY, "create status admin", "Offer")
    offered = page.split('name="scope_admin"', 1)[1].split(">", 1)[0]
    assert "checked" not in offered and "Navidrome administrator" in page
    deputy = connect(world, DEPUTY, "create status admin", "Deputy")["access_token"]
    assert tool(world, deputy, "get_server_status")["ok"] is True
    assert tool(world, deputy, "get_review_list")["ok"] is True
    feature = tool(world, deputy, "get_capabilities")["features"]["server_administration"]
    assert feature["administrator"] is True and feature["ready"] is True
    set_admin(world, DEPUTY[0], False)
    try:
        refused(deputy, "administrator_required")
        feature = tool(world, deputy, "get_capabilities")["features"]["server_administration"]
        assert feature["permitted"] is True and feature["administrator"] is False
        assert feature["ready"] is False
    finally:
        set_admin(world, DEPUTY[0], True)
    assert tool(world, deputy, "get_server_status")["ok"] is True

    # A revoked grant: no tool answers.
    revoking = connect(world, DEPUTY, "create status admin", "Revoked")["access_token"]
    assert tool(world, revoking, "get_server_status")["ok"] is True

    async def revoke() -> None:
        await store.execute(
            "UPDATE playlist_agent_tokens SET revoked=? WHERE name='Revoked'", [time.time()]
        )

    w.server.call(revoke)
    for name, arguments in calls:
        status, answer = call_tool(browser.http, base, revoking, name, arguments)
        assert status == 401 and answer == {}, name
    assert outcome(w, DEMOTED) == "review"


@pytest.mark.parametrize("how", ["revoked", "demoted", "client removed"])
def test_a_fill_under_way_stops_when_the_agent_may_no_longer_act(world: World, how: str) -> None:
    w, _, _ = world
    album = {"revoked": REVOKED, "demoted": DEMOTED, "client removed": UNREGISTERED}[how]
    name = f"Stopped {how}"
    token = connect(world, DEPUTY, "admin", name)["access_token"]
    fills = w.services.fills
    assert fills is not None
    ident = album_id(w, album.title)
    # A release no test fills an album from: one release fills one album only.
    [row] = [a for a in tool(world, token, "get_review_list")["albums"] if a["album_id"] == ident]
    release = next(r["release"] for r in row["releases"] if r["release"] != RELEASE)
    go = threading.Event()
    choose = fills.choose

    async def held(album_id: str, release_ref: str, **more: Any) -> Any:
        # The fill has started; the agent's access ends before it writes anything.
        await anyio.to_thread.run_sync(go.wait)
        return await choose(album_id, release_ref, **more)

    async def placeholders() -> int:
        row = await w.services.store.fetchone("SELECT COUNT(*) AS n FROM placeholders")
        return int(row["n"]) if row else 0

    before = w.server.call(placeholders)
    fills.choose = held  # type: ignore[method-assign]
    try:
        answer = tool(
            world, token, "settle_review_album", album_id=ident, action="fill",
            release=release, idempotency_key=f"fill-while-{how}".replace(" ", "-"),
        )  # fmt: skip
        assert answer["ok"] and answer["result"]["state"] == "running", answer
        end_access(world, how, name)
        go.set()
        wait_for_result(w, album)
    finally:
        go.set()
        fills.choose = choose  # type: ignore[method-assign]
        set_admin(world, DEPUTY[0], True)
    job = w.app.dashboard.jobs.latest(ident)
    assert job is not None and job.ok is False and NOT_ADMIN in job.message, job
    assert outcome(w, album) == "review" and w.server.call(placeholders) == before


def test_a_fill_outlasts_the_token_it_was_asked_with(world: World) -> None:
    """Tokens are replaced every hour; the agent's grant, not the call's token, is what a
    fill under way is checked against."""
    w, _, _ = world
    token = connect(world, DEPUTY, "admin", "Refreshed")["access_token"]
    fills = w.services.fills
    assert fills is not None
    ident = album_id(w, LISTED.title)
    go = threading.Event()
    choose = fills.choose

    async def held(album_id: str, release_ref: str, **more: Any) -> Any:
        await anyio.to_thread.run_sync(go.wait)
        await w.services.engine._writable()  # where a fill asks before it writes
        await fills._decidable()  # ... and before it records a decision
        return SimpleNamespace(outcome="complete", added=0)

    async def replace_token() -> None:
        await w.services.store.execute(
            "DELETE FROM agent_oauth_tokens WHERE kind='access' AND grant_id IN"
            " (SELECT id FROM playlist_agent_tokens WHERE name='Refreshed')"
        )

    fills.choose = held  # type: ignore[method-assign]
    try:
        answer = tool(
            world, token, "settle_review_album", album_id=ident, action="fill",
            release=RELEASE, idempotency_key="fill-past-its-token",
        )  # fmt: skip
        assert answer["ok"] and answer["result"]["state"] == "running", answer
        w.server.call(replace_token)
        go.set()
        wait_for_result(w, LISTED)
    finally:
        go.set()
        fills.choose = choose  # type: ignore[method-assign]
    job = w.app.dashboard.jobs.latest(ident)
    assert job is not None and job.ok is True and "it is complete" in job.message, job
    assert outcome(w, LISTED) == "review"


WAITS = ["keep", "rematch before forgetting", "rematch before recording"]


@pytest.mark.parametrize("wait", WAITS)
@pytest.mark.parametrize("how", ["revoked", "permission removed", "demoted"])
def test_a_decision_is_not_changed_once_the_agent_may_no_longer_act(
    world: World, how: str, wait: str
) -> None:
    """A keep and a match again wait - for the album's locks, for Navidrome, for the
    catalog - and are asked about again after each wait, immediately before a decision is
    forgotten or recorded: an agent whose access ended meanwhile changes nothing more."""
    w, _, _ = world
    name = f"Waiting {how} {wait}"
    token = connect(world, DEPUTY, "admin", name)["access_token"]
    fills = w.services.fills
    assert fills is not None
    ident = album_id(w, WAITING.title)
    before = match_row(w, ident)
    assert before is not None and before["outcome"] == "review"
    action = "keep" if wait == "keep" else "rematch"
    entered, go = threading.Event(), threading.Event()
    original = {"keep": fills.keep, "rematch": fills.rematch, "group": fills._match_group}

    async def keep(album: str, **more: Any) -> Any:
        entered.set()  # asked about before it started: it waits for the album's lock now
        return await original["keep"](album, **more)

    async def rematch(album: str, **more: Any) -> Any:
        if wait == "rematch before forgetting":
            entered.set()
        return await original["rematch"](album, **more)

    async def match_group(*args: Any, **more: Any) -> Any:
        if wait == "rematch before recording":  # the old match is forgotten: the catalog
            entered.set()
            await anyio.to_thread.run_sync(go.wait)
        return await original["group"](*args, **more)

    fills.keep, fills.rematch, fills._match_group = keep, rematch, match_group  # type: ignore[method-assign]
    lock = LockHeld(w, ident) if wait != "rematch before recording" else None
    try:
        with ThreadPoolExecutor(1) as pool:
            call = tool_later(
                pool, world, token, "settle_review_album", album_id=ident, action=action,
                idempotency_key=f"wait-{how}-{wait}".replace(" ", "-"),
            )  # fmt: skip
            try:
                assert entered.wait(60), "the action did not start"
                end_access(world, how, name)
            finally:
                go.set()
                if lock is not None:
                    lock.free()
            answer = call.result(90)
        wait_for_result(w, WAITING)
        job = w.app.dashboard.jobs.latest(ident)
        after = match_row(w, ident)
        if wait == "rematch before recording":
            # Forgotten while the agent might; the new match was not recorded.
            assert after is None
        else:
            assert after == before
        if action == "rematch":
            assert job is not None and job.ok is False and NOT_ADMIN in job.message, job
        # The call itself ends refused: by then the agent may not even read its result.
        assert answer["status"] == 401 or answer["ok"] is False, answer
        assert "is kept as it is" not in str(answer)
    finally:
        go.set()
        fills.keep, fills.rematch, fills._match_group = (  # type: ignore[method-assign]
            original["keep"],
            original["rematch"],
            original["group"],
        )
        set_admin(world, DEPUTY[0], True)
        # The album as every test finds it: matched again by a view, in the review list.
        if match_row(w, ident) is None:
            w.client(client="restoring").ok("getAlbum", {"id": ident})
        deadline = time.monotonic() + 60
        while outcome(w, WAITING) != "review":
            assert time.monotonic() < deadline, "the album did not return to the review list"
            time.sleep(0.2)


def test_one_key_stands_for_one_action_also_for_calls_at_the_same_time(world: World) -> None:
    w, _, _ = world
    token = connect(world, (ADMIN_USER, ADMIN_PASSWORD), "admin", "Racing")["access_token"]
    fills = w.services.fills
    assert fills is not None
    first, other = album_id(w, RACED.title), album_id(w, RACED_OTHER.title)
    keep = fills.keep
    entered = threading.Event()
    kept: list[str] = []

    async def counted(album: str, **more: Any) -> Any:
        kept.append(album)
        entered.set()
        return await keep(album, **more)

    fills.keep = counted  # type: ignore[method-assign]
    lock = LockHeld(w, first)
    asked = {"action": "keep", "idempotency_key": "one-key-two-calls"}
    try:
        with ThreadPoolExecutor(3) as pool:
            under_way = tool_later(
                pool, world, token, "settle_review_album", album_id=first, **asked
            )
            try:
                assert entered.wait(60), "the first call did not start"
                # Other input under the key while the first call is under way: refused,
                # and that album is left alone.
                refused = tool(world, token, "settle_review_album", album_id=other, **asked)
                assert refused["ok"] is False and refused["error"]["code"] == "conflict"
                # The same input joins the first call: it starts nothing of its own.
                joined = tool_later(
                    pool, world, token, "settle_review_album", album_id=first, **asked
                )
                time.sleep(1.0)
                assert not joined.done()
            finally:
                lock.free()
            done, same = under_way.result(60), joined.result(60)
    finally:
        fills.keep = keep  # type: ignore[method-assign]
    assert done["ok"] and done["result"]["state"] == "kept" and "replayed" not in done
    assert same["ok"] and same["replayed"] is True and same["result"] == done["result"]
    assert kept == [first]
    assert outcome(w, RACED) == "kept" and outcome(w, RACED_OTHER) == "review"


def test_a_key_keeps_its_own_result_for_an_hour_and_until_a_restart(world: World) -> None:
    w, _, _ = world
    token = connect(world, (ADMIN_USER, ADMIN_PASSWORD), "admin", "Replaying")["access_token"]
    ident = album_id(w, REPLAYED.title)
    wrong = {"album_id": ident, "action": "fill", "release": "demo:999999999",
             "idempotency_key": "replayed-refusal"}  # fmt: skip
    again = {"album_id": ident, "action": "rematch", "idempotency_key": "replayed-rematch"}
    refused = tool(world, token, "settle_review_album", **wrong)
    assert refused["result"]["state"] == "refused"
    tool(world, token, "settle_review_album", **again)
    rematched = settled(world, token, ident)["last_action"]
    assert "still needs review" in rematched["message"]
    # Each key answers with what its own call did, not with the album's latest action.
    replay = tool(world, token, "settle_review_album", **wrong)
    assert replay["replayed"] is True and replay["result"] == refused["result"]
    replay = tool(world, token, "settle_review_album", **again)
    assert replay["replayed"] is True and replay["result"] == rematched
    # An uncertain result is settled by reading the album: a match again can leave it in
    # the list, so its last action says how it went.
    read = tool(world, token, "get_review_list", album_id=ident)
    assert read["in_review_list"] is True and read["last_action"] == rematched

    # After an hour the key is forgotten, and so it is by a restart: the call is then a
    # new one (here one that changes nothing).
    asked = w.app.agents.tools.admin._asked
    [key] = [k for k in asked if k[1] == "replayed-refusal"]
    asked[key].at -= 3601
    fresh = tool(world, token, "settle_review_album", **wrong)
    assert "replayed" not in fresh and fresh["result"]["state"] == "refused"
    assert tool(world, token, "settle_review_album", **wrong)["replayed"] is True
    asked.clear()  # (what a restart leaves of them)
    fresh = tool(world, token, "settle_review_album", **wrong)
    assert "replayed" not in fresh and fresh["result"]["state"] == "refused"
    assert outcome(w, REPLAYED) == "review"
