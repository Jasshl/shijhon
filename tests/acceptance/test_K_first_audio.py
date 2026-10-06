"""Suite K (continued) - first audio when the primary cannot serve.

- The delivered audio's real length is read from its first bytes and logged; audio whose
  length differs from the catalog's by more than a tolerance is another recording: not
  used, the next source is tried, and the add-on is not asked for it again for a while.
- The fallbacks are ordered by each add-on's recent attempts - the seconds its attempts
  took for each song it delivered, for the answer its check gave now - not by fixed rules:
  the configured preferred fallback and neutral estimates only until they are measured.
- After the primary's miss the likely fallback is looked up at once (from the start when
  the primary lacks another song of the release) and goes next as soon as it has the song,
  unless a check has said by then that a source has it ready: checks still running do not
  hold it up, and order the fallbacks after it once they are in (until then a source that
  has not answered counts as "not now").
- An attempt that ends with an add-on error uses no attempt (it takes no time; the budget
  still bounds the routing).
- The stream a failed download-first falls back to continues that routing: the source its
  wait cap cut short first, then those it did not try; never again those it is done with.
- An add-on answering errors (an HTTP 5xx, no connection, a broken answer) cools down after
  a few in a row without a delivery in between (a setting, 3; 0: off), so plays go to the
  other sources at once; until it delivers again each further error cools it down again,
  for the short cooldown only, so it comes back quickly. Misses, refusals and slow answers
  do not count.
- A request that waited for another request's routing of the same song (a client's probe
  and its play at once) gets its first byte with a budget of its own: the wait used up its
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
from shijhon.delivery.sources import Attempt, Source
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack, fake_resolver
from tests.harness.running import RunningServer

CHECKS = ("stream", "isrc", "availability")
BUDGET = 3.0
OWN_BUDGET = 6.0  # the preparing source's own budget
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
        routing="primary_first",
        primary_source="Primary",
        primary_budget_seconds=1.0,
        reliable_source="Reliable",
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
        world.add_source(addon, budget_seconds=OWN_BUDGET if name == "preparer" else None)
    yield addons
    world.clear_sources()


def _forget(world: DeliveryWorld):  # type: ignore[no-untyped-def]
    async def forget() -> None:
        deliverer = world.services.deliverer
        deliverer._misses.clear()
        deliverer._release_misses.clear()
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


def plan_names(line: str) -> list[str]:
    """The sources of a routing's plan line, in order (each shown with its seconds a song
    and its check's answer)."""
    return re.findall(r"(?:plan |\), )([^(]+?) \(", line)


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
    """Ordered routing (concurrent requests share one lookup): the source found has the
    whole byte-zero budget, as the next source has a budget of its own; the request that
    waited for it gets the same, not half of it."""
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
    settings.routing = "ordered"
    settings.budget_seconds = 6.0
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(play, world, song, "terms-a")
            time.sleep(0.2)
            second = pool.submit(play, world, song, "terms-b")
            answers = [first.result(), second.result()]
    finally:
        settings.routing = "primary_first"
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
        assert lines_with(playback_log, "Primary delivered another recording of it (remembered)")


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
    """Another recording uses no attempt - the routing goes on to the
    next source within the budget, here the third one after two (the setting)."""
    [song], fakes, [audio] = release(world, "wrong-goes-on", sources)
    fakes["primary"][0].available = False
    long = world.audio("wrong-goes-on-long", "flac", seconds=12)
    fakes["reliable"][0].audio = long
    fakes["checker"][0].resolve_delay = BUDGET + 2  # cannot tell; its link times out
    assert world.services.deliverer.settings.max_attempts == 2
    with tolerance(world, 2.0):
        assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title wrong-goes-on Song 1' from Preparer")
    assert "Reliable: another recording (12.0s, the catalog's 3.0s)" in line
    assert "Checker: timeout" in line


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
    borrowed = lines_with(playback_log, "(another request's link, then")
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


def test_a_request_whose_borrowed_link_timed_out_routes_on_as_before(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Its own share of the budget ran out on a link another request still reads (a slow
    first byte): that timeout is its own, not the link's - it goes on to the next source
    at once, not after the other request's routing."""
    settings = world.services.deliverer.settings
    [song], fakes, [audio] = release(world, "own-timeout", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].available = False
    fakes["checker"][0].ready = False  # "not now": last in the plan
    fakes["preparer"][0].first_byte_delay = 4.5
    saved, settings.max_wait_seconds = settings.max_wait_seconds, 4.0
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(play, world, song, "own-timeout-a")
            time.sleep(1.0)
            began = time.monotonic()
            second = pool.submit(play, world, song, "own-timeout-b")
            answers = [first.result(), second.result()]
            took = time.monotonic() - began
    finally:
        settings.max_wait_seconds = saved
    assert answers[1].content == audio
    assert took < 3.5  # not after the first one's routing (cut at 4 s)


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
    assert lines_with(playback_log, "the routing that found that link ended without audio")
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
    assert lines_with(playback_log, "primary-first 'Title errors Song 4': Primary is cooling down")
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
    world.services.deliverer.settings.primary_release_miss_minutes = 0
    try:
        assert play(world, songs[0]).content == audio[0]
        sources["primary"].refuse_lookups = True  # HTTP 403
        assert play(world, songs[1]).content == audio[1]
        sources["primary"].refuse_lookups = False
        fakes["primary"][2].resolve_delay = BUDGET + 1  # a timeout
        assert play(world, songs[2]).content == audio[2]
    finally:
        world.services.deliverer.settings.primary_release_miss_minutes = 60.0
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
    world.services.deliverer.settings.primary_release_miss_minutes = 0
    try:
        for n in range(3):
            assert play(world, songs[n]).json()["subsonic-response"]["status"] == "failed"
        assert lines_with(playback_log, "source Reliable: 3 errors in a row")
        asked = len(sources["reliable"].requests())
        play(world, songs[3])
        assert len(sources["reliable"].requests()) == asked
    finally:
        world.services.deliverer.settings.primary_release_miss_minutes = 60.0


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
    settings = world.services.deliverer.settings
    songs, fakes, audio = release(world, "own-link", sources, count=3)
    for fake in fakes["primary"]:
        fake.available = False
    settings.primary_release_miss_minutes = 0
    try:
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
    finally:
        settings.primary_release_miss_minutes = 60.0


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
    [line] = lines_with(playback_log, "playing 'Title resume Song 1' from Preparer")
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
        assert re.match(r"no audio for 'Title resume-none Song 1' \(play\) after 0\.\ds:", line)
        assert re.search(r"continuing its routing of \d+\.\ds ago: nothing left to try", line)
    assert "nothing left to try: each source failed or lacked it" in body["error"]["message"]
    assert len(sources["primary"].requests("stream")) == 1
    assert len(sources["reliable"].requests("stream")) == 1
    assert len(sources["checker"].requests("availability")) == 1


def test_a_client_s_retry_skips_the_sources_that_just_failed_for_the_song(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A client's retry of a song that failed a moment ago goes to the sources that did
    not fail for it (here the one that said "not now" and got no time), not again to those
    that failed or lacked it - the primary, the reliable source that timed out, the worker
    that lacks it."""
    [song], fakes, [audio] = release(world, "retry", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].prepare_seconds = BUDGET + 1  # times out, preparing
    fakes["checker"][0].ready = False  # "not now": no time left for it
    fakes["preparer"][0].available = False
    first = play(world, song)
    assert first.json()["subsonic-response"]["status"] == "failed"
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
    fakes["checker"][0].ready = False
    fakes["preparer"][0].available = False
    settings.retry_skip_seconds = 0.5
    try:
        assert play(world, song).json()["subsonic-response"]["status"] == "failed"
        time.sleep(1.2)
        assert play(world, song).content == audio
    finally:
        settings.retry_skip_seconds = 60.0
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
    first, its preparation still going on - instead of a full routing of its own."""
    settings = world.services.deliverer.settings
    [song], fakes, _ = release(world, "two-fetches", sources)
    fakes["primary"][0].stream_status = 503
    fakes["reliable"][0].resolve_delay = BUDGET + 2  # a timeout on its whole budget
    fakes["checker"][0].ready = False
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
    [line] = lines_with(playback_log, "playing 'Title two-fetches Song 1' from Preparer")
    assert re.search(r"continuing its routing of \d+\.\ds ago: Preparer, Checker;", line)
    assert len(sources["primary"].requests("stream")) == 1
    assert len(sources["reliable"].requests("stream")) == 1
    assert len(sources["checker"].requests("availability")) == 1


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
    [line] = lines_with(playback_log, "playing 'Title resume-primary Song 1' from Primary")
    assert re.search(r"continuing its routing of \d+\.\ds ago: Primary", line)


def test_a_request_that_shared_the_failure_leaves_the_continuation_in_place(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A second request for the song waited while the download-first routing failed and got
    its answer at once: it tried nothing, so the continuation the download-first's stream
    needs stays as that routing left it."""
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
    assert lines_with(playback_log, "its answer is shared")
    [line] = lines_with(playback_log, "playing 'Title resume-shared Song 1' from Preparer")
    assert "continuing its routing of" in line and ": Preparer, Checker" in line


# --- the reliable source is not held up by the checks ------------------------------


def test_the_reliable_source_goes_next_without_waiting_for_slow_checks(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The primary lacks the song and says so at once; the Checker's check takes
    most of its timeout: the reliable source is looked up at the miss and plays before that
    check is in - which then counts as "not now" (last)."""
    [song], fakes, [audio] = release(world, "slow-checks", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 0.4  # the worker's answer is in by the plan
    fakes["checker"][0].ready = False
    fakes["checker"][0].availability_delay = 2.5
    with slow_checks(world):
        started = time.monotonic()
        assert play(world, song).content == audio
        took = time.monotonic() - started
    [line] = lines_with(playback_log, "playing 'Title slow-checks Song 1' from Reliable")
    assert "Reliable: looked up meanwhile (from " in line
    assert "Checker no answer yet" in line
    [plan] = lines_with(playback_log, "primary-first 'Title slow-checks Song 1'")
    assert plan_names(plan) == ["Reliable", "Preparer", "Checker"]
    assert "Checker (~60.0s, no answer yet)" in plan
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


def test_checks_answered_later_order_the_fallbacks_after_the_reliable_source(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The reliable source went first before the checks were in, and fails; by then the
    Checker's check has said it has the song ready: it goes next, before the
    slow worker (as it would have with every answer in)."""
    [song], fakes, [audio] = release(world, "late-ready", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 0.4  # the worker's answer is in by the plan
    fakes["reliable"][0].stream_status = 502
    fakes["checker"][0].ready = True
    fakes["checker"][0].availability_delay = 2.0
    with slow_checks(world):
        assert play(world, song).content == audio
    [plan] = lines_with(playback_log, "primary-first 'Title late-ready Song 1'")
    assert plan_names(plan) == ["Reliable", "Preparer", "Checker"]  # before the Checker's answer
    assert lines_with(playback_log, "playing 'Title late-ready Song 1' from Checker")
    assert sources["preparer"].requests("stream") == []


def test_a_late_not_now_stays_last(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, [audio] = release(world, "late-not-now", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].stream_status = 502
    fakes["checker"][0].ready = False
    fakes["checker"][0].stream_status = 503  # a stream there fails (and may start a job)
    fakes["checker"][0].availability_delay = 2.0
    with slow_checks(world):
        assert play(world, song).content == audio
    assert lines_with(playback_log, "playing 'Title late-not-now Song 1' from Preparer")
    assert sources["checker"].requests("stream") == []


def test_after_a_release_miss_the_reliable_source_is_looked_up_at_once(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The primary lacks another song of the release (its own attempt at once):
    meanwhile the reliable source is looked up from the start, so when the primary lacks
    this song too, the reliable source goes on at once."""
    songs, fakes, audio = release(world, "release-lookup", sources, count=2)
    for fake in fakes["primary"]:
        fake.available = False
    assert play(world, songs[0]).content == audio[0]
    assert play(world, songs[1]).content == audio[1]
    [line] = lines_with(playback_log, "playing 'Title release-lookup Song 2' from Reliable")
    assert "lacks a song of its release (remembered)" in line
    assert re.search(r"Reliable: looked up meanwhile \d+\.\ds;", line)  # from the start
    assert "Primary: not available" in line
    assert len(sources["reliable"].requests("resolve-isrc")) == 2  # once a song


def test_an_add_on_error_uses_no_attempt(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Two attempts (the setting): the Checker, which has the song ready, answers
    HTTP 500 at once, the reliable source then times out - the slow worker still gets its
    turn, as the error took none."""
    [song], fakes, [audio] = release(world, "error-attempt", sources)
    fakes["primary"][0].available = False
    fakes["checker"][0].ready = True
    fakes["checker"][0].stream_status = 500
    fakes["reliable"][0].resolve_delay = BUDGET + 1
    assert world.services.deliverer.settings.max_attempts == 2
    assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title error-attempt Song 1' from Preparer")
    assert "Checker: add-on error (HTTP 500)" in line and "Reliable: timeout" in line


# --- fallbacks ordered by their recent attempts ------------------------------------


def one_song(
    world: DeliveryWorld, key: str, sources: dict[str, FakeAddon]
) -> tuple[str, dict[str, FakeTrack], bytes]:
    [song], fakes, [audio] = release(world, key, sources)
    return song, {name: tracks[0] for name, tracks in fakes.items()}, audio


def test_a_preferred_fallback_that_keeps_lacking_songs_goes_after_one_that_delivers(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The configured fallback comes first only until measured: after three songs it lacked
    (each costing its lookup) and another source delivered, that one goes first."""
    for n in range(3):
        song, fakes, audio = one_song(world, f"measured-{n}", sources)
        fakes["primary"].available = False
        fakes["reliable"].available = False
        fakes["reliable"].lookup_delay = 0.3
        fakes["checker"].ready = False
        assert play(world, song).content == audio
        assert lines_with(playback_log, f"playing 'Title measured-{n} Song 1' from Preparer")
    song, fakes, audio = one_song(world, "measured-3", sources)
    fakes["primary"].available = False
    fakes["primary"].lookup_delay = 0.6  # the checks are in by its miss
    fakes["checker"].ready = False
    assert play(world, song).content == audio
    [plan] = lines_with(playback_log, "primary-first 'Title measured-3 Song 1'")
    assert plan_names(plan)[:2] == ["Preparer", "Reliable"]
    assert re.search(r"Preparer \(\d+\.\ds\)", plan)  # measured: no "~"
    assert lines_with(playback_log, "playing 'Title measured-3 Song 1' from Preparer")


def test_a_ready_source_whose_streams_fail_loses_its_first_place(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Measured, not built in: a source whose check says it has the song ready goes first
    only while its attempts deliver (on the day of use one such source failed 10 of 24):
    once its stream failed and another source delivered, that one goes first - also when
    the ready source works again, until its deliveries make it the quicker one."""
    settings = world.services.deliverer.settings
    saved = settings.cooldown_errors
    settings.cooldown_errors = 0  # its errors alone would pass it over for a while
    try:
        song, fakes, audio = one_song(world, "ready-fails-0", sources)
        fakes["primary"].available = False
        fakes["primary"].lookup_delay = 0.6  # the checks are in by its miss
        fakes["checker"].ready = True
        fakes["checker"].stream_status = 500
        assert play(world, song).content == audio
        [plan] = lines_with(playback_log, "primary-first 'Title ready-fails-0 Song 1'")
        assert plan_names(plan)[0] == "Checker"  # estimated: ready goes first
        assert lines_with(playback_log, "playing 'Title ready-fails-0 Song 1' from Reliable")
        song, fakes, audio = one_song(world, "ready-fails-1", sources)
        fakes["primary"].available = False
        fakes["primary"].lookup_delay = 0.6
        fakes["checker"].ready = True
        assert play(world, song).content == audio
        [plan] = lines_with(playback_log, "primary-first 'Title ready-fails-1 Song 1'")
        assert plan_names(plan)[:2] == ["Reliable", "Checker"]
        assert re.search(r"Reliable \(\d+\.\ds\), Checker \(\d+\.\ds, ready\)", plan)  # measured
    finally:
        settings.cooldown_errors = saved


def test_checks_answered_while_the_first_fallback_s_audio_fails_order_the_rest(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The checks outlive the first fallback's link: the reliable source hands over a link
    at once whose audio then answers HTTP 500; the Checker's check, still
    running then, says it has the song ready - it goes next, before the slow worker."""
    [song], fakes, [audio] = release(world, "late-ready-audio", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 0.4  # the worker's answer is in by the plan
    fakes["reliable"][0].expire_after = 0  # every audio request of its links fails
    fakes["reliable"][0].expire_status = 500
    fakes["checker"][0].ready = True
    fakes["checker"][0].availability_delay = 1.5
    with slow_checks(world):
        assert play(world, song).content == audio
    [plan] = lines_with(playback_log, "primary-first 'Title late-ready-audio Song 1'")
    assert plan_names(plan)[0] == "Reliable"  # before the Checker's answer
    [line] = lines_with(playback_log, "playing 'Title late-ready-audio Song 1' from Checker")
    assert "Reliable: HTTP 500" in line
    assert sources["preparer"].requests("stream") == []


def test_an_add_on_error_at_the_audio_uses_no_attempt(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Two attempts (the setting): a source that has the song ready hands over a link whose
    audio answers HTTP 500, the reliable source then times out - the slow worker still gets
    its turn, as the error took none."""
    [song], fakes, [audio] = release(world, "audio-error-attempt", sources)
    fakes["primary"][0].available = False
    fakes["primary"][0].lookup_delay = 0.6  # the checks are in by its miss
    fakes["checker"][0].ready = True
    fakes["checker"][0].expire_after = 0
    fakes["checker"][0].expire_status = 500
    fakes["reliable"][0].resolve_delay = BUDGET + 1
    assert world.services.deliverer.settings.max_attempts == 2
    assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title audio-error-attempt Song 1' from Preparer")
    assert "Checker: HTTP 500" in line and "Reliable: timeout" in line


def test_a_lookup_made_meanwhile_counts_in_its_source_s_attempt(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The reliable source's lookup at the primary's miss gave the track ID its attempt
    used: its seconds are that attempt's too (a source slow to find songs is measured so,
    not only by its link and first byte)."""
    [song], fakes, [audio] = release(world, "lookup-counted", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 0.6
    fakes["checker"][0].ready = False
    assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title lookup-counted Song 1' from Reliable")
    assert "Reliable: looked up meanwhile" in line
    [attempt] = source(world, "Reliable").stats.recent
    assert attempt.delivered and attempt.seconds >= 0.6, attempt


def test_a_fallback_that_lacked_the_song_meanwhile_is_not_asked_again(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, [audio] = release(world, "lacked-meanwhile", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].available = False
    fakes["checker"][0].ready = False
    assert play(world, song).content == audio
    [line] = lines_with(playback_log, "playing 'Title lacked-meanwhile Song 1' from Preparer")
    assert "Reliable: not available (looked up meanwhile" in line
    assert len(sources["reliable"].requests("resolve-isrc")) == 1  # one lookup, not two


def test_a_fallback_remembered_for_another_recording_is_not_counted_on(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The preferred fallback delivered another recording of the song: on the next play it
    counts as "not now" (last, not looked up meanwhile), like a checked source would."""
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
    plan = lines_with(playback_log, "primary-first 'Title wrong-preferred Song 1'")[-1]
    assert plan_names(plan) == ["Preparer", "Reliable", "Checker"]  # "not now": by position
    assert "Reliable (~60.0s, not now)" in plan


def test_not_now_stays_last_behind_a_source_measured_as_slow(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Measurements order the fallbacks, but a source that said "not now" still comes after
    every other (a stream there most likely fails and can start a preparation job) -
    also after one whose recent attempts took far longer than the "not now" estimate."""
    reliable = source(world, "Reliable")
    for _ in range(12):  # 108 s without a delivery: about 112 s a song
        reliable.stats.recent.append(Attempt(time.monotonic(), "-", False, 9.0))
    [song], fakes, [audio] = release(world, "not-now-last", sources)
    fakes["primary"][0].available = False
    fakes["primary"][0].lookup_delay = 0.6  # the checks are in by its miss
    fakes["checker"][0].ready = False
    assert play(world, song).content == audio
    [plan] = lines_with(playback_log, "primary-first 'Title not-now-last Song 1'")
    assert plan_names(plan) == ["Preparer", "Reliable", "Checker"]


def test_an_audio_error_gives_back_only_its_own_attempt(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Two attempts (the setting): the ready source fails (an attempt), the reliable one's
    audio answers HTTP 500 (its own attempt given back, not the ready source's), the worker's
    audio fails (the second attempt) - so the last source is not tried."""
    later = world.addon("Later")
    world.add_source(later)
    [song], fakes, _ = release(world, "own-refund", {**sources, "later": later})
    fakes["primary"][0].lookup_delay = 0.6  # the checks are in by its miss
    fakes["primary"][0].available = False
    fakes["checker"][0].ready = True
    fakes["checker"][0].stream_status = 400  # not an add-on error: an attempt
    fakes["reliable"][0].expire_after = 0
    fakes["reliable"][0].expire_status = 500
    fakes["preparer"][0].expire_after = 0
    fakes["preparer"][0].expire_status = 404
    answer = play(world, song)
    assert answer.json()["subsonic-response"]["status"] == "failed"
    [line] = lines_with(playback_log, "no audio for 'Title own-refund Song 1'")
    assert "attempts used up (2 of 2; not tried: Later)" in line
    assert later.requests("stream") == []
    assert len(sources["checker"].requests("stream")) == 1  # not asked again by a later pass


def test_the_checks_order_holds_for_each_later_fallback(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """Two links in a row whose audio answers HTTP 500, the checks answering meanwhile: the
    order they gave holds for every later pass - the source that said "not now" never goes
    before the one that cannot tell."""
    later = world.addon("Later", resources=CHECKS)
    world.add_source(later)
    [song], fakes, [audio] = release(world, "order-holds", {**sources, "later": later})
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = 0.4  # the worker's answer is in by the plan
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
    [plan] = lines_with(playback_log, "primary-first 'Title order-holds Song 1'")
    assert plan_names(plan) == ["Reliable", "Preparer", "Checker", "Later"]  # before the answers
    [line] = lines_with(playback_log, "playing 'Title order-holds Song 1' from Later")
    assert "Reliable: HTTP 500" in line and "Preparer: HTTP 500" in line
    assert sources["checker"].requests("stream") == []


def test_a_lookup_that_timed_out_meanwhile_is_its_source_s_attempt(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The likely fallback's lookup at the primary's miss is its attempt's start: bounded by
    the budget, and when it times out that is its attempt - not a second lookup with a
    fresh share after it."""
    [song], fakes, [audio] = release(world, "lookup-timeout", sources)
    fakes["primary"][0].available = False
    fakes["reliable"][0].lookup_delay = BUDGET + 1
    fakes["checker"][0].ready = False
    started = time.monotonic()
    assert play(world, song).content == audio
    took = time.monotonic() - started
    [line] = lines_with(playback_log, "playing 'Title lookup-timeout Song 1' from Preparer")
    assert "Reliable: timeout (looked up meanwhile" in line
    assert len(sources["reliable"].requests("resolve-isrc")) == 1
    assert took < 2 * BUDGET, took  # one lookup's budget, not two


def test_a_lookup_that_timed_out_meanwhile_uses_an_attempt(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """One attempt (here): the reliable source's lookup at the primary's miss took the whole
    budget - that was its attempt, so the worker (its own budget) is not tried after it."""
    settings = world.services.deliverer.settings
    saved = settings.max_attempts
    settings.max_attempts = 1
    try:
        [song], fakes, _ = release(world, "timeout-attempt", sources)
        fakes["primary"][0].available = False
        fakes["reliable"][0].lookup_delay = BUDGET + 1
        fakes["checker"][0].ready = False
        answer = play(world, song)
    finally:
        settings.max_attempts = saved
    assert answer.json()["subsonic-response"]["status"] == "failed"
    [line] = lines_with(playback_log, "no audio for 'Title timeout-attempt Song 1'")
    assert "Reliable: timeout (looked up meanwhile" in line
    assert "attempts used up (1 of 1; not tried: Preparer" in line
    assert sources["preparer"].requests("resolve-isrc") == []


def test_a_worker_s_own_budget_counts_from_its_lookup_meanwhile(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The worker is the likely fallback (the reliable source measured as slow): its lookup
    at the primary's miss is the start of its attempt, so its own budget (6 s) counts from
    then - 4 s of lookup leave 2 s for a link that takes 3 s."""
    reliable = source(world, "Reliable")
    for _ in range(12):  # about 112 s a song
        reliable.stats.recent.append(Attempt(time.monotonic(), "-", False, 9.0))
    [song], fakes, _ = release(world, "own-from-lookup", sources)
    fakes["primary"][0].available = False
    fakes["primary"][0].lookup_delay = 0.6  # the checks are in by its miss
    fakes["preparer"][0].lookup_delay = 4.0
    fakes["preparer"][0].resolve_delay = 3.0
    fakes["checker"][0].ready = False
    started = time.monotonic()
    play(world, song)
    took = time.monotonic() - started
    [line] = [
        *lines_with(playback_log, "no audio for 'Title own-from-lookup Song 1'"),
        *lines_with(playback_log, "playing 'Title own-from-lookup Song 1'"),
    ]
    assert "Preparer: looked up meanwhile" in line and "Preparer: timeout" in line
    assert took < OWN_BUDGET + 1.5, took  # not 4 s of lookup plus a fresh 6 s


# --- the measurements are kept across restarts ------------------------------------


def test_the_measured_order_is_kept_across_a_restart(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The fallbacks' measurements are in Shijhon's database: a Shijhon started afterwards
    on that state has them (with their age), not the estimates again. (The world's Shijhon
    still runs, and only one may on a state: the second starts on a copy of its
    database.)"""
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
