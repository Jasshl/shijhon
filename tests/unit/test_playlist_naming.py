"""Automatic titles use synthetic model answers and isolated durable state only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import ValidationError

from shijhon.playlists.model import Definition, PlaylistError, Track
from shijhon.playlists.prompt import NamedOrdered, Ordered, Proposals
from shijhon.playlists.requests import PlaylistRequest, digest
from shijhon.playlists.service import Playlists
from tests.harness.agents import agent_token
from tests.unit.test_playlist_requests import connected, service_at

TITLE = "Lanterns After Rain"
SONG = Track(id="native-song", artist="Glass Orchard", title="First Lantern")


def install_model(service: Playlists, name: Any = TITLE) -> list[type]:
    calls = []

    async def ai(connection: Any, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        calls.append(schema)
        if schema is Proposals:
            return {
                "tracks": [{"artist": SONG.artist, "title": SONG.title, "album": "", "version": ""}]
            }
        payload = json.loads(prompt)
        assert schema in (Ordered, NamedOrdered)
        answer = {
            "goal": "Quiet music",
            "constraints_satisfied": True,
            "selections": [
                {"id": payload["candidates"][0]["id"], "reason": "Fits", "fit": "meets"}
            ],
        }
        if schema is NamedOrdered:
            assert "playlist name" in kwargs["instructions"]
            answer["name"] = name
        return answer

    service.curator.api = ai
    service.resolver.resolve = AsyncMock(return_value=SONG)
    return calls


def test_only_empty_prompt_names_opt_in_and_old_definitions_load() -> None:
    assert not Definition(name="Old discovery").auto_name
    assert not PlaylistRequest(prompt="quiet").definition().auto_name
    assert PlaylistRequest(name="", prompt="quiet").definition().auto_name
    for name in ("Described playlist", "  Evening lights  ", "Paper & Rain"):
        definition = PlaylistRequest(name=name, prompt="quiet").definition()
        assert definition.name == name and not definition.auto_name
    for name in ("", " ", "Line\nbreak"):
        with pytest.raises(ValidationError):
            PlaylistRequest(name=name, count=1, tracks=[{"artist": "A", "title": "B"}])


@pytest.mark.parametrize("name", ["", " ", "Line\nbreak", "Hidden\x00name", "x" * 101, 4, None])
def test_invalid_generated_names_fail_before_freeze_or_publication(
    tmp_path: Path, name: Any
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            calls = install_model(service, name)
            service.publish = AsyncMock(side_effect=AssertionError("invalid title published"))
            ident, _ = await service.requests.create(
                "owner", "invalid-name", PlaylistRequest(name="", prompt="quiet", count=1)
            )
            first = (await service.repo.runs(ident))[0]
            await service.process(first["id"])
            after = await service.repo.run(first["id"])
            assert after["state"] == "failed" and not after["data"].get("selected")
            mix = await service.repo.mix(ident, "owner")
            assert Definition.model_validate_json(mix["definition"]).auto_name
            assert after["error"] == "AI did not return a valid playlist suggestion or selection."
            assert calls == [Proposals, NamedOrdered]
            service.publish.assert_not_awaited()

    anyio.run(run)


@pytest.mark.parametrize("abort_freeze", [False, True])
def test_name_and_selection_freeze_atomically_and_survive_reopen(
    tmp_path: Path, abort_freeze: bool
) -> None:
    async def run() -> None:
        path = tmp_path / "db"
        async with service_at(path) as service:
            calls = install_model(service)
            spec = PlaylistRequest(name="", prompt="quiet", count=1, preview=True)
            ident, _ = await service.requests.create("owner", "freeze-name", spec)
            first = (await service.repo.runs(ident))[0]
            original = dict(await service.repo.store.fetchone("SELECT * FROM playlist_requests"))
            if abort_freeze:
                # Fail the second write, after the mix title has changed in the transaction.
                await service.repo.store.execute(
                    "CREATE TRIGGER interrupt_freeze BEFORE UPDATE ON playlist_runs"
                    " WHEN NEW.state='preview' BEGIN SELECT RAISE(ABORT,'interrupted'); END"
                )
            await service.process(first["id"])
            assert calls == [Proposals, NamedOrdered]
        async with service_at(path) as service:
            after = await service.repo.run(first["id"])
            mix = await service.repo.mix(ident, "owner")
            definition = Definition.model_validate_json(mix["definition"])
            if abort_freeze:
                assert definition.auto_name and definition.name == "Described playlist"
                assert after["state"] == "failed" and not after["data"].get("selected")
            else:
                assert after["state"] == "preview" and not definition.auto_name
                assert definition.name == TITLE
                assert after["data"]["definition"] == definition.model_dump()
                assert after["data"]["selected"][0]["id"] == SONG.id
                assert (await service.requests.status("owner", ident))["name"] == TITLE
            saved = dict(await service.repo.store.fetchone("SELECT * FROM playlist_requests"))
            assert saved == original
            replay, created = await service.requests.create("owner", "freeze-name", spec)
            assert replay == ident and not created

    anyio.run(run)


def test_uncertain_create_reuses_frozen_title_and_native_id_after_restart(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "db"
        native: dict[str, Any] = {}
        creates = []
        uncertain = True

        async def call(connection: Any, method: str, params: Any = ()) -> Any:
            nonlocal uncertain
            values = dict(params)
            if method == "getSong":
                return {"song": SONG.model_dump()}
            if method == "getPlaylists":
                return {"playlists": {"playlist": [native.copy()]}}
            if method == "createPlaylist":
                creates.append(values)
                if "playlistId" not in values:
                    native.update(id="stable-playlist", name=values["name"], owner="owner")
                else:
                    assert values["playlistId"] == native["id"]
                native["entry"] = [{"id": value} for key, value in params if key == "songId"]
                if uncertain:
                    uncertain = False
                    raise PlaylistError("Native response was interrupted.")
                return {"playlist": native.copy()}
            assert method == "updatePlaylist"
            native["name"] = values["name"]
            return {}

        def install_native(service: Playlists) -> None:
            service.client.call = call
            service.client.playlist = AsyncMock(side_effect=lambda *args: native.copy())
            service.client.native.song = AsyncMock(return_value={"libraryId": 1})

        async with service_at(path) as service:
            calls = install_model(service)
            install_native(service)
            spec = PlaylistRequest(
                name="", prompt="quiet", count=1, save_instructions=True, cadence="daily"
            )
            ident, _ = await service.requests.create("owner", "uncertain-name", spec)
            first = (await service.repo.runs(ident))[0]
            await service.process(first["id"])
            frozen = await service.repo.run(first["id"])
            assert frozen["state"] == "uncertain"
            assert frozen["data"]["definition"]["name"] == TITLE
            assert native["name"] == frozen["data"]["provisional"] != TITLE
            assert calls == [Proposals, NamedOrdered]
        async with service_at(path) as service:
            install_native(service)
            service.generate = AsyncMock(side_effect=AssertionError("retry chose new music"))
            await service.requests.retry("owner", ident)
            await service.process(first["id"])
            service.generate.assert_not_awaited()
            recovered = await service.repo.run(first["id"])
            assert recovered["state"] == "published", recovered["error"]
            assert recovered["data"]["selected"] == frozen["data"]["selected"]
            assert recovered["data"]["definition"] == frozen["data"]["definition"]
            assert native["name"] == TITLE and len(creates) == 1
            status = await service.requests.status("owner", ident)
            assert status["name"] == TITLE and status["playlist_id"] == "stable-playlist"
        async with service_at(path) as service:
            install_native(service)
            calls = install_model(service, "A different title must not be requested")
            refresh = await service.repo.enqueue(ident)
            await service.process(refresh)
            assert (await service.repo.run(refresh))["state"] == "published"
            assert calls == [Proposals, Ordered]
            assert native["name"] == TITLE and native["id"] == "stable-playlist"
            assert len(creates) == 2 and creates[-1]["playlistId"] == native["id"]

    anyio.run(run)


def test_historical_canonical_inputs_replay_without_rewriting(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            # Literal pre-naming canonical shape, including the original API default.
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
            ident, _ = await service.requests.create(
                "owner", "historical-name", PlaylistRequest.model_validate(historical)
            )
            await service.repo.store.execute(
                "UPDATE playlist_requests SET input=?,input_hash=? WHERE id=?",
                [canonical, digest(canonical), ident],
            )
            assert await service.requests.create(
                "owner", "historical-name", PlaylistRequest(prompt="quiet", count=1)
            ) == (ident, False)
            row = await service.repo.store.fetchone(
                "SELECT * FROM playlist_requests WHERE id=?", [ident]
            )
            assert row["input"] == canonical and row["input_hash"] == digest(canonical)

    anyio.run(run)


@pytest.mark.parametrize("stop", ["revoked", "discarded", "paused", "connection"])
def test_post_generation_guards_prevent_freezing_a_new_name(tmp_path: Path, stop: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            install_model(service)
            token = await agent_token(
                service, "owner", "Synthetic agent", history=False, library=False
            )
            ident, _ = await service.requests.create(
                "owner",
                "guarded-name",
                PlaylistRequest(name="", prompt="quiet", count=1),
                creator=digest(token),
                scopes=["create", "status"],
            )
            first = (await service.repo.runs(ident))[0]
            if stop == "paused":
                await service.repo.store.execute(
                    "UPDATE playlist_mixes SET enabled=1 WHERE id=?", [ident]
                )
                await service.repo.store.execute(
                    "UPDATE playlist_runs SET automatic=1 WHERE id=?", [first["id"]]
                )
            generate = service.generate

            async def interrupted(connection: Any, mix: Any) -> dict[str, Any]:
                data = await generate(connection, mix)
                assert data["definition"]["name"] == TITLE
                async with service.changes:
                    if stop == "revoked":
                        await service.requests.revoke("owner", digest(token))
                    elif stop == "discarded":
                        await service.repo.update(first["id"], "discarded", {})
                    elif stop == "paused":
                        await service.repo.pause(ident)
                    else:
                        await service.repo.connect(connected(backend="api", model="changed"))
                return data

            service.generate = interrupted
            service.publish = AsyncMock(side_effect=AssertionError("guarded publication"))
            await service.process(first["id"])
            mix = await service.repo.mix(ident, "owner")
            assert Definition.model_validate_json(mix["definition"]).auto_name
            assert (await service.repo.run(first["id"]))["state"] in ("failed", "discarded")
            service.publish.assert_not_awaited()

    anyio.run(run)
