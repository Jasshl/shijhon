"""The songs' links (pins) kept by the deliverer: past the limit the expired go first, then
the oldest - never an error while doing so; a request whose link failed late drops only
that link, not a newer one another request found meanwhile."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import anyio
import httpx
import pytest

from shijhon.delivery import pacing
from shijhon.delivery.addon import StreamInfo
from shijhon.delivery.intercept import Interceptor
from shijhon.delivery.playback import (
    MAX_PINS,
    ByteRange,
    Deliverer,
    Known,
    Pin,
    PinBroken,
    PlaybackSettings,
    Track,
    _Changed,
    _holds,
    _Other,
)
from shijhon.proxy.params import RestCall


def link(created: float, source_id: int = 1) -> Any:
    """A pin as far as ``pinned`` looks at it."""
    return cast(
        Any,
        SimpleNamespace(
            source=SimpleNamespace(id=source_id),
            wrong=None,
            created=created,
            info=SimpleNamespace(expires_at=None),
        ),
    )


def deliverer(now: list[float]) -> Deliverer:
    return Deliverer(
        cast(Any, None), PlaybackSettings(pin_ttl_seconds=1800.0), clock=lambda: now[0]
    )


def test_past_the_limit_the_expired_links_go_and_new_ones_are_kept() -> None:
    now = [0.0]
    d = deliverer(now)
    for n in range(2000):
        d._remember(f"old-{n}", link(now[0]))
    now[0] = 1000.0
    for n in range(MAX_PINS - 2000):
        d._remember(f"live-{n}", link(now[0]))
    now[0] = 1900.0  # the first 2,000 have expired (30 minutes)
    rejected = link(now[0])
    rejected.wrong = "another recording"
    d._pins["rejected"] = rejected  # a link read and rejected is not kept either
    newest = link(now[0])
    d._remember("newest", newest)  # 4,097 kept: the limit is passed
    assert d.pinned("newest") is newest
    assert len(d._pins) == MAX_PINS - 2000 + 1
    assert d.pinned("old-0") is None and d.pinned("live-0") is not None
    # Every later new link is kept too (it used to fail from then on).
    for n in range(10):
        d._remember(f"later-{n}", link(now[0]))
        assert d.pinned(f"later-{n}") is not None


def test_past_the_limit_with_every_link_valid_the_oldest_go() -> None:
    now = [0.0]
    d = deliverer(now)
    for n in range(MAX_PINS):
        now[0] = float(n) / 10
        d._remember(f"song-{n}", link(now[0]))
    d._remember("song-0", link(now[0]))  # renewed: now the newest
    d._remember("another", link(now[0]))
    assert len(d._pins) == MAX_PINS
    assert d.pinned("song-1") is None  # the oldest went
    assert d.pinned("song-0") is not None and d.pinned("another") is not None


def test_forgetting_a_failed_link_keeps_a_newer_one() -> None:
    now = [0.0]
    d = deliverer(now)
    failed, newer = link(0.0), link(0.0, source_id=2)
    d._remember("song", failed)
    d._remember("song", newer)  # another request found a new link meanwhile
    d.forget("song", failed)  # the first request's late failure
    assert d.pinned("song") is newer
    d.forget("song", newer)
    assert d.pinned("song") is None
    d._remember("song", failed)
    d.forget("song")  # the song's link, whichever it is (its audio was delivered)
    assert d.pinned("song") is None


def test_a_continuing_play_takes_a_link_only_to_its_own_file() -> None:
    def pin(source: int, size: int | None, etag: str | None = None) -> Any:
        return cast(Any, SimpleNamespace(source=SimpleNamespace(id=source), size=size, etag=etag))

    play = Known(1000, '"a"', 1, "t1")
    assert _holds(pin(1, 1000, '"a"'), play) and _holds(pin(2, 1000), play)
    assert not _holds(pin(1, 2000, '"a"'), play)  # another size
    assert not _holds(pin(1, 1000, '"b"'), play)  # another strong ETag
    assert _holds(pin(2, None), play)  # not answered yet: its answer is checked
    assert not _holds(pin(2, None, '"b"'), play)  # no size told, but another strong ETag
    unknown = Known(None, None, 1, "t1")  # nothing to recognize the play's file by
    assert _holds(pin(1, 2000), unknown) and not _holds(pin(2, None), unknown)
    assert _holds(pin(2, 5), None)  # a new play takes any link


@pytest.mark.anyio
async def test_a_seek_checks_its_own_file_also_when_the_link_is_identified_meanwhile() -> None:
    """A seek of an earlier play asks a new play's link that has not answered yet; the new
    play's answer identifies the link (another file) before the seek's answer comes: the
    seek still checks that answer against its own file, and takes none of the other's."""
    seek_sent, release = anyio.Event(), anyio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("range") == "bytes=100-199":
            seek_sent.set()
            await release.wait()
            headers = {"content-range": "bytes 100-199/2000", "content-length": "100"}
            return httpx.Response(206, headers=headers, stream=httpx.ByteStream(b"B" * 100))
        return httpx.Response(
            200, headers={"content-length": "2000"}, stream=httpx.ByteStream(b"B" * 2000)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        source = SimpleNamespace(id=1, name="Synthetic", http=http, budget=None, pace=None)

        async def enabled() -> list[Any]:
            return []  # (nowhere to find the earlier play's file again)

        registry = SimpleNamespace(
            enabled=enabled,
            cooling=lambda _: False,
            succeeded=lambda *args: None,
            failed=lambda *args: None,
        )
        d = Deliverer(cast(Any, registry))
        track = Track("song", None, "Synthetic", "Synthetic", 0)
        link = StreamInfo("https://synthetic.invalid/audio", "direct")
        shared = Pin("song", cast(Any, source), "track", link, d.clock())
        d._known["song"] = Known(1000, None, 1, "track", "mp3", 320)  # the earlier play's file
        d._remember("song", shared)  # a new play's link, not answered yet
        outcome: list[Any] = []

        async def seek() -> None:
            try:
                opened = await d.open(track, "bytes=100-199")
            except PinBroken as exc:
                outcome.append(exc)
            else:
                outcome.append(opened)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(seek)
            await seek_sent.wait()
            play = await d.open(track, None)  # the new play's audio answers: 2,000 bytes
            await play.close()
            assert shared.size == 2000
            release.set()
    assert len(outcome) == 1 and isinstance(outcome[0], PinBroken)
    assert d.pinned("song") is shared  # the new play's link stays


@pytest.mark.anyio
async def test_a_play_remembers_its_file_under_the_name_it_was_asked_by() -> None:
    """A song committed while it was played is one song under both its names, and both
    remember the file that play started with (A). A later routing under the catalog track
    publishes a link to another file (B) under both names - the link's own name is the
    catalog track. A play by the native ID from byte zero gets B: B is remembered under
    the native ID, so that play's seek continues on B (held to A, it failed). The same the
    other way round; and a play going on under one name keeps its file while the other
    name starts a new one."""

    async def handle(request: httpx.Request) -> httpx.Response:
        total = 2000 if request.url.path == "/b" else 3000
        if request.headers.get("range") == "bytes=100-199":
            headers = {"content-range": f"bytes 100-199/{total}", "content-length": "100"}
            return httpx.Response(206, headers=headers, stream=httpx.ByteStream(b"x" * 100))
        return httpx.Response(
            200, headers={"content-length": str(total)}, stream=httpx.ByteStream(b"x" * total)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        source = cast(
            Any, SimpleNamespace(id=1, name="Synthetic", http=http, budget=None, pace=None)
        )

        async def enabled() -> list[Any]:
            return []  # (nowhere to find an earlier play's file again)

        registry = SimpleNamespace(
            enabled=enabled,
            cooling=lambda _: False,
            succeeded=lambda *args: None,
            failed=lambda *args: None,
        )
        d = Deliverer(cast(Any, registry))
        ref, native = "demo:1", "native-1"
        by_ref = Track(ref, None, "Synthetic", "Synthetic", 0, ref=ref)
        by_native = Track(native, None, "Synthetic", "Synthetic", 0, ref=ref)

        async def asked(track: Track, range_header: str | None = None) -> int:
            opened = await d.open(track, range_header)
            await opened.close()
            return opened.status

        a = StreamInfo("https://synthetic.invalid/a", "direct")
        link_a = Pin(ref, source, "track", a, d.clock())
        link_a.size = 1000
        d._remember(ref, link_a)
        d._known[ref] = Known(1000, None, 1, "track", "mp3", 320)
        d.adopt(ref, native)  # the commit, that play going on: one song under both names
        assert d.pinned(native) is link_a and d._known[native].size == 1000
        # A later routing under the catalog track: file B, played there from byte zero.
        b = StreamInfo("https://synthetic.invalid/b", "direct")
        link_b = Pin(ref, source, "track", b, d.clock())
        d._remember(ref, link_b)
        assert d.pinned(native) is link_b and link_b.song_id == ref
        assert await asked(by_ref) == 200 and d._known[ref].size == 2000
        assert d._known[native].size == 1000
        # A play by the native ID from byte zero gets B, and its seek continues on B.
        assert await asked(by_native) == 200
        assert await asked(by_native, "bytes=100-199") == 206
        assert d._known[native].size == 2000 and d.pinned(native) is link_b
        # The other way round: a routing under the native ID publishes file C, played there.
        c = StreamInfo("https://synthetic.invalid/c", "direct")
        link_c = Pin(native, source, "track", c, d.clock())
        d._remember(native, link_c)
        assert await asked(by_native) == 200 and d._known[native].size == 3000
        # The play of B going on under the catalog track keeps its file: its seek takes
        # nothing of C (and B is nowhere to be found again) ...
        assert d._known[ref].size == 2000
        with pytest.raises(PinBroken):
            await d.open(by_ref, "bytes=100-199")
        assert d.pinned(ref) is link_c and d._known[native].size == 3000
        # ... until it starts again from byte zero: C, and its seeks continue on C.
        assert await asked(by_ref) == 200 and d._known[ref].size == 3000
        assert await asked(by_ref, "bytes=100-199") == 206


def _answered(pin: Any, headers: dict[str, str], expect: Known, mine: bool = True) -> Any:
    """A continuing play's range answered by ``pin``'s link (206, bytes 100-199)."""
    d = deliverer([0.0])
    response = httpx.Response(206, headers=headers, stream=httpx.ByteStream(b"x" * 100))
    return d._answer(pin, ByteRange.parse("bytes=100-199"), False, response, expect, mine)


def _link(size: int | None = None, etag: str | None = None) -> Pin:
    info = StreamInfo("https://synthetic.invalid/audio", "direct")
    pin = Pin("song", cast(Any, SimpleNamespace(id=1, name="Synthetic")), "track", info, 0.0)
    pin.size, pin.etag = size, etag
    return pin


def test_a_renewed_link_keeps_what_the_play_knows_of_its_file() -> None:
    """The play's new link answers without an ETag: the play's own stays its validator
    (its next ranges still ask for that file), and its format stays known."""
    play = Known(2000, '"a"', 1, "track", "mp3", 320)
    link = StreamInfo("https://synthetic.invalid/audio", "direct")
    renewed = Pin("song", cast(Any, SimpleNamespace(id=1, name="Synthetic")), "track", link, 0.0)
    headers = {"content-range": "bytes 100-199/2000", "content-length": "100"}
    opened = _answered(renewed, headers, play)
    assert opened.status == 206
    assert (renewed.size, renewed.etag) == (2000, '"a"')
    assert (renewed.kind, renewed.kbps) == ("mp3", 320)
    # A HEAD identified the link (its size) while the range waited for its answer: the
    # format is still the play's.
    identified = Pin("song", cast(Any, SimpleNamespace(id=1, name="Synthetic")), "track", link, 0.0)
    identified.size = 2000
    _answered(identified, headers, play)
    assert (identified.kind, identified.kbps, identified.etag) == ("mp3", 320, '"a"')


def test_another_request_s_link_to_another_file_is_left_alone() -> None:
    """The play's file is checked on every answer of a continuing play - whatever is known
    of the link by then: a link identified by a HEAD without an ETag that answers another
    ETag, a link identified as another file while the play's range waited, a link whose
    answer is another size. Such a link is not the play's and stays as it is (it may be a
    new play's valid link); the play's own new link is dropped instead."""
    play = Known(2000, '"a"', 1, "track", "mp3", 320)
    same_size = {"content-range": "bytes 100-199/2000", "content-length": "100"}
    identified = _link(2000)  # by a HEAD, no ETag told
    with pytest.raises(_Other):
        _answered(identified, {**same_size, "etag": '"b"'}, play, mine=False)
    assert (identified.size, identified.etag) == (2000, None)
    meanwhile = _link(3000, '"b"')  # a new play's answer identified it while the range waited
    with pytest.raises(_Other):
        _answered(meanwhile, same_size, play, mine=False)  # (even an answer of the old file)
    assert (meanwhile.size, meanwhile.etag) == (3000, '"b"')
    unanswered = _link()
    other_size = {"content-range": "bytes 100-199/3000", "content-length": "100"}
    with pytest.raises(_Other):
        _answered(unanswered, other_size, play, mine=False)
    assert unanswered.size == 3000  # what that link serves: known from now on
    with pytest.raises(_Changed):  # the play's own new link: not its file - dropped
        _answered(_link(), other_size, play, mine=True)


def test_a_song_put_in_the_library_keeps_its_links_only_when_it_was_played() -> None:
    """...or while a request for it is being opened (a play whose first bytes are not
    there yet)."""
    now = [0.0]
    d = deliverer(now)
    played = Track("played", None, "T", "A", 1000, ref="demo:1")
    fetched = Track("fetched", None, "T", "A", 1000, ref="demo:2")
    opening = Track("opening", None, "T", "A", 1000, ref="demo:3")
    for name in ("played", "demo:1", "fetched", "demo:2", "opening", "demo:3"):
        d._remember(name, link(now[0]))
    d._heard["demo:1"] = None  # a client was sent its audio (under its key)
    d._urgencies["demo:3"] = [pacing.Urgency()]  # a request for it under way
    for track in (played, fetched, opening):
        d.in_library(track)
    assert d.holds(played) and d.pinned("played") is not None and d.holds(opening)
    assert not d.holds(fetched) and d.pinned("fetched") is None and d.pinned("demo:2") is None


def _call(range_header: str | None, query: bytes = b"") -> RestCall:
    headers = [(b"range", range_header.encode())] if range_header else []
    return RestCall.build("stream", "GET", b"/rest/stream", query, headers, None)


def test_a_play_from_the_add_ons_keeps_its_file_while_its_link_is_kept() -> None:
    """Once the song's audio is in the library: a later range of a listener's play served
    from the add-ons stays there - not a HEAD, not another listener's, not after the
    listener began another play of it (also when an older play's first bytes came after
    that), not once the song's link expired. A play for a format and bitrate is another
    one: its start does not end the plain stream's. A play first asked for at a later
    range (after a restart) counts once it was served there."""
    now = [0.0]
    d = deliverer(now)
    nothing = cast(Any, None)
    interceptor = Interceptor(nothing, d, cast(Any, SimpleNamespace(engine=None)), nothing, nothing)
    track = Track("song", None, "T", "A", 1000, ref="demo:1")
    me, other = ("user", "app"), ("user", "another app")
    at_zero, later = _call(None), _call("bytes=100-")
    converting = _call(None, b"format=mp3&maxBitRate=128")
    get, head = cast(Any, SimpleNamespace(head=False)), cast(Any, SimpleNamespace(head=True))
    start = interceptor._starting(at_zero, get, me, track.key)
    assert start is not None and interceptor._starting(later, get, me, track.key) == start
    assert interceptor._starting(at_zero, head, me, track.key) is None
    d._remember("demo:1", link(now[0]))  # the play's link, under its catalog track
    assert not interceptor._on_its_file(later, get, me, track)  # (no first bytes yet)
    interceptor._began(start)
    assert interceptor._on_its_file(later, get, me, track)
    assert not interceptor._on_its_file(later, head, me, track)
    assert not interceptor._on_its_file(at_zero, get, me, track)
    assert not interceptor._on_its_file(later, get, other, track)
    assert interceptor._starting(converting, get, me, track.key) is not None  # (a save)
    assert interceptor._on_its_file(later, get, me, track)
    converted_later = _call("bytes=100-", b"format=mp3&maxBitRate=128")
    assert not interceptor._on_its_file(converted_later, get, me, track)
    again = interceptor._starting(at_zero, get, me, track.key)  # (served by Navidrome)
    assert again is not None and again != start
    interceptor._began(start)  # the older play's first bytes, late
    assert not interceptor._on_its_file(later, get, me, track)
    interceptor._began(again)  # (had it been served at the add-ons)
    assert interceptor._on_its_file(later, get, me, track)
    resumed = interceptor._starting(later, get, other, track.key)  # first seen mid-file
    assert resumed is not None and not interceptor._on_its_file(later, get, other, track)
    interceptor._began(resumed)
    assert interceptor._on_its_file(later, get, other, track)
    now[0] = 1801.0  # the song's link expired
    assert not interceptor._on_its_file(later, get, me, track)
    d._remember("song", link(now[0]))
    assert not interceptor._on_its_file(later, get, me, track)  # handed over for good


def test_a_link_dropped_after_it_failed_still_holds_the_song_until_it_would_expire() -> None:
    """A play's request whose link failed drops it and looks for a new one to the same
    file: meanwhile the song still has its link for the play's other requests - until
    the dropped link would have expired; another recording's link never holds it."""
    now = [0.0]
    d = deliverer(now)
    track = Track("song", None, "T", "A", 1000)
    failed = link(now[0])
    d._remember("song", failed)
    d.forget("song", failed)  # its request routes on, for the play's file
    assert d.pinned("song") is None and d.holds(track)
    now[0] = 1000.0
    renewed = link(now[0])
    d._remember("song", renewed)
    d.forget("song", renewed)
    now[0] = 2000.0  # past the first link's lifetime, within the renewed one's
    assert d.holds(track)
    now[0] = 2801.0
    assert not d.holds(track)
    wrong = link(now[0])
    wrong.wrong = "another recording"
    d._remember("song", wrong)
    d.forget("song", wrong)
    assert not d.holds(track)
    d.forget("song")  # (all of the song's links, as for a song put in the library unplayed)
    assert not d.holds(track)
