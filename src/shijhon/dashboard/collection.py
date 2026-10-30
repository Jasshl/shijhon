"""Personal libraries, bounded browsing, frozen bulk review and reversible choices."""

from __future__ import annotations

import json
import secrets
from contextlib import suppress
from dataclasses import asdict, replace
from hashlib import sha256
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, quote, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from shijhon.collection.contracts import BrowseView, CollectionError, Query
from shijhon.collection.profiles import BUILTINS
from shijhon.collection.service import Collection
from shijhon.dashboard.auth import Session

Pool = Literal["personal", "server"]

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard


async def read_form(request: Request) -> dict[str, str]:
    from shijhon.dashboard.app import HEADERS, Refused

    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise Refused(
            PlainTextResponse("Submit the collection form from the page.", 415, headers=HEADERS)
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 256 * 1024:
            raise Refused(
                PlainTextResponse(
                    "The selection is too large. Choose every matching result.",
                    413,
                    headers=HEADERS,
                )
            )
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=1200)
        if len(set(k for k, _ in pairs)) != len(pairs):
            raise ValueError
        return dict(pairs)
    except (UnicodeError, ValueError):
        raise Refused(
            PlainTextResponse("Reload the collection form and try again.", 400, headers=HEADERS)
        ) from None


# Show: the library's music, the music you collected that is not in it, or everything on
# the server (browsing only: no app library is everything on the server).
SHOWS: dict[str, tuple[BrowseView, Pool]] = {
    "saved": ("saved", "personal"),
    "unsaved": ("unsaved", "personal"),
    "server": ("all", "server"),
}
SHOW_LABELS = {
    "saved": "In this library",
    "unsaved": "Not in this library",
    "server": "Everything on the server",
}
ORIGIN_LABELS = {"all": "Any", "imported": "Imported", "catalog": "From the catalog"}


def show_of(query: Query) -> str:
    return "server" if query.pool == "server" else "unsaved" if query.view == "unsaved" else "saved"


def parse_query(values: Any) -> Query:
    fields = {
        k: values[k]
        for k in ("view", "scope", "search", "origin", "sort", "album_id", "artist_id", "pool")
        if values.get(k)
    }
    view = fields.get("view", "saved")
    show = values.get("show") or (
        "unsaved"
        if view == "unsaved"
        else "server"
        if view in ("all", "original") or (view != "saved" and fields.get("pool") == "server")
        else "saved"
    )
    if show not in SHOWS:
        raise CollectionError("Choose a valid collection filter and page.")
    fields["view"], fields["pool"] = SHOWS[show]
    if fields.get("origin", "all") not in ORIGIN_LABELS or fields.get("sort", "artist") not in (
        "title",
        "artist",
    ):
        raise CollectionError("Choose a valid collection filter and page.")
    pool_changed = bool(values.get("previous_pool")) and fields["pool"] != values.get(
        "previous_pool"
    )
    if pool_changed:
        fields.pop("album_id", None)
        fields.pop("artist_id", None)
    if fields.get("scope", "album") != "song":
        fields.pop("album_id", None)
        fields.pop("artist_id", None)
    try:
        return Query(**fields, offset=0 if pool_changed else int(values.get("offset", 0)), limit=40)
    except (TypeError, ValueError):
        raise CollectionError("Choose a valid collection filter and page.") from None


def href(
    prefix: str,
    query: Query,
    *,
    profile_id: str = "all",
    layout: str = "grid",
    operation: str = "",
    **changes: Any,
) -> str:
    return (
        prefix
        + "/collection"
        + ("/changes/" + quote(operation, safe="") if operation else "")
        + "?"
        + urlencode(
            {**asdict(replace(query, **changes)), "profile_id": profile_id, "layout": layout}
        )
    )


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    @property
    def service(self) -> Collection:
        service = getattr(self.dashboard.services, "collection", None)
        if not isinstance(service, Collection):
            raise CollectionError("Your collection is starting. Try again shortly.")
        return service

    @property
    def library(self) -> str:
        assert self.dashboard.services
        return str(self.dashboard.services.navidrome.library_id)

    def routes(self) -> list[Route]:
        wrapper = self.dashboard._personal_page
        return [
            Route(
                "/collection", wrapper(self.index, form_reader=read_form), methods=["GET", "POST"]
            ),
            Route("/collection/changes/{ident}", wrapper(self.change), methods=["GET", "POST"]),
            Route("/collection/progress/{ident}", wrapper(self.progress, cache_identity=True)),
            Route(
                "/collection/source-progress", wrapper(self.source_progress, cache_identity=True)
            ),
            Route("/collection/artwork/{ident}", wrapper(self.artwork, cache_identity=True)),
        ]

    async def render(
        self,
        session: Session,
        *,
        query: Query,
        profile_id: str | None = None,
        layout: str = "grid",
        op: dict[str, Any] | None = None,
        error: str = "",
        create: bool = False,
    ) -> Response:
        from shijhon.dashboard.app import PATH

        listing: dict[str, Any] = {"rows": [], "total": 0, "counts": {}}
        listing_ready = False
        state, profiles, recent = None, [], []
        page_adjusted = False
        profile: dict[str, Any] = {"id": "", "selection_id": "all", "name": "All", "builtin": True}
        active = profile
        try:
            active = await self.service.profile(session.user_id)
            profile = await self.service.profile(session.user_id, profile_id)
            profiles = await self.service.list_profiles(session.user_id)
            state = await self.service.index_state(session.user_id, self.library)
            refresher = getattr(self.dashboard.services, "collection_refresh", None)
            if refresher is not None and refresher.running:
                state = replace(state, state="indexing")
            reasons = getattr(self.dashboard.services, "reasons", None)
            if (
                reasons is not None
                and refresher is not None
                and refresher.reader.has_bearer(session.user_id)
            ):
                # The account's favorites bring albums into Original imported library:
                # read with its own native sign-in when due (the first read is waited
                # for, briefly).
                reader, owner = refresher.reader, session.user_id
                await reasons.due_with(owner, lambda: reader.starred(owner))
            if state.generation:
                listing = await self.service.browse(
                    session.user_id, self.library, query, profile_id=profile["selection_id"]
                )
                listing_ready = True
                if listing["total"] and query.offset >= listing["total"]:
                    # A change can empty the last page without emptying the result set.
                    # Retry once at the nearest valid page; never expand the song query.
                    query = replace(
                        query, offset=((listing["total"] - 1) // query.limit) * query.limit
                    )
                    listing = await self.service.browse(
                        session.user_id, self.library, query, profile_id=profile["selection_id"]
                    )
                    page_adjusted = True
                if not listing["total"]:
                    query = replace(query, offset=0)
                    page_adjusted = False
                recent = [
                    dict(row)
                    for row in await self.service.store.fetchall(
                        "SELECT id,action,state,total,processed,changed,skipped"
                        " FROM collection_operations"
                        " WHERE owner_id=? AND profile_id=? AND library_id=?"
                        " ORDER BY created DESC LIMIT 8",
                        [session.user_id, profile["id"], self.library],
                    )
                ]
        except CollectionError as exc:
            error = error or str(exc)
        # Songs use a list without replacing the person's album/artist layout choice.
        preferred_layout = layout if layout in ("grid", "list") else "grid"
        layout = "list" if query.scope == "song" else preferred_layout
        options = {"profile_id": profile["selection_id"], "layout": preferred_layout}
        operation_scope, operation_target = "song", ""
        if op:
            source = (
                await self.service.operation(session.user_id, op["source"]) if op["source"] else op
            )
            operation_scope = Query(**json.loads(source["query"])).scope
            label = await self.service.store.fetchone(
                "SELECT min(t.title) AS title,min(t.album) AS album,min(t.artist) AS artist"
                " FROM collection_operation_items i JOIN collection_tracks t"
                " ON t.track_key=i.track_key AND t.library_id=? WHERE i.operation_id=?",
                [source["library_id"], source["id"]],
            )
            if label and source["group_count"] == 1:
                operation_target = str(
                    label[operation_scope if operation_scope != "song" else "title"] or ""
                )
        filters = []
        if query.search:
            filters.append(f"Search: {query.search}")
        if show_of(query) != "saved":
            filters.append(SHOW_LABELS[show_of(query)])
        if query.origin != "all":
            filters.append(ORIGIN_LABELS[query.origin])
        return await self.dashboard._render(
            "collection.html",
            session=session,
            current="collection",
            title="Your collection",
            query=query,
            listing=listing,
            listing_ready=listing_ready,
            index_state=state,
            op=op,
            recent=recent,
            error=error,
            profile=profile,
            active=active,
            custom_profiles=profiles,
            builtins=BUILTINS,
            layout=layout,
            preferred_layout=preferred_layout,
            asset_revision=sha256(
                b"".join(
                    files("shijhon.dashboard").joinpath("static", name).read_bytes()
                    for name in ("collection.js", "collection.css")
                )
            ).hexdigest()[:16],
            show=show_of(query),
            shows=SHOW_LABELS,
            origins=ORIGIN_LABELS,
            page_adjusted=page_adjusted,
            first=href(PATH, query, offset=0, **options),
            filter_summary=" · ".join(filters),
            reset_url=href(
                PATH, Query(view="saved", scope=query.scope, pool="personal"), **options
            ),
            server_url=href(
                PATH,
                Query(view="all", scope=query.scope, pool="server"),
                **options,
            ),
            create_url=href(PATH, query, **options) + "&create=1#new-library-name",
            scope_back=href(
                PATH,
                query,
                scope="album" if query.album_id else "artist",
                album_id="",
                artist_id="",
                offset=0,
                **options,
            ),
            all_songs=href(PATH, query, album_id="", artist_id="", offset=0, **options),
            operation_scope=operation_scope,
            operation_target=operation_target,
            history_links={
                row["id"]: href(PATH, query, operation=row["id"], **options) for row in recent
            },
            song_links={
                row["item_key"]: href(
                    PATH,
                    query,
                    scope="song",
                    album_id=row["item_key"] if query.scope == "album" else "",
                    artist_id=row["item_key"] if query.scope == "artist" else "",
                    offset=0,
                    **options,
                )
                for row in listing["rows"]
            },
            owner_id=session.user_id,
            create=create,
            request_key=secrets.token_hex(16),
            links={
                show: href(
                    PATH,
                    query,
                    view=SHOWS[show][0],
                    pool=SHOWS[show][1],
                    album_id="" if SHOWS[show][1] != query.pool else query.album_id,
                    artist_id="" if SHOWS[show][1] != query.pool else query.artist_id,
                    offset=0,
                    **options,
                )
                for show in SHOWS
            },
            layouts={
                kind: href(PATH, query, profile_id=profile["selection_id"], layout=kind)
                for kind in ("grid", "list")
            },
            previous=href(PATH, query, offset=max(0, query.offset - query.limit), **options),
            following=href(PATH, query, offset=query.offset + query.limit, **options),
            browse_url=href(PATH, query, **options),
        )

    def see(self, query: Query, profile: str, layout: str = "grid") -> Response:
        return self.dashboard._see(
            "/collection?" + urlencode({**asdict(query), "profile_id": profile, "layout": layout})
        )

    async def index(self, request: Request, session: Session) -> Response:
        query, profile, layout = Query(view="saved", pool="personal"), None, "grid"
        try:
            values = request.state.form if request.method == "POST" else request.query_params
            query = parse_query(values)
            profile = values.get("profile_id") or None
            layout = values.get("layout", "grid")
            if request.method != "POST":
                return await self.render(
                    session,
                    query=query,
                    profile_id=None if profile == "__create" else profile,
                    layout=layout,
                    create=profile == "__create" or values.get("create") == "1",
                )
            action = values.get("action", "")
            if action == "create_profile":
                new = await self.service.create_profile(
                    session.user_id,
                    values.get("name", ""),
                    start=values.get("start", "original"),
                    source=values.get("source", ""),
                )
                # An empty library opens on the music you collected, to choose from at once.
                start: BrowseView = "unsaved" if values.get("start") == "empty" else "saved"
                return self.see(Query(view=start, scope=query.scope, pool="personal"), new, layout)
            if action == "activate":
                await self.service.activate_profile(session.user_id, profile or "all")
                return self.see(query, profile or "all", layout)
            if action == "rename":
                await self.service.rename_profile(
                    session.user_id, profile or "", values.get("name", "")
                )
                return self.see(query, profile or "all", layout)
            if action in ("archive", "restore"):
                await self.service.archive_profile(
                    session.user_id, profile or "", archived=action == "archive"
                )
                return self.see(
                    replace(query, offset=0),
                    "all" if action == "archive" else profile or "all",
                    layout,
                )
            if action == "refresh":
                service, library = self.service, self.library
                await service.actor(session.user_id, library)
                refresher = getattr(self.dashboard.services, "collection_refresh", None)
                if refresher is not None and session.principal is not None:
                    token = await refresher.reader.token_for(session.principal)
                    if token is None:
                        from shijhon.dashboard.app import PATH

                        return self.dashboard._see(
                            "/sign-in?reauth=1&next="
                            + quote(
                                href(PATH, query, profile_id=profile or "all", layout=layout),
                                safe="/",
                            )
                        )
                    await refresher.signed_in(session.principal, token)
                    return self.see(query, profile or "all", layout)

                async def refresh() -> None:
                    with suppress(Exception):
                        await service.refresh(session.user_id, library)

                if not self.dashboard.host.try_spawn(refresh):
                    raise CollectionError("The index worker is unavailable. Try again shortly.")
                return self.see(query, profile or "all", layout)
            selected = None
            browse_query = query
            target = await self.service.profile(session.user_id, profile)
            profile = target["selection_id"]
            single = values.get("single_save") or values.get("single_remove")
            if single:
                selected = [single]
                action = "save" if values.get("single_save") else "remove"
                # The row button explicitly says whole album/artist and freezes current contents.
                mutation_query = replace(
                    query, view="all", origin="all", search="", album_id="", artist_id=""
                )
            elif values.get("selection") != "matching":
                selected = [k[5:] for k in values if k.startswith("pick_")]
                if values.get("selected"):
                    selected = json.loads(values["selected"])
                if not isinstance(selected, list) or any(not isinstance(k, str) for k in selected):
                    raise CollectionError("Select rows and try again.")
            ident = await self.service.prepare(
                session.user_id,
                self.library,
                mutation_query if single else query,
                action=action,
                request_key=values.get("request_key", ""),
                selected=selected,
                profile_id=profile,
            )
            if single:
                await self.service.repo.freeze(await self.service.operation(session.user_id, ident))
                frozen = await self.service.operation(session.user_id, ident)
                if frozen["total"] <= 100:
                    await self.service.approve(session.user_id, ident)
            return self.dashboard._see(
                href("", browse_query, profile_id=profile, layout=layout, operation=ident)
                + "#change-heading"
            )
        except (CollectionError, json.JSONDecodeError, RecursionError) as exc:
            return await self.render(
                session,
                query=query,
                profile_id=profile,
                layout=layout,
                error=str(exc)
                if isinstance(exc, CollectionError)
                else "Select rows and try again.",
            )

    async def change(self, request: Request, session: Session) -> Response:
        ident = request.path_params["ident"]
        query, profile, layout = Query(view="saved"), None, "grid"
        try:
            op = await self.service.operation(session.user_id, ident)
            profile = op["profile_id"]
            if request.query_params:
                query = parse_query(request.query_params)
                layout = request.query_params.get("layout", "grid")
                candidate = await self.service.profile(
                    session.user_id, request.query_params.get("profile_id") or profile
                )
                if candidate["id"] != op["profile_id"]:
                    raise CollectionError(
                        "This change belongs to a different library. Open it from Recent changes."
                    )
                profile = candidate["selection_id"]
            else:
                source = (
                    await self.service.operation(session.user_id, op["source"])
                    if op["source"]
                    else op
                )
                # A bookmark has no return query: browse what the change was made in.
                frozen = Query(**json.loads(source["query"]))
                view, pool = SHOWS[show_of(frozen)]
                query = Query(view=view, scope=frozen.scope, pool=pool)
            if request.method == "POST":
                action = request.state.form.get("action")
                if action == "approve":
                    await self.service.approve(session.user_id, ident)
                elif action == "undo":
                    ident = await self.service.undo(session.user_id, ident)
                elif action == "retry":
                    await self.service.retry(session.user_id, ident)
                elif action == "discard":
                    await self.service.discard(session.user_id, ident)
                else:
                    raise CollectionError("Choose a collection action.")
                return self.dashboard._see(
                    href("", query, profile_id=profile, layout=layout, operation=ident)
                    + "#change-heading"
                )
            return await self.render(session, query=query, profile_id=profile, layout=layout, op=op)
        except CollectionError as exc:
            return await self.render(
                session, query=query, profile_id=profile, layout=layout, error=str(exc)
            )

    async def source_progress(self, request: Request, session: Session) -> Response:
        try:
            state = await self.service.index_state(session.user_id, self.library)
            refresher = getattr(self.dashboard.services, "collection_refresh", None)
            running = refresher is not None and refresher.running
            return JSONResponse(
                {
                    "state": "indexing" if running else state.state,
                    "indexed": state.indexed,
                    "ready": bool(state.generation),
                    "failed": bool(state.error),
                },
                headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
            )
        except CollectionError:
            return JSONResponse(
                {"error": "Library progress is unavailable."},
                status_code=403,
                headers={"cache-control": "no-store"},
            )

    async def progress(self, request: Request, session: Session) -> Response:
        try:
            op = await self.service.operation(session.user_id, request.path_params["ident"])
            return JSONResponse(
                {k: op[k] for k in ("state", "total", "processed", "changed", "skipped")},
                headers={"cache-control": "no-store"},
            )
        except CollectionError:
            return JSONResponse({"error": "This change is unavailable."}, status_code=403)

    async def artwork(self, request: Request, session: Session) -> Response:
        try:
            body, mime = await self.service.artwork(
                session.user_id, self.library, request.path_params["ident"]
            )
            return Response(
                body,
                media_type=mime,
                headers={
                    "cache-control": "private, max-age=60",
                    "x-content-type-options": "nosniff",
                },
            )
        except Exception:
            return Response(status_code=404, headers={"cache-control": "no-store"})
