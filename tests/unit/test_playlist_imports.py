"""Faithful imports use invented input and durable owner-scoped publication state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from shijhon.dashboard.playlist_imports import Pages, read_form
from shijhon.playlists.ai import validate
from shijhon.playlists.imports import MAX_BYTES, parse
from shijhon.playlists.model import Definition, PlaylistError, Suggestion, Track
from shijhon.playlists.resolve import Resolver
from shijhon.store.db import _bundled_migrations, apply_migrations
from tests.unit.test_playlist_requests import connected, service_at


def test_parsing_keeps_order_repeats_qualifiers_and_import_specific_limits() -> None:
    assert [(s.artist, s.title) for s in parse("A — One\n\nB\tTwo\nA - One")] == [
        ("A", "One"),
        ("B", "Two"),
        ("A", "One"),
    ]
    rows = parse('\ufeffartist,title,album,version\n"A, B",One,Room,Live\nA,Two,,\n', csv_file=True)
    assert rows[0].wanted()["required_album"] == "Room"
    assert rows[0].wanted()["required_title"] == "One (Live)"
    repeated = parse("A — One\n" * 1000)
    spec = Definition(mode="import", count=1000, cadence="manual", review_before_publishing=True)
    song = Track(id="native", artist="A", title="One")
    validate([song] * len(repeated), spec)
    with pytest.raises(PlaylistError):
        validate([song], spec)
    with pytest.raises(ValidationError):
        Definition(count=101)
    with pytest.raises(ValidationError):
        Definition(mode="import", count=101, cadence="daily", review_before_publishing=True)


@pytest.mark.parametrize(
    "source,csv_file",
    [
        ("", False),
        ("A — One\nbroken row\nA — One", False),
        ("x" * (MAX_BYTES + 1), False),
        ("artist,title\nA,One\nB,\n", True),
        ("artist,title\nA,One,Extra\n", True),
        ("artist,title,title\nA,One,Two\n", True),
        ('artist,title\nA,"unclosed', True),
        ("artist,title\nA,One\x00\n", True),
    ],
)
def test_invalid_input_is_rejected_without_silently_dropping_rows(
    source: str, csv_file: bool
) -> None:
    with pytest.raises(PlaylistError):
        parse(source, csv_file=csv_file)


def test_bounded_multipart_and_urlencoded_form_reader() -> None:
    async def endpoint(request: Request):
        from shijhon.dashboard.app import Refused

        try:
            return JSONResponse(await read_form(request))
        except Refused as refused:
            return refused.response

    async def run() -> None:
        app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            answer = await c.post("/", data={"csrf": "csrf", "lines": "A — One"})
            assert answer.json()["lines"] == "A — One"
            answer = await c.post(
                "/",
                data={"csrf": "csrf"},
                files={"csv": ("music.csv", b"artist,title\nA,One\n", "text/csv")},
            )
            assert answer.json()["csv"].startswith("artist,title")
            assert answer.json()["csrf"] == "csrf"
            text = "#EXTM3U\nAlbum/Été.flac\n"
            answer = await c.post("/", files={"file": ("local.m3u", text.encode("utf-16"))})
            assert answer.json()["file"] == text and answer.json()["filename"] == "local.m3u"
            answer = await c.post(
                "/",
                data={"encoding": "cp1252"},
                files={"file": ("local.m3u", text.encode("cp1252"))},
            )
            assert answer.json()["file"] == text
            assert (
                await c.post("/", files={"csv": ("x.csv", b"x" * (MAX_BYTES + 1))})
            ).status_code == 413
            assert (await c.post("/", files={"csv": ("x.csv", b"\xff")})).status_code == 400
            assert (
                await c.post("/", content="{}", headers={"content-type": "application/json"})
            ).status_code == 415
            assert (
                await c.post(
                    "/",
                    content="csrf=a&csrf=b",
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
            ).status_code == 400

    anyio.run(run)


def test_ambiguous_owned_recordings_are_reported_without_catalog_substitution() -> None:
    async def run() -> None:
        client = AsyncMock()
        client.native.library_id = 1
        client.call.return_value = {
            "searchResult3": {
                "song": [
                    {"id": "one", "artist": "A", "title": "One", "album": "First"},
                    {"id": "two", "artist": "A", "title": "One", "album": "Second"},
                ]
            }
        }
        catalog = AsyncMock()
        resolver = Resolver(client, catalog)
        ambiguous: list[Track] = []
        assert (
            await resolver.resolve(
                connected(), Suggestion(artist="A", title="One").wanted(), ambiguous=ambiguous
            )
            is None
        )
        assert len(ambiguous) == 2
        catalog.search.assert_not_called()
        found = await resolver.resolve(
            connected(), Suggestion(artist="A", title="One", album="First").wanted(), ambiguous=[]
        )
        assert found and found.id == "one"
        assert (
            await resolver.resolve(
                connected(),
                Suggestion(artist="A", title="One", version="Live").wanted(),
                ambiguous=[],
            )
            is None
        )

    anyio.run(run)


def test_review_requires_explicit_current_exclusions_and_freezes_order_on_retry(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:

            async def resolve(
                connection: Any, wanted: dict[str, Any], **kwargs: Any
            ) -> Track | None:
                if wanted["title"] == "Missing":
                    return None
                return Track(id="native", artist=wanted["artist"], title=wanted["title"])

            service.resolver.resolve = AsyncMock(side_effect=resolve)
            ident = await service.imports.create(
                "owner", "import-key", "Ordered", "A — One\nB — Missing\nA — One"
            )
            assert ident == await service.imports.create(
                "owner", "import-key", "Ordered", "A — One\nB — Missing\nA — One"
            )
            with pytest.raises(PlaylistError):
                await service.imports.create("owner", "import-key", "Changed", "A — One")
            job = (await service.repo.runs(ident))[0]
            await service.process(job["id"])
            job = await service.repo.run(job["id"])
            assert job["state"] == "preview"
            assert [r["state"] for r in job["data"]["rows"]] == ["matched", "unresolved", "matched"]
            assert [s["id"] for s in job["data"]["selected"]] == ["native", "native"]
            assert service.resolver.resolve.call_count == 2
            key = service.imports.review_key(job["data"])
            with pytest.raises(PlaylistError, match="confirm"):
                await service.imports.action("owner", ident, job["id"], "publish", "", key)
            with pytest.raises(PlaylistError, match="review changed"):
                await service.imports.action("owner", ident, job["id"], "publish", "1", "stale")
            with pytest.raises(PlaylistError):
                await service.imports.action("other", ident, job["id"], "publish", "1", key)
            await service.imports.action("owner", ident, job["id"], "retry", "")
            await service.process(job["id"])
            assert service.resolver.resolve.call_count == 3
            job = await service.repo.run(job["id"])
            await service.imports.action(
                "owner", ident, job["id"], "publish", "1", service.imports.review_key(job["data"])
            )
            job = await service.repo.run(job["id"])
            assert job["state"] == "publishing"
            assert job["data"]["definition"]["count"] == 2
            assert job["data"]["import_review"] == {"total": 3, "excluded": [2]}
            original = await service.imports.evidence("owner", ident)
            assert len(json.loads(original["input"])["rows"]) == 3
            await service.repo.update(job["id"], "uncertain", job["data"])
            await service.imports.action("owner", ident, job["id"], "retry", "")
            assert (await service.repo.run(job["id"]))["data"] == job["data"]
            # Even a direct call into the publisher requires recorded import review.
            del job["data"]["import_review"]
            mix = await service.repo.mix(ident, "owner")
            with pytest.raises(PlaylistError, match="Review the complete import"):
                await service.publish(connected(), mix, job["id"], job["data"])

    anyio.run(run)


def test_completed_rows_survive_interruption_and_library_move_blocks_retry(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            service.resolver.resolve = AsyncMock(
                side_effect=[
                    Track(id="native", artist="A", title="One"),
                    RuntimeError("private remote body"),
                ]
            )
            ident = await service.imports.create("owner", "stop-key", "Stopped", "A — One\nB — Two")
            job = (await service.repo.runs(ident))[0]
            await service.process(job["id"])
            job = await service.repo.run(job["id"])
            assert job["state"] == "failed" and "private" not in job["error"]
            assert job["data"]["rows"][0]["track"]["id"] == "native"
            service.resolver.resolve = AsyncMock(
                return_value=Track(id="native-2", artist="B", title="Two")
            )
            await service.imports.action("owner", ident, job["id"], "retry", "")
            await service.process(job["id"])
            job = await service.repo.run(job["id"])
            assert job["state"] == "preview"
            assert [s["id"] for s in job["data"]["selected"]] == ["native", "native-2"]
            assert service.resolver.resolve.call_count == 1
            service.client.native.library_id = 2
            with pytest.raises(PlaylistError):
                await service.imports.action(
                    "owner",
                    ident,
                    job["id"],
                    "publish",
                    "0",
                    service.imports.review_key(job["data"]),
                )

    anyio.run(run)


def test_import_migration_preserves_existing_owner_and_cascades_private_evidence() -> None:
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    migrations = _bundled_migrations()
    apply_migrations(db, migrations[:8])
    db.execute(
        "INSERT INTO playlist_owners(id,username,connection,generation)"
        " VALUES('owner','owner','{}','generation')"
    )
    db.execute("INSERT INTO playlist_mixes(id,owner,definition) VALUES('mix','owner','{}')")
    assert apply_migrations(db) == len(migrations)
    assert db.execute("SELECT id FROM playlist_mixes").fetchone()[0] == "mix"
    db.execute("INSERT INTO playlist_imports VALUES('mix','owner','1','key','hash','{}')")
    db.execute("DELETE FROM playlist_owners WHERE id='owner'")
    assert db.execute("SELECT count(*) FROM playlist_imports").fetchone()[0] == 0


def test_failed_native_authorization_does_not_render_private_import_evidence(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db", ai=False) as service:
            ident = await service.imports.create(
                "owner", "private-key", "Private import", "A — One"
            )
            service.client.validate = AsyncMock(
                side_effect=PlaylistError("Reconnect your account.")
            )
            render = AsyncMock(return_value=HTMLResponse("safe"))
            dashboard = SimpleNamespace(services=SimpleNamespace(playlists=service), _render=render)
            session = SimpleNamespace(user_id="owner")
            await Pages(dashboard).render(session, ident=ident)
            context = render.call_args.kwargs
            assert context["shown"] is None and context["listing"] == []
            assert context["error"] == "Reconnect your account."

    anyio.run(run)


def test_import_without_a_run_shows_the_normal_error_notice(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            ident = await service.imports.create("owner", "no-run-key", "Quiet", "A — One")
            await service.repo.store.execute("DELETE FROM playlist_runs WHERE mix=?", [ident])
            dashboard = SimpleNamespace(
                services=SimpleNamespace(playlists=service),
                _render=AsyncMock(return_value=HTMLResponse("Imports")),
            )
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path_params": {"ident": ident},
                    "query_string": b"",
                }
            )
            response = await Pages(dashboard).detail(request, SimpleNamespace(user_id="owner"))
            assert response.status_code == 200
            values = dashboard._render.call_args.kwargs
            assert values["shown"] is None
            assert "no current run" in values["error"]

    anyio.run(run)
