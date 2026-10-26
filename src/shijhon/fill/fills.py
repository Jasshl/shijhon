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

Each album's outcome is kept per catalog and region in
``album_matches``: ``filled``, ``complete``, ``review`` (the review list), ``none``,
``failed`` (tried again later, waiting twice as long each time) or ``kept`` (the owner
chose to keep it as it is). Only negative matches expire: after a matcher revision,
changed owned metadata, or seven days. Fresh negatives never query the catalog again.
A dry run records what it would do as a plan (``planned``)
without writing anything; acting on the album later follows the plan without asking the
catalog again, unless the owned songs changed (their IDs, positions, titles, lengths or
totals). Albums that are catalog releases, and
albums already filled, are left alone - also after a switch to another catalog, whose
matches are kept apart (the scope): filled albums are reused, never refilled. A release
already in the library as another album goes to review.

The review list's actions: fill an album from a release the owner chose (refused when an
owned track is not on it, a new track would take an owned track's position, or the owned
files say the album is complete), keep it as it is, or match it again.

**Automatic picks** (``auto_pick``, on by default): an album the matcher would send to
review for several plausible editions, or for a release numbered differently from the
owned files, is matched with the most likely release instead (``matching/matcher.py``),
recorded as picked (``picked``: the reason it settled), and shown and filled like any
match. Its tracks keep the release's numbering, also where an owned song has the same disc
and number. A review row for one of those reasons is matched again while picks are on - by
the library pass, a view, or a search or artist page that shows the album. The review list
lists the picks too, with the same actions to change them while they are not filled.

**Fill policy**: automatic fills - the library pass, albums a search, an
artist page or a view shows - only for albums of which the owner has at least ``min_songs``
songs (3) or a share (a quarter) of the release; the others are matched and kept as
``deferred``, with their plan: shown complete, and filled on first use. While the
library pass is a dry run nothing is filled automatically: every album a view, search,
artist page or sync matches is kept as ``deferred``.

**Never two albums of one name**: a catalog release first used while an owned album of
its name is the same album (``matching/albums.py``) but has a recording the release does
not - so no fill - adds the release's songs that album lacks into it, at the release's
numbers, instead of an album beside it (``first_use``); an album of the release's very
name that is complete takes a bigger edition's other songs the same way on their first use
(automatic fills never pad a complete album); where that album
cannot take them, the use is refused.

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
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
import anyio
import mutagen

from shijhon.catalog.base import CatalogError
from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    artwork_url,
    release_data,
    release_from_data,
)
from shijhon.locks import KeyedLocks
from shijhon.matching import covers
from shijhon.matching.albums import Album, Song, pairs, same_album, same_name
from shijhon.matching.matcher import (
    INCOMPLETE,
    MATCH_REVISION,
    PICKABLE,
    Match,
    Matcher,
    Outcome,
    OwnedAlbum,
    OwnedSong,
    clashes,
    complete_by_tags,
    confirm,
    displaced,
    doubled,
    owned_fingerprint,
    unfit,
)
from shijhon.matching.normalize import core_title, credit_names, fold, same_artist, title_key
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.engine import ApprovalRefused, MaterializeError, PlaceholderEngine
from shijhon.store import Store
from shijhon.views.bursts import Burst, Bursts, Client

log = logging.getLogger(__name__)
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]
MAX_QUEUED = 200  # exposed albums waiting for a match
PER_ANSWER = 5  # albums of one search result or artist page queued
MAX_RETRY = 30 * 86400.0  # the longest wait before a failed album is tried again
IDLE_POLL = 0.5  # seconds: how often the library pass checks whether it is its turn
IN_LIBRARY = "the release is in the library as another album"  # a review reason
# A first use's fill of the same album that has an owned recording the release does not.
JOINED = "the same album: the release's other songs added beside the owned ones"
COVER_SIZE = 600  # the catalog cover asked for when two albums' covers are compared
_COVER_NAMES = ("cover", "folder", "front")  # Navidrome's default cover art priority
_PICTURE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
SHOWN_PLANS = 20_000  # plans kept read for album lists (a few hundred bytes each)
ANY_SHOWN_SECONDS = 10.0  # how long "no album is shown complete" is believed
NEGATIVE_SECONDS = 7 * 86400.0
LOOKUP_LIMIT = 20_000  # albums the owned-album lookup reads at most, all searches (more: refused)
LOOKUP_NAMES = 8  # credited names it searches at most (more: refused)
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
    the ``owned`` songs its plan links (Navidrome's song count while the plan fits), the
    lengths (ms) of the release's ``missing`` tracks, which its view adds, and the IDs of
    the owned ``songs`` (a selected library list checks the album's own songs by them)."""

    owned: int
    missing: tuple[int, ...]
    songs: frozenset[str] = frozenset()


class ChoiceRefused(Exception):
    """A release the owner chose from the review list cannot fill the album; ``reason``."""

    def __init__(self, reason: str, *, category: str = "owned_fill") -> None:
        super().__init__(reason)
        self.reason = reason
        self.category = category


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
    picked: str = ""  # the review reason an automatic pick settled ("": not picked)
    # A track the fill adds may take a disc and number an owned song of the album has: a
    # picked release keeps its numbering, and a fill taken out is added back as it was.
    shares: bool = False

    @classmethod
    def of(cls, match: Match) -> Decision:
        ref = str(match.release.ref) if match.release is not None else None
        outcome = _OUTCOMES[match.outcome]
        decision = cls(
            outcome, match.reason, match.release, dict(match.links), match.candidates, ref
        )
        decision.picked = match.picked
        decision.shares = bool(match.picked)
        return decision

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
        auto_pick: bool = True,
        clock: Callable[[], float] = time.time,
        negative_seconds: float = NEGATIVE_SECONDS,
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
        self.negative_seconds = negative_seconds
        self.syncs = syncs  # clients opening many unmatched albums: syncing
        self.exposures = exposures or Bursts(20, 60.0)  # clients shown many albums
        self.pause = pause_seconds  # between background matches (about 1 request/s)
        self.rest_seconds = rest_seconds
        self.policy = policy or FillPolicy()
        # Automatic fills at all (views, searches, artist pages, syncs, the pass): off while
        # the library pass is a dry run - only first use fills then.
        self.auto_fill = auto_fill
        # Several plausible editions, or a release numbered differently from the owned
        # files: the most likely release is picked, instead of the review list.
        self.auto_pick = auto_pick
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
        self.lookup_limit = LOOKUP_LIMIT
        self.fills = 0  # observable in tests

    # --- views and first use ------------------------------------------------------

    async def viewable(self, album_id: str) -> bool:
        """Whether a view of this album may have something to add (no work, one or two
        small queries): not a catalog album, not filled, and a plan that fills it (checked
        against the owned songs then), no match yet, a negative that may be stale, a failure
        whose wait is over, or a review an automatic pick may settle now. No native I/O
        before authentication. Fresh dry-run negatives add nothing; a view leaves those
        plans to the library pass."""
        if not album_id or "." in album_id or await self._filled(album_id):
            return False
        row = await self._row(album_id)
        return (
            row is None
            or _has_plan(row)
            or row["outcome"] == "none"
            or self._retry_due(row)
            or self._repick(row)
        )

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
        row = await self._row(album_id)
        if row is None or not _has_plan(row):
            return None
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

    async def unsettled(self, album_ids: Iterable[str]) -> set[str]:
        """The owned albums among ``album_ids`` that no release completes: in review (unless
        for a release that fills or completes another album, or is in the library - a part
        of that album's release, say), without a match, or kept as they are by the owner -
        not filled. Search
        results and artist pages show the catalog's editions of these beside them. Albums
        not matched yet are not among them (their match is under way)."""
        wanted = sorted({a for a in album_ids if a and "." not in a})
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            rows = await self.store.fetchall(
                "SELECT album_id FROM album_matches m"  # noqa: S608
                f" WHERE scope = ? AND album_id IN ({','.join('?' * len(chunk))})"
                " AND (outcome IN ('none', 'kept') OR (outcome = 'review' AND ("
                " release_ref IS NULL OR NOT ("
                " EXISTS (SELECT 1 FROM releases WHERE ref = m.release_ref)"
                " OR EXISTS (SELECT 1 FROM album_matches o WHERE o.scope = m.scope"
                " AND o.release_ref = m.release_ref AND o.album_id != m.album_id"
                " AND o.outcome IN ('filled', 'deferred', 'complete'))))))"
                " AND NOT EXISTS (SELECT 1 FROM releases WHERE owned_album_id = m.album_id)",
                [self.scope, *chunk],
            )
            found.update(str(r["album_id"]) for r in rows)
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

    async def first_use(self, release: CatalogRelease, trigger: str) -> str | None:
        """Claim an owned album for this exact release before a standalone commit.

        Existing plans retain priority. Otherwise the owned albums of the release's artist
        and title (``_same_title``: all of them, or refused) that have every owned
        recording on it are compatible: exactly one is claimed; several are refused (the
        owner resolves duplicates). With none, an owned album that is the same album as the
        release (``matching/albums.py``: the same name and a shared recording, else the same
        year and covers that do not clearly differ; of several, the one with the most songs)
        is claimed all the same: the release's songs it lacks are added into it, its own
        songs - also those the release has in another recording - staying as they are, so
        that apps never show two albums of one name. Without one
        (another album of the same name and another year, another edition): None, and the
        caller commits the release as its own album - unless Navidrome could put its songs
        into one of those owned albums (``_would_group``): refused before anything is
        written. Once an album is claimed, every refusal propagates: the caller must never
        retry it as a standalone catalog album.
        """
        ref = str(release.ref)
        album_id = await self.owner(ref)
        if album_id is not None and await self._owned(album_id) is None:
            album_id = None  # a saved plan outlived its owned files
        if album_id is None:
            found = await self._same_title(release)
            compatible = [o for _, o in found if confirm(o, release) is not None]
            if len(compatible) > 1:
                raise ChoiceRefused(
                    "the exact release fits several owned albums", category="owned_ambiguous"
                )
            if compatible:
                album_id = compatible[0].id
            else:
                same = [o for _, o in found if await self._same_album(o, release)]
                if same:
                    # Several albums of one name (already in the library): the fullest.
                    album_id = max(same, key=lambda o: (len(o.songs), o.id)).id
                    log.info("first use of %s: the same album as %s, its songs added there",
                             ref, _named(next(o for o in same if o.id == album_id)))  # fmt: skip
                else:
                    grouped = next((o for e, o in found if _would_group(e, release)), None)
                    if grouped is not None:
                        # Navidrome keeps a rolled-back placeholder as a missing song of
                        # the album it joined: refused before writing, not undone after.
                        log.info("first use of %s refused: its songs would join %s",
                                 ref, _named(grouped))  # fmt: skip
                        raise ChoiceRefused(
                            "the release's songs would join an owned album",
                            category="album_collision",
                        )
                    return None  # no owned album fits: the release is an album of its own
        group = await self._group_key(album_id)
        # Same order as ordinary fills/choices/keep; never hold the engine lock first.
        async with self._locks.hold(group), self._locks.hold(album_id):
            if await self._in_library(ref):
                return album_id  # another first use or normal fill won the race
            if await self._filled(album_id):
                raise ChoiceRefused(
                    "the owned album is already filled from another release", category="owned_plan"
                )
            owned = await self._owned(album_id)
            if owned is None:
                return None  # no owned files remain: ordinary standalone commit is safe
            if (
                title_key(owned.title) != title_key(release.title)
                or not same_artist(owned.artist, release.artist)
                or group != f"group:{title_key(owned.title)}"
            ):
                raise ChoiceRefused(
                    "the owned album changed during preparation", category="owned_plan"
                )
            row = await self._row(album_id)
            # An album of the release's very name that its files or a match found complete
            # takes the bigger edition's other songs on their first use (a star, a playlist
            # entry), never a second album beside it; automatic fills still never pad it.
            named = same_name(album_facts(owned), release_facts(release))
            completing = named and (
                complete_by_tags(owned) or (row is not None and row["outcome"] == "complete")
            )
            if row is not None:
                settled = row["outcome"] not in ("none", "deferred", "failed")
                if settled and not _has_plan(row) and not completing:
                    raise ChoiceRefused(
                        "the owned album has a settled match or owner decision",
                        category="owned_decision",
                    )
                if row["outcome"] == "failed" and not self._retry_due(row):
                    raise ChoiceRefused(
                        "the owned album is waiting after a failed fill", category="owned_retry"
                    )
            # A saved plan still fits while the owned songs are as it saw them and each is
            # still its track's recording (``_planned``), as views and the library pass ask:
            # a new year, or an ISRC that does not contradict the release, keeps the album
            # shown complete and fillable. Every owned recording is checked again below.
            if await self._part_of(owned) is not None:
                raise ChoiceRefused(
                    "the owned album is part of an existing filled release", category="owned_plan"
                )
            if complete_by_tags(owned) and not completing:
                raise ChoiceRefused(
                    "the owned files' track totals say every track is there",
                    category="owned_complete",
                )
            if owned.clean is not None and (
                (owned.clean and release.explicit) or (not owned.clean and release.clean)
            ):
                raise ChoiceRefused(
                    "the exact release has a different clean or explicit edition",
                    category="owned_incompatible",
                )
            links = confirm(owned, release)
            joining = links is None
            if links is None:
                # Not every owned recording is on it: the same album all the same takes the
                # release's other songs beside its own (``_same_album``).
                if not await self._same_album(owned, release):
                    raise ChoiceRefused(
                        "an owned recording is not on the exact release",
                        category="owned_incompatible",
                    )
                links = joined_links(owned, release)
            # (Completing: the match that found the album complete is no plan to keep.)
            planned = None if completing else await self._planned(album_id, owned, recover=True)
            if planned is not None and planned.release is not None:
                if _release_identity(planned.release) != _release_identity(release):
                    raise ChoiceRefused(
                        "the exact release differs from the owned album plan", category="owned_plan"
                    )
                # (Joining: the plan the cleanup left when it took this release's songs
                # out of the album - they come back as they were.)
                decision = planned
                decision.shares = decision.shares or joining
            else:
                if row is not None and row["plan"] and row["outcome"] != "none" and not completing:
                    # A plan whose files changed, or a fill failure retaining its plan:
                    # do not discard its recovery/numbering/part decisions implicitly.
                    raise ChoiceRefused(
                        "the owned album plan needs to be matched again", category="owned_plan"
                    )
                reason = JOINED if joining or completing else ""
                decision = Decision("filled", reason, release, links, [ref], ref)
                # The same album keeps the release's numbering: an added song may share a
                # disc and number with an owned song (another recording of it, say).
                decision.shares = joining
            if (why := unfit(owned, release, links, picking=decision.shares)) is not None:
                raise ChoiceRefused(why, category="owned_incompatible")
            decision.release = release
            # Every current owned recording on the exact release (joining: those it has).
            decision.links = links
            if len(links) == len(release.tracks):
                # Native completeness does not establish a catalog mapping. Preserve the
                # complete owned album instead of materializing another album beside it.
                raise ChoiceRefused(
                    "every recording of the exact release is already owned",
                    category="owned_complete",
                )
            try:
                await self._materialize(album_id, owned, decision, trigger)
            except ApprovalRefused:
                # A playlist's or collection's own approval: the album's fill did not fail
                # (no wait before it is filled, by this operation or another).
                raise
            except (CatalogError, NavidromeError, MaterializeError):
                # Preserve the old plan/ref and ordinary failure backoff for recovery.
                await self._record(
                    album_id, Decision("failed", "the owned album fill failed"), owned
                )
                raise
            if decision.outcome != "filled":
                raise ChoiceRefused("the owned album could not be filled from the exact release")
            await self._record(album_id, decision, owned)
            await self._record_parts(owned, decision, planned=False)
            return album_id

    async def _same_title(self, release: CatalogRelease) -> list[tuple[dict[str, Any], OwnedAlbum]]:
        """(Navidrome's album entry, the owned album) for every owned album of the
        release's artist and title (edition words aside): Navidrome's album searches for
        each credited name ("A feat. B": A and B) and for the release's core title, merged.
        The artist search finds a title written otherwise ("NightLetters"), the title search
        an artist written otherwise ("ACDC" for "AC/DC"). Each search is one request for
        all of its albums (one consistent answer, never pages a library change can shift).
        Refused beyond ``lookup_limit`` albums in all, beyond ``LOOKUP_NAMES`` names, or
        with nothing Navidrome would search for (it ignores a one-letter search): an
        incomplete list cannot show that no other owned album fits. An uncredited release
        is looked for by its title alone (no album is compatible with it, but one may be
        grouped with it)."""
        names = list({fold(n): n for n in credit_names(release.artist) if fold(n)}.values())
        if not names and fold(release.artist):
            names = [release.artist]
        if len(names) > LOOKUP_NAMES:
            raise ChoiceRefused("the owned album lookup is too large", category="owned_ambiguous")
        queries = {" ".join(q.split()).lower(): q for q in (*names, core_title(release.title))}
        searchable = [query for query in queries.values() if len(fold(query)) >= 2]
        if not searchable:
            raise ChoiceRefused(
                "the owned album lookup has nothing to search for", category="owned_ambiguous"
            )
        entries: dict[str, dict[str, Any]] = {}
        read = 0
        for query in searchable:
            left = self.lookup_limit - read
            albums = await self._found(query, left + 1)
            if len(albums) > left:
                raise ChoiceRefused(
                    "the owned album lookup is too large", category="owned_ambiguous"
                )
            read += len(albums)
            for entry in albums:
                entries.setdefault(str(entry.get("id") or ""), entry)
        found: list[tuple[dict[str, Any], OwnedAlbum]] = []
        for ident, entry in entries.items():
            if (
                ident
                and title_key(entry.get("name")) == title_key(release.title)
                and (not names or same_artist(entry.get("artist"), release.artist))
                and (owned := await self._owned(ident)) is not None
            ):
                found.append((entry, owned))
        return found

    async def _same_album(self, owned: OwnedAlbum, release: CatalogRelease) -> bool:
        """Whether the owned album is the release's album (``matching/albums.py``): the
        same name as apps show it and a shared recording, else the same year and covers
        that do not clearly differ. A clean or explicit edition the owned album says it is
        not is another edition (an album of its own, as before)."""
        if owned.clean is not None and (
            (owned.clean and release.explicit) or (not owned.clean and release.clean)
        ):
            return False
        mine, theirs = album_facts(owned), release_facts(release)
        if not same_name(mine, theirs):
            return False
        if pairs(theirs.songs, mine.songs):
            return True
        if mine.year and theirs.year and mine.year != theirs.year:
            return False
        try:
            differ = covers.differ(
                await self._owned_cover(owned), await self._release_cover(release)
            )
        except (NavidromeError, CatalogError, OSError) as exc:
            log.info("covers not compared: %s", type(exc).__name__)
            differ = None  # cannot tell: the same year decides
        return same_album(mine, theirs, covers_differ=lambda: differ)

    async def _owned_cover(self, owned: OwnedAlbum) -> int | None:
        """The owned album's cover fingerprint, as Navidrome picks the picture by default:
        a cover, folder or front image beside its first song, else the picture in that
        file (None: none, or it cannot be read)."""
        album = await self.engine.owned_album(owned.id)
        if not album.songs:
            return None
        first = sorted(album.songs, key=lambda s: (s.get("discNumber", 0), s.get("trackNumber", 0)))
        path = self.engine.layout.absolute(str(first[0]["path"]))
        try:
            picture = await anyio.to_thread.run_sync(_album_picture, path)
        except OSError:
            return None
        return await covers.fingerprint(picture) if picture else None

    async def _release_cover(self, release: CatalogRelease) -> int | None:
        url = artwork_url(release.artwork_template, COVER_SIZE)
        if url is None:
            return None
        try:
            data, _ = await self.matcher.catalog.artwork(url)
        except CatalogError:
            return None
        return await covers.fingerprint(data)

    async def _found(self, query: str, count: int) -> list[dict[str, Any]]:
        """Navidrome's first ``count`` albums for a search, in one request."""
        answer = await self.navidrome.subsonic(
            "search3",
            [
                ("query", " ".join(query.split())),
                ("albumCount", str(count)),
                ("artistCount", "0"),
                ("songCount", "0"),
            ],
        )
        return list(answer.get("searchResult3", {}).get("album", []))

    async def saved_release(self, release_ref: str) -> CatalogRelease | None:
        """A valid saved owned plan, without a catalog request or any library write.

        First use rechecks it under the group/album locks, and the engine still checks
        the actual release against any frozen approval. Legacy plans retain their
        historical fingerprint validation; newer plans also check all matcher inputs.
        """
        album_id = await self.owner(release_ref)
        if album_id is None:
            return None
        owned = await self._owned(album_id)
        if owned is None:
            return None
        row = await self._row(album_id)
        if row is None or (
            row["owned_fingerprint"] and row["owned_fingerprint"] != owned_fingerprint(owned)
        ):
            return None
        decision = await self._planned(album_id, owned, recover=True)
        if decision is None or decision.release is None or str(decision.release.ref) != release_ref:
            return None
        return decision.release

    async def matched_release(self, release: CatalogRelease) -> CatalogRelease | None:
        """An existing, still-valid owned-album plan for this title and artist.

        This lookup is read-only: unlike showing a complete album, it never
        queues an automatic fill. A preview may prefer this edition's recordings.
        """
        rows = await self.store.fetchall(
            "SELECT album_id FROM album_matches WHERE scope=? AND plan IS NOT NULL"
            " AND json_valid(plan) AND outcome IN ('filled','deferred')"
            " AND json_extract(plan, '$.release.title')=?"
            " AND json_extract(plan, '$.release.artist')=? ORDER BY album_id",
            [self.scope, release.title, release.artist],
        )
        for row in rows:
            owned = await self._owned(str(row["album_id"]))
            if owned is None:
                continue
            decision = await self._planned(owned.id, owned)
            if decision and decision.outcome == "filled" and decision.release:
                return decision.release
        return None

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
        owned: OwnedAlbum | None = None,
        act_negative_plan: bool = False,
        navidrome_errors: bool = True,
    ) -> bool:
        """Worth matching: not a catalog release, not filled, not matched, or a stale
        negative (a failure is tried again after a while, a review an automatic pick
        settles once picks are on);
        not while the catalog rests after failing (unless
        ``mind_rest`` is off: the caller waits for the rest itself). A dry run's plan is
        acted on (``dry_run``: it is already planned). A deferred album is filled on first
        use (not ``auto``), and automatically (``auto``) only once the fill policy allows
        it (``songs``: how many it has now, when the caller knows). With automatic fills off
        (a dry run of the library pass) nothing saved is acted on automatically: only new
        matches, stale negatives and failures whose wait is over."""
        if not album_id or "." in album_id or (mind_rest and self.resting):
            return False  # nothing, a catalog ID, or resting
        if await self._filled(album_id):
            return False
        row = await self._row(album_id)
        if row is None:
            return True
        if auto and row["cleaned_at"] is not None:
            return False  # its fill was taken out as unused: its next use fills it
        if row["outcome"] == "none":
            try:
                stale = await self._negative_stale(album_id, row, owned)
            except NavidromeError:
                if not navidrome_errors:
                    raise
                return False  # an outage is not new metadata or a negative match
            # Switching the pass on settles a fresh dry-run negative without another
            # catalog query. Views still leave plans with nothing to add to the pass.
            return stale or (
                act_negative_plan
                and bool(row["planned"])
                and not dry_run
                and (not auto or self.auto_fill)
            )
        if row["outcome"] == "deferred":
            owned_count = max(int(row["owned_songs"]), songs)
            allowed = self.auto_fill and self.policy.allows(owned_count, int(row["release_tracks"]))
            return not dry_run and (not auto or allowed)
        if row["outcome"] == "failed":
            return self._retry_due(row)
        if self._repick(row):
            return True  # matched again under the automatic pick, also by a dry run
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
            " release_tracks, cleaned_at, match_revision, owned_fingerprint, release_ref"
            " FROM album_matches WHERE album_id = ? AND scope = ?",
            [album_id, self.scope],
        )

    def _repick(self, row: Any) -> bool:
        """A review an automatic pick settles, while picks are on: matched again."""
        return self.auto_pick and row["outcome"] == "review" and row["reason"] in PICKABLE

    async def _negative_stale(
        self, album_id: str, row: Any, owned: OwnedAlbum | None = None
    ) -> bool:
        """Only negatives expire; no catalog I/O, and all callers use this same gate."""
        if row["outcome"] != "none":
            return False
        if (
            row["match_revision"] != MATCH_REVISION
            or not row["owned_fingerprint"]
            or self.clock() - float(row["checked_at"]) >= self.negative_seconds
        ):
            return True
        owned = owned if owned is not None else await self._owned(album_id)
        return owned is not None and row["owned_fingerprint"] != owned_fingerprint(owned)

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
            album_id,
            dry_run=dry_run,
            mind_rest=False,
            auto=True,
            songs=songs,
            act_negative_plan=True,
            navidrome_errors=False,
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
            return None  # never mutate another group without its lock during an outage
        async with self._locks.hold(group), self._locks.hold(album_id), AsyncExitStack() as members:
            return await self._fill_held(
                album_id,
                members=members,
                group=group,
                matcher=matcher,
                dry_run=dry_run,
                navidrome_errors=navidrome_errors,
                auto=auto,
                hold=hold,
                trigger=trigger,
            )

    async def _fill_held(
        self,
        album_id: str,
        *,
        members: AsyncExitStack,
        group: str,
        matcher: Matcher | None = None,
        dry_run: bool = False,
        navidrome_errors: bool = True,
        auto: bool = False,
        hold: bool = False,
        trigger: str | None = None,
        recheck: bool = False,
    ) -> Decision | None:
        """The ordinary match/fill with the group and album locks held."""
        # A saved plan is followed also while the catalog rests (it needs no catalog
        # request); a new match waits for the rest to end.
        owned: OwnedAlbum | None = None
        target: OwnedAlbum | None = None
        row = await self._row(album_id)
        if row is not None and row["outcome"] == "none":
            try:
                owned = await self._owned(album_id)
            except NavidromeError:
                if not navidrome_errors:
                    raise
                return None  # leave the negative intact during a native outage
            if owned is None:
                return None
        if not recheck and not await self._candidate(
            album_id,
            dry_run=dry_run,
            auto=auto,
            mind_rest=False,
            owned=owned,
            act_negative_plan=not hold,
            navidrome_errors=navidrome_errors,
        ):
            return None
        try:
            owned = owned if owned is not None else await self._owned(album_id)
            if owned is None:
                return None  # not an owned album (or no longer there)
            if group != f"group:{title_key(owned.title)}":
                return None  # retagged while acquiring the group: retry under its new lock
            target = owned
            member_states: dict[str, dict[str, Any] | None] = {}
            part = await self._part_of(owned)
            if part is not None:
                # Its songs are on a filled album's release: matched again (the
                # review list's "Match again"), another edition of that release would
                # put the same songs in two albums.
                as_part = Decision("review", part[0], None, {}, [part[2]], part[2])
                await self._record(album_id, as_part, owned, planned=dry_run)
                return as_part
            decision = None if dry_run or recheck else await self._planned(album_id, owned)
            fresh = decision is None  # matched now (not a saved plan)
            if decision is None:
                if self.resting:
                    return None
                target, decision = await self._match_group(
                    owned,
                    matcher or self.matcher,
                    member_locks=members,
                    states=member_states,
                    preserve_decisions=row is not None and row["outcome"] == "none",
                )
                await self._check_member_states(member_states)
                if target.id != album_id and await self._filled(target.id):
                    return None  # recovery filled the target while the match waited
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
                decision.reason = INCOMPLETE
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
            if fresh and decision.picked and decision.outcome in ("filled", "deferred"):
                others = len(set(decision.candidates) - {decision.release_ref})
                log.info(
                    "album matched by an automatic pick: %s (%d owned): %s, %d other"
                    " release(s) plausible (%s)%s",
                    _named(target),
                    len(target.songs),
                    decision.release_ref,
                    others,
                    decision.picked,
                    " (dry run)" if planned else "",
                )
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
        await self._check_member_states(member_states)
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
                decision.release,
                owned_album_id=album_id,
                links=decision.links,
                share_positions=decision.shares,
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
        """Serialize every compatible credit under one normalized-title lock.

        Shared-artist membership is not an equivalence relation: full artist credits
        cannot safely key group locks. Different artists sharing a title may wait too.
        """
        try:
            album = await self.navidrome.album(album_id)
        except NavidromeError as exc:
            if exc.code == 70:
                return f"album:{album_id}"  # not in Navidrome: nothing to group
            raise
        return f"group:{title_key(str(album.get('name') or ''))}"

    async def _match_group(
        self,
        owned: OwnedAlbum,
        matcher: Matcher,
        *,
        member_locks: AsyncExitStack,
        states: dict[str, dict[str, Any] | None],
        preserve_decisions: bool = False,
    ) -> tuple[OwnedAlbum, Decision]:
        """(The album the match concerns, the decision.) With other owned albums of the same
        release, the one with the most songs (the anchor) is matched; its release fills
        the album of the group with the closest title (then the most songs), the others'
        songs playing for their tracks there, and they become its parts. Otherwise the
        album is matched as it is."""
        pick = self.auto_pick
        others = await self._group(owned)
        # The title lock is already held. Keep every compatible member locked through
        # matching, materialization and recording, including a target other than owned.
        for other in sorted(others, key=lambda album: album.id):
            await member_locks.enter_async_context(self._locks.hold(other.id))
        for member in (owned, *others):
            row = await self._row(member.id)
            states[member.id] = dict(row) if row is not None else None
        if preserve_decisions:
            # Reconsidering a negative is not permission to replace another member's
            # review, saved plan, cleanup record or failure/backoff. The group may have
            # settled while this album's negative was cached. Leave that decision intact.
            for other in others:
                settled = await self._row(other.id)
                if (
                    settled is not None
                    and settled["outcome"] != "none"
                    and not self._repick(settled)
                ):
                    return owned, Decision(
                        "review", "another owned album in this group has a settled match"
                    )
        if not others:
            return owned, Decision.of(await matcher.match(owned, pick=pick))
        members = [owned, *others]
        anchor = max(members, key=lambda o: (len(o.songs), o.id))
        decision = Decision.of(await matcher.match(anchor, pick=pick))
        release = decision.release
        if decision.outcome not in ("filled", "complete") or release is None:
            if anchor.id == owned.id:
                return owned, decision
            # Nothing shared found.
            return owned, Decision.of(await matcher.match(owned, pick=pick))
        confirmed = [(anchor, decision.links)] + [
            (m, links)
            for m in members
            if m.id != anchor.id and (links := confirm(m, release)) is not None
        ]
        if owned.id not in {m.id for m, _ in confirmed}:
            # Not part of the release.
            return owned, Decision.of(await matcher.match(owned, pick=pick))
        if decision.outcome == "complete":  # the anchor owns every track: the rest are parts
            decision.parts = [m for m, _ in confirmed if m.id != anchor.id]
            return anchor, decision
        best, links = max(confirmed, key=lambda c: (_closeness(c[0].title, release.title),
                                                    len(c[0].songs), c[0].id))  # fmt: skip
        if (
            # (A pick keeps the release's numbering: a clash leaves the closest title - unless
            # two of the tracks it would add there share a place.)
            (clashes(best, release, links) and not decision.shares)
            or (decision.shares and doubled(release, links))
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

    async def _check_member_states(self, states: dict[str, dict[str, Any] | None]) -> None:
        """Do not replace a member's decision changed during an awaited match/write."""
        for album_id, before in states.items():
            row = await self._row(album_id)
            if (dict(row) if row is not None else None) != before:
                raise ChoiceRefused(
                    "the album group changed; retry after checking Library", category="owned_plan"
                )

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
            if (
                other is not None
                and other.songs
                and title_key(other.title) == wanted
                and same_artist(other.artist, owned.artist)
            ):
                found.append(other)
        return found

    async def _record_parts(self, owner: OwnedAlbum, decision: Decision, *, planned: bool) -> None:
        how = {"filled": "filled there", "deferred": "filled with it on first use"}.get(
            decision.outcome, "complete there"
        )
        reason = f"part of {_named(owner)} (album {owner.id}): {how}"
        for part in decision.parts:
            row = await self.store.fetchone(
                "SELECT outcome, planned, reason FROM album_matches"
                " WHERE album_id = ? AND scope = ?",
                [part.id, self.scope],
            )
            # The owner's choice, or a real outcome a dry run leaves alone - not a review an
            # automatic pick settles (it would be matched again at every pass).
            real = planned and not row["planned"] if row is not None else False
            if row is not None and (row["outcome"] == "kept" or (real and not self._repick(row))):
                continue
            part_decision = Decision(
                "review", reason, None, {}, [str(decision.release_ref)], decision.release_ref
            )
            await self._record(part.id, part_decision, part, planned=planned)
            if not planned:
                log.info("album match needs review: %s (%d owned): %s",
                         _named(part), len(part.songs), reason)  # fmt: skip

    async def _part_of(self, owned: OwnedAlbum) -> tuple[str, str, str] | None:
        """(the part reason, the album's name, the release) of an owned album that is a part
        of a filled album's release: the filled album is of the same artist and title,
        edition words aside, and every song of this album is on its release. Else None - a
        single or an edition with songs of its own is no part, whichever was filled first."""
        rows = await self.store.fetchall(
            "SELECT r.ref, r.owned_album_id, r.title, r.artist, r.data, m.title AS owner_title,"
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
            reason = f"part of {name} (album {row['owned_album_id']}): filled there"
            return reason, name, str(row["ref"])
        return None

    async def _in_library(self, release_ref: str | None) -> bool:
        if release_ref is None:
            return False
        row = await self.store.fetchone("SELECT 1 FROM releases WHERE ref = ?", [release_ref])
        return row is not None

    async def _planned(
        self, album_id: str, owned: OwnedAlbum, *, recover: bool = False
    ) -> Decision | None:
        """A dry run's plan for the album, as long as its owned songs are as they were then
        (a retag - track totals, numbers, titles - can change what the album needs)."""
        row = await self.store.fetchone(
            "SELECT outcome, reason, release_ref, candidates, plan, picked, checked_at,"
            " match_revision, owned_fingerprint FROM album_matches"
            " WHERE album_id = ? AND scope = ? AND (outcome != 'failed' OR ?)"
            " AND (planned = 1 OR outcome = 'deferred' OR (outcome = 'failed' AND ?))",
            [album_id, self.scope, recover, recover],
        )
        if row is None or not row["plan"] or self._repick(row):
            return None  # (a dry run's review a pick may settle now: matched again)
        if await self._negative_stale(album_id, row, owned):
            return None  # a dry run's old negative must not bypass the freshness gate
        try:
            plan = json.loads(row["plan"])
            if plan["owned"] != _fingerprint(owned):
                return None  # the album changed since: match it again
            release = release_from_data(plan["release"]) if plan.get("release") else None
            links = {CatalogRef.parse(k): str(v) for k, v in plan.get("links", {}).items()}
            backing = {CatalogRef.parse(k): str(v) for k, v in plan.get("backing", {}).items()}
            shares = plan.get("shares") is True
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.info("album plan unreadable: %s", type(exc).__name__)
            return None
        if (
            release is not None
            and row["owned_fingerprint"]
            and row["owned_fingerprint"] != owned_fingerprint(owned)
            and (
                not isinstance(plan.get("evidence"), dict)
                or _replaced(owned, links, plan["evidence"])
                or _isrc_conflict(owned, release, links)
                or confirm(owned, release) != links
            )
        ):
            # Retagged since the match so that a linked song is another recording: its
            # known ISRCs replaced by others, an ISRC its track does not have, or another
            # track's. Matched again; so is a plan saved without the ISRCs it saw. A new
            # year, or an ISRC where none was known, keeps the plan.
            return None
        # A deferred album is filled now (its first use; an automatic fill or a view defers
        # it again), without the reason it was deferred for.
        deferred = row["outcome"] in ("deferred", "failed")
        outcome = "filled" if deferred else row["outcome"]
        if outcome == "filled" and release is None:
            return None
        candidates = [c for c in str(row["candidates"]).split(",") if c]
        reason = "" if deferred else row["reason"]
        decision = Decision(outcome, reason, release, links, candidates, row["release_ref"])
        decision.backing = backing
        decision.picked = str(row["picked"] or "")
        decision.shares = shares or bool(decision.picked)
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
        group = await self._group_key(album_id)
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
            shares = clashes(owned, release, links)
            if shares and not self.auto_pick:
                raise ChoiceRefused("a track it would add takes an owned track's position")
            if shares and doubled(release, links):
                raise ChoiceRefused(
                    "the release has two tracks to add at one disc and track number"
                )
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
            decision.shares = shares
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
        group = await self._group_key(album_id)
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
        plan: dict[str, Any] = {
            "owned": _fingerprint(owned),
            "release": data,
            "links": links,
            "backing": backing,
            "evidence": _evidence(owned, {CatalogRef.parse(k): v for k, v in links.items()}),
        }
        # Added back as it was: where a track shares a disc and number with an owned song
        # (a picked release keeps its numbering), it shares them again.
        owned_at = {(song.disc, song.number) for song in owned.songs}
        placed = await self.store.fetchall(
            "SELECT disc, track FROM placeholders WHERE release_ref = ?", [release_ref]
        )
        if any((int(r["disc"]), int(r["track"])) in owned_at for r in placed):
            plan["shares"] = True
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

    async def recheck(self, album_id: str) -> Decision | None:
        """Recheck only a current negative, without filling or scheduling a fill.

        A concurrent keep, positive match, fill or cleanup wins unchanged. This action
        does not delete the old row/plan, so failures retain normal recovery provenance.
        """
        group = await self._group_key(album_id)
        async with self._locks.hold(group), self._locks.hold(album_id), AsyncExitStack() as members:
            row = await self._row(album_id)
            if row is None or row["outcome"] != "none" or await self._filled(album_id):
                return None
            if row["cleaned_at"] is not None:
                return None
            return await self._fill_held(
                album_id, members=members, group=group, hold=True, recheck=True
            )

    async def rematch(self, album_id: str) -> None:
        """Forget the current match for a later view/pass, serialized with owner actions.

        A decision changed while this action waited is never erased. Filled albums and
        their recovery records stay as they are.
        """
        before = await self._row(album_id)
        group = await self._group_key(album_id)
        async with self._locks.hold(group), self._locks.hold(album_id), AsyncExitStack() as held:
            row = await self._row(album_id)
            if row is not None and row["release_ref"]:
                # Cleanup/restoration owns this release lock, without taking group locks.
                # Wait in the same group -> album -> release order as a normal fill.
                await held.enter_async_context(self.engine.lock_for(str(row["release_ref"])))
                row = await self._row(album_id)
            if await self._filled(album_id):
                return
            if (dict(row) if row else None) != (dict(before) if before else None):
                raise ChoiceRefused("the album match changed; reload Library before matching again")
            await self.store.execute(
                "DELETE FROM album_matches WHERE album_id = ? AND scope = ?",
                [album_id, self.scope],
            )
            self._any_shown = None

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
                data["evidence"] = _evidence(owned, decision.links)
                if decision.shares:
                    data["shares"] = True
            plan = json.dumps(data)
        await self.store.execute(
            "INSERT INTO album_matches (album_id, scope, outcome, release_ref, reason,"
            " candidates, attempts, checked_at, planned, plan, title, artist, owned_songs,"
            " release_tracks, picked, match_revision, owned_fingerprint) VALUES"
            " (?, ?, ?, ?, ?, ?, CASE WHEN ? = 'failed' THEN 1 ELSE 0 END, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?) ON CONFLICT (album_id, scope) DO UPDATE SET outcome = excluded.outcome,"
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
            " picked = CASE WHEN excluded.outcome = 'failed' THEN album_matches.picked"
            " ELSE excluded.picked END,"
            " title = COALESCE(NULLIF(excluded.title, ''), album_matches.title),"
            " artist = COALESCE(NULLIF(excluded.artist, ''), album_matches.artist),"
            " owned_songs = excluded.owned_songs, release_tracks = excluded.release_tracks,"
            " match_revision = CASE WHEN excluded.outcome = 'failed'"
            " THEN album_matches.match_revision ELSE excluded.match_revision END,"
            " owned_fingerprint = CASE WHEN excluded.outcome = 'failed'"
            " THEN album_matches.owned_fingerprint ELSE excluded.owned_fingerprint END,"
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
                decision.picked if decision.outcome in ("filled", "deferred") else "",
                MATCH_REVISION,
                owned_fingerprint(owned) if owned is not None else None,
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
    songs = frozenset(links.values())
    return Shown(len(songs), missing, songs) if missing else None


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


def _release_identity(release: CatalogRelease) -> dict[str, Any]:
    """The approved release contents; artwork locations do not identify recordings."""
    data = release_data(release)
    data.pop("artwork_template", None)
    for track in data["tracks"]:
        track.pop("artwork_template", None)
    return data


def _isrc_conflict(
    owned: OwnedAlbum, release: CatalogRelease, links: dict[CatalogRef, str]
) -> bool:
    """Whether a linked owned song and its catalog track both have ISRCs, none shared."""
    songs = {song.id: song for song in owned.songs}
    for track in release.tracks:
        song = songs.get(links.get(track.ref, ""))
        code = "".join(ch for ch in track.isrc or "" if ch.isalnum()).upper()
        if song is not None and code and song.isrcs and code not in song.isrcs:
            return True
    return False


def album_facts(owned: OwnedAlbum) -> Album:
    """The owned album as the shared same-album rule compares it."""
    return Album(
        owned.title,
        owned.artist,
        owned.year,
        tuple(
            Song(s.title, s.artist, s.duration_ms, s.disc, s.number, s.isrcs) for s in owned.songs
        ),
    )


def release_facts(release: CatalogRelease) -> Album:
    """The release as the shared same-album rule compares it, named as Navidrome names
    the album Shijhon adds for it ("Title (Clean)" for a clean edition)."""
    name = f"{release.title} (Clean)" if release.clean else release.title
    return Album(
        name,
        release.artist,
        release.year,
        tuple(
            Song(
                t.title,
                t.artist,
                t.duration_ms,
                t.disc,
                t.number,
                (t.isrc,) if t.isrc else (),
                clean=True if t.clean else False if t.explicit else None,
            )
            for t in release.tracks
        ),
    )


def joined_links(owned: OwnedAlbum, release: CatalogRelease) -> dict[CatalogRef, str]:
    """Catalog track -> owned song for the release's songs the owned album already has
    (``matching/albums.pairs``: unambiguous, one to one); the others are added."""
    found = pairs(release_facts(release).songs, album_facts(owned).songs)
    return {release.tracks[i].ref: owned.songs[j].id for i, j in found.items()}


def _album_picture(first: Path) -> bytes | None:
    """A cover, folder or front picture in the folder of ``first`` (letter case aside, in
    that order), else the picture in that file; at most ``covers.MAX_IMAGE`` bytes."""
    try:
        entries = sorted(first.parent.iterdir())
    except OSError:
        entries = []
    for stem in _COVER_NAMES:
        for entry in entries:
            if (
                entry.stem.lower() == stem
                and entry.suffix.lower() in _PICTURE_SUFFIXES
                and entry.is_file()
                and entry.stat().st_size <= covers.MAX_IMAGE
            ):
                return entry.read_bytes()
    try:
        audio = mutagen.File(first)
    except mutagen.MutagenError:
        return None
    if audio is None:
        return None
    pictures = getattr(audio, "pictures", None)  # FLAC, Ogg
    if pictures:
        return bytes(pictures[0].data)
    tags = audio.tags
    if tags is None:
        return None
    if isinstance(tags, dict) or hasattr(tags, "get"):
        covr = tags.get("covr")  # MP4
        if covr:
            return bytes(covr[0])
    getall = getattr(tags, "getall", None)  # ID3
    if getall is not None:
        frames = getall("APIC")
        if frames:
            return bytes(frames[0].data)
    return None


def _would_group(entry: dict[str, Any], release: CatalogRelease) -> bool:
    """Whether Navidrome could put the release's placeholders, added as an album of its
    own, into this album of its title and artist (the caller compared those, edition
    words aside). Navidrome 0.64.2 tells albums apart by the MusicBrainz album ID, else by
    the album artist and album tags (``_identity``: letter case, Unicode quotation marks
    and dashes aside; spacing and other punctuation counted),
    the version tag and the release date tag; placeholders carry no MusicBrainz ID, the
    version "Clean" only for a clean edition, and the release's date. In doubt: yes."""
    if entry.get("musicBrainzId"):
        return False  # that album is told apart by its ID
    name, version = str(entry.get("name") or ""), str(entry.get("version") or "")
    if version and name.lower().endswith(f" ({version.lower()})"):
        name = name[: -len(version) - 3]  # Navidrome names a versioned album "Title (Clean)"
    if _identity(name) != _identity(release.title):
        return False  # another album tag ("Title (Deluxe Edition)"): a separate album
    if release.artist.strip() and _identity(entry.get("artist")) != _identity(release.artist):
        return False  # another album artist: "Tide Paper" and "TidePaper" are two artists
    if fold(version) != ("clean" if release.clean else ""):
        return False
    date, wanted = entry.get("releaseDate"), _date_parts(release.release_date)
    return not isinstance(date, dict) or wanted is None or date == wanted


# Navidrome 0.64.2's str.Clear: the quotation marks and dashes it makes plain.
_CLEAR = str.maketrans(
    dict.fromkeys("\u2018\u2019\u201b\u2032", "'")
    | dict.fromkeys(
        "\uff02\u3003\u02ee\u05f2\u1cd3\u2033\u2036\u02f6\u02ba\u201c\u201d\u02dd\u201f", '"'
    )
    | dict.fromkeys("\u2010\u2013\u2014\u2212\u2015", "-")
)


def _identity(tag: Any) -> str:
    """An album or album artist tag as Navidrome 0.64.2's album identity compares it:
    str.Clear(strings.ToLower(tag)) - lower case rune by rune (no final-sigma context:
    each character alone), the quotation marks and dashes above made plain. Spaces at
    either end aside (in doubt: the same)."""
    lowered = "".join(ch.lower()[:1] or ch for ch in str(tag or ""))
    return lowered.translate(_CLEAR).strip()


def _evidence(owned: OwnedAlbum, links: dict[CatalogRef, str]) -> dict[str, list[str]]:
    """The recordings a plan saw: each linked owned song's ISRCs (its length is in the
    plan's owned fingerprint)."""
    songs = {song.id: song for song in owned.songs}
    return {sid: list(songs[sid].isrcs) for sid in links.values() if sid in songs}


def _replaced(owned: OwnedAlbum, links: dict[CatalogRef, str], evidence: dict[str, Any]) -> bool:
    """Whether a linked owned song's known ISRCs were replaced by others: another
    recording, whatever the catalog knows. Also when the plan did not see the song."""
    songs = {song.id: song for song in owned.songs}
    for sid in links.values():
        before, song = evidence.get(sid), songs.get(sid)
        if not isinstance(before, list) or song is None:
            return True
        if before and song.isrcs and not set(before) & set(song.isrcs):
            return True
    return False


def _date_parts(date: str | None) -> dict[str, int] | None:
    """A release date as Navidrome describes an album's (year, month, day; {}: none).
    None: a date it may read otherwise."""
    if not date:
        return {}
    parts = date.split("-")
    if len(parts) > 3 or not all(part.isdigit() for part in parts):
        return None
    return dict(zip(("year", "month", "day"), map(int, parts), strict=False))


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
