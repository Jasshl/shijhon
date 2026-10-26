"""Refreshes that learn from the owner's edits, against a disposable stock Navidrome with
invented music and a scripted model."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.playlists.model import Connection
from shijhon.playlists.prompt import Ordered, Proposals
from tests.acceptance.test_R_playlist_requests import mint
from tests.acceptance.test_R_playlists import wait_state
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

ARTIST = "Lumen Harbor"
ALBUM = "Night Ferries"
BEST_OF = "Harbor Lights (Best Of)"
TITLES = ("Tidewater", "Fog Bell", "Quay Lights", "Salt Wind", "Lantern Row", "Driftline")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, Browser]]:
    nd = navidrome_factory()
    write_album(nd.music, Album(ARTIST, ALBUM, tuple(Track(t, i) for i, t in enumerate(TITLES, 1))))
    # The same recording on another album: another Navidrome song ID.
    write_album(nd.music, Album(ARTIST, BEST_OF, (Track("Tidewater", 1),)))
    nd.scan()
    with delivery_world(nd, tmp_path_factory.mktemp("edits"), agents=True) as w:
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        response = browser.submit(
            "playlists",
            {
                "password": ADMIN_PASSWORD,
                "history_user": "",
                "backend": "api",
                "endpoint": "https://model.invalid/v1",
                "model": "synthetic",
            },
            "playlists/connection",
        )
        assert response.status_code == 303, response.text
        yield w, browser
        browser.close()


class Model:
    """Proposes the scripted songs and keeps the first candidates in their order."""

    def __init__(self) -> None:
        self.proposals: list[tuple[str, str]] = []
        self.seen: list[tuple[Any, dict[str, Any], str]] = []

    async def __call__(
        self, connection: Connection, prompt: str, *, schema: Any, instructions: str, **_: Any
    ) -> Any:
        payload = json.loads(prompt)
        self.seen.append((schema, payload, instructions))
        if schema is Proposals:
            return {
                "tracks": [
                    {"artist": ARTIST, "title": title, "album": album, "version": ""}
                    for title, album in self.proposals
                ]
            }
        assert schema is Ordered
        return {
            "goal": "Calm harbor songs",
            "constraints_satisfied": True,
            "selections": [
                {"id": c["id"], "reason": "Calm", "fit": "meets"}
                for c in payload["candidates"][: payload["count"]]
            ],
        }


def test_a_refresh_learns_the_owners_edits_and_keeps_their_comment(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    model = Model()
    model.proposals = [(t, ALBUM) for t in ("Tidewater", "Fog Bell", "Quay Lights")]

    resolved: list[str] = []  # song IDs the suggestions resolved to
    resolve = service.resolver.resolve

    async def recording(connection: Connection, wanted: dict[str, Any]) -> Any:
        found = await resolve(connection, wanted)
        resolved.extend([found.id] if found else [])
        return found

    async def install() -> None:
        service.curator.api = model
        service.resolver.resolve = recording

    w.server.call(install)
    auth = {"Authorization": "Bearer " + mint(browser), "Idempotency-Key": "learn-edits"}
    path = "/shijhon/api/playlists/requests"
    payload = {
        "name": "Harbor evenings",
        "prompt": "Calm harbor songs",
        "count": 3,
        "max_per_artist": 3,
        "save_instructions": True,
        "cadence": "weekly",
    }
    response = browser.http.post(path, headers=auth, json=payload)
    assert response.status_code == 202, response.text
    ident, first_run = response.json()["id"], response.json()["run"]
    first = wait_state(w, first_run, ("published", "failed", "uncertain"))
    assert first["state"] == "published", first["error"]
    assert "edits" not in first["data"]  # nothing to learn from on the first run
    assert model.seen[0][1].get("user_edits") is None

    async def owner_of() -> str:
        row = await service.repo.store.fetchone(
            "SELECT owner FROM playlist_requests WHERE id=?", [ident]
        )
        return str(row["owner"])

    owner = w.server.call(owner_of)

    async def edit() -> tuple[str, dict[str, str]]:
        """The owner removes Tidewater, adds Salt Wind and writes a comment, in Navidrome."""
        connection = await service.connection(owner)
        mix = await service.repo.mix(ident, connection.user_id)
        found = await service.client.call(
            connection, "search3", [("query", ARTIST), ("songCount", "20")]
        )
        ids = {(s["title"], s["album"]): s["id"] for s in found["searchResult3"]["song"]}
        native = await service.client.playlist(connection, mix["playlist_id"])
        position = [s["title"] for s in native["entry"]].index("Tidewater")
        await service.client.call(
            connection,
            "updatePlaylist",
            [
                ("playlistId", mix["playlist_id"]),
                ("songIndexToRemove", str(position)),
                ("songIdToAdd", ids[("Salt Wind", ALBUM)]),
                ("comment", "Evenings by the water"),
            ],
        )
        return mix["playlist_id"], {f"{t} / {a}": i for (t, a), i in ids.items()}

    playlist, ids = w.server.call(edit)
    model.seen.clear()
    # The AI suggests the removed song again, also from the other album.
    model.proposals = [
        ("Tidewater", ALBUM),
        ("Tidewater", BEST_OF),
        ("Fog Bell", ALBUM),
        ("Lantern Row", ALBUM),
        ("Driftline", ALBUM),
    ]

    async def refresh() -> str:
        run = await service.repo.enqueue(ident)
        service.notify()
        return run

    second = wait_state(w, w.server.call(refresh), ("published", "failed", "uncertain"))
    assert second["state"] == "published", second["error"]
    assert [s["title"] for s in second["data"]["selected"]] == [
        "Fog Bell",
        "Lantern Row",
        "Driftline",
    ]
    removed = {"id": ids[f"Tidewater / {ALBUM}"], "artist": ARTIST, "title": "Tidewater"}
    added = {"id": ids[f"Salt Wind / {ALBUM}"], "artist": ARTIST, "title": "Salt Wind"}
    assert second["data"]["edits"] == {
        "learned": {"removed": [{**removed, "album": ALBUM}], "added": [{**added, "album": ALBUM}]},
        "remembered": {"removed": 1, "added": 1},
        "left_out": 1,
    }
    propose, order = (payload for _, payload, _ in model.seen)
    shown = {
        "removed": [{"artist": ARTIST, "title": "Tidewater", "album": ALBUM}],
        "added": [{"artist": ARTIST, "title": "Salt Wind", "album": ALBUM}],
    }
    assert propose["user_edits"] == shown and order["user_edits"] == shown
    assert "Tidewater" not in {c["title"] for c in order["candidates"]}
    # Left out by artist and title: the other album's Tidewater resolves to another ID.
    assert ids[f"Tidewater / {BEST_OF}"] != removed["id"]
    assert ids[f"Tidewater / {BEST_OF}"] in resolved

    async def native() -> dict[str, Any]:
        connection = await service.connection(owner)
        return await service.client.playlist(connection, playlist)

    replaced = w.server.call(native)
    # The same playlist, its songs replaced and the owner's comment kept.
    assert [s["id"] for s in replaced["entry"]] == second["data"]["native_ids"]
    assert replaced["comment"] == "Evenings by the water"
    status = browser.http.get(f"{path}/{ident}", headers=auth).json()
    assert status["edits"]["learned"]["removed"][0]["title"] == "Tidewater"

    # A playlist renamed in Navidrome stops the next refresh before any AI call.
    async def rename() -> None:
        connection = await service.connection(owner)
        await service.client.call(
            connection, "updatePlaylist", [("playlistId", playlist), ("name", "My harbor")]
        )

    w.server.call(rename)
    model.seen.clear()
    third = wait_state(w, w.server.call(refresh), ("published", "failed", "uncertain"))
    assert third["state"] == "failed" and "renamed outside Shijhon" in third["error"]
    assert model.seen == []
    assert w.server.call(native)["name"] == "My harbor"
