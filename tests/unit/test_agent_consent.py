"""The second consent step stays bound to its browser, account and expiry."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest

from shijhon.agents.app import COOKIE
from tests.harness.agents import callback_target
from tests.unit.test_agents import BASE, app_at, begin


async def signin(http, flow):
    return await http.post(
        flow["location"],
        data={
            "csrf": flow["csrf"],
            "action": "allow",
            "username": "owner",
            "password": "synthetic-password",
        },
    )


async def no_approval(app):
    for query in (
        "SELECT count(*) AS n FROM agent_oauth_codes",
        "SELECT count(*) AS n FROM playlist_agent_tokens",
    ):
        row = await app.oauth.store.fetchone(query)
        assert row["n"] == 0


@pytest.mark.parametrize("refusal", ["another_browser", "missing_csrf", "cross_origin"])
def test_passwordless_consent_still_requires_browser_csrf_and_origin(tmp_path: Path, refusal):
    async def run():
        async with app_at(tmp_path / "db") as (app, http):
            flow = await begin(http)
            response = await signin(http, flow)
            assert response.status_code == 200
            assert "Signed in as <strong>owner</strong>." in response.text
            assert 'name="password"' not in response.text
            form = {"csrf": flow["csrf"], "action": "allow", "scope_status": "true"}
            if refusal == "another_browser":
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url=BASE
                ) as other:
                    await begin(other)
                    # Even a valid cookie and matching form token from another flow
                    # cannot approve this signed-in account's flow.
                    form["csrf"] = other.cookies[COOKIE]
                    response = await other.post(flow["location"], data=form)
            elif refusal == "missing_csrf":
                form.pop("csrf")
                response = await http.post(flow["location"], data=form)
            else:
                response = await http.post(
                    flow["location"], data=form, headers={"origin": "https://else.invalid"}
                )
            assert response.status_code == 403
            await no_approval(app)
            assert app.service.client.login.await_count == 1
            accepted = await http.post(
                flow["location"],
                data={"csrf": flow["csrf"], "action": "allow", "scope_status": "true"},
            )
            assert "code" in parse_qs(urlsplit(callback_target(accepted)).query)

    anyio.run(run)


@pytest.mark.parametrize("action", ["deny", "expire"])
def test_cancel_and_expiry_between_signin_and_consent(tmp_path: Path, action):
    async def run():
        async with app_at(tmp_path / "db") as (app, http):
            flow = await begin(http)
            assert (await signin(http, flow)).status_code == 200
            if action == "expire":
                await app.oauth.store.execute("UPDATE agent_oauth_flows SET expires=1")
            response = await http.post(
                flow["location"],
                data={
                    "csrf": flow["csrf"],
                    "action": "deny" if action == "deny" else "allow",
                    "scope_status": "true",
                },
            )
            if action == "deny":
                assert response.status_code == 200
                query = parse_qs(urlsplit(callback_target(response)).query)
                assert query["error"] == ["access_denied"]
                assert query["state"] == ["state-123"]
                assert (await http.get(flow["location"])).status_code == 400
            else:
                assert response.status_code == 400
                assert "This sign-in expired." in response.text
            await no_approval(app)
            assert app.service.client.login.await_count == 1

    anyio.run(run)


@pytest.mark.parametrize("racing", [False, True])
def test_signin_cannot_replace_an_already_claimed_flow(tmp_path: Path, racing):
    async def run():
        async with app_at(tmp_path / "db") as (app, http):
            flow = await begin(http)
            if racing:
                login = app.service.client.login.return_value

                async def claimed_during_login(*args):
                    await app.oauth.store.execute("UPDATE agent_oauth_flows SET owner='another'")
                    return login

                app.service.client.login.side_effect = claimed_during_login
            else:
                assert (await signin(http, flow)).status_code == 200
            response = await signin(http, flow)
            assert response.status_code == 409
            assert "This sign-in is already complete." in response.text
            row = await app.oauth.store.fetchone("SELECT owner FROM agent_oauth_flows")
            assert row["owner"] == ("another" if racing else "owner")
            assert app.service.client.login.await_count == 1
            await no_approval(app)

    anyio.run(run)
