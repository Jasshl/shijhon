"""Accepted collection actions survive unavailable checks and large releases."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from shijhon.access import Principal
from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
    release_data,
)
from shijhon.collection.history import History
from shijhon.proxy.app import Forward
from shijhon.proxy.params import RestCall
from shijhon.store import Store
from shijhon.views.answers import LibraryAnswer
from shijhon.views.collection_requests import CollectionRequests

pytestmark = pytest.mark.anyio


@pytest.fixture
async def recorder(tmp_path: Path):
    store = await Store.open(tmp_path / "collection.db")
    commits = SimpleNamespace(
        navidrome=SimpleNamespace(library_id=1),
        materialized=SimpleNamespace(song=AsyncMock(return_value="native-song")),
    )
    upstream = SimpleNamespace(send=AsyncMock())
    try:
        yield CollectionRequests(commits, upstream, History(store))
    finally:
        await store.close()


def track(number: int = 1) -> CatalogTrack:
    return CatalogTrack(
        CatalogRef("demo", f"track{number}"),
        f"Song {number}",
        "Glass Orchard",
        180000,
        number=number,
        album=CatalogRef("demo", "release"),
        album_title="Cloud House",
    )


def call(method: str = "scrobble", *, ranged: bool = False) -> RestCall:
    headers = [(b"range", b"bytes=1-2")] if ranged else []
    return RestCall.build(
        method, "GET", ("/rest/" + method).encode(), b"id=native-song", headers, None
    )


def context():
    return SimpleNamespace(
        head=False,
        caller=AsyncMock(return_value=object()),
        kept={"shijhon.principal": Principal("one", "listener", False, frozenset({1}))},
    )


@pytest.mark.parametrize("favorite", [False, True])
@pytest.mark.parametrize("status", [503, 200])
async def test_unavailable_verification_is_queued_but_mismatch_is_not(
    recorder: CollectionRequests,
    favorite: bool,
    status: int,
) -> None:
    recorder._tracks = AsyncMock(return_value=[track()])
    body = {
        "subsonic-response": {
            "status": "ok",
            "song": {
                "id": "native-song",
                "title": "Different recording",
                "artist": "Someone Else",
                "duration": 180,
            },
        }
    }
    recorder.upstream.send.return_value = httpx.Response(status, json=body)
    if favorite:
        recorder.reasons = SimpleNamespace(favorited=AsyncMock())
    method = "star" if favorite else "scrobble"
    accepted = LibraryAnswer(
        200,
        [(b"content-type", b"application/json")],
        b'{"subsonic-response":{"status":"ok"}}',
    )
    recorder._answer = AsyncMock(return_value=accepted)
    await recorder(call(method), context(), None)
    pending = await recorder.store.fetchone("SELECT 1 FROM collection_pending")
    assert bool(pending) == (status == 503)
    assert not await recorder.store.fetchone("SELECT 1 FROM collection_history")


@pytest.mark.parametrize("failure", ["connection", "not-found", "malformed"])
async def test_verification_distinguishes_unavailable_from_native_not_found(
    recorder: CollectionRequests,
    failure: str,
) -> None:
    if failure == "connection":
        recorder.upstream.send.side_effect = httpx.ConnectError("unavailable")
    elif failure == "not-found":
        recorder.upstream.send.return_value = httpx.Response(
            200, json={"subsonic-response": {"status": "failed", "error": {"code": 70}}}
        )
    else:
        recorder.upstream.send.return_value = httpx.Response(200, content=b"unreadable")
    assert await recorder._verify(call(), [track()]) is (False if failure == "not-found" else None)


async def seed_large_release(recorder: CollectionRequests) -> None:
    release = CatalogRelease(
        CatalogRef("demo", "release"),
        "Cloud House",
        "Glass Orchard",
        ReleaseKind.ALBUM,
        "2020",
        tracks=tuple(track(n) for n in range(1, 1204)),
    )
    async with recorder.store.transaction() as db:
        await db.execute(
            "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
            " VALUES ('demo:release','_shijhon/release','native-album',"
            "'Cloud House','Glass Orchard','{}',?,1)",
            [json.dumps(release_data(release))],
        )
        await db.executemany(
            "INSERT INTO track_links VALUES (?,?,?,0)",
            [(str(t.ref), f"native-{t.number}", str(release.ref)) for t in release.tracks],
        )
    await recorder._queue("one", [("albumId", "native-album")])


async def test_deferred_album_records_every_page_and_keeps_queue_until_commit(
    recorder: CollectionRequests,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_large_release(recorder)
    assert await recorder._tracks([("albumId", "native-album")]) is None
    record = recorder.history.record_catalog
    pages = []

    async def fail_last_page(actor, library, tracks, *, db):
        pages.append(len(tracks))
        assert await (await db.execute("SELECT 1 FROM collection_pending")).fetchone()
        await record(actor, library, tracks, db=db)
        if len(pages) == 3:
            raise RuntimeError("last page failed")

    monkeypatch.setattr(recorder.history, "record_catalog", fail_last_page)
    with pytest.raises(RuntimeError, match="last page"):
        await recorder._record_kept("one", [("albumId", "native-album")])
    assert pages == [500, 500, 203]
    assert not await recorder.store.fetchone("SELECT 1 FROM collection_history")
    assert await recorder.store.fetchone("SELECT 1 FROM collection_pending")
    monkeypatch.setattr(recorder.history, "record_catalog", record)
    await recorder.drain()
    rows = await recorder.store.fetchall("SELECT catalog_data FROM collection_history")
    assert len(rows) == 1203
    assert {json.loads(r["catalog_data"])["ref"]["id"] for r in rows} == {
        f"track{n}" for n in range(1, 1204)
    }
    assert not await recorder.store.fetchone("SELECT 1 FROM collection_pending")


async def test_download_connection_failure_answers_502_without_attribution(
    recorder: CollectionRequests,
) -> None:
    recorder._tracks = AsyncMock(return_value=[track()])
    recorder.upstream.send.side_effect = httpx.ConnectError("unavailable")
    recorder._verify = AsyncMock()
    asked = call("download")
    reply = await recorder(asked, context(), Forward(asked))
    send = AsyncMock()
    await reply(AsyncMock(), send)
    assert send.await_args_list[0].args[0]["status"] == 502
    assert send.await_args_list[1].args[0]["body"] == b"Navidrome is unreachable\n"
    recorder._verify.assert_not_awaited()


@pytest.mark.parametrize("ranged", [False, True])
async def test_download_starts_and_delivers_body_while_verification_waits(
    recorder: CollectionRequests,
    ranged: bool,
) -> None:
    recorder._tracks = AsyncMock(return_value=[track()])
    verification_started, body_sent = anyio.Event(), anyio.Event()
    messages = []

    async def verify(*args):
        # A blocked verifier cannot hold either the start or the audio body.
        assert messages[0]["type"] == "http.response.start"
        verification_started.set()
        await body_sent.wait()
        return True

    recorder._verify = verify

    async def media(receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 206 if ranged else 200,
                "headers": [(b"content-type", b"audio/flac")],
            }
        )
        await verification_started.wait()
        await send({"type": "http.response.body", "body": b"audio"})

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            body_sent.set()

    reply = await recorder(call("download", ranged=ranged), context(), media)
    with anyio.fail_after(2):
        await reply(AsyncMock(), send)
    assert await recorder.store.fetchone("SELECT 1 FROM collection_history")


@pytest.mark.parametrize("at_start", [False, True])
async def test_accepted_download_keeps_attribution_on_disconnect(
    recorder: CollectionRequests,
    at_start: bool,
) -> None:
    recorder._tracks = AsyncMock(return_value=[track()])
    verification_started = anyio.Event()

    async def verify(*args):
        verification_started.set()
        await anyio.sleep(0.01)
        return None

    recorder._verify = verify

    async def media(receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 206,
                "headers": [(b"content-type", b"audio/flac")],
            }
        )
        await verification_started.wait()
        raise OSError("client disconnected")

    reply = await recorder(call("download", ranged=True), context(), media)
    send = AsyncMock(side_effect=OSError("client disconnected")) if at_start else AsyncMock()
    with pytest.raises(ExceptionGroup):
        await reply(AsyncMock(), send)
    assert await recorder.store.fetchone("SELECT 1 FROM collection_pending")
