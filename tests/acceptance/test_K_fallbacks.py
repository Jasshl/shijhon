"""Suite K (continued) - plays the plan's first add-on cannot serve.

A plan of four steps (Primary, Reliable, Checker, Preparer), with ready checks; after the
first add-on's miss:

- each later step has its whole time (a step's own, or the common one), whatever the
  steps before it took, and the add-ons asked before keep trying beside it;
- add-ons that said they cannot deliver the recording now are tried last (a stream there
  most likely fails, and can start a preparation job);
- an add-on's miss is remembered for the recording (later requests go to the other
  add-ons at once) for a while (a setting, 0: off) - not for its release: another song of
  it is asked of it as usual;
- a request that waited while another request's routing of the same song failed gets
  that answer at once instead of repeating the routing;
- a play that ends without audio is logged with the reason and each step's source and
  time, also when the client left before its first byte; a play a later step served is
  logged with the steps before it.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from shijhon.delivery.download_first import track_of
from shijhon.delivery.playback import NoSource
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack

CHECKS = ("stream", "isrc", "availability")
BUDGET = 3.0
OWN_BUDGET = 6.0  # the preparing source's own times (its step's and a download's)


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("fallbacks"),
        budget_seconds=BUDGET,
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
    world.server.call(_forget_misses(world))
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


def _forget_misses(world: DeliveryWorld):  # type: ignore[no-untyped-def]
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


def play(world: DeliveryWorld, song: str, client: str = "fallback-tests") -> httpx.Response:
    return world.client(client=client).request("stream", {"id": song})


def missing_at_primary(
    world: DeliveryWorld, key: str, sources: dict[str, FakeAddon], count: int = 1
) -> tuple[list[str], dict[str, list[FakeTrack]], list[bytes]]:
    """A release of ``count`` songs the primary does not have; every source gets them.
    Returns (song IDs, each source's fake tracks, each song's audio)."""
    release = catalog_release(key, f"Title {key}", f"Artist {key}", count)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    fakes: dict[str, list[FakeTrack]] = {name: [] for name in sources}
    audio: list[bytes] = []
    for track in release.tracks:
        path = world.audio(f"{key}-{track.number}")
        audio.append(path.read_bytes())
        assert track.isrc is not None
        for name, addon in sources.items():
            fake = addon.add(FakeTrack(isrc=track.isrc, audio=path))
            fake.available = name != "primary"
            fakes[name].append(fake)
    return songs, fakes, audio


def lines_with(lines: list[str], prefix: str) -> list[str]:
    return [line for line in lines if line.startswith(prefix)]


@contextlib.contextmanager
def waiting(world: DeliveryWorld, seconds: float) -> Iterator[None]:
    """Apps wait ``seconds`` for a song's first audio (an add-on asked keeps trying until
    then)."""
    settings = world.services.deliverer.settings
    before = settings.max_wait_seconds
    settings.max_wait_seconds = seconds
    try:
        yield
    finally:
        settings.max_wait_seconds = before


def test_a_later_step_has_its_whole_time(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The reliable source's step has the whole common time (its link takes most of
    it), whatever the step before it took and the next one's own time."""
    [song], fakes, [audio] = missing_at_primary(world, "whole-budget", sources)
    fakes["reliable"][0].resolve_delay = BUDGET * 0.7
    fakes["checker"][0].ready = False
    response = play(world, song)
    assert response.content == audio
    assert sources["preparer"].requests("stream") == []
    [line] = lines_with(playback_log, "playing 'Title whole-budget Song 1' from Reliable")
    assert "Primary: not available" in line and "Checker not now" in line
    assert re.search(r"Reliable: lookup 0\.\ds, link 2\.\ds, first byte", line)


def test_sources_that_said_not_now_come_last(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, [audio] = missing_at_primary(world, "not-now", sources)
    fakes["reliable"][0].resolve_delay = BUDGET + 1  # slower than its step
    fakes["checker"][0].ready = False
    fakes["checker"][0].stream_status = 503  # a stream there fails (and may start a job)
    response = play(world, song)
    assert response.content == audio
    assert sources["checker"].requests("stream") == []  # never streamed
    assert sources["preparer"].requests("stream")
    [line] = lines_with(playback_log, "playing 'Title not-now Song 1' from Preparer")
    # It kept trying beside the next step, and was slow when that one's audio came.
    assert "Reliable: stopped after" in line and "slow: counted" in line


def test_a_play_that_ends_without_audio_is_logged_with_its_steps(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, _ = missing_at_primary(world, "no-audio", sources)
    # It is slow, then fails (4 s, an error), after the checker, which said not now, joined
    # once the preparer's step had ended - all before the wait ends.
    fakes["reliable"][0].resolve_delay = 4.0
    fakes["reliable"][0].stream_status = 502
    fakes["checker"][0].ready = False
    fakes["checker"][0].stream_status = 503
    fakes["preparer"][0].available = False
    wait = 2 * BUDGET + 1
    started = time.monotonic()
    with waiting(world, wait):
        response = play(world, song)
    took = time.monotonic() - started
    body = response.json()["subsonic-response"]
    assert body["status"] == "failed" and "audio unavailable" in body["error"]["message"]
    assert took < 4.0 + 1.0  # the reliable source kept trying until it failed
    [line] = lines_with(playback_log, "no audio for 'Title no-audio Song 1' (play) after ")
    assert ": no source delivered; " in line
    for step in (
        "Primary: not available",
        "checks",
        "Checker not now",
        "Reliable: add-on error (HTTP 502)",
        "Preparer: not available",
        "Checker: ",  # said not now: last
    ):
        assert step in line, step
    assert line.index("Preparer: not available") < line.rindex("Checker: ")


def test_a_rejected_resolve_match_is_named(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, _ = missing_at_primary(world, "rejected", sources)
    fakes["primary"][0].resolve_item = {
        "id": "other", "title": "Title rejected Song 1", "artist": "Artist rejected",
        "durationMs": 99_000,
    }  # fmt: skip
    for name in ("reliable", "checker", "preparer"):
        for fake in fakes[name]:
            fake.available = False
    play(world, song)
    [line] = lines_with(playback_log, "no audio for 'Title rejected Song 1' (play)")
    assert ": no source had it (a match was rejected); " in line
    assert "Primary: not available (a match was rejected: its length differs)" in line


def test_a_miss_is_remembered_for_the_recording_and_not_its_release(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    songs, _, audio = missing_at_primary(world, "remembered", sources, count=3)
    assert play(world, songs[0]).content == audio[0]
    asked = len(sources["primary"].requests("resolve-isrc"))
    assert asked == 1
    world.server.call(lambda: _forget_pin(world, songs[0]))
    assert play(world, songs[0]).content == audio[0]  # the recording: remembered
    assert len(sources["primary"].requests("resolve-isrc")) == asked
    routing = world.services.deliverer.recent[-1]
    assert "Primary: did not have it (remembered)" in routing.steps
    # Another song of its release: asked as usual.
    assert play(world, songs[1]).content == audio[1]
    assert len(sources["primary"].requests("resolve-isrc")) == asked + 1
    asked += 1
    # Off: the primary is asked again.
    settings = world.services.deliverer.settings
    settings.miss_hours = 0
    try:
        world.server.call(_forget_misses(world))
        assert play(world, songs[2]).content == audio[2]
        world.server.call(lambda: _forget_pin(world, songs[2]))
        assert play(world, songs[2]).content == audio[2]
    finally:
        settings.miss_hours = 24.0
    assert len(sources["primary"].requests("resolve-isrc")) == asked + 2


def test_a_release_the_primary_lacks_one_song_of_stays_playable_there(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The primary lacks one song of a release; another song of it that the primary
    has is played from the primary - before the reliable worker and a slow one with its own
    time - not left to them (a miss is the recording's, not the release's)."""
    songs, fakes, audio = missing_at_primary(world, "hinted", sources, count=2)
    fakes["primary"][1].available = True  # it has the second song
    assert play(world, songs[0]).content == audio[0]  # its miss: remembered
    assert play(world, songs[1]).content == audio[1]
    [served] = lines_with(playback_log, "playing 'Title hinted Song 2' from Primary")
    assert served
    assert sources["preparer"].requests("stream") == []
    assert not [r for r in sources["reliable"].requests("stream")
                if r["key"] == fakes["reliable"][1].key()]  # fmt: skip


def test_after_a_slow_primary_tried_later_the_reliable_source_has_its_whole_budget(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The primary, asked for another song of a release it lacks a song of, takes its
    own attempt (here 2 s to say it lacks this one too); the reliable source's step then has
    its whole time (its link takes 2 s of the 3 s), not what the primary left of it."""
    songs, fakes, audio = missing_at_primary(world, "slow-hint", sources, count=2)
    assert play(world, songs[0]).content == audio[0]
    fakes["primary"][1].lookup_delay = 2.0
    fakes["reliable"][1].resolve_delay = 2.0
    fakes["checker"][1].ready = False  # says "not now": the reliable source shares with none
    assert play(world, songs[1]).content == audio[1]
    assert lines_with(playback_log, "playing 'Title slow-hint Song 2' from Reliable")


def test_after_a_miss_the_primary_starts_at_once_for_the_release_s_other_songs(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """The primary lacks one song of a release; for another song of it, the first step
    starts at once - not after the checks (slow here) - and plays it, although a source
    says it has the song ready."""
    songs, fakes, audio = missing_at_primary(world, "at-once", sources, count=2)
    assert play(world, songs[0]).content == audio[0]  # its miss: the release remembered
    fakes["primary"][1].available = True
    fakes["checker"][1].ready = True  # "ready", but only after its check's timeout (0.5 s)
    fakes["checker"][1].availability_delay = 1.0
    started = time.monotonic()
    assert play(world, songs[1]).content == audio[1]
    assert lines_with(playback_log, "playing 'Title at-once Song 2' from Primary")
    [lookup] = [r for r in sources["primary"].requests("resolve-isrc") if r["at"] > started]
    assert lookup["at"] - started < 0.4  # at once, not after the checks (0.5 s)
    assert not [r for r in sources["checker"].requests("stream")
                if r["key"] == fakes["checker"][1].key()]  # fmt: skip


def test_after_a_release_miss_a_ready_source_takes_over_from_a_slow_primary(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """As on an ordinary play, a source whose check says it has the song ready
    takes over once the primary has no first byte after the handover time."""
    songs, fakes, audio = missing_at_primary(world, "take-over", sources, count=2)
    assert play(world, songs[0]).content == audio[0]
    fakes["primary"][1].available = True
    fakes["primary"][1].resolve_delay = 2.5  # slower than the handover time (1 s)
    fakes["checker"][1].ready = True
    fakes["reliable"][1].available = False
    assert play(world, songs[1]).content == audio[1]
    steps = world.services.deliverer.recent[-1].steps
    assert any("Checker: has it ready" in s for s in steps)
    assert any("Primary: stopped after" in s for s in steps)  # (it kept trying beside it)
    assert lines_with(playback_log, "playing 'Title take-over Song 2' from Checker")


async def _forget_pin(world: DeliveryWorld, song: str) -> None:
    world.services.deliverer.forget(song)


def test_a_request_waiting_for_a_failing_routing_gets_its_answer_at_once(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, _ = missing_at_primary(world, "shared", sources)
    # It is slow, then fails (4 s, an error): the routing ends by itself, before apps stop
    # waiting - an answer to share.
    fakes["reliable"][0].resolve_delay = 4.0
    fakes["reliable"][0].stream_status = 502
    for name in ("checker", "preparer"):
        fakes[name][0].available = False
    started = time.monotonic()
    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda n: play(world, song, f"shared-{n}"), range(2)))
    took = time.monotonic() - started
    assert all(a.json()["subsonic-response"]["status"] == "failed" for a in answers)
    assert took < 4.0 + 2.0  # one routing, not two in a row
    assert len(sources["reliable"].requests("stream")) == 1
    # The second waited for the first's routing, or came just after it ended: its answer,
    # shared either way.
    shared = [line for line in playback_log if "its answer is shared" in line]
    assert len(shared) == 1
    assert any(
        words in shared[0]
        for words in ("for another request's routing of this song", "failed a moment ago")
    )


def test_a_client_that_leaves_before_its_first_byte_is_logged(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    [song], fakes, _ = missing_at_primary(world, "left", sources)
    fakes["reliable"][0].resolve_delay = 1.2  # within its share (half: the Checker can tell)
    impatient = world.client(client="impatient")
    impatient.http.timeout = httpx.Timeout(0.8)
    with pytest.raises(httpx.ReadTimeout):
        impatient.request("stream", {"id": song})
    deadline = time.monotonic() + 5
    while not lines_with(playback_log, "no audio for 'Title left Song 1'"):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    [line] = lines_with(playback_log, "no audio for 'Title left Song 1' (play): the client left")
    assert "the routing went on: first byte from Reliable after" in line
    assert not lines_with(playback_log, "playing 'Title left Song 1'")


def test_a_fetch_ahead_that_waited_out_its_turn_is_never_routed_to_the_fallbacks(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """After its turn ran out, the plan's first add-on alone - asked also for a song of
    a release it lacks another song of, and lacking this one too - so nothing, never the
    others side by side."""
    songs, _, audio = missing_at_primary(world, "alone", sources, count=2)
    assert play(world, songs[0]).content == audio[0]
    reliable_streams = len(sources["reliable"].requests("stream"))

    async def fetch_alone() -> str:
        row = await world.services.store.fetchone(
            "SELECT * FROM placeholders WHERE song_id = ?", [songs[1]]
        )
        try:
            await world.services.deliverer.open(track_of(row), None, purpose="alone")
        except NoSource as exc:
            return str(exc)
        return "served"

    started = time.monotonic()
    assert world.server.call(fetch_alone) == "Primary: not available"
    assert time.monotonic() - started < 0.5
    assert len(sources["reliable"].requests("stream")) == reliable_streams
    assert lines_with(playback_log, "no audio for 'Title alone Song 2' (fetch ahead)")


def test_a_fetch_ahead_that_waited_out_its_turn_uses_another_request_s_link(
    world: DeliveryWorld, sources: dict[str, FakeAddon], playback_log: list[str]
) -> None:
    """A fetch ahead that waited out its turn (the primary alone) waits for another
    request's routing of the song - here the play itself, at the primary - and uses the link
    that routing found, instead of failing."""
    songs, fakes, audio = missing_at_primary(world, "alone-pin", sources, count=1)
    fakes["primary"][0].available = True
    fakes["primary"][0].resolve_delay = 1.0  # the play holds the song meanwhile

    async def fetch_alone() -> bytes | str:
        row = await world.services.store.fetchone(
            "SELECT * FROM placeholders WHERE song_id = ?", [songs[0]]
        )
        try:
            opened = await world.services.deliverer.open(track_of(row), None, purpose="alone")
        except NoSource as exc:
            return str(exc)
        try:
            assert opened.body is not None
            return b"".join([chunk async for chunk in opened.body])
        finally:
            await opened.close()

    with ThreadPoolExecutor(2) as pool:
        played = pool.submit(play, world, songs[0], "alone-pin-play")
        time.sleep(0.3)
        fetched = pool.submit(world.server.call, fetch_alone)
        assert played.result().content == audio[0]
        assert fetched.result() == audio[0]
    assert len(sources["primary"].requests("stream")) == 1  # one link, shared


def test_a_refusal_is_not_remembered_as_a_miss(
    world: DeliveryWorld, sources: dict[str, FakeAddon]
) -> None:
    """Only the primary's lookup saying "not found" is a miss: a refusal (HTTP 403) is not
    remembered, so the next song of the release asks the primary again."""
    songs, _, audio = missing_at_primary(world, "refused", sources, count=2)
    sources["primary"].refuse_lookups = True
    try:
        assert play(world, songs[0]).content == audio[0]
        assert play(world, songs[1]).content == audio[1]
    finally:
        sources["primary"].refuse_lookups = False
    assert len(sources["primary"].requests("resolve-isrc")) == 2
