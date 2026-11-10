"""Add-on delivery: an add-on whose manifest declares no
``stream`` is asked for no audio; an HTTP 503 with a valid ``Retry-After`` at
``/stream/{id}`` is "not ready yet" - asked again within the add-on's budget, and no error;
and what Shijhon sends is counted per add-on (its address and manifest ``id``), not per
address."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from shijhon.config import DeliverySettings
from shijhon.delivery.addon import Addon, AddonError
from shijhon.delivery.bulk import BulkAcquisition, effective_capacity
from shijhon.delivery.pacing import Limits, Paces
from shijhon.delivery.playback import Deliverer, NoSource, PlaybackSettings, Track
from shijhon.delivery.sources import Source, SourceRegistry
from shijhon.store import Store
from tests.harness.library import tone

AUDIO = tone(440, 3, "flac").read_bytes()


class Body(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield AUDIO[:4096]
        yield AUDIO[4096:]


@dataclass
class Server:
    """An add-on behind a mock transport. ``preparing``: the first link requests answer
    503 with ``retry_after`` (None: no such header); ``stream_status``: every link request
    answers this status."""

    id: str = "demo.one"
    resources: tuple[str, ...] = ("isrc", "stream")
    preparing: int = 0
    retry_after: str | None = "0.2"
    stream_status: int | None = None
    calls: list[httpx.Request] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path.rsplit("/", 2)
        name = path[-1] if path[-2] != "stream" else "stream"
        if name == "manifest.json":
            body = {"id": self.id, "name": self.id, "resources": list(self.resources)}
            return httpx.Response(200, json=body)
        if name == "resolve-isrc":
            return httpx.Response(200, json={"trackId": request.url.params["isrc"]})
        if name == "availability":
            return httpx.Response(200, json={"available": True})
        if name == "stream":
            if self.preparing:
                self.preparing -= 1
                headers = {} if self.retry_after is None else {"retry-after": self.retry_after}
                return httpx.Response(503, json={"error": "preparing"}, headers=headers)
            if self.stream_status is not None:
                return httpx.Response(self.stream_status, json={"error": "unavailable"})
            audio = request.url.copy_with(path=request.url.path.rsplit("/stream/", 1)[0] + "/a")
            return httpx.Response(200, json={"url": str(audio.copy_with(query=None))})
        if name == "a":
            headers = {"content-length": str(len(AUDIO)), "content-type": "audio/flac"}
            return httpx.Response(200, headers=headers, stream=Body())
        return httpx.Response(404)

    def asked(self, endpoint: str) -> int:
        return sum(1 for c in self.calls if f"/{endpoint}" in c.url.path)


@asynccontextmanager
async def world(
    tmp_path: Path,
    servers: list[Server],
    bases: list[str] | None = None,
    *,
    together: bool = False,
    **settings: Any,
) -> AsyncIterator[tuple[Deliverer, list[Source]]]:
    """``together``: the add-ons are one step of the plan (else each a step of its own)."""
    store = await Store.open(tmp_path / "state.sqlite3")
    registry = SourceRegistry(store, limits=Limits(0, 4, 0))
    bases = bases or [f"https://demo{i}.example.invalid" for i in range(len(servers))]
    for index, base in enumerate(bases):
        await registry.add(str(index), base, step=1 if together else None)
    sources = await registry.enabled()
    for source, server in zip(sources, servers, strict=True):
        await source.http.aclose()
        source.http = source.addon.http = httpx.AsyncClient(
            transport=httpx.MockTransport(server.handle)
        )
    options: dict[str, Any] = {"budget_seconds": 2, "cooldown_seconds": 30, **settings}
    deliverer = Deliverer(registry, PlaybackSettings(**options))
    try:
        yield deliverer, sources
    finally:
        await registry.aclose()
        await store.close()


def track(number: int) -> Track:
    return Track(str(number), f"DMXXX2600{number:03}", f"Song {number}", "Demo", 3000)


async def play(deliverer: Deliverer, number: int) -> None:
    with anyio.fail_after(10):
        answer = await deliverer.open(track(number), None)
    await answer.close()


# --- add-ons that do not stream ---------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("plan", ["steps", "together", "checks"])
async def test_an_add_on_that_does_not_stream_is_asked_for_no_audio(
    tmp_path: Path, plan: str
) -> None:
    catalog = Server("demo.catalog", ("isrc", "search", "catalog", "availability"))
    audio = Server("demo.audio", ("isrc", "stream", "availability"))
    async with world(
        tmp_path, [catalog, audio], together=plan == "together", ready_checks=plan == "checks"
    ) as (d, sources):
        await play(d, 1)  # its manifest not known yet: read, and nothing more asked
        assert catalog.asked("resolve-isrc") == catalog.asked("stream") == 0
        assert catalog.asked("manifest.json") == 1 and audio.asked("stream") == 1
        assert not sources[0].streams and sources[1].streams
        assert [s.name for s in await d.registry.streaming()] == ["1"]
        for number in (2, 3):
            await play(d, number)
        # Never looked up, nor checked, nor asked for a link.
        assert catalog.asked("resolve-isrc") == catalog.asked("availability") == 0
        assert catalog.asked("stream") == 0 and audio.asked("stream") == 3
        assert sources[0].stats.failures == 0
        # A fetch ahead and warm-ahead too.
        with anyio.fail_after(10):
            await (await d.open(track(4), None, purpose="ahead")).close()
            await (await d.open(track(5), None, purpose="warm")).close()
        assert catalog.asked("resolve-isrc") == catalog.asked("stream") == 0
        # ... nor through the client's own lookup, whoever asks.
        assert await sources[0].addon.find(track(6).wanted) is None
        assert catalog.asked("resolve-isrc") == 0


@pytest.mark.anyio
async def test_downloads_and_preparations_leave_an_add_on_that_does_not_stream_out(
    tmp_path: Path,
) -> None:
    catalog = Server("demo.catalog", ("isrc", "search", "catalog"))
    audio = Server("demo.audio")
    async with world(tmp_path, [catalog, audio]) as (d, _):
        bulk = BulkAcquisition(d)
        try:
            with anyio.fail_after(10):
                answer = await bulk.open(track(1), "ann", background=True)
            await answer.close()
            with anyio.fail_after(10):
                await (await bulk.open(track(2), "ann")).close()
        finally:
            await bulk.aclose()
        assert catalog.asked("resolve-isrc") == catalog.asked("stream") == 0
        assert audio.asked("stream") == 2


@pytest.mark.anyio
async def test_an_add_on_whose_manifest_cannot_be_read_is_tried_as_before(tmp_path: Path) -> None:
    async def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    audio = Server("demo.audio")
    async with world(tmp_path, [Server(), audio]) as (d, sources):
        await sources[0].http.aclose()
        sources[0].http = sources[0].addon.http = httpx.AsyncClient(
            transport=httpx.MockTransport(broken)
        )
        await play(d, 1)
        assert sources[0].streams  # (not known: it may)
        assert sources[0].stats.failures == 1 and audio.asked("stream") == 1


# --- "not ready yet": 503 with Retry-After --------------------------------------------------


@pytest.mark.anyio
async def test_a_preparing_add_on_is_asked_again_within_its_budget_and_counts_no_error(
    tmp_path: Path,
) -> None:
    preparing = Server(preparing=2, retry_after="0.3")
    two = Server("demo.two")
    async with world(tmp_path, [preparing, two], budget_seconds=4, cooldown_errors=1) as (d, s):
        began = anyio.current_time()
        await play(d, 1)
        # Asked again twice, after what it named (half a second at least): within its share.
        assert anyio.current_time() - began >= 1.0
        assert preparing.asked("stream") == 3 and s[0].stats.successes == 1
        assert two.asked("stream") == 0
        assert s[0].stats.failures == s[0].stats.errors_since_success == 0
        assert not d.registry.cooling(s[0].id)


@pytest.mark.anyio
async def test_a_preparing_add_on_not_ready_in_time_is_no_error_and_is_asked_again(
    tmp_path: Path,
) -> None:
    # It names more than its budget leaves: not asked again in this play, nor waited for.
    preparing = Server(preparing=1, retry_after="5")
    two = Server("demo.two", stream_status=404)
    async with world(tmp_path, [preparing, two], cooldown_errors=1) as (d, sources):
        source = sources[0]
        with anyio.fail_after(1.5), pytest.raises(NoSource, match="not ready within its budget"):
            await d.open(track(1), None)
        assert preparing.asked("stream") == 1
        assert source.stats.errors_since_success == 0 and not d.registry.cooling(source.id)
        # Its time without audio is measured: a never-ready add-on drifts down the order.
        assert [a.delivered for a in source.stats.recent] == [False]
        # A client's retry asks it again (no "failed for this song" skip): ready now.
        await play(d, 1)
        assert preparing.asked("stream") == 2 and source.stats.successes == 1


@pytest.mark.anyio
async def test_a_preparing_add_on_that_never_gets_ready_uses_the_wait_and_no_more(
    tmp_path: Path,
) -> None:
    """In a play an add-on keeps trying until the client's wait, not its step's time."""
    preparing = Server(preparing=1000, retry_after="0.2")
    async with world(tmp_path, [preparing], budget_seconds=0.5, max_wait_seconds=2.0) as (
        d,
        sources,
    ):
        source = sources[0]
        began = anyio.current_time()
        with anyio.fail_after(4), pytest.raises(NoSource, match="asked again 3 times"):
            await d.open(track(1), None)
        # Asked at 0, 0.5, 1 and 1.5 s; then half a second is all that is left: not again.
        took = anyio.current_time() - began
        assert 1.4 <= took < 2.0 and preparing.asked("stream") == 4
        assert source.stats.errors_since_success == 0 and not d.registry.cooling(source.id)
        assert source.stats.recent[-1].seconds >= 1.4  # (the time it had, without audio)


@pytest.mark.anyio
async def test_its_own_budget_and_the_clients_wait_cap_bound_the_asking_again(
    tmp_path: Path,
) -> None:
    preparing = Server(preparing=1000, retry_after="0.2")
    async with world(tmp_path, [preparing], budget_seconds=0.5, max_wait_seconds=1.2) as (d, s):
        await d.registry.update(s[0].id, budget_seconds=5.0)  # a slow worker's own budget ...
        source = next(iter(await d.registry.enabled()))
        await source.http.aclose()
        source.http = source.addon.http = httpx.AsyncClient(
            transport=httpx.MockTransport(preparing.handle)
        )
        began = anyio.current_time()
        with anyio.fail_after(4), pytest.raises(NoSource):
            await d.open(track(1), None)
        assert anyio.current_time() - began < 1.6  # ... capped by the client's wait


@pytest.mark.anyio
@pytest.mark.parametrize("plan", ["steps", "together", "checks"])
async def test_a_continued_routing_asks_a_preparing_first_add_on_again(
    tmp_path: Path, plan: str
) -> None:
    """The first add-on said it was preparing - and moved on at once, not cut short by the
    wait cap - and the other lacked the song: a routing that continues it asks the first
    again (whether they were one step of the plan or two, with ready checks or without)."""
    preparing = Server(preparing=1, retry_after="5")
    lacking = Server("demo.two", stream_status=404)
    async with world(
        tmp_path,
        [preparing, lacking],
        together=plan == "together",
        ready_checks=plan == "checks",
    ) as (d, _):
        with anyio.fail_after(2), pytest.raises(NoSource, match="not ready"):
            await d.open(track(1), None)
        assert preparing.asked("stream") == 1
        lacked = lacking.asked("stream")
        with anyio.fail_after(5):
            answer = await d.open(track(1), None, resume=True)
        await answer.close()
        assert preparing.asked("stream") == 2 and lacking.asked("stream") == lacked


@pytest.mark.anyio
async def test_a_continued_routing_asks_a_preparing_add_on_again(tmp_path: Path) -> None:
    """An add-on not ready in time is not one the routing is done with - a
    routing that continues it (a download-first fetch's, say) asks it again."""
    preparing = Server(preparing=1, retry_after="5")
    async with world(tmp_path, [preparing]) as (d, _):
        with anyio.fail_after(2), pytest.raises(NoSource, match="not ready"):
            await d.open(track(1), None)
        with anyio.fail_after(5):
            answer = await d.open(track(1), None, resume=True)
        await answer.close()
        assert preparing.asked("stream") == 2


@pytest.mark.anyio
async def test_an_add_on_that_moves_on_at_once_is_not_timed_out_and_one_that_waits_is(
    tmp_path: Path,
) -> None:
    """An add-on that is not ready in time counts as a timeout only when the attempt
    waited until its time ran out. A Retry-After longer than what is left (the client's
    wait) moves on at once: nothing toward its timeouts or cooldown - and the time it took
    is still recorded. One asked again, still trying when the next step's add-on sends the
    song past its step's time, counts as slow."""
    primary = Server(preparing=1000, retry_after="40")
    async with world(tmp_path, [primary, Server("demo.two")], cooldown_timeouts=1) as (
        d,
        sources,
    ):
        source = sources[0]
        for number in (1, 2):
            began = anyio.current_time()
            await play(d, number)  # from the other at once
            assert anyio.current_time() - began < 1.0
        assert source.stats.timeouts_since_delivery == 0 and not d.registry.cooling(source.id)
        assert source.stats.failures == 0 and len(source.stats.recent) == 2
        primary.retry_after = "0.2"  # asked again until its time runs out: its timeout
        await play(d, 3)
        assert source.stats.timeouts_since_delivery == 1 and d.registry.cooling(source.id)


@pytest.mark.anyio
async def test_a_bulk_attempt_keeps_its_deadline_after_waiting_for_the_songs_routing(
    tmp_path: Path,
) -> None:
    """A download's link request after a wait for the song's routing gets what
    is left of its time, not a whole budget again."""
    preparing = Server(preparing=1000, retry_after="0.2")
    async with world(tmp_path, [preparing], budget_seconds=1.2) as (d, _):
        bulk = BulkAcquisition(d)
        try:
            await d.registry.streaming()
            held = anyio.Event()

            async def hold() -> None:
                async with d._routing_lock("1"):
                    held.set()
                    await anyio.sleep(0.8)

            async with anyio.create_task_group() as group:
                group.start_soon(hold)
                await held.wait()
                began = anyio.current_time()
                with anyio.fail_after(4), pytest.raises(NoSource):
                    await bulk.open(track(1), "ann")
                assert anyio.current_time() - began < 1.5  # 1.2 s in all, not 0.8 + 1.2
        finally:
            await bulk.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("retry_after", [None, "soon"])
async def test_a_503_without_a_valid_retry_after_stays_an_error(
    tmp_path: Path, retry_after: str | None
) -> None:
    failing = Server(preparing=1000, retry_after=retry_after)
    async with world(tmp_path, [failing, Server("demo.two")], cooldown_errors=1) as (d, s):
        await play(d, 1)  # from the second
        assert failing.asked("stream") == 1  # not asked again
        assert s[0].stats.errors_since_success == 1 and d.registry.cooling(s[0].id)


@pytest.mark.anyio
async def test_the_client_tells_a_503_with_retry_after_from_one_without() -> None:
    answers = [
        httpx.Response(503, headers={"retry-after": "3"}, json={"error": "anything"}),
        httpx.Response(503, json={"error": "preparing"}),
        httpx.Response(502, headers={"retry-after": "3"}),
    ]

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest.json"):
            return httpx.Response(200, json={"id": "demo", "resources": ["stream"]})
        return answers.pop(0)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    addon = Addon("https://addon.example.invalid", None, http)
    with pytest.raises(AddonError) as preparing:
        await addon.stream("t1")
    assert preparing.value.kind == "preparing" and preparing.value.retry_after == 3.0
    for _ in range(2):
        with pytest.raises(AddonError) as failed:
            await addon.stream("t1")
        assert failed.value.kind == "failed"
    await http.aclose()


# --- limits per add-on ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_add_ons_at_one_address_count_apart_and_one_added_twice_shares(
    tmp_path: Path,
) -> None:
    bridge = "http://127.0.0.1:4759"
    one, two, again = Server("demo.one"), Server("demo.two"), Server("demo.one")
    bases = [f"{bridge}/one", f"{bridge}/two", f"{bridge}/one?quality=high"]
    async with world(tmp_path, [one, two, again], bases) as (d, sources):
        for source in sources:
            assert source.pace is d.registry.paces.of(bridge)  # its address's, unread
            await source.addon.manifest()
        first, second, twice = (s.pace for s in sources)
        assert first is not None and second is not None
        assert first is twice and first is not second
        assert first is not d.registry.paces.of(bridge)
        # The manifest reads took their turns at the address, and count at each add-on too.
        assert (first.sent, second.sent) == (2, 1)
        # Listed again (a changed configuration): known by its manifest's id from the start.
        d.registry.limit(Limits(0, 4, 0))
        await d.registry.invalidate()
        assert [s.pace for s in await d.registry.enabled()] == [first, second, first]
        # A 429 from one add-on leaves the others at its address alone.
        first.limited(30.0)
        assert d.registry.cooling(sources[0].id) and d.registry.cooling(sources[2].id)
        assert not d.registry.cooling(sources[1].id)
        # Downloads count apart too, and one added twice shares its cap.
        stored = await d.registry.stored()
        assert [s.identity for s in stored] == ["demo.one", "demo.two", "demo.one"]
        assert effective_capacity(stored, 2, 1) == effective_capacity(sources, 2, 1)
        await d.registry.update(sources[0].id, downloads=1)
        stored = await d.registry.stored()
        caps = effective_capacity(stored, 2, 1)
        assert caps[sources[0].id] == caps[sources[2].id] == (1, 1)
        assert caps[sources[1].id] == (2, 1)


@pytest.mark.anyio
async def test_the_dashboards_manifest_requests_count_at_the_add_on_they_are_for(
    tmp_path: Path,
) -> None:
    """A manifest check of a configured add-on (its manifest's address, or a hop
    of its redirect at the same origin) counts at that add-on - its pause holds it, and its
    429 pauses that add-on; another origin's hop counts at that address."""
    bridge = "http://127.0.0.1:4759"
    bases = [f"{bridge}/one", f"{bridge}/two"]
    async with world(tmp_path, [Server("demo.one"), Server("demo.two")], bases) as (d, s):
        for source in s:
            await source.addon.manifest()
        one = await d.registry.pace_at(f"{bridge}/one/manifest.json", bases[0])
        assert one is s[0].pace and one is not s[1].pace
        assert await d.registry.pace_at(f"{bridge}/moved/manifest.json", bases[0]) is one
        elsewhere = await d.registry.pace_at("https://cdn.example.invalid/m.json", bases[0])
        assert elsewhere is d.registry.paces.of("https://cdn.example.invalid")
        # A new address (nothing configured there): the address's own count.
        assert await d.registry.pace_at(f"{bridge}/new/manifest.json", f"{bridge}/new") is (
            d.registry.paces.of(bridge)
        )
        assert one is not None
        one.limited(30.0)
        assert d.registry.cooling(s[0].id) and not d.registry.cooling(s[1].id)


def test_the_new_defaults_are_5_a_second_after_10_at_once() -> None:
    delivery = DeliverySettings()
    assert (delivery.addon_requests_per_second, delivery.addon_request_burst) == (5.0, 10)
    assert delivery.addon_audio_openings == 4
    pace = Paces().of("https://addon.example.invalid")
    assert pace is not None and pace.limits == Limits(5.0, 10, 4)


@pytest.mark.anyio
@pytest.mark.parametrize("shared", [False, True])
async def test_a_lookup_timeout_is_a_timeout_unless_it_was_another_request_s(
    tmp_path: Path, shared: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup that took too long is the add-on's timeout, as audio's is; one this request
    shared - another request's lookup, waited for - is not counted again here."""
    from shijhon.delivery import playback

    async with world(tmp_path, [Server()]) as (d, sources):
        source = sources[0]

        async def slow_lookup(*args: Any, **kwargs: Any) -> None:
            raise AddonError("timeout", "ReadTimeout", shared=shared)

        monkeypatch.setattr(source.addon, "find", slow_lookup)
        with pytest.raises(playback._Failed) as caught:
            await d._resolve_at(source, track(1), 2.0)
        assert isinstance(caught.value, playback._TimedOut) is not shared
