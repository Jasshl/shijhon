"""Keeping ``sound_facts`` in step with AudioMuse-AI's analysis.

A sync lists every analyzed song with a fingerprint of its analysis (``/api/sync``, a page of
1000 a request, cheap for AudioMuse-AI), fetches the analysis of the songs that are new or
whose fingerprint changed (100 a request, four requests at once), and removes what is gone:

- songs AudioMuse-AI no longer lists - each asked for once more before it goes, so that a
  song analyzed while the list was read is not mistaken for a missing one,
- songs that are silent placeholders now (their analysis, if any, is of silence),
- songs Shijhon's index of the library has seen go.

Work is saved as it comes, so a sync that is cut short (a stop, a failure, the time limit)
continues where the next one finds it. It runs in the background: once a while after startup
if the facts are older than the interval, then every interval.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import anyio

from shijhon.audiomuse import client as api
from shijhon.audiomuse.client import AudioMuseClient
from shijhon.audiomuse.facts import analysis
from shijhon.audiomuse.table import FactsTable
from shijhon.playlists.sound import SoundUnavailable

log = logging.getLogger(__name__)

MAX_PAGES = 5000  # of the list: five million songs
START_DELAY = 20.0  # seconds after startup before the first look
RETRY_SECONDS = 900.0  # after a failed sync
MAX_SYNC_SECONDS = 1800.0  # one sync's time
AT_ONCE = 2  # requests of a sync at once: the rest of the client's four are for playlists


@dataclass
class Report:
    listed: int = 0  # songs AudioMuse-AI lists
    fetched: int = 0  # ... whose analysis was fetched (new or changed)
    removed: int = 0  # songs whose facts were removed
    skipped: int = 0  # listed songs without facts worth keeping


class FactsSync:
    def __init__(
        self,
        client: AudioMuseClient,
        table: FactsTable,
        *,
        interval_seconds: float,
        library_id: str = "1",
        changed: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.time,
        start_delay: float | None = None,
        retry_seconds: float = RETRY_SECONDS,
        max_seconds: float = MAX_SYNC_SECONDS,
    ) -> None:
        self.client = client
        self.table = table
        self.interval = interval_seconds
        self.library_id = library_id
        self.changed = changed  # the facts changed (the percentiles are worked out again)
        self.clock = clock
        self.start_delay = START_DELAY if start_delay is None else start_delay
        self.retry_seconds = retry_seconds
        self.max_seconds = max_seconds
        self.error = ""  # why the last sync failed ("": it did not)
        self.finished = 0.0  # when the last sync was completed (0: since startup, none)
        self.running = False
        self._lock = anyio.Lock()

    async def run(self) -> None:
        """Background work: sync when the facts are older than the interval, and again every
        interval (sooner after a failure). Never ends by itself."""
        await anyio.sleep(self.start_delay)
        while True:
            last = max(self.finished, await self.table.last_synced())
            wait = last + self.interval - self.clock()
            if wait > 0 and not self.error:
                await anyio.sleep(wait)
                continue
            try:
                await self.sync()
            except SoundUnavailable:
                log.warning("sound analysis: the sync failed: %s", self.error)
                await anyio.sleep(min(self.retry_seconds, self.interval))
            except Exception as exc:
                log.warning("sound analysis: the sync failed (%s)", type(exc).__name__)
                self.error = "The sync failed."
                await anyio.sleep(min(self.retry_seconds, self.interval))

    async def sync(self) -> Report:
        """One sync now (a second one asked for meanwhile waits for it and does nothing)."""
        if self._lock.locked():
            async with self._lock:
                return Report()
        async with self._lock:
            self.running = True
            try:
                with anyio.fail_after(self.max_seconds):
                    report = await self._sync()
            except TimeoutError:
                self.error = "The sync took too long; it continues at the next one."
                raise SoundUnavailable(self.error) from None
            except SoundUnavailable as exc:
                self.error = str(exc)
                raise
            finally:
                self.running = False
            self.error = ""
            self.finished = self.clock()
            log.info(
                "sound analysis: %d songs listed, %d fetched, %d removed",
                report.listed,
                report.fetched,
                report.removed,
            )
            return report

    async def _sync(self) -> Report:
        report = Report()
        listed = await self._list()
        report.listed = len(listed)
        placeholders = await self.table.placeholders()
        stored = await self.table.fingerprints()
        wanted = {song: fp for song, fp in listed.items() if song not in placeholders}
        todo = [song for song, fp in wanted.items() if stored.get(song) != fp]
        fresh = set(todo)
        unchanged = [song for song in wanted if song in stored and song not in fresh]
        # New and changed songs.
        empty = await self._fetch(todo, report)
        report.skipped = len(empty)
        # Stored songs the list lacks are asked for once more: the list was read page by
        # page while songs were analyzed. What AudioMuse-AI still has is saved.
        lost = [song for song in stored if song not in listed and song not in placeholders]
        gone = await self._fetch(lost, report)
        # ... and what has no facts (any more), is a placeholder now, or has left the library.
        remove = empty | gone | placeholders | await self.table.gone(self.library_id)
        report.removed = await self.table.delete(sorted(remove & stored.keys()))
        if report.removed:
            self.changed()
        await self.table.confirm(unchanged, self.clock())
        return report

    async def _list(self) -> dict[str, str]:
        listed: dict[str, str] = {}
        for page in range(1, MAX_PAGES + 1):
            rows, more = await self.client.manifest(page)
            listed.update(rows)
            if not more:
                return listed
        raise SoundUnavailable(api.UNREADABLE)

    async def _fetch(self, songs: Sequence[str], report: Report) -> set[str]:
        """Fetch the facts of the songs, ``AT_ONCE`` requests at a time, and save each
        group as it comes. Returns the songs that have none. After a failed request no more
        are made; the groups under way are saved, and the failure is raised."""
        empty: set[str] = set()
        failed: list[SoundUnavailable] = []
        slots = anyio.Semaphore(AT_ONCE)

        async def ask(group: list[str]) -> None:
            async with slots:
                if failed:
                    return
                try:
                    tracks = await self.client.tracks(group)
                except SoundUnavailable as exc:
                    failed.append(exc)
                    return
            found = {track.id: analysis(track) for track in tracks if track.id in group}
            rows = [facts for facts in found.values() if facts is not None]
            await self.table.put(rows, self.clock())
            empty.update(song for song in group if found.get(song) is None)
            if rows:
                report.fetched += len(rows)
                self.changed()

        async with anyio.create_task_group() as tasks:
            for start in range(0, len(songs), api.IDS_PER_REQUEST):
                tasks.start_soon(ask, list(songs[start : start + api.IDS_PER_REQUEST]))
        if failed:
            raise failed[0]
        return empty
