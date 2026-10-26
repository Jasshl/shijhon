"""An app's accepted playlist update of more songs than one request confirms (follow-up
review R2-B1): 500 distinct songs of the library and one catalog song are added; the
catalog song's whole album joins the account's Shijhon library.

Its own world (the fixture of ``test_collection_reasons``): it adds a 500-song album."""

from __future__ import annotations

from unittest.mock import patch

from tests.acceptance.test_collection_reasons import (  # noqa: F401 - the fixture
    LISTED,
    PASSWORD,
    Library,
    library,
    listed,
    ok,
    wait_for,
)
from tests.harness.library import Album, Track, write_album

USER = "largeupdate"


def test_an_accepted_update_of_501_distinct_songs_is_collected(library: Library) -> None:  # noqa: F811
    nd = library.world.nd
    album = Album(
        "Invented Bulk Band",
        "Bulk Originals",
        tuple(Track(f"Song {n}", n, seconds=0.2) for n in range(1, 501)),
    )
    with patch("tests.harness.library.frequency_for", return_value=440):  # one tone, fast
        write_album(nd.music, album)
    nd.scan(full=True)
    native = nd.client()
    try:
        found = native.ok(
            "search3",
            {"query": "Bulk Originals", "songCount": 500, "artistCount": 0, "albumCount": 0},
        )
    finally:
        native.close()
    songs = [s["id"] for s in found["searchResult3"]["song"]]
    assert len(set(songs)) == 500
    library.ids[USER] = nd.create_user(USER, PASSWORD)
    client = library.client(USER, PASSWORD)
    made = ok(client, "createPlaylist", {"name": "An existing playlist"})["playlist"]["id"]
    target = library.native[str(LISTED.tracks[0].ref)]
    adding = [("songIdToAdd", song) for song in [*songs, target]]
    ok(client, "updatePlaylist", [("playlistId", made), *adding], http_method="POST")
    assert ok(client, "getPlaylist", {"id": made})["playlist"]["songCount"] == 501
    library.use(USER, "shijhon")
    wait_for(lambda: listed(client) == library.whole(LISTED.title))
