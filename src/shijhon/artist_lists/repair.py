"""Artist lists for files that name several artists in one text (``[catalog] artist_lists``,
on by default, applied at once).

A file whose only artist tag reads "Lumen Vale, Orrin Pike" makes Navidrome credit one
artist of that name (it splits " / ", " feat. ", "; " and the like, not a comma or "&"): no
page and no picture for either person. Shijhon gives such a file the multi-valued artists
tag (``ARTISTS``) when a catalog lists the song's artists and their names are exactly its
text (``names.listed``; the sources: ``sources.py``), so Navidrome credits each artist. The
artist text the file shows stays as it is. A file without an album artist tag also gets
the album artist Navidrome showed (that text), so its album stays the album; when every
song of an album gets the same list and the album's artist text is those names too, each
of them also gets the album artists list (``ALBUMARTISTS``), so the album is listed under
each person - all of the album's files or none.

After a start (once the start's own work is done) and then every few hours, at once when
the setting changes, a pass looks at the songs Navidrome credits to one artist whose name
has a separator: library files (``files.py``) and music imports' published files
(``music_imports/lists.py``); Shijhon's own placeholders are the credit repair's
(``placeholders/credits.py``). Each song's outcome is kept (``artist_lists``): a song
without a list, with differing lists, or whose file cannot take one is asked about again
after a while, not at every start; one asked about while a catalog failed, the next day.
Songs are asked about one at a time with a short pause; several failures in a row end the
pass. Every write is checked by a targeted scan: the song keeps its ID and its album, and
each artist is listed; otherwise the file is put back.

Turned off, a pass takes back what Shijhon wrote, for files still as it left them; a file
changed since is left alone, also when the setting is on again.

With ``SHIJHON_ARTIST_LISTS_REPORT=1`` in Shijhon's environment, passes only report: what
they would write, by source, and why other songs are left (one log line a pass); nothing
is written or recorded, and imports are published as they are.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio

from shijhon.artist_lists.files import Entry, LibraryFiles, ListError, Made, file_digest
from shijhon.artist_lists.names import listed, maybe_joined
from shijhon.artist_lists.sources import (
    Resolution,
    Skips,
    Sources,
    Subject,
    Unavailable,
    song_subject,
)
from shijhon.catalog.base import CatalogError
from shijhon.matching.normalize import fold
from shijhon.music_imports.lists import ImportLists
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.store import Store

log = logging.getLogger(__name__)

FIRST_PASS_SECONDS = 600.0  # after a start: the start's own passes go first
PASS_SECONDS = 6 * 3600.0
PAUSE_SECONDS = 1.0  # between two songs asked about
UNRESOLVED_SECONDS = 30 * 86400.0  # no list, or differing ones: asked again after
SKIPPED_SECONDS = 7 * 86400.0  # a file that could not take one: looked at again after
FAILED_SECONDS = 86400.0  # a catalog that failed, a write put back: tried again after
FAILURES_IN_A_ROW = 3  # end a pass: a catalog or Navidrome is down
PUBLISH_SECONDS = 20.0  # an imported file's list at its publication, at most
REPORT = "SHIJHON_ARTIST_LISTS_REPORT"
NOT_ALLOWED = "not_allowed"  # a music folder file while ``artist_lists_library`` is off
NOT_ARTISTS = frozenset(fold(n) for n in ("[Unknown Artist]", "Various Artists"))
# Skips that a file keeps (looked at again only after a while).
_LASTING = frozenset({"format", "listed", "identified", "other_tags", "identity", "link",
                      "hard_link", "other_mount", "read_only", "owner", "outside",
                      "edited"})  # fmt: skip


def report_only() -> bool:
    return os.environ.get(REPORT, "").strip().lower() not in ("", "0", "false", "no")


def _artists(song: Mapping[str, Any] | None, role: str = "artist") -> list[str]:
    people = (song or {}).get("participants") or {}
    return [str(p.get("name") or "") for p in people.get(role) or [] if isinstance(p, dict)]


@dataclass
class Candidate:
    song: dict[str, Any]
    subject: Subject
    kind: str  # "file" or "import"
    key: str
    relative: str
    album_id: str
    album_artist: str
    resolution: Resolution | None = None
    skip: str | None = None
    gated: bool = False  # (a report: a music folder file, its setting off)


@dataclass
class Summary:
    looked: int = 0
    written: Counter[str] = field(default_factory=Counter)  # by source
    albums: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    failed: Counter[str] = field(default_factory=Counter)
    undone: int = 0
    declined: int = 0
    held: Counter[str] = field(default_factory=Counter)  # why no list: one_artist, ...
    addons_skipped: int = 0  # add-ons asked no more in the pass (they kept failing)
    vetoes: Counter[str] = field(default_factory=Counter)  # one-artist answers, by who gave them
    gated: Counter[str] = field(default_factory=Counter)  # (a report) music folder files, by source

    def line(self, report: bool) -> str:
        def each(counts: Counter[str]) -> str:
            return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if v) or "none"

        given = sum(self.written.values())
        verb = "at most would be given" if report else "given"
        return (
            f"artist lists{' (report only)' if report else ''}: {self.looked} song(s) with"
            f" joined artists looked at; {given} {verb} their list (by source:"
            f" {each(self.written)}); {self.albums} album(s) also listed under each artist;"
            f" {self.unresolved} without a list, {self.ambiguous} ambiguous (by reason:"
            f" {each(self.held)}); skipped: {each(self.skipped)}; failed: {each(self.failed)};"
            f" add-ons skipped for the pass: {self.addons_skipped}; one artist named with the"
            f" whole text, by source: {each(self.vetoes)}"
            + (
                f"; {sum(self.gated.values())} more would be given with the music folder"
                f" setting on (by source: {each(self.gated)})"
                if self.gated
                else ""
            )
        )


class ArtistLists:
    def __init__(
        self,
        navidrome: NavidromeService,
        store: Store,
        sources: Sources,
        files: LibraryFiles,
        *,
        placeholder_folder: str,
        imports: ImportLists | None = None,
        enabled: bool = True,
        library: bool = False,
        report: bool | None = None,
        stopping: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
        pause: float | None = None,
    ) -> None:
        self.navidrome = navidrome
        self.store = store
        self.sources = sources
        self.files = files
        self.placeholders = placeholder_folder.strip("/") + "/"
        self.imports = imports
        self.report = report_only() if report is None else report
        self.stopping = stopping
        self.clock = clock
        self.pause = PAUSE_SECONDS if pause is None else pause
        self._enabled = enabled
        self._library = library
        self._last_line = ""  # (the last pass's summary: an unchanged one is not logged)
        self._blocked = 0  # (music folder files Shijhon could not write, as last counted)
        self._wake = anyio.Event()
        if imports is not None:
            imports.publish_names = self.publish_names

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """The dashboard's setting, applied at once: a pass follows."""
        if value != self._enabled:
            self._enabled = value
            self._wake.set()

    @property
    def library(self) -> bool:
        return self._library

    @library.setter
    def library(self, value: bool) -> None:
        """The music folder's files too (``[catalog] artist_lists_library``), applied at
        once: a pass follows (off gives those Shijhon changed back)."""
        if value != self._library:
            self._library = value
            self._wake.set()

    def read_only(self) -> bool:
        """Whether files of the music folder are to be listed but Shijhon cannot write the
        folder, or the last pass found files it cannot write (read-only for it, or another
        user's): Diagnostics says so."""
        if not (self._enabled and self._library):
            return False
        return self._blocked > 0 or not os.access(self.files.root, os.W_OK | os.X_OK)

    def lookups(self) -> bool:
        """Whether the credit repair may ask the add-on catalogs and write their lists
        now: while the setting is on and passes do more than report."""
        return self._enabled and not self.report

    async def run(self) -> None:
        try:  # (what a stop left goes first, not with the first pass; also when reporting)
            await self.recover()
        except Exception as exc:
            log.warning("artist lists: what a stop left is put right later: %s", exc)
        with anyio.move_on_after(FIRST_PASS_SECONDS):
            await self._wake.wait()
        while True:
            self._wake = anyio.Event()
            try:
                await self.once()
            except (NavidromeError, CatalogError) as exc:
                log.info("artist lists: pass ended early: %s", exc)
            except Exception:  # (the next pass goes on)
                log.exception("artist lists: pass failed")
            if self.report:
                await self._wake.wait()  # (one report a start; another when the setting changes)
            else:
                with anyio.move_on_after(PASS_SECONDS):
                    await self._wake.wait()

    async def recover(self) -> None:
        """What a stop left: a write or undo of a file or of an import not finished."""
        if await self.files.recover():
            log.info("artist lists: a write a stop interrupted was put right")
        if self.imports is not None and await self.imports.recover():
            log.info("artist lists: an imported file's change a stop interrupted was put right")

    async def once(self) -> Summary:
        """One pass (module docstring); returns what it did."""
        summary = Summary()
        await self.recover()  # (an earlier write a stop left: not a new one)
        if self.report:
            await self._pass(summary)
            log.info(summary.line(True))
            return summary
        await self.store.execute("DELETE FROM artist_lists WHERE state = 'undone' AND phase = ''")
        if not self._enabled or not self._library:
            # Off: everything Shijhon wrote goes back; the music folder's files alone off:
            # those (imports and placeholders keep following the first setting).
            kinds = ("file",) if self._enabled else ("file", "import")
            await self._undo_all(summary, kinds)
            if summary.undone or summary.declined:
                log.info(
                    "artist lists: turned off%s; %d file(s) given back as they were, %d"
                    " changed since and left alone",
                    " for the music folder" if self._enabled else "",
                    summary.undone,
                    summary.declined,
                )
        if not self._enabled:
            return summary
        await self._pass(summary)
        # (From the outcomes kept: such a skip is looked at again only after a while, so
        # the pass's own count would forget it at the next pass.)
        blocked = await self.store.fetchone(
            "SELECT COUNT(*) AS n FROM artist_lists WHERE kind = 'file' AND state = 'skipped'"
            " AND reason IN ('read_only', 'owner')"
        )
        self._blocked = int(blocked["n"]) if blocked is not None else 0
        if summary.looked:
            line = summary.line(False)
            if line != self._last_line:  # (passes that change nothing say it once)
                log.info(line)
                self._last_line = line
        return summary

    # --- the candidates ------------------------------------------------------------

    async def candidates(self) -> list[Candidate]:
        """The songs Navidrome credits to one artist whose name may join several: library
        files and imports' published files (not Shijhon's placeholders)."""
        found: dict[str, Candidate] = {}
        for artist in await self.navidrome.artists():
            name = str(artist.get("name") or "")
            if not maybe_joined(name) or fold(name) in NOT_ARTISTS:
                continue
            if self.stopping():
                break
            for song in await self.navidrome.every_artist_song(str(artist["id"])):
                ident, relative = str(song.get("id") or ""), str(song.get("path") or "")
                if (
                    not ident
                    or ident in found
                    or song.get("missing")
                    or str(song.get("artist") or "") != name
                    or _artists(song) != [name]
                    or relative.startswith(self.placeholders)
                ):
                    continue
                kind = (
                    "import"
                    if self.imports is not None
                    and relative.startswith(self.imports.imports.storage.folder + "/")
                    else "file"
                )
                found[ident] = Candidate(
                    song,
                    song_subject(song),
                    kind,
                    ident,
                    relative,
                    str(song.get("albumId") or ""),
                    str(song.get("albumArtist") or ""),
                )
        return list(found.values())

    async def _records(self) -> dict[str, Any]:
        rows = await self.store.fetchall("SELECT * FROM artist_lists")
        out: dict[str, Any] = {}
        for row in rows:
            out[str(row["key"])] = row
            if row["song_id"]:
                out.setdefault("song:" + str(row["song_id"]), row)
        return out

    def _due(self, candidate: Candidate, records: Mapping[str, Any]) -> bool:
        found = [r for r in (records.get(candidate.key), records.get("song:" + candidate.key)) if r]
        if any(r["state"] in ("written", "declined", "undone") or r["phase"] for r in found):
            return False
        record = records.get(candidate.key)
        if record is None:
            return True
        if record["artist"] != candidate.subject.artist or record["path"] != candidate.relative:
            return True
        return float(record["retry_at"] or 0) <= self.clock()

    # --- a pass --------------------------------------------------------------------

    async def _pass(self, summary: Summary) -> None:
        skips = Skips()
        try:
            await self._songs(summary, skips)
        finally:
            summary.addons_skipped = len(skips.addons)

    async def _songs(self, summary: Summary, skips: Skips) -> None:
        records = await self._records()
        albums: dict[str, list[Candidate]] = {}
        for candidate in await self.candidates():
            albums.setdefault(candidate.album_id or candidate.key, []).append(candidate)
        failures = 0
        asked = 0
        for album_id, members in albums.items():
            if self.stopping() or (not self._enabled and not self.report):
                return
            due = [c for c in members if self._due(c, records)]
            if not due:
                continue
            for candidate in due:
                if self.stopping():
                    return
                summary.looked += 1
                candidate.skip = await self._skip(candidate)
                if candidate.skip == NOT_ALLOWED:
                    summary.skipped[NOT_ALLOWED] += 1  # (counted, not recorded: on, it is due)
                    continue
                if candidate.skip is not None:
                    summary.skipped[candidate.skip] += 1
                    await self._note(
                        candidate,
                        "skipped",
                        reason=candidate.skip,
                        retry=SKIPPED_SECONDS if candidate.skip in _LASTING else FAILED_SECONDS,
                    )
                    continue
                if asked:
                    await anyio.sleep(self.pause)
                asked += 1
                try:
                    candidate.resolution = await self.sources.resolve(
                        candidate.subject, skips=skips
                    )
                    failures = 0
                except (Unavailable, CatalogError) as exc:
                    summary.failed["catalog"] += 1
                    await self._note(candidate, "failed", reason="catalog", retry=FAILED_SECONDS)
                    if isinstance(exc, Unavailable) and exc.answered:
                        continue  # (one add-on down: the others answered, the pass goes on)
                    failures += 1
                    if failures >= FAILURES_IN_A_ROW:
                        log.info("artist lists: catalogs failed %d times in a row (%s); the"
                                 " next pass goes on", failures, exc)  # fmt: skip
                        return
                    continue
                if candidate.resolution.state != "resolved":
                    if candidate.resolution.state == "ambiguous":
                        summary.ambiguous += 1
                    else:
                        summary.unresolved += 1
                    summary.held[candidate.resolution.source or "no_list"] += 1
                    summary.vetoes.update(candidate.resolution.vetoed_by)
                    await self._note(
                        candidate,
                        candidate.resolution.state,
                        reason=candidate.resolution.source,
                        retry=UNRESOLVED_SECONDS,
                    )
            resolved = [c for c in due if c.resolution and c.resolution.state == "resolved"]
            if resolved:
                album_names = await self._album_list(album_id, members, resolved)
                await self._write_album(resolved, album_names, summary)

    async def _skip(self, candidate: Candidate) -> str | None:
        if candidate.kind == "file" and not self._library:
            if not self.report:
                return NOT_ALLOWED
            # A report tells what the music folder's setting would do too (writing nothing).
            candidate.gated = True
        if candidate.kind == "import":
            assert self.imports is not None
            item = await self.imports.item_of(candidate.key)
            return None if item is not None else "import_busy"
        return await anyio.to_thread.run_sync(self.files.skip, candidate.relative)

    async def _album_list(
        self, album_id: str, members: Sequence[Candidate], resolved: Sequence[Candidate]
    ) -> list[str] | None:
        """The album artists list: when every song of the album gets the same list now and
        the album's artist text is exactly those names - or the album's other songs have
        it already, written with this list and album artists list (an album a hard stop
        or a failed commit left part listed is finished, not left split)."""
        if not album_id or album_id == resolved[0].key:
            return None
        names = {c.resolution.names for c in resolved if c.resolution}
        if len(names) != 1 or len(resolved) != len(members):
            return None
        shown = {c.album_artist for c in members}
        if len(shown) != 1:
            return None
        try:
            songs = await self.navidrome.songs_of_album(album_id)
        except NavidromeError:
            return None
        album = listed(shown.pop(), next(iter(names)))
        if album is None:
            return None
        written = await self.store.fetchall(
            "SELECT key, song_id, artists, album_artists FROM artist_lists"
            " WHERE album_id = ? AND state = 'written' AND kind IN ('file', 'import')",
            [album_id],
        )
        listed_before = {
            str(r["song_id"] or r["key"])
            for r in written
            if json.loads(r["artists"] or "null") == list(next(iter(names)))
            and json.loads(r["album_artists"] or "null") == album
        }
        present = {str(s.get("id")) for s in songs if not s.get("missing")}
        if present != {c.key for c in members} | (listed_before & present):
            return None
        return album

    async def _write_album(
        self, resolved: Sequence[Candidate], album_names: list[str] | None, summary: Summary
    ) -> None:
        """An album's files given their lists together: before one scan, all of them or -
        with the album artists list - none (those written go back)."""
        if self.stopping():
            return
        if self.report:
            for candidate in resolved:
                assert candidate.resolution is not None
                counted = summary.gated if candidate.gated else summary.written
                counted[candidate.resolution.source] += 1
            summary.albums += 1 if album_names else 0
            return
        outcomes: dict[str, Made | ListError] = {}
        files = [c for c in resolved if c.kind == "file"]
        imported = [c for c in resolved if c.kind == "import"]
        if files:
            for candidate in files:
                await self._note(candidate, "failed", reason="interrupted", retry=FAILED_SECONDS)
            outcomes |= await self.files.write_many([self._entry(c, album_names) for c in files])
        if imported:
            assert self.imports is not None
            outcomes |= await self.imports.write_many(
                [self._import_entry(c, album_names) for c in imported]
            )
        failed = [c for c in resolved if isinstance(outcomes.get(c.key), ListError)]
        written = [c for c in resolved if isinstance(outcomes.get(c.key), Made)]
        for candidate in failed:
            error = outcomes[candidate.key]
            assert isinstance(error, ListError)
            lasting = error.reason in _LASTING
            (summary.skipped if lasting else summary.failed)[error.reason] += 1
            await self._note(
                candidate,
                "skipped" if lasting else "failed",
                reason=error.reason,
                retry=SKIPPED_SECONDS if lasting else FAILED_SECONDS,
            )
        if album_names and failed and written:
            # All of an album's files or none: those written now go back.
            await self._take_back(written)
            return
        for candidate in written:
            assert candidate.resolution is not None
            summary.written[candidate.resolution.source] += 1
        if album_names and written and not failed:
            summary.albums += 1

    def _request(self, candidate: Candidate, album_names: list[str] | None) -> dict[str, Any]:
        assert candidate.resolution is not None
        values: dict[str, list[str]] = {"artists": list(candidate.resolution.names)}
        if album_names:
            values["albumartists"] = album_names
        return {"values": values, "pin": True, "artist": candidate.subject.artist}

    def _import_entry(
        self, candidate: Candidate, album_names: list[str] | None
    ) -> tuple[str, dict[str, Any], Any, dict[str, Any]]:
        assert candidate.resolution is not None
        names = list(candidate.resolution.names)
        record = {
            "artist": candidate.subject.artist,
            "source": candidate.resolution.source,
            "artists": names,
            "album_artists": album_names,
            "album_id": candidate.album_id,
        }
        return (
            candidate.key,
            self._request(candidate, album_names),
            self._settled(candidate, names, album_names),
            record,
        )

    def _entry(self, candidate: Candidate, album_names: list[str] | None) -> Entry:
        assert candidate.resolution is not None
        names = list(candidate.resolution.names)
        source = candidate.resolution.source

        def commit(made: Made) -> tuple[str, list[Any]]:
            previous = {"artists": None}
            if album_names:
                previous["albumartists"] = None
            if made.pinned:
                previous["albumartist"] = None
            return (
                "UPDATE artist_lists SET state = 'written', reason = '', source = ?,"
                " artists = ?, album_artists = ?, previous = ?, album_id = ?,"
                " size_before = ?, sha_before = ?, raw_before = ?, size_after = ?,"
                " sha_after = ?, phase = 'drop', updated_at = ?, retry_at = NULL WHERE key = ?",
                [
                    source,
                    json.dumps(names),
                    json.dumps(album_names) if album_names else None,
                    json.dumps(previous),
                    candidate.album_id,
                    made.size_before,
                    made.sha_before,
                    made.raw_before,
                    made.size,
                    made.sha256,
                    self.clock(),
                    candidate.key,
                ],
            )

        return Entry(
            candidate.key,
            candidate.relative,
            self._request(candidate, album_names),
            self._settled(candidate, names, album_names),
            commit,
        )

    def _settled(
        self, candidate: Candidate, names: list[str], album_names: list[str] | None
    ) -> Callable[[dict[str, Any] | None, int], bool]:
        def settled(song: dict[str, Any] | None, size: int) -> bool:
            return (
                song is not None
                and not song.get("missing")
                and song.get("path") == candidate.relative
                and song.get("size") == size
                and str(song.get("albumId") or "") == candidate.album_id
                and str(song.get("albumArtist") or "") == candidate.album_artist
                and _artists(song) == names
                and (not album_names or _artists(song, "albumartist") == album_names)
            )

        return settled

    async def _note(
        self,
        candidate: Candidate,
        state: str,
        *,
        reason: str = "",
        retry: float | None = None,
    ) -> None:
        if self.report:
            return
        resolution = candidate.resolution
        await self.store.execute(
            "INSERT INTO artist_lists (key, song_id, kind, path, item_id, artist, state, reason,"
            " source, artists, album_id, updated_at, retry_at) VALUES"
            " (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
            " song_id = excluded.song_id, path = excluded.path, artist = excluded.artist,"
            " state = excluded.state, reason = excluded.reason, source = excluded.source,"
            " artists = excluded.artists, album_id = excluded.album_id,"
            " updated_at = excluded.updated_at, retry_at = excluded.retry_at"
            " WHERE artist_lists.phase = '' AND artist_lists.state <> 'written'",
            [
                candidate.key,
                candidate.key,
                candidate.kind,
                candidate.relative,
                candidate.subject.artist,
                state,
                reason,
                resolution.source if resolution else "",
                json.dumps(list(resolution.names)) if resolution and resolution.names else None,
                candidate.album_id,
                self.clock(),
                self.clock() + retry if retry is not None else None,
            ],
        )

    # --- taking it back ------------------------------------------------------------

    async def _take_back(self, candidates: Sequence[Candidate]) -> None:
        """Give back the files of an album that was not listed whole."""
        pairs = [(c, r) for c in candidates if (r := await self._written_row(c.key)) is not None]
        outcomes = await self._undo_many([r for _, r in pairs])
        for candidate, row in pairs:
            outcome = outcomes.get(str(row["key"]))
            if outcome == "":
                await self._note(candidate, "failed", reason="album", retry=FAILED_SECONDS)
            else:
                log.info("artist lists: a file of an album not listed whole is not given back"
                         " now: %s", outcome)  # fmt: skip

    async def _written_row(self, song_id: str) -> Any:
        return await self.store.fetchone(
            "SELECT * FROM artist_lists WHERE (key = ? OR song_id = ?) AND state = 'written'",
            [song_id, song_id],
        )

    async def _undo_all(self, summary: Summary, kinds: tuple[str, ...]) -> None:
        marks = ",".join("?" * len(kinds))
        rows = await self.store.fetchall(
            "SELECT * FROM artist_lists WHERE state = 'written'"  # noqa: S608
            f" AND kind IN ({marks}) AND phase = ''"
            " AND (retry_at IS NULL OR retry_at <= ?) ORDER BY album_id, key",
            [*kinds, self.clock()],
        )
        albums: dict[tuple[str, str], list[Any]] = {}
        for row in rows:
            albums.setdefault((str(row["album_id"] or row["key"]), row["kind"]), []).append(row)
        for index, group in enumerate(albums.values()):
            off = not self._enabled if "import" in kinds else not self._library
            if self.stopping() or not off:
                return  # (on again meanwhile)
            if index:
                await anyio.sleep(self.pause)
            for key, outcome in (await self._undo_many(group)).items():
                if outcome == "":
                    summary.undone += 1
                elif outcome == "declined":
                    summary.declined += 1
                else:
                    log.info("artist lists: %s not given back now: %s", key, outcome)
                    summary.failed[str(outcome)] += 1
                    # (asked again a while later, not every pass: a folder read-only again;
                    # a removed import's file, only after a week: it may stay removed)
                    wait = SKIPPED_SECONDS if outcome == "import_removed" else FAILED_SECONDS
                    await self.store.execute(
                        "UPDATE artist_lists SET retry_at = ? WHERE key = ? AND state = 'written'",
                        [self.clock() + wait, key],
                    )

    def _undone(self, row: Any) -> Callable[[dict[str, Any] | None, int], bool]:
        artist, album_id = str(row["artist"]), str(row["album_id"] or "")

        def settled(song: dict[str, Any] | None, size: int) -> bool:
            return (
                song is not None
                and not song.get("missing")
                and song.get("size") == size
                and (not album_id or str(song.get("albumId") or "") == album_id)
                and _artists(song) == [artist]
            )

        return settled

    async def _undo_many(self, rows: Sequence[Any]) -> dict[str, str]:
        """Take files' lists back, a few at once (one album's): each row's outcome is ""
        when done, "declined" when its file changed since or is gone (left alone for good),
        else why not now (tried again later)."""
        outcomes: dict[str, str] = {}
        imported = [r for r in rows if r["kind"] == "import"]
        if imported:
            if self.imports is None:
                outcomes |= {str(r["key"]): "import_busy" for r in imported}
            else:
                done = await self.imports.undo_many([(r, self._undone(r)) for r in imported])
                for row in imported:
                    outcome = done[str(row["key"])]
                    if outcome in ("declined", "gone"):
                        await self._decline(row, str(outcome))
                        outcome = "declined"
                    outcomes[str(row["key"])] = (
                        outcome.reason if isinstance(outcome, ListError) else str(outcome)
                    )
        entries: list[Entry] = []
        for row in (r for r in rows if r["kind"] == "file"):
            why = await self._undoable(row)
            if why:
                outcomes[str(row["key"])] = why
                continue
            entries.append(self._undo_entry(row))
        if entries:
            for key, given in (await self.files.write_many(entries)).items():
                outcomes[key] = given.reason if isinstance(given, ListError) else ""
        return outcomes

    async def _undoable(self, row: Any) -> str:
        """Why a library file's list is not taken back now ("": it is): "declined" when
        the file changed since or is confirmed gone; another reason when that cannot be
        told now (the library unreachable or not mounted, an error): asked again later."""
        relative = str(row["path"])
        try:
            path = self.files.absolute(relative)
        except ListError as exc:
            return exc.reason
        try:
            info = await anyio.to_thread.run_sync(os.lstat, path)
            current = await anyio.to_thread.run_sync(file_digest, path)
        except FileNotFoundError:
            if await anyio.to_thread.run_sync(self.files.there, path):
                await self._decline(row, "gone")
                return "declined"
            return "library_missing"
        except ListError as exc:
            return exc.reason
        except OSError as exc:
            return type(exc).__name__
        if info.st_nlink != 1 or current != (row["size_after"], row["sha_after"]):
            await self._decline(row, "changed")
            return "declined"
        return ""

    def _undo_entry(self, row: Any) -> Entry:
        relative = str(row["path"])
        previous = json.loads(row["previous"] or "{}")
        settled = self._undone(row)

        def check(made: Made) -> None:
            if made.raw_after != row["raw_before"]:
                raise ListError("not_restorable")

        def commit(made: Made) -> tuple[str, list[Any]]:
            return (
                "UPDATE artist_lists SET state = 'undone', phase = 'drop', updated_at = ?"
                " WHERE key = ?",
                [self.clock(), row["key"]],
            )

        return Entry(
            str(row["key"]),
            relative,
            {"values": dict.fromkeys(previous), "artist": None},
            lambda song, size: settled(song, size) and (song or {}).get("path") == relative,
            commit,
            "undo",
            check,
        )

    async def _decline(self, row: Any, reason: str) -> None:
        await self.store.execute(
            "UPDATE artist_lists SET state = 'declined', reason = ?, updated_at = ?"
            " WHERE key = ? AND phase = ''",
            [reason, self.clock(), row["key"]],
        )

    # --- music imports being published ---------------------------------------------

    async def publish_names(
        self, facts: Mapping[str, Any], skips: Skips | None = None
    ) -> tuple[list[str], str] | None:
        """The list of a file a music import is about to publish, from its probe's facts
        (``ImportLists.at_publish``): when the setting is on and a catalog gives one within
        a short time; the linked catalog item is not asked (it has none yet). Raises when
        the catalogs failed or were slow (``ImportLists.at_publish_batch`` counts it)."""
        if not self._enabled or self.report:
            return None
        artist = str(facts.get("artist") or "")
        if not maybe_joined(artist) or fold(artist) in NOT_ARTISTS:
            return None
        isrcs = facts.get("isrcs")
        subject = Subject(
            song_id="",
            artist=artist,
            title=str(facts.get("title") or ""),
            duration_ms=int(facts.get("duration_ms") or 0),
            isrcs=tuple(str(i).upper() for i in isrcs) if isinstance(isrcs, list) else (),
        )
        with anyio.fail_after(PUBLISH_SECONDS):  # (slow or failing: a miss for the batch)
            found = await self.sources.resolve(subject, link=False, skips=skips)
        if found.state == "resolved":
            return list(found.names), found.source
        return None
