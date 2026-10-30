"""Private durable state. Every dashboard lookup includes its connected owner."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any

import anyio
from pydantic import SecretStr

from shijhon.playlists.model import AIModels, AIPreferences, Connection, Definition, PlaylistError
from shijhon.store import Store

AI_FIELDS = ("backend", "endpoint", "model", "api_key")
HOST_AI_SECTION = "playlist-ai"
AI_PREFERENCES_SECTION = "playlist-ai-preferences"


class Repository:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def connection(self, owner_id: str) -> Connection | None:
        row = await self.store.fetchone(
            "SELECT connection FROM playlist_owners WHERE id=?", [owner_id]
        )
        if not row:
            return None
        connection = Connection.model_validate_json(row["connection"])
        if connection.use_shared_ai:
            config = await self.shared_ai()
            if config:
                connection = Connection.model_validate(
                    {**connection.model_dump(), **{k: config[k] for k in AI_FIELDS}}
                )
        return connection

    async def shared_ai(self) -> dict[str, Any]:
        """Internal host configuration only; never expose this dictionary to a client."""
        row = await self.store.fetchone(
            "SELECT value FROM saved_settings WHERE section=? AND key='connection'",
            [HOST_AI_SECTION],
        )
        return json.loads(row["value"]) if row else {}

    async def save_shared_ai(
        self, connection: Connection, *, allowed_models: list[str] | None = None
    ) -> None:
        # Authorization belongs to Playlists.save_connection, before this storage call.
        values = json.loads(connection.stored())
        config = {key: values[key] for key in AI_FIELDS}
        config.update(AIModels(allowed_models=allowed_models or []).model_dump())
        await self.store.execute(
            "INSERT INTO saved_settings(section,key,value,saved_at,saved_by) VALUES (?,?,?,?,?)"
            " ON CONFLICT(section,key) DO UPDATE SET value=excluded.value,"
            " saved_at=excluded.saved_at,saved_by=excluded.saved_by",
            [HOST_AI_SECTION, "connection", json.dumps(config), time.time(), connection.user_id],
        )

    async def ai_preferences(self, owner: str) -> AIPreferences:
        row = await self.store.fetchone(
            "SELECT value FROM saved_settings WHERE section=? AND key=?",
            [AI_PREFERENCES_SECTION, owner],
        )
        return AIPreferences.model_validate_json(row["value"]) if row else AIPreferences()

    async def save_ai_preferences(self, owner: str, preferences: AIPreferences) -> None:
        await self.store.execute(
            "INSERT INTO saved_settings(section,key,value,saved_at,saved_by) VALUES (?,?,?,?,?)"
            " ON CONFLICT(section,key) DO UPDATE SET value=excluded.value,"
            " saved_at=excluded.saved_at,saved_by=excluded.saved_by",
            [AI_PREFERENCES_SECTION, owner, preferences.model_dump_json(), time.time(), owner],
        )

    async def disconnect(self, owner: str) -> None:
        async with self.store.transaction() as db:
            await db.execute(
                "DELETE FROM saved_settings WHERE section=? AND key=?",
                [AI_PREFERENCES_SECTION, owner],
            )
            await db.execute("DELETE FROM playlist_owners WHERE id=?", [owner])

    async def connect(self, connection: Connection) -> None:
        old = await self.connection(connection.user_id)
        if connection.use_shared_ai:
            # Keep a reference, not copies of host credentials in personal profiles.
            connection = connection.model_copy(
                update={"backend": "none", "endpoint": "", "model": "", "api_key": SecretStr("")}
            )
        async with self.store.transaction() as db:
            if old and old.history_user != connection.history_user:
                cursor = await db.execute(
                    "SELECT id FROM playlist_runs WHERE mix IN"
                    " (SELECT id FROM playlist_mixes WHERE owner=?)"
                    " AND state IN ('publishing','uncertain')",
                    [old.user_id],
                )
                if await cursor.fetchone():
                    raise PlaylistError(
                        "Recheck the current publication before changing history profiles."
                    )
                await db.execute("DELETE FROM playlist_listens WHERE owner=?", [old.user_id])
                await db.execute(
                    "UPDATE playlist_owners SET history_until=0, history_from=0 WHERE id=?",
                    [old.user_id],
                )
                await db.execute("UPDATE playlist_mixes SET enabled=0 WHERE owner=?", [old.user_id])
                await db.execute(
                    "UPDATE playlist_runs SET state='discarded' WHERE mix IN"
                    " (SELECT id FROM playlist_mixes WHERE owner=?) AND state!='published'",
                    [old.user_id],
                )
            cursor = await db.execute(
                "SELECT connection,generation FROM playlist_owners WHERE id=?", [connection.user_id]
            )
            saved = await cursor.fetchone()
            generation = (
                saved["generation"]
                if saved
                and connection.same_settings(Connection.model_validate_json(saved["connection"]))
                else secrets.token_hex(16)
            )
            await db.execute(
                "INSERT INTO playlist_owners(id,username,connection,generation)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET username=excluded.username,"
                " connection=excluded.connection,generation=excluded.generation",
                [
                    connection.user_id,
                    connection.username,
                    connection.stored(),
                    generation,
                ],
            )

    async def mixes(self, owner: str) -> list[dict[str, Any]]:
        rows = await self.store.fetchall(
            "SELECT * FROM playlist_mixes WHERE owner=? ORDER BY rowid DESC", [owner]
        )
        return [dict(row) for row in rows]

    async def mix(self, ident: str, owner: str) -> dict[str, Any]:
        row = await self.store.fetchone(
            "SELECT * FROM playlist_mixes WHERE id=? AND owner=?", [ident, owner]
        )
        if not row:
            raise PlaylistError("This mix is unavailable for this account.")
        return dict(row)

    async def create(self, owner: str, definition: Definition) -> str:
        ident = secrets.token_hex(12)
        await self.store.execute(
            "INSERT INTO playlist_mixes(id,owner,definition) VALUES (?,?,?)",
            [ident, owner, definition.model_dump_json()],
        )
        return ident

    async def create_discovery(
        self, owner: str, key: str, definition: Definition, *, enable_after: bool = False
    ) -> str:
        """Save one creation and its first run atomically; retries reuse that creation."""
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise PlaylistError("Reload the discovery mix form before submitting.")
        original = json.dumps([definition.model_dump(), enable_after], sort_keys=True)
        input_hash = hashlib.sha256(original.encode()).hexdigest()
        async with self.store.transaction() as db:
            cursor = await db.execute(
                "SELECT id,input_hash FROM playlist_discovery_requests"
                " WHERE owner=? AND request_key=?",
                [owner, key],
            )
            old = await cursor.fetchone()
            if old:
                if old["input_hash"] != input_hash:
                    raise PlaylistError("This mix key was already used with different input.")
                return str(old["id"])
            ident, run = secrets.token_hex(12), secrets.token_hex(12)
            await db.execute(
                "INSERT INTO playlist_mixes(id,owner,definition) VALUES (?,?,?)",
                [ident, owner, definition.model_dump_json()],
            )
            await db.execute(
                "INSERT INTO playlist_discovery_requests(id,owner,request_key,input_hash)"
                " VALUES (?,?,?,?)",
                [ident, owner, key, input_hash],
            )
            data = {
                "publish_after_generation": not definition.review_before_publishing,
                "enable_after": enable_after and bool(definition.interval),
            }
            await db.execute(
                "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,'queued',?)",
                [run, ident, time.time(), json.dumps(data)],
            )
        return ident

    async def run(self, ident: str) -> dict[str, Any]:
        row = await self.store.fetchone("SELECT * FROM playlist_runs WHERE id=?", [ident])
        if not row:
            raise PlaylistError("This run is no longer available.")
        data = dict(row)
        data["data"] = await anyio.to_thread.run_sync(json.loads, data["data"])
        return data

    async def runs(self, mix: str) -> list[dict[str, Any]]:
        rows = await self.store.fetchall(
            "SELECT * FROM playlist_runs WHERE mix=? ORDER BY created DESC LIMIT 20", [mix]
        )
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    async def enqueue(
        self,
        mix: str,
        *,
        automatic: bool = False,
        enable_after: bool = False,
        review: bool | None = None,
    ) -> str:
        ident = secrets.token_hex(12)
        async with self.store.transaction() as db:
            cursor = await db.execute(
                "SELECT id FROM playlist_runs WHERE mix=? AND state IN"
                " ('queued','generating','preview','publishing','uncertain')",
                [mix],
            )
            if await cursor.fetchone():
                raise PlaylistError("Finish or discard the current run before starting another.")
            cursor = await db.execute("SELECT definition FROM playlist_mixes WHERE id=?", [mix])
            row = await cursor.fetchone()
            if not row:
                raise PlaylistError("This mix is no longer available.")
            definition = Definition.model_validate_json(row["definition"])
            # Only newly requested runs receive publication permission. In particular,
            # old queued runs with empty data must still stop for explicit approval.
            data = {
                "publish_after_generation": not (
                    definition.review_before_publishing if review is None else review
                ),
                "enable_after": enable_after and bool(definition.interval),
            }
            await db.execute(
                "INSERT INTO playlist_runs(id,mix,created,state,automatic,data)"
                " VALUES (?,?,?,'queued',?,?)",
                [ident, mix, time.time(), int(automatic), json.dumps(data)],
            )
        return ident

    async def update(self, run: str, state: str, data: dict[str, Any], error: str = "") -> None:
        await self.store.execute(
            "UPDATE playlist_runs SET state=?,data=?,error=? WHERE id=?",
            [state, await anyio.to_thread.run_sync(json.dumps, data), error, run],
        )

    async def pause(self, mix: str) -> None:
        """Revoke pending refresh enables together with the mix's schedule."""
        async with self.store.transaction() as db:
            await db.execute("UPDATE playlist_mixes SET enabled=0 WHERE id=?", [mix])
            cursor = await db.execute(
                "SELECT id,data FROM playlist_runs WHERE mix=? AND state IN"
                " ('queued','generating','preview','publishing','uncertain','failed')",
                [mix],
            )
            # Include older failed runs outside the dashboard's bounded history: an
            # explicit later retry must not undo this pause either.
            for row in await cursor.fetchall():
                data = json.loads(row["data"])
                if data.get("enable_after"):
                    data["enable_after"] = False
                    await db.execute(
                        "UPDATE playlist_runs SET data=? WHERE id=?",
                        [json.dumps(data), row["id"]],
                    )
