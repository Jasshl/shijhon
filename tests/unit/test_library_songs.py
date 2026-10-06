"""Which catalog songs the library has (top songs): the caller's own requests, the
match of a song by its ISRC or its title, artist and length."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import anyio
import pytest

from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.navidrome.client import NavidromeError
from shijhon.proxy.params import RestCall
from shijhon.store import Store
from shijhon.views.library_songs import (
    PARALLEL,
    LibrarySongs,
    Unknown,
    as_caller,
    isrcs,
    same_recording,
    same_song,
)

FORM = (b"content-type", b"application/x-www-form-urlencoded")


def test_a_request_as_the_caller_carries_its_credentials_in_a_form() -> None:
    headers = [
        (b"host", b"music.test"),
        (b"x-forwarded-for", b"192.0.2.7"),
        (b"range", b"bytes=0-9"),
        (b"if-none-match", b'"x"'),
        (b"content-type", b"text/plain"),
        (b"content-length", b"3"),
    ]
    query = b"u=ann&t=aa&s=bb&v=1.16.1&c=Player&f=xml&artist=Somebody&count=5&u=other"
    call = RestCall.build("getTopSongs", "GET", b"/rest/getTopSongs.view", query, headers, None)
    asked = as_caller(call, "getSong", [("id", "S1")])
    assert (asked.name, asked.http_method, asked.raw_path) == ("getSong", "POST", b"/rest/getSong")
    assert asked.query == b""  # credentials never in an address
    assert parse_qsl((asked.body or b"").decode()) == [
        ("u", "ann"),
        ("t", "aa"),
        ("s", "bb"),
        ("v", "1.16.1"),
        ("c", "Player"),
        ("u", "other"),  # as sent: Navidrome reads the first
        ("f", "json"),
        ("id", "S1"),
    ]
    assert asked.get("u") == "ann" and asked.get("f") == "json" and asked.get("id") == "S1"
    assert asked.headers == [(b"host", b"music.test"), (b"x-forwarded-for", b"192.0.2.7"), FORM]
    # A caller whose credentials came in a form, with others in the query: in its order.
    form = RestCall.build(
        "getTopSongs", "POST", b"/rest/getTopSongs", b"u=q&p=1", [FORM], b"u=b&p=2"
    )
    again = as_caller(form, "getSong", [("id", "S2")])
    assert again.get("u") == "b" and again.get("p") == "2" and again.getall("u") == ["b", "q"]


def test_the_same_song_by_title_artist_and_length() -> None:
    track = CatalogTrack(
        CatalogRef("test", "1"), "Slow Tide (2011 Remaster)", "The Tidewrights", 201_400
    )
    song = {"title": "Slow Tide", "artist": "The Tidewrights", "duration": 201}
    assert same_song(track, song)
    assert same_song(track, {**song, "duration": 204})  # within a few seconds
    assert not same_song(track, {**song, "duration": 230})
    assert not same_song(track, {**song, "artist": "Another Band"})
    assert not same_song(track, {**song, "title": "Slow Tide (Live)"})
    assert not same_song(track, {**song, "duration": None})
    assert isrcs({"isrc": ["zzabc0000001", "", 5]}) == ["ZZABC0000001"]
    assert isrcs({"isrc": "ZZABC0000002"}) == ["ZZABC0000002"] and isrcs({}) == []


def test_only_the_same_recording_stands_for_a_catalog_song() -> None:
    """A library song stands for a catalog track in a list only
    when it is the same recording - a clean edit is other audio, two known ISRCs that
    differ are two recordings; a shared ISRC, or none to compare, is not a difference."""
    ref = CatalogRef("test", "1")
    explicit = CatalogTrack(ref, "Example", "The Tidewrights", 200_000, isrc="ZZABC0000001")
    clean = CatalogTrack(ref, "Example", "The Tidewrights", 200_000, clean=True)
    song = {"title": "Example", "artist": "The Tidewrights", "duration": 200}
    assert same_song(explicit, song) and same_recording(explicit, song)
    assert same_recording(explicit, {**song, "isrc": ["zzabc0000001", "ZZABC0000009"]})
    assert same_recording(explicit, {**song, "title": "Example (Explicit)"})
    assert same_recording(explicit, {**song, "explicitStatus": "explicit"})
    # Another recording by its ISRC (same_song alone would take it).
    other = {**song, "isrc": ["ZZABC0000002"]}
    assert same_song(explicit, other) and not same_recording(explicit, other)
    # A clean library song is no explicit (or plain) track's, and the other way round.
    for edit in ({**song, "explicitStatus": "clean"}, {**song, "title": "Example (Clean)"}):
        assert same_song(explicit, edit) and not same_recording(explicit, edit)
        assert same_recording(clean, edit)
    assert not same_recording(clean, song)
    assert not same_recording(clean, {**song, "title": "Example (Explicit)"})
    marked = CatalogTrack(ref, "Example (Clean)", "The Tidewrights", 200_000)
    assert same_recording(marked, {**song, "explicitStatus": "clean"})
    assert not same_recording(marked, song)


@pytest.mark.anyio
async def test_a_song_of_another_recording_is_not_the_catalog_songs(tmp_path: Path) -> None:
    """... as the title search finds them: the clean edit and the other ISRC are passed
    over, the same recording is taken - and one sharing the ISRC whatever its title."""

    class Titles:
        def __init__(self, songs: list[dict[str, Any]]) -> None:
            self.songs = songs

        async def subsonic(self, method: str, params: list[tuple[str, str]]) -> dict[str, Any]:
            return {"searchResult3": {"song": self.songs}}

    base = {"title": "Example", "artist": "The Tidewrights", "duration": 200}
    track = CatalogTrack(
        CatalogRef("test", "7"), "Example", "The Tidewrights", 200_000, isrc="ZZABC0000001"
    )
    store = await Store.open(tmp_path / "shijhon.sqlite3")
    try:
        cases: list[tuple[list[dict[str, Any]], str | None]] = [
            ([{**base, "id": "clean", "explicitStatus": "clean"}], None),
            ([{**base, "id": "other", "isrc": ["ZZABC0000002"]}], None),
            (
                [
                    {**base, "id": "clean", "title": "Example (Clean)"},
                    {**base, "id": "other", "isrc": ["ZZABC0000002"]},
                    {**base, "id": "same"},
                ],
                "same",
            ),
            ([{**base, "id": "by-isrc", "title": "Exmpl", "isrc": ["ZZABC0000001"]}], "by-isrc"),
        ]
        for songs, expected in cases:
            library = LibrarySongs(store, Titles(songs), None)  # type: ignore[arg-type]
            found = await library.native([track], search=True)
            assert found == ({track.ref: expected} if expected else {}), songs
    finally:
        await store.close()


class FakeNavidrome:
    """Navidrome's search, as the service account: one song of each title asked for."""

    def __init__(self) -> None:
        self.calls, self.active, self.most = 0, 0, 0
        self.failing = False

    async def subsonic(self, method: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        assert method == "search3"
        self.calls += 1
        self.active += 1
        self.most = max(self.most, self.active)
        try:
            await anyio.sleep(0.02)
            if self.failing:
                raise NavidromeError("unavailable")
            title = dict(params)["query"]
            song = {
                "id": f"N-{title}",
                "title": title,
                "artist": "The Tidewrights",
                "duration": 200,
            }
            return {"searchResult3": {"song": [song]}}
        finally:
            self.active -= 1


@pytest.mark.anyio
async def test_views_at_once_ask_navidrome_once_for_a_song(tmp_path: Path) -> None:
    """Many views of one artist at once (a client's pages, several clients): each song is
    asked for once, a few at a time for all of them, and remembered."""
    store = await Store.open(tmp_path / "shijhon.sqlite3")
    try:
        navidrome = FakeNavidrome()
        songs = LibrarySongs(store, navidrome, None)  # type: ignore[arg-type]
        tracks = [
            CatalogTrack(CatalogRef("test", str(n)), f"Song {n}", "The Tidewrights", 200_000)
            for n in range(6)
        ]
        expected = {t.ref: f"N-Song {n}" for n, t in enumerate(tracks)}
        found: list[dict[CatalogRef, str]] = []

        async def view() -> None:
            found.append(await songs.native(tracks, search=True))

        async with anyio.create_task_group() as tg:
            for _ in range(12):
                tg.start_soon(view)
        assert found == [expected] * 12
        assert navidrome.calls == 6 and navidrome.most <= PARALLEL
        assert await songs.native(tracks[:2], search=True) == dict(list(expected.items())[:2])
        assert navidrome.calls == 6  # remembered
        assert await songs.native(tracks, search=False) == {}  # an artist the library lacks
        # Navidrome failing: not known - and nothing remembered of it.
        other = CatalogTrack(CatalogRef("test", "x"), "Another", "The Tidewrights", 200_000)
        navidrome.failing = True
        with pytest.raises(Unknown):
            await songs.native([other], search=True)
        navidrome.failing = False
        assert await songs.native([other], search=True) == {other.ref: "N-Another"}
    finally:
        await store.close()
