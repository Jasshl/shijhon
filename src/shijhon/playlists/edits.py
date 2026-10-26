"""The owner's edits of a playlist Shijhon refreshes. Before a refresh, the native playlist
is compared with the last publication: songs the owner removed are remembered for that
playlist and left out of its later refreshes, songs the owner added are remembered as
examples of what they want, and the AI sees both. Edits belong to one playlist and feed
only its refreshes."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

from shijhon.playlists.model import Connection, PlaylistError, Track
from shijhon.playlists.references import entries
from shijhon.playlists.resolve import native_track
from shijhon.store import Store

if TYPE_CHECKING:
    from shijhon.playlists.service import Playlists

KEPT = 200  # songs remembered per playlist, of each kind
SHOWN = 100  # of those, the latest shown to the AI
NOTE = (
    " user_edits lists songs the user removed from earlier versions of this playlist, "
    "which are not candidates, and songs the user added to it. When the removed songs "
    "share a clear pattern, avoid similar choices; treat the added songs as examples of "
    "what the user wants here. The instructions still come first."
)
EDITED = (
    "This playlist was edited outside Shijhon. Pause the mix and use a new mix"
    " to preserve those edits."
)
RENAMED = (
    "This playlist was renamed outside Shijhon. Give it its earlier name to refresh it,"
    " or pause its refreshes to keep the new name."
)


def music(track: Track) -> dict[str, str]:
    return {"artist": track.artist, "title": track.title, "album": track.album}


@dataclass
class Edits:
    """A playlist's remembered edits, latest first, with those learned for this run."""

    removed: list[Track] = field(default_factory=list)
    added: list[Track] = field(default_factory=list)
    learned_removed: list[Track] = field(default_factory=list)
    learned_added: list[Track] = field(default_factory=list)
    ids: set[str] = field(init=False)
    keys: set[str] = field(init=False)
    left: set[str] = field(init=False, default_factory=set)  # candidates left out

    def __post_init__(self) -> None:
        self.ids = {t.id for t in self.removed}
        self.keys = {t.key for t in self.removed}

    def leaves_out(self, key: str, ident: str = "") -> bool:
        """A song the owner removed, by its music key or Navidrome song ID."""
        if key in self.keys or ident in self.ids:
            self.left.add(key)
            return True
        return False

    def shown(self) -> dict[str, Any]:
        """What the AI sees; nothing without edits."""
        if not self.removed and not self.added:
            return {}
        return {
            "removed": [music(t) for t in self.removed[:SHOWN]],
            "added": [music(t) for t in self.added[:SHOWN]],
        }

    def summary(self) -> dict[str, Any]:
        """For the run: what this refresh learned, how much is remembered and how many
        candidates were left out."""
        return {
            "learned": {
                "removed": [{"id": t.id, **music(t)} for t in self.learned_removed],
                "added": [{"id": t.id, **music(t)} for t in self.learned_added],
            },
            "remembered": {"removed": len(self.removed), "added": len(self.added)},
            "left_out": len(self.left),
        }


async def remembered(store: Store, mix: str) -> Edits:
    rows = await store.fetchall(
        "SELECT song_id,kind,artist,title,album FROM playlist_edits WHERE mix=?"
        " ORDER BY at DESC, rowid DESC",
        [mix],
    )
    found: dict[str, list[Track]] = {"removed": [], "added": []}
    for row in rows:
        found[row["kind"]].append(
            Track(id=row["song_id"], artist=row["artist"], title=row["title"], album=row["album"])
        )
    return Edits(found["removed"], found["added"])


async def learn(
    service: Playlists,
    connection: Connection,
    mix: dict[str, Any],
    playlist: dict[str, Any],
) -> tuple[list[Track], list[Track]]:
    """Compare the native playlist with the last publication; remember and return the
    songs the owner removed and added. A comment or visibility the owner set stays, as
    the replacement keeps them; a new name stops the refresh, which would undo it."""
    row = await service.repo.store.fetchone(
        "SELECT id,data FROM playlist_runs WHERE mix=? AND state='published'"
        " ORDER BY created DESC LIMIT 1",
        [mix["id"]],
    )
    if row is None:
        raise PlaylistError(EDITED)
    data = json.loads(row["data"])
    if playlist.get("name") != data["definition"]["name"]:
        raise PlaylistError(RENAMED)
    published = list(dict.fromkeys(str(i) for i in data.get("native_ids", [])))
    listed = playlist.get("entry")
    listed = listed if isinstance(listed, list) else []
    present = {str(e.get("id")) for e in listed if isinstance(e, dict)}
    removed = await in_library(service, connection, [i for i in published if i not in present])
    known = set(published)
    added = list({t.id: t for t in entries(listed) if t.id not in known}.values())[-KEPT:]
    await remember(service.repo.store, mix["id"], row["id"], removed, added)
    return removed, added


async def in_library(
    service: Playlists, connection: Connection, idents: Sequence[str]
) -> list[Track]:
    """The songs still in the library, as Navidrome describes them now: a song that left
    the library was not removed by the owner."""
    found: dict[str, Track] = {}
    limiter = anyio.CapacityLimiter(4)

    async def one(ident: str) -> None:
        async with limiter:
            try:
                reply = await service.client.call(connection, "getSong", [("id", ident)])
                track = native_track(reply["song"])
            except (PlaylistError, KeyError, TypeError, ValueError):
                return  # also an untagged file, which names no artist or title
            if track.id == ident:
                found[ident] = track

    async with anyio.create_task_group() as group:
        for ident in idents:
            group.start_soon(one, ident)
    return [found[i] for i in idents if i in found]


async def remember(
    store: Store, mix: str, publication: str, removed: list[Track], added: list[Track]
) -> None:
    now = time.time()
    async with store.transaction() as db:
        # A refresh learning again from the same publication (a retry) replaces what it
        # learned before: an edit the owner undid in between is not kept.
        await db.execute(
            "DELETE FROM playlist_edits WHERE mix=? AND publication=?", [mix, publication]
        )
        await db.executemany(
            "INSERT INTO playlist_edits"
            "(mix,song_key,song_id,kind,artist,title,album,publication,at)"
            " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(mix,song_key) DO UPDATE SET"
            " song_id=excluded.song_id,kind=excluded.kind,artist=excluded.artist,"
            "title=excluded.title,album=excluded.album,publication=excluded.publication,"
            "at=excluded.at",
            [
                (mix, t.key, t.id, kind, t.artist, t.title, t.album, publication, now)
                for kind, songs in (("removed", removed), ("added", added))
                for t in songs
            ],
        )
        for kind in ("removed", "added"):
            await db.execute(
                "DELETE FROM playlist_edits WHERE mix=? AND kind=? AND rowid NOT IN"
                " (SELECT rowid FROM playlist_edits WHERE mix=? AND kind=?"
                " ORDER BY at DESC, rowid DESC LIMIT ?)",
                [mix, kind, mix, kind, KEPT],
            )


async def forget(store: Store, mix: str) -> int:
    """Forget every edit remembered for one playlist; returns how many songs that was.
    Changes still in the native playlist since its last publication are learned again at
    the next refresh."""
    async with store.transaction() as db:
        cursor = await db.execute("DELETE FROM playlist_edits WHERE mix=?", [mix])
        return cursor.rowcount
