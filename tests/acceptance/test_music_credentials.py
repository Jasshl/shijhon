"""Revoked REST credentials cannot inherit private queries in the production stack."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.acceptance.test_B_identity import comparable, failures
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import simple_album, write_album
from tests.harness.navidrome import NavidromeInstance
from tests.harness.subsonic import AuthStyle

OLD_PASSWORD = "invented-music-old-password"
NEW_PASSWORD = "invented-music-new-password"
QUERY = {"genre": "Fiction", "count": 500}


@pytest.fixture(scope="module")
def nd(navidrome_factory: NavidromeFactory) -> NavidromeInstance:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Boundary Artist", "Boundary Album", 3, genre="Fiction"))
    nd.scan(full=True)
    return nd


@pytest.fixture
def world(nd: NavidromeInstance, tmp_path: Path) -> Iterator[DeliveryWorld]:
    with delivery_world(nd, tmp_path) as result:
        yield result


def sign_in(
    world: DeliveryWorld, stack: ExitStack, username: str, password: str, owner: str
) -> None:
    browser = Browser(world.server.base_url)
    stack.callback(browser.close)
    assert browser.sign_in(username, password).status_code == 303
    collection = world.services.collection
    assert collection is not None
    deadline = time.monotonic() + 10
    while True:
        state = world.server.call(lambda: collection.index_state(owner, "1"))
        if state.state == "ready":
            return
        assert time.monotonic() < deadline, state
        time.sleep(0.05)


def reset_password(world: DeliveryWorld, owner: str) -> None:
    current = world.nd.native("GET", "user/" + owner).json()
    world.nd.native("PUT", "user/" + owner, json={**current, "password": NEW_PASSWORD})


@pytest.mark.parametrize("transition", ["recreated", "password-reset"])
@pytest.mark.parametrize("auth", ["token", "password"])
def test_old_credentials_cannot_read_warmed_current_owners_private_annotations(
    world: DeliveryWorld,
    transition: str,
    auth: AuthStyle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nd = world.nd
    username = "proof-" + transition + "-" + auth
    old_owner = nd.create_user(username, OLD_PASSWORD)
    with ExitStack() as stack:
        sign_in(world, stack, username, OLD_PASSWORD, old_owner)
        old_native = nd.client(username, OLD_PASSWORD, auth=auth, salt="old-fixed-music-salt")
        old = old_native.with_base(world.server.base_url)
        stack.callback(old_native.close)
        stack.callback(old.close)
        if transition == "password-reset":
            song = old_native.ok("getSongsByGenre", QUERY)["songsByGenre"]["song"][0]["id"]
            old_native.ok("star", {"id": song})
        pings = world.app.checker.pings
        original = old.ok("getSongsByGenre", QUERY)["songsByGenre"]["song"]
        assert len(original) == 3
        assert bool(any(song.get("starred") for song in original)) == (
            transition == "password-reset"
        )
        assert world.app.checker.pings == pings + 1

        if transition == "recreated":
            nd.native("DELETE", "user/" + old_owner)
            owner = nd.create_user(username, NEW_PASSWORD)
            assert owner != old_owner
        else:
            reset_password(world, old_owner)
            owner = nd.login(username, NEW_PASSWORD)["id"]
            assert owner == old_owner
        sign_in(world, stack, username, NEW_PASSWORD, owner)
        current_native = nd.client(username, NEW_PASSWORD, auth=auth, salt="new-fixed-music-salt")
        current = current_native.with_base(world.server.base_url)
        stack.callback(current_native.close)
        stack.callback(current.close)
        # The account's favorite (after a reset, the one it had): Navidrome orders these
        # genre songs with ties, which a favorite or a read of favorites can reorder.
        starred = song if transition == "password-reset" else original[0]["id"]
        current_native.ok("star", {"id": starred})
        # Populate, then reuse the replacement/current owner's private query answer.
        for _ in range(2):
            songs = current.ok("getSongsByGenre", QUERY)["songsByGenre"]["song"]
            assert [song["id"] for song in songs if song.get("starred")] == [starred]

        own = old_native.request("getSongsByGenre", QUERY, headers={"Accept-Encoding": "identity"})
        assert own.json()["subsonic-response"]["error"] == {
            "code": 40,
            "message": "Wrong username or password",
        }
        send = AsyncMock(wraps=world.app.upstream.send)
        monkeypatch.setattr(world.app.upstream, "send", send)
        pings, refused = world.app.checker.pings, failures(nd)
        through = old.request("getSongsByGenre", QUERY, headers={"Accept-Encoding": "identity"})
        assert through.json()["subsonic-response"]["status"] == "failed"
        assert b"starred" not in through.content
        assert comparable(through) == comparable(own)
        assert world.app.checker.pings == pings + 1
        assert failures(nd) == refused + 1
        assert [asked.args[1] for asked in send.await_args_list] == [b"/rest/getUser"]


FORMATS = [
    {"f": "json"},
    {},
    {"f": "jsonp", "callback": "window.answer"},
    {"f": "jsonp", "callback": "not a name"},
]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("fmt", FORMATS, ids=["json", "xml", "jsonp", "bad-callback"])
def test_warm_revoked_credentials_keep_exact_native_refusal_format_and_one_failed_login(
    world: DeliveryWorld, method: str, fmt: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    username = "format-" + method.lower() + "-" + str(FORMATS.index(fmt))
    owner = world.nd.create_user(username, OLD_PASSWORD)
    with ExitStack() as stack:
        sign_in(world, stack, username, OLD_PASSWORD, owner)
        native = world.nd.client(username, OLD_PASSWORD, salt="fixed-format-salt")
        client = native.with_base(world.server.base_url)
        stack.callback(native.close)
        stack.callback(client.close)
        assert len(client.ok("getSongsByGenre", QUERY)["songsByGenre"]["song"]) == 3
        reset_password(world, owner)
        params = [*native.auth_params(), *fmt.items(), *((k, str(v)) for k, v in QUERY.items())]
        headers = {"Accept-Encoding": "identity", "Origin": "https://web.example"}
        own = httpx.request(
            method, world.nd.base_url + "/rest/getSongsByGenre", params=params, headers=headers
        )
        verify = AsyncMock(side_effect=AssertionError("refused request cannot resolve an owner"))
        monkeypatch.setattr(world.app.proxy.music_gate.access, "verify", verify)
        send = AsyncMock(wraps=world.app.upstream.send)
        monkeypatch.setattr(world.app.upstream, "send", send)
        pings, refused = world.app.checker.pings, failures(world.nd)
        through = httpx.request(
            method, world.server.base_url + "/rest/getSongsByGenre", params=params, headers=headers
        )
        assert comparable(through) == comparable(own)
        assert through.status_code == 200
        assert failures(world.nd) == refused + 1
        bad_callback = fmt.get("callback") == "not a name"
        # Invalid callbacks bypass selection work locally: Navidrome authenticates
        # the original request once. Selected reads require one fresh proof here.
        assert world.app.checker.pings == pings + int(not bad_callback)
        paths = [asked.args[1] for asked in send.await_args_list]
        assert paths == ([b"/rest/getSongsByGenre"] if bad_callback else [b"/rest/getUser"])
        verify.assert_not_awaited()
        if method == "HEAD":
            assert through.content == b""
        elif not fmt.get("callback"):
            assert b"40" in through.content and b"Wrong username or password" in through.content


def test_warmed_music_query_fails_closed_when_fresh_credentials_are_unavailable(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    username = "proof-unavailable"
    owner = world.nd.create_user(username, OLD_PASSWORD)
    with ExitStack() as stack:
        sign_in(world, stack, username, OLD_PASSWORD, owner)
        native = world.nd.client(username, OLD_PASSWORD, salt="unavailable-fixed-salt")
        stack.callback(native.close)
        client = native.with_base(world.server.base_url)
        stack.callback(client.close)
        assert len(client.ok("getSongsByGenre", QUERY)["songsByGenre"]["song"]) == 3
        verify = AsyncMock(side_effect=AssertionError("unverified current owner"))
        monkeypatch.setattr(world.app.proxy.music_gate.access, "verify", verify)
        send = AsyncMock(side_effect=httpx.ConnectError("invented native outage"))
        monkeypatch.setattr(world.app.upstream, "send", send)
        pings = world.app.checker.pings
        through = client.request("getSongsByGenre", QUERY)
        assert through.status_code == 502 and through.headers["retry-after"] == "5"
        assert b"starred" not in through.content
        assert world.app.checker.pings == pings + 1
        assert [asked.args[1] for asked in send.await_args_list] == [b"/rest/getUser"]
        verify.assert_not_awaited()
