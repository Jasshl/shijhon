"""Filling partially owned albums: an owned album that a catalog release
completes gets placeholders for its missing tracks, tagged from an owned file so that they
join the album - one album, whose owned tracks stay as they are.

**Complete on first view**: an owned album a matched release completes
is shown complete before it is filled - its missing tracks as catalog songs
(``views/complete.py``, from ``view``) - and filled on the first action on it or on one of
those songs (``first_use``, ``fill_album``: a play's report, a star, a rating, a playlist,
the saved queue's current song, a download...). A view never fills by itself, in JSON or
XML; an album the fill policy allows is filled automatically afterwards, in the paced
background queue - but not while the library pass is a dry run (``auto_fill`` off):
then views, searches, artist pages and syncs match albums and record what they would fill
as the dry run's plans, and only first use fills. A saved match answers a view at once,
and its plan is followed also while the catalog rests; an album never matched is matched
on its view, waiting up to a budget (the match then goes on in the background); a client
viewing many never-matched albums in a short time (a sync) gets the library's answers for
those, which are queued for background matching instead.

Priorities: albums a search result or an artist page shows are matched in the background,
one at a time with a pause in between, ahead of the paced pass over the whole library
(``library_pass.py``), which waits for them; a client whose pages show many albums in a
short time gets no more of them queued for a while.

Each album is matched once per catalog and region; the outcome is kept in
``album_matches``: ``filled``, ``complete``, ``review`` (the review list), ``none``,
``failed`` (tried again later, waiting twice as long each time) or ``kept`` (the owner
chose to keep it as it is). A dry run records what it would do as a plan (``planned``)
without writing anything; acting on the album later follows the plan without asking the
catalog again, unless the owned songs changed (their IDs, positions, titles, lengths or
totals). Albums that are catalog releases, and
albums already filled, are left alone - also after a switch to another catalog, whose
matches are kept apart (the scope): filled albums are reused, never refilled. A release
already in the library as another album goes to review.

The review list's actions: fill an album from a release the owner chose (refused when an
owned track is not on it, a new track would take an owned track's position, or the owned
files say the album is complete), keep it as it is, or match it again.

**Fill policy**: automatic fills - the library pass, albums a search, an
artist page or a view shows - only for albums of which the owner has at least ``min_songs``
songs (3) or a share (a quarter) of the release; the others are matched and kept as
``deferred``, with their plan: shown complete, and filled on first use. While the
library pass is a dry run nothing is filled automatically: every album a view, search,
artist page or sync matches is kept as ``deferred``.

**One release, one album**: owned files of one release can form several albums (a
song tagged "Album", five tagged "Album (Remastered)"). When a release fills an
album, the other owned albums of the same artist and title (edition words aside) whose
songs are all on it are its parts: the album with the closest title, then the most owned
songs, gets the fill - whatever order they are matched in - and the release's tracks owned
in the parts play the owned files there (owned-recording backing); the parts go to the
review list ("part of …").
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
import anyio

from shijhon.catalog.base import CatalogError
from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    release_data,
    release_from_data,
)
from shijhon.locks import KeyedLocks
from shijhon.matching.matcher import (
    Match,
    Matcher,
    Outcome,
    OwnedAlbum,
    OwnedSong,
    clashes,
    complete_by_tags,
    confirm,
    displaced,
)
from shijhon.matching.normalize import fold, same_artist, title_key
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.engine import MaterializeError, PlaceholderEngine
from shijhon.store import Store
from shijhon.views.bursts import Burst, Bursts, Client

log = logging.getLogger(__name__)
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
MAX_QUEUED = 200  # exposed albums waiting for a match
PER_ANSWER = 5  # albums of one search result or artist page queued
MAX_RETRY = 30 * 86400.0  # the longest wait before a failed album is tried again
IDLE_POLL = 0.5  # seconds: how often the library pass checks whether it is its turn
IN_LIBRARY = "the release is in the library as another album"  # a review reason
SHOWN_PLANS = 20_000  # plans kept read for album lists (a few hundred bytes each)
ANY_SHOWN_SECONDS = 10.0  # how long "no album is shown complete" is believed
_OUTCOMES = {
    Outcome.FILL: "filled",
    Outcome.COMPLETE: "complete",
    Outcome.REVIEW: "review",
    Outcome.NONE: "none",
}


@dataclass(frozen=True)
class FillPolicy:
    """Which albums are filled automatically: at least ``min_songs`` owned songs, or
    ``min_share`` of the release's tracks. ``min_songs`` 1: every album."""

    min_songs: int = 3
    min_share: float = 0.25

    def allows(self, owned: int, tracks: int) -> bool:
        return owned >= self.min_songs or (tracks > 0 and owned >= self.min_share * tracks)


@dataclass(frozen=True)
class Shown:
    """An owned album shown complete before its fill, as its list entries count it:
    the ``owned`` songs its plan links (Navidrome's song count while the plan fits) and the
    lengths (ms) of the release's ``missing`` tracks, which its view adds."""

    owned: int
    missing: tuple[int, ...]


class ChoiceRefused(Exception):
    """A release the owner chose from the review list cannot fill the album; ``reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Decision:
    """What happens to one album: from a match, or from a dry run's plan. ``outcome`` is
    ``filled`` (a release fills it; in a dry run: would fill it), ``complete``, ``review``,
    ``none`` or ``failed``."""

    outcome: str
    reason: str
    release: CatalogRelease | None = None
    links: dict[CatalogRef, str] = field(default_factory=dict)  # catalog track -> owned song
    candidates: list[str] = field(default_factory=list)
    release_ref: str | None = None
    # Tracks owned in other albums of the release (its parts): they play those files.
    backing: dict[CatalogRef, str] = field(default_factory=dict)
    parts: list[OwnedAlbum] = field(default_factory=list)
    planned: bool = False  # recorded as a dry run's plan (automatic fills are off)

    @classmethod
    def of(cls, match: Match) -> Decision:
        ref = str(match.release.ref) if match.release is not None else None
        outcome = _OUTCOMES[match.outcome]
        return cls(outcome, match.reason, match.release, dict(match.links), match.candidates, ref)

    @property
    def added(self) -> int:
        """Placeholders a fill creates."""
        return len(self.release.tracks) - len(self.links) if self.release is not None else 0

    @property
    def tracks(self) -> int:
        return len(self.release.tracks) if self.release is not None else 0

    @property
    def owned(self) -> int:
        """The release's tracks the owner has: in the album and in its parts."""
        return len(self.links) + len(self.backing)

    @property
    def owned_text(self) -> str:
        """The owned tracks as log lines and reasons count them: "5 owned + 1 from another
        album, of 17" (a part's song is owned, but not in this album)."""
        text = f"{len(self.links)} owned"
        if self.backing:
            others = "another album" if len(self.backing) == 1 else "other albums"
            text += f" + {len(self.backing)} from {others}"
        return f"{text}, of {self.tracks}"

    @property
    def deferred(self) -> bool:
        return self.outcome == "deferred"


class Fills:
    def __init__(
        self,
        store: Store,
        navidrome: NavidromeService,
        engine: PlaceholderEngine,
        matcher: Matcher,
        *,
        scope: str,
        budget_seconds: float = 3.0,
        retry_seconds: float = 3600.0,
        spawn: Spawn | None = None,
        syncs: Bursts | None = None,
        exposures: Bursts | None = None,
        pause_seconds: float = 5.0,
        rest_seconds: float = 60.0,
        policy: FillPolicy | None = None,
        auto_fill: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.navidrome = navidrome
        self.engine = engine
        self.matcher = matcher
        self.scope = scope  # catalog and region the matches belong to
        self.budget = budget_seconds
        self.retry_seconds = retry_seconds
        self.spawn = spawn
        self.clock = clock
        self.syncs = syncs  # clients opening many unmatched albums: syncing
        self.exposures = exposures or Bursts(20, 60.0)  # clients shown many albums
        self.pause = pause_seconds  # between background matches (about 1 request/s)
        self.rest_seconds = rest_seconds
        self.policy = policy or FillPolicy()
        # Automatic fills at all (views, searches, artist pages, syncs, the pass): off while
        # the library pass is a dry run - only first use fills then.
        self.auto_fill = auto_fill
        self.resting_until = 0.0  # after the catalog failed
        self._rest_noted = 0.0  # the rest a view's log line named
        self._locks = KeyedLocks()
        self._running: dict[str, anyio.Event] = {}
        self._queue: list[str] = []
        self._queued: set[str] = set()
        self._working = False
        self._any_shown: tuple[float, bool] | None = None  # (checked at, any album shown)
        # The plans of albums shown complete in lists, by row: the most recently listed kept.
        self._shown_plans: OrderedDict[tuple[str, float], Shown | None] = OrderedDict()
        self.fills = 0  # observable in tests

    # --- views and first use ------------------------------------------------------

    async def viewable(self, album_id: str) -> bool:
        """Whether a view of this album may have something to add (no work, one or two
        small queries): not a catalog album, not filled, and a plan that fills it (checked
        against the owned songs then), no match yet, or a failure whose wait is over. A dry
        run's other plans (complete, review, no match) add nothing: a view leaves them to
        the library pass."""
        if not album_id or "." in album_id or await self._filled(album_id):
            return False
        row = await self._row(album_id)
        return row is None or _has_plan(row) or self._retry_due(row)

    async def view(self, album_id: str, client: Client) -> tuple[Decision | None, str | None]:
        """(What to show the owned album complete with, why not): the release whose missing
        tracks are shown as catalog songs, or None - Navidrome's answer as it is - with
        the reason worth a log line, if any. Never fills; an album the fill policy allows
        is filled in the background afterwards. The caller checked the credentials."""
        row = await self._row(album_id)
        if row is not None and _has_plan(row):
            decision = await self._shown(album_id)
            if decision is not None or not await self._candidate(album_id, mind_rest=False):
                return decision, None
        if not await self._candidate(album_id):
            if self.resting and self._rest_noted != self.resting_until:
                self._rest_noted = self.resting_until  # one line a rest
                return None, "the catalog is resting after failing"
            return None, None
        if self.syncs is not None:
            burst = self.syncs.note(client, album_id)
            if burst is Burst.STARTED:
                log.info(
                    "albums: %s viewed %d never-matched albums within %gs (a sync?);"
                    " matching those in the background",
                    _printable(client[1]) or "a client",
                    self.syncs.count(client),
                    self.syncs.window,
                )
            if burst is not Burst.NO:
                self._enqueue([album_id])
                return None, None  # logged once per sync
        if self.spawn is None:  # no background: the answer waits for the whole match
            await self.fill(album_id, hold=True)
        else:
            done = self._start(album_id, hold=True)
            with anyio.move_on_after(self.budget):
                await done.wait()
            if not done.is_set():
                return None, f"no match within {self.budget:g}s (the match goes on)"
        decision = await self._shown(album_id)
        if decision is None:
            failed = await self._row(album_id)
            if failed is not None and failed["outcome"] == "failed":
                return None, f"the match failed ({failed['reason']})"
        return decision, None

    async def _shown(self, album_id: str) -> Decision | None:
        """The album's saved plan, when it still fits the owned songs and a release fills
        it; an album the fill policy allows is then filled in the background (not while
        the library pass is a dry run)."""
        owned = await self._owned(album_id)
        if owned is None:
            return None
        decision = await self._planned(album_id, owned)
        if decision is None or decision.outcome != "filled" or decision.release is None:
            return None
        if self.auto_fill and self.policy.allows(decision.owned, decision.tracks):
            self._enqueue([album_id])  # filled automatically, in the paced queue
        return decision

    async def owner(self, release_ref: str) -> str | None:
        """The owned album a matched release is shown with and not filled yet - also
        when its fill failed (the plan is kept): the release's songs never become a second,
        catalog copy of it."""
        rows = await self.store.fetchall(
            "SELECT album_id, outcome, planned, plan FROM album_matches"
            " WHERE scope = ? AND release_ref = ? AND plan IS NOT NULL",
            [self.scope, release_ref],
        )
        for row in rows:
            owns = _has_plan(row) or row["outcome"] == "failed"
            if owns and not await self._filled(str(row["album_id"])):
                return str(row["album_id"])
        return None

    async def pending(self, album_id: str) -> bool:
        """Whether an owned album is shown complete but not filled yet (no work)."""
        if not album_id or "." in album_id:
            return False
        row = await self._row(album_id)
        return row is not None and _has_plan(row) and not await self._filled(album_id)

    async def any_shown(self) -> bool:
        """Whether any owned album may be shown complete (a list has nothing to count
        otherwise): one small query, reused a few seconds and redone after a match."""
        now = self.clock()
        if self._any_shown is None or now - self._any_shown[0] > ANY_SHOWN_SECONDS:
            row = await self.store.fetchone(
                "SELECT 1 FROM album_matches WHERE scope = ? AND plan IS NOT NULL"
                " AND (outcome = 'deferred' OR (planned = 1 AND outcome = 'filled')) LIMIT 1",
                [self.scope],
            )
            self._any_shown = (now, row is not None)
        return self._any_shown[1]

    async def shown(self, album_ids: Iterable[str]) -> dict[str, Shown]:
        """The owned albums among ``album_ids`` shown complete and not filled yet, for
        album lists: one small query per 500 albums, and their plans read once (kept by row).
        The plan is not checked against the owned songs (that needs Navidrome): the caller
        compares counts."""
        wanted = sorted({a for a in album_ids if a and "." not in a})
        found: dict[str, Shown] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            rows = await self.store.fetchall(
                "SELECT album_id, checked_at FROM album_matches m"  # noqa: S608
                f" WHERE scope = ? AND album_id IN ({','.join('?' * len(chunk))})"
                " AND plan IS NOT NULL"
                " AND (outcome = 'deferred' OR (planned = 1 AND outcome = 'filled'))"
                " AND NOT EXISTS (SELECT 1 FROM releases WHERE album_id = m.album_id)"
                " AND NOT EXISTS (SELECT 1 FROM releases WHERE owned_album_id = m.album_id)",
                [self.scope, *chunk],
            )
            keys = [(str(r["album_id"]), float(r["checked_at"])) for r in rows]
            unread = [album for album, at in keys if (album, at) not in self._shown_plans]
            if unread:
                plans = await self.store.fetchall(
                    "SELECT album_id, checked_at, plan FROM album_matches"  # noqa: S608
                    f" WHERE scope = ? AND album_id IN ({','.join('?' * len(unread))})",
                    [self.scope, *unread],
                )
                for row in plans:
                    key = (str(row["album_id"]), float(row["checked_at"]))
                    self._shown_plans[key] = _shown(str(row["plan"] or ""))
            for key in keys:
                if key not in self._shown_plans:
                    continue  # changed between the two queries: counted next time
                self._shown_plans.move_to_end(key)
                if (plan := self._shown_plans[key]) is not None:
                    found[key[0]] = plan
            while len(self._shown_plans) > SHOWN_PLANS:
                self._shown_plans.popitem(last=False)
        return found

    async def backed(self, release_ref: str, track: CatalogRef) -> str | None:
        """The owned song (of another album of the release) that plays for a track of
        a release shown with an owned album before its fill."""
        album_id = await self.owner(release_ref)
        row = await self._row(album_id) if album_id else None
        try:
            plan = json.loads(row["plan"]) if row is not None and row["plan"] else {}
            song = plan.get("backing", {}).get(str(track))
        except (ValueError, TypeError, AttributeError):
            return None
        return str(song) if song else None

    async def first_use(self, release_ref: str, trigger: str) -> str | None:
        """The release's tracks were used (``trigger``: the request): the owned album it
        is shown with is filled now. Its album ID, or None when no owned album waits for
        this release."""
        album_id = await self.owner(release_ref)
        if album_id is not None:
            await self.fill_album(album_id, trigger)
        return album_id

    async def fill_album(self, album_id: str, trigger: str) -> Decision | None:
        """Fill an owned album shown complete, on its first use."""
        decision = await self.fill(album_id, trigger=trigger)
        if decision is not None and decision.outcome == "failed":
            log.info("fill on first use (%s) failed: %s", trigger, decision.reason)
        return decision

    def exposed(self, client: tuple[str, str], album_ids: Iterable[str]) -> None:
        """Albums a search result or an artist page shows to ``client``: the first few are
        matched in the background, in the order they came, ahead of the (later) library
        pass - unless the client has been shown many in a short time (walking pages)."""
        wanted = [a for a in album_ids if a and "." not in a][:PER_ANSWER]
        kept = [a for a in wanted if self.exposures.note(client, a) is Burst.NO]
        self._enqueue(kept)

    def _enqueue(self, album_ids: Iterable[str]) -> None:
        if self.spawn is None:
            return
        for album_id in album_ids:
            if album_id not in self._queued and len(self._queue) < MAX_QUEUED:
                self._queue.append(album_id)
                self._queued.add(album_id)
        if self._queue and not self._working:
            self._working = True
            self.spawn(self._work)

    async def _work(self) -> None:
        """One background match at a time, with a pause after each that asked the
        catalog: background matching stays near one request a second."""
        try:
            while self._queue:
                album_id = self._queue.pop(0)
                try:
                    if await self._candidate(album_id, auto=True):
                        await self._start(album_id, auto=True).wait()
                        if await self._candidate(album_id, auto=True):
                            # A view's match (which fills nothing) had the album meanwhile.
                            await self._start(album_id, auto=True).wait()
                        await anyio.sleep(self.pause)
                except Exception as exc:  # the next album still gets its turn
                    log.warning("background match failed: %s", type(exc).__name__)
                finally:
                    self._queued.discard(album_id)
        finally:
            self._working = False

    def _start(self, album_id: str, *, auto: bool = False, hold: bool = False) -> anyio.Event:
        """The album's fill, started once (in the background when possible). ``auto``: an
        automatic fill (the fill policy applies); ``hold``: a view's match, which fills
        nothing."""
        running = self._running.get(album_id)
        if running is not None:
            return running
        done = self._running[album_id] = anyio.Event()

        async def run() -> None:
            try:
                decision = await self.fill(album_id, auto=auto, hold=hold)
                if (
                    hold
                    and self.auto_fill
                    and decision is not None
                    and decision.deferred
                    and self.policy.allows(decision.owned, decision.tracks)
                ):
                    self._enqueue([album_id])  # filled automatically, paced
            except Exception as exc:  # recorded as failed by fill(); never in the way
                log.warning("fill of an album failed: %s", type(exc).__name__)
            finally:
                self._running.pop(album_id, None)
                done.set()

        assert self.spawn is not None
        self.spawn(run)
        return done

    # --- the library pass's turn ------------------------------------------------------------

    async def wait_idle(self) -> None:
        """Until the pass may go on: opened and shown albums go first, and nothing is asked
        while the catalog rests after failing."""
        # Several conditions, one of them a time: checked twice a second.
        while self._busy():  # noqa: ASYNC110
            await anyio.sleep(IDLE_POLL)

    def _busy(self) -> bool:
        return bool(self._queue or self._working or self._running) or self.resting

    @property
    def resting(self) -> bool:
        """The catalog failed a moment ago: nothing asks it for a while."""
        return self.clock() < self.resting_until

    # --- one album ---------------------------------------------------------------------------

    async def _candidate(
        self,
        album_id: str,
        *,
        dry_run: bool = False,
        mind_rest: bool = True,
        auto: bool = False,
        songs: int = 0,
    ) -> bool:
        """Worth matching: not a catalog release, not filled, not matched (a failure is
        tried again after a while); not while the catalog rests after failing (unless
        ``mind_rest`` is off: the caller waits for the rest itself). A dry run's plan is
        acted on (``dry_run``: it is already planned). A deferred album is filled on first
        use (not ``auto``), and automatically (``auto``) only once the fill policy allows
        it (``songs``: how many it has now, when the caller knows). With automatic fills off
        (a dry run of the library pass) nothing saved is acted on automatically: only new
        matches and failures whose wait is over."""
        if not album_id or "." in album_id or (mind_rest and self.resting):
            return False  # nothing, a catalog ID, or resting
        if await self._filled(album_id):
            return False
        row = await self._row(album_id)
        if row is None:
            return True
        if auto and row["cleaned_at"] is not None:
            return False  # its fill was taken out as unused: its next use fills it
        if row["outcome"] == "deferred":
            owned = max(int(row["owned_songs"]), songs)
            allowed = self.auto_fill and self.policy.allows(owned, int(row["release_tracks"]))
            return not dry_run and (not auto or allowed)
        if row["outcome"] == "failed":
            return self._retry_due(row)
        return bool(row["planned"]) and not dry_run and (not auto or self.auto_fill)

    async def _filled(self, album_id: str) -> bool:
        """Filled already, or a catalog album."""
        found = await self.store.fetchone(
            "SELECT 1 FROM releases WHERE album_id = ? OR owned_album_id = ?", [album_id, album_id]
        )
        return found is not None

    async def _row(self, album_id: str) -> Any:
        return await self.store.fetchone(
            "SELECT outcome, reason, attempts, checked_at, planned, plan, owned_songs,"
            " release_tracks, cleaned_at FROM album_matches WHERE album_id = ? AND scope = ?",
            [album_id, self.scope],
        )

    def _retry_due(self, row: Any) -> bool:
        """A failed match whose wait (doubling with each failure) is over."""
        if row["outcome"] != "failed":
            return False
        wait = min(MAX_RETRY, self.retry_seconds * 2 ** max(0, int(row["attempts"]) - 1))
        return bool(self.clock() - float(row["checked_at"]) > wait)

    async def would_fill(self, albums: set[str] | None = None) -> int:
        """Albums with a plan that an automatic fill would fill now: a dry run's plans and
        albums kept to fill on first use, that the fill policy allows now (it may have
        changed since they were matched) - the pass switched on fills them, a view while it
        is off. Not those whose fill the cleanup took out, nor ones no longer in the
        library (``albums``: those it has)."""
        rows = await self.store.fetchall(
            "SELECT album_id, owned_songs, release_tracks FROM album_matches WHERE scope = ?"
            " AND (outcome = 'deferred' OR (outcome = 'filled' AND planned = 1))"
            " AND cleaned_at IS NULL AND album_id NOT IN (SELECT owned_album_id FROM releases"
            " WHERE owned_album_id IS NOT NULL)",  # filled already (another catalog's)
            [self.scope],
        )
        return sum(
            1
            for row in rows
            if (albums is None or row["album_id"] in albums)
            and self.policy.allows(int(row["owned_songs"] or 0), int(row["release_tracks"] or 0))
        )

    async def due(self, album_id: str, *, dry_run: bool = False, songs: int = 0) -> bool:
        """Whether the library pass has anything to do for this album (a rest of the
        catalog aside: the pass waits for it)."""
        return await self._candidate(
            album_id, dry_run=dry_run, mind_rest=False, auto=True, songs=songs
        )

    async def fill(
        self,
        album_id: str,
        *,
        matcher: Matcher | None = None,
        dry_run: bool = False,
        navidrome_errors: bool = True,
        auto: bool = False,
        hold: bool = False,
        trigger: str | None = None,
    ) -> Decision | None:
        """Match the album and fill it if a release completes it; the outcome is kept. A
        dry run only records what it would do. None: nothing to do (not a candidate, or not
        an owned album). ``navidrome_errors`` off: a Navidrome error is raised rather than
        recorded as the album's failure (the library pass stops instead). ``auto``: an
        automatic fill, which the fill policy may defer to the album's first use; ``hold``:
        a view's match, kept as ``deferred`` whatever the policy (the view then starts an
        automatic fill if the policy allows it). ``trigger``: what the fill is for (logged).

        The owned albums of one release (the same artist and title apart from edition
        words) are handled together, one at a time: the release comes from the one
        with the most songs, and fills the one with the closest title."""
        try:
            group = await self._group_key(album_id)
        except NavidromeError:
            if not navidrome_errors:
                raise
            group = f"album:{album_id}"
        async with self._locks.hold(group), self._locks.hold(album_id):
            # A saved plan is followed also while the catalog rests (it needs no catalog
            # request); a new match waits for the rest to end.
            if not await self._candidate(album_id, dry_run=dry_run, auto=auto, mind_rest=False):
                return None
            owned: OwnedAlbum | None = None
            target: OwnedAlbum | None = None
            try:
                owned = await self._owned(album_id)
                if owned is None:
                    return None  # not an owned album (or no longer there)
                target = owned
                part = await self._part_of(owned)
                if part is not None:
                    # Its songs are on a filled album's release: matched again (the
                    # review list's "Match again"), another edition of that release would
                    # put the same songs in two albums.
                    as_part = Decision("review", part[0], None, {}, [], None)
                    await self._record(album_id, as_part, owned, planned=dry_run)
                    return as_part
                decision = None if dry_run else await self._planned(album_id, owned)
                if decision is None:
                    if self.resting:
                        return None
                    target, decision = await self._match_group(owned, matcher or self.matcher)
                    if auto and target.id != album_id:
                        chosen = await self._row(target.id)
                        if chosen is not None and chosen["cleaned_at"] is not None:
                            # The release's album is one whose fill the cleanup took out:
                            # only its own use fills it again, never automatically.
                            # This album is its part (not matched again at each occasion).
                            held = Decision("deferred", "", release_ref=decision.release_ref)
                            held.parts = decision.parts
                            await self._record_parts(target, held, planned=dry_run)
                            return None
                if (
                    decision.outcome == "filled"
                    and decision.release is not None
                    and decision.release.incomplete
                ):
                    # A track of it could not be read: filling would leave a gap for good.
                    decision.outcome = "review"
                    decision.reason = "the catalog's track list could not be read in full"
                if decision.outcome == "filled" and await self._in_library(decision.release_ref):
                    # Committed as a catalog album (or filling another owned album):
                    # filling this one too would show the release twice.
                    decision.outcome = "review"
                    decision.reason = IN_LIBRARY
                if decision.outcome == "filled":
                    allowed = self.policy.allows(decision.owned, decision.tracks)
                    if allowed and not self.auto_fill and not dry_run and (hold or auto):
                        # While the pass is a dry run: recorded as its plan (it would fill
                        # the album), shown complete, filled on first use.
                        decision.planned = True
                    elif hold or (auto and not allowed):
                        decision.outcome = "deferred"
                        decision.reason = f"{decision.owned_text}: " + (
                            "filled automatically" if allowed
                            else "shown complete, filled on first use"
                        )  # fmt: skip
                planned = dry_run or decision.planned
                if decision.outcome == "filled" and not planned:
                    await self._materialize(target.id, target, decision, trigger)
                elif decision.outcome == "review" and not planned:
                    log.info(
                        "album match needs review: %s (%d owned): %s",
                        _named(target),
                        len(target.songs),
                        decision.reason,
                    )
            except (CatalogError, NavidromeError, MaterializeError) as exc:
                if isinstance(exc, NavidromeError) and not navidrome_errors:
                    raise
                reason = getattr(exc, "reason", None) or str(exc)
                if isinstance(exc, CatalogError) and exc.kind != "not_found":
                    self.resting_until = self.clock() + self.rest_seconds
                named = f" for {_named(owned)}" if owned is not None else ""
                log.info("album match failed%s: %s", named, reason)
                await self._record(album_id, Decision("failed", reason), owned, planned=dry_run)
                return Decision("failed", reason)
            planned = dry_run or decision.planned
            await self._record(target.id, decision, target, planned=planned)
            if decision.outcome in ("filled", "deferred", "complete"):
                await self._record_parts(target, decision, planned=planned)
            return decision

    async def _materialize(
        self, album_id: str, owned: OwnedAlbum, decision: Decision, trigger: str | None = None
    ) -> None:
        assert decision.release is not None
        started = time.monotonic()
        ref = str(decision.release.ref)
        # One fill of a release at a time - the review list's choice, the pass, a view's fill
        # and a commit of it alike (the release's lock): the second sees the first's result.
        async with self.engine.lock_for(ref):
            if await self._in_library(ref):
                decision.outcome = "review"
                decision.reason = IN_LIBRARY
                log.info("album match needs review: %s (%d owned): %s (filled meanwhile)",
                         _named(owned), len(owned.songs), IN_LIBRARY)  # fmt: skip
                return
            result, planned, linked = await self.engine.materialize_held(
                decision.release, owned_album_id=album_id, links=decision.links
            )
        await self.engine.back_placeholders(decision.release, result, planned, linked)
        self.fills += 1
        if result.album_id != album_id:
            decision.outcome = "review"
            decision.reason = "the placeholders did not join the album"
        backed = 0
        for track, song in decision.backing.items():
            placeholder = result.created.get(track)
            if placeholder is not None:  # a part's owned file plays for it
                await self.engine.set_backing(placeholder, song)
                backed += 1
        playing = ""
        if backed:
            songs = "an owned song" if backed == 1 else "owned songs"
            playing = f", {backed} playing {songs} of another album"
        log.info(
            "filled %s (%s) with %d placeholder(s)%s from %s in %.1fs%s",
            _named(owned),
            decision.owned_text,
            len(result.created),
            playing,
            decision.release.ref,
            time.monotonic() - started,
            f" (first use: {trigger})" if trigger else "",
        )

    # --- one release, one album ------------------------------------------------------

    async def _group_key(self, album_id: str) -> str:
        """The lock of the album's group: its artist and title apart from edition words
        (never the album's own lock, which is held with it)."""
        try:
            album = await self.navidrome.album(album_id)
        except NavidromeError as exc:
            if exc.code == 70:
                return f"album:{album_id}"  # not in Navidrome: nothing to group
            raise
        artist = fold(str(album.get("artist") or album.get("displayArtist") or ""))
        return f"group:{artist}:{title_key(str(album.get('name') or ''))}"

    async def _match_group(
        self, owned: OwnedAlbum, matcher: Matcher
    ) -> tuple[OwnedAlbum, Decision]:
        """(The album the match concerns, the decision.) With other owned albums of the same
        release, the one with the most songs (the anchor) is matched; its release fills
        the album of the group with the closest title (then the most songs), the others'
        songs playing for their tracks there, and they become its parts. Otherwise the
        album is matched as it is."""
        others = await self._group(owned)
        if not others:
            return owned, Decision.of(await matcher.match(owned))
        members = [owned, *others]
        anchor = max(members, key=lambda o: (len(o.songs), o.id))
        decision = Decision.of(await matcher.match(anchor))
        release = decision.release
        if decision.outcome not in ("filled", "complete") or release is None:
            if anchor.id == owned.id:
                return owned, decision
            return owned, Decision.of(await matcher.match(owned))  # nothing shared found
        confirmed = [(anchor, decision.links)] + [
            (m, links)
            for m in members
            if m.id != anchor.id and (links := confirm(m, release)) is not None
        ]
        if owned.id not in {m.id for m, _ in confirmed}:
            return owned, Decision.of(await matcher.match(owned))  # not part of the release
        if decision.outcome == "complete":  # the anchor owns every track: the rest are parts
            decision.parts = [m for m, _ in confirmed if m.id != anchor.id]
            return anchor, decision
        best, links = max(confirmed, key=lambda c: (_closeness(c[0].title, release.title),
                                                    len(c[0].songs), c[0].id))  # fmt: skip
        if (
            clashes(best, release, links)
            or complete_by_tags(best)
            # (A release without track numbers of its own: only an album numbered like its
            # order is filled - the anchor is, the matcher saw to it.)
            or (not release.numbered and displaced(best, release, links))
        ):
            best, links = anchor, decision.links
        decision.links = dict(links)
        decision.backing = {}
        for member, member_links in confirmed:
            if member.id == best.id:
                continue
            for track, song in member_links.items():
                if track not in decision.links and track not in decision.backing:
                    decision.backing[track] = song
        decision.parts = [m for m, _ in confirmed if m.id != best.id]
        return best, decision

    async def _group(self, owned: OwnedAlbum) -> list[OwnedAlbum]:
        """The other owned albums of the same album artist and title (edition words aside):
        filled ones, catalog albums and those the owner keeps as they are aside."""
        wanted = title_key(owned.title)
        answer = await self.navidrome.subsonic(
            "search3",
            [("query", owned.artist or owned.title), ("albumCount", "500"),
             ("artistCount", "0"), ("songCount", "0")],
        )  # fmt: skip
        found = []
        for album in answer.get("searchResult3", {}).get("album", []):
            ident = str(album.get("id") or "")
            if (
                not ident
                or ident == owned.id
                or title_key(album.get("name")) != wanted
                or not same_artist(album.get("artist"), owned.artist)
            ):
                continue
            excluded = await self.store.fetchone(
                "SELECT 1 FROM releases WHERE album_id = ? OR owned_album_id = ?"
                " UNION SELECT 1 FROM album_matches WHERE album_id = ? AND scope = ?"
                " AND outcome = 'kept'",
                [ident, ident, ident, self.scope],
            )
            other = None if excluded else await self._owned(ident)
            if other is not None and other.songs:
                found.append(other)
        return found

    async def _record_parts(self, owner: OwnedAlbum, decision: Decision, *, planned: bool) -> None:
        how = {"filled": "filled there", "deferred": "filled with it on first use"}.get(
            decision.outcome, "complete there"
        )
        reason = f"part of {_named(owner)} (album {owner.id}): {how}"
        for part in decision.parts:
            row = await self.store.fetchone(
                "SELECT outcome, planned FROM album_matches WHERE album_id = ? AND scope = ?",
                [part.id, self.scope],
            )
            if row is not None and (row["outcome"] == "kept" or (planned and not row["planned"])):
                continue  # the owner's choice, or a real outcome a dry run leaves alone
            part_decision = Decision(
                "review", reason, None, {}, [str(decision.release_ref)], decision.release_ref
            )
            await self._record(part.id, part_decision, part, planned=planned)
            if not planned:
                log.info("album match needs review: %s (%d owned): %s",
                         _named(part), len(part.songs), reason)  # fmt: skip

    async def _part_of(self, owned: OwnedAlbum) -> tuple[str, str] | None:
        """(the part reason, the album's name) of an owned album that is a part of a filled
        album's release: the filled album is of the same artist and title,
        edition words aside, and every song of this album is on its release. Else None - a
        single or an edition with songs of its own is no part, whichever was filled first."""
        rows = await self.store.fetchall(
            "SELECT r.owned_album_id, r.title, r.artist, r.data, m.title AS owner_title,"
            " m.artist AS owner_artist FROM releases r LEFT JOIN album_matches m"
            " ON m.album_id = r.owned_album_id AND m.scope = ?"
            " WHERE r.owned_album_id IS NOT NULL AND r.owned_album_id != ?",
            [self.scope, owned.id],
        )
        wanted = title_key(owned.title)
        for row in rows:
            title = str(row["owner_title"] or row["title"] or "")
            artist = str(row["owner_artist"] or row["artist"] or "")
            if title_key(title) != wanted and title_key(str(row["title"])) != wanted:
                continue
            if not same_artist(artist, owned.artist):
                continue
            try:
                release = release_from_data(json.loads(row["data"]))
            except (ValueError, KeyError, TypeError):
                continue
            if confirm(owned, release) is None:
                continue  # a song of its own: an album of its own
            name = f"{artist or '?'} - {title}"
            return f"part of {name} (album {row['owned_album_id']}): filled there", name
        return None

    async def _in_library(self, release_ref: str | None) -> bool:
        if release_ref is None:
            return False
        row = await self.store.fetchone("SELECT 1 FROM releases WHERE ref = ?", [release_ref])
        return row is not None

    async def _planned(self, album_id: str, owned: OwnedAlbum) -> Decision | None:
        """A dry run's plan for the album, as long as its owned songs are as they were then
        (a retag - track totals, numbers, titles - can change what the album needs)."""
        row = await self.store.fetchone(
            "SELECT outcome, reason, release_ref, candidates, plan FROM album_matches"
            " WHERE album_id = ? AND scope = ? AND outcome != 'failed'"
            " AND (planned = 1 OR outcome = 'deferred')",
            [album_id, self.scope],
        )
        if row is None or not row["plan"]:
            return None
        try:
            plan = json.loads(row["plan"])
            if plan["owned"] != _fingerprint(owned):
                return None  # the album changed since: match it again
            release = release_from_data(plan["release"]) if plan.get("release") else None
            links = {CatalogRef.parse(k): str(v) for k, v in plan.get("links", {}).items()}
            backing = {CatalogRef.parse(k): str(v) for k, v in plan.get("backing", {}).items()}
        except (ValueError, KeyError, TypeError) as exc:
            log.info("album plan unreadable: %s", type(exc).__name__)
            return None
        # A deferred album is filled now (its first use; an automatic fill or a view defers
        # it again), without the reason it was deferred for.
        deferred = row["outcome"] == "deferred"
        outcome = "filled" if deferred else row["outcome"]
        if outcome == "filled" and release is None:
            return None
        candidates = [c for c in str(row["candidates"]).split(",") if c]
        reason = "" if deferred else row["reason"]
        decision = Decision(outcome, reason, release, links, candidates, row["release_ref"])
        decision.backing = backing
        return decision

    # --- the review list's actions --------------------------------------------------------

    async def choose(self, album_id: str, release_ref: str) -> Decision:
        """Fill the album from a release the owner chose (the review list). Raises
        ``ChoiceRefused`` when the release cannot fill it."""
        try:
            ref = CatalogRef.parse(release_ref)
        except ValueError:
            raise ChoiceRefused("not a catalog release") from None
        catalog = self.matcher.catalog
        if ref.catalog != catalog.key:
            raise ChoiceRefused("a release of another catalog")
        try:
            group = await self._group_key(album_id)
        except NavidromeError:
            group = f"album:{album_id}"
        # The group's lock, then the album's, as a fill takes them (the pass may be filling
        # this album from the group's release at this moment).
        async with self._locks.hold(group), self._locks.hold(album_id):
            filled = await self.store.fetchone(
                "SELECT 1 FROM releases WHERE album_id = ? OR owned_album_id = ?",
                [album_id, album_id],
            )
            if filled is not None:
                raise ChoiceRefused("the album is already filled (or a catalog album)")
            owned = await self._owned(album_id)
            if owned is None:
                raise ChoiceRefused("not an owned album")
            if (part := await self._part_of(owned)) is not None:
                raise ChoiceRefused(f"its songs are part of {part[1]}, filled already")
            if complete_by_tags(owned):
                raise ChoiceRefused("the owned files' track totals say every track is there")
            try:
                release = await catalog.album(ref.id)
            except CatalogError as exc:
                if exc.kind == "not_found":
                    raise ChoiceRefused("the catalog has no such release") from None
                raise
            if release.incomplete:
                # A track of it could not be read: filling would leave a gap for good.
                raise ChoiceRefused("the catalog's track list of this release is incomplete")
            links = confirm(owned, release)
            if links is None:
                raise ChoiceRefused("an owned track is not on this release")
            if clashes(owned, release, links):
                raise ChoiceRefused("a track it would add takes an owned track's position")
            if not release.numbered and displaced(owned, release, links):
                raise ChoiceRefused(
                    "the catalog lists this release's tracks without numbers, and the"
                    " owned files are numbered differently from its order"
                )
            if await self._in_library(str(release.ref)):
                raise ChoiceRefused(IN_LIBRARY)
            reason = "chosen from the review list"
            chosen = str(release.ref)
            decision = Decision("filled", reason, release, links, [chosen], chosen)
            if len(links) == len(release.tracks):
                decision.outcome = "complete"
            else:
                await self._materialize(album_id, owned, decision)
                if decision.reason == IN_LIBRARY:
                    raise ChoiceRefused(IN_LIBRARY)  # filled meanwhile, elsewhere
            await self._record(album_id, decision, owned)
            return decision

    async def keep(self, album_id: str) -> None:
        """The owner keeps the album as it is: it is not matched again (in this scope).
        Raises ``ChoiceRefused`` for an album that is filled, a catalog album, or not an
        owned album."""
        async with self._locks.hold(album_id):
            filled = await self.store.fetchone(
                "SELECT 1 FROM releases WHERE album_id = ? OR owned_album_id = ?",
                [album_id, album_id],
            )
            if filled is not None:
                raise ChoiceRefused("the album is already filled (or a catalog album)")
            owned = await self._owned(album_id)
            if owned is None:
                raise ChoiceRefused("not an owned album")
            row = await self.store.fetchone(
                "SELECT release_ref, candidates FROM album_matches"
                " WHERE album_id = ? AND scope = ?",
                [album_id, self.scope],
            )
            candidates = [c for c in str(row["candidates"] if row else "").split(",") if c]
            release = row["release_ref"] if row else None
            decision = Decision(
                "kept", "kept as it is (review list)", None, {}, candidates, release
            )
            await self._record(album_id, decision, owned)

    async def taken_out(
        self, album_id: str, release_ref: str
    ) -> Callable[[aiosqlite.Connection], Awaitable[None]] | None:
        """The album's match once the cleanup takes its fill out: as a deferred album
        with the fill's plan - shown complete again, filled on its next use, never
        automatically. Statements for the removal's transaction; None when the album or its
        release is not there as a fill (then it is not taken out)."""
        release = await self.store.fetchone(
            "SELECT data, owned_album_id FROM releases WHERE ref = ?", [release_ref]
        )
        if release is None or release["owned_album_id"] != album_id:
            return None
        owned = await self._owned(album_id)
        if owned is None:
            return None
        links = {
            str(r["track_ref"]): str(r["song_id"])
            for r in await self.store.fetchall(
                "SELECT track_ref, song_id FROM track_links WHERE release_ref = ? AND owned = 1",
                [release_ref],
            )
        }
        backing = {
            str(r["track_ref"]): str(r["backing_song_id"])
            for r in await self.store.fetchall(
                "SELECT track_ref, backing_song_id FROM placeholders"
                " WHERE release_ref = ? AND backing_song_id IS NOT NULL",
                [release_ref],
            )
        }
        data = json.loads(release["data"])
        plan = {"owned": _fingerprint(owned), "release": data, "links": links, "backing": backing}
        now = self.clock()
        reason = (
            f"its fill was taken out as unused ({time.strftime('%Y-%m-%d', time.localtime(now))}):"
            " shown complete, filled on its next use"
        )

        async def write(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "INSERT INTO album_matches (album_id, scope, outcome, release_ref, reason,"
                " checked_at, plan, title, artist, owned_songs, release_tracks, cleaned_at)"
                " VALUES (?, ?, 'deferred', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (album_id, scope) DO UPDATE SET outcome = 'deferred',"
                " release_ref = excluded.release_ref, reason = excluded.reason, attempts = 0,"
                " checked_at = excluded.checked_at, planned = 0, plan = excluded.plan,"
                " title = excluded.title, artist = excluded.artist,"
                " owned_songs = excluded.owned_songs, release_tracks = excluded.release_tracks,"
                " cleaned_at = excluded.cleaned_at",
                [album_id, self.scope, release_ref, reason, now, json.dumps(plan), owned.title,
                 owned.artist, len(links) + len(backing), len(data.get("tracks") or ()), now],
            )  # fmt: skip
            self._any_shown = None  # asked again at the next list

        return write

    async def rematch(self, album_id: str) -> None:
        """Forget the album's match: it is matched again when viewed, shown or passed (a
        filled album stays as it is)."""
        await self.store.execute(
            "DELETE FROM album_matches WHERE album_id = ? AND scope = ?", [album_id, self.scope]
        )

    async def _owned(self, album_id: str) -> OwnedAlbum | None:
        try:
            album = await self.navidrome.album(album_id)
        except NavidromeError as exc:
            if exc.code == 70:  # e.g. a client's stale album ID
                log.debug("album %s is not in Navidrome: left alone", album_id)
                return None
            raise
        owned = await self.engine.owned_album(album_id)
        if not owned.songs:
            return None
        status = album.get("explicitStatus")
        version = str(album.get("version") or "").lower()
        clean = True if status == "clean" or version == "clean" else False if status else None
        year = album.get("year")
        return OwnedAlbum(
            id=album_id,
            title=str(album.get("name") or ""),
            artist=str(album.get("artist") or album.get("displayArtist") or ""),
            year=year if isinstance(year, int) and year > 0 else None,
            songs=tuple(_owned_song(s) for s in owned.songs),
            clean=clean,
        )

    async def _record(
        self,
        album_id: str,
        decision: Decision,
        owned: OwnedAlbum | None,
        *,
        planned: bool = False,
    ) -> None:
        self._any_shown = None  # asked again at the next list
        plan: str | None = None
        if (planned or decision.outcome == "deferred") and owned is not None:
            data: dict[str, Any] = {"owned": _fingerprint(owned)}
            if decision.outcome in ("filled", "deferred") and decision.release is not None:
                data["release"] = release_data(decision.release)
                data["links"] = {str(k): v for k, v in decision.links.items()}
                data["backing"] = {str(k): v for k, v in decision.backing.items()}
            plan = json.dumps(data)
        await self.store.execute(
            "INSERT INTO album_matches (album_id, scope, outcome, release_ref, reason,"
            " candidates, attempts, checked_at, planned, plan, title, artist, owned_songs,"
            " release_tracks) VALUES"
            " (?, ?, ?, ?, ?, ?, CASE WHEN ? = 'failed' THEN 1 ELSE 0 END, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (album_id, scope) DO UPDATE SET outcome = excluded.outcome,"
            # A failure keeps the release and plan the album had (it is still shown with
            # them after the wait, and their songs never become a catalog copy).
            " release_ref = CASE WHEN excluded.outcome = 'failed'"
            " THEN COALESCE(excluded.release_ref, album_matches.release_ref)"
            " ELSE excluded.release_ref END,"
            " reason = excluded.reason,"
            " candidates = excluded.candidates, checked_at = excluded.checked_at,"
            " planned = excluded.planned,"
            " plan = CASE WHEN excluded.outcome = 'failed' THEN album_matches.plan"
            " ELSE excluded.plan END,"
            " title = COALESCE(NULLIF(excluded.title, ''), album_matches.title),"
            " artist = COALESCE(NULLIF(excluded.artist, ''), album_matches.artist),"
            " owned_songs = excluded.owned_songs, release_tracks = excluded.release_tracks,"
            " attempts = CASE WHEN excluded.outcome = 'failed'"
            " THEN album_matches.attempts + 1 ELSE 0 END,"
            # A fill taken out as unused is not filled automatically until it is
            # filled (or the album's match is decided otherwise).
            " cleaned_at = CASE WHEN excluded.outcome IN ('deferred', 'failed')"
            " OR excluded.planned = 1 THEN album_matches.cleaned_at ELSE NULL END",
            [
                album_id,
                self.scope,
                decision.outcome,
                decision.release_ref,
                decision.reason,
                ",".join(decision.candidates),
                decision.outcome,
                self.clock(),
                1 if planned else 0,
                plan,
                owned.title if owned is not None else "",
                owned.artist if owned is not None else "",
                _owned_count(decision, owned),
                decision.tracks,
            ],
        )


def _owned_count(decision: Decision, owned: OwnedAlbum | None) -> int:
    """The owned songs a match row notes: the release's tracks the owner has (its parts'
    too, as the fill policy counts them) for fills and deferred albums, else the album's."""
    if decision.outcome in ("filled", "deferred") and decision.release is not None:
        return decision.owned
    return len(owned.songs) if owned is not None else 0


def _has_plan(row: Any) -> bool:
    """A match row whose saved plan a view shows, and first use fills: a deferred album,
    or a dry run's plan to fill it."""
    outcome = row["outcome"]
    return bool(row["plan"]) and (
        outcome == "deferred" or (bool(row["planned"]) and outcome == "filled")
    )


def _shown(plan: str) -> Shown | None:
    """A saved plan as list entries count the album (None: unreadable, or nothing to add)."""
    try:
        data = json.loads(plan)
        release = release_from_data(data["release"])
        links = {CatalogRef.parse(k): str(v) for k, v in data.get("links", {}).items()}
    except (ValueError, KeyError, TypeError):
        return None
    missing = tuple(t.duration_ms for t in release.tracks if t.ref not in links)
    return Shown(len(set(links.values())), missing) if missing else None


def _printable(text: str) -> str:
    """A client-given value fit for one log line."""
    return "".join(ch if ch.isprintable() else "?" for ch in text[:64])


def _named(owned: OwnedAlbum) -> str:
    """An album as log lines and the review list name it: artist - title."""
    return f"{owned.artist or '?'} - {owned.title}"


def _closeness(title: str, release_title: str) -> int:
    """How closely an owned album's title names the release: exactly, or apart from
    edition words."""
    return (
        2
        if fold(title) == fold(release_title)
        else int(title_key(title) == title_key(release_title))
    )


def _fingerprint(owned: OwnedAlbum) -> list[list[Any]]:
    """The owned songs as a plan saw them (JSON-ready, in a stable order)."""
    return sorted(
        [s.id, s.disc, s.number, s.title, s.duration_ms, s.track_total, s.disc_total]
        for s in owned.songs
    )


def _owned_song(song: dict[str, Any]) -> OwnedSong:
    """A native (Navidrome API) song record of an owned file."""
    tags = song.get("tags") if isinstance(song.get("tags"), dict) else {}
    value = (tags or {}).get("isrc") or []
    values = value if isinstance(value, list) else [value]
    return OwnedSong(
        id=str(song["id"]),
        title=str(song.get("title") or ""),
        artist=str(song.get("artist") or ""),
        disc=int(song.get("discNumber") or 1),
        disc_tagged=int(song.get("discNumber") or 0) > 0,
        number=int(song.get("trackNumber") or 0),
        duration_ms=int(float(song.get("duration") or 0) * 1000),
        # "ZZ-SHJ-00-00146" and "ZZSHJ0000146" are one ISRC.
        isrcs=tuple(
            code
            for v in values
            if isinstance(v, str) and (code := "".join(ch for ch in v if ch.isalnum()).upper())
        ),
        # Navidrome keeps these album-level tags (also from "3/12" track numbers).
        track_total=_count(tags or {}, "tracktotal"),
        disc_total=_count(tags or {}, "disctotal"),
    )


def _count(tags: dict[str, Any], key: str) -> int | None:
    value = tags.get(key)
    first = value[0] if isinstance(value, list) and value else value
    try:
        number = int(str(first).strip())
    except ValueError:
        return None
    return number if number > 0 else None
