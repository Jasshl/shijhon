"""Reviewed, reversible removal of retained imports, separate from stopping upload."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import anyio

from shijhon.collection.contracts import CollectionError
from shijhon.collection.origins import sync_link_credit
from shijhon.music_imports.model import ImportError, NativeFile
from shijhon.music_imports.removal_storage import quarantine, restore, same_file, verified
from shijhon.music_imports.service import _RELEASE_UNPLACED, MusicImports, _key
from shijhon.navidrome.usage import Usage, UsageSource, _albums, _artists, current

BATCH = 100
NativeAbsent = Callable[[str, str, str], Awaitable[bool]]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ImportRemovals:
    def __init__(
        self,
        imports: MusicImports,
        *,
        usage: UsageSource | None,
        gate: Callable[[], Awaitable[None]],
        administrator: Callable[[str], Awaitable[bool]],
        native_absent: NativeAbsent | None = None,
    ) -> None:
        self.imports, self.store = imports, imports.store
        self.usage, self.gate, self.administrator = usage, gate, administrator
        self.native_absent = native_absent
        imports.audio_authorize = self._audio_authorize
        self.wake = anyio.Event()

    async def _job(self, actor: str, job_id: str) -> dict[str, Any]:
        await self.imports._actor(actor)
        found = await self.store.fetchone(
            "SELECT * FROM music_import_jobs WHERE id=? AND library_id=?",
            [job_id, self.imports.library_id],
        )
        if not found or (found["owner_id"] != actor and not await self.administrator(actor)):
            raise ImportError("not_found", "This import is unavailable.")
        if (
            found["root_id"] != self.imports.storage.root_id
            or found["config_id"] != self.imports.config_id
        ):
            raise ImportError(
                "configuration_changed", "The retained import library or folders changed."
            )
        return dict(found)

    async def _audio_authorize(self, owner: str, job_id: str) -> None:
        try:
            await self._job(owner, job_id)
            return
        except ImportError as exc:
            if exc.code != "access":
                raise
        restored = await self.store.fetchone(
            "SELECT actor_id FROM music_import_removals WHERE job_id=? AND state='restored'",
            [job_id],
        )
        actor = restored["actor_id"] if restored and restored["actor_id"] else owner
        # A current administrator may supply access when the importer lost it.
        # Credit still belongs to the importer; either account may authorize later audio.
        await self._job(actor, job_id)

    async def listing(self, actor: str, *, offset: int = 0) -> list[dict[str, Any]]:
        await self.imports._actor(actor)
        if not await self.administrator(actor):
            raise ImportError("access", "Only administrators can manage other import batches.")
        if type(offset) is not int or not 0 <= offset <= 10_000_000:
            raise ImportError("invalid", "Choose a bounded import history page.")
        return [
            dict(r)
            for r in await self.store.fetchall(
                "SELECT j.id,j.owner_id,j.created,j.state,count(i.id) AS files,"
                "r.state AS removal_state FROM music_import_jobs j"
                " LEFT JOIN music_import_items i ON i.job_id=j.id"
                " LEFT JOIN music_import_removals r ON r.job_id=j.id"
                " WHERE j.library_id=? AND j.root_id=? GROUP BY j.id"
                " ORDER BY j.created DESC,j.id LIMIT 20 OFFSET ?",
                [self.imports.library_id, self.imports.storage.root_id, offset],
            )
        ]

    async def _authorize(self, owner: str, job_id: str, *, include_used: bool = False) -> None:
        job = await self._job(owner, job_id)
        if job["state"] not in ("done", "cancelled"):
            raise ImportError("active", "Stop this import before reviewing its removal.")
        if include_used and not await self.administrator(owner):
            raise ImportError("access", "An administrator must approve removing music in use.")
        await self.gate()
        # Native and storage checks may have waited. Never rely on the earlier grant.
        await self._job(owner, job_id)
        if include_used and not await self.administrator(owner):
            raise ImportError("access", "An administrator must approve removing music in use.")

    async def review(
        self, owner: str, job_id: str, *, include_used: bool = False
    ) -> dict[str, Any]:
        if type(include_used) is not bool:
            raise ImportError("invalid", "Choose whether used songs must stay available.")
        async with self.imports._locks.hold(job_id):
            await self._authorize(owner, job_id, include_used=include_used)
            job = await self._job(owner, job_id)
            if self.usage is None:
                raise ImportError("usage", "The administrator must enable shared-use checks first.")
            async with self.store.transaction() as conn:
                old = await (
                    await conn.execute(
                        "SELECT id,state FROM music_import_removals WHERE job_id=?", [job_id]
                    )
                ).fetchone()
                if old and old["state"] not in ("review", "restored", "stale"):
                    raise ImportError("conflict", "Finish or restore the existing removal first.")
                if old:
                    await conn.execute(
                        "DELETE FROM music_import_removal_items WHERE removal_id=?", [old["id"]]
                    )
                    await conn.execute("DELETE FROM music_import_removals WHERE id=?", [old["id"]])
                await conn.execute(
                    "INSERT INTO music_import_removals"
                    "(id,job_id,owner_id,actor_id,state,include_used,created)"
                    " VALUES (?,?,?,?,'reviewing',?,?)",
                    [
                        secrets.token_hex(16),
                        job_id,
                        job["owner_id"],
                        owner,
                        int(include_used),
                        time.time(),
                    ],
                )
        self.wake.set()
        return await self.status(owner, job_id)

    async def status(self, owner: str, job_id: str, *, offset: int = 0) -> dict[str, Any]:
        job = await self._job(owner, job_id)
        if type(offset) is not int or not 0 <= offset <= self.imports.limits.max_job_files:
            raise ImportError("invalid", "Choose a bounded removal page.")
        row = await self.store.fetchone(
            "SELECT * FROM music_import_removals WHERE job_id=?", [job_id]
        )
        if not row:
            return {"state": "none", "items": [], "import_state": job["state"]}
        result = {k: row[k] for k in ("id", "state", "include_used", "fingerprint", "error")}
        result["import_state"] = job["state"]
        counts = await self.store.fetchone(
            "SELECT count(*) AS total,sum(remove_file) AS remove_files,"
            "sum(state='removed') AS removed_files"
            " FROM music_import_removal_items WHERE removal_id=?",
            [row["id"]],
        )
        result.update(dict(counts) if counts else {})
        links = await self.store.fetchone(
            "SELECT count(*) AS total,coalesce(sum(active),0) AS active,"
            "coalesce(sum(active=0 AND audio='placed'),0) AS withdrawing"
            " FROM music_import_links WHERE job_id=?",
            [job_id],
        )
        result["linked_songs"] = links["total"] if links else 0
        result["linked_active"] = links["active"] if links else 0
        # Catalog songs that still play this import's audio, to go back to the add-ons'.
        result["audio_withdrawing"] = links["withdrawing"] if links else 0
        result["newly_indexed_files"] = 0
        if row["state"] == "restored":
            indexed = await self.store.fetchone(
                "SELECT count(*) AS total FROM music_import_removal_items r"
                " JOIN music_import_items i ON i.id=r.item_id"
                " JOIN collection_track_aliases a ON a.library_id=?"
                " AND a.alias='import:'||i.asset_id"
                " JOIN collection_tracks t ON t.library_id=a.library_id AND t.track_key=a.track_key"
                " WHERE r.removal_id=? AND r.song_id='' AND r.remove_file=1 AND t.present=1",
                [self.imports.library_id, row["id"]],
            )
            result["newly_indexed_files"] = indexed["total"] if indexed else 0
        items = await self.store.fetchall(
            "SELECT r.item_id,r.song_id,r.reasons,r.remove_file,r.state,i.display_path AS path,"
            "i.expected_size AS size,CASE WHEN ?='restored' AND r.song_id='' AND r.remove_file=1"
            " AND t.present=1 THEN t.song_id ELSE '' END AS restored_song_id"
            " FROM music_import_removal_items r"
            " JOIN music_import_items i ON i.id=r.item_id"
            " LEFT JOIN collection_track_aliases a ON a.library_id=?"
            " AND a.alias='import:'||i.asset_id"
            " LEFT JOIN collection_tracks t ON t.library_id=a.library_id"
            " AND t.track_key=a.track_key"
            " WHERE r.removal_id=?"
            " ORDER BY r.item_id LIMIT ? OFFSET ?",
            [row["state"], self.imports.library_id, row["id"], BATCH, offset],
        )
        result["items"] = [{**dict(i), "reasons": json.loads(i["reasons"])} for i in items]
        result["next_offset"] = (
            offset + len(items) if offset + len(items) < result["total"] else None
        )
        return result

    async def stop(self, actor: str, job_id: str) -> dict[str, Any]:
        """An explicit administrator can stop an orphaned import without deleting it."""
        await self._job(actor, job_id)
        async with self.store.transaction() as conn:
            await conn.execute(
                "UPDATE music_import_jobs SET state='cancelled',error=''"
                " WHERE id=? AND state<>'done'",
                [job_id],
            )
            await conn.execute(_RELEASE_UNPLACED, [job_id])
        return await self.status(actor, job_id)

    async def approve(self, owner: str, job_id: str, fingerprint: str, key: str) -> dict[str, Any]:
        _key(key)
        async with self.imports._locks.hold(job_id):
            row = await self._row(owner, job_id)
            await self._authorize(owner, job_id, include_used=bool(row["include_used"]))
            if row["approval_key"]:
                if row["approval_key"] != key or row["fingerprint"] != fingerprint:
                    raise ImportError("conflict", "This removal already has a different approval.")
            elif row["state"] != "review" or not fingerprint or fingerprint != row["fingerprint"]:
                raise ImportError("revision", "Review the current removal before approving it.")
            else:
                await self.store.execute(
                    "UPDATE music_import_removals SET state='checking',cursor='',"
                    "approval_key=?,actor_id=?,error=''"
                    " WHERE id=? AND state='review'",
                    [key, owner, row["id"]],
                )
        self.wake.set()
        return await self.status(owner, job_id)

    async def restore(self, owner: str, job_id: str) -> dict[str, Any]:
        async with self.imports._locks.hold(job_id):
            row = await self._row(owner, job_id)
            await self._authorize(owner, job_id)
            if row["state"] not in ("removing", "removed", "restoring"):
                raise ImportError("conflict", "This import has no approved removal to restore.")
            await self.store.execute(
                "UPDATE music_import_removals SET state='restoring',cursor='',error='',actor_id=?"
                " WHERE id=?",
                [owner, row["id"]],
            )
        self.wake.set()
        return await self.status(owner, job_id)

    async def retry(self, owner: str, job_id: str) -> dict[str, Any]:
        async with self.imports._locks.hold(job_id):
            row = await self._row(owner, job_id)
            await self._authorize(
                owner,
                job_id,
                include_used=bool(row["include_used"]) and row["state"] != "restoring",
            )
            if row["state"] not in ("reviewing", "checking", "removing", "restoring"):
                raise ImportError("conflict", "There is no interrupted removal to retry.")
            await self.store.execute(
                "UPDATE music_import_removals SET error='',actor_id=? WHERE id=?",
                [owner, row["id"]],
            )
        self.wake.set()
        return await self.status(owner, job_id)

    async def _row(self, owner: str, job_id: str) -> dict[str, Any]:
        await self._job(owner, job_id)
        row = await self.store.fetchone(
            "SELECT * FROM music_import_removals WHERE job_id=?", [job_id]
        )
        if not row:
            raise ImportError("not_found", "Review this import's removal first.")
        return dict(row)

    async def _native_uses(
        self, rows: list[dict[str, Any]], gone: bool, source: str
    ) -> tuple[str, dict[str, Any]]:
        if self.usage is None:
            raise ImportError("usage", "Shared-use checks are unavailable. Nothing was discarded.")
        mark = await self.usage.asked(wait=10)

        def read() -> tuple[str, dict[str, Any]]:
            assert self.usage
            conn, snapshot = self.usage.open()
            try:
                if (
                    not self.usage.covers(snapshot, mark)
                    or not snapshot.source
                    or (source and snapshot.source != source)
                ):
                    raise ImportError(
                        "usage", "The current shared-use snapshot could not be verified."
                    )
                paths = {
                    r["native_id"]: self.imports.storage.folder + "/" + r["final_name"]
                    for r in rows
                    if r["native_id"]
                }
                if current(conn, paths, gone):
                    raise ImportError(
                        "identity", "The exact native import identities could not be verified."
                    )
                usage = Usage(conn)
                found: dict[str, Any] = {}
                has_scrobbles = (
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scrobbles'"
                    ).fetchone()
                    is not None
                )
                for row in rows:
                    song = row["native_id"]
                    if not song:
                        path = self.imports.storage.folder + "/" + row["final_name"]
                        if conn.execute(
                            "SELECT 1 FROM media_file WHERE path=? LIMIT 1", [path]
                        ).fetchone():
                            raise ImportError(
                                "review_changed",
                                "Native indexing changed. Review this import's identities again.",
                            )
                        found[row["id"]] = ([], _digest(["unindexed", path]))
                        continue
                    reasons = usage.in_use([song], album_since=0, album_plays=True)
                    # No user IDs or private playlist names leave this snapshot. Counts
                    # and anonymous evidence still invalidate an approval after changes.
                    detail: list[Any] = []
                    for table, column in (
                        ("annotation", "item_id"),
                        ("playlist_tracks", "media_file_id"),
                        ("bookmark", "item_id"),
                    ):
                        detail.append(
                            [
                                list(r)
                                for r in conn.execute(
                                    f"SELECT * FROM {table} WHERE {column}=? ORDER BY 1",  # noqa: S608
                                    [song],
                                )
                            ]
                        )
                    detail.append(sorted(reasons))
                    if has_scrobbles:
                        detail.append(
                            conn.execute(
                                "SELECT count(*) FROM scrobbles WHERE media_file_id=?", [song]
                            ).fetchone()[0]
                        )
                    albums = _albums(conn, "")
                    playlists = {
                        str(r[0])
                        for r in conn.execute(
                            "SELECT DISTINCT playlist_id FROM playlist_tracks"
                            " WHERE media_file_id=?",
                            [song],
                        )
                    }
                    affected = {song} | albums | playlists | _artists(conn, albums)
                    detail.append(
                        sorted(
                            str(items)
                            for (items,) in conn.execute("SELECT items FROM playqueue")
                            if song in str(items or "").split(",")
                        )
                    )
                    detail.append(
                        sorted(
                            str(items)
                            for (items,) in conn.execute("SELECT resource_ids FROM share")
                            if not affected.isdisjoint(str(items or "").split(","))
                        )
                    )
                    for album in sorted(albums):
                        detail.append(
                            sorted(
                                [
                                    list(r)
                                    for r in conn.execute(
                                        "SELECT * FROM annotation WHERE item_type='album'"
                                        " AND item_id=?",
                                        [album],
                                    )
                                ],
                                key=repr,
                            )
                        )
                    found[row["id"]] = (reasons, _digest(detail))
                return snapshot.source, found
            finally:
                conn.close()

        return await anyio.to_thread.run_sync(read)

    async def _uncredited(self, item: dict[str, Any]) -> None:
        credited = await self.store.fetchone(
            "SELECT 1 FROM collection_originals WHERE library_id=? AND provenance_id=? LIMIT 1",
            [self.imports.library_id, "import:" + item["id"]],
        )
        if (
            item["state"] not in ("placing", "placed")
            or item["song_id"]
            or item["track_key"]
            or item.get("reviewed_song")
            or credited
        ):
            raise ImportError(
                "identity", "The imported native identity is unavailable. Check indexing."
            )

    async def _unindexed(self, actor: str, item: dict[str, Any]) -> None:
        # A missing known receipt must never be downgraded into nonpresence.
        await self._uncredited(item)
        known = await self.store.fetchone(
            "SELECT 1 FROM collection_track_aliases WHERE library_id=? AND alias=?",
            [self.imports.library_id, "import:" + item["asset_id"]],
        )
        if known:
            raise ImportError(
                "review_changed", "Native indexing changed. Review this import's identities again."
            )
        if self.native_absent is None:
            raise ImportError(
                "identity", "Native nonpresence checks are unavailable. Nothing was discarded."
            )
        path = self.imports.storage.folder + "/" + item["final_name"]
        if await self.native_absent(actor, self.imports.library_id, path) is not True:
            raise ImportError(
                "review_changed", "Native indexing changed. Review this import's identities again."
            )

    async def _restore_native(self, actor: str, item: dict[str, Any]) -> NativeFile | None:
        path = self.imports.storage.folder + "/" + item["final_name"]
        native = await self.imports.native_lookup(actor, self.imports.library_id, path)
        if native is None:
            return None
        if not native.song_id or native.path != path or native.size != item["expected_size"]:
            raise ImportError(
                "identity", "The restored native file identity could not be verified."
            )
        # A refresh can learn the native alias after unlink but before the
        # missing-file scan, when the asset's final inode cannot be verified yet.
        previous = await self.store.fetchone(
            "SELECT n.track_key AS native_key,a.track_key AS asset_key"
            " FROM collection_track_aliases n LEFT JOIN collection_track_aliases a"
            " ON a.library_id=n.library_id AND a.alias=? WHERE n.library_id=? AND n.alias=?",
            ["import:" + item["asset_id"], self.imports.library_id, "native:" + native.song_id],
        )
        if previous and previous["native_key"] != previous["asset_key"]:
            if previous["asset_key"] is not None:
                raise ImportError(
                    "identity", "The restored import has conflicting durable identities."
                )
            await self._native_only_alias(item, native, previous["native_key"])
        return native

    async def _native_only_alias(
        self, item: dict[str, Any], native: NativeFile, track_key: str
    ) -> None:
        await self._uncredited(item)
        structural = await self.store.fetchone(
            "SELECT song_id,size,original,metadata FROM collection_tracks"
            " WHERE library_id=? AND track_key=?",
            [self.imports.library_id, track_key],
        )
        bound = await self.store.fetchone(
            "SELECT 1 FROM collection_track_aliases WHERE library_id=? AND track_key=? AND alias<>?"
            " UNION ALL SELECT 1 FROM music_import_items i"
            " JOIN music_import_jobs j ON j.id=i.job_id"
            " WHERE j.library_id=? AND i.id<>? AND (i.song_id=? OR i.track_key=?) LIMIT 1",
            [
                self.imports.library_id,
                track_key,
                "native:" + native.song_id,
                self.imports.library_id,
                item["id"],
                native.song_id,
                track_key,
            ],
        )
        if (
            not structural
            or bound
            or track_key != "native:" + native.song_id
            or structural["song_id"] != native.song_id
            or structural["size"] != item["expected_size"]
            or structural["original"] != 1
        ):
            raise ImportError("identity", "Navidrome reused a known file identity. Check indexing.")
        metadata = json.loads(structural["metadata"])
        if not isinstance(metadata, dict) or any(
            field in metadata and metadata[field] != expected
            for field, expected in (
                ("id", native.song_id),
                ("path", native.path),
                ("size", native.size),
            )
        ):
            raise ImportError(
                "identity", "The stored native file identity conflicts with this import."
            )
        # This grants no ownership and writes no alias. The caller must restore
        # the same retained inode, verify its asset, then use the index's normal
        # single-key alias addition without repointing an existing key.

    async def _restored_evidence(self, row: dict[str, Any], items: list[dict[str, Any]]) -> None:
        actor = row.get("actor_id") or row["owner_id"]
        known = [item for item in items if item["reviewed_song"]]
        if known:
            # Preserve the indexed Restore gate before any refresh can learn
            # aliases. Only an originally empty receipt may learn a new ID here.
            _, evidence = await self._evidence(row, known)
            if any(
                e["song_id"] != i["reviewed_song"] for e, i in zip(evidence, known, strict=True)
            ):
                raise ImportError(
                    "identity", "The restored native identities changed. Check indexing."
                )
        learned: dict[str, NativeFile | None] = {}
        songs, prepared = [], []
        for item in items:
            copy = dict(item)
            if not item["reviewed_song"]:
                await self._uncredited(item)
                native = await self._restore_native(actor, item)
                learned[item["id"]] = native
                if native:
                    songs.append(
                        {
                            "id": native.song_id,
                            "libraryId": self.imports.library_id,
                            "path": native.path,
                            "size": native.size,
                        }
                    )
                copy.pop("reviewed_song")
            prepared.append(copy)
        assets = await self.imports.asset_lookup(self.imports.library_id, songs) if songs else {}
        for item in items:
            native = learned.get(item["id"])
            if native and assets.get(native.song_id) != item["asset_id"]:
                raise ImportError("identity", "The restored import asset could not be verified.")
        indexed = []
        for item in items:
            native = learned.get(item["id"])
            if native:
                indexed.append({**item, "native_id": native.song_id})
        if indexed:
            await self._native_uses(indexed, False, row["source"])
        await self.imports.refresh(actor, self.imports.library_id)
        for item in items:
            if item["id"] not in learned:
                continue
            native = await self._restore_native(actor, item)
            if native != learned[item["id"]]:
                raise ImportError(
                    "identity", "Native indexing changed during Restore. Retry Restore."
                )
            if native:
                aliases = await self.store.fetchone(
                    "SELECT 1 FROM collection_track_aliases a JOIN collection_track_aliases n"
                    " ON n.library_id=a.library_id AND n.track_key=a.track_key"
                    " JOIN collection_tracks t ON t.library_id=a.library_id"
                    " AND t.track_key=a.track_key WHERE a.library_id=? AND a.alias=?"
                    " AND n.alias=? AND t.song_id=? AND t.present=1",
                    [
                        self.imports.library_id,
                        "import:" + item["asset_id"],
                        "native:" + native.song_id,
                        native.song_id,
                    ],
                )
                if not aliases:
                    raise ImportError(
                        "identity", "The restored import aliases could not be verified."
                    )
        _, evidence = await self._evidence(row, prepared)
        for item, entry in zip(items, evidence, strict=True):
            native = learned.get(item["id"])
            expected = native.song_id if native else item["reviewed_song"]
            if entry["song_id"] != expected:
                raise ImportError(
                    "identity", "The restored native identities changed. Retry Restore."
                )

    async def _evidence(
        self, row: dict[str, Any], items: list[dict[str, Any]], *, gone: bool = False
    ) -> tuple[str, list[dict[str, Any]]]:
        native_rows = []
        actor = row.get("actor_id") or row["owner_id"]
        for item in items:
            if "reviewed_song" in item and not item["reviewed_song"]:
                await self._unindexed(actor, item)
                item["native_id"] = ""
            elif gone:
                item["native_id"] = item["reviewed_song"]
            else:
                native = await self.imports.native_lookup(
                    actor,
                    self.imports.library_id,
                    self.imports.storage.folder + "/" + item["final_name"],
                )
                if native is None:
                    await self._unindexed(actor, item)
                    item["native_id"] = ""
                    native_rows.append(item)
                    continue
                if (
                    not native.song_id
                    or (item["state"] == "complete" and not item["song_id"])
                    or native.size != item["expected_size"]
                    or native.path != self.imports.storage.folder + "/" + item["final_name"]
                    or (item["song_id"] and item["song_id"] != native.song_id)
                ):
                    raise ImportError(
                        "identity", "Navidrome has not verified this exact imported file."
                    )
                item["native_id"] = native.song_id
            native_rows.append(item)
        source, uses = await self._native_uses(native_rows, gone, row["source"])
        evidence = []
        for item in native_rows:
            reasons, native_key = uses[item["id"]]
            if not item["native_id"]:
                evidence.append(
                    {
                        "item_id": item["id"],
                        "song_id": "",
                        "reasons": [],
                        "use_key": _digest([native_key, [], [], []]),
                        "remove_file": True,
                    }
                )
                continue
            structural = await self.store.fetchone(
                "SELECT track_key FROM collection_tracks WHERE library_id=? AND song_id=?",
                [self.imports.library_id, item["native_id"]],
            )
            if not structural or (
                item["track_key"] and item["track_key"] != structural["track_key"]
            ):
                raise ImportError("identity", "The imported collection identity changed.")
            track_key = structural["track_key"]
            shared = await self.store.fetchall(
                "SELECT owner_id,profile_id,revision FROM collection_membership WHERE library_id=?"
                " AND track_key=? AND saved=1 ORDER BY owner_id,profile_id",
                [self.imports.library_id, track_key],
            )
            credited = await self.store.fetchall(
                "SELECT owner_id,provenance_id FROM collection_originals"
                " WHERE library_id=? AND track_key=? ORDER BY owner_id",
                [self.imports.library_id, track_key],
            )
            history = await self.store.fetchall(
                "SELECT owner_id FROM collection_history WHERE library_id=? AND track_key=?"
                " AND owner_id<>? ORDER BY owner_id",
                [self.imports.library_id, track_key, row["owner_id"]],
            )
            if any(s["owner_id"] != row["owner_id"] for s in shared):
                reasons.append("saved in another person's library")
            if any(s["owner_id"] != row["owner_id"] for s in credited):
                reasons.append("credited to another person")
            if history:
                reasons.append("in another person's personal music history")
            evidence.append(
                {
                    "item_id": item["id"],
                    "song_id": item["native_id"],
                    "reasons": sorted(reasons),
                    "use_key": _digest(
                        [
                            native_key,
                            [list(s) for s in shared],
                            [list(s) for s in credited],
                            [list(s) for s in history],
                        ]
                    ),
                    "remove_file": bool(row["include_used"] or not reasons),
                }
            )
        return source, evidence

    async def _review(self, row: dict[str, Any]) -> None:
        if not row["cursor"]:
            await self.imports.scan([self.imports.storage.folder])
            await self._authorize(
                (row.get("actor_id") or row["owner_id"]),
                row["job_id"],
                include_used=bool(row["include_used"]),
            )
            await self.imports.refresh(
                (row.get("actor_id") or row["owner_id"]), self.imports.library_id
            )
        items = [
            dict(i)
            for i in await self.store.fetchall(
                "SELECT * FROM music_import_items WHERE job_id=? AND id>?"
                " AND state IN ('placing','placed','complete') ORDER BY id LIMIT ?",
                [row["job_id"], row["cursor"], BATCH],
            )
        ]
        published = []
        for item in items:
            present = await anyio.to_thread.run_sync(
                partial(
                    same_file, self.imports.storage, item, missing_ok=item["state"] == "placing"
                )
            )
            if present:
                await self.imports._verify(item)
                published.append(item)
        source, evidence = (
            await self._evidence(row, published) if published else (row["source"], [])
        )
        await self._authorize(
            (row.get("actor_id") or row["owner_id"]),
            row["job_id"],
            include_used=bool(row["include_used"]),
        )
        async with self.store.transaction() as conn:
            for e in evidence:
                await conn.execute(
                    "INSERT INTO music_import_removal_items"
                    "(removal_id,item_id,song_id,reasons,use_key,remove_file) VALUES (?,?,?,?,?,?)",
                    [
                        row["id"],
                        e["item_id"],
                        e["song_id"],
                        json.dumps(e["reasons"]),
                        e["use_key"],
                        int(e["remove_file"]),
                    ],
                )
            await conn.execute(
                "UPDATE music_import_removals SET cursor=?,source=? WHERE id=?",
                [items[-1]["id"] if items else row["cursor"], source, row["id"]],
            )
        if len(items) < BATCH:
            hashed = hashlib.sha256()
            cursor = ""
            while True:
                page = await self.store.fetchall(
                    "SELECT item_id,song_id,reasons,use_key,remove_file"
                    " FROM music_import_removal_items WHERE removal_id=?"
                    " AND item_id>? ORDER BY item_id LIMIT ?",
                    [row["id"], cursor, BATCH],
                )
                if not page:
                    break
                for entry in page:
                    hashed.update(json.dumps(list(entry)).encode())
                cursor = page[-1]["item_id"]
            await self.store.execute(
                "UPDATE music_import_removals SET state='review',fingerprint=?,cursor=''"
                " WHERE id=?",
                [hashed.hexdigest(), row["id"]],
            )

    async def _batch(self, row: dict[str, Any]) -> None:
        phase = row["state"]
        items = [
            dict(i)
            for i in await self.store.fetchall(
                "SELECT i.*,r.song_id AS reviewed_song,r.reasons AS reviewed_reasons,r.use_key,"
                "r.remove_file,r.state AS removal_state FROM music_import_removal_items r"
                " JOIN music_import_items i ON i.id=r.item_id WHERE r.removal_id=? AND r.item_id>?"
                " ORDER BY r.item_id LIMIT ?",
                [row["id"], row["cursor"], BATCH],
            )
        ]
        affected = [i for i in items if i["remove_file"]]
        if phase != "restoring" and items:
            # Uncertain unlinks are reconciled by retained inode + durable per-file intent.
            present = [i for i in items if i["removal_state"] == "present"]
            if present:
                try:
                    _, evidence = await self._evidence(row, present)
                except ImportError as exc:
                    if exc.code != "review_changed":
                        raise
                    await self._changed(row, phase)
                    return
                if any(
                    e["use_key"] != i["use_key"] or e["song_id"] != i["reviewed_song"]
                    for e, i in zip(evidence, present, strict=True)
                ):
                    await self._changed(row, phase)
                    return
        if phase in ("removing", "restoring"):
            for item in affected:
                receipt = await anyio.to_thread.run_sync(verified, self.imports.storage, item)
                if not item["reviewed_song"] and phase == "removing":
                    try:
                        # Hash first, then re-prove nonpresence even when durable
                        # unlink/link intent survived a crash without its receipt.
                        await self._evidence(row, [item])
                    except ImportError as exc:
                        if exc.code != "review_changed" or phase == "restoring":
                            raise
                        await self._changed(row, phase)
                        return
                elif not item["reviewed_song"]:
                    # Restoring an exact retained link is non-destructive. A
                    # delayed native scan must not block it merely by finding it.
                    await self._uncredited(item)
                    await self._restore_native(row.get("actor_id") or row["owner_id"], item)
                    await self._native_uses([], False, row["source"])
                if phase == "removing":
                    await self.store.execute(
                        "UPDATE music_import_removal_items SET state='removing'"
                        " WHERE removal_id=? AND item_id=?",
                        [row["id"], item["id"]],
                    )
                await self._authorize(
                    (row.get("actor_id") or row["owner_id"]),
                    row["job_id"],
                    include_used=bool(row["include_used"]) and phase == "removing",
                )
                if phase == "removing":
                    # No wait or hashing separates the fresh gates from the unlink.
                    quarantine(self.imports.storage, item, receipt)
                    state = "removed"
                else:
                    restore(self.imports.storage, item, receipt)
                    await self.imports._verify(item, final=True)
                    state = "present"
                await self.store.execute(
                    "UPDATE music_import_removal_items SET state=?"
                    " WHERE removal_id=? AND item_id=?",
                    [state, row["id"], item["id"]],
                )
            if affected:
                await self.imports.scan([self.imports.storage.folder])
                await self._authorize((row.get("actor_id") or row["owner_id"]), row["job_id"])
                if phase == "removing":
                    try:
                        _, evidence = await self._evidence(row, affected, gone=True)
                    except ImportError as exc:
                        if exc.code != "review_changed":
                            raise
                        await self._changed(row, phase)
                        return
                    if any(
                        e["use_key"] != i["use_key"]
                        for e, i in zip(evidence, affected, strict=True)
                    ):
                        await self.store.execute(
                            "UPDATE music_import_removals SET state='restoring',cursor=''"
                            " WHERE id=?",
                            [row["id"]],
                        )
                        return
                else:
                    await self._restored_evidence(row, affected)
                    await self._authorize(row.get("actor_id") or row["owner_id"], row["job_id"])
                if phase == "removing":
                    await self.imports.refresh(
                        (row.get("actor_id") or row["owner_id"]), self.imports.library_id
                    )
        if len(items) < BATCH:
            following = {"checking": "removing", "removing": "removed", "restoring": "restored"}[
                phase
            ]
            async with self.store.transaction() as conn:
                if following in ("removed", "restored"):
                    # The songs this import linked to were never its files: they leave
                    # (and come back to) the importer's own imports with it.
                    await self._links(conn, row["job_id"], active=following == "restored")
                await conn.execute(
                    "UPDATE music_import_removals SET state=?,cursor='' WHERE id=?",
                    [following, row["id"]],
                )
            if following in ("removed", "restored"):
                # Its audio in catalog songs goes out (comes back) next: the import
                # service's worker does that (``catalog_audio.py``).
                self.imports.poke()
        else:
            await self.store.execute(
                "UPDATE music_import_removals SET cursor=? WHERE id=?", [items[-1]["id"], row["id"]]
            )

    async def _links(self, conn: Any, job_id: str, *, active: bool) -> None:
        """The import's links go out (come back); each owner's credit for those songs then
        follows from every link of theirs still active (``sync_link_credit``): another
        import of theirs linked to the same song keeps it."""
        cursor = await conn.execute(
            "SELECT item_id,owner_id,library_id,track_key FROM music_import_links"
            " WHERE job_id=? AND active=?",
            [job_id, int(not active)],
        )
        links = list(await cursor.fetchall())
        for link in links:
            await conn.execute(
                "UPDATE music_import_links SET active=? WHERE item_id=?",
                [int(active), link["item_id"]],
            )
        for owner, library, track in sorted(
            {(link["owner_id"], link["library_id"], link["track_key"]) for link in links}
        ):
            await sync_link_credit(conn, owner_id=owner, library_id=library, track_key=track)

    async def _changed(self, row: dict[str, Any], phase: str) -> None:
        await self.store.execute(
            "UPDATE music_import_removals SET state=?,cursor='',error=? WHERE id=?",
            [
                "stale" if phase == "checking" else "restoring",
                "Sharing or native indexing changed. Review the removal again."
                if phase == "checking"
                else "",
                row["id"],
            ],
        )

    async def tick(self) -> bool:
        async with self.imports._worker:
            row = await self.store.fetchone(
                "SELECT * FROM music_import_removals WHERE state IN"
                " ('reviewing','checking','removing','restoring') AND error=''"
                " ORDER BY created LIMIT 1"
            )
            if not row:
                return False
            async with self.imports._locks.hold(row["job_id"]):
                # Selection can wait behind Restore, Retry or a new review. Act
                # on the state protected by the job lock, never the queued copy.
                current = await self.store.fetchone(
                    "SELECT * FROM music_import_removals WHERE id=?", [row["id"]]
                )
                if (
                    not current
                    or current["error"]
                    or current["state"] not in ("reviewing", "checking", "removing", "restoring")
                ):
                    return True
                item = dict(current)
                try:
                    await self._authorize(
                        (item.get("actor_id") or item["owner_id"]),
                        item["job_id"],
                        include_used=bool(item["include_used"]) and item["state"] != "restoring",
                    )
                    if item["state"] == "reviewing":
                        await self._review(item)
                    else:
                        await self._batch(item)
                except Exception as exc:
                    message = (
                        str(exc)
                        if isinstance(exc, (ImportError, CollectionError))
                        else (
                            "Operation paused. Check access, storage, indexing and shared-use "
                            "checks; retry or restore."
                        )
                    )
                    await self.store.execute(
                        "UPDATE music_import_removals SET error=? WHERE id=?",
                        [message, item["id"]],
                    )
            return True

    async def run(self) -> None:
        while True:
            self.wake = anyio.Event()
            if await self.tick():
                continue
            await self.wake.wait()
