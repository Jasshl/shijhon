"""Radio from catalog items: similar songs and similar artists.

``getSimilarSongs`` and ``getSimilarSongs2`` (JSON and XML) of a catalog song, album or
artist that is not in the library, and the ``similarArtist`` list of ``getArtistInfo`` and
``getArtistInfo2`` of a catalog artist. Navidrome knows nothing of such an item and its
answer is empty; ListenBrainz (unless it is turned off) and Last.fm - asked with the
Last.fm key the caller connected for discovery mixes, when there is one - name what is
similar, their lists in turn (``playlists/similar.py``), and the catalog and the library
say which of it they have:

- a song: the songs listed as similar to it, each looked up in the catalog (a search for
  its artist and title, the recording chosen by the rules discovery mixes use). A song
  with fewer than a handful is filled up with its artist's radio;
- an artist, and an album (its artist): the artist's top songs in the catalog, then those
  of the artists listed as similar, a few of each in turn;
- similar artists: the artists listed, as the library's artist of that name, else the
  catalog's.

A song the library has (an owned one, a placeholder) is the library's own entry for the
caller, the others are catalog songs as a search shows them; none twice, up to the
requested count. An item that is in the library is Navidrome's, as before: its ID is
mapped and Navidrome answers.

These are views. A list is saved like discographies and top songs (the same table, the
same age): a saved one answers at once and is refreshed in the background when old. Only
a radio without one waits, for the budget of the other views, and is then answered with
what is known so far; an artist's info never waits for its similar artists (resolving
them takes up to 15 catalog searches) and is answered with none. The list is completed in
the background, a few catalog requests at a time, and saved for the next request. Nothing
is asked while a client walks artist pages or trips the search guard, for a while after
every similarity source or the catalog failed, or for a caller without a Last.fm key while
ListenBrainz is off.

Three kinds of list are saved for an item, by the ending of their keys. Made with Last.fm
alone (no ending): for a caller with a key while ListenBrainz is off, and while
ListenBrainz's part is still to come - such a caller never waits for ListenBrainz, which is
asked in the background, and lists saved before ListenBrainz was added are of this kind
too. Made with both (``WITH_LISTENBRAINZ``): what callers with a key are shown while
ListenBrainz is on; when only a list of Last.fm alone is saved, it is shown and
ListenBrainz's part is added to it in the background (``_enrich_soon``): its similar songs,
or artists, looked up in the catalog and taken in turn with the list's, saved as the list
made with both - nothing asked of Last.fm again. Made without Last.fm
(``WITHOUT_LASTFM``): for a caller without a key, who waits for ListenBrainz (the only
source) within the request's budget, and is never shown Last.fm's songs. Whenever there is
nothing to show - and for JSONP and HEAD - the answer is the one from before: Navidrome's
own empty answer for similar songs, an empty list of similar artists. Nothing is written
to the library.

Credentials are checked before any work. Every request is logged without the names it is
about.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from typing import Any

import anyio
from pydantic import SecretStr

from shijhon.access import AccessService
from shijhon.catalog.addon import IDENTITY_CHANGED
from shijhon.catalog.base import Catalog, CatalogError, client_artwork_template
from shijhon.catalog.base import scope as catalog_scope
from shijhon.catalog.editions import merge_twins
from shijhon.catalog.model import CatalogArtist, CatalogRef, CatalogTrack
from shijhon.delivery import pacing
from shijhon.matching.normalize import artist_names, core_title, featured_names, fold
from shijhon.playlists.listenbrainz import ListenBrainz, ListeningUnavailable
from shijhon.playlists.repository import Repository
from shijhon.playlists.resolve import choose, searched
from shijhon.playlists.similar import Similarity, SimilarUnavailable, in_turn
from shijhon.proxy.app import Handler, HandlerResult, RequestContext
from shijhon.proxy.auth import Caller, CheckFailed
from shijhon.proxy.params import RestCall
from shijhon.proxy.responses import subsonic_ok
from shijhon.views.additions import (
    _NOBODY,
    SEARCH_LIMIT,
    TOP_SONGS,
    CatalogAdditions,
    _count,
    _printable,
    _term,
)
from shijhon.views.bursts import Burst, Bursts, Client
from shijhon.views.discographies import Named
from shijhon.views.entries import artist_entry, each, song_entry
from shijhon.views.ids import CatalogId, song_id
from shijhon.views.library_songs import Unknown
from shijhon.views.shown import ShownArtists

log = logging.getLogger(__name__)
Spawn = Callable[[Callable[[], Coroutine[Any, Any, None]]], None]

SONG_COUNT = 50  # getSimilarSongs' count when a client names none (Navidrome's default)
ARTIST_COUNT = 20  # ... getArtistInfo's
RADIO_SONGS = 50  # the songs of a saved list at most
SIMILAR_TRACKS = 50  # similar songs of a song, of both sources: a catalog search each
SIMILAR_ARTISTS = 15  # similar artists of an artist, of both sources
TITLES = 5  # an artist's top songs that tell ListenBrainz who it is
FEW_SONGS = 10  # a song's list with fewer is filled up with its artist's radio
OWN_SONGS = 10  # an artist's own top songs in its radio
THEIR_SONGS = 5  # ... and each similar artist's
LOOKUPS = 2  # catalog requests of one list at a time
# Lists made at a time, of each kind: a radio someone asked for never waits behind the
# similar artists of the pages a client opened, nor the other way round.
BUILDS = 2
# Seconds an artist's info waits, at most, for the catalog to say who it is now (a saved
# list is the catalog's as it identifies itself): never for the similar artists, which
# are made in the background for the page's next view.
INFO_WAIT = 1.0
# Of a request's budget, what is kept for telling which songs the library has (at most; a
# quarter of a smaller budget): a list is waited for until then.
ENTRY_SECONDS = 2.0
REST_SECONDS = 60.0  # nothing is asked for this long after the sources or the catalog failed
# The endings of the keys of the lists made without Last.fm (for a caller without a key),
# and of those made with both (a list made with Last.fm alone has none).
WITHOUT_LASTFM = ":listenbrainz"
WITH_LISTENBRAINZ = ":both"
ENRICHMENTS = 2  # lists that get ListenBrainz's part at a time
TOKEN_MARK = "radio:listenbrainz:token"  # noqa: S105 (a key: which token the lists had)
# What an ``Artist`` (getArtistInfo's similar artists) has of an ``ArtistID3``'s fields.
_ARTIST = ("id", "name", "coverArt", "artistImageUrl")


@dataclass(frozen=True)
class Seed:
    """What a radio starts from: an artist, or a song of theirs (``track``)."""

    artist: str  # one artist's name: the first one of a credit that names several
    ref: CatalogRef | None = None  # that artist's catalog item, when known
    track: CatalogTrack | None = None
    credit: str = ""  # a song's credit without its featured artists ("A & B")


class _Postponed(Exception):
    """Adding ListenBrainz's part waits for a later request (the reason is the message)."""


class _List[T]:
    """A list being made: what is known of it so far, in its order."""

    def __init__(self) -> None:
        self.done = anyio.Event()
        self.found: list[T | None] = []
        # Made with Last.fm alone while ListenBrainz's part is still to come: saved as such,
        # and ``after`` adds that part in the background (for the client that asked).
        self.waiting = False
        self.after: Callable[[Client], Awaitable[None]] | None = None
        # False: made from what ListenBrainz found while its token was refused or changed:
        # shown, not saved.
        self.kept = True

    def so_far(self) -> tuple[T, ...]:
        return tuple(item for item in self.found if item is not None)


class RadioReport:
    """One request for the instance log: never an artist's name or a title."""

    def __init__(self, call: RestCall, what: str) -> None:
        self.method = call.name
        self.client = _printable(call.client) or "?"
        self.what = what
        self.list = "not asked"
        self.shown = 0
        self.native = 0  # ... of them the library's own entries
        self.started = time.perf_counter()

    def __str__(self) -> str:
        return (
            f"{self.method} c={self.client}: {self.what}; list {self.list};"
            f" {self.shown} shown ({self.native} as the library's);"
            f" {time.perf_counter() - self.started:.2f}s"
        )


class CatalogRadio:
    def __init__(
        self,
        catalog: Catalog | None,
        additions: CatalogAdditions,
        *,
        enabled: bool = True,
        native: Callable[[CatalogId], Awaitable[str | None]] | None = None,
        shown: ShownArtists | None = None,
        sync: Bursts | None = None,
        spawn: Spawn | None = None,
        server: Callable[[], dict[str, object]] = dict,
    ) -> None:
        self.catalog = catalog
        # The other views' budget, saved lists, library songs and guards: one of each.
        self.additions = additions
        self.enabled = enabled  # ``[search] radio``
        self.native = native  # the native ID of a catalog item in the library
        self.shown = shown
        # Many different lists that would be asked for within a short time: a client
        # walking pages (the artist pages' settings, counted apart).
        self.sync = sync
        self.spawn = spawn  # background work for the app's lifetime
        self.server = server  # Navidrome's envelope fields
        self.clock = additions.clock
        # Set once playlists are there: Last.fm and ListenBrainz, and the Last.fm key of an
        # account (None: it connected none).
        self.similarity: Similarity | None = None
        self.key_of: Callable[[str], Awaitable[SecretStr | None]] | None = None
        self.resting_until = 0.0
        # ... and the lists made without Last.fm, after no source answered for one of them
        # (ListenBrainz failing for a caller without a key never holds Last.fm's lists).
        self.listening_resting_until = 0.0
        self._enriching: set[str] = set()  # the lists getting ListenBrainz's part now
        # The ListenBrainz token (its fingerprint) the saved lists made with ListenBrainz
        # were checked against; another one forgets them (``_token_check``).
        self._token_seen: str | None = None
        self._token_lock = anyio.Lock()
        self._making: dict[str, _List[Any]] = {}  # the lists being made, by their keys
        self._kinds: dict[str, str] = {}  # ... and what each is: songs, or artists

    def handlers(self, ids: Handler | None = None) -> dict[str, Handler]:
        """``getSimilarSongs`` and ``getSimilarSongs2``; a request left alone goes to ``ids``
        (catalog IDs in any method: the library's item, or Navidrome's empty answer)."""

        async def similar(call: RestCall, ctx: RequestContext) -> HandlerResult:
            try:
                found = await self.songs(call, ctx)
            except CheckFailed:  # no verdict on the credentials: the proxy's error
                raise
            except Exception as exc:
                log.warning("%s failed: %s", call.name, type(exc).__name__)
                found = None
            if found is None and ids is not None:
                return await ids(call, ctx)
            return found

        return {"getSimilarSongs": similar, "getSimilarSongs2": similar}

    # --- similar songs ----------------------------------------------------------------------

    async def songs(self, call: RestCall, ctx: RequestContext) -> HandlerResult:
        """None: the answer from before (the item's native ID, or Navidrome's empty
        answer)."""
        catalog = self.catalog
        if not self._on() or catalog is None:
            return None
        if call.fmt == "jsonp" or call.http_method == "HEAD":
            return None
        cid = CatalogId.parse(call.get("id"))
        if cid is None or cid.ref.catalog != catalog.key:
            return None
        count = _count(call.get("count"), SONG_COUNT)
        if count <= 0:
            return None
        caller = await ctx.caller()
        if caller is None:
            return None  # Navidrome answers with its own credential error
        if self.native is not None and await self.native(cid) is not None:
            return None  # in the library: Navidrome's own answer
        key = await self._key(caller)
        if key is None and not self._listening():
            return None
        kind = {"tr": "song", "al": "album", "ar": "artist"}[cid.kind]
        report = RadioReport(call, f"a catalog {kind}")
        try:
            return await self._songs(call, catalog, cid, count, caller, key, report)
        finally:
            log.info("%s", report)

    async def _songs(
        self,
        call: RestCall,
        catalog: Catalog,
        cid: CatalogId,
        count: int,
        caller: Caller,
        key: SecretStr | None,
        report: RadioReport,
    ) -> HandlerResult:
        deadline = self.clock() + self.additions.budget  # one budget for the whole request

        def left() -> float:
            return max(0.0, deadline - self.clock())

        seed: Seed | None = None
        try:
            with anyio.move_on_after(left()):
                seed = await self._seed(catalog, cid)
        except CatalogError as exc:
            log.info("radio: the catalog item unavailable: %s", exc.reason)
        if seed is None:
            report.list = "the item not told"
            return None
        scope = catalog_scope(catalog)
        store = self.additions.discographies
        await self._token_check()
        if seed.track is not None:
            base = f"{scope}:radio:song:{seed.track.ref}"
        else:
            base = f"{scope}:radio:artist:{fold(seed.artist)}"
        list_key = base + self._ending(key)
        saved = await store.get_songs(list_key) if store is not None else None
        alone = None  # a list of Last.fm alone, to add ListenBrainz's part to
        if saved is None and store is not None and list_key.endswith(WITH_LISTENBRAINZ):
            saved = alone = await store.get_songs(base)

        def enrich(songs: tuple[CatalogTrack, ...]) -> Callable[[Client], Awaitable[None]]:
            if seed.track is not None:
                return lambda client: self._enrich_songs(catalog, seed, list_key, songs, client)
            return lambda client: self._enrich_artist(catalog, key, seed, list_key, client)

        async def make(found: _List[CatalogTrack]) -> None:
            if seed.track is not None:
                await self._song_list(catalog, key, seed, found)
            else:
                await self._artist_list(catalog, key, seed, found)
            if found.waiting:
                found.after = enrich(found.so_far())

        songs, report.list = await self._list(
            list_key,
            (saved.songs, saved.fresh) if saved is not None else None,
            make,
            store.save_songs if store is not None else None,
            max(0.0, left() - min(ENTRY_SECONDS, self.additions.budget / 4)),
            (caller.username, call.client),
            "songs",
        )
        if alone is not None:
            self._enrich_soon(
                list_key, enrich(alone.songs), (caller.username, call.client), "songs"
            )
        # (A list still being made may name a recording twice, or the song itself.)
        songs = _distinct(songs or (), seed.track)
        if not songs:
            return None
        if self.additions.artwork is not None:  # their covers need no album request
            self.additions.artwork.tracks(songs)
        remember = getattr(catalog, "remember", None)
        if remember is not None:  # ... and a play no song lookup
            remember(songs)
        try:
            with anyio.fail_after(left()):
                added, report.native = await self._entries(call, songs, count)
        except (TimeoutError, Unknown) as exc:
            log.info("radio: the library's songs not told (%s)", type(exc).__name__)
            report.list += "; the library's songs not told"
            return None
        report.shown = len(added)
        if not added:
            return None
        element = "similarSongs2" if call.name == "getSimilarSongs2" else "similarSongs"
        return subsonic_ok(call, {element: {"song": added}}, self.server())

    async def _seed(self, catalog: Catalog, cid: CatalogId) -> Seed | None:
        if cid.kind == "tr":
            track = await catalog.song(cid.ref.id)
            name, ref = await self._first(catalog, track.artist, track.artists, track.artist_refs)
            if not _somebody(name) or not track.title.strip():
                return None
            return Seed(name, ref, track, _lead(track.artist))
        if cid.kind == "al":
            release = await catalog.album(cid.ref.id)
            name, ref = await self._first(
                catalog, release.artist, release.artists, release.artist_refs
            )
            return Seed(name, ref) if _somebody(name) else None
        known = self.shown.name_of(cid.ref) if self.shown is not None else None
        if known is None:
            known = (await catalog.artist(cid.ref.id)).name
        return Seed(known, cid.ref) if _somebody(known) else None

    @staticmethod
    async def _first(
        catalog: Catalog, credit: str, names: Sequence[str], refs: Sequence[CatalogRef]
    ) -> tuple[str, CatalogRef | None]:
        """The first artist of a credit, with its catalog item when known: Last.fm knows
        artists one by one, and a credit may name several ("A & B", or a duo of that
        name - only the catalog's artist items tell)."""
        if names:
            return names[0], refs[0] if len(refs) == len(names) else None
        if len(refs) > 1:
            try:
                return (await catalog.artist(refs[0].id)).name, refs[0]
            except CatalogError as exc:
                log.info("radio: a song's first artist unavailable: %s", exc.reason)
        return _lead(credit), refs[0] if len(refs) == 1 else None

    async def _entries(
        self, call: RestCall, songs: Sequence[CatalogTrack], count: int
    ) -> tuple[list[dict[str, Any]], int]:
        """(The first ``count`` of ``songs`` as entries, how many of them are the
        library's): each as the library has it (its own entry for the caller), else as a
        catalog song; none twice. :class:`Unknown` when Navidrome cannot say."""
        library = await self._library()
        library_songs = self.additions.library_songs
        held: set[str] = set()
        recordings: set[str] = set()
        chosen: list[tuple[CatalogTrack, str | None]] = []
        waiting = merge_twins(songs, self.additions.twins)  # clean/explicit twins
        # Only as many songs as the answer takes are matched with the library.
        while waiting and len(chosen) < count:
            room = count - len(chosen)
            batch, waiting = waiting[:room], waiting[room:]
            # Asked of Navidrome by title only for an artist the library has.
            ours = [t for t in batch if artist_names(t.artist) & library.keys()]
            songs_of = await library_songs.native(ours, search=True)
            songs_of.update(await library_songs.native(batch, search=False))
            # (A placeholder that plays an owned song is that song once more.)
            backing = await library_songs.backing(songs_of.values())
            for track in batch:
                song = songs_of.get(track.ref)
                same = {s for s in (song, backing.get(song or "")) if s}
                isrc = track.isrc.upper() if track.isrc else None
                if same & held or (isrc is not None and isrc in recordings):
                    continue  # the same recording again
                held |= same
                if isrc is not None:
                    recordings.add(isrc)
                chosen.append((track, song))
        wanted = [song for _, song in chosen if song is not None]
        entries = await library_songs.entries(call, wanted) if wanted else {}
        added: list[dict[str, Any]] = []
        natives = 0
        for track, song in chosen:
            if song is None:
                added += each([track], lambda t: song_entry(t, library=library), "song")
            elif song in entries:  # (else: a song Navidrome does not give this caller)
                natives += 1
                added.append(entries[song])
        return added, natives

    # --- similar artists --------------------------------------------------------------------

    async def similar_artists(
        self, call: RestCall, ctx: RequestContext, artist: CatalogArtist
    ) -> list[dict[str, Any]]:
        """The ``similarArtist`` entries of a catalog artist's info (``getArtistInfo``:
        ``Artist`` entries; ``getArtistInfo2``: ``ArtistID3`` entries, as a search shows
        artists); none when nothing is known in time."""
        catalog = self.catalog
        if not self._on() or catalog is None or not _somebody(artist.name):
            return []
        count = _count(call.get("count"), ARTIST_COUNT)
        caller = await ctx.caller()
        if count <= 0 or caller is None:
            return []
        key = await self._key(caller)
        if key is None and not self._listening():
            return []
        report = RadioReport(call, "a catalog artist's similar artists")
        try:
            return await self._similar_artists(call, catalog, artist, count, caller, key, report)
        finally:
            log.info("%s", report)

    async def _similar_artists(
        self,
        call: RestCall,
        catalog: Catalog,
        artist: CatalogArtist,
        count: int,
        caller: Caller,
        key: SecretStr | None,
        report: RadioReport,
    ) -> list[dict[str, Any]]:
        store = self.additions.discographies
        await self._token_check()
        base = _similar_key(catalog, artist.name)
        list_key = base + self._ending(key)
        saved = await store.get_artists(list_key) if store is not None else None
        alone = None  # a list of Last.fm alone, to add ListenBrainz's part to
        if saved is None and store is not None and list_key.endswith(WITH_LISTENBRAINZ):
            saved = alone = await store.get_artists(base)

        def enrich(named: tuple[Named, ...]) -> Callable[[Client], Awaitable[None]]:
            async def work(client: Client) -> None:
                await self._enrich_similar(catalog, artist.name, artist.ref, named, client)

            return work

        async def make(found: _List[Named]) -> None:
            await self._similar_list(catalog, key, artist.name, found, artist.ref)
            if found.waiting:
                found.after = enrich(found.so_far())

        named, report.list = await self._list(
            list_key,
            (saved.artists, saved.fresh) if saved is not None else None,
            make,
            store.save_artists if store is not None else None,
            min(INFO_WAIT, self.additions.budget),
            (caller.username, call.client),
            "artists",
            waits=False,  # an artist's page never waits for them: they are there next time
        )
        if alone is not None:
            self._enrich_soon(
                list_key, enrich(alone.artists), (caller.username, call.client), "artists"
            )
        own = await self._library_entries()
        entries: list[dict[str, Any]] = []
        theirs: list[CatalogArtist] = []
        for name, found in named or ():
            if len(entries) >= count:
                break
            if (native := own.get(fold(name))) is not None:
                entries.append(dict(native))
                report.native += 1
            elif found is not None:
                template = await client_artwork_template(catalog, found.artwork_template)
                shown = replace(found, artwork_template=template)
                added = each([shown], artist_entry, "artist")
                entries += added
                theirs += [found] if added else []
        if self.shown is not None and theirs:  # by name, for getTopSongs
            self.shown.shown(theirs)
        report.shown = len(entries)
        if call.name == "getArtistInfo":
            return [{k: e[k] for k in _ARTIST if k in e} for e in entries]
        return entries

    # --- the lists --------------------------------------------------------------------------

    async def _list[T](
        self,
        key: str,
        saved: tuple[tuple[T, ...], bool] | None,
        make: Callable[[_List[T]], Awaitable[None]],
        save: Callable[[str, tuple[T, ...]], Awaitable[None]] | None,
        wait: float,
        client: Client,
        kind: str,
        *,
        waits: bool = True,
    ) -> tuple[tuple[T, ...] | None, str]:
        """(A saved list at once - refreshed in the background when old; without one, what
        is known of it after ``wait`` seconds, the whole list when it was ready by then;
        and what happened, for the log). The list is made in the background and saved
        when it is complete; requests for the same list share one making. Nothing is
        made while the client is in a burst, while resting after a failure, or while
        ``BUILDS`` others of its ``kind`` are made: then the saved list as it is, or none.
        ``waits`` False: a list that is not saved is not waited for at all - what is known
        of it now (``wait`` then bounds only the check of the catalog's identity)."""
        items, fresh = saved if saved is not None else (None, False)
        guard = getattr(self.catalog, "validate_identity", None)
        if guard is not None:  # a saved list is the catalog's as it identifies itself now
            started = self.clock()
            unchecked = "saved (identity unchecked)" if items is not None else None
            try:
                with anyio.move_on_after(wait) as checking:
                    await guard()
            except CatalogError as exc:
                if exc.reason == IDENTITY_CHANGED:
                    return None, "failed"
                return items, unchecked or "failed"
            if checking.cancel_called:
                return items, unchecked or "timed out"
            wait = max(0.0, wait - (self.clock() - started))
        if fresh:
            return items, "saved"
        making: _List[T] | None = self._making.get(key)
        joined = making is not None
        if making is None:
            why = self._held(client, key, kind)
            if why is not None:
                return items, f"skipped: {why}" + (" (the saved list)" if items is not None else "")
            making = self._making[key] = _List()
            self._kinds[key] = kind
            run = self._run(key, making, make, save, client, kind)
            if self.spawn is not None:
                self.spawn(run)
            elif items is None:  # no background: made while its request waits
                with anyio.move_on_after(wait):
                    await run()
            else:
                self._making.pop(key, None)
                self._kinds.pop(key, None)
        if items is not None:
            return items, "saved (refreshing)"
        if waits:
            with anyio.move_on_after(wait):
                await making.done.wait()
        found = making.so_far()
        if making.done.is_set():
            return found, "joined" if joined else "asked"
        if not waits:
            return found, "made in the background" + (
                f" ({len(found)} known so far)" if found else ""
            )
        return found, "timed out" + (f" ({len(found)} known so far)" if found else "")

    def _held(self, client: Client, key: str, kind: str, *, noted: bool = False) -> str | None:
        """Why no list is made now, if so. ``noted``: the client's request for ``key`` was
        counted already (a burst is only looked at)."""
        resting = self.resting_until
        if key.endswith(WITHOUT_LASTFM):
            resting = max(resting, self.listening_resting_until)
        if self.clock() < resting or self.clock() < self.additions.resting_until:
            return "resting after a failure"
        if self.additions._quiet(client):
            return "sync or burst"
        if self.sync is not None and noted:
            return "sync or burst" if self.sync.bursting(client) else None
        if self.sync is not None:
            burst = self.sync.note(client, key)
            if burst is Burst.STARTED:
                log.info(
                    "radio: %s asked for %d different lists that are not saved within %gs"
                    " (a sync?); answering those without asking Last.fm or the catalog",
                    _printable(client[1]) or "a client",
                    self.sync.count(client),
                    self.sync.window,
                )
            if burst is not Burst.NO:
                return "sync or burst"
        if sum(1 for made in self._kinds.values() if made == kind) >= BUILDS:
            return "busy"
        return None

    def _run[T](
        self,
        key: str,
        making: _List[T],
        make: Callable[[_List[T]], Awaitable[None]],
        save: Callable[[str, tuple[T, ...]], Awaitable[None]] | None,
        client: Client,
        kind: str,
    ) -> Callable[[], Coroutine[Any, Any, None]]:
        """The making of a list: saved when it is complete, never after a failure (what
        was found until then is still shown to the requests that wait for it). One made with
        Last.fm alone while ListenBrainz's part is to come is saved under the plain key, the
        list made with both from an older one forgotten, and that part is added for
        ``client`` in the background."""
        store = self.additions.discographies

        async def run() -> None:
            made = False
            try:
                await make(making)
                if save is not None and making.kept:
                    if making.waiting:
                        await save(key.removesuffix(WITH_LISTENBRAINZ), making.so_far())
                        if store is not None:  # (made from the plain list before this one)
                            await store.forget(key)
                    else:
                        await save(key, making.so_far())
                made = True
            except SimilarUnavailable:
                log.info("radio: no similarity source answered")
                if key.endswith(WITHOUT_LASTFM):
                    self.listening_resting_until = self.clock() + REST_SECONDS
                else:
                    self.resting_until = self.clock() + REST_SECONDS
            except CatalogError as exc:
                log.info("radio: the catalog unavailable: %s", exc.reason)
                if exc.kind in ("unavailable", "rate_limited"):
                    self.resting_until = self.clock() + REST_SECONDS
            except Exception as exc:
                log.warning("radio: a list failed: %s", type(exc).__name__)
            finally:
                self._making.pop(key, None)
                self._kinds.pop(key, None)
                making.done.set()
            if made and making.waiting and making.after is not None:
                self._enrich_soon(key, making.after, client, kind, noted=True)

        return run

    async def _song_list(
        self, catalog: Catalog, key: SecretStr | None, seed: Seed, found: _List[CatalogTrack]
    ) -> None:
        """A song's radio: the songs listed as similar to it that the catalog has, in the
        lists' order; with few of them, its artist's radio after them."""
        assert self.similarity is not None and seed.track is not None
        listed = await self.similarity.tracks(key, _names(seed), SIMILAR_TRACKS)
        pairs, found.waiting, found.kept = listed.items, listed.waiting, listed.kept
        found.found = [None] * len(pairs)

        async def look_up(at: int) -> None:
            if len(found.so_far()) < RADIO_SONGS:
                found.found[at] = await self._song(catalog, *pairs[at])

        await _each(len(pairs), look_up)
        songs = _distinct(found.so_far(), seed.track)
        found.found = list(songs)
        if len(songs) < FEW_SONGS:
            more: _List[CatalogTrack] = _List()
            store = self.additions.discographies
            saved = None
            if store is not None:
                artist_key = f"{catalog_scope(catalog)}:radio:artist:{fold(seed.artist)}"
                saved = await store.get_songs(artist_key + self._ending(key))
                if saved is None and self._ending(key) == WITH_LISTENBRAINZ:
                    saved = await store.get_songs(artist_key)
            if saved is not None:
                more.found = list(saved.songs)
            else:
                await self._artist_list(catalog, key, Seed(seed.artist, seed.ref), more)
            songs = _distinct([*songs, *more.so_far()], seed.track)
        songs = await self._with_artists(catalog, songs[:RADIO_SONGS])
        found.found = list(songs)

    async def _song(self, catalog: Catalog, artist: str, title: str) -> CatalogTrack | None:
        """The catalog's recording of a song Last.fm names, as a search shows it: the
        original among several versions (``playlists/resolve.py``)."""
        term = _term(f"{artist} {title}")
        if not term:
            return None
        try:
            results = await catalog.search(term, SEARCH_LIMIT)
        except CatalogError as exc:
            if exc.kind != "not_found":
                raise
            return None
        wanted = {"artist": artist, "title": title, "prefer_original": True, "lenient_credit": True}
        by_id = {song_id(t.ref): t for t in results.songs}
        try:
            chosen = choose(wanted, searched(results.songs, by_release=True))
        except ValueError:  # a song of the answer without an artist or a title
            return None
        track = by_id.get(chosen.id) if chosen is not None else None
        if track is None or track.artist_refs:
            return track
        # Search results name no artist items: those of the search's own artists, by name.
        by_name = {fold(a.name): a.ref for a in results.artists}
        refs = [by_name.get(fold(p)) for p in featured_names(track.artist) or [track.artist]]
        named = tuple(r for r in refs if r is not None) if all(refs) else ()
        return replace(track, artist_refs=named)

    async def _with_artists(
        self, catalog: Catalog, songs: Sequence[CatalogTrack]
    ) -> tuple[CatalogTrack, ...]:
        """The songs with their artist items, asked for those a search did not name (as a
        search's answer asks; without them a song references its artists through itself)."""
        missing = tuple(t.ref.id for t in songs if not t.artist_refs)
        found: dict[str, tuple[CatalogRef, ...]] = {}
        if missing:
            try:
                found = await catalog.artists_of(missing, ())
            except CatalogError as exc:
                log.info("radio: artists of the songs unavailable: %s", exc.reason)
        return tuple(
            replace(t, artist_refs=found.get(f"songs:{t.ref.id}", t.artist_refs)) for t in songs
        )

    async def _artist_list(
        self, catalog: Catalog, key: SecretStr | None, seed: Seed, found: _List[CatalogTrack]
    ) -> None:
        """An artist's radio: the artist's top songs in the catalog and those of the
        similar artists, a few of each, in turn - the artist's first, the more similar
        before the less - until there are enough."""
        store = self.additions.discographies
        base = _similar_key(catalog, seed.artist)
        list_key = base + self._ending(key)
        saved = await store.get_artists(list_key) if store is not None else None
        if saved is None and store is not None and list_key.endswith(WITH_LISTENBRAINZ):
            saved = await store.get_artists(base)  # (ListenBrainz's part is still to come)
            found.waiting = saved is not None
        if saved is not None:
            named = saved.artists
        else:
            similar: _List[Named] = _List()
            await self._similar_list(catalog, key, seed.artist, similar, seed.ref)
            named = similar.so_far()
            found.waiting = similar.waiting
            if store is not None:
                await store.save_artists(base if similar.waiting else list_key, named)
        own = CatalogArtist(seed.ref, seed.artist) if seed.ref is not None else None
        artists: list[Named] = [(seed.artist, own), *named]
        lists: list[tuple[CatalogTrack, ...]] = []
        for start in range(0, len(artists), LOOKUPS):
            for at, songs in enumerate(await self._tops(catalog, artists[start : start + LOOKUPS])):
                most = OWN_SONGS if start + at == 0 else THEIR_SONGS
                lists.append(songs[:most])
            found.found = list(_distinct(_in_turn(lists)))
            if len(found.found) >= RADIO_SONGS:
                break
        found.found = found.found[:RADIO_SONGS]

    async def _tops(
        self, catalog: Catalog, artists: Sequence[Named]
    ) -> list[tuple[CatalogTrack, ...]]:
        """The top songs of each of a few artists, asked for together."""
        tops: list[tuple[CatalogTrack, ...]] = [()] * len(artists)

        async def top(at: int) -> None:
            name, artist = artists[at]
            if artist is None:  # one the library has: its catalog item, for its songs
                artist = await self._artist(catalog, name)
            if artist is not None:
                tops[at] = await self._top(catalog, artist)

        await _each(len(artists), top)
        return tops

    async def _top(self, catalog: Catalog, artist: CatalogArtist) -> tuple[CatalogTrack, ...]:
        """The artist's top songs: the list saved for ``getTopSongs``, else the catalog's."""
        store = self.additions.discographies
        if store is not None:
            saved = await store.get_songs(f"{catalog_scope(catalog)}:top:artist:{artist.ref}")
            if saved is not None:
                return saved.songs
        try:
            songs = await catalog.top_songs(artist.ref.id, TOP_SONGS)
        except CatalogError as exc:
            if exc.kind != "not_found":
                raise
            return ()  # the catalog has no list for this artist
        return tuple(
            replace(t, artist_refs=(artist.ref,))
            if not t.artist_refs and fold(t.artist) == fold(artist.name)
            else t
            for t in songs
        )

    async def _similar_list(
        self,
        catalog: Catalog,
        key: SecretStr | None,
        name: str,
        found: _List[Named],
        ref: CatalogRef | None = None,
    ) -> None:
        """The artists listed as similar, each with the catalog's artist of that name - not
        asked for an artist the library has: that one is shown as the library's. ``ref``:
        the artist's catalog item, when known (its top songs tell ListenBrainz who it is)."""
        assert self.similarity is not None
        listed = await self.similarity.artists(
            key, name, SIMILAR_ARTISTS, lambda: self._titles(catalog, name, ref)
        )
        found.waiting = listed.waiting
        found.found = list(await self._named(catalog, name, listed.items))

    async def _titles(self, catalog: Catalog, name: str, ref: CatalogRef | None) -> list[str]:
        """A few of the artist's top songs in the catalog (they tell ListenBrainz who it is);
        none when unavailable."""
        try:
            artist = CatalogArtist(ref, name) if ref is not None else None
            artist = artist or await self._artist(catalog, name)
            songs = await self._top(catalog, artist) if artist is not None else ()
        except CatalogError as exc:
            log.info("radio: an artist's top songs unavailable: %s", exc.reason)
            return []
        return [song.title for song in songs[:TITLES]]

    async def _named(self, catalog: Catalog, name: str, others: Sequence[str]) -> tuple[Named, ...]:
        """Each similar artist once (never the artist itself), with the catalog's artist of
        its name - not asked for one the library has: that one is shown as the library's."""
        names: dict[str, str] = {}
        for other in others:
            if _somebody(other) and fold(other) != fold(name):
                names.setdefault(fold(other), other)
        listed = list(names.values())
        library = await self._library()
        found: list[Named | None] = [None] * len(listed)

        async def look_up(at: int) -> None:
            if fold(listed[at]) in library:
                found[at] = (listed[at], None)
            elif (artist := await self._artist(catalog, listed[at])) is not None:
                found[at] = (artist.name, artist)

        await _each(len(listed), look_up)
        return tuple(item for item in found if item is not None)

    # --- ListenBrainz's part, added in the background ---------------------------------------

    def _enrich_soon(
        self,
        key: str,
        work: Callable[[Client], Awaitable[None]],
        client: Client,
        kind: str,
        *,
        noted: bool = False,
    ) -> None:
        """Add ListenBrainz's part to the list saved with Last.fm alone, in the background,
        as background work at the catalog (after the listeners' requests): not while
        ListenBrainz is off or resting (asked again by a later request), while the radio or
        the catalog rests after a failure, while the client is in a sync or a burst (the
        request is counted toward one, unless ``noted``), while the list is made or already
        getting it, or while ``ENRICHMENTS`` others are. The same guards are looked at again
        before the catalog is asked."""
        listening = self.similarity.listening if self.similarity is not None else None
        if listening is None or not listening.available or self.spawn is None:
            return
        if key in self._enriching or key in self._making or len(self._enriching) >= ENRICHMENTS:
            return
        if self._held(client, key, kind, noted=noted) not in (None, "busy"):
            return
        self._enriching.add(key)

        async def run() -> None:
            try:
                with pacing.urgent(pacing.WARM):
                    await work(client)
            except _Postponed as why:
                log.info("radio: ListenBrainz's part postponed: %s", why)
            except ListeningUnavailable:
                log.info("radio: ListenBrainz did not answer; its part is added later")
            except CatalogError as exc:
                log.info("radio: the catalog unavailable: %s", exc.reason)
                if exc.kind in ("unavailable", "rate_limited"):
                    self.resting_until = self.clock() + REST_SECONDS
            except Exception as exc:
                log.warning("radio: adding ListenBrainz's part failed: %s", type(exc).__name__)
            finally:
                self._enriching.discard(key)

        self.spawn(run)

    def _may_ask_catalog(self, client: Client, key: str) -> None:
        """:class:`_Postponed` when the catalog must not be asked for ``client`` now."""
        why = self._held(client, key, "", noted=True)
        if why not in (None, "busy"):
            raise _Postponed(why)

    def _listening_client(self) -> ListenBrainz:
        listening = self.similarity.listening if self.similarity is not None else None
        if listening is None:
            raise ListeningUnavailable
        return listening

    async def _token_check(self) -> None:
        """A ListenBrainz token other than the one the saved lists made with ListenBrainz were
        made with (or none where there was one, or the other way round): those lists are
        forgotten, the radio's and its similar artists', made with both or without Last.fm.
        Looked at once per token in this process; across restarts by a saved mark."""
        listening = self.similarity.listening if self.similarity is not None else None
        store = self.additions.discographies
        if listening is None or store is None:
            return
        token = listening.token_fingerprint() or "none"
        if token == self._token_seen:
            return
        async with self._token_lock:
            if token == self._token_seen:
                return
            if await store.get_name(TOKEN_MARK) != token:
                for ending in (WITH_LISTENBRAINZ, WITHOUT_LASTFM):
                    await store.forget_ending(":radio:", ending)
                await store.save_name(TOKEN_MARK, token)
            self._token_seen = token

    async def _enrich_songs(
        self,
        catalog: Catalog,
        seed: Seed,
        key: str,
        songs: Sequence[CatalogTrack],
        client: Client,
    ) -> None:
        """A song's radio made with Last.fm alone, with ListenBrainz's similar songs that the
        catalog has taken in turn with its own, saved under ``key`` - not when ListenBrainz's
        answer was found while its token was refused or changed (asked again later)."""
        store = self.additions.discographies
        similar, kept = await self._listening_client().similar_songs_kept(
            _names(seed), SIMILAR_TRACKS
        )
        if not kept:
            raise _Postponed("ListenBrainz's token was refused or changed")
        self._may_ask_catalog(client, key)
        pairs = [(artist, title) for artist, title, _ in similar]
        found: list[CatalogTrack | None] = [None] * len(pairs)

        async def look_up(at: int) -> None:
            if sum(1 for t in found if t is not None) < RADIO_SONGS:
                found[at] = await self._song(catalog, *pairs[at])

        await _each(len(pairs), look_up)
        theirs = [t for t in found if t is not None]
        merged = _distinct(_in_turn([list(songs), theirs]), seed.track)[:RADIO_SONGS]
        merged = await self._with_artists(catalog, merged)
        if store is not None:
            await store.save_songs(key, merged)

    async def _enrich_similar(
        self,
        catalog: Catalog,
        name: str,
        ref: CatalogRef | None,
        named: Sequence[Named],
        client: Client,
    ) -> tuple[Named, ...]:
        """An artist's similar artists listed by Last.fm alone, with ListenBrainz's taken in
        turn with them, saved as the list made with both; that list."""
        store = self.additions.discographies
        key = _similar_key(catalog, name) + WITH_LISTENBRAINZ
        self._may_ask_catalog(client, key)  # (the artist's top songs tell who it is)
        others = await self._listening_client().similar_artists(
            name, await self._titles(catalog, name, ref), SIMILAR_ARTISTS
        )
        known = {fold(n) for n, _ in named}
        self._may_ask_catalog(client, key)
        theirs = await self._named(catalog, name, [o for o in others if fold(o) not in known])
        merged = tuple(in_turn([list(named), list(theirs)], lambda n: fold(n[0])))
        merged = merged[: max(SIMILAR_ARTISTS, len(named))]
        if store is not None:
            await store.save_artists(key, merged)
        return merged

    async def _enrich_artist(
        self,
        catalog: Catalog,
        key: SecretStr | None,
        seed: Seed,
        list_key: str,
        client: Client,
    ) -> None:
        """An artist's radio made with Last.fm alone, made again from its similar artists
        with ListenBrainz's (those added first when missing), saved under ``list_key``."""
        store = self.additions.discographies
        if store is None:
            return
        base = _similar_key(catalog, seed.artist)
        if await store.get_artists(base + WITH_LISTENBRAINZ) is None:
            alone = await store.get_artists(base)
            await self._enrich_similar(
                catalog, seed.artist, seed.ref, alone.artists if alone is not None else (), client
            )
        self._may_ask_catalog(client, list_key)
        more: _List[CatalogTrack] = _List()
        await self._artist_list(catalog, key, seed, more)
        if not more.waiting:
            await store.save_songs(list_key, more.so_far())

    async def _artist(self, catalog: Catalog, name: str) -> CatalogArtist | None:
        """The catalog artist of exactly this name: the first one a search for it finds."""
        term = _term(name)
        if not term:
            return None
        try:
            results = await catalog.search(term, SEARCH_LIMIT)
        except CatalogError as exc:
            if exc.kind != "not_found":
                raise
            return None
        return next((a for a in results.artists if fold(a.name) == fold(name)), None)

    # --- the caller, the library ------------------------------------------------------------

    def _on(self) -> bool:
        return self.enabled and self.similarity is not None and self.key_of is not None

    def _listening(self) -> bool:
        """Whether ListenBrainz is asked (it needs no key)."""
        return self.similarity is not None and self.similarity.listening is not None

    def _ending(self, key: SecretStr | None) -> str:
        """The ending of the keys of the lists a caller is shown (see the module)."""
        if key is None:
            return WITHOUT_LASTFM
        return WITH_LISTENBRAINZ if self._listening() else ""

    async def _key(self, caller: Caller) -> SecretStr | None:
        """The caller's Last.fm key; None without one (or when that cannot be told)."""
        assert self.key_of is not None
        try:
            return await self.key_of(caller.username)
        except Exception as exc:
            log.info("radio: the caller's Last.fm key not told (%s)", type(exc).__name__)
            return None

    async def _library_entries(self) -> dict[str, dict[str, Any]]:
        """Navidrome's entries of the library's artists by folded name (none when
        unavailable)."""
        if self.additions.library_artists is None:
            return {}
        try:
            return await self.additions.library_artists.entries()
        except Exception as exc:
            log.info("library artists unavailable: %s", type(exc).__name__)
            return {}

    async def _library(self) -> dict[str, str]:
        return {name: str(e["id"]) for name, e in (await self._library_entries()).items()}


class CallerKeys:
    """The Last.fm key an account connected on the Playlists page (the one its discovery
    mixes use), by the name Navidrome accepted its credentials for."""

    def __init__(self, access: AccessService, repository: Repository) -> None:
        self.access = access  # the account's immutable ID: connections belong to it
        self.repository = repository

    async def __call__(self, username: str) -> SecretStr | None:
        principal = await self.access.grants(username)
        connection = await self.repository.connection(principal.user_id)
        if connection is None or not connection.history_key.get_secret_value():
            return None
        return connection.history_key


# --- helpers ------------------------------------------------------------------------------


def _names(seed: Seed) -> list[tuple[str, str]]:
    """A song as the catalog names it; then without its title's qualifiers ("(feat. X)"),
    and by its first artist alone: a source may know it under either."""
    assert seed.track is not None
    titles = dict.fromkeys((seed.track.title, core_title(seed.track.title)))
    credits = dict.fromkeys((seed.credit or seed.artist, seed.artist))
    return [(credit, title) for credit in credits for title in titles]


def _lead(credit: str) -> str:
    """A credit without its featured artists: the name Last.fm knows an artist by."""
    return (featured_names(credit) or [credit])[0].strip()


def _somebody(name: str) -> bool:
    """A name that stands for one artist (not "Various Artists")."""
    return bool(fold(name)) and fold(name) not in _NOBODY


def _similar_key(catalog: Catalog, name: str) -> str:
    return f"{catalog_scope(catalog)}:radio:similar:{fold(name)}"


async def _each(count: int, work: Callable[[int], Awaitable[None]]) -> None:
    """``work`` for each position in order, ``LOOKUPS`` at a time. A catalog failure ends
    the rest and is raised; what was found until then stays."""
    limiter = anyio.CapacityLimiter(LOOKUPS)
    failed: list[CatalogError] = []

    async def one(at: int) -> None:
        async with limiter:
            if failed:
                return
            try:
                await work(at)
            except CatalogError as exc:
                failed.append(exc)

    async with anyio.create_task_group() as group:
        for at in range(count):
            group.start_soon(one, at)
    if failed:
        raise failed[0]


def _distinct(
    songs: Sequence[CatalogTrack], seed: CatalogTrack | None = None
) -> tuple[CatalogTrack, ...]:
    """Each song and each recording (by its ISRC) once, in order; never the seed itself."""
    seen = {seed.ref} if seed is not None else set()
    recordings = {seed.isrc.upper()} if seed is not None and seed.isrc else set()
    kept: list[CatalogTrack] = []
    for song in songs:
        isrc = song.isrc.upper() if song.isrc else None
        if song.ref in seen or (isrc is not None and isrc in recordings):
            continue
        seen.add(song.ref)
        if isrc is not None:
            recordings.add(isrc)
        kept.append(song)
    return tuple(kept)


def _in_turn[T](lists: Sequence[Sequence[T]]) -> list[T]:
    """The lists' items in turn: each list's first, then each one's second..."""
    longest = max((len(items) for items in lists), default=0)
    return [items[at] for at in range(longest) for items in lists if at < len(items)]
