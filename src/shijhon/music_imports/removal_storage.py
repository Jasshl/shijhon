"""Remove only a verified publication link; private original bytes remain retained."""

from __future__ import annotations

import os
from typing import Any

from shijhon.music_imports.model import ImportError
from shijhon.music_imports.storage import Storage


def same_file(storage: Storage, item: dict[str, Any], *, missing_ok: bool = False) -> bool:
    staged = storage.file(item["staging_name"])
    try:
        try:
            final = storage.file(item["final_name"], final=True)
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        try:
            a, b = os.fstat(staged), os.fstat(final)
            if (a.st_dev, a.st_ino, a.st_size) != (b.st_dev, b.st_ino, item["expected_size"]):
                raise ImportError("changed", "The published import changed. Check storage.")
        finally:
            os.close(final)
    finally:
        os.close(staged)
    return True


def verified(storage: Storage, item: dict[str, Any]) -> tuple[int, ...]:
    descriptor = storage.file(item["staging_name"])
    try:
        before = signature(os.fstat(descriptor))
        storage.verify(item["staging_name"], item["expected_size"], item["digest"])
        if signature(os.fstat(descriptor)) != before:
            raise ImportError("changed", "The retained original changed during verification.")
        return before
    finally:
        os.close(descriptor)


def signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def unchanged(storage: Storage, item: dict[str, Any], receipt: tuple[int, ...]) -> None:
    descriptor = storage.file(item["staging_name"])
    try:
        if signature(os.fstat(descriptor)) != receipt:
            raise ImportError("changed", "The retained original changed before the file action.")
    finally:
        os.close(descriptor)


def quarantine(storage: Storage, item: dict[str, Any], receipt: tuple[int, ...]) -> None:
    unchanged(storage, item, receipt)
    with storage.final_parent(item["final_name"]) as (fd, leaf):
        if not same_file(storage, item, missing_ok=True):
            return  # Durable intent may have survived the unlink.
        source = storage.file(item["staging_name"])
        try:
            expected = os.fstat(source)
            current = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise ImportError("changed", "The published import changed. Check storage.")
            os.unlink(leaf, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(source)


def restore(storage: Storage, item: dict[str, Any], receipt: tuple[int, ...]) -> None:
    unchanged(storage, item, receipt)
    if not same_file(storage, item, missing_ok=True):
        storage.place(item["staging_name"], item["final_name"])
