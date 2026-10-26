"""Internal provenance publication and reviewed legacy conversion.

These are trusted service boundaries, never browser-supplied ownership assertions.
Scanning, native grants and profile creation cannot establish import credit.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence
from typing import Protocol

import aiosqlite

from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.policy import builtin_profile_id
from shijhon.collection.profiles import Profiles
from shijhon.collection.repository import JOIN, WITH_KEYS, joined, saved_sql
from shijhon.store import Store


class SetupAccess(Protocol):
    """Parent-supplied fresh native verification, including administrator authority."""

    async def authorize_setup(self, actor_id: str) -> Actor:
        """Refuse unless this immutable account currently has setup/admin authority."""
        ...

    async def authorize(self, owner_id: str) -> Actor: ...


async def _verified(access: SetupAccess, actor_id: str, library_id: str) -> None:
    actor = await access.authorize_setup(actor_id)
    if actor.owner_id != actor_id or library_id not in actor.library_ids:
        raise CollectionError("Library setup authority could not be verified.")


async def _accounts(access: SetupAccess, owners: Sequence[str], library_id: str) -> None:
    for owner in owners:
        actor = await access.authorize(owner)
        if actor.owner_id != owner or library_id not in actor.library_ids:
            raise CollectionError("An existing library account could not be verified.")


class Origins:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def initialize_legacy(self, library_id: str) -> int:
        """Explicit compatibility initialization for a known legacy installation.

        Idempotent. Never call as a fallback for missing imports/config/provenance.
        A personal policy cannot be downgraded, even when it has no originals.
        """
        return await self._initialize(library_id, "legacy_shared")

    async def initialize_personal(self, library_id: str) -> int:
        """Initialize a new installation. Legacy conversion requires reviewed setup."""
        return await self._initialize(library_id, "personal")

    async def _initialize(self, library_id: str, mode: str) -> int:
        if not library_id:
            raise CollectionError("Choose a music library.")
        async with self.store.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO collection_library_policy(library_id,mode,revision)"
                " VALUES (?,?,1)",
                [library_id, mode],
            )
            cursor = await conn.execute(
                "SELECT mode,revision FROM collection_library_policy WHERE library_id=?",
                [library_id],
            )
            row = await cursor.fetchone()
            assert row
            if row["mode"] != mode:
                raise CollectionError("This library policy requires its reviewed setup.")
            return int(row["revision"])

    async def review_legacy(
        self,
        *,
        actor_id: str,
        library_id: str,
        legacy_owner_id: str,
        existing_owner_ids: Sequence[str],
        access: SetupAccess,
    ) -> str:
        """Freeze present originals, named accounts and their original-based profiles.

        The caller must obtain the account list from native administration, not logins
        or remembered grants. The returned setup ID is the exact review/approval token.
        Later files/accounts/profiles are deliberately outside this frozen conversion.
        Explicit saved/remove overrides are retained, including newer choices.
        """
        owners = sorted(set(existing_owner_ids))
        if (
            not actor_id
            or not legacy_owner_id
            or legacy_owner_id not in owners
            or len(owners) > 10_000
            or any(not owner or len(owner) > 300 for owner in owners)
        ):
            raise CollectionError("Choose the existing accounts and the original music owner.")
        await _verified(access, actor_id, library_id)
        await _accounts(access, owners, library_id)
        for owner in owners:
            await Profiles(self.store).ensure(owner)
        ident = secrets.token_hex(16)
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT revision FROM collection_library_policy"
                " WHERE library_id=? AND mode='legacy_shared'",
                [library_id],
            )
            row = await cursor.fetchone()
            if not row:
                raise CollectionError(
                    "Initialize the legacy library policy before reviewing setup."
                )
            cursor = await conn.execute(
                "SELECT 1 FROM collection_index WHERE library_id=?"
                " AND generation<>'' AND updated_at>0",
                [library_id],
            )
            if not await cursor.fetchone():
                raise CollectionError("Prepare the music index before reviewing setup.")
            await conn.execute(
                "INSERT INTO collection_original_setups VALUES (?,?,?,?,?,'review',?,NULL)",
                [ident, library_id, actor_id, legacy_owner_id, row["revision"], time.time()],
            )
            await conn.executemany(
                "INSERT INTO collection_original_setup_accounts VALUES (?,?)",
                [(ident, owner) for owner in owners],
            )
            await conn.execute(
                "INSERT INTO collection_original_setup_tracks"
                " SELECT ?,track_key,song_id FROM collection_tracks"
                " WHERE library_id=? AND original=1 AND present=1",
                [ident, library_id],
            )
            await conn.execute(
                "INSERT INTO collection_original_setup_profiles"
                " SELECT ?,p.owner_id,p.id FROM collection_profiles p"
                " JOIN collection_original_setup_accounts a ON a.owner_id=p.owner_id"
                " WHERE a.setup_id=? AND p.base_original=1",
                [ident, ident],
            )
            await conn.execute(
                "INSERT INTO collection_original_setup_preferences"
                " SELECT ?,v.owner_id,v.active_profile_id,v.revision"
                " FROM collection_preferences v"
                " JOIN collection_original_setup_accounts a ON a.owner_id=v.owner_id"
                " WHERE a.setup_id=?",
                [ident, ident],
            )
        return ident

    async def apply_legacy(
        self,
        setup_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        access: SetupAccess,
    ) -> int:
        """Apply only the persisted review, atomically and idempotently.

        Fresh administrator/account grants are required on every attempt. The chosen
        immutable owner cannot be changed at approval. No files/native rows are written.
        """
        setup = await self.store.fetchone(
            "SELECT * FROM collection_original_setups WHERE id=? AND actor_id=?",
            [setup_id, actor_id],
        )
        if not setup or setup["expected_revision"] != expected_revision:
            raise CollectionError("This original library setup is unavailable.")
        library = str(setup["library_id"])
        await _verified(access, actor_id, library)
        accounts = await self.store.fetchall(
            "SELECT owner_id FROM collection_original_setup_accounts WHERE setup_id=?",
            [setup_id],
        )
        await _accounts(access, [row["owner_id"] for row in accounts], library)
        async with self.store.transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM collection_library_policy WHERE library_id=?", [library]
            )
            current = await cursor.fetchone()
            if current and current["mode"] == "personal" and current["setup_id"] == setup_id:
                return int(current["revision"])
            if (
                not current
                or current["mode"] != "legacy_shared"
                or current["revision"] != expected_revision
            ):
                raise CollectionError("The library policy changed. Review setup again.")
            cursor = await conn.execute(
                "SELECT 1 FROM collection_original_setup_accounts a"
                " LEFT JOIN collection_original_setup_preferences f"
                " ON f.setup_id=a.setup_id AND f.owner_id=a.owner_id"
                " WHERE a.setup_id=? AND f.owner_id IS NULL LIMIT 1",
                [setup_id],
            )
            if await cursor.fetchone():
                raise CollectionError(
                    "The reviewed library choices are unavailable. Review setup again."
                )
            cursor = await conn.execute(
                "SELECT 1 FROM collection_original_setup_tracks s"
                " LEFT JOIN collection_tracks t ON t.library_id=? AND t.track_key=s.track_key"
                " WHERE s.setup_id=? AND (t.track_key IS NULL OR t.present<>1"
                " OR t.original<>1 OR t.song_id<>s.song_id) LIMIT 1",
                [library, setup_id],
            )
            if await cursor.fetchone():
                raise CollectionError("Reviewed music changed. Review setup again.")
            now = time.time()
            await conn.execute(
                "INSERT INTO collection_originals"
                " SELECT ?,?,track_key,?,? FROM collection_original_setup_tracks WHERE setup_id=?"
                " ON CONFLICT(owner_id,library_id,track_key) DO NOTHING",
                [setup["legacy_owner_id"], library, "legacy:" + setup_id, now, setup_id],
            )
            # Materialize only formerly implicit choices. Explicit 0/1 overrides
            # and their revision/operation history stay intact.
            await conn.execute(
                "INSERT INTO collection_membership"
                "(owner_id,profile_id,library_id,track_key,saved,revision,last_operation)"
                " SELECT p.owner_id,p.profile_id,?,t.track_key,1,1,?"
                " FROM collection_original_setup_profiles p"
                " JOIN collection_profiles cp ON cp.id=p.profile_id AND cp.owner_id=p.owner_id"
                " JOIN collection_original_setup_tracks t ON t.setup_id=p.setup_id"
                " WHERE p.setup_id=? AND p.owner_id<>? AND cp.base_original=1"
                " ON CONFLICT(owner_id,profile_id,library_id,track_key) DO UPDATE"
                " SET saved=1,revision=collection_membership.revision+1,"
                "last_operation=excluded.last_operation"
                " WHERE collection_membership.saved IS NULL",
                [library, "legacy:" + setup_id, setup_id, setup["legacy_owner_id"]],
            )
            # A non-owner's Original imported library holds only their own imports once
            # the originals are the owner's. Keeping it active would silently empty their
            # app library; switching to All could add catalog music. Freeze an
            # independent exact selection of what it shows now (the shared originals'
            # whole albums, favorites, imported playlists, its hand choices) instead, and
            # activate it with the reviewed CAS.
            cursor = await conn.execute(
                "SELECT f.owner_id FROM collection_original_setup_preferences f"
                " JOIN collection_preferences v ON v.owner_id=f.owner_id"
                " WHERE f.setup_id=? AND f.owner_id<>? AND f.active_profile_id='original'"
                " AND v.active_profile_id=f.active_profile_id AND v.revision=f.revision",
                [setup_id, setup["legacy_owner_id"]],
            )
            for account in await cursor.fetchall():
                owner = str(account["owner_id"])
                snapshot = secrets.token_hex(16)
                cursor = await conn.execute(
                    "SELECT name FROM collection_profiles WHERE owner_id=?", [owner]
                )
                names = {str(row["name"]).casefold() for row in await cursor.fetchall()}
                name, suffix = "Shared original library", 1
                while name.casefold() in names:
                    suffix += 1
                    name = f"Shared original library ({suffix})"
                await conn.execute(
                    "INSERT INTO collection_profiles"
                    "(id,owner_id,name,base_original,mode,created) VALUES (?,?,?,0,'saved',?)",
                    [snapshot, owner, name, now],
                )
                original = builtin_profile_id(owner, "original")
                await conn.execute(
                    "INSERT OR IGNORE INTO collection_profiles"
                    "(id,owner_id,kind,name,base_original,created)"
                    " VALUES (?,?,'builtin',?,0,?)",
                    [original, owner, original, now],
                )
                # Read while the library's originals are still shared.
                join = joined(owner, original, library)
                await conn.execute(
                    WITH_KEYS + "INSERT INTO collection_membership"
                    "(owner_id,profile_id,library_id,track_key,saved,revision,last_operation)"
                    " SELECT p.owner_id,?,t.library_id,t.track_key,1,1,?"
                    + JOIN
                    + " WHERE t.library_id=? AND "
                    + saved_sql("original"),
                    [*join[:5], snapshot, "legacy:" + setup_id, *join[5:], library],
                )
                await conn.execute(
                    "UPDATE collection_preferences SET active_profile_id=?,revision=revision+1"
                    " WHERE owner_id=? AND active_profile_id='original' AND revision=("
                    "SELECT revision FROM collection_original_setup_preferences"
                    " WHERE setup_id=? AND owner_id=?)",
                    [snapshot, owner, setup_id, owner],
                )
            # The reviewed conversion explicitly preserves these owners' shared
            # choices. Keep that positive evidence outside the profile lifecycle.
            await conn.execute(
                "INSERT OR IGNORE INTO collection_history(owner_id,library_id,track_key,added_at)"
                " SELECT m.owner_id,m.library_id,m.track_key,? FROM collection_membership m"
                " JOIN collection_original_setup_accounts a ON a.owner_id=m.owner_id"
                " AND a.setup_id=? WHERE m.library_id=? AND m.saved=1",
                [now, setup_id, library],
            )
            await conn.execute(
                "UPDATE collection_library_policy SET mode='personal',"
                "revision=revision+1,setup_id=?"
                " WHERE library_id=?",
                [setup_id, library],
            )
            await conn.execute(
                "UPDATE collection_original_setups SET state='applied',applied_at=? WHERE id=?",
                [now, setup_id],
            )
        return expected_revision + 1


async def record_verified_original(
    conn: aiosqlite.Connection,
    *,
    owner_id: str,
    library_id: str,
    track_key: str,
    song_id: str,
    provenance_id: str,
    asset_id: str,
) -> None:
    """Publish inside the ingestion receipt/status transaction, after native verification.

    The trusted caller binds owner/provenance/asset from its durable receipt before
    writing audio, and freshly verifies identity, grant and exact native file/path.
    This helper checks structural identity and policy; it cannot prove file bytes or
    authenticate HTTP input. A path/scan/grant alone must never call this function.
    Do not report import success until this transaction (including receipt) commits.
    """
    if not conn.in_transaction or not all(
        (owner_id, library_id, track_key, song_id, provenance_id, asset_id)
    ):
        raise CollectionError("Verified import publication requires its receipt transaction.")
    cursor = await conn.execute(
        "SELECT 1 FROM collection_tracks t JOIN collection_library_policy p"
        " ON p.library_id=t.library_id AND p.mode='personal'"
        " WHERE t.library_id=? AND t.track_key=? AND t.song_id=?"
        " AND t.original=1 AND t.present=1",
        [library_id, track_key, song_id],
    )
    if not await cursor.fetchone():
        raise CollectionError("The verified imported song is not ready in the personal library.")
    alias = "import:" + asset_id
    cursor = await conn.execute(
        "SELECT track_key FROM collection_track_aliases WHERE library_id=? AND alias=?",
        [library_id, alias],
    )
    existing = await cursor.fetchone()
    if existing and existing["track_key"] != track_key:
        raise CollectionError("The imported asset identity conflicts with the music index.")
    await conn.execute(
        "INSERT OR IGNORE INTO collection_track_aliases VALUES (?,?,?)",
        [library_id, alias, track_key],
    )
    cursor = await conn.execute(
        "INSERT INTO collection_originals VALUES (?,?,?,?,?)"
        " ON CONFLICT(owner_id,library_id,track_key) DO NOTHING",
        [owner_id, library_id, track_key, provenance_id, time.time()],
    )
    if cursor.rowcount:
        await conn.execute(
            "UPDATE collection_library_policy SET revision=revision+1 WHERE library_id=?",
            [library_id],
        )


async def record_link_credit(
    conn: aiosqlite.Connection,
    *,
    owner_id: str,
    library_id: str,
    track_key: str,
    song_id: str,
    item_id: str,
) -> bool:
    """Credit an import with a song the library already had (``music_imports.overlap``).

    Inside the link's receipt transaction, after its ``music_import_links`` row is
    written and the importing caller's fresh native check of that song. The credit is the
    importer's own import credit, for any present song - also a catalog song Shijhon
    added (``sync_link_credit``). Returns whether this link holds it: an owner who
    already has credit for the song (an earlier import of their own, or another active
    link) keeps that one.
    """
    if not conn.in_transaction or not all((owner_id, library_id, track_key, song_id, item_id)):
        raise CollectionError("A linked import requires its receipt transaction.")
    cursor = await conn.execute(
        "SELECT 1 FROM collection_tracks t JOIN collection_library_policy p"
        " ON p.library_id=t.library_id AND p.mode='personal'"
        " WHERE t.library_id=? AND t.track_key=? AND t.song_id=? AND t.present=1",
        [library_id, track_key, song_id],
    )
    if not await cursor.fetchone():
        raise CollectionError("The linked song is not ready in the personal library.")
    return (
        await sync_link_credit(conn, owner_id=owner_id, library_id=library_id, track_key=track_key)
        == "link:" + item_id
    )


async def sync_link_credit(
    conn: aiosqlite.Connection, *, owner_id: str, library_id: str, track_key: str
) -> str:
    """The owner's import credit for a song their imports were linked to, derived from
    every active link of theirs to it (``music_import_links``): while one is active the
    owner has the credit (provenance ``link:<item>`` of the earliest active one, kept
    while its link stays active); with none it goes. A credit of another kind (their own
    imported file) is never changed. In the transaction that changes a link. Returns the
    provenance of the owner's credit for the song afterwards ("" for none)."""
    if not conn.in_transaction:
        raise CollectionError("A linked import requires its receipt transaction.")
    cursor = await conn.execute(
        "SELECT provenance_id FROM collection_originals"
        " WHERE owner_id=? AND library_id=? AND track_key=?",
        [owner_id, library_id, track_key],
    )
    row = await cursor.fetchone()
    current = str(row["provenance_id"]) if row else ""
    if row and not current.startswith("link:"):
        return current
    cursor = await conn.execute(
        "SELECT item_id FROM music_import_links WHERE owner_id=? AND library_id=?"
        " AND track_key=? AND active=1 ORDER BY item_id<>?,created,item_id LIMIT 1",
        [owner_id, library_id, track_key, current.removeprefix("link:")],
    )
    source = await cursor.fetchone()
    wanted = "link:" + str(source["item_id"]) if source else ""
    if wanted == current:
        return current
    if not wanted:
        await conn.execute(
            "DELETE FROM collection_originals WHERE owner_id=? AND library_id=? AND track_key=?",
            [owner_id, library_id, track_key],
        )
    elif row:
        # Another active link of the owner's carries the credit on: nothing they see
        # changes.
        await conn.execute(
            "UPDATE collection_originals SET provenance_id=?"
            " WHERE owner_id=? AND library_id=? AND track_key=?",
            [wanted, owner_id, library_id, track_key],
        )
        return wanted
    else:
        await conn.execute(
            "INSERT INTO collection_originals VALUES (?,?,?,?,?)",
            [owner_id, library_id, track_key, wanted, time.time()],
        )
    await conn.execute(
        "UPDATE collection_library_policy SET revision=revision+1 WHERE library_id=?",
        [library_id],
    )
    return wanted
