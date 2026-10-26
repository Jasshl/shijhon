"""Suite C - a folder parameter where Navidrome ignores it.

Navidrome reads ``musicFolderId`` only on its album lists, shuffle, genre songs,
favorites, artist indexes and searches - as a number, leaving out a value that is none.
On a stream, a download or a page it ignores it. So a placeholder's stream with any
folder parameter is a placeholder's stream: its audio from the add-ons, never its silence;
a list for another library stays Navidrome's own answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import pytest

from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import simple_album, write_album
from tests.harness.subsonic import AuthStyle


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    write_album(nd.music, simple_album("Folder Band", "Folder Record", 1))
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("folders")) as w:
        w.add_source(w.addon("Folder source"))
        yield w


def state(world: DeliveryWorld, song: str) -> str:
    async def read() -> str:
        row = await world.services.store.fetchone(
            "SELECT state FROM placeholders WHERE song_id = ?", [song]
        )
        assert row is not None
        return str(row["state"])

    return world.server.call(read)


@pytest.mark.parametrize("auth", ["token", "password"])
@pytest.mark.parametrize("folder", ["999", "01", "abc", "1"])
def test_a_placeholder_plays_from_the_add_ons_whatever_the_folder(
    world: DeliveryWorld, folder: str, auth: AuthStyle
) -> None:
    addon = world.addons[0]
    song, _, audio = world.placeholder_track(f"folder-{folder}-{auth}", [addon])
    client = world.client(auth=auth)
    asked = len(addon.requests())
    played = client.request("stream", {"id": song, "musicFolderId": folder})
    assert played.status_code == 200 and played.content == audio.read_bytes()
    assert len(addon.requests()) > asked  # its audio, from the add-ons
    assert state(world, song) == "placeholder"  # (a plain stream writes nothing)
    downloaded = client.request("download", {"id": song, "musicFolderId": folder})
    assert downloaded.status_code == 200 and len(downloaded.content) > 10_000
    assert state(world, song) == "delivered"  # downloaded first, as without a folder


def test_a_list_for_another_library_stays_navidrome_s(world: DeliveryWorld) -> None:
    direct = world.nd.client(salt="folder-list-salt")
    client = direct.with_base(world.server.base_url)
    try:
        for folder in ("999", "-1"):
            params = {"type": "newest", "musicFolderId": folder}
            own, through = (
                direct.request("getAlbumList2", params),
                client.request("getAlbumList2", params),
            )
            assert (through.status_code, through.content) == (own.status_code, own.content)
            assert own.json()["subsonic-response"]["status"] == "failed"  # no such library
        # This library, spelled otherwise: the same answer as spelled plainly.
        plain = client.request("getAlbumList2", {"type": "newest", "musicFolderId": "1"})
        padded = client.request("getAlbumList2", {"type": "newest", "musicFolderId": "001"})
        assert (padded.status_code, padded.content) == (plain.status_code, plain.content)
    finally:
        direct.close()
        client.close()


@pytest.mark.parametrize("fmt", ["json", "xml"])
@pytest.mark.parametrize("folders", [["01", "999"], ["+1", "999"], ["999", "1"]])
def test_a_folder_the_caller_cannot_read_gets_navidrome_s_own_answer(
    world: DeliveryWorld, folders: list[str], fmt: Literal["json", "xml"]
) -> None:
    """This library with one the caller cannot read: Navidrome's own answer, byte for
    byte (its error - for the artist indexes too, which then read no library)."""
    host = {"host": "music.test"}  # (image addresses are made from the request's host)
    direct = world.nd.client(salt="mixed-folder-salt", fmt=fmt, headers=host)
    client = direct.with_base(world.server.base_url)
    asked_for = (
        ("getAlbumList2", {"type": "newest"}),
        ("search3", {"query": "Folder"}),
        ("getArtists", {}),
        ("getIndexes", {}),
    )
    try:
        for method, params in asked_for:
            asked = {**params, "musicFolderId": folders}
            own, through = direct.request(method, asked), client.request(method, asked)
            assert (through.status_code, through.content) == (own.status_code, own.content), method
            assert b'"failed"' in own.content or b'status="failed"' in own.content, method
    finally:
        direct.close()
        client.close()
