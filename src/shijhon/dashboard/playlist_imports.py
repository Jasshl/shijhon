"""Bounded local playlist forms using the dashboard session and CSRF checks."""

from __future__ import annotations

import json
import secrets
from email import policy
from email.parser import BytesParser
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from shijhon.dashboard.auth import Session
from shijhon.playlists.import_completion import changes as changes_of
from shijhon.playlists.import_formats import COLUMNS, FORMATS, MAX_BYTES, MAX_ROWS
from shijhon.playlists.model import Definition, PlaylistError

if TYPE_CHECKING:
    from shijhon.dashboard.app import Dashboard
    from shijhon.playlists.service import Playlists


async def read_form(request: Request, *, max_bytes: int = MAX_BYTES) -> dict[str, str]:
    """Parse a bounded form in memory; no upload is written to disk."""
    from shijhon.dashboard.app import HEADERS, Refused

    kind = request.headers.get("content-type", "")
    multipart = kind.split(";", 1)[0].lower() == "multipart/form-data"
    limit = max_bytes * 2 + 65536 if multipart else max_bytes * 3 + 65536
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise Refused(
                PlainTextResponse(
                    "The import form exceeds the administrator's input budget.",
                    413,
                    headers=HEADERS,
                )
            )
    try:
        if multipart:
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + kind.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
            )
            if not message.is_multipart() or message.defects:
                raise ValueError
            parts = list(message.iter_parts())
            if len(parts) > 24:
                raise ValueError
            pairs: list[tuple[str, str]] = []
            uploaded: tuple[str, bytes] | None = None
            for part in parts:
                if (
                    part.is_multipart()
                    or part.defects
                    or part.get_content_disposition() != "form-data"
                ):
                    raise ValueError
                name = part.get_param("name", header="content-disposition")
                payload = part.get_payload(decode=True)
                if not isinstance(name, str) or not isinstance(payload, bytes):
                    raise ValueError
                if name in ("file", "csv") and part.get_filename():
                    if uploaded is not None:
                        raise ValueError
                    uploaded = (name, payload)
                    pairs.append(("filename", str(part.get_filename())))
                else:
                    if part.get_filename():
                        raise ValueError
                    pairs.append((name, payload.decode("utf-8-sig")))
            if uploaded:
                encoding = dict(pairs).get("encoding", "utf-8-sig")
                if encoding not in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
                    raise ValueError
                payload = uploaded[1]
                if encoding == "utf-8-sig" and payload.startswith((b"\xff\xfe", b"\xfe\xff")):
                    encoding = "utf-16"
                pairs.append((uploaded[0], payload.decode(encoding)))
        elif kind.split(";", 1)[0].lower() == "application/x-www-form-urlencoded":
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=24)
        else:
            raise Refused(
                PlainTextResponse(
                    "Submit pasted text or a local playlist file from the import page.",
                    415,
                    headers=HEADERS,
                )
            )
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError
        result = dict(pairs)
        if sum(len(result.get(k, "").encode()) for k in ("lines", "csv", "file")) > max_bytes:
            raise Refused(
                PlainTextResponse(
                    "The decoded playlist exceeds the administrator's input budget.",
                    413,
                    headers=HEADERS,
                )
            )
        return result
    except (UnicodeError, ValueError, TypeError):
        raise Refused(
            PlainTextResponse(
                "Unreadable import form. Check the file encoding and format.", 400, headers=HEADERS
            )
        ) from None


class Pages:
    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard

    @property
    def service(self) -> Playlists:
        if not self.dashboard.services or not self.dashboard.services.playlists:
            raise PlaylistError("Playlist services are starting.")
        return self.dashboard.services.playlists

    def routes(self) -> list[Route]:
        return [
            Route(
                "/playlists/import",
                self.dashboard._personal_page(self.index, form_reader=self.read_form),
                methods=["GET", "POST"],
            ),
            Route(
                "/playlists/import/{ident}",
                self.dashboard._personal_page(self.detail),
                methods=["GET", "POST"],
            ),
        ]

    async def read_form(self, request: Request) -> dict[str, str]:
        return await read_form(
            request, max_bytes=getattr(self.service.settings, "import_max_bytes", MAX_BYTES)
        )

    async def render(
        self,
        session: Session,
        *,
        ident: str = "",
        error: str = "",
        form: dict[str, str] | None = None,
        page: int = 0,
        only_missing: bool = False,
    ) -> Response:
        service = self.service
        # Authenticate the saved native identity before reading private import evidence.
        connection = await service.repo.connection(session.user_id)
        if connection:
            try:
                await service.connection(session.user_id)
            except PlaylistError as exc:
                error = error or str(exc)
                connection = None
        listing = []
        if connection:
            listing = await service.repo.store.fetchall(
                "SELECT m.*,r.state AS import_state,"
                "json_extract(r.data,'$.skipped') AS missing_count"
                " FROM playlist_mixes m JOIN playlist_imports i ON i.id=m.id"
                " LEFT JOIN playlist_runs r ON r.id=(SELECT id FROM playlist_runs"
                " WHERE mix=m.id ORDER BY created DESC LIMIT 1)"
                " WHERE m.owner=? AND i.library=? ORDER BY m.rowid DESC LIMIT 100",
                [session.user_id, service.requests.library],
            )
        else:
            ident = ""
        shown: dict[str, Any] | None = None
        if ident:
            evidence = await service.imports.evidence(session.user_id, ident)
            original = json.loads(evidence["input"])
            mix = await service.repo.mix(ident, session.user_id)
            runs = await service.repo.runs(ident)
            if not runs:
                raise PlaylistError("This import has no current run. Open it from the import list.")
            run = runs[0]
            data = run["data"]
            all_rows = data.get("rows", [])
            listed = [
                {
                    "number": n + 1,
                    "input": value,
                    **(all_rows[n] if n < len(all_rows) else {"state": "pending", "track": None}),
                }
                for n, value in enumerate(original["rows"])
            ]
            if only_missing:
                listed = [r for r in listed if r["state"] not in ("matched", "removed")]
            total = len(original["rows"])
            matched = sum(r["state"] == "matched" for r in all_rows)
            removed = sum(r["state"] == "removed" for r in all_rows)
            # Progress counts only entries matched in this attempt, not kept ones.
            attempt, cursor = data.get("attempt", 0), data.get("cursor", 0)
            checking = [
                i
                for i, r in enumerate(all_rows)
                if r["state"] not in ("matched", "removed") or r.get("attempt", 0) == attempt
            ]
            previous = await service.imports.publication(ident) if mix["playlist_id"] else None
            published_missing = (
                len(previous["data"].get("import_review", {}).get("excluded", []))
                if previous
                else 0
            )
            completion = data.get("completion")
            recheck_available = bool(published_missing) and (
                run["state"] in ("published", "discarded")
                or (run["state"] in ("preview", "failed") and bool(completion))
            )
            changes = changes_of(completion, all_rows) if completion else []
            shown = {
                "id": ident,
                "name": original["name"],
                "run": run,
                "rows": listed[page * 100 : (page + 1) * 100],
                "only_missing": only_missing,
                "matched": matched,
                "removed": removed,
                "total": total,
                "missing": total - matched - removed,
                "page": page,
                "pages": max(1, (len(listed) + 99) // 100),
                "listed": len(listed),
                "checked": sum(i < cursor for i in checking),
                "checking": len(checking) + total - len(all_rows),
                "releases": data.get("releases", [])[page * 100 : (page + 1) * 100],
                "release_total": len(data.get("releases", [])),
                "review_key": service.imports.review_key(data),
                "playlist_id": mix["playlist_id"],
                "recheck_available": recheck_available,
                "unfinished": service.imports.unfinished(run["state"], data),
                "published_missing": published_missing,
                "changes": changes[page * 100 : (page + 1) * 100],
                "change_total": len(changes),
                "length": (len(completion.get("snapshot", [])), len(completion.get("plan") or []))
                if completion
                else None,
            }
        return await self.dashboard._render(
            "playlist_imports.html",
            session=session,
            current="playlists",
            error=error,
            connected=bool(connection),
            shown=shown,
            listing=[
                {**dict(r), "spec": Definition.model_validate_json(r["definition"])}
                for r in listing
            ],
            form=form or {},
            request_key=(form or {}).get("request_key", secrets.token_hex(16)),
            formats=FORMATS,
            columns=COLUMNS,
            max_entries=getattr(service.settings, "import_max_entries", MAX_ROWS),
            max_bytes=getattr(service.settings, "import_max_bytes", MAX_BYTES),
        )

    async def index(self, request: Request, session: Session) -> Response:
        if request.method != "POST":
            return await self.render(session)
        form = request.state.form
        try:
            lines = form.get("lines", "")
            uploaded = form.get("file", "") or form.get("csv", "")
            if lines.strip() and uploaded:
                raise PlaylistError("Paste text or upload a playlist file, one source per import.")
            async with self.service.changes:
                ident = await self.service.imports.create(
                    session.user_id,
                    form.get("request_key", ""),
                    form.get("name", ""),
                    uploaded if uploaded else lines,
                    csv_file=bool(form.get("csv")),
                    format=form.get("format", "auto"),
                    filename=form.get("filename", ""),
                    mapping={key: form.get(key + "_column", "") for key in COLUMNS},
                )
            return self.dashboard._see(f"/playlists/import/{ident}")
        except (PlaylistError, ValidationError) as exc:
            error = (
                str(exc) if isinstance(exc, PlaylistError) else "Enter a printable playlist name."
            )
            return await self.render(session, error=error, form=form)

    async def detail(self, request: Request, session: Session) -> Response:
        ident = request.path_params["ident"]
        try:
            if request.method == "POST":
                form = request.state.form
                async with self.service.changes:
                    await self.service.imports.action(
                        session.user_id,
                        ident,
                        form.get("run", ""),
                        form.get("action", ""),
                        form.get("excluded", ""),
                        form.get("review", ""),
                        entry=form.get("entry", ""),
                        choice=form.get("choice", ""),
                    )
                return self.dashboard._see(f"/playlists/import/{ident}")
            try:
                page = max(0, min(9999, int(request.query_params.get("page", "0"))))
            except ValueError:
                page = 0
            only_missing = request.query_params.get("show") == "missing"
            return await self.render(session, ident=ident, page=page, only_missing=only_missing)
        except PlaylistError as exc:
            # An unavailable/foreign ID must never expose another owner's evidence.
            try:
                return await self.render(session, ident=ident, error=str(exc))
            except PlaylistError:
                return await self.render(session, error=str(exc))
