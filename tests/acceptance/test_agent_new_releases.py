"""The agent tool for new releases, over the real MCP client and a disposable Navidrome:
an account's own list, the permissions it needs, and what it says while the account has
not turned following on."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import date, timedelta

import anyio
import pytest

from tests.acceptance.test_agent_song_facts import agent, ask
from tests.acceptance.test_G_new_releases import (
    RELEASES,
    RESTLESS,
    WOLVES,
    dated,
    wait_checked,
)
from tests.acceptance.test_J_contract import ARTIST, OWNED, TERM
from tests.conftest import NavidromeFactory
from tests.harness.agents import connect_agent
from tests.harness.collection import prepare_collection
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay, fixture

LISTENER = ("listener", "listener-password")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, Browser, str]]:
    replay = Replay()
    replay.aliases[TERM] = str(fixture("search-single-vs-album")["params"]["term"])
    before = copy.deepcopy(replay.by_path[RELEASES])
    dated(replay, WOLVES, date.today() - timedelta(days=4))
    dated(replay, RESTLESS, date.today() - timedelta(days=9))
    nd = navidrome_factory()
    for album in OWNED:
        write_album(nd.music, album)
    nd.scan(full=True)
    nd.create_user(*LISTENER)
    admin = nd.client()
    artist = admin.ok("search3", {"query": ARTIST, "albumCount": 0, "songCount": 0})
    admin.ok("star", {"artistId": artist["searchResult3"]["artist"][0]["id"]})
    try:
        with delivery_world(
            nd, tmp_path_factory.mktemp("agent-new"), catalog=replay.catalog(), agents=True
        ) as w:
            owner = prepare_collection(nd, w.server)
            browser = Browser(w.server.base_url)
            assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
            yield w, browser, owner
            browser.close()
    finally:
        replay.by_path[RELEASES] = before


def test_an_agent_reads_its_accounts_new_releases(
    world: tuple[DeliveryWorld, Browser, str],
) -> None:
    w, browser, owner = world
    releases = w.services.new_releases
    assert releases is not None
    base = w.server.base_url
    full = connect_agent(browser.http, base, ADMIN_USER, ADMIN_PASSWORD, "library history")
    narrow = connect_agent(
        browser.http, base, ADMIN_USER, ADMIN_PASSWORD, "library", client_name="Narrow"
    )
    guest = connect_agent(browser.http, base, *LISTENER, "library history", client_name="Guest")

    async def off() -> None:
        async with agent(w, full["access_token"]) as session:
            listed = {tool.name for tool in (await session.list_tools()).tools}
            assert "get_new_releases" in listed
            answer = await ask(session, "get_new_releases", {})
            assert answer["ok"] and answer["enabled"] is False and answer["releases"] == []
            assert answer["setup_url"] == base + "/shijhon/new-releases"
            feature = (await ask(session, "get_capabilities", {}))["features"]["new_releases"]
            assert feature["permitted"] and not feature["ready"]
            assert feature["missing_configuration"] and feature["tools"] == ["get_new_releases"]

    anyio.run(off)
    w.server.call(lambda: releases.turn_on(owner, ADMIN_USER))
    wait_checked(w, owner)  # (the service's own check, in the background)

    async def on() -> None:
        async with agent(w, full["access_token"]) as session:
            answer = await ask(session, "get_new_releases", {})
            assert answer["ok"] and answer["enabled"] is True
            assert [r["album_id"] for r in answer["releases"]] == [
                f"sh.al.demo.{WOLVES}",
                f"sh.al.demo.{RESTLESS}",
            ]
            first = answer["releases"][0]
            assert first == {
                "album_id": f"sh.al.demo.{WOLVES}",
                "title": "Iron Wolves",
                "artist": ARTIST,
                "type": "album",
                "release_date": (date.today() - timedelta(days=4)).isoformat(),
                "tracks": 13,
            }
            assert (answer["total"], answer["has_more"], answer["window_days"]) == (2, False, 30)
            assert answer["artists_followed"] == 1 and answer["last_check"] == "finished"
            assert answer["last_checked"].endswith("Z")
            one = await ask(session, "get_new_releases", {"limit": 1})
            assert len(one["releases"]) == 1 and one["has_more"] is True
            assert (await ask(session, "get_new_releases", {"limit": 0}))["ok"] is False
            feature = (await ask(session, "get_capabilities", {}))["features"]["new_releases"]
            assert feature["ready"] and feature["missing_configuration"] == []
        # Favorites and plays decide the list: without the history permission, nothing.
        async with agent(w, narrow["access_token"]) as session:
            denied = await ask(session, "get_new_releases", {})
            assert denied["ok"] is False
            assert denied["error"]["code"] == "permission_required"
            assert denied["error"]["missing_permissions"] == ["history"]
            assert ARTIST not in str(denied) and "Iron Wolves" not in str(denied)
        # Another account's agent gets that account's own answer: it has not turned it on.
        async with agent(w, guest["access_token"]) as session:
            theirs = await ask(session, "get_new_releases", {})
            assert theirs["ok"] and theirs["enabled"] is False and theirs["releases"] == []
            assert ARTIST not in str(theirs) and "Iron Wolves" not in str(theirs)

    anyio.run(on)
