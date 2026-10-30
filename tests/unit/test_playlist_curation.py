"""Described playlists with references and hints: invented music, a scripted model, a fake
sound analyzer and a fake Last.fm; no accounts, inference or network."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from pydantic import ValidationError

from shijhon.playlists import facts as facts_module
from shijhon.playlists.model import (
    LIBRARY_LOST,
    Connection,
    Definition,
    PlaylistError,
    Track,
)
from shijhon.playlists.prompt import (
    NamedOrdered,
    Ordered,
    Proposals,
    SoundProposals,
    generate,
)
from shijhon.playlists.references import detect
from shijhon.playlists.requests import PlaylistRequest, RequestError, Requests, digest
from shijhon.playlists.service import Playlists
from shijhon.playlists.sound import SongSound, SoundStatus, SoundUnavailable
from tests.harness.agents import agent_token
from tests.unit.test_playlist_requests import connected, service_at

KEY = "invented-lastfm-key"

# The owner's library, as Navidrome answers it: two reference songs, three others.
SONGS = {
    "ref-1": {
        "artist": "Glass Orchard",
        "title": "First Lantern",
        "year": 2019,
        "genre": "Ambient",
    },
    "ref-2": {"artist": "Quill & Marrow", "title": "Night Light", "year": 2016},
    "own-bright": {"artist": "Ember Lane", "title": "Bright Hours", "year": 2021},
    "added-run": {"artist": "Copper Fields", "title": "Morning Run"},
    "own-hum": {"artist": "Velvet Static", "title": "Neon Hum"},
    "sound-1": {"artist": "Tin Harbor", "title": "Spark Drill", "year": 2018},
    "sound-2": {"artist": "Moss Atlas", "title": "Lift Off"},
    "sound-3": {"artist": "Rune Delta", "title": "Quick Steps"},
}
CATALOG = Track(id="sh.tr.demo.neon", artist="Harbor Kite Club", title="Neon Avenue", year=2022)
PLAYLISTS = [
    {"id": "pl-orbit", "name": "Velvet Orbit", "owner": "owner", "changed": "2026-01-02"},
    {"id": "pl-old", "name": "velvet orbit", "owner": "owner", "changed": "2025-01-02"},
    {"id": "pl-chill", "name": "Chill Mix", "owner": "owner"},
    {"id": "pl-word", "name": "Driftwood", "owner": "owner"},
    {"id": "pl-other", "name": "Borrowed Rooms", "owner": "someone-else"},
]
ENTRIES = {"pl-orbit": ["ref-1", "ref-2"], "pl-old": ["own-hum"]}


def song(ident: str) -> dict[str, Any]:
    return {"id": ident, "album": "Invented album", **SONGS[ident]}


class FakeSound:
    """The sound interface as curation uses it, scripted per test."""

    def __init__(
        self,
        facts: dict[str, SongSound] | None = None,
        *,
        similar: Sequence[str] = (),
        search: Sequence[str] = (),
        fail: bool = False,
        slow: bool = False,
        live_fail: bool = False,
    ) -> None:
        self.known = facts or {}
        self.similar_ids, self.search_ids = list(similar), list(search)
        self.fail, self.slow, self.live_fail = fail, slow, live_fail
        self.calls: list[tuple[str, Any]] = []

    async def answer(self, call: str, value: Any) -> None:
        self.calls.append((call, value))
        if self.fail or (self.live_fail and call != "facts"):
            raise SoundUnavailable
        if self.slow:
            await anyio.sleep(30)

    async def facts(self, song_ids: Sequence[str]) -> dict[str, SongSound]:
        await self.answer("facts", list(song_ids))
        return {i: f for i, f in self.known.items() if i in song_ids}

    async def similar(
        self, seeds: Sequence[str], *, avoid: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        await self.answer("similar", (list(seeds), count))
        return self.similar_ids[:count]

    async def search(
        self, text: str, *, more: Sequence[str] = (), less: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        await self.answer("search", (text, list(more), list(less), count))
        return self.search_ids[:count]

    async def path(self, start: str, end: str, *, steps: int = 12) -> list[str]:
        return []

    async def status(self) -> SoundStatus:
        return SoundStatus(configured=True, reachable=True)


class LastFm:
    """Community tags by artist and title; counts every request and the most at once."""

    def __init__(self, tags: dict[tuple[str, str], list[str]], *, status: int = 200) -> None:
        self.tags, self.status = tags, status
        self.requests: list[tuple[str, str, str]] = []
        self.active = self.most = 0
        self.slow = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["api_key"] == KEY and "user" not in params
        method, artist, title = params["method"], params["artist"], params.get("track", "")
        self.requests.append((method, artist, title))
        self.active += 1
        self.most = max(self.most, self.active)
        try:
            await anyio.sleep(30 if self.slow else 0.01)
        finally:
            self.active -= 1
        if self.status != 200:
            return httpx.Response(self.status, json={"error": 11, "message": "Unavailable"})
        found = self.tags.get((artist, title))
        if found is None:
            return httpx.Response(200, json={"error": 6, "message": "Not found"})
        tags = [{"name": t, "count": 100 - 10 * i} for i, t in enumerate(found)]
        return httpx.Response(200, json={"toptags": {"tag": tags, "@attr": {"artist": artist}}})


@asynccontextmanager
async def curation(
    path: Path,
    *,
    sound: FakeSound | None = None,
    lastfm: LastFm | None = None,
) -> AsyncIterator[tuple[Playlists, list[tuple[str, dict[str, str]]]]]:
    """A service with the invented library, its playlists and placeholders."""
    async with service_at(path) as service:
        await service.repo.connect(
            connected(
                backend="api",
                endpoint="https://model.invalid/v1",
                model="synthetic",
                history_key=KEY if lastfm else "",
            )
        )
        calls: list[tuple[str, dict[str, str]]] = []

        async def native(connection: Connection, method: str, params: Any = ()) -> Any:
            values = dict(params)
            calls.append((method, values))
            if method == "getPlaylists":
                return {"playlists": {"playlist": [p.copy() for p in PLAYLISTS]}}
            if method == "getPlaylist":
                found = next((p for p in PLAYLISTS if p["id"] == values["id"]), None)
                if found is None or found["owner"] != connection.username:
                    raise PlaylistError("Navidrome could not complete the request.")
                entries = [song(i) for i in ENTRIES.get(values["id"], [])]
                return {"playlist": {**found, "entry": entries}}
            if method == "getSong":
                if values["id"] not in SONGS:
                    raise PlaylistError("Navidrome could not complete the request.")
                return {"song": song(values["id"])}
            raise AssertionError(method)

        async def resolve(connection: Connection, wanted: dict[str, Any]) -> Track | None:
            if wanted["title"] == CATALOG.title:
                return CATALOG.model_copy(update={"reason": wanted["reason"]})
            ident = next((i for i, s in SONGS.items() if s["title"] == wanted["title"]), None)
            return (
                None
                if ident is None
                else Track.model_validate({k: v for k, v in song(ident).items() if k != "genre"})
            )

        async def native_track(connection: Connection, ident: str, **kwargs: Any) -> Track:
            if ident not in SONGS:
                raise PlaylistError("A selected recording is no longer in this library.")
            return Track.model_validate({k: v for k, v in song(ident).items() if k != "genre"})

        service.client.call = native
        service.resolver.resolve = resolve
        service.native_track = native_track  # type: ignore[method-assign]
        service.sound = sound

        async def library_context(owner: str, limit: int) -> list[dict[str, str]]:
            return []  # The library-context sample; sound pools need its opt-in.

        service.library_context = library_context
        if lastfm:
            await service.history.http.aclose()
            service.history.http = httpx.AsyncClient(transport=httpx.MockTransport(lastfm))
        store = service.repo.store
        await store.execute(
            "INSERT INTO releases(ref,folder,album_id,title,artist,album_tags,data,created_at)"
            " VALUES ('demo:run','Copper Fields/Run','album','Run','Copper Fields','{}','{}',0)"
        )
        await store.execute(
            "INSERT INTO placeholders(song_id,path,placeholder_path,track_ref,release_ref,"
            "title,artist,album,duration_ms,disc,track,tags,created_at) VALUES"
            " ('added-run','p.flac','p.flac','demo:t','demo:run','Morning Run',"
            "'Copper Fields','Run',1000,1,1,'{}',0)"
        )
        yield service, calls


def proposals(*titles: str, **extra: Any) -> dict[str, Any]:
    """Suggestions by title; ``sound_search`` belongs to answers with sound analysis."""
    by_title = {s["title"]: s["artist"] for s in SONGS.values()} | {CATALOG.title: CATALOG.artist}
    tracks = [{"artist": by_title[t], "title": t, "album": "", "version": ""} for t in titles]
    return {"tracks": tracks, **extra}


def picks(payload: dict[str, Any], titles: Sequence[str], fits: Sequence[str] = ()) -> list:
    handles = {c["title"]: c["id"] for c in payload["candidates"]}
    return [
        {"id": handles[t], "reason": f"{t} fits", "fit": fits[i] if fits else "meets"}
        for i, t in enumerate(titles)
    ]


def described(prompt: str, count: int = 2, **kwargs: Any) -> Definition:
    return PlaylistRequest(prompt=prompt, count=count, max_per_artist=1, **kwargs).definition()


def test_detection_takes_owned_names_written_in_the_instructions() -> None:
    owned = [
        {"id": "a", "name": "Velvet Orbit", "changed": "2026"},
        {"id": "b", "name": "Orbit", "changed": "2026"},
        {"id": "c", "name": "Night Drive"},
        {"id": "d", "name": "Chill Mix"},
        {"id": "e", "name": "Driftwood"},
        {"id": "f", "name": "Ok"},
        {"id": "g", "name": "VELVET  orbit", "changed": "2027"},
        {"id": "h", "name": "Paper Rooms"},
        {"id": "i", "name": "Glass Hours"},
    ]
    names = [
        p["id"]
        for p in detect(
            "A companion to velvet orbit, closer to PAPER ROOMS, for a night drive, ok?",
            owned,
        )
    ]
    # Longest first, the shorter name inside it not counted; the most recent of equal names.
    assert names == ["g", "h"]
    # Short names and names of only general words are not taken for references.
    assert detect("chill mix for a night drive, ok", owned) == []
    assert [p["id"] for p in detect("bright and driftwood songs", owned)] == []
    assert [p["id"] for p in detect('more like "Driftwood", please', owned)] == ["e"]
    assert [p["id"] for p in detect("my driftwood playlist, louder", owned)] == ["e"]
    assert [p["id"] for p in detect("velvetorbit", owned)] == []
    many = "Velvet Orbit, Night Drive, Paper Rooms and Glass Hours"
    assert len(detect(many, owned)) == 3


def test_reference_fields_and_old_inputs_keep_their_replay_hash(tmp_path: Path) -> None:
    for data in (
        {"prompt": "x", "reference_playlists": ["a", "b", "c", "d"]},
        {"prompt": "x", "reference_songs": ["s"] * 2},
        {"prompt": "x", "reference_playlists": ["../a"]},
        {"prompt": "x", "exclude_reference": "false"},
        {"count": 1, "tracks": [{"artist": "A", "title": "B"}], "reference_songs": ["s"]},
    ):
        with pytest.raises(ValidationError):
            PlaylistRequest.model_validate(data)
    with pytest.raises(ValidationError):
        Definition(reference_playlists=["pl"])  # discovery mixes have no references
    old = Definition.model_validate(
        {"name": "Old", "mode": "prompt", "brief": "x", "ai": True, "cadence": "manual"}
    )
    assert old.reference_playlists == [] and old.exclude_reference
    spec = PlaylistRequest(prompt="x", reference_playlists=["pl"], exclude_reference=False)
    definition = spec.definition()
    assert definition.reference_playlists == ["pl"] and not definition.exclude_reference
    assert json.loads(spec.canonical())["reference_playlists"] == ["pl"]

    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            # The canonical shape saved before references existed.
            historical = {
                "name": "Described playlist",
                "prompt": "quiet",
                "count": 1,
                "max_per_artist": 2,
                "use_history": False,
                "use_library": False,
                "tracks": [],
                "save_instructions": False,
                "cadence": "manual",
                "preview": False,
            }
            canonical = json.dumps(historical, sort_keys=True, ensure_ascii=False)
            assert PlaylistRequest.model_validate(historical).canonical() == canonical
            ident, _ = await service.requests.create(
                "owner", "historical-input", PlaylistRequest.model_validate(historical)
            )
            row = await service.repo.store.fetchone(
                "SELECT * FROM playlist_requests WHERE id=?", [ident]
            )
            assert row["input"] == canonical and row["input_hash"] == digest(canonical)
            same = PlaylistRequest(prompt="quiet", count=1, exclude_reference=True)
            assert await service.requests.create("owner", "historical-input", same) == (
                ident,
                False,
            )
            changed = PlaylistRequest(prompt="quiet", count=1, reference_songs=["ref-1"])
            with pytest.raises(PlaylistError, match="different input"):
                await service.requests.create("owner", "historical-input", changed)
            referenced, _ = await service.requests.create("owner", "referenced", changed)
            assert await service.requests.create("owner", "referenced", changed) == (
                referenced,
                False,
            )

    anyio.run(run)


def strict(schema: dict[str, Any], node: Any = None) -> None:
    """Every object closed with all properties required; nothing optional or defaulted."""
    node = schema if node is None else node
    if isinstance(node, dict):
        assert "default" not in node
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, node
            assert sorted(node.get("required", [])) == sorted(node["properties"]), node
        for value in node.values():
            strict(schema, value)
    elif isinstance(node, list):
        for value in node:
            strict(schema, value)


@pytest.mark.parametrize("schema", [Proposals, SoundProposals, Ordered, NamedOrdered])
def test_curation_schemas_suit_strict_structured_output(schema: type) -> None:
    strict(schema.model_json_schema())


def test_overlong_or_odd_hints_from_the_model_are_shortened_not_refused() -> None:
    track = {"artist": "Ember Lane", "title": "Bright Hours", "album": "", "version": ""}
    words = ["w" * 60, "bad\nword", *[f"word{i}" for i in range(8)]]
    answer = SoundProposals.model_validate(
        {"tracks": [track], "sound_search": {"text": "x " * 400, "more": words, "less": []}}
    )
    assert answer.sound_search is not None
    assert len(answer.sound_search.text) == 300
    assert answer.sound_search.more == ["w" * 40, "bad word", "word0", "word1", "word2"]
    assert SoundProposals.model_validate({"tracks": [track], "sound_search": None})
    ordered = Ordered.model_validate(
        {
            "goal": "g" * 400,
            "constraints_satisfied": True,
            "selections": [{"id": "candidate-0", "reason": "", "fit": "partly"}],
        }
    )
    assert len(ordered.goal) == 300
    with pytest.raises(ValidationError):  # fit is one of three words
        Ordered.model_validate(
            {
                "goal": "",
                "constraints_satisfied": True,
                "selections": [{"id": "candidate-0", "reason": "", "fit": "maybe"}],
            }
        )


def test_references_reach_both_prompts_with_facts_and_their_songs_are_left_out(
    tmp_path: Path,
) -> None:
    sound = FakeSound(
        {
            "ref-1": SongSound(tempo=92.4, key="A minor", energy=30, features={"happy": 40}),
            "ref-2": SongSound(tempo=88, energy=20, features={"happy": 20}, labels=["ambient"]),
            "own-bright": SongSound(tempo=128, energy=85, features={"happy": 90}),
        }
    )
    lastfm = LastFm(
        {
            ("Glass Orchard", "First Lantern"): ["ambient", "glass orchard", "seen live"],
            ("Quill & Marrow", ""): ["dream pop"],
            ("Ember Lane", "Bright Hours"): ["synthpop", "upbeat"],
        }
    )

    async def run() -> None:
        async with curation(tmp_path / "db", sound=sound, lastfm=lastfm) as (service, calls):
            prompts: list[tuple[type, dict[str, Any], str]] = []

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                prompts.append((schema, payload, kw["instructions"]))
                if schema is SoundProposals:
                    return proposals(
                        "First Lantern",
                        "Bright Hours",
                        "Morning Run",
                        "Neon Avenue",
                        sound_search=None,
                    )
                return {
                    "goal": "Livelier than Velvet Orbit, keeping its glassy textures",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
                }

            service.curator.api = ai
            definition = described(
                "A companion to Velvet Orbit but more energetic", use_library=True
            )
            curated = await generate(service, await service.connection("owner"), definition)
            assert [s.id for s in curated.selected] == ["own-bright", CATALOG.id]
            (_, propose, instructions), (_, order, order_instructions) = prompts
            reference = propose["reference"]
            assert reference["playlists"] == [{"name": "Velvet Orbit", "detected": True}]
            assert reference["leave_out_reference_songs"] is True
            first = reference["songs"][0]
            assert first == {
                "artist": "Glass Orchard",
                "title": "First Lantern",
                "album": "Invented album",
                "year": 2019,
                "genres": ["Ambient"],
                "tags": ["ambient"],
                "sound": {"tempo": 92, "key": "A minor", "energy": 30, "features": {"happy": 40}},
                "source": "own",
            }
            assert reference["songs"][1]["tags"] == ["dream pop"]  # the artist's tags
            assert reference["profile"] == {
                "songs": 2,
                "with_sound": 2,
                "energy": 25,
                "tempo": 90,
                "features": {"happy": 30},
                "tags": ["ambient", "dream pop"],
                "genres": ["ambient"],
                "years": [2016, 2019],
            }
            assert "never drop a song only because facts are missing" in instructions
            assert "sound_search" in instructions
            assert order["reference"] == reference
            candidates = {c["title"]: c for c in order["candidates"]}
            assert "First Lantern" in candidates  # named only: shown to the AI, not removed
            assert candidates["Bright Hours"]["tags"] == ["synthpop", "upbeat"]
            assert candidates["Bright Hours"]["sound"]["energy"] == 85
            assert candidates["Bright Hours"]["source"] == "own"
            assert candidates["Morning Run"]["source"] == "added"
            assert candidates["Neon Avenue"] == {
                "id": candidates["Neon Avenue"]["id"],
                "artist": CATALOG.artist,
                "title": CATALOG.title,
                "album": "",
                "year": 2022,
                "source": "catalog",
            }
            assert "goal" in order_instructions and "fit" in order_instructions
            # The older playlist of the same name and generic or one-word names are not read.
            assert [v["id"] for m, v in calls if m == "getPlaylist"] == ["pl-orbit"]
            # Only the analyzed library songs are asked about; the catalog song is not.
            asked = [ids for call, ids in sound.calls if call == "facts"]
            assert asked[0] == ["ref-1", "ref-2"] and CATALOG.id not in asked[1]
            info = curated.curation
            assert info["references"] == {
                "playlists": [
                    {"id": "pl-orbit", "name": "Velvet Orbit", "songs": 2, "detected": True}
                ],
                "songs": 0,
                "shown": 2,
                "exclude": True,
            }
            assert info["goal"].startswith("Livelier") and info["revision"] == "none"
            assert info["left_out"] == 0
            assert info["songs"]["own-bright"] == {
                "fit": "meets",
                "source": "own",
                "facts": {
                    "year": 2021,
                    "tags": ["synthpop", "upbeat"],
                    "sound": {"tempo": 128, "energy": 85, "features": {"happy": 90}},
                },
            }
            assert info["songs"][CATALOG.id]["source"] == "catalog"
            assert info["coverage"] == {
                "songs": 2,
                "with_sound": 1,
                "with_tags": 1,
                "sound": "used",
                "tags": "used",
            }
            assert info["reference_profile"] == reference["profile"]

    anyio.run(run)


def test_only_chosen_reference_playlists_leave_their_songs_out(tmp_path: Path) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, _):
            orders: list[dict[str, Any]] = []

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                if schema is Proposals:
                    return proposals("First Lantern", "Bright Hours", "Neon Avenue")
                orders.append(payload)
                return {
                    "goal": "Livelier",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
                }

            service.curator.api = ai
            curated = await generate(
                service,
                await service.connection("owner"),
                described("Livelier", reference_playlists=["pl-orbit"]),
            )
            assert "First Lantern" not in {c["title"] for c in orders[0]["candidates"]}
            assert curated.curation["left_out"] == 1
            assert orders[0]["reference"]["playlists"] == [
                {"name": "Velvet Orbit", "detected": False}
            ]

    anyio.run(run)


def test_explicit_references_win_and_unreadable_ones_fail_clearly(tmp_path: Path) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, calls):
            payloads: list[dict[str, Any]] = []

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                payloads.append(payload)
                if schema is Proposals:
                    return proposals("First Lantern", "Bright Hours", "Neon Avenue")
                return {
                    "goal": "Keeps a reference song",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["First Lantern", "Bright Hours"]),
                }

            service.curator.api = ai
            connection = await service.connection("owner")
            # Explicit references: names in the instructions are not looked up.
            definition = described(
                "Like Velvet Orbit", reference_songs=["ref-1"], exclude_reference=False
            )
            curated = await generate(service, connection, definition)
            assert [m for m, _ in calls] == ["getSong"]
            assert [s.id for s in curated.selected] == ["ref-1", "own-bright"]
            reference = payloads[0]["reference"]
            assert reference["playlists"] == [] and reference["leave_out_reference_songs"] is False
            assert [s["title"] for s in reference["songs"]] == ["First Lantern"]
            assert curated.curation["references"]["songs"] == 1
            assert curated.curation["left_out"] == 0
            for definition, message in (
                (described("x", reference_playlists=["pl-other"]), 'playlist "pl-other"'),
                (described("x", reference_playlists=["missing"]), 'playlist "missing"'),
                (described("x", reference_songs=["ref-2", "gone"]), 'song "gone"'),
            ):
                with pytest.raises(PlaylistError, match=message):
                    await generate(service, connection, definition)

    anyio.run(run)


def test_unreadable_detected_reference_is_skipped_and_own_playlist_is_not_a_reference(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, calls):
            payloads: list[dict[str, Any]] = []

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                payloads.append(payload)
                if schema is Proposals:
                    return proposals("Bright Hours", "Neon Avenue")
                return {
                    "goal": "Bright songs",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
                }

            service.curator.api = ai
            connection = await service.connection("owner")
            # The refreshed playlist itself is never its own detected reference.
            curated = await generate(
                service, connection, described("More Velvet Orbit"), own="pl-orbit"
            )
            assert [v["id"] for m, v in calls if m == "getPlaylist"] == ["pl-old"]
            assert curated.curation["references"]["playlists"][0]["id"] == "pl-old"
            ENTRIES["pl-gone"] = []
            PLAYLISTS.append({"id": "pl-gone", "name": "Vanished Rooms", "owner": "owner"})
            try:
                original = service.client.call

                async def failing(connection: Connection, method: str, params: Any = ()) -> Any:
                    if method == "getPlaylist" and dict(params)["id"] == "pl-gone":
                        raise PlaylistError("Navidrome could not complete the request.")
                    return await original(connection, method, params)

                service.client.call = failing
                payloads.clear()
                curated = await generate(service, connection, described("Like Vanished Rooms"))
                assert "reference" not in payloads[0]
                assert curated.curation["references"]["playlists"] == []
            finally:
                PLAYLISTS.pop()
                del ENTRIES["pl-gone"]

    anyio.run(run)


def test_sound_pools_join_labelled_bounded_and_without_reference_songs(tmp_path: Path) -> None:
    sound = FakeSound(
        similar=["ref-2", "sound-1", "sound-2", "missing", "sound-3"],
        search=["own-bright", "sound-1", "sound-2", "sound-3"],
    )

    async def run() -> None:
        async with curation(tmp_path / "db", sound=sound) as (service, _):
            orders: list[dict[str, Any]] = []
            search = {"text": "fast bright drums", "more": ["energetic"], "less": ["calm"]}

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                if schema is SoundProposals:
                    return proposals("Bright Hours", sound_search=search)
                orders.append(payload)
                return {
                    "goal": "Energetic songs",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Spark Drill", "Quick Steps"]),
                }

            service.curator.api = ai
            curated = await generate(
                service,
                await service.connection("owner"),
                described("Livelier than Velvet Orbit", count=3, use_library=True),
            )
            # Similar songs and search results: about the requested count of new songs
            # each; reference songs, unreadable songs and songs already found left out.
            # The analyzer is asked for the count plus the songs already known (pool and
            # references), so known answers do not crowd out new ones.
            assert ("similar", (["ref-1", "ref-2"], 3 + 3)) in sound.calls
            search_call = ("fast bright drums", ["energetic"], ["calm"], 3 + 5)
            assert ("search", search_call) in sound.calls
            candidates = {c["title"]: c for c in orders[0]["candidates"]}
            assert set(candidates) == {"Bright Hours", "Spark Drill", "Lift Off", "Quick Steps"}
            assert candidates["Spark Drill"]["suggested_by"] == "sound_similar"
            assert candidates["Lift Off"]["suggested_by"] == "sound_similar"
            assert candidates["Quick Steps"]["suggested_by"] == "sound_search"
            assert "suggested_by" not in candidates["Bright Hours"]  # the model's own
            assert curated.curation["sound_pool"] == {"similar": 2, "search": 1}
            info = curated.curation["songs"]["sound-1"]
            assert info["facts"]["suggested_by"] == "sound_similar"

    anyio.run(run)


def test_sound_search_is_only_offered_with_sound_analysis(tmp_path: Path) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, _):
            schemas: list[type] = []

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                schemas.append(schema)
                assert "sound_search" not in kw["instructions"]
                payload = json.loads(prompt)
                if schema is Proposals:
                    return proposals("Bright Hours", "Neon Avenue")
                assert all("sound" not in c for c in payload["candidates"])
                return {
                    "goal": "Bright songs",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
                }

            service.curator.api = ai
            curated = await generate(
                service, await service.connection("owner"), described("Bright songs")
            )
            assert schemas == [Proposals, Ordered]
            assert curated.curation["coverage"]["sound"] == "off"
            assert curated.curation["coverage"]["tags"] == "off"
            assert curated.curation["references"]["playlists"] == []

    anyio.run(run)


@pytest.mark.parametrize("problem", ["unavailable", "slow"])
def test_unavailable_or_slow_hints_never_fail_a_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    monkeypatch.setattr(facts_module, "SOUND_SECONDS", 0.2)
    monkeypatch.setattr(facts_module, "TAG_SECONDS", 0.2)
    monkeypatch.setattr(facts_module, "TAG_BUDGET", 0.5)
    sound = FakeSound(similar=["sound-1"], fail=problem == "unavailable", slow=problem == "slow")
    lastfm = LastFm({}, status=503 if problem == "unavailable" else 200)
    lastfm.slow = problem == "slow"

    async def run() -> None:
        async with curation(tmp_path / "db", sound=sound, lastfm=lastfm) as (service, _):

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                if schema is SoundProposals:
                    search = {"text": "bright", "more": [], "less": []}
                    return proposals("Bright Hours", "Neon Avenue", sound_search=search)
                assert all(
                    set(c) <= {"id", "artist", "title", "album", "year", "source"}
                    for c in payload["candidates"]
                )
                return {
                    "goal": "Bright songs",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
                }

            service.curator.api = ai
            started = time.monotonic()
            curated = await generate(
                service,
                await service.connection("owner"),
                described("Like Velvet Orbit", use_library=True),
            )
            assert time.monotonic() - started < 5
            assert [s.id for s in curated.selected] == ["own-bright", CATALOG.id]
            assert curated.curation["coverage"]["sound"] == "unavailable"
            assert curated.curation["coverage"]["tags"] == "unavailable"
            # After a failure of each kind (its synced facts, its live suggestions), that
            # kind is not asked again in this generation.
            assert [call for call, _ in sound.calls] == ["facts", "similar"]
            cached = await service.repo.store.fetchone("SELECT count(*) AS n FROM music_tags")
            assert cached["n"] == 0  # failures are not remembered as missing tags

    anyio.run(run)


def test_lastfm_tags_are_cached_bounded_and_fall_back_to_the_artist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lastfm = LastFm(
        {
            ("Ember Lane", "Bright Hours"): ["synthpop"],
            ("Velvet Static", "Neon Hum"): [],
            ("Velvet Static", ""): ["shoegaze"],
            ("Copper Fields", ""): ["folk"],
        }
    )

    async def run() -> None:
        async with curation(tmp_path / "db", lastfm=lastfm) as (service, _):
            connection = await service.connection("owner")
            tracks = [
                Track.model_validate({k: v for k, v in song(i).items() if k != "genre"})
                for i in ("own-bright", "own-hum", "added-run")
            ]
            found = facts_module.Facts(service, connection)
            await found.describe(tracks)
            assert found.song_tags == {
                "own-bright": ["synthpop"],
                "own-hum": ["shoegaze"],
                "added-run": ["folk"],
            }
            assert sorted(m for m, _, _ in lastfm.requests) == [
                *["artist.getTopTags"] * 2,
                *["track.getTopTags"] * 3,
            ]
            assert lastfm.most <= facts_module.CONCURRENCY
            # A new generation reads the cache: no requests.
            lastfm.requests.clear()
            again = facts_module.Facts(service, connection)
            await again.describe(tracks)
            assert again.song_tags == found.song_tags and lastfm.requests == []
            rows = await service.repo.store.fetchall("SELECT * FROM music_tags ORDER BY artist")
            assert ("emberlane", "brighthours") in {(r["artist"], r["title"]) for r in rows}
            # Expired entries are asked again.
            await service.repo.store.execute(
                "UPDATE music_tags SET fetched=?", [time.time() - facts_module.TAG_TTL - 1]
            )
            fresh = facts_module.Facts(service, connection)
            await fresh.describe(tracks[:1])
            assert lastfm.requests == [("track.getTopTags", "Ember Lane", "Bright Hours")]
            # The request cap of one generation holds.
            await service.repo.store.execute("DELETE FROM music_tags")
            lastfm.requests.clear()
            capped = facts_module.Facts(service, connection)
            capped.requests = 2
            many = [
                Track(id=f"song-{i}", artist=f"Invented Artist {i}", title=f"Tune {i}")
                for i in range(20)
            ]
            await capped.describe(many)
            assert len(lastfm.requests) == 2 and capped.tag_status == "used"
            # The time budget of a batch stops the paced lookups; Last.fm stays in use.
            monkeypatch.setattr(facts_module, "TAG_BUDGET", 0.3)
            lastfm.requests.clear()
            lastfm.tags.update({(t.artist, t.title): ["dream pop"] for t in many})
            await service.repo.store.execute("DELETE FROM music_tags")
            budgeted = facts_module.Facts(service, connection)
            await budgeted.describe(many)
            assert 0 < len(budgeted.song_tags) < len(many)
            assert budgeted.tag_status == "used"
            remembered = await service.repo.store.fetchone("SELECT count(*) AS n FROM music_tags")
            assert remembered["n"] == len(budgeted.song_tags)

    anyio.run(run)


@pytest.mark.parametrize("outcome", ["revised", "kept", "no_unused", "all_meet"])
def test_flagged_picks_get_one_revision(tmp_path: Path, outcome: str) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, _):
            calls: list[tuple[type, dict[str, Any], str]] = []

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                calls.append((schema, payload, kw["instructions"]))
                if schema is Proposals:
                    titles = ["Bright Hours", "Neon Avenue", "Morning Run"]
                    return proposals(*titles[: 2 if outcome == "no_unused" else 3])
                if "previous_selection" not in payload:
                    fits = ["meets", "meets"] if outcome == "all_meet" else ["meets", "misses"]
                    return {
                        "goal": "First try",
                        "constraints_satisfied": True,
                        "selections": picks(payload, ["Bright Hours", "Neon Avenue"], fits),
                    }
                if outcome == "kept":
                    return {
                        "goal": "Broken",
                        "constraints_satisfied": True,
                        "selections": [{"id": "invented", "reason": "", "fit": "meets"}],
                    }
                handles = {
                    c["title"]: c["id"]
                    for c in [*payload["previous_selection"], *payload["unused_candidates"]]
                }
                return {
                    "goal": "Second try",
                    "constraints_satisfied": True,
                    "selections": [
                        {"id": handles["Bright Hours"], "reason": "Bright", "fit": "meets"},
                        {"id": handles["Morning Run"], "reason": "Close", "fit": "partly"},
                    ],
                }

            service.curator.api = ai
            curated = await generate(
                service, await service.connection("owner"), described("Bright songs")
            )
            revisions = [c for c in calls if "previous_selection" in c[1]]
            assert len(revisions) == (1 if outcome in ("revised", "kept") else 0)
            info = curated.curation
            if outcome == "revised":
                _, payload, instructions = revisions[0]
                assert "flagged" in instructions
                flagged = payload["flagged"]
                assert [
                    p["title"] for p in payload["previous_selection"] if p["id"] in flagged
                ] == ["Neon Avenue"]
                assert [c["title"] for c in payload["unused_candidates"]] == ["Morning Run"]
                assert [s.id for s in curated.selected] == ["own-bright", "added-run"]
                assert curated.selected[1].reason == "Close"
                assert info["goal"] == "Second try" and info["revision"] == "revised"
                assert info["songs"]["added-run"]["fit"] == "partly"  # flags kept
            else:
                assert [s.id for s in curated.selected] == ["own-bright", CATALOG.id]
                assert info["goal"] == "First try"
                assert info["revision"] == ("kept" if outcome == "kept" else "none")
                fit = "meets" if outcome == "all_meet" else "misses"
                assert info["songs"][CATALOG.id]["fit"] == fit

    anyio.run(run)


def test_revision_keeps_automatic_naming_and_validation(tmp_path: Path) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, _):

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                if schema is Proposals:
                    return proposals("Bright Hours", "Neon Avenue", "Morning Run")
                assert schema is NamedOrdered
                if "previous_selection" not in payload:
                    selections = picks(
                        payload, ["Bright Hours", "Neon Avenue"], ["meets", "partly"]
                    )
                    return {
                        "name": "First Title",
                        "goal": "First try",
                        "constraints_satisfied": True,
                        "selections": selections,
                    }
                # The same artist twice breaks the artist limit: the first selection stays.
                handle = payload["previous_selection"][0]["id"]
                return {
                    "name": "Second Title",
                    "goal": "Second try",
                    "constraints_satisfied": True,
                    "selections": [
                        {"id": handle, "reason": "", "fit": "meets"},
                        {"id": handle, "reason": "", "fit": "meets"},
                    ],
                }

            service.curator.api = ai
            curated = await generate(
                service,
                await service.connection("owner"),
                PlaylistRequest(
                    name="", prompt="Bright songs", count=2, max_per_artist=1
                ).definition(),
            )
            assert curated.name == "First Title" and curated.curation["revision"] == "kept"

    anyio.run(run)


def test_run_data_and_request_status_show_the_curation(tmp_path: Path) -> None:
    lastfm = LastFm({("Ember Lane", "Bright Hours"): ["synthpop"]})

    async def run() -> None:
        async with curation(tmp_path / "db", lastfm=lastfm) as (service, _):

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                if schema is Proposals:
                    return proposals("Bright Hours", "Neon Avenue")
                return {
                    "goal": "Brighter than the reference",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
                }

            service.curator.api = ai
            spec = PlaylistRequest(
                prompt="Brighter",
                count=2,
                max_per_artist=1,
                reference_songs=["ref-1"],
                preview=True,
            )
            ident, _ = await service.requests.create("owner", "curation-status", spec)
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.process(run_id)
            frozen = await service.repo.run(run_id)
            assert frozen["state"] == "preview", frozen["error"]
            assert frozen["data"]["curation"]["goal"] == "Brighter than the reference"
            status = await service.requests.status("owner", ident)
            assert status["goal"] == "Brighter than the reference"
            assert status["references"]["songs"] == 1 and status["revision"] == "none"
            assert status["coverage"]["with_tags"] == 1 and status["left_out"] == 0
            assert status["selected"][0] == {
                "artist": "Ember Lane",
                "title": "Bright Hours",
                "album": "Invented album",
                "reason": "Bright Hours fits",
                "fit": "meets",
                "source": "own",
                "facts": {"year": 2021, "tags": ["synthpop"]},
            }
            assert status["selected"][1]["source"] == "catalog"
            encoded = json.dumps(status)
            assert KEY not in encoded and "native-secret" not in encoded

    anyio.run(run)


def test_explicit_tracks_read_no_references_or_hints(tmp_path: Path) -> None:
    lastfm = LastFm({})
    sound = FakeSound()

    async def run() -> None:
        async with curation(tmp_path / "db", sound=sound, lastfm=lastfm) as (service, calls):
            service.curator.api = AsyncMock(side_effect=AssertionError("no model"))
            spec = PlaylistRequest(
                prompt="Like Velvet Orbit",
                count=1,
                tracks=[{"artist": "Ember Lane", "title": "Bright Hours"}],
            )
            curated = await generate(service, await service.connection("owner"), spec.definition())
            assert [s.id for s in curated.selected] == ["own-bright"]
            assert curated.curation == {} and calls == []
            assert sound.calls == [] and lastfm.requests == []

    anyio.run(run)


def test_agents_need_the_library_grant_to_choose_references() -> None:
    without = {"scopes": json.dumps(["create", "status"])}
    with_library = {"scopes": json.dumps(["create", "status", "library"])}
    for spec in (
        PlaylistRequest(prompt="x", reference_playlists=["pl"]),
        PlaylistRequest(prompt="x", reference_songs=["song"]),
    ):
        with pytest.raises(RequestError):
            Requests.context_scopes(spec, without)
        Requests.context_scopes(spec, with_library)
    # Naming a playlist is accepted; it is looked up only under the library grant.
    Requests.context_scopes(PlaylistRequest(prompt="Like Velvet Orbit"), without)


async def agent_request(
    service: Playlists, key: str, *, library: bool, prompt: str = "More energetic than Velvet Orbit"
) -> tuple[str, str]:
    """A refreshable described request from an agent, stopped at a preview."""
    token = await agent_token(service, "owner", f"Agent {key}", history=False, library=library)
    spec = PlaylistRequest(
        prompt=prompt,
        count=2,
        max_per_artist=1,
        save_instructions=True,
        cadence="daily",
        preview=True,
    )
    ident, _ = await service.requests.create(
        "owner",
        key,
        spec,
        creator=digest(token),
        scopes=["create", "status", *(["library"] if library else [])],
    )
    return ident, digest(token)


def ordering(prompts: list[dict[str, Any]]) -> Any:
    async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
        payload = json.loads(prompt)
        prompts.append(payload)
        if schema is Proposals:
            return proposals("Bright Hours", "Neon Avenue")
        return {
            "goal": "Livelier",
            "constraints_satisfied": True,
            "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
        }

    return ai


@pytest.mark.parametrize("library", [True, False])
def test_agent_requests_look_up_named_playlists_only_with_the_library_grant(
    tmp_path: Path, library: bool
) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, calls):
            prompts: list[dict[str, Any]] = []
            service.curator.api = ordering(prompts)
            ident, grant = await agent_request(service, "agent-named", library=library)
            first = (await service.repo.runs(ident))[0]["id"]
            await service.process(first)
            assert (await service.repo.run(first))["state"] == "preview"
            listed = [m for m, _ in calls if m in ("getPlaylists", "getPlaylist")]
            status = await service.requests.status("owner", ident)
            if not library:
                # No playlist is listed or read, and nothing about one is shown.
                assert listed == [] and "reference" not in prompts[0]
                assert status["references"]["playlists"] == []
                return
            assert listed == ["getPlaylists", "getPlaylist"]
            assert prompts[0]["reference"]["playlists"] == [
                {"name": "Velvet Orbit", "detected": True}
            ]
            assert status["references"]["playlists"][0]["id"] == "pl-orbit"
            # The grant loses library: the next refresh looks nothing up.
            await service.repo.store.execute(
                "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
                [json.dumps(["create", "status"]), grant],
            )
            await service.repo.update(first, "discarded", {})
            # A scheduled refresh of the enabled playlist.
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET enabled=1 WHERE id=?", [ident]
            )
            calls.clear()
            prompts.clear()
            refresh = await service.repo.enqueue(ident, automatic=True)
            await service.process(refresh)
            refreshed = await service.repo.run(refresh)
            assert refreshed["state"] == "preview", refreshed["error"]
            assert [m for m, _ in calls if m in ("getPlaylists", "getPlaylist")] == []
            assert "reference" not in prompts[0]
            assert refreshed["data"]["curation"]["references"]["playlists"] == []

    anyio.run(run)


def test_losing_the_library_grant_during_generation_stops_a_named_reference(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, _):
            prompts: list[dict[str, Any]] = []
            answer = ordering(prompts)
            grant = ""

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                if schema is Proposals:
                    await service.repo.store.execute(
                        "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
                        [json.dumps(["create", "status"]), grant],
                    )
                return await answer(connection, prompt, schema=schema, **kw)

            service.curator.api = ai
            ident, grant = await agent_request(service, "agent-revoked", library=True)
            first = (await service.repo.runs(ident))[0]["id"]
            await service.process(first)
            stopped = await service.repo.run(first)
            assert stopped["state"] == "failed" and "library access" in stopped["error"]
            assert len(prompts) == 1 and "reference" in prompts[0]

    anyio.run(run)


def test_a_live_sound_failure_keeps_the_synced_facts(tmp_path: Path) -> None:
    sound = FakeSound(
        {"ref-1": SongSound(energy=30), "own-bright": SongSound(tempo=128, energy=85)},
        similar=["sound-1"],
        search=["sound-2"],
        live_fail=True,
    )

    async def run() -> None:
        async with curation(tmp_path / "db", sound=sound) as (service, _):
            orders: list[dict[str, Any]] = []

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                if schema is SoundProposals:
                    search = {"text": "bright", "more": [], "less": []}
                    return proposals("Bright Hours", "Neon Avenue", sound_search=search)
                orders.append(payload)
                return {
                    "goal": "Bright songs",
                    "constraints_satisfied": True,
                    "selections": picks(payload, ["Bright Hours", "Neon Avenue"]),
                }

            service.curator.api = ai
            curated = await generate(
                service,
                await service.connection("owner"),
                described("Like Velvet Orbit", use_library=True),
            )
            # The live suggestion failed once and was not asked again; facts still came.
            assert [call for call, _ in sound.calls] == ["facts", "similar", "facts"]
            candidates = {c["title"]: c for c in orders[0]["candidates"]}
            assert candidates["Bright Hours"]["sound"] == {"tempo": 128, "energy": 85}
            assert curated.curation["coverage"]["with_sound"] == 1
            assert curated.curation["coverage"]["sound"] == "unavailable"
            assert curated.curation["sound_pool"] == {"similar": 0, "search": 0}

    anyio.run(run)


async def reduce_grant(service: Playlists, grant: str) -> None:
    await service.repo.store.execute(
        "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
        [json.dumps(["create", "status"]), grant],
    )


def test_losing_the_library_grant_before_a_named_playlist_is_read_stops_the_read(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, calls):
            prompts: list[dict[str, Any]] = []
            service.curator.api = ordering(prompts)
            ident, grant = await agent_request(service, "agent-listing", library=True)
            native = service.client.call

            async def listing_then_reduced(
                connection: Connection, method: str, params: Any = ()
            ) -> Any:
                answer = await native(connection, method, params)
                if method == "getPlaylists":  # the grant shrinks while the listing is read
                    await reduce_grant(service, grant)
                return answer

            service.client.call = listing_then_reduced
            first = (await service.repo.runs(ident))[0]["id"]
            await service.process(first)
            stopped = await service.repo.run(first)
            assert stopped["state"] == "failed"
            assert stopped["error"] == LIBRARY_LOST
            # The playlist found by name was never read, and nothing reached the model.
            assert [m for m, _ in calls] == ["getPlaylists"] and prompts == []
            assert "curation" not in stopped["data"]
            status = await service.requests.status("owner", ident)
            assert status["references"] == {} and status["selected"] == []

    anyio.run(run)


@pytest.mark.parametrize("named", [True, False])
def test_losing_the_library_grant_while_saving_keeps_nothing_found_by_name(
    tmp_path: Path, named: bool
) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, _):
            prompts: list[dict[str, Any]] = []
            service.curator.api = ordering(prompts)
            prompt = "More energetic than Velvet Orbit" if named else "More energetic songs"
            ident, grant = await agent_request(service, "agent-saving", library=True, prompt=prompt)
            generate = service.generate

            async def generated_then_reduced(connection: Connection, mix: Any) -> Any:
                data = await generate(connection, mix)
                await reduce_grant(service, grant)  # after selection, before it is saved
                return data

            service.generate = generated_then_reduced  # type: ignore[method-assign]
            first = (await service.repo.runs(ident))[0]["id"]
            await service.process(first)
            saved = await service.repo.run(first)
            status = await service.requests.status("owner", ident)
            if not named:
                # A selection that found no playlist by name did not need the grant.
                assert saved["state"] == "preview", saved["error"]
                assert status["references"]["playlists"] == []
                return
            assert saved["state"] == "failed" and saved["error"] == LIBRARY_LOST
            assert "curation" not in saved["data"] and "selected" not in saved["data"]
            assert status["references"] == {} and status["selected"] == []
            assert "Velvet Orbit" not in json.dumps(status)

    anyio.run(run)


def test_publishing_a_selection_found_by_name_needs_the_library_grant(tmp_path: Path) -> None:
    async def run() -> None:
        async with curation(tmp_path / "db") as (service, _):
            prompts: list[dict[str, Any]] = []
            service.curator.api = ordering(prompts)
            ident, grant = await agent_request(service, "agent-publish", library=True)
            first = (await service.repo.runs(ident))[0]["id"]
            await service.process(first)
            preview = await service.repo.run(first)
            assert preview["state"] == "preview", preview["error"]
            assert preview["data"]["curation"]["references"]["playlists"][0]["detected"]
            # The owner publishes the preview after the agent's grant lost library.
            await reduce_grant(service, grant)
            service.publish = AsyncMock(side_effect=AssertionError("published"))  # type: ignore[method-assign]
            await service.repo.update(first, "publishing", preview["data"])
            await service.process(first)
            refused = await service.repo.run(first)
            assert refused["state"] == "failed" and refused["error"] == LIBRARY_LOST
            service.publish.assert_not_awaited()

    anyio.run(run)
