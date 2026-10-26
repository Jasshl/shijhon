"""AudioMuse-AI as the sound analyzer of described playlists (``playlists.sound.Sound``).

Facts come from Shijhon's own table (``sound_facts``), kept in step by ``FactsSync``;
similar songs, searches by description and paths come from AudioMuse-AI when asked. Every
answer is a list of Navidrome song IDs of songs that have facts, in AudioMuse-AI's order.
Every request is bounded in time and fails as ``SoundUnavailable``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Sequence

import anyio

from shijhon.audiomuse import client as api
from shijhon.audiomuse.client import AudioMuseClient, Concepts
from shijhon.audiomuse.facts import Distribution
from shijhon.audiomuse.sync import FactsSync
from shijhon.audiomuse.table import FactsTable
from shijhon.playlists.sound import SongSound, SoundStatus, SoundUnavailable
from shijhon.store import Store

log = logging.getLogger(__name__)

MAX_SEEDS = 50
MAX_IDS = 1000
MAX_ID_LENGTH = 200
PROBE_SECONDS = 30.0  # how long "reachable" is remembered
CONCEPTS_SECONDS = 600.0  # how long a vocabulary that is unavailable is remembered


class AudioMuse:
    def __init__(
        self,
        client: AudioMuseClient,
        store: Store,
        *,
        sync_hours: float,
        library_id: str = "1",
        clock: Callable[[], float] = time.time,
        start_delay: float | None = None,
    ) -> None:
        self.client = client
        self.table = FactsTable(store)
        self.clock = clock
        self.sync = FactsSync(
            client,
            self.table,
            interval_seconds=sync_hours * 3600,
            library_id=library_id,
            changed=self._changed,
            clock=clock,
            start_delay=start_delay,
        )
        self._distribution: Distribution | None = None
        self._generation = 0  # raised whenever a sync changes the facts
        self._building = anyio.Lock()
        self._concepts: Concepts | None = None
        self._concepts_at = 0.0
        self._probe: tuple[float, str] | None = None  # (when, the problem or "")

    async def run(self) -> None:
        """Background work for the app's lifetime: the periodic sync."""
        await self.sync.run()

    async def aclose(self) -> None:
        await self.client.aclose()

    def _changed(self) -> None:
        self._generation += 1
        self._distribution = None

    # --- facts --------------------------------------------------------------------------

    async def facts(self, song_ids: Sequence[str]) -> dict[str, SongSound]:
        ids = _ids(song_ids, MAX_IDS)
        rows = await self.table.rows(ids)
        if not rows:
            return {}
        distribution = await self._distributions()
        return {song: distribution.sound(row) for song, row in rows.items()}

    async def _distributions(self) -> Distribution:
        """The percentiles' base: the songs with facts, worked out again when a sync changed
        them."""
        held = self._distribution
        if held is not None:
            return held
        async with self._building:
            if self._distribution is None:
                generation = self._generation
                built = await self.table.distribution()
                if generation == self._generation:  # (not out of date already)
                    self._distribution = built
                return built
            return self._distribution

    # --- sound --------------------------------------------------------------------------

    async def similar(
        self, seeds: Sequence[str], *, avoid: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        liked, disliked = _ids(seeds, MAX_SEEDS), _ids(avoid, MAX_SEEDS)
        count = _count(count)
        if not liked or count == 0:
            return []
        ask = count * 2 + len(liked) + len(disliked)
        if len(liked) == 1 and not disliked:
            found = await self.client.similar_tracks(liked[0], ask)
        else:
            found = await self.client.alchemy(liked, disliked, ask)
        return await self._ours(found, count, without={*liked, *disliked})

    async def search(
        self, text: str, *, more: Sequence[str] = (), less: Sequence[str] = (), count: int = 50
    ) -> list[str]:
        text = " ".join(text.split())[:500]
        count = _count(count)
        if not text or count == 0:
            return []
        steering = await self._steering(more, less)
        found = await self.client.search(text, count * 2, steering)
        return await self._ours(found, count)

    async def path(self, start: str, end: str, *, steps: int = 12) -> list[str]:
        first, last = _ids([start], 1), _ids([end], 1)
        if not first or not last:
            return []
        if first == last:
            return first if await self.table.present(first) else []
        found = await self.client.path(first[0], last[0], steps)
        ours = await self.table.present(found)
        return [song for song in found if song in ours or song in (first[0], last[0])]

    async def _ours(
        self, found: Sequence[str], count: int, *, without: Collection[str] = ()
    ) -> list[str]:
        """The first ``count`` songs of AudioMuse-AI's answer that have facts here, and are
        not the ones asked about."""
        wanted = [song for song in found if song not in without]
        ours = await self.table.present(wanted)
        return [song for song in wanted if song in ours][:count]

    async def _steering(self, more: Sequence[str], less: Sequence[str]) -> list[dict[str, str]]:
        """The words as steering: the ones its vocabulary has, spelled as it has them. Unknown
        words, a word asked for both ways, and what is beyond its limit are left out."""
        if not more and not less:
            return []
        try:
            concepts = await self._vocabulary()
        except SoundUnavailable:
            return []  # (the search itself says if AudioMuse-AI cannot be asked)
        if not concepts.available:
            return []
        known = {term.casefold(): term for term in concepts.terms}
        wanted: dict[str, str] = {}
        for direction, words in (("more", more), ("less", less)):
            for word in words:
                term = known.get(" ".join(str(word).split()).casefold())
                if term is None:
                    continue
                if wanted.get(term, direction) != direction:
                    wanted[term] = ""  # both ways: neither
                else:
                    wanted[term] = direction
        return [
            {"term": term, "direction": direction}
            for term, direction in wanted.items()
            if direction
        ][: concepts.most]

    async def _vocabulary(self) -> Concepts:
        """Fetched once; asked again after a while when it was unavailable (the steering
        models may still have been loading)."""
        held = self._concepts
        if held is not None and (
            held.available or self.clock() - self._concepts_at < CONCEPTS_SECONDS
        ):
            return held
        fetched = await self.client.concepts()
        self._concepts, self._concepts_at = fetched, self.clock()
        return fetched

    # --- status -------------------------------------------------------------------------

    async def status(self) -> SoundStatus:
        problem = await self._reachable()
        error = problem or self.sync.error
        return SoundStatus(
            configured=True,
            reachable=not problem,
            songs=await self.table.count(),
            synced=max(self.sync.finished, await self.table.last_synced()),
            error=error,
        )

    async def _reachable(self) -> str:
        """ "" when AudioMuse-AI answers (and takes the token), else why not; looked at once
        in a while."""
        now = self.clock()
        if self._probe is not None and now - self._probe[0] < PROBE_SECONDS:
            return self._probe[1]
        try:
            await self.client.version()
            problem = ""
        except SoundUnavailable as exc:
            problem = str(exc)
        self._probe = (now, problem)
        return problem


def _ids(values: Sequence[str], limit: int) -> list[str]:
    """The song IDs, each once, in order; nothing that is not a plain ID."""
    found: dict[str, None] = {}
    for value in values:
        if isinstance(value, str) and value and len(value) <= MAX_ID_LENGTH and value.isprintable():
            found.setdefault(value)
        if len(found) >= limit:
            break
    return list(found)


def _count(count: int) -> int:
    return max(0, min(int(count), api.MAX_RESULTS))
