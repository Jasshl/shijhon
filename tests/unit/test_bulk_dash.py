"""DASH owns acquisition capacity after the HTTP caller stops waiting."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import httpx
import pytest

from shijhon.delivery.addon import StreamInfo
from shijhon.delivery.bulk import _Choice
from shijhon.delivery.dash import Dash, Joined, Link
from shijhon.delivery.playback import _BULK_LEASE, _BULK_REUSE, NoSource, Pin
from tests.harness.dash_fixtures import dash_audio
from tests.unit.test_bulk import track, world
from tests.unit.test_dash import MANIFEST, Server


@pytest.mark.anyio
async def test_continuing_dash_holds_one_lease_and_retry_joins_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        server = Server(dash_audio("bulk-lease", 6))
        server.segment_delay = 0.15
        source = sources[0]
        original = source.addon.http
        dash_http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))

        async def stream(ident: str) -> StreamInfo:
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        source.http = dash_http
        bulk.source_downloads = 1
        bulk.deliverer.settings.budget_seconds = 0.03
        bulk.deliverer.settings.max_wait_seconds = 2
        bulk.deliverer.settings.dash_start = "complete"
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        wanted = replace(track(1), duration_ms=6000)
        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            with pytest.raises(NoSource):
                await bulk.open(wanted, "ann")
            assert len(bulk._active) == 1
            bulk.deliverer.settings.budget_seconds = 2
            answer = await bulk.open(wanted, "ann")
            assert not bulk._active  # joined bytes are local; reading is not upstream work
            await answer.close()
            assert not bulk._active
        assert sum(r.url.host == "manifests.example" for r in server.seen) == 1
        await original.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["quality", "start", "during_callback"])
async def test_changed_live_selection_counts_each_continuing_dash_job_until_its_own_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        server = Server(dash_audio("bulk-distinct-jobs", 6))
        source = sources[0]
        original = source.addon.http
        fetching = [anyio.Event(), anyio.Event()]
        finish = [anyio.Event(), anyio.Event()]
        ended = [anyio.Event(), anyio.Event()]
        current: ContextVar[int] = ContextVar("dash_test_job", default=-1)
        jobs: list[str] = []
        leases = []

        async def handle(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit("/", 1)[-1]
            if name.endswith("00003.m4s") and not request.headers.get("range"):
                index = current.get()
                fetching[index].set()
                await finish[index].wait()
            return await server.handle(request)

        source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

        async def stream(ident: str) -> StreamInfo:
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        bulk.source_downloads = bulk.downloads = 2
        bulk.preparations = 0
        settings = bulk.deliverer.settings
        settings.budget_seconds, settings.max_wait_seconds = 0.03, 5
        settings.dash_start = "complete"
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        manifest = dash._manifest

        async def started(*args: Any) -> tuple[bytes, str]:
            current.set(len(jobs))
            jobs.append(args[0].key)
            return await manifest(*args)

        monkeypatch.setattr(dash, "_manifest", started)
        changed = bulk.changed

        def released() -> None:
            changed()
            for index, lease in enumerate(leases):
                if not lease.active:
                    ended[index].set()

        monkeypatch.setattr(bulk, "changed", released)
        answers = []
        observed = False

        async def progress(stage: str) -> None:
            nonlocal observed
            if change == "during_callback" and stage == "preparing" and not observed:
                observed = True
                settings.dash_quality_from = "128"
                await anyio.lowlevel.checkpoint()

        wanted = replace(track(1), duration_ms=6000)

        async def retry() -> None:
            answers.append(await bulk.open(wanted, "ann", progress))

        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            with pytest.raises(NoSource):
                await bulk.open(wanted, "ann")
            assert fetching[0].is_set() and len(bulk._active) == 1
            leases.append(bulk._active[0])
            settings.budget_seconds = 4
            if change == "quality":
                settings.dash_quality_from = "128"
            elif change == "start":
                settings.dash_start = "at_once"
            group.start_soon(retry)
            with anyio.fail_after(2):
                await fetching[1].wait()
            assert len(bulk._active) == 2
            leases.append(next(lease for lease in bulk._active if lease is not leases[0]))
            finish[0].set()
            with anyio.fail_after(2):
                await ended[0].wait()
            assert bulk._active == [leases[1]] and not finish[1].is_set()
            finish[1].set()
            with anyio.fail_after(2):
                await ended[1].wait()
            assert not bulk._active
        assert len(jobs) == 2
        assert (jobs[0] == jobs[1]) == (change == "start")
        await answers[0].close()
        await original.aclose()


@pytest.mark.anyio
async def test_complete_dash_eof_frees_all_joiners_before_local_remux_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        server = Server(dash_audio("bulk-complete-joiners", 6))
        source = sources[0]
        original = source.addon.http
        finish, remuxing, local_done = anyio.Event(), anyio.Event(), anyio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit("/", 1)[-1]
            if name.endswith("00003.m4s") and not request.headers.get("range"):
                await finish.wait()
            return await server.handle(request)

        source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        bulk.deliverer.settings.dash_start = "complete"
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        remux = dash._remux

        async def slow_remux(*args: Any) -> float:
            remuxing.set()
            await local_done.wait()
            return await remux(*args)

        monkeypatch.setattr(dash, "_remux", slow_remux)
        pin = Pin("1", source, "demo", StreamInfo(MANIFEST, "dash"), time.monotonic())
        pin.seconds = 6
        entered, ready = 0, anyio.Event()
        joined = dash.joined

        async def joining(*args: Any, **kwargs: Any) -> Any:
            nonlocal entered
            entered += 1
            if entered == 3:
                ready.set()
            return await joined(*args, **kwargs)

        monkeypatch.setattr(dash, "joined", joining)
        answers = []

        async def reader() -> None:
            answers.append(
                await bulk.deliverer._joined(pin, None, False, time.monotonic() + 5, None)
            )

        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            for _ in range(3):
                group.start_soon(reader)
            with anyio.fail_after(2):
                await ready.wait()
            assert len(bulk._playing) == 3
            key, job = next(iter(dash._jobs.items()))
            assert len(job.capacities) == 3
            assert all(dash.continuing(key, "complete", capacity) for capacity in job.capacities)
            finish.set()
            with anyio.fail_after(2):
                await remuxing.wait()
            assert not bulk._playing and not answers
            local_done.set()
        for answer in answers:
            await answer.aclose()
        assert len(answers) == 3 and not bulk._playing
        await original.aclose()


@pytest.mark.anyio
async def test_local_cache_does_not_release_admission_belonging_to_another_continuing_job(
    tmp_path: Path,
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        source = sources[0]
        _, lease = await bulk._reserve("ann", [_Choice(source, permitted=True)])
        dash = Dash(tmp_path / "dash", "ffmpeg")
        cached = Joined(tmp_path / "local.flac", 1, '"cached"', "audio/flac", 3)
        dash._keep("other-quality", cached)
        link = Link(
            "other-quality", MANIFEST, {}, source.http, source.pace, source.name, lease, True
        )
        answer = await dash.joined(
            link,
            quality=("any", "lossless"),
            at_once=1,
            seconds=3,
            tolerance=1,
            deadline=time.monotonic() + 1,
            limited=lambda named: None,
        )
        assert answer is cached and bulk._active == [lease]
        lease.close()


@pytest.mark.anyio
async def test_changed_dash_cache_reacquires_before_resolving_and_opening_direct_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, plain):
        server = Server(dash_audio("bulk-direct-fallback", 6))
        source = sources[0]
        original = source.addon.http
        finish = anyio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/audio":
                return await plain[0].handle(request)
            name = request.url.path.rsplit("/", 1)[-1]
            if name == "chunk-stream2-00003.m4s" and not request.headers.get("range"):
                await finish.wait()
            return await server.handle(request)

        source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        direct = False
        resolution_reuses = []

        async def stream(ident: str) -> StreamInfo:
            resolution_reuses.append(_BULK_REUSE.get())
            if direct:
                return StreamInfo("https://demo0.example.invalid/audio", "direct")
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        bulk.downloads = bulk.source_downloads = 2
        settings = bulk.deliverer.settings
        settings.budget_seconds, settings.max_wait_seconds = 4, 5
        settings.dash_quality_to = "128"
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        wanted = replace(track(1), duration_ms=6000)
        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            cached = await bulk.open(wanted, "ann")
            assert cached.body is not None
            _ = [chunk async for chunk in cached.body]
            await cached.close()
            low = next(iter(dash._served.values()))
            if low.job is not None:
                await low.job.done.wait()
            assert low.file.complete and not bulk._active
            # The local variant can outlive its old pin, as after pin expiry.
            bulk._pins.clear()
            bulk.deliverer.forget(wanted.song_id)
            settings.dash_quality_to = "lossless"
            first = await bulk.open(wanted, "ann")
            old = bulk._active[0]
            published = bulk.deliverer.pinned(wanted.song_id)
            assert published is not None and published.info.transport == "dash"
            high = next(stream for stream in dash._served.values() if stream is not low)
            assert high.job is not None and not high.file.complete
            old_job = high.job
            await first.close()
            assert old.active
            direct = True

            async def progress(stage: str) -> None:
                if stage == "preparing":
                    settings.dash_quality_to = "128"
                    await anyio.lowlevel.checkpoint()

            answer = await bulk.open(wanted, "ann", progress)
            assert len(plain[0].bodies) == 1 and not plain[0].bodies[0].closed
            assert len(bulk._active) == 2
            assert bulk.deliverer.pinned(wanted.song_id) is published
            new = next(lease for lease in bulk._active if lease is not old)
            assert not any(resolution_reuses)
            finish.set()
            with anyio.fail_after(2):
                await old_job.done.wait()
            assert bulk._active == [new] and not plain[0].bodies[0].closed
            await answer.close()
            assert not bulk._active and plain[0].bodies[0].closed
        await original.aclose()


@pytest.mark.anyio
async def test_fresh_admission_joining_other_dash_job_cannot_follow_412_into_direct_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, plain):
        server = Server(dash_audio("bulk-fresh-join-fallback", 6))
        source = sources[0]
        original = source.addon.http
        finish = anyio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/audio":
                return await plain[0].handle(request)
            name = request.url.path.rsplit("/", 1)[-1]
            if name == "chunk-stream0-00003.m4s" and not request.headers.get("range"):
                await finish.wait()
            return await server.handle(request)

        source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        direct = False
        resolution_owners = []

        async def stream(ident: str) -> StreamInfo:
            capacity = _BULK_LEASE.get()
            assert capacity is not None
            resolution_owners.append(capacity.owner)
            if direct:
                return StreamInfo("https://demo0.example.invalid/audio", "direct")
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        bulk.downloads = bulk.source_downloads = 2
        settings = bulk.deliverer.settings
        settings.budget_seconds, settings.max_wait_seconds = 4, 5
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        wanted = replace(track(1), duration_ms=6000)
        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            high = await bulk.open(wanted, "ann")
            assert high.body is not None
            _ = [chunk async for chunk in high.body]
            await high.close()
            high_pin, high_lease = bulk._pins[wanted.key]
            high_stream = next(iter(dash._served.values()))
            if high_stream.job is not None:
                await high_stream.job.done.wait()
            assert high_stream.file.complete and not high_lease.active
            # A lower representation can be prepared while a client retains the high
            # representation's pin. Build that running job with its own clean pin.
            bulk._pins.clear()
            bulk.deliverer.forget(wanted.song_id)
            settings.dash_quality_to = "128"
            low = await bulk.open(wanted, "ann")
            low_lease = bulk._active[0]
            low_stream = next(
                stream for stream in dash._served.values() if stream is not high_stream
            )
            assert low_stream.job is not None and not low_stream.file.complete
            low_job = low_stream.job
            await low.close()
            bulk._pins[wanted.key] = high_pin, high_lease
            bulk.deliverer._remember(wanted.song_id, high_pin)
            direct = True

            answer = await bulk.open(wanted, "ann")
            assert len(plain[0].bodies) == 1 and not plain[0].bodies[0].closed
            assert len(bulk._active) == 2
            replacement = next(lease for lease in bulk._active if lease is not low_lease)
            assert replacement.owner is None and not any(resolution_owners)
            assert bulk.deliverer.pinned(wanted.song_id) is high_pin
            assert low_lease.owner is low_stream.owner
            finish.set()
            with anyio.fail_after(2):
                await low_job.done.wait()
            assert bulk._active == [replacement] and not plain[0].bodies[0].closed
            await answer.close()
            assert not bulk._active and plain[0].bodies[0].closed
        await original.aclose()


@pytest.mark.anyio
async def test_plain_playback_gets_new_admission_for_at_once_to_complete_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        server = Server(dash_audio("bulk-playback-whole-fallback", 6))
        server.ranges = False
        source = sources[0]
        original = source.addon.http
        source.http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))

        async def stream(ident: str) -> StreamInfo:
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            answer = await bulk.deliverer.open(replace(track(1), duration_ms=6000), None)
            assert answer.body is not None
            data = b"".join([chunk async for chunk in answer.body])
            assert data.startswith(b"fLaC") and not bulk._playing
            await answer.close()
        assert not bulk._playing and not bulk._active
        await original.aclose()


@pytest.mark.anyio
async def test_local_remux_releases_upstream_capacity_before_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, plain):
        server = Server(dash_audio("bulk-local-remux", 6))
        source = sources[0]
        original = source.addon.http

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/audio":
                return await plain[0].handle(request)
            return await server.handle(request)

        source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

        async def stream(ident: str) -> StreamInfo:
            if ident.endswith("001"):
                return StreamInfo(MANIFEST, "dash")
            return StreamInfo("https://demo0.example.invalid/audio", "direct")

        monkeypatch.setattr(source.addon, "stream", stream)
        bulk.source_downloads = bulk.downloads = 1
        bulk.deliverer.settings.dash_start = "complete"
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        remuxing, finish = anyio.Event(), anyio.Event()
        remux = dash._remux

        async def slow_remux(*args: Any) -> float:
            remuxing.set()
            await finish.wait()
            return await remux(*args)

        monkeypatch.setattr(dash, "_remux", slow_remux)
        answers = []

        async def first() -> None:
            answers.append(await bulk.open(replace(track(1), duration_ms=6000), "ann"))

        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            group.start_soon(first)
            with anyio.fail_after(2):
                await remuxing.wait()
            assert not bulk._active and not answers
            with anyio.fail_after(1):
                second = await bulk.open(track(2), "bob")
            assert len(bulk._active) == 1 and not answers
            await second.close()
            finish.set()
        await answers[0].close()
        assert not bulk._active
        await original.aclose()


@pytest.mark.anyio
async def test_a_complete_dash_file_gets_fresh_reader_lease_without_leaking_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        server = Server(dash_audio("bulk-cached-reader", 6))
        source = sources[0]
        original = source.addon.http
        source.http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))

        async def stream(ident: str) -> StreamInfo:
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        wanted = replace(track(1), duration_ms=6000)
        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            first = await bulk.deliverer.open(wanted, None)
            assert first.body is not None
            data = b"".join([chunk async for chunk in first.body])
            await first.close()
            for _ in range(2):
                cached = await bulk.deliverer.open(wanted, "bytes=0-99")
                assert cached.status == 206 and cached.body is not None
                assert b"".join([chunk async for chunk in cached.body]) == data[:100]
                await cached.close()
                assert all(stream.file.readers == 0 for stream in dash._served.values())
            assert not bulk._playing
        assert sum(r.url.host == "manifests.example" for r in server.seen) == 1
        await original.aclose()


@pytest.mark.anyio
async def test_dash_eof_frees_all_three_reader_capacities_while_readers_remain_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        server = Server(dash_audio("bulk-two-readers", 6))
        source = sources[0]
        original = source.addon.http
        finish = anyio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit("/", 1)[-1]
            if name.endswith("00003.m4s") and not request.headers.get("range"):
                await finish.wait()
            return await server.handle(request)

        source.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

        async def stream(ident: str) -> StreamInfo:
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        wanted = replace(track(1), duration_ms=6000)
        finished = anyio.Event()
        ended = dash._ended

        def completed(*args: Any) -> None:
            ended(*args)
            if args[0] is not None and args[0].file.complete:
                finished.set()

        monkeypatch.setattr(dash, "_ended", completed)
        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            first = await bulk.deliverer.open(wanted, None)
            second = await bulk.deliverer.open(wanted, None)
            third = await bulk.deliverer.open(wanted, None)
            assert len(bulk._playing) == 3
            assert sum(stream.file.readers for stream in dash._served.values()) == 3
            finish.set()
            with anyio.fail_after(2):
                await finished.wait()
            assert not bulk._playing  # no reader has closed or consumed its local body
            assert sum(stream.file.readers for stream in dash._served.values()) == 3
            await first.close()
            await second.close()
            await third.close()
        assert all(stream.file.readers == 0 for stream in dash._served.values())
        await original.aclose()


@pytest.mark.anyio
async def test_dash_spawn_refusal_releases_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, _):

        async def stream(ident: str) -> StreamInfo:
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(sources[0].addon, "stream", stream)
        bulk.deliverer.dash = Dash(tmp_path / "dash", "ffmpeg", spawn=lambda work: False)
        with pytest.raises(NoSource):
            await bulk.open(track(1), "ann")
        assert not bulk._active


@pytest.mark.anyio
async def test_dash_upstream_completion_releases_lease_without_any_http_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path) as (bulk, sources, _):
        server = Server(dash_audio("bulk-orphan", 6))
        server.segment_delay = 0.15
        source = sources[0]
        original = source.addon.http
        source.http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))

        async def stream(ident: str) -> StreamInfo:
            return StreamInfo(MANIFEST, "dash")

        monkeypatch.setattr(source.addon, "stream", stream)
        bulk.deliverer.settings.budget_seconds = 0.03
        bulk.deliverer.settings.max_wait_seconds = 2
        bulk.deliverer.settings.dash_start = "complete"
        dash = Dash(tmp_path / "dash", shutil.which("ffmpeg") or "ffmpeg")
        bulk.deliverer.dash = dash
        async with anyio.create_task_group() as group:

            def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
                group.start_soon(work)

            dash.spawn = spawn
            with pytest.raises(NoSource):
                await bulk.open(replace(track(1), duration_ms=6000), "ann")
            assert len(bulk._active) == 1
            finished = anyio.Event()
            changed = bulk.changed

            def ended() -> None:
                changed()
                if not bulk._active:
                    finished.set()

            monkeypatch.setattr(bulk, "changed", ended)
            with anyio.fail_after(3):
                await finished.wait()
        await original.aclose()
