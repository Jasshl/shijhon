"""Suite C (commit half) — every commit endpoint materializes catalog IDs and rewrites
them to native IDs, checked against Navidrome's resulting state. A plain play
of a catalog song commits nothing; the client's "now playing" report does. A saved
play queue commits only its current song's album.

Catalog data comes from the replayed demo records; audio from a fake add-on that has
the fixtures' ISRCs.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CatalogRef
from tests.acceptance.test_C_interception import CLIENT_INFO
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.logs import collected
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient

ALBUM = "album-twins-clean"  # 14 tracks, clean edition
SINGLE = "album-single-radio-edit"  # 1 track, by the artist of "artist-duo"
FIRST_PLAY = "album-duo-deluxe-explicit"  # 16 tracks, played before anything else commits it
FAILING = "album-feat-standard"  # its commit fails in a test
RETRY = "album-ep-without-suffix"  # 9 tracks: a second request during its first play
TOGETHER = "album-editions-mix"  # 17 tracks: concurrent first plays
QUEUED = "album-soundtrack-compilation"  # warm-ahead before a commit
SHOWN = "album-editions-remastered"  # played while the catalog fails
AS_IS = "album-feat-anniversary"  # a format its audio already meets
AROUND = "album-editions-super-deluxe"  # two requests around its commit, the first link failing
UNLISTED = "album-anniversary-super-deluxe"  # named as the album of a song it does not list
CURRENT, OTHER, THIRD = (  # a saved queue
    "album-anniversary-standard",
    "album-soundtrack-expanded",
    "album-twins-explicit",
)


def item(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def tracks(name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = fixture(name)["body"]["tracks"]
    return rows


def song(name: str, index: int) -> str:
    return f"sh.tr.demo.{tracks(name)[index]['ref']['id']}"


@pytest.fixture(scope="module")
def replay() -> Replay:
    return Replay()


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
    replay: Replay,
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true"})
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("commits"),
        catalog=replay.catalog(),
        budget_seconds=8.0,
        warm_ahead_delay_seconds=0.2,
    ) as w:
        addon = w.addon("Source")
        w.add_source(addon)
        audio = w.audio("commit-audio")
        for name in (ALBUM, SINGLE, FIRST_PLAY, FAILING, RETRY, TOGETHER, QUEUED, SHOWN, AS_IS):
            for track in tracks(name):
                addon.add(FakeTrack(isrc=track["isrc"], audio=audio))
        yield w


@pytest.fixture
def client(world: DeliveryWorld) -> Iterator[SubsonicClient]:
    c = world.client()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def fresh_listening(world: DeliveryWorld) -> None:
    """What earlier tests reported playing must not steer this test's warm-ahead."""
    warm = world.services.interceptor.warm
    assert warm is not None
    warm.listening.clear()


@pytest.fixture
def commit_log() -> Iterator[list[str]]:
    with collected("shijhon.views.commits") as lines:
        yield lines


def native(client: SubsonicClient, catalog_id: str) -> str:
    """The native ID behind a committed catalog song (getSong is forwarded then)."""
    answer = client.ok("getSong", {"id": catalog_id})["song"]["id"]
    assert not answer.startswith("sh.")
    return str(answer)


def starred(client: SubsonicClient) -> dict[str, list[str]]:
    found = client.ok("getStarred2")["starred2"]
    return {k: [e["id"] for e in found.get(k, [])] for k in ("song", "album", "artist")}


def test_star_unstar_rating_and_scrobble(client: SubsonicClient) -> None:
    first, second = song(ALBUM, 0), song(ALBUM, 1)
    client.ok("star", {"id": first})
    one = native(client, first)
    assert one in starred(client)["song"]
    client.ok("unstar", {"id": first})
    assert one not in starred(client)["song"]
    client.ok("setRating", {"id": second, "rating": "4"})
    client.ok("scrobble", {"id": second, "submission": "true"})
    entry = client.ok("getSong", {"id": native(client, second)})["song"]
    assert entry["userRating"] == 4 and entry["playCount"] == 1


def test_star_album_and_artist(client: SubsonicClient) -> None:
    client.ok("star", {"albumId": f"sh.al.demo.{item(SINGLE)}"})
    album = client.ok("getAlbum", {"id": f"sh.al.demo.{item(SINGLE)}"})["album"]
    assert album["id"] in starred(client)["album"]
    # The single's artist is now known to Navidrome, so the catalog artist can be starred.
    artist = f"sh.ar.demo.{item('artist-duo')}"
    client.ok("star", {"artistId": artist})
    assert album["artistId"] in starred(client)["artist"]
    # An artist Navidrome does not know cannot be starred yet.
    assert client.error_code("star", {"artistId": f"sh.ar.demo.{item('artist-band')}"}) == 70


def test_now_playing_comes_from_report_playback(client: SubsonicClient) -> None:
    track = song(ALBUM, 2)
    client.ok(
        "reportPlayback",
        {"mediaId": track, "mediaType": "song", "positionMs": "0", "state": "playing"},
    )
    playing = client.ok("getNowPlaying")["nowPlaying"].get("entry", [])
    assert native(client, track) in [e["id"] for e in playing]


def test_playlists_keep_order_and_repeats(client: SubsonicClient) -> None:
    a, b, c = song(ALBUM, 3), song(ALBUM, 4), song(ALBUM, 5)
    created = client.ok(
        "createPlaylist", [("name", "Catalog mix"), ("songId", a), ("songId", b), ("songId", a)]
    )["playlist"]
    client.ok("updatePlaylist", {"playlistId": created["id"], "songIdToAdd": c})
    entries = client.ok("getPlaylist", {"id": created["id"]})["playlist"]["entry"]
    expected = [native(client, x) for x in (a, b, a, c)]
    assert [e["id"] for e in entries] == expected


def test_play_queues(client: SubsonicClient) -> None:
    a, b = song(ALBUM, 6), song(ALBUM, 7)
    client.ok("savePlayQueue", [("id", a), ("id", b), ("current", b), ("position", "0")])
    queue = client.ok("getPlayQueue")["playQueue"]
    assert [e["id"] for e in queue["entry"]] == [native(client, a), native(client, b)]
    assert queue["current"] == native(client, b)
    client.ok("savePlayQueueByIndex", [("id", b), ("id", a), ("currentIndex", "1")])
    indexed = client.ok("getPlayQueueByIndex")["playQueueByIndex"]
    assert [e["id"] for e in indexed["entry"]] == [native(client, b), native(client, a)]
    assert indexed["currentIndex"] == 1


def test_bookmark_and_share(client: SubsonicClient) -> None:
    track = song(ALBUM, 8)
    client.ok("createBookmark", {"id": track, "position": "1500"})
    bookmarks = client.ok("getBookmarks")["bookmarks"]["bookmark"]
    assert native(client, track) in [b["entry"]["id"] for b in bookmarks]
    share = client.ok("createShare", {"id": track})["shares"]["share"][0]
    assert [e["id"] for e in share["entry"]] == [native(client, track)]


def test_stream_download_and_transcode_decision(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    addon: FakeAddon = world.addons[0]
    track = song(ALBUM, 9)
    streamed = client.request("stream", {"id": track})
    assert streamed.status_code == 200 and streamed.content[:4] == b"fLaC"
    assert addon.requests("audio")
    downloaded = client.request("download", {"id": song(ALBUM, 10)})
    assert downloaded.status_code == 200 and downloaded.content[:4] == b"fLaC"
    decision = client.http.post(
        f"{client.base_url}/rest/getTranscodeDecision",
        params=[
            *client.auth_params(),
            ("f", "json"),
            ("mediaId", song(ALBUM, 11)),
            ("mediaType", "song"),
        ],
        content=json.dumps(CLIENT_INFO),
        headers={"content-type": "application/json"},
    )
    assert decision.json()["subsonic-response"]["status"] == "ok"


def materializations(world: DeliveryWorld) -> int:
    commits = world.services.commits
    assert commits is not None
    return commits.materializations


def in_library(world: DeliveryWorld, catalog_song: str) -> bool:
    ref = CatalogRef("demo", catalog_song.rsplit(".", 1)[1])
    views = world.services.views
    assert views is not None
    return world.server.call(lambda: views.materialized.song(ref)) is not None


def test_a_catalog_play_commits_nothing_until_the_client_reports_it(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """Clients fetch songs they may never play; the "now playing" report commits."""
    addon = world.addons[0]
    addon.clear()
    first = song(FIRST_PLAY, 0)
    before = materializations(world)
    answer = client.request("stream", {"id": first})
    assert answer.status_code == 200
    assert answer.content == world.audio("commit-audio").read_bytes()
    assert materializations(world) == before and not in_library(world, first)
    assert client.ok("getSong", {"id": first})["song"]["id"] == first  # still virtual
    client.ok("scrobble", {"id": first, "submission": "false"})
    assert materializations(world) == before + 1
    one = native(client, first)  # the whole release is in the library
    album = client.ok("getSong", {"id": one})["song"]["albumId"]
    assert len(client.ok("getAlbum", {"id": album})["album"]["song"]) == len(tracks(FIRST_PLAY))
    assert one in [e["id"] for e in client.ok("getNowPlaying")["nowPlaying"]["entry"]]
    # A seek, by catalog ID or native ID, continues from the same source and pin.
    for ident in (first, one):
        seek = client.request("stream", {"id": ident}, headers={"Range": "bytes=100-"})
        assert seek.status_code == 206
    assert uses_of(addon, FIRST_PLAY, 0) == 1


def test_a_playback_report_commits_too(world: DeliveryWorld, client: SubsonicClient) -> None:
    track = song(RETRY, 5)
    before = materializations(world)
    client.ok("reportPlayback", {"mediaId": track, "mediaType": "song", "positionMs": "0",
                                 "state": "starting"})  # fmt: skip
    assert materializations(world) == before + 1 and in_library(world, track)


def test_a_play_that_needs_the_placeholder_commits_first(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """Another format or a lower bitrate goes download-first: Navidrome serves the file."""
    addon = world.addons[0]
    addon.clear()
    track = song(TOGETHER, 9)
    before = materializations(world)
    answer = client.request("stream", {"id": track, "maxBitRate": 96, "format": "mp3"})
    assert answer.status_code == 200 and answer.headers["content-type"] == "audio/mpeg"
    assert materializations(world) == before + 1 and in_library(world, track)
    # Its first bytes told it needs converting before the commit; the fetch after the
    # commit read on from that answer: the link was looked up once and asked once.
    isrc = tracks(TOGETHER)[9]["isrc"]
    assert uses_of(addon, TOGETHER, 9) == 1
    assert len([r for r in addon.requests("audio") if r["isrc"] == isrc]) == 1


def test_a_format_its_audio_meets_plays_as_it_is_and_commits_nothing(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """A catalog song asked for in the format its audio already has (FLAC
    here) is a plain stream, committing nothing; its report commits it, and a later range by
    the native ID continues that play as it is - never download-first in the middle of it."""
    addon = world.addons[0]
    track = song(AS_IS, 0)
    before = materializations(world)
    fetches = world.services.download_first.fetches
    answer = client.request("stream", {"id": track, "format": "flac"})
    assert answer.content == world.audio("commit-audio").read_bytes()
    assert materializations(world) == before and not in_library(world, track)
    client.ok("scrobble", {"id": track, "submission": "false"})
    ident = native(client, track)
    for who in (track, ident):
        seek = client.request(
            "stream", {"id": who, "format": "flac"}, headers={"Range": "bytes=100-"}
        )
        assert seek.status_code == 206, who
    assert world.services.download_first.fetches == fetches
    assert uses_of(addon, AS_IS, 0) == 1  # one link for the play


def test_a_seek_after_the_commit_stays_on_the_first_representation(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    """Once the play's pin has expired, a seek by the new native ID must not continue from
    another file: it fails, and the client starts again at byte zero."""
    addon = world.addons[0]
    track = song(RETRY, 7)
    isrc = tracks(RETRY)[7]["isrc"]
    assert client.request("stream", {"id": track}).status_code == 200
    client.ok("scrobble", {"id": track, "submission": "false"})
    ident = native(client, track)
    settings = world.services.deliverer.settings
    original = addon.tracks[isrc].audio
    addon.tracks[isrc].audio = world.audio("another-master", seconds=5)  # another size
    settings.pin_ttl_seconds = 0.0  # the pin has expired
    try:
        seek = client.request("stream", {"id": ident, "f": "json"}, headers={"Range": "bytes=100-"})
    finally:
        settings.pin_ttl_seconds = 1800.0
        addon.tracks[isrc].audio = original
    assert seek.headers["content-type"].startswith("application/json")
    assert seek.json()["subsonic-response"]["status"] == "failed"


def test_a_catalog_song_that_cannot_be_looked_up_is_an_error(
    world: DeliveryWorld, replay: Replay, client: SubsonicClient
) -> None:
    addon = world.addons[0]
    addon.clear()
    track = song(FAILING, 0)
    catalog = world.services.catalog
    assert isinstance(catalog, CachedCatalog)
    catalog._shown.clear()  # never shown by an answer (else it plays as shown)
    replay.failing[f"songs/{track.rsplit('.', 1)[1]}"] = 500
    try:
        answer = client.request("stream", {"id": track, "f": "json"})
    finally:
        replay.failing.clear()
    body = answer.json()["subsonic-response"]
    assert body["status"] == "failed" and "catalog unavailable" in body["error"]["message"]
    assert addon.requests() == []


def test_a_report_whose_commit_fails_is_an_error(
    world: DeliveryWorld, replay: Replay, client: SubsonicClient
) -> None:
    rows = world.placeholder_rows()
    replay.failing[f"albums/{item(FAILING)}"] = 500
    try:
        code = client.error_code("scrobble", {"id": song(FAILING, 1), "submission": "false"})
    finally:
        replay.failing.clear()
    assert code == 0
    assert world.placeholder_rows() == rows


def test_a_song_whose_album_does_not_list_it_writes_nothing(
    world: DeliveryWorld, replay: Replay, client: SubsonicClient
) -> None:
    """The catalog names an album for a song, and that album's answer lacks the song:
    refused before anything is written (not the album first, then the error)."""
    stray = copy.deepcopy(fixture(UNLISTED)["body"]["tracks"][0])
    stray["ref"]["id"] = "999000111"
    replay.by_path["songs/999000111"] = {"path": "songs/999000111", "status": 200, "body": stray}
    rows = world.placeholder_rows()
    try:
        assert client.error_code("star", {"id": "sh.tr.demo.999000111"}) == 70
    finally:
        del replay.by_path["songs/999000111"]
    assert world.placeholder_rows() == rows  # its album was not added


def uses_of(addon: Any, name: str, index: int) -> int:
    isrc = tracks(name)[index]["isrc"]
    return [r["path"] for r in addon.requests("stream")].count(f"/stream/{isrc}")


def test_a_request_by_the_native_id_joins_the_catalog_play(world: DeliveryWorld) -> None:
    """The report commits the album while the play is still being routed: a request by the
    new native ID continues that play instead of asking the add-on again."""
    addon = world.addons[0]
    addon.clear()
    isrc = tracks(RETRY)[0]["isrc"]
    addon.tracks[isrc].resolve_delay = 3.0
    first = song(RETRY, 0)
    audio = world.audio("commit-audio").read_bytes()
    try:
        with ThreadPoolExecutor(2) as pool:
            one = pool.submit(lambda: world.client().request("stream", {"id": first}))
            time.sleep(0.3)
            world.client().ok("scrobble", {"id": first, "submission": "false"})
            assert in_library(world, first) and not one.done()  # still waiting for the add-on
            ident = native(world.client(), first)
            two = pool.submit(lambda: world.client().request("stream", {"id": ident}))
            answers = [one.result(), two.result()]
    finally:
        addon.tracks[isrc].resolve_delay = 0.0
    assert [a.content for a in answers] == [audio, audio]
    assert uses_of(addon, RETRY, 0) == 1


def test_concurrent_catalog_plays_share_one_routing_and_commit_nothing(
    world: DeliveryWorld,
) -> None:
    addon = world.addons[0]
    addon.clear()
    before = materializations(world)
    first = song(TOGETHER, 0)
    methods = ("HEAD", "GET", "GET")
    with ThreadPoolExecutor(len(methods)) as pool:
        answers = list(
            pool.map(
                lambda m: world.client().request("stream", {"id": first}, http_method=m), methods
            )
        )
    assert [a.status_code for a in answers] == [200, 200, 200]
    audio = world.audio("commit-audio").read_bytes()
    assert answers[1].content == answers[2].content == audio
    assert answers[0].headers["content-length"] == str(len(audio))
    assert materializations(world) == before
    assert uses_of(addon, TOGETHER, 0) == 1


def test_a_catalog_play_warms_the_next_songs_of_its_album(world: DeliveryWorld) -> None:
    """Warm-ahead follows the catalog's album order before anything is committed."""
    addon = world.addons[0]
    addon.clear()
    before = materializations(world)
    world.client(client="warm-catalog").request("stream", {"id": song(QUEUED, 2)})
    deadline = time.monotonic() + 5
    while uses_of(addon, QUEUED, 4) == 0 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert [uses_of(addon, QUEUED, i) for i in range(6)] == [0, 0, 1, 1, 1, 0]
    assert materializations(world) == before


def saved_queue(client: SubsonicClient) -> dict[str, Any]:
    queue: dict[str, Any] = client.ok("getPlayQueue").get("playQueue", {})
    return queue


def test_a_saved_queue_commits_only_the_current_song_s_album(
    world: DeliveryWorld, client: SubsonicClient, commit_log: list[str]
) -> None:
    """A queue of search results is not a reason to add every album in it."""
    before = materializations(world)
    other, current, third = song(OTHER, 0), song(CURRENT, 3), song(THIRD, 0)
    queue = [("id", other), ("id", current), ("id", third), ("id", song(OTHER, 1))]
    client.ok("savePlayQueue", [*queue, ("current", current), ("position", "1200")])
    assert materializations(world) == before + 1
    assert in_library(world, current) and not in_library(world, other)
    assert not in_library(world, third)
    saved = saved_queue(client)
    assert [e["id"] for e in saved["entry"]] == [native(client, current)]
    assert saved["current"] == native(client, current) and saved["position"] == 1200
    assert "play queue: left out 3 catalog song(s) not in the library yet (of 4)" in commit_log
    # By index: the current position follows the entries that are kept.
    kept = song(CURRENT, 4)  # its album is in the library now
    indexed = [("id", third), ("id", kept), ("id", other), ("id", current)]
    client.ok("savePlayQueueByIndex", [*indexed, ("currentIndex", "3")])
    got = client.ok("getPlayQueueByIndex")["playQueueByIndex"]
    assert [e["id"] for e in got["entry"]] == [native(client, kept), native(client, current)]
    assert got["currentIndex"] == 1
    assert materializations(world) == before + 1


def test_commits_that_would_change_nothing_do_no_work(
    world: DeliveryWorld, client: SubsonicClient
) -> None:
    commits = world.services.commits
    assert commits is not None
    before = commits.materializations
    album = f"sh.al.demo.{item('album-feat-standard')}"
    track = f"sh.tr.demo.{tracks('album-feat-standard')[0]['ref']['id']}"
    client.ok("unstar", {"albumId": album})  # nothing in the library to unstar
    client.ok("setRating", {"id": track, "rating": "0"})
    # Album downloads with catalog tracks are refused before anything is added.
    refused = client.request("download", {"id": album}).json()["subsonic-response"]
    assert refused["status"] == "failed" and "not supported" in refused["error"]["message"]
    # The jukebox is off in this Navidrome: its own answer (501), nothing added.
    assert client.request("jukeboxControl", {"action": "set", "id": track}).status_code == 501
    assert commits.materializations == before


def test_xml_and_form_post_commits(world: DeliveryWorld, client: SubsonicClient) -> None:
    first, second = song(SINGLE, 0), song(ALBUM, 12)
    xml = client.request("star", {"id": first}, fmt="xml")
    assert 'status="ok"' in xml.text
    assert native(client, first) in starred(client)["song"]
    created = client.request(
        "createPlaylist", [("name", "Form"), ("songId", second)], http_method="POST"
    ).json()["subsonic-response"]["playlist"]
    assert [e["id"] for e in created["entry"]] == [native(client, second)]


def test_a_queue_whose_current_song_cannot_be_added_is_an_error(
    world: DeliveryWorld, replay: Replay, client: SubsonicClient
) -> None:
    """The client's saved queue stays as it was, rather than losing its current song."""
    client.ok("savePlayQueue", [("id", song(ALBUM, 0)), ("current", song(ALBUM, 0))])
    before = saved_queue(client)
    replay.failing[f"albums/{item(FAILING)}"] = 500
    try:
        code = client.error_code(
            "savePlayQueue", [("id", song(FAILING, 2)), ("current", song(FAILING, 2))]
        )
    finally:
        replay.failing.clear()
    assert code == 0
    assert saved_queue(client)["entry"] == before["entry"]


def test_a_song_just_shown_plays_while_the_catalog_fails(
    world: DeliveryWorld, client: SubsonicClient, replay: Replay, commit_log: list[str]
) -> None:
    """A catalog song picked from a search plays while the catalog's lookup of the song
    fails: a song an answer showed moments ago (a search, its album) plays as that answer
    showed it - else every request of the play (and its reports) would be answered with an
    error, and the client would keep buffering. A failure with nothing to fall back on is
    logged."""
    album = f"sh.al.demo.{item(SHOWN)}"
    shown = client.ok("getAlbum", {"id": album})["album"]["song"]
    assert shown[3]["id"] == song(SHOWN, 3)
    paths = [f"songs/{tracks(SHOWN)[3]['ref']['id']}", "songs/1"]
    with collected("shijhon.catalog.cache") as cache_log:
        for path in paths:
            replay.failing[path] = 503
        try:
            answer = client.request("stream", {"id": song(SHOWN, 3)})
            unknown = client.request("stream", {"id": "sh.tr.demo.1"})  # never shown, failing
        finally:
            for path in paths:
                replay.failing.pop(path, None)
    assert answer.status_code == 200
    assert answer.content == world.audio("commit-audio").read_bytes()
    title = tracks(SHOWN)[3]["title"]
    assert f"catalog unavailable (HTTP 503): {title!r} as a recent answer showed it" in cache_log
    failed = unknown.json()["subsonic-response"]
    assert failed["status"] == "failed"
    assert "stream of a catalog item answered with an error: catalog unavailable: HTTP 503" in (
        commit_log
    )


def test_a_request_after_the_commit_gets_the_catalog_plays_next_link(
    world: DeliveryWorld,
) -> None:
    """A catalog play's routing found a link; its report commits
    the album; a request by the new native ID takes that link over - and the link fails for
    both. The routing goes on to the next source, and the request by the native ID gets
    that link as soon as it is found: it does not wait for the routing to end, ask
    the failed link again and then the next source once more."""
    settings = world.services.deliverer.settings
    first_source = world.addons[0]
    first_source.clear()
    isrc = tracks(AROUND)[0]["isrc"]
    audio = world.audio("commit-audio")
    failing = FakeTrack(isrc=isrc, audio=audio, first_byte_delay=5.0, first_byte_status=500)
    first_source.add(failing)
    then = world.addon("Next")
    then.add(FakeTrack(isrc=isrc, audio=audio, resolve_delay=4.0))
    source_id = world.add_source(then)
    first = song(AROUND, 0)
    saved, settings.budget_seconds = settings.budget_seconds, 20.0
    try:
        with ThreadPoolExecutor(2) as pool:
            started = time.monotonic()
            one = pool.submit(lambda: world.client().request("stream", {"id": first}))
            time.sleep(0.3)
            world.client().ok("scrobble", {"id": first, "submission": "false"})
            ident = native(world.client(), first)
            began = time.monotonic()
            # (The commit was done while the first link was still the song's: 5 s.)
            assert began - started < 4.0 and not one.done()
            two = pool.submit(lambda: world.client().request("stream", {"id": ident}))
            answers = [one.result(), two.result()]
            took = time.monotonic() - began
    finally:
        settings.budget_seconds = saved
        first_source.tracks.pop(isrc, None)

        async def as_before() -> None:  # the next source gone, the first one's errors past
            sources = world.services.sources
            await sources.remove(source_id)
            for stored in await sources.stored():
                await sources.set_enabled(stored.id, True)

        world.server.call(as_before)
    assert [a.content for a in answers] == [audio.read_bytes()] * 2
    # One lookup and one link at each source, for both requests together.
    for source in (then, first_source):
        asked = [r["path"] for r in source.requests("stream")]
        assert asked.count(f"/stream/{isrc}") == 1, source.name
    # Both asked the first link (the second took it over), and it failed for both.
    assert len([r for r in first_source.requests("audio") if r["isrc"] == isrc]) == 2
    assert took < 9.5  # (the next link was there about 9 s after the play began)
