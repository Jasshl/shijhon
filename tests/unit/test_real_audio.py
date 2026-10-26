"""Accounts that get real audio only (``[placeholders] real_audio_accounts``): who they are,
which songs have real audio, and their answers with only those (suite C has the routes
against a real Navidrome)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import httpx
import pydantic
import pytest

from shijhon.collection.policy import real_audio_predicate
from shijhon.config import PlaceholderSettings
from shijhon.proxy import xmlform
from shijhon.proxy.auth import CredentialChecker
from shijhon.proxy.params import RestCall, folders_read
from shijhon.proxy.upstream import Upstream
from shijhon.store import Store
from shijhon.views.real_audio import RealAudio, RealAudioAccounts, fold, keep, shown_to_native

pytestmark = pytest.mark.anyio


def test_accounts_are_named_as_navidrome_signs_them_in() -> None:
    accounts = RealAudioAccounts(["Analyzer", "Ärger"])
    assert accounts and not RealAudioAccounts([])
    assert accounts.named("analyzer") and accounts.named("ANALYZER")
    assert not accounts.named("analyzer2")
    # SQLite's NOCASE, Navidrome's rule: ASCII letters only.
    assert fold("Ärger") == "Ärger" and accounts.named("ÄRGER")
    assert not accounts.named("ärger")


def test_which_requests_may_be_theirs_without_asking_navidrome() -> None:
    accounts = RealAudioAccounts(["analyzer"])
    assert accounts.may_be(["Analyzer"], True)
    assert accounts.may_be(["someone", "analyzer"], True)  # a header user, then ``u``
    assert not accounts.may_be(["someone"], True)  # another account: no extra work
    assert accounts.may_be([], True)  # an API key: only Navidrome can tell
    assert not accounts.may_be([], False)  # no credentials: Navidrome answers
    assert not RealAudioAccounts([]).may_be(["analyzer"], True)


def test_the_names_a_request_claims() -> None:
    checker = CredentialChecker(Upstream("http://127.0.0.1:9"), proxy_user_header="Remote-User")

    def claimed(query: bytes, headers: list[tuple[bytes, bytes]] = []) -> tuple[list[str], bool]:  # noqa: B006
        return checker.claimed(RestCall.build("ping", "GET", b"/rest/ping", query, headers, None))

    assert claimed(b"u=Analyzer&p=enc:6869&v=1.16.1&c=analyzer") == (["Analyzer"], True)
    assert claimed(b"u=a&u=b&t=x&s=y") == (["a"], True)  # the first, as Navidrome reads it
    assert claimed(b"apiKey=k&v=1.16.1&c=x") == ([], True)
    assert claimed(b"v=1.16.1&c=x") == ([], False)
    assert claimed(b"u=a&p=x", [(b"remote-user", b"analyzer")]) == (["analyzer", "a"], True)


def test_the_setting() -> None:
    assert PlaceholderSettings().real_audio_accounts == []
    assert PlaceholderSettings(real_audio_accounts=[" analyzer "]).real_audio_accounts == [
        "analyzer"
    ]
    with pytest.raises(pydantic.ValidationError, match="empty user name"):
        PlaceholderSettings(real_audio_accounts=["analyzer", " "])


async def library(tmp_path: Path) -> Store:
    """Songs of every kind: owned (o1), imported (i1), a fill's placeholder (p1), delivered
    audio (d1), a placeholder an owned recording plays for (b1), a file in the placeholder
    folder no row tells of (x1), a song gone from the library (gone)."""
    store = await Store.open(tmp_path / "state.db")
    await store.execute("INSERT INTO collection_index VALUES ('1','g1','ready',7,1,'')")
    await store.execute(
        "INSERT INTO releases VALUES ('test:r','_shijhon/r','al2',NULL,'R','A','{}','{}',1,NULL)"
    )
    for song, state, backing in (("p1", "placeholder", None), ("d1", "delivered", None),
                                 ("b1", "placeholder", "o1")):  # fmt: skip
        await store.execute(
            "INSERT INTO placeholders (song_id,path,placeholder_path,track_ref,release_ref,"
            "title,artist,album,duration_ms,disc,track,tags,state,backing_song_id,created_at)"
            " VALUES (?,?,?,?,'test:r','T','A','R',1000,1,1,'{}',?,?,1)",
            [song, f"_shijhon/{song}", f"_shijhon/{song}", "test:" + song, state, backing],
        )
    for song, original, present in (("o1", 1, 1), ("i1", 1, 1), ("p1", 0, 1), ("d1", 0, 1),
                                    ("b1", 0, 1), ("x1", 0, 1), ("gone", 1, 0)):  # fmt: skip
        await store.execute(
            "INSERT INTO collection_tracks VALUES"
            " ('1',?,?,'r','al','ar','T','R','A','t',1,1,0,'{}',?,?,'g1',1)",
            ["native:" + song, song, original, present],
        )
    return store


async def test_real_audio_is_owned_files_and_delivered_audio(tmp_path: Path) -> None:
    store = await library(tmp_path)
    try:
        p = real_audio_predicate("1", song_expression="t.song_id")
        rows = await store.fetchall(
            f"SELECT t.song_id FROM collection_tracks t WHERE {p.sql}",  # noqa: S608
            list(p.params),
        )
        assert {r["song_id"] for r in rows} == {"o1", "i1", "d1"}
        assert real_audio_predicate("2", song_expression="t.song_id").params == ("2",)
        real = RealAudio(
            RealAudioAccounts(["analyzer"]),
            store,
            *cast(Any, (None, None, None, None, None)),
            library_id="1",
        )
        ids = ["o1", "i1", "p1", "d1", "b1", "x1", "new"]
        # (a song the index does not know yet is no placeholder: Shijhon records those)
        assert await real.silent(ids) == {"p1", "b1", "x1"}
        assert await real.silent([]) == set()
    finally:
        await store.close()


def song(ident: str, seconds: int = 3) -> dict[str, Any]:
    return {"id": ident, "isDir": False, "title": ident, "duration": seconds}


def test_a_playlist_keeps_its_real_songs_and_counts_them() -> None:
    data = {
        "id": "pl",
        "songCount": 3,
        "duration": 9,
        "entry": [song("a"), song("p", 4), song("b")],
    }
    assert keep("getPlaylist", data, {"p"}, {})
    assert [e["id"] for e in data["entry"]] == ["a", "b"]
    assert (data["songCount"], data["duration"]) == (2, 6)
    untouched = {"id": "pl", "songCount": 1, "duration": 3, "entry": [song("a")]}
    before = copy.deepcopy(untouched)
    assert not keep("getPlaylist", untouched, {"p"}, {}) and untouched == before
    only = {"id": "pl", "songCount": 1, "duration": 4, "entry": [song("p", 4)]}
    assert keep("getPlaylist", only, {"p"}, {})
    assert "entry" not in only and (only["songCount"], only["duration"]) == (0, 0)


def test_favorites_keep_albums_and_artists_with_real_audio() -> None:
    data = {
        "artist": [{"id": "ar1", "name": "Kept", "albumCount": 2}, {"id": "ar2", "name": "Gone"}],
        "album": [
            {"id": "al1", "name": "Partly", "songCount": 4, "duration": 12},
            {"id": "al2", "name": "Whole", "songCount": 2, "duration": 7},
            {"id": "al3", "name": "Silent", "songCount": 2, "duration": 6},
            {"id": "al4", "name": "Unknown", "songCount": 1, "duration": 3},
        ],
        "song": [song("a"), song("p")],
    }
    items = {
        "album": {
            "al1": {"songCount": 2, "duration": 6.4},
            "al2": {"songCount": 2, "duration": 6.9},
            "al3": {},
        },
        "artist": {
            "ar1": {"songCount": 3, "stats": {"albumartist": {"albumCount": 1}}},
            "ar2": {},
        },
    }
    assert keep("getStarred2", data, {"p"}, items)
    assert [a["id"] for a in data["artist"]] == ["ar1"] and data["artist"][0]["albumCount"] == 1
    assert [(a["id"], a["songCount"], a["duration"]) for a in data["album"]] == [
        ("al1", 2, 6),
        ("al2", 2, 7),  # every song real: Navidrome's own length stays
    ]
    assert [s["id"] for s in data["song"]] == ["a"]
    nothing = {"song": [song("p")]}
    assert keep("getStarred", nothing, {"p"}, {}) and nothing == {}


def test_queues_bookmarks_and_similar_artists() -> None:
    queue = {"entry": [song("p"), song("a")], "current": "p", "position": 5, "username": "x"}
    assert keep("getPlayQueue", queue, {"p"}, {})
    assert [e["id"] for e in queue["entry"]] == ["a"] and "current" not in queue
    assert "position" not in queue and queue["username"] == "x"
    kept_current = {"entry": [song("p"), song("a")], "current": "a", "position": 5}
    assert keep("getPlayQueue", kept_current, {"p"}, {})
    assert kept_current["current"] == "a" and kept_current["position"] == 5
    by_index = {"entry": [song("p"), song("a"), song("b")], "currentIndex": 2, "position": 1}
    assert keep("getPlayQueueByIndex", by_index, {"p"}, {})
    assert by_index["currentIndex"] == 1 and by_index["position"] == 1
    on_silence = {"entry": [song("p"), song("a")], "currentIndex": 0, "position": 1}
    assert keep("getPlayQueueByIndex", on_silence, {"p"}, {})
    assert "currentIndex" not in on_silence and "position" not in on_silence
    marks = {"bookmark": [{"entry": song("p"), "position": 1}, {"entry": song("a"), "position": 2}]}
    assert keep("getBookmarks", marks, {"p"}, {})
    assert [b["entry"]["id"] for b in marks["bookmark"]] == ["a"]
    info = {"biography": "", "similarArtist": [{"id": "ar1", "name": "K"}, {"id": "ar2"}]}
    assert keep("getArtistInfo", info, set(), {"artist": {"ar1": {"songCount": 1}}})
    assert info["similarArtist"] == [{"id": "ar1", "name": "K"}]
    playing = {"entry": [song("a")]}
    assert not keep("getNowPlaying", playing, {"p"}, {})


def test_folders_as_navidrome_reads_them() -> None:
    def read(method: str, query: bytes) -> list[str]:
        return folders_read(RestCall.build(method, "GET", b"/rest/x", query, [], None))

    assert read("getAlbumList2", b"musicFolderId=01&musicFolderId=%2B2") == ["1", "2"]
    assert read("search3", b"musicFolderId=abc&musicFolderId=%201") == []  # none: all
    assert read("getRandomSongs", b"musicFolderId=1&musicFolderId=001") == ["1"]
    # Methods that do not read it (Navidrome ignores it there): no folder at all.
    for method in ("stream", "getAlbum", "getArtist", "getMusicDirectory", "getGenres"):
        assert read(method, b"musicFolderId=999") == []
    # As Go's ParseInt: any number of leading zeros; past 64 bits, a sign alone, other
    # digits than ASCII's: none (and no long text converted: no error).
    assert read("search3", b"musicFolderId=" + b"9" * 4301) == []
    assert read("search3", b"musicFolderId=" + b"0" * 5000 + b"1") == ["1"]
    assert read("search3", b"musicFolderId=" + b"0" * 2_000_000 + b"x") == []
    assert read("search3", b"musicFolderId=9223372036854775808") == []
    assert read("search3", b"musicFolderId=-9223372036854775808") == ["-9223372036854775808"]
    assert read("search3", b"musicFolderId=-&musicFolderId=%C2%B2&musicFolderId=0") == ["0"]


class Shares:
    """Navidrome's shares (the native list) and their songs, as a test scripts them."""

    def __init__(self) -> None:
        self.listed = [{"id": "p", "username": "Analyzer"}, {"id": "o", "username": "other"}]
        self.songs = {"p": ["a"], "o": ["b"]}
        self.asked = 0

    async def native(self, method: str, path: str, **kwargs: Any) -> Any:
        self.asked += 1
        headers = {"x-total-count": str(len(self.listed))}
        return httpx.Response(200, json=self.listed, headers=headers)

    async def shared_songs(self, share: str) -> list[str]:
        return list(self.songs[share])


async def test_a_share_s_songs_are_read_anew_for_every_link() -> None:
    """A shared playlist of the account gains a placeholder: the very next link sees it -
    no earlier answer lets a delivery through."""
    shares = Shares()
    interceptor = cast(Any, shares)
    interceptor.navidrome = shares
    real = RealAudio(
        RealAudioAccounts(["analyzer"]),
        *cast(Any, (None, None, interceptor, None, None, None)),
        library_id="1",
    )
    assert await real._shared_by_accounts() == {"a"}  # (not the other account's)
    shares.songs["p"].append("silent")
    assert await real._shared_by_accounts() == {"a", "silent"}
    assert shares.asked == 2


def test_positions_to_remove_as_the_account_counts_them() -> None:
    # Entries: a, p (silent), b, q (silent), c - the account sees a, b, c.
    shown = [0, 2, 4]
    assert shown_to_native(["0", "1", "2"], shown, 5) == {"0": "0", "1": "2", "2": "4"}
    assert shown_to_native(["3", "x"], shown, 5) == {"3": "5"}  # past the end stays past it


def test_xml_answers_keep_navidrome_s_elements() -> None:
    envelope = (
        '<subsonic-response xmlns="http://subsonic.org/restapi" status="ok" version="1.16.1"'
        ' type="navidrome" serverVersion="0.64.2 (test)" openSubsonic="true">'
    )
    entry = '<entry id="{0}" isDir="false" title="{0}" duration="3" bpm="0"></entry>'
    bodies = {
        "playlist": (
            '<playlist id="pl" name="Mixed" songCount="2" duration="6" public="false"'
            ' owner="x" created="c" changed="c" coverArt="pl-pl">'
            + entry.format("a")
            + entry.format("p")
            + "</playlist>"
        ),
        "playQueue": (
            '<playQueue current="p" position="5" username="x" changed="c" changedBy="y">'
            + entry.format("a")
            + entry.format("p")
            + "</playQueue>"
        ),
        "bookmarks": (
            '<bookmarks><bookmark position="1" username="x" comment="" created="c" changed="c">'
            '<entry id="p" isDir="false" title="p"></entry></bookmark></bookmarks>'
        ),
        "nowPlaying": (
            '<nowPlaying><entry id="p" isDir="false" title="p" username="x" minutesAgo="0"'
            ' playerId="1" playerName="n" state="" positionMs="0" playbackRate="0"></entry>'
            "</nowPlaying>"
        ),
    }
    for key, inner in bodies.items():
        body = f"{envelope}{inner}</subsonic-response>".encode()
        document = xmlform.load(body)
        assert xmlform.dumps(document) == body  # unchanged: byte for byte
        method = {
            "playlist": "getPlaylist",
            "playQueue": "getPlayQueue",
            "bookmarks": "getBookmarks",
            "nowPlaying": "getNowPlaying",
        }[key]
        assert keep(method, document["subsonic-response"][key], {"p"}, {})
        written = xmlform.dumps(document).decode()
        assert 'id="p"' not in written and written.startswith(envelope)
    document = xmlform.load(f"{envelope}{bodies['playlist']}</subsonic-response>".encode())
    keep("getPlaylist", document["subsonic-response"]["playlist"], {"p"}, {})
    assert xmlform.dumps(document).decode() == (
        f'{envelope}<playlist id="pl" name="Mixed" songCount="1" duration="3" public="false"'
        f' owner="x" created="c" changed="c" coverArt="pl-pl">{entry.format("a")}</playlist>'
        "</subsonic-response>"
    )
