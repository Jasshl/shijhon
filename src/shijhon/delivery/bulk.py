"""Capacity-based whole-file acquisition through the generic add-on client.

The scheduler is custom because admission must reserve global, user, shared backend
and preparation capacity together, without holding one scarce slot while waiting for
another. Only pure checks run in parallel; one selected source resolves and transfers.

A background song (playlist preparation, not a client's request) waits for its turn, and
for add-ons that are paused - rate limited, or cooling down - instead of failing: nothing
is sent to a paused add-on or its capacity group meanwhile, and its pause is waited for
up to ``PAUSED_WAIT`` in all.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial

import anyio

from shijhon.delivery import pacing
from shijhon.delivery.addon import BULK_API_GUARD, AddonError, Availability, CachedAvailability
from shijhon.delivery.playback import (
    _BULK_LEASE,
    _BULK_REUSE,
    _LIBRARY_ACQUISITION,
    Accept,
    ByteRange,
    Deliverer,
    Known,
    NoSource,
    Opened,
    Pin,
    Track,
    _Changed,
    _Cooling,
    _Failed,
    _Limited,
    _Missing,
    _Paced,
    _Reacquire,
    _Refused,
)
from shijhon.delivery.playback import (
    log as playback_log,
)
from shijhon.delivery.sources import Source, StoredSource

type Progress = Callable[[str], Awaitable[None]]
type CapacitySource = Source | StoredSource

# A background song waits for paused add-ons at most this long in all, then fails: the
# longest "Retry-After" honored. A single pause of that length is waited out once.
PAUSED_WAIT = pacing.MAX_RETRY_AFTER
# While a background song waits, its observer hears "waiting" this often (seconds): a
# cancelled batch stops it without waiting for its turn.
HEARD_EVERY = 5.0
_LOOK_AGAIN = 0.5  # how often a song waiting for a paused add-on looks again (seconds)


class InPlace(Exception):
    """A background song's audio was put in place by another fetch while it waited."""


class ProgressError(Exception):
    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__(type(error).__name__)


def _origin(source: CapacitySource) -> pacing.Origin | None:
    return pacing.origin(
        source.base_url if isinstance(source, StoredSource) else str(source.addon.base)
    )


def _components(sources: Sequence[CapacitySource]) -> list[list[CapacitySource]]:
    parents = list(range(len(sources)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    seen: dict[object, int] = {}
    for index, source in enumerate(sources):
        keys: list[object] = [("id", source.id)]
        if (origin := _origin(source)) is not None:
            keys.append(("origin", origin))
        if source.capacity_group:
            keys.append(("group", source.capacity_group))
        for key in keys:
            if key in seen:
                parents[root(index)] = root(seen[key])
            else:
                seen[key] = index
    groups: dict[int, list[CapacitySource]] = {}
    for index, source in enumerate(sources):
        groups.setdefault(root(index), []).append(source)
    return list(groups.values())


def _least(values: Sequence[int]) -> int:
    return min((value for value in values if value > 0), default=0)


def effective_capacity(
    sources: Sequence[CapacitySource], downloads: int = 2, preparations: int = 1
) -> dict[int, tuple[int, int]]:
    """Effective shared caps, for the dashboard too. 0 means no cap; None inherits.

    A common origin always shares capacity, even when configured entries give it
    different group labels. Groups join those origins transitively.
    """
    enabled = [s for s in sources if not isinstance(s, StoredSource) or s.enabled]
    result: dict[int, tuple[int, int]] = {}
    for group in _components(enabled):
        limits = (
            _least([downloads if s.downloads is None else s.downloads for s in group]),
            _least([preparations if s.preparations is None else s.preparations for s in group]),
        )
        result.update((s.id, limits) for s in group)
    return result


@dataclass(eq=False)
class Lease:
    manager: BulkAcquisition
    source: Source
    user: str
    cold: bool
    references: int = 1
    started: bool = False
    active: bool = True
    owner: object | None = field(default=None, repr=False)

    def network_done(self) -> None:
        """The upstream is complete; local readers/remux keep references, not capacity."""
        if self.active:
            self.active = False
            self.cold = False
            self.manager._active.remove(self)
            self.manager.changed()

    def retain(self) -> Callable[[], None]:
        """Another owner, such as a continuing DASH job, holds the same capacity."""
        if self.references <= 0:
            raise RuntimeError("upstream lease is already closed")
        self.references += 1
        over = False

        def release() -> None:
            nonlocal over
            if not over:
                over = True
                self.close()

        return release

    def prepared(self) -> None:
        if self.cold:
            self.cold = False
            self.manager.changed()

    def close(self) -> None:
        if self.references > 0:
            self.references -= 1
            if self.references == 0:
                self.network_done()


@dataclass
class _PlaybackLease:
    ended: Callable[[], None]
    references: int = 1
    active: bool = True
    owner: object | None = field(default=None, repr=False)

    def network_done(self) -> None:
        if self.active:
            self.active = False
            self.ended()

    def prepared(self) -> None:
        pass

    def close(self) -> None:
        self.references -= 1
        if self.references == 0:
            self.network_done()

    def retain(self) -> Callable[[], None]:
        if self.references <= 0:
            raise RuntimeError("playback lease is already closed")
        self.references += 1
        over = False

        def release() -> None:
            nonlocal over
            if not over:
                over = True
                self.close()

        return release


@dataclass
class _Choice:
    source: Source
    cached: CachedAvailability | None = None
    availability: Availability | None = None
    permitted: bool = False
    exclusion: str = "its manifest could not be read"
    paused: bool = False  # its check was held back, rate limited or temporarily failed

    @property
    def ready(self) -> bool:
        return bool(self.cached and self.cached.available)

    @property
    def rank(self) -> int:
        if self.ready:
            return 0
        if self.availability is not None and self.availability.available is True:
            return 1
        if self.availability is not None and self.availability.available is False:
            return 3
        return 2


@dataclass(eq=False)
class _Waiting:
    user: str
    choices: list[_Choice]
    number: int
    urgent: Callable[[], bool] | None = None
    lease: Lease | None = None
    choice: _Choice | None = None


@dataclass(eq=False)
class _Patience:
    """A background song's waits: for its turn without a limit, for paused add-ons up to
    ``PAUSED_WAIT`` in all. Every ``HEARD_EVERY`` seconds meanwhile its observer hears
    "waiting" - a cancelled batch stops it - and ``in_place`` is asked whether another
    fetch put its audio in place (``InPlace``: nothing left to wait for)."""

    title: str
    progress: Progress | None = None
    in_place: Callable[[], Awaitable[bool]] | None = None
    paused: float = 0.0  # seconds waited while every add-on left for the song was paused
    heard: float = field(default_factory=time.monotonic)
    recheck_wait: float = 0.0  # last pause, counted only if the next check still finds no choice

    async def waited(self, seconds: float, *, paused: bool) -> None:
        if paused:
            self.paused += seconds
            if self.paused >= PAUSED_WAIT:
                reason = (
                    "its add-ons stayed paused (rate limited, cooling down or awaiting a manifest)"
                    f" for {PAUSED_WAIT / 60:g} minutes"
                )
                playback_log.info("no audio for %r: %s", self.title, reason)
                raise NoSource([reason])
        if time.monotonic() - self.heard >= HEARD_EVERY:
            self.heard = time.monotonic()
            await report(self.progress, "waiting")
            if self.in_place is not None and await self.in_place():
                raise InPlace


def _limited(exc: Exception) -> bool:
    """An attempt that "too many requests" ended, or kept from being sent: nothing about
    the song - a background song asks that add-on again once its time has passed."""
    if isinstance(exc, AddonError):
        return exc.kind in ("rate_limited", "cooling")
    return isinstance(exc, (_Limited, _Cooling))


@dataclass(eq=False)
class Plan:
    """One song's acquisition across its attempts (``BulkAcquisition.plan``): the choices
    left (None: to be made), why the others failed, its urgency at the add-ons' limits -
    and, a background song's, the add-ons ruled out for it and its patience."""

    track: Track
    user: str
    progress: Progress | None
    urgency: pacing.Urgency
    promoted: Callable[[], bool]
    patience: _Patience | None = None
    ruled_out: set[int] | None = None
    choices: list[_Choice] | None = None
    failures: list[str] = field(default_factory=list)
    allow_reuse: bool = True
    fresh: bool = False
    expect: Known | None = None


@dataclass(eq=False)
class Admission:
    """An admitted attempt (``BulkAcquisition.admit``): its choice and lease - a continuing
    DASH job's, retained, when ``reusing`` - until the attempt takes it over, or it is
    released unused."""

    plan: Plan
    choice: _Choice
    lease: Lease
    reusing: bool
    pin: Pin | None
    taken: bool = False

    def release(self) -> None:
        if not self.taken:
            self.taken = True
            self.lease.close()


class BulkAcquisition:
    def __init__(
        self,
        deliverer: Deliverer,
        *,
        downloads: int = 4,
        source_downloads: int = 2,
        preparations: int = 1,
        users: int = 4,
    ) -> None:
        self.deliverer = deliverer
        self.downloads = downloads
        self.source_downloads = source_downloads
        self.preparations = preparations
        self.users = users
        self._sources: list[Source] = []
        self._active: list[Lease] = []
        self._playing: list[Source] = []
        self._waiting: list[_Waiting] = []
        self._served: dict[str, int] = {}
        self._number = 0
        self._woken = anyio.Event()
        self._pins: dict[str, tuple[Pin, Lease]] = {}
        self._paused: list[tuple[Source, float]] = []
        self._stopped = False
        deliverer.bulk = self

    def changed(self) -> None:
        self._woken.set()

    def observe_playback(self, source: Source) -> Callable[[], None]:
        """Playback never waits for bulk; its full body reduces later bulk admission."""
        self._playing.append(source)
        self.changed()
        over = False

        def ended() -> None:
            nonlocal over
            if not over:
                over = True
                self._playing.remove(source)
                self.changed()

        return ended

    def playback_lease(self, source: Source) -> _PlaybackLease:
        return _PlaybackLease(self.observe_playback(source))

    def _group(self, source: Source) -> list[CapacitySource]:
        self._paused = [(s, until) for s, until in self._paused if until > time.monotonic()]
        all_sources: list[CapacitySource] = [
            *self._sources,
            *(lease.source for lease in self._active),
            *self._playing,
            *(s for s, until in self._paused if until > time.monotonic()),
            source,
        ]
        return next(group for group in _components(all_sources) if source in group)

    def _can(self, choice: _Choice, user: str, urgent: bool = False) -> bool:
        current = next((s for s in self._sources if s.id == choice.source.id), None)
        if current is None:
            return False
        if current.addon is not choice.source.addon:
            choice.cached = None
            choice.availability = None
        choice.source = current
        source = choice.source
        if self.deliverer.registry.cooling(source.id):
            return False
        group = self._group(source)
        ids = {s.id for s in group}
        if any(isinstance(s, Source) and s.pace and s.pace.blocked for s in group):
            return False
        if urgent:
            return True  # a reported play never waits behind queued bulk work
        if not choice.ready and any(
            s.id in ids and until > time.monotonic() for s, until in self._paused
        ):
            return False
        transfer, cold = effective_capacity(group, self.source_downloads, self.preparations)[
            source.id
        ]
        held = [lease for lease in self._active if lease.source.id in ids]
        playing = sum(s.id in ids for s in self._playing)
        return (
            (not self.downloads or len(self._active) + len(self._playing) < self.downloads)
            and (not self.users or sum(lease.user == user for lease in self._active) < self.users)
            and (not transfer or len(held) + playing < transfer)
            and (choice.ready or not cold or sum(lease.cold for lease in held) < cold)
        )

    def _held_back(self, source: Source) -> bool:
        """Whether the add-on is paused now: it cools down, or its origin - or that of an
        add-on sharing its capacity - is left alone after "too many requests"."""
        return self.deliverer.registry.cooling(source.id) or any(
            isinstance(s, Source) and s.pace is not None and s.pace.blocked > 0
            for s in self._group(source)
        )

    def _blocked(self, choice: _Choice) -> bool:
        """Whether a pause keeps the choice from admission: its add-on is held back - or,
        not ready, its backend's new preparations are paused after an uncertain one
        (cached work stays admissible then)."""
        if self._held_back(choice.source):
            return True
        if choice.ready:
            return False
        ids = {s.id for s in self._group(choice.source)}
        return any(s.id in ids and until > time.monotonic() for s, until in self._paused)

    def _recovered(self, choices: list[_Choice], ruled_out: set[int]) -> bool:
        """Whether an enabled add-on that is neither a choice nor ruled out - paused when
        the choices were made, or enabled since - may be asked now."""
        chosen = {c.source.id for c in choices}
        return any(
            s.id not in chosen and s.id not in ruled_out and not self._held_back(s)
            for s in self._sources
        )

    def _guard(self, source: Source) -> None:
        blocked = max(
            (s.pace.blocked for s in self._group(source) if isinstance(s, Source) and s.pace),
            default=0.0,
        )
        if blocked:
            raise pacing.Blocked(blocked)

    def _dispatch(self) -> None:
        while True:
            eligible = [
                w
                for w in self._waiting
                if w.lease is None
                and any(self._can(c, w.user, bool(w.urgent and w.urgent())) for c in w.choices)
            ]
            if not eligible:
                return
            waiting = min(
                eligible,
                key=lambda w: (
                    not (w.urgent and w.urgent()),
                    self._served.get(w.user, -1),
                    w.number,
                ),
            )
            choice = next(
                c
                for c in waiting.choices
                if self._can(c, waiting.user, bool(waiting.urgent and waiting.urgent()))
            )
            lease = Lease(self, choice.source, waiting.user, not choice.ready)
            self._active.append(lease)
            waiting.lease, waiting.choice = lease, choice
            self._number += 1
            self._served[waiting.user] = self._number
            self.changed()

    async def _reserve(
        self,
        user: str,
        choices: list[_Choice],
        urgent: Callable[[], bool] | None = None,
        patience: _Patience | None = None,
        ruled_out: set[int] | None = None,
    ) -> tuple[_Choice, Lease] | None:
        """The first of ``choices`` admitted (waited for). ``patience``: a background
        song's - its wait while a pause keeps every choice from admission counts against
        it. ``ruled_out`` (its add-ons that cannot serve it): None is returned once another
        add-on may be asked, or no choice is enabled any more, so that the song's choices
        are made again."""
        self._number += 1
        waiting = _Waiting(user, choices, self._number, urgent)
        self._waiting.append(waiting)
        handed = False
        entered = looked = time.monotonic()
        try:
            while True:
                if self._stopped:
                    raise NoSource(["bulk acquisition is stopping"])
                self._sources = await self.deliverer.registry.enabled()
                enabled = {s.id: s for s in self._sources}
                waiting.choices = [c for c in waiting.choices if c.source.id in enabled]
                for choice in waiting.choices:
                    if choice.source.addon is not enabled[choice.source.id].addon:
                        choice.cached = None
                        choice.availability = None
                    choice.source = enabled[choice.source.id]
                if not waiting.choices:
                    if ruled_out is not None:
                        return None  # (a background song: another add-on may recover)
                    raise NoSource(["no permitted download source remains"])
                self._dispatch()
                if waiting.lease is not None and waiting.choice is not None:
                    handed = True
                    return waiting.choice, waiting.lease
                if patience is not None:
                    now = time.monotonic()
                    if (
                        ruled_out is not None
                        and now - entered >= _LOOK_AGAIN
                        and self._recovered(waiting.choices, ruled_out)
                    ):
                        return None
                    paused = all(self._blocked(c) for c in waiting.choices)
                    await patience.waited(now - looked, paused=paused)
                    looked = now
                wake, self._woken = self._woken, anyio.Event()
                # A live setting, cooldown expiry or configuration change may have no
                # active lease to wake us. Poll locally; never poll an add-on here.
                with anyio.move_on_after(0.1):
                    await wake.wait()
        finally:
            self._waiting.remove(waiting)
            if not handed and waiting.lease is not None:
                waiting.lease.close()
            self.changed()

    async def _choices(
        self,
        track: Track,
        ruled_out: set[int] | None = None,
        known: list[_Choice] | None = None,
    ) -> tuple[list[_Choice], list[str]]:
        """The permitted sources for ``track``, best first, and why the others are not.
        ``ruled_out``: a background song's add-ons that cannot serve it - skipped, and the
        ones found here are added. Its add-ons paused now are neither asked nor ruled out:
        the song waits for them (``_wait_paused``). ``known``: its choices checked already
        (kept, not asked again)."""
        self._sources = await self.deliverer.registry.enabled()
        enabled = {s.id for s in self._sources}
        kept = [c for c in known or () if c.source.id in enabled]
        excluded = []
        if ruled_out is None:
            choices = []
            for source in self._sources:
                if self.deliverer.registry.cooling(source.id):
                    excluded.append(f"{source.name}: cooling down")
                else:
                    choices.append(_Choice(source))
        else:
            checked = {c.source.id for c in kept}
            choices = [
                _Choice(s)
                for s in self._sources
                if s.id not in ruled_out and s.id not in checked and not self._held_back(s)
            ]
        timeout = self.deliverer.settings.availability_timeout_seconds

        async def check(choice: _Choice) -> None:
            guard = BULK_API_GUARD.set(lambda: self._guard(choice.source))
            try:
                with pacing.watched() as waits, anyio.move_on_after(timeout) as scope:
                    self._guard(choice.source)
                    if not (await choice.source.addon.manifest()).downloads:
                        choice.exclusion = "the add-on asks this"
                        return
                    choice.permitted = True
                    choice.cached = await choice.source.addon.cached(track.wanted)
                    if not choice.ready:
                        choice.availability = await choice.source.addon.availability(track.wanted)
                if scope.cancelled_caught and not choice.permitted:
                    if waits.held() is None:
                        self.deliverer._source_error(
                            choice.source, AddonError("failed", "manifest read timed out")
                        )
                    choice.paused = True
                    choice.exclusion = "its manifest read timed out"
            except AddonError as exc:
                self.deliverer._source_error(choice.source, exc)
                choice.paused = _limited(exc)
                if not choice.permitted and exc.kind in ("timeout", "failed"):
                    if exc.kind == "timeout" and not exc.shared and waits.held() is None:
                        self.deliverer._errored(choice.source, exc.reason)
                    choice.paused = True
                    choice.exclusion = (
                        "its manifest read timed out"
                        if exc.kind == "timeout"
                        else "its manifest is temporarily unavailable"
                    )
            except pacing.Blocked:
                choice.paused = True
            finally:
                BULK_API_GUARD.reset(guard)

        async with anyio.create_task_group() as group:
            for choice in choices:
                group.start_soon(check, choice)
        ordered = sorted(
            [*kept, *(c for c in choices if c.permitted)],
            key=lambda c: (
                c.rank,
                self.deliverer._cost(c.source, "ready" if c.ready else "-")[0],
                c.source.position,
            ),
        )
        left_out = [c for c in choices if not c.permitted]
        if ruled_out is not None:
            left_out = [c for c in left_out if not c.paused]
            ruled_out.update(c.source.id for c in left_out)
        excluded.extend(
            f"{choice.source.name}: not used for downloads ({choice.exclusion})"
            for choice in left_out
        )
        return ordered, excluded

    def plan(
        self,
        track: Track,
        user: str,
        progress: Progress | None = None,
        *,
        playing: Callable[[], bool] | None = None,
        background: bool = False,
        in_place: Callable[[], Awaitable[bool]] | None = None,
    ) -> Plan:
        """A song's acquisition (``admit``, ``attempt``). ``playing``: whether the user's
        client plays it now (its admission then goes first). ``background``: a background
        song's (playlist preparation, not a client's request) - while every add-on that
        may serve it is paused, it waits instead of failing (``_Patience``; ``in_place``:
        whether another fetch put the song in place meanwhile)."""
        urgency = pacing.Urgency(pacing.QUEUED)

        def promoted() -> bool:
            if playing is not None and playing():
                urgency.raise_to(pacing.PLAY)
            return urgency.level <= pacing.PLAY

        promoted()
        return Plan(
            track,
            user,
            progress,
            urgency,
            promoted,
            _Patience(track.title, progress, in_place) if background else None,
            set() if background else None,
        )

    @contextmanager
    def urgent(self, plan: Plan) -> Iterator[None]:
        """Around a song's admissions and attempts: their urgency at the add-ons' limits,
        which a play of the song raises (``Deliverer._urgent``)."""
        with pacing.urgent(plan.urgency), self.deliverer._urgent(plan.track, "ahead", False):
            yield

    async def open(
        self,
        track: Track,
        user: str,
        progress: Progress | None = None,
        *,
        playing: Callable[[], bool] | None = None,
        background: bool = False,
        range_header: str | None = None,
        head: bool = False,
        accept: Accept | None = None,
    ) -> Opened:
        """The song's audio from one selected permitted source, its lease held through the
        transfer: its attempts, each admitted in turn (``plan``)."""
        plan = self.plan(track, user, progress, playing=playing, background=background)
        requested = ByteRange.parse(range_header)
        if (head or (requested is not None and not requested.at_zero)) and (
            continuing := self._pins.get(track.key)
        ):
            pin = continuing[0]
            plan.expect = Known(pin.size, pin.etag, pin.source.id, pin.track_id, pin.kind, pin.kbps)
            choices, excluded = await self._choices(track)
            plan.failures.extend(excluded)
            plan.choices = [
                choice for choice in choices if choice.source.id == continuing[0].source.id
            ]
        with self.urgent(plan):
            await report(progress, "waiting")
            while (admission := await self.admit(plan)) is not None:
                opened = await self.attempt(
                    admission,
                    progress,
                    requested=requested,
                    head=head,
                    accept=accept,
                )
                if opened is not None:
                    return opened
        raise self.failed(plan)

    def failed(self, plan: Plan) -> NoSource:
        """No source is left for the song (logged)."""
        playback_log.info("no audio for %r: %s", plan.track.title, "; ".join(plan.failures))
        return NoSource(plan.failures)

    async def admit(self, plan: Plan) -> Admission | None:
        """The song's next attempt, admitted - its turn waited for, and a background song's
        paused add-ons. None: no source is left to try (``failed``)."""
        track = plan.track
        while True:
            if plan.choices is None:
                checking = time.monotonic()
                plan.choices, excluded = await self._choices(track, plan.ruled_out)
                plan.failures.extend(excluded)
                if plan.patience is not None:
                    if not plan.choices:
                        await plan.patience.waited(
                            plan.patience.recheck_wait + time.monotonic() - checking, paused=True
                        )
                    plan.patience.recheck_wait = 0.0
            if not plan.choices:
                if (
                    plan.patience is None
                    or plan.ruled_out is None
                    or not await self._wait_paused(plan.ruled_out, plan.patience)
                ):
                    return None
                await report(plan.progress, "waiting")
                plan.choices = None
                continue
            reuse = self._pins.get(track.key)
            choice = next((c for c in plan.choices if reuse and c.source is reuse[0].source), None)
            if (
                reuse is not None
                and plan.allow_reuse
                and choice is not None
                and reuse[0].info.transport == "dash"
                and reuse[1].references > 0
                and reuse[1].active
                and self.deliverer._continuing_dash(reuse[0], reuse[1])
            ):
                reuse[1].retain()  # this caller's interest; the DASH job owns the other
                return Admission(plan, choice, reuse[1], True, reuse[0])
            reserved = await self._reserve(
                plan.user, plan.choices, plan.promoted, plan.patience, plan.ruled_out
            )
            if reserved is None:  # another add-on may be asked now: the choices again
                plan.choices, excluded = await self._choices(track, plan.ruled_out, plan.choices)
                plan.failures.extend(excluded)
                continue
            choice, lease = reserved
            pin = (
                reuse[0]
                if reuse is not None
                and reuse[0].source is choice.source
                and not self.deliverer._expired(reuse[0])
                and not reuse[0].wrong
                and not choice.ready
                and not plan.fresh
                else None
            )
            return Admission(plan, choice, lease, False, pin)

    async def attempt(
        self,
        admission: Admission,
        progress: Progress | None = None,
        *,
        requested: ByteRange | None = None,
        head: bool = False,
        accept: Accept | None = None,
    ) -> Opened | None:
        """One admitted attempt, under its lease through transfer EOF/close. None: it ended
        without audio - the plan says why, and what is left to try."""
        plan, choice, lease = admission.plan, admission.choice, admission.lease
        admission.taken = True
        track, pin, source = plan.track, admission.pin, choice.source
        opened: Opened | None = None
        transferred = False
        token = _BULK_LEASE.set(lease)
        reuse_token = _BULK_REUSE.set(admission.reusing)
        library = _LIBRARY_ACQUISITION.set(True)
        guard = BULK_API_GUARD.set(partial(self._guard, source))
        try:
            await report(progress, "fetching" if choice.ready else "preparing")
            lease.started = not choice.ready
            budget = source.budget or self.deliverer.settings.budget_seconds
            asked = self.deliverer.clock()
            deadline = asked + budget
            with self.deliverer._urgent(track, "ahead", False):
                pin, opened = await self._open_selected(
                    choice, lease, track, pin, budget, deadline, fresh=plan.fresh,
                    requested=requested, head=head, expect=plan.expect,
                )  # fmt: skip
                if accept is not None and (why := accept(opened.kind, opened.kbps)):
                    raise _Refused(why)
                if self.deliverer.pinned(track.song_id) is None:
                    self.deliverer._remember(track.song_id, pin)
                self.deliverer._delivered(pin, opened, asked)
            if opened.kind is not None or pin.told is not None:
                lease.prepared()
            await report(progress, "fetching")
            transferred = True
            return _held(opened, lease)
        except _Reacquire as exc:
            # The job or live selection changed while this caller awaited admission
            # observation. Reuse cannot authorize a distinct upstream job.
            plan.allow_reuse = False
            plan.fresh = plan.fresh or exc.resolve
        except (AddonError, _Missing, _Failed, _Changed) as exc:
            if isinstance(exc, _Changed) and plan.expect is not None:
                raise  # a continuation must never fall back to another representation
            if isinstance(exc, AddonError):
                self.deliverer._source_error(source, exc)
            elif isinstance(exc, _Failed) and pin is not None:
                self.deliverer._failure(source, exc, pin)
            if (
                lease.started
                and lease.cold
                and not isinstance(exc, (_Missing, _Cooling, _Paced, _Refused))
            ):
                # The protocol has no cancellation handle for remote preparation.
                # Leave this backend's new cold starts alone for a bounded pause;
                # cached ready-only work can still proceed. This is not a claim that
                # remote work stopped when our HTTP request ended.
                self._paused.append(
                    (source, time.monotonic() + self.deliverer.settings.cooldown_seconds)
                )
            reason = str(exc) if isinstance(exc, _Paced) else type(exc).__name__
            plan.failures.append(f"{source.name}: {reason}")
            plan.choices = [c for c in plan.choices or () if c is not choice]
            if plan.ruled_out is not None and not _limited(exc):
                plan.ruled_out.add(source.id)  # (rate limited: asked again after its time)
        except BaseException:
            if lease.started and lease.cold:
                self._paused.append(
                    (source, time.monotonic() + self.deliverer.settings.cooldown_seconds)
                )
            raise
        finally:
            BULK_API_GUARD.reset(guard)
            _LIBRARY_ACQUISITION.reset(library)
            _BULK_LEASE.reset(token)
            _BULK_REUSE.reset(reuse_token)
            if not transferred:
                if opened is not None:
                    with anyio.CancelScope(shield=True):
                        await opened.close()
                lease.close()
        return None

    async def later(self, plan: Plan) -> None:
        """A background song whose admission could not be used now (its caller has no turn
        for it yet): a moment's wait - heard - before it is admitted again."""
        began = time.monotonic()
        await anyio.sleep(_LOOK_AGAIN)
        if plan.patience is not None:
            await plan.patience.waited(time.monotonic() - began, paused=False)

    async def _wait_paused(self, ruled_out: set[int], patience: _Patience) -> bool:
        """A background song with no choice left: whether an enabled add-on that may serve
        it remains (not ``ruled_out``). While every such add-on is paused, the song waits -
        a moment at least, counted against its patience once it is still paused after it -
        and nothing is sent to them."""
        began: float | None = None
        while True:
            if self._stopped:
                raise NoSource(["bulk acquisition is stopping"])
            self._sources = await self.deliverer.registry.enabled()
            left = [s for s in self._sources if s.id not in ruled_out]
            if not left:
                return False
            if began is not None:
                if not all(self._held_back(s) for s in left):
                    patience.recheck_wait += time.monotonic() - began
                    return True  # (before its last moment counts: a pause just over)
                await patience.waited(time.monotonic() - began, paused=True)
            began = time.monotonic()
            await anyio.sleep(_LOOK_AGAIN)

    async def _open_selected(
        self,
        choice: _Choice,
        lease: Lease,
        track: Track,
        pin: Pin | None,
        budget: float,
        deadline: float,
        *,
        fresh: bool = False,
        requested: ByteRange | None = None,
        head: bool = False,
        expect: Known | None = None,
    ) -> tuple[Pin, Opened]:
        source = choice.source
        # A plain play and a bulk request share resolution too. Publish a new pin only
        # when no play has one already; a download never replaces a continuing play's.
        async with self.deliverer._routing_lock(track.song_id):
            if not (await source.addon.manifest()).downloads:
                raise AddonError("unavailable", "add-on is not used for downloads")
            published = self.deliverer.pinned(track.song_id)
            if (
                pin is None
                and not fresh
                and published is not None
                and published.source is source
                and not self.deliverer._expired(published)
                and not choice.ready
            ):
                pin = published
            reused = pin is not None
            if pin is not None and pin.info.transport != "dash":
                # A bulk fetch may validate its own response without changing the play's
                # representation or inheriting a previous request's pacing wait.
                pin = replace(pin, checked=False, waited=0.0, first_byte_by=None, own_by=None)
            if pin is None:
                if choice.ready and choice.cached is not None and choice.cached.selector:
                    if self.deliverer._known_wrong(source, track):
                        raise _Missing("another recording (remembered)")
                    info = await source.addon.cached_stream(choice.cached.selector)
                    if info.transport == "hls":
                        raise _Failed("HLS streams are not supported")
                    if info.transport == "dash" and self.deliverer.dash is None:
                        raise _Failed(f"DASH is off ({self.deliverer.dash_off})")
                    pin = Pin(
                        track.song_id,
                        source,
                        choice.cached.selector,
                        info,
                        self.deliverer.clock(),
                        cached=True,
                    )
                    pin.seconds = track.duration_ms / 1000
                else:
                    hint = choice.availability.track_id if choice.availability else None
                    pin = await self.deliverer._resolve_at(source, track, budget, hint)
            if expect is None:
                self._pins[track.key] = pin, lease
            while len(self._pins) > 4096:
                self._pins.pop(next(iter(self._pins)))
            try:
                opened = await self.deliverer._request(pin, requested, head, deadline, expect)
            except _Changed:
                if not reused or choice.ready or pin.cached:
                    raise
                if _BULK_REUSE.get() or not lease.active or lease.owner is not None:
                    raise _Reacquire(
                        "a replacement link needs its own upstream admission", resolve=True
                    ) from None
                pin = await self.deliverer._resolve_at(source, track, budget)
                if expect is None:
                    self._pins[track.key] = pin, lease
                opened = await self.deliverer._request(pin, requested, head, deadline, expect)
            opened = await self.deliverer._verify(pin, opened, track, requested, deadline)
            self._pins[track.key] = pin, lease
            return pin, opened

    async def aclose(self) -> None:
        self._stopped = True
        self.changed()


async def report(progress: Progress | None, state: str) -> None:
    if progress is not None:
        try:
            await progress(state)
        except Exception as exc:
            raise ProgressError(exc) from None


def _held(opened: Opened, lease: Lease) -> Opened:
    original_close, original_body = opened.close, opened.body
    released = False

    async def close() -> None:
        nonlocal released
        try:
            with anyio.CancelScope(shield=True):
                await original_close()
        finally:
            if not released:
                released = True
                lease.close()

    async def body() -> AsyncIterator[bytes]:
        try:
            if original_body is not None:
                async for chunk in original_body:
                    yield chunk
        finally:
            await close()

    opened.close = close
    if original_body is not None:
        opened.body = body()
    return opened
