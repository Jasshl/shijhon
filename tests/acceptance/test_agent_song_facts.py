"""Agent tools for song facts and sound, over the real MCP client and a disposable Navidrome.

The analyzer is scripted; the library, its tags, playlists and accounts are Navidrome's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from shijhon.playlists.sound import SongSound
from tests.conftest import NavidromeFactory
from tests.harness.agents import connect_agent
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.unit.test_agent_song_facts import FakeSound

LISTENER = ("listener", "listener-password")


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[DeliveryWorld, Browser]]:
    nd = navidrome_factory()
    write_album(
        nd.music,
        Album(
            "Aster & Vale",
            "Lamp Hours",
            (Track("Slow Lamp", 1), Track("Night Kettle", 2)),
            recording_date="1998",
            extra={"genre": ["Dream Pop", "Shoegaze"], "bpm": ["96"], "mood": ["calm"]},
        ),
    )
    write_album(
        nd.music,
        Album(
            "Inkwell & Reed",
            "Bright Rooms",
            (Track("Loud Window", 1), Track("Fast Door", 2), Track("Plain Song", 3)),
            recording_date="2011",
            genre="Punk",
        ),
    )
    nd.create_user(*LISTENER)
    nd.scan()
    with delivery_world(nd, tmp_path_factory.mktemp("facts"), agents=True) as w:
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        yield w, browser


@asynccontextmanager
async def agent(w: DeliveryWorld, access_token: str) -> AsyncIterator[ClientSession]:
    async with (
        httpx2.AsyncClient(headers={"authorization": "Bearer " + access_token}) as http,
        streamable_http_client(w.server.base_url + "/shijhon/mcp", http_client=http) as (
            read,
            write,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def ask(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    assert result.structured_content is not None
    return dict(result.structured_content)


def test_agent_reads_tags_playlists_and_sound_for_its_own_account(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    playlists = w.services.playlists
    assert playlists
    owner = connect_agent(
        browser.http, w.server.base_url, ADMIN_USER, ADMIN_PASSWORD, "create status library"
    )
    guest = connect_agent(
        browser.http, w.server.base_url, *LISTENER, "create status library", client_name="Guest"
    )
    narrow = connect_agent(
        browser.http, w.server.base_url, ADMIN_USER, ADMIN_PASSWORD, "create status"
    )

    async def run() -> None:
        async with agent(w, owner["access_token"]) as session:
            found = await ask(session, "search_library", {"query": "", "limit": 100})
            by_title = {t["title"]: t for t in found["tracks"]}
            assert {"Slow Lamp", "Loud Window", "Fast Door"} <= set(by_title)
            slow = by_title["Slow Lamp"]
            assert slow["year"] == 1998 and slow["bpm"] == 96 and slow["moods"] == ["calm"]
            assert set(slow["genres"]) == {"Dream Pop", "Shoegaze"}
            assert by_title["Loud Window"]["genres"] == ["Punk"]
            assert "bpm" not in by_title["Loud Window"] and "moods" not in by_title["Loud Window"]
            assert not {"playCount", "userRating", "starred", "path"} & set(slow)
            ids = {title: track["id"] for title, track in by_title.items()}

            def song(title: str) -> str:
                return ids[title]

            admin = w.client()
            admin.ok(
                "createPlaylist",
                [
                    ("name", "Evening"),
                    ("songId", song("Slow Lamp")),
                    ("songId", song("Night Kettle")),
                    ("songId", song("Loud Window")),
                ],
            )
            listed = await ask(session, "get_native_playlists", {})
            playlist_id = next(p["id"] for p in listed["playlists"] if p["name"] == "Evening")

            # No analyzer yet: tags still work, sound tools say what is missing.
            plain = await ask(session, "get_playlist_profile", {"playlist_id": playlist_id})
            assert plain["ok"] and plain["sound_analysis"] == "not_configured"
            assert plain["summary"]["year"] == {
                "songs": 3,
                "min": 1998,
                "median": 1998,
                "max": 2011,
            }
            assert plain["summary"]["with_sound_facts"] == 0 and "sound" not in plain["summary"]
            unset = await ask(session, "find_similar_songs", {"playlist_id": playlist_id})
            assert unset["error"]["code"] == "setup_required"
            capabilities = await ask(session, "get_capabilities", {})
            assert not capabilities["features"]["sound_analysis"]["configured"]

            sound = FakeSound()
            sound.data = {
                song("Slow Lamp"): SongSound(
                    tempo=96.0, key="A minor", energy=15, labels=["dream"]
                ),
                song("Loud Window"): SongSound(tempo=172.0, energy=95, features={"party": 80}),
                song("Fast Door"): SongSound(tempo=180.0, energy=90),
            }
            sound.neighbours = [song("Slow Lamp"), song("Fast Door"), song("Plain Song")]
            sound.matches = [song("Fast Door"), song("Plain Song")]
            sound.route = [song("Slow Lamp"), song("Fast Door"), song("Loud Window")]

            async def install() -> None:
                playlists.sound = sound

            w.server.call(install)

            tracks = await ask(
                session,
                "get_native_playlists",
                {"playlist_id": playlist_id, "include_sound": True},
            )
            assert [t["title"] for t in tracks["tracks"]] == [
                "Slow Lamp",
                "Night Kettle",
                "Loud Window",
            ]
            assert tracks["tracks"][0]["sound"]["energy"] == 15
            assert "sound" not in tracks["tracks"][1] and tracks["songs_without_sound_facts"] == 1
            assert tracks["tracks"][0]["genres"] and tracks["tracks"][2]["year"] == 2011

            profile = await ask(session, "get_playlist_profile", {"playlist_id": playlist_id})
            summary = profile["summary"]
            assert summary["songs"] == 3 and summary["with_sound_facts"] == 2
            assert summary["sound"]["energy"] == {"songs": 2, "min": 15, "median": 55, "max": 95}
            assert {g["name"] for g in summary["genres"]} == {"Dream Pop", "Shoegaze", "Punk"}

            facts = await ask(
                session, "get_song_facts", {"song_ids": [song("Fast Door"), "unknown-song"]}
            )
            assert facts["songs"][0]["title"] == "Fast Door"
            assert facts["songs"][0]["sound"]["tempo"] == 180.0
            assert facts["unreadable_song_ids"] == ["unknown-song"]

            similar = await ask(
                session, "find_similar_songs", {"playlist_id": playlist_id, "count": 5}
            )
            # The playlist's own Slow Lamp is not news; the others are library songs.
            assert [s["title"] for s in similar["songs"]] == ["Fast Door", "Plain Song"]
            assert similar["songs"][0]["artist"] == "Inkwell & Reed"
            assert similar["songs"][0]["album"] == "Bright Rooms"
            assert "sound" not in similar["songs"][1]
            by_text = await ask(session, "search_by_sound", {"text": "fast and loud", "count": 1})
            assert [s["title"] for s in by_text["songs"]] == ["Fast Door"]
            path = await ask(
                session,
                "find_sound_path",
                {"start_song_id": song("Slow Lamp"), "end_song_id": song("Loud Window")},
            )
            assert [s["title"] for s in path["songs"]] == ["Slow Lamp", "Fast Door", "Loud Window"]

            ready = (await ask(session, "get_capabilities", {}))["features"]["sound_analysis"]
            assert ready["ready"] and ready["tools"]

            sound.fail = True
            down = await ask(session, "search_by_sound", {"text": "fast"})
            assert down["error"]["code"] == "temporarily_unavailable"
            still = await ask(session, "get_song_facts", {"song_ids": [song("Fast Door")]})
            assert still["ok"] and still["sound_analysis"] == "unavailable"
            assert still["songs"][0]["year"] == 2011
            sound.fail = False

        # Another account reads the shared library but not the owner's playlist.
        async with agent(w, guest["access_token"]) as session:
            shared = await ask(session, "get_song_facts", {"song_ids": [song("Fast Door")]})
            assert shared["ok"] and shared["songs"][0]["title"] == "Fast Door"
            for name, arguments in (
                ("get_native_playlists", {"playlist_id": playlist_id}),
                ("get_playlist_profile", {"playlist_id": playlist_id}),
                ("find_similar_songs", {"playlist_id": playlist_id}),
            ):
                refused = await ask(session, name, arguments)
                assert not refused["ok"], name
            own = await ask(session, "get_native_playlists", {})
            assert own["playlists"] == []

        # Without library permission nothing of this is readable.
        async with agent(w, narrow["access_token"]) as session:
            for name, arguments in (
                ("get_song_facts", {"song_ids": [song("Fast Door")]}),
                ("get_playlist_profile", {"playlist_id": playlist_id}),
                ("search_by_sound", {"text": "fast"}),
            ):
                denied = await ask(session, name, arguments)
                assert denied["error"]["code"] == "permission_required", name

    anyio.run(run)
