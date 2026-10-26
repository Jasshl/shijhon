"""Owned immutable profile identities; activation is always explicit."""

from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Any

import aiosqlite

from shijhon.collection.contracts import CollectionError
from shijhon.collection.policy import builtin_profile_id, builtin_selection
from shijhon.collection.repository import JOIN, WITH_KEYS, joined, rule_sql, selection_of
from shijhon.store import Store

# The built-in libraries, in the picker's order (All is the default in apps).
BUILTINS = {"all": "All", "original": "Original imported library", "shijhon": "Shijhon library"}


class Profiles:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def ensure(self, owner: str) -> None:
        async with self.store.transaction() as conn:
            for selection in BUILTINS:
                ident = builtin_profile_id(owner, selection)
                # All's profile kept its name from before the others had one; theirs are
                # their IDs, which no custom name can be.
                # Only All's profile is a live base of the account's imports (as before);
                # the others hold their rules (``policy.predicate``).
                await conn.execute(
                    "INSERT OR IGNORE INTO collection_profiles"
                    "(id,owner_id,kind,name,base_original,created) VALUES (?,?,'builtin',?,?,?)",
                    [
                        ident,
                        owner,
                        "All" if selection == "all" else ident,
                        int(selection == "all"),
                        time.time(),
                    ],
                )
            await conn.execute(
                "INSERT OR IGNORE INTO "
                "collection_preferences(owner_id,active_profile_id) VALUES (?,?)",
                [owner, "all"],
            )

    async def get(self, owner: str, ident: str | None = None) -> dict[str, Any]:
        """A library by its selection (``all``, ``original``, ``shijhon`` or a custom
        library's ID) or by its membership profile's ID; ``id`` is the profile's ID."""
        await self.ensure(owner)
        if ident is None:
            selected = await self.store.fetchone(
                "SELECT active_profile_id FROM collection_preferences WHERE owner_id=?", [owner]
            )
            ident = str(selected["active_profile_id"]) if selected else "all"
        public = ident if ident in BUILTINS else builtin_selection(owner, ident)
        if public:
            ident = builtin_profile_id(owner, public)
        row = await self.store.fetchone(
            "SELECT * FROM collection_profiles WHERE owner_id=? AND id=? AND archived=0",
            [owner, ident],
        )
        if not row or (row["kind"] == "builtin") != bool(public):
            raise CollectionError("This collection library is unavailable.")
        result = dict(row)
        if public:
            result.update(selection_id=public, name=BUILTINS[public], builtin=True)
        else:
            result.update(selection_id=ident, builtin=False)
        return result

    async def listing(self, owner: str) -> list[dict[str, Any]]:
        await self.ensure(owner)
        return [
            dict(row)
            for row in await self.store.fetchall(
                "SELECT p.*,p.id=v.active_profile_id AS active FROM collection_profiles p"
                " JOIN collection_preferences v ON v.owner_id=p.owner_id"
                " WHERE p.owner_id=? AND p.kind='custom' ORDER BY p.archived,p.created,p.id",
                [owner],
            )
        ]

    async def create(
        self, owner: str, name: str, *, start: str = "original", source: str = ""
    ) -> str:
        """A custom library: a copy of Original imported library (``original``), an empty
        selection or a copy of a library. A copy holds what its source holds now, as its own
        choices; from then on it changes only by hand (no live base of the account's
        imports)."""
        name = checked_name(name)
        if start not in ("original", "empty", "copy"):
            raise CollectionError("Start from Original, empty, or an existing profile.")
        old = None
        if start == "copy":
            old = await self.get(owner, source)
        elif start == "original":
            old = await self.get(owner, "original")
        ident = secrets.token_hex(16)
        try:
            async with self.store.transaction() as conn:
                if old:
                    cursor = await conn.execute(
                        "SELECT 1 FROM collection_profiles"
                        " WHERE owner_id=? AND id=? AND archived=0",
                        [owner, old["id"]],
                    )
                    if not await cursor.fetchone():
                        raise CollectionError("The source library is unavailable.")
                await conn.execute(
                    "INSERT INTO collection_profiles(id,owner_id,name,base_original,mode,created)"
                    " VALUES (?,?,?,0,?,?)",
                    [ident, owner, name, old["mode"] if old else "saved", time.time()],
                )
                if old:
                    await _snapshot(conn, owner, old["id"], old["selection_id"], ident)
        except sqlite3.IntegrityError:
            raise CollectionError("Choose a different profile name.") from None
        return ident

    async def rename(self, owner: str, ident: str, name: str) -> None:
        profile = await self.get(owner, ident)
        if profile["builtin"]:
            raise CollectionError("Built-in libraries keep their names.")
        try:
            await self.store.execute(
                "UPDATE collection_profiles SET "
                "name=?,revision=revision+1 WHERE owner_id=? AND id=?",
                [checked_name(name), owner, ident],
            )
        except sqlite3.IntegrityError:
            raise CollectionError("Choose a different profile name.") from None

    async def activate(self, owner: str, ident: str) -> None:
        profile = await self.get(owner, ident)
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM collection_profiles WHERE owner_id=? AND id=? AND archived=0",
                [owner, profile["id"]],
            )
            if not await cursor.fetchone():
                raise CollectionError("This collection library is unavailable.")
            await conn.execute(
                "UPDATE collection_preferences SET active_profile_id=?,revision=revision+1"
                " WHERE owner_id=?",
                [profile["selection_id"], owner],
            )

    async def archive(self, owner: str, ident: str, *, archived: bool = True) -> None:
        await self.ensure(owner)
        if ident in BUILTINS or builtin_selection(owner, ident):
            raise CollectionError("Built-in libraries stay available.")
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT active_profile_id FROM collection_preferences WHERE owner_id=?", [owner]
            )
            current = await cursor.fetchone()
            if archived and current and current["active_profile_id"] == ident:
                raise CollectionError("Use another profile in apps before archiving this one.")
            await conn.execute(
                "UPDATE collection_profiles SET "
                "archived=?,revision=revision+1 WHERE owner_id=? AND id=?",
                [int(archived), owner, ident],
            )


def checked_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 80 or not name.isprintable():
        raise CollectionError("Enter a profile name of 1 to 80 printable characters.")
    if name.casefold() in (b.casefold() for b in BUILTINS.values()):
        raise CollectionError("Choose a name that is not a built-in library's.")
    return name


async def _snapshot(
    conn: aiosqlite.Connection, owner: str, source: str, selection: str, copy: str
) -> None:
    """``copy`` holds what ``source`` holds now, as its own hand choices: every song in it,
    and - in the albums it holds - a removal of every song it leaves out (a hand-added
    song brings its album, so the copy's albums keep the songs the source left out out)."""
    rule = rule_sql(selection_of(owner, source, selection if selection in BUILTINS else ""))
    cursor = await conn.execute("SELECT library_id FROM collection_library_policy")
    for (library,) in list(await cursor.fetchall()):
        join = joined(owner, source, str(library))
        await conn.execute(
            WITH_KEYS + "INSERT INTO collection_membership(owner_id,profile_id,"
            "library_id,track_key,saved,revision,last_operation)"
            " SELECT p.owner_id,?,t.library_id,t.track_key,1,0,''"
            + JOIN
            + f" WHERE t.library_id=? AND ({rule})=1",
            [*join[:5], copy, *join[5:], library],
        )
        await conn.execute(
            "INSERT INTO collection_membership(owner_id,profile_id,library_id,track_key,"
            "saved,revision,last_operation) SELECT ?,?,t.library_id,t.track_key,0,0,''"
            " FROM collection_tracks t WHERE t.library_id=? AND t.album_id IN ("
            "SELECT r.album_id FROM collection_membership c JOIN collection_tracks r"
            " ON r.library_id=c.library_id AND r.track_key=c.track_key"
            " WHERE c.owner_id=? AND c.profile_id=? AND c.library_id=? AND c.saved=1)"
            " AND NOT EXISTS (SELECT 1 FROM collection_membership x WHERE x.owner_id=?"
            " AND x.profile_id=? AND x.library_id=t.library_id AND x.track_key=t.track_key)",
            [owner, copy, library, owner, copy, library, owner, copy],
        )
