"""Rebuild Shijhon's placeholder records from the files themselves.

Every placeholder (and delivered audio written in its place) carries marker tags: the
catalog track and release it stands for, and whether it is the silent file. If the
database is lost, walking the placeholder folder and asking Navidrome for each file's
song ID restores the releases, placeholders and their links. Links to *owned* songs are
not in any file; matching recreates them. Files of new placeholders still pending
(written but not recorded) are not recovered: they are taken out again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import anyio.to_thread

from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import PlaceholderEngine

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".flac", ".m4a", ".mp4", ".mp3", ".ogg", ".opus"}


@dataclass
class RecoverReport:
    releases: int = 0
    placeholders: int = 0
    skipped: list[str] = field(default_factory=list)  # relative paths without a song


def _scan_files(root: Path) -> list[tuple[Path, tagging.Marker]]:
    found = []
    for path in sorted(root.rglob("*")):
        if ".staging" in path.parts or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        marker = tagging.marker_of(path)
        if marker is not None:
            found.append((path, marker))
    return found


async def recover(engine: PlaceholderEngine) -> RecoverReport:
    layout, store = engine.layout, engine.store
    report = RecoverReport()
    files = await anyio.to_thread.run_sync(_scan_files, layout.root)
    folders: dict[str, dict[str, dict[str, object]]] = {}
    for path, _ in files:
        folder = layout.relative(path.parent)
        if folder not in folders:
            songs = await engine.navidrome.songs_under(folder)
            folders[folder] = {str(s["path"]): s for s in songs if not s.get("missing")}

    known_releases = {r["ref"] for r in await store.fetchall("SELECT ref FROM releases")}
    known_songs = {r["song_id"] for r in await store.fetchall("SELECT song_id FROM placeholders")}
    pending = {
        path
        for row in await store.fetchall("SELECT paths FROM pending_placeholders")
        for path in json.loads(row["paths"])
    }
    now = engine.clock()
    async with store.transaction() as conn:
        for path, marker in files:
            relative = layout.relative(path)
            song = folders[layout.relative(path.parent)].get(relative)
            if song is None or relative in pending:
                report.skipped.append(relative)
                continue
            raw = marker.raw
            if marker.release not in known_releases:
                album_tags = {
                    name: values
                    for name, aliases in tagging.ALBUM_TAGS.items()
                    if (values := tagging.mapped(raw, aliases))
                }
                await conn.execute(
                    "INSERT INTO releases (ref, folder, album_id, owned_album_id, title, artist,"
                    " album_tags, data, created_at) VALUES (?, ?, ?, NULL, ?, ?, ?, '{}', ?)",
                    [
                        marker.release,
                        layout.relative(path.parent),
                        song["albumId"],
                        (raw.get("album") or [""])[0],
                        (raw.get("albumartist") or raw.get("artist") or [""])[0],
                        json.dumps(album_tags),
                        now,
                    ],
                )
                known_releases.add(marker.release)
                report.releases += 1
            song_id = str(song["id"])
            await conn.execute(
                "INSERT OR IGNORE INTO track_links (track_ref, song_id, release_ref, owned)"
                " VALUES (?, ?, ?, 0)",
                [marker.track, song_id, marker.release],
            )
            if song_id in known_songs:
                continue
            placeholder_path = (
                relative if marker.silence else str(Path(relative).with_suffix(".flac"))
            )
            if path.suffix.lower() == ".flac":
                comments = await anyio.to_thread.run_sync(tagging.read_vorbis, path)
                comments[tagging.MARKER_SILENCE] = ["1"]
            else:
                comments = tagging.placeholder_comments_from(raw, song)
            await conn.execute(
                "INSERT INTO placeholders (song_id, path, placeholder_path, track_ref,"
                " release_ref, isrc, title, artist, album, duration_ms, disc, track, tags,"
                " state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    song_id,
                    relative,
                    placeholder_path,
                    marker.track,
                    marker.release,
                    next(iter(raw.get("isrc") or []), None),
                    song.get("title", ""),
                    song.get("artist", ""),
                    song.get("album", ""),
                    int(float(song.get("duration", 0)) * 1000),  # type: ignore[arg-type]
                    int(song.get("discNumber") or 1),  # type: ignore[call-overload]
                    int(song.get("trackNumber") or 0),  # type: ignore[call-overload]
                    json.dumps(comments),
                    "placeholder" if marker.silence else "delivered",
                    now,
                ],
            )
            known_songs.add(song_id)
            report.placeholders += 1
    return report
