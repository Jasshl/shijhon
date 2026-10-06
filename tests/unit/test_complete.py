"""Owned albums shown complete: the release's missing tracks merged into Navidrome's
album entry."""

from __future__ import annotations

from typing import Any

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.fill.fills import Decision
from shijhon.views.complete import complete


def track(number: int, seconds: int, disc: int = 1) -> CatalogTrack:
    ref = CatalogRef("demo", f"{disc}{number:02d}")
    album = CatalogRef("demo", "9")
    return CatalogTrack(ref, f"Track {disc}-{number}", "Artist", seconds * 1000, disc, number,
                          album=album)  # fmt: skip


RELEASE = CatalogRelease(
    CatalogRef("demo", "9"),
    "Album",
    "Artist",
    ReleaseKind.ALBUM,
    "2001-02-03",
    tracks=(track(1, 100), track(2, 200), track(3, 300), track(1, 50, disc=2)),
)


def navidrome_album(song_count: int, duration: int) -> dict[str, Any]:
    songs = [
        # Owned: tagged without a track number, it is the release's track 3.
        {"id": "own-a", "title": "Track 1-3", "track": 0, "discNumber": 1, "duration": 300},
        {"id": "own-b", "title": "Track 1-1", "track": 1, "discNumber": 1, "duration": 100},
    ]
    return {
        "id": "al1",
        "name": "Album (Owned Title)",
        "coverArt": "al-al1_0",
        "year": 1999,
        "genre": "Rock",
        "genres": [{"name": "Rock"}],
        "artists": [{"id": "ar1", "name": "Artist"}],
        "displayArtist": "Artist",
        "songCount": song_count,
        "duration": duration,
        "song": songs,
    }


def decision() -> Decision:
    links = {RELEASE.tracks[2].ref: "own-a", RELEASE.tracks[0].ref: "own-b"}
    return Decision("filled", "", RELEASE, links, [], "demo:9")


def test_missing_tracks_join_in_track_order() -> None:
    album = navidrome_album(2, 400)
    assert complete(album, decision(), {})
    # By the songs' own tags, as Navidrome orders the album once filled: the owned song
    # tagged without a number comes first, as it will then.
    order = [(s.get("discNumber"), s["id"]) for s in album["song"]]
    assert order == [(1, "own-a"), (1, "own-b"), (1, "sh.tr.demo.102"), (2, "sh.tr.demo.201")]
    assert album["songCount"] == 4 and album["duration"] == 400 + 200 + 50
    added = album["song"][2]
    assert (added["albumId"], added["parent"]) == ("al1", "al1")
    assert added["album"] == "Album (Owned Title)"
    assert (added["coverArt"], added["year"], added["genre"]) == ("al-al1_0", 1999, "Rock")
    assert added["albumArtists"] == [{"id": "ar1", "name": "Artist"}]


def test_a_stale_album_row_does_not_skew_the_counts() -> None:
    """Navidrome's album row can still count removed placeholders."""
    album = navidrome_album(19, 5000)
    assert complete(album, decision(), {})
    assert album["songCount"] == 4 and album["duration"] == 300 + 100 + 200 + 50


def test_an_answer_without_the_linked_songs_is_left_alone() -> None:
    album = navidrome_album(2, 400)
    album["song"] = album["song"][:1]
    assert not complete(album, decision(), {})
    assert album["songCount"] == 2 and len(album["song"]) == 1


def test_an_answer_with_other_songs_is_left_alone() -> None:
    """A fill running at this moment: Navidrome's answer already holds placeholders."""
    album = navidrome_album(4, 650)
    album["song"].append({"id": "placeholder", "title": "Track 1-2", "track": 2, "duration": 200})
    assert not complete(album, decision(), {})
    assert album["songCount"] == 4 and len(album["song"]) == 3
