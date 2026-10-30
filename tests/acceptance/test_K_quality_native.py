"""Quality inference against Navidrome's own metadata of synthetic AAC."""

import subprocess

import pytest

from shijhon.delivery.quality_gate import native_quality
from shijhon.quality import DEFAULT, Quality
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import NavidromeInstance


@pytest.mark.canary
def test_aac_with_a_native_bit_depth_is_lossy(navidrome: NavidromeInstance) -> None:
    write_album(
        navidrome.music,
        Album("Invented Hollow", "AAC Evidence", (Track("AAC Tone", 1, seconds=2),), "m4a"),
    )
    navidrome.scan(full=True)
    song = navidrome.client().ok("search3", {"query": "AAC Tone"})["searchResult3"]["song"][0]
    native = navidrome.native("GET", f"song/{song['id']}").json()
    assert native["codec"] == "AAC" and native["bitDepth"] == 16
    for quality in (native_quality(native), Quality.of_song(native)):
        assert not quality.lossless and not DEFAULT.met_by(quality)


@pytest.mark.canary
def test_alac_with_a_native_bit_depth_is_lossless(navidrome: NavidromeInstance) -> None:
    folder = navidrome.music / "Invented ALAC"
    folder.mkdir(exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2",
            "-c:a",
            "alac",
            "-metadata",
            "title=ALAC Tone",
            "-metadata",
            "artist=Invented Hollow",
            "-metadata",
            "album=ALAC Evidence",
            str(folder / "tone.m4a"),
        ],
        check=True,
    )
    navidrome.scan(full=True)
    song = navidrome.client().ok("search3", {"query": "ALAC Tone"})["searchResult3"]["song"][0]
    native = navidrome.native("GET", f"song/{song['id']}").json()
    assert native["codec"] == "ALAC" and native["bitDepth"] == 16
    for quality in (native_quality(native), Quality.of_song(native)):
        assert quality.lossless and DEFAULT.met_by(quality)
