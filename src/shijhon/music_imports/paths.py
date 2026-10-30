"""Visible destination names without merging distinct source folders or files."""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath

from shijhon.music_imports.model import relative_path, scanner_ignored_path
from shijhon.placeholders.layout import safe_name


def _key(path: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).casefold())


class Destinations:
    """One bounded manifest's allocated source/destination prefixes.

    Persisted original destinations also survive overlap reviews which temporarily
    point a source at a retagged copy. The declaration lock serializes allocations.
    """

    def __init__(self) -> None:
        self.mapped: dict[str, str] = {}
        self.taken: dict[str, str] = {}

    def remember(self, source: str, destination: str) -> None:
        sources, destinations = source.split("/"), destination.split("/")
        for n in range(1, len(sources) + 1):
            prefix = "/".join(sources[:n])
            target = "/".join(destinations[:n])
            self.mapped[prefix] = target
            self.taken[_key(target)] = prefix

    def allocate(self, source: str) -> str:
        parts = source.split("/")
        parent = ""
        for n, part in enumerate(parts, 1):
            prefix = "/".join(parts[:n])
            if prefix in self.mapped:
                parent = self.mapped[prefix]
                continue
            folder = n < len(parts)
            ignored = scanner_ignored_path(part + "/song.flac" if folder else part)
            name = safe_name(part.lstrip("."), 255) if ignored else part
            # Scanner-reserved names such as lost+found need a visible alternative too.
            if scanner_ignored_path(name + "/song.flac" if folder else name):
                name = safe_name(name, 254) + "_"
            candidate, count = name, 1
            while (target := "/".join(filter(None, (parent, candidate)))) and _key(
                target
            ) in self.taken:
                count += 1
                suffix = PurePosixPath(name).suffix if not folder else ""
                stem = name[: -len(suffix)] if suffix else name
                tail = f" ({count})"
                candidate = (
                    safe_name(stem, 255 - len(tail.encode()) - len(suffix.encode())) + tail + suffix
                )
            self.mapped[prefix] = target
            self.taken[_key(target)] = prefix
            parent = target
        return relative_path(parent)
