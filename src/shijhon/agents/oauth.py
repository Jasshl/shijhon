"""Durable OAuth storage behind the official SDK's authorization endpoints.

The SDK validates clients, redirects and S256 PKCE. This provider binds every
authorization to this installation and a freshly checked native owner. Only
credential hashes are persisted for codes, browser flows and bearer tokens;
rotated pairs remain briefly in memory for idempotent refresh retries.
"""

from __future__ import annotations

import asyncio
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
    TokenErrorCode,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from shijhon.playlists.model import PlaylistError, UpstreamUnavailable
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
# Enough for overlapping requests and an immediate lost-response retry, while
# limiting how long possession of a spent credential can recover its successor.
REFRESH_REPLAY_SECONDS = 20
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
        self._refresh_replays: dict[str, tuple[float, OAuthToken]] = {}

    def _forget_refresh_replay(self, ident: str, replay: tuple[float, OAuthToken]) -> None:
        if self._refresh_replays.get(ident) is replay:
            del self._refresh_replays[ident]

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

    async def consent_scopes(self, flow: dict[str, Any], owner: str) -> list[str]:
        params = AuthorizationParams.model_validate_json(flow["params"])
        existing = await self.store.fetchone(
            "SELECT scopes FROM playlist_agent_tokens"
            " WHERE owner=? AND library=? AND oauth_client=? AND revoked=0",
            [owner, self.service.requests.library, flow["client"]],
        )
        return list(
            dict.fromkeys(
                [*(json.loads(existing["scopes"]) if existing else []), *(params.scopes or [])]
            )
        )

    async def approve(self, raw: str, owner: str, scopes: list[str]) -> str:
        flow = await self.flow(raw)
        params = AuthorizationParams.model_validate_json(flow["params"])
        if flow["owner"] and flow["owner"] != owner:
            raise RequestError("This consent belongs to another account.", 403)
        if params.resource != self.resource:
            raise RequestError("This installation's address changed. Connect your agent again.")
        if not scopes or not set(scopes) <= set(await self.consent_scopes(flow, owner)):
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
        except UpstreamUnavailable:
            raise
        except PlaylistError:
            raise TokenError("invalid_grant", "Reconnect this agent.") from None
        issued: OAuthToken | None = None
        error = ""
        error_code: TokenErrorCode = "invalid_grant"
        async with self.store.transaction() as db:
            now = time.time()
            fresh = await (
                await db.execute(
                    "SELECT g.revoked,g.scopes FROM playlist_agent_tokens g"
                    " JOIN agent_oauth_clients c ON c.id=g.oauth_client"
                    " WHERE g.id=? AND c.id=?",
                    [grant_id, client_id],
                )
            ).fetchone()
            if not fresh or fresh["revoked"]:
                error = "Reconnect this agent."
            else:
                permitted = set(scopes) <= set(json.loads(fresh["scopes"]))
                kind, ident = consume
                if kind == "code":
                    if not permitted:
                        error = "Reconnect this agent."
                    else:
                        result = await db.execute(
                            "DELETE FROM agent_oauth_codes WHERE id=? AND expires>?", [ident, now]
                        )
                        if result.rowcount != 1:
                            error = "This credential has already been used."
                else:
                    credential = await (
                        await db.execute(
                            "SELECT used,scopes FROM agent_oauth_tokens"
                            " WHERE id=? AND kind='refresh'"
                            " AND client=? AND grant_id=? AND resource=? AND expires>?",
                            [ident, client_id, grant_id, self.resource, now],
                        )
                    ).fetchone()
                    if not credential:
                        error = "Reconnect this agent."
                    elif credential["used"]:
                        replay = self._refresh_replays.get(ident)
                        if (
                            replay
                            and now < credential["used"] + REFRESH_REPLAY_SECONDS
                            and time.monotonic() < replay[0]
                        ):
                            successor = await (
                                await db.execute(
                                    "SELECT used FROM agent_oauth_tokens"
                                    " WHERE id=? AND kind='refresh' AND expires>?",
                                    [digest(replay[1].refresh_token or ""), now],
                                )
                            ).fetchone()
                            if not successor or successor["used"]:
                                error = "This refresh has already been superseded."
                            elif not permitted:
                                error = "Reconnect this agent."
                            elif set(scopes) != set((replay[1].scope or "").split()):
                                error_code = "invalid_scope"
                                error = "Retry a refresh with the same permissions."
                            else:
                                issued = replay[1].model_copy()
                        else:
                            error = "This credential has already been used."
                            await db.execute(
                                "UPDATE playlist_agent_tokens SET revoked=? WHERE id=?",
                                [now, grant_id],
                            )
                    elif not set(scopes) <= set(json.loads(credential["scopes"])):
                        error_code = "invalid_scope"
                        error = "Refresh cannot add permissions."
                    elif not permitted:
                        error = "Reconnect this agent."
                    else:
                        await db.execute(
                            "UPDATE agent_oauth_tokens SET used=? WHERE id=?", [now, ident]
                        )
            if not error and issued is None:
                access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
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
                issued = OAuthToken(
                    access_token=access,
                    refresh_token=refresh,
                    expires_in=ACCESS_SECONDS,
                    scope=" ".join(scopes),
                )
                if consume[0] == "refresh":
                    # Publish under the DB lock: the next exchange must see this
                    # pair when it sees used!=0. A rollback cannot replay it because
                    # the persisted credential remains unused. Never persist plaintext.
                    replay = (time.monotonic() + REFRESH_REPLAY_SECONDS, issued.model_copy())
                    self._refresh_replays[consume[1]] = replay
                    asyncio.get_running_loop().call_later(
                        REFRESH_REPLAY_SECONDS, self._forget_refresh_replay, consume[1], replay
                    )
        # SDK exceptions are frozen dataclasses. Raise outside the async-generator
        # transaction context, and keep replay revocation committed.
        if error:
            raise TokenError(error_code, error)
        assert issued is not None
        return issued

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
        async with self.store.transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM agent_oauth_tokens WHERE id=? AND kind='refresh' AND client=?",
                    [ident, client.client_id],
                )
            ).fetchone()
            now = time.time()
            if not row or row["expires"] <= now or row["resource"] != self.resource:
                return None
            replay = self._refresh_replays.get(ident)
            if row["used"] and not (
                replay
                and now < row["used"] + REFRESH_REPLAY_SECONDS
                and time.monotonic() < replay[0]
            ):
                # The SDK checks scopes before exchange. Recognize spent-token
                # reuse here too, so a wider scope cannot hide its revocation signal.
                await db.execute(
                    "UPDATE playlist_agent_tokens SET revoked=? WHERE id=?",
                    [now, row["grant_id"]],
                )
                return None
        # In-window used credentials reach exchange too: check their live successor
        # atomically with rotation. Preloaded credentials also recheck replay/reuse.
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
                or not connection.same_settings(await self.service.repo.connection(grant["owner"]))
            ):
                return None
        except UpstreamUnavailable:
            raise
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
