"""Synthetic OAuth credentials for unit tests, and a real browser authorization flow."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.shared.auth import OAuthClientInformationFull

from shijhon.agents.oauth import OAuth
from shijhon.playlists.requests import digest
from shijhon.playlists.service import Playlists


def callback_target(response: httpx.Response) -> str:
    """Follow the browser handoff without visiting a test client's callback server."""
    assert response.status_code == 200, response.text
    link = re.search(r'id="agent-return" href="([^"]+)"', response.text)
    assert link, response.text
    assert f'content="0;url={link[1]}"' in response.text
    return unescape(link[1])


async def agent_token(service: Playlists, owner: str, name: str, **grants: Any) -> str:
    """An access-token fixture; real PKCE/consent are covered separately."""
    if service.oauth is None:
        service.oauth = OAuth(service, "https://test.invalid")
    oauth = service.oauth
    client_id = "synthetic-agent"
    if not await oauth.get_client(client_id):
        await oauth.register_client(
            OAuthClientInformationFull(
                client_id=client_id,
                redirect_uris=["http://127.0.0.1/callback"],
                token_endpoint_auth_method="none",
            )
        )
    raw = await service.requests.mint(owner, name, **grants)
    grant_id = digest(raw)
    grant = await service.repo.store.fetchone(
        "SELECT scopes FROM playlist_agent_tokens WHERE id=?", [grant_id]
    )
    assert grant
    await service.repo.store.execute(
        "INSERT INTO agent_oauth_tokens(id,kind,client,grant_id,resource,scopes,expires)"
        " VALUES (?,'access',?,?,?,?,?)",
        [grant_id, client_id, grant_id, oauth.resource, grant["scopes"], time.time() + 3600],
    )
    return raw


def connect_agent(
    http: httpx.Client,
    base_url: str,
    username: str,
    password: str,
    scopes: str = "create status",
    *,
    client_name: str = "Test agent",
) -> dict[str, Any]:
    """Register, authorize, sign in, consent, and exchange a real S256 code."""
    issuer = base_url + "/shijhon/agents"
    resource = base_url + "/shijhon/mcp"
    registered = http.post(
        issuer + "/register",
        json={
            "client_name": client_name,
            "redirect_uris": ["http://127.0.0.1/callback"],
            "token_endpoint_auth_method": "none",
            "scope": scopes,
        },
    )
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    authorized = http.get(
        issuer + "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1/callback",
            "scope": scopes,
            "state": "synthetic-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        },
    )
    assert authorized.status_code == 302, authorized.text
    location = authorized.headers["location"]
    page = http.get(location)
    assert page.status_code == 200, page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf
    approved = http.post(
        location,
        data={
            "username": username,
            "password": password,
            "csrf": csrf[1],
            "action": "allow",
            **{"scope_" + scope: "true" for scope in scopes.split()},
        },
    )
    assert approved.status_code == 200 and 'class="agent-permissions"' in approved.text
    approved = http.post(
        location,
        data={"csrf": csrf[1], "action": "allow", **{"scope_" + s: "true" for s in scopes.split()}},
    )
    query = parse_qs(urlsplit(callback_target(approved)).query)
    assert query["state"] == ["synthetic-state"] and query["iss"] == [issuer]
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": query["code"][0],
        "redirect_uri": "http://127.0.0.1/callback",
        "code_verifier": verifier,
        "resource": resource,
    }
    reply = http.post(issuer + "/token", data=data)
    assert reply.status_code == 200, reply.text
    return {**reply.json(), "client_id": client_id, "exchange": data}
