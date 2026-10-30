"""All agent browser pages render the required attribution with strict context."""

from html import escape
from pathlib import Path

import anyio
from jinja2 import StrictUndefined

from shijhon.dashboard.context import legal_context
from tests.unit.test_agents import BASE, app_at, begin, finish


def test_agent_signin_consent_error_and_return_include_legal_footer(tmp_path: Path) -> None:
    async def run() -> None:
        async with app_at(tmp_path / "db") as (app, http):
            app.host.dashboard.env.undefined = StrictUndefined
            flow = await begin(http)
            signin = await http.get(flow["location"])
            tokens = await finish(http, flow, "create status library")
            error = await http.get(BASE + "/shijhon/agents/connect?flow=expired")
            assert error.status_code == 400
            returned = app.return_to_agent("http://127.0.0.1/callback")
            for html in (signin.text, flow["consent_page"], error.text, returned.body.decode()):
                assert '<footer class="signin-footer">' in html
                assert '<p class="legal">Shijhon <code>' in html
                for value in legal_context().values():
                    assert escape(value) in html
                assert ">Source</a>" in html
            assert tokens["access_token"]

    anyio.run(run)
