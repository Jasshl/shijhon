"""Streamed audio kept (``delivery/retain.py``): which answers fill a song's file, and
when it is whole."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.lowlevel
import httpx
import pytest

from shijhon.delivery import retain
from shijhon.delivery.dash import Joined
from shijhon.delivery.playback import Track
from shijhon.delivery.retain import Retention

pytestmark = pytest.mark.anyio

DATA = bytes(range(256)) * 40  # 10,240 bytes
TRACK = Track("song-1", None, "Invented Song", "Invented Artist", 3000, ref="demo:1")


def pin(url: str = "https://cdn.test/a", etag: str | None = None, size: int | None = None) -> Any:
    return SimpleNamespace(
        source=SimpleNamespace(id=1), track_id="t1", info=SimpleNamespace(url=url), etag=etag,
        size=size,
    )  # fmt: skip


def answer(first: int, last: int, total: int | None = len(DATA)) -> httpx.Response:
    if first == 0 and total is not None and last == total - 1:
        return httpx.Response(200, headers={"content-length": str(total)})
    size = "*" if total is None else str(total)
    return httpx.Response(206, headers={"content-range": f"bytes {first}-{last}/{size}"})


async def chunks(data: bytes, size: int = 1000) -> AsyncIterator[bytes]:
    for at in range(0, len(data), size):
        yield data[at : at + size]


async def read(body: AsyncIterator[bytes], until: int | None = None) -> None:
    got = 0
    async for chunk in body:
        got += len(chunk)
        if until is not None and got >= until:
            break


async def with_retention(
    tmp_path: Path, work: Callable[[Retention], Awaitable[None]], *, max_bytes: int = 2**30
) -> list[bytes]:
    """The files put in place while ``work`` runs (and a moment after)."""
    kept: list[bytes] = []

    async def keep(track: Track, path: Path, remux: str | None) -> str | None:
        kept.append(await anyio.Path(path).read_bytes())
        return None

    retention = Retention(keep, tmp_path / "work", enabled=True, max_bytes=max_bytes)
    async with anyio.create_task_group() as tg:
        retention.spawn = lambda job: tg.start_soon(job)
        tg.start_soon(retention.run)
        await anyio.lowlevel.checkpoint()
        await work(retention)
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    return kept


async def played(
    retention: Retention,
    first: int,
    last: int,
    data: bytes,
    *,
    total: int | None = len(DATA),
    etag: str | None = '"v1"',
    until: int | None = None,
    accept: bool = True,
) -> None:
    capture = retention.capture(pin(), answer(first, last, total), total, etag)
    assert capture is not None
    body = capture.tap(chunks(data))
    if accept:
        capture.accept(TRACK)
    await read(body, until)
    await body.aclose()


async def test_a_play_s_answers_fill_one_file_together(tmp_path: Path) -> None:
    async def work(retention: Retention) -> None:
        await played(retention, 0, 1, DATA[:2])
        await played(retention, 0, 4999, DATA[:5000])
        await played(retention, 4000, len(DATA) - 1, DATA[4000:])

    assert await with_retention(tmp_path, work) == [DATA]


async def test_answers_whose_bytes_differ_are_not_kept(tmp_path: Path) -> None:
    other = bytes(255 - b for b in DATA)

    async def work(retention: Retention) -> None:
        await played(retention, 0, 4999, DATA[:5000])
        await played(retention, 4000, len(DATA) - 1, other[4000:])

    assert await with_retention(tmp_path, work) == []
    assert list((tmp_path / "work").iterdir()) == []


async def test_answers_of_other_files_are_never_joined(tmp_path: Path) -> None:
    """Another ETag is another file, whatever its size."""

    async def work(retention: Retention) -> None:
        await played(retention, 0, 4999, DATA[:5000], etag='"v1"')
        await played(retention, 5000, len(DATA) - 1, DATA[5000:], etag='"v2"')

    assert await with_retention(tmp_path, work) == []


async def test_an_answer_of_a_size_not_told_is_kept_only_when_read_to_its_end(
    tmp_path: Path,
) -> None:
    async def chunked(retention: Retention, until: int | None, max_bytes: int = 2**30) -> None:
        framed = httpx.Response(200, headers={"transfer-encoding": "chunked"})
        capture = retention.capture(pin(), framed, None, None)
        assert capture is not None
        body = capture.tap(chunks(DATA))
        capture.accept(TRACK)
        await read(body, until)

    assert await with_retention(tmp_path, lambda r: chunked(r, None)) == [DATA]
    assert await with_retention(tmp_path, lambda r: chunked(r, 3000)) == []
    # (and at most a song's file)
    assert await with_retention(tmp_path, lambda r: chunked(r, None), max_bytes=3000) == []


async def test_an_answer_whose_end_is_not_told_is_never_taken_for_the_whole_file(
    tmp_path: Path,
) -> None:
    """A part of an untold size, or a plain answer that ends with its connection (as a
    broken connection does), could be a song cut short."""

    async def keep(track: Track, path: Path, remux: str | None) -> str | None:
        return None

    retention = Retention(keep, tmp_path, enabled=True)
    part = httpx.Response(206, headers={"content-range": "bytes 0-4999/*"})
    assert retention.capture(pin(), part, None, None) is None
    assert retention.capture(pin(), httpx.Response(200), None, None) is None


async def test_an_answer_without_its_link_s_etag_is_of_the_same_file(tmp_path: Path) -> None:
    """The link's first answer gave its ETag, a later one of the same size none."""

    async def work(retention: Retention) -> None:
        first = retention.capture(pin(), answer(0, 4999), len(DATA), '"v1"')
        later = retention.capture(
            pin(etag='"v1"', size=len(DATA)), answer(5000, len(DATA) - 1), len(DATA), None
        )
        assert first is not None and later is not None
        for capture, data in ((first, DATA[:5000]), (later, DATA[5000:])):
            body = capture.tap(chunks(data))
            capture.accept(TRACK)
            await read(body)

    assert await with_retention(tmp_path, work) == [DATA]


async def test_without_its_writer_the_audio_still_goes_to_the_client(tmp_path: Path) -> None:
    """Shijhon stopping (or its writer failed): nothing is kept, playback goes on."""

    async def keep(track: Track, path: Path, remux: str | None) -> str | None:
        return None

    retention = Retention(keep, tmp_path, enabled=True)
    async with anyio.create_task_group() as tg:  # (its writer ran, and ended)
        tg.start_soon(retention.run)
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()
    capture = retention.capture(pin(), answer(0, len(DATA) - 1), len(DATA), '"v1"')
    assert capture is not None
    body = capture.tap(chunks(DATA))
    capture.accept(TRACK)
    assert b"".join([chunk async for chunk in body]) == DATA
    assert [p async for p in anyio.Path(tmp_path).iterdir() if p.is_file()] == []


async def test_an_answer_that_never_went_to_a_client_is_not_kept(tmp_path: Path) -> None:
    async def work(retention: Retention) -> None:
        await played(retention, 0, len(DATA) - 1, DATA, accept=False)

    assert await with_retention(tmp_path, work) == []
    assert list((tmp_path / "work").iterdir()) == []


async def test_more_bytes_than_the_file_are_not_kept(tmp_path: Path) -> None:
    async def work(retention: Retention) -> None:
        await played(retention, 0, len(DATA) - 1, DATA + b"more")

    assert await with_retention(tmp_path, work) == []


async def test_nothing_is_kept_while_off(tmp_path: Path) -> None:
    async def work(retention: Retention) -> None:
        retention.enabled = False
        await played(retention, 0, len(DATA) - 1, DATA)

    assert await with_retention(tmp_path, work) == []


async def blocked_handover(
    tmp_path: Path, songs: int
) -> tuple[Retention, list[str], list[bytes], anyio.Event, list[Path]]:
    """``songs`` distinct songs streamed whole while putting one in place does not end."""
    calls: list[str] = []
    bodies: list[bytes] = []
    files: list[Path] = []
    release = anyio.Event()

    async def keep(track: Track, path: Path, remux: str | None) -> str | None:
        calls.append(track.key)
        await release.wait()
        return None

    retention = Retention(keep, tmp_path / "work", enabled=True)
    async with anyio.create_task_group() as tg:
        retention.spawn = lambda job: tg.start_soon(job)
        tg.start_soon(retention.run)
        await anyio.lowlevel.checkpoint()
        for number in range(songs):
            track = Track(
                f"song-{number}", None, "Invented", "Invented", 3000, ref=f"demo:{number}"
            )
            capture = retention.capture(pin(f"https://cdn.test/{number}"), answer(0, len(DATA) - 1),
                                        len(DATA), f'"v{number}"')  # fmt: skip
            assert capture is not None
            body = capture.tap(chunks(DATA))
            capture.accept(track)
            bodies.append(b"".join([chunk async for chunk in body]))
        await anyio.sleep(0.3)
        files.extend([p async for p in anyio.Path(tmp_path / "work").iterdir()])  # type: ignore[misc]
        release.set()
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    return retention, calls, bodies, release, files


async def test_whole_songs_held_while_their_library_write_is_slow_are_bounded(
    tmp_path: Path,
) -> None:
    """An app fetching many songs ahead while the library writes slowly: at most ``HELD``
    whole files wait to be put in place; the others are not kept, and every app still
    gets its whole audio."""
    retention, calls, bodies, _, files = await blocked_handover(tmp_path, 3 * retain.HELD)
    assert bodies == [DATA] * (3 * retain.HELD)
    assert len(calls) == retain.HELD and len(files) <= retain.HELD
    assert retention.kept == retain.HELD and retention._held == 0
    assert [p async for p in anyio.Path(tmp_path / "work").iterdir()] == []


async def test_whole_songs_held_are_bounded_by_their_size_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retain, "STAGED_BYTES", 3 * len(DATA))
    retention, calls, bodies, _, _ = await blocked_handover(tmp_path, 6)
    assert bodies == [DATA] * 6
    assert len(calls) == 3 and retention._held_bytes == 0


async def test_songs_being_filled_leave_no_room_for_more_whole_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct streams still being filled take their told sizes: a DASH file played then is
    not copied past the shared room (``STAGED_BYTES``)."""
    monkeypatch.setattr(retain, "STAGED_BYTES", 3 * len(DATA))
    leased: list[object] = []
    fake_dash: Any = SimpleNamespace(lease=lambda kept: leased.append(kept) or (lambda: None))
    joined = Joined(tmp_path / "joined.flac", len(DATA), '"j"', "audio/flac", 3.0)

    async def work(retention: Retention) -> None:
        bodies = []
        for number in range(3):
            track = Track(
                f"song-{number}", None, "Invented", "Invented", 3000, ref=f"demo:{number}"
            )
            capture = retention.capture(pin(f"https://cdn.test/{number}"), answer(0, len(DATA) - 1),
                                        len(DATA), f'"v{number}"')  # fmt: skip
            assert capture is not None
            body = capture.tap(chunks(DATA))
            capture.accept(track)
            await body.__anext__()  # (being filled: one chunk of it)
            bodies.append(body)
        retention._dash_played(TRACK, joined, fake_dash)
        for body in bodies:
            await body.aclose()

    assert await with_retention(tmp_path, work) == []
    assert leased == []


async def test_an_untold_size_takes_its_share_of_the_room_as_it_comes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retain, "STAGED_BYTES", 2 * len(DATA))
    data = DATA * 3

    async def work(retention: Retention) -> None:
        framed = httpx.Response(200, headers={"transfer-encoding": "chunked"})
        capture = retention.capture(pin(), framed, None, None)
        assert capture is not None
        body = capture.tap(chunks(data))
        capture.accept(TRACK)
        got = b""
        async for chunk in body:
            got += chunk
            if len(got) > 2 * len(DATA) + 1000:  # past the room: no longer being filled
                assert retention._entries == {}
        assert got == data  # (the app gets it all)

    assert await with_retention(tmp_path, work) == []
