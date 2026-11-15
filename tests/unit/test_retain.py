"""Streamed audio kept (``delivery/retain.py``): which answers fill a song's file, and
when it is whole."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
) -> tuple[Retention, list[str], list[bytes], list[str], list[Path]]:
    """``songs`` distinct songs streamed whole while putting one in place does not end, then
    does. Returns the songs put in place in the end, the bodies, the songs whose putting in
    place had begun while it did not end, and the files then."""
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
        before = list(calls)
        release.set()
        await anyio.sleep(0.5)
        tg.cancel_scope.cancel()
    return retention, calls, bodies, before, files


async def test_whole_songs_held_while_their_library_write_is_slow_are_bounded(
    tmp_path: Path,
) -> None:
    """An app fetching many songs ahead while the library writes slowly: at most ``HELD``
    whole files wait to be put in place at once (copies of the files held for the plays, at
    most ``ENTRIES``); the others held wait for a place and get one once the writes go on -
    the songs no longer held are not kept - and every app still gets its whole audio."""
    retention, calls, bodies, before, files = await blocked_handover(tmp_path, 3 * retain.HELD)
    assert bodies == [DATA] * (3 * retain.HELD)
    assert len(before) == retain.HELD and len(files) <= retain.ENTRIES + retain.HELD
    assert len(calls) == retain.ENTRIES == len(set(calls))  # every song still held, in the end
    assert retention.kept == retain.ENTRIES and retention._held == 0
    assert [p async for p in anyio.Path(tmp_path / "work").iterdir()] == []


async def test_whole_songs_held_are_bounded_by_their_size_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(The files held for the plays take their room too: fewer copies fit beside them.)"""
    monkeypatch.setattr(retain, "STAGED_BYTES", 3 * len(DATA))
    retention, calls, bodies, before, _ = await blocked_handover(tmp_path, 6)
    assert bodies == [DATA] * 6
    assert 1 <= len(before) <= 3 and len(calls) >= len(before)
    assert retention._held_bytes == 0


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


# --- an answer read on: the client is never cut off nor slowed by the disk ---------------


async def read_on_all(
    retention: Retention, data: bytes, *, slowly: float = 0.0, size: int = 1000
) -> bytes:
    """A whole answer read on (``read_on``), its client reading all of it."""
    capture = retention.capture(pin(), answer(0, len(data) - 1), len(data), '"v1"')
    assert capture is not None

    async def upstream() -> AsyncIterator[bytes]:
        async for chunk in chunks(data, size):
            if slowly:
                await anyio.sleep(slowly)
            yield chunk

    body = capture.tap(upstream())
    capture.accept(TRACK)

    async def close() -> None:
        pass

    following = capture.read_on(body, close, 0, len(data) - 1)
    assert following is not None
    client, closed = following
    got = b"".join([chunk async for chunk in client])
    await closed()
    return got


@pytest.mark.parametrize("fault", ["no space", "queue"])
async def test_a_file_that_cannot_be_written_never_cuts_its_client_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """The disk is full, or the bytes come faster than the queue's bound: the song is no
    longer held, and the client gets every byte all the same, from what was read."""
    if fault == "no space":

        def full(*args: Any) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Retention, "_write", staticmethod(full))
    else:
        monkeypatch.setattr(retain, "QUEUE_BYTES", 1500)
    got: list[bytes] = []

    async def work(retention: Retention) -> None:
        got.append(await read_on_all(retention, DATA, slowly=0.001 if fault == "no space" else 0))

    assert await with_retention(tmp_path, work) == []  # (not kept)
    assert got == [DATA]


async def test_a_slow_disk_never_slows_its_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each write takes 0.2 s: the client gets the song as fast as the add-on sends it
    (from memory), not as fast as the disk writes it."""
    write = Retention._write

    def slow(*args: Any) -> None:
        import time

        time.sleep(0.2)
        write(*args)

    monkeypatch.setattr(Retention, "_write", staticmethod(slow))
    monkeypatch.setattr(retain, "SLOW_BYTES", 3000)
    took: list[float] = []

    async def work(retention: Retention) -> None:
        started = anyio.current_time()
        assert await read_on_all(retention, DATA) == DATA
        took.append(anyio.current_time() - started)

    await with_retention(tmp_path, work)
    assert took[0] < 1.0  # (11 writes of 0.2 s each would take 2.2 s)


async def test_a_copy_for_the_library_that_failed_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy of a whole song held fails once (a full disk a moment): the song is put in
    place by the next try - a later play of it - not left out for good; nothing is tried
    again at once in a loop."""
    copy = retain.shutil.copyfile
    failures = [OSError(28, "No space left on device")]

    def flaky(source: Any, target: Any) -> Any:
        if failures:
            raise failures.pop()
        return copy(source, target)

    monkeypatch.setattr(retain.shutil, "copyfile", flaky)

    async def work(retention: Retention) -> None:
        await played(retention, 0, len(DATA) - 1, DATA)
        await anyio.sleep(0.2)
        assert failures == [] and retention.kept == 0  # (failed, and not tried again yet)
        await played(retention, 0, len(DATA) - 1, DATA)  # a later play of it

    assert await with_retention(tmp_path, work) == [DATA]


async def test_a_file_chosen_for_a_range_is_not_removed_before_its_answer_is_read(
    tmp_path: Path,
) -> None:
    """Chosen for a later range (leased), then dropped - its place needed - before the
    answer's body is read: the body still reads it in full; once closed, the file goes. A
    lease ends also when the body is never read."""
    files: list[Path] = []

    async def work(retention: Retention) -> None:
        retention.enabled = False
        await played(retention, 0, len(DATA) - 1, DATA)
        await anyio.sleep(0.1)  # (written)
        names = (TRACK.key,)
        found = retention.held(names, lambda r: True, 100, None)
        assert found is not None
        entry, end = found
        files.append(entry.path)
        body, close = retention.serve(entry, 100, end)
        retention._drop(entry, "test: its place is needed")
        assert entry.path.exists()
        assert b"".join([chunk async for chunk in body]) == DATA[100:]
        await close()
        assert not entry.path.exists()
        await played(retention, 0, len(DATA) - 1, DATA)  # (held again)
        await anyio.sleep(0.1)
        again = retention.held(names, lambda r: True, 0, None)
        assert again is not None
        _, never_read = retention.serve(again[0], 0, again[1])
        retention._drop(again[0], "test: its place is needed")
        assert again[0].path.exists()
        await never_read()  # (an answer that never went out)
        assert not again[0].path.exists()

    await with_retention(tmp_path, work)


async def test_a_library_write_that_keeps_failing_is_not_tried_again_in_a_loop(
    tmp_path: Path,
) -> None:
    """Putting a whole song in place fails each time: a place freed by the failure does not
    hand the same song over again at once; it waits (or a later play of it)."""
    calls: list[str] = []

    async def keep(track: Track, path: Path, remux: str | None) -> str | None:
        calls.append(track.key)
        return "the library write stopped"

    retention = Retention(keep, tmp_path / "work", enabled=True)
    async with anyio.create_task_group() as tg:
        retention.spawn = lambda job: tg.start_soon(job)
        tg.start_soon(retention.run)
        await anyio.lowlevel.checkpoint()
        await played(retention, 0, len(DATA) - 1, DATA)
        await anyio.sleep(0.3)
        [entry] = retention._entries.values()
        for _ in range(3):  # ranges served from it meanwhile: each frees a place
            retention.lease(entry)
            retention.release(entry)
            await anyio.sleep(0.1)
        tg.cancel_scope.cancel()
    assert calls == [TRACK.key]


@pytest.mark.parametrize("ends", ["failure", "cancel"])
async def test_a_file_chosen_but_not_served_is_not_leased_for_ever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ends: str
) -> None:
    """A later range of a play Shijhon does not know, its file told by its first bytes:
    reading them fails, or the request is canceled meanwhile - the file chosen is no longer
    leased (it can be evicted and removed as any other)."""
    from shijhon.delivery.playback import ByteRange, Deliverer, Known

    leftover: list[int] = []

    def reading(entry: Any, size: int) -> bytes | None:
        import time

        if ends == "failure":
            raise OSError(5, "Input/output error")
        time.sleep(0.3)
        return None

    async def work(retention: Retention) -> None:
        retention.enabled = False
        await played(retention, 0, len(DATA) - 1, DATA, etag=None)
        await anyio.sleep(0.1)
        monkeypatch.setattr(retention, "start_of", reading)
        deliverer = Deliverer(cast(Any, None))
        deliverer.retention = retention
        deliverer._known[TRACK.song_id] = Known(
            len(DATA), None, 1, "t1", link="https://cdn.test/another", head=DATA[:16]
        )
        wanted = ByteRange(100, None)
        if ends == "failure":
            with pytest.raises(OSError):
                await deliverer._from_held(TRACK, wanted)
        else:  # (the client left: canceled when the first bytes are to be read)
            with anyio.CancelScope() as scope:
                scope.cancel()
                await deliverer._from_held(TRACK, wanted)
        leftover.extend(e.active for e in retention._entries.values())

    await with_retention(tmp_path, work)
    assert leftover == [0]


async def test_files_dropped_while_read_still_count_in_the_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files dropped while answers still read them stay on disk until those end: they count
    in the bounds (songs and bytes) meanwhile, so dropping more cannot admit files past
    them; once their readers are done they go, and room is made."""
    monkeypatch.setattr(retain, "ENTRIES", 2)
    seen: dict[str, Any] = {}

    def capture(number: int) -> Any:
        track = Track(f"song-{number}", None, "Invented", "Invented", 3000, ref=f"demo:{number}")
        found = retention_of[0].capture(
            pin(f"https://cdn.test/{number}"), answer(0, len(DATA) - 1), len(DATA), f'"v{number}"'
        )
        assert found is not None
        found.accept(track)
        return found

    retention_of: list[Retention] = []

    async def work(retention: Retention) -> None:
        retention_of.append(retention)
        retention.enabled = False
        dropped = []
        for number in range(2):
            capture(number)
            [entry] = [e for e in retention._entries.values() if e.track.ref == f"demo:{number}"]
            retention.lease(entry)  # (an answer reads it)
            retention._drop(entry, "test: its place is needed")
            dropped.append(entry)
        seen["staged"] = retention._staged()
        seen["while read"] = capture(2).representation
        for entry in dropped:
            retention.release(entry)
        seen["gone"] = [entry.path.exists() for entry in dropped]
        seen["after"] = capture(3).representation

    await with_retention(tmp_path, work)
    assert seen["staged"] == 2 * len(DATA)
    assert seen["while read"] is None  # (not held: two files on disk already)
    assert seen["gone"] == [False, False]
    assert seen["after"] is not None
