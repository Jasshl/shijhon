"""Agents see and change all of the owner's Shijhon playlists: replacements, edits,
previews, discovery mixes and imports over MCP. Invented music and accounts only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from pydantic import SecretStr

from shijhon.playlists.model import Definition, Track, fingerprint
from shijhon.playlists.requests import PlaylistRequest
from shijhon.playlists.service import Playlists
from tests.unit.test_agents import app_at, begin, call, finish
from tests.unit.test_playlist_explicit_tracks import ARTIST, SONGS, install
from tests.unit.test_playlist_requests import connected

BY_ID = {s.id: s for s in SONGS.values()}


def described(service: Playlists, native: dict[str, Any]) -> None:
    """Native playlist entries with their tags, as Navidrome lists them."""

    async def playlist(*args: Any) -> dict[str, Any]:
        data = dict(native)
        data["entry"] = [
            {
                "id": e["id"],
                **{k: getattr(BY_ID[e["id"]], k) for k in ("artist", "title", "album")},
            }
            for e in native.get("entry", [])
        ]
        return data

    service.client.playlist = AsyncMock(side_effect=playlist)  # type: ignore[method-assign]


def tracks(*titles: str) -> list[dict[str, str]]:
    return [{"artist": SONGS[t].artist if t in SONGS else ARTIST, "title": t} for t in titles]


async def connect(http: httpx.AsyncClient, scopes: str = "create status") -> str:
    return str((await finish(http, await begin(http, scopes), scopes))["access_token"])


async def latest(service: Playlists, ident: str) -> None:
    """Run the playlist's latest run, as the worker does."""
    await service.process((await service.repo.runs(ident))[0]["id"])


async def created(service: Playlists, http: httpx.AsyncClient, token: str, *titles: str) -> str:
    reply = await call(
        http,
        token,
        "create_playlist",
        {
            "idempotency_key": "create-" + "-".join(t.split()[0] for t in titles),
            "request": {
                "name": "Lantern evening",
                "prompt": "calm songs for a lantern walk",
                "count": len(titles),
                "tracks": tracks(*titles),
            },
        },
    )
    assert reply["ok"], reply
    ident = str(reply["request"]["id"])
    await latest(service, ident)
    return ident


def test_an_agent_curated_playlist_is_replaced_in_place_with_its_brief(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http)
            ident = await created(app.service, http, token, "Night Bloom", "Lantern Hall")
            first = await call(http, token, "get_playlist_request", {"request_id": ident})
            assert first["request"]["kind"] == "agent_curated"
            assert first["request"]["brief"] == "calm songs for a lantern walk"
            assert [s["song_id"] for s in first["request"]["selected"]] == ["n1", "n2"]
            args = {
                "request_id": ident,
                "idempotency_key": "replace-1",
                "tracks": tracks("Grey Tide", "Night Bloom", "Unheard Song"),
                "brief": "calm songs for a lantern walk, fresh each week",
            }
            replaced = await call(http, token, "replace_playlist_tracks", args)
            assert replaced["ok"] and replaced["replaced"], replaced
            # The same key and tracks again: nothing new; other tracks under it conflict.
            again = await call(http, token, "replace_playlist_tracks", args)
            assert again["ok"] and not again["replaced"]
            other = {**args, "tracks": tracks("Paper Moths")}
            assert (await call(http, token, "replace_playlist_tracks", other))["error"][
                "code"
            ] == "conflict"
            await latest(app.service, ident)
            status = (await call(http, token, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert status["state"] == "published", status["error"]
            assert status["playlist_id"] == native["id"] == "explicit-playlist"
            assert [e["id"] for e in native["entry"]] == ["n3", "n1"]
            assert status["brief"] == "calm songs for a lantern walk, fresh each week"
            assert status["tracks"][2]["reason"] == "not_found"
            # A gap is closed on the same playlist.
            added = await call(
                http,
                token,
                "add_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "add-after-replace",
                    "tracks": [{"artist": "Mira Sole", "title": "Paper Moths", "replaces": 3}],
                },
            )
            assert added["ok"] and added["added"], added
            await latest(app.service, ident)
            assert [e["id"] for e in native["entry"]] == ["n3", "n1", "n4"]

    anyio.run(run)


def test_replacing_needs_create_permission_and_the_owner_s_own_playlist(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http)
            ident = await created(app.service, http, token, "Night Bloom")
            reader = await connect(http, "status")
            args = {
                "request_id": ident,
                "idempotency_key": "replace-denied",
                "tracks": tracks("Grey Tide"),
            }
            denied = await call(http, reader, "replace_playlist_tracks", args)
            assert denied["error"]["code"] == "permission_required"
            assert denied["error"]["missing_permissions"] == ["create"]
            await app.service.repo.connect(connected("another-owner"))
            elsewhere = await app.service.repo.create("another-owner", Definition())
            for tool, extra in (
                ("replace_playlist_tracks", {}),
                ("edit_playlist_tracks", {"remove": ["n1"]}),
                ("get_playlist_request", None),
            ):
                values = (
                    {"request_id": elsewhere}
                    if extra is None
                    else {**args, "request_id": elsewhere, **extra}
                )
                if tool == "edit_playlist_tracks":
                    values.pop("tracks")
                answer = await call(http, token, tool, values)
                assert answer["error"]["code"] == "not_found", (tool, answer)
            assert len(await app.service.repo.runs(ident)) == 1

    anyio.run(run)


def test_songs_the_user_removed_stay_out_of_a_replacement(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http, "create status library")
            ident = await created(app.service, http, token, "Night Bloom", "Lantern Hall")
            described(app.service, native)
            # The user removes a song in an app; an agent-curated playlist then replaces.
            native["entry"] = [{"id": "n2"}]
            native["changed"] = "edited in an app"
            replaced = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-after-edit",
                    "tracks": tracks("Night Bloom", "Grey Tide"),
                },
            )
            assert replaced["ok"], replaced
            await latest(app.service, ident)
            status = (await call(http, token, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n3"]
            assert status["tracks"][0]["reason"] == "removed_by_user"
            removed = status["user_edits"]["removed"]
            assert [(s["song_id"], s["title"]) for s in removed] == [("n1", "Night Bloom")]
            # Forgetting the edits lets it back in.
            forgot = await call(
                http,
                token,
                "manage_playlist_refresh",
                {"request_id": ident, "action": "forget_edits"},
            )
            assert forgot["forgotten"] == 1
            again = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-after-forget",
                    "tracks": tracks("Night Bloom", "Grey Tide"),
                },
            )
            assert again["ok"], again
            await latest(app.service, ident)
            assert [e["id"] for e in native["entry"]] == ["n1", "n3"]

    anyio.run(run)


def test_a_described_playlist_keeps_its_instructions_and_schedule(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            # A described weekly playlist made on the dashboard, published earlier.
            await service.repo.connect(
                connected(backend="api", endpoint="https://model.invalid/v1", model="synthetic")
            )
            spec = PlaylistRequest(
                name="Lantern evening",
                prompt="quiet lantern songs",
                count=2,
                save_instructions=True,
                cadence="weekly",
            )
            ident, _ = await service.requests.create("owner", "dashboard-key", spec)
            native.update(id="explicit-playlist", name="Lantern evening", owner="owner")
            native["entry"], native["changed"] = [{"id": "n1"}, {"id": "n2"}], "x"
            run_id = (await service.repo.runs(ident))[0]["id"]
            data = {
                "definition": spec.definition().model_dump(),
                "selected": [SONGS["Night Bloom"].model_dump(), SONGS["Lantern Hall"].model_dump()],
                "native_ids": ["n1", "n2"],
            }
            await service.repo.update(run_id, "published", data)
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET playlist_id=?,published=?,enabled=1 WHERE id=?",
                [native["id"], fingerprint(native), ident],
            )
            token = await connect(http)
            listed = await call(http, token, "list_playlist_requests")
            assert [(p["id"], p["kind"], p["made_by"]) for p in listed["playlists"]] == [
                (ident, "described", "dashboard")
            ]
            brief = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-described-1",
                    "tracks": tracks("Grey Tide"),
                    "brief": "something else",
                },
            )
            assert "brief" in brief["error"]["message"]
            replaced = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-described-2",
                    "tracks": tracks("Grey Tide", "Night Bloom", "Lantern Hall"),
                },
            )
            assert replaced["ok"], replaced
            await latest(service, ident)
            status = (await call(http, token, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n3", "n1", "n2"]
            mix = await service.repo.mix(ident, "owner")
            assert Definition.model_validate_json(mix["definition"]) == spec.definition()
            assert mix["enabled"] and status["refreshes"]
            # Its library outcomes stay hidden from an agent without library permission.
            assert {t["outcome"] for t in status["tracks"]} == {"resolved"}
            assert all(s["source"] == "" for s in status["selected"])
            # Server refreshes still need server AI; an agent-curated one has none.
            agent = await created(service, http, token, "Paper Moths")
            refused = await call(
                http, token, "manage_playlist_refresh", {"request_id": agent, "action": "refresh"}
            )
            assert "replace_playlist_tracks" in refused["error"]["message"]

    anyio.run(run)


def test_an_edit_removes_and_reorders_and_remembers_the_removal(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            token = await connect(http)
            ident = await created(service, http, token, "Night Bloom", "Lantern Hall", "Grey Tide")
            described(service, native)

            async def edit(key: str, **values: Any) -> dict[str, Any]:
                return dict(
                    await call(
                        http,
                        token,
                        "edit_playlist_tracks",
                        {"request_id": ident, "idempotency_key": key, **values},
                    )
                )

            unknown = await edit("edit-unknown", remove=["n4"])
            assert "not in the playlist" in unknown["error"]["message"]
            short = await edit("edit-short", remove=["n2"], order=["n3"])
            assert "remaining songs" in short["error"]["message"]
            done = await edit("edit-first", remove=["n2"], order=["n3", "n1"])
            assert done["ok"] and done["edited"], done
            assert (await edit("edit-first", remove=["n2"], order=["n3", "n1"]))["edited"] is False
            await latest(service, ident)
            status = (await call(http, token, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert status["state"] == "published", status["error"]
            assert [e["id"] for e in native["entry"]] == ["n3", "n1"]
            assert [s["song_id"] for s in status["selected"]] == ["n3", "n1"]
            removed = status["user_edits"]["removed"]
            assert [s["song_id"] for s in removed] == ["n2"]
            mix = await service.repo.mix(ident, "owner")
            titles = [t["title"] for t in json.loads(mix["definition"])["tracks"]]
            assert titles == ["Grey Tide", "Night Bloom"]
            # The removed song stays out of a later replacement.
            replaced = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-after-edit",
                    "tracks": tracks("Lantern Hall", "Night Bloom"),
                },
            )
            assert replaced["ok"]
            await latest(service, ident)
            assert [e["id"] for e in native["entry"]] == ["n1"]
            emptied = await edit("edit-second", remove=["n1"])
            assert "Keep at least one song" in emptied["error"]["message"]

    anyio.run(run)


def test_an_edit_sent_before_an_app_change_is_not_applied(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            native = install(service)
            token = await connect(http)
            ident = await created(service, http, token, "Night Bloom", "Lantern Hall")
            described(service, native)
            done = await call(
                http,
                token,
                "edit_playlist_tracks",
                {"request_id": ident, "idempotency_key": "edit-race", "order": ["n2", "n1"]},
            )
            assert done["ok"], done
            native["entry"], native["changed"] = [{"id": "n1"}], "edited meanwhile"
            await latest(service, ident)
            status = await service.requests.status("owner", ident)
            assert status["state"] == "failed" and "changed after this edit" in status["error"]
            assert native["entry"] == [{"id": "n1"}]

    anyio.run(run)


@pytest.mark.parametrize("action", ["publish", "discard"])
def test_an_agent_publishes_or_discards_a_preview(tmp_path: Path, action: str) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http)
            reply = await call(
                http,
                token,
                "create_playlist",
                {
                    "idempotency_key": "preview-key",
                    "request": {
                        "name": "Lantern evening",
                        "count": 1,
                        "tracks": tracks("Night Bloom"),
                        "preview": True,
                    },
                },
            )
            ident = reply["request"]["id"]
            await latest(app.service, ident)
            status = (await call(http, token, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert status["state"] == "preview" and not native
            review = {"request_id": ident, "run_id": "not-the-run", "action": action}
            stale = await call(http, token, "review_playlist_preview", review)
            assert stale["error"]["code"] == "conflict"
            reader = await connect(http, "status")
            review["run_id"] = status["run"]
            denied = await call(http, reader, "review_playlist_preview", review)
            assert denied["error"]["code"] == "permission_required"
            done = await call(http, token, "review_playlist_preview", review)
            assert done["ok"], done
            await latest(app.service, ident)
            final = await app.service.requests.status("owner", ident)
            if action == "publish":
                assert final["state"] == "published" and native["entry"] == [{"id": "n1"}]
            else:
                assert final["state"] == "discarded" and not native
            again = await call(http, token, "review_playlist_preview", review)
            assert again["error"]["code"] == "conflict"

    anyio.run(run)


def test_playlists_of_other_agents_are_visible_with_their_library_details_gated(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            first = await connect(http, "create status library")
            ident = await created(app.service, http, first, "Night Bloom")
            own = (await call(http, first, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert own["made_by"] == "this_agent" and own["tracks"][0]["outcome"] == "library"
            second = await connect(http, "create status")
            listed = await call(http, second, "list_playlist_requests", {"kind": "agent_curated"})
            assert [(p["id"], p["made_by"]) for p in listed["playlists"]] == [
                (ident, "another_agent")
            ]
            assert (await call(http, second, "list_playlist_requests", {"kind": "import"}))[
                "playlists"
            ] == []
            # Without library permission its songs stay hidden; its state does not.
            seen = (await call(http, second, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert seen["state"] == "published" and seen["selected"] == []
            assert "tracks" not in seen and "published_songs" not in seen
            assert "user_edits" not in seen and "library permission" in seen["songs_hidden"]
            # It can manage the other agent's playlist with its own grant.
            replaced = await call(
                http,
                second,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "second-agent",
                    "tracks": tracks("Lantern Hall"),
                },
            )
            assert replaced["ok"], replaced
            # Its own grant authorizes the run: the first agent's revocation does not stop it.
            grant = (await app.oauth.verify_token(first)).claims["grant_id"]
            await app.oauth.store.execute(
                "UPDATE playlist_agent_tokens SET revoked=1 WHERE id=?", [grant]
            )
            await latest(app.service, ident)
            status = await app.service.requests.status("owner", ident)
            assert status["state"] == "published", status["error"]
            # Its own change it reads back, without telling library songs apart.
            mine = (await call(http, second, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert mine["tracks"][0]["outcome"] == "resolved"
            assert mine["resolution"] == {"requested": 1, "resolved": 1, "unresolved": 0}
            assert [s["song_id"] for s in mine["published_songs"]] == ["n2"]
            assert "user_edits" not in mine

    anyio.run(run)


def test_discovery_mixes_need_history_permission_and_last_fm(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            token = await connect(http, "create status")
            mix = {"name": "Night discoveries", "count": 5, "unfamiliar_percent": 60}
            args = {"mix": mix, "idempotency_key": "discovery-1"}
            denied = await call(http, token, "create_discovery_mix", args)
            assert denied["error"]["missing_permissions"] == ["history"]
            token = await connect(http, "create status history")
            caps = await call(http, token, "get_capabilities")
            assert not caps["features"]["discovery_mix"]["configured"]
            missing = await call(http, token, "create_discovery_mix", args)
            assert missing["error"]["missing_configuration"] == ["listening_history"]
            await service.repo.connect(
                connected(history_user="listener", history_key=SecretStr("invented-key"))
            )
            assert (await call(http, token, "get_capabilities"))["features"]["discovery_mix"][
                "ready"
            ]
            weekly = {**args, "mix": {**mix, "enable_refreshes": True}}
            assert (
                "cadence"
                in (await call(http, token, "create_discovery_mix", weekly))["error"]["message"]
            )
            made = await call(http, token, "create_discovery_mix", args)
            assert made["ok"] and made["created"], made
            ident = made["request"]["id"]
            assert made["request"]["kind"] == "discovery" and made["request"]["state"] == "queued"
            replay = await call(http, token, "create_discovery_mix", args)
            assert replay["request"]["id"] == ident and not replay["created"]
            # Its rules change and its pending selection starts again.
            await call(
                http, token, "manage_playlist_refresh", {"request_id": ident, "action": "pause"}
            )
            changed = await call(
                http,
                token,
                "update_discovery_mix",
                {"request_id": ident, "mix": {**mix, "count": 8, "cadence": "weekly"}},
            )
            assert changed["ok"], changed
            runs = await service.repo.runs(ident)
            assert [r["state"] for r in runs] == ["queued", "discarded"]
            spec = Definition.model_validate_json(
                (await service.repo.mix(ident, "owner"))["definition"]
            )
            assert spec.count == 8 and spec.cadence == "weekly" and spec.mode == "discovery"
            early = await call(
                http, token, "manage_playlist_refresh", {"request_id": ident, "action": "resume"}
            )
            assert "Publish" in early["error"]["message"]
            busy = await call(
                http, token, "manage_playlist_refresh", {"request_id": ident, "action": "refresh"}
            )
            assert busy["error"]["code"] == "conflict"
            listed = await call(http, token, "list_playlist_requests", {"kind": "discovery"})
            assert [p["id"] for p in listed["playlists"]] == [ident]
            # Without history permission its songs are not shown.
            reader = await connect(http, "status")
            seen = (await call(http, reader, "get_playlist_request", {"request_id": ident}))[
                "request"
            ]
            assert seen["selected"] == [] and seen["rules"]["unfamiliar_percent"] == 60

    anyio.run(run)


def test_an_import_is_matched_reviewed_and_published_by_an_agent(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            install(service)
            choice = Track(id="n4", artist="Mira Sole", title="Paper Moths", album="Shore")

            async def resolve(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track | None:
                if wanted["title"] == "Paper Moths":
                    kw["ambiguous"].extend([choice, choice.model_copy(update={"id": "n9"})])
                    return None
                return SONGS.get(wanted["title"])

            service.resolver.resolve = resolve  # type: ignore[method-assign]
            args = {
                "name": "Imported lanterns",
                "idempotency_key": "import-1",
                "lines": "Velane - Night Bloom\nMira Sole - Paper Moths\nNobody - Unheard",
            }
            token = await connect(http, "create status")
            denied = await call(http, token, "import_playlist", args)
            assert denied["error"]["missing_permissions"] == ["library"]
            token = await connect(http, "create status library")
            both = await call(
                http, token, "import_playlist", {**args, "entries": [{"artist": "A", "title": "B"}]}
            )
            assert not both["ok"]
            started = await call(http, token, "import_playlist", args)
            assert started["ok"], started
            ident = started["request"]["id"]
            assert started["request"]["kind"] == "import"
            await latest(service, ident)
            review = await call(http, token, "get_playlist_import", {"request_id": ident})
            assert review["state"] == "preview"
            assert [e["state"] for e in review["entries"]] == ["matched", "ambiguous", "unresolved"]
            assert [c["song_id"] for c in review["entries"][1]["choices"]] == ["n4", "n9"]
            missing = await call(
                http, token, "get_playlist_import", {"request_id": ident, "only_missing": True}
            )
            assert [e["entry"] for e in missing["entries"]] == [2, 3]
            act = {"request_id": ident, "run_id": review["run"], "review_key": review["review_key"]}
            chosen = await call(
                http,
                token,
                "review_playlist_import",
                {**act, "action": "choose", "entry": 2, "choice": "n4"},
            )
            assert chosen["ok"], chosen
            await latest(service, ident)
            review = await call(http, token, "get_playlist_import", {"request_id": ident})
            assert [e["state"] for e in review["entries"]] == ["matched", "matched", "unresolved"]
            act["review_key"] = review["review_key"]
            unconfirmed = await call(
                http, token, "review_playlist_import", {**act, "action": "publish"}
            )
            assert "confirm" in unconfirmed["error"]["message"]
            published = await call(
                http,
                token,
                "review_playlist_import",
                {**act, "action": "publish", "missing_count": 1},
            )
            assert published["ok"], published
            assert published["request"]["state"] == "publishing"
            assert published["request"]["missing"] == 1
            # Replacing or editing an import goes through its own review instead.
            refused = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-import",
                    "tracks": tracks("Grey Tide"),
                },
            )
            assert "import review" in refused["error"]["message"]

    anyio.run(run)


def test_a_replacement_stops_when_its_agent_is_revoked_before_it_runs(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http)
            ident = await created(app.service, http, token, "Night Bloom")
            second = await connect(http)
            replaced = await call(
                http,
                second,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "revoked-agent",
                    "tracks": tracks("Grey Tide"),
                },
            )
            assert replaced["ok"], replaced
            grant = (await app.oauth.verify_token(second)).claims["grant_id"]
            await app.oauth.store.execute(
                "UPDATE playlist_agent_tokens SET revoked=1 WHERE id=?", [grant]
            )
            await latest(app.service, ident)
            status = await app.service.requests.status("owner", ident)
            assert status["state"] == "failed" and "revoked" in status["error"]
            assert native["entry"] == [{"id": "n1"}]

    anyio.run(run)


def test_capabilities_present_agent_curation_first(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            token = await connect(http)
            caps = await call(http, token, "get_capabilities")
            curated = caps["features"]["agent_curated_playlist"]
            assert curated["ready"] and "replace_playlist_tracks" in curated["tools"]
            assert caps["features"]["playlist_management"]["ready"]
            assert not caps["features"]["playlist_import"]["permitted"]
            assert not caps["features"]["description_playlist"]["ready"]
            assert caps["recurrence"]["described_playlists_need"] == "server_ai"
            assert "main path" in caps["notes"][0]
            assert "replace_playlist_tracks" in (app.mcp.instructions or "")

    anyio.run(run)
