# ruff: noqa: S608 -- Fixed insert column names; all caller values are bound.
"""Trusted, positive account attribution independent of profile and native lifetimes."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import replace

import aiosqlite

from shijhon.catalog.model import (
    CATALOG_KEY,
    ITEM_ID,
    CatalogRef,
    CatalogTrack,
    track_data,
    track_from_data,
)
from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.index import COLUMNS
from shijhon.store import Store
from shijhon.views.ids import album_id, artist_id


async def record_positive(db: aiosqlite.Connection, owner: str, library: str, key: str) -> None:
    """Join the membership worker transaction; only call for a successful Save. A choice
    on the Collection page, not activity: it stays a choice of the library it was made in
    (``activity`` 0) and brings in no album by itself."""
    await db.execute(
        "INSERT INTO collection_history(owner_id,library_id,track_key,added_at)"
        " VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
        [owner, library, key, time.time()],
    )
    await db.execute(
        "UPDATE collection_history AS h SET catalog_data=COALESCE(("
        "SELECT json_remove(j.value,'$.artwork_template') FROM collection_tracks t"
        " JOIN releases r ON r.ref=t.release_key JOIN json_each(r.data,'$.tracks') j"
        " JOIN collection_track_aliases a ON a.library_id=t.library_id"
        " AND a.track_key=t.track_key"
        " AND a.alias='catalog:' || json_extract(j.value,'$.ref.catalog') || ':' ||"
        " json_extract(j.value,'$.ref.id')"
        " WHERE t.library_id=h.library_id AND t.track_key=h.track_key LIMIT 1),'{}')"
        " WHERE h.owner_id=? AND h.library_id=? AND h.track_key=? AND h.catalog_data='{}'",
        [owner, library, key],
    )


# What an accepted action names, as its parameter: a song, an album, or either.
PENDING_PARAMS = ("id", "albumId", "songId")
# An action's parameter as the queue keeps it: a song, an album, or either.
_AS_PENDING = {
    "id": "id",
    "albumId": "albumId",
    "songId": "songId",
    "songIdToAdd": "songId",
    "mediaId": "songId",
}
MAX_PENDING = 10_000  # items one app or website action may queue


async def queue_pending(
    db: aiosqlite.Connection,
    owner: str,
    library: str,
    items: Sequence[tuple[str, str]],
    *,
    limit: int | None = MAX_PENDING,
) -> int:
    """Keep an accepted action's songs or albums to record as the account's activity
    (``collection_pending``), in the caller's transaction: the action is durable with
    whatever accepted it. ``CollectionRequests.drain`` records them. ``items`` name them
    by the action's own parameters (``songIdToAdd`` and ``mediaId`` name songs); ``limit``
    bounds one action (None: every item, as a favorites read keeps them). Keeping an item
    already kept makes it due again. The account's identity must have been verified for
    the action; nothing is fetched here."""
    now = time.time()
    rows = [
        (owner, library, _AS_PENDING[param], item, now)
        for param, item in dict.fromkeys(items)
        if param in _AS_PENDING and 0 < len(item) <= 300
    ]
    if limit is not None:
        rows = rows[:limit]
    await db.executemany(
        "INSERT INTO collection_pending(owner_id,library_id,param,item,queued_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(owner_id,library_id,param,item)"
        " DO UPDATE SET next_at=0",
        rows,
    )
    return len(rows)


def recordable(track: CatalogTrack) -> bool:
    """Whether ``History.record_catalog`` accepts this recording's metadata."""
    return bool(
        CATALOG_KEY.fullmatch(track.ref.catalog)
        and ITEM_ID.fullmatch(track.ref.id)
        and track.title
        and len(track.title) <= 1000
        and len(track.artist) <= 1000
        and len(track.album_title or "") <= 1000
        and not isinstance(track.duration_ms, bool)
        and 0 < track.duration_ms <= 1e12
    )


class History:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _grant(actor: Actor, library_id: str, count: int) -> None:
        if not actor.owner_id or not library_id or library_id not in actor.library_ids:
            raise CollectionError("Your access to this music library could not be verified.")
        if not 0 <= count <= 500:
            raise CollectionError("Record at most 500 collection tracks at once.")

    async def record_catalog(
        self,
        actor: Actor,
        library_id: str,
        tracks: Sequence[CatalogTrack],
        *,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        """Record an already verified, accepted explicit request/addition: the account's
        activity (an app's action, a publication for it, audio it prepared), which brings
        the songs' albums into its Shijhon library.

        Actor MUST come from the current authenticated request; never accept browser
        owner assertions or call this from search, browsing, warm-ahead or listen
        imports. No saved dashboard credential, scan, publication or membership is
        needed. An optional db joins the publisher's transaction and rollback.
        """
        self._grant(actor, library_id, len(tracks))
        if db is not None and not db.in_transaction:
            raise CollectionError("Collection attribution requires the publisher transaction.")
        snapshots = []
        for track in tracks:
            if not recordable(track):
                raise CollectionError("The collection recording metadata is invalid.")
            data = json.dumps(
                track_data(replace(track, artwork_template=None)),
                allow_nan=False,
                separators=(",", ":"),
            )
            if len(data.encode()) > 32768:
                raise CollectionError("The collection recording metadata is too large.")
            snapshots.append((track, data))
        if db is None:
            async with self.store.transaction() as conn:
                await self._record(conn, actor, library_id, snapshots)
        else:
            await self._record(db, actor, library_id, snapshots)

    async def _record(
        self,
        db: aiosqlite.Connection,
        actor: Actor,
        library: str,
        snapshots: Sequence[tuple[CatalogTrack, str]],
    ) -> None:
        for track, data in snapshots:
            alias = "catalog:" + str(track.ref)
            cursor = await db.execute(
                "SELECT track_key FROM collection_track_aliases WHERE library_id=? AND alias=?",
                [library, alias],
            )
            known = await cursor.fetchone()
            if not known:
                # A verified release can link a catalog recording to an owned
                # native file. Reuse its structural identity, without assigning
                # original-file provenance or fetching as another account.
                cursor = await db.execute(
                    "SELECT t.track_key FROM track_links l JOIN collection_tracks t"
                    " ON t.song_id=l.song_id WHERE l.track_ref=? AND t.library_id=?"
                    " AND t.present=1",
                    [str(track.ref), library],
                )
                known = await cursor.fetchone()
            key = str(known["track_key"]) if known else alias
            now = time.time()
            title, album, artist = track.title, track.album_title or "", track.artist
            # A metadata-only absent fallback keeps accepted history browseable even
            # before the caller's next structural scan. Never replace native facts.
            await db.execute(
                f"INSERT OR IGNORE INTO collection_tracks({COLUMNS})"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    library,
                    key,
                    "",
                    str(track.album) if track.album else "",
                    album_id(track.album) if track.album else "name:" + album,
                    artist_id(track.artist_refs[0]) if track.artist_refs else "name:" + artist,
                    title,
                    album,
                    artist,
                    " ".join((title, album, artist)).casefold(),
                    track.duration_ms / 1000,
                    0,
                    0,
                    "{}",
                    0,
                    0,
                    "",
                    now,
                ],
            )
            await db.execute(
                "INSERT OR IGNORE INTO collection_track_aliases VALUES (?,?,?)",
                [library, alias, key],
            )
            # The account's activity: its album joins the account's Shijhon library.
            await db.execute(
                "INSERT INTO collection_history(owner_id,library_id,track_key,added_at,"
                "catalog_data,activity) VALUES (?,?,?,?,?,1)"
                " ON CONFLICT(owner_id,library_id,track_key) DO UPDATE SET activity=1,"
                "catalog_data=CASE WHEN catalog_data='{}' THEN excluded.catalog_data"
                " ELSE catalog_data END",
                [actor.owner_id, library, key, now, data],
            )

    async def snapshots(
        self, actor: Actor, library_id: str, refs: Sequence[CatalogRef]
    ) -> dict[str, CatalogTrack]:
        """Bounded immutable first-accepted recording data for the trusted restorer."""
        self._grant(actor, library_id, len(refs))
        rows = await self.store.fetchall(
            "SELECT a.alias,h.catalog_data FROM collection_history h"
            " JOIN collection_track_aliases a ON a.library_id=h.library_id"
            " AND a.track_key=h.track_key WHERE h.owner_id=? AND h.library_id=?"
            " AND a.alias IN (SELECT value FROM json_each(?)) AND h.catalog_data<>'{}'",
            [actor.owner_id, library_id, json.dumps(["catalog:" + str(ref) for ref in refs])],
        )
        result = {}
        for row in rows:
            track = track_from_data(json.loads(row["catalog_data"]))
            if "catalog:" + str(track.ref) != row["alias"]:
                raise CollectionError("The collection recording identity is inconsistent.")
            result[str(track.ref)] = track
        return result
