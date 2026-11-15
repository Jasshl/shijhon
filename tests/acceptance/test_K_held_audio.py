"""Suite K (continued) - the audio Shijhon holds of a play from the add-ons.

- An answer that goes to its file's end is read on to that end while the app keeps its
  connection, even when the app reads nothing for a while (a player that loaded the song
  ahead), and the app is fed from the file. With ``keep_streamed_audio`` on, the whole file
  goes into the library; off, it is only held.
- A later range of the play is served from that file when it covers the range - before any
  link is asked for, also when the link expired and the add-on gives no new one -, never
  Navidrome's copy at the same offsets. A range it does not cover goes to the add-ons as
  before.
- A file read from for an answer is not removed under it when its place is needed.
- A new link to a file without a strong ETag is accepted only when its first bytes are the
  play's.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack

SECONDS = 60  # pink noise: a FLAC of several megabytes, more than a connection buffers


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(), tmp_path_factory.mktemp("held"), warm_ahead_depth=0
    ) as w:
        yield w


@pytest.fixture
def a(world: DeliveryWorld) -> Iterator[FakeAddon]:
    world.clear_sources()
    addon = world.addon("A")
    world.add_source(addon)
    retention = world.services.deliverer.retention
    assert retention is not None
    yield addon
    world.clear_sources()
    retention.enabled = False
    world.services.deliverer.settings.pin_ttl_seconds = 1800.0


def noise(world: DeliveryWorld, key: str, seed: int = 1) -> Path:
    path = world.tmp / f"{key}-{seed}.flac"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=d={SECONDS}:c=pink:a=0.3:r=44100:seed={seed}", "-ac", "2", "-c:a", "flac",
         str(path)],
        check=True,
    )  # fmt: skip
    return path


def song(world: DeliveryWorld, a: FakeAddon, key: str, **track: Any) -> tuple[str, FakeTrack]:
    found, _, _ = world.placeholder_track(key, [], seconds=SECONDS)
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT isrc FROM placeholders WHERE song_id = ?", [found]
        )
    )
    assert row is not None
    fake = a.add(FakeTrack(isrc=str(row["isrc"]), audio=noise(world, key), **track))
    return found, fake


def held(world: DeliveryWorld, song_id: str) -> list[tuple[int, int]]:
    """What Shijhon holds of the song's file at the add-ons."""
    retention = world.services.deliverer.retention
    assert retention is not None
    for (key, _), entry in retention._entries.items():
        if song_id in key or key.endswith(song_id):
            return list(entry.covered)
    rows = [e for e in retention._entries.values() if e.track.song_id == song_id]
    return list(rows[0].covered) if rows else []


def wait_for(done: Any, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "not in time"
        time.sleep(0.05)


class Paused:
    """A player that loaded the song ahead: it read its first bytes and then nothing, its
    connection open (its reader kept: a dropped one closes the connection)."""

    def __init__(self, world: DeliveryWorld, song_id: str, read: int = 65536) -> None:
        client = world.client(client="ahead-player")
        self.answer = client.request(
            "stream", {"id": song_id}, headers={"range": "bytes=0-"}, stream=True
        )
        self.chunks = self.answer.iter_raw(16384)
        self.got = b""
        while len(self.got) < read:
            self.got += next(self.chunks)

    def rest(self) -> bytes:
        rest = b"".join(self.chunks)
        self.close()
        return self.got + rest

    def close(self) -> None:
        self.answer.close()


def stream(world: DeliveryWorld, song_id: str, wanted: str) -> httpx.Response:
    return world.client(client="ahead-player").request(
        "stream", {"id": song_id}, headers={"range": wanted}
    )


def state(world: DeliveryWorld, song_id: str) -> str:
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT state FROM placeholders WHERE song_id = ?", [song_id]
        )
    )
    assert row is not None
    return str(row["state"])


def test_a_song_loaded_ahead_is_read_on_while_the_app_waits(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """The app reads its first bytes and then nothing for a while: Shijhon reads the whole
    file meanwhile, the add-on's answer is over, and the app still gets every byte, in
    order. Not kept in the library while the setting is off."""
    song_id, fake = song(world, a, "held-ahead", chunk_delay=0.001)
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: held(world, song_id) == [(0, len(data))])
    assert len(a.requests("audio")) == 1
    assert paused.rest() == data
    time.sleep(0.5)
    assert state(world, song_id) == "placeholder"


def test_a_song_read_on_in_full_is_kept_while_songs_are_kept(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    retention = world.services.deliverer.retention
    assert retention is not None
    retention.enabled = True
    song_id, fake = song(world, a, "held-kept")
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: state(world, song_id) == "delivered")
    paused.close()
    assert held(world, song_id) == [(0, len(data))]  # (still held, for the play's ranges)


def test_a_seek_after_the_link_expired_is_served_from_the_audio_held(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """The song's link expired and the add-on gives none now: the play's seek gets its own
    file's bytes from what Shijhon holds, at once, without asking the add-on."""
    song_id, fake = song(world, a, "held-expired")
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: held(world, song_id) == [(0, len(data))])
    paused.close()
    world.services.deliverer.settings.pin_ttl_seconds = 0.0  # expired
    fake.stream_status = 503  # and no new link
    asked = len(a.requests())
    middle = len(data) // 2
    seek = stream(world, song_id, f"bytes={middle}-")
    assert seek.status_code == 206 and seek.content == data[middle:]
    assert seek.headers["content-range"] == f"bytes {middle}-{len(data) - 1}/{len(data)}"
    assert len(a.requests()) == asked


def test_a_kept_song_s_play_never_gets_the_library_copy_at_its_offsets(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """The song went into the library (another, tagged file) while the play went on; its
    link expired and the add-on gives none: the play's seek still gets its own file."""
    retention = world.services.deliverer.retention
    assert retention is not None
    retention.enabled = True
    song_id, fake = song(world, a, "held-library")
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: state(world, song_id) == "delivered")
    paused.close()
    world.services.deliverer.settings.pin_ttl_seconds = 0.0
    fake.stream_status = 503
    seek = stream(world, song_id, "bytes=1000-")
    assert seek.status_code == 206 and seek.content == data[1000:]


def test_a_range_beyond_what_is_held_goes_to_the_add_on(world: DeliveryWorld, a: FakeAddon) -> None:
    """The app left early: what came is held. A range within it is served from it, one past
    it from the play's link."""
    song_id, fake = song(world, a, "held-partial", chunk_delay=0.02)
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id, read=16384)
    time.sleep(0.3)
    paused.close()  # it leaves: the reading on stops
    wait_for(lambda: bool(held(world, song_id)))
    time.sleep(0.3)
    [(first, end)] = held(world, song_id)
    assert first == 0 and 16384 <= end < len(data)
    asked = len(a.requests("audio"))
    inside = stream(world, song_id, f"bytes=100-{end - 1}")
    assert inside.content == data[100:end] and len(a.requests("audio")) == asked
    beyond = stream(world, song_id, f"bytes={end + 1000}-")
    assert beyond.content == data[end + 1000 :] and len(a.requests("audio")) == asked + 1


def test_a_file_served_from_is_not_removed_under_its_answer(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """A seek served from the file held while its place is taken for other songs: the file
    stays until the answer is done with it, then goes."""
    song_id, fake = song(world, a, "held-evicted")
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: held(world, song_id) == [(0, len(data))])
    paused.close()
    retention = world.services.deliverer.retention
    assert retention is not None
    seek = world.client(client="ahead-player").request(
        "stream", {"id": song_id}, headers={"range": "bytes=10-"}, stream=True
    )
    chunks = seek.iter_raw(16384)
    got = b""
    while len(got) <= 100_000:
        got += next(chunks)
    entries = [e for e in retention._entries.values() if e.track.song_id == song_id]

    async def drop() -> None:
        for entry in entries:
            retention._drop(entry, "test: its place is needed")

    world.server.call(drop)
    assert entries and all(e.path.exists() for e in entries)  # (read for the answer)
    got += b"".join(chunks)
    seek.close()
    assert got == data[10:]
    wait_for(lambda: not any(e.path.exists() for e in entries))  # removed once it is done


def test_a_new_link_to_another_file_of_the_same_size_is_refused_by_its_first_bytes(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """No strong ETag: the play's link expired, and the add-on now links another file of
    the same size. Its first bytes are not the play's: the seek is refused (never the
    other file's bytes); the same file behind a new link plays on."""
    retention = world.services.deliverer.retention
    assert retention is not None
    retention.holding = False  # (nothing held: the add-on's new link decides)
    try:
        song_id, fake = song(world, a, "held-other", etag=False)
        data = fake.audio.read_bytes()
        assert stream(world, song_id, "bytes=0-").content == data
        world.services.deliverer.settings.pin_ttl_seconds = 0.0
        other = noise(world, "held-other", seed=2).read_bytes()
        replaced = world.tmp / "held-other-replaced.flac"
        replaced.write_bytes((other * 2)[: len(data)])
        original = fake.audio
        fake.audio = replaced
        refused = stream(world, song_id, "bytes=5000-")
        assert refused.status_code == 200
        assert "audio unavailable" in refused.json()["subsonic-response"]["error"]["message"]
        fake.audio = original
        again = stream(world, song_id, "bytes=5000-")
        assert again.status_code == 206 and again.content == data[5000:]
    finally:
        retention.holding = True


def test_a_seek_served_from_the_audio_held_is_quicker_than_a_new_link(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """(Times printed for the record.) A new link takes the add-on's time to answer; the
    audio held answers at once. An ordinary seek on a live link stays as quick."""
    song_id, fake = song(world, a, "held-timing", resolve_delay=0.5, first_byte_delay=0.2)
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: held(world, song_id) == [(0, len(data))])
    paused.close()

    def timed(wanted: str) -> float:
        started = time.monotonic()
        response = world.client(client="ahead-player").request(
            "stream", {"id": song_id}, headers={"range": wanted}, stream=True
        )
        next(response.iter_raw(4096))
        took = time.monotonic() - started
        response.close()
        return took

    from_held = timed("bytes=2000000-")
    retention = world.services.deliverer.retention
    assert retention is not None
    retention.holding = False
    try:
        live = timed("bytes=2000000-")  # its link is live
        world.services.deliverer.settings.pin_ttl_seconds = 0.0
        renewed = timed("bytes=3000000-")  # a new link to its file
    finally:
        retention.holding = True
    print(f"\nSEEK held {from_held:.3f}s, live link {live:.3f}s, new link {renewed:.3f}s")
    assert from_held < 0.2 < 0.5 < renewed
    assert from_held <= live + 0.05


def test_an_add_on_that_asks_not_to_be_used_for_downloads_is_not_read_on(
    world: DeliveryWorld,
) -> None:
    """Its answers go as the app reads them: reading on would ask it for a whole song the
    app may never play."""
    world.clear_sources()
    addon = world.addon("No downloads")
    addon.manifest_extra = {"allowDownloads": False}
    world.add_source(addon)
    try:
        song_id, fake = song(world, addon, "held-no-downloads", chunk_delay=0.001)
        data = fake.audio.read_bytes()
        paused = Paused(world, song_id)
        time.sleep(1.5)
        sent = addon.bytes_sent[fake.isrc]
        assert sent < len(data) // 2, (sent, len(data))  # what the app read, and a buffer
        assert paused.rest() == data
    finally:
        world.clear_sources()


# --- a continuing play's file, told apart from another (no strong ETag) --------------------


def other_file(world: DeliveryWorld, key: str, size: int) -> Path:
    other = noise(world, key, seed=2).read_bytes()
    path = world.tmp / f"{key}-other.flac"
    path.write_bytes((other * 2)[:size])
    return path


def refused(answer: httpx.Response) -> bool:
    return answer.status_code == 200 and "audio unavailable" in answer.text


@pytest.fixture
def unheld(world: DeliveryWorld) -> Iterator[None]:
    """Nothing held: the add-on's links decide."""
    retention = world.services.deliverer.retention
    assert retention is not None
    retention.holding = False
    yield
    retention.holding = True


def test_another_file_refused_once_is_refused_again(
    world: DeliveryWorld, a: FakeAddon, unheld: None
) -> None:
    """A rejected new link leaves what the play is known by as it was: the next try is
    told apart the same way, never taken for want of the play's first bytes."""
    song_id, fake = song(world, a, "held-twice", etag=False)
    data = fake.audio.read_bytes()
    assert stream(world, song_id, "bytes=0-").content == data
    world.services.deliverer.settings.pin_ttl_seconds = 0.0
    fake.audio = other_file(world, "held-twice", len(data))
    assert refused(stream(world, song_id, "bytes=5000-"))
    assert refused(stream(world, song_id, "bytes=6000-"))
    assert refused(stream(world, song_id, "bytes=7000-"))


def test_a_second_renewal_is_told_apart_as_the_first(
    world: DeliveryWorld, a: FakeAddon, unheld: None
) -> None:
    """The first new link is the same file (taken); the play's link then expires again and
    the add-on links another file of the same size: told apart by the play's first bytes,
    which the first renewal kept."""
    song_id, fake = song(world, a, "held-renewed", etag=False)
    data = fake.audio.read_bytes()
    assert stream(world, song_id, "bytes=0-").content == data
    world.services.deliverer.settings.pin_ttl_seconds = 0.0
    assert stream(world, song_id, "bytes=5000-").content == data[5000:]  # (the same file)
    fake.audio = other_file(world, "held-renewed", len(data))
    assert refused(stream(world, song_id, "bytes=6000-"))


def test_the_first_bytes_are_known_also_from_small_first_chunks(
    world: DeliveryWorld, a: FakeAddon, unheld: None
) -> None:
    """The add-on sends its file in small chunks, the first only a FLAC header's worth (the
    length is read from it alone): the play's first bytes are noted as the answer comes, and
    another file behind a new link is refused."""
    song_id, fake = song(world, a, "held-small", etag=False, chunk_size=42)
    data = fake.audio.read_bytes()
    assert stream(world, song_id, "bytes=0-").content == data
    world.services.deliverer.settings.pin_ttl_seconds = 0.0
    fake.audio = other_file(world, "held-small", len(data))
    assert refused(stream(world, song_id, "bytes=5000-"))


def test_a_new_link_after_only_a_probe_is_not_taken_unchecked(
    world: DeliveryWorld, a: FakeAddon, unheld: None
) -> None:
    """The app asked only its first two bytes (a probe) and then from the middle: what the
    probe read is enough to tell the play's file; with none of its first bytes known, a new
    link to a file without an ETag would not be taken."""
    song_id, fake = song(world, a, "held-probe", etag=False, chunk_size=42)
    data = fake.audio.read_bytes()
    assert stream(world, song_id, "bytes=0-1").content == data[:2]
    world.services.deliverer.settings.pin_ttl_seconds = 0.0
    fake.audio = other_file(world, "held-probe", len(data))
    assert refused(stream(world, song_id, "bytes=5000-"))


def test_a_new_link_that_adds_an_etag_is_still_told_apart_by_its_first_bytes(
    world: DeliveryWorld, a: FakeAddon, unheld: None
) -> None:
    """The play's file had no strong ETag; the add-on's new link to another file of the same
    size sends one: the play still holds it to its first bytes."""
    song_id, fake = song(world, a, "held-tagged", etag=False)
    data = fake.audio.read_bytes()
    assert stream(world, song_id, "bytes=0-").content == data
    world.services.deliverer.settings.pin_ttl_seconds = 0.0
    fake.audio = other_file(world, "held-tagged", len(data))
    fake.etag = True
    assert refused(stream(world, song_id, "bytes=5000-"))


def test_two_listeners_of_two_files_each_keep_their_own(world: DeliveryWorld, a: FakeAddon) -> None:
    """One app plays the song; the add-on then gives another file for it (another size), and
    a second app's play gets that one. The first app's seek gets its own file, from what is
    held of it - not the second's, at its offsets."""
    song_id, fake = song(world, a, "held-two")
    first = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: held(world, song_id) == [(0, len(first))])
    paused.close()
    second_file = noise(world, "held-two", seed=3)
    second = second_file.read_bytes()[: len(first) - 1000]
    fake.audio = world.tmp / "held-two-second.flac"
    fake.audio.write_bytes(second)
    other = world.client(client="second-app")
    assert other.request("stream", {"id": song_id}).content == second
    seek = stream(world, song_id, "bytes=100000-")
    assert seek.status_code == 206 and seek.content == first[100000:]


def test_a_file_of_untold_size_is_read_on_and_its_seeks_served_from_it(
    world: DeliveryWorld, a: FakeAddon
) -> None:
    """An add-on that tells no size (a chunked answer) and ignores ranges: the song is read
    on to its end while the app waits, and a later range of the play is served from it."""
    song_id, fake = song(world, a, "held-untold", untold_size=True, ranges=False)
    data = fake.audio.read_bytes()
    paused = Paused(world, song_id)
    wait_for(lambda: held(world, song_id) == [(0, len(data))])
    assert paused.rest() == data
    asked = len(a.requests("audio"))
    seek = stream(world, song_id, "bytes=100000-")
    assert seek.status_code == 206 and seek.content == data[100000:]
    assert seek.headers["content-range"] == f"bytes 100000-{len(data) - 1}/{len(data)}"
    assert len(a.requests("audio")) == asked


def test_a_new_link_shown_to_be_one_play_s_file_is_not_another_play_s(
    world: DeliveryWorld, a: FakeAddon, unheld: None
) -> None:
    """Two apps play two files of the same size without an ETag (the add-on changed the file
    between their starts). Their links expire; the add-on's new link is the first app's
    file, shown so by its first bytes. The second app's seek, on that same new link, is held
    to its own file's first bytes: refused, never the first file at its offsets."""
    song_id, fake = song(world, a, "held-shared", etag=False, link_seconds=6.0)
    first = fake.audio.read_bytes()
    one = world.client(client="first-app")
    assert one.request("stream", {"id": song_id}).content == first
    time.sleep(1.3)  # its link expired (5 s before its time)
    second_path = other_file(world, "held-shared", len(first))
    fake.audio = second_path
    two = world.client(client="second-app")
    assert two.request("stream", {"id": song_id}).content == second_path.read_bytes()
    time.sleep(1.3)
    fake.link_seconds = None  # (the new link stays)
    fake.audio = noise(world, "held-shared")  # the first file again
    seek = one.request("stream", {"id": song_id}, headers={"range": "bytes=5000-"})
    assert seek.status_code == 206 and seek.content == first[5000:]
    other = two.request("stream", {"id": song_id}, headers={"range": "bytes=5000-"})
    assert refused(other), other.content[:40]
