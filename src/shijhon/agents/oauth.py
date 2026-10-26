"""Durable OAuth storage behind the official SDK's authorization endpoints.

The SDK validates clients, redirects and S256 PKCE. This provider binds every
authorization to this installation and a freshly checked native owner. Only
credential hashes are retained for codes, browser flows and bearer tokens.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from shijhon.playlists.model import PlaylistError
from shijhon.playlists.requests import RequestError, digest
from shijhon.playlists.service import Playlists

SCOPES = {
    "create": "Create playlists and manage refreshes",
    "status": "Check playlist jobs and results",
    "library": "Read your music library and playlists",
    "history": "Read your listening history",
    "settings": "Choose your playlist AI model from administrator-approved models",
    "downloads": "Prepare playlist audio on the server",
}
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
FLOW_SECONDS = 600
CODE_SECONDS = 120


class Code(AuthorizationCode):
    grant_id: str


class Refresh(RefreshToken):
    grant_id: str


class OAuth(OAuthAuthorizationServerProvider[Code, Refresh, AccessToken]):
    def __init__(self, service: Playlists, public_url: str) -> None:
        self.service, self.store = service, service.repo.store
        self.base = public_url.rstrip("/")
        self.issuer = self.base + "/shijhon/agents"
        self.resource = self.base + "/shijhon/mcp"
        self.registration_lock = anyio.Lock()

    async def clean(self) -> None:
        now = time.time()
        await self.store.execute("DELETE FROM agent_oauth_flows WHERE expires<=?", [now])
        await self.store.execute("DELETE FROM agent_oauth_codes WHERE expires<=?", [now])
        await self.store.execute("DELETE FROM agent_oauth_tokens WHERE expires<=?", [now])

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self.store.fetchone(
            "SELECT data FROM agent_oauth_clients WHERE id=?", [client_id]
        )
        if not row:
            return None
        client = OAuthClientInformationFull.model_validate_json(row["data"])
        # Older registrations can request newly supported optional permissions.
        # This is possible scope metadata only; browser consent alone updates grants.
        client.scope = " ".join(SCOPES)
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self.registration_lock:
            await self._register_client(client_info)

    async def _register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self.clean()
        count = await self.store.fetchone("SELECT count(*) AS n FROM agent_oauth_clients")
        if count and count["n"] >= 512:
            # Anonymous registrations must not permanently lock out real connections.
            # Keep clients with live credentials or authorization in progress.
            await self.store.execute(
                "DELETE FROM agent_oauth_clients WHERE id IN ("
                " SELECT c.id FROM agent_oauth_clients c"
                " WHERE NOT EXISTS (SELECT 1 FROM agent_oauth_tokens t WHERE t.client=c.id)"
                " AND NOT EXISTS (SELECT 1 FROM playlist_agent_tokens g"
                " WHERE g.oauth_client=c.id AND g.revoked=0)"
                " AND NOT EXISTS (SELECT 1 FROM agent_oauth_flows f WHERE f.client=c.id)"
                " AND NOT EXISTS (SELECT 1 FROM agent_oauth_codes k"
                " WHERE json_extract(k.data,'$.client_id')=c.id)"
                " ORDER BY c.created LIMIT 1)"
            )
            count = await self.store.fetchone("SELECT count(*) AS n FROM agent_oauth_clients")
            if count and count["n"] >= 512:
                raise RegistrationError(
                    "invalid_client_metadata", "Too many active registrations. Try again later."
                )
        if not client_info.redirect_uris or len(client_info.redirect_uris) > 10:
            raise RegistrationError("invalid_redirect_uri", "Register 1 to 10 callback URLs.")
        for uri in client_info.redirect_uris:
            url = urlsplit(str(uri))
            if (
                not url.hostname
                or url.username
                or url.password
                or url.fragment
                or url.scheme not in ("https", "http")
                or (url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"))
            ):
                raise RegistrationError("invalid_redirect_uri", "Use HTTPS or a loopback callback.")
        if len(client_info.model_dump_json()) > 16_000:
            raise RegistrationError("invalid_client_metadata", "Client metadata is too large.")
        # Registration declares possible scopes, not granted permissions. Keep all
        # supported scopes available for a later explicit step-up consent flow.
        client_info.scope = " ".join(SCOPES)
        await self.store.execute(
            "INSERT INTO agent_oauth_clients(id,data,created) VALUES (?,?,?)",
            [client_info.client_id, client_info.model_dump_json(), time.time()],
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self.resource:
            raise AuthorizeError("invalid_target", "Request this installation's MCP resource.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "An S256 PKCE challenge is required.")
        scopes = params.scopes if params.scopes is not None else ["create", "status"]
        if not scopes or not set(scopes) <= SCOPES.keys():
            raise AuthorizeError("invalid_scope", "Choose supported permissions.")
        params = params.model_copy(update={"scopes": list(dict.fromkeys(scopes))})
        await self.clean()
        count = await self.store.fetchone("SELECT count(*) AS n FROM agent_oauth_flows")
        if count and count["n"] >= 512:
            raise AuthorizeError("temporarily_unavailable", "Try signing in later.")
        flow = secrets.token_urlsafe(32)
        await self.store.execute(
            "INSERT INTO agent_oauth_flows(id,client,params,expires) VALUES (?,?,?,?)",
            [digest(flow), client.client_id, params.model_dump_json(), time.time() + FLOW_SECONDS],
        )
        return self.issuer + "/connect?" + urlencode({"flow": flow})

    async def flow(self, raw: str) -> dict[str, Any]:
        row = await self.store.fetchone("SELECT * FROM agent_oauth_flows WHERE id=?", [digest(raw)])
        if not row or row["expires"] <= time.time():
            raise RequestError("This sign-in expired. Connect your agent again.", 400)
        return dict(row)

    async def approve(self, raw: str, owner: str, scopes: list[str]) -> str:
        flow = await self.flow(raw)
        params = AuthorizationParams.model_validate_json(flow["params"])
        if params.resource != self.resource:
            raise RequestError("This installation's address changed. Connect your agent again.")
        if not scopes or not set(scopes) <= set(params.scopes or []):
            raise RequestError("Choose only the permissions this connection requested.")
        await self.service.connection(owner)
        client = await self.get_client(flow["client"])
        if not client:
            raise RequestError("This agent registration is unavailable.")
        # The grant ID survives token rotation. It is never a credential given to a client.
        existing = await self.store.fetchone(
            "SELECT id FROM playlist_agent_tokens"
            " WHERE owner=? AND library=? AND oauth_client=? AND revoked=0",
            [owner, self.service.requests.library, flow["client"]],
        )
        if existing:
            grant_id = existing["id"]
        else:
            handle = await self.service.requests.mint(
                owner,
                (client.client_name or "Connected agent")[:100],
                history="history" in scopes,
                library="library" in scopes,
            )
            grant_id = digest(handle)
        raw_code = secrets.token_urlsafe(32)
        code = Code(
            code=digest(raw_code),
            scopes=scopes,
            expires_at=time.time() + CODE_SECONDS,
            client_id=flow["client"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject=owner,
            grant_id=grant_id,
        )
        async with self.store.transaction() as db:
            removed = await db.execute(
                "DELETE FROM agent_oauth_flows WHERE id=? AND expires>?", [flow["id"], time.time()]
            )
            if removed.rowcount != 1:
                raise RequestError("This sign-in has already ended.")
            await db.execute(
                "UPDATE playlist_agent_tokens SET scopes=?,oauth_client=? WHERE id=?",
                [json.dumps(scopes), flow["client"], grant_id],
            )
            await db.execute(
                "INSERT INTO agent_oauth_codes(id,data,grant_id,expires) VALUES (?,?,?,?)",
                [code.code, code.model_dump_json(), grant_id, code.expires_at],
            )
        query = {"code": raw_code, "iss": self.issuer}
        if params.state is not None:
            query["state"] = params.state
        uri = str(params.redirect_uri)
        return uri + ("&" if "?" in uri else "?") + urlencode(query)

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> Code | None:
        row = await self.store.fetchone(
            "SELECT data FROM agent_oauth_codes WHERE id=? AND expires>?",
            [digest(authorization_code), time.time()],
        )
        code = Code.model_validate_json(row["data"]) if row else None
        return (
            code
            if (code and code.client_id == client.client_id and code.resource == self.resource)
            else None
        )

    async def _issue(
        self,
        client_id: str,
        grant_id: str,
        scopes: list[str],
        *,
        consume: tuple[str, str],
    ) -> OAuthToken:
        # Fresh native validation and revocation checks precede each credential exchange.
        try:
            grant = await self.store.fetchone(
                "SELECT * FROM playlist_agent_tokens WHERE id=?", [grant_id]
            )
            if not grant or grant["revoked"] or grant["library"] != self.service.requests.library:
                raise PlaylistError("Connection unavailable.")
            await self.service.connection(grant["owner"])
        except PlaylistError:
            raise TokenError("invalid_grant", "Reconnect this agent.") from None
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        now = time.time()
        error = ""
        async with self.store.transaction() as db:
            fresh = await (
                await db.execute(
                    "SELECT revoked,scopes FROM playlist_agent_tokens WHERE id=?", [grant_id]
                )
            ).fetchone()
            if not fresh or fresh["revoked"] or not set(scopes) <= set(json.loads(fresh["scopes"])):
                error = "Reconnect this agent."
            else:
                kind, ident = consume
                if kind == "code":
                    result = await db.execute(
                        "DELETE FROM agent_oauth_codes WHERE id=? AND expires>?", [ident, now]
                    )
                else:
                    result = await db.execute(
                        "UPDATE agent_oauth_tokens SET used=? WHERE id=? AND used=0 AND expires>?",
                        [now, ident, now],
                    )
                if result.rowcount != 1:
                    error = "This credential has already been used."
                    if kind == "refresh":
                        await db.execute(
                            "UPDATE playlist_agent_tokens SET revoked=? WHERE id=?", [now, grant_id]
                        )
            if not error:
                await db.execute(
                    "DELETE FROM agent_oauth_tokens WHERE grant_id=? AND kind='access'", [grant_id]
                )
                for kind, raw, seconds in (
                    ("access", access, ACCESS_SECONDS),
                    ("refresh", refresh, REFRESH_SECONDS),
                ):
                    await db.execute(
                        "INSERT INTO agent_oauth_tokens"
                        "(id,kind,client,grant_id,resource,scopes,expires) VALUES (?,?,?,?,?,?,?)",
                        [
                            digest(raw),
                            kind,
                            client_id,
                            grant_id,
                            self.resource,
                            json.dumps(scopes),
                            now + seconds,
                        ],
                    )
        # SDK exceptions are frozen dataclasses. Raise outside the async-generator
        # transaction context, and keep replay revocation committed.
        if error:
            raise TokenError("invalid_grant", error)
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=ACCESS_SECONDS,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: Code,
    ) -> OAuthToken:
        if authorization_code.resource != self.resource:
            raise TokenError("invalid_target", "Connect to this installation's current address.")
        return await self._issue(
            client.client_id,
            authorization_code.grant_id,
            authorization_code.scopes,
            consume=("code", authorization_code.code),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> Refresh | None:
        ident = digest(refresh_token)
        row = await self.store.fetchone(
            "SELECT * FROM agent_oauth_tokens WHERE id=? AND kind='refresh' AND client=?",
            [ident, client.client_id],
        )
        if not row or row["expires"] <= time.time() or row["resource"] != self.resource:
            return None
        if row["used"]:
            await self.store.execute(
                "UPDATE playlist_agent_tokens SET revoked=? WHERE id=?",
                [time.time(), row["grant_id"]],
            )
            return None
        return Refresh(
            token=ident,
            client_id=client.client_id,
            scopes=json.loads(row["scopes"]),
            expires_at=int(row["expires"]),
            resource=self.resource,
            grant_id=row["grant_id"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: Refresh,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "Refresh cannot add permissions.")
        return await self._issue(
            client.client_id,
            refresh_token.grant_id,
            scopes,
            consume=("refresh", refresh_token.token),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await self.verify_token(token)

    async def verify_token(self, token: str) -> AccessToken | None:
        row = await self.store.fetchone(
            "SELECT * FROM agent_oauth_tokens"
            " WHERE id=? AND kind='access' AND used=0 AND expires>?",
            [digest(token), time.time()],
        )
        if not row or row["resource"] != self.resource:
            return None
        try:
            grant = await self.store.fetchone(
                "SELECT * FROM playlist_agent_tokens WHERE id=?", [row["grant_id"]]
            )
            if not grant or grant["revoked"] or grant["library"] != self.service.requests.library:
                return None
            connection = await self.service.connection(grant["owner"])
            fresh = await self.store.fetchone(
                "SELECT * FROM playlist_agent_tokens WHERE id=?", [row["grant_id"]]
            )
            current = await self.store.fetchone(
                "SELECT * FROM agent_oauth_tokens WHERE id=? AND expires>?",
                [row["id"], time.time()],
            )
            if (
                not fresh
                or dict(fresh) != dict(grant)
                or not current
                or dict(current) != dict(row)
                or await self.service.repo.connection(grant["owner"]) != connection
            ):
                return None
        except PlaylistError:
            return None
        return AccessToken(
            token=token,
            client_id=row["client"],
            scopes=[
                scope for scope in json.loads(row["scopes"]) if scope in json.loads(grant["scopes"])
            ],
            expires_at=int(row["expires"]),
            resource=self.resource,
            subject=grant["owner"],
            claims={"grant_id": row["grant_id"], "iss": self.issuer},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        grant_id = (
            token.grant_id
            if isinstance(token, Refresh)
            else (token.claims or {}).get("grant_id")
            if isinstance(token, AccessToken)
            else None
        )
        if grant_id:
            await self.store.execute(
                "UPDATE playlist_agent_tokens SET revoked=? WHERE id=?", [time.time(), grant_id]
            )
