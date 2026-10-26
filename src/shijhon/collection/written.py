"""Songs Shijhon wrote into the library, added to the library index from apps' requests.

The library index (``index.py``) is refreshed in full only with a caller's native
bearer: **Refresh music**, a dashboard sign-in, or the native website (``SourceRefresh``).
Apps sign in with Subsonic credentials and never start that. So the placeholders that
commits, fills, playlist imports and restores write - each one's ID is in Shijhon's
own records (``placeholders``) once Navidrome lists it - would stay out of every
library list until someone opened those pages.

A verified app request that reads the library (``Visibility.answer``) therefore has
Shijhon look, at most every ``check_seconds`` and ``delay_seconds`` after the request
(which is answered first), for placeholders the index lacks (one local query). When
there are some, it reads those songs from
Navidrome as that account (Subsonic ``getSong`` with its request's credentials, never
kept; a few at a time, bounded) and adds them to the last complete snapshot in one
transaction, in its generation (``Index.add``), and to the facts of the lists cached for
it (``QueryIndex.extend``): those lists stay valid and are refreshed beside their next
requests, which they keep answering - no list is prepared again from nothing. One
addition runs at a time, at most one every ``gap_seconds``, so a bulk write is one
addition, not one per release. Their provenance
is Shijhon's own record of them, never the answer's (Subsonic paths are not the files').
Tags and roles that only Navidrome's native API describes come with the next full
refresh.

Deliveries and reverts keep a song's ID (a swap that would change it is undone), so
they need nothing here: lists read the audio's state as they answer.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from typing import Any
from urllib.parse import urlencode

import anyio

from shijhon.access import Principal
from shijhon.collection.index import Index
from shijhon.collection.query_index import QueryIndex, native_tag_id
from shijhon.proxy.auth import CREDENTIAL_PARAMS
from shijhon.proxy.params import RestCall
from shijhon.proxy.upstream import Upstream
from shijhon.store import Store
from shijhon.views.answers import LibraryAnswer, raw_library_answer

log = logging.getLogger(__name__)
CHECK_SECONDS = 10.0  # how often app requests look for songs the index lacks, at most
GAP_SECONDS = 60.0  # how long after one addition the next may start, at least
BATCH = 500  # songs read in one addition
PARALLEL = 4  # songs read at the same time
ADD_SECONDS = 120.0  # how long one addition may take
# How long after the request that starts it a check begins: that request is answered first.
DELAY_SECONDS = 2.0
SKIP_SECONDS = 1800.0  # a song Navidrome does not give is not asked for again before this

Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], bool]


class Written:
    def __init__(
        self,
        store: Store,
        index: Index,
        library_id: str,
        upstream: Upstream,
        spawn: Spawn,
        *,
        query_index: QueryIndex | None = None,
        check_seconds: float = CHECK_SECONDS,
        gap_seconds: float = GAP_SECONDS,
        delay_seconds: float = DELAY_SECONDS,
    ) -> None:
        self.store, self.index, self.library_id = store, index, library_id
        self.query_index = query_index
        self.upstream, self.spawn = upstream, spawn
        self.check_seconds, self.gap_seconds = check_seconds, gap_seconds
        self.delay_seconds = delay_seconds
        self._checked = -float("inf")  # when app requests last looked
        self._added = -float("inf")  # when the last addition began
        self._running = False
        self._skipped: dict[str, float] = {}  # song -> until when it is not asked for
        self.additions = 0  # observable in tests: additions that changed the index

    async def due(self, principal: Principal, call: RestCall) -> None:
        """A verified request of an account that may read this library reads its lists:
        add what Shijhon wrote since the last snapshot, when due (beside the request)."""
        now = time.monotonic()
        if self._running or now - self._checked < self.check_seconds:
            return
        self._checked = now
        if now - self._added < self.gap_seconds:
            self._checked = self._added + self.gap_seconds - self.check_seconds
            return
        if not principal.can_access(int(self.library_id)):
            return
        self._running = True
        read = _read_call(call)

        async def run() -> None:
            try:
                await anyio.sleep(self.delay_seconds)
                missing = await self._missing()
                if missing:
                    self._added = time.monotonic()
                    with anyio.fail_after(ADD_SECONDS):
                        await self._add(missing, read)
            except Exception as exc:
                log.warning("new library songs not added: %s", type(exc).__name__)
            finally:
                self._running = False

        if not self.spawn(run):
            self._running = False

    async def _missing(self) -> list[str]:
        """Placeholders Navidrome lists whose songs the last complete snapshot lacks."""
        state = await self.store.fetchone(
            "SELECT generation FROM collection_index WHERE library_id=?", [self.library_id]
        )
        if not state or not state["generation"]:
            return []  # no snapshot yet: the first one is prepared at a sign-in
        now = time.monotonic()
        self._skipped = {s: t for s, t in self._skipped.items() if t > now}
        rows = await self.store.fetchall(
            "SELECT p.song_id FROM placeholders p WHERE p.song_id<>''"
            " AND p.song_id NOT IN (SELECT value FROM json_each(?))"
            " AND NOT EXISTS (SELECT 1 FROM collection_tracks t WHERE t.library_id=?"
            " AND t.song_id=p.song_id AND t.present=1) ORDER BY p.song_id LIMIT ?",
            [json.dumps(sorted(self._skipped)), self.library_id, BATCH],
        )
        return [str(r["song_id"]) for r in rows]

    async def _add(self, missing: list[str], call: RestCall) -> None:
        """Read the songs as the caller and add those Navidrome gives. A song Navidrome
        says it does not have is not asked for again for a while; a refusal of the
        caller's credentials or access, or any other failure, stops the whole addition
        at once - nothing is held against the songs, and another request (another
        account's) may try again soon after a refusal."""
        songs: list[dict[str, Any]] = []
        limiter = anyio.CapacityLimiter(PARALLEL)
        stopped: list[str] = []

        async def one(ident: str, scope: anyio.CancelScope) -> None:
            async with limiter:
                if stopped:
                    return
                asked = _with_id(call, ident)
                answer = await raw_library_answer(self.upstream, asked, max_bytes=1 << 20)
                outcome, song = _outcome(answer, ident) if answer is not None else ("failed", None)
                if song is not None:
                    songs.append(native_shape(song))
                elif outcome == "missing":
                    self._skipped[ident] = time.monotonic() + SKIP_SECONDS
                else:
                    stopped.append(outcome)
                    scope.cancel()

        async with anyio.create_task_group() as group:
            for ident in missing:
                group.start_soon(one, ident, group.cancel_scope)
        if stopped:
            log.info("adding new library songs stopped: %s", stopped[0])
            if stopped[0] == "refused":
                self._added = -float("inf")  # the caller's: another may go on soon
        songs.sort(key=lambda s: str(s["id"]))
        generation, added = await self.index.add(self.library_id, songs)
        if added:
            self.additions += 1
            if self.query_index is not None:
                await self.query_index.extend(self.library_id, generation)


# Navidrome's Subsonic errors that refuse the caller - its credentials or its access -,
# not the song: the whole addition stops.
_REFUSALS = frozenset({10, 40, 41, 42, 43, 44, 50})


def _outcome(answer: LibraryAnswer, ident: str) -> tuple[str, dict[str, Any] | None]:
    """("song", the song) | ("missing", None): Navidrome does not have it (its error 70) |
    ("refused", None): the caller is refused | ("failed", None): anything else."""
    if answer.status != 200:
        return "failed", None
    try:
        envelope = json.loads(answer.body)["subsonic-response"]
    except (ValueError, KeyError, TypeError):
        return "failed", None
    if not isinstance(envelope, dict):
        return "failed", None
    if envelope.get("status") == "ok":
        song = envelope.get("song")
        if isinstance(song, dict) and str(song.get("id")) == ident:
            return "song", song
        return "failed", None
    error = envelope.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if code == 70:
        return "missing", None
    return ("refused" if code in _REFUSALS else "failed"), None


def native_shape(song: Mapping[str, Any]) -> dict[str, Any]:
    """A Subsonic song as the library index reads Navidrome's native songs: its structural
    fields, its album artist's ID and the roles and genre tags the Subsonic answer names.
    No path (Subsonic paths are not the files') and no annotations."""
    keys = (
        "id",
        "title",
        "album",
        "artist",
        "albumId",
        "artistId",
        "duration",
        "size",
        "year",
        "track",
        "discNumber",
        "bitRate",
        "suffix",
        "contentType",
        "coverArt",
        "genre",
        "musicBrainzId",
        "displayArtist",
        "displayAlbumArtist",
    )
    shaped: dict[str, Any] = {k: song[k] for k in keys if k in song}
    people = {
        role: [
            {k: str(v) for k, v in person.items() if k in ("id", "name")}
            for person in song.get(key) or []
            if isinstance(person, dict)
        ]
        for role, key in (("artist", "artists"), ("albumartist", "albumArtists"))
    }
    for contributor in song.get("contributors") or []:
        if isinstance(contributor, dict) and isinstance(contributor.get("artist"), dict):
            person = {k: str(v) for k, v in contributor["artist"].items() if k in ("id", "name")}
            if contributor.get("subRole"):
                person["subRole"] = str(contributor["subRole"])
            people.setdefault(str(contributor.get("role", "")), []).append(person)
    shaped["participants"] = {role: found for role, found in people.items() if role and found}
    if people["albumartist"]:
        shaped["albumArtistId"] = people["albumartist"][0].get("id", "")
        shaped["albumArtist"] = people["albumartist"][0].get("name", "")
    isrc = song.get("isrc")
    if isinstance(isrc, list) and isrc and isinstance(isrc[0], str):
        shaped["isrc"] = isrc[0]
    names = [
        str(g["name"]) for g in song.get("genres") or [] if isinstance(g, dict) and g.get("name")
    ]
    if names:
        shaped["genres"] = [{"id": native_tag_id("genre", n), "name": n} for n in names]
        shaped["tags"] = {"genre": names}
    return shaped


def _read_call(call: RestCall) -> RestCall:
    """The account's own request's credentials, for the songs read beside it."""
    params = [(k, v) for k, v in call.params if k in CREDENTIAL_PARAMS or k in ("v", "c")]
    params.append(("f", "json"))
    headers = [
        (k, v)
        for k, v in call.headers
        if k.lower() not in (b"range", b"content-length", b"content-type")
    ]
    return RestCall.build(
        "getSong", "GET", b"/rest/getSong", urlencode(params).encode(), headers, None
    )


def _with_id(call: RestCall, ident: str) -> RestCall:
    return RestCall.build(
        "getSong",
        "GET",
        b"/rest/getSong",
        urlencode([*call.params, ("id", ident)]).encode(),
        call.headers,
        None,
    )
