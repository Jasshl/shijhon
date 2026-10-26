"""Exact socket targets expose path normalization that ordinary HTTP clients hide."""

from __future__ import annotations

import http.client
import socket
from collections.abc import Iterator
from urllib.parse import urlencode, urlsplit

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import simple_album, write_album
from tests.harness.navidrome import ADMIN_USER
from tests.harness.subsonic import SubsonicClient


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, str, bytes, str]]:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Path Band", "Path Record", 1))
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("paths")) as w:
        addon = w.addon("Path source")
        w.add_source(addon)
        song, _, audio = w.placeholder_track("path-audio", [addon])
        owned = nd.client().ok("search3", {"query": "Path Record"})["searchResult3"]["song"][0][
            "id"
        ]
        yield w, song, audio.read_bytes(), owned


def raw_request(
    base: str,
    path: str,
    params: list[tuple[str, str]],
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    address = urlsplit(base)
    assert address.hostname and address.port
    request_headers = {
        "Host": "music.test",
        "Connection": "close",
        "Accept-Encoding": "identity",
        **(headers or {}),
    }
    target = path + "?" + urlencode(params)
    lines = [f"{method} {target} HTTP/1.1", *(f"{k}: {v}" for k, v in request_headers.items())]
    with socket.create_connection((address.hostname, address.port), timeout=30) as connection:
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        response = http.client.HTTPResponse(connection, method=method)
        response.begin()
        return httpx.Response(
            response.status, headers=response.getheaders(), content=response.read()
        )


@pytest.mark.parametrize(
    "path",
    [
        "/rest/x/../stream",
        "/rest/x/../stream.view",
        "/rest/x/../download",
        "/rest/x/../download.view",
        "/rest/x/../getTranscodeStream",
        "/rest/./stream",
        "/rest/x/../../rest/stream",
        "/x/../rest/stream",
        "/rest/%2e/stream",
        "/rest/x/%2e%2e/stream",
        "/rest/x/.%2E/stream",
        "/rest/x/%2E./stream",
        "/rest/x%2f..%2fstream",
        "/%2e%2e/rest/stream",
        "/shijhon/x/../../rest/stream",
        "/share/x/../../rest/stream",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_dot_segments_are_refused_before_work(
    world: tuple[DeliveryWorld, str, bytes, str], path: str, method: str
) -> None:
    w, song, _, _ = world
    pings, scans, asked = (
        w.app.checker.pings,
        w.services.scans.scans_started,
        len(w.addons[0].requests()),
    )
    response = raw_request(
        w.server.base_url, path, [*w.client().auth_params(), ("id", song)], method=method
    )
    assert response.status_code == 400
    assert response.content == (b"" if method == "HEAD" else b"Ambiguous request path\n")
    assert w.app.checker.pings == pings and w.services.scans.scans_started == scans
    assert len(w.addons[0].requests()) == asked


@pytest.mark.parametrize(
    "path",
    [
        "/rest/%73tream",
        "/rest/stream%2eview",
        "/rest/%64ownload.view",
        "/%72est/stream",
        "/rest%2fstream",
        "/rest%2fstream.view",
        "/rest/%252e%252e/stream",
        "/rest/%2/stream",
        "/rest/stream/",
        "/rest//stream",
        "//rest/stream",
    ],
)
def test_escaped_aliases_and_nonroutes_remain_navidromes(
    world: tuple[DeliveryWorld, str, bytes, str], path: str
) -> None:
    w, song, _, _ = world
    params = [*w.client(salt="fixed").auth_params(), ("id", song)]
    before = len(w.addons[0].requests())
    direct = raw_request(w.nd.base_url, path, params)
    through = raw_request(w.server.base_url, path, params)
    assert through.status_code == direct.status_code
    assert through.content == direct.content
    ignored = {"date", "transfer-encoding"}
    assert sorted((k, v) for k, v in through.headers.multi_items() if k not in ignored) == sorted(
        (k, v) for k, v in direct.headers.multi_items() if k not in ignored
    )
    assert len(w.addons[0].requests()) == before
    assert not through.headers.get("content-type", "").startswith("audio/")


@pytest.mark.parametrize("path", ["/rest/stream", "/rest/stream.view"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_canonical_aliases_intercept_placeholders_and_preserve_owned_files(
    world: tuple[DeliveryWorld, str, bytes, str], path: str, method: str
) -> None:
    w, song, audio, owned = world
    headers = {"Range": "bytes=3-15"}
    response = raw_request(
        w.server.base_url,
        path,
        [*w.client().auth_params(), ("id", song)],
        method=method,
        headers=headers,
    )
    assert response.status_code == 206
    assert response.content == (b"" if method == "HEAD" else audio[3:16])
    assert response.headers["content-range"] == f"bytes 3-15/{len(audio)}"
    params = [*w.client(salt="owned").auth_params(), ("id", owned)]
    for endpoint in (path, path.replace("stream", "download")):
        direct = raw_request(w.nd.base_url, endpoint, params, method=method, headers=headers)
        through = raw_request(w.server.base_url, endpoint, params, method=method, headers=headers)
        assert through.status_code == direct.status_code
        assert through.content == direct.content
        assert through.headers["content-length"] == direct.headers["content-length"]
        assert through.headers.get("content-range") == direct.headers.get("content-range")


def test_invalid_credentials_cannot_use_dot_paths_or_aliases(
    world: tuple[DeliveryWorld, str, bytes, str],
) -> None:
    w, song, _, _ = world
    client = SubsonicClient(w.server.base_url, ADMIN_USER, "wrong", salt="invalid")
    params = [*client.auth_params(), ("f", "json"), ("id", song)]
    scans, asks = w.services.scans.scans_started, len(w.addons[0].requests())
    assert raw_request(w.server.base_url, "/rest/x/../stream", params).status_code == 400
    for path in ("/rest/stream", "/rest/stream.view", "/rest/download.view"):
        direct = raw_request(w.nd.base_url, path, params)
        through = raw_request(w.server.base_url, path, params)
        assert through.content == direct.content
        assert through.json()["subsonic-response"]["error"]["code"] == 40
    assert w.services.scans.scans_started == scans and len(w.addons[0].requests()) == asks


def test_query_dot_segments_do_not_change_owned_audio(
    world: tuple[DeliveryWorld, str, bytes, str],
) -> None:
    w, _, _, owned = world
    params = [*w.client().auth_params(), ("id", owned), ("ignored", "/x/../stream")]
    direct = raw_request(w.nd.base_url, "/rest/stream", params)
    through = raw_request(w.server.base_url, "/rest/stream", params)
    assert through.status_code == direct.status_code == 200
    assert through.content == direct.content
