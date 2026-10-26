"""A native refusal between real fresh proofs survives the completion wrappers."""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from unittest.mock import AsyncMock

import httpx
import pytest

from shijhon.proxy.auth import Caller, Refusal, Verdict
from shijhon.proxy.params import RestCall
from tests.acceptance.test_B_identity import failures
from tests.acceptance.test_I_fill import OPENED
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import write_album
from tests.harness.replay import Replay
from tests.harness.subsonic import SubsonicClient

USERNAME = "late-refusal-listener"
PASSWORD = "invented-current-password"
REPLACEMENT = "invented-replacement-password"


@pytest.fixture(scope="module")
def completion(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, SubsonicClient, str, str, str]]:
    nd = navidrome_factory()
    write_album(nd.music, OPENED)
    nd.scan(full=True)
    owner = nd.create_user(USERNAME, PASSWORD)
    native = nd.client(USERNAME, PASSWORD, salt="late-refusal-salt")
    replay = Replay()
    replay.aliases["neve ashdown northern letters (remastered)"] = "tender rivers"
    try:
        album = native.ok("search3", {"query": OPENED.title, "artistCount": 0, "songCount": 0})[
            "searchResult3"
        ]["album"][0]["id"]
        artist = native.ok("getAlbum", {"id": album})["album"]["artistId"]
        with delivery_world(
            nd,
            tmp_path_factory.mktemp("late-refusal"),
            catalog=replay.catalog(),
            fill={
                "enabled": True,
                "auto_min_songs": 50,
                "library_pass": "off",
                "open_budget_seconds": 15,
            },
        ) as world:
            browser = Browser(world.server.base_url)
            try:
                assert browser.sign_in(USERNAME, PASSWORD).status_code == 303
            finally:
                browser.close()
            collection = world.services.collection
            assert collection is not None
            world.server.call(lambda: collection.refresh(owner, "1"))
            client = native.with_base(world.server.base_url)
            try:
                assert client.ok("getAlbum", {"id": album})["album"]["songCount"] == 17

                async def cached_plan() -> None:
                    assert world.services.fills is not None
                    shown = await world.services.fills.shown([album])
                    assert shown[album].owned == 2 and len(shown[album].missing) == 15

                world.server.call(cached_plan)
                assert world.placeholder_rows() == 0
                yield world, client, owner, album, artist
            finally:
                client.close()
    finally:
        native.close()


@pytest.mark.parametrize("method", ["getAlbum", "getAlbumList2", "getArtist", "search3"])
@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_late_fresh_refusal_retains_native_wire_bytes_and_headers(
    completion: tuple[DeliveryWorld, SubsonicClient, str, str, str],
    method: str,
    fmt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, client, owner, album, artist = completion
    nd = world.nd

    def password(value: str) -> None:
        current = nd.native("GET", "user/" + owner).json()
        nd.native("PUT", "user/" + owner, json={**current, "password": value})

    password(PASSWORD)
    checker = world.app.checker
    check = checker.check
    proofs: list[bool] = []
    verdicts: list[Verdict | None] = []

    async def revoke(call: RestCall, *, fresh: bool = False) -> Verdict | None:
        proofs.append(fresh)
        if len(proofs) == 2:
            # Deterministic scheduling only: both proofs and their verdicts are real.
            password(REPLACEMENT)
        verdict = await check(call, fresh=fresh)
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(checker, "check", revoke)
    send = AsyncMock(wraps=world.app.upstream.send)
    monkeypatch.setattr(world.app.upstream, "send", send)
    pings, refused = checker.pings, failures(nd)
    params = {
        "getAlbum": {"id": album},
        "getAlbumList2": {"type": "alphabeticalByName"},
        "getArtist": {"id": artist},
        "search3": {"query": OPENED.title, "songCount": 0},
    }[method]
    answer = client.request(
        method,
        params,
        fmt=fmt,
        headers={"Accept-Encoding": "gzip", "Origin": "https://client.example"},
        stream=True,
    )
    try:
        # iter_raw is essential: automatic client decompression would hide the defect.
        wire = b"".join(answer.iter_raw())
    finally:
        answer.close()
    assert proofs == [True, True]
    assert len(verdicts) == 2 and isinstance(verdicts[0], Caller)
    refusal = verdicts[1]
    assert isinstance(refusal, Refusal)
    assert checker.pings == pings + 2
    assert failures(nd) == refused + 1
    assert [(asked.args[0], asked.args[1]) for asked in send.await_args_list] == [
        ("GET", b"/rest/getUser"),
        ("GET", b"/rest/getUser"),
    ]
    assert world.placeholder_rows() == 0
    native_headers = httpx.Headers(refusal.headers)
    assert native_headers["content-encoding"] == "gzip"
    assert native_headers["content-length"] == str(len(refusal.body))
    assert b"Wrong username or password" in gzip.decompress(refusal.body)
    assert answer.status_code == refusal.status == 200
    assert wire == refusal.body
    assert dict(answer.headers) == dict(native_headers)
