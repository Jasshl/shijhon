"""Owner-bound intake, frozen review and explicit retained-original publication."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import sys
import time
import unicodedata
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import anyio

from shijhon.collection.contracts import Access, CollectionError, NativeAbsent
from shijhon.collection.origins import record_link_credit, record_verified_original
from shijhon.locks import KeyedLocks
from shijhon.matching import covers
from shijhon.matching.albums import Album, same_keys, same_name
from shijhon.matching.grouping import album_artist
from shijhon.matching.normalize import fold
from shijhon.music_imports import catalog_audio as audio
from shijhon.music_imports import tagmap
from shijhon.music_imports.assets import AssetResolver
from shijhon.music_imports.catalog_audio import CatalogSongAudio
from shijhon.music_imports.model import (
    FORMATS,
    MANIFEST_PAGE_SIZE,
    NATIVE_REJECTED,
    ImportError,
    ImportLimits,
    ManifestEntry,
    NativeFile,
    relative_path,
    scanner_ignored_path,
)
from shijhon.music_imports.overlap import (
    CATALOG,
    CATALOG_SONG,
    COPY,
    DUPLICATE,
    HELD,
    LINKED,
    LINKS,
    LOW_QUALITY,
    NEW,
    PUBLISHED,
    SAME_BYTES,
    UNSURE,
    CatalogFound,
    CatalogRefused,
    Decision,
    Incoming,
    LibraryAlbum,
    LibrarySong,
    Tags,
    classify,
    quality_of,
)
from shijhon.music_imports.storage import Storage
from shijhon.music_imports.upgrade import Upgrades
from shijhon.quality import Minimum
from shijhon.store import Store

log = logging.getLogger(__name__)

Scan = Callable[[Sequence[str]], Awaitable[None]]
NativeLookup = Callable[[str, str, str], Awaitable[NativeFile | None]]
Refresh = Callable[[str, str], Awaitable[None]]
# The importing caller's own native read of one song (``CallerLibrary.song``) and of a
# cover (``CallerLibrary.artwork``): matches with the library are checked with them.
NativeSong = Callable[[str, str, str], Awaitable[Mapping[str, Any]]]
Artwork = Callable[[str, str, str], Awaitable[tuple[bytes, str]]]
# The importing person's minimum audio quality, by user ID (``services.quality``).
MinimumOf = Callable[[str], Awaitable[Minimum]]


class CatalogVersions(Protocol):
    """A low-quality album's catalog version (``music_imports/catalog_route.py``)."""

    async def find(self, items: Sequence[Incoming]) -> CatalogFound | None: ...

    async def add(
        self,
        release: str,
        tracks: Sequence[str],
        *,
        fingerprint: str,
        authorize: Callable[[], Awaitable[None]],
    ) -> dict[str, str]: ...


_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
UPLOAD_AUTH_SECONDS = 1.0
_FINISHED = (
    "received",
    "eligible",
    "excluded",
    "placing",
    "placed",
    "complete",
    # Music the library already has (``overlap.py``): linked, not published.
    "matched",
    "linked",
    # Not published: a duplicate in the selection, or held back.
    "skipped",
    # Published as a copy with an album's tags: being made, and the retained original.
    "copying",
    "joined",
    # Below the person's minimum: the catalog's version is added at publication.
    "catalog",
)
# Item state after the review's decision (a low-quality album's: by its route).
_STATE_OF = {NEW: "eligible", UNSURE: "eligible", COPY: "copying", LINKED: "matched",
             CATALOG_SONG: "matched", DUPLICATE: "skipped", HELD: "skipped",
             LOW_QUALITY: "skipped"}  # fmt: skip
_RECLASSIFIED = ("matched", "skipped", "copying", "joined", "catalog")
LINK_BATCH = 200
SEPARATE = (
    "Navidrome shows this song as a separate album: its album details differ from the "
    "library's album."
)
REPLACED = (
    "The library's copy of this song was removed after the review, so this file was added instead."
)
MEANWHILE = "Another import added this song while this one waited; it is linked to that song."
CHANGED = (
    "The library changed after the review, so what happens to this file changed: review it again."
)
NOT_ADDED = "The catalog's version could not be added ({reason}), so this file was not added."
UNLINKED = "The catalog's version of this song is not in the library, so this file was not added."
# A file the quality gate looked at (``match.below_minimum``), as SQL reads it.
_BELOW = (
    "(CASE WHEN json_valid(i.match) THEN json_extract(i.match,'$.below_minimum') END IS NOT NULL)"
)

# The files a status page shows: 0 all, 1 those needing a look, 2 those the library
# already had or that join its albums (``?`` three times: the view).
_SHOWN = (
    "(?=0 OR (?=1 AND (i.state IN ('rejected','excluded') OR i.warnings<>'[]'"
    " OR i.outcome IN ('held','unsure','low_quality') OR " + _BELOW + "))"
    " OR (?=2 AND (i.outcome IN"
    " ('linked','catalog_song','duplicate','held','unsure','copy','low_quality')"
    " OR " + _BELOW + " OR i.upgraded_from<>'')))"
)
_STATUS_COUNTS = (  # constant fragments only
    "SELECT j.id,j.state,j.revision,j.error,"  # noqa: S608
    "coalesce(sum(i.derived_from=''),0) AS total_files,"
    "coalesce(sum(CASE WHEN i.derived_from='' THEN i.expected_size ELSE 0 END),0)"
    " AS total_bytes,"
    "coalesce(sum(i.received_size>0 AND i.derived_from=''),0) AS received_files,"
    "coalesce(sum(CASE WHEN i.derived_from='' THEN i.received_size ELSE 0 END),0)"
    " AS received_bytes,"
    "coalesce(sum(i.state IN ('eligible','placing','placed','complete')),0)"
    " AS eligible_files,"
    "coalesce(sum(CASE WHEN i.state IN ('eligible','placing','placed','complete')"
    " THEN i.expected_size ELSE 0 END),0) AS eligible_bytes,"
    "coalesce(sum(i.state='complete'),0) AS complete_files,"
    "coalesce(sum(i.state='rejected'),0) AS rejected_files,"
    "coalesce(sum(i.state='excluded'),0) AS excluded_files,"
    "coalesce(sum(i.state IN ('matched','linked') AND i.outcome<>'low_quality'),0)"
    " AS linked_files,"
    "coalesce(sum(i.state='linked' AND i.outcome<>'low_quality'),0) AS linked_done_files,"
    "coalesce(sum(i.outcome='low_quality' AND i.state IN ('catalog','matched','linked')),0)"
    " AS low_quality_files,"
    "coalesce(sum(i.outcome='low_quality' AND i.state='skipped'),0) AS unchecked_files,"
    "coalesce(sum(i.derived_from='' AND i.outcome<>'low_quality' AND " + _BELOW + "),0)"
    " AS below_minimum_files,"
    "coalesce(sum(i.state IN ('matched','linked') AND i.outcome='catalog_song'),0)"
    " AS catalog_files,"
    "coalesce(sum(i.state='skipped' AND i.outcome='duplicate'),0) AS duplicate_files,"
    "coalesce(sum(i.state='skipped' AND i.outcome='held'),0) AS held_files,"
    "coalesce(sum(i.derived_from<>'' AND i.state<>'discarding'),0) AS copy_files,"
    "coalesce(sum(i.derived_from='' AND i.outcome='unsure'),0) AS unsure_files,"
    "coalesce(sum(i.state='copying'),0) AS copying_files,"
    "count(CASE WHEN i.derived_from='' AND " + _SHOWN + " THEN i.id END) AS filtered_total"
    " FROM music_import_jobs j LEFT JOIN music_import_items i ON i.job_id=j.id"
    " WHERE j.id=? GROUP BY j.id"
)
_STATUS_ITEMS = (  # constant fragments only
    "SELECT i.id,i.client_key,i.display_path AS path,i.expected_size AS size,"  # noqa: S608
    "i.state,i.attempt,i.warnings,i.error,i.digest AS sha256,i.song_id,i.outcome,"
    "i.match,i.distinct_song AS distinct_song,coalesce(d.state,'') AS copy_state,"
    "coalesce(json_extract(CASE WHEN json_valid(i.facts) THEN i.facts END,"
    "'$.keep_files'),0) AS keep_files,"
    "coalesce(d.song_id,'') AS copy_song_id,coalesce(l.audio,'') AS audio,"
    "CASE WHEN d.id IS NULL THEN i.upgraded_from ELSE d.upgraded_from END AS upgraded"
    " FROM music_import_items i LEFT JOIN music_import_items d"
    # Copies always name a nonempty original ID. State this explicitly so SQLite
    # uses the partial derived_from index instead of scanning every item per row.
    " ON d.derived_from<>'' AND d.derived_from=i.id AND d.state<>'discarding'"
    " LEFT JOIN music_import_links l ON l.item_id=i.id"
    " WHERE i.job_id=? AND i.derived_from='' AND " + _SHOWN + " ORDER BY i.id LIMIT ? OFFSET ?"
)
# Stop keeps reserved what an unplaced item retains: its received bytes. A linked file's
# reservation goes only once its bytes are deleted (``MusicImports._release_linked``,
# which also does it for stopped imports), and so does a given-up or unfinished copy's
# (``discarding``: ``MusicImports._discard_copies``).
_RELEASE_UNPLACED = (
    "UPDATE music_import_items SET reserved_size="
    "CASE WHEN state='linked' AND outcome IN ('linked','low_quality') THEN reserved_size"
    " WHEN state='discarding' THEN reserved_size ELSE received_size END"
    " WHERE job_id=? AND state NOT IN ('uploading','placing','placed','complete')"
)

COPY_SLACK = 1024**2  # a copy's tags may be larger than the original's
_DEFAULT_LIMITS = ImportLimits()
PUBLISH_BATCH_FILES = 500
PUBLISH_BATCH_BYTES = 1024**3
AUDIO_TRIES = 10  # an import's audio for a catalog song: attempts that had to wait
AUDIO_RETRY = 30.0  # seconds before such an attempt is made again (times the attempts)
IDLE_SECONDS = 60.0  # the worker looks for work it postponed at least this often
# A catalog-song link's audio values other than ``placed`` (``catalog_audio.py``; "": none
# to put in).
_NOT_PLACED = ("", audio.PENDING, audio.KEPT, audio.UNTAGGABLE, audio.LOW_QUALITY, audio.FAILED)


def _state_of(decision: Decision) -> str:
    """The item state a review decision gives (a low-quality album waits for its catalog
    version at publication, or - the catalog unreachable - is not added; a song published
    as a copy carrying an album's tags, also an unsure one, has it made first)."""
    if decision.outcome == LOW_QUALITY and decision.gate.get("route") == CATALOG:
        return "catalog"
    if decision.outcome in (COPY, UNSURE) and decision.tags_from:
        return "copying"
    return _STATE_OF[decision.outcome]


def _plan(decision: Decision) -> tuple[Any, ...]:
    """What a decision does with a file at publication, for comparing a reviewed plan with
    the library as it is now: published as it is, published as a copy carrying an album's
    tags (with that album's identity), linked to a song, or not added."""
    if decision.outcome in PUBLISHED and decision.tags_from:
        return ("copy", tuple(decision.target_key))
    if decision.outcome in PUBLISHED:
        return ("publish",)
    if decision.outcome in LINKS:
        return ("link", decision.song.song_id if decision.song else "")
    return ("skip", decision.outcome)


def _approved_plan(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """``_plan`` of a reviewed file's recorded decision (its ``outcome`` and ``match``)."""
    try:
        match = json.loads(item.get("match") or "{}")
    except ValueError:
        match = {}
    match = match if isinstance(match, dict) else {}
    outcome = str(item.get("outcome") or "")
    if outcome in PUBLISHED and match.get("tags_from"):
        return ("copy", tuple(match.get("target_key") or ()))
    if outcome in PUBLISHED:
        return ("publish",)
    if outcome in LINKS:
        return ("link", str(match.get("song_id") or ""))
    return ("skip", outcome)


def _facts(item: Mapping[str, Any]) -> dict[str, Any]:
    try:
        facts = json.loads(item.get("facts") or "{}")
    except ValueError:
        return {}
    return facts if isinstance(facts, dict) else {}


def _keep_files(item: Mapping[str, Any]) -> bool:
    return _facts(item).get("keep_files") is True


def _album_of_facts(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """The album a file names (folded name and album artist), None without a name."""
    facts = _facts(item)
    name = fold(str(facts.get("album") or ""))
    if not name:
        return None
    return name, fold(Incoming(str(item.get("id") or ""), facts).album_artist)


def _key(value: str) -> None:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ImportError("invalid", "Choose a valid request or file key.")


def _path(value: str) -> str:
    return relative_path(value)


def _path_nodes(path: str) -> list[tuple[str, str, bool]]:
    parts = path.split("/")
    return [
        (
            unicodedata.normalize("NFC", unicodedata.normalize("NFC", prefix).casefold()),
            prefix,
            n == len(parts),
        )
        for n in range(1, len(parts) + 1)
        for prefix in ["/".join(parts[:n])]
    ]


class MusicImports:
    def __init__(
        self,
        store: Store,
        access: Access,
        *,
        library_id: str,
        library_path: Path,
        imports_folder: str = "_imports",
        managed_folder: str = "_shijhon",
        limits: ImportLimits = _DEFAULT_LIMITS,
        scan: Scan,
        native_lookup: NativeLookup,
        refresh: Refresh,
        native_song: NativeSong | None = None,
        artwork: Artwork | None = None,
        minimum: MinimumOf | None = None,
        catalog: CatalogVersions | None = None,
        catalog_audio: CatalogSongAudio | None = None,
        write_gate: Callable[[], Awaitable[None]] | None = None,
        forget: Callable[[str], None] | None = None,
    ) -> None:
        if not library_id:
            raise ImportError("invalid", "Choose a configured music library.")
        self.store, self.access, self.library_id = store, access, library_id
        self.limits, self.scan, self.native_lookup, self.refresh = (
            limits,
            scan,
            native_lookup,
            refresh,
        )
        self.native_song, self.artwork = native_song, artwork
        # The importer's minimum quality (by user ID): the quality gate (``overlap.py``)
        # and the catalog songs their files may go into; without it nothing is gated.
        self.minimum, self.catalog = minimum, catalog
        # The placeholder engine's way to put an import's audio in place of the catalog
        # song it matched (``catalog_audio.py``).
        self.catalog_audio = catalog_audio
        # Navidrome's settings checked right before a published file is replaced (keeping
        # a missing file's identity), raising when they refuse (``PlaceholderWrites``).
        self.write_gate = write_gate
        # The playback quality gate's cached facts of a song whose audio changed.
        self.forget = forget
        self.upgrades = Upgrades(self)
        self.storage = Storage(library_path, imports_folder, managed_folder)
        self.assets = AssetResolver(
            store,
            library_id=library_id,
            library_path=library_path,
            imports_folder=imports_folder,
            managed_folder=managed_folder,
        )
        self.config_id = hashlib.sha256(
            json.dumps(
                [library_id, str(library_path), imports_folder, managed_folder, asdict(limits)],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self._locks, self._worker = KeyedLocks(), anyio.Lock()
        self._uploads: set[str] = set()
        # Catalog songs whose import audio waits: item ID -> (not before, attempts).
        self._later: dict[str, tuple[float, int]] = {}
        self._wake = anyio.Event()
        self._ready = False

    async def initialize(self) -> None:
        if self._ready:
            return
        async with self.store.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO music_import_installation VALUES (1,?)",
                [secrets.token_hex(16)],
            )
        row = await self.store.fetchone(
            "SELECT id FROM music_import_installation WHERE singleton=1"
        )
        assert row
        await anyio.to_thread.run_sync(self.storage.open, row["id"], self.library_id)
        try:
            # Every unfinished attempt restarts from zero. Discard its private bytes
            # before declaring it retryable, whether Stop precedes or follows restart.
            while True:
                unfinished = await self.store.fetchall(
                    "SELECT i.* FROM music_import_items i"
                    " JOIN music_import_jobs j ON j.id=i.job_id"
                    " WHERE i.state='uploading'"
                    " AND j.library_id=? AND j.root_id=? LIMIT 100",
                    [self.library_id, self.storage.root_id],
                )
                if not unfinished:
                    break
                for item in unfinished:
                    await self._discard_upload(dict(item), item["attempt"], "interrupted")
            # Copies a new review gave up before a stop: their bytes, then their rows.
            while await self._discard_copies():
                pass
            async with self.store.transaction() as conn:
                await conn.execute(
                    "UPDATE music_import_jobs SET"
                    " resume_state=state,state='paused',error='interrupted'"
                    " WHERE state IN ('checking','approved','publishing')"
                )
            self._ready = True
        except BaseException:
            self.storage.close()
            raise

    async def aclose(self) -> None:
        # Parent cancels/joins the worker and requests before releasing this root lock.
        self._ready = False
        self.storage.close()

    async def _actor(self, owner: str, *, write: bool = True) -> None:
        try:
            actor = await self.access.authorize(owner)
            if not owner or actor.owner_id != owner or self.library_id not in actor.library_ids:
                raise ValueError
        except Exception:
            raise ImportError(
                "access", "Your current music library access could not be verified."
            ) from None
        if write:
            policy = await self.store.fetchone(
                "SELECT mode FROM collection_library_policy WHERE library_id=?", [self.library_id]
            )
            if not policy or policy["mode"] != "personal":
                raise ImportError(
                    "personal_setup_required",
                    "Finish personal library setup before importing music.",
                )
            if not self._ready:
                raise ImportError("storage", "The dedicated imports root is unavailable.")
            self.storage.check()

    async def _job(self, owner: str, ident: str, *, write: bool = True) -> dict[str, Any]:
        await self._actor(owner, write=write)
        row = await self.store.fetchone(
            "SELECT * FROM music_import_jobs WHERE id=? AND owner_id=? AND library_id=?",
            [ident, owner, self.library_id],
        )
        if not row:
            raise ImportError("not_found", "This import is unavailable.")
        job = dict(row)
        if write and (job["root_id"] != self.storage.root_id or job["config_id"] != self.config_id):
            raise ImportError(
                "configuration_changed",
                "Import storage or limits changed. Restore its configuration.",
            )
        return job

    async def capabilities(self, owner_id: str) -> dict[str, Any]:
        await self._actor(owner_id)
        return {
            "enabled": True,
            "ready": True,
            "formats": list(FORMATS),
            "native_rejected_formats": dict(NATIVE_REJECTED),
            "manifest_page_size": MANIFEST_PAGE_SIZE,
            "limits": asdict(self.limits),
        }

    async def asset_lookup(
        self, library_id: str, songs: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, str]:
        """Trusted read-only structural bridge, including when intake is stopped."""
        return await self.assets(library_id, songs)

    async def create(self, owner_id: str, request_key: str) -> dict[str, Any]:
        _key(request_key)
        await self._actor(owner_id)
        async with self._locks.hold("admission"):
            await self._actor(owner_id)
            existing = await self.store.fetchone(
                "SELECT id FROM music_import_jobs WHERE owner_id=? AND request_key=?",
                [owner_id, request_key],
            )
            if existing:
                return await self.status(owner_id, existing["id"])
            jobs = await self.store.fetchone(
                "SELECT count(*) AS n FROM music_import_jobs WHERE owner_id=?"
                " AND state NOT IN ('done','cancelled')",
                [owner_id],
            )
            if jobs and jobs["n"] >= self.limits.max_owner_jobs:
                raise ImportError("jobs_limit", "Finish or stop an existing import first.")
            ident = secrets.token_hex(16)
            await self.store.execute(
                "INSERT INTO music_import_jobs"
                "(id,owner_id,library_id,root_id,config_id,request_key,state,created)"
                " VALUES (?,?,?,?,?,?,'declaring',?)",
                [
                    ident,
                    owner_id,
                    self.library_id,
                    self.storage.root_id,
                    self.config_id,
                    request_key,
                    time.time(),
                ],
            )
        return await self.status(owner_id, ident)

    async def _capacity(self, owner: str, added: int = 0) -> None:
        totals = await self.store.fetchone(
            "SELECT coalesce(sum(i.reserved_size),0) AS global_bytes,"
            "coalesce(sum(CASE WHEN j.owner_id=? THEN i.reserved_size ELSE 0 END),0) AS"
            " owner_bytes,"
            "coalesce(sum(max(0,i.reserved_size-i.received_size)),0) AS pending_bytes"
            " FROM music_import_items i JOIN music_import_jobs j ON j.id=i.job_id",
            [owner],
        )
        assert totals
        if (
            totals["global_bytes"] + added > self.limits.max_global_bytes
            or totals["owner_bytes"] + added > self.limits.max_owner_bytes
        ):
            raise ImportError(
                "bytes_limit", "This import exceeds the reserved music storage limit."
            )
        if self.storage.free() - totals["pending_bytes"] - added < self.limits.min_free_bytes:
            raise ImportError("space", "There is not enough unreserved space for this import.")

    async def declare(
        self, owner_id: str, job_id: str, page_key: str, entries: Sequence[ManifestEntry]
    ) -> dict[str, Any]:
        _key(page_key)
        if not 1 <= len(entries) <= MANIFEST_PAGE_SIZE:
            raise ImportError("manifest_limit", "Declare at most 500 files in one page.")
        for entry in entries:
            if not isinstance(entry, ManifestEntry):
                raise ImportError("invalid", "Declare typed audio file entries.")
            _key(entry.client_key)
            _path(entry.path)
            if (
                type(entry.size) is not int
                or not 0 < entry.size <= self.limits.max_file_bytes
                or not isinstance(entry.sha256, str)
                or (entry.sha256 and not _DIGEST.fullmatch(entry.sha256))
            ):
                raise ImportError(
                    "file_limit", "Choose audio within the file size and digest limits."
                )
        if len({entry.client_key for entry in entries}) != len(entries):
            raise ImportError("conflict", "A file key is repeated in this manifest page.")
        fingerprint = hashlib.sha256(
            json.dumps(
                [asdict(entry) for entry in entries],
                sort_keys=True,
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        async with self._locks.hold("admission"):
            job = await self._job(owner_id, job_id)
            previous = await self.store.fetchone(
                "SELECT fingerprint FROM music_import_pages WHERE job_id=? AND page_key=?",
                [job_id, page_key],
            )
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ImportError(
                        "conflict", "This manifest page key already has different files."
                    )
                return await self.status(owner_id, job_id)
            if job["state"] != "declaring":
                raise ImportError("sealed", "This import's file list is already sealed.")
            # A bounded incoming page is checked against the existing manifest in
            # small cursor pages. Keep names exact; reject ambiguity on case-folding
            # or Unicode-normalizing filesystems, and file/folder prefix conflicts.
            nodes: dict[str, tuple[str, bool]] = {}
            for entry in entries:
                for key, exact, leaf in _path_nodes(entry.path):
                    previous_node = nodes.get(key)
                    if previous_node and (previous_node != (exact, False) or leaf):
                        raise ImportError("path_collision", "File paths conflict in this import.")
                    nodes[key] = exact, leaf
            async with self.store.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT display_path FROM music_import_items WHERE job_id=?", [job_id]
                )
                try:
                    while page := await cursor.fetchmany(MANIFEST_PAGE_SIZE):
                        for row in page:
                            for key, exact, leaf in _path_nodes(row["display_path"]):
                                incoming = nodes.get(key)
                                if incoming and (incoming != (exact, False) or leaf):
                                    raise ImportError(
                                        "path_collision", "File paths conflict in this import."
                                    )
                finally:
                    await cursor.close()
            count = await self.store.fetchone(
                "SELECT count(*) AS n,coalesce(sum(expected_size),0) AS b"
                " FROM music_import_items WHERE job_id=?",
                [job_id],
            )
            assert count
            added = sum(e.size for e in entries)
            if count["n"] + len(entries) > self.limits.max_job_files or (
                count["b"] + added > self.limits.max_job_bytes
            ):
                raise ImportError(
                    "job_limit", "This import exceeds the configured files or bytes limit."
                )
            await self._capacity(owner_id, added)
            await self._actor(owner_id)
            try:
                async with self.store.transaction() as conn:
                    cursor = await conn.execute(
                        "SELECT state FROM music_import_jobs WHERE id=?", [job_id]
                    )
                    current = await cursor.fetchone()
                    if not current or current["state"] != "declaring":
                        raise ImportError("sealed", "This import's file list is already sealed.")
                    await conn.execute(
                        "INSERT INTO music_import_pages VALUES (?,?,?)",
                        [job_id, page_key, fingerprint],
                    )
                    for entry in entries:
                        asset = secrets.token_hex(16)
                        fmt = entry.path.rsplit(".", 1)[-1].lower()
                        ignored = scanner_ignored_path(entry.path)
                        supported = fmt in FORMATS and not ignored
                        error, warnings = "", []
                        if ignored:
                            error = "native_ignored"
                            warnings = [
                                "The stock Navidrome scanner ignores this filename or folder. "
                                "No original was uploaded or published."
                            ]
                        elif fmt in NATIVE_REJECTED:
                            error, warnings = "native_unsupported", [NATIVE_REJECTED[fmt]]
                        elif not supported:
                            error = "unsupported"
                        await conn.execute(
                            "INSERT INTO music_import_items"
                            "(id,asset_id,job_id,client_key,display_path,expected_size,reserved_size,"
                            "expected_digest,state,staging_name,final_name,error,warnings)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [
                                secrets.token_hex(16),
                                asset,
                                job_id,
                                entry.client_key,
                                entry.path,
                                entry.size,
                                entry.size if supported else 0,
                                entry.sha256,
                                "declared" if supported else "rejected",
                                asset + ".part",
                                "batch-" + job_id + "/" + entry.path,
                                error,
                                json.dumps(warnings),
                            ],
                        )
            except sqlite3.IntegrityError:
                raise ImportError("conflict", "A manifest file key was already declared.") from None
        return await self.status(owner_id, job_id)

    async def seal(self, owner_id: str, job_id: str) -> dict[str, Any]:
        async with self._locks.hold("admission"):
            job = await self._job(owner_id, job_id)
            if job["state"] == "declaring":
                count = await self.store.fetchone(
                    "SELECT 1 FROM music_import_items WHERE job_id=? LIMIT 1", [job_id]
                )
                if not count:
                    raise ImportError("empty", "Choose some audio files first.")
                await self.store.execute(
                    "UPDATE music_import_jobs SET state='uploading' WHERE id=? AND"
                    " state='declaring'",
                    [job_id],
                )
        return await self.status(owner_id, job_id)

    async def _item(self, job_id: str, item_id: str) -> dict[str, Any]:
        row = await self.store.fetchone(
            "SELECT * FROM music_import_items WHERE id=? AND job_id=?", [item_id, job_id]
        )
        if not row:
            raise ImportError("not_found", "This import file is unavailable.")
        return dict(row)

    async def receive(
        self,
        owner_id: str,
        job_id: str,
        item_id: str,
        attempt: int,
        stream: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        # Admission is entirely before the first stream.__aiter__/__anext__ call.
        async with self._locks.hold("admission"):
            job = await self._job(owner_id, job_id)
            item = await self._item(job_id, item_id)
            if type(attempt) is not int or item["attempt"] != attempt:
                raise ImportError("attempt", "Refresh this import before retrying the file.")
            if item["state"] in _FINISHED:
                return await self.status(owner_id, job_id)
            if job["state"] != "uploading" or item["state"] != "declared":
                raise ImportError("state", "This file is not awaiting an upload.")
            if len(self._uploads) >= self.limits.max_uploads or item_id in self._uploads:
                raise ImportError("busy", "The upload slots are busy. Retry this file shortly.")
            await self._capacity(owner_id)
            await self._actor(owner_id)
            await self.store.execute(
                "UPDATE music_import_items SET state='uploading',error='' WHERE id=?", [item_id]
            )
            descriptor = -1
            try:
                self.storage.discard_attempt(item["staging_name"])
                descriptor = self.storage.file(item["staging_name"], create=True)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self.store.execute(
                        "UPDATE music_import_items SET state='declared',attempt=attempt+1,"
                        "error='storage' WHERE id=?",
                        [item_id],
                    )
                raise ImportError(
                    "storage", "The private upload file could not be opened."
                ) from None
            self._uploads.add(item_id)
        error = "interrupted"
        try:
            hashed, count = hashlib.sha256(), 0
            recheck_at = time.monotonic() + UPLOAD_AUTH_SECONDS
            with anyio.fail_after(self.limits.upload_seconds):
                async for chunk in stream:
                    if not isinstance(chunk, bytes) or len(chunk) > 8 * 1024**2:
                        raise ImportError(
                            "chunk", "The upload returned an invalid or oversized chunk."
                        )
                    count += len(chunk)
                    if count > item["expected_size"]:
                        raise ImportError("size", "The file exceeds its declared size.")
                    # Native identity/grants are fresh at admission, periodically while
                    # receiving, and before accepting the file. Local stop/storage
                    # checks still run for every chunk, independent of network pacing.
                    if time.monotonic() >= recheck_at:
                        await self._job(owner_id, job_id)
                        recheck_at = time.monotonic() + UPLOAD_AUTH_SECONDS
                    state = await self.store.fetchone(
                        "SELECT state FROM music_import_jobs WHERE id=?", [job_id]
                    )
                    if not state or state["state"] != "uploading":
                        raise ImportError("stopped", "This import was stopped.")
                    if self.storage.free() < self.limits.min_free_bytes + len(chunk):
                        raise ImportError("space", "There is not enough free space to continue.")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    hashed.update(chunk)
                digest = hashed.hexdigest()
                if count != item["expected_size"]:
                    raise ImportError("size", "The upload ended before the declared file size.")
                if item["expected_digest"] and digest != item["expected_digest"]:
                    raise ImportError(
                        "digest", "The file does not match its declared SHA-256 digest."
                    )
                os.fsync(descriptor)
                os.fsync(self.storage.incoming)
                current = await self._job(owner_id, job_id)
                if current["state"] != "uploading":
                    raise ImportError("stopped", "This import was stopped.")
                async with self.store.transaction() as conn:
                    cursor = await conn.execute(
                        "SELECT j.state AS job_state,i.state,i.attempt,i.expected_size,"
                        "i.reserved_size FROM music_import_items i"
                        " JOIN music_import_jobs j ON j.id=i.job_id"
                        " WHERE j.id=? AND j.owner_id=? AND j.library_id=?"
                        " AND j.root_id=? AND j.config_id=? AND i.id=?",
                        [
                            job_id,
                            owner_id,
                            self.library_id,
                            self.storage.root_id,
                            self.config_id,
                            item_id,
                        ],
                    )
                    live = await cursor.fetchone()
                    if not live or live["job_state"] != "uploading":
                        raise ImportError("stopped", "This import was stopped.")
                    if live["state"] != "uploading" or live["attempt"] != attempt:
                        raise ImportError("attempt", "The active upload attempt changed.")
                    if live["expected_size"] != count or live["reserved_size"] != count:
                        raise ImportError("reservation", "The upload reservation changed.")
                    await conn.execute(
                        "UPDATE music_import_items SET state='received',received_size=?,"
                        "digest=?,reserved_size=? WHERE id=? AND state='uploading' AND attempt=?",
                        [count, digest, count, item_id, attempt],
                    )
        except BaseException as exc:
            if isinstance(exc, ImportError):
                error = exc.code
            with anyio.CancelScope(shield=True):
                await self._discard_upload(item, attempt, error)
            if isinstance(exc, (OSError, TimeoutError)):
                raise ImportError(
                    "upload", "The upload could not finish. Retry the whole file."
                ) from None
            raise
        finally:
            os.close(descriptor)
            self._uploads.discard(item_id)
        return await self.status(owner_id, job_id)

    async def _discard_upload(self, item: dict[str, Any], attempt: int, error: str) -> None:
        """Discard only this unfinished attempt, before releasing cancelled capacity.

        Inspect durable state even after an uncertain commit: a completed receive
        keeps its bytes and reservation when cancellation arrives just after COMMIT.
        """
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT i.state,i.attempt,j.state AS job_state FROM music_import_items i"
                " JOIN music_import_jobs j ON j.id=i.job_id WHERE i.id=? AND i.job_id=?",
                [item["id"], item["job_id"]],
            )
            live = await cursor.fetchone()
            if not live or live["state"] != "uploading" or live["attempt"] != attempt:
                return
            self.storage.discard_attempt(item["staging_name"])
            await conn.execute(
                "UPDATE music_import_items SET state='declared',attempt=attempt+1,"
                "received_size=0,digest='',error=?,"
                "reserved_size=CASE WHEN ?='cancelled' THEN 0 ELSE reserved_size END"
                " WHERE id=? AND state='uploading' AND attempt=?",
                [error, live["job_state"], item["id"], attempt],
            )

    async def _probe(self, item: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        """The file's warnings and bounded facts (``facts.py``)."""
        result = await self._run_probe(item)
        warnings, facts = result["warnings"], result.get("facts", {})
        if (
            not isinstance(warnings, list)
            or any(not isinstance(w, str) for w in warnings)
            or not isinstance(facts, dict)
        ):
            raise ImportError("invalid_audio", "Audio headers or codec could not be validated.")
        return warnings, facts

    async def _cover(self, item: dict[str, Any]) -> int | None:
        """The fingerprint of a received file's embedded cover (None: none or unreadable)."""
        try:
            found = (await self._run_probe(item, "cover")).get("cover")
        except ImportError:
            return None
        return found if isinstance(found, int) else None

    async def _run_probe(self, item: dict[str, Any], mode: str = "") -> dict[str, Any]:
        descriptor = self.storage.file(item["staging_name"])
        process: anyio.abc.Process | None = None
        try:
            with anyio.fail_after(self.limits.validation_seconds):
                process = await anyio.open_process(
                    [
                        sys.executable,
                        "-m",
                        "shijhon.music_imports.probe",
                        str(descriptor),
                        item["display_path"].rsplit(".", 1)[-1].lower(),
                        *([mode] if mode else []),
                    ],
                    pass_fds=(descriptor,),
                    start_new_session=True,
                    stderr=-3,
                )
                assert process.stdout
                output = bytearray()
                try:
                    while True:
                        output.extend(await process.stdout.receive(4096))
                        if len(output) > 64 * 1024:
                            raise ValueError
                except anyio.EndOfStream:
                    pass
                if await process.wait() != 0:
                    raise ValueError
                result = json.loads(output)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except (ValueError, KeyError, TimeoutError):
            raise ImportError(
                "invalid_audio", "Audio headers or codec could not be validated."
            ) from None
        finally:
            os.close(descriptor)
            if process:
                with anyio.CancelScope(shield=True):
                    # Include ffprobe even if the parent died on a resource limit.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.aclose()

    async def _verify(self, item: dict[str, Any], *, final: bool = False) -> None:
        await anyio.to_thread.run_sync(
            lambda: self.storage.verify(
                item["final_name"] if final else item["staging_name"],
                item["expected_size"],
                item["digest"],
                final=final,
            )
        )

    async def review(
        self, owner_id: str, job_id: str, *, exclude: Sequence[str] = ()
    ) -> dict[str, Any]:
        """Queue bounded private checking. No parser/hash work runs in this request."""
        if len(exclude) > self.limits.max_job_files or len(set(exclude)) != len(exclude):
            raise ImportError("invalid", "Choose a bounded set of files to exclude.")
        for item_id in exclude:
            _key(item_id)
        async with self._locks.hold(job_id):
            job = await self._job(owner_id, job_id)
            if job["state"] not in ("uploading", "review", "checking"):
                raise ImportError("state", "Finish the uploads before reviewing this import.")
            pending = await self.store.fetchone(
                "SELECT 1 FROM music_import_items WHERE job_id=?"
                " AND state IN ('declared','uploading') LIMIT 1",
                [job_id],
            )
            if pending:
                raise ImportError("unfinished", "Finish or retry unfinished files before review.")
            if job["state"] == "checking":
                current_exclusions = await self.store.fetchall(
                    "SELECT id FROM music_import_items WHERE job_id=? AND review_excluded=1",
                    [job_id],
                )
                if {row["id"] for row in current_exclusions} == set(exclude):
                    return await self.status(owner_id, job_id)
            async with self.store.transaction() as conn:
                await self._reset_overlap(conn, job_id)
                await conn.execute(
                    "UPDATE music_import_items SET review_excluded=0 WHERE job_id=?", [job_id]
                )
                await conn.executemany(
                    "UPDATE music_import_items SET review_excluded=1 WHERE job_id=? AND id=?",
                    [(job_id, ident) for ident in exclude],
                )
                cursor = await conn.execute(
                    "SELECT count(*) AS n FROM music_import_items"
                    " WHERE job_id=? AND review_excluded=1",
                    [job_id],
                )
                found = await cursor.fetchone()
                if not found or found["n"] != len(exclude):
                    raise ImportError("invalid", "An excluded file is not part of this import.")
                await conn.execute(
                    "UPDATE music_import_items SET state='received' WHERE job_id=?"
                    " AND state IN ('eligible','excluded')",
                    [job_id],
                )
                await conn.execute(
                    "UPDATE music_import_jobs SET state='checking',error='' WHERE id=?"
                    " AND state IN ('uploading','review','checking')",
                    [job_id],
                )
            await self._discard_reset(job_id)
            self._wake.set()
        return await self.status(owner_id, job_id)

    async def _checking(self, owner: str, job_id: str) -> dict[str, Any]:
        job = await self._job(owner, job_id)
        if job["state"] != "checking":
            raise ImportError("stopped", "This import is no longer awaiting private review.")
        return job

    async def _check_one(self, job: dict[str, Any]) -> None:
        owner, job_id = job["owner_id"], job["id"]
        await self._checking(owner, job_id)
        # Copies an earlier review gave up go before any copy is made again.
        while await self._discard_copies(job_id):
            pass
        # Files checked before the review compared files with the library have no facts:
        # they are read again.
        await self.store.execute(
            "UPDATE music_import_items SET state='received' WHERE job_id=? AND state='eligible'"
            " AND derived_from='' AND facts=''",
            [job_id],
        )
        row = await self.store.fetchone(
            "SELECT * FROM music_import_items WHERE job_id=? AND state='received'"
            " ORDER BY id LIMIT 1",
            [job_id],
        )
        if row:
            item = dict(row)
            warnings: list[str] = []
            facts: dict[str, Any] = {}
            error = ""
            try:
                await self._verify(item)
                warnings, facts = await self._probe(item)
                if _keep_files(item):
                    facts["keep_files"] = True  # the person's choice survives a new check
                state = "excluded" if item["review_excluded"] else "eligible"
            except (ImportError, OSError) as exc:
                state = "rejected"
                error = exc.code if isinstance(exc, ImportError) else "changed"
            await self._checking(owner, job_id)
            await self.store.execute(
                "UPDATE music_import_items SET state=?,warnings=?,error=?,facts=?,outcome='',"
                "match='' WHERE id=?",
                [state, json.dumps(warnings), error, json.dumps(facts), item["id"]],
            )
        pending = await self.store.fetchone(
            "SELECT 1 FROM music_import_items WHERE job_id=? AND state='received' LIMIT 1", [job_id]
        )
        if pending:
            return
        # Every file is checked: compare them with the library once, then make the
        # copies that join existing albums one per tick, then freeze the review.
        unclassified = await self.store.fetchone(
            "SELECT 1 FROM music_import_items WHERE job_id=? AND state='eligible'"
            " AND outcome='' AND derived_from='' LIMIT 1",
            [job_id],
        )
        if unclassified:
            await self._classify(job)
        copying = await self.store.fetchone(
            "SELECT * FROM music_import_items WHERE job_id=? AND state='copying'"
            " ORDER BY id LIMIT 1",
            [job_id],
        )
        if copying:
            await self._copy(job, dict(copying))
            more = await self.store.fetchone(
                "SELECT 1 FROM music_import_items WHERE job_id=? AND state='copying' LIMIT 1",
                [job_id],
            )
            if more:
                return
        fingerprint = await self._fingerprint(job_id)
        await self._checking(owner, job_id)
        await self.store.execute(
            "UPDATE music_import_jobs SET state='review',"
            "revision=revision+CASE WHEN fingerprint<>? THEN 1 ELSE 0 END,"
            "fingerprint=?,approved_revision=NULL,approval_key=NULL,approved_at=NULL,error=''"
            " WHERE id=? AND state='checking'",
            [fingerprint, fingerprint, job_id],
        )

    # --- music the library already has (``overlap.py``) -------------------------------

    async def _library(self, names: set[str], *, without: set[str]) -> list[LibrarySong]:
        """Present songs of the albums named like ``names`` (folded), from the shared
        structural index, in bounded pages, except the songs ``without`` (track keys).
        Catalog songs Shijhon added are marked."""
        found: list[LibrarySong] = []
        after = 0
        while names:
            rows = await self.store.fetchall(
                "SELECT rowid,song_id,track_key,album_id,title,album,artist,year,duration,"
                "original,metadata FROM collection_tracks WHERE library_id=? AND present=1"
                " AND rowid>? ORDER BY rowid LIMIT 5000",
                [self.library_id, after],
            )
            if not rows:
                break
            after = rows[-1]["rowid"]

            def named(page: Sequence[Any]) -> list[dict[str, Any]]:
                return [
                    dict(r)
                    for r in page
                    if fold(r["album"]) in names and r["track_key"] not in without
                ]

            kept = await anyio.to_thread.run_sync(named, rows)
            if not kept:
                continue
            placeholders = {
                r["song_id"]
                for r in await self.store.fetchall(
                    "SELECT song_id FROM placeholders WHERE song_id IN"
                    " (SELECT value FROM json_each(?))",
                    [json.dumps([r["song_id"] for r in kept])],
                )
            }
            for row in kept:
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except ValueError:
                    metadata = {}
                catalog = row["song_id"] in placeholders or not row["original"]
                found.append(LibrarySong.of_index(row, metadata, placeholder=catalog))
        return found

    async def _own_tracks(self, job_id: str) -> set[str]:
        """The library songs this import published itself (its own files and copies, by
        their import aliases): the review compares a file with the rest of the library."""
        rows = await self.store.fetchall(
            "SELECT a.track_key FROM music_import_items i JOIN collection_track_aliases a"
            " ON a.library_id=? AND a.alias='import:'||i.asset_id WHERE i.job_id=?"
            " UNION SELECT t.track_key FROM music_import_items i JOIN collection_tracks t"
            " ON t.library_id=? AND t.song_id=i.song_id WHERE i.job_id=? AND i.song_id<>''",
            [self.library_id, job_id, self.library_id, job_id],
        )
        return {str(r["track_key"]) for r in rows}

    async def _imported(self, job_id: str, digests: Sequence[str]) -> dict[str, LibrarySong]:
        """Earlier imports' present songs by the exact digest of their published bytes,
        while that published file still holds them (``Storage.holds``: the retained file,
        or one of the same size and digest put in its place). Evidence that cannot be told
        either way stops the import (``identity``): never published on it."""
        if not digests:
            return {}
        rows = await self.store.fetchall(
            "SELECT i.digest,i.final_name,i.staging_name,i.expected_size,t.song_id,t.track_key,"
            "t.album_id,t.title,t.album,t.artist,t.year,t.duration,t.original,t.metadata"
            " FROM music_import_items i JOIN music_import_jobs j ON j.id=i.job_id"
            " JOIN collection_tracks t ON t.library_id=j.library_id AND t.song_id=i.song_id"
            " AND t.present=1 WHERE j.library_id=? AND j.root_id=? AND i.state='complete'"
            " AND i.job_id<>? AND i.digest IN (SELECT value FROM json_each(?))",
            [self.library_id, self.storage.root_id, job_id, json.dumps(sorted(set(digests)))],
        )
        found: dict[str, LibrarySong] = {}
        untold: dict[str, ImportError] = {}
        for row in rows:
            if row["digest"] in found:
                continue
            try:
                held = await anyio.to_thread.run_sync(
                    self.storage.holds,
                    row["final_name"],
                    row["staging_name"],
                    row["expected_size"],
                    row["digest"],
                )
            except ImportError as exc:
                if exc.code != "identity":
                    raise
                untold.setdefault(row["digest"], exc)
                continue
            if not held:
                continue
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except ValueError:
                metadata = {}
            found[row["digest"]] = LibrarySong.of_index(dict(row), metadata, placeholder=False)
        for digest, error in untold.items():
            if digest not in found:  # (another import's file holding them will do)
                raise error
        return found

    async def _album_tags(self, owner: str, album: LibraryAlbum) -> Tags | None:
        """The album-level tags of one of the album's files, as Navidrome reads them (the
        importing caller's native read gives its path). None when none can be read."""
        if self.native_song is None:
            return None
        for song in album.songs[:3]:
            try:
                row = await self.native_song(owner, self.library_id, song.song_id)
                path = row.get("path")
                if (
                    not isinstance(path, str)
                    or not path
                    or path.startswith("/")
                    or ".." in path.split("/")
                ):
                    continue
                return await anyio.to_thread.run_sync(
                    tagmap.album_tags_at, self.storage.library / path
                )
            except Exception:  # noqa: S112 - an unreadable existing file: try the next one
                continue
        return None

    async def _covers_differ(
        self,
        owner: str,
        items: Sequence[Incoming],
        other: LibraryAlbum | Sequence[Incoming],
        seen: dict[str, int | None],
    ) -> bool | None:
        """Whether the covers of incoming files and of another album clearly differ: a
        library album's (a song's own picture, read by the importing caller) or other
        incoming files'. ``seen``: fingerprints already worked out in this comparison."""

        async def incoming(group: Sequence[Incoming]) -> int | None:
            pictured = next((i for i in group if i.facts.get("picture")), None)
            if pictured is None:
                return None
            if pictured.id not in seen:
                row = await self.store.fetchone(
                    "SELECT * FROM music_import_items WHERE id=?", [pictured.id]
                )
                seen[pictured.id] = await self._cover(dict(row)) if row else None
            return seen[pictured.id]

        if isinstance(other, LibraryAlbum):
            if self.artwork is None or not other.cover:
                return None
            mine = await incoming(items)
            if mine is None:
                return None
            if "cover:" + other.cover not in seen:
                try:
                    data, _ = await self.artwork(owner, self.library_id, other.cover)
                except Exception:  # no cover answer: cannot tell
                    seen["cover:" + other.cover] = None
                else:
                    seen["cover:" + other.cover] = await covers.fingerprint(data)
            return covers.differ(mine, seen["cover:" + other.cover])
        mine = await incoming(items)
        return covers.differ(mine, await incoming(other)) if mine is not None else None

    async def _context(
        self, job_id: str, rows: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """The files of this import a decision about ``rows`` is made among: every file
        the review compared (also those already published or linked) named like them, as
        the review compared them together."""
        names = {fold(str(_facts(r).get("album") or "")) for r in rows} - {""}
        wanted = {r["id"] for r in rows}
        found: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page = await self.store.fetchall(
                "SELECT id,facts,digest,distinct_song FROM music_import_items WHERE job_id=?"
                " AND derived_from='' AND id>? AND state NOT IN"
                " ('declared','uploading','received','rejected','excluded') ORDER BY id LIMIT 500",
                [job_id, cursor],
            )
            if not page:
                break
            cursor = page[-1]["id"]
            found.extend(
                dict(r)
                for r in page
                if r["id"] in wanted or fold(str(_facts(dict(r)).get("album") or "")) in names
            )
        return found

    async def _decide(
        self,
        owner: str,
        job_id: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        gate: bool = True,
    ) -> dict[str, Decision]:
        """The review's decisions (``overlap.py``) for ``rows`` - files of this import -
        against the library as a completed caller-owned refresh shows it now, without the
        songs this import published itself."""
        incoming = []
        for row in rows:
            facts = _facts(row)
            incoming.append(Incoming(row["id"], facts, row["digest"], bool(row["distinct_song"])))
        names = {fold(i.album) for i in incoming if fold(i.album)}
        if names:
            # Albums are compared with the library as it is now, also with music added to
            # the folder since the last refresh. (A file without an album name is only
            # ever linked to exactly its bytes, an import whose receipt the index has.)
            try:
                await self.refresh(owner, self.library_id)
            except CollectionError:
                raise ImportError(
                    "access", "The library could not be read with your access. Sign in again."
                ) from None
        library = await self._library(names, without=await self._own_tracks(job_id))
        imported = await self._imported(job_id, [i.digest for i in incoming if i.digest])
        seen: dict[str, int | None] = {}

        async def differ(
            items: Sequence[Incoming], other: LibraryAlbum | Sequence[Incoming]
        ) -> bool | None:
            return await self._covers_differ(owner, items, other, seen)

        async def tags(album: LibraryAlbum) -> Tags | None:
            return await self._album_tags(owner, album)

        minimum = await self.minimum(owner) if gate and self.minimum is not None else None
        return await classify(
            incoming,
            library,
            imported=imported,
            covers_differ=differ,
            album_tags=tags,
            minimum=minimum,
            catalog=self.catalog.find if self.catalog is not None else None,
        )

    async def _classify(self, job: dict[str, Any]) -> None:
        """Decide what every checked file does (``overlap.py``), before the review. Files
        an earlier approval already published or linked are compared too (as the review
        compared them) but keep what they are."""
        owner, job_id = job["owner_id"], job["id"]
        rows = await self.store.fetchall(
            "SELECT id,facts,digest,distinct_song FROM music_import_items WHERE job_id=?"
            " AND state='eligible' AND derived_from='' ORDER BY id",
            [job_id],
        )
        context = await self._context(job_id, [dict(r) for r in rows])
        decisions = await self._decide(owner, job_id, context)
        await self._checking(owner, job_id)
        async with self.store.transaction() as conn:
            for row in rows:
                decision = decisions.get(row["id"]) or Decision(NEW)
                evidence = decision.evidence(Incoming(row["id"], _facts(dict(row))))
                await conn.execute(
                    "UPDATE music_import_items SET outcome=?,match=?,state=?"
                    " WHERE id=? AND state='eligible'",
                    [decision.outcome, json.dumps(evidence), _state_of(decision), row["id"]],
                )

    async def _copy_source(self, owner: str, match: Mapping[str, Any]) -> tuple[Tags | None, str]:
        source = str(match.get("tags_from") or "")
        if source.startswith("album:"):
            rows = await self.store.fetchall(
                "SELECT song_id,track_key,album_id,title,album,artist,year,duration,original,"
                "metadata FROM collection_tracks WHERE library_id=? AND album_id=? AND present=1"
                " ORDER BY original DESC,song_id LIMIT 3",
                [self.library_id, source[len("album:") :]],
            )
            songs = tuple(
                LibrarySong.of_index(dict(r), json.loads(r["metadata"] or "{}"), placeholder=False)
                for r in rows
            )
            if not songs:
                return None, ""
            album = LibraryAlbum(songs[0].album_id, songs)
            return await self._album_tags(owner, album), album.artist
        if source.startswith("item:"):
            row = await self.store.fetchone(
                "SELECT facts FROM music_import_items WHERE id=?", [source[len("item:") :]]
            )
            facts = _facts(dict(row)) if row else {}
            tags = facts.get("album_tags")
            if isinstance(tags, dict) and tags:
                return tags, album_artist(tags, str(facts.get("artist") or ""))
        return None, ""

    async def _copy(self, job: dict[str, Any], item: dict[str, Any]) -> None:
        """Make one copy carrying an existing album's tags; the received original stays
        unchanged. A copy that cannot be made holds the file back (reviewable). The copy
        is recorded, with its reservation, before its bytes are written (``_plan_copy``):
        whatever stops it, its bytes are found and deleted (``_discard_copies``)."""
        owner, job_id = job["owner_id"], job["id"]
        match = json.loads(item["match"] or "{}")
        tags, artist = await self._copy_source(owner, match)
        result: dict[str, Any] | None = None
        planned: tuple[str, str] | None = None
        if tags is not None:
            try:
                await self._capacity(owner, item["expected_size"] + COPY_SLACK)
                await self._verify(item)
                planned = await self._plan_copy(job, item)
                result = await self._retag(item, planned[1], tags, artist)
            except ImportError as exc:
                if exc.code not in ("bytes_limit", "space", "invalid_audio"):
                    raise
                result = None
        await self._checking(owner, job_id)
        if result is None:
            # (Its bytes, if any, then its record.)
            while await self._discard_copies(job_id):
                pass
            match["reason"] = (
                "Not added: Shijhon could not give this file the album's details, and on its"
                " own it would show as a second album of that name."
            )
            await self.store.execute(
                "UPDATE music_import_items SET state='skipped',outcome=?,match=?"
                " WHERE id=? AND state='copying'",
                [HELD, json.dumps(match), item["id"]],
            )
            return
        assert planned is not None
        async with self.store.transaction() as conn:
            cursor = await conn.execute("SELECT state FROM music_import_jobs WHERE id=?", [job_id])
            current = await cursor.fetchone()
            if not current or current["state"] != "checking":
                raise ImportError("stopped", "This import is no longer awaiting private review.")
            await self._copied(conn, item, planned[0], result)

    async def _plan_copy(self, job: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
        """Record the copy about to be made of ``item`` before any of its bytes exist: a
        row of its own, ``discarding`` - never shown, counted or published - with its
        private name and a reservation of the original's size and ``COPY_SLACK``, until
        the copy is complete (``_copied``). Startup, the idle worker and the next check of
        the import delete the bytes of such a row, and then the row (``_discard_copies``),
        whatever stopped the copy or reviewed the import again meanwhile. Returns the
        row's ID and the copy's private name."""
        ident, token = secrets.token_hex(16), secrets.token_hex(16)
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT state FROM music_import_jobs WHERE id=?", [job["id"]]
            )
            current = await cursor.fetchone()
            if not current or current["state"] != "checking":
                raise ImportError("stopped", "This import is no longer awaiting private review.")
            await conn.execute(
                "INSERT INTO music_import_items"
                "(id,asset_id,job_id,client_key,display_path,expected_size,reserved_size,"
                "state,received_size,digest,staging_name,final_name,facts,outcome,match,"
                "derived_from) VALUES (?,?,?,?,?,?,?,'discarding',0,'',?,?,?,?,?,?)",
                [
                    ident,
                    token,
                    job["id"],
                    "copy." + item["id"],
                    item["display_path"],
                    item["expected_size"],
                    item["expected_size"] + COPY_SLACK,
                    token + ".part",
                    "batch-" + job["id"] + "/.discarded/" + ident,
                    item["facts"],
                    COPY,
                    item["match"],
                    item["id"],
                ],
            )
        return ident, token + ".part"

    async def _copied(
        self, conn: Any, item: dict[str, Any], copy_id: str, result: Mapping[str, Any]
    ) -> None:
        """The copy recorded by ``_plan_copy`` is complete (in the caller's transaction):
        it takes the selected path, reserving its own size, and the original keeps its
        bytes under a receipt name that is never published."""
        cursor = await conn.execute(
            "UPDATE music_import_items SET state='joined',final_name=?"
            " WHERE id=? AND state='copying'",
            ["batch-" + item["job_id"] + "/.received/" + item["id"], item["id"]],
        )
        if cursor.rowcount != 1:
            raise ImportError("stopped", "This import is no longer awaiting private review.")
        cursor = await conn.execute(
            "UPDATE music_import_items SET state='eligible',expected_size=?,reserved_size=?,"
            "received_size=?,digest=?,final_name=? WHERE id=? AND state='discarding'",
            [
                result["size"],
                result["size"],
                result["size"],
                result["sha256"],
                item["final_name"],
                copy_id,
            ],
        )
        if cursor.rowcount != 1:
            raise ImportError("stopped", "This import is no longer awaiting private review.")

    async def _retag(
        self, item: dict[str, Any], name: str, tags: Tags, artist: str
    ) -> dict[str, Any] | None:
        source = self.storage.file(item["staging_name"])
        target = -1
        process: anyio.abc.Process | None = None
        try:
            target = self.storage.copy_target(name)
            with anyio.fail_after(self.limits.validation_seconds * 4):
                process = await anyio.open_process(
                    [sys.executable, "-m", "shijhon.music_imports.retag", str(source), str(target)],
                    pass_fds=(source, target),
                    start_new_session=True,
                    stderr=-3,
                )
                assert process.stdin and process.stdout
                await process.stdin.send(
                    json.dumps(
                        {
                            "tags": {k: list(v) for k, v in tags.items()},
                            "artist": artist,
                            "suffix": item["display_path"].rsplit(".", 1)[-1].lower(),
                        }
                    ).encode()[: 256 * 1024]
                )
                await process.stdin.aclose()
                output = bytearray()
                try:
                    while True:
                        output.extend(await process.stdout.receive(1024))
                        if len(output) > 4096:
                            raise ValueError
                except anyio.EndOfStream:
                    pass
                if await process.wait() != 0:
                    return None
                result = json.loads(output)
                if (
                    not isinstance(result, dict)
                    or type(result.get("size")) is not int
                    or not 0 < result["size"] <= self.limits.max_file_bytes + COPY_SLACK
                    or not _DIGEST.fullmatch(str(result.get("sha256")))
                ):
                    return None
            os.fsync(self.storage.incoming)
            await anyio.to_thread.run_sync(
                lambda: self.storage.verify(name, result["size"], result["sha256"])
            )
            return result
        except (ValueError, TimeoutError, OSError):
            return None
        finally:
            os.close(source)
            if target >= 0:
                os.close(target)
            if process:
                with anyio.CancelScope(shield=True):
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.aclose()

    async def _reset_overlap(self, conn: Any, job_id: str) -> None:
        """Undo a review's decisions before reviewing again: its copies go, retained
        originals take their selected path back. Nothing an approval already placed,
        published or linked changes (a copy being placed keeps its original). A copy's row
        is kept, with its reservation, as ``discarding`` until its bytes are deleted
        (``_discard_reset``); a copy still being made has such a row already
        (``_plan_copy``)."""
        unplaced = (
            " AND NOT EXISTS(SELECT 1 FROM music_import_items d WHERE d.job_id=? AND"
            " d.derived_from=music_import_items.id AND d.state IN ('placing','placed','complete'))"
        )
        # (Out of the way of the original's path and of a new copy's receipt names.)
        await conn.execute(
            "UPDATE music_import_items SET state='discarding',"
            "final_name='batch-'||job_id||'/.discarded/'||id"
            " WHERE job_id=? AND derived_from<>'' AND state='eligible'",
            [job_id],
        )
        await conn.execute(
            "UPDATE music_import_items SET final_name='batch-'||job_id||'/'||display_path"  # noqa: S608
            " WHERE job_id=? AND state='joined'" + unplaced,
            [job_id, job_id],
        )
        await conn.execute(
            "UPDATE music_import_items SET state='eligible',outcome='',match=''"  # noqa: S608
            " WHERE job_id=? AND derived_from='' AND state IN"
            " ('eligible','matched','skipped','copying','joined','catalog')" + unplaced,
            [job_id, job_id],
        )

    async def _discard_reset(self, job_id: str) -> None:
        """After a review's decisions were undone (``_reset_overlap``): the copies' bytes
        go."""
        while await self._discard_copies(job_id):
            pass

    async def _discard_copies(self, job_id: str | None = None) -> bool:
        """Copies a new review gave up, or a stop left unfinished (``discarding``): each
        one's private bytes are deleted, and the deletion synced, before its row - and with
        it its reservation - goes. Repeated after an interruption: at the start, when the
        worker is idle and before the import is checked again (``job_id``: only its
        copies). A copy is only made within the worker's tick, holding its import's lock,
        which each of these holds too (or comes before the worker): never one being made.
        True when there was any."""
        if job_id is not None:
            rows = await self.store.fetchall(
                "SELECT id,staging_name FROM music_import_items WHERE job_id=?"
                " AND state='discarding' ORDER BY id LIMIT 500",
                [job_id],
            )
        else:
            rows = await self.store.fetchall(
                "SELECT i.id,i.staging_name FROM music_import_items i"
                " JOIN music_import_jobs j ON j.id=i.job_id WHERE j.library_id=? AND j.root_id=?"
                " AND i.state='discarding' ORDER BY i.id LIMIT 500",
                [self.library_id, self.storage.root_id],
            )
        for row in rows:
            await anyio.to_thread.run_sync(self.storage.discard_attempt, row["staging_name"])
            await self.store.execute(
                "DELETE FROM music_import_items WHERE id=? AND state='discarding'", [row["id"]]
            )
        return bool(rows)

    async def distinct(
        self, owner_id: str, job_id: str, item_id: str, *, value: bool = True
    ) -> dict[str, Any]:
        """ "Not the same song": this file is never linked to a song the library has; it
        is published like a song its album lacks (``overlap.py``); ``value=False`` takes
        that back. The review runs its comparison again and gets a new revision. A held
        file has no such choice: it would make a second album of its name."""
        _key(item_id)
        if type(value) is not bool:
            raise ImportError("invalid", "Choose whether this file is a different song.")
        async with self._locks.hold(job_id):
            job = await self._job(owner_id, job_id)
            if job["state"] not in ("review", "checking") or job["approved_revision"] is not None:
                raise ImportError("state", "Change matches only while reviewing this import.")
            item = await self._item(job_id, item_id)
            if item["derived_from"] or item["state"] not in ("eligible", *_RECLASSIFIED):
                raise ImportError("state", "This file has no match to change.")
            if value and item["outcome"] == HELD and not item["distinct_song"]:
                raise ImportError(
                    "state",
                    "This file cannot be added: it would show as a second album of its name.",
                )
            async with self.store.transaction() as conn:
                await conn.execute(
                    "UPDATE music_import_items SET distinct_song=? WHERE id=?",
                    [int(value), item_id],
                )
                await self._reset_overlap(conn, job_id)
                await conn.execute(
                    "UPDATE music_import_jobs SET state='checking',error='' WHERE id=?"
                    " AND state IN ('review','checking')",
                    [job_id],
                )
            await self._discard_reset(job_id)
            self._wake.set()
        return await self.status(owner_id, job_id)

    async def keep_files(
        self, owner_id: str, job_id: str, item_id: str, *, value: bool = True
    ) -> dict[str, Any]:
        """ "Add my files anyway": the album of this file is published as it would have
        been, below the person's minimum quality or not; ``value=False`` takes that back
        (every file of the album). The review runs its comparison again."""
        _key(item_id)
        if type(value) is not bool:
            raise ImportError("invalid", "Choose whether to add your files anyway.")
        async with self._locks.hold(job_id):
            job = await self._job(owner_id, job_id)
            if job["state"] not in ("review", "checking") or job["approved_revision"] is not None:
                raise ImportError("state", "Change this only while reviewing this import.")
            item = await self._item(job_id, item_id)
            if item["derived_from"] or item["state"] not in ("eligible", *_RECLASSIFIED):
                raise ImportError("state", "This file has no quality choice to change.")
            album = _album_of_facts(item)
            async with self.store.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT id,facts FROM music_import_items WHERE job_id=? AND derived_from=''",
                    [job_id],
                )
                for row in await cursor.fetchall():
                    if row["id"] != item_id and (
                        album is None or _album_of_facts(dict(row)) != album
                    ):
                        continue  # the choice is the album's: every file of it
                    facts = _facts(dict(row))
                    if value:
                        facts["keep_files"] = True
                    else:
                        facts.pop("keep_files", None)
                    await conn.execute(
                        "UPDATE music_import_items SET facts=? WHERE id=?",
                        [json.dumps(facts), row["id"]],
                    )
                await self._reset_overlap(conn, job_id)
                await conn.execute(
                    "UPDATE music_import_jobs SET state='checking',error='' WHERE id=?"
                    " AND state IN ('review','checking')",
                    [job_id],
                )
            await self._discard_reset(job_id)
            self._wake.set()
        return await self.status(owner_id, job_id)

    async def check_again(self, owner_id: str, job_id: str) -> dict[str, Any]:
        """Compare the reviewed files with the library and the catalog again (after the
        catalog could not be reached, say). Nothing is uploaded or checked again."""
        async with self._locks.hold(job_id):
            job = await self._job(owner_id, job_id)
            if job["state"] not in ("review", "checking") or job["approved_revision"] is not None:
                raise ImportError("state", "Check again only while reviewing this import.")
            async with self.store.transaction() as conn:
                await self._reset_overlap(conn, job_id)
                await conn.execute(
                    "UPDATE music_import_jobs SET state='checking',error='' WHERE id=?"
                    " AND state IN ('review','checking')",
                    [job_id],
                )
            await self._discard_reset(job_id)
            self._wake.set()
        return await self.status(owner_id, job_id)

    async def _fingerprint(self, job_id: str) -> str:
        hashed = hashlib.sha256()
        hashed.update(self.config_id.encode())
        cursor = ""
        while True:
            rows = await self.store.fetchall(
                "SELECT id,asset_id,expected_size,digest,final_name,state,warnings,outcome,"
                "match,distinct_song,derived_from,"
                "coalesce(json_extract(CASE WHEN json_valid(facts) THEN facts END,"
                "'$.keep_files'),0) AS keep_files"
                " FROM music_import_items WHERE job_id=? AND id>? AND state<>'discarding'"
                " ORDER BY id LIMIT 500",
                [job_id, cursor],
            )
            if not rows:
                break
            for row in rows:
                hashed.update(json.dumps(list(row), separators=(",", ":")).encode())
                cursor = row["id"]
        return hashed.hexdigest()

    async def approve(
        self, owner_id: str, job_id: str, revision: int, request_key: str
    ) -> dict[str, Any]:
        _key(request_key)
        async with self._locks.hold(job_id):
            job = await self._job(owner_id, job_id)
            if job["approved_revision"] is not None:
                if job["approved_revision"] != revision or job["approval_key"] != request_key:
                    raise ImportError("conflict", "This import already has a different approval.")
                return await self.status(owner_id, job_id)
            if job["state"] != "review" or job["revision"] != revision:
                raise ImportError("revision", "Review the current file list before approving it.")
            legacy = await self.store.fetchone(
                "SELECT 1 FROM music_import_items WHERE job_id=? AND state='eligible'"
                " AND derived_from='' AND facts='' LIMIT 1",
                [job_id],
            )
            if legacy:
                # Approved files without facts or a decision: checked again.
                await self.store.execute(
                    "UPDATE music_import_jobs SET state='checking',error='' WHERE id=?"
                    " AND state='review'",
                    [job_id],
                )
                self._wake.set()
                raise ImportError("revision", "The reviewed file list changed. Review it again.")
            if await self._fingerprint(job_id) != job["fingerprint"]:
                raise ImportError("revision", "The reviewed file list changed. Review it again.")
            eligible = await self.store.fetchone(
                "SELECT 1 FROM music_import_items WHERE job_id=?"
                " AND state IN ('eligible','matched','catalog') LIMIT 1",
                [job_id],
            )
            if not eligible:
                raise ImportError("empty", "There are no eligible audio files to import.")
            current = await self._job(owner_id, job_id)
            if current["state"] != "review":
                raise ImportError("stopped", "This import was stopped.")
            await self.store.execute(
                "UPDATE music_import_jobs SET state='approved',approved_revision=revision,"
                "approval_key=?,approved_at=?,error='' WHERE id=? AND state='review'",
                [request_key, time.time(), job_id],
            )
            self._wake.set()
        return await self.status(owner_id, job_id)

    async def status(
        self,
        owner_id: str,
        job_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        problems_only: bool = False,
        overlap_only: bool = False,
    ) -> dict[str, Any]:
        """Counts and one page of the received files. ``problems_only``: files that cannot
        be imported, are skipped or need a look; ``overlap_only``: files the library
        already had or that join one of its albums (the report of what was not added and
        why). A published copy is shown on its original's row."""
        await self._job(owner_id, job_id, write=False)
        if (
            type(offset) is not int
            or type(limit) is not int
            or type(problems_only) is not bool
            or type(overlap_only) is not bool
            or not (0 <= offset <= self.limits.max_job_files and 1 <= limit <= 500)
        ):
            raise ImportError("invalid", "Choose a bounded import page.")
        view = 2 if overlap_only else 1 if problems_only else 0
        # Counts and page come from one snapshot, even while the checker advances.
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                _STATUS_COUNTS,
                [view, view, view, job_id],
            )
            row = await cursor.fetchone()
            assert row
            cursor = await conn.execute(
                _STATUS_ITEMS,
                [job_id, view, view, view, limit, offset],
            )
            rows = list(await cursor.fetchall())
        result = dict(row)
        result["skipped_files"] = (
            result["rejected_files"]
            + result["excluded_files"]
            + result["duplicate_files"]
            + result["held_files"]
            + result["unchecked_files"]
        )
        result["checked_files"] = (
            result["eligible_files"]
            + result["linked_files"]
            + result["low_quality_files"]
            + result["skipped_files"]
        )
        items = []
        for item in rows:
            entry = dict(item)
            entry["warnings"] = json.loads(item["warnings"])
            try:
                entry["match"] = json.loads(item["match"] or "{}")
            except ValueError:
                entry["match"] = {}
            entry["distinct"] = bool(entry.pop("distinct_song"))
            entry["keep_files"] = bool(entry["keep_files"])
            # Upgraded from the add-ons after a play (``upgrade.py``): from what to what.
            try:
                upgraded = json.loads(entry.pop("upgraded") or "{}")
            except ValueError:
                upgraded = {}
            entry["upgraded"] = (
                {"from": upgraded.get("quality", ""), "to": upgraded.get("upgraded_to", "")}
                if isinstance(upgraded, dict) and upgraded
                else None
            )
            items.append(entry)
        result["items"] = items
        result["next_offset"] = (
            offset + len(rows) if offset + len(rows) < result["filtered_total"] else None
        )
        return result

    async def listing(self, owner_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        await self._actor(owner_id, write=False)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ImportError("invalid", "Choose a bounded import history page.")
        rows = await self.store.fetchall(
            "SELECT id,state,revision,error,created FROM music_import_jobs"
            " WHERE owner_id=? AND library_id=? ORDER BY created DESC LIMIT ?",
            [owner_id, self.library_id, limit],
        )
        return [dict(row) for row in rows]

    async def cancel(self, owner_id: str, job_id: str) -> dict[str, Any]:
        # Do not wait for a scan/review holding the job lock. Every worker checks this
        # durable state after waits. Any already placed file is retained permanently.
        await self._job(owner_id, job_id, write=False)
        async with self.store.transaction() as conn:
            await conn.execute(
                "UPDATE music_import_jobs SET state='cancelled',error='' WHERE id=? AND"
                " state<>'done'",
                [job_id],
            )
            await conn.execute(
                _RELEASE_UNPLACED,
                [job_id],
            )
        return await self.status(owner_id, job_id)

    async def resume(self, owner_id: str, job_id: str) -> dict[str, Any]:
        async with self._locks.hold(job_id):
            job = await self._job(owner_id, job_id)
            if job["state"] == "paused":
                state = (
                    "approved"
                    if job["approved_revision"] is not None
                    else "checking"
                    if job["resume_state"] == "checking"
                    else "uploading"
                )
                await self.store.execute(
                    "UPDATE music_import_jobs SET state=?,error='' WHERE id=? AND state='paused'",
                    [state, job_id],
                )
                self._wake.set()
        return await self.status(owner_id, job_id)

    async def _publishing(
        self, owner: str, job_id: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        job = await self._job(owner, job_id)
        if job["state"] not in ("approved", "publishing") or (
            job["approved_revision"] is None
            or job["approved_revision"] != job["revision"]
            or (revision is not None and job["revision"] != revision)
        ):
            raise ImportError("stopped", "This import is no longer approved for publication.")
        return job

    async def _place(self, job: dict[str, Any], item: dict[str, Any]) -> None:
        """Durably place one approved original; scanning and credit are batch work."""
        owner, job_id = job["owner_id"], job["id"]
        await self._publishing(owner, job_id, revision=job["approved_revision"])
        if item["state"] == "eligible":
            await self._verify(item)
            await self._publishing(owner, job_id, revision=job["approved_revision"])
            # Durable intent commits before the directory entry becomes visible.
            await self.store.execute(
                "UPDATE music_import_items SET state='placing',error='' WHERE id=? AND"
                " state='eligible'",
                [item["id"]],
            )
            item["state"] = "placing"
            await self._publishing(owner, job_id, revision=job["approved_revision"])
            try:
                self.storage.place(item["staging_name"], item["final_name"])
            except FileExistsError:
                # A first placement must never adopt even identical preexisting bytes.
                raise ImportError(
                    "collision",
                    "The reserved audio destination is occupied. Stop and check storage.",
                ) from None
        elif item["state"] == "placing":
            # Recovery may see either side of the atomic publication. A placed result
            # is accepted only when it is our staging inode, with exact recorded bytes.
            try:
                final = self.storage.file(item["final_name"], final=True)
            except FileNotFoundError:
                await self._verify(item)
                await self._publishing(owner, job_id, revision=job["approved_revision"])
                self.storage.place(item["staging_name"], item["final_name"])
            else:
                staged = self.storage.file(item["staging_name"])
                try:
                    a, b = os.fstat(final), os.fstat(staged)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise ImportError(
                            "collision", "The reserved audio destination is occupied."
                        )
                finally:
                    os.close(final)
                    os.close(staged)
        await self._verify(item, final=True)
        await self._publishing(owner, job_id, revision=job["approved_revision"])
        await self.store.execute(
            "UPDATE music_import_items SET state='placed' WHERE id=?", [item["id"]]
        )

    async def _native_evidence(self, job: dict[str, Any], item: dict[str, Any]) -> NativeFile:
        owner, job_id = job["owner_id"], job["id"]
        relative = self.storage.folder + "/" + item["final_name"]
        await self._publishing(owner, job_id, revision=job["approved_revision"])
        native = await self.native_lookup(owner, self.library_id, relative)
        await self._publishing(owner, job_id, revision=job["approved_revision"])
        if (
            native is None
            or native.path != relative
            or native.size != item["expected_size"]
            or not native.song_id
            or len(native.song_id) > 300
        ):
            raise ImportError("indexing", "Navidrome has not verified the exact imported file yet.")
        reused = await self.store.fetchone(
            "SELECT id FROM music_import_items WHERE song_id<>'' AND song_id=? AND id<>?",
            [native.song_id, item["id"]],
        )
        if reused:
            raise ImportError(
                "identity", "Navidrome reused an imported song identity. Stop and check indexing."
            )
        return native

    async def _credit(self, job: dict[str, Any], item: dict[str, Any], native: NativeFile) -> None:
        owner, job_id = job["owner_id"], job["id"]
        # Recheck *after* refresh; identity reuse/path changes cannot adopt another file.
        after = await self._native_evidence(job, item)
        if after != native:
            raise ImportError(
                "identity", "The imported native identity changed. Stop and check indexing."
            )
        await self._verify(item, final=True)
        await self._publishing(owner, job_id, revision=job["approved_revision"])
        track = await self.store.fetchone(
            "SELECT track_key FROM collection_tracks WHERE library_id=? AND song_id=?"
            " AND original=1 AND present=1",
            [self.library_id, native.song_id],
        )
        if not track:
            raise ImportError("indexing", "The imported song is not ready in the music index yet.")
        await self._publishing(owner, job_id, revision=job["approved_revision"])
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT state,approved_revision,revision FROM music_import_jobs WHERE id=?",
                [job_id],
            )
            current = await cursor.fetchone()
            if (
                not current
                or current["state"] not in ("approved", "publishing")
                or (current["approved_revision"] != current["revision"])
                or current["revision"] != job["approved_revision"]
            ):
                raise ImportError("stopped", "This import was stopped.")
            cursor = await conn.execute(
                "SELECT id FROM music_import_items WHERE song_id<>'' AND song_id=? AND id<>?",
                [native.song_id, item["id"]],
            )
            if await cursor.fetchone():
                raise ImportError(
                    "identity",
                    "Navidrome reused an imported song identity. Stop and check indexing.",
                )
            await record_verified_original(
                conn,
                owner_id=owner,
                library_id=self.library_id,
                track_key=track["track_key"],
                song_id=native.song_id,
                provenance_id="import:" + item["id"],
                asset_id=item["asset_id"],
            )
            warnings = json.loads(item["warnings"] or "[]")
            expected = json.loads(item.get("match") or "{}").get("album_id")
            if expected:
                # The song was meant to join a library album; say so if Navidrome made a
                # second album of it after all (another tag than predicted).
                cursor = await conn.execute(
                    "SELECT album_id FROM collection_tracks WHERE library_id=? AND song_id=?",
                    [self.library_id, native.song_id],
                )
                placed = await cursor.fetchone()
                if placed and placed["album_id"] != expected and SEPARATE not in warnings:
                    warnings.append(SEPARATE)
            await conn.execute(
                "UPDATE music_import_items SET"
                " state='complete',song_id=?,track_key=?,error='',warnings=? WHERE id=?",
                [native.song_id, track["track_key"], json.dumps(warnings), item["id"]],
            )

    async def _linkable(
        self, owner: str, item: Mapping[str, Any], match: Mapping[str, Any]
    ) -> bool:
        """Whether the song a reviewed file is to be linked to is still that recording,
        right before the link: in the index, and by the importing caller's own native read
        of it, the same recording (and album) as the file. False when it is verifiably
        gone or became another recording - the file is then published instead (the checks
        before each publication batch decide how); an access failure stops the import."""
        song_id, track_key = str(match.get("song_id") or ""), str(match.get("track_key") or "")
        if not song_id or not track_key:
            return False
        row = await self.store.fetchone(
            "SELECT track_key FROM collection_tracks WHERE library_id=? AND song_id=?"
            " AND present=1",
            [self.library_id, song_id],
        )
        if not row or row["track_key"] != track_key:
            return False
        if str(match.get("reason") or "") == SAME_BYTES:
            return await self._same_bytes(owner, item, song_id, track_key)
        if self.native_song is None:
            return True
        try:
            native = await self.native_song(owner, self.library_id, song_id)
        except NativeAbsent:
            return False
        except CollectionError:
            raise ImportError(
                "access", "The library song this import links to could not be verified."
            ) from None
        if native.get("id") != song_id:
            return False
        if item.get("outcome") == LOW_QUALITY:
            return True  # the catalog's reviewed version of the file, not the file itself
        current = LibrarySong.of_index(
            {"song_id": song_id, "track_key": track_key, "album_id": native.get("albumId") or ""},
            native,
            placeholder=False,
        )
        incoming = Incoming(str(item["id"]), _facts(item))
        if not same_keys(incoming.keys, current.keys):
            return False
        if not fold(incoming.album):
            return True
        return same_name(
            Album(incoming.album, incoming.album_artist), Album(current.album, current.album_artist)
        )

    async def _same_bytes(
        self, owner: str, item: Mapping[str, Any], song_id: str, track_key: str
    ) -> bool:
        """Whether a file reviewed as exactly the bytes an earlier import published
        (``overlap.SAME_BYTES``) may be linked to that song now: that import's receipt
        still names this song and these bytes, its published file still holds them
        (``Storage.holds``: the retained file, or one of the same size and digest put in its
        place; what cannot be told stops the import), and the importing caller's
        native read shows the song at that path with that size. Its tags are not compared
        with the song's: these are the same bytes, which Navidrome may describe otherwise
        than the file's tags (an untagged file's name is its title, a Shorten file has no
        length to read). False when that no longer holds - the file is then published
        instead, ``_recheck`` deciding again with the same receipt checks; Navidrome
        showing the song at another file stops the import."""
        row = await self.store.fetchone(
            "SELECT i.final_name,i.staging_name,i.expected_size FROM music_import_items i"
            " JOIN music_import_jobs j ON j.id=i.job_id"
            " JOIN collection_tracks t ON t.library_id=j.library_id AND t.song_id=i.song_id"
            " AND t.present=1 WHERE j.library_id=? AND j.root_id=? AND i.state='complete'"
            " AND i.song_id=? AND i.digest=? AND i.job_id<>? AND t.track_key=? LIMIT 1",
            [
                self.library_id,
                self.storage.root_id,
                song_id,
                item["digest"],
                item["job_id"],
                track_key,
            ],
        )
        if row is None or not await anyio.to_thread.run_sync(
            self.storage.holds,
            row["final_name"],
            row["staging_name"],
            row["expected_size"],
            item["digest"],
        ):
            return False
        if self.native_song is None:
            return True
        try:
            native = await self.native_song(owner, self.library_id, song_id)
        except NativeAbsent:
            # Gone from Navidrome, still in the index: the index learns it first, so that
            # the check before this file's publication does not link it again.
            try:
                await self.refresh(owner, self.library_id)
            except CollectionError:
                raise ImportError(
                    "access", "The library could not be read with your access. Sign in again."
                ) from None
            return False
        except CollectionError:
            raise ImportError(
                "access", "The library song this import links to could not be verified."
            ) from None
        if (
            native.get("id") != song_id
            or native.get("path") != self.storage.folder + "/" + row["final_name"]
            or native.get("size") != row["expected_size"]
        ):
            raise ImportError(
                "identity",
                "Navidrome shows the imported song this file links to at another file."
                " Stop and check indexing.",
            )
        return True

    async def _link_matched(self, job: dict[str, Any]) -> bool:
        """Link a bounded batch of approved files to the songs the library already has.
        A song that went away or changed since the review gets this file published
        instead."""
        owner, job_id, revision = job["owner_id"], job["id"], job["approved_revision"]
        rows = await self.store.fetchall(
            "SELECT * FROM music_import_items WHERE job_id=? AND state='matched'"
            " ORDER BY id LIMIT ?",
            [job_id, LINK_BATCH],
        )
        for row in rows:
            item = dict(row)
            match = json.loads(item["match"] or "{}")
            song_id, track_key = str(match.get("song_id") or ""), str(match.get("track_key") or "")
            await self._publishing(owner, job_id, revision=revision)
            if not await self._linkable(owner, item, match):
                await self._publishing(owner, job_id, revision=revision)
                if item["outcome"] == LOW_QUALITY:
                    warnings = [*json.loads(item["warnings"] or "[]"), UNLINKED]
                    await self.store.execute(
                        "UPDATE music_import_items SET state='skipped',warnings=?"
                        " WHERE id=? AND state='matched'",
                        [json.dumps(warnings), item["id"]],
                    )
                    continue
                # Published as a new song; the check before its publication batch
                # (``_recheck``) compares it with the library as it is then.
                warnings = [*json.loads(item["warnings"] or "[]"), REPLACED]
                evidence = Decision(NEW, reason=REPLACED).evidence(
                    Incoming(item["id"], _facts(item))
                )
                await self.store.execute(
                    "UPDATE music_import_items SET state='eligible',outcome=?,match=?,warnings=?"
                    " WHERE id=? AND state='matched'",
                    [NEW, json.dumps(evidence), json.dumps(warnings), item["id"]],
                )
                continue
            async with self.store.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT state,approved_revision,revision FROM music_import_jobs WHERE id=?",
                    [job_id],
                )
                current = await cursor.fetchone()
                if (
                    not current
                    or current["state"] not in ("approved", "publishing")
                    or current["approved_revision"] != current["revision"]
                    or current["revision"] != revision
                ):
                    raise ImportError("stopped", "This import was stopped.")
                catalog_song = item["outcome"] == CATALOG_SONG
                # Its audio goes into the catalog song unless the review said it may not
                # (``match.audio`` False: below the importer's minimum, the gate's).
                fills = catalog_song and match.get("audio", True) is not False
                await conn.execute(
                    "INSERT INTO music_import_links(item_id,job_id,owner_id,library_id,"
                    "track_key,song_id,kind,credited,active,created,audio)"
                    " VALUES (?,?,?,?,?,?,?,0,1,?,?)",
                    [
                        item["id"],
                        job_id,
                        owner,
                        self.library_id,
                        track_key,
                        song_id,
                        "catalog_song"
                        if catalog_song or item["outcome"] == LOW_QUALITY
                        else "library",
                        time.time(),
                        # Next, by ``_catalog_audio``.
                        audio.PENDING if fills and self.catalog_audio else "",
                    ],
                )
                credited = await record_link_credit(
                    conn,
                    owner_id=owner,
                    library_id=self.library_id,
                    track_key=track_key,
                    song_id=song_id,
                    item_id=item["id"],
                )
                await conn.execute(
                    "UPDATE music_import_links SET credited=? WHERE item_id=?",
                    [int(credited), item["id"]],
                )
                await conn.execute(
                    "UPDATE music_import_items SET state='linked',error='' WHERE id=?"
                    " AND state='matched'",
                    [item["id"]],
                )
        return bool(rows)

    def poke(self) -> None:
        """Have the worker look for work now (a removal changed which links are active)."""
        self._wake.set()

    async def _catalog_audio(self) -> bool:
        """One step of putting imports' audio in place of the catalog songs they matched
        (``catalog_audio.py``), or taking it out again: an active link whose audio is to
        be in place (also again, when its file went or the song's own audio went), or an
        inactive one (its import removed) whose audio is in place. The copy is reserved
        against the importer's storage before it is made and released only once the song
        is known not to play it (also a link whose record a stop left behind the song: its
        audio in place is recorded as such). A step that has to wait (``pending``) or fails
        is tried again later, a bounded number of times for putting audio in; one link's
        failure never stops the others. False: nothing to do now."""
        if self.catalog_audio is None:
            return False
        rows = await self.store.fetchall(
            "SELECT l.item_id,l.song_id,l.owner_id,l.active,l.audio,l.reserved,i.staging_name,"
            "i.final_name,i.facts,i.state,i.received_size,p.state AS placeholder_state"
            " FROM music_import_links l"
            " JOIN music_import_items i ON i.id=l.item_id"
            " LEFT JOIN placeholders p ON p.song_id=l.song_id"
            " WHERE l.kind='catalog_song' AND l.library_id=? AND ("
            " (l.active=1 AND l.audio='pending')"
            " OR (l.active=1 AND l.audio='placed'"
            "  AND NOT (p.state='delivered' AND p.import_item=l.item_id))"
            " OR (l.active=1 AND l.audio='kept' AND p.state='placeholder')"
            " OR (l.active=0 AND l.audio='placed')"
            " OR (l.audio NOT IN ('pending','placed') AND l.reserved>0)"
            " OR (l.audio NOT IN ('pending','placed')"
            "  AND p.state='delivered' AND p.import_item=l.item_id))"
            " ORDER BY l.created,l.item_id LIMIT 50",
            [self.library_id],
        )
        now = time.monotonic()
        link = next(
            (dict(r) for r in rows if self._later.get(r["item_id"], (0, 0))[0] <= now), None
        )
        if link is None:
            return False
        try:
            outcome = await self._catalog_step(link)
        except Exception as exc:  # this link waits; the others go on
            log.warning("an import's audio for a catalog song failed: %s", type(exc).__name__)
            outcome = audio.PENDING
        if outcome == audio.PENDING:
            tries = self._later.get(link["item_id"], (0.0, 0))[1] + 1
            if link["active"] and tries >= AUDIO_TRIES and await self._absent(link):
                await self._audio_is(link, audio.FAILED, ("pending", "placed", "kept"))
                await self._release_audio(link)
                self._later.pop(link["item_id"], None)
            else:
                self._later[link["item_id"]] = (now + min(AUDIO_RETRY * tries, 3600.0), tries)
            return True
        self._later.pop(link["item_id"], None)
        return True

    async def _catalog_step(self, link: dict[str, Any]) -> str:
        """One link's step (``_catalog_audio``): its outcome. It starts from what the song
        plays (``CatalogSongAudio.holds``, a swap a stop interrupted put right first): this
        import's audio in place is the link's, recorded as placed and reserved - also after
        a stop between the engine's commit and the link's record, whatever changed since
        (the importer's minimum, say) - and taken out when its import was removed. A
        reservation goes only once the song is known not to play it."""
        assert self.catalog_audio is not None
        song, item = link["song_id"], link["item_id"]
        if await self.catalog_audio.holds(song, item):
            if link["audio"] != audio.PLACED:
                await self._hold_audio(link)
                await self._audio_is(link, audio.PLACED, _NOT_PLACED)
                link["audio"] = audio.PLACED
            if link["active"]:
                return audio.PLACED
            await self.catalog_audio.take(song, item)
            self._forget(song)
            if await self.catalog_audio.holds(song, item):
                return audio.PENDING  # not taken out yet: tried again
        if not link["active"]:
            async with self.store.transaction() as conn:
                await conn.execute(
                    "UPDATE music_import_links SET audio='pending' WHERE item_id=?"
                    " AND audio='placed' AND active=0",
                    [item],
                )
                await self._release_audio(link, conn)
            return audio.PLACED
        if link["audio"] not in ("pending", "placed"):
            # Not in place (a copy not made, or gone): its reservation goes. A song that had
            # audio of its own (``kept``) is tried again once it is a placeholder again.
            if link["reserved"]:
                await self._release_audio(link)
            if not (link["audio"] == audio.KEPT and link["placeholder_state"] == "placeholder"):
                return audio.FAILED
        facts = json.loads(link["facts"] or "{}")
        facts = facts if isinstance(facts, dict) else {}
        suffix = PurePosixPath(link["final_name"] or "").suffix.lower()
        if not facts.get("taggable") or suffix not in audio.SUFFIXES:
            outcome = audio.UNTAGGABLE
        elif self.minimum is not None and not (await self.minimum(link["owner_id"])).met_by(
            quality_of(facts.get("quality"))
        ):
            outcome = audio.LOW_QUALITY
        elif not await self._reserve_audio(link):
            outcome = audio.FAILED  # beyond the importer's storage: it stays a plain link
        else:
            try:
                source = await anyio.to_thread.run_sync(self.storage.file, link["staging_name"])
            except (OSError, ImportError):
                outcome = audio.FAILED  # its received file is not retained (stopped early)
            else:
                try:
                    outcome = await self.catalog_audio.put(
                        link["song_id"], link["item_id"], source, suffix
                    )
                finally:
                    os.close(source)
                self._forget(link["song_id"])
                if outcome == audio.FAILED and await self.catalog_audio.holds(song, item):
                    outcome = audio.PLACED  # (a swap whose record was not known: in place)
        await self._audio_is(link, outcome, ("pending", "placed", "kept"))
        if outcome not in (audio.PLACED, audio.PENDING):
            await self._release_audio(link)
        return outcome

    async def _absent(self, link: dict[str, Any]) -> bool:
        """Whether the catalog song is known not to play this link's import's audio."""
        assert self.catalog_audio is not None
        try:
            return not await self.catalog_audio.holds(link["song_id"], link["item_id"])
        except Exception:  # not known now
            return False

    async def _hold_audio(self, link: dict[str, Any]) -> None:
        """This import's audio is in place: its copy stays reserved against the importer's
        storage - reserved now when it is not (a stop left it without), within the limits
        or not: its bytes are there."""
        if link["reserved"]:
            return
        size = int(link["received_size"] or 0) + COPY_SLACK
        async with self._locks.hold("admission"), self.store.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE music_import_links SET reserved=? WHERE item_id=? AND reserved=0",
                [size, link["item_id"]],
            )
            if cursor.rowcount:
                await conn.execute(
                    "UPDATE music_import_items SET reserved_size=reserved_size+? WHERE id=?",
                    [size, link["item_id"]],
                )
        link["reserved"] = size

    def _forget(self, song_id: str) -> None:
        if self.forget is not None:
            self.forget(song_id)

    async def _reserve_audio(self, link: dict[str, Any]) -> bool:
        """The copy's bytes reserved against the importer's storage (once)."""
        if link["reserved"]:
            return True
        size = int(link["received_size"] or 0) + COPY_SLACK
        async with self._locks.hold("admission"):
            try:
                await self._capacity(link["owner_id"], size)
            except ImportError:
                return False
            async with self.store.transaction() as conn:
                cursor = await conn.execute(
                    "UPDATE music_import_links SET reserved=? WHERE item_id=? AND reserved=0",
                    [size, link["item_id"]],
                )
                if cursor.rowcount:
                    await conn.execute(
                        "UPDATE music_import_items SET reserved_size=reserved_size+? WHERE id=?",
                        [size, link["item_id"]],
                    )
        link["reserved"] = size
        return True

    async def _release_audio(self, link: dict[str, Any], conn: Any = None) -> None:
        """The copy's reservation goes (it was not made, or is gone again)."""
        if conn is None:
            async with self.store.transaction() as conn:
                await self._release_audio(link, conn)
            return
        cursor = await conn.execute(
            "SELECT reserved FROM music_import_links WHERE item_id=?", [link["item_id"]]
        )
        row = await cursor.fetchone()
        if row and row["reserved"]:
            await conn.execute(
                "UPDATE music_import_items SET reserved_size=max(0,reserved_size-?) WHERE id=?",
                [row["reserved"], link["item_id"]],
            )
            await conn.execute(
                "UPDATE music_import_links SET reserved=0 WHERE item_id=?", [link["item_id"]]
            )
        link["reserved"] = 0

    async def _audio_is(self, link: dict[str, Any], value: str, was: Sequence[str]) -> None:
        marks = ",".join("?" for _ in was)
        await self.store.execute(
            f"UPDATE music_import_links SET audio=? WHERE item_id=? AND audio IN ({marks})",  # noqa: S608
            [value, link["item_id"], *was],
        )

    async def _release_linked(self, job: dict[str, Any] | None = None) -> bool:
        """A linked file's received bytes are not kept, nor a low-quality file's whose
        album came from the catalog (a catalog song's are: a later stage puts its audio
        into that song). Its reservation goes only after its bytes are deleted (and the
        deletion is synced), also for an import stopped or finished meanwhile (``job``
        None: those). Repeated after an interruption. True when there was any."""
        if job is not None:
            rows = await self.store.fetchall(
                "SELECT id,staging_name FROM music_import_items WHERE job_id=? AND state='linked'"
                " AND outcome IN (?,?) AND reserved_size>0 ORDER BY id LIMIT 500",
                [job["id"], LINKED, LOW_QUALITY],
            )
        else:
            rows = await self.store.fetchall(
                "SELECT i.id,i.staging_name FROM music_import_items i"
                " JOIN music_import_jobs j ON j.id=i.job_id WHERE j.library_id=? AND j.root_id=?"
                " AND j.state IN ('cancelled','done') AND i.state='linked'"
                " AND i.outcome IN (?,?) AND i.reserved_size>0 ORDER BY i.id LIMIT 500",
                [self.library_id, self.storage.root_id, LINKED, LOW_QUALITY],
            )
        for row in rows:
            await anyio.to_thread.run_sync(self.storage.discard_attempt, row["staging_name"])
            await self.store.execute(
                "UPDATE music_import_items SET reserved_size=0 WHERE id=? AND state='linked'",
                [row["id"]],
            )
        return bool(rows)

    async def _route_catalog(self, job: dict[str, Any]) -> bool:
        """Add one reviewed catalog release of a low-quality album to the library and point
        its files at their catalog songs, to be linked (``_link_matched``). A refusal that
        a retry does not change leaves those files not added, saying why; anything else
        pauses the import for a retry."""
        owner, job_id, revision = job["owner_id"], job["id"], job["approved_revision"]
        rows = await self.store.fetchall(
            "SELECT * FROM music_import_items WHERE job_id=? AND state='catalog'"
            " ORDER BY id LIMIT 500",
            [job_id],
        )
        if not rows:
            return False
        items = [dict(r) for r in rows]
        matches = {item["id"]: json.loads(item["match"] or "{}") for item in items}
        release = str(matches[items[0]["id"]].get("release") or "")
        fingerprint = str(matches[items[0]["id"]].get("fingerprint") or "")
        group = [i for i in items if matches[i["id"]].get("release") == release]
        tracks = sorted({str(matches[i["id"]].get("track") or "") for i in group} - {""})
        await self._publishing(owner, job_id, revision=revision)

        async def authorize() -> None:
            await self._publishing(owner, job_id, revision=revision)

        if self.catalog is None or not release or not tracks:
            songs: dict[str, str] = {}
            refused = "no catalog is set up"
        else:
            try:
                songs = await self.catalog.add(
                    release, tracks, fingerprint=fingerprint, authorize=authorize
                )
                refused = ""
            except CatalogRefused as exc:
                songs, refused = {}, str(exc) or "the library refused it"
            except ImportError:
                raise
            except Exception as exc:
                raise ImportError(
                    "catalog", "The catalog's version could not be added yet. Resume to retry."
                ) from exc
        if songs:
            await self._publishing(owner, job_id, revision=revision)
            await self.refresh(owner, self.library_id)
        await self._publishing(owner, job_id, revision=revision)
        async with self.store.transaction() as conn:
            for item in group:
                match = matches[item["id"]]
                song_id = songs.get(str(match.get("track") or ""), "")
                found = None
                if song_id:
                    cursor = await conn.execute(
                        "SELECT track_key,album_id,title,album FROM collection_tracks"
                        " WHERE library_id=? AND song_id=? AND present=1",
                        [self.library_id, song_id],
                    )
                    found = await cursor.fetchone()
                if found is None:
                    reason = refused or "its catalog song did not appear in the library"
                    warnings = [
                        *json.loads(item["warnings"] or "[]"),
                        NOT_ADDED.format(reason=reason),
                    ]
                    await conn.execute(
                        "UPDATE music_import_items SET state='skipped',warnings=?"
                        " WHERE id=? AND state='catalog'",
                        [json.dumps(warnings), item["id"]],
                    )
                    continue
                match.update(
                    song_id=song_id,
                    track_key=found["track_key"],
                    album_id=found["album_id"],
                    title=found["title"],
                    album=found["album"],
                )
                await conn.execute(
                    "UPDATE music_import_items SET state='matched',match=?"
                    " WHERE id=? AND state='catalog'",
                    [json.dumps(match), item["id"]],
                )
        return True

    async def _recheck(self, job: dict[str, Any], batch: list[dict[str, Any]]) -> bool:
        """Before a publication batch, every file of it still to be published - also a
        copy carrying an album's tags - is compared again with the library as it is now
        (``_decide``). A file another import or anything else meanwhile added to the
        library is linked to that song instead; any other change of the reviewed plan (a
        song that would now make a second album of its name, a copy whose album changed)
        takes the import back to review. True when something changed."""
        pending = [i for i in batch if i["state"] == "eligible"]
        if not pending:
            return False
        owner, job_id, revision = job["owner_id"], job["id"], job["approved_revision"]
        wanted = sorted({i["derived_from"] or i["id"] for i in pending})
        originals = {
            r["id"]: dict(r)
            for r in await self.store.fetchall(
                "SELECT * FROM music_import_items WHERE job_id=? AND id IN"
                " (SELECT value FROM json_each(?))",
                [job_id, json.dumps(wanted)],
            )
        }
        decisions = await self._decide(
            owner, job_id, await self._context(job_id, list(originals.values())), gate=False
        )
        links: list[tuple[dict[str, Any], Decision]] = []
        changed: list[str] = []
        for item in pending:
            original = originals.get(item["derived_from"] or item["id"])
            decision = decisions.get(original["id"]) if original else None
            if original is None or decision is None:
                changed.append(item["derived_from"] or item["id"])
                continue
            if _plan(decision) == _approved_plan(original):
                continue
            if (
                not item["derived_from"]
                and _approved_plan(original) == ("publish",)
                and decision.outcome in LINKS
            ):
                links.append((original, decision))
            else:
                changed.append(original["id"])
        if changed:
            await self._back_to_review(job, changed)
            return True
        if not links:
            return False
        await self._publishing(owner, job_id, revision=revision)
        async with self.store.transaction() as conn:
            for row, decision in links:
                evidence = decision.evidence(Incoming(row["id"], _facts(row)))
                warnings = [*json.loads(row["warnings"] or "[]"), MEANWHILE]
                await conn.execute(
                    "UPDATE music_import_items SET state='matched',outcome=?,match=?,warnings=?"
                    " WHERE id=? AND state='eligible'",
                    [decision.outcome, json.dumps(evidence), json.dumps(warnings), row["id"]],
                )
        return True

    async def _back_to_review(self, job: dict[str, Any], changed: Sequence[str]) -> None:
        """The library changed so that the approved plan of these files no longer holds:
        the import goes back to review (checking first), and needs a new approval.
        Placed, published and linked files stay as they are."""
        owner, job_id, revision = job["owner_id"], job["id"], job["approved_revision"]
        await self._publishing(owner, job_id, revision=revision)
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT state,approved_revision,revision FROM music_import_jobs WHERE id=?",
                [job_id],
            )
            current = await cursor.fetchone()
            if (
                not current
                or current["state"] not in ("approved", "publishing")
                or current["revision"] != revision
            ):
                raise ImportError("stopped", "This import is no longer approved for publication.")
            for ident in sorted(set(changed)):
                cursor = await conn.execute(
                    "SELECT warnings FROM music_import_items WHERE id=?", [ident]
                )
                row = await cursor.fetchone()
                warnings = json.loads(row["warnings"] or "[]") if row else []
                if row and CHANGED not in warnings:
                    await conn.execute(
                        "UPDATE music_import_items SET warnings=? WHERE id=?",
                        [json.dumps([*warnings, CHANGED]), ident],
                    )
            await self._reset_overlap(conn, job_id)
            await conn.execute(
                "UPDATE music_import_jobs SET state='checking',approved_revision=NULL,"
                "approval_key=NULL,approved_at=NULL,error='' WHERE id=?",
                [job_id],
            )
        await self._discard_reset(job_id)

    async def _legacy(self, job: dict[str, Any]) -> bool:
        """Approved files without facts (and without a decision against the library) are
        read again (one per step), then compared with the library like the
        review does. Unless every such file would still be published as it is, the
        import goes back to review. Placed files keep their placement. True when it did
        something."""
        owner, job_id, revision = job["owner_id"], job["id"], job["approved_revision"]
        row = await self.store.fetchone(
            "SELECT * FROM music_import_items WHERE job_id=? AND state='eligible'"
            " AND derived_from='' AND facts='' ORDER BY id LIMIT 1",
            [job_id],
        )
        if row:
            item = dict(row)
            error, facts = "", dict[str, Any]()
            try:
                await self._verify(item)
                _, facts = await self._probe(item)
            except (ImportError, OSError) as exc:
                error = exc.code if isinstance(exc, ImportError) else "changed"
            await self._publishing(owner, job_id, revision=revision)
            if error:
                await self.store.execute(
                    "UPDATE music_import_items SET state='rejected',error=?,facts='{}'"
                    " WHERE id=? AND state='eligible' AND facts=''",
                    [error, item["id"]],
                )
                await self._back_to_review(job, [item["id"]])
            else:
                await self.store.execute(
                    "UPDATE music_import_items SET facts=? WHERE id=? AND state='eligible'"
                    " AND facts=''",
                    [json.dumps(facts), item["id"]],
                )
            return True
        rows = [
            dict(r)
            for r in await self.store.fetchall(
                "SELECT * FROM music_import_items WHERE job_id=? AND state='eligible'"
                " AND derived_from='' AND outcome='' ORDER BY id LIMIT ?",
                [job_id, PUBLISH_BATCH_FILES],
            )
        ]
        if not rows:
            return False
        decisions = await self._decide(owner, job_id, await self._context(job_id, rows))
        changed = [
            r["id"]
            for r in rows
            if r["id"] not in decisions
            or _plan(decisions[r["id"]]) != ("publish",)
            or decisions[r["id"]].outcome == LOW_QUALITY
        ]
        if changed:
            await self._back_to_review(job, changed)
            return True
        await self._publishing(owner, job_id, revision=revision)
        async with self.store.transaction() as conn:
            for item in rows:
                decision = decisions[item["id"]]
                evidence = decision.evidence(Incoming(item["id"], _facts(item)))
                await conn.execute(
                    "UPDATE music_import_items SET outcome=?,match=? WHERE id=?"
                    " AND state='eligible' AND outcome=''",
                    [decision.outcome, json.dumps(evidence), item["id"]],
                )
        return True

    async def _batch(self, job_id: str) -> list[dict[str, Any]]:
        # Recover already placed/uncertain originals before adding new approved ones.
        rows = await self.store.fetchall(
            "SELECT * FROM music_import_items WHERE job_id=?"
            " AND state IN ('eligible','placing','placed')"
            " ORDER BY CASE WHEN state='eligible' THEN 1 ELSE 0 END,id LIMIT ?",
            [job_id, PUBLISH_BATCH_FILES],
        )
        batch: list[dict[str, Any]] = []
        total = 0
        for row in rows:
            size = row["expected_size"]
            if batch and total + size > PUBLISH_BATCH_BYTES:
                break
            batch.append(dict(row))
            total += size
        return batch

    async def _publish_batch(self, job: dict[str, Any], batch: list[dict[str, Any]]) -> None:
        for item in batch:
            await self._place(job, item)
        owner, job_id, revision = job["owner_id"], job["id"], job["approved_revision"]
        await self._publishing(owner, job_id, revision=revision)
        await self.scan([self.storage.folder])
        await self._publishing(owner, job_id, revision=revision)
        evidence: list[NativeFile] = []
        identities: set[str] = set()
        for item in batch:
            native = await self._native_evidence(job, item)
            if native.song_id in identities:
                raise ImportError("identity", "Navidrome reused an imported song identity.")
            identities.add(native.song_id)
            evidence.append(native)
        await self._publishing(owner, job_id, revision=revision)
        await self.refresh(owner, self.library_id)
        await self._publishing(owner, job_id, revision=revision)
        for item, native in zip(batch, evidence, strict=True):
            await self._credit(job, item, native)

    async def tick(self) -> bool:
        """Check one private file or publish/reconcile one bounded approved batch."""
        async with self._worker:
            row = await self.store.fetchone(
                "SELECT * FROM music_import_jobs WHERE state IN"
                " ('checking','approved','publishing')"
                " ORDER BY created LIMIT 1"
            )
            if not row:
                # Upgrades a stop interrupted are rolled back first, then copies a new
                # review gave up and stopped imports' linked files are deleted, then
                # imports' audio goes into (or out of) their catalog songs.
                if await self.upgrades.recover():
                    return True
                if await self._discard_copies():
                    return True
                if await self._release_linked():
                    return True
                return await self._catalog_audio()
            job = dict(row)
            async with self._locks.hold(job["id"]):
                try:
                    if job["state"] == "checking":
                        await self._check_one(job)
                        return True
                    await self._publishing(job["owner_id"], job["id"])
                    await self.store.execute(
                        "UPDATE music_import_jobs SET state='publishing' WHERE id=?"
                        " AND state IN ('approved','publishing')",
                        [job["id"]],
                    )
                    if await self._legacy(job):
                        return True
                    if await self._link_matched(job):
                        return True
                    if await self._route_catalog(job):
                        return True
                    await self._release_linked(job)
                    batch = await self._batch(job["id"])
                    if batch and await self._recheck(job, batch):
                        return True
                    if batch:
                        await self._publish_batch(job, batch)
                    pending = await self.store.fetchone(
                        "SELECT 1 FROM music_import_items WHERE job_id=?"
                        " AND state IN ('eligible','placing','placed','matched','catalog')"
                        " LIMIT 1",
                        [job["id"]],
                    )
                    if not pending:
                        await self._publishing(job["owner_id"], job["id"])
                        await self.store.execute(
                            "UPDATE music_import_jobs SET state='done',error='' WHERE id=?"
                            " AND state='publishing'",
                            [job["id"]],
                        )
                except Exception as exc:
                    code = exc.code if isinstance(exc, ImportError) else "retry"
                    await self.store.execute(
                        "UPDATE music_import_jobs SET"
                        " resume_state=state,state='paused',error=? WHERE id=?"
                        " AND state IN ('checking','approved','publishing')",
                        [code, job["id"]],
                    )
                except BaseException:
                    with anyio.CancelScope(shield=True):
                        await self.store.execute(
                            "UPDATE music_import_jobs SET"
                            " resume_state=state,state='paused',error='interrupted' WHERE id=?"
                            " AND state IN ('checking','approved','publishing')",
                            [job["id"]],
                        )
                    raise
            return True

    async def run(self) -> None:
        while True:
            self._wake = anyio.Event()
            if await self.tick():
                continue
            # Postponed work (an upgrade to undo, an import's audio that had to wait) is
            # looked at again even without a wake-up.
            with anyio.move_on_after(IDLE_SECONDS):
                await self._wake.wait()
