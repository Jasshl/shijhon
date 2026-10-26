"""Review regressions use synthetic accounts, music and local library files only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.dashboard.auth import Session
from shijhon.dashboard.playlist_requests import Pages
from shijhon.placeholders.engine import MaterializeError, PlaceholderEngine
from shijhon.placeholders.layout import Layout
from shijhon.placeholders.silence import SilenceMaker
from shijhon.playlists.api import API
from shijhon.playlists.model import Connection, PlaylistError, Suggestion, Track
from shijhon.playlists.prompt import generate
from shijhon.playlists.requests import PlaylistRequest, RequestError, digest
from shijhon.playlists.service import release_fingerprint
from shijhon.views.commits import Commits
from tests.harness.agents import agent_token
from tests.unit.test_playlist_requests import connected, service_at

REMASTER_TITLES = [
    "Song - 2011 Remaster",
    "Song \u2013 2011 Remaster",
    "Song \u2014 2011 Remaster",
    "Song (2011 Remaster)",
    "Song [2011 Remaster]",
    "Song \u2014 Digitally Remastered Special Anniversary Edition 2011",
    "Song \u2014 2011 Reissue",
]


def test_stale_preview_form_cannot_publish_latest_run(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            ident, _ = await service.requests.create(
                "owner", "preview-1", PlaylistRequest(prompt="quiet", count=1, preview=True)
            )
            first = (await service.repo.runs(ident))[0]
            await service.repo.update(first["id"], "discarded", {"selected": ["old"]})
            second = await service.repo.enqueue(ident, review=True)
            await service.repo.update(second, "preview", {"selected": ["new"]})
            dashboard = SimpleNamespace(
                services=SimpleNamespace(playlists=service), _see=lambda _: Response()
            )
            pages = Pages(dashboard)
            pages.render = AsyncMock(return_value=Response())
            request = Request({"type": "http", "method": "POST", "path_params": {"ident": ident}})
            request.state.form = {"action": "publish", "run": first["id"]}
            await pages.detail(request, Session("hash", "owner", "csrf", 0, "owner"))
            assert (await service.repo.run(second))["state"] == "preview"
            pages.render.assert_awaited_once()

    anyio.run(run)


@pytest.mark.parametrize("version", ["2011 Remaster", "2011Remaster"])
def test_explicit_remaster_cannot_resolve_to_another_year(tmp_path: Path, version: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.client.call = AsyncMock(
                return_value={
                    "searchResult3": {
                        "song": [{"id": "native", "artist": "A", "title": "Song (2024 Remaster)"}]
                    }
                }
            )
            spec = PlaylistRequest(
                count=1, tracks=[{"artist": "A", "title": "Song", "version": version}]
            )
            with pytest.raises(PlaylistError, match="explicit track"):
                await generate(service, connected(), spec.definition())

    anyio.run(run)


@pytest.mark.parametrize("title", REMASTER_TITLES)
def test_title_only_editions_cannot_select_another_version(tmp_path: Path, title: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            spec = PlaylistRequest(count=1, tracks=[{"artist": "A", "title": title}])
            assert spec.tracks[0].wanted()["required_title"] == title
            service.curator.api = AsyncMock(side_effect=AssertionError("no model call"))
            for alternative in ("Song (2024 Remaster)", "Song", "Song (Live)"):
                service.client.call = AsyncMock(
                    return_value={
                        "searchResult3": {
                            "song": [{"id": "other", "artist": "A", "title": alternative}]
                        }
                    }
                )
                with pytest.raises(PlaylistError, match="explicit track"):
                    await generate(service, connected(), spec.definition())
            # Literal qualifier evidence is accepted, including case/punctuation folding.
            equivalent = title.replace("\u2014", "-").replace("\u2013", "-").upper()
            service.client.call = AsyncMock(
                return_value={
                    "searchResult3": {
                        "song": [
                            {"id": "other", "artist": "A", "title": "Song (2024 Remaster)"},
                            {"id": "wanted", "artist": "A", "title": equivalent},
                        ]
                    }
                }
            )
            selected = (await generate(service, connected(), spec.definition())).selected
            assert selected[0].id == "wanted" and selected[0].required_title == title
            service.curator.api.assert_not_awaited()

    anyio.run(run)


@pytest.mark.parametrize("title", ["Song", "Song feat. Guest", "Song (with Guest)"])
def test_plain_titles_keep_recording_matching_without_edition_constraints(
    tmp_path: Path, title: str
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            assert not Suggestion(artist="A", title=title).wanted()["required_title"]
            service.client.call = AsyncMock(
                return_value={
                    "searchResult3": {
                        "song": [{"id": "native", "artist": "A", "title": "Song (2024 Remaster)"}]
                    }
                }
            )
            selected, _, _, _ = await generate(
                service,
                connected(),
                PlaylistRequest(count=1, tracks=[{"artist": "A", "title": title}]).definition(),
            )
            assert selected[0].id == "native" and not selected[0].required_title

    anyio.run(run)


@pytest.mark.parametrize("stage", ["preparation", "prepared", "recovery"])
@pytest.mark.parametrize("constraint", ["album", "version", *REMASTER_TITLES])
def test_required_constraints_survive_all_publisher_checks(
    tmp_path: Path, stage: str, constraint: str
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            spec = PlaylistRequest(
                count=1,
                tracks=[
                    {
                        "artist": "A",
                        "title": "Song" if constraint in ("album", "version") else constraint,
                        **(
                            {"album": "Required album"}
                            if constraint == "album"
                            else {"version": "2011 Remaster"}
                            if constraint == "version"
                            else {}
                        ),
                    }
                ],
            )
            ident, _ = await service.requests.create("owner", "album-intent", spec)
            mix = await service.repo.mix(ident, "owner")
            run_id = (await service.repo.runs(ident))[0]["id"]
            service.resolver.resolve = AsyncMock(
                return_value=Track(
                    id="native",
                    artist="A",
                    title=(
                        "Song"
                        if constraint == "album"
                        else "Song (2011 Remaster)"
                        if constraint == "version"
                        else constraint
                    ),
                    album="Required album",
                )
            )
            data = await service.generate(connected(), mix)
            if constraint in REMASTER_TITLES:
                assert data["selected"][0]["required_title"] == constraint
            changed = {
                "id": "native",
                "artist": "A",
                "title": "Song" if constraint == "album" else "Song (2024 Remaster)",
                "album": "Different album",
            }
            native = {"id": "playlist", "owner": "owner", "name": spec.name, "entry": [changed]}
            service.client.native.song = AsyncMock(return_value={"libraryId": 1})

            async def call(c: Connection, method: str, params: Any = ()) -> Any:
                if method == "getSong":
                    return {"song": changed}
                if method == "getPlaylists":
                    return {"playlists": {"playlist": [native]}}
                return {"playlist": native}

            service.client.call = call
            if stage != "preparation":
                data["native_ids"] = ["native"]
            if stage == "recovery":
                data.update(intent="create", created_id="playlist", provisional=spec.name)
            await service.repo.update(run_id, "publishing", data)
            with pytest.raises(PlaylistError, match=r"selected|selection"):
                await service.publish(connected(), mix, run_id, data)
            assert (await service.repo.run(run_id))["state"] != "published"

    anyio.run(run)


@pytest.mark.parametrize("preview", [False, True])
def test_explicit_retry_of_failed_automatic_selection_runs_once_with_schedule_paused(
    tmp_path: Path, preview: bool
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            spec = PlaylistRequest(
                prompt="quiet", count=1, save_instructions=True, cadence="daily", preview=preview
            )
            ident, _ = await service.requests.create("owner", "scheduled-retry", spec)
            first = (await service.repo.runs(ident))[0]
            await service.repo.update(first["id"], "published", first["data"])
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET enabled=1 WHERE id=?", [ident]
            )
            scheduled = await service.repo.enqueue(ident, automatic=True, enable_after=True)
            frozen = await service.freeze(
                spec.definition(),
                [Track(id="native", artist="A", title="Song")],
                "",
                "",
                history_from=0,
                history_count=0,
                skipped=0,
            )
            service.generate = AsyncMock(side_effect=[PlaylistError("Synthetic failure."), frozen])

            async def publish(c: Connection, mix: Any, run_id: str, data: Any) -> None:
                assert data["enable_after"] is False
                await service.repo.update(run_id, "published", data)

            service.publish = AsyncMock(side_effect=publish)
            await service.process(scheduled)
            assert (await service.repo.run(scheduled))["state"] == "failed"
            assert not (await service.repo.mix(ident, "owner"))["enabled"]
            await service.requests.retry("owner", ident)
            queued = await service.repo.run(scheduled)
            assert queued["state"] == "queued" and not queued["automatic"] and not queued["error"]
            assert queued["data"]["enable_after"] is False
            assert queued["data"]["publish_after_generation"] is not preview
            with pytest.raises(RequestError, match="Only a failed"):
                await service.requests.retry("owner", ident)
            await service.process(scheduled)
            await service.process(scheduled)
            result = await service.repo.run(scheduled)
            assert result["state"] == ("preview" if preview else "published")
            assert service.generate.await_count == 2
            assert service.publish.await_count == int(not preview)
            assert not (await service.repo.mix(ident, "owner"))["enabled"]
            assert await service.requests.create("owner", "scheduled-retry", spec) == (ident, False)
            assert len(await service.repo.runs(ident)) == 2

    anyio.run(run)


@pytest.mark.parametrize("denial", ["owner", "native", "library", "key"])
def test_failed_automatic_retry_still_requires_current_authorization(
    tmp_path: Path, denial: str
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            token = await agent_token(service, "owner", "Agent", history=False, library=False)
            creator = digest(token)
            ident, _ = await service.requests.create(
                "owner", "retry-guard", PlaylistRequest(prompt="quiet", count=1), creator=creator
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            await service.repo.store.execute(
                "UPDATE playlist_runs SET state='failed',automatic=1 WHERE id=?", [run_id]
            )
            before = await service.repo.run(run_id)
            if denial == "native":
                service.client.validate = AsyncMock(side_effect=PlaylistError("Native revoked."))
            elif denial == "library":
                service.client.native.library_id = 2
            elif denial == "key":
                await service.requests.revoke("owner", creator)
            with pytest.raises(PlaylistError):
                await service.requests.retry(
                    "other" if denial == "owner" else "owner", ident, creator=creator
                )
            assert await service.repo.run(run_id) == before
            if denial == "key":
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(Starlette(routes=API(lambda: service).routes())),
                    base_url="https://test.invalid",
                ) as http:
                    response = await http.post(
                        f"/api/playlists/requests/{ident}/retry",
                        headers={"authorization": "Bearer " + token},
                        json={},
                    )
                assert response.status_code == 401
                assert await service.repo.run(run_id) == before

    anyio.run(run)


def test_paused_background_automatic_selection_is_still_discarded(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            ident, _ = await service.requests.create(
                "owner", "background-pause", PlaylistRequest(prompt="quiet", count=1)
            )
            first = (await service.repo.runs(ident))[0]
            await service.repo.update(first["id"], "published", first["data"])
            scheduled = await service.repo.enqueue(ident, automatic=True)
            service.generate = AsyncMock(side_effect=AssertionError("paused background generation"))
            await service.process(scheduled)
            assert (await service.repo.run(scheduled))["state"] == "discarded"
            service.generate.assert_not_awaited()

    anyio.run(run)


def test_context_revoked_during_history_cannot_start_library_read(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            key = await agent_token(service, "owner", "Agent", history=True, library=True)

            async def history(*args: Any, **kwargs: Any) -> Any:
                await service.requests.revoke("owner", digest(key))
                return [], 0

            service.history.sync = history
            service.client.call = AsyncMock(return_value={})
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(Starlette(routes=API(lambda: service).routes())),
                base_url="https://test.invalid",
            ) as http:
                response = await http.post(
                    "/api/playlists/context",
                    headers={"authorization": "Bearer " + key},
                    json={"use_history": True, "use_library": True},
                )
            service.client.call.assert_not_awaited()
            assert response.status_code == 401

    anyio.run(run)


@pytest.mark.parametrize("wait_at", ["release-lock", "staged"])
def test_native_revocation_blocks_actual_engine_writes(tmp_path: Path, wait_at: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            song = CatalogTrack(
                CatalogRef("demo", "song"), "Song", "A", 1000, album=CatalogRef("demo", "album")
            )
            album = CatalogRelease(
                CatalogRef("demo", "album"), "Album", "A", ReleaseKind.ALBUM, "2020", tracks=(song,)
            )
            fetched = anyio.Event()

            async def get_album(ident: str) -> CatalogRelease:
                fetched.set()
                return album

            catalog = SimpleNamespace(
                key="demo", song=AsyncMock(return_value=song), album=get_album
            )
            native = SimpleNamespace(songs_under=AsyncMock(return_value=[]))
            engine = PlaceholderEngine(
                layout=Layout(tmp_path / "music", "placeholders"),
                navidrome=native,
                scans=SimpleNamespace(scan=AsyncMock()),
                silence=SilenceMaker(),
                store=service.repo.store,
            )
            commits = Commits(
                catalog,
                engine,
                native,
                SimpleNamespace(
                    song=AsyncMock(return_value=None), album=AsyncMock(return_value=None)
                ),
            )
            service.catalog, service.commits = catalog, commits
            revoked = False
            reached, proceed = anyio.Event(), anyio.Event()
            moved = False

            async def validate(c: Connection) -> None:
                if revoked:
                    raise PlaylistError("Native authorization was revoked.")

            async def hook(stage: str) -> None:
                nonlocal moved
                if stage == "in place":
                    moved = True
                if wait_at == "staged" and stage == "staged":
                    reached.set()
                    await proceed.wait()

            async def verify(*args: Any) -> None:
                raise MaterializeError("synthetic scan refusal")

            engine._at = hook
            engine._verify_new = verify
            service.client.validate = validate
            service.client.call = AsyncMock(
                side_effect=PlaylistError("Native authorization was revoked.")
            )
            selected = Track(
                id="sh.tr.demo.song",
                artist="A",
                title="Song",
                album="Album",
                duration=1,
                release="demo:album",
            )

            async def publish() -> None:
                with pytest.raises((PlaylistError, ExceptionGroup)):
                    await service.check_tracks(
                        connected(),
                        [selected],
                        {"demo:album": release_fingerprint(album)},
                        library_bound=True,
                    )

            with anyio.fail_after(10):
                async with anyio.create_task_group() as tg:
                    if wait_at == "release-lock":
                        async with engine.lock_for("demo:album"):
                            tg.start_soon(publish)
                            # Catalog call proves the initial native authorization passed.
                            await fetched.wait()
                            revoked = True
                    else:
                        tg.start_soon(publish)
                        await reached.wait()
                        revoked = True
                        proceed.set()
            assert not moved, "materialization moved files after native authorization was revoked"
            assert not list(engine.layout.staging.glob("*.flac"))
            assert not await service.repo.store.fetchone("SELECT * FROM pending_placeholders")

    anyio.run(run)


@pytest.mark.parametrize("revoke", ["key", "native"])
def test_history_import_reauthorizes_each_page(tmp_path: Path, revoke: str) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            await service.repo.connect(connected(history_user="synthetic", history_key="synthetic"))
            key = await agent_token(service, "owner", "Agent", history=True, library=True)
            calls = []
            revoked = False

            async def validate(c: Connection) -> None:
                if revoked and revoke == "native":
                    raise PlaylistError("Native authorization revoked.")

            async def response(request: httpx.Request) -> httpx.Response:
                nonlocal revoked
                calls.append(request.url.params["page"])
                if revoke == "key":
                    await service.requests.revoke("owner", digest(key))
                revoked = True
                return httpx.Response(
                    200,
                    json={
                        "recenttracks": {
                            "@attr": {"page": "1", "totalPages": "2", "total": "2", "perPage": "1"},
                            "track": [
                                {
                                    "artist": {"#text": "A"},
                                    "name": "Song",
                                    "date": {"uts": request.url.params["to"]},
                                }
                            ],
                        }
                    },
                )

            service.client.validate = validate
            service.client.call = AsyncMock(side_effect=AssertionError("no library enumeration"))
            await service.history.http.aclose()
            service.history.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(Starlette(routes=API(lambda: service).routes())),
                base_url="https://test.invalid",
            ) as http:
                answer = await http.post(
                    "/api/playlists/context",
                    headers={"authorization": "Bearer " + key},
                    json={"use_history": True, "use_library": True},
                )
            assert answer.status_code == (401 if revoke == "key" else 403)
            assert calls == ["1"]
            service.client.call.assert_not_awaited()
            owner = await service.repo.store.fetchone(
                "SELECT * FROM playlist_owners WHERE id='owner'"
            )
            assert owner["history_until"] == 0

    anyio.run(run)


def test_context_import_leaves_revocation_lock_available(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            key = await agent_token(service, "owner", "Agent", history=True, library=True)
            started, release = anyio.Event(), anyio.Event()
            results = []

            async def history(*args: Any, **kwargs: Any) -> Any:
                started.set()
                await release.wait()
                return [], 0

            service.history.sync = history
            service.client.call = AsyncMock(side_effect=AssertionError("revoked"))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(Starlette(routes=API(lambda: service).routes())),
                base_url="https://test.invalid",
            ) as http:

                async def read_context() -> None:
                    results.append(
                        await http.post(
                            "/api/playlists/context",
                            headers={"authorization": "Bearer " + key},
                            json={"use_history": True, "use_library": True},
                        )
                    )

                with anyio.fail_after(2):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(read_context)
                        await started.wait()
                        async with service.changes:
                            await service.requests.revoke("owner", digest(key))
                        release.set()
            assert results[0].status_code == 401
            service.client.call.assert_not_awaited()

    anyio.run(run)


@pytest.mark.parametrize("pause", [False, True])
def test_new_standalone_intent_and_pause_survive_initial_authorization(
    tmp_path: Path, pause: bool
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            ident, _ = await service.requests.create(
                "owner",
                "initial-intent",
                PlaylistRequest(prompt="quiet", count=1, save_instructions=True, cadence="daily"),
            )
            run_id = (await service.repo.runs(ident))[0]["id"]
            initial = (await service.repo.run(run_id))["data"]
            assert initial == {"publish_after_generation": True, "enable_after": True}
            paused = False

            async def validate(c: Connection) -> None:
                nonlocal paused
                if pause and not paused:
                    await service.repo.pause(ident)
                    paused = True

            async def publish(c: Connection, mix: Any, run: str, data: Any) -> None:
                assert data["publish_after_generation"]
                assert data["enable_after"] is not pause
                await service.repo.update(run, "published", data)

            service.client.validate = validate
            service.generate = AsyncMock(return_value={"selected": []})
            service.publish = publish
            await service.process(run_id)
            assert (await service.repo.run(run_id))["state"] == "published"

    anyio.run(run)


def test_preview_render_uses_the_run_whose_selection_was_shown(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            ident, _ = await service.requests.create(
                "owner", "render-race", PlaylistRequest(prompt="quiet", count=1, preview=True)
            )
            first = (await service.repo.runs(ident))[0]
            await service.repo.update(first["id"], "preview", {"selected": []})
            status = service.requests.status

            async def raced_status(*args: Any, **kwargs: Any) -> Any:
                shown = await status(*args, **kwargs)
                await service.repo.update(first["id"], "discarded", first["data"])
                second = await service.repo.enqueue(ident, review=True)
                await service.repo.update(second, "preview", {"selected": []})
                return shown

            service.requests.status = raced_status
            dashboard = SimpleNamespace(
                services=SimpleNamespace(playlists=service),
                _render=AsyncMock(return_value=Response()),
            )
            await Pages(dashboard).render(Session("hash", "owner", "csrf", 0, "owner"), ident=ident)
            values = dashboard._render.call_args.kwargs
            assert values["run"]["id"] == values["shown"]["run"] == first["id"]

    anyio.run(run)


def test_context_has_bounded_deadline_and_returns_no_partial_metadata(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            key = await agent_token(service, "owner", "Agent", history=True, library=True)
            service.settings.run_seconds = 0.02

            async def history(*args: Any, **kwargs: Any) -> Any:
                await anyio.Event().wait()

            service.history.sync = history
            service.client.call = AsyncMock(side_effect=AssertionError("no later library read"))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(Starlette(routes=API(lambda: service).routes())),
                base_url="https://test.invalid",
            ) as http:
                with anyio.fail_after(1):
                    answer = await http.post(
                        "/api/playlists/context",
                        headers={"authorization": "Bearer " + key},
                        json={"use_history": True, "use_library": True},
                    )
                assert answer.status_code == 503
                assert set(answer.json()) == {"error"}
                assert answer.headers["cache-control"] == "no-store"
            service.client.call.assert_not_awaited()

    anyio.run(run)
