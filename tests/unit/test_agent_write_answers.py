"""What an agent reads after it changes a playlist: the answer says what is finished and
what to call next, errors say what to pass, a discarded run leaves a live playlist live.
Invented music and accounts only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.agents import playlists as agent_playlists
from shijhon.playlists.model import Track
from shijhon.playlists.requests import PlaylistRequest, digest
from tests.unit.test_agent_playlist_management import connect, created, latest, tracks
from tests.unit.test_agents import app_at, call
from tests.unit.test_playlist_explicit_tracks import SONGS, install


async def read(http: Any, token: str, ident: str) -> dict[str, Any]:
    return (await call(http, token, "get_playlist_request", {"request_id": ident}))["request"]


async def listed(http: Any, token: str, ident: str) -> dict[str, Any]:
    playlists = (await call(http, token, "list_playlist_requests"))["playlists"]
    return next(p for p in playlists if p["id"] == ident)


def titles(songs: list[dict[str, Any]]) -> list[str]:
    return [s["title"] for s in songs]


def test_a_queued_change_does_not_answer_with_the_old_songs(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http)
            first = await call(
                http,
                token,
                "create_playlist",
                {
                    "idempotency_key": "create-first",
                    "request": {"name": "Lanterns", "tracks": tracks("Night Bloom")},
                },
            )
            request = first["request"]
            assert first["created"] and request["state"] == "queued"
            assert request["playlist_id"] == "" and request["live"] is False
            assert "get_playlist_request" in request["next"] and request["id"] in request["next"]
            assert "playlist_id stays empty" in request["next"]
            ident = request["id"]
            await latest(app.service, ident)

            args = {
                "request_id": ident,
                "idempotency_key": "replace-pending",
                "tracks": tracks("Grey Tide"),
            }
            replaced = await call(http, token, "replace_playlist_tracks", args)
            pending = replaced["request"]
            assert pending["state"] == "queued" and pending["live"] is True
            assert "published_songs" not in pending
            assert "get_playlist_request" in pending["next"]
            assert "keeps its songs" in pending["next"]
            # A read still shows what is live, and the replay of the key is the same answer.
            status = await read(http, token, ident)
            assert titles(status["published_songs"]) == ["Night Bloom"]
            again = await call(http, token, "replace_playlist_tracks", args)
            assert again["ok"] and not again["replaced"]
            assert "published_songs" not in again["request"]
            assert len(await app.service.repo.runs(ident)) == 2
            # Finished: the answer has no next step and the read shows the new songs.
            await latest(app.service, ident)
            done = await read(http, token, ident)
            assert done["state"] == "published" and "next" not in done
            assert titles(done["published_songs"]) == ["Grey Tide"]

    anyio.run(run)


def test_a_change_waits_for_a_quick_run_and_answers_with_its_result(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http)
            async with anyio.create_task_group() as worker:
                worker.start_soon(app.service.run)
                await anyio.sleep(0.05)
                made = await call(
                    http,
                    token,
                    "create_playlist",
                    {
                        "idempotency_key": "create-waited",
                        "request": {"name": "Lanterns", "tracks": tracks("Night Bloom")},
                    },
                )
                assert made["request"]["state"] == "published", made
                assert made["request"]["playlist_id"] == native["id"]
                assert titles(made["request"]["published_songs"]) == ["Night Bloom"]
                assert "next" not in made["request"]
                worker.cancel_scope.cancel()
            assert app.service.working is False

    anyio.run(run)


def test_a_slow_run_ends_the_wait_with_the_way_to_check_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_playlists, "WRITE_WAIT", 0.3)

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http)
            # The worker is taken by other work: the change stays queued, and durable.
            app.service.working = True
            with anyio.fail_after(5):
                made = await call(
                    http,
                    token,
                    "create_playlist",
                    {
                        "idempotency_key": "create-slow",
                        "request": {"name": "Lanterns", "tracks": tracks("Night Bloom")},
                    },
                )
            assert made["created"] and made["request"]["state"] == "queued"
            assert "get_playlist_request" in made["request"]["next"]
            app.service.working = False
            await latest(app.service, made["request"]["id"])
            assert (await read(http, token, made["request"]["id"]))["state"] == "published"

    anyio.run(run)


def test_the_wait_leaves_the_changes_free_for_the_worker(tmp_path: Path) -> None:
    """The worker needs the changes to take up the run this answer waits for."""

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http)
            app.service.working = True
            answer: dict[str, Any] = {}

            async def create() -> None:
                answer.update(
                    await call(
                        http,
                        token,
                        "create_playlist",
                        {
                            "idempotency_key": "create-free",
                            "request": {"name": "Lanterns", "tracks": tracks("Night Bloom")},
                        },
                    )
                )

            async with anyio.create_task_group() as group:
                group.start_soon(create)
                with anyio.fail_after(5):
                    while not (  # noqa: ASYNC110 (a row in the database signals nothing)
                        rows := await app.service.repo.store.fetchall(
                            "SELECT id FROM playlist_runs"
                        )
                    ):
                        await anyio.sleep(0.01)
                await app.service.process(rows[0]["id"])
            assert answer["request"]["state"] == "published", answer

    anyio.run(run)


def test_the_limit_error_names_the_artists_and_the_parameter(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http)
            ident = await created(app.service, http, token, "Night Bloom", "Grey Tide")
            same = [{"artist": "Tomas Reed", "title": t} for t in ("One", "Two", "Three")]
            refused = await call(
                http,
                token,
                "replace_playlist_tracks",
                {"request_id": ident, "idempotency_key": "replace-over", "tracks": same},
            )
            message = refused["error"]["message"]
            assert "Tomas Reed (3 tracks)" in message and "which is 2" in message
            assert "Pass a higher max_per_artist, up to 10" in message
            allowed = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-higher",
                    "tracks": same,
                    "max_per_artist": 3,
                },
            )
            assert allowed["ok"], allowed

            other = await created(app.service, http, token, "Lantern Hall", "Paper Moths")
            added = await call(
                http,
                token,
                "add_playlist_tracks",
                {"request_id": other, "idempotency_key": "add-over", "tracks": same},
            )
            message = added["error"]["message"]
            assert "Tomas Reed (3 tracks)" in message and "replace_playlist_tracks" in message

            at_most = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": other,
                    "idempotency_key": "replace-eleven",
                    "tracks": [{"artist": "Tomas Reed", "title": f"T{n}"} for n in range(11)],
                    "max_per_artist": 10,
                },
            )
            assert "10 is the highest limit" in at_most["error"]["message"]

    anyio.run(run)


def test_a_new_request_over_the_limit_says_the_same() -> None:
    same = [{"artist": "Tomas Reed", "title": t} for t in ("One", "Two", "Three")]
    with pytest.raises(ValueError, match=r"Tomas Reed \(3 tracks\).*higher max_per_artist"):
        PlaylistRequest(name="N", tracks=same)
    assert PlaylistRequest(name="N", tracks=same, max_per_artist=3).count == 3


def test_explicit_tracks_are_their_own_count_and_keep_their_replay_hash() -> None:
    songs = [{"artist": "A", "title": "One"}, {"artist": "B", "title": "Two"}]
    inferred = PlaylistRequest(name="N", tracks=songs)
    assert inferred.count == 2 and inferred.definition().count == 2
    assert PlaylistRequest(name="N", tracks=songs, count=30).count == 2
    # An earlier request that sent the right count replays under the same hash.
    assert digest(PlaylistRequest(name="N", tracks=songs, count=2).canonical()) == digest(
        inferred.canonical()
    )
    assert PlaylistRequest(prompt="quiet").count == 30


def test_tool_descriptions_state_the_defaults_and_the_request_id(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (_, http):
            token = await connect(http)
            reply = await http.post(
                "/shijhon/mcp",
                headers={
                    "authorization": "Bearer " + token,
                    "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            described = {t["name"]: t["description"] for t in reply.json()["result"]["tools"]}
            create = " ".join(described["create_playlist"].split())
            assert "max_per_artist defaults to 2" in create
            assert "`count` is ignored" in create and "inside `request`" in create
            add = " ".join(described["add_playlist_tracks"].split())
            assert "not a Navidrome playlist ID" in add
            importing = " ".join(described["import_playlist"].split())
            assert "in the background" in importing and "get_playlist_import" in importing

    anyio.run(run)


def test_a_discarded_preview_leaves_a_live_playlist_reading_as_live(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http)
            ident = await created(app.service, http, token, "Night Bloom", "Lantern Hall")
            live = await read(http, token, ident)
            preview = await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-preview",
                    "tracks": tracks("Grey Tide"),
                    "preview": True,
                    "brief": "the brief of the preview",
                },
            )
            assert preview["ok"], preview
            await latest(app.service, ident)
            waiting = await read(http, token, ident)
            assert waiting["state"] == "preview" and waiting["live"] is True
            assert "review_playlist_preview" in waiting["next"]
            assert titles(waiting["selected"]) == ["Grey Tide"]
            assert titles(waiting["published_songs"]) == ["Night Bloom", "Lantern Hall"]
            assert waiting["brief"] == live["brief"]

            answer = await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": ident, "run_id": waiting["run"], "action": "discard"},
            )
            for status in (answer["request"], await read(http, token, ident)):
                assert status["state"] == "published" and status["live"] is True
                assert status["run"] == live["run"] and "next" not in status
                assert status["brief"] == live["brief"]
                assert titles(status["selected"]) == ["Night Bloom", "Lantern Hall"]
                assert titles(status["published_songs"]) == ["Night Bloom", "Lantern Hall"]
                assert [t["input"]["title"] for t in status["tracks"]] == [
                    "Night Bloom",
                    "Lantern Hall",
                ]
                assert status["discarded_run"] == {
                    "run": waiting["run"],
                    "selected": [{"artist": "Mira Sole", "title": "Grey Tide", "album": "Shore"}],
                    "brief": "the brief of the preview",
                }
            summary = await listed(http, token, ident)
            assert summary["state"] == "published" and summary["live"] is True
            assert summary["latest_run_state"] == "discarded"
            assert [e["id"] for e in native["entry"]] == ["n1", "n2"]
            # Nothing is waiting any more: the discarded run cannot be published.
            stale = await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": ident, "run_id": waiting["run"], "action": "publish"},
            )
            assert stale["error"]["code"] == "conflict"

    anyio.run(run)


def test_an_accepted_preview_and_a_never_published_one_read_as_they_are(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http)
            ident = await created(app.service, http, token, "Night Bloom")
            await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-accepted",
                    "tracks": tracks("Grey Tide"),
                    "preview": True,
                    "brief": "a new brief",
                },
            )
            await latest(app.service, ident)
            waiting = await read(http, token, ident)
            accepted = await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": ident, "run_id": waiting["run"], "action": "publish"},
            )
            assert accepted["request"]["state"] == "publishing"
            assert "published_songs" not in accepted["request"]
            await latest(app.service, ident)
            status = await read(http, token, ident)
            assert status["state"] == "published" and status["brief"] == "a new brief"
            assert titles(status["published_songs"]) == ["Grey Tide"]
            assert "discarded_run" not in status
            assert [e["id"] for e in native["entry"]] == ["n3"]

            never = await call(
                http,
                token,
                "create_playlist",
                {
                    "idempotency_key": "create-never",
                    "request": {"name": "Never", "tracks": tracks("Lantern Hall"), "preview": True},
                },
            )
            other = never["request"]["id"]
            await latest(app.service, other)
            first = await read(http, token, other)
            gone = await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": other, "run_id": first["run"], "action": "discard"},
            )
            assert gone["request"]["state"] == "discarded" and gone["request"]["live"] is False
            assert "nothing was published" in gone["request"]["next"]
            assert "discarded_run" not in gone["request"]
            summary = await listed(http, token, other)
            assert summary["state"] == "discarded" and summary["live"] is False

    anyio.run(run)


def test_a_failed_change_stays_failed_beside_the_live_songs(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http)
            ident = await created(app.service, http, token, "Night Bloom")
            await call(
                http,
                token,
                "replace_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "replace-fails",
                    "tracks": tracks("Unheard Song"),
                    "require_all": True,
                },
            )
            await latest(app.service, ident)
            status = await read(http, token, ident)
            assert status["state"] == "failed" and status["live"] is True
            assert "retry_playlist_request" in status["next"]
            assert "keeps its songs" in status["next"]
            assert titles(status["published_songs"]) == ["Night Bloom"]

    anyio.run(run)


def test_a_discarded_addition_does_not_come_back_with_the_next_one(tmp_path: Path) -> None:
    """Added tracks are saved when asked for, so discarding them has to give back the list:
    the live playlist never had them."""

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            native = install(app.service)
            token = await connect(http)
            made = await call(
                http,
                token,
                "create_playlist",
                {
                    "idempotency_key": "create-reviewed",
                    "request": {
                        "name": "Reviewed",
                        "prompt": "reviewed first",
                        "tracks": tracks("Night Bloom", "Unheard Song"),
                        "preview": True,
                    },
                },
            )
            ident = made["request"]["id"]
            await latest(app.service, ident)
            first = await read(http, token, ident)
            await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": ident, "run_id": first["run"], "action": "publish"},
            )
            await latest(app.service, ident)
            addition = await call(
                http,
                token,
                "add_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "add-discarded",
                    "tracks": [{"artist": "Mira Sole", "title": "Grey Tide"}],
                },
            )
            assert addition["ok"], addition
            await latest(app.service, ident)
            waiting = await read(http, token, ident)
            assert waiting["state"] == "preview"
            assert (await read(http, token, ident))["count"] == 3
            # Changed since the addition was asked for: discarding it leaves this alone.
            await app.service.repo.store.execute(
                "UPDATE playlist_mixes SET definition="
                "json_set(definition,'$.max_per_artist',5) WHERE id=?",
                [ident],
            )
            answer = await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": ident, "run_id": waiting["run"], "action": "discard"},
            )
            status = answer["request"]
            assert status["state"] == "published" and status["count"] == 2
            assert titles(status["discarded_run"]["selected"]) == ["Night Bloom", "Grey Tide"]
            mix = await app.service.repo.mix(ident, "owner")
            saved = [t["title"] for t in json.loads(mix["definition"])["tracks"]]
            assert saved == ["Night Bloom", "Unheard Song"]
            assert json.loads(mix["definition"])["max_per_artist"] == 5
            # A position of the discarded list is not one of this playlist.
            beyond = await call(
                http,
                token,
                "add_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "add-beyond",
                    "tracks": [{"artist": "Orrin Vale", "title": "Lantern Hall", "replaces": 3}],
                },
            )
            assert "not a left-out track" in beyond["error"]["message"]
            next_one = await call(
                http,
                token,
                "add_playlist_tracks",
                {
                    "request_id": ident,
                    "idempotency_key": "add-next",
                    "tracks": [{"artist": "Orrin Vale", "title": "Lantern Hall", "replaces": 2}],
                },
            )
            assert next_one["ok"], next_one
            await latest(app.service, ident)
            preview = await read(http, token, ident)
            assert titles(preview["selected"]) == ["Night Bloom", "Lantern Hall"]
            await call(
                http,
                token,
                "review_playlist_preview",
                {"request_id": ident, "run_id": preview["run"], "action": "publish"},
            )
            await latest(app.service, ident)
            assert [e["id"] for e in native["entry"]] == ["n1", "n2"]

    anyio.run(run)


def test_an_import_says_matching_runs_and_gives_a_key_only_when_it_holds(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            install(service)

            async def resolve(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track | None:
                return SONGS.get(wanted["title"])

            service.resolver.resolve = resolve  # type: ignore[method-assign]
            token = await connect(http, "create status library")
            started = await call(
                http,
                token,
                "import_playlist",
                {
                    "name": "Imported",
                    "idempotency_key": "import-key-1",
                    "lines": "Velane - Night Bloom\nNobody - Unheard",
                },
            )
            request = started["request"]
            assert request["state"] == "queued" and request["review_key"] == ""
            assert "in the background" in request["next"]
            assert "get_playlist_import" in request["next"]
            ident = request["id"]
            matching = await call(http, token, "get_playlist_import", {"request_id": ident})
            assert matching["review_key"] == "" and matching["total"] == 2
            assert matching["checked"] == 0 and "get_playlist_import" in matching["next"]
            # The run stays the same one from matching to review.
            assert matching["run"] == request["run"]

            await latest(service, ident)
            review = await call(http, token, "get_playlist_import", {"request_id": ident})
            assert review["state"] == "preview" and review["review_key"]
            assert review["checked"] == 2
            assert "missing_count 1" in review["next"] and "review_key" in review["next"]
            published = await call(
                http,
                token,
                "review_playlist_import",
                {
                    "request_id": ident,
                    "run_id": review["run"],
                    "review_key": review["review_key"],
                    "action": "publish",
                    "missing_count": 1,
                },
            )
            assert published["ok"], published
            assert published["request"]["state"] == "publishing"
            assert "get_playlist_import" in published["request"]["next"]

    anyio.run(run)


def test_an_import_stopped_while_matching_needs_no_key(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            install(app.service)
            token = await connect(http, "create status library")
            started = await call(
                http,
                token,
                "import_playlist",
                {"name": "Stopped", "idempotency_key": "import-stop", "lines": "A - One"},
            )
            request = started["request"]
            stopped = await call(
                http,
                token,
                "review_playlist_import",
                {
                    "request_id": request["id"],
                    "run_id": request["run"],
                    "review_key": request["review_key"],
                    "action": "stop",
                },
            )
            assert stopped["ok"] and stopped["request"]["state"] == "discarded", stopped

    anyio.run(run)


def test_an_entry_an_add_on_could_not_check_says_what_can_be_done(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            service = app.service
            install(service)
            silent = True

            async def resolve(connection: Any, wanted: dict[str, Any], **kw: Any) -> Track | None:
                found = SONGS.get(wanted["title"])
                if not found and silent:
                    kw["unanswered"].add("Slow Bot")
                return found

            service.resolver.resolve = resolve  # type: ignore[method-assign]
            token = await connect(http, "create status library")
            started = await call(
                http,
                token,
                "import_playlist",
                {
                    "name": "Partly",
                    "idempotency_key": "import-silent",
                    "lines": "Velane - Night Bloom\nNobody - Unheard\nNo One - Not Anywhere",
                },
            )
            ident = started["request"]["id"]
            await latest(service, ident)
            review = await call(http, token, "get_playlist_import", {"request_id": ident})
            matched, line, _ = review["entries"]
            assert "did not answer" not in matched["note"]
            assert "add-on Slow Bot did not answer" in line["note"]
            assert "missing_count" in line["note"] and "action retry" in line["note"]
            assert "Not found yet" not in line["note"]
            assert "Slow Bot" in review["next"] and "action retry" in review["next"]
            # The dashboard's words stay what they were.
            run_row = (await service.repo.runs(ident))[0]
            assert run_row["data"]["rows"][1]["note"].startswith("Not found yet: add-on Slow Bot")
            # Asking again works; with nothing silent the truth becomes a plain miss.
            silent = False
            act = {"request_id": ident, "run_id": review["run"], "review_key": review["review_key"]}
            retried = await call(http, token, "review_playlist_import", {**act, "action": "retry"})
            assert retried["ok"], retried
            await latest(service, ident)
            review = await call(http, token, "get_playlist_import", {"request_id": ident})
            assert "did not answer" not in review["entries"][1]["note"]
            assert "Slow Bot" not in review["next"]
            # Publishing without the lines passes their count.
            act["review_key"] = review["review_key"]
            published = await call(
                http,
                token,
                "review_playlist_import",
                {**act, "action": "publish", "missing_count": 2},
            )
            assert published["ok"] and published["request"]["missing"] == 2, published

    anyio.run(run)
