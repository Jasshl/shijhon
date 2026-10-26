"""Discovery workflows against disposable stock Navidrome and synthetic history."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiolimiter import AsyncLimiter
from pydantic import SecretStr

from shijhon.catalog.base import SearchResults
from shijhon.catalog.cache import CachedCatalog
from shijhon.catalog.model import CatalogRef, CatalogRelease, release_from_data
from shijhon.dashboard.playlists import definition
from shijhon.playlists.model import Connection, Definition, PlaylistError, Track, fingerprint
from shijhon.playlists.service import Playlists, release_fingerprint
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser, forms
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, write_album
from tests.harness.library import Track as OwnedTrack
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay, fixture

RELEASE = release_from_data(fixture("album-twins-explicit")["body"])
SONGS = RELEASE.tracks[:3]
HISTORY_KEY = "invented-history-key-381"


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    nd.native(
        "POST",
        "user",
        json={
            "userName": "helper",
            "name": "helper",
            "password": "invented-helper-password",
            "isAdmin": True,
        },
    )
    catalog = Replay().catalog()

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(songs=tuple(s for s in SONGS if s.title in term))

    catalog.search = search  # type: ignore[method-assign]
    with delivery_world(nd, tmp_path_factory.mktemp("playlist"), catalog=catalog) as w:
        w.app.dashboard.attempts.limit = 10000
        service = w.services.playlists
        assert service

        async def install() -> None:
            await service.history.http.aclose()

            def response(request: httpx.Request) -> httpx.Response:
                method = request.url.params["method"]
                if method == "user.getRecentTracks":
                    body: dict[str, Any] = {
                        "recenttracks": {
                            "@attr": {
                                "totalPages": "1",
                                "total": "1",
                                "perPage": "200",
                                "page": "1",
                            },
                            "track": [
                                {
                                    "artist": {"#text": "Invented seed artist"},
                                    "name": "Invented seed",
                                    "album": {"#text": "Seed release"},
                                    "date": {"uts": str(int(time.time()) - 86400)},
                                }
                            ],
                        }
                    }
                elif method == "track.getSimilar":
                    body = {
                        "similartracks": {
                            "track": [
                                {"name": s.title, "artist": {"name": s.artist}, "match": "1"}
                                for s in SONGS
                            ]
                        }
                    }
                else:
                    song = next((s for s in SONGS if s.title == request.url.params["track"]), None)
                    body = {
                        "track": {
                            "duration": str(song.duration_ms if song else 0),
                            "album": {"title": RELEASE.title if song else "Seed release"},
                        }
                    }
                return httpx.Response(200, json=body)

            service.history.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
            service.history.pace = AsyncLimiter(10000, 1)

        w.server.call(install)
        yield w


@pytest.fixture
def admin(world: DeliveryWorld) -> Iterator[Browser]:
    browser = Browser(world.server.base_url)
    assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
    if not browser.form("playlists", "playlists/connection").get("history_user"):
        response = browser.submit(
            "playlists",
            {
                "password": ADMIN_PASSWORD,
                "history_user": "invented-history",
                "history_key": HISTORY_KEY,
                "history_source": "manual",
            },
            "playlists/connection",
        )
        assert response.status_code == 303, response.text
    yield browser
    browser.close()


async def connected_admin(service: Playlists) -> Connection:
    row = await service.repo.store.fetchone(
        "SELECT id FROM playlist_owners WHERE username=?", [ADMIN_USER]
    )
    assert row
    return await service.connection(row["id"])


def wait_state(world: DeliveryWorld, ident: str, states: tuple[str, ...]) -> dict[str, Any]:
    service = world.services.playlists
    assert service
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        run = world.server.call(lambda: service.repo.run(ident))
        if run["state"] in states:
            return run
        time.sleep(0.05)
    raise AssertionError(run)


def create_mix(
    world: DeliveryWorld, admin: Browser, *, review: bool = False, enable: bool = False
) -> tuple[str, dict[str, Any]]:
    response = admin.submit(
        "playlists",
        {
            "name": "Invented discovery",
            "count": "3",
            "unfamiliar_percent": "100",
            "max_per_artist": "3",
            "exclude_days": "0",
            "repeat_days": "0",
            "cadence": "weekly",
            "review_before_publishing": "true" if review else "",
            "enable": "true" if enable else "",
        },
    )
    assert response.status_code == 303, response.text
    ident = response.headers["location"].rsplit("/", 1)[1]
    service = world.services.playlists
    assert service
    runs = world.server.call(lambda: service.repo.runs(ident))
    expected = "preview" if review else "published"
    run = wait_state(world, runs[0]["id"], (expected, "failed", "uncertain"))
    assert run["state"] == expected, run
    return ident, run


def test_dashboard_preview_publish_and_owner_isolation(
    world: DeliveryWorld, admin: Browser
) -> None:
    before = world.placeholder_rows()
    ident, run = create_mix(world, admin, review=True)
    assert world.placeholder_rows() == before
    page = admin.get(f"playlists/{ident}")
    assert "whole releases as placeholders" in page.text
    assert HISTORY_KEY not in page.text
    assert ADMIN_PASSWORD not in page.text
    assert len(run["data"]["selected"]) == 3
    helper = Browser(world.server.base_url)
    try:
        assert helper.sign_in("helper", "invented-helper-password").status_code == 303
        assert "Invented discovery" not in helper.get(f"playlists/{ident}").text
        assert (
            admin.post(
                f"playlists/{ident}/action", {"action": "publish", "run": run["id"]}
            ).status_code
            == 403
        )
    finally:
        helper.close()
    result = admin.post(
        f"playlists/{ident}/action",
        {"csrf": admin.csrf(), "action": "publish", "run": run["id"], "enable": "true"},
    )
    assert result.status_code == 303, result.text
    published = wait_state(world, run["id"], ("published", "failed", "uncertain"))
    assert published["state"] == "published", published
    assert world.placeholder_rows() >= before + len(RELEASE.tracks)
    assert published["data"]["native_ids"]
    assert all(not i.startswith("sh.") for i in published["data"]["native_ids"])
    service = world.services.playlists
    assert service

    async def verify() -> None:
        connected = await connected_admin(service)
        mix = await service.repo.mix(ident, connected.user_id)
        assert mix["enabled"] == 1
        saved = await service.client.playlist(connected, mix["playlist_id"])
        assert [s["id"] for s in saved["entry"]] == published["data"]["native_ids"]
        assert saved["name"] == "Invented discovery"
        assert not saved.get("public", False)
        assert mix["published"] == fingerprint(saved)
        await service.repo.store.execute("UPDATE playlist_mixes SET enabled=0 WHERE id=?", [ident])

    world.server.call(verify)


def test_default_creation_rules_and_manual_generation_publish_without_enabling_refreshes(
    world: DeliveryWorld, admin: Browser
) -> None:
    page = admin.get("playlists")
    fields = forms(page.text)["/shijhon/playlists"]
    assert "review_before_publishing" not in fields
    assert "enable" not in fields
    assert "Create and publish" in page.text
    assert "does not download audio" in page.text
    assert '<details class="form-more"><summary>Advanced settings</summary>' in page.text
    ident, first = create_mix(world, admin)
    service = world.services.playlists
    assert service

    async def saved() -> dict[str, Any]:
        owner = await connected_admin(service)
        mix = await service.repo.mix(ident, owner.user_id)
        native = await service.client.playlist(owner, mix["playlist_id"])
        assert [s["id"] for s in native["entry"]] == (await service.repo.runs(ident))[0]["data"][
            "native_ids"
        ]
        assert mix["published"] == fingerprint(native)
        assert not mix["enabled"]
        return mix

    before = world.server.call(saved)
    assert first["data"]["publish_after_generation"] is True
    assert first["data"]["intent"] == "create"
    page = admin.get(f"playlists/{ident}")
    assert "Ready in your music app" in page.text
    assert "<details><summary>Track and release details</summary>" in page.text
    assert 'value="generate">Generate and publish' in page.text
    edit = admin.submit(f"playlists/{ident}", {"name": "Changed discovery", "count": "2"})
    assert edit.status_code == 303, edit.text
    runs = world.server.call(lambda: service.repo.runs(ident))
    changed = wait_state(world, runs[0]["id"], ("published", "failed", "uncertain"))
    assert changed["state"] == "published", changed
    assert len(changed["data"]["native_ids"]) == 2
    assert changed["data"]["intent"] == "replace"
    assert world.server.call(saved)["playlist_id"] == before["playlist_id"]

    result = admin.post(f"playlists/{ident}/action", {"csrf": admin.csrf(), "action": "generate"})
    assert result.status_code == 303, result.text
    runs = world.server.call(lambda: service.repo.runs(ident))
    latest = wait_state(world, runs[0]["id"], ("published", "failed", "uncertain"))
    assert latest["state"] == "published", latest
    assert world.server.call(saved)["playlist_id"] == before["playlist_id"]


@pytest.mark.parametrize("cadence, interval", [("daily", 86400), ("weekly", 7 * 86400)])
def test_refreshes_require_explicit_choice_and_manual_generation_preserves_it(
    world: DeliveryWorld, admin: Browser, cadence: str, interval: int
) -> None:
    ident, _ = create_mix(world, admin)
    service = world.services.playlists
    assert service
    result = admin.submit(f"playlists/{ident}", {"cadence": cadence, "enable": "true"})
    assert result.status_code == 303, result.text
    run = world.server.call(lambda: service.repo.runs(ident))[0]
    assert wait_state(world, run["id"], ("published", "failed"))["state"] == "published"

    async def saved() -> dict[str, Any]:
        owner = await connected_admin(service)
        return await service.repo.mix(ident, owner.user_id)

    assert world.server.call(saved)["enabled"]
    assert time.time() + interval - 30 < world.server.call(saved)["due"]
    result = admin.post(f"playlists/{ident}/action", {"csrf": admin.csrf(), "action": "generate"})
    assert result.status_code == 303, result.text
    run = world.server.call(lambda: service.repo.runs(ident))[0]
    assert wait_state(world, run["id"], ("published", "failed"))["state"] == "published"
    assert world.server.call(saved)["enabled"]
    result = admin.post(f"playlists/{ident}/action", {"csrf": admin.csrf(), "action": "pause"})
    assert result.status_code == 303
    assert not world.server.call(saved)["enabled"]


def test_opt_in_review_applies_to_rule_edits_and_manual_generation(
    world: DeliveryWorld, admin: Browser
) -> None:
    ident, first = create_mix(world, admin)
    service = world.services.playlists
    assert service
    result = admin.submit(f"playlists/{ident}", {"review_before_publishing": "true"})
    assert result.status_code == 303, result.text
    run = world.server.call(lambda: service.repo.runs(ident))[0]
    preview = wait_state(world, run["id"], ("preview", "failed"))
    assert preview["state"] == "preview", preview
    assert not preview["data"]["native_ids"]
    assert preview["data"]["baseline"]
    assert '<input name="review_before_publishing" type="checkbox" value="true" checked>' in (
        admin.get(f"playlists/{ident}").text
    )
    result = admin.post(
        f"playlists/{ident}/action",
        {"csrf": admin.csrf(), "action": "discard", "run": run["id"]},
    )
    assert result.status_code == 303, result.text
    result = admin.post(f"playlists/{ident}/action", {"csrf": admin.csrf(), "action": "generate"})
    assert result.status_code == 303, result.text
    latest = world.server.call(lambda: service.repo.runs(ident))[0]
    assert wait_state(world, latest["id"], ("preview", "failed"))["state"] == "preview"
    assert (world.server.call(lambda: service.repo.run(first["id"])))["state"] == "published"


@pytest.mark.parametrize("edit", [False, True])
def test_pre_upgrade_create_and_rule_forms_still_only_preview(
    world: DeliveryWorld, admin: Browser, edit: bool
) -> None:
    service = world.services.playlists
    assert service
    path = "playlists"
    if edit:

        async def legacy_mix() -> str:
            owner = await connected_admin(service)
            spec = Definition(count=3, max_per_artist=3, exclude_days=0, repeat_days=0)
            ident = await service.repo.create(owner.user_id, spec)
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET definition=? WHERE id=?",
                [json.dumps(spec.model_dump(exclude={"review_before_publishing"})), ident],
            )
            return ident

        path = f"playlists/{world.server.call(legacy_mix)}"
    # These forms were opened before the new publication controls existed. In the
    # edit case, their definition version still matches the legacy stored definition.
    fields = admin.form(path)
    fields.pop("publication_mode", None)
    fields.pop("review_before_publishing", None)
    fields.pop("enable", None)
    fields.update(
        name="Legacy preview request",
        count="3",
        max_per_artist="3",
        exclude_days="0",
        repeat_days="0",
    )
    before = world.placeholder_rows()
    result = admin.post(path, fields)
    assert result.status_code == 303, result.text
    ident = result.headers["location"].rsplit("/", 1)[1]
    run = world.server.call(lambda: service.repo.runs(ident))[0]
    preview = wait_state(world, run["id"], ("preview", "published", "failed", "uncertain"))
    assert preview["state"] == "preview", preview
    assert not preview["data"]["publish_after_generation"]
    assert not preview["data"]["native_ids"]
    assert not preview["data"]["intent"]
    assert world.placeholder_rows() == before

    async def saved() -> dict[str, Any]:
        owner = await connected_admin(service)
        return await service.repo.mix(ident, owner.user_id)

    mix = world.server.call(saved)
    assert not mix["playlist_id"] and not mix["enabled"]


def test_stale_generate_preview_button_keeps_preview_intent_after_rule_change(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident, _ = create_mix(world, admin, review=True)
    service = world.services.playlists
    assert service
    # Keep tab A's actual button value while tab B changes to direct publication.
    page = admin.get(f"playlists/{ident}")
    button = re.search(
        r'<button[^>]* name="action" value="([^"]+)">Generate preview</button>', page.text
    )
    assert button, page.text
    stale_form = {"csrf": admin.csrf(), "action": button.group(1)}
    result = admin.submit(f"playlists/{ident}", {"review_before_publishing": ""})
    assert result.status_code == 303, result.text
    changed = world.server.call(lambda: service.repo.runs(ident))[0]
    assert wait_state(world, changed["id"], ("published", "failed"))["state"] == "published"
    before = world.placeholder_rows()
    original_call = service.client.call

    async def no_writes(c: Connection, method: str, params: Any = ()) -> dict[str, Any]:
        assert method not in ("createPlaylist", "updatePlaylist"), method
        return await original_call(c, method, params)

    monkeypatch.setattr(service.client, "call", no_writes)
    result = admin.post(f"playlists/{ident}/action", stale_form)
    assert result.status_code == 303, result.text
    run = world.server.call(lambda: service.repo.runs(ident))[0]
    preview = wait_state(world, run["id"], ("preview", "published", "failed", "uncertain"))
    assert preview["state"] == "preview", preview
    assert not preview["data"]["publish_after_generation"]
    assert not preview["data"]["definition"]["review_before_publishing"]
    assert not preview["data"]["native_ids"]
    assert world.placeholder_rows() == before


def test_manual_only_schedule_cannot_enable_refreshes(world: DeliveryWorld, admin: Browser) -> None:
    result = admin.submit("playlists", {"cadence": "manual", "enable": "true"})
    assert result.status_code == 200
    assert "Choose every day or every week" in result.text


def frozen_run(
    world: DeliveryWorld,
    *,
    target: str = "",
    baseline: str = "",
    intent: str = "",
    provisional: str = "",
    ids: list[str] | None = None,
    release: CatalogRelease = RELEASE,
    song_index: int = 0,
) -> tuple[str, str]:
    service = world.services.playlists
    assert service

    async def prepare() -> tuple[str, str]:
        connected = await connected_admin(service)
        definition = Definition(count=1, unfamiliar_percent=100, repeat_days=0)
        mix = await service.repo.create(connected.user_id, definition)
        if target:
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET playlist_id=?,published=? WHERE id=?",
                [target, baseline, mix],
            )
        song = release.tracks[song_index]
        selected = Track(
            id=f"sh.tr.demo.{song.ref.id}",
            artist=song.artist,
            title=song.title,
            album=release.title,
            duration=song.seconds,
            isrc=song.isrc or "",
            explicit=song.explicit,
            release=str(song.album),
            unfamiliar=True,
        )
        run = await service.repo.enqueue(mix)
        await service.repo.update(
            run,
            "preview",
            {
                "definition": definition.model_dump(),
                "selected": [selected.model_dump()],
                "releases": [
                    {
                        "ref": str(release.ref),
                        "fingerprint": release_fingerprint(release),
                        "title": release.title,
                        "artist": release.artist,
                        "tracks": len(release.tracks),
                    }
                ],
                "baseline": baseline,
                "native_ids": ids or [],
                "intent": intent,
                "provisional": provisional,
                "created_id": "",
                "history_count": 1,
                "history_from": int(time.time()) - 86400,
                "skipped": 0,
            },
        )
        return mix, run

    return world.server.call(prepare)


def publish_frozen(world: DeliveryWorld, run: str) -> dict[str, Any]:
    service = world.services.playlists
    assert service

    async def work() -> dict[str, Any]:
        row = await service.repo.run(run)
        await service.repo.update(run, "publishing", row["data"])
        await service.process(run)
        return await service.repo.run(run)

    return world.server.call(work)


def test_uncertain_creation_recovers_once_without_regenerating(
    world: DeliveryWorld, admin: Browser
) -> None:
    service = world.services.playlists
    assert service

    async def create() -> tuple[str, str]:
        connected = await connected_admin(service)
        song = SONGS[0]
        assert world.services.commits
        mapping = await world.services.commits.natives({f"sh.tr.demo.{song.ref.id}"})
        native = mapping[f"sh.tr.demo.{song.ref.id}"]
        name = "Shijhon preparing invented-unique-intent"
        playlist = (
            await service.client.call(
                connected, "createPlaylist", [("name", name), ("songId", native)]
            )
        )["playlist"]
        return native, playlist["id"]

    native, target = world.server.call(create)
    mix, run = frozen_run(
        world, intent="create", provisional="Shijhon preparing invented-unique-intent", ids=[native]
    )
    result = publish_frozen(world, run)
    assert result["state"] == "published", result
    connected = world.server.call(lambda: connected_admin(service))
    saved = world.server.call(lambda: service.repo.mix(mix, connected.user_id))
    assert saved["playlist_id"] == target
    # Another attempt cannot republish a completed run.
    world.server.call(lambda: service.process(run))
    assert world.server.call(lambda: service.repo.run(run))["state"] == "published"


def test_ambiguous_create_with_no_match_never_creates_another(
    world: DeliveryWorld, admin: Browser
) -> None:
    _, run = frozen_run(
        world,
        intent="create",
        provisional="Shijhon preparing absent-intent",
        ids=["missing-native"],
    )
    result = publish_frozen(world, run)
    assert result["state"] == "uncertain"
    assert "no second playlist" in result["error"]


def test_external_playlist_edit_is_preserved(world: DeliveryWorld, admin: Browser) -> None:
    service = world.services.playlists
    assert service

    async def create() -> tuple[str, str]:
        connected = await connected_admin(service)
        playlist = (
            await service.client.call(connected, "createPlaylist", [("name", "User edits")])
        )["playlist"]
        before = fingerprint(await service.client.playlist(connected, playlist["id"]))
        await service.client.call(
            connected,
            "updatePlaylist",
            [("playlistId", playlist["id"]), ("comment", "Keep my edit")],
        )
        return playlist["id"], before

    target, baseline = world.server.call(create)
    _, run = frozen_run(world, target=target, baseline=baseline)
    result = publish_frozen(world, run)
    assert result["state"] == "failed"
    assert "changed after" in result["error"]
    connected = world.server.call(lambda: connected_admin(service))
    saved = world.server.call(lambda: service.client.playlist(connected, target))
    assert saved["comment"] == "Keep my edit"
    assert not saved.get("entry", [])


def test_fresh_identity_guard_rejects_recreated_or_invalid_owner(
    world: DeliveryWorld, admin: Browser
) -> None:
    service = world.services.playlists
    assert service

    async def check() -> None:
        connected = await connected_admin(service)
        with pytest.raises(PlaylistError):
            await service.client.validate(
                connected.model_copy(update={"user_id": "deleted-owner-id"})
            )
        with pytest.raises(PlaylistError):
            await service.client.validate(
                connected.model_copy(update={"token": SecretStr("invalid-token")})
            )

    world.server.call(check)


def test_scheduled_refresh_reuses_playlist_id_and_coalesces_missed_intervals(
    world: DeliveryWorld, admin: Browser
) -> None:
    mix_id, run_id = frozen_run(world)
    first = publish_frozen(world, run_id)
    assert first["state"] == "published", first
    service = world.services.playlists
    assert service

    async def due() -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        connection = await connected_admin(service)
        before = await service.repo.mix(mix_id, connection.user_id)
        await service.repo.store.execute(
            "UPDATE playlist_mixes SET enabled=1,due=? WHERE id=?",
            [time.time() - 30 * 86400, mix_id],
        )
        await service.tick()
        after = await service.repo.mix(mix_id, connection.user_id)
        runs = await service.repo.runs(mix_id)
        await service.repo.store.execute("UPDATE playlist_mixes SET enabled=0 WHERE id=?", [mix_id])
        return before["playlist_id"], after, runs

    before, after, runs = world.server.call(due)
    assert after["playlist_id"] == before
    assert len(runs) == 2
    assert runs[0]["automatic"] == 1 and runs[0]["state"] == "published", runs
    assert after["due"] > time.time()


def test_recreated_username_cannot_read_previous_owner_history(
    world: DeliveryWorld, admin: Browser
) -> None:
    service = world.services.playlists
    assert service

    async def prepare() -> str:
        native = await service.client.login("helper", "invented-helper-password", "127.0.0.1")
        old = Connection(
            user_id="old-helper-id",
            username="helper",
            token=SecretStr("old-token"),
            salt=SecretStr("old-salt"),
            history_user="private-old-history",
            history_key=SecretStr("key"),
        )
        await service.repo.connect(old)
        mix = await service.repo.create(old.user_id, Definition(name="Private old mix"))
        assert native["id"] != old.user_id
        return mix

    mix = world.server.call(prepare)
    browser = Browser(world.server.base_url)
    try:
        assert browser.sign_in("helper", "invented-helper-password").status_code == 303
        assert "private-old-history" not in browser.get("playlists").text
        assert "Private old mix" not in browser.get(f"playlists/{mix}").text
        assert browser.post("playlists/disconnect", {"csrf": browser.csrf()}).status_code == 303
        assert world.server.call(lambda: service.repo.connection("old-helper-id")) is not None
        connected = browser.submit(
            "playlists",
            {
                "password": "invented-helper-password",
                "history_user": "new-helper-history",
                "history_key": HISTORY_KEY,
                "history_source": "manual",
            },
            "playlists/connection",
        )
        assert connected.status_code == 303, connected.text
        assert "private-old-history" not in browser.get("playlists").text
        assert "new-helper-history" in browser.get("playlists").text
        assert world.server.call(lambda: service.repo.connection("old-helper-id")) is not None
    finally:
        browser.close()
        world.server.call(
            lambda: service.repo.store.execute(
                "DELETE FROM playlist_owners WHERE username='helper'"
            )
        )


def test_changed_release_between_preflight_and_write_is_refused(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = world.services.playlists
    assert service and isinstance(service.catalog, CachedCatalog)
    catalog = service.catalog
    original = release_from_data(fixture("album-anniversary-standard")["body"])
    extra = dataclasses.replace(
        original.tracks[0],
        ref=CatalogRef("demo", "999999001"),
        title="Unreviewed extra recording",
        number=99,
    )
    changed = dataclasses.replace(original, tracks=(*original.tracks, extra))
    original_album = catalog.inner.album
    cached_album = catalog.album
    calls = 0

    async def changing_album(ident: str) -> CatalogRelease:
        nonlocal calls
        if ident == original.ref.id:
            calls += 1
            return original if calls == 1 else changed
        return await original_album(ident)

    cached_album.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(catalog.inner, "album", changing_album)
    monkeypatch.setattr(catalog, "album", changing_album)
    before = world.placeholder_rows()
    _, run = frozen_run(world, release=original)
    try:
        result = publish_frozen(world, run)
        assert result["state"] == "failed", result
        assert calls >= 2
        assert world.placeholder_rows() == before
    finally:
        monkeypatch.setattr(catalog.inner, "album", original_album)
        monkeypatch.setattr(catalog, "album", cached_album)
        cached_album.cache_clear()  # type: ignore[attr-defined]
    # The task-local guard is reset: a normal commit still works after the refusal.
    world.materialize(original)
    assert world.placeholder_rows() == before + len(original.tracks)


def test_api_destination_change_does_not_reuse_previous_key(
    world: DeliveryWorld, admin: Browser
) -> None:
    service = world.services.playlists
    assert service
    response = admin.submit(
        "playlists",
        {
            "backend": "api",
            "endpoint": "https://first.invalid/v1",
            "model": "invented-model",
            "api_key": "invented-api-secret",
        },
        "playlists/connection",
    )
    assert response.status_code == 303
    response = admin.submit(
        "playlists",
        {
            "backend": "api",
            "endpoint": "https://second.invalid/v1",
            "model": "invented-model",
            "api_key": "",
        },
        "playlists/connection",
    )
    assert response.status_code == 303
    connected = world.server.call(lambda: connected_admin(service))
    assert not connected.api_key.get_secret_value()
    assert "invented-api-secret" not in admin.get("playlists").text
    assert (
        admin.submit(
            "playlists", {"backend": "none", "endpoint": "", "model": ""}, "playlists/connection"
        ).status_code
        == 303
    )


@pytest.mark.parametrize("backend", ["none", "codex", "claude"])
def test_connection_ignores_fields_unused_by_backend(
    world: DeliveryWorld, admin: Browser, backend: str
) -> None:
    service = world.services.playlists
    assert service
    try:
        response = admin.submit(
            "playlists",
            {
                "backend": backend,
                "endpoint": "invalid stale URL with credentials",
                "model": "invented-model",
                "api_key": "unused-secret",
            },
            "playlists/connection",
        )
        assert response.status_code == 303, response.text
        owner = world.server.call(lambda: connected_admin(service))
        assert owner.endpoint == ""
        assert not owner.api_key.get_secret_value()
        assert owner.model == ("" if backend == "none" else "invented-model")
        assert definition({"name": "Invented", "brief": "stale brief"}).brief == ""
    finally:
        assert (
            admin.submit("playlists", {"backend": "none"}, "playlists/connection").status_code
            == 303
        )


def test_native_history_key_connection(navidrome_factory: NavidromeFactory, tmp_path: Path) -> None:
    # Enable only the local link route. No metadata agents, account linking or
    # listening requests run against the external service.
    nd = navidrome_factory(
        {
            "ND_ENABLEEXTERNALSERVICES": "true",
            "ND_AGENTS": "",
            "ND_LASTFM_ENABLED": "true",
            "ND_LASTFM_APIKEY": HISTORY_KEY,
            "ND_LASTFM_SECRET": "invented-application-secret",
            "ND_DEEZER_ENABLED": "false",
            "ND_LISTENBRAINZ_ENABLED": "false",
        }
    )
    with delivery_world(nd, tmp_path, catalog=Replay().catalog()) as w:
        service = w.services.playlists
        assert service
        browser = Browser(w.server.base_url)
        try:
            assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
            assert 'value="navidrome" selected' in browser.get("playlists").text
            response = browser.submit(
                "playlists",
                {"password": ADMIN_PASSWORD, "history_user": "invented-history"},
                "playlists/connection",
            )
            assert response.status_code == 303, response.text
            owner = w.server.call(lambda: connected_admin(service))
            assert owner.history_key.get_secret_value() == HISTORY_KEY
            assert owner.history_source == "navidrome"
            assert nd.login()["token"] not in owner.stored()
            assert "linkToken" not in owner.stored()
            assert (
                browser.submit(
                    "playlists", {"history_key": "unused-manual-key"}, "playlists/connection"
                ).status_code
                == 303
            )
            assert w.server.call(lambda: connected_admin(service)).history_key == owner.history_key
            refused = browser.submit(
                "playlists", {"history_source": "manual"}, "playlists/connection"
            )
            assert "Enter a Last.fm API key" in refused.text
            assert 'value="manual" selected' in refused.text
            assert (
                browser.submit(
                    "playlists",
                    {"history_source": "manual", "history_key": "invented-manual-key"},
                    "playlists/connection",
                ).status_code
                == 303
            )
            refused = browser.submit(
                "playlists", {"history_source": "navidrome"}, "playlists/connection"
            )
            assert "Enter your Navidrome password to read" in refused.text
            assert w.server.call(lambda: connected_admin(service)).history_source == "manual"
            assert all(
                HISTORY_KEY not in r.text and "invented-manual-key" not in r.text
                for r in browser.seen
            )
            # Reading the application key has not linked a personal account.
            assert nd.native("GET", "lastfm/link").json()["status"] is False
            page = browser.get("playlists")
            assert "; script-src 'self'" in page.headers["content-security-policy"]
            assert "connect-src" not in page.headers["content-security-policy"]
        finally:
            browser.close()


def test_disabled_native_history_key_keeps_existing_connection(
    world: DeliveryWorld, admin: Browser
) -> None:
    service = world.services.playlists
    assert service
    before = world.server.call(lambda: connected_admin(service)).stored()
    response = admin.submit(
        "playlists",
        {"password": ADMIN_PASSWORD, "history_source": "navidrome"},
        "playlists/connection",
    )
    assert "key is unavailable" in response.text
    assert 'value="navidrome" selected' in response.text
    assert world.server.call(lambda: connected_admin(service)).stored() == before


def test_lost_create_response_reconciles_saved_intent_without_duplicate(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = world.services.playlists
    assert service
    original_call = service.client.call
    creations = 0

    async def lose_response(c: Connection, method: str, params: Any = ()) -> dict[str, Any]:
        nonlocal creations
        reply = await original_call(c, method, params)
        if method == "createPlaylist":
            creations += 1
            raise PlaylistError("Synthetic response lost after native creation")
        return reply

    monkeypatch.setattr(service.client, "call", lose_response)
    _, run = frozen_run(world)
    interrupted = publish_frozen(world, run)
    assert interrupted["state"] == "uncertain", interrupted
    assert interrupted["data"]["intent"] == "create"
    monkeypatch.setattr(service.client, "call", original_call)
    resumed = publish_frozen(world, run)
    assert resumed["state"] == "published", resumed
    assert creations == 1
    assert resumed["data"]["native_ids"] == interrupted["data"]["native_ids"]


def test_default_publication_retry_keeps_frozen_selection_and_pause(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = world.services.playlists
    assert service
    original_call = service.client.call
    creations = 0

    async def lose_response(c: Connection, method: str, params: Any = ()) -> dict[str, Any]:
        nonlocal creations
        result = await original_call(c, method, params)
        if method == "createPlaylist":
            creations += 1
            raise PlaylistError("The native creation response was lost.")
        return result

    monkeypatch.setattr(service.client, "call", lose_response)
    response = admin.submit(
        "playlists",
        {
            "name": "Interrupted direct publication",
            "count": "3",
            "max_per_artist": "3",
            "exclude_days": "0",
            "repeat_days": "0",
            "enable": "true",
        },
    )
    assert response.status_code == 303, response.text
    ident = response.headers["location"].rsplit("/", 1)[1]
    run = world.server.call(lambda: service.repo.runs(ident))[0]
    interrupted = wait_state(world, run["id"], ("uncertain", "failed"))
    assert interrupted["state"] == "uncertain", interrupted
    assert interrupted["data"]["publish_after_generation"]
    assert interrupted["data"]["intent"] == "create"
    monkeypatch.setattr(service.client, "call", original_call)

    async def no_selection(c: Connection, mix: dict[str, Any]) -> dict[str, Any]:
        pytest.fail("Recovery must not choose new tracks")

    monkeypatch.setattr(service, "generate", no_selection)
    result = admin.post(f"playlists/{ident}/action", {"csrf": admin.csrf(), "action": "pause"})
    assert result.status_code == 303, result.text
    result = admin.post(
        f"playlists/{ident}/action",
        {"csrf": admin.csrf(), "action": "recheck", "run": run["id"]},
    )
    assert result.status_code == 303, result.text
    resumed = wait_state(world, run["id"], ("published", "failed", "uncertain"))
    assert resumed["state"] == "published", resumed
    assert creations == 1
    assert resumed["data"]["selected"] == interrupted["data"]["selected"]
    assert resumed["data"]["native_ids"] == interrupted["data"]["native_ids"]

    async def saved() -> dict[str, Any]:
        owner = await connected_admin(service)
        return await service.repo.mix(ident, owner.user_id)

    assert not world.server.call(saved)["enabled"]


def test_restore_plan_cannot_add_recordings_outside_preview(
    world: DeliveryWorld, admin: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = world.services.playlists
    assert service and isinstance(service.catalog, CachedCatalog)
    catalog = service.catalog
    full = release_from_data(fixture("album-ep-without-suffix")["body"])
    short = dataclasses.replace(full, tracks=full.tracks[:1], track_count=1)
    before = world.placeholder_rows()
    world.materialize(short)
    world.materialize(full)
    removal = world.server.call(lambda: world.services.engine.remove_release(str(full.ref)))
    assert removal.removed == len(full.tracks)
    assert world.placeholder_rows() == before
    original = catalog.inner.album

    async def current(ident: str) -> CatalogRelease:
        return short if ident == short.ref.id else await original(ident)

    monkeypatch.setattr(catalog.inner, "album", current)
    monkeypatch.setattr(catalog, "album", current)
    _, run = frozen_run(world, release=short)
    result = publish_frozen(world, run)
    assert result["state"] == "failed", result
    assert world.placeholder_rows() == before
    stored = world.server.call(lambda: world.services.engine.removed_record(str(full.ref)))
    assert stored is not None


def test_matching_publication_intent_still_checks_recording_identity(
    world: DeliveryWorld, admin: Browser
) -> None:
    service = world.services.playlists
    assert service and world.services.commits

    async def prepare_native() -> str:
        c = await connected_admin(service)
        mapping = await world.services.commits.natives({f"sh.tr.demo.{SONGS[0].ref.id}"})
        native = mapping[f"sh.tr.demo.{SONGS[0].ref.id}"]
        await service.client.call(
            c, "createPlaylist", [("name", "Shijhon preparing identity-proof"), ("songId", native)]
        )
        return native

    native = world.server.call(prepare_native)
    _, run = frozen_run(
        world, intent="create", provisional="Shijhon preparing identity-proof", ids=[native]
    )

    async def change_frozen() -> None:
        state = await service.repo.run(run)
        state["data"]["selected"][0].update({"explicit": False, "clean": True, "isrc": ""})
        await service.repo.update(run, "preview", state["data"])

    world.server.call(change_frozen)
    checked = publish_frozen(world, run)
    assert checked["state"] == "uncertain", checked
    assert "no longer the selected recording" in checked["error"]


@pytest.mark.parametrize("vanished", [False, True])
def test_publication_uses_existing_plan_or_falls_back_after_owned_files_vanish(
    navidrome_factory: NavidromeFactory, tmp_path: Path, vanished: bool
) -> None:
    nd = navidrome_factory()
    base = RELEASE
    alternate_ref = CatalogRef("demo", "alternatealbum")
    alternate = dataclasses.replace(
        base,
        ref=alternate_ref,
        tracks=tuple(
            dataclasses.replace(
                t, ref=CatalogRef("demo", f"alternate{t.ref.id}"), album=alternate_ref
            )
            for t in base.tracks
        ),
    )
    owned = Album(
        base.artist,
        base.title,
        tuple(
            OwnedTrack(t.title, t.number, t.disc, seconds=t.seconds, isrc=t.isrc)
            for t in base.tracks[:2]
        ),
        release_date=base.release_date,
        recording_date=base.release_date,
        extra={"itunesadvisory": ["1"]},
    )
    write_album(nd.music, owned)
    nd.scan()
    album_id = nd.client().ok("getAlbumList2", {"type": "newest"})["albumList2"]["album"][0]["id"]
    before = nd.client().ok("getAlbum", {"id": album_id})["album"]["song"]
    files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in nd.music.rglob("*.flac")}
    catalog = Replay().catalog()

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(albums=(base,), songs=alternate.tracks)

    async def album(ident: str) -> CatalogRelease:
        return alternate if ident == alternate_ref.id else base

    async def song(ident: str) -> Any:
        return next(t for r in (base, alternate) for t in r.tracks if t.ref.id == ident)

    catalog.search, catalog.album, catalog.song = search, album, song  # type: ignore[method-assign]
    with delivery_world(
        nd,
        tmp_path,
        catalog=catalog,
        fill={"library_pass": "dry_run", "auto_min_songs": 999999, "auto_min_share": 1.0},
    ) as w:
        service, fills = w.services.playlists, w.services.fills
        assert service and fills
        decision = w.server.call(lambda: fills.fill(album_id, hold=True))
        assert decision and decision.release and decision.outcome == "deferred"
        browser = Browser(w.server.base_url)
        try:
            assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
            assert (
                browser.submit(
                    "playlists",
                    {
                        "password": ADMIN_PASSWORD,
                        "history_user": "invented-history",
                        "history_source": "manual",
                        "history_key": HISTORY_KEY,
                    },
                    "playlists/connection",
                ).status_code
                == 303
            )
            if vanished:
                shutil.rmtree(nd.music / owned.relative_folder)
                nd.scan(full=True)
                assert not w.server.call(lambda: w.services.engine.owned_album(album_id)).songs
            _, ident = frozen_run(w, release=base if vanished else alternate, song_index=2)

            async def freeze_matched() -> None:
                run = await service.repo.run(ident)
                original = Track.model_validate(run["data"]["selected"][0])
                matched = await service.matched_edition(original)
                assert matched and matched.release == str(base.ref)
                assert matched.isrc == original.isrc
                assert not await service.repo.store.fetchall("SELECT * FROM placeholders")
                run["data"]["selected"] = [matched.model_dump()]
                run["data"]["releases"] = [
                    {
                        "ref": str(base.ref),
                        "fingerprint": release_fingerprint(base),
                        "title": base.title,
                        "artist": base.artist,
                        "tracks": len(base.tracks),
                    }
                ]
                await service.repo.update(ident, "preview", run["data"])

            w.server.call(freeze_matched)
            published = publish_frozen(w, ident)
            assert published["state"] == "published", published["error"]
            assert len(published["data"]["native_ids"]) == 1
            assert not w.server.call(
                lambda: service.repo.store.fetchall("SELECT * FROM pending_placeholders")
            )
            if not vanished:
                assert all(
                    hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in files.items()
                )
                after = nd.client().ok("getAlbum", {"id": album_id})["album"]["song"]
                by_id = {s["id"]: s for s in after}
                for s in before:
                    for key in ("id", "title", "artist", "track", "discNumber", "albumId"):
                        assert by_id[s["id"]].get(key) == s.get(key)
        finally:
            browser.close()
