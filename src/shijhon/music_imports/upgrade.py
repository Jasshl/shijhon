"""Upgrading a person's own imported song from the add-ons after they play it.

When someone plays one of their own imported songs whose file is below their minimum
quality, the quality gate plays it from the add-ons (``delivery/quality_gate.py``). With
"Upgrade my imports" on (each person's choice, on unless they switch it off), the add-ons'
audio - when it meets their minimum - then becomes the song's file: the whole of it,
fetched in the background as download-first does, in whatever format it comes (FLAC stays
FLAC; nothing is converted), carrying every tag of the imported file (``retag.py``,
``carry``) so the song keeps its ID, entry, favorites, plays and playlist entries.

Only the person's own imports: never a song someone else imported, never one they only
linked (``overlap.LINKED``), never a file of the music folder, never a song at or above
their minimum, never while the import is being removed.

The swap stays in step when stopped at any point: a durable intent (``upgrade`` on the
import item) commits before the files move; the upgraded file takes the imported file's
place (same folder and base name, its own suffix); a scan and the importer's own native
read must show the same song ID at the new path with the new size; only then does the
item record the upgraded file as its published file (``upgraded_from`` keeps what it
replaced). Anything short of that - a mismatch, an error, a stop - puts the imported file
back and drops the intent: the next start finishes such a roll back first. The replaced
file stays retained privately in the imports root, counted against the person's storage
as the upgraded one is. Removing the import takes the upgraded file out; restoring it puts
the upgraded file back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

import anyio
import anyio.abc

from shijhon.music_imports.model import ImportError
from shijhon.quality import Minimum, Quality

if TYPE_CHECKING:
    from shijhon.music_imports.service import MusicImports

log = logging.getLogger(__name__)

CARRY_SLACK = 1024**2  # the upgraded file's tags may be larger than the add-on's
# An upgrade's recorded phase (``music_import_items.upgrade``): its bytes being made, the
# swap under way, the imported file back and Navidrome to show it again.
CARRY, SWAP, VERIFY = "carry", "swap", "verify"
# Removal states during which an import's files are not upgraded.
_REMOVING = ("reviewing", "review", "checking", "removing", "removed", "restoring")
# The add-ons' audio in every container whose tags Shijhon can write and Navidrome reads
# (``tagmap.py``; ``delivery/download_first._EVERY``).
_SUFFIXES = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".oga", ".spx", ".wav",
                       ".aiff", ".dsf", ".tta", ".ape", ".wv", ".mpc", ".wma"})  # fmt: skip


def _staging_name(item_id: str, digest: str) -> str:
    return hashlib.sha256(f"upgrade:{item_id}:{digest}".encode()).hexdigest()[:32] + ".part"


class Upgrades:
    """Upgrades of imported songs (module docstring), under the import service's worker
    lock: never beside a publication, a removal or another upgrade."""

    def __init__(self, imports: MusicImports) -> None:
        self.imports = imports

    async def own_import(self, user_id: str, song_id: str) -> dict[str, Any] | None:
        """The import item that published ``song_id`` for ``user_id`` (None: the song is
        not that person's own import, or its import is being removed)."""
        if not user_id or not song_id:
            return None
        store = self.imports.store
        row = await store.fetchone(
            "SELECT i.* FROM music_import_items i JOIN music_import_jobs j ON j.id=i.job_id"
            " WHERE i.song_id=? AND i.state='complete' AND i.upgrade='' AND j.owner_id=?"
            " AND j.library_id=?",
            [song_id, user_id, self.imports.library_id],
        )
        if row is None:
            return None
        removal = await store.fetchone(
            "SELECT state FROM music_import_removals WHERE job_id=?", [row["job_id"]]
        )
        if removal is not None and removal["state"] in _REMOVING:
            return None
        return dict(row)

    async def upgrade(self, user_id: str, song_id: str, fetched: Path, quality: Quality) -> str:
        """Make ``fetched`` (the add-ons' whole audio of the song) the song's file. Returns
        "" when it is, else why not (nothing changed)."""
        suffix = fetched.suffix.lower()
        if suffix not in _SUFFIXES:
            return "the add-ons' audio is not a format that can carry the song's tags"
        imports = self.imports
        async with imports._worker:
            item = await self.own_import(user_id, song_id)
            if item is None:
                return "not the person's own imported song"
            async with imports._locks.hold(item["job_id"]):
                await self.recover()
                item = await self.own_import(user_id, song_id)
                if item is None:
                    return "not the person's own imported song"
                return await self._upgrade(user_id, item, fetched, suffix, quality)

    async def _upgrade(
        self, owner: str, item: dict[str, Any], fetched: Path, suffix: str, quality: Quality
    ) -> str:
        imports, storage = self.imports, self.imports.storage
        size = (await anyio.Path(fetched).stat()).st_size
        name = _staging_name(item["id"], item["digest"])
        reserved = size + CARRY_SLACK
        # Reserved and journaled before its bytes exist (admission is one at a time, as
        # declarations are): a stop leaves an intent that recovery takes back.
        async with imports._locks.hold("admission"):
            await imports._capacity(owner, reserved)
            async with imports.store.transaction() as conn:
                cursor = await conn.execute(
                    "UPDATE music_import_items SET upgrade=?,reserved_size=reserved_size+?"
                    " WHERE id=? AND state='complete' AND upgrade=''",
                    [
                        json.dumps({"phase": CARRY, "staging": name, "reserved": reserved}),
                        reserved,
                        item["id"],
                    ],
                )
                changed = cursor.rowcount
        if not changed:
            return "the imported song changed meanwhile"
        intent: dict[str, Any] = {"phase": CARRY, "staging": name, "reserved": reserved}
        try:
            await anyio.to_thread.run_sync(storage.discard_attempt, name)
            made = await self._carry(item, fetched, name)
            if made is None or made["size"] > reserved:
                raise _Refused("the add-ons' audio could not carry the imported song's tags")
            old_final = item["final_name"]
            new_final = str(PurePosixPath(old_final).with_suffix(suffix))
            if new_final != old_final:
                with storage.final_parent(new_final) as (parent, leaf):
                    try:
                        os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise _Refused("another file is at the upgraded song's path")
            intent = {
                "phase": SWAP,
                "staging": name,
                "final": new_final,
                "size": made["size"],
                "sha256": made["sha256"],
                "quality": quality.describe(),
                "reserved": made["size"],
            }
            async with imports.store.transaction() as conn:
                await conn.execute(
                    "UPDATE music_import_items SET upgrade=?,reserved_size=reserved_size-?+?"
                    " WHERE id=? AND state='complete'",
                    [json.dumps(intent), reserved, made["size"], item["id"]],
                )
            # Right before the original's name goes: the person's current access, and
            # Navidrome's settings that keep a missing file's identity (as every swap).
            await imports._actor(owner)
            if imports.write_gate is not None:
                await imports.write_gate()
            before = await self._native(owner, item["song_id"])
            await anyio.to_thread.run_sync(self._swap_files, item, intent)
            await imports.scan([storage.folder])
            native = await imports.native_lookup(
                owner, imports.library_id, storage.folder + "/" + intent["final"]
            )
            if native is None or native.song_id != item["song_id"] or native.size != made["size"]:
                raise _Refused("Navidrome did not keep the song's identity for the upgraded file")
            after = await self._native(owner, item["song_id"])
            if before is not None and _described(before) != _described(after):
                raise _Refused("Navidrome did not read the upgraded file's details as before")
            with anyio.CancelScope(shield=True):
                await self._commit(item, intent)
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                try:
                    await self._roll_back(dict(item, upgrade=json.dumps(intent)), owner)
                except Exception as failure:
                    log.error("an upgrade of an imported song is not undone yet (%s): put right"
                              " later", type(failure).__name__)  # fmt: skip
            if isinstance(exc, _Refused):
                return str(exc)
            if isinstance(exc, Exception):
                log.warning("upgrade of an imported song failed: %s", type(exc).__name__)
                return "the upgrade failed"
            raise
        try:
            await imports.refresh(owner, imports.library_id)
        except Exception as exc:  # the next refresh learns it
            log.info("the library view learns the upgraded file later: %s", type(exc).__name__)
        return ""

    async def _native(self, owner: str, song_id: str) -> Mapping[str, Any] | None:
        """The importing caller's own native read of the song (None without one)."""
        if self.imports.native_song is None:
            return None
        return await self.imports.native_song(owner, self.imports.library_id, song_id)

    async def _carry(self, item: dict[str, Any], fetched: Path, name: str) -> dict[str, Any] | None:
        """The upgraded file, made in a bounded subprocess (``retag.py``, ``carry``)."""
        imports = self.imports
        source = imports.storage.file(item["staging_name"])
        audio = target = -1
        process: anyio.abc.Process | None = None
        try:
            audio = os.open(fetched, os.O_RDONLY | os.O_NOFOLLOW)
            target = imports.storage.copy_target(name)
            with anyio.fail_after(imports.limits.validation_seconds * 8):
                process = await anyio.open_process(
                    [sys.executable, "-m", "shijhon.music_imports.retag",
                     str(source), str(target), str(audio)],
                    pass_fds=(source, target, audio),
                    start_new_session=True,
                    stderr=-3,
                )  # fmt: skip
                assert process.stdin and process.stdout
                request = {
                    "mode": "carry",
                    "source": PurePosixPath(item["final_name"]).suffix,
                    "audio": fetched.suffix,
                }
                await process.stdin.send(json.dumps(request).encode())
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
                or not 0 < result["size"] <= imports.limits.max_file_bytes + 1024**2
                or not isinstance(result.get("sha256"), str)
            ):
                return None
            os.fsync(imports.storage.incoming)
            await anyio.to_thread.run_sync(
                lambda: imports.storage.verify(name, result["size"], result["sha256"])
            )
            return result
        except (ValueError, TimeoutError, OSError, ImportError):
            return None
        finally:
            for descriptor in (source, audio, target):
                if descriptor >= 0:
                    os.close(descriptor)
            if process:
                with anyio.CancelScope(shield=True):
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.aclose()

    def _swap_files(self, item: dict[str, Any], intent: dict[str, Any]) -> None:
        """The upgraded file takes the imported file's place (its own suffix)."""
        storage = self.imports.storage
        old_final, new_final = item["final_name"], intent["final"]
        if new_final == old_final:
            _unlink_ours(storage, old_final, item["staging_name"])
            storage.place(intent["staging"], new_final)
        else:
            storage.place(intent["staging"], new_final)
            _unlink_ours(storage, old_final, item["staging_name"])

    async def _commit(self, item: dict[str, Any], intent: dict[str, Any]) -> None:
        """The upgraded file is the song's (its reservation, made before its bytes, stays)."""
        replaced = {
            "staging": item["staging_name"],
            "final": item["final_name"],
            "size": item["expected_size"],
            "sha256": item["digest"],
            "quality": json.loads(item["upgraded_from"] or "{}").get("upgraded_to")
            or _quality_words(item),
            "upgraded_to": intent["quality"],
            "at": time.time(),
        }
        original = json.loads(item["upgraded_from"] or "{}")
        if original:
            original.setdefault("upgrades", []).append(replaced)
            original["upgraded_to"] = intent["quality"]
            replaced = original
        async with self.imports.store.transaction() as conn:
            await conn.execute(
                "UPDATE music_import_items SET staging_name=?,final_name=?,expected_size=?,"
                "digest=?,upgrade='',upgraded_from=? WHERE id=? AND state='complete'",
                [
                    intent["staging"],
                    intent["final"],
                    intent["size"],
                    intent["sha256"],
                    json.dumps(replaced),
                    item["id"],
                ],
            )

    async def recover(self) -> int:
        """Roll back every upgrade a stop interrupted (its intent is still recorded), each
        on its own: one that cannot finish yet (its owner's native read is not at hand,
        say) waits for a later turn. Returns how many were put right."""
        rows = await self.imports.store.fetchall(
            "SELECT i.*,j.owner_id FROM music_import_items i"
            " JOIN music_import_jobs j ON j.id=i.job_id WHERE i.upgrade<>''"
            " ORDER BY i.id LIMIT 50"
        )
        done = 0
        for row in rows:
            try:
                await self._roll_back(dict(row), row["owner_id"])
            except Exception as exc:
                log.info("an interrupted upgrade waits to be undone: %s", type(exc).__name__)
                continue
            done += 1
        return done

    async def _roll_back(self, item: dict[str, Any], owner: str) -> None:
        """Undo an upgrade by its recorded phase: before the swap (``carry``) its private
        bytes and reservation go; once the swap may have begun (``swap``) the imported
        file is put back at its name, the upgraded bytes go and Navidrome must then show
        the song at that path again (``verify``: a scan and the owner's exact native read
        of its ID, path and size) before the intent goes."""
        intent = json.loads(item["upgrade"] or "{}")
        storage, store = self.imports.storage, self.imports.store
        phase = intent.get("phase") or SWAP

        def original_present() -> bool:
            original = storage.file(item["staging_name"])
            try:
                with storage.final_parent(item["final_name"]) as (parent, leaf):
                    current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                kept = os.fstat(original)
                return (current.st_dev, current.st_ino) == (kept.st_dev, kept.st_ino)
            except FileNotFoundError:
                return False
            finally:
                os.close(original)

        def replacement_present() -> bool:
            with storage.final_parent(intent["final"]) as (parent, leaf):
                try:
                    os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return False
            return True

        untouched = (
            phase == SWAP
            and await anyio.to_thread.run_sync(original_present)
            and (
                intent.get("final") == item["final_name"]
                or not await anyio.to_thread.run_sync(replacement_present)
            )
        )
        if phase == SWAP and not untouched and not intent.get("verify"):
            # Remember that native confirmation is needed *before* restoring the link.
            # A stop during rollback must not later mistake that link for an untouched one.
            intent["verify"] = True
            recorded = json.dumps(intent)
            await store.execute(
                "UPDATE music_import_items SET upgrade=? WHERE id=? AND upgrade=?",
                [recorded, item["id"], item["upgrade"]],
            )
            item["upgrade"] = recorded
        untouched = untouched and not intent.get("verify")

        def files() -> None:
            if phase == SWAP and intent.get("staging") and intent.get("final"):
                with suppress(FileNotFoundError):
                    _unlink_ours(
                        storage,
                        intent["final"],
                        intent["staging"],
                        keep=item["staging_name"],
                        missing_ok=True,
                    )
                try:
                    descriptor = storage.file(item["final_name"], final=True)
                except FileNotFoundError:
                    storage.place(item["staging_name"], item["final_name"])
                else:
                    os.close(descriptor)
            if intent.get("staging"):
                storage.discard_attempt(intent["staging"])

        if phase in (CARRY, SWAP):
            await anyio.to_thread.run_sync(files)
            following = "" if phase == CARRY or untouched else json.dumps({"phase": VERIFY})
            async with store.transaction() as conn:
                await conn.execute(
                    "UPDATE music_import_items SET upgrade=?,reserved_size=max(0,reserved_size-?)"
                    " WHERE id=? AND upgrade=?",
                    [following, int(intent.get("reserved") or 0), item["id"], item["upgrade"]],
                )
            if not following:
                return
        await self.imports.scan([storage.folder])
        native = await self.imports.native_lookup(
            owner, self.imports.library_id, storage.folder + "/" + item["final_name"]
        )
        if (
            native is None
            or native.song_id != item["song_id"]
            or native.size != item["expected_size"]
        ):
            raise ImportError("indexing", "Navidrome has not shown the imported file again yet.")
        await store.execute(
            "UPDATE music_import_items SET upgrade='' WHERE id=? AND upgrade<>''", [item["id"]]
        )


class Choices(Protocol):
    """Each person's choices (``delivery/quality_gate.Minimums``)."""

    async def user_id(self, username: str) -> str | None: ...
    async def upgrades(self, user_id: str) -> bool: ...
    async def minimum_for(
        self, *, user_id: str | None = None, username: str | None = None
    ) -> Minimum: ...


class Played(Protocol):
    """The song a play went to the add-ons for (``delivery/quality_gate.LibrarySong``)."""

    @property
    def song_id(self) -> str: ...
    @property
    def quality(self) -> Quality: ...
    def track(self) -> Any: ...


class AfterPlay:
    """Called after a play went to the add-ons because the song's file is below the
    listener's minimum (``Interceptor.upgrade_after``): the listener's own imported song
    is upgraded with the add-ons' audio when they leave upgrades switched on and that
    audio meets their minimum and is better than the file - fetched as the play went: an
    add-on whose audio is below the minimum is passed over for the next one. One at a
    time, in the background; a song already being upgraded is not asked for twice."""

    def __init__(
        self,
        upgrades: Upgrades,
        choices: Choices,
        fetch: Callable[[Any, Minimum], Awaitable[Path | str]],
        forget: Callable[[str], None],
        spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], bool],
    ) -> None:
        self.upgrades, self.choices, self.fetch, self.forget = upgrades, choices, fetch, forget
        self.spawn = spawn
        self.upgrading: set[str] = set()
        self.one = anyio.Semaphore(1)

    def __call__(self, username: str, song: Played) -> None:
        if song.song_id in self.upgrading:
            return
        self.upgrading.add(song.song_id)

        async def work() -> None:
            await self.run(username, song)

        if not self.spawn(work):
            self.upgrading.discard(song.song_id)

    async def run(self, username: str, song: Played) -> str:
        """The upgrade (module docstring): "" when the song was upgraded, else why not."""
        self.upgrading.add(song.song_id)
        fetched: Path | str | None = None
        try:
            async with self.one:
                user_id = await self.choices.user_id(username)
                if not user_id or not await self.choices.upgrades(user_id):
                    return "upgrades are switched off"
                if await self.upgrades.own_import(user_id, song.song_id) is None:
                    return "not the person's own imported song"
                minimum = await self.choices.minimum_for(user_id=user_id)
                fetched = await self.fetch(song.track(), minimum)
                if isinstance(fetched, str):
                    log.info("an imported song not upgraded: %s", fetched)
                    return fetched
                got = await anyio.to_thread.run_sync(file_quality, fetched)
                if not got.known() or not minimum.met_by(got) or got.rank() <= song.quality.rank():
                    log.info("an imported song not upgraded: the add-ons' audio is no better")
                    return "the add-ons' audio is no better"
                why = await self.upgrades.upgrade(user_id, song.song_id, fetched, got)
                if why:
                    log.info("an imported song not upgraded: %s", why)
                    return why
                self.forget(song.song_id)
                log.info("an imported song upgraded from the add-ons (%s)", got.describe())
                return ""
        except Exception as exc:  # the next play tries again
            log.warning("upgrade of an imported song failed: %s", type(exc).__name__)
            return "the upgrade failed"
        finally:
            self.upgrading.discard(song.song_id)
            if isinstance(fetched, Path):
                fetched.unlink(missing_ok=True)


class _Refused(Exception):
    """An upgrade not made, and why (nothing changed)."""


# The native fields an upgraded file must read back as the imported file did.
_DESCRIBED = (
    "title", "album", "artist", "albumArtist", "albumId", "trackNumber", "discNumber",
    "year", "releaseDate", "originalDate", "compilation", "mbzRecordingID", "mbzAlbumId",
    "mbzReleaseTrackId", "bpm", "genre", "comment",
)  # fmt: skip


def _described(song: Mapping[str, Any] | None) -> tuple[Any, ...]:
    """What Navidrome shows of a song besides its file: its fields, ISRCs and whether it
    has lyrics."""
    song = song or {}
    tags = song.get("tags") if isinstance(song.get("tags"), Mapping) else {}
    lyrics = song.get("lyrics")
    return (
        *(song.get(name) or None for name in _DESCRIBED),
        tuple(sorted(tags.get("isrc") or ())) if isinstance(tags, Mapping) else (),
        bool(lyrics) and lyrics != "[]",
    )


def _unlink_ours(
    storage: Any, final: str, staging: str, *, keep: str = "", missing_ok: bool = False
) -> bool:
    """Unlink the published name ``final`` when it is the retained ``staging`` file's
    link (never anything else). ``keep``: another retained file whose link may be there
    instead (an upgrade of the same format not swapped yet): it stays. Returns whether it
    was unlinked. ``missing_ok``: rollback ignores a destination it never installed."""
    source = storage.file(staging)
    kept = storage.file(keep) if keep else -1
    try:
        expected = os.fstat(source)
        with storage.final_parent(final) as (parent, leaf):
            try:
                current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if kept >= 0:
                other = os.fstat(kept)
                if (current.st_dev, current.st_ino) == (other.st_dev, other.st_ino):
                    return False
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                if missing_ok:
                    return False
                raise ImportError("changed", "The published import changed. Check storage.")
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)
            return True
    finally:
        os.close(source)
        if kept >= 0:
            os.close(kept)


def file_quality(path: Path) -> Quality:
    """The quality of an add-on's whole audio file (not known: unreadable)."""
    import mutagen

    from shijhon.music_imports import facts
    from shijhon.music_imports.overlap import quality_of

    try:
        parsed = mutagen.File(path)
    except mutagen.MutagenError:
        return Quality()
    if parsed is None:
        return Quality()
    rate = int(getattr(parsed.info, "sample_rate", 0) or 0)
    return quality_of(facts.quality(parsed, path.suffix.lstrip(".").lower(), rate))


def _quality_words(item: dict[str, Any]) -> str:
    from shijhon.music_imports.overlap import quality_of

    try:
        facts = json.loads(item["facts"] or "{}")
    except ValueError:
        return ""
    return quality_of(facts.get("quality") if isinstance(facts, dict) else None).describe()
