"""Owner-scoped durable snapshots and bounded progress reads."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from shijhon.downloads.model import MAX_BATCHES, PreparationError
from shijhon.store import Store

SHOWN_ITEMS = 50  # songs of a batch the Downloads page lists


class Repository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def owner(self, ident: str) -> dict[str, Any] | None:
        row = await self.store.fetchone("SELECT * FROM audio_owners WHERE id=?", [ident])
        return dict(row) if row else None

    async def batch(self, ident: str, owner: str) -> dict[str, Any]:
        row = await self.store.fetchone(
            "SELECT * FROM audio_batches WHERE id=? AND owner=?", [ident, owner]
        )
        if row is None:
            raise PreparationError("That preparation batch is unavailable.")
        return dict(row)

    async def state(self, ident: str, owner: str) -> str:
        """A batch's state alone (without its snapshot, read for each song's stages)."""
        row = await self.store.fetchone(
            "SELECT state FROM audio_batches WHERE id=? AND owner=?", [ident, owner]
        )
        if row is None:
            raise PreparationError("That preparation batch is unavailable.")
        return str(row["state"])

    async def batches(self, owner: str, shown: int = SHOWN_ITEMS) -> list[dict[str, Any]]:
        """The owner's recent batches with their totals and first ``shown`` songs (a
        batch can hold as many songs as a playlist import; its snapshot is not read)."""
        rows = await self.store.fetchall(
            "SELECT id,owner,generation,library_id,playlist_id,title,created,state,error,"
            "creator FROM audio_batches WHERE owner=? ORDER BY created DESC LIMIT 20",
            [owner],
        )
        found = []
        for row in rows:
            batch = dict(row)
            totals = await self.store.fetchone(
                "SELECT COUNT(*) AS n,COALESCE(SUM(entries),0) AS total,"
                "COALESCE(SUM(CASE WHEN state='available' THEN entries ELSE 0 END),0)"
                " AS available,"
                "COALESCE(SUM(CASE WHEN state='failed' THEN entries ELSE 0 END),0) AS failed"
                " FROM audio_items WHERE batch=?",
                [batch["id"]],
            )
            assert totals is not None
            items = await self.store.fetchall(
                "SELECT * FROM audio_items WHERE batch=? ORDER BY position LIMIT ?",
                [batch["id"], shown],
            )
            batch["items"] = [dict(item) for item in items]
            batch["total"] = totals["total"]
            batch["unique"] = totals["n"]
            batch["available"] = totals["available"]
            batch["failed"] = totals["failed"]
            found.append(batch)
        return found

    async def enqueue(
        self,
        owner: dict[str, Any],
        playlist: dict[str, Any],
        items: list[dict[str, Any]],
        snapshot: list[str],
        *,
        creator: str | None = None,
        idempotency_key: str = "",
        also: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
    ) -> str:
        """``also`` joins the batch's transaction (its attribution, kept to record)."""
        ident = secrets.token_hex(12)
        async with self.store.transaction() as db:
            async with db.execute(
                "SELECT id FROM audio_batches WHERE owner=? AND playlist_id=?"
                " AND state IN ('queued','running','cancelling')",
                [owner["id"], playlist["id"]],
            ) as cursor:
                if await cursor.fetchone():
                    raise PreparationError("This playlist already has an active preparation batch.")
            async with db.execute(
                "SELECT COUNT(*) AS n FROM audio_batches WHERE owner=?", [owner["id"]]
            ) as cursor:
                row = await cursor.fetchone()
            if row and row["n"] >= MAX_BATCHES:
                raise PreparationError("Remove a finished batch before starting another.")
            await db.execute(
                "INSERT INTO audio_batches"
                "(id,owner,generation,library_id,playlist_id,title,snapshot,created,state,creator)"
                " VALUES (?,?,?,?,?,?,?,?,'queued',?)",
                [
                    ident,
                    owner["id"],
                    owner["generation"],
                    owner["library_id"],
                    playlist["id"],
                    str(playlist.get("name") or "Playlist")[:300],
                    json.dumps(snapshot),
                    time.time(),
                    creator,
                ],
            )
            await db.executemany(
                "INSERT INTO audio_items"
                "(batch,song_id,position,entries,title,artist,track_ref)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    [
                        ident,
                        item["id"],
                        item["position"],
                        item["entries"],
                        item["title"],
                        item["artist"],
                        item["track_ref"],
                    ]
                    for item in items
                ],
            )
            if creator:
                await db.execute(
                    "INSERT INTO audio_agent_requests"
                    "(creator,owner,library_id,idempotency_key,playlist_id,batch)"
                    " VALUES (?,?,?,?,?,?)",
                    [
                        creator,
                        owner["id"],
                        owner["library_id"],
                        idempotency_key,
                        playlist["id"],
                        ident,
                    ],
                )
            if also is not None:
                await also(db)
        return ident

    async def replay(self, creator: str, key: str) -> dict[str, Any] | None:
        row = await self.store.fetchone(
            "SELECT * FROM audio_agent_requests WHERE creator=? AND idempotency_key=?",
            [creator, key],
        )
        return dict(row) if row else None

    async def agent_status(
        self, owner: str, creator: str, ident: str, offset: int = 0, limit: int = 30
    ) -> dict[str, Any]:
        batch = await self.batch(ident, owner)
        if batch["creator"] != creator:
            raise PreparationError("That preparation batch is unavailable.")
        totals = await self.store.fetchone(
            "SELECT COUNT(*) AS unique_songs,COALESCE(SUM(entries),0) AS total,"
            "COALESCE(SUM(CASE WHEN state='available' THEN entries ELSE 0 END),0) AS available,"
            "COALESCE(SUM(CASE WHEN state='failed' THEN entries ELSE 0 END),0) AS failed"
            " FROM audio_items WHERE batch=?",
            [ident],
        )
        rows = await self.store.fetchall(
            "SELECT song_id,position,entries,title,artist,state,error FROM audio_items"
            " WHERE batch=? ORDER BY position LIMIT ? OFFSET ?",
            [ident, limit, offset],
        )
        assert totals is not None
        return {
            **{
                key: batch[key]
                for key in ("id", "playlist_id", "title", "state", "error", "created")
            },
            **dict(totals),
            "items": [dict(row) for row in rows],
            "offset": offset,
            "has_more": offset + len(rows) < totals["unique_songs"],
            "location": "server",
        }

    async def progress(self, batch: str, song: str, state: str, error: str = "") -> None:
        await self.store.execute(
            "UPDATE audio_items SET state=?,error=? WHERE batch=? AND song_id=?",
            [state, error, batch, song],
        )

    async def finish(self, ident: str) -> None:
        """A batch changes terminal state only after every active song has finished."""
        async with self.store.transaction() as db:
            async with db.execute("SELECT state FROM audio_batches WHERE id=?", [ident]) as cursor:
                batch = await cursor.fetchone()
            if batch is None:
                return
            # Which states its songs have (each found by index, however many songs).
            async with db.execute(
                "SELECT"
                " EXISTS(SELECT 1 FROM audio_items WHERE batch=?1 AND state IN"
                " ('waiting','preparing','fetching','verifying')) AS active,"
                " EXISTS(SELECT 1 FROM audio_items WHERE batch=?1 AND state='queued') AS queued,"
                " EXISTS(SELECT 1 FROM audio_items WHERE batch=?1 AND state='paused') AS paused,"
                " EXISTS(SELECT 1 FROM audio_items WHERE batch=?1 AND state='failed') AS failed",
                [ident],
            ) as cursor:
                found = await cursor.fetchone()
            states = dict(found) if found is not None else {}
            if states.get("active"):
                return
            if batch["state"] == "cancelling":
                state = "cancelled"
            elif states.get("queued"):
                return
            elif states.get("paused"):
                state = "paused"
            elif states.get("failed"):
                state = "finished"
            else:
                state = "complete"
            await db.execute(
                "UPDATE audio_batches SET state=?,"
                "error=CASE WHEN ?='complete' THEN '' ELSE error END"
                " WHERE id=?",
                [state, state, ident],
            )

    async def pause(self, ident: str, error: str = "") -> None:
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE audio_batches SET state='paused',error=? WHERE id=?", [error, ident]
            )
            await db.execute(
                "UPDATE audio_items SET state='paused' WHERE batch=? AND state='queued'", [ident]
            )
