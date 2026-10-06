"""Suite K (continued) - delivered audio expires, per-user limits.

- Download-first audio in place of a placeholder goes back to the silent placeholder after
  a while without use, and the least recently used first past a size limit; the song ID
  and what users did with it stay, and its next play streams from the add-ons again.
  Audio used in the last couple of hours stays.
- Delivered audio whose file is gone gets its placeholder back at once, used or not; a
  play through a share link is a use.
- Per user: songs looked up at the add-ons at once (more wait their turn), download-first
  fetches at once, and an hour's allowance of them (more wait their turn, paced: a large
  offline sync slows down instead of failing). The song being played never waits behind
  the user's queued downloads: a fetch of it waiting its turn goes at once. The hour's
  allowance paces the download route only, never plain streams.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mutagen import MutagenError

from shijhon.navidrome.scans import ScanTimeout
from shijhon.placeholders.engine import ReplaceError
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon

DAY = 86400.0


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory({"ND_ENABLESHARING": "true"}),
        tmp_path_factory.mktemp("limits"),
        warm_ahead_depth=0,
        user_routings=2,
        budget_seconds=6.0,
        max_wait_seconds=20.0,
    ) as w:
        yield w


@pytest.fixture
def addon(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("Source")
    world.add_source(addon)
    yield addon
    world.clear_sources()


def row(world: DeliveryWorld, song: str) -> dict[str, Any]:
    async def get() -> dict[str, Any]:
        found = await world.services.store.fetchone(
            "SELECT * FROM placeholders WHERE song_id = ?", [song]
        )
        assert found is not None
        return dict(found)

    return world.server.call(get)


def delivered(world: DeliveryWorld, key: str, addon: FakeAddon) -> str:
    """A placeholder whose audio was delivered (a download)."""
    song, _, _ = world.placeholder_track(key, [addon])
    answer = world.client().request("download", {"id": song})
    assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    assert row(world, song)["state"] == "delivered"
    return song


def age(world: DeliveryWorld, song: str, days: float) -> None:
    """The song's audio was delivered and last used ``days`` ago (its use recorded when it
    was delivered is forgotten too: a use is recorded at most every few minutes)."""

    async def set_age() -> None:
        when = time.time() - days * DAY
        await world.services.store.execute(
            "UPDATE placeholders SET delivered_at = ?, last_used_at = ? WHERE song_id = ?",
            [when, when, song],
        )

    world.server.call(set_age)
    expiry = world.services.download_first.expiry
    assert expiry is not None
    expiry._touched.pop(song, None)


def sweep(world: DeliveryWorld, **settings: Any) -> Any:
    expiry = world.services.download_first.expiry
    assert expiry is not None
    saved = {k: getattr(expiry, k) for k in settings}
    for key, value in settings.items():
        setattr(expiry, key, value)
    try:
        return world.server.call(expiry.sweep)
    finally:
        for key, value in saved.items():
            setattr(expiry, key, value)


def test_delivered_audio_unused_for_a_while_goes_back_to_the_placeholder(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    song = delivered(world, "k-expire-old", addon)
    world.client().ok("star", {"id": song})
    fresh = delivered(world, "k-expire-fresh", addon)
    age(world, song, 31)
    age(world, fresh, 29)
    swept = sweep(world)
    assert swept.expired >= 1 and swept.failed == 0
    assert row(world, song)["state"] == "placeholder"
    assert row(world, fresh)["state"] == "delivered"
    info = world.client().ok("getSong", {"id": song})["song"]  # the same song, still starred
    assert info["id"] == song and info.get("starred") and info["suffix"] == "flac"
    streams = len(addon.requests("stream"))
    assert world.client().request("stream", {"id": song}).content[:4] == b"fLaC"
    assert len(addon.requests("stream")) == streams + 1  # from the add-on again


def test_past_the_size_limit_the_least_recently_used_goes_first(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    older = delivered(world, "k-size-older", addon)
    newer = delivered(world, "k-size-newer", addon)
    used_now = delivered(world, "k-size-used-now", addon)
    age(world, older, 100)  # the least recently used of all delivered audio here
    age(world, newer, 1)
    size = len(world.client().request("download", {"id": older}).content)
    age(world, older, 100)  # (the download above counted as a use)
    total = sweep(world, max_age=0, max_bytes=10**12).kept_bytes  # all of it, nothing goes
    swept = sweep(world, max_age=0, max_bytes=total - size // 2)
    assert swept.over_size == 1
    assert row(world, older)["state"] == "placeholder"
    assert row(world, newer)["state"] == "delivered"
    # Everything past the limit, but just used: kept (a paused play may seek in it).
    sweep(world, max_age=0, max_bytes=1)
    assert row(world, used_now)["state"] == "delivered"
    assert row(world, newer)["state"] == "placeholder"


def test_a_use_of_delivered_audio_keeps_it(world: DeliveryWorld, addon: FakeAddon) -> None:
    song = delivered(world, "k-expire-used", addon)
    age(world, song, 31)
    assert world.client().request("stream", {"id": song}).status_code == 200
    assert time.time() - float(row(world, song)["last_used_at"]) < 60
    assert sweep(world).expired == 0
    assert row(world, song)["state"] == "delivered"


def test_delivered_audio_whose_file_is_gone_gets_its_placeholder_back(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """E.g. after a restore from a backup that leaves delivered audio out: used just now, it
    is repaired all the same (it would otherwise stay silent-less for good)."""
    song = delivered(world, "k-gone", addon)
    (world.nd.music / row(world, song)["path"]).unlink()
    swept = sweep(world)
    assert swept.missing == 1 and swept.failed == 0
    assert row(world, song)["state"] == "placeholder"
    assert world.client().ok("getSong", {"id": song})["song"]["id"] == song
    assert world.client().request("stream", {"id": song}).content[:4] == b"fLaC"


def test_one_song_s_failing_repair_leaves_the_others_to_the_sweep(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One song whose delivered file is gone cannot be repaired (its tags fail): that song
    fails (logged), the next one is repaired all the same. Navidrome unavailable (its scan
    times out) ends the sweep instead - every other song would wait for it too - and the
    next sweep repairs what is left."""
    first = delivered(world, "k-gone-fails", addon)
    second = delivered(world, "k-gone-repaired", addon)
    for song in (first, second):
        (world.nd.music / row(world, song)["path"]).unlink()
    engine = world.services.engine
    real = engine.revert_to_placeholder
    asked: list[str] = []
    failure: Exception = MutagenError("the tags could not be written")

    async def revert(song_id: str, **kwargs: Any) -> bool:
        asked.append(song_id)
        if song_id == first:
            raise failure
        return await real(song_id, **kwargs)

    monkeypatch.setattr(engine, "revert_to_placeholder", revert)
    swept = sweep(world)
    assert swept.missing == 1 and swept.failed == 1
    assert row(world, second)["state"] == "placeholder"
    third = delivered(world, "k-gone-later", addon)
    (world.nd.music / row(world, third)["path"]).unlink()
    failure, asked[:] = ScanTimeout("the scan did not end"), []
    swept = sweep(world)
    assert swept.failed == 1 and asked == [first]  # the sweep ended there
    assert row(world, third)["state"] == "delivered"
    monkeypatch.setattr(engine, "revert_to_placeholder", real)
    assert sweep(world).missing == 2  # the next sweep repairs both
    assert row(world, first)["state"] == row(world, third)["state"] == "placeholder"


def test_delivered_audio_played_through_a_share_link_is_a_use(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    song = delivered(world, "k-shared", addon)
    share = world.client().ok("createShare", {"id": song})["shares"]["share"][0]
    page = httpx.get(world.server.base_url + "/share/" + share["id"])
    info = re.search(r"__SHARE_INFO__\s*=\s*(\".*?\")\s*</script>", page.text, re.S)
    assert info is not None
    token = json.loads(json.loads(info.group(1)))["tracks"][0]["id"]
    age(world, song, 31)
    expiry = world.services.download_first.expiry
    assert expiry is not None
    expiry._touched.clear()  # its last recorded use was long ago
    assert httpx.get(f"{world.server.base_url}/share/s/{token}").status_code == 200
    assert time.time() - float(row(world, song)["last_used_at"]) < 60


def test_an_hour_s_downloads_per_user_are_paced(world: DeliveryWorld, addon: FakeAddon) -> None:
    """Past the allowance a download waits its turn (here one every 2 s), no error."""
    limits = world.services.download_first.limits
    assert limits is not None
    saved = limits.per_hour, limits.hour, limits.sleep
    paused: list[float] = []

    async def sleep(seconds: float) -> None:
        paused.append(seconds)
        await anyio.sleep(seconds)

    limits.per_hour, limits.hour, limits.sleep = 1, 2.0, sleep
    limits._users.clear()
    try:
        first, _, _ = world.placeholder_track("k-hour-1", [addon])
        second, _, _ = world.placeholder_track("k-hour-2", [addon])
        assert world.client().request("download", {"id": first}).status_code == 200
        assert paused == []
        paced = world.client().request("download", {"id": second})
        assert paced.status_code == 200 and paced.content[:4] == b"fLaC"
        assert paused and 0 < paused[0] <= 2.0  # it waited for its turn
        assert row(world, second)["state"] == "delivered"
    finally:
        limits.per_hour, limits.hour, limits.sleep = saved
        limits._users.clear()


def test_a_sync_at_a_lower_bitrate_looks_its_songs_up_in_its_turn(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A stream at a lower bitrate that is not the song being played (an
    offline sync) waits for the hour's allowance before its song is looked up at the
    add-ons - not only before its download; the download then reads on from the answer its
    first bytes came from (the link is asked once)."""
    limits = world.services.download_first.limits
    assert limits is not None
    saved = limits.per_hour, limits.hour, limits.sleep
    paused: list[tuple[float, float]] = []

    async def sleep(seconds: float) -> None:
        paused.append((time.monotonic(), seconds))
        await anyio.sleep(seconds)

    limits.per_hour, limits.hour, limits.sleep = 1, 2.0, sleep
    limits._users.clear()
    try:
        first, _, _ = world.placeholder_track("k-sync-1", [addon])
        second, fakes, _ = world.placeholder_track("k-sync-2", [addon])
        assert world.client().request("download", {"id": first}).status_code == 200
        answer = world.client().request("stream", {"id": second, "maxBitRate": 96})
        assert answer.status_code == 200
        assert row(world, second)["state"] == "delivered"
        assert paused  # the allowance was used up: it waited
        waited_until = paused[0][0] + paused[0][1]
        track = fakes[addon]
        [looked_up] = [r for r in addon.requests("stream") if r["path"] == f"/stream/{track.key()}"]
        assert looked_up["at"] >= waited_until - 0.05  # looked up only once its turn came
        assert len([r for r in addon.requests("audio") if r["isrc"] == track.isrc]) == 1
    finally:
        limits.per_hour, limits.hour, limits.sleep = saved
        limits._users.clear()


def test_plain_streams_are_not_paced_by_the_hour_s_downloads(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """The hour's allowance is the download route's: with it used up, plain streams -
    a client saving songs for offline use by streaming them at their own quality, also one
    naming a bitrate with ``format=raw`` (Navidrome serves the file as it is then) - play at
    once, and take none of it."""
    limits = world.services.download_first.limits
    assert limits is not None
    saved = limits.per_hour, limits.sleep
    paused: list[float] = []

    async def sleep(seconds: float) -> None:  # a stream waiting its turn: fails at once
        paused.append(seconds)
        raise RuntimeError("paced")

    limits.per_hour, limits.sleep = 1, sleep  # the next download would wait an hour
    limits._users.clear()
    try:
        downloaded, _, _ = world.placeholder_track("k-route-download", [addon])
        assert world.client().request("download", {"id": downloaded}).status_code == 200
        allowance = [r.allowance for r in limits._users.values()]
        assert allowance and max(allowance) < 1  # used up
        for n, extra in enumerate(({}, {"format": "raw"}, {"format": "raw", "maxBitRate": 320})):
            song, _, _ = world.placeholder_track(f"k-route-stream-{n}", [addon])
            answer = world.client().request("stream", {"id": song, **extra})
            assert answer.content[:4] == b"fLaC"
        assert paused == []
        assert min(r.allowance for r in limits._users.values()) >= min(allowance)  # none taken
    finally:
        limits.per_hour, limits.sleep = saved
        limits._users.clear()


def test_a_play_goes_ahead_of_the_user_s_queued_downloads(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """One download at a time (the setting here): a slow download holds it; the client's
    fetch ahead of the next song waits its turn; the client then plays that song (a lower
    bitrate: download-first) - it is fetched at once, and only once."""
    limits = world.services.download_first.limits
    assert limits is not None
    saved = limits.downloads
    limits.downloads = 1
    limits._users.clear()
    slow, fakes, _ = world.placeholder_track("k-ahead-slow", [addon])
    fakes[addon].resolve_delay = 4.0
    upcoming, next_fakes, _ = world.placeholder_track("k-ahead-next", [addon])
    download_first = world.services.download_first

    async def scenario() -> tuple[str | None, str]:
        async with anyio.create_task_group() as tg:
            tg.start_soon(download_first.ensure, slow, "k-user")  # holds the one turn
            await anyio.sleep(0.3)
            tg.start_soon(download_first.ensure, upcoming, "k-user")  # a fetch ahead: waits
            await anyio.sleep(0.3)
            failure = await download_first.ensure(upcoming, "k-user", playing=True)
            slow_now = await world.services.store.fetchone(
                "SELECT state FROM placeholders WHERE song_id = ?", [slow]
            )
            assert slow_now is not None
            return failure, str(slow_now["state"])

    try:
        failure, slow_then = world.server.call(scenario)
    finally:
        limits.downloads = saved
        limits._users.clear()
    assert failure is None and slow_then == "placeholder"  # not behind the slow download
    assert row(world, upcoming)["state"] == "delivered"
    assert row(world, slow)["state"] == "delivered"
    key = next_fakes[addon].key()
    assert len([r for r in addon.requests("stream") if r["key"] == key]) == 1  # fetched once


@pytest.mark.parametrize("report", ["while it waits", "just before"])
def test_a_report_takes_a_lookup_out_of_its_wait_for_a_turn(
    navidrome_factory: NavidromeFactory, tmp_path: Path, report: str
) -> None:
    """A stream naming a bitrate looks its song up first. When that lookup waits
    for one of the user's turns - behind a slow lookup here - and the client reports the
    song as playing, it goes on at once, as the song being played; so does one that comes
    just after the report."""
    with delivery_world(
        navidrome_factory(), tmp_path, warm_ahead_depth=1, user_routings=1, budget_seconds=12.0
    ) as world:
        addon = world.addon("Source")
        world.add_source(addon)
        slow, slow_fakes, _ = world.placeholder_track("k-report-slow", [addon])
        slow_fakes[addon].resolve_delay = 6.0  # holds the user's one turn
        song, fakes, _ = world.placeholder_track("k-report-next", [addon])

        def asked(track: Any) -> list[float]:
            return [r["at"] for r in addon.requests("stream") if r["key"] == track.key()]

        def until(done: Any, seconds: float = 10.0) -> None:
            end = time.monotonic() + seconds
            while not done():
                assert time.monotonic() < end
                time.sleep(0.02)

        def tell() -> float:
            answer = world.client().request("scrobble", {"id": song, "submission": "false"})
            assert answer.status_code == 200
            return time.monotonic()

        with ThreadPoolExecutor(2) as pool:
            held = pool.submit(lambda: world.client().request("stream", {"id": slow}))
            until(lambda: asked(slow_fakes[addon]))  # the slow lookup holds the turn
            if report == "just before":
                told = tell()
            waiting = pool.submit(
                lambda: world.client().request("stream", {"id": song, "maxBitRate": 96})
            )
            if report == "while it waits":
                time.sleep(1.0)
                assert asked(fakes[addon]) == []  # behind the slow lookup
                told = tell()
            until(lambda: asked(fakes[addon]))
            # Looked up as soon as the client said so: while the slow lookup still ran.
            assert asked(fakes[addon])[0] - told < 2.0
            assert asked(fakes[addon])[0] < asked(slow_fakes[addon])[0] + 5.0
            assert not held.done()
            assert waiting.result().status_code == 200 and held.result().status_code == 200
        assert len(asked(fakes[addon])) == 1
        assert row(world, song)["state"] == "delivered"


def test_songs_looked_up_at_once_per_user(world: DeliveryWorld, addon: FakeAddon) -> None:
    """Two routings at a time for one user (the setting here); the others wait their
    turn and are served."""
    songs = []
    for n in range(4):
        song, fakes, _ = world.placeholder_track(f"k-turns-{n}", [addon])
        fakes[addon].resolve_delay = 1.0
        songs.append(song)

    def play(n: int) -> httpx.Response:
        return world.client(client=f"turns-{n}").request("stream", {"id": songs[n]})

    with ThreadPoolExecutor(4) as pool:
        answers = list(pool.map(play, range(4)))
    assert all(a.status_code == 200 and a.content[:4] == b"fLaC" for a in answers)
    starts = sorted(r["at"] for r in addon.requests("stream"))
    assert len(starts) == 4
    assert starts[1] - starts[0] < 0.5  # two at once
    assert starts[2] - starts[0] >= 0.9  # the third waited for a turn


def test_probes_of_cold_songs_take_the_user_s_lookup_turns(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A HEAD of a song without a link is looked up at the add-ons like a GET: two at a
    time for one user here, the others wait their turn and are answered (before, every
    probe went to the add-ons at once, whatever the user's limit)."""
    songs = []
    for n in range(6):
        song, fakes, _ = world.placeholder_track(f"k-probe-{n}", [addon])
        fakes[addon].resolve_delay = 0.6
        songs.append(song)

    def probe(n: int) -> httpx.Response:
        client = world.client(client=f"probe-{n}")
        return client.request("stream", {"id": songs[n]}, http_method="HEAD")

    with ThreadPoolExecutor(6) as pool:
        answers = list(pool.map(probe, range(6)))
    assert all(a.status_code == 200 for a in answers)
    starts = sorted(r["at"] for r in addon.requests("stream"))
    assert len(starts) == 6
    # Each link takes 0.6 s: never more than two being asked for at once.
    for index, at in enumerate(starts):
        assert sum(1 for other in starts[: index + 1] if other > at - 0.5) <= 2
    assert starts[-1] - starts[0] >= 2 * 0.6 - 0.1
    # A song with a link is probed at once, whatever waits: nothing is looked up for it.
    began = time.monotonic()
    with ThreadPoolExecutor(1) as pool:
        again = list(pool.map(probe, range(1)))
    assert again[0].status_code == 200 and time.monotonic() - began < 1.5
    assert len(addon.requests("stream")) == 6


def test_a_later_range_of_a_play_whose_link_is_gone_takes_a_play_s_turn(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A continuing play (a seek after its link expired) is looked up again in one of the
    user's turns too - a play's, never behind the user's waiting lookups."""
    played, _, _ = world.placeholder_track("k-seek-turn", [addon], seconds=6)
    assert world.client().request("stream", {"id": played}).status_code == 200
    world.server.call(_forget(world, played))  # its link expired
    waiting = []
    for n in range(3):
        song, fakes, _ = world.placeholder_track(f"k-seek-wait-{n}", [addon])
        fakes[addon].resolve_delay = 1.5
        waiting.append(song)
    limits = world.services.interceptor.limits
    assert limits is not None
    taken: list[bool] = []
    real = limits.routing

    def noted(user: str, **kwargs: Any) -> Any:
        taken.append(bool(kwargs.get("queue", True)))
        return real(user, **kwargs)

    limits.routing = noted  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(3) as pool:
            probes = [
                pool.submit(
                    world.client(client=f"seek-{n}").request,
                    "stream",
                    {"id": song},
                    http_method="HEAD",
                )
                for n, song in enumerate(waiting)
            ]
            deadline = time.monotonic() + 10
            while len(taken) < 3 and time.monotonic() < deadline:
                time.sleep(0.02)  # the user's two lookup turns are taken, a third waits
            began = time.monotonic()
            seek = world.client().request(
                "stream", {"id": played}, headers={"range": "bytes=1000-"}
            )
            assert seek.status_code == 206 and time.monotonic() - began < 1.0
            assert all(p.result().status_code == 200 for p in probes)
    finally:
        limits.routing = real  # type: ignore[method-assign]
    assert taken == [True, True, True, False]  # the probes queued; the seek took a play's


def test_a_later_range_has_a_seek_s_time_in_all_its_wait_for_a_turn_included(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A probe with a later range of a song never played waits its turn among the user's
    lookups; what is left of the seek's time is what its routing gets (before: the whole
    time again)."""
    waiting = []
    for n in range(2):
        song, fakes, _ = world.placeholder_track(f"k-range-wait-{n}", [addon])
        fakes[addon].resolve_delay = 1.2
        waiting.append(song)
    ranged, _, _ = world.placeholder_track("k-range", [addon])
    deliverer = world.services.deliverer
    real = deliverer.open
    budgets: list[float | None] = []

    async def noted(track: Any, range_header: Any, **kwargs: Any) -> Any:
        if range_header == "bytes=10-":
            budgets.append(kwargs.get("budget"))
        return await real(track, range_header, **kwargs)

    deliverer.open = noted  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(2) as pool:
            probes = [
                pool.submit(
                    world.client(client=f"range-{n}").request,
                    "stream",
                    {"id": song},
                    http_method="HEAD",
                )
                for n, song in enumerate(waiting)
            ]
            time.sleep(0.3)  # both of the user's lookup turns are taken for about a second
            answer = world.client().request(
                "stream", {"id": ranged}, http_method="HEAD", headers={"range": "bytes=10-"}
            )
            assert answer.status_code == 206
            assert all(p.result().status_code == 200 for p in probes)
    finally:
        deliverer.open = real  # type: ignore[method-assign]
    seek = deliverer.settings.seek_timeout_seconds
    assert len(budgets) == 1 and budgets[0] is not None
    assert 0.1 <= budgets[0] <= seek - 0.5  # what its wait for a turn left of a seek's time


def _forget(world: DeliveryWorld, song: str) -> Any:
    async def forget() -> None:
        world.services.deliverer.forget(song)

    return forget


def test_without_fetches_ahead_told_apart_every_fetch_waits_its_turn(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """``ahead_window_seconds`` 0 (as in this world): nothing tells the song being played
    from fetches ahead, so a lower-bitrate stream takes a download turn like any other (its
    audio's first bytes, read before, tell that it needs converting)."""
    limits = world.services.download_first.limits
    assert limits is not None
    saved = limits.downloads
    limits.downloads = 1
    limits._users.clear()
    slow, fakes, _ = world.placeholder_track("k-noahead-slow", [addon])
    fakes[addon].chunk_delay = 0.25  # its whole file takes a while
    other, _, _ = world.placeholder_track("k-noahead-other", [addon])

    def fetch(song: str, **params: Any) -> httpx.Response:
        return world.client().request("stream", {"id": song, "maxBitRate": 96, **params})

    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(fetch, slow)
            time.sleep(0.3)
            second = pool.submit(fetch, other)
            assert second.result().status_code == 200 and first.result().status_code == 200
    finally:
        limits.downloads = saved
        limits._users.clear()
    delivered = {song: row(world, song)["delivered_at"] for song in (slow, other)}
    assert delivered[other] >= delivered[slow]  # it waited for the slow one's turn


def test_a_half_done_revert_is_put_back(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swap's second move fails (a full disk): the delivered file is back in place and
    its row says so - never a row without its file."""
    song = delivered(world, "k-half-done", addon)
    path = world.nd.music / row(world, song)["path"]
    audio = path.read_bytes()
    real, moves = os.replace, []

    def second_fails(source: Any, target: Any) -> None:
        moves.append(target)
        if len(moves) == 2:
            raise OSError("no space left")
        real(source, target)

    monkeypatch.setattr(os, "replace", second_fails)

    async def revert() -> bool:
        return await world.services.engine.revert_to_placeholder(song)

    with pytest.raises(ReplaceError):
        world.server.call(revert)
    monkeypatch.setattr(os, "replace", real)
    assert row(world, song)["state"] == "delivered"
    assert path.read_bytes() == audio


def test_a_source_s_success_is_its_first_byte_of_audio(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """An add-on whose links work but whose audio then fails reaches three failures in a row
    (the dashboard's "Error"): a link handed over is no success; its first byte of audio
    is, and starts the count again."""
    registry = world.services.sources
    [stored] = [s for s in world.server.call(registry.stored) if s.name == "Source"]
    for n in range(3):
        song, fakes, _ = world.placeholder_track(f"k-links-{n}", [addon])
        fakes[addon].expire_after, fakes[addon].expire_status = 0, 503  # audio fails
        failed = world.client().request("stream", {"id": song})
        assert failed.json()["subsonic-response"]["status"] == "failed"
    stats = registry.stats(stored.id)
    assert stats is not None and stats.failures_since_success >= 3
    assert stats.last_success_at is None and stats.successes == 0
    # Three errors in a row (HTTP 503) also cooled it down: it is asked again after.
    assert registry.cooling(stored.id)
    deadline = time.monotonic() + 5
    while registry.cooling(stored.id):
        assert time.monotonic() < deadline
        time.sleep(0.1)
    working, _, _ = world.placeholder_track("k-links-ok", [addon])
    assert world.client().request("stream", {"id": working}).content[:4] == b"fLaC"
    assert stats.failures_since_success == 0 and stats.last_success_at is not None
    assert stats.successes == 1 and stats.errors_since_success == 0


def test_a_new_link_that_does_not_work_counts_a_stale_one_does_not(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A link it just gave answering 410 is the source's failure; a play's own link that
    expired (a seek after a while) is not - the play continues with a new link."""
    registry = world.services.sources
    [stored] = [s for s in world.server.call(registry.stored) if s.name == "Source"]
    song, fakes, audio = world.placeholder_track("k-stale-link", [addon])
    fakes[addon].expire_after = 1  # each link serves one audio request, then answers 410
    assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
    seek = world.client().request("stream", {"id": song}, headers={"range": "bytes=100-199"})
    assert seek.status_code == 206 and seek.content == audio.read_bytes()[100:200]
    stats = registry.stats(stored.id)
    assert stats is not None and stats.failures == 0 and stats.failures_since_success == 0
    fresh, other, _ = world.placeholder_track("k-fresh-410", [addon])
    other[addon].expire_after, other[addon].expire_status = 0, 410
    world.client().request("stream", {"id": fresh})
    assert stats.failures >= 1 and stats.last_failure is not None
    assert "a new link answered HTTP 410" in stats.last_failure
