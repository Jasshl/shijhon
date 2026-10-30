"""Standalone request contracts: synthetic identities/music, no inference or providers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette

from shijhon.access import Principal
from shijhon.config import PlaylistSettings
from shijhon.playlists.api import API
from shijhon.playlists.model import Connection, Definition, PlaylistError, Suggestion, Track
from shijhon.playlists.prompt import Ordered, Proposals, generate
from shijhon.playlists.requests import PlaylistRequest, RequestError, digest
from shijhon.playlists.resolve import choose
from shijhon.playlists.service import Playlists
from shijhon.store import Store
from shijhon.store.db import _bundled_migrations, apply_migrations
from tests.harness.agents import agent_token


def connected(owner: str = "owner", **kwargs: Any) -> Connection:
    return Connection(user_id=owner, username=owner, token="native-secret", salt="salt", **kwargs)


@asynccontextmanager
async def service_at(path: Path, *, ai: bool = True) -> AsyncIterator[Playlists]:
    store = await Store.open(path)
    service = Playlists(
        store,
        SimpleNamespace(base_url="", library_id=1),
        None,
        None,
        PlaylistSettings(candidate_limit=30),
    )
    service.client.validate = AsyncMock()
    service.client.principal = AsyncMock(
        side_effect=lambda connection: Principal(
            connection.user_id, connection.username, True, frozenset({1})
        )
    )
    await service.repo.connect(
        connected(
            backend="api" if ai else "none",
            endpoint="https://model.invalid/v1" if ai else "",
            model="synthetic" if ai else "",
        )
    )
    try:
        yield service
    finally:
        await service.aclose()
        await store.close()


def test_request_boundaries_and_legacy_defaults() -> None:
    old = Definition.model_validate({"name": "Old discovery"})
    assert old.mode == "discovery"
    assert connected().history_user == ""
    for data in (
        {},
        {"prompt": "music", "count": True},
        {"prompt": "music", "count": 101},
        {"prompt": "music", "use_history": "false"},
        {"prompt": "music", "cadence": "weekly"},
        {"tracks": [{"artist": "A", "title": "B", "id": "native"}], "count": 1},
        {"prompt": "music", "model": "external"},
        {"prompt": "music", "endpoint": "https://elsewhere.invalid"},
        {"prompt": "music", "actions": ["publish"]},
        {"tracks": [{"artist": "A", "title": "B"}], "count": 2},
        {"tracks": [{"artist": "A", "title": "B"}], "count": 1, "use_library": True},
        {
            "tracks": [{"artist": "A", "title": "B"}],
            "count": 1,
            "save_instructions": True,
            "cadence": "daily",
        },
    ):
        with pytest.raises(ValidationError):
            PlaylistRequest.model_validate(data)
    spec = PlaylistRequest(prompt="instrumental music", count=3)
    assert not spec.use_history and not spec.use_library and not spec.preview
    assert spec.cadence == "manual" and spec.definition().interval == 0


@pytest.mark.parametrize(
    "history,library", [(False, False), (True, False), (False, True), (True, True)]
)
def test_contexts_are_independent_and_off_by_default(
    tmp_path: Path,
    history: bool,
    library: bool,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            captured = []
            searches = []
            service.history.sync = AsyncMock(
                return_value=([{"artist": "Heard artist", "title": "Heard song"}], 0)
            )

            async def native(connection: Connection, method: str, params: Any = ()) -> Any:
                if method == "getPlaylists":
                    # Owned names are compared with the instructions; none is mentioned.
                    owned = {"id": "owned", "name": "Paper Lantern Evenings", "owner": "owner"}
                    return {"playlists": {"playlist": [owned]}}
                assert method == "search3"
                searches.append(dict(params))
                return {
                    "searchResult3": {
                        "song": [
                            {
                                "id": "secret-native-id",
                                "artist": "Library artist",
                                "title": "Library song",
                                "album": "Album",
                            }
                        ]
                    }
                }

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                payload = json.loads(prompt)
                captured.append(payload)
                if schema is Proposals:
                    return {
                        "tracks": [
                            {
                                "artist": "Requested artist",
                                "title": "Wanted song",
                                "album": "",
                                "version": "",
                            }
                        ]
                    }
                assert schema is Ordered
                return {
                    "goal": "Quiet music",
                    "constraints_satisfied": True,
                    "selections": [{"id": "candidate-0", "reason": "fits", "fit": "meets"}],
                }

            service.client.call = native
            service.curator.api = ai
            service.resolver.resolve = AsyncMock(
                return_value=Track(id="native", artist="Requested artist", title="Wanted song")
            )
            definition = PlaylistRequest(
                prompt="quiet music", count=1, use_history=history, use_library=library
            ).definition()
            curated = await generate(service, await service.connection("owner"), definition)
            selected = curated.selected
            assert len(selected) == 1
            assert service.history.sync.await_count == int(history)
            assert len(searches) == int(library)
            for payload in captured:
                assert ("history" in payload) is history
                assert ("library" in payload) is library
                encoded = json.dumps(payload)
                assert "native-secret" not in encoded
                assert "secret-native-id" not in encoded
                if not history:
                    assert "Heard artist" not in encoded
                if not library:
                    assert "Library artist" not in encoded
                assert "Paper Lantern" not in encoded and "reference" not in payload

    anyio.run(run)


def test_strict_album_and_version_constraints() -> None:
    option = Track(id="a", artist="A", title="B", album="Different album")
    assert (
        choose({"artist": "A", "title": "B", "album": "Required", "strict_album": True}, [option])
        is None
    )
    assert choose({"artist": "A", "title": "B (Live)"}, [option]) is None
    assert Suggestion(artist="A", title="Alive", version="Live").wanted()["title"] == "Alive (Live)"


@pytest.mark.parametrize("problem", ["unknown", "duplicate", "artist", "constraints", "missing"])
def test_bounded_replenishment_never_silently_fills_bad_choices(
    tmp_path: Path, problem: str
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            calls = 0

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kw: Any) -> Any:
                nonlocal calls
                calls += 1
                if schema is Proposals:
                    return {
                        "tracks": [
                            {"artist": "A", "title": "Song1", "album": "", "version": ""},
                            {
                                "artist": "A" if problem == "artist" else "B",
                                "title": "Song2",
                                "album": "",
                                "version": "",
                            },
                        ]
                    }
                return {
                    "goal": "Two different artists",
                    "constraints_satisfied": problem != "constraints",
                    "selections": [
                        {
                            "id": "invented" if problem == "unknown" else "candidate-0",
                            "reason": "",
                            "fit": "meets",
                        },
                        {
                            "id": "candidate-0" if problem == "duplicate" else "candidate-1",
                            "reason": "",
                            "fit": "meets",
                        },
                    ],
                }

            async def resolve(connection: Connection, wanted: Any) -> Track | None:
                if problem == "missing":
                    return None
                return Track(id=wanted["title"], artist=wanted["artist"], title=wanted["title"])

            service.curator.api = ai
            service.resolver.resolve = resolve
            with pytest.raises(PlaylistError):
                await generate(
                    service,
                    await service.connection("owner"),
                    PlaylistRequest(
                        prompt="two different artists", count=2, max_per_artist=1
                    ).definition(),
                )
            assert calls <= 6

    anyio.run(run)


def test_explicit_tracks_bypass_model_and_keep_order(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.curator.api = AsyncMock(side_effect=AssertionError("no model"))
            service.history.sync = AsyncMock(side_effect=AssertionError("no history"))

            async def resolve(connection: Connection, wanted: Any) -> Track:
                return Track(
                    id=wanted["title"],
                    artist=wanted["artist"],
                    title=wanted["title"],
                    album=wanted["album"],
                )

            service.resolver.resolve = resolve
            spec = PlaylistRequest(
                count=2, tracks=[{"artist": "A", "title": "Two"}, {"artist": "B", "title": "One"}]
            )
            songs = (await generate(service, connected(), spec.definition())).selected
            assert [s.title for s in songs] == ["Two", "One"]
            ident, _ = await service.requests.create("owner", "explicit-1", spec)
            mix = await service.repo.mix(ident, "owner")
            assert not mix["enabled"] and not mix["due"]

    anyio.run(run)


def test_replenishment_replaces_unresolvable_suggestions_with_verified_candidates(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            proposals = 0

            async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
                nonlocal proposals
                payload = json.loads(prompt)
                if schema is Proposals:
                    proposals += 1
                    if proposals == 2:
                        assert payload["already_attempted"][0]["title"] == "Unavailable"
                    title = "Unavailable" if proposals == 1 else "Available"
                    return {"tracks": [{"artist": "A", "title": title, "album": "", "version": ""}]}
                return {
                    "goal": "One quiet recording",
                    "constraints_satisfied": True,
                    "selections": [
                        {"id": payload["candidates"][0]["id"], "reason": "Fits", "fit": "meets"}
                    ],
                }

            async def resolve(connection: Connection, wanted: Any) -> Track | None:
                return (
                    Track(id="verified", artist="A", title="Available")
                    if wanted["title"] == "Available"
                    else None
                )

            service.curator.api = ai
            service.resolver.resolve = resolve
            songs, skipped, _, _ = await generate(
                service,
                await service.connection("owner"),
                PlaylistRequest(prompt="one quiet recording", count=1).definition(),
            )
            assert [s.id for s in songs] == ["verified"] and skipped == 1
            assert proposals == 2

    anyio.run(run)


def test_bearer_isolation_scopes_idempotency_and_revocation(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            token = await agent_token(service, "owner", "First agent", history=False, library=False)
            second = await agent_token(service, "owner", "Second agent", history=True, library=True)
            await service.repo.connect(connected("other"))
            foreign = await agent_token(service, "other", "Other", history=False, library=False)
            app = Starlette(routes=API(lambda: service).routes())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="https://test.invalid"
            ) as http:
                path = "/api/playlists/requests"
                headers = {"authorization": f"Bearer {token}", "Idempotency-Key": "request-1"}
                payload = {"prompt": "A quiet instrumental mix", "count": 1}
                response = await http.post(path, headers=headers, json=payload)
                assert response.status_code == 202, response.text
                ident = response.json()["id"]
                assert (await http.post(path, headers=headers, json=payload)).json()["id"] == ident
                assert (
                    await http.post(path, headers=headers, json={**payload, "count": 2})
                ).status_code == 409
                assert (
                    await http.get(
                        path + "/" + ident, headers={"authorization": f"Bearer {second}"}
                    )
                ).status_code == 404
                assert (
                    await http.get(
                        path + "/" + ident, headers={"authorization": f"Bearer {foreign}"}
                    )
                ).status_code == 404
                assert (
                    await http.get(
                        path + "/" + ident, headers={"cookie": "shijhon_session=" + token}
                    )
                ).status_code == 401
                for influence in ("use_history", "use_library"):
                    response = await http.post(
                        "/api/playlists/context", headers=headers, json={influence: True}
                    )
                    assert response.status_code == 403
                service.history.sync = AsyncMock(side_effect=AssertionError("no context"))
                service.client.call = AsyncMock(side_effect=AssertionError("no library"))
                response = await http.post("/api/playlists/context", headers=headers, json={})
                assert response.status_code == 200 and response.json() == {}
                granted = {"authorization": f"Bearer {second}"}
                service.history.sync = AsyncMock(
                    return_value=([{"artist": "A", "title": "Heard", "at": 42}], 0)
                )
                response = await http.post(
                    "/api/playlists/context",
                    headers=granted,
                    json={"use_history": True, "limit": 1},
                )
                assert response.json() == {
                    "history": [{"artist": "A", "title": "Heard", "listens": 1}]
                }
                service.client.call = AsyncMock(
                    return_value={
                        "searchResult3": {
                            "song": [
                                {
                                    "id": "native",
                                    "artist": "B",
                                    "title": "Library",
                                    "album": "Release",
                                }
                            ]
                        }
                    }
                )
                response = await http.post(
                    "/api/playlists/context",
                    headers=granted,
                    json={"use_library": True, "limit": 1},
                )
                assert response.json() == {
                    "library": [{"artist": "B", "title": "Library", "album": "Release"}]
                }
                service.client.validate = AsyncMock(
                    side_effect=PlaylistError("private-native-error")
                )
                response = await http.get(path + "/" + ident, headers=headers)
                assert response.status_code == 401 and "private-native-error" not in response.text
                service.client.validate = AsyncMock()
                await service.requests.revoke("owner", digest(token))
                assert (await http.get(path + "/" + ident, headers=headers)).status_code == 401
                await service.process((await service.repo.runs(ident))[0]["id"])
                assert (await service.requests.status("owner", ident))["state"] == "failed"
            row = await service.repo.store.fetchone("SELECT * FROM playlist_agent_tokens LIMIT 1")
            assert token not in str(dict(row))

    anyio.run(run)


def test_inflight_revocation_stops_before_resolving_or_publishing(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            token = await agent_token(service, "owner", "Agent", history=False, library=False)
            ident, _ = await service.requests.create(
                "owner",
                "revocation-1",
                PlaylistRequest(prompt="quiet music", count=1),
                creator=digest(token),
                scopes=["create", "status"],
            )

            async def ai(connection: Connection, prompt: str, **kwargs: Any) -> Any:
                await service.requests.revoke("owner", digest(token))
                return {"tracks": [{"artist": "A", "title": "B", "album": "", "version": ""}]}

            service.curator.api = ai
            service.resolver.resolve = AsyncMock(side_effect=AssertionError("revoked"))
            service.publish = AsyncMock(side_effect=AssertionError("revoked"))
            run = (await service.repo.runs(ident))[0]
            await service.process(run["id"])
            assert (await service.repo.run(run["id"]))["state"] == "failed"
            service.resolver.resolve.assert_not_awaited()
            service.publish.assert_not_awaited()

    anyio.run(run)


def test_native_song_checks_owner_identity_and_bound_library(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            service.client.call = AsyncMock(
                return_value={"song": {"id": "native", "artist": "A", "title": "B"}}
            )
            service.client.native.song = AsyncMock(return_value={"id": "native", "libraryId": 2})
            with pytest.raises(PlaylistError, match="no longer in this library"):
                await service.native_track(connected(), "native", library_bound=True)
            service.client.native.song = AsyncMock(return_value={"id": "native", "libraryId": 1})
            assert (
                await service.native_track(connected(), "native", library_bound=True)
            ).id == "native"
            service.client.call = AsyncMock(return_value={"song": {"id": "other"}})
            with pytest.raises(PlaylistError, match="identity"):
                await service.native_track(connected(), "native", library_bound=True)

    anyio.run(run)


def test_frozen_retry_survives_reopen(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "db"
        async with service_at(path) as service:
            ident, _ = await service.requests.create(
                "owner", "request-key", PlaylistRequest(prompt="quiet music", count=1)
            )
            run = (await service.repo.runs(ident))[0]
            frozen = {
                "selected": [{"id": "frozen-native"}],
                "intent": "create",
                "provisional": "unique-provisional",
                "native_ids": ["frozen-native"],
            }
            await service.repo.update(run["id"], "uncertain", frozen)
        async with service_at(path) as service:
            await service.requests.retry("owner", ident)
            after = await service.repo.run(run["id"])
            assert after["state"] == "publishing" and after["data"] == frozen
            service.client.native.library_id = 2
            with pytest.raises(RequestError):
                await service.requests.status("owner", ident)

    anyio.run(run)


def test_upgrade_keeps_legacy_connections_definitions_and_frozen_runs(tmp_path: Path) -> None:
    path = tmp_path / "db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        apply_migrations(conn, _bundled_migrations()[:5])
        legacy = connected(history_user="old-profile", history_key="old-key").stored()
        spec = json.dumps({"name": "Original discovery", "count": 1})
        frozen = '{"selected":[{"id":"same-native-id"}],"intent":"create"}'
        conn.execute(
            "INSERT INTO playlist_owners(id,username,connection,generation)"
            " VALUES ('owner','owner',?,'generation')",
            [legacy],
        )
        conn.execute(
            "INSERT INTO playlist_mixes(id,owner,definition) VALUES ('mix','owner',?)", [spec]
        )
        conn.execute(
            "INSERT INTO playlist_runs(id,mix,created,state,data)"
            " VALUES ('run','mix',1,'uncertain',?)",
            [frozen],
        )
        assert apply_migrations(conn) == _bundled_migrations()[-1][0]
        assert conn.execute("SELECT connection FROM playlist_owners").fetchone()[0] == legacy
        assert conn.execute("SELECT definition FROM playlist_mixes").fetchone()[0] == spec
        assert conn.execute("SELECT data FROM playlist_runs").fetchone()[0] == frozen
    finally:
        conn.close()
