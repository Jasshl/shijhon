"""What counts as a use in Navidrome's records: a fill's album favorite or rating only
when given after the fill, its plays never (they are the owned songs' too); a catalog
album's any time; a song's favorite taken back still counts; Navidrome's times read in
its formats."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from shijhon.navidrome.usage import _after, in_use

TABLES = """
CREATE TABLE album (id TEXT PRIMARY KEY, album_artist_id TEXT);
CREATE TABLE media_file (id TEXT PRIMARY KEY, album_id TEXT, artist_id TEXT,
                         album_artist_id TEXT);
CREATE TABLE annotation (user_id TEXT, item_id TEXT, item_type TEXT, play_count INTEGER,
                         play_date TEXT, rating INTEGER, starred INTEGER, starred_at TEXT,
                         rated_at TEXT);
CREATE TABLE playlist_tracks (playlist_id TEXT, media_file_id TEXT);
CREATE TABLE bookmark (item_id TEXT);
CREATE TABLE playqueue (items TEXT);
CREATE TABLE share (id TEXT, resource_ids TEXT, resource_type TEXT);
CREATE TABLE scrobbles (media_file_id TEXT, user_id TEXT, submission_time INTEGER);
"""
FILLED_AT = datetime(2025, 3, 10, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def navidrome() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(TABLES)
    conn.execute("INSERT INTO media_file VALUES ('s1', 'a1', 'ar1', 'ar1')")
    return conn


def album(conn: sqlite3.Connection, **values: object) -> None:
    conn.execute("DELETE FROM annotation")
    columns = {"play_count": 0, "rating": 0, "starred": 0, **values}
    names = ", ".join(columns)
    conn.execute(
        f"INSERT INTO annotation (item_id, item_type, {names}) VALUES"  # noqa: S608
        f" ('a1', 'album', {', '.join('?' * len(columns))})",
        list(columns.values()),
    )


def test_a_fill_counts_album_actions_after_it_and_no_album_plays(
    navidrome: sqlite3.Connection,
) -> None:
    def fill() -> list[str]:
        return in_use(navidrome, ["s1"], "a1", album_since=FILLED_AT)

    album(navidrome, starred=1, starred_at="2025-02-10 06:15:00.5-03:00")
    assert fill() == []  # favorited before the fill
    album(navidrome, starred=1, starred_at="2025-03-11 19:40:00.123456789+05:30")
    assert fill() == ["its album favorited"]
    album(navidrome, rating=4, rated_at="2025-03-10T12:30:00Z")
    assert fill() == ["its album rated"]
    album(navidrome, play_count=40, play_date="2025-03-20 10:00:00")
    assert fill() == []  # the owned songs' plays
    # A catalog album: its own actions and plays, whenever.
    assert in_use(navidrome, ["s1"], "a1", album_since=0, album_plays=True) == ["its album played"]
    album(navidrome, starred=1, starred_at="2025-02-10 08:00:00-01:00")
    assert in_use(navidrome, ["s1"], "a1", album_since=0) == ["its album favorited"]
    # fills-undo: no album actions at all.
    assert in_use(navidrome, ["s1"], "a1") == []
    # Taken back: Navidrome keeps the album's record, and its time.
    album(navidrome, starred=0, starred_at="2025-03-12 10:00:00+09:00")
    assert fill() == ["its album favorited or rated once"]
    album(navidrome, starred=0, starred_at="2025-02-12 10:00:00+00:00")
    assert fill() == []
    assert in_use(navidrome, ["s1"], "a1", album_since=0, album_plays=True) == [
        "its album favorited, rated or played once"
    ]


def test_a_song_used_once_or_by_its_plays_stays(navidrome: sqlite3.Connection) -> None:
    navidrome.execute(
        "INSERT INTO annotation (item_id, item_type, play_count, rating, starred)"
        " VALUES ('s1', 'media_file', 0, 0, 0)"
    )
    assert in_use(navidrome, ["s1"]) == ["favorited, rated or played once"]
    navidrome.execute("DELETE FROM annotation")
    navidrome.execute("INSERT INTO scrobbles VALUES ('s1', 'u1', 1)")
    assert in_use(navidrome, ["s1"]) == ["played"]
    navidrome.execute("DELETE FROM scrobbles")
    navidrome.execute("INSERT INTO playqueue VALUES ('x,s1')")
    assert in_use(navidrome, ["s1"]) == ["in a play queue"]
    navidrome.execute("DELETE FROM playqueue")
    navidrome.execute("INSERT INTO share VALUES ('sh', 'ar1', 'artist')")
    assert in_use(navidrome, ["s1"]) == ["shared"]  # the song's album's artist, as Navidrome has it


def test_navidromes_times_are_read_in_its_formats() -> None:
    at = datetime(2025, 3, 14, 7, 45, 30, tzinfo=UTC).timestamp()
    for text in (
        "2025-03-14 03:45:30.250000-04:00",
        "2025-03-14T07:45:30Z",
        "2025-03-14 07:45:30",
        "2025-03-14 13:15:30.125000001+0530",
    ):
        assert _after(text, at), text
        assert not _after(text, at + 60), text
    assert _after("not a time", at + 10**9)  # unreadable: kept
    assert _after(None, at)
