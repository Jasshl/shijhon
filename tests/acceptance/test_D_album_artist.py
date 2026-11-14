"""Suite D - the album artist Shijhon predicts is the one the pinned Navidrome shows.

Files without an album artist tag: an artist tag of one value is split on its separators
in any letter case (an empty first part dropped), an artist tag of several values is not
split, and the first value of an artists list is taken whole (``matching/grouping.py``).
"""

from __future__ import annotations

from shijhon.matching.grouping import album_artist, first_artist
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import NavidromeInstance

# album title (distinct whatever the letter case) -> (artist tag values, artists list values)
CASES: dict[str, tuple[list[str], list[str]]] = {
    "Night One": (["Ash Feat. Bell"], []),
    "Night Two": (["Ash FT Bell"], []),
    "Night Three": (["Ash Ft. Bell"], []),
    "Night Four": (["Ash feat. Bell"], []),
    "Night Five": (["Ash FEAT Bell"], []),
    "Night Six": (["Ash Featuring Bell"], []),
    "Night Seven": (["Zed"], ["Ash / Bell", "Cora"]),
    "Night Eight": (["; Bell"], []),
    "Night Nine": (["; Ash; Bell"], []),
    "Night Ten": (["Ash feat. Bell", "Cora"], []),
}


def test_predicted_album_artists_match_navidrome(navidrome: NavidromeInstance) -> None:
    for n, (title, (artist, artists)) in enumerate(CASES.items()):
        write_album(
            navidrome.music,
            Album(
                artist=artist[0],
                title=title,
                folder=f"grouping/{n}",
                tracks=(Track("Lantern", 1, artists=tuple(artist)),),
                write_album_artist=False,
                extra={"artists": artists} if artists else {},
            ),
        )
    navidrome.scan(full=True)
    answer = navidrome.client().ok(
        "getAlbumList2", [("type", "alphabeticalByName"), ("size", "50")]
    )
    shown = {a["name"]: a.get("artist") for a in answer["albumList2"]["album"]}
    predicted = {
        title: album_artist({}, first_artist(artist, artists))
        for title, (artist, artists) in CASES.items()
    }
    assert predicted == {title: shown.get(title) for title in CASES}
    assert predicted["Night One"] == "Ash"
    assert predicted["Night Seven"] == "Ash / Bell"
    assert (predicted["Night Eight"], predicted["Night Nine"]) == ("Bell", "Ash")
    assert predicted["Night Ten"] == "Ash feat. Bell"
