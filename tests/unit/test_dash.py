"""Joins of DASH links (``delivery.dash``) behind a mock transport: one join for requests
at once, a join that outlives the request that started it - up to the wait cap, as urgent
as the requests waiting for it -, only so many joins at once, the most urgent first, the
link's headers for its own origin only, ffmpeg killed when a join is canceled; and the
kept files - the least recently used go past the size, never the newest or one being read,
and what a stop left behind goes at start."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from shijhon.delivery import dash as dash_module
from shijhon.delivery import pacing
from shijhon.delivery.dash import Dash, DashError, Joined, Link, Stream, _Job, _process
from tests.harness.dash_fixtures import dash_audio

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
MANIFEST = "https://manifests.example/t/1/out.mpd?sig=made-up"
CDN = "https://cdn.example/seg/"


class Server:
    """The manifest on one origin (its segments' addresses on another), the segments there.
    ``manifest``: the manifest's answer instead (a status, or an exception raised);
    ``moved``: the manifest's address redirects to another origin first."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.seen: list[httpx.Request] = []
        self.times: list[float] = []  # when each request came
        self.delay = 0.0
        self.manifest: int | Exception | None = None
        self.moved = False
        self.ranges = True  # the segments answer a range (a probe's one byte)
        self.span: str | None = None  # a range's Content-Range instead ("bytes 0-0/*")
        self.segment_delay = 0.0  # each media segment's (not a probe's)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        self.times.append(time.monotonic())
        if self.delay:
            await anyio.sleep(self.delay)
        if request.url.host == "manifests.example" and self.moved:
            return httpx.Response(302, headers={"location": "https://mirror.example/out.mpd"})
        if request.url.host in ("manifests.example", "mirror.example"):
            if isinstance(self.manifest, Exception):
                raise self.manifest
            if self.manifest is not None:
                return httpx.Response(self.manifest)
            text = (self.folder / "out.mpd").read_text()
            for attribute in ('initialization="', 'media="'):
                text = text.replace(attribute, attribute + CDN)
            return httpx.Response(200, text=text, headers={"content-type": "application/dash+xml"})
        name = request.url.path.rsplit("/", 1)[1]
        data = (self.folder / name).read_bytes()
        wanted = request.headers.get("range", "")
        if self.segment_delay and not wanted and not name.startswith("init"):
            await anyio.sleep(self.segment_delay)
        if wanted.startswith("bytes=") and self.ranges:
            first, _, last = wanted[6:].partition("-")
            start, end = int(first), min(int(last), len(data) - 1)
            span = self.span or f"bytes {start}-{end}/{len(data)}"
            return httpx.Response(
                206, content=data[start : end + 1], headers={"content-range": span}
            )
        return httpx.Response(200, content=data)


def link(server: Server, **headers: str) -> Link:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    return Link("k" * 64, MANIFEST, headers, http, None, "Made-up")


async def joined(
    joiner: Dash, the_link: Link, deadline: float = 30.0, lasting: float = 0.0
) -> Joined:
    return await joiner.joined(
        the_link,
        quality=("any", "lossless"),
        at_once=4,
        seconds=6.0,
        tolerance=1.0,
        deadline=time.monotonic() + deadline,
        lasting=lasting,
        limited=lambda named: None,
    )


@pytest.mark.anyio
async def test_requests_at_once_share_one_join_and_the_headers_stay_at_the_link_s_origin(
    tmp_path: Path,
) -> None:
    server = Server(dash_audio("unit-join", 6))
    joiner = Dash(tmp_path, FFMPEG)
    the_link = link(server, authorization="Bearer made-up")
    results: list[Joined] = []

    async def one() -> None:
        results.append(await joined(joiner, the_link))

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(one)
    assert len({id(r) for r in results}) == 1 and results[0].content_type == "audio/flac"
    manifests = [r for r in server.seen if r.url.host == "manifests.example"]
    segments = [r for r in server.seen if r.url.host == "cdn.example"]
    assert len(manifests) == 1 and len(segments) == 4  # the FLAC's init and three segments
    assert manifests[0].headers["authorization"] == "Bearer made-up"
    assert all("authorization" not in r.headers for r in segments)
    assert all(r.headers["accept-encoding"] == "identity" for r in server.seen)
    # Kept: asked for again, nothing is fetched.
    server.seen.clear()
    assert await joined(joiner, the_link) is results[0] and server.seen == []


@pytest.mark.anyio
async def test_the_link_s_headers_are_not_sent_on_to_another_origin(tmp_path: Path) -> None:
    server = Server(dash_audio("unit-moved", 6))
    server.moved = True
    joiner = Dash(tmp_path, FFMPEG)
    await joined(joiner, link(server, authorization="Bearer made-up", **{"x-key": "made-up"}))
    first, moved = server.seen[0], server.seen[1]
    assert first.headers["x-key"] == "made-up" and first.headers["authorization"]
    assert moved.url.host == "mirror.example"
    assert "x-key" not in moved.headers and "authorization" not in moved.headers


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer,kind,reason",
    [
        (503, "error", "HTTP 503"),
        (httpx.ConnectError("refused"), "error", "ConnectError"),
        (httpx.ReadTimeout("slow"), "timeout", "ReadTimeout"),
        (404, "failed", "HTTP 404"),
        (410, "expired", "HTTP 410"),
    ],
)
async def test_a_manifest_s_failure_is_told_as_a_direct_link_s_would_be(
    tmp_path: Path, answer: int | Exception, kind: str, reason: str
) -> None:
    server = Server(dash_audio("unit-manifest-fails", 6))
    server.manifest = answer
    with pytest.raises(DashError) as failed:
        await joined(Dash(tmp_path, FFMPEG), link(server))
    assert (failed.value.kind, str(failed.value)) == (kind, reason)


@pytest.mark.anyio
async def test_a_later_request_s_deadline_keeps_a_join_going(tmp_path: Path) -> None:
    """A probe's short time starts the join; the play that waits for it has more."""
    server = Server(dash_audio("unit-extended", 6))
    server.delay = 0.2  # the manifest and four segments, one after another: about 1 s
    outcomes: list[Any] = []
    async with anyio.create_task_group() as background:

        def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
            background.start_soon(work)
            return True

        joiner = Dash(tmp_path, FFMPEG, spawn=spawn)
        the_link = link(server)

        async def probe() -> None:
            with anyio.move_on_after(0.4):
                await joined(joiner, the_link, deadline=0.3)

        async def play() -> None:
            await anyio.sleep(0.1)
            outcomes.append(await joined(joiner, the_link, deadline=30.0))

        async with anyio.create_task_group() as group:
            group.start_soon(probe)
            group.start_soon(play)
    assert isinstance(outcomes[0], Joined) and outcomes[0].path.exists()
    assert len([r for r in server.seen if r.url.host == "manifests.example"]) == 1


@pytest.mark.anyio
async def test_no_join_while_shijhon_stops(tmp_path: Path) -> None:
    joiner = Dash(tmp_path, FFMPEG, spawn=lambda work: False)
    with pytest.raises(DashError, match="stopping"):
        await joined(joiner, link(Server(dash_audio("unit-stopping", 6))), deadline=30.0)
    assert joiner._jobs == {}


@pytest.mark.anyio
async def test_a_join_outlives_the_request_that_started_it(tmp_path: Path) -> None:
    server = Server(dash_audio("unit-outlives", 6))
    server.delay = 0.2
    async with anyio.create_task_group() as background:

        def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
            background.start_soon(work)
            return True

        joiner = Dash(tmp_path, FFMPEG, spawn=spawn)
        the_link = link(server)
        with anyio.move_on_after(0.3):  # the client leaves while the segments come
            await joined(joiner, the_link)
        assert joiner._files == {}
        with anyio.fail_after(10):
            while not joiner._files:  # noqa: ASYNC110 (the background's join: no event)
                await anyio.sleep(0.05)
    kept = next(iter(joiner._files.values()))
    assert kept.path.exists() and kept.seconds == pytest.approx(6.0, abs=0.05)
    left = [p.name async for p in anyio.Path(tmp_path).iterdir()]
    assert not [name for name in left if name.startswith(".")]  # no part left


@pytest.mark.anyio
async def test_a_join_goes_on_up_to_the_wait_cap_and_then_leaves_nothing(tmp_path: Path) -> None:
    """The request gives up after 0.3 s; its join goes on for the wait cap (1 s) - not to
    its end (the segments' delay) - and then leaves nothing behind."""
    server = Server(dash_audio("unit-capped", 6))
    server.delay = 0.6  # the manifest, the init segment, the segments: about 1.8 s
    async with anyio.create_task_group() as background:

        def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
            background.start_soon(work)
            return True

        joiner = Dash(tmp_path, FFMPEG, spawn=spawn)
        started = anyio.current_time()
        with anyio.move_on_after(0.3):
            await joined(joiner, link(server), deadline=0.3, lasting=1.0)
        assert joiner.joining("k" * 64)
        with anyio.fail_after(5):
            while joiner.joining("k" * 64):  # noqa: ASYNC110 (the background's join)
                await anyio.sleep(0.02)
        assert 0.9 < anyio.current_time() - started < 1.6
    assert joiner._files == {} and [p async for p in anyio.Path(tmp_path).iterdir()] == []


@pytest.mark.anyio
async def test_a_join_is_as_urgent_as_the_requests_waiting_for_it(tmp_path: Path) -> None:
    """Started by warm-ahead: background work; a play waits: the play's; nobody waits:
    background work again."""
    server = Server(dash_audio("unit-urgency", 6))
    server.delay = 0.3
    levels: list[int] = []
    async with anyio.create_task_group() as background:

        def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
            background.start_soon(work)
            return True

        joiner = Dash(tmp_path, FFMPEG, spawn=spawn)
        the_link = link(server)

        async def waiting(level: int, seconds: float) -> None:
            with pacing.urgent(level), anyio.move_on_after(seconds):
                await joined(joiner, the_link, deadline=seconds, lasting=10.0)

        async def watch() -> None:
            await anyio.sleep(0.1)
            job = joiner._jobs["k" * 64]
            levels.append(job.urgency.level)  # warm-ahead's alone
            await anyio.sleep(0.2)
            levels.append(job.urgency.level)  # a play's too
            await anyio.sleep(0.3)
            levels.append(job.urgency.level)  # neither waits any longer

        async with anyio.create_task_group() as group:
            group.start_soon(waiting, pacing.WARM, 0.2)
            group.start_soon(watch)
            await anyio.sleep(0.15)
            group.start_soon(waiting, pacing.PLAY, 0.3)
    assert levels == [pacing.WARM, pacing.PLAY, pacing.WARM]
    assert len(joiner._files) == 1  # (joined in the end, nobody waiting)


def spawning(background: anyio.abc.TaskGroup) -> Callable[..., bool]:
    def spawn(work: Callable[[], Coroutine[Any, Any, None]]) -> bool:
        background.start_soon(work)
        return True

    return spawn


async def one(
    joiner: Dash, name: str, urgency: int | pacing.Urgency, servers: dict[str, Server]
) -> None:
    """A join of its own (the key ``name``), from a server of its own, at ``urgency``."""
    server = servers[name] = Server(dash_audio("unit-turns", 6))
    server.delay = 0.1
    the_link = Link(name * 64, MANIFEST, {}, link(server).http, None, "Made-up")
    with pacing.urgent(urgency):
        await joined(joiner, the_link)


@pytest.mark.anyio
async def test_only_so_many_joins_at_once_besides_the_songs_being_played(tmp_path: Path) -> None:
    """One at a time: a warm-ahead's join runs; another warm-ahead's waits; a play's starts
    at once (and counts); a fetch ahead's waits, and goes before the warm-ahead's."""
    servers: dict[str, Server] = {}
    async with anyio.create_task_group() as background:
        joiner = Dash(tmp_path, FFMPEG, spawn=spawning(background))
        joiner.joins_at_once = 1
        async with anyio.create_task_group() as group:
            group.start_soon(one, joiner, "a", pacing.WARM, servers)
            await anyio.sleep(0.03)
            group.start_soon(one, joiner, "b", pacing.WARM, servers)
            await anyio.sleep(0.03)
            group.start_soon(one, joiner, "c", pacing.PLAY, servers)
            await anyio.sleep(0.03)
            group.start_soon(one, joiner, "d", pacing.QUEUED, servers)
            await anyio.sleep(0.03)
            assert joiner._turns.taken == 2 and len(joiner._jobs) == 4
    times = {name: server.times for name, server in servers.items()}
    assert min(times["c"]) < max(times["a"])  # not held back
    assert min(times["d"]) > max(max(times["a"]), max(times["c"]))
    assert min(times["b"]) > max(times["d"])


@pytest.mark.anyio
async def test_a_request_raised_to_the_song_being_played_takes_its_join_along(
    tmp_path: Path,
) -> None:
    """A warm-ahead waits for its join, which waits for its turn; the client reports the
    song as playing: the join starts at once."""
    servers: dict[str, Server] = {}
    warm = pacing.Urgency(pacing.WARM)
    async with anyio.create_task_group() as background:
        joiner = Dash(tmp_path, FFMPEG, spawn=spawning(background))
        joiner.joins_at_once = 1
        async with anyio.create_task_group() as group:
            group.start_soon(one, joiner, "a", pacing.WARM, servers)
            await anyio.sleep(0.03)
            group.start_soon(one, joiner, "b", warm, servers)
            await anyio.sleep(0.03)
            assert joiner._turns.taken == 1 and servers["b"].seen == []
            warm.raise_to(pacing.PLAY)
            await anyio.sleep(0.03)
            assert joiner._turns.taken == 2
    times = {name: server.times for name, server in servers.items()}
    assert min(times["b"]) < max(times["a"])


@pytest.mark.anyio
async def test_a_request_whose_time_ran_out_is_told_whether_its_join_goes_on(
    tmp_path: Path,
) -> None:
    joiner = Dash(tmp_path, FFMPEG)
    now = anyio.current_time()
    joiner._jobs["a" * 64] = _Job(anyio.CancelScope(deadline=now + 10))
    joiner._jobs["b" * 64] = _Job(anyio.CancelScope(deadline=now + 0.5))  # it ends now
    assert joiner.outlasts("a" * 64) == "first" and joiner.outlasts("a" * 64) == "again"
    assert joiner.outlasts("b" * 64) is None and joiner.outlasts("c" * 64) is None
    kept(joiner, "c" * 64, tmp_path, 10)  # joined a moment ago
    assert joiner.outlasts("c" * 64) == "kept"


@pytest.mark.anyio
async def test_a_join_past_its_deadline_is_a_timeout_and_leaves_nothing(tmp_path: Path) -> None:
    server = Server(dash_audio("unit-deadline", 6))
    server.delay = 0.5
    joiner = Dash(tmp_path, FFMPEG)
    with pytest.raises(DashError) as failed:
        await joined(joiner, link(server), deadline=0.8)
    assert failed.value.kind == "timeout"
    assert [p async for p in anyio.Path(tmp_path).iterdir()] == []


@pytest.mark.anyio
async def test_a_remux_that_fails_is_an_error_and_ffmpeg_is_killed_when_canceled(
    tmp_path: Path,
) -> None:
    server = Server(dash_audio("unit-remux", 6))
    with pytest.raises(DashError, match="could not be remuxed") as failed:
        await joined(Dash(tmp_path, shutil.which("false") or "false"), link(server))
    assert failed.value.kind == "error"
    started = time.monotonic()
    with anyio.move_on_after(0.3):
        await _process(["sleep", "30"], 60)
    assert time.monotonic() - started < 2  # killed and waited for, not left running


def kept(joiner: Dash, key: str, folder: Path, size: int) -> Joined:
    path = folder / f"{key[:24]}-{'0' * 32}.flac"
    path.write_bytes(b"x" * size)
    entry = Joined(path, size, '"e"', "audio/flac", 1.0)
    joiner._keep(key, entry)
    return entry


@pytest.mark.anyio
async def test_past_the_size_the_least_recently_used_go_never_the_newest_or_one_being_read(
    tmp_path: Path,
) -> None:
    now = [0.0]
    joiner = Dash(tmp_path, FFMPEG, max_bytes=250, clock=lambda: now[0])

    def later(key: str, size: int) -> Joined:
        now[0] += 10  # (past the moment a file just joined is kept for its waiters)
        return kept(joiner, key, tmp_path, size)

    first = later("a" * 64, 100)
    second = later("b" * 64, 100)
    reading = joiner.body(first, 0, 9)  # a request reads the oldest
    third = later("c" * 64, 100)  # 300 > 250: the oldest not being read
    assert list(joiner._files) == ["a" * 64, "c" * 64]
    assert first.path.exists() and not second.path.exists()
    assert joiner._kept("a" * 64) is first  # (used: the newest but one now)
    fourth = later("d" * 64, 400)  # larger than the size on its own
    assert list(joiner._files) == ["a" * 64, "d" * 64] and not third.path.exists()
    chunks = [chunk async for chunk in reading]  # read to its end: closed
    assert chunks == [b"x" * 10]
    later("e" * 64, 10)
    assert list(joiner._files) == ["e" * 64] and not first.path.exists()
    assert not fourth.path.exists()


def test_a_file_just_joined_is_not_pushed_out_before_its_waiters_take_it(tmp_path: Path) -> None:
    now = [0.0]
    joiner = Dash(tmp_path, FFMPEG, max_bytes=0, clock=lambda: now[0])
    first = kept(joiner, "a" * 64, tmp_path, 100)
    now[0] += 1
    kept(joiner, "b" * 64, tmp_path, 100)  # another join's, a moment later: both kept
    assert list(joiner._files) == ["a" * 64, "b" * 64] and first.path.exists()
    now[0] += 10
    kept(joiner, "c" * 64, tmp_path, 100)
    assert list(joiner._files) == ["c" * 64] and not first.path.exists()


@pytest.mark.anyio
async def test_a_file_replaced_while_it_is_read_goes_when_its_reader_is_done(
    tmp_path: Path,
) -> None:
    joiner = Dash(tmp_path, FFMPEG)
    old = kept(joiner, "a" * 64, tmp_path, 10)
    reading = joiner.body(old, 0, 4)
    new_path = tmp_path / f"{'a' * 24}-{'1' * 32}.flac"
    new_path.write_bytes(b"y" * 10)
    joiner._keep("a" * 64, Joined(new_path, 10, '"f"', "audio/flac", 1.0))
    assert old.path.exists() and old.gone
    await reading.aclose()
    assert not old.path.exists() and new_path.exists()


def test_what_a_stop_left_behind_goes_at_start(tmp_path: Path) -> None:
    for name in (
        f".{'a' * 32}.part",
        f".{'b' * 32}.m4a",
        f"{'c' * 24}-{'d' * 32}.flac",
        f"{'c' * 24}-{'e' * 32}.mp3",
    ):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not a join's")
    assert Dash(tmp_path, FFMPEG).clear() == 4
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]
    assert Dash(tmp_path / "missing", FFMPEG).clear() == 0


def test_ffmpeg_is_looked_for_on_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash_module.shutil, "which", lambda name: None)
    assert dash_module.ffmpeg_path() is None


# --- served at once ---------------------------------------------------------------------


async def served(joiner: Dash, the_link: Link, deadline: float = 30.0) -> Stream:
    return await joiner.served(
        the_link,
        quality=("any", "lossless"),
        at_once=4,
        seconds=6.0,
        tolerance=1.0,
        deadline=time.monotonic() + deadline,
        lasting=30.0,
        limited=lambda named: None,
    )


async def read(joiner: Dash, stream: Stream, first: int = 0, last: int | None = None) -> bytes:
    body = joiner.stream_body(stream, first, stream.file.size - 1 if last is None else last)
    try:
        return b"".join([chunk async for chunk in body])
    finally:
        await body.aclose()


@pytest.mark.anyio
async def test_a_file_served_at_once_is_its_segments_behind_their_index(tmp_path: Path) -> None:
    folder = dash_audio("unit-served", 6)
    server = Server(folder)
    the_link = link(server, authorization="Bearer made-up")
    async with anyio.create_task_group() as group:
        joiner = Dash(tmp_path, FFMPEG, spawn=group.start_soon)
        stream = await served(joiner, the_link)
        data = await read(joiner, stream)
        with anyio.fail_after(5):
            while not stream.file.complete:  # noqa: ASYNC110
                await anyio.sleep(0.01)
    init = (folder / "init-stream2.m4s").read_bytes()
    assert data.startswith(init) and data[len(init) + 4 : len(init) + 8] == b"sidx"
    assert stream.file.path.read_bytes() == data and stream.file.content_type == "audio/mp4"
    probes = [r for r in server.seen if r.headers.get("range") == "bytes=0-0"]
    assert len(probes) == 3
    cdn = [r for r in server.seen if r.url.host == "cdn.example"]
    assert all("authorization" not in r.headers for r in cdn)  # (the link's origin only)
    # Asked for again: the kept file, nothing fetched.
    server.seen.clear()
    assert await served(joiner, the_link) is stream and server.seen == []


@pytest.mark.anyio
@pytest.mark.parametrize("answer", ["whole", "unknown-total"])
async def test_segments_that_tell_no_size_are_joined_instead(tmp_path: Path, answer: str) -> None:
    """A probe answered with the whole segment (200), or with a range of no stated total."""
    server = Server(dash_audio("unit-sizeless", 6))
    server.ranges = answer != "whole"
    server.span = "bytes 0-0/*" if answer == "unknown-total" else None
    async with anyio.create_task_group() as group:
        joiner = Dash(tmp_path, FFMPEG, spawn=group.start_soon)
        with pytest.raises(DashError) as failed:
            await served(joiner, link(server))
        assert failed.value.kind == "whole"
        server.seen.clear()
        with pytest.raises(DashError):  # remembered: not planned again
            await served(joiner, link(server))
        assert server.seen == []
        joined_file = await joined(joiner, link(server))
        assert joined_file.content_type == "audio/flac"


@pytest.mark.anyio
async def test_a_file_being_read_is_not_pushed_out_the_newest_never(tmp_path: Path) -> None:
    async with anyio.create_task_group() as group:
        joiner = Dash(tmp_path, FFMPEG, max_bytes=1, spawn=group.start_soon)
        first_link = link(Server(dash_audio("unit-evict-1", 6)))
        first = await served(joiner, first_link)
        body = joiner.stream_body(first, 0, 9)  # a reader holds it
        with anyio.fail_after(5):
            while not first.file.complete:  # noqa: ASYNC110
                await anyio.sleep(0.01)
        other = link(Server(dash_audio("unit-evict-2", 6)))
        other = Link("o" * 64, other.url, other.headers, other.http, None, "Made-up")
        dash_module.FRESH_SECONDS, fresh = 0.0, dash_module.FRESH_SECONDS
        try:
            second = await served(joiner, other)
            assert first.key in joiner._served and second.key in joiner._served
            await body.aclose()
            with anyio.fail_after(5):
                while not second.file.complete:  # noqa: ASYNC110
                    await anyio.sleep(0.01)
            joiner._evict(second.file)
            assert first.key not in joiner._served and not first.file.path.exists()
            assert second.key in joiner._served  # the newest, whatever its size
        finally:
            dash_module.FRESH_SECONDS = fresh


@pytest.mark.anyio
async def test_a_file_dropped_while_its_fill_goes_on_is_planned_again(tmp_path: Path) -> None:
    """(Its planning is over: a request after the drop plans anew, at once.)"""
    server = Server(dash_audio("unit-dropped", 6))
    server.segment_delay = 0.5
    the_link = link(server)
    async with anyio.create_task_group() as group:
        joiner = Dash(tmp_path, FFMPEG, spawn=group.start_soon)
        stream = await served(joiner, the_link)
        assert stream.job is not None
        job = stream.job
        joiner._drop(stream)  # (as a newer link to another file drops it)
        assert job.scope.cancel_called  # its fill ends
        with anyio.fail_after(5):
            again = await served(joiner, the_link)
        assert again is not stream and again.file.etag == stream.file.etag
        group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_another_link_is_taken_only_when_the_segments_own_one_stops(
    tmp_path: Path,
) -> None:
    server = Server(dash_audio("unit-spare", 6))
    server.segment_delay = 0.2
    first = link(server)
    other = Link(first.key, MANIFEST + "&other=1", {}, first.http, None, "Made-up")
    async with anyio.create_task_group() as group:
        joiner = Dash(tmp_path, FFMPEG, spawn=group.start_soon)
        stream = await served(joiner, first)
        assert await served(joiner, other) is stream and stream.spare is other
        with anyio.fail_after(5):
            while not stream.file.complete:  # noqa: ASYNC110
                await anyio.sleep(0.01)
        group.cancel_scope.cancel()
    manifests = [r for r in server.seen if r.url.host == "manifests.example"]
    assert len(manifests) == 1  # the first link worked: the other's manifest never read
    assert stream.link is first


@pytest.mark.anyio
async def test_a_segment_over_the_size_limit_refuses_the_manifest_before_any_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dash_module, "MAX_SEGMENT_BYTES", 1000)
    server = Server(dash_audio("unit-oversize", 6))
    async with anyio.create_task_group() as group:
        joiner = Dash(tmp_path, FFMPEG, spawn=group.start_soon)
        with pytest.raises(DashError) as failed:
            await served(joiner, link(server))
    assert failed.value.kind == "unsupported"
    fetched = [r for r in server.seen if r.url.host == "cdn.example" and "range" not in r.headers]
    assert [r.url.path.rsplit("/", 1)[1] for r in fetched] == ["init-stream2.m4s"]
    assert not [p.name for p in tmp_path.iterdir()]  # noqa: ASYNC240 (no file made)


@pytest.mark.anyio
async def test_a_request_timed_out_on_a_file_being_fetched_is_told_it_goes_on(
    tmp_path: Path,
) -> None:
    server = Server(dash_audio("unit-outlasts", 6))
    server.segment_delay = 0.5
    async with anyio.create_task_group() as group:
        joiner = Dash(tmp_path, FFMPEG, spawn=group.start_soon)
        stream = await served(joiner, link(server))
        assert joiner.outlasts(stream.key) == "first"  # not complete: its fill goes on
        assert joiner.outlasts(stream.key) == "again"
        with anyio.fail_after(5):
            while not stream.file.complete:  # noqa: ASYNC110
                await anyio.sleep(0.01)
        assert joiner.outlasts(stream.key) == "kept"
        group.cancel_scope.cancel()
