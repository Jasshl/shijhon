"""Where placeholder files live.

``<library>/<placeholder folder>/<album artist>/<album> [<hash>]/<disc>-<track> <title>.flac``

One folder per catalog release. Names are readable (clients that browse folders show
them) but carry no catalog name or ID; the short hash only keeps editions apart.
Files are prepared in a hidden staging folder on the same filesystem and moved into
place, so Navidrome never indexes a half-written file.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from shijhon.catalog.model import CatalogRef, CatalogTrack
from shijhon.placeholders import durable

_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


class LayoutError(ValueError):
    """A path that is not inside the placeholder folder: never written."""


def _trimmed(name: str) -> str:
    """Without dots and spaces at either end, in whatever order they come."""
    while (shorter := name.strip().strip(".")) != name:
        name = shorter
    return name


def safe_name(text: str, limit: int = 80) -> str:
    """One path component for a catalog's text: no separator, at most ``limit`` bytes,
    and no dot or space at either end - so never empty, ".", ".." or a hidden name (the
    staging folder's, or one Navidrome skips), whatever the text."""
    name = _trimmed(_UNSAFE.sub("_", unicodedata.normalize("NFC", text)))
    if len(name.encode()) > limit:
        name = _trimmed(name.encode()[:limit].decode("utf-8", "ignore"))
    return name or "_"


@dataclass(frozen=True)
class Layout:
    library_root: Path
    folder: str  # placeholder folder, relative to the library root

    @property
    def root(self) -> Path:
        return self.library_root / self.folder

    @property
    def staging(self) -> Path:
        """Where files are prepared, backed up during a swap and cleaned up. One that is a
        link out of the placeholder folder is refused (:class:`LayoutError`): nothing is
        written, moved or removed through it."""
        path = self.root / ".staging"
        if os.path.islink(path):
            real, root = os.path.realpath(path), os.path.realpath(self.root)
            if real == root or os.path.commonpath([root, real]) != root:
                raise LayoutError("the staging folder is a link out of the placeholder folder")
        return path

    def release_folder(self, ref: CatalogRef, artist: str, title: str) -> str:
        """Library-relative folder for a release."""
        digest = hashlib.sha256(str(ref).encode()).hexdigest()[:8]
        folder = str(
            PurePosixPath(self.folder, safe_name(artist), f"{safe_name(title)} [{digest}]")
        )
        self.contained(folder)
        return folder

    @staticmethod
    def file_name(track: CatalogTrack, suffix: str = ".flac") -> str:
        return f"{track.disc}-{track.number:02d} {safe_name(track.title, 100)}{suffix}"

    def contained(self, relative: str, *, resolve: bool = False, folder: bool = False) -> Path:
        """The absolute path of a library-relative path that lies inside the placeholder
        folder - below it, outside its staging folder, and named without "." or ".." -
        whatever names it was made from. :class:`LayoutError` for any other: the last
        check before a file is written. ``resolve``: also where it really is - a folder on
        the way that is a link out of the placeholder folder (the folder itself may be
        one) is refused; reads the file system. ``folder``: the path is a folder (a
        release's: what is in it is changed), so it must itself be no such link."""
        root, staging = os.path.normpath(self.root), os.path.normpath(self.staging)
        path = os.path.normpath(self.library_root / relative)
        if (
            relative.startswith("/")
            or "\x00" in relative
            or any(part in (".", "..") for part in relative.split("/"))
            or path == root
            or os.path.commonpath([root, path]) != root
            or os.path.commonpath([staging, path]) == staging
        ):
            raise LayoutError("a path outside the placeholder folder")
        if resolve:
            real_root = os.path.realpath(root)
            # (A file itself is replaced, never followed: its folder is what counts.)
            real = os.path.realpath(path if folder else os.path.dirname(path))
            if real != real_root and os.path.commonpath([real_root, real]) != real_root:
                raise LayoutError("a path that leads outside the placeholder folder")
        return self.library_root / relative

    def absolute(self, relative: str) -> Path:
        return self.library_root / relative

    def relative(self, path: Path) -> str:
        return str(PurePosixPath(path.relative_to(self.library_root)))

    def is_placeholder_path(self, relative: str) -> bool:
        return relative.startswith(self.folder.rstrip("/") + "/")

    def ensure(self) -> None:
        """The placeholder folder and its staging folder (hidden from Navidrome) are there.
        What a first write on a fresh installation makes here is on disk before anything
        depends on it: the folders' own entries - the placeholder root's in its
        parent, the library root, too - and the staging folder's ignore file."""
        staging = self.staging
        made = durable.made_dirs(staging)
        staging.mkdir(parents=True, exist_ok=True)
        ignore = staging / ".ndignore"
        wrote = False
        # Never written through a link left there; one a crash left empty is written again.
        empty = (
            not os.path.islink(ignore) and os.path.isfile(ignore) and os.path.getsize(ignore) == 0
        )
        if empty or not os.path.lexists(ignore):
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                with os.fdopen(
                    os.open(ignore, flags | (0 if empty else os.O_EXCL), 0o644), "w"
                ) as file:
                    file.write("*\n")
                wrote = True
            except FileExistsError:
                pass  # made meanwhile
        if wrote:
            durable.sync_file(ignore)
        if made or wrote:
            durable.sync_dirs(staging, *(folder.parent for folder in made))
