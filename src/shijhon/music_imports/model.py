"""Bounded, credential-free contracts for importing retained original audio."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields


class ImportError(ValueError):
    """A safe public refusal; ``code`` never contains paths or upstream details."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ManifestEntry:
    client_key: str
    path: str
    size: int
    sha256: str = ""


@dataclass(frozen=True)
class NativeFile:
    """Result of an exact caller-native lookup, with a library-relative path."""

    song_id: str
    path: str
    size: int


@dataclass(frozen=True)
class ImportLimits:
    max_file_bytes: int = 2 * 1024**3
    max_job_bytes: int = 512 * 1024**3
    max_job_files: int = 100_000
    max_owner_jobs: int = 4
    max_owner_bytes: int = 512 * 1024**3
    max_global_bytes: int = 1024**4
    min_free_bytes: int = 5 * 1024**3
    max_uploads: int = 2
    upload_seconds: int = 1800
    validation_seconds: int = 15

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if (
                type(value) is not int
                or value < (0 if field.name == "min_free_bytes" else 1)
                or value > 2**63 - 1
            ):
                raise ValueError("Import limits must be positive whole numbers.")
        if self.max_job_files > 1_000_000 or self.max_uploads > 32:
            raise ValueError("Import concurrency and manifest limits are too large.")


FORMATS = (
    "mp3",
    "mp2",
    "flac",
    "aac",
    "m4a",
    "m4b",
    "alac",
    "ogg",
    "oga",
    "opus",
    "wav",
    "aif",
    "aiff",
    "aifc",
    "spx",
    "wma",
    "wv",
    "wvp",
    "ape",
    "mpc",
    "tta",
    "shn",
    "dsf",
    "mka",
)
NATIVE_REJECTED = {
    "tak": (
        "The stock Navidrome scanner cannot read TAK files. No original was uploaded or published."
    )
}
MANIFEST_PAGE_SIZE = 500


def scanner_ignored_path(path: str) -> bool:
    """Paths the pinned scanner always ignores, regardless of its dot-folder setting."""
    parts = path.split("/")
    ignored_folders = {
        "$recycle.bin",
        "#snapshot",
        "@recycle",
        "@eadir",
        "@recently-snapshot",
        ".git",
        ".streams",
        "lost+found",
    }
    return (parts[-1].startswith(".") and not parts[-1].startswith("..")) or any(
        part.casefold() in ignored_folders for part in parts[:-1]
    )


def relative_path(value: str) -> str:
    """Validate a preserved UTF-8 source tree; never sanitize or rename it."""
    try:
        encoded = value.encode() if isinstance(value, str) else b""
    except UnicodeError:
        raise ImportError("invalid_path", "Choose valid UTF-8 relative file paths.") from None
    if (
        not isinstance(value, str)
        or not value
        or len(encoded) > 2048
        or "\\" in value
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        or len(value.split("/")) > 32
        or any(part in ("", ".", "..") or len(part.encode()) > 255 for part in value.split("/"))
        or re.match(r"(?i)^(?:[a-z]:|https?:|file:|ftp:|smb:)", value)
    ):
        raise ImportError("invalid_path", "Choose audio files with safe relative paths.")
    return value
