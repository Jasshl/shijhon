"""Suite K - songs found through an add-on's search.

An add-on that declares ``search`` but no ``resolve`` is asked ``GET /search?q=<artist>
<title>`` where ``/resolve`` would be (after its ISRC lookup, when it has one). Its tracks
are held to the rules of ``/resolve``: the same title, artist, length (within 3 s) and
version, the ISRC settling it when a track carries the wanted one. The best of them plays;
none rather than a doubtful one, and the next add-on is asked. An add-on with ``resolve``
is never searched for a song. The search is a lookup at the add-on's limits.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.logs import collected

SEARCH_ONLY = ("search", "stream")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("search-lookup")) as w:
        yield w


@pytest.fixture
def fresh(world: DeliveryWorld) -> Iterator[None]:
    world.clear_sources()
    yield
    world.clear_sources()


def stream(world: DeliveryWorld, song: str) -> httpx.Response:
    return world.client().request("stream", {"id": song})


def played(response: httpx.Response) -> bytes | None:
    """The audio an answer carried; None for an error."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return None
    return response.content


def track(key: str, ident: str, **fields: Any) -> dict[str, Any]:
    """A search's track for the placeholder ``key`` (``placeholder_track``: its title,
    artist and 3 s), with ``fields`` changed."""
    item = {"id": ident, "title": f"Title {key} Song 1", "artist": f"Artist {key}",
            "duration": 3}  # fmt: skip
    return item | fields


def other(world: DeliveryWorld, addon: FakeAddon, ident: str, n: int) -> bytes:
    """Another recording at ``addon``, under the ID ``ident``, with audio of its own: if
    it were taken for the song, its audio would play."""
    audio = world.audio(f"other-{ident}")
    addon.add(FakeTrack(isrc=f"ZZOTHER{n:05d}", audio=audio, track_id=ident))
    return audio.read_bytes()


def test_an_add_on_with_only_search_and_stream_plays_the_right_recording(
    world: DeliveryWorld, fresh: None
) -> None:
    addon = world.addon("Searcher", resources=SEARCH_ONLY)
    world.add_source(addon)
    key = "ks-only"
    song, _, audio = world.placeholder_track(key, [addon], track_id="right-" + key)
    other(world, addon, "live-" + key, 1)
    addon.search_tracks = [
        track(key, "live-" + key, title=f"Title {key} Song 1 (Live)"),
        track(key, "right-" + key),
    ]
    assert played(stream(world, song)) == audio.read_bytes()
    (search,) = addon.requests("search")
    assert search["params"]["q"] == f"Artist {key} Title {key} Song 1"
    assert [r["key"] for r in addon.requests("stream")] == ["right-" + key]
    assert not addon.requests("resolve-isrc") and not addon.requests("resolve")


@pytest.mark.parametrize(
    "case,fields",
    [
        ("length", {"duration": 7}),
        ("version", {"title": "Title ks-refused-version Song 1 - Radio Edit"}),
        ("artist", {"artist": "Morrow Lane"}),
        ("clean", {"title": "Title ks-refused-clean Song 1 (Clean)"}),
    ],
)
def test_another_recording_is_refused_and_the_next_add_on_plays(
    world: DeliveryWorld, fresh: None, case: str, fields: dict[str, Any]
) -> None:
    searcher = world.addon("Searcher", resources=SEARCH_ONLY)
    backup = world.addon("Backup")
    world.add_source(searcher)
    world.add_source(backup)
    key = f"ks-refused-{case}"
    song, _, audio = world.placeholder_track(key, [backup])
    other(world, searcher, "near-" + key, 2)
    searcher.search_tracks = [track(key, "near-" + key, **fields)]
    with collected("shijhon.delivery.playback") as lines:
        assert played(stream(world, song)) == audio.read_bytes()
    assert len(searcher.requests("search")) == 1
    assert not searcher.requests("stream")  # never asked for the other recording's link
    assert backup.requests("resolve-isrc")
    # The routing's log line says why the add-on was passed over (no address in it).
    (line,) = [line for line in lines if "a match was rejected: its" in line]
    assert "Searcher: not available" in line and "http" not in line
    # With no other add-on, the play fails, and says that a match was rejected.
    world.clear_sources()
    world.add_source(searcher)
    alone, _, _ = world.placeholder_track(key + "-alone", [])
    searcher.search_tracks = [track(key + "-alone", "near-" + key, **fields)]
    with collected("shijhon.delivery.playback") as lines:
        assert played(stream(world, alone)) is None
    assert any("no source had it (a match was rejected)" in line for line in lines)


def test_of_several_results_the_right_one_plays(world: DeliveryWorld, fresh: None) -> None:
    addon = world.addon("Searcher", resources=SEARCH_ONLY)
    world.add_source(addon)
    key = "ks-several"
    song, _, audio = world.placeholder_track(key, [addon], track_id="right-" + key)
    for n, ident in enumerate(("live", "artist", "far"), start=10):
        other(world, addon, f"{ident}-{key}", n)
    addon.search_tracks = [
        track(key, "live-" + key, title=f"Title {key} Song 1 (Live)"),
        track(key, "artist-" + key, artist="Morrow Lane"),
        track(key, "far-" + key, durationMs=5_500),  # acceptable, 2.5 s off
        track(key, "right-" + key, durationMs=3_100),
    ]
    assert played(stream(world, song)) == audio.read_bytes()
    assert [r["key"] for r in addon.requests("stream")] == ["right-" + key]


@pytest.mark.parametrize("signal", ["flag", "album"])
def test_of_clean_and_explicit_twins_the_explicit_one_plays(
    world: DeliveryWorld, fresh: None, signal: str
) -> None:
    """An explicit and a clean version under one plain title, neither with the wanted
    ISRC: the explicit one, by the tracks' explicit flags or by the clean one's album
    title - although the clean one is the closer in length."""
    addon = world.addon("Searcher", resources=SEARCH_ONLY)
    world.add_source(addon)
    key = f"ks-twins-{signal}"
    song, _, audio = world.placeholder_track(key, [addon], track_id="explicit-" + key)
    other(world, addon, "clean-" + key, 30)
    clean, explicit = (
        ({"explicit": False}, {"explicit": True})
        if signal == "flag"
        else ({"album": f"Album {key} (Edited)"}, {"album": f"Album {key}"})
    )
    addon.search_tracks = [
        track(key, "clean-" + key, durationMs=3_000, **clean),
        track(key, "explicit-" + key, durationMs=3_900, **explicit),
    ]
    assert played(stream(world, song)) == audio.read_bytes()
    assert [r["key"] for r in addon.requests("stream")] == ["explicit-" + key]


def test_an_isrc_in_a_result_settles_it(world: DeliveryWorld, fresh: None) -> None:
    """The track that carries the wanted ISRC is the recording - before a twin by title,
    artist and length, and without a length of its own."""
    addon = world.addon("Searcher", resources=SEARCH_ONLY)
    world.add_source(addon)
    key = "ks-isrc"
    song, fakes, audio = world.placeholder_track(key, [addon], track_id="right-" + key)
    other(world, addon, "twin-" + key, 20)
    right = track(key, "right-" + key, isrc=fakes[addon].isrc)
    del right["duration"]
    addon.search_tracks = [track(key, "twin-" + key), right]
    assert played(stream(world, song)) == audio.read_bytes()
    assert [r["key"] for r in addon.requests("stream")] == ["right-" + key]


@pytest.mark.parametrize("status", [503, 429, 404])
def test_a_search_error_falls_through_to_the_next_add_on(
    world: DeliveryWorld, fresh: None, status: int
) -> None:
    searcher = world.addon("Searcher", resources=SEARCH_ONLY)
    backup = world.addon("Backup")
    searcher_id = world.add_source(searcher)
    world.add_source(backup)
    key = f"ks-error-{status}"
    song, _, audio = world.placeholder_track(key, [searcher, backup], track_id="right-" + key)
    searcher.search_tracks = [track(key, "right-" + key)]
    searcher.search_status = status
    assert played(stream(world, song)) == audio.read_bytes()
    assert len(searcher.requests("search")) == 1 and not searcher.requests("stream")
    assert backup.requests("stream")
    # "Too many requests" leaves the add-on alone, as after any lookup's.
    assert world.services.sources.cooling(searcher_id) is (status == 429)


def test_an_add_on_that_declares_resolve_is_never_searched(
    world: DeliveryWorld, fresh: None
) -> None:
    resolver = world.addon("Resolver", resources=("stream", "isrc", "resolve", "search"))
    backup = world.addon("Backup")
    world.add_source(resolver)
    world.add_source(backup)
    key = "ks-resolver"
    song, fakes, audio = world.placeholder_track(key, [resolver, backup], available=False)
    fakes[backup].available = True
    resolver.search_tracks = [track(key, fakes[resolver].key())]
    assert played(stream(world, song)) == audio.read_bytes()
    assert resolver.requests("resolve-isrc") and resolver.requests("resolve")
    assert not resolver.requests("search") and not resolver.requests("stream")


def test_the_search_is_a_request_at_the_add_on_s_limit(world: DeliveryWorld, fresh: None) -> None:
    addon = world.addon("Searcher", resources=SEARCH_ONLY)
    world.add_source(addon)
    key = "ks-paced"
    song, _, audio = world.placeholder_track(key, [addon], track_id="right-" + key)
    addon.search_tracks = [track(key, "right-" + key)]
    assert played(stream(world, song)) == audio.read_bytes()
    api = [r["endpoint"] for r in addon.requests() if r["endpoint"] != "audio"]
    assert api == ["manifest", "search", "stream"]
    (source,) = world.server.call(world.services.sources.enabled)
    assert source.pace is not None and source.pace.sent == 3
