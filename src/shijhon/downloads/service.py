"""A bounded audio worker; native playlist snapshots never become playlist writes."""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import aiosqlite
import anyio
from pydantic import SecretStr

from shijhon.delivery.download_first import DownloadFirst
from shijhon.downloads.model import (
    ACTIVE,
    MAX_AGENT_REQUESTS,
    MAX_ENTRIES,
    MAX_PLAYLISTS,
    Authorization,
    ConnectionRequired,
    PreparationError,
    native_id,
)
from shijhon.downloads.repository import Repository
from shijhon.navidrome.client import NavidromeService
from shijhon.playlists.client import OwnerClient
from shijhon.playlists.model import PlaylistError
from shijhon.playlists.requests import RequestError
from shijhon.playlists.service import Playlists
from shijhon.store import Store

log = logging.getLogger(__name__)

_READ_AT_ONCE = 500  # songs looked up in one statement


class Attribution(Protocol):
    """Keeps a preparation's catalog songs to record as the account's activity
    (``CollectionRequests``)."""

    async def pending(
        self, db: aiosqlite.Connection, owner: str, items: Sequence[tuple[str, str]]
    ) -> None: ...

    def notify(self) -> None: ...


class _Stopped(PreparationError):
    """The batch stopped before this song was admitted; no acquisition should start."""


class _AgentStopped(PreparationError):
    """An unfinished agent job needs explicit reauthorization and resume."""


class Preparation:
    def __init__(
        self,
        store: Store,
        native: NavidromeService,
        download_first: DownloadFirst,
        *,
        playlists: Playlists | None = None,
        workers: int | Callable[[], int] | None = None,
    ) -> None:
        self.repo = Repository(store)
        self.client = OwnerClient(native)
        self.fetcher, self.playlists = download_first, playlists
        self.workers = workers
        self._running: set[int] = set()
        self.changes = anyio.Lock()
        self.wake = anyio.Event()
        self._active: Counter[str] = Counter()
        self._last_owner = ""
        self._started = False
        # Keeps the catalog songs a preparation was asked for to record as the account's
        # activity (their albums join its Shijhon library), with the batch.
        self.attribution: Attribution | None = None

    @property
    def max_entries(self) -> int:
        """The longest playlist prepared: as long as an import may be."""
        settings = getattr(self.playlists, "settings", None)
        return int(getattr(settings, "import_max_entries", MAX_ENTRIES))

    @property
    def worker_limit(self) -> int:
        value = self.workers() if callable(self.workers) else self.workers
        if value is None:
            scheduler = getattr(self.fetcher, "bulk", None)
            value = getattr(scheduler, "downloads", 4)
        # An unlimited installation still has a finite number of background tasks.
        return min(1000, max(1, value or 32))

    async def aclose(self) -> None:
        await self.client.aclose()

    def notify(self) -> None:
        self.wake.set()
        self.wake = anyio.Event()

    async def connect(self, owner: str, username: str, password: str, address: str) -> None:
        async with self.changes:
            self._idle_owner(owner)
            try:
                login = await self.client.login(username, password, address)
                if login["id"] != owner:
                    raise PreparationError("Your account changed. Sign out and sign in again.")
                authorization = Authorization(
                    user_id=owner,
                    username=username,
                    token=SecretStr(login["subsonicToken"]),
                    salt=SecretStr(login["subsonicSalt"]),
                )
                await self.client.validate(authorization)
            except PlaylistError:
                raise PreparationError(
                    "Your Navidrome authorization could not be verified."
                ) from None
            await self._save(authorization)

    def _idle_owner(self, owner: str) -> None:
        if self._active[owner]:
            raise PreparationError(
                "Cancel preparation and wait for active songs before changing authorization."
            )

    async def _save(self, authorization: Authorization) -> None:
        async with self.repo.store.transaction() as db:
            await db.execute(
                "INSERT INTO audio_owners(id,username,authorization,generation,library_id)"
                " VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                " username=excluded.username,authorization=excluded.authorization,"
                " generation=excluded.generation,library_id=excluded.library_id",
                [
                    authorization.user_id,
                    authorization.username,
                    authorization.stored(),
                    secrets.token_hex(16),
                    self.client.native.library_id,
                ],
            )
            await db.execute(
                "UPDATE audio_batches SET state='paused',"
                "error='Authorization changed. Resume to continue.'"
                " WHERE owner=? AND state IN ('queued','running')",
                [authorization.user_id],
            )
            await db.execute(
                "UPDATE audio_items SET state='paused' WHERE state='queued' AND batch IN"
                " (SELECT id FROM audio_batches WHERE owner=?)",
                [authorization.user_id],
            )

    async def disconnect(self, owner: str) -> None:
        async with self.changes:
            self._idle_owner(owner)
            row = await self.repo.owner(owner)
            if row is None:
                raise PreparationError("There is no saved download authorization.")
            async with self.repo.store.transaction() as db:
                await db.execute(
                    "UPDATE audio_owners SET authorization=NULL,generation=? WHERE id=?",
                    [secrets.token_hex(16), owner],
                )
                await db.execute(
                    "UPDATE audio_batches SET state='paused',"
                    "error='Reconnect to resume preparation.'"
                    " WHERE owner=? AND state IN ('queued','running')",
                    [owner],
                )
                await db.execute(
                    "UPDATE audio_items SET state='paused' WHERE state='queued' AND batch IN"
                    " (SELECT id FROM audio_batches WHERE owner=?)",
                    [owner],
                )

    async def connection(self, owner: str, username: str) -> Authorization:
        async with self.changes:
            return await self._connection(owner, username)

    async def _connection(
        self,
        owner: str,
        username: str,
        authorize: Callable[[], Awaitable[None]] | None = None,
        *,
        reuse_playlist: bool = False,
    ) -> Authorization:
        row = await self.repo.owner(owner)
        if self.playlists is not None and (
            row is None or (reuse_playlist and row["authorization"])
        ):
            saved = await self.playlists.repo.connection(owner)
            if saved is not None and saved.username == username:
                auth = Authorization(
                    user_id=owner, username=username, token=saved.token, salt=saved.salt
                )
                if row is None or row["authorization"] != auth.stored():
                    self._idle_owner(owner)
                    await self.client.validate(auth)
                    if authorize:
                        await authorize()
                    await self._save(auth)
                    row = await self.repo.owner(owner)
        if not owner or row is None or not row["authorization"]:
            raise ConnectionRequired("Connect your Navidrome account to prepare playlist audio.")
        auth = Authorization.model_validate_json(row["authorization"])
        if auth.user_id != owner or auth.username != username or row["username"] != username:
            raise PreparationError("Your account changed. Sign out and sign in again.")
        if row["library_id"] != self.client.native.library_id:
            raise PreparationError("The configured library changed. Reconnect your account.")
        try:
            await self.client.validate(auth)
        except PlaylistError:
            raise PreparationError(
                "Your authorization expired or library access changed. Reconnect."
            ) from None
        return auth

    async def playlists_for(self, owner: str, username: str) -> list[dict[str, Any]]:
        auth = await self.connection(owner, username)
        try:
            reply = await self.client.call(auth, "getPlaylists")
        except PlaylistError:
            raise PreparationError(
                "Your playlists could not be read. Check your authorization."
            ) from None
        listing = reply.get("playlists", {}).get("playlist", [])
        if not isinstance(listing, list) or len(listing) > MAX_PLAYLISTS:
            raise PreparationError("The playlist list is too large or could not be read.")
        return [
            {
                "id": native_id(row.get("id")),
                "name": str(row.get("name") or "Playlist")[:300],
                "songCount": row.get("songCount", 0),
            }
            for row in listing
            if isinstance(row, dict) and row.get("owner") == username
        ]

    async def _agent_check(self, creator: str | None, owner: str) -> None:
        if creator is None:
            return
        try:
            if self.playlists is None:
                raise PlaylistError("Unavailable.")
            connection, _grant = await self.playlists.requests.grant(creator, "downloads")
            if connection.user_id != owner:
                raise PlaylistError("Unavailable.")
        except PlaylistError:
            raise _AgentStopped(
                "Agent authorization changed. Reauthorize audio preparation and resume."
            ) from None

    async def prepare(
        self,
        owner: str,
        username: str,
        ident: str,
        *,
        creator: str | None = None,
        idempotency_key: str = "",
        authorize: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        ident = native_id(ident)
        async with self.changes:
            if creator:
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", idempotency_key):
                    raise RequestError(
                        "Use an idempotency key of 8 to 128 letters, digits or _.:-."
                    )
                await self._agent_check(creator, owner)
                replay = await self.repo.replay(creator, idempotency_key)
                if replay:
                    if (replay["owner"], replay["library_id"], replay["playlist_id"]) != (
                        owner,
                        self.client.native.library_id,
                        ident,
                    ):
                        raise RequestError("This preparation key already has different input.", 409)
                    if not replay["batch"]:
                        raise RequestError("The original preparation record was removed.", 410)
                    if authorize:
                        await authorize()
                    if self.attribution is not None:
                        self.attribution.notify()  # its attribution, kept: tried again now
                    return str(replay["batch"])
                count = await self.repo.store.fetchone(
                    "SELECT COUNT(*) AS n FROM audio_agent_requests WHERE owner=?", [owner]
                )
                if count and count["n"] >= MAX_AGENT_REQUESTS:
                    raise PreparationError("This account has reached its agent preparation limit.")
            if authorize:
                await authorize()
            auth = await self._connection(owner, username, authorize, reuse_playlist=bool(creator))
            try:
                playlist = await self.client.playlist(auth, ident)
            except PlaylistError:
                raise PreparationError("Choose a native playlist owned by your account.") from None
            entries = playlist.get("entry", [])
            limit = self.max_entries
            if not isinstance(entries, list) or len(entries) > limit:
                raise PreparationError(f"Prepare a playlist with at most {limit:,} entries.")
            if playlist.get("id") != ident or playlist.get("songCount", len(entries)) != len(
                entries
            ):
                raise PreparationError("The playlist could not be read completely.")
            snapshot = []
            items: dict[str, dict[str, Any]] = {}
            for position, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise PreparationError("The playlist contains an unreadable song.")
                song = native_id(entry.get("id"))
                snapshot.append(song)
                if song in items:
                    items[song]["entries"] += 1
                    continue
                items[song] = {
                    "id": song,
                    "position": position,
                    "entries": 1,
                    "title": str(entry.get("title") or "Song")[:300],
                    "artist": str(entry.get("artist") or "")[:300],
                    "track_ref": None,
                }
            await self._recordings(items)
            row = await self.repo.owner(owner)
            assert row is not None
            await self._agent_check(creator, owner)
            catalog = [("songId", song) for song, item in items.items() if item["track_ref"]]
            attribution, also = self.attribution, None
            if attribution is not None and catalog:
                principal = await self.client.principal(auth)
                if principal.can_access(self.client.native.library_id):

                    async def also(db: aiosqlite.Connection) -> None:
                        assert attribution is not None
                        await attribution.pending(db, principal.user_id, catalog)

            if authorize:
                await authorize()
            batch = await self.repo.enqueue(
                row,
                playlist,
                list(items.values()),
                snapshot,
                creator=creator,
                idempotency_key=idempotency_key,
                also=also,
            )
            await self.repo.finish(batch)
            self.notify()
            if attribution is not None and also is not None:
                attribution.notify()
            return batch

    async def _recordings(self, items: dict[str, dict[str, Any]]) -> None:
        """Each placeholder song's recording (``track_ref``), read some at a time."""
        songs = list(items)
        for start in range(0, len(songs), _READ_AT_ONCE):
            for row in await self.repo.store.fetchall(
                "SELECT song_id,track_ref FROM placeholders"
                " WHERE song_id IN (SELECT value FROM json_each(?))",
                [json.dumps(songs[start : start + _READ_AT_ONCE])],
            ):
                items[row["song_id"]]["track_ref"] = row["track_ref"]

    async def action(
        self,
        owner: str,
        username: str,
        ident: str,
        action: str,
        *,
        creator: str | None = None,
        authorize: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        async with self.changes:
            batch = await self.repo.batch(ident, owner)
            if creator is not None and batch["creator"] != creator:
                raise PreparationError("That preparation batch is unavailable.")
            # A human can stop/remove an agent job, but cannot restart it around its grant.
            if action in ("resume", "retry"):
                await self._agent_check(batch["creator"], owner)
            if authorize:
                await authorize()
            if action == "cancel":
                if batch["state"] not in ("queued", "running", "cancelling"):
                    raise PreparationError("This batch is no longer running.")
                async with self.repo.store.transaction() as db:
                    await db.execute(
                        "UPDATE audio_batches SET state='cancelling' WHERE id=?", [ident]
                    )
                    await db.execute(
                        "UPDATE audio_items SET state='paused' WHERE batch=? AND state='queued'",
                        [ident],
                    )
                await self.repo.finish(ident)
            elif action == "forget":
                if batch["state"] in ("queued", "running", "cancelling"):
                    raise PreparationError(
                        "Cancel and wait for this batch before removing its record."
                    )
                await self.repo.store.execute("DELETE FROM audio_batches WHERE id=?", [ident])
            elif action in ("resume", "retry"):
                if batch["state"] not in ("paused", "cancelled", "finished", "complete"):
                    raise PreparationError("Wait for active preparation to finish first.")
                active = await self.repo.store.fetchone(
                    "SELECT id FROM audio_batches WHERE owner=? AND playlist_id=? AND id!=?"
                    " AND state IN ('queued','running','cancelling')",
                    [owner, batch["playlist_id"], ident],
                )
                if active:
                    raise PreparationError("This playlist already has an active preparation batch.")
                auth = await self._connection(
                    owner, username, authorize, reuse_playlist=bool(batch["creator"])
                )
                if batch["library_id"] != self.client.native.library_id:
                    raise PreparationError("This snapshot belongs to another configured library.")
                try:
                    await self.client.playlist(auth, batch["playlist_id"])
                except PlaylistError:
                    raise PreparationError(
                        "This playlist is no longer owned by your account."
                    ) from None
                row = await self.repo.owner(owner)
                assert row is not None
                await self._agent_check(batch["creator"], owner)
                if authorize:
                    await authorize()
                async with self.repo.store.transaction() as db:
                    await db.execute(
                        "UPDATE audio_items SET state='queued',error='' WHERE batch=? AND state=?",
                        [ident, "failed" if action == "retry" else "paused"],
                    )
                    await db.execute(
                        "UPDATE audio_batches SET state='queued',generation=?,error='' WHERE id=?",
                        [row["generation"], ident],
                    )
                await self.repo.finish(ident)
            else:
                raise PreparationError("That action is unavailable. Reload the page.")
            self.notify()

    async def reconcile(self) -> None:
        """Startup is local-only: completed swaps are found, unfinished work stays paused."""
        async with self.changes:
            await self.repo.store.execute(
                "UPDATE audio_batches SET state='paused',"
                "error='Preparation stopped. Resume to continue.'"
                " WHERE state IN ('queued','running','cancelling')"
            )
            await self.repo.store.execute(
                "UPDATE audio_items SET state='paused'"
                " WHERE state IN ('queued','waiting','preparing','fetching','verifying')"
            )
            after = 0
            while rows := await self.repo.store.fetchall(
                "SELECT i.rowid AS marker,i.batch,i.song_id FROM audio_items i"
                " JOIN placeholders p ON p.song_id=i.song_id WHERE i.state='paused'"
                " AND p.state='delivered' AND i.rowid>? ORDER BY i.rowid LIMIT 200",
                [after],
            ):
                for row in rows:
                    after = row["marker"]
                    try:
                        settled = await self.fetcher.settled(row["song_id"])
                    except Exception as exc:
                        log.warning("audio preparation recovery: %s", type(exc).__name__)
                        continue  # interrupted file swaps remain paused until repair succeeds
                    if (
                        settled is not None
                        and settled["state"] == "delivered"
                        and self.fetcher.engine.layout.absolute(settled["path"]).is_file()
                    ):
                        await self.repo.progress(row["batch"], row["song_id"], "available")
            for row in await self.repo.store.fetchall(
                "SELECT id FROM audio_batches WHERE state='paused'"
            ):
                await self.repo.finish(row["id"])

    async def run(self) -> None:
        if self._started:
            raise RuntimeError("the audio preparation worker is already running")
        self._started = True
        try:
            await self.reconcile()
            async with anyio.create_task_group() as group:
                while True:
                    for slot in range(self.worker_limit):
                        if slot not in self._running:
                            self._running.add(slot)
                            group.start_soon(self._slot, slot)
                    wake = self.wake
                    with anyio.move_on_after(0.5):
                        await wake.wait()
        finally:
            self._started = False

    async def _slot(self, slot: int) -> None:
        try:
            await self._worker(slot)
        finally:
            self._running.discard(slot)

    async def _worker(self, slot: int) -> None:
        while slot < self.worker_limit:
            try:
                work = await self._claim()
                if work is not None:
                    await self._work(*work)
                    continue
            except Exception as exc:
                log.warning("audio preparation worker: %s", type(exc).__name__)
            wake = self.wake
            with anyio.move_on_after(1):
                await wake.wait()

    async def _claim(self) -> tuple[dict[str, Any], Authorization] | None:
        async with self.changes:
            # The batch first, then its first queued song: both found by index, however
            # many songs are queued.
            row = await self.repo.store.fetchone(
                "SELECT i.*,b.owner,b.generation,b.library_id,b.creator"
                " FROM audio_batches b JOIN audio_items i ON i.batch=b.id"
                " WHERE b.id=(SELECT c.id FROM audio_batches c"
                " WHERE c.state IN ('queued','running') AND EXISTS"
                " (SELECT 1 FROM audio_items q WHERE q.state='queued' AND q.batch=c.id)"
                " ORDER BY (c.owner=?),c.created,c.id LIMIT 1)"
                " AND i.state='queued' ORDER BY i.position LIMIT 1",
                [self._last_owner],
            )
            if row is None:
                return None
            item = dict(row)
            owner = await self.repo.owner(item["owner"])
            if owner is None:
                await self.repo.pause(item["batch"], "Reconnect to resume preparation.")
                return None
            try:
                await self._agent_check(item["creator"], item["owner"])
                auth = await self._connection(item["owner"], owner["username"])
                if owner["generation"] != item["generation"]:
                    raise PreparationError("Authorization changed. Resume to continue.")
                await self._agent_check(item["creator"], item["owner"])
            except (PreparationError, PlaylistError) as exc:
                await self.repo.pause(
                    item["batch"],
                    str(exc)
                    if isinstance(exc, PreparationError)
                    else "Your authorization could not be verified. Reconnect.",
                )
                return None
            await self.repo.progress(item["batch"], item["song_id"], "waiting")
            await self.repo.store.execute(
                "UPDATE audio_batches SET state='running' WHERE id=?", [item["batch"]]
            )
            self._active[item["owner"]] += 1
            self._last_owner = item["owner"]
            return item, auth

    async def _accessible(self, item: dict[str, Any], auth: Authorization) -> dict[str, Any]:
        await self._agent_check(item["creator"], item["owner"])
        owner = await self.repo.owner(auth.user_id)
        if (
            owner is None
            or owner["generation"] != item["generation"]
            or owner["authorization"] != auth.stored()
            or owner["library_id"] != item["library_id"]
            or item["library_id"] != self.client.native.library_id
        ):
            raise PreparationError("Authorization or library changed. Reconnect and resume.")
        await self.client.validate(auth)
        answer = await self.client.call(auth, "getSong", [("id", item["song_id"])])
        song = answer.get("song", {})
        native = await self.client.native.song(item["song_id"])
        if (
            not isinstance(song, dict)
            or song.get("id") != item["song_id"]
            or not native
            or native.get("libraryId") != item["library_id"]
        ):
            raise PreparationError("This song is unavailable in the configured library.")
        await self._agent_check(item["creator"], item["owner"])
        # Native reads may await while the owner connection is changed.
        fresh = await self.repo.owner(auth.user_id)
        if fresh != owner:
            raise PreparationError("Authorization or library changed. Reconnect and resume.")
        return native

    def _owned_file(self, song: dict[str, Any]) -> bool:
        path = song.get("path")
        if not isinstance(path, str) or not path or song.get("missing"):
            return False
        root = self.fetcher.engine.layout.library_root.resolve()
        file = (root / path).resolve()
        return file.is_relative_to(root) and file.is_file()

    async def _work(self, item: dict[str, Any], auth: Authorization) -> None:
        batch, song, owner = item["batch"], item["song_id"], item["owner"]
        began, stopped = False, False
        try:
            native = await self._accessible(item, auth)
            row = await self.fetcher.settled(song)
            if row is None:
                if not self._owned_file(native):
                    raise PreparationError("The library song's audio file is unavailable.")
            elif row["track_ref"] != item["track_ref"]:
                raise PreparationError("The snapshot's recording changed. Prepare a new snapshot.")
            else:

                async def progress(stage: str) -> None:
                    nonlocal began, stopped
                    if stage in ("preparing", "fetching", "verifying"):
                        await self._accessible(item, auth)
                    # Before its transfer starts - also while it waits again, for its turn
                    # or a paused add-on, after an attempt - a cancelled batch stops it.
                    if not began or stage == "waiting":
                        if await self.repo.state(batch, owner) not in ("running", "queued"):
                            stopped = True
                            raise _Stopped("Preparation cancelled before this song started.")
                        began = stage in ("preparing", "fetching", "verifying")
                    if stage in ACTIVE:
                        await self.repo.progress(batch, song, stage)

                # A background song: it waits for its turn and for paused add-ons.
                failure = await self.fetcher.ensure(
                    song, auth.username, bulk=True, progress=progress, background=True
                )
                if stopped:
                    raise _Stopped("Preparation cancelled before this song started.")
                if failure is not None:
                    raise PreparationError("Audio could not be prepared. Retry this song.")
            await self.repo.progress(batch, song, "available")
        except _Stopped:
            await self.repo.progress(batch, song, "paused")
        except _AgentStopped as exc:
            await self.repo.progress(batch, song, "paused")
            await self.repo.pause(batch, str(exc))
        except Exception as exc:
            log.warning("audio preparation song failed: %s", type(exc).__name__)
            error = (
                str(exc)
                if isinstance(exc, PreparationError)
                else "This song could not be verified or prepared."
            )
            await self.repo.progress(batch, song, "failed", error)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self.repo.progress(batch, song, "paused")
                await self.repo.pause(batch, "Preparation stopped. Resume to continue.")
            raise
        finally:
            self._active[owner] -= 1
            with anyio.CancelScope(shield=True):
                await self.repo.finish(batch)
            self.notify()
