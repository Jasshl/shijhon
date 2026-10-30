from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
import pytest

from shijhon.matching import covers
from shijhon.matching.albums import Album, Song, pairs, same_album, same_name, same_recording


def song(title: str, seconds: float = 200.0, **kw: object) -> Song:
    return Song(
        title, artist=str(kw.pop("artist", "Tide Paper")), duration_ms=int(seconds * 1000), **kw
    )  # type: ignore[arg-type]


def test_same_recording_by_title_artist_and_length() -> None:
    assert same_recording(song("Lantern"), song("Lantern", 201.5))
    assert not same_recording(song("Lantern"), song("Lantern", 203.0))
    assert same_recording(song("Lantern (feat. Glass Orchard)"), song("Lantern"))
    assert same_recording(song("Lantern - 2011 Remaster"), song("Lantern"))
    assert not same_recording(song("Lantern (Live)"), song("Lantern"))
    assert not same_recording(song("Lantern (Remix)"), song("Lantern"))
    assert not same_recording(song("Lantern", artist="Quill & Marrow"), song("Lantern"))
    assert same_recording(song("Lantern", artist=""), song("Lantern"))


def test_clean_edit_is_other_audio() -> None:
    assert not same_recording(song("Lantern (Clean)"), song("Lantern"))
    assert not same_recording(song("Lantern", clean=True), song("Lantern", clean=False))
    assert not same_recording(song("Lantern", clean=True), song("Lantern"))
    assert same_recording(song("Lantern (Clean)"), song("Lantern", clean=True))
    assert same_recording(song("Lantern (Explicit)"), song("Lantern"))


def test_identifiers_decide_when_both_sides_have_them() -> None:
    a = song("Lantern", isrcs=("XXA0L2000001",))
    assert same_recording(a, song("Lantern Light", isrcs=("xxa0l2000001",)))
    assert not same_recording(a, song("Lantern", isrcs=("XXA0L2000002",)))
    assert not same_recording(song("Lantern", recording_id="a"), song("Lantern", recording_id="b"))
    assert same_recording(song("Other", recording_id="a"), song("Lantern", recording_id="A"))
    # A length that is not known (raw AAC): only an identifier can tell.
    assert not same_recording(song("Lantern", 0), song("Lantern"))
    assert same_recording(song("Lantern", 0, isrcs=("X1",)), song("Lantern", isrcs=("X1",)))


def test_pairs_are_unambiguous_and_one_to_one() -> None:
    existing = [song("Intro", 60, number=1), song("Lantern", number=2), song("Intro", 60, number=9)]
    assert pairs([song("Lantern", number=5)], existing) == {0: 1}
    # Two equal candidates: the track position decides, else nothing is matched.
    assert pairs([song("Intro", 60, number=9)], existing) == {0: 2}
    assert pairs([song("Intro", 60)], existing) == {}
    # Two incoming songs claiming one existing song: neither is matched.
    assert pairs([song("Lantern"), song("Lantern", 200.5)], existing) == {}


def test_same_name_counts_edition_words_but_not_spelling() -> None:
    def album(name: str, artist: str = "Tide Paper") -> Album:
        return Album(name, artist)

    assert same_name(album("Night Letters"), album("NightLetters"))
    assert same_name(album("Night Letters"), album("night letters", "TIDE PAPER"))
    assert same_name(album("Night Letters"), album("Night Letters", "Tide Paper & Glass Orchard"))
    assert not same_name(album("Night Letters"), album("Night Letters (Deluxe Edition)"))
    assert not same_name(album("Night Letters"), album("Night Letters", "Quill & Marrow"))
    assert not same_name(album(""), album(""))


def test_same_album_rule() -> None:
    songs = (song("Lantern 1", number=1), song("Lantern 2", number=2))
    original = Album("Night Letters", "Tide Paper", 2015, songs)
    # A reissue sharing a song is the same album, whatever its year or cover.
    reissue = Album("Night Letters", "Tide Paper", 2023, songs[:1])
    assert same_album(original, reissue, covers_differ=lambda: True)
    # Sharing no song: different years are different albums (a self-titled series).
    second = Album("Night Letters", "Tide Paper", 2019, (song("Ember", number=1),))
    assert not same_album(original, second)
    # Same year, no song in common (a second disc on its own): the covers decide.
    disc_two = Album("Night Letters", "Tide Paper", 2015, (song("Ember", disc=2, number=1),))
    assert same_album(original, disc_two)
    assert same_album(original, disc_two, covers_differ=lambda: None)
    assert same_album(original, disc_two, covers_differ=lambda: False)
    assert not same_album(original, disc_two, covers_differ=lambda: True)
    # A year that is not known does not separate them.
    assert same_album(original, Album("Night Letters", "Tide Paper", None, ()))
    # Another edition's name is another album.
    deluxe = Album("Night Letters (Deluxe Edition)", "Tide Paper", 2015, songs)
    assert not same_album(original, deluxe)


def _picture(path: Path, source: str) -> bytes:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def test_cover_fingerprints_tell_pictures_apart(tmp_path: Path) -> None:
    pattern = _picture(tmp_path / "a.png", "testsrc=size=320x320")
    smaller = _picture(tmp_path / "b.jpg", "testsrc=size=160x160")
    other = _picture(tmp_path / "c.png", "mandelbrot=size=320x320")

    async def prints() -> list[int | None]:
        return [await covers.fingerprint(i) for i in (pattern, smaller, other, b"not a picture")]

    a, b, c, broken = anyio.run(prints)
    assert broken is None
    assert covers.differ(a, b) is False
    assert covers.differ(a, c) is True
    assert covers.differ(a, None) is None


@pytest.mark.parametrize("size", [0, covers.MAX_IMAGE + 1])
def test_cover_fingerprint_refuses_empty_or_oversized_input(size: int) -> None:
    assert anyio.run(covers.fingerprint, b"\0" * size) is None


def test_symbol_only_recording_titles_are_distinct() -> None:
    assert not same_recording(song("?"), song("..."))
    assert not same_recording(song("♫"), song("★"))
    assert same_recording(song("?"), song("?"))
    assert not same_recording(song(""), song(""))


@pytest.mark.parametrize("identifier", ["isrcs", "recording_id"])
def test_shared_identifiers_do_not_join_clean_and_other_versions(identifier: str) -> None:
    same = {identifier: ("XXA0L2000001",) if identifier == "isrcs" else "recording-a"}
    clean = song("Lantern", clean=True, **same)
    other = song("Lantern", clean=False, **same)
    assert not same_recording(clean, other)
    assert not same_recording(other, clean)
    assert same_recording(clean, song("Lantern (Clean)", **same))
