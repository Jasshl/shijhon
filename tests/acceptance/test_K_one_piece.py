"""Suite K (continued) — delivered audio from before whose length Navidrome reads as zero
(``delivery/lengths.py``). Add-on audio in a fragmented MP4 or in Ogg FLAC went into the
library as it came: Navidrome lists such a song as 0:00. In the background it is put in
one piece - the same encoded audio, under the song's ID, with its tags, album and
favorite -, never while it was used just now, never a music import's own audio.

Against a real, disposable Navidrome, with invented music; such a file "from before" is put
in place through the engine directly, as deliveries put it.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from shijhon.delivery.lengths import OnePiece, Swept
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fragmented import fragmented_mp4, is_fragmented
from tests.harness.library import frequency_for
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(navidrome_factory(), tmp_path_factory.mktemp("one-piece")) as w:
        yield w


def delivered_before(world: DeliveryWorld, key: str, audio: Path) -> str:
    """A catalog song whose delivered audio is ``audio`` as it came (as before this fix)."""
    song, _, _ = world.placeholder_track(key, [], seconds=6)
    world.server.call(lambda: world.services.engine.replace_with_delivered(song, audio))
    return song


def sweep(world: DeliveryWorld, **settings: Any) -> Swept:
    repair = OnePiece(world.services.store, world.services.download_first, **settings)
    return world.server.call(repair.sweep)


def native(world: DeliveryWorld, song: str) -> dict[str, Any]:
    found = world.server.call(lambda: world.services.navidrome.song(song))
    assert found is not None
    return found


def listed(world: DeliveryWorld, song: str) -> dict[str, Any]:
    """What an app sees of the song, but its file's own properties."""
    found = native(world, song)
    return {k: found.get(k) for k in ("id", "albumId", "title", "album", "artist", "trackNumber")}


def packets(path: Path) -> list[str]:
    found = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-c", "copy",
         "-f", "framemd5", "-"],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return [line.split(",")[5].strip() for line in found.splitlines() if line[:1].isdigit()]


def file_of(world: DeliveryWorld, song: str) -> Path:
    return world.services.engine.layout.absolute(str(native(world, song)["path"]))


@pytest.mark.parametrize(("codec", "suffix"), [("aac", ".m4a"), ("flac", ".flac")])
def test_delivered_audio_in_fragments_is_put_in_one_piece(
    world: DeliveryWorld, tmp_path: Path, codec: str, suffix: str
) -> None:
    pieces = fragmented_mp4(tmp_path / "pieces.m4a", frequency_for(codec), 6, codec)
    song = delivered_before(world, f"one-piece-{codec}", pieces)
    client = world.client()
    client.ok("star", {"id": song})
    before = listed(world, song)
    assert native(world, song)["duration"] == 0  # (the fault: 0:00, no seeking)
    swept = sweep(world, recent_seconds=0)
    assert swept.done == 1 and swept.failed == 0
    after = native(world, song)
    assert listed(world, song) == before and after["suffix"] == suffix[1:]
    assert abs(after["duration"] - 6) < 0.5
    path = file_of(world, song)
    assert not is_fragmented(path) and packets(path) == packets(pieces)
    assert song in [e["id"] for e in client.ok("getStarred2")["starred2"].get("song", [])]
    assert client.request("stream", {"id": song}).content == path.read_bytes()
    assert sweep(world, recent_seconds=0) == Swept()  # nothing left


def test_ogg_flac_goes_in_as_a_native_flac(world: DeliveryWorld, tmp_path: Path) -> None:
    formats = {f.key: f for f in IMPORT_FORMATS}
    song = delivered_before(world, "one-piece-oga", import_tone(tmp_path, formats["ogg-flac"],
                                                                tagged=False))  # fmt: skip
    before = listed(world, song)
    assert native(world, song)["duration"] == 0
    assert sweep(world, recent_seconds=0).done == 1
    after = native(world, song)
    assert listed(world, song) == before and after["suffix"] == "flac" and after["duration"] > 0.2


def test_audio_used_just_now_is_left_for_later(world: DeliveryWorld, tmp_path: Path) -> None:
    """A paused play may still seek in its file: two hours after its last use, not before."""
    pieces = fragmented_mp4(tmp_path / "pieces.m4a", frequency_for("later"), 6)
    song = delivered_before(world, "one-piece-later", pieces)
    now = time.time()
    world.server.call(
        lambda: world.services.store.execute(
            "UPDATE placeholders SET last_used_at = ? WHERE song_id = ?", [now, song]
        )
    )
    swept = sweep(world)
    assert swept.later >= 1 and native(world, song)["duration"] == 0
    assert is_fragmented(file_of(world, song))
    assert sweep(world, clock=lambda: now + 7300).done >= 1
    assert abs(native(world, song)["duration"] - 6) < 0.5


def test_a_use_after_the_sweep_looked_keeps_the_file(world: DeliveryWorld, tmp_path: Path) -> None:
    """Looked at again under the song's lock, which every request for its audio takes (and
    records its use under): a use after the sweep's look keeps the file for now."""
    pieces = fragmented_mp4(tmp_path / "pieces.m4a", frequency_for("meanwhile"), 6)
    song = delivered_before(world, "one-piece-meanwhile", pieces)
    world.server.call(
        lambda: world.services.store.execute(
            "UPDATE placeholders SET last_used_at = 1000 WHERE song_id = ?", [song]
        )
    )
    first = world.services.download_first

    def again(since: float) -> bool:
        return world.server.call(lambda: first.in_one_piece(song, unused_since=since))

    assert again(999.0) is False and is_fragmented(file_of(world, song))
    assert again(1000.0) is True and not is_fragmented(file_of(world, song))


def test_a_music_import_s_own_audio_is_never_touched(world: DeliveryWorld, tmp_path: Path) -> None:
    pieces = fragmented_mp4(tmp_path / "pieces.m4a", frequency_for("own"), 6)
    song = delivered_before(world, "one-piece-own", pieces)
    world.server.call(
        lambda: world.services.store.execute(
            "UPDATE placeholders SET import_item = 'an-import-item' WHERE song_id = ?", [song]
        )
    )
    data = file_of(world, song).read_bytes()
    sweep(world, recent_seconds=0)
    assert file_of(world, song).read_bytes() == data and native(world, song)["duration"] == 0
