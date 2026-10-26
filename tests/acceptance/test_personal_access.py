"""Personal owners, current grants and immutable identity against disposable Navidrome."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import SecretStr

from shijhon.access import AccessDenied, AccessService
from shijhon.agents.tools import Choices, Tools
from shijhon.cli import main
from shijhon.dashboard.saved import effective_from_database, list_saved
from shijhon.navidrome.client import NavidromeError
from shijhon.playlists.model import Connection, Definition, PlaylistError
from shijhon.playlists.requests import RequestError
from tests.acceptance.test_R_playlists import wait_state
from tests.conftest import NavidromeFactory
from tests.harness.agents import connect_agent
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

PASSWORD = "invented-personal-password"
HOST_KEY = "invented-shared-host-key"


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory()
    other = nd.root / "other-music"
    other.mkdir()
    library = int(
        nd.native(
            "POST",
            "library",
            json={"name": "Other invented library", "path": str(other), "defaultNewUsers": False},
        ).json()["id"]
    )
    for username in ("listener-a", "listener-b", "blocked", "renewed"):
        nd.create_user(username, PASSWORD)
    nd.native(
        "POST",
        "user",
        json={"userName": "manager", "name": "manager", "password": PASSWORD, "isAdmin": True},
    )
    blocked = next(u for u in nd.native("GET", "user").json() if u["userName"] == "blocked")
    nd.native("PUT", f"user/{blocked['id']}/library", json={"libraryIds": [library]})
    listener = next(u for u in nd.native("GET", "user").json() if u["userName"] == "listener-a")
    nd.native("PUT", f"user/{listener['id']}/library", json={"libraryIds": [1]})
    write_album(
        nd.music,
        Album("Glass Orchard", "Paper Garden", (Track("First Light", 1), Track("Harbor", 2))),
    )
    nd.scan()
    with delivery_world(nd, tmp_path_factory.mktemp("personal"), agents=True) as w:
        w.app.dashboard.attempts.limit = 10000
        yield w


def signed(world: DeliveryWorld, username: str) -> Browser:
    b = Browser(world.server.base_url)
    reply = b.sign_in(username, ADMIN_PASSWORD if username == ADMIN_USER else PASSWORD)
    assert reply.status_code == 303, reply.text
    return b


def owner(world: DeliveryWorld, username: str) -> str:
    return next(u["id"] for u in world.nd.native("GET", "user").json() if u["userName"] == username)


def test_personal_navigation_profiles_and_installation_denials(world: DeliveryWorld) -> None:
    a, b = signed(world, "listener-a"), signed(world, "listener-b")
    blocked = Browser(world.server.base_url)
    try:
        assert a.get("").headers["location"] == "/shijhon/collection"
        page = a.get("playlists")
        assert page.status_code == 200 and "Connected as listener-a." in page.text
        assert 'name="password" autocomplete="current-password" required' not in page.text
        for path in (
            "addons",
            "playback",
            "catalog",
            "library",
            "cleanup",
            "diagnostics",
            "restarting",
        ):
            assert a.get(path).status_code == 403
            assert f'href="/shijhon/{path}"' not in page.text
        for path in (
            "addons",
            "addons/configuration",
            "addons/1/settings",
            "catalog/check",
            "catalog/wait",
            "playback",
            "library/policy",
            "library/pass",
            "library/settings",
            "library/review/other",
            "library/no-match/other",
            "cleanup/settings",
            "cleanup/downloads",
            "cleanup/check",
            "restart",
        ):
            assert a.post(path, {"csrf": a.csrf()}).status_code == 403
        assert blocked.sign_in("blocked", PASSWORD).status_code == 403
        assert blocked.get("playlists").status_code == 303
        assert a.submit("appearance", {"accent": "purple"}).status_code == 303
        assert 'data-accent="purple"' in a.get("playlists").text
        assert 'data-accent="green"' in b.get("playlists").text
        assert a.post("appearance", {"csrf": b.csrf(), "accent": "teal"}).status_code == 403
        assert (
            a.post(
                "appearance",
                {"csrf": a.csrf(), "accent": "teal"},
                headers={"sec-fetch-site": "cross-site"},
            ).status_code
            == 403
        )
        assert (
            a.submit(
                "playlists",
                {
                    "history_user": "invented-profile-a",
                    "history_source": "manual",
                    "history_key": "invented-profile-key",
                },
                "playlists/connection",
            ).status_code
            == 303
        )
        assert "invented-profile-a" not in b.get("playlists").text
    finally:
        a.close()
        b.close()
        blocked.close()


def test_host_ai_requires_admin_and_personal_model_choices_are_scoped(
    world: DeliveryWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    admin, a = signed(world, ADMIN_USER), signed(world, "listener-a")
    service = world.services.playlists
    assert service and world.app.agents
    try:
        response = admin.submit(
            "playlists",
            {
                "backend": "api",
                "endpoint": "https://shared-model.invalid/v1",
                "model": "invented-model",
                "api_key": HOST_KEY,
                "allowed_models": "approved-model",
            },
            "playlists/host-ai",
        )
        assert response.status_code == 303, response.text
        before = world.server.call(service.repo.shared_ai)
        assert HOST_KEY not in list_saved(world.app.configured.database_path)
        main(["saved", "--database", str(world.app.configured.database_path)])
        listing = capsys.readouterr().out
        assert "playlist-ai.connection = (secret)" in listing
        assert HOST_KEY not in listing and "shared-model.invalid" not in listing
        applied = effective_from_database(world.app.configured, world.app.configured.database_path)
        assert HOST_KEY not in json.dumps(applied.model_dump(mode="json"))
        diagnostics = admin.get("diagnostics")
        assert diagnostics.status_code == 200
        assert all(
            value not in diagnostics.text
            for value in (HOST_KEY, "shared-model.invalid", "invented-model")
        )
        assert HOST_KEY not in admin.get("playlists").text
        assert (
            a.submit("playlists", {"ai_mode": "shared"}, "playlists/connection").status_code == 303
        )
        connection = world.server.call(lambda: service.connection(owner(world, "listener-a")))
        assert connection.use_shared_ai and connection.api_key.get_secret_value() == HOST_KEY
        service.requests.usable_ai(connection)
        raw = world.server.call(
            lambda: service.repo.store.fetchone(
                "SELECT connection FROM playlist_owners WHERE id=?", [connection.user_id]
            )
        )
        assert HOST_KEY not in raw["connection"] and "shared-model.invalid" not in raw["connection"]
        page = a.get("playlists").text
        assert all(value not in page for value in (HOST_KEY, "shared-model.invalid"))
        assert "invented-model" in page and "approved-model" in page
        assert not any(
            f'name="{key}"' in page for key in ("endpoint", "api_key", "backend", "model")
        )
        for route in ("connection", "host-ai", "private-ai"):
            denied = a.post(
                f"playlists/{route}",
                {
                    "csrf": a.csrf(),
                    "backend": "api",
                    "endpoint": "https://forged.invalid",
                    "model": "forged",
                    "allowed_models": "forged",
                },
            )
            assert "Only administrators" in denied.text
        assert world.server.call(service.repo.shared_ai) == before
        selected = a.submit(
            "playlists",
            {"preferred_model": "approved-model", "ai_mode": "shared"},
            "playlists/ai-preferences",
        )
        assert selected.status_code == 303
        assert world.server.call(service.repo.shared_ai) == before
        assert (
            "Personal AI preferences"
            in a.post(
                "playlists/ai-preferences",
                {"csrf": a.csrf(), "ai_mode": "shared", "endpoint": "https://forged.invalid"},
            ).text
        )
        tokens = connect_agent(
            a.http, world.server.base_url, "listener-a", PASSWORD, "create status library"
        )
        personal_tokens = connect_agent(
            a.http, world.server.base_url, "listener-a", PASSWORD, "create status settings"
        )
        oauth = world.app.agents.oauth

        async def check() -> None:
            # An agent needs explicit personal-settings consent; it gains no host authority.
            token = await oauth.verify_token(tokens["access_token"])
            assert token
            with (
                patch("shijhon.agents.tools.get_access_token", return_value=token),
                pytest.raises(RequestError, match="Reconnect and grant"),
            ):
                await Tools(oauth).configure(Choices(model="forged"))
            with pytest.raises(PlaylistError, match="Only administrators"):
                # The service boundary independently refuses host/model edits.
                await service.save_connection(
                    connection.model_copy(update={"model": "forged", "use_shared_ai": False})
                )
            checked = await oauth.verify_token(personal_tokens["access_token"])
            assert checked and "settings" in checked.scopes
            with patch("shijhon.agents.tools.get_access_token", return_value=checked):
                updated = await Tools(oauth).configure(Choices(model="approved-model"))
                assert updated["updated"] == ["model"]
                with pytest.raises(PlaylistError, match="approved"):
                    await Tools(oauth).configure(Choices(model="forged"))
            assert await service.repo.shared_ai() == before
            assert (
                await service.ai_connection(await service.connection(connection.user_id))
            ).model == "approved-model"
            assert (await service.repo.ai_preferences(owner(world, "listener-b"))).model == ""

        world.server.call(check)
    finally:
        admin.close()
        a.close()


def test_ordinary_import_publication_native_ownership_and_preparation(world: DeliveryWorld) -> None:
    a, b = signed(world, "listener-a"), signed(world, "listener-b")
    service, prep = world.services.playlists, world.services.downloads
    assert service and prep
    try:
        posted = a.submit(
            "playlists/import",
            {
                "name": "Personal ordered import",
                "lines": "Glass Orchard - First Light\nGlass Orchard - Harbor",
            },
        )
        assert posted.status_code == 303, posted.text
        mix_id = posted.headers["location"].rsplit("/", 1)[-1]
        run = world.server.call(lambda: service.repo.runs(mix_id))[0]
        assert wait_state(world, run["id"], ("preview", "failed"))["state"] == "preview"
        assert "Personal ordered import" not in b.get("playlists/import").text
        assert b.get(f"playlists/import/{mix_id}").status_code in (200, 404)
        assert "Personal ordered import" not in b.get(f"playlists/import/{mix_id}").text
        assert a.submit(f"playlists/import/{mix_id}", {"action": "publish"}).status_code == 303
        result = wait_state(world, run["id"], ("published", "failed", "uncertain"))
        assert result["state"] == "published", result["error"]

        async def verify() -> tuple[str, str]:
            ca = await service.connection(owner(world, "listener-a"))
            cb = await service.connection(owner(world, "listener-b"))
            mix = await service.repo.mix(mix_id, ca.user_id)
            playlist = await service.client.playlist(ca, mix["playlist_id"])
            assert playlist["owner"] == ca.username
            assert [s["id"] for s in playlist["entry"]] == result["data"]["native_ids"]
            with pytest.raises(PlaylistError):
                await service.client.playlist(cb, playlist["id"])
            return ca.user_id, playlist["id"]

        ident, playlist = world.server.call(verify)
        assert (
            a.post("downloads/prepare", {"csrf": a.csrf(), "playlist": playlist}).status_code == 303
        )
        batches = world.server.call(lambda: prep.repo.batches(ident))
        assert batches
        assert batches[0]["id"] not in b.get("downloads").text
        assert b.post(
            f"downloads/{batches[0]['id']}/action", {"csrf": b.csrf(), "action": "cancel"}
        ).status_code in (200, 303)
        assert world.server.call(lambda: prep.repo.batches(ident))[0]["state"] != "canceled"
    finally:
        a.close()
        b.close()


def test_native_bearer_proof_precedes_service_work_and_ignores_ambient_auth(
    world: DeliveryWorld,
) -> None:
    service = world.services.navidrome
    assert service
    body = httpx.post(
        world.nd.base_url + "/auth/login", json={"username": "listener-a", "password": PASSWORD}
    ).json()
    admin = httpx.post(
        world.nd.base_url + "/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    ).json()

    async def verify() -> None:
        access = AccessService(service)
        principal = await access.verify_native(body["token"])
        assert principal.user_id == body["id"] and not principal.is_admin
        original = service.native_json
        spy = AsyncMock(wraps=original)
        service.native_json = spy
        service.http.cookies.set("jwt", admin["token"])
        service.http.headers["x-nd-authorization"] = "Bearer " + admin["token"]

        def forged(claims: Any) -> str:
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            header, _, signature = body["token"].split(".")
            return header + "." + payload + "." + signature

        try:
            for token in (
                "",
                "a.b.c",
                forged({}),
                forged([]),
                forged({"sub": "listener-a"}),
                forged({"uid": [], "sub": "listener-a"}),
                forged({"uid": admin["id"], "sub": ADMIN_USER}),
                forged({"uid": body["id"], "sub": ADMIN_USER}),
            ):
                with pytest.raises(AccessDenied):
                    await access.verify_native(token)
            playlists = world.services.playlists
            assert playlists
            saved = await playlists.repo.connection(body["id"]) or Connection(
                # (A later test of this module disconnects listener-a; only the shape of a
                # saved connection matters here, its token is replaced by an invalid one.)
                user_id=body["id"],
                username="listener-a",
                token=SecretStr("invented"),
                salt=SecretStr("invented"),
            )
            with pytest.raises(PlaylistError):
                await playlists.client.validate(
                    saved.model_copy(update={"token": SecretStr("invalid-invented-native-token")})
                )
            assert spy.await_count == 0
        finally:
            service.native_json = original
            service.http.cookies.clear()
            del service.http.headers["x-nd-authorization"]

    world.server.call(verify)
    original = service.native_json
    spy = AsyncMock(wraps=original)
    service.native_json = spy
    browser = Browser(world.server.base_url)
    try:
        assert browser.sign_in("listener-a", "wrong-invented-password").status_code == 400
        assert spy.await_count == 0
    finally:
        browser.close()
        service.native_json = original


def test_current_role_grants_and_unknown_access_close_writes(world: DeliveryWorld) -> None:
    manager, b = signed(world, "manager"), signed(world, "listener-b")
    service = world.services.navidrome
    playlists = world.services.playlists
    assert service and playlists and world.app.agents
    user = next(u for u in world.nd.native("GET", "user").json() if u["userName"] == "manager")
    ident = owner(world, "listener-b")
    tokens = connect_agent(b.http, world.server.base_url, "listener-b", PASSWORD, "create status")
    native_login = httpx.post(
        world.nd.base_url + "/auth/login", json={"username": "listener-b", "password": PASSWORD}
    ).json()
    try:
        world.nd.native("PUT", f"user/{user['id']}", json={**user, "isAdmin": False})
        assert manager.get("addons").status_code == 403
        assert manager.get("playlists").status_code == 200
        csrf = b.csrf()
        original = service.native_json
        service.native_json = AsyncMock(side_effect=NavidromeError("not available"))
        try:
            refused = b.post("appearance", {"csrf": csrf, "accent": "teal"})
            assert refused.status_code == 503 and "access can" in refused.text
            assert manager.post("playback", {"csrf": csrf}).status_code == 503
        finally:
            service.native_json = original
        connection = world.server.call(lambda: playlists.connection(ident))
        other = next(
            lib["id"] for lib in world.nd.native("GET", "library").json() if lib["id"] != 1
        )
        world.nd.native("PUT", f"user/{ident}/library", json={"libraryIds": [other]})
        assert b.get("playlists").status_code == 303

        async def denied() -> None:
            current = await playlists.client.access.verify_native(native_login["token"])
            assert not current.can_access(1)
            with pytest.raises(PlaylistError):
                await playlists.client.validate(connection)
            assert await world.app.agents.oauth.verify_token(tokens["access_token"]) is None

        world.server.call(denied)
    finally:
        libraries = [lib["id"] for lib in world.nd.native("GET", "library").json()]
        world.nd.native("PUT", f"user/{ident}/library", json={"libraryIds": libraries})
        world.nd.native("PUT", f"user/{user['id']}", json={**user, "isAdmin": True})
        manager.close()
        b.close()


def test_deleted_recreated_name_inherits_no_session_profile_or_grant(world: DeliveryWorld) -> None:
    old = signed(world, "renewed")
    service = world.services.playlists
    assert service and world.app.agents
    ident = owner(world, "renewed")
    token = old.http.cookies.get("shijhon_session")
    tokens = connect_agent(old.http, world.server.base_url, "renewed", PASSWORD, "create status")
    connection = world.server.call(lambda: service.connection(ident))
    native_login = httpx.post(
        world.nd.base_url + "/auth/login", json={"username": "renewed", "password": PASSWORD}
    ).json()
    assert (
        old.submit(
            "playlists",
            {
                "history_source": "manual",
                "history_user": "old-profile",
                "history_key": "invented-old-key",
            },
            "playlists/connection",
        ).status_code
        == 303
    )
    world.nd.native("DELETE", f"user/{ident}")
    world.nd.create_user("renewed", PASSWORD)
    new = signed(world, "renewed")
    try:
        assert owner(world, "renewed") != ident
        assert "old-profile" not in new.get("playlists").text
        assert old.get("playlists").status_code == 303
        old.http.cookies.set("shijhon_session", token, path="/shijhon")
        assert old.get("downloads").status_code == 303

        async def denied() -> None:
            with pytest.raises(AccessDenied):
                await service.client.access.verify_native(native_login["token"])
            with pytest.raises(PlaylistError):
                await service.client.validate(connection)
            assert await world.app.agents.oauth.verify_token(tokens["access_token"]) is None

        world.server.call(denied)
    finally:
        old.close()
        new.close()


def test_personal_requests_idempotency_status_and_disconnect_are_isolated(
    world: DeliveryWorld,
) -> None:
    a, b = signed(world, "listener-a"), signed(world, "listener-b")
    service = world.services.playlists
    assert service and world.app.agents
    try:
        ta = connect_agent(a.http, world.server.base_url, "listener-a", PASSWORD, "create status")
        tb = connect_agent(b.http, world.server.base_url, "listener-b", PASSWORD, "create status")

        def auth(token: str) -> dict[str, str]:
            return {"Authorization": "Bearer " + token, "Idempotency-Key": "same-personal-key"}

        payload = {
            "name": "Personal native request",
            "count": 1,
            "tracks": [{"artist": "Glass Orchard", "title": "First Light"}],
        }
        aa, bb = auth(ta["access_token"]), auth(tb["access_token"])
        first = a.http.post("/shijhon/api/playlists/requests", headers=aa, json=payload)
        assert first.status_code == 202, first.text
        replay = a.http.post("/shijhon/api/playlists/requests", headers=aa, json=payload)
        assert replay.status_code in (200, 202) and replay.json()["id"] == first.json()["id"]
        second = b.http.post("/shijhon/api/playlists/requests", headers=bb, json=payload)
        assert second.status_code == 202 and second.json()["id"] != first.json()["id"]
        result = wait_state(world, first.json()["run"], ("published", "failed", "uncertain"))
        assert result["state"] == "published", result["error"]
        assert (
            b.http.get(
                "/shijhon/api/playlists/requests/" + first.json()["id"], headers=bb
            ).status_code
            == 404
        )
        assert first.json()["id"] not in b.get("playlists/describe/" + first.json()["id"]).text
        assert a.post("playlists/disconnect", {"csrf": a.csrf()}).status_code == 303

        async def verify() -> None:
            assert await service.repo.connection(owner(world, "listener-a")) is None
            assert await service.repo.connection(owner(world, "listener-b")) is not None
            assert await world.app.agents.oauth.verify_token(ta["access_token"]) is None
            assert await world.app.agents.oauth.verify_token(tb["access_token"]) is not None

        world.server.call(verify)
        assert "Personal native request" in b.get("playlists/describe").text
    finally:
        a.close()
        b.close()


def test_signin_and_shared_host_changes_preserve_legacy_profiles_and_schedules(
    world: DeliveryWorld,
) -> None:
    service = world.services.playlists
    assert service
    first = signed(world, "listener-b")
    first.close()
    owner_id = owner(world, "listener-b")
    due = time.time() + 86400

    async def legacy() -> str:
        connection = await service.connection(owner_id)
        previous = connection.model_copy(
            update={
                "backend": "api",
                "endpoint": "https://legacy-model.invalid/v1",
                "model": "legacy-model",
                "api_key": SecretStr("invented-legacy-key"),
                "history_user": "legacy-profile",
                "history_source": "manual",
                "history_key": SecretStr("invented-legacy-history"),
                "use_shared_ai": False,
            }
        )
        # Reproduce pre-upgrade connection JSON, without the new optional field.
        raw = json.loads(previous.stored())
        raw.pop("use_shared_ai")
        await service.repo.store.execute(
            "UPDATE playlist_owners SET connection=? WHERE id=?", [json.dumps(raw), owner_id]
        )
        mix = await service.repo.create(owner_id, Definition(name="Legacy scheduled mix", ai=True))
        await service.repo.store.execute(
            "UPDATE playlist_mixes SET enabled=1,due=? WHERE id=?", [due, mix]
        )
        return mix

    mix = world.server.call(legacy)
    admin = signed(world, ADMIN_USER)
    b = signed(world, "listener-b")
    try:
        assert (
            admin.submit(
                "playlists",
                {
                    "backend": "api",
                    "endpoint": "https://replacement-host.invalid/v1",
                    "model": "new-host-model",
                    "api_key": "invented-new-host-key",
                    "share_ai": "true",
                },
                "playlists/connection",
            ).status_code
            == 303
        )

        async def check() -> None:
            kept = await service.connection(owner_id)
            assert kept.backend == "api" and kept.endpoint == "https://legacy-model.invalid/v1"
            assert kept.model == "legacy-model" and not kept.use_shared_ai
            assert kept.api_key.get_secret_value() == "invented-legacy-key"
            assert kept.history_user == "legacy-profile"
            row = await service.repo.mix(mix, owner_id)
            assert row["enabled"] == 1 and row["due"] == due

        world.server.call(check)
        page = b.get("playlists").text
        assert "legacy-profile" in page and "legacy-model.invalid" not in page
        assert "invented-legacy-key" not in page
    finally:
        admin.close()
        b.close()
