"""Agent access on the dashboard: the agent address and plain HTTP on a home network as saved
settings (the environment locks them), the Agent access page, the Playlists page's "Your AI
agent" for an administrator and for anyone else, and local and network AI APIs as a saved
setting. Invented hosts and accounts; no Navidrome (the service account is never asked)."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from pydantic import SecretStr

from shijhon.access import Principal
from shijhon.app import ShijhonApp
from shijhon.config import Settings, load_settings
from shijhon.dashboard import sections
from shijhon.dashboard.auth import Session
from shijhon.dashboard.playlists import Pages as PlaylistPages
from shijhon.dashboard.saved import REMOVE, Saved, SavedSettings, effective, encode, locked_keys
from shijhon.playlists.ai import Curator
from shijhon.playlists.model import Connection, PlaylistError
from shijhon.playlists.requests import digest
from shijhon.store import Store
from tests.harness.agents import agent_token

pytestmark = pytest.mark.anyio

HOME = "http://192.168.1.20:4533"
Saved_ = dict[str, dict[str, Saved]]


@pytest.fixture(autouse=True)
def _no_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("SHIJHON_"):
            monkeypatch.delenv(name)


def person(admin: bool, user_id: str = "listener-id") -> Session:
    principal = Principal(user_id, "listener", admin, frozenset({1}))
    return Session("hash", "listener", "csrf", 9_999_999_999, user_id, principal)


async def save(
    configured: Settings,
    saved: Saved_,
    section: str,
    form: Mapping[str, str],
) -> tuple[sections.Submitted, Saved_]:
    """A settings form's save, and the saved values after it (applied as the store does)."""
    locked = locked_keys(configured)
    submitted = await sections.submit(
        section, configured, saved, form, addon_names=[], locked=locked
    )
    if submitted.problems:
        return submitted, saved
    written = sections.writes(section, configured, saved, submitted, locked=locked)
    values = dict(saved.get(section, {}))
    for key, value in written.writes.items():
        if value is REMOVE:
            values.pop(key, None)
        else:
            values[key] = Saved(json.loads(encode(value)), 0.0, "tester")
    return submitted, {**saved, section: values}


def row(configured: Settings, saved: Saved_, section: str, name: str) -> dict[str, Any]:
    groups = sections.rows(
        section, configured, saved, addon_names=[], locked=locked_keys(configured)
    )
    return next(r for _, rows in groups for r in rows if r["name"] == name)


# --- the saved settings ---------------------------------------------------------------


async def test_the_agent_address_is_saved_and_the_environment_locks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = load_settings(None, agents={"public_url": "https://file.example"})
    shown = row(configured, {}, "agents", "public_url")
    assert shown["default"] == "Default https://file.example, from the configuration"
    assert shown["restart"] and "signs connected agents out" in shown["help"]
    submitted, saved = await save(
        configured, {}, "agents", {"public_url": "https://music.example/", "private_http": "false"}
    )
    assert not submitted.problems
    assert saved["agents"]["public_url"].value == "https://music.example"
    applying = effective(configured, saved, locked=locked_keys(configured))[0]
    assert applying.agents.public_url == "https://music.example"
    # Saving the configuration's value removes the saved one.
    _, back = await save(configured, saved, "agents", {"public_url": "https://file.example"})
    assert "public_url" not in back["agents"]

    monkeypatch.setenv("SHIJHON_AGENTS__PUBLIC_URL", "https://env.example")
    monkeypatch.setenv("SHIJHON_AGENTS__PRIVATE_HTTP", "true")
    configured = load_settings(None)
    assert locked_keys(configured)["agents"] == {
        "public_url": "SHIJHON_AGENTS__PUBLIC_URL",
        "private_http": "SHIJHON_AGENTS__PRIVATE_HTTP",
    }
    locked = row(configured, saved, "agents", "public_url")
    assert locked["kind"] == "locked" and locked["hidden"]  # the saved value is kept, unused
    assert locked["text"] == "https://env.example"  # an address is shown as written
    _, after = await save(configured, saved, "agents", {"public_url": "https://other.example"})
    assert after == saved  # never taken from a form
    applying = effective(configured, after, locked=locked_keys(configured))[0]
    assert applying.agents.public_url == "https://env.example" and applying.agents.private_http


async def test_plain_http_needs_the_home_network_switch_and_a_private_host() -> None:
    configured = load_settings(None)
    refused, _ = await save(configured, {}, "agents", {"public_url": HOME, "private_http": "false"})
    assert "Plain HTTP on a home network is off" in refused.problems["public_url"].row
    public, _ = await save(
        configured, {}, "agents", {"public_url": "http://8.8.8.8:4533", "private_http": "true"}
    )
    assert "use HTTPS" in public.problems["public_url"].row
    submitted, saved = await save(
        configured, {}, "agents", {"public_url": HOME, "private_http": "true"}
    )
    assert not submitted.problems
    applying = effective(configured, saved, locked=locked_keys(configured))[0].agents
    assert (applying.public_url, applying.private_http) == (HOME, True)
    note = row(configured, saved, "agents", "private_http")["help"]
    assert "travel unencrypted on that network" in note
    # Switched off again while the address is plain HTTP: refused, the address keeps working.
    off, kept = await save(configured, saved, "agents", {"private_http": "false"})
    assert off.problems and kept == saved


async def test_local_ai_is_a_saved_setting_with_the_owners_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = load_settings(None)
    shown = row(configured, {}, "playlists", "allow_private_api")
    assert shown["help"] == "Lets Shijhon connect to AI on this machine or your network."
    assert not shown["restart"] and shown["kind"] == "switch" and not shown["checked"]
    submitted, saved = await save(configured, {}, "playlists", {"allow_private_api": "true"})
    assert not submitted.problems
    assert effective(configured, saved, locked={})[0].playlists.allow_private_api
    # Only this setting of [playlists] is ever applied from saved values.
    saved["playlists"]["codex_command"] = Saved("/bin/invented", 0.0, "tester")
    assert effective(configured, saved, locked={})[0].playlists.codex_command == ""

    monkeypatch.setenv("SHIJHON_PLAYLISTS__ALLOW_PRIVATE_API", "false")
    configured = load_settings(None)
    assert row(configured, saved, "playlists", "allow_private_api")["kind"] == "locked"
    applying = effective(configured, saved, locked=locked_keys(configured))[0]
    assert not applying.playlists.allow_private_api


@pytest.fixture
def local_ai() -> Iterator[str]:
    """An invented chat completions API on this machine."""

    class Answer(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            body = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def _connection(endpoint: str) -> Connection:
    return Connection(
        user_id="listener-id",
        username="listener",
        token=SecretStr("t"),
        salt=SecretStr("s"),
        backend="api",
        endpoint=endpoint,
        model="invented-model",
    )


async def test_local_ai_switched_on_in_the_dashboard_reaches_a_private_api(
    tmp_path: Path, local_ai: str
) -> None:
    """Off: an API on this machine is refused (over HTTP and over HTTPS alike); saved on, it
    applies at once to the running playlist services, whose curator then reaches it."""
    app = ShijhonApp(_settings(tmp_path))
    await app.startup()
    try:
        services = app.services
        assert services is not None and services.playlists is not None
        curator: Curator = services.playlists.curator
        with pytest.raises(PlaylistError, match="needs HTTPS"):
            await curator.api(_connection(local_ai), "{}")
        with pytest.raises(Exception):  # noqa: B017 (the network policy's refusal)
            await curator.api(_connection(local_ai.replace("http://", "https://")), "{}")

        _, written, applied = await app.dashboard._save(
            person(True), "playlists", {"allow_private_api": "true"}, whole=False
        )
        assert written is not None and applied == ["allow_private_api"]
        assert curator.settings.allow_private_api
        assert await curator.api(_connection(local_ai), "{}") == {"ok": True}
        assert await app.dashboard._pending() == []  # nothing waits for a restart

        _, _, applied = await app.dashboard._save(
            person(True), "playlists", {"allow_private_api": "false"}, whole=False
        )
        assert applied == ["allow_private_api"]
        with pytest.raises(PlaylistError, match="needs HTTPS"):
            await curator.api(_connection(local_ai), "{}")
    finally:
        await app.shutdown()


# --- the pages --------------------------------------------------------------------------


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return load_settings(
        None,
        state_dir=tmp_path / "state",
        navidrome={"url": "http://127.0.0.1:9", "library_path": tmp_path},
        **overrides,
    )


async def _saved_agents(settings: Settings, values: dict[str, Any]) -> None:
    store = await Store.open(settings.database_path)
    await SavedSettings(store).put("agents", values, "tester")
    await store.close()


@asynccontextmanager
async def serving(app: ShijhonApp) -> AsyncIterator[None]:
    """Agent access serving MCP, as in a running Shijhon (a startup outside a lifespan
    spawns no background work)."""
    assert app.agents is not None
    async with anyio.create_task_group() as group:
        group.start_soon(app.agents.run)
        await app.agents.ready.wait()
        yield
        group.cancel_scope.cancel()


async def _playlists_page(app: ShijhonApp, session: Session) -> str:
    response = await PlaylistPages(app.dashboard).render(session)
    assert response.status_code == 200
    return bytes(response.body).decode()


async def test_a_saved_home_network_address_runs_agent_access_over_http(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _saved_agents(settings, {"public_url": HOME, "private_http": True})
    app = ShijhonApp(settings)
    await app.startup()
    try:
        async with serving(app):
            assert app.agents is not None and app.agents.oauth.issuer == HOME + "/shijhon/agents"
            page = bytes(
                (await app.dashboard._section_render(person(True), "agent-access", "agents")).body
            ).decode()
            assert f"On: agents connect to <code>{HOME}/shijhon/mcp</code>" in page
            assert "travel unencrypted on your network" in page
            assert 'name="public_url"' in page and 'name="private_http"' in page

            service = app.services.playlists if app.services else None
            assert service is not None
            await service.repo.connect(_connection(""))  # (an agent's sign-in makes it)
            service.connection = AsyncMock()  # (Navidrome's check of the account)
            await agent_token(service, "listener-id", "Invented agent", history=False, library=True)
            # Another agent, signed in at an address used before: it is signed out now.
            old = await service.requests.mint(
                "listener-id", "Earlier agent", history=False, library=False
            )
            await service.repo.store.execute(
                "INSERT INTO agent_oauth_tokens(id,kind,client,grant_id,resource,scopes,expires)"
                " VALUES (?,'refresh','synthetic-agent',?,?,'[]',?)",
                [
                    digest(old) + "-r",
                    digest(old),
                    "https://old.example/shijhon/mcp",
                    time.time() + 99,
                ],
            )
            del service.connection  # Navidrome is unreachable from here on
            listener = await _playlists_page(app, person(False))
            assert listener.index('id="agents-title"') < listener.index('id="make-title"')
            assert f'<code id="agent-address">{HOME}/shijhon/mcp</code>' in listener
            assert "sign in with a Navidrome account" in listener
            assert "the password and the agent's access travel unencrypted" in listener
            # Plain HTTP on a home network: only agent apps there can connect.
            assert "an agent app on this network" in listener
            assert "Agent apps that run in the cloud can't reach it." in listener
            assert "custom connector" not in listener
            agents = listener[listener.index('aria-label="Connected agents"') :]
            invented = agents[agents.index("Invented agent") :]
            invented = invented[: invented.index("</li>")]
            assert "Can make playlists" in invented and "read your library" in invented
            assert "Connected " in invented and "Signed out" not in invented
            earlier = agents[agents.index("Earlier agent") :]
            earlier = earlier[: earlier.index("</li>")]
            assert "Signed out: connect it again" in earlier and 'value="revoke"' in earlier
            assert "/shijhon/agent-access" not in listener  # an administrator's page
            assert "Administrator · for all accounts" not in listener

            # Revoking needs no Navidrome: the signed-in session is enough, for its own agents.
            grant = digest(old)
            pages = PlaylistPages(app.dashboard)

            def posted(agent: str) -> Any:
                form = {"action": "revoke", "agent": agent}
                return SimpleNamespace(state=SimpleNamespace(form=form))

            other = await pages.agents(posted(grant), person(False, "someone-else"))
            assert other.status_code == 303
            assert "Earlier agent" in await _playlists_page(app, person(False))
            revoked = await pages.agents(posted(grant), person(False))
            assert revoked.headers["location"] == "/shijhon/playlists#agents"
            after = await _playlists_page(app, person(False))
            assert "Earlier agent" not in after and "1 revoked earlier" in after
            assert "Invented agent" in after

            # Serving stopped (it failed): the pages say so, not "On".
            assert app.agents is not None
            app.agents.serving = False
            page = bytes(
                (await app.dashboard._section_render(person(True), "agent-access", "agents")).body
            ).decode()
            assert "Not running: agent access failed to start" in page and "On:" not in page
            listener = await _playlists_page(app, person(False))
            assert "Agent access isn't running right now." in listener
            assert "agent-address" not in listener
    finally:
        await app.shutdown()


async def test_an_https_address_is_for_agent_apps_anywhere(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _saved_agents(settings, {"public_url": "https://music.example.com"})
    app = ShijhonApp(settings)
    await app.startup()
    try:
        async with serving(app):
            listener = await _playlists_page(app, person(False))
            assert "a remote MCP server (a custom connector)" in listener
            assert "in the cloud" not in listener and "unencrypted" not in listener
    finally:
        await app.shutdown()


async def test_agent_access_off_for_an_administrator_and_for_anyone_else(tmp_path: Path) -> None:
    app = ShijhonApp(_settings(tmp_path))
    await app.startup()
    try:
        assert app.agents is None
        admin = await _playlists_page(app, person(True))
        assert "Agent access is off in this installation" in admin
        assert 'href="/shijhon/agent-access">Agent access</a> to turn it on' in admin
        listener = await _playlists_page(app, person(False))
        assert "An administrator can turn it on." in listener
        assert "/shijhon/agent-access" not in listener
        page = bytes(
            (await app.dashboard._section_render(person(True), "agent-access", "agents")).body
        ).decode()
        assert "Off: no agent address is set" in page

        await app.dashboard._save(person(True), "agents", {"public_url": "https://music.example"})
        page = bytes(
            (await app.dashboard._section_render(person(True), "agent-access", "agents")).body
        ).decode()
        assert "after Shijhon restarts" in page and "https://music.example/shijhon/mcp" in page
        assert [w["page"] for w in await app.dashboard._pending()] == ["Agent access"]
    finally:
        await app.shutdown()


async def test_server_ai_is_one_form_with_every_connection(tmp_path: Path) -> None:
    """One form chooses an API or a local runner, and holds the local AI switch; saving
    the switch with an API connection needs no form of its own."""
    app = ShijhonApp(_settings(tmp_path))
    await app.startup()
    try:
        assert app.services is not None and app.services.playlists is not None
        repo = app.services.playlists.repo
        admin = person(True, "admin-id")
        await repo.connect(
            Connection(
                user_id="admin-id",
                username="listener",
                token=SecretStr("t"),
                salt=SecretStr("s"),
            )
        )
        page = await _playlists_page(app, admin)
        form = page[page.index('action="/shijhon/playlists/host-ai"') :]
        form = form[: form.index("</form>")]
        for key in ("none", "api", "codex", "claude"):
            assert f'value="{key}"' in form
        assert "AI on this machine or your network" in form
        assert 'name="allow_private_api"' in form
        assert 'id="additional"' not in page and "/playlists/local-ai" not in page

        pages = PlaylistPages(app.dashboard)
        # (Navidrome's checks of the account and its role.)
        service = app.services.playlists
        service.connection = AsyncMock(return_value=await repo.connection("admin-id"))
        service.client.validate = AsyncMock()
        service.client.principal = AsyncMock(return_value=SimpleNamespace(is_admin=True))

        class Posted:
            state = type(
                "State",
                (),
                {"form": {"backend": "claude", "model": "invented-runner-model"}},
            )

        saved = await pages.host_ai(Posted(), admin)  # type: ignore[arg-type]
        assert saved.status_code == 303, bytes(saved.body).decode()
        shared_ai = await repo.shared_ai()
        assert shared_ai and shared_ai["backend"] == "claude"
        assert shared_ai["model"] == "invented-runner-model"
        page = await _playlists_page(app, admin)
        form = page[page.index('action="/shijhon/playlists/host-ai"') :]
        assert '<option value="claude" selected>' in form
        assert 'value="invented-runner-model"' in form
    finally:
        await app.shutdown()
