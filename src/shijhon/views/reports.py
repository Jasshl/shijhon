"""What clients report about their listening, kept for warm-ahead: the play queues
they save and the songs they report as playing (``scrobble`` with ``submission=false``,
``reportPlayback`` while starting or playing, a saved queue's current song). The request
then continues unchanged (commits, forwarding).

Only reports and queues with catalog songs or placeholders matter to warm-ahead (owned
songs need no add-on): others are forwarded without anything kept, as before. Keeping
this is work, so for those the caller's credentials are checked first (cached); with
warm-ahead off nothing is kept or checked.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from shijhon.delivery.listening import Listening
from shijhon.proxy.app import Handler, HandlerResult, RequestContext
from shijhon.proxy.params import RestCall
from shijhon.store import Store
from shijhon.views.ids import CatalogId

log = logging.getLogger(__name__)

REPORTS = ("scrobble", "reportPlayback", "savePlayQueue", "savePlayQueueByIndex")


class Reports:
    def __init__(
        self, store: Store, listening: Listening, *, enabled: Callable[[], bool] = lambda: True
    ) -> None:
        self.store = store
        self.listening = listening
        self.enabled = enabled  # warm-ahead is on (the setting can change at runtime)

    def wrap(self, method: str, inner: Handler | None) -> Handler:
        async def handle(call: RestCall, ctx: RequestContext) -> HandlerResult:
            if self.enabled():
                try:
                    await self._note(method, call, ctx)
                except Exception as exc:  # never in the way of the request itself
                    log.info("%s not noted: %s", method, type(exc).__name__)
            return await inner(call, ctx) if inner is not None else None

        return handle

    async def _note(self, method: str, call: RestCall, ctx: RequestContext) -> None:
        queue: list[str] | None = None
        playing: str | None = None
        index: int | None = None
        if method == "scrobble":
            if (call.get("submission") or "true").lower() == "false":
                playing = call.get("id")
        elif method == "reportPlayback":
            if (call.get("state") or "").lower() in ("starting", "playing") and (
                call.get("mediaType") or "song"
            ).lower() == "song":
                playing = call.get("mediaId")
        else:
            queue = call.getall("id")
            if method == "savePlayQueue":
                playing = call.get("current")
                index = queue.index(playing) if playing in queue else None
            else:
                try:
                    index = int(call.get("currentIndex") or "")
                except ValueError:
                    index = None
                valid = index is not None and 0 <= index < len(queue)
                playing = queue[index] if valid and index is not None else None
        if queue is None and not playing:
            return
        ids = [*(queue or []), *([playing] if playing else [])]
        keys, relevant = await self.keys(ids)
        if not relevant:
            return  # owned songs only: nothing for warm-ahead
        caller = await ctx.caller()
        if caller is None:
            return
        if queue is not None:
            self.listening.saved_queue(caller.username, [keys[i] for i in queue], index)
        if playing:
            self.listening.reported(caller.username, keys[playing])

    async def keys(self, ids: list[str]) -> tuple[dict[str, str], bool]:
        """Each ID's listening key - the catalog track for catalog songs and
        placeholders, else the native song ID - and whether any is one of those."""
        keys: dict[str, str] = {}
        natives: list[str] = []
        relevant = False
        for ident in ids:
            cid = CatalogId.parse(ident)
            if cid is not None:
                keys[ident] = str(cid.ref)
                relevant = True
            else:
                keys[ident] = ident
                natives.append(ident)
        unique = sorted(set(natives))
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = await self.store.fetchall(
                f"SELECT song_id, track_ref FROM placeholders WHERE song_id IN ({marks})",  # noqa: S608
                chunk,
            )
            for row in rows:
                keys[row["song_id"]] = row["track_ref"]
                relevant = True
        return keys, relevant
