"""The permissions that reach beyond playlists - adding to the library and server
administration - at consent and at the tools' own checks, with a stand-in for Navidrome's
answer about the account. (The tools' work is in the acceptance suites
``test_agent_library_additions`` and ``test_agent_admin_tools``.)"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest

from shijhon.access import Principal
from shijhon.agents import admin as admin_module
from shijhon.agents.admin import KEY_SECONDS, KEYS_KEPT, AdminTools
from shijhon.agents.oauth import ELEVATED, SCOPES
from shijhon.dashboard.app import _plain_page
from shijhon.playlists.model import PlaylistError, UpstreamUnavailable
from shijhon.playlists.requests import RequestError
from tests.harness.agents import callback_target
from tests.unit.test_agent_consent import signin
from tests.unit.test_agents import app_at, begin, call, finish

EVERYTHING = " ".join(SCOPES)
ADMIN_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("get_review_list", {}),
    ("settle_review_album", {"album_id": "album", "action": "keep", "idempotency_key": "k" * 8}),
    ("get_server_status", {}),
]
ADD_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("search_catalog", {"query": "anything"}),
    ("add_to_library", {"album_ids": ["sh.al.demo.one"], "idempotency_key": "k" * 8}),
    ("add_to_library", {"album_ids": ["sh.al.demo.one"], "preview": True}),
]


def account(app: Any, *, admin: bool) -> None:
    """What Navidrome says about the account from now on."""
    app.service.client.principal = AsyncMock(
        side_effect=lambda connection: Principal(
            connection.user_id, connection.username, admin, frozenset({1})
        )
    )


def box(page: str, scope: str) -> str | None:
    """The permission's checkbox on the consent page (its tag), or None: not offered."""
    name = f'name="scope_{scope}"'
    return page.split(name, 1)[1].split(">", 1)[0] if name in page else None


def test_the_elevated_permissions_are_the_two_that_reach_beyond_playlists() -> None:
    assert ELEVATED == ("add", "admin") and set(ELEVATED) <= set(SCOPES)


def test_consent_offers_them_cleared_and_administration_to_administrators_only(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            # An agent app that asks for everything there is: an account that is no
            # administrator is offered adding, cleared, and not administration.
            account(app, admin=False)
            flow = await begin(http, EVERYTHING)
            page = (await signin(http, flow)).text
            for scope in SCOPES:
                if scope not in ELEVATED:
                    assert "checked" in (box(page, scope) or ""), scope
            assert box(page, "add") is not None and "checked" not in (box(page, "add") or "")
            assert box(page, "admin") is None and "Navidrome administrator" not in page
            # Sending the field all the same grants nothing.
            forced = await http.post(
                flow["location"],
                data={"csrf": flow["csrf"], "action": "allow", "scope_create": "true",
                      "scope_admin": "true"},
            )  # fmt: skip
            assert forced.status_code == 400
            row = await app.oauth.store.fetchone("SELECT count(*) AS n FROM playlist_agent_tokens")
            assert row["n"] == 0
            # Approved as shown, neither is granted.
            approved = await http.post(
                flow["location"],
                data={"csrf": flow["csrf"], "action": "allow", "scope_create": "true",
                      "scope_status": "true"},
            )  # fmt: skip
            assert "code" in parse_qs(urlsplit(callback_target(approved)).query)
            grant = await app.oauth.store.fetchone("SELECT scopes FROM playlist_agent_tokens")
            assert "add" not in grant["scopes"] and "admin" not in grant["scopes"]

            # An administrator is offered both, cleared; selected, they are granted, and
            # the connection's next consent shows them selected.
            account(app, admin=True)
            flow = await begin(http, EVERYTHING, flow["client_id"])
            page = (await http.get(flow["location"])).text
            page = (await signin(http, flow)).text
            for scope in ELEVATED:
                assert box(page, scope) is not None and "checked" not in (box(page, scope) or "")
            assert "Offered because you are a Navidrome administrator" in page
            tokens = await finish_signed_in(http, flow, "create status add admin")
            assert set(tokens["scope"].split()) == {"create", "status", "add", "admin"}
            flow = await begin(http, "create status", flow["client_id"])
            page = (await signin(http, flow)).text
            for scope in ELEVATED:
                assert "checked" in (box(page, scope) or ""), scope
            # Demoted meanwhile: administration is no longer on the form, adding stays.
            account(app, admin=False)
            page = (await http.get(flow["location"])).text
            assert box(page, "admin") is None and "checked" in (box(page, "add") or "")

    anyio.run(run)


async def finish_signed_in(http: Any, flow: dict[str, Any], scopes: str) -> dict[str, Any]:
    """Approve an already signed-in flow with these permissions and exchange its code."""
    approved = await http.post(
        flow["location"],
        data={
            "csrf": flow["csrf"],
            "action": "allow",
            **{f"scope_{s}": "true" for s in scopes.split()},
        },
    )
    query = parse_qs(urlsplit(callback_target(approved)).query)
    tokens = await http.post(
        "/shijhon/agents/token",
        data={
            "grant_type": "authorization_code",
            "client_id": flow["client_id"],
            "code": query["code"][0],
            "code_verifier": flow["verifier"],
            "redirect_uri": "http://127.0.0.1/callback",
            "resource": flow["params"]["resource"],
        },
    )
    assert tokens.status_code == 200, tokens.text
    return dict(tokens.json())


def test_the_tools_need_their_permission_and_administration_an_administrator_now(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            account(app, admin=True)
            # Every other permission: none of the new tools answers.
            others = " ".join(scope for scope in SCOPES if scope not in ELEVATED)
            narrow = (await finish(http, await begin(http, others), others))["access_token"]
            for name, arguments in ADMIN_TOOLS:
                refused = await call(http, narrow, name, arguments)
                assert refused["error"]["code"] == "permission_required", name
                assert refused["error"]["missing_permissions"] == ["admin"]
            for name, arguments in ADD_TOOLS:
                refused = await call(http, narrow, name, arguments)
                assert refused["error"]["code"] == "permission_required", name
                assert refused["error"]["missing_permissions"] == ["add"]
            # Each of the two alone does not stand for the other.
            adder = (await finish(http, await begin(http, "add"), "add"))["access_token"]
            for name, arguments in ADMIN_TOOLS:
                refused = await call(http, adder, name, arguments)
                assert refused["error"]["missing_permissions"] == ["admin"], name
            manager = (await finish(http, await begin(http, "admin"), "admin"))["access_token"]
            for name, arguments in ADD_TOOLS:
                refused = await call(http, manager, name, arguments)
                assert refused["error"]["missing_permissions"] == ["add"], name

            # With the permission, an administrator gets past the check (this stand-in
            # installation has no review list and no catalog: said as missing setup).
            for name, arguments in ADMIN_TOOLS[:2]:
                answer = await call(http, manager, name, arguments)
                assert answer["error"]["code"] == "setup_required", (name, answer)
            for name, arguments in ADD_TOOLS:
                answer = await call(http, adder, name, arguments)
                assert answer["error"]["code"] == "setup_required", (name, answer)
            feature = (await call(http, manager, "get_capabilities"))["features"]
            assert feature["server_administration"]["administrator"] is True
            assert feature["server_administration"]["ready"] is True
            assert feature["library_additions"]["permitted"] is False

            # Demoted in Navidrome, with the same token and grant: refused at the call.
            account(app, admin=False)
            for name, arguments in ADMIN_TOOLS:
                refused = await call(http, manager, name, arguments)
                assert refused["ok"] is False, name
                assert refused["error"]["code"] == "administrator_required", (name, refused)
                assert "missing_permissions" not in refused["error"]
            feature = (await call(http, manager, "get_capabilities"))["features"]
            assert feature["server_administration"]["permitted"] is True
            assert feature["server_administration"]["administrator"] is False
            assert feature["server_administration"]["ready"] is False
            # An account Navidrome no longer knows, or whose library access ended: none.
            app.service.client.principal = AsyncMock(side_effect=PlaylistError("gone"))
            assert await app.oauth.administrator("owner") is False
            # Navidrome not answering is no answer: never taken for an administrator.
            app.service.client.principal = AsyncMock(side_effect=UpstreamUnavailable("away"))
            with pytest.raises(UpstreamUnavailable):
                await app.oauth.administrator("owner")
            for name, arguments in ADMIN_TOOLS:
                away = await call(http, manager, name, arguments)
                assert away["error"]["code"] == "temporarily_unavailable", name
            # Promoted again: the same token works again.
            account(app, admin=True)
            answer = await call(http, manager, "get_review_list")
            assert answer["error"]["code"] == "setup_required"
            assert await app.oauth.administrator("nobody") is False

    anyio.run(run)


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://addon.invalid/sign-in", True),
        ("http://addon.invalid:8080/pages/sign-in/", True),
        ("https://addon.invalid/sign-in?key=value", False),
        ("https://addon.invalid/sign-in?", False),
        ("https://addon.invalid/sign-in#part", False),
        ("https://addon.invalid/sign-in#", False),
        ("https://user@addon.invalid/sign-in", False),
        ("https://user:word@addon.invalid/sign-in", False),
        ("ftp://addon.invalid/sign-in", False),
        ("javascript:alert(1)", False),
        ("//addon.invalid/sign-in", False),
        ("https:///sign-in", False),
        ("https://[addon/sign-in", False),
        ("", False),
        (None, False),
        ("https://addon.invalid/" + "a" * 2048, False),
    ],
)
def test_an_add_ons_page_is_named_only_as_a_plain_page_address(url: Any, kept: bool) -> None:
    assert _plain_page(url) == (url if kept else None)


def test_a_grant_authorizes_work_only_while_its_oauth_client_is_registered(tmp_path: Path) -> None:
    """What goes on after an agent's call - an addition, a fill, a playlist's run - is
    authorized by its grant. A grant that came from an OAuth client stands only while
    that client is registered, as its tokens do; one that did not keeps its treatment."""

    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            account(app, admin=True)
            requests, store = app.service.requests, app.oauth.store
            tokens = await finish(http, await begin(http, "add admin library"), "add admin library")
            grant = (await store.fetchone("SELECT id FROM playlist_agent_tokens"))["id"]
            for scope in ("add", "admin"):
                connection, row = await requests.grant(grant, scope)
                assert connection.user_id == "owner" and row["oauth_client"] == tokens["client_id"]
            assert await requests.grants_library(grant) is True
            # A grant that is not OAuth-backed: as before.
            await requests.mint("owner", "A key", history=False, library=True)
            plain = await store.fetchone("SELECT id FROM playlist_agent_tokens WHERE name='A key'")
            assert (await requests.grant(plain["id"], "create"))[0].user_id == "owner"

            # The client goes while Navidrome is being asked: refused after that wait too.
            async def removed_meanwhile(connection: Any) -> None:
                await store.execute("DELETE FROM agent_oauth_clients")

            app.service.client.validate = AsyncMock(side_effect=removed_meanwhile)
            with pytest.raises(RequestError) as refused:
                await requests.grant(grant, "add")
            assert refused.value.status == 401
            app.service.client.validate = AsyncMock()
            # Gone: the token is refused, and so is the grant, for every permission.
            assert await app.oauth.verify_token(tokens["access_token"]) is None
            for scope in ("add", "admin", "library"):
                with pytest.raises(RequestError) as refused:
                    await requests.grant(grant, scope)
                assert refused.value.status == 401, scope
            assert app.service.client.validate.await_count == 0  # (refused before Navidrome)
            assert await requests.grants_library(grant) is False
            assert (await requests.grant(plain["id"], "create"))[0].user_id == "owner"
            assert await requests.grants_library(plain["id"]) is True

    anyio.run(run)


def test_review_action_keys_are_taken_at_once_and_kept_within_their_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        tools = AdminTools(None, None)  # type: ignore[arg-type]
        now = [1_000_000.0]
        monkeypatch.setattr(admin_module.time, "time", lambda: now[0])
        entry, first = tools._reserve("owner", "key-one-1", "input", "album", "keep")
        assert first and not entry.done.is_set()
        # Taken before the action starts: the same input joins, other input is refused,
        # another account's same key is its own.
        assert tools._reserve("owner", "key-one-1", "input", "album", "keep") == (entry, False)
        with pytest.raises(RequestError) as conflict:
            tools._reserve("owner", "key-one-1", "other", "album", "keep")
        assert conflict.value.status == 409
        assert tools._reserve("other", "key-one-1", "other", "album", "keep")[1] is True
        # An action under way is never forgotten, however old; a finished one after an hour.
        now[0] += KEY_SECONDS + 1
        assert tools._reserve("owner", "key-one-1", "input", "album", "keep") == (entry, False)
        entry.done.set()
        assert tools._reserve("owner", "key-one-1", "input", "album", "keep")[0] is not entry
        # The newest keys are kept: a new one takes the oldest finished one's place.
        tools._asked.clear()
        for number in range(KEYS_KEPT):
            kept, _ = tools._reserve("owner", f"key-{number:05d}", "input", "album", "keep")
            if number:  # the oldest is still under way
                kept.done.set()
        assert tools._reserve("owner", "key-newest", "input", "album", "keep")[1] is True
        assert len(tools._asked) == KEYS_KEPT
        assert ("owner", "key-00000") in tools._asked and ("owner", "key-00001") not in tools._asked
        # All of them under way: no room is made by forgetting one.
        for kept in tools._asked.values():
            kept.done = anyio.Event()
        with pytest.raises(RequestError):
            tools._reserve("owner", "key-too-many", "input", "album", "keep")
        assert len(tools._asked) == KEYS_KEPT

    anyio.run(run)
