"""Saved settings that take effect at once: each entry names the running services'
attributes that hold the setting and are read on every use (paths from ``Services``). A
setting without an entry applies after a restart (the pages say so), which is also the
safe default for settings added later. An attribute that is gone raises: the setting is
then reported as waiting for a restart instead of silently doing nothing.

What is not here and why: the catalog's kind and its adapter's settings (its region, its
credentials, its request rate) build the catalog and the fill scope at startup; its cache
time and timeout live in private or copied state; ``twins`` is copied into four objects; the
cover cache exists only when its size was above 0 at startup; the delivered-audio expiry
does not run when both its limits were 0; the per-user limits' semaphores are sized when a
user's record is made (and idle records are dropped, so a change would reach users one by
one). Changes to those need a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from shijhon.delivery.pacing import Limits

if TYPE_CHECKING:
    from shijhon.app import Services

log = logging.getLogger(__name__)


def ahead_wait(budget_seconds: float, max_wait_seconds: float) -> float:
    """How long a fetch ahead waits for its turn: while a routing still fits the cap."""
    return max(1.0, max_wait_seconds - budget_seconds)


def addon_limits(section: Any) -> Limits:
    """What one add-on is sent in all, unless it has limits of its own."""
    return Limits(
        section.addon_requests_per_second,
        section.addon_request_burst,
        section.addon_audio_openings,
    )


@dataclass(frozen=True)
class Target:
    path: str  # e.g. "deliverer.settings.budget_seconds"; an optional part may be None
    value: Callable[[Any], Any]  # from the section's settings
    optional: bool = False  # a part of the path may be None (no catalog, no warm-ahead)
    # Whether this change can apply at once (the attribute's current value, the new one);
    # when not, the setting waits for a restart.
    live_if: Callable[[Any, Any], bool] | None = None
    # For a change that waits for a restart: a value to set meanwhile, or None.
    interim: Callable[[Any, Any], Any] | None = None


def _pass_mode(current: Any, wanted: Any) -> bool:
    """The library pass reads its mode when a run starts: between dry run and on it changes
    from the next run. "Off" stops it only at startup (set live, the next run would fill)
    and a pass that started off is not running: both need a restart."""
    return bool(current != "off" and wanted != "off")


def _pass_interim(current: Any, wanted: Any) -> Any:
    """ "off" chosen while the pass runs "on" makes it fill nothing from
    its next run - dry runs until the restart turns it off. (A run under way keeps the mode
    it started with: LibraryPass.run_once reads it once.)"""
    return "dry_run" if current == "on" and wanted == "off" else None


def _policy(section: Any) -> Any:
    from shijhon.fill.fills import FillPolicy

    return FillPolicy(section.auto_min_songs, section.auto_min_share)


def _same(key: str) -> Callable[[Any], Any]:
    return lambda section: getattr(section, key)


def _playback(key: str) -> tuple[Target, ...]:
    # DeliverySettings fields that PlaybackSettings holds under the same name; the
    # deliverer reads them on every use (delivery/playback.py).
    return (Target(f"deliverer.settings.{key}", _same(key)),)


def _ahead(section: Any) -> float:
    return ahead_wait(section.budget_seconds, section.max_wait_seconds)


LIVE: dict[tuple[str, str], tuple[Target, ...]] = {
    **{
        ("delivery", key): _playback(key)
        for key in (
            "max_attempts",
            "cooldown_seconds",
            "seek_timeout_seconds",
            "routing",
            "reliable_source",
            "primary_source",
            "primary_budget_seconds",
            "primary_cooldown_timeouts",
            "primary_cooldown_switches",
            "cooldown_errors",
            "availability_timeout_seconds",
            "primary_miss_hours",
            "primary_release_miss_minutes",
            "prepare_when_not_ready",
            "warm_ahead_depth",
            "warm_ahead_budget_seconds",
            "reliable_lookup_after_seconds",
            "length_tolerance_seconds",
            "length_tolerance_percent",
            "retry_skip_seconds",
            "dash_quality_from",
            "dash_quality_to",
            "dash_start",
            "dash_segments_at_once",
        )
    },
    # Read on every download-first fetch (unlike the per-user semaphores' sizes).
    ("delivery", "user_downloads_per_hour"): (
        Target("download_first.limits.per_hour", _same("user_downloads_per_hour"), optional=True),
    ),
    # Each add-on origin's limits: from the next request on, those waiting too.
    **{
        ("delivery", key): (Target("sources.paces.defaults", addon_limits),)
        for key in ("addon_requests_per_second", "addon_request_burst", "addon_audio_openings")
    },
    # Read at every search and artist page.
    ("search", "budget_seconds"): (
        Target("additions.budget", _same("budget_seconds"), optional=True),
    ),
    ("delivery", "user_download_burst"): (
        Target("download_first.limits.burst", _same("user_download_burst"), optional=True),
    ),
    ("delivery", "warm_ahead_jobs"): (
        Target("interceptor.warm.jobs", _same("warm_ahead_jobs"), optional=True),
    ),
    # (None while DASH is off.)
    ("delivery", "dash_joins_at_once"): (
        Target("deliverer.dash.joins_at_once", _same("dash_joins_at_once"), optional=True),
    ),
    ("delivery", "budget_seconds"): (
        *_playback("budget_seconds"),
        Target("interceptor.ahead.wait", _ahead, optional=True),
    ),
    ("delivery", "max_wait_seconds"): (
        *_playback("max_wait_seconds"),
        Target("interceptor.ahead.wait", _ahead, optional=True),
    ),
    ("delivery", "pin_ttl_seconds"): (
        *_playback("pin_ttl_seconds"),
        Target("sources.retire_after", _same("pin_ttl_seconds")),
    ),
    ("delivery", "request_timeout_seconds"): (
        Target("sources.request_timeout", _same("request_timeout_seconds")),
    ),
    ("delivery", "download_timeout_seconds"): (
        Target("download_first.timeout", _same("download_timeout_seconds")),
    ),
    ("delivery", "warm_ahead_delay_seconds"): (
        Target("interceptor.warm.delay", _same("warm_ahead_delay_seconds"), optional=True),
    ),
    ("delivery", "prefetch_memory_seconds"): (
        Target(
            "interceptor.warm.listening.memory", _same("prefetch_memory_seconds"), optional=True
        ),
    ),
    ("delivery", "ahead_window_seconds"): (
        Target("interceptor.ahead.window", _same("ahead_window_seconds"), optional=True),
    ),
    ("catalog", "artwork_size"): (
        Target("commits.artwork_size", _same("artwork_size"), optional=True),
    ),
    # [fill]: the fills and the library pass read these on every use (None without a
    # catalog or with filling off).
    ("fill", "auto_min_songs"): (Target("fills.policy", _policy, optional=True),),
    ("fill", "auto_min_share"): (Target("fills.policy", _policy, optional=True),),
    ("fill", "library_pass"): (
        Target(
            "library_pass.mode",
            _same("library_pass"),
            optional=True,
            live_if=_pass_mode,
            interim=_pass_interim,
        ),
    ),
    ("fill", "open_budget_seconds"): (
        Target("fills.budget", _same("open_budget_seconds"), optional=True),
    ),
    ("fill", "sync_albums"): (Target("fills.syncs.limit", _same("sync_albums"), optional=True),),
    ("fill", "sync_seconds"): (Target("fills.syncs.window", _same("sync_seconds"), optional=True),),
    ("fill", "retry_hours"): (
        Target("fills.retry_seconds", lambda s: s.retry_hours * 3600, optional=True),
    ),
    ("fill", "background_pause_seconds"): (
        Target("fills.pause", _same("background_pause_seconds"), optional=True),
        Target("library_pass.pause", _same("background_pause_seconds"), optional=True),
    ),
    # [cleanup]: the daily check reads these at every check.
    **{
        ("cleanup", key): (Target(f"cleanup.{key}", _same(key), optional=True),)
        for key in ("mode", "unused_days", "catalog_albums", "fills")
    },
    # From the pass's next wait (the one running keeps its length).
    ("fill", "pass_interval_hours"): (
        Target(
            "library_pass.interval_seconds", lambda s: s.pass_interval_hours * 3600, optional=True
        ),
    ),
}


async def _relimit(services: Services) -> None:
    services.sources.limit(services.sources.paces.defaults)


async def _recool(services: Services) -> None:
    services.sources.cool(services.deliverer.settings.cooldown_seconds)


# After these change, work is needed besides setting the attributes.
_AFTER: dict[tuple[str, str], Callable[[Services], Awaitable[None]]] = {
    # New add-on clients with the new timeout; pinned plays keep theirs.
    ("delivery", "request_timeout_seconds"): lambda services: services.sources.invalidate(),
    # The add-ons without limits of their own get the new ones.
    # ... and the time an add-on is left alone after a rate limit that names none.
    ("delivery", "cooldown_seconds"): _recool,
    ("delivery", "addon_requests_per_second"): _relimit,
    ("delivery", "addon_request_burst"): _relimit,
    ("delivery", "addon_audio_openings"): _relimit,
}


def is_live(section: str, key: str) -> bool:
    return (section, key) in LIVE


def owner(services: Services, path: str, *, optional: bool) -> tuple[Any, str] | None:
    """The object holding a target's attribute, and the attribute's name (None: an optional
    part is absent). Raises AttributeError when the attribute is gone."""
    *parents, name = path.split(".")
    holder: Any = services
    for part in parents:
        holder = getattr(holder, part)
        if holder is None:
            if optional:
                return None
            raise AttributeError(f"{path}: {part} is None")
    if not hasattr(holder, name):
        raise AttributeError(f"{path} does not exist")
    return holder, name


async def apply(
    services: Services,
    section: str,
    keys: list[str],
    values: Any,
    interim: list[str] | None = None,
) -> list[str]:
    """Apply the live ones of ``keys``; returns those applied. One whose target is gone is
    logged and left out (it then waits for a restart); one that waits for a restart may set
    a value meanwhile (its target's ``interim``; such keys are added to ``interim``)."""
    applied = []
    for key in keys:
        targets = LIVE.get((section, key))
        if targets is None:
            continue
        try:
            found = [owner(services, t.path, optional=t.optional) for t in targets]
            if not all(
                t.live_if is None
                or (h is not None and t.live_if(getattr(h[0], h[1]), t.value(values)))
                for t, h in zip(targets, found, strict=True)
            ):
                # This change waits for a restart (also when its holder is absent); some
                # set a value meanwhile.
                for target, holder in zip(targets, found, strict=True):
                    if target.interim is None or holder is None:
                        continue
                    meanwhile = target.interim(getattr(holder[0], holder[1]), target.value(values))
                    if meanwhile is not None:
                        setattr(holder[0], holder[1], meanwhile)
                        if interim is not None:
                            interim.append(key)
                continue
            for target, holder in zip(targets, found, strict=True):
                if holder is not None:
                    setattr(holder[0], holder[1], target.value(values))
            after = _AFTER.get((section, key))
            if after is not None:
                await after(services)
        except Exception as exc:  # the others still apply; this one waits for a restart
            log.error(
                "dashboard: %s.%s cannot apply at once (%s: %s); it applies after a restart",
                section,
                key,
                type(exc).__name__,
                exc,
            )
            continue
        applied.append(key)
    return applied
