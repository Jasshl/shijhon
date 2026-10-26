"""Last.fm tags on the song entries agents read: looked up for the songs asked about,
already known for lists, and never a reason for a tool to fail."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from shijhon.agents import tools
from shijhon.playlists.sound import SongSound
from tests.unit.test_agent_song_facts import FakeSound, Library, connect, library_with_songs
from tests.unit.test_agents import app_at, call
from tests.unit.test_playlist_curation import KEY, LastFm
from tests.unit.test_playlist_requests import connected

ARTIST = "Glass Orchard"


def tags_of(*numbers: int) -> dict[tuple[str, str], list[str]]:
    return {(ARTIST, f"Lantern {n}"): [f"tag {n}", "dream pop"] for n in numbers}


async def use_lastfm(app: Any, lastfm: LastFm | None) -> None:
    """The owner's connection with a Last.fm key (or none) and a scripted Last.fm."""
    await app.service.repo.connect(connected(history_key=KEY if lastfm else ""))
    if lastfm:
        await app.service.history.http.aclose()
        app.service.history.http = httpx.AsyncClient(transport=httpx.MockTransport(lastfm))


def test_song_facts_look_tags_up_once_and_lists_show_the_ones_known(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, lastfm = library_with_songs(), LastFm(tags_of(1, 2, 3))
            library.playlists["p"] = ("Evening", ["s1", "s2", "s4"])
            app.service.client.call = library
            await use_lastfm(app, lastfm)
            token = await connect(app, http)

            # Lists never ask Last.fm: nothing is known yet.
            before = await call(http, token, "search_library", {"query": "lantern", "limit": 4})
            assert before["tags"] == "cached_only" and lastfm.requests == []
            assert not any("tags" in t for t in before["tracks"])

            facts = await call(http, token, "get_song_facts", {"song_ids": ["s1", "s2", "s4"]})
            assert facts["ok"] and facts["tags"] == "ok"
            by_id = {s["id"]: s for s in facts["songs"]}
            assert by_id["s1"]["tags"] == ["tag 1", "dream pop"]
            assert by_id["s2"]["tags"] == ["tag 2", "dream pop"]
            assert "tags" not in by_id["s4"]  # Last.fm knows nothing of it, nor of the artist
            asked = len(lastfm.requests)
            assert asked == 4  # three songs, and the artist for the one without tags

            # Known now: the same songs cost no request, and lists show them.
            again = await call(http, token, "get_song_facts", {"song_ids": ["s1", "s2", "s4"]})
            assert again["tags"] == "ok" and len(lastfm.requests) == asked
            listed = await call(http, token, "search_library", {"query": "lantern", "limit": 4})
            shown = {t["id"]: t.get("tags") for t in listed["tracks"]}
            assert shown == {
                "s1": ["tag 1", "dream pop"],
                "s2": ["tag 2", "dream pop"],
                "s3": None,
                "s4": None,
            }
            assert listed["tags"] == "cached_only" and len(lastfm.requests) == asked
            playlist = await call(http, token, "get_native_playlists", {"playlist_id": "p"})
            assert playlist["tracks"][0]["tags"] == ["tag 1", "dream pop"]
            assert playlist["tags"] == "cached_only" and len(lastfm.requests) == asked
            # Only the artist and title (and the owner's key) go to Last.fm.
            assert {r[1] for r in lastfm.requests} == {ARTIST}

    anyio.run(run)


def test_without_a_lastfm_key_nothing_is_asked_and_the_tools_say_so(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library = library_with_songs()
            app.service.client.call = library
            token = await connect(app, http)
            facts = await call(http, token, "get_song_facts", {"song_ids": ["s1"]})
            assert facts["ok"] and facts["tags"] == "not_configured"
            assert "tags" not in facts["songs"][0]
            listed = await call(http, token, "search_library", {"query": "", "limit": 2})
            assert "tags" not in listed  # nothing to say about a feature that is not there
            capabilities = await call(http, token, "get_capabilities")
            assert capabilities["features"]["song_facts"]["tags"] is False
            await use_lastfm(app, LastFm({}))
            capabilities = await call(http, token, "get_capabilities")
            assert capabilities["features"]["song_facts"]["tags"] is True

    anyio.run(run)


@pytest.mark.parametrize("problem", ["unavailable", "slow", "limit"])
def test_a_missing_slow_or_limited_lastfm_never_fails_a_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library = library_with_songs()
            lastfm = LastFm(tags_of(1, 2, 3, 4), status=503 if problem == "unavailable" else 200)
            lastfm.slow = problem == "slow"
            library.playlists["p"] = ("Evening", ["s1", "s2", "s3", "s4"])
            app.service.client.call = library
            app.service.sound = FakeSound()
            await use_lastfm(app, lastfm)
            if problem == "slow":
                monkeypatch.setattr(tools, "TAG_SECONDS", 0.4)
                monkeypatch.setattr(tools, "TAG_LEAST", 0.1)
            if problem == "limit":
                monkeypatch.setattr(tools, "TAG_REQUESTS", 2)
            token = await connect(app, http)
            ids = ["s1", "s2", "s3", "s4"]
            started = anyio.current_time()
            facts = await call(http, token, "get_song_facts", {"song_ids": ids})
            assert anyio.current_time() - started < 5
            assert facts["ok"] and [s["id"] for s in facts["songs"]] == ids
            assert all(s["year"] for s in facts["songs"])  # the rest of the facts stay
            found = sum("tags" in s for s in facts["songs"])
            if problem == "unavailable":
                assert facts["tags"] == "unavailable" and found == 0
            elif problem == "slow":
                assert facts["tags"] == "partial" and found == 0
            else:
                assert facts["tags"] == "partial" and 0 < found < 4
            # Failures are not remembered as songs without tags.
            rows = await app.service.repo.store.fetchone("SELECT count(*) AS n FROM music_tags")
            assert rows["n"] == (found if problem == "limit" else 0)
            profile = await call(http, token, "get_playlist_profile", {"playlist_id": "p"})
            assert profile["ok"] and len(profile["songs"]) == 4
            listed = await call(http, token, "search_library", {"query": "", "limit": 3})
            assert listed["ok"] and len(listed["tracks"]) == 3

    anyio.run(run)


def test_playlist_profile_looks_up_the_listed_songs_and_summarizes_all_known_tags(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, lastfm = library_with_songs(), LastFm(tags_of(1, 2, 3, 4, 5, 6, 7, 8))
            library.playlists["p"] = ("Evening", [f"s{n}" for n in range(1, 9)])
            app.service.client.call = library
            await use_lastfm(app, lastfm)
            token = await connect(app, http)
            # Songs 7 and 8, far down the playlist, were looked up on an earlier occasion.
            await call(http, token, "get_song_facts", {"song_ids": ["s7", "s8"]})
            lastfm.requests.clear()

            profile = await call(
                http, token, "get_playlist_profile", {"playlist_id": "p", "limit": 3}
            )
            assert profile["ok"] and profile["tags"] == "ok"
            assert [s["id"] for s in profile["songs"]] == ["s1", "s2", "s3"]
            assert all(s["tags"][1] == "dream pop" for s in profile["songs"])
            assert len(lastfm.requests) == 3  # only the listed songs were asked for
            digest = profile["summary"]
            # Five songs have tags: the three listed and the two known from before.
            assert digest["songs"] == 8 and digest["with_tags"] == 5
            assert digest["tags"][0] == {"name": "dream pop", "songs": 5}
            assert {t["name"] for t in digest["tags"]} >= {"tag 1", "tag 7"}

    anyio.run(run)


def test_sound_tools_show_tags_beside_sound_facts(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound, lastfm = library_with_songs(), FakeSound(), LastFm(tags_of(5, 6))
            sound.data = {"s5": SongSound(energy=80), "s6": SongSound(energy=60)}
            sound.neighbours = sound.matches = ["s5", "s6", "s7"]
            sound.route = ["s5", "s7", "s6"]
            app.service.client.call, app.service.sound = library, sound
            await use_lastfm(app, lastfm)
            token = await connect(app, http)
            for name, args in (
                ("find_similar_songs", {"seed_song_ids": ["s1"]}),
                ("search_by_sound", {"text": "bright"}),
                ("find_sound_path", {"start_song_id": "s5", "end_song_id": "s6"}),
            ):
                result = await call(http, token, name, args)
                assert result["ok"] and result["tags"] == "ok", name
                by_id = {s["id"]: s for s in result["songs"]}
                assert by_id["s5"]["tags"] == ["tag 5", "dream pop"], name
                assert by_id["s5"]["sound"] == {"energy": 80}, name
                assert "tags" not in by_id["s7"], name

    anyio.run(run)


def test_a_call_near_its_deadline_asks_last_fm_for_nothing() -> None:
    async def run() -> None:
        def within(seconds: float) -> float:
            # (Only the deadline is read: no checkpoint is wanted.)
            with anyio.CancelScope(deadline=anyio.current_time() + seconds):  # noqa: ASYNC100
                return tools.Tools.tag_wait()

        assert tools.Tools.tag_wait() == tools.TAG_SECONDS  # no deadline
        assert within(100) == tools.TAG_SECONDS
        assert 0 < within(tools.TAG_MARGIN + 2) <= 2
        assert within(tools.TAG_MARGIN - 1) == 0

    anyio.run(run)


def test_tag_lookups_read_the_library_only_through_song_reads(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library = Library()
            library.songs["x"] = {
                "id": "x",
                "title": "Lantern X",
                "artist": ARTIST,
                "album": "Small Lights",
                "duration": 200,
            }
            app.service.client.call = library
            await use_lastfm(app, LastFm({(ARTIST, "Lantern X"): ["quiet"]}))
            token = await connect(app, http)
            await call(http, token, "get_song_facts", {"song_ids": ["x"]})
            assert {name for name, _ in library.calls} <= {"getSong", "ping"}

    anyio.run(run)
