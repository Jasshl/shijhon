"""Representation and warm query identity through the production native composition."""

from __future__ import annotations

import gzip
import json
import time
from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from typing import Any

import httpx
import pytest
from defusedxml import ElementTree

from shijhon.collection.contracts import Query
from shijhon.proxy.app import Reply
from shijhon.views.answers import LibraryAnswer
from tests.acceptance.test_A_fidelity import canonical_body
from tests.conftest import NavidromeFactory
from tests.harness.collection_app import prepared_searches
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import simple_album, write_album
from tests.harness.navidrome import NavidromeInstance

CALLBACK = "fixture.library"


class ProtocolWorld:
    def __init__(self, nd: NavidromeInstance, world: DeliveryWorld, owner: str, token: str) -> None:
        self.nd, self.world, self.owner, self.token = nd, world, owner, token
        self.direct = nd.client("protocol-listener", "fixture-password", salt="protocol-salt")
        self.client = self.direct.with_base(world.server.base_url)
        self.songs = self.native(False, []).json()
        self.chosen = {
            next(s["id"] for s in self.songs if s["album"] == album)
            for album in ("First Fiction", "Second Fable")
        }
        self.profile = ""

    def activate(self, profile: str) -> None:
        self.world.server.call(
            lambda: self.world.services.collection.activate_profile(self.owner, profile)
        )

    def read(
        self,
        method: str,
        params: Sequence[tuple[str, Any]],
        *,
        fmt: str = "json",
        verb: str = "GET",
        direct: bool = False,
    ) -> httpx.Response:
        args = list(params)
        if fmt == "jsonp":
            args.extend((("f", "jsonp"), ("callback", CALLBACK)))
        client = self.direct if direct else self.client
        # One request, as an app sends it: apps do not ask again after an answer that the
        # view is being prepared.
        return client.request(
            method, args, fmt="json" if fmt == "json" else "xml", http_method=verb
        )

    def native(self, proxied: bool, params: Sequence[tuple[str, str]]) -> httpx.Response:
        deadline = time.monotonic() + 8
        while True:
            answer = httpx.get(
                (self.world.server.base_url if proxied else self.nd.base_url) + "/api/song",
                headers={"x-nd-authorization": "Bearer " + self.token},
                params=[*params, ("_start", "0"), ("_end", "100"), ("_sort", "id")],
            )
            if answer.status_code != 503 or time.monotonic() >= deadline:
                return answer
            time.sleep(0.05)


def document(answer: httpx.Response, fmt: str) -> dict[str, Any]:
    assert answer.status_code == 200, answer.text
    if fmt == "jsonp":
        assert answer.headers["content-type"].startswith("application/javascript")
        assert answer.text.startswith(CALLBACK + "(") and answer.text.endswith(")")
        value = json.loads(answer.text[len(CALLBACK) + 1 : -1])["subsonic-response"]
    else:
        value = answer.json()["subsonic-response"]
    assert value["status"] == "ok", value
    return value


def song_ids(answer: httpx.Response, fmt: str, container: str) -> list[str]:
    assert answer.status_code == 200, answer.text
    if fmt == "xml":
        root = ElementTree.fromstring(answer.content)
        assert root.attrib["status"] == "ok"
        return [song.attrib["id"] for song in root.findall(".//{*}song")]
    return [song["id"] for song in document(answer, fmt)[container].get("song", [])]


@pytest.mark.parametrize("subset", [False, True])
def test_random_genre_patterns_match_native_before_selection(protocol_world, subset):
    fixture = protocol_world
    fixture.activate(fixture.profile if subset else "all")
    selected = fixture.chosen if subset else {song["id"] for song in fixture.songs}
    for fmt in ("json", "xml"):
        for genre in ("Fict%", "Fabl_", "%", "Absent%"):
            params = [("genre", genre), ("size", 500)]
            native = song_ids(
                fixture.read("getRandomSongs", params, fmt=fmt, direct=True), fmt, "randomSongs"
            )
            actual = song_ids(fixture.read("getRandomSongs", params, fmt=fmt), fmt, "randomSongs")
            assert sorted(actual) == sorted(set(native) & selected), (fmt, genre)


@pytest.fixture(scope="module")
def protocol_world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ProtocolWorld]:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Protocol One", "First Fiction", 3, genre="Fiction"))
    write_album(nd.music, simple_album("Protocol Two", "Second Fable", 3, genre="Fable"))
    nd.scan(full=True)
    owner = nd.create_user("protocol-listener", "fixture-password")
    token = nd.login("protocol-listener", "fixture-password")["token"]
    with delivery_world(nd, tmp_path_factory.mktemp("collection-protocol")) as world:
        browser = Browser(world.server.base_url)
        try:
            assert browser.sign_in("protocol-listener", "fixture-password").status_code == 303
        finally:
            browser.close()
        world.server.call(lambda: world.services.collection.refresh(owner, "1"))
        fixture = ProtocolWorld(nd, world, owner, token)

        async def select() -> None:
            collection = world.services.collection
            fixture.profile = await collection.create_profile(
                owner, "Protocol subset", start="empty"
            )
            operation = await collection.prepare(
                owner,
                "1",
                Query(view="all", scope="song"),
                action="save",
                selected=["native:" + song for song in fixture.chosen],
                profile_id=fixture.profile,
                request_key="protocol-subset",
            )
            await collection.tick()
            frozen = await collection.operation(owner, operation)
            assert frozen["state"] == "review" and frozen["total"] == 2
            await collection.approve(owner, operation)
            await collection.tick()
            assert (await collection.operation(owner, operation))["state"] == "done"
            # A song added by hand brings its album: the others are removed by hand, so
            # the subset is exactly the songs chosen.
            rows = await collection.store.fetchall(
                "SELECT track_key FROM collection_tracks WHERE library_id='1' AND present=1"
            )
            others = [r["track_key"] for r in rows if r["track_key"][7:] not in fixture.chosen]
            operation = await collection.prepare(
                owner,
                "1",
                Query(view="all", scope="song"),
                action="remove",
                selected=others,
                profile_id=fixture.profile,
                request_key="protocol-subset-rest",
            )
            await collection.tick()
            await collection.approve(owner, operation)
            await collection.tick()
            assert (await collection.operation(owner, operation))["state"] == "done"

        world.server.call(select)
        try:
            yield fixture
        finally:
            fixture.client.close()
            fixture.direct.close()


def test_head_reads_describe_selected_lists_and_complete_album_gets(
    protocol_world: ProtocolWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = protocol_world
    reply = LibraryAnswer.reply
    head_bodies: list[bytes] = []

    def observed(answer: LibraryAnswer, **kwargs: Any) -> Reply:
        if kwargs.get("head"):
            assert kwargs.get("document") is None
            assert kwargs.get("compress") is True  # this client accepts gzip
            head_bodies.append(answer.body)
        return reply(answer, **kwargs)

    monkeypatch.setattr(LibraryAnswer, "reply", observed)

    def canonical(body: bytes, fmt: str) -> Any:
        if fmt == "jsonp":
            assert body.startswith(CALLBACK.encode() + b"(") and body.endswith(b")")
            body = body[len(CALLBACK) + 1 : -1]
        return canonical_body(
            httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/xml" if fmt == "xml" else "application/json"},
            )
        )

    first = next(s for s in w.songs if s["album"] == "First Fiction")
    for profile in (w.profile, "all"):
        w.activate(profile)
        for fmt in ("json", "xml", "jsonp"):
            for method, params in (
                ("getSongsByGenre", [("genre", "Fiction"), ("count", 2)]),
                ("getAlbum", [("id", first["albumId"])]),
                ("getMusicDirectory", [("id", first["albumId"])]),
                ("getArtist", [("id", first["albumArtistId"])]),
                ("getScanStatus", []),
            ):
                head_bodies.clear()
                head = w.read(method, params, fmt=fmt, verb="HEAD")
                get = w.read(method, params, fmt=fmt)
                assert head.status_code == get.status_code == 200, (method, fmt, head.headers)
                assert head.content == b""
                for field in (
                    "content-type",
                    "content-encoding",
                    "cache-control",
                    "x-total-count",
                    "vary",
                ):
                    assert head.headers.get(field) == get.headers.get(field), (method, fmt, field)
                assert len(head_bodies) == 1
                body = head_bodies[0]
                wire = gzip.compress(body, 5) if len(body) >= 1024 else body
                assert int(head.headers["content-length"]) == len(wire)
                assert int(get.headers["content-length"]) == get.num_bytes_downloaded
                if body == get.content:
                    assert head.headers["content-length"] == get.headers["content-length"]
                else:
                    # Only native artist role order may vary between separate reads.
                    # HEAD still describes its exact unchanged representation above.
                    assert method in ("getArtist", "getMusicDirectory")  # both list roles
                    assert canonical(body, fmt) == canonical(get.content, fmt)
                if method == "getSongsByGenre":
                    ids = song_ids(get, fmt, "songsByGenre")
                    assert len(ids) == (1 if profile == w.profile else 2)
                    if profile == w.profile:
                        assert set(ids) <= w.chosen


def test_warm_reverse_repeated_rest_filters_keep_first_value(protocol_world: ProtocolWorld) -> None:
    w = protocol_world
    w.activate(w.profile)
    for fmt in ("json", "xml", "jsonp"):
        for first, second in (("Fiction", "Fable"), ("Fable", "Fiction"), ("Fiction", "Fable")):
            params = [("genre", first), ("genre", second), ("count", 500)]
            expected = song_ids(
                w.read("getSongsByGenre", params, fmt=fmt, direct=True), fmt, "songsByGenre"
            )
            actual = song_ids(w.read("getSongsByGenre", params, fmt=fmt), fmt, "songsByGenre")
            assert len(expected) == 3
            assert actual == [song for song in expected if song in w.chosen]
    # Searches answered directly, then prepared (as results of 500 or more are): each
    # first value its own generation, the repeated one warm.
    written = w.world.services.written
    assert written is not None and written.query_index is not None
    for prepared in (False, True):
        with prepared_searches(written.query_index) if prepared else nullcontext([]) as built:
            for fmt in ("json", "xml", "jsonp"):
                for method in ("search2", "search3"):
                    container = "searchResult2" if method == "search2" else "searchResult3"
                    for first, second in (
                        ("First Fiction", "Second Fable"),
                        ("Second Fable", "First Fiction"),
                        ("First Fiction", "Second Fable"),
                    ):
                        params = [
                            ("query", first),
                            ("query", second),
                            ("artistCount", 0),
                            ("albumCount", 0),
                            ("songCount", 500),
                        ]
                        direct = w.read(method, params, fmt=fmt, direct=True)
                        expected = song_ids(direct, fmt, container)
                        actual = song_ids(w.read(method, params, fmt=fmt), fmt, container)
                        assert len(expected) == 3
                        assert actual == [song for song in expected if song in w.chosen]
        assert len(built) == len(set(built)) == (4 if prepared else 0)


def test_warm_reverse_native_filters_keep_native_multivalue_semantics(
    protocol_world: ProtocolWorld,
) -> None:
    w = protocol_world
    w.activate(w.profile)
    genres = {g["name"]: g["id"] for song in w.songs for g in song["genres"]}
    for first, second in (("Fiction", "Fable"), ("Fable", "Fiction"), ("Fiction", "Fable")):
        params = [("genre_id", genres[first]), ("genre_id", genres[second])]
        direct, actual = w.native(False, params), w.native(True, params)
        assert direct.status_code == actual.status_code == 200, actual.text
        expected = [s["id"] for s in direct.json()]
        assert len(expected) == 6
        assert [s["id"] for s in actual.json()] == [song for song in expected if song in w.chosen]


def test_jsonp_album_details_are_complete_and_preserve_native_refusals(
    protocol_world: ProtocolWorld,
) -> None:
    w = protocol_world
    w.activate(w.profile)
    first = next(s for s in w.songs if s["album"] == "First Fiction")
    album = document(w.read("getAlbum", [("id", first["albumId"])], fmt="jsonp"), "jsonp")["album"]
    assert album["songCount"] == len(album["song"]) == 3
    assert {s["id"] for s in album["song"]} == {
        s["id"] for s in w.songs if s["albumId"] == first["albumId"]
    }
    absent = w.read("getAlbum", [("id", "absent-protocol-album")], fmt="jsonp")
    native = w.read("getAlbum", [("id", "absent-protocol-album")], fmt="jsonp", direct=True)
    assert absent.status_code == native.status_code == 200
    assert absent.content == native.content
    invalid = [("genre", "Fiction"), ("f", "jsonp"), ("callback", "not-a-function()")]
    assert (
        w.read("getSongsByGenre", invalid, fmt="xml").content
        == w.read("getSongsByGenre", invalid, fmt="xml", direct=True).content
    )
    refused = w.nd.client("protocol-listener", "incorrect-fixture-password", salt="refused-salt")
    proxied = refused.with_base(w.world.server.base_url)
    try:
        for method, parameters in (
            ("getSongsByGenre", [("genre", "Fiction")]),
            ("getAlbum", [("id", first["albumId"])]),
            ("getMusicDirectory", [("id", first["albumId"])]),
        ):
            args = [*parameters, ("f", "jsonp"), ("callback", CALLBACK)]
            for verb in ("GET", "HEAD"):
                original = refused.request(method, args, fmt="xml", http_method=verb)
                actual = proxied.request(method, args, fmt="xml", http_method=verb)
                assert actual.status_code == original.status_code == 200
                assert actual.content == original.content
                assert actual.headers["content-type"] == original.headers["content-type"]
    finally:
        proxied.close()
        refused.close()


@pytest.mark.parametrize("album_list", ["getAlbumList", "getAlbumList2"])
def test_star_refreshes_filtered_lists_without_cooling_normal_browse(protocol_world, album_list):
    w = protocol_world
    w.activate(w.profile)
    album = next(song["albumId"] for song in w.songs if song["id"] in w.chosen)
    document(w.read("unstar", [("albumId", album)]), "json")
    container = "albumList2" if album_list.endswith("2") else "albumList"

    def starred():
        return {
            row["id"]
            for row in document(w.read(album_list, [("type", "starred")]), "json")[container].get(
                "album", []
            )
        }

    assert album not in starred()
    browse = "getAlbumList2"
    params = [("type", "alphabeticalByName")]
    before = document(w.read(browse, params), "json")["albumList2"]
    document(w.read("star", [("albumId", album)]), "json")
    try:
        assert album in starred()
        after = document(w.read(browse, params), "json")["albumList2"]
        assert [a["id"] for a in after["album"]] == [a["id"] for a in before["album"]]
    finally:
        document(w.read("unstar", [("albumId", album)]), "json")
    assert album not in starred()


def test_playback_invalidates_only_when_navidrome_records_a_play(protocol_world, monkeypatch):
    w = protocol_world
    w.activate(w.profile)
    song = next(song for song in w.songs if song["id"] in w.chosen)
    index = w.world.services.written.query_index
    assert index is not None
    from unittest.mock import AsyncMock

    changed = AsyncMock(wraps=index.annotations_changed)
    monkeypatch.setattr(index, "annotations_changed", changed)
    before = document(w.read("getSong", [("id", song["id"])]), "json")["song"].get("playCount", 0)
    document(w.read("scrobble", [("id", song["id"]), ("submission", "false")]), "json")
    params = [("mediaId", song["id"]), ("mediaType", "song")]
    for state, position, ignored in (
        ("starting", 0, False),
        ("playing", 0, False),
        ("paused", 0, False),
        ("stopped", 0, False),
        ("stopped", 999999, True),
    ):
        document(
            w.read(
                "reportPlayback",
                [
                    *params,
                    ("state", state),
                    ("positionMs", position),
                    ("ignoreScrobble", str(ignored).lower()),
                ],
            ),
            "json",
        )
    changed.assert_not_awaited()
    document(
        w.read("reportPlayback", [*params, ("state", "stopped"), ("positionMs", 999999)]), "json"
    )
    after = document(w.read("getSong", [("id", song["id"])]), "json")["song"].get("playCount", 0)
    assert after == before + 1
    changed.assert_awaited_once_with(w.owner)
