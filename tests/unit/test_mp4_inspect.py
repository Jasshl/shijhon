"""What ``mpd.inspect`` reads of an MP4's boxes: the length of its first audio track - the
samples its header holds and its own fragments - however many tracks it has; and whether
its movie is in fragments (``mpd.fragmented``)."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from shijhon.delivery import mpd
from tests.harness.fragmented import fragmented_mp4
from tests.harness.library import tone


@pytest.mark.parametrize(
    ("codec", "options"),
    [("aac", {}), ("flac", {}), ("flac", {"tracks": 2}), ("aac", {"tracks": 2}),
     ("flac", {"held": True, "mehd": False}), ("aac", {"held": True, "mehd": False})],
)  # fmt: skip
def test_a_fragmented_file_tells_its_first_audio_track_s_whole_length(
    tmp_path: Path, codec: str, options: dict[str, object]
) -> None:
    path = fragmented_mp4(tmp_path / "pieces.m4a", 440, 6, codec, **options)  # type: ignore[arg-type]
    found = mpd.inspect(path.read_bytes())
    assert found.seconds is not None and abs(found.seconds - 6) < 0.05
    with path.open("rb") as source:
        assert mpd.fragmented(source)


def test_an_ordinary_mp4_tells_its_header_s_length_and_is_not_fragmented(tmp_path: Path) -> None:
    path = tmp_path / "whole.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(tone(440, 6, "flac")), "-c:a",
         "aac", "-movflags", "+faststart", str(path)],
        check=True, capture_output=True,
    )  # fmt: skip
    found = mpd.inspect(path.read_bytes())
    assert found.seconds is not None and abs(found.seconds - 6) < 0.05
    assert not mpd.fragmented(io.BytesIO(path.read_bytes()))


def test_a_media_segment_alone_counts_all_its_runs(tmp_path: Path) -> None:
    """A DASH media segment has no movie header: its runs count as before."""
    path = fragmented_mp4(tmp_path / "pieces.m4a", 440, 6, "flac", mehd=False)
    data = path.read_bytes()
    moof = data.index(b"moof") - 4
    found = mpd.inspect(data[moof:])  # (every fragment, without the header)
    assert found.units == 6 * 44100 and found.timescale is None
