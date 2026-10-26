"""Full-body capacity, generic readiness, cache binding and admission fairness."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import pytest

import shijhon.delivery.bulk as bulk_module
import shijhon.delivery.download_first as download_module
from shijhon.delivery.bulk import (
    BulkAcquisition,
    Progress,
    ProgressError,
    _Choice,
    effective_capacity,
)
from shijhon.delivery.download_first import DownloadFirst
from shijhon.delivery.limits import UserLimits
from shijhon.delivery.netpolicy import Reach
from shijhon.delivery.pacing import OPENINGS, PLAY, Limits
from shijhon.delivery.playback import Deliverer, NoSource, Opened, PlaybackSettings, Track
from shijhon.delivery.sources import Source, SourceRegistry, StoredSource
from shijhon.store import Store
from tests.harness.library import tone


class Body(httpx.AsyncByteStream):
    def __init__(self, server: Server) -> None:
        self.server = server
        self.closed = False
        self.tail = anyio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.server.audio[:4096]
        if not self.server.flowing:
            await self.tail.wait()
        yield self.server.audio[4096:]

    async def aclose(self) -> None:
        self.closed = True


class Server:
    def __init__(self) -> None:
        self.audio = tone(440, 3, "flac").read_bytes()
        self.calls: list[httpx.Request] = []
        self.bodies: list[Body] = []
        self.cached = False
        self.expired = False
        self.streams = anyio.Event()
        self.streams.set()
        self.flowing = False  # bodies send all at once (else the rest once ``tail`` is set)
        self.limited_streams = 0  # the first link requests answer 429 (Retry-After: 1)
        self.stream_status: int | None = None  # link requests answer this status

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/manifest.json":
            return httpx.Response(
                200,
                json={
                    "id": "demo",
                    "name": "Demo",
                    "resources": ["isrc", "stream", *(["cached"] if self.cached else [])],
                    "settings": [{"key": "quality", "default": "highest"}],
                },
            )
        if path == "/cached":
            return httpx.Response(200, json={"available": True, "selector": "actual-low-variant"})
        if path == "/resolve-isrc":
            return httpx.Response(200, json={"trackId": request.url.params["isrc"]})
        if path.startswith(("/stream/", "/cached-stream/")):
            if self.expired and path.startswith("/cached-stream/"):
                return httpx.Response(410)
            if self.limited_streams:
                self.limited_streams -= 1
                return httpx.Response(429, headers={"retry-after": "1"})
            if self.stream_status is not None:
                return httpx.Response(self.stream_status)
            await self.streams.wait()
            return httpx.Response(
                200,
                json={
                    "url": str(request.url.copy_with(path="/audio", query=None)),
                    "quality": "low",
                },
            )
        assert path == "/audio"
        body = Body(self)
        self.bodies.append(body)
        return httpx.Response(
            200,
            headers={"content-length": str(len(self.audio)), "content-type": "audio/flac"},
            stream=body,
        )

    def paths(self) -> list[str]:
        return [call.url.path for call in self.calls]


@asynccontextmanager
async def world(
    tmp_path: Path, count: int = 1, cached: bool = False
) -> AsyncIterator[tuple[BulkAcquisition, list[Source], list[Server]]]:
    store = await Store.open(tmp_path / "state.sqlite3")
    registry = SourceRegistry(store, limits=Limits(0, 4, 0))
    servers = [Server() for _ in range(count)]
    for index in range(count):
        await registry.add(str(index), f"https://demo{index}.example.invalid")
        servers[index].cached = cached
    sources = await registry.enabled()
    for source, server in zip(sources, servers, strict=True):
        await source.http.aclose()
        source.http = source.addon.http = httpx.AsyncClient(
            transport=httpx.MockTransport(server.handle)
        )
    deliverer = Deliverer(registry, PlaybackSettings(budget_seconds=2, cooldown_seconds=0.05))
    bulk = BulkAcquisition(deliverer)
    try:
        yield bulk, sources, servers
    finally:
        await bulk.aclose()
        await registry.aclose()
        await store.close()


def track(number: int) -> Track:
    return Track(str(number), f"DMXXX2600{number:03}", f"Song {number}", "Demo", 3000)


async def opened(bulk: BulkAcquisition, number: int, out: list[Opened], user: str = "ann") -> None:
    out.append(await bulk.open(track(number), user))


async def background(
    bulk: BulkAcquisition, number: int, out: list[Opened], progress: Progress | None = None
) -> None:
    with anyio.fail_after(5):
        out.append(await bulk.open(track(number), "ann", progress, background=True))


@pytest.mark.anyio
async def test_full_bodies_hold_two_slots_but_unknown_preparation_ends_at_valid_audio(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        first = await bulk.open(track(1), "ann")
        second = await bulk.open(track(2), "ann")
        assert len(bulk._active) == 2 and not any(lease.cold for lease in bulk._active)
        third: list[Opened] = []
        third_ready = anyio.Event()

        async def third_open() -> None:
            await opened(bulk, 3, third)
            third_ready.set()

        async with anyio.create_task_group() as group:
            group.start_soon(third_open)
            await anyio.sleep(0.03)
            assert not third and len(servers[0].bodies) == 2
            await first.close()
            with anyio.fail_after(2):
                await third_ready.wait()
        await second.close()
        await third[0].close()
        assert not bulk._active and all(body.closed for body in servers[0].bodies)


@pytest.mark.anyio
async def test_only_one_unknown_start_until_audio_comes(tmp_path: Path) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        servers[0].streams = anyio.Event()
        answers: list[Opened] = []
        async with anyio.create_task_group() as group:
            group.start_soon(opened, bulk, 1, answers)
            group.start_soon(opened, bulk, 2, answers)
            await anyio.sleep(0.05)
            assert sum(path.startswith("/stream/") for path in servers[0].paths()) == 1
            servers[0].streams.set()
        assert len(answers) == 2
        for answer in answers:
            await answer.close()


@pytest.mark.anyio
async def test_independent_ready_sources_use_free_capacity_and_shared_aliases_do_not(
    tmp_path: Path,
) -> None:
    async with world(tmp_path, 2, cached=True) as (bulk, sources, servers):
        bulk.source_downloads = 1
        first = await bulk.open(track(1), "ann")
        second = await bulk.open(track(2), "ann")
        assert [len(server.bodies) for server in servers] == [1, 1]
        await first.close()
        await second.close()
        for source in sources:
            source.capacity_group = "shared worker"
        first = await bulk.open(track(3), "ann")
        waiting: list[Opened] = []
        async with anyio.create_task_group() as group:
            group.start_soon(opened, bulk, 4, waiting)
            await anyio.sleep(0.03)
            assert not waiting
            await first.close()
        await waiting[0].close()


@pytest.mark.anyio
async def test_cached_variant_is_bound_and_expiry_never_resolves_normally(tmp_path: Path) -> None:
    async with world(tmp_path, cached=True) as (bulk, _, servers):
        answer = await bulk.open(track(1), "ann")
        cached_calls = [call for call in servers[0].calls if call.url.path.startswith("/cached")]
        assert cached_calls and all("quality" not in call.url.params for call in cached_calls)
        assert "/cached-stream/actual-low-variant" in servers[0].paths()
        assert not any(path.startswith(("/resolve", "/stream/")) for path in servers[0].paths())
        await answer.close()
        servers[0].expired = True
        with pytest.raises(NoSource):
            await bulk.open(track(2), "ann")
        assert not any(path.startswith(("/resolve", "/stream/")) for path in servers[0].paths())
        assert not bulk._active


@pytest.mark.anyio
async def test_grouped_429_prevents_sibling_requests_and_a_preparation_waits_it_out(
    tmp_path: Path,
) -> None:
    async with world(tmp_path, 2, cached=True) as (bulk, sources, servers):
        for source in sources:
            source.capacity_group = "one account"
            await source.addon.manifest()
        assert sources[0].pace is not None
        sources[0].pace.limited(0.5)  # (a rate limit lasts a second at least)
        for server in servers:
            server.calls.clear()
        # A client's download is not kept waiting: it fails at once.
        with pytest.raises(NoSource):
            await bulk.open(track(1), "ann")
        assert not servers[1].calls
        # A background song (playlist preparation) waits for the group's pause instead of
        # failing - and sends nothing to the group meanwhile: no check, no link, no audio.
        waited: list[Opened] = []
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: background(bulk, 1, waited))
            await anyio.sleep(0.5)
            assert sources[0].pace.blocked > 0
            assert not waited and not servers[0].calls and not servers[1].calls
            assert not bulk._active and not bulk._waiting
        assert len(waited) == 1 and sources[0].pace.blocked == 0
        await waited[0].close()
        sources[0].pace._blocked_until = 0
        bulk.source_downloads = 1
        held = bulk.observe_playback(sources[0])
        answers: list[Opened] = []
        async with anyio.create_task_group() as group:
            group.start_soon(opened, bulk, 2, answers)
            await anyio.sleep(0.03)
            sources[0].pace.limited(1)
            before = len(servers[1].calls)
            held()
            await anyio.sleep(0.05)
            assert not answers and len(servers[1].calls) == before
            group.cancel_scope.cancel()
        assert not bulk._active


@pytest.mark.anyio
async def test_fair_admission_prefers_another_waiting_user_and_cancellation_keeps_nothing(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        choice = _Choice(sources[0], permitted=True)
        _, first = await bulk._reserve("ann", [choice])
        first.prepared()
        _, second = await bulk._reserve("ann", [choice])
        second.prepared()
        order: list[str] = []

        async def wait(user: str) -> None:
            _, lease = await bulk._reserve(user, [choice])
            order.append(user)
            lease.close()

        async with anyio.create_task_group() as group:
            group.start_soon(wait, "ann")
            group.start_soon(wait, "bob")
            await anyio.sleep(0.03)
            first.close()
        assert order == ["bob", "ann"]
        second.close()
        assert not bulk._active and not bulk._waiting


def test_effective_capacity_joins_origins_and_groups_transitively() -> None:
    def source(ident: int, host: str, group: str, downloads: int | None) -> StoredSource:
        return StoredSource(
            ident,
            str(ident),
            host,
            {},
            True,
            ident,
            Reach.PUBLIC,
            None,
            downloads=downloads,
            capacity_group=group,
        )

    sources = [
        source(1, "https://a.example.invalid/first", "left", 4),
        source(2, "https://a.example.invalid/second", "right", None),
        source(3, "https://b.example.invalid", "right", 1),
        source(4, "https://c.example.invalid", "", 0),
    ]
    assert effective_capacity(sources) == {1: (1, 1), 2: (1, 1), 3: (1, 1), 4: (0, 1)}


@pytest.mark.anyio
async def test_observer_can_stop_before_upstream_work_without_a_leaked_or_paused_lease(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, _, servers):

        async def progress(state: str) -> None:
            if state == "preparing":
                raise ValueError("stopped")

        with pytest.raises(ProgressError):
            await bulk.open(track(1), "ann", progress)
        assert not bulk._active and not bulk._paused
        assert not any(path.startswith(("/resolve", "/stream/")) for path in servers[0].paths())


@pytest.mark.anyio
async def test_bulk_acquisition_does_not_change_the_play_s_pin_or_known_representation(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        play = await bulk.deliverer.open(track(1), None)
        pin = bulk.deliverer.pinned("1")
        known = bulk.deliverer._known["1"]
        await play.close()
        servers[0].audio = tone(440, 3, "mp3").read_bytes()
        acquired = await bulk.open(track(1), "ann")
        assert acquired.kind == "mp3"
        assert bulk.deliverer.pinned("1") is pin and bulk.deliverer._known["1"] == known
        await acquired.close()


@pytest.mark.anyio
async def test_cancellation_closes_full_body_and_releases_capacity(tmp_path: Path) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        answer = await bulk.open(track(1), "ann")
        assert answer.body is not None
        with anyio.move_on_after(0.03):
            async for _ in answer.body:
                pass
        assert not bulk._active and servers[0].bodies[0].closed


@pytest.mark.anyio
async def test_cancellation_during_unknown_preparation_pauses_cold_starts(tmp_path: Path) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        servers[0].streams = anyio.Event()
        with anyio.move_on_after(0.05):
            await bulk.open(track(1), "ann")
        assert not bulk._active and bulk._paused


@pytest.mark.anyio
async def test_global_user_limits_and_playback_headroom(tmp_path: Path) -> None:
    async with world(tmp_path, 2, cached=True) as (bulk, sources, _):
        bulk.downloads = 2
        bulk.users = 1
        first = await bulk.open(track(1), "ann")
        ann: list[Opened] = []
        async with anyio.create_task_group() as group:
            group.start_soon(opened, bulk, 2, ann)
            await anyio.sleep(0.02)
            second = await bulk.open(track(3), "bob")
            assert not ann
            await first.close()
            ended = bulk.observe_playback(sources[0])
            await anyio.sleep(0.02)
            assert not ann
            ended()
        for answer in [*ann, second]:
            await answer.close()


@pytest.mark.anyio
async def test_source_capacity_overrides_persist_clear_and_sync(tmp_path: Path) -> None:
    from shijhon.config import AddonSettings

    store = await Store.open(tmp_path / "state.sqlite3")
    registry = SourceRegistry(store)
    try:
        ident = await registry.add(
            "demo",
            "https://demo.example.invalid",
            downloads=1,
            preparations=0,
            capacity_group="account",
        )
        (stored,) = await registry.stored()
        assert (stored.downloads, stored.preparations, stored.capacity_group) == (1, 0, "account")
        await registry.update(
            ident, clear_downloads=True, clear_preparations=True, capacity_group=""
        )
        (stored,) = await registry.stored()
        assert (stored.downloads, stored.preparations, stored.capacity_group) == (None, None, "")
        configured = AddonSettings(
            name="demo",
            base_url="https://demo.example.invalid",
            downloads=3,
            preparations=2,
            capacity_group="shared",
        )
        assert await registry.differs([configured]) == ["demo"]
        await registry.sync([configured])
        assert await registry.differs([configured]) == []
        (source,) = await registry.enabled()
        assert (source.downloads, source.preparations, source.capacity_group) == (3, 2, "shared")
        for invalid in (-1, 1001, True):
            with pytest.raises(ValueError):
                await registry.update(ident, downloads=invalid)
    finally:
        await registry.aclose()
        await store.close()


@pytest.mark.anyio
async def test_now_playing_promotes_a_bulk_acquisition_waiting_for_capacity(tmp_path: Path) -> None:
    async with world(tmp_path) as (bulk, _, _):
        bulk.downloads = bulk.source_downloads = 1
        first = await bulk.open(track(1), "ann")
        promoted = anyio.Event()
        ready = anyio.Event()
        answer: list[Opened] = []

        async def next_song() -> None:
            answer.append(await bulk.open(track(2), "ann", playing=promoted.is_set))
            ready.set()

        async with anyio.create_task_group() as group:
            group.start_soon(next_song)
            await anyio.sleep(0.03)
            assert not ready.is_set()
            promoted.set()
            with anyio.fail_after(2):
                await ready.wait()
            assert len(bulk._active) == 2
        await first.close()
        await answer[0].close()


@pytest.mark.anyio
async def test_an_opening_paced_bulk_attempt_keeps_its_resolved_link_for_retry(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, sources, servers):
        source = sources[0]
        assert source.pace is not None
        await source.addon.manifest()
        source.pace.configure(Limits(0, 4, 1))
        bulk.preparations = 0  # isolate the independent audio-opening queue
        bulk.deliverer.settings.budget_seconds = 0.05
        release = await source.pace.open()
        try:
            with pytest.raises(NoSource, match=OPENINGS):
                await bulk.open(track(1), "ann")
        finally:
            release()
        assert source.stats.failures == 0 and not bulk._active
        servers[0].calls.clear()
        answer = await bulk.open(track(1), "ann")
        assert servers[0].paths() == ["/audio"]
        await answer.close()


@pytest.mark.anyio
@pytest.mark.parametrize("pause", ["rate limit", "cooldown", "429 on its link"])
async def test_a_paused_add_on_keeps_a_preparation_waiting_instead_of_failing(
    tmp_path: Path, pause: str
) -> None:
    async with world(tmp_path) as (bulk, sources, servers):
        source = sources[0]
        pace = source.pace
        assert pace is not None
        await source.addon.manifest()

        def paused() -> None:
            if pause == "rate limit":  # another request was answered "Retry-After: 1"
                pace.limited(1)
            elif pause == "cooldown":  # errors in a row
                bulk.deliverer.registry.cool_down(source.id, 1)
            else:  # this song's own link request is answered 429 once
                servers[0].limited_streams = 1

        paused()
        with pytest.raises(NoSource):  # a client's download: at once, as before
            await bulk.open(track(1), "ann")
        pace._blocked_until = 0
        bulk.deliverer.registry._cooldown.clear()
        paused()
        servers[0].calls.clear()
        waited: list[Opened] = []
        await background(bulk, 2, waited)
        assert len(waited) == 1 and waited[0].body is not None
        links = [p for p in servers[0].paths() if p.startswith("/stream/")]
        assert len(links) == (2 if pause == "429 on its link" else 1)
        await waited[0].close()
        assert not bulk._active


@pytest.mark.anyio
async def test_a_preparation_waits_for_a_paused_add_on_another_lacks_and_fails_when_none_can(
    tmp_path: Path,
) -> None:
    async with world(tmp_path, 2) as (bulk, sources, servers):
        for source in sources:
            await source.addon.manifest()
        servers[1].stream_status = 404  # the second add-on does not have it
        assert sources[0].pace is not None
        sources[0].pace.limited(1)
        waited: list[Opened] = []
        await background(bulk, 1, waited)  # from the first, once its pause has passed
        assert len(waited) == 1 and len(servers[0].bodies) == 1
        await waited[0].close()
        # No enabled add-on can serve it at all: no waiting.
        servers[0].stream_status = 404
        with anyio.fail_after(1), pytest.raises(NoSource):
            await bulk.open(track(2), "ann", background=True)
        assert not bulk._active and not bulk._waiting


@pytest.mark.anyio
async def test_a_preparation_waits_for_paused_add_ons_a_bounded_time_and_can_be_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bulk_module, "PAUSED_WAIT", 0.6)
    monkeypatch.setattr(bulk_module, "HEARD_EVERY", 0.1)
    monkeypatch.setattr(bulk_module, "_LOOK_AGAIN", 0.1)
    async with world(tmp_path) as (bulk, sources, servers):
        await sources[0].addon.manifest()
        pace = sources[0].pace
        assert pace is not None
        pace.limited(60)
        servers[0].calls.clear()
        heard: list[str] = []

        async def progress(stage: str) -> None:
            heard.append(stage)

        started = time.monotonic()
        with anyio.fail_after(3), pytest.raises(NoSource, match="stayed paused"):
            await bulk.open(track(1), "ann", progress, background=True)
        assert 0.5 < time.monotonic() - started < 2
        assert heard.count("waiting") >= 3 and not servers[0].calls

        async def cancelled(stage: str) -> None:
            if stage == "waiting" and heard:
                raise ValueError("the batch was cancelled")
            heard.append(stage)

        heard.clear()
        with anyio.fail_after(1), pytest.raises(ProgressError):
            await bulk.open(track(1), "ann", cancelled, background=True)
        # While it waits for its turn too.
        pace._blocked_until = 0
        bulk.source_downloads = 1
        first = await bulk.open(track(2), "ann")
        heard.clear()
        with anyio.fail_after(1), pytest.raises(ProgressError):
            await bulk.open(track(3), "ann", cancelled, background=True)
        await first.close()
        assert not bulk._active and not bulk._waiting
        assert not any(p.startswith("/stream/") and "003" in p for p in servers[0].paths())


class Engine:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.placed: list[str] = []

    async def refused(self) -> str | None:
        return None

    async def replace_with_delivered(self, song_id: str, path: Path) -> None:
        self.placed.append(song_id)
        self.rows[song_id]["state"] = "delivered"


def placeholder(number: int) -> dict[str, Any]:
    return {
        "song_id": str(number),
        "state": "placeholder",
        "track_ref": f"demo:{number}",
        "release_ref": "demo:release",
        "title": f"Song {number}",
        "artist": "Demo",
        "isrc": f"DMXXX2600{number:03}",
        "duration_ms": 3000,
        "disc": 1,
        "track": number,
        "tags": "{}",
        "backing_song_id": None,
    }


def fetcher_for(
    tmp_path: Path,
    bulk: BulkAcquisition,
    *,
    timeout: float = 0.5,
    limits: UserLimits | None = None,
) -> tuple[DownloadFirst, Engine]:
    """Download-first over ``bulk``, its placeholders 1-9 in memory (no library)."""
    rows = {str(number): placeholder(number) for number in range(1, 10)}
    engine = Engine(rows)
    fetcher = DownloadFirst(
        bulk.deliverer,
        cast(Any, engine),
        cast(Any, None),
        tmp_path / "work",
        timeout_seconds=timeout,
        limits=limits,
        bulk=bulk,
    )

    async def settled(song_id: str, **kwargs: Any) -> dict[str, Any] | None:
        return rows.get(song_id)

    fetcher.settled = settled  # type: ignore[method-assign]
    return fetcher, engine


class BatchStopped(Exception):
    pass


@pytest.mark.anyio
async def test_a_preparation_s_wait_for_a_slot_is_not_its_transfer_s_time(tmp_path: Path) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        bulk.source_downloads = 1
        fetcher, engine = fetcher_for(tmp_path, bulk)
        first = await bulk.open(track(1), "zed")  # holds the only slot, its body unread
        # A client's download still has the timeout for the whole fetch, its wait included.
        assert await fetcher.ensure("2", "bob", bulk=True) == "download timed out"
        assert len(servers[0].bodies) == 1
        results: list[str | None] = []

        async def prepare() -> None:
            results.append(await fetcher.ensure("3", "bob", bulk=True, background=True))

        async with anyio.create_task_group() as group:
            group.start_soon(prepare)
            await anyio.sleep(1.0)  # twice the timeout waiting for the slot
            assert not results and len(servers[0].bodies) == 1
            await first.close()
            with anyio.fail_after(2):
                while len(servers[0].bodies) < 2:
                    await anyio.lowlevel.checkpoint()
            servers[0].bodies[1].tail.set()
        assert results == [None] and engine.placed == ["3"]
        assert bulk.deliverer.pinned("3") is None  # in the library: its link is forgotten
        # Once admitted, a transfer that stalls still times out.
        started = time.monotonic()
        stalled = await fetcher.ensure("4", "bob", bulk=True, background=True)
        assert stalled == "download timed out" and time.monotonic() - started < 2
        assert not bulk._active


@pytest.mark.anyio
async def test_a_waiting_preparation_holds_up_no_client_request_and_stops_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bulk_module, "HEARD_EVERY", 0.1)
    async with world(tmp_path) as (bulk, sources, _):
        fetcher, _ = fetcher_for(tmp_path, bulk, limits=UserLimits(downloads=1))
        await sources[0].addon.manifest()
        assert sources[0].pace is not None
        sources[0].pace.limited(60)  # the only add-on is rate limited for a minute
        cancelled = False
        heard: list[str] = []

        async def progress(stage: str) -> None:
            heard.append(stage)
            if cancelled:
                raise BatchStopped

        done = anyio.Event()

        async def prepare() -> None:
            with pytest.raises(BatchStopped):
                await fetcher.ensure("1", "ann", bulk=True, progress=progress, background=True)
            done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(prepare)
            await anyio.sleep(0.3)
            # The user's download of that song and of another (one turn per user), and a
            # play of it, each answer at once: the preparation holds no turn and no lock.
            with anyio.fail_after(1.5):
                assert (await fetcher.ensure("1", "ann", bulk=True) or "").startswith("no source")
                assert (await fetcher.ensure("2", "ann", bulk=True) or "").startswith("no source")
                played = await fetcher.ensure("1", "bob", playing=True)
                assert (played or "").startswith("no source")
            cancelled = True  # heard while it waits for the pause: it stops
            with anyio.fail_after(1.5):
                await done.wait()
        assert heard.count("waiting") >= 2
        assert not bulk._active and not fetcher._waiting and not fetcher._locks.held("1")


@pytest.mark.anyio
async def test_a_preparation_finding_the_song_s_lock_taken_holds_no_slot_and_stops(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        fetcher, engine = fetcher_for(tmp_path, bulk, timeout=5)
        stop = False
        heard: list[str] = []

        async def progress(stage: str) -> None:
            heard.append(stage)
            if stage == "waiting" and stop:
                raise BatchStopped

        first: list[str | None] = []
        stopped = anyio.Event()

        async def ann() -> None:
            first.append(await fetcher.ensure("1", "ann", bulk=True, background=True))

        async def bob() -> None:
            with pytest.raises(BatchStopped):
                await fetcher.ensure("1", "bob", bulk=True, progress=progress, background=True)
            stopped.set()

        async with anyio.create_task_group() as group:
            group.start_soon(ann)
            with anyio.fail_after(2):
                while not servers[0].bodies:  # ann's transfer holds the song's lock
                    await anyio.lowlevel.checkpoint()
            group.start_soon(bob)
            await anyio.sleep(0.4)  # bob is admitted again and again: the lock is taken
            assert heard.count("waiting") >= 2 and len(bulk._active) == 1  # no slot kept
            stop = True
            with anyio.fail_after(1):
                await stopped.wait()
            servers[0].bodies[0].tail.set()
        assert first == [None] and engine.placed == ["1"]
        assert not bulk._active


@pytest.mark.anyio
async def test_an_admitted_preparation_gives_its_slot_to_the_user_s_waiting_request(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        bulk.source_downloads = 1
        servers[0].flowing = True
        fetcher, engine = fetcher_for(tmp_path, bulk, timeout=3, limits=UserLimits(downloads=1))
        first = await bulk.open(track(9), "zed")  # holds the only slot
        results: dict[str, str | None] = {}

        async def prepare() -> None:
            results["prepared"] = await fetcher.ensure("3", "ann", bulk=True, background=True)

        async def download() -> None:
            results["downloaded"] = await fetcher.ensure("2", "ann", bulk=True)

        async with anyio.create_task_group() as group:
            group.start_soon(prepare)
            await anyio.sleep(0.2)  # waiting for the slot first, holding no turn
            group.start_soon(download)
            await anyio.sleep(0.2)  # the user's only turn, waiting for the slot
            await first.close()
        # The preparation was admitted first, had no turn free and gave its slot back: the
        # user's download was not kept waiting behind it.
        assert results == {"prepared": None, "downloaded": None}
        assert engine.placed == ["2", "3"]
        assert not bulk._active and not fetcher._waiting


@pytest.mark.anyio
async def test_an_add_on_paused_when_the_choices_were_made_is_asked_once_it_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bulk_module, "PAUSED_WAIT", 2.0)
    async with world(tmp_path, 2) as (bulk, sources, servers):
        for source in sources:
            await source.addon.manifest()
        bulk.source_downloads = 1
        first = await bulk.open(track(1), "zed")  # the first add-on's only slot
        assert len(servers[0].bodies) == 1
        bulk.deliverer.registry.cool_down(sources[1].id, 0.6)  # the second, briefly
        waited: list[Opened] = []
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: background(bulk, 2, waited))
            await anyio.sleep(0.2)
            assert sources[0].pace is not None
            sources[0].pace.limited(60)  # now the first is rate limited too
        assert len(waited) == 1 and len(servers[1].bodies) == 1
        await waited[0].close()
        await first.close()


@pytest.mark.anyio
@pytest.mark.parametrize("cached", [False, True])
async def test_a_paused_backend_s_new_preparations_count_against_the_wait_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    monkeypatch.setattr(bulk_module, "PAUSED_WAIT", 0.6)
    async with world(tmp_path, cached=cached) as (bulk, sources, _):
        await sources[0].addon.manifest()
        # An uncertain preparation ended: new ones wait a long while on this backend.
        bulk._paused.append((sources[0], time.monotonic() + 60))
        if cached:  # cached work stays admissible
            waited: list[Opened] = []
            await background(bulk, 1, waited)
            await waited[0].close()
        else:
            with anyio.fail_after(3), pytest.raises(NoSource, match="stayed paused"):
                await bulk.open(track(1), "ann", background=True)
        assert not bulk._active and not bulk._waiting


@pytest.mark.anyio
async def test_a_pause_as_long_as_the_wait_bound_is_waited_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bulk_module, "PAUSED_WAIT", 1.0)
    async with world(tmp_path) as (bulk, sources, servers):
        await sources[0].addon.manifest()
        assert sources[0].pace is not None
        sources[0].pace.limited(1.0)
        waited: list[Opened] = []
        await background(bulk, 1, waited)
        assert len(waited) == 1 and len(servers[0].bodies) == 1
        await waited[0].close()


@pytest.mark.anyio
async def test_an_app_download_holding_the_song_s_lock_gets_the_slot_a_preparation_had(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        bulk.source_downloads = 1
        servers[0].flowing = True
        fetcher, engine = fetcher_for(tmp_path, bulk, timeout=2)
        first = await bulk.open(track(9), "zed")  # holds the only slot
        results: dict[str, str | None] = {}

        async def prepare() -> None:
            results["prepared"] = await fetcher.ensure("1", "ann", bulk=True, background=True)

        async def download() -> None:
            results["downloaded"] = await fetcher.ensure("1", "bob", bulk=True)

        async with anyio.create_task_group() as group:
            group.start_soon(prepare)
            await anyio.sleep(0.2)  # the preparation waits for the slot first
            group.start_soon(download)
            await anyio.sleep(0.2)  # another user's download: the song's lock, then the slot
            assert fetcher._locks.held("1")
            await first.close()
        # The preparation, admitted first, found the lock taken and gave its slot back:
        # the download was not left to time out behind it.
        assert results == {"prepared": None, "downloaded": None}
        assert engine.placed == ["1"] and len(servers[0].bodies) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("noticed", ["while waiting", "before failing"])
async def test_a_preparation_whose_song_another_fetch_placed_is_done_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, noticed: str
) -> None:
    if noticed == "while waiting":
        monkeypatch.setattr(bulk_module, "HEARD_EVERY", 0.1)
        monkeypatch.setattr(bulk_module, "PAUSED_WAIT", 30.0)
    else:  # (not looked at while it waits: before it reports a failure)
        monkeypatch.setattr(bulk_module, "HEARD_EVERY", 60.0)
        monkeypatch.setattr(bulk_module, "PAUSED_WAIT", 0.8)
    async with world(tmp_path) as (bulk, sources, servers):
        fetcher, engine = fetcher_for(tmp_path, bulk)
        await sources[0].addon.manifest()
        assert sources[0].pace is not None
        sources[0].pace.limited(60)
        servers[0].calls.clear()
        results: list[str | None] = []

        async def prepare() -> None:
            results.append(await fetcher.ensure("1", "ann", bulk=True, background=True))

        with anyio.fail_after(3):
            async with anyio.create_task_group() as group:
                group.start_soon(prepare)
                await anyio.sleep(0.3)
                engine.rows["1"]["state"] = "delivered"  # a play's fetch put it in place
        assert results == [None] and engine.placed == [] and not servers[0].calls


@pytest.mark.anyio
async def test_a_preparation_played_after_its_admission_attempts_as_a_play(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, _, servers):
        servers[0].flowing = True
        fetcher, engine = fetcher_for(tmp_path, bulk, limits=UserLimits())
        levels: list[int] = []
        attempt, in_turn = bulk.attempt, fetcher._in_turn

        async def attempted(admission: Any, progress: Progress | None = None) -> Opened | None:
            levels.append(admission.plan.urgency.level)
            return await attempt(admission, progress)

        async def played(song_id: str, turn: Any, *args: Any) -> str | None:
            # The client reports the song playing once the preparation has its turn.
            fetcher.promote("ann", download_module.track_of(placeholder(1)).key)
            return await in_turn(song_id, turn, *args)

        monkeypatch.setattr(bulk, "attempt", attempted)
        monkeypatch.setattr(fetcher, "_in_turn", played)
        assert await fetcher.ensure("1", "ann", bulk=True, background=True) is None
        assert levels == [PLAY] and engine.placed == ["1"]


@pytest.mark.anyio
async def test_a_preparation_whose_choice_is_disabled_waits_for_a_paused_add_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path, 2) as (bulk, sources, servers):
        for source in sources:
            await source.addon.manifest()
        bulk.source_downloads = 1
        first = await bulk.open(track(1), "zed")  # the first add-on's only slot
        registry = bulk.deliverer.registry
        registry.cool_down(sources[1].id, 0.8)  # the second, a while
        enabled = registry.enabled

        async def without_first() -> list[Source]:  # (the first disabled; mocks kept)
            return [s for s in await enabled() if s.id != sources[0].id]

        waited: list[Opened] = []
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: background(bulk, 2, waited))
            await anyio.sleep(0.2)  # it waits for the first add-on's slot
            monkeypatch.setattr(registry, "enabled", without_first)
        assert len(waited) == 1 and len(servers[1].bodies) == 1
        await waited[0].close()
        await first.close()
