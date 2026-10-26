"""Late local recordings complete the same, possibly edited, native playlist after review."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from shijhon.playlists.model import PlaylistError
from tests.acceptance.test_R_playlist_imports import submit
from tests.acceptance.test_R_playlist_imports import world as world
from tests.acceptance.test_R_playlists import connected_admin, wait_state
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld
from tests.harness.library import Album, Track, write_album


def titles(playlist: dict[str, Any]) -> list[str]:
    return [s["title"] for s in playlist.get("entry", [])]


def test_recheck_keeps_native_edits_and_adds_found_songs_next_to_their_neighbours(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    write_album(
        w.nd.music,
        Album(
            "Merge Orchard",
            "Kept Room",
            (Track("Kept One", 1), Track("Kept Two", 2), Track("Kept Three", 3)),
        ),
    )
    write_album(w.nd.music, Album("Owner Pick", "Own Room", (Track("Own Song", 1),)))
    w.nd.scan()
    # Import A B C D E; C and E are not in the library yet.
    ident, original_run = submit(
        world,
        lines=(
            "Merge Orchard — Kept One\nMerge Orchard — Kept Two\nMerge Late — Late One\n"
            "Merge Orchard — Kept Three\nMerge Late — Late Two"
        ),
    )
    assert (
        browser.submit(
            f"playlists/import/{ident}", {"action": "publish", "excluded": "2"}
        ).status_code
        == 303
    )
    published = wait_state(w, original_run, ("published", "failed", "uncertain"))
    assert published["state"] == "published", published

    async def playlist() -> dict[str, Any]:
        connection = await connected_admin(service)
        mix = await service.repo.mix(ident, connection.user_id)
        return await service.client.playlist(connection, mix["playlist_id"])

    before = w.server.call(playlist)
    assert titles(before) == ["Kept One", "Kept Two", "Kept Three"]

    async def edit() -> None:
        """The owner removes B, adds a song and changes the details in Navidrome."""
        connection = await connected_admin(service)
        found = await service.client.call(
            connection, "search3", [("query", "Own Song"), ("albumCount", "0")]
        )
        own = next(s["id"] for s in found["searchResult3"]["song"] if s["title"] == "Own Song")
        await service.client.call(
            connection, "updatePlaylist", [("playlistId", before["id"]), ("songIndexToRemove", "1")]
        )
        await service.client.call(
            connection,
            "updatePlaylist",
            [
                ("playlistId", before["id"]),
                ("songIdToAdd", own),
                ("comment", "Owner note"),
                ("public", "true"),
            ],
        )

    w.server.call(edit)
    edited = w.server.call(playlist)
    assert titles(edited) == ["Kept One", "Kept Three", "Own Song"]
    assert edited["comment"] == "Owner note" and edited["public"] is True
    page = browser.get(f"playlists/import/{ident}")
    assert "Recheck 2 missing entries" in page.text
    assert "2 not found" in browser.get("playlists/import").text
    missing = browser.get(f"playlists/import/{ident}?show=missing")
    assert 'data-label="Entry">3' in missing.text and 'data-label="Entry">5' in missing.text
    assert 'data-label="Entry">1<' not in missing.text
    write_album(
        w.nd.music, Album("Merge Late", "Late Room", (Track("Late One", 1), Track("Late Two", 2)))
    )
    w.nd.scan()
    response = browser.submit(f"playlists/import/{ident}", {"action": "recheck"})
    assert response.status_code == 303, response.text
    next_run = w.server.call(lambda: service.repo.runs(ident))[0]["id"]
    assert next_run != original_run
    preview = wait_state(w, next_run, ("preview", "failed"))
    assert preview["state"] == "preview", preview
    assert w.server.call(playlist) == edited
    page = browser.get(f"playlists/import/{ident}").text
    assert "Changes to your playlist: 3 → 5 songs" in page
    assert "Add Merge Late · Late One after Merge Orchard · Kept One" in page
    assert "Add Merge Late · Late Two after Merge Orchard · Kept Three" in page
    assert "1 removed by you" in page
    assert browser.submit(f"playlists/import/{ident}", {"action": "publish"}).status_code == 303
    completed = wait_state(w, next_run, ("published", "failed", "uncertain"))
    assert completed["state"] == "published", completed
    after = w.server.call(playlist)
    assert after["id"] == before["id"]
    assert titles(after) == ["Kept One", "Late One", "Kept Three", "Late Two", "Own Song"]
    assert [s["id"] for s in after["entry"]] == completed["data"]["playlist_ids"]
    assert completed["data"]["import_publish"]["write"] == "replace"  # One native write.
    for key in ("name", "comment", "public", "owner"):
        assert after.get(key) == edited.get(key), key
    assert (w.server.call(lambda: service.repo.run(original_run)))["data"] == published["data"]
    assert "Recheck" not in browser.get(f"playlists/import/{ident}").text


def test_stopped_before_native_write_can_start_fresh_matching_from_the_page(
    world: tuple[DeliveryWorld, Browser], monkeypatch: pytest.MonkeyPatch
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    ident, original_run = submit(
        world, lines="Glass Orchard — First Light\nLate Gate — Fresh Recording"
    )
    assert (
        browser.submit(
            f"playlists/import/{ident}", {"action": "publish", "excluded": "1"}
        ).status_code
        == 303
    )
    published = wait_state(w, original_run, ("published", "failed", "uncertain"))
    assert published["state"] == "published", published
    write_album(w.nd.music, Album("Late Gate", "Fresh Room", (Track("Fresh Recording", 1),)))
    w.nd.scan()
    assert browser.submit(f"playlists/import/{ident}", {"action": "recheck"}).status_code == 303
    next_run = w.server.call(lambda: service.repo.runs(ident))[0]["id"]
    assert wait_state(w, next_run, ("preview", "failed"))["state"] == "preview"
    original_prepare = service.check_tracks
    monkeypatch.setattr(
        service,
        "check_tracks",
        AsyncMock(side_effect=PlaylistError("Synthetic preparation failure.")),
    )
    assert browser.submit(f"playlists/import/{ident}", {"action": "publish"}).status_code == 303
    failed = wait_state(w, next_run, ("failed", "published", "uncertain"))
    assert failed["state"] == "failed", failed
    assert browser.submit(f"playlists/import/{ident}", {"action": "stop"}).status_code == 303
    page = browser.get(f"playlists/import/{ident}")
    assert "Resume" not in page.text  # Only large lists are updated in resumable steps.
    assert "Recheck 1 missing entries" in page.text
    monkeypatch.setattr(service, "check_tracks", original_prepare)
    assert browser.submit(f"playlists/import/{ident}", {"action": "recheck"}).status_code == 303
    fresh_run = w.server.call(lambda: service.repo.runs(ident))[0]["id"]
    assert fresh_run not in (original_run, next_run)
    assert wait_state(w, fresh_run, ("preview", "failed"))["state"] == "preview"
    assert browser.submit(f"playlists/import/{ident}", {"action": "publish"}).status_code == 303
    completed = wait_state(w, fresh_run, ("published", "failed", "uncertain"))
    assert completed["state"] == "published", completed
