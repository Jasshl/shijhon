"""Suite K (continued) - what an app receives when an add-on's audio answer is broken.

Each test drives one kind of broken answer and records what the app gets when Shijhon
holds none of the song's audio (else it is served from that); the notes say
what headless Chromium (159) did with it, which is what a web player's "paused" depends on:
it raises a media error - and a web player that gives up after a few retries pauses - for
a Subsonic error answer, never for the transport faults below, which it re-requests or
plays through.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Iterator
from urllib.parse import urlencode

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon
from tests.harness.library import frequency_for, tone
from tests.harness.logs import collected

SECONDS = 20


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(), tmp_path_factory.mktemp("broken"), warm_ahead_depth=0
    ) as w:
        # What an add-on's answers do when Shijhon holds none of the song's audio: what it
        # holds is served first (test_K_held_audio.py).
        retention = w.services.deliverer.retention
        assert retention is not None
        retention.holding = False
        yield w


@pytest.fixture
def a(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("A")
    world.add_source(addon)
    yield addon
    world.clear_sources()


def stream(world: DeliveryWorld, song: str, wanted: str) -> httpx.Response:
    return world.client().request("stream", {"id": song}, headers={"range": wanted})


def test_a_range_answered_from_another_offset_is_passed_on_as_the_add_on_said(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """Its Content-Range says where it starts, and the body matches it (Chromium takes
    it so; an app that holds the range to the one it asked for gets another one)."""
    song, fakes, audio = world.placeholder_track("broken-shift", [a], seconds=SECONDS)
    fakes[a].range_shift = 4096
    data = audio.read_bytes()
    assert stream(world, song, "bytes=0-").status_code == 206
    middle = len(data) // 2
    answer = stream(world, song, f"bytes={middle}-")
    assert answer.status_code == 206
    assert answer.headers["content-range"] == f"bytes {middle - 4096}-{len(data) - 1}/{len(data)}"
    assert answer.content == data[middle - 4096 :]


def test_a_range_whose_length_says_more_than_it_sends_reaches_the_app_cut_short(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """Its Content-Length is passed on: the app's read ends early (Chromium: a length
    mismatch, then it asks again from where it stopped)."""
    song, fakes, audio = world.placeholder_track("broken-length", [a], seconds=SECONDS)
    fakes[a].range_length_extra = 5000
    data = audio.read_bytes()
    middle = len(data) // 2
    with pytest.raises(httpx.RemoteProtocolError):
        stream(world, song, f"bytes={middle}-")


def test_an_answer_cut_short_after_its_start_continues_from_where_it_stopped(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """The app's read ends early; its next range, from where it stopped, gets the same
    file (Chromium does exactly that, without an error)."""
    song, fakes, audio = world.placeholder_track("broken-cut", [a], seconds=SECONDS)
    data = audio.read_bytes()
    cut = len(data) // 3
    fakes[a].truncate_after = cut
    received = b""
    answer = world.client().request(
        "stream", {"id": song}, headers={"range": "bytes=0-"}, stream=True
    )
    with pytest.raises(httpx.RemoteProtocolError):
        for chunk in answer.iter_raw():
            received += chunk
    answer.close()
    assert received == data[: len(received)] and len(received) <= cut
    fakes[a].truncate_after = None
    rest = stream(world, song, f"bytes={len(received)}-")
    assert rest.status_code == 206 and rest.content == data[len(received) :]


def test_a_range_of_a_file_of_untold_size_gets_the_whole_file(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """An add-on that ignores ranges and tells no size: a range from an offset gets the
    whole file from its start, with a 200 (Chromium: the song cannot be sought; a seek
    starts it over, without an error)."""
    song, fakes, audio = world.placeholder_track("broken-untold", [a], seconds=SECONDS)
    fakes[a].ranges = False
    fakes[a].untold_size = True
    data = audio.read_bytes()
    answer = stream(world, song, f"bytes={len(data) // 2}-")
    assert answer.status_code == 200 and "content-range" not in answer.headers
    assert answer.content == data


def test_another_file_of_the_same_size_without_validators_is_not_told_apart(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """Without an ETag only the size tells the file: a later range of a file the add-on
    replaced by another of the same size gets the other's bytes (Chromium decodes on)."""
    song, fakes, audio = world.placeholder_track("broken-changed", [a], seconds=SECONDS)
    fakes[a].etag = False
    data = audio.read_bytes()
    assert stream(world, song, "bytes=0-").status_code == 206
    other = tone(frequency_for("broken-changed-other"), SECONDS * 2, "flac").read_bytes()
    replaced = world.tmp / "broken-changed-replaced.flac"
    replaced.write_bytes((other * 2)[: len(data)])
    fakes[a].audio = replaced
    middle = len(data) // 2
    answer = stream(world, song, f"bytes={middle}-")
    assert answer.status_code == 206 and answer.content == replaced.read_bytes()[middle:]


def test_a_later_range_whose_link_cannot_be_renewed_is_a_subsonic_error(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """The song's link expired and the add-on refuses a new one: a seek gets Navidrome's
    kind of error answer - which Chromium reports as a media error (a network error), so a
    web player retries, from the song's start, and pauses after a few failures."""
    song, fakes, audio = world.placeholder_track("broken-renewal", [a], seconds=SECONDS)
    fakes[a].expire_after = 1
    data = audio.read_bytes()
    assert stream(world, song, "bytes=0-").status_code == 206
    fakes[a].stream_status = 503
    answer = stream(world, song, f"bytes={len(data) // 2}-")
    assert answer.status_code == 200
    error = answer.json()["subsonic-response"]
    assert error["status"] == "failed" and "audio unavailable" in error["error"]["message"]


def test_an_app_that_closed_its_connection_before_the_answer_is_logged_as_such(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """The app closes its socket while the song is still being looked up (the add-on takes
    1.5 s): the server (uvicorn) tells Shijhon, the routing goes on for the next request, and
    the log line says that no start was sent and when the disconnect was seen - never
    that an answer went out after it."""
    song, fakes, _ = world.placeholder_track("broken-gone", [a], seconds=SECONDS)
    fakes[a].resolve_delay = 1.5
    client = world.client(client="gone-app")
    query = urlencode([*client.auth_params(), ("id", song)])
    host, port = world.server.base_url.removeprefix("http://").split(":")
    with collected("shijhon.access") as access:
        with socket.create_connection((host, int(port))) as raw:
            raw.sendall(
                f"GET /rest/stream?{query} HTTP/1.1\r\nHost: {host}\r\nRange: bytes=0-\r\n"
                "\r\n".encode()
            )
            time.sleep(0.3)
        deadline = time.monotonic() + 5
        while not [line for line in access if "c=gone-app" in line]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    [line] = [line for line in access if "c=gone-app" in line]
    assert re.search(r"GET 0 \d+ms, no start sent, disconnect seen \d+ms$", line), line
    seen = int(re.search(r"disconnect seen (\d+)ms", line).group(1))  # type: ignore[union-attr]
    assert 250 <= seen < 1500  # when it left, not when the add-on answered
