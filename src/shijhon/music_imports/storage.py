"""Descriptor-relative retained storage, one installation and one writer per root."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from shijhon.music_imports.model import ImportError, relative_path

_NAME = re.compile(r"[0-9a-f]{32}(?:\.(?:mp3|flac|part))?\Z")
# An earlier import's published file that is neither gone nor holding its bytes.
_UNTOLD = "An earlier import's published file was replaced or changed. Stop and check storage."
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _final_parts(name: str) -> list[str]:
    # Existing opaque receipts remain valid; new receipts retain the source tree
    # below a server-generated batch directory. Never allow private root entries.
    if _NAME.fullmatch(name) and not name.endswith(".part"):
        return [name]
    parts = name.split("/")
    if not re.fullmatch(r"batch-[0-9a-f]{32}", parts[0]) or len(parts) < 2:
        raise ImportError("storage", "The import storage receipt is invalid.")
    relative_path("/".join(parts[1:]))
    return parts


def _open_directory(path: Path) -> int:
    """Reject every symlink component, including trusted configured ancestors."""
    if not path.is_absolute() or ".." in path.parts:
        raise ImportError("storage", "Configure an absolute imports library path.")
    descriptor = os.open("/", _DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class Storage:
    def __init__(self, library: Path, folder: str, managed: str) -> None:
        if (
            folder in ("", ".", "..", managed)
            or folder.startswith(".")
            or any(ch in folder for ch in "/\\\x00")
            or len(folder.encode()) > 100
            or any(ord(ch) < 32 for ch in folder)
        ):
            raise ImportError("storage", "Choose a separate imports folder beside managed music.")
        self.library, self.folder = library, folder
        self.fd = self.incoming = self.lock = -1
        self.root_id = ""

    def open_readonly(self, installation: str, library_id: str) -> None:
        """Inspect existing retained state; no lock/create/chmod/marker repair."""
        try:
            self.fd = _open_directory(self.library / self.folder)
            marker = os.open(".import-root", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
            try:
                if (
                    not stat.S_ISREG(os.fstat(marker).st_mode)
                    or os.fstat(marker).st_nlink != 1
                    or json.loads(os.read(marker, 4097))
                    != {"installation": installation, "library": library_id, "version": 1}
                ):
                    raise OSError
            finally:
                os.close(marker)
            self.incoming = os.open(".incoming", _DIRECTORY, dir_fd=self.fd)
            root = os.fstat(self.fd)
            self.root_id = hashlib.sha256(
                json.dumps(
                    [installation, library_id, root.st_dev, root.st_ino, self.folder]
                ).encode()
            ).hexdigest()
        except BaseException:
            self.close()
            raise ImportError(
                "storage", "Existing imported music storage could not be verified."
            ) from None

    def open(self, installation: str, library_id: str) -> None:
        try:
            parent = _open_directory(self.library)
            try:
                # Provisioning/mounting is the parent's job. Never create a missing mount.
                self.fd = os.open(self.folder, _DIRECTORY, dir_fd=parent)
            finally:
                os.close(parent)
            self.lock = os.open(
                ".import-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
            )
            if not stat.S_ISREG(os.fstat(self.lock).st_mode) or os.fstat(self.lock).st_nlink != 1:
                raise OSError
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            expected = {"installation": installation, "library": library_id, "version": 1}
            try:
                marker = os.open(".import-root", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
            except FileNotFoundError:
                if set(os.listdir(self.fd)) != {".import-lock"}:
                    raise ImportError(
                        "storage", "The imports root is not an empty dedicated root."
                    ) from None
                marker = os.open(
                    ".import-root",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.fd,
                )
                try:
                    os.write(marker, json.dumps(expected, sort_keys=True).encode())
                    os.fsync(marker)
                finally:
                    os.close(marker)
                os.fsync(self.fd)
            else:
                try:
                    if (
                        not stat.S_ISREG(os.fstat(marker).st_mode)
                        or os.fstat(marker).st_nlink != 1
                        or json.loads(os.read(marker, 4097)) != expected
                    ):
                        raise ImportError("storage", "This imports root belongs to other state.")
                finally:
                    os.close(marker)
            with suppress(FileExistsError):
                os.mkdir(".incoming", 0o700, dir_fd=self.fd)
            self.incoming = os.open(".incoming", _DIRECTORY, dir_fd=self.fd)
            os.fchmod(self.incoming, 0o700)
            ignore = os.open(
                ".ndignore",
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.incoming,
            )
            try:
                if not stat.S_ISREG(os.fstat(ignore).st_mode) or os.fstat(ignore).st_nlink != 1:
                    raise OSError
                os.fsync(ignore)
            finally:
                os.close(ignore)
            os.fsync(self.incoming)
            os.fsync(self.fd)
            root = os.fstat(self.fd)
            self.root_id = hashlib.sha256(
                json.dumps(
                    [installation, library_id, root.st_dev, root.st_ino, self.folder]
                ).encode()
            ).hexdigest()
        except BaseException as exc:
            self.close()
            if isinstance(exc, ImportError):
                raise
            raise ImportError(
                "storage", "The dedicated imports root is unavailable or locked."
            ) from None

    def check(self) -> None:
        """A renamed mount/root is a configuration failure, never a new destination."""
        if self.fd < 0:
            raise ImportError("storage", "The dedicated imports root is unavailable.")
        descriptor = -1
        try:
            descriptor = _open_directory(self.library / self.folder)
            current, held = os.fstat(descriptor), os.fstat(self.fd)
            child = os.stat(".incoming", dir_fd=self.fd, follow_symlinks=False)
            incoming = os.fstat(self.incoming)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino) or (
                child.st_dev,
                child.st_ino,
            ) != (incoming.st_dev, incoming.st_ino):
                raise OSError
        except OSError:
            raise ImportError(
                "storage", "The dedicated imports root changed. Stop and check storage."
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def free(self) -> int:
        self.check()
        volume = os.fstatvfs(self.fd)
        return volume.f_bavail * volume.f_frsize

    @contextmanager
    def final_parent(self, name: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        """Open a final receipt's parent without following any symlink.

        The descriptor belongs to this context. Creation fsyncs every new entry
        before publication; reads never create/repair anything. Flat v1 receipts
        and their root marker remain compatible.
        """
        parts = _final_parts(name)
        self.check()
        held = [os.dup(self.fd)]
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=held[-1])
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(held[-1])
                child = os.open(part, _DIRECTORY, dir_fd=held[-1])
                held.append(child)
                if create:
                    # Published originals can be scanned by a separate native
                    # account even when the service runs with a private umask.
                    # Also completes a crash between mkdir and this fsync.
                    os.fchmod(child, 0o755)
                    os.fsync(child)
            self._check_parents(parts, held)
            yield held[-1], parts[-1]
            self._check_parents(parts, held)
        finally:
            for descriptor in reversed(held):
                os.close(descriptor)

    def _check_parents(self, parts: list[str], held: list[int]) -> None:
        self.check()
        for parent, child, part in zip(held, held[1:], parts[:-1], strict=False):
            actual = os.stat(part, dir_fd=parent, follow_symlinks=False)
            expected = os.fstat(child)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ImportError("storage", "An import folder changed. Stop and check storage.")

    def file(self, name: str, *, final: bool = False, create: bool = False) -> int:
        if not final and not _NAME.fullmatch(name):
            raise ImportError("storage", "The import storage receipt is invalid.")
        self.check()
        flags = os.O_NOFOLLOW | (os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY)
        if final:
            descriptor = -1
            try:
                with self.final_parent(name) as (parent, leaf):
                    descriptor = os.open(leaf, flags, 0o600, dir_fd=parent)
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                raise
        else:
            descriptor = os.open(name, flags, 0o600, dir_fd=self.incoming)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > (1 if create else 2):
            os.close(descriptor)
            raise ImportError("storage", "An import file changed unexpectedly.")
        return descriptor

    def copy_target(self, name: str) -> int:
        """A new private file for a published copy, open for reading and writing (its
        tags are written in it). Never an existing file."""
        if not _NAME.fullmatch(name) or not name.endswith(".part"):
            raise ImportError("storage", "The import storage receipt is invalid.")
        self.check()
        return os.open(
            name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.incoming
        )

    def discard_attempt(self, name: str) -> None:
        if not _NAME.fullmatch(name):
            raise ImportError("storage", "The import storage receipt is invalid.")
        self.check()
        try:
            os.unlink(name, dir_fd=self.incoming)
            os.fsync(self.incoming)
        except FileNotFoundError:
            pass

    def holds(self, final: str, staging: str, size: int, digest: str) -> bool:
        """Whether the published file ``final`` still holds exactly an earlier import's
        bytes: ``size`` bytes of SHA-256 ``digest``. Its retained ``staging`` file itself
        (the same inode) is taken as it is; any other file of that size at that path - one
        put in its place, say - is read (at most ``size`` bytes) and holds them when its
        digest is the same. False when it is gone, or of another size. Anything else at
        that path - such a file with other bytes, something that is not a file, a file
        changing while it is read - is neither: it raises (``identity``), as does a changed
        imports root (``storage``)."""
        if not _NAME.fullmatch(staging):
            raise ImportError("storage", "The import storage receipt is invalid.")
        self.check()
        descriptor = -1
        try:
            with self.final_parent(final) as (parent, leaf):
                descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except BaseException as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
                return False
            if isinstance(exc, OSError):
                raise ImportError("identity", _UNTOLD) from None
            raise
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ImportError("identity", _UNTOLD)
            if before.st_size != size:
                return False
            try:
                kept = os.stat(staging, dir_fd=self.incoming, follow_symlinks=False)
            except FileNotFoundError:
                kept = None
            if kept is not None and (kept.st_dev, kept.st_ino) == (before.st_dev, before.st_ino):
                return True
            hashed = hashlib.sha256()
            count = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                count += len(chunk)
                if count > size:
                    break
                hashed.update(chunk)
            after = os.fstat(descriptor)
            if (
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                or count != size
                or hashed.hexdigest() != digest
            ):
                raise ImportError("identity", _UNTOLD)
            return True
        finally:
            os.close(descriptor)

    def verify(self, name: str, size: int, digest: str, *, final: bool = False) -> None:
        descriptor = self.file(name, final=final)
        try:
            before = os.fstat(descriptor)
            hashed = hashlib.sha256()
            count = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                count += len(chunk)
                if count > size:
                    break
                hashed.update(chunk)
            after = os.fstat(descriptor)
            if (
                count != size
                or hashed.hexdigest() != digest
                or before.st_size != size
                or after.st_size != size
                or (before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ImportError("changed", "The received audio changed. Review the files again.")
        finally:
            os.close(descriptor)

    def place(self, staging: str, final: str) -> None:
        self.check()
        if not _NAME.fullmatch(staging):
            raise ImportError("storage", "The import storage receipt is invalid.")
        source = self.file(staging)
        try:
            # Private staging + the root lock exclude application writers. Detect a
            # replaced source before accepting a publication from an external writer.
            expected = os.fstat(source)
            current = os.stat(staging, dir_fd=self.incoming, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise ImportError("changed", "The received audio changed before publication.")
            # Staging's directory remains 0700; the visible original must be readable
            # by a separately running native scanner under the provisioned music root.
            os.fchmod(source, 0o644)
            os.fsync(source)
            # Atomic link fails if *anything* occupies the name. Never replace.
            with self.final_parent(final, create=True) as (parent, leaf):
                os.link(
                    staging,
                    leaf,
                    src_dir_fd=self.incoming,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                os.fsync(parent)
                placed = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if (placed.st_dev, placed.st_ino) != (expected.st_dev, expected.st_ino):
                raise ImportError("changed", "The placed audio changed. Stop and check storage.")
        finally:
            os.close(source)

    def close(self) -> None:
        for name in ("incoming", "lock", "fd"):
            descriptor: Any = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)
