"""The daily cap on adding: how many catalog releases one account may have added to the
library within 24 hours (``[catalog] user_releases_per_day``; 0, the default: no limit).

Whose action a write is travels with the task (``adding_for``), as the engine's write
guards do: an app's request, a playlist's publication, an import or a collection change
says whose it is around the calls that may add a release, and the engine asks here at the
two places a release's row is written - a release added (``_materialize``) and one added
back from its record (``_restore_held``). So every path to those writes is counted, also
the ones that go through a fill of an owned album. Work nobody is named for is the
installation's own (the fill policy, the library pass, repairs) and is never counted;
background tasks start that way (``installation``), whoever's request started them.

What counts: a release whose first files are written for a named account that is not an
administrator. Each needs a place among the account's cap - unless the same account
already used a place for that release within the last 24 hours (it was taken out and is
added back). The count is of the 24 hours before now, so a new day brings nothing back at
once, and it is kept in the database: a restart changes nothing. Rows older than that are
removed (at every reservation while a cap is set, and at a start).

A request takes all its places at once (``places``): what an app's request, with its old
IDs, would add is reserved in one step before its first write, or nothing is written and
it is refused; a playlist takes places for its releases in the order of its songs, as far
as the room goes. The places travel with the task, the engine's writes use them
(``reserve``), each is counted in the transaction that records its release (``record``),
and those not used - a write that failed or was stopped - are given back when the request
ends. A write nobody reserved for takes its own place at the write. So nothing passes the
cap, whatever runs at the same time.

Whether the account is an administrator is read when a place is taken, never kept for an
operation: an answer is used for ``role_seconds`` at most (5 seconds, and never longer
than a credential check is reused), so an account demoted while a request waits for the
catalog, or between two releases of a publication, is counted from then on.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiosqlite

from shijhon.locks import KeyedLocks
from shijhon.placeholders.engine import MaterializeError

if TYPE_CHECKING:
    from shijhon.store import Store

log = logging.getLogger(__name__)

DAY = 86400.0
ROLE_SECONDS = 5.0  # how long an answer about an account's role is used, at most
# The refusal's reason, as ``MaterializeError.reason``: callers know the refusal by it,
# also where an error is passed on as a plain ``MaterializeError``.
REACHED = "the daily limit of added releases is reached"
CATEGORY = "daily_limit"  # the refusal as a commit's error names it
# What an app's request is answered with (a Subsonic error's message).
MESSAGE = "The daily limit of added releases is reached. Try again later."


class DailyLimitReached(MaterializeError):
    """The account has added as many releases in the last 24 hours as it may."""

    def __init__(self) -> None:
        super().__init__(REACHED)


@dataclass
class Adder:
    """The account a release is added for. ``admin``: whether it is an administrator, as
    last read (None: not read yet), ``admin_at`` when (monotonic) - read again once that
    is older than the cap's ``role_seconds``."""

    user_id: str
    admin: bool | None = None
    admin_at: float = field(default_factory=time.monotonic)


@dataclass
class _Place:
    user_id: str
    busy: bool = False  # a write is using it now


@dataclass
class Places:
    """The places one request took, by release; those a write did not use are given back
    when the request ends (``DailyCap.give_back``)."""

    places: dict[str, _Place] = field(default_factory=dict)
    closed: bool = False


@dataclass(frozen=True)
class Taken:
    """What a reservation got: the releases beyond the room, in the order they were asked
    for (only when it may be partly met; otherwise it is refused whole)."""

    refused: tuple[str, ...] = ()


@dataclass
class Hold:
    """A place a write is using: its own, or one of its request's (``of``)."""

    user_id: str
    ref: str
    of: Places | None = None
    recorded: bool = False


@dataclass(frozen=True)
class Standing:
    """Where an account stands: the cap, the releases counted in the last 24 hours, and
    when the oldest of them stops counting (None: none counted)."""

    limit: int
    used: int
    frees_at: float | None

    @property
    def left(self) -> int:
        return max(self.limit - self.used, 0)


_ADDER: ContextVar[Adder | None] = ContextVar("shijhon_release_adder", default=None)
_PLACES: ContextVar[Places | None] = ContextVar("shijhon_release_places", default=None)


@contextmanager
def adding_for(user_id: str | None, *, admin: bool | None = None) -> Iterator[None]:
    """The releases added in this block (and the tasks it starts) are added for the
    account ``user_id``. None: for nobody, the installation's own work. ``admin``: its
    role as the caller just read it (read again when a place is taken later)."""
    token = _ADDER.set(Adder(user_id, admin) if user_id else None)
    try:
        yield
    finally:
        _ADDER.reset(token)


def adding_for_request(kept: Mapping[str, Any], username: str) -> AbstractContextManager[None]:
    """``adding_for`` the account of an app's request: the one its library access was
    read for (``collection.gates``: kept with the request), administrator or not. A
    request without it is counted under its user name, as an account that is not one."""
    principal = kept.get("shijhon.principal")
    user_id = getattr(principal, "user_id", None)
    if isinstance(user_id, str) and user_id:
        return adding_for(user_id, admin=bool(getattr(principal, "is_admin", False)))
    return adding_for(username, admin=False)


def installation() -> None:
    """This task's work is the installation's own from here on: a background task never
    adds for the person whose request happened to start it, nor with its places."""
    _ADDER.set(None)
    _PLACES.set(None)


def adder() -> Adder | None:
    """The account this task adds for, or None."""
    return _ADDER.get()


def reached(exc: BaseException) -> bool:
    """Whether ``exc`` is the cap's refusal: the engine's (by its reason: errors are passed
    on as plain ``MaterializeError``s too), a commit's (by its category), or a group of
    errors that holds one (a refusal among tasks)."""
    if isinstance(exc, BaseExceptionGroup):
        return any(reached(inner) for inner in exc.exceptions)
    return getattr(exc, "reason", None) == REACHED or getattr(exc, "category", None) == CATEGORY


class DailyCap:
    def __init__(
        self,
        store: Store,
        *,
        limit: int = 0,
        is_admin: Callable[[str], Awaitable[bool]] | None = None,
        role_seconds: float = ROLE_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.limit = limit  # releases an account may add in 24 hours; 0: no limit
        # Whether an account is a Navidrome administrator now. Without it the caller's
        # word stands; when it cannot be told, the account is counted like anyone.
        self.is_admin = is_admin
        self.role_seconds = role_seconds
        self.clock = clock
        self.monotonic = monotonic
        self._held: Counter[str] = Counter()  # places taken and not yet counted, by account
        self._locks = KeyedLocks()

    @property
    def on(self) -> bool:
        """Whether a cap is set (off: nothing here asks, counts or keeps anything)."""
        return self.limit > 0

    async def limited(self, who: Adder | None = None) -> Adder | None:
        """The account this task's additions are counted for (``who``: that account
        instead), or None: no cap is set, nobody is named, or an administrator - by an
        answer no older than ``role_seconds``."""
        who = who if who is not None else _ADDER.get()
        if who is None or self.limit <= 0:
            return None
        now = self.monotonic()
        if self.is_admin is not None and (
            who.admin is None or now - who.admin_at >= self.role_seconds
        ):
            try:
                who.admin, who.admin_at = bool(await self.is_admin(who.user_id)), now
            except Exception as exc:  # not known now: counted, like anyone; asked again
                log.info("daily cap: the account's role is not known (%s)", type(exc).__name__)
                return who
        return None if who.admin else who

    async def prune(self) -> None:
        """Forget the additions that no longer count (older than 24 hours)."""
        await self.store.execute(
            "DELETE FROM release_additions WHERE added_at <= ?", [self.clock() - DAY]
        )

    async def _used(self, user_id: str) -> int:
        row = await self.store.fetchone(
            "SELECT COUNT(*) AS n FROM release_additions WHERE user_id = ? AND added_at > ?",
            [user_id, self.clock() - DAY],
        )
        return int(row["n"]) if row is not None else 0

    async def _counted(self, user_id: str, refs: Iterable[str]) -> set[str]:
        """Those of ``refs`` the account used a place for within the last 24 hours."""
        found: set[str] = set()
        for ref in refs:
            row = await self.store.fetchone(
                "SELECT 1 FROM release_additions WHERE user_id = ? AND release_ref = ?"
                " AND added_at > ?",
                [user_id, ref, self.clock() - DAY],
            )
            if row is not None:
                found.add(ref)
        return found

    # --- a request's places -------------------------------------------------------------

    async def _take(self, batch: Places, refs: Iterable[str], partial: bool) -> Taken:
        """Places in ``batch`` for those of ``refs`` that need one, in one step: all of
        them, or (``partial``) as many as there is room for, in their order. Raises
        ``DailyLimitReached`` when all are needed and not all are there: none is taken."""
        who = await self.limited()
        if who is None:
            return Taken()
        user = who.user_id
        async with self._locks.hold(user):
            await self.prune()
            wanted = [r for r in dict.fromkeys(refs) if r not in batch.places]
            counted = await self._counted(user, wanted)
            need = [r for r in wanted if r not in counted]
            room = max(self.limit - await self._used(user) - self._held[user], 0)
            if len(need) > room and not partial:
                raise DailyLimitReached
            for ref in need[:room]:
                batch.places[ref] = _Place(user)
                self._held[user] += 1
            return Taken(tuple(need[room:]))

    async def reserve_all(self, refs: Iterable[str], *, partial: bool = False) -> Places:
        """The places a request needs for the releases ``refs``, taken in one step before
        its first write (``_take``); given back with ``give_back``. ``keep`` makes them
        this task's."""
        batch = Places()
        await self._take(batch, refs, partial)
        return batch

    def keep(self, batch: Places) -> None:
        """``batch`` is this task's from here on (a request whose steps are not one block:
        the caller gives it back when the request ends)."""
        _PLACES.set(batch)

    def give_back(self, batch: Places) -> None:
        """The request ended: the places no write used are free again. (One a write still
        uses is settled when that write ends.)"""
        batch.closed = True
        for ref, place in list(batch.places.items()):
            if not place.busy:
                del batch.places[ref]
                self._drop(place.user_id)

    @asynccontextmanager
    async def places(self, refs: Iterable[str], *, partial: bool = False) -> AsyncIterator[Taken]:
        """The releases written in this block for this task's account have their places
        from here: all of ``refs``, or the block is refused (``DailyLimitReached``) before
        it began; ``partial``: as many as there is room for, in their order - the rest are
        told (``Taken.refused``). Within a request that has places already (its old IDs
        took them), those are used and only the missing ones are taken."""
        outer = _PLACES.get()
        if outer is not None and not outer.closed:
            yield await self._take(outer, refs, partial)
            return
        batch = Places()
        taken = await self._take(batch, refs, partial)
        token = _PLACES.set(batch)
        try:
            yield taken
        finally:
            _PLACES.reset(token)
            self.give_back(batch)

    def _mine(self, ref: str, user_id: str) -> _Place | None:
        batch = _PLACES.get()
        place = batch.places.get(ref) if batch is not None and not batch.closed else None
        return place if place is not None and place.user_id == user_id else None

    def _drop(self, user_id: str) -> None:
        self._held[user_id] -= 1
        if self._held[user_id] <= 0:
            del self._held[user_id]

    # --- a write ------------------------------------------------------------------------

    async def check(self, refs: Iterable[str]) -> None:
        """Refuse (``DailyLimitReached``) when this task's account may not add all of
        ``refs`` now, those its request has places for aside. Nothing is taken: a look
        before work that would be wasted. The write itself asks again (``reserve``)."""
        who = await self.limited()
        if who is None:
            return
        wanted = {r for r in refs if self._mine(r, who.user_id) is None}
        new = wanted - await self._counted(who.user_id, wanted)
        if not new:
            return
        used = await self._used(who.user_id) + self._held[who.user_id]
        if used + len(new) > self.limit:
            raise DailyLimitReached

    async def reserve(self, ref: str) -> Hold | None:
        """The place for the release ``ref``, about to be written for this task's account:
        its request's, else one of its own; None when the write is not counted. Raises
        ``DailyLimitReached``: nothing may be written. Given up with ``release``."""
        who = await self.limited()
        if who is None:
            return None
        user = who.user_id
        async with self._locks.hold(user):
            place = self._mine(ref, user)
            if place is not None and not place.busy:
                place.busy = True
                return Hold(user, ref, _PLACES.get())
            await self.prune()
            if await self._counted(user, [ref]):
                return None  # a place was used for it within these 24 hours
            if await self._used(user) + self._held[user] >= self.limit:
                raise DailyLimitReached
            self._held[user] += 1
        return Hold(user, ref)

    async def record(self, conn: aiosqlite.Connection, hold: Hold | None) -> None:
        """The release is in the library: counted, in the transaction that records it."""
        if hold is None:
            return
        await conn.execute(
            "INSERT OR REPLACE INTO release_additions (user_id, release_ref, added_at)"
            " VALUES (?, ?, ?)",
            [hold.user_id, hold.ref, self.clock()],
        )
        hold.recorded = True

    def release(self, hold: Hold | None) -> None:
        """The write ended. Recorded: its place is counted in the database from now on.
        Not recorded: its own place is free again; its request's goes back to the request
        (for another try), which gives it back at its end."""
        if hold is None:
            return
        batch = hold.of
        place = batch.places.get(hold.ref) if batch is not None else None
        if batch is None or place is None:
            self._drop(hold.user_id)
        elif hold.recorded or batch.closed:
            del batch.places[hold.ref]
            self._drop(hold.user_id)
        else:
            place.busy = False

    async def standing(self, user_id: str, *, admin: bool | None = None) -> Standing | None:
        """Where the account stands, or None when it is not limited (no cap, or an
        administrator)."""
        who = await self.limited(Adder(user_id, admin, self.monotonic()))
        if who is None:
            return None
        row = await self.store.fetchone(
            "SELECT COUNT(*) AS n, MIN(added_at) AS first FROM release_additions"
            " WHERE user_id = ? AND added_at > ?",
            [user_id, self.clock() - DAY],
        )
        used = int(row["n"]) if row is not None else 0
        first = row["first"] if row is not None else None
        return Standing(self.limit, used, float(first) + DAY if first is not None else None)
