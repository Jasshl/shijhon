"""How urgent a request's add-on work is at the add-ons' limits: the song being played
first, then a client's other requests, then Shijhon's own background work."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest

from shijhon.delivery import pacing
from shijhon.delivery.download_first import Turn, _Waiting
from shijhon.delivery.playback import Deliverer, Track


def track(name: str = "song") -> Track:
    return Track(name, None, name, "Artist", 180_000, ref=f"demo:{name}")


def deliverer() -> Deliverer:
    return Deliverer(cast(Any, None))


@pytest.mark.parametrize(
    ("purpose", "head", "level"),
    [
        ("play", False, pacing.PLAY),  # a play, a seek
        ("play", True, pacing.QUEUED),  # a probe
        ("ahead", False, pacing.QUEUED),
        ("alone", False, pacing.QUEUED),
        ("warm", False, pacing.WARM),
        ("warm", True, pacing.WARM),
    ],
)
def test_a_request_is_as_urgent_as_what_it_is(purpose: str, head: bool, level: int) -> None:
    d = deliverer()
    assert pacing.current() is None
    with d._urgent(track(), purpose, head):
        mine = pacing.current()
        assert mine is not None and mine.level == level
        assert d._urgencies == {"demo:song": [mine]}
    assert pacing.current() is None and d._urgencies == {}


def test_a_caller_s_urgency_stands() -> None:
    """A queued download is routed as a play is (fallbacks and all), but waits like the
    download it is."""
    d = deliverer()
    with pacing.urgent(pacing.QUEUED) as download, d._urgent(track(), "play", False):
        assert pacing.current() is download and download.level == pacing.QUEUED


def test_a_play_takes_the_requests_for_its_song_under_way_along() -> None:
    d = deliverer()
    with d._urgent(track("next"), "warm", False):
        warming = pacing.current()
        assert warming is not None and warming.level == pacing.WARM
        with pacing.urgent(pacing.WARM):  # (another request's own context)
            token = pacing._URGENCY.set(None)
            try:
                with d._urgent(track("other"), "play", False):
                    assert warming.level == pacing.WARM  # another song's play: nothing
                with d._urgent(track("next"), "play", True):  # a probe of it
                    assert warming.level == pacing.QUEUED
                with d._urgent(track("next"), "play", False):  # the listener skipped to it
                    assert warming.level == pacing.PLAY
            finally:
                pacing._URGENCY.reset(token)
    assert d._urgencies == {}


def test_a_report_that_a_song_is_playing_takes_its_requests_under_way_along() -> None:
    d = deliverer()
    with d._urgent(track("queued"), "ahead", False):
        ahead = pacing.current()
        assert ahead is not None and ahead.level == pacing.QUEUED
        d.promote("demo:other")
        assert ahead.level == pacing.QUEUED
        d.promote("demo:queued")  # (the client's "now playing" report, its saved queue)
        assert ahead.level == pacing.PLAY
    d.promote("demo:queued")  # nothing under way: nothing to do


def test_a_download_is_queued_unless_it_is_the_song_being_played() -> None:
    assert Turn().urgency == pacing.QUEUED  # a queued download, a share link's
    assert Turn(playing=True).urgency == pacing.PLAY
    waiting = _Waiting()
    queued = Turn(waiting=waiting)
    assert queued.urgency == pacing.QUEUED
    waiting.promoted.set()  # the client plays it now
    assert queued.urgency == pacing.PLAY
    # The jukebox's upcoming songs go without waiting for the user's turns, but are no
    # song being played at the add-ons.
    assert Turn(playing=True, level=pacing.QUEUED).urgency == pacing.QUEUED


@pytest.mark.anyio
async def test_a_preparation_request_is_background_work_whoever_started_it() -> None:
    """... and goes to the first source that said "not now" and does not ask not to be
    used for downloads."""
    asked: list[tuple[str, int]] = []

    class Addon:
        def __init__(self, name: str, downloads: bool) -> None:
            self.name, self.downloads = name, downloads

        async def manifest(self) -> Any:
            return SimpleNamespace(downloads=self.downloads)

        async def availability(self, wanted: Any, *, prepare: bool = False) -> None:
            mine = pacing.current()
            asked.append((self.name, -1 if mine is None else mine.level))
            assert prepare

    d = deliverer()
    declined = [
        cast(Any, SimpleNamespace(addon=Addon(name, downloads), name=name))
        for name, downloads in (("not-for-downloads", False), ("S", True), ("T", True))
    ]
    with pacing.urgent(pacing.PLAY):  # started by a play's routing
        async with anyio.create_task_group() as tg:
            tg.start_soon(d._prepare, declined, track())
    assert asked == [("S", pacing.WARM)]


def test_a_probe_or_a_later_range_of_a_song_never_played_is_no_play() -> None:
    """Download-first streams (another format, a lower bitrate): the song being played never
    waits for the user's download turns - a HEAD is no play, and a later range only of a
    song being played from the add-ons (before: both went past every turn)."""
    from shijhon.delivery.ahead import AheadGate
    from shijhon.delivery.intercept import Interceptor
    from shijhon.delivery.listening import Listening
    from shijhon.delivery.playback import Known

    listening = Listening()
    interceptor = Interceptor.__new__(Interceptor)
    interceptor.warm = cast(Any, SimpleNamespace(listening=listening))
    interceptor.ahead = AheadGate(listening, window_seconds=0.5)
    interceptor.deliverer = deliverer()
    who, song = ("ann", "phone"), track()

    def call(method: str, range_header: str | None = None) -> Any:
        headers = [(b"range", range_header.encode())] if range_header else []
        return SimpleNamespace(http_method=method, headers=headers)

    assert interceptor._playing(call("GET"), who, song) is True  # a play at byte zero
    assert interceptor._playing(call("HEAD"), who, song) is False
    assert interceptor._playing(call("HEAD", "bytes=0-1"), who, song) is False
    assert interceptor._playing(call("GET", "bytes=4096-"), who, song) is False  # never played
    # A probe (or a warm-ahead) leaves what is known of the file, and is no play.
    interceptor.deliverer._known[song.song_id] = Known(1000, None, 1, "t1")
    assert interceptor._playing(call("GET", "bytes=4096-"), who, song) is False
    assert not interceptor.deliverer.played(song)
    interceptor.deliverer._heard[song.key] = None  # a client was sent its audio
    assert interceptor._playing(call("GET", "bytes=4096-"), who, song) is True  # its seek
    assert interceptor.deliverer.played(song)
    assert not interceptor.deliverer.played(track("another"))
    interceptor.ahead.window = 0  # nothing told apart: every fetch waits its turn
    assert interceptor._playing(call("GET"), who, song) is False


@pytest.mark.anyio
async def test_a_redirected_audio_request_stops_at_the_hop_once_the_audio_is_left_alone() -> None:
    """An audio address may redirect (a cache, a CDN): the add-on's audio being left alone
    after a 429 - another request's answer meanwhile - is looked at before every hop, not
    only before the first; the opening it held is over."""
    import httpx

    from shijhon.delivery.addon import StreamInfo
    from shijhon.delivery.playback import Pin, _Cooling

    pace = pacing.AddonPace(pacing.Limits(0, 1, 1))
    seen: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/audio":
            pace.block(30, audio=True)  # (another audio request of it was told 429)
            return httpx.Response(302, headers={"location": "https://cdn.invalid/file"})
        return httpx.Response(200, content=b"audio")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        source = SimpleNamespace(id=1, name="Synthetic", http=http, budget=None, pace=pace)
        d = deliverer()
        link = StreamInfo("https://synthetic.invalid/audio", "direct")
        pin = Pin("song", cast(Any, source), "track", link, d.clock())
        with pacing.urgent(pacing.QUEUED), pytest.raises(_Cooling):
            await d._request(pin, None, False, d.clock() + 5)
    assert seen == ["/audio"]  # the hop to the other address was not sent
    assert pace._opening == 0

    # Without a block the redirect is followed, and the opening lasts to the first bytes.
    free = pacing.AddonPace(pacing.Limits(0, 1, 1))

    async def plain(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/audio":
            return httpx.Response(302, headers={"location": "https://cdn.invalid/file"})
        length = {"content-length": "5"}
        return httpx.Response(200, headers=length, stream=httpx.ByteStream(b"audio"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(plain)) as http:
        source = SimpleNamespace(id=1, name="Synthetic", http=http, budget=None, pace=free)
        registry = SimpleNamespace(succeeded=lambda *a: None, failed=lambda *a: None)
        d = Deliverer(cast(Any, registry))
        pin = Pin("song", cast(Any, source), "track", link, d.clock())
        with pacing.urgent(pacing.QUEUED):
            opened = await d._request(pin, None, False, d.clock() + 5)
        assert opened.status == 200 and opened.body is not None and free._opening == 1
        assert b"".join([chunk async for chunk in opened.body]) == b"audio"
        assert free._opening == 0
        await opened.close()
        assert free._opening == 0 and free.idle


@pytest.mark.anyio
async def test_only_audio_sent_to_a_client_makes_a_song_played() -> None:
    """A HEAD and a warm-ahead open a song's audio too: neither is a play (a later range
    of such a song, with a format or bitrate, waits its turn among the user's downloads)."""
    import httpx

    from shijhon.delivery.addon import StreamInfo
    from shijhon.delivery.playback import Pin

    async def handle(request: httpx.Request) -> httpx.Response:
        total = {"content-range": "bytes 0-0/5", "content-length": "1"}
        if request.headers.get("range") == "bytes=0-0":
            return httpx.Response(206, headers=total, stream=httpx.ByteStream(b"a"))
        return httpx.Response(
            200, headers={"content-length": "5"}, stream=httpx.ByteStream(b"audio")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        source = SimpleNamespace(id=1, name="Synthetic", http=http, budget=None, pace=None)

        async def enabled() -> list[Any]:
            return []

        registry = SimpleNamespace(
            enabled=enabled,
            cooling=lambda _: False,
            succeeded=lambda *a: None,
            failed=lambda *a: None,
            attempted=lambda *a: None,
        )
        d = Deliverer(cast(Any, registry))
        d.settings.length_tolerance_seconds = d.settings.length_tolerance_percent = 0.0
        link = StreamInfo("https://synthetic.invalid/audio", "direct")
        for name in ("probed", "warmed", "played"):
            d._remember(name, Pin(name, cast(Any, source), "t", link, d.clock()))
        probed, warmed, played = (Track(n, None, n, "A", 0) for n in ("probed", "warmed", "played"))
        opened = await d.open(probed, None, head=True)
        assert opened.status == 200 and "probed" in d._known and not d.played(probed)
        assert await d.prewarm(warmed) is True  # (it has a link: nothing to do)
        opened = await d.open(warmed, "bytes=0-0", purpose="warm", budget=5.0)
        await opened.close()
        assert "warmed" in d._known and not d.played(warmed)
        # A download-first fetch gets the whole file - for the library, not for a client.
        fetched = Track("fetched", None, "fetched", "A", 0)
        d._remember("fetched", Pin("fetched", cast(Any, source), "t", link, d.clock()))
        opened = await d.open(fetched, None, heard=False)
        await opened.close()
        assert "fetched" in d._known and not d.played(fetched)
        opened = await d.open(played, None)
        await opened.close()
        assert d.played(played)
