from __future__ import annotations

from shijhon.quality import ANY, DEFAULT, Minimum, Quality


def test_quality_of_native_song_entries() -> None:
    flac = Quality.of_song(
        {"suffix": "flac", "bitRate": 900, "samplingRate": 96000, "bitDepth": 24}
    )
    alac = Quality.of_song({"suffix": "m4a", "bitRate": 800, "samplingRate": 44100, "bitDepth": 16})
    aac = Quality.of_song({"suffix": "m4a", "bitRate": 256, "samplingRate": 44100})
    mp3 = Quality.of_song({"suffix": "mp3", "bitRate": "128", "samplingRate": 44100})
    assert flac.lossless and alac.lossless and not aac.lossless and not mp3.lossless
    assert flac.describe() == "FLAC 24-bit 96 kHz"
    assert mp3.describe() == "MP3 128 kbps"
    assert sorted([mp3, flac, aac], key=Quality.rank) == [mp3, aac, flac]
    assert not Quality.of_song({}).known()


def test_minimum_keeps_what_is_at_or_above_it() -> None:
    mp3_128 = Quality("mp3", kbps=128, sample_rate=44100)
    mp3_320 = Quality("mp3", kbps=320, sample_rate=44100)
    flac = Quality("flac", lossless=True, kbps=900, sample_rate=44100, bit_depth=16)
    low_rate = Quality("mp3", kbps=320, sample_rate=22050)
    assert not DEFAULT.met_by(mp3_128)
    assert DEFAULT.met_by(mp3_320) and DEFAULT.met_by(flac)
    assert not DEFAULT.met_by(low_rate)
    assert DEFAULT.met_by(Quality("aac"))  # not known: never turned away
    assert not Minimum(lossless_only=True).met_by(mp3_320)
    assert Minimum(lossless_only=True).met_by(flac)
    assert ANY.met_by(mp3_128) and ANY.met_by(low_rate)
    assert DEFAULT.describe() == (
        "compressed at 192 kbps or more; lossless at any bitrate; 44.1 kHz or more"
    )
    assert Minimum(lossless_only=True).describe() == "lossless only; 44.1 kHz or more"
    assert ANY.describe() == "any quality"
