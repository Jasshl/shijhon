"""Refresh from the current catalog: the track list of a release in the library - one
that filled an owned album, or a committed catalog album - read again and applied. The
library pass does it for every added release from time to time (``fill.track_lists``),
in the service's narrow mode (``narrow``: tracks added and lengths corrected, nothing
else); as an action on one album (``Services.refresh``) no page or command offers it yet.

The release is read again, without caches, and, under the release's lock:

- tracks it gained are added as placeholders (joining the same album);
- placeholders whose track changed (title, artists, ISRC, number, length, version) are
  rewritten in place - their song IDs, and so every favorite, rating, play count,
  playlist entry and queue, stay; the album must stay the same too;
- positions are planned for the whole release at once: a placeholder of the release that
  moves frees its position for another, while every other song of the album (owned songs,
  delivered audio, tracks the release no longer lists) keeps its own; a track whose
  position stays taken is left out, and so is a placeholder that would move onto it;
- ``narrow``: no song moves and no tag but the length changes - a silent placeholder
  whose length the catalog corrected gets silence of that length under its own tags
  (its title, credits, ISRC, explicit mark, disc and number stay, and so does the
  recording that plays for it), a song the catalog lists differently in another way is
  counted and left as it is, and a new track is added only where no song of the album is;
- a new track is left out when the album has a song the release does not know (a file
  the owner added since) with its title: it may be that track;
- a new track is also left out when it is, or may be, a recording the release already
  has in the library (``_reconcile``): the catalog gave a track a new ID - and perhaps
  another place - while its ISRC, or its title, stayed (a length within a few seconds
  says it is that track; another length that it may be). Adding it would put the
  recording into the album twice. Nothing else is done for it (its old ID stays the
  song's, in Shijhon's links too);
- nothing is removed: tracks the release no longer lists stay as they are, and so do
  placeholders with delivered audio;
- album-level tags never change (they keep the album one album);
- the owner's recordings on other albums back the new placeholders and those without a
  backing.

Nothing is done from an answer that lists none of the release's tracks (another
release under its ID), and a release from another catalog than the current one is not
matched again here (not built yet): the result says so.

The release's recorded data follows what was applied: the catalog's answer, with the
positions the placeholders have - or, ``narrow``, the data as it was with the lengths
corrected and the tracks added (what the catalog says otherwise was not applied, and is
not recorded as if it were).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from shijhon.catalog.base import CatalogError
from shijhon.catalog.model import (
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    release_data,
    release_from_data,
)
from shijhon.matching.normalize import close_duration, same_title
from shijhon.navidrome.client import NavidromeService
from shijhon.placeholders.engine import MaterializeError, PlaceholderEngine, ReplaceError
from shijhon.placeholders.tags import track_artists
from shijhon.store import Store

log = logging.getLogger(__name__)

Position = tuple[int, int]


@dataclass(frozen=True)
class _Recording:
    """A recording the release has in the library, as far as it tells one from another."""

    ref: str  # the release's track it is linked as
    title: str
    isrc: str | None
    duration_ms: int | None


class ReleaseSource(Protocol):
    """Where a refresh reads releases: a catalog, or one behind a pace."""

    key: str

    async def album(self, album_id: str) -> CatalogRelease: ...


@dataclass
class Refreshed:
    """What a refresh did to one release of an album."""

    release: str
    added: int = 0
    updated: int = 0
    kept: list[str] = field(default_factory=list)  # titles no longer on the release
    skipped: list[str] = field(default_factory=list)  # what was left out, and why
    refused: str | None = None  # why nothing was done
    retry: bool = False  # refused for now only (the library may not be written, a stop's rest)
    failed: int = 0  # writes that did not go through (of the skipped): tried again
    planned: bool = False  # nothing written: ``added`` and ``updated`` are what would be

    def __str__(self) -> str:
        if self.refused:
            return f"{self.release}: not refreshed ({self.refused})"
        text = f"{self.release}: {self.added} added, {self.updated} updated"
        if self.kept:
            text += f", {len(self.kept)} no longer on the release (kept)"
        if self.skipped:
            text += f", {len(self.skipped)} left out"
        return text


class Refresh:
    """``catalog``: the current catalog, not its cache (the release as it is now)."""

    def __init__(
        self,
        store: Store,
        navidrome: NavidromeService,
        engine: PlaceholderEngine,
        catalog: ReleaseSource,
    ) -> None:
        self.store = store
        self.navidrome = navidrome
        self.engine = engine
        self.catalog = catalog

    async def refresh(self, album_id: str) -> list[Refreshed]:
        """Refresh the album's releases; an empty list when no release made or filled it."""
        rows = await self.store.fetchall(
            "SELECT ref, title, artist FROM releases WHERE album_id = ? OR owned_album_id = ?"
            " ORDER BY created_at",
            [album_id, album_id],
        )
        done = []
        for row in rows:
            result = await self._release(album_id, str(row["ref"]))
            log.info("refreshed %s - %s from the catalog: %s", row["artist"], row["title"],
                     result)  # fmt: skip
            done.append(result)
        return done

    async def _release(self, album_id: str, ref_text: str) -> Refreshed:
        result = Refreshed(ref_text)
        ref = CatalogRef.parse(ref_text)
        if ref.catalog != self.catalog.key:
            result.refused = f"a release of another catalog ({ref.catalog})"
            return result
        try:
            fresh = await self.read(ref_text)
        except CatalogError as exc:
            result.refused = (
                "the catalog no longer has it" if exc.kind == "not_found" else exc.reason
            )
            return result
        return await self.apply(album_id, ref_text, fresh)

    async def read(self, ref_text: str) -> CatalogRelease:
        """The release as the catalog has it now (``CatalogError``: it could not say)."""
        return await self.catalog.album(CatalogRef.parse(ref_text).id)

    async def apply(
        self,
        album_id: str,
        ref_text: str,
        fresh: CatalogRelease,
        *,
        narrow: bool = False,
        write: bool = True,
        back_unchanged: bool = True,
    ) -> Refreshed:
        """Apply the release as it was just read (``fresh``) to its album. ``narrow``:
        only tracks added and lengths corrected (module docstring). ``write`` off: nothing
        is written, the result says what would be added and corrected (``planned``).
        ``back_unchanged`` off: the owner's recordings on other albums are looked for (a
        search of Navidrome a placeholder) only when a track was added or corrected."""
        result = Refreshed(ref_text)
        async with self.engine.lock_for(ref_text):
            if write:
                try:  # placeholders a stop left half written go first: not its songs
                    await self.engine.settle(ref_text)
                except MaterializeError as exc:
                    result.refused, result.retry = exc.reason, True
                    return result
            elif (
                await self.store.fetchone(
                    "SELECT 1 FROM pending_placeholders WHERE release_ref = ?", [ref_text]
                )
                is not None
            ):
                result.refused, result.retry = "an earlier write of it is not finished", True
                return result
            stored = await self.store.fetchone(
                "SELECT data, removing_at FROM releases WHERE ref = ?", [ref_text]
            )
            if stored is None:
                result.refused = "no longer in the library"
                return result
            if stored["removing_at"] is not None:
                result.refused, result.retry = "it is being taken out of the library", True
                return result
            if (why := await self._unfit(album_id, ref_text, fresh, narrow)) is not None:
                result.refused = why
                return result
            new, retag, lengths = await self._plan(
                album_id, ref_text, fresh, result, stored["data"], narrow
            )
            if not write:
                result.added, result.planned = len(new), True
                result.updated = len(lengths) if narrow else len(retag)
                return result
            for song_id, title, duration_ms in lengths:  # (``narrow``: nothing else changes)
                try:
                    if await self.engine.correct_length(song_id, duration_ms):
                        result.updated += 1
                except ReplaceError as exc:
                    result.skipped.append(f"{title}: {exc}")
                    result.failed += 1
            stayed: set[Position] = set()  # positions kept after a failed rewrite
            occupied = {was for _, _, was in retag}
            temporary_songs: set[str] = set()
            origins = {song: was for _, song, was in retag}
            now_at = dict(origins)
            tracks = {song: track for track, song, _ in retag}

            async def restore_temporary(song_id: str, was: Position) -> Position:
                if song_id not in temporary_songs:
                    return was

                async def restore(song: str, visiting: set[str]) -> bool:
                    original, current = origins[song], now_at[song]
                    if original == current or song in visiting:
                        return False
                    visiting.add(song)
                    occupant = next((s for s, pos in now_at.items() if pos == original), None)
                    # A later mover may already have taken the temporary mover's old
                    # slot. Unwind that part of the cycle first, through ordinary retags
                    # whose files/rows/scans remain recoverable at every stop.
                    if occupant is not None and not await restore(occupant, visiting):
                        return False
                    restoring = tracks[song]
                    try:
                        await self.engine.retag(
                            song, replace(restoring, disc=original[0], number=original[1]), fresh
                        )
                    except ReplaceError as exc:
                        result.skipped.append(
                            f"{restoring.title}: restoring its position failed ({exc})"
                        )
                        result.failed += 1
                        return False
                    occupied.discard(current)
                    occupied.add(original)
                    now_at[song] = original
                    stayed.add(original)
                    return True

                await restore(song_id, set())
                return now_at[song_id]

            while retag:
                # A failed mover keeps its position, as do all movers depending on it.
                blocked = next((r for r in retag if (r[0].disc, r[0].number) in stayed), None)
                if blocked is not None:
                    track, song_id, was = blocked
                    result.skipped.append(f"{track.title}: its position is another song's")
                    stayed.add(await restore_temporary(song_id, was))
                    retag.remove(blocked)
                    continue
                ready = next(
                    (
                        r
                        for r in retag
                        if (r[0].disc, r[0].number) == r[2]
                        or (r[0].disc, r[0].number) not in occupied
                    ),
                    None,
                )
                temporary = ready is None
                track, song_id, was = ready or retag[0]
                desired = track
                if temporary:
                    # Break a cycle at a free position. This is an ordinary recoverable
                    # retag; a stop leaves its row/file/scan together and a later refresh
                    # finishes the move from there.
                    places = occupied | stayed | {(t.disc, t.number) for t in fresh.tracks}
                    places |= {
                        (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0))
                        for s in await self.navidrome.songs_of_album(album_id)
                    }
                    number = max(n for _, n in places) + 1
                    track = replace(track, number=number)
                try:
                    changed = await self.engine.retag(song_id, track, fresh)
                except ReplaceError as exc:
                    result.skipped.append(f"{track.title}: {exc}")
                    result.failed += 1
                    stayed.add(await restore_temporary(song_id, was))
                    retag.remove((desired, song_id, was))
                else:
                    occupied.discard(was)
                    retag.remove((desired, song_id, was))
                    now_at[song_id] = (track.disc, track.number)
                    if temporary:
                        temporary_songs.add(song_id)
                        position = (track.disc, track.number)
                        occupied.add(position)
                        retag.append((desired, song_id, position))
                    elif changed:
                        result.updated += 1
            clash = [t for t in new if (t.disc, t.number) in stayed]
            for track in clash:
                result.skipped.append(f"{track.title}: its position is another song's")
            new = [t for t in new if t not in clash]
            if new:
                try:
                    made, _, _ = await self.engine.materialize_held(
                        fresh, only=[t.ref for t in new]
                    )
                except MaterializeError as exc:
                    result.skipped.append(f"{len(new)} new track(s): {exc.reason}")
                    result.failed += len(new)
                else:
                    result.added = len(made.created)
            applied = await self._as_applied(ref_text, fresh, stored["data"], narrow)
            if result.added or result.updated or await self._stale(ref_text, stored["data"], fresh):
                await self.store.execute(
                    "UPDATE releases SET data = ? WHERE ref = ?",
                    [json.dumps(release_data(applied)), ref_text],
                )
            # (``narrow``: the songs as the library has them, not as the catalog lists them.)
            fresh = applied if narrow else fresh
            unbacked = await self._unbacked(ref_text, fresh)
            linked = [
                str(r["song_id"])
                for r in await self.store.fetchall(
                    "SELECT song_id FROM track_links WHERE release_ref = ? AND owned = 1",
                    [ref_text],
                )
            ]
        # The owner's recordings on other albums (outside the release's lock), once: the
        # new placeholders and those still without a backing.
        if back_unchanged or result.added or result.updated:
            await self.engine.back_placeholders(fresh, None, unbacked, linked)
        return result

    async def _as_applied(
        self, ref_text: str, fresh: CatalogRelease, data: str, narrow: bool
    ) -> CatalogRelease:
        """The release as the library has it now: the catalog's answer, each track that
        has a placeholder at the disc and number its placeholder has. ``narrow``: the
        release as it was recorded (``data``), each track that has a placeholder with what
        its row says (the corrected length), and the tracks added since - nothing of the
        catalog's answer that was not applied."""
        rows = {
            str(r["track_ref"]): r
            for r in await self.store.fetchall(
                "SELECT track_ref, title, artist, isrc, duration_ms, disc, track"
                " FROM placeholders WHERE release_ref = ?",
                [ref_text],
            )
        }
        recorded = None
        if narrow:
            try:
                recorded = release_from_data(json.loads(data))
            except (ValueError, KeyError, TypeError):
                recorded = None  # (unreadable: the catalog's answer, as below)
        if recorded is None:
            return replace(
                fresh,
                tracks=tuple(
                    replace(t, disc=int(row["disc"]), number=int(row["track"]))
                    if (row := rows.get(str(t.ref))) is not None
                    else t
                    for t in fresh.tracks
                ),
            )

        def as_row(track: CatalogTrack) -> CatalogTrack:
            row = rows.get(str(track.ref))
            if row is None:
                return track
            return replace(
                track,
                title=str(row["title"]),
                artist=str(row["artist"]),
                isrc=row["isrc"],
                duration_ms=int(row["duration_ms"]),
                disc=int(row["disc"]),
                number=int(row["track"]),
            )

        known = {str(t.ref) for t in recorded.tracks}
        added = [t for t in fresh.tracks if str(t.ref) in rows and str(t.ref) not in known]
        tracks = sorted(
            (as_row(t) for t in (*recorded.tracks, *added)), key=lambda t: (t.disc, t.number)
        )
        return replace(recorded, tracks=tuple(tracks))

    async def _stale(self, ref_text: str, data: str, fresh: CatalogRelease) -> bool:
        """Whether the release's recorded data is behind its silent placeholders (a stop
        between a write and the data's): one the catalog lists is not in it, or is there
        with another title, credit, ISRC, length or position than its row has."""
        try:
            recorded = {str(t.ref): t for t in release_from_data(json.loads(data)).tracks}
        except (ValueError, KeyError, TypeError):
            return True
        listed = {str(t.ref) for t in fresh.tracks}
        for row in await self.store.fetchall(
            "SELECT track_ref, title, artist, isrc, duration_ms, disc, track FROM placeholders"
            " WHERE release_ref = ? AND state = 'placeholder'",
            [ref_text],
        ):
            if str(row["track_ref"]) not in listed:
                continue
            track = recorded.get(str(row["track_ref"]))
            if track is None or (
                track.title,
                track.artist,
                track.isrc or None,
                track.duration_ms,
                track.disc,
                track.number,
            ) != (
                row["title"],
                row["artist"],
                row["isrc"] or None,
                row["duration_ms"],
                row["disc"],
                row["track"],
            ):
                return True
        return False

    async def _unfit(
        self, album_id: str, ref_text: str, fresh: CatalogRelease, narrow: bool = False
    ) -> str | None:
        """Why the release is not refreshed from this answer, or None: an answer that lists
        none of its tracks (another release under its ID); for an owned album, a track list
        that could not be read in full, or track numbers that are only the order of the
        catalog's list (``numbered`` off) with an owned song elsewhere than that order
        puts it - its placeholders would be moved around files that stay - or, when no
        song moves (``narrow``), with a placeholder elsewhere."""
        known = {
            str(r["track_ref"])
            for r in await self.store.fetchall(
                "SELECT track_ref FROM track_links WHERE release_ref = ?", [ref_text]
            )
        }
        if known and known.isdisjoint(str(t.ref) for t in fresh.tracks):
            return "the catalog lists none of the release's tracks any more"
        row = await self.store.fetchone(
            "SELECT owned_album_id FROM releases WHERE ref = ?", [ref_text]
        )
        if row is None or not row["owned_album_id"]:
            return None
        if fresh.incomplete:
            return "the catalog's track list of the release is incomplete"
        if fresh.numbered:
            return None
        links = {
            str(r["track_ref"]): str(r["song_id"])
            for r in await self.store.fetchall(
                "SELECT track_ref, song_id FROM track_links WHERE release_ref = ? AND owned = 1",
                [ref_text],
            )
        }
        places = {
            str(s["id"]): (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0))
            for s in await self.navidrome.songs_of_album(album_id)
        }
        for track in fresh.tracks:
            song = links.get(str(track.ref))
            if song is not None and places.get(song, (track.disc, track.number)) != (
                track.disc,
                track.number,
            ):
                return (
                    "the catalog lists the release's tracks without numbers, and the owned"
                    " files are numbered differently from its order"
                )
        if not narrow:
            return None  # (its placeholders move to where the list puts them)
        placed = {
            str(r["track_ref"]): (int(r["disc"]), int(r["track"]))
            for r in await self.store.fetchall(
                "SELECT track_ref, disc, track FROM placeholders WHERE release_ref = ?",
                [ref_text],
            )
        }
        for track in fresh.tracks:
            at = (track.disc, track.number)
            if placed.get(str(track.ref), at) != at:
                return (
                    "the catalog lists the release's tracks without numbers, and in another"
                    " order than the album's songs have"
                )
        return None

    async def _plan(
        self,
        album_id: str,
        ref_text: str,
        fresh: CatalogRelease,
        result: Refreshed,
        data: str = "",
        narrow: bool = False,
    ) -> tuple[
        list[CatalogTrack],
        list[tuple[CatalogTrack, str, tuple[int, int]]],
        list[tuple[str, str, int]],
    ]:
        """(Tracks to add, placeholders to rewrite with their current positions, lengths
        to correct as (song, title, length)): with every position planned at once and no
        lengths apart - or, ``narrow``, with every song where it is and nothing to rewrite
        but the lengths. ``data``: the release as it was recorded."""
        linked = await self.store.fetchall(
            "SELECT track_ref, song_id FROM track_links WHERE release_ref = ?", [ref_text]
        )
        links = {str(r["track_ref"]) for r in linked}
        known_songs = {str(r["song_id"]) for r in linked}
        placeholders = {
            str(r["track_ref"]): r
            for r in await self.store.fetchall(
                "SELECT * FROM placeholders WHERE release_ref = ?", [ref_text]
            )
        }
        fresh_refs = {str(t.ref) for t in fresh.tracks}
        result.kept = [
            str(row["title"]) for key, row in placeholders.items() if key not in fresh_refs
        ]
        # The release's silent placeholders the catalog still lists may move; every other
        # song of the album keeps its position.
        movable: dict[str, tuple[CatalogTrack, Any]] = {}
        lengths: list[tuple[str, str, int]] = []
        for track in fresh.tracks:
            row = placeholders.get(str(track.ref))
            if row is None:
                continue
            if narrow:
                # Where it is and as it is, but for its length; what else the catalog says
                # of it now is counted, not written.
                same_length = replace(track, duration_ms=int(row["duration_ms"]))
                longer = track.duration_ms != row["duration_ms"]
                if row["state"] != "placeholder":
                    if longer or _changed(same_length, row, fresh):
                        result.skipped.append(f"{row['title']}: delivered audio is kept as it is")
                    continue
                if _changed(same_length, row, fresh):
                    result.skipped.append(
                        f"{row['title']}: the catalog lists it differently now (its title,"
                        " artists, ISRC, explicit mark or number); left as it is"
                    )
                if longer:
                    lengths.append((str(row["song_id"]), str(row["title"]), track.duration_ms))
                movable[str(row["song_id"])] = (track, row)
            elif row["state"] == "placeholder":
                movable[str(row["song_id"])] = (track, row)
            elif _changed(track, row, fresh):
                result.skipped.append(f"{track.title}: delivered audio is kept as it is")
        songs = [s for s in await self.navidrome.songs_of_album(album_id) if not s.get("missing")]
        # Songs of the album the release does not know (files the owner added since): a new
        # track with the title of one may be that song, and is not added beside it.
        own = [str(s.get("title") or "") for s in songs if str(s["id"]) not in known_songs]
        unknown = []
        for track in fresh.tracks:
            if str(track.ref) in links:
                continue
            if any(same_title(track.title, title) for title in own):
                result.skipped.append(
                    f"{track.title}: the album has a song of its own with this title"
                )
            else:
                unknown.append(track)
        gained = _reconcile(
            unknown, _recordings(linked, placeholders, data, songs), fresh_refs, result
        )
        fixed: dict[Position, str] = {
            (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0)): str(s["id"])
            for s in songs
            if str(s["id"]) not in movable
        }
        if narrow:
            taken = set(fixed) | {(int(r["disc"]), int(r["track"])) for _, r in movable.values()}
            added = []
            for track in gained:
                if (track.disc, track.number) in taken:
                    result.skipped.append(f"{track.title}: its position is another song's")
                else:
                    taken.add((track.disc, track.number))
                    added.append(track)
            return added, [], lengths
        wanted = [(track, None) for track in gained] + [
            (track, song_id) for song_id, (track, _) in movable.items()
        ]
        blocked: set[str] = set()
        while True:  # until no blocked placeholder takes back a position someone wanted
            claims: dict[Position, str] = {}
            newly = False
            for track, song_id in wanted:
                key = str(track.ref)
                if key in blocked:
                    continue
                position = (track.disc, track.number)
                if position in fixed or position in claims:
                    blocked.add(key)
                    newly = True
                    if song_id is not None:  # it stays where it is, as it is
                        row = movable[song_id][1]
                        fixed[(int(row["disc"]), int(row["track"]))] = song_id
                else:
                    claims[position] = key
            if not newly:
                break
        new, retag = [], []
        for track, song_id in wanted:
            key = str(track.ref)
            if key in blocked:
                if song_id is None or _changed(track, movable[song_id][1], fresh):
                    result.skipped.append(f"{track.title}: its position is another song's")
            elif song_id is None:
                new.append(track)
            elif _changed(track, (row := movable[song_id][1]), fresh):
                retag.append((track, song_id, (int(row["disc"]), int(row["track"]))))
        return new, retag, []

    async def _unbacked(
        self, ref_text: str, fresh: CatalogRelease
    ) -> list[tuple[CatalogTrack, str]]:
        """The release's placeholders without a backing, with their current tracks."""
        tracks = {str(t.ref): t for t in fresh.tracks}
        return [
            (tracks[str(r["track_ref"])], str(r["song_id"]))
            for r in await self.store.fetchall(
                "SELECT song_id, track_ref FROM placeholders WHERE release_ref = ?"
                " AND backing_song_id IS NULL AND state = 'placeholder'",
                [ref_text],
            )
            if str(r["track_ref"]) in tracks
        ]


def _recordings(
    linked: list[Any], placeholders: dict[str, Any], data: str, songs: list[dict[str, Any]]
) -> list[_Recording]:
    """The recordings the release has in the library, by the track each is linked as: a
    placeholder (silent or with audio) as its row has it, and every linked track - an
    owned song's too - as the release was recorded; an owned song without that, as
    Navidrome has it."""
    try:
        recorded = {str(t.ref): t for t in release_from_data(json.loads(data)).tracks}
    except (ValueError, KeyError, TypeError):
        recorded = {}
    by_id = {str(s["id"]): s for s in songs}
    found = []
    for link in linked:
        ref = str(link["track_ref"])
        row, track = placeholders.get(ref), recorded.get(ref)
        if row is not None:
            found.append(_Recording(ref, str(row["title"]), row["isrc"], row["duration_ms"]))
        if track is not None:
            found.append(_Recording(ref, track.title, track.isrc, track.duration_ms))
        song = by_id.get(str(link["song_id"]))
        if row is None and track is None and song is not None:
            length = int(float(song.get("duration") or 0) * 1000) or None
            found.append(_Recording(ref, str(song.get("title") or ""), None, length))
    return found


def _reconcile(
    tracks: list[CatalogTrack],
    recordings: list[_Recording],
    listed: set[str],
    result: Refreshed,
) -> list[CatalogTrack]:
    """The new tracks that are no recording the release already has in the library; the
    others are left out, with the reason (``result.skipped``).

    The catalog can give a track a new ID - and move it - without it being another
    recording. A new track is one the release has when their ISRCs are the same, or,
    where either has none, when their titles name the same recording. It is then **that
    track** when exactly one linked track fits, the catalog no longer lists that one
    under its old ID, no other new track fits it too, and - without ISRCs - their
    lengths are within a few seconds; otherwise it only **may be** one. Either way it is
    not added: two known ISRCs that differ are the only proof of another recording. A
    same title leaves a track out whatever the lengths (they only decide which of the two
    reasons is given). New tracks the catalog lists with one ISRC are one recording:
    the first is added, the others are left out."""
    fits: dict[str, set[str]] = {}
    sure: dict[str, set[str]] = {}
    for track in tracks:
        key = str(track.ref)
        fits[key], sure[key] = set(), set()
        for known in recordings:
            if track.isrc and known.isrc:
                if track.isrc.upper() == known.isrc.upper():
                    fits[key].add(known.ref)
                    sure[key].add(known.ref)
            elif same_title(track.title, known.title):
                fits[key].add(known.ref)
                if close_duration(track.duration_ms, known.duration_ms):
                    sure[key].add(known.ref)
    claimed = [ref for found in fits.values() for ref in found]
    gained = []
    for track in tracks:
        found = fits[str(track.ref)]
        if not found:
            gained.append(track)
            continue
        [only] = found if len(found) == 1 else [None]
        if (
            only is not None
            and only in sure[str(track.ref)]
            and only not in listed
            and claimed.count(only) == 1
        ):
            result.skipped.append(
                f"{track.title}: a song of the album under a new ID at the catalog; it"
                " stays as it is"
            )
        else:
            result.skipped.append(
                f"{track.title}: it may be a song the album already has; not added"
            )
    once: list[CatalogTrack] = []
    isrcs: set[str] = set()
    for track in gained:
        isrc = (track.isrc or "").upper()
        if isrc and isrc in isrcs:
            result.skipped.append(
                f"{track.title}: the catalog lists this recording twice among its new"
                " tracks; added once"
            )
            continue
        if isrc:
            isrcs.add(isrc)
        once.append(track)
    return once


def _changed(track: CatalogTrack, row: Any, release: CatalogRelease) -> bool:
    """Whether the catalog's track differs from the placeholder's (as far as the
    placeholder shows it)."""
    tags = json.loads(row["tags"] or "{}")
    artists = track_artists(track, release)
    if artists is None and track.artist == row["artist"]:
        artists = tags.get("artists")  # (an answer without the credit's artists: kept)
    release_clean = release.clean
    return (
        track.title != row["title"]
        or track.artist != row["artist"]
        or (track.isrc or None) != (row["isrc"] or None)
        or track.disc != row["disc"]
        or track.number != row["track"]
        or track.duration_ms != row["duration_ms"]
        or tags.get("artists") != artists
        or tags.get("itunesadvisory")
        != (["1"] if track.explicit else ["2"] if track.clean or release_clean else None)
    )
