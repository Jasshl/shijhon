"""Placeholders written with a joined credit ("A & B") where the catalog names each artist:
rewritten in place with each artist listed (``tags.listed_artists``), after a start and then
every few hours.

Navidrome credits a song to every artist of its ``artists`` tag (and an album to every one of
``albumartists``), and to the ``artist`` tag as one artist without it: a placeholder from
before those tags were written made "A & B" an artist of its own (no picture, no page of A
or B). The release's recorded catalog data names the tracks and the album's artist items;
the tracks' items it lacks are asked for in batches (``PlaceholderEngine.credits``).

A catalog-only album credited to several artists gets ``albumartists``
(``PlaceholderEngine.list_album_artists``: the album stays the album) and all its silent
placeholders are rewritten; otherwise only those whose own credit gains its artists. A
placeholder of such an album that a failed retag or a stop left without them is rewritten
at a later pass (its own tags lack the album's ``albumartists``). Each is
rewritten by ``PlaceholderEngine.recredit`` (its song ID, its album and the owner's recording
playing for it kept): only its credits, on the song as its row has it when it is written -
the tracks here are read before the catalog is asked, and a song corrected meanwhile (its
length, by the library pass's reading of its track list) keeps that; one whose credit is
another by then is left for a later pass. Delivered audio keeps its tags until it is a
placeholder again. A
credit the catalog gives no items for, or not as many as it names, stays as it was, and is
asked about again after the next start. A new placeholder is written with what the catalog's answer
names (a write makes no extra catalog requests), and listed here afterwards.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import anyio
import anyio.lowlevel

from shijhon.artist_lists.names import maybe_joined
from shijhon.artist_lists.sources import Skips, Sources, Subject, Unavailable
from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, release_from_data
from shijhon.navidrome.client import NavidromeError
from shijhon.placeholders import tags as tagging
from shijhon.placeholders.engine import MaterializeError, PlaceholderEngine, ReplaceError
from shijhon.store import Store

log = logging.getLogger(__name__)

BATCH = 100  # tracks whose artist items are asked for together
BATCH_SECONDS = 60.0  # ... at most this long (in the background: nobody waits)
START_DELAY_SECONDS = 120.0  # after a start: the startup checks and first requests go first
REPEAT_SECONDS = 6 * 3600.0  # placeholders written since (a write asks the catalog nothing)
PAUSE_SECONDS = 1.0  # between two credits asked of the add-on catalogs
UNRESOLVED_SECONDS = 30 * 86400.0  # no list from the add-on catalogs: asked again after
FAILED_SECONDS = 86400.0  # an add-on catalog failed: asked again after
FAILURES_IN_A_ROW = 3  # the add-on catalogs are not asked again in this pass
RELEASES_AT_ONCE = 100  # releases whose recorded data is read together


Credits = Callable[[Sequence[CatalogTrack]], Awaitable[Mapping[CatalogRef, tuple[CatalogRef, ...]]]]


def catalog_credits(catalog: Catalog) -> Credits:
    """``PlaceholderEngine.credits`` from a catalog: the artist items of its own songs
    (``Catalog.artists_of``); none when it cannot say."""

    async def credits(
        tracks: Sequence[CatalogTrack],
    ) -> Mapping[CatalogRef, tuple[CatalogRef, ...]]:
        mine = [t.ref for t in tracks if t.ref.catalog == catalog.key]
        if not mine:
            return {}
        try:
            found = await catalog.artists_of(tuple(r.id for r in mine), ())
        except CatalogError as exc:
            log.info("artists of %d song(s) unavailable: %s", len(mine), exc.reason)
            return {}
        return {r: refs for r in mine if (refs := found.get(f"songs:{r.id}"))}

    return credits


class CreditRepair:
    def __init__(
        self,
        engine: PlaceholderEngine,
        store: Store,
        *,
        catalog: str | None,
        stopping: Callable[[], bool] = lambda: False,
        delay_seconds: float = START_DELAY_SECONDS,
    ) -> None:
        self.engine = engine
        self.store = store
        self.catalog = catalog  # the current catalog's key: other catalogs' items are not asked
        self.stopping = stopping
        self.delay_seconds = delay_seconds
        # Tracks whose artist items were asked for since the start, with the credit they
        # had then: not again (an add-on catalog answers one song a request), however the
        # answer was. A track whose credit changed since is asked for its new credit.
        self._asked: set[tuple[CatalogRef, str]] = set()
        # The add-on catalogs' lists, asked while ``lookups()`` (the artist lists' setting).
        self.lists: Sources | None = None
        self.lookups: Callable[[], bool] = lambda: False
        self.clock: Callable[[], float] = time.time
        self.pause = PAUSE_SECONDS

    async def run(self) -> None:
        await anyio.sleep(self.delay_seconds)
        while not self.stopping():
            try:
                await self.repair()
            except Exception:
                # (Whatever went wrong, the next pass asks again: the worker stays.)
                log.exception("credits: the pass failed")
            await anyio.sleep(REPEAT_SECONDS)

    async def candidates(self) -> list[_Release]:
        """The releases with silent placeholders to rewrite: a catalog-only album credited
        to several artists without ``albumartists`` (all its silent placeholders), one with
        them whose placeholders lack them (a pass cut short), or placeholders without an
        ``artists`` tag whose credit may name several artists.

        Read through a snapshot (no store lock), each release's recorded data once and a
        few releases at a time."""
        found: list[_Release] = []
        async with self.store.snapshot() as conn:
            async with conn.execute(
                "SELECT p.song_id, p.track_ref, p.release_ref,"
                " json_extract(p.tags, '$.artists') IS NOT NULL AS listed,"
                " json_extract(p.tags, '$.albumartists') IS NOT NULL AS album_listed"
                " FROM placeholders p JOIN releases r ON r.ref = p.release_ref"
                " WHERE p.state = 'placeholder' AND r.removing_at IS NULL"
                " ORDER BY p.release_ref"
            ) as cursor:
                rows = await cursor.fetchall()
            songs: dict[str, list[tuple[str, str, bool, bool]]] = {}
            for row in rows:
                songs.setdefault(str(row["release_ref"]), []).append(
                    (
                        str(row["song_id"]),
                        str(row["track_ref"]),
                        bool(row["listed"]),
                        bool(row["album_listed"]),
                    )
                )
            del rows
            refs = list(songs)
            for start in range(0, len(refs), RELEASES_AT_ONCE):
                page = refs[start : start + RELEASES_AT_ONCE]
                async with conn.execute(
                    "SELECT ref, data, album_tags, owned_album_id FROM releases"  # noqa: S608
                    f" WHERE ref IN ({', '.join('?' * len(page))})",
                    page,
                ) as cursor:
                    stored = {str(r["ref"]): r for r in await cursor.fetchall()}
                for ref in page:
                    entry = _release(stored[ref], songs[ref]) if ref in stored else None
                    if entry is not None:
                        found.append(entry)
                await anyio.lowlevel.checkpoint()
        return found

    async def repair(self) -> int:
        """Rewrite what can be; returns how many placeholders were rewritten."""
        releases = await self.candidates()
        if not releases:
            return 0
        await self._ask(releases)
        rewritten = unknown = failed = 0
        self._addon_failures = 0
        self._addon_asked = 0
        self._skips = Skips()
        for entry in releases:
            if self.stopping():
                break
            before = (
                await self.engine.list_album_artists(entry.ref, entry.album)
                if entry.album
                else None
            )
            for song_id, track in entry.songs:
                if self.stopping():
                    break
                listed = tagging.track_artists(track, entry.release)
                if before is None and listed is None:
                    names = await self._addon_list(song_id, track)
                    if names is not None:
                        track = replace(track, artists=names)
                    elif song_id not in entry.pending:
                        unknown += 1
                        continue
                try:
                    if await self.engine.recredit(song_id, track):
                        rewritten += 1
                except (ReplaceError, MaterializeError, NavidromeError) as exc:
                    failed += 1
                    log.debug("credits of placeholder %s not rewritten: %s", song_id, exc)
                    if before is not None and "album changed" in str(exc):
                        # (The album would not stay one: its tags as they were.)
                        await self.engine.set_album_tags(entry.ref, json.loads(before))
                        break
        log.info(
            "credits: %d placeholder(s) rewritten with each artist; %d without the catalog's"
            " artists, %d left for the next start",
            rewritten,
            unknown,
            failed,
        )
        return rewritten

    async def _ask(self, releases: list[_Release]) -> None:
        """The tracks' artist items the catalog has, asked for in batches (in place)."""
        wanting = [
            (entry, i)
            for entry in releases
            for i, (_, track) in enumerate(entry.songs)
            if self._askable(track.ref)
            and (track.ref, track.artist) not in self._asked
            and tagging.needs_credits(track)
        ]
        for start in range(0, len(wanting), BATCH):
            if self.stopping():
                return
            batch = wanting[start : start + BATCH]
            tracks = [entry.songs[i][1] for entry, i in batch]
            self._asked.update((t.ref, t.artist) for t in tracks)
            credited = {t.ref: t for t in await self.engine.credited(tracks, BATCH_SECONDS)}
            for entry, i in batch:
                song_id, track = entry.songs[i]
                entry.songs[i] = (song_id, credited.get(track.ref, track))

    async def _addon_list(self, song_id: str, track: CatalogTrack) -> tuple[str, ...] | None:
        """The add-on catalogs' list for a credit the catalog could not split (module
        docstring), when due and its names are exactly the credit."""
        if (
            self.lists is None
            or not self.lookups()
            or self._addon_failures >= FAILURES_IN_A_ROW
            or not maybe_joined(track.artist)
        ):
            return None
        row = await self.store.fetchone(
            "SELECT state, artist, retry_at, artists FROM artist_lists WHERE key = ?", [song_id]
        )
        if row is not None and row["artist"] == track.artist:
            if row["state"] == "written" and row["artists"]:
                return tuple(json.loads(row["artists"]))  # (a retag that did not finish)
            if float(row["retry_at"] or 0) > self.clock():
                return None
        if self._addon_asked:
            await anyio.sleep(self.pause)
        self._addon_asked += 1
        isrcs = (track.isrc,) if track.isrc else ()
        subject = Subject(song_id, track.artist, track.title, track.duration_ms, isrcs)
        try:
            found = await self.lists.resolve(subject, link=False, skips=self._skips)
        except (Unavailable, CatalogError) as exc:
            if not (isinstance(exc, Unavailable) and exc.answered):
                self._addon_failures += 1
            await self._note(song_id, track.artist, "failed", "", None, FAILED_SECONDS)
            return None
        self._addon_failures = 0
        if found.state != "resolved":
            await self._note(song_id, track.artist, found.state, "", None, UNRESOLVED_SECONDS)
            return None
        await self._note(song_id, track.artist, "written", found.source, found.names, None)
        return found.names

    async def _note(
        self,
        song_id: str,
        artist: str,
        state: str,
        source: str,
        names: tuple[str, ...] | None,
        retry: float | None,
    ) -> None:
        row = await self.store.fetchone(
            "SELECT path FROM placeholders WHERE song_id = ?", [song_id]
        )
        await self.store.execute(
            "INSERT INTO artist_lists (key, song_id, kind, path, artist, state, source, artists,"
            " updated_at, retry_at) VALUES (?, ?, 'placeholder', ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET path = excluded.path, artist = excluded.artist,"
            " state = excluded.state, source = excluded.source, artists = excluded.artists,"
            " updated_at = excluded.updated_at, retry_at = excluded.retry_at",
            [
                song_id,
                song_id,
                str(row["path"]) if row else "",
                artist,
                state,
                source,
                json.dumps(list(names)) if names else None,
                self.clock(),
                self.clock() + retry if retry is not None else None,
            ],
        )

    def _askable(self, ref: CatalogRef) -> bool:
        return self.catalog is not None and ref.catalog == self.catalog


@dataclass
class _Release:
    ref: str
    release: CatalogRelease
    album: list[str] | None  # the album's artists to add, if any
    songs: list[tuple[str, CatalogTrack]] = field(default_factory=list)
    # Placeholders without the ``albumartists`` the release's album tags have already:
    # rewritten with them even when their own credit gains nothing.
    pending: set[str] = field(default_factory=set)


def _release(row: Any, songs: Sequence[tuple[str, str, bool, bool]]) -> _Release | None:
    """A release's placeholders to rewrite (``CreditRepair.candidates``), from its stored
    row and its placeholders' (song ID, track, own ``artists``, own ``albumartists``)."""
    try:
        release = release_from_data(json.loads(row["data"]))
        album_tags = json.loads(row["album_tags"])
    except (ValueError, KeyError, TypeError):
        return None
    album = None
    listed_album = False
    if not row["owned_album_id"]:
        if "albumartists" in album_tags:
            listed_album = True
        else:
            album = tagging.listed_artists(release.artists, release.artist_refs, release.artist)
    entry = _Release(str(row["ref"]), release, album)
    tracks = {str(t.ref): t for t in release.tracks}
    for song_id, track_ref, listed, album_listed in songs:
        track = tracks.get(track_ref)
        if track is None:
            continue
        joined = not listed and (
            tagging.needs_credits(track) or tagging.track_artists(track, release) is not None
        )
        pending = listed_album and not album_listed
        if entry.album or joined or pending:
            entry.songs.append((song_id, track))
            if pending:
                entry.pending.add(song_id)
    return entry if entry.songs else None
