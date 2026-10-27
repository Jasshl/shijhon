"""Read-only import identity resolution, independent of upload enablement."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from sqlite3 import Row
from typing import Any

import anyio

from shijhon.music_imports.model import ImportError
from shijhon.music_imports.storage import Storage
from shijhon.store import Store


class AssetResolver:
    """Trusted structural callback over existing state; no intake worker or writes.

    It is safe to compose this when uploads are disabled. Callers still obtain and
    authorize native source rows themselves. This callback never mints ownership.
    Each call closes every descriptor, so it has no startup/shutdown lifecycle.
    """

    def __init__(
        self,
        store: Store,
        *,
        library_id: str,
        library_path: Path,
        imports_folder: str = "_imports",
        managed_folder: str = "_shijhon",
    ) -> None:
        self.store, self.library_id = store, library_id
        self.library_path, self.imports_folder = library_path, imports_folder
        self.managed_folder = managed_folder

    async def __call__(
        self, library_id: str, songs: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, str]:
        if library_id != self.library_id:
            return {}
        if len(songs) > 500:
            raise ImportError("invalid", "Choose a bounded native asset page.")
        roots = await self.store.fetchall(
            "SELECT DISTINCT j.root_id FROM music_import_jobs j"
            " JOIN music_import_items i ON i.job_id=j.id WHERE j.library_id=?"
            " AND i.state IN ('placing','placed','complete') LIMIT 2",
            [library_id],
        )
        if not roots:
            return {}
        installation = await self.store.fetchone(
            "SELECT id FROM music_import_installation WHERE singleton=1"
        )
        if not installation:
            raise ImportError("storage", "The import installation receipt is unavailable.")
        storage = Storage(self.library_path, self.imports_folder, self.managed_folder)
        await anyio.to_thread.run_sync(storage.open_readonly, installation["id"], library_id)
        try:
            if len(roots) != 1 or roots[0]["root_id"] != storage.root_id:
                raise ImportError("storage", "The retained import root changed. Check storage.")
            prefix = storage.folder + "/"
            native_ids = [song["id"] for song in songs if isinstance(song.get("id"), str)]
            # Resolve the page's distinct native/import bindings before joining
            # receipts, avoiding a scan of all import aliases for every native ID.
            receipts = await self.store.fetchall(
                "WITH page_ids AS (SELECT value AS id FROM json_each(?)),"
                " native_page AS ("
                " SELECT DISTINCT p.id AS song_id,n.library_id,n.track_key"
                " FROM page_ids p JOIN collection_track_aliases n"
                " ON n.library_id=? AND n.alias='native:'||p.id"
                " WHERE n.alias IN (SELECT 'native:'||id FROM page_ids)),"
                " import_page AS ("
                " SELECT DISTINCT a.library_id,a.track_key,a.alias FROM collection_track_aliases a"
                " WHERE a.library_id IN (SELECT library_id FROM native_page)"
                " AND a.track_key IN (SELECT track_key FROM native_page)"
                " AND a.alias GLOB 'import:*')"
                " SELECT p.id AS song_id,i.asset_id,i.final_name,i.expected_size,j.root_id"
                " FROM page_ids p JOIN music_import_items i ON i.song_id<>'' AND i.song_id=p.id"
                " JOIN music_import_jobs j ON j.id=i.job_id WHERE j.library_id=?"
                " UNION"
                " SELECT n.song_id,i.asset_id,i.final_name,i.expected_size,j.root_id"
                " FROM native_page n JOIN import_page a ON a.library_id=n.library_id"
                " AND a.track_key=n.track_key"
                " JOIN music_import_items i ON i.asset_id=substr(a.alias,8)"
                " JOIN music_import_jobs j ON j.id=i.job_id AND j.library_id=n.library_id"
                " WHERE i.state IN ('placing','placed','complete') LIMIT 501",
                [json.dumps(native_ids), library_id, library_id],
            )
            # Native aliases learned during restore remain bound to the asset, even
            # before a placed item's immutable receipt song/track fields are filled.
            expected: dict[str, Row] = {}
            for receipt_row in receipts:
                previous = expected.get(receipt_row["song_id"])
                if previous and previous["asset_id"] != receipt_row["asset_id"]:
                    raise ImportError("identity", "The imported native identity is ambiguous.")
                if receipt_row["root_id"] != storage.root_id:
                    raise ImportError("storage", "The retained import root changed. Check storage.")
                expected[receipt_row["song_id"]] = receipt_row
            candidates = []
            for song in songs:
                path, ident, size = song.get("path"), song.get("id"), song.get("size")
                receipt = expected.get(ident) if isinstance(ident, str) else None
                if receipt and (
                    path != prefix + receipt["final_name"]
                    or type(size) is not int
                    or size != receipt["expected_size"]
                    or str(song.get("libraryId", "")) != library_id
                ):
                    raise ImportError("identity", "Navidrome reused an imported song identity.")
                if (
                    isinstance(path, str)
                    and isinstance(ident, str)
                    and ident
                    and type(size) is int
                    and size > 0
                    and str(song.get("libraryId", "")) == library_id
                    and path.startswith(prefix)
                ):
                    candidates.append((ident, size, path[len(prefix) :]))
            rows = await self.store.fetchall(
                "SELECT i.asset_id,i.expected_size,i.final_name,i.staging_name"
                " FROM music_import_items i JOIN music_import_jobs j ON j.id=i.job_id"
                " WHERE j.library_id=? AND j.root_id=?"
                " AND i.state IN ('placing','placed','complete')"
                " AND i.final_name IN (SELECT value FROM json_each(?))",
                [library_id, storage.root_id, json.dumps([name for _, _, name in candidates])],
            )
            known = {row["final_name"]: row for row in rows}
            result: dict[str, str] = {}
            for ident, size, name in candidates:
                row = known.get(name)
                if not row or row["expected_size"] != size:
                    continue
                try:
                    final = storage.file(row["final_name"], final=True)
                except FileNotFoundError:
                    continue
                try:
                    try:
                        staged = storage.file(row["staging_name"])
                    except FileNotFoundError:
                        raise ImportError(
                            "storage", "The retained import inode receipt is unavailable."
                        ) from None
                    try:
                        a, b = os.fstat(final), os.fstat(staged)
                    finally:
                        os.close(staged)
                    if a.st_size == size and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                        if ident in result and result[ident] != row["asset_id"]:
                            raise ImportError(
                                "identity", "The imported native identity is ambiguous."
                            )
                        result[ident] = row["asset_id"]
                finally:
                    os.close(final)
            for ident, receipt in expected.items():
                if result.get(ident) != receipt["asset_id"]:
                    raise ImportError(
                        "storage", "The retained imported file could not be verified."
                    )
            return result
        finally:
            storage.close()
