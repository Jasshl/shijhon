"""Lazy credential/grant boundaries with a live catalog and disposable Navidrome."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import simple_album, write_album
from tests.harness.replay import Replay, fixture

PASSWORD = "fixture-password"
USERNAME = "gate-listener"
TABLES = (
    "releases",
    "track_links",
    "placeholders",
    "album_matches",
    "removed_releases",
    "removed_songs",
    "collection_track_aliases",
    "collection_scan_aliases",
)


@dataclass
class World:
    delivery: DeliveryWorld
    replay: Replay
    foreign: int
    song: str
    album: str


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[World]:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Gate Artist", "Owned Gate Album", 2))
    foreign_path = nd.root / "foreign-music"
    foreign_path.mkdir()
    foreign = int(
        nd.native(
            "POST", "library", json={"name": "Other fixture library", "path": str(foreign_path)}
        ).json()["id"]
    )
    nd.scan(full=True)
    song = nd.native("GET", "song", params={"_start": 0, "_end": 1}).json()[0]
    replay = Replay()
    with delivery_world(
        nd,
        tmp_path_factory.mktemp("lazy-music-gate"),
        catalog=replay.catalog(),
        fill={"enabled": True, "library_pass": "off"},
    ) as delivery:
        yield World(delivery, replay, foreign, song["id"], song["albumId"])


def state(world: World) -> tuple[Any, ...]:
    delivery = world.delivery

    async def rows() -> tuple[Any, ...]:
        snapshot = []
        for table in TABLES:
            rows = await delivery.services.store.fetchall("SELECT * FROM " + table)  # noqa: S608
            snapshot.append(tuple(tuple(row) for row in rows))
        return tuple(snapshot)

    return (
        delivery.server.call(rows),
        tuple((str(p), p.read_bytes()) for p in delivery.placeholder_files()),
        delivery.services.scans.scans_started,
        delivery.services.commits.materializations,
        delivery.services.fills.fills,
        delivery.services.old_ids.added_back,
        world.replay.api_requests,
        world.replay.artwork_requests,
    )


@pytest.mark.parametrize("identity", ["never-granted", "revoked", "recreated"])
def test_denied_current_grants_leave_catalog_fills_backing_and_aliases_untouched(
    world: World, identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery, nd = world.delivery, world.delivery.nd
    user = USERNAME + "-" + identity
    owner = nd.create_user(user, PASSWORD)
    direct = nd.client(user, PASSWORD, salt="stable-gate-salt")
    client = direct.with_base(delivery.server.base_url)
    album_id = str(fixture("album-twins-clean")["path"]).split("/")[1]
    track_id = fixture("album-twins-clean")["body"]["tracks"][0]["ref"]["id"]
    catalog_album, catalog_song = f"sh.al.demo.{album_id}", f"sh.tr.demo.{track_id}"
    try:
        if identity != "never-granted":
            # Warm the credential proof with a real catalog view, before revoking access.
            assert client.ok("getSong", {"id": catalog_song})["song"]["id"] == catalog_song
        if identity == "recreated":
            nd.native("DELETE", "user/" + owner)
            owner = nd.create_user(user, PASSWORD)
        nd.native("PUT", "user/" + owner + "/library", json={"libraryIds": [world.foreign]})
        playlist = direct.ok("createPlaylist", {"name": "Native empty playlist"})["playlist"]["id"]
        # Grant resolution may use native user/library endpoints; music metadata may not
        # be fetched with service credentials to decide whether this caller is allowed.
        metadata = []
        for name in ("song", "album", "artist", "search", "search_page"):
            if hasattr(delivery.services.navidrome, name):
                check = AsyncMock(side_effect=AssertionError("service metadata before grant"))
                metadata.append(check)
                monkeypatch.setattr(delivery.services.navidrome, name, check)
        before = state(world)
        pings = delivery.app.checker.pings
        requests = [
            ("getAlbum", {"id": catalog_album}),
            ("getAlbum", {"id": world.album}),
            ("getSong", {"id": catalog_song}),
            ("getCoverArt", {"id": "al-" + catalog_album}),
            ("stream", {"id": catalog_song}),
            ("download", {"id": catalog_song}),
            ("star", {"albumId": catalog_album}),
            ("setRating", {"id": catalog_song, "rating": 4}),
            ("updatePlaylist", {"playlistId": playlist, "songIdToAdd": catalog_song}),
            ("savePlayQueue", {"id": catalog_song, "current": catalog_song}),
            ("getLyricsBySongId", {"id": catalog_song}),
        ]
        for checked, (method, params) in enumerate(requests, 1):
            own, through = direct.request(method, params), client.request(method, params)
            assert (through.status_code, through.content) == (own.status_code, own.content), method
            assert state(world) == before, method
            # Current music authority needs one fresh credential proof per request,
            # including when the same credentials were accepted before grant removal.
            assert delivery.app.checker.pings == pings + checked, method
        for check in metadata:
            check.assert_not_awaited()
    finally:
        nd.native("PUT", "user/" + owner + "/library", json={"libraryIds": [1, world.foreign]})
        direct.close()
        client.close()


def test_owned_streams_and_playlists_do_no_credential_or_grant_checks(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = world.delivery
    gate = delivery.app.proxy.music_gate
    monkeypatch.setattr(
        gate.access,
        "verify",
        AsyncMock(side_effect=AssertionError("native passthrough grant lookup")),
    )
    direct = delivery.nd.client(salt="unchanged-native-salt")
    client = direct.with_base(delivery.server.base_url)
    pings = delivery.app.checker.pings
    try:
        for method, params in (("stream", {"id": world.song}), ("getPlaylists", {})):
            own, through = direct.request(method, params), client.request(method, params)
            assert (through.status_code, through.content) == (own.status_code, own.content)
            assert delivery.app.checker.pings == pings
        gate.access.verify.assert_not_awaited()
    finally:
        direct.close()
        client.close()


def test_owned_streams_stay_unchecked_beside_another_songs_play_from_the_addons(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While no one has a minimum, a play of another song that began at the add-ons (when
    someone had one) keeps its own later ranges there, and changes nothing for an owned
    song's stream: no credential proof, no grant lookup."""
    delivery = world.delivery
    interceptor = delivery.services.interceptor
    play = ((USERNAME, "another-app"), "another-song", None)

    async def begun() -> None:
        interceptor._set_play(play, (-1, True))

    async def ended() -> None:
        interceptor._drop_play(play)

    gate = delivery.app.proxy.music_gate
    monkeypatch.setattr(
        gate.access,
        "verify",
        AsyncMock(side_effect=AssertionError("native passthrough grant lookup")),
    )
    direct = delivery.nd.client(salt="unchanged-native-salt")
    client = direct.with_base(delivery.server.base_url)
    delivery.server.call(begun)
    try:
        pings = delivery.app.checker.pings
        for headers in ({}, {"range": "bytes=10-99"}):
            own = direct.request("stream", {"id": world.song}, headers=headers)
            through = client.request("stream", {"id": world.song}, headers=headers)
            assert (through.status_code, through.content) == (own.status_code, own.content)
            assert delivery.app.checker.pings == pings
        gate.access.verify.assert_not_awaited()
    finally:
        delivery.server.call(ended)
        direct.close()
        client.close()


def test_an_owned_stream_checks_credentials_once_while_someone_has_a_minimum(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With someone's minimum quality set, an owned song's stream depends on its
    listener's minimum: its credentials are checked once - one credential proof, one grant
    lookup - and a file that meets the minimum plays as Navidrome serves it."""
    delivery, nd = world.delivery, world.delivery.nd
    user = USERNAME + "-minimum"
    owner = nd.create_user(user, PASSWORD)
    minimums = delivery.services.quality
    assert minimums is not None
    delivery.server.call(lambda: minimums.set(owner, "128"))
    gate = delivery.app.proxy.music_gate
    verify = AsyncMock(side_effect=gate.access.verify)
    monkeypatch.setattr(gate.access, "verify", verify)
    direct = nd.client(user, PASSWORD, salt="minimum-gate-salt")
    client = direct.with_base(delivery.server.base_url)
    try:
        pings = delivery.app.checker.pings
        own, through = (
            direct.request("stream", {"id": world.song}),
            client.request("stream", {"id": world.song}),
        )
        assert own.status_code == 200 and own.content[:4] == b"fLaC"
        assert (through.status_code, through.content) == (own.status_code, own.content)
        assert delivery.app.checker.pings == pings + 1
        assert verify.await_count == 1
    finally:
        delivery.server.call(lambda: minimums.set(owner, None))
        direct.close()
        client.close()


def test_a_foreign_folder_passes_through_only_where_navidrome_reads_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list or a search of another library is Navidrome's own answer: no grant lookup,
    nothing written. Where Navidrome ignores the folder - a catalog album's page, a stream
    - so does Shijhon: the same answer as without it."""
    delivery = world.delivery
    gate = delivery.app.proxy.music_gate
    direct = delivery.nd.client(salt="foreign-catalog-salt")
    client = direct.with_base(delivery.server.base_url)
    album = str(fixture("album-twins-clean")["path"]).split("/")[1]
    before = state(world)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(
                gate.access,
                "verify",
                AsyncMock(side_effect=AssertionError("foreign library grant lookup")),
            )
            for method, asked in (
                ("getAlbumList2", {"type": "newest"}),
                ("search3", {"query": "Gate"}),
            ):
                params = {**asked, "musicFolderId": world.foreign}
                own, through = direct.request(method, params), client.request(method, params)
                assert (through.status_code, through.content) == (own.status_code, own.content)
            gate.access.verify.assert_not_awaited()
        assert state(world) == before
        params = {"id": "sh.al.demo." + album}
        plain = client.request("getAlbum", params)
        viewed = state(world)
        foreign = client.request("getAlbum", {**params, "musicFolderId": world.foreign})
        assert plain.status_code == 200 and b'"status":"ok"' in plain.content
        assert foreign.content == plain.content and state(world) == viewed
    finally:
        direct.close()
        client.close()
