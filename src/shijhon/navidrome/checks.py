"""Startup checks, and the one that guards every placeholder write.

- **Placeholder writes need ``Scanner.PurgeMissing = "never"``** (Navidrome's default).
  Otherwise a placeholder that is gone for a moment - taken out as unused, or while its
  file is swapped - would be purged with every user's favorites, ratings and playlist
  entries of it. The value comes from Navidrome's configuration as admins see it
  (``/api/config``, which Navidrome serves only while its ``DevUIShowConfig`` is on, the
  default); when that is not shown, from ``[navidrome] purge_missing`` (what the owner
  says Navidrome runs with). Until it is known to be "never", nothing is added or taken
  out (commits and fills answer with the reason), no placeholder's file is swapped
  (delivered audio in or out, a retag: download-first then streams the source format) and
  nothing is repaired. Every library write asks Navidrome again: its setting changes
  with a restart of it, which Shijhon does not see - an answer kept from before would
  not do. While Navidrome does not answer (it may start after Shijhon), that is asked
  again within seconds, not minutes.
- At startup, once Navidrome answers (waited for as long as it takes): its version
  (Shijhon is tested against one), the library (``library_id``) and the placeholder
  folder (writable), and repairs of what a stop in the middle of work can leave: new
  placeholders written but not recorded (taken out again), a swap of a placeholder's
  file interrupted (the file the database names is put in place), staged files left over,
  and a release being taken out (put back; the next daily check decides again). Each
  repair holds the locks a request's write would (the release's, the songs'), so requests
  served meanwhile never interleave with it; and, like every library write, the repairs
  wait while the gate above refuses. Delivered audio whose file is gone is the
  expiry's (``delivery/expiry.py``); an owned song backing placeholders that is gone, the
  play's (``delivery/intercept.py``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import anyio
import anyio.to_thread

from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.engine import PlaceholderEngine, ReplaceError
from shijhon.store import Store

log = logging.getLogger(__name__)

TESTED_VERSION = "0.64.2"  # the Navidrome version the suites run against
STALE_SECONDS = 3600.0  # a staged file older than this was left by a stop
UNREACHABLE_SECONDS = 5.0  # how long "Navidrome does not answer" is believed


class PlaceholderWrites:
    """Whether placeholders may be written (``Scanner.PurgeMissing`` is "never")."""

    def __init__(
        self,
        navidrome: NavidromeService,
        *,
        stated: str = "",
        recheck_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.navidrome = navidrome
        self.stated = stated.strip().lower()
        self.recheck = recheck_seconds
        self.clock = clock
        self._until = 0.0
        self._refused: str | None = "not checked yet"
        self._said: str | None = ""  # the last state logged

    async def refused(self, *, fresh: bool = False) -> str | None:
        """None when placeholders may be written, else why not. ``fresh``: asked of
        Navidrome now - as before every library write (the engine); otherwise a value read
        is kept a while (for displays)."""
        now = self.clock()
        if not fresh and now < self._until:
            return self._refused
        value, source, answered = await self._purge_missing()
        if value == "never":
            refused = None
        elif value:
            refused = (
                f'Navidrome\'s Scanner.PurgeMissing is "{value}" ({source}): placeholders'
                " are not written - Navidrome would purge the favorites, ratings and"
                ' playlist entries of placeholders it cannot find; set it to "never"'
            )
        elif not answered:
            refused = f"Navidrome does not answer ({source}): placeholders are not written now"
        else:
            refused = (
                "Navidrome's Scanner.PurgeMissing is not known (its configuration is not"
                " shown: Navidrome's DevUIShowConfig is off): placeholders are not written;"
                ' set [navidrome] purge_missing = "never" if Navidrome runs with that'
            )
        self._refused = refused
        self._until = now + (self.recheck if answered else UNREACHABLE_SECONDS)
        if refused != self._said:
            if refused is None:
                log.info("Navidrome keeps missing files (%s): placeholders are written", source)
            elif answered:
                log.error("%s", refused)
            else:
                log.warning("%s", refused)
            self._said = refused
        return refused

    async def _purge_missing(self) -> tuple[str, str, bool]:
        """(value, where it came from or why not, whether Navidrome answered). Navidrome
        answers 404 when it does not show its configuration."""
        try:
            config = await self.navidrome.native_json("GET", "config/")
        except NavidromeError as exc:
            if exc.status not in (403, 404):
                return "", str(exc), False
            config = None
        except Exception as exc:  # never an error for the request that asked
            return "", type(exc).__name__, False
        settings = config.get("config") if isinstance(config, dict) else None
        scanner = settings.get("Scanner") if isinstance(settings, dict) else None
        value = scanner.get("PurgeMissing") if isinstance(scanner, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip().lower(), "Navidrome's configuration", True
        if self.stated:
            return self.stated, "[navidrome] purge_missing", True
        return "", "", True


class StartupChecks:
    def __init__(
        self,
        navidrome: NavidromeService,
        engine: PlaceholderEngine,
        store: Store,
        writes: PlaceholderWrites,
        *,
        wait_seconds: float = 300.0,
        retry_seconds: tuple[float, float] = (5.0, 30.0),
        repair_seconds: float = 60.0,
    ) -> None:
        self.navidrome = navidrome
        self.engine = engine
        self.store = store
        self.writes = writes
        self.wait = wait_seconds  # then an error is logged, and Navidrome asked less often
        self.retry = retry_seconds
        self.repair = repair_seconds

    async def run(self) -> None:
        """Once Navidrome answers (it may start after Shijhon, or take long to migrate its
        database after an upgrade: waited for as long as it takes): check and repair."""
        deadline = time.monotonic() + self.wait
        warned = False
        while True:
            try:
                pong = await self.navidrome.subsonic("ping")
                break
            except NavidromeError as exc:
                if not warned and time.monotonic() > deadline:
                    log.error("startup checks: Navidrome does not answer (%s); the repairs"
                              " wait for it", exc)  # fmt: skip
                    warned = True
                await anyio.sleep(self.retry[1] if warned else self.retry[0])
        version = str(pong.get("serverVersion") or "?").split(" ")[0]
        if version != TESTED_VERSION:
            log.warning(
                "Navidrome %s: Shijhon is tested against %s (run the canary suite before an"
                " upgrade: suites D, E and C)",
                version,
                TESTED_VERSION,
            )
        await self._library()
        await self._repair(self._stale)
        # The repairs change the library: only while it may be written - asked again
        # until then (Navidrome's setting changes with a restart of it).
        waiting = False
        while await self.writes.refused(fresh=True):
            if not waiting:
                log.warning("the startup repairs wait until placeholders may be written")
                waiting = True
            await anyio.sleep(self.retry[1])
        engine = self.engine
        for repair in (engine.repair_pending, self._staging, engine.repair_removals):
            await self._repair(repair)

    async def _repair(self, repair: Callable[[], Awaitable[object]]) -> None:
        try:
            await repair()
        except Exception as exc:  # the others still run
            log.warning("startup repair %s failed: %s", repair.__name__, type(exc).__name__)

    async def keep_repairing(self) -> None:
        """What a rollback, an undone swap or a repair left unfinished while running -
        Navidrome did not confirm it, a file could not go - is finished as soon as that
        works: new placeholders left half written, swaps with a backup still in the staging
        folder, releases still marked as being taken out. Looked at every minute (the app's
        background task)."""
        while True:
            await anyio.sleep(self.repair)
            try:
                swaps = await anyio.to_thread.run_sync(self._interrupted)
                marked = await self.engine.marked()
                if not (self.engine.left() or swaps or marked):
                    continue
                if await self.writes.refused(fresh=True):
                    continue
                await self.engine.repair_pending()
                if swaps:
                    await self._staging()
                if marked:  # (one being taken out now is waited for: its lock)
                    await self.engine.repair_removals()
            except Exception as exc:  # the next round tries again
                log.warning("unfinished library writes not repaired: %s", type(exc).__name__)

    async def _library(self) -> None:
        library = self.navidrome.library_id
        try:
            folders = (await self.navidrome.subsonic("getMusicFolders"))["musicFolders"]
            ids = {int(f["id"]) for f in folders.get("musicFolder", []) if "id" in f}
        except (NavidromeError, KeyError, TypeError, ValueError):
            ids = set()
        if ids and library not in ids:
            log.error(
                "Navidrome has no library %d (it has %s): set [navidrome] library_id",
                library,
                ", ".join(str(i) for i in sorted(ids)),
            )
        try:
            await anyio.to_thread.run_sync(self.engine.layout.ensure)
        except OSError as exc:
            log.error(
                "the placeholder folder cannot be written (%s): check [navidrome]"
                " library_path and the folder's permissions",
                exc.strerror or type(exc).__name__,
            )

    async def _staging(self) -> None:
        """Swaps a stop interrupted (a backup in the staging folder): the file the database
        names ends up in place. Each on its own: one Navidrome does not confirm (its scan
        fails, or takes too long) is tried again a minute later, and the others still go."""
        recovered = 0
        for song_id in await anyio.to_thread.run_sync(self._interrupted):
            try:
                recovered += await self.engine.recover_swap(song_id)
            except Exception as exc:  # e.g. Navidrome: the others still get their turn
                why = str(exc) if isinstance(exc, (ReplaceError, OSError)) else type(exc).__name__
                log.warning("interrupted swap of %s not recovered: %s", song_id, why)
        if recovered:
            log.info("%d interrupted swap(s) put right", recovered)

    def _interrupted(self) -> list[str]:
        """The songs with a backup in the staging folder."""
        staging = self.engine.layout.staging
        backups = staging.glob("backup-*") if staging.exists() else []
        return sorted({b.name.removeprefix("backup-").split(".", 1)[0] for b in backups})

    async def _stale(self) -> None:
        """Staged files a stop left behind (older than an hour; backups are the swaps', and
        covers set aside the removals')."""
        staging = self.engine.layout.staging
        now = time.time()

        def stale() -> int:
            removed = 0
            for path in staging.glob("*") if staging.exists() else []:
                if path.name.startswith(("backup-", "removing-", ".nd")) or not path.is_file():
                    continue
                if now - path.stat().st_mtime > STALE_SECONDS:
                    path.unlink(missing_ok=True)
                    removed += 1
            return removed

        removed = await anyio.to_thread.run_sync(stale)
        if removed:
            log.info("startup: %d staged file(s) left over removed", removed)
