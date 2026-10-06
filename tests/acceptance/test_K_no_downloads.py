"""Suite K - an add-on that asks not to be used for downloads.

A manifest's ``allowDownloads`` of 0, false or "0" says the add-on does not want to be used
for bulk downloads. Shijhon's download-first work - a ``download`` request, a stream that
has to be converted, filling the library with the whole file - and its requests to prepare
a song for next time then go to the other add-ons; when none can deliver, a ``download``
is answered as when no add-on has the song, and a stream that had to be converted is
streamed as it is. Plays, warm-ahead and the checks whether a song is ready still use it.
Without the field, or with 1, nothing changes.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(),
        tmp_path_factory.mktemp("no-downloads"),
        warm_ahead_depth=0,
        availability_timeout_seconds=1.0,
    ) as w:
        yield w


@pytest.fixture
def fresh(world: DeliveryWorld) -> Iterator[None]:
    world.clear_sources()
    settings = world.services.deliverer.settings
    saved = (settings.routing, settings.reliable_source, settings.prepare_when_not_ready)
    saved_warm = settings.warm_ahead_depth
    yield
    settings.routing, settings.reliable_source, settings.prepare_when_not_ready = saved
    settings.warm_ahead_depth = saved_warm
    world.clear_sources()


def shy(world: DeliveryWorld, name: str = "Shy", value: Any = 0, **kwargs: Any) -> FakeAddon:
    """An add-on whose manifest says ``allowDownloads: value``."""
    addon = world.addon(name, **kwargs)
    addon.manifest_extra = {"allowDownloads": value}
    return addon


def row(world: DeliveryWorld, song: str) -> dict[str, Any]:
    async def get() -> dict[str, Any]:
        found = await world.services.store.fetchone(
            "SELECT * FROM placeholders WHERE song_id = ?", [song]
        )
        assert found is not None
        return dict(found)

    return world.server.call(get)


def failed(response: httpx.Response) -> str | None:
    """The error message of a Subsonic error answer; None for anything else."""
    if not response.headers.get("content-type", "").startswith("application/json"):
        return None
    body = response.json()["subsonic-response"]
    return str(body["error"]["message"]) if body["status"] == "failed" else None


def asked_for_audio(addon: FakeAddon) -> bool:
    """Whether the add-on was asked for a link or its audio."""
    return bool(addon.requests("stream") or addon.requests("audio"))


@pytest.mark.parametrize("value", [0, False, "0"], ids=["0", "false", "quoted-0"])
def test_a_download_goes_to_another_add_on(world: DeliveryWorld, fresh: None, value: Any) -> None:
    first, other = shy(world, value=value), world.addon("Other")
    world.add_source(first)
    world.add_source(other)
    song, _, _ = world.placeholder_track(f"nd-download-{value!r}", [first, other])
    answer = world.client().request("download", {"id": song})
    assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    assert row(world, song)["state"] == "delivered"
    assert not asked_for_audio(first)
    assert other.requests("stream") and other.requests("audio")


@pytest.mark.parametrize("routing", ["ordered", "primary_first"])
def test_a_download_after_a_play_from_it_goes_elsewhere(
    world: DeliveryWorld, fresh: None, routing: str
) -> None:
    """The play's link, at the add-on that asks not to be used for downloads, is not the
    download's - also with that add-on as the primary."""
    settings = world.services.deliverer.settings
    settings.routing, settings.primary_source = routing, "Shy"
    first, other = shy(world), world.addon("Other")
    world.add_source(first)
    world.add_source(other)
    try:
        song, _, audio = world.placeholder_track(f"nd-after-play-{routing}", [first, other])
        assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
        assert first.requests("audio") and not other.requests("stream")
        played = len(first.requests("audio"))
        answer = world.client().request("download", {"id": song})
        assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
        assert row(world, song)["state"] == "delivered"
        assert len(first.requests("audio")) == played  # nothing more asked of it
        assert other.requests("stream") and other.requests("audio")
    finally:
        settings.primary_source = ""


def test_a_play_from_it_keeps_its_file_while_the_song_is_downloaded_elsewhere(
    world: DeliveryWorld, fresh: None
) -> None:
    """The download's link at the other add-on is the download's own: a seek of the play
    meanwhile is still served from the add-on the play started at."""
    first, other = shy(world), world.addon("Other")
    world.add_source(first)
    world.add_source(other)
    song, fakes, audio = world.placeholder_track("nd-seek", [first, other])
    # The other add-on's file is another one (another tone, as long), its download slow.
    fakes[other].audio = world.audio("nd-seek-other")
    fakes[other].chunk_delay = 0.4
    data = audio.read_bytes()
    assert fakes[other].audio.read_bytes()[100:] != data[100:]
    assert world.client().request("stream", {"id": song}).content == data
    with ThreadPoolExecutor(1) as pool:
        downloading = pool.submit(world.client().request, "download", {"id": song})
        deadline = time.monotonic() + 5
        while not other.requests("audio") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert other.requests("audio") and not downloading.done()
        before = len(first.requests("audio"))
        seek = world.client().request("stream", {"id": song}, headers={"range": "bytes=100-"})
        assert seek.status_code == 206 and seek.content == data[100:]
        assert not downloading.done()  # (the seek came while the download went on)
        assert len(first.requests("audio")) == before + 1  # from the play's own add-on
        assert len(other.requests("audio")) == 1  # the download alone
        answer = downloading.result(timeout=30)
    assert answer.status_code == 200 and row(world, song)["state"] == "delivered"
    # In the library now - the other add-on's file: the play's seeks stay on its own.
    seek = world.client().request("stream", {"id": song}, headers={"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == data[100:]
    assert len(first.requests("audio")) == before + 2 and len(other.requests("audio")) == 1
    library = world.services.engine.layout.absolute(row(world, song)["path"]).read_bytes()
    assert library[100:] != data[100:]
    assert world.client().request("stream", {"id": song}).content == library  # a new play


def test_a_download_no_other_add_on_can_deliver_is_answered_as_without_the_song(
    world: DeliveryWorld, fresh: None
) -> None:
    alone = shy(world)
    world.add_source(alone)
    song, _, _ = world.placeholder_track("nd-alone", [alone])
    message = failed(world.client().request("download", {"id": song}))
    assert message is not None and message.startswith("download unavailable: no source")
    assert "Shy: not used for downloads (the add-on asks this)" in message
    assert row(world, song)["state"] != "delivered"
    assert not asked_for_audio(alone)
    # With an add-on that lacks the song, the same answer.
    world.clear_sources()
    lacking = world.addon("Lacking")
    world.add_source(lacking)
    other, _, _ = world.placeholder_track("nd-lacking", [lacking], available=False)
    message = failed(world.client().request("download", {"id": other}))
    assert message is not None and message.startswith("download unavailable: no source")


def test_plays_and_warm_ahead_still_use_it(world: DeliveryWorld, fresh: None) -> None:
    world.services.deliverer.settings.warm_ahead_depth = 1
    alone = shy(world)
    world.add_source(alone)
    release = catalog_release("nd-plays", "Plays", "Player", 2)
    result = world.materialize(release)
    songs = [result.created[t.ref] for t in release.tracks]
    audios = []
    for track in release.tracks:
        assert track.isrc
        audios.append(world.audio(track.title))
        alone.add(FakeTrack(isrc=track.isrc, audio=audios[-1]))
    played = world.client(client="nd-player").request("stream", {"id": songs[0]})
    assert played.content == audios[0].read_bytes()
    deadline = time.monotonic() + 10
    while len(alone.requests("stream")) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(alone.requests("stream")) == 2  # the next song warmed there too
    assert row(world, songs[0])["state"] != "delivered"


def test_a_conversion_is_fetched_elsewhere_or_streamed_as_it_is(
    world: DeliveryWorld, fresh: None
) -> None:
    """A stream in another format is download-first: its whole file comes from the other
    add-on. Without one, the song is streamed from this one in its own format."""
    first, other = shy(world), world.addon("Other")
    world.add_source(first)
    world.add_source(other)
    song, _, _ = world.placeholder_track("nd-convert", [first, other])
    converted = world.client().request("stream", {"id": song, "format": "mp3", "maxBitRate": 96})
    assert converted.status_code == 200 and converted.headers["content-type"] == "audio/mpeg"
    assert row(world, song)["state"] == "delivered"
    assert other.requests("audio")
    # Its first bytes told that it needs converting (a stream there, which it allows);
    # the whole file was not read from it.
    assert len(first.requests("audio")) <= 1
    world.clear_sources()
    alone = shy(world, "Alone")
    world.add_source(alone)
    single, _, audio = world.placeholder_track("nd-convert-alone", [alone])
    answer = world.client().request("stream", {"id": single, "format": "mp3", "maxBitRate": 96})
    assert answer.status_code == 200 and answer.content == audio.read_bytes()
    assert answer.headers["content-type"] == "audio/flac"  # as it is
    assert row(world, single)["state"] != "delivered"


def test_ready_checks_go_on_and_preparation_goes_elsewhere(
    world: DeliveryWorld, fresh: None
) -> None:
    settings = world.services.deliverer.settings
    settings.routing, settings.reliable_source = "ready_first", "Reliable"
    settings.prepare_when_not_ready = True
    checks = ("stream", "isrc", "availability")
    first, preparer = shy(world, resources=checks), world.addon("Preparer", resources=checks)
    reliable = world.addon("Reliable")
    for addon in (first, preparer, reliable):
        world.add_source(addon)
    song, _, audio = world.placeholder_track("nd-ready", [first, preparer, reliable], ready=False)
    assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
    deadline = time.monotonic() + 5
    while len(preparer.requests("availability")) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [r["prepare"] for r in preparer.requests("availability")] == [False, True]
    assert [r["prepare"] for r in first.requests("availability")] == [False]  # checked only
    # When only that add-on said "not now", nothing is asked to prepare the song.
    world.clear_sources()
    alone, reliable = shy(world, "Alone", resources=checks), world.addon("Reliable")
    world.add_source(alone)
    world.add_source(reliable)
    other, _, audio = world.placeholder_track("nd-ready-alone", [alone, reliable], ready=False)
    assert world.client().request("stream", {"id": other}).content == audio.read_bytes()
    time.sleep(0.5)
    assert [r["prepare"] for r in alone.requests("availability")] == [False]


@pytest.mark.parametrize("value", [None, 1, True, "1"])
def test_without_the_field_or_with_1_downloads_use_the_add_on(
    world: DeliveryWorld, fresh: None, value: Any
) -> None:
    addon = world.addon("Willing")
    if value is not None:
        addon.manifest_extra = {"allowDownloads": value}
    world.add_source(addon)
    song, _, _ = world.placeholder_track(f"nd-willing-{value!r}", [addon])
    answer = world.client().request("download", {"id": song})
    assert answer.status_code == 200 and answer.content[:4] == b"fLaC"
    assert row(world, song)["state"] == "delivered"
    assert addon.requests("audio")
