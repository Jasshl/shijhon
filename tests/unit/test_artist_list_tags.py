"""The copy with a file's artist lists (``retag.lists``): only those tags change."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1
from mutagen.mp4 import MP4

from shijhon.music_imports import retag, tagmap
from tests.harness.library import tone

JOINED = "Ash Vane, Bell Orrin"
NAMES = ["Ash Vane", "Bell Orrin"]


def flac(tmp_path: Path, **tags: list[str]) -> bytes:
    path = tmp_path / "a.flac"
    shutil.copyfile(tone(440, 0.3, "flac"), path)
    audio = FLAC(path)
    for key, values in {"title": ["Night"], "artist": [JOINED], "album": ["Night"], **tags}.items():
        audio[key] = values
    audio.save()
    return path.read_bytes()


def copy(data: bytes, values: Any, artist: str | None = JOINED, suffix: str = "flac", **kw: Any):
    target = io.BytesIO()
    result = retag.lists(io.BytesIO(data), target, values, artist, suffix, **kw)
    return result, target.getvalue()


def raw(data: bytes, suffix: str = "flac") -> dict[str, list[str]]:
    handle = io.BytesIO(data)
    audio = tagmap.parse(handle, suffix)
    return tagmap.raw(audio, tagmap.tags_of(audio, handle))


def test_only_the_lists_and_the_pinned_album_artist_are_added(tmp_path: Path) -> None:
    data = flac(tmp_path, comment=["a note"], genre=["Ambient"])
    (before, after, pinned), made = copy(data, {"artists": NAMES}, pin=True)
    assert pinned
    found = raw(made)
    assert found["artists"] == NAMES and found["albumartist"] == [JOINED]
    assert {k: v for k, v in found.items() if k not in ("artists", "albumartist")} == raw(data)
    assert before == retag.raw_digest(raw(data)) and after == retag.raw_digest(found)
    # Undone: the tags it had.
    (_, again, _), back = copy(made, {"artists": None, "albumartist": None}, artist=None)
    assert raw(back) == raw(data) and again == before


def test_a_compilation_and_a_file_with_an_album_artist_get_none_pinned(tmp_path: Path) -> None:
    for tags in ({"compilation": ["1"]}, {"albumartist": ["Ash Vane"]}):
        (_, _, pinned), made = copy(flac(tmp_path, **tags), {"artists": NAMES}, pin=True)
        assert not pinned and raw(made).get("albumartist", []) == tags.get("albumartist", [])


@pytest.mark.parametrize(
    ("tags", "artist", "reason"),
    [
        ({"artists": [JOINED]}, JOINED, "listed"),
        ({"musicbrainz_artistid": ["00000000-0000-4000-8000-000000000001"]}, JOINED, "identified"),
        ({}, "Someone Else", "changed"),
    ],
)
def test_files_that_cannot_take_their_list_are_refused(
    tmp_path: Path, tags: dict[str, list[str]], artist: str, reason: str
) -> None:
    with pytest.raises(retag.Refused) as refused:
        copy(flac(tmp_path, **tags), {"artists": NAMES}, artist=artist)
    assert refused.value.reason == reason


def test_a_copy_that_would_change_another_tag_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = tagmap.set_listed

    def also(tags: Any, kind: str, values: Any) -> None:
        listed(tags, kind, values)
        tags["genre"] = ["Changed"]

    monkeypatch.setattr(tagmap, "set_listed", also)
    with pytest.raises(retag.Refused, match="other_tags"):
        copy(flac(tmp_path), {"artists": NAMES})


def test_a_copy_that_would_join_another_album_is_refused(tmp_path: Path) -> None:
    # (An artists list alone, without the album artist Navidrome showed.)
    with pytest.raises(retag.Refused, match="identity"):
        copy(flac(tmp_path), {"artists": NAMES}, pin=False)


def test_mp3_and_m4a_keep_their_tag_kinds(tmp_path: Path) -> None:
    mp3 = tmp_path / "a.mp3"
    shutil.copyfile(tone(440, 0.3, "mp3"), mp3)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Night"]))
    tags.add(TPE1(encoding=3, text=[JOINED]))
    tags.add(TALB(encoding=3, text=["Night"]))
    tags.save(mp3, v2_version=3)
    (_, _, pinned), made = copy(mp3.read_bytes(), {"artists": NAMES}, suffix="mp3", pin=True)
    out = tmp_path / "b.mp3"
    out.write_bytes(made)
    assert pinned and ID3(out).version[:2] == (2, 3)
    assert raw(made, "mp3")["txxx:artists"] == NAMES
    m4a = tmp_path / "a.m4a"
    shutil.copyfile(tone(440, 0.3, "m4a"), m4a)
    audio = MP4(m4a)
    audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"] = ["Night"], [JOINED], ["Night"]
    audio.save()
    (_, _, _), made = copy(m4a.read_bytes(), {"artists": NAMES}, suffix="m4a", pin=True)
    assert raw(made, "m4a")["artists"] == NAMES and raw(made, "m4a")["aart"] == [JOINED]
