"""Faithful ordered imports; human review authorizes the existing native publisher."""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from shijhon.catalog.base import CatalogError
from shijhon.collection.contracts import Access, CollectionError
from shijhon.playlists.import_completion import (
    CHANGED,
    align,
    merge,
    missing_entries,
    published_entries,
    staged,
)
from shijhon.playlists.import_formats import MAX_BYTES, MAX_ROWS, Entry, local_path, parse
from shijhon.playlists.model import Connection, Definition, PlaylistError, Track, fingerprint
from shijhon.playlists.requests import digest
from shijhon.playlists.resolve import listed, same_recording
from shijhon.views.ids import CatalogId

if TYPE_CHECKING:
    from shijhon.catalog.model import CatalogRelease, CatalogTrack
    from shijhon.playlists.service import Playlists

BATCH_ROWS = 100
PATH_PAGE = 500
MAX_CHOICES = 50


class Imports:
    def __init__(self, service: Playlists) -> None:
        self.service = service
        self.store = service.repo.store
        self.library_reader: Access | None = None

    @staticmethod
    def review_key(data: dict[str, Any]) -> str:
        return digest(
            json.dumps(
                {
                    k: data.get(k)
                    for k in ("rows", "selected", "releases", "definition", "completion")
                },
                sort_keys=True,
            )
        )

    async def evidence(self, owner: str, ident: str) -> dict[str, Any]:
        row = await self.store.fetchone(
            "SELECT * FROM playlist_imports WHERE id=? AND owner=?", [ident, owner]
        )
        if not row or row["library"] != self.service.requests.library:
            raise PlaylistError("This import is unavailable for this account and library.")
        return dict(row)

    async def publication(self, ident: str) -> dict[str, Any] | None:
        row = await self.store.fetchone(
            "SELECT * FROM playlist_runs WHERE mix=? AND state='published'"
            " ORDER BY created DESC LIMIT 1",
            [ident],
        )
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    async def recheck(self, mix: dict[str, Any], replaces: str = "") -> None:
        """Start a new review from saved input and the playlist as it is now.

        Entries the owner kept keep their native recordings; entries the owner removed
        stay removed; only entries never published are matched again.
        """
        from shijhon.playlists.import_publish import entries, metadata

        connection = await self.guard(mix)
        previous = await self.publication(mix["id"])
        if not previous or not previous["data"].get("import_review", {}).get("excluded"):
            raise PlaylistError("This import has no missing entries to recheck.")
        saved = previous["data"]
        try:
            playlist = await self.service.client.playlist(connection, mix["playlist_id"])
            if str(playlist.get("id")) != mix["playlist_id"]:
                raise PlaylistError("")
            current = entries(playlist)
        except PlaylistError:
            raise PlaylistError(
                "The imported playlist could not be read from your Navidrome account. "
                "If you deleted it, start a new import."
            ) from None
        await self.leftovers(mix, current)
        published, published_rows = published_entries(saved)
        current_rows = align(published, published_rows, current)
        kept = {n: current[j] for j, n in enumerate(current_rows) if n is not None}
        before = {n: published[j] for j, n in enumerate(published_rows) if n is not None}
        missing = saved["import_review"]["excluded"]
        if set(before) != {
            i + 1 for i, r in enumerate(saved["rows"]) if r["state"] == "matched"
        } or set(before) & set(missing):
            raise PlaylistError("The published recording positions could not be verified.")
        active = await self.store.fetchone(
            "SELECT count(*) AS n FROM playlist_runs r JOIN playlist_mixes m ON m.id=r.mix"
            " WHERE m.owner=? AND r.state IN ('queued','generating','publishing')",
            [mix["owner"]],
        )
        if active and active["n"] >= 10:
            raise PlaylistError("Too many playlist requests are running. Try again later.")
        rows = []
        for number, row in enumerate(saved["rows"], 1):
            row = dict(row)
            if number in kept or number in before:
                # Published music now has native identity, independent of an old catalog.
                row["track"] = (
                    Track.model_validate(row["track"])
                    .model_copy(update={"id": kept.get(number, before[number]), "release": ""})
                    .model_dump()
                )
            if number in before and number not in kept:
                row.update(state="removed", note="You removed this song in Navidrome.")
            rows.append(row)
        snapshot: list[dict[str, Any]] = [
            {"id": song_id, "row": n}
            if n is not None
            else {
                "id": song_id,
                "artist": str(entry.get("artist", "")),
                "title": str(entry.get("title", "")),
            }
            for song_id, n, entry in zip(
                current, current_rows, playlist.get("entry", []), strict=True
            )
        ]
        spec = Definition.model_validate_json(mix["definition"]).model_copy(
            update={"count": len(rows)}
        )
        data = {
            "rows": rows,
            "skipped": len(missing),
            "attempt": max((r.get("attempt", 0) for r in rows), default=0) + 1,
            "publish_after_generation": False,
            "completion": {
                "playlist_id": mix["playlist_id"],
                "published": mix["published"],
                "baseline": fingerprint(playlist),
                "metadata": metadata(playlist),
                "source_run": previous["id"],
                "missing_entries": missing,
                "snapshot": snapshot,
            },
        }
        async with self.store.transaction() as db:
            if replaces:
                await db.execute(
                    "UPDATE playlist_runs SET state='discarded' WHERE id=?"
                    " AND state IN ('preview','failed')",
                    [replaces],
                )
            await db.execute(
                "UPDATE playlist_mixes SET definition=? WHERE id=?",
                [spec.model_dump_json(), mix["id"]],
            )
            await db.execute(
                "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,'queued',?)",
                [secrets.token_hex(12), mix["id"], time.time(), json.dumps(data)],
            )

    @staticmethod
    def unfinished(state: str, data: dict[str, Any]) -> bool:
        """A large-list update that stopped or failed after it began writing: the playlist
        can hold its temporary copies, mixed with any later edits of the owner's."""
        progress = data.get("import_publish", {})
        return (
            state in ("discarded", "failed", "uncertain")
            and "prefix" in progress
            and "snapshot" in data.get("completion", {})
            and not data.get("kept_as_is")
            and not progress.get("written")  # every write done: no copies are left
            and bool(
                progress["appended"] > progress["prefix"]
                or progress["removed"]
                or progress.get("pending")
            )
        )

    async def leftovers(self, mix: dict[str, Any], current: list[str]) -> None:
        """Refuse while an unfinished large-list update can have left temporary copies.

        Copies cannot be told from the owner's own songs once the owner edited the
        playlist too, so only the playlist as reviewed or as finished passes; otherwise
        the owner finishes the update or explicitly keeps the playlist as it is."""
        latest = (await self.service.repo.runs(mix["id"]))[0]
        data = latest["data"]
        if not self.unfinished(latest["state"], data):
            return
        original = [s["id"] for s in data["completion"]["snapshot"]]
        if current in (original, data.get("playlist_ids", [])):
            return
        raise PlaylistError(
            "An unfinished update of this playlist can have left extra copies. Resume or "
            "retry it. If you edited the playlist since, remove any extra copies in "
            "Navidrome, choose Keep playlist as it is, then Recheck."
        )

    async def guard(self, mix: dict[str, Any]) -> Connection:
        connection = await self.service.connection(mix["owner"])
        # Per-recording guards need only the binding, not the entire original input.
        row = await self.store.fetchone(
            "SELECT id FROM playlist_imports WHERE id=? AND owner=? AND library=?",
            [mix["id"], mix["owner"], self.service.requests.library],
        )
        if not row:
            raise PlaylistError("This import is unavailable for this account and library.")
        return connection

    async def create(
        self,
        owner: str,
        key: str,
        name: str,
        source: str,
        *,
        csv_file: bool = False,
        format: str = "auto",
        filename: str = "",
        mapping: dict[str, str] | None = None,
    ) -> str:
        """Called under changes; enqueue only, with no catalog or library mutation."""
        await self.service.connection(owner)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise PlaylistError("Reload the import form before submitting.")
        rows = parse(
            source,
            csv_file=csv_file,
            format=format,
            filename=filename,
            mapping=mapping,
            max_bytes=getattr(self.service.settings, "import_max_bytes", MAX_BYTES),
            max_rows=getattr(self.service.settings, "import_max_entries", MAX_ROWS),
        )
        definition = Definition(
            name=name,
            mode="import",
            count=len(rows),
            cadence="manual",
            unfamiliar_percent=0,
            review_before_publishing=True,
        )
        # Keep original text as well as normalized ordered rows, including repeats.
        original = json.dumps(
            {
                "name": name,
                "format": "csv" if csv_file else format,
                "filename": filename,
                "mapping": mapping or {},
                "source": source,
                "rows": [s.model_dump() for s in rows],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        old = await self.store.fetchone(
            "SELECT * FROM playlist_imports WHERE owner=? AND request_key=?", [owner, key]
        )
        if old:
            if old["library"] != self.service.requests.library or old["input_hash"] != digest(
                original
            ):
                raise PlaylistError("This import key was already used with different input.")
            return str(old["id"])
        active = await self.store.fetchone(
            "SELECT count(*) AS n FROM playlist_runs r JOIN playlist_mixes m ON m.id=r.mix"
            " WHERE m.owner=? AND r.state IN ('queued','generating','publishing')",
            [owner],
        )
        if active and active["n"] >= 10:
            raise PlaylistError("Too many playlist requests are running. Try again later.")
        ident, run = secrets.token_hex(12), secrets.token_hex(12)
        async with self.store.transaction() as db:
            await db.execute(
                "INSERT INTO playlist_mixes(id,owner,definition) VALUES (?,?,?)",
                [ident, owner, definition.model_dump_json()],
            )
            await db.execute(
                "INSERT INTO playlist_imports(id,owner,library,request_key,input_hash,input)"
                " VALUES (?,?,?,?,?,?)",
                [ident, owner, self.service.requests.library, key, digest(original), original],
            )
            await db.execute(
                "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,'queued',?)",
                [
                    run,
                    ident,
                    time.time(),
                    json.dumps({"rows": [], "publish_after_generation": False}),
                ],
            )
        self.service.notify()
        return ident

    async def checkpoint(self, ident: str, state: str, data: dict[str, Any]) -> bool:
        # A stop/disconnect while a read is in flight must never be overwritten.
        async with self.store.transaction() as db:
            cursor = await db.execute(
                "UPDATE playlist_runs SET data=?,error='' WHERE id=? AND state=?",
                [json.dumps(data), ident, state],
            )
            return cursor.rowcount == 1

    async def requeue(self, ident: str) -> None:
        async with self.service.changes:
            await self.store.execute(
                "UPDATE playlist_runs SET state='queued' WHERE id=? AND state='generating'", [ident]
            )
        self.service.notify()

    async def running(
        self,
        mix: dict[str, Any],
        ident: str,
        state: str,
        connection: Connection | None = None,
    ) -> None:
        current_connection = await self.guard(mix)
        if connection is not None and current_connection != connection:
            raise PlaylistError("The connection changed. Retry with the current connection.")
        current = await self.store.fetchone("SELECT state FROM playlist_runs WHERE id=?", [ident])
        if not current or current["state"] != state:
            raise PlaylistError("This import has stopped. Published playlist entries are kept.")

    async def paths(
        self,
        connection: Connection,
        mix: dict[str, Any],
        ident: str,
        data: dict[str, Any],
        inputs: list[Entry],
    ) -> bool:
        """Scan bounded owner-authenticated metadata pages, never server/client files."""
        paths = {
            local_path(v) for entry in inputs if not entry.native_id for v in entry.locations
        } - {""}
        if not paths:
            return True
        scan = data.setdefault("path_scan", {"offset": 0, "matches": {}, "complete": False})
        if scan["complete"]:
            return True
        lookup: dict[str, list[tuple[str, int, str]]] = {}
        for path in paths:
            parts = path.split("/")
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                lookup.setdefault(suffix.casefold(), []).append((path, len(parts) - i, suffix))
        await self.running(mix, ident, "generating", connection)
        reader = self.library_reader
        if reader is None:
            raise PlaylistError(
                "Sign in again to enable native file-path matching, then retry this import."
            )
        try:
            actor = await reader.authorize(connection.user_id)
            if (
                actor.owner_id != connection.user_id
                or self.service.requests.library not in actor.library_ids
            ):
                raise CollectionError("Your access to this library could not be verified.")
            await self.running(mix, ident, "generating", connection)
            songs = await reader.songs(
                connection.user_id, self.service.requests.library, scan["offset"], PATH_PAGE
            )
        except CollectionError as exc:
            raise PlaylistError(str(exc)) from None
        if len(songs) > PATH_PAGE:
            raise PlaylistError("The native path listing returned an invalid page.")
        page_key = digest(json.dumps([s.get("id") for s in songs]))
        if songs and scan.get("last_page") == page_key:
            raise PlaylistError("The native path listing repeated a page. Retry matching.")
        for song in songs:
            song_id = str(song.get("id", ""))
            if not song_id or song_id <= scan.get("last_id", ""):
                raise PlaylistError(
                    "The native path listing changed. Retry matching from its saved input."
                )
            scan["last_id"] = song_id
            path = local_path(str(song.get("path", "")))
            if not path or not song.get("id"):
                continue
            parts = path.split("/")
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                for wanted, count, exact in lookup.get(suffix.casefold(), []):
                    rank = [count, int(suffix == exact)]
                    found = scan["matches"].setdefault(wanted, {"rank": rank, "songs": []})
                    if rank < found["rank"]:
                        continue
                    if rank > found["rank"]:
                        found = scan["matches"][wanted] = {"rank": rank, "songs": []}
                    if any(s["id"] == song["id"] for s in found["songs"]):
                        continue
                    if len(found["songs"]) >= MAX_CHOICES:
                        found["overflow"] = True
                    else:
                        found["songs"].append(
                            {
                                "id": str(song["id"]),
                                "path": str(song["path"]),
                                "strong": count == len(parts) or count == len(wanted.split("/")),
                            }
                        )
        scan.update(
            offset=scan["offset"] + len(songs), last_page=page_key, complete=len(songs) < PATH_PAGE
        )
        await self.running(mix, ident, "generating", connection)
        if not await self.checkpoint(ident, "generating", data):
            return False
        return bool(scan["complete"])

    async def recording(
        self, connection: Connection, entry: Entry, data: dict[str, Any]
    ) -> dict[str, Any]:
        choices: list[dict[str, Any]] = []
        if entry.native_id:
            try:
                track = await self.service.native_track(
                    connection, entry.native_id, library_bound=True
                )
            except PlaylistError:
                return {"state": "unresolved", "track": None, "note": "Native ID is unavailable."}
            return {"state": "matched", "track": track.model_dump(), "note": "Native song ID"}
        matches: dict[str, dict[str, Any]] = {}
        overflow = False
        for value in entry.locations:
            group = data.get("path_scan", {}).get("matches", {}).get(local_path(value), {})
            overflow = overflow or group.get("overflow", False)
            for match in group.get("songs", []):
                matches[match["id"]] = match
        overflow = overflow or len(matches) > MAX_CHOICES
        if matches:
            # Every offered choice is checked as the caller and exact installation library.
            for match in list(matches.values())[:MAX_CHOICES]:
                try:
                    track = await self.service.native_track(
                        connection, match["id"], library_bound=True
                    )
                except PlaylistError:
                    continue
                choices.append({"track": track.model_dump(), "path": match["path"]})
            if len(choices) == 1 and not overflow and matches[choices[0]["track"]["id"]]["strong"]:
                return {"state": "matched", **choices[0], "note": "Native file path"}
            return {
                "state": "ambiguous",
                "track": None,
                "choices": choices,
                "overflow": overflow,
                "note": "Choose a native recording; shortened paths may match several files.",
            }
        suggestion = entry.suggestion()
        # File paths are an explicit native identity request. Do not silently substitute
        # music by title when the requested file was not found. Remote addresses are
        # only metadata hints and are never fetched.
        if not suggestion or any(local_path(v) for v in entry.locations):
            return {
                "state": "unresolved",
                "track": None,
                "note": "No accessible native file matched. Choose a recording by native ID.",
            }
        wanted = suggestion.wanted()
        ambiguous: list[Track] = []
        # Add-ons that did not answer are skipped for the rest of this matching attempt.
        unanswered = set(data.get("unanswered", []))
        unreadable = set(data.get("unreadable", []))
        found = await self.service.resolver.resolve(
            connection,
            wanted,
            ambiguous=ambiguous,
            addons=True,
            unanswered=unanswered,
            unreadable=unreadable,
        )
        if unanswered:
            data["unanswered"] = sorted(unanswered)
        if unreadable:
            data["unreadable"] = sorted(unreadable)
        if found:
            found = await self.service.matched_edition(found)
            if found and not same_recording(wanted, found):
                found = None
            if found:
                found = found.model_copy(
                    update={
                        "required_title": wanted["required_title"],
                        "required_album": wanted["required_album"],
                    }
                )
        # Where a recording of an add-on's catalog was found is said.
        addon = self.addon(found.id if found else ambiguous[0].id if ambiguous else "")
        if found:
            note = (
                f"Found in the catalog of add-on {addon}"
                if addon
                else "Found in the catalog"
                if CatalogId.parse(found.id)
                else "Found in your library"
            )
            if entry.locations:
                note += "; locations are not fetched."
        elif ambiguous:
            note = "Several recordings match"
            note += f" in the catalog of add-on {addon}." if addon else "."
            note += " Choose the requested recording."
        elif unanswered:
            note = (
                f"Not found yet: add-on {', '.join(sorted(unanswered))} did not answer. "
                "Recheck later."
            )
        else:
            note = "No matching recording found in your library or the catalogs."
        return {
            "state": "matched" if found else "ambiguous" if ambiguous else "unresolved",
            "track": found.model_dump() if found else None,
            "choices": [{"track": s.model_dump(), "path": ""} for s in ambiguous[:MAX_CHOICES]],
            "overflow": len(ambiguous) > MAX_CHOICES,
            "note": note,
        }

    def addon(self, ident: str) -> str | None:
        """The name of the add-on whose catalog the recording ``ident`` is of; None for
        the installation's catalog and the library."""
        cid = CatalogId.parse(ident)
        addons, catalog = self.service.addons, self.service.catalog
        if cid is None or addons is None:
            return None
        if catalog is not None and catalog.key == cid.ref.catalog:
            return None
        return addons.name(cid.ref.catalog)

    async def resolve(
        self, connection: Connection, mix: dict[str, Any], ident: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        evidence = await self.evidence(connection.user_id, mix["id"])
        inputs = [Entry.model_validate(s) for s in json.loads(evidence["input"])["rows"]]
        if not await self.paths(connection, mix, ident, data, inputs):
            return None
        rows = data.setdefault("rows", [])
        attempt = data.get("attempt", 0)
        cache = {
            entry.model_dump_json(): result
            for entry, result in zip(inputs, rows, strict=False)
            if result["state"] == "matched"
            or (result["state"] != "removed" and result.get("attempt", 0) == attempt)
        }
        start = data.get("cursor", 0)
        # Slow catalogs (an add-on's) can take long for each entry: a batch stops in time
        # to save what it matched, and the next one goes on from there.
        deadline = time.monotonic() + self.service.settings.run_seconds / 2
        try:
            for index in range(start, min(start + BATCH_ROWS, len(inputs))):
                if index > start and time.monotonic() > deadline:
                    break
                entry = inputs[index]
                if index < len(rows) and rows[index]["state"] in ("matched", "removed"):
                    data["cursor"] = index + 1
                    continue
                key = entry.model_dump_json()
                if key in cache:
                    result = {**cache[key], "attempt": attempt}
                else:
                    # Fresh authorization/Stop checks remain per recording; persisting
                    # the growing JSON here would rewrite it once per distinct input.
                    await self.running(mix, ident, "generating", connection)
                    result = {**await self.recording(connection, entry, data), "attempt": attempt}
                    cache[key] = result
                if index < len(rows):
                    rows[index] = result
                else:
                    rows.append(result)
                data["cursor"] = index + 1
        except Exception:
            # Preserve this batch's completed matches on an ordinary matching failure.
            # A concurrent Stop/disconnect must win over this partial checkpoint too.
            await self.running(mix, ident, "generating", connection)
            await self.checkpoint(ident, "generating", data)
            raise
        await self.running(mix, ident, "generating", connection)
        if not await self.checkpoint(ident, "generating", data):
            return None
        if data.get("cursor", 0) < len(inputs):
            return None
        selected = [Track.model_validate(r["track"]) for r in rows if r["state"] == "matched"]
        previous = {r["ref"]: r for r in data.get("releases", [])}
        previous.update({r["ref"]: r for r in data.get("import_releases", [])})
        pending = sorted({s.release for s in selected if s.release} - previous.keys())
        if pending:
            batch = set(pending[:20])
            await self.running(mix, ident, "generating", connection)
            extra = await self.service.freeze(
                Definition.model_validate_json(mix["definition"]),
                [s for s in selected if s.release in batch],
                "",
                "",
                history_from=0,
                history_count=0,
                skipped=0,
                addons=True,
            )
            previous.update({r["ref"]: r for r in extra["releases"]})
            data["import_releases"] = list(previous.values())
            await self.running(mix, ident, "generating", connection)
            if not await self.checkpoint(ident, "generating", data):
                return None
            if len(pending) > 20:
                return None
        await self.running(mix, ident, "generating", connection)
        frozen = await self.service.freeze(
            Definition.model_validate_json(mix["definition"]),
            [],
            "",
            "",
            history_from=0,
            history_count=0,
            skipped=len(missing_entries(rows)),
        )
        frozen.update(
            selected=[s.model_dump() for s in selected],
            releases=[previous[ref] for ref in sorted({s.release for s in selected if s.release})],
        )
        if completion := data.get("completion"):
            completion["plan"] = merge(completion["snapshot"], rows, completion["missing_entries"])
            completion["staged"] = staged(completion["plan"])
        return {**data, **frozen, "rows": rows, "publish_after_generation": False}

    async def publish(
        self, connection: Connection, mix: dict[str, Any], ident: str, data: dict[str, Any]
    ) -> None:
        from shijhon.playlists.import_publish import Publisher

        await Publisher(self).step(connection, mix, ident, data)

    async def reviewed(self, mix: dict[str, Any], data: dict[str, Any]) -> None:
        evidence = await self.evidence(mix["owner"], mix["id"])
        total = len(json.loads(evidence["input"])["rows"])
        rows = data.get("rows", [])
        selected = [r["track"] for r in rows if r["state"] == "matched"]
        excluded = missing_entries(rows)
        if (
            len(rows) != total
            or not selected
            or data.get("selected") != selected
            or data.get("import_review") != {"total": total, "excluded": excluded}
        ):
            raise PlaylistError("Review the complete import and its exclusions before publishing.")
        if (completion := data.get("completion")) and (
            completion["playlist_id"] != mix["playlist_id"]
            or completion["published"] != mix["published"]
        ):
            raise PlaylistError("This review is out of date. Press Recheck to review again.")
        if completion:
            # Entries already in the playlist keep their recordings; only missing
            # entries can change, and the plan must be the one shown for review.
            missing = set(completion["missing_entries"])
            kept = {s["row"]: s["id"] for s in completion["snapshot"] if "row" in s}
            fixed = [
                (r["state"] == "matched", r["track"]["id"] if r["state"] == "matched" else "")
                for i, r in enumerate(rows, 1)
                if i not in missing
            ]
            if fixed != [
                (i in kept, kept.get(i, "")) for i in range(1, len(rows) + 1) if i not in missing
            ] or completion.get("plan") != merge(
                completion["snapshot"], rows, completion["missing_entries"]
            ):
                raise PlaylistError(
                    "Previously published recordings must stay fixed during completion."
                )

    async def action(
        self,
        owner: str,
        ident: str,
        run_id: str,
        action: str,
        excluded: str,
        review_key: str = "",
        *,
        entry: str = "",
        choice: str = "",
    ) -> None:
        """Owner/CSRF protection comes from the dashboard; callers hold changes."""
        mix = await self.service.repo.mix(ident, owner)
        await self.guard(mix)
        run = (await self.service.repo.runs(ident))[0]
        if run["id"] != run_id:
            raise PlaylistError("This review is no longer current. Reload the page.")
        data, state = run["data"], run["state"]
        if action == "recheck" and (
            state in ("published", "discarded")
            or (state in ("preview", "failed") and data.get("completion"))
        ):
            if review_key != self.review_key(data):
                raise PlaylistError(
                    "This import changed. Reload it before retrying missing entries."
                )
            await self.recheck(mix, run_id)
        elif (
            action == "resume"
            and state == "discarded"
            and data.get("stopped")
            and not data.get("kept_as_is")
            and data.get("completion", {}).get("staged")
            and data.get("import_review")
        ):
            if review_key != self.review_key(data):
                raise PlaylistError("This completion review changed. Reload it before resuming.")
            await self.reviewed(mix, data)
            data.pop("stopped")
            await self.service.repo.update(run_id, "publishing", data)
        elif action == "keep" and self.unfinished(state, data):
            # The owner's explicit choice: what is in the playlist now counts as theirs.
            if review_key != self.review_key(data):
                raise PlaylistError("This update changed. Reload it before keeping the playlist.")
            data.update(kept_as_is=True, stopped=True)
            await self.service.repo.update(run_id, "discarded", data)
        elif action == "stop" and state in (
            "queued",
            "generating",
            "publishing",
            "failed",
            "uncertain",
        ):
            data["stopped"] = True
            await self.service.repo.update(run_id, "discarded", data)
        elif action == "choose" and state == "preview":
            if review_key != self.review_key(data):
                raise PlaylistError("This review changed. Reload it before choosing a recording.")
            try:
                index = int(entry) - 1
                if not 0 <= index < len(data["rows"]):
                    raise ValueError
                if (
                    data.get("completion")
                    and index + 1 not in data["completion"]["missing_entries"]
                ):
                    raise PlaylistError(
                        "Previously published recordings stay fixed during completion."
                    )
                row = data["rows"][index]
                offered = next(
                    (s for s in row.get("choices", []) if s["track"]["id"] == choice), None
                )
                if offered:
                    track = Track.model_validate(offered["track"])
                    connection = await self.service.connection(owner)
                    cid = CatalogId.parse(track.id)
                    if cid:
                        catalog = await self.service.catalog_of(cid.ref.catalog, addons=True)
                        if not catalog:
                            raise PlaylistError("The catalog connection is unavailable.")
                        song: CatalogTrack | None
                        album: CatalogRelease | None
                        if catalog is self.service.catalog:
                            song = await catalog.song(cid.ref.id)
                            album = await catalog.album(song.album.id) if song.album else None
                        else:
                            # An add-on's: the song as its release lists it now.
                            answer = await listed(catalog, cid.ref, track.release)
                            if answer is None and not track.release:
                                # (A search shows its release: also after a restart.)
                                await catalog.search(f"{track.artist} {track.title}", limit=50)
                                answer = await listed(catalog, cid.ref, track.release)
                            song, album = answer if answer else (None, None)
                        if not song or not album or album.incomplete:
                            raise PlaylistError(
                                "The selected recording's complete release is unavailable."
                            )
                        fresh = Track(
                            id=track.id,
                            artist=song.artist,
                            title=song.title,
                            album=album.title,
                            duration=song.seconds,
                            isrc=song.isrc or "",
                            explicit=song.explicit,
                            clean=song.clean,
                            release=str(album.ref),
                        )
                        if not same_recording(track.model_dump(), fresh):
                            raise ValueError
                        matched = await self.service.matched_edition(fresh)
                        if matched is None or not same_recording(fresh.model_dump(), matched):
                            raise ValueError
                        track = matched
                    else:
                        native = await self.service.native_track(
                            connection, track.id, library_bound=True
                        )
                        if not same_recording(track.model_dump(), native):
                            raise ValueError
                        track = native
                    evidence = await self.evidence(owner, ident)
                    requested = Entry.model_validate(json.loads(evidence["input"])["rows"][index])
                    if not requested.locations and (suggestion := requested.suggestion()):
                        wanted = suggestion.wanted()
                        if not same_recording(wanted, track):
                            raise ValueError
                        track = track.model_copy(
                            update={
                                "required_title": wanted["required_title"],
                                "required_album": wanted["required_album"],
                            }
                        )
                else:
                    candidate = Entry(native_id=choice)
                    track = await self.service.native_track(
                        await self.service.connection(owner),
                        candidate.native_id,
                        library_bound=True,
                    )
            except CatalogError:
                raise PlaylistError(
                    "The selected catalog recording could not be verified. Retry later."
                ) from None
            except (ValueError, IndexError):
                raise PlaylistError(
                    "Choose an offered recording or enter an accessible native song ID."
                ) from None
            row.update(
                state="matched",
                track=track.model_dump(),
                note="Recording chosen explicitly",
                path=offered.get("path", "") if offered else "",
            )
            data["cursor"] = 0
            await self.service.repo.update(run_id, "queued", data)
        elif action == "publish" and state == "preview":
            if review_key != self.review_key(data):
                raise PlaylistError(
                    "This review changed. Reload and review the current recordings."
                )
            rows = data["rows"]
            omissions = missing_entries(rows)
            if excluded != str(len(omissions)):
                raise PlaylistError(
                    "Explicitly confirm the excluded entry count before publishing."
                )
            data["import_review"] = {"total": len(rows), "excluded": omissions}
            await self.reviewed(mix, data)
            if completion := data.get("completion"):
                current = await self.service.client.playlist(
                    await self.service.connection(owner), completion["playlist_id"]
                )
                if fingerprint(current) != completion["baseline"]:
                    raise PlaylistError(CHANGED)
            spec = Definition.model_validate(data["definition"]).model_copy(
                update={"count": len(data["selected"])}
            )
            data["definition"] = spec.model_dump()
            async with self.store.transaction() as db:
                await db.execute(
                    "UPDATE playlist_mixes SET definition=? WHERE id=?",
                    [spec.model_dump_json(), ident],
                )
                await db.execute(
                    "UPDATE playlist_runs SET state='publishing',data=? WHERE id=?",
                    [json.dumps(data), run_id],
                )
        elif action == "discard" and state == "preview":
            await self.service.repo.update(run_id, "discarded", data)
        elif action == "retry" and state in ("preview", "failed", "uncertain"):
            # Reviewed writes reconcile; matching retries never authorize publication.
            target = "publishing" if data.get("import_review") else "queued"
            if state == "preview" and (
                data.get("import_review") or not missing_entries(data.get("rows", []))
            ):
                raise PlaylistError("All entries are resolved. Review and publish this import.")
            if target == "queued":
                data["cursor"] = 0
                data["attempt"] = data.get("attempt", 0) + 1
                data.pop("path_scan", None)
                data.pop("unanswered", None)
                data.pop("unreadable", None)
            await self.service.repo.update(run_id, target, data)
        else:
            raise PlaylistError("This action is no longer available. Reload the page.")
        self.service.notify()
