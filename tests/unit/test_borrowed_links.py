"""A request whose borrowed link failed waits for the routing that found it: it wakes
as soon as that routing publishes its next link - also when the map of waiters overflows."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest

from shijhon.delivery import playback
from shijhon.delivery.playback import Deliverer, Trace, Track


def link(source_id: int) -> Any:
    """A pin as far as ``pinned`` and ``_after`` look at it."""
    return cast(
        Any,
        SimpleNamespace(
            source=SimpleNamespace(id=source_id),
            wrong=None,
            created=time.monotonic(),
            info=SimpleNamespace(expires_at=None),
        ),
    )


def track(song: str) -> Track:
    return Track(song, None, "Title", "Artist", 180_000)


@pytest.mark.anyio
async def test_a_waiter_takes_the_next_link_at_once_also_after_an_overflow() -> None:
    deliverer = Deliverer(cast(Any, None))
    owner = Trace(time.monotonic())
    found: list[Any] = []

    async def wait_for_a() -> None:
        found.append(await deliverer._after(owner, track("a"), time.monotonic() + 5, set(), None))

    next_link = link(2)
    started = time.monotonic()
    async with anyio.create_task_group() as group:
        group.start_soon(wait_for_a)
        await anyio.sleep(0.05)
        for n in range(4100):  # other songs' waiters come and go
            deliverer._published[f"other-{n}"] = anyio.Event()
        with anyio.move_on_after(0.05):  # another song's waiter: the map overflows
            await deliverer._after(owner, track("b"), time.monotonic() + 0.05, set(), None)
        await anyio.sleep(0.05)
        deliverer._remember("a", next_link)
    assert found == [next_link]
    assert time.monotonic() - started < 1.0  # not at its 5 s deadline


@pytest.mark.anyio
async def test_a_request_that_took_a_link_over_at_a_commit_gets_that_routings_next_link() -> None:
    """Two requests around a commit: the catalog play's routing
    found a link; the song was committed and a request by its native ID took that link
    over; the link fails. The routing drops it and publishes its next one under the
    catalog track: under the native ID the failed link is gone too, and the request
    waiting there gets the next one at once. One routing at a time under both names."""
    deliverer = Deliverer(cast(Any, None))
    ref, native = "demo:1", "native-1"
    owner = Trace(time.monotonic())
    first, second = link(1), link(2)
    deliverer._remember(ref, first)  # the catalog play's routing found it
    deliverer.adopt(ref, native)  # the commit; the request by the native ID
    assert deliverer.pinned(native) is first
    found: list[Any] = []

    async def waits() -> None:  # ... whose link failed: it waits for that routing's next
        found.append(
            await deliverer._after(owner, track(native), time.monotonic() + 5, set(), first)
        )

    started = time.monotonic()
    async with anyio.create_task_group() as group:
        group.start_soon(waits)
        await anyio.sleep(0.05)
        deliverer.forget(ref, first)  # the routing found it failed too
        assert deliverer.pinned(native) is None and not found
        await anyio.sleep(0.05)
        deliverer._remember(ref, second)  # its next link
    assert found == [second] and time.monotonic() - started < 1.0
    assert deliverer.pinned(native) is second and deliverer.pinned(ref) is second
    # A link found under the native ID is the catalog track's too (requests still using it).
    third = link(3)
    deliverer._remember(native, third)
    assert deliverer.pinned(ref) is third
    deliverer.forget(native, third)
    assert deliverer.pinned(ref) is None
    routed: list[str] = []

    async def routes() -> None:  # (a routing under the native ID)
        async with deliverer._routing_lock(native):
            routed.append(native)

    async with anyio.create_task_group() as group, deliverer._routing_lock(ref):
        group.start_soon(routes)
        await anyio.sleep(0.05)
        assert not routed  # it waits for the one under the catalog track
    assert routed == [native]
    # A routing that ended without audio is the song's answer under both names.
    assert deliverer._names(native) == (native, ref) and deliverer._names(ref) == (ref, native)
    since = time.monotonic()
    deliverer._routing_failed(track(ref), None, "play", playback.NoSource(["nothing had it"]))
    for name in (ref, native):
        with pytest.raises(playback._Shared):
            deliverer._shared_failure(name, since)
    # A song nobody uses under its catalog track has one name.
    deliverer.adopt("demo:2", "native-2")
    assert deliverer._names("native-2") == ("native-2",)
    deliverer._remember("native-2", link(4))
    assert deliverer.pinned("demo:2") is None


@pytest.mark.anyio
async def test_a_request_by_the_native_id_stays_under_it_once_the_names_are_one() -> None:
    """While a routing of a song committed mid-play goes on - under either name: they share
    the lock - another request by the native ID is not turned into one under the catalog
    track (what is known of the play it continues is kept under the native ID); a song
    with one name still joins the catalog play's routing, as before."""
    deliverer = Deliverer(cast(Any, None))
    seen: list[str] = []

    async def opened(asked: Track, *args: Any, **kwargs: Any) -> Any:
        seen.append(asked.song_id)
        return object()

    deliverer._open = opened  # type: ignore[method-assign]
    ref, native = "demo:1", "native-1"
    request = Track(native, None, "Title", "Artist", 180_000, ref=ref)
    async with deliverer._routing_lock(ref):  # the catalog play's routing, going on
        await deliverer.open(request, None, head=True)
    assert seen == [ref]  # one name so far: it joins that routing
    first = link(1)
    deliverer._remember(ref, first)
    deliverer.adopt(ref, native)  # one song under both names now
    deliverer.forget(native, first)
    async with deliverer._routing_lock(native):  # (the same lock)
        await deliverer.open(request, None, head=True)
    assert seen == [ref, native]


def test_names_in_use_are_never_parted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the limit the oldest pairs of names go - never one with a link (or a routing,
    or a waiter): apart, its two names would have a lock and a link each."""
    monkeypatch.setattr(playback, "MAX_TWINS", 1)
    deliverer = Deliverer(cast(Any, None))
    for n in (1, 2, 3):
        deliverer._remember(f"demo:{n}", link(n))
        deliverer.adopt(f"demo:{n}", f"native-{n}")
    assert len(deliverer._provisional) == 3  # all in use: each has its link
    deliverer.forget("native-1")
    deliverer.forget("native-2")
    deliverer._remember("demo:4", link(4))
    deliverer.adopt("demo:4", "native-4")
    assert set(deliverer._provisional) == {"native-3", "native-4"}
    assert deliverer._names("demo:3") == ("demo:3", "native-3")
    assert deliverer._names("demo:1") == ("demo:1",)
