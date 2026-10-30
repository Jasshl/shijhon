"""Browser consent and Streamable HTTP, co-hosted with the ordinary application."""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import anyio
from mcp.server import MCPServer
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.auth.routes import (
    build_metadata,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from pydantic import AnyHttpUrl, SecretStr
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from shijhon.agents.oauth import FLOW_SECONDS, SCOPES, OAuth
from shijhon.agents.tools import register_tools
from shijhon.dashboard.auth import Attempts, client_key, same
from shijhon.dashboard.context import legal_context
from shijhon.playlists.model import Connection, PlaylistError, UpstreamUnavailable
from shijhon.playlists.requests import RequestError, digest

if TYPE_CHECKING:
    from shijhon.app import ShijhonApp
    from shijhon.playlists.service import Playlists

COOKIE = "shijhon_agent_signin"
PRIVATE = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; style-src 'self'; font-src 'self';"
    " img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
}


class UpstreamErrors:
    """Run outside SDK authentication middleware, before any response has started."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except UpstreamUnavailable:
            await JSONResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "Navidrome is temporarily unavailable. Retry later.",
                },
                503,
                headers=PRIVATE,
            )(scope, receive, send)


class Agents:
    def __init__(self, host: ShijhonApp, service: Playlists, public_url: str) -> None:
        self.host, self.service = host, service
        self.oauth = OAuth(service, public_url)
        service.oauth = self.oauth
        self.ready = anyio.Event()
        self.login_attempts = Attempts()
        self.register_attempts = Attempts(limit=20)
        self.authorize_attempts = Attempts(limit=30)
        url = urlsplit(public_url)
        self.authority = url.netloc
        self.origin = f"{url.scheme}://{url.netloc}"
        self.secure = url.scheme == "https"
        self.cookie_path = urlsplit(self.oauth.issuer).path
        registration = ClientRegistrationOptions(
            enabled=True,
            valid_scopes=list(SCOPES),
            default_scopes=["create", "status"],
        )
        revocation = RevocationOptions(enabled=True)
        issuer = AnyHttpUrl(self.oauth.issuer)
        self.metadata = build_metadata(issuer, None, registration, revocation).model_dump(
            mode="json", exclude_none=True
        )
        self.metadata["authorization_response_iss_parameter_supported"] = True
        self.metadata["token_endpoint_auth_methods_supported"] = [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ]
        auth_routes = create_auth_routes(
            self.oauth,
            issuer,
            client_registration_options=registration,
            revocation_options=revocation,
        )
        for route in auth_routes:
            if route.path in ("/token", "/register", "/revoke"):
                route.app = CORSMiddleware(
                    route.app,
                    allow_origins=["*"],
                    allow_methods=["POST", "OPTIONS"],
                    allow_headers=["authorization", "content-type", "mcp-protocol-version"],
                )
        # The metadata is served at the RFC 8414 path, including a path-based issuer.
        self.metadata_path = (
            "/.well-known/oauth-authorization-server" + urlsplit(self.oauth.issuer).path
        )
        auth = Starlette(
            routes=[
                Route("/connect", self.connect, methods=["GET", "POST"]),
                Mount("/static", StaticFiles(packages=[("shijhon.dashboard", "static")])),
                *auth_routes,
            ]
        )
        self.mcp = MCPServer(
            "Shijhon",
            log_level="WARNING",
            instructions=(
                "Call get_capabilities before choosing a playlist workflow. Use saved server AI "
                "for descriptions and recurring playlists. Without server AI you can curate tracks "
                "yourself. Read personal history/library only when the user asks to use it. "
                "Missing setup blocks only affected features. Preserve all user constraints. "
                "Creating a playlist authorizes publication; confirm success using request status. "
                "Never create again under a new idempotency key after an uncertain result. "
                "Recurrence runs on Shijhon and requires an explicit user request."
                " When a request relates to a playlist or songs the user has, find them with "
                "get_native_playlists or search_library and pass their IDs as "
                "reference_playlists or reference_songs of create_playlist instead of pasting "
                "names. get_song_facts, get_playlist_profile and the sound tools "
                "(find_similar_songs, search_by_sound, find_sound_path) give approximate, "
                "optional hints; many songs have none and sound analysis may not be set up. "
                "Your own musical knowledge comes first: never reject a song only for missing "
                "facts, and say how many songs you could not check."
                " Prepare playlist audio only on explicit request with downloads permission; "
                "this saves audio on the server, separate from device copies."
            ),
            token_verifier=self.oauth,
            auth=AuthSettings(
                issuer_url=issuer,
                resource_server_url=AnyHttpUrl(self.oauth.resource),
                required_scopes=[],
                validate_token_resource=True,
            ),
        )
        register_tools(
            self.mcp, self.oauth, getattr(getattr(host, "services", None), "downloads", None)
        )
        http = self.mcp.streamable_http_app(
            streamable_http_path="/shijhon/mcp",
            json_response=True,
            stateless_http=True,
            max_request_body_size=128_000,
            transport_security=TransportSecuritySettings(
                allowed_hosts=[self.authority],
                allowed_origins=[self.origin],
            ),
        )
        auth.add_middleware(UpstreamErrors)
        http.add_middleware(UpstreamErrors)
        self.app = RequestBodyLimitMiddleware(
            Starlette(
                routes=[
                    Route(self.metadata_path, self.discovery),
                    *create_protected_resource_routes(
                        resource_url=AnyHttpUrl(self.oauth.resource),
                        authorization_servers=[issuer],
                        scopes_supported=list(SCOPES),
                        resource_name="Shijhon",
                    ),
                    Mount("/shijhon/agents", auth),
                    Mount("/", http),
                ]
            ),
            max_body_size=128_000,
        )

    def owns(self, path: str) -> bool:
        return (
            path in ("/shijhon/mcp", self.metadata_path)
            or path.startswith("/shijhon/agents/")
            or path == "/shijhon/agents"
            or path == "/.well-known/oauth-protected-resource" + urlsplit(self.oauth.resource).path
        )

    async def run(self) -> None:
        async with self.mcp.session_manager.run():
            self.ready.set()
            await anyio.sleep_forever()

    async def discovery(self, request: Request) -> Response:
        return JSONResponse(
            self.metadata, headers={"access-control-allow-origin": "*", "cache-control": "no-store"}
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if request.headers.get("host") != self.authority:
            await Response("Unrecognized host.", 400, headers=PRIVATE)(scope, receive, send)
            return
        peer = request.client.host if request.client else ""
        client = self.host.forwarding.client(peer, request.headers.raw)
        address = client_key(client, peer)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        attempts = {"register": self.register_attempts, "authorize": self.authorize_attempts}
        if (
            request.method != "OPTIONS"
            and endpoint in attempts
            and not attempts[endpoint].allow(address)
        ):
            await Response("Try again later.", 429, headers=PRIVATE)(scope, receive, send)
            return
        # The SDK checks PKCE and redirect binding. Require the resource indicator at
        # both token exchanges too, and reject ambiguous duplicate form/query fields.
        if endpoint in ("authorize", "token") and request.method != "OPTIONS":
            if request.method == "GET":
                encoded = scope.get("query_string", b"")
            else:
                if (
                    request.headers.get("content-type", "").split(";")[0]
                    != "application/x-www-form-urlencoded"
                ):
                    await JSONResponse({"error": "invalid_request"}, 400, headers=PRIVATE)(
                        scope, receive, send
                    )
                    return
                encoded = bytearray()
                async for chunk in request.stream():
                    encoded.extend(chunk)
                    if len(encoded) > 128_000:
                        await Response(status_code=413)(scope, receive, send)
                        return
            pairs = parse_qsl(
                bytes(encoded).decode("utf-8", errors="replace"), keep_blank_values=True
            )
            fields = dict(pairs)
            if len(fields) != len(pairs) or fields.get("resource") != self.oauth.resource:
                await JSONResponse({"error": "invalid_target"}, 400, headers=PRIVATE)(
                    scope, receive, send
                )
                return
            if request.method == "POST":
                body = bytes(encoded)

                async def replay() -> dict[str, Any]:
                    return {"type": "http.request", "body": body, "more_body": False}

                receive = replay
        await self.app(scope, receive, send)

    def return_to_agent(self, target: str) -> Response:
        # A new document starts a GET navigation without widening the credential
        # form's CSP to the client's callback. The callback was validated by the SDK.
        response = HTMLResponse(
            self.host.dashboard.env.get_template("agent_return.html").render(
                target=target, **legal_context()
            ),
            headers=PRIVATE,
        )
        response.delete_cookie(COOKIE, path=self.cookie_path)
        return response

    async def connect(self, request: Request) -> Response:
        raw = request.query_params.get("flow", "")
        try:
            flow = await self.oauth.flow(raw)
            params = AuthorizationParams.model_validate_json(flow["params"])
            client = await self.oauth.get_client(flow["client"])
            if not client:
                raise RequestError("This agent registration is unavailable.")
            cookie = request.cookies.get(COOKIE, "")
            if request.method == "GET":
                if flow["csrf"]:
                    if not same(flow["csrf"], digest(cookie)):
                        raise RequestError(
                            "Continue sign-in in the browser where you started.", 403
                        )
                else:
                    cookie = secrets.token_urlsafe(32)
                    await self.oauth.store.execute(
                        "UPDATE agent_oauth_flows SET csrf=? WHERE id=? AND csrf=''",
                        [digest(cookie), flow["id"]],
                    )
            else:
                if request.headers.get("origin", self.origin) != self.origin:
                    raise RequestError("Sign in from this installation's page.", 403)
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16_000:
                        raise RequestError("The sign-in form is too large.", 413)
                pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
                form = dict(pairs)
                # Scope checkboxes use distinct field names, so every field is unique.
                if (
                    len(pairs) != len(form)
                    or not cookie
                    or not same(form.get("csrf", ""), cookie)
                    or not same(flow["csrf"], digest(cookie))
                ):
                    raise RequestError("Reload the sign-in page and try again.", 403)
                if form.get("action") == "deny":
                    await self.oauth.store.execute(
                        "DELETE FROM agent_oauth_flows WHERE id=?", [flow["id"]]
                    )
                    query = {"error": "access_denied", "iss": self.oauth.issuer}
                    if params.state is not None:
                        query["state"] = params.state
                    uri = str(params.redirect_uri)
                    return self.return_to_agent(
                        uri + ("&" if "?" in uri else "?") + urlencode(query)
                    )
                if form.get("action") != "allow":
                    raise RequestError("Choose Connect or Cancel.")
                if flow["owner"]:
                    if "username" in form or "password" in form:
                        raise RequestError(
                            "This sign-in is already complete. "
                            "Reload the page to choose permissions.",
                            409,
                        )
                    scopes = [scope for scope in SCOPES if form.get("scope_" + scope) == "true"]
                    async with self.service.changes:
                        target = await self.oauth.approve(raw, flow["owner"], scopes)
                    return self.return_to_agent(target)
                peer = request.client.host if request.client else ""
                client_address = self.host.forwarding.client(peer, request.headers.raw)
                if not self.login_attempts.allow(client_key(client_address, peer)):
                    raise RequestError("Too many sign-in attempts. Try again later.", 429)
                login = await self.service.client.login(
                    form.get("username", ""),
                    form.get("password", ""),
                    str(client_address or "127.0.0.1"),
                )
                async with self.service.changes:
                    old = await self.service.repo.connection(login["id"])
                    values = {
                        "user_id": login["id"],
                        "username": login["username"],
                        "token": SecretStr(login["subsonicToken"]),
                        "salt": SecretStr(login["subsonicSalt"]),
                    }
                    connection = old.model_copy(update=values) if old else Connection(**values)
                    if not old:
                        connection = connection.model_copy(update={"use_shared_ai": True})
                    await self.service.save_connection(connection)
                    async with self.oauth.store.transaction() as db:
                        changed = await db.execute(
                            "UPDATE agent_oauth_flows SET owner=?"
                            " WHERE id=? AND expires>? AND owner=''",
                            [login["id"], flow["id"], time.time()],
                        )
                        claimed = changed.rowcount == 1
                    if not claimed:
                        await self.oauth.flow(raw)  # distinguish expiry from another sign-in
                        raise RequestError(
                            "This sign-in is already complete. "
                            "Reload the page to choose permissions.",
                            409,
                        )
                flow = await self.oauth.flow(raw)
            selected = await self.oauth.consent_scopes(flow, flow["owner"]) if flow["owner"] else []
            signed_in = await self.service.repo.connection(flow["owner"]) if flow["owner"] else None
            template = self.host.dashboard.env.get_template("agent_connect.html")
            response = HTMLResponse(
                template.render(
                    client_name=(client.client_name or "Connected agent")[:100],
                    callback_host=urlsplit(str(params.redirect_uri)).netloc,
                    scopes={scope: SCOPES[scope] for scope in selected},
                    selected_scopes=selected,
                    consenting=bool(flow["owner"]),
                    username=signed_in.username if signed_in else "",
                    **legal_context(),
                    csrf=cookie,
                    action=self.oauth.issuer + "/connect?" + urlencode({"flow": raw}),
                    prefix=self.oauth.issuer,
                ),
                headers={**PRIVATE, "referrer-policy": "same-origin"},
            )
            response.set_cookie(
                COOKIE,
                cookie,
                max_age=FLOW_SECONDS,
                path=self.cookie_path,
                secure=self.secure,
                httponly=True,
                samesite="lax",
            )
            return response
        except (PlaylistError, UnicodeError) as exc:
            message = str(exc) if isinstance(exc, PlaylistError) else "Send a valid sign-in form."
            code = exc.status if isinstance(exc, RequestError) else 400
            # Fixed messages only; never echo form fields or native login responses.
            return HTMLResponse(
                self.host.dashboard.env.get_template("agent_error.html").render(
                    error=message, **legal_context()
                ),
                code,
                headers=PRIVATE,
            )
