"""A shared archive is served only after all of its placeholders have real audio."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from zipfile import ZipFile

import anyio
import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeTrack
from tests.harness.library import simple_album, write_album


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    write_album(nd.music, simple_album("Share Band", "Share Record", 2))
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("share-archives")) as w:
        addon = w.addon("Share source")
        w.add_source(addon)
        yield w


def shared(world: DeliveryWorld, resource: str, kind: str, **options: object) -> str:
    return str(
        world.nd.native(
            "POST",
            "share",
            json={
                "resourceIds": resource,
                "resourceType": kind,
                "downloadable": True,
                **options,
            },
        ).json()["id"]
    )


def states(world: DeliveryWorld, songs: list[str]) -> list[str]:
    async def read() -> list[str]:
        rows = await world.services.store.fetchall("SELECT song_id, state FROM placeholders")
        by_id = {r["song_id"]: str(r["state"]) for r in rows}
        return [by_id[s] for s in songs]

    return world.server.call(read)


def library_bytes(world: DeliveryWorld, song: str) -> bytes:
    path = world.nd.native("GET", f"song/{song}").json()["path"]
    return (world.nd.music / path).read_bytes()


@pytest.mark.parametrize(
    ("count", "silent_tail"), [(5000, False), (5000, True), (5001, False), (5001, True)]
)
def test_shared_playlist_archive_at_the_inspection_boundary(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch, count: int, silent_tail: bool
) -> None:
    owned = world.nd.client().ok("search3", {"query": "Share Record"})["searchResult3"]["song"][0][
        "id"
    ]
    tail = owned
    if silent_tail:
        tail, _, _ = world.placeholder_track(f"share-boundary-{count}", [world.addons[0]])
    entries = [owned] * (count - 1) + [tail]
    playlist = world.nd.client().ok(
        "createPlaylist",
        {"name": f"Boundary {count} {silent_tail}", "songId": entries},
        http_method="POST",
    )["playlist"]["id"]
    assert world.nd.client().ok("getPlaylist", {"id": playlist})["playlist"]["songCount"] == count
    link = "/share/d/" + shared(world, playlist, "playlist")
    original = world.services.interceptor._ask
    forwarded = []

    async def observe(method, path, *args, **kwargs):
        if method == "GET" and path == link.encode():
            forwarded.append(method)
            # The decision is real; avoid building thousands of repeated ZIP members.
            return httpx.Response(418, stream=httpx.ByteStream(b"Verified archive"))
        return await original(method, path, *args, **kwargs)

    monkeypatch.setattr(world.services.interceptor, "_ask", observe)
    before = len(world.addons[0].requests("audio"))
    response = httpx.get(world.server.base_url + link, timeout=60)
    assert response.status_code == 418 and forwarded == ["GET"]
    if silent_tail:
        assert states(world, [tail]) == ["delivered"]
        assert len(world.addons[0].requests("audio")) == before + 1


def test_large_mixed_share_prepares_in_the_background_and_eventually_serves(
    world: DeliveryWorld,
) -> None:
    release = catalog_release("shared-large", "Shared Large", "Share Artist", 30)
    materialized = world.materialize(release)
    songs = [materialized.created[t.ref] for t in release.tracks]
    addon = world.addons[0]
    for track in release.tracks:
        addon.add(FakeTrack(isrc=track.isrc, audio=world.audio("shared-large")))
    assert world.client().request("download", {"id": songs[0]}).status_code == 200
    owned = world.nd.client().ok("search3", {"query": "Share Record"})["searchResult3"]["song"][0][
        "id"
    ]
    playlist = world.nd.client().ok(
        "createPlaylist",
        {"name": "Mixed shared archive", "songId": [owned, *songs]},
    )["playlist"]["id"]
    link = "/share/d/" + shared(world, playlist, "playlist")
    before = len(addon.requests("audio"))
    # Hold background preparation: both requests answer before it can finish.
    entered, resume = threading.Event(), threading.Event()
    original = world.services.download_first.ensure

    async def held(*args, **kwargs):
        if kwargs.get("background"):
            entered.set()
            await anyio.to_thread.run_sync(resume.wait)
        return await original(*args, **kwargs)

    world.services.download_first.ensure = held
    try:
        for method in ("GET", "HEAD"):
            response = httpx.request(method, world.server.base_url + link, timeout=10)
            assert response.status_code == 503 and response.headers["retry-after"] == "5"
            assert response.content == (b"Audio unavailable\n" if method == "GET" else b"")
        assert entered.wait(5)
        assert len(world.services.interceptor._share_preparing) == 29
        resume.set()
        deadline = time.monotonic() + 120
        while True:
            response = httpx.get(world.server.base_url + link, timeout=60)
            if response.status_code == 200:
                break
            assert response.status_code == 503 and time.monotonic() < deadline
            time.sleep(0.1)
        assert states(world, songs) == ["delivered"] * 30
        assert len(addon.requests("audio")) == before + 29
        with ZipFile(io.BytesIO(response.content)) as archive:
            actual = [archive.read(name) for name in archive.namelist() if name.endswith(".flac")]
        assert sorted(actual) == sorted(library_bytes(world, song) for song in [owned, *songs])
    finally:
        resume.set()
        world.services.download_first.ensure = original


@pytest.mark.parametrize("invalid", ["missing", "expired", "not-downloadable"])
def test_invalid_archive_links_prepare_nothing(world: DeliveryWorld, invalid: str) -> None:
    song, _, _ = world.placeholder_track("share-invalid-" + invalid, [world.addons[0]])
    options = {
        "expired": {"expiresAt": "2000-01-01T00:00:00Z"},
        "not-downloadable": {"downloadable": False},
    }
    ident = (
        "does-not-exist"
        if invalid == "missing"
        else shared(world, song, "media_file", **options[invalid])
    )
    before, scans = len(world.addons[0].requests()), world.services.scans.scans_started
    result = httpx.get(world.server.base_url + "/share/d/" + ident)
    assert result.status_code != 200
    assert len(world.addons[0].requests()) == before
    assert world.services.scans.scans_started == scans
    assert states(world, [song]) == ["placeholder"]


def test_archive_rechecks_after_a_concurrent_fill(world: DeliveryWorld) -> None:
    release = catalog_release("shared-write", "Shared Write", "Share Writer", 3)
    owned_album = simple_album(release.artist, release.title, 2)
    write_album(world.nd.music, owned_album)
    world.nd.scan(full=True)
    album = world.nd.client().ok("search3", {"query": release.title})["searchResult3"]["album"][0][
        "id"
    ]
    link = "/share/d/" + shared(world, album, "album")
    engine = world.services.engine
    reached, listing = threading.Event(), threading.Event()
    go: list[anyio.Event] = []

    async def setup() -> None:
        go.append(anyio.Event())

    async def pause(step: str) -> None:
        if step == "verified":
            reached.set()
            await go[0].wait()

    async def resume() -> None:
        go[0].set()

    async def fill():
        owned = await engine.owned_album(album)
        links = {t.ref: owned.songs[t.number - 1]["id"] for t in release.tracks[:2]}
        return await engine.materialize(release, owned_album_id=album, links=links)

    # Observe the actual listing gate, without replacing its locking behavior.
    from contextlib import asynccontextmanager

    original = engine.listing

    @asynccontextmanager
    async def observed(*, wait: float):
        listing.set()
        async with original(wait=wait) as ended:
            yield ended

    world.server.call(setup)
    engine.step = pause
    engine.listing = observed
    assert world.app.loop is not None
    done = asyncio.run_coroutine_threadsafe(fill(), world.app.loop)
    try:
        assert reached.wait(60)
        with ThreadPoolExecutor(1) as pool:
            request = pool.submit(httpx.get, world.server.base_url + link, timeout=60)
            assert listing.wait(10)
            assert not request.done()
            world.server.call(resume)
            created = done.result(60).created
            response = request.result(60)
        assert len(created) == 1
        assert response.status_code == 503 and response.content == b"Audio unavailable\n"
        assert states(world, list(created.values())) == ["placeholder"]
    finally:
        world.server.call(resume)
        done.result(60)
        engine.step = None
        engine.listing = original


def test_an_archive_whose_songs_cannot_be_listed_is_refused(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shijhon.navidrome.client import NavidromeError

    song, _, _ = world.placeholder_track("share-unlisted", [world.addons[0]])
    playlist = world.nd.client().ok(
        "createPlaylist",
        {
            "name": "Unlisted shared archive",
            "songId": [song],
        },
    )["playlist"]["id"]
    link = "/share/d/" + shared(world, playlist, "playlist")

    async def failed(*args: object, **kwargs: object) -> list[str]:
        raise NavidromeError("unavailable")

    monkeypatch.setattr(world.services.navidrome, "archive_song_ids", failed)
    response = httpx.get(world.server.base_url + link)
    assert response.status_code == 503 and response.content == b"Audio unavailable\n"
    assert states(world, [song]) == ["placeholder"]


@pytest.mark.parametrize("failure", ["connection", "server-error"])
def test_a_share_validation_without_a_verdict_never_forwards_an_unchecked_archive(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    song, _, _ = world.placeholder_track("share-no-verdict-" + failure, [world.addons[0]])
    link = "/share/d/" + shared(world, song, "media_file")
    upstream = world.services.interceptor.upstream
    original = upstream.send
    forwarded: list[str] = []

    async def unavailable(method: str, path: bytes, *args: object, **kwargs: object):
        if path == link.encode():
            if method == "HEAD":
                if failure == "connection":
                    raise httpx.ConnectError("unavailable")
                return httpx.Response(503)
            forwarded.append(method)
        return await original(method, path, *args, **kwargs)

    monkeypatch.setattr(upstream, "send", unavailable)
    before = len(world.addons[0].requests())
    response = httpx.get(world.server.base_url + link)
    assert response.status_code == 503 and response.content == b"Audio unavailable\n"
    assert forwarded == [] and len(world.addons[0].requests()) == before
    assert states(world, [song]) == ["placeholder"]


def test_a_share_preparation_failure_has_no_head_body(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    song, _, _ = world.placeholder_track("share-prepare-failure", [world.addons[0]])
    link = "/share/d/" + shared(world, song, "song")

    async def failed(*args: object, **kwargs: object) -> str:
        return "unavailable"

    monkeypatch.setattr(world.services.download_first, "ensure", failed)
    for method in ("GET", "HEAD"):
        response = httpx.request(method, world.server.base_url + link)
        assert response.status_code == 503
        assert response.content == (b"Audio unavailable\n" if method == "GET" else b"")
