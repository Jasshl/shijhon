"""Pictures for library artists Navidrome has none for (``[catalog] artist_pictures``, on by
default, applied at once).

Navidrome looks for an artist's picture itself: an image uploaded for the artist, one in the
artist's folders, then its external services. Some artists end with none: the external
services have no picture under that name, or pick another artist of the same name. When
Navidrome has settled on none (``imageAbsent``) and nothing was uploaded for the artist,
Shijhon uploads the catalog artist's picture through Navidrome's own artist image upload, as
a person setting one in Navidrome would. Navidrome then serves, resizes and lists it for
every app, and shows it in its own interface, where it can be replaced or removed.

The catalog artist comes from the catalog items of the artist's songs in the library (the
releases Shijhon wrote or filled), else from a catalog search for the name: the one artist
of that name with a release (album, single or EP) titled as one of the artist's albums in
the library, or with two releases titled as two of the artist's song titles (singles, for
example); song titles are not looked up in the catalog. When several artists of that name
qualify, none is taken. A name matches also with its words in another order ("Utada
Hikaru", "Hikaru Utada"), never as a part of a longer one.

What Shijhon set is kept (``artist_pictures``), and it:

- never replaces a picture set in Navidrome: an artist whose uploaded image is not Shijhon's
  any more (another name; another content - Navidrome names an artist's upload by the
  artist alone -; or none) is left alone from then on, also after the setting was off. An
  upload whose content Navidrome has not read yet counts as Shijhon's only while the
  artist is as Shijhon left it (its ``updatedAt``); otherwise nothing is done until it can
  be told;
- takes its picture back when Navidrome's external services name one for the artist (its
  ``largeImageUrl``), unless Navidrome could not use that one when Shijhon set its own;
- with the setting off, removes the pictures it set, those still as it set them.

An upload is read back right after it (a few times, a few seconds apart) and recorded at
once, all within 40 seconds. One that cannot be recorded (Navidrome unreachable all that
while, or the record failing) is removed again: unrecorded, it would look like a person's.
When the removal fails too, Navidrome did not answer the upload and shows none yet, or the
40 seconds run out, the artist is kept as an upload whose outcome is not known (an
``unmatched`` record with the upload's time as ``uploaded_at``), settled about two minutes
later (else by the next pass): the artist's uploaded image is taken as Shijhon's only when
Navidrome changed the artist within 90 seconds of the upload (``updatedAt``) - a person
uploading one within those seconds is taken as negligible -; a later one is the person's
(``declined``, left alone); without one the record becomes a plain ``unmatched`` with its
retry. Turned off, such an artist's image is removed by the same rule. An upload Navidrome
answered with an error, or that was never sent (no connection, no sign-in), kept nothing:
nothing is removed or kept. A picture once recorded stays recorded whatever fails after it.

An artist without a catalog artist found is asked about again after a month; one whose
picture could not be given (an image Navidrome refused, a catalog address that failed),
after a day. A pass runs a few minutes after a start and then every few hours (at once when
the setting changes). It asks about every artist due, one at a time with a short pause
between them, so that a library's first pass is done within hours at a gentle pace; several
failures in a row end it (Navidrome or the catalog is down), and so does turning the
setting off (a new pass follows at once).
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import anyio

from shijhon.catalog.artwork import raster_type
from shijhon.catalog.base import Catalog, CatalogError
from shijhon.catalog.model import CatalogArtist, CatalogRef, artwork_url, release_from_data
from shijhon.delivery import pacing
from shijhon.matching.normalize import fold, title_key
from shijhon.navidrome.client import NavidromeError, NavidromeService
from shijhon.placeholders.tags import listed_artists
from shijhon.store import Store

log = logging.getLogger(__name__)

FIRST_PASS_SECONDS = 300.0  # after a start: startup work and the first requests go first
PASS_SECONDS = 6 * 3600.0
PAUSE_SECONDS = 1.0  # between two artists asked about in a pass
PROGRESS_EVERY = 100  # artists asked about between two progress lines of a long pass
UNMATCHED_SECONDS = 30 * 86400.0  # no catalog artist found: asked again after
FAILED_SECONDS = 86400.0  # giving the picture failed: tried again after
FAILURES_IN_A_ROW = 3  # end a pass: Navidrome or the catalog is down
HASH_WAIT_SECONDS = 20.0  # for Navidrome to read an image just uploaded
READ_PAUSES = (2.0, 3.0)  # between the reads of an upload just made, while they fail
UPLOAD_SECONDS = 40.0  # the upload, its reads and its record or removal at most (shielded)
SETTLE_SECONDS = 120.0  # an upload whose outcome is not known is told after
ADOPT_SECONDS = 90.0  # an artist changed this close to an unknown upload: Shijhon's upload
IMAGE_SIZE = 1200  # pixels: as the catalog's artist images are given to apps
# Navidrome's own artists, never a person or band of the catalog.
NOT_ARTISTS = frozenset(fold(n) for n in ("[Unknown Artist]", "Various Artists"))

SHIJHON, PERSON = "shijhon", "person"  # whose an artist's uploaded image is (None: unknown)
UNCERTAIN = "uncertain "  # an unknown upload's ``uploaded_at``: this, then its epoch time

# An upload's artist ID, name, catalog artist, external picture address and start time.
type _Unsure = tuple[str, str, str, str | None, float]


class _Failed(Exception):
    """Giving one artist its picture failed (tried again later)."""


class ArtistPictures:
    def __init__(
        self,
        navidrome: NavidromeService,
        catalog: Catalog | None,
        store: Store,
        *,
        enabled: bool = True,
        stopping: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
        pause: float | None = None,
    ) -> None:
        self.navidrome = navidrome
        self.catalog = catalog
        self.store = store
        self._enabled = enabled
        self.stopping = stopping
        self.clock = clock
        self.pause = PAUSE_SECONDS if pause is None else pause
        self._wake = anyio.Event()
        self._unsettled = False  # an upload whose outcome is not known was kept this pass

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """The dashboard's setting, applied at once: a pass follows."""
        if value != self._enabled:
            self._enabled = value
            self._wake.set()

    async def run(self) -> None:
        with anyio.move_on_after(FIRST_PASS_SECONDS):
            await self._wake.wait()
        while True:
            self._wake = anyio.Event()
            self._unsettled = False
            try:
                await self.once()
            except (NavidromeError, CatalogError) as exc:
                log.info("artist pictures: pass ended early: %s", exc)
            except Exception as exc:  # (the next pass goes on, and the setting still applies)
                # By its type only: messages may carry an add-on's link.
                log.warning("artist pictures: pass failed: %s", type(exc).__name__)
            if self._unsettled:  # (told soon, not after hours)
                with anyio.move_on_after(SETTLE_SECONDS):
                    await self._wake.wait()
                if not self._wake.is_set():
                    try:
                        await self.settle()
                    except Exception as exc:  # (the next pass tells)
                        log.info("artist pictures: not settled now: %s", type(exc).__name__)
            with anyio.move_on_after(PASS_SECONDS):
                await self._wake.wait()

    async def settle(self) -> None:
        """Tell the uploads whose outcome was not known (``_settle``), nothing else."""
        artists = {str(a["id"]): a for a in await self.navidrome.artists()}
        for ident, record in (await self._records()).items():
            if ident in artists and _uncertain(record):
                await self._settle(artists[ident], record)

    async def once(self) -> int:
        """One pass; returns how many pictures were set. Its catalog requests are
        background work: where they wait by urgency (an add-on's catalog), they go after
        the listeners'."""
        with pacing.urgent(pacing.WARM):
            return await self._pass()

    async def _pass(self) -> int:
        artists = {str(a["id"]): a for a in await self.navidrome.artists()}
        records = await self._records()
        if not self._enabled:
            await self._remove_all(artists, records)
            return 0
        for ident, record in records.items():
            if self.stopping():
                return 0
            artist = artists.get(ident)
            if (
                artist is None
                and record["state"] in ("unmatched", "withdrawn")
                and not _uncertain(record)
            ):
                # (gone from the library: asked anew if it comes back)
                await self.store.execute("DELETE FROM artist_pictures WHERE artist_id = ?", [ident])
            elif artist is not None and _uncertain(record):
                await self._settle(artist, record)
            elif artist is not None and record["state"] == "set":
                try:
                    await self._follow(artist, record)
                except NavidromeError as exc:
                    log.info("artist pictures: %s not checked: %s", ident, exc)
        if self.catalog is None:
            return 0
        records = await self._records()
        now = self.clock()
        wanted = [
            a
            for ident, a in artists.items()
            if a.get("imageAbsent")
            and not a.get("uploadedImage")
            and fold(str(a.get("name") or "")) not in NOT_ARTISTS
            and fold(str(a.get("name") or ""))
            and _askable(records.get(ident), now)
        ]
        set_now = failures = asked = 0
        for artist in wanted:
            if asked:
                await anyio.sleep(self.pause)
                if asked % PROGRESS_EVERY == 0:
                    log.info(
                        "artist pictures: %d set of %d asked about so far, %d to go",
                        set_now,
                        asked,
                        len(wanted) - asked,
                    )
            if self.stopping() or not self._enabled:
                break  # (stopping, or turned off: the next pass removes them)
            asked += 1
            ident, name = str(artist["id"]), str(artist.get("name") or "")
            try:
                if await self._give(artist):
                    set_now += 1
                failures = 0
            except Exception as exc:  # (also an unexpected one: the next artists go on)
                # (a picture recorded as set, or an upload not told yet, stays as it is)
                await self._note(
                    ident, name, "unmatched", retry_at=self.clock() + FAILED_SECONDS, keep_set=True
                )
                known = isinstance(exc, (_Failed, NavidromeError, CatalogError))
                # (another error by its type only: messages may carry an add-on's link)
                log.info(
                    "artist pictures: none set for %s now: %s",
                    ident,
                    exc if known else type(exc).__name__,
                )
                failures += 1
                if failures >= FAILURES_IN_A_ROW:
                    log.info(
                        "artist pictures: %d failures in a row; the next pass goes on", failures
                    )
                    break
        if asked:
            log.info(
                "artist pictures: %d set of %d artist(s) without one asked about",
                set_now,
                asked,
            )
        return set_now

    async def _records(self) -> dict[str, Any]:
        rows = await self.store.fetchall("SELECT * FROM artist_pictures")
        return {str(r["artist_id"]): r for r in rows}

    # --- what Shijhon set -------------------------------------------------------------

    async def _follow(self, artist: dict[str, Any], record: Any) -> None:
        """A picture Shijhon set: left alone when replaced or removed in Navidrome, taken
        back when Navidrome's external services name one."""
        ident = str(artist["id"])
        owner = _owner(artist, record)
        if owner == PERSON:
            await self._note(ident, record["name"], "declined", catalog_ref=record["catalog_ref"])
            log.info("artist pictures: %s's picture was changed in Navidrome; left alone", ident)
            return
        if owner is None:
            return  # (Navidrome has not read the image yet: next time)
        content = str(artist.get("imageHash") or "")
        if content and not record["image_hash"]:
            await self.store.execute(
                "UPDATE artist_pictures SET image_hash = ? WHERE artist_id = ?", [content, ident]
            )
        external = str(artist.get("largeImageUrl") or "")
        withdraw = external and external != (record["external_url"] or "")
        if withdraw and await self._remove_if_ours(ident, record):
            await self._note(ident, record["name"], "withdrawn", external_url=external)
            log.info(
                "artist pictures: %s has Navidrome's own picture now; Shijhon's removed", ident
            )

    async def _remove_if_ours(self, ident: str, record: Any) -> bool:
        """Remove Shijhon's picture, asked again right before: never a person's."""
        now = await self.navidrome.artist(ident)
        if now is None or _owner(now, record) != SHIJHON:
            return False
        await self.navidrome.remove_artist_image(ident)
        return True

    async def _settle(self, artist: dict[str, Any], record: Any) -> None:
        """An upload whose outcome was not known: the artist's uploaded image is taken as
        Shijhon's when Navidrome changed the artist within ``ADOPT_SECONDS`` of the upload
        (a person uploading one within those seconds is taken as negligible), else it is
        the person's (``declined``); without one, a plain ``unmatched`` with its retry."""
        ident = str(artist["id"])
        uploaded = str(artist.get("uploadedImage") or "")
        if not uploaded:
            await self.store.execute(
                "UPDATE artist_pictures SET uploaded_at = NULL WHERE artist_id = ?", [ident]
            )
            return
        if not _near_upload(artist, record):
            await self._note(ident, record["name"], "declined", catalog_ref=record["catalog_ref"])
            log.info("artist pictures: %s's picture is not Shijhon's; left alone", ident)
            return
        await self._note(
            ident,
            record["name"],
            "set",
            image=uploaded,
            image_hash=str(artist.get("imageHash") or "") or None,
            uploaded_at=str(artist.get("updatedAt") or ""),
            catalog_ref=record["catalog_ref"],
            external_url=record["external_url"],
        )
        log.info("artist pictures: the upload for %s is Shijhon's", ident)

    async def _remove_all(
        self, artists: dict[str, dict[str, Any]], records: dict[str, Any]
    ) -> None:
        """The setting is off: the pictures Shijhon set go, those still as it set them, and
        their records (on again, they are asked anew); a picture changed in Navidrome
        stays the person's choice."""
        removed = 0
        for ident, record in records.items():
            if self.stopping():
                return
            if record["state"] == "declined":
                continue
            if record["state"] == "set":
                try:
                    if ident in artists:
                        if await self._remove_if_ours(ident, record):
                            removed += 1
                        elif _owner(artists[ident], record) is None:
                            continue  # (not told apart yet: the next pass)
                        else:
                            await self._note(
                                ident, record["name"], "declined", catalog_ref=record["catalog_ref"]
                            )
                            continue
                    elif await self.navidrome.artist(ident) is not None:
                        continue  # (not in the library's list now: kept until it is)
                except NavidromeError as exc:
                    log.info("artist pictures: %s's picture not removed now: %s", ident, exc)
                    continue
            elif _uncertain(record):
                try:  # (told as the next pass would)
                    if ident in artists:
                        if not artists[ident].get("uploadedImage"):
                            pass
                        elif _near_upload(artists[ident], record):
                            await self.navidrome.remove_artist_image(ident)
                            removed += 1
                        else:
                            await self._note(
                                ident, record["name"], "declined", catalog_ref=record["catalog_ref"]
                            )
                            continue
                    elif await self.navidrome.artist(ident) is not None:
                        continue  # (not in the library's list now: kept until it is)
                except NavidromeError as exc:
                    log.info("artist pictures: %s's picture not removed now: %s", ident, exc)
                    continue
            await self.store.execute("DELETE FROM artist_pictures WHERE artist_id = ?", [ident])
        if removed:
            log.info("artist pictures: turned off; %d picture(s) Shijhon set removed", removed)

    # --- giving one -------------------------------------------------------------------

    async def _give(self, artist: dict[str, Any]) -> bool:
        ident, name = str(artist["id"]), str(artist.get("name") or "")
        assert self.catalog is not None
        found = await self.catalog_artist(ident, name)
        url = artwork_url(found.artwork_template, IMAGE_SIZE) if found else None
        if found is None or url is None:
            await self._note(ident, name, "unmatched", retry_at=self.clock() + UNMATCHED_SECONDS)
            return False
        image, _ = await self.catalog.artwork(url)
        content_type = raster_type(image)  # (the bytes say, never the catalog's claim)
        if content_type is None:
            raise _Failed("the catalog's picture is not an image")
        # Asked again right before: a picture set meanwhile in Navidrome stays.
        now = await self.navidrome.artist(ident)
        if now is None or now.get("uploadedImage") or not now.get("imageAbsent"):
            return False
        # Uploaded and recorded together: a stop in between would leave a picture that
        # looks like a person's, never taken back.
        external = str(artist.get("largeImageUrl") or "") or None
        unsure = (ident, name, str(found.ref), external, self.clock())
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(UPLOAD_SECONDS) as bounded:
                after = await self._upload(image, content_type, unsure)
            if bounded.cancelled_caught:  # (a stop waits no longer than that)
                await self._note_uncertain(unsure)
                raise _Failed("Navidrome took too long with the upload")
        uploaded_at = str(after.get("updatedAt") or "")
        log.info("artist pictures: set for %s from the catalog's %s", ident, found.ref)
        # Navidrome reads the image in the background: its content's hash, when it has
        # (else the next pass records it, ``_follow``).
        try:
            with anyio.move_on_after(HASH_WAIT_SECONDS):
                while not after.get("imageHash"):
                    await anyio.sleep(1.0)
                    after = await self.navidrome.artist(ident) or {}
        except NavidromeError as exc:
            log.info("artist pictures: %s's picture is checked next time: %s", ident, exc)
            return True
        content = str(after.get("imageHash") or "")
        if content and str(after.get("updatedAt") or "") == uploaded_at:
            await self.store.execute(
                "UPDATE artist_pictures SET image_hash = ?"
                " WHERE artist_id = ? AND image_hash IS NULL",
                [content, ident],
            )
        return True

    async def _upload(self, image: bytes, content_type: str, unsure: _Unsure) -> dict[str, Any]:
        """Upload the picture, read it back and record it as Shijhon's (the caller shields
        and bounds it); the artist as Navidrome answered right after. What cannot be
        recorded is removed again, or kept as an upload not told yet (``_remove_unrecorded``).
        ``unsure``: the artist, its name, the catalog artist, the external picture's address
        and the upload's time."""
        ident, name, catalog_ref, external, _ = unsure
        answered = True
        try:
            await self.navidrome.set_artist_image(ident, image, content_type)
        except NavidromeError as exc:
            if exc.status is not None or exc.unsent:
                raise  # (Navidrome answered with an error, or never got it: nothing kept)
            answered = False  # (no answer: it may have kept the picture)
        try:
            after = await self._read_back(ident)
            uploaded = str(after.get("uploadedImage") or "")
            if not uploaded and answered:
                raise _Failed("Navidrome did not keep the picture")
            if not uploaded:  # (it may still come: told soon)
                await self._note_uncertain(unsure)
                raise _Failed("Navidrome did not answer the upload")
            await self._note(
                ident,
                name,
                "set",
                image=uploaded,
                image_hash=str(after.get("imageHash") or "") or None,
                uploaded_at=str(after.get("updatedAt") or ""),
                catalog_ref=catalog_ref,
                external_url=external,
            )
        except _Failed:
            raise
        except Exception:
            await self._remove_unrecorded(unsure)
            raise
        return after

    async def _read_back(self, ident: str) -> dict[str, Any]:
        """The artist right after an upload: read again a few times while Navidrome fails
        (a read changes nothing)."""
        for pause in READ_PAUSES:
            try:
                return await self.navidrome.artist(ident) or {}
            except NavidromeError:
                await anyio.sleep(pause)
        return await self.navidrome.artist(ident) or {}

    async def _remove_unrecorded(self, unsure: _Unsure) -> None:
        """An upload that could not be recorded: removed, since unrecorded it would look
        like a person's (it replaced whatever the artist had uploaded, which was nothing a
        moment before); when that fails too, kept as an upload not told yet."""
        ident = unsure[0]
        try:
            await self.navidrome.remove_artist_image(ident)
            return
        except NavidromeError as exc:
            log.warning(
                "artist pictures: %s's upload could not be recorded or removed; the next pass"
                " tells: %s",  # (soon)
                ident,
                exc,
            )
        try:
            await self._note_uncertain(unsure)
        except Exception as exc:  # (the store fails too: the original error is raised)
            log.warning(
                "artist pictures: %s may keep a picture Shijhon could not record: %s",
                ident,
                type(exc).__name__,
            )

    async def _note_uncertain(self, unsure: _Unsure) -> None:
        """An upload whose outcome is not known: ``unmatched`` with the upload's time as
        ``uploaded_at`` (``_uncertain``), told soon (``_settle``). Never over a picture
        recorded as set (a record cut short by the bound may have been written)."""
        ident, name, catalog_ref, external, since = unsure
        await self._note(
            ident,
            name,
            "unmatched",
            uploaded_at=f"{UNCERTAIN}{since:.3f}",
            catalog_ref=catalog_ref,
            external_url=external,
            retry_at=self.clock() + FAILED_SECONDS,
            keep_set=True,
        )
        self._unsettled = True

    async def catalog_artist(self, ident: str, name: str) -> CatalogArtist | None:
        """The catalog artist of a library artist: from the catalog items of its songs in
        the library, else the one artist of its name the catalog finds with a release
        titled as one of the artist's albums, or two releases titled as two of its song
        titles (none when several of that name qualify)."""
        assert self.catalog is not None
        songs = await self.navidrome.artist_songs(ident)
        for ref, _ in (await self._items_of(songs, name)).most_common(3):
            try:
                artist = await self.catalog.artist(ref.id)
            except CatalogError as exc:
                if exc.kind != "not_found":
                    raise
                continue
            if same_name(artist.name, name):
                return artist
        albums = {title_key(str(s.get("album") or "")) for s in songs} - {""}
        titles = {title_key(str(s.get("title") or "")) for s in songs} - {""}
        results = await self.catalog.search(name, limit=10)
        qualified: dict[CatalogRef, CatalogArtist] = {}
        for candidate in (a for a in results.artists if same_name(a.name, name)):
            if candidate.ref in qualified:
                continue
            releases = {
                title_key(r.title) for r in await self.catalog.artist_releases(candidate.ref.id)
            }
            if releases & albums or len(releases & titles) >= 2:
                qualified[candidate.ref] = candidate
                if len(qualified) > 1:
                    return None  # (two artists of that name: which one cannot be told)
        return next(iter(qualified.values()), None)

    async def _items_of(self, songs: Iterable[dict[str, Any]], name: str) -> Counter[CatalogRef]:
        """The current catalog's items credited as ``name`` (also with its words in another
        order) on these songs' catalog tracks and releases (paired by position with the
        credit's artists)."""
        assert self.catalog is not None
        ids = [str(s["id"]) for s in songs if s.get("id")]
        found: Counter[CatalogRef] = Counter()
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))  # (bound values only)
            rows = await self.store.fetchall(
                "SELECT l.track_ref, r.data FROM track_links l JOIN releases r"  # noqa: S608
                f" ON r.ref = l.release_ref WHERE l.song_id IN ({marks})",
                chunk,
            )
            for row in rows:
                try:
                    release = release_from_data(json.loads(row["data"]))
                except (ValueError, KeyError, TypeError):
                    continue
                credits = [(release.artists, release.artist_refs, release.artist, "")]
                track = next((t for t in release.tracks if str(t.ref) == row["track_ref"]), None)
                if track is not None:
                    credits.append((track.artists, track.artist_refs, track.artist, track.title))
                for names, refs, credit, title in credits:
                    listed = listed_artists(names, refs, credit, title) or [credit]
                    if len(listed) != len(refs):
                        continue
                    for person, ref in zip(listed, refs, strict=True):
                        if same_name(person, name) and ref.catalog == self.catalog.key:
                            found[ref] += 1
        return found

    async def _note(
        self,
        ident: str,
        name: str,
        state: str,
        *,
        image: str | None = None,
        image_hash: str | None = None,
        uploaded_at: str | None = None,
        catalog_ref: str | None = None,
        external_url: str | None = None,
        retry_at: float | None = None,
        keep_set: bool = False,
    ) -> None:
        """Record an artist's state (``keep_set``: unless a picture is recorded as set, or
        an upload is not told yet)."""
        values = {
            "artist_id": ident,
            "name": name,
            "state": state,
            "image": image,
            "image_hash": image_hash,
            "uploaded_at": uploaded_at,
            "catalog_ref": catalog_ref,
            "external_url": external_url,
            "updated_at": self.clock(),
            "retry_at": retry_at,
        }
        columns = ", ".join(values)
        updates = ", ".join(f"{c} = excluded.{c}" for c in values if c != "artist_id")
        await self.store.execute(
            f"INSERT INTO artist_pictures ({columns}) VALUES ({', '.join('?' * len(values))})"  # noqa: S608
            f" ON CONFLICT (artist_id) DO UPDATE SET {updates}"
            + (
                " WHERE artist_pictures.state <> 'set' AND artist_pictures.uploaded_at IS NULL"
                if keep_set
                else ""
            ),
            list(values.values()),
        )


def _owner(artist: dict[str, Any], record: Any) -> str | None:
    """Whose the artist's uploaded image is: Shijhon's (as recorded), a person's, or None
    while that cannot be told (Navidrome has not read an image of Shijhon's name yet, and the
    artist changed since Shijhon's upload)."""
    if str(artist.get("uploadedImage") or "") != record["image"]:
        return PERSON
    content = str(artist.get("imageHash") or "")
    as_left = str(artist.get("updatedAt") or "") == str(record["uploaded_at"] or "")
    if record["image_hash"]:
        if content:
            return SHIJHON if content == record["image_hash"] else PERSON
        return SHIJHON if as_left else None
    if as_left:
        return SHIJHON
    return PERSON if content else None


def _uncertain(record: Any) -> bool:
    """Whether a record is an upload whose outcome is not known (``_note_uncertain``)."""
    return bool(record["state"] == "unmatched" and record["uploaded_at"])


def _near_upload(artist: dict[str, Any], record: Any) -> bool:
    """Whether Navidrome changed the artist (``updatedAt``) within ``ADOPT_SECONDS`` of an
    upload whose outcome was not known; no if either time cannot be read."""
    try:
        since = float(str(record["uploaded_at"]).removeprefix(UNCERTAIN))
        changed = datetime.fromisoformat(str(artist.get("updatedAt") or "")).timestamp()
    except ValueError:
        return False
    return abs(changed - since) <= ADOPT_SECONDS


def same_name(one: str, other: str) -> bool:
    """The same name, also with its words in another order ("Utada Hikaru", "Hikaru Utada")."""
    if fold(one) == fold(other):
        return bool(fold(one))
    words = sorted(w for w in (fold(x) for x in one.split()) if w)
    return len(words) > 1 and words == sorted(w for w in (fold(x) for x in other.split()) if w)


def _askable(record: Any, now: float) -> bool:
    """Whether an artist without a picture is asked about: never before, or found without a
    catalog artist (or failed) a while ago, or Navidrome's own picture taken back but not
    usable after all; never once a picture was changed in Navidrome."""
    if record is None:
        return True
    if record["state"] == "unmatched":
        return float(record["retry_at"] or 0) <= now
    return bool(record["state"] == "withdrawn")
