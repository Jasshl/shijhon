"""Plain HTTP for the agent address on the owner's network (``agents.private_http``): any
host name, and of addresses those of a private network (owner, 2026-11-07); the whole
sign-in and an MCP call over such an address. Invented hosts."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from shijhon.config import AgentSettings, private_host
from tests.unit.test_agents import app_at, begin, call, finish

HOME = "http://192.168.1.20:4533"


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:4533",
        "http://10.0.0.7",
        "http://172.16.4.2:8080",
        "http://172.31.255.1",
        "http://100.64.0.1",  # carrier-grade NAT, Tailscale
        "http://100.101.102.103:4533",
        "http://[fd12:3456::1]:4533",  # unique local
        "http://musicbox:4533",  # one label
        "http://musicbox.local",
        "http://musicbox.lan:4533",
        "http://musicbox.home.arpa",
        "http://musicbox.internal",
        "http://MusicBox.Local.",
        "http://music.example.com",  # any name: the owner's network resolves it
        "http://musicbox.localhost.example:4533",
        "http://127.0.0.2:4533",  # loopback beyond the three that need no setting
    ],
)
def test_private_http_accepts_home_network_hosts(url: str) -> None:
    assert AgentSettings(public_url=url, private_http=True).public_url.startswith("http://")
    with pytest.raises(ValidationError, match="Plain HTTP on a home network is off"):
        AgentSettings(public_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8",
        "http://172.32.0.1",
        "http://100.128.0.1",
        "http://192.169.0.1",
        "http://[2001:db8::1]",
        "http://169.254.10.1",  # link-local: not a home network's address
        "http://[fe80::1]",
        "http://1234",  # an address in disguise (0.0.4.210)
        "http://0x8.0x8.0x8.0x8",
    ],
)
def test_public_hosts_over_http_stay_refused(url: str) -> None:
    for allowed in (False, True):
        with pytest.raises(ValidationError, match="use HTTPS"):
            AgentSettings(public_url=url, private_http=allowed)


@pytest.mark.parametrize(
    "url", ["http://localhost:4533", "http://127.0.0.1:4533", "http://[::1]:4533"]
)
def test_loopback_http_needs_no_setting(url: str) -> None:
    assert AgentSettings(public_url=url).public_url == url


def test_https_is_unchanged_by_the_setting() -> None:
    for allowed in (False, True):
        settings = AgentSettings(public_url="https://music.example.com/", private_http=allowed)
        assert settings.public_url == "https://music.example.com"


def test_private_host_names() -> None:
    assert private_host("192.168.0.1") and private_host("box") and private_host("box.example")
    assert private_host("fd00::1") and private_host("127.0.0.5")
    assert not private_host("") and not private_host("1.1.1.1") and not private_host("fe80::1")


def test_sign_in_and_an_mcp_call_over_a_private_http_address(tmp_path: Path) -> None:
    """Discovery, registration, the browser sign-in (its cookie must come back over HTTP),
    the token exchange and a tool call, all at an http:// address on the home network."""

    async def run() -> None:
        async with app_at(tmp_path / "db", base=HOME) as (_, http):
            issuer = HOME + "/shijhon/agents"
            resource = await http.get("/.well-known/oauth-protected-resource/shijhon/mcp")
            assert resource.json()["resource"] == HOME + "/shijhon/mcp"
            assert resource.json()["authorization_servers"] == [issuer]
            for path in (
                "/.well-known/oauth-authorization-server/shijhon/agents",
                "/shijhon/agents/.well-known/oauth-authorization-server",
            ):
                metadata = (await http.get(path)).json()
                assert metadata["issuer"] == issuer
                assert metadata["authorization_endpoint"] == issuer + "/authorize"
                assert metadata["token_endpoint"] == issuer + "/token"
                assert metadata["registration_endpoint"] == issuer + "/register"
            denied = await http.post("/shijhon/mcp", json={})
            assert denied.status_code == 401
            assert (
                HOME + "/.well-known/oauth-protected-resource"
                in (denied.headers["www-authenticate"])
            )

            flow = await begin(http, base=HOME)
            page = await http.get(flow["location"])
            cookie = page.headers["set-cookie"].lower()
            assert "httponly" in cookie and "secure" not in cookie
            assert f'action="{issuer}/connect?' in page.text
            tokens = await finish(http, flow, "create status library", base=HOME)
            capabilities = await call(http, tokens["access_token"], "get_capabilities")
            assert capabilities["features"]["library"]["ready"]

            # Another host name for the same machine is not this installation.
            elsewhere = await http.post(
                "/shijhon/mcp",
                headers={"host": "192.168.1.21:4533"},
                json={},
            )
            assert elsewhere.status_code == 400

    anyio.run(run)
