"""Suite B (complete) — every addition and commit path with a catalog ID does no work for
a caller Navidrome rejects: no catalog call, no image
fetch, no file write, no scan. The caller gets Navidrome's own error.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

import pytest

from shijhon.views.commits import COMMITS
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.navidrome import ADMIN_USER
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient


def item(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


ALBUM = f"sh.al.demo.{item('album-twins-clean')}"
SONG = f"sh.tr.demo.{item('song-twins-explicit')}"
ARTIST = f"sh.ar.demo.{item('artist-duo')}"
VIEWS: list[tuple[str, dict[str, str]]] = [
    ("getAlbum", {"id": ALBUM}),
    ("getSong", {"id": SONG}),
    ("getArtist", {"id": ARTIST}),
    ("getArtistInfo", {"id": ARTIST}),
    ("getArtistInfo2", {"id": ARTIST}),
    ("getAlbumInfo", {"id": ALBUM}),
    ("getAlbumInfo2", {"id": ALBUM}),
    ("getCoverArt", {"id": f"al-{ALBUM}"}),
    ("getCoverArt", {"id": f"ar-{ARTIST}"}),
    ("search3", {"query": fixture("search-duo")["params"]["term"]}),
    ("getArtist", {"id": "0123456789abcdef"}),  # a library artist's page
]
EXTRA = {"star": {"albumId": ALBUM}, "savePlayQueue": {"current": SONG}}
COMMIT_CALLS: list[tuple[str, dict[str, str]]] = [
    (method, {params[0]: ALBUM if method in ("star", "unstar") else SONG, **EXTRA.get(method, {})})
    for method, params in COMMITS.items()
]


@pytest.fixture(scope="module")
def replay() -> Replay:
    return Replay()


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(), tmp_path_factory.mktemp("b-catalog"), catalog=replay.catalog()
    ) as w:
        yield w


def work(world: DeliveryWorld, replay: Replay) -> tuple[Any, ...]:
    return (
        replay.api_requests,
        replay.artwork_requests,
        world.placeholder_rows(),
        world.placeholder_files(),
        world.services.scans.scans_started,
        world.services.commits.materializations,  # type: ignore[union-attr]
    )


@pytest.mark.parametrize("http_method", ["GET", "POST"])
@pytest.mark.parametrize("password", ["wrong", ""])
@pytest.mark.parametrize(("method", "params"), VIEWS + COMMIT_CALLS)
def test_rejected_callers_cause_no_work(
    world: DeliveryWorld,
    replay: Replay,
    method: str,
    params: dict[str, str],
    password: str,
    http_method: Literal["GET", "POST"],
) -> None:
    before = work(world, replay)
    client = SubsonicClient(world.server.base_url, ADMIN_USER, password, auth="password")
    if not password:
        client = SubsonicClient(world.server.base_url, ADMIN_USER, "", auth="none")
    answer = client.request(method, params, http_method=http_method)
    if method != "getCoverArt" or answer.headers.get("content-type", "").endswith("json"):
        body = answer.json()["subsonic-response"]
        assert body["status"] == "failed" and body["error"]["code"] in (10, 40)
    else:
        assert not answer.headers.get("content-type", "").startswith("image/jpeg")
    assert work(world, replay) == before
    client.close()


def test_the_same_requests_work_for_a_valid_caller(world: DeliveryWorld, replay: Replay) -> None:
    """The negative test above is meaningful: with good credentials, work happens."""
    client = world.client()
    before = replay.api_requests
    assert client.ok("getAlbum", {"id": ALBUM})["album"]["id"] == ALBUM
    assert replay.api_requests > before
    client.close()


@pytest.fixture(scope="module")
def bare(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    """No catalog configured."""
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("b-bare")) as w:
        yield w


@pytest.mark.parametrize(
    ("method", "params"),
    [c for c in VIEWS + COMMIT_CALLS if c[0] in ("getAlbum", "getSong", "stream", "star")],
)
def test_without_a_catalog_catalog_ids_are_not_found(
    bare: DeliveryWorld, method: str, params: dict[str, str]
) -> None:
    client = bare.client()
    assert client.error_code(method, params) == 70
    assert bare.placeholder_rows() == 0 and bare.placeholder_files() == []
    client.close()
