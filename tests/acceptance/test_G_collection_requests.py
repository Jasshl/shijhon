"""Real native publication/reuse with account attribution independent of profiles."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.catalog.model import release_from_data
from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.history import History
from shijhon.collection.reader import CallerLibrary
from shijhon.playlists.model import Connection, Definition, Track
from shijhon.proxy.auth import subsonic_answer
from shijhon.views.collection_requests import CollectionRequests, CollectionRestorer
from shijhon.views.ids import CatalogId
from shijhon.views.native_collection_requests import NativeCollectionRequests
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeTrack
from tests.harness.replay import Replay, fixture
from tests.harness.subsonic import SubsonicClient


def _album(name: str) -> str:
    return str(fixture(name)["path"]).split("/")[1]


def _songs(name: str) -> list[str]:
    return [f"sh.tr.demo.{t['ref']['id']}" for t in fixture(name)["body"]["tracks"]]


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    with delivery_world(
        navidrome_factory(), tmp_path_factory.mktemp("account-requests"), catalog=Replay().catalog()
    ) as world:
        reader: CallerLibrary | None = None

        async def wire() -> None:
            nonlocal reader
            commits = world.services.commits
            assert commits is not None
            requests = CollectionRequests(
                commits, world.app.upstream, History(world.services.store)
            )
            commits.requested = requests
            assert world.services.playlists is not None
            world.services.playlists.published = requests.published
            reader = CallerLibrary(world.services.playlists)
            NativeCollectionRequests(requests, reader).attach(world.app.proxy)

        world.server.call(wire)
        yield world
        assert reader is not None
        world.server.call(reader.aclose)


def _history(world: DeliveryWorld, owner: str) -> set[str]:
    async def read() -> set[str]:
        rows = await world.services.store.fetchall(
            "SELECT track_key FROM collection_history WHERE owner_id=?", [owner]
        )
        return {str(row["track_key"]) for row in rows}

    return world.server.call(read)


def _owner(world: DeliveryWorld, username: str) -> tuple[str, SubsonicClient]:
    owner = world.nd.create_user(username, "synthetic-password")
    return owner, SubsonicClient(world.server.base_url, username, "synthetic-password")


def _key(song: str) -> str:
    return "catalog:demo:" + song.removeprefix("sh.tr.demo.")


@pytest.mark.parametrize("fmt", ["json", "xml", "jsonp"])
def test_second_account_reuses_materialization_and_claims_only_played_track(
    world: DeliveryWorld, fmt: str
) -> None:
    album = {
        "json": "album-twins-clean",
        "xml": "album-twins-explicit",
        "jsonp": "album-feat-standard",
    }[fmt]
    first, a = _owner(world, "first-" + fmt)
    second, b = _owner(world, "second-" + fmt)
    songs = _songs(album)
    commits = world.services.commits
    assert commits is not None
    before = commits.materializations
    a.ok("scrobble", {"id": songs[0], "submission": False})
    assert commits.materializations == before + 1
    cid = CatalogId.parse(songs[1])
    assert cid is not None
    native = world.server.call(lambda: commits.materialized.song(cid.ref))
    assert native
    params = [*b.auth_params(), ("f", fmt), ("id", native), ("submission", "false")]
    if fmt == "jsonp":
        params.append(("callback", "window.accepted"))
    response = b.http.get(b.base_url + "/rest/scrobble", params=params)
    assert subsonic_answer(response, response.content)["status"] == "ok"
    assert commits.materializations == before + 1
    assert _history(world, first) == {_key(songs[0])}
    assert _history(world, second) == {_key(songs[1])}
    a.close()
    b.close()


def test_browse_prefetch_queue_rejection_and_head_do_not_claim(world: DeliveryWorld) -> None:
    owner, client = _owner(world, "no-intent")
    name = "album-duo-deluxe-clean"
    songs = _songs(name)
    client.ok("getAlbum", {"id": "sh.al.demo." + _album(name)})
    client.ok("getSong", {"id": songs[0]})
    before = world.placeholder_rows()
    client.request("star", {"id": songs[0]}, http_method="HEAD")
    invalid = client.http.get(
        client.base_url + "/rest/star",
        params=[
            *client.auth_params(),
            ("f", "jsonp"),
            ("callback", "invalid callback"),
            ("id", songs[0]),
        ],
    )
    assert "invalid callback" in invalid.text.lower()
    assert world.placeholder_rows() == before
    addon = world.addon("request-prefetch")
    track = fixture(name)["body"]["tracks"][0]
    addon.add(FakeTrack(isrc=track["isrc"], audio=world.audio("request-prefetch")))
    world.add_source(addon)
    assert client.request("stream", {"id": songs[0]}).is_success
    assert world.placeholder_rows() == before
    client.ok("savePlayQueue", [("id", s) for s in songs] + [("current", songs[0])])
    rejected = client.request("updatePlaylist", {"songIdToAdd": songs[1], "playlistId": "missing"})
    assert subsonic_answer(rejected, rejected.content)["status"] == "failed"
    assert _history(world, owner) == set()
    bad = SubsonicClient(client.base_url, "no-intent", "wrong-password")
    refused = bad.request("scrobble", {"id": songs[0]})
    assert refused.json()["subsonic-response"]["status"] == "failed"
    empty = world.tmp / "empty-library"
    empty.mkdir()
    other = world.nd.native("POST", "library", json={"name": "Empty", "path": str(empty)}).json()[
        "id"
    ]
    world.nd.native("PUT", f"user/{owner}/library", json={"libraryIds": [int(other)]})
    client.request("scrobble", {"id": songs[0], "submission": False})
    assert _history(world, owner) == set()
    bad.close()
    client.close()


def test_explicit_album_action_records_whole_album_without_membership(world: DeliveryWorld) -> None:
    owner, client = _owner(world, "album-intent")
    name = "album-ep-without-suffix"
    client.ok("star", {"albumId": "sh.al.demo." + _album(name)})
    assert _history(world, owner) == {_key(song) for song in _songs(name)}

    async def memberships() -> Any:
        return await world.services.store.fetchone(
            "SELECT 1 FROM collection_membership WHERE owner_id=?", [owner]
        )

    assert world.server.call(memberships) is None
    client.close()


def test_playlist_finish_records_only_selected_tracks_in_publication_transaction(
    world: DeliveryWorld,
) -> None:
    owner, client = _owner(world, "playlist-intent")
    release = release_from_data(fixture("album-twins-clean")["body"])
    login = world.nd.login("playlist-intent", "synthetic-password")

    async def publish() -> None:
        commits, service = world.services.commits, world.services.playlists
        assert commits is not None and service is not None
        connection = Connection(
            user_id=owner,
            username="playlist-intent",
            token=login["subsonicToken"],
            salt=login["subsonicSalt"],
        )
        song = release.tracks[0]
        native = await commits.native(CatalogId("tr", song.ref))
        definition = Definition(name="Ordered synthetic playlist", count=2, max_per_artist=2)
        selected = Track(
            id=str(CatalogId("tr", song.ref)),
            artist=song.artist,
            title=song.title,
            album=release.title,
            duration=song.seconds,
            isrc=song.isrc or "",
            explicit=song.explicit,
            clean=song.clean,
            release=str(release.ref),
        )
        current = (
            await service.client.call(
                connection,
                "createPlaylist",
                [
                    ("name", definition.name),
                    ("songId", native),
                    ("songId", native),
                ],
            )
        )["playlist"]
        async with service.worker:
            await service.repo.connect(connection)
            mix_id = await service.repo.create(owner, definition)
            run_id = await service.repo.enqueue(mix_id)
            mix = await service.repo.mix(mix_id, owner)
            data = {
                "definition": definition.model_dump(mode="json"),
                "selected": [selected.model_dump(), selected.model_dump()],
                "native_ids": [native, native],
            }
            callback = service.published
            assert callback is not None

            async def refuse(*args: Any) -> None:
                await callback(*args)
                raise RuntimeError("synthetic publication rollback")

            service.published = refuse
            try:
                with pytest.raises(RuntimeError, match="rollback"):
                    await service.finish(connection, mix, run_id, data, current)
                assert not await service.repo.store.fetchone(
                    "SELECT 1 FROM collection_history WHERE owner_id=?", [owner]
                )
            finally:
                service.published = callback
            await service.finish(connection, mix, run_id, data, current)
            assert (await service.repo.run(run_id))["state"] == "published"

    world.server.call(publish)
    assert _history(world, owner) == {"catalog:" + str(release.tracks[0].ref)}
    client.close()


def test_cleaned_release_restores_from_personal_snapshot_and_keeps_ids(
    world: DeliveryWorld,
) -> None:
    owner, client = _owner(world, "restore-intent")
    name = "album-single-radio-edit"
    song = _songs(name)[0]
    client.ok("star", {"id": song})
    assert _history(world, owner) == {_key(song)}
    login = world.nd.login("restore-intent", "synthetic-password")

    async def restore() -> None:
        commits, playlists = world.services.commits, world.services.playlists
        assert commits is not None and playlists is not None
        reader = CallerLibrary(playlists)
        principal = await reader.access.verify_native(login["token"])
        reader.remember(principal, login["token"])
        actor = Actor(owner, frozenset(str(i) for i in principal.library_ids))
        library = str(reader.library_id)
        history = History(world.services.store)
        cid = CatalogId.parse(song)
        assert cid is not None
        original = await commits.materialized.song(cid.ref)
        assert original
        snapshots = await history.snapshots(actor, library, [cid.ref])
        snapshot = snapshots[str(cid.ref)]
        assert snapshot.album
        assert (await commits.engine.remove_release(str(snapshot.album))).removed > 0
        assert await commits.materialized.song(cid.ref) is None
        restorer = CollectionRestorer(commits, reader, history)

        async def authorize() -> None:
            return None

        # The latest catalog may change; retained publication data is authoritative.
        latest = commits.catalog
        commits.catalog = None
        try:
            restored = await restorer(actor, library, [cid.ref], authorize=authorize)
            assert restored[str(cid.ref)]["id"] == original
            assert await commits.materialized.song(cid.ref) == original
            await history.store.execute(
                "UPDATE collection_history SET catalog_data=json_set(catalog_data,'$.title',?)"
                " WHERE owner_id=?",
                ["Different version", owner],
            )
            with pytest.raises(CollectionError, match="changed"):
                await restorer(actor, library, [cid.ref], authorize=authorize)
            with pytest.raises(CollectionError, match="250"):
                await restorer(actor, library, [cid.ref] * 251, authorize=authorize)
        finally:
            commits.catalog = latest
            await reader.aclose()

    world.server.call(restore)
    client.close()


def test_native_playlist_song_publication_attributes_the_exact_caller_and_order(
    world: DeliveryWorld,
) -> None:
    owner, client = _owner(world, "native-playlist-intent")
    other, other_client = _owner(world, "native-playlist-other")
    commits = world.services.commits
    assert commits is not None
    songs = _songs("album-twins-clean")[:3]
    ids = []
    for song in songs:
        cid = CatalogId.parse(song)
        assert cid is not None
        ids.append(world.server.call(lambda cid=cid: commits.native(cid)))
    login = world.nd.login("native-playlist-intent", "synthetic-password")
    headers = {"x-nd-authorization": "Bearer " + login["token"]}
    playlist = client.http.post(
        client.base_url + "/api/playlist", headers=headers, json={"name": "Native ordered fixture"}
    ).json()
    path = client.base_url + "/api/playlist/" + playlist["id"] + "/tracks"
    published = client.http.post(path, headers=headers, json={"ids": [ids[1], ids[0], ids[0]]})
    assert published.status_code == 200 and published.json()["added"] == 3
    assert [row["mediaFileId"] for row in client.http.get(path, headers=headers).json()] == [
        ids[1],
        ids[0],
        ids[0],
    ]
    assert _history(world, owner) == {_key(song) for song in songs[:2]}
    assert _history(world, other) == set()
    before = _history(world, owner)
    failed = client.http.post(
        client.base_url + "/api/playlist/missing/tracks", headers=headers, json={"ids": [ids[2]]}
    )
    assert failed.status_code != 200
    assert _history(world, owner) == before
    # Verify a second append at its existing native offset.
    appended = client.http.post(path, headers=headers, json={"ids": [ids[2], ids[0]]})
    assert appended.status_code == 200 and appended.json()["added"] == 2
    assert _history(world, owner) == {_key(song) for song in songs}
    other_login = world.nd.login("native-playlist-other", "synthetic-password")
    other_headers = {"x-nd-authorization": "Bearer " + other_login["token"]}
    other_playlist = other_client.http.post(
        other_client.base_url + "/api/playlist",
        headers=other_headers,
        json={"name": "Native unknown-shape fixture"},
    ).json()
    other_path = other_client.base_url + "/api/playlist/" + other_playlist["id"] + "/tracks"
    # Unknown bodies retain native behavior without attribution guesses.
    unknown = other_client.http.post(
        other_path, headers=other_headers, json={"ids": [ids[0]], "unknown": True}
    )
    assert unknown.status_code == 200 and unknown.json()["added"] == 1
    assert _history(world, other) == set()
    refused_headers = {"x-nd-authorization": "Bearer invalid-fixture-bearer"}
    refused = other_client.http.post(other_path, headers=refused_headers, json={"ids": [ids[1]]})
    direct = other_client.http.post(
        other_path.replace(other_client.base_url, world.nd.base_url),
        headers=refused_headers,
        json={"ids": [ids[1]]},
    )
    assert (refused.status_code, refused.content) == (direct.status_code, direct.content)
    assert len(other_client.http.get(other_path, headers=other_headers).json()) == 1
    assert _history(world, other) == set()
    client.close()
    other_client.close()
