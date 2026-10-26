"""Tags of every format an import can carry (``music_imports/tagmap.py``): a copy that
joins an album takes its tags, an upgraded file keeps the whole metadata (invented
tones)."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import (
    COMM,
    ID3,
    POPM,
    PRIV,
    TALB,
    TIT1,
    TIT2,
    TKEY,
    TLEN,
    TOAL,
    TOPE,
    TPE1,
    TRCK,
    TSSE,
    TXXX,
    USLT,
    WXXX,
)
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggvorbis import OggVorbis
from mutagen.wavpack import WavPack

from shijhon.matching.grouping import album_key
from shijhon.music_imports import retag, tagmap
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone

WRITABLE = [f for f in IMPORT_FORMATS if f.key not in ("matroska-flac", "shorten", "tak", "ac3")]
ALBUM = {
    "album": ["Night Letters"],
    "albumartist": ["Tide Paper"],
    "releasedate": ["2020-05-01"],
    "recordingdate": ["2019"],
    "albumversion": ["Tour"],
    "genre": ["Ambient"],
    "compilation": ["1"],
}


def test_every_family_is_known() -> None:
    assert {f.key for f in IMPORT_FORMATS} - {f.key for f in WRITABLE} == {
        "matroska-flac",
        "shorten",
        "tak",
        "ac3",
    }


def _parsed(data: io.BytesIO, suffix: str) -> tuple[object, object]:
    audio = tagmap.parse(data, suffix)
    return audio, tagmap.tags_of(audio, data)


@pytest.mark.parametrize("fmt", WRITABLE, ids=lambda f: f.key)
def test_a_copy_in_every_writable_format_takes_the_albums_tags(tmp_path: Path, fmt) -> None:
    path = import_tone(tmp_path, fmt, tagged=True)
    target = io.BytesIO()
    with open(path, "rb") as source:
        retag.retag(source, target, ALBUM, "Tide Paper", fmt.suffix)
    audio, tags = _parsed(target, fmt.suffix)
    assert tagmap.family(audio)
    mine = tagmap.album_tags(audio, tags)
    assert album_key(mine, "") == album_key(ALBUM, "Tide Paper")
    assert mine["album"] == ["Night Letters"] and mine["releasedate"] == ["2020-05-01"]
    # The original's own album ("Paper Garden") is gone under every name.
    assert "Paper Garden" not in str(tagmap.raw(audio, tags))


def _original(tmp_path: Path) -> Path:
    """A FLAC with lyrics, BPM, totals and the rest of a well-tagged file."""
    path = import_tone(tmp_path / "flac", next(f for f in IMPORT_FORMATS if f.key == "flac"),
                       tagged=False)  # fmt: skip
    audio = FLAC(path)
    for key, value in {
        "title": "Lantern 3",
        "artist": "Tide Paper",
        "album": "Night Letters",
        "albumartist": "Tide Paper",
        "tracknumber": "3",
        "tracktotal": "12",
        "discnumber": "1",
        "disctotal": "2",
        "releasedate": "2020-05-01",
        "lyrics": "Lights along the water",
        "bpm": "96",
        "composer": "Ada Quill",
        "isrc": "XXA0L2000003",
        "musicbrainz_releasetrackid": "11111111-2222-3333-4444-555555555555",
        "replaygain_track_gain": "-6.20 dB",
        "comment": "Invented for a test",
    }.items():
        audio[key] = [value]
    audio.save()
    return path


@pytest.mark.parametrize("fmt", WRITABLE, ids=lambda f: f.key)
def test_an_upgraded_file_in_every_format_keeps_the_whole_metadata(tmp_path: Path, fmt) -> None:
    original = _original(tmp_path)
    delivered = import_tone(tmp_path / "addon", fmt, tagged=True)  # the add-on's own tags
    target = tmp_path / "upgraded"
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with open(original, "rb") as source, open(delivered, "rb") as audio:
            retag.carry(source, audio, descriptor, ".flac", "." + fmt.suffix)
    finally:
        os.close(descriptor)
    with open(target, "rb+") as data:
        parsed = tagmap.parse(data, fmt.suffix)
        found = tagmap.metadata(parsed, tagmap.tags_of(parsed, data))
    expected = {
        "title": "Lantern 3",
        "tracknumber": "3",
        "tracktotal": "12",
        "discnumber": "1",
        "disctotal": "2",
        "lyrics": "Lights along the water",
        "bpm": "96",
        "composer": "Ada Quill",
        "isrc": "XXA0L2000003",
        "replaygain_track_gain": "-6.20 dB",
        "comment": "Invented for a test",
    }
    assert {k: (found.get(k) or [""])[0] for k in expected} == expected
    assert "Paper Garden" not in str(found)  # the add-on's own tags are gone


@pytest.mark.parametrize("fmt", WRITABLE, ids=lambda f: f.key)
def test_an_original_in_every_format_carries_its_metadata_into_flac(tmp_path: Path, fmt) -> None:
    values = {
        "title": ["Lantern 4"],
        "artist": ["Tide Paper"],
        "album": ["Night Letters"],
        "tracknumber": ["4"],
        "tracktotal": ["12"],
        "lyrics": ["Lights along the water"],
        "bpm": ["96"],
        "isrc": ["XXA0L2000004"],
    }
    original = import_tone(tmp_path / "original", fmt, tagged=False)
    with open(original, "rb+") as data:
        parsed = tagmap.parse(data, fmt.suffix)
        tags = tagmap.ensure_tags(parsed, data)
        tagmap.write(parsed, tags, tagmap.family(parsed), values)
        tagmap.save(parsed, tags, data)
    delivered = _original(tmp_path)  # a FLAC with other tags
    target = tmp_path / "upgraded.flac"
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with open(original, "rb") as source, open(delivered, "rb") as audio:
            retag.carry(source, audio, descriptor, "." + fmt.suffix, ".flac")
    finally:
        os.close(descriptor)
    found = tagmap.metadata(FLAC(target))
    assert {k: found.get(k) for k in values} == values
    assert "Lantern 3" not in str(found)


def _upgrade(tmp_path: Path, original: Path, key: str) -> Path:
    """``original`` carried into the add-on's tagged audio of format ``key``."""
    fmt = next(f for f in IMPORT_FORMATS if f.key == key)
    delivered = import_tone(tmp_path / ("addon-" + key), fmt, tagged=True)
    target = tmp_path / ("upgraded-" + key + "." + fmt.suffix)
    descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with open(original, "rb") as source, open(delivered, "rb") as audio:
            retag.carry(source, audio, descriptor, original.suffix, "." + fmt.suffix)
    finally:
        os.close(descriptor)
    return target


def _tone(tmp_path: Path, key: str) -> Path:
    return import_tone(tmp_path / ("original-" + key),
                       next(f for f in IMPORT_FORMATS if f.key == key), tagged=False)  # fmt: skip


def test_an_upgraded_file_of_the_same_family_keeps_every_frame(tmp_path: Path) -> None:
    """ID3 to ID3: frames the logical names do not know (an original artist), comments
    with their descriptions and languages, lyrics in their language, user text and URL
    frames under their own names, ratings and an application's private frames - never what
    describes the imported file's own encoding or a player's state of it (its length,
    encoder settings, gapless data, volume-leveling measurements, a streaming timestamp)."""
    original = _tone(tmp_path, "mp3")
    tags = ID3()
    for frame in (
        TIT2(encoding=3, text=["Lantern 5"]),
        TPE1(encoding=3, text=["Tide Paper"]),
        TALB(encoding=3, text=["Night Letters"]),
        TRCK(encoding=3, text=["5/12"]),
        TOPE(encoding=3, text=["Ada Quill"]),
        TOAL(encoding=3, text=["Older Letters"]),
        TKEY(encoding=3, text=["Am"]),
        TIT1(encoding=3, text=["Harbour Works"]),
        COMM(encoding=3, lang="deu", desc="", text=["Erfunden"]),
        COMM(encoding=3, lang="eng", desc="Rip notes", text=["Invented notes"]),
        COMM(encoding=3, lang="eng", desc="iTunSMPB", text=[" 00000000 00000210"]),
        USLT(encoding=3, lang="deu", desc="", text="Lichter am Wasser"),
        TXXX(encoding=3, desc="My Tag", text=["kept as named"]),
        WXXX(encoding=3, desc="Home", url="https://example.invalid/tide"),
        POPM(email="listener@example.invalid", rating=196, count=3),
        TLEN(encoding=3, text=["12345"]),
        TSSE(encoding=3, text=["LAME 3.100"]),
        PRIV(owner="org.example.notes", data=b"invented private notes"),
        PRIV(owner="WM/MediaClassPrimaryID", data=b"\x01" * 16),
        PRIV(owner="com.apple.streaming.transportStreamTimestamp", data=b"\x00" * 8),
        PRIV(owner="PeakValue", data=b"\x10\x27\x00\x00"),
        PRIV(owner="AverageLevel", data=b"\xe8\x03\x00\x00"),
    ):
        tags.add(frame)
    tags.save(original)
    found = ID3(_upgrade(tmp_path, original, "mp3"))
    assert {(f.owner, f.data) for f in found.getall("PRIV")} == {
        ("org.example.notes", b"invented private notes"),
        ("WM/MediaClassPrimaryID", b"\x01" * 16),
    }
    assert found["TOPE"].text == ["Ada Quill"] and found["TOAL"].text == ["Older Letters"]
    assert found["TKEY"].text == ["Am"] and found["TIT1"].text == ["Harbour Works"]
    assert found["COMM::deu"].text == ["Erfunden"]
    assert found["COMM:Rip notes:eng"].text == ["Invented notes"]
    assert found["USLT::deu"].text == "Lichter am Wasser"
    assert found["TXXX:My Tag"].text == ["kept as named"]
    assert found.getall("WXXX")[0].url == "https://example.invalid/tide"
    assert found.getall("POPM")[0].rating == 196
    assert found["TRCK"].text == ["5/12"]
    for gone in ("COMM:iTunSMPB:eng", "TLEN", "TSSE"):
        assert gone not in found, gone
    assert "Paper Garden" not in found.pprint()  # the add-on's own tags are gone


def test_an_upgraded_file_of_another_family_maps_what_it_can(tmp_path: Path) -> None:
    """Across families by name: an original artist and album, the key, a comment by its
    description (TagLib's "COMMENT:<description>") - and back again."""
    original = _tone(tmp_path, "mp3")
    tags = ID3()
    for frame in (
        TIT2(encoding=3, text=["Lantern 6"]),
        TPE1(encoding=3, text=["Tide Paper"]),
        TOPE(encoding=3, text=["Ada Quill"]),
        TOAL(encoding=3, text=["Older Letters"]),
        TKEY(encoding=3, text=["Am"]),
        COMM(encoding=3, lang="eng", desc="", text=["Plain"]),
        COMM(encoding=3, lang="eng", desc="Rip notes", text=["Invented notes"]),
        COMM(encoding=3, lang="eng", desc="iTunNORM", text=[" 00000210"]),
    ):
        tags.add(frame)
    tags.save(original)
    flac = _upgrade(tmp_path, original, "flac")
    found = tagmap.metadata(FLAC(flac))
    assert found["originalartist"] == ["Ada Quill"]
    assert found["originalalbum"] == ["Older Letters"] and found["initialkey"] == ["Am"]
    assert found["comment"] == ["Plain"] and found["comment:rip notes"] == ["Invented notes"]
    assert "comment:itunnorm" not in found
    back = ID3(_upgrade(tmp_path, flac, "mp3"))
    assert back["TOPE"].text == ["Ada Quill"] and back["TKEY"].text == ["Am"]
    assert back["COMM:rip notes:eng"].text == ["Invented notes"]
    assert back["COMM::eng"].text == ["Plain"]


def test_mpeg4_ape_and_vorbis_upgrades_keep_their_own_items(tmp_path: Path) -> None:
    """MPEG-4 freeform atoms under their own mean and name, an atom the logical names do
    not know, APEv2 and Vorbis items under their own keys - without the encoder's."""
    m4a = _tone(tmp_path, "aac-m4a")
    audio = MP4(m4a)
    assert audio.tags is not None
    audio.tags.update(
        {
            "\xa9nam": ["Lantern 7"],
            "\xa9ART": ["Tide Paper"],
            "desc": ["An invented description"],
            "----:org.example.tide:Custom": [MP4FreeForm(b"kept as named")],
            "----:com.apple.iTunes:iTunSMPB": [MP4FreeForm(b" 00000000 00000840")],
            "\xa9too": ["Lavf61"],
        }
    )
    audio.save()
    found = MP4(_upgrade(tmp_path, m4a, "aac-m4a")).tags
    assert found is not None
    assert bytes(found["----:org.example.tide:Custom"][0]) == b"kept as named"
    assert found["desc"] == ["An invented description"] and found["\xa9nam"] == ["Lantern 7"]
    assert "----:com.apple.iTunes:iTunSMPB" not in found and "\xa9too" not in found
    wv = _tone(tmp_path, "wavpack")
    ape = WavPack(wv)
    if ape.tags is None:
        ape.add_tags()
    assert ape.tags is not None
    ape.tags.update({"Title": "Lantern 8", "Artist": "Tide Paper",
                     "Custom Key": "kept as named", "Encoder": "invented 1.0"})  # fmt: skip
    ape.save()
    found = WavPack(_upgrade(tmp_path, wv, "wavpack")).tags
    assert found is not None
    assert str(found["Custom Key"]) == "kept as named" and str(found["Title"]) == "Lantern 8"
    assert "Encoder" not in found
    flac = _tone(tmp_path, "flac")
    vorbis = FLAC(flac)
    vorbis["title"], vorbis["My_Key"], vorbis["encoder"] = "Lantern 9", "kept", "Lavf61"
    vorbis.save()
    found_ogg = OggVorbis(_upgrade(tmp_path, flac, "vorbis"))  # (FLAC to Ogg: Vorbis comments)
    assert found_ogg["my_key"] == ["kept"] and found_ogg["title"] == ["Lantern 9"]
    assert "encoder" not in found_ogg
