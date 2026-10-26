"""Song facts and sound tools for agents, with a scripted analyzer and a scripted library."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.agents import tools
from shijhon.agents.songs import entry, spaced, summary
from shijhon.playlists.model import PlaylistError
from shijhon.playlists.sound import SongSound, SoundStatus, SoundUnavailable
from tests.unit.test_agents import BASE, app_at, begin, call, finish

OUTAGE = PlaylistError("Navidrome could not complete the owner-authenticated request.")


def song(ident: str, title: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "title": title,
        "artist": "Glass Orchard",
        "album": "Small Lights",
        "duration": 200,
        **fields,
    }


class Library:
    """Navidrome's answers for the calls the tools make; every call is recorded."""

    def __init__(self) -> None:
        self.songs: dict[str, dict[str, Any]] = {}
        self.playlists: dict[str, tuple[str, list[str]]] = {}
        self.owner = "owner"
        self.down = False
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(
        self, connection: Any, method: str, params: Sequence[tuple[str, str]] = ()
    ) -> dict[str, Any]:
        fields = dict(params)
        self.calls.append((method, fields))
        if self.down:
            raise OUTAGE
        if method == "ping":
            return {}
        if method == "getSong":
            if fields["id"] not in self.songs:
                raise OUTAGE
            return {"song": self.songs[fields["id"]]}
        if method == "getPlaylist":
            if fields["id"] not in self.playlists:
                raise OUTAGE
            name, ids = self.playlists[fields["id"]]
            return {
                "playlist": {
                    "id": fields["id"],
                    "name": name,
                    "owner": self.owner,
                    "songCount": len(ids),
                    "duration": 60 * len(ids),
                    "entry": [self.songs[i] for i in ids],
                }
            }
        if method == "getPlaylists":
            return {
                "playlists": {
                    "playlist": [
                        {"id": i, "name": n, "owner": self.owner, "songCount": len(ids)}
                        for i, (n, ids) in self.playlists.items()
                    ]
                }
            }
        if method == "search3":
            start = int(fields["songOffset"])
            return {
                "searchResult3": {
                    "song": list(self.songs.values())[start : start + int(fields["songCount"])]
                }
            }
        raise AssertionError(method)

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)


class FakeSound:
    """Scripted facts, neighbours, descriptions and paths, with unavailable and slow modes."""

    def __init__(self) -> None:
        self.data: dict[str, SongSound] = {}
        self.neighbours: list[str] = []
        self.matches: list[str] = []
        self.route: list[str] = []
        self.fail = False
        self.delay = 0.0
        self.status_answer = SoundStatus(configured=True, reachable=True, songs=3)
        self.calls: list[tuple[str, Any]] = []

    async def wait(self) -> None:
        if self.delay:
            await anyio.sleep(self.delay)
        if self.fail:
            raise SoundUnavailable("scripted outage")

    async def facts(self, song_ids: Sequence[str]) -> dict[str, SongSound]:
        self.calls.append(("facts", list(song_ids)))
        await self.wait()
        return {i: self.data[i] for i in song_ids if i in self.data}

    async def similar(
        self, seeds: Sequence[str], *, avoid: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        self.calls.append(("similar", (list(seeds), list(avoid), count)))
        await self.wait()
        return self.neighbours[:count]

    async def search(
        self, text: str, *, more: Sequence[str] = (), less: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        self.calls.append(("search", (text, list(more), list(less), count)))
        await self.wait()
        return self.matches[:count]

    async def path(self, start: str, end: str, *, steps: int = 12) -> list[str]:
        self.calls.append(("path", (start, end, steps)))
        await self.wait()
        return self.route

    async def status(self) -> SoundStatus:
        await self.wait()
        return self.status_answer

    def used(self, name: str) -> list[Any]:
        return [arguments for called, arguments in self.calls if called == name]


def library_with_songs() -> Library:
    library = Library()
    for number in range(1, 9):
        library.songs[f"s{number}"] = song(f"s{number}", f"Lantern {number}", year=2000 + number)
    return library


async def connect(app: Any, http: Any, scopes: str = "create status library") -> str:
    token: str = (await finish(http, await begin(http, scopes), scopes))["access_token"]
    return token


def test_entries_show_native_facts_but_never_listening_evidence() -> None:
    raw = song(
        "a",
        "Lantern",
        year=1999,
        genres=[{"name": "Dream Pop"}, {"name": "dream pop"}, {"name": "Shoegaze"}, {"name": ""}],
        genre="Dream Pop",
        bpm=96,
        moods=["calm", "dreamy"],
        playCount=40,
        userRating=5,
        starred="2026-01-01",
        path="/private/a.flac",
        played="2026-01-01",
    )
    assert entry(raw) == {
        "id": "a",
        "title": "Lantern",
        "artist": "Glass Orchard",
        "album": "Small Lights",
        "duration": 200,
        "year": 1999,
        "genres": ["Dream Pop", "Shoegaze"],
        "bpm": 96,
        "moods": ["calm", "dreamy"],
    }
    # The single genre of an older answer is used when there is no list; unset values vanish.
    assert entry(song("b", "Plain", genre="Folk", year=0, bpm=0, moods=[])) == {
        "id": "b",
        "title": "Plain",
        "artist": "Glass Orchard",
        "album": "Small Lights",
        "duration": 200,
        "genres": ["Folk"],
    }
    assert "year" not in entry({"id": "c", "year": True})


def test_summary_maths_and_unknowns() -> None:
    entries = [
        {"id": "1", "year": 1990, "genres": ["Rock", "Pop"], "bpm": 100},
        {"id": "2", "year": 2000, "genres": ["rock"], "bpm": 120},
        {"id": "3", "year": 2010, "genres": ["Jazz"]},
        {"id": "4", "year": 2020, "genres": ["Pop"]},
        {"id": "5"},
    ]
    sounds = {
        "1": SongSound(
            tempo=100.0,
            energy=10,
            features={"danceable": 20, "happy": 40},
            labels=["rock", "90s"],
        ),
        "2": SongSound(tempo=120.0, energy=30, features={"danceable": 60}, labels=["rock"]),
        "3": SongSound(tempo=90.5, energy=80, labels=["jazz"]),
        "gone": SongSound(energy=100),
    }
    assert summary(entries, sounds) == {
        "songs": 5,
        "year": {"songs": 4, "min": 1990, "median": 2005, "max": 2020},
        "bpm": {"songs": 2, "min": 100, "median": 110, "max": 120},
        "genres": [
            {"name": "Pop", "songs": 2},
            {"name": "Rock", "songs": 2},
            {"name": "Jazz", "songs": 1},
        ],
        "with_sound_facts": 3,
        "sound": {
            "energy": {"songs": 3, "min": 10, "median": 30, "max": 80},
            "tempo": {"songs": 3, "min": 90.5, "median": 100, "max": 120},
            "features": {
                "danceable": {"songs": 2, "min": 20, "median": 40, "max": 60},
                "happy": {"songs": 1, "min": 40, "median": 40, "max": 40},
            },
            "labels": [
                {"name": "rock", "songs": 2},
                {"name": "90s", "songs": 1},
                {"name": "jazz", "songs": 1},
            ],
        },
    }
    # Nothing known: only counts, no invented numbers.
    assert summary([{"id": "x"}], {}) == {"songs": 1, "with_sound_facts": 0}


def test_spaced_samples_the_whole_list() -> None:
    ids = [str(i) for i in range(100)]
    assert spaced(ids[:5], 30) == ids[:5]
    sample = spaced(ids, 30)
    assert len(sample) == 30 and sample[0] == "0" and int(sample[-1]) >= 90
    assert sample == sorted(sample, key=int)


def test_enriched_library_and_playlist_entries_with_optional_sound(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            library.songs["s1"].update(
                genres=[{"name": "Dream Pop"}], bpm=96, moods=["calm"], playCount=9, userRating=4
            )
            library.playlists["p"] = ("Evening", ["s1", "s2", "s3"])
            sound.data = {
                "s1": SongSound(tempo=96.2, key="C major", energy=22, labels=["dream pop"]),
                "s3": SongSound(energy=70, features={"happy": 55}),
            }
            app.service.client.call = library
            token = await connect(app, http)

            plain = await call(http, token, "search_library", {"query": "lantern", "limit": 3})
            assert [t["id"] for t in plain["tracks"]] == ["s1", "s2", "s3"]
            first = plain["tracks"][0]
            assert first["genres"] == ["Dream Pop"] and first["bpm"] == 96
            assert first["moods"] == ["calm"] and first["year"] == 2001
            assert "genre" not in first
            dumped = json.dumps(plain)
            assert "playCount" not in dumped and "userRating" not in dumped
            # No sound unless asked for, and then no call to the analyzer at all.
            assert "sound" not in dumped and "sound_analysis" not in plain
            app.service.sound = sound
            assert sound.calls == []
            await call(http, token, "search_library", {"query": "lantern", "limit": 3})
            assert sound.calls == []

            rich = await call(
                http,
                token,
                "search_library",
                {"query": "lantern", "limit": 3, "include_sound": True},
            )
            assert rich["sound_analysis"] == "ok" and rich["songs_without_sound_facts"] == 1
            assert rich["tracks"][0]["sound"] == {
                "tempo": 96.2,
                "key": "C major",
                "energy": 22,
                "labels": ["dream pop"],
            }
            assert "sound" not in rich["tracks"][1]
            assert rich["tracks"][2]["sound"] == {"energy": 70, "features": {"happy": 55}}
            assert sound.used("facts") == [["s1", "s2", "s3"]]

            listed = await call(
                http,
                token,
                "get_native_playlists",
                {"playlist_id": "p", "limit": 2, "include_sound": True},
            )
            assert [t["id"] for t in listed["tracks"]] == ["s1", "s2"] and listed["has_more"]
            assert listed["tracks"][0]["genres"] == ["Dream Pop"]
            assert listed["tracks"][0]["sound"]["energy"] == 22
            assert listed["songs_without_sound_facts"] == 1
            bare = await call(http, token, "get_native_playlists", {"playlist_id": "p"})
            assert "sound" not in json.dumps(bare) and bare["tracks"][0]["year"] == 2001

    anyio.run(run)


def test_include_sound_without_or_with_a_failing_analyzer_keeps_the_library_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            app.service.client.call = library
            token = await connect(app, http)
            args = {"query": "", "limit": 2, "include_sound": True}
            missing = await call(http, token, "search_library", args)
            assert missing["ok"] and missing["sound_analysis"] == "not_configured"
            assert len(missing["tracks"]) == 2 and missing["songs_without_sound_facts"] == 2
            app.service.sound = sound
            sound.fail = True
            down = await call(http, token, "search_library", args)
            assert down["ok"] and down["sound_analysis"] == "unavailable"
            assert len(down["tracks"]) == 2
            sound.fail, sound.delay = False, 5
            monkeypatch.setattr(tools, "FACTS_SECONDS", 0.05)
            slow = await call(http, token, "search_library", args)
            assert slow["ok"] and slow["sound_analysis"] == "unavailable"

    anyio.run(run)


def test_song_facts_lists_unreadable_and_songs_without_facts_and_is_bounded(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            library.songs["s2"].update(genre="Folk", bpm=80, moods=["warm"], playCount=3)
            sound.data = {"s1": SongSound(tempo=120.0, energy=90)}
            app.service.client.call, app.service.sound = library, sound
            token = await connect(app, http)

            facts = await call(
                http, token, "get_song_facts", {"song_ids": ["s2", "s1", "nope", "s2"]}
            )
            assert [s["id"] for s in facts["songs"]] == ["s2", "s1"]
            assert facts["unreadable_song_ids"] == ["nope"]
            assert facts["without_sound_facts"] == ["s2"]
            assert facts["songs"][0]["genres"] == ["Folk"] and facts["songs"][0]["moods"] == [
                "warm"
            ]
            assert facts["songs"][1]["sound"] == {"tempo": 120.0, "energy": 90}
            assert library.count("getSong") == 3  # the repeated ID is read once
            assert "playCount" not in json.dumps(facts)

            for bad in ([], [f"s{i}" for i in range(101)], [""], ["x" * 101], ["a\nb"]):
                refused = await call(http, token, "get_song_facts", {"song_ids": bad})
                assert not refused["ok"] and refused["error"]["code"] == "request_unavailable"
            library.songs.update({f"m{i}": song(f"m{i}", f"M{i}") for i in range(100)})
            many = await call(
                http, token, "get_song_facts", {"song_ids": [f"m{i}" for i in range(100)]}
            )
            assert len(many["songs"]) == 100

            # Only unreadable IDs: unknown songs, not an outage; a real outage is an error.
            none = await call(http, token, "get_song_facts", {"song_ids": ["nope"]})
            assert none["ok"] and none["songs"] == [] and none["unreadable_song_ids"] == ["nope"]
            library.down = True
            down = await call(http, token, "get_song_facts", {"song_ids": ["s1"]})
            assert not down["ok"]

            library.down, sound.fail = False, True
            degraded = await call(http, token, "get_song_facts", {"song_ids": ["s1"]})
            assert degraded["ok"] and degraded["sound_analysis"] == "unavailable"
            assert degraded["songs"][0]["year"] == 2001 and "sound" not in degraded["songs"][0]
            app.service.sound = None
            plain = await call(http, token, "get_song_facts", {"song_ids": ["s1"]})
            assert plain["ok"] and plain["sound_analysis"] == "not_configured"

    anyio.run(run)


def test_playlist_profile_summarizes_sound_and_survives_missing_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = Library(), FakeSound()
            for ident, fields in {
                "a": {"year": 1990, "genres": [{"name": "Rock"}, {"name": "Pop"}], "bpm": 100},
                "b": {"year": 2000, "genres": [{"name": "rock"}], "bpm": 120},
                "c": {"year": 2010, "genre": "Jazz"},
                "d": {"year": 2020, "genre": "Pop"},
                "e": {},
            }.items():
                library.songs[ident] = song(ident, ident.upper(), **fields)
            library.playlists["p"] = ("Mixed", list("abcde"))
            sound.data = {
                "a": SongSound(tempo=100.0, energy=10, features={"danceable": 20}, labels=["rock"]),
                "b": SongSound(tempo=120.0, energy=30, features={"danceable": 60}, labels=["rock"]),
                "c": SongSound(tempo=90.5, energy=80, labels=["jazz"]),
            }
            app.service.client.call, app.service.sound = library, sound
            token = await connect(app, http)

            profile = await call(
                http, token, "get_playlist_profile", {"playlist_id": "p", "limit": 3}
            )
            assert profile["playlist"] == {
                "id": "p",
                "name": "Mixed",
                "songCount": 5,
                "duration": 300,
            }
            assert profile["sound_analysis"] == "ok"
            assert [s["id"] for s in profile["songs"]] == ["a", "b", "c"]
            assert profile["songs"][0]["sound"]["energy"] == 10
            digest = profile["summary"]
            assert digest["songs"] == 5 and digest["playlist_songs"] == 5
            assert digest["with_sound_facts"] == 3
            assert digest["year"] == {"songs": 4, "min": 1990, "median": 2005, "max": 2020}
            assert digest["sound"]["energy"] == {"songs": 3, "min": 10, "median": 30, "max": 80}
            assert digest["sound"]["tempo"]["median"] == 100
            assert digest["genres"][0] == {"name": "Pop", "songs": 2}
            assert sound.used("facts") == [list("abcde")]

            # A long playlist is summarized from its first songs and says how long it is.
            monkeypatch.setattr(tools, "PROFILE_SONGS", 2)
            short = await call(http, token, "get_playlist_profile", {"playlist_id": "p"})
            monkeypatch.setattr(tools, "PROFILE_SONGS", 500)
            assert short["summary"]["songs"] == 2 and short["summary"]["playlist_songs"] == 5

            app.service.sound = None
            plain = await call(http, token, "get_playlist_profile", {"playlist_id": "p"})
            assert plain["ok"] and plain["sound_analysis"] == "not_configured"
            assert plain["summary"]["with_sound_facts"] == 0 and "sound" not in plain["summary"]
            assert plain["summary"]["year"]["median"] == 2005

            for bad in (
                {"playlist_id": ""},
                {"playlist_id": "p", "limit": 0},
                {"playlist_id": "x" * 201},
            ):
                refused = await call(http, token, "get_playlist_profile", bad)
                assert not refused["ok"]
            absent = await call(http, token, "get_playlist_profile", {"playlist_id": "nope"})
            assert not absent["ok"]
            library.owner = "someone-else"
            foreign = await call(http, token, "get_playlist_profile", {"playlist_id": "p"})
            assert not foreign["ok"] and "songs" not in foreign

    anyio.run(run)


def test_sound_tools_without_setup_or_with_a_failing_analyzer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = {
        "find_similar_songs": {"seed_song_ids": ["s1"]},
        "search_by_sound": {"text": "slow piano"},
        "find_sound_path": {"start_song_id": "s1", "end_song_id": "s2"},
    }

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            app.service.client.call = library
            token = await connect(app, http)
            for name, args in arguments.items():
                unset = await call(http, token, name, args)
                assert not unset["ok"] and unset["error"]["code"] == "setup_required", name
                assert unset["error"]["missing_configuration"] == ["sound_analysis"]
                # Whoever reads it is pointed to the Sound analysis page, which only
                # administrators use.
                assert "AudioMuse-AI" in unset["error"]["message"]
                assert "administrator" in unset["error"]["message"]
                assert "Sound analysis page" in unset["error"]["message"]
                assert unset["error"]["setup_url"] == BASE + "/shijhon/sound"
            app.service.sound = sound
            sound.fail = True
            for name, args in arguments.items():
                failed = await call(http, token, name, args)
                assert not failed["ok"], name
                assert failed["error"]["code"] == "temporarily_unavailable"
                assert failed["error"]["feature"] == "sound_analysis"
                assert "own musical knowledge" in failed["error"]["message"]
            sound.fail, sound.delay = False, 5
            monkeypatch.setattr(tools, "SOUND_SECONDS", 0.05)
            for name, args in arguments.items():
                slow = await call(http, token, name, args)
                assert slow["error"]["code"] == "temporarily_unavailable", name

    anyio.run(run)


def test_sound_tools_need_library_permission_before_touching_anything(tmp_path: Path) -> None:
    calls = {
        "get_song_facts": {"song_ids": ["s1"]},
        "get_playlist_profile": {"playlist_id": "p"},
        "find_similar_songs": {"seed_song_ids": ["s1"]},
        "search_by_sound": {"text": "slow piano"},
        "find_sound_path": {"start_song_id": "s1", "end_song_id": "s2"},
    }

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            app.service.client.call, app.service.sound = library, sound
            token = await connect(app, http, "create status")
            for name, args in calls.items():
                denied = await call(http, token, name, args)
                assert denied["error"]["code"] == "permission_required", name
                assert denied["error"]["missing_permissions"] == ["library"]
            assert library.calls == [] and sound.calls == []

    anyio.run(run)


def test_similar_songs_from_seeds_and_a_playlist(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            library.playlists["p"] = ("Evening", ["s1", "s2"])
            sound.data = {"s5": SongSound(energy=80), "s6": SongSound(energy=60)}
            sound.neighbours = ["s5", "s1", "s6", "s3", "s7", "s8"]
            app.service.client.call, app.service.sound = library, sound
            token = await connect(app, http)

            result = await call(
                http,
                token,
                "find_similar_songs",
                {
                    "seed_song_ids": ["s4", "missing"],
                    "playlist_id": "p",
                    "avoid_song_ids": ["s3"],
                    "count": 3,
                },
            )
            assert result["ok"], result
            # The playlist's own songs, the seeds and avoided songs are not suggestions.
            assert [s["id"] for s in result["songs"]] == ["s5", "s6", "s7"]
            assert result["songs"][0]["title"] == "Lantern 5"
            assert result["songs"][0]["artist"] == "Glass Orchard"
            assert result["songs"][0]["sound"] == {"energy": 80}
            assert "sound" not in result["songs"][2]
            assert [s["id"] for s in result["seeds"]] == ["s4"]
            assert result["unreadable_seed_ids"] == ["missing"]
            assert result["playlist_seed_songs"] == 2
            seeds, avoid, count = sound.used("similar")[0]
            assert seeds == ["s4", "s1", "s2"] and avoid == ["s3"] and count == 5
            # An unreadable song suggested by the analyzer is reported, not shown.
            sound.neighbours = ["s5", "s1", "ghost", "s6", "s3", "s7", "s8"]
            wide = await call(
                http, token, "find_similar_songs", {"seed_song_ids": ["s4"], "count": 100}
            )
            assert [s["id"] for s in wide["songs"]] == ["s5", "s1", "s6", "s3", "s7", "s8"]
            assert wide["unreadable_song_ids"] == ["ghost"]
            assert sound.used("similar")[1] == (["s4"], [], 100)

    anyio.run(run)


def test_similar_songs_boundaries(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            library.playlists["p"] = ("Evening", ["s1", "s2"])
            library.playlists["empty"] = ("Empty", [])
            app.service.client.call, app.service.sound = library, sound
            token = await connect(app, http)
            bad: list[dict[str, Any]] = [
                {},
                {"seed_song_ids": [f"s{i}" for i in range(21)]},
                {"seed_song_ids": ["s1"], "avoid_song_ids": [f"s{i}" for i in range(21)]},
                {"seed_song_ids": ["s1"], "avoid_song_ids": ["s1"]},
                {"seed_song_ids": ["s1"], "count": 0},
                {"seed_song_ids": ["s1"], "count": 101},
                {"playlist_id": "empty"},
                {"playlist_id": "x" * 201},
            ]
            for args in bad:
                refused = await call(http, token, "find_similar_songs", args)
                assert not refused["ok"], args
            gone = await call(http, token, "find_similar_songs", {"seed_song_ids": ["nothing"]})
            assert gone["error"]["code"] == "not_found"
            library.owner = "someone-else"
            foreign = await call(http, token, "find_similar_songs", {"playlist_id": "p"})
            assert not foreign["ok"]
            assert sound.used("similar") == []
            # Twenty seeds and twenty avoided songs are allowed.
            library.owner = "owner"
            library.songs.update({f"t{i}": song(f"t{i}", f"T{i}") for i in range(20)})
            sound.neighbours = ["s8"]
            fine = await call(
                http,
                token,
                "find_similar_songs",
                {
                    "seed_song_ids": [f"t{i}" for i in range(20)],
                    "avoid_song_ids": [f"a{i}" for i in range(20)],
                },
            )
            assert fine["ok"] and [s["id"] for s in fine["songs"]] == ["s8"]

    anyio.run(run)


def test_search_by_sound_and_sound_path(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            sound.data = {"s2": SongSound(tempo=88.0), "s4": SongSound(energy=40)}
            sound.matches = ["s4", "s2", "s6"]
            sound.route = ["s1", "s4", "s2", "s3"]
            app.service.client.call, app.service.sound = library, sound
            token = await connect(app, http)

            found = await call(
                http,
                token,
                "search_by_sound",
                {
                    "text": " slow dreamy piano ",
                    "more": ["energetic"],
                    "less": ["acoustic"],
                    "count": 2,
                },
            )
            assert [s["id"] for s in found["songs"]] == ["s4", "s2"]
            assert found["songs"][0]["sound"] == {"energy": 40}
            assert sound.used("search") == [("slow dreamy piano", ["energetic"], ["acoustic"], 2)]

            route = await call(
                http,
                token,
                "find_sound_path",
                {"start_song_id": "s1", "end_song_id": "s3", "steps": 4},
            )
            assert [s["id"] for s in route["songs"]] == ["s1", "s4", "s2", "s3"]
            assert route["songs"][2]["sound"] == {"tempo": 88.0}
            assert sound.used("path") == [("s1", "s3", 4)]

            sound.route = ["s1", "s4", "ghost", "s3"]
            holes = await call(
                http, token, "find_sound_path", {"start_song_id": "s1", "end_song_id": "s3"}
            )
            assert [s["id"] for s in holes["songs"]] == ["s1", "s4", "s3"]
            assert holes["unreadable_song_ids"] == ["ghost"]

            for name, args in [
                ("search_by_sound", {"text": ""}),
                ("search_by_sound", {"text": "  "}),
                ("search_by_sound", {"text": "x" * 301}),
                ("search_by_sound", {"text": "piano", "more": ["x"] * 11}),
                ("search_by_sound", {"text": "piano", "less": ["y" * 101]}),
                ("search_by_sound", {"text": "piano", "more": [" "]}),
                ("search_by_sound", {"text": "piano", "count": 0}),
                ("search_by_sound", {"text": "piano", "count": 101}),
                ("find_sound_path", {"start_song_id": "s1", "end_song_id": "s1"}),
                ("find_sound_path", {"start_song_id": "s1", "end_song_id": "s2", "steps": 1}),
                ("find_sound_path", {"start_song_id": "s1", "end_song_id": "s2", "steps": 31}),
                ("find_sound_path", {"start_song_id": "", "end_song_id": "s2"}),
            ]:
                refused = await call(http, token, name, args)
                assert not refused["ok"], (name, args)
            before = len(sound.used("path"))
            unknown = await call(
                http, token, "find_sound_path", {"start_song_id": "s1", "end_song_id": "nope"}
            )
            assert unknown["error"]["code"] == "not_found"
            assert len(sound.used("path")) == before and len(sound.used("search")) == 1

    anyio.run(run)


def test_capabilities_report_sound_analysis_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_tools = ["find_similar_songs", "search_by_sound", "find_sound_path"]

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            library, sound = library_with_songs(), FakeSound()
            app.service.client.call = library
            token = await connect(app, http)

            unset = await call(http, token, "get_capabilities")
            facts = unset["features"]["song_facts"]
            assert facts["ready"] and facts["tools"] == ["get_song_facts", "get_playlist_profile"]
            feature = unset["features"]["sound_analysis"]
            assert not feature["configured"] and not feature["ready"] and not feature["reachable"]
            assert feature["permitted"] and feature["tools"] == new_tools
            assert "AudioMuse-AI" in feature["missing_configuration"][0]
            assert "Sound analysis page" in feature["missing_configuration"][0]
            assert feature["setup_url"] == BASE + "/shijhon/sound"
            assert unset["limits"]["sound_results"] == 100
            assert unset["limits"]["song_facts_songs"] == 100

            app.service.sound = sound
            ready = (await call(http, token, "get_capabilities"))["features"]["sound_analysis"]
            assert ready["configured"] and ready["reachable"] and ready["ready"]
            assert ready["analyzed_songs"] == 3 and ready["missing_configuration"] == []

            sound.status_answer = SoundStatus(configured=True, reachable=False, error="secret")
            away = (await call(http, token, "get_capabilities"))["features"]["sound_analysis"]
            assert away["configured"] and not away["reachable"] and not away["ready"]
            assert "secret" not in json.dumps(away)

            sound.fail = True
            broken = (await call(http, token, "get_capabilities"))["features"]["sound_analysis"]
            assert broken["configured"] and not broken["reachable"] and not broken["ready"]
            sound.fail, sound.delay = False, 5
            monkeypatch.setattr(tools, "STATUS_SECONDS", 0.05)
            slow = await call(http, token, "get_capabilities")
            assert slow["ok"] and not slow["features"]["sound_analysis"]["reachable"]

            narrow = await connect(app, http, "create status")
            limited = (await call(http, narrow, "get_capabilities"))["features"]
            assert limited["sound_analysis"]["missing_permissions"] == ["library"]
            assert not limited["sound_analysis"]["permitted"] and not limited["song_facts"]["ready"]

    anyio.run(run)


def test_choosing_references_needs_the_library_permission_and_asks_for_it(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            narrow = await connect(app, http, "create status")
            wide = await connect(app, http, "create status library")
            for field in ({"reference_playlists": ["p"]}, {"reference_songs": ["s1"]}):
                args = {
                    "idempotency_key": "reference-scope",
                    "request": {"prompt": "like my Evening mix, but livelier", **field},
                }
                refused = await call(http, narrow, "create_playlist", args)
                assert refused["error"]["code"] == "permission_required", field
                assert refused["error"]["missing_permissions"] == ["library"]
                # With it, the next missing thing is reported (here, the server AI).
                allowed = await call(http, wide, "create_playlist", args)
                assert allowed["error"]["code"] == "setup_required", field
            # Naming a playlist in the prompt alone is no library read by the agent.
            named = {
                "idempotency_key": "named-reference",
                "request": {"prompt": "like my Evening mix, but livelier"},
            }
            assert (await call(http, narrow, "create_playlist", named))["error"]["code"] == (
                "setup_required"
            )

    anyio.run(run)


def test_tools_are_registered_with_instructions_about_hints_and_references(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            token = await connect(app, http)
            reply = await http.post(
                "/shijhon/mcp",
                headers={
                    "authorization": "Bearer " + token,
                    "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            listed = {t["name"]: t for t in reply.json()["result"]["tools"]}
            for name in (
                "get_song_facts",
                "get_playlist_profile",
                "find_similar_songs",
                "search_by_sound",
                "find_sound_path",
            ):
                assert listed[name]["annotations"]["readOnlyHint"], name
            text = listed["create_playlist"]["description"]
            assert "reference_playlists" in text and "reference_songs" in text
            assert "own musical knowledge" in text
            assert "include_sound" in json.dumps(listed["search_library"]["inputSchema"])
            assert "include_sound" in json.dumps(listed["get_native_playlists"]["inputSchema"])
            instructions = app.mcp.instructions or ""
            assert "reference_playlists" in instructions and "musical knowledge" in instructions

    anyio.run(run)
