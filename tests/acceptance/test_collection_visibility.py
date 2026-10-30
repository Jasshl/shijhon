"""Native web and XML/JSON client views against disposable Navidrome 0.64.2."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from defusedxml import ElementTree

from shijhon.collection.policy import default_profile_id
from shijhon.proxy.params import RestCall
from shijhon.views.answers import library_answer
from tests.conftest import NavidromeFactory
from tests.harness.collection_app import CollectionApp, prepared_searches
from tests.harness.library import simple_album, write_album
from tests.harness.navidrome import NavidromeInstance
from tests.harness.running import RunningServer, running


class World:
    def __init__(
        self, nd: NavidromeInstance, app: CollectionApp, proxy: RunningServer, users: dict[str, str]
    ) -> None:
        self.nd, self.app, self.proxy, self.users = nd, app, proxy, users
        self.tokens = {name: nd.login(name, "fixture-password")["token"] for name in users}

    def native(
        self, name: str, resource: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        deadline = time.monotonic() + 8
        while True:
            response = httpx.get(
                self.proxy.base_url + "/api/" + resource,
                params=params,
                headers={"x-nd-authorization": "Bearer " + self.tokens[name]},
                timeout=10,
            )
            if response.status_code != 503 or time.monotonic() >= deadline:
                return response
            time.sleep(0.05)

    def set_mode(self, name: str, mode: str) -> None:
        selected = {"saved": "shijhon", "all": "all", "original": "original"}[mode]

        async def update() -> None:
            await self.app.store.execute(
                (
                    "INSERT INTO collection_preferences VALUES (?,?,0) ON "
                    "CONFLICT(owner_id) DO UPDATE SET "
                    "active_profile_id=excluded.active_profile_id"
                ),
                [self.users[name], selected],
            )

        self.proxy.call(update)


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    for i in range(4):
        write_album(
            nd.music,
            simple_album(
                "Invented Artist " + str(i % 2),
                "Collection Album " + str(i),
                5,
                genre="Fiction" if i % 2 else "Imaginary",
                seconds=3.7,
            ),
        )
    nd.scan(full=True)
    songs = nd.native(
        "GET", "song", params={"_start": 0, "_end": 500, "_sort": "id", "missing": "false"}
    ).json()
    users = {name: nd.create_user(name, "fixture-password") for name in ("first", "second")}
    app = CollectionApp(
        nd, tmp_path_factory.mktemp("collection-proxy"), songs, wait=2, removed=True
    )
    with running(app) as proxy:
        yield World(nd, app, proxy, users)


def test_native_exact_pages_modes_partial_album_and_direct_playlist(world: World) -> None:
    expected = [s for s in world.app.songs if s["id"] in world.app.originals]
    params = {"_sort": "id", "_start": 2, "_end": 5, "missing": "false"}
    response = world.native("first", "song", params)
    assert response.status_code == 200, response.text
    assert response.headers["x-total-count"] == str(len(expected))
    assert [s["id"] for s in response.json()] == [s["id"] for s in expected[2:5]]
    hidden = next(
        s
        for s in world.app.songs
        if s["id"] not in world.app.originals
        and any(
            o["albumId"] == s["albumId"] and o["id"] in world.app.originals for o in world.app.songs
        )
    )
    assert world.native("first", "song/" + hidden["id"]).json()["id"] == hidden["id"]
    album = world.native("first", "album/" + hidden["albumId"]).json()
    tracks = world.native(
        "first", "song", {"album_id": hidden["albumId"], "_sort": "id", "_start": 0, "_end": 50}
    ).json()
    assert album["songCount"] == len(tracks)
    assert [s["id"] for s in tracks] == [
        s["id"] for s in world.app.songs if s["albumId"] == hidden["albumId"]
    ]
    playlist = world.nd.client("first", "fixture-password").ok(
        "createPlaylist",
        {"name": "Kept order", "songId": [hidden["id"], expected[0]["id"], hidden["id"]]},
    )["playlist"]["id"]
    native_tracks = world.native("first", "playlist/" + playlist + "/tracks").json()
    assert [s["mediaFileId"] for s in native_tracks] == [
        hidden["id"],
        expected[0]["id"],
        hidden["id"],
    ]
    world.set_mode("first", "all")
    assert world.native("first", "song", params).headers["x-total-count"] == str(len(expected))
    world.set_mode("first", "original")
    assert world.native("first", "song", params).headers["x-total-count"] == str(len(expected))
    world.set_mode("first", "all")


def test_json_xml_sync_library_lists_random_and_user_isolation(world: World) -> None:
    first = world.nd.client("first", "fixture-password").with_base(world.proxy.base_url)
    expected = {s["id"] for s in world.app.songs if s["id"] in world.app.originals}
    response = first.request(
        "search3", {"query": "", "songCount": 4, "songOffset": 2, "artistCount": 0, "albumCount": 0}
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["subsonic-response"]["searchResult3"]["song"]) == 4
    xml = first.request(
        "search3",
        {"query": "", "songCount": 4, "songOffset": 2, "artistCount": 0, "albumCount": 0},
        fmt="xml",
    )
    assert xml.status_code == 200, xml.text
    root = ElementTree.fromstring(xml.content)
    assert [s.attrib["id"] for s in root.findall(".//{*}song")] == [
        s["id"] for s in response.json()["subsonic-response"]["searchResult3"]["song"]
    ]
    for method, params, key in (
        ("getRandomSongs", {"size": 500}, "randomSongs"),
        ("getSongsByGenre", {"genre": "Fiction", "count": 500}, "songsByGenre"),
    ):
        result = first.ok(method, params)[key]
        assert {s["id"] for s in result.get("song", [])} <= expected
    albums = first.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 2})["albumList2"][
        "album"
    ]
    for album in albums:
        detail = first.ok("getAlbum", {"id": album["id"]})["album"]
        assert album["songCount"] == sum(
            song["albumId"] == album["id"] and song["id"] in expected for song in world.app.songs
        )
        assert detail["songCount"] == len(detail["song"]) == 5
    for method in ("getArtists", "getIndexes", "getGenres", "getScanStatus"):
        assert first.request(method).status_code == 200
    hidden = next(s["id"] for s in world.app.songs if s["id"] not in expected)

    async def save() -> None:
        await world.app.store.execute(
            "INSERT OR REPLACE INTO collection_membership VALUES (?, ?, '1', ?,1,1,'save')",
            [world.users["first"], default_profile_id(world.users["first"]), "native:" + hidden],
        )

    world.proxy.call(save)
    assert world.native("first", "song", {"_sort": "id", "_start": 0, "_end": 100}).headers[
        "x-total-count"
    ] == str(len(expected) + 1)
    assert world.native("second", "song", {"_sort": "id", "_start": 0, "_end": 100}).headers[
        "x-total-count"
    ] == str(len(expected))


def test_revoked_grants_and_forged_bearer_cannot_use_warm_index(world: World) -> None:
    assert world.native("second", "song", {"_sort": "id"}).status_code == 200
    response = httpx.get(
        world.proxy.base_url + "/api/song",
        headers={"x-nd-authorization": "Bearer " + world.tokens["second"] + "bad"},
    )
    assert response.status_code == 401
    empty_path = world.nd.root / "empty-library"
    empty_path.mkdir()
    empty_library = world.nd.native(
        "POST", "library", json={"name": "Empty fixture", "path": str(empty_path)}
    ).json()["id"]

    async def empty_index() -> None:
        await world.app.store.execute(
            "INSERT INTO collection_index VALUES (?,'empty','ready',0,1,'')", [str(empty_library)]
        )

    world.proxy.call(empty_index)
    grants = "user/" + world.users["second"] + "/library"
    world.nd.native("PUT", grants, json={"libraryIds": [int(empty_library)]})
    try:
        response = world.native("second", "song", {"library_id": 1, "_sort": "id"})
        assert response.status_code == 200 and response.json() == []
        empty = world.native("second", "song", {"_sort": "id"})
        assert empty.status_code == 200 and empty.json() == []
    finally:  # the module's later tests use the second account's library again
        world.nd.native("PUT", grants, json={"libraryIds": [1]})


def test_two_physical_libraries_keep_foreign_music_before_exact_pages(
    navidrome_factory: NavidromeFactory, tmp_path: Any
) -> None:
    nd = navidrome_factory()
    write_album(
        nd.music,
        simple_album("Shared Fixture Artist", "Configured album", 6, genre="Common fiction"),
    )
    foreign_path = nd.root / "foreign-music"
    foreign_path.mkdir()
    write_album(
        foreign_path,
        simple_album("Shared Fixture Artist", "Foreign album", 4, genre="Foreign fiction"),
    )
    foreign_id = int(
        nd.native(
            "POST", "library", json={"name": "Independent music", "path": str(foreign_path)}
        ).json()["id"]
    )
    nd.scan(full=True)
    songs = nd.native("GET", "song", params={"_sort": "id", "_end": 500}).json()
    configured = [s for s in songs if s["libraryId"] == 1]
    foreign = [s for s in songs if s["libraryId"] == foreign_id]
    assert len(configured) == 6 and len(foreign) == 4
    owner = nd.create_user("both-grants", "fixture-password")
    nd.native("PUT", "user/" + owner + "/library", json={"libraryIds": [1, foreign_id]})
    app = CollectionApp(nd, tmp_path / "both", configured, wait=2, removed=True)
    with running(app) as proxy:
        world = World(nd, app, proxy, {"both-grants": owner})
        expected = sorted([*app.originals, *(s["id"] for s in foreign)])
        for start in range(0, len(expected), 2):
            response = world.native(
                "both-grants", "song", {"_sort": "id", "_start": start, "_end": start + 2}
            )
            assert response.status_code == 200, response.text
            assert response.headers["x-total-count"] == str(len(expected))
            assert [s["id"] for s in response.json()] == expected[start : start + 2]
        untouched = world.native(
            "both-grants", "song", {"library_id": foreign_id, "_sort": "id", "_end": 500}
        )
        assert [s["id"] for s in untouched.json()] == sorted(s["id"] for s in foreign)
        assert "x-shijhon-indexed-at" not in untouched.headers
        for kind in ("album", "artist", "tag", "genre"):
            response = world.native("both-grants", kind, {"_sort": "name", "_end": 500})
            assert response.status_code == 200 and response.json(), response.text
            if kind == "artist":
                assert response.json()[0]["songCount"] == len(expected)
        rest = nd.client("both-grants", "fixture-password").with_base(proxy.base_url)
        result = rest.ok(
            "search3", {"query": "", "songCount": 500, "artistCount": 0, "albumCount": 0}
        )
        assert {s["id"] for s in result["searchResult3"]["song"]} == set(expected)
        assert rest.ok("getArtists")["artists"]["index"][0]["artist"][0]["albumCount"] == 2
        # No remembered personal connection is involved in this raw authenticated REST caller.
        assert (
            proxy.call(
                lambda: app.store.fetchone(
                    "SELECT 1 FROM collection_index WHERE library_id=?", [str(foreign_id)]
                )
            )
            is None
        )


def test_rest_missing_source_reports_setup_instead_of_retry_forever(world: World) -> None:
    rest = world.nd.client("first", "fixture-password").with_base(world.proxy.base_url)

    async def state(generation: str, status: str) -> None:
        await world.app.store.execute(
            "UPDATE collection_index SET generation=?,state=?,updated_at=? WHERE library_id='1'",
            [generation, status, 0 if not generation else 1],
        )

    try:
        world.proxy.call(lambda: state("", "missing"))
        for fmt in ("json", "xml"):
            album_id = world.app.songs[0]["albumId"]
            for method, field in (("getAlbum", "album"), ("getMusicDirectory", "directory")):
                detail = rest.request(method, {"id": album_id}, fmt=fmt)
                assert detail.status_code == 200 and "x-shijhon-view-state" not in detail.headers
                if fmt == "json":
                    assert detail.json()["subsonic-response"][field]["songCount"] == 5
                else:
                    assert b'songCount="5"' in detail.content
            response = rest.request("getAlbumList2", {"type": "newest"}, fmt=fmt)
            assert response.status_code == 503
            assert "/shijhon/sign-in?next=/shijhon/collection" in response.text
            assert "x-shijhon-view-state" not in response.headers
        world.proxy.call(lambda: state("", "indexing"))
        pending = rest.request("getAlbumList2", {"type": "newest"})
        assert pending.status_code == 503
        assert pending.headers["x-shijhon-view-state"] == "indexing"
    finally:
        world.proxy.call(lambda: state("fixture", "ready"))


def test_selected_list_duration_complete_details_direct_errors_and_xml_parity(world: World) -> None:
    from tests.acceptance.test_A_fidelity import canonical_body

    direct = world.nd.client("first", "fixture-password", headers={"host": "music.test"})
    proxied = direct.with_base(world.proxy.base_url)
    albums = direct.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})["albumList2"][
        "album"
    ]
    partial = next(
        a
        for a in albums
        if 1
        < sum(s["albumId"] == a["id"] and s["id"] in world.app.originals for s in world.app.songs)
        < a["songCount"]
    )
    selected_songs = [
        s
        for s in world.app.songs
        if s["albumId"] == partial["id"] and s["id"] in world.app.originals
    ]
    expected_duration = int(sum(s["duration"] for s in selected_songs))
    assert expected_duration != sum(int(s["duration"]) for s in selected_songs)
    # Earlier tests may have enabled one catalog candidate; Original remains stable.
    world.set_mode("first", "original")
    listed = proxied.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})["albumList2"][
        "album"
    ]
    assert next(a for a in listed if a["id"] == partial["id"])["duration"] == expected_duration
    detail = proxied.ok("getAlbum", {"id": partial["id"]})["album"]
    assert detail == direct.ok("getAlbum", {"id": partial["id"]})["album"]
    directory = proxied.ok("getMusicDirectory", {"id": partial["id"]})["directory"]
    native_directory = direct.ok("getMusicDirectory", {"id": partial["id"]})["directory"]
    assert directory == native_directory
    assert ("duration" in directory) == ("duration" in native_directory)
    for fmt in ("json", "xml"):
        missing = proxied.request("getAlbum", {"id": "unknown-native-fixture"}, fmt=fmt)
        assert canonical_body(missing) == canonical_body(
            direct.request("getAlbum", {"id": "unknown-native-fixture"}, fmt=fmt)
        )

    async def enable_all() -> None:
        for song in world.app.songs:
            if song["id"] not in world.app.originals:
                await world.app.store.execute(
                    "INSERT OR REPLACE INTO collection_membership VALUES (?,?,'1',?,1,1,'parity')",
                    [
                        world.users["first"],
                        default_profile_id(world.users["first"]),
                        "native:" + song["id"],
                    ],
                )

    world.proxy.call(enable_all)
    world.set_mode("first", "all")
    for fmt in ("json", "xml"):
        for method, params in (
            ("getIndexes", {}),
            ("getArtists", {}),
            ("getGenres", {}),
            ("getScanStatus", {}),
            ("getAlbumList", {"type": "alphabeticalByName"}),
            ("getAlbumList2", {"type": "alphabeticalByName"}),
            ("search2", {"query": "Collection"}),
            ("search3", {"query": "Collection"}),
            ("getSongsByGenre", {"genre": "Fiction"}),
            ("getAlbum", {"id": partial["id"]}),
            ("getMusicDirectory", {"id": partial["id"]}),
        ):
            expected, actual = (
                direct.request(method, params, fmt=fmt),
                proxied.request(method, params, fmt=fmt),
            )
            assert actual.status_code == expected.status_code == 200, (method, actual.text)
            assert canonical_body(actual) == canonical_body(expected), method
    future = proxied.request("getIndexes", {"ifModifiedSince": "9999999999999"}, fmt="xml")
    assert ElementTree.fromstring(future.content).findall(".//{*}artist")


def test_native_query_token_and_refused_rest_head_keep_original_auth(world: World) -> None:
    response = httpx.get(
        world.proxy.base_url + "/api/song",
        params={
            "jwt": world.tokens["first"],
            "_sort": "id",
            "_start": 0,
            "_end": 3,
        },
    )
    assert response.status_code == 200, response.text
    assert "x-shijhon-indexed-at" in response.headers
    invalid = world.nd.client("first", "incorrect-synthetic-password").with_base(
        world.proxy.base_url
    )
    for method, params, verb in (
        ("getArtists", {}, "HEAD"),
        ("getAlbumList2", {"f": "jsonp", "callback": "fixture"}, "GET"),
    ):
        before = world.nd.log_path.read_text(errors="replace").count("Invalid login")
        result = invalid.request(method, params, http_method=verb, fmt="xml")
        assert result.status_code == 200
        assert world.nd.log_path.read_text(errors="replace").count("Invalid login") == before + 1


def test_direct_library_hook_retains_fresh_refusal_without_second_native_login(
    world: World,
) -> None:
    for fmt in ("json", "xml"):
        call = RestCall.build(
            "getArtists",
            "GET",
            b"/rest/getArtists",
            urlencode(
                {
                    "u": "first",
                    "p": "incorrect-synthetic-password",
                    "v": "1.16.1",
                    "c": "hook-fixture",
                    "f": fmt,
                }
            ).encode(),
            [],
            None,
        )
        before = world.nd.log_path.read_text(errors="replace").count("Invalid login")
        answer = world.proxy.call(lambda call=call: library_answer(world.app.native.upstream, call))
        assert answer is not None and answer.status == 200
        assert b"Wrong username or password" in answer.body
        assert world.nd.log_path.read_text(errors="replace").count("Invalid login") == before + 1


def test_direct_partial_details_keep_fresh_caller_annotations(world: World) -> None:
    client = world.nd.client("first", "fixture-password").with_base(world.proxy.base_url)
    world.set_mode("first", "original")
    album_id = next(s["albumId"] for s in world.app.songs if s["id"] in world.app.originals)
    before = client.ok("getAlbum", {"id": album_id})["album"]
    song = before["song"][0]["id"]
    try:
        client.ok("setRating", {"id": album_id, "rating": 4})
        client.ok("star", {"id": song})
        after = client.ok("getAlbum", {"id": album_id})["album"]
        assert after["userRating"] == 4
        assert after["songCount"] == before["songCount"] and after["duration"] == before["duration"]
        assert [s["id"] for s in after["song"]] == [s["id"] for s in before["song"]]
        assert next(s for s in after["song"] if s["id"] == song)["starred"]
        xml = ElementTree.fromstring(
            client.request("getAlbum", {"id": album_id}, fmt="xml").content
        )
        assert xml.find("{*}album").attrib["userRating"] == "4"
        assert next(s for s in xml.findall(".//{*}song") if s.attrib["id"] == song).attrib[
            "starred"
        ]
    finally:
        client.ok("setRating", {"id": album_id, "rating": 0})
        client.ok("unstar", {"id": song})
        world.set_mode("first", "all")


def test_random_genre_comparison_matches_navidrome_case_insensitivity(world: World) -> None:
    direct = world.nd.client("first", "fixture-password")
    client = direct.with_base(world.proxy.base_url)
    world.set_mode("first", "original")
    try:
        params = {"genre": "fIcTiOn", "size": 500}
        expected = direct.ok("getRandomSongs", params)["randomSongs"]["song"]
        assert expected
        actual = client.ok("getRandomSongs", params)["randomSongs"]["song"]
        assert {s["id"] for s in actual} == {s["id"] for s in expected} & world.app.originals
    finally:
        world.set_mode("first", "all")


def test_complete_genre_answers_preserve_native_limit_dependent_order(world: World) -> None:
    from tests.acceptance.test_A_fidelity import assert_selected_headers, canonical_body

    direct = world.nd.client("first", "fixture-password", headers={"host": "music.test"})
    client = direct.with_base(world.proxy.base_url)

    async def enable_all() -> None:
        for song in world.app.songs:
            await world.app.store.execute(
                (
                    "INSERT OR REPLACE INTO collection_membership "
                    "VALUES (?,?,'1',?,1,1,'genre-parity')"
                ),
                [
                    world.users["first"],
                    default_profile_id(world.users["first"]),
                    "native:" + song["id"],
                ],
            )

    world.proxy.call(enable_all)
    world.set_mode("first", "all")
    # Fetching a canonical source of 500
    # must not replace a fully selected request's native LIMIT-dependent tie order.
    for fmt in ("json", "xml"):
        for count in (1, 3, 10, 500):
            for offset in (0, 1):
                params = {"genre": "Fiction", "count": count, "offset": offset}
                actual = client.request("getSongsByGenre", params, fmt=fmt)
                expected = direct.request("getSongsByGenre", params, fmt=fmt)
                assert canonical_body(actual) == canonical_body(expected)
                assert_selected_headers(expected, actual)
                assert actual.headers["cache-control"] == "no-store"
                assert ";" not in actual.headers["content-type"]
                assert "set-cookie" not in actual.headers


def test_a_complete_search_answers_at_once_while_both_index_workers_are_busy(
    world: World,
) -> None:
    """A voice assistant's "play album" search: one request, not asked again. While other
    library preparation holds both index workers, a search whose albums Navidrome answers
    completely in one page is answered at once, the selection applied to it, and nothing of
    it is kept: the same albums and counts as the album list."""
    client = world.nd.client("second", "fixture-password").with_base(world.proxy.base_url)
    params = {"query": "Collection Album", "artistCount": 0, "artistOffset": 0,
              "albumCount": 20, "albumOffset": 0, "songCount": 0, "songOffset": 0}  # fmt: skip
    holders = (object(), object())
    index = world.app.index

    async def hold() -> None:
        for holder in holders:
            # Earlier requests can still have background builds finishing. Wait for
            # their slots before deliberately occupying both for this scenario.
            await index.slots.acquire_on_behalf_of(holder)

    async def release() -> None:
        for holder in holders:
            index.slots.release_on_behalf_of(holder)

    def kept() -> int:
        if not index.path.exists():
            return 0
        with sqlite3.connect(index.path.as_uri() + "?mode=ro", uri=True) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0])

    before = kept()
    world.proxy.call(hold)
    try:
        started = time.monotonic()
        answer = client.request("search3", params)
        elapsed = time.monotonic() - started
    finally:
        world.proxy.call(release)
    try:
        assert answer.status_code == 200, answer.text
        assert elapsed < world.app.wait  # no index worker was needed
        assert kept() == before
        found = {
            album["id"]: album["songCount"]
            for album in answer.json()["subsonic-response"]["searchResult3"]["album"]
        }
        listed = client.ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
        assert found and found == {
            album["id"]: album["songCount"]
            for album in listed["albumList2"]["album"]
            if album["name"].startswith("Collection Album")
        }
        assert all(count < 5 for count in found.values())  # the selection applied
    finally:
        client.close()


def test_a_prepared_search_waits_once_for_all_its_kinds(world: World) -> None:
    """A search whose albums and songs both need preparing (as results of 500 or more
    do) prepares them together and waits as long as one list would, not once per kind."""
    # The account holds the originals alone (``removed``), and which songs those are follows
    # Navidrome's random ids: an album can have none and is then rightly not found. The
    # search names an album the library holds and expects what Navidrome finds of it.
    originals = world.app.originals
    held = {song["albumId"] for song in world.app.songs if song["id"] in originals}
    name = next(song["album"] for song in world.app.songs if song["albumId"] in held)
    client = world.nd.client("second", "fixture-password").with_base(world.proxy.base_url)
    native = world.nd.client("second", "fixture-password")
    params = {"query": name, "artistCount": 0, "albumCount": 20, "songCount": 20}
    delay = world.app.wait * 0.6  # each build: one alone fits the wait, two in a row do not
    try:
        with prepared_searches(world.app.index, delay) as built:
            started = time.monotonic()
            answer = client.request("search3", params)
            elapsed = time.monotonic() - started
        assert answer.status_code == 200, answer.text
        assert len(built) == len(set(built)) == 2
        assert elapsed < world.app.wait, elapsed
        found = answer.json()["subsonic-response"]["searchResult3"]
        source = native.ok("search3", params)["searchResult3"]
        albums = [album["name"] for album in found.get("album", [])]
        assert name in albums
        assert albums == [album["name"] for album in source["album"] if album["id"] in held]
        assert [song["id"] for song in found.get("song", [])] == [
            song["id"] for song in source["song"] if song["id"] in originals
        ]
    finally:
        client.close()
        native.close()


def test_empty_rest_bursts_need_no_query_generation(
    navidrome_factory: NavidromeFactory, tmp_path: Any
) -> None:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Fixture", "Synthetic", 3))
    nd.scan(full=True)
    songs = nd.native("GET", "song", params={"_sort": "id", "_end": 500}).json()
    nd.create_user("empty-listener", "fixture-password")
    app = CollectionApp(nd, tmp_path / "empty-search", songs, wait=0)
    with running(app) as proxy:

        def search(n: int) -> httpx.Response:
            client = nd.client("empty-listener", "fixture-password").with_base(proxy.base_url)
            try:
                return client.request(
                    "search3",
                    {
                        "query": f"absent term {n}",
                        "artistCount": 0,
                        "albumCount": 0,
                        "songCount": 50,
                    },
                )
            finally:
                client.close()

        with ThreadPoolExecutor(12) as pool:
            answers = list(pool.map(search, range(12)))
        assert all(
            a.status_code == 200 and a.json()["subsonic-response"]["searchResult3"] == {}
            for a in answers
        )
        assert app.index.network_pages == 0


def test_native_artist_roles_maincredit_stats_and_count_sort(
    navidrome_factory: NavidromeFactory, tmp_path: Any
) -> None:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Role Switch", "Hidden main credit", 3))
    write_album(
        nd.music,
        simple_album(
            "Other Credit", "Selected composition", 2, extra={"composer": ["Role Switch"]}
        ),
    )
    nd.scan(full=True)
    songs = nd.native("GET", "song", params={"_sort": "id", "_end": 500}).json()
    owner = nd.create_user("role-listener", "fixture-password")
    app = CollectionApp(nd, tmp_path / "role-cache", songs, wait=2)
    with running(app) as proxy:
        world = World(nd, app, proxy, {"role-listener": owner})
        selected = [s for s in songs if s["album"] == "Selected composition"]

        async def select() -> None:
            await app.store.execute(
                (
                    "INSERT INTO collection_profiles(id,owner_id,name,base_original,created) "
                    "VALUES ('roles',?,'Roles',0,1)"
                ),
                [owner],
            )
            await app.store.execute(
                "INSERT INTO collection_preferences VALUES (?,'roles',1)", [owner]
            )
            for song in selected:
                await app.store.execute(
                    (
                        "INSERT INTO collection_membership "
                        "VALUES (?,'roles','1',?,1,1,'role-subset')"
                    ),
                    [owner, "native:" + song["id"]],
                )

        proxy.call(select)
        main = world.native(
            "role-listener", "artist", {"role": "albumartist", "_sort": "name", "_end": 20}
        )
        assert main.status_code == 200, main.text
        assert [a["name"] for a in main.json()] == ["Other Credit"]
        composer = world.native(
            "role-listener", "artist", {"role": "composer", "_sort": "name", "_end": 20}
        )
        assert [a["name"] for a in composer.json()] == ["Role Switch"]
        by_count = world.native(
            "role-listener",
            "artist",
            {"_sort": "maincredit_song_count", "_order": "DESC", "_start": 0, "_end": 1},
        )
        assert by_count.headers["x-total-count"] == "2"
        assert by_count.json()[0]["name"] == "Other Credit"
        stats = by_count.json()[0]
        assert stats["songCount"] == stats["stats"]["maincredit"]["songCount"] == 2
        assert (
            stats["size"]
            == stats["stats"]["maincredit"]["size"]
            == sum(s["size"] for s in selected) * 2
        )
        detail = world.native("role-listener", "artist/" + stats["id"]).json()
        assert detail["stats"] == stats["stats"] and detail["size"] == stats["size"]


@pytest.mark.parametrize("participations", [False, True])
def test_rest_artist_indexes_and_role_counts_match_navidrome_configuration(
    navidrome_factory: NavidromeFactory, tmp_path: Any, participations: bool
) -> None:
    nd = navidrome_factory({"ND_SUBSONIC_ARTISTPARTICIPATIONS": str(participations).lower()})
    write_album(nd.music, simple_album("Role Switch", "Hidden main credit", 3))
    write_album(
        nd.music,
        simple_album(
            "Composer Credit", "Selected composition", 2, extra={"composer": ["Role Switch"]}
        ),
    )
    guest = simple_album("Album Credit", "Selected performance", 2)
    write_album(
        nd.music,
        replace(guest, tracks=tuple(replace(t, artists=("Role Switch",)) for t in guest.tracks)),
    )
    nd.scan(full=True)
    songs = nd.native("GET", "song", params={"_sort": "id", "_end": 500}).json()
    owner = nd.create_user("role-listener", "fixture-password")
    app = CollectionApp(
        nd, tmp_path / "rest-roles", songs, wait=2, artist_participations=participations
    )
    with running(app) as proxy:

        async def select() -> None:
            await app.store.execute(
                (
                    "INSERT INTO collection_profiles(id,owner_id,name,base_original,created) "
                    "VALUES ('roles',?,'Roles',0,1)"
                ),
                [owner],
            )
            await app.store.execute(
                "INSERT INTO collection_preferences VALUES (?,'roles',1)", [owner]
            )
            for song in songs:
                if song["album"] != "Hidden main credit":
                    await app.store.execute(
                        (
                            "INSERT INTO collection_membership "
                            "VALUES (?,'roles','1',?,1,1,'role-subset')"
                        ),
                        [owner, "native:" + song["id"]],
                    )

        proxy.call(select)
        direct = nd.client("role-listener", "fixture-password", headers={"host": "music.test"})
        client = direct.with_base(proxy.base_url)
        for method, key in (("getArtists", "artists"), ("getIndexes", "indexes")):
            answer = client.ok(method)[key]
            assert {a["name"] for g in answer.get("index", []) for a in g["artist"]} == {
                "Album Credit",
                "Composer Credit",
            }
            xml = client.request(method, fmt="xml")
            assert {
                a.attrib["name"]
                for a in ElementTree.fromstring(xml.content).findall(".//{*}artist")
            } == {"Album Credit", "Composer Credit"}
        params = {"query": "Role Switch", "albumCount": 0, "songCount": 0}
        original = direct.ok("search3", params)["searchResult3"]["artist"][0]
        partial = client.ok("search3", params)["searchResult3"]["artist"][0]
        assert original["albumCount"] == (2 if participations else 1)
        assert partial["albumCount"] == (1 if participations else 0)
        assert sorted(partial["roles"]) == sorted(original["roles"])
        assert {k: v for k, v in partial.items() if k not in ("albumCount", "roles")} == {
            k: v for k, v in original.items() if k not in ("albumCount", "roles")
        }

        async def select_remaining() -> None:
            for song in songs:
                if song["album"] == "Hidden main credit":
                    await app.store.execute(
                        (
                            "INSERT INTO collection_membership "
                            "VALUES (?,'roles','1',?,1,1,'role-full')"
                        ),
                        [owner, "native:" + song["id"]],
                    )

        proxy.call(select_remaining)
        from tests.acceptance.test_A_fidelity import canonical_body

        assert canonical_body(client.request("search3", params)) == canonical_body(
            direct.request("search3", params)
        )
