"""Boundaries of agents managing the owner's playlists: permissions for songs, other
owners and libraries, discarded and uncertain changes, duplicates and putting a song
back. Invented music and accounts only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
from pydantic import SecretStr

from shijhon.playlists.model import Definition, PlaylistError, fingerprint
from shijhon.playlists.requests import PlaylistRequest
from shijhon.playlists.service import Playlists
from tests.unit.test_agent_playlist_management import (
    connect,
    created,
    described,
    latest,
    tracks,
)
from tests.unit.test_agents import app_at, call
from tests.unit.test_playlist_explicit_tracks import SONGS, install
from tests.unit.test_playlist_requests import connected


async def dashboard_playlist(service: Playlists, native: dict[str, Any], **context: bool) -> str:
    """A described playlist made on the dashboard (with ``context``), published earlier."""
    await service.repo.connect(
        connected(
            backend="api",
            endpoint="https://model.invalid/v1",
            model="synthetic",
            history_user="listener",
            history_key=SecretStr("invented-key"),
        )
    )
    spec = PlaylistRequest(name="Lantern evening", prompt="quiet lantern songs", count=2, **context)
    ident, _ = await service.requests.create("owner", "dashboard-key", spec)
    native.update(id="explicit-playlist", name="Lantern evening", owner="owner")
    native["entry"], native["changed"] = [{"id": "n1"}, {"id": "n2"}], "x"
    await service.repo.update(
        (await service.repo.runs(ident))[0]["id"],
        "published",
        {
            "definition": spec.definition().model_dump(),
            "selected": [SONGS["Night Bloom"].model_dump(), SONGS["Lantern Hall"].model_dump()],
            "native_ids": ["n1", "n2"],
        },
    )
    await service.repo.store.execute(
        "UPDATE playlist_mixes SET playlist_id=?,published=? WHERE id=?",
        [native["id"], fingerprint(native), ident],
    )
    return ident


async def read(http: httpx.AsyncClient, token: str, ident: str) -> dict[str, Any]:
    return dict((await call(http, token, "get_playlist_request", {"request_id": ident}))["request"])


def titles(status: dict[str, Any]) -> list[str]:
    return [s["title"] for s in status["selected"]]


def published(status: dict[str, Any]) -> list[str] | None:
    songs = status.get("published_songs")
    return None if songs is None else [s["song_id"] for s in songs]


def test_a_replacement_preview_shows_only_the_songs_its_agent_chose(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            ident = await dashboard_playlist(service, native, use_history=True)
            agent = await connect(http, "create status")
            replaced = await call(
                http,
                agent,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "probe-preview",
                    "tracks": tracks("Grey Tide"),
                    "preview": True,
                },
            )
            assert replaced["ok"], replaced
            await latest(service, ident)
            waiting = await read(http, agent, ident)
            assert waiting["state"] == "preview" and titles(waiting) == ["Grey Tide"]
            # The history-chosen songs it did not choose stay hidden.
            assert published(waiting) is None and "history permission" in waiting["songs_hidden"]
            discarded = await call(
                http,
                agent,
                "review_playlist_preview",
                {"request_id": ident, "run_id": waiting["run"], "action": "discard"},
            )
            assert discarded["ok"] and published(discarded["request"]) is None
            refused = await call(
                http,
                agent,
                "edit_playlist_tracks",
                {"request_id": ident, "idempotency_key": "probe-edit", "order": ["n2", "n1"]},
            )
            assert refused["error"]["missing_permissions"] == ["library", "history"]
            reader = await connect(http, "status library history")
            assert published(await read(http, reader, ident)) == ["n1", "n2"]

    anyio.run(run)


def test_another_agent_s_refresh_or_retry_does_not_show_it_the_songs(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            ident = await dashboard_playlist(
                service, native, save_instructions=True, cadence="weekly"
            )
            agent = await connect(http, "create status")
            assert published(await read(http, agent, ident)) is None
            refreshed = await call(
                http, agent, "manage_playlist_refresh", {"request_id": ident, "action": "preview"}
            )
            assert refreshed["ok"], refreshed
            queued = (await service.repo.runs(ident))[0]
            assert (
                queued["data"]["actor"] == (await app.oauth.verify_token(agent)).claims["grant_id"]
            )
            # The server's selection for that refresh is not the agent's choice.
            chosen = {**queued["data"], "selected": [SONGS["Grey Tide"].model_dump()]}
            await service.repo.update(queued["id"], "preview", chosen)
            seen = await read(http, agent, ident)
            assert seen["state"] == "preview" and titles(seen) == [] and published(seen) is None
            library = await connect(http, "create status library")
            assert titles(await read(http, library, ident)) == ["Grey Tide"]
        # A retry of a selection chosen from history shows it neither.
        async with app_at(tmp_path / "db2") as (app, http):
            service = app.service
            native = install(service)
            ident = await dashboard_playlist(service, native, use_history=True)
            run_id = (await service.repo.runs(ident))[0]["id"]
            data = (await service.repo.run(run_id))["data"]
            await service.repo.update(run_id, "failed", data, "Synthetic failure.")
            agent = await connect(http, "create status history")
            retried = await call(http, agent, "retry_playlist_request", {"request_id": ident})
            assert retried["ok"], retried
            assert retried["request"]["state"] == "publishing"
            assert titles(retried["request"]) == []
            full = await connect(http, "status library history")
            assert titles(await read(http, full, ident)) == ["Night Bloom", "Lantern Hall"]

    anyio.run(run)


def test_an_addition_to_another_agent_s_playlist_shows_no_songs_it_did_not_send(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            first = await connect(http)
            ident = await created(service, http, first, "Night Bloom", "Unheard Song")
            second = await connect(http, "create status")
            added = await call(
                http,
                second,
                "add_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "probe-addition",
                    "tracks": [{"artist": "Mira Sole", "title": "Grey Tide", "replaces": 2}],
                },
            )
            assert added["ok"], added
            await latest(service, ident)
            assert [e["id"] for e in native["entry"]] == ["n1", "n3"]
            seen = await read(http, second, ident)
            assert seen["state"] == "published"
            assert titles(seen) == [] and published(seen) is None and "tracks" not in seen
            library = await connect(http, "create status library")
            assert published(await read(http, library, ident)) == ["n1", "n3"]
            # Its creator still sees its own playlist.
            assert published(await read(http, first, ident)) == ["n1", "n3"]

    anyio.run(run)


def test_songs_of_a_dashboard_playlist_need_the_permissions_its_context_used(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            ident = await dashboard_playlist(app.service, native, use_history=True)
            for scopes, visible in (
                ("status", False),
                ("status library", False),
                ("status history", False),
                ("status library history", True),
            ):
                seen = await read(http, await connect(http, scopes), ident)
                assert seen["made_by"] == "dashboard" and seen["state"] == "published"
                titles = [s["title"] for s in seen["selected"]]
                assert titles == (["Night Bloom", "Lantern Hall"] if visible else []), scopes
                assert ("published_songs" in seen) is visible
                assert ("history permission" in seen.get("songs_hidden", "")) is not visible

    anyio.run(run)


def test_songs_of_a_dashboard_playlist_using_the_library_need_library(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            ident = await dashboard_playlist(app.service, native, use_library=True)
            hidden = await read(http, await connect(http, "status"), ident)
            assert hidden["selected"] == [] and "history" not in hidden["songs_hidden"]
            shown = await read(http, await connect(http, "status library"), ident)
            assert [s["song_id"] for s in shown["published_songs"]] == ["n1", "n2"]

    anyio.run(run)


def test_another_owner_s_or_another_library_s_playlist_is_not_found(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http, "create status library history")
            ident = await created(app.service, http, token, "Night Bloom")
            await app.service.repo.connect(connected("another-owner"))
            elsewhere = await app.service.repo.create("another-owner", Definition())
            calls = {
                "review_playlist_preview": {"run_id": "any-run", "action": "publish"},
                "review_playlist_import": {
                    "run_id": "any-run",
                    "review_key": "any-key",
                    "action": "discard",
                },
                "get_playlist_import": {},
                "manage_playlist_refresh": {"action": "pause"},
                "retry_playlist_request": {},
                "update_discovery_mix": {"mix": {"name": "Other"}},
            }
            for tool, args in calls.items():
                answer = await call(http, token, tool, {"request_id": elsewhere, **args})
                assert answer["error"]["code"] == "not_found", (tool, answer)
            # The same owner's playlist in another library is not this installation's.
            await app.service.repo.store.execute(
                "UPDATE playlist_requests SET library='2' WHERE id=?", [ident]
            )
            assert (await call(http, token, "list_playlist_requests"))["playlists"] == []
            for tool, args in (
                ("get_playlist_request", {}),
                ("manage_playlist_refresh", {"action": "pause"}),
                (
                    "replace_playlist_tracks",
                    {"idempotency_key": "other-library", "tracks": tracks("Grey Tide")},
                ),
            ):
                answer = await call(http, token, tool, {"request_id": ident, **args})
                assert answer["error"]["code"] == "not_found", (tool, answer)

    anyio.run(run)


def test_a_discarded_replacement_leaves_additions_on_the_published_list(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            token = await connect(http)
            ident = await created(service, http, token, "Night Bloom", "Unheard Song")
            assert [e["id"] for e in native["entry"]] == ["n1"]
            preview = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-preview",
                    "tracks": tracks("Grey Tide", "Paper Moths"),
                    "preview": True,
                },
            )
            assert preview["ok"], preview
            await latest(service, ident)
            waiting = await read(http, token, ident)
            assert waiting["state"] == "preview"
            assert [s["title"] for s in waiting["selected"]] == ["Grey Tide", "Paper Moths"]
            # The published list keeps its IDs while the preview waits.
            assert [s["song_id"] for s in waiting["published_songs"]] == ["n1"]
            discarded = await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": ident, "run_id": waiting["run"], "action": "discard"},
            )
            assert discarded["ok"], discarded
            mix = await service.repo.mix(ident, "owner")
            titles = [t["title"] for t in json.loads(mix["definition"])["tracks"]]
            assert titles == ["Night Bloom", "Unheard Song"]
            added = await call(
                http,
                token,
                "add_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "add-after-discard",
                    "tracks": [{"artist": "Orrin Vale", "title": "Lantern Hall", "replaces": 2}],
                },
            )
            assert added["ok"], added
            await latest(service, ident)
            status = await read(http, token, ident)
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n1", "n2"]
            assert [t["input"]["title"] for t in status["tracks"]] == [
                "Night Bloom",
                "Lantern Hall",
            ]

    anyio.run(run)


def test_a_failed_replacement_leaves_the_definition_as_published(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            install(service)
            token = await connect(http)
            ident = await created(service, http, token, "Night Bloom")
            before = (await service.repo.mix(ident, "owner"))["definition"]
            failed = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-nothing",
                    "tracks": tracks("Unheard Song"),
                },
            )
            assert failed["ok"]
            await latest(service, ident)
            assert (await service.requests.status("owner", ident))["state"] == "failed"
            assert (await service.repo.mix(ident, "owner"))["definition"] == before
            assert [s["song_id"] for s in (await read(http, token, ident))["published_songs"]] == [
                "n1"
            ]

    anyio.run(run)


def test_the_owner_settles_an_uncertain_change_of_a_revoked_agent(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            token = await connect(http)
            ident = await created(service, http, token, "Night Bloom")
            replaced = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-uncertain",
                    "tracks": tracks("Grey Tide"),
                },
            )
            assert replaced["ok"]
            working = service.client.call

            async def lost(connection: Any, method: str, params: Any = ()) -> Any:
                if method == "createPlaylist":
                    raise PlaylistError("Navidrome did not answer.")
                return await working(connection, method, params)

            service.client.call = lost  # type: ignore[method-assign]
            await latest(service, ident)
            assert (await service.requests.status("owner", ident))["state"] == "uncertain"
            grant = (await app.oauth.verify_token(token)).claims["grant_id"]
            await app.oauth.store.execute(
                "UPDATE playlist_agent_tokens SET revoked=1 WHERE id=?", [grant]
            )
            service.client.call = working  # type: ignore[method-assign]
            # The owner's retry on the dashboard reconciles it under their own authority.
            await service.requests.retry("owner", ident)
            await latest(service, ident)
            status = await service.requests.status("owner", ident)
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n3"]

    anyio.run(run)


def test_an_addition_or_retry_by_another_agent_runs_under_its_own_grant(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            first = await connect(http)
            ident = await created(service, http, first, "Night Bloom", "Unheard Song")
            grant = (await app.oauth.verify_token(first)).claims["grant_id"]
            await app.oauth.store.execute(
                "UPDATE playlist_agent_tokens SET revoked=1 WHERE id=?", [grant]
            )
            second = await connect(http)
            added = await call(
                http,
                second,
                "add_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "second-addition",
                    "tracks": [{"artist": "Mira Sole", "title": "Grey Tide", "replaces": 2}],
                },
            )
            assert added["ok"], added
            await latest(service, ident)
            assert (await service.requests.status("owner", ident))["state"] == "published"
            assert [e["id"] for e in native["entry"]] == ["n1", "n3"]

    anyio.run(run)


def test_a_discovery_review_needs_history_and_keeps_the_refreshes_asked_for(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            await service.repo.connect(
                connected(history_user="listener", history_key=SecretStr("invented-key"))
            )
            token = await connect(http, "create status history")
            made = await call(
                http,
                token,
                "create_discovery_mix",
                {
                    "idempotency_key": "discovery-review",
                    "mix": {
                        "name": "Night discoveries",
                        "count": 5,
                        "cadence": "weekly",
                        "preview": True,
                        "enable_refreshes": True,
                    },
                },
            )
            ident = made["request"]["id"]
            run_ = (await service.repo.runs(ident))[0]
            await service.repo.update(run_["id"], "preview", run_["data"])
            review = {"request_id": ident, "run_id": run_["id"], "action": "publish"}
            no_history = await connect(http, "create status")
            denied = await call(http, no_history, "review_playlist_preview", review)
            assert denied["error"]["missing_permissions"] == ["history"]
            assert (await service.repo.run(run_["id"]))["state"] == "preview"
            done = await call(http, token, "review_playlist_preview", review)
            assert done["ok"], done
            published = await service.repo.run(run_["id"])
            assert published["state"] == "publishing"
            assert published["data"]["enable_after"] is True

    anyio.run(run)


def test_an_edit_keeps_the_user_s_repeated_song_and_remove_takes_one_copy(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            token = await connect(http)
            ident = await created(service, http, token, "Night Bloom", "Lantern Hall")
            described(service, native)
            # The user added a second copy in an app.
            native["entry"], native["changed"] = [{"id": "n1"}, {"id": "n2"}, {"id": "n1"}], "y"

            async def edit(key: str, **values: Any) -> dict[str, Any]:
                return dict(
                    await call(
                        http,
                        token,
                        "edit_playlist_tracks",
                        {"request_id": ident, "idempotency_key": key, **values},
                    )
                )

            # A reorder keeps both copies the user made: publication accepts the repeat.
            reordered = await edit("edit-keeps-copy", order=["n1", "n1", "n2"])
            assert reordered["ok"], reordered
            await latest(service, ident)
            status = await read(http, token, ident)
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n1", "n1", "n2"]
            # A second copy the edit itself would add is refused when it is asked for.
            added = await edit("edit-adds-copy", order=["n1", "n1", "n2", "n2"])
            assert "remaining songs" in added["error"]["message"]
            # Removing one copy keeps the other, which is not remembered as removed.
            done = await edit("edit-one-copy", remove=["n1"])
            assert done["ok"], done
            await latest(service, ident)
            status = await read(http, token, ident)
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n1", "n2"]
            assert status["user_edits"]["removed"] == []

    anyio.run(run)


def test_forgetting_one_removal_puts_that_song_back(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            token = await connect(http)
            ident = await created(service, http, token, "Night Bloom", "Lantern Hall", "Grey Tide")
            described(service, native)
            edited = await call(
                http,
                token,
                "edit_playlist_tracks",
                {"request_id": ident, "idempotency_key": "remove-two", "remove": ["n1", "n2"]},
            )
            assert edited["ok"], edited
            await latest(service, ident)
            removed = (await read(http, token, ident))["user_edits"]["removed"]
            assert sorted(s["song_id"] for s in removed) == ["n1", "n2"]
            refused = await call(
                http,
                token,
                "manage_playlist_refresh",
                {"request_id": ident, "action": "pause", "song_ids": ["n1"]},
            )
            assert not refused["ok"]
            forgot = await call(
                http,
                token,
                "manage_playlist_refresh",
                {"request_id": ident, "action": "forget_edits", "song_ids": ["n1"]},
            )
            assert forgot["forgotten"] == 1
            removed = forgot["request"]["user_edits"]["removed"]
            assert [s["song_id"] for s in removed] == ["n2"]
            replaced = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "put-one-back",
                    "tracks": tracks("Night Bloom", "Lantern Hall", "Grey Tide"),
                },
            )
            assert replaced["ok"]
            await latest(service, ident)
            assert [e["id"] for e in native["entry"]] == ["n1", "n3"]

    anyio.run(run)
