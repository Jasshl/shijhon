"""The user's audio add-ons: order, enablement, network reach, limits, cooldowns,
diagnostics."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
import anyio
import httpx

from shijhon.config import AddonSettings
from shijhon.delivery.addon import Addon
from shijhon.delivery.netpolicy import Reach, Resolver, policy_client, system_resolver
from shijhon.delivery.pacing import AddonPace, Limits, Paces
from shijhon.store import Store

log = logging.getLogger(__name__)

RECENT_ATTEMPTS = 50  # a source's attempts kept for ordering the fallbacks
MEASURED_SECONDS = 7 * 86400.0  # ... within this age
SAVE_SECONDS = 60.0  # attempts are written to the database this often (and at a stop)
MAX_UNSAVED = 5000  # attempts kept for the next write while the database refuses them
MAX_LIMIT = 1000  # the most an add-on's limit can be set to (requests a second, at once)


@dataclass(frozen=True)
class Attempt:
    """One attempt at a source at byte zero: what its availability check had said
    ("ready", "not now", or "-": it cannot tell, has no check, had not answered), whether
    its audio's first byte came, and the seconds it took (lookup, link and first byte, or
    until it failed or said it lacked the song)."""

    at: float  # monotonic
    answer: str
    delivered: bool
    seconds: float


@dataclass
class SourceStats:
    successes: int = 0  # first bytes of audio (a link alone is not a success)
    failures: int = 0
    last_latency: float | None = None
    last_success_at: float | None = None  # wall clock
    last_failure: str | None = None  # a short reason, never a URL
    last_failure_at: float | None = None  # wall clock
    failures_since_success: int = 0  # in a row, whatever the song
    # Of those, errors (an HTTP 5xx, no answer, a broken answer): the source's cooldown.
    errors_since_success: int = 0
    last_error_at: float = float("-inf")  # monotonic: errors far apart are not one run
    timeouts_since_delivery: int = 0  # as the primary source
    switches_since_delivery: int = 0  # plays switched away from it as the primary
    # Its recent attempts: the fallbacks are ordered by them.
    recent: deque[Attempt] = field(default_factory=lambda: deque(maxlen=RECENT_ATTEMPTS))


@dataclass(frozen=True)
class StoredSource:
    """An add-on as stored, enabled or not (the dashboard's list)."""

    id: int
    name: str
    base_url: str = field(repr=False)  # often carries a key: never shown in full
    settings: dict[str, Any] = field(repr=False)
    enabled: bool
    position: int
    reach: Reach
    budget: float | None
    # Its own limits on what Shijhon sends it; None: the installation's.
    limits: Limits = field(default_factory=Limits)


@dataclass
class Source:
    id: int
    name: str
    position: int
    reach: Reach
    addon: Addon
    http: httpx.AsyncClient
    stats: SourceStats = field(default_factory=SourceStats)
    # Own byte-zero budget, e.g. for a worker that prepares complete files; None:
    # the global budget applies.
    budget: float | None = None
    # Its origin's limits on what Shijhon sends it: requests a second, audio openings
    # at once - shared with every add-on at the same origin. None: none (tests).
    pace: AddonPace | None = None


class SourceRegistry:
    def __init__(
        self,
        store: Store,
        *,
        resolver: Resolver = system_resolver,
        clock: Callable[[], float] = time.monotonic,
        request_timeout: float = 10.0,
        retire_after: float = 1800.0,
        limits: Limits | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.clock = clock
        self.request_timeout = request_timeout
        # What one add-on is sent in all: ``limits`` are the installation's, for an
        # add-on without its own. They belong to the add-on's origin, and outlive a changed
        # configuration (a new list does not start with a full allowance).
        self.paces = Paces() if limits is None else Paces(limits)
        if cooldown_seconds is not None:  # after a rate limit that names no time
            self.cool(cooldown_seconds)
        self._sources: dict[int, Source] | None = None
        self._cooldown: dict[int, float] = {}
        self._stats: dict[int, SourceStats] = {}
        self._rebuild = anyio.Lock()
        # Clients of replaced configurations stay open for pinned plays until this age.
        self.retire_after = retire_after
        self._retired: list[tuple[float, httpx.AsyncClient]] = []
        # Attempts recorded since the last write: (source, wall clock, answer, delivered,
        # seconds). The fallbacks' order is kept across restarts.
        self._unsaved: list[tuple[int, float, str, int, float]] = []
        self._limited: list[tuple[str, Limits]] = []  # the enabled add-ons' own limits
        self._pace_of: dict[int, AddonPace] = {}  # the enabled add-ons' origins' limits

    async def add(
        self,
        name: str,
        base_url: str,
        settings: dict[str, object] | None = None,
        *,
        reach: Reach = Reach.PUBLIC,
        enabled: bool = True,
        budget_seconds: float | None = None,
        limits: Limits | None = None,
    ) -> int:
        if budget_seconds is not None and not 0 < budget_seconds <= 600:
            raise ValueError("budget_seconds must be between 0 and 600")
        own = _checked(limits or Limits())
        row = await self.store.fetchone("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM sources")
        position = int(row["p"]) if row else 1
        source_id = await self.store.execute(
            "INSERT INTO sources (name, base_url, settings, enabled, position, reach,"
            " budget_seconds, requests_per_second, request_burst, audio_openings, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                name,
                base_url,
                json.dumps(settings or {}),
                int(enabled),
                position,
                reach.value,
                budget_seconds,
                own.requests_per_second,
                own.request_burst,
                own.audio_openings,
                time.time(),
            ],
        )
        await self.invalidate()
        return source_id

    async def update(
        self,
        source_id: int,
        *,
        base_url: str | None = None,
        settings: dict[str, object] | None = None,
        reach: Reach | None = None,
        budget_seconds: float | None = None,
        clear_budget: bool = False,
        limits: Limits | None = None,
        also: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None,
    ) -> None:
        """Change a source's configuration (the dashboard's edit). ``limits``: its own
        limits, all three (one left out goes back to the installation's). ``also`` writes
        what must be saved with it or not at all, in the same transaction (the dashboard's
        marks of which settings may be shown: a secret is never stored without its mark)."""
        if budget_seconds is not None and not 0 < budget_seconds <= 600:
            raise ValueError("budget_seconds must be between 0 and 600")
        changes: dict[str, object] = {}
        if limits is not None:
            own = _checked(limits)
            changes["requests_per_second"] = own.requests_per_second
            changes["request_burst"] = own.request_burst
            changes["audio_openings"] = own.audio_openings
        if base_url is not None:
            changes["base_url"] = base_url
        if settings is not None:
            changes["settings"] = json.dumps(settings)
        if reach is not None:
            changes["reach"] = reach.value
        if budget_seconds is not None or clear_budget:
            changes["budget_seconds"] = budget_seconds
        if changes or also is not None:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            async with self.store.transaction() as conn:
                if changes:
                    await conn.execute(
                        f"UPDATE sources SET {assignments} WHERE id = ?",  # noqa: S608 - fixed columns
                        [*changes.values(), source_id],
                    )
                if also is not None:
                    await also(conn)
        if changes:
            if changes.keys() & {"base_url", "settings", "reach"}:
                self._fresh_start(source_id)  # a changed add-on: its old failures are past
            await self.invalidate()

    async def set_enabled(self, source_id: int, enabled: bool) -> None:
        await self.store.execute(
            "UPDATE sources SET enabled = ? WHERE id = ?", [int(enabled), source_id]
        )
        if enabled:
            self._fresh_start(source_id)
        await self.invalidate()

    def _fresh_start(self, source_id: int) -> None:
        stats = self._stats.get(source_id)
        if stats is not None:
            stats.failures_since_success = stats.errors_since_success = 0
        self._cooldown.pop(source_id, None)  # a changed add-on: it may work again

    async def reorder(self, ordered_ids: list[int]) -> None:
        async with self.store.transaction() as conn:
            for position, source_id in enumerate(ordered_ids, start=1):
                await conn.execute(
                    "UPDATE sources SET position = ? WHERE id = ?", [position, source_id]
                )
        await self.invalidate()

    async def remove(self, source_id: int) -> None:
        await self.store.execute("DELETE FROM sources WHERE id = ?", [source_id])
        self._cooldown.pop(source_id, None)
        self._stats.pop(source_id, None)
        await self.invalidate()

    async def sync(self, addons: Sequence[AddonSettings]) -> None:
        """Make the stored add-ons match the configuration: added, updated and enabled in
        its order. Stored add-ons it does not name are disabled and placed after them."""
        rows = await self.store.fetchall("SELECT id, name FROM sources ORDER BY position, id")
        existing: dict[str, int] = {}
        for row in rows:  # names are not unique in storage: the first one is the add-on
            existing.setdefault(row["name"], int(row["id"]))
        order = []
        for addon in addons:
            reach = Reach(addon.reach)
            base_url = addon.base_url.get_secret_value()
            source_id = existing.get(addon.name)
            if source_id is None:
                source_id = await self.add(
                    addon.name,
                    base_url,
                    addon.settings,
                    reach=reach,
                    budget_seconds=addon.budget_seconds,
                    limits=_configured(addon),
                )
            else:
                await self.update(
                    source_id,
                    base_url=base_url,
                    settings=addon.settings,
                    reach=reach,
                    budget_seconds=addon.budget_seconds,
                    clear_budget=addon.budget_seconds is None,
                    limits=_configured(addon),
                )
                await self.set_enabled(source_id, True)
            order.append(source_id)
        others = [int(row["id"]) for row in rows if int(row["id"]) not in order]
        for source_id in others:
            await self.set_enabled(source_id, False)
        await self.reorder(order + others)

    async def differs(self, addons: Sequence[AddonSettings]) -> list[str]:
        """What ``sync`` would change: "the list" (other add-ons on, or another order) and
        the names of the add-ons whose address, network, settings, budget or limits differ
        (never their values). Empty: the stored list is the configuration's."""
        stored = await self.stored()
        found: list[str] = []
        if [s.name for s in stored if s.enabled] != [a.name for a in addons]:
            found.append("the list")
        first: dict[str, StoredSource] = {}
        for source in stored:
            first.setdefault(source.name, source)
        for addon in addons:
            kept = first.get(addon.name)
            if kept is not None and (
                kept.base_url != addon.base_url.get_secret_value()
                or kept.reach != Reach(addon.reach)
                or kept.settings != addon.settings
                or kept.budget != addon.budget_seconds
                or kept.limits != _configured(addon)
            ):
                found.append(addon.name)
        return found

    async def invalidate(self) -> None:
        """Reload the configuration on next use. Clients in use by pinned plays are kept
        open (retired) instead of being closed under them."""
        if self._sources:
            deadline = self.clock() + self.retire_after
            self._retired += [(deadline, source.http) for source in self._sources.values()]
        self._sources = None
        await self._close_retired()

    async def _close_retired(self, everything: bool = False) -> None:
        now = self.clock()
        keep = []
        for deadline, client in self._retired:
            if everything or deadline <= now:
                await client.aclose()
            else:
                keep.append((deadline, client))
        self._retired = keep

    async def enabled(self) -> list[Source]:
        """Enabled sources in the user's order (connections are reused between calls)."""
        async with self._rebuild:
            if self._sources is None:
                rows = await self.store.fetchall(
                    "SELECT id, name, base_url, settings, position, reach, budget_seconds,"
                    " requests_per_second, request_burst, audio_openings"
                    " FROM sources WHERE enabled = 1 ORDER BY position, id"
                )
                # Each origin's limits: its add-on's own over the installation's.
                self._limited = [(row["base_url"], _limits(row)) for row in rows]
                self.paces.apply(self._limited)
                sources = {}
                for row in rows:
                    reach = Reach(row["reach"])
                    budget = row["budget_seconds"]
                    # A slow add-on's requests may take as long as its own budget.
                    timeout = max(self.request_timeout, budget or 0)
                    http = policy_client(
                        reach, timeout=httpx.Timeout(timeout), resolver=self.resolver
                    )
                    pace = self.paces.of(row["base_url"])
                    addon = Addon(
                        row["base_url"], json.loads(row["settings"]), http, pace, self.paces
                    )
                    stats = self._stats.setdefault(row["id"], SourceStats())
                    sources[row["id"]] = Source(
                        row["id"], row["name"], row["position"], reach, addon, http, stats,
                        budget, pace,
                    )  # fmt: skip
                self._sources = sources
                self._pace_of = {s.id: s.pace for s in sources.values() if s.pace is not None}
            return sorted(self._sources.values(), key=lambda s: (s.position, s.id))

    def cooling(self, source_id: int) -> bool:
        """Whether the routing passes the source over now: it cools down (errors in a row,
        a slow primary), or its origin is left alone after a rate limit."""
        return self._cooling_for(source_id) > 0

    def _cooling_for(self, source_id: int) -> float:
        until = self._cooldown.get(source_id)
        left = 0.0 if until is None else max(0.0, until - self.clock())
        pace = self._pace_of.get(source_id)
        return left if pace is None else max(left, pace.blocked)

    def cooling_until(self, source_id: int) -> float | None:
        """When a source's cooldown ends (wall clock), or None when it is not cooling down."""
        left = self._cooling_for(source_id)
        return time.time() + left if left > 0 else None

    def stats(self, source_id: int) -> SourceStats | None:
        """A source's counters since startup, and its recent attempts - also those saved
        before it (None: neither)."""
        return self._stats.get(source_id)

    async def stored(self, conn: aiosqlite.Connection | None = None) -> list[StoredSource]:
        """Every stored add-on, disabled ones too, in the user's order (``conn``: read in
        the caller's transaction)."""
        sql = (
            "SELECT id, name, base_url, settings, enabled, position, reach, budget_seconds,"
            " requests_per_second, request_burst, audio_openings"
            " FROM sources ORDER BY position, id"
        )
        if conn is None:
            rows = await self.store.fetchall(sql)
        else:
            async with conn.execute(sql) as cursor:
                rows = list(await cursor.fetchall())
        found = []
        for row in rows:
            try:
                settings = json.loads(row["settings"])
            except ValueError:
                settings = {}
            found.append(
                StoredSource(
                    int(row["id"]),
                    row["name"],
                    row["base_url"],
                    settings if isinstance(settings, dict) else {},
                    bool(row["enabled"]),
                    int(row["position"]),
                    Reach(row["reach"]),
                    row["budget_seconds"],
                    _limits(row),
                )
            )
        return found

    async def pace_at(self, url: str) -> AddonPace | None:
        """The limits of the origin ``url`` is at - with the enabled add-ons' own limits
        applied (they are read with the list: before the first play, after an edit)."""
        await self.enabled()
        return self.paces.of(url)

    def cool(self, seconds: float) -> None:
        """How long an add-on is left alone after a rate limit that names no time (the
        setting ``cooldown_seconds``, changed)."""
        self.paces.cooldown = seconds
        for pace in self.paces._paces.values():
            pace.cooldown = seconds

    def limit(self, limits: Limits) -> None:
        """The installation's limits on what one add-on is sent (the settings, changed):
        from now on, for every add-on without its own."""
        self.paces.defaults = limits
        self.paces.apply(self._limited)

    def cool_down(self, source_id: int, seconds: float) -> None:
        """The routing passes the source over for ``seconds`` - or until its cooldown under
        way ends, when that is later: a new cooldown never shortens one."""
        until = self.clock() + seconds
        self._cooldown[source_id] = max(until, self._cooldown.get(source_id, until))

    def limited(self, source: Source, seconds: float, *, audio: bool = False) -> float:
        """The source answered "too many requests": no API request is sent to its origin -
        by any add-on configured there - for ``seconds`` (``AddonPace.block``: at least a
        moment, an hour at most, never shorter than a block under way); ``audio``: its
        audio said so - no audio request either. Returns the seconds it is left alone for
        now. (A source without limits of its origin - tests: its cooldown.)"""
        if source.pace is None:
            self.cool_down(source.id, seconds)
            return self._cooling_for(source.id)
        return source.pace.block(seconds, audio=audio)

    def succeeded(self, source: Source, latency: float) -> None:
        source.stats.successes += 1
        source.stats.last_latency = latency
        source.stats.last_success_at = time.time()
        source.stats.failures_since_success = source.stats.errors_since_success = 0

    def failed(self, source: Source, reason: str) -> None:
        source.stats.failures += 1
        source.stats.last_failure = reason
        source.stats.last_failure_at = time.time()
        source.stats.failures_since_success += 1

    def attempted(self, source: Source, attempt: Attempt) -> None:
        """One attempt at ``source`` at byte zero: kept with its recent ones, and in the
        database with the next write."""
        source.stats.recent.append(attempt)
        row = (source.id, time.time(), attempt.answer, int(attempt.delivered), attempt.seconds)
        self._unsaved.append(row)
        del self._unsaved[:-MAX_UNSAVED]

    async def load_attempts(self, clock: Callable[[], float]) -> None:
        """The recent attempts saved before a restart, each source's last
        ``RECENT_ATTEMPTS`` within ``MEASURED_SECONDS``, on ``clock`` (the deliverer's).
        Attempts recorded since the start (none, when called at startup) come after them."""
        wall, now = time.time(), clock()
        rows = await self.store.fetchall(
            "SELECT source_id, at, answer, delivered, seconds FROM source_attempts"
            " WHERE at >= ? ORDER BY source_id, id",
            [wall - MEASURED_SECONDS],
        )
        loaded: dict[int, list[Attempt]] = {}
        for row in rows:
            # A wall clock that was ahead then: not newer than now.
            age = max(0.0, wall - float(row["at"]))
            attempt = Attempt(now - age, str(row["answer"]), bool(row["delivered"]),
                              float(row["seconds"]))  # fmt: skip
            loaded.setdefault(int(row["source_id"]), []).append(attempt)
        for source_id, attempts in loaded.items():
            stats = self._stats.setdefault(source_id, SourceStats())
            recorded = list(stats.recent)
            stats.recent.clear()
            stats.recent.extend(attempts + recorded)

    async def save_attempts(self) -> None:
        """Write the attempts recorded since the last write; keep each source's last
        ``RECENT_ATTEMPTS`` (in the order they were written) within ``MEASURED_SECONDS`` in
        the database. Those of a source removed meanwhile are dropped. Not cut short by
        a cancellation (the stop's own write would repeat a committed one)."""
        rows, self._unsaved = self._unsaved, []
        try:
            with anyio.CancelScope(shield=True):
                async with self.store.transaction() as conn:
                    await conn.executemany(
                        "INSERT INTO source_attempts (source_id, at, answer, delivered, seconds)"
                        " SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM sources WHERE id = ?)",
                        [(*row, row[0]) for row in rows],
                    )
                    for source_id in sorted({row[0] for row in rows}):
                        await conn.execute(
                            "DELETE FROM source_attempts WHERE source_id = ? AND id NOT IN"
                            " (SELECT id FROM source_attempts WHERE source_id = ?"
                            " ORDER BY id DESC LIMIT ?)",
                            [source_id, source_id, RECENT_ATTEMPTS],
                        )
                    await conn.execute(
                        "DELETE FROM source_attempts WHERE at < ?",
                        [time.time() - MEASURED_SECONDS],
                    )
        except BaseException:
            self._unsaved = (rows + self._unsaved)[-MAX_UNSAVED:]  # the next write, then
            raise

    async def keep_attempts(self, interval: float = SAVE_SECONDS) -> None:
        """Write the recent attempts now and then (the app's background task)."""
        while True:
            await anyio.sleep(interval)
            try:
                await self.save_attempts()
            except Exception as exc:  # the next round tries again
                log.warning("add-on measurements not saved: %s", type(exc).__name__)

    async def aclose(self) -> None:
        try:
            await self.save_attempts()
        except Exception as exc:
            log.warning("add-on measurements not saved: %s", type(exc).__name__)
        await self.invalidate()
        await self._close_retired(everything=True)


def _limits(row: Any) -> Limits:
    return Limits(row["requests_per_second"], row["request_burst"], row["audio_openings"])


def _configured(addon: AddonSettings) -> Limits:
    return Limits(addon.requests_per_second, addon.request_burst, addon.audio_openings)


def _checked(limits: Limits) -> Limits:
    """An add-on's own limits as they are stored (ValueError: out of range)."""
    rate, burst, openings = limits.requests_per_second, limits.request_burst, limits.audio_openings
    if rate is not None and not 0 <= rate <= MAX_LIMIT:
        raise ValueError(f"requests_per_second must be between 0 and {MAX_LIMIT}")
    if burst is not None and not 1 <= burst <= MAX_LIMIT:
        raise ValueError(f"request_burst must be between 1 and {MAX_LIMIT}")
    if openings is not None and not 0 <= openings <= MAX_LIMIT:
        raise ValueError(f"audio_openings must be between 0 and {MAX_LIMIT}")
    return Limits(
        None if rate is None else float(rate),
        None if burst is None else int(burst),
        None if openings is None else int(openings),
    )
