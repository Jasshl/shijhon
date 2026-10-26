"""Generated music answers use the pinned Navidrome's browser access policy."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from shijhon.proxy.auth import CheckFailed
from shijhon.proxy.responses import music_headers
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import simple_album, write_album
from tests.harness.replay import Replay, fixture

ORIGIN = {"Origin": "https://browser.example"}


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, str, str]]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    write_album(nd.music, simple_album("Browser Band", "Browser Record", 1, cover_file=True))
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("cors"), catalog=Replay().catalog()) as w:
        addon = w.addon("Browser source")
        w.add_source(addon)
        song, _, _ = w.placeholder_track("cors-audio", [addon])
        owned = nd.client().ok("search3", {"query": "Browser Record"})["searchResult3"]["song"][0]
        yield w, song, owned["id"]


def policy(response: httpx.Response) -> list[tuple[str, str]]:
    return sorted(
        (k, v)
        for k, v in response.headers.multi_items()
        if k.startswith("access-control-") or k == "vary"
    )


@pytest.mark.parametrize("origin", [None, "", "https://browser.example"])
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "PATCH", "OPTIONS", "TRACE"])
def test_generated_music_policy_matches_navidrome(
    world: tuple[DeliveryWorld, str, str], origin: str | None, method: str
) -> None:
    w, song, owned = world
    headers = {"Accept-Encoding": "identity", **({} if origin is None else {"Origin": origin})}
    album = "sh.al.demo." + fixture("album-twins-clean")["path"].split("/")[1]
    cover = "al-" + album
    owned_album = w.nd.client().ok("getSong", {"id": owned})["song"]["albumId"]
    for name, generated, native in (
        (
            "getAlbum",
            {"id": album},
            {"id": owned_album},
        ),
        ("getAlbum", {"id": "sh.al.demo.does-not-exist"}, {"id": "does-not-exist"}),
        (
            "getCoverArt",
            {"id": cover},
            {"id": "al-" + owned_album},
        ),
        ("stream", {"id": song}, {"id": owned}),
    ):
        for fmt in ("json", "xml") if name == "getAlbum" else ("json",):
            direct = w.nd.client().request(
                name, native, http_method=method, fmt=fmt, headers=headers
            )
            through = w.client().request(
                name, generated, http_method=method, fmt=fmt, headers=headers
            )
            assert policy(through) == policy(direct), (name, fmt, method, origin)
            assert [v.lower() for k, v in policy(through) if k == "vary"].count("origin") == 1
            if method == "GET" and name == "getCoverArt":
                assert through.headers["content-type"] == "image/jpeg"
                assert through.content
            if method == "GET" and name == "getAlbum" and generated["id"] == album:
                assert b"CRIMSON." in through.content


@pytest.mark.parametrize("range_header", ["bytes=3-15", "bytes=999999999-"])
def test_audio_range_and_error_headers(
    world: tuple[DeliveryWorld, str, str], range_header: str
) -> None:
    w, song, owned = world
    headers = {**ORIGIN, "Range": range_header, "Accept-Encoding": "identity"}
    direct = w.nd.client().request("stream", {"id": owned}, headers=headers)
    through = w.client().request("stream", {"id": song}, headers=headers)
    assert through.status_code == direct.status_code
    assert policy(through) == policy(direct)
    assert "content-range" in through.headers
    if through.status_code == 206:
        assert len(through.content) == 13


def test_forwarded_answers_remain_unchanged(world: tuple[DeliveryWorld, str, str]) -> None:
    w, _, owned = world
    for name, params in (
        ("getSong", {"id": owned}),
        ("stream", {"id": owned}),
        ("getSong", {"id": "absent"}),
    ):
        direct = w.nd.client(salt="fixed").request(name, params, headers=ORIGIN)
        through = w.client(salt="fixed").request(name, params, headers=ORIGIN)
        assert through.status_code == direct.status_code and through.content == direct.content
        ignored = {"date", "transfer-encoding"}
        assert [(k, v) for k, v in through.headers.multi_items() if k not in ignored] == [
            (k, v) for k, v in direct.headers.multi_items() if k not in ignored
        ]


@pytest.mark.parametrize("origin", [None, "", "https://browser.example"])
@pytest.mark.parametrize("requested", ["GET", "POST", "TRACE"])
def test_preflights_are_forwarded_without_work(
    world: tuple[DeliveryWorld, str, str], origin: str | None, requested: str
) -> None:
    w, song, _ = world
    headers = {
        "Access-Control-Request-Method": requested,
        "Access-Control-Request-Headers": "Range, Content-Type, X-Not-Allowed",
        **({} if origin is None else {"Origin": origin}),
    }
    pings, asks = w.app.checker.pings, len(w.addons[0].requests())
    answers = [
        httpx.options(base + "/rest/stream", headers=headers, params={"id": song})
        for base in (w.nd.base_url, w.server.base_url)
    ]
    assert answers[0].status_code == answers[1].status_code
    assert answers[0].content == answers[1].content
    assert policy(answers[0]) == policy(answers[1])
    assert w.app.checker.pings == pings and len(w.addons[0].requests()) == asks


def test_generated_auth_failure_has_music_policy_only(
    world: tuple[DeliveryWorld, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    w, song, _ = world

    async def failed(*args: object, fresh: bool = False) -> None:
        raise CheckFailed("unavailable")

    monkeypatch.setattr(w.app.checker, "check", failed)
    failure = w.client().request("stream", {"id": song}, headers=ORIGIN)
    assert failure.status_code == 502
    direct = w.nd.client().request("ping", headers={**ORIGIN, "Accept-Encoding": "identity"})
    assert policy(failure) == policy(direct)
    for path in ("/shijhon/", "/agents/", "/oauth/authorize"):
        response = httpx.get(w.server.base_url + path, headers=ORIGIN)
        # Mounted pages retain their own policy; forwarded paths retain Navidrome's.
        if path == "/shijhon/":
            assert "access-control-allow-origin" not in response.headers
        else:
            assert policy(response) == policy(httpx.get(w.nd.base_url + path, headers=ORIGIN))


def test_vary_and_existing_policy_are_preserved() -> None:
    original = [(b"vary", b"Accept-Encoding"), (b"content-range", b"bytes 1-2/10")]
    headers = music_headers("GET", [(b"origin", b"")], original)
    assert headers[:2] == original and (b"vary", b"Origin") in headers
    assert music_headers("GET", [(b"origin", b"")], headers) == headers
    custom = [(b"vary", b"*"), (b"access-control-allow-origin", b"https://fixed.example")]
    assert music_headers("TRACE", [(b"origin", b"x")], custom) == custom


def test_generated_share_errors_have_navidrome_music_policy(
    world: tuple[DeliveryWorld, str, str],
) -> None:
    w, _, _ = world
    song, _, _ = w.placeholder_track("cors-share-unavailable", [])
    ident = w.nd.native(
        "POST",
        "share",
        json={
            "resourceIds": song,
            "resourceType": "media_file",
            "downloadable": True,
        },
    ).json()["id"]
    headers = {**ORIGIN, "Accept-Encoding": "identity"}
    direct = httpx.get(w.nd.base_url + "/share/d/" + ident, headers=headers)
    through = httpx.get(w.server.base_url + "/share/d/" + ident, headers=headers)
    assert through.status_code == 503 and through.content == b"Audio unavailable\n"
    assert policy(through) == policy(direct)
