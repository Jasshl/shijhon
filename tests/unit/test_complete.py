"""Owned albums shown complete: the release's missing tracks merged into Navidrome's
album entry, and the complete counts in the selected library's lists."""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.fill.fills import Decision, Shown
from shijhon.proxy.app import Forward, HandlerResult, RequestContext
from shijhon.proxy.auth import Caller
from shijhon.proxy.params import RestCall, header
from shijhon.proxy.upstream import Upstream
from shijhon.views.answers import LibraryAnswer, captured
from shijhon.views.complete import CompleteAlbums, complete


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


# --- the counts in the selected library's lists -----------------------------------------

LISTED = [
    {"id": "al1", "name": "Every owned song selected", "songCount": 2, "duration": 400},
    {"id": "al2", "name": "One of two owned songs selected", "songCount": 1, "duration": 100},
    {"id": "al3", "name": "No plan", "songCount": 2, "duration": 90},
    # The plan's two songs selected, a third original song imported since left out.
    {"id": "al4", "name": "A new song left out", "songCount": 2, "duration": 300},
    # As many songs as the plan links, but one of them another song.
    {"id": "al5", "name": "Another song", "songCount": 2, "duration": 200},
]
MISSING = (200_000, 50_000)
SHOWN = {
    "al1": Shown(2, MISSING, frozenset({"s1", "s2"})),
    "al2": Shown(2, MISSING, frozenset({"s3", "s4"})),
    "al4": Shown(2, MISSING, frozenset({"s5", "s6"})),
    "al5": Shown(2, MISSING, frozenset({"s7", "s8"})),
}
# The selected list's albums listed with all of their original songs, and those songs.
ORIGINALS = {
    "al1": frozenset({"s1", "s2"}),
    "al3": frozenset({"s9", "s10"}),
    "al5": frozenset({"s7", "s11"}),
}


def selected(
    body: bytes,
    status: int = 200,
    original_songs: dict[str, frozenset[str]] | None = None,
    **headers: bytes,
) -> LibraryAnswer:
    """A selected library answer (``collection/visibility.py``) to a list request."""
    return LibraryAnswer(
        status,
        [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
         *((k.replace("_", "-").encode(), v) for k, v in headers.items())],
        body,
        selected_view=True,
        original_songs=original_songs,
    )  # fmt: skip


def album_list(albums: list[dict[str, Any]]) -> bytes:
    payload = {"status": "ok", "version": "1.16.1", "albumList2": {"album": albums}}
    return json.dumps({"subsonic-response": payload}).encode()


async def listed(
    answer: LibraryAnswer, *, ready: bool = False, fmt: str = "json"
) -> tuple[HandlerResult, HandlerResult, list[str]]:
    """(``getAlbumList2``'s reply, the ready reply its inner handler gave, the albums whose
    plans were looked up) for a caller with accepted credentials."""
    looked: list[str] = []

    async def shown(album_ids: Any) -> dict[str, Shown]:
        looked.extend(album_ids)
        return {k: v for k, v in SHOWN.items() if k in looked}

    fills = SimpleNamespace(any_shown=AsyncMock(return_value=True), shown=shown)
    upstream = Upstream("http://native.test")
    upstream.library_answer = AsyncMock(return_value=answer)
    query = b"u=listener&p=fixture-password&v=1.16.1&c=tests&f=" + fmt.encode()
    asked = RestCall.build("getAlbumList2", "GET", b"/rest/getAlbumList2", query, [], None)
    ctx = RequestContext(SimpleNamespace(check=AsyncMock(return_value=Caller("listener"))),
                         asked, {})  # fmt: skip
    inner_reply = answer.reply() if ready else None

    async def inner(call: RestCall, ctx: RequestContext) -> HandlerResult:
        return inner_reply

    try:
        complete_albums = CompleteAlbums(fills, upstream)
        result = await complete_albums.wrap_list("getAlbumList2", inner)(asked, ctx)
    finally:
        await upstream.aclose()
    return result, inner_reply, looked


@pytest.mark.anyio
@pytest.mark.parametrize("ready", [False, True])
async def test_a_selected_list_counts_only_albums_whose_owned_songs_are_all_selected(
    ready: bool,
) -> None:
    """The selection decides which albums and X-Total-Count; an album whose original songs
    are its plan's owned songs, all selected, carries the complete counts. One of which the
    selection holds fewer songs, one with an original song left out or another one in the
    plan's place (the count alone would fit), or one without a plan keeps its own."""
    answer = selected(album_list(LISTED), original_songs=ORIGINALS, x_total_count=b"5")
    result, _, looked = await listed(answer, ready=ready)
    assert sorted(looked) == ["al1", "al2", "al3", "al4", "al5"]
    assert result is not None and not isinstance(result, Forward)
    sent = await captured(result)
    assert sent.selected_view
    assert header(sent.headers, b"x-total-count") == "5"
    assert header(sent.headers, b"cache-control") == "no-store"
    albums = json.loads(sent.body)["subsonic-response"]["albumList2"]["album"]
    assert [(a["id"], a["songCount"], a["duration"]) for a in albums] == [
        ("al1", 4, 400 + 200 + 50),
        ("al2", 1, 100),
        ("al3", 2, 90),
        ("al4", 2, 300),
        ("al5", 2, 200),
    ]


@pytest.mark.anyio
async def test_a_selected_list_without_its_albums_original_songs_counts_nothing() -> None:
    """A selected answer that does not tell its albums' original songs: no plan fits."""
    answer = selected(album_list(LISTED), x_total_count=b"5")
    result, inner_reply, looked = await listed(answer, ready=True)
    assert looked and result is inner_reply


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        # A refusal retained with its encoded bytes and headers.
        selected(
            gzip.compress(b'{"subsonic-response":{"status":"failed","error":{"code":40}}}'),
            content_encoding=b"gzip",
            vary=b"Accept-Encoding",
        ),
        selected(b'{"subsonic-response":{"status":"failed"}}', status=503, retry_after=b"1"),
        # Nothing to count: no album of the list is shown complete.
        selected(album_list(LISTED[2:3]), original_songs=ORIGINALS, x_total_count=b"1"),
    ],
)
async def test_a_selected_ready_reply_left_unchanged_is_sent_as_it_came(
    answer: LibraryAnswer,
) -> None:
    result, inner_reply, _ = await listed(answer, ready=True)
    assert result is inner_reply
