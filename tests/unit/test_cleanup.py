"""The cleanup's listing on Shijhon's own records: a release is due counting from when it
was added, and delivered audio is a use."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from shijhon.cleanup import Listing, listing, report
from shijhon.navidrome.usage import NOT_GONE, NOT_PRESENT, NOT_THERE, current, not_there, past
from shijhon.store.db import apply_migrations

DAY = 86400.0


def shijhon_database(path: Path) -> sqlite3.Connection:
    """Two releases added now; the second's audio delivered (and so streamed) then."""
    conn = sqlite3.connect(path, isolation_level=None)
    apply_migrations(conn)
    now = time.time()
    for n in (1, 2):
        conn.execute(
            "INSERT INTO releases (ref, folder, album_id, title, artist, album_tags, data,"
            " created_at) VALUES (?, ?, ?, ?, 'Artist', '{}', '{}', ?)",
            [f"x:{n}", f"_shijhon/r{n}", f"a{n}", f"Album {n}", now],
        )
        delivered = now if n == 2 else None
        conn.execute(
            "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref, release_ref,"
            " title, artist, album, duration_ms, disc, track, tags, created_at, state,"
            " delivered_at, last_used_at) VALUES"
            " (?, ?, ?, ?, ?, 'T', 'A', 'B', 1000, 1, 1, '{}', ?, ?, ?, ?)",
            [f"p{n}", f"_shijhon/r{n}/1.flac", f"_shijhon/r{n}/1.flac", f"x:{n}-1", f"x:{n}",
             now, "delivered" if n == 2 else "placeholder", delivered, delivered],
        )  # fmt: skip
    return conn


def navidrome_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE annotation (item_id TEXT, item_type TEXT, starred INTEGER,"
        " rating INTEGER, play_count INTEGER, play_date TEXT, starred_at TEXT, rated_at TEXT);"
        " CREATE TABLE playlist_tracks (playlist_id TEXT, media_file_id TEXT);"
        " CREATE TABLE bookmark (item_id TEXT); CREATE TABLE playqueue (items TEXT);"
        " CREATE TABLE share (id TEXT, resource_ids TEXT, resource_type TEXT);"
        " CREATE TABLE media_file (id TEXT PRIMARY KEY, path TEXT, album_id TEXT,"
        " artist_id TEXT, album_artist_id TEXT, missing INTEGER DEFAULT 0);"
    )
    for n in (1, 2):  # the library's songs, where Shijhon recorded them
        conn.execute(
            "INSERT INTO media_file (id, path, album_id) VALUES (?, ?, ?)",
            [f"p{n}", f"_shijhon/r{n}/1.flac", f"a{n}"],
        )
    conn.commit()
    return conn


def test_releases_are_due_counting_from_when_they_were_added(tmp_path: Path) -> None:
    shijhon = shijhon_database(tmp_path / "shijhon.sqlite3")
    shijhon.row_factory = sqlite3.Row
    navidrome = navidrome_database(tmp_path / "navidrome.db")
    navidrome.row_factory = sqlite3.Row
    now = time.time()
    found = listing(shijhon, navidrome, now=now, unused_days=30)
    assert found.due == [] and found.waiting == 2  # added now
    later = listing(shijhon, navidrome, now=now + 31 * DAY, unused_days=30)
    assert [c.ref for c in later.unused] == ["x:1"]
    [delivered] = [c for c in later.due if c.ref == "x:2"]
    assert delivered.kept == ["delivered audio in place", "streamed or downloaded"]
    # A delivery stays a use after its audio went back to the placeholder.
    shijhon.execute("UPDATE placeholders SET state = 'placeholder', delivered_at = NULL")
    again = listing(shijhon, navidrome, now=now + 31 * DAY, unused_days=30)
    assert [c.ref for c in again.unused] == ["x:1"]


def test_a_database_that_is_not_this_librarys_says_nothing_about_use(tmp_path: Path) -> None:
    """A release looks unused in any database that does not know its songs - another
    Navidrome's, an empty one, an old copy. Its songs must be there at their recorded
    paths, or the listing refuses (a file Navidrome lists as missing is still there)."""
    shijhon = shijhon_database(tmp_path / "shijhon.sqlite3")
    shijhon.row_factory = sqlite3.Row
    shijhon.execute("UPDATE placeholders SET state = 'placeholder', delivered_at = NULL,"
                    " last_used_at = NULL")  # fmt: skip
    navidrome = navidrome_database(tmp_path / "navidrome.db")
    navidrome.row_factory = sqlite3.Row
    later = time.time() + 31 * DAY

    def found() -> Listing:
        return listing(shijhon, navidrome, now=later, unused_days=30)

    assert found().refused == "" and len(found().unused) == 2
    navidrome.execute("UPDATE media_file SET missing = 1")
    assert found().refused == ""  # missing files' records are still this library's
    navidrome.execute("UPDATE media_file SET path = 'elsewhere/1.flac' WHERE id = 'p2'")
    refused = found()
    assert "do not have the songs of 1 of the 2 release(s)" in refused.refused
    assert "Artist - Album 2" in refused.refused and "not this library's" in refused.refused
    assert len(refused.unused) == 2  # listed, and none of them taken out by it
    text = report(refused, mode="on", unused_days=30)
    assert "or Shijhon's records of those releases are out of date" in refused.refused
    assert [c.stranger for c in refused.unused] == [False, True]
    assert "Refused - nothing is taken out: Navidrome's records do not have" in text
    assert "Not taken out (refused):" in text and "Take out:" not in text
    assert "2 release(s) not known to be used for 30 days" in text
    navidrome.execute("DELETE FROM media_file")  # another Navidrome's, or an empty one
    assert "2 of the 2 release(s)" in found().refused
    assert "out of date" not in found().refused
    navidrome.execute("DROP TABLE media_file")  # not Navidrome's at all
    assert found().refused == (
        "what is read as Navidrome's records is not its database (it has no media_file table)"
    )
    assert found().due == []
    # Nothing looks unused: nothing to refuse (what is kept is kept either way).
    navidrome.execute("CREATE TABLE media_file (id TEXT, path TEXT, missing INTEGER)")
    navidrome.execute("INSERT INTO playlist_tracks VALUES ('pl', 'p1'), ('pl', 'p2')")
    assert found().refused == "" and found().unused == []


def test_a_removal_needs_its_songs_present_and_then_missing_in_the_database_read() -> None:
    """Only Navidrome's current database shows what Shijhon does through Navidrome.
    Before a release is touched its songs are there as present files; once its files are
    gone, as missing ones. A copy made at any one time fails one of the two."""
    navidrome = sqlite3.connect(":memory:")
    for table in ("annotation", "playlist_tracks", "bookmark", "playqueue", "share"):
        navidrome.execute(f"CREATE TABLE {table} (x)")
    navidrome.execute("CREATE TABLE media_file (id TEXT, path TEXT, missing INTEGER)")
    navidrome.execute("INSERT INTO media_file VALUES ('p1', 'x/1.flac', 0), ('p2', 'x/2.flac', 1)")
    recorded = {"p1": "x/1.flac", "p2": "x/2.flac"}
    assert not_there(navidrome, recorded) == []
    assert not_there(navidrome, recorded, missing=True) == ["p1"]
    assert not_there(navidrome, recorded, missing=False) == ["p2"]
    assert not_there(navidrome, {"p1": "x/1.flac", "p3": "x/3.flac"}) == ["p3"]
    # A copy made while the release was out (it was added back since): nothing by it.
    assert current(navidrome, recorded, False) == [NOT_PRESENT]
    navidrome.execute("UPDATE media_file SET missing = 0")
    assert current(navidrome, recorded, False) == []
    # A copy made while it was in: it does not show the removal just made.
    assert current(navidrome, recorded, True) == [NOT_GONE]
    navidrome.execute("UPDATE media_file SET missing = 1")
    assert current(navidrome, recorded, True) == []
    navidrome.execute("UPDATE media_file SET path = 'y/1.flac' WHERE id = 'p1'")
    assert current(navidrome, recorded, False) == current(navidrome, recorded, True) == [NOT_THERE]
    navidrome.execute("DROP TABLE media_file")
    assert current(navidrome, recorded, False) == [NOT_THERE]  # not Navidrome's at all
    with pytest.raises(sqlite3.OperationalError):  # an error is an error, not "foreign"
        not_there(navidrome, recorded)


def test_a_use_a_check_saw_keeps_its_release_once_it_is_gone(tmp_path: Path) -> None:
    """Navidrome's database shows the playlists, queues, bookmarks and shares there are
    now; a use an earlier check recorded keeps its release once it was removed again."""
    shijhon = shijhon_database(tmp_path / "shijhon.sqlite3")
    shijhon.row_factory = sqlite3.Row
    shijhon.execute("UPDATE placeholders SET state = 'placeholder', delivered_at = NULL,"
                    " last_used_at = NULL")  # fmt: skip
    navidrome = navidrome_database(tmp_path / "navidrome.db")
    navidrome.row_factory = sqlite3.Row
    now = time.time()
    navidrome.execute("INSERT INTO playlist_tracks VALUES ('pl', 'p1')")
    navidrome.execute("INSERT INTO playqueue VALUES ('other,p2')")
    first = listing(shijhon, navidrome, now=now, unused_days=30)
    # Not due (added now), and what they are used for is seen.
    assert first.due == [] and first.waiting == 2
    assert first.seen == {"x:1": ["in a playlist"], "x:2": ["in a play queue"]}
    shijhon.execute("INSERT INTO seen_uses VALUES ('x:1', 'in a playlist', ?)", [now])
    navidrome.execute("DELETE FROM playlist_tracks")
    navidrome.execute("DELETE FROM playqueue")
    later = listing(shijhon, navidrome, now=now + 31 * DAY, unused_days=30)
    assert later.seen == {} and [c.ref for c in later.unused] == ["x:2"]  # (never recorded)
    [kept] = [c for c in later.due if c.kept]
    assert kept.ref == "x:1" and kept.kept == ["in a playlist once"]
    # There again: said as it is now, once.
    navidrome.execute("INSERT INTO playlist_tracks VALUES ('pl', 'p1')")
    again = listing(shijhon, navidrome, now=now + 31 * DAY, unused_days=30)
    assert [c.kept for c in again.due if c.ref == "x:1"] == [["in a playlist"]]
    assert past(["favorited, rated or played once", "shared"], ["shared"]) == [
        "favorited, rated or played once"
    ]
    # (what Navidrome's own record of a favorite taken back says is not said twice)
    assert past(["favorited", "in a playlist"], ["favorited, rated or played once"]) == [
        "in a playlist once"
    ]
    assert past(["its album rated"], ["its album favorited or rated once"]) == []
    # The uses of a kind the settings leave alone are seen all the same: switched on for
    # it later, a use that ended meanwhile keeps the release.
    navidrome.execute("INSERT INTO bookmark VALUES ('p2')")
    left = listing(shijhon, navidrome, now=now + 31 * DAY, unused_days=30, catalog_albums=False)
    assert left.due == [] and left.waiting == 0
    assert left.seen == {"x:1": ["in a playlist"], "x:2": ["bookmarked"]}


def test_only_the_actions_that_commit_use_an_old_id() -> None:
    """A use adds a release taken out back - the actions that commit a catalog
    album; views, covers, plain streams, HEADs and taking back a favorite or a rating
    are no use."""
    from urllib.parse import urlencode

    from shijhon.cleanup import OldIds
    from shijhon.proxy.params import RestCall

    class Engine:
        def removed_song(self, item: str) -> str | None:
            return "x:1" if item.startswith("song") else None

        def removing_song(self, item: str) -> str | None:
            return "x:2" if item == "going" else None

    def used(method: str, params: list[tuple[str, str]], http: str = "GET") -> set[str]:
        call = RestCall.build(
            method, http, f"/rest/{method}".encode(), urlencode(params).encode(), [], None
        )
        return OldIds(Engine()).used(call)  # type: ignore[arg-type]

    assert used("scrobble", [("id", "song1"), ("submission", "false")]) == {"song1"}
    assert used("star", [("id", "song1"), ("albumId", "album1")]) == {"song1", "album1"}
    assert used("star", [("id", "song1")], "HEAD") == {"song1"}  # Navidrome stars it too
    assert used("stream", [("id", "song1"), ("maxBitRate", "96")], "HEAD") == set()  # asks
    assert used("unstar", [("id", "song1")]) == set()
    assert used("setRating", [("id", "song1"), ("rating", "3")]) == {"song1"}
    assert used("setRating", [("id", "song1"), ("rating", "0")]) == set()
    assert used("stream", [("id", "song1")]) == set()  # a plain stream
    assert used("stream", [("id", "song1"), ("format", "raw"), ("maxBitRate", "96")]) == set()
    assert used("stream", [("id", "song1"), ("maxBitRate", "96")]) == set()  # the stream's
    assert used("download", [("id", "song1")]) == {"song1"}
    assert used("download", [("id", "album1")]) == set()  # an album's archive
    assert used("download", [("id", "going")]) == {"going"}  # being taken out
    assert used("savePlayQueue", [("id", "song1"), ("id", "song2")]) == set()
    assert used("savePlayQueue", [("id", "song1"), ("current", "song2")]) == {"song2"}
    queue = [("id", "song1"), ("id", "song2"), ("currentIndex", "1")]
    assert used("savePlayQueueByIndex", queue) == {"song2"}
    assert used("savePlayQueueByIndex", [("id", "song1"), ("currentIndex", "5")]) == set()
    assert used("savePlayQueueByIndex", [("id", "song1"), ("currentIndex", "\u00b2")]) == set()
    huge = [("id", "song1"), ("currentIndex", "9" * 5000)]
    assert used("savePlayQueueByIndex", huge) == set()
    assert used("jukeboxControl", [("action", "set"), ("id", "song1")]) == {"song1"}
    assert used("jukeboxControl", [("action", "status"), ("id", "song1")]) == set()
    assert used("getTranscodeDecision", [("mediaId", "song1"), ("mediaType", "song")]) == {"song1"}
    assert used("getTranscodeStream", [("mediaId", "p1"), ("mediaType", "podcast")]) == set()
    for view in ("getSong", "getAlbum", "getMusicDirectory", "getCoverArt", "getLyricsBySongId"):
        assert used(view, [("id", "song1")]) == set(), view


def test_the_pages_listing_says_what_became_of_each_release() -> None:
    """The Cleanup page: what a check takes out (or would), what it keeps and why,
    what failed or was left - those it takes out first."""
    from shijhon.cleanup import Candidate, Listing, Swept
    from shijhon.dashboard import cleanup as page

    def candidate(ref: str, kept: list[str] | None = None, *, fill: bool = False) -> Candidate:
        return Candidate(ref, "a" + ref, fill, f"Title {ref}", "Artist", 1.0, ["s1", "s2"],
                         kept or [])  # fmt: skip

    due = [candidate("k", ["favorited"]), candidate("o"), candidate("f", fill=True)]
    listing = Listing(time.time(), due, waiting=3, next_due=time.time() + DAY)
    dry = page.rows(Swept(listing, mode="dry_run"))
    assert [(r.title, r.state, r.words) for r in dry] == [
        ("Title o", "out", "Would be taken out"),
        ("Title f", "out", "Would be taken out"),
        ("Title k", "kept", "Kept, in use: favorited"),
    ]
    assert page.counts(dry) == page.Counts(out=2, kept=1, songs_out=4)
    done = Swept(listing, mode="on", outcomes={"o": "taken out", "f": "failed"})
    on = {r.title: (r.state, r.words) for r in page.rows(done)}
    assert on["Title o"] == ("out", "Taken out")
    assert on["Title f"][0] == "failed" and "tried again at the next check" in on["Title f"][1]
    refused = page.rows(Swept(listing, mode="on", refused="PurgeMissing is always"))
    assert {r.state for r in refused} == {"left", "kept"}
    assert "PurgeMissing is always" in refused[0].words
    gone = page.rows(Swept(listing, mode="on", outcomes={"o": "gone"}, stopped="switched off"))
    words = {r.title: r.words for r in gone}
    assert words["Title o"] == "Not taken out: it was no longer in the library as listed"
    assert words["Title f"] == "Not taken out: the cleanup was switched off meanwhile"
    broken = {r.title: r.words for r in page.rows(Swept(listing, mode="on", stopped="KeyError"))}
    assert "the check stopped (KeyError" in broken["Title o"]
    rows = [r for _ in range(60) for r in dry[:1]]
    shown, number, pages = page.page_of(rows, 9)
    assert (len(shown), number, pages) == (10, 3, 3)  # a page past the end: the last


@pytest.mark.anyio
async def test_the_pages_overview_counts_by_origin(tmp_path: Path) -> None:
    from shijhon.dashboard import cleanup as page
    from shijhon.store import Store

    store = await Store.open(tmp_path / "state.sqlite3")
    try:
        for ref, owned in (("x:1", None), ("x:2", "owned-a"), ("x:3", None)):
            await store.execute(
                "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
                " album_tags, data, created_at) VALUES (?, ?, ?, ?, 'T', 'A', '{}', '{}', 0)",
                [ref, f"f/{ref}", f"al-{ref}", owned],
            )
        for n, (ref, state) in enumerate(
            (("x:1", "placeholder"), ("x:1", "delivered"), ("x:2", "placeholder"))
        ):
            await store.execute(
                "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref,"
                " release_ref, title, artist, album, duration_ms, disc, track, tags, created_at,"
                " state) VALUES (?, ?, ?, ?, ?, 'T', 'A', 'B', 1000, 1, 1, '{}', 0, ?)",
                [f"s{n}", f"p{n}", f"p{n}", f"t{n}", ref, state],
            )
        await store.execute(
            "INSERT INTO removed_releases (ref, album_id, owned_album_id, title, artist, record,"
            " created_at, removed_at) VALUES ('x:9', 'al-9', NULL, 'T', 'A', '{}', 0, 0)"
        )
        await store.execute("INSERT INTO removed_songs (song_id, release_ref) VALUES ('r1', 'x:9')")
        found = await page.overview(store)
        assert found.catalog == page.Origin(releases=1, songs=2, delivered=1)  # x:3: none
        assert found.fills == page.Origin(releases=1, songs=1)
        assert (found.removed_catalog, found.removed_fills, found.removed_songs) == (1, 0, 1)
    finally:
        await store.close()
