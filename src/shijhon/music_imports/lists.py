"""Artist lists for music imports' published files (``artist_lists``).

An import's published file is a link to a file the import retains privately, recorded with
its size and SHA-256: it is never written in place. Its list comes as an import operation
instead: a copy of the recorded file with the list (the bounded tag subprocess,
``retag.py``, ``lists``: nothing else changes) becomes the item's file and takes the
published name; the file it was stays retained (the received original, a copy that joined
an album, or an upgraded file), as an upgrade keeps the file it replaced, and both count
against the person's storage. The item's records then name the copy, so removing and
restoring the import, and later upgrades, work on it.

- **When it is published** (``at_publish``): before an approved file is placed, when the
  catalogs give its list within a short time; nothing is in Navidrome yet.
- **Later** (``write``): the background pass, for a published file. The swap is an
  upgrade's (``upgrade.py``): its intent is the item's ``upgrade`` record, so a stop is
  rolled back as an interrupted upgrade is; the copy takes the name only once Navidrome
  shows the song with its ID, its album and each artist, and the item's records and the
  list's row change together.
- **Undo** (``undo``): the retained file takes the published name back, for a file still
  as Shijhon left it; the copy goes.

Never while the import is being removed, while another swap of it is under way, or for a
file of someone's import that is not complete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import anyio

from shijhon.artist_lists.files import ListError, Made, make_copy, updated
from shijhon.artist_lists.names import listed
from shijhon.artist_lists.sources import Skips
from shijhon.matching.grouping import album_artist, album_key
from shijhon.music_imports.facts import first_track_artist
from shijhon.music_imports.model import ImportError
from shijhon.music_imports.upgrade import _REMOVING, CARRY, SWAP, _unlink_ours
from shijhon.navidrome.client import NavidromeError

if TYPE_CHECKING:
    from shijhon.music_imports.service import MusicImports
    from shijhon.navidrome.client import NavidromeService

log = logging.getLogger(__name__)

COPY_SLACK = 1024**2  # the copy's tags may be a little larger
CONFIRM_SCANS = 3
PUBLISH_BATCH_SECONDS = 60.0  # the catalogs asked for a publication batch's files, at most
PUBLISH_MISSES = 3  # failed or slow answers that end the asking for a batch

Settled = Callable[[dict[str, Any] | None, int], bool]
# The list a file being published is given, from its probe's facts (None: none now).
PublishNames = Callable[[Mapping[str, Any], Skips], Awaitable[tuple[list[str], str] | None]]


def _staging_name(item_id: str, digest: str) -> str:
    return hashlib.sha256(f"lists:{item_id}:{digest}".encode()).hexdigest()[:32] + ".part"


def import_key(item_id: str) -> str:
    return "import:" + item_id


def _touch(storage: Any, staging: str) -> None:
    """A retained file about to be published again gets the time of now: Navidrome reads a
    file again only when it is newer than its record (its bytes, which the records hash,
    stay as they are)."""
    os.utime(staging, dir_fd=storage.incoming, follow_symlinks=False)


@dataclass
class _Swap:
    item: dict[str, Any]
    made: Made
    settled: Settled
    record: Mapping[str, Any]
    intent: dict[str, Any]


class ImportLists:
    def __init__(self, imports: MusicImports, navidrome: NavidromeService) -> None:
        self.imports = imports
        self.navidrome = navidrome
        # Test hook: awaited at each named step of a write or an undo.
        self.step: Callable[[str], Awaitable[None]] | None = None
        # Set by the artist lists' worker while its setting is on (``ArtistLists``).
        self.publish_names: PublishNames | None = None
        self.clock: Callable[[], float] = time.time

    async def _at(self, step: str) -> None:
        if self.step is not None:
            await self.step(step)

    def published(self, item: Mapping[str, Any]) -> str:
        """The library-relative path of an item's published file."""
        return self.imports.storage.folder + "/" + str(item["final_name"])

    async def item_of(self, song_id: str) -> dict[str, Any] | None:
        """The complete import item that published ``song_id`` (None: none, or one being
        removed or swapped)."""
        store = self.imports.store
        row = await store.fetchone(
            "SELECT i.*,j.owner_id FROM music_import_items i JOIN music_import_jobs j"
            " ON j.id=i.job_id WHERE i.song_id=? AND i.state='complete' AND j.library_id=?",
            [song_id, self.imports.library_id],
        )
        if row is None or row["upgrade"]:
            return None
        if await self._removing(str(row["job_id"])):
            return None
        return dict(row)

    async def _removing(self, job_id: str) -> bool:
        return await self._removal(job_id) in _REMOVING

    async def _removal(self, job_id: str) -> str:
        """The state of the import's removal ("" : none)."""
        removal = await self.imports.store.fetchone(
            "SELECT state FROM music_import_removals WHERE job_id=?", [job_id]
        )
        return str(removal["state"]) if removal is not None else ""

    async def _copy(self, item: Mapping[str, Any], name: str, request: Mapping[str, Any]) -> Made:
        storage = self.imports.storage
        source = target = -1
        try:
            source = storage.file(item["staging_name"])
            target = storage.copy_target(name)
            suffix = PurePosixPath(str(item["final_name"])).suffix.lower()
            result = await make_copy(
                source, target, {**request, "suffix": suffix}, int(item["expected_size"])
            )
        except ImportError as exc:
            raise ListError(exc.code) from None
        finally:
            for descriptor in (source, target):
                if descriptor >= 0:
                    os.close(descriptor)
        if "refused" in result:
            raise ListError(result["refused"])
        os.fsync(storage.incoming)
        try:
            await anyio.to_thread.run_sync(
                lambda: storage.verify(name, result["size"], result["sha256"])
            )
        except ImportError as exc:
            raise ListError(exc.code) from None
        return Made(
            result["size"],
            result["sha256"],
            str(result.get("raw_before") or ""),
            str(result.get("raw_digest") or ""),
            result.get("pinned") is True,
        )

    # --- when it is published -------------------------------------------------------

    @staticmethod
    def _facts(item: Mapping[str, Any]) -> dict[str, Any] | None:
        """The probe's facts of a file that may be given its list when published."""
        try:
            facts = json.loads(item.get("facts") or "{}")
        except ValueError:
            return None
        if not isinstance(facts, dict) or facts.get("artists") or not facts.get("taggable"):
            return None
        return facts

    async def at_publish_batch(
        self,
        job: Mapping[str, Any],
        batch: list[dict[str, Any]],
        still: Callable[[], Awaitable[None]] | None = None,
    ) -> int:
        """Give the approved files of a batch about to be placed their lists, when the
        catalogs give them (``at_publish``); the files of a new album of this import that
        all get the same list, which its album artist text is, also get the album artists
        list - all of them or, when one is refused its copy, none (the others' copies are
        made again without it). The catalogs are asked within ``PUBLISH_BATCH_SECONDS``
        for the whole batch, and no more after ``PUBLISH_MISSES`` failed or slow answers:
        the rest is published as it is (the background pass asks later). ``still()``
        raises when the import was stopped meanwhile (asked between files). Returns how
        many were given one. Called under the worker's and the import's locks."""
        if self.publish_names is None:
            return 0
        await self._reconcile_albums(str(job["id"]), batch)
        found: dict[str, tuple[list[str], str]] = {}
        earlier = {
            str(r["item_id"])
            for r in await self.imports.store.fetchall(
                "SELECT item_id FROM artist_lists WHERE kind = 'import' AND item_id IS NOT NULL"
                " AND (state = 'written' OR phase <> '')"
            )
        }
        misses = 0
        skips = Skips()  # (an add-on that keeps failing is asked no more for the batch)
        with anyio.move_on_after(PUBLISH_BATCH_SECONDS):
            for item in batch:
                facts = self._facts(item) if item["state"] == "eligible" else None
                if facts is None or str(item["id"]) in earlier:
                    continue  # (one given its list before a pause keeps it)
                if still is not None:
                    await still()
                try:
                    names = await self.publish_names(facts, skips)
                except Exception as exc:  # published as it is: the background pass asks again
                    log.info("artist lists: an imported file's list not asked now: %s",
                             type(exc).__name__)  # fmt: skip
                    if getattr(exc, "answered", False):
                        continue  # (one add-on down, the others answered: not a miss)
                    misses += 1
                    if misses >= PUBLISH_MISSES:
                        break
                    continue
                if names is not None:
                    found[str(item["id"])] = names
        if not found:
            return 0
        albums = await self._album_lists(str(job["id"]), found)
        given = 0
        refused: set[str] = set()  # albums of which a file was not given its list
        with_album: list[dict[str, Any]] = []
        for item in batch:
            ident = str(item["id"])
            if ident not in found:
                continue
            if still is not None:
                await still()
            album, album_names = albums.get(ident, ("", None))
            if await self.at_publish(job, item, found[ident], album_names):
                given += 1
                if album_names:
                    with_album.append(item)
            elif album_names:
                refused.add(album)
        # All of an album's files get the album artists list or none: the others are made
        # again without it (nothing is placed yet).
        for item in with_album:
            if albums[str(item["id"])][0] not in refused:
                continue
            if still is not None:
                await still()
            try:
                unlisted = await self._unlist_unplaced(item)
            except Exception as exc:  # (its copy goes at the next recovery)
                log.info("artist lists: an imported file's copy is deleted later (%s)",
                         type(exc).__name__)  # fmt: skip
                given -= 1
                continue
            if unlisted and not await self.at_publish(job, item, found[str(item["id"])]):
                given -= 1
        return given

    async def _reconcile_albums(self, job_id: str, batch: list[dict[str, Any]]) -> None:
        """Files of this batch given the album artists list before a stop (or before the
        album's take-back ran), whose album's other files do not all have it - one refused,
        or not given its list yet: taken back first (nothing is placed yet), so this batch
        decides the album again, all of it or none. (A file placed already stays as it is:
        the background pass may complete its album.)"""
        rows = await self.imports.store.fetchall(
            "SELECT a.item_id, a.album_artists FROM artist_lists a JOIN music_import_items i"
            " ON i.id = a.item_id WHERE a.kind = 'import' AND a.state = 'written'"
            " AND a.phase = '' AND a.album_artists IS NOT NULL AND i.job_id = ?",
            [job_id],
        )
        if not rows:
            return
        listed_with = {str(r["item_id"]): str(r["album_artists"]) for r in rows}
        unplaced = {str(i["id"]): i for i in batch if i["state"] == "eligible"}
        for members in (await self._groups(job_id)).values():
            ids = [ident for ident, _, _ in members]
            if len({listed_with.get(ident) for ident in ids}) == 1 and ids[0] in listed_with:
                continue  # (whole)
            for ident in ids:
                if ident in listed_with and ident in unplaced:
                    try:
                        await self._unlist_unplaced(unplaced[ident])
                    except Exception as exc:  # (its copy goes at the next recovery)
                        log.info("artist lists: an imported file's copy is deleted later"
                                 " (%s)", type(exc).__name__)  # fmt: skip

    async def _unlist_unplaced(self, item: dict[str, Any]) -> bool:
        """An approved file given its list (``at_publish``) and not placed: its records
        name the file the copy was made from again (``item`` is updated), and the copy goes
        - its row, in phase 'write' with the copy's name and reservation, keeps that until
        its bytes are deleted (``_undo_publish_copy``; after a stop or a refused deletion,
        ``recover``). Returns whether its records name the original now."""
        store = self.imports.store
        key = import_key(str(item["id"]))
        row = await store.fetchone(
            "SELECT * FROM artist_lists WHERE key = ? AND state = 'written' AND phase = ''",
            [key],
        )
        if row is None or row["staging_after"] != item["staging_name"]:
            return False
        copy, reserved = str(row["staging_after"]), int(row["size_after"])
        async with store.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE music_import_items SET staging_name=?,expected_size=?,digest=?"
                " WHERE id=? AND state='eligible' AND staging_name=?",
                [row["staging_before"], row["size_before"], row["sha_before"], item["id"],
                 copy],
            )  # fmt: skip
            if cursor.rowcount != 1:
                return False
            await conn.execute(
                "UPDATE artist_lists SET state = 'failed', reason = 'album', phase = 'write',"
                " work = ?, updated_at = ? WHERE key = ?",
                [json.dumps({"staging": copy, "reserved": reserved}), self.clock(), key],
            )
        item.update(
            staging_name=row["staging_before"],
            expected_size=row["size_before"],
            digest=row["sha_before"],
        )
        # The copy, then its reservation and the row's phase (raises when refused).
        await self._undo_publish_copy(key, str(item["id"]), copy, reserved, "album")
        return True

    async def _groups(
        self, job_id: str
    ) -> dict[tuple[str, ...], list[tuple[str, dict[str, Any], dict[str, Any]]]]:
        """The files of the import that are or will be published, by album: (ID, probe
        facts, match) each."""
        rows = await self.imports.store.fetchall(
            "SELECT id,facts,match FROM music_import_items WHERE job_id=?"
            " AND state IN ('eligible','placing','placed','complete')",
            [job_id],
        )
        groups: dict[tuple[str, ...], list[tuple[str, dict[str, Any], dict[str, Any]]]] = {}
        for row in rows:
            try:
                facts = json.loads(row["facts"] or "{}")
                match = json.loads(row["match"] or "{}")
            except ValueError:
                continue
            if not isinstance(facts, dict) or not str(facts.get("album") or "").strip():
                continue
            found_tags = facts.get("album_tags")
            tags: dict[str, Any] = found_tags if isinstance(found_tags, dict) else {}
            first = first_track_artist(facts)
            key = album_key(tags, first)
            groups.setdefault(key, []).append(
                (str(row["id"]), facts, match if isinstance(match, dict) else {})
            )
        return groups

    async def _album_lists(
        self, job_id: str, found: Mapping[str, tuple[list[str], str]]
    ) -> dict[str, tuple[str, list[str]]]:
        """The album artists list of each file of a new album of the import (one it does
        not add to a library album) whose files all get the same list now, when its album
        artist text is exactly those names; with the album (its first file's ID)."""
        groups = await self._groups(job_id)
        out: dict[str, tuple[str, list[str]]] = {}
        for members in groups.values():
            lists = {tuple(found[i][0]) if i in found else None for i, _, _ in members}
            if len(lists) != 1 or None in lists or any(m.get("album_id") for _, _, m in members):
                continue
            ident, facts, _ = members[0]
            found_tags = facts.get("album_tags")
            album: dict[str, Any] = found_tags if isinstance(found_tags, dict) else {}
            shown = album_artist(album, first_track_artist(facts))
            names = listed(shown, found[ident][0])
            if names is not None:
                out.update((i, (ident, names)) for i, _, _ in members)
        return out

    async def at_publish(
        self,
        job: Mapping[str, Any],
        item: dict[str, Any],
        found: tuple[list[str], str],
        album_names: list[str] | None = None,
    ) -> bool:
        """Give an approved file about to be placed the list ``found`` (and the album
        artists list): the item's file becomes the copy (``item`` is updated). Returns
        whether it did; nothing changes otherwise."""
        facts = self._facts(item) if item["state"] == "eligible" else None
        if facts is None:
            return False
        artist = str(facts.get("artist") or "")
        names, source = found
        imports, store = self.imports, self.imports.store
        key = import_key(str(item["id"]))
        name = _staging_name(str(item["id"]), str(item["digest"]))
        reserved = int(item["expected_size"]) + COPY_SLACK
        try:
            async with imports._locks.hold("admission"):
                await imports._capacity(str(job["owner_id"]), reserved)
                async with store.transaction() as conn:
                    cursor = await conn.execute(
                        "UPDATE music_import_items SET reserved_size=reserved_size+?"
                        " WHERE id=? AND state='eligible' AND staging_name=?",
                        [reserved, item["id"], item["staging_name"]],
                    )
                    if cursor.rowcount != 1:
                        return False
                    await conn.execute(
                        "INSERT INTO artist_lists (key, kind, path, item_id, artist, state,"
                        " reason, phase, work, updated_at) VALUES"
                        " (?, 'import', ?, ?, ?, 'failed', 'interrupted', 'write', ?, ?)"
                        " ON CONFLICT (key) DO UPDATE SET state = 'failed',"
                        " reason = 'interrupted', phase = 'write', work = excluded.work,"
                        " updated_at = excluded.updated_at",
                        [
                            key,
                            self.published(item),
                            item["id"],
                            artist,
                            json.dumps({"staging": name, "reserved": reserved}),
                            self.clock(),
                        ],
                    )
        except ImportError as exc:
            log.info("artist lists: an imported file published without its list: %s", exc.code)
            return False
        committed = False
        try:
            await anyio.to_thread.run_sync(imports.storage.discard_attempt, name)
            values = {"artists": names, **({"albumartists": album_names} if album_names else {})}
            made = await self._copy(item, name, {"values": values, "pin": True, "artist": artist})
            previous: dict[str, None] = dict.fromkeys(values)
            if made.pinned:
                previous["albumartist"] = None
            async with store.transaction() as conn:
                cursor = await conn.execute(
                    "UPDATE music_import_items SET staging_name=?,expected_size=?,digest=?,"
                    "reserved_size=reserved_size-?+? WHERE id=? AND state='eligible'"
                    " AND staging_name=?",
                    [name, made.size, made.sha256, reserved, made.size, item["id"],
                     item["staging_name"]],
                )  # fmt: skip
                if cursor.rowcount != 1:
                    raise ListError("import_busy")
                await conn.execute(
                    "UPDATE artist_lists SET state = 'written', reason = '', source = ?,"
                    " artists = ?, album_artists = ?, previous = ?, size_before = ?,"
                    " sha_before = ?,"
                    " raw_before = ?, size_after = ?, sha_after = ?, staging_before = ?,"
                    " staging_after = ?, phase = '', work = NULL, updated_at = ?,"
                    " retry_at = NULL WHERE key = ?",
                    [
                        source,
                        json.dumps(names),
                        json.dumps(album_names) if album_names else None,
                        json.dumps(previous),
                        item["expected_size"],
                        item["digest"],
                        made.raw_before,
                        made.size,
                        made.sha256,
                        item["staging_name"],
                        name,
                        self.clock(),
                        key,
                    ],
                )
            committed = True
            item.update(staging_name=name, expected_size=made.size, digest=made.sha256)
            return True
        except BaseException as exc:
            if not committed:
                with anyio.CancelScope(shield=True):
                    await self._undo_publish_copy(key, str(item["id"]), name, reserved,
                                                  getattr(exc, "reason", "failed"))  # fmt: skip
            if isinstance(exc, Exception):
                log.info("artist lists: an imported file published without its list: %s",
                         getattr(exc, "reason", type(exc).__name__))  # fmt: skip
                return False
            raise

    async def _undo_publish_copy(
        self, key: str, item_id: str, name: str, reserved: int, reason: str
    ) -> None:
        store = self.imports.store
        row = await store.fetchone(
            "SELECT staging_name FROM music_import_items WHERE id=?", [item_id]
        )
        if row is not None and row["staging_name"] != name:
            await anyio.to_thread.run_sync(self.imports.storage.discard_attempt, name)
        async with store.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE artist_lists SET state = ?, reason = ?, phase = '', work = NULL,"
                " retry_at = ? WHERE key = ? AND phase = 'write'",
                [
                    "skipped" if reason in _LASTING else "failed",
                    reason,
                    self.clock() + 86400.0,
                    key,
                ],
            )
            if cursor.rowcount:
                await conn.execute(
                    "UPDATE music_import_items SET reserved_size=max(0,reserved_size-?)"
                    " WHERE id=? AND staging_name<>?",
                    [reserved, item_id, name],
                )

    # --- later --------------------------------------------------------------------

    async def write_many(
        self, entries: Sequence[tuple[str, Mapping[str, Any], Settled, Mapping[str, Any]]]
    ) -> dict[str, Made | ListError]:
        """Give published import files their lists (module docstring): each entry is a
        song ID, the subprocess request, ``settled(song, size)`` and the row's other
        columns (``source``, ``artists``, ``album_artists``, ``album_id``, ``artist``). The
        files are swapped before one scan; one Navidrome does not show as it should is
        rolled back alone. Returns each song's ``Made``, or its ``ListError`` (nothing
        changed for it)."""
        imports = self.imports
        outcomes: dict[str, Made | ListError] = {}
        async with imports._worker:
            items: dict[str, dict[str, Any]] = {}
            for song_id, *_ in entries:
                item = await self.item_of(song_id)
                if item is None:
                    outcomes[song_id] = ListError("import_busy")
                else:
                    items[song_id] = item
            async with AsyncExitStack() as held:
                for job_id in sorted({str(i["job_id"]) for i in items.values()}):
                    await held.enter_async_context(imports._locks.hold(job_id))
                await imports.upgrades.recover()
                swapped: list[_Swap] = []
                try:
                    for song_id, request, settled, record in entries:
                        if song_id not in items:
                            continue
                        item = await self.item_of(song_id)
                        if item is None:
                            outcomes[song_id] = ListError("import_busy")
                            continue
                        try:
                            swapped.append(await self._swap_in(item, request, settled, record))
                        except ListError as exc:
                            outcomes[song_id] = exc
                    shown = await self._shown(swapped)
                    for one in swapped:
                        song_id = str(one.item["song_id"])
                        if song_id not in shown:
                            with anyio.CancelScope(shield=True):
                                await self._swap_back(one)
                            outcomes[song_id] = ListError("not_confirmed")
                            continue
                        await self._at("write verified")
                        try:
                            with anyio.CancelScope(shield=True):
                                await self._commit(one)
                        except Exception as exc:  # (this one rolled back; the others go on)
                            with anyio.CancelScope(shield=True):
                                await self._swap_back(one)
                            outcomes[song_id] = (
                                exc if isinstance(exc, ListError) else ListError(type(exc).__name__)
                            )
                            continue
                        outcomes[song_id] = one.made
                except BaseException:
                    with anyio.CancelScope(shield=True):
                        for one in swapped:
                            if str(one.item["song_id"]) not in outcomes:
                                await self._swap_back(one)
                    raise
        return outcomes

    async def write(
        self,
        song_id: str,
        request: Mapping[str, Any],
        settled: Settled,
        record: Mapping[str, Any],
    ) -> Made:
        """One file's ``write_many``; raises its ``ListError``."""
        outcome = (await self.write_many([(song_id, request, settled, record)]))[song_id]
        if isinstance(outcome, ListError):
            raise outcome
        return outcome

    async def _swap_in(
        self,
        item: dict[str, Any],
        request: Mapping[str, Any],
        settled: Settled,
        record: Mapping[str, Any],
    ) -> _Swap:
        """Reserve, make the copy, and swap it in as an upgrade swaps (its intent the
        item's ``upgrade``); ``ListError`` with everything rolled back."""
        imports, store, storage = self.imports, self.imports.store, self.imports.storage
        owner = str(item["owner_id"])
        name = _staging_name(str(item["id"]), str(item["digest"]))
        reserved = int(item["expected_size"]) + COPY_SLACK
        intent: dict[str, Any] = {"phase": CARRY, "staging": name, "reserved": reserved}
        # The published file still as the item records it: one edited in place since (a
        # tag editor changes the retained file through its link) is not written.
        try:
            held = await anyio.to_thread.run_sync(
                lambda: storage.holds(
                    item["final_name"],
                    item["staging_name"],
                    int(item["expected_size"]),
                    item["digest"],
                )
            )
        except ImportError as exc:
            raise ListError("edited" if exc.code == "identity" else exc.code) from None
        if not held:
            raise ListError("edited")
        try:
            async with imports._locks.hold("admission"):
                await imports._capacity(owner, reserved)
                changed = await updated(
                    store,
                    "UPDATE music_import_items SET upgrade=?,"
                    "reserved_size=reserved_size+? WHERE id=? AND state='complete'"
                    " AND upgrade=''",
                    [json.dumps(intent), reserved, item["id"]],
                )
        except ImportError as exc:
            raise ListError(exc.code) from None
        if not changed:
            raise ListError("import_busy")
        one = _Swap(item, Made(0, "", "", ""), settled, record, intent)
        try:
            await anyio.to_thread.run_sync(storage.discard_attempt, name)
            one.made = await self._copy(item, name, request)
            if one.made.size > reserved:
                raise ListError("space")
            following = {
                "phase": SWAP,
                "staging": name,
                "final": item["final_name"],
                "size": one.made.size,
                "sha256": one.made.sha256,
                "reserved": one.made.size,
            }
            await store.execute(
                "UPDATE music_import_items SET upgrade=?,"
                "reserved_size=reserved_size-?+? WHERE id=? AND upgrade=?",
                [json.dumps(following), reserved, one.made.size, item["id"],
                 json.dumps(intent)],
            )  # fmt: skip
            one.intent = following
            if imports.write_gate is not None:
                await imports.write_gate()
            await anyio.to_thread.run_sync(imports.upgrades._swap_files, item, following)
            await self._at("write swapped")
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await self._swap_back(one)
            if isinstance(exc, ListError):
                raise
            if isinstance(exc, ImportError):
                raise ListError(exc.code) from None
            if isinstance(exc, Exception):
                raise ListError(type(exc).__name__) from None
            raise
        return one

    async def _shown(self, swapped: Sequence[_Swap]) -> set[str]:
        """The songs of ``swapped`` Navidrome shows with their copies, after a scan (a few
        while some do not)."""
        shown: set[str] = set()
        if not swapped:
            return shown
        for _ in range(CONFIRM_SCANS):
            try:
                await self.imports.scan([self.imports.storage.folder])  # (as an upgrade's)
                for one in swapped:
                    song_id = str(one.item["song_id"])
                    if song_id in shown:
                        continue
                    song = await self.navidrome.song(song_id)
                    if (
                        song is not None
                        and song.get("path") == self.published(one.item)
                        and one.settled(song, one.made.size)
                    ):
                        shown.add(song_id)
            except (NavidromeError, RuntimeError) as exc:
                log.info("artist lists: a scan did not confirm imported files: %s", exc)
            if len(shown) == len(swapped):
                break
        return shown

    async def _commit(self, one: _Swap) -> None:
        item, made = one.item, one.made
        async with self.imports.store.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE music_import_items SET staging_name=?,expected_size=?,"
                "digest=?,upgrade='' WHERE id=? AND state='complete' AND upgrade=?",
                [one.intent["staging"], made.size, made.sha256, item["id"],
                 json.dumps(one.intent)],
            )  # fmt: skip
            if cursor.rowcount != 1:
                raise ListError("import_busy")
            await _record(
                conn,
                import_key(str(item["id"])),
                str(item["song_id"]),
                item,
                self.published(item),
                one.record,
                made,
                self.clock(),
            )

    async def _swap_back(self, one: _Swap) -> None:
        """An uncommitted swap rolled back as an interrupted upgrade is (the file it was
        back under its name, the copy and its reservation gone)."""
        try:
            await self.imports.upgrades._roll_back(
                dict(one.item, upgrade=json.dumps(one.intent)), str(one.item["owner_id"])
            )
        except Exception as failure:
            log.info("artist lists: an imported file's swap is undone later (%s)",
                     type(failure).__name__)  # fmt: skip

    # --- undo ---------------------------------------------------------------------

    async def undo(self, row: Mapping[str, Any], settled: Settled) -> str:
        """One file's ``undo_many``: "" when done, "declined" when it changed, "gone",
        else why not now (``ListError`` for a failure)."""
        outcome = (await self.undo_many([(row, settled)]))[str(row["key"])]
        if isinstance(outcome, ListError):
            raise outcome
        return outcome

    async def undo_many(
        self, entries: Sequence[tuple[Mapping[str, Any], Settled]]
    ) -> dict[str, str | ListError]:
        """Put the retained files back under their published names, for files still as
        Shijhon left them, before one scan. Each row's outcome: "" when done, "declined"
        when the file changed (left alone), "gone", another reason when not now, or a
        ``ListError`` (nothing changed for it)."""
        imports = self.imports
        outcomes: dict[str, str | ListError] = {}
        async with imports._worker:
            items: dict[str, dict[str, Any]] = {}
            for row, _ in entries:
                item = await self._item(str(row["item_id"]))
                if item is None:
                    outcomes[str(row["key"])] = "gone"
                else:
                    items[str(row["key"])] = item
            async with AsyncExitStack() as held:
                for job_id in sorted({str(i["job_id"]) for i in items.values()}):
                    await held.enter_async_context(imports._locks.hold(job_id))
                swapped: list[tuple[Mapping[str, Any], dict[str, Any], Settled]] = []
                try:
                    for row, settled in entries:
                        key = str(row["key"])
                        if key not in items:
                            continue
                        item = await self._item(str(row["item_id"]))
                        if item is None:
                            outcomes[key] = "gone"
                            continue
                        why = await self._undoable(row, item)
                        if why:
                            outcomes[key] = why
                            continue
                        try:
                            await self._undo_in(row, item)
                        except ListError as exc:
                            outcomes[key] = exc
                            continue
                        swapped.append((row, item, settled))
                    shown = await self._shown_back(swapped)
                    for row, item, _ in swapped:
                        key = str(row["key"])
                        if key not in shown:
                            with anyio.CancelScope(shield=True):
                                await self._back(row, item)
                            outcomes[key] = ListError("not_confirmed")
                            continue
                        try:
                            with anyio.CancelScope(shield=True):
                                await self._undo_commit(row, item)
                        except Exception as exc:  # (this one put back; the others go on)
                            with anyio.CancelScope(shield=True):
                                await self._back(row, item)
                            outcomes[key] = (
                                exc if isinstance(exc, ListError) else ListError(type(exc).__name__)
                            )
                            continue
                        outcomes[key] = ""
                        await self._drop(row)
                except BaseException:
                    with anyio.CancelScope(shield=True):
                        for row, item, _ in swapped:
                            if str(row["key"]) not in outcomes:
                                await self._back(row, item)
                    raise
        return outcomes

    async def _undoable(self, row: Mapping[str, Any], item: Mapping[str, Any]) -> str:
        """Why an import file's list is not taken back now ("" : it is)."""
        storage = self.imports.storage
        if item["state"] != "complete" or item["upgrade"]:
            return "import_busy"
        removal = await self._removal(str(item["job_id"]))
        if removal == "removed":
            return "import_removed"  # (kept: a restored import can still be given back)
        if removal in _REMOVING:
            return "import_busy"
        if (item["staging_name"], item["digest"], item["expected_size"]) != (
            row["staging_after"],
            row["sha_after"],
            row["size_after"],
        ):
            return "declined"
        try:
            held = await anyio.to_thread.run_sync(
                lambda: storage.holds(
                    item["final_name"],
                    row["staging_after"],
                    row["size_after"],
                    row["sha_after"],
                )
            )
            await anyio.to_thread.run_sync(
                lambda: storage.verify(row["staging_before"], row["size_before"], row["sha_before"])
            )
        except FileNotFoundError:
            return "declined"
        except ImportError as exc:
            # (The imports root unreachable: told later. Other bytes: the file changed.)
            return exc.code if exc.code == "storage" else "declined"
        return "" if held else "declined"

    async def _undo_in(self, row: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        imports, storage = self.imports, self.imports.storage
        changed = await updated(
            imports.store,
            "UPDATE artist_lists SET phase = 'undo', work = '{}' WHERE key = ? AND phase = ''",
            [row["key"]],
        )
        if not changed:
            raise ListError("import_busy")
        try:
            if imports.write_gate is not None:
                await imports.write_gate()
            await anyio.to_thread.run_sync(_touch, storage, row["staging_before"])
            await anyio.to_thread.run_sync(
                _unlink_ours, storage, item["final_name"], row["staging_after"]
            )
            await self._at("undo unlinked")
            await anyio.to_thread.run_sync(storage.place, row["staging_before"], item["final_name"])
            await self._at("undo placed")
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await self._back(row, item)
            if isinstance(exc, ListError):
                raise
            if isinstance(exc, ImportError):
                raise ListError(exc.code) from None
            if isinstance(exc, Exception):
                raise ListError(type(exc).__name__) from None
            raise

    async def _shown_back(
        self, swapped: Sequence[tuple[Mapping[str, Any], dict[str, Any], Settled]]
    ) -> set[str]:
        shown: set[str] = set()
        if not swapped:
            return shown
        for _ in range(CONFIRM_SCANS):
            try:
                await self.imports.scan([self.imports.storage.folder])
                for row, item, settled in swapped:
                    if str(row["key"]) in shown:
                        continue
                    song = await self.navidrome.song(str(item["song_id"]))
                    if (
                        song is not None
                        and song.get("path") == self.published(item)
                        and settled(song, int(row["size_before"]))
                    ):
                        shown.add(str(row["key"]))
            except (NavidromeError, RuntimeError) as exc:
                log.info("artist lists: a scan did not confirm imported files: %s", exc)
            if len(shown) == len(swapped):
                break
        return shown

    async def _undo_commit(self, row: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        async with self.imports.store.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE music_import_items SET staging_name=?,expected_size=?,"
                "digest=?,reserved_size=max(0,reserved_size-?) WHERE id=?"
                " AND staging_name=?",
                [row["staging_before"], row["size_before"], row["sha_before"],
                 row["size_after"], item["id"], row["staging_after"]],
            )  # fmt: skip
            if cursor.rowcount != 1:
                raise ListError("import_busy")
            await conn.execute(
                "UPDATE artist_lists SET state = 'undone', phase = 'drop' WHERE key = ?",
                [row["key"]],
            )

    async def _item(self, item_id: str) -> dict[str, Any] | None:
        found = await self.imports.store.fetchone(
            "SELECT i.*,j.owner_id FROM music_import_items i JOIN music_import_jobs j"
            " ON j.id=i.job_id WHERE i.id=?",
            [item_id],
        )
        return dict(found) if found is not None else None

    async def _back(self, row: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        """An undo not committed: the copy back under the published name, the row's phase
        cleared once a scan of the imports' folder has run (until then ``recover`` does
        this again)."""
        storage = self.imports.storage

        def files() -> None:
            _unlink_ours(
                storage, item["final_name"], row["staging_before"],
                keep=row["staging_after"], missing_ok=True,
            )  # fmt: skip
            try:
                descriptor = storage.file(item["final_name"], final=True)
            except FileNotFoundError:
                _touch(storage, row["staging_after"])
                storage.place(row["staging_after"], item["final_name"])
            else:
                os.close(descriptor)

        try:
            await anyio.to_thread.run_sync(files)
        except Exception as exc:
            log.warning("artist lists: an imported file's undo is put back later (%s)",
                        type(exc).__name__)  # fmt: skip
            return
        try:
            await self.imports.scan([self.imports.storage.folder])
        except Exception as exc:
            # (The row keeps its phase: the next pass's recovery asks for the scan again.)
            log.info("artist lists: Navidrome reads the imported file later (%s)",
                     type(exc).__name__)  # fmt: skip
            return
        await self.imports.store.execute(
            "UPDATE artist_lists SET phase = '', work = NULL WHERE key = ?", [row["key"]]
        )

    async def _drop(self, row: Mapping[str, Any]) -> None:
        """After an undo's commit: the copy goes, then the row."""
        storage = self.imports.storage
        with anyio.CancelScope(shield=True):
            item = await self._item(str(row["item_id"]))
            if item is None or item["staging_name"] != row["staging_after"]:
                try:
                    await anyio.to_thread.run_sync(
                        storage.discard_unpublished, row["staging_after"]
                    )
                except (ImportError, OSError) as exc:
                    log.warning("artist lists: an imported file's copy is not deleted (%s)",
                                getattr(exc, "code", type(exc).__name__))  # fmt: skip
                    return
            await self.imports.store.execute(
                "DELETE FROM artist_lists WHERE key = ? AND state = 'undone'", [row["key"]]
            )
            await self.imports.store.execute(
                "UPDATE artist_lists SET phase = '', work = NULL WHERE key = ?", [row["key"]]
            )

    async def recover(self) -> int:
        """Finish what a stop left (an interrupted swap of a published file is the
        upgrades' to roll back): an undo not committed goes back, a copy after an undo's
        commit goes, a copy made at publication but not recorded goes."""
        store = self.imports.store
        rows = await store.fetchall(
            "SELECT * FROM artist_lists WHERE kind = 'import' AND phase <> ''"
        )
        done = 0
        async with self.imports._worker:
            for row in rows:
                item = await self._item(str(row["item_id"]))
                if row["phase"] == "drop":
                    await self._drop(dict(row))
                elif row["phase"] == "undo" and item is not None:
                    await self._back(dict(row), item)
                elif row["phase"] == "write":
                    work = json.loads(row["work"] or "{}")
                    await self._undo_publish_copy(
                        str(row["key"]), str(row["item_id"]), str(work.get("staging") or ""),
                        int(work.get("reserved") or 0), "interrupted",
                    )  # fmt: skip
                else:
                    await store.execute(
                        "UPDATE artist_lists SET phase = '', work = NULL WHERE key = ?",
                        [row["key"]],
                    )
                done += 1
        return done


# Reasons a file will not take its list later either (asked again only after a while).
_LASTING = frozenset({"format", "listed", "identified", "other_tags", "identity", "edited"})


async def _record(
    conn: Any,
    key: str,
    song_id: str,
    item: Mapping[str, Any],
    relative: str,
    record: Mapping[str, Any],
    made: Made,
    now: float,
) -> None:
    # (Its earlier outcomes, by song, give way to this record by import item.)
    await conn.execute(
        "DELETE FROM artist_lists WHERE key = ? AND kind = 'import' AND phase = ''", [song_id]
    )
    previous = {"artists": None}
    if record.get("album_artists"):
        previous["albumartists"] = None
    if made.pinned:
        previous["albumartist"] = None
    await conn.execute(
        "INSERT INTO artist_lists (key, song_id, kind, path, item_id, artist, state, reason,"
        " source, artists, album_artists, previous, album_id, size_before, sha_before,"
        " raw_before, size_after, sha_after, staging_before, staging_after, phase, work,"
        " updated_at, retry_at) VALUES (?, ?, 'import', ?, ?, ?, 'written', '', ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, ?, NULL) ON CONFLICT (key) DO UPDATE SET"
        " song_id = excluded.song_id, path = excluded.path, item_id = excluded.item_id,"
        " artist = excluded.artist, state = 'written', reason = '', source = excluded.source,"
        " artists = excluded.artists, album_artists = excluded.album_artists,"
        " previous = excluded.previous, album_id = excluded.album_id,"
        " size_before = excluded.size_before, sha_before = excluded.sha_before,"
        " raw_before = excluded.raw_before, size_after = excluded.size_after,"
        " sha_after = excluded.sha_after, staging_before = excluded.staging_before,"
        " staging_after = excluded.staging_after, phase = '', work = NULL,"
        " updated_at = excluded.updated_at, retry_at = NULL",
        [
            key,
            song_id,
            relative,
            item["id"],
            record.get("artist") or "",
            record.get("source") or "",
            json.dumps(list(record.get("artists") or [])),
            json.dumps(list(record["album_artists"])) if record.get("album_artists") else None,
            json.dumps(previous),
            record.get("album_id"),
            item["expected_size"],
            item["digest"],
            made.raw_before,
            made.size,
            made.sha256,
            item["staging_name"],
            _staging_name(str(item["id"]), str(item["digest"])),
            now,
        ],
    )
