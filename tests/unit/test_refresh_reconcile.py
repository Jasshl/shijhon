"""A new track that is, or may be, a recording the release already has in the library is
not added again (``fill.refresh._reconcile``): the catalog can give a track a new ID, and
move it, without it being another recording."""

from __future__ import annotations

from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.fill.refresh import Refreshed, _reconcile, _Recording

NEW_ID = "a song of the album under a new ID at the catalog; it stays as it is"
MAY_BE = "it may be a song the album already has; not added"


def track(ident: str, title: str, *, isrc: str | None = None, seconds: int = 200) -> CatalogTrack:
    return CatalogTrack(
        ref=CatalogRef("test", ident),
        title=title,
        artist="Someone",
        duration_ms=seconds * 1000,
        disc=1,
        number=9,
        isrc=isrc,
    )


def known(ident: str, title: str, *, isrc: str | None = None, seconds: int = 200) -> _Recording:
    return _Recording(f"test:{ident}", title, isrc, seconds * 1000)


def reconciled(
    tracks: list[CatalogTrack],
    recordings: list[_Recording],
    listed: set[str] = frozenset(),  # type: ignore[assignment]
) -> tuple[list[str], list[str]]:
    """(The tracks to add, why the others are left out.)"""
    result = Refreshed("test:release")
    gained = _reconcile(tracks, recordings, {f"test:{ident}" for ident in listed}, result)
    return [t.ref.id for t in gained], result.skipped


HAS = [
    known("1", "Quiet Engines", isrc="ZZSHJ2600001"),
    known("2", "Paper Lanterns", isrc="ZZSHJ2600002", seconds=180),
    known("3", "Interlude", seconds=45),
]


def test_another_recording_is_added() -> None:
    new = track("9", "Late Harbor", isrc="ZZSHJ2600009")
    assert reconciled([new], HAS, {"1", "2", "3"}) == (["9"], [])
    assert reconciled([track("9", "Late Harbor")], HAS) == (["9"], [])


def test_the_same_isrc_under_a_new_id_is_the_track_it_was() -> None:
    """Whatever its place, its title's spelling or its length at the catalog now."""
    new = track("9", "Paper Lanterns (2019 Remaster)", isrc="zzshj2600002", seconds=240)
    assert reconciled([new], HAS, {"1", "3"}) == ([], [f"{new.title}: {NEW_ID}"])


def test_the_same_title_and_length_without_an_isrc_is_the_track_it_was() -> None:
    new = track("9", "Interlude", isrc="ZZSHJ2600009", seconds=47)  # (the old one has none)
    assert reconciled([new], HAS, {"1", "2"}) == ([], [f"Interlude: {NEW_ID}"])
    bare = track("9", "Quiet Engines", seconds=201)  # (the new one has none)
    assert reconciled([bare], HAS, {"2", "3"}) == ([], [f"Quiet Engines: {NEW_ID}"])


def test_two_isrcs_that_differ_are_two_recordings_whatever_the_title() -> None:
    new = track("9", "Quiet Engines", isrc="ZZSHJ2600099")
    assert reconciled([new], HAS, {"2", "3"}) == (["9"], [])


def test_what_may_be_a_song_of_the_album_is_not_added_either() -> None:
    # The same title with another length, and no ISRCs to tell.
    longer = track("9", "Interlude", seconds=90)
    assert reconciled([longer], HAS, {"1", "2"}) == ([], [f"Interlude: {MAY_BE}"])
    # A recording the catalog still lists under its old ID too.
    twice = track("9", "Paper Lanterns", isrc="ZZSHJ2600002")
    assert reconciled([twice], HAS, {"1", "2", "3"}) == ([], [f"Paper Lanterns: {MAY_BE}"])
    # Two new tracks that fit one song.
    pair = [track("8", "Interlude", seconds=45), track("9", "Interlude", seconds=46)]
    assert reconciled(pair, HAS, {"1", "2"}) == ([], [f"Interlude: {MAY_BE}"] * 2)
    # One new track that fits two songs.
    both = [*HAS, known("4", "Interlude", seconds=46)]
    assert reconciled([track("9", "Interlude", seconds=45)], both) == (
        [],
        [f"Interlude: {MAY_BE}"],
    )


def test_each_track_is_reconciled_on_its_own() -> None:
    tracks = [
        track("7", "Late Harbor", isrc="ZZSHJ2600007"),
        track("8", "Paper Lanterns", isrc="ZZSHJ2600002"),
        track("9", "Interlude", seconds=300),
    ]
    gained, left = reconciled(tracks, HAS, {"1"})
    assert gained == ["7"]
    assert left == [f"Paper Lanterns: {NEW_ID}", f"Interlude: {MAY_BE}"]


def test_new_tracks_with_one_isrc_are_added_once() -> None:
    """Two new IDs for one recording in the same reading: neither is in the library yet,
    so they are compared with each other."""
    tracks = [
        track("7", "Late Harbor", isrc="ZZSHJ2600007"),
        track("8", "Late Harbor", isrc="zzshj2600007"),
        track("9", "Late Harbor"),  # (no ISRC: titles alone do not make two new tracks one)
    ]
    gained, left = reconciled(tracks, HAS)
    assert gained == ["7", "9"]
    assert left == [
        "Late Harbor: the catalog lists this recording twice among its new tracks; added once"
    ]
