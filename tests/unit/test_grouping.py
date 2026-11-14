"""Navidrome 0.64.2's album artist, as its album ID uses it (``matching/grouping.py``).

Measured with the pinned release: an artist tag "A, B" alone gives the album artist "A, B";
with an artists list [A, B] and no album artist tag, "A" (another album); with the album
artist tag "A, B" pinned, "A, B" again; a compilation stays "Various Artists"; an artist
tag of one value is split on " / ", " feat. ", "; " (and their like, in any letter case),
not on ", ", "&",
"x", "+", "with", "and", "/" or ";" without spaces; several album artist values, or an
album artists list alone, are joined with " • ". The acceptance tests of the artist lists
check the album IDs that follow from it (``test_artist_lists.py``).
"""

from __future__ import annotations

import pytest

from shijhon.matching.grouping import album_artist, album_key, first_artist


@pytest.mark.parametrize(
    ("artist", "artists", "first"),
    [
        (["Ash, Bell"], [], "Ash, Bell"),
        (["Ash, Bell"], ["Ash", "Bell"], "Ash"),
        (["Ash feat. Bell"], [], "Ash"),
        (["Ash / Bell"], [], "Ash"),
        (["Ash; Bell"], [], "Ash"),
        (["Ash ft Bell"], [], "Ash"),
        # In any letter case (measured: Navidrome splits "Feat.", "FT" as "feat.", "ft").
        (["Ash Feat. Bell"], [], "Ash"),
        (["Ash FEAT. Bell"], [], "Ash"),
        (["Ash FEAT Bell"], [], "Ash"),
        (["Ash Ft. Bell"], [], "Ash"),
        (["Ash FT Bell"], [], "Ash"),
        (["Ash Featuring Bell"], [], "Ash Featuring Bell"),
        (["Ash Feat. Bell / Cole"], [], "Ash"),
        # An empty first part is dropped (measured).
        (["; Bell"], [], "Bell"),
        (["; Ash; Bell"], [], "Ash"),
        (["Ash Feat. "], [], "Ash"),
        (["Ash/Bell"], [], "Ash/Bell"),
        (["Ash;Bell"], [], "Ash;Bell"),
        (["Ash & Bell"], [], "Ash & Bell"),
        (["Ash x Bell"], [], "Ash x Bell"),
        (["Ash", "Bell"], [], "Ash"),  # several values: not split
        ([], [], ""),
    ],
)
def test_the_first_track_artist_is_as_navidrome_reads_it(
    artist: list[str], artists: list[str], first: str
) -> None:
    assert first_artist(artist, artists) == first


def test_the_first_value_of_an_artists_list_is_the_album_artist_unsplit() -> None:
    # Measured: ARTISTS ["Ash / Bell", "Cora"] without an album artist tag shows the album
    # artist "Ash / Bell"; only an artist tag's one value is split.
    assert album_artist({}, first_artist(["Zed"], ["Ash / Bell", "Cora"])) == "Ash / Bell"
    assert album_artist({}, first_artist(["Ash Feat. Bell"])) == "Ash"


def test_an_artists_list_without_an_album_artist_tag_changes_the_album() -> None:
    tags = {"album": ["Night"]}
    joined = album_key(tags, first_artist(["Ash, Bell"]))
    assert album_key(tags, first_artist(["Ash, Bell"], ["Ash", "Bell"])) != joined
    pinned = {**tags, "albumartist": ["Ash, Bell"]}
    assert album_key(pinned, first_artist(["Ash, Bell"], ["Ash", "Bell"])) == joined


def test_a_compilation_keeps_various_artists_whatever_the_track_artists() -> None:
    tags = {"album": ["Night"], "compilation": ["1"]}
    assert album_artist(tags, first_artist(["Ash, Bell"], ["Ash", "Bell"])) == "Various Artists"


def test_album_artists_lists_are_joined_as_navidrome_shows_them() -> None:
    assert album_artist({"albumartist": ["Pale", "Quill"]}, "Cora") == "Pale • Quill"
    assert album_artist({"albumartists": ["Pale", "Quill"]}, "Cora") == "Pale • Quill"
    # The singular tag wins: an album artists list beside it leaves the album as it is.
    assert album_artist({"albumartist": ["Ash, Bell"], "albumartists": ["Ash", "Bell"]}) == (
        "Ash, Bell"
    )
