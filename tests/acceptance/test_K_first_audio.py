"""Suite K (continued) - first audio when the plan's first add-on cannot serve.

A plan of four steps (Primary, Reliable, Checker, Preparer with a time of its own), with
ready checks.

- The delivered audio's real length is read from its first bytes and logged; audio whose
  length differs from the catalog's by more than a tolerance is another recording: not
  used, the next source is tried, and the add-on is not asked for it again for a while.
- Checks answered while a step goes on order the steps after it: an add-on that has the
  song ready goes next (or takes over after the handover time), one that said "not now"
  last; one whose check has not answered keeps its place in the plan.
- The stream a failed download-first falls back to continues that routing: the source its
  wait cap cut short first, then those it did not try; never again those it is done with.
- An add-on answering errors (an HTTP 5xx, no connection, a broken answer) cools down after
  a few in a row without a delivery in between (a setting, 3; 0: off), so plays go to the
  other sources at once; until it delivers again each further error cools it down again,
  for the short cooldown only, so it comes back quickly. Misses, refusals and slow answers
  do not count.
- A request that waited for another request's routing of the same song (a client's probe
  and its play at once) gets its first byte with a time of its own: the wait used up its
  own, and it failed although the audio was there.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest

from shijhon.app import ShijhonApp
from shijhon.delivery.sources import Source
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack, fake_resolver
from tests.harness.running import RunningServer

CHECKS = ("stream", "isrc", "availability")
BUDGET = 3.0
OWN_BUDGET = 6.0  # the preparing source's own times (its step's and a download's)
COOLDOWN = 3.0  # long enough that the next play surely comes within it


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("first-audio"),
        budget_seconds=BUDGET,
        cooldown_seconds=COOLDOWN,
        ready_checks=True,
        ready_handover_seconds=1.0,
        availability_timeout_seconds=0.5,
        max_wait_seconds=12.0,
        warm_ahead_depth=0,
    ) as w:
        yield w


@pytest.fixture
def sources(world: DeliveryWorld) -> Iterator[dict[str, FakeAddon]]:
    world.clear_sources()
    world.server.call(_forget(world))
    addons = {
        "primary": world.addon("Primary", resources=("stream", "isrc", "resolve")),
        "reliable": world.addon("Reliable"),
        "checker": world.addon("Checker", resources=CHECKS),
        "preparer": world.addon("Preparer"),
    }
    for name, addon in addons.items():
        own = OWN_BUDGET if name == "preparer" else None
        world.add_source(addon, budget_seconds=own, step_seconds=own)
    yield addons
    world.clear_sources()


def _forget(world: DeliveryWorld):  # type: ignore[no-untyped-def]
    async def forget() -> None:
        deliverer = world.services.deliverer
        deliverer._misses.clear()
        deliverer._failed.clear()

    return forget


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def playback_log() -> Iterator[list[str]]:
    collector = Collect()
    logger = logging.getLogger("shijhon.delivery.playback")
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(collector)
    yield collector.lines
    logger.removeHandler(collector)
    logger.setLevel(previous)


def play(world: DeliveryWorld, song: str, client: str = "first-audio") -> httpx.Response:
    return world.client(client=client).request("stream", {"id": song})


def release(
    world: DeliveryWorld, key: str, sources: dict[str, FakeAddon], count: int = 1
) -> tuple[list[str], dict[str, list[FakeTrack]], list[bytes]]:
    """A release of ``count`` songs every source has. Returns (song IDs, each source's fake
    tracks, each song's audio)."""
    made = catalog_release(key, f"Title {key}", f"Artist {key}", count)
    result = world.materialize(made)
    songs = [result.created[t.ref] for t in made.tracks]
    fakes: dict[str, list[FakeTrack]] = {name: [] for name in sources}
    audio: list[bytes] = []
    for track in made.tracks:
        path = world.audio(f"{key}-{track.number}")
        audio.append(path.read_bytes())
        assert track.isrc is not None
        for name, addon in sources.items():
            fakes[name].append(addon.add(FakeTrack(isrc=track.isrc, audio=path)))
    return songs, fakes, audio


def source(world: DeliveryWorld, name: str) -> Source:
    async def find() -> Source:
        return next(s for s in await world.services.sources.enabled() if s.name == name)

    return world.server.call(find)


@contextmanager
def slow_checks(world: DeliveryWorld) -> Iterator[None]:
    """Checks that may take up to 3 s (a check's answer then comes in when the test wants
    it, whatever the machine's load)."""
    settings = world.services.deliverer.settings
    saved = settings.availability_timeout_seconds
    settings.availability_timeout_seconds = 3.0
    try:
        yield
    finally:
        settings.availability_timeout_seconds = saved


def lines_with(lines: list[str], text: str) -> list[str]:
    return [line for line in lines if text in line]


# --- a request that waited for another's routing -----------------------------------------


def test_a_request_that_waited_for_another_s_routing_gets_its_first_byte(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Two requests for one song at once (a client's probe and its play); the routing takes
    longer than the byte-zero budget (a preparing worker). The request that waited for it
    has spent its own budget waiting: its first byte still gets its time, and it plays the
    audio that is there."""
    [song], fakes, [audio] = release(world, "waited", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].available = False
    fakes["checker"][0].available = False
    fakes["preparer"][0].resolve_delay = BUDGET + 0.5  # its own budget covers it
    fakes["preparer"][0].first_byte_delay = 1.8  # more than half the byte-zero budget
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(play, world, song, "waited-a")
        time.sleep(0.3)
        second = pool.submit(play, world, song, "waited-b")
        answers = [first.result(), second.result()]
    assert [a.content for a in answers] == [audio, audio]
    assert lines_with(playback_log, "for another request's routing of this song")
    assert not lines_with(playback_log, "no audio for 'Title waited Song 1'")


def test_a_request_that_waited_keeps_the_terms_of_the_routing_that_found_the_link(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Without ready checks (concurrent requests share one routing): the source found has
    its step's whole time; the request that waited for it gets the same for its first byte,
    not what the wait left of it."""
    settings = world.services.deliverer.settings
    [song], fakes, [audio] = release(world, "waited-terms", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].resolve_delay = 0.5  # the second request waits for it
    fakes["reliable"][0].first_byte_delay = 3.5  # over half the budget (6 s here)

    async def without_checker(enabled: bool) -> None:
        registry = world.services.sources
        [checker] = [s for s in await registry.stored() if s.name == "Checker"]
        await registry.set_enabled(checker.id, enabled)

    world.server.call(lambda: without_checker(False))  # the reliable one, then the preparer
    settings.ready_checks = False
    settings.budget_seconds = 6.0
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(play, world, song, "terms-a")
            time.sleep(0.2)
            second = pool.submit(play, world, song, "terms-b")
            answers = [first.result(), second.result()]
    finally:
        settings.ready_checks = True
        settings.budget_seconds = BUDGET
    assert [a.content for a in answers] == [audio, audio]
    assert not lines_with(playback_log, "no audio for 'Title waited-terms Song 1'")
    # One source, one link: the request that waited did not give up on it for the next.
    assert len(sources["reliable"].requests("stream")) == 1
    assert sources["preparer"].requests("stream") == []


# --- another recording, by its length ----------------------------------------------


@contextmanager
def tolerance(world: DeliveryWorld, seconds: float) -> Iterator[None]:
    """The length check (off in the test harness), with a tolerance of test songs' size."""
    settings = world.services.deliverer.settings
    settings.length_tolerance_seconds = seconds
    try:
        yield
    finally:
        settings.length_tolerance_seconds = 0.0


def test_another_recording_is_not_used_and_is_remembered(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """An add-on's ISRC lookup hands over a remix album's extended version of a much
    shorter track. The length read from the first bytes gives it away: the next
    source plays, and the add-on is not asked for that recording again."""
    [song], fakes, [audio] = release(world, "wrong-length", sources)
    long = world.audio("wrong-length-long", "flac", seconds=12).read_bytes()
    fakes["primary"][0].audio = world.tmp / "long.flac"
    fakes["primary"][0].audio.write_bytes(long)
    with tolerance(world, 2.0):
        assert play(world, song).content == audio
        [line] = lines_with(playback_log, "playing 'Title wrong-length Song 1' from Reliable")
        assert "Primary: another recording (12.0s, the catalog's 3.0s)" in line
        assert ", length 3.0s" in line
        assert lines_with(
            playback_log,
            "Primary delivered another recording of 'Title wrong-length Song 1': 12.0s long,"
            " the catalog's 3.0s",
        )
        primary = source(world, "Primary").stats
        assert primary.successes == 0 and primary.failures_since_success == 1  # once
        world.server.call(_forget_pin(world, song))
        asked = len(sources["primary"].requests())
        assert play(world, song).content == audio
        assert len(sources["primary"].requests()) == asked  # not asked again at all
        [again] = lines_with(playback_log, "playing 'Title wrong-length Song 1' from Reliable")[1:]
        assert "Primary: another recording (remembered)" in again


def test_a_client_s_probe_is_answered_from_the_right_recording(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A client that asks for two bytes first (a player's probe; warm-ahead asks for one)
    gets them from a recording whose length was read: the source is asked for its first
    512 KB, so the probe itself finds another recording (MP3 here) and the next source
    answers it - the client never sees the other file's size. Also from a source that
    ignores ranges (it sends the whole file)."""
    [song], fakes, [audio] = release(world, "wrong-probe", sources)
    long = world.audio("wrong-probe-long", "mp3", seconds=12)
    fakes["primary"][0].audio = long
    fakes["primary"][0].content_type = "audio/mpeg"
    fakes["reliable"][0].ranges = False
    client = world.client(client="probe")
    with tolerance(world, 2.0):
        probe = client.request("stream", {"id": song}, headers={"Range": "bytes=0-1"})
        assert lines_with(playback_log, "Primary delivered another recording of 'Title wrong-probe")
        assert probe.status_code == 206 and probe.content == audio[:2]
        assert probe.headers["content-range"] == f"bytes 0-1/{len(audio)}"
        full = client.request("stream", {"id": song})
    assert full.content == audio


def test_a_length_within_the_tolerance_plays(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, _ = release(world, "near-length", sources)
    near = world.audio("near-length-4", "flac", seconds=4)
    fakes["primary"][0].audio = near
    with tolerance(world, 2.0):
        assert play(world, song).content == near.read_bytes()
    [line] = lines_with(playback_log, "playing 'Title near-length Song 1' from Primary")
    assert ", length 4.0s" in line


def test_the_tolerance_can_be_a_share_of_the_length(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The larger of the seconds and the share: 50 % of a 3 s song lets 4 s through, not
    12 s."""
    settings = world.services.deliverer.settings
    songs, fakes, audio = release(world, "share", sources, count=2)
    near = world.audio("share-near", "flac", seconds=4)
    far = world.audio("share-far", "flac", seconds=12)
    fakes["primary"][0].audio, fakes["primary"][1].audio = near, far
    settings.length_tolerance_percent = 50.0
    try:
        with tolerance(world, 0.5):
            assert play(world, songs[0]).content == near.read_bytes()
            assert play(world, songs[1]).content == audio[1]
    finally:
        settings.length_tolerance_percent = 0.0
    assert lines_with(playback_log, "Primary delivered another recording of 'Title share Song 2'")


def test_after_another_recording_the_routing_goes_on_to_the_next_source(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """After another recording the routing goes on to the next steps: here one too slow,
    then the worker."""
    [song], fakes, [audio] = release(world, "wrong-goes-on", sources)
    fakes["primary"][0].available = False
    long = world.audio("wrong-goes-on-long", "flac", seconds=12)
    fakes["reliable"][0].audio = long
    fakes["checker"][0].resolve_delay = BUDGET + 2  # cannot tell; its link times out
    with tolerance(world, 2.0):
        assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title wrong-goes-on Song 1' from Preparer")
    assert "Reliable: another recording (12.0s, the catalog's 3.0s)" in line
    assert "Checker: stopped after" in line and "slow: counted" in line


def test_a_request_whose_borrowed_link_was_another_recording_waits_for_that_routing(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A client's two requests at once; the second used the link the first
    one's routing found, and both found it another recording. The second started a
    routing of its own - the sources in the user's order, a "not now" one first - and held
    the song for its whole budget, so the first one's next source, a worker preparing the
    song, was left too little of the client's wait and the play failed. Now the second waits
    for the first one's routing, which goes on at once, and both get the worker's audio."""
    settings = world.services.deliverer.settings
    [song], fakes, [audio] = release(world, "two-requests", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].audio = world.audio("two-requests-long", "flac", seconds=12)
    fakes["reliable"][0].first_byte_delay = 1.0  # the second request uses its link
    fakes["checker"][0].ready = False  # "not now"
    fakes["checker"][0].resolve_delay = 10.0  # and it would hang
    fakes["preparer"][0].prepare_seconds = 4.5  # its own budget is 6 s
    saved, settings.max_wait_seconds = settings.max_wait_seconds, 7.5
    try:
        with tolerance(world, 2.0), ThreadPoolExecutor(2) as pool:
            first = pool.submit(play, world, song, "two-requests-a")
            time.sleep(0.15)
            second = pool.submit(play, world, song, "two-requests-b")
            answers = [first.result(), second.result()]
    finally:
        settings.max_wait_seconds = saved
    assert [a.content for a in answers] == [audio, audio]
    assert not lines_with(playback_log, "no audio for 'Title two-requests Song 1'")
    assert sources["checker"].requests("stream") == []
    # The second found the link failed itself, or left it when the first one's routing
    # dropped it - either way it followed that routing.
    borrowed = [
        line
        for line in playback_log
        if "(another request's link, then" in line or "routing went on without it" in line
    ]
    assert borrowed and all(
        "playing 'Title two-requests Song 1' from Preparer" in b for b in borrowed
    )


def test_a_request_whose_borrowed_link_failed_takes_the_next_link_at_once(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """It waits for the routing's next link, not for that routing's first byte: both get
    the next source's audio side by side (its first byte takes 2 s)."""
    [song], fakes, [audio] = release(world, "next-link", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].audio = world.audio("next-link-long", "flac", seconds=12)
    fakes["reliable"][0].first_byte_delay = 1.0  # the second request uses its link
    fakes["checker"][0].ready = False
    fakes["preparer"][0].first_byte_delay = 2.0

    def timed(client: str) -> tuple[httpx.Response, float]:
        started = time.monotonic()
        answer = play(world, song, client)
        return answer, time.monotonic() - started

    with tolerance(world, 2.0), ThreadPoolExecutor(2) as pool:
        first = pool.submit(timed, "next-link-a")
        time.sleep(0.15)
        second = pool.submit(timed, "next-link-b")
        (one, one_took), (two, two_took) = first.result(), second.result()
    assert one.content == audio and two.content == audio
    assert two_took < one_took + 1.0  # not the first one's 2 s, then its own 2 s


def test_a_request_whose_borrowed_link_failed_shares_that_routing_s_answer(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The routing that found the link ends without audio: the request that used the link
    gets its answer, without asking the sources again."""
    [song], fakes, _ = release(world, "borrowed-fails", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].audio = world.audio("borrowed-fails-long", "flac", seconds=12)
    fakes["reliable"][0].first_byte_delay = 1.0
    fakes["checker"][0].available = False
    fakes["preparer"][0].available = False
    with tolerance(world, 2.0), ThreadPoolExecutor(2) as pool:
        first = pool.submit(play, world, song, "borrowed-a")
        time.sleep(0.15)
        second = pool.submit(play, world, song, "borrowed-b")
        answers = [first.result(), second.result()]
    assert [a.json()["subsonic-response"]["status"] for a in answers] == ["failed", "failed"]
    assert lines_with(playback_log, "(its answer is shared)")
    assert len(sources["preparer"].requests("resolve-isrc")) == 1
    assert len(sources["checker"].requests("resolve-isrc")) == 1


def _forget_pin(world: DeliveryWorld, song: str):  # type: ignore[no-untyped-def]
    async def forget() -> None:
        world.services.deliverer.forget(song)
        world.services.deliverer._known.pop(song, None)

    return forget


# --- errors cool an add-on down ----------------------------------------------------


def test_errors_in_a_row_cool_an_add_on_down_and_it_comes_back_quickly(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A run of HTTP 503 from the primary (its tokens used up for a while): after the third
    in a row it is passed over, so plays go to the others at once; after the cooldown it is
    asked again, and one more error cools it down again at once; once it delivers, it is
    back."""
    songs, fakes, audio = release(world, "errors", sources, count=7)
    for fake in fakes["primary"]:
        fake.stream_status = 503
    for n in range(3):
        assert play(world, songs[n]).content == audio[n]
    assert len(sources["primary"].requests("stream")) == 3
    [cooled] = lines_with(playback_log, "source Primary: 3 errors in a row")
    assert f"(the last: add-on error (HTTP 503)); passed over for {COOLDOWN:g}s" in cooled
    asked = len(sources["primary"].requests())
    assert play(world, songs[3]).content == audio[3]  # straight to the others
    assert len(sources["primary"].requests()) == asked
    [cooling] = lines_with(playback_log, "playing 'Title errors Song 4'")
    assert "Primary: cooling down" in cooling
    # After the cooldown it is asked again; a further error cools it down again at once.
    time.sleep(COOLDOWN + 0.2)
    assert play(world, songs[4]).content == audio[4]
    assert len(sources["primary"].requests("stream")) == 4
    assert lines_with(playback_log, "source Primary: 4 errors in a row")
    asked = len(sources["primary"].requests())
    assert play(world, songs[5]).content == audio[5]
    assert len(sources["primary"].requests()) == asked
    # It delivers again: back, its count cleared.
    time.sleep(COOLDOWN + 0.2)
    fakes["primary"][6].stream_status = None
    assert play(world, songs[6]).content == audio[6]
    assert lines_with(playback_log, "playing 'Title errors Song 7' from Primary")
    assert source(world, "Primary").stats.errors_since_success == 0


def test_a_delivery_between_errors_keeps_an_add_on_in_use(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Errors for some songs only (an add-on that fails a song now and then): a delivery in
    between clears the count, so it never cools down."""
    songs, fakes, audio = release(world, "some-errors", sources, count=5)
    for n in (0, 1, 3, 4):
        fakes["primary"][n].stream_status = 500
    for n in range(5):
        assert play(world, songs[n]).content == audio[n]
    assert lines_with(playback_log, "playing 'Title some-errors Song 3' from Primary")
    assert not lines_with(playback_log, "errors in a row")
    assert source(world, "Primary").stats.errors_since_success == 2
    assert len(sources["primary"].requests("stream")) == 5


def test_misses_refusals_and_slow_answers_are_no_errors(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    songs, fakes, audio = release(world, "no-errors", sources, count=3)
    fakes["primary"][0].available = False  # not available
    try:
        assert play(world, songs[0]).content == audio[0]
        sources["primary"].refuse_lookups = True  # HTTP 403
        assert play(world, songs[1]).content == audio[1]
        sources["primary"].refuse_lookups = False
        fakes["primary"][2].resolve_delay = BUDGET + 1  # a timeout
        assert play(world, songs[2]).content == audio[2]
    finally:
        sources["primary"].refuse_lookups = False
    assert source(world, "Primary").stats.errors_since_success == 0
    assert not lines_with(playback_log, "errors in a row")


def test_a_fallback_answering_errors_cools_down_too(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Not only the primary: the reliable source's errors pass it over too (its /stream
    answers HTTP 502 here; no other source has these songs, so it is tried each time)."""
    songs, fakes, _ = release(world, "fallback-errors", sources, count=4)
    for n in range(4):
        fakes["primary"][n].available = False
        fakes["reliable"][n].stream_status = 502
        fakes["checker"][n].available = False
        fakes["preparer"][n].available = False
    for n in range(3):
        assert play(world, songs[n]).json()["subsonic-response"]["status"] == "failed"
    assert lines_with(playback_log, "source Reliable: 3 errors in a row")
    asked = len(sources["reliable"].requests())
    play(world, songs[3])
    assert len(sources["reliable"].requests()) == asked


def test_the_error_count_is_a_setting(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    settings = world.services.deliverer.settings
    songs, fakes, audio = release(world, "error-setting", sources, count=4)
    for fake in fakes["primary"]:
        fake.stream_status = 503
    settings.cooldown_errors = 0  # off
    try:
        for n in range(3):
            assert play(world, songs[n]).content == audio[n]
        assert not lines_with(playback_log, "errors in a row")
        assert len(sources["primary"].requests("stream")) == 3
        settings.cooldown_errors = 1
        assert play(world, songs[3]).content == audio[3]
        assert lines_with(playback_log, "source Primary: 4 errors in a row")
    finally:
        settings.cooldown_errors = 3


def test_a_play_s_own_old_link_failing_does_not_cool_its_source_down(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A play pinned at the reliable source, which then answered two other
    songs with errors; the play's own (old) link answering HTTP 502 on a seek is not the
    add-on's third error - the seek gets a fresh link from it, the same file."""
    songs, fakes, audio = release(world, "own-link", sources, count=3)
    for fake in fakes["primary"]:
        fake.available = False
    client = world.client(client="own-link")
    assert client.request("stream", {"id": songs[0]}).content == audio[0]
    for n in (1, 2):
        fakes["reliable"][n].stream_status = 503
        assert play(world, songs[n]).content == audio[n]  # from the others
    reliable = source(world, "Reliable")
    assert reliable.stats.errors_since_success == 2
    fakes["reliable"][0].expire_after = 1  # its link, used once: now HTTP 502
    fakes["reliable"][0].expire_status = 502
    seek = client.request("stream", {"id": songs[0]}, headers={"Range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == audio[0][100:]
    assert reliable.stats.errors_since_success < 3
    assert not world.services.sources.cooling(reliable.id)
    # While it cools down (for whatever reason), the play still gets its fresh link
    # there - here the only source with its file.
    fakes["checker"][0].available = fakes["preparer"][0].available = False
    world.services.sources.cool_down(reliable.id, 60)

    async def expire() -> None:  # a paused play: its pin expired, its file known
        world.services.deliverer.forget(songs[0])

    world.server.call(expire)
    again = client.request("stream", {"id": songs[0]}, headers={"Range": "bytes=200-"})
    assert again.status_code == 206 and again.content == audio[0][200:]
    world.services.sources.cool_down(reliable.id, 0)


# --- a failed download-first's stream continues its routing ------------------------


def test_after_download_first_failed_the_stream_continues_its_routing(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A lower-bitrate play whose routing the wait cap ended while a slow
    worker was preparing the song (its first bytes, which tell whether it needs
    converting): download-first then continues that routing - the worker first, its preparation
    still going on - instead of asking the primary, the checks and the reliable source
    again, and Navidrome serves the file."""
    settings = world.services.deliverer.settings
    [song], fakes, [audio] = release(world, "resume", sources)
    fakes["primary"][0].stream_status = 503
    fakes["reliable"][0].resolve_delay = BUDGET + 2  # a timeout on its whole budget
    fakes["checker"][0].ready = False  # "not now": last
    fakes["preparer"][0].prepare_seconds = 4.0  # longer than the wait cap leaves it
    settings.max_wait_seconds = 5.0
    try:
        response = world.client(client="resume").request(
            "stream", {"id": song, "maxBitRate": "128"}
        )
    finally:
        settings.max_wait_seconds = 12.0
    assert response.status_code == 200 and response.content[:4] in (b"fLaC", b"OggS")
    assert len(response.content) >= len(audio) // 2  # Navidrome's (the tags are the song's)
    [failed] = lines_with(playback_log, "no audio for 'Title resume Song 1' (play) after 5.")
    assert "Preparer: timeout" in failed
    [line] = lines_with(playback_log, "fetched 'Title resume Song 1' from Preparer")
    assert re.search(r"continuing its routing of \d+\.\ds ago: Preparer, Checker;", line)
    assert "Reliable" not in line.split("continuing")[1]
    # Each asked once: the second routing asked none of them again.
    assert len(sources["primary"].requests("stream")) == 1
    assert len(sources["reliable"].requests("stream")) == 1
    assert len(sources["checker"].requests("availability")) == 1
    assert len(sources["preparer"].requests("stream")) == 2
    assert sources["checker"].requests("stream") == []


def test_a_continued_routing_with_nothing_left_fails_at_once(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Every source failed or lacked the song a moment ago: download-first and the stream
    after it fail at once instead of more full routings."""
    [song], fakes, _ = release(world, "resume-none", sources)
    fakes["primary"][0].stream_status = 503
    fakes["reliable"][0].stream_status = 502
    fakes["preparer"][0].available = False
    fakes["checker"][0].available = False
    response = world.client(client="resume").request("stream", {"id": song, "maxBitRate": "128"})
    body = response.json()["subsonic-response"]
    assert body["status"] == "failed" and "audio unavailable" in body["error"]["message"]
    lines = lines_with(playback_log, "continuing its routing of")
    assert lines
    for line in lines:
        assert re.match(
            r"no audio for 'Title resume-none Song 1' \((?:play|download)\) after 0\.\ds:", line
        )
        assert re.search(r"continuing its routing of \d+\.\ds ago: nothing left to try", line)
    assert "nothing left to try: each source failed or lacked it" in body["error"]["message"]
    assert len(sources["primary"].requests("stream")) == 1
    assert len(sources["reliable"].requests("stream")) == 1
    assert len(sources["checker"].requests("availability")) == 1


def test_a_client_s_retry_skips_the_sources_that_just_failed_for_the_song(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A client's retry of a song that failed a moment ago goes to the sources that did
    not fail for it (here the one that said "not now", last, which the client's wait cap
    cut short), not again to those that failed or lacked it - the primary, the reliable
    source that timed out, the worker that lacks it."""
    settings = world.services.deliverer.settings
    [song], fakes, [audio] = release(world, "retry", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].prepare_seconds = BUDGET + 1  # times out, preparing
    fakes["checker"][0].ready = False  # "not now": last, and cut short by the wait cap
    fakes["checker"][0].resolve_delay = 1.0
    fakes["preparer"][0].available = False
    settings.max_wait_seconds = BUDGET + 0.5
    try:
        first = play(world, song)
    finally:
        settings.max_wait_seconds = 12.0
    assert first.json()["subsonic-response"]["status"] == "failed"
    fakes["checker"][0].resolve_delay = 0.0
    streams = {name: len(addon.requests("stream")) for name, addon in sources.items()}
    lookups = {name: len(addon.requests("resolve-isrc")) for name, addon in sources.items()}
    time.sleep(1.2)
    assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title retry Song 1' from Checker")
    assert re.search(
        r"not asked again yet \(failed for this song\): Primary \d+s ago, Reliable \d+s ago,"
        r" Preparer \d+s ago",
        line,
    )
    for name in ("primary", "reliable", "preparer"):
        assert len(sources[name].requests("stream")) == streams[name], name
        assert len(sources[name].requests("resolve-isrc")) == lookups[name], name


def test_after_the_window_a_retry_tries_every_source_again(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The window is a setting (0: off): past it, the source that timed out while preparing
    the song is asked again, and has it ready now."""
    settings = world.services.deliverer.settings
    [song], fakes, [audio] = release(world, "retry-later", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].prepare_seconds = BUDGET + 1
    fakes["checker"][0].available = False
    fakes["preparer"][0].available = False
    settings.retry_skip_seconds = 0.5
    settings.max_wait_seconds = BUDGET + 0.5  # it is still preparing when apps stop waiting
    try:
        assert play(world, song).json()["subsonic-response"]["status"] == "failed"
        settings.max_wait_seconds = 12.0
        time.sleep(1.2)
        assert play(world, song).content == audio
    finally:
        settings.retry_skip_seconds = 60.0
        settings.max_wait_seconds = 12.0
    [line] = lines_with(playback_log, "playing 'Title retry-later Song 1' from Reliable")
    assert "not asked again yet" not in line


def test_a_retry_tries_the_source_the_wait_cap_cut_short(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The worker was still preparing the song when the client's wait cap
    ended the play - it did not fail, so the retry asks it (not those that failed), and it
    has the song ready now."""
    settings = world.services.deliverer.settings
    [song], fakes, [audio] = release(world, "retry-cut", sources)
    fakes["primary"][0].stream_status = 503
    fakes["reliable"][0].available = False
    fakes["checker"][0].available = False
    fakes["preparer"][0].prepare_seconds = 5.0  # its own budget is 6 s; the cap comes first
    settings.max_wait_seconds = 4.0
    try:
        assert play(world, song).json()["subsonic-response"]["status"] == "failed"
        time.sleep(1.2)
        assert play(world, song).content == audio
    finally:
        settings.max_wait_seconds = 12.0
    [line] = lines_with(playback_log, "playing 'Title retry-cut Song 1' from Preparer")
    assert re.search(r"not asked again yet \(failed for this song\): Primary \d+s ago,", line)
    assert "Preparer" not in line.split("not asked again yet")[1].split(";")[0]
    assert len(sources["primary"].requests("stream")) == 1


def test_a_second_download_first_of_a_song_continues_the_first_s_routing(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Two download-first fetches of one song at once (here two users'); the first one's
    routing fails (the wait cap cut the worker short): the second continues it - the worker
    first, its preparation still going on - instead of a full routing of its own. (Downloads
    ask one add-on at a time in the plan's order, without checks.)"""
    settings = world.services.deliverer.settings
    [song], fakes, _ = release(world, "two-fetches", sources)
    fakes["primary"][0].stream_status = 503
    fakes["reliable"][0].resolve_delay = BUDGET + 2  # a timeout on its whole budget
    fakes["checker"][0].available = False
    fakes["preparer"][0].prepare_seconds = 4.0
    settings.max_wait_seconds = 5.0
    download_first = world.services.download_first

    async def fetch(user: str) -> str | None:
        return await download_first.ensure(song, user)

    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(world.server.call, lambda: fetch("one"))
            time.sleep(0.3)
            second = pool.submit(world.server.call, lambda: fetch("two"))
            outcomes = [first.result(), second.result()]
    finally:
        settings.max_wait_seconds = 12.0
    assert outcomes[0] is not None and "Preparer: timeout" in outcomes[0]
    assert outcomes[1] is None  # delivered
    [line] = lines_with(playback_log, "fetched 'Title two-fetches Song 1' from Preparer")
    assert re.search(r"continuing its routing of \d+\.\ds ago: Preparer;", line)
    assert len(sources["primary"].requests("stream")) == 1
    assert len(sources["reliable"].requests("stream")) == 1
    assert sources["checker"].requests("availability") == []  # (a download asks no checks)


def test_a_primary_the_wait_cap_cut_short_is_continued_first(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The primary itself was preparing the song (its own budget longer than the client's
    wait cap): the routing that continues the one cut short (download-first's here) goes
    there first, not left out."""
    settings = world.services.deliverer.settings
    registry = world.services.sources
    [song], fakes, _ = release(world, "resume-primary", sources)
    primary = source(world, "Primary").id
    fakes["primary"][0].prepare_seconds = 5.4  # ready soon after the cap cut it
    for name in ("reliable", "checker", "preparer"):
        fakes[name][0].available = False
    world.server.call(lambda: registry.update(primary, budget_seconds=8.0))
    settings.max_wait_seconds = 5.0  # below the primary's own budget: the cap cuts it
    try:
        response = world.client(client="resume").request(
            "stream", {"id": song, "maxBitRate": "128"}
        )
    finally:
        settings.max_wait_seconds = 12.0
        world.server.call(lambda: registry.update(primary, clear_budget=True))
    assert response.status_code == 200 and response.content[:4] in (b"fLaC", b"OggS")
    [line] = lines_with(playback_log, "fetched 'Title resume-primary Song 1' from Primary")
    assert re.search(r"continuing its routing of \d+\.\ds ago: Primary", line)


def test_a_request_that_shared_the_failure_leaves_the_continuation_in_place(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A second request for the song waited while the download-first routing failed (the
    wait cap cut its worker short: an answer not shared), and routed it within what its own
    wait left; the download-first's stream still continues a routing - the worker first, its
    preparation still going on - and plays."""
    settings = world.services.deliverer.settings
    [song], fakes, _ = release(world, "resume-shared", sources)
    fakes["primary"][0].stream_status = 503
    fakes["reliable"][0].resolve_delay = BUDGET + 2
    fakes["checker"][0].ready = False
    fakes["preparer"][0].prepare_seconds = 4.0
    settings.max_wait_seconds = 5.0
    try:
        with ThreadPoolExecutor(2) as pool:
            fetch = pool.submit(
                lambda: world.client(client="resume").request(
                    "stream", {"id": song, "maxBitRate": "128"}
                )
            )
            time.sleep(0.3)
            other = pool.submit(play, world, song, "resume-other")
            answers = [fetch.result(), other.result()]
    finally:
        settings.max_wait_seconds = 12.0
    assert answers[0].status_code == 200 and answers[0].content[:4] in (b"fLaC", b"OggS")
    [line] = lines_with(playback_log, "fetched 'Title resume-shared Song 1' from Preparer")
    assert re.search(r"continuing its routing of \d+\.\ds ago: Preparer", line)


# --- the steps are not held up by the checks ---------------------------------------


def test_the_next_step_goes_on_without_waiting_for_slow_checks(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The primary lacks the song and says so at once; the Checker's check takes
    most of its timeout: the next step, the reliable source, plays before that check is
    in."""
    [song], fakes, [audio] = release(world, "slow-checks", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 0.4  # the worker's answer is in by the plan
    fakes["checker"][0].ready = False
    fakes["checker"][0].availability_delay = 2.5
    with slow_checks(world):
        started = time.monotonic()
        assert play(world, song).content == audio
        took = time.monotonic() - started
    assert lines_with(playback_log, "playing 'Title slow-checks Song 1' from Reliable")
    assert sources["checker"].requests("stream") == []
    assert took < 2.5, took


def test_a_primary_served_play_logs_no_unanswered_checks(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, [audio] = release(world, "served-checks", sources)
    fakes["checker"][0].availability_delay = 2.5
    with slow_checks(world):
        assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title served-checks Song 1' from Primary")
    assert "no answer yet" not in line and ": checks" not in line


def test_a_check_answered_later_still_hands_the_song_over(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The reliable source's step went on before the checks were in, slowly; once the
    Checker's check says it has the song ready (past the handover time), it takes over -
    the slow worker is not asked."""
    [song], fakes, [audio] = release(world, "late-ready", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 2.5
    fakes["reliable"][0].stream_status = 502
    fakes["checker"][0].ready = True
    fakes["checker"][0].availability_delay = 2.0
    with slow_checks(world):
        assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title late-ready Song 1' from Checker")
    assert "Reliable: stopped after" in line and "Checker: has it ready" in line
    assert sources["preparer"].requests("stream") == []


def test_a_late_not_now_stays_last(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A "not now" that comes while a step goes on puts that add-on after the others left
    (its place in the plan was before the worker's)."""
    [song], fakes, [audio] = release(world, "late-not-now", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 2.5  # the checks are in by its end
    fakes["reliable"][0].stream_status = 502
    fakes["checker"][0].ready = False
    fakes["checker"][0].stream_status = 503  # a stream there fails (and may start a job)
    fakes["checker"][0].availability_delay = 2.0
    with slow_checks(world):
        assert play(world, song).content == audio
    assert lines_with(playback_log, "playing 'Title late-not-now Song 1' from Preparer")
    assert sources["checker"].requests("stream") == []


def test_checks_answered_while_the_first_fallback_s_audio_fails_order_the_rest(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The checks outlive the reliable source's step: it hands over a link at once whose
    audio then answers HTTP 500; the Checker, whose check has not answered yet, keeps its
    place in the plan - next, before the slow worker - and plays."""
    [song], fakes, [audio] = release(world, "late-ready-audio", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 0.4  # the worker's answer is in by the plan
    fakes["reliable"][0].expire_after = 0  # every audio request of its links fails
    fakes["reliable"][0].expire_status = 500
    fakes["checker"][0].ready = True
    fakes["checker"][0].availability_delay = 1.5
    with slow_checks(world):
        assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title late-ready-audio Song 1' from Checker")
    assert "Reliable: HTTP 500" in line
    assert sources["preparer"].requests("stream") == []


def test_a_fallback_remembered_for_another_recording_is_not_counted_on(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The reliable source delivered another recording of the song: on the next play it is
    not asked for it at all (nor looked up)."""
    [song], fakes, [audio] = release(world, "wrong-preferred", sources)
    fakes["primary"][0].available = False
    fakes["checker"][0].ready = False
    fakes["reliable"][0].audio = world.audio("wrong-preferred-long", "flac", seconds=12)
    with tolerance(world, 2.0):
        assert play(world, song).content == audio
        assert lines_with(playback_log, "Reliable delivered another recording of")
        world.server.call(_forget_pin(world, song))
        looked = len(sources["reliable"].requests("resolve-isrc"))
        assert play(world, song).content == audio
    assert len(sources["reliable"].requests("resolve-isrc")) == looked
    line = lines_with(playback_log, "playing 'Title wrong-preferred Song 1' from ")[-1]
    assert "Reliable: another recording (remembered)" in line


def test_the_checks_order_holds_for_each_later_fallback(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Two links in a row whose audio answers HTTP 500, the checks answering meanwhile: the
    order they gave holds for every later step - the source that said "not now" never goes
    before the one that cannot tell."""
    later = world.addon("Later", resources=CHECKS)
    world.add_source(later)
    [song], fakes, [audio] = release(world, "order-holds", {**sources, "later": later})
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 2.5  # the checks are in by its end
    for name in ("reliable", "preparer"):
        fakes[name][0].expire_after = 0
        fakes[name][0].expire_status = 500
    fakes["checker"][0].ready = False
    fakes["checker"][0].stream_status = 503
    fakes["later"][0].ready = None  # it cannot tell
    for name in ("checker", "later"):
        fakes[name][0].availability_delay = 2.0
    with slow_checks(world):
        assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title order-holds Song 1' from Later")
    assert "Reliable: HTTP 500" in line and "Preparer: HTTP 500" in line
    assert sources["checker"].requests("stream") == []


# --- the recent attempts are kept across restarts ---------------------------------


def test_the_recent_attempts_are_kept_across_a_restart(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The add-ons' recent attempts (Add-ons and Diagnostics show them) are in Shijhon's
    database: a Shijhon started afterwards on that state has them (with their age). (The
    world's Shijhon still runs, and only one may on a state: the second starts on a copy of
    its database.)"""
    songs, fakes, audio = release(world, "kept", sources, count=2)
    for fake in fakes["primary"]:
        fake.available = False
    assert play(world, songs[0]).content == audio[0]
    [measured] = source(world, "Reliable").stats.recent
    assert measured.delivered
    world.server.call(world.services.sources.save_attempts)
    copied = world.tmp / "state-restarted"
    copied.mkdir()
    saved = sqlite3.connect(f"file:{world.app.configured.database_path}?mode=ro", uri=True)
    copy = sqlite3.connect(copied / world.app.configured.database_path.name)
    saved.backup(copy)
    saved.close()
    copy.close()
    settings = world.app.settings.model_copy(update={"state_dir": copied})
    again = ShijhonApp(settings, resolver=fake_resolver(world.dns))
    server = RunningServer(again)
    server.start()
    try:

        async def reliable() -> Source:
            assert again.services is not None
            found = await again.services.sources.enabled()
            return next(s for s in found if s.name == "Reliable")

        [kept] = server.call(reliable).stats.recent
        assert (kept.answer, kept.delivered, kept.seconds) == (
            measured.answer,
            measured.delivered,
            measured.seconds,
        )
        assert 0 <= time.monotonic() - kept.at < 60  # its age kept
    finally:
        server.stop()
