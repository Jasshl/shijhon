"""Suite K — delivery from direct sources.

Direct and redirected sources stream with correct ranges; lenient parsing of real-world
add-on answers; pinning never switches source during seeks; re-resolve only at byte zero;
fallback within the time budget; cooldown after rate limits; settings sent on every
request; the ``/resolve`` fallback is accepted only on a title/artist/duration match;
download-first serves bitrate/format requests through Navidrome in the delivered format.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx
import pytest

from shijhon.delivery.playback import _Changed, _Error
from shijhon.navidrome.client import NavidromeError
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.subsonic import SubsonicClient


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    with delivery_world(nd, tmp_path_factory.mktemp("k")) as w:
        yield w


@pytest.fixture
def a_and_b(world: DeliveryWorld) -> Iterator[tuple[FakeAddon, FakeAddon]]:
    """Two fresh add-ons, A before B, as the only sources."""
    world.clear_sources()
    a, b = world.addon("A"), world.addon("B")
    world.add_source(a)
    world.add_source(b)
    yield a, b
    world.clear_sources()


def stream(
    world: DeliveryWorld, song: str, headers: dict[str, str] | None = None, **params: object
) -> httpx.Response:
    return world.client().request("stream", {"id": song, **params}, headers=headers)


def failed(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json") and (
        response.json()["subsonic-response"]["status"] == "failed"
    )


def test_whole_file_ranges_and_head(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    song, _, audio = world.placeholder_track("k-basic", [a])
    data = audio.read_bytes()
    whole = stream(world, song)
    assert whole.status_code == 200 and whole.content == data
    assert whole.headers["content-type"] == "audio/flac"
    assert whole.headers["content-length"] == str(len(data))
    for header, expected in (
        ("bytes=0-99", data[:100]),
        ("bytes=100-", data[100:]),
        ("bytes=-64", data[-64:]),
        ("bytes=10-10", data[10:11]),
    ):
        part = stream(world, song, {"range": header})
        assert part.status_code == 206 and part.content == expected, header
        assert part.headers["content-length"] == str(len(expected))
    assert stream(world, song, {"range": f"bytes={len(data) + 10}-"}).status_code == 416
    head = world.client().request("stream", {"id": song}, http_method="HEAD")
    assert head.status_code == 200 and head.headers["content-length"] == str(len(data))
    assert head.content == b""
    post = world.client().request("stream.view", {"id": song}, http_method="POST")
    assert post.content == data


def test_cross_origin_redirect(world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]) -> None:
    a, _ = a_and_b
    song, _, audio = world.placeholder_track("k-redirect", [a], redirect=True)
    assert stream(world, song, {"range": "bytes=5-20"}).content == audio.read_bytes()[5:21]
    assert a.requests("redirect")
    assert {r["origin"] for r in a.requests("audio")} == {"cdn"}


@pytest.mark.parametrize(
    "variant",
    [
        {"track_id": 12345},
        {"stream_extra": {"format": "", "codec": None, "expiresAt": None, "quality": ""}},
        {"wrap": "stream"},
        {"wrap": "streams"},
        {"etag": False},
        {"content_type": "application/octet-stream", "stream_extra": {"format": "flac"}},
    ],
    ids=[
        "numeric-id",
        "null-and-empty-fields",
        "wrapped",
        "wrapped-list",
        "no-etag",
        "generic-type",
    ],
)
def test_lenient_answers(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], variant: dict[str, object]
) -> None:
    a, _ = a_and_b
    key = "k-lenient-" + str(abs(hash(repr(sorted(variant.items())))) % 10**8)
    song, _, audio = world.placeholder_track(key, [a], **variant)
    response = stream(world, song, {"range": "bytes=0-"})
    assert response.content == audio.read_bytes()
    assert response.headers["content-type"] == "audio/flac"


def test_non_ranged_source_is_sliced(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    song, _, audio = world.placeholder_track("k-noranges", [a], ranges=False)
    data = audio.read_bytes()
    part = stream(world, song, {"range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == data[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(data)}"


def test_seeks_stay_pinned_to_one_source(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    song, _, audio = world.placeholder_track("k-pin", [a, b])
    data = audio.read_bytes()
    assert stream(world, song).content == data
    for header in ("bytes=1000-", "bytes=50-60", "bytes=0-"):
        assert stream(world, song, {"range": header}).status_code == 206
    audio_requests = a.requests("audio")
    assert len(audio_requests) == 4
    assert all(r["if_match"] for r in audio_requests[1:])  # pinned by strong ETag
    assert b.requests("audio") == [] and b.requests("stream") == []
    assert len(a.requests("stream")) == 1  # one resolution per play


def test_an_expired_link_is_renewed_at_the_play_s_source(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """A seek after the link stopped working: a fresh link from the same source, accepted
    only for the same representation."""
    a, b = a_and_b
    song, _, audio = world.placeholder_track("k-expire", [a, b], expire_after=1, expire_status=410)
    data = audio.read_bytes()
    assert stream(world, song).content == data  # first request uses the URL once
    seek = stream(world, song, {"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == data[100:]
    assert a.requests("audio")[-1]["if_match"]  # the same representation
    again = stream(world, song, {"range": "bytes=0-"})
    assert again.content == data  # byte zero: re-resolved to a fresh URL
    assert len(a.requests("stream")) == 3
    assert b.requests() == []


def library_file(world: DeliveryWorld, song: str) -> bytes:
    """The song's file in the library, once its audio is in place."""

    async def path() -> str:
        row = await world.services.store.fetchone(
            "SELECT state, path FROM placeholders WHERE song_id = ?", [song]
        )
        assert row is not None and row["state"] == "delivered"
        return str(row["path"])

    return world.services.engine.layout.absolute(world.server.call(path)).read_bytes()


def test_a_play_keeps_its_file_when_the_song_is_saved_while_it_plays(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """An app downloads the song while it plays: the library's copy is retagged, another
    file. The play's seeks get its own file's bytes from its link; a new play gets the
    library's file, and so do its seeks and another app's. Once the link expires, the
    play's seeks are the library's too."""
    a, b = a_and_b
    song, _, audio = world.placeholder_track("k-saved", [a, b])
    data = audio.read_bytes()
    assert stream(world, song, {"range": "bytes=0-99"}).content == data[:100]
    assert world.client().request("download", {"id": song}).status_code == 200
    library = library_file(world, song)
    assert library[100:] != data[100:]
    before = len(a.requests("audio"))
    seek = stream(world, song, {"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == data[100:]
    assert seek.headers["content-range"] == f"bytes 100-{len(data) - 1}/{len(data)}"
    assert len(a.requests("audio")) == before + 1  # from its link (no new one)
    assert len(a.requests("stream")) == 1
    assert b.requests("stream") == [] and b.requests("audio") == []
    other = world.client(client="other-app")
    seek = other.request("stream", {"id": song}, headers={"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == library[100:]
    settings = world.services.deliverer.settings
    ttl = settings.pin_ttl_seconds
    settings.pin_ttl_seconds = 0.0  # the song's link expires now
    try:
        seek = stream(world, song, {"range": "bytes=100-"})
    finally:
        settings.pin_ttl_seconds = ttl
    assert seek.status_code == 206 and seek.content == library[100:]
    assert stream(world, song).content == library  # a new play
    assert stream(world, song, {"range": "bytes=100-"}).content == library[100:]
    assert len(a.requests("audio")) == before + 1 and len(a.requests("stream")) == 1


def test_a_play_first_asked_for_mid_file_keeps_its_file_too(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """An app resumes the song at a later range (it had its start already): served from
    the add-ons, that play keeps its file after the song is saved."""
    a, _ = a_and_b
    song, _, audio = world.placeholder_track("k-saved-resumed", [a])
    data = audio.read_bytes()
    resumed = world.client(client="resuming-app")
    first = resumed.request("stream", {"id": song}, headers={"range": "bytes=100-199"})
    assert first.status_code == 206 and first.content == data[100:200]
    assert world.client().request("download", {"id": song}).status_code == 200
    seek = resumed.request("stream", {"id": song}, headers={"range": "bytes=200-"})
    assert seek.status_code == 206 and seek.content == data[200:]
    assert library_file(world, song)[200:] != data[200:]


def test_seeks_during_the_play_s_link_renewal_after_the_save_keep_its_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """After the save, the play's link stops working: a seek asks for a new link (a slow
    one) to the same file, and another seek of the play meanwhile waits for it - both
    get the play's bytes, never the library's."""
    a, _ = a_and_b
    song, fakes, audio = world.placeholder_track(
        "k-saved-renewed", [a], expire_after=2, expire_status=410
    )
    data = audio.read_bytes()
    assert stream(world, song, {"range": "bytes=0-99"}).content == data[:100]
    assert world.client().request("download", {"id": song}).status_code == 200
    fakes[a].resolve_delay = 1.0  # the new link is slow
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(stream, world, song, {"range": "bytes=100-"})
        time.sleep(0.4)
        second = pool.submit(stream, world, song, {"range": "bytes=200-"})
        answers = [first.result(timeout=20), second.result(timeout=20)]
    assert [r.status_code for r in answers] == [206, 206]
    assert answers[0].content == data[100:] and answers[1].content == data[200:]
    assert len(a.requests("stream")) == 2  # the play's link, and one new one
    assert library_file(world, song)[200:] != data[200:]


def test_a_new_play_after_the_save_keeps_to_the_library_s_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """The play's own app starts the song again once it is in the library: that play and
    its seeks are the library's, while the song's link is still kept."""
    a, _ = a_and_b
    song, _, audio = world.placeholder_track("k-saved-again", [a])
    data = audio.read_bytes()
    assert stream(world, song).content == data
    assert world.client().request("download", {"id": song}).status_code == 200
    library = library_file(world, song)
    assert stream(world, song, {"range": "bytes=0-1"}).content == library[:2]  # a new play
    seek = stream(world, song, {"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == library[100:]

    async def kept() -> bool:
        return world.services.deliverer.pinned(song) is not None

    assert world.server.call(kept)  # (its link is kept meanwhile)


def test_fallback_within_budget(world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]) -> None:
    a, b = a_and_b
    song, fakes, audio = world.placeholder_track("k-fallback", [a, b])
    fakes[a].resolve_delay = 5.0
    started = time.monotonic()
    response = stream(world, song)
    elapsed = time.monotonic() - started
    assert response.content == audio.read_bytes()
    assert elapsed < 3.5  # budget 2.5 s, first attempt cut at half of it (A needs 5 s)
    assert b.requests("audio")


def test_everything_slow_fails_within_budget(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    song, _, _ = world.placeholder_track("k-slow", [a, b], resolve_delay=5.0)
    started = time.monotonic()
    response = stream(world, song)
    assert failed(response)
    assert time.monotonic() - started < 4.0  # both would need 5 s


def test_slow_first_byte_counts_against_the_budget(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    song, fakes, audio = world.placeholder_track("k-firstbyte", [a, b])
    fakes[a].first_byte_delay = 5.0
    started = time.monotonic()
    assert stream(world, song).content == audio.read_bytes()
    assert time.monotonic() - started < 3.5  # A's first byte would need 5 s
    assert b.requests("audio")


def test_cooldown_after_rate_limit(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], caplog: pytest.LogCaptureFixture
) -> None:
    a, b = a_and_b
    first, fakes, audio = world.placeholder_track("k-rate-1", [a, b])
    fakes[a].rate_limit_streams = 1  # only A is rate limited
    with caplog.at_level("INFO", logger="shijhon.delivery.playback"):
        assert stream(world, first).content == audio.read_bytes()
    assert b.requests("audio")  # served by the fallback
    assert "source A rate limited (Retry-After 1s); not asked for 1s" in caplog.messages
    second, _, audio2 = world.placeholder_track("k-rate-2", [a, b])
    a.clear()
    assert stream(world, second).content == audio2.read_bytes()
    assert a.requests() == []  # cooling down: not even asked
    time.sleep(1.6)
    third, _, audio3 = world.placeholder_track("k-rate-3", [a, b])
    assert stream(world, third).content == audio3.read_bytes()
    assert a.requests("audio")


def test_nothing_available_fails_cleanly(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    song, _, _ = world.placeholder_track("k-none", [a, b], available=False)
    response = stream(world, song)
    assert response.status_code == 200 and failed(response)
    xml = world.client(fmt="xml").request("stream", {"id": song})
    assert b'status="failed"' in xml.content


def test_settings_are_sent_on_every_request(world: DeliveryWorld) -> None:
    world.clear_sources()
    addon = world.addon(
        "Configured",
        settings=[
            {"key": "quality", "type": "select", "default": "LOSSLESS"},
            {"key": "mirror", "type": "text", "default": "north"},
        ],
    )
    world.add_source(addon, settings={"mirror": "south", "accessKey": "secret-value"})
    song, _, audio = world.placeholder_track("k-settings", [addon])
    assert stream(world, song).content == audio.read_bytes()
    for endpoint in ("resolve-isrc", "stream"):
        [request] = addon.requests(endpoint)
        assert request["params"]["quality"] == "LOSSLESS"
        assert request["params"]["mirror"] == "south"
        assert request["params"]["accessKey"] == "secret-value"
    world.clear_sources()


@pytest.mark.parametrize(
    "offset_ms,title_suffix,accepted",
    [(1500, "", True), (-2900, "", True), (4000, "", False), (0, " (Live)", False)],
)
def test_resolve_fallback_requires_a_match(
    world: DeliveryWorld, offset_ms: int, title_suffix: str, accepted: bool
) -> None:
    world.clear_sources()
    addon = world.addon("Resolver", resources=("stream", "isrc", "resolve"))
    world.add_source(addon)
    key = f"k-resolve-{offset_ms}-{bool(title_suffix)}"
    song, fakes, audio = world.placeholder_track(key, [addon], available=False)
    fake = fakes[addon]
    fake.track_id = "resolved-" + key
    addon.by_key[fake.track_id] = fake
    fake.resolve_item = {
        "id": fake.track_id,
        "type": "track",
        "title": f"Title {key} Song 1{title_suffix}",
        "artist": f"Artist {key}",
        "durationMs": 3000 + offset_ms,
    }
    response = stream(world, song)
    assert (response.content == audio.read_bytes()) is accepted
    assert failed(response) is not accepted
    world.clear_sources()


@pytest.mark.parametrize(
    "case,accepted",
    [("clean", False), ("no length, same ISRC", True), ("no length, other ISRC", False)],
)
def test_resolve_fallback_version_and_missing_length(
    world: DeliveryWorld, case: str, accepted: bool
) -> None:
    """A clean edit is other audio; an item without a length only when its ISRC is the
    wanted one (an add-on's /resolve that sends no length)."""
    world.clear_sources()
    addon = world.addon("Resolver", resources=("stream", "isrc", "resolve"))
    world.add_source(addon)
    key = "k-resolve-" + case.replace(" ", "-").replace(",", "")
    song, fakes, audio = world.placeholder_track(key, [addon], available=False)
    fake = fakes[addon]
    fake.track_id = "resolved-" + key
    addon.by_key[fake.track_id] = fake
    item = {"id": fake.track_id, "title": f"Title {key} Song 1", "artist": f"Artist {key}"}
    if case == "clean":
        item |= {"title": f"Title {key} Song 1 (Clean)", "durationMs": 3000}
    else:
        item["isrc"] = fake.isrc if "same" in case else "ZZ0000000000"
    fake.resolve_item = item
    response = stream(world, song)
    assert (response.content == audio.read_bytes()) is accepted
    assert failed(response) is not accepted
    world.clear_sources()


def test_download_first_for_other_formats_keeps_the_delivered_format(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    song, _, _ = world.placeholder_track("k-transcode", [a], fmt="m4a", content_type="audio/mp4")
    response = stream(world, song, maxBitRate=96, format="mp3")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg"
    assert len(response.content) > 1000
    info = world.client().ok("getSong", {"id": song})["song"]
    assert info["suffix"] == "m4a"  # delivered format kept, same song ID
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT state FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert row is not None and row["state"] == "delivered"
    # Afterwards Navidrome serves it for plain requests too; the add-on is not asked again.
    a.clear()
    raw = stream(world, song)
    assert raw.headers["content-type"] == "audio/mp4" and a.requests() == []


def test_disconnect_stops_the_upstream_download(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    song, fakes, audio = world.placeholder_track("k-disconnect", [a], seconds=60, chunk_delay=0.05)
    size = audio.stat().st_size
    assert size > 16384 * 20
    client = world.client()
    query = str(httpx.QueryParams([*client.auth_params(), ("id", song)]))
    with socket.create_connection(("127.0.0.1", world.server.port)) as sock:
        sock.sendall(f"GET /rest/stream?{query} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        sock.recv(4096)
    time.sleep(1.0)
    sent = a.bytes_sent.get(fakes[a].isrc, 0)
    time.sleep(0.5)
    assert a.bytes_sent.get(fakes[a].isrc, 0) == sent < size


# --- regressions ------------------------------------------------------------------------


def test_changing_the_source_list_keeps_pinned_plays(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    song, _, audio = world.placeholder_track("k-reconfigure", [a])
    data = audio.read_bytes()
    assert stream(world, song).content == data
    extra = world.addon("Extra")
    world.add_source(extra)  # rebuilds the registry's clients
    seek = stream(world, song, {"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == data[100:]


def test_a_resumed_play_never_switches_to_another_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    song, fakes, audio = world.placeholder_track("k-resume", [a])
    assert stream(world, song).content == audio.read_bytes()
    world.server.call(lambda: _forget(world, song))  # e.g. the pin expired while paused
    fakes[a].audio = world.audio("k-resume-other", seconds=5)  # the source now has another file
    assert failed(stream(world, song, {"range": "bytes=100-"}))
    fresh = stream(world, song)  # a new play at byte zero takes the new file
    assert fresh.content == fakes[a].audio.read_bytes()


async def _forget(world: DeliveryWorld, song: str) -> None:
    world.services.deliverer.forget(song)


def test_a_late_failure_of_a_link_keeps_the_newer_link_found_meanwhile(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two seeks share the play's link, which fails for both: the first gets a fresh link
    from the play's source; the second's failure comes later and must not drop that newer
    link - it uses it too (no third link)."""
    a, _ = a_and_b
    song, _, audio = world.placeholder_track("k-late-failure", [a])
    data = audio.read_bytes()
    assert stream(world, song).content == data
    deliverer = world.services.deliverer
    old = deliverer.pinned(song)
    assert old is not None
    real = deliverer._request
    failures: list[Any] = []

    async def request(pin: Any, ask: Any, head: bool, limit: float, *more: Any, **kw: Any) -> Any:
        if pin is old:  # the play's link now fails, for both seeks
            failures.append(pin)
            with anyio.fail_after(10):
                if len(failures) == 1:  # the first: once the second uses that link too
                    while len(failures) < 2:  # noqa: ASYNC110
                        await anyio.sleep(0.01)
                else:  # the second: only after the first has found a newer link
                    while deliverer.pinned(song) in (None, old):  # noqa: ASYNC110
                        await anyio.sleep(0.01)
            raise _Error("the link answered HTTP 500")
        return await real(pin, ask, head, limit, *more, **kw)

    monkeypatch.setattr(deliverer, "_request", request)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(stream, world, song, {"range": "bytes=100-199"})
        second = pool.submit(stream, world, song, {"range": "bytes=200-299"})
        answers = first.result(), second.result()
    assert [r.content for r in answers] == [data[100:200], data[200:300]]
    assert len(failures) == 2
    assert len(a.requests("stream")) == 2  # the play's link and the first seek's, no third
    newer = deliverer.pinned(song)
    assert newer is not None and newer is not old


def test_a_late_seek_never_takes_another_request_s_link_to_another_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seek's link fails late - after a new play of the song at byte zero found another
    file at the source meanwhile: the seek does not take that play's link to the other file,
    it looks for its own file and fails when that is gone."""
    a, _ = a_and_b
    song, fakes, audio = world.placeholder_track("k-late-other-file", [a])
    assert stream(world, song).content == audio.read_bytes()
    deliverer = world.services.deliverer
    old = deliverer.pinned(song)
    assert old is not None
    other = world.audio("k-late-other-file-new", seconds=5)
    fakes[a].audio = other  # the source has another file now
    real = deliverer._request
    seeking: list[Any] = []

    async def request(pin: Any, ask: Any, head: bool, limit: float, *more: Any, **kw: Any) -> Any:
        if pin is old:  # the play's link has expired
            if ask is not None:  # the seek: its answer comes after the new play's link
                seeking.append(pin)
                with anyio.fail_after(10):
                    while deliverer.pinned(song) in (None, old):  # noqa: ASYNC110
                        await anyio.sleep(0.01)
            raise _Changed("HTTP 410", 410)
        return await real(pin, ask, head, limit, *more, **kw)

    monkeypatch.setattr(deliverer, "_request", request)
    with ThreadPoolExecutor(2) as pool:
        seek = pool.submit(stream, world, song, {"range": "bytes=100-199"})
        deadline = time.monotonic() + 10
        while not seeking:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        play = pool.submit(stream, world, song)  # a new play at byte zero
        answers = seek.result(), play.result()
    assert answers[1].content == other.read_bytes()  # the new play: the new file
    assert failed(answers[0])  # the seek: never the other file's bytes


def test_a_seek_never_takes_a_new_play_s_unanswered_link_to_another_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new play of the song has found a link - to another file - but its audio has not
    answered yet when a seek of the earlier play comes: the seek checks that link's answer
    against its own file for itself (never taking the other file's bytes, never spoiling
    the link for the new play) and looks for its own file, failing when that is gone."""
    a, _ = a_and_b
    song, fakes, audio = world.placeholder_track("k-seek-unanswered", [a])
    assert stream(world, song).content == audio.read_bytes()
    deliverer = world.services.deliverer
    old = deliverer.pinned(song)
    assert old is not None
    other = world.audio("k-seek-unanswered-new", seconds=5)
    fakes[a].audio = other  # the source has another file now
    real = deliverer._request
    seek_done: list[bool] = []

    async def request(pin: Any, ask: Any, head: bool, limit: float, *more: Any, **kw: Any) -> Any:
        if pin is old:  # the play's link has expired
            raise _Changed("HTTP 410", 410)
        if ask is None:  # the new play's audio: answered only once the seek is over
            with anyio.fail_after(10):
                while not seek_done:  # noqa: ASYNC110
                    await anyio.sleep(0.01)
        return await real(pin, ask, head, limit, *more, **kw)

    monkeypatch.setattr(deliverer, "_request", request)
    # (The fake add-on ends a link when it hands out the next: the new play gets another.)
    monkeypatch.setattr(deliverer.settings, "max_attempts", 4)
    with ThreadPoolExecutor(2) as pool:
        play = pool.submit(stream, world, song)  # a new play at byte zero: a new link
        deadline = time.monotonic() + 10
        while deliverer._pins.get(song) in (None, old):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        new = deliverer._pins[song]
        seek = stream(world, song, {"range": "bytes=100-199"})
        took = (new.size, new.etag)  # what the seek left of the new play's link
        seek_done.append(True)
        played = play.result()
    assert failed(seek)  # its own file is gone: never the other file's bytes
    assert took[0] in (None, other.stat().st_size) and took[1] != old.etag  # not the old file's
    assert played.content == other.read_bytes()  # the new play: its file, whole


def test_malformed_stream_url_falls_back(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    song, fakes, audio = world.placeholder_track("k-badurl", [a, b])
    fakes[a].stream_extra = {"url": "https://[::1/broken"}
    response = stream(world, song)
    assert response.content == audio.read_bytes()
    assert b.requests("audio")


def test_broken_download_is_not_a_server_error(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, _ = a_and_b
    song, _, _ = world.placeholder_track("k-truncated", [a], truncate_after=20_000)
    params = {"id": song, "maxBitRate": 96, "format": "mp3"}
    response = world.client().request("stream", params, stream=True)
    # Download-first failed, so it falls back to streaming (the source truncates that too,
    # which the client sees as a short body); never a 500.
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/flac"
    response.close()
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT state FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert row is not None and row["state"] == "placeholder"


def test_an_addon_with_its_own_budget_may_take_longer(world: DeliveryWorld) -> None:
    """For example a worker that prepares complete files on the first request."""
    world.clear_sources()
    quick, slow = world.addon("Quick"), world.addon("Preparing")
    world.add_source(quick)
    world.add_source(slow, budget_seconds=6.0)
    song, fakes, audio = world.placeholder_track("k-own-budget", [slow])
    fakes[slow].resolve_delay = 4.0  # longer than the global 2.5 s budget
    started = time.monotonic()
    assert stream(world, song).content == audio.read_bytes()
    assert 4.0 <= time.monotonic() - started < 6.5
    # Without its own budget the same source would be cut off.
    other, fakes2, _ = world.placeholder_track("k-no-own-budget", [quick])
    fakes2[quick].resolve_delay = 4.0
    assert failed(stream(world, other))
    world.clear_sources()


def test_own_budget_after_a_failing_first_source(world: DeliveryWorld) -> None:
    world.clear_sources()
    first, preparing = world.addon("First"), world.addon("Last resort")
    world.add_source(first)
    world.add_source(preparing, budget_seconds=6.0)
    song, fakes, audio = world.placeholder_track("k-own-budget-2", [first, preparing])
    fakes[first].resolve_delay = 5.0  # cut at its share of the global budget
    fakes[preparing].resolve_delay = 3.0  # beyond what is left of the global budget
    assert stream(world, song).content == audio.read_bytes()
    world.clear_sources()


def test_a_single_source_gets_the_whole_budget(world: DeliveryWorld) -> None:
    """With nothing to fall back to, no time is kept back for a fallback."""
    world.clear_sources()
    only = world.addon("Only")
    world.add_source(only)
    song, fakes, audio = world.placeholder_track("k-single-source", [only])
    fakes[only].resolve_delay = 1.8  # more than half of the 2.5 s budget
    assert stream(world, song).content == audio.read_bytes()
    world.clear_sources()


def test_jukebox_disabled_in_navidrome_does_no_work(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """Navidrome answers "not implemented" (501) when its jukebox is off (the default) and
    logs each such request with its full URL, credentials included. Shijhon gives the same
    answer itself - status, headers, body - so those requests never reach Navidrome,
    and fetches nothing for placeholders: whatever the HTTP method, also for a catalog
    song's ID and for a browser's request across origins."""
    a, _ = a_and_b
    song, _, _ = world.placeholder_track("k-jukebox-off", [a])
    log = world.nd.log_path
    catalog_song = "sh.tr.cat.1"  # never looked up: the jukebox is off
    assert world.services.commits is not None
    committed = world.services.commits.materializations

    def logged() -> int:
        return log.read_text(errors="replace").count("jukeboxControl")

    origin = {"Origin": "https://player.example"}
    for method, params, headers in (
        ("GET", {"action": "get"}, {}),
        ("GET", {"action": "get", "f": "json"}, {}),
        ("GET", {"action": "status", "f": "json"}, {}),
        ("GET", {"action": "set", "id": song, "f": "json"}, {}),
        ("POST", {"action": "get", "f": "json"}, {}),  # credentials in the form body
        ("HEAD", {"action": "get"}, {}),
        # A catalog ID: nothing to commit, and still Shijhon's answer.
        ("GET", {"action": "set", "id": catalog_song}, {}),
        ("GET", {"action": "add", "id": [song, catalog_song]}, {}),
        ("GET", {"action": "status", "id": catalog_song}, {}),
        ("POST", {"action": "set", "id": catalog_song}, {}),
        # A browser's request across origins: with the CORS headers Navidrome adds.
        ("GET", {"action": "get", "f": "json"}, origin),
        ("POST", {"action": "status"}, origin),
        ("HEAD", {"action": "get"}, origin),
        ("GET", {"action": "set", "id": catalog_song}, origin),
        ("GET", {"action": "get"}, {"Origin": ""}),
        # Other methods: Navidrome routes them all to the same answer.
        ("PUT", {"action": "get"}, {}),
        ("DELETE", {"action": "get"}, {}),
        ("PATCH", {"action": "get"}, origin),
        ("DELETE", {"action": "set", "id": catalog_song}, origin),
        ("OPTIONS", {"action": "get"}, {}),
        ("OPTIONS", {"action": "get"}, origin),  # no preflight: no method asked about
        # ... nor is one that names no origin: a request like any other for Navidrome.
        ("OPTIONS", {"action": "get"}, {"Access-Control-Request-Method": "GET"}),
        ("TRACE", {"action": "get"}, origin),
    ):
        case = (method, params, headers)
        before = logged()
        direct = world.nd.client(salt="s").request(
            "jukeboxControl", params, http_method=method, headers=headers
        )
        assert logged() > before, case  # Navidrome logs each one it answers
        before = logged()
        proxied = world.client(salt="s").request(
            "jukeboxControl", params, http_method=method, headers=headers
        )
        assert logged() == before, case  # this one never reached Navidrome
        assert direct.status_code == proxied.status_code == 501, case
        assert direct.content == proxied.content, case
        drop = {"date", "server"}
        assert {k: v for k, v in direct.headers.multi_items() if k not in drop} == {
            k: v for k, v in proxied.headers.multi_items() if k not in drop
        }, case
    bare = {"u": ADMIN_USER, "p": ADMIN_PASSWORD, "v": "1.16.1", "action": "get", "f": "json"}
    # A browser's preflight is Navidrome's own to answer (no login in it, nothing logged).
    asking = {**origin, "Access-Control-Request-Method": "GET"}
    before = logged()
    preflight = [
        httpx.request("OPTIONS", f"{base}/rest/jukeboxControl", params=bare, headers=asking)
        for base in (world.nd.base_url, world.server.base_url)
    ]
    assert preflight[0].status_code == preflight[1].status_code == 200
    assert "access-control-allow-methods" in preflight[1].headers and logged() == before
    # A method in small letters reaches Navidrome in capitals: so it counts for Shijhon.
    address = httpx.URL(world.server.base_url)
    form = urlencode([*world.client(salt="s").auth_params(), ("action", "get")]).encode()
    head = (
        f"post /rest/jukeboxControl HTTP/1.1\r\nHost: {address.host}\r\n"
        "Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(form)}\r\nConnection: close\r\n\r\n"
    ).encode()
    answer = b""
    with socket.create_connection((address.host, address.port), timeout=30) as connection:
        connection.sendall(head + form)
        while chunk := connection.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.1 501") and logged() == before
    # A form longer than Navidrome reads, the credentials in the address: Navidrome's own
    # error, which it does not log.
    long_form = {"content-type": "application/x-www-form-urlencoded"}
    logged_so_far = len(log.read_text(errors="replace"))
    too_long = [
        httpx.post(
            f"{base}/rest/jukeboxControl",
            params=bare,
            content=b"pad=" + b"x" * (10 << 20),
            headers=long_form,
            timeout=60,
        )
        for base in (world.nd.base_url, world.server.base_url)
    ]
    assert too_long[0].status_code == too_long[1].status_code == 200
    assert too_long[0].content == too_long[1].content and b"too large" in too_long[1].content
    since = log.read_text(errors="replace")[logged_so_far:]
    assert "request body too large" in since and "jukeboxControl?" not in since  # no address
    # A request Navidrome answers otherwise (no client name: error 10) gets that answer.
    direct_bare = httpx.get(f"{world.nd.base_url}/rest/jukeboxControl", params=bare)
    proxied_bare = httpx.get(f"{world.server.base_url}/rest/jukeboxControl", params=bare)
    assert proxied_bare.status_code == direct_bare.status_code == 200
    assert proxied_bare.json() == direct_bare.json()
    # Wrong credentials: Navidrome's own answer (its credential error), as before.
    stranger = SubsonicClient(world.server.base_url, ADMIN_USER, "wrong")
    wrong = stranger.request("jukeboxControl", {"action": "get"})
    assert wrong.status_code == 200 and b"Wrong username or password" in wrong.content
    assert a.requests() == []
    assert world.services.commits is not None
    assert world.services.commits.materializations == committed


def test_a_jukebox_whose_state_is_not_known_is_never_forwarded_on_a_guess(
    world: DeliveryWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While Navidrome cannot say whether its jukebox is on, an "off" it said last
    stands; when it never said - or said "on" a while ago - the request gets an error, not
    a forward, which Navidrome would log with its credentials if the jukebox is off."""
    interceptor = world.services.interceptor
    log = world.nd.log_path

    def logged() -> int:
        return log.read_text(errors="replace").count("jukeboxControl")

    class Silent:
        user = interceptor.navidrome.user

        async def config(self) -> None:
            return None

        async def subsonic(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise NavidromeError("unavailable")

    client = world.client(salt="s")
    assert client.request("jukeboxControl", {"action": "get"}).status_code == 501
    before = logged()
    monkeypatch.setattr(interceptor, "navidrome", Silent())
    # What it said last (off) has expired: it stands while Navidrome cannot be asked.
    monkeypatch.setattr(interceptor, "_jukebox", (time.monotonic() - 1, False))
    for params in ({"action": "get"}, {"action": "set", "id": "sh.tr.cat.1"}):
        assert client.request("jukeboxControl", params).status_code == 501
    assert logged() == before
    # Never said, or "on" a while ago: an error from Shijhon, for a poll and a command.
    for known in (None, (time.monotonic() - 1, True)):
        for method in ("GET", "POST", "HEAD"):
            monkeypatch.setattr(interceptor, "_jukebox", known)
            unknown = client.request(
                "jukeboxControl", {"action": "get", "id": "sh.tr.cat.1"}, http_method=method
            )
            assert unknown.status_code == 503 and unknown.headers["retry-after"] == "5"
            assert logged() == before
    # A fresh "on" is Navidrome's to answer, as with the jukebox on (here it is off: 501).
    monkeypatch.setattr(interceptor, "_jukebox", (time.monotonic() + 30, True))
    assert client.request("jukeboxControl", {"action": "get"}).status_code == 501
    assert logged() == before + 1
    # A state that held "on" and expired is asked for again: a poll is not forwarded on it.
    monkeypatch.undo()
    monkeypatch.setattr(interceptor, "_jukebox", (time.monotonic() - 1, True))
    before = logged()
    assert client.request("jukeboxControl", {"action": "status"}).status_code == 501
    assert logged() == before


def test_a_paused_play_continues_at_its_source_after_its_link_expired(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon], caplog: pytest.LogCaptureFixture
) -> None:
    """The source the play started from first, with its track,
    even when an earlier source in the order has the track by now."""
    a, b = a_and_b
    # B's links declare their expiry; the pin lapses five seconds before it.
    song, fakes, audio = world.placeholder_track("k-paused", [a, b], link_seconds=5.4)
    data = audio.read_bytes()
    fakes[a].available = False
    assert stream(world, song).content == data  # played from B
    fakes[a].available = True  # A has it by now (the same file)
    a.clear()
    time.sleep(1.0)  # paused: the pin has expired
    with caplog.at_level("INFO", logger="shijhon.delivery.playback"):
        resumed = stream(world, song, {"range": "bytes=1000-"})
    assert resumed.status_code == 206 and resumed.content == data[1000:]
    assert a.requests() == []
    assert len(b.requests("stream")) == 2 and len(b.requests("resolve-isrc")) == 1
    assert b.requests("audio")[-1]["if_match"]
    assert "continuing 'Title k-paused Song 1' at B with a new link to the same file" in (
        caplog.messages
    )


def test_a_continuing_play_takes_another_source_only_with_the_same_file(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    a, b = a_and_b
    song, fakes, audio = world.placeholder_track("k-elsewhere", [a, b])
    data = audio.read_bytes()
    fakes[a].available = False
    assert stream(world, song).content == data  # played from B
    world.server.call(lambda: _forget(world, song))  # the pin expired while paused
    del b.by_key[fakes[b].key()]  # B no longer has the track
    fakes[b].available = False
    fakes[a].available = True
    fakes[a].audio = world.audio("k-elsewhere-other", seconds=5)  # A has another file
    assert failed(stream(world, song, {"range": "bytes=100-"}))
    fakes[a].audio = audio  # the same file
    resumed = stream(world, song, {"range": "bytes=100-"})
    assert resumed.status_code == 206 and resumed.content == data[100:]
    assert a.requests("audio")[-1]["if_match"]


def test_the_play_s_source_with_another_file_now_gives_way_to_one_with_the_same(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """The source the play started from is asked first; it has another file by now, so the
    play continues from another source that has the same one."""
    a, b = a_and_b
    song, fakes, audio = world.placeholder_track("k-gives-way", [a, b])
    data = audio.read_bytes()
    fakes[a].available = False
    assert stream(world, song).content == data  # played from B
    world.server.call(lambda: _forget(world, song))
    fakes[b].audio = world.audio("k-gives-way-other", seconds=5)
    fakes[a].available = True
    a.clear()
    b.clear()
    resumed = stream(world, song, {"range": "bytes=100-"})
    assert resumed.status_code == 206 and resumed.content == data[100:]
    assert b.requests("stream")[0]["at"] < a.requests("stream")[0]["at"]  # B first
    assert len(b.requests("stream")) == 1  # not asked again


def test_a_continuing_play_finds_its_track_again_under_a_new_id(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """The add-on's track ID the play used is gone (404): the recording is looked up there
    again rather than given up."""
    a, _ = a_and_b
    song, fakes, audio = world.placeholder_track("k-new-id", [a])
    data = audio.read_bytes()
    assert stream(world, song).content == data
    world.server.call(lambda: _forget(world, song))
    del a.by_key[fakes[a].key()]
    fakes[a].track_id = "k-new-id-renamed"
    a.by_key[fakes[a].key()] = fakes[a]
    resumed = stream(world, song, {"range": "bytes=100-"})
    assert resumed.status_code == 206 and resumed.content == data[100:]
    assert [r["key"] for r in a.requests("stream")][-2:] == [fakes[a].isrc, "k-new-id-renamed"]


def test_a_live_link_that_fails_gets_a_fresh_one_from_its_source(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """Not only 403/410: a signed link that answers an error after a while is renewed at
    the play's source for the same file."""
    a, b = a_and_b
    song, _, audio = world.placeholder_track(
        "k-link-500", [a, b], expire_after=1, expire_status=500
    )
    data = audio.read_bytes()
    assert stream(world, song).content == data
    seek = stream(world, song, {"range": "bytes=100-"})
    assert seek.status_code == 206 and seek.content == data[100:]
    assert len(a.requests("stream")) == 2 and b.requests() == []


def test_a_timeout_on_an_own_budget_the_cap_ends_with_is_a_failure(
    world: DeliveryWorld,
) -> None:
    """An add-on whose own budget ends with the client's wait cap, and whose first
    byte does not come, failed (its whole budget) - it was not cut short by the cap: the
    client's retry does not ask it again within the window."""
    world.clear_sources()
    stalling = world.addon("Stalling")
    world.add_source(stalling, budget_seconds=2.0)
    settings = world.services.deliverer.settings
    saved = settings.max_wait_seconds
    settings.max_wait_seconds = 2.0
    try:
        song, fakes, _ = world.placeholder_track("k-own-budget-cap", [stalling])
        fakes[stalling].first_byte_delay = 6.0
        assert failed(stream(world, song))
        started = time.monotonic()
        assert failed(stream(world, song))  # the retry: nothing left to ask
        assert time.monotonic() - started < 1.0
        assert len(stalling.requests("stream")) == 1
    finally:
        settings.max_wait_seconds = saved
        world.clear_sources()


def test_a_share_of_the_budget_the_cap_shortened_was_cut_short(
    world: DeliveryWorld, a_and_b: tuple[FakeAddon, FakeAddon]
) -> None:
    """Two sources share the byte-zero budget (4 s here: 2 s each), but the client's wait
    cap comes after 1.2 s: the first one's share is 0.6 s - the cap took more than a second
    of it. It did not fail (it may still be preparing the song): a retry asks it again."""
    a, b = a_and_b
    settings = world.services.deliverer.settings
    saved = settings.budget_seconds, settings.max_wait_seconds
    settings.budget_seconds, settings.max_wait_seconds = 4.0, 1.2
    try:
        song, fakes, _ = world.placeholder_track("k-share-cut", [a, b])
        fakes[a].first_byte_delay = fakes[b].first_byte_delay = 6.0
        assert failed(stream(world, song))
        assert failed(stream(world, song))  # the retry
        assert len(a.requests("stream")) == 2  # asked again: cut short, not failed
    finally:
        settings.budget_seconds, settings.max_wait_seconds = saved


def test_a_continuing_seek_waits_at_most_the_seek_timeout(world: DeliveryWorld) -> None:
    """An add-on's own byte-zero budget does not stretch a seek."""
    world.clear_sources()
    slow = world.addon("Preparing")
    world.add_source(slow, budget_seconds=20.0)
    try:
        song, fakes, audio = world.placeholder_track("k-seek-bound", [slow])
        assert stream(world, song).content == audio.read_bytes()
        world.server.call(lambda: _forget(world, song))
        fakes[slow].resolve_delay = 8.0
        started = time.monotonic()
        assert failed(stream(world, song, {"range": "bytes=100-"}))
        assert time.monotonic() - started < 4.5  # the seek timeout is 3 s here
    finally:
        world.clear_sources()
