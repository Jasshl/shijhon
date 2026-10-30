"""Small JSON API. Bearer grants never authenticate through dashboard cookies."""

from __future__ import annotations

import json
from collections.abc import Callable

import anyio
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shijhon.playlists.model import PlaylistError, UpstreamUnavailable
from shijhon.playlists.prompt import context
from shijhon.playlists.requests import ContextRequest, PlaylistRequest, RequestError
from shijhon.playlists.service import Playlists


class API:
    def __init__(self, service: Callable[[], Playlists]) -> None:
        self.service = service

    def routes(self) -> list[Route]:
        return [
            Route("/api/playlists/context", self.handle, methods=["POST"]),
            Route("/api/playlists/requests", self.handle, methods=["POST"]),
            Route("/api/playlists/requests/{ident}", self.handle, methods=["GET"]),
            Route("/api/playlists/requests/{ident}/retry", self.handle, methods=["POST"]),
        ]

    async def body(self, request: Request) -> object:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise RequestError("Send application/json.", 415)
        body = bytearray()
        with anyio.fail_after(10):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 128_000:
                    raise RequestError("The request body is too large.", 413)
        try:
            # Duplicate keys must not produce different interpretations across clients.
            def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in items:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            return json.loads(body, object_pairs_hook=pairs)
        except (ValueError, UnicodeError):
            raise RequestError("Send a valid JSON object.") from None

    async def handle(self, request: Request) -> JSONResponse:
        code = 200
        try:
            headers = request.headers.getlist("authorization")
            if len(headers) != 1:
                raise RequestError("An OAuth access token is required.", 401)
            scheme, _, raw = headers[0].partition(" ")
            if scheme.lower() != "bearer" or not raw or len(raw) > 256:
                raise RequestError("An OAuth access token is required.", 401)
            if request.url.query:
                raise RequestError("Use the JSON body, without URL query parameters.")
            ident = request.path_params.get("ident", "")
            service = self.service()
            token = await service.oauth.verify_token(raw) if service.oauth else None
            if not token:
                raise RequestError("Reconnect this agent with OAuth.", 401)
            creator = str((token.claims or {})["grant_id"])
            scope = "status" if request.method == "GET" else "create"
            if scope not in token.scopes:
                raise RequestError("The connection does not grant that operation.", 403)
            if request.url.path.endswith("/context"):
                # Context imports do not mutate run/publication state. Keep revocation
                # available while importing and put a deadline inside the client's wait.
                with anyio.fail_after(min(12, service.settings.run_seconds)):
                    spec = ContextRequest.model_validate(await self.body(request))
                    if (spec.use_history and "history" not in token.scopes) or (
                        spec.use_library and "library" not in token.scopes
                    ):
                        raise RequestError("The context needs its separate OAuth permission.", 403)
                    connection, grant = await service.requests.grant(creator, scope)
                    service.requests.context_scopes(spec, grant)

                    async def authorize() -> None:
                        current, fresh = await service.requests.grant(creator, scope)
                        if not connection.same_settings(current):
                            raise RequestError("The saved playlist authorization changed.", 403)
                        service.requests.context_scopes(spec, fresh)

                    result = await context(
                        service,
                        connection,
                        history=spec.use_history,
                        library=spec.use_library,
                        limit=spec.limit,
                        authorize=authorize,
                    )
            else:
                async with service.changes:
                    connection, grant = await service.requests.grant(creator, scope)
                    owner = connection.user_id
                    if request.method == "GET":
                        result = await service.requests.status(owner, ident, creator=creator)
                    elif ident:
                        if await self.body(request) != {}:
                            raise RequestError("Retry takes an empty JSON object.")
                        await service.requests.retry(owner, ident, creator=creator)
                        result = await service.requests.status(owner, ident, creator=creator)
                        code = 202
                    else:
                        creation = PlaylistRequest.model_validate(await self.body(request))
                        if (creation.use_history and "history" not in token.scopes) or (
                            (
                                creation.use_library
                                or creation.reference_playlists
                                or creation.reference_songs
                            )
                            and "library" not in token.scopes
                        ):
                            raise RequestError(
                                "The context needs its separate OAuth permission.", 403
                            )
                        ident, created = await service.requests.create(
                            owner,
                            request.headers.get("idempotency-key", ""),
                            creation,
                            creator=creator,
                            scopes=token.scopes,
                        )
                        result = await service.requests.status(owner, ident, creator=creator)
                        code = 202 if created else 200
        except RequestError as exc:
            code, result = exc.status, {"error": str(exc)}
        except ValidationError:
            code, result = 400, {"error": "Check the playlist input and supported fields."}
        except UpstreamUnavailable:
            code, result = 503, {"error": "Navidrome is temporarily unavailable. Retry later."}
        except PlaylistError:
            code, result = 403, {"error": "The saved playlist authorization is unavailable."}
        except Exception:
            code, result = (
                503,
                {"error": "The playlist request could not be completed. Retry later."},
            )
        return JSONResponse(
            result,
            code,
            headers={
                "cache-control": "no-store",
                "x-content-type-options": "nosniff",
                "referrer-policy": "no-referrer",
            },
        )
