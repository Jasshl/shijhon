"""Account attribution at verified request/publication boundaries, never on fetch-ahead."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

import aiosqlite
import anyio
import httpx

from shijhon.access import Principal
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, release_from_data
from shijhon.collection.contracts import Actor, CollectionError
from shijhon.collection.history import MAX_PENDING, History, queue_pending, recordable
from shijhon.collection.reader import CallerLibrary
from shijhon.collection.reasons import Reasons
from shijhon.placeholders.engine import MaterializeError
from shijhon.playlists.model import Track
from shijhon.playlists.resolve import native_track, same_recording
from shijhon.playlists.service import release_fingerprint
from shijhon.proxy.app import Forward, HandlerResult, RequestContext
from shijhon.proxy.auth import CREDENTIAL_PARAMS, subsonic_answer
from shijhon.proxy.params import RestCall, header
from shijhon.proxy.responses import callback_refused, subsonic_error
from shijhon.proxy.upstream import Receive, Send, Upstream, relay, request_headers
from shijhon.views.answers import LibraryAnswer, captured, raw_library_answer
from shijhon.views.commits import Commits
from shijhon.views.ids import CatalogId

log = logging.getLogger(__name__)
_MAX_TRACKS = 500
DRAIN_ITEMS = 100  # kept action items recorded in one transaction
DRAIN_SECONDS = 60.0  # kept actions are looked at again after this, at most
RETRY_SECONDS = 60.0  # a kept item that failed waits this long, doubling each time...
MAX_RETRY_SECONDS = 3600.0  # ...up to this
_MAX_ANSWER = 8 * 1024 * 1024
# Fetching audio (including a transcode decision) and saving a future queue never
# prove that its music was played. Explicitly removing a choice proves no addition.
_ACTIONS = {
    "download": ("id",),
    "star": ("id", "albumId"),
    "setRating": ("id",),
    "scrobble": ("id",),
    "reportPlayback": ("mediaId",),
    "createPlaylist": ("songId",),
    "updatePlaylist": ("songIdToAdd",),
    "createBookmark": ("id",),
    "createShare": ("id",),
}


def _actor(principal: Principal) -> Actor:
    return Actor(principal.user_id, frozenset(str(i) for i in principal.library_ids))


def _same_snapshot(left: CatalogTrack, right: CatalogTrack) -> bool:
    # Artwork may expire; recording identity, version, position and release may not.
    return replace(left, artwork_template=None) == replace(right, artwork_template=None)


def _matches(track: CatalogTrack, song: Mapping[str, Any]) -> bool:
    data = dict(song)
    # The pinned native API uses c/e and keeps ISRC in its tag map; REST uses
    # clean/explicit and a top-level ISRC. Normalize only these documented shapes.
    if data.get("explicitStatus") == "c":
        data["explicitStatus"] = "clean"
    elif data.get("explicitStatus") == "e":
        data["explicitStatus"] = "explicit"
    tags = data.get("tags")
    if not data.get("isrc") and isinstance(tags, dict):
        isrcs = tags.get("isrc")
        if isinstance(isrcs, list) and len(isrcs) == 1 and isinstance(isrcs[0], str):
            data["isrc"] = isrcs[0]
    wanted = {
        "artist": track.artist,
        "title": track.title,
        "duration": track.seconds,
        "isrc": track.isrc or "",
        "clean": track.clean,
        "explicit": track.explicit,
        "required_title": track.title,
    }
    return same_recording(wanted, native_track(data))


class CollectionRequests:
    """Optional Commits.requested and Playlists.published callbacks.

    Local ledger applicability is checked before credential work. Ordinary owned
    requests keep their native path, and request credentials are never persisted.
    """

    def __init__(self, commits: Commits, upstream: Upstream, history: History) -> None:
        self.commits, self.upstream, self.history = commits, upstream, history
        self.store = history.store
        self.library_id = str(commits.navidrome.library_id)
        # The account's favorites, for the albums they bring into its Original imported library.
        self.reasons: Reasons | None = None
        self._wake = anyio.Event()

    async def _tracks(
        self,
        values: Sequence[tuple[str, str]],
        *,
        db: aiosqlite.Connection | None = None,
        cap: int | None = _MAX_TRACKS,
    ) -> list[CatalogTrack] | None:
        """The catalog recordings the values name, from Shijhon's placeholder ledger; None
        when there are more than ``cap``."""
        found: dict[str, CatalogTrack] = {}
        releases: dict[str, dict[str, CatalogTrack]] = {}
        for key, value in values:
            cid = CatalogId.parse(value)
            if cid and cid.kind == "ar":
                continue
            song_ref = str(cid.ref) if cid and cid.kind == "tr" else ""
            album_ref = str(cid.ref) if cid and cid.kind == "al" else ""
            native = value if cid is None else ""
            sql = (
                "SELECT r.data,t.track_ref FROM releases r JOIN track_links t"
                " ON t.release_ref=r.ref WHERE"
                " (?<>'' AND t.track_ref=?) OR (?<>'' AND r.ref=?) OR"
                " (?<>'' AND t.owned=0 AND (t.song_id=? OR (? AND r.album_id=?))) LIMIT 501"
            )
            params = [
                song_ref,
                song_ref,
                album_ref,
                album_ref,
                native,
                native,
                key in ("id", "albumId"),
                native,
            ]
            if db is None:
                rows = await self.store.fetchall(sql, params)
            else:
                async with db.execute(sql, params) as cursor:
                    rows = list(await cursor.fetchall())
            for row in rows:
                data = str(row["data"])
                if data not in releases:
                    release = release_from_data(json.loads(data))
                    releases[data] = {str(t.ref): t for t in release.tracks}
                track = releases[data].get(str(row["track_ref"]))
                if track:
                    found[str(track.ref)] = track
                if cap is not None and len(found) > cap:
                    return None
        return list(found.values())

    def _read_call(self, original: RestCall, ident: str) -> RestCall:
        params = [(k, v) for k, v in original.params if k in CREDENTIAL_PARAMS or k in ("v", "c")]
        params += [("f", "json"), ("id", ident)]
        headers = [
            (k, v) for k, v in original.headers if k.lower() not in (b"range", b"content-length")
        ]
        return RestCall.build(
            "getSong", "GET", b"/rest/getSong", urlencode(params).encode(), headers, None
        )

    async def _verify(self, original: RestCall, tracks: Sequence[CatalogTrack]) -> bool:
        limiter = anyio.CapacityLimiter(4)
        accepted: list[bool] = []

        async def one(track: CatalogTrack) -> None:
            async with limiter:
                ident = await self.commits.materialized.song(track.ref)
                if not ident:
                    accepted.append(False)
                    return
                answer = await raw_library_answer(
                    self.upstream, self._read_call(original, ident), max_bytes=128 * 1024
                )
                parsed = await answer.parsed("song") if answer else None
                accepted.append(
                    bool(parsed and parsed[1].get("id") == ident and _matches(track, parsed[1]))
                )

        async with anyio.create_task_group() as group:
            for track in tracks:
                group.start_soon(one, track)
        return len(accepted) == len(tracks) and all(accepted)

    async def __call__(
        self, call: RestCall, ctx: RequestContext, result: HandlerResult
    ) -> HandlerResult:
        keys = _ACTIONS.get(call.name)
        favorite = call.name in ("star", "unstar") and self.reasons is not None
        if (keys is None and not favorite) or ctx.head or callback_refused(call):
            return result
        if favorite:
            return await self._favorite(call, ctx, result, keys or ())
        assert keys is not None
        if call.name == "setRating":
            try:
                if int(call.get("rating") or "0") <= 0:
                    return result
            except ValueError:
                return result
        if call.name == "reportPlayback" and (
            (call.get("state") or "").lower() not in ("starting", "playing")
            or (call.get("mediaType") or "song").lower() != "song"
        ):
            return result
        values = list(dict.fromkeys((k, v) for k, v in call.params if k in keys))
        if not values or len(values) > MAX_PENDING:
            return result
        # Up to 500 recordings are confirmed with the request itself; more are kept to
        # record from the ledger (``drain``), as one accepted action.
        tracks = await self._tracks(values) if len(values) <= _MAX_TRACKS else None
        if tracks == [] or await ctx.caller() is None:
            return result
        principal = self._principal(ctx)
        if principal is None:
            return result
        owner = principal.user_id

        async def record() -> None:
            if tracks is None:
                await self._queue(owner, values)
                return
            try:
                with anyio.fail_after(5):
                    if await self._verify(call, tracks):
                        await self.history.record_catalog(
                            _actor(principal), self.library_id, tracks
                        )
            except Exception as exc:
                # An accepted native mutation must never be sent a second time merely
                # because attribution failed. Keep the already captured native answer, and
                # the action to record later from the ledger.
                log.warning("collection request attribution failed: %s", type(exc).__name__)
                await self._queue(owner, values)

        forwarded = result.call if isinstance(result, Forward) else call
        if call.name == "download":

            async def downloaded(receive: Receive, send: Send) -> None:
                async def sending(message: Any) -> None:
                    if (
                        message["type"] == "http.response.start"
                        and message["status"] in (200, 206)
                        and header(message.get("headers", []), b"content-type").split(";", 1)[0]
                        in (
                            "audio/flac",
                            "audio/mpeg",
                            "audio/mp4",
                            "audio/aac",
                            "audio/ogg",
                            "audio/x-flac",
                            "audio/wav",
                            "audio/x-wav",
                            "application/octet-stream",
                        )
                    ):
                        await record()
                    await send(message)

                if result is not None and not isinstance(result, Forward):
                    await result(receive, sending)
                else:
                    response = await self.upstream.send(
                        forwarded.http_method,
                        forwarded.raw_path,
                        forwarded.query,
                        request_headers(forwarded.headers),
                        forwarded.body,
                    )
                    await relay(response, receive, sending)

            return downloaded
        answer = await self._answer(forwarded, result)
        if answer is None:
            # The upstream mutation may have happened; forwarding again is unsafe.
            return subsonic_error(
                call, 0, "The library request could not be verified.", self.commits.server()
            )
        if _accepted(answer):
            await record()
        return answer.reply()

    def _principal(self, ctx: RequestContext) -> Principal | None:
        """The verified caller, when it may read this library (after ``ctx.caller()``)."""
        principal = ctx.kept.get("shijhon.principal")
        if (
            not isinstance(principal, Principal)
            or self.library_id not in _actor(principal).library_ids
        ):
            return None
        return principal

    async def _answer(self, forwarded: RestCall, result: HandlerResult) -> LibraryAnswer | None:
        """Navidrome's answer to the request (a handler's own, or the request sent on)."""
        try:
            with anyio.fail_after(10):
                return (
                    await captured(result)
                    if result is not None and not isinstance(result, Forward)
                    else await raw_library_answer(self.upstream, forwarded, max_bytes=_MAX_ANSWER)
                )
        except (TimeoutError, ValueError):
            return None

    async def _favorite(
        self, call: RestCall, ctx: RequestContext, result: HandlerResult, keys: Sequence[str]
    ) -> HandlerResult:
        """A star or unstar: once Navidrome accepts it, the account's favorites change
        (``Reasons.favorited``), and a starred catalog song is attributed as any other
        request's."""
        assert self.reasons is not None
        if await ctx.caller() is None or (principal := self._principal(ctx)) is None:
            return result
        values = list(dict.fromkeys((k, v) for k, v in call.params if k in keys))[:MAX_PENDING]
        tracks = await self._tracks(values) if 0 < len(values) <= _MAX_TRACKS else None
        forwarded = result.call if isinstance(result, Forward) else call
        answer = await self._answer(forwarded, result)
        if answer is None:
            # The upstream mutation may have happened; forwarding again is unsafe.
            return subsonic_error(
                call, 0, "The library request could not be verified.", self.commits.server()
            )
        if _accepted(answer):
            try:
                await self.reasons.favorited(principal.user_id, forwarded)
            except Exception as exc:  # Navidrome's answer stands; the next read corrects
                log.warning("favorite not recorded: %s", type(exc).__name__)
            if values and tracks is None:
                await self._queue(principal.user_id, values)
            elif tracks:
                try:
                    with anyio.fail_after(5):
                        if await self._verify(call, tracks):
                            await self.history.record_catalog(
                                _actor(principal), self.library_id, tracks
                            )
                except Exception as exc:
                    log.warning("collection request attribution failed: %s", type(exc).__name__)
                    await self._queue(principal.user_id, values)
        return answer.reply()

    # --- accepted actions recorded later ------------------------------------------------

    async def pending(
        self, db: aiosqlite.Connection, owner: str, items: Sequence[tuple[str, str]]
    ) -> None:
        """Keep an accepted action of the verified ``owner`` to record, in the transaction
        that accepted it (a Downloads-page preparation's, for one). Call ``notify`` after
        it commits."""
        await queue_pending(db, owner, self.library_id, items)

    async def _queue(self, owner: str, items: Sequence[tuple[str, str]]) -> None:
        try:
            async with self.store.transaction() as db:
                await queue_pending(db, owner, self.library_id, items)
        except Exception as exc:
            log.warning("accepted action not kept to record: %s", type(exc).__name__)
            return
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        """Record the kept actions, at once when told of new ones (``notify``), else every
        minute. A start makes every kept item due once more (one more try each)."""
        try:
            async with self.store.transaction() as db:
                await db.execute(
                    "UPDATE collection_pending SET next_at=0 WHERE library_id=?",
                    [self.library_id],
                )
        except Exception as exc:
            log.warning("kept actions not made due: %s", type(exc).__name__)
        while True:
            self._wake = anyio.Event()
            try:
                await self.drain()
            except Exception as exc:
                log.warning("kept actions not recorded: %s", type(exc).__name__)
            with anyio.move_on_after(DRAIN_SECONDS):
                await self._wake.wait()

    async def drain(self) -> None:
        """Record the due kept actions as their accounts' activity, some at a time, each set
        in one transaction with its removal from the queue. Their recordings come from
        Shijhon's placeholder ledger; what is not a catalog recording records nothing. A set
        that fails is tried item by item, so the others go on; an item that fails waits,
        longer each time up to an hour (``next_at``), and is tried again then."""
        while True:
            now = time.time()
            head = await self.store.fetchone(
                "SELECT owner_id FROM collection_pending WHERE library_id=? AND next_at<=?"
                " ORDER BY next_at,queued_at LIMIT 1",
                [self.library_id, now],
            )
            if head is None:
                return
            owner = str(head["owner_id"])
            rows = await self.store.fetchall(
                "SELECT param,item FROM collection_pending WHERE owner_id=? AND library_id=?"
                " AND next_at<=? ORDER BY next_at,queued_at LIMIT ?",
                [owner, self.library_id, now, DRAIN_ITEMS],
            )
            items = [(str(r["param"]), str(r["item"])) for r in rows]
            try:
                await self._record_kept(owner, items)
            except Exception as exc:
                log.warning("kept actions not recorded: %s", type(exc).__name__)
                failed = 0
                for item in items:
                    try:
                        await self._record_kept(owner, [item])
                    except Exception:
                        failed += 1
                        await self._later(owner, item)
                log.warning("kept actions: %d of %d wait to be tried again", failed, len(items))

    async def _record_kept(self, owner: str, items: Sequence[tuple[str, str]]) -> None:
        """Record these kept items and take them out of the queue, in one transaction."""
        async with self.store.transaction() as db:
            tracks = await self._tracks(items, db=db, cap=None) or []
            kept = [track for track in tracks if recordable(track)]
            actor = Actor(owner, frozenset({self.library_id}))
            for start in range(0, len(kept), _MAX_TRACKS):
                await self.history.record_catalog(
                    actor, self.library_id, kept[start : start + _MAX_TRACKS], db=db
                )
            await db.executemany(
                "DELETE FROM collection_pending WHERE owner_id=? AND library_id=?"
                " AND param=? AND item=?",
                [(owner, self.library_id, param, item) for param, item in items],
            )
        if len(kept) < len(tracks):
            log.info("kept actions: %d recordings cannot be recorded", len(tracks) - len(kept))

    async def _later(self, owner: str, item: tuple[str, str]) -> None:
        """An item that failed: due again after a backoff that doubles, up to an hour."""
        async with self.store.transaction() as db:
            await db.execute(
                "UPDATE collection_pending SET attempts=attempts+1,"
                "next_at=?+min(?*(1<<min(attempts,6)),?) WHERE owner_id=? AND library_id=?"
                " AND param=? AND item=?",
                [time.time(), RETRY_SECONDS, MAX_RETRY_SECONDS, owner, self.library_id, *item],
            )

    async def published(
        self, principal: Principal, selected: list[Track], ids: list[str], db: aiosqlite.Connection
    ) -> None:
        # The publisher already verified exact native IDs, order and recordings using
        # this owner's authorization. Join its durable success transaction atomically.
        if len(selected) != len(ids):
            raise CollectionError("The published selection could not be verified.")
        for start in range(0, len(selected), _MAX_TRACKS):
            tracks = await self._tracks(
                [("songId", song.id) for song in selected[start : start + _MAX_TRACKS]], db=db
            )
            await self.history.record_catalog(
                _actor(principal), self.library_id, tracks or [], db=db
            )


def _accepted(answer: LibraryAnswer) -> bool:
    """Whether Navidrome answered "ok"."""
    response = httpx.Response(answer.status, headers=answer.headers)
    envelope = subsonic_answer(response, answer.body)
    return envelope is not None and envelope.get("status") == "ok"


class CollectionRestorer:
    """Restore only an approved Save's bounded, previously attributed catalog refs."""

    def __init__(self, commits: Commits, reader: CallerLibrary, history: History) -> None:
        self.commits, self.reader, self.history = commits, reader, history

    async def __call__(
        self,
        actor: Actor,
        library_id: str,
        refs: Sequence[CatalogRef],
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> Mapping[str, Mapping[str, Any]]:
        if len(refs) > 250 or library_id != str(self.reader.library_id):
            raise CollectionError("Restore at most 250 recordings from this library.")

        async def verify_access() -> None:
            if (
                await self.reader.native_actor(actor.owner_id, library_id) != actor
                or library_id not in actor.library_ids
            ):
                raise CollectionError("Your current library access changed. Retry this selection.")

        async def authorize_write() -> None:
            await verify_access()
            await authorize()

        await authorize_write()
        snapshots = await self.history.snapshots(actor, library_id, refs)
        approved: dict[str, CatalogRelease] = {}
        removed: set[str] = set()
        for track in snapshots.values():
            if track.album is None:
                continue
            ref = str(track.album)
            if ref not in approved:
                row = await self.history.store.fetchone(
                    "SELECT data FROM releases WHERE ref=?", [ref]
                )
                if row:
                    approved[ref] = release_from_data(json.loads(row["data"]))
                else:
                    old = await self.commits.engine.removed_record(ref)
                    if old:
                        record = json.loads(old["record"])
                        approved[ref] = release_from_data(json.loads(record["release"]["data"]))
                        removed.add(ref)
            release = approved.get(ref)
            saved = (
                next((t for t in release.tracks if t.ref == track.ref), None) if release else None
            )
            if saved is None or not _same_snapshot(saved, track):
                raise CollectionError(
                    "A saved recording changed. Its collection selection was kept."
                )

        def check(release: CatalogRelease) -> None:
            expected = approved.get(str(release.ref))
            if expected is None or release_fingerprint(expected) != release_fingerprint(release):
                raise MaterializeError("the release differs from the personal collection snapshot")

        result: dict[str, Mapping[str, Any]] = {}
        with (
            anyio.fail_after(120),
            self.commits.engine.guard_materializations(check),
            self.commits.engine.guard_writes(authorize_write),
        ):
            for ref in sorted(removed):
                await self.commits.engine.restore_release(ref)
            limiter = anyio.CapacityLimiter(4)

            async def verify(ref: str) -> None:
                wanted = snapshots.get(ref)
                if wanted is None or wanted.album is None or str(wanted.album) not in approved:
                    return
                async with limiter:
                    ident = await self.commits.native(CatalogId("tr", wanted.ref), "collection")
                    song = await self.reader.song(actor.owner_id, library_id, ident)
                    if not _matches(wanted, song):
                        raise CollectionError(
                            "A restored recording differs from your saved collection."
                        )
                    result[ref] = song

            async with anyio.create_task_group() as group:
                for ref in dict.fromkeys(str(r) for r in refs):
                    group.start_soon(verify, ref)
            await authorize_write()
        return result
