"""The agents' find_listened_together: songs listened together with seed songs, by
ListenBrainz (for everyone, unless it is off) and Last.fm (with the user's key), with
stand-ins for both and a scripted library."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import httpx

from shijhon.agents import tools
from shijhon.config import ListenBrainzSettings
from shijhon.playlists.listenbrainz import ListenBrainz
from tests.harness.fake_lastfm import KEY, FakeLastFm
from tests.harness.fake_listenbrainz import FakeListenBrainz
from tests.unit.test_agent_song_facts import connect, library_with_songs
from tests.unit.test_agents import app_at, call
from tests.unit.test_playlist_requests import connected

ARTIST = "Glass Orchard"  # the scripted library's artist


def listening(app: Any, fake: FakeListenBrainz, **settings: Any) -> ListenBrainz:
    found = ListenBrainz(ListenBrainzSettings(**settings), transport=fake.transport())
    app.service.history.listenbrainz = found
    return found


async def use_lastfm(app: Any, lastfm: FakeLastFm) -> None:
    await app.service.repo.connect(connected(history_key=KEY))
    app.service.history.http = httpx.AsyncClient(transport=lastfm.transport())


def test_songs_listened_together_with_library_songs_and_named_ones(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            app.service.client.call = library_with_songs()
            fake = FakeListenBrainz()
            fake.song(ARTIST, "Lantern 1", [("Copper Owls", "Low Lights"), (ARTIST, "Lantern 2")])
            fake.song(
                "Marrow Lane", "Paper Boats", [("Copper Owls", "Low Lights"), ("Ash", "Ember")]
            )
            listening(app, fake)
            token = await connect(app, http)
            found = await call(
                http,
                token,
                "find_listened_together",
                {
                    "seed_song_ids": ["s1", "nope"],
                    "seed_tracks": [{"artist": "Marrow Lane", "title": "Paper Boats"}],
                    "count": 10,
                },
            )
            assert found["ok"], found
            assert found["seeds"] == [
                {"artist": ARTIST, "title": "Lantern 1"},
                {"artist": "Marrow Lane", "title": "Paper Boats"},
            ]
            assert found["unreadable_seed_ids"] == ["nope"]
            # The song both seeds' lists start with first; names and their sources only.
            assert found["songs"][0] == {
                "artist": "Copper Owls",
                "title": "Low Lights",
                "sources": ["listenbrainz"],
            }
            assert {(s["artist"], s["title"]) for s in found["songs"]} == {
                ("Copper Owls", "Low Lights"),
                (ARTIST, "Lantern 2"),
                ("Ash", "Ember"),
            }
            assert found["sources"] == {"listenbrainz": "ok", "lastfm": "not_configured"}
            # Only names went to ListenBrainz: no account, no token.
            for asked in fake.requests:
                assert "authorization" not in asked.headers
                assert "owner" not in str([asked.params, asked.body])
            # The capabilities say what answers it.
            capabilities = await call(http, token, "get_capabilities")
            feature = capabilities["features"]["listening_similarity"]
            assert feature["ready"] and feature["tools"] == ["find_listened_together"]
            assert feature["sources"] == {"listenbrainz": True, "lastfm": False}

    anyio.run(run)


def test_both_sources_together_and_a_failing_one_is_said(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            app.service.client.call = library_with_songs()
            lastfm = FakeLastFm()
            lastfm.tracks[(ARTIST, "Lantern 1")] = [("Ash", "Ember"), ("Copper Owls", "Low Lights")]
            await use_lastfm(app, lastfm)
            fake = FakeListenBrainz()
            fake.song(ARTIST, "Lantern 1", [("Copper Owls", "Low Lights"), ("Birch", "Hollow")])
            listening(app, fake)
            token = await connect(app, http)
            found = await call(http, token, "find_listened_together", {"seed_song_ids": ["s1"]})
            assert found["sources"] == {"listenbrainz": "ok", "lastfm": "ok"}
            assert found["songs"][0] == {
                "artist": "Copper Owls",
                "title": "Low Lights",
                "sources": ["lastfm", "listenbrainz"],
            }
            assert len(found["songs"]) == 3
            fake.status = 503
            listening(app, fake)
            found = await call(http, token, "find_listened_together", {"seed_song_ids": ["s1"]})
            assert found["ok"] and found["sources"] == {
                "listenbrainz": "unavailable",
                "lastfm": "ok",
            }
            assert [s["sources"] for s in found["songs"]] == [["lastfm"], ["lastfm"]]

    anyio.run(run)


def test_without_any_source_it_says_what_to_set_up(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            app.service.client.call = library_with_songs()
            fake = FakeListenBrainz()
            listening(app, fake, enabled=False)
            token = await connect(app, http)
            found = await call(http, token, "find_listened_together", {"seed_song_ids": ["s1"]})
            assert not found["ok"]
            assert found["error"]["code"] == "setup_required"
            assert found["error"]["missing_configuration"] == ["listening_similarity"]
            assert "ListenBrainz page" in found["error"]["message"]
            assert fake.requests == []
            capabilities = await call(http, token, "get_capabilities")
            feature = capabilities["features"]["listening_similarity"]
            assert not feature["configured"] and feature["missing_configuration"]

    anyio.run(run)


def test_inputs_are_bounded(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            app.service.client.call = library_with_songs()
            fake = FakeListenBrainz()
            listening(app, fake)
            token = await connect(app, http)
            for args in (
                {},
                {"seed_song_ids": ["s1"], "count": 0},
                {"seed_song_ids": ["s1"], "count": 101},
                {"seed_song_ids": [f"s{n}" for n in range(1, 7)]},
                {
                    "seed_song_ids": ["s1", "s2", "s3"],
                    "seed_tracks": [{"artist": "A", "title": f"T{n}"} for n in range(3)],
                },
            ):
                found = await call(http, token, "find_listened_together", args)
                assert not found["ok"], args
            assert fake.requests == []

    anyio.run(run)


def test_a_source_past_the_deadline_leaves_the_other_s_finished_songs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(tools, "LISTENING_SECONDS", 1.0)

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            app.service.client.call = library_with_songs()
            lastfm = FakeLastFm()
            lastfm.tracks[(ARTIST, "Lantern 1")] = [("Ash", "Ember"), ("Birch", "Hollow")]
            await use_lastfm(app, lastfm)
            fake = FakeListenBrainz()
            fake.song(ARTIST, "Lantern 1", [("Copper Owls", "Low Lights")])
            fake.delay = 5.0  # it never answers within the call
            listening(app, fake, timeout_seconds=10)
            token = await connect(app, http)
            found = await call(http, token, "find_listened_together", {"seed_song_ids": ["s1"]})
            assert found["ok"]
            assert [(s["artist"], s["title"], s["sources"]) for s in found["songs"]] == [
                ("Ash", "Ember", ["lastfm"]),
                ("Birch", "Hollow", ["lastfm"]),
            ]
            assert found["sources"] == {"lastfm": "ok", "listenbrainz": "unavailable"}

    anyio.run(run)
