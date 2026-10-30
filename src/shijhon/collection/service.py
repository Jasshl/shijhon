# ruff: noqa: S608 -- SQL fragments are fixed choices; all caller values are bound.
"""Explicit membership actions, frozen review, resumable batches and conditional Undo."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from typing import Any, cast

import anyio

from shijhon.catalog.artwork import raster_type
from shijhon.catalog.model import CatalogRef
from shijhon.collection import policy
from shijhon.collection.contracts import (
    MODES,
    Access,
    Actor,
    AssetLookup,
    CollectionError,
    IndexState,
    Mode,
    Predicate,
    Query,
    Restore,
)
from shijhon.collection.history import record_positive
from shijhon.collection.index import COLUMNS, Index
from shijhon.collection.policy import builtin_profile_id
from shijhon.collection.profiles import Profiles
from shijhon.collection.query_index import QueryIndex
from shijhon.collection.repository import (
    IMPORTED_SQL,
    WITH_KEYS,
    Repository,
    keys,
    rule_sql,
    selection_of,
)
from shijhon.store import Store


class Collection:
    def __init__(
        self,
        store: Store,
        access: Access,
        *,
        managed_folder: str = "_shijhon",
        asset_lookup: AssetLookup | None = None,
        restore: Restore | None = None,
    ) -> None:
        self.store, self.access = store, access
        self.repo = Repository(store)
        self.profiles = Profiles(store)
        self.index = Index(store, access, managed_folder, asset_lookup=asset_lookup)
        self.restore = restore
        self.query_index: QueryIndex | None = None
        self.worker = anyio.Lock()
        self.wake = anyio.Event()

    async def actor(self, owner_id: str, library_id: str = "") -> Actor:
        actor = await self.access.authorize(owner_id)
        if (
            not owner_id
            or actor.owner_id != owner_id
            or (library_id and library_id not in actor.library_ids)
        ):
            raise CollectionError("Your access to this music library could not be verified.")
        return actor

    async def profile(self, owner_id: str, profile_id: str | None = None) -> dict[str, Any]:
        await self.actor(owner_id)
        return await self.profiles.get(owner_id, profile_id)

    async def list_profiles(self, owner_id: str) -> list[dict[str, Any]]:
        await self.actor(owner_id)
        return await self.profiles.listing(owner_id)

    async def create_profile(
        self, owner_id: str, name: str, *, start: str = "original", source: str = ""
    ) -> str:
        await self.actor(owner_id)
        await self.profiles.ensure(owner_id)
        return await self.profiles.create(owner_id, name, start=start, source=source)

    async def rename_profile(self, owner_id: str, profile_id: str, name: str) -> None:
        await self.actor(owner_id)
        await self.profiles.rename(owner_id, profile_id, name)

    async def activate_profile(self, owner_id: str, profile_id: str) -> None:
        await self.actor(owner_id)
        await self.profiles.activate(owner_id, profile_id)

    async def archive_profile(
        self, owner_id: str, profile_id: str, *, archived: bool = True
    ) -> None:
        await self.actor(owner_id)
        await self.profiles.archive(owner_id, profile_id, archived=archived)

    async def set_mode(self, owner_id: str, mode: Mode, *, profile_id: str | None = None) -> None:
        await self.actor(owner_id)
        if mode not in MODES:
            raise CollectionError("Choose Original imported library, Shijhon library or All.")
        if profile_id is not None and profile_id not in ("original", "shijhon", "all"):
            raise CollectionError("Use the library picker to activate a custom library.")
        await self.activate_profile(
            owner_id, {"original": "original", "saved": "shijhon", "all": "all"}[mode]
        )

    async def index_state(self, owner_id: str, library_id: str) -> IndexState:
        await self.actor(owner_id, library_id)
        row = await self.store.fetchone(
            "SELECT * FROM collection_index WHERE library_id=?", [library_id]
        )
        return IndexState(**dict(row)) if row else IndexState(library_id, "", "empty", 0, 0, "")

    async def ready(self, owner_id: str, library_id: str) -> None:
        state = await self.index_state(owner_id, library_id)
        if not state.generation or not state.updated_at:
            raise CollectionError("Your library is still being prepared. Try again shortly.")

    async def refresh(self, owner_id: str, library_id: str) -> None:
        await self.index.scan(owner_id, library_id)

    async def predicate(
        self,
        owner_id: str,
        library_id: str,
        *,
        song_expression: str = "id",
        database: str = "main",
        mode: Mode | None = None,
        profile_id: str | None = None,
    ) -> Predicate:
        await self.ready(owner_id, library_id)
        return policy.predicate(
            owner_id,
            library_id,
            song_expression=song_expression,
            database=database,
            mode=mode,
            profile_id=profile_id,
        )

    async def visible_song_ids(
        self,
        owner_id: str,
        library_id: str,
        song_ids: Sequence[str],
        *,
        mode: Mode | None = None,
        profile_id: str | None = None,
    ) -> frozenset[str]:
        if len(song_ids) > 500:
            raise CollectionError("Check at most 500 song identities at once.")
        pred = await self.predicate(
            owner_id,
            library_id,
            song_expression="candidate.value",
            mode=mode,
            profile_id=profile_id,
        )
        rows = await self.store.fetchall(
            "SELECT candidate.value AS id FROM json_each(?) AS candidate WHERE " + pred.sql,
            [json.dumps(list(song_ids)), *pred.params],
        )
        return frozenset(row["id"] for row in rows)

    async def context_names(
        self, owner_id: str, library_id: str, *, limit: int = 100
    ) -> list[dict[str, str]]:
        """Bounded names from the active selection, without caller annotations or IDs."""
        if not 1 <= limit <= 100:
            raise CollectionError("Choose at most 100 library context songs.")
        pred = await self.predicate(owner_id, library_id, song_expression="t.song_id")
        rows = await self.store.fetchall(
            "SELECT t.artist,t.title,t.album FROM collection_tracks t"
            " WHERE t.library_id=? AND t.present=1 AND " + pred.sql + " ORDER BY t.song_id LIMIT ?",
            [library_id, *pred.params, limit],
        )
        return [dict(row) for row in rows]

    async def browse(
        self, owner_id: str, library_id: str, query: Query, *, profile_id: str | None = None
    ) -> dict[str, Any]:
        await self.ready(owner_id, library_id)
        profile = await self.profile(owner_id, profile_id)
        listing = await self.repo.browse(
            owner_id, library_id, query, profile["id"], profile["selection_id"]
        )
        for row in listing["rows"]:
            cover = row["cover_art"]
            if not cover and row["album_id"] and not row["album_id"].startswith("name:"):
                cover = "al-" + row["album_id"]
            row["artwork_id"] = (
                ("ar-" + row["artist_id"])
                if query.scope == "artist" and not row["artist_id"].startswith("name:")
                else cover
            )
        return listing

    async def artwork(self, owner_id: str, library_id: str, ident: str) -> tuple[bytes, str]:
        await self.ready(owner_id, library_id)
        if not ident or len(ident) > 300 or any(c in ident for c in ("/", "\\", ":", "?", "#")):
            raise CollectionError("Artwork is unavailable.")
        known = await self.store.fetchone(
            "SELECT 1 FROM collection_tracks WHERE library_id=? AND present=1"
            " AND (json_extract(metadata,'$.coverArt')=? OR (artist_id=? AND ?<>'')"
            " OR (album_id=? AND ?<>'')) LIMIT 1",
            [
                library_id,
                ident,
                ident[3:] if ident.startswith("ar-") else "",
                ident[3:] if ident.startswith("ar-") else "",
                ident[3:] if ident.startswith("al-") else "",
                ident[3:] if ident.startswith("al-") else "",
            ],
        )
        fetch = getattr(self.access, "artwork", None)
        if not known or not callable(fetch):
            raise CollectionError("Artwork is unavailable.")
        callback = cast(Callable[[str, str, str], Awaitable[tuple[bytes, str]]], fetch)
        with anyio.fail_after(5):
            body, mime = await callback(owner_id, library_id, ident)
        if raster_type(body) != mime or len(body) > 5 * 1024 * 1024:
            raise CollectionError("Artwork is unavailable.")
        return body, mime

    async def prepare(
        self,
        owner_id: str,
        library_id: str,
        query: Query,
        *,
        action: str,
        request_key: str,
        selected: Sequence[str] | None = None,
        profile_id: str | None = None,
    ) -> str:
        await self.ready(owner_id, library_id)
        profile = await self.profile(owner_id, profile_id)
        if action not in ("save", "remove"):
            raise CollectionError("Choose Save or Remove from Saved.")
        if not request_key or len(request_key) > 100:
            raise CollectionError("Reload the collection form and try again.")
        if selected is not None and (not selected or len(selected) > 1000):
            raise CollectionError("Select 1 to 1,000 rows, or choose every matching result.")
        selection = sorted(set(selected)) if selected is not None else None
        if selection is not None and (
            any(len(k) > 300 for k in selection) or len(json.dumps(selection).encode()) > 60000
        ):
            raise CollectionError("The row selection is too large. Choose every matching result.")
        query_json = json.dumps(asdict(query), sort_keys=True)
        selected_json = json.dumps(selection)
        fingerprint = hashlib.sha256(
            json.dumps(
                [owner_id, profile["id"], library_id, query_json, selected_json, action]
            ).encode()
        ).hexdigest()
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT id,fingerprint FROM collection_operations"
                " WHERE owner_id=? AND profile_id=? AND library_id=? AND request_key=?",
                [owner_id, profile["id"], library_id, request_key],
            )
            existing = await cursor.fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise CollectionError(
                        "This request key was already used for a different selection."
                    )
                return str(existing["id"])
            ident = secrets.token_hex(16)
            await conn.execute(
                "INSERT INTO collection_operations(id,owner_id,library_id,request_key,fingerprint,"
                "action,state,query,selected,created,profile_id,selection) VALUES "
                "(?,?,?,?,?,?,'preparing',?,?,?,?,?)",
                [
                    ident,
                    owner_id,
                    library_id,
                    request_key,
                    fingerprint,
                    action,
                    query_json,
                    selected_json,
                    time.time(),
                    profile["id"],
                    # The built-in library the change edits ('' for a custom one).
                    profile["selection_id"] if profile["builtin"] else "",
                ],
            )
        self.notify()
        return ident

    async def operation(self, owner_id: str, ident: str) -> dict[str, Any]:
        await self.actor(owner_id)
        op = await self.repo.operation(owner_id, ident)
        if not op:
            raise CollectionError("This collection change is unavailable.")
        await self.actor(owner_id, op["library_id"])
        await self.profile(owner_id, op["profile_id"])
        return op

    async def approve(self, owner_id: str, ident: str) -> None:
        op = await self.operation(owner_id, ident)
        if op["state"] == "review":
            await self.store.execute(
                "UPDATE collection_operations SET "
                "state='queued',approved_at=? WHERE id=? AND state='review'",
                [time.time(), ident],
            )
        elif op["state"] not in ("queued", "applying", "done"):
            raise CollectionError("Wait for the frozen selection to be ready for review.")
        self.notify()

    async def retry(self, owner_id: str, ident: str) -> None:
        op = await self.operation(owner_id, ident)
        if op["state"] != "failed":
            return
        # A failed freeze must be retried into review; never invent consent on retry.
        state = "queued" if op["approved_at"] is not None else "preparing"
        await self.store.execute(
            "UPDATE collection_operations SET state=?,error='' WHERE id=?", [state, ident]
        )
        self.notify()

    async def discard(self, owner_id: str, ident: str) -> None:
        await self.operation(owner_id, ident)
        await self.store.execute(
            "UPDATE collection_operations SET state='discarded' WHERE id=?"
            " AND state IN ('preparing','review','failed')",
            [ident],
        )

    async def undo(self, owner_id: str, ident: str) -> str:
        source = await self.operation(owner_id, ident)
        if source["state"] != "done" or source["action"] == "undo":
            raise CollectionError("Only a completed collection change can be undone.")
        key = "undo:" + ident
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT id FROM collection_operations WHERE owner_id=? "
                "AND profile_id=? AND library_id=? AND request_key=?",
                [owner_id, source["profile_id"], source["library_id"], key],
            )
            old = await cursor.fetchone()
            if old:
                return str(old["id"])
            new = secrets.token_hex(16)
            await conn.execute(
                "INSERT INTO collection_operations(id,owner_id,library_id,request_key,fingerprint,"
                "action,state,query,selected,source,total,affected,created,profile_id,approved_at,"
                "selection) VALUES (?,?,?,?,?,'undo','queued','{}','[]',?,?,?,?,?,?,?)",
                [
                    new,
                    owner_id,
                    source["library_id"],
                    key,
                    key,
                    ident,
                    source["total"],
                    source["changed"],
                    time.time(),
                    source["profile_id"],
                    time.time(),
                    source["selection"],
                ],
            )
            await conn.execute(
                "INSERT INTO collection_operation_items(operation_id,track_key,before_saved,"
                "before_revision,after_revision) SELECT ?,track_key,before_saved,before_revision,"
                "after_revision FROM collection_operation_items WHERE operation_id=?",
                [new, ident],
            )
        self.notify()
        return new

    def notify(self) -> None:
        self.wake.set()

    async def run(self) -> None:
        while True:
            self.wake = anyio.Event()
            await self.tick()
            with anyio.move_on_after(5):
                await self.wake.wait()

    async def tick(self) -> None:
        async with self.worker:
            pending = await self.store.fetchall(
                "SELECT * FROM collection_operations"
                " WHERE state IN ('preparing','queued','applying')"
                " ORDER BY created LIMIT 20"
            )
            for row in pending:
                op = dict(row)
                try:
                    await self.actor(op["owner_id"], op["library_id"])
                    await self.profile(op["owner_id"], op["profile_id"])
                    if op["state"] == "preparing":
                        await self.repo.freeze(op)
                    else:
                        await self._batch(op)
                except Exception:
                    # Upstream exceptions may contain credentials. Store only a fixed message.
                    await self.store.execute(
                        "UPDATE collection_operations SET state='failed',"
                        "error='The change stopped. Verify library access and retry.' WHERE id=?",
                        [op["id"]],
                    )

    async def _batch(self, op: dict[str, Any]) -> None:
        if op["action"] == "save" and self.restore is not None:
            await self._restore_batch(op)
        async with self.store.transaction() as conn:
            # A concurrent discard/approval never gets lost behind a stale worker snapshot.
            cursor = await conn.execute(
                "SELECT state FROM collection_operations WHERE id=?", [op["id"]]
            )
            state = await cursor.fetchone()
            if not state or state["state"] not in ("queued", "applying"):
                return
            if op["action"] == "undo":
                await self._undo_batch(conn, op)
                self.notify()
                return
            catalog_only = json.loads(op["query"]).get("origin") == "catalog"
            selection = selection_of(op["owner_id"], op["profile_id"], op["selection"])
            cursor = await conn.execute(
                WITH_KEYS + "SELECT i.*,m.revision AS revision,m.last_operation AS last_operation,"
                f"(t.present=1 AND {rule_sql(selection)}) AS effective,"
                "t.present AS present,"
                f"(lp.library_id IS NOT NULL AND (?=0 OR NOT {IMPORTED_SQL})) AS editable"
                " FROM collection_operation_items i JOIN collection_profiles p"
                " ON p.id=? AND p.owner_id=? AND p.archived=0"
                " LEFT JOIN collection_membership m ON"
                " m.owner_id=p.owner_id AND m.profile_id=p.id AND m.library_id=?"
                " AND m.track_key=i.track_key"
                " LEFT JOIN collection_tracks t ON t.library_id=? AND t.track_key=i.track_key"
                " LEFT JOIN collection_library_policy lp ON lp.library_id=t.library_id"
                " LEFT JOIN collection_originals o ON o.owner_id=p.owner_id"
                " AND o.library_id=t.library_id AND o.track_key=t.track_key"
                + " WHERE i.operation_id=? AND i.processed=0 ORDER BY i.track_key LIMIT 250",
                [
                    *keys(op["owner_id"], op["library_id"], op["profile_id"]),
                    int(catalog_only),
                    op["profile_id"],
                    op["owner_id"],
                    op["library_id"],
                    op["library_id"],
                    op["id"],
                ],
            )
            rows = list(await cursor.fetchall())
            changed, skipped = 0, 0
            for item in rows:
                revision = item["revision"] or 0
                allowed = (
                    revision == item["before_revision"]
                    and (op["action"] == "remove" or bool(item["present"]))
                    and bool(item["editable"])
                )
                if allowed:
                    value = int(op["action"] == "save")
                    await conn.execute(
                        "INSERT INTO "
                        "collection_membership(owner_id,profile_id,library_id,track_key,saved,"
                        "revision,last_operation) VALUES (?,?,?,?,?,?,?)"
                        " ON CONFLICT(owner_id,profile_id,library_id,"
                        "track_key) DO UPDATE SET saved=excluded.saved,revision=excluded.revision,"
                        "last_operation=excluded.last_operation",
                        [
                            op["owner_id"],
                            op["profile_id"],
                            op["library_id"],
                            item["track_key"],
                            value,
                            revision + 1,
                            op["id"],
                        ],
                    )
                    if value == 1:
                        await record_positive(
                            conn, op["owner_id"], op["library_id"], item["track_key"]
                        )
                    changed += int(item["effective"] != value)
                    await conn.execute(
                        "UPDATE collection_operation_items SET after_revision=?"
                        " WHERE operation_id=? AND track_key=?",
                        [revision + 1, op["id"], item["track_key"]],
                    )
                else:
                    skipped += 1
                await conn.execute(
                    "UPDATE collection_operation_items SET processed=1"
                    " WHERE operation_id=? AND track_key=?",
                    [op["id"], item["track_key"]],
                )
            await conn.execute(
                "UPDATE collection_operations SET processed=processed+?,changed=changed+?,"
                "skipped=skipped+?,state=CASE WHEN processed+?>=total"
                " THEN 'done' ELSE 'applying' END"
                " WHERE id=?",
                [len(rows), changed, skipped, len(rows), op["id"]],
            )
        self.notify()

    async def _undo_batch(self, conn: Any, op: dict[str, Any]) -> None:
        """Undo a change where its choices are now: every choice it made last, at the
        revision it made, goes back to what it was before. For a built-in library that is
        any of the account's built-in profiles (a change made before each had a profile of
        its own may have choices in Shijhon library's and All's); newer choices are skipped."""
        owner = op["owner_id"]
        profiles = (
            [builtin_profile_id(owner, s) for s in ("all", "original", "shijhon")]
            if selection_of(owner, op["profile_id"], op["selection"])
            else [op["profile_id"]]
        )
        cursor = await conn.execute(
            "SELECT * FROM collection_operation_items WHERE operation_id=? AND processed=0"
            " ORDER BY track_key LIMIT 250",
            [op["id"]],
        )
        rows = list(await cursor.fetchall())
        changed = 0
        for item in rows:
            done = 0
            if item["after_revision"] is not None:
                cursor = await conn.execute(
                    "UPDATE collection_membership SET saved=?,revision=revision+1,"
                    "last_operation=? WHERE owner_id=? AND library_id=? AND track_key=?"
                    " AND last_operation=? AND revision=? AND profile_id IN"
                    " (SELECT value FROM json_each(?))",
                    [
                        item["before_saved"],
                        op["id"],
                        owner,
                        op["library_id"],
                        item["track_key"],
                        op["source"],
                        item["after_revision"],
                        json.dumps(profiles),
                    ],
                )
                done = int(cursor.rowcount > 0)
            changed += done
            await conn.execute(
                "UPDATE collection_operation_items SET processed=1"
                " WHERE operation_id=? AND track_key=?",
                [op["id"], item["track_key"]],
            )
        await conn.execute(
            "UPDATE collection_operations SET processed=processed+?,changed=changed+?,"
            "skipped=skipped+?,state=CASE WHEN processed+?>=total"
            " THEN 'done' ELSE 'applying' END WHERE id=?",
            [len(rows), changed, len(rows) - changed, len(rows), op["id"]],
        )

    async def _restore_batch(self, op: dict[str, Any]) -> None:
        """Augment the last complete snapshot only after approved, verified restoration."""
        assert self.restore is not None
        async with self.index.lock:
            # Match the worker's next bounded page, including revision and current
            # profile checks. Stale/newer intent must not trigger a library write.
            pending_sql = (
                "SELECT i.track_key,i.before_saved,i.before_revision,i.after_revision,a.alias"
                " FROM (SELECT * FROM collection_operation_items"
                " WHERE operation_id=? AND processed=0 ORDER BY track_key LIMIT 250) i"
                " JOIN collection_operations op ON op.id=i.operation_id"
                " JOIN collection_profiles p ON p.id=op.profile_id"
                " AND p.owner_id=op.owner_id AND p.archived=0"
                " JOIN collection_tracks t ON t.library_id=op.library_id"
                " AND t.track_key=i.track_key AND t.present=0 AND t.original=0"
                " JOIN collection_library_policy lp ON lp.library_id=t.library_id"
                " LEFT JOIN collection_membership m ON m.owner_id=op.owner_id"
                " AND m.profile_id=op.profile_id AND m.library_id=op.library_id"
                " AND m.track_key=i.track_key"
                " JOIN collection_history h ON h.owner_id=op.owner_id"
                " AND h.library_id=t.library_id AND h.track_key=t.track_key"
                " JOIN collection_track_aliases a ON a.library_id=t.library_id"
                " AND a.track_key=t.track_key AND a.alias='catalog:' ||"
                " json_extract(h.catalog_data,'$.ref.catalog') || ':' ||"
                " json_extract(h.catalog_data,'$.ref.id')"
                " WHERE op.state IN ('queued','applying') AND op.approved_at IS NOT NULL"
                " AND op.action='save' AND op.owner_id=? AND op.profile_id=?"
                " AND op.library_id=? AND op.query=? AND op.fingerprint=?"
                " AND COALESCE(m.revision,0)=i.before_revision ORDER BY i.track_key,a.alias"
            )
            params = [
                op["id"],
                op["owner_id"],
                op["profile_id"],
                op["library_id"],
                op["query"],
                op["fingerprint"],
            ]
            rows = await self.store.fetchall(pending_sql, params)
            if not rows:
                return
            frozen = [tuple(row) for row in rows]
            expected = {str(row["alias"])[8:]: str(row["track_key"]) for row in rows}
            actor = await self.actor(op["owner_id"], op["library_id"])

            async def authorize() -> None:
                await self.actor(op["owner_id"], op["library_id"])
                current = await self.store.fetchall(pending_sql, params)
                if [tuple(row) for row in current] != frozen:
                    raise CollectionError("This approved collection selection changed. Retry it.")

            await authorize()
            restored = await self.restore(
                actor,
                op["library_id"],
                [CatalogRef.parse(ref) for ref in expected],
                authorize=authorize,
            )
            await authorize()
            if len(restored) > len(expected) or any(ref not in expected for ref in restored):
                raise CollectionError("The restored collection identities are inconsistent.")
            if not restored:
                return
            state = await self.store.fetchone(
                "SELECT generation FROM collection_index WHERE library_id=?", [op["library_id"]]
            )
            generation = str(state["generation"]) if state else ""
            if not generation:
                raise CollectionError("Refresh your music before restoring this selection.")
            tracks, aliases = await self.index._rows(
                op["library_id"], generation, list(restored.values())
            )
            if any(
                track[1] != expected[ref]
                or (op["library_id"], generation, "catalog:" + ref, expected[ref]) not in aliases
                for ref, track in zip(restored, tracks, strict=True)
            ):
                raise CollectionError("The restored collection identities are inconsistent.")
            async with self.store.transaction() as conn:
                for library, _, alias, key in aliases:
                    cursor = await conn.execute(
                        "SELECT track_key FROM collection_track_aliases"
                        " WHERE library_id=? AND alias=?",
                        [library, alias],
                    )
                    old = await cursor.fetchone()
                    if old and old["track_key"] != key:
                        raise CollectionError(
                            "The restored collection identities are inconsistent."
                        )
                await conn.executemany(
                    f"INSERT INTO collection_tracks({COLUMNS})"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(library_id,track_key) DO UPDATE SET "
                    + ",".join(
                        f"{column}=excluded.{column}"
                        for column in COLUMNS.split(",")
                        if column not in ("library_id", "track_key")
                    ),
                    tracks,
                )
                await conn.executemany(
                    "INSERT OR IGNORE INTO collection_track_aliases VALUES (?,?,?)",
                    [(library, alias, key) for library, _, alias, key in aliases],
                )
                # Preserve the complete source generation. Cached query facts reconcile
                # this durable addition before their next read, also after a restart.
                await conn.execute(
                    "INSERT INTO collection_index_additions SELECT ?,?,COALESCE((SELECT"
                    " max(revision) FROM collection_index_additions WHERE library_id=?"
                    " AND generation=?),0)+1,value FROM json_each(?)",
                    [
                        op["library_id"],
                        generation,
                        op["library_id"],
                        generation,
                        json.dumps([str(track[2]) for track in tracks]),
                    ],
                )
                await conn.execute(
                    "UPDATE collection_index SET state='ready',updated_at=?,"
                    "indexed=(SELECT count(*) FROM collection_tracks"
                    " WHERE library_id=? AND present=1),error='' WHERE library_id=?",
                    [time.time(), op["library_id"], op["library_id"]],
                )
            if self.query_index is not None:
                await self.query_index.extend(op["library_id"], generation)
