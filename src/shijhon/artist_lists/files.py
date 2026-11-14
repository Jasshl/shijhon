"""Giving a library file Shijhon did not write its artist lists, and taking them back.

The new file is a copy made by the music imports' bounded tag subprocess
(``music_imports/retag.py``, ``lists``): only the artist list names change, every other
tag as Navidrome reads it stays as it was, and so does the song's identity. The copy is
made beside the file (a hidden name Navidrome does not read), with the file's mode and
owner, synced, and takes the file's name in one rename; the file's modification time
changes, which is how Navidrome notices it.

Not written: a link, a file with other links (hard links), one on another file system than
its folder, one that cannot be written or whose owner Shijhon cannot keep, a format
without a writable tag family (and Matroska), a file that changed meanwhile, one that
already lists its artists or has MusicBrainz artist identifiers (``Skip``).

A write stays in step when stopped at any point: its intent (the names of the copy and of
a second link to the file it replaces) is recorded before either exists; the replaced file
keeps that link until a targeted scan shows Navidrome read the new file with the song's
ID, its album and the artists listed, and the record says so (the commit). The files of
an album are written together before one scan of their folders; one that Navidrome does
not show as it should is put back alone. Anything short
of that puts the old file back under its name (with the time of now: Navidrome reads
again only a file newer than its record); the next start finishes what a stop left
(``recover``), and a file put back by a stop that could not wait for a scan keeps its row
until a targeted scan of its folder has been asked for. An undo is the same swap the other
way, accepted only for a file still as Shijhon left it, and only when the tags it gives
back are the tags the file had.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import secrets
import signal
import stat
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import anyio
import anyio.abc

from shijhon.music_imports import tagmap
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.navidrome.scans import ScanCoordinator, ScanInterrupted, ScanTimeout
from shijhon.placeholders import durable
from shijhon.store import Store

log = logging.getLogger(__name__)

# A second link to a file cannot be made here (some unions, file systems without hard
# links): its backup is a whole copy instead.
NO_LINKS = frozenset(
    {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.ENOSYS}
)

CHUNK = 1024 * 1024
MAKE_SECONDS = 60.0  # the subprocess, at most (plus a second a MiB)
OUTPUT_BYTES = 4096


class ListError(Exception):
    """A write or undo not made (the file as it was); ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def make_copy(
    source: int, target: int, request: Mapping[str, Any], size: int
) -> dict[str, Any]:
    """Run the tag subprocess on two open descriptors: the copy's size and SHA-256 and its
    tag digests, or ``{"refused": reason}``. ``ListError("failed")`` when it failed."""
    process: anyio.abc.Process | None = None
    try:
        with anyio.fail_after(MAKE_SECONDS + size / CHUNK):
            process = await anyio.open_process(
                [sys.executable, "-m", "shijhon.music_imports.retag", str(source), str(target)],
                pass_fds=(source, target),
                start_new_session=True,
                stderr=-3,
            )
            assert process.stdin and process.stdout
            await process.stdin.send(json.dumps({**request, "mode": "lists"}).encode())
            await process.stdin.aclose()
            output = bytearray()
            try:
                while True:
                    output.extend(await process.stdout.receive(1024))
                    if len(output) > OUTPUT_BYTES:
                        raise ValueError
            except anyio.EndOfStream:
                pass
            if await process.wait() != 0:
                raise ValueError
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError
        if "refused" in result:
            return {"refused": str(result["refused"])[:40]}
        if type(result.get("size")) is not int or not isinstance(result.get("sha256"), str):
            raise ValueError
        return result
    except (ValueError, TimeoutError, OSError):
        raise ListError("failed") from None
    finally:
        if process:
            with anyio.CancelScope(shield=True):
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.aclose()


async def updated(store: Store, sql: str, params: Sequence[Any]) -> int:
    """How many rows one statement changed (``Store.execute`` gives the last row ID)."""
    async with store.transaction() as conn:
        cursor = await conn.execute(sql, list(params))
        return int(cursor.rowcount)


def file_digest(path: Path) -> tuple[int, str]:
    hashed, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            hashed.update(chunk)
            size += len(chunk)
    return size, hashed.hexdigest()


@dataclass(frozen=True)
class Made:
    size: int
    sha256: str
    raw_before: str
    raw_after: str
    pinned: bool = False  # the album artist Navidrome showed was set too
    size_before: int = 0  # the file it replaced
    sha_before: str = ""


Settled = Callable[[dict[str, Any] | None, int], bool]


@dataclass(frozen=True)
class Entry:
    """A file's write or undo (``LibraryFiles.write_many``)."""

    key: str
    relative: str
    request: Mapping[str, Any]
    settled: Settled
    commit: Callable[[Made], tuple[str, list[Any]]]
    what: str = "write"
    check: Callable[[Made], None] | None = None


@dataclass
class _Prepared:
    entry: Entry
    path: Path
    folder: str
    work: dict[str, str]
    seen: os.stat_result
    made: Made
    swapped: bool = False  # put back over the copy (Navidrome must read it again)


class LibraryFiles:
    """Library files Shijhon did not write (its own folders are another's work)."""

    def __init__(
        self,
        root: Path,
        store: Store,
        navidrome: NavidromeService,
        scans: ScanCoordinator,
        *,
        not_in: tuple[str, ...] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.store = store
        self.navidrome = navidrome
        self.scans = scans
        self.not_in = tuple(f.strip("/") + "/" for f in not_in if f.strip("/"))
        self.clock = clock
        # Test hook: awaited at each named step of a write or an undo.
        self.step: Callable[[str], Awaitable[None]] | None = None

    async def _at(self, step: str) -> None:
        if self.step is not None:
            await self.step(step)

    def absolute(self, relative: str) -> Path:
        """The file of a library-relative path: inside the library, in none of Shijhon's
        own folders, reached through no link (``ListError`` otherwise)."""
        parts = relative.split("/")
        if (
            not relative
            or relative.startswith("/")
            or any(p in ("", ".", "..") for p in parts)
            or "\x00" in relative
            or any(relative.startswith(f) for f in self.not_in)
        ):
            raise ListError("outside")
        path = self.root.joinpath(*parts)
        real, root = os.path.realpath(path.parent), os.path.realpath(self.root)
        if real != root and os.path.commonpath([root, real]) != root:
            raise ListError("link")
        if real != os.path.normpath(os.path.join(root, *parts[:-1])):
            raise ListError("link")
        return path

    def there(self, path: Path) -> bool:
        """Whether a missing file's absence can be trusted: only when its own folder is
        there with something in it, and is not the library's own folder. A folder that is
        missing or empty may be a disk or a share not mounted now (also below a library
        whose other folders are there, or the library itself), and the library's folder
        may join several disks (a union) of which the file's is missing: the file may come
        back with it, so nothing is told."""
        if os.path.normpath(path.parent) == os.path.normpath(self.root):
            return False
        try:
            with os.scandir(path.parent) as entries:
                return any(True for _ in entries)
        except OSError:
            return False

    def skip(self, relative: str) -> str | None:
        """Why the file cannot be given its list (None: it can be tried)."""
        try:
            path = self.absolute(relative)
            info = os.lstat(path)
        except ListError as exc:
            return exc.reason
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unreadable"
        if stat.S_ISLNK(info.st_mode):
            return "link"
        if not stat.S_ISREG(info.st_mode):
            return "format"
        if info.st_nlink > 1:
            return "hard_link"
        if info.st_dev != os.stat(path.parent).st_dev:
            # (Another file system than its folder's: the copy made beside it could not take
            # its name in one rename. A union's files share the union's device.)
            return "other_mount"
        suffix = path.suffix.lower().lstrip(".")
        if suffix not in tagmap.PARSERS:
            return "format"
        if not os.access(path, os.R_OK | os.W_OK) or not os.access(path.parent, os.W_OK | os.X_OK):
            return "read_only"
        if os.geteuid() != 0 and (
            info.st_uid != os.geteuid() or info.st_gid not in {os.getegid(), *os.getgroups()}
        ):
            return "owner"
        return None

    # --- the swap -------------------------------------------------------------------

    async def write(
        self,
        key: str,
        relative: str,
        request: Mapping[str, Any],
        settled: Settled,
        commit: Callable[[Made], tuple[str, list[Any]]],
        what: str = "write",
        check: Callable[[Made], None] | None = None,
    ) -> Made:
        """One file's ``write_many``; raises its ``ListError``."""
        outcome = (
            await self.write_many([Entry(key, relative, request, settled, commit, what, check)])
        )[key]
        if isinstance(outcome, ListError):
            raise outcome
        return outcome

    async def write_many(self, entries: Sequence[Entry]) -> dict[str, Made | ListError]:
        """Give each entry's file the copy its ``request`` makes, all of them before one
        targeted scan of their folders: ``settled(song, size)`` says whether Navidrome's
        song shows a file's copy, ``check(made)`` raises ``ListError`` when a copy will not
        do (before anything moved), ``commit(made)`` is its row's update (it must leave
        ``phase = 'drop'``). A file whose copy Navidrome does not show is put back alone.
        Returns each entry's ``Made``, or its ``ListError`` (the file as it was, the row's
        phase cleared - or, put back over a copy and its scan not run, kept for
        ``recover``)."""
        outcomes: dict[str, Made | ListError] = {}
        prepared: list[_Prepared] = []
        committed: set[str] = set()
        try:
            for entry in entries:
                try:
                    prepared.append(await self._prepare(entry))
                except ListError as exc:
                    outcomes[entry.key] = exc
            shown: set[str] = set()

            async def ready() -> bool:
                for one in prepared:
                    if one.entry.key not in shown:
                        song = await self.navidrome.song(key_song(one.entry.key))
                        if one.entry.settled(song, one.made.size):
                            shown.add(one.entry.key)
                return len(shown) == len(prepared)

            if prepared:
                try:
                    await self.scans.until(sorted({one.folder for one in prepared}), ready)
                except (ScanTimeout, ScanInterrupted, NavidromeError) as exc:
                    log.info("artist lists: a scan did not confirm the files: %s", exc)
            unshown = [one for one in prepared if one.entry.key not in shown]
            with anyio.CancelScope(shield=True):
                for one in unshown:
                    await self._restore_one(one)
                    outcomes[one.entry.key] = ListError("not_confirmed")
            await self._reread(unshown)
            for one in prepared:
                if one.entry.key not in shown:
                    continue
                await self._at(f"{one.entry.what} verified")
                with anyio.CancelScope(shield=True):
                    await self.store.execute(*one.entry.commit(one.made))
                committed.add(one.entry.key)
                outcomes[one.entry.key] = one.made
                await self._at(f"{one.entry.what} committed")
        except BaseException:
            # Files first; Navidrome reads them later: the rows keep that a scan is owed
            # (``recover``, at the next pass or start), as none can be waited for now.
            with anyio.CancelScope(shield=True):
                for one in prepared:
                    if one.entry.key not in committed and one.entry.key not in outcomes:
                        await self._restore_one(one)
            raise
        finally:
            with anyio.CancelScope(shield=True):
                for one in prepared:
                    if one.entry.key in committed:
                        await self._drop(one.entry.key, one.path, one.work)
        return outcomes

    async def _prepare(self, entry: Entry) -> _Prepared:
        """A file's copy in place of it, its old file kept by a second link (the intent
        recorded first); ``ListError`` with the file as it was."""
        try:
            path = self.absolute(entry.relative)
            seen = await anyio.to_thread.run_sync(os.lstat, path)
            before = await anyio.to_thread.run_sync(file_digest, path)
        except FileNotFoundError:
            raise ListError("missing") from None
        except OSError as exc:
            raise ListError(type(exc).__name__) from None
        folder = entry.relative.rsplit("/", 1)[0] if "/" in entry.relative else ""
        token = secrets.token_hex(8)
        work = {
            "temp": f".shijhon-{token}.tmp",
            "backup": f".shijhon-{token}.keep",
            "part": f".shijhon-{token}.part",  # (a whole-copy backup until it is complete)
            # The old file's bytes: a backup goes back over the name only when it is them.
            "size": str(before[0]),
            "sha": before[1],
        }
        changed = await updated(
            self.store,
            "UPDATE artist_lists SET phase = ?, work = ? WHERE key = ? AND phase = ''",
            [entry.what, json.dumps(work), entry.key],
        )
        if not changed:
            raise ListError("busy")
        one = _Prepared(entry, path, folder, work, seen, None)  # type: ignore[arg-type]
        temp, backup = path.with_name(work["temp"]), path.with_name(work["backup"])
        try:
            await self._at(f"{entry.what} intended")
            one.made = replace(
                await self._make(path, temp, entry.request, seen),
                size_before=before[0],
                sha_before=before[1],
            )
            if entry.check is not None:
                entry.check(one.made)
            await self._at(f"{entry.what} staged")

            def swap() -> None:
                now = os.lstat(path)
                if (now.st_ino, now.st_size, now.st_mtime_ns) != (
                    seen.st_ino,
                    seen.st_size,
                    seen.st_mtime_ns,
                ):
                    raise ListError("changed")
                try:
                    os.link(path, backup, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno not in NO_LINKS:
                        raise
                    # No hard links here (as on some unions): a whole copy, under its own
                    # name until it is complete and on disk.
                    _copy_backup(path, path.with_name(work["part"]), backup, seen)
                durable.sync_dir(path.parent)

            await anyio.to_thread.run_sync(swap)
            await self._at(f"{entry.what} linked")

            def place() -> None:
                os.replace(temp, path)
                durable.sync_dir(path.parent)

            await anyio.to_thread.run_sync(place)
            await self._at(f"{entry.what} swapped")
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await self._restore_one(one)
            if isinstance(exc, Exception):
                await self._reread([one])
            if isinstance(exc, ListError):
                raise
            if isinstance(exc, (OSError, NavidromeError, RuntimeError)):
                raise ListError(type(exc).__name__) from None
            raise
        return one

    async def _make(
        self, path: Path, temp: Path, request: Mapping[str, Any], seen: os.stat_result
    ) -> Made:
        source = target = -1
        try:
            source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            target = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            result = await make_copy(
                source, target, {**request, "suffix": path.suffix.lower()}, seen.st_size
            )
            if "refused" in result:
                raise ListError(result["refused"])
            os.fchmod(target, stat.S_IMODE(seen.st_mode))
            if (seen.st_uid, seen.st_gid) != (os.geteuid(), os.getegid()):
                try:
                    os.fchown(target, seen.st_uid, seen.st_gid)
                except PermissionError:
                    raise ListError("owner") from None
            os.fsync(target)
            durable.sync_dir(path.parent)
            return Made(
                result["size"],
                result["sha256"],
                str(result.get("raw_before") or ""),
                str(result.get("raw_digest") or ""),
                result.get("pinned") is True,
            )
        finally:
            for descriptor in (source, target):
                if descriptor >= 0:
                    os.close(descriptor)

    async def _restore_one(self, one: _Prepared) -> None:
        """The file as it was under its name, the copy gone. A scan is owed first (its
        row's work says ``reread``, written before anything moves: a stop at any point
        after leaves it); when the file was put back over the copy, the row keeps it until
        a scan of its folder has run (``_reread``, else ``recover``), otherwise the phase
        is cleared now."""
        one.work = await self._owe_scan(one.entry.key, one.work)
        one.swapped = await anyio.to_thread.run_sync(self._restore, one.path, one.work)
        if not one.swapped:
            await self._done(one.entry.key)

    async def _owe_scan(self, key: str, work: Mapping[str, str]) -> dict[str, str]:
        """Record that Navidrome may have to read the file again (before it is put back)."""
        owed = {**work, "reread": "1"}
        await self.store.execute(
            "UPDATE artist_lists SET work = ? WHERE key = ?", [json.dumps(owed), key]
        )
        return owed

    async def _done(self, key: str) -> None:
        await self.store.execute(
            "UPDATE artist_lists SET phase = '', work = NULL WHERE key = ?", [key]
        )

    async def _reread(self, restored: Sequence[_Prepared]) -> None:
        """One scan of the folders of files put back, so that Navidrome reads them again
        (not waited for long); their rows are cleared once it ran. Cut short or failing,
        they keep the scan owed, which ``recover`` asks for."""
        back = [one for one in restored if one.swapped]
        if not back:
            return

        async def ready() -> bool:
            for one in back:
                if not await self._shows(one.entry.key, one.path, one.seen.st_size):
                    return False
            return True

        try:
            await self.scans.until(sorted({one.folder for one in back}), ready, attempts=2)
        except Exception as exc:  # the next recovery asks for the scan again
            log.info("artist lists: Navidrome reads the files put back later: %s", exc)
            return
        with anyio.CancelScope(shield=True):
            for one in back:
                await self._done(one.entry.key)

    async def _shows(self, key: str, path: Path, size: int) -> bool:
        song = await self.navidrome.song(key_song(key))
        return song is not None and song.get("size") == size

    @staticmethod
    def _restore(path: Path, work: Mapping[str, str]) -> bool:
        """Put the old file back by its backup - a second link, or a whole copy where the
        file system has no hard links - when the swap happened; the backup and the new copy
        go. Whether it happened is told by the files, never by inode numbers (a union may
        derive them from the path): the swap consumes the copy, so a backup beside the
        copy means it did not, and a backup without it means it did. A backup goes back
        over the name only when its bytes are the old file's. Returns whether the file
        under the name changed."""
        temp, backup = path.with_name(work["temp"]), path.with_name(work["backup"])
        swapped = False
        if os.path.lexists(backup):
            if os.path.lexists(temp):
                os.unlink(backup)  # (not swapped: the old file is still under the name)
            elif _is_old(backup, work):
                os.replace(backup, path)
                # Navidrome reads a file again only when it is newer than its record.
                os.utime(path)
                swapped = True
            else:
                # Not the old file's bytes: nothing is moved over the name; the backup
                # stays beside it for a person to look at.
                log.warning("artist lists: a backup is not the file it was made of: %s", backup)
        for leftover in (temp, path.with_name(work["part"])) if work.get("part") else (temp,):
            with suppress(FileNotFoundError):
                os.unlink(leftover)
        durable.sync_dir(path.parent)
        return swapped

    async def _drop(self, key: str, path: Path, work: Mapping[str, str]) -> None:
        """After the commit: the old file's second link goes, then the phase."""

        def drop() -> None:
            backup = path.with_name(work["backup"])
            try:
                kept, current = os.lstat(backup), os.lstat(path)
            except FileNotFoundError:
                return
            if (kept.st_dev, kept.st_ino) != (current.st_dev, current.st_ino):
                os.unlink(backup)
                durable.sync_dir(path.parent)

        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(drop)
            await self.store.execute(
                "UPDATE artist_lists SET phase = '', work = NULL WHERE key = ?", [key]
            )
            await self.store.execute(
                "DELETE FROM artist_lists WHERE key = ? AND state = 'undone'", [key]
            )

    async def recover(self) -> int:
        """Finish what a stop left: a write or undo not committed is put back, a backup
        after a commit goes; nothing while the music folder is not there (``there``).
        Returns how many were put right."""
        rows = await self.store.fetchall(
            "SELECT key, path, phase, work FROM artist_lists WHERE kind = 'file' AND phase <> ''"
        )
        done = 0
        for row in rows:
            work = json.loads(row["work"] or "{}")
            try:
                path = self.absolute(str(row["path"]))
            except ListError:
                await self.store.execute(
                    "UPDATE artist_lists SET phase = '', work = NULL WHERE key = ?", [row["key"]]
                )
                continue
            if not work.get("temp") or not work.get("backup"):
                await self.store.execute(
                    "UPDATE artist_lists SET phase = '', work = NULL WHERE key = ?", [row["key"]]
                )
                continue
            seen = await anyio.to_thread.run_sync(_any_of, path, work)
            if not seen and not await anyio.to_thread.run_sync(self.there, path):
                # The music folder is not there (not mounted yet): neither the file nor its
                # backup can be seen, so nothing is told; the next start looks again.
                log.warning(
                    "artist lists: a write a stop interrupted is put right once the music"
                    " folder is there again"
                )
                continue
            relative = str(row["path"])
            folder = relative.rsplit("/", 1)[0] if "/" in relative else ""
            try:
                if row["phase"] == "drop":
                    await self._drop(str(row["key"]), path, work)
                else:
                    # A scan owed already (the file may have been put back before a stop),
                    # or recorded before this put-back: a stop after it still scans.
                    owed = bool(work.get("reread"))
                    if not owed:
                        work = await self._owe_scan(str(row["key"]), work)
                    if await anyio.to_thread.run_sync(self._restore, path, work) or owed:
                        await self.scans.scan([folder])  # (failing: the next start asks)
                    await self._done(str(row["key"]))
            except (OSError, NavidromeError, RuntimeError) as exc:
                log.warning(
                    "artist lists: a write a stop interrupted is not put right yet (%s)",
                    type(exc).__name__,
                )
                continue
            done += 1
        return done


def _any_of(path: Path, work: Mapping[str, str]) -> bool:
    """Whether the file or one of its write's files is there: its folder's disk is."""
    names = (path.name, work["temp"], work["backup"], work.get("part") or work["temp"])
    return any(os.path.lexists(path.with_name(name)) for name in names)


def _is_old(backup: Path, work: Mapping[str, str]) -> bool:
    """Whether a backup holds the old file's bytes, as recorded before the write."""
    if not work.get("sha"):
        return True  # (no record of them: the backup is trusted, as it was made complete)
    try:
        size, sha = file_digest(backup)
    except OSError:
        return False
    return str(size) == work.get("size") and sha == work["sha"]


def _copy_backup(path: Path, part: Path, backup: Path, seen: os.stat_result) -> None:
    """A whole copy of the file beside it, with its mode and owner, on disk, then named
    as its backup: where a second link cannot be made (``NO_LINKS``). A copy cut short
    stays under ``part`` and is never taken for the backup."""
    source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        target = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            while chunk := os.read(source, CHUNK):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(target, view) :]
            os.fchmod(target, stat.S_IMODE(seen.st_mode))
            if (seen.st_uid, seen.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(target, seen.st_uid, seen.st_gid)
            os.fsync(target)
        finally:
            os.close(target)
    finally:
        os.close(source)
    os.rename(part, backup)


def key_song(key: str) -> str:
    """The Navidrome song ID of a row's key (a file's or a placeholder's)."""
    return key
