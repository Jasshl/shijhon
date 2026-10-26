"""Synthetic history and bounded curator/resolver failures: no accounts or inference."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
import time
from contextlib import nullcontext, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from pydantic import ValidationError

from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.config import PlaylistSettings
from shijhon.playlists.ai import Curator, plain, run_process
from shijhon.playlists.client import OwnerClient
from shijhon.playlists.history import History
from shijhon.playlists.model import Connection, Definition, PlaylistError, Track, fingerprint
from shijhon.playlists.repository import Repository
from shijhon.playlists.resolve import choose
from shijhon.playlists.service import Playlists
from shijhon.store import Store
from shijhon.views.commits import CommitError


@pytest.mark.parametrize(
    ("message", "expected", "category"),
    [
        (
            "could not add the catalog album: the placeholders joined another album",
            "conflicts with an existing library album",
            "album_collision",
        ),
        (
            "could not add the album's missing songs",
            "missing tracks could not be added",
            "owned_fill",
        ),
        (
            "catalog unavailable: private-adapter-secret",
            "could not be prepared",
            "library_preparation",
        ),
        ("private-adapter-secret", "settled Library decision", "owned_decision"),
        ("private-adapter-secret", "with confidence", "owned_ambiguous"),
        ("private-adapter-secret", "cannot safely complete", "owned_incompatible"),
        ("private-adapter-secret", "saved match", "owned_plan"),
        ("private-adapter-secret", "will not be expanded", "owned_complete"),
        ("private-adapter-secret", "waiting before retry", "owned_retry"),
        ("private-adapter-secret", "Generate again", "selection_changed"),
    ],
)
def test_publication_reports_preparation_failures_without_adapter_details(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, message: str, expected: str, category: str
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        t = CatalogTrack(
            ref=CatalogRef("demo", "track"),
            title="Night Light",
            artist="Quill & Marrow",
            duration_ms=180000,
            album=CatalogRef("demo", "album"),
        )

        async def song(ident: str) -> CatalogTrack:
            return t

        async def natives(ids: set[str], **kwargs: Any) -> dict[str, str]:
            raise CommitError(0, message, category=category, item="sh.tr.demo.track")

        service = Playlists(
            store,
            SimpleNamespace(base_url="", library_id=1),
            SimpleNamespace(key="demo", song=song),
            SimpleNamespace(
                engine=SimpleNamespace(guard_materializations=lambda check: nullcontext()),
                natives=natives,
            ),
            PlaylistSettings(),
        )
        try:
            with pytest.raises(PlaylistError, match=expected) as caught:
                await service.check_tracks(
                    connection(), [track("sh.tr.demo.track", release="demo:album")], {}
                )
            assert "private-adapter-secret" not in str(caught.value)
            assert "private-adapter-secret" not in caplog.text
            assert category in caplog.text
            assert "Night Light" in str(caught.value) and "Quill & Marrow" in str(caught.value)
            if category.startswith("owned_") and category != "owned_fill":
                assert "No match" not in str(caught.value)
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("case", ["confirmed", "no_isrc", "explicit", "duplicate", "changed"])
def test_matched_edition_requires_one_unchanged_corroborated_recording(
    tmp_path: Path, case: str
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        source = track("sh.tr.demo.source", release="demo:original", isrc="invented-isrc")
        t = CatalogTrack(
            CatalogRef("demo", "plannedtrack"),
            source.title,
            source.artist,
            source.duration * 1000,
            isrc=source.isrc,
            album=CatalogRef("demo", "planned"),
            explicit=case == "explicit",
        )
        tracks = (
            (t, dataclasses.replace(t, ref=CatalogRef("demo", "duplicate")))
            if case == "duplicate"
            else (t,)
        )
        planned = CatalogRelease(
            CatalogRef("demo", "planned"),
            "Invented album",
            source.artist,
            ReleaseKind.ALBUM,
            "2020",
            tracks=tracks,
        )
        original = dataclasses.replace(planned, ref=CatalogRef("demo", "original"))
        fresh = (
            dataclasses.replace(planned, label="Changed label") if case == "changed" else planned
        )
        if case == "no_isrc":
            source = source.model_copy(update={"isrc": ""})

        async def album(ident: str) -> CatalogRelease:
            return original if ident == "original" else fresh

        async def matched(release: CatalogRelease) -> CatalogRelease:
            return planned

        service = Playlists(
            store,
            SimpleNamespace(base_url="", library_id=1),
            SimpleNamespace(key="demo", album=album),
            SimpleNamespace(owned=SimpleNamespace(matched_release=matched)),
            PlaylistSettings(),
        )
        try:
            result = await service.matched_edition(source)
            if case == "confirmed":
                assert result and result.id == "sh.tr.demo.plannedtrack"
                assert result.release == "demo:planned" and result.isrc == source.isrc
                assert result.score == source.score and result.reason == source.reason
            else:
                assert result is None
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize(
    ("code", "body"),
    [(200, {"apiKey": ""}), (200, []), (404, {}), (302, {}), (200, {"apiKey": 123})],
)
def test_native_history_key_refuses_unavailable_responses(code: int, body: Any) -> None:
    async def check() -> None:
        client = OwnerClient(SimpleNamespace(base_url="http://native.invalid"))
        await client.http.aclose()
        seen = []

        def response(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert request.method == "GET"
            assert str(request.url) == "http://native.invalid/api/lastfm/link"
            assert request.headers["x-nd-authorization"] == "Bearer invented-owner-jwt"
            return httpx.Response(
                code, json=body, headers={"location": "https://unrelated.invalid/secret"}
            )

        client.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
        try:
            with pytest.raises(PlaylistError, match="key is unavailable"):
                await client.history_key("invented-owner-jwt")
            assert len(seen) == 1
            with pytest.raises(PlaylistError):
                await client.history_key("")
            assert len(seen) == 1
        finally:
            await client.aclose()

    anyio.run(check)


def connection(**changes: Any) -> Connection:
    return Connection.model_validate(
        {
            "user_id": "invented-owner-id",
            "username": "invented",
            "token": "invented-token",
            "salt": "invented-salt",
            "history_user": "invented-history",
            "history_key": "invented-history-key",
            **changes,
        }
    )


def track(ident: str = "one", **changes: Any) -> Track:
    return Track.model_validate(
        {
            "id": ident,
            "artist": "Quill & Marrow",
            "title": "Night Light",
            "duration": 180,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "wanted,found",
    [
        ({"title": "Night Light"}, {"title": "Night Light (Live)"}),
        ({"title": "Night Light - Radio Edit"}, {"title": "Night Light"}),
        ({"title": "Night Light (Clean)"}, {"explicit": True}),
        ({"duration": 180}, {"duration": 240}),
        ({"isrc": "XXAAA0000001"}, {"isrc": "XXAAA0000002"}),
    ],
)
def test_resolver_keeps_recording_versions(wanted: dict[str, Any], found: dict[str, Any]) -> None:
    target = {"artist": "Quill & Marrow", "title": "Night Light", "duration": 180, **wanted}
    assert choose(target, [track(**found)]) is None


def test_resolver_ambiguity_and_corroborated_releases() -> None:
    target = {"artist": "Quill & Marrow", "title": "Night Light"}
    assert choose(target, [track("one"), track("two")]) is None
    options = [track("one", isrc="XXAAA0000001"), track("two", isrc="XXAAA0000001")]
    assert choose(target, options) is not None
    assert choose(target, [options[0], options[1].model_copy(update={"clean": True})]) is None
    assert choose({**target, "clean": True}, [track(explicit=True)]) is None
    assert choose({**target, "explicit": True}, [track(clean=True)]) is None


def test_plain_selection_respects_familiarity_and_artist_limits() -> None:
    pool = [
        track(
            str(i),
            artist=f"Invented artist {i // 2}",
            title=f"Song {i}",
            unfamiliar=i >= 3,
            score=10 - i,
        )
        for i in range(8)
    ]
    result = plain(pool, Definition(count=4, unfamiliar_percent=75, max_per_artist=1))
    assert len(result) == 4
    assert sum(s.unfamiliar for s in result) >= 3
    assert len({s.artist for s in result}) == 4
    with pytest.raises(PlaylistError):
        plain(pool, Definition(count=8, max_per_artist=1))


def test_blank_or_unbounded_definitions_refused() -> None:
    for changes in ({"name": " "}, {"count": 0}, {"count": 101}, {"brief": "evening"}):
        with pytest.raises(ValidationError):
            Definition.model_validate(changes)
    with pytest.raises(ValidationError):
        connection(backend="codex", model="--dangerous-flag")


def test_ai_candidate_contract_refuses_invented_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    curator = Curator(PlaylistSettings())

    async def fake_api(c: Connection, prompt: str) -> Any:
        assert "invented-token" not in prompt
        assert "invented-history-key" not in prompt
        return {"selections": [{"id": "invented", "reason": "Unknown ID"}]}

    monkeypatch.setattr(curator, "api", fake_api)

    async def run() -> None:
        with pytest.raises(PlaylistError, match="invalid selection"):
            await curator.select(
                connection(backend="api"), Definition(count=1, ai=True), [track(unfamiliar=True)]
            )

    anyio.run(run)


@pytest.mark.parametrize("legacy", [False, True])
def test_history_import_is_atomic_and_ignores_now_playing(tmp_path: Path, legacy: bool) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "state.sqlite3")
        history = History(store, PlaylistSettings(history_pages=2))
        owner = connection()
        repo = Repository(store)
        await repo.connect(owner)
        if legacy:
            saved = json.loads(owner.stored())
            saved.pop("history_source")
            await store.execute(
                "UPDATE playlist_owners SET connection=? WHERE id=?",
                [json.dumps(saved, sort_keys=True), owner.user_id],
            )
            assert await repo.connection(owner.user_id) == owner
        at = int(time.time()) - 100
        broken = True

        async def ask(c: Connection, method: str, **params: Any) -> dict[str, Any]:
            assert params["to"] < time.time()
            page = params["page"]
            if page == 2 and broken:
                raise PlaylistError("synthetic page failure")
            songs = [
                {
                    "artist": {"#text": "Quill & Marrow"},
                    "name": "Night Light",
                    "album": {"#text": "Lanterns"},
                    "date": {"uts": str(at - page)},
                }
            ]
            if page == 1:
                songs.append({"name": "Now", "@attr": {"nowplaying": "true"}})
            return {
                "recenttracks": {
                    "@attr": {"totalPages": "2", "total": "2", "perPage": "1", "page": str(page)},
                    "track": songs,
                }
            }

        history.ask = ask  # type: ignore[method-assign]
        try:
            with pytest.raises(PlaylistError):
                await history.sync(owner)
            assert not await store.fetchall("SELECT * FROM playlist_listens")
            row = await store.fetchone("SELECT history_until FROM playlist_owners")
            assert row and row["history_until"] == 0
            broken = False
            listens, first = await history.sync(owner)
            assert len(listens) == 2
            assert first < at
            again, _ = await history.sync(owner)
            assert len(again) == 2
        finally:
            await history.aclose()
            await store.close()

    anyio.run(run)


def test_history_page_limit_does_not_advance_cursor(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        await Repository(store).connect(connection())
        history = History(store, PlaylistSettings(history_pages=1))
        history.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "recenttracks": {
                            "@attr": {"totalPages": "2", "total": "2", "perPage": "1", "page": "1"},
                            "track": [
                                {
                                    "artist": {"#text": "Invented artist"},
                                    "name": "Invented song",
                                    "date": {"uts": str(int(time.time()) - 100)},
                                    "album": {"#text": "Album"},
                                }
                            ],
                        }
                    },
                )
            )
        )
        try:
            with pytest.raises(PlaylistError, match="page limit"):
                await history.sync(connection())
            row = await store.fetchone("SELECT history_until FROM playlist_owners")
            assert row and row["history_until"] == 0
        finally:
            await history.aclose()
            await store.close()

    anyio.run(run)


def test_repository_owner_scope_and_single_active_run(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        repo = Repository(store)
        owner = connection()
        try:
            await repo.connect(owner)
            ident = await repo.create(owner.user_id, Definition())
            with pytest.raises(PlaylistError):
                await repo.mix(ident, "other-owner")
            run_id = await repo.enqueue(ident)
            with pytest.raises(PlaylistError):
                await repo.enqueue(ident)
            await repo.update(run_id, "preview", {"selected": []})
            with pytest.raises(PlaylistError):
                await repo.enqueue(ident)
            await repo.update(run_id, "discarded", {})
            assert await repo.enqueue(ident) != run_id
            assert "invented-token" not in repr(owner)
            assert "invented-token" in owner.stored()
            replacement = owner.model_copy(update={"user_id": "new-id"})
            await repo.connect(replacement)
            assert await repo.connection(owner.user_id) == owner
            assert await repo.connection(replacement.user_id) == replacement
        finally:
            await store.close()

    anyio.run(run)


def test_fingerprint_detects_tracks_order_and_user_edits() -> None:
    value = {"id": "pl", "owner": "invented", "name": "Mix", "entry": [{"id": "a"}, {"id": "b"}]}
    before = fingerprint(value)
    assert fingerprint({**value, "entry": list(reversed(value["entry"]))}) != before
    assert fingerprint({**value, "comment": "User edit"}) != before
    assert fingerprint({**value, "public": True}) != before


def test_cli_receives_no_service_credentials_and_uses_bounded_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shijhon.playlists import ai

    captured: list[list[str]] = []

    async def fake_process(args: list[str], prompt: str, root: Path, env: dict[str, str]) -> bytes:
        captured.append(args)
        assert "SHIJHON_NAVIDROME__PASSWORD" not in env
        assert "invented-token" not in prompt
        answer = {"selections": [{"id": "one", "reason": "Matches your taste"}]}
        if args[0] == "codex":
            output = Path(args[args.index("--output-last-message") + 1])
            await anyio.to_thread.run_sync(output.write_text, json.dumps(answer))
            return b""
        return json.dumps({"structured_output": answer}).encode()

    monkeypatch.setattr(ai, "run_process", fake_process)
    monkeypatch.setenv("SHIJHON_NAVIDROME__PASSWORD", "synthetic-service-secret")

    async def run() -> None:
        curator = Curator(
            PlaylistSettings(allow_cli=True, codex_command="codex", claude_command="claude")
        )
        for backend in ("codex", "claude"):
            selected = await curator.select(
                connection(backend=backend), Definition(count=1, ai=True), [track(unfamiliar=True)]
            )
            assert selected[0].id == "one"

    anyio.run(run)
    assert "--sandbox" in captured[0]
    assert "--ignore-user-config" in captured[0]
    assert "--tools" in captured[1] and captured[1][captured[1].index("--tools") + 1] == ""
    assert "--safe-mode" in captured[1]


@pytest.mark.parametrize(
    "recent",
    [
        {"@attr": {"totalPages": "1", "total": "200"}},
        {"@attr": {"totalPages": "1", "total": "1"}, "track": []},
        {"@attr": {"totalPages": "1"}, "track": ["malformed entry"]},
    ],
)
def test_incomplete_history_preserves_cursor(tmp_path: Path, recent: dict[str, Any]) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        await Repository(store).connect(connection())
        history = History(store, PlaylistSettings())
        await history.http.aclose()
        history.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"recenttracks": recent})
            )
        )
        try:
            with pytest.raises(PlaylistError):
                await history.sync(connection())
            row = await store.fetchone("SELECT history_until FROM playlist_owners")
            assert row and row["history_until"] == 0
        finally:
            await history.aclose()
            await store.close()

    anyio.run(run)


def test_history_reset_cannot_be_overwritten_by_inflight_import(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        repo = Repository(store)
        original = connection()
        await repo.connect(original)
        history = History(store, PlaylistSettings())

        async def ask(c: Connection, method: str, **params: Any) -> dict[str, Any]:
            await repo.connect(original.model_copy(update={"history_user": "other-history"}))
            return {
                "recenttracks": {
                    "@attr": {"totalPages": "1", "total": "1", "perPage": "200", "page": "1"},
                    "track": [
                        {
                            "artist": {"#text": "Old profile artist"},
                            "name": "Old profile song",
                            "album": {"#text": "Old album"},
                            "date": {"uts": str(int(time.time()) - 100)},
                        }
                    ],
                }
            }

        history.ask = ask  # type: ignore[method-assign]
        try:
            with pytest.raises(PlaylistError, match="changed during import"):
                await history.sync(original)
            assert not await store.fetchall("SELECT * FROM playlist_listens")
            row = await store.fetchone("SELECT history_until FROM playlist_owners")
            assert row and row["history_until"] == 0
        finally:
            await history.aclose()
            await store.close()

    anyio.run(run)


def test_history_validates_total_across_all_pages(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        owner = connection()
        await Repository(store).connect(owner)
        history = History(store, PlaylistSettings())

        async def ask(c: Connection, method: str, **params: Any) -> dict[str, Any]:
            return {
                "recenttracks": {
                    "@attr": {
                        "totalPages": "2",
                        "total": "3",
                        "perPage": "2",
                        "page": str(params["page"]),
                    },
                    "track": [
                        {
                            "artist": {"#text": "Invented artist"},
                            "name": "Invented song",
                            "album": {"#text": "Album"},
                            "date": {"uts": str(int(time.time()) - 100 - params["page"])},
                        }
                    ],
                }
            }

        history.ask = ask  # type: ignore[method-assign]
        try:
            with pytest.raises(PlaylistError, match="incomplete"):
                await history.sync(owner)
            row = await store.fetchone("SELECT history_until FROM playlist_owners")
            assert row and row["history_until"] == 0
            assert not await store.fetchall("SELECT * FROM playlist_listens")
        finally:
            await history.aclose()
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("returned_page", [1, 2])
def test_repeated_history_pages_do_not_advance_cursor(tmp_path: Path, returned_page: int) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        owner = connection()
        await Repository(store).connect(owner)
        history = History(store, PlaylistSettings())
        at = int(time.time()) - 100

        async def ask(c: Connection, method: str, **params: Any) -> dict[str, Any]:
            return {
                "recenttracks": {
                    "@attr": {
                        "totalPages": "2",
                        "total": "2",
                        "perPage": "1",
                        "page": str(1 if params["page"] == 1 else returned_page),
                    },
                    "track": [
                        {
                            "artist": {"#text": "Invented artist"},
                            "name": "Same listen",
                            "album": {"#text": "Album"},
                            "date": {"uts": str(at)},
                        }
                    ],
                }
            }

        history.ask = ask  # type: ignore[method-assign]
        try:
            with pytest.raises(PlaylistError, match="incomplete"):
                await history.sync(owner)
            row = await store.fetchone("SELECT history_until FROM playlist_owners")
            assert row and row["history_until"] == 0
            assert not await store.fetchall("SELECT * FROM playlist_listens")
        finally:
            await history.aclose()
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("ignore_term", [False, True])
def test_runner_cleans_descendants_after_leader_exit(tmp_path: Path, ignore_term: bool) -> None:
    child = (
        "import signal,time; "
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
        + "print('ready',flush=True); time.sleep(30)"
    )
    script = (
        f"import subprocess,sys; child=subprocess.Popen([sys.executable,'-c',{child!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL); "
        "child.stdout.readline(); print(child.pid,flush=True)"
    )

    async def run() -> None:
        result = await run_process([sys.executable, "-c", script], "", tmp_path, {})
        pid = int(result.strip())
        try:
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                await anyio.sleep(0.01)
            pytest.fail("Runner descendant remained alive after the leader exited")
        finally:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    anyio.run(run)


@pytest.mark.parametrize("automatic, review", [(True, False), (False, False), (False, True)])
def test_interrupted_selection_pauses_publication_but_keeps_opt_in_review(
    tmp_path: Path, automatic: bool, review: bool
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        await service.repo.connect(connection())
        mix = await service.repo.create(
            connection().user_id, Definition(review_before_publishing=review)
        )
        ident = await service.repo.enqueue(mix, automatic=automatic)
        await service.repo.update(ident, "generating", (await service.repo.run(ident))["data"])
        await store.execute("UPDATE playlist_mixes SET enabled=1 WHERE id=?", [mix])
        checked = anyio.Event()

        async def tick() -> None:
            checked.set()

        service.tick = tick  # type: ignore[method-assign]
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(service.run)
                await checked.wait()
                assert (await service.repo.run(ident))["state"] == "failed"
                enabled = (await service.repo.mix(mix, connection().user_id))["enabled"]
                assert bool(enabled) == (review and not automatic)
                group.cancel_scope.cancel()
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


def test_connection_change_during_automatic_selection_pauses_mix(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        await service.repo.connect(connection())
        mix = await service.repo.create(connection().user_id, Definition())
        ident = await service.repo.enqueue(mix, automatic=True)
        await store.execute("UPDATE playlist_mixes SET enabled=1 WHERE id=?", [mix])

        async def validate(c: Connection) -> None:
            pass

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            await service.repo.connect(c.model_copy(update={"model": "changed-model"}))
            return {}

        service.client.validate = validate  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        try:
            await service.process(ident)
            assert (await service.repo.run(ident))["state"] == "failed"
            assert not (await service.repo.mix(mix, connection().user_id))["enabled"]
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("automatic", [False, True])
def test_legacy_queued_and_preview_runs_never_gain_publication_permission(
    tmp_path: Path, automatic: bool
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        owner = connection()
        await service.repo.connect(owner)
        # This is the exact pre-upgrade shape, including a definition without the switch.
        spec = Definition().model_dump(exclude={"review_before_publishing"})
        mix = await service.repo.create(owner.user_id, Definition.model_validate(spec))
        await store.execute(
            "UPDATE playlist_mixes SET definition=?,enabled=?,due=? WHERE id=?",
            [json.dumps(spec), int(automatic), time.time() + 86400, mix],
        )
        await store.execute(
            "INSERT INTO playlist_runs(id,mix,created,state,automatic) VALUES (?,?,?,'queued',?)",
            ["legacy", mix, time.time(), int(automatic)],
        )

        async def validate(c: Connection) -> None:
            pass

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            return {"definition": spec, "selected": [track().model_dump()]}

        async def publish(*args: Any) -> None:
            pytest.fail("An upgrade cannot authorize publication of an old queued run")

        service.client.validate = validate  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        service.publish = publish  # type: ignore[method-assign]
        try:
            await service.tick()
            preview = await service.repo.run("legacy")
            assert preview["state"] == "preview"
            assert not preview["data"]["publish_after_generation"]
            assert not (await service.repo.mix(mix, owner.user_id))["enabled"]
            await service.tick()
            assert await service.repo.run("legacy") == preview
            # A new request against the same legacy definition gets the new default.
            await service.repo.update("legacy", "discarded", preview["data"])
            fresh = await service.repo.enqueue(mix)
            assert (await service.repo.run(fresh))["data"]["publish_after_generation"]
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


def test_scheduled_review_waits_for_publication_without_adding_more_runs(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        owner = connection()
        await service.repo.connect(owner)
        spec = Definition(review_before_publishing=True, cadence="daily")
        mix = await service.repo.create(owner.user_id, spec)
        await store.execute("UPDATE playlist_mixes SET enabled=1,due=0 WHERE id=?", [mix])

        async def validate(c: Connection) -> None:
            pass

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            return {"definition": spec.model_dump(), "selected": [track().model_dump()]}

        async def publish(*args: Any) -> None:
            pytest.fail("Scheduled review must stop before library/native writes")

        service.client.validate = validate  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        service.publish = publish  # type: ignore[method-assign]
        try:
            await service.tick()
            runs = await service.repo.runs(mix)
            assert len(runs) == 1 and runs[0]["state"] == "preview"
            assert runs[0]["automatic"]
            await store.execute("UPDATE playlist_mixes SET due=0 WHERE id=?", [mix])
            await service.tick()
            assert await service.repo.runs(mix) == runs
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


def test_pause_during_scheduled_selection_prevents_publication(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        owner = connection()
        await service.repo.connect(owner)
        mix = await service.repo.create(owner.user_id, Definition())
        ident = await service.repo.enqueue(mix, automatic=True)
        await store.execute("UPDATE playlist_mixes SET enabled=1 WHERE id=?", [mix])

        async def validate(c: Connection) -> None:
            pass

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            await store.execute("UPDATE playlist_mixes SET enabled=0 WHERE id=?", [mix])
            return {"selected": [track().model_dump()]}

        async def publish(*args: Any) -> None:
            pytest.fail("A paused scheduled generation cannot start publication")

        service.client.validate = validate  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        service.publish = publish  # type: ignore[method-assign]
        try:
            await service.process(ident)
            assert (await service.repo.run(ident))["state"] == "discarded"
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


def test_new_direct_publication_recovers_frozen_selection_after_restart(tmp_path: Path) -> None:
    class Interrupted(BaseException):
        pass

    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        owner = connection()
        await service.repo.connect(owner)
        mix = await service.repo.create(owner.user_id, Definition())
        ident = await service.repo.enqueue(mix, enable_after=True)

        async def validate(c: Connection) -> None:
            pass

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            return {"definition": Definition().model_dump(), "selected": [track().model_dump()]}

        async def interrupted(c: Connection, row: dict[str, Any], run: str, data: Any) -> None:
            saved = await service.repo.run(run)
            assert saved["state"] == "publishing"
            assert saved["data"] == data
            raise Interrupted

        service.client.validate = validate  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        service.publish = interrupted  # type: ignore[method-assign]
        try:
            with pytest.raises(Interrupted):
                await service.process(ident)
            frozen = (await service.repo.run(ident))["data"]
            await service.aclose()
            await store.close()
            store = await Store.open(tmp_path / "db")
            service = Playlists(
                store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
            )

            async def no_selection(*args: Any) -> dict[str, Any]:
                pytest.fail("Restart must use the durable frozen selection")

            async def recovered(c: Connection, row: Any, run: str, data: Any) -> None:
                assert data == frozen
                assert data["enable_after"] is True
                await service.repo.update(run, "published", data)

            service.client.validate = validate  # type: ignore[method-assign]
            service.generate = no_selection  # type: ignore[method-assign]
            service.publish = recovered  # type: ignore[method-assign]
            await service.tick()
            assert (await service.repo.run(ident))["state"] == "published"
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


def test_pause_revokes_pending_refreshes_in_old_failed_runs(tmp_path: Path) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        repo = Repository(store)
        owner = connection()
        try:
            await repo.connect(owner)
            mix = await repo.create(owner.user_id, Definition())
            old = await repo.enqueue(mix, enable_after=True)
            await repo.update(old, "failed", (await repo.run(old))["data"], "Saved failure")
            for _ in range(21):
                recent = await repo.enqueue(mix)
                await repo.update(recent, "discarded", {})
            assert old not in [r["id"] for r in await repo.runs(mix)]
            pending = await repo.enqueue(mix, enable_after=True)
            await store.execute("UPDATE playlist_mixes SET enabled=1 WHERE id=?", [mix])
            await repo.pause(mix)
            assert not (await repo.mix(mix, owner.user_id))["enabled"]
            assert not (await repo.run(old))["data"]["enable_after"]
            assert not (await repo.run(pending))["data"]["enable_after"]
            assert (await repo.run(old))["error"] == "Saved failure"
        finally:
            await store.close()

    anyio.run(run)


def test_pause_during_manual_selection_keeps_publication_but_revokes_refreshes(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        owner = connection()
        await service.repo.connect(owner)
        mix = await service.repo.create(owner.user_id, Definition())
        ident = await service.repo.enqueue(mix, enable_after=True)

        async def validate(c: Connection) -> None:
            pass

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            await service.repo.pause(mix)
            return {"selected": [track().model_dump()]}

        async def publish(c: Connection, row: Any, run: str, data: Any) -> None:
            assert not data["enable_after"]
            assert data["publish_after_generation"]
            await service.repo.update(run, "published", data)

        service.client.validate = validate  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        service.publish = publish  # type: ignore[method-assign]
        try:
            await service.process(ident)
            assert (await service.repo.run(ident))["state"] == "published"
            assert not (await service.repo.mix(mix, owner.user_id))["enabled"]
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("pause", [False, True])
def test_pause_during_initial_validation_survives_publication_and_finish(
    tmp_path: Path, pause: bool
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        owner = connection()
        spec = Definition(count=1, exclude_days=0, repeat_days=0)
        selected = track(unfamiliar=True)
        await service.repo.connect(owner)
        mix = await service.repo.create(owner.user_id, spec)
        ident = await service.repo.enqueue(mix, enable_after=True)
        validating, validated = anyio.Event(), anyio.Event()
        native: dict[str, Any] = {}
        calls: list[str] = []

        async def validate(c: Connection) -> None:
            assert c == owner
            if not validating.is_set():
                validating.set()
                await validated.wait()

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            return {"definition": spec.model_dump(), "selected": [selected.model_dump()]}

        async def call(c: Connection, method: str, params: Any = ()) -> dict[str, Any]:
            assert c == owner
            calls.append(method)
            fields = dict(params)
            if method == "getSong":
                assert fields["id"] == selected.id
                return {"song": selected.model_dump()}
            if method == "createPlaylist":
                assert [value for key, value in params if key == "songId"] == [selected.id]
                native.update(
                    id="invented-playlist",
                    owner=owner.username,
                    name=fields["name"],
                    entry=[selected.model_dump()],
                )
            elif method == "updatePlaylist":
                assert fields["playlistId"] == native["id"]
                native["name"] = fields["name"]
            else:
                assert method == "getPlaylist" and fields["id"] == native["id"]
            return {"playlist": dict(native)}

        service.client.validate = validate  # type: ignore[method-assign]
        service.client.call = call  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        try:
            with anyio.fail_after(10):
                async with anyio.create_task_group() as group:
                    group.start_soon(service.process, ident)
                    await validating.wait()
                    if pause:
                        async with service.changes:
                            await service.repo.pause(mix)
                        assert not (await service.repo.run(ident))["data"]["enable_after"]
                    validated.set()
            published = await service.repo.run(ident)
            saved = await service.repo.mix(mix, owner.user_id)
            assert published["state"] == "published", published
            assert published["data"]["publish_after_generation"]
            assert published["data"]["enable_after"] is not pause
            assert bool(saved["enabled"]) is not pause
            assert saved["playlist_id"] == native["id"]
            assert saved["published"] == fingerprint(native)
            assert native["name"] == spec.name
            assert published["data"]["selected"] == [selected.model_dump()]
            assert published["data"]["native_ids"] == [selected.id]
            assert calls.count("createPlaylist") == 1
            assert calls.count("getSong") == 2  # preparation and real finish verification
        finally:
            await service.aclose()
            await store.close()

    anyio.run(run)


@pytest.mark.parametrize("pause", [False, True])
def test_pause_during_publication_failure_survives_uncertain_create_recovery(
    tmp_path: Path, pause: bool
) -> None:
    async def run() -> None:
        store = await Store.open(tmp_path / "db")
        service = Playlists(
            store, SimpleNamespace(base_url="", library_id=1), None, None, PlaylistSettings()
        )
        owner = connection()
        spec = Definition(count=1, exclude_days=0, repeat_days=0)
        selected = track(unfamiliar=True)
        await service.repo.connect(owner)
        mix = await service.repo.create(owner.user_id, spec)
        ident = await service.repo.enqueue(mix, enable_after=True)
        failure_read, release_handler, pause_requested, paused = (
            anyio.Event(),
            anyio.Event(),
            anyio.Event(),
            anyio.Event(),
        )
        native: dict[str, Any] = {}
        calls: list[str] = []
        generated = 0
        response_lost, gated = False, False
        original_run = service.repo.run

        async def validate(c: Connection) -> None:
            assert c == owner

        async def generate(c: Connection, row: dict[str, Any]) -> dict[str, Any]:
            nonlocal generated
            generated += 1
            return {"definition": spec.model_dump(), "selected": [selected.model_dump()]}

        async def call(c: Connection, method: str, params: Any = ()) -> dict[str, Any]:
            nonlocal response_lost
            assert c == owner
            calls.append(method)
            fields = dict(params)
            if method == "getSong":
                assert fields["id"] == selected.id
                return {"song": selected.model_dump()}
            if method == "createPlaylist":
                assert not native  # a retry must never create a second native playlist
                assert [value for key, value in params if key == "songId"] == [selected.id]
                native.update(
                    id="invented-recovered-playlist",
                    owner=owner.username,
                    name=fields["name"],
                    entry=[selected.model_dump()],
                )
                response_lost = True
                raise httpx.ReadError("Synthetic create response lost after native success")
            if method == "getPlaylists":
                return {"playlists": {"playlist": [dict(native)]}}
            if method == "updatePlaylist":
                assert fields["playlistId"] == native["id"]
                native["name"] = fields["name"]
            else:
                assert method == "getPlaylist" and fields["id"] == native["id"]
            return {"playlist": dict(native)}

        async def read_run(run_id: str) -> dict[str, Any]:
            nonlocal gated
            current = await original_run(run_id)
            if response_lost and not gated:
                gated = True
                assert current["state"] == "publishing"
                assert current["data"]["intent"] == "create"
                assert current["data"]["enable_after"] is True
                failure_read.set()
                await release_handler.wait()
            return current

        async def pause_refreshes() -> None:
            pause_requested.set()
            async with service.changes:
                await service.repo.pause(mix)
            paused.set()

        service.client.validate = validate  # type: ignore[method-assign]
        service.client.call = call  # type: ignore[method-assign]
        service.generate = generate  # type: ignore[method-assign]
        service.repo.run = read_run  # type: ignore[method-assign]
        try:
            with anyio.fail_after(10):
                async with anyio.create_task_group() as group:
                    group.start_soon(service.process, ident)
                    await failure_read.wait()
                    if pause:
                        group.start_soon(pause_refreshes)
                        await pause_requested.wait()
                        # Either pause completed in the vulnerable gap or it is queued
                        # behind the corrected failure-handler lock. Release the handler
                        # in both cases: awaiting paused here would deadlock the fix.
                        while (
                            not paused.is_set() and not service.changes.statistics().tasks_waiting
                        ):
                            await anyio.lowlevel.checkpoint()
                    release_handler.set()
            uncertain = await service.repo.run(ident)
            assert uncertain["state"] == "uncertain"
            assert uncertain["data"]["intent"] == "create"
            assert uncertain["data"]["native_ids"] == [selected.id]
            assert not uncertain["data"].get("created_id")
            native_id = native["id"]
            # Retry the frozen selection through the real publisher's provisional-name
            # reconciliation and finish, without another generation or creation.
            async with service.changes:
                await service.repo.update(ident, "publishing", uncertain["data"])
            await service.process(ident)
            published = await service.repo.run(ident)
            saved = await service.repo.mix(mix, owner.user_id)
            assert published["state"] == "published", published
            assert saved["playlist_id"] == native_id == native["id"]
            assert saved["published"] == fingerprint(native)
            assert native["name"] == spec.name
            assert published["data"]["selected"] == [selected.model_dump()]
            assert published["data"]["created_id"] == native_id
            assert generated == 1
            assert calls.count("createPlaylist") == 1
            assert calls.count("getPlaylists") == 1
            assert calls.count("updatePlaylist") == 1
            assert calls.count("getSong") == 2  # preparation and actual recovery finish
            assert (published["data"]["enable_after"], bool(saved["enabled"])) == (
                not pause,
                not pause,
            )
        finally:
            release_handler.set()
            await service.aclose()
            await store.close()

    anyio.run(run)
